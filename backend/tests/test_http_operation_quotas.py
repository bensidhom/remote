from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from app.services.operations.http_quotas import (
    OperationQuotaMiddleware,
    operation_for_http_request,
)
from app.services.operations.quotas import OperationQuotaRegistry


class _IdentityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.user = SimpleNamespace(email="editor@example.com")
        return await call_next(request)


def _app(
    registry: OperationQuotaRegistry,
    *,
    entered: threading.Event | None = None,
    release: threading.Event | None = None,
) -> FastAPI:
    app = FastAPI()

    @app.post("/api/convert-v2")
    async def convert():
        if entered is not None:
            entered.set()
        if release is not None:
            await asyncio.to_thread(release.wait, 5)
        return {"ok": True}

    @app.get("/api/map-data")
    async def map_data():
        return {"ok": True}

    @app.get("/api/validate-bridge/34/1")
    async def fail_validation():
        raise RuntimeError("internal failure")

    app.add_middleware(
        OperationQuotaMiddleware,
        workspace_id="workspace-a",
        registry=registry,
    )
    # Middleware added last is outermost; it simulates the real AuthMiddleware.
    app.add_middleware(_IdentityMiddleware)
    return app


def _registry() -> OperationQuotaRegistry:
    return OperationQuotaRegistry(
        operation_limits={"conversion": 1, "validation": 1},
        workspace_limit=2,
        owner_limit=2,
        maximum_leases=4,
    )


def test_expensive_route_classification_is_exact():
    assert operation_for_http_request("POST", "/api/convert-v2") == "conversion"
    assert (
        operation_for_http_request("POST", "/api/convert-v2/inspection-history")
        == "conversion"
    )
    assert operation_for_http_request("POST", "/api/imports") == "conversion"
    assert operation_for_http_request("POST", "/api/revisions/stage-upload") == "validation"
    assert operation_for_http_request(
        "POST", "/api/imports/7f7f0a00-0000-4000-8000-000000000001/commit"
    ) == "validation"
    assert operation_for_http_request(
        "PATCH", "/api/revisions/snapshots/34/0001/2026"
    ) == "validation"
    assert operation_for_http_request("GET", "/api/validate-bridge/34/0001") == "validation"
    assert operation_for_http_request("GET", "/api/map-data") is None
    assert operation_for_http_request("POST", "/api/point-cloud/preview") is None


def test_concurrent_conversion_returns_typed_429_and_releases_capacity():
    registry = _registry()
    entered = threading.Event()
    release = threading.Event()
    app = _app(registry, entered=entered, release=release)

    with TestClient(app) as first_client, TestClient(app) as second_client:
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(
                first_client.post,
                "/api/convert-v2",
                headers={"X-Request-ID": str(uuid4())},
            )
            assert entered.wait(3)
            rejected = second_client.post(
                "/api/convert-v2",
                headers={"X-Request-ID": str(uuid4())},
            )
            assert rejected.status_code == 429
            assert rejected.headers["retry-after"] == "1"
            assert rejected.json()["detail"]["code"] == "conversion_concurrency_limit"
            release.set()
            assert first.result(timeout=5).status_code == 200

        reused = second_client.post(
            "/api/convert-v2", headers={"X-Request-ID": str(uuid4())}
        )
        assert reused.status_code == 200
    assert registry.snapshot(
        workspace_id="workspace-a",
        owner_id="editor@example.com",
        operation="conversion",
    ).active_total == 0


def test_invalid_request_id_is_rejected_before_work_and_read_route_is_ungated():
    registry = _registry()
    with TestClient(_app(registry)) as client:
        bad = client.post("/api/convert-v2", headers={"X-Request-ID": "not-a-uuid"})
        assert bad.status_code == 400
        assert bad.json()["detail"]["code"] == "invalid_request_id"
        assert client.get("/api/map-data", headers={"X-Request-ID": "not-a-uuid"}).status_code == 200


def test_unhandled_validation_failure_still_releases_lease():
    registry = _registry()
    app = _app(registry)
    with TestClient(app, raise_server_exceptions=False) as client:
        failed = client.get(
            "/api/validate-bridge/34/1",
            headers={"X-Request-ID": str(uuid4())},
        )
        assert failed.status_code == 500
    assert registry.snapshot(
        workspace_id="workspace-a",
        owner_id="editor@example.com",
        operation="validation",
    ).active_total == 0
