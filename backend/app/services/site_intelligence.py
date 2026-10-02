"""Bridge-scoped, refreshable site-intelligence adapters.

The authoritative SNBI database is read-only here. External responses are kept
in a separate cache so adding or refreshing site context never changes bridge
inventory data. Providers fail independently: one unavailable source does not
hide the other sections or the cached response.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

try:
    from ..routers.services.datapaths import SITE_INTELLIGENCE_CACHE_DB, USER_DB
except ImportError:  # pragma: no cover - direct app execution
    from routers.services.datapaths import SITE_INTELLIGENCE_CACHE_DB, USER_DB


SECTIONS = ("weather", "traffic", "water", "flood", "terrain", "constraints", "imagery")
TTLS = {
    "weather": 15 * 60,
    "traffic": 5 * 60,
    "water": 15 * 60,
    "flood": 30 * 24 * 60 * 60,
    "terrain": 180 * 24 * 60 * 60,
    "constraints": 30 * 24 * 60 * 60,
    "imagery": 24 * 60 * 60,
}
STALE_TTLS = {name: max(ttl * 4, 24 * 60 * 60) for name, ttl in TTLS.items()}
MANUAL_REFRESH_COOLDOWN_SECONDS = 60
USER_AGENT = os.getenv("SITE_INTELLIGENCE_USER_AGENT", "BridgeGuard/1.0 site-intelligence")
PROVIDER_INFO = {
    "weather": ("National Weather Service", "https://www.weather.gov/"),
    "traffic": ("NJDOT Traffic Counts / ArcGIS World Traffic", "https://data.nj.gov/Transportation/Traffic-Counts-Data/c74r-6c8d"),
    "water": ("USGS Water Data / NOAA NWPS / NOAA CO-OPS", "https://water.noaa.gov/"),
    "flood": ("FEMA National Flood Hazard Layer", "https://msc.fema.gov/portal/home"),
    "terrain": ("New Jersey 10-foot Digital Elevation Model", "https://njgin.nj.gov/"),
    "constraints": ("FWS / NJDEP / NJGIN / USACE", "https://dep.nj.gov/gis/nj-geoweb-profiles/"),
    "imagery": ("NJGIN / Nearmap", "https://www.nj.gov/njgin/edata/imagery/"),
}

_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()
_provider_limits = {name: asyncio.Semaphore(3 if name in {"weather", "traffic"} else 2) for name in SECTIONS}


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


def _utc_iso(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(epoch or time.time(), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _open_ro(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _init_cache() -> None:
    Path(SITE_INTELLIGENCE_CACHE_DB).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(SITE_INTELLIGENCE_CACHE_DB)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS site_intelligence_cache (
                bridge_key TEXT NOT NULL,
                coordinate_hash TEXT NOT NULL,
                section TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                source_observed_at TEXT,
                fetched_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                stale_until REAL NOT NULL,
                status TEXT NOT NULL,
                error_code TEXT,
                PRIMARY KEY (bridge_key, coordinate_hash, section)
            )
            """
        )
        connection.commit()


def bridge_context(state_code: int, bridge_number: str, year: int | None = None) -> dict[str, Any]:
    """Resolve the canonical bridge snapshot and water-crossing context."""
    with closing(_open_ro(USER_DB)) as connection:
        params: list[Any] = [int(state_code), str(bridge_number)]
        year_clause = ""
        if year is not None:
            year_clause = "AND b.data_year = ?"
            params.append(int(year))
        row = connection.execute(
            f"""
            SELECT b.state_code, b.bridge_number, b.data_year, b.bridge_name,
                   b.bridge_location, b.latitude, b.longitude, b.year_built,
                   b.owner, b.maintenance_responsibility,
                   b.deck_condition_rating, b.superstructure_condition_rating,
                   b.substructure_condition_rating, b.channel_condition_rating,
                   b.scour_condition_rating, b.scour_vulnerability,
                   b.scour_plan_of_action, b.overtopping_likelihood,
                   b.calculated_deck_area
              FROM bridges b
             WHERE b.state_code = ? AND b.bridge_number = ? {year_clause}
             ORDER BY b.data_year DESC LIMIT 1
            """,
            params,
        ).fetchone()
        if not row:
            raise KeyError("bridge_not_found")
        context = dict(row)
        features = connection.execute(
            """
            SELECT feature_type, feature_location, feature_name, navigable_waterway
              FROM features
             WHERE state_code=? AND bridge_number=? AND data_year=?
             ORDER BY feature_type
            """,
            (int(row["state_code"]), str(row["bridge_number"]), int(row["data_year"])),
        ).fetchall()

    try:
        latitude = float(context["latitude"])
        longitude = float(context["longitude"])
    except (TypeError, ValueError) as exc:
        raise ValueError("bridge_coordinates_missing") from exc
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("bridge_coordinates_invalid")

    feature_rows = [dict(item) for item in features]
    water_features = [item for item in feature_rows if str(item.get("feature_type") or "").upper().startswith("W")]
    context.update(
        latitude=latitude,
        longitude=longitude,
        features=feature_rows,
        water_crossing=bool(water_features),
        waterway_names=[item["feature_name"] for item in water_features if item.get("feature_name")],
    )
    context["bridge_key"] = f'{context["state_code"]}:{context["bridge_number"]}:{context["data_year"]}'
    context["coordinate_hash"] = hashlib.sha256(f"{latitude:.6f},{longitude:.6f}".encode()).hexdigest()[:16]
    return context


def _cached_rows(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    _init_cache()
    with closing(sqlite3.connect(SITE_INTELLIGENCE_CACHE_DB)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM site_intelligence_cache WHERE bridge_key=? AND coordinate_hash=?",
            (context["bridge_key"], context["coordinate_hash"]),
        ).fetchall()
    now = time.time()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except Exception:
            continue
        payload.update(
            fetched_at=_utc_iso(float(row["fetched_at"])),
            expires_at=_utc_iso(float(row["expires_at"])),
            source_observed_at=row["source_observed_at"],
            stale=now > float(row["expires_at"]),
            cache_state="fresh" if now <= float(row["expires_at"]) else "stale",
        )
        result[str(row["section"])] = payload
    return result


def _response(context: dict[str, Any], sections: dict[str, dict[str, Any]]) -> dict[str, Any]:
    public_context = {key: value for key, value in context.items() if key not in {"features", "coordinate_hash"}}
    return {
        "bridge": public_context,
        "sections": {name: sections.get(name, {"status": "missing", "cache_state": "missing"}) for name in SECTIONS},
        "generated_at": _utc_iso(),
        "refresh_policy": "Data is requested only while this bridge's Site Intelligence tab is open.",
    }


def read_site_intelligence(state_code: int, bridge_number: str, year: int | None = None) -> dict[str, Any]:
    context = bridge_context(state_code, bridge_number, year)
    return _response(context, _cached_rows(context))


async def _json_value(client: httpx.AsyncClient, url: str, **kwargs: Any) -> Any:
    try:
        response = await client.get(url, **kwargs)
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise ProviderError("source_unreachable", str(exc)) from exc
    if response.status_code == 429:
        value = response.headers.get("retry-after", "")
        raise ProviderError("rate_limited", "The source rate limit was reached.", retry_after=int(value) if value.isdigit() else None)
    if response.status_code in {401, 403}:
        raise ProviderError("source_not_authorized", "The configured source credential was not accepted.")
    if response.status_code >= 400:
        raise ProviderError("source_http_error", f"Source returned HTTP {response.status_code}.")
    try:
        value = response.json()
    except ValueError as exc:
        raise ProviderError("invalid_source_response", "The source did not return JSON.") from exc
    if isinstance(value, dict) and value.get("error"):
        raise ProviderError("source_error", str(value.get("error")))
    return value


async def _json(client: httpx.AsyncClient, url: str, **kwargs: Any) -> dict[str, Any]:
    value = await _json_value(client, url, **kwargs)
    if not isinstance(value, dict):
        raise ProviderError("invalid_source_response", "The source returned an unexpected response.")
    return value


async def _json_list(client: httpx.AsyncClient, url: str, **kwargs: Any) -> list[dict[str, Any]]:
    value = await _json_value(client, url, **kwargs)
    if not isinstance(value, list):
        raise ProviderError("invalid_source_response", "The source returned an unexpected response.")
    return [item for item in value if isinstance(item, dict)]


def _distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


async def _weather(client: httpx.AsyncClient, context: dict[str, Any]) -> dict[str, Any]:
    lat, lon = context["latitude"], context["longitude"]
    points = await _json(client, f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")
    props = points.get("properties") or {}
    hourly_url = props.get("forecastHourly")
    alerts_url = "https://api.weather.gov/alerts/active"
    requests: list[Awaitable[dict[str, Any]]] = []
    if hourly_url:
        requests.append(_json(client, str(hourly_url)))
    requests.append(_json(client, alerts_url, params={"point": f"{lat:.4f},{lon:.4f}", "status": "actual"}))
    values = await asyncio.gather(*requests, return_exceptions=True)
    hourly = values[0] if hourly_url and values and isinstance(values[0], dict) else {}
    alert_value = values[-1] if values and isinstance(values[-1], dict) else {}
    periods = (hourly.get("properties") or {}).get("periods") or []
    alerts = []
    for feature in alert_value.get("features") or []:
        item = feature.get("properties") or {}
        alerts.append({key: item.get(key) for key in ("event", "severity", "urgency", "headline", "effective", "expires")})
    return {
        "status": "available",
        "provider": "National Weather Service",
        "source_url": f"https://forecast.weather.gov/MapClick.php?lat={lat:.4f}&lon={lon:.4f}",
        "current": periods[0] if periods else None,
        "hourly": periods[:12],
        "alerts": alerts[:20],
        "forecast_office": props.get("cwa"),
        "grid": {"x": props.get("gridX"), "y": props.get("gridY")},
    }


async def _traffic(client: httpx.AsyncClient, context: dict[str, Any]) -> dict[str, Any]:
    service = "https://traffic.arcgis.com/arcgis/rest/services/World/Traffic/MapServer"
    token = os.getenv("ARCGIS_API_TOKEN", "").strip()
    lat, lon = context["latitude"], context["longitude"]
    traffic_counts: list[dict[str, Any]] = []
    count_error: str | None = None
    try:
        raw_counts = await _json_list(
            client,
            "https://data.nj.gov/resource/c74r-6c8d.json",
            params={
                "$select": "si_station_num,si_sri,si_mp,si_lat,si_lon,county_descr,muni,ev_aadt_tot,ev_aadt_dir1,ev_aadt_dir2,count_year,station_desc,direction",
                "$where": (
                    f"si_lat between {lat - 0.15:.6f} and {lat + 0.15:.6f} "
                    f"and si_lon between {lon - 0.20:.6f} and {lon + 0.20:.6f} "
                    "and ev_aadt_tot is not null"
                ),
                "$order": "count_year DESC",
                "$limit": "500",
            },
        )
        by_station: dict[str, dict[str, Any]] = {}
        for item in raw_counts:
            station_id = str(item.get("si_station_num") or "").strip()
            if not station_id or station_id in by_station:
                continue
            try:
                station_lat = float(item["si_lat"])
                station_lon = float(item["si_lon"])
            except (KeyError, TypeError, ValueError):
                continue
            normalized = dict(item)
            normalized["latitude"] = station_lat
            normalized["longitude"] = station_lon
            normalized["distance_miles"] = _distance_miles(lat, lon, station_lat, station_lon)
            try:
                normalized["aadt"] = int(float(str(item.get("ev_aadt_tot") or "")))
            except ValueError:
                normalized["aadt"] = None
            by_station[station_id] = normalized
        traffic_counts = sorted(by_station.values(), key=lambda item: item["distance_miles"])[:20]
    except ProviderError as exc:
        count_error = exc.code

    result: dict[str, Any] = {
        "status": "available" if traffic_counts or token else "unavailable",
        "provider": "NJDOT Traffic Counts / ArcGIS World Traffic",
        "source_url": "https://data.nj.gov/Transportation/Traffic-Counts-Data/c74r-6c8d",
        "map_service_url": service,
        "map_available": bool(token),
        "traffic_counts": traffic_counts,
        "nearest_count": traffic_counts[0] if traffic_counts else None,
        "traffic_counts_error": count_error,
        "camera_map_url": "https://www.511nj.org/",
        "camera_note": "Open the official 511NJ map for current camera images; BridgeGuard does not request or store recorded footage.",
        "incidents": [],
        "message": None if token else "Public NJDOT traffic counts are shown when available. Add an ArcGIS access token only for live flow and incident layers.",
    }
    if not token:
        return result
    params = {
        "f": "json", "token": token, "where": "1=1", "outFields": "*",
        "geometry": f'{context["longitude"]},{context["latitude"]}', "geometryType": "esriGeometryPoint",
        "inSR": "4326", "spatialRel": "esriSpatialRelIntersects", "distance": "5",
        "units": "esriSRUnit_StatuteMile", "returnGeometry": "true", "resultRecordCount": "50",
    }
    # Incident layer availability varies by ArcGIS subscription. Traffic flow
    # remains usable even when the incident query is not licensed.
    try:
        data = await _json(client, f"{service}/0/query", params=params)
        result["incidents"] = [feature.get("attributes") or {} for feature in data.get("features") or []]
    except ProviderError as exc:
        result["incident_error"] = exc.code
    return result


async def _water(client: httpx.AsyncClient, context: dict[str, Any]) -> dict[str, Any]:
    if not context["water_crossing"]:
        return {
            "status": "not_applicable", "provider": "USGS Water Data / NOAA NWPS / NOAA CO-OPS",
            "message": "The selected SNBI snapshot is not identified as a water crossing.",
            "waterway_names": [],
        }
    lat, lon = context["latitude"], context["longitude"]
    pad = 0.18
    headers = {}
    if os.getenv("USGS_WATER_API_KEY", "").strip():
        headers["X-Api-Key"] = os.getenv("USGS_WATER_API_KEY", "").strip()
    params = {
        "f": "json", "bbox": f"{lon-pad},{lat-pad},{lon+pad},{lat+pad}",
        "limit": "200", "parameter_code": "00060,00065",
    }
    usgs_result, nwps_result, coops_stations_result = await asyncio.gather(
        _json(
            client,
            "https://api.waterdata.usgs.gov/ogcapi/v1/collections/latest-continuous/items",
            params=params,
            headers=headers,
        ),
        _json(
            client,
            "https://api.water.noaa.gov/nwps/v1/gauges",
            params={
                "bbox.xmin": f"{lon-pad:.6f}", "bbox.ymin": f"{lat-pad:.6f}",
                "bbox.xmax": f"{lon+pad:.6f}", "bbox.ymax": f"{lat+pad:.6f}",
                "srid": "EPSG_4326",
            },
        ),
        _json(
            client,
            "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json",
            params={"type": "waterlevels", "units": "english"},
        ),
        return_exceptions=True,
    )
    data = usgs_result if isinstance(usgs_result, dict) else {}
    readings = []
    for feature in data.get("features") or []:
        prop = feature.get("properties") or {}
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        station_lat = float(coords[1]) if len(coords) >= 2 else None
        station_lon = float(coords[0]) if len(coords) >= 2 else None
        distance = _distance_miles(lat, lon, station_lat, station_lon) if station_lat is not None and station_lon is not None else None
        readings.append({
            "station_id": prop.get("monitoring_location_id") or prop.get("monitoring_location_number"),
            "station_name": prop.get("monitoring_location_name") or prop.get("monitoring_location_id"),
            "parameter_code": prop.get("parameter_code"),
            "value": prop.get("value") or prop.get("last_value"),
            "unit": prop.get("unit_of_measure") or prop.get("unit_code"),
            "observed_at": prop.get("time") or prop.get("datetime"),
            "latitude": station_lat, "longitude": station_lon, "distance_miles": distance,
        })
    readings.sort(key=lambda item: item["distance_miles"] if item["distance_miles"] is not None else 10_000)
    nearest = readings[0] if readings else None
    nwps_gauges = []
    if isinstance(nwps_result, dict):
        raw_gauges = nwps_result.get("gauges") or nwps_result.get("items") or nwps_result.get("features") or []
        for raw in raw_gauges:
            gauge = (raw.get("properties") or {}) if isinstance(raw, dict) and raw.get("properties") else raw
            if not isinstance(gauge, dict):
                continue
            try:
                gauge_lat = float(gauge.get("latitude"))
                gauge_lon = float(gauge.get("longitude"))
            except (TypeError, ValueError):
                geometry = raw.get("geometry") if isinstance(raw, dict) else None
                coords = (geometry or {}).get("coordinates") or []
                if len(coords) < 2:
                    continue
                gauge_lon, gauge_lat = float(coords[0]), float(coords[1])
            nwps_gauges.append({
                "identifier": gauge.get("lid") or gauge.get("identifier") or gauge.get("id"),
                "usgs_id": gauge.get("usgsId") or gauge.get("usgs_id"),
                "name": gauge.get("name"),
                "latitude": gauge_lat, "longitude": gauge_lon,
                "distance_miles": _distance_miles(lat, lon, gauge_lat, gauge_lon),
                "observed_status": (gauge.get("status") or {}).get("observed") if isinstance(gauge.get("status"), dict) else None,
                "forecast_status": (gauge.get("status") or {}).get("forecast") if isinstance(gauge.get("status"), dict) else None,
            })
    nwps_gauges.sort(key=lambda item: item["distance_miles"])
    nearest_nwps = nwps_gauges[0] if nwps_gauges else None
    hydrograph = None
    if nearest_nwps and nearest_nwps.get("identifier"):
        try:
            hydrograph = await _json(client, f'https://api.water.noaa.gov/nwps/v1/gauges/{nearest_nwps["identifier"]}/stageflow')
        except ProviderError:
            hydrograph = None

    coops_station = None
    coops_latest = None
    coops_predictions: list[dict[str, Any]] = []
    if isinstance(coops_stations_result, dict):
        coops_candidates = []
        for station in coops_stations_result.get("stations") or []:
            if not isinstance(station, dict):
                continue
            try:
                station_lat = float(station.get("lat"))
                station_lon = float(station.get("lng"))
            except (TypeError, ValueError):
                continue
            candidate = {
                "station_id": station.get("id"),
                "station_name": station.get("name"),
                "latitude": station_lat,
                "longitude": station_lon,
                "distance_miles": _distance_miles(lat, lon, station_lat, station_lon),
            }
            coops_candidates.append(candidate)
        coops_candidates.sort(key=lambda item: item["distance_miles"])
        # CO-OPS observations are useful for coastal/tidal crossings, not as a
        # substitute for a river gage far inland.
        if coops_candidates and coops_candidates[0]["distance_miles"] <= 60:
            coops_station = coops_candidates[0]
            station_id = coops_station.get("station_id")
            common = {
                "station": station_id,
                "datum": "MLLW",
                "time_zone": "gmt",
                "units": "english",
                "application": "BridgeGuard",
                "format": "json",
            }
            latest_result, prediction_result = await asyncio.gather(
                _json(
                    client,
                    "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter",
                    params={**common, "date": "latest", "product": "water_level"},
                ),
                _json(
                    client,
                    "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter",
                    params={**common, "date": "today", "product": "predictions", "interval": "hilo"},
                ),
                return_exceptions=True,
            )
            if isinstance(latest_result, dict):
                observations = latest_result.get("data") or []
                if observations:
                    observation = observations[0]
                    coops_latest = {
                        "value": observation.get("v"),
                        "unit": "ft MLLW",
                        "observed_at": observation.get("t"),
                        "quality": observation.get("q"),
                    }
            if isinstance(prediction_result, dict):
                coops_predictions = [item for item in prediction_result.get("predictions") or [] if isinstance(item, dict)][:8]
    source_errors = []
    if isinstance(usgs_result, Exception):
        source_errors.append({"provider": "USGS Water Data", "error_code": getattr(usgs_result, "code", "source_error")})
    if isinstance(nwps_result, Exception):
        source_errors.append({"provider": "NOAA NWPS", "error_code": getattr(nwps_result, "code", "source_error")})
    if isinstance(coops_stations_result, Exception):
        source_errors.append({"provider": "NOAA CO-OPS", "error_code": getattr(coops_stations_result, "code", "source_error")})
    return {
        "status": "available" if readings or nwps_gauges or coops_station else "unavailable",
        "provider": "USGS Water Data / NOAA NWPS / NOAA CO-OPS",
        "source_url": "https://water.noaa.gov/",
        "waterway_names": context["waterway_names"],
        "nearest_station": nearest,
        "readings": readings[:12],
        "nearest_nwps_gauge": nearest_nwps,
        "nwps_gauges": nwps_gauges[:12],
        "nwps_stageflow": hydrograph,
        "coops_station": coops_station,
        "coops_latest": coops_latest,
        "coops_predictions": coops_predictions,
        "coops_source_url": "https://tidesandcurrents.noaa.gov/",
        "streamstats_url": f"https://streamstats.usgs.gov/ss/?lat={lat:.6f}&lon={lon:.6f}",
        "source_errors": source_errors,
        "nwps_source_url": "https://water.noaa.gov/",
        "message": None if readings or nwps_gauges or coops_station else "No current USGS, NWPS, or coastal NOAA station was found near this water crossing.",
    }


async def _arcgis_query(
    client: httpx.AsyncClient, url: str, context: dict[str, Any], *, distance: int | None = None
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "f": "json", "where": "1=1", "outFields": "*", "returnGeometry": "false",
        "geometry": f'{context["longitude"]},{context["latitude"]}', "geometryType": "esriGeometryPoint",
        "inSR": "4326", "spatialRel": "esriSpatialRelIntersects", "resultRecordCount": "30",
    }
    if distance is not None:
        params.update(distance=str(distance), units="esriSRUnit_StatuteMile")
    data = await _json(client, url, params=params)
    return [feature.get("attributes") or {} for feature in data.get("features") or []]


def _constraint_sources() -> list[tuple[str, str, int | None, str]]:
    """Public, bridge-queryable screening layers shared by counts and overlays."""
    geology = "https://mapsdep.nj.gov/arcgis/rest/services/Features/Geology/MapServer"
    environmental = "https://mapsdep.nj.gov/arcgis/rest/services/Features/Environmental/MapServer"
    njem = "https://mapsdep.nj.gov/arcgis/rest/services/Features/Environmental_NJEMS/MapServer"
    hydro = "https://mapsdep.nj.gov/arcgis/rest/services/Features/Hydrography/MapServer"
    land = "https://mapsdep.nj.gov/arcgis/rest/services/Features/Land/MapServer"
    land_use = "https://mapsdep.nj.gov/arcgis/rest/services/Features/Land_lu/MapServer"
    habitat = "https://mapsdep.nj.gov/arcgis/rest/services/Features/Environmental_habitat/MapServer"
    utilities = "https://mapsdep.nj.gov/arcgis/rest/services/Features/Utilities/MapServer"
    structures = "https://mapsdep.nj.gov/arcgis/rest/services/Features/Structures/MapServer"
    environmental_mon = "https://mapsdep.nj.gov/arcgis/rest/services/Features/Environmental_mon/MapServer"
    return [
        ("parcels", os.getenv("SITE_PARCELS_QUERY_URL", "").strip() or "https://services2.arcgis.com/XVOqAjTOJ5P6ngMu/arcgis/rest/services/Parcels_Composite_NJ_WM/FeatureServer/0/query", None, "property"),
        ("land_use_2015", f"{land_use}/13/query", None, "property"),
        ("impervious_surface_2012", f"{land_use}/3/query", None, "property"),
        ("national_wetlands", os.getenv("SITE_WETLANDS_QUERY_URL", "").strip() or "https://fwspublicservices.wim.usgs.gov/wetlandsmapservice/rest/services/Wetlands/MapServer/0/query", None, "environmental"),
        ("njdep_wetlands_2012", f"{land_use}/2/query", None, "environmental"),
        ("epa_priority_wetlands", f"{land}/79/query", None, "environmental"),
        ("contaminated_sites", os.getenv("SITE_CONTAMINATED_SITES_QUERY_URL", "").strip() or f"{njem}/0/query", 1, "environmental"),
        ("brownfield_inventory", f"{environmental_mon}/36/query", 1, "environmental"),
        ("federally_regulated_sites", f"{environmental}/85/query", 1, "environmental"),
        ("underground_storage_tanks", f"{njem}/9/query", 1, "environmental"),
        ("immediate_environmental_concerns", f"{njem}/20/query", 1, "environmental"),
        ("deed_notice_areas", f"{environmental}/40/query", 1, "environmental"),
        ("soils", os.getenv("SITE_SOILS_QUERY_URL", "").strip() or f"{geology}/12/query", None, "geotechnical"),
        ("landslides", os.getenv("SITE_LANDSLIDES_QUERY_URL", "").strip() or f"{geology}/1/query", 1, "geotechnical"),
        ("historic_fill", f"{geology}/22/query", None, "geotechnical"),
        ("potential_acid_soils", f"{geology}/27/query", None, "geotechnical"),
        ("abandoned_mines", f"{geology}/0/query", 2, "geotechnical"),
        ("bedrock_geology", f"{geology}/14/query", None, "geotechnical"),
        ("groundwater_recharge", f"{geology}/18/query", None, "geotechnical"),
        ("sole_source_aquifer", f"{geology}/19/query", None, "geotechnical"),
        ("surficial_geology", f"{geology}/25/query", None, "geotechnical"),
        ("watershed", f"{hydro}/22/query", None, "water"),
        ("category_one_waters", f"{hydro}/6/query", 1, "water"),
        ("surface_water_classification", f"{hydro}/7/query", 1, "water"),
        ("tmdl_streamshed", f"{environmental}/75/query", None, "water"),
        ("wellhead_protection", f"{hydro}/25/query", None, "water"),
        ("head_of_tide", f"{hydro}/2/query", 1, "water"),
        ("tidal_climate_adjusted_flood", f"{hydro}/48/query", None, "water"),
        ("nearby_dams", "https://geospatial.sec.usace.army.mil/dls/rest/services/NID/National_Inventory_of_Dams_Public_Service/FeatureServer/0/query", 5, "water"),
        ("historic_properties", f"{land}/55/query", 1, "historic_ecology"),
        ("historic_districts", f"{land}/57/query", 1, "historic_ecology"),
        ("archaeological_site_grid", f"{land}/56/query", None, "historic_ecology"),
        ("natural_heritage_priority", f"{habitat}/93/query", None, "historic_ecology"),
        ("building_footprints", f"{structures}/8/query", None, "coordination"),
        ("mapped_utility_lines", f"{utilities}/3/query", 1, "coordination"),
        ("sewer_service_area", f"{utilities}/8/query", None, "coordination"),
        ("electric_utility_territory", f"{utilities}/10/query", None, "coordination"),
        ("gas_utility_territory", f"{utilities}/11/query", None, "coordination"),
    ]


def _map_layer_sources() -> list[tuple[str, str, int | None, str]]:
    """All public feature services exposed as selectable bridge-map overlays."""
    fema = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer"
    return [
        *_constraint_sources(),
        ("fema_flood_zones", f"{fema}/28/query", None, "flood"),
        ("base_flood_elevations", f"{fema}/16/query", 1, "flood"),
    ]


def _layer_summaries(features: list[dict[str, Any]], fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build compact categorical figures without assuming provider field names."""
    excluded = {"objectid", "fid", "shape", "shape_length", "shape_area", "globalid"}
    preferred_tokens = ("status", "type", "class", "category", "zone", "use", "name", "label")
    candidates: list[tuple[int, str, str, list[dict[str, Any]]]] = []
    for field in fields:
        name = str(field.get("name") or "")
        if not name or name.lower() in excluded or name.lower().startswith("shape"):
            continue
        values: dict[str, int] = {}
        for feature in features:
            value = (feature.get("attributes") or {}).get(name)
            if value in (None, ""):
                continue
            label = str(value).strip()
            if label:
                values[label] = values.get(label, 0) + 1
        if not values or len(values) > 12:
            continue
        label = str(field.get("alias") or name).strip()
        priority = 0 if any(token in f"{name} {label}".lower() for token in preferred_tokens) else 1
        ranked = [{"label": key, "count": count} for key, count in sorted(values.items(), key=lambda item: (-item[1], item[0]))[:8]]
        candidates.append((priority, name, label, ranked))
    candidates.sort(key=lambda item: (item[0], item[2].lower()))
    return [{"field": name, "label": label, "values": values} for _, name, label, values in candidates[:3]]


async def _arcgis_feature_set(
    client: httpx.AsyncClient,
    url: str,
    context: dict[str, Any],
    *,
    distance: int | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "f": "json", "where": "1=1", "outFields": "*", "returnGeometry": "true",
        "geometry": f'{context["longitude"]},{context["latitude"]}', "geometryType": "esriGeometryPoint",
        "inSR": "4326", "outSR": "4326", "spatialRel": "esriSpatialRelIntersects",
        "resultRecordCount": "100", "geometryPrecision": "6", "returnZ": "false", "returnM": "false",
    }
    if distance is not None:
        params.update(distance=str(distance), units="esriSRUnit_StatuteMile")
    data = await _json(client, url, params=params)
    features = [item for item in data.get("features") or [] if isinstance(item, dict)]
    fields = [item for item in data.get("fields") or [] if isinstance(item, dict)]
    return {
        "geometry_type": data.get("geometryType"),
        "spatial_reference": data.get("spatialReference") or {"wkid": 4326},
        "fields": fields,
        "features": features,
        "exceeded_transfer_limit": bool(data.get("exceededTransferLimit")),
        "summaries": _layer_summaries(features, fields),
    }


def _cached_layer(context: dict[str, Any], layer_name: str) -> dict[str, Any] | None:
    _init_cache()
    section = f"layer:{layer_name}"
    with closing(sqlite3.connect(SITE_INTELLIGENCE_CACHE_DB)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM site_intelligence_cache WHERE bridge_key=? AND coordinate_hash=? AND section=?",
            (context["bridge_key"], context["coordinate_hash"], section),
        ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except Exception:
        return None
    now = time.time()
    payload.update(
        fetched_at=_utc_iso(float(row["fetched_at"])),
        expires_at=_utc_iso(float(row["expires_at"])),
        cache_state="fresh" if now <= float(row["expires_at"]) else "stale",
    )
    return payload


async def load_site_layer_geometry(
    state_code: int,
    bridge_number: str,
    layer_name: str,
    year: int | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Load one selected public layer; never fan out geometry for all layers."""
    sources = {name: (url, distance, category) for name, url, distance, category in _map_layer_sources()}
    if layer_name not in sources:
        raise ValueError("Unknown site-intelligence layer.")
    context = bridge_context(state_code, bridge_number, year)
    cached = _cached_layer(context, layer_name)
    if cached and cached.get("cache_state") == "fresh" and not force:
        return cached

    url, distance, category = sources[layer_name]
    timeout = httpx.Timeout(20.0, connect=8.0)
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
        feature_set = await _arcgis_feature_set(client, url, context, distance=distance)
    payload = {
        "status": "available",
        "layer": layer_name,
        "label": layer_name.replace("_", " ").title(),
        "category": category,
        "source_url": url[:-6] if url.endswith("/query") else url,
        "search_radius_miles": distance,
        **feature_set,
    }
    now = time.time()
    _init_cache()
    ttl = TTLS["constraints"]
    with closing(sqlite3.connect(SITE_INTELLIGENCE_CACHE_DB)) as connection:
        connection.execute(
            """
            INSERT INTO site_intelligence_cache
                (bridge_key, coordinate_hash, section, payload_json, source_observed_at,
                 fetched_at, expires_at, stale_until, status, error_code)
            VALUES (?, ?, ?, ?, NULL, ?, ?, ?, 'available', NULL)
            ON CONFLICT(bridge_key, coordinate_hash, section) DO UPDATE SET
                payload_json=excluded.payload_json, fetched_at=excluded.fetched_at,
                expires_at=excluded.expires_at, stale_until=excluded.stale_until,
                status=excluded.status, error_code=NULL
            """,
            (
                context["bridge_key"], context["coordinate_hash"], f"layer:{layer_name}",
                json.dumps(payload, separators=(",", ":"), default=str), now, now + ttl,
                now + STALE_TTLS["constraints"],
            ),
        )
        connection.commit()
    payload.update(fetched_at=_utc_iso(now), expires_at=_utc_iso(now + ttl), cache_state="fresh")
    return payload


async def _flood(client: httpx.AsyncClient, context: dict[str, Any]) -> dict[str, Any]:
    root = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer"
    zones, elevations = await asyncio.gather(
        _arcgis_query(client, f"{root}/28/query", context),
        _arcgis_query(client, f"{root}/16/query", context, distance=1),
        return_exceptions=True,
    )
    zone_rows = zones if isinstance(zones, list) else []
    elevation_rows = elevations if isinstance(elevations, list) else []
    return {
        "status": "available" if zone_rows or elevation_rows else "unavailable",
        "provider": "FEMA National Flood Hazard Layer",
        "source_url": "https://msc.fema.gov/portal/home",
        "map_service_url": root,
        "flood_zones": zone_rows,
        "base_flood_elevations": elevation_rows,
        "message": None if zone_rows or elevation_rows else "No intersecting published NFHL feature was returned at this location.",
    }


def _sample_points(
    context: dict[str, Any], half_span_feet: int = 600, grid_size: int = 13,
) -> tuple[list[list[float]], list[dict[str, Any]]]:
    """Build a north-up square sampling grid centered on the bridge."""
    if grid_size < 3 or grid_size % 2 == 0:
        raise ValueError("grid_size must be an odd number of at least 3")
    lat, lon = context["latitude"], context["longitude"]
    points: list[list[float]] = []
    meta: list[dict[str, Any]] = []
    spacing = half_span_feet * 2 / (grid_size - 1)
    longitude_feet = 364000 * max(math.cos(math.radians(lat)), 0.2)
    for row in range(grid_size):
        north_offset = half_span_feet - row * spacing
        for column in range(grid_size):
            east_offset = -half_span_feet + column * spacing
            sample_lat = lat + north_offset / 364000
            sample_lon = lon + east_offset / longitude_feet
            points.append([sample_lon, sample_lat])
            meta.append({
                "row": row,
                "column": column,
                "north_offset_feet": round(north_offset, 1),
                "east_offset_feet": round(east_offset, 1),
                "latitude": sample_lat,
                "longitude": sample_lon,
            })
    return points, meta


def _grid_with_slopes(samples: list[dict[str, Any]], grid_size: int, spacing_feet: float) -> list[dict[str, Any]]:
    """Estimate surface slope at each cell with centered or one-sided differences."""
    lookup = {(int(item["row"]), int(item["column"])): item for item in samples}

    def elevation(row: int, column: int) -> float | None:
        item = lookup.get((row, column))
        value = item.get("elevation_feet") if item else None
        return float(value) if value is not None else None

    completed: list[dict[str, Any]] = []
    for item in samples:
        row, column = int(item["row"]), int(item["column"])
        center = elevation(row, column)
        west, east = elevation(row, column - 1), elevation(row, column + 1)
        north, south = elevation(row - 1, column), elevation(row + 1, column)
        dx = None
        dy = None
        if west is not None and east is not None:
            dx = (east - west) / (spacing_feet * 2)
        elif center is not None and east is not None:
            dx = (east - center) / spacing_feet
        elif center is not None and west is not None:
            dx = (center - west) / spacing_feet
        if north is not None and south is not None:
            dy = (north - south) / (spacing_feet * 2)
        elif center is not None and north is not None:
            dy = (north - center) / spacing_feet
        elif center is not None and south is not None:
            dy = (center - south) / spacing_feet
        slope = math.sqrt(dx * dx + dy * dy) * 100 if dx is not None and dy is not None else None
        completed.append({**item, "slope_percent": round(slope, 3) if slope is not None else None})
    return completed


def _profile_summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    elevations = [float(item["elevation_feet"]) for item in samples if item.get("elevation_feet") is not None]
    grades = []
    for left, right in zip(samples, samples[1:]):
        if left.get("elevation_feet") is None or right.get("elevation_feet") is None:
            continue
        distance = abs(float(right["offset_feet"]) - float(left["offset_feet"]))
        if distance:
            grades.append(abs(float(right["elevation_feet"]) - float(left["elevation_feet"])) / distance * 100)
    return {
        "minimum_elevation_feet": min(elevations) if elevations else None,
        "maximum_elevation_feet": max(elevations) if elevations else None,
        "maximum_segment_grade_percent": max(grades) if grades else None,
        "average_segment_grade_percent": sum(grades) / len(grades) if grades else None,
    }


async def _terrain(client: httpx.AsyncClient, context: dict[str, Any]) -> dict[str, Any]:
    service = os.getenv("NJ_DEM_IMAGE_SERVICE", "https://maps.nj.gov/arcgis/rest/services/Elevation/NJ_10ft_DEM/ImageServer")
    half_span_feet = 600
    grid_size = 13
    spacing_feet = half_span_feet * 2 / (grid_size - 1)
    points, meta = _sample_points(context, half_span_feet, grid_size)
    params = {
        "f": "json", "geometryType": "esriGeometryMultipoint",
        "geometry": json.dumps({"points": points, "spatialReference": {"wkid": 4326}}),
        "returnFirstValueOnly": "true", "outFields": "*", "interpolation": "RSP_BilinearInterpolation",
    }
    data = await _json(client, f"{service}/getSamples", params=params)
    raw_samples = data.get("samples") or []
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw_samples[: len(meta)]):
        value = item.get("value")
        try:
            elevation = float(str(value).split()[0])
        except (TypeError, ValueError):
            elevation = None
        normalized.append({**meta[index], "elevation_feet": elevation})
    grid_samples = _grid_with_slopes(normalized, grid_size, spacing_feet)
    middle = grid_size // 2
    north_south = sorted(
        [
            {**item, "axis": "north_south", "offset_feet": item["north_offset_feet"]}
            for item in grid_samples if item["column"] == middle
        ],
        key=lambda item: item["offset_feet"],
    )
    east_west = sorted(
        [
            {**item, "axis": "east_west", "offset_feet": item["east_offset_feet"]}
            for item in grid_samples if item["row"] == middle
        ],
        key=lambda item: item["offset_feet"],
    )
    elevations = [item["elevation_feet"] for item in grid_samples if item.get("elevation_feet") is not None]
    slopes = [item["slope_percent"] for item in grid_samples if item.get("slope_percent") is not None]
    profiles = {
        "north_south": {"samples": north_south, **_profile_summary(north_south)},
        "east_west": {"samples": east_west, **_profile_summary(east_west)},
    }
    return {
        "status": "available" if grid_samples else "unavailable",
        "provider": "New Jersey 10-foot Digital Elevation Model",
        "source_url": service,
        "grid": {
            "size": grid_size,
            "half_span_feet": half_span_feet,
            "spacing_feet": spacing_feet,
            "samples": grid_samples,
            "minimum_elevation_feet": min(elevations) if elevations else None,
            "maximum_elevation_feet": max(elevations) if elevations else None,
            "minimum_slope_percent": min(slopes) if slopes else None,
            "maximum_slope_percent": max(slopes) if slopes else None,
            "average_slope_percent": sum(slopes) / len(slopes) if slopes else None,
        },
        "profiles": profiles,
        "method_note": "Screening-grade 13×13 terrain grid and centerline profiles centered on the inventory coordinate; field survey is required for design.",
    }


async def _constraints(client: httpx.AsyncClient, context: dict[str, Any]) -> dict[str, Any]:
    sources = _constraint_sources()

    # Bound fan-out even though these are public endpoints. Results live in the
    # bridge cache for 30 days, so a bridge tab normally pays this cost once.
    query_limit = asyncio.Semaphore(6)

    async def query(url: str, distance: int | None) -> list[dict[str, Any]]:
        async with query_limit:
            return await _arcgis_query(client, url, context, distance=distance)

    results = await asyncio.gather(
        *[query(url, distance) for _, url, distance, _ in sources],
        return_exceptions=True,
    )
    layers: dict[str, Any] = {}
    groups: dict[str, list[str]] = {}
    for (name, url, distance, group), value in zip(sources, results):
        groups.setdefault(group, []).append(name)
        public_url = url[:-6] if url.endswith("/query") else url
        if isinstance(value, Exception):
            layers[name] = {
                "status": "unavailable", "features": [], "source_url": public_url,
                "category": group, "search_radius_miles": distance,
                "error_code": getattr(value, "code", "source_error"),
            }
        else:
            layers[name] = {
                "status": "available", "features": value, "source_url": public_url,
                "category": group, "search_radius_miles": distance,
            }
    return {
        "status": "available" if any(item["status"] == "available" for item in layers.values()) else "unavailable",
        "provider": "FWS / NJDEP / NJGIN / USACE",
        "source_url": "https://dep.nj.gov/gis/nj-geoweb-profiles/",
        "layers": layers,
        "groups": groups,
        "local_screening": {
            "water_crossing": context["water_crossing"],
            "scour_condition_rating": context.get("scour_condition_rating"),
            "scour_vulnerability": context.get("scour_vulnerability"),
            "scour_plan_of_action": context.get("scour_plan_of_action"),
            "overtopping_likelihood": context.get("overtopping_likelihood"),
        },
        "method_note": "Public screening information only. A zero count does not rule out a constraint, and mapped utility layers are not a mark-out. Verify current boundaries, utilities, permits, geotechnical conditions, historic resources, and ownership before design or construction.",
    }


async def _imagery(client: httpx.AsyncClient, context: dict[str, Any]) -> dict[str, Any]:
    public_imagery = [
        {"year": 2020, "label": "NJ 2020 orthophoto", "map_service_url": "https://maps.nj.gov/arcgis/rest/services/Basemap/Orthos_Natural_2020_NJ_WM/MapServer"},
        {"year": 2015, "label": "NJ 2015 orthophoto", "map_service_url": "https://maps.nj.gov/arcgis/rest/services/Basemap/Orthos_Natural_2015_NJ_WM/MapServer"},
        {"year": 2012, "label": "NJ 2012 orthophoto", "map_service_url": "https://maps.nj.gov/arcgis/rest/services/Basemap/Orthos_Natural_2012_NJ_WM/MapServer"},
    ]
    api_key = os.getenv("NEARMAP_API_KEY", "").strip()
    if not api_key:
        return {
            "status": "available", "provider": "NJGIN / Nearmap",
            "source_url": "https://www.nj.gov/njgin/edata/imagery/", "surveys": [],
            "public_imagery": public_imagery, "nearmap_status": "not_configured",
            "message": "Free NJ orthophotos are available. Add a Nearmap API key for recent survey dates and oblique views.",
        }
    url = f'https://api.nearmap.com/coverage/v2/point/{context["longitude"]:.6f},{context["latitude"]:.6f}'
    try:
        data = await _json(
            client, url,
            params={"apikey": api_key, "resources": "photos,tiles:Vert,tiles:North,tiles:South,tiles:East,tiles:West", "limit": "20", "sort": "-captureDate"},
        )
    except ProviderError as exc:
        return {
            "status": "available", "provider": "NJGIN / Nearmap",
            "source_url": "https://www.nj.gov/njgin/edata/imagery/", "surveys": [],
            "public_imagery": public_imagery, "nearmap_status": "unavailable",
            "nearmap_error": exc.code,
            "message": "Free NJ orthophotos are available; Nearmap could not be reached with the configured key.",
        }
    raw = data.get("surveys") or data.get("items") or data.get("results") or []
    surveys = []
    for item in raw:
        resources = item.get("resources") or []
        surveys.append({
            "survey_id": item.get("id") or item.get("surveyId"),
            "capture_date": item.get("captureDate") or item.get("date"),
            "published_date": item.get("publishedDate"),
            "resources": resources,
        })
    return {
        "status": "available", "provider": "NJGIN / Nearmap",
        "source_url": "https://www.nj.gov/njgin/edata/imagery/", "surveys": surveys,
        "public_imagery": public_imagery, "nearmap_status": "available" if surveys else "unavailable",
        "tile_proxy_template": "/api/site-intelligence/nearmap-tiles/{survey_id}/{content}/{z}/{x}/{y}",
        "message": None if surveys else "Free NJ orthophotos are available; no licensed Nearmap survey was returned at this coordinate.",
    }


PROVIDERS: dict[str, Callable[[httpx.AsyncClient, dict[str, Any]], Awaitable[dict[str, Any]]]] = {
    "weather": _weather, "traffic": _traffic, "water": _water, "flood": _flood,
    "terrain": _terrain, "constraints": _constraints, "imagery": _imagery,
}


def _store(context: dict[str, Any], section: str, payload: dict[str, Any], now: float) -> None:
    _init_cache()
    expires = now + TTLS[section]
    observed = payload.get("source_observed_at")
    with closing(sqlite3.connect(SITE_INTELLIGENCE_CACHE_DB)) as connection:
        connection.execute(
            """
            INSERT INTO site_intelligence_cache
                (bridge_key, coordinate_hash, section, payload_json, source_observed_at,
                 fetched_at, expires_at, stale_until, status, error_code)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bridge_key, coordinate_hash, section) DO UPDATE SET
                payload_json=excluded.payload_json, source_observed_at=excluded.source_observed_at,
                fetched_at=excluded.fetched_at, expires_at=excluded.expires_at,
                stale_until=excluded.stale_until, status=excluded.status, error_code=excluded.error_code
            """,
            (
                context["bridge_key"], context["coordinate_hash"], section,
                json.dumps(payload, separators=(",", ":"), default=str), observed,
                now, expires, now + STALE_TTLS[section], payload.get("status", "available"), payload.get("error_code"),
            ),
        )
        connection.commit()


async def _refresh_section(client: httpx.AsyncClient, context: dict[str, Any], section: str) -> dict[str, Any]:
    async with _provider_limits[section]:
        payload = await PROVIDERS[section](client, context)
    payload.setdefault("status", "available")
    payload["section"] = section
    return payload


async def refresh_site_intelligence(
    state_code: int,
    bridge_number: str,
    year: int | None = None,
    sections: list[str] | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    context = bridge_context(state_code, bridge_number, year)
    requested = list(dict.fromkeys(sections or SECTIONS))
    invalid = [name for name in requested if name not in SECTIONS]
    if invalid:
        raise ValueError(f"unknown_sections:{','.join(invalid)}")
    lock_key = f'{context["bridge_key"]}:{context["coordinate_hash"]}'
    async with _locks_guard:
        lock = _locks.setdefault(lock_key, asyncio.Lock())
    async with lock:
        cached = _cached_rows(context)
        now = time.time()
        refresh_names = []
        for name in requested:
            item = cached.get(name)
            if item is None:
                refresh_names.append(name)
                continue
            fetched_epoch = datetime.fromisoformat(str(item["fetched_at"]).replace("Z", "+00:00")).timestamp()
            expired = item.get("cache_state") == "stale"
            if expired or (force and now - fetched_epoch >= MANUAL_REFRESH_COOLDOWN_SECONDS):
                refresh_names.append(name)

        if refresh_names:
            timeout = httpx.Timeout(9.0, connect=5.0)
            async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}) as client:
                results = await asyncio.gather(
                    *[_refresh_section(client, context, name) for name in refresh_names],
                    return_exceptions=True,
                )
            for name, value in zip(refresh_names, results):
                if isinstance(value, Exception):
                    previous = cached.get(name)
                    if previous and previous.get("status") not in {"missing"}:
                        previous.update(
                            stale=True, cache_state="stale", refresh_error_code=getattr(value, "code", "source_error"),
                            refresh_error="The live source could not be refreshed; the last cached result is shown.",
                        )
                        cached[name] = previous
                        continue
                    provider, source_url = PROVIDER_INFO[name]
                    value = {
                        "section": name, "status": "rate_limited" if getattr(value, "code", "") == "rate_limited" else "unavailable",
                        "provider": provider, "source_url": source_url,
                        "error_code": getattr(value, "code", "source_error"),
                        "message": "This source is temporarily unavailable. Other site information is still shown.",
                    }
                _store(context, name, value, now)
        return _response(context, _cached_rows(context) | {name: value for name, value in cached.items() if value.get("refresh_error_code")})
