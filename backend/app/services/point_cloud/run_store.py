"""Durable, workspace-scoped point-cloud run evidence and publication store.

The store deliberately contains no point-cloud bytes, local paths, provider
secrets, prompts, or model response bodies.  It binds immutable receipts to an
exact published canonical bridge revision and keeps mutable job state separate
from append-only analysis/review evidence.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping

from app.routers.services.database import ensure_schema, get_connection
from app.services.conversion.canonical_json import (
    CanonicalJsonError,
    canonical_json_text,
    canonical_sha256,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "cancelled"})
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_ALLOWED_TRANSITIONS = {
    "queued": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset({"succeeded", "failed", "cancelled"}),
}
_REVIEW_DECISIONS = frozenset({"accept", "reject"})
_FORBIDDEN_EVIDENCE_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "local_path",
        "path",
        "prompt",
        "raw_prompt",
        "raw_response",
        "report_text",
        "response",
        "response_text",
        "secret",
        "storage_path",
        "storage_uri",
    }
)
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_PROGRESS_BYTES = 32 * 1024
_MAX_EVIDENCE_ROWS = 100_000


class PointCloudRunStoreError(Exception):
    """HTTP-ready, path-free point-cloud store error."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)


class PointCloudRunInputError(PointCloudRunStoreError):
    pass


class PointCloudRunNotFoundError(PointCloudRunStoreError):
    pass


class PointCloudRunConflictError(PointCloudRunStoreError):
    pass


@dataclass(frozen=True, slots=True)
class PointCloudSourceEvidence:
    source_kind: str
    source_id: str
    sha256: str
    byte_size: int
    receipt: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class PointCloudVlmEvidence:
    receipt: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class PointCloudComponentEvidence:
    candidate_id: str
    proposed_class: str
    final_class: str
    disposition: str
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class PointCloudMeasurementEvidence:
    candidate_id: str
    feature_id: str | None
    product: str
    status: str
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CreatePointCloudRunRequest:
    workspace_id: str
    actor_id: str
    job_id: str
    idempotency_key: str
    state_code: int
    bridge_number: str
    data_year: int
    snapshot_revision_id: str
    canonical_sha256: str
    source: PointCloudSourceEvidence
    analysis_profile_sha256: str
    knowledge_pack_sha256: str
    report_source_set_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class AppendPointCloudEvidenceRequest:
    workspace_id: str
    run_id: str
    sources: tuple[PointCloudSourceEvidence, ...] = ()
    vlm_calls: tuple[PointCloudVlmEvidence, ...] = ()
    components: tuple[PointCloudComponentEvidence, ...] = ()
    measurements: tuple[PointCloudMeasurementEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class TransitionPointCloudRunRequest:
    workspace_id: str
    run_id: str
    expected_status: str
    status: str
    phase: str
    progress: Mapping[str, Any]
    result_sha256: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewPointCloudRunRequest:
    workspace_id: str
    run_id: str
    review_id: str
    reviewer_id: str
    decision: str
    reason: str
    expected_etag: str
    corrections: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PointCloudRunRecord:
    run_id: str
    workspace_id: str
    job_id: str
    actor_id: str
    state_code: int
    bridge_number: str
    data_year: int
    snapshot_revision_id: str
    canonical_sha256: str
    source_sha256: str
    source_byte_size: int
    analysis_profile_sha256: str
    knowledge_pack_sha256: str
    report_source_set_sha256: str | None
    status: str
    phase: str
    progress: Mapping[str, Any]
    result_sha256: str | None
    error_code: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None
    updated_at: str
    source_count: int
    vlm_call_count: int
    component_count: int
    measurement_count: int
    review_count: int
    published_review_id: str | None
    etag: str


@dataclass(frozen=True, slots=True)
class PointCloudReviewResult:
    review_id: str
    run_id: str
    decision: str
    before_sha256: str
    after_sha256: str
    published: bool
    data_generation: int
    etag: str
    idempotent: bool


@dataclass(frozen=True, slots=True)
class PublishedPointCloudResult:
    publication_id: str
    workspace_id: str
    state_code: int
    bridge_number: str
    data_year: int
    source_sha256: str
    analysis_profile_sha256: str
    run_id: str
    review_id: str
    published_by: str
    published_at: str


@dataclass(frozen=True, slots=True)
class PointCloudRunEvidence:
    run_id: str
    sources: tuple[Mapping[str, Any], ...]
    vlm_calls: tuple[Mapping[str, Any], ...]
    components: tuple[Mapping[str, Any], ...]
    measurements: tuple[Mapping[str, Any], ...]
    reviews: tuple[Mapping[str, Any], ...]
    truncated: bool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _text(value: Any, field: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PointCloudRunInputError(422, f"{field}_required", f"{field} is required")
    result = value.strip()
    if len(result) > maximum or "\x00" in result:
        raise PointCloudRunInputError(422, f"{field}_invalid", f"{field} is invalid")
    return result


def _sha(value: Any, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise PointCloudRunInputError(422, f"{field}_invalid", f"{field} must be a lowercase SHA-256")
    return value


def _uuid(value: Any, field: str) -> str:
    text = _text(value, field, maximum=64)
    try:
        parsed = uuid.UUID(text)
    except ValueError as exc:
        raise PointCloudRunInputError(422, f"{field}_invalid", f"{field} must be a UUID") from exc
    if str(parsed) != text.lower():
        raise PointCloudRunInputError(422, f"{field}_invalid", f"{field} must be canonical")
    return str(parsed)


def _validate_safe_evidence(value: Any, *, pointer: str = "") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise PointCloudRunInputError(422, "evidence_key_invalid", "Evidence object keys must be strings")
            if key.lower() in _FORBIDDEN_EVIDENCE_KEYS:
                raise PointCloudRunInputError(422, "evidence_contains_private_content", "Evidence contains a forbidden private field")
            _validate_safe_evidence(child, pointer=f"{pointer}/{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_safe_evidence(child, pointer=f"{pointer}/{index}")


def _canonical(value: Any, *, maximum: int = _MAX_JSON_BYTES) -> tuple[str, str]:
    _validate_safe_evidence(value)
    try:
        text = canonical_json_text(value)
    except CanonicalJsonError as exc:
        raise PointCloudRunInputError(422, "evidence_json_invalid", "Evidence is not valid canonical JSON") from exc
    if len(text.encode("utf-8")) > maximum:
        raise PointCloudRunInputError(413, "evidence_too_large", "Evidence exceeds the bounded size")
    return text, canonical_sha256(value)


def _safe_mapping(value: str) -> Mapping[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        parsed = {"value": parsed}
    return MappingProxyType(copy.deepcopy(parsed))


def _connect(db_path: str) -> sqlite3.Connection:
    connection = get_connection(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def initialize_point_cloud_run_store(db_path: str) -> None:
    ensure_schema(db_path)


def _request_payload(request: CreatePointCloudRunRequest, source_receipt_sha256: str) -> dict[str, Any]:
    return {
        "workspace_id": request.workspace_id,
        "actor_id": request.actor_id,
        "job_id": request.job_id,
        "idempotency_key": request.idempotency_key,
        "state_code": request.state_code,
        "bridge_number": request.bridge_number,
        "data_year": request.data_year,
        "snapshot_revision_id": request.snapshot_revision_id,
        "canonical_sha256": request.canonical_sha256,
        "source_kind": request.source.source_kind,
        "source_id": request.source.source_id,
        "source_sha256": request.source.sha256,
        "source_byte_size": request.source.byte_size,
        "source_receipt_sha256": source_receipt_sha256,
        "analysis_profile_sha256": request.analysis_profile_sha256,
        "knowledge_pack_sha256": request.knowledge_pack_sha256,
        "report_source_set_sha256": request.report_source_set_sha256,
    }


def create_point_cloud_run(
    db_path: str,
    request: CreatePointCloudRunRequest,
) -> tuple[PointCloudRunRecord, bool]:
    initialize_point_cloud_run_store(db_path)
    workspace_id = _text(request.workspace_id, "workspace_id")
    actor_id = _text(request.actor_id, "actor_id")
    job_id = _uuid(request.job_id, "job_id")
    idempotency_key = _text(request.idempotency_key, "idempotency_key", maximum=256)
    snapshot_revision_id = _uuid(request.snapshot_revision_id, "snapshot_revision_id")
    bridge_number = _text(request.bridge_number, "bridge_number", maximum=32)
    if type(request.state_code) is not int or not 1 <= request.state_code <= 99:
        raise PointCloudRunInputError(422, "state_code_invalid", "state_code is invalid")
    if type(request.data_year) is not int or not 1900 <= request.data_year <= 2200:
        raise PointCloudRunInputError(422, "data_year_invalid", "data_year is invalid")
    source = request.source
    source_kind = _text(source.source_kind, "source_kind", maximum=64)
    source_id = _text(source.source_id, "source_id", maximum=256)
    source_sha256 = _sha(source.sha256, "source_sha256")
    if type(source.byte_size) is not int or not 1 <= source.byte_size <= (1 << 50):
        raise PointCloudRunInputError(422, "source_byte_size_invalid", "source_byte_size is invalid")
    receipt_json, source_receipt_sha256 = _canonical(source.receipt)
    canonical_hash = _sha(request.canonical_sha256, "canonical_sha256")
    profile_hash = _sha(request.analysis_profile_sha256, "analysis_profile_sha256")
    pack_hash = _sha(request.knowledge_pack_sha256, "knowledge_pack_sha256")
    report_hash = _sha(request.report_source_set_sha256, "report_source_set_sha256", optional=True)
    normalized = CreatePointCloudRunRequest(
        workspace_id=workspace_id,
        actor_id=actor_id,
        job_id=job_id,
        idempotency_key=idempotency_key,
        state_code=request.state_code,
        bridge_number=bridge_number,
        data_year=request.data_year,
        snapshot_revision_id=snapshot_revision_id,
        canonical_sha256=str(canonical_hash),
        source=PointCloudSourceEvidence(source_kind, source_id, str(source_sha256), source.byte_size, source.receipt),
        analysis_profile_sha256=str(profile_hash),
        knowledge_pack_sha256=str(pack_hash),
        report_source_set_sha256=report_hash,
    )
    request_sha256 = canonical_sha256(_request_payload(normalized, source_receipt_sha256))
    connection = _connect(db_path)
    run_id = str(uuid.uuid4())
    created = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        replay = connection.execute(
            "SELECT id, request_sha256 FROM point_cloud_runs "
            "WHERE workspace_id=? AND actor_id=? AND idempotency_key=?",
            (workspace_id, actor_id, idempotency_key),
        ).fetchone()
        if replay is not None:
            if str(replay["request_sha256"]) != request_sha256:
                raise PointCloudRunConflictError(409, "point_cloud_idempotency_conflict", "Idempotency key was already used for another request")
            run_id = str(replay["id"])
        else:
            revision = connection.execute(
                "SELECT sr.canonical_sha256 FROM snapshot_revisions sr "
                "JOIN published_snapshots ps ON ps.workspace_id=sr.workspace_id "
                "AND ps.snapshot_revision_id=sr.id WHERE sr.workspace_id=? AND sr.id=? "
                "AND sr.state_code=? AND sr.bridge_number=? AND sr.data_year=?",
                (workspace_id, snapshot_revision_id, request.state_code, bridge_number, request.data_year),
            ).fetchone()
            if revision is None:
                raise PointCloudRunConflictError(409, "published_snapshot_required", "The exact published bridge revision is required")
            if str(revision["canonical_sha256"]) != canonical_hash:
                raise PointCloudRunConflictError(409, "published_snapshot_hash_mismatch", "The published bridge revision hash changed")
            now = _now()
            connection.execute(
                "INSERT INTO point_cloud_runs "
                "(id,workspace_id,job_id,idempotency_key,request_sha256,actor_id,state_code,bridge_number,data_year,"
                "snapshot_revision_id,canonical_sha256,source_sha256,source_byte_size,source_receipt_sha256,"
                "analysis_profile_sha256,knowledge_pack_sha256,report_source_set_sha256,status,phase,progress_json,"
                "result_sha256,error_code,created_at,started_at,completed_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued','queued','{}',NULL,NULL,?,NULL,NULL,?)",
                (run_id, workspace_id, job_id, idempotency_key, request_sha256, actor_id, request.state_code,
                 bridge_number, request.data_year, snapshot_revision_id, canonical_hash, source_sha256,
                 source.byte_size, source_receipt_sha256, profile_hash, pack_hash, report_hash, now, now),
            )
            connection.execute(
                "INSERT INTO point_cloud_run_sources "
                "(id,workspace_id,run_id,source_ordinal,source_kind,source_id,sha256,byte_size,receipt_json,receipt_sha256,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), workspace_id, run_id, 0, source_kind, source_id, source_sha256,
                 source.byte_size, receipt_json, source_receipt_sha256, now),
            )
            created = True
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return get_point_cloud_run(db_path, workspace_id, run_id), created


def _run_row(connection: sqlite3.Connection, workspace_id: str, run_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM point_cloud_runs WHERE workspace_id=? AND id=?",
        (workspace_id, run_id),
    ).fetchone()
    if row is None:
        raise PointCloudRunNotFoundError(404, "point_cloud_run_not_found", "Point-cloud run not found")
    return row


def _record(connection: sqlite3.Connection, row: sqlite3.Row) -> PointCloudRunRecord:
    workspace_id, run_id = str(row["workspace_id"]), str(row["id"])
    counts = {}
    for key, table in (
        ("source_count", "point_cloud_run_sources"),
        ("vlm_call_count", "point_cloud_vlm_calls"),
        ("component_count", "point_cloud_components"),
        ("measurement_count", "point_cloud_measurement_candidates"),
        ("review_count", "point_cloud_reviews"),
    ):
        counts[key] = int(connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE workspace_id=? AND run_id=?",
            (workspace_id, run_id),
        ).fetchone()[0])
    published = connection.execute(
        "SELECT review_id FROM published_point_cloud_results WHERE workspace_id=? AND run_id=? "
        "ORDER BY published_at DESC LIMIT 1",
        (workspace_id, run_id),
    ).fetchone()
    published_review_id = str(published[0]) if published else None
    etag_payload = {
        "run_id": run_id,
        "status": str(row["status"]),
        "updated_at": str(row["updated_at"]),
        "result_sha256": row["result_sha256"],
        "review_count": counts["review_count"],
        "published_review_id": published_review_id,
    }
    return PointCloudRunRecord(
        run_id=run_id, workspace_id=workspace_id, job_id=str(row["job_id"]), actor_id=str(row["actor_id"]),
        state_code=int(row["state_code"]), bridge_number=str(row["bridge_number"]), data_year=int(row["data_year"]),
        snapshot_revision_id=str(row["snapshot_revision_id"]), canonical_sha256=str(row["canonical_sha256"]),
        source_sha256=str(row["source_sha256"]), source_byte_size=int(row["source_byte_size"]),
        analysis_profile_sha256=str(row["analysis_profile_sha256"]), knowledge_pack_sha256=str(row["knowledge_pack_sha256"]),
        report_source_set_sha256=str(row["report_source_set_sha256"]) if row["report_source_set_sha256"] else None,
        status=str(row["status"]), phase=str(row["phase"]), progress=_safe_mapping(str(row["progress_json"])),
        result_sha256=str(row["result_sha256"]) if row["result_sha256"] else None,
        error_code=str(row["error_code"]) if row["error_code"] else None,
        created_at=str(row["created_at"]), started_at=str(row["started_at"]) if row["started_at"] else None,
        completed_at=str(row["completed_at"]) if row["completed_at"] else None, updated_at=str(row["updated_at"]),
        published_review_id=published_review_id, etag=f'"{canonical_sha256(etag_payload)}"', **counts,
    )


def get_point_cloud_run(db_path: str, workspace_id: str, run_id: str) -> PointCloudRunRecord:
    workspace_id = _text(workspace_id, "workspace_id")
    run_id = _uuid(run_id, "run_id")
    connection = _connect(db_path)
    try:
        return _record(connection, _run_row(connection, workspace_id, run_id))
    finally:
        connection.close()


def get_point_cloud_run_by_job(db_path: str, workspace_id: str, job_id: str) -> PointCloudRunRecord:
    workspace_id = _text(workspace_id, "workspace_id")
    job_id = _uuid(job_id, "job_id")
    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT * FROM point_cloud_runs WHERE workspace_id=? AND job_id=?",
            (workspace_id, job_id),
        ).fetchone()
        if row is None:
            raise PointCloudRunNotFoundError(404, "point_cloud_run_not_found", "Point-cloud run not found")
        return _record(connection, row)
    finally:
        connection.close()


def transition_point_cloud_run(db_path: str, request: TransitionPointCloudRunRequest) -> PointCloudRunRecord:
    workspace_id = _text(request.workspace_id, "workspace_id")
    run_id = _uuid(request.run_id, "run_id")
    expected = _text(request.expected_status, "expected_status", maximum=32)
    status = _text(request.status, "status", maximum=32)
    phase = _text(request.phase, "phase", maximum=128)
    if expected not in _RUN_STATUSES or status not in _RUN_STATUSES:
        raise PointCloudRunInputError(422, "point_cloud_status_invalid", "Point-cloud run status is invalid")
    if status not in _ALLOWED_TRANSITIONS.get(expected, frozenset()):
        raise PointCloudRunConflictError(409, "point_cloud_transition_invalid", "Point-cloud run transition is not allowed")
    progress_json, _ = _canonical(request.progress, maximum=_MAX_PROGRESS_BYTES)
    result_hash = _sha(request.result_sha256, "result_sha256", optional=True)
    error_code = _text(request.error_code, "error_code", maximum=128) if request.error_code is not None else None
    if status == "succeeded" and result_hash is None:
        raise PointCloudRunInputError(422, "point_cloud_result_hash_required", "A successful run requires result_sha256")
    if status != "succeeded" and result_hash is not None:
        raise PointCloudRunInputError(422, "point_cloud_result_hash_forbidden", "Only a successful run may store result_sha256")
    if status == "failed" and error_code is None:
        raise PointCloudRunInputError(422, "point_cloud_error_code_required", "A failed run requires error_code")
    if status != "failed" and error_code is not None:
        raise PointCloudRunInputError(422, "point_cloud_error_code_forbidden", "Only a failed run may store error_code")
    now = _now()
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _run_row(connection, workspace_id, run_id)
        if str(row["status"]) != expected:
            raise PointCloudRunConflictError(409, "point_cloud_status_conflict", "Point-cloud run status changed")
        started_at = now if expected == "queued" and status == "running" else row["started_at"]
        completed_at = now if status in _TERMINAL_STATUSES else None
        connection.execute(
            "UPDATE point_cloud_runs SET status=?,phase=?,progress_json=?,result_sha256=?,error_code=?,"
            "started_at=?,completed_at=?,updated_at=? WHERE workspace_id=? AND id=? AND status=?",
            (status, phase, progress_json, result_hash, error_code, started_at, completed_at, now, workspace_id, run_id, expected),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return get_point_cloud_run(db_path, workspace_id, run_id)


def update_point_cloud_run_progress(
    db_path: str,
    *,
    workspace_id: str,
    run_id: str,
    expected_status: str,
    phase: str,
    progress: Mapping[str, Any],
) -> PointCloudRunRecord:
    workspace_id = _text(workspace_id, "workspace_id")
    run_id = _uuid(run_id, "run_id")
    expected_status = _text(expected_status, "expected_status", maximum=32)
    phase = _text(phase, "phase", maximum=128)
    if expected_status not in {"queued", "running"}:
        raise PointCloudRunInputError(422, "point_cloud_status_invalid", "Point-cloud run status is invalid")
    progress_json, _ = _canonical(progress, maximum=_MAX_PROGRESS_BYTES)
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _run_row(connection, workspace_id, run_id)
        if str(row["status"]) != expected_status:
            raise PointCloudRunConflictError(409, "point_cloud_status_conflict", "Point-cloud run status changed")
        connection.execute(
            "UPDATE point_cloud_runs SET phase=?,progress_json=?,updated_at=? "
            "WHERE workspace_id=? AND id=? AND status=?",
            (phase, progress_json, _now(), workspace_id, run_id, expected_status),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return get_point_cloud_run(db_path, workspace_id, run_id)


def request_point_cloud_run_cancel(db_path: str, workspace_id: str, run_id: str, actor_id: str) -> PointCloudRunRecord:
    record = get_point_cloud_run(db_path, workspace_id, run_id)
    actor_id = _text(actor_id, "actor_id")
    if record.actor_id != actor_id:
        raise PointCloudRunNotFoundError(404, "point_cloud_run_not_found", "Point-cloud run not found")
    if record.status == "cancelled":
        return record
    if record.status in {"succeeded", "failed"}:
        raise PointCloudRunConflictError(409, "point_cloud_run_terminal", "Point-cloud run is already terminal")
    return transition_point_cloud_run(
        db_path,
        TransitionPointCloudRunRequest(
            workspace_id=workspace_id,
            run_id=run_id,
            expected_status=record.status,
            status="cancelled",
            phase="cancelled",
            progress={"cancelled": True},
        ),
    )


def append_point_cloud_run_evidence(db_path: str, request: AppendPointCloudEvidenceRequest) -> PointCloudRunRecord:
    workspace_id = _text(request.workspace_id, "workspace_id")
    run_id = _uuid(request.run_id, "run_id")
    total = len(request.sources) + len(request.vlm_calls) + len(request.components) + len(request.measurements)
    if total > _MAX_EVIDENCE_ROWS:
        raise PointCloudRunInputError(413, "point_cloud_evidence_row_limit", "Too many evidence rows")
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        run = _run_row(connection, workspace_id, run_id)
        if str(run["status"]) != "running":
            raise PointCloudRunConflictError(409, "point_cloud_evidence_run_not_running", "Evidence can only be appended while a run is running")
        now = _now()
        next_source = int(connection.execute("SELECT COALESCE(MAX(source_ordinal),-1)+1 FROM point_cloud_run_sources WHERE workspace_id=? AND run_id=?", (workspace_id, run_id)).fetchone()[0])
        for source in request.sources:
            receipt_json, receipt_hash = _canonical(source.receipt)
            sha = _sha(source.sha256, "source_sha256")
            if type(source.byte_size) is not int or source.byte_size < 0:
                raise PointCloudRunInputError(422, "source_byte_size_invalid", "source byte size is invalid")
            source_kind = _text(source.source_kind, "source_kind", maximum=64)
            source_id = _text(source.source_id, "source_id", maximum=256)
            existing = connection.execute(
                "SELECT sha256,byte_size,receipt_json,receipt_sha256 FROM point_cloud_run_sources "
                "WHERE workspace_id=? AND run_id=? AND source_kind=? AND source_id=?",
                (workspace_id, run_id, source_kind, source_id),
            ).fetchone()
            if existing is not None:
                if (str(existing["sha256"]), int(existing["byte_size"]), str(existing["receipt_json"]), str(existing["receipt_sha256"])) != (sha, source.byte_size, receipt_json, receipt_hash):
                    raise PointCloudRunConflictError(409, "point_cloud_source_evidence_conflict", "Source evidence identity changed")
                continue
            connection.execute(
                "INSERT INTO point_cloud_run_sources (id,workspace_id,run_id,source_ordinal,source_kind,source_id,sha256,byte_size,receipt_json,receipt_sha256,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), workspace_id, run_id, next_source, source_kind, source_id, sha, source.byte_size, receipt_json, receipt_hash, now),
            )
            next_source += 1
        next_vlm = int(connection.execute("SELECT COALESCE(MAX(call_ordinal),-1)+1 FROM point_cloud_vlm_calls WHERE workspace_id=? AND run_id=?", (workspace_id, run_id)).fetchone()[0])
        for evidence in request.vlm_calls:
            receipt = dict(evidence.receipt)
            receipt_json, receipt_hash = _canonical(receipt)
            required = ("task", "provider", "model", "messages_sha256", "response_schema_sha256", "response_sha256")
            if any(key not in receipt for key in required):
                raise PointCloudRunInputError(422, "vlm_receipt_incomplete", "VLM receipt is incomplete")
            for key in ("messages_sha256", "response_schema_sha256", "response_sha256"):
                _sha(receipt[key], key)
            if connection.execute(
                "SELECT 1 FROM point_cloud_vlm_calls WHERE workspace_id=? AND run_id=? AND receipt_sha256=?",
                (workspace_id, run_id, receipt_hash),
            ).fetchone() is not None:
                continue
            connection.execute(
                "INSERT INTO point_cloud_vlm_calls (id,workspace_id,run_id,call_ordinal,task,provider,model,prompt_sha256,schema_sha256,receipt_json,receipt_sha256,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), workspace_id, run_id, next_vlm, _text(receipt["task"], "task", maximum=64), _text(receipt["provider"], "provider", maximum=128), _text(receipt["model"], "model", maximum=128), receipt["messages_sha256"], receipt["response_schema_sha256"], receipt_json, receipt_hash, now),
            )
            next_vlm += 1
        next_component = int(connection.execute("SELECT COALESCE(MAX(component_ordinal),-1)+1 FROM point_cloud_components WHERE workspace_id=? AND run_id=?", (workspace_id, run_id)).fetchone()[0])
        for item in request.components:
            payload_json, payload_hash = _canonical(item.payload)
            candidate_id = _text(item.candidate_id, "candidate_id", maximum=128)
            proposed_class = _text(item.proposed_class, "proposed_class", maximum=64)
            final_class = _text(item.final_class, "final_class", maximum=64)
            disposition = _text(item.disposition, "disposition", maximum=64)
            existing = connection.execute(
                "SELECT proposed_class,final_class,disposition,component_json,component_sha256 "
                "FROM point_cloud_components WHERE workspace_id=? AND run_id=? AND candidate_id=?",
                (workspace_id, run_id, candidate_id),
            ).fetchone()
            if existing is not None:
                if tuple(str(existing[key]) for key in ("proposed_class", "final_class", "disposition", "component_json", "component_sha256")) != (proposed_class, final_class, disposition, payload_json, payload_hash):
                    raise PointCloudRunConflictError(409, "point_cloud_component_evidence_conflict", "Component evidence identity changed")
                continue
            connection.execute(
                "INSERT INTO point_cloud_components (id,workspace_id,run_id,component_ordinal,candidate_id,proposed_class,final_class,disposition,component_json,component_sha256,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), workspace_id, run_id, next_component, candidate_id, proposed_class, final_class, disposition, payload_json, payload_hash, now),
            )
            next_component += 1
        next_measurement = int(connection.execute("SELECT COALESCE(MAX(candidate_ordinal),-1)+1 FROM point_cloud_measurement_candidates WHERE workspace_id=? AND run_id=?", (workspace_id, run_id)).fetchone()[0])
        for item in request.measurements:
            payload_json, payload_hash = _canonical(item.payload)
            feature_id = _text(item.feature_id, "feature_id", maximum=128) if item.feature_id is not None else None
            candidate_id = _text(item.candidate_id, "candidate_id", maximum=128)
            product = _text(item.product, "product", maximum=64)
            measurement_status = _text(item.status, "measurement_status", maximum=64)
            existing = connection.execute(
                "SELECT feature_id,product,status,candidate_json,candidate_sha256 "
                "FROM point_cloud_measurement_candidates WHERE workspace_id=? AND run_id=? AND candidate_id=?",
                (workspace_id, run_id, candidate_id),
            ).fetchone()
            if existing is not None:
                existing_values = (
                    str(existing["feature_id"]) if existing["feature_id"] is not None else None,
                    str(existing["product"]), str(existing["status"]),
                    str(existing["candidate_json"]), str(existing["candidate_sha256"]),
                )
                if existing_values != (feature_id, product, measurement_status, payload_json, payload_hash):
                    raise PointCloudRunConflictError(409, "point_cloud_measurement_evidence_conflict", "Measurement evidence identity changed")
                continue
            connection.execute(
                "INSERT INTO point_cloud_measurement_candidates (id,workspace_id,run_id,candidate_ordinal,candidate_id,feature_id,product,status,candidate_json,candidate_sha256,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), workspace_id, run_id, next_measurement, candidate_id, feature_id, product, measurement_status, payload_json, payload_hash, now),
            )
            next_measurement += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return get_point_cloud_run(db_path, workspace_id, run_id)


def _published_revision_is_current(connection: sqlite3.Connection, run: sqlite3.Row) -> bool:
    row = connection.execute(
        "SELECT sr.canonical_sha256 FROM published_snapshots ps JOIN snapshot_revisions sr "
        "ON sr.workspace_id=ps.workspace_id AND sr.id=ps.snapshot_revision_id "
        "WHERE ps.workspace_id=? AND ps.state_code=? AND ps.bridge_number=? AND ps.data_year=? "
        "AND ps.snapshot_revision_id=?",
        (run["workspace_id"], run["state_code"], run["bridge_number"], run["data_year"], run["snapshot_revision_id"]),
    ).fetchone()
    return row is not None and str(row["canonical_sha256"]) == str(run["canonical_sha256"])


def review_point_cloud_run(db_path: str, request: ReviewPointCloudRunRequest) -> PointCloudReviewResult:
    workspace_id = _text(request.workspace_id, "workspace_id")
    run_id = _uuid(request.run_id, "run_id")
    review_id = _uuid(request.review_id, "review_id")
    reviewer_id = _text(request.reviewer_id, "reviewer_id")
    decision = _text(request.decision, "decision", maximum=16).lower()
    reason = _text(request.reason, "reason", maximum=2000)
    if decision not in _REVIEW_DECISIONS:
        raise PointCloudRunInputError(422, "point_cloud_review_decision_invalid", "Review decision is invalid")
    corrections = dict(request.corrections or {})
    corrections_json, corrections_hash = _canonical(corrections)
    connection = _connect(db_path)
    idempotent = False
    published = False
    generation = 0
    before_hash = after_hash = ""
    try:
        connection.execute("BEGIN IMMEDIATE")
        run = _run_row(connection, workspace_id, run_id)
        current = _record(connection, run)
        existing = connection.execute(
            "SELECT * FROM point_cloud_reviews WHERE workspace_id=? AND id=?",
            (workspace_id, review_id),
        ).fetchone()
        before_hash = str(run["result_sha256"] or "")
        if not before_hash or str(run["status"]) != "succeeded":
            raise PointCloudRunConflictError(409, "point_cloud_run_not_reviewable", "Only a successful run can be reviewed")
        after_hash = before_hash if not corrections else canonical_sha256({"run_result_sha256": before_hash, "corrections": corrections})
        if existing is not None:
            if (str(existing["run_id"]), str(existing["decision"]), str(existing["reason"]), str(existing["corrections_json"]), str(existing["before_sha256"]), str(existing["after_sha256"]), str(existing["reviewer_id"])) != (run_id, decision, reason, corrections_json, before_hash, after_hash, reviewer_id):
                raise PointCloudRunConflictError(409, "point_cloud_review_idempotency_conflict", "Review ID was already used for another decision")
            idempotent = True
        else:
            if current.etag != request.expected_etag:
                raise PointCloudRunConflictError(409, "point_cloud_review_etag_conflict", "Point-cloud run changed before review")
            if decision == "accept" and not _published_revision_is_current(connection, run):
                raise PointCloudRunConflictError(409, "point_cloud_snapshot_stale", "The exact published bridge revision changed before acceptance")
            ordinal = int(connection.execute("SELECT COUNT(*) FROM point_cloud_reviews WHERE workspace_id=? AND run_id=?", (workspace_id, run_id)).fetchone()[0])
            now = _now()
            connection.execute(
                "INSERT INTO point_cloud_reviews (id,workspace_id,run_id,review_ordinal,decision,reason,corrections_json,before_sha256,after_sha256,reviewer_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (review_id, workspace_id, run_id, ordinal, decision, reason, corrections_json, before_hash, after_hash, reviewer_id, now),
            )
            if decision == "accept":
                key = (workspace_id, run["state_code"], run["bridge_number"], run["data_year"], run["source_sha256"], run["analysis_profile_sha256"])
                previous = connection.execute(
                    "SELECT id,run_id FROM published_point_cloud_results WHERE workspace_id=? AND state_code=? AND bridge_number=? AND data_year=? AND source_sha256=? AND analysis_profile_sha256=?",
                    key,
                ).fetchone()
                if previous is None:
                    connection.execute(
                        "INSERT INTO published_point_cloud_results (id,workspace_id,state_code,bridge_number,data_year,source_sha256,analysis_profile_sha256,run_id,review_id,published_by,published_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), *key, run_id, review_id, reviewer_id, now),
                    )
                else:
                    previous_run = str(previous["run_id"])
                    connection.execute(
                        "UPDATE published_point_cloud_results SET run_id=?,review_id=?,published_by=?,published_at=? WHERE id=? AND workspace_id=?",
                        (run_id, review_id, reviewer_id, now, previous["id"], workspace_id),
                    )
                    if previous_run != run_id:
                        connection.execute(
                            "INSERT OR IGNORE INTO point_cloud_retention_outbox (id,workspace_id,run_id,source_sha256,status,attempts,error_code,created_at,updated_at) VALUES (?,?,?,?, 'pending',0,NULL,?,?)",
                            (str(uuid.uuid4()), workspace_id, previous_run, run["source_sha256"], now, now),
                        )
                connection.execute(
                    "UPDATE workspace_state SET data_generation=data_generation+1,updated_at=? WHERE workspace_id=?",
                    (now, workspace_id),
                )
                published = True
        state = connection.execute("SELECT data_generation FROM workspace_state WHERE workspace_id=?", (workspace_id,)).fetchone()
        if state is None:
            raise PointCloudRunNotFoundError(404, "point_cloud_run_not_found", "Point-cloud run not found")
        generation = int(state[0])
        if idempotent:
            publication = connection.execute("SELECT 1 FROM published_point_cloud_results WHERE workspace_id=? AND run_id=? AND review_id=?", (workspace_id, run_id, review_id)).fetchone()
            published = publication is not None
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    record = get_point_cloud_run(db_path, workspace_id, run_id)
    return PointCloudReviewResult(review_id, run_id, decision, before_hash, after_hash, published, generation, record.etag, idempotent)


def get_published_point_cloud_result(
    db_path: str,
    workspace_id: str,
    state_code: int,
    bridge_number: str,
    data_year: int,
    source_sha256: str,
    analysis_profile_sha256: str,
) -> PublishedPointCloudResult:
    workspace_id = _text(workspace_id, "workspace_id")
    bridge_number = _text(bridge_number, "bridge_number", maximum=32)
    source_sha256 = str(_sha(source_sha256, "source_sha256"))
    analysis_profile_sha256 = str(_sha(analysis_profile_sha256, "analysis_profile_sha256"))
    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT * FROM published_point_cloud_results WHERE workspace_id=? AND state_code=? AND bridge_number=? AND data_year=? AND source_sha256=? AND analysis_profile_sha256=?",
            (workspace_id, state_code, bridge_number, data_year, source_sha256, analysis_profile_sha256),
        ).fetchone()
        if row is None:
            raise PointCloudRunNotFoundError(404, "point_cloud_result_not_found", "Published point-cloud result not found")
        return PublishedPointCloudResult(
            publication_id=str(row["id"]), workspace_id=str(row["workspace_id"]), state_code=int(row["state_code"]),
            bridge_number=str(row["bridge_number"]), data_year=int(row["data_year"]), source_sha256=str(row["source_sha256"]),
            analysis_profile_sha256=str(row["analysis_profile_sha256"]), run_id=str(row["run_id"]), review_id=str(row["review_id"]),
            published_by=str(row["published_by"]), published_at=str(row["published_at"]),
        )
    finally:
        connection.close()


def get_latest_published_point_cloud_result(
    db_path: str,
    workspace_id: str,
    state_code: int,
    bridge_number: str,
    source_sha256: str,
    *,
    data_year: int | None = None,
) -> PublishedPointCloudResult:
    """Resolve the accepted result for the current source and requested year."""

    workspace_id = _text(workspace_id, "workspace_id")
    bridge_number = _text(bridge_number, "bridge_number", maximum=32)
    source_sha256 = str(_sha(source_sha256, "source_sha256"))
    if data_year is not None and (type(data_year) is not int or not 1900 <= data_year <= 2200):
        raise PointCloudRunInputError(422, "point_cloud_data_year_invalid", "Data year is invalid")
    connection = _connect(db_path)
    try:
        where = (
            "workspace_id=? AND state_code=? AND bridge_number=? AND source_sha256=?"
        )
        parameters: list[Any] = [workspace_id, int(state_code), bridge_number, source_sha256]
        if data_year is not None:
            where += " AND data_year=?"
            parameters.append(data_year)
        row = connection.execute(
            "SELECT * FROM published_point_cloud_results WHERE "
            + where
            + " ORDER BY data_year DESC,published_at DESC,id DESC LIMIT 1",
            tuple(parameters),
        ).fetchone()
        if row is None:
            raise PointCloudRunNotFoundError(
                404,
                "point_cloud_result_not_found",
                "Published point-cloud result not found",
            )
        return PublishedPointCloudResult(
            publication_id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            state_code=int(row["state_code"]),
            bridge_number=str(row["bridge_number"]),
            data_year=int(row["data_year"]),
            source_sha256=str(row["source_sha256"]),
            analysis_profile_sha256=str(row["analysis_profile_sha256"]),
            run_id=str(row["run_id"]),
            review_id=str(row["review_id"]),
            published_by=str(row["published_by"]),
            published_at=str(row["published_at"]),
        )
    finally:
        connection.close()


def _verified_json(raw: str, expected_sha256: str, code: str) -> Mapping[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PointCloudRunStoreError(500, code, "Stored point-cloud evidence failed integrity validation") from exc
    if not isinstance(value, dict) or canonical_sha256(value) != expected_sha256:
        raise PointCloudRunStoreError(500, code, "Stored point-cloud evidence failed integrity validation")
    return MappingProxyType(copy.deepcopy(value))


def get_point_cloud_run_evidence(
    db_path: str,
    workspace_id: str,
    run_id: str,
    *,
    limit_per_section: int = 2_000,
) -> PointCloudRunEvidence:
    workspace_id = _text(workspace_id, "workspace_id")
    run_id = _uuid(run_id, "run_id")
    if type(limit_per_section) is not int or not 1 <= limit_per_section <= 10_000:
        raise PointCloudRunInputError(422, "point_cloud_evidence_limit_invalid", "Evidence limit is invalid")
    connection = _connect(db_path)
    try:
        _run_row(connection, workspace_id, run_id)
        bounded = limit_per_section + 1
        source_rows = connection.execute(
            "SELECT source_ordinal,source_kind,source_id,sha256,byte_size,receipt_json,receipt_sha256 "
            "FROM point_cloud_run_sources WHERE workspace_id=? AND run_id=? ORDER BY source_ordinal LIMIT ?",
            (workspace_id, run_id, bounded),
        ).fetchall()
        vlm_rows = connection.execute(
            "SELECT call_ordinal,task,provider,model,prompt_sha256,schema_sha256,receipt_json,receipt_sha256 "
            "FROM point_cloud_vlm_calls WHERE workspace_id=? AND run_id=? ORDER BY call_ordinal LIMIT ?",
            (workspace_id, run_id, bounded),
        ).fetchall()
        component_rows = connection.execute(
            "SELECT component_ordinal,candidate_id,proposed_class,final_class,disposition,component_json,component_sha256 "
            "FROM point_cloud_components WHERE workspace_id=? AND run_id=? ORDER BY component_ordinal LIMIT ?",
            (workspace_id, run_id, bounded),
        ).fetchall()
        measurement_rows = connection.execute(
            "SELECT candidate_ordinal,candidate_id,feature_id,product,status,candidate_json,candidate_sha256 "
            "FROM point_cloud_measurement_candidates WHERE workspace_id=? AND run_id=? ORDER BY candidate_ordinal LIMIT ?",
            (workspace_id, run_id, bounded),
        ).fetchall()
        review_rows = connection.execute(
            "SELECT review_ordinal,id,decision,reason,corrections_json,before_sha256,after_sha256,created_at "
            "FROM point_cloud_reviews WHERE workspace_id=? AND run_id=? ORDER BY review_ordinal LIMIT ?",
            (workspace_id, run_id, bounded),
        ).fetchall()
        truncated = any(len(rows) > limit_per_section for rows in (source_rows, vlm_rows, component_rows, measurement_rows, review_rows))
        sources = tuple(
            MappingProxyType({
                "ordinal": int(row["source_ordinal"]), "kind": str(row["source_kind"]),
                "source_id": str(row["source_id"]), "sha256": str(row["sha256"]),
                "byte_size": int(row["byte_size"]),
                "receipt": _verified_json(str(row["receipt_json"]), str(row["receipt_sha256"]), "point_cloud_source_evidence_corrupt"),
            })
            for row in source_rows[:limit_per_section]
        )
        vlm_calls = tuple(
            MappingProxyType({
                "ordinal": int(row["call_ordinal"]), "task": str(row["task"]),
                "provider": str(row["provider"]), "model": str(row["model"]),
                "prompt_sha256": str(row["prompt_sha256"]), "schema_sha256": str(row["schema_sha256"]),
                "receipt": _verified_json(str(row["receipt_json"]), str(row["receipt_sha256"]), "point_cloud_vlm_evidence_corrupt"),
            })
            for row in vlm_rows[:limit_per_section]
        )
        components = tuple(
            MappingProxyType({
                "ordinal": int(row["component_ordinal"]), "candidate_id": str(row["candidate_id"]),
                "proposed_class": str(row["proposed_class"]), "final_class": str(row["final_class"]),
                "disposition": str(row["disposition"]),
                "evidence": _verified_json(str(row["component_json"]), str(row["component_sha256"]), "point_cloud_component_evidence_corrupt"),
            })
            for row in component_rows[:limit_per_section]
        )
        measurements = tuple(
            MappingProxyType({
                "ordinal": int(row["candidate_ordinal"]), "candidate_id": str(row["candidate_id"]),
                "feature_id": str(row["feature_id"]) if row["feature_id"] is not None else None,
                "product": str(row["product"]), "status": str(row["status"]),
                "evidence": _verified_json(str(row["candidate_json"]), str(row["candidate_sha256"]), "point_cloud_measurement_evidence_corrupt"),
            })
            for row in measurement_rows[:limit_per_section]
        )
        reviews = tuple(
            MappingProxyType({
                "ordinal": int(row["review_ordinal"]), "review_id": str(row["id"]),
                "decision": str(row["decision"]), "reason": str(row["reason"]),
                "corrections": MappingProxyType(copy.deepcopy(json.loads(str(row["corrections_json"])))),
                "before_sha256": str(row["before_sha256"]), "after_sha256": str(row["after_sha256"]),
                "created_at": str(row["created_at"]),
            })
            for row in review_rows[:limit_per_section]
        )
        return PointCloudRunEvidence(run_id, sources, vlm_calls, components, measurements, reviews, truncated)
    finally:
        connection.close()


__all__ = [
    "AppendPointCloudEvidenceRequest",
    "CreatePointCloudRunRequest",
    "PointCloudComponentEvidence",
    "PointCloudMeasurementEvidence",
    "PointCloudReviewResult",
    "PointCloudRunConflictError",
    "PointCloudRunInputError",
    "PointCloudRunNotFoundError",
    "PointCloudRunRecord",
    "PointCloudRunEvidence",
    "PointCloudRunStoreError",
    "PointCloudSourceEvidence",
    "PointCloudVlmEvidence",
    "PublishedPointCloudResult",
    "ReviewPointCloudRunRequest",
    "TransitionPointCloudRunRequest",
    "append_point_cloud_run_evidence",
    "create_point_cloud_run",
    "get_point_cloud_run",
    "get_point_cloud_run_by_job",
    "get_point_cloud_run_evidence",
    "get_latest_published_point_cloud_result",
    "get_published_point_cloud_result",
    "initialize_point_cloud_run_store",
    "request_point_cloud_run_cancel",
    "review_point_cloud_run",
    "transition_point_cloud_run",
    "update_point_cloud_run_progress",
]
