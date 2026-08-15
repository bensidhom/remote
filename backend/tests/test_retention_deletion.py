from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from app.services.operations.jobs import claim_next_job, complete_job, enqueue_job
from app.services.operations.retention import (
    RetentionError,
    initialize_retention_store,
    process_attachment_deletion_outbox,
    request_attachment_deletion,
    run_retention_sweep,
    set_retention_policy,
    sweep_completed_report_cache_tombstones,
)


NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
WORKSPACE = "workspace-a"


@contextmanager
def _opened(path: Path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "bridge.db"
    with _opened(database) as connection:
        connection.execute(
            """
            CREATE TABLE bridge_attachments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              state_code TEXT NOT NULL,
              bridge_number TEXT NOT NULL,
              attachment_type TEXT NOT NULL,
              filename TEXT NOT NULL,
              filepath TEXT NOT NULL,
              size_bytes INTEGER,
              sha256 TEXT,
              workspace_id TEXT,
              uploaded_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE chat_sessions (
              id TEXT PRIMARY KEY,
              workspace_id TEXT NOT NULL,
              owner_id TEXT NOT NULL,
              title TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              retention_expires_at TEXT NOT NULL
            )
            """
        )
    initialize_retention_store(database)
    return database


def _attachment(
    database: Path,
    root: Path,
    *,
    workspace: str = WORKSPACE,
    kind: str = "inspection_report",
    content: bytes = b"report",
) -> tuple[int, Path, str]:
    digest = hashlib.sha256(content).hexdigest()
    source = root / "attachments" / f"{digest[:8]}.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    with _opened(database) as connection:
        cursor = connection.execute(
            "INSERT INTO bridge_attachments "
            "(state_code,bridge_number,attachment_type,filename,filepath,size_bytes,"
            "sha256,workspace_id,uploaded_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "34", "0001", kind, "inspection.pdf",
                source.relative_to(root).as_posix(), len(content), digest, workspace,
                "2026-01-01T00:00:00Z",
            ),
        )
        attachment_id = int(cursor.lastrowid)
    return attachment_id, source, digest


def _report_cache(rag_root: Path, digest: str) -> tuple[Path, Path, Path]:
    cache = rag_root / "bridge_reports" / "v2"
    identity_dir = cache / "manifests" / "scope" / "identity"
    identity_dir.mkdir(parents=True)
    stage_key = "a" * 64
    stage_dir = cache / "stages" / "parsed" / stage_key
    stage_dir.mkdir(parents=True)
    (stage_dir / "artifact.json").write_text("{}", encoding="utf-8")
    manifest = {
        "identity": {"sources": [{"report_id": "1", "sha256": digest, "byte_size": 6}]},
        "stage_keys": {"parsed": stage_key},
    }
    (identity_dir / "manifest-value.json").write_text(json.dumps(manifest), encoding="utf-8")
    (identity_dir / "current.json").write_text(
        json.dumps({"manifest_path": "manifest-value.json"}), encoding="utf-8"
    )
    extracted = rag_root / "extracted_images" / digest[:16]
    extracted.mkdir(parents=True)
    (extracted / "page.png").write_bytes(b"image")
    return identity_dir, stage_dir, extracted


def test_legal_hold_and_retention_block_catalog_deletion(tmp_path: Path):
    database = _database(tmp_path)
    attachment_id, source, _digest = _attachment(database, tmp_path)
    set_retention_policy(
        database, workspace_id=WORKSPACE, resource_type="attachment",
        resource_id=attachment_id, actor_id="admin@example.com",
        reason="litigation preservation", legal_hold=True, now=NOW,
    )
    with pytest.raises(RetentionError) as held:
        request_attachment_deletion(
            database, workspace_id=WORKSPACE, attachment_id=attachment_id,
            attachment_type="inspection_report", actor_id="editor@example.com",
            reason="duplicate", now=NOW,
        )
    assert held.value.code == "attachment_legal_hold"
    assert source.exists()

    set_retention_policy(
        database, workspace_id=WORKSPACE, resource_type="attachment",
        resource_id=attachment_id, actor_id="admin@example.com",
        reason="hold released", legal_hold=False,
        retain_until=NOW + timedelta(days=1), now=NOW,
    )
    with pytest.raises(RetentionError) as retained:
        request_attachment_deletion(
            database, workspace_id=WORKSPACE, attachment_id=attachment_id,
            attachment_type="inspection_report", actor_id="editor@example.com",
            reason="duplicate", now=NOW,
        )
    assert retained.value.code == "attachment_retention_active"
    receipt = request_attachment_deletion(
        database, workspace_id=WORKSPACE, attachment_id=attachment_id,
        attachment_type="inspection_report", actor_id="editor@example.com",
        reason="duplicate", now=NOW + timedelta(days=2),
    )
    assert receipt.status == "pending"
    with _opened(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM bridge_attachments WHERE id=?", (attachment_id,)
        ).fetchone()[0] == 0


def test_deletion_audit_and_report_cache_are_purged_without_path_leak(tmp_path: Path):
    database = _database(tmp_path)
    attachment_id, source, digest = _attachment(database, tmp_path)
    rag_root = tmp_path / "rag"
    identity_dir, stage_dir, extracted = _report_cache(rag_root, digest)
    requested = request_attachment_deletion(
        database, workspace_id=WORKSPACE, attachment_id=attachment_id,
        attachment_type="inspection_report", actor_id="editor@example.com",
        reason="superseded inspection", state_code="34", bridge_number="0001", now=NOW,
    )
    completed = process_attachment_deletion_outbox(
        database, data_root=tmp_path, rag_working_dir=rag_root,
        workspace_id=WORKSPACE, deletion_id=requested.id, now=NOW,
    )[0]
    assert completed.status == "complete"
    assert not source.exists()
    assert not identity_dir.exists()
    assert not stage_dir.exists()
    assert not extracted.exists()
    assert "path" not in json.dumps(completed.public_dict()).lower()
    # A cache build that began before deletion may finish late.  The durable
    # tombstone is re-applied by maintenance, so that race cannot resurrect it.
    late_identity, late_stage, late_extracted = _report_cache(rag_root, digest)
    assert sweep_completed_report_cache_tombstones(
        database, rag_working_dir=rag_root, workspace_id=WORKSPACE,
        now=NOW + timedelta(seconds=1),
    ) == 1
    assert not late_identity.exists()
    assert not late_stage.exists()
    assert not late_extracted.exists()
    with _opened(database) as connection:
        events = connection.execute(
            "SELECT action,actor_id,reason FROM retention_audit_events ORDER BY occurred_at,id"
        ).fetchall()
        assert ("attachment_deletion_requested", "editor@example.com", "superseded inspection") in events
        assert any(event[0] == "attachment_deletion_completed" for event in events)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM retention_audit_events")


def test_filesystem_failure_keeps_retryable_tombstone(tmp_path: Path):
    database = _database(tmp_path)
    attachment_id, source, _digest = _attachment(database, tmp_path)
    requested = request_attachment_deletion(
        database, workspace_id=WORKSPACE, attachment_id=attachment_id,
        attachment_type="inspection_report", actor_id="editor@example.com",
        reason="invalid upload", now=NOW,
    )

    def fail_unlink(_path: Path) -> None:
        raise PermissionError("private storage path must not escape")

    retried = process_attachment_deletion_outbox(
        database, data_root=tmp_path, rag_working_dir=tmp_path / "rag",
        deletion_id=requested.id, now=NOW, unlinker=fail_unlink,
    )[0]
    assert retried.status == "retry"
    assert retried.last_error_code == "filesystem_finalize_failed"
    assert source.exists()
    assert "private storage path" not in json.dumps(retried.public_dict())

    completed = process_attachment_deletion_outbox(
        database, data_root=tmp_path, rag_working_dir=tmp_path / "rag",
        deletion_id=requested.id, now=NOW + timedelta(seconds=3),
    )[0]
    assert completed.status == "complete"
    assert not source.exists()


def test_point_cloud_uses_same_audited_catalog_deletion(tmp_path: Path):
    database = _database(tmp_path)
    attachment_id, source, _digest = _attachment(
        database, tmp_path, kind="point_cloud", content=b"point cloud"
    )
    requested = request_attachment_deletion(
        database, workspace_id=WORKSPACE, attachment_id=attachment_id,
        attachment_type="point_cloud", actor_id="editor@example.com",
        reason="bad coordinate reference", now=NOW,
    )
    completed = process_attachment_deletion_outbox(
        database, data_root=tmp_path, rag_working_dir=tmp_path / "rag",
        deletion_id=requested.id, now=NOW,
        cache_purger=lambda *_args, **_kwargs: pytest.fail("report cache touched"),
    )[0]
    assert completed.status == "complete"
    assert not source.exists()


def test_workspace_isolation_is_non_enumerating(tmp_path: Path):
    database = _database(tmp_path)
    attachment_id, source, _digest = _attachment(database, tmp_path, workspace="workspace-b")
    with pytest.raises(RetentionError) as missing:
        request_attachment_deletion(
            database, workspace_id=WORKSPACE, attachment_id=attachment_id,
            attachment_type="inspection_report", actor_id="editor@example.com",
            reason="try other tenant", now=NOW,
        )
    assert missing.value.code == "attachment_not_found"
    assert source.exists()


def test_scheduled_sweep_purges_expired_sessions_and_terminal_jobs_but_not_holds(tmp_path: Path):
    database = _database(tmp_path)
    expired = "11111111-1111-4111-8111-111111111111"
    held_session = "22222222-2222-4222-8222-222222222222"
    with _opened(database) as connection:
        for session_id in (expired, held_session):
            connection.execute(
                "INSERT INTO chat_sessions VALUES (?,?,?,?,?,?,?)",
                (
                    session_id, WORKSPACE, "owner@example.com", "Old", "2025-01-01T00:00:00Z",
                    "2025-01-01T00:00:00Z", "2025-02-01T00:00:00Z",
                ),
            )
    set_retention_policy(
        database, workspace_id=WORKSPACE, resource_type="chat_session",
        resource_id=held_session, actor_id="admin@example.com",
        reason="records hold", legal_hold=True, now=NOW,
    )

    jobs = tmp_path / "operation_jobs.db"
    terminal = []
    for key in ("purge", "hold"):
        job = enqueue_job(
            jobs, workspace_id=WORKSPACE, owner_id="owner@example.com",
            operation="test", idempotency_key=key, payload={"key": key},
            now=NOW - timedelta(days=90),
        )
        claimed = claim_next_job(
            jobs, worker_id="worker", allowed_operations=("test",), lease_seconds=30,
            now=NOW - timedelta(days=90),
        )
        assert claimed is not None
        terminal.append(complete_job(
            jobs, job=claimed, worker_id="worker", now=NOW - timedelta(days=90)
        ))
    set_retention_policy(
        database, workspace_id=WORKSPACE, resource_type="operation_job",
        resource_id=terminal[1].id, actor_id="admin@example.com",
        reason="billing dispute", legal_hold=True, now=NOW,
    )

    result = run_retention_sweep(
        database, job_database_path=jobs, workspace_id=WORKSPACE,
        data_root=tmp_path, rag_working_dir=tmp_path / "rag", now=NOW,
        job_retention_days=30,
    )
    assert result["expired_sessions"] == 1
    assert result["expired_jobs"] == 1
    with _opened(database) as connection:
        assert connection.execute("SELECT id FROM chat_sessions").fetchall() == [(held_session,)]
    with _opened(jobs) as connection:
        assert connection.execute("SELECT id FROM operation_jobs").fetchall() == [(terminal[1].id,)]
