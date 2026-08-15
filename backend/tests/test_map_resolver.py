from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import map_api
from app.map_api import router
from app.services.conversion.canonical_json import canonical_sha256
from app.services.map_resolver import (
    MAX_RESPONSE_CANDIDATES,
    normalize_bridge_identifier,
    resolve_bridge,
)


WORKSPACE = "shared-research-workspace"


def _database(tmp_path):
    path = tmp_path / "map.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE workspace_state (
            workspace_id TEXT PRIMARY KEY,
            data_generation INTEGER NOT NULL,
            schema_generation INTEGER NOT NULL
        );
        CREATE TABLE bridges (
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            data_year INTEGER NOT NULL,
            latitude REAL,
            longitude REAL,
            county_code INTEGER,
            bridge_name TEXT,
            bridge_location TEXT,
            year_built INTEGER,
            PRIMARY KEY (state_code, bridge_number, data_year)
        );
        CREATE TABLE features (
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            data_year INTEGER NOT NULL,
            feature_type TEXT,
            feature_location TEXT,
            feature_name TEXT
        );
        CREATE TABLE snapshot_revisions (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            data_year INTEGER NOT NULL,
            revision_number INTEGER NOT NULL,
            canonical_json TEXT NOT NULL,
            canonical_sha256 TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            rule_bundle_sha256 TEXT NOT NULL
        );
        CREATE TABLE published_snapshots (
            workspace_id TEXT NOT NULL,
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            data_year INTEGER NOT NULL,
            snapshot_revision_id TEXT NOT NULL,
            published_at TEXT NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO workspace_state VALUES (?, 17, 7)",
        (WORKSPACE,),
    )
    rows = (
        (34, "0102151", 2023, 40.10, -74.20, 1, "Alpha Crossing", "River Road", 1960),
        (34, "0102151", 2024, 40.11, -74.21, 1, "Alpha Crossing", "River Road", 1960),
        (34, "0200001", 2024, 40.20, -74.30, 3, "Main Street", "North Fork", 1970),
        (34, "0200002", 2024, 40.30, -74.40, 5, "Main Street", "South Fork", 1980),
        (34, "0300001", 2024, 40.40, -74.50, 7, "<img src=x onerror=alert(1)>", "Unsafe & Location", 1990),
        (36, "NY-ONLY", 2024, 41.00, -73.00, 1, "Alpha Crossing", "Elsewhere", 2000),
    )
    connection.executemany("INSERT INTO bridges VALUES (?,?,?,?,?,?,?,?,?)", rows)
    connection.executemany(
        "INSERT INTO features VALUES (?,?,?,?,?,?)",
        (
            (34, "0102151", 2023, "highway", "C", "Garden State Parkway"),
            (34, "0102151", 2023, "waterway", "B", "Alpha River"),
            (34, "0102151", 2024, "highway", "C", "Garden State Parkway"),
            (34, "0102151", 2024, "waterway", "B", "Alpha River"),
        ),
    )
    for state, number, year, *_ in rows:
        revision = f"rev-{state}-{number}-{year}"
        connection.execute(
            "INSERT INTO snapshot_revisions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                revision,
                WORKSPACE,
                state,
                number,
                year,
                2 if year == 2024 else 1,
                "{}",
                canonical_sha256({}),
                "test-profile",
                "b" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO published_snapshots VALUES (?,?,?,?,?,?)",
            (WORKSPACE, state, number, year, revision, "2026-08-13T12:00:00Z"),
        )
    connection.commit()
    connection.close()
    return path


def test_identifier_normalization_retains_leading_zeroes():
    assert normalize_bridge_identifier(" 01-02 151 ") == "0102151"


def test_exact_identifier_uses_latest_published_snapshot_and_revision(tmp_path):
    path = _database(tmp_path)
    result = resolve_bridge(
        str(path), workspace_id=WORKSPACE, state_code=34, query="Show bridge 01-02151 on map"
    )
    assert result["status"] == "exact"
    assert result["data_generation"] == 17
    assert result["schema_generation"] == 7
    bridge = result["bridge"]
    assert bridge["bridge_number"] == "0102151"
    assert bridge["snapshot_year"] == 2024
    assert bridge["snapshot_revision_id"] == "rev-34-0102151-2024"
    assert bridge["inventory_revision"] == "rev-34-0102151-2024"
    assert bridge["bridge_id"] == f"{WORKSPACE}:34:0102151"
    assert bridge["snapshot_id"] == f"{WORKSPACE}:34:0102151:2024"
    assert bridge["identity_status"] == "published"
    assert bridge["coordinates"] == {"latitude": 40.11, "longitude": -74.21}
    assert bridge["county_name"] == "Atlantic"


def test_explicit_year_never_falls_forward(tmp_path):
    path = _database(tmp_path)
    old = resolve_bridge(
        str(path), workspace_id=WORKSPACE, state_code=34, query="Alpha Crossing", year=2023
    )
    assert old["status"] == "exact"
    assert old["bridge"]["snapshot_year"] == 2023
    assert old["bridge"]["snapshot_revision_id"] == "rev-34-0102151-2023"

    missing = resolve_bridge(
        str(path), workspace_id=WORKSPACE, state_code=34, query="Alpha Crossing", year=2022
    )
    assert missing["status"] == "not_found"


def test_exact_approved_alias_and_controlled_text_are_deterministic(tmp_path):
    path = _database(tmp_path)
    alias = resolve_bridge(
        str(path),
        workspace_id=WORKSPACE,
        state_code=34,
        query="Garden State Parkway over Alpha River",
    )
    assert alias["status"] == "exact"
    assert alias["bridge"]["match_basis"] == "approved_alias"

    location = resolve_bridge(
        str(path), workspace_id=WORKSPACE, state_code=34, query="Alpha River Road"
    )
    assert location["status"] == "exact"
    assert location["bridge"]["match_basis"] == "controlled_text"


def test_equal_aliases_return_bounded_ambiguity(tmp_path):
    path = _database(tmp_path)
    result = resolve_bridge(
        str(path), workspace_id=WORKSPACE, state_code=34, query="Main Street"
    )
    assert result["status"] == "ambiguous"
    assert result["bridge"] is None
    assert result["candidate_count"] == 2
    assert 1 < len(result["candidates"]) <= MAX_RESPONSE_CANDIDATES
    assert {candidate["bridge_number"] for candidate in result["candidates"]} == {
        "0200001",
        "0200002",
    }


def test_controlled_text_requires_a_clear_top_score_margin(tmp_path):
    path = _database(tmp_path)
    ambiguous = resolve_bridge(
        str(path), workspace_id=WORKSPACE, state_code=34, query="Fork"
    )
    assert ambiguous["status"] == "ambiguous"
    assert all(
        candidate["match_basis"] == "controlled_text"
        for candidate in ambiguous["candidates"]
    )

    clear = resolve_bridge(
        str(path), workspace_id=WORKSPACE, state_code=34, query="North Fork"
    )
    assert clear["status"] == "exact"
    assert clear["bridge"]["bridge_number"] == "0200001"


def test_not_found_and_state_isolation(tmp_path):
    path = _database(tmp_path)
    result = resolve_bridge(
        str(path), workspace_id=WORKSPACE, state_code=34, query="NY-ONLY"
    )
    assert result["status"] == "not_found"
    assert result["candidates"] == []


def test_database_strings_remain_data_in_response(tmp_path):
    path = _database(tmp_path)
    result = resolve_bridge(
        str(path), workspace_id=WORKSPACE, state_code=34, query="0300001"
    )
    assert result["status"] == "exact"
    assert result["bridge"]["bridge_name"] == "<img src=x onerror=alert(1)>"


def test_legacy_projection_fallback_is_explicitly_unversioned(tmp_path):
    path = _database(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO bridges VALUES (34, '0400001', 2024, 40.5, -74.6, 9, "
        "'Legacy Only', 'Old Road', 1950)"
    )
    connection.commit()
    connection.close()

    result = resolve_bridge(
        str(path), workspace_id=WORKSPACE, state_code=34, query="0400001"
    )
    assert result["status"] == "exact"
    bridge = result["bridge"]
    assert bridge["identity_status"] == "legacy_projection_unversioned"
    assert bridge["snapshot_revision_id"] is None
    assert bridge["inventory_revision"] is None
    assert bridge["canonical_sha256"] is None
    assert result["inventory_revision"] is None


def test_another_workspace_cannot_suppress_legacy_fallback(tmp_path):
    path = _database(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO bridges VALUES (34, '0500001', 2024, 40.6, -74.7, 11, "
        "'Workspace Boundary', 'Scoped Road', 2001)"
    )
    connection.execute(
        "INSERT INTO published_snapshots VALUES (?,?,?,?,?,?)",
        ("other-workspace", 34, "0500001", 2024, "other-revision", "2026-08-13T12:00:00Z"),
    )
    connection.commit()
    connection.close()

    result = resolve_bridge(
        str(path), workspace_id=WORKSPACE, state_code=34, query="0500001"
    )
    assert result["status"] == "exact"
    assert result["bridge"]["identity_status"] == "legacy_projection_unversioned"


@pytest.fixture
def api_client(tmp_path):
    path = _database(tmp_path)
    app = FastAPI()

    @app.middleware("http")
    async def authenticated(request: Request, call_next):
        request.state.user = SimpleNamespace(email="viewer@example.com", role="viewer")
        return await call_next(request)

    app.include_router(router)
    map_api.configure_database_path(lambda: str(path))
    try:
        yield TestClient(app)
    finally:
        map_api.configure_database_path(None)


def test_http_route_rejects_all_client_scope_fields(api_client):
    workspace = api_client.post(
        "/api/map/resolve-bridge",
        json={"query": "0102151", "workspace_id": "other"},
    )
    assert workspace.status_code == 422

    state = api_client.post(
        "/api/map/resolve-bridge", json={"query": "0102151", "state_code": 36}
    )
    assert state.status_code == 422


def test_http_route_returns_revision_header_and_forbids_extra_scope(api_client):
    response = api_client.post(
        "/api/map/resolve-bridge",
        json={
            "query": "0102151",
            "year": 2024,
        },
    )
    assert response.status_code == 200
    assert response.headers["x-bridgeguard-data-version"] == "17"
    assert response.json()["bridge"]["snapshot_revision_id"] == "rev-34-0102151-2024"
    assert response.headers["cache-control"] == "private, no-store"

    extra = api_client.post(
        "/api/map/resolve-bridge", json={"query": "0102151", "owner_id": "attacker"}
    )
    assert extra.status_code == 422


def test_http_route_requires_an_authenticated_actor(tmp_path):
    path = _database(tmp_path)
    app = FastAPI()
    app.include_router(router)
    map_api.configure_database_path(lambda: str(path))
    try:
        response = TestClient(app).post(
            "/api/map/resolve-bridge", json={"query": "0102151"}
        )
    finally:
        map_api.configure_database_path(None)
    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"


def test_main_exposes_resolver_before_the_spa_catch_all():
    from app.main import app

    paths = [getattr(route, "path", None) for route in app.routes]
    assert paths.count("/api/map/resolve-bridge") == 1
