"""Durable point-cloud analysis job adapter.

HTTP enqueue freezes the source, published bridge revision, knowledge pack, and
analysis profile.  A worker later verifies all four identities before touching
the VLM or geometry pipeline, appends content-free evidence receipts, and
publishes only an immutable candidate artifact.  Human acceptance is separate.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, Mapping

from fastapi import HTTPException

from .services.conversion.canonical_json import canonical_sha256
from .services.operations.jobs import (
    JobStoreError,
    OperationJob,
    enqueue_job,
    get_job,
    is_job_cancellation_requested,
    request_job_cancellation,
)
from .services.point_cloud.run_store import (
    AppendPointCloudEvidenceRequest,
    CreatePointCloudRunRequest,
    PointCloudComponentEvidence,
    PointCloudMeasurementEvidence,
    PointCloudRunConflictError,
    PointCloudRunNotFoundError,
    PointCloudSourceEvidence,
    PointCloudVlmEvidence,
    TransitionPointCloudRunRequest,
    append_point_cloud_run_evidence,
    create_point_cloud_run,
    get_point_cloud_run,
    get_point_cloud_run_by_job,
    request_point_cloud_run_cancel,
    transition_point_cloud_run,
    update_point_cloud_run_progress,
)


POINT_CLOUD_JOB_OPERATION = "point_cloud_analysis"
POINT_CLOUD_JOB_FORMAT = "bridgeguard-point-cloud-job-v1"
POINT_CLOUD_ANALYSIS_PROFILE_VERSION = "bridgeguard-point-cloud-analysis-v3"
_SAFE_MODEL_MODES = frozenset({"clearance", "structural"})


class PointCloudJobError(RuntimeError):
    def __init__(self, code: str, *, status: int = 409, retry: bool = False) -> None:
        self.code = str(code)
        self.status = int(status)
        self.retry = bool(retry)
        super().__init__(self.code)


class PointCloudJobCancelled(PointCloudJobError):
    def __init__(self) -> None:
        super().__init__("cancelled_by_user", retry=False)


def _api():
    from .routers import point_cloud_api

    return point_cloud_api


def _analysis_profile(
    *,
    knowledge_pack_sha256: str,
    under_feature: str | None,
    model_mode: str,
    use_vlm: bool,
) -> dict[str, object]:
    mode = str(model_mode or "clearance").strip().lower()
    if mode not in _SAFE_MODEL_MODES:
        raise PointCloudJobError("invalid_point_cloud_model_mode", status=422)
    return {
        "profile_version": POINT_CLOUD_ANALYSIS_PROFILE_VERSION,
        "algorithm": "hosted-bridge-analysis",
        "scale_known": True,
        "under_feature": under_feature,
        "use_vlm": bool(use_vlm),
        "use_ai_cleaning": True,
        "model_mode": mode,
        "use_local_sam": False,
        "knowledge_pack_sha256": knowledge_pack_sha256,
    }


def _source_from_current(state_code: str, bridge_number: str):
    api = _api()
    row = api._raw_cloud_row(state_code, bridge_number)
    if row is None:
        raise PointCloudJobError("point_cloud_source_not_found", status=404)
    try:
        source = api._point_cloud_source_evidence(row)
        receipt = api._public_source_receipt(api._load_source_receipt(row))
    except Exception as exc:
        raise PointCloudJobError("point_cloud_source_evidence_unavailable", status=409) from exc
    if not isinstance(receipt, Mapping):
        raise PointCloudJobError("point_cloud_source_evidence_unavailable", status=409)
    source_receipt = {
        "attachment_id": source["attachment_id"],
        "sha256": source["sha256"],
        "byte_size": source["byte_size"],
        "source_profile": dict(receipt),
    }
    return row, source, source_receipt


def enqueue_point_cloud_analysis(
    *,
    database_path: str | Path,
    job_database_path: str | Path,
    workspace_id: str,
    owner_id: str,
    idempotency_key: str,
    state_code: str,
    bridge_number: str,
    data_year: int | None,
    model_mode: str = "clearance",
    use_vlm: bool = True,
) -> tuple[OperationJob, object, bool]:
    """Freeze exact identities and enqueue without running geometry or a model."""

    api = _api()
    row, source, source_receipt = _source_from_current(state_code, bridge_number)
    try:
        context, pack = api._build_exact_bridge_knowledge_pack(
            state_code=str(state_code),
            bridge_number=str(bridge_number),
            data_year=data_year,
            source_row=row,
        )
    except Exception as exc:
        raise PointCloudJobError("point_cloud_context_unavailable", status=409) from exc
    if context.identity.workspace_id != workspace_id:
        raise PointCloudJobError("point_cloud_context_unavailable", status=404)
    under_feature = api._canonical_under_feature_kind(context.canonical_document)
    profile = _analysis_profile(
        knowledge_pack_sha256=pack.sha256,
        under_feature=under_feature,
        model_mode=model_mode,
        use_vlm=use_vlm,
    )
    report = pack.payload.get("untrusted_report_evidence")
    report_hash = (
        str(report.get("source_set_sha256"))
        if isinstance(report, Mapping) and report.get("source_set_sha256")
        else None
    )
    payload: dict[str, object] = {
        "format": POINT_CLOUD_JOB_FORMAT,
        "bridge_snapshot": context.identity.public_payload(),
        "source": source_receipt,
        "analysis_profile": profile,
        "analysis_profile_sha256": canonical_sha256(profile),
        "knowledge_pack_sha256": pack.sha256,
        "report_source_set_sha256": report_hash,
    }
    try:
        job = enqueue_job(
            job_database_path,
            workspace_id=workspace_id,
            owner_id=owner_id,
            operation=POINT_CLOUD_JOB_OPERATION,
            idempotency_key=idempotency_key,
            payload=payload,
            max_attempts=3,
        )
    except (JobStoreError, TypeError, ValueError) as exc:
        if isinstance(exc, JobStoreError):
            raise
        raise PointCloudJobError("invalid_point_cloud_job", status=422) from exc
    run_request = CreatePointCloudRunRequest(
        workspace_id=workspace_id,
        actor_id=owner_id,
        job_id=job.id,
        idempotency_key=idempotency_key,
        state_code=int(context.identity.state_code),
        bridge_number=context.identity.bridge_number,
        data_year=int(context.identity.data_year),
        snapshot_revision_id=context.identity.snapshot_revision_id,
        canonical_sha256=context.identity.canonical_sha256,
        source=PointCloudSourceEvidence(
            source_kind="point_cloud",
            source_id=f"attachment:{source['attachment_id']}",
            sha256=str(source["sha256"]),
            byte_size=int(source["byte_size"]),
            receipt=source_receipt,
        ),
        analysis_profile_sha256=str(payload["analysis_profile_sha256"]),
        knowledge_pack_sha256=pack.sha256,
        report_source_set_sha256=report_hash,
    )
    try:
        run, created = create_point_cloud_run(str(database_path), run_request)
    except (PointCloudRunConflictError, PointCloudRunNotFoundError):
        raise
    return job, run, not created


def _verify_frozen_scope(database_path: str | Path, job: OperationJob):
    api = _api()
    if job.operation != POINT_CLOUD_JOB_OPERATION or job.payload.get("format") != POINT_CLOUD_JOB_FORMAT:
        raise PointCloudJobError("point_cloud_job_payload_corrupt", status=500)
    identity = job.payload.get("bridge_snapshot")
    if not isinstance(identity, Mapping):
        raise PointCloudJobError("point_cloud_job_payload_corrupt", status=500)
    state_code = str(identity.get("state_code") or "")
    bridge_number = str(identity.get("bridge_number") or "")
    data_year = int(identity.get("data_year") or 0)
    row, source, _receipt = _source_from_current(state_code, bridge_number)
    claimed_source = job.payload.get("source")
    if not isinstance(claimed_source, Mapping):
        raise PointCloudJobError("point_cloud_job_payload_corrupt", status=500)
    if (
        str(source["sha256"]) != str(claimed_source.get("sha256") or "")
        or int(source["byte_size"]) != int(claimed_source.get("byte_size") or 0)
        or str(source["attachment_id"]) != str(claimed_source.get("attachment_id") or "")
    ):
        raise PointCloudJobError("point_cloud_source_changed", retry=False)
    try:
        context, pack = api._build_exact_bridge_knowledge_pack(
            state_code=state_code,
            bridge_number=bridge_number,
            data_year=data_year,
            source_row=row,
        )
    except Exception as exc:
        raise PointCloudJobError("point_cloud_context_unavailable", retry=True) from exc
    if context.identity.public_payload() != dict(identity):
        raise PointCloudJobError("point_cloud_snapshot_changed", retry=False)
    if pack.sha256 != str(job.payload.get("knowledge_pack_sha256") or ""):
        raise PointCloudJobError("point_cloud_knowledge_pack_changed", retry=False)
    report = pack.payload.get("untrusted_report_evidence")
    report_hash = (
        str(report.get("source_set_sha256"))
        if isinstance(report, Mapping) and report.get("source_set_sha256")
        else None
    )
    if report_hash != job.payload.get("report_source_set_sha256"):
        raise PointCloudJobError("point_cloud_report_sources_changed", retry=False)
    profile = job.payload.get("analysis_profile")
    if not isinstance(profile, Mapping) or canonical_sha256(profile) != str(job.payload.get("analysis_profile_sha256") or ""):
        raise PointCloudJobError("point_cloud_analysis_profile_corrupt", status=500)
    active = _analysis_profile(
        knowledge_pack_sha256=pack.sha256,
        under_feature=api._canonical_under_feature_kind(context.canonical_document),
        model_mode=str(profile.get("model_mode") or ""),
        use_vlm=bool(profile.get("use_vlm")),
    )
    if canonical_sha256(active) != canonical_sha256(profile):
        raise PointCloudJobError("point_cloud_analysis_profile_changed", retry=False)
    return api, row, context, pack, active


def verify_point_cloud_job_scope(
    *, database_path: str | Path, job: OperationJob
) -> None:
    """Revalidate every frozen source/context receipt before human acceptance."""

    _verify_frozen_scope(database_path, job)


def _cancel_requested(job_database_path: str | Path, job: OperationJob) -> bool:
    return is_job_cancellation_requested(
        job_database_path,
        workspace_id=job.workspace_id,
        owner_id=job.owner_id,
        job_id=job.id,
    )


def _safe_component_payload(component: Mapping[str, Any], manifest_sha256: str) -> dict[str, Any]:
    keys = (
        "id", "key", "label", "kind", "point_count", "proposed_class",
        "final_class", "fusion_disposition", "review_status", "protected",
        "removal_eligible", "deterministic_evidence", "vlm_label",
        "vlm_specific_label", "vlm_material", "vlm_confidence",
        "vlm_evidence_ids", "geometry", "bounds", "dimensions", "quality",
    )
    result = {key: component[key] for key in keys if key in component}
    result["artifact_manifest_sha256"] = manifest_sha256
    return result


def _evidence_request(
    *,
    workspace_id: str,
    run_id: str,
    pack: Any,
    result: Mapping[str, Any],
    manifest_sha256: str,
) -> AppendPointCloudEvidenceRequest:
    report = pack.payload.get("untrusted_report_evidence")
    report_sources = report.get("sources") if isinstance(report, Mapping) else []
    sources = []
    for item in report_sources if isinstance(report_sources, list) else []:
        if not isinstance(item, Mapping):
            continue
        sources.append(
            PointCloudSourceEvidence(
                source_kind="inspection_report",
                source_id=f"report:{item.get('report_id')}",
                sha256=str(item.get("sha256") or ""),
                byte_size=int(item.get("byte_size") or 0),
                receipt={
                    "report_id": str(item.get("report_id") or ""),
                    "sha256": str(item.get("sha256") or ""),
                    "byte_size": int(item.get("byte_size") or 0),
                    "report_source_set_sha256": report.get("source_set_sha256"),
                },
            )
        )
    vlm_calls = tuple(
        PointCloudVlmEvidence(dict(item))
        for item in result.get("vlm_call_receipts", [])
        if isinstance(item, Mapping)
    )
    components = tuple(
        PointCloudComponentEvidence(
            candidate_id=str(item.get("id") or item.get("key") or f"component-{index}"),
            proposed_class=str(item.get("proposed_class") or "unknown_structure"),
            final_class=str(item.get("final_class") or "unknown_structure"),
            disposition=str(item.get("fusion_disposition") or "retained_unreviewed"),
            payload=_safe_component_payload(item, manifest_sha256),
        )
        for index, item in enumerate(result.get("components", []), start=1)
        if isinstance(item, Mapping)
    )
    clearance = result.get("clearance")
    workflow = clearance.get("feature_measurement") if isinstance(clearance, Mapping) else None
    bindings = workflow.get("bindings") if isinstance(workflow, Mapping) else []
    measurements = []
    if isinstance(bindings, list) and bindings:
        for index, binding in enumerate(bindings, start=1):
            if not isinstance(binding, Mapping):
                continue
            measurements.append(
                PointCloudMeasurementEvidence(
                    candidate_id=str(binding.get("detected_id") or f"measurement-{index}"),
                    feature_id=(str(binding.get("feature_id")) if binding.get("feature_id") else None),
                    product="BH12_BH13",
                    status=str(workflow.get("status") or "review_required"),
                    payload={
                        "binding": dict(binding),
                        "reason": workflow.get("reason"),
                        "feature_binding_version": workflow.get("feature_binding_version"),
                        "clearance_profile_version": workflow.get("clearance_profile_version"),
                        "artifact_manifest_sha256": manifest_sha256,
                    },
                )
            )
    else:
        measurements.append(
            PointCloudMeasurementEvidence(
                candidate_id="measurement-gate",
                feature_id=None,
                product="BH12_BH13",
                status=str(workflow.get("status") if isinstance(workflow, Mapping) else "not_measurable"),
                payload={
                    "reason": workflow.get("reason") if isinstance(workflow, Mapping) else "measurement_evidence_unavailable",
                    "artifact_manifest_sha256": manifest_sha256,
                },
            )
        )
    return AppendPointCloudEvidenceRequest(
        workspace_id=workspace_id,
        run_id=run_id,
        sources=tuple(sources),
        vlm_calls=vlm_calls,
        components=components,
        measurements=tuple(measurements),
    )


def process_point_cloud_analysis_job(
    *,
    job: OperationJob,
    database_path: str | Path,
    job_database_path: str | Path,
) -> dict[str, object]:
    """Run one claimed job; exact evidence appends are restart-safe."""

    if job.status != "running":
        raise PointCloudJobError("invalid_point_cloud_worker_job", status=500)
    try:
        run = get_point_cloud_run_by_job(str(database_path), job.workspace_id, job.id)
    except PointCloudRunNotFoundError as exc:
        raise PointCloudJobError("point_cloud_run_initializing", retry=True) from exc
    if _cancel_requested(job_database_path, job) or run.status == "cancelled":
        if run.status in {"queued", "running"}:
            request_point_cloud_run_cancel(str(database_path), job.workspace_id, run.run_id, run.actor_id)
        raise PointCloudJobCancelled()
    api, row, context, pack, profile = _verify_frozen_scope(database_path, job)
    if run.status == "queued":
        run = transition_point_cloud_run(
            str(database_path),
            TransitionPointCloudRunRequest(job.workspace_id, run.run_id, "queued", "running", "verify_source", {"completed": 1, "total": 8}),
        )
    elif run.status != "running":
        if run.status == "succeeded":
            return {
                "run_id": run.run_id,
                "result_sha256": run.result_sha256,
                "source_generation": run.progress.get("source_generation"),
                "idempotent": True,
            }
        raise PointCloudJobCancelled() if run.status == "cancelled" else PointCloudJobError("point_cloud_run_terminal", retry=False)
    update_point_cloud_run_progress(str(database_path), workspace_id=job.workspace_id, run_id=run.run_id, expected_status="running", phase="parse_source", progress={"completed": 2, "total": 8})
    if _cancel_requested(job_database_path, job):
        request_point_cloud_run_cancel(str(database_path), job.workspace_id, run.run_id, run.actor_id)
        raise PointCloudJobCancelled()
    try:
        points = asyncio.run(
            api._analysis_points(
                str(context.identity.state_code),
                context.identity.bridge_number,
                None,
                {"data_year": context.identity.data_year},
                True,
                source_row=row,
            )
        )
    except HTTPException as exc:
        raise PointCloudJobError("point_cloud_source_parse_failed", retry=False) from exc
    update_point_cloud_run_progress(str(database_path), workspace_id=job.workspace_id, run_id=run.run_id, expected_status="running", phase="geometry_and_vlm", progress={"completed": 3, "total": 8, "provider_interruption": "application_boundaries_only"})
    provider_context = pack.provider_context()
    provider_context["_measurement_features"] = [
        dict(item)
        for item in context.canonical_document.get("Features", [])
        if isinstance(item, Mapping)
    ]
    try:
        result = api.analyze_bridge(
            points,
            True,
            profile.get("under_feature"),
            bool(profile.get("use_vlm")),
            True,
            str(profile.get("model_mode")),
            provider_context,
            False,
        )
    except Exception as exc:
        raise PointCloudJobError("point_cloud_analysis_failed", retry=False) from exc
    if _cancel_requested(job_database_path, job):
        request_point_cloud_run_cancel(str(database_path), job.workspace_id, run.run_id, run.actor_id)
        raise PointCloudJobCancelled()
    if not isinstance(result, dict) or result.get("status") != "success":
        raise PointCloudJobError("point_cloud_analysis_incomplete", retry=False)
    update_point_cloud_run_progress(str(database_path), workspace_id=job.workspace_id, run_id=run.run_id, expected_status="running", phase="persist_candidate", progress={"completed": 6, "total": 8})
    try:
        persistence = api._save_processed(
            str(context.identity.state_code),
            context.identity.bridge_number,
            points,
            result,
            source_row=row,
            analysis_profile=dict(profile),
            snapshot_identity=context.identity.public_payload(),
            knowledge_pack_json=pack.canonical_json,
            publish_pointer=False,
        )
    except Exception as exc:
        raise PointCloudJobError("point_cloud_candidate_persistence_failed", retry=True) from exc
    manifest_sha256 = str(persistence.get("manifest_sha256") or "")
    if len(manifest_sha256) != 64:
        raise PointCloudJobError("point_cloud_candidate_receipt_invalid", status=500)
    append_point_cloud_run_evidence(
        str(database_path),
        _evidence_request(
            workspace_id=job.workspace_id,
            run_id=run.run_id,
            pack=pack,
            result=result,
            manifest_sha256=manifest_sha256,
        ),
    )
    if _cancel_requested(job_database_path, job):
        request_point_cloud_run_cancel(str(database_path), job.workspace_id, run.run_id, run.actor_id)
        raise PointCloudJobCancelled()
    completed = transition_point_cloud_run(
        str(database_path),
        TransitionPointCloudRunRequest(
            job.workspace_id,
            run.run_id,
            "running",
            "succeeded",
            "review_required",
            {
                "completed": 8,
                "total": 8,
                "source_generation": persistence.get("source_generation"),
                "artifact_sha256": persistence.get("artifact_sha256"),
                "manifest_sha256": manifest_sha256,
                "publication_status": "candidate_not_published",
            },
            manifest_sha256,
        ),
    )
    return {
        "run_id": completed.run_id,
        "result_sha256": manifest_sha256,
        "source_generation": persistence.get("source_generation"),
        "component_count": completed.component_count,
        "measurement_count": completed.measurement_count,
        "review_required": True,
    }


def mark_point_cloud_job_failed(
    *, database_path: str | Path, job: OperationJob, error_code: str
) -> None:
    try:
        run = get_point_cloud_run_by_job(str(database_path), job.workspace_id, job.id)
    except PointCloudRunNotFoundError:
        return
    if run.status == "running":
        transition_point_cloud_run(
            str(database_path),
            TransitionPointCloudRunRequest(
                job.workspace_id,
                run.run_id,
                "running",
                "failed",
                "failed",
                {"failed": True},
                error_code=error_code,
            ),
        )


def get_owned_point_cloud_job(
    job_database_path: str | Path,
    *, workspace_id: str,
    owner_id: str,
    job_id: str,
) -> OperationJob:
    job = get_job(job_database_path, workspace_id=workspace_id, owner_id=owner_id, job_id=job_id)
    if job.operation != POINT_CLOUD_JOB_OPERATION:
        raise PointCloudJobError("point_cloud_run_not_found", status=404)
    return job


def cancel_point_cloud_job(
    *,
    database_path: str | Path,
    job_database_path: str | Path,
    workspace_id: str,
    owner_id: str,
    job_id: str,
) -> OperationJob:
    job = request_job_cancellation(
        job_database_path,
        workspace_id=workspace_id,
        owner_id=owner_id,
        job_id=job_id,
    )
    if job.operation != POINT_CLOUD_JOB_OPERATION:
        raise PointCloudJobError("point_cloud_run_not_found", status=404)
    try:
        run = get_point_cloud_run_by_job(str(database_path), workspace_id, job_id)
        if run.status in {"queued", "running"}:
            request_point_cloud_run_cancel(str(database_path), workspace_id, run.run_id, owner_id)
    except PointCloudRunNotFoundError:
        pass
    return job


def public_point_cloud_job(job: OperationJob, run: Any) -> dict[str, object]:
    status = "cancellation_requested" if job.status == "running" and job.cancellation_requested_at else run.status
    return {
        "job_id": job.id,
        "run_id": run.run_id,
        "data_year": run.data_year,
        "status": status,
        "phase": run.phase,
        "progress": dict(run.progress),
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "error_code": run.error_code or job.error_code,
        "source_sha256": run.source_sha256,
        "analysis_profile_sha256": run.analysis_profile_sha256,
        "knowledge_pack_sha256": run.knowledge_pack_sha256,
        "snapshot_revision_id": run.snapshot_revision_id,
        "canonical_sha256": run.canonical_sha256,
        "counts": {
            "sources": run.source_count,
            "vlm_calls": run.vlm_call_count,
            "components": run.component_count,
            "measurements": run.measurement_count,
            "reviews": run.review_count,
        },
        "review_required": run.status == "succeeded" and run.published_review_id is None,
        "published": run.published_review_id is not None,
        "etag": run.etag,
    }


__all__ = [
    "POINT_CLOUD_JOB_OPERATION",
    "PointCloudJobCancelled",
    "PointCloudJobError",
    "cancel_point_cloud_job",
    "enqueue_point_cloud_analysis",
    "get_owned_point_cloud_job",
    "mark_point_cloud_job_failed",
    "process_point_cloud_analysis_job",
    "public_point_cloud_job",
    "verify_point_cloud_job_scope",
]
