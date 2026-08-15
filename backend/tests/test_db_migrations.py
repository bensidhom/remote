from __future__ import annotations

import sqlite3

import pytest

from app.routers.services.db_migrations import (
    MIGRATIONS,
    Migration,
    MigrationChecksumError,
    MigrationExecutionError,
    MigrationStateError,
    apply_migrations,
    migrate_database,
)
from app.routers.services.db_schema import (
    AUTHORITATIVE_SCHEMA,
    CHAT_SESSION_TABLES,
    DEPLOYMENT_IDENTITY_TABLES,
    FEATURE_TAG_TO_COLUMN,
    FORECAST_ANALYTICS_TABLES,
    KPI_PROGRAM_TABLES,
    POINT_CLOUD_RUN_TABLES,
    LEGACY_FEATURES_CREATE_SQL,
    LATEST_SCHEMA_VERSION,
    PROJECTION_CHILD_TABLES,
    QUERY_RESPONSE_AUDIT_TABLE,
    REVISION_STORE_TABLES,
    SCHEMA_MIGRATIONS_TABLE,
    SchemaManifest,
    SchemaDriftError,
    assert_schema_matches,
    assert_table_matches,
    collect_schema_drift,
    table_exists,
)


def _legacy_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE bridges (
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            data_year INTEGER,
            UNIQUE(state_code, bridge_number, data_year)
        )
        """
    )
    connection.execute(LEGACY_FEATURES_CREATE_SQL)
    connection.execute(
        "CREATE INDEX idx_features_bridge_year "
        "ON features(state_code, bridge_number, data_year)"
    )
    connection.commit()
    return connection


def _columns(connection: sqlite3.Connection, table: str) -> dict[str, tuple]:
    return {
        str(row[1]): tuple(row)
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


V3_MANIFEST = SchemaManifest(
    version=3,
    tables=(SCHEMA_MIGRATIONS_TABLE, *PROJECTION_CHILD_TABLES),
)

V11_VIEWS = tuple(
    view
    for view in AUTHORITATIVE_SCHEMA.views
    if view.name
    not in {
        "v_bridge_span_sets",
        "v_bridge_deck_material_summary",
        "v_current_bridge_deck_material_summary",
    }
)


def _apply_through_v3(connection: sqlite3.Connection):
    return apply_migrations(
        connection,
        migrations=MIGRATIONS[:3],
        manifest=V3_MANIFEST,
    )


def _insert_v4_evidence_graph(connection: sqlite3.Connection) -> None:
    timestamp = "2026-08-13T12:00:00Z"
    connection.executemany(
        "INSERT INTO workspaces "
        "(id, slug, display_name, status, created_at, updated_at) "
        "VALUES (?, ?, ?, 'active', ?, ?)",
        (
            ("ws-a", "workspace-a", "Workspace A", timestamp, timestamp),
            ("ws-b", "workspace-b", "Workspace B", timestamp, timestamp),
        ),
    )
    connection.executemany(
        "INSERT INTO source_artifacts "
        "(id, workspace_id, sha256, byte_size, original_filename, media_type, "
        "artifact_kind, storage_uri, source_system, created_by, created_at) "
        "VALUES (?, 'ws-a', ?, 10, ?, 'application/json', 'nbi-over', ?, "
        "'FHWA', 'actor-1', ?)",
        (
            ("artifact-1", "a" * 64, "first.json", "internal://one", timestamp),
            ("artifact-2", "a" * 64, "repeat.json", "internal://two", timestamp),
        ),
    )
    connection.execute(
        "INSERT INTO source_records "
        "(id, workspace_id, source_artifact_id, record_ordinal, source_role, "
        "record_identity, raw_record_json, raw_record_sha256, parse_status, "
        "parse_issues_json, state_code, bridge_number, data_year, created_at) "
        "VALUES ('record-1', 'ws-a', 'artifact-1', 0, 'over', '34:B1:2025', "
        "'{}', ?, 'parsed', '[]', 34, 'B1', 2025, ?)",
        ("b" * 64, timestamp),
    )
    connection.execute(
        "INSERT INTO conversion_batches "
        "(id, workspace_id, profile_id, rule_bundle_id, rule_bundle_sha256, "
        "options_json, request_sha256, idempotency_key, actor_id, status, "
        "counts_json, requested_at, started_at, completed_at) "
        "VALUES ('batch-1', 'ws-a', 'research', 'bundle-1', ?, '{}', ?, "
        "'request-1', 'actor-1', 'completed', '{}', ?, ?, ?)",
        ("c" * 64, "d" * 64, timestamp, timestamp, timestamp),
    )
    connection.execute(
        "INSERT INTO eligibility_decisions "
        "(id, workspace_id, conversion_batch_id, source_record_id, decision, "
        "reason_code, reason_detail, evidence_json, created_at) "
        "VALUES ('decision-1', 'ws-a', 'batch-1', 'record-1', 'include', "
        "'eligible', NULL, '{}', ?)",
        (timestamp,),
    )
    connection.execute(
        "INSERT INTO snapshot_revisions "
        "(id, workspace_id, conversion_batch_id, source_record_id, state_code, "
        "bridge_number, data_year, revision_number, lifecycle_state, "
        "canonical_json, canonical_sha256, parent_revision_id, profile_id, "
        "rule_bundle_sha256, created_by, created_at) "
        "VALUES ('revision-1', 'ws-a', 'batch-1', 'record-1', 34, 'B1', "
        "2025, 1, 'staged', '{}', ?, NULL, 'research', ?, 'actor-1', ?)",
        ("e" * 64, "c" * 64, timestamp),
    )
    connection.execute(
        "INSERT INTO snapshot_revisions "
        "(id, workspace_id, conversion_batch_id, source_record_id, state_code, "
        "bridge_number, data_year, revision_number, lifecycle_state, "
        "canonical_json, canonical_sha256, parent_revision_id, profile_id, "
        "rule_bundle_sha256, created_by, created_at) "
        "VALUES ('revision-2', 'ws-a', 'batch-1', 'record-1', 34, 'B1', "
        "2025, 2, 'staged', '{\"revision\":2}', ?, 'revision-1', "
        "'research', ?, 'actor-1', ?)",
        ("f" * 64, "c" * 64, timestamp),
    )
    connection.execute(
        "INSERT INTO snapshot_revision_sources "
        "(id, workspace_id, snapshot_revision_id, source_record_id, "
        "source_ordinal, source_role) "
        "VALUES ('revision-source-1', 'ws-a', 'revision-1', 'record-1', 0, 'over')"
    )
    connection.execute(
        "INSERT INTO published_snapshots "
        "(id, workspace_id, state_code, bridge_number, data_year, "
        "snapshot_revision_id, published_by, published_at) "
        "VALUES ('publication-1', 'ws-a', 34, 'B1', 2025, 'revision-1', "
        "'actor-1', ?)",
        (timestamp,),
    )
    connection.execute(
        "INSERT INTO field_lineage "
        "(id, workspace_id, snapshot_revision_id, target_pointer, "
        "lineage_ordinal, source_record_id, source_pointer, rule_id, "
        "resolution_status, value_json, evidence_json, created_at) "
        "VALUES ('lineage-1', 'ws-a', 'revision-1', '/BID01', 0, "
        "'record-1', '/BID01', 'direct', 'resolved', '\"B1\"', '{}', ?)",
        (timestamp,),
    )
    connection.execute(
        "INSERT INTO validation_runs "
        "(id, workspace_id, snapshot_revision_id, profile_id, rule_bundle_id, "
        "rule_bundle_sha256, validator_version, status, canonical_sha256, "
        "summary_json, started_at, completed_at) "
        "VALUES ('validation-1', 'ws-a', 'revision-1', 'research', "
        "'bundle-1', ?, 'validator-1', 'complete', ?, '{}', ?, ?)",
        ("c" * 64, "e" * 64, timestamp, timestamp),
    )
    connection.execute(
        "INSERT INTO validation_issues "
        "(id, workspace_id, validation_run_id, issue_ordinal, severity, code, "
        "json_pointer, message, evidence_json, created_at) "
        "VALUES ('issue-1', 'ws-a', 'validation-1', 0, 'warning', "
        "'TEST', '/BID01', 'test issue', '{}', ?)",
        (timestamp,),
    )
    connection.execute(
        "INSERT INTO mutation_audit "
        "(id, workspace_id, actor_id, action, entity_type, entity_id, "
        "request_id, reason, before_sha256, after_sha256, metadata_json, "
        "occurred_at) VALUES ('audit-1', 'ws-a', 'actor-1', 'publish', "
        "'published_snapshot', 'publication-1', 'request-1', NULL, NULL, ?, "
        "'{}', ?)",
        ("e" * 64, timestamp),
    )
    connection.execute(
        "INSERT INTO workspace_state "
        "(workspace_id, data_generation, schema_generation, updated_at) "
        "VALUES ('ws-a', 1, 4, ?)",
        (timestamp,),
    )


def test_fresh_database_reaches_authoritative_schema_and_is_idempotent(test_workspace):
    database_path = test_workspace / "fresh.sqlite3"

    first = migrate_database(database_path, app_revision="test-revision")
    second = migrate_database(database_path, app_revision="ignored-on-rerun")

    assert first.previous_version == 0
    assert first.current_version == LATEST_SCHEMA_VERSION
    assert first.applied_versions == tuple(range(1, LATEST_SCHEMA_VERSION + 1))
    assert second.previous_version == LATEST_SCHEMA_VERSION
    assert second.applied_versions == ()

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
        history = connection.execute(
            "SELECT version, checksum, app_revision FROM schema_migrations "
            "ORDER BY version"
        ).fetchall()
        assert history == [
            (migration.version, migration.checksum, "test-revision")
            for migration in MIGRATIONS
        ]
        assert set(FEATURE_TAG_TO_COLUMN.values()).issubset(_columns(connection, "features"))
        assert collect_schema_drift(connection) == ()
    finally:
        connection.close()


def test_legacy_v0_rows_survive_and_new_fields_are_null():
    connection = _legacy_connection()
    connection.execute(
        "INSERT INTO bridges VALUES (34, '0000001', 2025)"
    )
    connection.execute(
        "INSERT INTO features "
        "(state_code, bridge_number, feature_type, feature_name, data_year) "
        "VALUES (34, '0000001', 'H01', 'US 1', 2025)"
    )
    connection.commit()

    result = apply_migrations(connection, app_revision="legacy-test")

    assert result.applied_versions == tuple(range(1, LATEST_SCHEMA_VERSION + 1))
    projected = connection.execute(
        "SELECT feature_type, feature_name, railroad_service_type, "
        "navigable_waterway FROM features"
    ).fetchone()
    assert projected == ("H01", "US 1", None, None)
    assert_schema_matches(connection)
    connection.close()


def test_already_present_nullable_transition_column_is_adopted():
    connection = _legacy_connection()
    connection.execute("ALTER TABLE features ADD COLUMN railroad_service_type TEXT")
    connection.commit()

    result = apply_migrations(connection)

    assert result.current_version == LATEST_SCHEMA_VERSION
    assert set(FEATURE_TAG_TO_COLUMN.values()).issubset(_columns(connection, "features"))
    connection.close()


def test_migrated_feature_rows_are_snapshot_owned_and_identity_is_unique():
    connection = _legacy_connection()
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("INSERT INTO bridges VALUES (34, 'CASCADE', 2025)")
    connection.execute(
        "INSERT INTO features "
        "(state_code, bridge_number, data_year, feature_type) "
        "VALUES (34, 'CASCADE', 2025, 'H01')"
    )
    connection.commit()

    apply_migrations(connection)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO features "
            "(state_code, bridge_number, data_year, feature_type) "
            "VALUES (34, 'CASCADE', 2025, 'H01')"
        )
    connection.rollback()
    connection.execute(
        "DELETE FROM bridges "
        "WHERE state_code = 34 AND bridge_number = 'CASCADE' AND data_year = 2025"
    )
    assert connection.execute("SELECT COUNT(*) FROM features").fetchone()[0] == 0
    connection.close()


def test_duplicate_feature_identity_aborts_without_modifying_legacy_rows():
    connection = _legacy_connection()
    connection.execute("INSERT INTO bridges VALUES (34, 'DUPLICATE', 2025)")
    connection.executemany(
        "INSERT INTO features "
        "(state_code, bridge_number, data_year, feature_type, feature_name) "
        "VALUES (34, 'DUPLICATE', 2025, 'H01', ?)",
        (("first",), ("second",)),
    )
    connection.commit()

    with pytest.raises(MigrationStateError, match="duplicate rows exist"):
        apply_migrations(connection)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM features").fetchone()[0] == 2
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'schema_migrations'"
    ).fetchone() is None
    assert "railroad_service_type" not in _columns(connection, "features")
    connection.close()


def test_wrong_transition_column_type_fails_and_rolls_back_history():
    connection = _legacy_connection()
    connection.execute("ALTER TABLE features ADD COLUMN railroad_service_type INTEGER")
    connection.commit()

    with pytest.raises(SchemaDriftError, match="railroad_service_type"):
        apply_migrations(connection)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'schema_migrations'"
    ).fetchone() is None
    assert "navigable_waterway" not in _columns(connection, "features")
    connection.close()


def test_induced_failure_rolls_back_entire_fresh_migration():
    connection = sqlite3.connect(":memory:")

    def fail_after_write(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE must_rollback (value TEXT)")
        raise ValueError("injected failure")

    failing = Migration(
        version=LATEST_SCHEMA_VERSION + 1,
        name="injected_failure",
        descriptor=("create then fail",),
        operation=fail_after_write,
    )
    custom_manifest = type(AUTHORITATIVE_SCHEMA)(
        version=LATEST_SCHEMA_VERSION + 1,
        tables=AUTHORITATIVE_SCHEMA.tables,
        views=AUTHORITATIVE_SCHEMA.views,
    )

    with pytest.raises(MigrationExecutionError, match="injected failure"):
        apply_migrations(
            connection,
            migrations=MIGRATIONS + (failing,),
            manifest=custom_manifest,
        )

    assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "features" not in names
    assert "schema_migrations" not in names
    assert "must_rollback" not in names
    connection.close()


def test_checksum_tampering_is_detected_before_new_writes():
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection)
    connection.execute(
        "UPDATE schema_migrations SET checksum = ? WHERE version = 1",
        ("0" * 64,),
    )
    connection.commit()

    with pytest.raises(MigrationChecksumError, match="checksum mismatch"):
        apply_migrations(connection)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
    connection.close()


def test_user_version_and_history_must_agree():
    connection = sqlite3.connect(":memory:")
    _apply_through_v3(connection)
    connection.execute("PRAGMA user_version = 1")

    with pytest.raises(MigrationStateError, match="does not match"):
        apply_migrations(connection)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    connection.close()


def test_manual_column_drift_is_reported_deterministically():
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection)
    connection.execute("ALTER TABLE features ADD COLUMN rogue_value TEXT")
    connection.commit()

    with pytest.raises(SchemaDriftError) as exc_info:
        apply_migrations(connection)

    assert exc_info.value.differences == (
        "features.rogue_value: unexpected column",
    )
    connection.close()


def test_manual_index_drift_is_reported_deterministically():
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection)
    connection.execute("CREATE INDEX rogue_features_index ON features(feature_type)")
    connection.commit()

    with pytest.raises(SchemaDriftError) as exc_info:
        apply_migrations(connection)

    assert exc_info.value.differences == (
        "features: unexpected index rogue_features_index",
    )
    connection.close()


def test_nonzero_user_version_without_history_is_rejected_without_writes():
    connection = _legacy_connection()
    connection.execute("PRAGMA user_version = 1")

    with pytest.raises(MigrationStateError, match="schema_migrations is absent"):
        apply_migrations(connection)

    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'schema_migrations'"
    ).fetchone() is None
    assert "railroad_service_type" not in _columns(connection, "features")
    connection.close()


def test_legacy_null_posting_dates_and_duplicate_work_years_are_not_rejected():
    connection = _legacy_connection()
    _apply_through_v3(connection)
    connection.execute("INSERT INTO bridges VALUES (34, '0000001', 2025)")
    connection.execute(
        "INSERT INTO posting_statuses "
        "(state_code, bridge_number, load_posting_status, "
        "posting_status_change_date, data_year) "
        "VALUES (34, '0000001', 'OPEN', NULL, 2025)"
    )
    connection.execute(
        "INSERT INTO posting_statuses "
        "(state_code, bridge_number, load_posting_status, "
        "posting_status_change_date, data_year) "
        "VALUES (34, '0000001', 'POSTED', NULL, 2025)"
    )
    connection.execute(
        "INSERT INTO works "
        "(state_code, bridge_number, year_work_performed, work_performed, data_year) "
        "VALUES (34, '0000001', 1999, 'BR1', 2025)"
    )
    connection.execute(
        "INSERT INTO works "
        "(state_code, bridge_number, year_work_performed, work_performed, data_year) "
        "VALUES (34, '0000001', 1999, 'RECONSTRUCTED', 2025)"
    )
    connection.execute("DELETE FROM schema_migrations WHERE version = 3")
    connection.execute("PRAGMA user_version = 2")
    connection.commit()

    result = _apply_through_v3(connection)

    assert result.applied_versions == (3,)
    assert connection.execute(
        "SELECT COUNT(*) FROM posting_statuses "
        "WHERE posting_status_change_date IS NULL"
    ).fetchone()[0] == 2
    assert connection.execute("SELECT COUNT(*) FROM works").fetchone()[0] == 2
    connection.close()


def test_child_duplicate_preflight_preserves_rows_and_v2_history():
    connection = _legacy_connection()
    _apply_through_v3(connection)
    connection.execute("INSERT INTO bridges VALUES (34, 'DUP-WORK', 2025)")
    connection.execute("DROP INDEX uq_works_snapshot_year_work")
    connection.executemany(
        "INSERT INTO works "
        "(state_code, bridge_number, data_year, year_work_performed, work_performed) "
        "VALUES (34, 'DUP-WORK', 2025, 2000, 'BR1')",
        ((), ()),
    )
    connection.execute("DELETE FROM schema_migrations WHERE version = 3")
    connection.execute("PRAGMA user_version = 2")
    connection.commit()

    with pytest.raises(MigrationStateError, match="works.*duplicate rows"):
        apply_migrations(connection)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    assert connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall() == [(1,), (2,)]
    assert connection.execute("SELECT COUNT(*) FROM works").fetchone()[0] == 2
    assert connection.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='index' AND name='uq_works_snapshot_year_work'"
    ).fetchone() is None
    connection.close()


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("features", "lanes_on_highway"),
        ("elements", "element_total_quantity"),
    ],
)
def test_nonintegral_integer_retype_fails_closed(table, column):
    connection = _legacy_connection()
    _apply_through_v3(connection)
    connection.execute("INSERT INTO bridges VALUES (34, 'BAD-INT', 2025)")
    if table == "features":
        connection.execute(
            f"INSERT INTO features "
            f"(state_code, bridge_number, data_year, feature_type, {column}) "
            f"VALUES (34, 'BAD-INT', 2025, 'H01', 1.5)"
        )
    else:
        connection.execute(
            f"INSERT INTO elements "
            f"(state_code, bridge_number, data_year, element_number, "
            f"element_parent_number, {column}) "
            f"VALUES (34, 'BAD-INT', 2025, '12', '0', 1.5)"
        )
    connection.execute("DELETE FROM schema_migrations WHERE version = 3")
    connection.execute("PRAGMA user_version = 2")
    connection.commit()

    with pytest.raises(MigrationStateError, match=f"{table}\\.{column}.*incompatible"):
        apply_migrations(connection)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    assert connection.execute(
        f"SELECT {column} FROM {table}"
    ).fetchone()[0] == 1.5
    connection.close()


def test_v4_revision_store_is_additive_empty_and_manifest_exact():
    connection = _legacy_connection()
    connection.execute("INSERT INTO bridges VALUES (34, 'LEGACY-ONLY', 2025)")
    connection.commit()

    result = apply_migrations(connection)

    assert result.current_version == LATEST_SCHEMA_VERSION
    for table in REVISION_STORE_TABLES:
        assert table_exists(connection, table.name)
        assert_table_matches(connection, table)
        assert connection.execute(
            f'SELECT COUNT(*) FROM "{table.name}"'
        ).fetchone() == (0,)
    artifact_columns = _columns(connection, "source_artifacts")
    assert "storage_uri" in artifact_columns
    assert not any("byte" in name.lower() and name != "byte_size" for name in artifact_columns)
    assert connection.execute("SELECT COUNT(*) FROM bridges").fetchone() == (1,)
    connection.close()


def test_v6_response_audit_migration_is_additive_and_advances_only_schema_generation():
    connection = _legacy_connection()
    apply_migrations(
        connection,
        migrations=MIGRATIONS[:5],
        manifest=SchemaManifest(
            version=5,
            tables=tuple(
                table
                for table in AUTHORITATIVE_SCHEMA.tables
                if table.name != QUERY_RESPONSE_AUDIT_TABLE.name
                and table not in CHAT_SESSION_TABLES
                and table not in DEPLOYMENT_IDENTITY_TABLES
                and table not in FORECAST_ANALYTICS_TABLES
                and table not in KPI_PROGRAM_TABLES
                and table not in POINT_CLOUD_RUN_TABLES
            ),
            views=V11_VIEWS,
        ),
    )
    timestamp = "2026-08-13T12:00:00Z"
    connection.execute(
        "INSERT INTO workspaces (id,slug,display_name,status,created_at,updated_at) "
        "VALUES ('v6-workspace','v6-workspace','V6','active',?,?)",
        (timestamp, timestamp),
    )
    connection.execute(
        "INSERT INTO workspace_state "
        "(workspace_id,data_generation,schema_generation,updated_at) "
        "VALUES ('v6-workspace',17,5,?)",
        (timestamp,),
    )
    connection.commit()

    result = apply_migrations(
        connection,
        migrations=MIGRATIONS[:6],
        manifest=SchemaManifest(
            version=6,
            tables=tuple(
                table
                for table in AUTHORITATIVE_SCHEMA.tables
                if table not in CHAT_SESSION_TABLES
                and table not in DEPLOYMENT_IDENTITY_TABLES
                and table not in FORECAST_ANALYTICS_TABLES
                and table not in KPI_PROGRAM_TABLES
                and table not in POINT_CLOUD_RUN_TABLES
            ),
            views=V11_VIEWS,
        ),
    )

    assert result.previous_version == 5
    assert result.applied_versions == (6,)
    assert_table_matches(connection, QUERY_RESPONSE_AUDIT_TABLE)
    assert connection.execute(
        "SELECT data_generation,schema_generation FROM workspace_state "
        "WHERE workspace_id='v6-workspace'"
    ).fetchone() == (17, 6)
    assert connection.execute(
        "SELECT COUNT(*) FROM query_response_audit"
    ).fetchone() == (0,)
    connection.close()


def test_v6_collision_rolls_back_without_history_or_generation_change():
    connection = _legacy_connection()
    v5_manifest = SchemaManifest(
        version=5,
        tables=tuple(
            table
            for table in AUTHORITATIVE_SCHEMA.tables
            if table.name != QUERY_RESPONSE_AUDIT_TABLE.name
            and table not in CHAT_SESSION_TABLES
            and table not in DEPLOYMENT_IDENTITY_TABLES
            and table not in FORECAST_ANALYTICS_TABLES
            and table not in KPI_PROGRAM_TABLES
            and table not in POINT_CLOUD_RUN_TABLES
        ),
        views=V11_VIEWS,
    )
    apply_migrations(connection, migrations=MIGRATIONS[:5], manifest=v5_manifest)
    connection.execute("CREATE TABLE query_response_audit (unexpected TEXT)")
    connection.commit()

    with pytest.raises(SchemaDriftError, match="untracked response-audit"):
        apply_migrations(connection)

    assert connection.execute("PRAGMA user_version").fetchone() == (5,)
    assert connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall() == [(1,), (2,), (3,), (4,), (5,)]
    assert _columns(connection, "query_response_audit") == {
        "unexpected": (0, "unexpected", "TEXT", 0, None, 0)
    }
    connection.close()


def test_v9_forecast_analytics_is_additive_immutable_and_advances_only_schema_generation():
    connection = _legacy_connection()
    connection.execute("PRAGMA foreign_keys = ON")
    v8_manifest = SchemaManifest(
        version=8,
        tables=tuple(
            table
            for table in AUTHORITATIVE_SCHEMA.tables
            if table not in FORECAST_ANALYTICS_TABLES
            and table not in KPI_PROGRAM_TABLES
            and table not in POINT_CLOUD_RUN_TABLES
        ),
        views=V11_VIEWS,
    )
    apply_migrations(connection, migrations=MIGRATIONS[:8], manifest=v8_manifest)
    timestamp = "2026-08-14T12:00:00Z"
    connection.execute(
        "INSERT INTO workspaces (id,slug,display_name,status,created_at,updated_at) "
        "VALUES ('forecast-workspace','forecast-workspace','Forecast','active',?,?)",
        (timestamp, timestamp),
    )
    connection.execute(
        "INSERT INTO workspace_state "
        "(workspace_id,data_generation,schema_generation,updated_at) "
        "VALUES ('forecast-workspace',23,8,?)",
        (timestamp,),
    )
    connection.commit()

    result = apply_migrations(
        connection,
        migrations=MIGRATIONS[:9],
        manifest=SchemaManifest(
            version=9,
            tables=tuple(
                table
                for table in AUTHORITATIVE_SCHEMA.tables
                if table not in KPI_PROGRAM_TABLES
                and table not in POINT_CLOUD_RUN_TABLES
            ),
            views=V11_VIEWS,
        ),
    )

    assert result.previous_version == 8
    assert result.applied_versions == (9,)
    assert connection.execute(
        "SELECT data_generation,schema_generation FROM workspace_state "
        "WHERE workspace_id='forecast-workspace'"
    ).fetchone() == (23, 9)
    for table in FORECAST_ANALYTICS_TABLES:
        assert_table_matches(connection, table)
        assert connection.execute(
            f'SELECT COUNT(*) FROM "{table.name}"'
        ).fetchone() == (0,)

    connection.execute(
        "INSERT INTO forecast_datasets "
        "(id,workspace_id,source_receipt_sha256,through_year,"
        "projection_specification_sha256,configuration_sha256,statistics_json,"
        "created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "dataset-1",
            "forecast-workspace",
            "a" * 64,
            2025,
            "b" * 64,
            "c" * 64,
            "{}",
            "analyst@example.com",
            timestamp,
        ),
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE forecast_datasets SET through_year=2026 WHERE id='dataset-1'"
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        connection.execute(
            "INSERT INTO forecast_datasets "
            "(id,workspace_id,source_receipt_sha256,through_year,"
            "projection_specification_sha256,configuration_sha256,statistics_json,"
            "created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "cross-workspace",
                "missing-workspace",
                "d" * 64,
                2025,
                "e" * 64,
                "f" * 64,
                "{}",
                "analyst@example.com",
                timestamp,
            ),
        )
    connection.rollback()
    connection.close()


def test_v9_collision_rolls_back_without_partial_forecast_schema():
    connection = _legacy_connection()
    v8_manifest = SchemaManifest(
        version=8,
        tables=tuple(
            table
            for table in AUTHORITATIVE_SCHEMA.tables
            if table not in FORECAST_ANALYTICS_TABLES
            and table not in KPI_PROGRAM_TABLES
            and table not in POINT_CLOUD_RUN_TABLES
        ),
        views=V11_VIEWS,
    )
    apply_migrations(connection, migrations=MIGRATIONS[:8], manifest=v8_manifest)
    connection.execute("CREATE TABLE forecast_datasets (unexpected TEXT)")
    connection.commit()

    with pytest.raises(SchemaDriftError, match="untracked forecast-analytics"):
        apply_migrations(connection)

    assert connection.execute("PRAGMA user_version").fetchone() == (8,)
    assert not table_exists(connection, "forecast_model_versions")
    assert _columns(connection, "forecast_datasets") == {
        "unexpected": (0, "unexpected", "TEXT", 0, None, 0)
    }
    connection.close()


def test_v10_kpi_program_store_is_additive_immutable_and_generation_neutral():
    connection = _legacy_connection()
    connection.execute("PRAGMA foreign_keys = ON")
    v9_manifest = SchemaManifest(
        version=9,
        tables=tuple(
            table
            for table in AUTHORITATIVE_SCHEMA.tables
            if table not in KPI_PROGRAM_TABLES
            and table not in POINT_CLOUD_RUN_TABLES
        ),
        views=V11_VIEWS,
    )
    apply_migrations(connection, migrations=MIGRATIONS[:9], manifest=v9_manifest)
    timestamp = "2026-08-14T15:00:00Z"
    connection.execute(
        "INSERT INTO workspaces (id,slug,display_name,status,created_at,updated_at) "
        "VALUES ('kpi-workspace','kpi-workspace','KPI','active',?,?)",
        (timestamp, timestamp),
    )
    connection.execute(
        "INSERT INTO workspace_state "
        "(workspace_id,data_generation,schema_generation,updated_at) "
        "VALUES ('kpi-workspace',31,9,?)",
        (timestamp,),
    )
    connection.commit()

    result = apply_migrations(
        connection,
        migrations=MIGRATIONS[:10],
        manifest=SchemaManifest(
            version=10,
            tables=tuple(
                table
                for table in AUTHORITATIVE_SCHEMA.tables
                if table not in POINT_CLOUD_RUN_TABLES
            ),
            views=V11_VIEWS,
        ),
    )

    assert result.previous_version == 9
    assert result.applied_versions == (10,)
    assert connection.execute(
        "SELECT data_generation,schema_generation FROM workspace_state "
        "WHERE workspace_id='kpi-workspace'"
    ).fetchone() == (31, 10)
    for table in KPI_PROGRAM_TABLES:
        assert_table_matches(connection, table)
        assert connection.execute(f'SELECT COUNT(*) FROM "{table.name}"').fetchone() == (0,)

    connection.execute(
        "INSERT INTO kpi_measure_definitions "
        "(id,workspace_id,measure_id,definition_version,definition_json,"
        "definition_sha256,approval_status,created_by,created_at) "
        "VALUES ('definition-1','kpi-workspace','bridge_count','1.0','{}',?,"
        "'research','analyst@example.com',?)",
        ("a" * 64, timestamp),
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE kpi_measure_definitions SET approval_status='approved' "
            "WHERE id='definition-1'"
        )
    connection.rollback()
    connection.close()


def test_v10_collision_rolls_back_without_partial_kpi_schema():
    connection = _legacy_connection()
    v9_manifest = SchemaManifest(
        version=9,
        tables=tuple(
            table
            for table in AUTHORITATIVE_SCHEMA.tables
            if table not in KPI_PROGRAM_TABLES
            and table not in POINT_CLOUD_RUN_TABLES
        ),
        views=V11_VIEWS,
    )
    apply_migrations(connection, migrations=MIGRATIONS[:9], manifest=v9_manifest)
    connection.execute("CREATE TABLE kpi_measure_definitions (unexpected TEXT)")
    connection.commit()

    with pytest.raises(SchemaDriftError, match="untracked KPI program"):
        apply_migrations(connection)

    assert connection.execute("PRAGMA user_version").fetchone() == (9,)
    assert not table_exists(connection, "kpi_program_values")
    connection.close()


def test_v4_workspace_scope_publication_pointer_and_evidence_fks():
    connection = _legacy_connection()
    connection.execute("PRAGMA foreign_keys = ON")
    apply_migrations(connection)
    _insert_v4_evidence_graph(connection)
    connection.commit()

    # Repeated identical bytes are separate immutable receipts, not collapsed.
    assert connection.execute(
        "SELECT COUNT(*) FROM source_artifacts "
        "WHERE workspace_id='ws-a' AND sha256=? AND byte_size=10",
        ("a" * 64,),
    ).fetchone() == (2,)
    with pytest.raises(sqlite3.IntegrityError, match="digest is immutable"):
        connection.execute(
            "UPDATE source_artifacts SET sha256=? WHERE id='artifact-1'",
            ("9" * 64,),
        )
    connection.rollback()

    # A receipt may only be referenced from its own workspace.
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        connection.execute(
            "INSERT INTO source_records "
            "(id, workspace_id, source_artifact_id, record_ordinal, source_role, "
            "raw_record_json, raw_record_sha256, parse_status, created_at) "
            "VALUES ('record-cross-scope', 'ws-b', 'artifact-1', 1, 'over', "
            "'{}', ?, 'parsed', '2026-08-13T12:00:00Z')",
            ("8" * 64,),
        )
    connection.rollback()

    # One logical publication pointer may move to a matching immutable revision.
    connection.execute(
        "UPDATE published_snapshots SET snapshot_revision_id='revision-2', "
        "published_at='2026-08-13T12:01:00Z' WHERE id='publication-1'"
    )
    assert connection.execute(
        "SELECT snapshot_revision_id FROM published_snapshots"
    ).fetchone() == ("revision-2",)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO published_snapshots "
            "(id, workspace_id, state_code, bridge_number, data_year, "
            "snapshot_revision_id, published_by, published_at) "
            "VALUES ('publication-duplicate', 'ws-a', 34, 'B1', 2025, "
            "'revision-1', 'actor-1', '2026-08-13T12:02:00Z')"
        )
    connection.rollback()

    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        connection.execute("DELETE FROM workspaces WHERE id='ws-a'")
    connection.rollback()
    connection.close()


@pytest.mark.parametrize(
    "table",
    (
        "source_records",
        "conversion_batches",
        "eligibility_decisions",
        "snapshot_revisions",
        "snapshot_revision_sources",
        "field_lineage",
        "validation_runs",
        "validation_issues",
        "mutation_audit",
    ),
)
def test_v4_evidence_tables_are_append_only(table):
    connection = _legacy_connection()
    connection.execute("PRAGMA foreign_keys = ON")
    apply_migrations(connection)
    _insert_v4_evidence_graph(connection)
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(f'UPDATE "{table}" SET id=id')
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(f'DELETE FROM "{table}"')
    connection.rollback()
    assert connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] > 0
    connection.close()


def test_v4_revision_content_and_lifecycle_are_fully_immutable():
    connection = _legacy_connection()
    apply_migrations(connection)
    _insert_v4_evidence_graph(connection)
    connection.commit()

    for assignment in (
        "canonical_json='{\"changed\":true}'",
        "canonical_sha256='changed'",
        "lifecycle_state='published'",
        "revision_number=99",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                f"UPDATE snapshot_revisions SET {assignment} WHERE id='revision-1'"
            )
        connection.rollback()
    assert connection.execute(
        "SELECT lifecycle_state, revision_number FROM snapshot_revisions "
        "WHERE id='revision-1'"
    ).fetchone() == ("staged", 1)
    connection.close()


def test_v4_revision_sources_are_ordered_unique_and_workspace_scoped():
    connection = _legacy_connection()
    connection.execute("PRAGMA foreign_keys = ON")
    apply_migrations(connection)
    _insert_v4_evidence_graph(connection)
    timestamp = "2026-08-13T12:04:00Z"
    connection.execute(
        "INSERT INTO source_records "
        "(id, workspace_id, source_artifact_id, record_ordinal, source_role, "
        "record_identity, raw_record_json, raw_record_sha256, parse_status, "
        "parse_issues_json, state_code, bridge_number, data_year, created_at) "
        "VALUES ('record-2', 'ws-a', 'artifact-1', 1, 'under', '34:B1:2025', "
        "'{}', ?, 'parsed', '[]', 34, 'B1', 2025, ?)",
        ("7" * 64, timestamp),
    )
    connection.execute(
        "INSERT INTO snapshot_revision_sources "
        "(id, workspace_id, snapshot_revision_id, source_record_id, "
        "source_ordinal, source_role) "
        "VALUES ('revision-source-2', 'ws-a', 'revision-1', 'record-2', 1, 'under')"
    )
    assert connection.execute(
        "SELECT source_record_id, source_role FROM snapshot_revision_sources "
        "WHERE snapshot_revision_id='revision-1' ORDER BY source_ordinal"
    ).fetchall() == [("record-1", "over"), ("record-2", "under")]

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO snapshot_revision_sources "
            "(id, workspace_id, snapshot_revision_id, source_record_id, "
            "source_ordinal, source_role) "
            "VALUES ('duplicate-record', 'ws-a', 'revision-1', 'record-2', 2, 'under')"
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO snapshot_revision_sources "
            "(id, workspace_id, snapshot_revision_id, source_record_id, "
            "source_ordinal, source_role) "
            "VALUES ('duplicate-ordinal', 'ws-a', 'revision-1', 'record-2', 0, 'under')"
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        connection.execute(
            "INSERT INTO snapshot_revision_sources "
            "(id, workspace_id, snapshot_revision_id, source_record_id, "
            "source_ordinal, source_role) "
            "VALUES ('cross-scope', 'ws-b', 'revision-1', 'record-1', 0, 'over')"
        )
    connection.rollback()
    connection.close()


def test_v4_can_revisit_an_older_canonical_hash_as_a_new_revision():
    connection = _legacy_connection()
    apply_migrations(connection)
    _insert_v4_evidence_graph(connection)
    connection.execute(
        "INSERT INTO snapshot_revisions "
        "(id, workspace_id, conversion_batch_id, source_record_id, state_code, "
        "bridge_number, data_year, revision_number, lifecycle_state, "
        "canonical_json, canonical_sha256, parent_revision_id, profile_id, "
        "rule_bundle_sha256, created_by, created_at) "
        "VALUES ('revision-3', 'ws-a', 'batch-1', 'record-1', 34, 'B1', "
        "2025, 3, 'staged', '{}', ?, 'revision-2', 'research', ?, "
        "'actor-1', '2026-08-13T12:03:00Z')",
        ("e" * 64, "c" * 64),
    )

    assert connection.execute(
        "SELECT revision_number FROM snapshot_revisions "
        "WHERE workspace_id='ws-a' AND state_code=34 AND bridge_number='B1' "
        "AND data_year=2025 AND canonical_sha256=? ORDER BY revision_number",
        ("e" * 64,),
    ).fetchall() == [(1,), (3,)]
    connection.close()


def test_v4_collision_rolls_back_without_partial_revision_schema():
    connection = _legacy_connection()
    _apply_through_v3(connection)
    connection.execute("CREATE TABLE workspaces (unexpected TEXT)")
    connection.commit()

    with pytest.raises(SchemaDriftError, match="untracked table"):
        apply_migrations(connection)

    assert connection.execute("PRAGMA user_version").fetchone() == (3,)
    assert connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall() == [(1,), (2,), (3,)]
    assert not table_exists(connection, "source_artifacts")
    assert _columns(connection, "workspaces") == {
        "unexpected": (0, "unexpected", "TEXT", 0, None, 0)
    }
    connection.close()


def test_v4_trigger_drift_is_detected_on_idempotent_preflight():
    connection = _legacy_connection()
    apply_migrations(connection)
    connection.execute("DROP TRIGGER trg_snapshot_revisions_no_update")
    connection.commit()

    with pytest.raises(SchemaDriftError, match="missing trigger"):
        apply_migrations(connection)

    assert connection.execute("PRAGMA user_version").fetchone() == (LATEST_SCHEMA_VERSION,)
    connection.close()


def test_registry_must_be_contiguous_before_database_is_touched():
    connection = sqlite3.connect(":memory:")
    invalid = (
        Migration(2, "gap", ("invalid",), lambda _connection: None),
    )
    manifest = type(AUTHORITATIVE_SCHEMA)(version=1, tables=())

    with pytest.raises(MigrationStateError, match="contiguous"):
        apply_migrations(connection, migrations=invalid, manifest=manifest)

    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master"
    ).fetchone()[0] == 0
    connection.close()
