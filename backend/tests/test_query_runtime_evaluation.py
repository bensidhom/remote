from __future__ import annotations

import json
from pathlib import Path
import socket

import pytest

from app.services.query.runtime_evaluation import (
    DEFAULT_RUNTIME_FIXTURE_PATH,
    RUNTIME_RECEIPT_SCHEMA_VERSION,
    RUNTIME_SUITE_VERSION,
    RuntimeBenchmarkError,
    calculate_runtime_fixture_hash,
    load_runtime_fixture,
    run_runtime_benchmark,
)
from scripts.run_bridgette_runtime_benchmark import main as runtime_cli


EXPECTED_CASES = {
    "revision.exact_snapshot",
    "current.per_bridge_latest",
    "trend.annual_inventory",
    "county.names_ranking_ties",
    "county.null_is_reported",
    "age.snapshot_not_today",
    "condition.culvert_only",
    "features.nhs_carried_highway",
    "routes.reviewed_join",
    "data.prompt_injection_is_text",
}

EXPECTED_PROBES = {
    "hostile.plan_sql_field": "unknown_plan_field",
    "hostile.drop_sql": "forbidden_statement",
    "hostile.excessive_joins": "join_limit_exceeded",
    "hostile.recursive_cte": "recursive_cte",
    "sandbox.row_cap": "row_cap_enforced",
    "sandbox.byte_cap": "query_byte_limit_exceeded",
    "sandbox.vm_cap": "query_vm_budget_exceeded",
    "sandbox.deadline_cap": "query_deadline_exceeded",
    "sandbox.request_owned_cancellation": "request_owned_cancellation",
    "sandbox.concurrent_users": "concurrent_scope_isolated",
    "evidence.stale_generation": "stale_data_generation_rejected",
    "evidence.revision_identity": "revision_and_generation_bound",
}


def _fixture_payload() -> dict:
    return json.loads(DEFAULT_RUNTIME_FIXTURE_PATH.read_text(encoding="utf-8"))


def _write_fixture(path: Path, payload: dict, *, rehash: bool) -> Path:
    if rehash:
        payload["fixture_hash"] = calculate_runtime_fixture_hash(payload)
    path.write_text(
        json.dumps(payload, allow_nan=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def test_checked_in_runtime_gate_executes_every_case_and_security_control(
    test_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_network(*_args, **_kwargs):
        raise AssertionError("runtime benchmark must never access the network")

    monkeypatch.setattr(socket, "create_connection", no_network)
    receipt = run_runtime_benchmark(work_directory=test_workspace)

    assert receipt.release_passed is True
    assert receipt.case_pass_rate == 1.0
    assert receipt.security_pass_rate == 1.0
    assert receipt.passed_cases == receipt.total_cases == len(EXPECTED_CASES)
    assert receipt.passed_security_probes == receipt.total_security_probes == len(
        EXPECTED_PROBES
    )
    assert {case.case_id for case in receipt.cases} == EXPECTED_CASES
    assert all(case.passed for case in receipt.cases)
    assert all(case.evidence_coverage == 1.0 for case in receipt.cases)
    assert all(case.row_count > 0 for case in receipt.cases)
    assert all(case.serialized_bytes > 0 for case in receipt.cases)
    assert all(len(case.question_sha256) == 64 for case in receipt.cases)
    assert all(len(case.plan_hash or "") == 64 for case in receipt.cases)
    assert all(len(case.result_sha256 or "") == 64 for case in receipt.cases)
    assert all(len(case.answer_sha256 or "") == 64 for case in receipt.cases)
    assert {
        probe.probe_id: probe.code for probe in receipt.security_probes
    } == EXPECTED_PROBES
    assert list(test_workspace.iterdir()) == []


def test_receipt_is_versioned_and_truthfully_excludes_provider_evaluation(
    test_workspace: Path,
) -> None:
    receipt = run_runtime_benchmark(work_directory=test_workspace).public_dict()

    assert receipt["receipt_schema_version"] == RUNTIME_RECEIPT_SCHEMA_VERSION
    assert receipt["suite_version"] == RUNTIME_SUITE_VERSION
    assert len(receipt["fixture_hash"]) == 64
    assert len(receipt["catalog_hash"]) == 64
    assert len(receipt["metric_registry_hash"]) == 64
    assert receipt["provider_evaluation"]["executed"] is False
    assert receipt["provider_evaluation"]["release_gate"] is False
    assert "separate optional evaluation" in receipt["provider_evaluation"]["limitation"]
    json.dumps(receipt, allow_nan=False)


def test_fixture_hash_and_strict_schema_fail_closed(tmp_path: Path) -> None:
    payload = _fixture_payload()
    payload["stress_row_count"] += 1
    tampered = _write_fixture(tmp_path / "tampered.json", payload, rehash=False)

    with pytest.raises(RuntimeBenchmarkError) as raised:
        load_runtime_fixture(tampered)
    assert raised.value.code == "fixture_hash_mismatch"

    payload = _fixture_payload()
    payload["unexpected"] = "field"
    strict = _write_fixture(tmp_path / "strict.json", payload, rehash=True)
    with pytest.raises(RuntimeBenchmarkError) as raised:
        load_runtime_fixture(strict)
    assert raised.value.code == "unknown_fixture_field"


def test_wrong_golden_result_produces_a_failed_release_receipt(tmp_path: Path) -> None:
    payload = _fixture_payload()
    payload["cases"][0]["expected_rows"][0][1] = 1999
    fixture_path = _write_fixture(tmp_path / "regression.json", payload, rehash=True)

    receipt = run_runtime_benchmark(fixture_path, work_directory=tmp_path)

    failed = next(
        case for case in receipt.cases if case.case_id == "revision.exact_snapshot"
    )
    assert failed.passed is False
    assert failed.code == "result_mismatch"
    assert receipt.release_passed is False
    assert receipt.case_pass_rate < 1.0


def test_cli_emits_the_machine_readable_receipt_and_removes_database(
    test_workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = runtime_cli(["--work-directory", str(test_workspace)])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["release_passed"] is True
    assert output["receipt_schema_version"] == RUNTIME_RECEIPT_SCHEMA_VERSION
    assert output["total_cases"] == len(EXPECTED_CASES)
    assert output["total_security_probes"] == len(EXPECTED_PROBES)
    assert list(test_workspace.iterdir()) == []
