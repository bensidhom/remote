"""Release contracts for the disposable production SQLite scale gate."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from app.routers.services.sqlite_runtime import inspect_sqlite_wal_runtime
from app.services.operations import sqlite_scale_gate


def _approve_pinned_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    approved = inspect_sqlite_wal_runtime("wal", version=(3, 53, 4), environ={})
    monkeypatch.setattr(
        sqlite_scale_gate,
        "require_safe_sqlite_wal_runtime",
        lambda _journal_mode: approved,
    )


def test_release_gate_exercises_two_writers_twenty_readers_and_online_restore(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # The host test runtime may predate the approved WAL fix.  Approval is
    # injected only for this disposable behavioral exercise; deployment runs
    # the same function against the container's pinned SQLite 3.53.4 runtime.
    _approve_pinned_runtime(monkeypatch)

    receipt = sqlite_scale_gate.run_sqlite_scale_gate(
        writer_iterations=3,
        gate_timeout_seconds=15,
        scratch_root=test_workspace,
    )

    assert receipt.writers == 2
    assert receipt.readers == 20
    assert receipt.committed_writes == 6
    assert receipt.final_data_generation == receipt.committed_writes
    assert receipt.reader_snapshots_verified == receipt.readers
    assert receipt.reader_refreshes_verified == receipt.readers
    assert receipt.deterministic_busy_wait_ms >= 5
    assert receipt.maximum_observed_busy_wait_ms < 1_000
    assert receipt.cancellation_latency_ms < 1_000
    assert receipt.passive_checkpoint_busy == 0
    assert receipt.passive_checkpointed_frames == receipt.passive_checkpoint_log_frames
    assert receipt.truncate_checkpoint_busy == 0
    assert receipt.wal_bytes_before_checkpoint > 0
    assert receipt.wal_bytes_after_checkpoint == 0
    assert 1 <= receipt.concurrent_backup_generation <= receipt.committed_writes
    assert receipt.concurrent_backup_generation == receipt.concurrent_backup_rows
    assert receipt.restored_generation == receipt.committed_writes
    assert receipt.restored_rows == receipt.committed_writes
    assert receipt.restore_quick_check == "ok"
    assert receipt.restore_foreign_key_issues == 0
    assert len(receipt.logical_sha256) == 64
    assert receipt.elapsed_ms < 15_000

    # A successful run leaves no database, WAL, SHM, backup, or path in its
    # public receipt.  It can never be mistaken for a customer-data backup.
    assert list(test_workspace.iterdir()) == []
    serialized = json.dumps(receipt.public_dict(), sort_keys=True)
    assert str(test_workspace) not in serialized
    assert "disposable-scale-gate.db" not in serialized


def test_gate_fails_runtime_approval_before_creating_scratch_artifacts(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    def reject(_journal_mode: str):
        raise RuntimeError("unapproved WAL runtime")

    monkeypatch.setattr(
        sqlite_scale_gate, "require_safe_sqlite_wal_runtime", reject
    )

    with pytest.raises(RuntimeError, match="unapproved WAL runtime"):
        sqlite_scale_gate.run_sqlite_scale_gate(scratch_root=test_workspace)

    assert list(test_workspace.iterdir()) == []


@pytest.mark.parametrize(
    ("keyword", "value", "message"),
    [
        ("writer_count", 1, "writer_count must be at least 2"),
        ("reader_count", 19, "reader_count must be at least 20"),
        ("busy_timeout_ms", 0, "busy_timeout_ms must be between"),
    ],
)
def test_gate_rejects_a_weaker_release_shape(keyword: str, value: int, message: str):
    with pytest.raises(ValueError, match=message):
        sqlite_scale_gate.run_sqlite_scale_gate(**{keyword: value})


def test_gate_api_and_cli_cannot_accept_a_customer_database_path():
    signature = inspect.signature(sqlite_scale_gate.run_sqlite_scale_gate)
    assert "database" not in signature.parameters
    assert "database_path" not in signature.parameters

    root = Path(__file__).resolve().parents[2]
    script = (root / "backend/scripts/run_sqlite_scale_gate.py").read_text(
        encoding="utf-8"
    )
    verify = (root / "deploy/oci/verify.sh").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "--database" not in script
    assert "USER_DB" not in script
    assert "python -m scripts.run_sqlite_scale_gate" in verify
    assert "-m scripts.run_sqlite_scale_gate" in workflow
