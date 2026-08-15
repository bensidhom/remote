"""Workspace-scoped, immutable canonical snapshot revision lifecycle.

This module is the write boundary between conversion/validation evidence and
the legacy relational SNBI read model.  Staging is append-only and generation
neutral.  Publication is a separate optimistic transaction which refreshes the
complete projection and advances the workspace data generation exactly once.

Public request/result objects are deliberately frozen.  JSON values returned
from reads are detached copies; no filesystem path or storage URI is exposed.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from app.routers.services.db_schema import LATEST_SCHEMA_VERSION
from app.routers.services.database import (
    ALL_NESTED_ARRAY_NAMES,
    SNBI_PROJECTION_CHILD_TABLES,
    SNBI_PROVENANCE_TABLES,
    TOP_LEVEL_FIELDS,
    _LOAD_SECTION_TABLES,
    _encode_projection_value,
    _insert_plain,
    _preflight_snbi_batch,
    _project_child_row,
    _projection_schema_errors,
    ensure_schema,
    get_connection,
    mapping,
)
from app.services.conversion.canonical_json import (
    CanonicalJsonError,
    canonical_json_text,
    canonical_sha256,
)
from app.services.conversion.canonical_schema import (
    CanonicalResourceLimitError,
    normalize_canonical_document,
)
from app.services.conversion.rule_bundle_manifest import (
    RuleBundleManifest,
    build_rule_bundle_manifest,
)


_PASSING_VALIDATION_STATUSES = frozenset(
    {"passed", "passed_with_warnings", "valid", "success"}
)


class RevisionStoreError(Exception):
    """Typed service error suitable for translation to an HTTP response."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)
        self.details = dict(details or {})


class WorkspaceBoundaryError(RevisionStoreError):
    pass


class RevisionNotFoundError(RevisionStoreError):
    pass


class RevisionConflictError(RevisionStoreError):
    pass


class IdempotencyConflictError(RevisionStoreError):
    pass


class RevisionInputError(RevisionStoreError):
    pass


@dataclass(frozen=True)
class CanonicalRevisionInput:
    state_code: int
    bridge_number: str
    data_year: int | None
    canonical_document: Mapping[str, Any]
    source_records: tuple[Mapping[str, Any], ...] = ()
    lineage: tuple[Mapping[str, Any], ...] = ()
    validation: Mapping[str, Any] | None = None
    workspace_id: str = ""
    artifact_receipts: tuple[Mapping[str, Any], ...] = ()
    enforce_latest_precondition: bool = False
    expected_latest_content_hash: str | None = None


@dataclass(frozen=True)
class StageCanonicalBatchRequest:
    workspace_id: str
    profile_id: str
    actor_id: str
    request_id: str
    idempotency_key: str
    expected_data_generation: int | None
    atomic_mode: str
    revisions: tuple[CanonicalRevisionInput, ...]
    write_mode: str = "replace_snapshot"
    request_metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class StagedRevisionResult:
    workspace_id: str
    revision_id: str
    state_code: int
    bridge_number: str
    data_year: int
    revision_number: int
    content_hash: str
    created: bool
    idempotent: bool
    diff: tuple[dict[str, Any], ...]
    created_at: str


@dataclass(frozen=True)
class StageCanonicalBatchResult:
    workspace_id: str
    batch_id: str
    data_generation: int
    revisions: tuple[StagedRevisionResult, ...]
    request_hash: str


@dataclass(frozen=True)
class WorkspaceState:
    workspace_id: str
    data_generation: int
    schema_generation: int
    updated_at: str


@dataclass(frozen=True)
class SnapshotRevisionRecord:
    workspace_id: str
    revision_id: str
    state_code: int
    bridge_number: str
    data_year: int
    revision_number: int
    lifecycle_state: str
    canonical_json: str
    canonical_document: Any
    content_hash: str
    parent_revision_id: str | None
    profile_id: str
    created_by: str
    created_at: str
    source_records: tuple[dict[str, Any], ...]
    lineage: tuple[dict[str, Any], ...]
    validation: dict[str, Any]
    rule_bundle_id: str = ""
    rule_bundle_sha256: str = ""
    rule_bundle_manifest: dict[str, Any] | None = None


@dataclass(frozen=True)
class PublishedSnapshotSummary:
    """Bounded, path-free metadata for one active publication pointer."""

    revision_id: str
    state_code: int
    bridge_number: str
    bridge_name: str | None
    data_year: int
    revision_number: int
    content_hash: str
    profile_id: str
    validation_status: str | None
    created_at: str
    published_at: str


@dataclass(frozen=True)
class PublishedSnapshotPage:
    """One keyset-paginated read transaction and its exact generation."""

    workspace_id: str
    data_generation: int
    schema_generation: int
    items: tuple[PublishedSnapshotSummary, ...]
    next_key: tuple[int, int, str] | None
    total_count: int
    available_data_years: tuple[int, ...]
    available_years_truncated: bool


@dataclass(frozen=True)
class MutationAuditRecord:
    workspace_id: str
    audit_id: str
    actor_id: str
    action: str
    entity_type: str
    entity_id: str
    request_id: str
    reason: str | None
    before_hash: str | None
    after_hash: str | None
    idempotency_key: str | None
    metadata: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class PublishSnapshotRevisionRequest:
    workspace_id: str
    revision_id: str
    actor_id: str
    request_id: str
    reason: str
    expected_revision_id: str | None
    expected_data_generation: int


@dataclass(frozen=True)
class PublishSnapshotRevisionResult:
    workspace_id: str
    revision_id: str
    previous_revision_id: str | None
    state_code: int
    bridge_number: str
    data_year: int
    data_generation: int
    published_at: str


@dataclass(frozen=True)
class PatchSnapshotRevisionRequest:
    workspace_id: str
    state_code: int
    bridge_number: str
    data_year: int
    patch: Any
    actor_id: str
    request_id: str
    reason: str
    expected_revision_id: str
    expected_data_generation: int
    validation: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class PatchSnapshotRevisionResult:
    workspace_id: str
    revision_id: str
    previous_revision_id: str
    state_code: int
    bridge_number: str
    data_year: int
    revision_number: int
    content_hash: str
    data_generation: int
    diff: tuple[dict[str, Any], ...]
    created_at: str


@dataclass(frozen=True)
class ArchivePublishedSnapshotRequest:
    """Exact optimistic request to remove one active publication pointer.

    Archiving is intentionally not revision deletion.  The canonical revision,
    source evidence, lineage, and validation records remain immutable and can
    be reviewed or explicitly re-published later.
    """

    workspace_id: str
    state_code: int
    bridge_number: str
    data_year: int
    revision_id: str
    actor_id: str
    request_id: str
    reason: str
    expected_data_generation: int


@dataclass(frozen=True)
class ArchivePublishedSnapshotResult:
    workspace_id: str
    revision_id: str
    state_code: int
    bridge_number: str
    data_year: int
    data_generation: int
    archived_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _new_id() -> str:
    return str(uuid.uuid4())


def _scoped_id(kind: str, workspace_id: str, external_id: str, salt: str = "") -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"bridgeguard:{kind}:{workspace_id}:{external_id}:{salt}",
        )
    )


def _json(value: Any) -> str:
    return canonical_json_text(value)


def _loads(value: str | None, default: Any) -> Any:
    if value is None:
        return copy.deepcopy(default)
    return json.loads(value)


def _connect(db_path: str) -> sqlite3.Connection:
    connection = get_connection(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def initialize_revision_store(db_path: str, *, app_revision: str = "unknown") -> None:
    """Create/migrate the revision schema without touching application data."""

    # ``ensure_schema`` is the single migration entry point and records the
    # application revision used by the deployed build.
    # The legacy-compatible entry point owns migration connection lifetime.
    # ``app_revision`` is accepted as stable API metadata for a later runner
    # integration; ensure_schema currently records its own deployed revision.
    del app_revision
    connection = sqlite3.connect(db_path)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_state'"
        ).fetchone()
    finally:
        connection.close()
    if exists is None:
        ensure_schema(db_path)


def ensure_workspace(db_path: str, workspace_id: str, display_name: str) -> WorkspaceState:
    workspace_id = _required_text(workspace_id, "workspace_id")
    display_name = _required_text(display_name, "display_name")
    initialize_revision_store(db_path)
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT id FROM workspaces WHERE id=?", (workspace_id,)
        ).fetchone()
        if existing is None:
            now = _now()
            connection.execute(
                "INSERT INTO workspaces "
                "(id, slug, display_name, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'active', ?, ?)",
                (workspace_id, workspace_id, display_name, now, now),
            )
            connection.execute(
                "INSERT INTO workspace_state "
                "(workspace_id, data_generation, schema_generation, updated_at) "
                "VALUES (?, 0, ?, ?)",
                (workspace_id, LATEST_SCHEMA_VERSION, now),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return get_workspace_state(db_path, workspace_id)


def get_workspace_state(db_path: str, workspace_id: str) -> WorkspaceState:
    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT workspace_id, data_generation, schema_generation, updated_at "
            "FROM workspace_state WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()
        if row is None:
            raise WorkspaceBoundaryError(404, "workspace_not_found", "Workspace not found")
        return WorkspaceState(
            workspace_id=str(row["workspace_id"]),
            data_generation=int(row["data_generation"]),
            schema_generation=int(row["schema_generation"]),
            updated_at=str(row["updated_at"]),
        )
    finally:
        connection.close()


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RevisionInputError(422, f"{field}_required", f"{field} is required")
    return value.strip()


def _request_payload(
    request: StageCanonicalBatchRequest,
    rule_bundle: RuleBundleManifest,
) -> dict[str, Any]:
    return {
        "workspace_id": request.workspace_id,
        "profile_id": request.profile_id,
        "actor_id": request.actor_id,
        "request_id": request.request_id,
        "idempotency_key": request.idempotency_key,
        "expected_data_generation": request.expected_data_generation,
        "atomic_mode": request.atomic_mode,
        "write_mode": request.write_mode,
        "request_metadata": dict(request.request_metadata or {}),
        "rule_bundle_id": rule_bundle.bundle_id,
        "rule_bundle_sha256": rule_bundle.sha256,
        "revisions": [asdict(revision) for revision in request.revisions],
    }


def _normalize_for_store(document: Any, profile_id: str) -> dict[str, Any]:
    """Normalize a complete document and reject every lossy diagnostic."""

    try:
        normalization = normalize_canonical_document(
            document,
            profile_id=profile_id,
        )
    except CanonicalResourceLimitError as exc:
        raise RevisionInputError(422, "canonical_resource_limit", str(exc)) from exc
    diagnostics = (
        tuple(normalization.unknown_tags)
        + tuple(normalization.wrong_container_types)
        + tuple(normalization.rejected_values)
        + tuple(normalization.duplicate_logical_child_keys)
    )
    if diagnostics:
        raise RevisionInputError(
            422,
            "canonical_normalization_failed",
            "Canonical document contains fields or structures that cannot be retained safely",
            details={
                "diagnostics": [
                    {
                        key: value.value if hasattr(value, "value") else value
                        for key, value in asdict(diagnostic).items()
                    }
                    for diagnostic in diagnostics
                ]
            },
        )
    return normalization.normalized_document


def _preflight_stage(request: StageCanonicalBatchRequest) -> list[dict[str, Any]]:
    if request.atomic_mode != "batch":
        raise RevisionInputError(
            422,
            "unsupported_atomic_mode",
            "Only all-or-nothing batch staging is supported",
        )
    if request.write_mode not in {"create_only", "replace_snapshot"}:
        raise RevisionInputError(
            422,
            "unsupported_write_mode",
            "write_mode must be create_only or replace_snapshot",
        )
    if request.request_metadata is not None and not isinstance(
        request.request_metadata, Mapping
    ):
        raise RevisionInputError(
            422, "invalid_request_metadata", "request_metadata must be an object"
        )
    _required_text(request.workspace_id, "workspace_id")
    _required_text(request.profile_id, "profile_id")
    _required_text(request.actor_id, "actor_id")
    _required_text(request.request_id, "request_id")
    _required_text(request.idempotency_key, "idempotency_key")
    if not request.revisions:
        raise RevisionInputError(422, "empty_batch", "At least one revision is required")

    keys: set[tuple[int, str, int]] = set()
    prepared: list[dict[str, Any]] = []
    for item in request.revisions:
        if item.workspace_id and item.workspace_id != request.workspace_id:
            raise WorkspaceBoundaryError(404, "workspace_not_found", "Workspace not found")
        if item.data_year is None:
            raise RevisionInputError(422, "data_year_required", "data_year is required")
        try:
            state_code = int(item.state_code)
            data_year = int(item.data_year)
        except (TypeError, ValueError) as exc:
            raise RevisionInputError(
                422, "invalid_logical_key", "state_code and data_year must be integers"
            ) from exc
        bridge_number = _required_text(item.bridge_number, "bridge_number")
        key = (state_code, bridge_number, data_year)
        if key in keys:
            raise RevisionInputError(
                422,
                "duplicate_logical_snapshot",
                "A batch cannot contain the same state/bridge/year more than once",
            )
        keys.add(key)
        if not isinstance(item.canonical_document, Mapping):
            raise RevisionInputError(
                422, "canonical_document_required", "canonical_document must be an object"
            )
        try:
            document = _normalize_for_store(
                item.canonical_document,
                request.profile_id,
            )
            canonical_text = _json(document)
            content_hash = canonical_sha256(document)
        except RevisionInputError:
            raise
        except CanonicalJsonError as exc:
            raise RevisionInputError(
                422,
                "invalid_canonical_json",
                f"Canonical document is invalid: {exc}",
            ) from exc
        if document.get("BL01") != state_code or str(document.get("BID01", "")) != bridge_number:
            raise RevisionInputError(
                422,
                "logical_key_mismatch",
                "Canonical BL01/BID01 must match the revision logical key",
            )
        if item.enforce_latest_precondition and item.expected_latest_content_hash is not None:
            expected_hash = str(item.expected_latest_content_hash).lower()
            if len(expected_hash) != 64 or any(
                character not in "0123456789abcdef" for character in expected_hash
            ):
                raise RevisionInputError(
                    422,
                    "invalid_latest_content_hash",
                    "expected_latest_content_hash must be a lowercase SHA-256 value",
                )
        structural_errors = _preflight_snbi_batch([document])
        if structural_errors:
            raise RevisionInputError(
                422,
                "invalid_canonical_document",
                structural_errors[0],
                details={"issues": structural_errors[:50]},
            )
        if not item.source_records:
            raise RevisionInputError(
                422, "source_evidence_required", "At least one source record is required"
            )
        validation = dict(item.validation or {})
        if not validation.get("status"):
            raise RevisionInputError(
                422, "validation_evidence_required", "Validation status is required"
            )
        prepared.append(
            {
                "input": item,
                "state_code": state_code,
                "bridge_number": bridge_number,
                "data_year": data_year,
                "document": document,
                "canonical_text": canonical_text,
                "content_hash": content_hash,
                "validation": validation,
            }
        )
    return prepared


def _workspace_row(connection: sqlite3.Connection, workspace_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT ws.id, st.data_generation, st.schema_generation, st.updated_at "
        "FROM workspaces ws JOIN workspace_state st ON st.workspace_id=ws.id "
        "WHERE ws.id=? AND ws.status='active'",
        (workspace_id,),
    ).fetchone()
    if row is None:
        raise WorkspaceBoundaryError(404, "workspace_not_found", "Workspace not found")
    return row


def _id_is_available(
    connection: sqlite3.Connection, table: str, candidate: str, workspace_id: str
) -> tuple[bool, bool]:
    row = connection.execute(
        f'SELECT workspace_id FROM "{table}" WHERE id=?', (candidate,)
    ).fetchone()
    return (row is None, bool(row is not None and row[0] == workspace_id))


def _ensure_artifact(
    connection: sqlite3.Connection,
    workspace_id: str,
    external_id: str,
    actor_id: str,
    *,
    trusted_receipt: Mapping[str, Any] | None = None,
) -> str:
    external_id = _required_text(external_id, "source_artifact_id")
    free, same_workspace = _id_is_available(
        connection, "source_artifacts", external_id, workspace_id
    )
    artifact_id = external_id
    if not free and not same_workspace:
        artifact_id = _scoped_id("artifact", workspace_id, external_id)
        free, same_workspace = _id_is_available(
            connection, "source_artifacts", artifact_id, workspace_id
        )
    if free:
        evidence = external_id.encode("utf-8")
        sha256 = canonical_sha256({"external_source_artifact_id": external_id})
        byte_size = len(evidence)
        original_filename = None
        media_type = "application/json"
        artifact_kind = "inline_source_reference"
        storage_uri = None
        source_system = "revision_store"
        if trusted_receipt is not None:
            if str(trusted_receipt.get("artifact_id") or "") != external_id:
                raise RevisionInputError(
                    422, "artifact_receipt_mismatch", "Artifact receipt identity does not match"
                )
            claimed_sha = str(trusted_receipt.get("sha256") or "").lower()
            if len(claimed_sha) != 64 or any(ch not in "0123456789abcdef" for ch in claimed_sha):
                raise RevisionInputError(
                    422, "artifact_receipt_invalid", "Artifact receipt SHA-256 is invalid"
                )
            try:
                claimed_size = int(trusted_receipt.get("size_bytes"))
            except (TypeError, ValueError) as exc:
                raise RevisionInputError(
                    422, "artifact_receipt_invalid", "Artifact receipt size is invalid"
                ) from exc
            if claimed_size < 1:
                raise RevisionInputError(
                    422, "artifact_receipt_invalid", "Artifact receipt size is invalid"
                )
            sha256 = claimed_sha
            byte_size = claimed_size
            original_filename = str(trusted_receipt.get("original_filename") or "")[:180] or None
            media_type = str(trusted_receipt.get("media_type") or "application/json")[:128]
            artifact_kind = str(trusted_receipt.get("artifact_kind") or "immutable_json_upload")[:128]
            storage_uri = f"source-artifact://{external_id}"
            source_system = "bridgeguard_source_artifact_store"
        connection.execute(
            "INSERT INTO source_artifacts "
            "(id, workspace_id, sha256, byte_size, original_filename, media_type, "
            "artifact_kind, storage_uri, source_system, created_by, created_at, retention_until) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                artifact_id,
                workspace_id,
                sha256,
                byte_size,
                original_filename,
                media_type,
                artifact_kind,
                storage_uri,
                source_system,
                actor_id,
                _now(),
            ),
        )
    elif not same_workspace:
        raise RevisionConflictError(409, "source_id_conflict", "Source identifier conflict")
    return artifact_id


def _ensure_source_records(
    connection: sqlite3.Connection,
    workspace_id: str,
    item: CanonicalRevisionInput,
    actor_id: str,
    state_code: int,
    bridge_number: str,
    data_year: int,
) -> tuple[list[str], dict[str, str]]:
    resolved: list[str] = []
    by_external: dict[str, str] = {}
    trusted_receipts = {
        str(receipt.get("artifact_id") or ""): receipt
        for receipt in item.artifact_receipts
        if isinstance(receipt, Mapping) and receipt.get("artifact_id")
    }
    for source in item.source_records:
        if not isinstance(source, Mapping):
            raise RevisionInputError(422, "invalid_source_record", "Source record must be an object")
        external_artifact = _required_text(source.get("source_artifact_id"), "source_artifact_id")
        external_record = _required_text(source.get("source_record_id"), "source_record_id")
        artifact_id = _ensure_artifact(
            connection,
            workspace_id,
            external_artifact,
            actor_id,
            trusted_receipt=trusted_receipts.get(external_artifact),
        )
        raw_text = _json(source)
        raw_hash = canonical_sha256(source)

        free, same_workspace = _id_is_available(
            connection, "source_records", external_record, workspace_id
        )
        record_id = external_record
        if not free:
            existing = connection.execute(
                "SELECT raw_record_sha256 FROM source_records WHERE id=? AND workspace_id=?",
                (record_id, workspace_id),
            ).fetchone()
            if same_workspace and existing is not None and existing[0] == raw_hash:
                resolved.append(record_id)
                by_external[external_record] = record_id
                continue
            record_id = _scoped_id("record", workspace_id, external_record, raw_hash)
            free, same_workspace = _id_is_available(
                connection, "source_records", record_id, workspace_id
            )
            if not free:
                existing = connection.execute(
                    "SELECT raw_record_sha256 FROM source_records WHERE id=? AND workspace_id=?",
                    (record_id, workspace_id),
                ).fetchone()
                if not same_workspace or existing is None or existing[0] != raw_hash:
                    raise RevisionConflictError(
                        409, "source_id_conflict", "Source identifier conflict"
                    )
                resolved.append(record_id)
                by_external[external_record] = record_id
                continue

        ordinal = int(
            connection.execute(
                "SELECT COALESCE(MAX(record_ordinal), -1) + 1 FROM source_records "
                "WHERE workspace_id=? AND source_artifact_id=?",
                (workspace_id, artifact_id),
            ).fetchone()[0]
        )
        connection.execute(
            "INSERT INTO source_records "
            "(id, workspace_id, source_artifact_id, record_ordinal, source_role, "
            "record_identity, raw_record_json, raw_record_sha256, parse_status, "
            "parse_issues_json, state_code, bridge_number, data_year, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'parsed', NULL, ?, ?, ?, ?)",
            (
                record_id,
                workspace_id,
                artifact_id,
                ordinal,
                str(source.get("record_kind") or "source"),
                external_record,
                raw_text,
                raw_hash,
                state_code,
                bridge_number,
                data_year,
                _now(),
            ),
        )
        resolved.append(record_id)
        by_external[external_record] = record_id
    return resolved, by_external


def _json_pointer_get(document: Any, pointer: str) -> tuple[bool, Any]:
    if pointer == "":
        return True, document
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        return False, None
    current = document
    for encoded in pointer[1:].split("/"):
        token = encoded.replace("~1", "/").replace("~0", "~")
        try:
            if isinstance(current, list):
                current = current[int(token)]
            elif isinstance(current, dict):
                current = current[token]
            else:
                return False, None
        except (KeyError, IndexError, TypeError, ValueError):
            return False, None
    return True, current


def _insert_evidence(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    revision_id: str,
    source_ids: Sequence[str],
    source_map: Mapping[str, str],
    item: CanonicalRevisionInput,
    document: Mapping[str, Any],
    content_hash: str,
    profile_id: str,
    rule_bundle: RuleBundleManifest,
) -> None:
    for ordinal, source_id in enumerate(source_ids):
        connection.execute(
            "INSERT INTO snapshot_revision_sources "
            "(id, workspace_id, snapshot_revision_id, source_record_id, source_ordinal, source_role) "
            "VALUES (?, ?, ?, ?, ?, 'evidence')",
            (_new_id(), workspace_id, revision_id, source_id, ordinal),
        )

    first_source = source_ids[0] if source_ids else None
    for ordinal, lineage_value in enumerate(item.lineage):
        lineage = dict(lineage_value)
        requested_source = lineage.get("source_record_id")
        bound_source = source_map.get(str(requested_source), first_source)
        target_pointer = str(lineage.get("target_pointer") or "")
        present, value = _json_pointer_get(document, target_pointer)
        connection.execute(
            "INSERT INTO field_lineage "
            "(id, workspace_id, snapshot_revision_id, target_pointer, lineage_ordinal, "
            "source_record_id, source_pointer, rule_id, resolution_status, value_json, "
            "evidence_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _new_id(),
                workspace_id,
                revision_id,
                target_pointer,
                ordinal,
                bound_source,
                lineage.get("source_pointer"),
                lineage.get("rule_id"),
                str(lineage.get("status") or "unknown"),
                _json(value) if present else None,
                _json(lineage),
                _now(),
            ),
        )

    validation = dict(item.validation or {})
    issues = list(validation.get("issues") or ())
    validation_id = _new_id()
    now = _now()
    summary = {key: value for key, value in validation.items() if key != "issues"}
    summary["rule_bundle_manifest"] = rule_bundle.manifest
    summary["rule_bundle_sha256"] = rule_bundle.sha256
    connection.execute(
        "INSERT INTO validation_runs "
        "(id, workspace_id, snapshot_revision_id, profile_id, rule_bundle_id, "
        "rule_bundle_sha256, validator_version, status, canonical_sha256, "
        "summary_json, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            validation_id,
            workspace_id,
            revision_id,
            str(validation.get("profile_id") or profile_id),
            rule_bundle.bundle_id,
            rule_bundle.sha256,
            "revision-store-v1",
            str(validation.get("status")),
            content_hash,
            _json(summary),
            now,
            now,
        ),
    )
    for ordinal, issue_value in enumerate(issues):
        issue = dict(issue_value)
        connection.execute(
            "INSERT INTO validation_issues "
            "(id, workspace_id, validation_run_id, issue_ordinal, severity, code, "
            "json_pointer, message, evidence_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _new_id(),
                workspace_id,
                validation_id,
                ordinal,
                str(issue.get("severity") or "error"),
                str(issue.get("rule_id") or issue.get("code") or "validation_issue"),
                str(issue.get("json_pointer") or ""),
                str(issue.get("message") or "Validation issue"),
                _json(issue),
                _now(),
            ),
        )


def _escape_pointer(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _diff(before: Any, after: Any, pointer: str = "") -> tuple[dict[str, Any], ...]:
    changes: list[dict[str, Any]] = []

    def visit(left: Any, right: Any, path: str) -> None:
        if type(left) is not type(right):
            changes.append({"op": "replace", "path": path, "before": left, "after": right})
            return
        if isinstance(left, dict):
            for key in sorted(set(left) | set(right)):
                child_path = f"{path}/{_escape_pointer(str(key))}"
                if key not in left:
                    changes.append({"op": "add", "path": child_path, "after": right[key]})
                elif key not in right:
                    changes.append({"op": "remove", "path": child_path, "before": left[key]})
                else:
                    visit(left[key], right[key], child_path)
            return
        if isinstance(left, list):
            shared = min(len(left), len(right))
            for index in range(shared):
                visit(left[index], right[index], f"{path}/{index}")
            for index in range(shared, len(left)):
                changes.append(
                    {"op": "remove", "path": f"{path}/{index}", "before": left[index]}
                )
            for index in range(shared, len(right)):
                changes.append(
                    {"op": "add", "path": f"{path}/{index}", "after": right[index]}
                )
            return
        if left != right:
            changes.append({"op": "replace", "path": path, "before": left, "after": right})

    visit(before, after, pointer)
    changes.sort(key=lambda change: change["path"])
    return tuple(changes)


def _stage_result_payload(result: StageCanonicalBatchResult) -> dict[str, Any]:
    return asdict(result)


def _stage_result_from_payload(payload: Mapping[str, Any]) -> StageCanonicalBatchResult:
    revisions: list[StagedRevisionResult] = []
    for raw_item in payload["revisions"]:
        item = dict(raw_item)
        item["diff"] = tuple(item.get("diff") or ())
        revisions.append(StagedRevisionResult(**item))
    return StageCanonicalBatchResult(
        workspace_id=str(payload["workspace_id"]),
        batch_id=str(payload["batch_id"]),
        data_generation=int(payload["data_generation"]),
        revisions=tuple(revisions),
        request_hash=str(payload["request_hash"]),
    )


def _audit(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    actor_id: str,
    action: str,
    entity_id: str,
    request_id: str,
    reason: str | None,
    before_hash: str | None,
    after_hash: str | None,
    metadata: Mapping[str, Any],
) -> None:
    connection.execute(
        "INSERT INTO mutation_audit "
        "(id, workspace_id, actor_id, action, entity_type, entity_id, request_id, "
        "reason, before_sha256, after_sha256, metadata_json, occurred_at) "
        "VALUES (?, ?, ?, ?, 'snapshot_revision', ?, ?, ?, ?, ?, ?, ?)",
        (
            _new_id(),
            workspace_id,
            actor_id,
            action,
            entity_id,
            request_id,
            reason,
            before_hash,
            after_hash,
            _json(metadata),
            _now(),
        ),
    )


def _request_replay_metadata(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    action: str,
    request_id: str,
    request_hash: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT metadata_json FROM mutation_audit WHERE workspace_id=? "
        "AND action=? AND request_id=? ORDER BY occurred_at, id LIMIT 1",
        (workspace_id, action, request_id),
    ).fetchone()
    if row is None:
        return None
    metadata = _loads(row["metadata_json"], {})
    if metadata.get("request_hash") != request_hash:
        raise IdempotencyConflictError(
            409,
            "request_id_payload_conflict",
            "The request ID was already used for a different mutation payload",
        )
    if not isinstance(metadata.get("result"), dict):
        raise RevisionConflictError(
            409,
            "request_replay_unavailable",
            "The original request completed but its replay result is unavailable",
        )
    return metadata


def stage_canonical_batch(
    db_path: str, request: StageCanonicalBatchRequest
) -> StageCanonicalBatchResult:
    prepared = _preflight_stage(request)
    try:
        rule_bundle = build_rule_bundle_manifest(request.profile_id)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RevisionInputError(
            422,
            "rule_bundle_manifest_unavailable",
            "The revision profile could not be bound to an immutable rule bundle",
        ) from exc
    try:
        request_hash = canonical_sha256(_request_payload(request, rule_bundle))
    except CanonicalJsonError as exc:
        raise RevisionInputError(422, "invalid_request_json", str(exc)) from exc

    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        state = _workspace_row(connection, request.workspace_id)
        existing_batch = connection.execute(
            "SELECT request_sha256, counts_json FROM conversion_batches "
            "WHERE workspace_id=? AND idempotency_key=?",
            (request.workspace_id, request.idempotency_key),
        ).fetchone()
        if existing_batch is not None:
            if existing_batch["request_sha256"] != request_hash:
                raise IdempotencyConflictError(
                    409,
                    "idempotency_payload_conflict",
                    "The idempotency key was already used for a different payload",
                )
            result = _stage_result_from_payload(json.loads(existing_batch["counts_json"])["result"])
            connection.rollback()
            return result

        generation = int(state["data_generation"])
        if (
            request.expected_data_generation is not None
            and int(request.expected_data_generation) != generation
        ):
            raise RevisionConflictError(
                409,
                "stale_data_generation",
                "Workspace data generation changed",
            )

        # Plan every outcome while holding the writer reservation.  IDs and
        # diffs are reserved before any evidence row is written, allowing the
        # immutable conversion-batch row to be inserted in final form once.
        batch_id = _new_id()
        staged: list[StagedRevisionResult] = []
        plans: list[tuple[dict[str, Any], sqlite3.Row | None, StagedRevisionResult]] = []
        for value in prepared:
            latest = connection.execute(
                "SELECT * FROM snapshot_revisions WHERE workspace_id=? AND state_code=? "
                "AND bridge_number=? AND data_year=? ORDER BY revision_number DESC LIMIT 1",
                (
                    request.workspace_id,
                    value["state_code"],
                    value["bridge_number"],
                    value["data_year"],
                ),
            ).fetchone()
            if value["input"].enforce_latest_precondition:
                actual_latest_hash = (
                    str(latest["canonical_sha256"]) if latest is not None else None
                )
                if actual_latest_hash != value["input"].expected_latest_content_hash:
                    raise RevisionConflictError(
                        409,
                        "stale_snapshot_preflight",
                        "A logical snapshot changed after import preflight",
                        details={
                            "state_code": value["state_code"],
                            "bridge_number": value["bridge_number"],
                            "data_year": value["data_year"],
                        },
                    )
            if (
                latest is not None
                and latest["canonical_sha256"] == value["content_hash"]
                and str(latest["profile_id"]) == request.profile_id
                and str(latest["rule_bundle_sha256"] or "") == rule_bundle.sha256
            ):
                outcome = StagedRevisionResult(
                        workspace_id=request.workspace_id,
                        revision_id=str(latest["id"]),
                        state_code=value["state_code"],
                        bridge_number=value["bridge_number"],
                        data_year=value["data_year"],
                        revision_number=int(latest["revision_number"]),
                        content_hash=value["content_hash"],
                        created=False,
                        idempotent=True,
                        diff=(),
                        created_at=str(latest["created_at"]),
                )
                staged.append(outcome)
                plans.append((value, latest, outcome))
                continue
            if latest is not None and request.write_mode == "create_only":
                raise RevisionConflictError(
                    409,
                    "snapshot_already_exists",
                    "create_only cannot add a new revision to an existing logical snapshot",
                    details={
                        "state_code": value["state_code"],
                        "bridge_number": value["bridge_number"],
                        "data_year": value["data_year"],
                    },
                )
            revision_id = _new_id()
            revision_number = int(latest["revision_number"]) + 1 if latest else 1
            before_document = json.loads(latest["canonical_json"]) if latest else None
            difference = _diff(before_document, value["document"]) if latest else ()
            created_at = _now()
            outcome = StagedRevisionResult(
                workspace_id=request.workspace_id,
                revision_id=revision_id,
                state_code=value["state_code"],
                bridge_number=value["bridge_number"],
                data_year=value["data_year"],
                revision_number=revision_number,
                content_hash=value["content_hash"],
                created=True,
                idempotent=False,
                diff=difference,
                created_at=created_at,
            )
            staged.append(outcome)
            plans.append((value, latest, outcome))

        result = StageCanonicalBatchResult(
            workspace_id=request.workspace_id,
            batch_id=batch_id,
            data_generation=generation,
            revisions=tuple(staged),
            request_hash=request_hash,
        )
        now = _now()
        connection.execute(
            "INSERT INTO conversion_batches "
            "(id, workspace_id, profile_id, rule_bundle_id, rule_bundle_sha256, "
            "options_json, request_sha256, idempotency_key, actor_id, status, counts_json, "
            "requested_at, started_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?)",
            (
                batch_id,
                request.workspace_id,
                request.profile_id,
                rule_bundle.bundle_id,
                rule_bundle.sha256,
                _json(
                    {
                        "atomic_mode": request.atomic_mode,
                        "write_mode": request.write_mode,
                        "request_metadata": dict(request.request_metadata or {}),
                        "rule_bundle_manifest": rule_bundle.manifest,
                    }
                ),
                request_hash,
                request.idempotency_key,
                request.actor_id,
                _json({"result": _stage_result_payload(result)}),
                now,
                now,
                now,
            ),
        )

        for value, latest, outcome in plans:
            source_ids, source_map = _ensure_source_records(
                connection,
                request.workspace_id,
                value["input"],
                request.actor_id,
                value["state_code"],
                value["bridge_number"],
                value["data_year"],
            )
            for source_id in source_ids:
                connection.execute(
                    "INSERT INTO eligibility_decisions "
                    "(id, workspace_id, conversion_batch_id, source_record_id, decision, "
                    "reason_code, reason_detail, evidence_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _new_id(),
                        request.workspace_id,
                        batch_id,
                        source_id,
                        "accepted" if outcome.created else "no_change",
                        "canonical_revision_created" if outcome.created else "identical_canonical_content",
                        None if outcome.created else "Latest canonical content hash is identical",
                        _json(
                            {
                                "snapshot_revision_id": outcome.revision_id,
                                "canonical_sha256": outcome.content_hash,
                            }
                        ),
                        _now(),
                    ),
                )
            if not outcome.created:
                continue
            connection.execute(
                "INSERT INTO snapshot_revisions "
                "(id, workspace_id, conversion_batch_id, source_record_id, state_code, "
                "bridge_number, data_year, revision_number, lifecycle_state, canonical_json, "
                "canonical_sha256, parent_revision_id, profile_id, rule_bundle_sha256, "
                "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'staged', ?, ?, ?, ?, ?, ?, ?)",
                (
                    outcome.revision_id,
                    request.workspace_id,
                    batch_id,
                    source_ids[0],
                    value["state_code"],
                    value["bridge_number"],
                    value["data_year"],
                    outcome.revision_number,
                    value["canonical_text"],
                    value["content_hash"],
                    str(latest["id"]) if latest else None,
                    request.profile_id,
                    rule_bundle.sha256,
                    request.actor_id,
                    outcome.created_at,
                ),
            )
            _insert_evidence(
                connection,
                workspace_id=request.workspace_id,
                revision_id=outcome.revision_id,
                source_ids=source_ids,
                source_map=source_map,
                item=value["input"],
                document=value["document"],
                content_hash=value["content_hash"],
                profile_id=request.profile_id,
                rule_bundle=rule_bundle,
            )
            _audit(
                connection,
                workspace_id=request.workspace_id,
                actor_id=request.actor_id,
                action="stage_revision",
                entity_id=outcome.revision_id,
                request_id=request.request_id,
                reason=None,
                before_hash=str(latest["canonical_sha256"]) if latest else None,
                after_hash=value["content_hash"],
                metadata={
                    "idempotency_key": request.idempotency_key,
                    "batch_id": batch_id,
                    "rule_bundle_id": rule_bundle.bundle_id,
                    "rule_bundle_sha256": rule_bundle.sha256,
                    "diff": outcome.diff,
                    "logical_key": [
                        value["state_code"], value["bridge_number"], value["data_year"]
                    ],
                },
            )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _revision_record(connection: sqlite3.Connection, row: sqlite3.Row) -> SnapshotRevisionRecord:
    workspace_id = str(row["workspace_id"])
    revision_id = str(row["id"])
    source_rows = connection.execute(
        "SELECT sr.raw_record_json FROM snapshot_revision_sources srs "
        "JOIN source_records sr ON sr.workspace_id=srs.workspace_id AND sr.id=srs.source_record_id "
        "WHERE srs.workspace_id=? AND srs.snapshot_revision_id=? ORDER BY srs.source_ordinal",
        (workspace_id, revision_id),
    ).fetchall()
    lineage_rows = connection.execute(
        "SELECT target_pointer, source_record_id, source_pointer, rule_id, resolution_status, "
        "value_json, evidence_json FROM field_lineage WHERE workspace_id=? "
        "AND snapshot_revision_id=? ORDER BY target_pointer, lineage_ordinal",
        (workspace_id, revision_id),
    ).fetchall()
    lineage: list[dict[str, Any]] = []
    for value in lineage_rows:
        evidence = _loads(value["evidence_json"], {})
        evidence.update(
            {
                "target_pointer": value["target_pointer"],
                "source_record_id": value["source_record_id"],
                "source_pointer": value["source_pointer"],
                "rule_id": value["rule_id"],
                "status": value["resolution_status"],
            }
        )
        lineage.append(evidence)

    validation_row = connection.execute(
        "SELECT * FROM validation_runs WHERE workspace_id=? AND snapshot_revision_id=? "
        "ORDER BY completed_at DESC, id DESC LIMIT 1",
        (workspace_id, revision_id),
    ).fetchone()
    validation: dict[str, Any] = {}
    rule_bundle_id = ""
    rule_bundle_sha256 = str(row["rule_bundle_sha256"] or "")
    rule_bundle_manifest: dict[str, Any] | None = None
    if validation_row is not None:
        validation = _loads(validation_row["summary_json"], {})
        manifest_value = validation.get("rule_bundle_manifest")
        if isinstance(manifest_value, Mapping):
            rule_bundle_manifest = dict(manifest_value)
        rule_bundle_id = str(validation_row["rule_bundle_id"] or "")
        rule_bundle_sha256 = str(
            validation_row["rule_bundle_sha256"] or rule_bundle_sha256
        )
        validation.update(
            {
                "profile_id": validation_row["profile_id"],
                "status": validation_row["status"],
                "rule_bundle_id": rule_bundle_id,
                "rule_bundle_sha256": rule_bundle_sha256,
                "validator_version": validation_row["validator_version"],
                "canonical_sha256": validation_row["canonical_sha256"],
            }
        )
        issue_rows = connection.execute(
            "SELECT * FROM validation_issues WHERE workspace_id=? AND validation_run_id=? "
            "ORDER BY issue_ordinal",
            (workspace_id, validation_row["id"]),
        ).fetchall()
        issues: list[dict[str, Any]] = []
        for issue_row in issue_rows:
            issue = _loads(issue_row["evidence_json"], {})
            issue.update(
                {
                    "rule_id": issue_row["code"],
                    "severity": issue_row["severity"],
                    "json_pointer": issue_row["json_pointer"],
                    "message": issue_row["message"],
                }
            )
            issues.append(issue)
        validation["issues"] = issues

    return SnapshotRevisionRecord(
        workspace_id=workspace_id,
        revision_id=revision_id,
        state_code=int(row["state_code"]),
        bridge_number=str(row["bridge_number"]),
        data_year=int(row["data_year"]),
        revision_number=int(row["revision_number"]),
        lifecycle_state=str(row["lifecycle_state"]),
        canonical_json=str(row["canonical_json"]),
        canonical_document=json.loads(row["canonical_json"]),
        content_hash=str(row["canonical_sha256"]),
        parent_revision_id=str(row["parent_revision_id"]) if row["parent_revision_id"] else None,
        profile_id=str(row["profile_id"]),
        created_by=str(row["created_by"]),
        created_at=str(row["created_at"]),
        source_records=tuple(json.loads(source["raw_record_json"]) for source in source_rows),
        lineage=tuple(lineage),
        validation=validation,
        rule_bundle_id=rule_bundle_id,
        rule_bundle_sha256=rule_bundle_sha256,
        rule_bundle_manifest=rule_bundle_manifest,
    )


def get_snapshot_revision(
    db_path: str, workspace_id: str, revision_id: str
) -> SnapshotRevisionRecord:
    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT * FROM snapshot_revisions WHERE workspace_id=? AND id=?",
            (workspace_id, revision_id),
        ).fetchone()
        if row is None:
            # Deliberately identical for an absent row and a row in another
            # workspace: callers cannot enumerate cross-workspace IDs.
            exists_elsewhere = connection.execute(
                "SELECT 1 FROM snapshot_revisions WHERE id=?", (revision_id,)
            ).fetchone()
            error_type = WorkspaceBoundaryError if exists_elsewhere else RevisionNotFoundError
            raise error_type(404, "revision_not_found", "Revision not found")
        return _revision_record(connection, row)
    finally:
        connection.close()


def get_snapshot_history(
    db_path: str,
    workspace_id: str,
    state_code: int,
    bridge_number: str,
    data_year: int,
) -> tuple[SnapshotRevisionRecord, ...]:
    connection = _connect(db_path)
    try:
        _workspace_row(connection, workspace_id)
        rows = connection.execute(
            "SELECT * FROM snapshot_revisions WHERE workspace_id=? AND state_code=? "
            "AND bridge_number=? AND data_year=? ORDER BY revision_number",
            (workspace_id, state_code, bridge_number, data_year),
        ).fetchall()
        records = [_revision_record(connection, row) for row in rows]
        if not records:
            return ()

        # Lifecycle is derived from immutable mutation events and the active
        # pointer.  The snapshot row itself is never rewritten merely because
        # it was published, superseded, archived, or restored.
        revision_ids = {record.revision_id for record in records}
        last_actions: dict[str, str] = {}
        for audit in connection.execute(
            "SELECT ma.entity_id, ma.action FROM mutation_audit ma "
            "JOIN snapshot_revisions sr ON sr.workspace_id=ma.workspace_id "
            "AND sr.id=ma.entity_id WHERE ma.workspace_id=? "
            "AND sr.state_code=? AND sr.bridge_number=? AND sr.data_year=? "
            "AND ma.entity_type='snapshot_revision' "
            "AND ma.action IN ('publish_revision', 'archive_revision') "
            "ORDER BY ma.occurred_at, ma.rowid",
            (workspace_id, state_code, bridge_number, data_year),
        ).fetchall():
            entity_id = str(audit["entity_id"])
            if entity_id in revision_ids:
                last_actions[entity_id] = str(audit["action"])
        published = connection.execute(
            "SELECT snapshot_revision_id FROM published_snapshots WHERE workspace_id=? "
            "AND state_code=? AND bridge_number=? AND data_year=?",
            (workspace_id, state_code, bridge_number, data_year),
        ).fetchone()
        current_id = str(published["snapshot_revision_id"]) if published else None
        effective: list[SnapshotRevisionRecord] = []
        for record in records:
            if record.revision_id == current_id:
                status = "published"
            elif last_actions.get(record.revision_id) == "archive_revision":
                status = "archived"
            elif last_actions.get(record.revision_id) == "publish_revision":
                status = "superseded"
            else:
                status = record.lifecycle_state
            effective.append(replace(record, lifecycle_state=status))
        return tuple(effective)
    finally:
        connection.close()


def get_published_snapshot(
    db_path: str,
    workspace_id: str,
    state_code: int,
    bridge_number: str,
    data_year: int,
) -> SnapshotRevisionRecord | None:
    connection = _connect(db_path)
    try:
        _workspace_row(connection, workspace_id)
        row = connection.execute(
            "SELECT sr.* FROM published_snapshots ps JOIN snapshot_revisions sr "
            "ON sr.workspace_id=ps.workspace_id AND sr.id=ps.snapshot_revision_id "
            "WHERE ps.workspace_id=? AND ps.state_code=? AND ps.bridge_number=? AND ps.data_year=?",
            (workspace_id, state_code, bridge_number, data_year),
        ).fetchone()
        return _revision_record(connection, row) if row is not None else None
    finally:
        connection.close()


def list_published_snapshots(
    db_path: str,
    workspace_id: str,
    *,
    state_code: int | None = None,
    data_year: int | None = None,
    limit: int = 100,
    after: tuple[int, int, str] | None = None,
    expected_data_generation: int | None = None,
) -> PublishedSnapshotPage:
    """List active snapshots without exposing raw projection tables.

    The workspace state, count, year facets, and page rows are read from one
    SQLite snapshot.  Cursors carry the generation returned by a prior page;
    callers must restart pagination if publication changes the generation.
    """

    workspace_id = _required_text(workspace_id, "workspace_id")
    if type(limit) is not int or limit < 1 or limit > 500:
        raise RevisionInputError(
            422, "invalid_snapshot_page_limit", "limit must be between 1 and 500"
        )
    if state_code is not None and (type(state_code) is not int or state_code < 0):
        raise RevisionInputError(
            422, "invalid_snapshot_filter", "state_code must be a non-negative integer"
        )
    if data_year is not None and (type(data_year) is not int or data_year < 0):
        raise RevisionInputError(
            422, "invalid_snapshot_filter", "data_year must be a non-negative integer"
        )
    if after is not None:
        if (
            not isinstance(after, tuple)
            or len(after) != 3
            or type(after[0]) is not int
            or type(after[1]) is not int
            or type(after[2]) is not str
            or not after[2]
        ):
            raise RevisionInputError(
                422, "invalid_snapshot_cursor", "Snapshot cursor is invalid"
            )

    connection = _connect(db_path)
    try:
        # Pin all reads below to one WAL snapshot before inspecting generation.
        connection.execute("BEGIN")
        workspace = _workspace_row(connection, workspace_id)
        generation = int(workspace["data_generation"])
        if (
            expected_data_generation is not None
            and int(expected_data_generation) != generation
        ):
            raise RevisionConflictError(
                409,
                "stale_snapshot_cursor",
                "Published snapshots changed; restart listing from the first page",
            )

        filters = ["ps.workspace_id=?"]
        parameters: list[Any] = [workspace_id]
        if state_code is not None:
            filters.append("ps.state_code=?")
            parameters.append(state_code)
        if data_year is not None:
            filters.append("ps.data_year=?")
            parameters.append(data_year)
        filter_sql = " AND ".join(filters)

        total_row = connection.execute(
            f"SELECT COUNT(*) AS count FROM published_snapshots ps WHERE {filter_sql}",
            tuple(parameters),
        ).fetchone()
        total_count = int(total_row["count"] if total_row is not None else 0)

        year_filters = ["workspace_id=?"]
        year_parameters: list[Any] = [workspace_id]
        if state_code is not None:
            year_filters.append("state_code=?")
            year_parameters.append(state_code)
        year_rows = connection.execute(
            "SELECT DISTINCT data_year FROM published_snapshots WHERE "
            + " AND ".join(year_filters)
            + " ORDER BY data_year DESC LIMIT 201",
            tuple(year_parameters),
        ).fetchall()
        available_years_truncated = len(year_rows) > 200
        available_data_years = tuple(
            int(row["data_year"]) for row in year_rows[:200]
        )

        page_filters = list(filters)
        page_parameters = list(parameters)
        if after is not None:
            cursor_state, cursor_year, cursor_bridge = after
            page_filters.append(
                "(ps.state_code>? OR "
                "(ps.state_code=? AND ps.data_year>?) OR "
                "(ps.state_code=? AND ps.data_year=? AND "
                "ps.bridge_number COLLATE BINARY>? COLLATE BINARY))"
            )
            page_parameters.extend(
                [
                    cursor_state,
                    cursor_state,
                    cursor_year,
                    cursor_state,
                    cursor_year,
                    cursor_bridge,
                ]
            )

        rows = connection.execute(
            "SELECT ps.state_code, ps.bridge_number, ps.data_year, "
            "ps.snapshot_revision_id, ps.published_at, sr.revision_number, "
            "sr.canonical_sha256, sr.profile_id, sr.created_at, "
            "json_extract(sr.canonical_json, '$.BID02') AS bridge_name, "
            "(SELECT vr.status FROM validation_runs vr "
            " WHERE vr.workspace_id=ps.workspace_id "
            " AND vr.snapshot_revision_id=ps.snapshot_revision_id "
            " ORDER BY vr.completed_at DESC, vr.id DESC LIMIT 1) AS validation_status "
            "FROM published_snapshots ps JOIN snapshot_revisions sr "
            "ON sr.workspace_id=ps.workspace_id AND sr.id=ps.snapshot_revision_id "
            "WHERE "
            + " AND ".join(page_filters)
            + " ORDER BY ps.state_code, ps.data_year, "
            "ps.bridge_number COLLATE BINARY LIMIT ?",
            tuple([*page_parameters, limit + 1]),
        ).fetchall()
        has_more = len(rows) > limit
        selected_rows = rows[:limit]
        items = tuple(
            PublishedSnapshotSummary(
                revision_id=str(row["snapshot_revision_id"]),
                state_code=int(row["state_code"]),
                bridge_number=str(row["bridge_number"]),
                bridge_name=(
                    str(row["bridge_name"]) if row["bridge_name"] is not None else None
                ),
                data_year=int(row["data_year"]),
                revision_number=int(row["revision_number"]),
                content_hash=str(row["canonical_sha256"]),
                profile_id=str(row["profile_id"]),
                validation_status=(
                    str(row["validation_status"])
                    if row["validation_status"] is not None
                    else None
                ),
                created_at=str(row["created_at"]),
                published_at=str(row["published_at"]),
            )
            for row in selected_rows
        )
        next_key = None
        if has_more and selected_rows:
            last = selected_rows[-1]
            next_key = (
                int(last["state_code"]),
                int(last["data_year"]),
                str(last["bridge_number"]),
            )
        return PublishedSnapshotPage(
            workspace_id=workspace_id,
            data_generation=generation,
            schema_generation=int(workspace["schema_generation"]),
            items=items,
            next_key=next_key,
            total_count=total_count,
            available_data_years=available_data_years,
            available_years_truncated=available_years_truncated,
        )
    finally:
        connection.close()


def list_mutation_audit(
    db_path: str, workspace_id: str
) -> tuple[MutationAuditRecord, ...]:
    connection = _connect(db_path)
    try:
        _workspace_row(connection, workspace_id)
        rows = connection.execute(
            "SELECT * FROM mutation_audit WHERE workspace_id=? ORDER BY occurred_at, id",
            (workspace_id,),
        ).fetchall()
        result: list[MutationAuditRecord] = []
        for row in rows:
            metadata = _loads(row["metadata_json"], {})
            result.append(
                MutationAuditRecord(
                    workspace_id=workspace_id,
                    audit_id=str(row["id"]),
                    actor_id=str(row["actor_id"]),
                    action=str(row["action"]),
                    entity_type=str(row["entity_type"]),
                    entity_id=str(row["entity_id"]),
                    request_id=str(row["request_id"]),
                    reason=str(row["reason"]) if row["reason"] is not None else None,
                    before_hash=str(row["before_sha256"]) if row["before_sha256"] else None,
                    after_hash=str(row["after_sha256"]) if row["after_sha256"] else None,
                    idempotency_key=metadata.get("idempotency_key"),
                    metadata=metadata,
                    created_at=str(row["occurred_at"]),
                )
            )
        return tuple(result)
    finally:
        connection.close()


def _refresh_projection(connection: sqlite3.Connection, document: Mapping[str, Any], data_year: int) -> None:
    errors = _projection_schema_errors(connection.cursor())
    if errors:
        raise RevisionStoreError(
            500, "projection_schema_invalid", errors[0], details={"issues": errors}
        )
    state_code = document["BL01"]
    bridge_number = document["BID01"]
    bridge_row: dict[str, Any] = {}
    for tag in sorted(TOP_LEVEL_FIELDS):
        bridge_row[mapping[tag]] = _encode_projection_value(document.get(tag))
    bridge_row.update(
        {
            "state_code": _encode_projection_value(state_code),
            "bridge_number": _encode_projection_value(bridge_number),
            "data_year": data_year,
        }
    )
    source_data = document.get("_SourceData", {})
    nbi_source = source_data.get("nbi") if isinstance(source_data, dict) else None
    if isinstance(nbi_source, dict):
        service_on = str(nbi_source.get("SERVICE_ON_042A") or "").strip()
        bridge_row["fhwa_omit"] = 1 if service_on in {"", "0", "2", "3", "9"} else 0
    else:
        bridge_row["fhwa_omit"] = 0

    existing = connection.execute(
        "SELECT 1 FROM bridges WHERE state_code=? AND bridge_number=? AND data_year=?",
        (state_code, bridge_number, data_year),
    ).fetchone()
    if existing:
        columns = [
            key for key in bridge_row if key not in {"state_code", "bridge_number", "data_year"}
        ]
        connection.execute(
            "UPDATE bridges SET " + ", ".join(f'"{key}"=?' for key in columns)
            + " WHERE state_code=? AND bridge_number=? AND data_year=?",
            tuple(bridge_row[key] for key in columns) + (state_code, bridge_number, data_year),
        )
    else:
        _insert_plain(connection.cursor(), "bridges", bridge_row)

    for table in (
        "routes", "features", "span_sets", "substructure_sets", "elements",
        "inspections", "posting_evaluations", "posting_statuses", "works",
    ):
        connection.execute(
            f'DELETE FROM "{table}" WHERE state_code=? AND bridge_number=? AND data_year=?',
            (state_code, bridge_number, data_year),
        )
    for table in ("bridge_field_metadata", "bridge_source_records"):
        connection.execute(
            f'DELETE FROM "{table}" WHERE state_code=? AND bridge_number=? AND data_year=?',
            (state_code, bridge_number, data_year),
        )

    cursor = connection.cursor()
    try:
        for feature in document.get("Features", []):
            _insert_plain(
                cursor,
                "features",
                _project_child_row(
                    "Features", feature, state_code=state_code,
                    bridge_number=bridge_number, data_year=data_year,
                ),
            )
            for route in feature.get("Routes", []):
                _insert_plain(
                    cursor,
                    "routes",
                    _project_child_row(
                        "Routes", route, state_code=state_code,
                        bridge_number=bridge_number, data_year=data_year,
                        feature_type=feature["BF01"],
                    ),
                )
        for section in ALL_NESTED_ARRAY_NAMES:
            if section == "Features":
                continue
            for child in document.get(section, []):
                _insert_plain(
                    cursor,
                    _LOAD_SECTION_TABLES[section],
                    _project_child_row(
                        section, child, state_code=state_code,
                        bridge_number=bridge_number, data_year=data_year,
                    ),
                )
    finally:
        cursor.close()


def publish_snapshot_revision(
    db_path: str, request: PublishSnapshotRevisionRequest
) -> PublishSnapshotRevisionResult:
    request_hash = canonical_sha256(asdict(request))
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        state = _workspace_row(connection, request.workspace_id)
        replay = _request_replay_metadata(
            connection,
            workspace_id=request.workspace_id,
            action="publish_revision",
            request_id=request.request_id,
            request_hash=request_hash,
        )
        if replay is not None:
            payload = dict(replay["result"])
            connection.rollback()
            return PublishSnapshotRevisionResult(**payload)
        if int(state["data_generation"]) != int(request.expected_data_generation):
            raise RevisionConflictError(409, "stale_data_generation", "Workspace data generation changed")
        revision = connection.execute(
            "SELECT * FROM snapshot_revisions WHERE workspace_id=? AND id=?",
            (request.workspace_id, request.revision_id),
        ).fetchone()
        if revision is None:
            raise RevisionNotFoundError(404, "revision_not_found", "Revision not found")
        logical = (
            request.workspace_id,
            revision["state_code"],
            revision["bridge_number"],
            revision["data_year"],
        )
        current = connection.execute(
            "SELECT * FROM published_snapshots WHERE workspace_id=? AND state_code=? "
            "AND bridge_number=? AND data_year=?",
            logical,
        ).fetchone()
        current_id = str(current["snapshot_revision_id"]) if current else None
        if current_id != request.expected_revision_id:
            raise RevisionConflictError(409, "stale_revision", "Published revision changed")
        validation = connection.execute(
            "SELECT status, canonical_sha256, summary_json FROM validation_runs "
            "WHERE workspace_id=? AND snapshot_revision_id=? "
            "ORDER BY completed_at DESC, id DESC LIMIT 1",
            (request.workspace_id, request.revision_id),
        ).fetchone()
        if validation is None:
            raise RevisionInputError(
                422, "validation_gate_failed", "Revision does not have a passing validation run"
            )
        summary = _loads(validation["summary_json"], {})
        if not isinstance(summary, Mapping) or summary.get("validation_owner") != "server":
            raise RevisionInputError(
                422,
                "untrusted_validation_evidence",
                "Revision validation was not produced by the server",
            )
        if str(validation["canonical_sha256"]) != str(revision["canonical_sha256"]):
            raise RevisionInputError(
                422,
                "validation_content_mismatch",
                "Revision validation does not match the immutable canonical content",
            )
        if str(validation["status"]).lower() not in _PASSING_VALIDATION_STATUSES:
            raise RevisionInputError(
                422, "validation_gate_failed", "Revision does not have a passing validation run"
            )
        collision = connection.execute(
            "SELECT 1 FROM published_snapshots WHERE workspace_id<>? AND state_code=? "
            "AND bridge_number=? AND data_year=? LIMIT 1",
            logical,
        ).fetchone()
        if collision is not None:
            raise RevisionConflictError(
                409,
                "projection_workspace_conflict",
                "This logical snapshot is already projected by another workspace",
            )

        document = json.loads(revision["canonical_json"])
        structural_errors = _preflight_snbi_batch([document])
        if structural_errors:
            raise RevisionInputError(
                422, "invalid_canonical_document", structural_errors[0]
            )
        _refresh_projection(connection, document, int(revision["data_year"]))
        published_at = _now()
        if current is None:
            connection.execute(
                "INSERT INTO published_snapshots "
                "(id, workspace_id, state_code, bridge_number, data_year, snapshot_revision_id, "
                "published_by, published_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _new_id(), request.workspace_id, revision["state_code"],
                    revision["bridge_number"], revision["data_year"], request.revision_id,
                    request.actor_id, published_at,
                ),
            )
        else:
            connection.execute(
                "UPDATE published_snapshots SET snapshot_revision_id=?, published_by=?, published_at=? "
                "WHERE id=? AND workspace_id=?",
                (request.revision_id, request.actor_id, published_at, current["id"], request.workspace_id),
            )
        new_generation = int(state["data_generation"]) + 1
        connection.execute(
            "UPDATE workspace_state SET data_generation=?, schema_generation=?, updated_at=? "
            "WHERE workspace_id=?",
            (new_generation, LATEST_SCHEMA_VERSION, published_at, request.workspace_id),
        )
        result = PublishSnapshotRevisionResult(
            workspace_id=request.workspace_id,
            revision_id=str(request.revision_id),
            previous_revision_id=current_id,
            state_code=int(revision["state_code"]),
            bridge_number=str(revision["bridge_number"]),
            data_year=int(revision["data_year"]),
            data_generation=new_generation,
            published_at=published_at,
        )
        _audit(
            connection,
            workspace_id=request.workspace_id,
            actor_id=request.actor_id,
            action="publish_revision",
            entity_id=str(request.revision_id),
            request_id=request.request_id,
            reason=request.reason,
            before_hash=None if current_id is None else connection.execute(
                "SELECT canonical_sha256 FROM snapshot_revisions WHERE workspace_id=? AND id=?",
                (request.workspace_id, current_id),
            ).fetchone()[0],
            after_hash=str(revision["canonical_sha256"]),
            metadata={
                "previous_revision_id": current_id,
                "data_generation": new_generation,
                "logical_key": [revision["state_code"], revision["bridge_number"], revision["data_year"]],
                "request_hash": request_hash,
                "result": asdict(result),
            },
        )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _remove_published_projection(
    connection: sqlite3.Connection,
    *,
    state_code: int,
    bridge_number: str,
    data_year: int,
) -> dict[str, int]:
    """Remove only the deterministic, rebuildable SNBI read projection.

    Report history, point-cloud generations, forecasts, RAG artifacts, source
    artifacts, canonical revisions, lineage, and validation evidence are not
    projection rows and are deliberately outside this allowlist.
    """

    parameters = (state_code, bridge_number, data_year)
    deleted: dict[str, int] = {}
    for table in SNBI_PROJECTION_CHILD_TABLES + SNBI_PROVENANCE_TABLES:
        cursor = connection.execute(
            f'DELETE FROM "{table}" '
            "WHERE state_code=? AND bridge_number=? AND data_year=?",
            parameters,
        )
        deleted[table] = max(int(cursor.rowcount), 0)
    cursor = connection.execute(
        "DELETE FROM bridges WHERE state_code=? AND bridge_number=? AND data_year=?",
        parameters,
    )
    deleted["bridges"] = max(int(cursor.rowcount), 0)
    return deleted


def archive_published_snapshot(
    db_path: str, request: ArchivePublishedSnapshotRequest
) -> ArchivePublishedSnapshotResult:
    """Unpublish exactly one active bridge/year revision without purging it."""

    workspace_id = _required_text(request.workspace_id, "workspace_id")
    bridge_number = _required_text(request.bridge_number, "bridge_number")
    revision_id = _required_text(request.revision_id, "revision_id")
    actor_id = _required_text(request.actor_id, "actor_id")
    request_id = _required_text(request.request_id, "request_id")
    reason = _required_text(request.reason, "reason")
    if (
        workspace_id != request.workspace_id
        or bridge_number != request.bridge_number
        or revision_id != request.revision_id
        or actor_id != request.actor_id
        or request_id != request.request_id
    ):
        raise RevisionInputError(
            422,
            "invalid_archive_identity",
            "Archive identity values must not contain surrounding whitespace",
        )
    if len(reason) > 2_000:
        raise RevisionInputError(422, "reason_too_long", "reason exceeds 2000 characters")
    try:
        state_code = int(request.state_code)
        data_year = int(request.data_year)
        expected_generation = int(request.expected_data_generation)
    except (TypeError, ValueError) as exc:
        raise RevisionInputError(
            422,
            "invalid_archive_precondition",
            "state_code, data_year, and expected_data_generation must be integers",
        ) from exc
    if expected_generation < 0:
        raise RevisionInputError(
            422,
            "invalid_archive_precondition",
            "expected_data_generation must be non-negative",
        )

    try:
        request_hash = canonical_sha256(asdict(request))
    except CanonicalJsonError as exc:
        raise RevisionInputError(422, "invalid_archive_request", str(exc)) from exc

    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        state = _workspace_row(connection, workspace_id)
        replay = _request_replay_metadata(
            connection,
            workspace_id=workspace_id,
            action="archive_revision",
            request_id=request_id,
            request_hash=request_hash,
        )
        if replay is not None:
            connection.rollback()
            return ArchivePublishedSnapshotResult(**dict(replay["result"]))

        if int(state["data_generation"]) != expected_generation:
            raise RevisionConflictError(
                409,
                "stale_data_generation",
                "Workspace data generation changed",
            )

        candidate = connection.execute(
            "SELECT * FROM snapshot_revisions WHERE workspace_id=? AND id=? "
            "AND state_code=? AND bridge_number=? AND data_year=?",
            (workspace_id, revision_id, state_code, bridge_number, data_year),
        ).fetchone()
        if candidate is None:
            # The same response covers absent, cross-workspace, and wrong-key
            # identifiers so a caller cannot use this mutation to enumerate
            # another workspace's revision IDs or logical keys.
            raise RevisionNotFoundError(404, "revision_not_found", "Revision not found")

        published = connection.execute(
            "SELECT * FROM published_snapshots WHERE workspace_id=? AND state_code=? "
            "AND bridge_number=? AND data_year=?",
            (workspace_id, state_code, bridge_number, data_year),
        ).fetchone()
        if published is None:
            raise RevisionConflictError(
                409,
                "snapshot_not_published",
                "The requested revision is not currently published",
            )
        current_revision_id = str(published["snapshot_revision_id"])
        if current_revision_id != revision_id:
            raise RevisionConflictError(
                409,
                "stale_revision",
                "Published revision changed",
                details={"current_revision_id": current_revision_id},
            )

        deleted_pointer = connection.execute(
            "DELETE FROM published_snapshots WHERE id=? AND workspace_id=? "
            "AND snapshot_revision_id=?",
            (published["id"], workspace_id, revision_id),
        )
        if int(deleted_pointer.rowcount) != 1:  # pragma: no cover - write lock invariant
            raise RevisionConflictError(
                409, "stale_revision", "Published revision changed"
            )
        projection_rows = _remove_published_projection(
            connection,
            state_code=state_code,
            bridge_number=bridge_number,
            data_year=data_year,
        )

        archived_at = _now()
        new_generation = int(state["data_generation"]) + 1
        connection.execute(
            "UPDATE workspace_state SET data_generation=?, schema_generation=?, updated_at=? "
            "WHERE workspace_id=?",
            (new_generation, LATEST_SCHEMA_VERSION, archived_at, workspace_id),
        )
        result = ArchivePublishedSnapshotResult(
            workspace_id=workspace_id,
            revision_id=revision_id,
            state_code=state_code,
            bridge_number=bridge_number,
            data_year=data_year,
            data_generation=new_generation,
            archived_at=archived_at,
        )
        _audit(
            connection,
            workspace_id=workspace_id,
            actor_id=actor_id,
            action="archive_revision",
            entity_id=revision_id,
            request_id=request_id,
            reason=reason,
            before_hash=str(candidate["canonical_sha256"]),
            after_hash=None,
            metadata={
                "logical_key": [state_code, bridge_number, data_year],
                "archived_revision_id": revision_id,
                "data_generation": new_generation,
                "projection_rows_removed": projection_rows,
                "retained": [
                    "snapshot_revision",
                    "source_evidence",
                    "field_lineage",
                    "validation_evidence",
                    "reports",
                    "point_clouds",
                    "forecasts",
                    "rag_artifacts",
                ],
                "request_hash": request_hash,
                "result": asdict(result),
            },
        )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _merge_patch(target: Any, patch: Any) -> Any:
    if not isinstance(patch, Mapping):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = _merge_patch(result.get(key), value)
    return result


def _pointer_overlaps(left: str, right: str) -> bool:
    return (
        left == right
        or left.startswith(right + "/")
        or right.startswith(left + "/")
    )


def _leaf_pointers(value: Any, pointer: str = "") -> tuple[str, ...]:
    """Return stable RFC-6901 pointers for every primitive/empty-container leaf."""

    if isinstance(value, dict):
        if not value:
            return (pointer,)
        result: list[str] = []
        for key in sorted(value):
            result.extend(
                _leaf_pointers(value[key], f"{pointer}/{_escape_pointer(str(key))}")
            )
        return tuple(result)
    if isinstance(value, list):
        if not value:
            return (pointer,)
        result = []
        for index, child in enumerate(value):
            result.extend(_leaf_pointers(child, f"{pointer}/{index}"))
        return tuple(result)
    return (pointer,)


def _assert_lineage_coverage(
    connection: sqlite3.Connection,
    workspace_id: str,
    revision_id: str,
    document: Mapping[str, Any],
) -> None:
    lineage_pointers = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT target_pointer FROM field_lineage WHERE workspace_id=? "
            "AND snapshot_revision_id=?",
            (workspace_id, revision_id),
        ).fetchall()
    )
    uncovered = [
        leaf
        for leaf in _leaf_pointers(document)
        if not any(
            candidate == leaf or leaf.startswith(candidate + "/")
            for candidate in lineage_pointers
        )
    ]
    if uncovered:
        raise RevisionStoreError(
            500,
            "lineage_coverage_incomplete",
            "Patched canonical revision has incomplete field lineage",
            details={"uncovered_pointers": uncovered[:50]},
        )


def patch_snapshot_revision(
    db_path: str, request: PatchSnapshotRevisionRequest
) -> PatchSnapshotRevisionResult:
    try:
        canonical_sha256(request.patch)
        request_hash = canonical_sha256(asdict(request))
    except CanonicalJsonError as exc:
        raise RevisionInputError(422, "invalid_merge_patch", str(exc)) from exc
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        state = _workspace_row(connection, request.workspace_id)
        replay = _request_replay_metadata(
            connection,
            workspace_id=request.workspace_id,
            action="patch_revision",
            request_id=request.request_id,
            request_hash=request_hash,
        )
        if replay is not None:
            payload = dict(replay["result"])
            payload["diff"] = tuple(payload.get("diff") or ())
            connection.rollback()
            return PatchSnapshotRevisionResult(**payload)
        generation = int(state["data_generation"])
        if generation != int(request.expected_data_generation):
            raise RevisionConflictError(409, "stale_data_generation", "Workspace data generation changed")
        published = connection.execute(
            "SELECT ps.snapshot_revision_id, sr.* FROM published_snapshots ps "
            "JOIN snapshot_revisions sr ON sr.workspace_id=ps.workspace_id "
            "AND sr.id=ps.snapshot_revision_id WHERE ps.workspace_id=? AND ps.state_code=? "
            "AND ps.bridge_number=? AND ps.data_year=?",
            (request.workspace_id, request.state_code, request.bridge_number, request.data_year),
        ).fetchone()
        if published is None:
            raise RevisionNotFoundError(404, "published_snapshot_not_found", "Published snapshot not found")
        if str(published["snapshot_revision_id"]) != str(request.expected_revision_id):
            raise RevisionConflictError(409, "stale_revision", "Published revision changed")
        try:
            patched_rule_bundle = build_rule_bundle_manifest(
                str(published["profile_id"])
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise RevisionInputError(
                422,
                "rule_bundle_manifest_unavailable",
                "The revision profile could not be bound to an immutable rule bundle",
            ) from exc
        before = json.loads(published["canonical_json"])
        after = _merge_patch(before, request.patch)
        if not isinstance(after, dict):
            raise RevisionInputError(422, "invalid_canonical_document", "Patch result must be an object")
        after = _normalize_for_store(after, str(published["profile_id"]))
        if after.get("BL01") != request.state_code or str(after.get("BID01", "")) != request.bridge_number:
            raise RevisionInputError(
                422, "logical_key_mismatch", "A patch cannot change BL01 or BID01"
            )
        structural_errors = _preflight_snbi_batch([after])
        if structural_errors:
            raise RevisionInputError(
                422,
                "invalid_canonical_document",
                structural_errors[0],
                details={"issues": structural_errors[:50]},
            )
        canonical_text = _json(after)
        content_hash = canonical_sha256(after)
        difference = _diff(before, after)
        # Validation is deliberately checked before the identical-content
        # return below.  Otherwise a caller could target an already-staged
        # candidate and bypass the requirement that every patch operation be
        # evaluated from its freshly merged canonical document by the server.
        validation = dict(request.validation or {})
        if validation.get("validation_owner") != "server" or not validation.get("status"):
            raise RevisionInputError(
                422,
                "validation_evidence_required",
                "A fresh server-owned validation result is required for a patched revision",
            )
        latest = connection.execute(
            "SELECT * FROM snapshot_revisions WHERE workspace_id=? AND state_code=? "
            "AND bridge_number=? AND data_year=? ORDER BY revision_number DESC LIMIT 1",
            (request.workspace_id, request.state_code, request.bridge_number, request.data_year),
        ).fetchone()
        if (
            latest is not None
            and latest["canonical_sha256"] == content_hash
            and str(latest["profile_id"]) == str(published["profile_id"])
            and str(latest["rule_bundle_sha256"] or "")
            == patched_rule_bundle.sha256
        ):
            connection.rollback()
            return PatchSnapshotRevisionResult(
                workspace_id=request.workspace_id,
                revision_id=str(latest["id"]),
                previous_revision_id=str(request.expected_revision_id),
                state_code=request.state_code,
                bridge_number=request.bridge_number,
                data_year=request.data_year,
                revision_number=int(latest["revision_number"]),
                content_hash=content_hash,
                data_generation=generation,
                diff=difference,
                created_at=str(latest["created_at"]),
            )

        revision_id = _new_id()
        created_at = _now()
        revision_number = int(latest["revision_number"]) + 1 if latest else 1
        source_links = connection.execute(
            "SELECT source_record_id, source_ordinal, source_role FROM snapshot_revision_sources "
            "WHERE workspace_id=? AND snapshot_revision_id=? ORDER BY source_ordinal",
            (request.workspace_id, request.expected_revision_id),
        ).fetchall()
        connection.execute(
            "INSERT INTO snapshot_revisions "
            "(id, workspace_id, conversion_batch_id, source_record_id, state_code, bridge_number, "
            "data_year, revision_number, lifecycle_state, canonical_json, canonical_sha256, "
            "parent_revision_id, profile_id, rule_bundle_sha256, created_by, created_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, 'staged', ?, ?, ?, ?, ?, ?, ?)",
            (
                revision_id,
                request.workspace_id,
                source_links[0]["source_record_id"] if source_links else None,
                request.state_code,
                request.bridge_number,
                request.data_year,
                revision_number,
                canonical_text,
                content_hash,
                request.expected_revision_id,
                published["profile_id"],
                patched_rule_bundle.sha256,
                request.actor_id,
                created_at,
            ),
        )
        for link in source_links:
            connection.execute(
                "INSERT INTO snapshot_revision_sources "
                "(id, workspace_id, snapshot_revision_id, source_record_id, source_ordinal, source_role) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    _new_id(), request.workspace_id, revision_id, link["source_record_id"],
                    link["source_ordinal"], link["source_role"],
                ),
            )
        # Carry unchanged lineage forward exactly.  Any lineage whose target
        # overlaps a changed pointer is replaced by explicit editor evidence.
        parent_lineage = connection.execute(
            "SELECT * FROM field_lineage WHERE workspace_id=? AND snapshot_revision_id=? "
            "ORDER BY target_pointer, lineage_ordinal",
            (request.workspace_id, request.expected_revision_id),
        ).fetchall()
        covered_pointers: set[str] = set()
        changed_pointers = tuple(change["path"] for change in difference)
        for inherited in parent_lineage:
            target = str(inherited["target_pointer"])
            if any(_pointer_overlaps(target, changed) for changed in changed_pointers):
                continue
            connection.execute(
                "INSERT INTO field_lineage "
                "(id, workspace_id, snapshot_revision_id, target_pointer, lineage_ordinal, "
                "source_record_id, source_pointer, rule_id, resolution_status, value_json, "
                "evidence_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _new_id(), request.workspace_id, revision_id, target,
                    inherited["lineage_ordinal"], inherited["source_record_id"],
                    inherited["source_pointer"], inherited["rule_id"],
                    inherited["resolution_status"], inherited["value_json"],
                    inherited["evidence_json"], created_at,
                ),
            )
            covered_pointers.add(target)

        # A patch is explicit editor lineage.  It intentionally does not claim
        # that edited fields were re-derived from an original source file.
        for ordinal, change in enumerate(difference):
            present, changed_value = _json_pointer_get(after, change["path"])
            connection.execute(
                "INSERT INTO field_lineage "
                "(id, workspace_id, snapshot_revision_id, target_pointer, lineage_ordinal, "
                "source_record_id, source_pointer, rule_id, resolution_status, value_json, "
                "evidence_json, created_at) VALUES (?, ?, ?, ?, ?, NULL, NULL, "
                "'RFC7396_PATCH', 'editor_override', ?, ?, ?)",
                (
                    _new_id(), request.workspace_id, revision_id, change["path"], ordinal,
                    _json(changed_value) if present else None,
                    _json({"request_id": request.request_id, "change": change}),
                    _now(),
                ),
            )
            covered_pointers.add(change["path"])

        # Historical fixtures can predate complete field-level lineage.  Fill
        # uncovered leaves with an explicit, honest source-evidence fallback;
        # this is not represented as a direct transition rule.
        fallback_source = source_links[0]["source_record_id"] if source_links else None
        fallback_ordinal = 0
        for leaf in _leaf_pointers(after):
            if any(
                candidate == leaf or leaf.startswith(candidate + "/")
                for candidate in covered_pointers
            ):
                continue
            present, leaf_value = _json_pointer_get(after, leaf)
            connection.execute(
                "INSERT INTO field_lineage "
                "(id, workspace_id, snapshot_revision_id, target_pointer, lineage_ordinal, "
                "source_record_id, source_pointer, rule_id, resolution_status, value_json, "
                "evidence_json, created_at) VALUES (?, ?, ?, ?, ?, ?, NULL, "
                "'BRIDGEGUARD_SOURCE_COVERAGE', 'inherited_source_evidence', ?, ?, ?)",
                (
                    _new_id(), request.workspace_id, revision_id, leaf,
                    fallback_ordinal, fallback_source,
                    _json(leaf_value) if present else None,
                    _json(
                        {
                            "kind": "lineage_coverage_fallback",
                            "source_revision_id": request.expected_revision_id,
                            "note": "No field-level rule was present on the source revision",
                        }
                    ),
                    created_at,
                ),
            )
            covered_pointers.add(leaf)
            fallback_ordinal += 1
        _assert_lineage_coverage(
            connection, request.workspace_id, revision_id, after
        )

        # The caller must provide a fresh server-owned validation result for
        # the patched canonical document.  Inheriting the parent verdict would
        # make the evidence refer to the wrong content hash.
        parent_validation = connection.execute(
            "SELECT * FROM validation_runs WHERE workspace_id=? AND snapshot_revision_id=? "
            "ORDER BY completed_at DESC, id DESC LIMIT 1",
            (request.workspace_id, request.expected_revision_id),
        ).fetchone()
        if parent_validation is None:
            raise RevisionInputError(
                422,
                "validation_evidence_required",
                "The published source revision has no validation evidence",
            )
        validation_id = _new_id()
        issues = list(validation.get("issues") or ())
        validation_summary = {
            key: value for key, value in validation.items() if key != "issues"
        }
        validation_summary["patch_integrity"] = {
            "status": "passed",
            "scope": "bridgeguard_shape_lineage_only",
            "changed_pointer_count": len(difference),
        }
        validation_summary["rule_bundle_manifest"] = patched_rule_bundle.manifest
        validation_summary["rule_bundle_sha256"] = patched_rule_bundle.sha256
        validation_summary["parent_rule_bundle_sha256"] = str(
            published["rule_bundle_sha256"] or ""
        )
        connection.execute(
            "INSERT INTO validation_runs "
            "(id, workspace_id, snapshot_revision_id, profile_id, rule_bundle_id, "
            "rule_bundle_sha256, validator_version, status, canonical_sha256, summary_json, "
            "started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                validation_id,
                request.workspace_id,
                revision_id,
                str(validation.get("profile_id") or published["profile_id"]),
                patched_rule_bundle.bundle_id,
                patched_rule_bundle.sha256,
                str(validation.get("validator_version") or "revision-store-v1"),
                str(validation["status"]),
                content_hash,
                _json(validation_summary),
                created_at, created_at,
            ),
        )
        for ordinal, issue_value in enumerate(issues):
            issue = dict(issue_value)
            connection.execute(
                "INSERT INTO validation_issues "
                "(id, workspace_id, validation_run_id, issue_ordinal, severity, code, "
                "json_pointer, message, evidence_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _new_id(), request.workspace_id, validation_id, ordinal,
                    str(issue.get("severity") or "error"),
                    str(issue.get("rule_id") or issue.get("code") or "validation_issue"),
                    str(issue.get("json_pointer") or ""),
                    str(issue.get("message") or "Validation issue"),
                    _json(issue),
                    created_at,
                ),
            )
        connection.execute(
            "INSERT INTO validation_issues "
            "(id, workspace_id, validation_run_id, issue_ordinal, severity, code, "
            "json_pointer, message, evidence_json, created_at) "
            "VALUES (?, ?, ?, ?, 'info', 'BG-PATCH-INTEGRITY', '', ?, ?, ?)",
            (
                _new_id(), request.workspace_id, validation_id, len(issues),
                "BridgeGuard canonical shape and lineage coverage checks passed after merge patch",
                _json({"scope": "bridgeguard_only", "changed_pointers": changed_pointers}),
                created_at,
            ),
        )
        result = PatchSnapshotRevisionResult(
            workspace_id=request.workspace_id,
            revision_id=revision_id,
            previous_revision_id=str(request.expected_revision_id),
            state_code=request.state_code,
            bridge_number=request.bridge_number,
            data_year=request.data_year,
            revision_number=revision_number,
            content_hash=content_hash,
            data_generation=generation,
            diff=difference,
            created_at=created_at,
        )
        _audit(
            connection,
            workspace_id=request.workspace_id,
            actor_id=request.actor_id,
            action="patch_revision",
            entity_id=revision_id,
            request_id=request.request_id,
            reason=request.reason,
            before_hash=str(published["canonical_sha256"]),
            after_hash=content_hash,
            metadata={
                "previous_revision_id": request.expected_revision_id,
                "diff": difference,
                "request_hash": request_hash,
                "result": asdict(result),
            },
        )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


__all__ = [
    "ArchivePublishedSnapshotRequest",
    "ArchivePublishedSnapshotResult",
    "CanonicalRevisionInput",
    "IdempotencyConflictError",
    "MutationAuditRecord",
    "PatchSnapshotRevisionRequest",
    "PatchSnapshotRevisionResult",
    "PublishedSnapshotPage",
    "PublishedSnapshotSummary",
    "PublishSnapshotRevisionRequest",
    "PublishSnapshotRevisionResult",
    "RevisionConflictError",
    "RevisionInputError",
    "RevisionNotFoundError",
    "RevisionStoreError",
    "SnapshotRevisionRecord",
    "StageCanonicalBatchRequest",
    "StageCanonicalBatchResult",
    "StagedRevisionResult",
    "WorkspaceBoundaryError",
    "WorkspaceState",
    "archive_published_snapshot",
    "ensure_workspace",
    "get_published_snapshot",
    "list_published_snapshots",
    "get_snapshot_history",
    "get_snapshot_revision",
    "get_workspace_state",
    "initialize_revision_store",
    "list_mutation_audit",
    "patch_snapshot_revision",
    "publish_snapshot_revision",
    "stage_canonical_batch",
]
