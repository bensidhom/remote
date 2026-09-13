from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from app.services.query.metrics import DEFAULT_METRIC_REGISTRY
from app.services.query.semantic import build_semantic_catalog


QUESTION_BANK_PATH = (
    Path(__file__).parents[1]
    / "app"
    / "data"
    / "query"
    / "bridgette_snbi_question_bank_v1.json"
)
DECK_AREA_QUESTION_BANK_PATH = (
    Path(__file__).parents[1]
    / "app"
    / "data"
    / "query"
    / "bridgette_deck_area_question_bank_v1.json"
)


def _load_question_bank() -> dict[str, object]:
    return json.loads(QUESTION_BANK_PATH.read_text(encoding="utf-8"))


def _load_deck_area_question_bank() -> dict[str, object]:
    return json.loads(DECK_AREA_QUESTION_BANK_PATH.read_text(encoding="utf-8"))


def test_snbi_question_bank_has_100_unique_sequential_cases() -> None:
    payload = _load_question_bank()
    cases = payload["cases"]

    assert len(cases) == 100
    assert len({case["id"] for case in cases}) == 100
    assert len({case["question"] for case in cases}) == 100
    assert [case["id"] for case in cases] == [
        f"snbi.{index:03d}" for index in range(1, 101)
    ]


def test_snbi_question_bank_behavior_distribution_is_intentional() -> None:
    payload = _load_question_bank()
    cases = payload["cases"]
    actual_counts = Counter(case["expected_behavior"] for case in cases)

    assert dict(actual_counts) == payload["expected_behavior_counts"]
    assert actual_counts == {"answer": 81, "clarification": 14, "abstention": 5}


def test_deck_area_question_bank_covers_nine_answer_phrasings() -> None:
    cases = _load_deck_area_question_bank()["cases"]

    assert [case["id"] for case in cases] == [
        f"deck.{index:03d}" for index in range(1, 10)
    ]
    assert len({case["question"] for case in cases}) == 9
    assert {case["expected_behavior"] for case in cases} == {"answer"}
    assert {case["expected_code"] for case in cases} == {"ready_for_planning"}


def test_snbi_question_bank_contract_fields_are_complete() -> None:
    payload = _load_question_bank()
    allowed_behaviors = {"answer", "clarification", "abstention"}
    allowed_time_modes = {"latest", "exact", "trend"}

    for case in payload["cases"]:
        assert case["category"]
        assert case["question"]
        assert case["expected_behavior"] in allowed_behaviors
        assert case["expected_code"]
        assert case["request_scope"]["time_mode"] in allowed_time_modes
        assert isinstance(case["target_fields"], list)
        assert isinstance(case["snbi_items"], list)

        if case["expected_behavior"] == "answer":
            assert case["target_resource"]
            assert case["target_fields"]
            assert case["snbi_items"]
            assert case["expected_code"] == "ready_for_planning"
        if case["expected_behavior"] == "clarification":
            assert case["expected_code"] != "ready_for_planning"
        if case["expected_behavior"] == "abstention":
            assert case["expected_code"] == "unsupported_fact"


def test_uhpc_case_preserves_fhwa_errata_boundary() -> None:
    payload = _load_question_bank()
    uhpc_case = next(case for case in payload["cases"] if case["id"] == "snbi.081")

    assert uhpc_case["expected_behavior"] == "clarification"
    assert uhpc_case["expected_code"] == "query_needs_more_context"
    assert "C06" in uhpc_case["question"]
    assert "informational" in uhpc_case["gap"]


def test_answer_targets_resolve_to_reviewed_catalog_and_active_metrics() -> None:
    catalog = build_semantic_catalog(
        database_identity="question-bank-test",
        workspace_id="question-bank-test",
        schema_generation=1,
        data_generation=1,
    )
    resources = catalog.resources_by_id
    metrics = {metric.metric_id: metric for metric in DEFAULT_METRIC_REGISTRY.metrics}

    cases = [
        *_load_question_bank()["cases"],
        *_load_deck_area_question_bank()["cases"],
    ]
    for case in cases:
        target_resource_id = case["target_resource"]
        if target_resource_id is not None:
            assert target_resource_id in resources, case["id"]

        if case["expected_behavior"] != "answer":
            continue

        target_resource = resources[target_resource_id]
        target_columns = {column.field_id for column in target_resource.columns}
        for field in case["target_fields"]:
            if "." not in field:
                assert field in target_columns, (case["id"], field)
                continue

            qualified_resource_id, qualified_field = field.rsplit(".", 1)
            assert qualified_resource_id in resources, (case["id"], field)
            assert qualified_field in {
                column.field_id for column in resources[qualified_resource_id].columns
            }, (case["id"], field)

        target_metric_id = case.get("target_metric")
        if target_metric_id is not None:
            assert target_metric_id in metrics, (case["id"], target_metric_id)
            assert metrics[target_metric_id].status.value == "active", (
                case["id"],
                target_metric_id,
            )
