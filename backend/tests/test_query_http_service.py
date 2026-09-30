from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
import csv
import io
import json
import logging
import sqlite3
import threading

import pytest

from app.routers.services.database import ensure_schema
from app.routers.services.db_schema import LATEST_SCHEMA_VERSION
from app.services.query import http_service
from app.query_api import BoundedResultStore
from app.services.query.service import (
    PlanGenerationRequest,
    QueryAstEvidence,
    SemanticQueryAbstained,
    SemanticQueryAuditFailed,
    SemanticQueryExecutionFailed,
    SemanticQueryEvidence,
    SemanticQueryGenerationFailed,
    SqlGenerationRequest,
)
from app.services.query.sql_ast import QueryScope


WORKSPACE = "http-query-workspace"
REQUEST_ID = "12345678-1234-4234-9234-123456789abc"


@dataclass
class Request:
    request_id: str = REQUEST_ID
    question: str = "How many published bridges are there?"
    model: str = "ignored-client-model"
    surface: str = "full"
    row_limit: int = 200
    actor_id: str = "viewer@example.com"
    actor_role: str = "viewer"
    scope: QueryScope = QueryScope(
        workspace_id=WORKSPACE,
        state_code=34,
        time_mode="latest",
    )
    cancellation_token: threading.Event = threading.Event()


def _evidence(columns, rows, *, truncated=False):
    return SemanticQueryEvidence(
        request_id=REQUEST_ID,
        attempt=1,
        columns=tuple(columns),
        rows=tuple(tuple(row) for row in rows),
        row_count=len(rows),
        truncated=truncated,
        serialized_bytes=100,
        elapsed_ms=5,
        progress_steps=1,
        scope=QueryScope(
            workspace_id=WORKSPACE,
            state_code=34,
            time_mode="latest",
        ),
        ast=QueryAstEvidence(
            relations=("v_current_bridge_snapshots",),
            referenced_fields=tuple(columns),
            output_fields=tuple(columns),
            cte_count=0,
            join_count=0,
            subquery_count=0,
            expression_depth=1,
            literal_count=0,
        ),
        semantic_catalog_sha256="a" * 64,
        snbi_dictionary_sha256="b" * 64,
        prompt_context_sha256="d" * 64,
        compiled_sql_sha256="c" * 64,
    )


def _install_query(monkeypatch, evidence):
    monkeypatch.setattr(
        http_service,
        "get_workspace_state",
        lambda _database, _workspace: SimpleNamespace(
            schema_generation=5,
            data_generation=9,
        ),
    )

    def execute_semantic_plan_query(*, request, generate_plan, **_kwargs):
        plan = generate_plan(
            PlanGenerationRequest(
                request_id=request.request_id,
                attempt=1,
                question=request.question,
                scope=request.scope,
                context_json="{}",
                context_sha256="e" * 64,
            )
        )
        assert plan == _PLAN_JSON
        return evidence

    monkeypatch.setattr(
        http_service,
        "execute_semantic_plan_query",
        execute_semantic_plan_query,
    )
    monkeypatch.setattr(
        http_service,
        "record_query_response_audit",
        lambda _database_path, audit_request: audit_request,
    )


def test_county_codes_are_enriched_with_reviewed_names_without_provider_call():
    columns, rows = http_service._enrich_county_names(
        ("county_code", "bridge_count"),
        ((3, 577), (23, 563), (41, 268)),
    )

    assert columns == ("county_code", "county_name", "bridge_count")
    assert rows == (
        (3, "Bergen", 577),
        (23, "Middlesex", 563),
        (41, "Warren", 268),
    )


_PLAN_JSON = (
    '{"intent":"count","resource_id":"v_current_bridge_snapshots",'
    '"metric_ids":["inventory.bridge_count"],"selected_fields":[],'
    '"dimensions":[],"filters":[],"time_scope":{"mode":"latest"},'
    '"order":[],"limit":200}'
)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What is the deck area of bridge 01M0019?", "01M0019"),
        ("What is the deck area of bridge 1234?", "1234"),
        ("How many square feet is bridge number 0103152's deck?", "0103152"),
        ("For 01M0019, what is the current bridge deck square footage?", "01M0019"),
        ("What is the total deck square footage for all bridges?", None),
        ("List bridge deck material types.", None),
        (
            "Find Poor bridges with concrete decks and report owner plus deck area; "
            "do not double-count bridge identities.",
            None,
        ),
        (
            "Find Poor bridges with concrete decks and report owner plus deck area, "
            "and retain bridge number as the stable inventory identity.",
            None,
        ),
    ],
)
def test_single_bridge_deck_area_candidate_is_deliberately_narrow(question, expected):
    assert http_service._candidate_bridge_number(question) == expected


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What is the total deck area for the bridge state owned?", True),
        ("How many square feet of deck area are on state-owned bridges?", True),
        ("How many square feet of deck belong to state-owned structures?", True),
        ("For the latest inventory, compare the number and deck area of state-owned bridges.", True),
        ("What is the combined deck square footage for NJDOT-owned bridges?", True),
        ("What is the deck area of state-owned bridge 01M0019?", False),
        (
            "What is the total deck area for state-owned bridges that are operationally Poor?",
            False,
        ),
        (
            "What is the total deck area for state-owned concrete-deck bridges?",
            False,
        ),
        ("What is the total deck area for county-owned bridges?", False),
        ("Count state-owned bridges.", False),
    ],
)
def test_state_owned_total_deck_area_intent_is_narrow(question, expected):
    assert http_service._is_state_owned_total_deck_area_question(question) is expected


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("How many square feet of bridge deck are in the latest New Jersey inventory?", True),
        ("What is the total deck area for all bridges?", True),
        ("What is the total deck area for state-owned bridges?", False),
        ("What is the total deck area for operationally Poor bridges?", False),
        ("What is the deck area of bridge 01M0019?", False),
    ],
)
def test_unfiltered_total_deck_area_intent_is_narrow(question, expected):
    assert http_service._is_unfiltered_total_deck_area_question(question) is expected


def test_unfiltered_total_deck_area_plan_uses_governed_metric_without_filter():
    plan = json.loads(
        http_service._total_deck_plan(
            QueryScope(workspace_id=WORKSPACE, state_code=34, time_mode="latest")
        )
    )

    assert plan["resource_id"] == "v_published_bridge_snapshots"
    assert plan["metric_ids"] == ["inventory.total_deck_area"]
    assert plan["filters"] == []


def test_state_owned_total_deck_area_plan_uses_s01_owner_filter():
    plan = json.loads(
        http_service._state_owned_total_deck_plan(
            QueryScope(workspace_id=WORKSPACE, state_code=34, time_mode="latest")
        )
    )

    assert plan["metric_ids"] == ["inventory.total_deck_area"]
    assert plan["filters"] == [
        {"field_id": "owner", "operator": "eq", "value": "S01"}
    ]


def test_state_owned_deck_plan_can_return_count_and_area_together():
    plan = json.loads(
        http_service._state_owned_total_deck_plan(
            QueryScope(workspace_id=WORKSPACE, state_code=34, time_mode="latest"),
            question=(
                "For the latest inventory, compare the number and deck area of "
                "state-owned bridges."
            ),
        )
    )

    assert plan["metric_ids"] == [
        "inventory.bridge_count",
        "inventory.total_deck_area",
    ]

    coverage_plan = json.loads(
        http_service._state_owned_total_deck_plan(
            QueryScope(workspace_id=WORKSPACE, state_code=34, time_mode="latest"),
            question=(
                "Report bridge count, known-area count, and deck square footage "
                "for state-owned."
            ),
        )
    )
    assert coverage_plan["metric_ids"] == [
        "inventory.bridge_count",
        "inventory.known_deck_area_count",
        "inventory.total_deck_area",
    ]


@pytest.mark.parametrize(
    ("question", "expected_field", "expected_code", "expected_condition", "expected_metrics"),
    [
        (
            "Return bridge count and total deck area for state-owned operationally Poor bridges",
            "owner",
            "S01",
            "Poor",
            ["inventory.bridge_count", "inventory.total_deck_area"],
        ),
        (
            "Give bridge count for county-owned structures that are operationally Fair",
            "owner",
            "L01",
            "Fair",
            ["inventory.bridge_count"],
        ),
        (
            "Show total bridge length for NJDOT-maintained operationally Good bridges",
            "maintenance_responsibility",
            "S01",
            "Good",
            ["inventory.total_bridge_length"],
        ),
    ],
)
def test_reviewed_owner_condition_plan_preserves_both_filters(
    question,
    expected_field,
    expected_code,
    expected_condition,
    expected_metrics,
):
    raw = http_service._reviewed_owner_condition_plan(
        QueryScope(workspace_id=WORKSPACE, state_code=34, time_mode="latest"),
        question=question,
    )

    assert raw is not None
    plan = json.loads(raw)
    assert plan["metric_ids"] == expected_metrics
    assert plan["filters"] == [
        {"field_id": expected_field, "operator": "eq", "value": expected_code},
        {
            "field_id": "v_bridge_condition.condition_class",
            "operator": "eq",
            "value": expected_condition,
        },
    ]


@pytest.mark.parametrize(
    ("question", "required_fragments"),
    [
        (
            "What is the average deck area for state toll authority owned NHS-designated bridges?",
            ("s.owner = 'S03'", "h.nhs_designation = 'Y'", "s.bridge_number IN"),
        ),
        (
            "What is the concrete-deck bridge count for county-owned NHS-designated bridges?",
            (
                "s.owner = 'L01'",
                "d.has_concrete_deck = 1",
                "h.nhs_designation = 'Y'",
                "s.bridge_number IN",
            ),
        ),
        (
            "What is the total deck area for state park agency-owned non-NHS bridges?",
            ("s.owner = 'S02'", "h.nhs_designation = 'N'", "s.bridge_number IN"),
        ),
        (
            "What is the bridge count for state-owned bridges with AADT above 20000?",
            (
                "s.owner = 'S01'",
                "h.annual_average_daily_traffic > 20000",
                "s.bridge_number IN",
            ),
        ),
        (
            "What is the total deck area for county-owned bridges with minimum highway clearance below 14 feet?",
            (
                "s.owner = 'L01'",
                "h.highway_min_vertical_clearance < 14",
                "s.bridge_number IN",
            ),
        ),
        (
            "Rank deck-material status by concrete-deck bridge count from highest to lowest.",
            (
                "d.has_concrete_deck",
                "AS concrete_deck_bridge_count",
                "GROUP BY d.has_concrete_deck",
            ),
        ),
        (
            "Rank county by total deck area from highest to lowest.",
            (
                "c.county_code",
                "c.county_name",
                "c.total_deck_area",
                "ORDER BY c.total_deck_area DESC, c.county_code ASC",
            ),
        ),
        (
            "Using 2022 through 2025, show annual Poor counts and percentages.",
            (
                "c.data_year",
                "c.condition_class = 'Poor'",
                "AS poor_bridge_count",
                "AS poor_bridge_percent",
                "GROUP BY c.data_year",
            ),
        ),
        (
            "Operational Good, Fair, Poor, and Unknown bridge counts and state the rule.",
            (
                "c.condition_class = 'Good'",
                "AS good_bridge_count",
                "AS fair_bridge_count",
                "AS poor_bridge_count",
                "AS unknown_condition_count",
            ),
        ),
        (
            "Percentage of bridges with concrete deck material and state the calculation rule.",
            (
                "d.has_concrete_deck = 1",
                "AS concrete_deck_bridge_count",
                "AS concrete_deck_bridge_percent",
            ),
        ),
        (
            "Sum deck area in square feet; report unknown values separately.",
            (
                "s.calculated_deck_area",
                "total_calculated_deck_area_square_feet",
                "unknown_deck_area_count",
            ),
        ),
        (
            "Average AADT in vehicles per day; report unknown values separately.",
            (
                "h.annual_average_daily_traffic",
                "average_aadt_vehicles_per_day",
                "unknown_aadt_count",
            ),
        ),
        (
            "List deck condition ratings where N means not applicable.",
            (
                "s.deck_condition_rating",
                "CAST(s.deck_condition_rating AS TEXT) = 'N'",
                "deck_condition_rating_status",
            ),
        ),
        (
            "Compare total length and maximum span length in feet; report unknown values separately.",
            (
                "s.total_bridge_length",
                "s.maximum_span_length",
                "average_total_bridge_length_feet",
                "average_maximum_span_length_feet",
            ),
        ),
        (
            "What is the total deck area for operationally Poor bridges?",
            ("s.calculated_deck_area", "c.condition_class = 'Poor'"),
        ),
        (
            "What is the total deck area for carried-highway AADT above 20,000 vehicles per day bridges?",
            (
                "s.calculated_deck_area",
                "h.annual_average_daily_traffic > 20000",
                "s.bridge_number IN",
            ),
        ),
        (
            "What is the total deck area for longest-maximum-span bridges?",
            (
                "s.calculated_deck_area",
                "MAX(s.maximum_span_length)",
                "SELECT MAX(x.maximum_span_length)",
            ),
        ),
        (
            "What is the total deck area for minimum-highway-clearance bridges?",
            (
                "s.calculated_deck_area",
                "h.highway_min_vertical_clearance IS NOT NULL",
                "s.bridge_number IN",
            ),
        ),
        (
            "For each county, report Poor bridge count and total bridges.",
            (
                "s.county_code",
                "c.condition_class = 'Poor'",
                "AS poor_bridge_count",
                "COUNT(*) AS bridge_count",
                "ORDER BY poor_bridge_count DESC, s.county_code ASC",
            ),
        ),
        (
            "Rank condition class by bridge count from highest to lowest.",
            (
                "c.condition_class",
                "COUNT(*) AS bridge_count",
                "GROUP BY c.condition_class",
                "ORDER BY bridge_count DESC, c.condition_class ASC",
            ),
        ),
        (
            "List NHS bridges carrying Interstate routes and show AADT, returning one row per bridge.",
            (
                "h.nhs_designation = 'Y'",
                "r.route_type = '1'",
                "MAX(h.annual_average_daily_traffic)",
                "GROUP BY h.bridge_number",
            ),
        ),
        (
            "Count distinct county-owned bridges having concrete deck span sets, returning one row per bridge.",
            (
                "COUNT(DISTINCT s.bridge_number)",
                "s.owner = 'L01'",
                "d.has_concrete_deck = 1",
                "m.deck_material_and_type IN",
            ),
        ),
        (
            "Sum element quantities by condition state for NHS bridges, returning one row per bridge.",
            (
                "SUM(e.element_total_quantity)",
                "element_quantity_condition_state_one",
                "h.nhs_designation = 'Y'",
                "GROUP BY e.bridge_number",
            ),
        ),
        (
            "Show bridges with load-posting events and the latest status-change date.",
            (
                "p.bridge_number",
                "MAX(p.load_posting_status) AS load_posting_status",
                "MAX(q.posting_status_change_date) AS posting_status_change_date",
                "p.posting_status_change_date = latest.posting_status_change_date",
            ),
        ),
        (
            "List routes and directions for bridges whose carried-highway AADT exceeds 15000.",
            (
                "MIN(r.route_number)",
                "MIN(r.route_direction)",
                "h.annual_average_daily_traffic > 15000",
                "GROUP BY h.bridge_number",
            ),
        ),
        (
            "Compare number of span configurations with total spans for every bridge.",
            (
                "COUNT(DISTINCT d.span_configuration_designation)",
                "SUM(d.number_of_spans)",
                "GROUP BY d.bridge_number",
            ),
        ),
        (
            "Find Poor bridges with concrete decks and report owner plus deck area.",
            (
                "c.condition_class = 'Poor'",
                "d.has_concrete_deck = 1",
                "s.owner",
                "s.calculated_deck_area",
            ),
        ),
        (
            "Using the latest published inventory, rank New Jersey counties by the total deck area of distinct bridges carrying an NHS-designated highway feature with AADT above 20,000 and minimum highway vertical clearance below 14.5 feet. Also return the distinct bridge count, without double-counting bridges with multiple features.",
            (
                "COUNT(DISTINCT s.bridge_number)",
                "SUM(s.calculated_deck_area)",
                "h.nhs_designation = 'Y'",
                "h.annual_average_daily_traffic > 20000",
                "f.highway_min_vertical_clearance < 14.5",
            ),
        ),
        (
            "Using the latest snapshot for each bridge, list operationally Poor bridges with concrete decks that have major superstructure rehabilitation and an element quantity greater than zero in condition state four. Return one row per bridge with bridge number, bridge name, deck area, most recent work year, and summed condition-state-four quantity; show the top 20 by that quantity.",
            (
                "WITH latest_sp2 AS",
                "w.work_performed = 'SP2'",
                "WITH latest_sp2 AS (SELECT",
                "SUM(e.element_quantity_condition_state_four)",
                "c.condition_class = 'Poor'",
                "d.has_concrete_deck = 1",
                "LIMIT 20",
            ),
        ),
        (
            "Rank New Jersey counties by average calculated deck area for county-owned bridges, returning county name, distinct bridge count, and average deck area.",
            (
                "s.owner = 'L01'",
                "AS county_name",
                "COUNT(DISTINCT s.bridge_number)",
                "AVG(s.calculated_deck_area) AS average_deck_area",
            ),
        ),
        (
            "For each New Jersey county, count distinct operationally Poor bridges with concrete decks and sum their calculated deck area; rank counties by total deck area.",
            (
                "c.condition_class = 'Poor'",
                "d.has_concrete_deck = 1",
                "COUNT(DISTINCT s.bridge_number)",
                "SUM(s.calculated_deck_area) AS total_deck_area",
            ),
        ),
        (
            "From inventory years 2021 through 2025, return yearly NJDOT-maintained distinct bridge count, total deck area, and operational Poor bridge percentage. Use maintenance responsibility B.CL.02 = S01, not owner.",
            (
                "s.maintenance_responsibility = 'S01'",
                "COUNT(DISTINCT s.bridge_number) AS bridge_count",
                "SUM(s.calculated_deck_area) AS total_deck_area",
                "AS poor_bridge_percent",
                "GROUP BY s.data_year",
            ),
        ),
    ],
)
def test_reviewed_cross_resource_sql_preserves_compound_filters_without_correlated_aliases(
    question,
    required_fragments,
):
    sql = http_service._reviewed_cross_resource_sql(
        QueryScope(workspace_id=WORKSPACE, state_code=34, time_mode="latest"),
        question=question,
    )

    assert sql is not None
    for fragment in required_fragments:
        assert fragment in sql
    assert "WHERE s.workspace_id = h.workspace_id" not in sql


@pytest.mark.parametrize(
    ("question", "required_fragments"),
    [
        (
            "Which latest published bridges have known latitude and longitude? List their identifiers and coordinates.",
            ("v_current_bridge_snapshots", "s.bridge_name", "s.latitude", "s.longitude"),
        ),
        (
            "List the 20 oldest latest-published bridges by year built, including bridge number and name.",
            ("v_current_bridge_snapshots", "s.year_built", "LIMIT 20"),
        ),
        (
            "How many current published bridges in Bergen County, county FIPS 003, have concrete decks?",
            (
                "v_current_bridge_snapshots.county_code = 3",
                "d.has_concrete_deck = 1",
                "metric__inventory__current_concrete_deck_bridge_count",
            ),
        ),
        (
            "List latest published span configurations with prestressed pre-tensioned concrete deck code C03.",
            ("v_bridge_span_sets", "d.deck_material_and_type = 'C03'"),
        ),
        (
            "Which latest concrete deck span configurations use epoxy-coated reinforcing protection, BSP12 code C01?",
            ("v_bridge_span_sets", "d.deck_reinforcing_protective_system = 'C01'"),
        ),
        (
            "Which latest published bridges have a superstructure condition rating of 4 or lower?",
            ("v_current_bridge_snapshots", "s.superstructure_condition_rating BETWEEN 0 AND 4"),
        ),
        (
            "List latest bridge condition records where the supplied ratings conflict with the controlling-condition evidence.",
            ("v_bridge_condition", "c.bc01", "c.bc13", "c.condition_conflict = 1"),
        ),
        (
            "List latest carried-highway features with AADT greater than 50000 vehicles per day.",
            ("v_carried_highway_features", "h.feature_name", "h.annual_average_daily_traffic > 50000"),
        ),
        (
            "Which latest highway features have minimum vertical clearance below 14.5 feet?",
            ("v_bridge_features", "h.feature_name", "h.highway_min_vertical_clearance < 14.5"),
        ),
        (
            "List route number, route direction, route type, and service type for latest published bridge routes.",
            ("v_bridge_routes", "r.route_number", "r.route_direction", "r.route_type"),
        ),
        (
            "List latest work records that contain major superstructure rehabilitation code SP2.",
            ("v_bridge_works", "w.work_performed = 'SP2'"),
        ),
        (
            "Which latest element records have a quantity greater than zero in condition state four?",
            ("v_bridge_elements", "e.element_total_quantity", "e.element_quantity_condition_state_four > 0"),
        ),
    ],
)
def test_reviewed_snbi_example_sql_preserves_requested_resource_and_fields(
    question,
    required_fragments,
):
    sql = http_service._reviewed_snbi_example_sql(
        QueryScope(workspace_id=WORKSPACE, state_code=34, time_mode="latest"),
        question=question,
    )

    assert sql is not None
    for fragment in required_fragments:
        assert fragment in sql


def _insert_http_query_workspace(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO workspaces (id,slug,display_name,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            WORKSPACE,
            WORKSPACE,
            "HTTP query tests",
            "active",
            "2026-09-13T00:00:00Z",
            "2026-09-13T00:00:00Z",
        ),
    )
    connection.execute(
        "INSERT INTO workspace_state "
        "(workspace_id,data_generation,schema_generation,updated_at) VALUES (?,?,?,?)",
        (WORKSPACE, 1, LATEST_SCHEMA_VERSION, "2026-09-13T00:00:00Z"),
    )


def test_single_bridge_deck_area_uses_verified_scope_and_governed_fallback(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "single-bridge-deck.sqlite3"
    ensure_schema(str(database))
    connection = sqlite3.connect(database)
    try:
        _insert_http_query_workspace(connection)
        connection.execute(
            "INSERT INTO bridges "
            "(state_code,bridge_number,bridge_name,data_year,total_bridge_length,"
            "bridge_width_out_to_out,approach_roadway_width,calculated_deck_area) "
            "VALUES (34,'01M0019','Fallback deck bridge',2025,30.8,32.8,29.9,NULL)"
        )
        connection.commit()
    finally:
        connection.close()

    monkeypatch.setenv("BRIDGEGUARD_QUERY_MODE", "plan_required")
    monkeypatch.setattr(
        http_service,
        "_provider_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("the reviewed single-bridge route must be provider-free")
        ),
    )
    events = list(
        http_service.stream_bridgette_semantic_query(
            database_path=database,
            workspace_id=WORKSPACE,
            request=Request(
                question="What is the deck area in square feet of bridge 01M0019?"
            ),
        )
    )

    evidence_event, token_event = events
    assert evidence_event["type"] == "evidence"
    assert evidence_event["scope"]["bridge_number"] == "01M0019"
    assert evidence_event["interpreted_scope"]["bridge_number"] == "01M0019"
    assert evidence_event["query_plan"]["routing"] == (
        "reviewed_single_bridge_deck_area_lookup"
    )
    assert evidence_event["metric_definitions"] == []
    assert evidence_event["columns"] == [
        "bridge_number",
        "bridge_name",
        "data_year",
        "total_bridge_length",
        "bridge_width_out_to_out",
        "reported_calculated_deck_area",
        "calculated_deck_area",
        "calculated_deck_area_method",
    ]
    assert evidence_event["rows"] == [
        [
            "01M0019",
            "Fallback deck bridge",
            2025,
            30.8,
            32.8,
            None,
            1010.2,
            "calculated_length_times_out_to_out_width",
        ]
    ]
    assert token_event["type"] == "token"
    assert token_event["content"] == (
        "For inventory year **2025**, bridge **01M0019** has a deck area of "
        "**1,010.2 square feet**. No positive reported BG16 value is available, "
        "so Bridgette used **30.8 ft × 32.8 ft** (total length × out-to-out "
        "width), rounded to one decimal place."
    )


def test_state_owned_total_deck_area_uses_s01_and_real_governed_metric(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "state-owned-deck.sqlite3"
    ensure_schema(str(database))
    connection = sqlite3.connect(database)
    try:
        _insert_http_query_workspace(connection)
        connection.executemany(
            "INSERT INTO bridges "
            "(state_code,bridge_number,data_year,owner,total_bridge_length,"
            "bridge_width_out_to_out,approach_roadway_width,calculated_deck_area) "
            "VALUES (34,?,?,?,?,?,?,?)",
            (
                ("S01-A", 2025, "S01", 30.0, 10.0, 9.0, None),
                ("S01-B", 2025, "S01", 20.0, 5.0, 4.0, None),
                ("S03-A", 2025, "S03", 100.0, 10.0, 9.0, None),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    monkeypatch.setenv("BRIDGEGUARD_QUERY_MODE", "plan_required")
    monkeypatch.setattr(
        http_service,
        "_provider_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("the reviewed state-owned deck route must be provider-free")
        ),
    )
    events = list(
        http_service.stream_bridgette_semantic_query(
            database_path=database,
            workspace_id=WORKSPACE,
            request=Request(
                question="What is the total deck area for the bridge state owned?"
            ),
        )
    )

    evidence_event, token_event = events
    assert evidence_event["type"] == "evidence"
    assert evidence_event["query_plan"]["routing"] == (
        "reviewed_state_owned_total_deck_area"
    )
    assert evidence_event["columns"] == ["metric__inventory__total_deck_area"]
    assert evidence_event["rows"] == [[400.0]]
    assert token_event["type"] == "token"
    assert token_event["content"] == (
        "Bridges owned by the **State Transportation Department** "
        "(**Owner B.CL.01 = S01**) have a total calculated deck area of "
        "**400.0 square feet** in the selected inventory. State parks, the state "
        "toll authority, and other state agencies use separate owner codes and "
        "are not included."
    )


def test_unknown_single_bridge_deck_area_does_not_fall_back_to_portfolio_query(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "missing-single-bridge-deck.sqlite3"
    ensure_schema(str(database))
    connection = sqlite3.connect(database)
    try:
        _insert_http_query_workspace(connection)
        connection.execute(
            "INSERT INTO bridges (state_code,bridge_number,data_year) "
            "VALUES (34,'KNOWN-01',2025)"
        )
        connection.commit()
    finally:
        connection.close()

    monkeypatch.setattr(
        http_service,
        "_provider_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an unresolved bridge must not reach a provider")
        ),
    )
    events = list(
        http_service.stream_bridgette_semantic_query(
            database_path=database,
            workspace_id=WORKSPACE,
            request=Request(question="What is the deck area of bridge UNKNOWN-99?"),
        )
    )

    assert len(events) == 1
    assert events[0]["type"] == "clarification"
    assert events[0]["code"] == "unresolved_bridge_identity"
    assert "did not substitute a portfolio deck-area total" in events[0]["message"]


def test_scalar_answer_is_provider_free_after_sql_and_emits_rich_evidence(
    monkeypatch,
):
    _install_query(monkeypatch, _evidence(["bridge_count"], [[12]]))
    prompts = []

    def provider(prompt, _cancellation_token, *, timeout, num_predict=1024, response_format=None):
        prompts.append((prompt, timeout))
        assert response_format["type"] == "json_schema"
        assert response_format["json_schema"]["strict"] is True
        return _PLAN_JSON

    monkeypatch.setattr(http_service, "_provider_text", provider)
    events = list(
        http_service.stream_bridgette_semantic_query(
            database_path="isolated.sqlite3",
            workspace_id=WORKSPACE,
            request=Request(),
        )
    )

    assert len(prompts) == 1
    evidence_event, token_event = events
    assert evidence_event["type"] == "evidence"
    assert evidence_event["interpreted_scope"]["scope_injected_by_server"] is True
    assert "every bridge" in evidence_event["interpreted_scope"]["interpretation"]
    assert evidence_event["query_plan"]["compiled_from_validated_sql"] is True
    assert evidence_event["query_plan"]["result_shape"] == "scalar"
    assert evidence_event["metric_definitions"][0]["metric_id"] == "inventory.bridge_count"
    assert evidence_event["source_snapshot_status"] == "loaded_inventory"
    assert evidence_event["source_status"]["authoritative_source"].startswith("snbi_bridges.db")
    assert evidence_event["catalog_version"] == "bridgeguard-semantic-v6-safe-metric-joins"
    assert evidence_event["catalog_sha256"] == "a" * 64
    assert evidence_event["snbi_dictionary_version"] == "bridgeguard-snbi-dictionary-v1"
    assert evidence_event["snbi_dictionary_sha256"] == "b" * 64
    assert evidence_event["compiled_query_sha256"] == "c" * 64
    assert evidence_event["result_download"] == {
        "available": False,
        "bounded": True,
        "reason": "result_artifact_not_registered",
    }
    verification = evidence_event["answer_verification"]
    assert verification["mode"] == "deterministic"
    assert verification["provider_calls"] == 0
    assert verification["claims"][0]["evidence_cells"] == ["r0.bridge_count"]
    assert token_event["content"].startswith("**12 bridges**")


def test_plan_prompt_embeds_reviewed_context_without_double_json_encoding() -> None:
    generation = PlanGenerationRequest(
        request_id=REQUEST_ID,
        attempt=1,
        question="Count bridges",
        scope=Request.scope,
        context_json='{"semantic_catalog":{"resources":[]}}',
        context_sha256="e" * 64,
    )

    prompt = http_service._semantic_plan_prompt(generation)

    assert '"content":{"semantic_catalog":{"resources":[]}}' in prompt
    assert '"content_json":"{' not in prompt
    assert "maximum span length uses maximum_span_length" in prompt
    assert "filters reference joined fields" in prompt
    assert "never add state_code for New Jersey" in prompt


def test_plan_response_format_is_strict_and_scope_bound() -> None:
    generation = PlanGenerationRequest(
        request_id=REQUEST_ID,
        attempt=1,
        question="Count bridges",
        scope=Request.scope,
        context_json="{}",
        context_sha256="e" * 64,
    )

    response_format = http_service._bridge_query_plan_response_format(generation)
    assert response_format["type"] == "json_schema"
    contract = response_format["json_schema"]
    assert contract["strict"] is True
    schema = contract["schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["time_scope"]["properties"]["mode"]["enum"] == [
        "latest"
    ]
    assert set(schema["required"]) == set(schema["properties"])


def test_plan_response_format_constrains_reviewed_resource_and_metric_ids() -> None:
    context = {
        "semantic_catalog": {
            "resources": [
                {
                    "id": "v_current_bridge_snapshots",
                    "columns": [
                        ["bridge_number", "text"],
                        ["owner", "text"],
                        ["workspace_id", "text"],
                    ],
                }
            ]
        },
        "reviewed_metric_registry": {
            "columns": ["metric_id", "status"],
            "rows": [
                ["inventory.bridge_count", "active"],
                ["inventory.disabled_count", "disabled"],
            ],
        },
    }
    generation = PlanGenerationRequest(
        request_id=REQUEST_ID,
        attempt=1,
        question="Count bridges",
        scope=Request.scope,
        context_json=json.dumps(context),
        context_sha256="e" * 64,
    )

    schema = http_service._bridge_query_plan_response_format(generation)["json_schema"][
        "schema"
    ]
    properties = schema["properties"]
    assert properties["resource_id"]["enum"] == ["v_current_bridge_snapshots"]
    assert properties["metric_ids"]["items"]["enum"] == [
        "inventory.bridge_count"
    ]
    # Field identifiers remain checked by the typed validator. Repeating the
    # full field enum here would exceed Cerebras's 5,000-character strict
    # response-schema limit.
    assert properties["filters"]["items"]["properties"]["field_id"] == {
        "type": "string"
    }


def test_provider_failure_log_is_classified_without_sensitive_exception_text(caplog) -> None:
    class ProviderFailure(Exception):
        status_code = 429
        code = None
        body = {"error": {"code": "rate_limit_exceeded"}}

    failure = ProviderFailure("secret prompt and provider response body")
    with caplog.at_level(logging.WARNING, logger=http_service.__name__):
        http_service._log_provider_failure(
            request_id=REQUEST_ID,
            phase="plan",
            attempt=2,
            exc=failure,
        )

    assert "exception_type=ProviderFailure" in caplog.text
    assert "http_status=429" in caplog.text
    assert "provider_code=rate_limit_exceeded" in caplog.text
    assert "secret prompt" not in caplog.text
    assert "provider response body" not in caplog.text


def test_verified_response_is_audited_before_download_or_release(monkeypatch):
    _install_query(monkeypatch, _evidence(["bridge_count"], [[12]]))
    monkeypatch.setattr(
        http_service,
        "_provider_text",
        lambda _prompt, _token, *, timeout, num_predict=1024, response_format=None: _PLAN_JSON,
    )
    calls = []

    def audit(_database_path, audit_request):
        calls.append(("audit", audit_request))
        return audit_request

    def download(_request, _evidence):
        calls.append(("download", None))
        return {"available": False, "bounded": True}

    events = list(
        http_service.stream_bridgette_semantic_query(
            database_path="isolated.sqlite3",
            workspace_id=WORKSPACE,
            request=Request(),
            response_audit_hook=audit,
            result_download_hook=download,
        )
    )
    assert [name for name, _value in calls] == ["audit", "download"]
    receipt = calls[0][1]
    assert receipt.request_id == REQUEST_ID
    assert receipt.workspace_id == WORKSPACE
    assert receipt.actor_id == "viewer@example.com"
    assert receipt.plan.canonical_payload() == json.loads(_PLAN_JSON)
    assert receipt.compiled_query_sha256 == "c" * 64
    assert receipt.claim_count == receipt.verified_claim_count == 1
    assert receipt.rejected_claim_count == 0
    # This isolated mock omits embedded PlanEvidence; production plan evidence
    # records the separate plan-provider call.
    assert receipt.provider_call_count == 0
    assert [event["type"] for event in events] == ["evidence", "token"]


def test_response_audit_failure_blocks_download_and_every_stream_event(monkeypatch):
    _install_query(monkeypatch, _evidence(["bridge_count"], [[12]]))
    monkeypatch.setattr(
        http_service,
        "_provider_text",
        lambda _prompt, _token, *, timeout, num_predict=1024, response_format=None: _PLAN_JSON,
    )
    download_called = False

    def download(_request, _evidence):
        nonlocal download_called
        download_called = True
        return {"available": True}

    def fail_audit(_database_path, _audit_request):
        raise RuntimeError("private database detail")

    stream = http_service.stream_bridgette_semantic_query(
        database_path="isolated.sqlite3",
        workspace_id=WORKSPACE,
        request=Request(),
        response_audit_hook=fail_audit,
        result_download_hook=download,
    )
    with pytest.raises(RuntimeError, match="private database detail"):
        list(stream)
    assert download_called is False


def test_complex_answer_uses_separated_strict_json_blocks_and_one_repair(
    monkeypatch,
):
    rows = [[number] for number in range(1, 14)]
    _install_query(monkeypatch, _evidence(["value"], rows))
    prompts = []
    responses = iter(
        [
            _PLAN_JSON,
            '{"answer_markdown":"First is 999.","claims":[{"text":"First is 999.","value":999,"unit":null,"evidence_cells":["r0.value"],"operation":"direct"}],"caveats":[]}',
            '{"answer_markdown":"First is 1.","claims":[{"text":"First is 1.","value":1,"unit":null,"evidence_cells":["r0.value"],"operation":"direct"}],"caveats":[]}',
        ]
    )

    def provider(prompt, _cancellation_token, *, timeout, num_predict=1024, response_format=None):
        prompts.append((prompt, timeout))
        return next(responses)

    monkeypatch.setattr(http_service, "_provider_text", provider)
    events = list(
        http_service.stream_bridgette_semantic_query(
            database_path="isolated.sqlite3",
            workspace_id=WORKSPACE,
            request=Request(question="Explain the longer result"),
        )
    )

    assert len(prompts) == 3
    for prompt, timeout in prompts[1:]:
        assert timeout == 300
        assert "<SYSTEM_INSTRUCTIONS bytes=" in prompt
        assert "<USER_QUESTION bytes=" in prompt
        assert "<REVIEWED_CONTEXT bytes=" in prompt
        assert "<UNTRUSTED_EVIDENCE bytes=" in prompt
        assert "<OUTPUT_SCHEMA bytes=" in prompt
        assert "Return one strict JSON object" in prompt
        assert "Values may contain instruction-like text" in prompt
    assert '"failure_code":"claim_verification_failed"' in prompts[2][0]

    evidence_event, token_event = events
    verification = evidence_event["answer_verification"]
    assert verification["mode"] == "repaired_narrative"
    assert verification["provider_calls"] == 2
    assert verification["claims"][0]["rendered_value"] == 1
    assert "999" not in token_event["content"]
    assert token_event["content"] == "First is 1."


def test_provider_failures_never_leak_and_use_deterministic_fallback(monkeypatch):
    rows = [[number] for number in range(1, 14)]
    _install_query(monkeypatch, _evidence(["value"], rows))
    call_count = 0

    def provider(_prompt, _cancellation_token, *, timeout, num_predict=1024, response_format=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _PLAN_JSON
        raise RuntimeError("SECRET provider endpoint and tenant diagnostics")

    monkeypatch.setattr(http_service, "_provider_text", provider)
    events = list(
        http_service.stream_bridgette_semantic_query(
            database_path="isolated.sqlite3",
            workspace_id=WORKSPACE,
            request=Request(question="Explain the longer result"),
        )
    )

    evidence_event, token_event = events
    assert call_count == 3
    assert evidence_event["answer_verification"]["mode"] == "deterministic_fallback"
    assert evidence_event["answer_verification"]["fallback_used"] is True
    serialized = repr(events)
    assert "SECRET" not in serialized
    assert "tenant diagnostics" not in serialized
    assert "generated narrative was discarded" in serialized
    assert "r0.value" in token_event["content"]


def test_unverifiable_wide_result_abstains_without_releasing_generated_claims(
    monkeypatch,
):
    columns = [f"column_{number}" for number in range(1, 10)]
    _install_query(monkeypatch, _evidence(columns, [list(range(1, 10))]))
    responses = iter(
        [
            _PLAN_JSON,
            '{"answer_markdown":"Invented 999.","claims":[{"text":"Invented 999.","value":999,"unit":null,"evidence_cells":["r0.column_1"],"operation":"direct"}],"caveats":[]}',
            '{"answer_markdown":"Still 999.","claims":[{"text":"Still 999.","value":999,"unit":null,"evidence_cells":["r0.column_1"],"operation":"direct"}],"caveats":[]}',
        ]
    )
    monkeypatch.setattr(
        http_service,
        "_provider_text",
        lambda _prompt, _token, *, timeout, num_predict=1024, response_format=None: next(responses),
    )

    evidence_event, token_event = list(
        http_service.stream_bridgette_semantic_query(
            database_path="isolated.sqlite3",
            workspace_id=WORKSPACE,
            request=Request(question="Explain the wide result"),
        )
    )
    assert evidence_event["answer_status"] == "provisional"
    assert evidence_event["answer_verification"]["status"] == "provisional"
    assert evidence_event["answer_verification"]["claims"] == []
    assert "999" not in token_event["content"]
    assert "could not verify a narrative summary" in token_event["content"]


def test_plan_and_download_hooks_are_canonicalized_and_fail_closed(monkeypatch):
    evidence = _evidence(["bridge_count"], [[12]])
    _install_query(monkeypatch, evidence)
    monkeypatch.setattr(
        http_service,
        "_provider_text",
        lambda _prompt, _token, *, timeout, num_predict=1024, response_format=None: (
            _PLAN_JSON
        ),
    )
    download = {
        "available": True,
        "href": f"/api/bridgette/requests/{REQUEST_ID}/result",
        "sha256": "f" * 64,
        "bounded": True,
    }
    event = list(
        http_service.stream_bridgette_semantic_query(
            database_path="isolated.sqlite3",
            workspace_id=WORKSPACE,
            request=Request(),
            query_plan_hook=lambda _request, _evidence: SimpleNamespace(
                as_dict=lambda: {
                    "version": "bridge-query-plan-v1",
                    "intent": "scalar",
                }
            ),
            result_download_hook=lambda _request, _evidence: download,
        )
    )[0]
    download["sha256"] = "changed-after-hook"
    assert event["query_plan"] == {
        "intent": "scalar",
        "version": "bridge-query-plan-v1",
    }
    assert event["result_download"]["sha256"] == "f" * 64

    failed = list(
        http_service.stream_bridgette_semantic_query(
            database_path="isolated.sqlite3",
            workspace_id=WORKSPACE,
            request=Request(request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            result_download_hook=lambda _request, _evidence: (_ for _ in ()).throw(
                RuntimeError("storage path secret")
            ),
        )
    )[0]
    assert failed["result_download"]["reason"] == "metadata_hook_failed"
    assert "storage path secret" not in repr(failed)


def test_deterministic_csv_neutralizes_formula_prefixes_after_whitespace():
    evidence = _evidence(
        ["bridge_number", "note"],
        [
            ["0001", "=HYPERLINK(\"https://bad\")"],
            ["0002", "   +SUM(1,2)"],
            ["0003", "\t-2+3"],
            ["0004", "\n@IMPORTXML(x)"],
            ["0005", "ordinary text"],
        ],
    )
    content = http_service.deterministic_result_csv(evidence)
    assert content == http_service.deterministic_result_csv(evidence)
    assert content.endswith(b"\r\n")
    parsed = list(csv.reader(io.StringIO(content.decode("utf-8"), newline="")))
    assert parsed[0] == ["bridge_number", "note"]
    assert parsed[1][1].startswith("'=")
    assert parsed[2][1].startswith("'   +")
    assert parsed[3][1].startswith("'\t-")
    assert parsed[4][1].startswith("'\n@")
    assert parsed[5][1] == "ordinary text"


def test_register_csv_result_is_owner_scoped_hash_addressed_and_path_free():
    store = BoundedResultStore()
    request = Request()
    evidence = _evidence(
        ["bridge_number", "formula_like_note"],
        [["0001", " =1+1"]],
    )
    metadata = http_service.register_csv_result(store, request, evidence)
    stored = store.get(
        request_id=request.request_id,
        owner_id=request.actor_id,
        workspace_id=request.scope.workspace_id,
    )
    assert stored is not None
    assert stored.content == http_service.deterministic_result_csv(evidence)
    assert metadata == stored.metadata()
    assert metadata["available"] is True
    assert metadata["href"] == f"/api/bridgette/requests/{REQUEST_ID}/result"
    assert metadata["filename"].startswith("bridgette-")
    assert metadata["filename"].endswith(".csv")
    assert metadata["sha256"] == stored.sha256
    assert metadata["row_count"] == 1
    assert metadata["column_count"] == 2
    assert "path" not in repr(metadata).casefold()
    assert "isolated.sqlite3" not in repr(metadata)
    assert store.get(
        request_id=request.request_id,
        owner_id="other@example.com",
        workspace_id=request.scope.workspace_id,
    ) is None
    assert store.get(
        request_id=request.request_id,
        owner_id=request.actor_id,
        workspace_id="other-workspace",
    ) is None


def test_interpretation_clarifies_before_database_model_or_sql(monkeypatch):
    calls = []
    monkeypatch.setattr(
        http_service,
        "get_workspace_state",
        lambda *_args, **_kwargs: calls.append("database") or (_ for _ in ()).throw(
            AssertionError("database must not be touched")
        ),
    )
    monkeypatch.setattr(
        http_service,
        "_provider_text",
        lambda *_args, **_kwargs: calls.append("model") or (_ for _ in ()).throw(
            AssertionError("model must not be touched")
        ),
    )
    events = list(
        http_service.stream_bridgette_semantic_query(
            database_path="does-not-exist.sqlite3",
            workspace_id=WORKSPACE,
            request=Request(question="How many current NHS bridges have bad condition?"),
        )
    )
    assert len(events) == 1
    assert events[0]["type"] == "clarification"
    assert events[0]["code"] == "nhs_meaning"
    assert calls == []


def test_rejected_plan_prompts_for_context_instead_of_emitting_an_error(monkeypatch):
    monkeypatch.setenv("BRIDGEGUARD_QUERY_MODE", "plan_required")
    monkeypatch.setattr(
        http_service,
        "get_workspace_state",
        lambda _database, _workspace: SimpleNamespace(
            schema_generation=5,
            data_generation=9,
        ),
    )

    def reject_plan(**_kwargs):
        raise SemanticQueryAbstained(
            "Bridgette could not produce an approved query plan and abstained.",
            request_id=REQUEST_ID,
            attempts=2,
            details={"last_failure_code": "invalid_plan_json"},
        )

    monkeypatch.setattr(http_service, "execute_semantic_plan_query", reject_plan)

    events = list(
        http_service.stream_bridgette_semantic_query(
            database_path="isolated.sqlite3",
            workspace_id=WORKSPACE,
            request=Request(),
        )
    )

    assert len(events) == 1
    event = events[0]
    assert event["type"] == "clarification"
    assert event["code"] == "query_needs_more_context"
    assert "query-specific suggestions" in event["message"]
    assert len(event["options"]) == 3
    assert [option["id"] for option in event["options"]] == [
        "latest_bridge_count",
        "latest_county_counts",
        "latest_bridge_examples",
    ]
    assert len({option["suggested_question"] for option in event["options"]}) == 3
    assert all("Additional context:" in option["suggested_question"] for option in event["options"])
    assert event["interpreted_scope"]["workspace_id"] == WORKSPACE
    assert event["warnings"] == [
        "No database query was run because the generated plans did not pass validation."
    ]
    assert "invalid_plan_json" not in repr(event)


def test_hybrid_mode_returns_validated_sql_answer_when_plan_is_rejected(monkeypatch):
    monkeypatch.setenv("BRIDGEGUARD_QUERY_MODE", "hybrid")
    monkeypatch.setattr(
        http_service,
        "get_workspace_state",
        lambda _database, _workspace: SimpleNamespace(
            schema_generation=5,
            data_generation=9,
        ),
    )

    def reject_plan(**_kwargs):
        raise SemanticQueryAbstained(
            "The generated plan did not pass validation.",
            request_id=REQUEST_ID,
            attempts=2,
            details={"last_failure_code": "invalid_plan_json"},
        )

    def execute_validated_sql(*, request, generate_sql, **_kwargs):
        sql = generate_sql(
            SqlGenerationRequest(
                request_id=request.request_id,
                attempt=1,
                question=request.question,
                scope=request.scope,
                context_json="{}",
                context_sha256="e" * 64,
            )
        )
        assert sql == "SELECT COUNT(*) AS bridge_count FROM v_current_bridge_snapshots"
        return _evidence(["bridge_count"], [[12]])

    def provider(_prompt, _cancellation_token, **_kwargs):
        return json.dumps(
            {
                "sql": "SELECT COUNT(*) AS bridge_count FROM v_current_bridge_snapshots"
            }
        )

    monkeypatch.setattr(http_service, "execute_semantic_plan_query", reject_plan)
    monkeypatch.setattr(http_service, "execute_semantic_query", execute_validated_sql)
    monkeypatch.setattr(http_service, "_provider_text", provider)

    events = list(
        http_service.stream_bridgette_semantic_query(
            database_path="isolated.sqlite3",
            workspace_id=WORKSPACE,
            request=Request(),
        )
    )

    assert [event["type"] for event in events] == ["evidence", "token"]
    evidence_event = events[0]
    assert evidence_event["answer_mode"] == "validated_sql_fallback"
    assert evidence_event["answer_status"] == "verified"
    assert evidence_event["query_language"] == "expert_sql"
    assert evidence_event["fallback"] == {
        "used": True,
        "reason": "plan_rejected",
    }
    assert evidence_event["query_plan"]["hybrid_fallback_used"] is True
    assert any("SQL-assisted" in warning for warning in evidence_event["warnings"])
    assert "12" in events[1]["content"]


def test_hybrid_mode_keeps_verified_plan_when_planning_succeeds(monkeypatch):
    monkeypatch.setenv("BRIDGEGUARD_QUERY_MODE", "hybrid")
    _install_query(monkeypatch, _evidence(["bridge_count"], [[12]]))
    monkeypatch.setattr(
        http_service,
        "execute_semantic_query",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("expert SQL must not run when the reviewed plan succeeds")
        ),
    )
    monkeypatch.setattr(
        http_service,
        "_provider_text",
        lambda *_args, **_kwargs: _PLAN_JSON,
    )

    events = list(
        http_service.stream_bridgette_semantic_query(
            database_path="isolated.sqlite3",
            workspace_id=WORKSPACE,
            request=Request(),
        )
    )

    assert events[0]["answer_mode"] == "verified_semantic_plan"
    assert events[0]["answer_status"] == "verified"
    assert events[0]["query_language"] == "bridge_query_plan"
    assert events[0]["fallback"] == {"used": False, "reason": None}


def test_hybrid_mode_cross_checks_empty_plan_with_validated_sql(monkeypatch):
    monkeypatch.setenv("BRIDGEGUARD_QUERY_MODE", "hybrid")
    monkeypatch.setattr(
        http_service,
        "get_workspace_state",
        lambda _database, _workspace: SimpleNamespace(
            schema_generation=5,
            data_generation=9,
        ),
    )
    monkeypatch.setattr(
        http_service,
        "execute_semantic_plan_query",
        lambda **_kwargs: _evidence(["span_configuration"], []),
    )

    def execute_validated_sql(*, request, generate_sql, **_kwargs):
        generate_sql(
            SqlGenerationRequest(
                request_id=request.request_id,
                attempt=1,
                question=request.question,
                scope=request.scope,
                context_json="{}",
                context_sha256="e" * 64,
            )
        )
        return _evidence(["span_configuration"], [["M01"], ["A01"]])

    monkeypatch.setattr(http_service, "execute_semantic_query", execute_validated_sql)
    monkeypatch.setattr(
        http_service,
        "_provider_text",
        lambda *_args, **_kwargs: json.dumps(
            {"sql": "SELECT span_configuration FROM v_current_bridge_snapshots"}
        ),
    )

    events = list(
        http_service.stream_bridgette_semantic_query(
            database_path="isolated.sqlite3",
            workspace_id=WORKSPACE,
            request=Request(question="List latest span configurations with a concrete deck"),
        )
    )

    assert events[0]["answer_mode"] == "validated_sql_fallback"
    assert events[0]["fallback"] == {
        "used": True,
        "reason": "plan_returned_no_rows",
    }
    assert events[0]["rows"] == [["M01"], ["A01"]]


def test_hybrid_mode_uses_guarded_raw_sql_after_both_reviewed_paths_fail(monkeypatch):
    monkeypatch.setenv("BRIDGEGUARD_QUERY_MODE", "hybrid")
    monkeypatch.setattr(
        http_service,
        "get_workspace_state",
        lambda _database, _workspace: SimpleNamespace(
            schema_generation=5,
            data_generation=9,
        ),
    )

    def reject_plan(**_kwargs):
        raise SemanticQueryAbstained(
            "The generated plan did not pass validation.",
            request_id=REQUEST_ID,
            attempts=2,
            details={"last_failure_code": "invalid_plan_json"},
        )

    def reject_expert_sql(**_kwargs):
        raise SemanticQueryGenerationFailed(
            "The reviewed SQL could not be generated.",
            request_id=REQUEST_ID,
            attempts=2,
        )

    def guarded_raw(**_kwargs):
        yield {
            "type": "evidence",
            "sql": "SELECT span_configuration FROM span_sets WHERE data_year = 2025",
            "query_language": "sqlite",
            "columns": ["span_configuration"],
            "rows": [["M01"], ["A01"]],
            "count": 2,
            "truncated": False,
            "scope": {"workspace_id": WORKSPACE, "year": 2025},
            "catalog_version": "raw-test",
            "schema_generation": 5,
            "data_generation": 9,
            "warnings": [],
        }
        yield {"type": "token", "content": "- M01\n- A01"}

    monkeypatch.setattr(http_service, "execute_semantic_plan_query", reject_plan)
    monkeypatch.setattr(http_service, "execute_semantic_query", reject_expert_sql)
    monkeypatch.setattr(http_service, "_stream_raw_bridge_query", guarded_raw)

    events = list(
        http_service.stream_bridgette_semantic_query(
            database_path="isolated.sqlite3",
            workspace_id=WORKSPACE,
            request=Request(question="List latest span configurations with a concrete deck"),
        )
    )

    assert [event["type"] for event in events] == ["evidence", "token"]
    assert events[0]["answer_mode"] == "guarded_raw_sql_fallback"
    assert events[0]["answer_status"] == "provisional"
    assert events[0]["query_language"] == "guarded_raw_sql"
    assert events[0]["fallback"]["used"] is True
    assert "plan_rejected" in events[0]["fallback"]["reason"]
    assert any("guarded raw-schema SQL" in warning for warning in events[0]["warnings"])


def test_non_planning_abstention_becomes_actionable_without_releasing_data(monkeypatch):
    monkeypatch.setattr(
        http_service,
        "get_workspace_state",
        lambda _database, _workspace: SimpleNamespace(
            schema_generation=5,
            data_generation=9,
        ),
    )

    def deny_execution(**_kwargs):
        raise SemanticQueryAbstained(
            "The query was denied by the read-only data boundary.",
            request_id=REQUEST_ID,
            attempts=1,
            details={"failure_code": "query_denied"},
        )

    monkeypatch.setattr(http_service, "execute_semantic_plan_query", deny_execution)

    events = list(
        http_service.stream_bridgette_semantic_query(
            database_path="isolated.sqlite3",
            workspace_id=WORKSPACE,
            request=Request(),
        )
    )

    assert len(events) == 1
    assert events[0]["type"] == "clarification"
    assert events[0]["code"] == "query_could_not_be_completed"
    assert len(events[0]["options"]) == 3
    assert "query_denied" not in repr(events[0])
    assert events[0]["warnings"] == [
        "No partial or unverified result was shown."
    ]


def test_execution_failure_becomes_actionable_without_raw_error(monkeypatch):
    monkeypatch.setattr(
        http_service,
        "get_workspace_state",
        lambda _database, _workspace: SimpleNamespace(
            schema_generation=5,
            data_generation=9,
        ),
    )

    def fail_execution(**_kwargs):
        raise SemanticQueryExecutionFailed(
            "The bounded query could not be completed.",
            request_id=REQUEST_ID,
            attempts=1,
            details={"failure_code": "query_execution_error"},
        )

    monkeypatch.setattr(http_service, "execute_semantic_plan_query", fail_execution)
    event = list(
        http_service.stream_bridgette_semantic_query(
            database_path="isolated.sqlite3",
            workspace_id=WORKSPACE,
            request=Request(),
        )
    )[0]

    assert event["type"] == "clarification"
    assert event["code"] == "query_could_not_be_completed"
    assert "couldn't safely" not in event["message"]
    assert "database query couldn't be completed" in event["message"].casefold()
    assert event["options"][0]["suggested_question"] != Request.question
    assert "distinct bridges" in event["options"][0]["suggested_question"]


def test_provider_unavailability_is_named_instead_of_called_a_safety_failure(monkeypatch):
    class APIConnectionError(RuntimeError):
        pass

    monkeypatch.setenv("BRIDGETTE_QUERY_ENGINE", "agentic")
    monkeypatch.setattr(
        http_service,
        "_stream_agentic_bridge_query",
        lambda **_kwargs: (_ for _ in ()).throw(APIConnectionError("offline")),
    )

    event = list(
        http_service.stream_bridgette_semantic_query(
            database_path="does-not-exist.sqlite3",
            workspace_id=WORKSPACE,
            request=Request(
                question="List latest span configurations with concrete decks"
            ),
        )
    )[0]

    assert event["code"] == "query_provider_unavailable"
    assert "language-model service was temporarily unavailable" in event["message"]
    assert "couldn't safely" not in event["message"]


@pytest.mark.parametrize(
    ("question", "expected_tags"),
    (
        (
            "How many distinct bridges were built before 1970?\n\n"
            "Additional context: Use inventory year 2025. Use Year Built (B.W.01), "
            "exclude missing values, and return the distinct bridge count.",
            ("BW01",),
        ),
        ("Which 10 bridges have the lowest deck condition rating?", ("BC01",)),
        (
            "Show bridges with maximum highway vertical clearance below 14 feet.",
            ("BH12",),
        ),
        ("What is the total deck square footage in the latest inventory?", ("BG16",)),
        ("What is the total reported bridge length for inventory year 2025?", ("BG02",)),
        (
            "List 10 distinct bridge numbers.\n\nAdditional context: exclude missing "
            "values and return one row per bridge.",
            ("BID01",),
        ),
        ("Which bridges have known latitude and longitude?", ("BL05", "BL06")),
        (
            "List owner and maintenance responsibility codes for every bridge.",
            ("BCL02", "BCL01"),
        ),
    ),
)
def test_recovery_field_ranking_ignores_generic_boilerplate_and_nearby_fields(
    question, expected_tags
):
    assert tuple(
        field.tag for field, _codes in http_service._rank_recovery_fields(question)
    ) == expected_tags


def test_audit_failure_becomes_actionable_without_raw_error(monkeypatch):
    monkeypatch.setattr(
        http_service,
        "get_workspace_state",
        lambda _database, _workspace: SimpleNamespace(
            schema_generation=5,
            data_generation=9,
        ),
    )

    def fail_audit(**_kwargs):
        raise SemanticQueryAuditFailed(
            "Audit storage was unavailable.",
            request_id=REQUEST_ID,
            attempts=0,
            details={"failure_code": "private_driver_detail"},
        )

    monkeypatch.setattr(http_service, "execute_semantic_plan_query", fail_audit)
    event = list(
        http_service.stream_bridgette_semantic_query(
            database_path="isolated.sqlite3",
            workspace_id=WORKSPACE,
            request=Request(question="How many bridges are in each county?"),
        )
    )[0]

    assert event["type"] == "clarification"
    assert event["code"] == "query_could_not_be_completed"
    assert "county" in event["message"]
    assert "private_driver_detail" not in repr(event)
    assert len(event["options"]) == 3


def test_recovery_prompt_uses_query_specific_grounded_inventory_options(tmp_path):
    database = tmp_path / "inventory.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE bridges (
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            data_year INTEGER,
            county_code INTEGER
        );
        CREATE TABLE counties (
            state_code INTEGER NOT NULL,
            county_code INTEGER NOT NULL,
            county_name TEXT NOT NULL
        );
        CREATE TABLE published_snapshots (
            workspace_id TEXT NOT NULL,
            state_code INTEGER NOT NULL,
            data_year INTEGER NOT NULL
        );
        INSERT INTO counties VALUES (34, 3, 'Bergen');
        INSERT INTO counties VALUES (34, 23, 'Middlesex');
        INSERT INTO bridges VALUES (34, 'A', 2024, 3);
        INSERT INTO bridges VALUES (34, 'A', 2025, 3);
        INSERT INTO bridges VALUES (34, 'B', 2025, 3);
        INSERT INTO bridges VALUES (34, 'C', 2025, 23);
        """
    )
    connection.commit()
    connection.close()

    event = http_service._query_recovery_event(
        Request(question="How many bridges are in each county?"),
        failure_kind="generation",
        database_path=database,
    )

    assert "count about county" in event["message"]
    assert "County Code (BL02)" in event["message"]
    assert "3 authoritative inventory records are available for 2025" in event["message"]
    assert [option["label"] for option in event["options"]] == [
        "Count bridges in every county (2025)",
        "Show the 10 largest county bridge counts (2025)",
        "List 10 bridges in Bergen County (2025)",
    ]
    assert "one count per county" in event["options"][0]["suggested_question"]
    assert "ordered from largest to smallest" in event["options"][1]["suggested_question"]
    assert "Bergen County" in event["options"][2]["suggested_question"]


def test_percentage_recovery_preserves_shape_and_replaces_generated_context():
    question = (
        "What percentage of NHS deck area is Poor under the federal TPM definition?"
        "\n\nAdditional context: Use inventory year 2025. Return only the requested count."
    )
    request = Request(
        question=question,
        scope=QueryScope(
            workspace_id=WORKSPACE,
            state_code=34,
            time_mode="exact",
            data_year=2025,
        ),
    )

    event = http_service._query_recovery_event(
        request,
        failure_kind="generation",
        database_path="does-not-exist.sqlite3",
    )

    assert "percentage about deck" in event["message"]
    assert len({option["suggested_question"] for option in event["options"]}) == 3
    assert event["options"][0]["label"].startswith("Calculate the deck percentage")
    assert "percentage" in event["options"][0]["suggested_question"]
    assert "Count distinct bridges by" in event["options"][1]["suggested_question"]
    assert "List 10 bridge records" in event["options"][2]["suggested_question"]


def test_disabled_federal_tpm_request_gets_data_informed_executable_alternatives(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "inventory.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE bridges (
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            data_year INTEGER,
            county_code INTEGER,
            calculated_deck_area REAL,
            nhs_designation TEXT
        );
        INSERT INTO bridges VALUES (34, 'A', 2025, 3, NULL, NULL);
        INSERT INTO bridges VALUES (34, 'B', 2025, 3, NULL, NULL);
        CREATE VIEW v_published_bridge_snapshots AS SELECT * FROM bridges;
        CREATE TABLE features (
            state_code INTEGER NOT NULL,
            data_year INTEGER,
            feature_type TEXT,
            nhs_designation TEXT
        );
        INSERT INTO features VALUES (34, 2025, 'H01', 'Y');
        INSERT INTO features VALUES (34, 2025, 'H01', NULL);
        CREATE VIEW v_carried_highway_features AS
            SELECT * FROM features WHERE feature_type = 'H01';
        """
    )
    connection.commit()
    connection.close()
    monkeypatch.setenv("BRIDGEGUARD_QUERY_MODE", "plan_required")
    monkeypatch.setattr(
        http_service,
        "_provider_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("provider must not be called for a reviewed disabled metric")
        ),
    )
    request = Request(
        question=(
            "What percentage of NHS bridge deck area is in Poor condition under "
            "the federal TPM definition?"
        ),
        scope=QueryScope(
            workspace_id=WORKSPACE,
            state_code=34,
            time_mode="exact",
            data_year=2025,
        ),
    )

    events = list(
        http_service.stream_bridgette_semantic_query(
            database_path=database,
            workspace_id=WORKSPACE,
            request=request,
        )
    )

    assert len(events) == 1
    event = events[0]
    assert event["type"] == "clarification"
    assert event["code"] == "query_needs_more_context"
    assert "official federal TPM Poor NHS deck-area percentage" in event["message"]
    assert "0 of 2 bridge snapshots have a populated BG16" in event["message"]
    assert "Feature-level BH03 is populated on 1 of 2" in event["message"]
    assert "No bridge-count percentage or other proxy was substituted" in event["message"]
    assert [option["id"] for option in event["options"]] == [
        "operational_condition_percentage",
        "operational_condition_count",
        "operational_condition_counts",
    ]
    assert all(
        request.question not in option["suggested_question"]
        for option in event["options"]
    )


def test_recovery_customizes_fields_codes_and_buttons_for_span_deck_query(tmp_path):
    database = tmp_path / "inventory.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE bridges (
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            data_year INTEGER,
            county_code INTEGER
        );
        INSERT INTO bridges VALUES (34, 'A', 2025, 3);
        """
    )
    connection.commit()
    connection.close()
    question = (
        "List latest span configurations with reinforced concrete cast-in-place deck"
    )

    event = http_service._query_recovery_event(
        Request(question=question),
        failure_kind="generation",
        database_path=database,
    )

    assert "list about span configuration" in event["message"]
    assert "Span Configuration Designation (BSP01)" in event["message"]
    assert "Deck Material and Type (BSP09)" in event["message"]
    assert [option["label"] for option in event["options"]] == [
        "List matching span configurations (2025)",
        "Count bridges by matching span configuration (2025)",
        "Show 10 matching bridge spans (2025)",
    ]
    exact = event["options"][0]["suggested_question"]
    assert "code is C01 or CR-T" in exact
    assert "Use inventory year 2025" in exact
    assert "one row per configuration" in exact
    assert "no bridge-level columns" in exact
    assert "distinct bridge count" in event["options"][1]["suggested_question"]
    assert "span-set number" in event["options"][2]["suggested_question"]


def test_raw_schema_provider_failure_uses_customized_recovery(monkeypatch):
    monkeypatch.setenv("BRIDGEGUARD_QUERY_MODE", "raw_schema")
    monkeypatch.setattr(
        http_service,
        "_stream_raw_bridge_query",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
    )
    question = (
        "List latest span configurations with reinforced concrete cast-in-place deck"
    )

    events = list(
        http_service.stream_bridgette_semantic_query(
            database_path="does-not-exist.sqlite3",
            workspace_id=WORKSPACE,
            request=Request(question=question),
        )
    )

    assert len(events) == 1
    assert events[0]["code"] == "query_generation_unavailable"
    assert "Span Configuration Designation (BSP01)" in events[0]["message"]
    assert [option["label"] for option in events[0]["options"]] == [
        "List matching span configurations (latest)",
        "Count bridges by matching span configuration (latest)",
        "Show 10 matching bridge spans (latest)",
    ]


def _agentic_inventory_database(tmp_path):
    database = tmp_path / "agentic-inventory.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE bridges (
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            data_year INTEGER NOT NULL,
            county_code INTEGER,
            owner TEXT,
            maintenance_responsibility TEXT,
            calculated_deck_area REAL,
            operational_condition TEXT
        );
        CREATE TABLE ref_counties (
            state_code INTEGER NOT NULL,
            county_code INTEGER NOT NULL,
            county_name TEXT NOT NULL
        );
        INSERT INTO ref_counties VALUES (34, 1, 'Alpha'), (34, 3, 'Beta');
        INSERT INTO bridges VALUES
            (34, 'A', 2025, 1, 'L01', 'S01', 100.0, 'Poor'),
            (34, 'B', 2025, 1, 'L01', 'S01', 300.0, 'Good'),
            (34, 'C', 2025, 3, 'L01', 'S01', 600.0, 'Poor'),
            (34, 'D', 2025, 3, 'S01', 'S01', 900.0, 'Good'),
            (34, 'A', 2024, 1, 'L01', 'S01', 90.0, 'Poor');
        CREATE VIEW v_bridge_condition AS
            SELECT state_code, bridge_number, data_year,
                   operational_condition AS condition_class
            FROM bridges;
        """
    )
    connection.commit()
    connection.close()
    return database


def _install_agentic_workspace(monkeypatch):
    monkeypatch.setattr(
        http_service,
        "get_workspace_state",
        lambda _database, _workspace: SimpleNamespace(
            schema_generation=5,
            data_generation=9,
        ),
    )


def test_agentic_county_query_returns_count_and_average_area(tmp_path, monkeypatch):
    database = _agentic_inventory_database(tmp_path)
    _install_agentic_workspace(monkeypatch)
    question = (
        "For each New Jersey county, show the number of county-owned bridges and "
        "their average deck area, ranked from largest average deck area to smallest"
    )
    sql = (
        "SELECT c.county_name, COUNT(DISTINCT b.bridge_number) AS bridge_count, "
        "AVG(b.calculated_deck_area) AS average_deck_area FROM bridges AS b "
        "JOIN ref_counties AS c ON c.state_code=b.state_code AND "
        "c.county_code=b.county_code WHERE b.state_code=34 AND "
        "b.data_year=2025 AND b.owner='L01' GROUP BY c.county_name "
        "ORDER BY average_deck_area DESC"
    )

    def provider(_prompt, _token, **kwargs):
        name = kwargs["response_format"]["json_schema"]["name"]
        if name == "bridgette_agentic_query":
            return json.dumps(
                {
                    "interpretation_summary": "Group county-owned bridges by county.",
                    "requirements": [
                        {"id": "county", "description": "county name", "kind": "grouping", "output_aliases": ["county_name"]},
                        {"id": "count", "description": "distinct bridge count", "kind": "output", "output_aliases": ["bridge_count"]},
                        {"id": "area", "description": "average deck area", "kind": "output", "output_aliases": ["average_deck_area"]},
                        {"id": "owner", "description": "county-owned", "kind": "filter", "output_aliases": []},
                        {"id": "rank", "description": "largest average first", "kind": "ordering", "output_aliases": []},
                    ],
                    "sql": sql,
                }
            )
        return json.dumps(
            {
                "answer_markdown": "Beta ranks first, followed by Alpha.",
                "complete": True,
                "missing_requirements": [],
            }
        )

    monkeypatch.setattr(http_service, "_provider_text", provider)
    events = list(
        http_service._stream_raw_bridge_query(
            database_path=database,
            request=Request(question=question),
            inventory_context={"latest_loaded_year": 2025},
            agentic=True,
        )
    )

    assert events[0]["answer_mode"] == "agentic_sql"
    assert events[0]["answer_status"] == "verified"
    assert events[0]["columns"] == [
        "county_name", "bridge_count", "average_deck_area"
    ]
    assert events[0]["rows"] == [["Beta", 1, 600.0], ["Alpha", 2, 200.0]]


def test_agentic_yearly_query_returns_every_requested_measure(tmp_path, monkeypatch):
    database = _agentic_inventory_database(tmp_path)
    _install_agentic_workspace(monkeypatch)
    question = (
        "For each inventory year from 2024 through 2025, show the number of "
        "NJDOT-maintained bridges, their total deck area, and the percentage "
        "classified as operationally Poor."
    )
    sql = (
        "SELECT b.data_year, COUNT(DISTINCT b.bridge_number) AS bridge_count, "
        "SUM(b.calculated_deck_area) AS total_deck_area, "
        "100.0 * COUNT(DISTINCT CASE WHEN c.condition_class='Poor' THEN "
        "b.bridge_number END) / NULLIF(COUNT(DISTINCT b.bridge_number),0) "
        "AS poor_percentage FROM bridges AS b JOIN v_bridge_condition AS c ON "
        "c.state_code=b.state_code AND c.bridge_number=b.bridge_number AND "
        "c.data_year=b.data_year WHERE b.state_code=34 AND "
        "b.data_year BETWEEN 2024 AND 2025 AND "
        "b.maintenance_responsibility='S01' GROUP BY b.data_year "
        "ORDER BY b.data_year"
    )

    def provider(_prompt, _token, **kwargs):
        name = kwargs["response_format"]["json_schema"]["name"]
        if name == "bridgette_agentic_query":
            return json.dumps(
                {
                    "interpretation_summary": "One row per inventory year.",
                    "requirements": [
                        {"id": "year", "description": "inventory year", "kind": "grouping", "output_aliases": ["data_year"]},
                        {"id": "count", "description": "bridge count", "kind": "output", "output_aliases": ["bridge_count"]},
                        {"id": "area", "description": "total deck area", "kind": "output", "output_aliases": ["total_deck_area"]},
                        {"id": "poor", "description": "Poor percentage", "kind": "output", "output_aliases": ["poor_percentage"]},
                        {"id": "maintainer", "description": "NJDOT maintained", "kind": "filter", "output_aliases": []},
                    ],
                    "sql": sql,
                }
            )
        return json.dumps(
            {
                "answer_markdown": "The result includes all requested yearly measures.",
                "complete": True,
                "missing_requirements": [],
            }
        )

    monkeypatch.setattr(http_service, "_provider_text", provider)
    events = list(
        http_service._stream_raw_bridge_query(
            database_path=database,
            request=Request(question=question),
            inventory_context={"latest_loaded_year": 2025},
            agentic=True,
        )
    )

    assert events[0]["answer_status"] == "verified"
    assert events[0]["columns"] == [
        "data_year", "bridge_count", "total_deck_area", "poor_percentage"
    ]
    assert len(events[0]["rows"]) == 2


def test_agentic_safe_incomplete_query_is_shown_as_provisional(tmp_path, monkeypatch):
    database = _agentic_inventory_database(tmp_path)
    _install_agentic_workspace(monkeypatch)
    responses = iter(
        [
            json.dumps(
                {
                    "interpretation_summary": "Count and total area.",
                    "requirements": [
                        {"id": "count", "description": "bridge count", "kind": "output", "output_aliases": ["bridge_count"]},
                        {"id": "area", "description": "total deck area", "kind": "output", "output_aliases": ["total_deck_area"]},
                    ],
                    "sql": "SELECT COUNT(*) AS bridge_count FROM bridges WHERE state_code=34 AND data_year=2025",
                }
            ),
            json.dumps(
                {
                    "answer_markdown": "The result contains the bridge count only.",
                    "complete": False,
                    "missing_requirements": ["total deck area"],
                }
            ),
        ]
    )
    monkeypatch.setattr(
        http_service,
        "_provider_text",
        lambda *_args, **_kwargs: next(responses),
    )

    events = list(
        http_service._stream_raw_bridge_query(
            database_path=database,
            request=Request(question="How many bridges are there and what is their total deck area?"),
            inventory_context={"latest_loaded_year": 2025},
            agentic=True,
        )
    )

    assert [event["type"] for event in events] == ["evidence", "token"]
    assert events[0]["answer_status"] == "provisional"
    assert events[0]["rows"] == [[4]]
    assert events[1]["content"] == "The result contains the bridge count only."
    assert "total deck area" in events[0]["agentic_review"]["missing_requirements"]


def test_agentic_model_routes_paraphrased_dashboard_question_to_kpi(
    tmp_path, monkeypatch
):
    database = _agentic_inventory_database(tmp_path)
    _install_agentic_workspace(monkeypatch)
    captured = {}

    def kpi_route(**kwargs):
        captured["intent"] = kwargs["interpreted_intent"].as_dict()
        return iter(
            [
                {
                    "type": "evidence",
                    "answer_mode": "verified_kpi",
                    "rows": [[548134.0]],
                },
                {"type": "token", "content": "548,134.0 ft²"},
            ]
        )

    monkeypatch.setattr(http_service, "stream_bridgette_kpi_query", kpi_route)
    monkeypatch.setattr(
        http_service,
        "_provider_text",
        lambda *_args, **_kwargs: json.dumps(
            {
                "route": "kpi",
                "kpi_intent": {
                    "family": "age_condition",
                    "measure": "deck_area",
                    "population": "statewide",
                    "condition": "Good",
                    "component": "",
                    "age_bin": "50-54",
                    "county": "",
                    "scenario": "",
                    "projection_year": None,
                    "inventory_year": None,
                    "program_measure_id": "",
                },
                "interpretation_summary": "Good bridge deck area for one age band.",
                "requirements": [
                    {
                        "id": "area",
                        "description": "deck area",
                        "kind": "output",
                        "output_aliases": [],
                    },
                    {
                        "id": "age",
                        "description": "50 through 54 years",
                        "kind": "filter",
                        "output_aliases": [],
                    },
                    {
                        "id": "condition",
                        "description": "Good condition",
                        "kind": "filter",
                        "output_aliases": [],
                    },
                ],
                "sql": "",
            }
        ),
    )

    events = list(
        http_service._stream_raw_bridge_query(
            database_path=database,
            request=Request(
                question=(
                    "How many square feet of deck belong to Good-rated structures "
                    "whose ages lie in the fifty-to-fifty-four-year band?"
                )
            ),
            inventory_context={"latest_loaded_year": 2025},
            agentic=True,
        )
    )

    assert captured["intent"] == {
        "family": "age_condition",
        "measure": "deck_area",
        "population": "statewide",
        "condition": "Good",
        "age_bin": "50-54",
    }
    assert events[0]["answer_mode"] == "verified_kpi"


def test_agentic_semantic_router_selects_kpi_before_sql_generation(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        http_service,
        "_provider_text",
        lambda _prompt, _token, **kwargs: (
            captured.setdefault(
                "schema", kwargs["response_format"]["json_schema"]["name"]
            )
            and json.dumps(
                {
                    "route": "kpi",
                    "kpi_intent": {
                        "family": "age_condition",
                        "measure": "deck_area",
                        "population": "statewide",
                        "condition": "Good",
                        "component": "",
                        "age_bin": "50-54",
                        "county": "",
                        "scenario": "",
                        "projection_year": None,
                        "inventory_year": None,
                        "program_measure_id": "",
                    },
                    "interpretation_summary": "Governed age-condition deck area.",
                }
            )
        ),
    )

    def kpi_route(**kwargs):
        captured["intent"] = kwargs["interpreted_intent"].as_dict()
        return iter(
            [
                {"type": "evidence", "answer_mode": "verified_kpi"},
                {"type": "token", "content": "548,134.0 ft²"},
            ]
        )

    monkeypatch.setattr(http_service, "stream_bridgette_kpi_query", kpi_route)

    events = list(
        http_service._stream_agentic_bridge_query(
            database_path="not-opened.sqlite3",
            request=Request(
                question=(
                    "How many square feet of deck belong to Good-rated structures "
                    "whose ages lie in the fifty-to-fifty-four-year band?"
                )
            ),
        )
    )

    assert captured["schema"] == "bridgette_semantic_route"
    assert captured["intent"]["age_bin"] == "50-54"
    assert events[0]["answer_mode"] == "verified_kpi"


def test_agentic_semantic_router_locks_general_question_to_database(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        http_service,
        "_provider_text",
        lambda *_args, **_kwargs: json.dumps(
            {
                "route": "database",
                "kpi_intent": {
                    "family": "none",
                    "measure": "",
                    "population": "",
                    "condition": "",
                    "component": "",
                    "age_bin": "",
                    "county": "",
                    "scenario": "",
                    "projection_year": None,
                    "inventory_year": None,
                    "program_measure_id": "",
                },
                "interpretation_summary": "Owner-filtered deck area is a database query.",
            }
        ),
    )

    def raw_route(**kwargs):
        captured.update(kwargs)
        return iter([{"type": "token", "content": "database answer"}])

    monkeypatch.setattr(http_service, "_stream_raw_bridge_query", raw_route)

    events = list(
        http_service._stream_agentic_bridge_query(
            database_path="unused.sqlite3",
            request=Request(
                question="Total deck area for bridges owned by the state DOT"
            ),
        )
    )

    assert captured["agentic"] is True
    assert captured["semantic_route_locked"] == "database"
    assert events == [{"type": "token", "content": "database answer"}]


def test_default_engine_uses_model_router_not_legacy_regex(monkeypatch):
    monkeypatch.delenv("BRIDGETTE_QUERY_ENGINE", raising=False)
    monkeypatch.setattr(
        http_service,
        "_stream_agentic_bridge_query",
        lambda **_kwargs: iter(
            [
                {"type": "evidence", "answer_mode": "agentic_sql"},
                {"type": "token", "content": "answer"},
            ]
        ),
    )
    monkeypatch.setattr(
        http_service,
        "stream_bridgette_kpi_query",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy regex KPI router must not run")
        ),
    )

    events = list(
        http_service.stream_bridgette_semantic_query(
            database_path="not-opened.sqlite3",
            workspace_id=WORKSPACE,
            request=Request(question="Use whatever phrasing I choose."),
        )
    )

    assert [event["type"] for event in events] == ["evidence", "token"]


def test_agentic_context_exposes_all_codes_and_aliases(tmp_path):
    database = _agentic_inventory_database(tmp_path)

    context = json.loads(
        http_service._raw_bridge_schema_context(
            database,
            "How many bridges are there?",
            full_context=True,
        )
    )

    assert len(context["snbi_field_dictionary"]) == 154
    assert "BCL01" in context["all_limited_value_fields"]
    assert "BAP01" in context["all_limited_value_fields"]
    assert len(context["all_reviewed_semantic_aliases"]) == len(
        http_service.DEFAULT_SEMANTIC_ALIASES.records
    )


def test_agentic_engine_bypasses_legacy_semantic_gates(monkeypatch):
    monkeypatch.setenv("BRIDGETTE_QUERY_ENGINE", "agentic")
    monkeypatch.setattr(
        http_service,
        "_stream_agentic_bridge_query",
        lambda **_kwargs: iter(
            [
                {"type": "evidence", "answer_mode": "agentic_sql"},
                {"type": "token", "content": "answer"},
            ]
        ),
    )
    monkeypatch.setattr(
        http_service,
        "_known_capability_gap_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy capability gate must not run")
        ),
    )

    events = list(
        http_service.stream_bridgette_semantic_query(
            database_path="not-opened.sqlite3",
            workspace_id=WORKSPACE,
            request=Request(question="Ask the complete SNBI schema"),
        )
    )

    assert [event["type"] for event in events] == ["evidence", "token"]


def test_data_availability_follow_up_is_answered_without_provider(tmp_path, monkeypatch):
    database = tmp_path / "inventory.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE bridges (
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            data_year INTEGER,
            county_code INTEGER
        );
        CREATE TABLE published_snapshots (
            workspace_id TEXT NOT NULL,
            state_code INTEGER NOT NULL,
            data_year INTEGER NOT NULL
        );
        INSERT INTO bridges VALUES (34, 'A', 2025, 3);
        """
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(
        http_service,
        "_provider_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("provider must not be called")
        ),
    )

    events = list(
        http_service.stream_bridgette_semantic_query(
            database_path=database,
            workspace_id=WORKSPACE,
            request=Request(question="What bridge data is currently loaded and published?"),
        )
    )

    assert len(events) == 1
    assert events[0]["code"] == "inventory_availability"
    assert "1 loaded New Jersey bridge record" in events[0]["message"]
    assert "latest loaded inventory year (2025) by default" in events[0]["message"]


def test_inventory_time_contract_defaults_to_latest_and_respects_year_intent():
    context = {"latest_loaded_year": 2025}

    latest = http_service._raw_inventory_time_contract(
        Request(question="How many bridges were built before 1970?"),
        context,
    )
    exact = http_service._raw_inventory_time_contract(
        Request(question="How many bridges are in each county in 2024?"),
        context,
    )
    trend = http_service._raw_inventory_time_contract(
        Request(question="Show bridge counts by inventory year from 2020 through 2025."),
        context,
    )
    oldest = http_service._raw_inventory_time_contract(
        Request(question="List the 20 oldest latest-published bridges by year built."),
        context,
    )

    assert latest["mode"] == "latest_loaded_inventory"
    assert latest["data_year"] == 2025
    assert exact["mode"] == "exact_inventory_year"
    assert exact["data_year"] == 2024
    assert trend["mode"] == "group_by_inventory_year"
    assert trend["start_year"] == 2020
    assert trend["end_year"] == 2025
    assert oldest["mode"] == "latest_loaded_inventory"
    assert oldest["data_year"] == 2025
    assert http_service._raw_sql_respects_time_contract(
        "SELECT COUNT(*) FROM bridges b WHERE b.data_year = 2025",
        latest,
    )
    assert not http_service._raw_sql_respects_time_contract(
        "SELECT COUNT(*) FROM bridges",
        latest,
    )
    assert http_service._raw_sql_respects_time_contract(
        "SELECT data_year, COUNT(*) FROM bridges "
        "WHERE data_year BETWEEN 2020 AND 2025 GROUP BY data_year",
        trend,
    )
    assert not http_service._raw_sql_respects_time_contract(
        "SELECT data_year, COUNT(*) FROM bridges GROUP BY data_year",
        trend,
    )
    assert not http_service._raw_sql_respects_time_contract(
        "SELECT DISTINCT s.span_configuration_designation "
        "FROM bridges b JOIN span_sets s "
        "ON s.state_code=b.state_code AND s.bridge_number=b.bridge_number "
        "WHERE b.data_year=2025",
        latest,
    )
    assert http_service._raw_sql_respects_time_contract(
        "SELECT DISTINCT s.span_configuration_designation "
        "FROM bridges b JOIN span_sets s "
        "ON s.state_code=b.state_code AND s.bridge_number=b.bridge_number "
        "AND s.data_year=b.data_year WHERE b.data_year=2025",
        latest,
    )
    assert not http_service._raw_sql_respects_time_contract(
        "SELECT MAX(p.posting_status_change_date) FROM bridges b "
        "JOIN posting_statuses p ON p.state_code=b.state_code "
        "AND p.bridge_number=b.bridge_number WHERE b.data_year=2025",
        latest,
    )
    result_contract = http_service._raw_result_contract(
        "List latest span configurations with reinforced concrete cast-in-place deck"
    )
    assert "exactly one column named span_configuration_designation" in result_contract
    assert "C01, CR-T" in result_contract


def test_reviewed_raw_clearance_posting_query_uses_server_scope():
    sql = http_service._reviewed_raw_bridge_sql(
        "List distinct bridges with minimum highway vertical clearance below 13.5 "
        "feet and at least one load-posting status; show bridge number, bridge name, "
        "minimum clearance, latest posting status-change date, and maximum AADT.",
        contract={"mode": "latest_loaded_inventory", "data_year": 2025},
        state_code=34,
    )

    assert sql is not None
    assert "b.state_code = 34" in sql
    assert "b.data_year = 2025" in sql
    assert "f.highway_min_vertical_clearance < 13.5" in sql
    assert "b.data_year = p.data_year" in sql
    assert http_service._raw_sql_respects_time_contract(
        sql,
        {"mode": "latest_loaded_inventory", "data_year": 2025},
    )


def test_reviewed_raw_missing_county_query_uses_server_scope():
    sql = http_service._reviewed_raw_bridge_sql(
        "Which latest published bridge records are missing a county code?",
        contract={"mode": "latest_loaded_inventory", "data_year": 2025},
        state_code=34,
    )

    assert sql is not None
    assert "b.state_code = 34" in sql
    assert "b.data_year = 2025" in sql
    assert "b.county_code IS NULL" in sql
    assert http_service._raw_sql_respects_time_contract(
        sql,
        {"mode": "latest_loaded_inventory", "data_year": 2025},
    )


def test_loaded_inventory_answers_with_server_resolved_latest_year(tmp_path, monkeypatch):
    database = tmp_path / "inventory.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE bridges (
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            data_year INTEGER,
            county_code INTEGER
        );
        CREATE TABLE published_snapshots (
            workspace_id TEXT NOT NULL,
            state_code INTEGER NOT NULL,
            data_year INTEGER NOT NULL
        );
        INSERT INTO bridges VALUES (34, 'A', 2025, 3);
        """
    )
    connection.commit()
    connection.close()
    _install_query(monkeypatch, _evidence(["bridge_count"], [[1]]))
    prompts = []

    def provider(prompt, *_args, **_kwargs):
        prompts.append(prompt)
        return _PLAN_JSON

    monkeypatch.setattr(
        http_service,
        "_provider_text",
        provider,
    )
    events = list(
        http_service.stream_bridgette_semantic_query(
            database_path=database,
            workspace_id=WORKSPACE,
            request=Request(question="How many bridges are in each county?"),
        )
    )

    assert [event["type"] for event in events] == ["evidence", "token"]
    assert events[0]["scope"]["time_mode"] == "latest"
    assert events[0]["interpreted_scope"]["snapshot_status"] == "loaded_inventory"
    assert events[0]["source_snapshot_status"] == "loaded_inventory"
    assert events[0]["source_status"]["exact_year_identity"] == [
        "state_code", "bridge_number", "data_year"
    ]
    assert events[1]["content"].startswith("**1 bridges**")
    assert len(prompts) == 1


def test_known_unsupported_fact_abstains_before_database_and_names_no_proxy(monkeypatch):
    monkeypatch.setattr(
        http_service,
        "get_workspace_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("database must not be touched")
        ),
    )
    event = list(
        http_service.stream_bridgette_semantic_query(
            database_path="does-not-exist.sqlite3",
            workspace_id=WORKSPACE,
            request=Request(question="What was this bridge's original paint color?"),
        )
    )[0]
    assert event["type"] == "abstention"
    assert event["code"] == "unsupported_fact"
    assert event["nearest_fields_not_substituted"] == [
        "span_material",
        "span_protective_system",
    ]
    assert len(event["options"]) == 2
    assert all(option["suggested_question"] for option in event["options"])


@pytest.mark.parametrize(
    ("question", "required_terms"),
    [
        (
            "How many current bridges use the proposed C06 ultra-high-performance concrete deck code, and should C06 count as concrete?",
            ("deck_material_and_type",),
        ),
        (
            "How many distinct current bridges have ever had major rehabilitation on any component?",
            ("v_bridge_works", "bridge_number", "work_performed"),
        ),
        (
            "What is the average deck condition rating by county in the latest published inventory?",
            ("v_bridge_condition", "bc01", "v_published_bridge_snapshots.county_code"),
        ),
        (
            "What is the average superstructure condition rating for each inventory year?",
            ("v_bridge_condition", "data_year", "bc02"),
        ),
        (
            "How many distinct current bridges are weight-posted?",
            ("v_bridge_posting_statuses", "bridge_number", "load_posting_status"),
        ),
        (
            "How many distinct current bridges have any element quantity in condition state four?",
            ("v_bridge_elements", "bridge_number", "element_quantity_condition_state_four"),
        ),
        (
            "Which bridges have an inspection due date before today?",
            ("inspection_due_date",),
        ),
        (
            "How many current bridges have reinforced-concrete substructures?",
            ("substructure_material",),
        ),
        (
            "Which current bridges have a controlling legal load rating factor below 1.0?",
            ("controlling_legal_load_rating_factor",),
        ),
    ],
)
def test_known_capability_gaps_are_precise_and_do_not_call_a_provider(
    monkeypatch,
    question,
    required_terms,
):
    monkeypatch.setattr(
        http_service,
        "_provider_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("known capability gaps must not call a provider")
        ),
    )
    event = http_service._known_capability_gap_event(Request(question=question))

    assert event is not None
    assert event["type"] == "clarification"
    assert event["code"] == "query_needs_more_context"
    assert all(term in event["message"] for term in required_terms)
    assert len(event["options"]) == 2
