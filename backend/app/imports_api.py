"""Restart-safe, two-phase canonical import API.

The request thread accepts and hashes source bytes, then enqueues a durable
preflight.  The one supported worker parses, normalizes, and validates the
source into a second immutable artifact.  Commit re-verifies both receipts and
stages canonical revisions under optimistic generation and idempotency gates;
it never publishes a snapshot.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import io
import json
import re
import sqlite3
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app import revision_api
from app.routers.services.database import _projection_schema_errors
from app.services.conversion.canonical_json import (
    CanonicalJsonError,
    CanonicalJsonLimits,
    canonical_sha256,
    parse_canonical_json,
)
from app.services.conversion.canonical_schema import (
    CanonicalResourceLimitError,
    normalize_canonical_document,
)
from app.services.conversion.revision_store import (
    RevisionInputError,
    RevisionStoreError,
    get_workspace_state,
)
from app.services.conversion.revision_validation import derive_server_validation
from app.services.conversion.source_artifacts import (
    ArtifactErrorCode,
    SourceArtifactError,
    SourceArtifactStore,
)
from app.services.operations.jobs import (
    JobStoreError,
    OperationJob,
    cancel_job,
    enqueue_job,
    get_job,
    get_job_by_idempotency,
)


router = APIRouter(prefix="/api/imports", tags=["canonical-imports"])

IMPORT_PREFLIGHT_OPERATION = "import_preflight"
MAX_IMPORT_BYTES = revision_api.MAX_CONVERSION_BYTES
MAX_IMPORT_RECORDS = revision_api.MAX_UPLOAD_RECORDS
MAX_PREFLIGHT_BYTES = 64 * 1024 * 1024
MAX_PREFLIGHT_DIFF_CHANGES = 100
SUPPORTED_PROFILES = frozenset({"research_historical_import", "native_snbi"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_INGEST_LOCK = threading.Lock()
_IMPORT_MUTATION_LOCK = threading.Lock()


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImportCommitBody(_StrictModel):
    preflight_digest: str = Field(min_length=64, max_length=64)
    expected_data_generation: int = Field(ge=0)
    action: Literal["create_only", "replace_snapshot"]
    validation_policy: Literal["advisory", "reject_critical"]
    atomic_mode: Literal["batch", "per_bridge"]
    override_reason: str | None = Field(default=None, max_length=2_000)


class ImportApiError(RuntimeError):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)
        self.details = dict(details or {})
        super().__init__(message)


def _error(exc: ImportApiError) -> JSONResponse:
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.details:
        detail["details"] = exc.details
    return JSONResponse(status_code=exc.status, content={"detail": detail})


def _actor(request: Request) -> str:
    user = getattr(request.state, "user", None)
    email = str(getattr(user, "email", "") or "").strip().lower()
    if not email:
        raise ImportApiError(401, "actor_required", "Authenticated actor is required")
    return email


def _role(request: Request) -> str:
    return str(getattr(getattr(request.state, "user", None), "role", "") or "")


def _header(value: str | None, *, code: str, label: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ImportApiError(400, code, f"{label} is required")
    if len(clean) > 256 or "\r" in clean or "\n" in clean:
        raise ImportApiError(400, f"invalid_{code}", f"{label} is invalid")
    return clean


def _database_path() -> str:
    return revision_api._database_path()


def _job_db() -> Path:
    return Path(_database_path()).parent / "operation_jobs.db"


def _source_store() -> SourceArtifactStore:
    return SourceArtifactStore(
        revision_api._artifact_root(), max_bytes=MAX_IMPORT_BYTES
    )


def _preflight_store() -> SourceArtifactStore:
    return SourceArtifactStore(
        revision_api._artifact_root() / "import_preflights",
        max_bytes=MAX_PREFLIGHT_BYTES,
    )


def _public_source(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": str(value.get("artifact_id") or ""),
        "sha256": str(value.get("sha256") or ""),
        "size_bytes": int(value.get("size_bytes") or 0),
        "original_filename": str(value.get("original_filename") or ""),
        "created_at": str(value.get("created_at") or ""),
        "status": str(value.get("status") or "accepted"),
    }


def _public_job(job: OperationJob) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "import_id": job.id,
        "status": job.status,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "progress": job.progress,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "source": _public_source(job.payload.get("source") or {}),
    }
    if job.error_code:
        payload["error_code"] = job.error_code
    if isinstance(job.result, Mapping):
        payload.update(
            {
                "preflight_digest": job.result.get("preflight_digest"),
                "preflight_receipt": job.result.get("preflight_receipt"),
                "summary": job.result.get("summary"),
            }
        )
    return payload


def _read_upload_identity(upload: UploadFile) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    upload.file.seek(0)
    while True:
        chunk = upload.file.read(256 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_IMPORT_BYTES:
            upload.file.seek(0)
            raise ImportApiError(
                413,
                "upload_too_large",
                "The upload exceeds the configured byte limit.",
                details={"max_bytes": MAX_IMPORT_BYTES},
            )
        digest.update(chunk)
    upload.file.seek(0)
    if size == 0:
        raise ImportApiError(400, "empty_upload", "The upload contains no bytes.")
    return digest.hexdigest(), size


def _submission_identity(
    *,
    sha256: str,
    size_bytes: int,
    filename: str,
    inventory_year: int,
    state_code: int,
    agency_id: str,
    profile_id: str,
) -> dict[str, Any]:
    return {
        "sha256": sha256,
        "size_bytes": int(size_bytes),
        "original_filename": filename,
        "inventory_year": int(inventory_year),
        "state_code": int(state_code),
        "agency_id": agency_id,
        "profile_id": profile_id,
    }


def _assert_replay_identity(job: OperationJob, expected: Mapping[str, Any]) -> None:
    actual = job.payload.get("submission")
    if not isinstance(actual, Mapping) or any(
        actual.get(key) != value for key, value in expected.items()
    ):
        raise ImportApiError(
            409,
            "idempotency_payload_conflict",
            "The idempotency key was already used for a different import.",
        )


def _queue_import(
    *,
    owner_id: str,
    idempotency_key: str,
    upload: UploadFile,
    inventory_year: int,
    state_code: int,
    agency_id: str,
    profile_id: str,
) -> tuple[OperationJob, bool]:
    if profile_id not in SUPPORTED_PROFILES:
        raise ImportApiError(
            422,
            "unsupported_profile",
            "Only research_historical_import and native_snbi imports are supported.",
        )
    if not 1990 <= int(inventory_year) <= 2100:
        raise ImportApiError(
            422,
            "invalid_inventory_year",
            "inventory_year must be between 1990 and 2100.",
        )
    if not 1 <= int(state_code) <= 99:
        raise ImportApiError(422, "invalid_state_code", "state_code must be 1 through 99.")
    agency = str(agency_id or "").strip()
    if not agency or len(agency) > 128:
        raise ImportApiError(422, "invalid_agency_id", "agency_id is required.")

    safe_filename = SourceArtifactStore.sanitize_original_filename(
        upload.filename or "bridge-import.json"
    )
    sha256, size_bytes = _read_upload_identity(upload)
    submission = _submission_identity(
        sha256=sha256,
        size_bytes=size_bytes,
        filename=safe_filename,
        inventory_year=inventory_year,
        state_code=state_code,
        agency_id=agency,
        profile_id=profile_id,
    )

    # The supported deployment has one API process.  This lock also prevents
    # two simultaneous idempotent replays from creating unreferenced manifests
    # before SQLite can establish the unique durable job identity.
    with _INGEST_LOCK:
        try:
            existing = get_job_by_idempotency(
                _job_db(),
                workspace_id=revision_api.SHARED_WORKSPACE_ID,
                owner_id=owner_id,
                operation=IMPORT_PREFLIGHT_OPERATION,
                idempotency_key=idempotency_key,
            )
        except JobStoreError as exc:
            if exc.code != "job_not_found":
                raise
        else:
            _assert_replay_identity(existing, submission)
            return existing, True

        receipt = _source_store().ingest(
            upload.file, original_filename=safe_filename
        )
        if receipt.sha256 != sha256 or receipt.size_bytes != size_bytes:
            raise ImportApiError(
                409, "upload_changed", "The upload changed while it was being stored."
            )
        job = enqueue_job(
            _job_db(),
            workspace_id=revision_api.SHARED_WORKSPACE_ID,
            owner_id=owner_id,
            operation=IMPORT_PREFLIGHT_OPERATION,
            idempotency_key=idempotency_key,
            payload={"source": receipt.to_public_dict(), "submission": submission},
            max_attempts=3,
        )
        return job, False


@router.post("", status_code=202)
async def create_import(
    request: Request,
    file: UploadFile = File(...),
    inventory_year: int = Form(...),
    state_code: int = Form(...),
    agency_id: str = Form(...),
    profile_id: str = Form("native_snbi"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Accept exact bytes and queue a durable preflight; perform no revision write."""

    try:
        key = _header(
            idempotency_key,
            code="idempotency_key_required",
            label="Idempotency-Key",
        )
        job, replay = await run_in_threadpool(
            _queue_import,
            owner_id=_actor(request),
            idempotency_key=key,
            upload=file,
            inventory_year=inventory_year,
            state_code=state_code,
            agency_id=agency_id,
            profile_id=str(profile_id).strip(),
        )
        payload = _public_job(job)
        payload["idempotent_replay"] = replay
        return JSONResponse(status_code=202, content=payload)
    except ImportApiError as exc:
        return _error(exc)
    except SourceArtifactError as exc:
        status = 413 if exc.code == ArtifactErrorCode.UPLOAD_TOO_LARGE.value else 400
        return _error(ImportApiError(status, exc.code, str(exc), details=exc.details))
    except JobStoreError as exc:
        return _error(ImportApiError(exc.status, exc.code, "The import could not be queued."))


def _diagnostic(value: Any, *, prefix: str = "") -> dict[str, Any]:
    raw = asdict(value)
    pointer = str(raw.get("path") or raw.get("duplicate_path") or "")
    if prefix:
        pointer = f"{prefix}{pointer}" if pointer else prefix
    code = value.__class__.__name__
    return {
        "code": re.sub(r"(?<!^)(?=[A-Z])", "_", code).lower(),
        "json_pointer": pointer,
        "message": str(raw.get("message") or code),
        "severity": str(getattr(raw.get("severity"), "value", raw.get("severity", "error"))),
    }


def _parse_envelope(
    raw: bytes,
    submission: Mapping[str, Any],
    *,
    source_artifact_id: str,
    idempotency_key: str,
) -> tuple[list[Any], list[dict[str, Any]]]:
    parsed = parse_canonical_json(
        raw,
        CanonicalJsonLimits(
            max_depth=64,
            max_nodes=1_000_000,
            max_bytes=MAX_IMPORT_BYTES,
        ),
    )
    conflicts: list[dict[str, Any]] = []
    if isinstance(parsed, Mapping) and "bridges" in parsed:
        unknown_root = set(parsed) - {"manifest", "bridges"}
        if unknown_root:
            raise ImportApiError(
                422,
                "unknown_envelope_field",
                "The import envelope contains unknown fields.",
                details={"json_pointers": [f"/{key}" for key in sorted(unknown_root)]},
            )
        manifest = parsed.get("manifest")
        if manifest is not None and not isinstance(manifest, Mapping):
            raise ImportApiError(422, "invalid_manifest", "manifest must be an object.")
        embedded = dict(manifest or {})
        allowed_manifest = {
            "profile_id", "inventory_year", "state_code", "agency_id",
            "source_artifact_id", "idempotency_key",
        }
        unknown_manifest = set(embedded) - allowed_manifest
        if unknown_manifest:
            raise ImportApiError(
                422,
                "unknown_manifest_field",
                "The import manifest contains unknown fields.",
                details={
                    "json_pointers": [f"/manifest/{key}" for key in sorted(unknown_manifest)]
                },
            )
        comparisons = {
            "profile_id": submission["profile_id"],
            "inventory_year": submission["inventory_year"],
            "state_code": submission["state_code"],
            "agency_id": submission["agency_id"],
            "source_artifact_id": source_artifact_id,
            "idempotency_key": idempotency_key,
        }
        for key, expected in comparisons.items():
            if key in embedded and embedded[key] != expected:
                conflicts.append(
                    {
                        "code": "manifest_metadata_conflict",
                        "json_pointer": f"/manifest/{key}",
                        "embedded": embedded[key],
                        "authoritative": expected,
                    }
                )
        bridges = parsed.get("bridges")
    elif isinstance(parsed, list):
        bridges = parsed
    elif isinstance(parsed, Mapping):
        bridges = [parsed]
    else:
        raise ImportApiError(
            422, "bridge_array_required", "The import must contain bridge objects."
        )
    if not isinstance(bridges, list) or not bridges:
        raise ImportApiError(422, "bridge_array_required", "bridges must be a non-empty array.")
    if len(bridges) > MAX_IMPORT_RECORDS:
        raise ImportApiError(
            413,
            "record_limit_exceeded",
            f"An import may contain at most {MAX_IMPORT_RECORDS} bridges.",
        )
    return list(bridges), conflicts


def _latest_hashes(database_path: str) -> dict[tuple[int, str, int], str]:
    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute(
            "SELECT sr.state_code,sr.bridge_number,sr.data_year,sr.canonical_sha256 "
            "FROM snapshot_revisions sr JOIN ("
            "SELECT workspace_id,state_code,bridge_number,data_year,MAX(revision_number) AS revision_number "
            "FROM snapshot_revisions WHERE workspace_id=? "
            "GROUP BY workspace_id,state_code,bridge_number,data_year"
            ") latest ON latest.workspace_id=sr.workspace_id "
            "AND latest.state_code=sr.state_code AND latest.bridge_number=sr.bridge_number "
            "AND latest.data_year=sr.data_year AND latest.revision_number=sr.revision_number "
            "WHERE sr.workspace_id=?",
            (revision_api.SHARED_WORKSPACE_ID, revision_api.SHARED_WORKSPACE_ID),
        ).fetchall()
    finally:
        connection.close()
    return {(int(row[0]), str(row[1]), int(row[2])): str(row[3]) for row in rows}


def _existing_inventory_keys(database_path: str) -> set[tuple[int, str, int]]:
    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute(
            "SELECT state_code,bridge_number,data_year FROM bridges"
        ).fetchall()
    finally:
        connection.close()
    return {(int(row[0]), str(row[1]), int(row[2])) for row in rows}


def _latest_document(
    connection: sqlite3.Connection,
    key: tuple[int, str, int],
    expected_hash: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT canonical_json FROM snapshot_revisions "
        "WHERE workspace_id=? AND state_code=? AND bridge_number=? AND data_year=? "
        "AND canonical_sha256=? ORDER BY revision_number DESC LIMIT 1",
        (revision_api.SHARED_WORKSPACE_ID, *key, expected_hash),
    ).fetchone()
    if row is None:
        return None
    parsed = json.loads(str(row[0]))
    return parsed if isinstance(parsed, dict) else None


def _bounded_diff(before: Any, after: Any) -> dict[str, Any]:
    """Return deterministic, path-only review evidence with a strict output cap."""

    changes: list[dict[str, str]] = []
    change_count = 0

    def record(operation: str, path: str) -> None:
        nonlocal change_count
        change_count += 1
        if len(changes) < MAX_PREFLIGHT_DIFF_CHANGES:
            changes.append({"op": operation, "path": path or "/"})

    def escape(token: str) -> str:
        return token.replace("~", "~0").replace("/", "~1")

    def visit(left: Any, right: Any, path: str = "") -> None:
        if type(left) is not type(right):
            record("replace", path)
            return
        if isinstance(left, dict):
            for key in sorted(set(left) | set(right)):
                child_path = f"{path}/{escape(str(key))}"
                if key not in left:
                    record("add", child_path)
                elif key not in right:
                    record("remove", child_path)
                else:
                    visit(left[key], right[key], child_path)
            return
        if isinstance(left, list):
            shared = min(len(left), len(right))
            for index in range(shared):
                visit(left[index], right[index], f"{path}/{index}")
            for index in range(shared, len(left)):
                record("remove", f"{path}/{index}")
            for index in range(shared, len(right)):
                record("add", f"{path}/{index}")
            return
        if left != right:
            record("replace", path)

    visit(before, after)
    changes.sort(key=lambda change: (change["path"], change["op"]))
    return {
        "change_count": change_count,
        "changes": changes,
        "truncated": change_count > len(changes),
    }


def _projection_field_loss_check(
    database_path: str,
    *,
    summaries: list[dict[str, Any]],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Read-only coverage check against the publication projection schema.

    Canonical normalization already rejects unknown official and child fields.
    This companion check proves that the deployed relational schema contains
    every column/table expected by the shared projection mapping.  Namespaced
    extensions remain losslessly canonical but are intentionally not flattened
    into relational columns.
    """

    connection = sqlite3.connect(
        f"file:{Path(database_path).resolve().as_posix()}?mode=ro", uri=True
    )
    try:
        connection.execute("PRAGMA query_only=ON")
        cursor = connection.cursor()
        try:
            schema_errors = list(_projection_schema_errors(cursor))
        finally:
            cursor.close()
    finally:
        connection.close()
    unknown_fields = sum(
        sum(issue.get("code") == "unknown_tag" for issue in item.get("issues") or [])
        for item in summaries
    )
    extension_documents = sum(
        isinstance(item.get("canonical_document", {}).get("extensions"), Mapping)
        and bool(item["canonical_document"]["extensions"])
        for item in items
    )
    return {
        "status": "passed" if not schema_errors and unknown_fields == 0 else "failed",
        "mode": "canonical_and_projection_schema_coverage",
        "unknown_fields": unknown_fields,
        "projection_schema_errors": schema_errors[:100],
        "canonical_is_authoritative": True,
        "extension_documents_retained_canonical_only": extension_documents,
    }


def _preflight_document(
    value: Any,
    *,
    index: int,
    submission: Mapping[str, Any],
    source: Mapping[str, Any],
    seen: set[tuple[int, str, int]],
    latest: Mapping[tuple[int, str, int], str],
    existing_inventory: set[tuple[int, str, int]],
    latest_connection: sqlite3.Connection,
    artifact_store: SourceArtifactStore,
    artifact_receipts: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    prefix = f"/bridges/{index}"
    summary: dict[str, Any] = {
        "index": index,
        "logical_key": None,
        "valid": False,
        "projected_action": "invalid",
        "validation_status": None,
        "base_content_hash": None,
        "diff": {"change_count": 0, "changes": [], "truncated": False},
        "eligibility": {
            "status": "unknown",
            "retained_by_profile": None,
            "profile_id": None,
        },
        "issue_counts": {"error": 0, "warning": 0},
        "issues": [],
    }
    if not isinstance(value, Mapping):
        summary["issues"] = [{
            "code": "bridge_object_required",
            "json_pointer": prefix,
            "message": "Each bridge entry must be an object.",
            "severity": "error",
        }]
        summary["issue_counts"]["error"] = 1
        return summary, None

    canonical_value = {
        key: child
        for key, child in value.items()
        if not str(key).startswith("_")
    }
    try:
        normalized = normalize_canonical_document(
            canonical_value, profile_id=str(submission["profile_id"])
        )
    except CanonicalResourceLimitError as exc:
        summary["issues"] = [{
            "code": "canonical_resource_limit",
            "json_pointer": prefix,
            "message": str(exc),
            "severity": "error",
        }]
        summary["issue_counts"]["error"] = 1
        return summary, None
    diagnostics = (
        tuple(normalized.unknown_tags)
        + tuple(normalized.wrong_container_types)
        + tuple(normalized.rejected_values)
        + tuple(normalized.duplicate_logical_child_keys)
    )
    issues = [_diagnostic(item, prefix=prefix) for item in diagnostics]
    document = normalized.normalized_document
    state = document.get("BL01")
    bridge = str(document.get("BID01") or "").strip()
    try:
        state_int = int(state)
    except (TypeError, ValueError):
        state_int = -1
    key = (state_int, bridge, int(submission["inventory_year"]))
    summary["logical_key"] = {
        "state_code": state_int if state_int >= 0 else None,
        "bridge_number": bridge or None,
        "data_year": int(submission["inventory_year"]),
    }
    if state_int < 1 or not bridge:
        issues.append({
            "code": "logical_key_required",
            "json_pointer": prefix,
            "message": "BL01 and BID01 are required.",
            "severity": "error",
        })
    elif state_int != int(submission["state_code"]):
        issues.append({
            "code": "state_code_mismatch",
            "json_pointer": f"{prefix}/BL01",
            "message": "BL01 does not match the server-authoritative import state.",
            "severity": "error",
        })
    if key in seen:
        issues.append({
            "code": "duplicate_logical_snapshot",
            "json_pointer": prefix,
            "message": "The import contains the same state/bridge/year more than once.",
            "severity": "error",
        })
    seen.add(key)
    if issues:
        summary["issues"] = issues[:100]
        summary["issue_counts"]["error"] = len(issues)
        return summary, None

    source_data = value.get("_SourceData")
    eligibility = source_data.get("eligibility") if isinstance(source_data, Mapping) else None
    if isinstance(eligibility, Mapping):
        federally_eligible = eligibility.get("has_federally_eligible_record")
        if isinstance(federally_eligible, bool):
            summary["eligibility"] = {
                "status": "eligible" if federally_eligible else "excluded",
                "retained_by_profile": eligibility.get("retained_by_profile"),
                "profile_id": eligibility.get("profile_id"),
            }
    try:
        original_receipts, original_records = revision_api._converter_source_evidence(
            artifact_store, source_data
        )
    except RevisionStoreError as exc:
        summary["issues"] = [{
            "code": exc.code,
            "json_pointer": f"{prefix}/_SourceData",
            "message": exc.message,
            "severity": "error",
        }]
        summary["issue_counts"]["error"] = 1
        return summary, None
    for receipt in original_receipts:
        artifact_receipts[str(receipt["artifact_id"])] = dict(receipt)

    record_kind = "native_snbi" if submission["profile_id"] == "native_snbi" else "json_upload_record"
    if isinstance(source_data, Mapping) and (
        source_data.get("over_record") is not None or source_data.get("nbi") is not None
    ):
        record_kind = "nbi_json_upload_record"
    source_record = {
        "source_artifact_id": str(source["artifact_id"]),
        "source_record_id": f"{source['artifact_id']}:{index}",
        "record_kind": record_kind,
        "record_pointer": f"/bridges/{index}",
        "payload": dict(value),
    }
    lineage_raw = value.get("_TransitionLineage")
    lineage: list[dict[str, Any]] = []
    if isinstance(lineage_raw, Mapping):
        for pointer, raw_lineage in sorted(lineage_raw.items()):
            if not isinstance(raw_lineage, Mapping):
                continue
            normalized_lineage = {
                **dict(raw_lineage),
                "target_pointer": raw_lineage.get("target_pointer") or pointer,
            }
            record_ids = raw_lineage.get("source_record_ids")
            if (
                not normalized_lineage.get("source_record_id")
                and isinstance(record_ids, list)
                and record_ids
            ):
                lineage.extend(
                    {**normalized_lineage, "source_record_id": record_id}
                    for record_id in record_ids
                )
            else:
                lineage.append(normalized_lineage)
    elif isinstance(lineage_raw, list):
        lineage = [dict(item) for item in lineage_raw if isinstance(item, Mapping)]
    validation = derive_server_validation(
        document,
        profile_id=str(submission["profile_id"]),
        data_year=int(submission["inventory_year"]),
        source_records=(source_record, *original_records),
    )
    content_hash = canonical_sha256(document)
    current = latest.get(key)
    projected = (
        "replace_inventory"
        if current is None and key in existing_inventory
        else "insert"
        if current is None
        else "no_op"
        if current == content_hash
        else "new_revision"
    )
    difference = {"change_count": 0, "changes": [], "truncated": False}
    if projected == "new_revision" and current is not None:
        before_document = _latest_document(latest_connection, key, current)
        if before_document is not None:
            difference = _bounded_diff(before_document, document)
    validation_issues = validation.get("issues") or []
    summary.update(
        {
            "valid": True,
            "projected_action": projected,
            "validation_status": validation.get("status"),
            "content_hash": content_hash,
            "base_content_hash": current,
            "diff": difference,
            "issue_counts": dict(validation.get("summary") or {}),
            "issues": [
                {
                    "code": str(item.get("rule_id") or "validation_issue"),
                    "json_pointer": f"{prefix}{item.get('json_pointer') or ''}",
                    "message": str(item.get("message") or "Validation issue"),
                    "severity": str(item.get("severity") or "error"),
                }
                for item in validation_issues[:100]
                if isinstance(item, Mapping)
            ],
        }
    )
    return summary, {
        "canonical_document": document,
        "data_year": int(submission["inventory_year"]),
        "source_records": [source_record, *original_records],
        "lineage": lineage,
    }


def _invalid_parse_preflight(
    job: OperationJob,
    *,
    submission: Mapping[str, Any],
    source: Mapping[str, Any],
    code: str,
    message: str,
    pointers: list[str],
    data_generation: int,
) -> dict[str, Any]:
    return {
        "format": "bridgeguard-import-preflight-v1",
        "import_id": job.id,
        "data_generation": data_generation,
        "valid": False,
        "source": _public_source(source),
        "artifact_receipts": [{
            **_public_source(source),
            "media_type": "application/json",
            "artifact_kind": "immutable_json_upload",
        }],
        "submission": dict(submission),
        "manifest_conflicts": [],
        "summary": {
            "total": 0, "importable": 0, "eligible": 0, "excluded": 0,
            "eligibility_unknown": 0, "duplicates": 0,
            "invalid": 1, "insert": 0, "new_revision": 0, "no_op": 0,
            "conflict": 0,
        },
        "field_loss_check": {"status": "not_run", "unknown_fields": None},
        "estimated_growth": {
            "canonical_bytes": 0,
            "artifact_bytes": int(source["size_bytes"]),
        },
        "bridges": [{
            "index": None,
            "valid": False,
            "projected_action": "invalid",
            "issues": [
                {
                    "code": code,
                    "json_pointer": pointer,
                    "message": message,
                    "severity": "error",
                }
                for pointer in (pointers or [""])
            ],
        }],
        "items": [],
    }


def _build_preflight(job: OperationJob, raw: bytes) -> dict[str, Any]:
    submission = job.payload.get("submission")
    source = job.payload.get("source")
    if not isinstance(submission, Mapping) or not isinstance(source, Mapping):
        raise ImportApiError(500, "import_job_corrupt", "The import job payload is invalid.")
    data_generation = get_workspace_state(
        _database_path(), revision_api.SHARED_WORKSPACE_ID
    ).data_generation
    try:
        bridges, manifest_conflicts = _parse_envelope(
            raw,
            submission,
            source_artifact_id=str(source["artifact_id"]),
            idempotency_key=job.idempotency_key,
        )
    except CanonicalJsonError as exc:
        return _invalid_parse_preflight(
            job,
            submission=submission,
            source=source,
            code=exc.code,
            message=exc.message,
            pointers=[exc.json_pointer or ""],
            data_generation=data_generation,
        )
    except ImportApiError as exc:
        raw_pointers = exc.details.get("json_pointers") or [""]
        pointers = [str(pointer) for pointer in raw_pointers][:100]
        return _invalid_parse_preflight(
            job,
            submission=submission,
            source=source,
            code=exc.code,
            message=exc.message,
            pointers=pointers,
            data_generation=data_generation,
        )
    latest = _latest_hashes(_database_path())
    existing_inventory = _existing_inventory_keys(_database_path())
    seen: set[tuple[int, str, int]] = set()
    summaries: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    artifact_store = _source_store()
    artifact_receipts: dict[str, dict[str, Any]] = {
        str(source["artifact_id"]): {
            **_public_source(source),
            "media_type": "application/json",
            "artifact_kind": "immutable_json_upload",
        }
    }
    latest_connection = sqlite3.connect(
        f"file:{Path(_database_path()).resolve().as_posix()}?mode=ro", uri=True
    )
    try:
        latest_connection.execute("PRAGMA query_only=ON")
        for index, value in enumerate(bridges):
            summary, item = _preflight_document(
                value,
                index=index,
                submission=submission,
                source=source,
                seen=seen,
                latest=latest,
                existing_inventory=existing_inventory,
                latest_connection=latest_connection,
                artifact_store=artifact_store,
                artifact_receipts=artifact_receipts,
            )
            summaries.append(summary)
            if item is not None:
                items.append(item)
    finally:
        latest_connection.close()
    counts = {
        "total": len(summaries),
        "importable": sum(bool(item.get("valid")) for item in summaries),
        "eligible": sum(
            (item.get("eligibility") or {}).get("status") == "eligible"
            for item in summaries
        ),
        "excluded": sum(
            (item.get("eligibility") or {}).get("status") == "excluded"
            for item in summaries
        ),
        "eligibility_unknown": sum(
            (item.get("eligibility") or {}).get("status") == "unknown"
            for item in summaries
        ),
        "duplicates": sum(
            any(issue.get("code") == "duplicate_logical_snapshot" for issue in item.get("issues") or [])
            for item in summaries
        ),
        "invalid": sum(not bool(item.get("valid")) for item in summaries),
        "insert": sum(item.get("projected_action") == "insert" for item in summaries),
        "new_revision": sum(item.get("projected_action") == "new_revision" for item in summaries),
        "replace_inventory": sum(item.get("projected_action") == "replace_inventory" for item in summaries),
        "no_op": sum(item.get("projected_action") == "no_op" for item in summaries),
        "conflict": len(manifest_conflicts),
    }
    canonical_bytes = sum(
        len(json.dumps(item["canonical_document"], ensure_ascii=True, separators=(",", ":")))
        for item in items
    )
    field_loss_check = _projection_field_loss_check(
        _database_path(), summaries=summaries, items=items
    )
    return {
        "format": "bridgeguard-import-preflight-v1",
        "import_id": job.id,
        "data_generation": get_workspace_state(
            _database_path(), revision_api.SHARED_WORKSPACE_ID
        ).data_generation,
        "valid": counts["invalid"] == 0 and field_loss_check["status"] == "passed",
        "source": _public_source(source),
        "artifact_receipts": list(artifact_receipts.values()),
        "submission": dict(submission),
        "manifest_conflicts": manifest_conflicts,
        "summary": counts,
        "field_loss_check": field_loss_check,
        "proposed_actions": {
            "import_to_inventory": True,
            "stage_only": False,
            "publish": False,
            "replace_snapshot_count": counts["new_revision"] + counts["replace_inventory"],
        },
        "estimated_growth": {
            "canonical_bytes": canonical_bytes,
            "artifact_bytes": int(source["size_bytes"]),
            "estimated_database_bytes": canonical_bytes * 2,
        },
        "bridges": summaries,
        "items": items,
    }


def process_import_preflight(job: OperationJob) -> dict[str, Any]:
    """Worker entry point: verify source, preflight it, persist bounded output."""

    if job.operation != IMPORT_PREFLIGHT_OPERATION:
        raise ValueError("unsupported import job operation")
    source = job.payload.get("source")
    if not isinstance(source, Mapping):
        raise ImportApiError(500, "import_job_corrupt", "The import source receipt is missing.")
    source_receipt = _source_store().get(str(source.get("artifact_id") or ""))
    for key in ("sha256", "size_bytes", "original_filename", "created_at", "status"):
        if str(getattr(source_receipt, key)) != str(source.get(key)):
            raise ImportApiError(
                409, "source_receipt_mismatch", "The immutable source receipt does not match the job."
            )
    raw = _source_store().read_bytes(source_receipt.artifact_id)
    preflight = _build_preflight(job, raw)
    encoded = json.dumps(
        preflight, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_PREFLIGHT_BYTES:
        raise ImportApiError(
            413, "preflight_too_large", "The bounded preflight artifact exceeds its limit."
        )
    receipt = _preflight_store().ingest(
        io.BytesIO(encoded), original_filename=f"import-{job.id}-preflight.json"
    )
    return {
        "preflight_digest": receipt.sha256,
        "preflight_receipt": _public_source(receipt.to_public_dict()),
        "summary": preflight["summary"],
        "valid": bool(preflight["valid"]),
    }


def _load_preflight(job: OperationJob, digest: str) -> dict[str, Any]:
    if job.status != "succeeded" or not isinstance(job.result, Mapping):
        raise ImportApiError(409, "preflight_not_ready", "The import preflight is not complete.")
    expected = str(job.result.get("preflight_digest") or "")
    if not _SHA256_RE.fullmatch(digest) or digest != expected:
        raise ImportApiError(409, "preflight_digest_mismatch", "The preflight digest does not match.")
    receipt = job.result.get("preflight_receipt")
    if not isinstance(receipt, Mapping):
        raise ImportApiError(500, "preflight_receipt_missing", "The preflight receipt is unavailable.")
    try:
        stored = _preflight_store().get(str(receipt.get("artifact_id") or ""))
        if stored.sha256 != expected or stored.size_bytes != int(receipt.get("size_bytes") or 0):
            raise ImportApiError(409, "preflight_receipt_mismatch", "The preflight receipt does not match.")
        raw = _preflight_store().read_bytes(stored.artifact_id)
        # This artifact was emitted by the server with allow_nan=False and is
        # already byte/hash bounded.  It can contain the source documents plus
        # per-bridge summaries, so parsing it through the source parser's one
        # million-node ceiling would reject a valid input near that ceiling
        # merely because the preflight adds bounded metadata.
        parsed = json.loads(raw.decode("utf-8"))
    except SourceArtifactError as exc:
        raise ImportApiError(
            409, "preflight_artifact_unavailable", "The preflight artifact failed integrity verification."
        ) from exc
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ImportApiError(409, "preflight_artifact_invalid", "The preflight artifact is invalid.") from exc
    if not isinstance(parsed, dict) or parsed.get("format") != "bridgeguard-import-preflight-v1":
        raise ImportApiError(409, "preflight_artifact_invalid", "The preflight artifact is invalid.")
    if parsed.get("import_id") != job.id:
        raise ImportApiError(409, "preflight_import_mismatch", "The preflight belongs to another import.")
    return parsed


@router.get("/{import_id}")
def get_import(
    import_id: str,
    request: Request,
    include_bridges: bool = Query(default=True),
):
    try:
        job = get_job(
            _job_db(),
            workspace_id=revision_api.SHARED_WORKSPACE_ID,
            owner_id=_actor(request),
            job_id=import_id,
        )
        payload = _public_job(job)
        if include_bridges and job.status == "succeeded" and isinstance(job.result, Mapping):
            preflight = _load_preflight(job, str(job.result.get("preflight_digest") or ""))
            payload["preflight"] = {
                key: preflight.get(key)
                for key in (
                    "valid", "data_generation", "submission", "manifest_conflicts", "summary",
                    "field_loss_check", "proposed_actions", "estimated_growth", "bridges",
                )
            }
        return payload
    except ImportApiError as exc:
        return _error(exc)
    except JobStoreError as exc:
        return _error(ImportApiError(exc.status, exc.code, "Import not found."))


def _committed_batch_for_artifact(database_path: str, artifact_id: str) -> sqlite3.Row | None:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT DISTINCT cb.id,cb.idempotency_key,cb.counts_json "
            "FROM conversion_batches cb "
            "JOIN eligibility_decisions ed ON ed.workspace_id=cb.workspace_id "
            "AND ed.conversion_batch_id=cb.id "
            "JOIN source_records sr ON sr.workspace_id=ed.workspace_id "
            "AND sr.id=ed.source_record_id "
            "WHERE cb.workspace_id=? AND sr.source_artifact_id=? "
            "ORDER BY cb.completed_at,cb.id LIMIT 1",
            (revision_api.SHARED_WORKSPACE_ID, artifact_id),
        ).fetchone()
    finally:
        connection.close()


@router.post("/{import_id}/commit")
def commit_import(
    import_id: str,
    body: ImportCommitBody,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    request_id: str | None = Header(default=None, alias="X-Request-ID"),
):
    lock_acquired = False
    try:
        key = _header(idempotency_key, code="idempotency_key_required", label="Idempotency-Key")
        _header(request_id, code="request_id_required", label="X-Request-ID")
        if body.atomic_mode != "batch":
            raise ImportApiError(
                422,
                "unsupported_atomic_mode",
                "Only all-or-nothing batch commit is currently supported.",
            )
        _IMPORT_MUTATION_LOCK.acquire()
        lock_acquired = True
        job = get_job(
            _job_db(),
            workspace_id=revision_api.SHARED_WORKSPACE_ID,
            owner_id=_actor(request),
            job_id=import_id,
        )
        preflight = _load_preflight(job, body.preflight_digest.lower())
        if not preflight.get("valid"):
            raise ImportApiError(422, "preflight_failed", "Invalid imports cannot be committed.")
        conflicts = preflight.get("manifest_conflicts") or []
        if conflicts and not str(body.override_reason or "").strip():
            raise ImportApiError(
                422,
                "metadata_override_reason_required",
                "Conflicting embedded metadata requires an explicit override reason.",
            )
        if body.validation_policy == "reject_critical":
            blocked = [
                item
                for item in preflight.get("bridges") or []
                if any(
                    str(issue.get("severity") or "").lower() in {"critical", "error", "fatal"}
                    for issue in item.get("issues") or []
                    if isinstance(issue, Mapping)
                )
            ]
            if blocked:
                raise ImportApiError(
                    422,
                    "validation_policy_failed",
                    "The selected validation policy rejects this preflight.",
                    details={"blocked_bridges": len(blocked)},
                )
        source = job.payload.get("source")
        if not isinstance(source, Mapping):
            raise ImportApiError(500, "import_job_corrupt", "The source receipt is missing.")
        source_receipt = _source_store().get(str(source.get("artifact_id") or ""))
        if source_receipt.sha256 != str(source.get("sha256") or ""):
            raise ImportApiError(409, "source_receipt_mismatch", "The source artifact changed.")

        database_path = _database_path()
        existing = _committed_batch_for_artifact(database_path, source_receipt.artifact_id)
        key_hash = canonical_sha256({"idempotency_key": key})
        service_key = f"import:{job.id}:{key_hash}"
        if existing is not None and str(existing["idempotency_key"]) != service_key:
            raise ImportApiError(409, "import_already_committed", "This import was already committed.")

        items = preflight.get("items")
        submission = preflight.get("submission")
        if not isinstance(items, list) or not items or not isinstance(submission, Mapping):
            raise ImportApiError(409, "preflight_artifact_invalid", "The preflight has no commit payload.")
        claimed_receipts = preflight.get("artifact_receipts")
        if not isinstance(claimed_receipts, list) or not claimed_receipts:
            raise ImportApiError(
                409, "preflight_artifact_invalid", "The preflight has no source receipts."
            )
        trusted_receipts: list[dict[str, Any]] = []
        receipt_store = _source_store()
        for claimed in claimed_receipts:
            if not isinstance(claimed, Mapping):
                raise ImportApiError(
                    409, "preflight_artifact_invalid", "A source receipt is invalid."
                )
            stored_receipt = receipt_store.get(str(claimed.get("artifact_id") or ""))
            for field in (
                "sha256", "size_bytes", "original_filename", "created_at", "status"
            ):
                if str(getattr(stored_receipt, field)) != str(claimed.get(field)):
                    raise ImportApiError(
                        409,
                        "source_receipt_mismatch",
                        "A source receipt no longer matches immutable storage.",
                    )
            trusted_receipts.append(
                {
                    **stored_receipt.to_public_dict(),
                    "media_type": str(claimed.get("media_type") or "application/json")[:128],
                    "artifact_kind": str(
                        claimed.get("artifact_kind") or "immutable_json_upload"
                    )[:128],
                }
            )
        expected_latest_by_key: dict[tuple[int, str, int], str | None] = {}
        for bridge_summary in preflight.get("bridges") or []:
            if not isinstance(bridge_summary, Mapping) or not bridge_summary.get("valid"):
                continue
            logical = bridge_summary.get("logical_key")
            if not isinstance(logical, Mapping):
                raise ImportApiError(
                    409, "preflight_artifact_invalid", "A preflight logical key is missing."
                )
            logical_key = (
                int(logical["state_code"]),
                str(logical["bridge_number"]),
                int(logical["data_year"]),
            )
            base_hash = bridge_summary.get("base_content_hash")
            if base_hash is not None and not _SHA256_RE.fullmatch(str(base_hash)):
                raise ImportApiError(
                    409, "preflight_artifact_invalid", "A preflight base hash is invalid."
                )
            expected_latest_by_key[logical_key] = (
                str(base_hash) if base_hash is not None else None
            )
        if len(expected_latest_by_key) != len(items):
            raise ImportApiError(
                409, "preflight_artifact_invalid", "Preflight snapshot versions are incomplete."
            )
        result = revision_api.stage_server_documents(
            database_path=database_path,
            actor_id=_actor(request),
            # The revision-store request identity is stable across transport
            # retries.  X-Request-ID remains required and is returned by the
            # outer HTTP boundary, but changing it must not defeat the commit
            # idempotency key or manufacture a second conversion batch.
            request_id=f"import-commit:{job.id}:{key_hash}",
            idempotency_key=service_key,
            expected_data_generation=body.expected_data_generation,
            profile_id=str(submission["profile_id"]),
            data_year=int(submission["inventory_year"]),
            items=items,
            artifact_receipts=tuple(trusted_receipts),
            write_mode=body.action,
            request_metadata={
                "operation": "reviewed_inventory_import",
                "import_id": job.id,
                "preflight_digest": body.preflight_digest.lower(),
                "validation_policy": body.validation_policy,
                "override_reason": str(body.override_reason or "").strip() or None,
            },
            expected_latest_by_key=expected_latest_by_key,
            project_to_inventory=True,
        )
        payload = revision_api._public_stage_result(
            database_path, result, source=source_receipt.to_public_dict()
        )
        payload.update(
            {
                "import_id": job.id,
                "preflight_digest": body.preflight_digest.lower(),
                "status": "imported",
                "imported": True,
                "published": False,
                "action": body.action,
                "validation_policy": body.validation_policy,
            }
        )
        return JSONResponse(
            status_code=200,
            content=payload,
            headers=revision_api._version_headers(
                result.data_generation, etag=result.request_hash
            ),
        )
    except ImportApiError as exc:
        return _error(exc)
    except SourceArtifactError as exc:
        return _error(ImportApiError(409, exc.code, "An immutable import artifact is unavailable."))
    except JobStoreError as exc:
        return _error(ImportApiError(exc.status, exc.code, "Import not found."))
    except RevisionStoreError as exc:
        return revision_api._error_response(exc, database_path=_database_path())
    except (TypeError, ValueError, KeyError) as exc:
        return _error(ImportApiError(422, "invalid_commit_payload", str(exc)))
    finally:
        if lock_acquired:
            _IMPORT_MUTATION_LOCK.release()


@router.post("/{import_id}/rollback")
def rollback_import(import_id: str, request: Request):
    """Invalidate a pre-commit import; immutable evidence remains for retention."""

    lock_acquired = False
    try:
        if _role(request) != "admin":
            raise ImportApiError(403, "admin_required", "Administrator access is required.")
        _IMPORT_MUTATION_LOCK.acquire()
        lock_acquired = True
        owner = _actor(request)
        job = get_job(
            _job_db(),
            workspace_id=revision_api.SHARED_WORKSPACE_ID,
            owner_id=owner,
            job_id=import_id,
        )
        source = job.payload.get("source")
        if not isinstance(source, Mapping):
            raise ImportApiError(500, "import_job_corrupt", "The source receipt is missing.")
        if _committed_batch_for_artifact(
            _database_path(), str(source.get("artifact_id") or "")
        ) is not None:
            raise ImportApiError(
                409,
                "rollback_not_safe",
                "Committed revisions cannot be rolled back through the import job.",
            )
        cancelled = cancel_job(
            _job_db(),
            workspace_id=revision_api.SHARED_WORKSPACE_ID,
            owner_id=owner,
            job_id=job.id,
        )
        return {
            "import_id": cancelled.id,
            "status": cancelled.status,
            "artifacts_retained": True,
            "message": "The import was cancelled before commit; immutable evidence follows retention policy.",
        }
    except ImportApiError as exc:
        return _error(exc)
    except JobStoreError as exc:
        return _error(ImportApiError(exc.status, exc.code, "Import not found."))
    finally:
        if lock_acquired:
            _IMPORT_MUTATION_LOCK.release()


__all__ = [
    "IMPORT_PREFLIGHT_OPERATION",
    "ImportApiError",
    "process_import_preflight",
    "router",
]
