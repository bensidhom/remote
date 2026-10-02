from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing

from app.services import site_intelligence as service


def _inventory(path):
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE bridges (
                state_code INTEGER, bridge_number TEXT, data_year INTEGER,
                bridge_name TEXT, bridge_location TEXT, latitude REAL, longitude REAL,
                year_built INTEGER, owner TEXT, maintenance_responsibility TEXT,
                deck_condition_rating REAL, superstructure_condition_rating REAL,
                substructure_condition_rating REAL, channel_condition_rating REAL,
                scour_condition_rating REAL, scour_vulnerability TEXT,
                scour_plan_of_action TEXT, overtopping_likelihood TEXT,
                calculated_deck_area REAL
            );
            CREATE TABLE features (
                state_code INTEGER, bridge_number TEXT, data_year INTEGER,
                feature_type TEXT, feature_location TEXT, feature_name TEXT,
                navigable_waterway TEXT
            );
            INSERT INTO bridges VALUES (
                34, 'TEST-1', 2025, 'Test Bridge', 'Test location', 40.1, -74.5,
                1980, '01', '01', 7, 6, 6, 5, 5, 'B', 'Y', '3', 1200
            );
            INSERT INTO features VALUES (34, 'TEST-1', 2025, 'W01', 'B', 'Test Creek', 'N');
            """
        )
        connection.commit()


def test_site_intelligence_uses_separate_cache_and_refreshes_only_when_needed(tmp_path, monkeypatch):
    inventory = tmp_path / "inventory.sqlite3"
    cache = tmp_path / "site-cache.sqlite3"
    _inventory(inventory)
    monkeypatch.setattr(service, "USER_DB", str(inventory))
    monkeypatch.setattr(service, "SITE_INTELLIGENCE_CACHE_DB", str(cache))

    calls = []

    async def fake_weather(client, context):
        calls.append(context["bridge_key"])
        return {"status": "available", "provider": "test", "current": {"temperature": 70}}

    monkeypatch.setitem(service.PROVIDERS, "weather", fake_weather)
    first = asyncio.run(service.refresh_site_intelligence(34, "TEST-1", 2025, ["weather"]))
    second = asyncio.run(service.refresh_site_intelligence(34, "TEST-1", 2025, ["weather"]))

    assert first["sections"]["weather"]["current"]["temperature"] == 70
    assert second["sections"]["weather"]["cache_state"] == "fresh"
    assert calls == ["34:TEST-1:2025"]
    with closing(sqlite3.connect(inventory)) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='site_intelligence_cache'").fetchone() is None
    with closing(sqlite3.connect(cache)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM site_intelligence_cache").fetchone()[0] == 1


def test_water_section_short_circuits_for_non_water_bridge():
    context = {"water_crossing": False, "waterway_names": []}
    result = asyncio.run(service._water(None, context))
    assert result["status"] == "not_applicable"


def test_bridge_context_uses_requested_snapshot(tmp_path, monkeypatch):
    inventory = tmp_path / "inventory.sqlite3"
    _inventory(inventory)
    monkeypatch.setattr(service, "USER_DB", str(inventory))
    context = service.bridge_context(34, "TEST-1", 2025)
    assert context["data_year"] == 2025
    assert context["water_crossing"] is True
    assert context["waterway_names"] == ["Test Creek"]


def test_free_public_constraint_layers_are_enabled_without_credentials(monkeypatch):
    calls = []

    async def fake_query(client, url, context, *, distance=None):
        calls.append((url, distance))
        return [{"source": url}]

    monkeypatch.setattr(service, "_arcgis_query", fake_query)
    for name in (
        "SITE_PARCELS_QUERY_URL", "SITE_WETLANDS_QUERY_URL",
        "SITE_CONTAMINATED_SITES_QUERY_URL", "SITE_SOILS_QUERY_URL",
        "SITE_LANDSLIDES_QUERY_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    result = asyncio.run(service._constraints(None, {
        "latitude": 40.1, "longitude": -74.5, "water_crossing": True,
        "scour_condition_rating": 6, "scour_vulnerability": "B",
        "scour_plan_of_action": "Y", "overtopping_likelihood": "3",
    }))

    assert len(calls) == 38
    assert result["layers"]["parcels"]["status"] == "available"
    assert result["layers"]["contaminated_sites"]["status"] == "available"
    assert result["layers"]["historic_fill"]["category"] == "geotechnical"
    assert result["layers"]["nearby_dams"]["search_radius_miles"] == 5
    assert result["groups"]["water"] == [
        "watershed", "category_one_waters", "surface_water_classification",
        "tmdl_streamshed", "wellhead_protection", "head_of_tide",
        "tidal_climate_adjusted_flood", "nearby_dams",
    ]
    assert result["groups"]["coordination"] == [
        "building_footprints", "mapped_utility_lines", "sewer_service_area",
        "electric_utility_territory", "gas_utility_territory",
    ]


def test_traffic_uses_public_njdot_counts_without_arcgis_key(monkeypatch):
    async def fake_counts(client, url, **kwargs):
        return [{
            "si_station_num": "A-1", "si_lat": "40.11", "si_lon": "-74.51",
            "ev_aadt_tot": "12345", "count_year": "2024-01-01T00:00:00",
        }]

    monkeypatch.delenv("ARCGIS_API_TOKEN", raising=False)
    monkeypatch.setattr(service, "_json_list", fake_counts)
    result = asyncio.run(service._traffic(None, {"latitude": 40.1, "longitude": -74.5}))

    assert result["status"] == "available"
    assert result["nearest_count"]["aadt"] == 12345
    assert result["map_available"] is False


def test_imagery_keeps_public_orthophotos_without_nearmap_key(monkeypatch):
    monkeypatch.delenv("NEARMAP_API_KEY", raising=False)
    result = asyncio.run(service._imagery(None, {"latitude": 40.1, "longitude": -74.5}))

    assert result["status"] == "available"
    assert [item["year"] for item in result["public_imagery"]] == [2020, 2015, 2012]
    assert result["nearmap_status"] == "not_configured"


def test_selected_layer_geometry_is_lazy_and_cached(tmp_path, monkeypatch):
    inventory = tmp_path / "inventory.sqlite3"
    cache = tmp_path / "site-cache.sqlite3"
    _inventory(inventory)
    monkeypatch.setattr(service, "USER_DB", str(inventory))
    monkeypatch.setattr(service, "SITE_INTELLIGENCE_CACHE_DB", str(cache))
    calls = []

    async def fake_feature_set(client, url, context, *, distance=None):
        calls.append((url, distance, context["bridge_key"]))
        return {
            "geometry_type": "esriGeometryPolygon",
            "spatial_reference": {"wkid": 4326},
            "fields": [{"name": "STATUS", "alias": "Status", "type": "esriFieldTypeString"}],
            "features": [{
                "attributes": {"STATUS": "LISTED"},
                "geometry": {"rings": [[[-74.51, 40.09], [-74.49, 40.09], [-74.49, 40.11], [-74.51, 40.09]]]},
            }],
            "exceeded_transfer_limit": False,
            "summaries": [{"field": "STATUS", "label": "Status", "values": [{"label": "LISTED", "count": 1}]}],
        }

    monkeypatch.setattr(service, "_arcgis_feature_set", fake_feature_set)
    first = asyncio.run(service.load_site_layer_geometry(34, "TEST-1", "archaeological_site_grid", 2025))
    second = asyncio.run(service.load_site_layer_geometry(34, "TEST-1", "archaeological_site_grid", 2025))

    assert first["features"][0]["attributes"]["STATUS"] == "LISTED"
    assert first["summaries"][0]["values"][0]["count"] == 1
    assert second["cache_state"] == "fresh"
    assert len(calls) == 1
    with closing(sqlite3.connect(cache)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM site_intelligence_cache WHERE section='layer:archaeological_site_grid'"
        ).fetchone()[0] == 1


def test_layer_summaries_prefers_status_fields():
    summaries = service._layer_summaries(
        [
            {"attributes": {"STATUS": "LISTED", "COUNTY": "Mercer"}},
            {"attributes": {"STATUS": "ELIGIBLE", "COUNTY": "Mercer"}},
            {"attributes": {"STATUS": "LISTED", "COUNTY": "Bergen"}},
        ],
        [
            {"name": "COUNTY", "alias": "County"},
            {"name": "STATUS", "alias": "Status"},
        ],
    )
    assert summaries[0]["field"] == "STATUS"
    assert summaries[0]["values"][0] == {"label": "LISTED", "count": 2}


def test_map_layer_catalog_adds_fema_geometry_without_expanding_constraint_fanout():
    constraint_names = [item[0] for item in service._constraint_sources()]
    map_names = [item[0] for item in service._map_layer_sources()]
    assert "fema_flood_zones" not in constraint_names
    assert "base_flood_elevations" not in constraint_names
    assert map_names[-2:] == ["fema_flood_zones", "base_flood_elevations"]


def test_terrain_sampling_grid_is_bridge_centered_and_north_up():
    points, metadata = service._sample_points({"latitude": 40.1, "longitude": -74.5})

    assert len(points) == 169
    assert len(metadata) == 169
    assert metadata[0]["row"] == 0
    assert metadata[0]["column"] == 0
    assert metadata[0]["north_offset_feet"] == 600
    assert metadata[0]["east_offset_feet"] == -600
    assert metadata[84]["north_offset_feet"] == 0
    assert metadata[84]["east_offset_feet"] == 0
    assert metadata[-1]["north_offset_feet"] == -600
    assert metadata[-1]["east_offset_feet"] == 600
    assert points[84] == [-74.5, 40.1]


def test_terrain_grid_calculates_surface_slope_and_centerline_profiles(monkeypatch):
    async def fake_json(client, url, **kwargs):
        return {"samples": [{"value": str(index)} for index in range(169)]}

    monkeypatch.setattr(service, "_json", fake_json)
    result = asyncio.run(service._terrain(None, {"latitude": 40.1, "longitude": -74.5}))

    assert result["status"] == "available"
    assert result["grid"]["size"] == 13
    assert result["grid"]["spacing_feet"] == 100
    assert len(result["grid"]["samples"]) == 169
    assert len(result["profiles"]["north_south"]["samples"]) == 13
    assert len(result["profiles"]["east_west"]["samples"]) == 13
    assert result["grid"]["samples"][84]["slope_percent"] == 13.038
