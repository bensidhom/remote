from __future__ import annotations

import sqlite3

import pytest

from app.services.analytics.life_history import EvidenceClass
from app.services.analytics.projection import (
    AnalyticalProjectionError,
    FeatureSelectionStatus,
    load_bridge_analysis_history,
)
from app.services.lcca_v2.facts import bridge_lcca_facts


def _create_fixture(path: str) -> None:
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
            PRIMARY KEY (state_code, bridge_number, data_year)
        );
        CREATE TABLE features (
            id INTEGER PRIMARY KEY,
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            data_year INTEGER NOT NULL,
            feature_type TEXT,
            feature_location TEXT,
            nhs_designation TEXT,
            annual_average_daily_traffic INTEGER,
            annual_average_daily_truck_traffic INTEGER,
            year_of_annual_average_daily_traffic INTEGER
        );
        CREATE TABLE span_sets (
            id INTEGER PRIMARY KEY,
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            data_year INTEGER NOT NULL,
            span_configuration_designation TEXT,
            span_material TEXT,
            span_type TEXT,
            deck_material_and_type TEXT,
            wearing_surface TEXT
        );
        CREATE TABLE substructure_sets (
            id INTEGER PRIMARY KEY,
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            data_year INTEGER NOT NULL,
            substructure_configuration_designation TEXT,
            substructure_material TEXT,
            substructure_type TEXT
        );
        CREATE TABLE works (
            id INTEGER PRIMARY KEY,
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            data_year INTEGER NOT NULL,
            year_work_performed INTEGER,
            work_performed TEXT
        );
        """
    )
    connection.executemany(
        "INSERT INTO workspace_state VALUES (?,?,?,?)",
        (("workspace-a", 7, 8, "2026-08-14T00:00:00Z"), ("workspace-b", 2, 8, "2026-08-14T00:00:00Z")),
    )
    bridge_rows = (
        (34, "0101150", 2020, "Test Bridge", 1, "1", "1", 1980, 100.0, 30.0, 3000.0, "Y", 7, 8, 8, None, 7),
        (34, "0101150", 2022, "Test Bridge", 1, "1", "1", 1980, 100.0, 30.0, 3000.0, "Y", 6, 8, 8, None, 6),
        (34, "0101150", 2024, "Test Bridge", 1, "1", "1", 1980, 100.0, 30.0, 3000.0, "Y", 7, 8, 8, None, 7),
        (34, "0101150", 2025, "Unpublished", 1, "1", "1", 1980, 100.0, 30.0, 3000.0, "Y", 9, 9, 9, None, 9),
    )
    connection.executemany(
        "INSERT INTO bridges VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        bridge_rows,
    )
    for year in (2020, 2022, 2024):
        revision_id = f"revision-{year}"
        connection.execute(
            "INSERT INTO snapshot_revisions VALUES (?,?,?,?,?,?)",
            (revision_id, "workspace-a", 34, "0101150", year, f"{year - 2000:02x}" * 32),
        )
        connection.execute(
            "INSERT INTO published_snapshots VALUES (?,?,?,?,?,?,?)",
            (f"published-{year}", "workspace-a", 34, "0101150", year, revision_id, f"{year}-12-31T00:00:00Z"),
        )
    connection.execute(
        "INSERT INTO snapshot_revisions VALUES (?,?,?,?,?,?)",
        ("revision-other", "workspace-b", 34, "0101150", 2024, "ff" * 32),
    )
    connection.execute(
        "INSERT INTO published_snapshots VALUES (?,?,?,?,?,?,?)",
        ("published-other", "workspace-b", 34, "0101150", 2024, "revision-other", "2024-12-31T00:00:00Z"),
    )
    connection.executemany(
        "INSERT INTO features VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            (1, 34, "0101150", 2020, "H01", "C", "Y", 9000, 900, 2020),
            (2, 34, "0101150", 2022, "H01", "C", "Y", 10000, 1000, 2022),
            (3, 34, "0101150", 2024, "H01", "C", "Y", 11000, 1100, 2024),
            (4, 34, "0101150", 2024, "H02", "C", "N", 12000, 1200, 2024),
        ),
    )
    connection.execute(
        "INSERT INTO span_sets VALUES (?,?,?,?,?,?,?,?,?)",
        (1, 34, "0101150", 2024, "1", "S", "01", "CR-T", "ASPHALT"),
    )
    connection.execute(
        "INSERT INTO substructure_sets VALUES (?,?,?,?,?,?,?)",
        (1, 34, "0101150", 2024, "1", "C", "01"),
    )
    connection.execute(
        "INSERT INTO works VALUES (?,?,?,?,?,?)",
        (1, 34, "0101150", 2024, 2023, "DECK REHABILITATION"),
    )
    connection.commit()
    connection.close()


def test_projection_reads_only_exact_workspace_publications(tmp_path):
    database = tmp_path / "analytics.db"
    _create_fixture(str(database))
    result = load_bridge_analysis_history(
        database_path=database,
        workspace_id="workspace-a",
        state_code=34,
        bridge_number="0101150",
    )
    assert [snapshot.data_year for snapshot in result.snapshots] == [2020, 2022, 2024]
    assert all(snapshot.identity.workspace_id == "workspace-a" for snapshot in result.snapshots)
    assert all(snapshot.bridge_number == "0101150" for snapshot in result.snapshots)
    assert result.snapshots[-1].identity.data_generation == 7
    assert result.snapshots[-1].bridge_name == "Test Bridge"


def test_projection_uses_snapshot_age_and_exact_feature_evidence(tmp_path):
    database = tmp_path / "analytics.db"
    _create_fixture(str(database))
    result = load_bridge_analysis_history(
        database_path=database,
        workspace_id="workspace-a",
        state_code=34,
        bridge_number="0101150",
    )
    assert [snapshot.age_at_snapshot for snapshot in result.snapshots] == [40, 42, 44]
    assert result.snapshots[1].carried_features.status is FeatureSelectionStatus.SELECTED
    assert result.snapshots[1].carried_features.selected is not None
    assert result.snapshots[1].carried_features.selected.adtt == 1000
    assert result.snapshots[2].carried_features.status is FeatureSelectionStatus.AMBIGUOUS
    assert result.snapshots[2].carried_features.selected is None
    assert len(result.snapshots[2].carried_features.candidates) == 2


def test_projection_retains_materials_condition_and_life_history_provenance(tmp_path):
    database = tmp_path / "analytics.db"
    _create_fixture(str(database))
    result = load_bridge_analysis_history(
        database_path=database,
        workspace_id="workspace-a",
        state_code=34,
        bridge_number="0101150",
    )
    latest = result.snapshots[-1]
    assert latest.span_materials[0].deck_material_code == "CR-T"
    assert latest.span_materials[0].span_material_code == "S"
    assert latest.substructure_materials[0].material_code == "C"
    assert latest.condition.controlling_rating == 7
    assert latest.condition.condition_class == "Good"

    work_events = tuple(
        event for event in result.life_history.events
        if event.evidence_class is EvidenceClass.INVENTORY_REPORTED_WORK
    )
    assert len(work_events) == 1
    assert work_events[0].event_year == 2023
    assert work_events[0].source_record_ids == ("revision-2024",)
    candidates = result.life_history.condition_change_candidates
    assert len(candidates) == 1
    assert candidates[0].component == "deck"
    assert candidates[0].before_rating == 6
    assert candidates[0].after_rating == 7
    assert result.life_history.observed_events == ()
    assert result.life_history.unknown_history_interval == (1980, 2019)


def test_projection_through_year_is_fail_closed_and_deterministic(tmp_path):
    database = tmp_path / "analytics.db"
    _create_fixture(str(database))
    first = load_bridge_analysis_history(
        database_path=database,
        workspace_id="workspace-a",
        state_code=34,
        bridge_number="0101150",
        through_year=2022,
    )
    second = load_bridge_analysis_history(
        database_path=database,
        workspace_id="workspace-a",
        state_code=34,
        bridge_number="0101150",
        through_year=2022,
    )
    assert first == second
    assert [snapshot.data_year for snapshot in first.snapshots] == [2020, 2022]

    with pytest.raises(AnalyticalProjectionError, match="not found"):
        load_bridge_analysis_history(
            database_path=database,
            workspace_id="workspace-a",
            state_code=34,
            bridge_number="missing",
        )


def test_projection_does_not_open_missing_database_for_write(tmp_path):
    database = tmp_path / "missing.db"
    with pytest.raises(AnalyticalProjectionError, match="database"):
        load_bridge_analysis_history(
            database_path=database,
            workspace_id="workspace-a",
            state_code=34,
            bridge_number="0101150",
        )
    assert not database.exists()


def test_lcca_facts_use_exact_snapshot_geometry_and_carried_feature(tmp_path):
    database = tmp_path / "analytics.db"
    _create_fixture(str(database))
    history = load_bridge_analysis_history(
        database_path=database,
        workspace_id="workspace-a",
        state_code=34,
        bridge_number="0101150",
        through_year=2022,
    )
    facts = bridge_lcca_facts(history.snapshots[-1])
    assert facts.snapshot_revision_id == "revision-2022"
    assert facts.data_year == 2022
    assert facts.geometry.deck_area == 3000.0
    assert facts.geometry.method == "reported_calculated_deck_area"
    assert facts.nhs.value is True
    assert facts.nhs.source == "selected_carried_highway_feature"
