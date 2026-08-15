"""Focused replay contract for revision publish and patch mutations."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import AuthMiddleware
from app import revision_api
from app.revision_api import configure_database_path, router
from app.routers.services.database import ensure_schema


def _document(name: str = "Replay bridge") -> dict:
    return {
        "BL01": 34,
        "BID01": "HTTP-IDEMPOTENCY-1",
        "BID02": name,
        "BL03": "00123",
        "BW01": 1977,
        "Elements": [],
        "Features": [],
        "Inspections": [],
        "PostingEvaluations": [],
        "PostingStatuses": [],
        "SpanSets": [],
        "SubstructureSets": [],
        "Works": [],
    }


@pytest.fixture
def revision_client(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("BRIDGEGUARD_AUTH_DISABLED", "1")
    database_path = str(test_workspace / "revision-replay.db")
    ensure_schema(database_path)
    configure_database_path(lambda: database_path)
    monkeypatch.setattr(
        revision_api,
        "_server_validation",
        lambda document, *, profile_id, data_year, source_records: {
            "profile_id": profile_id,
            "status": "passed",
            "validation_owner": "server",
            "official_validation": False,
            "official_validation_status": "unavailable",
            "issues": [],
        },
    )
    focused = FastAPI()
    focused.include_router(router)
    focused.add_middleware(AuthMiddleware)
    try:
        with TestClient(focused) as client:
            yield client
    finally:
        configure_database_path(None)


def _stage(client: TestClient):
    return client.post(
        "/api/revisions/stage",
        headers={"Idempotency-Key": "stage-replay", "X-Request-ID": "stage-1"},
        json={
            "profile_id": "research_historical_import",
            "atomic_mode": "batch",
            "expected_data_generation": 0,
            "data_year": 2026,
            "bridges": [
                {
                    "canonical_document": _document(),
                    "source_records": [],
                    "lineage": [],
                }
            ],
        },
    )


def test_publish_and_patch_keys_replay_exactly_and_reject_changed_payload(
    revision_client: TestClient,
):
    staged = _stage(revision_client)
    assert staged.status_code == 201, staged.text
    revision_id = staged.json()["revisions"][0]["revision_id"]

    publish_body = {
        "reason": "Approved replay fixture",
        "expected_data_generation": 0,
        "reviewed_research_warnings": False,
    }
    publish_headers = {
        "Idempotency-Key": "publish-replay",
        "If-Match": '"none"',
        "X-Request-ID": "publish-1",
    }
    published = revision_client.post(
        f"/api/revisions/{revision_id}/publish",
        headers=publish_headers,
        json=publish_body,
    )
    assert published.status_code == 200, published.text

    replay = revision_client.post(
        f"/api/revisions/{revision_id}/publish",
        headers={**publish_headers, "X-Request-ID": "publish-retry"},
        json=publish_body,
    )
    assert replay.status_code == 200
    assert replay.json() == published.json()
    assert replay.headers["etag"] == published.headers["etag"]

    conflict = revision_client.post(
        f"/api/revisions/{revision_id}/publish",
        headers=publish_headers,
        json={**publish_body, "reason": "Changed payload"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_payload_conflict"

    patch_body = {
        "patch": {"BID02": "Replay-safe edit"},
        "reason": "Reviewed edit",
        "expected_data_generation": 1,
    }
    patch_headers = {
        "Idempotency-Key": "patch-replay",
        "If-Match": published.headers["etag"],
        "X-Request-ID": "patch-1",
    }
    patched = revision_client.patch(
        "/api/revisions/snapshots/34/HTTP-IDEMPOTENCY-1/2026",
        headers=patch_headers,
        json=patch_body,
    )
    assert patched.status_code == 201, patched.text

    patch_replay = revision_client.patch(
        "/api/revisions/snapshots/34/HTTP-IDEMPOTENCY-1/2026",
        headers={**patch_headers, "X-Request-ID": "patch-retry"},
        json=patch_body,
    )
    assert patch_replay.status_code == 201
    assert patch_replay.json() == patched.json()
    assert patch_replay.headers["etag"] == patched.headers["etag"]

    patch_conflict = revision_client.patch(
        "/api/revisions/snapshots/34/HTTP-IDEMPOTENCY-1/2026",
        headers=patch_headers,
        json={**patch_body, "patch": {"BID02": "Different edit"}},
    )
    assert patch_conflict.status_code == 409
    assert patch_conflict.json()["detail"]["code"] == "idempotency_payload_conflict"
