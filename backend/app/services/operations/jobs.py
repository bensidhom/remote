"""Small durable job queue for the supported single-worker deployment.

Jobs are stored in a dedicated SQLite database under the isolated customer data
root.  The API is workspace/owner scoped, idempotent, lease based, bounded, and
recovers expired running work after process restart.  It deliberately does not
claim distributed-worker semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Mapping
from uuid import uuid4


JOB_SCHEMA_VERSION = 2
MAX_JOB_JSON_BYTES = 64 * 1024
MAX_QUEUED_PER_OWNER = 50
MAX_QUEUED_PER_WORKSPACE = 500
RESERVED_INTERACTIVE_WORKSPACE_SLOTS = 50
_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "cancelled"})


class JobStoreError(RuntimeError):
    def __init__(self, code: str, status: int = 409):
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class OperationJob:
    id: str
    workspace_id: str
    owner_id: str
    operation: str
    idempotency_key: str
    status: str
    payload: dict[str, object]
    payload_sha256: str
    attempts: int
    max_attempts: int
    progress: dict[str, object]
    result: dict[str, object] | None
    error_code: str | None
    created_at: str
    updated_at: str
    available_at: str
    lease_expires_at: str | None
    cancellation_requested_at: str | None

    def public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "operation": self.operation,
            "status": self.status,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "progress": self.progress,
            "result": self.result,
            "error_code": self.error_code,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "cancellation_requested": self.cancellation_requested_at is not None,
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _token(value: object, field: str, maximum: int = 255) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        raise ValueError(f"{field} is invalid")
    return text


def _json_object(value: Mapping[str, object] | None, field: str) -> tuple[str, dict[str, object]]:
    source = dict(value or {})
    try:
        encoded = json.dumps(
            source, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite JSON object") from exc
    if len(encoded) > MAX_JOB_JSON_BYTES:
        raise ValueError(f"{field} exceeds the bounded JSON size")
    return encoded.decode("utf-8"), json.loads(encoded)


def _connect(path: str | Path) -> sqlite3.Connection:
    database = Path(path)
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(database), timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.execute("PRAGMA busy_timeout=5000")
    # Keep this small coordination store out of WAL mode.  The application DB
    # has a separately enforced patched-runtime gate for WAL; using the rollback
    # journal here avoids silently reintroducing the documented WAL-reset race
    # through an auxiliary database on older embedded SQLite builds.
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def initialize_job_store(path: str | Path) -> None:
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS operation_jobs (
              id TEXT PRIMARY KEY NOT NULL,
              workspace_id TEXT NOT NULL,
              owner_id TEXT NOT NULL,
              operation TEXT NOT NULL,
              idempotency_key TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              payload_sha256 TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed','cancelled')),
              attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
              max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 10),
              progress_json TEXT NOT NULL DEFAULT '{}',
              result_json TEXT,
              error_code TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              available_at TEXT NOT NULL,
              lease_owner TEXT,
              lease_expires_at TEXT,
              cancellation_requested_at TEXT,
              UNIQUE(workspace_id, owner_id, operation, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS idx_operation_jobs_claim
              ON operation_jobs(status,available_at,created_at,id);
            CREATE INDEX IF NOT EXISTS idx_operation_jobs_owner
              ON operation_jobs(workspace_id,owner_id,updated_at DESC);
            CREATE TRIGGER IF NOT EXISTS trg_operation_jobs_payload_immutable
              BEFORE UPDATE OF workspace_id,owner_id,operation,idempotency_key,payload_json,payload_sha256,created_at
              ON operation_jobs BEGIN SELECT RAISE(ABORT,'job identity is immutable'); END;
            """
        )
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(operation_jobs)")
        }
        if "cancellation_requested_at" not in columns:
            connection.execute(
                "ALTER TABLE operation_jobs ADD COLUMN cancellation_requested_at TEXT"
            )
        connection.execute(f"PRAGMA user_version={JOB_SCHEMA_VERSION}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _row(row: sqlite3.Row) -> OperationJob:
    status = str(row["status"])
    if status not in _STATUSES:
        raise JobStoreError("job_status_corrupt", 500)
    return OperationJob(
        id=str(row["id"]), workspace_id=str(row["workspace_id"]),
        owner_id=str(row["owner_id"]), operation=str(row["operation"]),
        idempotency_key=str(row["idempotency_key"]), status=status,
        payload=json.loads(row["payload_json"]), payload_sha256=str(row["payload_sha256"]),
        attempts=int(row["attempts"]), max_attempts=int(row["max_attempts"]),
        progress=json.loads(row["progress_json"]),
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        error_code=row["error_code"], created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]), available_at=str(row["available_at"]),
        lease_expires_at=row["lease_expires_at"],
        cancellation_requested_at=row["cancellation_requested_at"],
    )


def enqueue_job(
    path: str | Path,
    *,
    workspace_id: str,
    owner_id: str,
    operation: str,
    idempotency_key: str,
    payload: Mapping[str, object],
    max_attempts: int = 3,
    now: datetime | None = None,
) -> OperationJob:
    initialize_job_store(path)
    workspace = _token(workspace_id, "workspace_id")
    owner = _token(str(owner_id).lower(), "owner_id")
    kind = _token(operation, "operation", 64)
    key = _token(idempotency_key, "idempotency_key", 255)
    if not 1 <= int(max_attempts) <= 10:
        raise ValueError("max_attempts must be between 1 and 10")
    encoded, safe_payload = _json_object(payload, "payload")
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    instant = now or _now()
    timestamp = _iso(instant)
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM operation_jobs WHERE workspace_id=? AND owner_id=? "
            "AND operation=? AND idempotency_key=?",
            (workspace, owner, kind, key),
        ).fetchone()
        if existing is not None:
            if str(existing["payload_sha256"]) != digest:
                raise JobStoreError("idempotency_payload_conflict")
            connection.commit()
            return _row(existing)
        queued_workspace = int(connection.execute(
            "SELECT COUNT(*) FROM operation_jobs WHERE workspace_id=? AND status IN ('queued','running')",
            (workspace,),
        ).fetchone()[0])
        queued_owner = int(connection.execute(
            "SELECT COUNT(*) FROM operation_jobs WHERE workspace_id=? AND owner_id=? "
            "AND status IN ('queued','running')", (workspace, owner),
        ).fetchone()[0])
        if queued_workspace >= MAX_QUEUED_PER_WORKSPACE:
            raise JobStoreError("workspace_job_queue_full", 429)
        if (
            kind == "report_index"
            and queued_workspace
            >= MAX_QUEUED_PER_WORKSPACE - RESERVED_INTERACTIVE_WORKSPACE_SLOTS
        ):
            # Background reconciliation must not consume every durable slot
            # and make a user upload impossible to enqueue.  The deployment's
            # one worker prioritizes import_preflight jobs as well.
            raise JobStoreError("workspace_job_queue_reserved", 429)
        if queued_owner >= MAX_QUEUED_PER_OWNER:
            raise JobStoreError("owner_job_queue_full", 429)
        job_id = str(uuid4())
        connection.execute(
            "INSERT INTO operation_jobs "
            "(id,workspace_id,owner_id,operation,idempotency_key,payload_json,payload_sha256,"
            "status,max_attempts,created_at,updated_at,available_at) "
            "VALUES (?,?,?,?,?,?,?,'queued',?,?,?,?)",
            (job_id, workspace, owner, kind, key, encoded, digest, int(max_attempts),
             timestamp, timestamp, timestamp),
        )
        created = connection.execute(
            "SELECT * FROM operation_jobs WHERE id=?", (job_id,)
        ).fetchone()
        connection.commit()
        assert created is not None
        return _row(created)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def claim_next_job(
    path: str | Path,
    *,
    worker_id: str,
    allowed_operations: tuple[str, ...],
    lease_seconds: int = 300,
    now: datetime | None = None,
) -> OperationJob | None:
    initialize_job_store(path)
    worker = _token(worker_id, "worker_id")
    operations = tuple(_token(item, "operation", 64) for item in allowed_operations)
    if not operations or not 10 <= int(lease_seconds) <= 3600:
        raise ValueError("claim settings are invalid")
    instant = now or _now()
    timestamp = _iso(instant)
    expiry = _iso(instant + timedelta(seconds=int(lease_seconds)))
    placeholders = ",".join("?" for _ in operations)
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        # Expired running work is recoverable until max_attempts is reached.
        connection.execute(
            "UPDATE operation_jobs SET status=CASE WHEN attempts<max_attempts THEN 'queued' ELSE 'failed' END, "
            "error_code=CASE WHEN attempts<max_attempts THEN NULL ELSE 'retry_exhausted' END, "
            "lease_owner=NULL,lease_expires_at=NULL,available_at=?,updated_at=? "
            "WHERE status='running' AND cancellation_requested_at IS NULL "
            "AND lease_expires_at<=?",
            (timestamp, timestamp, timestamp),
        )
        # A cancellation request survives a worker/process crash.  Once its
        # lease expires, no replacement worker may resume provider work.
        connection.execute(
            "UPDATE operation_jobs SET status='cancelled',error_code='cancelled_by_user',"
            "lease_owner=NULL,lease_expires_at=NULL,updated_at=? "
            "WHERE status='running' AND cancellation_requested_at IS NOT NULL "
            "AND lease_expires_at<=?",
            (timestamp, timestamp),
        )
        candidate = connection.execute(
            f"SELECT id FROM operation_jobs WHERE status='queued' AND available_at<=? "
            f"AND cancellation_requested_at IS NULL AND operation IN ({placeholders}) "
            f"ORDER BY available_at,created_at,id LIMIT 1",
            (timestamp, *operations),
        ).fetchone()
        if candidate is None:
            connection.commit()
            return None
        job_id = str(candidate[0])
        connection.execute(
            "UPDATE operation_jobs SET status='running',attempts=attempts+1,lease_owner=?,"
            "lease_expires_at=?,updated_at=? WHERE id=? AND status='queued'",
            (worker, expiry, timestamp, job_id),
        )
        claimed = connection.execute("SELECT * FROM operation_jobs WHERE id=?", (job_id,)).fetchone()
        connection.commit()
        return _row(claimed) if claimed else None
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_job(
    path: str | Path, *, workspace_id: str, owner_id: str, job_id: str
) -> OperationJob:
    initialize_job_store(path)
    connection = _connect(path)
    try:
        row = connection.execute(
            "SELECT * FROM operation_jobs WHERE workspace_id=? AND owner_id=? AND id=?",
            (workspace_id, str(owner_id).lower(), job_id),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise JobStoreError("job_not_found", 404)
    return _row(row)


def get_job_by_idempotency(
    path: str | Path,
    *,
    workspace_id: str,
    owner_id: str,
    operation: str,
    idempotency_key: str,
) -> OperationJob:
    """Resolve an idempotent request without exposing another owner's job.

    Upload adapters use this read before accepting a replay's bytes.  Keeping
    the lookup in the job store means a retry can return the original durable
    job and immutable artifact identity instead of manufacturing a second
    source receipt which would then conflict with the original job payload.
    """

    initialize_job_store(path)
    connection = _connect(path)
    try:
        row = connection.execute(
            "SELECT * FROM operation_jobs WHERE workspace_id=? AND owner_id=? "
            "AND operation=? AND idempotency_key=?",
            (
                _token(workspace_id, "workspace_id"),
                _token(str(owner_id).lower(), "owner_id"),
                _token(operation, "operation", 64),
                _token(idempotency_key, "idempotency_key", 255),
            ),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise JobStoreError("job_not_found", 404)
    return _row(row)


def update_job_progress(
    path: str | Path,
    *,
    job: OperationJob,
    worker_id: str,
    progress: Mapping[str, object],
    now: datetime | None = None,
) -> OperationJob:
    """Persist one bounded progress snapshot under the active worker lease."""

    encoded, _safe = _json_object(progress, "progress")
    timestamp = _iso(now or _now())
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            "UPDATE operation_jobs SET progress_json=?,updated_at=? WHERE id=? "
            "AND workspace_id=? AND owner_id=? AND status='running' AND lease_owner=?",
            (
                encoded,
                timestamp,
                job.id,
                job.workspace_id,
                job.owner_id,
                _token(worker_id, "worker_id"),
            ),
        )
        if updated.rowcount != 1:
            raise JobStoreError("job_lease_lost")
        row = connection.execute(
            "SELECT * FROM operation_jobs WHERE id=?", (job.id,)
        ).fetchone()
        connection.commit()
        assert row is not None
        return _row(row)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def cancel_job(
    path: str | Path,
    *,
    workspace_id: str,
    owner_id: str,
    job_id: str,
    now: datetime | None = None,
) -> OperationJob:
    """Cancel queued or completed pre-commit work under exact ownership.

    A running lease cannot be revoked safely in the supported in-process
    worker, so running work returns a typed conflict.  Calling this operation
    repeatedly is idempotent.  Domain adapters must separately prove that a
    succeeded job has not already caused an irreversible mutation.
    """

    initialize_job_store(path)
    workspace = _token(workspace_id, "workspace_id")
    owner = _token(str(owner_id).lower(), "owner_id")
    identifier = _token(job_id, "job_id")
    timestamp = _iso(now or _now())
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM operation_jobs WHERE workspace_id=? AND owner_id=? AND id=?",
            (workspace, owner, identifier),
        ).fetchone()
        if row is None:
            raise JobStoreError("job_not_found", 404)
        status = str(row["status"])
        if status == "running":
            raise JobStoreError("job_running", 409)
        if status != "cancelled":
            connection.execute(
                "UPDATE operation_jobs SET status='cancelled',updated_at=?,"
                "lease_owner=NULL,lease_expires_at=NULL WHERE id=? AND workspace_id=? "
                "AND owner_id=? AND status IN ('queued','succeeded','failed')",
                (timestamp, identifier, workspace, owner),
            )
        updated = connection.execute(
            "SELECT * FROM operation_jobs WHERE id=?", (identifier,)
        ).fetchone()
        connection.commit()
        assert updated is not None
        return _row(updated)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def request_job_cancellation(
    path: str | Path,
    *,
    workspace_id: str,
    owner_id: str,
    job_id: str,
    now: datetime | None = None,
) -> OperationJob:
    """Durably request cancellation without revoking an active worker lease.

    Queued work becomes terminal immediately.  Running work retains its lease
    and receives a durable marker which operation-specific cancellation tokens
    poll before and after provider calls.  Provider calls already in flight are
    not claimed to be interruptible.  Terminal work is returned unchanged.
    """

    initialize_job_store(path)
    workspace = _token(workspace_id, "workspace_id")
    owner = _token(str(owner_id).lower(), "owner_id")
    identifier = _token(job_id, "job_id")
    timestamp = _iso(now or _now())
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM operation_jobs WHERE workspace_id=? AND owner_id=? AND id=?",
            (workspace, owner, identifier),
        ).fetchone()
        if row is None:
            raise JobStoreError("job_not_found", 404)
        status = str(row["status"])
        if status == "queued":
            connection.execute(
                "UPDATE operation_jobs SET status='cancelled',error_code='cancelled_by_user',"
                "cancellation_requested_at=COALESCE(cancellation_requested_at,?),"
                "updated_at=?,lease_owner=NULL,lease_expires_at=NULL "
                "WHERE id=? AND workspace_id=? AND owner_id=? AND status='queued'",
                (timestamp, timestamp, identifier, workspace, owner),
            )
        elif status == "running":
            connection.execute(
                "UPDATE operation_jobs SET cancellation_requested_at="
                "COALESCE(cancellation_requested_at,?),updated_at=? "
                "WHERE id=? AND workspace_id=? AND owner_id=? AND status='running'",
                (timestamp, timestamp, identifier, workspace, owner),
            )
        updated = connection.execute(
            "SELECT * FROM operation_jobs WHERE id=?", (identifier,)
        ).fetchone()
        connection.commit()
        assert updated is not None
        return _row(updated)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def is_job_cancellation_requested(
    path: str | Path,
    *,
    workspace_id: str,
    owner_id: str,
    job_id: str,
) -> bool:
    """Return the exact owned job's durable cancellation state."""

    initialize_job_store(path)
    connection = _connect(path)
    try:
        row = connection.execute(
            "SELECT status,cancellation_requested_at FROM operation_jobs "
            "WHERE workspace_id=? AND owner_id=? AND id=?",
            (
                _token(workspace_id, "workspace_id"),
                _token(str(owner_id).lower(), "owner_id"),
                _token(job_id, "job_id"),
            ),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise JobStoreError("job_not_found", 404)
    return bool(row["cancellation_requested_at"] is not None or row["status"] == "cancelled")


def _finish(
    path: str | Path,
    *,
    job: OperationJob,
    worker_id: str,
    status: str,
    result: Mapping[str, object] | None,
    error_code: str | None,
    retry_delay_seconds: int = 0,
    now: datetime | None = None,
) -> OperationJob:
    if status not in {"succeeded", "failed", "queued"}:
        raise ValueError("terminal job status is invalid")
    result_json = None
    if result is not None:
        result_json, _safe = _json_object(result, "result")
    instant = now or _now()
    timestamp = _iso(instant)
    available = _iso(instant + timedelta(seconds=max(0, int(retry_delay_seconds))))
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT cancellation_requested_at FROM operation_jobs WHERE id=? "
            "AND workspace_id=? AND owner_id=? AND status='running' AND lease_owner=?",
            (job.id, job.workspace_id, job.owner_id, worker_id),
        ).fetchone()
        if current is None:
            raise JobStoreError("job_lease_lost")
        cancelled = current["cancellation_requested_at"] is not None
        updated = connection.execute(
            "UPDATE operation_jobs SET status=?,result_json=?,error_code=?,updated_at=?,"
            "available_at=?,lease_owner=NULL,lease_expires_at=NULL "
            "WHERE id=? AND workspace_id=? AND owner_id=? AND status='running' AND lease_owner=?",
            (
                "cancelled" if cancelled else status,
                None if cancelled else result_json,
                "cancelled_by_user" if cancelled else error_code,
                timestamp,
                available,
                job.id,
                job.workspace_id,
                job.owner_id,
                worker_id,
            ),
        )
        if updated.rowcount != 1:
            raise JobStoreError("job_lease_lost")
        row = connection.execute("SELECT * FROM operation_jobs WHERE id=?", (job.id,)).fetchone()
        connection.commit()
        assert row is not None
        return _row(row)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def complete_job(
    path: str | Path, *, job: OperationJob, worker_id: str,
    result: Mapping[str, object] | None = None, now: datetime | None = None,
) -> OperationJob:
    return _finish(
        path, job=job, worker_id=worker_id, status="succeeded", result=result,
        error_code=None, now=now,
    )


def fail_job(
    path: str | Path, *, job: OperationJob, worker_id: str, error_code: str,
    retry: bool = True, now: datetime | None = None,
) -> OperationJob:
    should_retry = bool(retry and job.attempts < job.max_attempts)
    return _finish(
        path, job=job, worker_id=worker_id,
        status="queued" if should_retry else "failed", result=None,
        error_code=None if should_retry else _token(error_code, "error_code", 64),
        retry_delay_seconds=min(300, 2 ** max(0, job.attempts - 1)) if should_retry else 0,
        now=now,
    )


def purge_terminal_jobs(
    path: str | Path,
    *,
    workspace_id: str,
    older_than: datetime,
    held_job_ids: frozenset[str] | set[str] | tuple[str, ...] = (),
) -> int:
    """Delete old terminal receipts for one workspace, excluding legal holds.

    Queued/running work is never selected.  Callers obtain held IDs from the
    customer database's retention catalog, keeping this coordination database
    small without weakening workspace isolation.
    """

    initialize_job_store(path)
    workspace = _token(workspace_id, "workspace_id")
    if older_than.tzinfo is None:
        raise ValueError("older_than must be timezone-aware")
    cutoff = _iso(older_than.astimezone(timezone.utc))
    held = tuple(sorted({_token(value, "held_job_id") for value in held_job_ids}))
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        parameters: list[object] = [workspace, cutoff]
        sql = (
            "DELETE FROM operation_jobs WHERE workspace_id=? "
            "AND status IN ('succeeded','failed','cancelled') AND updated_at<=?"
        )
        if held:
            sql += " AND id NOT IN (" + ",".join("?" for _ in held) + ")"
            parameters.extend(held)
        cursor = connection.execute(sql, tuple(parameters))
        connection.commit()
        return max(0, int(cursor.rowcount))
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


__all__ = [
    "JOB_SCHEMA_VERSION", "RESERVED_INTERACTIVE_WORKSPACE_SLOTS", "JobStoreError", "OperationJob", "cancel_job",
    "claim_next_job", "complete_job", "enqueue_job", "fail_job", "get_job",
    "get_job_by_idempotency", "initialize_job_store", "purge_terminal_jobs",
    "is_job_cancellation_requested", "request_job_cancellation", "update_job_progress",
]
