from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, dataclass
from types import SimpleNamespace

import pytest

from app.standards import load_rule_bundle_registry
from app.services.conversion.validation_profiles import (
    BRIDGEGUARD_VALIDATION_LABEL,
    NOT_OFFICIAL_DISCLAIMER,
    VALIDATION_LAYER_ORDER,
    CoverageRegistry,
    CoverageRow,
    CoverageStatus,
    DecisionReason,
    EvidenceRef,
    LayerResult,
    PublishDecision,
    PublishPolicy,
    ValidationIssue,
    ValidationLayer,
    ValidationProfile,
    ValidationProfileKind,
    ValidationSeverity,
    evaluate_validation,
    validation_report_json,
)


DOCUMENT_BYTES = b'{"BID01":"TEST-1"}'
DOCUMENT_SHA256 = hashlib.sha256(DOCUMENT_BYTES).hexdigest()
ARTIFACT_SHA256 = hashlib.sha256(b"pinned FHWA artifact").hexdigest()
EXPECTED_RULES = ("FHWA-RULE-1", "FHWA-RULE-2", "FHWA-RULE-3")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _profile(
    *,
    kind=ValidationProfileKind.SUBMISSION,
    policy=PublishPolicy.PUBLISH_GATE,
    publication_enabled=True,
    require_executable_bundle=True,
    expected_rule_ids=EXPECTED_RULES,
):
    return ValidationProfile(
        profile_id=(
            "research_historical_import"
            if kind is ValidationProfileKind.RESEARCH_IMPORT
            else "submission_2026"
        ),
        kind=kind,
        rule_bundle_id="fhwa_submission_2026",
        publish_policy=policy,
        expected_rule_ids=tuple(expected_rule_ids),
        publication_enabled=publication_enabled,
        require_executable_bundle=require_executable_bundle,
    )


def _coverage(profile=None, *, rows=None, profile_id=None):
    profile = profile or _profile()
    return CoverageRegistry(
        profile_id=profile_id or profile.profile_id,
        registry_version="coverage_2026_v1",
        rows=tuple(
            rows
            if rows is not None
            else (
                CoverageRow(
                    "FHWA-RULE-1",
                    CoverageStatus.IMPLEMENTED_TESTED,
                    "validation.xlsx row 1",
                    test_ids=("test_rule_1",),
                ),
                CoverageRow(
                    "FHWA-RULE-2",
                    CoverageStatus.JSON_SCHEMA,
                    "schema.json pointer /properties/BID01",
                ),
                CoverageRow(
                    "FHWA-RULE-3",
                    CoverageStatus.NOT_APPLICABLE,
                    "validation.xlsx row 3",
                    reason="Rule is outside this bridge record profile.",
                ),
            )
        ),
    )


class FakeRegistry:
    def __init__(
        self,
        *,
        state="vendored_verified",
        sha256=ARTIFACT_SHA256,
        unverified=(),
        implementation_status="executable_tested",
        selection_error=None,
    ):
        self.registry_id = "test_fhwa_registry"
        self._artifact = SimpleNamespace(
            id="snbi_schema_v04",
            state=state,
            sha256=sha256,
        )
        self._unverified = tuple(unverified)
        self._implementation_status = implementation_status
        self._selection_error = selection_error
        self.select_calls = []

    def bundle(self, bundle_id):
        return SimpleNamespace(id=bundle_id)

    def artifacts_for(self, bundle_id):
        return (self._artifact,)

    def unverified_artifact_ids(self, bundle_id):
        return self._unverified

    def select(self, bundle_id, *, require_verified_artifacts=True):
        self.select_calls.append((bundle_id, require_verified_artifacts))
        if self._selection_error is not None:
            raise self._selection_error
        return SimpleNamespace(
            id=bundle_id,
            implementation_status=self._implementation_status,
        )


class FakeCanonicalAdapter:
    document_id = "revision-test-1"
    document_sha256 = DOCUMENT_SHA256

    def __init__(self, mutate=None, issues=None, exception_layer=None):
        self.calls = []
        self.mutate = mutate
        self.issues = issues or {}
        self.exception_layer = exception_layer

    def validate_layer(self, layer, profile):
        self.calls.append(layer)
        if layer is self.exception_layer:
            raise RuntimeError(r"secret at C:\production\snbi_bridges.db")
        evidence = EvidenceRef(
            kind="validation_run",
            evidence_id=f"{layer.value}_evidence",
            sha256=_digest(f"{layer.value}:{self.document_sha256}"),
            attributes=(("layer", layer.value),),
        )
        result = LayerResult(
            layer=layer,
            profile_id=profile.profile_id,
            input_sha256=self.document_sha256,
            validator_id=f"{layer.value}_validator",
            validator_version="validator_v1",
            completed=True,
            issues=tuple(self.issues.get(layer, ())),
            evidence=(evidence,),
        )
        return self.mutate(layer, result) if self.mutate else result


def _evaluate(
    *, adapter=None, profile=None, registry=None, coverage=None
):
    profile = profile or _profile()
    adapter = adapter or FakeCanonicalAdapter()
    registry = registry or FakeRegistry()
    coverage = coverage or _coverage(profile)
    return evaluate_validation(
        adapter,
        profile=profile,
        standards_registry=registry,
        coverage_registry=coverage,
    ), adapter, registry


def _blocker_codes(report):
    return {item.code for item in report.decision.blockers}


def _warning_codes(report):
    return {item.code for item in report.decision.warnings}


def test_validated_layers_run_in_fixed_order_and_allow_internal_publication():
    report, adapter, registry = _evaluate()

    assert adapter.calls == list(VALIDATION_LAYER_ORDER)
    assert tuple(item.layer for item in report.layers) == VALIDATION_LAYER_ORDER
    assert all(item.completed and item.passed for item in report.layers)
    assert report.decision.allowed is True
    assert report.decision.blockers == ()
    assert registry.select_calls == [("fhwa_submission_2026", True)]
    assert any(
        item.kind == "standards_artifact" for item in report.decision.evidence
    )
    assert any(
        item.kind == "coverage_registry" for item in report.decision.evidence
    )


def test_report_never_claims_official_fhwa_or_nbtis_validation():
    report, _adapter, _registry = _evaluate()
    payload = report.to_dict()

    assert report.official_validation is False
    assert report.decision.official_validation is False
    assert report.decision.validation_label == BRIDGEGUARD_VALIDATION_LABEL
    assert report.decision.disclaimer == NOT_OFFICIAL_DISCLAIMER
    assert payload["official_validation"] is False
    assert payload["decision"]["official_validation"] is False
    assert "not official FHWA or NBTIS validation" in payload["decision"][
        "disclaimer"
    ]
    with pytest.raises(TypeError):
        PublishDecision(
            allowed=True,
            blockers=(),
            warnings=(),
            evidence=(),
            official_validation=True,
        )


def test_validation_report_json_is_deterministic_and_contains_immutable_digests():
    report, _adapter, _registry = _evaluate()

    first = validation_report_json(report)
    second = validation_report_json(report)
    decoded = json.loads(first)

    assert first == second
    assert decoded["document_sha256"] == DOCUMENT_SHA256
    assert decoded["orchestrator_version"]
    assert len(decoded["layers"]) == 6
    assert all(
        len(item["sha256"]) == 64
        for item in decoded["decision"]["evidence"]
    )


@pytest.mark.parametrize(
    ("rows", "expected_code"),
    [
        (
            (
                CoverageRow(
                    "FHWA-RULE-1",
                    CoverageStatus.IMPLEMENTED_TESTED,
                    "row 1",
                    test_ids=("test_1",),
                ),
                CoverageRow(
                    "FHWA-RULE-2",
                    CoverageStatus.JSON_SCHEMA,
                    "schema",
                ),
            ),
            "missing_coverage_rows",
        ),
        (
            (
                *_coverage().rows,
                CoverageRow(
                    "UNKNOWN-RULE",
                    CoverageStatus.JSON_SCHEMA,
                    "unknown schema row",
                ),
            ),
            "unknown_coverage_rows",
        ),
        (
            (
                *_coverage().rows,
                CoverageRow(
                    "FHWA-RULE-1",
                    CoverageStatus.IMPLEMENTED_TESTED,
                    "duplicate row",
                    test_ids=("test_duplicate",),
                ),
            ),
            "duplicate_coverage_rows",
        ),
        (
            (
                CoverageRow(
                    "FHWA-RULE-1",
                    CoverageStatus.IMPLEMENTED_TESTED,
                    "row 1",
                    test_ids=("test_1",),
                ),
                CoverageRow(
                    "FHWA-RULE-2",
                    CoverageStatus.UNIMPLEMENTED,
                    "row 2",
                    reason="Executable rule has not been reviewed.",
                ),
                CoverageRow(
                    "FHWA-RULE-3",
                    CoverageStatus.NOT_APPLICABLE,
                    "row 3",
                    reason="Not applicable.",
                ),
            ),
            "unimplemented_official_rules",
        ),
    ],
)
def test_coverage_missing_unknown_duplicate_or_unimplemented_blocks_publish(
    rows, expected_code
):
    report, _adapter, _registry = _evaluate(
        coverage=_coverage(rows=rows)
    )

    assert report.decision.allowed is False
    assert expected_code in _blocker_codes(report)


def test_coverage_profile_mismatch_blocks_publish():
    report, _adapter, _registry = _evaluate(
        coverage=_coverage(profile_id="another_profile")
    )

    assert "coverage_profile_mismatch" in _blocker_codes(report)


@pytest.mark.parametrize(
    ("status", "kwargs", "message"),
    [
        ("unknown", {}, "coverage status"),
        (CoverageStatus.IMPLEMENTED_TESTED, {}, "test IDs"),
        (CoverageStatus.NOT_APPLICABLE, {}, "require a reason"),
        (CoverageStatus.FHWA_EXTERNAL, {}, "require a reason"),
        (CoverageStatus.UNIMPLEMENTED, {}, "require a reason"),
    ],
)
def test_coverage_rows_reject_unknown_or_incomplete_accounting(
    status, kwargs, message
):
    with pytest.raises(ValueError, match=message):
        CoverageRow("FHWA-RULE-1", status, "source row", **kwargs)


def test_external_coverage_is_accounted_but_visibly_requires_external_validation():
    rows = list(_coverage().rows)
    rows[2] = CoverageRow(
        "FHWA-RULE-3",
        CoverageStatus.FHWA_EXTERNAL,
        "NBTIS validation row 3",
        reason="NBTIS evaluates this rule outside BridgeGuard.",
    )
    report, _adapter, _registry = _evaluate(
        coverage=_coverage(rows=tuple(rows))
    )

    assert report.decision.allowed is True
    assert "coverage_requires_external_evaluation" in _warning_codes(report)
    assert "external_fhwa_validation_required" in _warning_codes(report)
    assert report.decision.official_validation is False


def test_unverified_registry_blocks_even_if_adapter_attempts_to_select_it():
    registry = FakeRegistry(
        state="remote_unverified",
        sha256=None,
        unverified=("snbi_schema_v04",),
    )
    report, _adapter, registry = _evaluate(registry=registry)

    assert report.decision.allowed is False
    assert "unverified_standards_artifacts" in _blocker_codes(report)
    assert registry.select_calls == [("fhwa_submission_2026", True)]
    assert not any(
        item.kind == "standards_artifact" for item in report.decision.evidence
    )


def test_registry_selection_failure_is_path_safe_and_fail_closed():
    registry = FakeRegistry(
        selection_error=RuntimeError(
            r"missing C:\production\private\schema.json"
        )
    )
    report, _adapter, _registry = _evaluate(registry=registry)
    serialized = validation_report_json(report)

    assert "standards_registry_verification_failed" in _blocker_codes(report)
    assert "production" not in serialized
    assert "private" not in serialized


def test_current_repository_registry_fails_closed_until_artifacts_are_vendored():
    profile = ValidationProfile(
        profile_id="submission_2026",
        kind=ValidationProfileKind.SUBMISSION,
        rule_bundle_id="submission_2026_v04_v1_1",
        publish_policy=PublishPolicy.PUBLISH_GATE,
        expected_rule_ids=EXPECTED_RULES,
        publication_enabled=True,
    )
    report, _adapter, _registry = _evaluate(
        profile=profile,
        registry=load_rule_bundle_registry(),
        coverage=_coverage(profile),
    )
    serialized = validation_report_json(report)

    assert report.decision.allowed is False
    assert "unverified_standards_artifacts" in _blocker_codes(report)
    assert "standards_registry_verification_failed" in _blocker_codes(report)
    assert "backend/data" not in serialized
    assert "C:\\" not in serialized


@pytest.mark.parametrize(
    "implementation_status",
    ["registry_only", "metadata_only", "unimplemented", None],
)
def test_nonexecutable_rule_bundle_blocks_publication(implementation_status):
    report, _adapter, _registry = _evaluate(
        registry=FakeRegistry(implementation_status=implementation_status)
    )

    assert "rule_bundle_not_executable" in _blocker_codes(report)


def test_research_profile_is_always_nonpublishable_even_when_every_layer_passes():
    profile = _profile(
        kind=ValidationProfileKind.RESEARCH_IMPORT,
        policy=PublishPolicy.ADVISORY,
        publication_enabled=False,
        expected_rule_ids=EXPECTED_RULES,
    )
    report, adapter, _registry = _evaluate(
        profile=profile,
        coverage=_coverage(profile),
    )

    assert adapter.calls == list(VALIDATION_LAYER_ORDER)
    assert report.decision.allowed is False
    assert "research_profile_non_publishable" in _blocker_codes(report)


def test_research_profile_cannot_be_constructed_with_publication_enabled():
    with pytest.raises(ValueError, match="research profiles"):
        _profile(
            kind=ValidationProfileKind.RESEARCH_IMPORT,
            publication_enabled=True,
        )


def test_publishable_profile_cannot_bypass_coverage_with_empty_expected_list():
    with pytest.raises(ValueError, match="expected rule IDs"):
        _profile(expected_rule_ids=())


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (
            lambda layer, result: (
                None if layer is ValidationLayer.PINNED_SCHEMA else result
            ),
            "invalid_layer_result",
        ),
        (
            lambda layer, result: (
                LayerResult(
                    layer=ValidationLayer.JSON_RESOURCE,
                    profile_id=result.profile_id,
                    input_sha256=result.input_sha256,
                    validator_id=result.validator_id,
                    validator_version=result.validator_version,
                    completed=result.completed,
                    issues=result.issues,
                    evidence=result.evidence,
                )
                if layer is ValidationLayer.PINNED_SCHEMA
                else result
            ),
            "layer_identity_mismatch",
        ),
        (
            lambda layer, result: (
                LayerResult(
                    layer=result.layer,
                    profile_id="different_profile",
                    input_sha256=result.input_sha256,
                    validator_id=result.validator_id,
                    validator_version=result.validator_version,
                    completed=result.completed,
                    issues=result.issues,
                    evidence=result.evidence,
                )
                if layer is ValidationLayer.PINNED_SCHEMA
                else result
            ),
            "layer_profile_mismatch",
        ),
        (
            lambda layer, result: (
                LayerResult(
                    layer=result.layer,
                    profile_id=result.profile_id,
                    input_sha256="0" * 64,
                    validator_id=result.validator_id,
                    validator_version=result.validator_version,
                    completed=result.completed,
                    issues=result.issues,
                    evidence=result.evidence,
                )
                if layer is ValidationLayer.PINNED_SCHEMA
                else result
            ),
            "stale_layer_result",
        ),
        (
            lambda layer, result: (
                LayerResult(
                    layer=result.layer,
                    profile_id=result.profile_id,
                    input_sha256=result.input_sha256,
                    validator_id=result.validator_id,
                    validator_version=result.validator_version,
                    completed=False,
                    issues=result.issues,
                    evidence=result.evidence,
                )
                if layer is ValidationLayer.PINNED_SCHEMA
                else result
            ),
            "layer_incomplete",
        ),
        (
            lambda layer, result: (
                LayerResult(
                    layer=result.layer,
                    profile_id=result.profile_id,
                    input_sha256=result.input_sha256,
                    validator_id=result.validator_id,
                    validator_version=result.validator_version,
                    completed=result.completed,
                    issues=result.issues,
                    evidence=(),
                )
                if layer is ValidationLayer.PINNED_SCHEMA
                else result
            ),
            "missing_layer_evidence",
        ),
    ],
)
def test_layer_result_bypass_attempts_fail_closed(mutation, expected_code):
    adapter = FakeCanonicalAdapter(mutate=mutation)
    report, adapter, _registry = _evaluate(adapter=adapter)

    assert adapter.calls == list(VALIDATION_LAYER_ORDER)
    assert report.decision.allowed is False
    assert expected_code in _blocker_codes(report)
    assert tuple(item.layer for item in report.layers) == VALIDATION_LAYER_ORDER


def test_validator_exception_does_not_skip_later_layers_or_leak_details():
    adapter = FakeCanonicalAdapter(exception_layer=ValidationLayer.PINNED_SCHEMA)
    report, adapter, _registry = _evaluate(adapter=adapter)
    serialized = validation_report_json(report)

    assert adapter.calls == list(VALIDATION_LAYER_ORDER)
    assert "validator_execution_failed" in _blocker_codes(report)
    assert "production" not in serialized
    assert "snbi_bridges" not in serialized


def test_issue_referencing_missing_layer_evidence_blocks_publish():
    issue = ValidationIssue(
        issue_id="schema-warning-1",
        rule_id="schema:warning",
        severity=ValidationSeverity.WARNING,
        message="Schema emitted a warning.",
        evidence_keys=("validation_run:missing-evidence",),
    )
    adapter = FakeCanonicalAdapter(
        issues={ValidationLayer.PINNED_SCHEMA: (issue,)}
    )
    report, _adapter, _registry = _evaluate(adapter=adapter)

    assert "missing_issue_evidence" in _blocker_codes(report)


def test_duplicate_issue_ids_across_layers_block_publish():
    issue_one = ValidationIssue(
        issue_id="duplicate-issue",
        rule_id="test:one",
        severity=ValidationSeverity.WARNING,
        message="First issue.",
    )
    issue_two = ValidationIssue(
        issue_id="duplicate-issue",
        rule_id="test:two",
        severity=ValidationSeverity.WARNING,
        message="Second issue.",
    )
    adapter = FakeCanonicalAdapter(
        issues={
            ValidationLayer.JSON_RESOURCE: (issue_one,),
            ValidationLayer.PINNED_SCHEMA: (issue_two,),
        }
    )
    report, _adapter, _registry = _evaluate(adapter=adapter)

    assert "duplicate_issue_id" in _blocker_codes(report)


@pytest.mark.parametrize(
    ("policy", "severity", "allowed"),
    [
        (PublishPolicy.PUBLISH_GATE, ValidationSeverity.ERROR, False),
        (PublishPolicy.REJECT_CRITICAL, ValidationSeverity.ERROR, True),
        (PublishPolicy.ADVISORY, ValidationSeverity.ERROR, True),
        (PublishPolicy.PUBLISH_GATE, ValidationSeverity.CRITICAL, False),
        (PublishPolicy.REJECT_CRITICAL, ValidationSeverity.CRITICAL, False),
        (PublishPolicy.ADVISORY, ValidationSeverity.CRITICAL, False),
        (PublishPolicy.PUBLISH_GATE, ValidationSeverity.WARNING, True),
    ],
)
def test_issue_severity_is_routed_by_profile_policy(policy, severity, allowed):
    issue = ValidationIssue(
        issue_id=f"policy-{policy.value}-{severity.value}",
        rule_id="test:policy",
        severity=severity,
        message="Policy-routing fixture.",
    )
    adapter = FakeCanonicalAdapter(
        issues={ValidationLayer.WORKSPACE_POLICY: (issue,)}
    )
    profile = _profile(policy=policy)
    report, _adapter, _registry = _evaluate(
        adapter=adapter,
        profile=profile,
        coverage=_coverage(profile),
    )

    assert report.decision.allowed is allowed
    if severity is ValidationSeverity.WARNING or (
        severity is ValidationSeverity.ERROR and policy is not PublishPolicy.PUBLISH_GATE
    ):
        assert "validation_warning" in _warning_codes(report) or (
            "validation_error" in _warning_codes(report)
        )


def test_validation_objects_and_decision_evidence_are_immutable():
    report, _adapter, _registry = _evaluate()

    with pytest.raises(FrozenInstanceError):
        report.document_id = "changed"
    with pytest.raises(FrozenInstanceError):
        report.layers[0].completed = False
    with pytest.raises(FrozenInstanceError):
        report.decision.evidence[0].sha256 = "0" * 64


def test_decision_constructor_rejects_allowed_with_blockers():
    blocker = DecisionReason("blocked", "Publication must be blocked.")
    with pytest.raises(ValueError, match="absence of blockers"):
        PublishDecision(
            allowed=True,
            blockers=(blocker,),
            warnings=(),
            evidence=(),
        )
