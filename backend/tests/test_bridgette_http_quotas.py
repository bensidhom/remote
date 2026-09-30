from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import AuthMiddleware, AuthUser, COOKIE_NAME, issue_session
from app.query_api import (
    ProviderRequestRegistry,
    _stream_events,
    create_bridgette_router,
)
from app.services.operations.quotas import OperationQuotaRegistry, QuotaExceeded
from app.services.query.registry import RequestRegistry


WORKSPACE = "quota-http-workspace"
STATE_CODE = 34
FIRST_REQUEST_ID = "11111111-1111-4111-8111-111111111111"
SECOND_REQUEST_ID = "22222222-2222-4222-8222-222222222222"
THIRD_REQUEST_ID = "33333333-3333-4333-8333-333333333333"

AUTH_ENV = {
    "BRIDGEGUARD_AUTH_DISABLED": "0",
    "BRIDGEGUARD_AUTH_MODE": "password",
    "BRIDGEGUARD_SESSION_SECRET": "quota-http-secret-longer-than-thirty-two-characters",
    "BRIDGEGUARD_PASSWORD": "quota-http-password",
    "BRIDGEGUARD_ALLOWED_EMAILS": "viewer@example.com,other@example.com",
    "BRIDGEGUARD_EDITOR_EMAILS": "",
    "BRIDGEGUARD_ADMIN_EMAILS": "",
    "BRIDGEGUARD_ALLOWED_DOMAINS": "",
    "BRIDGEGUARD_OWNER_EMAIL": "",
}


def _headers(request_id: str, *, email: str = "viewer@example.com") -> dict[str, str]:
    token = issue_session(AuthUser(email=email, role="viewer"))
    return {
        "Cookie": f"{COOKIE_NAME}={token}",
        "X-Request-ID": request_id,
    }


def _evidence(request) -> dict[str, object]:
    return {
        "type": "evidence",
        "sql": "SELECT COUNT(*) AS bridge_count FROM v_current_bridge_snapshots",
        "columns": ["bridge_count"],
        "rows": [[12]],
        "count": 1,
        "truncated": False,
        "scope": {
            "workspace_id": request.scope.workspace_id,
            "state_code": request.scope.state_code,
            "time_mode": request.scope.time_mode.value,
        },
        "catalog_version": "quota-test-v1",
        "schema_generation": 1,
        "data_generation": 1,
    }


class BlockingService:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, request) -> Iterator[dict[str, object]]:
        self.started.set()
        while not self.release.wait(0.01):
            if request.cancellation_token.is_set():
                return
        yield _evidence(request)
        yield {"type": "token", "content": "There are 12 bridges."}


class FailingService:
    def __call__(self, _request) -> Iterator[dict[str, object]]:
        if False:  # pragma: no cover - makes this a generator intentionally
            yield {}
        raise RuntimeError("private provider failure")


def _app(service, provider_registry: ProviderRequestRegistry) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_bridgette_router(
            stream_handler_getter=lambda: service,
            registry_getter=RequestRegistry,
            workspace_id=WORKSPACE,
            state_code=STATE_CODE,
            provider_registry=provider_registry,
        )
    )
    app.add_middleware(AuthMiddleware)
    return app


@pytest.fixture(autouse=True)
def _auth_environment(monkeypatch: pytest.MonkeyPatch):
    for key, value in AUTH_ENV.items():
        monkeypatch.setenv(key, value)


def test_concurrent_http_request_over_quota_returns_429_and_retry_after():
    quotas = OperationQuotaRegistry(
        operation_limits={"ask": 1},
        workspace_limit=4,
        owner_limit=4,
        maximum_leases=4,
    )
    providers = ProviderRequestRegistry(
        max_active_requests=4,
        quota_registry=quotas,
    )
    service = BlockingService()
    app = _app(service, providers)

    with TestClient(app) as first_client, TestClient(app) as second_client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(
                first_client.post,
                "/api/bridgette/stream",
                headers=_headers(FIRST_REQUEST_ID),
                data={"question": "Count bridges"},
            )
            assert service.started.wait(3)
            snapshot = quotas.snapshot(
                workspace_id=WORKSPACE,
                owner_id="viewer@example.com",
                operation="ask",
            )
            assert snapshot.active_total == snapshot.active_for_operation == 1

            rejected = second_client.post(
                "/api/bridgette/stream",
                headers=_headers(SECOND_REQUEST_ID, email="other@example.com"),
                data={"question": "Count bridges"},
            )
            assert rejected.status_code == 429
            assert rejected.headers["retry-after"] == "1"
            assert rejected.json() == {
                "detail": "Too many active Bridgette requests. Try again shortly."
            }

            service.release.set()
            completed = first.result(timeout=3)

        assert completed.status_code == 200
        assert json.loads(completed.text.splitlines()[-1])["type"] == "done"
        assert quotas.snapshot(
            workspace_id=WORKSPACE,
            owner_id="viewer@example.com",
            operation="ask",
        ).active_total == 0

        # Capacity is immediately reusable after the terminal stream event.
        admitted = second_client.post(
            "/api/bridgette/stream",
            headers=_headers(THIRD_REQUEST_ID, email="other@example.com"),
            data={"question": "Count bridges"},
        )
        assert admitted.status_code == 200


def test_stream_close_after_acceptance_releases_quota_exactly_once():
    quotas = OperationQuotaRegistry(
        operation_limits={"ask": 1},
        workspace_limit=1,
        owner_limit=1,
        maximum_leases=1,
    )
    providers = ProviderRequestRegistry(quota_registry=quotas)
    entry = providers.register(
        request_id=FIRST_REQUEST_ID,
        actor_id="viewer@example.com",
        workspace_id=WORKSPACE,
    )
    from app.query_api import BridgetteStreamRequest
    from app.services.query.sql_ast import QueryScope

    request = BridgetteStreamRequest(
        request_id=FIRST_REQUEST_ID,
        question="Count bridges",
        model="",
        surface="full",
        row_limit=200,
        actor_id="viewer@example.com",
        actor_role="viewer",
        scope=QueryScope(
            workspace_id=WORKSPACE,
            state_code=STATE_CODE,
            time_mode="latest",
        ),
        cancellation_token=entry.cancellation_token,
    )
    stream = _stream_events(
        request,
        stream_handler_getter=lambda: (_ for _ in ()).throw(
            AssertionError("service must not start before the accepted event is consumed")
        ),
        provider_registry=providers,
    )
    assert json.loads(next(stream))["type"] == "accepted"
    stream.close()

    assert quotas.snapshot(
        workspace_id=WORKSPACE,
        owner_id="viewer@example.com",
        operation="ask",
    ).active_total == 0
    assert providers.finish(FIRST_REQUEST_ID) is False


def test_http_cancellation_keeps_then_releases_the_same_quota_lease():
    quotas = OperationQuotaRegistry(
        operation_limits={"ask": 1},
        workspace_limit=1,
        owner_limit=1,
        maximum_leases=1,
    )
    providers = ProviderRequestRegistry(quota_registry=quotas)
    service = BlockingService()
    app = _app(service, providers)

    with TestClient(app) as stream_client, TestClient(app) as cancel_client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            active = pool.submit(
                stream_client.post,
                "/api/bridgette/stream",
                headers=_headers(FIRST_REQUEST_ID),
                data={"question": "Count bridges"},
            )
            assert service.started.wait(3)
            assert quotas.snapshot(
                workspace_id=WORKSPACE,
                owner_id="viewer@example.com",
                operation="ask",
            ).active_total == 1

            cancelled = cancel_client.post(
                f"/api/bridgette/requests/{FIRST_REQUEST_ID}/cancel",
                headers=_headers(SECOND_REQUEST_ID),
            )
            assert cancelled.status_code == 202
            stopped = active.result(timeout=3)

    assert json.loads(stopped.text.splitlines()[-1])["type"] == "stopped"
    assert quotas.snapshot(
        workspace_id=WORKSPACE,
        owner_id="viewer@example.com",
        operation="ask",
    ).active_total == 0
    assert providers.finish(FIRST_REQUEST_ID) is False


def test_http_unexpected_failure_returns_alternatives_and_releases_quota():
    quotas = OperationQuotaRegistry(
        operation_limits={"ask": 1},
        workspace_limit=1,
        owner_limit=1,
        maximum_leases=1,
    )
    providers = ProviderRequestRegistry(quota_registry=quotas)
    with TestClient(_app(FailingService(), providers)) as client:
        response = client.post(
            "/api/bridgette/stream",
            headers=_headers(FIRST_REQUEST_ID),
            data={"question": "Count bridges"},
        )

    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines()]
    assert [event["type"] for event in events] == [
        "accepted",
        "clarification",
        "done",
    ]
    decision = events[1]
    assert decision["code"] == "query_temporarily_unavailable"
    assert "could not finish this request" in decision["message"]
    assert "safely" not in decision["message"]
    assert len(decision["options"]) == 3
    assert decision["options"][0]["suggested_question"] == "Count bridges"
    assert "private provider failure" not in response.text
    assert quotas.snapshot(
        workspace_id=WORKSPACE,
        owner_id="viewer@example.com",
        operation="ask",
    ).active_total == 0


def test_provider_registry_has_an_independent_hard_entry_bound():
    quotas = OperationQuotaRegistry(
        operation_limits={"ask": 4},
        workspace_limit=4,
        owner_limit=4,
        maximum_leases=4,
    )
    providers = ProviderRequestRegistry(
        max_active_requests=1,
        quota_registry=quotas,
    )
    providers.register(
        request_id=FIRST_REQUEST_ID,
        actor_id="viewer@example.com",
        workspace_id=WORKSPACE,
    )
    with pytest.raises(QuotaExceeded) as exc:
        providers.register(
            request_id=SECOND_REQUEST_ID,
            actor_id="other@example.com",
            workspace_id=WORKSPACE,
        )
    assert exc.value.code == "bridgette_request_capacity"
    assert quotas.snapshot(
        workspace_id=WORKSPACE,
        owner_id="viewer@example.com",
        operation="ask",
    ).active_total == 1
    assert providers.finish(FIRST_REQUEST_ID) is True
    assert providers.finish(FIRST_REQUEST_ID) is False
    assert quotas.snapshot(
        workspace_id=WORKSPACE,
        owner_id="viewer@example.com",
        operation="ask",
    ).active_total == 0


def test_public_request_id_cannot_alias_a_lease_from_another_http_boundary():
    quotas = OperationQuotaRegistry(
        operation_limits={"ask": 2},
        workspace_limit=2,
        owner_limit=2,
        maximum_leases=2,
    )
    legacy_lease = quotas.acquire(
        request_id=FIRST_REQUEST_ID,
        workspace_id=WORKSPACE,
        owner_id="viewer@example.com",
        operation="ask",
    )
    providers = ProviderRequestRegistry(quota_registry=quotas)
    providers.register(
        request_id=FIRST_REQUEST_ID,
        actor_id="viewer@example.com",
        workspace_id=WORKSPACE,
    )
    assert quotas.snapshot(
        workspace_id=WORKSPACE,
        owner_id="viewer@example.com",
        operation="ask",
    ).active_total == 2

    assert providers.finish(FIRST_REQUEST_ID) is True
    assert quotas.snapshot(
        workspace_id=WORKSPACE,
        owner_id="viewer@example.com",
        operation="ask",
    ).active_total == 1
    quotas.release(legacy_lease)
