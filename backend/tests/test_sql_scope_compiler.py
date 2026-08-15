from __future__ import annotations

import sqlite3

import pytest

from backend.app.services.query.sql_ast import (
    CatalogTable,
    CompiledSql,
    QueryScope,
    SqlCatalog,
    SqlCompilationError,
    SqlLimits,
    TableTimeSemantics,
    TimeMode,
    compile_scoped_sql,
    validate_and_compile_sql,
    validate_generated_sql,
)


COLUMNS = {
    "workspace_id",
    "state_code",
    "bridge_number",
    "data_year",
    "county",
    "condition_rating",
    "deck_area",
}


@pytest.fixture
def catalog() -> SqlCatalog:
    return SqlCatalog(
        {
            "v_bridges": CatalogTable("v_bridges", COLUMNS),
            "v_conditions": CatalogTable("v_conditions", COLUMNS),
            "v_current_bridges": CatalogTable(
                "v_current_bridges",
                COLUMNS,
                time_semantics=TableTimeSemantics.CURRENT,
            ),
            "v_county_inventory": CatalogTable(
                "v_county_inventory",
                {
                    "workspace_id",
                    "state_code",
                    "data_year",
                    "county",
                    "bridge_count",
                },
                bridge_column=None,
            ),
        },
        functions={"count", "sum", "avg", "min", "max", "round"},
    )


@pytest.fixture
def connection() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    for table in ("v_bridges", "v_conditions", "v_current_bridges"):
        db.execute(
            f"CREATE TABLE {table}("
            "workspace_id TEXT, state_code INTEGER, bridge_number TEXT, "
            "data_year INTEGER, county TEXT, condition_rating INTEGER, deck_area REAL)"
        )
    rows = [
        ("workspace-a", 34, "B-1", 2022, "Gloucester", 6, 100.0),
        ("workspace-a", 34, "B-1", 2023, "Gloucester", 5, 100.0),
        ("workspace-a", 34, "B-2", 2023, "Camden", 7, 125.0),
        ("workspace-a", 34, "B-1", 2024, "Gloucester", 4, 100.0),
        ("workspace-a", 36, "NY-1", 2024, "Other", 2, 200.0),
        ("workspace-b", 34, "SECRET", 2024, "Gloucester", 1, 500.0),
    ]
    db.executemany("INSERT INTO v_bridges VALUES(?,?,?,?,?,?,?)", rows)
    db.executemany("INSERT INTO v_conditions VALUES(?,?,?,?,?,?,?)", rows)
    db.executemany(
        "INSERT INTO v_current_bridges VALUES(?,?,?,?,?,?,?)",
        [rows[3], rows[2], rows[4], rows[5]],
    )
    db.execute(
        "CREATE TABLE v_county_inventory("
        "workspace_id TEXT, state_code INTEGER, data_year INTEGER, "
        "county TEXT, bridge_count INTEGER)"
    )
    db.executemany(
        "INSERT INTO v_county_inventory VALUES(?,?,?,?,?)",
        [
            ("workspace-a", 34, 2023, "Camden", 1),
            ("workspace-a", 34, 2024, "Gloucester", 1),
            ("workspace-b", 34, 2024, "Secret", 999),
        ],
    )
    yield db
    db.close()


def execute(db: sqlite3.Connection, compiled: CompiledSql) -> list[tuple]:
    return db.execute(compiled.sql, compiled.parameters).fetchall()


def test_exact_scope_is_parameterized_and_executable(
    catalog: SqlCatalog, connection: sqlite3.Connection
) -> None:
    compiled = validate_and_compile_sql(
        "SELECT bridge_number FROM v_bridges WHERE county = 'Gloucester' ORDER BY bridge_number",
        catalog,
        QueryScope("workspace-a", 34, TimeMode.EXACT, data_year=2023),
    )

    assert "workspace-a" not in compiled.sql
    assert "Gloucester" not in compiled.sql
    assert compiled.sql.endswith('AS "__bg_bounded_result" LIMIT ?')
    assert compiled.parameters == (
        "workspace-a",
        34,
        2023,
        "Gloucester",
        201,
    )
    assert execute(connection, compiled) == [("B-1",)]


def test_bridge_scope_is_injected_into_every_join_scan(
    catalog: SqlCatalog, connection: sqlite3.Connection
) -> None:
    compiled = validate_and_compile_sql(
        "SELECT b.bridge_number, c.condition_rating AS rating "
        "FROM v_bridges AS b JOIN v_conditions AS c "
        "ON b.bridge_number = c.bridge_number AND b.data_year = c.data_year",
        catalog,
        QueryScope(
            "workspace-a", 34, "exact", bridge_number="B-1", data_year=2024
        ),
    )

    assert compiled.sql.count('"workspace_id" = ?') == 2
    assert compiled.sql.count('"state_code" = ?') == 2
    assert compiled.sql.count('"bridge_number" = ?') == 2
    assert compiled.sql.count('"data_year" = ?') == 2
    assert compiled.parameters[:-1] == (
        "workspace-a",
        34,
        "B-1",
        2024,
        "workspace-a",
        34,
        "B-1",
        2024,
    )
    assert execute(connection, compiled) == [("B-1", 4)]


def test_latest_scope_uses_a_scoped_max_year(
    catalog: SqlCatalog, connection: sqlite3.Connection
) -> None:
    compiled = validate_and_compile_sql(
        "SELECT bridge_number FROM v_bridges ORDER BY bridge_number",
        catalog,
        QueryScope("workspace-a", 34, "latest"),
    )

    assert "SELECT MAX" in compiled.sql
    # Outer scan and trusted latest-year scan each carry workspace and state.
    assert compiled.parameters == ("workspace-a", 34, "workspace-a", 34, 201)
    assert execute(connection, compiled) == [("B-1",), ("B-2",)]


def test_latest_bridge_scope_cannot_select_another_bridge(
    catalog: SqlCatalog, connection: sqlite3.Connection
) -> None:
    compiled = validate_and_compile_sql(
        "SELECT bridge_number, data_year FROM v_bridges",
        catalog,
        QueryScope("workspace-a", 34, "latest", bridge_number="B-2"),
    )
    assert compiled.parameters == (
        "workspace-a",
        34,
        "B-2",
        "workspace-a",
        34,
        "B-2",
        201,
    )
    assert execute(connection, compiled) == [("B-2", 2023)]


def test_current_view_declares_latest_semantics_without_reinterpreting_year(
    catalog: SqlCatalog, connection: sqlite3.Connection
) -> None:
    compiled = validate_and_compile_sql(
        "SELECT bridge_number, data_year FROM v_current_bridges ORDER BY bridge_number",
        catalog,
        QueryScope("workspace-a", 34, "latest"),
    )
    assert "SELECT MAX" not in compiled.sql
    assert compiled.parameters == ("workspace-a", 34, 201)
    assert execute(connection, compiled) == [("B-1", 2024), ("B-2", 2023)]


def test_trend_rejects_current_only_view(catalog: SqlCatalog) -> None:
    with pytest.raises(SqlCompilationError) as raised:
        validate_and_compile_sql(
            "SELECT data_year FROM v_current_bridges",
            catalog,
            QueryScope(
                "workspace-a", 34, "trend", start_year=2022, end_year=2024
            ),
        )
    assert raised.value.code == "incompatible_time_resource"


def test_aggregate_resource_without_bridge_identity_supports_exact_scope(
    catalog: SqlCatalog, connection: sqlite3.Connection
) -> None:
    compiled = validate_and_compile_sql(
        "SELECT county, bridge_count FROM v_county_inventory",
        catalog,
        QueryScope("workspace-a", 34, "exact", data_year=2023),
    )
    assert execute(connection, compiled) == [("Camden", 1)]


def test_aggregate_resource_rejects_bridge_scope(catalog: SqlCatalog) -> None:
    with pytest.raises(SqlCompilationError) as raised:
        validate_and_compile_sql(
            "SELECT county FROM v_county_inventory",
            catalog,
            QueryScope(
                "workspace-a", 34, "exact", bridge_number="B-1", data_year=2023
            ),
        )
    assert raised.value.code == "incompatible_bridge_resource"


def test_historical_aggregate_resource_rejects_ambiguous_latest(
    catalog: SqlCatalog,
) -> None:
    with pytest.raises(SqlCompilationError) as raised:
        validate_and_compile_sql(
            "SELECT county, bridge_count FROM v_county_inventory",
            catalog,
            QueryScope("workspace-a", 34, "latest"),
        )
    assert raised.value.code == "latest_requires_bridge_identity"


def test_trend_scope_uses_inclusive_server_range(
    catalog: SqlCatalog, connection: sqlite3.Connection
) -> None:
    compiled = validate_and_compile_sql(
        "SELECT data_year, COUNT(*) AS total FROM v_bridges "
        "GROUP BY data_year ORDER BY data_year",
        catalog,
        QueryScope("workspace-a", 34, "trend", start_year=2022, end_year=2023),
    )
    assert "BETWEEN ? AND ?" in compiled.sql
    assert execute(connection, compiled) == [(2022, 1), (2023, 2)]


def test_user_literals_before_and_after_from_preserve_parameter_order(
    catalog: SqlCatalog, connection: sqlite3.Connection
) -> None:
    compiled = validate_and_compile_sql(
        "SELECT 7 AS marker, bridge_number FROM v_bridges WHERE condition_rating < 6",
        catalog,
        QueryScope("workspace-a", 34, "exact", data_year=2024),
    )
    assert compiled.parameters == (7, "workspace-a", 34, 2024, 6, 201)
    assert execute(connection, compiled) == [(7, "B-1")]


def test_outer_limit_is_always_max_plus_one_even_with_inner_limit(
    catalog: SqlCatalog, connection: sqlite3.Connection
) -> None:
    compiled = validate_and_compile_sql(
        "SELECT bridge_number FROM v_bridges LIMIT 999999",
        catalog,
        QueryScope("workspace-a", 34, "trend", start_year=2022, end_year=2024),
        limits=SqlLimits(max_rows=50),
        max_rows=5000,
    )
    assert compiled.fetch_limit == 51
    assert compiled.parameters[-2:] == (999999, 51)
    assert compiled.sql.count("LIMIT ?") == 2


def test_cte_base_scan_is_scoped_not_the_cte_reference(
    catalog: SqlCatalog, connection: sqlite3.Connection
) -> None:
    compiled = validate_and_compile_sql(
        "WITH poor AS (SELECT bridge_number FROM v_bridges WHERE condition_rating < 5) "
        "SELECT bridge_number FROM poor",
        catalog,
        QueryScope("workspace-a", 34, "exact", data_year=2024),
    )
    assert compiled.sql.count('FROM "v_bridges"') == 1
    assert "FROM poor" in compiled.sql
    assert execute(connection, compiled) == [("B-1",)]


def test_scope_values_cannot_be_overridden_by_model_predicates(
    catalog: SqlCatalog, connection: sqlite3.Connection
) -> None:
    compiled = validate_and_compile_sql(
        "SELECT bridge_number FROM v_bridges WHERE workspace_id = 'workspace-b'",
        catalog,
        QueryScope("workspace-a", 34, "exact", data_year=2024),
    )
    assert execute(connection, compiled) == []


def test_compile_requires_typed_validation_result(catalog: SqlCatalog) -> None:
    with pytest.raises(SqlCompilationError) as raised:
        compile_scoped_sql(  # type: ignore[arg-type]
            "SELECT bridge_number FROM v_bridges",
            QueryScope("workspace-a", 34, "latest"),
        )
    assert raised.value.code == "unvalidated_sql"


@pytest.mark.parametrize(
    "scope",
    [
        lambda: QueryScope("workspace-a", 34, "exact"),
        lambda: QueryScope("workspace-a", 34, "latest", data_year=2024),
        lambda: QueryScope("workspace-a", 34, "trend", start_year=2025, end_year=2024),
        lambda: QueryScope("", 34, "latest"),
    ],
)
def test_invalid_scope_combinations_are_rejected(scope) -> None:
    with pytest.raises(ValueError):
        scope()


def test_prevalidated_sql_can_be_compiled_with_smaller_endpoint_limit(
    catalog: SqlCatalog,
) -> None:
    validated = validate_generated_sql(
        "SELECT bridge_number FROM v_bridges", catalog
    )
    compiled = compile_scoped_sql(
        validated,
        QueryScope("workspace-a", 34, "latest"),
        limits=SqlLimits(max_rows=200),
        max_rows=50,
    )
    assert compiled.fetch_limit == 51


def test_compiled_parameter_limit_includes_injected_scope(catalog: SqlCatalog) -> None:
    validated = validate_generated_sql(
        "SELECT bridge_number FROM v_bridges WHERE county IN ('A', 'B', 'C')",
        catalog,
    )
    with pytest.raises(SqlCompilationError) as raised:
        compile_scoped_sql(
            validated,
            QueryScope("workspace-a", 34, "exact", data_year=2024),
            limits=SqlLimits(max_parameters=6),
        )
    assert raised.value.code == "parameter_limit_exceeded"


def test_scoped_scan_projects_only_catalog_columns() -> None:
    catalog = SqlCatalog(
        {
            "v_safe": CatalogTable(
                "v_safe",
                {
                    "workspace_id",
                    "state_code",
                    "bridge_number",
                    "data_year",
                    "safe_value",
                },
            )
        },
        functions={"count"},
    )
    compiled = validate_and_compile_sql(
        "SELECT safe_value FROM v_safe",
        catalog,
        QueryScope("workspace-a", 34, "exact", data_year=2025),
    )

    assert 'SELECT * FROM "v_safe"' not in compiled.sql
    assert '"__bg_raw_1"."safe_value" AS "safe_value"' in compiled.sql
