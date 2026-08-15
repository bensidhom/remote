from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
import threading

import pytest

from app.services.operations import jobs as job_store
from app.services.operations.jobs import (
    JobStoreError,
    cancel_job,
    claim_next_job,
    complete_job,
    enqueue_job,
    fail_job,
    get_job,
    get_job_by_idempotency,
    initialize_job_store,
    update_job_progress,
)


NOW = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)


def test_enqueue_is_durable_idempotent_and_payload_conflicts(tmp_path):
    path = tmp_path / "jobs.db"
    first = enqueue_job(
        path, workspace_id="ws-a", owner_id="a@example.com",
        operation="report_index", idempotency_key="source-sha", payload={"bridge": "B1"},
        now=NOW,
    )
    same = enqueue_job(
        path, workspace_id="ws-a", owner_id="a@example.com",
        operation="report_index", idempotency_key="source-sha", payload={"bridge": "B1"},
        now=NOW,
    )
    assert first.id == same.id
    assert get_job_by_idempotency(
        path,
        workspace_id="ws-a",
        owner_id="a@example.com",
        operation="report_index",
        idempotency_key="source-sha",
    ).id == first.id
    assert get_job(
        path, workspace_id="ws-a", owner_id="a@example.com", job_id=first.id
    ).payload == {"bridge": "B1"}
    with pytest.raises(JobStoreError) as conflict:
        enqueue_job(
            path, workspace_id="ws-a", owner_id="a@example.com",
            operation="report_index", idempotency_key="source-sha", payload={"bridge": "B2"},
            now=NOW,
        )
    assert conflict.value.code == "idempotency_payload_conflict"


def test_cancel_is_owner_scoped_idempotent_and_never_revokes_a_running_lease(tmp_path):
    path = tmp_path / "jobs.db"
    queued = enqueue_job(
        path,
        workspace_id="ws",
        owner_id="a@example.com",
        operation="import_preflight",
        idempotency_key="cancel-me",
        payload={},
        now=NOW,
    )
    with pytest.raises(JobStoreError) as hidden:
        cancel_job(
            path,
            workspace_id="ws",
            owner_id="other@example.com",
            job_id=queued.id,
            now=NOW,
        )
    assert hidden.value.code == "job_not_found"
    cancelled = cancel_job(
        path,
        workspace_id="ws",
        owner_id="a@example.com",
        job_id=queued.id,
        now=NOW,
    )
    assert cancelled.status == "cancelled"
    assert cancel_job(
        path,
        workspace_id="ws",
        owner_id="a@example.com",
        job_id=queued.id,
        now=NOW,
    ).status == "cancelled"

    running = enqueue_job(
        path,
        workspace_id="ws",
        owner_id="a@example.com",
        operation="import_preflight",
        idempotency_key="running",
        payload={},
        now=NOW,
    )
    claimed = claim_next_job(
        path,
        worker_id="worker",
        allowed_operations=("import_preflight",),
        now=NOW,
    )
    assert claimed and claimed.id == running.id
    with pytest.raises(JobStoreError) as unsafe:
        cancel_job(
            path,
            workspace_id="ws",
            owner_id="a@example.com",
            job_id=running.id,
            now=NOW,
        )
    assert unsafe.value.code == "job_running"


def test_background_reports_reserve_workspace_capacity_for_imports(tmp_path, monkeypatch):
    path = tmp_path / "jobs.db"
    monkeypatch.setattr(job_store, "MAX_QUEUED_PER_WORKSPACE", 3)
    monkeypatch.setattr(job_store, "RESERVED_INTERACTIVE_WORKSPACE_SLOTS", 1)
    for index in range(2):
        enqueue_job(
            path,
            workspace_id="ws",
            owner_id=f"report-{index}@bridgeguard",
            operation="report_index",
            idempotency_key=f"report-{index}",
            payload={},
            now=NOW,
        )
    with pytest.raises(JobStoreError) as reserved:
        enqueue_job(
            path,
            workspace_id="ws",
            owner_id="report-2@bridgeguard",
            operation="report_index",
            idempotency_key="report-2",
            payload={},
            now=NOW,
        )
    assert reserved.value.code == "workspace_job_queue_reserved"
    assert enqueue_job(
        path,
        workspace_id="ws",
        owner_id="editor@example.com",
        operation="import_preflight",
        idempotency_key="interactive-import",
        payload={},
        now=NOW,
    ).status == "queued"


def test_claim_complete_and_non_enumerating_owner_scope(tmp_path):
    path = tmp_path / "jobs.db"
    queued = enqueue_job(
        path, workspace_id="ws", owner_id="a@example.com", operation="report_index",
        idempotency_key="k", payload={"state": 34}, now=NOW,
    )
    claimed = claim_next_job(
        path, worker_id="worker-1", allowed_operations=("report_index",), now=NOW
    )
    assert claimed and claimed.id == queued.id and claimed.attempts == 1
    claimed = update_job_progress(
        path,
        job=claimed,
        worker_id="worker-1",
        progress={"phase": "running", "completed": 1, "total": 2},
        now=NOW,
    )
    assert claimed.progress == {"phase": "running", "completed": 1, "total": 2}
    with pytest.raises(JobStoreError, match="job_lease_lost"):
        update_job_progress(
            path,
            job=claimed,
            worker_id="other-worker",
            progress={"phase": "stolen"},
            now=NOW,
        )
    done = complete_job(
        path, job=claimed, worker_id="worker-1", result={"indexed": True}, now=NOW
    )
    assert done.status == "succeeded" and done.result == {"indexed": True}
    with pytest.raises(JobStoreError) as hidden:
        get_job(path, workspace_id="ws", owner_id="b@example.com", job_id=queued.id)
    assert hidden.value.status == 404


def test_expired_lease_recovers_after_restart_and_exhaustion_is_terminal(tmp_path):
    path = tmp_path / "jobs.db"
    enqueue_job(
        path, workspace_id="ws", owner_id="a@example.com", operation="report_index",
        idempotency_key="k", payload={}, max_attempts=2, now=NOW,
    )
    first = claim_next_job(
        path, worker_id="old", allowed_operations=("report_index",),
        lease_seconds=10, now=NOW,
    )
    assert first is not None
    recovered = claim_next_job(
        path, worker_id="new", allowed_operations=("report_index",),
        lease_seconds=10, now=NOW + timedelta(seconds=11),
    )
    assert recovered and recovered.id == first.id and recovered.attempts == 2
    # A second crash exhausts the configured attempts on the next recovery scan.
    assert claim_next_job(
        path, worker_id="third", allowed_operations=("report_index",),
        lease_seconds=10, now=NOW + timedelta(seconds=22),
    ) is None
    terminal = get_job(path, workspace_id="ws", owner_id="a@example.com", job_id=first.id)
    assert terminal.status == "failed" and terminal.error_code == "retry_exhausted"


def test_retry_backoff_and_exact_worker_lease(tmp_path):
    path = tmp_path / "jobs.db"
    enqueue_job(
        path, workspace_id="ws", owner_id="a@example.com", operation="report_index",
        idempotency_key="k", payload={}, now=NOW,
    )
    job = claim_next_job(
        path, worker_id="worker", allowed_operations=("report_index",), now=NOW
    )
    assert job
    with pytest.raises(JobStoreError, match="job_lease_lost"):
        complete_job(path, job=job, worker_id="attacker", now=NOW)
    queued = fail_job(
        path, job=job, worker_id="worker", error_code="provider_failed", now=NOW
    )
    assert queued.status == "queued" and queued.error_code is None
    assert claim_next_job(
        path, worker_id="worker", allowed_operations=("report_index",), now=NOW
    ) is None
    assert claim_next_job(
        path, worker_id="worker", allowed_operations=("report_index",),
        now=NOW + timedelta(seconds=2),
    ) is not None


def test_two_claimers_cannot_own_the_same_job(tmp_path):
    path = tmp_path / "jobs.db"
    enqueue_job(
        path, workspace_id="ws", owner_id="a@example.com", operation="report_index",
        idempotency_key="k", payload={}, now=NOW,
    )
    barrier = threading.Barrier(2)
    claims = []
    lock = threading.Lock()

    def claim(worker: str):
        barrier.wait()
        value = claim_next_job(
            path, worker_id=worker, allowed_operations=("report_index",), now=NOW
        )
        with lock:
            claims.append(value)

    threads = [threading.Thread(target=claim, args=(f"w{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(value is not None for value in claims) == 1


def test_schema_identity_is_immutable_and_health_is_clean(tmp_path):
    path = tmp_path / "jobs.db"
    initialize_job_store(path)
    job = enqueue_job(
        path, workspace_id="ws", owner_id="a@example.com", operation="report_index",
        idempotency_key="k", payload={}, now=NOW,
    )
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE operation_jobs SET workspace_id='other' WHERE id=?", (job.id,)
            )
    finally:
        connection.close()
