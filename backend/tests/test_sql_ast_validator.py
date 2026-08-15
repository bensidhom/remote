from __future__ import annotations

import pytest

from backend.app.services.query.sql_ast import (
    CatalogTable,
    SqlCatalog,
    SqlLimits,
    SqlValidationError,
    ValidatedSql,
    validate_generated_sql,
)


BRIDGE_COLUMNS = {
    "workspace_id",
    "state_code",
    "bridge_number",
    "data_year",
    "county",
    "condition_rating",
    "deck_area",
    "owner_code",
}


@pytest.fixture
def catalog() -> SqlCatalog:
    return SqlCatalog(
        tables={
            "v_bridges": CatalogTable("v_bridges", BRIDGE_COLUMNS),
            "v_inspections": CatalogTable(
                "v_inspections",
                BRIDGE_COLUMNS | {"inspection_type", "inspection_date"},
            ),
        },
        functions={"count", "sum", "avg", "min", "max", "round", "coalesce"},
    )


def assert_rejected(catalog: SqlCatalog, sql: str, code: str, **kwargs) -> None:
    with pytest.raises(SqlValidationError) as raised:
        validate_generated_sql(sql, catalog, **kwargs)
    assert raised.value.code == code
    assert raised.value.as_dict()["code"] == code


def test_valid_select_returns_typed_catalog_evidence(catalog: SqlCatalog) -> None:
    result = validate_generated_sql(
        "SELECT county, COUNT(*) AS bridge_count "
        "FROM v_bridges b WHERE b.condition_rating < 5 "
        "GROUP BY county ORDER BY bridge_count DESC LIMIT 20",
        catalog,
    )

    assert isinstance(result, ValidatedSql)
    assert result.tables[0].table == "v_bridges"
    assert result.tables[0].alias == "b"
    assert result.output_columns == ("county", "bridge_count")
    assert {item.column for item in result.columns} >= {"county", "condition_rating"}
    assert result.literal_count == 2


def test_nonrecursive_cte_is_allowed(catalog: SqlCatalog) -> None:
    result = validate_generated_sql(
        "WITH poor AS ("
        " SELECT bridge_number, data_year FROM v_bridges WHERE condition_rating < 5"
        ") SELECT bridge_number FROM poor",
        catalog,
    )
    assert result.cte_count == 1
    assert result.output_columns == ("bridge_number",)


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("", "empty_sql"),
        ("DELETE FROM v_bridges", "forbidden_statement"),
        ("PRAGMA table_info(v_bridges)", "forbidden_statement"),
        ("EXPLAIN SELECT bridge_number FROM v_bridges", "forbidden_statement"),
        ("ATTACH 'other.db' AS other", "forbidden_statement"),
        ("SELECT bridge_number FROM v_bridges; SELECT 1", "multiple_statements"),
        ("SELECT bridge_number FROM v_bridges SELECT county FROM v_bridges", "multiple_statements"),
        ("SELECT bridge_number FROM v_bridges -- trailing", "comments_forbidden"),
        ("SELECT bridge_number /* hidden */ FROM v_bridges", "comments_forbidden"),
        ("SELECT bridge_number FROM v_bridges UNION SELECT bridge_number FROM v_bridges", "forbidden_statement"),
        ("SELECT name FROM sqlite_master", "schema_access_forbidden"),
        ("SELECT key FROM json_each('[1]')", "table_valued_source_forbidden"),
        ("SELECT bridge_number FROM v_bridges RIGHT JOIN v_inspections ON 1=1", "unsupported_syntax"),
        ('SELECT bridge_number AS "__bg_bounded_result" FROM v_bridges', "reserved_identifier"),
    ],
)
def test_statement_and_source_attacks_are_rejected(
    catalog: SqlCatalog, sql: str, code: str
) -> None:
    assert_rejected(catalog, sql, code)


def test_comment_markers_inside_a_bound_string_are_data(catalog: SqlCatalog) -> None:
    result = validate_generated_sql(
        "SELECT bridge_number FROM v_bridges WHERE county = '--not a comment'",
        catalog,
    )
    assert result.literal_count == 1


def test_nonfinite_numeric_literal_is_rejected(catalog: SqlCatalog) -> None:
    assert_rejected(
        catalog,
        "SELECT bridge_number FROM v_bridges WHERE deck_area < 1e9999",
        "invalid_number",
    )


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("WITH RECURSIVE x AS (SELECT bridge_number FROM v_bridges) SELECT bridge_number FROM x", "recursive_cte"),
        ("WITH x AS (SELECT bridge_number FROM x) SELECT bridge_number FROM x", "recursive_cte"),
        ("SELECT bridge_number FROM missing_view", "unknown_table"),
        ("SELECT secret_column FROM v_bridges", "unknown_column"),
        ("SELECT mystery(deck_area) AS x FROM v_bridges", "unknown_function"),
        ("SELECT * FROM v_bridges", "wildcard_forbidden"),
        ("SELECT b.* FROM v_bridges b", "wildcard_forbidden"),
        ("SELECT bridge_number FROM main.v_bridges", "schema_access_forbidden"),
        ("SELECT bridge_number FROM v_bridges JOIN v_inspections ON v_bridges.bridge_number = v_inspections.bridge_number", "ambiguous_column"),
        ("SELECT bridge_number FROM v_bridges JOIN v_inspections", "invalid_join"),
        ("SELECT bridge_number FROM v_bridges WHERE", "invalid_select"),
        ("SELECT bridge_number FROM v_bridges ORDER BY 1", "positional_ordering_forbidden"),
    ],
)
def test_unknown_ambiguous_or_unreviewed_constructs_fail_closed(
    catalog: SqlCatalog, sql: str, code: str
) -> None:
    assert_rejected(catalog, sql, code)


def test_count_star_is_the_only_wildcard_exception(catalog: SqlCatalog) -> None:
    result = validate_generated_sql("SELECT COUNT(*) AS total FROM v_bridges", catalog)
    assert result.output_columns == ("total",)


def test_arithmetic_multiplication_is_not_mistaken_for_wildcard(catalog: SqlCatalog) -> None:
    result = validate_generated_sql(
        "SELECT deck_area * 2 AS doubled FROM v_bridges", catalog
    )
    assert result.output_columns == ("doubled",)


def test_unsafe_function_cannot_be_enabled_by_catalog() -> None:
    dangerous_catalog = SqlCatalog(
        {"v_bridges": CatalogTable("v_bridges", BRIDGE_COLUMNS)},
        functions={"count", "random"},
    )
    assert_rejected(
        dangerous_catalog,
        "SELECT random() AS value FROM v_bridges",
        "unsafe_function",
    )


@pytest.mark.parametrize(
    ("sql", "limits", "code"),
    [
        ("SELECT bridge_number FROM v_bridges", SqlLimits(max_sql_bytes=10), "sql_bytes_exceeded"),
        (
            "WITH a AS (SELECT bridge_number FROM v_bridges), "
            "b AS (SELECT bridge_number FROM a), "
            "c AS (SELECT bridge_number FROM b) SELECT bridge_number FROM c",
            SqlLimits(max_ctes=2),
            "cte_limit_exceeded",
        ),
        (
            "SELECT a.bridge_number FROM v_bridges a "
            "JOIN v_bridges b ON a.bridge_number=b.bridge_number "
            "JOIN v_bridges c ON a.bridge_number=c.bridge_number "
            "JOIN v_bridges d ON a.bridge_number=d.bridge_number "
            "JOIN v_bridges e ON a.bridge_number=e.bridge_number "
            "JOIN v_bridges f ON a.bridge_number=f.bridge_number",
            SqlLimits(max_joins=4),
            "join_limit_exceeded",
        ),
        (
            "SELECT (((((bridge_number))))) FROM v_bridges",
            SqlLimits(max_expression_depth=4),
            "expression_depth_exceeded",
        ),
    ],
)
def test_complexity_limits_are_enforced(
    catalog: SqlCatalog, sql: str, limits: SqlLimits, code: str
) -> None:
    assert_rejected(catalog, sql, code, limits=limits)


def test_result_column_limit_is_enforced(catalog: SqlCatalog) -> None:
    sql = "SELECT " + ", ".join(
        f"bridge_number AS b{index}" for index in range(41)
    ) + " FROM v_bridges"
    assert_rejected(
        catalog,
        sql,
        "result_column_limit_exceeded",
        limits=SqlLimits(max_result_columns=40),
    )


def test_subquery_limit_is_enforced(catalog: SqlCatalog) -> None:
    sql = (
        "SELECT bridge_number FROM v_bridges WHERE bridge_number IN "
        "(SELECT bridge_number FROM v_bridges WHERE bridge_number IN "
        "(SELECT bridge_number FROM v_bridges WHERE bridge_number IN "
        "(SELECT bridge_number FROM v_bridges WHERE bridge_number IN "
        "(SELECT bridge_number FROM v_bridges WHERE bridge_number IN "
        "(SELECT bridge_number FROM v_bridges)))))"
    )
    assert_rejected(
        catalog,
        sql,
        "subquery_limit_exceeded",
        limits=SqlLimits(max_subqueries=4, max_expression_depth=20),
    )


def test_cte_expression_requires_stable_alias(catalog: SqlCatalog) -> None:
    assert_rejected(
        catalog,
        "WITH totals AS (SELECT COUNT(*) FROM v_bridges) SELECT expression_1 FROM totals",
        "derived_output_alias_required",
    )


def test_catalog_requires_all_scope_columns() -> None:
    with pytest.raises(ValueError, match="required workspace column"):
        CatalogTable(
            "v_unscoped",
            {"state_code", "bridge_number", "data_year", "value"},
        )
