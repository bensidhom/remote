from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from app.services.conversion.source_artifacts import (
    ArtifactErrorCode,
    SourceArtifactError,
    SourceArtifactStore,
)
from app.services.operations.jobs import (
    claim_next_job,
    complete_job,
    enqueue_job,
    purge_terminal_jobs,
)
from app.services.operations.retention import (
    RetentionError,
    initialize_retention_store,
    run_source_artifact_retention_sweep,
    set_retention_policy,
)


NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
WORKSPACE = "workspace-a"


def _database(path: Path) -> Path:
    database = path / "bridge.db"
    with closing(sqlite3.connect(database)) as connection:
        with connection:
            connection.executescript(
                """
            CREATE TABLE source_artifacts (
              id TEXT PRIMARY KEY,
              workspace_id TEXT NOT NULL
            );
            CREATE TABLE source_records (
              id TEXT PRIMARY KEY,
              workspace_id TEXT NOT NULL,
              source_artifact_id TEXT NOT NULL
            );
                """
            )
    initialize_retention_store(database)
    return database


def _age_manifest(root: Path, artifact_id: str, created_at: datetime) -> None:
    manifest = root / "manifests" / f"{artifact_id}.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["created_at"] = created_at.isoformat()
    if os.name != "nt":
        manifest.chmod(0o600)
    manifest.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _sweep(
    database: Path,
    jobs: Path,
    source_root: Path,
    preflight_root: Path,
    **kwargs,
) -> dict[str, object]:
    return run_source_artifact_retention_sweep(
        database,
        job_database_path=jobs,
        source_store_root=source_root,
        preflight_store_root=preflight_root,
        workspace_id=WORKSPACE,
        now=NOW,
        retention_days=90,
        job_retention_days=30,
        **kwargs,
    )


def test_deduplicated_object_survives_until_last_manifest_is_unreferenced(
    tmp_path: Path,
):
    database = _database(tmp_path)
    jobs = tmp_path / "jobs.db"
    source_root = tmp_path / "sources"
    preflight_root = tmp_path / "preflights"
    store = SourceArtifactStore(source_root)
    first = store.ingest(b"same source bytes", original_filename="first.json")
    second = store.ingest(b"same source bytes", original_filename="second.json")
    for receipt in (first, second):
        _age_manifest(source_root, receipt.artifact_id, NOW - timedelta(days=120))
    with closing(sqlite3.connect(database)) as connection:
        with connection:
            connection.execute(
                "INSERT INTO source_artifacts VALUES (?,?)",
                (first.artifact_id, WORKSPACE),
            )

    report = _sweep(database, jobs, source_root, preflight_root)

    assert store.read_bytes(first.artifact_id) == b"same source bytes"
    with pytest.raises(SourceArtifactError) as missing:
        store.get(second.artifact_id)
    assert missing.value.code == ArtifactErrorCode.ARTIFACT_NOT_FOUND.value
    assert report["source_store"]["deleted_manifest_count"] == 1
    assert report["source_store"]["deleted_object_count"] == 0
    assert len(list((source_root / "objects").rglob(first.sha256))) == 1

    with closing(sqlite3.connect(database)) as connection:
        with connection:
            connection.execute(
                "DELETE FROM source_artifacts WHERE id=?", (first.artifact_id,)
            )
    report = _sweep(database, jobs, source_root, preflight_root)
    assert report["source_store"]["deleted_manifest_count"] == 1
    assert report["source_store"]["deleted_object_count"] == 1
    assert list((source_root / "objects").rglob(first.sha256)) == []


def test_source_record_job_preflight_and_embedded_receipts_are_reachable(
    tmp_path: Path,
):
    database = _database(tmp_path)
    jobs = tmp_path / "jobs.db"
    source_root = tmp_path / "sources"
    preflight_root = tmp_path / "preflights"
    sources = SourceArtifactStore(source_root)
    preflights = SourceArtifactStore(preflight_root)
    primary = sources.ingest(b"upload", original_filename="upload.json")
    embedded = sources.ingest(b"NBI source", original_filename="nbi.txt")
    record_only = sources.ingest(b"record source", original_filename="record.txt")
    preflight_body = {
        "format": "bridgeguard-import-preflight-v1",
        "artifact_receipts": [
            primary.to_public_dict(),
            embedded.to_public_dict(),
        ],
    }
    preflight = preflights.ingest(
        json.dumps(preflight_body).encode("utf-8"),
        original_filename="preflight.json",
    )
    for root, receipts in (
        (source_root, (primary, embedded, record_only)),
        (preflight_root, (preflight,)),
    ):
        for receipt in receipts:
            _age_manifest(root, receipt.artifact_id, NOW - timedelta(days=120))
    with closing(sqlite3.connect(database)) as connection:
        with connection:
            connection.execute(
                "INSERT INTO source_records VALUES (?,?,?)",
                ("record-1", WORKSPACE, record_only.artifact_id),
            )
    job = enqueue_job(
        jobs,
        workspace_id=WORKSPACE,
        owner_id="owner@example.com",
        operation="import_preflight",
        idempotency_key="import-1",
        payload={"source": primary.to_public_dict()},
        now=NOW - timedelta(days=120),
    )
    claimed = claim_next_job(
        jobs,
        worker_id="worker",
        allowed_operations=("import_preflight",),
        now=NOW - timedelta(days=120),
    )
    assert claimed is not None and claimed.id == job.id
    complete_job(
        jobs,
        job=claimed,
        worker_id="worker",
        result={
            "preflight_digest": preflight.sha256,
            "preflight_receipt": preflight.to_public_dict(),
        },
        now=NOW - timedelta(days=120),
    )

    report = _sweep(database, jobs, source_root, preflight_root)
    assert report["protected_source_artifacts"] == 3
    assert report["protected_preflight_artifacts"] == 1
    for receipt in (primary, embedded, record_only):
        assert sources.get(receipt.artifact_id).artifact_id == receipt.artifact_id
    assert preflights.get(preflight.artifact_id).artifact_id == preflight.artifact_id

    with closing(sqlite3.connect(database)) as connection:
        with connection:
            connection.execute("DELETE FROM source_records")
    assert purge_terminal_jobs(
        jobs,
        workspace_id=WORKSPACE,
        older_than=NOW - timedelta(days=30),
    ) == 1
    report = _sweep(database, jobs, source_root, preflight_root)
    assert report["source_store"]["deleted_manifest_count"] == 3
    assert report["preflight_store"]["deleted_manifest_count"] == 1


def test_source_and_preflight_legal_holds_are_audited_and_fail_safe(tmp_path: Path):
    database = _database(tmp_path)
    jobs = tmp_path / "jobs.db"
    source_root = tmp_path / "sources"
    preflight_root = tmp_path / "preflights"
    source_store = SourceArtifactStore(source_root)
    preflight_store = SourceArtifactStore(preflight_root)
    source = source_store.ingest(b"held source", original_filename="source.json")
    preflight = preflight_store.ingest(b"{}", original_filename="preflight.json")
    for root, receipt in ((source_root, source), (preflight_root, preflight)):
        _age_manifest(root, receipt.artifact_id, NOW - timedelta(days=120))
        set_retention_policy(
            database,
            workspace_id=WORKSPACE,
            resource_type="source_artifact",
            resource_id=receipt.artifact_id,
            actor_id="records@example.com",
            reason="records hold",
            legal_hold=True,
            now=NOW,
        )

    report = _sweep(database, jobs, source_root, preflight_root)
    assert report["protected_source_artifacts"] == 1
    assert report["protected_preflight_artifacts"] == 1
    assert source_store.get(source.artifact_id)
    assert preflight_store.get(preflight.artifact_id)
    with closing(sqlite3.connect(database)) as connection:
        events = connection.execute(
            "SELECT resource_type,resource_id,action FROM retention_audit_events "
            "ORDER BY occurred_at,id"
        ).fetchall()
    assert ("source_artifact", source.artifact_id, "retention_policy_updated") in events
    assert ("source_artifact", preflight.artifact_id, "retention_policy_updated") in events


def test_orphan_object_gc_is_old_bounded_path_confined_and_receipted(tmp_path: Path):
    root = tmp_path / "sources"
    store = SourceArtifactStore(root)
    digest = hashlib.sha256(b"orphan").hexdigest()
    object_path = root / "objects" / digest[:2] / digest[2:4] / digest
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(b"orphan")
    old_epoch = (NOW - timedelta(days=120)).timestamp()
    os.utime(object_path, (old_epoch, old_epoch))

    report = store.sweep_unreferenced(
        protected_artifact_ids=(),
        older_than=NOW - timedelta(days=90),
        now=NOW,
        max_deletions=1,
    )

    assert report.deleted_manifest_count == 0
    assert report.deleted_object_count == 1
    assert report.deleted_object_sha256s == (digest,)
    assert not object_path.exists()
    receipts = list((root / "gc" / "receipts").glob("*.json"))
    assert len(receipts) == 1
    payload = json.loads(receipts[0].read_text(encoding="utf-8"))
    claimed = payload.pop("receipt_sha256")
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert claimed == expected == report.receipt_sha256
    assert str(root) not in json.dumps(payload)


def test_corrupt_manifest_blocks_all_object_deletion(tmp_path: Path):
    root = tmp_path / "sources"
    store = SourceArtifactStore(root)
    digest = hashlib.sha256(b"orphan").hexdigest()
    object_path = root / "objects" / digest[:2] / digest[2:4] / digest
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(b"orphan")
    old_epoch = (NOW - timedelta(days=120)).timestamp()
    os.utime(object_path, (old_epoch, old_epoch))
    (root / "manifests" / "not-a-uuid.json").write_text("{", encoding="utf-8")

    report = store.sweep_unreferenced(
        protected_artifact_ids=(),
        older_than=NOW - timedelta(days=90),
        now=NOW,
    )

    assert report.corrupt_manifests == 1
    assert report.deleted_object_count == 0
    assert object_path.exists()


def test_store_quota_rejects_atomically_without_losing_accepted_data(tmp_path: Path):
    root = tmp_path / "sources"
    first_store = SourceArtifactStore(root)
    accepted = first_store.ingest(b"accepted", original_filename="accepted.json")
    used = first_store.persistent_usage_bytes()
    constrained = SourceArtifactStore(root, max_store_bytes=used + 1)

    with pytest.raises(SourceArtifactError) as raised:
        constrained.ingest(b"different payload", original_filename="second.json")

    assert raised.value.code == ArtifactErrorCode.STORE_QUOTA_EXCEEDED.value
    assert constrained.read_bytes(accepted.artifact_id) == b"accepted"
    assert len(list((root / "manifests").glob("*.json"))) == 1
    assert len([path for path in (root / "objects").rglob("*") if path.is_file()]) == 1
    assert not list((root / "staging").rglob("*.part"))


def test_retention_cannot_be_shorter_than_job_receipt_retention(tmp_path: Path):
    database = _database(tmp_path)
    with pytest.raises(RetentionError) as raised:
        run_source_artifact_retention_sweep(
            database,
            job_database_path=tmp_path / "jobs.db",
            source_store_root=tmp_path / "sources",
            preflight_store_root=tmp_path / "preflights",
            workspace_id=WORKSPACE,
            retention_days=29,
            job_retention_days=30,
            now=NOW,
        )
    assert raised.value.code == "source_retention_shorter_than_job_retention"
