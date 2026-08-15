"""Red-first HTTP contract for PR5's staged revision API.

This suite deliberately exercises the ASGI boundary rather than duplicating the
revision-store unit contract.  It freezes the security properties that are easy
to lose in a thin HTTP adapter: the shared workspace and actor are server
derived, optimistic tokens and idempotency keys are mandatory, publication is
the only visibility mutation, and provenance is not returned by ordinary read
endpoints.

The focused app below reuses the real BridgeGuard routes and authentication
middleware but omits the production lifespan.  Every request therefore targets
an isolated SQLite database created by ``conftest.py`` and can never open the
deployed database.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main, revision_api
from app.auth import AuthMiddleware, AuthUser, COOKIE_NAME, issue_session
from app.routers.services.database import ensure_schema


SHARED_WORKSPACE = "shared-research-workspace"
STATE = 34
BRIDGE = "HTTP-REV-0001"
YEAR = 2026
RAW_EVIDENCE_MARKER = "TOP-SECRET-RAW-EVIDENCE-C:/private/source.txt"


AUTH_ENV = {
    "BRIDGEGUARD_AUTH_DISABLED": "0",
    "BRIDGEGUARD_AUTH_MODE": "password",
    "BRIDGEGUARD_SESSION_SECRET": "revision-api-contract-secret-longer-than-32-characters",
    "BRIDGEGUARD_PASSWORD": "revision-contract-password",
    "BRIDGEGUARD_ALLOWED_EMAILS": "viewer@example.com",
    "BRIDGEGUARD_EDITOR_EMAILS": "editor@example.com",
    "BRIDGEGUARD_ADMIN_EMAILS": "admin@example.com",
    "BRIDGEGUARD_ALLOWED_DOMAINS": "",
    "BRIDGEGUARD_OWNER_EMAIL": "",
}

ROLE_EMAIL = {
    "viewer": "viewer@example.com",
    "editor": "editor@example.com",
    "admin": "admin@example.com",
}


@dataclass
class ApiHarness:
    client: TestClient
    database_path: str

    def headers(self, role: str, **extra: str) -> dict[str, str]:
        email = ROLE_EMAIL[role]
        token = issue_session(AuthUser(email=email, role=role))
        return {
            "Cookie": f"{COOKIE_NAME}={token}",
            **extra,
        }


def _contract_app() -> FastAPI:
    """Mount only revision and legacy-loader routes, with real authorization."""

    focused = FastAPI()
    # FastAPI 0.131+ keeps included routers as a lazy ``_IncludedRouter``
    # sentinel in ``app.router.routes`` rather than flattening their APIRoutes.
    # Mount the real revision router explicitly so this focused harness tests
    # the same handlers used by the production app.
    focused.include_router(main.revision_router)
    for route in main.app.router.routes:
        path = str(getattr(route, "path", ""))
        if path == "/api/load-json":
            focused.router.routes.append(route)
    focused.add_middleware(AuthMiddleware)
    return focused


@pytest.fixture
def api_harness(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> ApiHarness:
    for key, value in AUTH_ENV.items():
        monkeypatch.setenv(key, value)

    database_path = str(test_workspace / "revision-http.db")
    ensure_schema(database_path)
    monkeypatch.setattr(main, "USER_DB", database_path)
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

    # Once the PR5 service exists, prepare the same server-owned workspace the
    # deployed app prepares at startup.  Keeping this setup out of the HTTP API
    # is intentional: clients must never create or select arbitrary workspaces.
    try:
        from app.services.conversion.revision_store import (
            ensure_workspace,
            initialize_revision_store,
        )
    except (ImportError, ModuleNotFoundError):
        pass
    else:
        initialize_revision_store(database_path, app_revision="pr5-http-contract")
        ensure_workspace(database_path, SHARED_WORKSPACE, "BridgeGuard research")

    with TestClient(_contract_app()) as client:
        yield ApiHarness(client=client, database_path=database_path)


def _document(
    *,
    name: str = "HTTP revision one",
    year_built: int = 1977,
) -> dict[str, Any]:
    return {
        "BL01": STATE,
        "BID01": BRIDGE,
        "BID02": name,
        "BL03": "00123",
        "BW01": year_built,
        "Elements": [],
        "Features": [
            {
                "BF01": "H01",
                "BF02": "1",
                "Routes": [
                    {"BRT01": "C01", "BRT02": "0001", "BRT03": "N"}
                ],
            }
        ],
        "Inspections": [],
        "PostingEvaluations": [],
        "PostingStatuses": [],
        "SpanSets": [],
        "SubstructureSets": [],
        "Works": [],
    }


def _validation(severity: str | None = None) -> dict[str, Any]:
    if severity is None:
        return {
            "profile_id": "research_historical_import",
            "status": "passed",
            "issues": [],
        }
    return {
        "profile_id": "research_historical_import",
        "status": "passed_with_warnings" if severity == "warning" else "failed",
        "issues": [
            {
                "rule_id": f"BG-HTTP-{severity.upper()}",
                "severity": severity,
                "json_pointer": "/BID02",
                "message": f"Fixture {severity} retained for publication review",
            }
        ],
    }


def _bridge_input(
    *,
    document: Mapping[str, Any] | None = None,
    data_year: int | None = None,
    include_private_evidence: bool = False,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "canonical_document": dict(document or _document()),
        "source_records": [],
        "lineage": [],
    }
    if data_year is not None:
        item["data_year"] = data_year
    if include_private_evidence:
        item["source_records"] = [
            {
                "source_artifact_id": "http-artifact-001",
                "source_record_id": "http-record-001",
                "record_kind": "native_snbi",
                "record_pointer": "/0",
                "payload": {"raw": RAW_EVIDENCE_MARKER},
            }
        ]
        item["lineage"] = [
            {
                "target_pointer": "/BID02",
                "source_artifact_id": "http-artifact-001",
                "source_record_id": "http-record-001",
                "source_pointer": "/BID02",
                "rule_id": "DIRECT-BID02",
                "status": "exact",
            }
        ]
    return item


def _stage_body(
    *bridges: Mapping[str, Any],
    expected_data_generation: int = 0,
    data_year: int | None = YEAR,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "profile_id": "research_historical_import",
        "atomic_mode": "batch",
        "expected_data_generation": expected_data_generation,
        "bridges": list(bridges or (_bridge_input(),)),
    }
    if data_year is not None:
        body["data_year"] = data_year
    return body


def _stage(
    api: ApiHarness,
    *,
    key: str,
    body: Mapping[str, Any] | None = None,
    role: str = "editor",
):
    return api.client.post(
        "/api/revisions/stage",
        headers=api.headers(
            role,
            **{
                "Idempotency-Key": key,
                "X-Request-ID": f"request-{key}",
            },
        ),
        json=dict(body or _stage_body()),
    )


def _publish(
    api: ApiHarness,
    revision_id: str,
    *,
    key: str,
    if_match: str,
    expected_data_generation: int,
    reviewed_research_warnings: bool = False,
    role: str = "editor",
):
    return api.client.post(
        f"/api/revisions/{revision_id}/publish",
        headers=api.headers(
            role,
            **{
                "Idempotency-Key": key,
                "If-Match": if_match,
                "X-Request-ID": f"request-{key}",
            },
        ),
        json={
            "reason": "HTTP contract publication review",
            "expected_data_generation": expected_data_generation,
            "reviewed_research_warnings": reviewed_research_warnings,
        },
    )


def _patch(
    api: ApiHarness,
    *,
    key: str,
    if_match: str,
    expected_data_generation: int,
    patch: Mapping[str, Any],
    role: str = "editor",
):
    return api.client.patch(
        f"/api/revisions/snapshots/{STATE}/{BRIDGE}/{YEAR}",
        headers=api.headers(
            role,
            **{
                "Idempotency-Key": key,
                "If-Match": if_match,
                "X-Request-ID": f"request-{key}",
            },
        ),
        json={
            "patch": dict(patch),
            "reason": "Reviewed HTTP editor correction",
            "expected_data_generation": expected_data_generation,
        },
    )


def _error_code(response) -> str | None:
    payload = response.json()
    detail = payload.get("detail", payload)
    return detail.get("code") if isinstance(detail, dict) else None


def _assert_strong_etag(response) -> str:
    etag = response.headers.get("etag")
    assert etag, response.text
    assert etag.startswith('"') and etag.endswith('"')
    assert not etag.startswith("W/")
    return etag


def _assert_no_private_evidence(value: Any) -> None:
    serialized = json.dumps(value, sort_keys=True)
    assert RAW_EVIDENCE_MARKER not in serialized
    lowered = serialized.lower()
    for forbidden in (
        "source_path",
        "local_path",
        "raw_record_json",
        "raw_evidence",
    ):
        assert forbidden not in lowered


def test_workspace_creation_is_not_public_and_workspace_actor_cannot_be_spoofed(
    api_harness: ApiHarness,
):
    initialize = api_harness.client.post(
        "/api/revisions/workspaces/initialize",
        headers=api_harness.headers("admin"),
        json={"workspace_id": "attacker-workspace"},
    )
    assert initialize.status_code == 404

    for forbidden_field, value in (
        ("workspace_id", "attacker-workspace"),
        ("actor_id", "forged@example.com"),
    ):
        body = _stage_body()
        body[forbidden_field] = value
        response = _stage(
            api_harness,
            key=f"forbidden-{forbidden_field}",
            body=body,
        )
        assert response.status_code == 422, response.text


def test_stage_requires_editor_idempotency_and_expected_generation(
    api_harness: ApiHarness,
):
    viewer = _stage(api_harness, key="viewer-cannot-stage", role="viewer")
    assert viewer.status_code == 403

    missing_key = api_harness.client.post(
        "/api/revisions/stage",
        headers=api_harness.headers("editor"),
        json=_stage_body(),
    )
    assert missing_key.status_code == 400
    assert _error_code(missing_key) == "idempotency_key_required"

    missing_generation = _stage_body()
    del missing_generation["expected_data_generation"]
    no_generation = _stage(
        api_harness,
        key="missing-generation",
        body=missing_generation,
    )
    assert no_generation.status_code == 422

    stale = _stage(
        api_harness,
        key="stale-stage-generation",
        body=_stage_body(expected_data_generation=99),
    )
    assert stale.status_code == 409
    assert _error_code(stale) == "stale_data_generation"


def test_stage_is_typed_idempotent_and_generation_neutral(api_harness: ApiHarness):
    first = _stage(api_harness, key="stage-idempotent")
    assert first.status_code == 201, first.text
    first_payload = first.json()
    first_item = first_payload["revisions"][0]
    assert first_payload["status"] == "staged"
    assert first_payload["data_generation"] == 0
    assert first.headers["x-bridgeguard-data-version"] == "0"
    assert first_item["outcome"] == "created"
    assert first_item["revision_number"] == 1
    assert first_item["state_code"] == STATE
    assert first_item["bridge_number"] == BRIDGE
    assert first_item["data_year"] == YEAR
    assert first_item["revision_id"]
    first_etag = _assert_strong_etag(first)

    replay = _stage(api_harness, key="stage-idempotent")
    assert replay.status_code == 200, replay.text
    assert replay.json() == first_payload
    assert _assert_strong_etag(replay) == first_etag
    assert replay.headers["x-bridgeguard-data-version"] == "0"

    changed = _stage(
        api_harness,
        key="stage-changed",
        body=_stage_body(
            _bridge_input(document=_document(name="HTTP revision two"))
        ),
    )
    assert changed.status_code == 201, changed.text
    changed_item = changed.json()["revisions"][0]
    assert changed_item["outcome"] == "created"
    assert changed_item["revision_number"] == 2
    assert changed.headers["x-bridgeguard-data-version"] == "0"
    assert _assert_strong_etag(changed) != first_etag

    changed_key_payload = _stage_body(
        _bridge_input(document=_document(name="Conflicting idempotency payload"))
    )
    conflict = _stage(
        api_harness,
        key="stage-idempotent",
        body=changed_key_payload,
    )
    assert conflict.status_code == 409
    assert _error_code(conflict) == "idempotency_payload_conflict"

    # A fresh idempotency key with byte-equivalent canonical content is a
    # successful no-op, not a conflict and not a second revision.
    noop = _stage(
        api_harness,
        key="stage-same-content-new-key",
        body=_stage_body(
            _bridge_input(document=_document(name="HTTP revision two"))
        ),
    )
    assert noop.status_code == 201, noop.text
    assert noop.json()["revisions"][0]["outcome"] == "noop"
    assert noop.json()["revisions"][0]["revision_number"] == 2
    assert noop.json()["data_generation"] == 0


def test_stage_accepts_item_or_batch_year_but_never_infers_a_missing_year(
    api_harness: ApiHarness,
):
    missing = _stage(
        api_harness,
        key="year-missing",
        body=_stage_body(_bridge_input(), data_year=None),
    )
    assert missing.status_code == 422
    assert _error_code(missing) == "data_year_required"

    item_year = _stage(
        api_harness,
        key="year-item",
        body=_stage_body(_bridge_input(data_year=YEAR), data_year=None),
    )
    assert item_year.status_code == 201, item_year.text
    assert item_year.json()["revisions"][0]["data_year"] == YEAR


def test_research_warnings_require_explicit_review_and_reads_are_viewer_safe(
    api_harness: ApiHarness,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        revision_api,
        "_server_validation",
        lambda *args, **kwargs: {
            **_validation("warning"),
            "validation_owner": "server",
            "official_validation": False,
            "official_validation_status": "unavailable",
        },
    )
    staged = _stage(
        api_harness,
        key="warning-stage",
        body=_stage_body(
            _bridge_input(
                include_private_evidence=True,
            )
        ),
    )
    assert staged.status_code == 201, staged.text
    _assert_no_private_evidence(staged.json())
    revision_id = str(staged.json()["revisions"][0]["revision_id"])

    unreviewed = _publish(
        api_harness,
        revision_id,
        key="warning-unreviewed",
        if_match='"none"',
        expected_data_generation=0,
        reviewed_research_warnings=False,
    )
    assert unreviewed.status_code == 422
    assert _error_code(unreviewed) == "research_review_required"

    published = _publish(
        api_harness,
        revision_id,
        key="warning-reviewed",
        if_match='"none"',
        expected_data_generation=0,
        reviewed_research_warnings=True,
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"
    assert published.json()["revision_id"] == staged.json()["revisions"][0]["revision_id"]
    assert published.json()["data_generation"] == 1
    assert published.headers["x-bridgeguard-data-version"] == "1"
    published_etag = _assert_strong_etag(published)

    exact = api_harness.client.get(
        f"/api/revisions/snapshots/{STATE}/{BRIDGE}/{YEAR}",
        headers=api_harness.headers("viewer"),
    )
    assert exact.status_code == 200, exact.text
    assert _assert_strong_etag(exact) == published_etag
    assert exact.headers["x-bridgeguard-data-version"] == "1"
    exact_payload = exact.json()
    assert exact_payload["revision_id"] == staged.json()["revisions"][0]["revision_id"]
    assert exact_payload["state_code"] == STATE
    assert exact_payload["bridge_number"] == BRIDGE
    assert exact_payload["data_year"] == YEAR
    assert exact_payload["canonical_document"]["BID02"] == "HTTP revision one"
    assert exact_payload["validation"]["status"] == "passed_with_warnings"
    assert [issue["rule_id"] for issue in exact_payload["validation_issues"]] == [
        "BG-HTTP-WARNING"
    ]
    _assert_no_private_evidence(exact_payload)

    connection = sqlite3.connect(api_harness.database_path)
    try:
        actors = connection.execute(
            "SELECT DISTINCT actor_id FROM mutation_audit ORDER BY actor_id"
        ).fetchall()
    finally:
        connection.close()
    assert actors == [("editor@example.com",)]


def test_critical_validation_can_stage_but_cannot_publish(
    api_harness: ApiHarness,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        revision_api,
        "_server_validation",
        lambda *args, **kwargs: {
            **_validation("critical"),
            "validation_owner": "server",
            "official_validation": False,
            "official_validation_status": "unavailable",
        },
    )
    staged = _stage(
        api_harness,
        key="critical-stage",
        body=_stage_body(_bridge_input()),
    )
    assert staged.status_code == 201, staged.text
    revision_id = str(staged.json()["revisions"][0]["revision_id"])

    blocked = _publish(
        api_harness,
        revision_id,
        key="critical-publish",
        if_match='"none"',
        expected_data_generation=0,
        reviewed_research_warnings=True,
    )
    assert blocked.status_code == 422
    assert _error_code(blocked) == "critical_validation_issues"

    absent = api_harness.client.get(
        f"/api/revisions/snapshots/{STATE}/{BRIDGE}/{YEAR}",
        headers=api_harness.headers("viewer"),
    )
    assert absent.status_code == 404
    assert absent.headers.get("x-bridgeguard-data-version") == "0"


def test_publish_and_patch_require_strong_optimistic_preconditions(
    api_harness: ApiHarness,
):
    staged = _stage(api_harness, key="precondition-stage")
    assert staged.status_code == 201, staged.text
    revision_id = str(staged.json()["revisions"][0]["revision_id"])

    missing_publish_headers = api_harness.client.post(
        f"/api/revisions/{revision_id}/publish",
        headers=api_harness.headers("editor"),
        json={
            "reason": "Missing preconditions",
            "expected_data_generation": 0,
        },
    )
    assert missing_publish_headers.status_code == 428
    assert _error_code(missing_publish_headers) == "precondition_required"

    viewer_publish = _publish(
        api_harness,
        revision_id,
        key="viewer-publish",
        if_match='"none"',
        expected_data_generation=0,
        role="viewer",
    )
    assert viewer_publish.status_code == 403

    published = _publish(
        api_harness,
        revision_id,
        key="precondition-publish",
        if_match='"none"',
        expected_data_generation=0,
    )
    assert published.status_code == 200, published.text
    published_etag = _assert_strong_etag(published)

    stale_publish = _publish(
        api_harness,
        revision_id,
        key="stale-publish",
        if_match='"none"',
        expected_data_generation=1,
    )
    assert stale_publish.status_code == 409
    assert _error_code(stale_publish) == "stale_revision"

    missing_patch_headers = api_harness.client.patch(
        f"/api/revisions/snapshots/{STATE}/{BRIDGE}/{YEAR}",
        headers=api_harness.headers("editor"),
        json={
            "patch": {"BID02": "Missing preconditions"},
            "reason": "Missing preconditions",
            "expected_data_generation": 1,
        },
    )
    assert missing_patch_headers.status_code == 428
    assert _error_code(missing_patch_headers) == "precondition_required"

    stale_patch = _patch(
        api_harness,
        key="stale-patch",
        if_match='"none"',
        expected_data_generation=1,
        patch={"BID02": "Must not apply"},
    )
    assert stale_patch.status_code == 409
    assert _error_code(stale_patch) == "stale_revision"

    patched = _patch(
        api_harness,
        key="valid-patch",
        if_match=published_etag,
        expected_data_generation=1,
        patch={"BID02": "Staged editor change", "BW01": None},
    )
    assert patched.status_code == 201, patched.text
    patch_payload = patched.json()
    assert patch_payload["status"] == "staged"
    assert patch_payload["previous_revision_id"] == published.json()["revision_id"]
    assert patch_payload["revision_number"] == 2
    assert patch_payload["data_generation"] == 1
    assert {(item["op"], item["path"]) for item in patch_payload["diff"]} == {
        ("replace", "/BID02"),
        ("remove", "/BW01"),
    }
    assert patch_payload["validation"]["validation_owner"] == "server"
    assert patch_payload["validation"]["status"] == "passed"
    assert [issue["rule_id"] for issue in patch_payload["validation_issues"]] == [
        "BG-PATCH-INTEGRITY"
    ]
    assert patched.headers["x-bridgeguard-data-version"] == "1"
    assert _assert_strong_etag(patched) != published_etag

    connection = sqlite3.connect(api_harness.database_path)
    try:
        candidate_json = connection.execute(
            "SELECT canonical_json FROM snapshot_revisions WHERE id=?",
            (patch_payload["revision_id"],),
        ).fetchone()[0]
    finally:
        connection.close()
    candidate_document = json.loads(candidate_json)
    assert candidate_document["BID02"] == "Staged editor change"
    assert "BW01" not in candidate_document

    # PATCH creates an immutable candidate; it does not silently change the
    # published read model or its concurrency token.
    active = api_harness.client.get(
        f"/api/revisions/snapshots/{STATE}/{BRIDGE}/{YEAR}",
        headers=api_harness.headers("viewer"),
    )
    assert active.status_code == 200
    assert _assert_strong_etag(active) == published_etag
    assert active.json()["canonical_document"]["BID02"] == "HTTP revision one"
    assert active.json()["canonical_document"]["BW01"] == 1977


def test_history_is_exact_ordered_private_by_default_and_not_workspace_selectable(
    api_harness: ApiHarness,
):
    first = _stage(
        api_harness,
        key="history-one",
        body=_stage_body(_bridge_input(include_private_evidence=True)),
    )
    assert first.status_code == 201, first.text
    second = _stage(
        api_harness,
        key="history-two",
        body=_stage_body(
            _bridge_input(document=_document(name="History revision two"))
        ),
    )
    assert second.status_code == 201, second.text

    history = api_harness.client.get(
        f"/api/revisions/snapshots/{STATE}/{BRIDGE}/{YEAR}/history",
        headers=api_harness.headers("viewer"),
    )
    assert history.status_code == 200, history.text
    payload = history.json()
    assert payload["state_code"] == STATE
    assert payload["bridge_number"] == BRIDGE
    assert payload["data_year"] == YEAR
    assert [item["revision_number"] for item in payload["revisions"]] == [2, 1]
    assert payload["data_generation"] == 0
    assert history.headers["x-bridgeguard-data-version"] == "0"
    _assert_no_private_evidence(payload)

    selected = api_harness.client.get(
        f"/api/revisions/snapshots/{STATE}/{BRIDGE}/{YEAR}/history",
        params={"workspace_id": "attacker-workspace"},
        headers=api_harness.headers("viewer"),
    )
    assert selected.status_code == 422


def test_legacy_loader_never_logs_source_payload_or_gives_destructive_reload_advice(
    api_harness: ApiHarness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    legacy_document = _document(name=RAW_EVIDENCE_MARKER)
    monkeypatch.setattr(
        main,
        "validate_snbi_payload",
        lambda *args, **kwargs: {"summary": {}, "issues": []},
    )
    monkeypatch.setattr(
        main,
        "load_json_to_db",
        lambda *args, **kwargs: (0, 1, 0, []),
    )

    response = api_harness.client.post(
        "/api/load-json",
        headers=api_harness.headers("editor"),
        files={
            "file": (
                "legacy.json",
                json.dumps([legacy_document]).encode("utf-8"),
                "application/json",
            )
        },
        data={"data_year": str(YEAR)},
    )
    assert response.status_code == 200, response.text
    captured = capsys.readouterr()
    assert RAW_EVIDENCE_MARKER not in captured.out
    assert RAW_EVIDENCE_MARKER not in captured.err

    response_text = json.dumps(response.json()).lower()
    assert "delete data year" not in response_text
    assert "delete" not in response_text or "reload" not in response_text
