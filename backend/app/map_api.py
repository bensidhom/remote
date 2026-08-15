"""Authenticated HTTP adapter for deterministic map bridge resolution."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.revision_api import SHARED_WORKSPACE_ID
from app.services.map_resolver import MapResolutionError, resolve_bridge


SERVER_STATE_CODE = 34


class ResolveBridgeBody(BaseModel):
    """The client supplies only a query and optional explicit inventory year."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=512)
    year: int | None = Field(default=None, ge=1900, le=2100)


_database_path_provider: Callable[[], str] | None = None
router = APIRouter(prefix="/api/map", tags=["map"])


def configure_database_path(provider: Callable[[], str] | None) -> None:
    global _database_path_provider
    _database_path_provider = provider


def _database_path() -> str:
    if _database_path_provider is not None:
        return str(_database_path_provider())
    try:
        from app import main as app_main

        return str(app_main.USER_DB)
    except (AttributeError, ImportError):
        from app.routers.services.datapaths import USER_DB

        return str(USER_DB)


def _safe_error(error: MapResolutionError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={"status": "error", "code": error.code, "detail": str(error)},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/resolve-bridge")
async def resolve_bridge_route(body: ResolveBridgeBody, request: Request):
    user = getattr(request.state, "user", None)
    if not str(getattr(user, "email", "")).strip():
        return JSONResponse(
            status_code=401,
            content={"status": "error", "code": "authentication_required", "detail": "Authentication required."},
            headers={"Cache-Control": "no-store"},
        )
    try:
        result: dict[str, Any] = await run_in_threadpool(
            resolve_bridge,
            _database_path(),
            workspace_id=SHARED_WORKSPACE_ID,
            state_code=SERVER_STATE_CODE,
            query=body.query,
            year=body.year,
        )
        return JSONResponse(
            status_code=200,
            content=result,
            headers={
                "Cache-Control": "private, no-store",
                "X-BridgeGuard-Data-Version": str(result["data_generation"]),
            },
        )
    except MapResolutionError as exc:
        return _safe_error(exc)


__all__ = [
    "ResolveBridgeBody",
    "SERVER_STATE_CODE",
    "configure_database_path",
    "router",
]
