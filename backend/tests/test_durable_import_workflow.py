from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import imports_api, revision_api
from app.auth import AuthMiddleware, AuthUser, COOKIE_NAME, issue_session
from app.routers.services.database import ensure_schema
from app.services.conversion.revision_store import (
    ensure_workspace,
    get_published_snapshot,
    initialize_revision_store,
)
from app.services.conversion.source_artifacts import SourceArtifactError
from app.services.operations.jobs import claim_next_job, complete_job, get_job


AUTH_ENV = {
    "BRIDGEGUARD_AUTH_DISABLED": "0",
    "BRIDGEGUARD_AUTH_MODE": "password",
    "BRIDGEGUARD_SESSION_SECRET": "durable-import-contract-secret-longer-than-thirty-two",
    "BRIDGEGUARD_PASSWORD": "durable-import-password",
    "BRIDGEGUARD_ALLOWED_EMAILS": "viewer@example.com,other@example.com",
    "BRIDGEGUARD_EDITOR_EMAILS": "editor@example.com,other@example.com",
    "BRIDGEGUARD_ADMIN_EMAILS": "admin@example.com",
    "BRIDGEGUARD_ALLOWED_DOMAINS": "",
    "BRIDGEGUARD_OWNER_EMAIL": "",
}


def _document(*, bridge: str = "IMPORT-0001", name: str = "Durable import"):
    return {
        "BL01": 34,
        "BID01": bridge,
        "BID02": name,
        "BL03": "00123",
        "BW01": 1977,
        "Elements": [],
        "Features": [
            {
                "BF01": "H01",
                "BF02": "1",
                "Routes": [{"BRT01": "C01", "BRT02": "0001", "BRT03": "N"}],
            }
        ],
        "Inspections": [],
        "PostingEvaluations": [],
        "PostingStatuses": [],
        "SpanSets": [],
        "SubstructureSets": [],
        "Works": [],
    }


@dataclass
class Harness:
    client: TestClient
    database: Path
    data_root: Path

    def headers(self, role: str, *, email: str | None = None, key: str = "import-key"):
        address = email or f"{role}@example.com"
        token = issue_session(AuthUser(address, role))
        return {"Cookie": f"{COOKIE_NAME}={token}", "Idempotency-Key": key}

    def create(self, raw: bytes, *, role: str = "editor", email: str | None = None, key="import-key"):
        return self.client.post(
            "/api/imports",
            headers=self.headers(role, email=email, key=key),
            data={
                "inventory_year": "2026",
                "state_code": "34",
                "agency_id": "NJDOT",
                "profile_id": "native_snbi",
            },
            files={"file": ("bridges.json", raw, "application/json")},
        )

    def run_worker(self, import_id: str):
        job = claim_next_job(
            self.database.parent / "operation_jobs.db",
            worker_id="test-import-worker",
            allowed_operations=(imports_api.IMPORT_PREFLIGHT_OPERATION,),
            lease_seconds=30,
        )
        assert job is not None and job.id == import_id
        result = imports_api.process_import_preflight(job)
        return complete_job(
            self.database.parent / "operation_jobs.db",
            job=job,
            worker_id="test-import-worker",
            result=result,
        )


@pytest.fixture
def harness(test_workspace: Path, monkeypatch: pytest.MonkeyPatch):
    for key, value in AUTH_ENV.items():
        monkeypatch.setenv(key, value)
    database = test_workspace / "imports.db"
    data_root = test_workspace / "data"
    data_root.mkdir()
    ensure_schema(str(database))
    initialize_revision_store(str(database), app_revision="durable-import-test")
    ensure_workspace(
        str(database), revision_api.SHARED_WORKSPACE_ID, "BridgeGuard research"
    )
    revision_api.configure_database_path(lambda: str(database))
    monkeypatch.setattr(revision_api, "DATA_DIR", data_root, raising=False)
    monkeypatch.setattr(imports_api, "MAX_IMPORT_BYTES", 1024 * 1024)
    monkeypatch.setattr(imports_api, "MAX_PREFLIGHT_BYTES", 2 * 1024 * 1024)
    app = FastAPI()
    app.include_router(imports_api.router)
    app.add_middleware(AuthMiddleware)
    try:
        with TestClient(app) as client:
            yield Harness(client, database, data_root)
    finally:
        revision_api.configure_database_path(None)


def _raw(document=None) -> bytes:
    return json.dumps(
        {
            "manifest": {
                "profile_id": "native_snbi",
                "inventory_year": 2026,
                "state_code": 34,
                "agency_id": "NJDOT",
            },
            "bridges": [document or _document()],
        },
        separators=(",", ":"),
    ).encode()


def test_upload_is_editor_gated_idempotent_and_does_not_write_revisions(harness: Harness):
    denied = harness.create(_raw(), role="viewer")
    assert denied.status_code == 403

    first = harness.create(_raw())
    assert first.status_code == 202
    payload = first.json()
    assert payload["status"] == "queued"
    assert payload["source"]["sha256"]
    assert "job_id" not in payload["source"]

    replay = harness.create(_raw())
    assert replay.status_code == 202
    assert replay.json()["import_id"] == payload["import_id"]
    assert replay.json()["idempotent_replay"] is True

    changed = harness.create(_raw(_document(name="changed")))
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "idempotency_payload_conflict"

    with closing(sqlite3.connect(harness.database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM snapshot_revisions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM conversion_batches").fetchone()[0] == 0


def test_restart_safe_preflight_is_owner_scoped_and_persisted_outside_job_json(harness: Harness):
    created = harness.create(_raw())
    import_id = created.json()["import_id"]
    completed = harness.run_worker(import_id)
    assert completed.status == "succeeded"
    assert completed.result and completed.result["preflight_digest"]
    assert "bridges" not in completed.result and "items" not in completed.result

    owner = harness.client.get(
        f"/api/imports/{import_id}", headers=harness.headers("editor")
    )
    assert owner.status_code == 200
    preflight = owner.json()["preflight"]
    assert preflight["valid"] is True
    assert preflight["data_generation"] == 0
    assert preflight["field_loss_check"]["status"] == "passed"
    assert preflight["field_loss_check"]["projection_schema_errors"] == []
    assert preflight["summary"]["total"] == 1
    assert preflight["bridges"][0]["logical_key"]["bridge_number"] == "IMPORT-0001"
    assert preflight["bridges"][0]["projected_action"] == "insert"
    assert preflight["bridges"][0]["diff"] == {
        "change_count": 0,
        "changes": [],
        "truncated": False,
    }

    hidden = harness.client.get(
        f"/api/imports/{import_id}",
        headers=harness.headers("editor", email="other@example.com"),
    )
    assert hidden.status_code == 404
    assert hidden.json()["detail"]["code"] == "job_not_found"

    # A fresh store instance can resolve the durable preflight after the worker
    # that produced it has gone away.
    persisted = get_job(
        harness.database.parent / "operation_jobs.db",
        workspace_id=revision_api.SHARED_WORKSPACE_ID,
        owner_id="editor@example.com",
        job_id=import_id,
    )
    loaded = imports_api._load_preflight(
        persisted, str(persisted.result["preflight_digest"])
    )
    assert loaded["import_id"] == import_id


def test_commit_rechecks_digest_generation_and_stages_without_publish(harness: Harness):
    created = harness.create(_raw())
    import_id = created.json()["import_id"]
    job = harness.run_worker(import_id)
    digest = str(job.result["preflight_digest"])

    bad = harness.client.post(
        f"/api/imports/{import_id}/commit",
        headers={
            **harness.headers("editor", key="commit-key"),
            "X-Request-ID": str(uuid4()),
        },
        json={
            "preflight_digest": "0" * 64,
            "expected_data_generation": 0,
            "action": "replace_snapshot",
            "validation_policy": "advisory",
            "atomic_mode": "batch",
        },
    )
    assert bad.status_code == 409
    assert bad.json()["detail"]["code"] == "preflight_digest_mismatch"

    stale = harness.client.post(
        f"/api/imports/{import_id}/commit",
        headers={
            **harness.headers("editor", key="commit-key"),
            "X-Request-ID": str(uuid4()),
        },
        json={
            "preflight_digest": digest,
            "expected_data_generation": 99,
            "action": "replace_snapshot",
            "validation_policy": "advisory",
            "atomic_mode": "batch",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_data_generation"

    request_id = str(uuid4())
    committed = harness.client.post(
        f"/api/imports/{import_id}/commit",
        headers={
            **harness.headers("editor", key="commit-key"),
            "X-Request-ID": request_id,
        },
        json={
            "preflight_digest": digest,
            "expected_data_generation": 0,
            "action": "replace_snapshot",
            "validation_policy": "advisory",
            "atomic_mode": "batch",
        },
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["status"] == "staged"
    assert committed.json()["published"] is False
    assert committed.json()["revisions"][0]["outcome"] == "created"
    assert get_published_snapshot(
        str(harness.database),
        revision_api.SHARED_WORKSPACE_ID,
        34,
        "IMPORT-0001",
        2026,
    ) is None

    replay = harness.client.post(
        f"/api/imports/{import_id}/commit",
        headers={
            **harness.headers("editor", key="commit-key"),
            "X-Request-ID": str(uuid4()),
        },
        json={
            "preflight_digest": digest,
            "expected_data_generation": 0,
            "action": "replace_snapshot",
            "validation_policy": "advisory",
            "atomic_mode": "batch",
        },
    )
    assert replay.status_code == 200
    assert replay.json()["batch_id"] == committed.json()["batch_id"]
    assert replay.json()["revisions"][0]["revision_id"] == committed.json()["revisions"][0]["revision_id"]

    changed_policy = harness.client.post(
        f"/api/imports/{import_id}/commit",
        headers={
            **harness.headers("editor", key="commit-key"),
            "X-Request-ID": str(uuid4()),
        },
        json={
            "preflight_digest": digest,
            "expected_data_generation": 0,
            "action": "replace_snapshot",
            "validation_policy": "advisory",
            "atomic_mode": "batch",
            "override_reason": "Changed after the original commit",
        },
    )
    assert changed_policy.status_code == 409
    assert changed_policy.json()["detail"]["code"] == "idempotency_payload_conflict"


def test_tampered_preflight_is_rejected_before_database_write(harness: Harness):
    created = harness.create(_raw())
    import_id = created.json()["import_id"]
    job = harness.run_worker(import_id)
    digest = str(job.result["preflight_digest"])
    object_path = (
        harness.data_root
        / "conversion_sources"
        / "import_preflights"
        / "objects"
        / digest[:2]
        / digest[2:4]
        / digest
    )
    object_path.write_bytes(b"tampered")

    response = harness.client.post(
        f"/api/imports/{import_id}/commit",
        headers={
            **harness.headers("editor", key="commit-key"),
            "X-Request-ID": str(uuid4()),
        },
        json={
            "preflight_digest": digest,
            "expected_data_generation": 0,
            "action": "replace_snapshot",
            "validation_policy": "advisory",
            "atomic_mode": "batch",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "preflight_artifact_unavailable"
    with closing(sqlite3.connect(harness.database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM snapshot_revisions").fetchone()[0] == 0


def test_tampered_source_cannot_be_preflighted(harness: Harness):
    created = harness.create(_raw(), key="tampered-source")
    payload = created.json()
    digest = payload["source"]["sha256"]
    object_path = (
        harness.data_root
        / "conversion_sources"
        / "objects"
        / digest[:2]
        / digest[2:4]
        / digest
    )
    object_path.write_bytes(b"tampered")
    job = claim_next_job(
        harness.database.parent / "operation_jobs.db",
        worker_id="source-tamper-worker",
        allowed_operations=(imports_api.IMPORT_PREFLIGHT_OPERATION,),
        lease_seconds=30,
    )
    assert job is not None
    with pytest.raises(SourceArtifactError) as caught:
        imports_api.process_import_preflight(job)
    assert caught.value.code == "io_failure"
    assert "tampered" not in str(caught.value).lower()
    with closing(sqlite3.connect(harness.database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM snapshot_revisions").fetchone()[0] == 0


def test_rollback_is_admin_only_owner_scoped_and_refuses_committed_import(harness: Harness):
    admin_created = harness.create(_raw(), role="admin", key="admin-import")
    import_id = admin_created.json()["import_id"]
    editor = harness.client.post(
        f"/api/imports/{import_id}/rollback",
        headers=harness.headers("editor", key="unused"),
    )
    assert editor.status_code == 403
    rolled_back = harness.client.post(
        f"/api/imports/{import_id}/rollback",
        headers=harness.headers("admin", key="unused"),
    )
    assert rolled_back.status_code == 200
    assert rolled_back.json()["status"] == "cancelled"
    assert rolled_back.json()["artifacts_retained"] is True

    created = harness.create(_raw(_document(bridge="IMPORT-0002")), key="second-import")
    second_id = created.json()["import_id"]
    job = harness.run_worker(second_id)
    digest = str(job.result["preflight_digest"])
    committed = harness.client.post(
        f"/api/imports/{second_id}/commit",
        headers={
            **harness.headers("editor", key="second-commit"),
            "X-Request-ID": str(uuid4()),
        },
        json={
            "preflight_digest": digest,
            "expected_data_generation": 0,
            "action": "replace_snapshot",
            "validation_policy": "advisory",
            "atomic_mode": "batch",
        },
    )
    assert committed.status_code == 200
    # Admin cannot enumerate another owner's import through rollback.
    hidden = harness.client.post(
        f"/api/imports/{second_id}/rollback",
        headers=harness.headers("admin", key="unused"),
    )
    assert hidden.status_code == 404


def test_create_only_is_enforced_inside_the_staging_transaction(harness: Harness):
    first = harness.create(_raw(_document(bridge="CREATE-ONLY")), key="create-first")
    first_job = harness.run_worker(first.json()["import_id"])
    first_commit = harness.client.post(
        f"/api/imports/{first.json()['import_id']}/commit",
        headers={
            **harness.headers("editor", key="create-first-commit"),
            "X-Request-ID": str(uuid4()),
        },
        json={
            "preflight_digest": first_job.result["preflight_digest"],
            "expected_data_generation": 0,
            "action": "create_only",
            "validation_policy": "advisory",
            "atomic_mode": "batch",
        },
    )
    assert first_commit.status_code == 200

    second = harness.create(
        _raw(_document(bridge="CREATE-ONLY", name="replacement")), key="create-second"
    )
    second_job = harness.run_worker(second.json()["import_id"])
    second_preflight = imports_api._load_preflight(
        second_job, str(second_job.result["preflight_digest"])
    )
    bridge_review = second_preflight["bridges"][0]
    assert bridge_review["projected_action"] == "new_revision"
    assert bridge_review["diff"]["change_count"] == 1
    assert bridge_review["diff"]["changes"] == [
        {"op": "replace", "path": "/BID02"}
    ]
    assert bridge_review["diff"]["truncated"] is False
    blocked = harness.client.post(
        f"/api/imports/{second.json()['import_id']}/commit",
        headers={
            **harness.headers("editor", key="create-second-commit"),
            "X-Request-ID": str(uuid4()),
        },
        json={
            "preflight_digest": second_job.result["preflight_digest"],
            "expected_data_generation": 0,
            "action": "create_only",
            "validation_policy": "advisory",
            "atomic_mode": "batch",
        },
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "snapshot_already_exists"
    with closing(sqlite3.connect(harness.database)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM snapshot_revisions WHERE bridge_number='CREATE-ONLY'"
        ).fetchone()[0] == 1


def test_commit_atomically_rejects_snapshot_changed_after_preflight(harness: Harness):
    created = harness.create(_raw(_document(bridge="STALE-PREFLIGHT")), key="stale-preflight")
    import_id = created.json()["import_id"]
    job = harness.run_worker(import_id)

    intervening = _document(bridge="STALE-PREFLIGHT", name="intervening revision")
    revision_api.stage_server_documents(
        database_path=str(harness.database),
        actor_id="other-editor@example.com",
        request_id="intervening-request",
        idempotency_key="intervening-key",
        expected_data_generation=0,
        profile_id="native_snbi",
        data_year=2026,
        items=[{"canonical_document": intervening, "data_year": 2026}],
    )

    response = harness.client.post(
        f"/api/imports/{import_id}/commit",
        headers={
            **harness.headers("editor", key="stale-preflight-commit"),
            "X-Request-ID": str(uuid4()),
        },
        json={
            "preflight_digest": job.result["preflight_digest"],
            "expected_data_generation": 0,
            "action": "replace_snapshot",
            "validation_policy": "advisory",
            "atomic_mode": "batch",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_snapshot_preflight"
    with closing(sqlite3.connect(harness.database)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM snapshot_revisions WHERE bridge_number='STALE-PREFLIGHT'"
        ).fetchone()[0] == 1


def test_duplicate_property_and_unknown_field_produce_non_committable_preflight(harness: Harness):
    duplicate_raw = b'{"bridges":[{"BL01":34,"BL01":34,"BID01":"DUP"}]}'
    created = harness.create(duplicate_raw, key="duplicate-json")
    job = harness.run_worker(created.json()["import_id"])
    persisted = imports_api._load_preflight(job, str(job.result["preflight_digest"]))
    assert persisted["valid"] is False
    assert persisted["bridges"][0]["issues"][0]["code"] == "duplicate_key"

    unknown = _document(bridge="UNKNOWN")
    unknown["mystery"] = "would be dropped"
    created = harness.create(_raw(unknown), key="unknown-field")
    job = harness.run_worker(created.json()["import_id"])
    persisted = imports_api._load_preflight(job, str(job.result["preflight_digest"]))
    assert persisted["valid"] is False
    assert any(
        issue["json_pointer"] == "/bridges/0/mystery"
        for issue in persisted["bridges"][0]["issues"]
    )

    unknown_manifest = json.dumps(
        {"manifest": {"mystery": True}, "bridges": [_document(bridge="MANIFEST")]}
    ).encode()
    created = harness.create(unknown_manifest, key="unknown-manifest")
    job = harness.run_worker(created.json()["import_id"])
    persisted = imports_api._load_preflight(job, str(job.result["preflight_digest"]))
    assert persisted["valid"] is False
    assert persisted["bridges"][0]["issues"][0]["json_pointer"] == "/manifest/mystery"


def test_converter_private_metadata_is_preserved_as_verified_evidence_not_canonical_data(
    harness: Harness,
):
    original = imports_api._source_store().ingest(
        b"original fixed-width NBI evidence",
        original_filename="original-nbi.txt",
    )
    document = _document(bridge="CONVERTER-EVIDENCE")
    document["_SourceData"] = {
        "artifact": original.to_public_dict(),
        "over_record": {
            "source_record_id": f"nbi:{original.sha256}:0",
            "source_order": 0,
            "raw_fields": {"8": "CONVERTER-EVIDENCE", "112": "Y"},
        },
        "eligibility": {
            "profile_id": "research_historical_import",
            "retained_by_profile": True,
            "has_federally_eligible_record": True,
        },
    }
    created = harness.create(_raw(document), key="converter-evidence")
    job = harness.run_worker(created.json()["import_id"])
    preflight = imports_api._load_preflight(
        job, str(job.result["preflight_digest"])
    )
    assert preflight["valid"] is True
    assert preflight["summary"]["eligible"] == 1
    assert preflight["bridges"][0]["eligibility"]["status"] == "eligible"
    assert len(preflight["artifact_receipts"]) == 2
    assert "_SourceData" not in preflight["items"][0]["canonical_document"]
    kinds = {
        receipt["artifact_kind"] for receipt in preflight["artifact_receipts"]
    }
    assert kinds == {"immutable_json_upload", "legacy_nbi_source"}
    assert {
        record["record_kind"] for record in preflight["items"][0]["source_records"]
    } == {"nbi_json_upload_record", "nbi_over_record"}


def test_projection_schema_gap_fails_closed_before_commit(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        imports_api,
        "_projection_schema_errors",
        lambda _cursor: ["projection column missing"],
    )
    created = harness.create(_raw(_document(bridge="PROJECTION-GAP")), key="projection-gap")
    job = harness.run_worker(created.json()["import_id"])
    preflight = imports_api._load_preflight(job, str(job.result["preflight_digest"]))
    assert preflight["valid"] is False
    assert preflight["field_loss_check"] == {
        "status": "failed",
        "mode": "canonical_and_projection_schema_coverage",
        "unknown_fields": 0,
        "projection_schema_errors": ["projection column missing"],
        "canonical_is_authoritative": True,
        "extension_documents_retained_canonical_only": 0,
    }
    response = harness.client.post(
        f"/api/imports/{created.json()['import_id']}/commit",
        headers={
            **harness.headers("editor", key="projection-gap-commit"),
            "X-Request-ID": str(uuid4()),
        },
        json={
            "preflight_digest": job.result["preflight_digest"],
            "expected_data_generation": 0,
            "action": "replace_snapshot",
            "validation_policy": "advisory",
            "atomic_mode": "batch",
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "preflight_failed"
