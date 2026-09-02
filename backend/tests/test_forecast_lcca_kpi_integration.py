from __future__ import annotations

import sqlite3

from app.services.kpis_v2.repository import compute_operational_kpis


def _database(path: str) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE workspace_state (
          workspace_id TEXT PRIMARY KEY,
          data_generation INTEGER NOT NULL,
          schema_generation INTEGER NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE snapshot_revisions (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL,
          state_code INTEGER NOT NULL,
          bridge_number TEXT NOT NULL,
          data_year INTEGER NOT NULL,
          canonical_sha256 TEXT NOT NULL
        );
        CREATE TABLE published_snapshots (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL,
          state_code INTEGER NOT NULL,
          bridge_number TEXT NOT NULL,
          data_year INTEGER NOT NULL,
          snapshot_revision_id TEXT NOT NULL,
          published_at TEXT NOT NULL
        );
        CREATE TABLE bridges (
          state_code INTEGER NOT NULL,
          bridge_number TEXT NOT NULL,
          data_year INTEGER NOT NULL,
          county_code INTEGER,
          maintenance_responsibility TEXT,
          year_built INTEGER,
          nbis_bridge_length REAL,
          total_bridge_length REAL,
          bridge_width_out_to_out REAL,
          calculated_deck_area REAL,
          deck_condition_rating REAL,
          superstructure_condition_rating REAL,
          substructure_condition_rating REAL,
          culvert_condition_rating REAL,
          lowest_condition_rating_code REAL,
          PRIMARY KEY(state_code,bridge_number,data_year)
        );
        CREATE TABLE features (
          id INTEGER PRIMARY KEY,
          state_code INTEGER NOT NULL,
          bridge_number TEXT NOT NULL,
          data_year INTEGER NOT NULL,
          feature_type TEXT,
          feature_location TEXT,
          annual_average_daily_traffic REAL
        );
        CREATE TABLE ref_counties (
          state_code INTEGER NOT NULL,
          county_code INTEGER NOT NULL,
          county_name TEXT NOT NULL
        );
        CREATE TABLE forecast_predictions (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL,
          forecast_year INTEGER NOT NULL,
          expected_rating REAL NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO workspace_state VALUES ('workspace-a',4,10,'2026-08-14T00:00:00Z')"
    )
    connection.execute("INSERT INTO ref_counties VALUES (34,1,'Atlantic')")
    for year, rating in ((2023, 8), (2024, 7)):
        revision = f"revision-a-{year}-1"
        connection.execute(
            "INSERT INTO snapshot_revisions VALUES (?,?,?,?,?,?)",
            (revision, "workspace-a", 34, "A", year, f"{year % 256:02x}" * 32),
        )
        connection.execute(
            "INSERT INTO published_snapshots VALUES (?,?,?,?,?,?,?)",
            (f"published-a-{year}", "workspace-a", 34, "A", year, revision, "now"),
        )
        connection.execute(
            "INSERT INTO bridges VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (34, "A", year, 1, "S01", 2000, 25.0, 100.0, 30.0, 3000.0, rating, rating, rating, None, rating),
        )
        connection.execute(
            "INSERT INTO features VALUES (?,?,?,?,?,?,?)",
            (year, 34, "A", year, "H01", "C", 25000),
        )
    connection.commit()
    connection.close()


def _report(path, year: int):
    return compute_operational_kpis(
        database_path=path,
        workspace_id="workspace-a",
        state_code=34,
        year=year,
    )


def test_forecasts_and_publication_pointers_never_enter_loaded_inventory_kpis(tmp_path):
    database = tmp_path / "analytics.sqlite3"
    _database(str(database))
    historical_before = _report(database, 2023)
    current_before = _report(database, 2024)

    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO forecast_predictions VALUES (?,?,?,?)",
        ("prediction-a-2025", "workspace-a", 2025, 6.4),
    )
    connection.commit()
    connection.close()

    # A modeled future state is not an observed bridge snapshot and cannot alter
    # any operational KPI value or its exact loaded-inventory receipt.
    assert _report(database, 2024) == current_before

    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO snapshot_revisions VALUES (?,?,?,?,?,?)",
        ("revision-a-2024-2", "workspace-a", 34, "A", 2024, "ef" * 32),
    )
    connection.execute(
        "UPDATE published_snapshots SET snapshot_revision_id='revision-a-2024-2' "
        "WHERE workspace_id='workspace-a' AND state_code=34 AND bridge_number='A' AND data_year=2024"
    )
    connection.execute(
        "UPDATE workspace_state SET data_generation=data_generation+1 WHERE workspace_id='workspace-a'"
    )
    connection.commit()
    connection.close()

    current_after = _report(database, 2024)
    historical_after = _report(database, 2023)
    assert current_after["data_generation"] == current_before["data_generation"] + 1
    assert current_after["revision_set_sha256"] == current_before["revision_set_sha256"]
    assert current_after["values"] == current_before["values"]
    # The global generation signals staleness, while the untouched historical
    # population retains its exact revision set and observed values.
    assert historical_after["revision_set_sha256"] == historical_before["revision_set_sha256"]
    assert historical_after["values"] == historical_before["values"]


def test_history_ui_uses_only_exact_revision_forecast_and_lcca_for_rehabilitation():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    history = (root / "frontend/src/components/BridgeHistoryModal.tsx").read_text(
        encoding="utf-8"
    )
    detail = (root / "frontend/src/components/BridgeDetailModal.tsx").read_text(
        encoding="utf-8"
    )
    lcca = (root / "frontend/src/components/BridgeLCCAModal.tsx").read_text(
        encoding="utf-8"
    )
    map_page = (root / "frontend/src/pages/Map.tsx").read_text(encoding="utf-8")

    assert "'/api/forecasts/v2/bridge'" in history
    assert "'/api/forecasts/v2/bridge/backtest'" in history
    assert "expected_data_generation" in history
    assert "as_of_year: requestedYear" in history
    assert "Test a past year" in history
    assert "later real values were not used to produce them" in history
    assert "Loaded source:" not in history
    assert "Forecast package:" not in history
    assert "Historical evaluation:" not in history
    assert "research unvalidated" not in history
    assert "/api/bridge-history/" not in history
    assert "/api/forecasts/train-bridge/" not in history
    assert "/api/forecasts/bridge/" not in history
    assert "Rehabilitation and maintenance simulations are in the LCCA tab" in history
    assert "dataYear={bridge.data_year}" in detail
    assert "dataYear={historyModal.data_year}" in map_page
    assert "Planning/research comparison" not in lcca
    assert "Enable illustrative treatment effects" not in lcca
    assert "Run yearly simulation" not in lcca
    assert "enable_research_effect_assumptions: true" in lcca
    assert "rehabilitation_year: rehabilitationYear" in lcca
    assert "replacement_year: replacementYear" in lcca
    assert "Results update automatically" in lcca
    assert "NJDOT defaults · editable" in lcca
    assert "No work is scheduled; the Markov model advances condition one year at a time." in lcca
    assert "Action details" in lcca
    assert "Condition, cost, and actions" in lcca
    assert "every strategy selected above" in lcca
    assert "Cumulative present value" in lcca
    assert 'yAxisId="condition"' in lcca
    assert 'yAxisId="money"' in lcca
    assert "Condition and actions" not in lcca
    assert "Money and actions" not in lcca
    assert "Scenario receipt:" not in lcca
    assert "Forecast status" not in lcca
