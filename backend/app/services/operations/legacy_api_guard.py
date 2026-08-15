"""Fail-closed boundary for retired projection-mutation APIs.

BridgeGuard keeps a few pre-revision endpoints available in research mode so
historical demos and local tools do not break abruptly.  Those endpoints must
not be reachable in a dedicated/commercial data plane: several write directly
to relational projections and therefore bypass canonical revision validation,
optimistic concurrency, provenance, and mutation audit guarantees.

The guard is intentionally an exact method/path allow-deny classifier.  It does
not use broad prefixes that could accidentally disable read-only projections or
the canonical revision, export, report, and point-cloud workflows.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


@dataclass(frozen=True)
class RetiredEndpoint:
    """Public migration guidance for one disabled legacy operation."""

    operation: str
    replacement: str


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def commercial_legacy_guard_enabled(
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether the dedicated/commercial API boundary is active.

    Dedicated mode gets the same surface even in a non-production rehearsal,
    preventing a deployment from passing tests with APIs that disappear only
    after launch.  The explicit commercial readiness gate also fails closed if
    it is enabled before the deployment mode is evaluated by startup policy.
    """

    env = os.environ if environ is None else environ
    mode = str(env.get("BRIDGEGUARD_DEPLOYMENT_MODE", "research_shared")).strip().lower()
    return mode == "dedicated_customer" or _truthy(
        env.get("BRIDGEGUARD_REQUIRE_COMMERCIAL_READY")
    )


def retired_endpoint_for_request(method: str, path: str) -> RetiredEndpoint | None:
    """Classify an exact retired endpoint without blocking safe projections."""

    method = method.upper().strip()
    path = path.rstrip("/") or "/"
    parts = [part for part in path.split("/") if part]

    # The v1 analytics endpoints read mutable projection rows and, for forecast
    # requests, can rebuild a separate legacy database during a request.  They
    # remain available only for the documented research compatibility window.
    # Dedicated/commercial deployments must use the publication-scoped v2
    # forecast/LCCA/KPI services.
    if method == "GET" and path in {"/api/kpis", "/api/kpis/county"}:
        return RetiredEndpoint(
            operation="legacy_raw_kpi_projection",
            replacement="GET /api/analytics/kpis/operational",
        )
    if path.startswith("/api/forecasts/") and not path.startswith(
        "/api/forecasts/v2/"
    ):
        return RetiredEndpoint(
            operation="legacy_request_owned_forecast",
            replacement="/api/forecast-jobs and /api/forecasts/v2/bridge",
        )
    if (
        method == "GET"
        and len(parts) >= 4
        and parts[:3] == ["api", "lcca", "bridge"]
    ):
        return RetiredEndpoint(
            operation="legacy_raw_lcca_projection",
            replacement="POST /api/lcca/v2/bridge",
        )

    if method == "POST" and path == "/api/convert":
        return RetiredEndpoint(
            operation="legacy_conversion",
            replacement="POST /api/convert-v2",
        )
    if method == "POST" and path == "/api/load-json":
        return RetiredEndpoint(
            operation="legacy_load_adapter",
            replacement="POST /api/revisions/stage-upload",
        )
    if method == "POST" and path == "/api/inspection-reports/apply":
        return RetiredEndpoint(
            operation="legacy_inspection_projection_apply",
            replacement=(
                "PATCH /api/revisions/snapshots/{state_code}/{bridge_number}/{data_year}"
            ),
        )
    if method == "DELETE" and path == "/api/clear-data":
        return RetiredEndpoint(
            operation="raw_projection_clear",
            replacement="reviewed canonical revision retention/archive workflow",
        )

    # The current custom-table helpers do not yet write the common mutation
    # audit or advance workspace data_generation as required by Workstream 8.
    if method == "POST" and path == "/api/custom-tables":
        return RetiredEndpoint(
            operation="legacy_custom_table_create",
            replacement="audited workspace-scoped custom-table repository",
        )
    if (
        method == "POST"
        and len(parts) == 4
        and parts[:2] == ["api", "custom-tables"]
        and parts[-1] == "rows"
    ):
        return RetiredEndpoint(
            operation="legacy_custom_table_row_create",
            replacement="audited workspace-scoped custom-table repository",
        )
    if (
        method in {"PUT", "DELETE"}
        and len(parts) == 4
        and parts[:3] == ["api", "custom-tables", "rows"]
    ):
        return RetiredEndpoint(
            operation=(
                "legacy_custom_table_row_update"
                if method == "PUT"
                else "legacy_custom_table_row_delete"
            ),
            replacement="audited workspace-scoped custom-table repository",
        )
    if (
        method == "DELETE"
        and len(parts) == 3
        and parts[:2] == ["api", "custom-tables"]
    ):
        return RetiredEndpoint(
            operation="legacy_custom_table_delete",
            replacement="audited workspace-scoped custom-table repository",
        )

    # Raw table administration writes bypass the canonical snapshot and can
    # leave its relational projection inconsistent with the published JSON.
    if (
        method == "POST"
        and len(parts) == 4
        and parts[:2] == ["api", "table"]
        and parts[-1] == "row"
    ):
        return RetiredEndpoint(
            operation="raw_table_insert",
            replacement=(
                "PATCH /api/revisions/snapshots/{state_code}/{bridge_number}/{data_year}"
            ),
        )
    if (
        method in {"PUT", "DELETE"}
        and len(parts) == 5
        and parts[:2] == ["api", "table"]
        and parts[-2] == "row"
    ):
        return RetiredEndpoint(
            operation=(
                "raw_table_update" if method == "PUT" else "raw_table_delete"
            ),
            replacement=(
                "PATCH /api/revisions/snapshots/{state_code}/{bridge_number}/{data_year}"
            ),
        )

    if (
        method == "PUT"
        and len(parts) == 4
        and parts[:2] == ["api", "bridge"]
    ):
        return RetiredEndpoint(
            operation="legacy_bridge_update",
            replacement=(
                "PATCH /api/revisions/snapshots/{state_code}/{bridge_number}/{data_year}"
            ),
        )
    if method == "DELETE" and path == "/api/bridges/by-year":
        return RetiredEndpoint(
            operation="legacy_year_delete",
            replacement="reviewed canonical revision retention/archive workflow",
        )
    if (
        method == "DELETE"
        and len(parts) == 6
        and parts[:2] == ["api", "bridges"]
        and parts[-2] == "year"
    ):
        return RetiredEndpoint(
            operation="legacy_snapshot_delete",
            replacement="reviewed canonical revision retention/archive workflow",
        )
    return None


class LegacyApiGuardMiddleware(BaseHTTPMiddleware):
    """Return a typed 410 before a retired commercial operation can execute."""

    def __init__(
        self,
        app,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(app)
        self._environ = environ

    async def dispatch(self, request: Request, call_next):
        retired = retired_endpoint_for_request(request.method, request.url.path)
        if retired is None or not commercial_legacy_guard_enabled(self._environ):
            return await call_next(request)
        return JSONResponse(
            status_code=410,
            headers={"Cache-Control": "no-store"},
            content={
                "detail": {
                    "code": "legacy_endpoint_disabled",
                    "message": (
                        "This legacy operation is disabled in dedicated/commercial "
                        "deployments because it bypasses the canonical revision boundary."
                    ),
                    "operation": retired.operation,
                    "replacement": retired.replacement,
                }
            },
        )


__all__ = [
    "LegacyApiGuardMiddleware",
    "RetiredEndpoint",
    "commercial_legacy_guard_enabled",
    "retired_endpoint_for_request",
]
