from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
import csv
import io
import json
import logging
import threading

import pytest

from app.services.query import http_service
from app.query_api import BoundedResultStore
from app.services.query.service import (
    PlanGenerationRequest,
    QueryAstEvidence,
    SemanticQueryEvidence,
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
    assert "each bridge" in evidence_event["interpreted_scope"]["interpretation"]
    assert evidence_event["query_plan"]["compiled_from_validated_sql"] is True
    assert evidence_event["query_plan"]["result_shape"] == "scalar"
    assert evidence_event["metric_definitions"][0]["metric_id"] == "inventory.bridge_count"
    assert evidence_event["source_snapshot_status"] == "published_only"
    assert evidence_event["source_status"]["unpublished_rows_excluded"] is True
    assert evidence_event["catalog_version"] == "bridgeguard-semantic-v1"
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
    assert events[0]["code"] == "current_time_semantics"
    assert calls == []


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
