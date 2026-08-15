from __future__ import annotations

from dataclasses import replace
import json
import sqlite3

import pytest

from app.routers.services.database import ensure_schema
from app.routers.services.db_schema import LATEST_SCHEMA_VERSION
from app.services.query.answers import EvidenceTable
from app.services.query.audit import (
    QueryAuditInputError,
    QueryAuditStateError,
    QueryResponseAuditExists,
    QueryResponseAuditWriteError,
    RecordQueryResponseAuditRequest,
    list_query_response_audit_events,
    record_query_response_audit,
)
from app.services.query.plans import BridgeQueryPlan


WORKSPACE = "response-audit-workspace"
OTHER_WORKSPACE = "response-audit-other"
NOW = "2026-08-13T13:00:00Z"


@pytest.fixture
def response_audit_database(test_workspace):
    path = test_workspace / "response-audit.sqlite3"
    ensure_schema(str(path))
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        for workspace in (WORKSPACE, OTHER_WORKSPACE):
            connection.execute(
                "INSERT INTO workspaces (id,slug,display_name,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (workspace, workspace, workspace, "active", NOW, NOW),
            )
            connection.execute(
                "INSERT INTO workspace_state "
                "(workspace_id,data_generation,schema_generation,updated_at) VALUES (?,?,?,?)",
                (workspace, 9, LATEST_SCHEMA_VERSION, NOW),
            )
        connection.commit()
    finally:
        connection.close()
    return path


def _plan(*, secret: str = "Atlantic") -> BridgeQueryPlan:
    return BridgeQueryPlan.from_payload(
        {
            "intent": "count",
            "resource_id": "bridges",
            "metric_ids": ["bridges.bridge_count"],
            "selected_fields": [],
            "dimensions": [],
            "filters": [
                {"field_id": "county_name", "operator": "eq", "value": secret}
            ],
            "time_scope": {"mode": "latest"},
            "order": [],
            "limit": 50,
        }
    )


def _request(**overrides) -> RecordQueryResponseAuditRequest:
    values = {
        "workspace_id": WORKSPACE,
        "request_id": "request-1",
        "actor_id": "user-1",
        "surface": "bridgette-full",
        "plan": _plan(),
        "compiled_query_sha256": "a" * 64,
        "evidence": EvidenceTable.from_rows(
            ["bridge_count"], [[12]], column_units={"bridge_count": "bridges"}
        ),
        "answer_verification_version": "answer-verification-v1",
        "answer_mode": "deterministic",
        "verification_status": "verified",
        "provider_call_count": 0,
        "claim_count": 1,
        "verified_claim_count": 1,
        "rejected_claim_count": 0,
        "fallback_count": 0,
        "schema_generation": LATEST_SCHEMA_VERSION,
        "data_generation": 9,
        "catalog_version": "semantic-catalog-v1",
        "catalog_sha256": "b" * 64,
        "dictionary_version": "bridgeguard-snbi-dictionary-v1",
        "dictionary_sha256": "c" * 64,
        "created_at": NOW,
    }
    values.update(overrides)
    return RecordQueryResponseAuditRequest(**values)


def test_response_receipt_is_append_only_checksummed_and_privacy_minimized(
    response_audit_database,
):
    event = record_query_response_audit(response_audit_database, _request())

    stored = list_query_response_audit_events(
        response_audit_database, WORKSPACE, request_id="request-1"
    )
    assert stored == (event,)
    plan = json.loads(event.plan_json)
    assert plan["filters"][0]["value"] == {"redacted": True, "type": "text"}
    serialized = repr(event)
    for private in (
        "Atlantic",
        "response-audit-workspace",
        "SELECT ",
        "bridge_count': 12",
    ):
        if private == "response-audit-workspace":
            # The workspace identity is required metadata, but must not leak
            # into the plan itself.
            assert private not in event.plan_json
        else:
            assert private not in serialized
    assert event.evidence_row_count == 1
    assert event.evidence_column_count == 1
    assert len(event.evidence_sha256) == 64
    assert len(event.record_sha256) == 64

    connection = sqlite3.connect(response_audit_database)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE query_response_audit SET claim_count=9 WHERE id=?",
                (event.audit_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM query_response_audit WHERE id=?", (event.audit_id,)
            )
    finally:
        connection.close()


def test_duplicate_and_cross_workspace_access_are_non_enumerating(
    response_audit_database,
):
    record_query_response_audit(response_audit_database, _request())
    with pytest.raises(QueryResponseAuditExists):
        record_query_response_audit(response_audit_database, _request())
    assert list_query_response_audit_events(
        response_audit_database, OTHER_WORKSPACE, request_id="request-1"
    ) == ()


def test_missing_workspace_fk_fails_closed_and_rolls_back(response_audit_database):
    with pytest.raises(QueryResponseAuditWriteError):
        record_query_response_audit(
            response_audit_database,
            _request(workspace_id="missing-workspace", request_id="missing-request"),
        )
    assert list_query_response_audit_events(
        response_audit_database, WORKSPACE
    ) == ()


def test_induced_insert_failure_rolls_back_without_partial_receipt(
    response_audit_database,
):
    connection = sqlite3.connect(response_audit_database)
    try:
        connection.execute(
            "CREATE TRIGGER fail_response_audit BEFORE INSERT ON query_response_audit "
            "BEGIN SELECT RAISE(ABORT,'private failure'); END"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(QueryResponseAuditWriteError) as error:
        record_query_response_audit(response_audit_database, _request())
    assert "private failure" not in str(error.value)
    assert list_query_response_audit_events(response_audit_database, WORKSPACE) == ()


def test_reader_detects_row_or_plan_tampering(response_audit_database):
    event = record_query_response_audit(response_audit_database, _request())
    connection = sqlite3.connect(response_audit_database)
    try:
        connection.execute("DROP TRIGGER trg_query_response_audit_no_update")
        connection.execute(
            "UPDATE query_response_audit SET provider_call_count=1 WHERE id=?",
            (event.audit_id,),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(QueryAuditStateError, match="record checksum mismatch"):
        list_query_response_audit_events(response_audit_database, WORKSPACE)


def test_constructor_rejects_scope_override_and_inconsistent_counts():
    payload = _plan().canonical_payload()
    payload["workspace_id"] = "attacker"
    with pytest.raises(QueryAuditInputError):
        _request(plan=payload)
    with pytest.raises(QueryAuditInputError, match="claim_count"):
        _request(claim_count=2, verified_claim_count=1, rejected_claim_count=0)


def test_evidence_hash_changes_without_persisting_evidence_values(
    response_audit_database,
):
    first = record_query_response_audit(response_audit_database, _request())
    second = record_query_response_audit(
        response_audit_database,
        _request(
            request_id="request-2",
            evidence=EvidenceTable.from_rows(["bridge_count"], [[13]]),
        ),
    )
    assert first.evidence_sha256 != second.evidence_sha256
    connection = sqlite3.connect(response_audit_database)
    try:
        values = connection.execute(
            "SELECT * FROM query_response_audit ORDER BY request_id"
        ).fetchall()
        columns = tuple(
            str(row[1]) for row in connection.execute("PRAGMA table_info(query_response_audit)")
        )
    finally:
        connection.close()
    assert not any(name in columns for name in ("question", "answer", "rows", "sql", "path"))
    assert "Atlantic" not in repr(values)

