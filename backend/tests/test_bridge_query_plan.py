from __future__ import annotations

import math
import sqlite3

import pytest

from app.services.query.metrics import DEFAULT_METRIC_REGISTRY
from app.services.query.models import (
    MetricClassification,
    MetricDefinition,
    MetricRegistry,
    SemanticDataType,
)
from app.services.query.plans import (
    BridgeQueryPlan,
    CompiledBridgeQueryPlan,
    PlanCompilationError,
    PlanFilter,
    PlanIntent,
    PlanLimits,
    PlanOrder,
    PlanParseError,
    PlanTimeScope,
    PlanValidationError,
    compile_bridge_query_plan,
    validate_bridge_query_plan,
)
from app.services.query.semantic import build_semantic_catalog
from app.services.query.sql_ast import QueryScope


@pytest.fixture
def catalog():
    return build_semantic_catalog(
        database_identity="test-db",
        workspace_id="workspace-a",
        schema_generation=4,
        data_generation=7,
    )


def list_plan(**changes) -> BridgeQueryPlan:
    values = {
        "intent": "list",
        "resource_id": "v_published_bridge_snapshots",
        "selected_fields": ("bridge_number", "bridge_name"),
        "time_scope": PlanTimeScope("exact"),
        "limit": 25,
    }
    values.update(changes)
    return BridgeQueryPlan(**values)


def count_plan(**changes) -> BridgeQueryPlan:
    values = {
        "intent": "count",
        "resource_id": "v_published_bridge_snapshots",
        "metric_ids": ("inventory.bridge_count",),
        "dimensions": ("county_code",),
        "selected_fields": ("county_code",),
        "time_scope": PlanTimeScope("exact"),
        "limit": 25,
    }
    values.update(changes)
    return BridgeQueryPlan(**values)


def assert_plan_error(
    error_type: type[PlanParseError | PlanValidationError | PlanCompilationError],
    code: str,
    callback,
) -> None:
    with pytest.raises(error_type) as raised:
        callback()
    assert raised.value.code == code
    assert raised.value.as_dict()["code"] == code


def test_strict_payload_builds_typed_immutable_plan() -> None:
    payload = {
        "resource_id": "v_published_bridge_snapshots",
        "intent": "list",
        "selected_fields": ["bridge_number", "bridge_name"],
        "metric_ids": [],
        "dimensions": [],
        "filters": [
            {"field_id": "owner", "operator": "eq", "value": "State DOT"}
        ],
        "time_scope": {"mode": "exact"},
        "order": [{"by": "bridge_number", "direction": "asc"}],
        "limit": 10,
    }

    plan = BridgeQueryPlan.from_payload(payload)

    assert plan.intent is PlanIntent.LIST
    assert plan.selected_fields == ("bridge_number", "bridge_name")
    assert isinstance(plan.filters[0], PlanFilter)
    assert plan.canonical_payload()["time_scope"] == {"mode": "exact"}
    assert len(plan.plan_hash) == 64


def test_canonical_payload_and_hash_are_deterministic() -> None:
    first = BridgeQueryPlan.from_payload(
        {
            "intent": "list",
            "resource_id": "v_published_bridge_snapshots",
            "selected_fields": ["bridge_number"],
            "filters": [{"operator": "in", "value": ["A", "B"], "field_id": "owner"}],
            "time_scope": {"mode": "latest"},
        }
    )
    second = BridgeQueryPlan.from_payload(
        {
            "time_scope": {"mode": "latest"},
            "filters": [{"field_id": "owner", "value": ["A", "B"], "operator": "in"}],
            "selected_fields": ["bridge_number"],
            "resource_id": "v_published_bridge_snapshots",
            "intent": "list",
        }
    )
    assert first.canonical_json == second.canonical_json
    assert first.plan_hash == second.plan_hash


@pytest.mark.parametrize(
    "extra",
    [
        {"workspace_id": "attacker"},
        {"state_code": 36},
        {"bridge_number": "OTHER"},
        {"data_year": 1900},
        {"sql": "SELECT * FROM sqlite_master"},
    ],
)
def test_root_scope_and_sql_overrides_are_rejected(extra) -> None:
    payload = {
        "intent": "list",
        "resource_id": "v_published_bridge_snapshots",
        "selected_fields": ["bridge_number"],
        "time_scope": {"mode": "latest"},
        **extra,
    }
    code = "client_scope_override" if set(extra).intersection(
        {"workspace_id", "state_code", "bridge_number", "data_year"}
    ) else "unknown_plan_field"
    assert_plan_error(
        PlanParseError,
        code,
        lambda: BridgeQueryPlan.from_payload(payload),
    )


def test_time_scope_cannot_embed_year_values() -> None:
    payload = {
        "intent": "list",
        "resource_id": "v_published_bridge_snapshots",
        "selected_fields": ["bridge_number"],
        "time_scope": {"mode": "exact", "data_year": 2024},
    }
    assert_plan_error(
        PlanParseError,
        "client_scope_override",
        lambda: BridgeQueryPlan.from_payload(payload),
    )


@pytest.mark.parametrize(
    "field",
    ["", "*", "bridge_*", "main.bridge_number", "Bridge_Number", "a.b.c"],
)
def test_blank_wildcard_and_nonsemantic_fields_are_rejected(field: str) -> None:
    assert_plan_error(
        PlanParseError,
        "invalid_field_reference",
        lambda: list_plan(selected_fields=(field,)),
    )


def test_syntactically_valid_unknown_qualified_resource_is_rejected(catalog) -> None:
    assert_plan_error(
        PlanValidationError,
        "unknown_resource",
        lambda: validate_bridge_query_plan(
            list_plan(selected_fields=("not_a_resource.bridge_number",)), catalog
        ),
    )


def test_simple_list_compiles_through_sql_guard_with_server_scope(catalog) -> None:
    plan = list_plan(
        filters=(PlanFilter("owner", "eq", "NJ'DOT"),),
        order=(PlanOrder("bridge_number", "asc"),),
    )
    scope = QueryScope("workspace-a", 34, "exact", data_year=2024)

    result = compile_bridge_query_plan(plan, catalog, scope)

    assert isinstance(result, CompiledBridgeQueryPlan)
    assert "workspace-a" not in result.logical_sql
    assert "state_code" not in result.logical_sql
    assert "NJ''DOT" in result.logical_sql
    assert "NJ'DOT" not in result.compiled_sql.sql
    assert "NJ'DOT" in result.compiled_sql.parameters
    assert result.compiled_sql.parameters[-1] == 26
    assert result.evidence.plan_hash == plan.plan_hash
    assert result.evidence.catalog_hash == catalog.catalog_hash
    assert result.evidence.metric_registry_hash == DEFAULT_METRIC_REGISTRY.registry_hash
    assert result.evidence.scope["workspace_id"] == "workspace-a"
    assert result.evidence.compiled_tables == ("v_published_bridge_snapshots",)


def test_filter_that_looks_like_sql_is_only_a_bound_value(catalog) -> None:
    value = "x' OR 1=1 --"
    result = compile_bridge_query_plan(
        list_plan(filters=(PlanFilter("bridge_name", "eq", value),)),
        catalog,
        QueryScope("workspace-a", 34, "exact", data_year=2024),
    )
    assert value not in result.compiled_sql.sql
    assert value in result.compiled_sql.parameters
    assert result.validated_sql.tables[0].table == "v_published_bridge_snapshots"


def test_compiled_plan_executes_with_exact_scope(catalog) -> None:
    resource = catalog.resources_by_id["v_published_bridge_snapshots"]
    sql_types = {
        SemanticDataType.INTEGER: "INTEGER",
        SemanticDataType.NUMBER: "REAL",
        SemanticDataType.BOOLEAN: "INTEGER",
    }
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            'CREATE TABLE "v_published_bridge_snapshots" ('
            + ", ".join(
                f'"{column.field_id}" {sql_types.get(column.data_type, "TEXT")}'
                for column in resource.columns
            )
            + ")"
        )
        identity = ("workspace_id", "state_code", "bridge_number", "data_year", "county_code")
        connection.executemany(
            'INSERT INTO "v_published_bridge_snapshots" '
            + "(" + ",".join(f'"{name}"' for name in identity) + ") VALUES(?,?,?,?,?)",
            [
                ("workspace-a", 34, "A", 2024, 1),
                ("workspace-a", 34, "B", 2024, 1),
                ("workspace-a", 34, "C", 2024, 2),
                ("workspace-b", 34, "SECRET", 2024, 1),
                ("workspace-a", 36, "NY", 2024, 1),
            ],
        )
        result = compile_bridge_query_plan(
            count_plan(order=(PlanOrder("county_code"),)),
            catalog,
            QueryScope("workspace-a", 34, "exact", data_year=2024),
        )
        rows = connection.execute(
            result.compiled_sql.sql, result.compiled_sql.parameters
        ).fetchall()
        assert rows == [(1, 2), (2, 1)]
    finally:
        connection.close()


def test_plan_time_must_match_server_scope(catalog) -> None:
    assert_plan_error(
        PlanValidationError,
        "time_scope_mismatch",
        lambda: compile_bridge_query_plan(
            list_plan(), catalog, QueryScope("workspace-a", 34, "latest")
        ),
    )


def test_catalog_identity_must_match_server_workspace(catalog) -> None:
    assert_plan_error(
        PlanValidationError,
        "catalog_scope_mismatch",
        lambda: compile_bridge_query_plan(
            list_plan(),
            catalog,
            QueryScope("another-workspace", 34, "exact", data_year=2024),
        ),
    )


def test_bridge_scope_requires_bridge_identity_resource(catalog) -> None:
    plan = BridgeQueryPlan(
        "list",
        "v_county_bridge_inventory",
        selected_fields=("county_name",),
        time_scope=PlanTimeScope("exact"),
    )
    assert_plan_error(
        PlanValidationError,
        "incompatible_bridge_resource",
        lambda: compile_bridge_query_plan(
            plan,
            catalog,
            QueryScope(
                "workspace-a", 34, "exact", bridge_number="B-1", data_year=2024
            ),
        ),
    )


@pytest.mark.parametrize(
    ("plan", "code"),
    [
        (list_plan(resource_id="v_missing"), "unknown_resource"),
        (list_plan(selected_fields=("missing",)), "unknown_field"),
        (
            list_plan(filters=(PlanFilter("bridge_name", "gt", "A"),)),
            "operator_not_allowed",
        ),
        (
            list_plan(filters=(PlanFilter("year_built", "eq", "2024"),)),
            "filter_type_mismatch",
        ),
        (
            list_plan(
                filters=(PlanFilter("condition_conflict", "eq", 1),),
                resource_id="v_bridge_condition",
                selected_fields=("bridge_number",),
            ),
            "filter_type_mismatch",
        ),
    ],
)
def test_unknown_or_wrongly_typed_semantics_are_rejected(catalog, plan, code) -> None:
    assert_plan_error(
        PlanValidationError,
        code,
        lambda: validate_bridge_query_plan(plan, catalog),
    )


@pytest.mark.parametrize("scope_field", ["workspace_id", "state_code", "bridge_number", "data_year", "snapshot_revision_id"])
def test_filter_cannot_override_any_server_scope_field(catalog, scope_field: str) -> None:
    assert_plan_error(
        PlanValidationError,
        "client_scope_override",
        lambda: validate_bridge_query_plan(
            list_plan(filters=(PlanFilter(scope_field, "eq", "attacker"),)),
            catalog,
        ),
    )


def test_unknown_and_disabled_metrics_have_distinct_errors(catalog) -> None:
    assert_plan_error(
        PlanValidationError,
        "unknown_metric",
        lambda: validate_bridge_query_plan(
            count_plan(metric_ids=("inventory.not_real",)), catalog
        ),
    )
    assert_plan_error(
        PlanValidationError,
        "disabled_metric",
        lambda: validate_bridge_query_plan(
            count_plan(metric_ids=("federal.good_deck_area_percent",)), catalog
        ),
    )


def test_metric_must_match_declared_base_resource(catalog) -> None:
    assert_plan_error(
        PlanValidationError,
        "metric_resource_mismatch",
        lambda: validate_bridge_query_plan(
            BridgeQueryPlan(
                "count",
                "v_bridge_condition",
                metric_ids=("inventory.bridge_count",),
                time_scope=PlanTimeScope("exact"),
            ),
            catalog,
        ),
    )


def test_metric_needing_unrepresented_server_parameter_abstains(catalog) -> None:
    plan = BridgeQueryPlan(
        "aggregate",
        "v_current_bridge_snapshots",
        metric_ids=("inventory.average_current_age",),
        time_scope=PlanTimeScope("latest"),
    )
    assert_plan_error(
        PlanValidationError,
        "metric_requires_server_parameter",
        lambda: validate_bridge_query_plan(plan, catalog),
    )


@pytest.mark.parametrize(
    ("filter_item", "code"),
    [
        (PlanFilter("bridge_name", "eq", "   "), "blank_filter_value"),
        (PlanFilter("bridge_name", "eq", "x" * 257), "filter_value_too_large"),
        (PlanFilter("year_built", "eq", 10**16), "filter_value_too_large"),
        (PlanFilter("bridge_name", "contains", "NJ%"), "unsafe_wildcard"),
        (PlanFilter("bridge_name", "starts_with", "NJ_*"), "unsafe_wildcard"),
        (PlanFilter("owner", "in", ()), "invalid_filter_cardinality"),
        (PlanFilter("year_built", "between", (2025, 2020)), "invalid_filter_range"),
        (PlanFilter("published_at", "eq", "not-a-date"), "filter_type_mismatch"),
        (PlanFilter("bridge_name", "is_null", "value"), "filter_value_forbidden"),
        (PlanFilter("published_at", "is_null"), "null_check_nonnullable"),
    ],
)
def test_blank_huge_wildcard_and_invalid_filter_values_fail_closed(
    catalog, filter_item, code
) -> None:
    assert_plan_error(
        PlanValidationError,
        code,
        lambda: validate_bridge_query_plan(
            list_plan(filters=(filter_item,)), catalog
        ),
    )


def test_nonfinite_values_are_rejected_before_a_plan_exists() -> None:
    assert_plan_error(
        PlanParseError,
        "invalid_filter_value",
        lambda: PlanFilter("latitude", "eq", math.inf),
    )


def test_in_filter_cardinality_is_bounded(catalog) -> None:
    assert_plan_error(
        PlanValidationError,
        "invalid_filter_cardinality",
        lambda: validate_bridge_query_plan(
            list_plan(filters=(PlanFilter("owner", "in", tuple(str(i) for i in range(51))),)),
            catalog,
        ),
    )


@pytest.mark.parametrize(
    ("plan", "code"),
    [
        (
            BridgeQueryPlan(
                "aggregate",
                "v_published_bridge_snapshots",
                metric_ids=("inventory.bridge_count",),
                selected_fields=("county_code",),
                dimensions=(),
                time_scope=PlanTimeScope("exact"),
            ),
            "aggregate_grain_error",
        ),
        (
            BridgeQueryPlan(
                "list",
                "v_published_bridge_snapshots",
                selected_fields=("bridge_number",),
                metric_ids=("inventory.bridge_count",),
                time_scope=PlanTimeScope("exact"),
            ),
            "intent_shape_mismatch",
        ),
        (
            BridgeQueryPlan(
                "compare",
                "v_published_bridge_snapshots",
                metric_ids=("inventory.bridge_count",),
                time_scope=PlanTimeScope("exact"),
            ),
            "dimension_required",
        ),
        (
            BridgeQueryPlan(
                "trend",
                "v_published_bridge_snapshots",
                metric_ids=("inventory.bridge_count",),
                dimensions=("county_code",),
                selected_fields=("county_code",),
                time_scope=PlanTimeScope("trend"),
            ),
            "trend_year_dimension_required",
        ),
        (
            BridgeQueryPlan(
                "aggregate",
                "v_published_bridge_snapshots",
                metric_ids=("inventory.total_deck_area",),
                time_scope=PlanTimeScope("trend"),
            ),
            "intent_time_mismatch",
        ),
    ],
)
def test_intent_time_and_grain_contracts(catalog, plan, code) -> None:
    assert_plan_error(
        PlanValidationError,
        code,
        lambda: validate_bridge_query_plan(plan, catalog),
    )


def test_metric_dimensions_are_registry_constrained(catalog) -> None:
    assert_plan_error(
        PlanValidationError,
        "metric_dimension_not_allowed",
        lambda: validate_bridge_query_plan(
            count_plan(dimensions=("bridge_name",), selected_fields=("bridge_name",)),
            catalog,
        ),
    )


def test_reviewed_cross_resource_dimension_compiles_one_to_one_join(catalog) -> None:
    plan = BridgeQueryPlan(
        "aggregate",
        "v_bridge_condition",
        metric_ids=("inventory.poor_bridge_count",),
        dimensions=("v_published_bridge_snapshots.county_code",),
        selected_fields=("v_published_bridge_snapshots.county_code",),
        time_scope=PlanTimeScope("exact"),
    )
    result = compile_bridge_query_plan(
        plan, catalog, QueryScope("workspace-a", 34, "exact", data_year=2024)
    )
    assert "JOIN \"v_published_bridge_snapshots\"" in result.logical_sql
    assert result.evidence.joins == ("published_to_condition",)
    assert result.evidence.compiled_tables == (
        "v_bridge_condition",
        "v_published_bridge_snapshots",
    )


def test_one_to_many_join_is_rejected_for_aggregate_grain(catalog) -> None:
    metric = MetricDefinition(
        metric_id="test.bridge_count",
        label="Test count",
        description="Test-only metric.",
        source_resource_id="v_published_bridge_snapshots",
        expression="COUNT(*)",
        required_columns=(),
        unit="bridges",
        null_semantics="Rows are counted.",
        denominator_semantics="Not a ratio.",
        time_semantics="Exact snapshot.",
        allowed_dimensions=("v_bridge_features.feature_type",),
        classification=MetricClassification.OPERATIONAL_INVENTORY,
    )
    registry = MetricRegistry("test-v1", (metric,))
    plan = BridgeQueryPlan(
        "aggregate",
        "v_published_bridge_snapshots",
        metric_ids=("test.bridge_count",),
        dimensions=("v_bridge_features.feature_type",),
        selected_fields=("v_bridge_features.feature_type",),
        time_scope=PlanTimeScope("exact"),
    )
    assert_plan_error(
        PlanValidationError,
        "aggregate_grain_error",
        lambda: validate_bridge_query_plan(
            plan, catalog, metric_registry=registry
        ),
    )


def test_trend_plan_requires_and_compiles_explicit_year_dimension(catalog) -> None:
    plan = BridgeQueryPlan(
        "trend",
        "v_published_bridge_snapshots",
        metric_ids=("inventory.bridge_count",),
        dimensions=("data_year",),
        selected_fields=("data_year",),
        time_scope=PlanTimeScope("trend"),
        order=(PlanOrder("data_year"),),
        limit=10,
    )
    result = compile_bridge_query_plan(
        plan,
        catalog,
        QueryScope("workspace-a", 34, "trend", start_year=2020, end_year=2024),
    )
    assert "GROUP BY" in result.logical_sql
    assert "BETWEEN ? AND ?" in result.compiled_sql.sql
    assert result.compiled_sql.parameters[-1] == 11


def test_current_resource_and_county_aggregate_time_semantics(catalog) -> None:
    current = BridgeQueryPlan(
        "list",
        "v_current_bridge_snapshots",
        selected_fields=("bridge_number",),
        time_scope=PlanTimeScope("exact"),
    )
    assert_plan_error(
        PlanValidationError,
        "current_resource_requires_latest",
        lambda: validate_bridge_query_plan(current, catalog),
    )
    county = BridgeQueryPlan(
        "list",
        "v_county_bridge_inventory",
        selected_fields=("county_name",),
        time_scope=PlanTimeScope("latest"),
    )
    assert_plan_error(
        PlanValidationError,
        "latest_requires_bridge_identity",
        lambda: validate_bridge_query_plan(county, catalog),
    )
    deck_percent = BridgeQueryPlan(
        "aggregate",
        "v_current_bridge_deck_material_summary",
        metric_ids=("inventory.current_concrete_deck_bridge_percent",),
        time_scope=PlanTimeScope("latest"),
    )
    compiled = compile_bridge_query_plan(
        deck_percent,
        catalog,
        QueryScope("workspace-a", 34, "latest"),
    )
    assert "MAX(" not in compiled.compiled_sql.sql


def test_limit_complexity_duplicates_and_order_are_bounded(catalog) -> None:
    assert_plan_error(
        PlanValidationError,
        "invalid_limit",
        lambda: validate_bridge_query_plan(list_plan(limit=201), catalog),
    )
    assert_plan_error(
        PlanValidationError,
        "invalid_limit",
        lambda: validate_bridge_query_plan(
            list_plan(intent="lookup", limit=21), catalog
        ),
    )
    assert_plan_error(
        PlanValidationError,
        "duplicate_plan_item",
        lambda: validate_bridge_query_plan(
            list_plan(selected_fields=("bridge_number", "bridge_number")), catalog
        ),
    )
    assert_plan_error(
        PlanValidationError,
        "unknown_order_output",
        lambda: validate_bridge_query_plan(
            list_plan(order=(PlanOrder("bridge_name"),), selected_fields=("bridge_number",)),
            catalog,
        ),
    )
    assert_plan_error(
        PlanValidationError,
        "plan_complexity_exceeded",
        lambda: validate_bridge_query_plan(
            list_plan(
                selected_fields=("bridge_number", "bridge_name", "owner"),
            ),
            catalog,
            limits=PlanLimits(max_selected_fields=2),
        ),
    )


def test_plan_evidence_is_deeply_immutable_and_serializable(catalog) -> None:
    result = compile_bridge_query_plan(
        count_plan(),
        catalog,
        QueryScope("workspace-a", 34, "exact", data_year=2024),
    )
    with pytest.raises(TypeError):
        result.evidence.canonical_plan["limit"] = 999  # type: ignore[index]
    with pytest.raises(TypeError):
        result.evidence.outputs[0]["alias"] = "changed"  # type: ignore[index]
    payload = result.evidence.as_dict()
    assert payload["canonical_plan"]["metric_ids"] == ["inventory.bridge_count"]
    assert json_round_trip(payload)["plan_hash"] == result.plan.plan_hash


def json_round_trip(value):
    import json

    return json.loads(json.dumps(value, allow_nan=False, sort_keys=True))
