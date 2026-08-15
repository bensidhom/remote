"""Two-phase, content-bound import of a legacy relational SNBI projection.

The source database is inventory-only evidence.  Dry-run opens it with SQLite
``mode=ro`` and never initializes or migrates it.  Apply writes either a fresh
shadow database (the default) or, with an explicit opt-in, the source database
itself.  A manifest digest binds apply to the exact bytes and reconstructed
canonical snapshots reviewed during dry-run.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.routers.services.constants import REVERSE_MAPPING
from app.routers.services.database import _decode_projection_value, ensure_schema, get_connection
from app.routers.services.db_schema import LATEST_SCHEMA_VERSION
from app.routers.services.snbi_schema import (
    ALL_NESTED_ARRAY_NAMES,
    NESTED_ARRAY_SPECS,
    TOP_LEVEL_FIELDS,
)
from app.services.conversion.canonical_json import canonical_json_text, canonical_sha256
from app.services.conversion.canonical_schema import (
    CanonicalResourceLimitError,
    normalize_canonical_document,
)
from app.services.conversion.revision_store import _refresh_projection


_FORMAT_VERSION = "bridgeguard-legacy-backfill-v1"
_PROJECTION_TABLES = (
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
_SECTION_TABLES = {
    "Elements": "elements",
    "Features": "features",
    "Inspections": "inspections",
    "PostingEvaluations": "posting_evaluations",
    "PostingStatuses": "posting_statuses",
    "SpanSets": "span_sets",
    "SubstructureSets": "substructure_sets",
    "Works": "works",
}


class LegacyBackfillError(Exception):
    """Typed, non-path-leaking backfill failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LegacyBackfillRequest:
    source_db_path: Path
    destination_db_path: Path
    workspace_id: str
    actor_id: str
    profile_id: str
    request_id: str
    dry_run: bool = True
    publish: bool = False
    in_place: bool = False
    expected_manifest_sha256: str | None = None


@dataclass(frozen=True)
class BackfillSnapshotPlan:
    state_code: int
    bridge_number: str
    data_year: int
    canonical_sha256: str
    canonical_size: int


@dataclass(frozen=True)
class BackfillManifest:
    format_version: str
    source_database_sha256: str
    source_database_size: int
    source_schema_sha256: str
    source_user_version: int
    quick_check: str
    foreign_key_violations: tuple[tuple[Any, ...], ...]
    table_counts: tuple[tuple[str, int], ...]
    snapshots: tuple[BackfillSnapshotPlan, ...]
    manifest_sha256: str


@dataclass(frozen=True)
class LegacyBackfillResult:
    manifest: BackfillManifest
    dry_run: bool
    applied: bool
    no_op: bool
    staged_revision_ids: tuple[str, ...]
    published_revision_ids: tuple[str, ...]


@dataclass(frozen=True)
class _Inventory:
    manifest: BackfillManifest
    documents: tuple[dict[str, Any], ...]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _stable_id(kind: str, *parts: Any) -> str:
    material = ":".join(str(part) for part in parts)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"bridgeguard:{kind}:{material}"))


def _required_scope(request: LegacyBackfillRequest) -> None:
    for value in (request.workspace_id, request.actor_id, request.profile_id):
        if not isinstance(value, str) or not value.strip():
            raise LegacyBackfillError(
                "explicit_scope_required",
                "Workspace, actor, and validation profile must be explicit",
            )
    if not isinstance(request.request_id, str) or not request.request_id.strip():
        raise LegacyBackfillError(
            "explicit_scope_required", "A nonblank request ID is required"
        )


def _resolved_paths(request: LegacyBackfillRequest) -> tuple[Path, Path, bool]:
    source = Path(request.source_db_path).expanduser().resolve()
    destination = Path(request.destination_db_path).expanduser().resolve()
    same_file = source == destination
    if same_file and (request.dry_run or not request.in_place):
        raise LegacyBackfillError(
            "path_collision",
            "Source and destination must differ unless apply explicitly opts into in-place migration",
        )
    return source, destination, same_file


def _read_database_bytes(path: Path) -> bytes:
    """Return source bytes for compatibility with small fixture callers.

    Production-sized inventory never uses this helper: :func:`_sha256_file`
    streams the source instead of materializing a multi-gigabyte database in
    memory.  Keeping this narrow helper avoids changing the public contract for
    older tests that monkeypatch it while removing it from the real inventory
    path.
    """

    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise LegacyBackfillError("source_not_found", "Source database does not exist") from exc
    except OSError as exc:
        raise LegacyBackfillError("source_unreadable", "Source database cannot be read") from exc


def _sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    """Stream one immutable-source digest without exposing its path publicly."""

    if (
        isinstance(chunk_size, bool)
        or not isinstance(chunk_size, int)
        or not 64 * 1024 <= chunk_size <= 8 * 1024 * 1024
    ):
        raise ValueError("chunk_size must be between 64 KiB and 8 MiB")
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as source:
            while True:
                block = source.read(chunk_size)
                if not block:
                    break
                digest.update(block)
                size += len(block)
    except FileNotFoundError as exc:
        raise LegacyBackfillError("source_not_found", "Source database does not exist") from exc
    except OSError as exc:
        raise LegacyBackfillError("source_unreadable", "Source database cannot be read") from exc
    return digest.hexdigest(), size


def _readonly_connection(path: Path) -> sqlite3.Connection:
    # A nonempty WAL would make the main-file digest incomplete evidence.  Do
    # not checkpoint or create shared-memory files on an inventory operation.
    wal_path = Path(str(path) + "-wal")
    if wal_path.exists() and wal_path.stat().st_size:
        raise LegacyBackfillError(
            "source_integrity_error",
            "Source has an uncheckpointed WAL and cannot be content-bound safely",
        )
    uri = f"{path.as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection
    except sqlite3.Error as exc:
        raise LegacyBackfillError(
            "source_integrity_error", "Source database cannot be opened read-only"
        ) from exc


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _mapped_row(row: sqlite3.Row, allowed: set[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for column in row.keys():
        if column not in REVERSE_MAPPING:
            continue
        tag = REVERSE_MAPPING[column]
        if tag not in allowed:
            continue
        value = _decode_projection_value(row[column])
        if value is None or value == "":
            continue
        result[tag] = value
    return result


def _fetch_children(
    connection: sqlite3.Connection,
    table: str,
    key: tuple[int, str, int],
) -> list[sqlite3.Row]:
    columns = _table_columns(connection, table)
    order = "id" if "id" in columns else "rowid"
    return connection.execute(
        f'SELECT * FROM "{table}" WHERE state_code=? AND bridge_number=? '
        f'AND data_year=? ORDER BY "{order}"',
        key,
    ).fetchall()


def _reconstruct_document(
    connection: sqlite3.Connection, bridge: sqlite3.Row
) -> dict[str, Any]:
    state_code = bridge["state_code"]
    bridge_number = bridge["bridge_number"]
    data_year = bridge["data_year"]
    if data_year is None:
        raise LegacyBackfillError(
            "data_year_required",
            "Every legacy bridge snapshot must have an explicit stored data_year",
        )
    try:
        key = (int(state_code), str(bridge_number), int(data_year))
    except (TypeError, ValueError) as exc:
        raise LegacyBackfillError(
            "source_integrity_error", "A legacy bridge has an invalid logical key"
        ) from exc

    document = _mapped_row(bridge, set(TOP_LEVEL_FIELDS))
    # The identities are authoritative relational key columns, not inferred
    # from inspection dates or wall-clock time.
    document["BL01"] = key[0]
    document["BID01"] = key[1]

    for section in ALL_NESTED_ARRAY_NAMES:
        table = _SECTION_TABLES[section]
        allowed = set(NESTED_ARRAY_SPECS[section]["fields"]) - {"Routes"}
        rows = _fetch_children(connection, table, key)
        document[section] = [_mapped_row(row, allowed) for row in rows]

    route_rows = _fetch_children(connection, "routes", key)
    routes_by_feature: dict[str, list[dict[str, Any]]] = {}
    route_allowed = set(NESTED_ARRAY_SPECS["Routes"]["fields"])
    for row in route_rows:
        feature_type = _decode_projection_value(row["feature_type"])
        if feature_type is None or feature_type == "":
            raise LegacyBackfillError(
                "source_integrity_error", "A route has no owning feature identity"
            )
        routes_by_feature.setdefault(str(feature_type), []).append(
            _mapped_row(row, route_allowed)
        )
    for feature in document["Features"]:
        feature_type = str(feature.get("BF01", ""))
        feature["Routes"] = routes_by_feature.pop(feature_type, [])
    if any(routes_by_feature.values()):
        raise LegacyBackfillError(
            "source_integrity_error", "A route references a missing feature"
        )

    normalization = normalize_canonical_document(
        document, profile_id="research_historical_import"
    )
    if normalization.has_errors:
        raise LegacyBackfillError(
            "source_integrity_error",
            "A reconstructed snapshot is not a lossless canonical SNBI document",
        )
    normalized = normalization.normalized_document
    for section in ALL_NESTED_ARRAY_NAMES:
        normalized.setdefault(section, [])
    return normalized


def _manifest_payload(
    *,
    database_sha256: str,
    database_size: int,
    schema_sha256: str,
    user_version: int,
    quick_check: str,
    foreign_key_violations: Sequence[Sequence[Any]],
    table_counts: Sequence[tuple[str, int]],
    snapshots: Sequence[BackfillSnapshotPlan],
) -> dict[str, Any]:
    return {
        "format_version": _FORMAT_VERSION,
        "source_database_sha256": database_sha256,
        "source_database_size": database_size,
        "source_schema_sha256": schema_sha256,
        "source_user_version": user_version,
        "quick_check": quick_check,
        "foreign_key_violations": [list(row) for row in foreign_key_violations],
        "table_counts": [list(row) for row in table_counts],
        "snapshots": [asdict(snapshot) for snapshot in snapshots],
    }


def _inventory(path: Path, profile_id: str) -> _Inventory:
    database_sha256, database_size = _sha256_file(path)
    connection = _readonly_connection(path)
    try:
        quick_rows = connection.execute("PRAGMA quick_check").fetchall()
        quick_check = str(quick_rows[0][0]) if len(quick_rows) == 1 else "failed"
        if quick_check != "ok":
            raise LegacyBackfillError(
                "source_integrity_error", "SQLite quick_check did not pass"
            )
        violations = tuple(
            tuple(row) for row in connection.execute("PRAGMA foreign_key_check").fetchall()
        )
        if violations:
            raise LegacyBackfillError(
                "source_integrity_error", "Source projection violates relational integrity"
            )

        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        schema_rows = connection.execute(
            "SELECT type, name, tbl_name, COALESCE(sql, '') FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name COLLATE BINARY"
        ).fetchall()
        schema_sha256 = canonical_sha256([list(row) for row in schema_rows])
        table_names = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name COLLATE BINARY"
            ).fetchall()
        )
        missing = [table for table in _PROJECTION_TABLES if table not in table_names]
        if missing:
            raise LegacyBackfillError(
                "source_integrity_error", "Source is missing required projection tables"
            )
        for table in _PROJECTION_TABLES:
            required = {"state_code", "bridge_number", "data_year"}
            if not required.issubset(_table_columns(connection, table)):
                raise LegacyBackfillError(
                    "source_integrity_error", "Source projection schema is unsupported"
                )

        table_counts = tuple(
            (table, int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]))
            for table in table_names
        )
        bridges = connection.execute(
            "SELECT * FROM bridges ORDER BY state_code, bridge_number COLLATE BINARY, data_year"
        ).fetchall()
        documents: list[dict[str, Any]] = []
        plans: list[BackfillSnapshotPlan] = []
        for bridge in bridges:
            # Use the requested profile for normalization diagnostics.  The
            # manifest remains deterministic because profile does not alter
            # accepted canonical values.
            document = _reconstruct_document(connection, bridge)
            if profile_id != "research_historical_import":
                normalization = normalize_canonical_document(
                    document, profile_id=profile_id
                )
                if normalization.has_errors:
                    raise LegacyBackfillError(
                        "source_integrity_error", "Profile normalization failed"
                    )
                document = normalization.normalized_document
                for section in ALL_NESTED_ARRAY_NAMES:
                    document.setdefault(section, [])
            canonical_text = canonical_json_text(document)
            plans.append(
                BackfillSnapshotPlan(
                    state_code=int(bridge["state_code"]),
                    bridge_number=str(bridge["bridge_number"]),
                    data_year=int(bridge["data_year"]),
                    canonical_sha256=canonical_sha256(document),
                    canonical_size=len(canonical_text.encode("utf-8")),
                )
            )
            documents.append(document)
    except LegacyBackfillError:
        raise
    except (sqlite3.Error, CanonicalResourceLimitError, ValueError, TypeError) as exc:
        raise LegacyBackfillError(
            "source_integrity_error", "Source projection could not be inventoried safely"
        ) from exc
    finally:
        connection.close()

    after_sha256, after_size = _sha256_file(path)
    if after_sha256 != database_sha256 or after_size != database_size:
        raise LegacyBackfillError(
            "source_changed", "Source database changed during inventory"
        )
    payload = _manifest_payload(
        database_sha256=database_sha256,
        database_size=database_size,
        schema_sha256=schema_sha256,
        user_version=user_version,
        quick_check=quick_check,
        foreign_key_violations=violations,
        table_counts=table_counts,
        snapshots=plans,
    )
    manifest = BackfillManifest(
        format_version=_FORMAT_VERSION,
        source_database_sha256=database_sha256,
        source_database_size=database_size,
        source_schema_sha256=schema_sha256,
        source_user_version=user_version,
        quick_check=quick_check,
        foreign_key_violations=violations,
        table_counts=table_counts,
        snapshots=tuple(plans),
        manifest_sha256=canonical_sha256(payload),
    )
    return _Inventory(manifest=manifest, documents=tuple(documents))


def _insert_workspace(
    connection: sqlite3.Connection, request: LegacyBackfillRequest, now: str
) -> None:
    row = connection.execute(
        "SELECT id, status FROM workspaces WHERE id=?", (request.workspace_id,)
    ).fetchone()
    if row is None:
        connection.execute(
            "INSERT INTO workspaces "
            "(id, slug, display_name, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'active', ?, ?)",
            (
                request.workspace_id,
                request.workspace_id,
                "Legacy projection research backfill",
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO workspace_state "
            "(workspace_id, data_generation, schema_generation, updated_at) "
            "VALUES (?, 0, ?, ?)",
            (request.workspace_id, LATEST_SCHEMA_VERSION, now),
        )
    elif str(row["status"]) != "active":
        raise LegacyBackfillError("destination_schema_error", "Workspace is not active")


def _audit(
    connection: sqlite3.Connection,
    *,
    audit_id: str,
    workspace_id: str,
    actor_id: str,
    action: str,
    entity_id: str,
    request_id: str,
    after_hash: str,
    metadata: Mapping[str, Any],
    now: str,
) -> None:
    connection.execute(
        "INSERT INTO mutation_audit "
        "(id, workspace_id, actor_id, action, entity_type, entity_id, request_id, "
        "reason, before_sha256, after_sha256, metadata_json, occurred_at) "
        "VALUES (?, ?, ?, ?, 'snapshot_revision', ?, ?, ?, NULL, ?, ?, ?)",
        (
            audit_id,
            workspace_id,
            actor_id,
            action,
            entity_id,
            request_id,
            "Legacy relational projection reconstruction",
            after_hash,
            canonical_json_text(metadata),
            now,
        ),
    )


def _existing_result(
    connection: sqlite3.Connection,
    request: LegacyBackfillRequest,
    inventory: _Inventory,
    batch_id: str,
    revision_ids: tuple[str, ...],
) -> LegacyBackfillResult | None:
    batch = connection.execute(
        "SELECT request_sha256 FROM conversion_batches WHERE workspace_id=? AND id=?",
        (request.workspace_id, batch_id),
    ).fetchone()
    if batch is None:
        return None
    operation_hash = _operation_hash(request, inventory.manifest)
    if str(batch["request_sha256"]) != operation_hash:
        raise LegacyBackfillError(
            "destination_schema_error", "Backfill operation identity conflicts with existing data"
        )
    for revision_id in revision_ids:
        if connection.execute(
            "SELECT 1 FROM snapshot_revisions WHERE workspace_id=? AND id=?",
            (request.workspace_id, revision_id),
        ).fetchone() is None:
            raise LegacyBackfillError(
                "destination_schema_error", "Existing backfill is incomplete"
            )
    published_ids: tuple[str, ...] = ()
    if request.publish:
        rows = connection.execute(
            "SELECT snapshot_revision_id FROM published_snapshots WHERE workspace_id=? "
            "AND snapshot_revision_id IN ("
            + ",".join("?" for _ in revision_ids)
            + ") ORDER BY state_code, bridge_number COLLATE BINARY, data_year",
            (request.workspace_id, *revision_ids),
        ).fetchall()
        published_ids = tuple(str(row[0]) for row in rows)
        if set(published_ids) != set(revision_ids):
            raise LegacyBackfillError(
                "destination_schema_error", "Existing published backfill is incomplete"
            )
    return LegacyBackfillResult(
        manifest=inventory.manifest,
        dry_run=False,
        applied=True,
        no_op=True,
        staged_revision_ids=revision_ids,
        published_revision_ids=published_ids,
    )


def _operation_hash(
    request: LegacyBackfillRequest, manifest: BackfillManifest
) -> str:
    return canonical_sha256(
        {
            "format_version": _FORMAT_VERSION,
            "manifest_sha256": manifest.manifest_sha256,
            "workspace_id": request.workspace_id,
            "actor_id": request.actor_id,
            "profile_id": request.profile_id,
            "request_id": request.request_id,
            "publish": request.publish,
        }
    )


def _apply(
    destination: Path,
    request: LegacyBackfillRequest,
    inventory: _Inventory,
) -> LegacyBackfillResult:
    # Avoid re-running the strict migration assertion on an already-current
    # destination.  Besides being unnecessary, a deliberate test/operations
    # trigger is valid local state and must be exercised by the transaction,
    # not rejected by startup while its connection is still unwinding.
    initialize = not destination.exists()
    if not initialize:
        probe = sqlite3.connect(str(destination), timeout=10.0)
        try:
            version = int(probe.execute("PRAGMA user_version").fetchone()[0])
            if version < LATEST_SCHEMA_VERSION:
                initialize = True
            else:
                required_tables = {
                    "workspaces",
                    "workspace_state",
                    "source_artifacts",
                    "source_records",
                    "conversion_batches",
                    "eligibility_decisions",
                    "snapshot_revisions",
                    "snapshot_revision_sources",
                    "published_snapshots",
                    "validation_runs",
                    "validation_issues",
                    "mutation_audit",
                    *_PROJECTION_TABLES,
                }
                actual_tables = {
                    str(row[0])
                    for row in probe.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if not required_tables.issubset(actual_tables):
                    raise LegacyBackfillError(
                        "destination_schema_error",
                        "Destination schema is incomplete",
                    )
        finally:
            probe.close()
    if initialize:
        try:
            ensure_schema(str(destination))
        except Exception as exc:
            raise LegacyBackfillError(
                "destination_schema_error", "Destination schema could not be initialized"
            ) from exc

    manifest = inventory.manifest
    batch_id = _stable_id(
        "legacy-backfill-batch",
        request.workspace_id,
        manifest.manifest_sha256,
        request.publish,
    )
    artifact_id = _stable_id(
        "legacy-backfill-artifact", request.workspace_id, manifest.source_database_sha256
    )
    revision_ids = tuple(
        _stable_id(
            "legacy-backfill-revision",
            request.workspace_id,
            plan.state_code,
            plan.bridge_number,
            plan.data_year,
            plan.canonical_sha256,
        )
        for plan in manifest.snapshots
    )
    operation_hash = _operation_hash(request, manifest)
    connection = get_connection(str(destination))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA synchronous=FULL")
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = _existing_result(
            connection, request, inventory, batch_id, revision_ids
        )
        if existing is not None:
            connection.rollback()
            return existing

        now = _now()
        _insert_workspace(connection, request, now)
        connection.execute(
            "INSERT INTO source_artifacts "
            "(id, workspace_id, sha256, byte_size, original_filename, media_type, "
            "artifact_kind, storage_uri, source_system, created_by, created_at, retention_until) "
            "VALUES (?, ?, ?, ?, NULL, 'application/vnd.sqlite3', "
            "'legacy_projection_database', NULL, 'bridgeguard_legacy_projection', ?, ?, NULL)",
            (
                artifact_id,
                request.workspace_id,
                manifest.source_database_sha256,
                manifest.source_database_size,
                request.actor_id,
                now,
            ),
        )
        counts = {
            "snapshots": len(inventory.documents),
            "published": len(inventory.documents) if request.publish else 0,
        }
        connection.execute(
            "INSERT INTO conversion_batches "
            "(id, workspace_id, profile_id, rule_bundle_id, rule_bundle_sha256, "
            "options_json, request_sha256, idempotency_key, actor_id, status, counts_json, "
            "requested_at, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                batch_id,
                request.workspace_id,
                request.profile_id,
                _FORMAT_VERSION,
                manifest.manifest_sha256,
                canonical_json_text({"publish": request.publish, "in_place": request.in_place}),
                operation_hash,
                manifest.manifest_sha256 + (":publish" if request.publish else ":stage"),
                request.actor_id,
                "published" if request.publish else "staged",
                canonical_json_text(counts),
                now,
                now,
                now,
            ),
        )

        for ordinal, (plan, document, revision_id) in enumerate(
            zip(manifest.snapshots, inventory.documents, revision_ids, strict=True)
        ):
            record_id = _stable_id("legacy-backfill-record", artifact_id, ordinal, plan.canonical_sha256)
            raw_json = canonical_json_text(document)
            connection.execute(
                "INSERT INTO source_records "
                "(id, workspace_id, source_artifact_id, record_ordinal, source_role, "
                "record_identity, raw_record_json, raw_record_sha256, parse_status, "
                "parse_issues_json, state_code, bridge_number, data_year, created_at) "
                "VALUES (?, ?, ?, ?, 'legacy_projection_reconstruction', ?, ?, ?, "
                "'reconstructed_research', ?, ?, ?, ?, ?)",
                (
                    record_id,
                    request.workspace_id,
                    artifact_id,
                    ordinal,
                    f"{plan.state_code}:{plan.bridge_number}:{plan.data_year}",
                    raw_json,
                    plan.canonical_sha256,
                    canonical_json_text(
                        {
                            "warning": "Relational projection is reconstructed research evidence, not original upload bytes"
                        }
                    ),
                    plan.state_code,
                    plan.bridge_number,
                    plan.data_year,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO eligibility_decisions "
                "(id, workspace_id, conversion_batch_id, source_record_id, decision, "
                "reason_code, reason_detail, evidence_json, created_at) "
                "VALUES (?, ?, ?, ?, 'accepted_research_only', "
                "'legacy_projection_reconstruction', ?, ?, ?)",
                (
                    _stable_id("legacy-backfill-eligibility", batch_id, record_id),
                    request.workspace_id,
                    batch_id,
                    record_id,
                    "Accepted only for research migration; original source fidelity is unknown",
                    canonical_json_text({"manifest_sha256": manifest.manifest_sha256}),
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO snapshot_revisions "
                "(id, workspace_id, conversion_batch_id, source_record_id, state_code, "
                "bridge_number, data_year, revision_number, lifecycle_state, canonical_json, "
                "canonical_sha256, parent_revision_id, profile_id, rule_bundle_sha256, "
                "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'staged', ?, ?, "
                "NULL, ?, ?, ?, ?)",
                (
                    revision_id,
                    request.workspace_id,
                    batch_id,
                    record_id,
                    plan.state_code,
                    plan.bridge_number,
                    plan.data_year,
                    raw_json,
                    plan.canonical_sha256,
                    request.profile_id,
                    manifest.manifest_sha256,
                    request.actor_id,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO snapshot_revision_sources "
                "(id, workspace_id, snapshot_revision_id, source_record_id, source_ordinal, source_role) "
                "VALUES (?, ?, ?, ?, 0, 'legacy_projection_reconstruction')",
                (
                    _stable_id("legacy-backfill-revision-source", revision_id, record_id),
                    request.workspace_id,
                    revision_id,
                    record_id,
                ),
            )
            validation_id = _stable_id("legacy-backfill-validation", revision_id)
            connection.execute(
                "INSERT INTO validation_runs "
                "(id, workspace_id, snapshot_revision_id, profile_id, rule_bundle_id, "
                "rule_bundle_sha256, validator_version, status, canonical_sha256, "
                "summary_json, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, "
                "'passed_with_warnings', ?, ?, ?, ?)",
                (
                    validation_id,
                    request.workspace_id,
                    revision_id,
                    request.profile_id,
                    _FORMAT_VERSION,
                    manifest.manifest_sha256,
                    _FORMAT_VERSION,
                    plan.canonical_sha256,
                    canonical_json_text(
                        {
                            "research_only": True,
                            "field_lineage_available": False,
                            "original_source_bytes_available": False,
                        }
                    ),
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO validation_issues "
                "(id, workspace_id, validation_run_id, issue_ordinal, severity, code, "
                "json_pointer, message, evidence_json, created_at) "
                "VALUES (?, ?, ?, 0, 'warning', 'LEGACY_PROJECTION_RECONSTRUCTION', '', ?, ?, ?)",
                (
                    _stable_id("legacy-backfill-validation-issue", validation_id),
                    request.workspace_id,
                    validation_id,
                    "Snapshot was reconstructed from BridgeGuard relational projection; original bytes and field lineage are unavailable",
                    canonical_json_text({"manifest_sha256": manifest.manifest_sha256}),
                    now,
                ),
            )
            _audit(
                connection,
                audit_id=_stable_id("legacy-backfill-stage-audit", revision_id),
                workspace_id=request.workspace_id,
                actor_id=request.actor_id,
                action="stage_revision",
                entity_id=revision_id,
                request_id=request.request_id,
                after_hash=plan.canonical_sha256,
                metadata={
                    "kind": "legacy_projection_reconstruction",
                    "manifest_sha256": manifest.manifest_sha256,
                },
                now=now,
            )

        published_ids: tuple[str, ...] = ()
        if request.publish:
            for plan, document, revision_id in zip(
                manifest.snapshots, inventory.documents, revision_ids, strict=True
            ):
                _refresh_projection(connection, document, plan.data_year)
                connection.execute(
                    "INSERT INTO published_snapshots "
                    "(id, workspace_id, state_code, bridge_number, data_year, "
                    "snapshot_revision_id, published_by, published_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _stable_id("legacy-backfill-published", revision_id),
                        request.workspace_id,
                        plan.state_code,
                        plan.bridge_number,
                        plan.data_year,
                        revision_id,
                        request.actor_id,
                        now,
                    ),
                )
                _audit(
                    connection,
                    audit_id=_stable_id("legacy-backfill-publish-audit", revision_id),
                    workspace_id=request.workspace_id,
                    actor_id=request.actor_id,
                    action="publish_revision",
                    entity_id=revision_id,
                    request_id=request.request_id,
                    after_hash=plan.canonical_sha256,
                    metadata={
                        "kind": "atomic_legacy_backfill_publish",
                        "manifest_sha256": manifest.manifest_sha256,
                    },
                    now=now,
                )
            connection.execute(
                "UPDATE workspace_state SET data_generation=data_generation+1, "
                "schema_generation=?, updated_at=? WHERE workspace_id=?",
                (LATEST_SCHEMA_VERSION, now, request.workspace_id),
            )
            published_ids = revision_ids

        connection.commit()
        return LegacyBackfillResult(
            manifest=manifest,
            dry_run=False,
            applied=True,
            no_op=False,
            staged_revision_ids=revision_ids,
            published_revision_ids=published_ids,
        )
    except LegacyBackfillError:
        connection.rollback()
        raise
    except Exception:
        connection.rollback()
        raise LegacyBackfillError(
            "apply_failed", "Backfill failed; the complete destination transaction was rolled back"
        ) from None
    finally:
        connection.close()


def backfill_legacy_projection(request: LegacyBackfillRequest) -> LegacyBackfillResult:
    """Inventory or atomically apply one explicit legacy projection backfill."""

    if not isinstance(request, LegacyBackfillRequest):
        raise TypeError("request must be a LegacyBackfillRequest")
    _required_scope(request)
    source, destination, _same_file = _resolved_paths(request)
    if not request.dry_run and (
        not isinstance(request.expected_manifest_sha256, str)
        or not request.expected_manifest_sha256.strip()
    ):
        raise LegacyBackfillError(
            "manifest_precondition_required",
            "Apply requires the exact manifest digest returned by dry-run",
        )

    inventory = _inventory(source, request.profile_id.strip())
    if request.dry_run:
        return LegacyBackfillResult(
            manifest=inventory.manifest,
            dry_run=True,
            applied=False,
            no_op=False,
            staged_revision_ids=(),
            published_revision_ids=(),
        )
    if request.expected_manifest_sha256 != inventory.manifest.manifest_sha256:
        raise LegacyBackfillError(
            "source_changed",
            "Source content or reconstructed snapshot inventory changed after dry-run",
        )
    return _apply(destination, request, inventory)


__all__ = [
    "BackfillManifest",
    "BackfillSnapshotPlan",
    "LegacyBackfillError",
    "LegacyBackfillRequest",
    "LegacyBackfillResult",
    "backfill_legacy_projection",
]
