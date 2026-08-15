from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path

import pytest

from app.services.query.evaluation import (
    DEFAULT_FIXTURE_PATH,
    EVALUATION_SCHEMA_VERSION,
    EVALUATION_SUITE_VERSION,
    EvalThresholds,
    EvaluationFixtureError,
    calculate_fixture_hash,
    load_evaluation_fixture,
    run_evaluation,
)
from app.services.query.interpretation import InterpretationDecision
from app.services.query.plans import BridgeQueryPlan


EXPECTED_CATEGORIES = {
    "ambiguity",
    "condition",
    "count",
    "exact",
    "latest",
    "list",
    "nhs",
    "prompt_injection",
    "snbi_tag",
    "tenant_scope",
    "traffic",
    "trend",
    "unsupported",
}
SERVER_SCOPE_KEYS = {
    "workspace",
    "workspace_id",
    "state",
    "state_code",
    "bridge",
    "bridge_number",
    "data_year",
    "year",
    "start_year",
    "end_year",
    "revision_id",
    "snapshot_revision_id",
}


def _fixture_payload() -> dict:
    return json.loads(DEFAULT_FIXTURE_PATH.read_text(encoding="utf-8"))


def _write_fixture(path: Path, payload: dict, *, rehash: bool = True) -> Path:
    if rehash:
        payload["fixture_hash"] = calculate_fixture_hash(payload)
    path.write_text(
        json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _case(payload: dict, case_id: str) -> dict:
    return next(item for item in payload["cases"] if item["id"] == case_id)


def _result(report, case_id: str):
    return next(item for item in report.results if item.case_id == case_id)


def test_checked_in_suite_passes_every_release_threshold() -> None:
    report = run_evaluation()

    assert report.schema_version == EVALUATION_SCHEMA_VERSION
    assert report.suite_version == EVALUATION_SUITE_VERSION
    assert report.total_cases == 16
    assert report.passed_cases == report.total_cases
    assert report.overall_pass_rate == 1.0
    assert report.release_passed is True
    assert set(report.category_pass_rates) == EXPECTED_CATEGORIES
    assert all(rate == 1.0 for rate in report.category_pass_rates.values())
    assert all(result.passed for result in report.results)


def test_fixture_and_reports_are_frozen() -> None:
    fixture = load_evaluation_fixture()
    report = run_evaluation()

    with pytest.raises(FrozenInstanceError):
        fixture.cases[0].question = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        fixture.cases[0].plan["limit"] = 999  # type: ignore[index]
    with pytest.raises(TypeError):
        report.category_pass_rates["exact"] = 0.0  # type: ignore[index]


def test_fixture_hash_detects_content_tampering(tmp_path: Path) -> None:
    payload = _fixture_payload()
    payload["cases"][0]["question"] += " tampered"
    path = _write_fixture(tmp_path / "tampered.json", payload, rehash=False)

    with pytest.raises(EvaluationFixtureError) as raised:
        load_evaluation_fixture(path)
    assert raised.value.code == "fixture_hash_mismatch"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("schema_version", 2, "unsupported_schema_version"),
        ("suite_version", "bridgeguard-query-eval-v2", "unsupported_suite_version"),
    ],
)
def test_fixture_versions_fail_closed(
    tmp_path: Path, field: str, value: object, code: str
) -> None:
    payload = _fixture_payload()
    payload[field] = value
    path = _write_fixture(tmp_path / f"bad-{field}.json", payload)

    with pytest.raises(EvaluationFixtureError) as raised:
        load_evaluation_fixture(path)
    assert raised.value.code == code


def test_every_proceed_case_has_a_full_canonical_plan_and_external_scope() -> None:
    fixture = load_evaluation_fixture()
    proceed = [
        case
        for case in fixture.cases
        if case.expected_decision is InterpretationDecision.PROCEED
    ]

    assert proceed
    for case in proceed:
        assert case.plan is not None
        assert case.server_scope is not None
        assert not SERVER_SCOPE_KEYS.intersection(case.plan)
        assert set(case.plan["time_scope"]) == {"mode"}
        plan = BridgeQueryPlan.from_payload(case.plan)
        assert plan.canonical_payload() == json.loads(plan.canonical_json)


@pytest.mark.parametrize(
    ("case_id", "mutation", "expected_code"),
    [
        (
            "traffic.average_aadt",
            lambda plan: plan["metric_ids"].__setitem__(0, "inventory.not_a_metric"),
            "unknown_metric",
        ),
        (
            "snbi_tag.bc01",
            lambda plan: plan["selected_fields"].__setitem__(1, "not_a_field"),
            "unknown_field",
        ),
    ],
)
def test_metric_and_field_identifiers_are_exact(
    tmp_path: Path, case_id: str, mutation, expected_code: str
) -> None:
    payload = _fixture_payload()
    mutation(_case(payload, case_id)["plan"])
    path = _write_fixture(tmp_path / f"{case_id}.json", payload)

    report = run_evaluation(path)

    failed = _result(report, case_id)
    assert failed.passed is False
    assert failed.code == expected_code
    assert report.release_passed is False


def test_adversarial_injection_and_tenant_scope_cases_pass_boundaries() -> None:
    report = run_evaluation()

    prompt_results = [
        result for result in report.results if result.category == "prompt_injection"
    ]
    tenant = _result(report, "tenant_scope.override_attempt")
    assert len(prompt_results) == 2
    assert all(result.passed for result in prompt_results)
    assert tenant.passed is True
    assert tenant.plan_hash is not None


def test_threshold_gate_fails_when_one_expected_interpretation_regresses(
    tmp_path: Path,
) -> None:
    payload = _fixture_payload()
    _case(payload, "ambiguity.current")["expected_code"] = "wrong_code"
    path = _write_fixture(tmp_path / "regression.json", payload)

    report = run_evaluation(path)

    failed = _result(report, "ambiguity.current")
    assert failed.passed is False
    assert failed.code == "interpretation_code_mismatch"
    assert report.overall_pass_rate < report.thresholds.minimum_overall_pass_rate
    assert report.category_pass_rates["ambiguity"] < (
        report.thresholds.minimum_category_pass_rate
    )
    assert report.release_passed is False


def test_threshold_override_is_explicit_and_frozen() -> None:
    thresholds = EvalThresholds(
        minimum_overall_pass_rate=0.95,
        minimum_category_pass_rate=0.90,
    )
    report = run_evaluation(thresholds=thresholds)

    assert report.thresholds is thresholds
    assert report.release_passed is True
    with pytest.raises(FrozenInstanceError):
        thresholds.minimum_overall_pass_rate = 0.0  # type: ignore[misc]


def test_answer_claim_must_equal_its_cited_evidence(tmp_path: Path) -> None:
    payload = _fixture_payload()
    answer = _case(payload, "count.published_bridges")["answer"]
    answer["narrative"]["answer_markdown"] = (
        "The selected published inventory contains 126 bridges."
    )
    answer["narrative"]["claims"][0]["text"] = (
        "The published bridge count is 126."
    )
    answer["narrative"]["claims"][0]["value"] = 126
    path = _write_fixture(tmp_path / "unsupported-claim.json", payload)

    report = run_evaluation(path)

    failed = _result(report, "count.published_bridges")
    assert failed.passed is False
    assert failed.stage.value == "answer"
    assert failed.code == "claim_verification_failed"
    assert report.release_passed is False
