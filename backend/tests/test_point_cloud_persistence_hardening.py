from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
from fastapi import HTTPException, Request, UploadFile

from app.routers import point_cloud_api
from app.services.conversion.canonical_json import canonical_json_text, canonical_sha256
from app.services.operations.quotas import OperationQuotaRegistry
from app.services.point_cloud.source_reader import (
    E57ScanSummary,
    E57SourceInspection,
    parser_capabilities as build_parser_capabilities,
)


@contextmanager
def _connection(database: Path):
    connection = sqlite3.connect(database)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _schema(database: Path) -> None:
    with _connection(database) as connection:
        connection.execute(
            """
            CREATE TABLE bridge_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id TEXT NOT NULL,
                state_code TEXT NOT NULL,
                bridge_number TEXT NOT NULL,
                attachment_type TEXT NOT NULL,
                filename TEXT NOT NULL,
                filepath TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
                sha256 TEXT
            )
            """
        )
        connection.execute(
            "CREATE TABLE point_cloud_runs (workspace_id TEXT NOT NULL, source_sha256 TEXT NOT NULL)"
        )


@pytest.fixture
def point_cloud_store(test_workspace: Path, monkeypatch: pytest.MonkeyPatch):
    data_root = test_workspace / "customer-data"
    attachments = data_root / "attachments"
    temp = data_root / "temp"
    attachments.mkdir(parents=True)
    temp.mkdir()
    database = data_root / "bridge_data.db"
    _schema(database)
    monkeypatch.setattr(point_cloud_api, "DATA_DIR", data_root)
    monkeypatch.setattr(point_cloud_api, "ATTACHMENTS_DIR", attachments)
    monkeypatch.setattr(point_cloud_api, "TEMP_DIR", temp)
    monkeypatch.setattr(point_cloud_api, "RAG_WORKING_DIR", data_root / "rag_storage")
    monkeypatch.setattr(point_cloud_api, "USER_DB", database)
    monkeypatch.setattr(point_cloud_api, "POINT_CLOUD_WORKSPACE_ID", "workspace-a")
    capability_builder = point_cloud_api.parser_capabilities
    monkeypatch.setattr(
        point_cloud_api,
        "parser_capabilities",
        lambda: capability_builder(pye57_available=False),
    )
    monkeypatch.setattr(
        point_cloud_api,
        "_POINT_CLOUD_LOCKS",
        tuple(asyncio.Lock() for _ in range(64)),
    )
    monkeypatch.setattr(
        point_cloud_api,
        "DEFAULT_OPERATION_QUOTAS",
        OperationQuotaRegistry(
            operation_limits={"point_cloud": 8},
            workspace_limit=8,
            owner_limit=8,
        ),
    )
    return data_root, database


def _request(email: str = "editor@example.com", method: str = "DELETE") -> Request:
    request = Request(
        {
            "type": "http",
            "method": method,
            "path": "/",
            "headers": [],
        }
    )
    request.state.user = SimpleNamespace(email=email, role="editor")
    return request


def _insert_cloud(
    data_root: Path,
    database: Path,
    *,
    workspace_id: str,
    payload: bytes,
    state_code: str = "34",
    bridge_number: str = "same-bridge",
    sha256: str | None = "auto",
    filepath: str | None = None,
) -> tuple[int, Path]:
    object_path = data_root / "objects" / workspace_id / f"{hashlib.sha256(payload).hexdigest()}.e57"
    object_path.parent.mkdir(parents=True, exist_ok=True)
    object_path.write_bytes(payload)
    stored_path = filepath or object_path.relative_to(data_root).as_posix()
    digest = hashlib.sha256(payload).hexdigest() if sha256 == "auto" else sha256
    with _connection(database) as connection:
        cursor = connection.execute(
            """
            INSERT INTO bridge_attachments
                (workspace_id, state_code, bridge_number, attachment_type,
                 filename, filepath, size_bytes, sha256)
            VALUES (?, ?, ?, 'point_cloud', 'scan.e57', ?, ?, ?)
            """,
            (
                workspace_id,
                state_code,
                bridge_number,
                stored_path,
                len(payload),
                digest,
            ),
        )
        return int(cursor.lastrowid), object_path


def _analysis_result(labels: tuple[int, ...] = (1, 0, 2), *, marker: str = "run"):
    encoded = base64.b64encode(np.asarray(labels, dtype=np.uint8).tobytes()).decode("ascii")
    return {
        "status": "success",
        "marker": marker,
        "labels_b64u8": encoded,
        "classes": [
            {"id": 1, "color_rgb": [1.0, 0.0, 0.0]},
            {"id": 2, "color_rgb": [0.0, 0.0, 1.0]},
        ],
    }


def _points() -> np.ndarray:
    return np.asarray(
        [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0], [2.0, 2.0, 2.0]],
        dtype=np.float32,
    )


def test_two_workspaces_with_same_bridge_have_distinct_rows_paths_and_delete_scope(
    point_cloud_store, monkeypatch: pytest.MonkeyPatch
):
    data_root, database = point_cloud_store
    _id_a, path_a = _insert_cloud(
        data_root, database, workspace_id="workspace-a", payload=b"alpha"
    )
    _id_b, path_b = _insert_cloud(
        data_root, database, workspace_id="workspace-b", payload=b"bravo"
    )

    monkeypatch.setattr(point_cloud_api, "POINT_CLOUD_WORKSPACE_ID", "workspace-a")
    root_a = point_cloud_api._bridge_cloud_root("34", "same-bridge")
    assert point_cloud_api._raw_cloud_row("34", "same-bridge")[2] == "scan.e57"
    asyncio.run(
        point_cloud_api.delete_point_cloud(
            "34", "same-bridge", _request(), "superseded source"
        )
    )

    monkeypatch.setattr(point_cloud_api, "POINT_CLOUD_WORKSPACE_ID", "workspace-b")
    root_b = point_cloud_api._bridge_cloud_root("34", "same-bridge")
    remaining = point_cloud_api._raw_cloud_row("34", "same-bridge")

    assert root_a != root_b
    assert not path_a.exists()
    assert path_b.is_file()
    assert remaining is not None
    with _connection(database) as connection:
        assert connection.execute(
            "SELECT workspace_id FROM bridge_attachments"
        ).fetchall() == [("workspace-b",)]


def test_concurrent_same_bridge_uploads_are_serialized_and_replacement_is_rejected(
    point_cloud_store,
):
    data_root, database = point_cloud_store
    payloads = (b"ASTM-E57first-point-cloud", b"ASTM-E57second-point-cloud")

    async def upload_both():
        files = [
            UploadFile(filename="customer scan.e57", file=io.BytesIO(payload))
            for payload in payloads
        ]
        return await asyncio.gather(
            *(
                point_cloud_api.upload_point_cloud(
                    "34", "same-bridge", _request(method="POST"), file
                )
                for file in files
            ),
            return_exceptions=True,
        )

    responses = asyncio.run(upload_both())
    with _connection(database) as connection:
        rows = connection.execute(
            """
            SELECT id, workspace_id, filepath, sha256
            FROM bridge_attachments
            WHERE workspace_id='workspace-a' AND state_code='34'
              AND bridge_number='same-bridge' AND attachment_type='point_cloud'
            """
        ).fetchall()

    assert len(rows) == 1
    successful = [item for item in responses if isinstance(item, dict)]
    rejected = [item for item in responses if isinstance(item, HTTPException)]
    attachment_id, workspace_id, stored_path, digest = rows[0]
    raw_path = data_root / stored_path
    assert workspace_id == "workspace-a"
    assert len(successful) == 1
    assert len(rejected) == 1
    assert rejected[0].status_code == 409
    assert rejected[0].detail == "point_cloud_delete_required_before_replace"
    assert attachment_id == successful[0]["source_attachment_id"]
    assert digest in {hashlib.sha256(payload).hexdigest() for payload in payloads}
    assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == digest
    assert re.fullmatch(r"[0-9a-f]{32}\.e57", raw_path.name)
    assert not list(data_root.rglob("*.uploading"))
    assert len(list(raw_path.parent.glob("*.e57"))) == 1


def test_concurrent_same_name_uploads_to_different_bridges_have_unique_objects_and_staging(
    point_cloud_store,
):
    data_root, database = point_cloud_store

    async def upload_both():
        return await asyncio.gather(
            point_cloud_api.upload_point_cloud(
                "34",
                "bridge-a",
                _request(method="POST"),
                UploadFile(filename="same.e57", file=io.BytesIO(b"ASTM-E57cloud-a")),
            ),
            point_cloud_api.upload_point_cloud(
                "34",
                "bridge-b",
                _request(method="POST"),
                UploadFile(filename="same.e57", file=io.BytesIO(b"ASTM-E57cloud-b")),
            ),
        )

    responses = asyncio.run(upload_both())
    with _connection(database) as connection:
        rows = connection.execute(
            "SELECT bridge_number,filepath,sha256 FROM bridge_attachments "
            "WHERE workspace_id='workspace-a' ORDER BY bridge_number"
        ).fetchall()

    assert [row[0] for row in rows] == ["bridge-a", "bridge-b"]
    assert len({row[1] for row in rows}) == 2
    assert {row[2] for row in rows} == {
        hashlib.sha256(b"ASTM-E57cloud-a").hexdigest(),
        hashlib.sha256(b"ASTM-E57cloud-b").hexdigest(),
    }
    assert len({item["source_attachment_id"] for item in responses}) == 2
    assert not list(data_root.rglob("*.uploading"))


def test_source_upload_rejects_signature_mismatch_without_catalog_or_file_residue(
    point_cloud_store,
):
    data_root, database = point_cloud_store
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            point_cloud_api.upload_point_cloud(
                "34",
                "bad-signature",
                _request(method="POST"),
                UploadFile(filename="fake.e57", file=io.BytesIO(b"LASF-not-e57")),
            )
        )
    assert caught.value.status_code == 400
    assert caught.value.detail["code"] == "source_signature_mismatch"
    with _connection(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM bridge_attachments WHERE bridge_number='bad-signature'"
        ).fetchone()[0] == 0
    assert not list(data_root.rglob("*bad-signature*"))
    assert not list(data_root.rglob("*.source.json"))


def test_reviewed_source_declaration_is_hash_bound_and_public_profile_hides_actor(
    point_cloud_store,
):
    data_root, _database = point_cloud_store
    response = asyncio.run(
        point_cloud_api.upload_point_cloud(
            "34",
            "reviewed-source",
            _request(method="POST"),
            UploadFile(filename="reviewed.e57", file=io.BytesIO(b"ASTM-E57reviewed")),
            horizontal_unit="metre",
            vertical_unit="metre",
            frame_kind="engineering_local",
            up_axis="+z",
            declaration_evidence="NJDOT control note PC-42",
            declaration_reviewed=True,
        )
    )
    public = response["source_profile"]
    assert public["coordinate_declaration"]["reviewed"] is True
    assert public["coordinate_declaration"]["frame"]["metric_ready"] is True
    assert "reviewed_by" not in str(public)
    assert "example.com" not in str(public)
    assert len(public["receipt_sha256"]) == 64
    info = point_cloud_api.point_cloud_info("34", "reviewed-source")
    assert info["source_profile"] == public
    row = point_cloud_api._raw_cloud_row("34", "reviewed-source")
    source = data_root / row[1]
    receipt_path = point_cloud_api._source_receipt_path(source)
    assert receipt_path.is_file()
    internal = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert len(internal["coordinate_declaration"]["reviewed_by_sha256"]) == 64
    assert internal["source_sha256"] == hashlib.sha256(b"ASTM-E57reviewed").hexdigest()


def test_reviewed_source_declaration_requires_evidence_and_leaves_no_residue(
    point_cloud_store,
):
    data_root, database = point_cloud_store
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            point_cloud_api.upload_point_cloud(
                "34",
                "missing-evidence",
                _request(method="POST"),
                UploadFile(filename="reviewed.e57", file=io.BytesIO(b"ASTM-E57reviewed")),
                horizontal_unit="metre",
                vertical_unit="metre",
                frame_kind="engineering_local",
                up_axis="+z",
                declaration_reviewed=True,
            )
        )
    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "point_cloud_source_evidence_required"
    with _connection(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM bridge_attachments WHERE bridge_number='missing-evidence'"
        ).fetchone()[0] == 0
    assert not list(data_root.rglob("*.source.json"))


def test_reviewed_multiscan_merge_requires_and_persists_registration_receipt(
    point_cloud_store,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        point_cloud_api,
        "parser_capabilities",
        lambda: build_parser_capabilities(pye57_available=True),
    )
    transform_hash = "a" * 64
    inspection = E57SourceInspection(
        source_sha256=hashlib.sha256(b"ASTM-E57multi").hexdigest(),
        source_size_bytes=len(b"ASTM-E57multi"),
        total_scan_count=2,
        total_declared_point_count=20,
        scans=(
            E57ScanSummary(0, 10, ("cartesianX", "cartesianY", "cartesianZ"), "b" * 64),
            E57ScanSummary(1, 10, ("cartesianX", "cartesianY", "cartesianZ"), "c" * 64),
        ),
        registration_transform_sha256=transform_hash,
    )
    monkeypatch.setattr(point_cloud_api, "inspect_e57_source", lambda *_args, **_kwargs: inspection)

    response = asyncio.run(
        point_cloud_api.upload_point_cloud(
            "34",
            "multi-scan",
            _request(method="POST"),
            UploadFile(filename="multi.e57", file=io.BytesIO(b"ASTM-E57multi")),
            horizontal_unit="metre",
            vertical_unit="metre",
            frame_kind="engineering_local",
            up_axis="+z",
            declaration_evidence="registered survey targets",
            declaration_reviewed=True,
            selected_scan_indices="0,1",
            registration_reference_scan=0,
            registration_rms_metres=0.012,
            registration_maximum_residual_metres=0.024,
            registration_method="survey targets",
            registration_reviewed=True,
        )
    )
    profile = response["source_profile"]
    assert profile["metric_processing_authorized"] is True
    assert profile["selected_scan_indices"] == [0, 1]
    assert profile["registration"]["transform_sha256"] == transform_hash
    assert profile["registration"]["reviewed"] is True
    assert profile["source_inspection"]["total_scan_count"] == 2


def test_processed_pointer_and_manifest_bind_source_profile_workspace_and_snapshot(
    point_cloud_store,
):
    data_root, database = point_cloud_store
    source_attachment_id, _source_path = _insert_cloud(
        data_root,
        database,
        workspace_id="workspace-a",
        payload=b"source-object",
    )
    source_row = point_cloud_api._raw_cloud_row("34", "same-bridge")
    source_sha256 = hashlib.sha256(b"source-object").hexdigest()
    profile_a = {"analysis_version": 2, "model_mode": "clearance", "use_vlm": False}
    snapshot = {
        "workspace_id": "workspace-a",
        "state_code": 34,
        "bridge_number": "same-bridge",
        "data_year": 2026,
        "snapshot_revision_id": "revision-1",
        "revision_number": 3,
        "canonical_sha256": "a" * 64,
        "profile_id": "snbi-2026",
        "rule_bundle_sha256": "b" * 64,
        "published_at": "2026-08-13T00:00:00Z",
    }

    first = point_cloud_api._save_processed(
        "34",
        "same-bridge",
        _points(),
        _analysis_result(marker="first"),
        source_row=source_row,
        analysis_profile=profile_a,
        snapshot_identity=snapshot,
    )
    first_root = point_cloud_api._processed_paths(
        "34", "same-bridge", first["source_generation"]
    )[0]
    first_manifest = (first_root / "analysis.json").read_bytes()
    first_npz = (first_root / "processed_cloud.npz").read_bytes()

    knowledge_pack_json = json.dumps(
        {
            "schema_version": "bridgeguard-point-cloud-knowledge-pack-v1",
            "scope": {"bridge": snapshot},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    knowledge_pack_sha256 = hashlib.sha256(
        knowledge_pack_json.encode("utf-8")
    ).hexdigest()
    profile_b = {
        **profile_a,
        "model_mode": "inventory",
        "knowledge_pack_sha256": knowledge_pack_sha256,
    }
    second = point_cloud_api._save_processed(
        "34",
        "same-bridge",
        _points(),
        _analysis_result(marker="second"),
        source_row=source_row,
        analysis_profile=profile_b,
        snapshot_identity=snapshot,
        knowledge_pack_json=knowledge_pack_json,
    )
    pointer_path = point_cloud_api._processed_pointer_path("34", "same-bridge")
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    second_root = point_cloud_api._processed_paths(
        "34", "same-bridge", second["source_generation"]
    )[0]
    manifest_bytes = (second_root / "analysis.json").read_bytes()
    manifest = json.loads(manifest_bytes)

    assert pointer["format"] == point_cloud_api._PROCESSED_POINTER_FORMAT
    assert pointer["workspace_id"] == "workspace-a"
    assert pointer["source_attachment_id"] == source_attachment_id
    assert pointer["source_sha256"] == source_sha256
    assert pointer["analysis_profile_sha256"] == point_cloud_api._canonical_sha256(profile_b)
    assert pointer["snapshot_identity_sha256"] == point_cloud_api._canonical_sha256(snapshot)
    assert pointer["generation_key_sha256"] == second["generation_key_sha256"]
    assert pointer["manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
    assert manifest["persistence"]["analysis_profile"] == profile_b
    assert manifest["persistence"]["snapshot_identity"] == snapshot
    assert manifest["persistence"]["artifact_sha256"]["npz"] == hashlib.sha256(
        (second_root / "processed_cloud.npz").read_bytes()
    ).hexdigest()
    assert manifest["persistence"]["artifact_sha256"]["ply"] == hashlib.sha256(
        (second_root / "processed_cloud.ply").read_bytes()
    ).hexdigest()
    assert manifest["persistence"]["artifact_sha256"]["knowledge_pack"] == knowledge_pack_sha256
    assert manifest["persistence"]["knowledge_pack"] == {
        "sha256": knowledge_pack_sha256,
        "byte_size": len(knowledge_pack_json.encode("utf-8")),
        "schema_version": "bridgeguard-point-cloud-knowledge-pack-v1",
    }
    assert (second_root / "knowledge_pack.json").read_text(
        encoding="utf-8"
    ) == knowledge_pack_json
    assert first["source_generation"] != second["source_generation"]
    assert first["generation_key_sha256"] != second["generation_key_sha256"]
    assert (first_root / "analysis.json").read_bytes() == first_manifest
    assert (first_root / "processed_cloud.npz").read_bytes() == first_npz
    assert point_cloud_api._load_processed("34", "same-bridge")["marker"] == "second"
    assert not list(data_root.rglob("*.staging"))
    assert not list(data_root.rglob("*.tmp"))


def test_snapshot_identity_is_resolved_from_the_server_owned_workspace(
    point_cloud_store,
):
    _data_root, database = point_cloud_store
    with _connection(database) as connection:
        connection.executescript(
            """
            CREATE TABLE workspace_state (
              workspace_id TEXT PRIMARY KEY,
              data_generation INTEGER NOT NULL,
              schema_generation INTEGER NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE snapshot_revisions (
              id TEXT NOT NULL,
              workspace_id TEXT NOT NULL,
              state_code INTEGER NOT NULL,
              bridge_number TEXT NOT NULL,
              data_year INTEGER NOT NULL,
              revision_number INTEGER NOT NULL,
              canonical_json TEXT NOT NULL,
              canonical_sha256 TEXT NOT NULL,
              profile_id TEXT NOT NULL,
              rule_bundle_sha256 TEXT NOT NULL
            );
            CREATE TABLE published_snapshots (
              workspace_id TEXT NOT NULL,
              state_code INTEGER NOT NULL,
              bridge_number TEXT NOT NULL,
              data_year INTEGER NOT NULL,
              snapshot_revision_id TEXT NOT NULL,
              published_at TEXT NOT NULL
            );
            """
        )
        for workspace, revision, year in (
            ("workspace-a", "revision-a", 2026),
            ("workspace-b", "revision-b", 2027),
        ):
            document = {"BL01": 34, "BID01": "same-bridge", "BID02": workspace}
            digest = canonical_sha256(document)
            connection.execute(
                "INSERT INTO workspace_state VALUES (?,?,?,?)",
                (workspace, 4, 6, "2026-08-13T00:00:00Z"),
            )
            connection.execute(
                "INSERT INTO snapshot_revisions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    revision,
                    workspace,
                    34,
                    "same-bridge",
                    year,
                    4,
                    canonical_json_text(document),
                    digest,
                    "profile",
                    "c" * 64,
                ),
            )
            connection.execute(
                "INSERT INTO published_snapshots VALUES (?,?,?,?,?,?)",
                (
                    workspace,
                    34,
                    "same-bridge",
                    year,
                    revision,
                    "2026-08-13T00:00:00Z",
                ),
            )

    identity = point_cloud_api._latest_published_snapshot_identity(
        "34", "same-bridge"
    )
    assert identity["workspace_id"] == "workspace-a"
    assert identity["snapshot_revision_id"] == "revision-a"
    assert identity["data_year"] == 2026
    assert identity["canonical_sha256"] == canonical_sha256(
        {"BL01": 34, "BID01": "same-bridge", "BID02": "workspace-a"}
    )


def test_processed_storage_isolated_for_same_bridge_in_two_workspaces(
    point_cloud_store, monkeypatch: pytest.MonkeyPatch
):
    _data_root, _database = point_cloud_store
    saved = {}
    for workspace, marker in (("workspace-a", "alpha"), ("workspace-b", "bravo")):
        monkeypatch.setattr(point_cloud_api, "POINT_CLOUD_WORKSPACE_ID", workspace)
        saved[workspace] = point_cloud_api._save_processed(
            "34",
            "same-bridge",
            _points(),
            _analysis_result(marker=marker),
            analysis_profile={"model_mode": marker},
        )

    monkeypatch.setattr(point_cloud_api, "POINT_CLOUD_WORKSPACE_ID", "workspace-a")
    path_a = point_cloud_api._processed_pointer_path("34", "same-bridge")
    assert point_cloud_api._load_processed("34", "same-bridge")["marker"] == "alpha"
    monkeypatch.setattr(point_cloud_api, "POINT_CLOUD_WORKSPACE_ID", "workspace-b")
    path_b = point_cloud_api._processed_pointer_path("34", "same-bridge")
    assert point_cloud_api._load_processed("34", "same-bridge")["marker"] == "bravo"
    assert path_a != path_b
    assert saved["workspace-a"]["workspace_id"] == "workspace-a"
    assert saved["workspace-b"]["workspace_id"] == "workspace-b"


def test_processed_artifact_hash_mismatch_fails_closed(
    point_cloud_store, monkeypatch: pytest.MonkeyPatch
):
    _data_root, _database = point_cloud_store
    persisted = point_cloud_api._save_processed(
        "34",
        "same-bridge",
        _points(),
        _analysis_result(),
        analysis_profile={"model_mode": "clearance"},
    )
    _root, npz_path, _ply, _analysis = point_cloud_api._processed_paths(
        "34", "same-bridge", persisted["source_generation"]
    )
    with npz_path.open("ab") as handle:
        handle.write(b"tampered")

    assert point_cloud_api._processed_preview("34", "same-bridge") is None
    monkeypatch.setenv("BRIDGEGUARD_POINT_CLOUD_LEGACY_BRIDGE_ROUTES", "true")
    with pytest.raises(HTTPException) as caught:
        point_cloud_api.download_processed("34", "same-bridge", format="npz")
    assert caught.value.status_code == 404


def test_processed_knowledge_pack_tamper_fails_closed(point_cloud_store):
    _data_root, _database = point_cloud_store
    pack_json = json.dumps(
        {"schema_version": "bridgeguard-point-cloud-knowledge-pack-v1"},
        sort_keys=True,
        separators=(",", ":"),
    )
    pack_sha256 = hashlib.sha256(pack_json.encode("utf-8")).hexdigest()
    persisted = point_cloud_api._save_processed(
        "34",
        "same-bridge",
        _points(),
        _analysis_result(),
        analysis_profile={
            "model_mode": "clearance",
            "knowledge_pack_sha256": pack_sha256,
        },
        knowledge_pack_json=pack_json,
    )
    root = point_cloud_api._processed_paths(
        "34", "same-bridge", persisted["source_generation"]
    )[0]
    with (root / "knowledge_pack.json").open("ab") as handle:
        handle.write(b"tampered")

    assert point_cloud_api._load_processed("34", "same-bridge") is None


def test_source_change_during_analysis_never_publishes_stale_generation(
    point_cloud_store, monkeypatch: pytest.MonkeyPatch
):
    data_root, database = point_cloud_store
    _insert_cloud(
        data_root, database, workspace_id="workspace-a", payload=b"original"
    )

    async def fake_payload(_request):
        return None, None, {"use_vlm": False, "use_ai_cleaning": False}

    async def fake_points(*_args, **_kwargs):
        return _points()

    def analyze_then_replace(*_args, **_kwargs):
        with _connection(database) as connection:
            connection.execute(
                "DELETE FROM bridge_attachments WHERE workspace_id='workspace-a'"
            )
        _insert_cloud(
            data_root, database, workspace_id="workspace-a", payload=b"replacement"
        )
        return _analysis_result(marker="stale")

    monkeypatch.setattr(point_cloud_api, "_cloud_payload", fake_payload)
    monkeypatch.setattr(point_cloud_api, "_analysis_points", fake_points)
    identity = SimpleNamespace(
        snapshot_revision_id="revision-a",
        canonical_sha256="a" * 64,
        data_year=2026,
        public_payload=lambda: {
            "workspace_id": "workspace-a",
            "state_code": 34,
            "bridge_number": "same-bridge",
            "data_year": 2026,
            "snapshot_revision_id": "revision-a",
            "revision_number": 1,
            "canonical_sha256": "a" * 64,
            "profile_id": "profile",
            "rule_bundle_sha256": "b" * 64,
            "published_at": "2026-08-14T00:00:00Z",
            "data_generation": 1,
            "schema_generation": 1,
            "snapshot_status": "published",
        },
    )
    canonical_context = SimpleNamespace(identity=identity, canonical_document={})
    pack = SimpleNamespace(
        sha256="c" * 64,
        byte_size=10,
        payload={
            "schema_version": "test-pack",
            "untrusted_report_evidence": {},
        },
        provider_context=lambda: {
            "schema_version": "test-pack",
            "knowledge_pack_sha256": "c" * 64,
            "trusted_structured_context": {},
            "untrusted_report_evidence": {},
        },
    )
    monkeypatch.setattr(
        point_cloud_api,
        "_build_exact_bridge_knowledge_pack",
        lambda **_kwargs: (canonical_context, pack),
    )
    monkeypatch.setattr(point_cloud_api, "analyze_bridge", analyze_then_replace)

    result = asyncio.run(
        point_cloud_api._run_analysis(object(), "34", "same-bridge")
    )

    assert result["persistence"] == {
        "saved": False,
        "error_code": "point_cloud_source_changed",
    }
    assert not point_cloud_api._processed_pointer_path(
        "34", "same-bridge"
    ).exists()
    assert "replacement" not in " ".join(result["warnings"])


def test_legacy_row_without_sha_fails_closed_before_analysis(
    point_cloud_store, monkeypatch: pytest.MonkeyPatch
):
    data_root, database = point_cloud_store
    _insert_cloud(
        data_root,
        database,
        workspace_id="workspace-a",
        payload=b"legacy",
        sha256=None,
    )

    async def fake_payload(_request):
        return None, None, {}

    called = False

    def should_not_analyze(*_args, **_kwargs):
        nonlocal called
        called = True
        return _analysis_result()

    monkeypatch.setattr(point_cloud_api, "_cloud_payload", fake_payload)
    monkeypatch.setattr(point_cloud_api, "analyze_bridge", should_not_analyze)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            point_cloud_api._run_analysis(object(), "34", "same-bridge")
        )

    assert caught.value.status_code == 409
    assert "re-upload" in caught.value.detail.lower()
    assert called is False
    assert not point_cloud_api._processed_pointer_path(
        "34", "same-bridge"
    ).exists()


def test_outside_legacy_path_is_never_read_or_deleted(
    point_cloud_store, test_workspace: Path
):
    data_root, database = point_cloud_store
    outside = test_workspace / "outside.e57"
    outside.write_bytes(b"do-not-touch")
    _insert_cloud(
        data_root,
        database,
        workspace_id="workspace-a",
        payload=b"placeholder",
        filepath=str(outside),
    )

    with pytest.raises(HTTPException) as caught:
        point_cloud_api.download_point_cloud("34", "same-bridge")
    assert caught.value.status_code == 409

    asyncio.run(
        point_cloud_api.delete_point_cloud(
            "34", "same-bridge", _request(), "invalid legacy storage path"
        )
    )
    assert outside.read_bytes() == b"do-not-touch"
    with _connection(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM bridge_attachments WHERE workspace_id='workspace-a'"
        ).fetchone()[0] == 0


def test_delete_requires_authenticated_actor_and_nonblank_audited_reason(
    point_cloud_store,
):
    data_root, database = point_cloud_store
    _attachment_id, source = _insert_cloud(
        data_root, database, workspace_id="workspace-a", payload=b"delete-me"
    )
    anonymous = _request()
    del anonymous.state.user
    with pytest.raises(HTTPException) as unauthenticated:
        asyncio.run(
            point_cloud_api.delete_point_cloud(
                "34", "same-bridge", anonymous, "customer request"
            )
        )
    assert unauthenticated.value.status_code == 401

    with pytest.raises(HTTPException) as invalid_reason:
        asyncio.run(
            point_cloud_api.delete_point_cloud(
                "34", "same-bridge", _request(), "   "
            )
        )
    assert invalid_reason.value.status_code == 400
    assert invalid_reason.value.detail == "invalid_deletion_reason"
    assert source.is_file()

    response = asyncio.run(
        point_cloud_api.delete_point_cloud(
            "34", "same-bridge", _request(), "bad coordinate reference"
        )
    )
    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["status"] == "deleted"
    assert body["deletion"]["attachment_type"] == "point_cloud"
    assert "path" not in json.dumps(body).lower()
    assert not source.exists()
    with _connection(database) as connection:
        requested = connection.execute(
            "SELECT actor_id,reason FROM retention_audit_events "
            "WHERE action='attachment_deletion_requested'"
        ).fetchone()
    assert requested == ("editor@example.com", "bad coordinate reference")


def test_delete_cannot_destroy_source_referenced_by_durable_run_evidence(
    point_cloud_store,
):
    data_root, database = point_cloud_store
    _attachment_id, source = _insert_cloud(
        data_root, database, workspace_id="workspace-a", payload=b"protected-source"
    )
    source_sha256 = hashlib.sha256(b"protected-source").hexdigest()
    with _connection(database) as connection:
        connection.execute(
            "INSERT INTO point_cloud_runs (workspace_id,source_sha256) VALUES (?,?)",
            ("workspace-a", source_sha256),
        )

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            point_cloud_api.delete_point_cloud(
                "34", "same-bridge", _request(), "customer requested deletion"
            )
        )
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "point_cloud_source_has_durable_evidence"
    assert source.is_file()
    with _connection(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM bridge_attachments").fetchone()[0] == 1


def test_point_cloud_quota_rejects_concurrent_analysis_with_retry_after_and_releases(
    point_cloud_store, monkeypatch: pytest.MonkeyPatch
):
    registry = OperationQuotaRegistry(
        operation_limits={"point_cloud": 1},
        workspace_limit=1,
        owner_limit=1,
    )
    monkeypatch.setattr(point_cloud_api, "DEFAULT_OPERATION_QUOTAS", registry)

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def held_analysis(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return {"status": "success"}

        monkeypatch.setattr(point_cloud_api, "_run_analysis", held_analysis)
        first = asyncio.create_task(
            point_cloud_api.analyze_ad_hoc_cloud(_request(method="POST"))
        )
        await entered.wait()
        try:
            with pytest.raises(HTTPException) as limited:
                await point_cloud_api.analyze_ad_hoc_cloud(
                    _request(method="POST")
                )
            assert limited.value.status_code == 429
            assert limited.value.headers["Retry-After"] == "1"
            assert "capacity" in limited.value.detail.lower()
        finally:
            release.set()
        assert await first == {"status": "success"}

    asyncio.run(scenario())
    snapshot = registry.snapshot(
        workspace_id="workspace-a",
        owner_id="editor@example.com",
        operation="point_cloud",
    )
    assert snapshot.active_total == 0
    assert snapshot.active_for_operation == 0


@pytest.mark.parametrize("surface", ["upload", "segment", "clearance"])
def test_point_cloud_heavy_surfaces_share_the_bounded_quota_and_retry_after(
    point_cloud_store,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
):
    registry = OperationQuotaRegistry(
        operation_limits={"point_cloud": 1},
        workspace_limit=1,
        owner_limit=1,
    )
    monkeypatch.setattr(point_cloud_api, "DEFAULT_OPERATION_QUOTAS", registry)
    blocker = registry.acquire(
        request_id="test:blocker",
        workspace_id="workspace-a",
        owner_id="other@example.com",
        operation="point_cloud",
    )

    async def invoke():
        request = _request(method="POST")
        if surface == "upload":
            return await point_cloud_api.upload_point_cloud(
                "34",
                "bridge-a",
                request,
                UploadFile(filename="scan.e57", file=io.BytesIO(b"ASTM-E57cloud")),
            )
        if surface == "segment":
            return await point_cloud_api.segment_ad_hoc_cloud(request)
        return await point_cloud_api.ad_hoc_clearance(request)

    try:
        with pytest.raises(HTTPException) as limited:
            asyncio.run(invoke())
        assert limited.value.status_code == 429
        assert limited.value.headers["Retry-After"] == "1"
    finally:
        registry.release(blocker)
    assert registry.snapshot(
        workspace_id="workspace-a",
        owner_id="editor@example.com",
        operation="point_cloud",
    ).active_total == 0


@pytest.mark.parametrize("exit_mode", ["error", "cancel"])
def test_point_cloud_quota_is_released_on_error_and_cancellation(
    point_cloud_store,
    monkeypatch: pytest.MonkeyPatch,
    exit_mode: str,
):
    registry = OperationQuotaRegistry(
        operation_limits={"point_cloud": 1},
        workspace_limit=1,
        owner_limit=1,
    )
    monkeypatch.setattr(point_cloud_api, "DEFAULT_OPERATION_QUOTAS", registry)

    async def scenario():
        entered = asyncio.Event()
        never = asyncio.Event()

        async def terminal_analysis(*_args, **_kwargs):
            entered.set()
            if exit_mode == "error":
                raise RuntimeError("private provider failure")
            await never.wait()

        monkeypatch.setattr(point_cloud_api, "_run_analysis", terminal_analysis)
        task = asyncio.create_task(
            point_cloud_api.analyze_ad_hoc_cloud(_request(method="POST"))
        )
        await entered.wait()
        if exit_mode == "error":
            with pytest.raises(RuntimeError, match="private provider failure"):
                await task
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
    assert registry.snapshot(
        workspace_id="workspace-a",
        owner_id="editor@example.com",
        operation="point_cloud",
    ).active_total == 0
