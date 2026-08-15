"""Red-first contract for exact-byte multipart revision staging.

The browser loader must not reconstruct an uploaded JSON document and call the
ordinary JSON API: doing that loses the exact source bytes.  This suite freezes
the boundary expected from ``POST /api/revisions/stage-upload``.  The endpoint
stores a bounded immutable source artifact, parses JSON strictly, derives all
identity and validation evidence on the server, and stages (but never publishes)
the resulting canonical revisions.

Every test uses the isolated data tree installed by ``conftest.py``.  No test
may read or mutate the deployed BridgeGuard database or upload store.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main, revision_api
from app.auth import AuthMiddleware, AuthUser, COOKIE_NAME, issue_session
from app.routers.services.database import ensure_schema
from app.services.conversion.revision_store import (
    ensure_workspace,
    initialize_revision_store,
)
from app.services.conversion.source_artifacts import SourceArtifactStore
from app.services.conversion.export_service import (
    RevisionExportRequest,
    export_snapshot_revision,
)


SHARED_WORKSPACE = "shared-research-workspace"
STATE = 34
BRIDGE = "UPLOAD-REV-0001"
YEAR = 2026
MAX_TEST_UPLOAD_BYTES = 4_096
RAW_EVIDENCE_MARKER = "DO-NOT-LOG-RAW-UPLOAD-7f0c24c2"

AUTH_ENV = {
    "BRIDGEGUARD_AUTH_DISABLED": "0",
    "BRIDGEGUARD_AUTH_MODE": "password",
    "BRIDGEGUARD_SESSION_SECRET": "revision-upload-contract-secret-longer-than-32-characters",
    "BRIDGEGUARD_PASSWORD": "revision-upload-contract-password",
    "BRIDGEGUARD_ALLOWED_EMAILS": "viewer@example.com",
    "BRIDGEGUARD_EDITOR_EMAILS": "editor@example.com",
    "BRIDGEGUARD_ADMIN_EMAILS": "admin@example.com",
    "BRIDGEGUARD_ALLOWED_DOMAINS": "",
    "BRIDGEGUARD_OWNER_EMAIL": "",
}

ROLE_EMAIL = {
    "viewer": "viewer@example.com",
    "editor": "editor@example.com",
    "admin": "admin@example.com",
}


@dataclass
class UploadHarness:
    client: TestClient
    database_path: str
    data_root: Path

    def headers(
        self,
        role: str,
        *,
        idempotency_key: str | None = "upload-contract",
        request_id: str | None = "request-upload-contract",
    ) -> dict[str, str]:
        token = issue_session(AuthUser(email=ROLE_EMAIL[role], role=role))
        headers = {"Cookie": f"{COOKIE_NAME}={token}"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        if request_id is not None:
            headers["X-Request-ID"] = request_id
        return headers


def _focused_app() -> FastAPI:
    """Mount the real route without running the production lifespan."""

    focused = FastAPI()
    focused.include_router(revision_api.router)

    # The upload adapter may live on the main application while it is being
    # migrated into the revision router.  Mount it once either way so the test
    # describes the public URL rather than constraining internal module layout.
    mounted = {
        str(getattr(route, "path", "")) for route in focused.router.routes
    }
    if "/api/revisions/stage-upload" not in mounted:
        for route in main.app.router.routes:
            if str(getattr(route, "path", "")) == "/api/revisions/stage-upload":
                focused.router.routes.append(route)
                break

    focused.add_middleware(AuthMiddleware)
    return focused


@pytest.fixture
def upload_harness(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> UploadHarness:
    for key, value in AUTH_ENV.items():
        monkeypatch.setenv(key, value)

    database_path = str(test_workspace / "revision-upload.db")
    data_root = test_workspace / "upload-data"
    data_root.mkdir(parents=True)
    ensure_schema(database_path)
    initialize_revision_store(database_path, app_revision="pr5-upload-contract")
    ensure_workspace(database_path, SHARED_WORKSPACE, "BridgeGuard research")

    monkeypatch.setattr(main, "USER_DB", database_path)
    monkeypatch.setattr(main, "DATA_DIR", data_root)
    monkeypatch.setattr(
        main, "MAX_CONVERSION_BYTES", MAX_TEST_UPLOAD_BYTES, raising=False
    )
    # Keep the fixture agnostic to whether the endpoint is implemented in
    # ``main`` or moved into the revision router.
    monkeypatch.setattr(revision_api, "DATA_DIR", data_root, raising=False)
    monkeypatch.setattr(
        revision_api,
        "MAX_CONVERSION_BYTES",
        MAX_TEST_UPLOAD_BYTES,
        raising=False,
    )

    with TestClient(_focused_app()) as client:
        yield UploadHarness(client, database_path, data_root)


def _document(
    *,
    bridge_number: str = BRIDGE,
    name: str = "Exact byte upload contract",
) -> dict[str, Any]:
    return {
        "BL01": STATE,
        "BID01": bridge_number,
        "BID02": name,
        "BL03": "00123",
        "BW01": 1977,
        "Elements": [],
        "Features": [
            {
                "BF01": "H01",
                "BF02": "1",
                "Routes": [
                    {"BRT01": "C01", "BRT02": "0001", "BRT03": "N"}
                ],
            }
        ],
        "Inspections": [],
        "PostingEvaluations": [],
        "PostingStatuses": [],
        "SpanSets": [],
        "SubstructureSets": [],
        "Works": [],
    }


def _exact_upload_bytes(*, marker: bool = False) -> bytes:
    document = _document()
    if marker:
        # Converter metadata beginning with '_' is source evidence and must not
        # become part of the canonical SNBI document or an API/log disclosure.
        document["_PrivateEvidence"] = RAW_EVIDENCE_MARKER
    compact = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    return ("\r\n[\r\n  " + compact + "\r\n]\r\n").encode("utf-8")


def _post_upload(
    harness: UploadHarness,
    raw: bytes,
    *,
    role: str = "editor",
    key: str | None = "upload-contract",
    request_id: str | None = "request-upload-contract",
    data_year: int | None = YEAR,
    expected_generation: int | None = 0,
    filename: str = "bridge-upload.json",
):
    form: dict[str, str] = {}
    if data_year is not None:
        form["data_year"] = str(data_year)
    if expected_generation is not None:
        form["expected_data_generation"] = str(expected_generation)
    return harness.client.post(
        "/api/revisions/stage-upload",
        headers=harness.headers(
            role, idempotency_key=key, request_id=request_id
        ),
        data=form,
        files={"file": (filename, raw, "application/json")},
    )


def _error_code(response) -> str | None:
    payload = response.json()
    detail = payload.get("detail", payload)
    return detail.get("code") if isinstance(detail, dict) else None


def _table_count(database_path: str, table: str) -> int:
    connection = sqlite3.connect(database_path)
    try:
        return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    finally:
        connection.close()


def _assert_no_staged_or_published_rows(database_path: str) -> None:
    for table in (
        "source_artifacts",
        "source_records",
        "conversion_batches",
        "snapshot_revisions",
        "published_snapshots",
        "validation_runs",
    ):
        assert _table_count(database_path, table) == 0, table


def test_upload_requires_editor_headers_year_and_generation(
    upload_harness: UploadHarness,
):
    raw = _exact_upload_bytes()

    viewer = _post_upload(upload_harness, raw, role="viewer", key="viewer")
    assert viewer.status_code == 403

    missing_key = _post_upload(upload_harness, raw, key=None)
    assert missing_key.status_code == 400
    assert _error_code(missing_key) == "idempotency_key_required"

    missing_request_id = _post_upload(
        upload_harness, raw, key="missing-request", request_id=None
    )
    assert missing_request_id.status_code == 400
    assert _error_code(missing_request_id) == "request_id_required"

    missing_year = _post_upload(
        upload_harness, raw, key="missing-year", data_year=None
    )
    assert missing_year.status_code == 422
    assert _error_code(missing_year) == "data_year_required"

    missing_generation = _post_upload(
        upload_harness,
        raw,
        key="missing-generation",
        expected_generation=None,
    )
    assert missing_generation.status_code == 422
    assert _error_code(missing_generation) == "expected_data_generation_required"

    stale_generation = _post_upload(
        upload_harness,
        raw,
        key="stale-generation",
        expected_generation=99,
    )
    assert stale_generation.status_code == 409
    assert _error_code(stale_generation) == "stale_data_generation"

    _assert_no_staged_or_published_rows(upload_harness.database_path)


def test_upload_preserves_exact_bytes_and_stages_loader_compatible_revision(
    upload_harness: UploadHarness,
):
    raw = _exact_upload_bytes(marker=True)
    response = _post_upload(
        upload_harness,
        raw,
        key="exact-byte-stage",
        request_id="request-exact-byte-stage",
        filename="NJ bridge evidence.json",
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["status"] == "staged"
    assert payload["batch_id"]
    assert payload["data_generation"] == 0
    assert response.headers["x-bridgeguard-data-version"] == "0"

    receipt = payload["source"]
    assert receipt["artifact_id"]
    assert receipt["sha256"] == hashlib.sha256(raw).hexdigest()
    assert receipt["size_bytes"] == len(raw)
    assert receipt["original_filename"] == "NJ bridge evidence.json"
    assert "path" not in json.dumps(receipt).lower()

    revisions = payload["revisions"]
    assert len(revisions) == 1
    revision = revisions[0]
    assert revision["state_code"] == STATE
    assert revision["bridge_number"] == BRIDGE
    assert revision["data_year"] == YEAR
    assert revision["revision_number"] == 1
    assert revision["revision_id"]
    assert revision["content_hash"]
    assert revision["outcome"] == "created"
    assert isinstance(revision["diff"], list)
    assert isinstance(revision["validation_issues"], list)
    assert revision["validation_status"]
    assert revision["validation_summary"]["validation_owner"] == "server"

    store = SourceArtifactStore(
        upload_harness.data_root / "conversion_sources",
        max_bytes=MAX_TEST_UPLOAD_BYTES,
    )
    assert store.read_bytes(receipt["artifact_id"]) == raw

    connection = sqlite3.connect(upload_harness.database_path)
    connection.row_factory = sqlite3.Row
    try:
        artifact = connection.execute(
            "SELECT * FROM source_artifacts WHERE workspace_id=?",
            (SHARED_WORKSPACE,),
        ).fetchone()
        assert artifact is not None
        assert artifact["id"] == receipt["artifact_id"]
        assert artifact["sha256"] == hashlib.sha256(raw).hexdigest()
        assert artifact["byte_size"] == len(raw)
        assert artifact["created_by"] == ROLE_EMAIL["editor"]
        assert artifact["storage_uri"] == f"source-artifact://{receipt['artifact_id']}"
        assert not Path(str(artifact["storage_uri"])).is_absolute()

        source_link = connection.execute(
            "SELECT sr.source_artifact_id FROM snapshot_revision_sources srs "
            "JOIN source_records sr ON sr.workspace_id=srs.workspace_id "
            "AND sr.id=srs.source_record_id WHERE srs.snapshot_revision_id=?",
            (revision["revision_id"],),
        ).fetchone()
        assert source_link is not None
        assert source_link["source_artifact_id"] == receipt["artifact_id"]

        validation = connection.execute(
            "SELECT status, summary_json FROM validation_runs "
            "WHERE workspace_id=? AND snapshot_revision_id=?",
            (SHARED_WORKSPACE, revision["revision_id"]),
        ).fetchone()
        assert validation is not None
        assert validation["status"] == revision["validation_status"]
        assert json.loads(validation["summary_json"])["validation_owner"] == "server"
    finally:
        connection.close()

    # Staging is generation-neutral and cannot make data visible to readers.
    assert _table_count(upload_harness.database_path, "snapshot_revisions") == 1
    assert _table_count(upload_harness.database_path, "published_snapshots") == 0
    assert _table_count(upload_harness.database_path, "bridges") == 0


def test_upload_retry_is_idempotent_and_changed_payload_conflicts(
    upload_harness: UploadHarness,
):
    raw = _exact_upload_bytes()
    first = _post_upload(
        upload_harness,
        raw,
        key="multipart-retry",
        request_id="multipart-attempt-one",
    )
    assert first.status_code == 201, first.text

    replay = _post_upload(
        upload_harness,
        raw,
        key="multipart-retry",
        request_id="multipart-attempt-two",
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert _table_count(upload_harness.database_path, "source_artifacts") == 1
    assert _table_count(upload_harness.database_path, "source_records") == 1
    assert _table_count(upload_harness.database_path, "conversion_batches") == 1
    assert _table_count(upload_harness.database_path, "snapshot_revisions") == 1
    assert _table_count(upload_harness.database_path, "published_snapshots") == 0

    changed = (
        "["
        + json.dumps(
            _document(name="Changed bytes under a reused key"),
            separators=(",", ":"),
        )
        + "]"
    ).encode("utf-8")
    conflict = _post_upload(
        upload_harness,
        changed,
        key="multipart-retry",
        request_id="multipart-attempt-three",
    )
    assert conflict.status_code == 409
    assert _error_code(conflict) == "idempotency_payload_conflict"
    assert _table_count(upload_harness.database_path, "snapshot_revisions") == 1
    assert _table_count(upload_harness.database_path, "published_snapshots") == 0


def test_converter_upload_links_and_restores_the_original_nbi_source(
    upload_harness: UploadHarness,
):
    store = SourceArtifactStore(
        upload_harness.data_root / "conversion_sources",
        max_bytes=MAX_TEST_UPLOAD_BYTES,
    )
    original_bytes = b"342ORIGINAL-NBI  1" + (b" " * 426) + b"\r\n"
    original = store.ingest(
        original_bytes,
        original_filename="NJDOT_2025_original.txt",
    )
    document = _document()
    document["_SourceData"] = {
        "artifact": original.to_public_dict(),
        "over_record": {
            "source_record_id": "nbi-source-record-001",
            "source_order": 0,
            "line_number": 1,
            "byte_offset": 0,
            "raw_sha256": hashlib.sha256(original_bytes.rstrip(b"\r\n")).hexdigest(),
            "record_type": "1",
            "raw_item_001": "342",
            "raw_item_008": BRIDGE,
        },
        "under_records": [],
    }
    raw = json.dumps([document], separators=(",", ":")).encode("utf-8")

    response = _post_upload(
        upload_harness,
        raw,
        key="converter-original-source",
        request_id="converter-original-source",
        filename="converted-snbi.json",
    )
    assert response.status_code == 201, response.text
    revision_id = response.json()["revisions"][0]["revision_id"]

    connection = sqlite3.connect(upload_harness.database_path)
    connection.row_factory = sqlite3.Row
    try:
        links = connection.execute(
            "SELECT sa.id, sa.artifact_kind, sr.source_role "
            "FROM snapshot_revision_sources srs "
            "JOIN source_records sr ON sr.workspace_id=srs.workspace_id "
            "AND sr.id=srs.source_record_id "
            "JOIN source_artifacts sa ON sa.workspace_id=sr.workspace_id "
            "AND sa.id=sr.source_artifact_id "
            "WHERE srs.snapshot_revision_id=? ORDER BY srs.source_ordinal",
            (revision_id,),
        ).fetchall()
    finally:
        connection.close()
    assert [(row["artifact_kind"], row["source_role"]) for row in links] == [
        ("immutable_json_upload", "nbi_json_upload_record"),
        ("legacy_nbi_source", "nbi_over_record"),
    ]
    assert links[1]["id"] == original.artifact_id

    restored = export_snapshot_revision(
        upload_harness.database_path,
        store,
        RevisionExportRequest(
            workspace_id=SHARED_WORKSPACE,
            snapshot_revision_id=revision_id,
            export_kind="restore_original",
            profile_id="exact_source_restore_v1",
            agency_id="NJDOT",
            actor_id=ROLE_EMAIL["editor"],
            exported_at="2026-08-13T18:00:00Z",
        ),
    )
    originals = [
        item
        for item in restored.artifacts
        if item.filename == "NJDOT_2025_original.txt"
    ]
    assert len(originals) == 1
    assert originals[0].content == original_bytes
    assert restored.manifest["exact_reverse"] is True
    assert restored.manifest["source_artifacts"][0]["artifact_id"] == original.artifact_id

@pytest.mark.parametrize(
    ("raw", "expected_status", "expected_code"),
    [
        (b'[{"BL01":34,"BID01":', 400, "invalid_json"),
        (
            b'[{"BL01":34,"BID01":"first","BID01":"second"}]',
            400,
            "duplicate_json_key",
        ),
        (
            b'[{"BL01":34,"BID01":"nonfinite","BW01":NaN}]',
            400,
            "invalid_json_constant",
        ),
        (
            b'[{"BL01":34,"BID01":"nonfinite","BW01":Infinity}]',
            400,
            "invalid_json_constant",
        ),
        (
            b'[{"BL01":34,"BID01":"too-large","_Padding":"'
            + (b"x" * MAX_TEST_UPLOAD_BYTES)
            + b'"}]',
            413,
            "upload_too_large",
        ),
    ],
    ids=("malformed", "duplicate-key", "nan", "infinity", "oversize"),
)
def test_upload_rejects_ambiguous_or_unbounded_json_atomically(
    upload_harness: UploadHarness,
    raw: bytes,
    expected_status: int,
    expected_code: str,
):
    response = _post_upload(
        upload_harness,
        raw,
        key=f"reject-{expected_code}-{hashlib.sha256(raw).hexdigest()[:8]}",
    )
    assert response.status_code == expected_status, response.text
    assert _error_code(response) == expected_code
    _assert_no_staged_or_published_rows(upload_harness.database_path)


def test_upload_never_returns_or_logs_raw_payloads_or_local_paths(
    upload_harness: UploadHarness,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
):
    caplog.set_level(logging.DEBUG)
    raw = _exact_upload_bytes(marker=True)
    response = _post_upload(
        upload_harness,
        raw,
        key="privacy-safe-upload",
        request_id="privacy-safe-upload-request",
        filename="private-evidence.json",
    )
    assert response.status_code == 201, response.text

    captured = capsys.readouterr()
    emitted = "\n".join(
        (
            response.text,
            caplog.text,
            captured.out,
            captured.err,
        )
    )
    assert RAW_EVIDENCE_MARKER not in emitted
    assert str(upload_harness.data_root) not in emitted
    assert str(Path(upload_harness.database_path).parent) not in emitted

    serialized = json.dumps(response.json(), sort_keys=True).lower()
    for forbidden_key in (
        "source_path",
        "local_path",
        "storage_path",
        "raw_record_json",
        "raw_payload",
    ):
        assert forbidden_key not in serialized
