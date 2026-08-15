"""HTTP contracts for server-owned revision validation and workspace state.

These tests freeze two PR5 trust boundaries that are intentionally separate
from the revision-store unit contract:

* an HTTP client may submit canonical data, but may never submit or override
  the validation verdict attached to an immutable revision; and
* optimistic-concurrency generations come from the server-owned shared
  workspace, not from a browser-selected workspace.

The focused ASGI app uses the real revision router and authorization middleware
against an isolated SQLite database.  Critical/warning reports are injected at
the server validation seam only; they are never accepted from request JSON.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main, revision_api
from app.auth import AuthMiddleware, AuthUser, COOKIE_NAME, issue_session
from app.routers.services.database import ensure_schema
from app.routers.services.db_schema import LATEST_SCHEMA_VERSION
from app.services.conversion.revision_store import (
    ensure_workspace,
    initialize_revision_store,
)


SHARED_WORKSPACE = "shared-research-workspace"
STATE = 34
BRIDGE = "SERVER-VALIDATION-0001"
YEAR = 2026

AUTH_ENV = {
    "BRIDGEGUARD_AUTH_DISABLED": "0",
    "BRIDGEGUARD_AUTH_MODE": "password",
    "BRIDGEGUARD_SESSION_SECRET": (
        "revision-server-validation-contract-secret-longer-than-32-characters"
    ),
    "BRIDGEGUARD_PASSWORD": "revision-validation-contract-password",
    "BRIDGEGUARD_ALLOWED_EMAILS": "viewer@example.com",
    "BRIDGEGUARD_EDITOR_EMAILS": "editor@example.com",
    "BRIDGEGUARD_ADMIN_EMAILS": "admin@example.com",
    "BRIDGEGUARD_ALLOWED_DOMAINS": "",
    "BRIDGEGUARD_OWNER_EMAIL": "",
}


@dataclass
class Harness:
    client: TestClient
    database_path: str

    def headers(self, role: str, **extra: str) -> dict[str, str]:
        email = f"{role}@example.com"
        token = issue_session(AuthUser(email=email, role=role))
        return {"Cookie": f"{COOKIE_NAME}={token}", **extra}


def _app() -> FastAPI:
    focused = FastAPI()
    focused.include_router(main.revision_router)
    focused.add_middleware(AuthMiddleware)
    return focused


@pytest.fixture
def harness(test_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    for key, value in AUTH_ENV.items():
        monkeypatch.setenv(key, value)

    database_path = str(test_workspace / "server-validation-http.db")
    ensure_schema(database_path)
    initialize_revision_store(database_path, app_revision="pr5-validation-contract")
    ensure_workspace(database_path, SHARED_WORKSPACE, "BridgeGuard research")
    monkeypatch.setattr(main, "USER_DB", database_path)

    with TestClient(_app()) as client:
        yield Harness(client=client, database_path=database_path)


def _document() -> dict[str, Any]:
    return {
        "BL01": STATE,
        "BID01": BRIDGE,
        "BID02": "Server-owned validation fixture",
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


def _stage_body() -> dict[str, Any]:
    return {
        "profile_id": "research_historical_import",
        "atomic_mode": "batch",
        "expected_data_generation": 0,
        "data_year": YEAR,
        "bridges": [
            {
                "canonical_document": _document(),
                "source_records": [],
                "lineage": [],
            }
        ],
    }


def _stage(harness: Harness, *, key: str, body: dict[str, Any] | None = None):
    return harness.client.post(
        "/api/revisions/stage",
        headers=harness.headers(
            "editor",
            **{
                "Idempotency-Key": key,
                "X-Request-ID": f"request-{key}",
            },
        ),
        json=body or _stage_body(),
    )


def _publish(
    harness: Harness,
    revision_id: str,
    *,
    key: str,
    reviewed: bool,
):
    return harness.client.post(
        f"/api/revisions/{revision_id}/publish",
        headers=harness.headers(
            "editor",
            **{
                "Idempotency-Key": key,
                "If-Match": '"none"',
                "X-Request-ID": f"request-{key}",
            },
        ),
        json={
            "reason": "Server validation contract review",
            "expected_data_generation": 0,
            "reviewed_research_warnings": reviewed,
        },
    )


def _server_report(severity: str) -> dict[str, Any]:
    failed = severity in {"critical", "error"}
    return {
        "profile_id": "research_historical_import",
        "status": "failed" if failed else "passed_with_warnings",
        "validation_owner": "server",
        "validation_label": "BridgeGuard validation",
        "official_validation": False,
        "official_validation_status": "unavailable",
        "issues": [
            {
                "rule_id": f"BG-SERVER-{severity.upper()}",
                "severity": severity,
                "json_pointer": "/BID02",
                "message": f"Server-derived {severity} fixture",
            }
        ],
    }


def _stored_validation(database_path: str, revision_id: str) -> dict[str, Any]:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        run = connection.execute(
            "SELECT id, profile_id, status, summary_json FROM validation_runs "
            "WHERE workspace_id=? AND snapshot_revision_id=?",
            (SHARED_WORKSPACE, revision_id),
        ).fetchone()
        assert run is not None
        issues = connection.execute(
            "SELECT severity, code, json_pointer, message FROM validation_issues "
            "WHERE workspace_id=? AND validation_run_id=? ORDER BY issue_ordinal",
            (SHARED_WORKSPACE, run["id"]),
        ).fetchall()
    finally:
        connection.close()

    result = json.loads(str(run["summary_json"]))
    result.update({"profile_id": run["profile_id"], "status": run["status"]})
    result["issues"] = [dict(row) for row in issues]
    return result


def test_client_validation_claim_is_forbidden_and_server_summary_is_explicit(
    harness: Harness,
):
    forged = _stage_body()
    forged["bridges"][0]["validation"] = {
        "status": "passed",
        "official_validation": True,
        "issues": [],
    }
    rejected = _stage(harness, key="forged-validation", body=forged)
    assert rejected.status_code == 422, rejected.text
    detail = rejected.json()["detail"]
    assert any(
        item["loc"][-1] == "validation" and item["type"] == "extra_forbidden"
        for item in detail
    )

    staged = _stage(harness, key="server-validation")
    assert staged.status_code == 201, staged.text
    revision_id = str(staged.json()["revisions"][0]["revision_id"])

    stored = _stored_validation(harness.database_path, revision_id)
    assert stored["validation_owner"] == "server"
    assert stored["official_validation"] is False
    assert stored["official_validation_status"] == "unavailable"

    history = harness.client.get(
        f"/api/revisions/snapshots/{STATE}/{BRIDGE}/{YEAR}/history",
        headers=harness.headers("viewer"),
    )
    assert history.status_code == 200, history.text
    public_summary = history.json()["revisions"][0]["validation"]
    assert public_summary["validation_owner"] == "server"
    assert public_summary["official_validation"] is False
    assert public_summary["official_validation_status"] == "unavailable"
    assert "issues" not in public_summary


@pytest.mark.parametrize("severity", ["critical", "error"])
def test_server_critical_or_error_blocks_publish_regardless_of_review_intent(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    severity: str,
):
    monkeypatch.setattr(
        revision_api,
        "_server_validation",
        lambda *args, **kwargs: _server_report(severity),
    )
    staged = _stage(harness, key=f"server-{severity}-stage")
    assert staged.status_code == 201, staged.text
    revision_id = str(staged.json()["revisions"][0]["revision_id"])
    stored = _stored_validation(harness.database_path, revision_id)
    assert stored["validation_owner"] == "server"
    assert stored["issues"][0]["severity"] == severity

    blocked = _publish(
        harness,
        revision_id,
        key=f"server-{severity}-publish",
        reviewed=True,
    )
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["detail"]["code"] == "critical_validation_issues"


def test_server_research_warning_requires_explicit_review(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        revision_api,
        "_server_validation",
        lambda *args, **kwargs: _server_report("warning"),
    )
    staged = _stage(harness, key="server-warning-stage")
    assert staged.status_code == 201, staged.text
    revision_id = str(staged.json()["revisions"][0]["revision_id"])

    unreviewed = _publish(
        harness,
        revision_id,
        key="server-warning-unreviewed",
        reviewed=False,
    )
    assert unreviewed.status_code == 422, unreviewed.text
    assert unreviewed.json()["detail"]["code"] == "research_review_required"

    published = _publish(
        harness,
        revision_id,
        key="server-warning-reviewed",
        reviewed=True,
    )
    assert published.status_code == 200, published.text
    assert published.json()["data_generation"] == 1


def test_revision_state_is_viewer_readable_and_server_workspace_owned(
    harness: Harness,
):
    state = harness.client.get(
        "/api/revisions/state",
        headers=harness.headers("viewer"),
    )
    assert state.status_code == 200, state.text
    assert state.json() == {
        "workspace_id": SHARED_WORKSPACE,
        "data_generation": 0,
        "schema_generation": LATEST_SCHEMA_VERSION,
    }
    assert state.headers["x-bridgeguard-data-version"] == "0"

    selected = harness.client.get(
        "/api/revisions/state",
        params={"workspace_id": "attacker-workspace"},
        headers=harness.headers("viewer"),
    )
    assert selected.status_code == 422, selected.text

