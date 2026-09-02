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
    assert evidence_event["catalog_version"] == "bridgeguard-semantic-v2-available-inventory"
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
    assert evidence_event["answer_verification"]["status"] == "abstained"
    assert evidence_event["answer_verification"]["claims"] == []
    assert "999" not in token_event["content"]
    assert "could not safely summarize" in token_event["content"]


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
    assert evidence_event["answer_status"] == "sql_assisted"
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
        "No failed, partial, or unverified result was released."
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
    assert event["options"][0]["suggested_question"] != Request.question
    assert "distinct bridges" in event["options"][0]["suggested_question"]


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

    assert latest["mode"] == "latest_loaded_inventory"
    assert latest["data_year"] == 2025
    assert exact["mode"] == "exact_inventory_year"
    assert exact["data_year"] == 2024
    assert trend["mode"] == "group_by_inventory_year"
    assert trend["start_year"] == 2020
    assert trend["end_year"] == 2025
    assert http_service._raw_sql_respects_time_contract(
        "SELECT COUNT(*) FROM bridges b WHERE b.data_year = 2025",
        latest,
    )
    assert not http_service._raw_sql_respects_time_contract(
        "SELECT COUNT(*) FROM bridges",
        latest,
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
    result_contract = http_service._raw_result_contract(
        "List latest span configurations with reinforced concrete cast-in-place deck"
    )
    assert "exactly one column named span_configuration_designation" in result_contract
    assert "C01, CR-T" in result_contract


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
