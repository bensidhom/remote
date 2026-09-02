from __future__ import annotations

import sqlite3

import pytest

from app.forecast_api import (
    BridgeBacktestBody,
    BridgeForecastBody,
    ForecastApiError,
    _loaded_backtest_payload,
    _loaded_forecast_payload,
)
from app.services.analytics.projection import load_bridge_analysis_history


def _loaded_history(path: str):
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
            bridge_name TEXT,
            county_code INTEGER,
            owner TEXT,
            maintenance_responsibility TEXT,
            year_built INTEGER,
            total_bridge_length REAL,
            bridge_width_out_to_out REAL,
            calculated_deck_area REAL,
            nhs_designation TEXT,
            deck_condition_rating REAL,
            superstructure_condition_rating REAL,
            substructure_condition_rating REAL,
            culvert_condition_rating REAL,
            lowest_condition_rating_code REAL,
            PRIMARY KEY (state_code,bridge_number,data_year)
        );
        CREATE TABLE features (
            id INTEGER PRIMARY KEY,
            state_code INTEGER, bridge_number TEXT, data_year INTEGER,
            feature_type TEXT, feature_location TEXT, nhs_designation TEXT,
            annual_average_daily_traffic INTEGER,
            annual_average_daily_truck_traffic INTEGER,
            year_of_annual_average_daily_traffic INTEGER
        );
        CREATE TABLE span_sets (
            id INTEGER PRIMARY KEY,
            state_code INTEGER, bridge_number TEXT, data_year INTEGER,
            span_configuration_designation TEXT, span_material TEXT,
            span_type TEXT, deck_material_and_type TEXT, wearing_surface TEXT
        );
        CREATE TABLE substructure_sets (
            id INTEGER PRIMARY KEY,
            state_code INTEGER, bridge_number TEXT, data_year INTEGER,
            substructure_configuration_designation TEXT,
            substructure_material TEXT, substructure_type TEXT
        );
        CREATE TABLE works (
            id INTEGER PRIMARY KEY,
            state_code INTEGER, bridge_number TEXT, data_year INTEGER,
            year_work_performed INTEGER, work_performed TEXT
        );
        """
    )
    connection.execute("INSERT INTO workspace_state VALUES ('workspace-a',7,16)")
    rows = (
        (34, "TEST-1", 2024, "Test bridge", 1, "1", "1", 1980, 100, 30, 3000, "Y", 7, 8, 8, None, 7),
        (34, "TEST-1", 2025, "Test bridge", 1, "1", "1", 1980, 100, 30, 3000, "Y", 6, 8, 8, None, 6),
    )
    connection.executemany("INSERT INTO bridges VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    connection.commit()
    connection.close()
    return load_bridge_analysis_history(
        database_path=path,
        workspace_id="workspace-a",
        state_code=34,
        bridge_number="TEST-1",
    )


def test_loaded_forecast_is_built_and_bounded_from_exact_inventory(tmp_path):
    history = _loaded_history(str(tmp_path / "inventory.sqlite3"))
    payload = _loaded_forecast_payload(
        BridgeForecastBody(
            state_code=34,
            bridge_number="TEST-1",
            as_of_year=2025,
            horizon_years=2,
            expected_data_generation=7,
        ),
        history=history,
        source_data_generation=7,
    )

    assert payload is not None
    assert payload["source_status"] == "loaded_inventory"
    assert payload["source_database"] == "snbi_bridges.db"
    assert payload["qualification"] == "research_unvalidated"
    assert [point["year"] for point in payload["metrics"]["deck"]["observed"]] == [2024, 2025]
    assert [point["year"] for point in payload["metrics"]["deck"]["forecast"]] == [2026, 2027]


def test_earlier_as_of_uses_only_inventory_available_through_that_year(tmp_path):
    history = _loaded_history(str(tmp_path / "inventory.sqlite3"))
    payload = _loaded_forecast_payload(
        BridgeForecastBody(
            state_code=34,
            bridge_number="TEST-1",
            as_of_year=2024,
            horizon_years=2,
            expected_data_generation=7,
        ),
        history=history,
        source_data_generation=7,
    )

    assert payload is not None
    assert [point["year"] for point in payload["metrics"]["deck"]["observed"]] == [2024]
    assert [point["year"] for point in payload["metrics"]["deck"]["forecast"]] == [2025, 2026]
    assert all(
        point["snapshot_revision_id"] == "loaded-inventory:34:TEST-1:2024"
        for point in payload["metrics"]["deck"]["observed"]
    )


def test_historical_test_keeps_later_actual_values_out_of_markov_training(tmp_path):
    database = str(tmp_path / "inventory.sqlite3")
    first_history = _loaded_history(database)
    body = BridgeBacktestBody(
        state_code=34,
        bridge_number="TEST-1",
        start_year=2024,
        expected_data_generation=7,
    )
    first = _loaded_backtest_payload(
        body,
        history=first_history,
        source_data_generation=7,
    )

    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE bridges SET deck_condition_rating=1 WHERE bridge_number='TEST-1' AND data_year=2025"
    )
    connection.commit()
    connection.close()
    changed_history = load_bridge_analysis_history(
        database_path=database,
        workspace_id="workspace-a",
        state_code=34,
        bridge_number="TEST-1",
    )
    changed = _loaded_backtest_payload(
        body,
        history=changed_history,
        source_data_generation=7,
    )

    assert first is not None and changed is not None
    assert first["test_start_year"] == 2024
    assert first["latest_actual_year"] == 2025
    assert first["available_start_years"] == [2024]
    assert first["metrics"]["deck"]["forecast"] == changed["metrics"]["deck"]["forecast"]
    assert first["metrics"]["deck"]["observed"][-1]["value"] == 6.0
    assert changed["metrics"]["deck"]["observed"][-1]["value"] == 1.0
    assert first["comparison"]["deck"]["compared_years"] == [2025]


def test_historical_test_requires_a_year_before_latest_actual(tmp_path):
    history = _loaded_history(str(tmp_path / "inventory.sqlite3"))

    with pytest.raises(ForecastApiError) as error:
        _loaded_backtest_payload(
            BridgeBacktestBody(
                state_code=34,
                bridge_number="TEST-1",
                start_year=2025,
                expected_data_generation=7,
            ),
            history=history,
            source_data_generation=7,
        )

    assert error.value.status == 422
    assert error.value.code == "backtest_start_year_not_historical"
