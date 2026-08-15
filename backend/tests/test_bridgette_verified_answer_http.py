"""Red-first PR8 HTTP contract for verified Bridgette answers and evidence.

This suite does not exercise a provider or production database.  It freezes the
protocol expected from the forthcoming query-plan, deterministic renderer,
structured-claim verifier, ambiguity gate, and bounded-result store through the
same late-bound service seam used by the PR7 sandbox HTTP tests.

Security invariants belong at this boundary: full-page and sidebar consumers
receive equal evidence, downloads are request/owner/workspace scoped and
content-addressed without local paths, and clarification or abstention happens
before any SQL or model call when semantics cannot be selected safely.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Iterator, Mapping

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main, revision_api
from app.auth import AuthMiddleware, AuthUser, COOKIE_NAME, issue_session


WORKSPACE = revision_api.SHARED_WORKSPACE_ID
STATE = 34
FULL_REQUEST_ID = "10101010-1010-4010-8010-101010101010"
SIDEBAR_REQUEST_ID = "20202020-2020-4020-8020-202020202020"
SCALAR_REQUEST_ID = "30303030-3030-4030-8030-303030303030"
TABLE_REQUEST_ID = "40404040-4040-4040-8040-404040404040"
NARRATIVE_REQUEST_ID = "50505050-5050-4050-8050-505050505050"
AMBIGUOUS_REQUEST_ID = "60606060-6060-4060-8060-606060606060"
ABSTAIN_REQUEST_ID = "70707070-7070-4070-8070-707070707070"
DOWNLOAD_REQUEST_ID = "80808080-8080-4080-8080-808080808080"

CATALOG_VERSION = "semantic-catalog-sha256:" + "a" * 64
DICTIONARY_VERSION = "snbi-dictionary-sha256:" + "b" * 64
COMPILED_QUERY_SHA256 = "c" * 64
RESULT_BYTES = (
    b"bridge_number,data_year,controlling_condition_class\r\n"
    b"0000001,2025,Good\r\n"
    b"0000002,2025,Fair\r\n"
)
RESULT_SHA256 = hashlib.sha256(RESULT_BYTES).hexdigest()


AUTH_ENV = {
    "BRIDGEGUARD_AUTH_DISABLED": "0",
    "BRIDGEGUARD_AUTH_MODE": "password",
    "BRIDGEGUARD_SESSION_SECRET": (
        "bridgette-pr8-contract-secret-longer-than-32-characters"
    ),
    "BRIDGEGUARD_PASSWORD": "bridgette-pr8-contract-password",
    "BRIDGEGUARD_ALLOWED_EMAILS": "viewer@example.com,other@example.com",
    "BRIDGEGUARD_EDITOR_EMAILS": "",
    "BRIDGEGUARD_ADMIN_EMAILS": "",
    "BRIDGEGUARD_ALLOWED_DOMAINS": "",
    "BRIDGEGUARD_OWNER_EMAIL": "",
}


def _value(value: object, name: str):
    if isinstance(value, Mapping):
        return value[name]
    return getattr(value, name)


def _enum_value(value: object):
    return getattr(value, "value", value)


def _interpreted_scope(request: object) -> dict[str, object]:
    scope = _value(request, "scope")
    return {
        "workspace_id": _value(scope, "workspace_id"),
        "state_code": _value(scope, "state_code"),
        "state_name": "New Jersey",
        "bridge_number": _value(scope, "bridge_number"),
        "time_mode": _enum_value(_value(scope, "time_mode")),
        "data_year": _value(scope, "data_year"),
        "start_year": _value(scope, "start_year"),
        "end_year": _value(scope, "end_year"),
        "snapshot_status": "published",
        "interpretation": "Latest published snapshot for each bridge in scope.",
    }


def _query_plan(intent: str, *, metric_ids: list[str]) -> dict[str, object]:
    return {
        "version": "bridge-query-plan-v1",
        "intent": intent,
        "metric_ids": metric_ids,
        "dimensions": ["data_year"] if intent == "table" else [],
        "filters": [],
        "order": [],
        "limit": 200,
        "compiled_from_validated_plan": True,
    }


def _metric_definitions(metric_ids: list[str]) -> list[dict[str, object]]:
    definitions = {
        "inventory.bridge_count": {
            "metric_id": "inventory.bridge_count",
            "label": "Bridge count",
            "unit": "bridges",
            "classification": "operational_inventory",
            "null_semantics": "Rows with a published bridge identity are counted.",
            "denominator_semantics": "Not applicable to this count.",
            "time_semantics": "Per selected published snapshot scope.",
        },
        "inventory.condition_class": {
            "metric_id": "inventory.condition_class",
            "label": "Controlling bridge condition class",
            "unit": "class",
            "classification": "operational_inventory",
            "null_semantics": "Missing controlling ratings are Unknown, not Poor.",
            "denominator_semantics": "Not an FHWA deck-area performance measure.",
            "time_semantics": "Condition at the selected published snapshot.",
        },
    }
    return [definitions[metric_id] for metric_id in metric_ids]


def _verification(
    *,
    mode: str,
    claims: list[dict[str, object]],
    provider_calls: int,
) -> dict[str, object]:
    return {
        "version": "answer-verification-v1",
        "mode": mode,
        "status": "verified",
        "provider_calls": provider_calls,
        "claims": claims,
        "unverified_claim_count": 0,
        "fallback_used": False,
    }


def _download_metadata(request_id: str, *, rows: int, columns: int) -> dict[str, object]:
    return {
        "available": True,
        "href": f"/api/bridgette/requests/{request_id}/result",
        "filename": f"bridgette-{RESULT_SHA256}.csv",
        "media_type": "text/csv",
        "row_count": rows,
        "column_count": columns,
        "byte_count": len(RESULT_BYTES),
        "sha256": RESULT_SHA256,
        "truncated": False,
        "bounded": True,
    }


def _evidence(
    request: object,
    *,
    intent: str,
    metric_ids: list[str],
    columns: list[str],
    rows: list[list[object]],
    answer_verification: dict[str, object],
) -> dict[str, object]:
    request_id = str(_value(request, "request_id"))
    return {
        "type": "evidence",
        "sql": "SELECT reviewed_fields FROM reviewed_semantic_view",
        "columns": columns,
        "rows": rows,
        "count": len(rows),
        "truncated": False,
        "scope": {
            "workspace_id": WORKSPACE,
            "state_code": STATE,
            "time_mode": "latest",
            "year": None,
            "start_year": None,
            "end_year": None,
        },
        "interpreted_scope": _interpreted_scope(request),
        "query_plan": _query_plan(intent, metric_ids=metric_ids),
        "metric_definitions": _metric_definitions(metric_ids),
        "compiled_query_sha256": COMPILED_QUERY_SHA256,
        "catalog_version": CATALOG_VERSION,
        "snbi_dictionary_version": DICTIONARY_VERSION,
        "schema_generation": 5,
        "data_generation": 9,
        "source_snapshot_status": "published_only",
        "warnings": [],
        "answer_verification": answer_verification,
        "result_download": _download_metadata(
            request_id,
            rows=len(rows),
            columns=len(columns),
        ),
    }


class VerifiedAnswerService:
    """Provider-free PR8 service double with observable phase counters."""

    def __init__(self) -> None:
        self.requests: list[object] = []
        self.sql_calls = 0
        self.model_calls = 0

    def __call__(self, request: object) -> Iterator[dict[str, object]]:
        self.requests.append(request)
        question = str(_value(request, "question")).casefold()

        ambiguous = {
            "show the current bridges": (
                "current_time_scope",
                "What should current mean: each bridge's latest published snapshot, "
                "or one specific inventory year?",
            ),
            "how many bridges are in springfield county": (
                "county_identity",
                "Which county did you mean? Choose an approved county in the selected state.",
            ),
            "show condition for washington bridge": (
                "bridge_identity",
                "More than one published bridge matches that name. Choose a bridge number.",
            ),
            "how many nhs bridges are there": (
                "nhs_meaning",
                "Should NHS mean the SNBI inventory designation or another program scope?",
            ),
            "how many bridges have bad condition": (
                "condition_metric",
                "Should condition mean the reviewed controlling bridge condition class, "
                "a component rating, or an FHWA deck-area performance measure?",
            ),
        }
        if question in ambiguous:
            code, prompt = ambiguous[question]
            yield {
                "type": "clarification",
                "code": code,
                "message": prompt,
                "options": [
                    {"id": "reviewed-default", "label": "Reviewed default"},
                    {"id": "specify", "label": "Specify another meaning"},
                ],
                "interpreted_scope": _interpreted_scope(request),
                "warnings": ["No SQL or model call was made."],
            }
            return

        if question == "what color was the bridge originally painted":
            yield {
                "type": "abstention",
                "code": "unsupported_fact",
                "message": (
                    "The approved bridge semantic catalog does not contain the "
                    "bridge's original paint color."
                ),
                "requested_fact": "original paint color",
                "nearest_fields_not_substituted": [
                    "superstructure_material",
                    "protective_system",
                ],
                "interpreted_scope": _interpreted_scope(request),
                "warnings": ["No nearby field was used as a proxy."],
            }
            return

        self.sql_calls += 1
        request_id = str(_value(request, "request_id"))
        owner_id = str(_value(request, "actor_id"))
        result_store = getattr(main, "BRIDGETTE_RESULT_STORE", None)
        if result_store is not None:
            result_store.put(
                request_id=request_id,
                owner_id=owner_id,
                workspace_id=WORKSPACE,
                content=RESULT_BYTES,
                filename=f"bridgette-{RESULT_SHA256}.csv",
                media_type="text/csv",
                row_count=2,
                column_count=3,
                truncated=False,
            )
        if "how many" in question:
            claims = [
                {
                    "claim_id": "claim-1",
                    "claim": "There are 12 published bridges.",
                    "status": "verified",
                    "evidence_cells": ["r0.bridge_count"],
                    "operation": "direct",
                    "rendered_value": 12,
                    "unit": "bridges",
                }
            ]
            yield _evidence(
                request,
                intent="scalar",
                metric_ids=["inventory.bridge_count"],
                columns=["bridge_count"],
                rows=[[12]],
                answer_verification=_verification(
                    mode="deterministic",
                    claims=claims,
                    provider_calls=0,
                ),
            )
            yield {"type": "token", "content": "There are 12 published bridges."}
            return

        if "table" in question or "list" in question:
            claims = [
                {
                    "claim_id": "table-rows",
                    "claim": "Rendered two bounded result rows.",
                    "status": "verified",
                    "evidence_cells": ["r0.*", "r1.*"],
                    "operation": "table_render",
                    "rendered_value": 2,
                    "unit": "rows",
                }
            ]
            yield _evidence(
                request,
                intent="table",
                metric_ids=["inventory.condition_class"],
                columns=[
                    "bridge_number",
                    "data_year",
                    "controlling_condition_class",
                ],
                rows=[
                    ["0000001", 2025, "Good"],
                    ["0000002", 2025, "Fair"],
                ],
                answer_verification=_verification(
                    mode="deterministic",
                    claims=claims,
                    provider_calls=0,
                ),
            )
            yield {
                "type": "token",
                "content": (
                    "| Bridge | Year | Condition |\n"
                    "|---|---:|---|\n"
                    "| 0000001 | 2025 | Good |\n"
                    "| 0000002 | 2025 | Fair |"
                ),
            }
            return

        self.model_calls += 1
        claims = [
            {
                "claim_id": "claim-1",
                "claim": "One of the two returned bridges is Good.",
                "status": "verified",
                "evidence_cells": [
                    "r0.controlling_condition_class",
                    "r1.controlling_condition_class",
                ],
                "operation": "count_equal",
                "rendered_value": 1,
                "unit": "bridges",
            }
        ]
        yield _evidence(
            request,
            intent="narrative",
            metric_ids=["inventory.condition_class"],
            columns=["bridge_number", "controlling_condition_class"],
            rows=[["0000001", "Good"], ["0000002", "Fair"]],
            answer_verification=_verification(
                mode="structured_claims",
                claims=claims,
                provider_calls=1,
            ),
        )
        yield {
            "type": "token",
            "content": "One of the two returned bridges is Good, and the other is Fair.",
        }


@dataclass
class Harness:
    client: TestClient
    service: VerifiedAnswerService

    def headers(
        self,
        request_id: str,
        *,
        email: str = "viewer@example.com",
    ) -> dict[str, str]:
        token = issue_session(AuthUser(email=email, role="viewer"))
        return {
            "Cookie": f"{COOKIE_NAME}={token}",
            "X-Request-ID": request_id,
        }


def _contract_app() -> FastAPI:
    focused = FastAPI()
    focused.include_router(main.bridgette_query_router)
    focused.add_middleware(AuthMiddleware)
    return focused


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    for key, value in AUTH_ENV.items():
        monkeypatch.setenv(key, value)
    service = VerifiedAnswerService()
    monkeypatch.setattr(main, "_bridgette_service_stream", service)
    # The HTTP result store must be an injectable in-memory, owner-scoped
    # boundary; tests must never rely on a local result path.
    try:
        from app.query_api import BoundedResultStore
    except ImportError:
        pass
    else:
        result_store = BoundedResultStore()
        monkeypatch.setattr(main, "BRIDGETTE_RESULT_STORE", result_store)
    with TestClient(_contract_app()) as client:
        yield Harness(client=client, service=service)


def _events(response) -> list[dict[str, object]]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def _post(
    harness: Harness,
    path: str,
    request_id: str,
    question: str,
):
    return harness.client.post(
        path,
        headers=harness.headers(request_id),
        data={"question": question},
    )


RICH_EVIDENCE_KEYS = {
    "type",
    "request_id",
    "sql",
    "columns",
    "rows",
    "count",
    "truncated",
    "scope",
    "interpreted_scope",
    "query_plan",
    "metric_definitions",
    "compiled_query_sha256",
    "catalog_version",
    "snbi_dictionary_version",
    "schema_generation",
    "data_generation",
    "source_snapshot_status",
    "warnings",
    "answer_verification",
    "result_download",
}


@pytest.mark.parametrize(
    ("path", "request_id", "surface"),
    (
        ("/api/bridgette/stream", FULL_REQUEST_ID, "full"),
        ("/api/bridgette/sidebar/stream", SIDEBAR_REQUEST_ID, "sidebar"),
    ),
)
def test_full_and_sidebar_expose_the_same_complete_verified_evidence_contract(
    harness: Harness,
    path: str,
    request_id: str,
    surface: str,
):
    response = _post(harness, path, request_id, "List the condition table")

    assert response.status_code == 200, response.text
    events = _events(response)
    assert [event["type"] for event in events] == [
        "accepted",
        "evidence",
        "token",
        "done",
    ]
    assert all(event["request_id"] == request_id for event in events)
    assert events[0]["surface"] == surface
    evidence = events[1]
    assert set(evidence) == RICH_EVIDENCE_KEYS

    interpreted = evidence["interpreted_scope"]
    assert interpreted == {
        "workspace_id": WORKSPACE,
        "state_code": STATE,
        "state_name": "New Jersey",
        "bridge_number": None,
        "time_mode": "latest",
        "data_year": None,
        "start_year": None,
        "end_year": None,
        "snapshot_status": "published",
        "interpretation": "Latest published snapshot for each bridge in scope.",
    }
    assert evidence["query_plan"]["version"] == "bridge-query-plan-v1"
    assert evidence["query_plan"]["compiled_from_validated_plan"] is True
    assert evidence["metric_definitions"][0]["metric_id"] == (
        "inventory.condition_class"
    )
    assert evidence["compiled_query_sha256"] == COMPILED_QUERY_SHA256
    assert evidence["catalog_version"] == CATALOG_VERSION
    assert evidence["snbi_dictionary_version"] == DICTIONARY_VERSION
    assert evidence["schema_generation"] == 5
    assert evidence["data_generation"] == 9
    assert evidence["warnings"] == []

    download = evidence["result_download"]
    assert download["bounded"] is True
    assert download["row_count"] == evidence["count"] == len(evidence["rows"])
    assert download["column_count"] == len(evidence["columns"])
    assert download["truncated"] == evidence["truncated"] is False
    assert download["sha256"] == RESULT_SHA256
    assert RESULT_SHA256 in download["filename"]
    serialized = json.dumps(evidence)
    assert "C:\\" not in serialized
    assert "file://" not in serialized


def test_full_and_sidebar_evidence_key_parity_is_exact(harness: Harness):
    full = _post(
        harness,
        "/api/bridgette/stream",
        FULL_REQUEST_ID,
        "List the condition table",
    )
    sidebar = _post(
        harness,
        "/api/bridgette/sidebar/stream",
        SIDEBAR_REQUEST_ID,
        "List the condition table",
    )

    full_evidence = next(e for e in _events(full) if e["type"] == "evidence")
    sidebar_evidence = next(
        e for e in _events(sidebar) if e["type"] == "evidence"
    )
    assert set(full_evidence) == set(sidebar_evidence) == RICH_EVIDENCE_KEYS
    for key in (
        "interpreted_scope",
        "query_plan",
        "metric_definitions",
        "compiled_query_sha256",
        "catalog_version",
        "snbi_dictionary_version",
        "schema_generation",
        "data_generation",
        "source_snapshot_status",
        "warnings",
        "answer_verification",
    ):
        assert full_evidence[key] == sidebar_evidence[key]


@pytest.mark.parametrize(
    ("request_id", "question", "expected_text"),
    (
        (
            SCALAR_REQUEST_ID,
            "How many bridges are published?",
            "There are 12 published bridges.",
        ),
        (
            TABLE_REQUEST_ID,
            "List the condition table",
            "| Bridge | Year | Condition |",
        ),
    ),
)
def test_scalar_and_short_table_answers_are_deterministic_without_model_call(
    harness: Harness,
    request_id: str,
    question: str,
    expected_text: str,
):
    before_model_calls = harness.service.model_calls
    response = _post(
        harness,
        "/api/bridgette/stream",
        request_id,
        question,
    )

    assert response.status_code == 200
    events = _events(response)
    answer = "".join(
        str(event["content"])
        for event in events
        if event["type"] == "token"
    )
    assert expected_text in answer
    assert harness.service.model_calls == before_model_calls
    verification = next(
        event for event in events if event["type"] == "evidence"
    )["answer_verification"]
    assert verification["mode"] == "deterministic"
    assert verification["status"] == "verified"
    assert verification["provider_calls"] == 0
    assert verification["unverified_claim_count"] == 0
    assert verification["fallback_used"] is False
    assert verification["claims"]
    assert all(claim["status"] == "verified" for claim in verification["claims"])


def test_model_narrative_exposes_structured_cell_claim_verification(
    harness: Harness,
):
    response = _post(
        harness,
        "/api/bridgette/stream",
        NARRATIVE_REQUEST_ID,
        "Explain the returned condition distribution",
    )

    assert response.status_code == 200
    events = _events(response)
    evidence = next(event for event in events if event["type"] == "evidence")
    verification = evidence["answer_verification"]
    assert verification["version"] == "answer-verification-v1"
    assert verification["mode"] == "structured_claims"
    assert verification["status"] == "verified"
    assert verification["provider_calls"] == 1
    assert verification["unverified_claim_count"] == 0
    assert verification["fallback_used"] is False
    assert harness.service.model_calls == 1

    allowed_cells = {
        f"r{row_index}.{column}"
        for row_index, _row in enumerate(evidence["rows"])
        for column in evidence["columns"]
    }
    claims = verification["claims"]
    assert claims
    for claim in claims:
        assert claim["status"] == "verified"
        assert claim["evidence_cells"]
        assert set(claim["evidence_cells"]) <= allowed_cells
        assert claim["operation"] in {
            "direct",
            "count_equal",
            "sum",
            "difference",
            "percent",
        }
        assert claim["rendered_value"] is not None
        assert claim["unit"]


@pytest.mark.parametrize(
    ("question", "expected_code"),
    (
        ("Show the current bridges", "current_time_scope"),
        ("How many bridges are in Springfield County", "county_identity"),
        ("Show condition for Washington Bridge", "bridge_identity"),
        ("How many NHS bridges are there", "nhs_meaning"),
        ("How many bridges have bad condition", "condition_metric"),
    ),
)
def test_semantic_ambiguity_clarifies_before_sql_or_model(
    harness: Harness,
    question: str,
    expected_code: str,
):
    before_sql = harness.service.sql_calls
    before_model = harness.service.model_calls
    response = _post(
        harness,
        "/api/bridgette/stream",
        AMBIGUOUS_REQUEST_ID,
        question,
    )

    assert response.status_code == 200, response.text
    events = _events(response)
    assert [event["type"] for event in events] == [
        "accepted",
        "clarification",
        "done",
    ]
    assert all(event["request_id"] == AMBIGUOUS_REQUEST_ID for event in events)
    clarification = events[1]
    assert clarification["code"] == expected_code
    assert clarification["message"]
    assert len(clarification["options"]) >= 2
    assert clarification["interpreted_scope"]["workspace_id"] == WORKSPACE
    assert clarification["warnings"] == ["No SQL or model call was made."]
    assert harness.service.sql_calls == before_sql
    assert harness.service.model_calls == before_model
    assert not any(event["type"] in {"evidence", "token"} for event in events)


def test_unsupported_fact_abstains_without_substituting_a_nearby_field(
    harness: Harness,
):
    response = _post(
        harness,
        "/api/bridgette/stream",
        ABSTAIN_REQUEST_ID,
        "What color was the bridge originally painted",
    )

    assert response.status_code == 200
    events = _events(response)
    assert [event["type"] for event in events] == [
        "accepted",
        "abstention",
        "done",
    ]
    assert all(event["request_id"] == ABSTAIN_REQUEST_ID for event in events)
    abstention = events[1]
    assert abstention["code"] == "unsupported_fact"
    assert abstention["requested_fact"] == "original paint color"
    assert abstention["nearest_fields_not_substituted"] == [
        "superstructure_material",
        "protective_system",
    ]
    assert abstention["warnings"] == ["No nearby field was used as a proxy."]
    assert harness.service.sql_calls == 0
    assert harness.service.model_calls == 0
    assert not any(event["type"] in {"evidence", "token"} for event in events)


def test_bounded_result_download_is_owner_scoped_hash_addressed_and_no_store(
    harness: Harness,
):
    response = _post(
        harness,
        "/api/bridgette/stream",
        DOWNLOAD_REQUEST_ID,
        "List the condition table",
    )
    evidence = next(
        event for event in _events(response) if event["type"] == "evidence"
    )
    metadata = evidence["result_download"]

    owner = harness.client.get(
        metadata["href"],
        headers=harness.headers(DOWNLOAD_REQUEST_ID),
    )
    assert owner.status_code == 200, owner.text
    assert owner.content == RESULT_BYTES
    assert owner.headers["content-type"].startswith("text/csv")
    assert owner.headers["cache-control"] == "no-store"
    assert owner.headers["x-content-type-options"] == "nosniff"
    assert owner.headers["x-request-id"] == DOWNLOAD_REQUEST_ID
    assert owner.headers["x-content-sha256"] == RESULT_SHA256
    assert f'filename="{metadata["filename"]}"' in owner.headers[
        "content-disposition"
    ]
    assert len(owner.content) == metadata["byte_count"]
    assert hashlib.sha256(owner.content).hexdigest() == metadata["sha256"]

    cross_user = harness.client.get(
        metadata["href"],
        headers=harness.headers(DOWNLOAD_REQUEST_ID, email="other@example.com"),
    )
    missing = harness.client.get(
        "/api/bridgette/requests/90909090-9090-4090-8090-909090909090/result",
        headers=harness.headers(
            "90909090-9090-4090-8090-909090909090",
        ),
    )
    malformed = harness.client.get(
        "/api/bridgette/requests/not-a-uuid/result",
        headers=harness.headers(FULL_REQUEST_ID),
    )
    assert [cross_user.status_code, missing.status_code, malformed.status_code] == [
        404,
        404,
        404,
    ]
    assert cross_user.json() == missing.json() == malformed.json()
