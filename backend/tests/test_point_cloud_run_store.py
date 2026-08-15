from __future__ import annotations

import sqlite3
import uuid
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from app.routers.services.database import ensure_schema
from app.services.conversion.canonical_json import canonical_json_text, canonical_sha256
from app.services.conversion.revision_store import ensure_workspace
from app.services.point_cloud.run_store import (
    AppendPointCloudEvidenceRequest,
    CreatePointCloudRunRequest,
    PointCloudComponentEvidence,
    PointCloudMeasurementEvidence,
    PointCloudRunConflictError,
    PointCloudRunInputError,
    PointCloudRunNotFoundError,
    PointCloudSourceEvidence,
    PointCloudVlmEvidence,
    ReviewPointCloudRunRequest,
    TransitionPointCloudRunRequest,
    append_point_cloud_run_evidence,
    create_point_cloud_run,
    get_point_cloud_run,
    get_point_cloud_run_evidence,
    get_latest_published_point_cloud_result,
    get_published_point_cloud_result,
    request_point_cloud_run_cancel,
    review_point_cloud_run,
    transition_point_cloud_run,
)


WORKSPACE_A = "njdot-point-cloud"
WORKSPACE_B = "other-workspace"
STATE = 34
BRIDGE = "0101150"
YEAR = 2026
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


@pytest.fixture
def run_db(test_workspace: Path) -> tuple[str, str]:
    path = str(test_workspace / "point-cloud-runs.db")
    ensure_schema(path)
    ensure_workspace(path, WORKSPACE_A, "NJDOT point-cloud research")
    ensure_workspace(path, WORKSPACE_B, "Other")
    revision_id = str(uuid.uuid4())
    publication_id = str(uuid.uuid4())
    document = {
        "BL01": STATE,
        "BID01": BRIDGE,
        "BID02": "Fixture bridge",
        "Features": [{"BF01": "H02", "BF02": "2", "BH12": 16.5, "BH13": 15.9}],
    }
    document_hash = canonical_sha256(document)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(
        "INSERT INTO snapshot_revisions "
        "(id,workspace_id,conversion_batch_id,source_record_id,state_code,bridge_number,data_year,revision_number,"
        "lifecycle_state,canonical_json,canonical_sha256,parent_revision_id,profile_id,rule_bundle_sha256,created_by,created_at) "
        "VALUES (?,?,NULL,NULL,?,?,?,1,'staged',?,?,NULL,'research_historical_import',?,?,?)",
        (revision_id, WORKSPACE_A, STATE, BRIDGE, YEAR, canonical_json_text(document), document_hash, SHA_B, "fixture", "2026-08-14T12:00:00.000Z"),
    )
    connection.execute(
        "INSERT INTO published_snapshots "
        "(id,workspace_id,state_code,bridge_number,data_year,snapshot_revision_id,published_by,published_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (publication_id, WORKSPACE_A, STATE, BRIDGE, YEAR, revision_id, "fixture", "2026-08-14T12:01:00.000Z"),
    )
    connection.commit()
    connection.close()
    return path, revision_id


def _source(*, receipt=None) -> PointCloudSourceEvidence:
    return PointCloudSourceEvidence(
        source_kind="point_cloud",
        source_id="attachment:cloud-001",
        sha256=SHA_A,
        byte_size=1234,
        receipt=receipt or {"format": "e57", "scan_ids": ["scan-1"], "units": "m"},
    )


def _create_request(revision_id: str, *, key="run-001", job_id=None, workspace=WORKSPACE_A):
    return CreatePointCloudRunRequest(
        workspace_id=workspace,
        actor_id="analyst@example.test",
        job_id=job_id or str(uuid.uuid4()),
        idempotency_key=key,
        state_code=STATE,
        bridge_number=BRIDGE,
        data_year=YEAR,
        snapshot_revision_id=revision_id,
        canonical_sha256=_revision_hash(),
        source=_source(),
        analysis_profile_sha256=SHA_C,
        knowledge_pack_sha256=SHA_D,
        report_source_set_sha256=None,
    )


def _revision_hash() -> str:
    return canonical_sha256(
        {
            "BL01": STATE,
            "BID01": BRIDGE,
            "BID02": "Fixture bridge",
            "Features": [{"BF01": "H02", "BF02": "2", "BH12": 16.5, "BH13": 15.9}],
        }
    )


def _start(path: str, revision_id: str, *, key="run-001"):
    record, _ = create_point_cloud_run(path, _create_request(revision_id, key=key))
    return transition_point_cloud_run(
        path,
        TransitionPointCloudRunRequest(WORKSPACE_A, record.run_id, "queued", "running", "verify_source", {"percent": 1}),
    )


def _succeed(path: str, record):
    return transition_point_cloud_run(
        path,
        TransitionPointCloudRunRequest(WORKSPACE_A, record.run_id, "running", "succeeded", "complete", {"percent": 100}, SHA_B),
    )


def test_schema_v11_contains_all_durable_point_cloud_tables(run_db):
    path, _ = run_db
    connection = sqlite3.connect(path)
    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'point_cloud_%'"
        )
    }
    connection.close()
    assert {
        "point_cloud_runs",
        "point_cloud_run_sources",
        "point_cloud_vlm_calls",
        "point_cloud_components",
        "point_cloud_measurement_candidates",
        "point_cloud_reviews",
        "point_cloud_retention_outbox",
    } <= names


def test_create_is_exact_revision_bound_and_idempotent(run_db):
    path, revision_id = run_db
    request = _create_request(revision_id)
    first, created = create_point_cloud_run(path, request)
    replay, replay_created = create_point_cloud_run(path, request)
    assert created is True
    assert replay_created is False
    assert first.run_id == replay.run_id
    assert first.status == "queued"
    assert first.source_count == 1
    assert first.canonical_sha256 == _revision_hash()
    assert first.etag.startswith('"') and first.etag.endswith('"')
    with pytest.raises(FrozenInstanceError):
        first.status = "failed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        first.progress["percent"] = 2  # type: ignore[index]


def test_idempotency_conflict_and_unpublished_revision_fail_closed(run_db):
    path, revision_id = run_db
    request = _create_request(revision_id)
    create_point_cloud_run(path, request)
    changed = replace(request, knowledge_pack_sha256=SHA_A)
    with pytest.raises(PointCloudRunConflictError, match="Idempotency") as conflict:
        create_point_cloud_run(path, changed)
    assert conflict.value.code == "point_cloud_idempotency_conflict"

    unpublished_id = str(uuid.uuid4())
    connection = sqlite3.connect(path)
    row = connection.execute("SELECT * FROM snapshot_revisions WHERE id=?", (revision_id,)).fetchone()
    columns = [item[1] for item in connection.execute("PRAGMA table_info(snapshot_revisions)")]
    values = dict(zip(columns, row))
    values["id"] = unpublished_id
    values["revision_number"] = 2
    placeholders = ",".join("?" for _ in columns)
    connection.execute(f"INSERT INTO snapshot_revisions ({','.join(columns)}) VALUES ({placeholders})", tuple(values[name] for name in columns))
    connection.commit()
    connection.close()
    with pytest.raises(PointCloudRunConflictError) as unpublished:
        create_point_cloud_run(path, _create_request(unpublished_id, key="unpublished"))
    assert unpublished.value.code == "published_snapshot_required"


def test_evidence_is_atomic_hash_only_and_content_bounded(run_db):
    path, revision_id = run_db
    run = _start(path, revision_id)
    request = AppendPointCloudEvidenceRequest(
        workspace_id=WORKSPACE_A,
        run_id=run.run_id,
        sources=(PointCloudSourceEvidence("report", "report:1", SHA_B, 42, {"citation_ids": ["c1"]}),),
        vlm_calls=(
            PointCloudVlmEvidence(
                {
                    "task": "semantic_review",
                    "provider": "cerebras",
                    "model": "vision-model",
                    "messages_sha256": SHA_A,
                    "response_schema_sha256": SHA_B,
                    "response_sha256": SHA_C,
                    "usage": {"input_tokens": 123, "output_tokens": 7},
                    "images": [{"sha256": SHA_D, "media_type": "image/png"}],
                }
            ),
        ),
        components=(PointCloudComponentEvidence("component-1", "deck", "deck", "retained", {"point_count": 100}),),
        measurements=(PointCloudMeasurementEvidence("measurement-1", "feature:H02:2", "BH13", "review_required", {"raw_geometry_ft": 15.92}),),
    )
    stored = append_point_cloud_run_evidence(path, request)
    assert (stored.source_count, stored.vlm_call_count, stored.component_count, stored.measurement_count) == (2, 1, 1, 1)
    replay = append_point_cloud_run_evidence(path, request)
    assert (replay.source_count, replay.vlm_call_count, replay.component_count, replay.measurement_count) == (2, 1, 1, 1)

    connection = sqlite3.connect(path)
    receipt = connection.execute("SELECT receipt_json FROM point_cloud_vlm_calls WHERE run_id=?", (run.run_id,)).fetchone()[0]
    connection.close()
    assert "response_sha256" in receipt
    assert "raw response" not in receipt
    public_evidence = get_point_cloud_run_evidence(path, WORKSPACE_A, run.run_id)
    assert public_evidence.sources[1]["source_id"] == "report:1"
    assert public_evidence.components[0]["evidence"]["point_count"] == 100
    assert public_evidence.measurements[0]["feature_id"] == "feature:H02:2"
    assert public_evidence.vlm_calls[0]["receipt"]["response_sha256"] == SHA_C
    with pytest.raises(TypeError):
        public_evidence.components[0]["final_class"] = "pier"  # type: ignore[index]

    bad = AppendPointCloudEvidenceRequest(
        WORKSPACE_A,
        run.run_id,
        sources=(PointCloudSourceEvidence("report", "report:2", SHA_C, 5, {"citation_ids": ["c2"]}),),
        components=(PointCloudComponentEvidence("component-2", "pier", "pier", "retained", {"local_path": "C:/secret"}),),
    )
    with pytest.raises(PointCloudRunInputError) as exc:
        append_point_cloud_run_evidence(path, bad)
    assert exc.value.code == "evidence_contains_private_content"
    after = get_point_cloud_run(path, WORKSPACE_A, run.run_id)
    assert after.source_count == 2
    assert after.component_count == 1

    changed = AppendPointCloudEvidenceRequest(
        WORKSPACE_A,
        run.run_id,
        components=(PointCloudComponentEvidence("component-1", "deck", "pier", "retained", {"point_count": 100}),),
    )
    with pytest.raises(PointCloudRunConflictError) as changed_error:
        append_point_cloud_run_evidence(path, changed)
    assert changed_error.value.code == "point_cloud_component_evidence_conflict"


def test_cancel_is_owner_scoped_and_late_success_is_rejected(run_db):
    path, revision_id = run_db
    run = _start(path, revision_id)
    with pytest.raises(PointCloudRunNotFoundError):
        request_point_cloud_run_cancel(path, WORKSPACE_A, run.run_id, "someone-else")
    cancelled = request_point_cloud_run_cancel(path, WORKSPACE_A, run.run_id, "analyst@example.test")
    assert cancelled.status == "cancelled"
    with pytest.raises(PointCloudRunConflictError):
        transition_point_cloud_run(
            path,
            TransitionPointCloudRunRequest(WORKSPACE_A, run.run_id, "cancelled", "succeeded", "late", {}, SHA_B),
        )


def test_success_requires_hash_and_terminal_run_rejects_new_evidence(run_db):
    path, revision_id = run_db
    run = _start(path, revision_id)
    with pytest.raises(PointCloudRunInputError) as exc:
        transition_point_cloud_run(path, TransitionPointCloudRunRequest(WORKSPACE_A, run.run_id, "running", "succeeded", "complete", {}))
    assert exc.value.code == "point_cloud_result_hash_required"
    completed = _succeed(path, run)
    assert completed.status == "succeeded"
    with pytest.raises(PointCloudRunConflictError):
        append_point_cloud_run_evidence(path, AppendPointCloudEvidenceRequest(WORKSPACE_A, run.run_id, components=(PointCloudComponentEvidence("late", "deck", "deck", "retained", {}),)))


def test_accept_is_optimistic_publishes_once_and_increments_generation(run_db):
    path, revision_id = run_db
    completed = _succeed(path, _start(path, revision_id))
    review_id = str(uuid.uuid4())
    request = ReviewPointCloudRunRequest(
        WORKSPACE_A,
        completed.run_id,
        review_id,
        "reviewer@example.test",
        "accept",
        "Geometry and feature binding reviewed",
        completed.etag,
        {"/measurements/0/status": "accepted"},
    )
    accepted = review_point_cloud_run(path, request)
    assert accepted.published is True
    assert accepted.data_generation == 1
    published = get_published_point_cloud_result(path, WORKSPACE_A, STATE, BRIDGE, YEAR, SHA_A, SHA_C)
    assert published.run_id == completed.run_id
    assert published.review_id == review_id
    latest = get_latest_published_point_cloud_result(
        path, WORKSPACE_A, STATE, BRIDGE, SHA_A, data_year=YEAR
    )
    assert latest == published
    with pytest.raises(PointCloudRunNotFoundError):
        get_latest_published_point_cloud_result(
            path, WORKSPACE_A, STATE, BRIDGE, SHA_B, data_year=YEAR
        )

    replay = review_point_cloud_run(path, request)
    assert replay.idempotent is True
    assert replay.data_generation == 1


def test_review_conflict_and_stale_snapshot_are_atomic(run_db):
    path, revision_id = run_db
    completed = _succeed(path, _start(path, revision_id))
    bad_etag = ReviewPointCloudRunRequest(WORKSPACE_A, completed.run_id, str(uuid.uuid4()), "reviewer", "reject", "Not enough coverage", '"stale"')
    with pytest.raises(PointCloudRunConflictError) as conflict:
        review_point_cloud_run(path, bad_etag)
    assert conflict.value.code == "point_cloud_review_etag_conflict"

    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM published_snapshots WHERE workspace_id=?", (WORKSPACE_A,))
    connection.commit()
    connection.close()
    accept = ReviewPointCloudRunRequest(WORKSPACE_A, completed.run_id, str(uuid.uuid4()), "reviewer", "accept", "Accept", completed.etag)
    with pytest.raises(PointCloudRunConflictError) as stale:
        review_point_cloud_run(path, accept)
    assert stale.value.code == "point_cloud_snapshot_stale"
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT COUNT(*) FROM point_cloud_reviews WHERE run_id=?", (completed.run_id,)).fetchone()[0] == 0
    connection.close()


def test_reject_is_append_only_but_does_not_publish_or_advance_generation(run_db):
    path, revision_id = run_db
    completed = _succeed(path, _start(path, revision_id))
    result = review_point_cloud_run(
        path,
        ReviewPointCloudRunRequest(WORKSPACE_A, completed.run_id, str(uuid.uuid4()), "reviewer", "reject", "Feature identity is ambiguous", completed.etag),
    )
    assert result.published is False
    assert result.data_generation == 0
    assert get_point_cloud_run(path, WORKSPACE_A, completed.run_id).review_count == 1
    with pytest.raises(PointCloudRunNotFoundError):
        get_published_point_cloud_result(path, WORKSPACE_A, STATE, BRIDGE, YEAR, SHA_A, SHA_C)


def test_workspace_boundary_is_non_enumerating(run_db):
    path, revision_id = run_db
    record, _ = create_point_cloud_run(path, _create_request(revision_id))
    with pytest.raises(PointCloudRunNotFoundError) as exc:
        get_point_cloud_run(path, WORKSPACE_B, record.run_id)
    assert exc.value.code == "point_cloud_run_not_found"
    with pytest.raises(PointCloudRunNotFoundError):
        request_point_cloud_run_cancel(path, WORKSPACE_B, record.run_id, "analyst@example.test")


def test_database_triggers_protect_identity_and_evidence(run_db):
    path, revision_id = run_db
    run = _start(path, revision_id)
    append_point_cloud_run_evidence(
        path,
        AppendPointCloudEvidenceRequest(WORKSPACE_A, run.run_id, components=(PointCloudComponentEvidence("c1", "deck", "deck", "retained", {}),)),
    )
    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE point_cloud_runs SET source_sha256=? WHERE id=?", (SHA_B, run.run_id))
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE point_cloud_components SET final_class='pier' WHERE run_id=?", (run.run_id,))
    connection.rollback()
    connection.close()
