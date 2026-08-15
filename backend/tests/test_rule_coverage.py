from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.conversion.rule_coverage import (
    CoverageClassification,
    CoverageIntent,
    CoverageLayer,
    OfficialRuleCoverageRegistry,
    RuleCoverageDeclaration,
    RuleKind,
    ValidationOutcome,
    ValidationRuleEvaluation,
    reconcile_rule_coverage,
)
from app.services.conversion.transition_values import (
    TransitionCategory,
    TransitionStatus,
    TransitionValue,
)
from app.standards.fhwa_registry import (
    ArtifactState,
    load_rule_bundle_registry,
)


PROFILE_ID = "submission_2026_test"
BUNDLE_ID = "submission_2026_test_bundle"
ARTIFACT_ID = "reviewed_rule_workbook"


def _write_artifact_registry(
    workspace: Path,
    *,
    artifact_state: ArtifactState = ArtifactState.VENDORED_VERIFIED,
    include_primary_in_bundle: bool = True,
    include_extra_unverified: bool = False,
    enabled: bool = True,
):
    artifact_bytes = b"reviewed FHWA rule rows\nrow-1\n"
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    artifact_path = workspace / "reviewed-rules.bin"
    artifact_path.write_bytes(artifact_bytes)

    if artifact_state is ArtifactState.VENDORED_VERIFIED:
        primary = {
            "id": ARTIFACT_ID,
            "title": "Reviewed rule workbook fixture",
            "role": "validation_rules",
            "authority": "FHWA test fixture",
            "legal_status": "operational",
            "state": artifact_state.value,
            "source_url": "https://www.fhwa.dot.gov/bridge/snbi.cfm",
            "published_on": "2025-12-01",
            "published_label": None,
            "updated_on": None,
            "media_type": "application/octet-stream",
            "local_path": artifact_path.name,
            "sha256": artifact_sha256,
            "notes": ["Fixture bytes are verified by the production loader."],
        }
    else:
        primary = {
            "id": ARTIFACT_ID,
            "title": "Unverified remote rule workbook fixture",
            "role": "validation_rules",
            "authority": "FHWA test fixture",
            "legal_status": "operational",
            "state": artifact_state.value,
            "source_url": "https://www.fhwa.dot.gov/bridge/snbi.cfm",
            "published_on": "2025-12-01",
            "published_label": None,
            "updated_on": None,
            "media_type": "application/octet-stream",
            "local_path": None,
            "sha256": None,
            "notes": ["Remote metadata is not executable evidence."],
        }

    artifacts = [primary]
    artifact_ids = [ARTIFACT_ID] if include_primary_in_bundle else []
    if include_extra_unverified:
        artifacts.append(
            {
                "id": "unverified_schema",
                "title": "Unverified schema fixture",
                "role": "json_schema",
                "authority": "FHWA test fixture",
                "legal_status": "operational",
                "state": "remote_unverified",
                "source_url": "https://www.fhwa.dot.gov/bridge/snbi.cfm",
                "published_on": None,
                "published_label": None,
                "updated_on": None,
                "media_type": "application/schema+json",
                "local_path": None,
                "sha256": None,
                "notes": [],
            }
        )
        artifact_ids.append("unverified_schema")

    payload = {
        "format_version": 1,
        "registry_id": "coverage_test_registry",
        "observed_on": "2026-08-13",
        "artifacts": artifacts,
        "bundles": [
            {
                "id": BUNDLE_ID,
                "title": "Submission coverage test bundle",
                "profile_kind": "submission",
                "legal_status": "operational_submission",
                "compliance_use": (
                    "submission_support_draft_external_verification_required"
                ),
                "implementation_status": "registry_only",
                "enabled": enabled,
                "effective_submission_years": [2026],
                "artifact_ids": artifact_ids,
                "inherits": [],
                "disabled_reason": None if enabled else "Fixture is disabled.",
                "notes": [],
            }
        ],
    }
    registry_path = workspace / "registry.json"
    registry_path.write_text(json.dumps(payload), encoding="utf-8")
    return load_rule_bundle_registry(registry_path), artifact_sha256


def _declaration(
    rule_id: str,
    *,
    kind: RuleKind = RuleKind.TRANSITION,
    intent: CoverageIntent = CoverageIntent.IMPLEMENTED_TESTED,
    patterns: tuple[str, ...] = ("/BID01",),
    digest: str | None,
    artifact_id: str | None = ARTIFACT_ID,
) -> RuleCoverageDeclaration:
    official = intent in {
        CoverageIntent.IMPLEMENTED_TESTED,
        CoverageIntent.INTENTIONALLY_OMITTED_NON_REPORTABLE,
    }
    return RuleCoverageDeclaration(
        rule_id=rule_id,
        rule_kind=kind,
        intent=intent,
        pointer_patterns=patterns,
        artifact_id=artifact_id if official else None,
        artifact_sha256=digest if official else None,
        source_locator=("validation.xlsx!Rules!A2:H2" if official else None),
        test_ids=(f"test_{rule_id}",) if intent is CoverageIntent.IMPLEMENTED_TESTED else (),
        reason=(
            "No reportable legacy source value exists for this rule."
            if intent is CoverageIntent.INTENTIONALLY_OMITTED_NON_REPORTABLE
            else "BridgeGuard application-derived field."
            if intent is CoverageIntent.INTERNAL_DERIVED
            else "Awaiting reviewed implementation."
            if intent is CoverageIntent.PENDING_IMPLEMENTATION
            else ""
        ),
    )


def _coverage_registry(
    *declarations: RuleCoverageDeclaration,
    profile_id: str = PROFILE_ID,
    bundle_id: str = BUNDLE_ID,
) -> OfficialRuleCoverageRegistry:
    return OfficialRuleCoverageRegistry(
        registry_version="coverage_rules_2026_v1",
        profile_id=profile_id,
        rule_bundle_id=bundle_id,
        declarations=tuple(declarations),
    )


def _transition(
    rule_id: str,
    *,
    target_tag: str = "BID01",
    value="000123",
    category: TransitionCategory = TransitionCategory.DIRECT,
) -> TransitionValue:
    return TransitionValue(
        value=value,
        target_tag=target_tag,
        rule_id=rule_id,
        category=category,
        source_record_ids=("source-record-1",),
        status=(
            TransitionStatus.NULL_SOURCE
            if value is None
            else TransitionStatus.POPULATED
        ),
    )


def _reconcile(
    *,
    document,
    lineage,
    registry,
    artifact_registry,
    review=None,
    evaluations=(),
    profile_id: str = PROFILE_ID,
):
    return reconcile_rule_coverage(
        document,
        profile_id=profile_id,
        transition_lineage=lineage,
        review_lineage=review or {},
        validation_evaluations=evaluations,
        coverage_registry=registry,
        artifact_registry=artifact_registry,
    )


def test_verified_pinned_rules_reconcile_all_layers_but_never_mark_submission_ready(
    test_workspace: Path,
):
    artifacts, digest = _write_artifact_registry(test_workspace)
    transition_rule = _declaration("fhwa_bid01_direct", digest=digest)
    internal_rule = _declaration(
        "bridgeguard_internal_score",
        intent=CoverageIntent.INTERNAL_DERIVED,
        patterns=("/Derived/BG16",),
        digest=None,
        artifact_id=None,
    )
    validation_rule = _declaration(
        "fhwa_bid01_required",
        kind=RuleKind.VALIDATION,
        patterns=("/BID01",),
        digest=digest,
    )
    registry = _coverage_registry(transition_rule, internal_rule, validation_rule)

    report = _reconcile(
        document={"BID01": "000123", "Derived": {"BG16": 7}},
        lineage={
            "/BID01": _transition("fhwa_bid01_direct"),
            "/Derived/BG16": _transition(
                "bridgeguard_internal_score",
                target_tag="BG16",
                value=7,
                category=TransitionCategory.DERIVED,
            ),
        },
        evaluations=(
            ValidationRuleEvaluation(
                "fhwa_bid01_required",
                ValidationOutcome.PASSED,
                ("/BID01",),
            ),
        ),
        registry=registry,
        artifact_registry=artifacts,
    )

    assert [row.classification for row in report.canonical_leaf_records] == [
        CoverageClassification.COVERED,
        CoverageClassification.INTERNAL_DERIVED,
    ]
    assert report.validation_records[0].classification is CoverageClassification.COVERED
    assert report.artifact_evidence[0].verified is True
    assert report.blocker_codes == ()
    assert report.coverage_complete is True
    assert report.submission_ready is False


@pytest.mark.parametrize(
    ("declared_digest", "artifact_state", "expected_reason"),
    [
        (None, ArtifactState.VENDORED_VERIFIED, "does not pin"),
        ("0" * 64, ArtifactState.VENDORED_VERIFIED, "does not match"),
        ("actual", ArtifactState.REMOTE_UNVERIFIED, "not marked"),
    ],
)
def test_unpinned_mismatched_and_remote_artifacts_fail_closed(
    test_workspace: Path,
    declared_digest,
    artifact_state,
    expected_reason,
):
    artifacts, actual_digest = _write_artifact_registry(
        test_workspace, artifact_state=artifact_state
    )
    digest = actual_digest if declared_digest == "actual" else declared_digest
    registry = _coverage_registry(
        _declaration("fhwa_bid01_direct", digest=digest)
    )

    report = _reconcile(
        document={"BID01": "000123"},
        lineage={"/BID01": _transition("fhwa_bid01_direct")},
        registry=registry,
        artifact_registry=artifacts,
    )

    assert report.canonical_leaf_records[0].classification is (
        CoverageClassification.ARTIFACT_UNVERIFIED
    )
    assert report.artifact_evidence[0].verified is False
    assert expected_reason in report.artifact_evidence[0].reason
    assert "artifact_unverified" in report.blocker_codes
    assert report.coverage_complete is False
    assert report.submission_ready is False


def test_every_declaration_must_pin_the_shared_artifact(test_workspace: Path):
    artifacts, digest = _write_artifact_registry(test_workspace)
    registry = _coverage_registry(
        _declaration("rule_with_hash", digest=digest),
        _declaration(
            "rule_without_hash",
            patterns=("/BW01",),
            digest=None,
        ),
    )
    report = _reconcile(
        document={"BID01": "000123", "BW01": 1939},
        lineage={
            "/BID01": _transition("rule_with_hash"),
            "/BW01": _transition(
                "rule_without_hash", target_tag="BW01", value=1939
            ),
        },
        registry=registry,
        artifact_registry=artifacts,
    )
    assert report.artifact_evidence[0].verified is False
    assert all(
        row.classification is CoverageClassification.ARTIFACT_UNVERIFIED
        for row in report.canonical_leaf_records
    )


def test_artifact_must_belong_to_selected_bundle(test_workspace: Path):
    artifacts, digest = _write_artifact_registry(
        test_workspace, include_primary_in_bundle=False
    )
    registry = _coverage_registry(
        _declaration("fhwa_bid01_direct", digest=digest)
    )
    report = _reconcile(
        document={"BID01": "000123"},
        lineage={"/BID01": _transition("fhwa_bid01_direct")},
        registry=registry,
        artifact_registry=artifacts,
    )
    assert report.artifact_evidence[0].verified is False
    assert "not governed" in report.artifact_evidence[0].reason


def test_unverified_required_bundle_artifact_is_a_registry_blocker(
    test_workspace: Path,
):
    artifacts, digest = _write_artifact_registry(
        test_workspace, include_extra_unverified=True
    )
    registry = _coverage_registry(
        _declaration("fhwa_bid01_direct", digest=digest)
    )
    report = _reconcile(
        document={"BID01": "000123"},
        lineage={"/BID01": _transition("fhwa_bid01_direct")},
        registry=registry,
        artifact_registry=artifacts,
    )
    assert report.canonical_leaf_records[0].classification is CoverageClassification.COVERED
    assert "rule_bundle_contains_unverified_artifacts" in report.blocker_codes
    assert any(
        row.artifact_id == "unverified_schema"
        and row.classification is CoverageClassification.ARTIFACT_UNVERIFIED
        for row in report.registry_records
    )
    assert report.coverage_complete is False


def test_missing_lineage_unknown_rule_value_mismatch_and_target_mismatch_are_uncovered(
    test_workspace: Path,
):
    artifacts, digest = _write_artifact_registry(test_workspace)
    registry = _coverage_registry(
        _declaration("declared_rule", digest=digest),
        _declaration("value_rule", patterns=("/BW01",), digest=digest),
        _declaration("target_rule", patterns=("/BL01",), digest=digest),
    )
    report = _reconcile(
        document={"BID01": "000123", "BW01": 1939, "BL01": 34, "BC01": "7"},
        lineage={
            "/BID01": _transition("not_declared"),
            "/BW01": _transition("value_rule", target_tag="BW01", value=1977),
            "/BL01": _transition("target_rule", target_tag="WRONG", value=34),
        },
        registry=registry,
        artifact_registry=artifacts,
    )
    reasons = {
        row.pointer: row.reasons[0] for row in report.canonical_leaf_records
    }
    assert reasons == {
        "/BC01": "missing_transition_lineage",
        "/BID01": "transition_rule_not_declared",
        "/BL01": "transition_target_tag_does_not_match_pointer",
        "/BW01": "canonical_value_does_not_match_transition_lineage",
    }
    assert "unknown_uncovered" in report.blocker_codes


def test_internal_derived_requires_matching_registry_declaration(test_workspace: Path):
    artifacts, digest = _write_artifact_registry(test_workspace)
    undeclared = _coverage_registry(
        _declaration("some_official_rule", digest=digest)
    )
    report = _reconcile(
        document={"BG16": 7},
        lineage={
            "/BG16": _transition(
                "internal_score",
                target_tag="BG16",
                value=7,
                category=TransitionCategory.DERIVED,
            )
        },
        registry=undeclared,
        artifact_registry=artifacts,
    )
    assert report.canonical_leaf_records[0].classification is (
        CoverageClassification.UNKNOWN_UNCOVERED
    )

    wrong_intent = _coverage_registry(
        _declaration(
            "internal_score", patterns=("/BG16",), digest=digest
        )
    )
    report = _reconcile(
        document={"BG16": 7},
        lineage={
            "/BG16": _transition(
                "internal_score",
                target_tag="BG16",
                value=7,
                category=TransitionCategory.DERIVED,
            )
        },
        registry=wrong_intent,
        artifact_registry=artifacts,
    )
    assert report.canonical_leaf_records[0].reasons == (
        "internal_transition_not_declared_internal_derived",
    )


def test_verified_nonreportable_review_is_explicit_but_emitting_it_is_uncovered(
    test_workspace: Path,
):
    artifacts, digest = _write_artifact_registry(test_workspace)
    declaration = _declaration(
        "fhwa_bg01_nonreportable",
        intent=CoverageIntent.INTENTIONALLY_OMITTED_NON_REPORTABLE,
        patterns=("/BG01",),
        digest=digest,
    )
    registry = _coverage_registry(declaration)
    review_value = _transition(
        "fhwa_bg01_nonreportable",
        target_tag="BG01",
        value=None,
        category=TransitionCategory.NO_TRANSITION,
    )
    report = _reconcile(
        document={},
        lineage={},
        review={"/BG01": review_value},
        registry=registry,
        artifact_registry=artifacts,
    )
    assert report.review_records[0].classification is (
        CoverageClassification.INTENTIONALLY_OMITTED_NON_REPORTABLE
    )
    assert report.coverage_complete is True

    report = _reconcile(
        document={"BG01": 20.1},
        lineage={
            "/BG01": _transition(
                "fhwa_bg01_nonreportable",
                target_tag="BG01",
                value=Decimal("20.1"),
            )
        },
        registry=registry,
        artifact_registry=artifacts,
    )
    assert report.canonical_leaf_records[0].classification is (
        CoverageClassification.UNKNOWN_UNCOVERED
    )
    assert report.canonical_leaf_records[0].reasons == (
        "non_reportable_rule_emitted_a_canonical_value",
    )


def test_orphan_lineage_is_always_uncovered(test_workspace: Path):
    artifacts, digest = _write_artifact_registry(test_workspace)
    registry = _coverage_registry(
        _declaration("fhwa_bid01_direct", digest=digest)
    )
    report = _reconcile(
        document={},
        lineage={"/BID01": _transition("fhwa_bid01_direct")},
        registry=registry,
        artifact_registry=artifacts,
    )
    assert report.orphan_lineage_records[0].layer is CoverageLayer.ORPHAN_LINEAGE
    assert report.orphan_lineage_records[0].classification is (
        CoverageClassification.UNKNOWN_UNCOVERED
    )


def test_same_pointer_in_emitted_and_review_lineage_is_a_conflict(
    test_workspace: Path,
):
    artifacts, digest = _write_artifact_registry(test_workspace)
    registry = _coverage_registry(
        _declaration("fhwa_bid01_direct", digest=digest)
    )
    report = _reconcile(
        document={"BID01": "000123"},
        lineage={"/BID01": _transition("fhwa_bid01_direct")},
        review={
            "/BID01": _transition(
                "fhwa_bid01_direct", value=None
            )
        },
        registry=registry,
        artifact_registry=artifacts,
    )
    assert "conflicting_lineage" in report.blocker_codes
    assert any(
        row.reasons == ("pointer_present_in_transition_and_review_lineage",)
        for row in report.registry_records
    )
    assert report.coverage_complete is False


@pytest.mark.parametrize(
    ("evaluation", "classification", "reason"),
    [
        (None, CoverageClassification.UNKNOWN_UNCOVERED, "validation_rule_not_evaluated"),
        (
            ValidationRuleEvaluation(
                "fhwa_validate_bid01",
                ValidationOutcome.NOT_EVALUATED,
                (),
                reason="Execution was unavailable.",
            ),
            CoverageClassification.UNKNOWN_UNCOVERED,
            "validation_rule_not_evaluated",
        ),
        (
            ValidationRuleEvaluation(
                "fhwa_validate_bid01",
                ValidationOutcome.NOT_APPLICABLE,
                (),
                reason="Record subtype is outside this rule.",
            ),
            CoverageClassification.INTENTIONALLY_OMITTED_NON_REPORTABLE,
            "validation_rule_not_applicable",
        ),
        (
            ValidationRuleEvaluation(
                "fhwa_validate_bid01",
                ValidationOutcome.ISSUES,
                ("/BID01",),
                issue_count=1,
            ),
            CoverageClassification.COVERED,
            "validation_rule_executed_with_issues",
        ),
        (
            ValidationRuleEvaluation(
                "fhwa_validate_bid01",
                ValidationOutcome.PASSED,
                ("/BW01",),
            ),
            CoverageClassification.UNKNOWN_UNCOVERED,
            "validation_pointer_outside_declared_patterns",
        ),
    ],
)
def test_validation_rule_execution_states_are_reconciled(
    test_workspace: Path,
    evaluation,
    classification,
    reason,
):
    artifacts, digest = _write_artifact_registry(test_workspace)
    registry = _coverage_registry(
        _declaration(
            "fhwa_validate_bid01",
            kind=RuleKind.VALIDATION,
            digest=digest,
        )
    )
    evaluations = () if evaluation is None else (evaluation,)
    report = _reconcile(
        document={},
        lineage={},
        evaluations=evaluations,
        registry=registry,
        artifact_registry=artifacts,
    )
    assert report.validation_records[0].classification is classification
    assert report.validation_records[0].reasons[0] == reason


def test_unknown_validation_evaluation_is_not_credited(test_workspace: Path):
    artifacts, digest = _write_artifact_registry(test_workspace)
    registry = _coverage_registry(
        _declaration(
            "declared_validation", kind=RuleKind.VALIDATION, digest=digest
        )
    )
    report = _reconcile(
        document={},
        lineage={},
        evaluations=(
            ValidationRuleEvaluation(
                "undeclared_validation", ValidationOutcome.PASSED, ("/BID01",)
            ),
        ),
        registry=registry,
        artifact_registry=artifacts,
    )
    by_id = {row.rule_id: row for row in report.validation_records}
    assert by_id["declared_validation"].classification is (
        CoverageClassification.UNKNOWN_UNCOVERED
    )
    assert by_id["undeclared_validation"].reasons == (
        "validation_rule_not_declared",
    )


def test_declared_omitted_validation_cannot_execute_as_covered(
    test_workspace: Path,
):
    artifacts, digest = _write_artifact_registry(test_workspace)
    registry = _coverage_registry(
        _declaration(
            "fhwa_nonreportable_validation",
            kind=RuleKind.VALIDATION,
            intent=CoverageIntent.INTENTIONALLY_OMITTED_NON_REPORTABLE,
            digest=digest,
        )
    )
    report = _reconcile(
        document={},
        lineage={},
        evaluations=(
            ValidationRuleEvaluation(
                "fhwa_nonreportable_validation",
                ValidationOutcome.PASSED,
                ("/BID01",),
            ),
        ),
        registry=registry,
        artifact_registry=artifacts,
    )
    assert report.validation_records[0].classification is (
        CoverageClassification.UNKNOWN_UNCOVERED
    )
    assert report.validation_records[0].reasons == (
        "intentionally_omitted_validation_rule_was_executed",
    )


def test_pending_validation_is_uncovered_even_without_artifact_evidence(
    test_workspace: Path,
):
    artifacts, _ = _write_artifact_registry(test_workspace)
    registry = _coverage_registry(
        _declaration(
            "pending_validation",
            kind=RuleKind.VALIDATION,
            intent=CoverageIntent.PENDING_IMPLEMENTATION,
            digest=None,
            artifact_id=None,
        )
    )
    report = _reconcile(
        document={},
        lineage={},
        registry=registry,
        artifact_registry=artifacts,
    )
    assert report.validation_records[0].classification is (
        CoverageClassification.UNKNOWN_UNCOVERED
    )
    assert report.validation_records[0].reasons == (
        "validation_rule_pending_implementation",
    )


def test_wildcard_patterns_and_rfc6901_escaping_match_exact_leaf_paths(
    test_workspace: Path,
):
    artifacts, digest = _write_artifact_registry(test_workspace)
    registry = _coverage_registry(
        _declaration(
            "escaped_feature_rule",
            patterns=("/Features/*/a~1b",),
            digest=digest,
        )
    )
    report = _reconcile(
        document={"Features": [{"a/b": "yes"}]},
        lineage={
            "/Features/0/a~1b": _transition(
                "escaped_feature_rule", target_tag="a/b", value="yes"
            )
        },
        registry=registry,
        artifact_registry=artifacts,
    )
    assert report.canonical_leaf_records[0].classification is CoverageClassification.COVERED


def test_empty_rfc6901_pointer_can_identify_document_wide_validation(
    test_workspace: Path,
):
    artifacts, digest = _write_artifact_registry(test_workspace)
    registry = _coverage_registry(
        _declaration(
            "fhwa_document_rule",
            kind=RuleKind.VALIDATION,
            patterns=("",),
            digest=digest,
        )
    )
    report = _reconcile(
        document={},
        lineage={},
        evaluations=(
            ValidationRuleEvaluation(
                "fhwa_document_rule", ValidationOutcome.PASSED, ("",)
            ),
        ),
        registry=registry,
        artifact_registry=artifacts,
    )
    assert report.validation_records[0].classification is CoverageClassification.COVERED


def test_profile_bundle_and_registry_availability_fail_closed(test_workspace: Path):
    artifacts, digest = _write_artifact_registry(test_workspace, enabled=False)
    registry = _coverage_registry(
        _declaration("fhwa_bid01_direct", digest=digest)
    )
    report = _reconcile(
        document={"BID01": "000123"},
        lineage={"/BID01": _transition("fhwa_bid01_direct")},
        registry=registry,
        artifact_registry=artifacts,
        profile_id="different_profile",
    )
    assert "coverage_profile_mismatch" in report.blocker_codes
    assert "rule_bundle_disabled" in report.blocker_codes
    assert report.submission_ready is False

    report = _reconcile(
        document={"BID01": "000123"},
        lineage={"/BID01": _transition("fhwa_bid01_direct")},
        registry=registry,
        artifact_registry=None,
    )
    assert "artifact_registry_unavailable" in report.blocker_codes
    assert "artifact_unverified" in report.blocker_codes


def test_registry_is_immutable_unique_and_hash_is_order_independent():
    digest = "a" * 64
    first = _declaration(
        "rule_a", patterns=("/B", "/A"), digest=digest
    )
    second = _declaration(
        "rule_b", patterns=("/C",), digest=digest
    )
    left = _coverage_registry(first, second)
    right = _coverage_registry(second, first)
    assert first.pointer_patterns == ("/A", "/B")
    assert left.sha256 == right.sha256
    with pytest.raises(FrozenInstanceError):
        left.profile_id = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="duplicate"):
        _coverage_registry(first, first)


@pytest.mark.parametrize(
    "kwargs, match",
    [
        (
            {
                "rule_id": "rule",
                "rule_kind": RuleKind.TRANSITION,
                "intent": CoverageIntent.IMPLEMENTED_TESTED,
                "pointer_patterns": ("/BID01",),
                "artifact_id": ARTIFACT_ID,
                "artifact_sha256": "a" * 64,
                "source_locator": "sheet row 1",
                "test_ids": (),
            },
            "test_ids",
        ),
        (
            {
                "rule_id": "rule",
                "rule_kind": RuleKind.TRANSITION,
                "intent": CoverageIntent.INTERNAL_DERIVED,
                "pointer_patterns": ("/BG16",),
                "artifact_id": ARTIFACT_ID,
                "reason": "Internal.",
            },
            "may not claim",
        ),
        (
            {
                "rule_id": "rule",
                "rule_kind": RuleKind.TRANSITION,
                "intent": CoverageIntent.PENDING_IMPLEMENTATION,
                "pointer_patterns": ("not-a-pointer",),
                "reason": "Pending.",
            },
            "RFC 6901",
        ),
        (
            {
                "rule_id": "rule",
                "rule_kind": RuleKind.TRANSITION,
                "intent": CoverageIntent.PENDING_IMPLEMENTATION,
                "pointer_patterns": ("/bad~2escape",),
                "reason": "Pending.",
            },
            "escape",
        ),
    ],
)
def test_declaration_contract_rejects_unsupported_claims(kwargs, match):
    with pytest.raises(ValueError, match=match):
        RuleCoverageDeclaration(**kwargs)


def test_validation_evaluation_contract_is_strict():
    with pytest.raises(ValueError, match="at least one evaluated pointer"):
        ValidationRuleEvaluation("rule", ValidationOutcome.PASSED, ())
    with pytest.raises(ValueError, match="greater than zero"):
        ValidationRuleEvaluation(
            "rule", ValidationOutcome.ISSUES, ("/BID01",), issue_count=0
        )
    with pytest.raises(ValueError, match="requires a reason"):
        ValidationRuleEvaluation("rule", ValidationOutcome.NOT_EVALUATED, ())
    with pytest.raises(ValueError, match="unique"):
        ValidationRuleEvaluation(
            "rule", ValidationOutcome.PASSED, ("/BID01", "/BID01")
        )
