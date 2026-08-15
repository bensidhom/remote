"""Read-only source / disposable-shadow migration rehearsal.

The release gate deliberately operates on a copy made with SQLite's online
backup API.  The source is opened ``mode=ro`` and is never migrated or
checkpointed.  The shadow uses the rollback journal so an otherwise useful
offline rehearsal cannot be used to waive BridgeGuard's WAL runtime gate.

The receipt contains schema, row-count, identity, and full-row content hashes
for the relational projection.  It does not certify FHWA submission validity;
it proves only that the checksummed BridgeGuard database migration preserved
the supported projection on the supplied source bytes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterable, Sequence

from app.routers.services.db_migrations import migrate_database
from app.routers.services.db_schema import (
    AUTHORITATIVE_SCHEMA,
    LATEST_SCHEMA_VERSION,
    collect_schema_drift,
)
from app.services.conversion.canonical_json import canonical_json_text


REHEARSAL_FORMAT_VERSION = "bridgeguard-migration-rehearsal-v1"
DEFAULT_TABLES = (
    "bridges",
    "features",
    "routes",
    "span_sets",
    "substructure_sets",
    "elements",
    "inspections",
    "posting_evaluations",
    "posting_statuses",
    "works",
)
_IDENTITY_COLUMNS = ("state_code", "bridge_number", "data_year")
_MAX_TABLES = 64
_HASH_FETCH_ROWS = 2_000


class MigrationRehearsalError(RuntimeError):
    """Typed, path-free failure from the offline release gate."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class TableRehearsalReceipt:
    table: str
    source_rows: int
    shadow_rows: int
    source_identity_sha256: str
    shadow_identity_sha256: str
    source_content_sha256: str
    shadow_content_sha256: str

    @property
    def preserved(self) -> bool:
        return (
            self.source_rows == self.shadow_rows
            and self.source_identity_sha256 == self.shadow_identity_sha256
            and self.source_content_sha256 == self.shadow_content_sha256
        )


@dataclass(frozen=True, slots=True)
class MigrationRehearsalReceipt:
    format_version: str
    source_database_sha256: str
    source_database_size: int
    source_user_version: int
    shadow_user_version: int
    target_schema_version: int
    source_quick_check: str
    shadow_quick_check: str
    source_foreign_key_violations: int
    shadow_foreign_key_violations: int
    migration_applied_versions: tuple[int, ...]
    table_receipts: tuple[TableRehearsalReceipt, ...]
    schema_drift: tuple[str, ...]
    copy_elapsed_ms: int
    migration_elapsed_ms: int
    verification_elapsed_ms: int
    completed_at: str
    receipt_sha256: str

    @property
    def preserved(self) -> bool:
        return (
            self.source_quick_check == "ok"
            and self.shadow_quick_check == "ok"
            and self.source_foreign_key_violations == 0
            and self.shadow_foreign_key_violations == 0
            and self.shadow_user_version == self.target_schema_version
            and not self.schema_drift
            and all(item.preserved for item in self.table_receipts)
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["preserved"] = self.preserved
        for index, item in enumerate(self.table_receipts):
            payload["table_receipts"][index]["preserved"] = item.preserved
        return payload

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1_000))


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
    except OSError as exc:
        raise MigrationRehearsalError(
            "source_unreadable", "The rehearsal source cannot be read."
        ) from exc
    return digest.hexdigest(), size


def _readonly(path: Path) -> sqlite3.Connection:
    wal = Path(str(path) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise MigrationRehearsalError(
            "source_wal_not_checkpointed",
            "The source has an uncheckpointed WAL and is not a stable rehearsal input.",
        )
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro", uri=True, timeout=10.0
        )
    except sqlite3.Error as exc:
        raise MigrationRehearsalError(
            "source_unreadable", "The rehearsal source cannot be opened read-only."
        ) from exc
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    return connection


def _quick_check(connection: sqlite3.Connection) -> str:
    rows = connection.execute("PRAGMA quick_check").fetchall()
    return "ok" if len(rows) == 1 and str(rows[0][0]) == "ok" else "failed"


def _table_names(connection: sqlite3.Connection) -> frozenset[str]:
    return frozenset(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    )


def _quoted(identifier: str) -> str:
    if not identifier or not identifier.replace("_", "").isalnum():
        raise MigrationRehearsalError(
            "unsafe_table_name", "The rehearsal table allowlist is invalid."
        )
    return f'"{identifier}"'


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({_quoted(table)})").fetchall()
    )


def _hash_rows(
    connection: sqlite3.Connection,
    table: str,
    *,
    columns: Sequence[str],
) -> tuple[int, str]:
    """Hash rows deterministically in bounded batches.

    Column names, affinities, and ordering are migration inputs, so the source
    column list is also used for the post-migration shadow.  Newly added
    nullable columns therefore cannot create a false mismatch.
    """

    if not columns:
        raise MigrationRehearsalError(
            "projection_schema_unsupported", "A projection table has no columns."
        )
    projection = ",".join(_quoted(column) for column in columns)
    ordering = ",".join(_quoted(column) for column in columns)
    cursor = connection.execute(
        f"SELECT {projection} FROM {_quoted(table)} ORDER BY {ordering}"
    )
    digest = hashlib.sha256()
    count = 0
    while True:
        rows = cursor.fetchmany(_HASH_FETCH_ROWS)
        if not rows:
            break
        for row in rows:
            encoded = canonical_json_text(list(row)).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            count += 1
    return count, digest.hexdigest()


def _identity_hash(
    connection: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
) -> str:
    identity = tuple(column for column in _IDENTITY_COLUMNS if column in columns)
    if not identity:
        return hashlib.sha256(b"").hexdigest()
    _count, digest = _hash_rows(connection, table, columns=identity)
    return digest


def _source_snapshot(
    connection: sqlite3.Connection, tables: Sequence[str]
) -> tuple[int, str, int, dict[str, tuple[tuple[str, ...], int, str, str]]]:
    quick = _quick_check(connection)
    violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    available = _table_names(connection)
    missing = tuple(table for table in tables if table not in available)
    if missing:
        raise MigrationRehearsalError(
            "projection_schema_unsupported",
            "The source is missing a required projection table.",
        )
    snapshots: dict[str, tuple[tuple[str, ...], int, str, str]] = {}
    for table in tables:
        columns = _columns(connection, table)
        count, content_hash = _hash_rows(connection, table, columns=columns)
        snapshots[table] = (
            columns,
            count,
            _identity_hash(connection, table, columns),
            content_hash,
        )
    return version, quick, violations, snapshots


def _copy_source(source: Path, shadow: Path) -> None:
    source_connection = _readonly(source)
    destination: sqlite3.Connection | None = None
    try:
        destination = sqlite3.connect(str(shadow), timeout=30.0)
        destination.execute("PRAGMA journal_mode=DELETE")
        destination.execute("PRAGMA synchronous=FULL")
        source_connection.backup(destination, pages=4096, sleep=0.01)
        destination.commit()
    except sqlite3.Error as exc:
        raise MigrationRehearsalError(
            "shadow_copy_failed", "SQLite could not create the rehearsal shadow."
        ) from exc
    finally:
        if destination is not None:
            destination.close()
        source_connection.close()


def run_migration_rehearsal(
    source_database: str | Path,
    shadow_database: str | Path,
    *,
    tables: Iterable[str] = DEFAULT_TABLES,
    app_revision: str = "migration-rehearsal",
) -> MigrationRehearsalReceipt:
    source = Path(source_database).expanduser().resolve()
    shadow = Path(shadow_database).expanduser().resolve()
    selected = tuple(dict.fromkeys(str(table).strip() for table in tables))
    if not selected or len(selected) > _MAX_TABLES:
        raise MigrationRehearsalError(
            "invalid_table_scope", "The rehearsal table scope is invalid."
        )
    for table in selected:
        _quoted(table)
    if source == shadow:
        raise MigrationRehearsalError(
            "path_collision", "Source and shadow must be different files."
        )
    if shadow.exists():
        raise MigrationRehearsalError(
            "shadow_exists", "The rehearsal shadow must not already exist."
        )
    if not source.is_file():
        raise MigrationRehearsalError(
            "source_not_found", "The rehearsal source does not exist."
        )
    shadow.parent.mkdir(parents=True, exist_ok=True)

    before_sha256, before_size = _sha256_file(source)
    source_connection = _readonly(source)
    try:
        (
            source_version,
            source_quick,
            source_violations,
            source_tables,
        ) = _source_snapshot(source_connection, selected)
    finally:
        source_connection.close()
    if source_quick != "ok" or source_violations:
        raise MigrationRehearsalError(
            "source_integrity_failed", "The source failed integrity verification."
        )

    copied_at = time.monotonic()
    try:
        _copy_source(source, shadow)
        copy_elapsed = _elapsed_ms(copied_at)

        migrated_at = time.monotonic()
        result = migrate_database(
            shadow, app_revision=app_revision, timeout_seconds=60.0
        )
        migration_elapsed = _elapsed_ms(migrated_at)

        verified_at = time.monotonic()
        shadow_connection = sqlite3.connect(str(shadow), timeout=30.0)
        try:
            shadow_connection.execute("PRAGMA foreign_keys=ON")
            shadow_connection.execute("PRAGMA trusted_schema=OFF")
            shadow_connection.execute("PRAGMA query_only=ON")
            shadow_version = int(
                shadow_connection.execute("PRAGMA user_version").fetchone()[0]
            )
            shadow_quick = _quick_check(shadow_connection)
            shadow_violations = len(
                shadow_connection.execute("PRAGMA foreign_key_check").fetchall()
            )
            drift = collect_schema_drift(shadow_connection, AUTHORITATIVE_SCHEMA)
            table_receipts: list[TableRehearsalReceipt] = []
            for table in selected:
                columns, source_rows, source_identity, source_content = source_tables[table]
                shadow_columns = _columns(shadow_connection, table)
                missing_columns = tuple(column for column in columns if column not in shadow_columns)
                if missing_columns:
                    raise MigrationRehearsalError(
                        "shadow_projection_changed",
                        "The migrated shadow lost a source projection column.",
                    )
                shadow_rows, shadow_content = _hash_rows(
                    shadow_connection, table, columns=columns
                )
                table_receipts.append(
                    TableRehearsalReceipt(
                        table=table,
                        source_rows=source_rows,
                        shadow_rows=shadow_rows,
                        source_identity_sha256=source_identity,
                        shadow_identity_sha256=_identity_hash(
                            shadow_connection, table, columns
                        ),
                        source_content_sha256=source_content,
                        shadow_content_sha256=shadow_content,
                    )
                )
        finally:
            shadow_connection.close()
        verification_elapsed = _elapsed_ms(verified_at)

        after_sha256, after_size = _sha256_file(source)
        if after_sha256 != before_sha256 or after_size != before_size:
            raise MigrationRehearsalError(
                "source_changed", "The source changed during the rehearsal."
            )

        payload = {
            "format_version": REHEARSAL_FORMAT_VERSION,
            "source_database_sha256": before_sha256,
            "source_database_size": before_size,
            "source_user_version": source_version,
            "shadow_user_version": shadow_version,
            "target_schema_version": LATEST_SCHEMA_VERSION,
            "source_quick_check": source_quick,
            "shadow_quick_check": shadow_quick,
            "source_foreign_key_violations": source_violations,
            "shadow_foreign_key_violations": shadow_violations,
            "migration_applied_versions": list(result.applied_versions),
            "table_receipts": [asdict(item) for item in table_receipts],
            "schema_drift": list(drift),
            "copy_elapsed_ms": copy_elapsed,
            "migration_elapsed_ms": migration_elapsed,
            "verification_elapsed_ms": verification_elapsed,
            "completed_at": _now(),
        }
        receipt_hash = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        receipt = MigrationRehearsalReceipt(
            format_version=REHEARSAL_FORMAT_VERSION,
            source_database_sha256=before_sha256,
            source_database_size=before_size,
            source_user_version=source_version,
            shadow_user_version=shadow_version,
            target_schema_version=LATEST_SCHEMA_VERSION,
            source_quick_check=source_quick,
            shadow_quick_check=shadow_quick,
            source_foreign_key_violations=source_violations,
            shadow_foreign_key_violations=shadow_violations,
            migration_applied_versions=tuple(result.applied_versions),
            table_receipts=tuple(table_receipts),
            schema_drift=tuple(drift),
            copy_elapsed_ms=copy_elapsed,
            migration_elapsed_ms=migration_elapsed,
            verification_elapsed_ms=verification_elapsed,
            completed_at=str(payload["completed_at"]),
            receipt_sha256=receipt_hash,
        )
        if not receipt.preserved:
            raise MigrationRehearsalError(
                "migration_not_preserving",
                "The migrated shadow did not preserve the source projection.",
            )
        return receipt
    except Exception:
        # The shadow is a disposable gate artifact.  Remove it on failure so a
        # rerun can never mistake a partially migrated file for a new target.
        try:
            if shadow.exists():
                shadow.unlink()
        except OSError:
            pass
        raise


__all__ = [
    "DEFAULT_TABLES",
    "MigrationRehearsalError",
    "MigrationRehearsalReceipt",
    "REHEARSAL_FORMAT_VERSION",
    "TableRehearsalReceipt",
    "run_migration_rehearsal",
]
