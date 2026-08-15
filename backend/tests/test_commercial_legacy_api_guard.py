from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.services.operations.legacy_api_guard import (
    LegacyApiGuardMiddleware,
    commercial_legacy_guard_enabled,
    retired_endpoint_for_request,
)


RETIRED = (
    ("GET", "/api/kpis", "legacy_raw_kpi_projection"),
    ("GET", "/api/kpis/county", "legacy_raw_kpi_projection"),
    ("POST", "/api/forecasts/build-training-db", "legacy_request_owned_forecast"),
    ("POST", "/api/forecasts/train", "legacy_request_owned_forecast"),
    ("POST", "/api/forecasts/train-bridge/34/0000001", "legacy_request_owned_forecast"),
    ("GET", "/api/forecasts/summary", "legacy_request_owned_forecast"),
    ("GET", "/api/forecasts/bridge/34/0000001", "legacy_request_owned_forecast"),
    ("GET", "/api/forecasts/bridge/34/0000001/simulate", "legacy_request_owned_forecast"),
    ("GET", "/api/lcca/bridge/34/0000001", "legacy_raw_lcca_projection"),
    ("POST", "/api/convert", "legacy_conversion"),
    ("POST", "/api/load-json", "legacy_load_adapter"),
    (
        "POST",
        "/api/inspection-reports/apply",
        "legacy_inspection_projection_apply",
    ),
    ("POST", "/api/table/bridges/row", "raw_table_insert"),
    ("PUT", "/api/table/bridges/row/7", "raw_table_update"),
    ("DELETE", "/api/table/bridges/row/7", "raw_table_delete"),
    ("DELETE", "/api/clear-data", "raw_projection_clear"),
    ("POST", "/api/custom-tables", "legacy_custom_table_create"),
    (
        "POST",
        "/api/custom-tables/12/rows",
        "legacy_custom_table_row_create",
    ),
    (
        "PUT",
        "/api/custom-tables/rows/7",
        "legacy_custom_table_row_update",
    ),
    (
        "DELETE",
        "/api/custom-tables/rows/7",
        "legacy_custom_table_row_delete",
    ),
    ("DELETE", "/api/custom-tables/12", "legacy_custom_table_delete"),
    ("PUT", "/api/bridge/34/0000001", "legacy_bridge_update"),
    ("DELETE", "/api/bridges/by-year", "legacy_year_delete"),
    (
        "DELETE",
        "/api/bridges/34/0000001/year/2026",
        "legacy_snapshot_delete",
    ),
)


SAFE = (
    ("GET", "/api/analytics/kpis/operational"),
    ("GET", "/api/forecasts/v2/summary"),
    ("POST", "/api/forecasts/v2/bridge"),
    ("POST", "/api/lcca/v2/bridge"),
    ("GET", "/api/table/bridges"),
    ("GET", "/api/table-rows/bridges"),
    ("GET", "/api/bridge/34/0000001"),
    ("GET", "/api/export-bridges/34"),
    ("POST", "/api/convert-v2"),
    ("POST", "/api/reverse-convert"),
    ("POST", "/api/revisions/stage-upload"),
    ("POST", "/api/revisions/stage"),
    ("PATCH", "/api/revisions/snapshots/34/0000001/2026"),
    ("POST", "/api/revisions/revision-id/publish"),
    ("POST", "/api/revisions/revision-id/exports"),
    ("POST", "/api/inspection-reports/validate"),
    ("GET", "/api/bridges/34/0000001/custom-tables"),
    ("POST", "/api/bridges/34/0000001/reports"),
    ("DELETE", "/api/bridges/34/0000001/reports/report-id"),
    ("POST", "/api/bridges/34/0000001/point-cloud"),
    ("DELETE", "/api/bridges/34/0000001/point-cloud"),
)


def _app(environ: dict[str, str]) -> FastAPI:
    app = FastAPI()

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def reachable(request: Request, path: str):
        return {"executed": True, "method": request.method, "path": f"/{path}"}

    app.add_middleware(LegacyApiGuardMiddleware, environ=environ)
    return app


@pytest.mark.parametrize(("method", "path", "operation"), RETIRED)
def test_retired_route_classifier_is_exact(method: str, path: str, operation: str):
    retired = retired_endpoint_for_request(method, path)
    assert retired is not None
    assert retired.operation == operation
    # A trailing slash must not create a bypass before framework redirecting.
    assert retired_endpoint_for_request(method, f"{path}/") == retired


@pytest.mark.parametrize(("method", "path"), SAFE)
def test_canonical_and_read_only_routes_are_not_classified(method: str, path: str):
    assert retired_endpoint_for_request(method, path) is None


@pytest.mark.parametrize(
    ("environ", "expected"),
    (
        ({}, False),
        ({"BRIDGEGUARD_ENV": "production"}, False),
        ({"BRIDGEGUARD_DEPLOYMENT_MODE": "research_shared"}, False),
        ({"BRIDGEGUARD_DEPLOYMENT_MODE": "dedicated_customer"}, True),
        ({"BRIDGEGUARD_REQUIRE_COMMERCIAL_READY": "true"}, True),
        (
            {
                "BRIDGEGUARD_DEPLOYMENT_MODE": "dedicated_customer",
                "BRIDGEGUARD_REQUIRE_COMMERCIAL_READY": "false",
            },
            True,
        ),
    ),
)
def test_guard_activation_follows_server_owned_deployment_mode(environ, expected):
    assert commercial_legacy_guard_enabled(environ) is expected


@pytest.mark.parametrize(("method", "path", "operation"), RETIRED)
def test_dedicated_mode_rejects_legacy_operation_before_route_execution(
    method: str,
    path: str,
    operation: str,
):
    with TestClient(
        _app({"BRIDGEGUARD_DEPLOYMENT_MODE": "dedicated_customer"})
    ) as client:
        response = client.request(method, path)

    assert response.status_code == 410
    assert response.headers["cache-control"] == "no-store"
    detail = response.json()["detail"]
    assert detail["code"] == "legacy_endpoint_disabled"
    assert detail["operation"] == operation
    assert detail["replacement"]
    assert "executed" not in response.text


@pytest.mark.parametrize(("method", "path"), SAFE)
def test_dedicated_mode_preserves_canonical_and_safe_projection_routes(
    method: str,
    path: str,
):
    with TestClient(
        _app({"BRIDGEGUARD_REQUIRE_COMMERCIAL_READY": "yes"})
    ) as client:
        response = client.request(method, path)

    assert response.status_code == 200
    assert response.json()["executed"] is True


@pytest.mark.parametrize(("method", "path", "operation"), RETIRED)
def test_research_mode_preserves_time_limited_legacy_compatibility(
    method: str,
    path: str,
    operation: str,
):
    del operation
    with TestClient(
        _app({"BRIDGEGUARD_DEPLOYMENT_MODE": "research_shared"})
    ) as client:
        response = client.request(method, path)

    assert response.status_code == 200
    assert response.json()["executed"] is True


def test_main_application_registers_guard_inside_authentication_boundary():
    from app.auth import AuthMiddleware
    from app.main import app

    middleware = [item.cls for item in app.user_middleware]
    assert LegacyApiGuardMiddleware in middleware
    assert AuthMiddleware in middleware
    # Last-added middleware is outermost in Starlette.  Authentication must run
    # first so anonymous callers cannot probe which compatibility APIs exist.
    assert middleware.index(AuthMiddleware) < middleware.index(LegacyApiGuardMiddleware)
