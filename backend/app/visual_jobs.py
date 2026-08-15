"""Durable two-phase visual-investigation jobs.

The HTTP request only freezes an authenticated bridge/revision/source/profile
identity and enqueues work.  The single supported worker performs bounded VLM
processing later, stores the full response as an immutable content-addressed
artifact, and commits only a small path-free receipt to the operation queue.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID, uuid4

from .routers.services import vision as vision_service
from .routers.services.report_rag_cache import canonical_sha256 as report_sha256
from .services.ai.context import (
    AiAnalysisMode,
    AiRequestCancelled,
    AiRequestContext,
    VisualAnalysisLimits,
)
from .services.bridge_identity import (
    BridgeIdentityError,
    BridgeSnapshotIdentity,
    resolve_published_bridge_identity,
)
from .services.conversion.source_artifacts import (
    SourceArtifactError,
    SourceArtifactStore,
)
from .services.operations.jobs import (
    JobStoreError,
    OperationJob,
    enqueue_job,
    get_job,
    is_job_cancellation_requested,
)
from .services.operations.quotas import DEFAULT_OPERATION_QUOTAS, QuotaExceeded


VISUAL_JOB_OPERATION = "bridge_visual_investigation"
VISUAL_JOB_FORMAT = "bridgeguard-visual-job-v1"
VISUAL_RESULT_FORMAT = "bridgeguard-visual-result-v1"
MAX_QUERY_CHARACTERS = 4_000
MAX_SELECTED_SOURCES = 100
MAX_RESULT_BYTES = 24 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_MODES = frozenset({"fast", "balanced", "high_precision"})
_PATH_KEYS = frozenset(
    {"path", "filepath", "file_path", "local_path", "absolute_path", "object_path"}
)


class VisualJobError(RuntimeError):
    """Bounded public failure with retry guidance for the durable worker."""

    def __init__(self, code: str, *, status: int = 409, retry: bool = False) -> None:
        self.code = str(code)
        self.status = int(status)
        self.retry = bool(retry)
        super().__init__(self.code)


class VisualJobCancelled(VisualJobError):
    def __init__(self) -> None:
        super().__init__("cancelled_by_user", status=409, retry=False)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _canonical_uuid(value: object, field: str) -> str:
    text = str(value or "").strip().lower()
    try:
        parsed = UUID(text)
    except (TypeError, ValueError, AttributeError) as exc:
        raise VisualJobError(f"invalid_{field}", status=422) from exc
    if str(parsed) != text:
        raise VisualJobError(f"invalid_{field}", status=422)
    return text


def _clean_query(value: object) -> str:
    text = str(value or "").strip()
    if not text or len(text) > MAX_QUERY_CHARACTERS or any(
        ord(character) < 32 and character not in "\t\n\r" for character in text
    ):
        raise VisualJobError("invalid_visual_query", status=422)
    return text


def normalize_selected_sources(value: Sequence[object] | None) -> tuple[str, ...] | None:
    """Normalize a filename allowlist without accepting path syntax."""

    if value is None:
        return None
    if isinstance(value, (str, bytes, bytearray)):
        raise VisualJobError("invalid_selected_sources", status=422)
    if len(value) > MAX_SELECTED_SOURCES:
        raise VisualJobError("too_many_selected_sources", status=422)
    normalized: set[str] = set()
    for source in value:
        text = str(source or "").strip()
        if text == "Database":
            continue
        if (
            not text
            or len(text) > 180
            or text in {".", ".."}
            or "/" in text
            or "\\" in text
            or any(ord(character) < 32 for character in text)
        ):
            raise VisualJobError("invalid_selected_source", status=422)
        normalized.add(text)
    return tuple(sorted(normalized))


def _source_receipts(
    database_path: str | Path,
    *,
    workspace_id: str,
    state_code: int,
    bridge_number: str,
    selected_sources: tuple[str, ...] | None,
) -> tuple[dict[str, object], ...]:
    try:
        path = Path(database_path).resolve(strict=True)
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        rows = connection.execute(
            "SELECT id,filename,size_bytes,sha256,uploaded_at FROM bridge_attachments "
            "WHERE workspace_id=? AND state_code=? AND bridge_number=? "
            "AND attachment_type='inspection_report' ORDER BY id",
            (workspace_id, str(state_code), str(bridge_number)),
        ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        raise VisualJobError("report_source_inventory_unavailable", status=503, retry=True) from exc
    finally:
        if "connection" in locals():
            connection.close()

    raw_available_names = tuple(str(row["filename"]) for row in rows)
    normalized_available = normalize_selected_sources(raw_available_names) or ()
    if len(normalized_available) != len(set(raw_available_names)):
        # A report named like the reserved Database source or duplicate names
        # would make a filename-only allowlist ambiguous.  Do not guess which
        # bytes the user intended to submit to a hosted model.
        raise VisualJobError("ambiguous_report_source_identity", status=409)
    available_names = set(normalized_available)
    if selected_sources is None:
        selected_names = tuple(sorted(available_names))
    else:
        unknown = set(selected_sources) - available_names
        if unknown:
            raise VisualJobError("selected_report_source_not_found", status=422)
        selected_names = selected_sources
    selected_set = set(selected_names)
    receipts: list[dict[str, object]] = []
    for row in rows:
        filename = str(row["filename"])
        if filename not in selected_set:
            continue
        digest = str(row["sha256"] or "").strip().lower()
        size = int(row["size_bytes"] or 0)
        if not _SHA256_RE.fullmatch(digest) or size < 1:
            raise VisualJobError("report_source_identity_unavailable", status=409)
        receipts.append(
            {
                "report_id": str(row["id"]),
                "filename": filename,
                "sha256": digest,
                "size_bytes": size,
                "uploaded_at": str(row["uploaded_at"] or ""),
            }
        )
    return tuple(receipts)


def _profile_payload(
    limits: VisualAnalysisLimits, *, segmentation_mode: str
) -> dict[str, object]:
    profiles = vision_service._visual_profiles_for_request(limits)
    return {
        "profile_version": "bridgeguard-durable-visual-v1",
        "report_rag_profiles": {
            "parser": profiles.parser,
            "caption": profiles.caption,
            "embedding": profiles.embedding,
            "retrieval": profiles.retrieval,
            "processing_version": profiles.processing_version,
        },
        "detection": {
            "model_id": vision_service.llm_providers.generation_model(),
            "profile_version": "bridge-visual-detection-v1",
            "prompt_version": "bridge-visual-defect-prompt-v1",
            "segmentation_mode": segmentation_mode,
        },
        "limits": {
            "max_images": limits.max_images,
            "max_estimated_input_tokens": limits.max_estimated_input_tokens,
            "max_estimated_cost_usd": limits.max_estimated_cost_usd,
        },
    }


def enqueue_visual_investigation(
    *,
    database_path: str | Path,
    job_database_path: str | Path,
    workspace_id: str,
    owner_id: str,
    idempotency_key: str,
    request_id: str,
    query: str,
    bridge_identity: BridgeSnapshotIdentity,
    selected_sources: Sequence[object] | None,
    defect_segmentation_mode: str,
    conversation_id: str | None,
) -> tuple[OperationJob, bool]:
    """Freeze exact evidence/profile identity and enqueue without calling a model."""

    canonical_request = _canonical_uuid(request_id, "request_id")
    clean_query = _clean_query(query)
    mode = str(defect_segmentation_mode or "fast").strip().lower()
    if mode not in _SAFE_MODES:
        raise VisualJobError("invalid_defect_segmentation_mode", status=422)
    sources = normalize_selected_sources(selected_sources)
    receipts = _source_receipts(
        database_path,
        workspace_id=workspace_id,
        state_code=bridge_identity.state_code,
        bridge_number=bridge_identity.bridge_number,
        selected_sources=sources,
    )
    # Freeze "all reports" as an explicit filename set.  Reports attached
    # later are not silently added to an already accepted investigation.
    selected = tuple(sorted({str(item["filename"]) for item in receipts}))
    limits = VisualAnalysisLimits()
    profile = _profile_payload(limits, segmentation_mode=mode)
    payload: dict[str, object] = {
        "format": VISUAL_JOB_FORMAT,
        "request_id": canonical_request,
        "query": clean_query,
        "analysis_mode": "visual",
        "bridge_snapshot": bridge_identity.public_payload(),
        "selected_sources": list(selected),
        "source_receipts": list(receipts),
        "source_set_sha256": report_sha256(list(receipts)),
        "visual_profile": profile,
        "visual_profile_sha256": report_sha256(profile),
        "defect_segmentation_mode": mode,
        "conversation_id": (
            _canonical_uuid(conversation_id, "conversation_id")
            if conversation_id else None
        ),
    }
    try:
        existing = None
        try:
            from .services.operations.jobs import get_job_by_idempotency

            existing = get_job_by_idempotency(
                job_database_path,
                workspace_id=workspace_id,
                owner_id=owner_id,
                operation=VISUAL_JOB_OPERATION,
                idempotency_key=idempotency_key,
            )
        except JobStoreError as exc:
            if exc.code != "job_not_found":
                raise
        job = enqueue_job(
            job_database_path,
            workspace_id=workspace_id,
            owner_id=owner_id,
            operation=VISUAL_JOB_OPERATION,
            idempotency_key=idempotency_key,
            payload=payload,
            max_attempts=3,
        )
        return job, existing is not None
    except JobStoreError:
        raise
    except (TypeError, ValueError) as exc:
        raise VisualJobError("invalid_visual_job", status=422) from exc


def _identity_from_payload(value: object) -> BridgeSnapshotIdentity:
    if not isinstance(value, Mapping):
        raise VisualJobError("visual_job_identity_corrupt", status=500)
    try:
        return BridgeSnapshotIdentity(
            workspace_id=str(value["workspace_id"]),
            state_code=int(value["state_code"]),
            bridge_number=str(value["bridge_number"]),
            data_year=int(value["data_year"]),
            snapshot_revision_id=str(value["snapshot_revision_id"]),
            revision_number=int(value["revision_number"]),
            canonical_sha256=str(value["canonical_sha256"]),
            profile_id=str(value["profile_id"]),
            rule_bundle_sha256=str(value["rule_bundle_sha256"]),
            published_at=str(value["published_at"]),
            data_generation=int(value["data_generation"]),
            schema_generation=int(value["schema_generation"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise VisualJobError("visual_job_identity_corrupt", status=500) from exc


def _verify_exact_identity(database_path: str | Path, job: OperationJob) -> BridgeSnapshotIdentity:
    identity = _identity_from_payload(job.payload.get("bridge_snapshot"))
    if identity.workspace_id != job.workspace_id:
        raise VisualJobError("visual_job_scope_mismatch", status=500)
    try:
        current = resolve_published_bridge_identity(
            database_path,
            workspace_id=identity.workspace_id,
            state_code=identity.state_code,
            bridge_number=identity.bridge_number,
            data_year=identity.data_year,
        )
    except BridgeIdentityError as exc:
        raise VisualJobError("published_bridge_snapshot_unavailable", status=409) from exc
    if current.public_payload() != identity.public_payload():
        raise VisualJobError("published_bridge_snapshot_changed", status=409)
    return identity


def _verify_profile(job: OperationJob) -> VisualAnalysisLimits:
    claimed = job.payload.get("visual_profile")
    claimed_hash = str(job.payload.get("visual_profile_sha256") or "")
    mode = str(job.payload.get("defect_segmentation_mode") or "")
    if not isinstance(claimed, Mapping) or not _SHA256_RE.fullmatch(claimed_hash):
        raise VisualJobError("visual_profile_receipt_corrupt", status=500)
    limits_payload = claimed.get("limits")
    if not isinstance(limits_payload, Mapping):
        raise VisualJobError("visual_profile_receipt_corrupt", status=500)
    try:
        limits = VisualAnalysisLimits(
            max_images=int(limits_payload["max_images"]),
            max_estimated_input_tokens=int(limits_payload["max_estimated_input_tokens"]),
            max_estimated_cost_usd=float(limits_payload["max_estimated_cost_usd"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise VisualJobError("visual_profile_receipt_corrupt", status=500) from exc
    if report_sha256(claimed) != claimed_hash:
        raise VisualJobError("visual_profile_receipt_mismatch", status=500)
    active = _profile_payload(limits, segmentation_mode=mode)
    if report_sha256(active) != claimed_hash:
        raise VisualJobError("visual_profile_changed", status=409)
    return limits


def _verify_exact_sources(database_path: str | Path, job: OperationJob) -> None:
    identity = _identity_from_payload(job.payload.get("bridge_snapshot"))
    selected = normalize_selected_sources(job.payload.get("selected_sources"))
    claimed = job.payload.get("source_receipts")
    if not isinstance(claimed, list):
        raise VisualJobError("visual_source_receipt_corrupt", status=500)
    current = list(
        _source_receipts(
            database_path,
            workspace_id=job.workspace_id,
            state_code=identity.state_code,
            bridge_number=identity.bridge_number,
            selected_sources=selected,
        )
    )
    if current != claimed or report_sha256(current) != str(
        job.payload.get("source_set_sha256") or ""
    ):
        raise VisualJobError("visual_report_sources_changed", status=409)

    # Verify the bytes before any VLM call.  Database receipt fields alone are
    # not enough if an operator or failed restore changed a private object.
    files = vision_service._selected_report_files(
        vision_service._existing_report_files(
            identity.state_code,
            identity.bridge_number,
            workspace_id=job.workspace_id,
        ),
        list(selected or ()),
    )
    try:
        actual = vision_service._report_source_identities(files)
    except Exception as exc:
        raise VisualJobError("visual_report_source_integrity_failed", status=409) from exc
    actual_compact = sorted(
        (
            str(item.report_id),
            str(item.sha256),
            int(item.byte_size),
        )
        for item in actual
    )
    claimed_compact = sorted(
        (
            str(item.get("report_id") or ""),
            str(item.get("sha256") or ""),
            int(item.get("size_bytes") or 0),
        )
        for item in claimed
        if isinstance(item, Mapping)
    )
    if actual_compact != claimed_compact:
        raise VisualJobError("visual_report_source_integrity_failed", status=409)


class DurableVisualCancellationToken:
    """Event-like token backed by the durable job cancellation marker."""

    def __init__(self, job_database_path: str | Path, job: OperationJob) -> None:
        self._path = Path(job_database_path)
        self._job = job
        self._local = threading.Event()

    def set(self) -> None:
        self._local.set()

    def is_set(self) -> bool:
        if self._local.is_set():
            return True
        return self.durable_requested()

    def durable_requested(self) -> bool:
        return is_job_cancellation_requested(
            self._path,
            workspace_id=self._job.workspace_id,
            owner_id=self._job.owner_id,
            job_id=self._job.id,
        )


def _finite_public_json(value: object) -> object:
    """Copy provider output while dropping any accidental internal path key."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            name = str(key)
            if name.strip().lower() in _PATH_KEYS:
                continue
            result[name] = _finite_public_json(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_finite_public_json(item) for item in value]
    return str(value)


def _result_store(artifact_root: str | Path) -> SourceArtifactStore:
    return SourceArtifactStore(
        Path(artifact_root) / "visual_investigation_results",
        max_bytes=MAX_RESULT_BYTES,
    )


async def process_visual_investigation_job(
    *,
    job: OperationJob,
    database_path: str | Path,
    job_database_path: str | Path,
    artifact_root: str | Path,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> dict[str, object]:
    """Execute one claimed job and return its small immutable-result receipt."""

    if job.operation != VISUAL_JOB_OPERATION or job.status != "running":
        raise VisualJobError("invalid_visual_worker_job", status=500)
    if job.payload.get("format") != VISUAL_JOB_FORMAT:
        raise VisualJobError("visual_job_payload_corrupt", status=500)
    identity = _verify_exact_identity(database_path, job)
    limits = _verify_profile(job)
    _verify_exact_sources(database_path, job)
    if progress_callback is not None:
        progress_callback("visual_analysis", 1, 3)
    token = DurableVisualCancellationToken(job_database_path, job)
    if token.is_set():
        raise VisualJobCancelled()

    processing_request_id = str(uuid4())
    context = AiRequestContext(
        request_id=processing_request_id,
        owner_id=job.owner_id,
        workspace_id=job.workspace_id,
        conversation_id=(
            str(job.payload["conversation_id"])
            if job.payload.get("conversation_id") else None
        ),
        mode=AiAnalysisMode.VISUAL,
        operation=VISUAL_JOB_OPERATION,
        state_code=identity.state_code,
        bridge_number=identity.bridge_number,
        data_year=identity.data_year,
        bridge_identity=identity,
        deadline_monotonic=time.monotonic() + 1800,
        cancellation_token=token,  # type: ignore[arg-type]
    )
    try:
        with DEFAULT_OPERATION_QUOTAS.claim(
            request_id=f"visual-job:{job.id}:{job.attempts}",
            workspace_id=job.workspace_id,
            owner_id=job.owner_id,
            operation="visual",
        ):
            result = await vision_service.process_bridge_visual_investigation(
                query=str(job.payload["query"]),
                state_code=identity.state_code,
                bridge_number=identity.bridge_number,
                selected_sources=list(job.payload.get("selected_sources") or ()),
                defect_segmentation_mode=str(job.payload["defect_segmentation_mode"]),
                request_context=context,
                limits=limits,
            )
    except AiRequestCancelled as exc:
        if token.durable_requested():
            raise VisualJobCancelled() from exc
        raise VisualJobError("visual_analysis_deadline_exceeded", retry=False) from exc
    except QuotaExceeded as exc:
        raise VisualJobError("visual_worker_quota_exhausted", status=429, retry=True) from exc

    if token.is_set():
        raise VisualJobCancelled()
    public_result = _finite_public_json(result)
    assert isinstance(public_result, dict)
    public_result.update(
        {
            "event_schema_version": "ai-event-v1",
            "request_id": str(job.payload["request_id"]),
            "visual_job_id": job.id,
            "bridge_snapshot": {
                "status": "resolved",
                "identity": identity.public_payload(),
            },
            "execution_mode": "durable_two_phase",
        }
    )
    envelope = {
        "format": VISUAL_RESULT_FORMAT,
        "job_id": job.id,
        "job_payload_sha256": job.payload_sha256,
        "completed_at": _utc_now(),
        "bridge_snapshot": identity.public_payload(),
        "selected_sources": list(job.payload.get("selected_sources") or ()),
        "source_receipts": job.payload.get("source_receipts") or [],
        "source_set_sha256": job.payload["source_set_sha256"],
        "visual_profile_sha256": job.payload["visual_profile_sha256"],
        "result": public_result,
    }
    try:
        raw = json.dumps(
            envelope,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise VisualJobError("visual_result_invalid", status=500) from exc
    if len(raw) > MAX_RESULT_BYTES:
        raise VisualJobError("visual_result_too_large", status=500)
    if progress_callback is not None:
        progress_callback("persist_receipt", 2, 3)
    try:
        receipt = _result_store(artifact_root).ingest(
            raw,
            original_filename=f"visual-result-{job.id}.json",
        )
    except SourceArtifactError as exc:
        raise VisualJobError("visual_result_store_failed", status=503, retry=True) from exc
    if progress_callback is not None:
        progress_callback("complete", 3, 3)
    return {
        "result_receipt": receipt.to_public_dict(),
        "evidence_receipt_sha256": receipt.sha256,
        "source_set_sha256": str(job.payload["source_set_sha256"]),
        "visual_profile_sha256": str(job.payload["visual_profile_sha256"]),
        "snapshot_revision_id": identity.snapshot_revision_id,
        "checked_images": int(public_result.get("checked_images") or 0),
        "detection_count": sum(
            len(item.get("detections") or [])
            for item in public_result.get("detections") or []
            if isinstance(item, Mapping)
        ),
    }


def public_visual_job(job: OperationJob) -> dict[str, object]:
    if job.operation != VISUAL_JOB_OPERATION:
        raise VisualJobError("visual_job_not_found", status=404)
    status = (
        "cancellation_requested"
        if job.status == "running" and job.cancellation_requested_at
        else job.status
    )
    payload: dict[str, object] = {
        "job_id": job.id,
        "operation": job.operation,
        "status": status,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "progress": job.progress,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "cancellation_requested": job.cancellation_requested_at is not None,
        "source_set_sha256": job.payload.get("source_set_sha256"),
        "visual_profile_sha256": job.payload.get("visual_profile_sha256"),
    }
    identity = job.payload.get("bridge_snapshot")
    if isinstance(identity, Mapping):
        payload["bridge_snapshot"] = {
            "state_code": identity.get("state_code"),
            "bridge_number": identity.get("bridge_number"),
            "data_year": identity.get("data_year"),
            "snapshot_revision_id": identity.get("snapshot_revision_id"),
            "canonical_sha256": identity.get("canonical_sha256"),
        }
    if job.error_code:
        payload["error_code"] = job.error_code
    if job.status == "succeeded" and isinstance(job.result, Mapping):
        payload["result_receipt"] = job.result.get("result_receipt")
        payload["evidence_receipt_sha256"] = job.result.get(
            "evidence_receipt_sha256"
        )
        payload["summary"] = {
            "checked_images": job.result.get("checked_images"),
            "detection_count": job.result.get("detection_count"),
        }
    return payload


def get_owned_visual_job(
    job_database_path: str | Path,
    *,
    workspace_id: str,
    owner_id: str,
    job_id: str,
) -> OperationJob:
    job = get_job(
        job_database_path,
        workspace_id=workspace_id,
        owner_id=owner_id,
        job_id=job_id,
    )
    if job.operation != VISUAL_JOB_OPERATION:
        raise VisualJobError("visual_job_not_found", status=404)
    return job


def load_visual_job_result(
    *,
    job: OperationJob,
    artifact_root: str | Path,
) -> dict[str, object]:
    if job.operation != VISUAL_JOB_OPERATION:
        raise VisualJobError("visual_job_not_found", status=404)
    if job.status != "succeeded" or not isinstance(job.result, Mapping):
        raise VisualJobError("visual_result_not_ready", status=409)
    claimed = job.result.get("result_receipt")
    if not isinstance(claimed, Mapping):
        raise VisualJobError("visual_result_receipt_missing", status=500)
    try:
        receipt = _result_store(artifact_root).get(str(claimed.get("artifact_id") or ""))
        for field in ("artifact_id", "sha256", "size_bytes", "original_filename", "created_at"):
            if str(getattr(receipt, field)) != str(claimed.get(field)):
                raise VisualJobError("visual_result_receipt_mismatch", status=409)
        raw = _result_store(artifact_root).read_bytes(receipt.artifact_id)
        envelope = json.loads(raw.decode("utf-8"))
    except VisualJobError:
        raise
    except (SourceArtifactError, UnicodeError, json.JSONDecodeError) as exc:
        raise VisualJobError("visual_result_integrity_failed", status=409) from exc
    if (
        not isinstance(envelope, dict)
        or envelope.get("format") != VISUAL_RESULT_FORMAT
        or envelope.get("job_id") != job.id
        or envelope.get("job_payload_sha256") != job.payload_sha256
        or envelope.get("source_set_sha256") != job.payload.get("source_set_sha256")
        or envelope.get("visual_profile_sha256")
        != job.payload.get("visual_profile_sha256")
        or envelope.get("bridge_snapshot") != job.payload.get("bridge_snapshot")
        or not isinstance(envelope.get("result"), dict)
    ):
        raise VisualJobError("visual_result_integrity_failed", status=409)
    result = dict(envelope["result"])
    result["evidence_receipt"] = receipt.to_public_dict()
    result["evidence_receipt_sha256"] = receipt.sha256
    return result


__all__ = [
    "DurableVisualCancellationToken",
    "MAX_RESULT_BYTES",
    "VISUAL_JOB_OPERATION",
    "VisualJobCancelled",
    "VisualJobError",
    "enqueue_visual_investigation",
    "get_owned_visual_job",
    "load_visual_job_result",
    "normalize_selected_sources",
    "process_visual_investigation_job",
    "public_visual_job",
]
