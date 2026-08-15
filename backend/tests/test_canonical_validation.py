from __future__ import annotations

import hashlib
import inspect
import json
from types import SimpleNamespace

import pytest

from app.services.conversion import canonical_validation as canonical_validation_module
from app.services.conversion.canonical_schema import normalize_canonical_document
from app.services.conversion.canonical_validation import (
    CANONICAL_VALIDATION_ADAPTER_VERSION,
    CanonicalDocumentValidationAdapter,
    ProjectionIntegritySnapshot,
    WorkspacePolicyContext,
    canonical_document_sha256,
)
from app.services.conversion.pinned_schema import (
    PinnedSchemaResult,
    SchemaExecutionStatus,
    SchemaValidationIssue,
    ValidationTarget,
)
from app.services.conversion.transition_engine import (
    TransitionCoverage,
    TransitionIssue,
    TransitionIssueSeverity,
)
from app.services.conversion.transition_values import (
    TransitionCategory,
    TransitionValue,
)
from app.services.conversion.validation_profiles import (
    CoverageRegistry,
    CoverageRow,
    CoverageStatus,
    PublishPolicy,
    ValidationLayer,
    ValidationProfile,
    ValidationProfileKind,
    ValidationSeverity,
    evaluate_validation,
)


RESEARCH_PROFILE = "research_historical_import"
SUBMISSION_PROFILE = "submission_2026"
_UNSET = object()


def _profile(
    *,
    profile_id: str = RESEARCH_PROFILE,
    kind: ValidationProfileKind = ValidationProfileKind.RESEARCH_IMPORT,
    publication_enabled: bool = False,
    expected_rule_ids: tuple[str, ...] = (),
) -> ValidationProfile:
    return ValidationProfile(
        profile_id=profile_id,
        kind=kind,
        rule_bundle_id="test_bundle_2026",
        publish_policy=PublishPolicy.PUBLISH_GATE,
        expected_rule_ids=expected_rule_ids,
        publication_enabled=publication_enabled,
    )


def _normalization(
    document=None,
    *,
    profile_id: str = RESEARCH_PROFILE,
):
    return normalize_canonical_document(
        document if document is not None else {"BID01": "B-1", "BL01": 34},
        profile_id=profile_id,
    )


def _workspace(
    *,
    profiles: tuple[str, ...] = (RESEARCH_PROFILE,),
    bridges: tuple[str, ...] = ("B-1",),
    states: tuple[str, ...] = ("34",),
    require_approval: bool = False,
    approval: str | None = None,
) -> WorkspacePolicyContext:
    return WorkspacePolicyContext(
        workspace_id="workspace-njdot-1",
        authorized_profile_ids=profiles,
        authorized_bridge_ids=bridges,
        authorized_state_codes=states,
        require_approval=require_approval,
        approval_evidence_id=approval,
    )


def _lineage(document=None):
    document = document or {"BID01": "B-1", "BL01": 34}
    return {
        f"/{tag}": TransitionValue(
            value=value,
            target_tag=tag,
            rule_id=f"test-direct-{tag.lower()}-v1",
            category=TransitionCategory.DIRECT,
            source_record_ids=("legacy-record-1",),
        )
        for tag, value in document.items()
    }


def _coverage(
    *,
    document_leaf_count: int = 2,
    lineage_leaf_count: int = 2,
    uncovered_leaf_count: int = 0,
    mapping_exception_count: int = 0,
    populated_temporary_count: int = 0,
) -> TransitionCoverage:
    return TransitionCoverage(
        document_leaf_count=document_leaf_count,
        lineage_leaf_count=lineage_leaf_count,
        internal_derived_leaf_count=0,
        uncovered_leaf_count=uncovered_leaf_count,
        status_counts={"populated": lineage_leaf_count},
        category_counts={"direct": lineage_leaf_count},
        source_record_count=lineage_leaf_count,
        populated_permanent_count=lineage_leaf_count,
        populated_temporary_count=populated_temporary_count,
        null_source_count=0,
        invalid_source_count=0,
        no_transition_count=0,
        assumed_generated_count=0,
        mapping_exception_count=mapping_exception_count,
        reviewed_verified_count=lineage_leaf_count,
    )


def _adapter(
    *,
    normalization=None,
    projection=None,
    workspace=None,
    lineage=_UNSET,
    issues=(),
    coverage=_UNSET,
    pinned_schema_result=None,
) -> CanonicalDocumentValidationAdapter:
    normalization = normalization or _normalization()
    if projection is None:
        projection = ProjectionIntegritySnapshot(normalization.normalized_document)
    return CanonicalDocumentValidationAdapter(
        document_id="revision-1",
        normalization=normalization,
        projection=projection,
        workspace_policy=workspace or _workspace(),
        transition_lineage=_lineage() if lineage is _UNSET else lineage,
        transition_issues=issues,
        transition_coverage=_coverage() if coverage is _UNSET else coverage,
        pinned_schema_result=pinned_schema_result,
    )


def _rule_ids(result):
    return {issue.rule_id for issue in result.issues}


def test_canonical_sha256_is_key_order_independent_and_matches_canonical_bytes():
    left = _normalization({"BL01": 34, "BID01": "B-1"})
    right = _normalization({"BID01": "B-1", "BL01": 34})
    expected_bytes = b'{"BID01":"B-1","BL01":34}'

    assert canonical_document_sha256(left) == canonical_document_sha256(right)
    assert canonical_document_sha256(left) == hashlib.sha256(expected_bytes).hexdigest()


def test_adapter_snapshots_mutable_documents_and_emits_deterministic_evidence():
    normalization = _normalization()
    adapter = _adapter(normalization=normalization)
    original_digest = adapter.document_sha256
    first = adapter.validate_layer(ValidationLayer.JSON_RESOURCE, _profile())

    normalization.normalized_document["BID01"] = "MUTATED"
    normalization.normalized_document["BG01"] = 99
    second = adapter.validate_layer(ValidationLayer.JSON_RESOURCE, _profile())

    assert adapter.document_sha256 == original_digest
    assert first == second
    assert first.validator_version == CANONICAL_VALIDATION_ADAPTER_VERSION
    assert len(first.evidence) == 1


def test_resource_layer_maps_normalizer_diagnostics_without_schema_claims():
    normalization = _normalization(
        {"BID01": " B-1 ", "BZZ99": 7, "Features": {"BF01": "H01"}}
    )
    adapter = _adapter(
        normalization=normalization,
        projection=ProjectionIntegritySnapshot(normalization.normalized_document),
        lineage={
            "/BID01": TransitionValue(
                value="B-1",
                target_tag="BID01",
                rule_id="test-bid01-v1",
                category=TransitionCategory.DIRECT,
            )
        },
        coverage=_coverage(document_leaf_count=1, lineage_leaf_count=1),
    )

    result = adapter.validate_layer(ValidationLayer.JSON_RESOURCE, _profile())

    assert result.completed is True
    assert result.passed is False
    assert {
        "BG-CANONICAL-UNKNOWN-TAG",
        "BG-CANONICAL-CONTAINER-TYPE",
        "BG-CANONICAL-REJECTED-array_type_mismatch",
        "BG-CANONICAL-COERCION",
    }.issubset(_rule_ids(result))
    assert all(issue.evidence_keys == (result.evidence[0].key,) for issue in result.issues)
    assert any(issue.severity is ValidationSeverity.INFO for issue in result.issues)


@pytest.mark.parametrize(
    "layer,expected_rule",
    [
        (ValidationLayer.PINNED_SCHEMA, "BG-PINNED-SCHEMA-AVAILABILITY"),
        (
            ValidationLayer.OFFICIAL_RULES,
            "BG-EXECUTABLE-RULES-AVAILABILITY",
        ),
    ],
)
def test_unverified_schema_and_rule_layers_are_explicitly_incomplete(
    layer, expected_rule
):
    result = _adapter().validate_layer(layer, _profile())

    assert result.completed is False
    assert result.passed is False
    assert _rule_ids(result) == {expected_rule}
    assert result.issues[0].severity is ValidationSeverity.CRITICAL
    assert result.evidence[0].attributes[-1] == (
        "status",
        "unavailable_unverified",
    )
    assert "no " in result.issues[0].message.lower()
    assert "executed" in result.issues[0].message.lower() or "performed" in result.issues[0].message.lower()


def _schema_result(status, *, issues=(), detail="test schema result"):
    return PinnedSchemaResult(
        status=status,
        bundle_id="test_bundle_2026",
        artifact_id="test_schema_artifact",
        validation_target=ValidationTarget.BRIDGE,
        dialect="https://json-schema.org/draft/2024-07/schema",
        schema_sha256="a" * 64,
        issues=issues,
        issues_truncated=False,
        detail=detail,
    )


def test_pinned_schema_unsupported_dialect_is_fail_closed_with_exact_diagnostic():
    result = _adapter(
        pinned_schema_result=_schema_result(
            SchemaExecutionStatus.UNSUPPORTED_DIALECT,
            detail="unsupported JSON Schema dialect: draft/2024-07",
        )
    ).validate_layer(ValidationLayer.PINNED_SCHEMA, _profile())

    assert result.completed is False
    assert result.passed is False
    assert _rule_ids(result) == {"BG-PINNED-SCHEMA-AVAILABILITY"}
    assert "unsupported_dialect" in result.issues[0].message
    assert "draft/2024-07" in result.issues[0].message
    assert ("status", "unsupported_dialect") in result.evidence[0].attributes


def test_pinned_schema_valid_result_completes_layer_without_issues():
    result = _adapter(
        pinned_schema_result=_schema_result(SchemaExecutionStatus.VALID)
    ).validate_layer(ValidationLayer.PINNED_SCHEMA, _profile())

    assert result.completed is True
    assert result.passed is True
    assert result.issues == ()
    assert ("status", "executed_valid") in result.evidence[0].attributes


def test_pinned_schema_invalid_result_preserves_pointer_and_keyword():
    result = _adapter(
        pinned_schema_result=_schema_result(
            SchemaExecutionStatus.INVALID,
            issues=(
                SchemaValidationIssue(
                    instance_pointer="/BID01",
                    schema_pointer="/$defs/bridge/properties/BID01/type",
                    keyword="type",
                    message="'B-1' is not of type integer",
                ),
            ),
        )
    ).validate_layer(ValidationLayer.PINNED_SCHEMA, _profile())

    assert result.completed is True
    assert result.passed is False
    assert _rule_ids(result) == {"BG-PINNED-SCHEMA-type"}
    assert result.issues[0].json_pointer == "/BID01"


def test_module_never_imports_or_calls_the_legacy_validation_function():
    source = inspect.getsource(canonical_validation_module)

    assert "validate_snbi_payload" not in source
    assert "routers.services.snbi_validation" not in source


def test_exact_projection_round_trip_passes_bridgeguard_integrity_check():
    result = _adapter().validate_layer(
        ValidationLayer.PROJECTION_INTEGRITY,
        _profile(),
    )

    assert result.completed is True
    assert result.passed is True
    assert result.issues == ()


def test_projection_is_incomplete_without_round_trip_evidence():
    adapter = _adapter(projection=ProjectionIntegritySnapshot(None))

    result = adapter.validate_layer(ValidationLayer.PROJECTION_INTEGRITY, _profile())

    assert result.completed is False
    assert _rule_ids(result) == {"BG-PROJECTION-EVIDENCE"}


def test_projection_detects_round_trip_loss_orphans_and_duplicate_keys():
    projection = ProjectionIntegritySnapshot(
        {"BID01": "B-1"},
        missing_json_pointers=("/BL01",),
        orphan_record_ids=("row-orphan",),
        duplicate_record_keys=("feature:H01",),
    )
    result = _adapter(projection=projection).validate_layer(
        ValidationLayer.PROJECTION_INTEGRITY,
        _profile(),
    )

    assert result.completed is True
    assert result.passed is False
    assert {
        "BG-PROJECTION-ROUND-TRIP",
        "BG-PROJECTION-MISSING-PATH",
        "BG-PROJECTION-ORPHAN",
        "BG-PROJECTION-DUPLICATE",
    } == _rule_ids(result)


def test_research_transition_lineage_and_coverage_reconcile():
    result = _adapter().validate_layer(ValidationLayer.TRANSITION_QA, _profile())

    assert result.completed is True
    assert result.passed is True
    assert result.issues == ()


def test_research_transition_is_incomplete_when_evidence_is_absent():
    result = _adapter(lineage={}, coverage=None).validate_layer(
        ValidationLayer.TRANSITION_QA,
        _profile(),
    )

    assert result.completed is False
    assert "BG-TRANSITION-EVIDENCE" in _rule_ids(result)
    assert "BG-TRANSITION-LINEAGE-MISSING" in _rule_ids(result)


def test_transition_detects_missing_lineage_and_inconsistent_coverage():
    lineage = _lineage()
    del lineage["/BL01"]
    result = _adapter(
        lineage=lineage,
        coverage=_coverage(
            document_leaf_count=99,
            lineage_leaf_count=7,
            uncovered_leaf_count=0,
            mapping_exception_count=1,
        ),
    ).validate_layer(ValidationLayer.TRANSITION_QA, _profile())

    assert result.completed is True
    assert result.passed is False
    assert {
        "BG-TRANSITION-LINEAGE-MISSING",
        "BG-TRANSITION-COVERAGE-RECONCILIATION",
        "BG-TRANSITION-COVERAGE-TOTAL",
        "BG-TRANSITION-MAPPING-EXCEPTION",
    }.issubset(_rule_ids(result))


def test_typed_transition_issues_are_preserved_with_severity_and_pointer():
    transition_issue = TransitionIssue(
        code="source_conflict",
        severity=TransitionIssueSeverity.ERROR,
        message="Two eligible legacy records disagree.",
        target_path="/BID01",
        source_record_ids=("record-1", "record-2"),
        source_fields=("STRUCTURE_NUMBER_008",),
    )
    result = _adapter(issues=(transition_issue,)).validate_layer(
        ValidationLayer.TRANSITION_QA,
        _profile(),
    )

    matching = [
        issue for issue in result.issues if issue.rule_id == "BG-TRANSITION-source_conflict"
    ]
    assert len(matching) == 1
    assert matching[0].severity is ValidationSeverity.ERROR
    assert matching[0].json_pointer == "/BID01"


def test_native_document_without_transition_input_is_explicitly_not_applicable():
    profile = _profile(
        profile_id=SUBMISSION_PROFILE,
        kind=ValidationProfileKind.NATIVE_SNBI,
    )
    normalization = _normalization(profile_id=SUBMISSION_PROFILE)
    adapter = _adapter(
        normalization=normalization,
        projection=ProjectionIntegritySnapshot(normalization.normalized_document),
        workspace=_workspace(profiles=(SUBMISSION_PROFILE,)),
        lineage={},
        coverage=None,
    )

    result = adapter.validate_layer(ValidationLayer.TRANSITION_QA, profile)

    assert result.completed is True
    assert result.passed is True
    assert _rule_ids(result) == {"BG-TRANSITION-NOT-APPLICABLE"}


def test_workspace_layer_enforces_profile_bridge_state_and_approval_scope():
    profile = _profile()
    adapter = _adapter(
        workspace=_workspace(
            profiles=("different-profile",),
            bridges=("B-OTHER",),
            states=("36",),
            require_approval=True,
        )
    )

    result = adapter.validate_layer(ValidationLayer.WORKSPACE_POLICY, profile)

    assert result.completed is True
    assert result.passed is False
    assert {
        "BG-WORKSPACE-PROFILE-AUTHORIZATION",
        "BG-WORKSPACE-BRIDGE-AUTHORIZATION",
        "BG-WORKSPACE-STATE-AUTHORIZATION",
        "BG-WORKSPACE-APPROVAL",
    } == _rule_ids(result)


def test_workspace_context_rejects_empty_authorization_sets_and_bad_projection_paths():
    with pytest.raises(ValueError, match="authorized_profile_ids"):
        _workspace(profiles=())
    with pytest.raises(ValueError, match="authorized_bridge_ids"):
        _workspace(bridges=())
    with pytest.raises(ValueError, match="RFC 6901"):
        ProjectionIntegritySnapshot({}, missing_json_pointers=("BL01",))


class _VerifiedRegistry:
    registry_id = "test_registry"

    def __init__(self):
        self.artifact = SimpleNamespace(
            id="test_schema_artifact",
            state="vendored_verified",
            sha256=hashlib.sha256(b"verified artifact").hexdigest(),
        )

    def bundle(self, bundle_id):
        return SimpleNamespace(id=bundle_id)

    def artifacts_for(self, bundle_id):
        return (self.artifact,)

    def unverified_artifact_ids(self, bundle_id):
        return ()

    def select(self, bundle_id, *, require_verified_artifacts=True):
        return SimpleNamespace(
            id=bundle_id,
            implementation_status="executable_tested",
        )


def test_orchestrator_cannot_publish_when_adapter_executors_are_unavailable():
    profile = _profile(
        profile_id=SUBMISSION_PROFILE,
        kind=ValidationProfileKind.SUBMISSION,
        publication_enabled=True,
        expected_rule_ids=("TEST-RULE-1",),
    )
    normalization = _normalization(profile_id=SUBMISSION_PROFILE)
    adapter = _adapter(
        normalization=normalization,
        projection=ProjectionIntegritySnapshot(normalization.normalized_document),
        workspace=_workspace(profiles=(SUBMISSION_PROFILE,)),
        lineage={},
        coverage=None,
    )
    coverage_registry = CoverageRegistry(
        profile_id=profile.profile_id,
        registry_version="test_coverage_v1",
        rows=(
            CoverageRow(
                rule_id="TEST-RULE-1",
                status=CoverageStatus.IMPLEMENTED_TESTED,
                source_reference="BridgeGuard test rule fixture",
                test_ids=("test_rule_1",),
            ),
        ),
    )

    report = evaluate_validation(
        adapter,
        profile=profile,
        standards_registry=_VerifiedRegistry(),
        coverage_registry=coverage_registry,
    )

    assert report.decision.allowed is False
    assert report.official_validation is False
    assert report.decision.official_validation is False
    incomplete_layers = {
        blocker.layer
        for blocker in report.decision.blockers
        if blocker.code == "layer_incomplete"
    }
    assert incomplete_layers == {
        ValidationLayer.PINNED_SCHEMA,
        ValidationLayer.OFFICIAL_RULES,
    }
    assert all(layer.evidence for layer in report.layers)
    rendered = json.dumps(report.to_dict(), sort_keys=True)
    assert "official_validation\": false" in rendered


def test_issue_and_evidence_serialization_is_deterministic_across_instances():
    first = _adapter().validate_layer(ValidationLayer.TRANSITION_QA, _profile())
    second = _adapter().validate_layer(ValidationLayer.TRANSITION_QA, _profile())

    assert first == second
    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(
        second.to_dict(), sort_keys=True
    )
