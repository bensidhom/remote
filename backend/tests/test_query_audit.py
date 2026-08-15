from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import sqlite3

import pytest

from app.routers.services.database import ensure_schema
from app.routers.services.db_schema import LATEST_SCHEMA_VERSION
from app.services.query.audit import (
    AcceptQueryAuditRequest,
    AcceptedAuditWriteError,
    FinishQueryAuditRequest,
    QueryAuditDuplicateTerminal,
    QueryAuditOutcome,
    QueryAuditScope,
    QueryAuditStateError,
    TerminalAuditWriteError,
    ValidatedAstAudit,
    accept_query_attempt,
    finish_query_attempt,
    list_query_audit_events,
)


WORKSPACE = "query-audit-workspace"
NOW = "2026-08-13T12:00:00Z"


@pytest.fixture
def audit_database(test_workspace):
    path = test_workspace / "query-audit.sqlite3"
    ensure_schema(str(path))
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO workspaces (id,slug,display_name,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (WORKSPACE, WORKSPACE, "Query Audit", "active", NOW, NOW),
        )
        connection.execute(
            "INSERT INTO workspace_state "
            "(workspace_id,data_generation,schema_generation,updated_at) VALUES (?,?,?,?)",
            (WORKSPACE, 7, LATEST_SCHEMA_VERSION, NOW),
        )
        connection.commit()
    finally:
        connection.close()
    return path


def _accept_request(*, attempt: int = 1, question: str = "How many poor bridges?"):
    return AcceptQueryAuditRequest(
        workspace_id=WORKSPACE,
        request_id=f"request-{attempt}",
        attempt=attempt,
        actor_id="user-001",
        role="viewer",
        session_id="session-001",
        surface="bridgette-full",
        question=question,
        scope=QueryAuditScope(
            workspace_id=WORKSPACE,
            state_code=34,
            time_mode="latest",
        ),
        schema_generation=LATEST_SCHEMA_VERSION,
        data_generation=7,
        catalog_sha256="c" * 64,
        model_id="model-v1",
        prompt_version="bridgette-v7",
        accepted_at=NOW,
    )


def _ast():
    return ValidatedAstAudit(
        tables=("v_bridge_condition",),
        columns=("condition_class",),
        output_columns=("bridge_count",),
        cte_count=0,
        join_count=0,
        subquery_count=0,
        expression_depth=1,
        literal_count=1,
    )


def _finish(handle, **overrides):
    values = {
        "handle": handle,
        "outcome": QueryAuditOutcome.COMPLETED,
        "finished_at": "2026-08-13T12:00:00.125Z",
        "duration_ms": 125,
        "progress_steps": 4,
        "termination_reason": "completed",
        "validated_ast": _ast(),
        "compiled_sql": "SELECT COUNT(*) AS bridge_count FROM v_bridge_condition WHERE workspace_id=? AND state_code=? LIMIT ?",
        "parameters": (WORKSPACE, 34, 201),
        "row_count": 1,
        "byte_count": 22,
    }
    values.update(overrides)
    return FinishQueryAuditRequest(**values)


def test_success_records_accepted_then_exactly_one_terminal(audit_database):
    handle = accept_query_attempt(audit_database, _accept_request())
    result = finish_query_attempt(audit_database, _finish(handle))

    assert result.event_type == "terminal"
    assert result.outcome == "completed"
    events = list_query_audit_events(audit_database, WORKSPACE, request_id="request-1")
    assert [event.event_type for event in events] == ["accepted", "terminal"]
    assert [event.outcome for event in events] == ["accepted", "completed"]
    assert events[0].question_sha256 == hashlib.sha256(
        b"How many poor bridges?"
    ).hexdigest()
    assert events[0].ast_json is None
    assert json.loads(events[1].ast_json)["statement_kind"] == "select"


@pytest.mark.parametrize(
    ("attempt", "outcome", "reason"),
    [
        (2, QueryAuditOutcome.REJECTED, "validation_rejected"),
        (3, QueryAuditOutcome.TIMED_OUT, "deadline_exceeded"),
        (4, QueryAuditOutcome.CANCELLED, "client_cancelled"),
        (5, QueryAuditOutcome.FAILED, "execution_failed"),
    ],
)
def test_non_success_terminal_outcomes_are_typed_and_value_free(
    audit_database, attempt, outcome, reason
):
    handle = accept_query_attempt(audit_database, _accept_request(attempt=attempt))
    event = finish_query_attempt(
        audit_database,
        FinishQueryAuditRequest(
            handle=handle,
            outcome=outcome,
            finished_at="2026-08-13T12:00:01Z",
            duration_ms=10,
            progress_steps=1,
            termination_reason=reason,
        ),
    )
    assert event.outcome == outcome.value
    assert event.termination_reason == reason
    assert event.ast_json is None
    assert event.compiled_sql is None
    assert event.parameters_json is None
    assert event.row_count is None
    assert event.byte_count is None


def test_duplicate_terminal_is_rejected_without_a_third_event(audit_database):
    handle = accept_query_attempt(audit_database, _accept_request())
    request = _finish(handle)
    finish_query_attempt(audit_database, request)
    with pytest.raises(QueryAuditDuplicateTerminal):
        finish_query_attempt(audit_database, request)
    assert len(list_query_audit_events(audit_database, WORKSPACE)) == 2


def test_privacy_contract_never_persists_question_parameter_values_or_results(
    audit_database,
):
    question = "Show secret inspection notes for bridge 123456"
    handle = accept_query_attempt(
        audit_database, _accept_request(question=question)
    )
    finish_query_attempt(
        audit_database,
        _finish(
            handle,
            parameters=("TOP-SECRET-VALUE", 34),
            row_count=9,
            byte_count=777,
        ),
    )

    connection = sqlite3.connect(audit_database)
    try:
        rows = connection.execute(
            "SELECT question_sha256,scope_json,ast_json,compiled_sql,parameters_json,"
            "row_count,byte_count FROM query_audit ORDER BY event_type"
        ).fetchall()
    finally:
        connection.close()
    serialized = repr(rows)
    assert question not in serialized
    assert "TOP-SECRET-VALUE" not in serialized
    assert "inspection notes" not in serialized
    terminal = next(row for row in rows if row[4] is not None)
    parameter_evidence = json.loads(terminal[4])
    assert parameter_evidence == {
        "kind": "positional",
        "parameters": [
            {"position": 1, "redacted": True, "type": "text"},
            {"position": 2, "redacted": True, "type": "integer"},
        ],
    }
    assert terminal[5:] == (9, 777)


def test_terminal_write_failure_rolls_back_and_is_typed(audit_database):
    handle = accept_query_attempt(audit_database, _accept_request())
    connection = sqlite3.connect(audit_database)
    try:
        connection.execute(
            "CREATE TRIGGER fail_query_terminal BEFORE INSERT ON query_audit "
            "WHEN NEW.event_type='terminal' BEGIN "
            "SELECT RAISE(ABORT,'induced terminal failure'); END"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(TerminalAuditWriteError) as error:
        finish_query_attempt(audit_database, _finish(handle))
    assert "induced terminal failure" not in str(error.value)
    assert [event.event_type for event in list_query_audit_events(audit_database, WORKSPACE)] == [
        "accepted"
    ]


def test_accepted_write_failure_is_fail_closed_and_rolls_back(audit_database):
    connection = sqlite3.connect(audit_database)
    try:
        connection.execute(
            "CREATE TRIGGER fail_query_accept BEFORE INSERT ON query_audit "
            "BEGIN SELECT RAISE(ABORT,'induced accepted failure'); END"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(AcceptedAuditWriteError) as error:
        accept_query_attempt(audit_database, _accept_request())
    assert "induced accepted failure" not in str(error.value)
    assert list_query_audit_events(audit_database, WORKSPACE) == ()


def test_cross_workspace_fk_failure_is_typed_and_writes_nothing(audit_database):
    request = _accept_request()
    request = replace(
        request,
        workspace_id="missing-workspace",
        scope=replace(request.scope, workspace_id="missing-workspace"),
    )
    with pytest.raises(AcceptedAuditWriteError):
        accept_query_attempt(audit_database, request)
    assert list_query_audit_events(audit_database, WORKSPACE) == ()


def test_tampered_handle_cannot_write_terminal_to_an_accepted_attempt(audit_database):
    handle = accept_query_attempt(audit_database, _accept_request())
    forged = replace(handle, actor_id="different-actor")
    with pytest.raises(QueryAuditStateError):
        finish_query_attempt(audit_database, _finish(forged))
    assert [event.event_type for event in list_query_audit_events(audit_database, WORKSPACE)] == [
        "accepted"
    ]


def test_json_and_ids_are_deterministic(audit_database):
    handle = accept_query_attempt(audit_database, _accept_request())
    event = finish_query_attempt(
        audit_database,
        _finish(handle, parameters={"state": 34, "name": "Atlantic"}),
    )
    assert handle.audit_id == "93227923-5720-5e59-8142-b9f065197736"
    assert event.audit_id == "751933b0-0e2e-5f2a-bf91-36a2bc867570"
    assert event.parameters_json == (
        '{"kind":"named","parameters":['
        '{"name":"name","redacted":true,"type":"text"},'
        '{"name":"state","redacted":true,"type":"integer"}]}'
    )
