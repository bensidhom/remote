from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from app.services.point_cloud.evaluation import (
    DEFAULT_MANIFEST,
    EvaluationObservation,
    EvaluationVersions,
    PointCloudEvaluationError,
    evaluate_point_cloud_observations,
    load_evaluation_bundle,
    load_evaluation_receipt,
    load_observations,
    require_current_release_receipt,
    receipt_matches_versions,
)


def _versions(**overrides) -> EvaluationVersions:
    values = {
        "code_revision": "test-revision",
        "parser_version": "parser-v1",
        "geometry_version": "geometry-v1",
        "fusion_version": "fusion-v1",
        "measurement_version": "measurement-v1",
        "prompt_version": "prompt-v1",
        "provider": "test-provider",
        "model": "test-multimodal-model",
        "context_builder_version": "context-v1",
    }
    values.update(overrides)
    return EvaluationVersions(**values)


def _passing_observations(bundle):
    return tuple(
        EvaluationObservation(
            case_id=case.case_id,
            numeric_output=False,
            retained_restriction_ids=case.protected_restriction_ids,
            measurable=False if case.require_no_numeric_output else None,
            provider_calls=0,
        )
        for case in bundle.cases
    )


def test_bundled_manifest_is_hash_sealed_and_truthfully_limited():
    bundle = load_evaluation_bundle()
    assert bundle.claim_level == "synthetic_safety_contract_only"
    assert len(bundle.cases) == 8
    assert len(bundle.manifest_sha256) == 64
    assert {receipt["role"] for receipt in bundle.artifact_receipts} == {
        "synthetic_safety_contract",
        "threshold_and_approval_policy",
    }
    assert any("No case contains registered survey control" in item for item in bundle.limitations)
    assert all(case.ground_truth_kind == "synthetic_contract" for case in bundle.cases)


def test_manifest_rejects_one_byte_artifact_tamper(tmp_path: Path):
    root = DEFAULT_MANIFEST.parent
    for path in root.glob("*.json"):
        (tmp_path / path.name).write_bytes(path.read_bytes())
    target = tmp_path / "safety_contract_v1.json"
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(PointCloudEvaluationError) as caught:
        load_evaluation_bundle(tmp_path / "manifest_v1.json")
    assert caught.value.code == "evaluation_artifact_hash_mismatch"


def test_duplicate_and_nonfinite_observation_json_fail_closed(tmp_path: Path):
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema_version":"bridgeguard-point-cloud-observations-v1",'
        '"schema_version":"bridgeguard-point-cloud-observations-v1","observations":[]}',
        encoding="utf-8",
    )
    with pytest.raises(PointCloudEvaluationError) as caught:
        load_observations(duplicate)
    assert caught.value.code == "evaluation_json_duplicate_key"
    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text(
        '{"schema_version":"bridgeguard-point-cloud-observations-v1",'
        '"observations":[{"case_id":"x","numeric_output":false,'
        '"retained_restriction_ids":[],"latency_ms":NaN}]}',
        encoding="utf-8",
    )
    with pytest.raises(PointCloudEvaluationError) as caught:
        load_observations(nonfinite)
    assert caught.value.code == "evaluation_json_nonfinite"


def test_safety_contract_can_pass_without_falsely_claiming_accuracy_or_release():
    bundle = load_evaluation_bundle()
    receipt = evaluate_point_cloud_observations(
        bundle,
        _passing_observations(bundle),
        _versions(),
        evaluated_at="2026-08-14T20:00:00Z",
    )
    assert receipt.safety_contract_passed is True
    assert receipt.empirical_accuracy_passed is False
    assert receipt.release_passed is False
    assert receipt.metrics["false_numeric_mandatory_abstention_count"] == 0
    assert receipt.metrics["protected_restriction_retention"] == 1.0
    assert receipt.metrics["empirical_clearance_case_count"] == 0
    assert set(receipt.blockers) >= {
        "empirical_physical_bridge_splits_missing",
        "registered_survey_ground_truth_missing",
        "domain_accuracy_thresholds_unapproved",
        "domain_owner_approval_missing",
        "empirical_clearance_observations_missing",
    }
    public = receipt.to_dict()
    assert public["official_validation"] is False
    assert public["submission_ready"] is False


def test_false_numeric_and_removed_restriction_fail_safety_gate():
    bundle = load_evaluation_bundle()
    observations = list(_passing_observations(bundle))
    abstention_index = next(
        index for index, case in enumerate(bundle.cases) if case.require_no_numeric_output
    )
    observations[abstention_index] = replace(
        observations[abstention_index], numeric_output=True, measurable=True
    )
    protection_index = next(
        index for index, case in enumerate(bundle.cases) if case.protected_restriction_ids
    )
    observations[protection_index] = replace(
        observations[protection_index], retained_restriction_ids=()
    )
    receipt = evaluate_point_cloud_observations(
        bundle,
        observations,
        _versions(),
        evaluated_at="2026-08-14T20:00:00Z",
    )
    assert receipt.safety_contract_passed is False
    assert receipt.metrics["false_numeric_mandatory_abstention_count"] == 1
    assert receipt.metrics["false_measurable_count"] == 1
    assert receipt.metrics["protected_restriction_retention"] < 1.0
    assert "mandatory_safety_threshold_failed" in receipt.blockers


def test_observation_coverage_is_exact_and_unknown_fields_are_rejected(tmp_path: Path):
    bundle = load_evaluation_bundle()
    with pytest.raises(PointCloudEvaluationError) as caught:
        evaluate_point_cloud_observations(
            bundle,
            _passing_observations(bundle)[:-1],
            _versions(),
        )
    assert caught.value.code == "evaluation_observation_coverage_mismatch"
    path = tmp_path / "unknown.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "bridgeguard-point-cloud-observations-v1",
                "observations": [
                    {
                        "case_id": "x",
                        "numeric_output": False,
                        "retained_restriction_ids": [],
                        "raw_path": "C:/secret",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PointCloudEvaluationError) as caught:
        load_observations(path)
    assert caught.value.code == "evaluation_observation_unknown_field"


def test_receipt_is_deterministic_and_invalidated_by_any_bound_version_change():
    bundle = load_evaluation_bundle()
    observations = _passing_observations(bundle)
    versions = _versions()
    first = evaluate_point_cloud_observations(
        bundle, observations, versions, evaluated_at="2026-08-14T20:00:00Z"
    )
    second = evaluate_point_cloud_observations(
        bundle, tuple(reversed(observations)), versions, evaluated_at="2026-08-14T20:00:00Z"
    )
    assert first.receipt_id == second.receipt_id
    assert first.canonical_json() == second.canonical_json()
    assert receipt_matches_versions(first, bundle, versions) is True
    assert receipt_matches_versions(first, bundle, _versions(model="changed-model")) is False
    assert receipt_matches_versions(first, bundle, _versions(prompt_version="changed-prompt")) is False
    assert receipt_matches_versions(first, bundle, _versions(parser_version="changed-parser")) is False
    assert receipt_matches_versions(first, bundle, _versions(context_builder_version="changed-context")) is False


def test_receipt_contains_no_rows_paths_prompts_or_source_content():
    bundle = load_evaluation_bundle()
    receipt = evaluate_point_cloud_observations(
        bundle,
        _passing_observations(bundle),
        _versions(),
        evaluated_at="2026-08-14T20:00:00Z",
    )
    serialized = receipt.canonical_json().lower()
    for forbidden in ("local_path", "filepath", "prompt_text", "source_bytes", "result_rows"):
        assert forbidden not in serialized
    assert len(receipt.observation_sha256) == 64
    assert len(receipt.receipt_id) == 64


def test_receipt_loader_detects_tamper_and_release_check_stays_blocked(tmp_path: Path):
    bundle = load_evaluation_bundle()
    versions = _versions()
    receipt = evaluate_point_cloud_observations(
        bundle,
        _passing_observations(bundle),
        versions,
        evaluated_at="2026-08-14T20:00:00Z",
    )
    path = tmp_path / "receipt.json"
    path.write_text(receipt.canonical_json(), encoding="utf-8")
    assert load_evaluation_receipt(path).receipt_id == receipt.receipt_id
    with pytest.raises(PointCloudEvaluationError) as caught:
        require_current_release_receipt(path, bundle=bundle, versions=versions)
    assert caught.value.code == "point_cloud_evaluation_release_blocked"

    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["metrics"]["case_count"] = 999
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(PointCloudEvaluationError) as caught:
        load_evaluation_receipt(path)
    assert caught.value.code == "evaluation_receipt_invalid"
