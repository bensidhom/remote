"""Viewer-safe contracts for the canonical published-snapshot selector."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import revision_api
from app.auth import AuthMiddleware, AuthUser, COOKIE_NAME, issue_session
from app.routers.services.database import ensure_schema
from app.services.conversion.revision_store import (
    CanonicalRevisionInput,
    PublishSnapshotRevisionRequest,
    StageCanonicalBatchRequest,
    ensure_workspace,
    get_workspace_state,
    initialize_revision_store,
    publish_snapshot_revision,
    stage_canonical_batch,
)


AUTH_ENV = {
    "BRIDGEGUARD_AUTH_DISABLED": "0",
    "BRIDGEGUARD_AUTH_MODE": "password",
    "BRIDGEGUARD_SESSION_SECRET": "published-list-secret-longer-than-thirty-two-characters",
    "BRIDGEGUARD_PASSWORD": "published-list-password",
    "BRIDGEGUARD_ALLOWED_EMAILS": "viewer@example.com",
    "BRIDGEGUARD_EDITOR_EMAILS": "editor@example.com",
    "BRIDGEGUARD_ADMIN_EMAILS": "admin@example.com",
    "BRIDGEGUARD_ALLOWED_DOMAINS": "",
    "BRIDGEGUARD_OWNER_EMAIL": "",
}


@dataclass
class ListingHarness:
    client: TestClient
    database_path: str

    def headers(self, role: str) -> dict[str, str]:
        token = issue_session(AuthUser(email=f"{role}@example.com", role=role))
        return {"Cookie": f"{COOKIE_NAME}={token}"}


def _document(bridge_number: str, name: str) -> dict:
    return {
        "BL01": 34,
        "BID01": bridge_number,
        "BID02": name,
        "Elements": [],
        "Features": [],
        "Inspections": [],
        "PostingEvaluations": [],
        "PostingStatuses": [],
        "SpanSets": [],
        "SubstructureSets": [],
        "Works": [],
    }


def _stage_and_publish(
    database_path: str,
    bridge_number: str,
    data_year: int,
    name: str,
) -> str:
    generation = get_workspace_state(
        database_path, revision_api.SHARED_WORKSPACE_ID
    ).data_generation
    staged = stage_canonical_batch(
        database_path,
        StageCanonicalBatchRequest(
            workspace_id=revision_api.SHARED_WORKSPACE_ID,
            profile_id="research_historical_import",
            actor_id="editor@example.com",
            request_id=f"stage-{bridge_number}-{data_year}",
            idempotency_key=f"stage-{bridge_number}-{data_year}",
            expected_data_generation=generation,
            atomic_mode="batch",
            revisions=(
                CanonicalRevisionInput(
                    workspace_id=revision_api.SHARED_WORKSPACE_ID,
                    state_code=34,
                    bridge_number=bridge_number,
                    data_year=data_year,
                    canonical_document=_document(bridge_number, name),
                    source_records=(
                        {
                            "source_artifact_id": f"artifact-{bridge_number}-{data_year}",
                            "source_record_id": f"record-{bridge_number}-{data_year}",
                            "record_kind": "native_snbi",
                            "record_pointer": "/0",
                            "payload": {"BID01": bridge_number},
                        },
                    ),
                    validation={
                        "profile_id": "research_historical_import",
                        "status": "passed",
                        "validation_owner": "server",
                        "official_validation": False,
                        "issues": [],
                    },
                ),
            ),
        ),
    ).revisions[0]
    published = publish_snapshot_revision(
        database_path,
        PublishSnapshotRevisionRequest(
            workspace_id=revision_api.SHARED_WORKSPACE_ID,
            revision_id=staged.revision_id,
            actor_id="editor@example.com",
            request_id=f"publish-{bridge_number}-{data_year}",
            reason="Reviewed selector fixture",
            expected_revision_id=None,
            expected_data_generation=generation,
        ),
    )
    return published.revision_id


@pytest.fixture
def listing_harness(test_workspace: Path, monkeypatch: pytest.MonkeyPatch):
    for key, value in AUTH_ENV.items():
        monkeypatch.setenv(key, value)
    database_path = str(test_workspace / "published-list.db")
    ensure_schema(database_path)
    initialize_revision_store(database_path, app_revision="published-list-contract")
    ensure_workspace(
        database_path,
        revision_api.SHARED_WORKSPACE_ID,
        "BridgeGuard research",
    )
    _stage_and_publish(database_path, "A-002", 2026, "Second exact bridge")
    _stage_and_publish(database_path, "A-001", 2026, "First exact bridge")
    _stage_and_publish(database_path, "OLD-1", 2025, "Prior inventory bridge")

    revision_api.configure_database_path(lambda: database_path)
    focused = FastAPI()
    focused.include_router(revision_api.router)
    focused.add_middleware(AuthMiddleware)
    try:
        with TestClient(focused) as client:
            yield ListingHarness(client=client, database_path=database_path)
    finally:
        revision_api.configure_database_path(None)


@pytest.mark.parametrize("role", ("viewer", "editor"))
def test_listing_is_role_readable_bounded_and_exact(listing_harness, role):
    first = listing_harness.client.get(
        "/api/revisions/snapshots",
        headers=listing_harness.headers(role),
        params={"state_code": 34, "data_year": 2026, "limit": 1},
    )

    assert first.status_code == 200, first.text
    body = first.json()
    assert body["workspace_id"] == revision_api.SHARED_WORKSPACE_ID
    assert body["data_generation"] == 3
    assert first.headers["x-bridgeguard-data-version"] == "3"
    assert first.headers["etag"].startswith('"')
    assert body["total_count"] == 2
    assert body["count"] == 1
    assert body["available_data_years"] == [2026, 2025]
    assert body["available_years_truncated"] is False
    assert body["next_cursor"]
    item = body["snapshots"][0]
    assert item["bridge_number"] == "A-001"
    assert item["bridge_name"] == "First exact bridge"
    assert item["revision_id"]
    assert item["content_hash"]
    assert item["validation_status"] == "passed"
    assert "canonical_document" not in item
    assert "published_by" not in first.text
    assert "created_by" not in first.text

    second = listing_harness.client.get(
        "/api/revisions/snapshots",
        headers=listing_harness.headers(role),
        params={
            "state_code": 34,
            "data_year": 2026,
            "limit": 1,
            "cursor": body["next_cursor"],
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["snapshots"][0]["bridge_number"] == "A-002"
    assert second.json()["next_cursor"] is None


def test_workspace_selection_cursor_tampering_and_limits_are_rejected(listing_harness):
    headers = listing_harness.headers("viewer")
    selected = listing_harness.client.get(
        "/api/revisions/snapshots?workspace_id=attacker", headers=headers
    )
    malformed = listing_harness.client.get(
        "/api/revisions/snapshots?cursor=not-a-cursor", headers=headers
    )
    oversized = listing_harness.client.get(
        "/api/revisions/snapshots?limit=501", headers=headers
    )

    assert selected.status_code == 422
    assert selected.json()["detail"]["code"] == "workspace_not_selectable"
    assert malformed.status_code == 422
    assert malformed.json()["detail"]["code"] == "invalid_snapshot_cursor"
    assert oversized.status_code == 422


def test_cursor_is_bound_to_the_publication_generation(listing_harness):
    headers = listing_harness.headers("viewer")
    first = listing_harness.client.get(
        "/api/revisions/snapshots",
        headers=headers,
        params={"state_code": 34, "data_year": 2026, "limit": 1},
    )
    cursor = first.json()["next_cursor"]
    _stage_and_publish(listing_harness.database_path, "A-003", 2026, "New bridge")

    stale = listing_harness.client.get(
        "/api/revisions/snapshots",
        headers=headers,
        params={
            "state_code": 34,
            "data_year": 2026,
            "limit": 1,
            "cursor": cursor,
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_snapshot_cursor"
    assert stale.headers["x-bridgeguard-data-version"] == "4"

