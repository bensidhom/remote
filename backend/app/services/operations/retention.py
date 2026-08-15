"""Retention, legal-hold, and deletion-outbox controls.

Attachment catalog deletion and its durable receipt live in the same SQLite
transaction.  Files and derived report indexes are removed only after that
transaction commits.  Failed filesystem work remains in a bounded, retryable
outbox; public receipts deliberately omit storage paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from typing import Callable, Mapping
from uuid import UUID, uuid4

from ..conversion.source_artifacts import (
    ArtifactErrorCode,
    ArtifactSweepReport,
    SourceArtifactError,
    SourceArtifactStore,
)
from .storage_paths import StoragePathError, resolve_confined_path


_RESOURCE_TYPES = frozenset(
    {"attachment", "chat_session", "operation_job", "source_artifact"}
)
_ATTACHMENT_TYPES = frozenset({"inspection_report", "point_cloud"})
_TERMINAL_TOMBSTONE_STATUSES = frozenset({"complete"})
_MAX_REASON_CHARACTERS = 1000
_MAX_CACHE_MANIFESTS = 4096
_MAX_CACHE_JSON_BYTES = 2 * 1024 * 1024
DEFAULT_JOB_RETENTION_DAYS = 30
DEFAULT_SOURCE_ARTIFACT_RETENTION_DAYS = 90
MAX_ARTIFACT_REFERENCE_ROWS = 1_000_000
MAX_PREFLIGHT_REFERENCE_BYTES = 512 * 1024 * 1024


class RetentionError(RuntimeError):
    def __init__(self, code: str, status: int = 409):
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    workspace_id: str
    resource_type: str
    resource_id: str
    retain_until: str | None
    legal_hold: bool
    updated_at: str

    def public_dict(self) -> dict[str, object]:
        return {
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "retain_until": self.retain_until,
            "legal_hold": self.legal_hold,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class DeletionReceipt:
    id: str
    workspace_id: str
    attachment_id: int
    attachment_type: str
    source_sha256: str
    status: str
    requested_at: str
    source_deleted_at: str | None
    cache_purged_at: str | None
    attempts: int
    last_error_code: str | None

    def public_dict(self) -> dict[str, object]:
        return {
            "deletion_id": self.id,
            "attachment_id": self.attachment_id,
            "attachment_type": self.attachment_type,
            "source_sha256": self.source_sha256,
            "status": self.status,
            "requested_at": self.requested_at,
            "source_deleted_at": self.source_deleted_at,
            "cache_purged_at": self.cache_purged_at,
            "attempts": self.attempts,
            "last_error_code": self.last_error_code,
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _token(value: object, field: str, maximum: int = 255) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        raise RetentionError(f"invalid_{field}", 400)
    return text


def _reason(value: object) -> str:
    return _token(value, "deletion_reason", _MAX_REASON_CHARACTERS)


def _parse_time(value: str | datetime | None, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise RetentionError(f"invalid_{field}", 400) from exc


def _connect(database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database_path), timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


@contextmanager
def _opened(database_path: str | Path):
    connection = _connect(database_path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_retention_store(database_path: str | Path) -> None:
    """Create additive operational tables without adopting attachment rows."""

    with _opened(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS retention_resource_policies (
              workspace_id TEXT NOT NULL,
              resource_type TEXT NOT NULL CHECK(resource_type IN ('attachment','chat_session','operation_job')),
              resource_id TEXT NOT NULL,
              retain_until TEXT,
              legal_hold INTEGER NOT NULL DEFAULT 0 CHECK(legal_hold IN (0,1)),
              updated_by TEXT NOT NULL,
              update_reason TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(workspace_id,resource_type,resource_id)
            );
            CREATE INDEX IF NOT EXISTS idx_retention_policy_expiry
              ON retention_resource_policies(workspace_id,resource_type,legal_hold,retain_until);

            CREATE TABLE IF NOT EXISTS source_artifact_retention_policies (
              workspace_id TEXT NOT NULL,
              artifact_id TEXT NOT NULL,
              retain_until TEXT,
              legal_hold INTEGER NOT NULL DEFAULT 0 CHECK(legal_hold IN (0,1)),
              updated_by TEXT NOT NULL,
              update_reason TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(workspace_id,artifact_id)
            );
            CREATE INDEX IF NOT EXISTS idx_source_artifact_retention_expiry
              ON source_artifact_retention_policies(workspace_id,legal_hold,retain_until,artifact_id);

            CREATE TABLE IF NOT EXISTS attachment_deletion_tombstones (
              id TEXT PRIMARY KEY NOT NULL,
              workspace_id TEXT NOT NULL,
              attachment_id INTEGER NOT NULL,
              attachment_type TEXT NOT NULL CHECK(attachment_type IN ('inspection_report','point_cloud')),
              state_code TEXT NOT NULL,
              bridge_number TEXT NOT NULL,
              source_sha256 TEXT NOT NULL,
              source_portable_path TEXT NOT NULL,
              requested_by TEXT NOT NULL,
              reason TEXT NOT NULL,
              requested_at TEXT NOT NULL,
              catalog_deleted_at TEXT NOT NULL,
              source_deleted_at TEXT,
              cache_purged_at TEXT,
              status TEXT NOT NULL CHECK(status IN ('pending','retry','held','complete')),
              attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
              next_attempt_at TEXT NOT NULL,
              last_error_code TEXT,
              UNIQUE(workspace_id,attachment_id)
            );
            CREATE INDEX IF NOT EXISTS idx_attachment_deletion_outbox
              ON attachment_deletion_tombstones(status,next_attempt_at,requested_at,id);
            CREATE TRIGGER IF NOT EXISTS trg_attachment_deletion_tombstones_no_delete
              BEFORE DELETE ON attachment_deletion_tombstones
              BEGIN SELECT RAISE(ABORT,'attachment deletion tombstones are append-only'); END;

            CREATE TABLE IF NOT EXISTS retention_audit_events (
              id TEXT PRIMARY KEY NOT NULL,
              workspace_id TEXT NOT NULL,
              resource_type TEXT NOT NULL,
              resource_id TEXT NOT NULL,
              action TEXT NOT NULL,
              actor_id TEXT NOT NULL,
              reason TEXT NOT NULL,
              occurred_at TEXT NOT NULL,
              details_json TEXT NOT NULL,
              previous_event_sha256 TEXT,
              event_sha256 TEXT NOT NULL UNIQUE
            );
            CREATE INDEX IF NOT EXISTS idx_retention_audit_resource
              ON retention_audit_events(workspace_id,resource_type,resource_id,occurred_at,id);
            CREATE TRIGGER IF NOT EXISTS trg_retention_audit_no_update
              BEFORE UPDATE ON retention_audit_events
              BEGIN SELECT RAISE(ABORT,'retention audit is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS trg_retention_audit_no_delete
              BEFORE DELETE ON retention_audit_events
              BEGIN SELECT RAISE(ABORT,'retention audit is append-only'); END;
            """
        )


def _audit(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    resource_type: str,
    resource_id: str,
    action: str,
    actor_id: str,
    reason: str,
    occurred_at: str,
    details: Mapping[str, object] | None = None,
) -> None:
    previous_row = connection.execute(
        "SELECT event_sha256 FROM retention_audit_events WHERE workspace_id=? "
        "ORDER BY rowid DESC LIMIT 1",
        (workspace_id,),
    ).fetchone()
    previous = str(previous_row[0]) if previous_row else None
    event_id = str(uuid4())
    safe_details = json.dumps(
        dict(details or {}), ensure_ascii=True, allow_nan=False,
        sort_keys=True, separators=(",", ":"),
    )
    if len(safe_details.encode("utf-8")) > 16 * 1024:
        raise RetentionError("audit_details_too_large", 400)
    body = json.dumps(
        {
            "id": event_id,
            "workspace_id": workspace_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "action": action,
            "actor_id": actor_id,
            "reason": reason,
            "occurred_at": occurred_at,
            "details_json": safe_details,
            "previous_event_sha256": previous,
        },
        ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    )
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    connection.execute(
        "INSERT INTO retention_audit_events "
        "(id,workspace_id,resource_type,resource_id,action,actor_id,reason,"
        "occurred_at,details_json,previous_event_sha256,event_sha256) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            event_id, workspace_id, resource_type, resource_id, action, actor_id,
            reason, occurred_at, safe_details, previous, digest,
        ),
    )


def set_retention_policy(
    database_path: str | Path,
    *,
    workspace_id: str,
    resource_type: str,
    resource_id: str | int,
    actor_id: str,
    reason: str,
    retain_until: str | datetime | None = None,
    legal_hold: bool = False,
    now: datetime | None = None,
) -> RetentionPolicy:
    initialize_retention_store(database_path)
    workspace = _token(workspace_id, "workspace_id")
    kind = _token(resource_type, "resource_type", 64)
    if kind not in _RESOURCE_TYPES:
        raise RetentionError("invalid_resource_type", 400)
    resource = _token(resource_id, "resource_id")
    actor = _token(str(actor_id).lower(), "actor_id")
    why = _reason(reason)
    parsed_until = _parse_time(retain_until, "retain_until")
    timestamp = _iso((now or _utc_now()).astimezone(timezone.utc))
    until_iso = _iso(parsed_until) if parsed_until else None
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if kind == "source_artifact":
            try:
                resource = str(UUID(resource))
            except (TypeError, ValueError, AttributeError) as exc:
                raise RetentionError("invalid_resource_id", 400) from exc
            connection.execute(
                "INSERT INTO source_artifact_retention_policies "
                "(workspace_id,artifact_id,retain_until,legal_hold,"
                "updated_by,update_reason,updated_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(workspace_id,artifact_id) DO UPDATE SET "
                "retain_until=excluded.retain_until,legal_hold=excluded.legal_hold,"
                "updated_by=excluded.updated_by,update_reason=excluded.update_reason,"
                "updated_at=excluded.updated_at",
                (
                    workspace, resource, until_iso, int(bool(legal_hold)),
                    actor, why, timestamp,
                ),
            )
        else:
            connection.execute(
                "INSERT INTO retention_resource_policies "
                "(workspace_id,resource_type,resource_id,retain_until,legal_hold,"
                "updated_by,update_reason,updated_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(workspace_id,resource_type,resource_id) DO UPDATE SET "
                "retain_until=excluded.retain_until,legal_hold=excluded.legal_hold,"
                "updated_by=excluded.updated_by,update_reason=excluded.update_reason,"
                "updated_at=excluded.updated_at",
                (
                    workspace, kind, resource, until_iso, int(bool(legal_hold)),
                    actor, why, timestamp,
                ),
            )
        if kind == "attachment" and not legal_hold:
            connection.execute(
                "UPDATE attachment_deletion_tombstones SET status='pending',"
                "next_attempt_at=?,last_error_code=NULL WHERE workspace_id=? "
                "AND attachment_id=? AND status='held'",
                (timestamp, workspace, resource),
            )
        _audit(
            connection, workspace_id=workspace, resource_type=kind,
            resource_id=resource, action="retention_policy_updated",
            actor_id=actor, reason=why, occurred_at=timestamp,
            details={"legal_hold": bool(legal_hold), "retain_until": until_iso},
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return RetentionPolicy(
        workspace_id=workspace, resource_type=kind, resource_id=resource,
        retain_until=until_iso, legal_hold=bool(legal_hold), updated_at=timestamp,
    )


def held_resource_ids(
    database_path: str | Path, *, workspace_id: str, resource_type: str
) -> frozenset[str]:
    initialize_retention_store(database_path)
    workspace = _token(workspace_id, "workspace_id")
    kind = _token(resource_type, "resource_type", 64)
    if kind not in _RESOURCE_TYPES:
        raise RetentionError("invalid_resource_type", 400)
    with _opened(database_path) as connection:
        if kind == "source_artifact":
            rows = connection.execute(
                "SELECT artifact_id FROM source_artifact_retention_policies "
                "WHERE workspace_id=? AND legal_hold=1",
                (workspace,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT resource_id FROM retention_resource_policies "
                "WHERE workspace_id=? AND resource_type=? AND legal_hold=1",
                (workspace, kind),
            ).fetchall()
    return frozenset(str(row[0]) for row in rows)


def _receipt(row: sqlite3.Row) -> DeletionReceipt:
    return DeletionReceipt(
        id=str(row["id"]), workspace_id=str(row["workspace_id"]),
        attachment_id=int(row["attachment_id"]),
        attachment_type=str(row["attachment_type"]),
        source_sha256=str(row["source_sha256"]), status=str(row["status"]),
        requested_at=str(row["requested_at"]),
        source_deleted_at=row["source_deleted_at"],
        cache_purged_at=row["cache_purged_at"], attempts=int(row["attempts"]),
        last_error_code=row["last_error_code"],
    )


def request_attachment_deletion(
    database_path: str | Path,
    *,
    workspace_id: str,
    attachment_id: int,
    attachment_type: str,
    actor_id: str,
    reason: str,
    state_code: str | None = None,
    bridge_number: str | None = None,
    now: datetime | None = None,
) -> DeletionReceipt:
    """Delete a catalog row only after a same-transaction tombstone and audit."""

    initialize_retention_store(database_path)
    workspace = _token(workspace_id, "workspace_id")
    kind = _token(attachment_type, "attachment_type", 64)
    if kind not in _ATTACHMENT_TYPES:
        raise RetentionError("invalid_attachment_type", 400)
    if isinstance(attachment_id, bool) or int(attachment_id) < 1:
        raise RetentionError("invalid_attachment_id", 400)
    numeric_id = int(attachment_id)
    actor = _token(str(actor_id).lower(), "actor_id")
    why = _reason(reason)
    timestamp = _iso((now or _utc_now()).astimezone(timezone.utc))
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT id,state_code,bridge_number,attachment_type,filepath,sha256 "
            "FROM bridge_attachments WHERE workspace_id=? AND id=? AND attachment_type=?",
            (workspace, numeric_id, kind),
        ).fetchone()
        if row is None:
            existing = connection.execute(
                "SELECT * FROM attachment_deletion_tombstones "
                "WHERE workspace_id=? AND attachment_id=? AND attachment_type=?",
                (workspace, numeric_id, kind),
            ).fetchone()
            if existing is not None:
                if state_code is not None and str(existing["state_code"]) != str(state_code):
                    raise RetentionError("attachment_not_found", 404)
                if (
                    bridge_number is not None
                    and str(existing["bridge_number"]) != str(bridge_number)
                ):
                    raise RetentionError("attachment_not_found", 404)
                connection.commit()
                return _receipt(existing)
            raise RetentionError("attachment_not_found", 404)
        if state_code is not None and str(row["state_code"]) != str(state_code):
            raise RetentionError("attachment_not_found", 404)
        if bridge_number is not None and str(row["bridge_number"]) != str(bridge_number):
            raise RetentionError("attachment_not_found", 404)
        policy = connection.execute(
            "SELECT retain_until,legal_hold FROM retention_resource_policies "
            "WHERE workspace_id=? AND resource_type='attachment' AND resource_id=?",
            (workspace, str(numeric_id)),
        ).fetchone()
        if policy is not None and int(policy["legal_hold"]):
            raise RetentionError("attachment_legal_hold", 423)
        retain_until = _parse_time(policy["retain_until"], "retain_until") if policy else None
        instant = (now or _utc_now()).astimezone(timezone.utc)
        if retain_until is not None and retain_until > instant:
            raise RetentionError("attachment_retention_active", 409)
        source_sha256 = str(row["sha256"] or "").strip().lower()
        if len(source_sha256) != 64 or any(c not in "0123456789abcdef" for c in source_sha256):
            raise RetentionError("attachment_integrity_receipt_missing", 409)
        tombstone_id = str(uuid4())
        connection.execute(
            "INSERT INTO attachment_deletion_tombstones "
            "(id,workspace_id,attachment_id,attachment_type,state_code,bridge_number,"
            "source_sha256,source_portable_path,requested_by,reason,requested_at,"
            "catalog_deleted_at,status,next_attempt_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'pending',?)",
            (
                tombstone_id, workspace, numeric_id, kind, str(row["state_code"]),
                str(row["bridge_number"]), source_sha256, str(row["filepath"]),
                actor, why, timestamp, timestamp, timestamp,
            ),
        )
        _audit(
            connection, workspace_id=workspace, resource_type="attachment",
            resource_id=str(numeric_id), action="attachment_deletion_requested",
            actor_id=actor, reason=why, occurred_at=timestamp,
            details={"attachment_type": kind, "source_sha256": source_sha256},
        )
        deleted = connection.execute(
            "DELETE FROM bridge_attachments WHERE workspace_id=? AND id=? AND attachment_type=?",
            (workspace, numeric_id, kind),
        )
        if deleted.rowcount != 1:
            raise RetentionError("attachment_delete_race", 409)
        created = connection.execute(
            "SELECT * FROM attachment_deletion_tombstones WHERE id=?",
            (tombstone_id,),
        ).fetchone()
        connection.commit()
        assert created is not None
        return _receipt(created)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _safe_json(path: Path) -> Mapping[str, object] | None:
    try:
        size = path.stat().st_size
        if size <= 0 or size > _MAX_CACHE_JSON_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, Mapping) else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def purge_report_rag_derivatives(
    rag_working_dir: str | Path,
    *,
    source_sha256: str,
    state_code: str | None = None,
    bridge_number: str | None = None,
) -> int:
    """Remove v2 manifests/stages and extracted assets naming a source hash."""

    digest = str(source_sha256).strip().lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise RetentionError("invalid_source_sha256", 400)
    working_root = Path(rag_working_dir).resolve()
    cache_root = (working_root / "bridge_reports" / "v2").resolve()
    removed = 0
    stage_keys: set[tuple[str, str]] = set()
    identity_dirs: set[Path] = set()
    pointers = list(cache_root.glob("manifests/*/*/current.json"))
    immutable_manifests = list(cache_root.glob("manifests/*/*/manifest-*.json"))
    if len(pointers) + len(immutable_manifests) > _MAX_CACHE_MANIFESTS:
        raise RetentionError("report_cache_inventory_too_large", 503)
    manifest_paths: set[Path] = set(immutable_manifests)
    for pointer_path in pointers:
        try:
            pointer_path.resolve().relative_to(cache_root)
        except ValueError as exc:
            raise RetentionError("report_cache_path_escape", 500) from exc
        pointer = _safe_json(pointer_path)
        if pointer is None:
            continue
        name = pointer.get("manifest_path")
        if not isinstance(name, str) or Path(name).name != name:
            continue
        manifest_path = (pointer_path.parent / name).resolve()
        try:
            manifest_path.relative_to(cache_root)
        except ValueError:
            continue
        manifest_paths.add(manifest_path)
    for manifest_path in sorted(manifest_paths):
        try:
            manifest_path.resolve().relative_to(cache_root)
        except ValueError as exc:
            raise RetentionError("report_cache_path_escape", 500) from exc
        manifest = _safe_json(manifest_path)
        identity = manifest.get("identity") if manifest else None
        sources = identity.get("sources") if isinstance(identity, Mapping) else None
        if not isinstance(sources, list) or not any(
            isinstance(source, Mapping) and source.get("sha256") == digest
            for source in sources
        ):
            continue
        identity_dirs.add(manifest_path.parent.resolve())
        raw_keys = manifest.get("stage_keys") if manifest else None
        if isinstance(raw_keys, Mapping):
            for stage, key in raw_keys.items():
                if stage in {"parsed", "captions", "embeddings", "lexical_index"}:
                    text = str(key).lower()
                    if len(text) == 64 and all(c in "0123456789abcdef" for c in text):
                        stage_keys.add((str(stage), text))
    for directory in sorted(identity_dirs, key=lambda value: len(value.parts), reverse=True):
        directory.relative_to(cache_root)
        if directory.is_dir():
            shutil.rmtree(directory)
            removed += 1
    for stage, key in sorted(stage_keys):
        directory = (cache_root / "stages" / stage / key).resolve()
        directory.relative_to(cache_root)
        if directory.is_dir():
            shutil.rmtree(directory)
            removed += 1
    extracted = (working_root / "extracted_images" / digest[:16]).resolve()
    extracted.relative_to(working_root)
    if extracted.is_dir():
        shutil.rmtree(extracted)
        removed += 1
    if state_code is not None and bridge_number is not None:
        legacy = (working_root / "bridge_reports" / f"{state_code}_{bridge_number}.json").resolve()
        legacy.relative_to(working_root)
        if legacy.is_file():
            payload = _safe_json(legacy)
            if payload is None or digest in json.dumps(payload, ensure_ascii=True):
                legacy.unlink()
                removed += 1
    return removed


def process_attachment_deletion_outbox(
    database_path: str | Path,
    *,
    data_root: str | Path,
    rag_working_dir: str | Path,
    workspace_id: str | None = None,
    deletion_id: str | None = None,
    now: datetime | None = None,
    limit: int = 25,
    unlinker: Callable[[Path], None] | None = None,
    cache_purger: Callable[..., int] | None = None,
) -> tuple[DeletionReceipt, ...]:
    """Finalize committed deletions; failures remain retryable and path-private."""

    initialize_retention_store(database_path)
    instant = (now or _utc_now()).astimezone(timezone.utc)
    timestamp = _iso(instant)
    bounded = max(1, min(int(limit), 100))
    clauses = ["status IN ('pending','retry','held')", "next_attempt_at<=?"]
    params: list[object] = [timestamp]
    if workspace_id is not None:
        clauses.append("workspace_id=?")
        params.append(_token(workspace_id, "workspace_id"))
    if deletion_id is not None:
        clauses.append("id=?")
        params.append(_token(deletion_id, "deletion_id"))
    with _opened(database_path) as connection:
        rows = connection.execute(
            "SELECT * FROM attachment_deletion_tombstones WHERE "
            + " AND ".join(clauses)
            + " ORDER BY requested_at,id LIMIT ?",
            (*params, bounded),
        ).fetchall()
    remove_file = unlinker or (lambda path: path.unlink(missing_ok=True))
    purge_cache = cache_purger or purge_report_rag_derivatives
    receipts: list[DeletionReceipt] = []
    root = Path(data_root)
    for selected in rows:
        tombstone_id = str(selected["id"])
        workspace = str(selected["workspace_id"])
        attachment_id = int(selected["attachment_id"])
        with _opened(database_path) as connection:
            held = connection.execute(
                "SELECT legal_hold FROM retention_resource_policies WHERE workspace_id=? "
                "AND resource_type='attachment' AND resource_id=?",
                (workspace, str(attachment_id)),
            ).fetchone()
            if held is not None and int(held[0]):
                connection.execute(
                    "UPDATE attachment_deletion_tombstones SET status='held',"
                    "last_error_code='legal_hold' WHERE id=?",
                    (tombstone_id,),
                )
                current = connection.execute(
                    "SELECT * FROM attachment_deletion_tombstones WHERE id=?",
                    (tombstone_id,),
                ).fetchone()
                assert current is not None
                receipts.append(_receipt(current))
                continue
        try:
            if str(selected["attachment_type"]) == "inspection_report":
                purge_cache(
                    rag_working_dir,
                    source_sha256=str(selected["source_sha256"]),
                    state_code=str(selected["state_code"]),
                    bridge_number=str(selected["bridge_number"]),
                )
            try:
                source = resolve_confined_path(root, str(selected["source_portable_path"]))
            except StoragePathError as exc:
                raise RetentionError("attachment_path_unconfined", 500) from exc
            remove_file(source)
            completed_at = _iso(instant)
            with _opened(database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE attachment_deletion_tombstones SET status='complete',"
                    "attempts=attempts+1,source_deleted_at=?,cache_purged_at=?,"
                    "last_error_code=NULL,next_attempt_at=? WHERE id=?",
                    (completed_at, completed_at, completed_at, tombstone_id),
                )
                _audit(
                    connection, workspace_id=workspace, resource_type="attachment",
                    resource_id=str(attachment_id), action="attachment_deletion_completed",
                    actor_id="system@bridgeguard", reason=str(selected["reason"]),
                    occurred_at=completed_at,
                    details={
                        "attachment_type": str(selected["attachment_type"]),
                        "source_sha256": str(selected["source_sha256"]),
                    },
                )
                current = connection.execute(
                    "SELECT * FROM attachment_deletion_tombstones WHERE id=?",
                    (tombstone_id,),
                ).fetchone()
                connection.commit()
            assert current is not None
            receipts.append(_receipt(current))
        except Exception:
            attempts = int(selected["attempts"]) + 1
            retry_at = _iso(instant + timedelta(seconds=min(86_400, 2 ** min(attempts, 16))))
            with _opened(database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE attachment_deletion_tombstones SET status='retry',"
                    "attempts=?,next_attempt_at=?,last_error_code='filesystem_finalize_failed' "
                    "WHERE id=?",
                    (attempts, retry_at, tombstone_id),
                )
                _audit(
                    connection, workspace_id=workspace, resource_type="attachment",
                    resource_id=str(attachment_id),
                    action="attachment_deletion_finalize_failed",
                    actor_id="system@bridgeguard", reason=str(selected["reason"]),
                    occurred_at=timestamp,
                    details={
                        "attachment_type": str(selected["attachment_type"]),
                        "error_code": "filesystem_finalize_failed",
                    },
                )
                current = connection.execute(
                    "SELECT * FROM attachment_deletion_tombstones WHERE id=?",
                    (tombstone_id,),
                ).fetchone()
                connection.commit()
            assert current is not None
            receipts.append(_receipt(current))
    return tuple(receipts)


def sweep_completed_report_cache_tombstones(
    database_path: str | Path,
    *,
    rag_working_dir: str | Path,
    workspace_id: str,
    now: datetime | None = None,
    limit: int = 100,
) -> int:
    """Re-apply report cache tombstones to catch a finishing in-flight index.

    A completed source deletion remains a durable revocation.  Rechecking at a
    daily cadence prevents an index that began before catalog deletion from
    recreating derived content after the first purge.
    """

    initialize_retention_store(database_path)
    workspace = _token(workspace_id, "workspace_id")
    instant = (now or _utc_now()).astimezone(timezone.utc)
    timestamp = _iso(instant)
    bounded = max(1, min(int(limit), 500))
    with _opened(database_path) as connection:
        rows = connection.execute(
            "SELECT * FROM attachment_deletion_tombstones WHERE workspace_id=? "
            "AND attachment_type='inspection_report' AND status='complete' "
            "AND next_attempt_at<=? ORDER BY next_attempt_at,id LIMIT ?",
            (workspace, timestamp, bounded),
        ).fetchall()
    processed = 0
    for row in rows:
        tombstone_id = str(row["id"])
        try:
            purge_report_rag_derivatives(
                rag_working_dir,
                source_sha256=str(row["source_sha256"]),
                state_code=str(row["state_code"]),
                bridge_number=str(row["bridge_number"]),
            )
            error_code = None
            next_check = _iso(instant + timedelta(days=1))
            cache_purged_at = timestamp
        except Exception:
            error_code = "report_cache_gc_failed"
            next_check = _iso(instant + timedelta(minutes=10))
            cache_purged_at = row["cache_purged_at"]
        with _opened(database_path) as connection:
            connection.execute(
                "UPDATE attachment_deletion_tombstones SET cache_purged_at=?,"
                "next_attempt_at=?,last_error_code=? WHERE id=? AND workspace_id=? "
                "AND status='complete'",
                (cache_purged_at, next_check, error_code, tombstone_id, workspace),
            )
        processed += 1
    return processed


def _is_table(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _physical_artifact_id(value: object, *, strict: bool = False) -> str | None:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        if strict:
            raise RetentionError("artifact_reference_catalog_corrupt", 503) from exc
        return None


def _bounded_reference_rows(
    connection: sqlite3.Connection,
    sql: str,
    parameters: tuple[object, ...],
) -> list[sqlite3.Row]:
    rows = connection.execute(
        f"{sql} LIMIT ?", (*parameters, MAX_ARTIFACT_REFERENCE_ROWS + 1)
    ).fetchall()
    if len(rows) > MAX_ARTIFACT_REFERENCE_ROWS:
        raise RetentionError("artifact_reference_inventory_too_large", 503)
    return rows


def collect_source_artifact_references(
    database_path: str | Path,
    *,
    job_database_path: str | Path,
    source_store_root: str | Path,
    preflight_store_root: str | Path,
    workspace_id: str,
    now: datetime | None = None,
) -> tuple[frozenset[str], frozenset[str]]:
    """Resolve every durable source/preflight receipt before a GC decision.

    Any malformed job receipt or unavailable protected preflight fails closed;
    the caller performs no deletion.  Legacy inline source identifiers are not
    physical UUID manifests and are therefore ignored.
    """

    initialize_retention_store(database_path)
    workspace = _token(workspace_id, "workspace_id")
    timestamp = _iso((now or _utc_now()).astimezone(timezone.utc))
    source_ids: set[str] = set()
    preflight_ids: set[str] = set()
    policy_ids: set[str] = set()
    with _opened(database_path) as connection:
        if _is_table(connection, "source_artifacts"):
            rows = _bounded_reference_rows(
                connection,
                "SELECT id FROM source_artifacts WHERE workspace_id=?",
                (workspace,),
            )
            source_ids.update(
                physical
                for row in rows
                if (physical := _physical_artifact_id(row[0])) is not None
            )
        if _is_table(connection, "source_records"):
            rows = _bounded_reference_rows(
                connection,
                "SELECT DISTINCT source_artifact_id FROM source_records "
                "WHERE workspace_id=?",
                (workspace,),
            )
            source_ids.update(
                physical
                for row in rows
                if (physical := _physical_artifact_id(row[0])) is not None
            )
        rows = _bounded_reference_rows(
            connection,
            "SELECT artifact_id FROM source_artifact_retention_policies "
            "WHERE workspace_id=? AND (legal_hold=1 OR retain_until>?)",
            (workspace, timestamp),
        )
        policy_ids.update(
            _physical_artifact_id(row[0], strict=True) for row in rows
        )

    source_store = SourceArtifactStore(source_store_root)
    preflight_store = SourceArtifactStore(preflight_store_root)
    for artifact_id in policy_ids:
        found = False
        for store, destination in (
            (source_store, source_ids),
            (preflight_store, preflight_ids),
        ):
            try:
                store.get(artifact_id)
            except SourceArtifactError as exc:
                if exc.code != ArtifactErrorCode.ARTIFACT_NOT_FOUND.value:
                    raise RetentionError("artifact_reference_catalog_corrupt", 503) from exc
            else:
                destination.add(artifact_id)
                found = True
        # A policy may intentionally outlive already-erased bytes.  It remains
        # audited in SQLite but has no path to protect in this store inventory.
        if not found:
            continue

    job_path = Path(job_database_path)
    if job_path.is_file():
        try:
            job_connection = sqlite3.connect(
                f"file:{job_path.resolve().as_posix()}?mode=ro", uri=True, timeout=15
            )
            job_connection.row_factory = sqlite3.Row
            job_connection.execute("PRAGMA query_only=ON")
            if _is_table(job_connection, "operation_jobs"):
                rows = _bounded_reference_rows(
                    job_connection,
                    "SELECT payload_json,result_json FROM operation_jobs "
                    "WHERE workspace_id=? AND operation='import_preflight'",
                    (workspace,),
                )
            else:
                rows = []
        except sqlite3.Error as exc:
            raise RetentionError("artifact_reference_catalog_unavailable", 503) from exc
        finally:
            if "job_connection" in locals():
                job_connection.close()
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
                result = (
                    json.loads(str(row["result_json"]))
                    if row["result_json"] is not None
                    else None
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RetentionError("artifact_reference_catalog_corrupt", 503) from exc
            if not isinstance(payload, Mapping):
                raise RetentionError("artifact_reference_catalog_corrupt", 503)
            source = payload.get("source")
            if not isinstance(source, Mapping):
                raise RetentionError("artifact_reference_catalog_corrupt", 503)
            source_ids.add(
                _physical_artifact_id(source.get("artifact_id"), strict=True)
            )
            if result is not None:
                if not isinstance(result, Mapping):
                    raise RetentionError("artifact_reference_catalog_corrupt", 503)
                receipt = result.get("preflight_receipt")
                if receipt is not None:
                    if not isinstance(receipt, Mapping):
                        raise RetentionError("artifact_reference_catalog_corrupt", 503)
                    preflight_ids.add(
                        _physical_artifact_id(receipt.get("artifact_id"), strict=True)
                    )

    # Successful preflights can reference source receipts embedded in imported
    # conversion evidence.  Read only the job-referenced, bounded artifacts;
    # never infer reachability from untrusted filenames or filesystem paths.
    total_preflight_bytes = 0
    for artifact_id in sorted(preflight_ids):
        try:
            receipt = preflight_store.get(artifact_id)
            total_preflight_bytes += receipt.size_bytes
            if total_preflight_bytes > MAX_PREFLIGHT_REFERENCE_BYTES:
                raise RetentionError("artifact_reference_inventory_too_large", 503)
            payload = json.loads(preflight_store.read_bytes(artifact_id).decode("utf-8"))
        except RetentionError:
            raise
        except (SourceArtifactError, UnicodeError, json.JSONDecodeError) as exc:
            raise RetentionError("artifact_reference_catalog_corrupt", 503) from exc
        if not isinstance(payload, Mapping):
            raise RetentionError("artifact_reference_catalog_corrupt", 503)
        claimed = payload.get("artifact_receipts")
        if claimed is not None:
            if not isinstance(claimed, list):
                raise RetentionError("artifact_reference_catalog_corrupt", 503)
            for item in claimed:
                if not isinstance(item, Mapping):
                    raise RetentionError("artifact_reference_catalog_corrupt", 503)
                source_ids.add(
                    _physical_artifact_id(item.get("artifact_id"), strict=True)
                )
    return frozenset(source_ids), frozenset(preflight_ids)


def run_source_artifact_retention_sweep(
    database_path: str | Path,
    *,
    job_database_path: str | Path,
    source_store_root: str | Path,
    preflight_store_root: str | Path,
    workspace_id: str,
    now: datetime | None = None,
    retention_days: int = DEFAULT_SOURCE_ARTIFACT_RETENTION_DAYS,
    job_retention_days: int = DEFAULT_JOB_RETENTION_DAYS,
    max_deletions_per_store: int = 100,
) -> dict[str, object]:
    """Perform a bounded, reachability-first sweep of both import stores.

    This is intentionally separate from the minute-level report maintenance
    loop: a production store can contain hundreds of thousands of immutable
    receipts, so operators should schedule this lower-frequency inventory task.
    """

    if (
        isinstance(retention_days, bool)
        or not 1 <= int(retention_days) <= 3650
        or isinstance(job_retention_days, bool)
        or not 1 <= int(job_retention_days) <= 3650
    ):
        raise RetentionError("invalid_source_artifact_retention", 400)
    if int(retention_days) < int(job_retention_days):
        raise RetentionError("source_retention_shorter_than_job_retention", 400)
    instant = (now or _utc_now()).astimezone(timezone.utc)
    source_refs, preflight_refs = collect_source_artifact_references(
        database_path,
        job_database_path=job_database_path,
        source_store_root=source_store_root,
        preflight_store_root=preflight_store_root,
        workspace_id=workspace_id,
        now=instant,
    )
    cutoff = instant - timedelta(days=int(retention_days))
    source_report = SourceArtifactStore(source_store_root).sweep_unreferenced(
        protected_artifact_ids=source_refs,
        older_than=cutoff,
        now=instant,
        max_deletions=max_deletions_per_store,
    )
    preflight_report = SourceArtifactStore(preflight_store_root).sweep_unreferenced(
        protected_artifact_ids=preflight_refs,
        older_than=cutoff,
        now=instant,
        max_deletions=max_deletions_per_store,
    )
    return {
        "format": "bridgeguard-source-artifact-retention-sweep-v1",
        "workspace_id": workspace_id,
        "retention_days": int(retention_days),
        "job_retention_days": int(job_retention_days),
        "protected_source_artifacts": len(source_refs),
        "protected_preflight_artifacts": len(preflight_refs),
        "source_store": source_report.to_public_dict(),
        "preflight_store": preflight_report.to_public_dict(),
    }


def run_retention_sweep(
    database_path: str | Path,
    *,
    job_database_path: str | Path,
    workspace_id: str,
    data_root: str | Path,
    rag_working_dir: str | Path,
    now: datetime | None = None,
    job_retention_days: int = DEFAULT_JOB_RETENTION_DAYS,
) -> dict[str, int]:
    """Run bounded maintenance independently of incoming API traffic."""

    initialize_retention_store(database_path)
    instant = (now or _utc_now()).astimezone(timezone.utc)
    from ..ai.sessions import purge_expired_sessions
    from .jobs import purge_terminal_jobs

    expired_sessions = purge_expired_sessions(database_path, now=instant)
    held_jobs = held_resource_ids(
        database_path, workspace_id=workspace_id, resource_type="operation_job"
    )
    expired_jobs = purge_terminal_jobs(
        job_database_path,
        workspace_id=workspace_id,
        older_than=instant - timedelta(days=max(1, int(job_retention_days))),
        held_job_ids=held_jobs,
    )
    finalized = process_attachment_deletion_outbox(
        database_path, data_root=data_root, rag_working_dir=rag_working_dir,
        workspace_id=workspace_id, now=instant,
    )
    cache_gc = sweep_completed_report_cache_tombstones(
        database_path, rag_working_dir=rag_working_dir,
        workspace_id=workspace_id, now=instant,
    )
    return {
        "expired_sessions": expired_sessions,
        "expired_jobs": expired_jobs,
        "deletion_outbox_processed": len(finalized),
        "deletion_outbox_completed": sum(
            receipt.status in _TERMINAL_TOMBSTONE_STATUSES for receipt in finalized
        ),
        "report_cache_tombstones_applied": cache_gc,
    }


__all__ = [
    "DEFAULT_JOB_RETENTION_DAYS", "DEFAULT_SOURCE_ARTIFACT_RETENTION_DAYS",
    "DeletionReceipt", "RetentionError", "RetentionPolicy",
    "collect_source_artifact_references", "held_resource_ids",
    "initialize_retention_store",
    "process_attachment_deletion_outbox", "purge_report_rag_derivatives",
    "request_attachment_deletion", "run_retention_sweep",
    "run_source_artifact_retention_sweep", "set_retention_policy",
    "sweep_completed_report_cache_tombstones",
]
