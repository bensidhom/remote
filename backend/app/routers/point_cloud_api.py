"""Hosted point-cloud API.

The geometry implementation lives in ``services.point_cloud``.  This router owns
HTTP transport and persistence so the hosted application can expose cleaning and
segmentation without importing desktop-only reconstruction or local GPU models.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import struct
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Annotated, Any, Dict, Literal, Mapping, Optional
from uuid import NAMESPACE_URL, uuid4, uuid5

import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .services.datapaths import (
    ATTACHMENTS_DIR,
    DATA_DIR,
    RAG_WORKING_DIR,
    TEMP_DIR,
    USER_DB,
)
from ..services.operations.storage_paths import (
    StoragePathError,
    portable_confined_path,
    resolve_confined_path,
)
from ..services.operations.retention import (
    RetentionError,
    process_attachment_deletion_outbox,
    request_attachment_deletion,
)
from ..services.operations.quotas import (
    DEFAULT_OPERATION_QUOTAS,
    OperationLease,
    QuotaExceeded,
)
from ..services.point_cloud.contracts import (
    GravityStatus,
    PipelineConfigurationError,
    PointCloudPipelineMode,
    SourceIdentityStatus,
    UnitsStatus,
    annotate_point_cloud_result,
    require_available_point_cloud_pipeline,
    resolve_point_cloud_pipeline_mode,
)
from ..services.point_cloud.source_reader import (
    PointCloudSourceError,
    detect_source_format,
    inspect_e57_source,
    parser_capabilities,
    read_e57_source,
    source_ingest_extensions,
)
from ..services.point_cloud.coordinates import (
    CoordinateContractError,
    CoordinateFrame,
    FrameKind,
    GravitySource,
    LinearUnit,
    RegistrationReceipt,
)
from ..services.point_cloud.context import (
    PointCloudCanonicalContext,
    PointCloudContextError,
    PublishedSnapshotRequired,
    resolve_point_cloud_canonical_context,
)
from ..services.point_cloud.knowledge_pack import (
    PointCloudKnowledgePack,
    PointCloudKnowledgePackError,
    build_point_cloud_knowledge_pack,
)
from ..point_cloud_jobs import (
    PointCloudJobError,
    cancel_point_cloud_job,
    enqueue_point_cloud_analysis,
    get_owned_point_cloud_job,
    public_point_cloud_job,
    verify_point_cloud_job_scope,
)
from ..services.operations.jobs import JobStoreError
from ..services.point_cloud.run_store import (
    PointCloudRunStoreError,
    ReviewPointCloudRunRequest,
    get_latest_published_point_cloud_result,
    get_point_cloud_run,
    get_point_cloud_run_evidence,
    review_point_cloud_run,
)
from ..services.point_cloud.clearance import build_feature_measurement_workflow
from ..services.point_cloud.evaluation import (
    PointCloudEvaluationError,
    require_current_release_receipt,
)
from .services.point_cloud import (
    PointCloudError,
    analyze_bridge,
    clean_and_smooth_preview,
    compute_clearance,
    read_e57_preview,
    segment_structure,
)

router = APIRouter()


class _StrictPointCloudModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DurablePointCloudRunBody(_StrictPointCloudModel):
    data_year: int | None = Field(default=None, ge=1900, le=2200)
    model_mode: Literal["clearance", "structural"] = "clearance"
    use_vlm: bool = True


class DurablePointCloudReviewBody(_StrictPointCloudModel):
    decision: Literal["accept", "reject"]
    reason: str = Field(min_length=3, max_length=2000)
    corrections: dict[str, Any] = Field(default_factory=dict)

# Immutable bridge source uploads are intentionally narrower than local browser
# visualization. E57 is the only external format with a reviewed server parser
# in the deployed dependency set. NPZ is an internal derivative format.
POINT_CLOUD_EXTS = {".e57"}
MAX_PC_BYTES = int(os.getenv("BRIDGEGUARD_MAX_POINT_CLOUD_MB", "500")) * 1024 * 1024
MAX_ANALYSIS_BODY_BYTES = int(os.getenv("BRIDGEGUARD_MAX_POINT_BODY_MB", "128")) * 1024 * 1024
_SAFE_BN_RE = re.compile(r"[^A-Za-z0-9_-]")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GENERATION_RE = re.compile(r"^pcv2-(?:inline|[1-9][0-9]*)-[0-9a-f]{32}-[0-9a-f]{12}$")
_PROCESSED_POINTER_FORMAT = "bridgeguard-point-cloud-pointer-v2"
_PROCESSED_MANIFEST_FORMAT = "bridgeguard-point-cloud-analysis-v2"
_SOURCE_RECEIPT_FORMAT = "bridgeguard-point-cloud-source-receipt-v1"
POINT_CLOUD_WORKSPACE_ID = os.getenv(
    "BRIDGEGUARD_WORKSPACE_ID", "shared-research-workspace"
).strip() or "shared-research-workspace"
_POINT_CLOUD_LOCKS = tuple(asyncio.Lock() for _ in range(64))


class _StalePointCloudSource(RuntimeError):
    """The server-owned source identity changed while analysis was running."""


class _PointCloudReplacementRequired(RuntimeError):
    """An audited delete must precede replacement of a current raw object."""


def _active_pipeline_mode() -> PointCloudPipelineMode:
    """Fail before request parsing when the configured engine is unavailable."""

    try:
        return require_available_point_cloud_pipeline()
    except PipelineConfigurationError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "message": exc.public_message},
            headers={"Retry-After": "60"},
        ) from exc


def _point_cloud_evaluation_required() -> bool:
    explicit = os.getenv("BRIDGEGUARD_POINT_CLOUD_REQUIRE_EVALUATION")
    if explicit is not None:
        return explicit.strip().lower() in {"1", "true", "yes", "on"}
    return bool(
        os.getenv("BRIDGEGUARD_DEPLOYMENT_MODE", "research_shared").strip().lower()
        == "dedicated_customer"
        or os.getenv("BRIDGEGUARD_REQUIRE_COMMERCIAL_READY", "").strip().lower()
        in {"1", "true", "yes", "on"}
    )


def _require_point_cloud_evaluation_release() -> None:
    if not _point_cloud_evaluation_required():
        return
    receipt_path = os.getenv("BRIDGEGUARD_POINT_CLOUD_EVALUATION_RECEIPT", "").strip()
    if not receipt_path:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "point_cloud_evaluation_receipt_required",
                "message": "A current point-cloud scientific evaluation receipt is required before publication.",
            },
        )
    try:
        require_current_release_receipt(receipt_path)
    except PointCloudEvaluationError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "message": exc.public_message},
        ) from exc


def _require_durable_bridge_processing() -> None:
    """Retire bridge-scoped synchronous processing without a silent fallback."""

    compatibility = os.getenv(
        "BRIDGEGUARD_POINT_CLOUD_LEGACY_BRIDGE_ROUTES", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    dedicated = bool(
        os.getenv("BRIDGEGUARD_DEPLOYMENT_MODE", "research_shared").strip().lower()
        == "dedicated_customer"
        or os.getenv("BRIDGEGUARD_REQUIRE_COMMERCIAL_READY", "").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    if compatibility and not dedicated:
        return
    raise HTTPException(
        status_code=410,
        detail={
            "code": "point_cloud_bridge_route_retired",
            "message": "Use the durable bridge point-cloud run workflow. Ad-hoc local preview remains available separately.",
        },
        headers={"Cache-Control": "no-store"},
    )


def _legacy_units_status(scale_known: bool) -> UnitsStatus:
    # The current browser flag is an assertion, not a unit/CRS receipt.
    return (
        UnitsStatus.CLIENT_ASSERTED_UNVERIFIED
        if scale_known
        else UnitsStatus.UNKNOWN_OR_RELATIVE
    )


def _legacy_source_status(
    *,
    state_code: Optional[str],
    bridge_number: Optional[str],
    raw_points: Optional[np.ndarray],
    snapshot_identity: Optional[Dict[str, Any]] = None,
) -> SourceIdentityStatus:
    if state_code is None or bridge_number is None:
        return SourceIdentityStatus.INLINE_REQUEST_UNRETAINED
    if raw_points is not None:
        return (
            SourceIdentityStatus.INLINE_REQUEST_SNAPSHOT_BOUND
            if snapshot_identity is not None
            else SourceIdentityStatus.INLINE_REQUEST_UNRETAINED
        )
    return (
        SourceIdentityStatus.SOURCE_HASH_AND_SNAPSHOT_BOUND
        if snapshot_identity is not None
        else SourceIdentityStatus.SOURCE_HASH_BOUND
    )


def _annotate_legacy_result(
    result: Dict[str, Any],
    *,
    pipeline_mode: PointCloudPipelineMode,
    scale_known: bool,
    source_status: SourceIdentityStatus,
) -> Dict[str, Any]:
    annotated = annotate_point_cloud_result(
        result,
        pipeline_mode=pipeline_mode,
        units_status=_legacy_units_status(scale_known),
        gravity_status=GravityStatus.ASSUMED_Z_UP_UNVERIFIED,
        source_identity_status=source_status,
    )
    clearance = annotated.get("clearance")
    if isinstance(clearance, dict):
        annotated["clearance"] = annotate_point_cloud_result(
            clearance,
            pipeline_mode=pipeline_mode,
            units_status=_legacy_units_status(scale_known),
            gravity_status=GravityStatus.ASSUMED_Z_UP_UNVERIFIED,
            source_identity_status=source_status,
        )
    return annotated


@contextmanager
def _sqlite_connection(database, **kwargs):
    """Commit/rollback writes and always release the SQLite file handle."""

    connection = sqlite3.connect(database, **kwargs)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _workspace_token() -> str:
    # Do not leak customer identifiers into filenames, and do not let two
    # identifiers which sanitize to the same text share a storage namespace.
    return "ws-" + hashlib.sha256(POINT_CLOUD_WORKSPACE_ID.encode("utf-8")).hexdigest()[:24]


def _point_cloud_actor(request: Request) -> str:
    user = getattr(request.state, "user", None)
    actor = str(getattr(user, "email", "") or "").strip().lower()
    if not actor or "@" not in actor:
        raise HTTPException(status_code=401, detail="authentication_required")
    return actor


def _point_cloud_job_db() -> Path:
    return Path(USER_DB).parent / "operation_jobs.db"


def _durable_error(exc: Exception) -> HTTPException:
    status = int(getattr(exc, "status", 500))
    code = str(getattr(exc, "code", "point_cloud_workflow_failed"))
    message = str(getattr(exc, "message", "Point-cloud workflow could not complete."))
    if status >= 500:
        message = "Point-cloud workflow is temporarily unavailable."
    headers = {"Retry-After": "5"} if status in {429, 503} else None
    return HTTPException(status_code=status, detail={"code": code, "message": message}, headers=headers)


def _owned_durable_run(request: Request, run_id: str):
    actor = _point_cloud_actor(request)
    try:
        run = get_point_cloud_run(USER_DB, POINT_CLOUD_WORKSPACE_ID, run_id)
        if run.actor_id != actor:
            raise PointCloudRunStoreError(404, "point_cloud_run_not_found", "Point-cloud run not found")
        job = get_owned_point_cloud_job(
            _point_cloud_job_db(),
            workspace_id=POINT_CLOUD_WORKSPACE_ID,
            owner_id=actor,
            job_id=run.job_id,
        )
        return actor, run, job
    except (PointCloudRunStoreError, PointCloudJobError, JobStoreError) as exc:
        raise _durable_error(exc) from exc


def _acquire_point_cloud_quota(request: Request, surface: str) -> OperationLease:
    actor = _point_cloud_actor(request)
    try:
        return DEFAULT_OPERATION_QUOTAS.acquire(
            # A fresh internal ID is deliberate: repeated public request IDs
            # must never alias a live lease and let one request release another.
            request_id=f"point-cloud:{surface}:{uuid4()}",
            workspace_id=POINT_CLOUD_WORKSPACE_ID,
            owner_id=actor,
            operation="point_cloud",
        )
    except QuotaExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail="Point-cloud operation capacity is temporarily exhausted.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc


@asynccontextmanager
async def _point_cloud_quota(request: Request, surface: str):
    lease = _acquire_point_cloud_quota(request, surface)
    try:
        yield lease
    finally:
        DEFAULT_OPERATION_QUOTAS.release(lease)


@asynccontextmanager
async def _bridge_write_lock(state_code: str, bridge_number: str):
    key = f"{POINT_CLOUD_WORKSPACE_ID}\0{state_code}\0{bridge_number}".encode("utf-8")
    lock = _POINT_CLOUD_LOCKS[int.from_bytes(hashlib.sha256(key).digest()[:2], "big") % len(_POINT_CLOUD_LOCKS)]
    async with lock:
        yield


def _safe_bridge_id(state_code: str, bridge_number: str) -> str:
    sc = re.sub(r"[^0-9]", "", str(state_code))
    bn = _SAFE_BN_RE.sub("_", str(bridge_number))
    if not sc or not bn:
        raise ValueError("Invalid state_code or bridge_number")
    return f"{sc}_{bn}"


def _bridge_storage_token(state_code: str, bridge_number: str) -> str:
    sc = re.sub(r"[^0-9]", "", str(state_code))
    bridge = str(bridge_number).strip()
    if not sc or not bridge or len(bridge) > 255 or "\x00" in bridge:
        raise ValueError("Invalid state_code or bridge_number")
    digest = hashlib.sha256(f"{sc}\0{bridge}".encode("utf-8")).hexdigest()
    return f"{sc}-{digest[:32]}"


def _safe_upload_name(filename: str | None, fallback: str = "cloud.ply") -> str:
    basename = Path(filename or fallback).name
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", basename).strip("._")
    return safe[:180] or fallback


def _relative_data_path(filepath: str | Path) -> str:
    try:
        return portable_confined_path(DATA_DIR, filepath)
    except StoragePathError as exc:
        raise ValueError("point-cloud path is outside the application data root") from exc


def _resolve_data_path(filepath: str | Path) -> Path:
    try:
        return resolve_confined_path(DATA_DIR, filepath)
    except StoragePathError as exc:
        raise ValueError("point-cloud path is outside the application data root") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        _json_safe(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_UP_AXIS = {
    "+x": (1.0, 0.0, 0.0),
    "-x": (-1.0, 0.0, 0.0),
    "+y": (0.0, 1.0, 0.0),
    "-y": (0.0, -1.0, 0.0),
    "+z": (0.0, 0.0, 1.0),
    "-z": (0.0, 0.0, -1.0),
}


def _source_receipt_path(source_path: Path) -> Path:
    return _resolve_data_path(source_path.with_name(source_path.name + ".source.json"))


def _source_declaration(
    *,
    source_sha256: str,
    actor_id: str,
    horizontal_unit: str,
    vertical_unit: str,
    frame_kind: str,
    crs: str,
    up_axis: str,
    evidence: str,
    reviewed: bool,
) -> tuple[CoordinateFrame, dict[str, Any]]:
    evidence = str(evidence or "").strip()
    try:
        horizontal = LinearUnit(str(horizontal_unit).strip().lower())
        vertical = LinearUnit(str(vertical_unit).strip().lower())
        kind = FrameKind(str(frame_kind).strip().lower())
        up = _UP_AXIS[str(up_axis).strip().lower()]
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "point_cloud_source_declaration_invalid",
                "message": "Point-cloud unit, frame, or up-axis declaration is invalid.",
            },
        ) from exc
    if reviewed:
        if len(evidence) < 3:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "point_cloud_source_evidence_required",
                    "message": "A reviewed source declaration requires a short evidence reference.",
                },
            )
        evidence_id = hashlib.sha256(
            json.dumps(
                {
                    "source_sha256": source_sha256,
                    "actor": actor_id.strip().lower(),
                    "horizontal_unit": horizontal.value,
                    "vertical_unit": vertical.value,
                    "frame_kind": kind.value,
                    "crs": str(crs or "").strip() or None,
                    "up_axis": str(up_axis).strip().lower(),
                    "evidence": evidence,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        gravity_source = GravitySource.EDITOR_REVIEWED
    else:
        evidence_id = None
        gravity_source = GravitySource.ASSUMED if up_axis else GravitySource.UNKNOWN
    try:
        frame = CoordinateFrame(
            kind=kind,
            horizontal_unit=horizontal,
            vertical_unit=vertical,
            up_vector=up,
            gravity_source=gravity_source,
            crs=str(crs or "").strip() or None,
            metadata_source=("editor declaration" if reviewed else "unreviewed client declaration"),
            review_evidence_id=evidence_id,
        )
        if reviewed:
            frame.require_metric_ready()
    except CoordinateContractError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": exc.public_message},
        ) from exc
    internal = {
        "frame": frame.to_dict(),
        "reviewed": bool(reviewed),
        "evidence_summary": evidence[:300] if reviewed else None,
        "reviewed_by_sha256": (
            hashlib.sha256(actor_id.strip().lower().encode("utf-8")).hexdigest()
            if reviewed
            else None
        ),
    }
    return frame, internal


def _write_source_receipt(
    *,
    source_path: Path,
    source_sha256: str,
    source_size_bytes: int,
    source_format: str,
    declared_filename: str,
    actor_id: str,
    horizontal_unit: str,
    vertical_unit: str,
    frame_kind: str,
    crs: str,
    up_axis: str,
    evidence: str,
    reviewed: bool,
    selected_scan_indices: str,
    registration_reference_scan: int | None,
    registration_rms_metres: float | None,
    registration_maximum_residual_metres: float | None,
    registration_method: str,
    registration_reviewed: bool,
) -> tuple[Path, dict[str, Any]]:
    frame, declaration = _source_declaration(
        source_sha256=source_sha256,
        actor_id=actor_id,
        horizontal_unit=horizontal_unit,
        vertical_unit=vertical_unit,
        frame_kind=frame_kind,
        crs=crs,
        up_axis=up_axis,
        evidence=evidence,
        reviewed=reviewed,
    )
    capability = next(
        item for item in parser_capabilities() if item.format.value == source_format
    )
    inspection = None
    selected: tuple[int, ...] = ()
    registration = None
    inspection_issues: list[str] = []
    if capability.server_available and source_format == "e57":
        try:
            inspection = inspect_e57_source(
                source_path, declared_filename=declared_filename
            )
        except PointCloudSourceError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": exc.code, "message": exc.public_message},
            ) from exc
        raw_selection = str(selected_scan_indices or "").strip()
        if raw_selection:
            try:
                selected = tuple(int(value.strip()) for value in raw_selection.split(","))
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "point_cloud_scan_selection_invalid",
                        "message": "Selected scans must be a comma-separated list of integer indices.",
                    },
                ) from exc
        elif inspection.total_scan_count == 1:
            selected = (0,)
        else:
            inspection_issues.append("scan_selection_required")
        if selected and (
            len(set(selected)) != len(selected)
            or any(value < 0 or value >= inspection.total_scan_count for value in selected)
        ):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "point_cloud_scan_selection_invalid",
                    "message": "Selected scans are duplicated or outside the E57 scan range.",
                },
            )
        if len(selected) > 1:
            try:
                reference = int(
                    registration_reference_scan
                    if registration_reference_scan is not None
                    else selected[0]
                )
                registration = RegistrationReceipt(
                    selected_scan_indices=selected,
                    reference_scan_index=reference,
                    rms_metres=float(registration_rms_metres),
                    maximum_residual_metres=float(
                        registration_maximum_residual_metres
                    ),
                    method=str(registration_method or "").strip(),
                    transform_sha256=inspection.registration_transform_sha256,
                    reviewed=bool(registration_reviewed),
                )
                if not registration.method:
                    raise CoordinateContractError(
                        "registration_method_required",
                        "A multi-scan registration requires a method/evidence description.",
                    )
                registration.require_accepted()
            except (TypeError, ValueError, CoordinateContractError) as exc:
                code = getattr(exc, "code", "registration_evidence_required")
                message = getattr(
                    exc,
                    "public_message",
                    "A multi-scan merge requires reviewed RMS and residual evidence.",
                )
                raise HTTPException(
                    status_code=422, detail={"code": code, "message": message}
                ) from exc
    else:
        inspection_issues.append("reviewed_server_parser_unavailable")
    receipt: dict[str, Any] = {
        "format": _SOURCE_RECEIPT_FORMAT,
        "source_sha256": source_sha256,
        "source_size_bytes": int(source_size_bytes),
        "source_format": source_format,
        "declared_filename": _safe_upload_name(declared_filename, "cloud.e57"),
        "parser_capability": capability.to_dict(),
        "coordinate_declaration": declaration,
        "source_inspection": inspection.to_dict() if inspection else None,
        "selected_scan_indices": list(selected),
        "registration": registration.to_dict() if registration else None,
        "inspection_issues": inspection_issues,
        "metric_processing_authorized": bool(
            frame.metric_ready
            and capability.server_available
            and selected
            and (len(selected) == 1 or registration is not None)
        ),
    }
    receipt["receipt_sha256"] = _canonical_sha256(receipt)
    path = _source_receipt_path(source_path)
    _atomic_write_bytes(
        path,
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )
    return path, receipt


def _load_source_receipt(row) -> dict[str, Any] | None:
    if row is None:
        return None
    try:
        _attachment_id, source_sha256 = _source_identity(row)
        source_path = _resolve_data_path(row[1])
        payload = json.loads(_source_receipt_path(source_path).read_text(encoding="utf-8"))
        receipt_hash = str(payload.pop("receipt_sha256", ""))
        if (
            payload.get("format") != _SOURCE_RECEIPT_FORMAT
            or payload.get("source_sha256") != source_sha256
            or receipt_hash != _canonical_sha256(payload)
        ):
            return None
        payload["receipt_sha256"] = receipt_hash
        return payload
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _public_source_receipt(receipt: dict[str, Any] | None) -> dict[str, Any] | None:
    if receipt is None:
        return None
    declaration = dict(receipt.get("coordinate_declaration") or {})
    declaration.pop("reviewed_by_sha256", None)
    return {
        "format": receipt.get("format"),
        "source_format": receipt.get("source_format"),
        "parser_capability": receipt.get("parser_capability"),
        "coordinate_declaration": declaration,
        "source_inspection": receipt.get("source_inspection"),
        "selected_scan_indices": receipt.get("selected_scan_indices"),
        "registration": receipt.get("registration"),
        "inspection_issues": receipt.get("inspection_issues"),
        "metric_processing_authorized": bool(receipt.get("metric_processing_authorized")),
        "receipt_sha256": receipt.get("receipt_sha256"),
    }


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temp_path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _atomic_replace_path(source: Path, destination: Path) -> None:
    """Replace a path atomically, including long absolute paths on Windows."""

    source_text = os.path.abspath(os.fspath(source))
    destination_text = os.path.abspath(os.fspath(destination))
    if os.name == "nt":
        if not source_text.startswith("\\\\?\\"):
            source_text = "\\\\?\\" + source_text
        if not destination_text.startswith("\\\\?\\"):
            destination_text = "\\\\?\\" + destination_text
    os.replace(source_text, destination_text)


def _bridge_cloud_root(state_code: str, bridge_number: str) -> Path:
    root = (
        Path(ATTACHMENTS_DIR)
        / "point_clouds"
        / _workspace_token()
        / _bridge_storage_token(state_code, bridge_number)
    )
    return _resolve_data_path(root)


def _source_identity(row) -> tuple[int, str]:
    if row is None:
        raise ValueError("Saved point cloud is unavailable.")
    try:
        attachment_id = int(row[0])
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError("Saved point cloud has invalid identity metadata.") from exc
    digest = str(row[5] or "").strip().lower()
    if attachment_id < 1 or not _SHA256_RE.fullmatch(digest):
        raise ValueError("Saved point cloud has no verified SHA-256; re-upload it before analysis.")
    return attachment_id, digest


def _source_generation(row) -> str:
    if not row:
        return "legacy"
    attachment_id, digest = _source_identity(row)
    return f"{attachment_id}-{digest[:20]}"


def _processed_paths(state_code: str, bridge_number: str, generation: str):
    if generation != "legacy" and not _GENERATION_RE.fullmatch(generation):
        raise ValueError("Processed point-cloud generation is invalid.")
    root = _bridge_cloud_root(state_code, bridge_number) / "processed" / generation
    root = _resolve_data_path(root)
    return root, root / "processed_cloud.npz", root / "processed_cloud.ply", root / "analysis.json"


def _processed_pointer_path(state_code: str, bridge_number: str) -> Path:
    return _resolve_data_path(
        _bridge_cloud_root(state_code, bridge_number) / "processed" / "current.json"
    )


def _normalize_snapshot_identity(
    snapshot_identity: Optional[Dict[str, Any]],
    *,
    state_code: str,
    bridge_number: str,
) -> Optional[Dict[str, Any]]:
    if snapshot_identity is None:
        return None
    try:
        normalized = {
            "workspace_id": str(snapshot_identity["workspace_id"]),
            "state_code": int(snapshot_identity["state_code"]),
            "bridge_number": str(snapshot_identity["bridge_number"]),
            "data_year": int(snapshot_identity["data_year"]),
            "snapshot_revision_id": str(snapshot_identity["snapshot_revision_id"]),
            "revision_number": int(snapshot_identity["revision_number"]),
            "canonical_sha256": str(snapshot_identity["canonical_sha256"]).strip().lower(),
            "profile_id": str(snapshot_identity.get("profile_id") or ""),
            "rule_bundle_sha256": str(snapshot_identity.get("rule_bundle_sha256") or "").strip().lower(),
            "published_at": str(snapshot_identity.get("published_at") or ""),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Published snapshot identity is invalid.") from exc
    if (
        normalized["workspace_id"] != POINT_CLOUD_WORKSPACE_ID
        or normalized["state_code"] != int(state_code)
        or normalized["bridge_number"] != str(bridge_number)
        or normalized["data_year"] < 1900
        or normalized["revision_number"] < 1
        or not normalized["snapshot_revision_id"]
        or not _SHA256_RE.fullmatch(normalized["canonical_sha256"])
        or (
            normalized["rule_bundle_sha256"]
            and not _SHA256_RE.fullmatch(normalized["rule_bundle_sha256"])
        )
    ):
        raise ValueError("Published snapshot identity is invalid.")
    return normalized


def _latest_published_snapshot_identity(
    state_code: str,
    bridge_number: str,
    data_year: int | None = None,
) -> Optional[Dict[str, Any]]:
    """Compatibility projection over the shared verified canonical boundary."""

    try:
        resolved = resolve_point_cloud_canonical_context(
            USER_DB,
            workspace_id=POINT_CLOUD_WORKSPACE_ID,
            state_code=int(state_code),
            bridge_number=str(bridge_number),
            data_year=data_year,
        )
    except PublishedSnapshotRequired:
        return None
    except (PointCloudContextError, TypeError, ValueError) as exc:
        raise ValueError("Published snapshot identity could not be verified.") from exc
    return resolved.identity.public_payload()


def _generation_key(
    *,
    source_attachment_id: Optional[int],
    source_sha256: str,
    analysis_profile_sha256: str,
    snapshot_identity: Optional[Dict[str, Any]],
) -> str:
    return _canonical_sha256(
        {
            "workspace_id": POINT_CLOUD_WORKSPACE_ID,
            "source_attachment_id": source_attachment_id,
            "source_sha256": source_sha256,
            "analysis_profile_sha256": analysis_profile_sha256,
            "snapshot_identity": snapshot_identity,
        }
    )


def _active_processed_record(state_code: str, bridge_number: str):
    pointer_path = _processed_pointer_path(state_code, bridge_number)
    if not pointer_path.is_file():
        raise ValueError("Processed point-cloud pointer does not exist.")
    pointer_bytes = pointer_path.read_bytes()
    if len(pointer_bytes) > 64 * 1024:
        raise ValueError("Processed point-cloud pointer is invalid.")
    pointer = json.loads(pointer_bytes)
    generation = str(pointer.get("source_generation") or "")
    if (
        pointer.get("format") != _PROCESSED_POINTER_FORMAT
        or pointer.get("workspace_id") != POINT_CLOUD_WORKSPACE_ID
        or not _GENERATION_RE.fullmatch(generation)
    ):
        raise ValueError("Processed point-cloud pointer is invalid.")
    root, npz_path, ply_path, analysis_path = _processed_paths(
        state_code, bridge_number, generation
    )
    if not root.is_dir() or not analysis_path.is_file():
        raise ValueError("Processed point-cloud generation is incomplete.")
    analysis_bytes = analysis_path.read_bytes()
    manifest_sha256 = str(pointer.get("manifest_sha256") or "").lower()
    if (
        not _SHA256_RE.fullmatch(manifest_sha256)
        or hashlib.sha256(analysis_bytes).hexdigest() != manifest_sha256
    ):
        raise ValueError("Processed point-cloud manifest failed integrity validation.")
    result = json.loads(analysis_bytes)
    persistence = result.get("persistence")
    if not isinstance(persistence, dict):
        raise ValueError("Processed point-cloud manifest is invalid.")
    source_sha256 = str(persistence.get("source_sha256") or "").lower()
    profile_sha256 = str(persistence.get("analysis_profile_sha256") or "").lower()
    generation_key_sha256 = str(persistence.get("generation_key_sha256") or "").lower()
    artifact_sha256 = persistence.get("artifact_sha256")
    source_attachment_id = persistence.get("source_attachment_id")
    if (
        result.get("manifest_format") != _PROCESSED_MANIFEST_FORMAT
        or persistence.get("workspace_id") != POINT_CLOUD_WORKSPACE_ID
        or persistence.get("source_generation") != generation
        or pointer.get("source_attachment_id") != persistence.get("source_attachment_id")
        or pointer.get("source_sha256") != source_sha256
        or pointer.get("analysis_profile_sha256") != profile_sha256
        or pointer.get("generation_key_sha256") != generation_key_sha256
        or pointer.get("snapshot_identity_sha256")
        != persistence.get("snapshot_identity_sha256")
        or not _SHA256_RE.fullmatch(source_sha256)
        or not _SHA256_RE.fullmatch(profile_sha256)
        or not _SHA256_RE.fullmatch(generation_key_sha256)
        or not isinstance(artifact_sha256, dict)
        or not _SHA256_RE.fullmatch(str(artifact_sha256.get("npz") or "").lower())
        or not _SHA256_RE.fullmatch(str(artifact_sha256.get("ply") or "").lower())
        or (
            source_attachment_id is not None
            and (
                isinstance(source_attachment_id, bool)
                or not isinstance(source_attachment_id, int)
                or source_attachment_id < 1
            )
        )
    ):
        raise ValueError("Processed point-cloud pointer and manifest do not match.")
    profile = persistence.get("analysis_profile")
    if _canonical_sha256(profile if isinstance(profile, dict) else {}) != profile_sha256:
        raise ValueError("Processed point-cloud analysis profile is invalid.")
    knowledge_pack = persistence.get("knowledge_pack")
    expected_knowledge_pack_sha256 = (
        str(profile.get("knowledge_pack_sha256") or "").lower()
        if isinstance(profile, dict)
        else ""
    )
    if expected_knowledge_pack_sha256:
        knowledge_pack_path = root / "knowledge_pack.json"
        if (
            not _SHA256_RE.fullmatch(expected_knowledge_pack_sha256)
            or not isinstance(knowledge_pack, dict)
            or knowledge_pack.get("sha256") != expected_knowledge_pack_sha256
            or not isinstance(knowledge_pack.get("byte_size"), int)
            or knowledge_pack["byte_size"] < 1
            or knowledge_pack["byte_size"] > 64 * 1024
            or artifact_sha256.get("knowledge_pack")
            != expected_knowledge_pack_sha256
            or not knowledge_pack_path.is_file()
        ):
            raise ValueError("Processed point-cloud knowledge pack is invalid.")
        knowledge_pack_bytes = knowledge_pack_path.read_bytes()
        if (
            len(knowledge_pack_bytes) != knowledge_pack["byte_size"]
            or hashlib.sha256(knowledge_pack_bytes).hexdigest()
            != expected_knowledge_pack_sha256
        ):
            raise ValueError(
                "Processed point-cloud knowledge pack failed integrity validation."
            )
        try:
            knowledge_pack_payload = json.loads(knowledge_pack_bytes)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("Processed point-cloud knowledge pack is invalid.") from exc
        if (
            not isinstance(knowledge_pack_payload, dict)
            or knowledge_pack_payload.get("schema_version")
            != knowledge_pack.get("schema_version")
        ):
            raise ValueError("Processed point-cloud knowledge pack is invalid.")
    elif knowledge_pack is not None or "knowledge_pack" in artifact_sha256:
        raise ValueError("Processed point-cloud knowledge pack is not profile-bound.")
    snapshot_identity = _normalize_snapshot_identity(
        persistence.get("snapshot_identity"),
        state_code=state_code,
        bridge_number=bridge_number,
    )
    snapshot_sha256 = _canonical_sha256(snapshot_identity)
    if persistence.get("snapshot_identity_sha256") != snapshot_sha256:
        raise ValueError("Processed point-cloud snapshot identity is invalid.")
    expected_generation_key = _generation_key(
        source_attachment_id=persistence.get("source_attachment_id"),
        source_sha256=source_sha256,
        analysis_profile_sha256=profile_sha256,
        snapshot_identity=snapshot_identity,
    )
    if expected_generation_key != generation_key_sha256:
        raise ValueError("Processed point-cloud generation identity is invalid.")
    expected_attachment_token = (
        str(source_attachment_id) if source_attachment_id is not None else "inline"
    )
    if not generation.startswith(f"pcv2-{expected_attachment_token}-"):
        raise ValueError("Processed point-cloud generation identity is invalid.")
    if source_attachment_id is not None:
        current_row = _raw_cloud_row(state_code, bridge_number)
        try:
            current_identity = (
                _source_identity(current_row) if current_row is not None else None
            )
        except ValueError as exc:
            raise ValueError("Processed point-cloud source is no longer current.") from exc
        if current_identity != (source_attachment_id, source_sha256):
            raise ValueError("Processed point-cloud source is no longer current.")
    expected_ply_path = _relative_data_path(ply_path)
    if persistence.get("point_cloud_file") != expected_ply_path:
        raise ValueError("Processed point-cloud artifact path is invalid.")
    if not npz_path.is_file() or not ply_path.is_file():
        raise ValueError("Processed point-cloud generation is incomplete.")
    return (
        root,
        npz_path,
        ply_path,
        analysis_path,
        result,
        pointer,
    )


def _active_processed_paths(state_code: str, bridge_number: str):
    root, npz_path, ply_path, analysis_path, _result, _pointer = (
        _active_processed_record(state_code, bridge_number)
    )
    return root, npz_path, ply_path, analysis_path


def _invalidate_processed_pointer(state_code: str, bridge_number: str) -> None:
    """Make every prior processed generation undiscoverable before source mutation."""

    pointer_path = _processed_pointer_path(state_code, bridge_number)
    pointer_path.unlink(missing_ok=True)


def _remove_processed(state_code: str, bridge_number: str) -> None:
    try:
        _invalidate_processed_pointer(state_code, bridge_number)
        root = _bridge_cloud_root(state_code, bridge_number) / "processed"
        if root.is_dir():
            shutil.rmtree(root)
    except (OSError, ValueError):
        pass


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, np.ndarray):
        return _json_safe(value.tolist())
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _write_binary_ply(path: Path, points: np.ndarray, colors: Optional[np.ndarray]) -> None:
    xyz = np.asarray(points, dtype=np.float32)[:, :3]
    rgb = None
    if colors is not None:
        rgb = np.clip(np.asarray(colors, dtype=np.float64)[:, :3] * 255.0, 0, 255).astype(np.uint8)
    fields = [("x", "<f4"), ("y", "<f4"), ("z", "<f4")]
    if rgb is not None:
        fields += [("red", "u1"), ("green", "u1"), ("blue", "u1")]
    rows = np.empty(len(xyz), dtype=np.dtype(fields))
    rows["x"], rows["y"], rows["z"] = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    if rgb is not None:
        rows["red"], rows["green"], rows["blue"] = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    header = [
        "ply",
        "format binary_little_endian 1.0",
        f"element vertex {len(rows)}",
        "property float x",
        "property float y",
        "property float z",
    ]
    if rgb is not None:
        header += ["property uchar red", "property uchar green", "property uchar blue"]
    header += ["end_header", ""]
    temp_path = path.with_suffix(".tmp")
    with temp_path.open("wb") as handle:
        handle.write("\n".join(header).encode("ascii"))
        rows.tofile(handle)
    temp_path.replace(path)


def _save_processed(
    state_code: str,
    bridge_number: str,
    world_points: np.ndarray,
    result: Dict[str, Any],
    *,
    source_row=None,
    analysis_profile: Optional[Dict[str, Any]] = None,
    snapshot_identity: Optional[Dict[str, Any]] = None,
    knowledge_pack_json: str | None = None,
    publish_pointer: bool = True,
) -> Dict[str, Any]:
    encoded = str(result.get("labels_b64u8") or "")
    labels = np.frombuffer(base64.b64decode(encoded), dtype=np.uint8) if encoded else np.empty(0, np.uint8)
    points = np.asarray(world_points, dtype=np.float32)[:, :3]
    if source_row is not None:
        source_attachment_id, source_sha256 = _source_identity(source_row)
    else:
        source_attachment_id = None
        source_sha256 = hashlib.sha256(
            np.ascontiguousarray(points).tobytes()
        ).hexdigest()
    profile = _json_safe(analysis_profile or {})
    if not isinstance(profile, dict):
        raise ValueError("Analysis profile is invalid.")
    profile_sha256 = _canonical_sha256(profile)
    knowledge_pack_bytes: bytes | None = None
    knowledge_pack_sha256: str | None = None
    parsed_knowledge_pack: dict[str, Any] | None = None
    if knowledge_pack_json is not None:
        knowledge_pack_bytes = knowledge_pack_json.encode("utf-8")
        if not knowledge_pack_bytes or len(knowledge_pack_bytes) > 64 * 1024:
            raise ValueError("Point-cloud knowledge pack is invalid.")
        try:
            parsed_knowledge_pack = json.loads(knowledge_pack_bytes)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("Point-cloud knowledge pack is invalid.") from exc
        if not isinstance(parsed_knowledge_pack, dict):
            raise ValueError("Point-cloud knowledge pack is invalid.")
        knowledge_pack_sha256 = hashlib.sha256(knowledge_pack_bytes).hexdigest()
        if profile.get("knowledge_pack_sha256") != knowledge_pack_sha256:
            raise ValueError(
                "Point-cloud knowledge pack does not match the analysis profile."
            )
    normalized_snapshot = _normalize_snapshot_identity(
        snapshot_identity,
        state_code=state_code,
        bridge_number=bridge_number,
    )
    snapshot_sha256 = _canonical_sha256(normalized_snapshot)
    generation_key_sha256 = _generation_key(
        source_attachment_id=source_attachment_id,
        source_sha256=source_sha256,
        analysis_profile_sha256=profile_sha256,
        snapshot_identity=normalized_snapshot,
    )
    attachment_token = str(source_attachment_id) if source_attachment_id else "inline"
    generation = (
        f"pcv2-{attachment_token}-{generation_key_sha256[:32]}-{uuid4().hex[:12]}"
    )
    root, npz_path, ply_path, analysis_path = _processed_paths(
        state_code, bridge_number, generation
    )
    processed_root = _resolve_data_path(root.parent)
    processed_root.mkdir(parents=True, exist_ok=True)
    staging_root = _resolve_data_path(
        processed_root / f".{generation}.{uuid4().hex}.staging"
    )
    staging_npz = staging_root / npz_path.name
    staging_ply = staging_root / ply_path.name
    staging_analysis = staging_root / analysis_path.name
    staging_knowledge_pack = staging_root / "knowledge_pack.json"
    if labels.size != points.shape[0]:
        raise ValueError("Analysis labels are not aligned with the source point cloud.")
    kept = labels > 0
    if not np.any(kept):
        raise ValueError("Analysis did not retain any bridge points.")
    cleaned_points = points[kept]
    cleaned_labels = labels[kept]
    palette = {
        int(item.get("id")): item.get("color_rgb", [0.6, 0.6, 0.6])
        for item in result.get("classes", [])
    }
    cleaned_colors = np.asarray(
        [palette.get(int(label), [0.6, 0.6, 0.6]) for label in cleaned_labels],
        dtype=np.float32,
    )

    staging_root.mkdir(parents=False, exist_ok=False)
    try:
        npz_tmp = staging_npz.with_suffix(".tmp.npz")
        np.savez_compressed(
            npz_tmp,
            points=cleaned_points,
            colors=cleaned_colors,
            labels=cleaned_labels,
        )
        npz_tmp.replace(staging_npz)
        _write_binary_ply(staging_ply, cleaned_points, cleaned_colors)
        artifact_sha256 = {
            "npz": _sha256_file(staging_npz),
            "ply": _sha256_file(staging_ply),
        }
        knowledge_pack_receipt = None
        if knowledge_pack_bytes is not None and knowledge_pack_sha256 is not None:
            _atomic_write_bytes(staging_knowledge_pack, knowledge_pack_bytes)
            artifact_sha256["knowledge_pack"] = _sha256_file(
                staging_knowledge_pack
            )
            knowledge_pack_receipt = {
                "sha256": knowledge_pack_sha256,
                "byte_size": len(knowledge_pack_bytes),
                "schema_version": parsed_knowledge_pack.get("schema_version"),
            }
        saved = _json_safe({k: v for k, v in result.items() if k != "persistence"})
        saved["manifest_format"] = _PROCESSED_MANIFEST_FORMAT
        saved["labels_b64u8"] = base64.b64encode(cleaned_labels.tobytes()).decode("ascii")
        saved["persistence"] = {
            "saved": True,
            "analysis_version": 2,
            "workspace_id": POINT_CLOUD_WORKSPACE_ID,
            "source_generation": generation,
            "source_attachment_id": source_attachment_id,
            "source_sha256": source_sha256,
            "analysis_profile": profile,
            "analysis_profile_sha256": profile_sha256,
            "snapshot_identity": normalized_snapshot,
            "snapshot_identity_sha256": snapshot_sha256,
            "generation_key_sha256": generation_key_sha256,
            "artifact_sha256": artifact_sha256,
            "knowledge_pack": knowledge_pack_receipt,
            "source_point_count": int(points.shape[0]),
            "processed_point_count": int(cleaned_points.shape[0]),
            "point_cloud_file": _relative_data_path(ply_path),
        }
        manifest_bytes = json.dumps(
            saved,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        _atomic_write_bytes(staging_analysis, manifest_bytes)
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()

        # The generation is never mutated after this same-filesystem rename.
        # Durable research runs leave it undiscoverable to legacy readers until
        # the separate human-review publication boundary accepts the run.
        _atomic_replace_path(staging_root, root)
        pointer = {
            "format": _PROCESSED_POINTER_FORMAT,
            "workspace_id": POINT_CLOUD_WORKSPACE_ID,
            "source_generation": generation,
            "source_attachment_id": source_attachment_id,
            "source_sha256": source_sha256,
            "analysis_profile_sha256": profile_sha256,
            "snapshot_identity_sha256": snapshot_sha256,
            "generation_key_sha256": generation_key_sha256,
            "manifest_sha256": manifest_sha256,
        }
        if publish_pointer:
            _atomic_write_bytes(
                _processed_pointer_path(state_code, bridge_number),
                json.dumps(
                    pointer,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode("utf-8"),
            )
        returned = dict(saved["persistence"])
        returned["manifest_sha256"] = manifest_sha256
        returned["published_to_legacy_pointer"] = bool(publish_pointer)
        return returned
    finally:
        if staging_root.is_dir():
            shutil.rmtree(staging_root, ignore_errors=True)


def _load_processed(state_code: str, bridge_number: str) -> Optional[Dict[str, Any]]:
    try:
        _root, _npz_path, ply_path, _analysis_path, result, pointer = (
            _active_processed_record(state_code, bridge_number)
        )
        result.setdefault("persistence", {})
        result["persistence"].update(
            {
                "saved": True,
                "point_cloud_file": _relative_data_path(ply_path),
                "manifest_sha256": pointer["manifest_sha256"],
            }
        )
        return result
    except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error):
        return None


def _processed_preview(state_code: str, bridge_number: str, as_arrays: bool = False) -> Optional[Dict]:
    try:
        _root, npz_path, _ply, _analysis, result, _pointer = (
            _active_processed_record(state_code, bridge_number)
        )
        expected_npz_sha256 = result["persistence"]["artifact_sha256"]["npz"]
        if _sha256_file(npz_path) != expected_npz_sha256:
            raise ValueError("Processed point-cloud artifact failed integrity validation.")
        with np.load(npz_path, allow_pickle=False) as saved:
            world = np.asarray(saved["points"], dtype=np.float32)[:, :3]
            colors = np.asarray(saved["colors"], dtype=np.float32)[:, :3] if "colors" in saved.files else None
        if world.size == 0:
            return None
        center = world.mean(axis=0)
        mins, maxs = world.min(axis=0), world.max(axis=0)
        return {
            "status": "success",
            "source": "saved_processed_analysis",
            "original_point_count": int(world.shape[0]),
            "rendered_point_count": int(world.shape[0]),
            "downsampled": False,
            "bounds": {"min": mins.tolist(), "max": maxs.tolist(), "center": center.tolist()},
            "points": (world - center).astype(np.float32) if as_arrays else (world - center).round(5).tolist(),
            "colors": colors if as_arrays else (colors.round(4).tolist() if colors is not None else None),
        }
    except (OSError, ValueError, KeyError, sqlite3.Error):
        return None


def _frame_cloud_response(payload: Dict[str, Any]) -> Response:
    values = dict(payload)
    points = np.ascontiguousarray(np.asarray(values.pop("points"), dtype="<f4"))
    colors = values.pop("colors", None)
    header = json.dumps(
        _json_safe({**values, "n": int(points.shape[0]), "has_colors": colors is not None})
    ).encode("utf-8")
    header += b" " * (-(4 + len(header)) % 4)
    body = struct.pack("<I", len(header)) + header + points.tobytes()
    if colors is not None:
        body += np.clip(np.asarray(colors, dtype=np.float64) * 255.0, 0, 255).astype(np.uint8).tobytes()
    return Response(content=body, media_type="application/octet-stream")


async def _stream_upload(file: UploadFile, destination: Path) -> tuple[int, str]:
    destination = _resolve_data_path(destination)
    if destination.exists():
        raise ValueError("Point-cloud object destination already exists.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(f".{destination.name}.{uuid4().hex}.uploading")
    size = 0
    digest = hashlib.sha256()
    try:
        with temp_path.open("xb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_PC_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Point-cloud file exceeds the {MAX_PC_BYTES // (1024 * 1024)} MB upload limit.",
                    )
                handle.write(chunk)
                digest.update(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="Uploaded point-cloud file is empty.")
        temp_path.replace(destination)
        return size, digest.hexdigest()
    finally:
        temp_path.unlink(missing_ok=True)


async def _read_body_limited(request: Request) -> bytes:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_ANALYSIS_BODY_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Point payload exceeds the {MAX_ANALYSIS_BODY_BYTES // (1024 * 1024)} MB limit.",
            )
    return bytes(raw)


def _param_bool(params: Dict[str, Any], key: str, default: bool) -> bool:
    value = params.get(key)
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _param_origin(params: Dict[str, Any]) -> np.ndarray:
    value = params.get("origin")
    if not value:
        return np.zeros(3, dtype=np.float64)
    if isinstance(value, str):
        try:
            value = [float(part) for part in value.split(",")]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="`origin` must be 'x,y,z'.") from exc
    origin = np.asarray(value, dtype=np.float64)
    if origin.size < 3 or not np.isfinite(origin[:3]).all():
        raise HTTPException(status_code=400, detail="`origin` must contain three finite values.")
    return origin[:3]


async def _cloud_payload(request: Request):
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    raw = await _read_body_limited(request)
    if content_type == "application/octet-stream":
        if len(raw) % 4:
            raise HTTPException(status_code=400, detail="Binary body must be float32-aligned.")
        values = np.frombuffer(raw, dtype="<f4")
        params = dict(request.query_params)
        colors = None
        if _param_bool(params, "has_colors", False):
            if values.size % 6:
                raise HTTPException(status_code=400, detail="xyz+rgb body must hold N*6 float32 values.")
            n_points = values.size // 6
            points = values[: n_points * 3].astype(np.float64).reshape(-1, 3)
            colors = values[n_points * 3 :].astype(np.float64).reshape(-1, 3)
        else:
            if values.size % 3:
                raise HTTPException(status_code=400, detail="xyz body must hold N*3 float32 values.")
            points = values.astype(np.float64).reshape(-1, 3)
        return (points if points.size else None), colors, params

    try:
        body = json.loads(raw or b"{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON point payload.") from exc
    points_value = body.get("points")
    points = np.asarray(points_value, dtype=np.float64) if points_value else None
    if points is not None:
        if points.ndim != 2 or points.shape[1] < 3:
            raise HTTPException(status_code=400, detail="`points` must be an (N,3) array.")
        points = points[:, :3]
    colors_value = body.get("colors")
    colors = np.asarray(colors_value, dtype=np.float64)[:, :3] if colors_value else None
    return points, colors, body


def _require_points(points: Optional[np.ndarray], params: Dict[str, Any], to_world: bool) -> np.ndarray:
    if points is None:
        raise HTTPException(
            status_code=400,
            detail="Request must include points (binary body or JSON `points`).",
        )
    return points + _param_origin(params) if to_world else points


def _raw_cloud_row(state_code: str, bridge_number: str):
    with _sqlite_connection(
        f"file:{USER_DB}?mode=ro", uri=True, timeout=5
    ) as conn:
        return conn.execute(
            """
            SELECT id, filepath, filename, size_bytes, uploaded_at, sha256
            FROM bridge_attachments
            WHERE workspace_id=? AND state_code=? AND bridge_number=?
              AND attachment_type='point_cloud'
            ORDER BY id DESC LIMIT 1
            """,
            (POINT_CLOUD_WORKSPACE_ID, state_code, bridge_number),
        ).fetchone()


def _verified_raw_path(row) -> Path:
    _attachment_id, expected_sha256 = _source_identity(row)
    filepath = _resolve_data_path(row[1])
    if not filepath.is_file() or _sha256_file(filepath) != expected_sha256:
        raise ValueError("Saved point cloud failed integrity validation.")
    return filepath


async def _analysis_points(
    state_code: str,
    bridge_number: str,
    points: Optional[np.ndarray],
    params: Dict[str, Any],
    to_world: bool,
    source_row=None,
    include_attributes: bool = False,
) -> np.ndarray | tuple[np.ndarray, Mapping[str, np.ndarray]]:
    if points is not None:
        normalized = points + _param_origin(params) if to_world else points
        return (normalized, {}) if include_attributes else normalized
    row = source_row or _raw_cloud_row(state_code, bridge_number)
    if row is not None:
        try:
            _attachment_id, expected_sha256 = _source_identity(row)
            filepath = _resolve_data_path(row[1])
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail="Saved point cloud is not integrity-verified; re-upload it before analysis.",
            ) from exc
    else:
        filepath = None
    if row is None or filepath is None or not filepath.is_file():
        raise HTTPException(status_code=400, detail="No points supplied and no saved cloud exists.")
    if not str(row[2] or filepath).lower().endswith(".e57"):
        raise HTTPException(status_code=415, detail="Send points in the request for non-E57 clouds.")
    try:
        actual_sha256 = await asyncio.to_thread(_sha256_file, filepath)
    except OSError as exc:
        raise HTTPException(
            status_code=409,
            detail="Saved point cloud could not be integrity-verified; re-upload it.",
        ) from exc
    if actual_sha256 != expected_sha256:
        raise HTTPException(
            status_code=409,
            detail="Saved point cloud failed its integrity check; re-upload it before analysis.",
        )
    receipt = _load_source_receipt(row)
    if receipt is None or receipt.get("metric_processing_authorized") is not True:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "point_cloud_metric_source_receipt_required",
                "message": "Review the point-cloud units, coordinate frame, gravity, and scan registration before analysis.",
            },
        )
    try:
        frame_payload = receipt["coordinate_declaration"]["frame"]
        frame = CoordinateFrame(
            kind=FrameKind(str(frame_payload["kind"])),
            horizontal_unit=LinearUnit(str(frame_payload["horizontal_unit"])),
            vertical_unit=LinearUnit(str(frame_payload["vertical_unit"])),
            up_vector=tuple(frame_payload["up_vector"]),
            gravity_source=GravitySource(str(frame_payload["gravity_source"])),
            crs=frame_payload.get("crs"),
            metadata_source=frame_payload.get("metadata_source"),
            review_evidence_id=frame_payload.get("review_evidence_id"),
        )
        registration_payload = receipt.get("registration")
        registration = (
            RegistrationReceipt(
                selected_scan_indices=tuple(registration_payload["selected_scan_indices"]),
                reference_scan_index=int(registration_payload["reference_scan_index"]),
                rms_metres=float(registration_payload["rms_metres"]),
                maximum_residual_metres=float(
                    registration_payload["maximum_residual_metres"]
                ),
                method=str(registration_payload["method"]),
                transform_sha256=str(registration_payload["transform_sha256"]),
                reviewed=bool(registration_payload["reviewed"]),
            )
            if isinstance(registration_payload, dict)
            else None
        )
        parsed = await asyncio.to_thread(
            read_e57_source,
            filepath,
            selected_scan_indices=tuple(receipt["selected_scan_indices"]),
            coordinate_frame=frame,
            registration=registration,
        )
    except (KeyError, TypeError, ValueError, CoordinateContractError, PointCloudSourceError) as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "point_cloud_source_receipt_invalid",
                "message": "The saved point-cloud source receipt could not be reproduced.",
            },
        ) from exc
    if parsed.local_points is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "point_cloud_metric_transform_unavailable",
                "message": "The saved point cloud has no verified metric analysis transform.",
            },
        )
    normalized = np.asarray(parsed.local_points, dtype=np.float64)
    if include_attributes:
        return normalized, parsed.attributes
    return normalized


def _requested_data_year(params: Mapping[str, Any]) -> int | None:
    raw = params.get("data_year")
    if raw in (None, ""):
        return None
    if isinstance(raw, bool):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_data_year", "message": "data_year must be an integer."},
        )
    try:
        year = int(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_data_year", "message": "data_year must be an integer."},
        ) from exc
    if not 1900 <= year <= 2200:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_data_year", "message": "data_year is outside the supported range."},
        )
    return year


def _canonical_below_features(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = document.get("Features")
    if not isinstance(rows, list):
        return []
    return [
        row
        for row in rows
        if isinstance(row, Mapping) and str(row.get("BF02") or "").strip().upper() == "B"
    ]


def _canonical_under_feature_kind(document: Mapping[str, Any]) -> Optional[str]:
    kinds: set[str] = set()
    for row in _canonical_below_features(document):
        code = str(row.get("BF01") or "").strip().upper()
        if code.startswith(("W", "F")):
            kinds.add("waterway")
        elif code.startswith("H"):
            kinds.add("highway")
        elif code.startswith("R"):
            kinds.add("railroad")
        elif code.startswith("P"):
            kinds.add("pathway")
        elif code:
            kinds.add("other")
    return next(iter(kinds)) if len(kinds) == 1 else None


def _canonical_db_clearance_ft(document: Mapping[str, Any]) -> Optional[float]:
    values: list[float] = []
    for row in _canonical_below_features(document):
        if not str(row.get("BF01") or "").strip().upper().startswith("H"):
            continue
        raw = row.get("BH13")
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        values.append(value)
    return values[0] if len(values) == 1 else None


def _canonical_component_terms(document: Mapping[str, Any]) -> list[str]:
    from ..services.query.snbi_official_information import DEFAULT_SNBI_INFORMATION

    terms: list[str] = []
    for container, tags in (
        ("SpanSets", ("BSP04", "BSP06", "BSP09")),
        ("SubstructureSets", ("BSB03", "BSB04")),
    ):
        rows = document.get(container)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            for tag in tags:
                raw = row.get(tag)
                if raw in (None, ""):
                    continue
                meaning = DEFAULT_SNBI_INFORMATION.get(tag).code_meanings_by_value.get(str(raw))
                if meaning and meaning not in terms:
                    terms.append(meaning)
    return terms[:12]


def _point_cloud_source_evidence(row) -> dict[str, Any]:
    if row is None:
        raise PointCloudKnowledgePackError(
            "An immutable saved point-cloud source is required for bridge-scoped segmentation."
        )
    attachment_id, digest = _source_identity(row)
    _verified_raw_path(row)
    receipt = _load_source_receipt(row)
    if receipt is None or receipt.get("metric_processing_authorized") is not True:
        raise PointCloudKnowledgePackError(
            "A reviewed metric point-cloud source receipt is required before segmentation."
        )
    if receipt.get("source_sha256") != digest:
        raise PointCloudKnowledgePackError(
            "The point-cloud source receipt does not match the saved source."
        )
    try:
        byte_size = int(row[3])
    except (TypeError, ValueError) as exc:
        raise PointCloudKnowledgePackError(
            "The saved point-cloud source has no valid byte-size receipt."
        ) from exc
    return {
        "attachment_id": attachment_id,
        "sha256": digest,
        "byte_size": byte_size,
        "format": str(receipt.get("source_format") or ""),
        "receipt_sha256": str(receipt.get("receipt_sha256") or ""),
    }


def _build_exact_bridge_knowledge_pack(
    *,
    state_code: str,
    bridge_number: str,
    data_year: int | None,
    source_row,
) -> tuple[PointCloudCanonicalContext, PointCloudKnowledgePack]:
    canonical = resolve_point_cloud_canonical_context(
        USER_DB,
        workspace_id=POINT_CLOUD_WORKSPACE_ID,
        state_code=int(state_code),
        bridge_number=str(bridge_number),
        data_year=data_year,
    )
    source = _point_cloud_source_evidence(source_row)
    report_evidence = None
    try:
        from .services.report_rag_cache import (
            ReportRagCacheIncomplete,
            ReportRagCacheIntegrityError,
        )
        from .services.vision import point_cloud_segmentation_report_evidence

        report_evidence = point_cloud_segmentation_report_evidence(
            state_code,
            bridge_number,
            workspace_id=POINT_CLOUD_WORKSPACE_ID,
            inventory_identity=canonical.identity,
            component_terms=_canonical_component_terms(canonical.canonical_document),
        )
    except (ReportRagCacheIncomplete, ReportRagCacheIntegrityError) as exc:
        raise PointCloudKnowledgePackError(
            "The exact-revision report evidence is unavailable or stale."
        ) from exc
    pack = build_point_cloud_knowledge_pack(
        canonical,
        point_cloud_source=source,
        report_evidence=report_evidence,
    )
    return canonical, pack


def _npz_preview(raw: bytes, max_points: int = 0, as_arrays: bool = False) -> Dict[str, Any]:
    import io

    try:
        with np.load(io.BytesIO(raw), allow_pickle=False) as saved:
            if "points" not in saved.files:
                raise PointCloudError("NPZ requires a numeric 'points' array.")
            world = np.asarray(saved["points"], dtype=np.float32)
            colors = np.asarray(saved["colors"], dtype=np.float32) if "colors" in saved.files else None
    except (OSError, ValueError, KeyError) as exc:
        raise PointCloudError("Could not read the NPZ point cloud.") from exc
    if world.ndim != 2 or world.shape[1] < 3:
        raise PointCloudError("NPZ 'points' must have shape (N,3) or (N,3+).")
    world = world[:, :3]
    if colors is not None:
        colors = colors[:, :3] if colors.ndim == 2 and colors.shape[0] == len(world) and colors.shape[1] >= 3 else None
    valid = np.isfinite(world).all(axis=1)
    world = world[valid]
    if colors is not None:
        colors = colors[valid]
        if colors.size and float(np.nanmax(colors)) > 1.0:
            colors = colors / 255.0
        colors = np.clip(colors, 0.0, 1.0)
    if not len(world):
        raise PointCloudError("NPZ contains no finite XYZ points.")
    original_count = int(len(world))
    limit = max(0, min(int(max_points or 0), 2_000_000))
    if limit and len(world) > limit:
        indices = np.linspace(0, len(world) - 1, num=limit, dtype=np.int64)
        world = world[indices]
        if colors is not None:
            colors = colors[indices]
    center = world.mean(axis=0)
    mins, maxs = world.min(axis=0), world.max(axis=0)
    return {
        "status": "success",
        "source": "npz",
        "original_point_count": original_count,
        "rendered_point_count": int(len(world)),
        "downsampled": original_count > len(world),
        "bounds": {"min": mins.tolist(), "max": maxs.tolist(), "center": center.tolist()},
        "points": (world - center).astype(np.float32) if as_arrays else (world - center).round(5).tolist(),
        "colors": colors.astype(np.float32) if as_arrays and colors is not None else (
            colors.round(4).tolist() if colors is not None else None
        ),
    }


async def _upload_point_cloud_impl(
    state_code: str,
    bridge_number: str,
    actor_id: str,
    file: UploadFile = File(...),
    *,
    horizontal_unit: str = "unknown",
    vertical_unit: str = "unknown",
    frame_kind: str = "unknown",
    crs: str = "",
    up_axis: str = "+z",
    declaration_evidence: str = "",
    declaration_reviewed: bool = False,
    selected_scan_indices: str = "",
    registration_reference_scan: int | None = None,
    registration_rms_metres: float | None = None,
    registration_maximum_residual_metres: float | None = None,
    registration_method: str = "",
    registration_reviewed: bool = False,
):
    try:
        bridge_id = _safe_bridge_id(state_code, bridge_number)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Invalid state code or bridge number."
        ) from exc
    ext = Path(file.filename or "").suffix.lower()
    if ext not in POINT_CLOUD_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported type '{ext}'. Allowed: {', '.join(sorted(POINT_CLOUD_EXTS))}",
        )
    del bridge_id  # validated above; paths use the same safe identity helper.
    try:
        async with _bridge_write_lock(state_code, bridge_number):
            if _raw_cloud_row(state_code, bridge_number) is not None:
                raise _PointCloudReplacementRequired
            cloud_dir = _resolve_data_path(
                _bridge_cloud_root(state_code, bridge_number) / "raw"
            )
            destination = _resolve_data_path(cloud_dir / f"{uuid4().hex}{ext}")
            size, source_sha256 = await _stream_upload(file, destination)
            try:
                source_format = detect_source_format(
                    destination, declared_filename=file.filename
                )
            except PointCloudSourceError as exc:
                destination.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=400,
                    detail={"code": exc.code, "message": exc.public_message},
                ) from exc
            receipt_path, source_receipt = _write_source_receipt(
                source_path=destination,
                source_sha256=source_sha256,
                source_size_bytes=size,
                source_format=source_format.value,
                declared_filename=file.filename or f"cloud{ext}",
                actor_id=actor_id,
                horizontal_unit=horizontal_unit,
                vertical_unit=vertical_unit,
                frame_kind=frame_kind,
                crs=crs,
                up_axis=up_axis,
                evidence=declaration_evidence,
                reviewed=declaration_reviewed,
                selected_scan_indices=selected_scan_indices,
                registration_reference_scan=registration_reference_scan,
                registration_rms_metres=registration_rms_metres,
                registration_maximum_residual_metres=registration_maximum_residual_metres,
                registration_method=registration_method,
                registration_reviewed=registration_reviewed,
            )
            attachment_id: Optional[int] = None
            # An inline processed result cannot remain current when a new raw
            # source is attached to this bridge.
            _invalidate_processed_pointer(state_code, bridge_number)
            with _sqlite_connection(USER_DB) as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    """
                    SELECT 1 FROM bridge_attachments
                    WHERE workspace_id=? AND state_code=? AND bridge_number=?
                      AND attachment_type='point_cloud'
                    LIMIT 1
                    """,
                    (POINT_CLOUD_WORKSPACE_ID, state_code, bridge_number),
                ).fetchone()
                if current is not None:
                    raise _PointCloudReplacementRequired
                cursor = conn.execute(
                    """
                    INSERT INTO bridge_attachments
                        (workspace_id, state_code, bridge_number, attachment_type,
                         filename, filepath, size_bytes, sha256)
                    VALUES (?, ?, ?, 'point_cloud', ?, ?, ?, ?)
                    """,
                    (
                        POINT_CLOUD_WORKSPACE_ID,
                        state_code,
                        bridge_number,
                        _safe_upload_name(file.filename, f"cloud{ext}"),
                        _relative_data_path(destination),
                        size,
                        source_sha256,
                    ),
                )
                attachment_id = int(cursor.lastrowid)
            _remove_processed(state_code, bridge_number)
            return {
                "status": "success",
                "filename": file.filename,
                "size_bytes": size,
                "source_attachment_id": attachment_id,
                "source_sha256": source_sha256,
                "source_format": source_format.value,
                "source_profile": _public_source_receipt(source_receipt),
            }
    except _PointCloudReplacementRequired as exc:
        try:
            destination.unlink(missing_ok=True)
            receipt_path.unlink(missing_ok=True)
        except (NameError, OSError):
            pass
        raise HTTPException(
            status_code=409,
            detail="point_cloud_delete_required_before_replace",
        ) from exc
    except HTTPException:
        try:
            destination.unlink(missing_ok=True)
            receipt_path.unlink(missing_ok=True)
        except (NameError, OSError):
            pass
        raise
    except (OSError, sqlite3.Error, ValueError) as exc:
        try:
            destination.unlink(missing_ok=True)
            receipt_path.unlink(missing_ok=True)
        except (NameError, OSError):
            pass
        raise HTTPException(
            status_code=500,
            detail="Point-cloud upload could not be saved.",
        ) from exc


@router.post("/api/bridges/{state_code}/{bridge_number}/point-cloud")
async def upload_point_cloud(
    state_code: str,
    bridge_number: str,
    request: Request,
    file: UploadFile = File(...),
    horizontal_unit: Annotated[str, Form()] = "unknown",
    vertical_unit: Annotated[str, Form()] = "unknown",
    frame_kind: Annotated[str, Form()] = "unknown",
    crs: Annotated[str, Form()] = "",
    up_axis: Annotated[str, Form()] = "+z",
    declaration_evidence: Annotated[str, Form()] = "",
    declaration_reviewed: Annotated[bool, Form()] = False,
    selected_scan_indices: Annotated[str, Form()] = "",
    registration_reference_scan: Annotated[int | None, Form()] = None,
    registration_rms_metres: Annotated[float | None, Form()] = None,
    registration_maximum_residual_metres: Annotated[float | None, Form()] = None,
    registration_method: Annotated[str, Form()] = "",
    registration_reviewed: Annotated[bool, Form()] = False,
):
    async with _point_cloud_quota(request, "upload"):
        return await _upload_point_cloud_impl(
            state_code,
            bridge_number,
            _point_cloud_actor(request),
            file,
            horizontal_unit=horizontal_unit,
            vertical_unit=vertical_unit,
            frame_kind=frame_kind,
            crs=crs,
            up_axis=up_axis,
            declaration_evidence=declaration_evidence,
            declaration_reviewed=declaration_reviewed,
            selected_scan_indices=selected_scan_indices,
            registration_reference_scan=registration_reference_scan,
            registration_rms_metres=registration_rms_metres,
            registration_maximum_residual_metres=registration_maximum_residual_metres,
            registration_method=registration_method,
            registration_reviewed=registration_reviewed,
        )


@router.get("/api/bridges/{state_code}/{bridge_number}/point-cloud/info")
def point_cloud_info(state_code: str, bridge_number: str):
    row = _raw_cloud_row(state_code, bridge_number)
    processed = _load_processed(state_code, bridge_number)
    publication = None
    if row is not None:
        try:
            _attachment_id, source_sha256 = _source_identity(row)
            publication = get_latest_published_point_cloud_result(
                USER_DB,
                POINT_CLOUD_WORKSPACE_ID,
                int(state_code),
                str(bridge_number),
                source_sha256,
            )
        except (PointCloudRunStoreError, sqlite3.Error, TypeError, ValueError):
            # Discovery is intentionally cheap. The accepted-result reader does
            # the full source, revision, knowledge-pack, and artifact recheck.
            publication = None
    if not row and not processed:
        return {
            "exists": False,
            "raw_exists": False,
            "accepted_result_exists": False,
            "legacy_processed_exists": False,
        }
    return {
        "exists": True,
        "raw_exists": bool(row),
        "accepted_result_exists": publication is not None,
        "accepted_result_data_year": (
            publication.data_year if publication is not None else None
        ),
        "accepted_result_run_id": publication.run_id if publication is not None else None,
        # Kept as a migration diagnostic only. The active UI never loads this
        # retired mutable pointer as an accepted scientific result.
        "legacy_processed_exists": bool(processed),
        "filename": row[2] if row else "processed_cloud.ply",
        "size_bytes": row[3] if row else 0,
        "uploaded_at": row[4] if row else None,
        "source_sha256": row[5] if row else None,
        "source_profile": _public_source_receipt(_load_source_receipt(row)),
        "processed_point_count": (
            processed.get("persistence", {}).get("processed_point_count") if processed else None
        ),
    }


@router.get("/api/bridges/{state_code}/{bridge_number}/point-cloud")
def download_point_cloud(state_code: str, bridge_number: str):
    row = _raw_cloud_row(state_code, bridge_number)
    try:
        filepath = _verified_raw_path(row) if row else None
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail="Saved point cloud failed its integrity check; re-upload it.",
        ) from exc
    if row is None or filepath is None or not filepath.is_file():
        raise HTTPException(status_code=404, detail="No point cloud saved for this bridge.")
    return FileResponse(filepath, filename=row[2], media_type="application/octet-stream")


@router.get("/api/bridges/{state_code}/{bridge_number}/point-cloud/preview")
def preview_bridge_point_cloud(
    state_code: str,
    bridge_number: str,
    scan_index: int = 0,
    max_points: int = 0,
    binary: int = 0,
):
    row = _raw_cloud_row(state_code, bridge_number)
    try:
        filepath = _verified_raw_path(row) if row else None
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail="Saved point cloud failed its integrity check; re-upload it.",
        ) from exc
    if row is None or filepath is None or not filepath.is_file():
        raise HTTPException(status_code=404, detail="No point cloud saved for this bridge.")
    if not str(row[2] or filepath).lower().endswith(".e57"):
        raise HTTPException(status_code=415, detail="Backend preview supports saved E57 files only.")
    try:
        payload = read_e57_preview(
            filepath,
            scan_index=scan_index,
            max_points=max_points,
            as_arrays=bool(binary),
        )
        return _frame_cloud_response(payload) if binary else _json_safe(payload)
    except PointCloudError as exc:
        raise HTTPException(
            status_code=400,
            detail="Saved point-cloud preview could not be generated.",
        ) from exc


@router.get("/api/bridges/{state_code}/{bridge_number}/point-cloud/processed")
def processed_analysis(state_code: str, bridge_number: str):
    _require_durable_bridge_processing()
    result = _load_processed(state_code, bridge_number)
    if result is None:
        raise HTTPException(status_code=404, detail="No processed point-cloud result exists.")
    return result


@router.get("/api/bridges/{state_code}/{bridge_number}/point-cloud/processed/preview")
def processed_preview(state_code: str, bridge_number: str, binary: int = 0):
    _require_durable_bridge_processing()
    payload = _processed_preview(state_code, bridge_number, as_arrays=bool(binary))
    if payload is None:
        raise HTTPException(status_code=404, detail="No processed point cloud exists.")
    return _frame_cloud_response(payload) if binary else _json_safe(payload)


@router.get("/api/bridges/{state_code}/{bridge_number}/point-cloud/processed/download")
def download_processed(state_code: str, bridge_number: str, format: str = "ply"):
    _require_durable_bridge_processing()
    if format not in {"ply", "npz"}:
        raise HTTPException(status_code=400, detail="format must be 'ply' or 'npz'.")
    try:
        _root, npz_path, ply_path, _analysis, result, _pointer = _active_processed_record(
            state_code, bridge_number
        )
        path = ply_path if format == "ply" else npz_path
        expected_sha256 = result["persistence"]["artifact_sha256"][format]
        if _sha256_file(path) != expected_sha256:
            raise ValueError("artifact digest mismatch")
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        sqlite3.Error,
    ) as exc:
        raise HTTPException(status_code=404, detail="No processed point cloud exists.") from exc
    return FileResponse(
        path,
        filename=f"{_safe_bridge_id(state_code, bridge_number)}_processed.{format}",
        media_type="application/octet-stream",
    )


@router.delete("/api/bridges/{state_code}/{bridge_number}/point-cloud")
async def delete_point_cloud(
    state_code: str,
    bridge_number: str,
    request: Request,
    reason: str,
):
    """Request an audited deletion and immediately attempt its durable outbox."""

    user = getattr(request.state, "user", None)
    actor_id = str(getattr(user, "email", "") or "").strip().lower()
    if not actor_id or "@" not in actor_id:
        raise HTTPException(status_code=401, detail="authentication_required")
    try:
        async with _bridge_write_lock(state_code, bridge_number):
            with _sqlite_connection(USER_DB) as conn:
                rows = conn.execute(
                    """
                    SELECT id,sha256 FROM bridge_attachments
                    WHERE workspace_id=? AND state_code=? AND bridge_number=?
                      AND attachment_type='point_cloud'
                    ORDER BY id DESC LIMIT 2
                    """,
                    (POINT_CLOUD_WORKSPACE_ID, state_code, bridge_number),
                ).fetchall()
            if not rows:
                raise HTTPException(status_code=404, detail="point_cloud_not_found")
            if len(rows) != 1:
                raise HTTPException(
                    status_code=409, detail="point_cloud_catalog_conflict"
                )
            source_sha256 = str(rows[0][1] or "")
            with _sqlite_connection(
                f"file:{USER_DB}?mode=ro", uri=True, timeout=5
            ) as conn:
                run_store_available = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='point_cloud_runs'"
                ).fetchone()
                durable_evidence = (
                    conn.execute(
                        "SELECT 1 FROM point_cloud_runs "
                        "WHERE workspace_id=? AND source_sha256=? LIMIT 1",
                        (POINT_CLOUD_WORKSPACE_ID, source_sha256),
                    ).fetchone()
                    if run_store_available is not None
                    else None
                )
            if durable_evidence is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "point_cloud_source_has_durable_evidence",
                        "message": (
                            "This source is referenced by immutable point-cloud run evidence "
                            "and cannot be deleted by the attachment endpoint."
                        ),
                    },
                )
            receipt = request_attachment_deletion(
                USER_DB,
                workspace_id=POINT_CLOUD_WORKSPACE_ID,
                attachment_id=int(rows[0][0]),
                attachment_type="point_cloud",
                actor_id=actor_id,
                reason=reason,
                state_code=state_code,
                bridge_number=bridge_number,
            )

            def finalize_point_cloud(source: Path) -> None:
                # This callback runs inside the durable outbox. Any failure is
                # recorded and retried without exposing private paths.
                _invalidate_processed_pointer(state_code, bridge_number)
                _source_receipt_path(source).unlink(missing_ok=True)
                source.unlink(missing_ok=True)
                processed_root = _resolve_data_path(
                    _bridge_cloud_root(state_code, bridge_number) / "processed"
                )
                if processed_root.is_dir():
                    shutil.rmtree(processed_root)

            finalized = process_attachment_deletion_outbox(
                USER_DB,
                data_root=DATA_DIR,
                rag_working_dir=RAG_WORKING_DIR,
                workspace_id=POINT_CLOUD_WORKSPACE_ID,
                deletion_id=receipt.id,
                unlinker=finalize_point_cloud,
            )
            public = (finalized[0] if finalized else receipt).public_dict()
            return JSONResponse(
                {
                    "status": (
                        "deleted" if public["status"] == "complete" else "pending"
                    ),
                    "deletion": public,
                },
                status_code=200 if public["status"] == "complete" else 202,
            )
    except HTTPException:
        raise
    except RetentionError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.code) from exc
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise HTTPException(
            status_code=500,
            detail="Point-cloud deletion could not be completed.",
        ) from exc


@router.post("/api/bridges/{state_code}/{bridge_number}/point-cloud/runs", status_code=202)
async def create_durable_point_cloud_run(
    state_code: str,
    bridge_number: str,
    body: DurablePointCloudRunBody,
    request: Request,
):
    """Freeze exact source/context receipts and enqueue; never analyze inline."""

    actor = _point_cloud_actor(request)
    idempotency_key = str(request.headers.get("Idempotency-Key") or "").strip()
    if not idempotency_key or len(idempotency_key) > 255:
        raise HTTPException(
            status_code=422,
            detail={"code": "idempotency_key_required", "message": "Idempotency-Key is required."},
        )
    try:
        job, run, idempotent = await asyncio.to_thread(
            enqueue_point_cloud_analysis,
            database_path=USER_DB,
            job_database_path=_point_cloud_job_db(),
            workspace_id=POINT_CLOUD_WORKSPACE_ID,
            owner_id=actor,
            idempotency_key=idempotency_key,
            state_code=state_code,
            bridge_number=bridge_number,
            data_year=body.data_year,
            model_mode=body.model_mode,
            use_vlm=body.use_vlm,
        )
    except (PointCloudRunStoreError, PointCloudJobError, JobStoreError) as exc:
        raise _durable_error(exc) from exc
    response = public_point_cloud_job(job, run)
    response["idempotent"] = bool(idempotent)
    return JSONResponse(
        response,
        status_code=202,
        headers={"ETag": run.etag, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/api/bridges/{state_code}/{bridge_number}/point-cloud/runs/published")
async def published_point_cloud_run(
    state_code: str,
    bridge_number: str,
    data_year: int | None = None,
):
    publication, run, _record = await asyncio.to_thread(
        _published_durable_record, state_code, bridge_number, data_year
    )
    return JSONResponse(
        {
            "job_id": run.job_id,
            "run_id": run.run_id,
            "status": "published",
            "phase": run.phase,
            "progress": dict(run.progress),
            "error_code": None,
            "published": True,
            "published_at": publication.published_at,
            "data_year": publication.data_year,
            "snapshot_revision_id": run.snapshot_revision_id,
            "canonical_sha256": run.canonical_sha256,
            "source_sha256": run.source_sha256,
            "analysis_profile_sha256": run.analysis_profile_sha256,
            "knowledge_pack_sha256": run.knowledge_pack_sha256,
            "counts": {
                "sources": run.source_count,
                "vlm_calls": run.vlm_call_count,
                "components": run.component_count,
                "measurements": run.measurement_count,
                "reviews": run.review_count,
            },
            "review_required": False,
            "etag": run.etag,
        },
        headers={"ETag": run.etag, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/api/bridges/{state_code}/{bridge_number}/point-cloud/runs/published/result")
async def published_point_cloud_run_result(
    state_code: str,
    bridge_number: str,
    data_year: int | None = None,
):
    _publication, run, record = await asyncio.to_thread(
        _published_durable_record, state_code, bridge_number, data_year
    )
    _root, _npz, _ply, result = record
    public = _json_safe(result)
    if isinstance(public, dict) and isinstance(public.get("persistence"), dict):
        public["persistence"].pop("point_cloud_file", None)
        public["persistence"]["publication_status"] = "accepted"
        public["persistence"]["run_id"] = run.run_id
    return JSONResponse(public, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/api/bridges/{state_code}/{bridge_number}/point-cloud/runs/published/evidence")
async def published_point_cloud_run_evidence(
    state_code: str,
    bridge_number: str,
    data_year: int | None = None,
):
    _publication, run, record = await asyncio.to_thread(
        _published_durable_record, state_code, bridge_number, data_year
    )
    root, _npz, _ply, _result = record
    try:
        evidence = await asyncio.to_thread(
            get_point_cloud_run_evidence,
            USER_DB,
            POINT_CLOUD_WORKSPACE_ID,
            run.run_id,
        )
        raw_knowledge = (root / "knowledge_pack.json").read_bytes()
        if len(raw_knowledge) > 64 * 1024 or hashlib.sha256(raw_knowledge).hexdigest() != run.knowledge_pack_sha256:
            raise ValueError("knowledge-pack digest mismatch")
        knowledge = json.loads(raw_knowledge)
    except PointCloudRunStoreError as exc:
        raise _durable_error(exc) from exc
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "point_cloud_knowledge_context_integrity_failed", "message": "Accepted point-cloud knowledge context failed integrity validation."},
        ) from exc
    return JSONResponse(
        _json_safe(
            {
                "run_id": evidence.run_id,
                "context": {
                    "snapshot_revision_id": run.snapshot_revision_id,
                    "canonical_sha256": run.canonical_sha256,
                    "source_sha256": run.source_sha256,
                    "analysis_profile_sha256": run.analysis_profile_sha256,
                    "knowledge_pack_sha256": run.knowledge_pack_sha256,
                    "report_source_set_sha256": run.report_source_set_sha256,
                },
                "sources": list(evidence.sources),
                "vlm_calls": list(evidence.vlm_calls),
                "components": list(evidence.components),
                "measurements": list(evidence.measurements),
                "reviews": list(evidence.reviews),
                "knowledge_context": {
                    "schema_version": knowledge.get("schema_version"),
                    "builder": knowledge.get("builder"),
                    "trusted_snbi": knowledge.get("trusted_structured_context"),
                    "cited_report_evidence": knowledge.get("untrusted_report_evidence"),
                    "omissions": knowledge.get("omissions"),
                    "policy_notes": knowledge.get("policy_notes"),
                },
                "truncated": evidence.truncated,
            }
        ),
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/api/bridges/{state_code}/{bridge_number}/point-cloud/runs/published/preview")
async def published_point_cloud_run_preview(
    state_code: str,
    bridge_number: str,
    data_year: int | None = None,
    binary: int = 1,
):
    _publication, run, record = await asyncio.to_thread(
        _published_durable_record, state_code, bridge_number, data_year
    )
    _root, npz_path, _ply, _result = record
    try:
        with np.load(npz_path, allow_pickle=False) as saved:
            world = np.asarray(saved["points"], dtype=np.float32)[:, :3]
            colors = np.asarray(saved["colors"], dtype=np.float32)[:, :3] if "colors" in saved.files else None
        if world.size == 0:
            raise ValueError("empty accepted result")
        center = world.mean(axis=0)
        mins, maxs = world.min(axis=0), world.max(axis=0)
        payload = {
            "status": "success",
            "source": "durable_accepted_result",
            "original_point_count": int(world.shape[0]),
            "rendered_point_count": int(world.shape[0]),
            "downsampled": False,
            "bounds": {"min": mins.tolist(), "max": maxs.tolist(), "center": center.tolist()},
            "points": (world - center).astype(np.float32) if binary else (world - center).round(5).tolist(),
            "colors": colors if binary else (colors.round(4).tolist() if colors is not None else None),
            "run_id": run.run_id,
            "publication_status": "accepted",
        }
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "point_cloud_result_integrity_failed", "message": "Accepted point-cloud result failed integrity validation."},
        ) from exc
    return _frame_cloud_response(payload) if binary else _json_safe(payload)


@router.get("/api/bridges/{state_code}/{bridge_number}/point-cloud/runs/published/download")
async def published_point_cloud_run_download(
    state_code: str,
    bridge_number: str,
    data_year: int | None = None,
    format: Literal["ply", "npz"] = "ply",
):
    _publication, run, record = await asyncio.to_thread(
        _published_durable_record, state_code, bridge_number, data_year
    )
    _root, npz_path, ply_path, _result = record
    path = ply_path if format == "ply" else npz_path
    return FileResponse(
        path,
        filename=f"{_safe_bridge_id(str(run.state_code), run.bridge_number)}_{run.run_id}.{format}",
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/api/point-cloud/runs/{run_id}")
async def durable_point_cloud_run_status(run_id: str, request: Request):
    _actor, run, job = await asyncio.to_thread(_owned_durable_run, request, run_id)
    return JSONResponse(
        public_point_cloud_job(job, run),
        headers={"ETag": run.etag, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def _durable_candidate_record(run):
    if run.status != "succeeded" or not run.result_sha256:
        raise HTTPException(status_code=409, detail={"code": "point_cloud_result_not_ready", "message": "Point-cloud candidate is not ready."})
    generation = str(run.progress.get("source_generation") or "")
    if not _GENERATION_RE.fullmatch(generation):
        raise HTTPException(status_code=500, detail={"code": "point_cloud_result_receipt_invalid", "message": "Point-cloud result receipt is invalid."})
    try:
        root, npz_path, ply_path, analysis_path = _processed_paths(
            str(run.state_code), run.bridge_number, generation
        )
        raw = analysis_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != run.result_sha256:
            raise ValueError("manifest digest mismatch")
        result = json.loads(raw)
        persistence = result["persistence"]
        if (
            persistence.get("source_generation") != generation
            or persistence.get("source_sha256") != run.source_sha256
            or persistence.get("analysis_profile_sha256") != run.analysis_profile_sha256
            or persistence.get("snapshot_identity", {}).get("snapshot_revision_id") != run.snapshot_revision_id
            or persistence.get("snapshot_identity", {}).get("canonical_sha256") != run.canonical_sha256
        ):
            raise ValueError("candidate scope mismatch")
        for name, path in (("npz", npz_path), ("ply", ply_path)):
            if _sha256_file(path) != str(persistence["artifact_sha256"][name]):
                raise ValueError("candidate artifact digest mismatch")
        knowledge_path = root / "knowledge_pack.json"
        knowledge_receipt = persistence.get("knowledge_pack")
        if isinstance(knowledge_receipt, Mapping):
            if (
                not knowledge_path.is_file()
                or _sha256_file(knowledge_path) != run.knowledge_pack_sha256
                or str(persistence["artifact_sha256"].get("knowledge_pack") or "") != run.knowledge_pack_sha256
            ):
                raise ValueError("knowledge-pack digest mismatch")
        return root, npz_path, ply_path, result
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "point_cloud_result_integrity_failed", "message": "Point-cloud candidate failed integrity validation."},
        ) from exc


def _published_durable_record(
    state_code: str, bridge_number: str, data_year: int | None
):
    """Resolve an accepted run only when its source and exact context are current."""

    row = _raw_cloud_row(state_code, bridge_number)
    try:
        _attachment_id, source_sha256 = _source_identity(row)
        publication = get_latest_published_point_cloud_result(
            USER_DB,
            POINT_CLOUD_WORKSPACE_ID,
            int(state_code),
            str(bridge_number),
            source_sha256,
            data_year=data_year,
        )
        run = get_point_cloud_run(
            USER_DB, POINT_CLOUD_WORKSPACE_ID, publication.run_id
        )
        if (
            run.status != "succeeded"
            or run.published_review_id != publication.review_id
            or run.source_sha256 != source_sha256
        ):
            raise ValueError("publication identity mismatch")
        context, pack = _build_exact_bridge_knowledge_pack(
            state_code=str(state_code),
            bridge_number=str(bridge_number),
            data_year=publication.data_year,
            source_row=row,
        )
        if (
            context.identity.snapshot_revision_id != run.snapshot_revision_id
            or context.identity.canonical_sha256 != run.canonical_sha256
            or pack.sha256 != run.knowledge_pack_sha256
        ):
            raise ValueError("publication context is stale")
        record = _durable_candidate_record(run)
        return publication, run, record
    except PointCloudRunStoreError as exc:
        raise _durable_error(exc) from exc
    except (
        OSError,
        ValueError,
        TypeError,
        PointCloudContextError,
        PointCloudKnowledgePackError,
    ) as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "published_point_cloud_result_stale",
                "message": "The accepted point-cloud result does not match the current source and published bridge context.",
            },
        ) from exc


@router.get("/api/point-cloud/runs/{run_id}/evidence")
async def durable_point_cloud_run_evidence(run_id: str, request: Request):
    _actor, run, _job = await asyncio.to_thread(_owned_durable_run, request, run_id)
    try:
        evidence = await asyncio.to_thread(
            get_point_cloud_run_evidence,
            USER_DB,
            POINT_CLOUD_WORKSPACE_ID,
            run_id,
        )
    except PointCloudRunStoreError as exc:
        raise _durable_error(exc) from exc
    knowledge_context = None
    if run.status == "succeeded":
        root, _npz, _ply, _result = await asyncio.to_thread(_durable_candidate_record, run)
        try:
            raw_knowledge = (root / "knowledge_pack.json").read_bytes()
            if len(raw_knowledge) > 64 * 1024 or hashlib.sha256(raw_knowledge).hexdigest() != run.knowledge_pack_sha256:
                raise ValueError("knowledge-pack digest mismatch")
            knowledge = json.loads(raw_knowledge)
            knowledge_context = {
                "schema_version": knowledge.get("schema_version"),
                "builder": knowledge.get("builder"),
                "trusted_snbi": knowledge.get("trusted_structured_context"),
                "cited_report_evidence": knowledge.get("untrusted_report_evidence"),
                "omissions": knowledge.get("omissions"),
                "policy_notes": knowledge.get("policy_notes"),
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=500, detail={"code": "point_cloud_knowledge_context_integrity_failed", "message": "Point-cloud knowledge context failed integrity validation."}) from exc
    payload = _json_safe(
        {
            "run_id": evidence.run_id,
            "context": {
                "snapshot_revision_id": run.snapshot_revision_id,
                "canonical_sha256": run.canonical_sha256,
                "source_sha256": run.source_sha256,
                "analysis_profile_sha256": run.analysis_profile_sha256,
                "knowledge_pack_sha256": run.knowledge_pack_sha256,
                "report_source_set_sha256": run.report_source_set_sha256,
            },
            "sources": list(evidence.sources),
            "vlm_calls": list(evidence.vlm_calls),
            "components": list(evidence.components),
            "measurements": list(evidence.measurements),
            "reviews": list(evidence.reviews),
            "knowledge_context": knowledge_context,
            "truncated": evidence.truncated,
        }
    )
    return JSONResponse(payload, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/api/point-cloud/runs/{run_id}/result")
async def durable_point_cloud_run_result(run_id: str, request: Request):
    _actor, run, _job = await asyncio.to_thread(_owned_durable_run, request, run_id)
    _root, _npz, _ply, result = await asyncio.to_thread(_durable_candidate_record, run)
    public = _json_safe(result)
    if isinstance(public, dict) and isinstance(public.get("persistence"), dict):
        public["persistence"].pop("point_cloud_file", None)
        public["persistence"]["publication_status"] = (
            "accepted" if run.published_review_id else "candidate_review_required"
        )
        public["persistence"]["run_id"] = run.run_id
    return JSONResponse(public, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/api/point-cloud/runs/{run_id}/preview")
async def durable_point_cloud_run_preview(run_id: str, request: Request, binary: int = 1):
    _actor, run, _job = await asyncio.to_thread(_owned_durable_run, request, run_id)
    _root, npz_path, _ply, result = await asyncio.to_thread(_durable_candidate_record, run)
    try:
        with np.load(npz_path, allow_pickle=False) as saved:
            world = np.asarray(saved["points"], dtype=np.float32)[:, :3]
            colors = np.asarray(saved["colors"], dtype=np.float32)[:, :3] if "colors" in saved.files else None
        if world.size == 0:
            raise ValueError("empty candidate")
        center = world.mean(axis=0)
        mins, maxs = world.min(axis=0), world.max(axis=0)
        payload = {
            "status": "success",
            "source": "durable_review_candidate",
            "original_point_count": int(world.shape[0]),
            "rendered_point_count": int(world.shape[0]),
            "downsampled": False,
            "bounds": {"min": mins.tolist(), "max": maxs.tolist(), "center": center.tolist()},
            "points": (world - center).astype(np.float32) if binary else (world - center).round(5).tolist(),
            "colors": colors if binary else (colors.round(4).tolist() if colors is not None else None),
            "run_id": run.run_id,
            "publication_status": "accepted" if run.published_review_id else "candidate_review_required",
        }
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=500, detail={"code": "point_cloud_result_integrity_failed", "message": "Point-cloud candidate failed integrity validation."}) from exc
    return _frame_cloud_response(payload) if binary else _json_safe(payload)


@router.get("/api/point-cloud/runs/{run_id}/download")
async def durable_point_cloud_run_download(run_id: str, request: Request, format: Literal["ply", "npz"] = "ply"):
    _actor, run, _job = await asyncio.to_thread(_owned_durable_run, request, run_id)
    _root, npz_path, ply_path, _result = await asyncio.to_thread(_durable_candidate_record, run)
    path = ply_path if format == "ply" else npz_path
    return FileResponse(
        path,
        filename=f"{_safe_bridge_id(str(run.state_code), run.bridge_number)}_{run.run_id}.{format}",
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/api/point-cloud/runs/{run_id}/cancel", status_code=202)
async def cancel_durable_point_cloud_run(run_id: str, request: Request):
    actor, run, _job = await asyncio.to_thread(_owned_durable_run, request, run_id)
    try:
        job = await asyncio.to_thread(
            cancel_point_cloud_job,
            database_path=USER_DB,
            job_database_path=_point_cloud_job_db(),
            workspace_id=POINT_CLOUD_WORKSPACE_ID,
            owner_id=actor,
            job_id=run.job_id,
        )
        current = await asyncio.to_thread(get_point_cloud_run, USER_DB, POINT_CLOUD_WORKSPACE_ID, run_id)
    except (PointCloudRunStoreError, PointCloudJobError, JobStoreError) as exc:
        raise _durable_error(exc) from exc
    return JSONResponse(
        public_point_cloud_job(job, current),
        status_code=202,
        headers={"ETag": current.etag, "Cache-Control": "no-store"},
    )


@router.post("/api/point-cloud/runs/{run_id}/review")
async def review_durable_point_cloud_run(
    run_id: str,
    body: DurablePointCloudReviewBody,
    request: Request,
):
    actor, _run, job = await asyncio.to_thread(_owned_durable_run, request, run_id)
    expected_etag = str(request.headers.get("If-Match") or "").strip()
    idempotency_key = str(request.headers.get("Idempotency-Key") or "").strip()
    if not expected_etag:
        raise HTTPException(status_code=428, detail={"code": "if_match_required", "message": "If-Match is required."})
    if not idempotency_key or len(idempotency_key) > 255:
        raise HTTPException(status_code=422, detail={"code": "idempotency_key_required", "message": "Idempotency-Key is required."})
    review_id = str(
        uuid5(
            NAMESPACE_URL,
            f"bridgeguard:point-cloud-review:{POINT_CLOUD_WORKSPACE_ID}:{actor}:{run_id}:{idempotency_key}",
        )
    )
    if body.decision == "accept":
        _require_point_cloud_evaluation_release()
        try:
            await asyncio.to_thread(
                verify_point_cloud_job_scope,
                database_path=USER_DB,
                job=job,
            )
        except (PointCloudJobError, JobStoreError) as exc:
            raise _durable_error(exc) from exc
    try:
        reviewed = await asyncio.to_thread(
            review_point_cloud_run,
            USER_DB,
            ReviewPointCloudRunRequest(
                workspace_id=POINT_CLOUD_WORKSPACE_ID,
                run_id=run_id,
                review_id=review_id,
                reviewer_id=actor,
                decision=body.decision,
                reason=body.reason,
                expected_etag=expected_etag,
                corrections=body.corrections,
            ),
        )
    except PointCloudRunStoreError as exc:
        raise _durable_error(exc) from exc
    return JSONResponse(
        {
            "run_id": reviewed.run_id,
            "review_id": reviewed.review_id,
            "decision": reviewed.decision,
            "published": reviewed.published,
            "data_generation": reviewed.data_generation,
            "before_sha256": reviewed.before_sha256,
            "after_sha256": reviewed.after_sha256,
            "idempotent": reviewed.idempotent,
        },
        headers={
            "ETag": reviewed.etag,
            "X-BridgeGuard-Data-Version": str(reviewed.data_generation),
            "Cache-Control": "no-store",
        },
    )


@router.get("/api/point-cloud/health")
def point_cloud_health():
    try:
        pipeline_mode = require_available_point_cloud_pipeline()
    except PipelineConfigurationError as exc:
        return {
            "status": "blocked",
            "pipeline_mode": None,
            "pipeline_available": False,
            "error_code": exc.code,
        }
    evaluation_required = _point_cloud_evaluation_required()
    evaluation_ready = not evaluation_required
    evaluation_error = None
    if evaluation_required:
        receipt_path = os.getenv("BRIDGEGUARD_POINT_CLOUD_EVALUATION_RECEIPT", "").strip()
        if not receipt_path:
            evaluation_error = "point_cloud_evaluation_receipt_required"
        else:
            try:
                require_current_release_receipt(receipt_path)
                evaluation_ready = True
            except PointCloudEvaluationError as exc:
                evaluation_error = exc.code
    try:
        from .services.llm_providers import (
            require_generation_capability,
            vision_model,
        )

        selected_vision_model = vision_model()
        require_generation_capability("vision", model=selected_vision_model)
        require_generation_capability("json_schema", model=selected_vision_model)
        multimodal_model_ready = True
        multimodal_error = None
    except Exception:
        selected_vision_model = None
        multimodal_model_ready = False
        multimodal_error = "point_cloud_multimodal_capability_unavailable"
    try:
        import pye57  # type: ignore

        e57_supported = True
        pye57_version = getattr(pye57, "__version__", None)
    except Exception:
        e57_supported = False
        pye57_version = None
    parser_ready = e57_supported
    ready = evaluation_ready and parser_ready and multimodal_model_ready
    return {
        "status": "ok" if ready else "degraded",
        "pipeline_mode": pipeline_mode.value,
        "pipeline_available": True,
        "durable_workflow_available": parser_ready,
        "bridge_synchronous_routes_retired": True,
        "e57_supported": e57_supported,
        "pye57_version": pye57_version,
        "parser_ready": parser_ready,
        "multimodal_model_ready": multimodal_model_ready,
        "multimodal_model": selected_vision_model,
        "multimodal_error_code": multimodal_error,
        "evaluation_required": evaluation_required,
        "evaluation_release_ready": evaluation_ready,
        "evaluation_error_code": evaluation_error,
    }


@router.get("/api/point-cloud/source-capabilities")
def point_cloud_source_capabilities():
    """Return safe, runtime-derived parser claims for the source review UI."""

    capabilities = parser_capabilities()
    return {
        "contract_version": "bridgeguard-point-cloud-source-capabilities-v1",
        "source_attachment_extensions": sorted(POINT_CLOUD_EXTS),
        "metric_ingest_extensions": sorted(source_ingest_extensions()),
        "formats": [capability.to_dict() for capability in capabilities],
        "browser_visualization_is_not_metric_ingestion": True,
    }


@router.post("/api/point-cloud/preview")
async def preview_e57(
    file: UploadFile = File(...),
    scan_index: int = Form(0),
    max_points: int = Form(0),
    binary: int = 0,
):
    filename = _safe_upload_name(file.filename, "cloud.e57")
    if not filename.lower().endswith(".e57"):
        raise HTTPException(status_code=400, detail="Please upload an .e57 point-cloud file.")
    temp_path = _resolve_data_path(
        Path(TEMP_DIR) / f"preview-{uuid4().hex}-{filename}"
    )
    try:
        await _stream_upload(file, temp_path)
        payload = await asyncio.to_thread(
            read_e57_preview,
            temp_path,
            scan_index,
            max_points,
            bool(binary),
        )
        return _frame_cloud_response(payload) if binary else _json_safe(payload)
    except PointCloudError as exc:
        raise HTTPException(
            status_code=400,
            detail="E57 point-cloud preview could not be generated.",
        ) from exc
    finally:
        temp_path.unlink(missing_ok=True)


@router.post("/api/point-cloud/preview-npz")
async def preview_npz(
    file: UploadFile = File(...),
    max_points: int = Form(0),
    binary: int = 0,
):
    if not (file.filename or "").lower().endswith(".npz"):
        raise HTTPException(status_code=400, detail="Please upload an .npz point-cloud file.")
    raw = await file.read(MAX_PC_BYTES + 1)
    if len(raw) > MAX_PC_BYTES:
        raise HTTPException(status_code=413, detail="NPZ point-cloud file exceeds the upload limit.")
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded NPZ file is empty.")
    try:
        payload = await asyncio.to_thread(_npz_preview, raw, max_points, bool(binary))
        return _frame_cloud_response(payload) if binary else _json_safe(payload)
    except PointCloudError as exc:
        raise HTTPException(
            status_code=400,
            detail="NPZ point-cloud preview could not be generated.",
        ) from exc


async def _run_analysis(
    request: Request,
    state_code: Optional[str] = None,
    bridge_number: Optional[str] = None,
):
    pipeline_mode = _active_pipeline_mode()
    raw_points, _colors, params = await _cloud_payload(request)
    captured_row = None
    captured_identity = None
    captured_snapshot = None
    canonical_context: PointCloudCanonicalContext | None = None
    knowledge_pack: PointCloudKnowledgePack | None = None
    persistence_source_row = None
    if state_code is not None and bridge_number is not None:
        if raw_points is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "saved_point_cloud_source_required",
                    "message": "Bridge-scoped analysis must use the immutable saved source; use the ad-hoc endpoint for inline preview points.",
                },
            )
        requested_year = _requested_data_year(params)
        captured_row = _raw_cloud_row(state_code, bridge_number)
        if captured_row is not None:
            try:
                captured_identity = _source_identity(captured_row)
            except ValueError as exc:
                raise HTTPException(
                    status_code=409,
                    detail="Saved point cloud is not integrity-verified; re-upload it before analysis.",
                ) from exc
        try:
            canonical_context, knowledge_pack = await asyncio.to_thread(
                _build_exact_bridge_knowledge_pack,
                state_code=state_code,
                bridge_number=bridge_number,
                data_year=requested_year,
                source_row=captured_row,
            )
            captured_snapshot = canonical_context.identity.public_payload()
        except PublishedSnapshotRequired as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": exc.code,
                    "message": "Publish the exact bridge snapshot before bridge-scoped point-cloud analysis.",
                },
            ) from exc
        except (PointCloudContextError, PointCloudKnowledgePackError) as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": getattr(exc, "code", "point_cloud_context_unavailable"),
                    "message": str(exc),
                },
            ) from exc
        persistence_source_row = captured_row if raw_points is None else None
        points = await _analysis_points(
            state_code,
            bridge_number,
            raw_points,
            params,
            to_world=True,
            source_row=persistence_source_row,
        )
        canonical_document = canonical_context.canonical_document
        under_feature = _canonical_under_feature_kind(canonical_document)
        context = knowledge_pack.provider_context()
        # Internal measurement evidence is kept separate from the bounded VLM
        # prompt projection.  It supplies the exact canonical feature rows to the
        # deterministic feature-binding layer without exposing raw DB schema or
        # letting the model select a feature identity.
        context["_measurement_features"] = [
            dict(row)
            for row in canonical_document.get("Features", [])
            if isinstance(row, Mapping)
        ]
    else:
        points = _require_points(raw_points, params, to_world=True)
        under_feature = params.get("under_feature") or None
        context = None
    analysis_profile = {
        "analysis_version": 2,
        "algorithm": "hosted-bridge-analysis",
        "scale_known": (
            True
            if canonical_context is not None
            else _param_bool(params, "scale_known", True)
        ),
        "under_feature": under_feature,
        "use_vlm": _param_bool(params, "use_vlm", True),
        "use_ai_cleaning": _param_bool(
            params,
            "use_ai_cleaning",
            _param_bool(params, "use_sam", True),
        ),
        "model_mode": params.get("model_mode") or "clearance",
        "use_local_sam": _param_bool(params, "use_local_sam", False),
        "knowledge_pack_sha256": knowledge_pack.sha256 if knowledge_pack else None,
        "knowledge_pack_version": (
            knowledge_pack.payload.get("schema_version") if knowledge_pack else None
        ),
    }
    try:
        result = await asyncio.to_thread(
            analyze_bridge,
            points,
            analysis_profile["scale_known"],
            under_feature,
            analysis_profile["use_vlm"],
            analysis_profile["use_ai_cleaning"],
            analysis_profile["model_mode"],
            context,
            analysis_profile["use_local_sam"],
        )
    except PointCloudError as exc:
        raise HTTPException(
            status_code=400,
            detail="Point-cloud analysis request could not be processed.",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="Point-cloud analysis failed."
        ) from exc
    source_status = _legacy_source_status(
        state_code=state_code,
        bridge_number=bridge_number,
        raw_points=raw_points,
        snapshot_identity=captured_snapshot,
    )
    result = _annotate_legacy_result(
        result,
        pipeline_mode=pipeline_mode,
        scale_known=analysis_profile["scale_known"],
        source_status=source_status,
    )
    if knowledge_pack is not None:
        result["knowledge_pack"] = {
            "sha256": knowledge_pack.sha256,
            "byte_size": knowledge_pack.byte_size,
            "schema_version": knowledge_pack.payload["schema_version"],
            "snapshot_revision_id": canonical_context.identity.snapshot_revision_id,
            "canonical_sha256": canonical_context.identity.canonical_sha256,
            "report_source_set_sha256": knowledge_pack.payload[
                "untrusted_report_evidence"
            ].get("source_set_sha256"),
        }
    if state_code is not None and bridge_number is not None and result.get("status") == "success":
        try:
            async with _bridge_write_lock(state_code, bridge_number):
                current_row = _raw_cloud_row(state_code, bridge_number)
                try:
                    current_identity = (
                        _source_identity(current_row)
                        if current_row is not None
                        else None
                    )
                except ValueError as exc:
                    raise _StalePointCloudSource from exc
                if captured_identity != current_identity:
                    raise _StalePointCloudSource
                try:
                    current_context, current_pack = await asyncio.to_thread(
                        _build_exact_bridge_knowledge_pack,
                        state_code=state_code,
                        bridge_number=bridge_number,
                        data_year=canonical_context.identity.data_year,
                        source_row=current_row,
                    )
                    current_snapshot = current_context.identity.public_payload()
                except (PointCloudContextError, PointCloudKnowledgePackError) as exc:
                    raise _StalePointCloudSource from exc
                if (
                    captured_snapshot != current_snapshot
                    or knowledge_pack is None
                    or current_pack.sha256 != knowledge_pack.sha256
                ):
                    raise _StalePointCloudSource
                result["persistence"] = await asyncio.to_thread(
                    _save_processed,
                    state_code,
                    bridge_number,
                    points,
                    result,
                    source_row=persistence_source_row,
                    analysis_profile=analysis_profile,
                    snapshot_identity=captured_snapshot,
                    knowledge_pack_json=knowledge_pack.canonical_json,
                )
        except _StalePointCloudSource:
            result.setdefault("warnings", []).append(
                "The saved source or published snapshot changed during analysis; rerun the analysis."
            )
            result["persistence"] = {
                "saved": False,
                "error_code": "point_cloud_source_changed",
            }
        except Exception:
            result.setdefault("warnings", []).append(
                "The processed result could not be published."
            )
            result["persistence"] = {
                "saved": False,
                "error_code": "point_cloud_publication_failed",
            }
    return _json_safe(result)


@router.post("/api/bridges/{state_code}/{bridge_number}/point-cloud/analyze")
async def analyze_bridge_cloud(state_code: str, bridge_number: str, request: Request):
    _require_durable_bridge_processing()
    async with _point_cloud_quota(request, "analyze"):
        return await _run_analysis(request, state_code, bridge_number)


@router.post("/api/point-cloud/analyze")
async def analyze_ad_hoc_cloud(request: Request):
    async with _point_cloud_quota(request, "analyze"):
        return await _run_analysis(request)


async def _run_segment(
    request: Request,
    state_code: Optional[str] = None,
    bridge_number: Optional[str] = None,
):
    pipeline_mode = _active_pipeline_mode()
    raw_points, colors, params = await _cloud_payload(request)
    knowledge_pack: PointCloudKnowledgePack | None = None
    canonical_context: PointCloudCanonicalContext | None = None
    source_row = None
    source_intensity = None
    if state_code is not None and bridge_number is not None:
        if raw_points is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "saved_point_cloud_source_required",
                    "message": "Bridge-scoped segmentation must use the immutable saved source; use the ad-hoc endpoint for inline preview points.",
                },
            )
        source_row = _raw_cloud_row(state_code, bridge_number)
        try:
            canonical_context, knowledge_pack = await asyncio.to_thread(
                _build_exact_bridge_knowledge_pack,
                state_code=state_code,
                bridge_number=bridge_number,
                data_year=_requested_data_year(params),
                source_row=source_row,
            )
        except PublishedSnapshotRequired as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": exc.code,
                    "message": "Publish the exact bridge snapshot before bridge-scoped segmentation.",
                },
            ) from exc
        except (PointCloudContextError, PointCloudKnowledgePackError) as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": getattr(exc, "code", "point_cloud_context_unavailable"), "message": str(exc)},
            ) from exc
        loaded = await _analysis_points(
            state_code,
            bridge_number,
            raw_points,
            params,
            to_world=False,
            source_row=source_row,
            include_attributes=True,
        )
        if isinstance(loaded, tuple):
            points, source_attributes = loaded
        else:  # compatibility with narrow test seams
            points, source_attributes = loaded, {}
        source_rgb = source_attributes.get("rgb")
        if (
            source_rgb is not None
            and np.asarray(source_rgb).shape == (len(points), 3)
            and np.isfinite(source_rgb).all()
        ):
            colors = np.asarray(source_rgb)
        raw_intensity = source_attributes.get("intensity")
        if (
            raw_intensity is not None
            and np.asarray(raw_intensity).reshape(-1).shape == (len(points),)
            and np.isfinite(raw_intensity).all()
        ):
            source_intensity = np.asarray(raw_intensity).reshape(-1)
        bridge_context = knowledge_pack.provider_context()
    else:
        points = _require_points(raw_points, params, to_world=False)
        bridge_context = None
    try:
        result = await asyncio.to_thread(
            segment_structure,
            points,
            colors,
            _param_bool(params, "use_qwen", True),
            _param_bool(params, "use_local_sam", False),
            True,
            "",
            bridge_context,
            source_intensity,
        )
        if knowledge_pack is not None and canonical_context is not None:
            current_row = _raw_cloud_row(state_code, bridge_number)
            try:
                current_context, current_pack = await asyncio.to_thread(
                    _build_exact_bridge_knowledge_pack,
                    state_code=state_code,
                    bridge_number=bridge_number,
                    data_year=canonical_context.identity.data_year,
                    source_row=current_row,
                )
            except (PointCloudContextError, PointCloudKnowledgePackError) as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "point_cloud_context_changed",
                        "message": "The saved source, publication, or report evidence changed during segmentation; rerun it.",
                    },
                ) from exc
            if (
                _source_identity(current_row) != _source_identity(source_row)
                or current_context.identity != canonical_context.identity
                or current_pack.sha256 != knowledge_pack.sha256
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "point_cloud_context_changed",
                        "message": "The saved source, publication, or report evidence changed during segmentation; rerun it.",
                    },
                )
            result["knowledge_pack"] = {
                "sha256": knowledge_pack.sha256,
                "byte_size": knowledge_pack.byte_size,
                "schema_version": knowledge_pack.payload["schema_version"],
                "snapshot_revision_id": canonical_context.identity.snapshot_revision_id,
                "canonical_sha256": canonical_context.identity.canonical_sha256,
            }
        return _json_safe(
            _annotate_legacy_result(
                result,
                pipeline_mode=pipeline_mode,
                scale_known=False,
                source_status=_legacy_source_status(
                    state_code=state_code,
                    bridge_number=bridge_number,
                    raw_points=raw_points,
                ),
            )
        )
    except PointCloudError as exc:
        raise HTTPException(
            status_code=400,
            detail="Point-cloud segmentation request could not be processed.",
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Segmentation failed.") from exc


@router.post("/api/bridges/{state_code}/{bridge_number}/point-cloud/segment")
async def segment_bridge_cloud(state_code: str, bridge_number: str, request: Request):
    _require_durable_bridge_processing()
    async with _point_cloud_quota(request, "segment"):
        return await _run_segment(request, state_code, bridge_number)


@router.post("/api/point-cloud/segment")
async def segment_ad_hoc_cloud(request: Request):
    async with _point_cloud_quota(request, "segment"):
        return await _run_segment(request)


@router.post("/api/point-cloud/smooth")
async def smooth_cloud(request: Request):
    raw_points, colors, params = await _cloud_payload(request)
    points = _require_points(raw_points, params, to_world=False)
    iterations = max(1, min(4, int(params.get("iterations") or 1)))
    origin = _param_origin(params).tolist() if params.get("origin") else None
    as_binary = _param_bool(params, "binary", False)
    try:
        result = await asyncio.to_thread(
            clean_and_smooth_preview,
            points,
            colors,
            origin,
            True,
            iterations,
            300_000,
            "native",
            as_binary,
        )
        return _frame_cloud_response(result) if as_binary and result.get("status") == "success" else _json_safe(result)
    except PointCloudError as exc:
        raise HTTPException(
            status_code=400,
            detail="Point-cloud smoothing request could not be processed.",
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Smoothing failed.") from exc


async def _run_clearance(
    request: Request,
    state_code: Optional[str] = None,
    bridge_number: Optional[str] = None,
):
    pipeline_mode = _active_pipeline_mode()
    raw_points, _colors, params = await _cloud_payload(request)
    scale_known = _param_bool(params, "scale_known", True)
    canonical_document: Mapping[str, Any] | None = None
    if state_code is not None and bridge_number is not None:
        if raw_points is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "saved_point_cloud_source_required",
                    "message": "Bridge-scoped clearance must use the immutable saved source; use the ad-hoc endpoint for inline preview points.",
                },
            )
        requested_year = _requested_data_year(params)
        try:
            canonical_context = await asyncio.to_thread(
                resolve_point_cloud_canonical_context,
                USER_DB,
                workspace_id=POINT_CLOUD_WORKSPACE_ID,
                state_code=int(state_code),
                bridge_number=str(bridge_number),
                data_year=requested_year,
            )
        except PublishedSnapshotRequired as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": exc.code,
                    "message": "Publish the exact bridge snapshot before bridge-scoped clearance screening.",
                },
            ) from exc
        except PointCloudContextError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        # A saved bridge source reaches this point only when its reviewed source
        # receipt authorizes metric units and a gravity-aligned frame.
        scale_known = True
        points = await _analysis_points(
            state_code, bridge_number, raw_points, params, to_world=True
        )
        canonical_document = canonical_context.canonical_document
        under_feature = _canonical_under_feature_kind(canonical_document)
        db_clearance = _canonical_db_clearance_ft(canonical_document) if scale_known else None
    else:
        points = _require_points(raw_points, params, to_world=True)
        under_feature = params.get("under_feature") or None
        raw_db = params.get("db_clearance_ft")
        db_clearance = float(raw_db) if scale_known and raw_db not in (None, "") else None
    try:
        result = await asyncio.to_thread(
            compute_clearance,
            points,
            64,
            db_clearance,
            True,
            scale_known,
            under_feature,
        )
        feature_rows = (
            canonical_document.get("Features", [])
            if isinstance(canonical_document, Mapping)
            else []
        )
        workflow = build_feature_measurement_workflow(
            feature_rows=[
                dict(row) for row in feature_rows if isinstance(row, Mapping)
            ],
            geometry_result=result,
            under_feature=under_feature,
        )
        if result.get("status") == "success":
            result = {
                "status": workflow["status"],
                "reason": workflow["reason"],
                "under_feature": under_feature,
                "feature_measurement": workflow,
                "raw_geometry_candidate": result,
                "note": (
                    "Raw local surface evidence is retained, but BH12/BH13 require "
                    "reviewed feature, lane/shoulder, station, and obstruction inputs."
                ),
            }
        else:
            result["feature_measurement"] = workflow
        return _json_safe(
            _annotate_legacy_result(
                result,
                pipeline_mode=pipeline_mode,
                scale_known=scale_known,
                source_status=_legacy_source_status(
                    state_code=state_code,
                    bridge_number=bridge_number,
                    raw_points=raw_points,
                    snapshot_identity=(
                        canonical_context.identity.public_payload()
                        if state_code is not None and bridge_number is not None
                        else None
                    ),
                ),
            )
        )
    except PointCloudError as exc:
        raise HTTPException(
            status_code=400,
            detail="Point-cloud clearance request could not be processed.",
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Clearance analysis failed.") from exc


@router.post("/api/bridges/{state_code}/{bridge_number}/point-cloud/clearance")
async def bridge_clearance(state_code: str, bridge_number: str, request: Request):
    _require_durable_bridge_processing()
    async with _point_cloud_quota(request, "clearance"):
        return await _run_clearance(request, state_code, bridge_number)


@router.post("/api/point-cloud/clearance")
async def ad_hoc_clearance(request: Request):
    async with _point_cloud_quota(request, "clearance"):
        return await _run_clearance(request)
