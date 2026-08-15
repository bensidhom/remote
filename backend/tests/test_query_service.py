from __future__ import annotations

import json
import sqlite3
import time

import pytest

from app.routers.services import database as legacy_database
from app.routers.services.database import ensure_schema
from app.routers.services.db_schema import LATEST_SCHEMA_VERSION
from app.services.query.audit import list_query_audit_events
from app.services.query.registry import CancelOutcome, RequestRegistry, new_request_id
from app.services.query.semantic import build_semantic_catalog
from app.services.query import service as query_service_module
from app.services.query.service import (
    MAX_PLAN_CONTEXT_BYTES,
    MAX_QUERY_CONTEXT_BYTES,
    PLAN_PROMPT_VERSION,
    PlanGenerationRequest,
    SemanticQueryAbstained,
    SemanticQueryExecutionFailed,
    SemanticQueryGenerationFailed,
    SemanticQueryRequest,
    ServerQueryIdentity,
    build_plan_context,
    build_query_context,
    build_sql_catalog,
    cancel_semantic_query,
    execute_semantic_query,
    execute_semantic_plan_query,
)
from app.services.query.snbi_dictionary import DEFAULT_SNBI_DICTIONARY
from app.services.query.sql_ast import QueryScope


WORKSPACE = "query-service-workspace"
OTHER_WORKSPACE = "other-query-workspace"
NOW = "2026-08-13T12:00:00Z"


def _insert_workspace(connection: sqlite3.Connection, workspace_id: str) -> None:
    connection.execute(
        "INSERT INTO workspaces (id,slug,display_name,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (workspace_id, workspace_id, workspace_id, "active", NOW, NOW),
    )
    connection.execute(
        "INSERT INTO workspace_state "
        "(workspace_id,data_generation,schema_generation,updated_at) VALUES (?,?,?,?)",
        (workspace_id, 9, LATEST_SCHEMA_VERSION, NOW),
    )


def _publish(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    bridge_number: str,
    year: int,
    condition: float = 7,
) -> None:
    revision_id = f"revision-{workspace_id}-{bridge_number}-{year}"
    connection.execute(
        "INSERT INTO bridges "
        "(state_code,bridge_number,bridge_name,county_code,data_year,year_built,"
        "deck_condition_rating,superstructure_condition_rating,"
        "substructure_condition_rating) VALUES (34,?,?,?,?,?,?,?,?)",
        (
            bridge_number,
            f"Bridge {bridge_number}",
            1,
            year,
            year - 50,
            condition,
            condition,
            condition,
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
            workspace_id,
            34,
            bridge_number,
            year,
            (revision_id.encode().hex() + "0" * 64)[:64],
            "a" * 64,
            NOW,
        ),
    )
    connection.execute(
        "INSERT INTO published_snapshots "
        "(id,workspace_id,state_code,bridge_number,data_year,snapshot_revision_id,"
        "published_by,published_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            f"publication-{workspace_id}-{bridge_number}-{year}",
            workspace_id,
            34,
            bridge_number,
            year,
            revision_id,
            "tester",
            NOW,
        ),
    )


@pytest.fixture
def query_database(test_workspace):
    path = test_workspace / "query-service.sqlite3"
    ensure_schema(str(path))
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_workspace(connection, WORKSPACE)
        _insert_workspace(connection, OTHER_WORKSPACE)
        _publish(connection, workspace_id=WORKSPACE, bridge_number="A", year=2024)
        _publish(connection, workspace_id=WORKSPACE, bridge_number="A", year=2025)
        _publish(connection, workspace_id=WORKSPACE, bridge_number="B", year=2023)
        _publish(
            connection,
            workspace_id=OTHER_WORKSPACE,
            bridge_number="PRIVATE",
            year=2026,
        )
        connection.commit()
    finally:
        connection.close()
    return path


def _identity(workspace_id: str = WORKSPACE) -> ServerQueryIdentity:
    return ServerQueryIdentity(
        actor_id="user-001",
        role="viewer",
        workspace_id=workspace_id,
        session_id="session-001",
        surface="bridgette-full",
    )


def _request(scope: QueryScope) -> SemanticQueryRequest:
    return SemanticQueryRequest(
        request_id=new_request_id(),
        identity=_identity(scope.workspace_id),
        scope=scope,
        question="Which bridges are in this authorized scope?",
        database_identity="isolated-test-database",
        schema_generation=LATEST_SCHEMA_VERSION,
        data_generation=9,
        model_id="test-model",
        max_rows=20,
    )


def test_success_is_server_scoped_audited_and_never_uses_legacy_executor(
    query_database, monkeypatch
):
    def forbidden_legacy(*_args, **_kwargs):
        raise AssertionError("legacy SQL executor must never be called")

    monkeypatch.setattr(legacy_database, "execute_readonly_sql", forbidden_legacy)
    request = _request(
        QueryScope(
            workspace_id=WORKSPACE,
            state_code=34,
            time_mode="exact",
            data_year=2025,
        )
    )
    evidence = execute_semantic_query(
        database_path=query_database,
        request=request,
        generate_sql=lambda _context: (
            "SELECT bridge_number, data_year "
            "FROM v_published_bridge_snapshots ORDER BY bridge_number"
        ),
    )

    assert evidence.rows == (("A", 2025),)
    assert evidence.columns == ("bridge_number", "data_year")
    assert evidence.ast.relations == ("v_published_bridge_snapshots",)
    assert len(evidence.semantic_catalog_sha256) == 64
    assert evidence.snbi_dictionary_sha256 == DEFAULT_SNBI_DICTIONARY.dictionary_hash
    assert len(evidence.compiled_sql_sha256) == 64

    events = list_query_audit_events(query_database, WORKSPACE, request_id=request.request_id)
    assert [event.outcome for event in events] == ["accepted", "completed"]
    assert events[0].catalog_sha256 == evidence.semantic_catalog_sha256
    assert f"catalog-{evidence.semantic_catalog_sha256}" in events[0].prompt_version
    assert f"dictionary-{evidence.snbi_dictionary_sha256}" in events[0].prompt_version
    serialized = repr(events)
    assert request.question not in serialized
    assert "Bridge A" not in serialized


def test_latest_scope_is_per_bridge_not_global_year(query_database):
    request = _request(
        QueryScope(
            workspace_id=WORKSPACE,
            state_code=34,
            time_mode="latest",
        )
    )
    evidence = execute_semantic_query(
        database_path=query_database,
        request=request,
        generate_sql=lambda _context: (
            "SELECT bridge_number, data_year "
            "FROM v_published_bridge_snapshots ORDER BY bridge_number"
        ),
    )
    assert evidence.rows == (("A", 2025), ("B", 2023))
    assert all(row[0] != "PRIVATE" for row in evidence.rows)


def test_one_typed_retry_receives_safe_feedback_and_succeeds(query_database):
    request = _request(
        QueryScope(
            workspace_id=WORKSPACE,
            state_code=34,
            time_mode="exact",
            data_year=2024,
        )
    )
    calls = []

    def generate(context):
        calls.append(context)
        if context.attempt == 1:
            return "DELETE FROM v_published_bridge_snapshots"
        assert context.feedback is not None
        assert context.feedback.code
        return "SELECT bridge_number FROM v_published_bridge_snapshots"

    evidence = execute_semantic_query(
        database_path=query_database,
        request=request,
        generate_sql=generate,
    )
    assert evidence.attempt == 2
    assert evidence.rows == (("A",),)
    assert [call.attempt for call in calls] == [1, 2]
    events = list_query_audit_events(query_database, WORKSPACE, request_id=request.request_id)
    assert [(event.attempt, event.outcome) for event in events] == [
        (1, "accepted"),
        (1, "rejected"),
        (2, "accepted"),
        (2, "completed"),
    ]


def test_malicious_sql_is_never_executed_and_abstains_after_two_attempts(
    query_database,
):
    request = _request(
        QueryScope(
            workspace_id=WORKSPACE,
            state_code=34,
            time_mode="latest",
        )
    )
    with pytest.raises(SemanticQueryAbstained) as caught:
        execute_semantic_query(
            database_path=query_database,
            request=request,
            generate_sql=lambda _context: "SELECT name FROM sqlite_master",
        )
    assert caught.value.attempts == 2
    events = list_query_audit_events(query_database, WORKSPACE, request_id=request.request_id)
    assert [event.outcome for event in events] == [
        "accepted",
        "rejected",
        "accepted",
        "rejected",
    ]
    assert all(event.compiled_sql is None for event in events)


def test_exact_scope_rejects_current_only_view_then_routes_to_history(query_database):
    request = _request(
        QueryScope(
            workspace_id=WORKSPACE,
            state_code=34,
            time_mode="exact",
            data_year=2024,
        )
    )

    def generate(context):
        if context.attempt == 1:
            return "SELECT bridge_number FROM v_current_bridge_snapshots"
        assert context.feedback.code == "incompatible_time_resource"
        return "SELECT bridge_number FROM v_published_bridge_snapshots"

    evidence = execute_semantic_query(
        database_path=query_database,
        request=request,
        generate_sql=generate,
    )
    assert evidence.rows == (("A",),)
    assert evidence.attempt == 2


def test_generator_failure_gets_terminal_audit_and_no_retry(query_database):
    request = _request(
        QueryScope(
            workspace_id=WORKSPACE,
            state_code=34,
            time_mode="latest",
        )
    )

    def fail(_context):
        raise RuntimeError("provider secret must not enter audit")

    with pytest.raises(SemanticQueryGenerationFailed):
        execute_semantic_query(
            database_path=query_database,
            request=request,
            generate_sql=fail,
        )
    events = list_query_audit_events(query_database, WORKSPACE, request_id=request.request_id)
    assert [event.outcome for event in events] == ["accepted", "failed"]
    assert "provider secret" not in repr(events)


def test_unexpected_executor_failure_still_gets_terminal_audit(
    query_database, monkeypatch
):
    request = _request(
        QueryScope(
            workspace_id=WORKSPACE,
            state_code=34,
            time_mode="latest",
        )
    )

    def fail_executor(**_kwargs):
        raise RuntimeError("sqlite driver internals must not enter audit")

    monkeypatch.setattr(query_service_module, "execute_read_query", fail_executor)
    with pytest.raises(SemanticQueryExecutionFailed):
        execute_semantic_query(
            database_path=query_database,
            request=request,
            generate_sql=lambda _context: (
                "SELECT bridge_number FROM v_current_bridge_snapshots"
            ),
        )
    events = list_query_audit_events(query_database, WORKSPACE, request_id=request.request_id)
    assert [event.outcome for event in events] == ["accepted", "failed"]
    assert events[-1].termination_reason == "execution_integration_failed"
    assert "sqlite driver internals" not in repr(events)


def test_context_indexes_all_fields_selects_bounded_details_and_hides_base_schema():
    catalog = build_semantic_catalog(
        database_identity="test-db",
        workspace_id=WORKSPACE,
        schema_generation=LATEST_SCHEMA_VERSION,
        data_generation=9,
    )
    scope = QueryScope(
        workspace_id=WORKSPACE,
        state_code=34,
        time_mode="latest",
    )
    first = build_query_context(
        catalog,
        scope,
        question="Explain deck condition BC01 and legacy Item 58",
    )
    second = build_query_context(
        catalog,
        scope,
        question="Explain deck condition BC01 and legacy Item 58",
    )
    assert first == second
    assert len(first.encode("utf-8")) <= MAX_QUERY_CONTEXT_BYTES
    payload = json.loads(first)
    dictionary_payload = payload["snbi_dictionary"]
    index = dictionary_payload["complete_compact_index"]
    assert len(index) == len(DEFAULT_SNBI_DICTIONARY.fields) == 154
    assert len({item["tag"] for item in index}) == 154
    assert "BC01" in dictionary_payload["selected_detailed_tags"]
    detail = next(
        item
        for item in dictionary_payload["selected_details"]["fields"]
        if item["tag"] == "BC01"
    )
    assert detail["legacy_source_fields"]
    assert "transition_note" in detail
    assert dictionary_payload["selected_details"]["provenance"]
    assert "physical_table" not in first
    assert "physical_column" not in first

    sql_catalog = build_sql_catalog(catalog)
    assert set(sql_catalog.tables) == set(catalog.resources_by_id)
    assert "bridges" not in sql_catalog.tables
    assert sql_catalog.tables["v_bridge_condition"].columns == frozenset(
        catalog.resources_by_id["v_bridge_condition"].column_ids
    )


def test_cancel_helper_is_owner_scoped_and_only_targets_registered_sqlite_phase():
    registry = RequestRegistry()
    request_id = new_request_id()
    registry.register(
        request_id=request_id,
        owner_id="user-001",
        workspace_id=WORKSPACE,
        operation="sqlite_query",
        deadline=time.monotonic() + 10,
    )
    assert cancel_semantic_query(
        request_id=request_id,
        identity=ServerQueryIdentity(
            actor_id="other-user", role="viewer", workspace_id=WORKSPACE
        ),
        registry=registry,
    ) is CancelOutcome.NOT_FOUND
    assert cancel_semantic_query(
        request_id=request_id,
        identity=_identity(),
        registry=registry,
    ) is CancelOutcome.CANCELLED


def test_typed_plan_is_primary_scope_safe_and_audited(query_database, monkeypatch):
    def forbidden_legacy(*_args, **_kwargs):
        raise AssertionError("legacy SQL executor must never be called")

    monkeypatch.setattr(legacy_database, "execute_readonly_sql", forbidden_legacy)
    request = _request(
        QueryScope(
            workspace_id=WORKSPACE,
            state_code=34,
            time_mode="exact",
            data_year=2025,
        )
    )
    calls: list[PlanGenerationRequest] = []

    def generate(context: PlanGenerationRequest):
        calls.append(context)
        return {
            "intent": "list",
            "resource_id": "v_published_bridge_snapshots",
            "selected_fields": ["bridge_number", "data_year"],
            "time_scope": {"mode": "exact"},
            "order": [{"by": "bridge_number", "direction": "asc"}],
            "limit": 20,
        }

    evidence = execute_semantic_plan_query(
        database_path=query_database,
        request=request,
        generate_plan=generate,
    )

    assert evidence.rows == (("A", 2025),)
    assert len(calls) == 1
    assert calls[0].scope.data_year == 2025
    assert evidence.plan_evidence is not None
    assert evidence.plan_evidence.intent == "list"
    assert evidence.plan_evidence.scope["workspace_id"] == WORKSPACE
    assert "workspace_id" not in evidence.plan_evidence.canonical_plan
    assert "state_code" not in evidence.plan_evidence.canonical_plan
    assert "data_year" not in evidence.plan_evidence.canonical_plan["time_scope"]

    events = list_query_audit_events(
        query_database, WORKSPACE, request_id=request.request_id
    )
    assert [event.outcome for event in events] == ["accepted", "completed"]
    assert events[0].prompt_version.startswith(PLAN_PROMPT_VERSION)
    assert "metrics-" in events[0].prompt_version
    assert request.question not in repr(events)


def test_typed_plan_latest_is_per_bridge_and_hides_other_workspace(query_database):
    request = _request(
        QueryScope(workspace_id=WORKSPACE, state_code=34, time_mode="latest")
    )
    evidence = execute_semantic_plan_query(
        database_path=query_database,
        request=request,
        generate_plan=lambda _context: {
            "intent": "list",
            "resource_id": "v_current_bridge_snapshots",
            "selected_fields": ["bridge_number", "data_year"],
            "time_scope": {"mode": "latest"},
            "order": [{"by": "bridge_number", "direction": "asc"}],
            "limit": 20,
        },
    )
    assert evidence.rows == (("A", 2025), ("B", 2023))
    assert all(row[0] != "PRIVATE" for row in evidence.rows)


def test_typed_plan_retries_only_with_code_only_feedback(query_database):
    request = _request(
        QueryScope(
            workspace_id=WORKSPACE,
            state_code=34,
            time_mode="exact",
            data_year=2024,
        )
    )
    calls: list[PlanGenerationRequest] = []

    def generate(context: PlanGenerationRequest):
        calls.append(context)
        if context.attempt == 1:
            return {
                "intent": "list",
                "resource_id": "v_published_bridge_snapshots",
                "selected_fields": ["bridge_number"],
                "time_scope": {"mode": "exact"},
                "workspace_id": OTHER_WORKSPACE,
            }
        assert context.feedback is not None
        assert context.feedback.code == "client_scope_override"
        assert OTHER_WORKSPACE not in context.feedback.instruction
        return {
            "intent": "list",
            "resource_id": "v_published_bridge_snapshots",
            "selected_fields": ["bridge_number"],
            "time_scope": {"mode": "exact"},
        }

    evidence = execute_semantic_plan_query(
        database_path=query_database,
        request=request,
        generate_plan=generate,
    )
    assert evidence.attempt == 2
    assert evidence.rows == (("A",),)
    events = list_query_audit_events(
        query_database, WORKSPACE, request_id=request.request_id
    )
    assert [(event.attempt, event.outcome) for event in events] == [
        (1, "accepted"),
        (1, "rejected"),
        (2, "accepted"),
        (2, "completed"),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        '{"intent":"list","intent":"count","resource_id":"v_published_bridge_snapshots","time_scope":{"mode":"latest"}}',
        '{"intent":"list","resource_id":"v_published_bridge_snapshots","time_scope":{"mode":"latest"},"sql":"SELECT * FROM sqlite_master"}',
        '{"intent":"list","resource_id":"v_published_bridge_snapshots","time_scope":{"mode":"latest"},"limit":NaN}',
    ],
)
def test_typed_plan_malformed_or_sql_payload_abstains_without_execution(
    query_database, payload
):
    request = _request(
        QueryScope(workspace_id=WORKSPACE, state_code=34, time_mode="latest")
    )
    with pytest.raises(SemanticQueryAbstained):
        execute_semantic_plan_query(
            database_path=query_database,
            request=request,
            generate_plan=lambda _context: payload,
        )
    events = list_query_audit_events(
        query_database, WORKSPACE, request_id=request.request_id
    )
    assert [event.outcome for event in events] == [
        "accepted",
        "rejected",
        "accepted",
        "rejected",
    ]
    assert all(event.compiled_sql is None for event in events)


def test_plan_context_has_all_snbi_fields_metrics_and_no_executable_sql():
    catalog = build_semantic_catalog(
        database_identity="test-db",
        workspace_id=WORKSPACE,
        schema_generation=LATEST_SCHEMA_VERSION,
        data_generation=9,
    )
    context = build_plan_context(
        catalog,
        QueryScope(workspace_id=WORKSPACE, state_code=34, time_mode="latest"),
        question="Compare BC01 deck condition and NHS BH03",
    )
    payload = json.loads(context)
    assert len(context.encode("utf-8")) <= MAX_PLAN_CONTEXT_BYTES
    assert payload["protocol_version"] == PLAN_PROMPT_VERSION
    packed_index = payload["snbi_dictionary"]["complete_compact_index"]
    assert packed_index["format"] == "columnar_rows_v2"
    assert packed_index["field_count"] == len(packed_index["rows"]) == 154
    tag_index = packed_index["columns"].index("tag")
    assert {row[tag_index] for row in packed_index["rows"]} == {
        field.tag for field in DEFAULT_SNBI_DICTIONARY.fields
    }
    assert payload["reviewed_metric_registry"]["rows"]
    assert payload["fhwa_informational_enrichment"]["authority_status"] == (
        "informational_source_attributed_not_compliance_verified"
    )
    assert payload["fhwa_informational_enrichment"]["may_satisfy_official_validation"] is False
    assert payload["fhwa_informational_enrichment"]["runtime_network_required"] is False
    assert "snbi_informational_enrichment" not in payload
    assert payload["plan_contract"]["time_scope"]["shape"] == {"mode": "latest"}
    assert "workspace_id" in payload["plan_contract"]["forbidden_fields"]
    assert "sql" in payload["plan_contract"]["forbidden_fields"]
    assert "physical_table" not in context
    assert "physical_column" not in context
    column_format = payload["semantic_catalog"]["column_format"]
    description_index = column_format.index("description")
    prompt_resources = {
        resource["id"]: resource
        for resource in payload["semantic_catalog"]["resources"]
    }
    for resource in catalog.resources:
        prompt_resource = prompt_resources[resource.resource_id]
        assert prompt_resource["description"] == resource.description
        prompt_columns = {
            row[0]: row for row in prompt_resource["columns"]
        }
        assert {
            field_id: row[description_index]
            for field_id, row in prompt_columns.items()
        } == {
            column.field_id: column.description
            for column in resource.columns
        }
