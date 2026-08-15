from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request

from app import main as main_module
from app.auth import AuthUser
from app.services.ai.context import VisualAnalysisLimits
from app.services.bridge_identity import BridgeSnapshotIdentity
from app.services.operations.jobs import (
    JobStoreError,
    claim_next_job,
    complete_job,
    enqueue_job,
    get_job,
    request_job_cancellation,
)
from app.services.operations.quotas import DEFAULT_OPERATION_QUOTAS
from app import visual_jobs
from app.visual_jobs import (
    VISUAL_JOB_FORMAT,
    VISUAL_JOB_OPERATION,
    VisualJobCancelled,
    VisualJobError,
    enqueue_visual_investigation,
    get_owned_visual_job,
    load_visual_job_result,
    process_visual_investigation_job,
    public_visual_job,
)


NOW = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
OWNER = "visual@example.com"
WORKSPACE = main_module.SHARED_WORKSPACE_ID


def _identity(*, revision: str = "revision-1", generation: int = 7) -> BridgeSnapshotIdentity:
    return BridgeSnapshotIdentity(
        workspace_id=WORKSPACE,
        state_code=34,
        bridge_number="A-1",
        data_year=2025,
        snapshot_revision_id=revision,
        revision_number=1,
        canonical_sha256="a" * 64,
        profile_id="native-snbi",
        rule_bundle_sha256="b" * 64,
        published_at="2026-08-13T12:00:00Z",
        data_generation=generation,
        schema_generation=8,
    )


def _profiles() -> dict[str, object]:
    return {
        "profile_version": "test-visual-v1",
        "report_rag_profiles": {"parser": {}, "caption": {}, "embedding": {}, "retrieval": {}},
        "detection": {"model_id": "test-vlm", "segmentation_mode": "fast"},
        "limits": {
            "max_images": 8,
            "max_estimated_input_tokens": 32_000,
            "max_estimated_cost_usd": 0.25,
        },
    }


def _source_receipts() -> tuple[dict[str, object], ...]:
    return (
        {
            "report_id": "4",
            "filename": "inspection.pdf",
            "sha256": "c" * 64,
            "size_bytes": 123,
            "uploaded_at": "2026-08-13T10:00:00Z",
        },
    )


def _payload(identity: BridgeSnapshotIdentity | None = None) -> dict[str, object]:
    frozen = identity or _identity()
    profiles = _profiles()
    receipts = list(_source_receipts())
    return {
        "format": VISUAL_JOB_FORMAT,
        "request_id": str(uuid4()),
        "query": "find spalling",
        "analysis_mode": "visual",
        "bridge_snapshot": frozen.public_payload(),
        "selected_sources": ["inspection.pdf"],
        "source_receipts": receipts,
        "source_set_sha256": visual_jobs.report_sha256(receipts),
        "visual_profile": profiles,
        "visual_profile_sha256": visual_jobs.report_sha256(profiles),
        "defect_segmentation_mode": "fast",
        "conversation_id": None,
    }


def _request(
    path: str,
    *,
    method: str = "POST",
    owner: str = OWNER,
    request_id: str | None = None,
    idempotency_key: str | None = None,
) -> Request:
    headers = [(b"x-request-id", (request_id or str(uuid4())).encode("ascii"))]
    if idempotency_key is not None:
        headers.append((b"idempotency-key", idempotency_key.encode("ascii")))
    request = Request(
        {"type": "http", "method": method, "path": path, "headers": headers}
    )
    request.state.user = AuthUser(owner, "viewer")
    return request


def _queued_visual(path: Path, *, owner: str = OWNER, key: str = "visual-1"):
    return enqueue_job(
        path,
        workspace_id=WORKSPACE,
        owner_id=owner,
        operation=VISUAL_JOB_OPERATION,
        idempotency_key=key,
        payload=_payload(),
        max_attempts=3,
        now=NOW,
    )


def test_visual_enqueue_freezes_exact_identity_sources_profile_and_is_idempotent(tmp_path):
    jobs = tmp_path / "operation_jobs.db"
    receipts = _source_receipts()
    profiles = _profiles()
    kwargs = {
        "database_path": tmp_path / "bridges.db",
        "job_database_path": jobs,
        "workspace_id": WORKSPACE,
        "owner_id": OWNER,
        "idempotency_key": "visual-submit-1",
        "request_id": str(uuid4()),
        "query": "find spalling",
        "bridge_identity": _identity(),
        "selected_sources": ["inspection.pdf"],
        "defect_segmentation_mode": "fast",
        "conversation_id": None,
    }
    with (
        patch.object(visual_jobs, "_source_receipts", return_value=receipts),
        patch.object(visual_jobs, "_profile_payload", return_value=profiles),
    ):
        first, replay = enqueue_visual_investigation(**kwargs)
        second, second_replay = enqueue_visual_investigation(**kwargs)
        assert replay is False and second_replay is True and first.id == second.id
        assert first.payload["bridge_snapshot"] == _identity().public_payload()
        assert first.payload["source_receipts"] == list(receipts)
        assert first.payload["selected_sources"] == ["inspection.pdf"]
        assert first.payload["source_set_sha256"] == visual_jobs.report_sha256(list(receipts))
        assert first.payload["visual_profile_sha256"] == visual_jobs.report_sha256(profiles)

        with pytest.raises(JobStoreError, match="idempotency_payload_conflict"):
            enqueue_visual_investigation(**{**kwargs, "query": "find cracking"})


def test_visual_job_restart_recovery_owner_hiding_and_durable_cancel(tmp_path):
    jobs = tmp_path / "operation_jobs.db"
    queued = _queued_visual(jobs)
    with pytest.raises(JobStoreError) as hidden:
        get_owned_visual_job(
            jobs,
            workspace_id=WORKSPACE,
            owner_id="other@example.com",
            job_id=queued.id,
        )
    assert hidden.value.code == "job_not_found"

    first = claim_next_job(
        jobs,
        worker_id="old-worker",
        allowed_operations=(VISUAL_JOB_OPERATION,),
        lease_seconds=10,
        now=NOW,
    )
    assert first is not None and first.attempts == 1
    recovered = claim_next_job(
        jobs,
        worker_id="new-worker",
        allowed_operations=(VISUAL_JOB_OPERATION,),
        lease_seconds=10,
        now=NOW + timedelta(seconds=11),
    )
    assert recovered is not None and recovered.id == first.id and recovered.attempts == 2
    signalled = request_job_cancellation(
        jobs,
        workspace_id=WORKSPACE,
        owner_id=OWNER,
        job_id=recovered.id,
        now=NOW + timedelta(seconds=12),
    )
    assert signalled.status == "running" and signalled.cancellation_requested_at
    assert public_visual_job(signalled)["status"] == "cancellation_requested"
    # After another process starts and the old lease expires, the cancelled
    # operation is terminal and is never reclaimed.
    assert claim_next_job(
        jobs,
        worker_id="restart-worker",
        allowed_operations=(VISUAL_JOB_OPERATION,),
        lease_seconds=10,
        now=NOW + timedelta(seconds=22),
    ) is None
    terminal = get_job(jobs, workspace_id=WORKSPACE, owner_id=OWNER, job_id=queued.id)
    assert terminal.status == "cancelled"
    assert terminal.error_code == "cancelled_by_user"


def test_visual_worker_persists_path_free_immutable_result_and_releases_quota(tmp_path):
    jobs = tmp_path / "operation_jobs.db"
    artifacts = tmp_path / "artifacts"
    _queued_visual(jobs)
    claimed = claim_next_job(
        jobs,
        worker_id="visual-worker",
        allowed_operations=(VISUAL_JOB_OPERATION,),
        now=NOW,
    )
    assert claimed is not None
    before = DEFAULT_OPERATION_QUOTAS.snapshot(
        workspace_id=WORKSPACE, owner_id=OWNER, operation="visual"
    )
    provider_result = {
        "status": "success",
        "answer": "One likely spall.",
        "path": r"C:\private\report-image.png",
        "images": [
            {
                "filename": "inspection_annotated.png",
                "mime": "image/png",
                "data": "cG5n",
                "filepath": r"C:\private\overlay.png",
            }
        ],
        "detections": [{"source": "inspection.pdf", "detections": [{"label": "spall"}]}],
        "checked_images": 1,
        "visual_evidence": {"visual_processing_ran": True, "usage": {}},
    }
    with (
        patch.object(visual_jobs, "_verify_exact_identity", return_value=_identity()),
        patch.object(visual_jobs, "_verify_profile", return_value=VisualAnalysisLimits()),
        patch.object(visual_jobs, "_verify_exact_sources"),
        patch.object(
            visual_jobs.vision_service,
            "process_bridge_visual_investigation",
            new=AsyncMock(return_value=provider_result),
        ),
    ):
        receipt = asyncio.run(
            process_visual_investigation_job(
                job=claimed,
                database_path=tmp_path / "bridges.db",
                job_database_path=jobs,
                artifact_root=artifacts,
            )
        )
    after = DEFAULT_OPERATION_QUOTAS.snapshot(
        workspace_id=WORKSPACE, owner_id=OWNER, operation="visual"
    )
    assert after == before
    done = complete_job(
        jobs,
        job=claimed,
        worker_id="visual-worker",
        result=receipt,
        now=NOW,
    )
    loaded = load_visual_job_result(job=done, artifact_root=artifacts)
    assert loaded["answer"] == "One likely spall."
    assert "path" not in loaded
    assert "filepath" not in loaded["images"][0]
    assert loaded["request_id"] == claimed.payload["request_id"]
    assert loaded["bridge_snapshot"]["identity"]["snapshot_revision_id"] == "revision-1"
    assert loaded["evidence_receipt_sha256"] == receipt["evidence_receipt_sha256"]
    public = public_visual_job(done)
    assert "payload" not in public and "query" not in json.dumps(public)
    assert "private" not in json.dumps(public).lower()

    artifact_id = str(receipt["result_receipt"]["artifact_id"])
    manifest = artifacts / "visual_investigation_results" / "manifests" / f"{artifact_id}.json"
    tampered = json.loads(manifest.read_text(encoding="utf-8"))
    tampered["sha256"] = "f" * 64
    manifest.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(VisualJobError, match="visual_result_receipt_mismatch"):
        load_visual_job_result(job=done, artifact_root=artifacts)


def test_running_visual_cancellation_wins_late_provider_success_and_releases_quota(tmp_path):
    jobs = tmp_path / "operation_jobs.db"
    _queued_visual(jobs)
    claimed = claim_next_job(
        jobs,
        worker_id="visual-worker",
        allowed_operations=(VISUAL_JOB_OPERATION,),
        now=NOW,
    )
    assert claimed is not None
    before = DEFAULT_OPERATION_QUOTAS.snapshot(
        workspace_id=WORKSPACE, owner_id=OWNER, operation="visual"
    )

    async def late_success(**_kwargs):
        request_job_cancellation(
            jobs,
            workspace_id=WORKSPACE,
            owner_id=OWNER,
            job_id=claimed.id,
        )
        return {"status": "success", "images": [], "detections": []}

    with (
        patch.object(visual_jobs, "_verify_exact_identity", return_value=_identity()),
        patch.object(visual_jobs, "_verify_profile", return_value=VisualAnalysisLimits()),
        patch.object(visual_jobs, "_verify_exact_sources"),
        patch.object(
            visual_jobs.vision_service,
            "process_bridge_visual_investigation",
            new=late_success,
        ),
    ):
        with pytest.raises(VisualJobCancelled):
            asyncio.run(
                process_visual_investigation_job(
                    job=claimed,
                    database_path=tmp_path / "bridges.db",
                    job_database_path=jobs,
                    artifact_root=tmp_path / "artifacts",
                )
            )
    after = DEFAULT_OPERATION_QUOTAS.snapshot(
        workspace_id=WORKSPACE, owner_id=OWNER, operation="visual"
    )
    assert after == before


def test_exact_revision_source_and_profile_tampering_fail_closed(tmp_path):
    jobs = tmp_path / "operation_jobs.db"
    queued = _queued_visual(jobs)
    with patch.object(
        visual_jobs,
        "resolve_published_bridge_identity",
        return_value=_identity(revision="revision-2", generation=8),
    ):
        with pytest.raises(VisualJobError, match="published_bridge_snapshot_changed"):
            visual_jobs._verify_exact_identity(tmp_path / "bridges.db", queued)

    with patch.object(
        visual_jobs,
        "_source_receipts",
        return_value=({**_source_receipts()[0], "sha256": "d" * 64},),
    ):
        with pytest.raises(VisualJobError, match="visual_report_sources_changed"):
            visual_jobs._verify_exact_sources(tmp_path / "bridges.db", queued)

    changed = {**_profiles(), "detection": {"model_id": "different-vlm"}}
    with patch.object(visual_jobs, "_profile_payload", return_value=changed):
        with pytest.raises(VisualJobError, match="visual_profile_changed"):
            visual_jobs._verify_profile(queued)

    # The coordination database also prevents an attacker from rewriting a
    # queued job's immutable evidence identity in place.
    import sqlite3

    connection = sqlite3.connect(jobs)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE operation_jobs SET payload_json='{}' WHERE id=?", (queued.id,)
            )
    finally:
        connection.close()


def test_visual_http_enqueues_statuses_and_cancels_owner_scoped_job(tmp_path, monkeypatch):
    jobs = tmp_path / "operation_jobs.db"
    monkeypatch.setattr(main_module, "_report_job_db", lambda: jobs)
    monkeypatch.setattr(main_module, "_visual_result_artifact_root", lambda: tmp_path / "artifacts")
    identity = _identity()
    request_id = str(uuid4())

    def enqueue_stub(**kwargs):
        job = enqueue_job(
            jobs,
            workspace_id=kwargs["workspace_id"],
            owner_id=kwargs["owner_id"],
            operation=VISUAL_JOB_OPERATION,
            idempotency_key=kwargs["idempotency_key"],
            payload=_payload(identity),
            now=NOW,
        )
        return job, False

    with (
        patch.object(
            main_module,
            "_published_identity_evidence",
            return_value=({"status": "resolved", "identity": identity.public_payload()}, 2025, identity),
        ),
        patch.object(main_module, "enqueue_visual_investigation", side_effect=enqueue_stub),
    ):
        accepted = asyncio.run(
            main_module.bridge_visual_investigation(
                request=_request(
                    "/api/bridge-visual-investigation",
                    request_id=request_id,
                    idempotency_key="http-visual-1",
                ),
                query="find spalling",
                state_code=34,
                bridge_number="A-1",
                selected_sources=json.dumps(["inspection.pdf"]),
                defect_segmentation_mode="fast",
                analysis_mode="visual",
                conversation_id=None,
                data_year=2025,
                snapshot_revision_id="revision-1",
                idempotency_key="http-visual-1",
            )
        )
    assert accepted.status_code == 202
    body = json.loads(accepted.body)
    assert body["execution_mode"] == "durable_two_phase"
    job_id = body["job"]["job_id"]
    assert accepted.headers["location"].endswith(job_id)

    status = main_module.visual_investigation_job_status(
        job_id, _request("/status", method="GET")
    )
    assert status["job"]["status"] == "queued"
    cancelled = main_module.cancel_visual_investigation_job(
        job_id, _request("/cancel")
    )
    assert cancelled["job"]["status"] == "cancelled"
    assert cancelled["provider_interruption"] == "application_boundaries_only"
    with pytest.raises(HTTPException) as hidden:
        main_module.visual_investigation_job_status(
            job_id, _request("/status", method="GET", owner="other@example.com")
        )
    assert hidden.value.status_code == 404


def test_dedicated_visual_http_never_falls_back_to_request_owned_path(monkeypatch):
    monkeypatch.setenv("BRIDGEGUARD_DEPLOYMENT_MODE", "dedicated_customer")
    identity = _identity()
    with (
        patch.object(
            main_module,
            "_published_identity_evidence",
            return_value=({"status": "resolved"}, 2025, identity),
        ),
        patch.object(
            main_module,
            "process_bridge_visual_investigation",
            new=AsyncMock(),
        ) as legacy,
    ):
        with pytest.raises(HTTPException) as required:
            asyncio.run(
                main_module.bridge_visual_investigation(
                    request=_request("/api/bridge-visual-investigation"),
                    query="find spalling",
                    state_code=34,
                    bridge_number="A-1",
                    selected_sources=None,
                    defect_segmentation_mode="fast",
                    analysis_mode="visual",
                    conversation_id=None,
                    data_year=2025,
                    snapshot_revision_id="revision-1",
                    idempotency_key=None,
                )
            )
    assert required.value.status_code == 428
    legacy.assert_not_awaited()


def test_bridge_detail_visual_frontend_uses_durable_enqueue_poll_result_and_cancel():
    source = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "components"
        / "BridgeDetailModal.tsx"
    ).read_text(encoding="utf-8")
    assert "'Idempotency-Key': requestId" in source
    assert "/api/bridge-visual-investigation/jobs/${encodeURIComponent(visualJobId)}" in source
    assert "/result`" in source
    assert "/cancel`" in source
    assert "activeVisualJobIdRef" in source
    assert "resultResponse.data?.request_id !== requestId" in source
