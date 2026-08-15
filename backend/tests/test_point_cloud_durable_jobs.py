from __future__ import annotations

import base64
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import point_cloud_jobs
from app.routers import point_cloud_api
from app.routers.services.database import ensure_schema
from app.services.bridge_identity import BridgeSnapshotIdentity
from app.services.conversion.canonical_json import canonical_json_text, canonical_sha256
from app.services.conversion.revision_store import ensure_workspace
from app.services.operations.jobs import JobStoreError, claim_next_job, complete_job
from app.services.point_cloud.run_store import get_point_cloud_run_by_job


WORKSPACE = "shared-research-workspace"
STATE = 34
BRIDGE = "0101150"
YEAR = 2026
SOURCE_SHA = "a" * 64
PACK_SHA = "b" * 64
MANIFEST_SHA = "c" * 64


class FakePack:
    def __init__(self, identity):
        self.sha256 = PACK_SHA
        self.canonical_json = canonical_json_text(
            {
                "schema_version": "point-cloud-knowledge-pack-v1",
                "scope": {"bridge": identity.public_payload()},
                "untrusted_report_evidence": {
                    "source_set_sha256": "d" * 64,
                    "sources": [{"report_id": "r1", "sha256": "e" * 64, "byte_size": 55}],
                    "chunks": [],
                },
            }
        )

    @property
    def payload(self):
        import json

        return json.loads(self.canonical_json)

    def provider_context(self):
        return {
            "schema_version": "point-cloud-knowledge-pack-v1",
            "knowledge_pack_sha256": self.sha256,
            "trusted_structured_context": {},
            "untrusted_report_evidence": self.payload["untrusted_report_evidence"],
        }


class FakeApi:
    def __init__(self, identity, document):
        self.identity = identity
        self.document = document
        self.row = (7, "not-exposed.e57", "cloud.e57", 1234, "now", SOURCE_SHA)
        self.analyze_calls = 0
        self.save_calls = 0
        self.source_changed = False

    def _raw_cloud_row(self, state_code, bridge_number):
        digest = "f" * 64 if self.source_changed else SOURCE_SHA
        return (7, "not-exposed.e57", "cloud.e57", 1234, "now", digest)

    def _point_cloud_source_evidence(self, row):
        return {"attachment_id": 7, "sha256": str(row[5]), "byte_size": 1234, "format": "e57", "receipt_sha256": "9" * 64}

    def _load_source_receipt(self, row):
        return {"format": "receipt-v1", "source_format": "e57", "metric_processing_authorized": True, "receipt_sha256": "9" * 64}

    def _public_source_receipt(self, receipt):
        return dict(receipt)

    def _build_exact_bridge_knowledge_pack(self, **kwargs):
        return SimpleNamespace(identity=self.identity, canonical_document=self.document), FakePack(self.identity)

    def _canonical_under_feature_kind(self, document):
        return "highway"

    async def _analysis_points(self, *args, **kwargs):
        return np.asarray([[0.0, 0.0, 0.0], [0.0, 0.0, 5.0]], dtype=np.float64)

    def analyze_bridge(self, points, *args):
        self.analyze_calls += 1
        return {
            "status": "success",
            "labels_b64u8": base64.b64encode(bytes([1, 1])).decode("ascii"),
            "classes": [{"id": 1, "name": "Deck", "color_rgb": [1, 0, 0]}],
            "components": [
                {
                    "id": 1,
                    "proposed_class": "deck_slab",
                    "final_class": "deck_slab",
                    "fusion_disposition": "retained_reviewed",
                    "point_count": 2,
                }
            ],
            "vlm_call_receipts": [
                {
                    "task": "semantic_review",
                    "provider": "cerebras",
                    "model": "vision-model",
                    "messages_sha256": "1" * 64,
                    "response_schema_sha256": "2" * 64,
                    "response_sha256": "3" * 64,
                    "usage": {"input_tokens": 5, "output_tokens": 2},
                }
            ],
            "clearance": {
                "feature_measurement": {
                    "status": "review_required",
                    "reason": "lane_shoulder_and_station_review_required",
                    "feature_binding_version": "feature-v1",
                    "clearance_profile_version": "clearance-v1",
                    "bindings": [{"detected_id": "h02-1", "feature_id": "feature:H02:2", "status": "bound"}],
                }
            },
        }

    def _save_processed(self, *args, **kwargs):
        self.save_calls += 1
        assert kwargs["publish_pointer"] is False
        return {
            "source_generation": "pcv2-7-" + "a" * 32 + "-" + "b" * 12,
            "manifest_sha256": MANIFEST_SHA,
            "artifact_sha256": {"npz": "4" * 64, "ply": "5" * 64},
            "published_to_legacy_pointer": False,
        }


@pytest.fixture
def durable_fixture(test_workspace: Path, monkeypatch):
    database = str(test_workspace / "durable-point-cloud.db")
    jobs = str(test_workspace / "operation-jobs.db")
    ensure_schema(database)
    ensure_workspace(database, WORKSPACE, "Shared research")
    revision_id = str(uuid.uuid4())
    document = {
        "BL01": STATE,
        "BID01": BRIDGE,
        "BID02": "Atlantic County bridge",
        "Features": [{"BF01": "H02", "BF02": "B"}],
    }
    digest = canonical_sha256(document)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(
        "INSERT INTO snapshot_revisions "
        "(id,workspace_id,conversion_batch_id,source_record_id,state_code,bridge_number,data_year,revision_number,lifecycle_state,canonical_json,canonical_sha256,parent_revision_id,profile_id,rule_bundle_sha256,created_by,created_at) "
        "VALUES (?,?,NULL,NULL,?,?,?,1,'staged',?,?,NULL,'research',?,'fixture','2026-08-14T12:00:00Z')",
        (revision_id, WORKSPACE, STATE, BRIDGE, YEAR, canonical_json_text(document), digest, "8" * 64),
    )
    connection.execute(
        "INSERT INTO published_snapshots (id,workspace_id,state_code,bridge_number,data_year,snapshot_revision_id,published_by,published_at) VALUES (?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), WORKSPACE, STATE, BRIDGE, YEAR, revision_id, "fixture", "2026-08-14T12:01:00Z"),
    )
    connection.commit()
    connection.close()
    identity = BridgeSnapshotIdentity(
        workspace_id=WORKSPACE,
        state_code=STATE,
        bridge_number=BRIDGE,
        data_year=YEAR,
        snapshot_revision_id=revision_id,
        revision_number=1,
        canonical_sha256=digest,
        profile_id="research",
        rule_bundle_sha256="8" * 64,
        published_at="2026-08-14T12:01:00Z",
        data_generation=0,
        schema_generation=11,
    )
    fake = FakeApi(identity, document)
    monkeypatch.setattr(point_cloud_jobs, "_api", lambda: fake)
    monkeypatch.setattr(point_cloud_api, "USER_DB", database)
    monkeypatch.setattr(point_cloud_api, "POINT_CLOUD_WORKSPACE_ID", WORKSPACE)
    monkeypatch.setattr(point_cloud_api, "_point_cloud_job_db", lambda: Path(jobs))
    return database, jobs, fake


def _enqueue(database, jobs, *, key="job-1"):
    return point_cloud_jobs.enqueue_point_cloud_analysis(
        database_path=database,
        job_database_path=jobs,
        workspace_id=WORKSPACE,
        owner_id="analyst@example.test",
        idempotency_key=key,
        state_code=str(STATE),
        bridge_number=BRIDGE,
        data_year=YEAR,
    )


def _client(database: str) -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.user = SimpleNamespace(
            email=request.headers.get("X-Test-User", "analyst@example.test"),
            role="editor",
        )
        return await call_next(request)

    app.include_router(point_cloud_api.router)
    return TestClient(app)


def test_enqueue_freezes_scope_without_running_geometry(durable_fixture):
    database, jobs, fake = durable_fixture
    job, run, replay = _enqueue(database, jobs)
    assert replay is False
    assert fake.analyze_calls == 0
    assert run.job_id == job.id
    assert run.status == "queued"
    assert run.snapshot_revision_id == fake.identity.snapshot_revision_id
    assert run.knowledge_pack_sha256 == PACK_SHA
    assert job.payload["bridge_snapshot"]["canonical_sha256"] == fake.identity.canonical_sha256


def test_enqueue_is_idempotent_and_changed_payload_conflicts(durable_fixture):
    database, jobs, _ = durable_fixture
    first_job, first_run, _ = _enqueue(database, jobs)
    replay_job, replay_run, replay = _enqueue(database, jobs)
    assert replay is True
    assert (replay_job.id, replay_run.run_id) == (first_job.id, first_run.run_id)
    with pytest.raises(Exception) as exc:
        point_cloud_jobs.enqueue_point_cloud_analysis(
            database_path=database,
            job_database_path=jobs,
            workspace_id=WORKSPACE,
            owner_id="analyst@example.test",
            idempotency_key="job-1",
            state_code=str(STATE),
            bridge_number=BRIDGE,
            data_year=YEAR,
            model_mode="structural",
        )
    assert getattr(exc.value, "code", "") == "idempotency_payload_conflict"


def test_worker_persists_candidate_and_evidence_but_does_not_publish(durable_fixture):
    database, jobs, fake = durable_fixture
    queued, run, _ = _enqueue(database, jobs)
    claimed = claim_next_job(jobs, worker_id="worker-1", allowed_operations=(point_cloud_jobs.POINT_CLOUD_JOB_OPERATION,), lease_seconds=60)
    assert claimed is not None
    result = point_cloud_jobs.process_point_cloud_analysis_job(job=claimed, database_path=database, job_database_path=jobs)
    complete_job(jobs, job=claimed, worker_id="worker-1", result=result)
    stored = get_point_cloud_run_by_job(database, WORKSPACE, queued.id)
    assert stored.status == "succeeded"
    assert stored.result_sha256 == MANIFEST_SHA
    assert stored.vlm_call_count == 1
    assert stored.component_count == 1
    assert stored.measurement_count == 1
    assert stored.source_count == 2
    assert stored.published_review_id is None
    assert fake.analyze_calls == 1 and fake.save_calls == 1


def test_worker_exact_retry_returns_terminal_receipt_without_second_model_call(durable_fixture):
    database, jobs, fake = durable_fixture
    _, _, _ = _enqueue(database, jobs)
    claimed = claim_next_job(jobs, worker_id="worker-1", allowed_operations=(point_cloud_jobs.POINT_CLOUD_JOB_OPERATION,), lease_seconds=60)
    first = point_cloud_jobs.process_point_cloud_analysis_job(job=claimed, database_path=database, job_database_path=jobs)
    second = point_cloud_jobs.process_point_cloud_analysis_job(job=claimed, database_path=database, job_database_path=jobs)
    assert first["result_sha256"] == second["result_sha256"] == MANIFEST_SHA
    assert second["idempotent"] is True
    assert fake.analyze_calls == 1


def test_changed_source_fails_before_geometry_or_vlm(durable_fixture):
    database, jobs, fake = durable_fixture
    _enqueue(database, jobs)
    claimed = claim_next_job(jobs, worker_id="worker-1", allowed_operations=(point_cloud_jobs.POINT_CLOUD_JOB_OPERATION,), lease_seconds=60)
    fake.source_changed = True
    with pytest.raises(point_cloud_jobs.PointCloudJobError) as exc:
        point_cloud_jobs.process_point_cloud_analysis_job(job=claimed, database_path=database, job_database_path=jobs)
    assert exc.value.code == "point_cloud_source_changed"
    assert fake.analyze_calls == 0


def test_cancel_is_durable_owner_scoped_and_late_worker_cannot_succeed(durable_fixture):
    database, jobs, fake = durable_fixture
    job, run, _ = _enqueue(database, jobs)
    with pytest.raises(JobStoreError):
        point_cloud_jobs.cancel_point_cloud_job(
            database_path=database,
            job_database_path=jobs,
            workspace_id=WORKSPACE,
            owner_id="other@example.test",
            job_id=job.id,
        )
    cancelled_job = point_cloud_jobs.cancel_point_cloud_job(
        database_path=database,
        job_database_path=jobs,
        workspace_id=WORKSPACE,
        owner_id="analyst@example.test",
        job_id=job.id,
    )
    assert cancelled_job.status == "cancelled"
    assert get_point_cloud_run_by_job(database, WORKSPACE, job.id).status == "cancelled"
    assert fake.analyze_calls == 0


def test_http_enqueue_status_and_cancel_are_path_free_and_owner_scoped(durable_fixture):
    database, _jobs, fake = durable_fixture
    client = _client(database)
    missing = client.post(
        f"/api/bridges/{STATE}/{BRIDGE}/point-cloud/runs",
        json={"data_year": YEAR, "model_mode": "clearance", "use_vlm": True},
    )
    assert missing.status_code == 422
    response = client.post(
        f"/api/bridges/{STATE}/{BRIDGE}/point-cloud/runs",
        headers={"Idempotency-Key": "http-run-1"},
        json={"data_year": YEAR, "model_mode": "clearance", "use_vlm": True},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert fake.analyze_calls == 0
    assert "ETag" in response.headers
    serialized = response.text.lower()
    assert "not-exposed.e57" not in serialized
    assert "filepath" not in serialized and "local_path" not in serialized

    status = client.get(f"/api/point-cloud/runs/{body['run_id']}")
    assert status.status_code == 200
    evidence = client.get(f"/api/point-cloud/runs/{body['run_id']}/evidence")
    assert evidence.status_code == 200
    assert evidence.json()["context"]["snapshot_revision_id"] == fake.identity.snapshot_revision_id
    assert "not-exposed.e57" not in evidence.text
    hidden = client.get(
        f"/api/point-cloud/runs/{body['run_id']}",
        headers={"X-Test-User": "other@example.test"},
    )
    assert hidden.status_code == 404
    cancelled = client.post(f"/api/point-cloud/runs/{body['run_id']}/cancel")
    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == "cancelled"


def test_http_review_requires_optimistic_and_idempotency_headers(durable_fixture):
    database, _jobs, _fake = durable_fixture
    client = _client(database)
    created = client.post(
        f"/api/bridges/{STATE}/{BRIDGE}/point-cloud/runs",
        headers={"Idempotency-Key": "http-review-run"},
        json={"data_year": YEAR},
    ).json()
    missing = client.post(
        f"/api/point-cloud/runs/{created['run_id']}/review",
        json={"decision": "reject", "reason": "Insufficient scan coverage", "corrections": {}},
    )
    assert missing.status_code == 428
    missing_key = client.post(
        f"/api/point-cloud/runs/{created['run_id']}/review",
        headers={"If-Match": created["etag"]},
        json={"decision": "reject", "reason": "Insufficient scan coverage", "corrections": {}},
    )
    assert missing_key.status_code == 422


def test_http_accept_rechecks_frozen_source_and_context_before_publication(durable_fixture):
    database, jobs, fake = durable_fixture
    queued, _run, _ = _enqueue(database, jobs, key="http-stale-review")
    claimed = claim_next_job(
        jobs,
        worker_id="worker-review",
        allowed_operations=(point_cloud_jobs.POINT_CLOUD_JOB_OPERATION,),
        lease_seconds=60,
    )
    result = point_cloud_jobs.process_point_cloud_analysis_job(
        job=claimed,
        database_path=database,
        job_database_path=jobs,
    )
    complete_job(jobs, job=claimed, worker_id="worker-review", result=result)
    completed = get_point_cloud_run_by_job(database, WORKSPACE, queued.id)
    fake.source_changed = True

    response = _client(database).post(
        f"/api/point-cloud/runs/{completed.run_id}/review",
        headers={
            "If-Match": completed.etag,
            "Idempotency-Key": "stale-review-attempt",
        },
        json={
            "decision": "accept",
            "reason": "Reviewed geometry and feature evidence",
            "corrections": {},
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "point_cloud_source_changed"
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM point_cloud_reviews WHERE run_id=?",
            (completed.run_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM published_point_cloud_results WHERE run_id=?",
            (completed.run_id,),
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_commercial_acceptance_requires_current_scientific_release_receipt(
    durable_fixture, monkeypatch
):
    database, jobs, _fake = durable_fixture
    queued, _run, _ = _enqueue(database, jobs, key="http-evaluation-gate")
    claimed = claim_next_job(
        jobs,
        worker_id="worker-evaluation",
        allowed_operations=(point_cloud_jobs.POINT_CLOUD_JOB_OPERATION,),
        lease_seconds=60,
    )
    result = point_cloud_jobs.process_point_cloud_analysis_job(
        job=claimed,
        database_path=database,
        job_database_path=jobs,
    )
    complete_job(jobs, job=claimed, worker_id="worker-evaluation", result=result)
    completed = get_point_cloud_run_by_job(database, WORKSPACE, queued.id)
    monkeypatch.setenv("BRIDGEGUARD_POINT_CLOUD_REQUIRE_EVALUATION", "1")
    monkeypatch.delenv("BRIDGEGUARD_POINT_CLOUD_EVALUATION_RECEIPT", raising=False)

    response = _client(database).post(
        f"/api/point-cloud/runs/{completed.run_id}/review",
        headers={
            "If-Match": completed.etag,
            "Idempotency-Key": "evaluation-gate-attempt",
        },
        json={
            "decision": "accept",
            "reason": "Reviewed geometry and feature evidence",
            "corrections": {},
        },
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "point_cloud_evaluation_receipt_required"
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM point_cloud_reviews WHERE run_id=?",
            (completed.run_id,),
        ).fetchone()[0] == 0
    finally:
        connection.close()
