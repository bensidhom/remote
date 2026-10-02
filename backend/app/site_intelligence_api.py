"""HTTP surface for bridge-scoped site intelligence."""

from __future__ import annotations

import asyncio
import os
import re
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field

try:
    from .services.site_intelligence import (
        SECTIONS,
        load_site_layer_geometry,
        read_site_intelligence,
        refresh_site_intelligence,
    )
except ImportError:  # pragma: no cover - direct app execution
    from services.site_intelligence import SECTIONS, load_site_layer_geometry, read_site_intelligence, refresh_site_intelligence


router = APIRouter(prefix="/api/site-intelligence", tags=["site-intelligence"])
_tile_limit = asyncio.Semaphore(8)
_safe_survey = re.compile(r"^[A-Za-z0-9_-]{3,100}$")
_near_content = {"Vert", "North", "South", "East", "West"}


class RefreshRequest(BaseModel):
    year: int | None = Field(default=None, ge=1900, le=2200)
    sections: list[Literal["weather", "traffic", "water", "flood", "terrain", "constraints", "imagery"]] | None = None
    force: bool = False


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="Bridge snapshot not found.")
    if isinstance(exc, ValueError):
        message = str(exc)
        if "coordinates" in message:
            return HTTPException(status_code=422, detail="This bridge does not have valid inventory coordinates.")
        return HTTPException(status_code=400, detail=message)
    return HTTPException(status_code=500, detail="Site intelligence could not be prepared.")


@router.get("/{state_code}/{bridge_number}")
def get_site_intelligence(
    state_code: int,
    bridge_number: str,
    year: int | None = Query(default=None, ge=1900, le=2200),
):
    try:
        return read_site_intelligence(state_code, bridge_number, year)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/{state_code}/{bridge_number}/refresh")
async def refresh_bridge_site_intelligence(state_code: int, bridge_number: str, request: RefreshRequest):
    try:
        return await refresh_site_intelligence(
            state_code,
            bridge_number,
            request.year,
            request.sections,
            force=request.force,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/{state_code}/{bridge_number}/layers/{layer_name}")
async def get_bridge_site_layer(
    state_code: int,
    bridge_number: str,
    layer_name: str,
    year: int | None = Query(default=None, ge=1900, le=2200),
    force: bool = Query(default=False),
):
    """Lazy-load geometry and attributes for one user-selected public layer."""
    try:
        return await load_site_layer_geometry(
            state_code, bridge_number, layer_name, year, force=force,
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/nearmap-tiles/{survey_id}/{content}/{z}/{x}/{y}")
async def nearmap_tile(survey_id: str, content: str, z: int, x: int, y: int):
    """Credential-hiding pass-through for one selected Nearmap survey tile.

    Tiles are never persisted by BridgeGuard. Browser/private caches may retain
    them briefly according to the response header below.
    """
    api_key = os.getenv("NEARMAP_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=404, detail="Nearmap imagery is not configured.")
    if not _safe_survey.fullmatch(survey_id) or content not in _near_content:
        raise HTTPException(status_code=400, detail="Invalid Nearmap tile request.")
    if z < 0 or z > 24 or x < 0 or y < 0 or x >= 2**z or y >= 2**z:
        raise HTTPException(status_code=400, detail="Invalid tile coordinates.")
    url = f"https://api.nearmap.com/tiles/v3/surveys/{survey_id}/{content}/{z}/{x}/{y}.img"
    async with _tile_limit:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                upstream = await client.get(url, params={"apikey": api_key}, headers={"User-Agent": "BridgeGuard/1.0"})
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise HTTPException(status_code=502, detail="Nearmap tile source is temporarily unavailable.") from exc
    if upstream.status_code == 429:
        raise HTTPException(status_code=429, detail="Nearmap rate limit reached.")
    if upstream.status_code >= 400:
        raise HTTPException(status_code=upstream.status_code if upstream.status_code in {401, 403, 404} else 502, detail="Nearmap tile unavailable.")
    return Response(
        content=upstream.content,
        media_type=upstream.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/metadata/configuration/status")
def site_intelligence_configuration():
    return {
        "sections": list(SECTIONS),
        "arcgis_configured": bool(os.getenv("ARCGIS_API_TOKEN", "").strip()),
        "nearmap_configured": bool(os.getenv("NEARMAP_API_KEY", "").strip()),
        "usgs_api_key_configured": bool(os.getenv("USGS_WATER_API_KEY", "").strip()),
    }
