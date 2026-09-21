from __future__ import annotations

from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).parents[2]
COMPLEX_BANK = (
    ROOT / "backend" / "app" / "data" / "query" / "bridgette_complex_question_bank_v1.json"
)


def _script_module(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_BUILDER = _script_module("build_bridgette_complex_question_bank")
_LIVE = _script_module("run_bridgette_live_evaluation")
build_bank = _BUILDER.build_bank
DEFAULT_BANKS = _LIVE.DEFAULT_BANKS
_parse_events = _LIVE._parse_events
_score_case = _LIVE._score_case
_validated_endpoint = _LIVE._validated_endpoint
_bank_receipts = _LIVE._bank_receipts
load_cases = _LIVE.load_cases


def _stable_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def test_complex_bank_is_reproducible_and_hash_verified() -> None:
    checked_in = json.loads(COMPLEX_BANK.read_text(encoding="utf-8"))
    generated = build_bank()

    assert checked_in == generated
    claimed = checked_in["bank_sha256"]
    hash_input = dict(checked_in)
    del hash_input["bank_sha256"]
    assert hashlib.sha256(_stable_json(hash_input).encode("utf-8")).hexdigest() == claimed


def test_complex_bank_has_600_unique_sequential_cases() -> None:
    payload = json.loads(COMPLEX_BANK.read_text(encoding="utf-8"))
    cases = payload["cases"]

    assert len(cases) == 600
    assert [case["id"] for case in cases] == [
        f"complex.{index:03d}" for index in range(1, 601)
    ]
    assert len({case["question"] for case in cases}) == 600
    assert Counter(case["category"] for case in cases) == payload["category_counts"]
    assert Counter(case["expected_behavior"] for case in cases) == payload["behavior_counts"]
    assert payload["behavior_counts"] == {
        "answer": 560,
        "clarification": 20,
        "abstention": 20,
    }


def test_complex_case_contract_is_complete_and_bounded() -> None:
    payload = json.loads(COMPLEX_BANK.read_text(encoding="utf-8"))
    for case in payload["cases"]:
        assert case["question"].strip()
        assert len(case["question"]) <= 2_000
        assert case["request_scope"]["time_mode"] in {"latest", "exact", "trend"}
        semantics = case["expected_semantics"]
        assert set(semantics) == {"must_include", "any_of", "must_not_include"}
        assert all(isinstance(value, str) and value for value in semantics["must_include"])
        assert all(
            isinstance(group, list) and group and all(isinstance(value, str) for value in group)
            for group in semantics["any_of"]
        )
        assert case["review_note"].strip()
        if case["expected_behavior"] == "answer":
            assert semantics["must_include"] or semantics["any_of"]


def test_default_live_suite_contains_715_cases() -> None:
    paths = tuple(ROOT / path for path in DEFAULT_BANKS)
    cases = load_cases(paths)
    receipts = _bank_receipts(paths)

    assert len(cases) == 715
    assert len({case.case_id for case in cases}) == 715
    assert sum(case.bank.endswith("question-bank-v1") for case in cases) == 100
    assert sum(case.bank.endswith("examples-v2") for case in cases) == 15
    assert sum(case.bank.endswith("semantic-bank-v1") for case in cases) == 600
    assert len(receipts) == 3
    assert all(len(item["file_sha256"]) == 64 for item in receipts)
    assert receipts[-1]["declared_bank_sha256"] == json.loads(
        COMPLEX_BANK.read_text(encoding="utf-8")
    )["bank_sha256"]


def test_live_stream_parser_and_scorer_use_server_evidence_not_question() -> None:
    case = next(
        case
        for case in load_cases((COMPLEX_BANK,))
        if case.case_id == "complex.001"
    )
    request_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    raw = (
        json.dumps({"type": "accepted", "request_id": request_id})
        + "\n"
        + json.dumps(
            {
                "type": "evidence",
                "request_id": request_id,
                "answer_mode": "verified_semantic_plan",
                "query_plan": {
                    "canonical_plan": {
                        "metric_ids": ["inventory.total_deck_area"],
                        "filters": [
                            {"field_id": "owner", "operator": "eq", "value": "S01"}
                        ],
                    }
                },
                "scope": {"time_mode": "latest", "year": 2025},
                "columns": ["metric__inventory__total_deck_area"],
                "answer_verification": {"status": "verified"},
            }
        )
        + "\n"
        + json.dumps({"type": "done", "request_id": request_id})
        + "\n"
    ).encode("utf-8")
    events = _parse_events(raw, request_id=request_id)
    result = _score_case(case, events)

    assert result["passed"] is True
    assert result["observed_behavior"] == "answer"

    empty_raw = (
        json.dumps({"type": "accepted", "request_id": request_id})
        + "\n"
        + json.dumps({"type": "error", "request_id": request_id, "detail": "failed"})
        + "\n"
    ).encode("utf-8")
    empty_events = _parse_events(empty_raw, request_id=request_id)
    failed = _score_case(case, empty_events)
    assert failed["passed"] is False
    assert "owner" in failed["missing_terms"]


def test_live_scorer_accepts_required_clarification_without_query_evidence() -> None:
    case = next(
        case
        for case in load_cases((COMPLEX_BANK,))
        if case.expected_behavior == "clarification"
    )
    request_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    raw = (
        json.dumps(
            {
                "type": "clarification",
                "request_id": request_id,
                "message": "Do you mean owner or maintenance_responsibility?",
            }
        )
        + "\n"
        + json.dumps({"type": "done", "request_id": request_id})
        + "\n"
    ).encode("utf-8")

    assert _score_case(case, _parse_events(raw, request_id=request_id))["passed"] is True


def test_live_endpoint_is_absolute_and_never_records_url_credentials() -> None:
    endpoint, origin = _validated_endpoint("https://bridge.example.test/review/")

    assert endpoint == "https://bridge.example.test/review/api/bridgette/stream"
    assert origin == "https://bridge.example.test/review"
    with pytest.raises(ValueError, match="credentials"):
        _validated_endpoint("https://user:secret@bridge.example.test")
    with pytest.raises(ValueError, match="absolute"):
        _validated_endpoint("localhost:8000")


def test_live_answer_requires_verified_database_evidence_and_matching_scope() -> None:
    case = next(
        case
        for case in load_cases((COMPLEX_BANK,))
        if case.case_id == "complex.001"
    )
    request_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    events = (
        {
            "type": "evidence",
            "request_id": request_id,
            "scope": {"time_mode": "latest", "year": 2025},
            "query_plan": {
                "canonical_plan": {
                    "metric_ids": ["inventory.total_deck_area"],
                    "filters": [
                        {"field_id": "owner", "operator": "eq", "value": "S01"}
                    ],
                }
            },
            "answer_verification": {"status": "advisory_unverified"},
        },
        {"type": "done", "request_id": request_id},
    )

    score = _score_case(case, events)
    assert score["semantic_passed"] is True
    assert score["evidence_verified"] is False
    assert score["scope_passed"] is True
    assert score["passed"] is False


def test_live_scorer_distinguishes_bounded_provisional_database_answers() -> None:
    case = next(
        case
        for case in load_cases((COMPLEX_BANK,))
        if case.case_id == "complex.001"
    )
    request_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    events = (
        {
            "type": "evidence",
            "request_id": request_id,
            "answer_mode": "guarded_raw_sql_fallback",
            "answer_status": "provisional",
            "query_language": "guarded_raw_sql",
            "scope": {"time_mode": "latest_loaded_inventory", "year": 2025},
            "sql": (
                "SELECT SUM(calculated_deck_area) AS total_deck_area FROM bridges "
                "WHERE owner = 'S01'"
            ),
        },
        {"type": "done", "request_id": request_id},
    )

    score = _score_case(case, events)
    assert score["passed"] is True
    assert score["database_evidence_passed"] is True
    assert score["evidence_verified"] is False
    assert score["scope_passed"] is True
