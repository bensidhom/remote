"""Red-first HTTP contract for PR7's request-scoped Bridgette API.

The SQL validator, SQLite sandbox, semantic catalog, and request registry have
their own unit contracts.  This file freezes the ASGI adapter between those
services and the browser.  In particular, it prevents a later UI refactor from
reintroducing client-selected identity/scope, unbounded sidebar queries, or the
old process-global cancellation behavior.

No provider or production database is used here.  ``_bridgette_service_stream``
is a deliberately narrow test seam: it receives one immutable request object,
emits evidence/token events, and lets the HTTP adapter own the accepted and
terminal protocol events.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Any, Iterator, Mapping

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main, revision_api
from app.auth import AuthMiddleware, AuthUser, COOKIE_NAME, issue_session
from app.services.query.registry import RequestRegistry
from app.services.query.sandbox import QueryCancelled


SHARED_WORKSPACE = revision_api.SHARED_WORKSPACE_ID
SERVER_STATE_CODE = 34

FULL_REQUEST_ID = "11111111-1111-4111-8111-111111111111"
SIDEBAR_REQUEST_ID = "22222222-2222-4222-8222-222222222222"
EXACT_REQUEST_ID = "33333333-3333-4333-8333-333333333333"
TREND_REQUEST_ID = "44444444-4444-4444-8444-444444444444"
CANCEL_REQUEST_ID = "55555555-5555-4555-8555-555555555555"
LEGACY_REQUEST_ID = "66666666-6666-4666-8666-666666666666"


AUTH_ENV = {
    "BRIDGEGUARD_AUTH_DISABLED": "0",
    "BRIDGEGUARD_AUTH_MODE": "password",
    "BRIDGEGUARD_SESSION_SECRET": (
        "bridgette-http-contract-secret-longer-than-32-characters"
    ),
    "BRIDGEGUARD_PASSWORD": "bridgette-http-contract-password",
    "BRIDGEGUARD_ALLOWED_EMAILS": "viewer@example.com,other@example.com",
    "BRIDGEGUARD_EDITOR_EMAILS": "",
    "BRIDGEGUARD_ADMIN_EMAILS": "",
    "BRIDGEGUARD_ALLOWED_DOMAINS": "",
    "BRIDGEGUARD_OWNER_EMAIL": "",
}


def _read(value: object, name: str) -> Any:
    if isinstance(value, Mapping):
        return value[name]
    return getattr(value, name)


def _enum_value(value: object) -> object:
    return getattr(value, "value", value)


def _scope_payload(request: object) -> dict[str, object]:
    scope = _read(request, "scope")
    return {
        "workspace_id": _read(scope, "workspace_id"),
        "state_code": _read(scope, "state_code"),
        "time_mode": _enum_value(_read(scope, "time_mode")),
        "year": _read(scope, "data_year"),
        "start_year": _read(scope, "start_year"),
        "end_year": _read(scope, "end_year"),
    }


class FakeBridgetteService:
    """Provider-free stream used to inspect the trusted service request."""

    def __init__(self) -> None:
        self.requests: list[object] = []
        self.mode = "complete"

    def __call__(self, request: object) -> Iterator[dict[str, object]]:
        self.requests.append(request)
        if self.mode == "cancelled":
            raise QueryCancelled("request cancelled")
        if self.mode == "wrapped_cancelled":
            _read(request, "cancellation_token").set()
            raise RuntimeError("provider cancellation wrapped by audit service")
        if self.mode == "error":
            raise RuntimeError("simulated provider failure")

        yield {
            "type": "evidence",
            "sql": (
                "SELECT state_code, COUNT(*) AS bridge_count "
                "FROM v_bridge_condition GROUP BY state_code"
            ),
            "columns": ["state_code", "bridge_count"],
            "rows": [[SERVER_STATE_CODE, 12]],
            "count": 1,
            "truncated": False,
            "scope": _scope_payload(request),
            "catalog_version": "semantic-catalog-test-v1",
            "schema_generation": 5,
            "data_generation": 9,
        }
        yield {"type": "token", "content": "There are "}
        yield {"type": "token", "content": "12 bridges."}


@dataclass
class HttpHarness:
    client: TestClient
    service: FakeBridgetteService
    registry: RequestRegistry

    def headers(
        self,
        request_id: str,
        *,
        email: str = "viewer@example.com",
    ) -> dict[str, str]:
        token = issue_session(AuthUser(email=email, role="viewer"))
        return {
            "Cookie": f"{COOKIE_NAME}={token}",
            "X-Request-ID": request_id,
        }


def _contract_app() -> FastAPI:
    focused = FastAPI()
    # FastAPI 0.131+ retains included routers as lazy sentinels, so mount the
    # same production router directly just like the revision HTTP contracts.
    focused.include_router(main.bridgette_query_router)
    for route in main.app.router.routes:
        if str(getattr(route, "path", "")) == "/api/ask-cancel":
            focused.router.routes.append(route)
    focused.add_middleware(AuthMiddleware)
    return focused


@pytest.fixture
def http_harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[HttpHarness]:
    for key, value in AUTH_ENV.items():
        monkeypatch.setenv(key, value)

    service = FakeBridgetteService()
    registry = RequestRegistry()
    monkeypatch.setattr(
        main,
        "_bridgette_service_stream",
        service,
        raising=False,
    )
    # Patch both lookup locations so implementations may import the singleton
    # at module load or resolve it lazily without weakening this contract.
    from app.services.query import registry as registry_module

    monkeypatch.setattr(registry_module, "DEFAULT_REQUEST_REGISTRY", registry)
    monkeypatch.setattr(
        main,
        "DEFAULT_REQUEST_REGISTRY",
        registry,
        raising=False,
    )

    with TestClient(_contract_app()) as client:
        yield HttpHarness(client=client, service=service, registry=registry)


def _events(response) -> list[dict[str, object]]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


@pytest.mark.parametrize(
    ("path", "request_id", "surface", "row_limit"),
    (
        ("/api/bridgette/stream", FULL_REQUEST_ID, "full", 200),
        (
            "/api/bridgette/sidebar/stream",
            SIDEBAR_REQUEST_ID,
            "sidebar",
            50,
        ),
    ),
)
def test_full_and_sidebar_streams_have_parity_and_server_owned_caps(
    http_harness: HttpHarness,
    path: str,
    request_id: str,
    surface: str,
    row_limit: int,
):
    response = http_harness.client.post(
        path,
        headers=http_harness.headers(request_id),
        data={"question": "How many published bridges are in New Jersey?"},
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert response.headers["x-request-id"] == request_id
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"

    events = _events(response)
    assert [event["type"] for event in events] == [
        "accepted",
        "evidence",
        "token",
        "token",
        "done",
    ]
    assert all(event["request_id"] == request_id for event in events)
    assert events[0]["surface"] == surface
    assert events[0]["row_limit"] == row_limit
    assert "".join(
        str(event["content"])
        for event in events
        if event["type"] == "token"
    ) == "There are 12 bridges."

    evidence = events[1]
    assert {
        "sql",
        "columns",
        "rows",
        "count",
        "truncated",
        "scope",
        "catalog_version",
        "schema_generation",
        "data_generation",
    } <= evidence.keys()
    assert evidence["count"] == len(evidence["rows"])
    assert evidence["columns"] == ["state_code", "bridge_count"]
    assert evidence["truncated"] is False

    assert len(http_harness.service.requests) == 1
    service_request = http_harness.service.requests[0]
    assert _read(service_request, "request_id") == request_id
    assert _read(service_request, "question") == (
        "How many published bridges are in New Jersey?"
    )
    assert _read(service_request, "surface") == surface
    assert _read(service_request, "row_limit") == row_limit
    assert _read(service_request, "actor_id") == "viewer@example.com"
    assert _read(service_request, "actor_role") == "viewer"
    scope = _read(service_request, "scope")
    assert _read(scope, "workspace_id") == SHARED_WORKSPACE
    assert _read(scope, "state_code") == SERVER_STATE_CODE
    assert _enum_value(_read(scope, "time_mode")) == "latest"


def test_full_and_sidebar_evidence_objects_use_the_same_contract(
    http_harness: HttpHarness,
):
    full = http_harness.client.post(
        "/api/bridgette/stream",
        headers=http_harness.headers(FULL_REQUEST_ID),
        data={"question": "Count bridges"},
    )
    sidebar = http_harness.client.post(
        "/api/bridgette/sidebar/stream",
        headers=http_harness.headers(SIDEBAR_REQUEST_ID),
        data={"question": "Count bridges"},
    )

    assert full.status_code == sidebar.status_code == 200
    full_evidence = next(e for e in _events(full) if e["type"] == "evidence")
    sidebar_evidence = next(
        e for e in _events(sidebar) if e["type"] == "evidence"
    )
    assert set(full_evidence) == set(sidebar_evidence)
    assert full_evidence["catalog_version"] == sidebar_evidence["catalog_version"]
    assert full_evidence["schema_generation"] == sidebar_evidence["schema_generation"]
    assert full_evidence["data_generation"] == sidebar_evidence["data_generation"]


@pytest.mark.parametrize(
    ("request_id", "form", "expected"),
    (
        (
            EXACT_REQUEST_ID,
            {"time_mode": "exact", "year": "2024"},
            {
                "time_mode": "exact",
                "data_year": 2024,
                "start_year": None,
                "end_year": None,
            },
        ),
        (
            TREND_REQUEST_ID,
            {
                "time_mode": "trend",
                "start_year": "2019",
                "end_year": "2024",
            },
            {
                "time_mode": "trend",
                "data_year": None,
                "start_year": 2019,
                "end_year": 2024,
            },
        ),
    ),
)
def test_time_scope_is_validated_then_translated_to_a_trusted_query_scope(
    http_harness: HttpHarness,
    request_id: str,
    form: dict[str, str],
    expected: dict[str, object],
):
    response = http_harness.client.post(
        "/api/bridgette/stream",
        headers=http_harness.headers(request_id),
        data={"question": "Show the condition trend", **form},
    )

    assert response.status_code == 200, response.text
    scope = _read(http_harness.service.requests[-1], "scope")
    assert _enum_value(_read(scope, "time_mode")) == expected["time_mode"]
    assert _read(scope, "data_year") == expected["data_year"]
    assert _read(scope, "start_year") == expected["start_year"]
    assert _read(scope, "end_year") == expected["end_year"]
    evidence = next(e for e in _events(response) if e["type"] == "evidence")
    assert evidence["scope"]["workspace_id"] == SHARED_WORKSPACE
    assert evidence["scope"]["state_code"] == SERVER_STATE_CODE


@pytest.mark.parametrize(
    "form",
    (
        {"time_mode": "unknown"},
        {"time_mode": "exact"},
        {"time_mode": "exact", "year": "2024", "start_year": "2020"},
        {"time_mode": "latest", "year": "2024"},
        {"time_mode": "trend", "start_year": "2020"},
        {"time_mode": "trend", "start_year": "2025", "end_year": "2020"},
        {"time_mode": "trend", "year": "2024", "start_year": "2020", "end_year": "2024"},
        {"time_mode": "exact", "year": "1799"},
    ),
)
def test_invalid_time_scope_fails_before_any_query_or_model_call(
    http_harness: HttpHarness,
    form: dict[str, str],
):
    response = http_harness.client.post(
        "/api/bridgette/stream",
        headers=http_harness.headers(EXACT_REQUEST_ID),
        data={"question": "Count bridges", **form},
    )

    assert response.status_code == 422
    assert http_harness.service.requests == []


@pytest.mark.parametrize(
    "request_id",
    (
        "not-a-uuid",
        "00000000-0000-1000-8000-000000000000",  # UUIDv1, not UUIDv4
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",  # non-canonical case
        "aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa",  # non-canonical compact form
    ),
)
def test_stream_requires_a_canonical_uuidv4_request_id(
    http_harness: HttpHarness,
    request_id: str,
):
    response = http_harness.client.post(
        "/api/bridgette/stream",
        headers=http_harness.headers(request_id),
        data={"question": "Count bridges"},
    )

    assert response.status_code == 422
    assert http_harness.service.requests == []


def test_stream_requires_request_id_and_non_blank_question(
    http_harness: HttpHarness,
):
    missing_id = http_harness.client.post(
        "/api/bridgette/stream",
        headers={
            "Cookie": http_harness.headers(FULL_REQUEST_ID)["Cookie"],
        },
        data={"question": "Count bridges"},
    )
    missing_question = http_harness.client.post(
        "/api/bridgette/stream",
        headers=http_harness.headers(FULL_REQUEST_ID),
        data={},
    )
    blank_question = http_harness.client.post(
        "/api/bridgette/stream",
        headers=http_harness.headers(FULL_REQUEST_ID),
        data={"question": " \t\r\n "},
    )

    assert missing_id.status_code == 422
    assert missing_question.status_code == 422
    assert blank_question.status_code == 422
    assert http_harness.service.requests == []


@pytest.mark.parametrize(
    ("field", "spoofed_value"),
    (
        ("actor_id", "admin@example.com"),
        ("actor_role", "admin"),
        ("workspace_id", "another-tenant"),
    ),
)
def test_client_identity_and_workspace_spoof_fields_are_rejected(
    http_harness: HttpHarness,
    field: str,
    spoofed_value: str,
):
    response = http_harness.client.post(
        "/api/bridgette/stream",
        headers=http_harness.headers(FULL_REQUEST_ID),
        data={"question": "Count bridges", field: spoofed_value},
    )

    assert response.status_code == 422
    assert http_harness.service.requests == []


@pytest.mark.parametrize(
    ("mode", "terminal_type"),
    (
        ("complete", "done"),
        ("cancelled", "stopped"),
        ("wrapped_cancelled", "stopped"),
        ("error", "error"),
    ),
)
def test_stream_has_exactly_one_request_owned_terminal_event(
    http_harness: HttpHarness,
    mode: str,
    terminal_type: str,
):
    http_harness.service.mode = mode
    response = http_harness.client.post(
        "/api/bridgette/stream",
        headers=http_harness.headers(FULL_REQUEST_ID),
        data={"question": "Count bridges"},
    )

    assert response.status_code == 200
    events = _events(response)
    assert events[0]["type"] == "accepted"
    terminal = [e for e in events if e["type"] in {"done", "stopped", "error"}]
    assert len(terminal) == 1
    assert terminal[0]["type"] == terminal_type
    assert terminal[0]["request_id"] == FULL_REQUEST_ID
    assert all(event["request_id"] == FULL_REQUEST_ID for event in events)
    assert events[-1] == terminal[0]


class InterruptProbe:
    def __init__(self) -> None:
        self.interrupt_count = 0

    def interrupt(self) -> None:
        self.interrupt_count += 1


def test_cancel_is_exact_owner_scoped_and_interrupts_that_request_only(
    http_harness: HttpHarness,
):
    entry = http_harness.registry.register(
        request_id=CANCEL_REQUEST_ID,
        owner_id="viewer@example.com",
        workspace_id=SHARED_WORKSPACE,
        operation="bridgette-full",
        deadline=time.monotonic() + 30,
    )
    probe = InterruptProbe()
    http_harness.registry.attach_connection(CANCEL_REQUEST_ID, probe)  # type: ignore[arg-type]

    cross_user = http_harness.client.post(
        f"/api/bridgette/requests/{CANCEL_REQUEST_ID}/cancel",
        headers=http_harness.headers(
            "77777777-7777-4777-8777-777777777777",
            email="other@example.com",
        ),
    )
    assert cross_user.status_code == 404
    assert entry.cancellation_token.is_set() is False
    assert probe.interrupt_count == 0

    owner = http_harness.client.post(
        f"/api/bridgette/requests/{CANCEL_REQUEST_ID}/cancel",
        headers=http_harness.headers(
            "88888888-8888-4888-8888-888888888888",
        ),
    )
    assert owner.status_code == 202, owner.text
    assert owner.json() == {
        "request_id": CANCEL_REQUEST_ID,
        "status": "cancellation_requested",
    }
    assert entry.cancellation_token.is_set() is True
    assert probe.interrupt_count == 1


def test_cancel_missing_malformed_and_cross_user_ids_are_non_enumerating(
    http_harness: HttpHarness,
):
    owned = http_harness.registry.register(
        request_id=CANCEL_REQUEST_ID,
        owner_id="viewer@example.com",
        workspace_id=SHARED_WORKSPACE,
        operation="bridgette-full",
        deadline=time.monotonic() + 30,
    )
    request_headers = http_harness.headers(
        "99999999-9999-4999-8999-999999999999",
        email="other@example.com",
    )
    responses = [
        http_harness.client.post(
            f"/api/bridgette/requests/{CANCEL_REQUEST_ID}/cancel",
            headers=request_headers,
        ),
        http_harness.client.post(
            "/api/bridgette/requests/not-a-uuid/cancel",
            headers=request_headers,
        ),
        http_harness.client.post(
            "/api/bridgette/requests/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/cancel",
            headers=request_headers,
        ),
    ]

    assert [response.status_code for response in responses] == [404, 404, 404]
    assert responses[0].json() == responses[1].json() == responses[2].json()
    assert owned.cancellation_token.is_set() is False


def test_legacy_ask_cancel_cannot_cancel_a_bridgette_registry_entry(
    http_harness: HttpHarness,
):
    entry = http_harness.registry.register(
        request_id=LEGACY_REQUEST_ID,
        owner_id="viewer@example.com",
        workspace_id=SHARED_WORKSPACE,
        operation="bridgette-sidebar",
        deadline=time.monotonic() + 30,
    )

    response = http_harness.client.post(
        "/api/ask-cancel",
        headers=http_harness.headers(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        ),
    )

    # A compatibility endpoint may still cancel only legacy Ask work, or it may
    # be retired.  Either way it must never signal the Bridgette request token.
    assert response.status_code in {200, 410}
    assert entry.cancellation_token.is_set() is False
