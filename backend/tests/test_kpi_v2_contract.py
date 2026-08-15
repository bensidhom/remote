from __future__ import annotations

import sqlite3

from app.services.kpis_v2.registry import (
    ACCEPTABLE_DEFINITION_VERSION,
    DEFAULT_KPI_REGISTRY,
)
from app.services.kpis_v2.repository import (
    compute_age_condition_area,
    compute_operational_kpis,
)
from app.services.kpis_v2.fhwa_tpm import compute_fhwa_tpm_diagnostic


def _fixture(path: str) -> None:
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
            id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL, data_year INTEGER NOT NULL, canonical_sha256 TEXT NOT NULL
        );
        CREATE TABLE published_snapshots (
            id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL, data_year INTEGER NOT NULL,
            snapshot_revision_id TEXT NOT NULL, published_at TEXT NOT NULL
        );
        CREATE TABLE bridges (
            state_code INTEGER NOT NULL, bridge_number TEXT NOT NULL, data_year INTEGER NOT NULL,
            bridge_name TEXT, county_code INTEGER, owner TEXT, maintenance_responsibility TEXT,
            year_built INTEGER, nbis_bridge_length REAL, total_bridge_length REAL,
            bridge_width_out_to_out REAL, calculated_deck_area REAL,
            deck_condition_rating REAL, superstructure_condition_rating REAL,
            substructure_condition_rating REAL, culvert_condition_rating REAL,
            lowest_condition_rating_code REAL,
            PRIMARY KEY(state_code,bridge_number,data_year)
        );
        CREATE TABLE features (
            id INTEGER PRIMARY KEY, state_code INTEGER NOT NULL, bridge_number TEXT NOT NULL,
            data_year INTEGER NOT NULL, feature_type TEXT, feature_location TEXT,
            annual_average_daily_traffic REAL
        );
        CREATE TABLE ref_counties (
            state_code INTEGER NOT NULL, county_code INTEGER NOT NULL, county_name TEXT NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO workspace_state VALUES ('workspace-a',12,10,'2026-08-14T00:00:00Z')"
    )
    connection.executemany(
        "INSERT INTO ref_counties VALUES (34,?,?)",
        ((1, "Atlantic"), (3, "Bergen")),
    )
    rows = (
        ("A", 1, "S01", 2000, 25.0, 100.0, 30.0, 3000.0, 7, 8, 8, None, 7),
        ("B", 1, "S01", 1990, 25.0, 100.0, 20.0, None, 5, 6, 6, None, 5),
        ("C", 3, "S01", 1980, 25.0, 100.0, 10.0, 1000.0, None, None, None, None, None),
        ("D", None, "L01", None, 25.0, 100.0, None, None, 4, 6, 6, None, 4),
        ("E", 3, "S01", 2024, 19.9, 40.0, 10.0, 400.0, 8, 8, 8, None, 8),
    )
    for ordinal, row in enumerate(rows, 1):
        bridge, county, maintenance, built, bg01, length, width, area, bc01, bc02, bc03, bc04, bc13 = row
        revision = f"revision-{bridge}"
        connection.execute(
            "INSERT INTO snapshot_revisions VALUES (?,?,?,?,?,?)",
            (revision, "workspace-a", 34, bridge, 2024, f"{ordinal:02x}" * 32),
        )
        connection.execute(
            "INSERT INTO published_snapshots VALUES (?,?,?,?,?,?,?)",
            (f"published-{bridge}", "workspace-a", 34, bridge, 2024, revision, "2024-12-31T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO bridges VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (34, bridge, 2024, bridge, county, "S01", maintenance, built, bg01, length, width, area, bc01, bc02, bc03, bc04, bc13),
        )
    connection.executemany(
        "INSERT INTO features VALUES (?,?,?,?,?,?,?)",
        (
            (1, 34, "A", 2024, "H01", "C", 30000),
            (2, 34, "B", 2024, "H01", "C", 10000),
            (3, 34, "C", 2024, "H01", "C", 20000),
            (4, 34, "C", 2024, "H02", "C", 21000),
        ),
    )
    connection.commit()
    connection.close()


def test_kpi_registry_is_complete_deterministic_and_labels_candidate_definition():
    assert len(DEFAULT_KPI_REGISTRY.sha256) == 64
    assert DEFAULT_KPI_REGISTRY.as_dict() == DEFAULT_KPI_REGISTRY.as_dict()
    definition = DEFAULT_KPI_REGISTRY.get("state_maintained_20ft_acceptable")
    assert definition.definition_version == ACCEPTABLE_DEFINITION_VERSION
    assert "pending_agency_approval" in definition.status
    assert definition.denominator
    assert definition.source_fields == (
        "BCL02", "BG01", "BC01", "BC02", "BC03", "BC04", "BC13"
    )


def test_operational_kpis_reconcile_counts_areas_unknowns_and_state_policy(tmp_path):
    database = tmp_path / "kpis.db"
    _fixture(str(database))

    report = compute_operational_kpis(
        database_path=database,
        workspace_id="workspace-a",
        state_code=34,
        year=2024,
    )

    assert report["data_generation"] == 12
    assert report["schema_generation"] == 10
    assert len(str(report["revision_set_sha256"])) == 64
    values = report["values"]
    assert values["total_bridges"] == 5
    assert values["condition_counts"] == {"Good": 2, "Fair": 1, "Poor": 1, "Unknown": 1}
    assert sum(values["county_counts"].values()) == 5
    assert values["county_counts"]["Unknown county"] == 1
    assert values["total_valid_deck_area_ft2"] == 6400.0
    assert values["deck_area_method_counts"] == {
        "reported_bg15": 3,
        "derived_bg02_times_bg05": 1,
        "missing_or_invalid_geometry": 1,
    }
    assert values["high_traffic_bridge_count"] == 1
    assert values["carried_aadt_ambiguous_count"] == 1

    count_measure = values["state_maintained_20ft_acceptable"]
    assert count_measure["eligible_count"] == 3
    assert count_measure["classifiable_denominator"] == 2
    assert count_measure["acceptable_numerator"] == 2
    assert count_measure["unknown_condition_count"] == 1
    assert count_measure["percent_classifiable"] == 100.0
    assert count_measure["percent_all_eligible"] == 66.7

    area_measure = values["state_maintained_deck_area_acceptable"]
    assert area_measure["valid_classifiable_area_denominator_ft2"] == 5000.0
    assert area_measure["acceptable_area_numerator_ft2"] == 5000.0
    assert area_measure["unknown_condition_area_ft2"] == 1000.0
    assert area_measure["percent_all_valid_area"] == 83.3


def test_age_condition_area_uses_snapshot_age_and_reconciles_bins(tmp_path):
    database = tmp_path / "kpis.db"
    _fixture(str(database))

    result = compute_age_condition_area(
        database_path=database,
        workspace_id="workspace-a",
        state_code=34,
        year=2024,
        population="statewide",
        include_unknown=True,
    )

    by_bin = {row["age_bin"]: row for row in result["series"]}
    assert by_bin["0-4"]["Good"] == 400.0
    assert by_bin["20-24"]["Good"] == 3000.0
    assert by_bin["30-34"]["Lower Fair"] == 2000.0
    assert by_bin["40-44"]["Unknown"] == 1000.0
    assert result["excluded"]["missing_or_invalid_age_count"] == 1
    assert sum(row["total_ft2"] for row in result["series"]) == 6400.0


def _tpm_fixture(path: str, year: int) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE workspace_state (workspace_id TEXT PRIMARY KEY,data_generation INTEGER,schema_generation INTEGER,updated_at TEXT);
        CREATE TABLE snapshot_revisions (id TEXT PRIMARY KEY,workspace_id TEXT,state_code INTEGER,bridge_number TEXT,data_year INTEGER,canonical_sha256 TEXT);
        CREATE TABLE published_snapshots (id TEXT PRIMARY KEY,workspace_id TEXT,state_code INTEGER,bridge_number TEXT,data_year INTEGER,snapshot_revision_id TEXT,published_at TEXT);
        CREATE TABLE bridges (
          state_code INTEGER,bridge_number TEXT,data_year INTEGER,nbis_bridge_length REAL,
          total_bridge_length REAL,bridge_width_out_to_out REAL,approach_roadway_width REAL,
          irregular_deck_area REAL,border_bridge_number TEXT,border_bridge_designated_lead_state TEXT,
          deck_condition_rating REAL,superstructure_condition_rating REAL,
          substructure_condition_rating REAL,culvert_condition_rating REAL,lowest_condition_rating_code REAL
        );
        CREATE TABLE features (id INTEGER PRIMARY KEY,state_code INTEGER,bridge_number TEXT,data_year INTEGER,feature_type TEXT,feature_location TEXT,nhs_designation TEXT);
        """
    )
    connection.execute("INSERT INTO workspace_state VALUES ('workspace-a',8,10,'now')")
    rows = (
        ("A", 25.0, 100.0, 30.0, None, None, None, None, 7, 8, 8, None, 7, "H01", "Y"),
        ("B", 25.0, 100.0, None, 20.0, None, None, None, 4, 6, 6, None, 4, "H01", "Y"),
        ("C", 25.0, 100.0, 20.0, None, None, None, None, 7, 7, 7, None, 7, "H01", "N"),
        ("D", 25.0, 100.0, 20.0, None, None, None, None, None, None, None, None, None, "H01", "Y"),
        ("E", 25.0, 100.0, 20.0, None, None, "OTHER", "36", 7, 7, 7, None, 7, "H02", "Y"),
        ("F", 20.0, 100.0, 20.0, None, None, None, None, 7, 7, 7, None, 7, "H01", "Y"),
        ("G", 25.0, 10.05, 10.0, None, 555.0, None, None, 6, 7, 7, None, 6, "H03", "Y"),
    )
    for ordinal, row in enumerate(rows, 1):
        bridge, bg01, bg02, bg05, bg09, bg15, border, lead, bc01, bc02, bc03, bc04, bc13, feature, nhs = row
        revision = f"r-{bridge}"
        connection.execute("INSERT INTO snapshot_revisions VALUES (?,?,?,?,?,?)", (revision, "workspace-a", 34, bridge, year, f"{ordinal:02x}" * 32))
        connection.execute("INSERT INTO published_snapshots VALUES (?,?,?,?,?,?,?)", (f"p-{bridge}", "workspace-a", 34, bridge, year, revision, "now"))
        connection.execute("INSERT INTO bridges VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (34, bridge, year, bg01, bg02, bg05, bg09, bg15, border, lead, bc01, bc02, bc03, bc04, bc13))
        connection.execute("INSERT INTO features VALUES (?,?,?,?,?,?,?)", (ordinal, 34, bridge, year, feature, "C", nhs))
    connection.commit()
    connection.close()


def test_fhwa_transition_diagnostic_uses_nhs_area_rounding_and_exclusions(tmp_path):
    database = tmp_path / "tpm.db"
    _tpm_fixture(str(database), 2026)
    result = compute_fhwa_tpm_diagnostic(
        database_path=database,
        workspace_id="workspace-a",
        state_code=34,
        year=2026,
    )
    assert result["definition_version"] == "fhwa-hif-25-010-transition-v1"
    assert result["official_validation"] is False
    assert result["submission_ready"] is False
    assert result["areas_ft2"] == {"good": "3000", "fair": "101", "poor": "2000"}
    assert result["total_applicable_deck_area_ft2"] == "5101"
    assert result["good_deck_area_percent"] == "58.8"
    assert result["poor_deck_area_percent"] == "39.2"
    assert result["area_method_counts"] == {"bg02_times_bg05": 2, "bg02_times_bg09": 1}
    assert result["exclusions"] == {
        "border_other_lead_state": 1,
        "condition_missing_or_invalid": 1,
        "no_qualifying_carried_nhs_highway_feature": 1,
        "not_nbis_length_over_20ft": 1,
    }
    assert result["population"]["qualifying_ramp_feature_count"] == 2


def test_fhwa_2028_profile_prefers_irregular_deck_area(tmp_path):
    database = tmp_path / "tpm.db"
    _tpm_fixture(str(database), 2028)
    result = compute_fhwa_tpm_diagnostic(
        database_path=database,
        workspace_id="workspace-a",
        state_code=34,
        year=2028,
    )
    assert result["definition_version"] == "fhwa-hif-25-010-snbi-v1"
    assert result["areas_ft2"]["fair"] == "555"
    assert result["area_method_counts"]["reported_bg15"] == 1
