from __future__ import annotations

import sqlite3

from app.routers.services.database import ensure_schema
from app.routers.services.db_schema import (
    LATEST_SCHEMA_VERSION,
    QUERY_AUDIT_TABLE,
    QUERY_SEMANTIC_VIEWS,
    REF_COUNTIES_TABLE,
    assert_schema_matches,
    assert_table_matches,
)


WORKSPACE = "shared-research-workspace"


def _insert_workspace(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO workspaces (id,slug,display_name,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (WORKSPACE, WORKSPACE, "Shared research", "active", "2026-08-13T00:00:00Z", "2026-08-13T00:00:00Z"),
    )
    connection.execute(
        "INSERT INTO workspace_state "
        "(workspace_id,data_generation,schema_generation,updated_at) VALUES (?,?,?,?)",
        (WORKSPACE, 0, LATEST_SCHEMA_VERSION, "2026-08-13T00:00:00Z"),
    )


def _insert_published_snapshot(
    connection: sqlite3.Connection,
    *,
    revision_id: str,
    bridge_number: str,
    year: int,
    county: int = 1,
    bc01: float | None = 7,
    bc02: float | None = 6,
    bc03: float | None = 8,
    bc04: float | None = None,
    bc13: float | None = None,
) -> None:
    connection.execute(
        "INSERT INTO bridges "
        "(state_code,bridge_number,bridge_name,county_code,data_year,year_built,"
        "total_bridge_length,bridge_width_out_to_out,deck_condition_rating,"
        "superstructure_condition_rating,substructure_condition_rating,"
        "culvert_condition_rating,lowest_condition_rating_code) "
        "VALUES (34,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            bridge_number,
            f"Bridge {bridge_number}",
            county,
            year,
            1970,
            100.0,
            20.0,
            bc01,
            bc02,
            bc03,
            bc04,
            bc13,
        ),
    )
    connection.execute(
        "INSERT INTO snapshot_revisions "
        "(id,workspace_id,state_code,bridge_number,data_year,revision_number,"
        "lifecycle_state,canonical_json,canonical_sha256,profile_id,"
        "rule_bundle_sha256,created_by,created_at) "
        "VALUES (?,?,?,?,?,1,'staged','{}',?,'research',?,'tester',?)",
        (
            revision_id,
            WORKSPACE,
            34,
            bridge_number,
            year,
            revision_id.rjust(64, "0")[-64:],
            "a" * 64,
            "2026-08-13T00:00:00Z",
        ),
    )
    connection.execute(
        "INSERT INTO published_snapshots "
        "(id,workspace_id,state_code,bridge_number,data_year,snapshot_revision_id,"
        "published_by,published_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            f"pub-{revision_id}",
            WORKSPACE,
            34,
            bridge_number,
            year,
            revision_id,
            "tester",
            "2026-08-13T00:00:00Z",
        ),
    )


def test_query_boundary_migration_is_manifest_owned_and_seeded(test_workspace):
    database_path = test_workspace / "query-schema.sqlite3"
    ensure_schema(str(database_path))
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (
            LATEST_SCHEMA_VERSION,
        )
        assert_table_matches(connection, REF_COUNTIES_TABLE)
        assert_table_matches(connection, QUERY_AUDIT_TABLE)
        assert connection.execute(
            "SELECT county_name FROM ref_counties "
            "WHERE state_code=34 AND county_code=1"
        ).fetchone() == ("Atlantic",)
        assert {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='view'"
            ).fetchall()
        }.issuperset(view.name for view in QUERY_SEMANTIC_VIEWS)
        assert_schema_matches(connection)
    finally:
        connection.close()


def test_semantic_views_ignore_publication_pointers_and_latest_is_one_global_year(test_workspace):
    database_path = test_workspace / "query-view-behavior.sqlite3"
    ensure_schema(str(database_path))
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_workspace(connection)
        _insert_published_snapshot(connection, revision_id="r1", bridge_number="A", year=2024)
        _insert_published_snapshot(connection, revision_id="r2", bridge_number="A", year=2025)
        _insert_published_snapshot(connection, revision_id="r3", bridge_number="B", year=2023)
        connection.execute(
            "INSERT INTO bridges (state_code,bridge_number,data_year,county_code) "
            "VALUES (34,'STAGED-ONLY',2026,3)"
        )
        connection.commit()

        assert connection.execute(
            "SELECT bridge_number,data_year FROM v_published_bridge_snapshots "
            "ORDER BY bridge_number,data_year"
        ).fetchall() == [("A", 2024), ("A", 2025), ("B", 2023), ("STAGED-ONLY", 2026)]
        assert connection.execute(
            "SELECT bridge_number,data_year FROM v_current_bridge_snapshots "
            "ORDER BY bridge_number"
        ).fetchall() == [("STAGED-ONLY", 2026)]
    finally:
        connection.close()


def test_semantic_views_use_loaded_inventory_when_workspace_has_no_publications(test_workspace):
    database_path = test_workspace / "query-view-loaded-fallback.sqlite3"
    ensure_schema(str(database_path))
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_workspace(connection)
        connection.executemany(
            "INSERT INTO bridges (state_code,bridge_number,data_year,county_code) "
            "VALUES (34,?,?,3)",
            (("A", 2024), ("A", 2025), ("B", 2023)),
        )
        connection.commit()

        assert connection.execute(
            "SELECT bridge_number,data_year FROM v_published_bridge_snapshots "
            "ORDER BY bridge_number,data_year"
        ).fetchall() == [("A", 2024), ("A", 2025), ("B", 2023)]
        assert connection.execute(
            "SELECT bridge_number,data_year FROM v_current_bridge_snapshots "
            "ORDER BY bridge_number"
        ).fetchall() == [("A", 2025)]
    finally:
        connection.close()


def test_condition_precedence_and_county_aggregation_are_deterministic(test_workspace):
    database_path = test_workspace / "query-condition.sqlite3"
    ensure_schema(str(database_path))
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_workspace(connection)
        _insert_published_snapshot(
            connection,
            revision_id="condition-a",
            bridge_number="A",
            year=2025,
            bc01=8,
            bc02=7,
            bc03=6,
            bc13=5,
        )
        _insert_published_snapshot(
            connection,
            revision_id="condition-b",
            bridge_number="B",
            year=2025,
            bc01="N",
            bc02="N",
            bc03="N",
            bc04=4,
        )
        connection.execute(
            "INSERT INTO span_sets "
            "(state_code,bridge_number,data_year,span_configuration_designation,deck_material_and_type) "
            "VALUES (34,'A',2025,'M01','CR-T')"
        )
        connection.execute(
            "INSERT INTO span_sets "
            "(state_code,bridge_number,data_year,span_configuration_designation,deck_material_and_type) "
            "VALUES (34,'B',2025,'C01','0')"
        )
        connection.commit()

        rows = connection.execute(
            "SELECT bridge_number,controlling_rating,condition_class,"
            "condition_basis,condition_conflict FROM v_bridge_condition "
            "ORDER BY bridge_number"
        ).fetchall()
        assert rows == [
            ("A", 6.0, "Fair", "derived_primary_min", 1),
            ("B", 4.0, "Poor", "culvert_only", 0),
        ]
        assert connection.execute(
            "SELECT county_name,bridge_count,good_bridge_count,fair_bridge_count,"
            "poor_bridge_count,unknown_condition_count,total_deck_area "
            "FROM v_county_bridge_inventory"
        ).fetchone() == ("Atlantic", 2, 0, 1, 1, 0, 4000.0)
        assert connection.execute(
            "SELECT bridge_number,deck_material_and_type FROM v_bridge_span_sets "
            "ORDER BY bridge_number"
        ).fetchall() == [("A", "CR-T"), ("B", "0")]
        assert connection.execute(
            "SELECT bridge_number,span_set_count,concrete_deck_span_count,"
            "has_concrete_deck,has_known_deck_material "
            "FROM v_bridge_deck_material_summary ORDER BY bridge_number"
        ).fetchall() == [("A", 1, 1, 1, 1), ("B", 1, 0, 0, 0)]
        assert connection.execute(
            "SELECT bridge_number,span_set_count,concrete_deck_span_count,"
            "has_concrete_deck,has_known_deck_material "
            "FROM v_current_bridge_deck_material_summary ORDER BY bridge_number"
        ).fetchall() == [("A", 1, 1, 1, 1), ("B", 1, 0, 0, 0)]
    finally:
        connection.close()


def test_query_reference_and_audit_rows_are_append_only(test_workspace):
    database_path = test_workspace / "query-immutable.sqlite3"
    ensure_schema(str(database_path))
    connection = sqlite3.connect(database_path)
    try:
        _insert_workspace(connection)
        connection.execute(
            "INSERT INTO query_audit "
            "(id,workspace_id,request_id,attempt,event_type,actor_id,role,surface,"
            "question_sha256,scope_json,schema_generation,data_generation,"
            "catalog_sha256,outcome,created_at) "
            "VALUES ('audit-1',?,?,1,'accepted','actor','viewer','full',?,"
            "'{}',5,0,?,'accepted','2026-08-13T00:00:00Z')",
            (WORKSPACE, "request-1", "q" * 64, "c" * 64),
        )
        connection.commit()
        for statement in (
            "UPDATE ref_counties SET county_name='Changed' WHERE state_code=34 AND county_code=1",
            "DELETE FROM query_audit WHERE id='audit-1'",
        ):
            try:
                connection.execute(statement)
            except sqlite3.IntegrityError:
                connection.rollback()
            else:  # pragma: no cover - explicit security assertion
                raise AssertionError("immutable query evidence accepted a mutation")
    finally:
        connection.close()
