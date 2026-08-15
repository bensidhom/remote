"""Authenticated, request-lifetime quotas for expensive HTTP operations.

The application currently supports one worker per isolated customer.  This
middleware provides a single server-owned mapping from HTTP routes to the
process-wide operation quota registry.  It deliberately does not inspect or
trust a client-supplied workspace/actor and always releases the lease in a
``finally`` block, including transport cancellation and unhandled failures.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Final
from uuid import UUID, uuid4

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from .quotas import (
    DEFAULT_OPERATION_QUOTAS,
    OperationQuotaRegistry,
    QuotaExceeded,
)


_REVISION_SNAPSHOT_RE: Final = re.compile(
    r"^/api/revisions/snapshots/[^/]+/[^/]+/[^/]+$"
)


def operation_for_http_request(method: str, path: str) -> str | None:
    """Return the reviewed quota class for one expensive route."""

    verb = str(method or "").upper()
    route = (str(path or "").rstrip("/") or "/")
    if verb == "POST" and route in {
        "/api/convert",
        "/api/convert-v2",
        "/api/convert-v2/inspection-history",
        "/api/reverse-convert",
        "/api/imports",
    }:
        return "conversion"
    if verb == "POST" and route in {
        "/api/inspection-reports/validate",
        "/api/revisions/stage",
        "/api/revisions/stage-upload",
    }:
        return "validation"
    if verb == "POST" and route.startswith("/api/imports/") and route.endswith("/commit"):
        return "validation"
    if verb == "PATCH" and _REVISION_SNAPSHOT_RE.fullmatch(route):
        return "validation"
    if verb == "GET" and (
        route.startswith("/api/validate-bridge/")
        or route.startswith("/api/validate-bridges/")
    ):
        return "validation"
    return None


def _canonical_or_new_request_id(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return str(uuid4())
    try:
        parsed = UUID(raw)
    except (ValueError, AttributeError) as exc:
        raise ValueError("X-Request-ID must be a canonical UUID") from exc
    if str(parsed) != raw:
        raise ValueError("X-Request-ID must be a canonical UUID")
    return raw


@dataclass(frozen=True, slots=True)
class HttpQuotaIdentity:
    request_id: str
    workspace_id: str
    owner_id: str
    operation: str


class OperationQuotaMiddleware(BaseHTTPMiddleware):
    """Hold one quota lease for an expensive HTTP request's full lifetime.

    ``AuthMiddleware`` must wrap this middleware so ``request.state.user`` is
    already server-authenticated.  A missing identity fails closed instead of
    silently bypassing limits.
    """

    def __init__(
        self,
        app,
        *,
        workspace_id: str,
        registry: OperationQuotaRegistry = DEFAULT_OPERATION_QUOTAS,
    ) -> None:
        super().__init__(app)
        workspace = str(workspace_id or "").strip()
        if not workspace:
            raise ValueError("workspace_id is required")
        self._workspace_id = workspace
        self._registry = registry

    async def dispatch(self, request: Request, call_next):
        operation = operation_for_http_request(request.method, request.url.path)
        if operation is None:
            return await call_next(request)

        user = getattr(request.state, "user", None)
        owner = str(getattr(user, "email", "") or "").strip().lower()
        if not owner:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": {
                        "code": "quota_identity_unavailable",
                        "message": "Authenticated operation identity is unavailable.",
                    }
                },
            )
        try:
            request_id = _canonical_or_new_request_id(
                request.headers.get("X-Request-ID")
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": {
                        "code": "invalid_request_id",
                        "message": str(exc),
                    }
                },
            )

        identity = HttpQuotaIdentity(
            request_id=request_id,
            workspace_id=self._workspace_id,
            owner_id=owner,
            operation=operation,
        )
        try:
            lease = self._registry.acquire(
                request_id=f"http:{operation}:{request_id}",
                workspace_id=identity.workspace_id,
                owner_id=identity.owner_id,
                operation=identity.operation,
            )
        except QuotaExceeded as exc:
            return JSONResponse(
                status_code=429,
                content={
                    "detail": {
                        "code": exc.code,
                        "message": "This operation is at its concurrency limit.",
                    }
                },
                headers={
                    "Retry-After": str(exc.retry_after_seconds),
                    "X-Request-ID": request_id,
                },
            )

        try:
            response = await call_next(request)
            response.headers.setdefault("X-Request-ID", request_id)
            return response
        finally:
            self._registry.release(lease)


__all__ = [
    "HttpQuotaIdentity",
    "OperationQuotaMiddleware",
    "operation_for_http_request",
]
