"""HTTP contract for deterministic, revision-scoped export downloads."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import revision_api
from app.auth import AuthMiddleware, AuthUser, COOKIE_NAME, issue_session
from app.routers.services.database import ensure_schema
from app.services.conversion.revision_store import (
    CanonicalRevisionInput,
    StageCanonicalBatchRequest,
    ensure_workspace,
    initialize_revision_store,
    stage_canonical_batch,
)
from app.services.conversion.source_artifacts import SourceArtifactStore


AUTH_ENV = {
    "BRIDGEGUARD_AUTH_DISABLED": "0",
    "BRIDGEGUARD_AUTH_MODE": "password",
    "BRIDGEGUARD_SESSION_SECRET": (
        "revision-export-api-contract-secret-longer-than-32-characters"
    ),
    "BRIDGEGUARD_PASSWORD": "revision-export-contract-password",
    "BRIDGEGUARD_ALLOWED_EMAILS": "viewer@example.com",
    "BRIDGEGUARD_EDITOR_EMAILS": "editor@example.com",
    "BRIDGEGUARD_ADMIN_EMAILS": "admin@example.com",
    "BRIDGEGUARD_ALLOWED_DOMAINS": "",
    "BRIDGEGUARD_OWNER_EMAIL": "",
}
EXPORTED_AT = "2026-08-13T16:45:00.000Z"
ORIGINAL_BYTES = (
    b"STATE_CODE_001,STRUCTURE_NUMBER_008,RECORD_TYPE_005A\r\n"
    b"342,EXPORT-HTTP-1,1\r\n"
)


@dataclass
class ExportHarness:
    client: TestClient
    revision_id: str
    database_path: str
    artifact_root: Path

    def headers(self, role: str) -> dict[str, str]:
        token = issue_session(
            AuthUser(email=f"{role}@example.com", role=role)
        )
        return {"Cookie": f"{COOKIE_NAME}={token}"}


def _body(export_kind: str = "canonical_profile") -> dict[str, str]:
    profile = {
        "canonical_profile": "research_snbi_export_v1",
        "restore_original": "exact_source_restore_v1",
        "legacy_projection": "legacy_nbi_review_v1",
        "human_report": "human_review_report_v1",
    }[export_kind]
    return {
        "export_kind": export_kind,
        "profile_id": profile,
        "agency_id": "NJDOT",
        "exported_at": EXPORTED_AT,
    }


@pytest.fixture
def export_harness(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    for key, value in AUTH_ENV.items():
        monkeypatch.setenv(key, value)

    database_path = str(test_workspace / "revision-export-http.db")
    artifact_root = test_workspace / "conversion_sources"
    ensure_schema(database_path)
    initialize_revision_store(database_path, app_revision="pr6-export-http")
    ensure_workspace(
        database_path,
        revision_api.SHARED_WORKSPACE_ID,
        "BridgeGuard research",
    )
    store = SourceArtifactStore(artifact_root)
    receipt = store.ingest(
        ORIGINAL_BYTES,
        original_filename="NJDOT_2026_NBI.csv",
    )
    staged = stage_canonical_batch(
        database_path,
        StageCanonicalBatchRequest(
            workspace_id=revision_api.SHARED_WORKSPACE_ID,
            profile_id="research_historical_import",
            actor_id="editor@example.com",
            request_id="stage-export-http",
            idempotency_key="stage-export-http",
            expected_data_generation=0,
            atomic_mode="batch",
            revisions=(
                CanonicalRevisionInput(
                    workspace_id=revision_api.SHARED_WORKSPACE_ID,
                    state_code=34,
                    bridge_number="EXPORT-HTTP-1",
                    data_year=2026,
                    canonical_document={
                        "BL01": 34,
                        "BID01": "EXPORT-HTTP-1",
                        "BID02": "HTTP export bridge",
                        "Elements": [],
                        "Features": [],
                        "Inspections": [],
                        "PostingEvaluations": [],
                        "PostingStatuses": [],
                        "SpanSets": [],
                        "SubstructureSets": [],
                        "Works": [],
                    },
                    source_records=(
                        {
                            "source_artifact_id": receipt.artifact_id,
                            "source_record_id": "export-http-source-row",
                            "record_kind": "nbi_over_record",
                            "record_pointer": "/records/0",
                            "payload": {
                                "STATE_CODE_001": "342",
                                "STRUCTURE_NUMBER_008": "EXPORT-HTTP-1",
                                "RECORD_TYPE_005A": "1",
                            },
                        },
                    ),
                    artifact_receipts=(
                        {
                            **receipt.to_public_dict(),
                            "media_type": "text/csv",
                            "artifact_kind": "legacy_nbi_source",
                        },
                    ),
                    validation={
                        "profile_id": "research_historical_import",
                        "status": "passed_with_warnings",
                        "validation_owner": "server",
                        "official_validation": False,
                        "issues": [],
                    },
                ),
            ),
        ),
    )

    revision_api.configure_database_path(lambda: database_path)
    monkeypatch.setattr(revision_api, "DATA_DIR", test_workspace, raising=False)
    focused = FastAPI()
    focused.include_router(revision_api.router)
    focused.add_middleware(AuthMiddleware)
    try:
        with TestClient(focused) as client:
            yield ExportHarness(
                client=client,
                revision_id=staged.revisions[0].revision_id,
                database_path=database_path,
                artifact_root=artifact_root,
            )
    finally:
        revision_api.configure_database_path(None)


@pytest.mark.parametrize("role", ("viewer", "editor"))
def test_viewer_and_editor_can_download_revision_export(export_harness, role):
    response = export_harness.client.post(
        f"/api/revisions/{export_harness.revision_id}/exports",
        headers=export_harness.headers(role),
        json=_body(),
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="bridgeguard-canonical_profile-exp-'
    )
    assert response.headers["x-bridgeguard-export-id"].startswith("exp-")
    assert response.headers["x-bridgeguard-data-version"] == "0"
    assert response.headers["x-content-type-options"] == "nosniff"

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == sorted(archive.namelist())
        assert len(archive.namelist()) == 2
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        manifest_name = next(name for name in archive.namelist() if name.endswith(".manifest.json"))
        manifest = json.loads(archive.read(manifest_name))
        assert manifest["workspace_id"] == revision_api.SHARED_WORKSPACE_ID
        assert manifest["actor_id"] == f"{role}@example.com"
        assert manifest["snapshot_revision_id"] == export_harness.revision_id
        assert manifest["export_id"] == response.headers["x-bridgeguard-export-id"]
        assert export_harness.database_path not in json.dumps(manifest)
        assert "source-artifact://" not in json.dumps(manifest)


def test_identical_export_request_produces_identical_zip_bytes(export_harness):
    arguments = {
        "headers": export_harness.headers("viewer"),
        "json": _body("restore_original"),
    }
    url = f"/api/revisions/{export_harness.revision_id}/exports"
    first = export_harness.client.post(url, **arguments)
    second = export_harness.client.post(url, **arguments)

    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    assert (
        first.headers["x-bridgeguard-export-id"]
        == second.headers["x-bridgeguard-export-id"]
    )
    with zipfile.ZipFile(io.BytesIO(first.content)) as archive:
        assert archive.read("NJDOT_2026_NBI.csv") == ORIGINAL_BYTES


@pytest.mark.parametrize("forbidden", ("workspace_id", "actor_id"))
def test_client_cannot_supply_workspace_or_actor(export_harness, forbidden):
    body = {**_body(), forbidden: "attacker-selected"}
    response = export_harness.client.post(
        f"/api/revisions/{export_harness.revision_id}/exports",
        headers=export_harness.headers("viewer"),
        json=body,
    )

    assert response.status_code == 422
    assert export_harness.database_path not in response.text
    assert str(export_harness.artifact_root) not in response.text


def test_export_errors_are_typed_and_path_free(export_harness):
    response = export_harness.client.post(
        "/api/revisions/absent-revision/exports",
        headers=export_harness.headers("viewer"),
        json=_body(),
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "revision_not_found"
    assert response.headers["x-bridgeguard-data-version"] == "0"
    assert export_harness.database_path not in response.text
    assert str(export_harness.artifact_root) not in response.text
    assert "source-artifact://" not in response.text


def test_export_query_parameters_are_rejected(export_harness):
    response = export_harness.client.post(
        f"/api/revisions/{export_harness.revision_id}/exports?workspace_id=other",
        headers=export_harness.headers("viewer"),
        json=_body(),
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "export_parameters_not_selectable"
