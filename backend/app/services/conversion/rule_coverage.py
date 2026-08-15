"""Versioned reconciliation of canonical leaves, lineage, and rule coverage.

Coverage declared in this module is only effective when its source artifact is
present in the local FHWA registry as ``vendored_verified`` and its SHA-256 is
the exact digest named by the declaration.  Remote URLs and unverified files
can never produce an official-coverage claim.

Even a complete report is not, by itself, sufficient for submission.  This
slice therefore always returns ``submission_ready=False``; later validation,
workspace-policy, approval, and external FHWA/NBTIS gates remain mandatory.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.standards.fhwa_registry import (
    ArtifactState,
    RegistryError,
    RuleBundleRegistry,
)

from .transition_values import TransitionCategory, TransitionValue


RULE_COVERAGE_FORMAT_VERSION = 1
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_INTERNAL_CATEGORIES = frozenset(
    {
        TransitionCategory.DERIVED,
        TransitionCategory.STRUCTURAL,
        TransitionCategory.APP_PROJECTION,
    }
)


class RuleKind(str, Enum):
    TRANSITION = "transition"
    VALIDATION = "validation"


class CoverageIntent(str, Enum):
    IMPLEMENTED_TESTED = "implemented_tested"
    INTENTIONALLY_OMITTED_NON_REPORTABLE = "intentionally_omitted_non_reportable"
    INTERNAL_DERIVED = "internal_derived"
    PENDING_IMPLEMENTATION = "pending_implementation"


class CoverageClassification(str, Enum):
    COVERED = "covered"
    INTENTIONALLY_OMITTED_NON_REPORTABLE = "intentionally_omitted_non_reportable"
    INTERNAL_DERIVED = "internal_derived"
    UNKNOWN_UNCOVERED = "unknown_uncovered"
    ARTIFACT_UNVERIFIED = "artifact_unverified"


class CoverageLayer(str, Enum):
    CANONICAL_TRANSITION = "canonical_transition"
    REVIEW_OMISSION = "review_omission"
    ORPHAN_LINEAGE = "orphan_lineage"
    VALIDATION_RULE = "validation_rule"
    REGISTRY = "registry"


class ValidationOutcome(str, Enum):
    PASSED = "passed"
    ISSUES = "issues"
    NOT_APPLICABLE = "not_applicable"
    NOT_EVALUATED = "not_evaluated"


@dataclass(frozen=True, slots=True)
class RuleCoverageDeclaration:
    rule_id: str
    rule_kind: RuleKind
    intent: CoverageIntent
    pointer_patterns: Tuple[str, ...]
    artifact_id: Optional[str] = None
    artifact_sha256: Optional[str] = None
    source_locator: Optional[str] = None
    test_ids: Tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        rule_id = _identifier(self.rule_id, "rule_id")
        rule_kind = _enum(RuleKind, self.rule_kind, "rule_kind")
        intent = _enum(CoverageIntent, self.intent, "intent")
        patterns = tuple(sorted(self.pointer_patterns))
        if not patterns:
            raise ValueError("pointer_patterns must not be empty")
        for pattern in patterns:
            _validate_pointer(pattern, allow_wildcard=True)
        if len(patterns) != len(set(patterns)):
            raise ValueError("pointer_patterns must be unique")

        artifact_id = _optional_identifier(self.artifact_id, "artifact_id")
        artifact_sha256 = _optional_sha256(
            self.artifact_sha256, "artifact_sha256"
        )
        source_locator = _optional_nonempty(self.source_locator)
        test_ids = tuple(
            sorted(_identifier(value, "test_id") for value in self.test_ids)
        )
        if len(test_ids) != len(set(test_ids)):
            raise ValueError("test_ids must be unique")
        reason = str(self.reason or "").strip()

        if intent in {
            CoverageIntent.IMPLEMENTED_TESTED,
            CoverageIntent.INTENTIONALLY_OMITTED_NON_REPORTABLE,
        }:
            if artifact_id is None or source_locator is None:
                raise ValueError(
                    f"{intent.value} declarations require artifact_id and source_locator"
                )
        if intent is CoverageIntent.IMPLEMENTED_TESTED and not test_ids:
            raise ValueError("implemented_tested declarations require test_ids")
        if intent in {
            CoverageIntent.INTENTIONALLY_OMITTED_NON_REPORTABLE,
            CoverageIntent.INTERNAL_DERIVED,
            CoverageIntent.PENDING_IMPLEMENTATION,
        } and not reason:
            raise ValueError(f"{intent.value} declarations require a reason")
        if artifact_sha256 is not None and artifact_id is None:
            raise ValueError("artifact_sha256 requires artifact_id")
        if intent is CoverageIntent.INTERNAL_DERIVED and (
            artifact_id is not None or artifact_sha256 is not None
        ):
            raise ValueError("internal-derived declarations may not claim an FHWA artifact")

        object.__setattr__(self, "rule_id", rule_id)
        object.__setattr__(self, "rule_kind", rule_kind)
        object.__setattr__(self, "intent", intent)
        object.__setattr__(self, "pointer_patterns", patterns)
        object.__setattr__(self, "artifact_id", artifact_id)
        object.__setattr__(self, "artifact_sha256", artifact_sha256)
        object.__setattr__(self, "source_locator", source_locator)
        object.__setattr__(self, "test_ids", test_ids)
        object.__setattr__(self, "reason", reason)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_kind": self.rule_kind.value,
            "intent": self.intent.value,
            "pointer_patterns": list(self.pointer_patterns),
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
            "source_locator": self.source_locator,
            "test_ids": list(self.test_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class OfficialRuleCoverageRegistry:
    registry_version: str
    profile_id: str
    rule_bundle_id: str
    declarations: Tuple[RuleCoverageDeclaration, ...]
    format_version: int = RULE_COVERAGE_FORMAT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.format_version, int) or isinstance(
            self.format_version, bool
        ):
            raise TypeError("format_version must be an integer")
        if self.format_version != RULE_COVERAGE_FORMAT_VERSION:
            raise ValueError(
                f"unsupported coverage format_version {self.format_version}"
            )
        registry_version = _identifier(self.registry_version, "registry_version")
        profile_id = _identifier(self.profile_id, "profile_id")
        rule_bundle_id = _identifier(self.rule_bundle_id, "rule_bundle_id")
        declarations = tuple(self.declarations)
        if not declarations:
            raise ValueError("coverage registry requires at least one declaration")
        if any(not isinstance(item, RuleCoverageDeclaration) for item in declarations):
            raise TypeError("declarations must contain RuleCoverageDeclaration values")
        keys = [(item.rule_kind, item.rule_id) for item in declarations]
        if len(keys) != len(set(keys)):
            raise ValueError("coverage registry contains duplicate rule declarations")
        object.__setattr__(self, "registry_version", registry_version)
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "rule_bundle_id", rule_bundle_id)
        object.__setattr__(self, "declarations", declarations)

    @property
    def sha256(self) -> str:
        payload = {
            "format_version": self.format_version,
            "registry_version": self.registry_version,
            "profile_id": self.profile_id,
            "rule_bundle_id": self.rule_bundle_id,
            "declarations": [
                item.to_dict()
                for item in sorted(
                    self.declarations,
                    key=lambda row: (row.rule_kind.value, row.rule_id),
                )
            ],
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ValidationRuleEvaluation:
    rule_id: str
    outcome: ValidationOutcome
    evaluated_pointers: Tuple[str, ...]
    issue_count: int = 0
    reason: str = ""

    def __post_init__(self) -> None:
        rule_id = _identifier(self.rule_id, "validation rule_id")
        outcome = _enum(ValidationOutcome, self.outcome, "validation outcome")
        pointers = tuple(self.evaluated_pointers)
        for pointer in pointers:
            _validate_pointer(pointer, allow_wildcard=False)
        if len(pointers) != len(set(pointers)):
            raise ValueError("evaluated_pointers must be unique")
        if not isinstance(self.issue_count, int) or isinstance(self.issue_count, bool):
            raise TypeError("issue_count must be an integer")
        if self.issue_count < 0:
            raise ValueError("issue_count may not be negative")
        reason = str(self.reason or "").strip()
        if outcome is ValidationOutcome.ISSUES and self.issue_count == 0:
            raise ValueError("issues outcome requires issue_count greater than zero")
        if outcome is ValidationOutcome.PASSED and self.issue_count:
            raise ValueError("passed outcome cannot have issues")
        if outcome in {ValidationOutcome.PASSED, ValidationOutcome.ISSUES} and not pointers:
            raise ValueError(
                f"{outcome.value} outcome requires at least one evaluated pointer"
            )
        if outcome in {
            ValidationOutcome.NOT_APPLICABLE,
            ValidationOutcome.NOT_EVALUATED,
        } and not reason:
            raise ValueError(f"{outcome.value} outcome requires a reason")
        object.__setattr__(self, "rule_id", rule_id)
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "evaluated_pointers", pointers)
        object.__setattr__(self, "reason", reason)


@dataclass(frozen=True, slots=True)
class ArtifactCoverageEvidence:
    artifact_id: str
    expected_sha256: Optional[str]
    registered_sha256: Optional[str]
    registered_state: str
    verified: bool
    reason: str


@dataclass(frozen=True, slots=True)
class CoverageRecord:
    layer: CoverageLayer
    classification: CoverageClassification
    rule_id: Optional[str]
    pointer: Optional[str]
    artifact_id: Optional[str]
    reasons: Tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuleCoverageReport:
    profile_id: str
    registry_version: str
    registry_sha256: str
    rule_bundle_id: str
    canonical_leaf_records: Tuple[CoverageRecord, ...]
    review_records: Tuple[CoverageRecord, ...]
    orphan_lineage_records: Tuple[CoverageRecord, ...]
    validation_records: Tuple[CoverageRecord, ...]
    registry_records: Tuple[CoverageRecord, ...]
    artifact_evidence: Tuple[ArtifactCoverageEvidence, ...]
    classification_counts: Tuple[Tuple[str, int], ...]
    blocker_codes: Tuple[str, ...]
    coverage_complete: bool
    submission_ready: bool = False


def reconcile_rule_coverage(
    canonical_document: Mapping[str, Any],
    *,
    profile_id: str,
    transition_lineage: Mapping[str, TransitionValue],
    review_lineage: Mapping[str, TransitionValue],
    validation_evaluations: Sequence[ValidationRuleEvaluation],
    coverage_registry: OfficialRuleCoverageRegistry,
    artifact_registry: Optional[RuleBundleRegistry],
) -> RuleCoverageReport:
    """Reconcile document leaves and executed rules without inferring coverage."""

    selected_profile = _identifier(profile_id, "profile_id")
    if not isinstance(canonical_document, Mapping):
        raise TypeError("canonical_document must be a mapping")
    if not isinstance(coverage_registry, OfficialRuleCoverageRegistry):
        raise TypeError(
            "coverage_registry must be an OfficialRuleCoverageRegistry"
        )
    if artifact_registry is not None and not isinstance(
        artifact_registry, RuleBundleRegistry
    ):
        raise TypeError("artifact_registry must be a RuleBundleRegistry or None")
    _validate_lineage(transition_lineage, "transition_lineage")
    _validate_lineage(review_lineage, "review_lineage")
    evaluations = tuple(validation_evaluations)
    if any(not isinstance(item, ValidationRuleEvaluation) for item in evaluations):
        raise TypeError("validation_evaluations must contain ValidationRuleEvaluation values")
    evaluation_ids = [item.rule_id for item in evaluations]
    if len(evaluation_ids) != len(set(evaluation_ids)):
        raise ValueError("duplicate validation rule evaluations are not allowed")

    declarations = {
        (row.rule_kind, row.rule_id): row for row in coverage_registry.declarations
    }
    artifact_evidence = _artifact_evidence(
        coverage_registry.declarations,
        artifact_registry,
        rule_bundle_id=coverage_registry.rule_bundle_id,
    )
    evidence_by_id = {item.artifact_id: item for item in artifact_evidence}
    blockers: set[str] = set()
    registry_records: List[CoverageRecord] = []

    overlapping_lineage = sorted(
        set(transition_lineage).intersection(review_lineage)
    )
    for pointer in overlapping_lineage:
        blockers.add("conflicting_lineage")
        registry_records.append(
            CoverageRecord(
                layer=CoverageLayer.REGISTRY,
                classification=CoverageClassification.UNKNOWN_UNCOVERED,
                rule_id=transition_lineage[pointer].rule_id,
                pointer=pointer,
                artifact_id=None,
                reasons=("pointer_present_in_transition_and_review_lineage",),
            )
        )

    if selected_profile != coverage_registry.profile_id:
        blockers.add("coverage_profile_mismatch")
        registry_records.append(
            CoverageRecord(
                layer=CoverageLayer.REGISTRY,
                classification=CoverageClassification.UNKNOWN_UNCOVERED,
                rule_id=None,
                pointer=None,
                artifact_id=None,
                reasons=("coverage_profile_mismatch",),
            )
        )

    if artifact_registry is None:
        blockers.add("artifact_registry_unavailable")
    else:
        try:
            selected_bundle = artifact_registry.bundle(coverage_registry.rule_bundle_id)
            if not selected_bundle.enabled:
                blockers.add("rule_bundle_disabled")
            else:
                declared_artifact_ids = {
                    item.artifact_id for item in artifact_evidence
                }
                for artifact_id in artifact_registry.unverified_artifact_ids(
                    selected_bundle.id
                ):
                    blockers.add("rule_bundle_contains_unverified_artifacts")
                    if artifact_id not in declared_artifact_ids:
                        registry_records.append(
                            CoverageRecord(
                                layer=CoverageLayer.REGISTRY,
                                classification=(
                                    CoverageClassification.ARTIFACT_UNVERIFIED
                                ),
                                rule_id=None,
                                pointer=None,
                                artifact_id=artifact_id,
                                reasons=(
                                    "selected_rule_bundle_requires_unverified_artifact",
                                ),
                            )
                        )
        except RegistryError:
            blockers.add("rule_bundle_missing")

    for evidence in artifact_evidence:
        if not evidence.verified:
            blockers.add("artifact_unverified")
            registry_records.append(
                CoverageRecord(
                    layer=CoverageLayer.REGISTRY,
                    classification=CoverageClassification.ARTIFACT_UNVERIFIED,
                    rule_id=None,
                    pointer=None,
                    artifact_id=evidence.artifact_id,
                    reasons=(evidence.reason,),
                )
            )

    leaf_values = _leaf_values(canonical_document)
    leaf_paths = set(leaf_values)
    leaf_records: List[CoverageRecord] = []
    for pointer in sorted(leaf_paths):
        transition = transition_lineage.get(pointer)
        leaf_records.append(
            _classify_lineage_pointer(
                pointer,
                transition,
                layer=CoverageLayer.CANONICAL_TRANSITION,
                declaration=declarations.get(
                    (RuleKind.TRANSITION, transition.rule_id)
                )
                if transition is not None
                else None,
                evidence_by_id=evidence_by_id,
                is_output_leaf=True,
                output_value=leaf_values[pointer],
            )
        )

    review_records: List[CoverageRecord] = []
    for pointer, transition in sorted(review_lineage.items()):
        _validate_pointer(pointer, allow_wildcard=False)
        review_records.append(
            _classify_lineage_pointer(
                pointer,
                transition,
                layer=CoverageLayer.REVIEW_OMISSION,
                declaration=declarations.get(
                    (RuleKind.TRANSITION, transition.rule_id)
                ),
                evidence_by_id=evidence_by_id,
                is_output_leaf=False,
                output_value=None,
            )
        )

    orphan_records: List[CoverageRecord] = []
    for pointer, transition in sorted(transition_lineage.items()):
        _validate_pointer(pointer, allow_wildcard=False)
        if pointer in leaf_paths:
            continue
        orphan_records.append(
            CoverageRecord(
                layer=CoverageLayer.ORPHAN_LINEAGE,
                classification=CoverageClassification.UNKNOWN_UNCOVERED,
                rule_id=transition.rule_id,
                pointer=pointer,
                artifact_id=None,
                reasons=("lineage_pointer_not_present_in_canonical_document",),
            )
        )

    validation_records = _validation_records(
        evaluations=evaluations,
        declarations=declarations,
        evidence_by_id=evidence_by_id,
    )

    all_records = (
        leaf_records
        + review_records
        + orphan_records
        + validation_records
        + registry_records
    )
    for record in all_records:
        if record.classification is CoverageClassification.UNKNOWN_UNCOVERED:
            blockers.add("unknown_uncovered")
        elif record.classification is CoverageClassification.ARTIFACT_UNVERIFIED:
            blockers.add("artifact_unverified")

    counts: Dict[str, int] = {classification.value: 0 for classification in CoverageClassification}
    for record in all_records:
        counts[record.classification.value] += 1
    coverage_complete = not blockers and all(
        record.classification
        not in {
            CoverageClassification.UNKNOWN_UNCOVERED,
            CoverageClassification.ARTIFACT_UNVERIFIED,
        }
        for record in all_records
    )
    return RuleCoverageReport(
        profile_id=selected_profile,
        registry_version=coverage_registry.registry_version,
        registry_sha256=coverage_registry.sha256,
        rule_bundle_id=coverage_registry.rule_bundle_id,
        canonical_leaf_records=tuple(leaf_records),
        review_records=tuple(review_records),
        orphan_lineage_records=tuple(orphan_records),
        validation_records=tuple(validation_records),
        registry_records=tuple(registry_records),
        artifact_evidence=artifact_evidence,
        classification_counts=tuple(sorted(counts.items())),
        blocker_codes=tuple(sorted(blockers)),
        coverage_complete=coverage_complete,
        submission_ready=False,
    )


def _classify_lineage_pointer(
    pointer: str,
    transition: Optional[TransitionValue],
    *,
    layer: CoverageLayer,
    declaration: Optional[RuleCoverageDeclaration],
    evidence_by_id: Mapping[str, ArtifactCoverageEvidence],
    is_output_leaf: bool,
    output_value: Any,
) -> CoverageRecord:
    if transition is None:
        return CoverageRecord(
            layer,
            CoverageClassification.UNKNOWN_UNCOVERED,
            None,
            pointer,
            None,
            ("missing_transition_lineage",),
        )
    if is_output_leaf and not _target_matches_pointer(
        pointer, transition.target_tag
    ):
        return CoverageRecord(
            layer,
            CoverageClassification.UNKNOWN_UNCOVERED,
            transition.rule_id,
            pointer,
            declaration.artifact_id if declaration is not None else None,
            ("transition_target_tag_does_not_match_pointer",),
        )
    if is_output_leaf and not _lineage_value_matches(
        output_value, transition.value
    ):
        return CoverageRecord(
            layer,
            CoverageClassification.UNKNOWN_UNCOVERED,
            transition.rule_id,
            pointer,
            declaration.artifact_id if declaration is not None else None,
            ("canonical_value_does_not_match_transition_lineage",),
        )
    if declaration is None:
        return CoverageRecord(
            layer,
            CoverageClassification.UNKNOWN_UNCOVERED,
            transition.rule_id,
            pointer,
            None,
            ("transition_rule_not_declared",),
        )
    if not any(_pointer_matches(pointer, pattern) for pattern in declaration.pointer_patterns):
        return CoverageRecord(
            layer,
            CoverageClassification.UNKNOWN_UNCOVERED,
            transition.rule_id,
            pointer,
            declaration.artifact_id,
            ("lineage_pointer_outside_declared_patterns",),
        )
    if transition.category in _INTERNAL_CATEGORIES:
        if declaration.intent is not CoverageIntent.INTERNAL_DERIVED:
            return CoverageRecord(
                layer,
                CoverageClassification.UNKNOWN_UNCOVERED,
                transition.rule_id,
                pointer,
                declaration.artifact_id,
                ("internal_transition_not_declared_internal_derived",),
            )
        return CoverageRecord(
            layer,
            CoverageClassification.INTERNAL_DERIVED,
            transition.rule_id,
            pointer,
            None,
            (declaration.reason, f"transition_category:{transition.category.value}"),
        )
    if declaration.intent is CoverageIntent.INTERNAL_DERIVED:
        return CoverageRecord(
            layer,
            CoverageClassification.UNKNOWN_UNCOVERED,
            transition.rule_id,
            pointer,
            None,
            ("official_transition_category_declared_internal",),
        )
    if declaration.intent is CoverageIntent.PENDING_IMPLEMENTATION:
        return CoverageRecord(
            layer,
            CoverageClassification.UNKNOWN_UNCOVERED,
            transition.rule_id,
            pointer,
            declaration.artifact_id,
            ("rule_pending_implementation",),
        )

    evidence = evidence_by_id.get(declaration.artifact_id or "")
    if evidence is None or not evidence.verified:
        return CoverageRecord(
            layer,
            CoverageClassification.ARTIFACT_UNVERIFIED,
            transition.rule_id,
            pointer,
            declaration.artifact_id,
            ("governing_artifact_not_locally_sha_verified",),
        )

    if declaration.intent is CoverageIntent.INTENTIONALLY_OMITTED_NON_REPORTABLE:
        if is_output_leaf:
            return CoverageRecord(
                layer,
                CoverageClassification.UNKNOWN_UNCOVERED,
                transition.rule_id,
                pointer,
                declaration.artifact_id,
                ("non_reportable_rule_emitted_a_canonical_value",),
            )
        return CoverageRecord(
            layer,
            CoverageClassification.INTENTIONALLY_OMITTED_NON_REPORTABLE,
            transition.rule_id,
            pointer,
            declaration.artifact_id,
            (declaration.reason,),
        )
    return CoverageRecord(
        layer,
        CoverageClassification.COVERED,
        transition.rule_id,
        pointer,
        declaration.artifact_id,
        ("declared_implemented_tested_and_artifact_verified",),
    )


def _validation_records(
    *,
    evaluations: Sequence[ValidationRuleEvaluation],
    declarations: Mapping[Tuple[RuleKind, str], RuleCoverageDeclaration],
    evidence_by_id: Mapping[str, ArtifactCoverageEvidence],
) -> List[CoverageRecord]:
    results: List[CoverageRecord] = []
    evaluation_by_id = {item.rule_id: item for item in evaluations}
    validation_declarations = {
        rule_id: row
        for (kind, rule_id), row in declarations.items()
        if kind is RuleKind.VALIDATION
    }

    for rule_id, row in sorted(validation_declarations.items()):
        evaluation = evaluation_by_id.get(rule_id)
        evidence = evidence_by_id.get(row.artifact_id or "")
        artifact_verified = row.intent is CoverageIntent.INTERNAL_DERIVED or (
            evidence is not None and evidence.verified
        )
        if row.intent is CoverageIntent.PENDING_IMPLEMENTATION:
            classification = CoverageClassification.UNKNOWN_UNCOVERED
            reasons = ("validation_rule_pending_implementation",)
        elif row.intent is not CoverageIntent.INTERNAL_DERIVED and not artifact_verified:
            classification = CoverageClassification.ARTIFACT_UNVERIFIED
            reasons = ("governing_artifact_not_locally_sha_verified",)
        elif row.intent is CoverageIntent.INTERNAL_DERIVED:
            classification = CoverageClassification.INTERNAL_DERIVED
            reasons = (row.reason,)
        elif row.intent is CoverageIntent.INTENTIONALLY_OMITTED_NON_REPORTABLE:
            if evaluation is not None and evaluation.outcome in {
                ValidationOutcome.PASSED,
                ValidationOutcome.ISSUES,
            }:
                classification = CoverageClassification.UNKNOWN_UNCOVERED
                reasons = ("intentionally_omitted_validation_rule_was_executed",)
            else:
                classification = (
                    CoverageClassification.INTENTIONALLY_OMITTED_NON_REPORTABLE
                )
                reasons = (row.reason,)
        elif evaluation is None:
            classification = CoverageClassification.UNKNOWN_UNCOVERED
            reasons = ("validation_rule_not_evaluated",)
        elif not all(
            any(_pointer_matches(pointer, pattern) for pattern in row.pointer_patterns)
            for pointer in evaluation.evaluated_pointers
        ):
            classification = CoverageClassification.UNKNOWN_UNCOVERED
            reasons = ("validation_pointer_outside_declared_patterns",)
        elif evaluation.outcome is ValidationOutcome.NOT_EVALUATED:
            classification = CoverageClassification.UNKNOWN_UNCOVERED
            reasons = ("validation_rule_not_evaluated", evaluation.reason)
        elif evaluation.outcome is ValidationOutcome.NOT_APPLICABLE:
            classification = CoverageClassification.INTENTIONALLY_OMITTED_NON_REPORTABLE
            reasons = ("validation_rule_not_applicable", evaluation.reason)
        else:
            classification = CoverageClassification.COVERED
            reasons = (
                "validation_rule_executed_with_issues"
                if evaluation.outcome is ValidationOutcome.ISSUES
                else "validation_rule_executed_and_passed",
            )
        results.append(
            CoverageRecord(
                CoverageLayer.VALIDATION_RULE,
                classification,
                rule_id,
                None,
                row.artifact_id,
                reasons,
            )
        )

    for evaluation in sorted(evaluations, key=lambda item: item.rule_id):
        if evaluation.rule_id in validation_declarations:
            continue
        results.append(
            CoverageRecord(
                CoverageLayer.VALIDATION_RULE,
                CoverageClassification.UNKNOWN_UNCOVERED,
                evaluation.rule_id,
                None,
                None,
                ("validation_rule_not_declared",),
            )
        )
    return results


def _artifact_evidence(
    declarations: Iterable[RuleCoverageDeclaration],
    artifact_registry: Optional[RuleBundleRegistry],
    *,
    rule_bundle_id: str,
) -> Tuple[ArtifactCoverageEvidence, ...]:
    expected: Dict[str, set[Optional[str]]] = {}
    for row in declarations:
        if row.artifact_id is not None:
            expected.setdefault(row.artifact_id, set()).add(row.artifact_sha256)

    output: List[ArtifactCoverageEvidence] = []
    for artifact_id, digests in sorted(expected.items()):
        non_null_digests = {value for value in digests if value is not None}
        expected_digest = next(iter(non_null_digests)) if len(non_null_digests) == 1 else None
        if len(non_null_digests) > 1:
            output.append(
                ArtifactCoverageEvidence(
                    artifact_id,
                    None,
                    None,
                    "conflicting_declaration_digests",
                    False,
                    "coverage declarations name conflicting artifact SHA-256 digests",
                )
            )
            continue
        if expected_digest is None or None in digests:
            output.append(
                ArtifactCoverageEvidence(
                    artifact_id,
                    None,
                    None,
                    "unverified",
                    False,
                    "coverage declaration does not pin an artifact SHA-256",
                )
            )
            continue
        if artifact_registry is None:
            output.append(
                ArtifactCoverageEvidence(
                    artifact_id,
                    expected_digest,
                    None,
                    "registry_unavailable",
                    False,
                    "local FHWA artifact registry is unavailable",
                )
            )
            continue
        try:
            artifact = artifact_registry.artifact(artifact_id)
        except RegistryError:
            output.append(
                ArtifactCoverageEvidence(
                    artifact_id,
                    expected_digest,
                    None,
                    "missing",
                    False,
                    "artifact is absent from the local FHWA registry",
                )
            )
            continue
        try:
            bundle_artifact_ids = {
                item.id for item in artifact_registry.artifacts_for(rule_bundle_id)
            }
        except RegistryError:
            bundle_artifact_ids = set()
        if artifact_id not in bundle_artifact_ids:
            output.append(
                ArtifactCoverageEvidence(
                    artifact_id,
                    expected_digest,
                    artifact.sha256,
                    getattr(artifact.state, "value", str(artifact.state)),
                    False,
                    "artifact is not governed by the selected rule bundle",
                )
            )
            continue
        registered_state = getattr(artifact.state, "value", str(artifact.state))
        verified = (
            artifact.state is ArtifactState.VENDORED_VERIFIED
            and artifact.sha256 == expected_digest
            and bool(artifact.local_path)
        )
        if artifact.state is not ArtifactState.VENDORED_VERIFIED:
            reason = "artifact is not marked vendored_verified"
        elif artifact.sha256 != expected_digest:
            reason = "coverage declaration digest does not match the verified artifact"
        elif not artifact.local_path:
            reason = "verified artifact has no local path"
        else:
            reason = "local artifact SHA-256 matches the coverage declaration"
        output.append(
            ArtifactCoverageEvidence(
                artifact_id,
                expected_digest,
                artifact.sha256,
                registered_state,
                verified,
                reason,
            )
        )
    return tuple(output)


def _validate_lineage(
    values: Mapping[str, TransitionValue], name: str
) -> None:
    if not isinstance(values, Mapping):
        raise TypeError(f"{name} must be a mapping")
    for pointer, transition in values.items():
        _validate_pointer(pointer, allow_wildcard=False)
        if not isinstance(transition, TransitionValue):
            raise TypeError(f"{name} values must be TransitionValue instances")


def _leaf_values(value: Any, pointer: str = "") -> Dict[str, Any]:
    if isinstance(value, Mapping):
        output: Dict[str, Any] = {}
        for key, child in value.items():
            output.update(_leaf_values(child, _join_pointer(pointer, key)))
        return output
    if isinstance(value, list):
        output = {}
        for index, child in enumerate(value):
            output.update(_leaf_values(child, _join_pointer(pointer, str(index))))
        return output
    return {pointer: value}


def _target_matches_pointer(pointer: str, target_tag: str) -> bool:
    if pointer == "":
        return True
    token = pointer.rsplit("/", 1)[-1]
    if token.isdigit():
        return True
    decoded = token.replace("~1", "/").replace("~0", "~")
    return decoded == target_tag


def _lineage_value_matches(canonical_value: Any, lineage_value: Any) -> bool:
    if lineage_value is None:
        return False
    if isinstance(canonical_value, bool) or isinstance(lineage_value, bool):
        return type(canonical_value) is type(lineage_value) and canonical_value == lineage_value
    numeric_types = (int, float, Decimal)
    try:
        if isinstance(lineage_value, numeric_types) and isinstance(
            canonical_value, numeric_types
        ):
            left = Decimal(str(canonical_value))
            right = Decimal(str(lineage_value))
            return left.is_finite() and right.is_finite() and left == right
    except (ArithmeticError, InvalidOperation, ValueError):
        return False
    return type(canonical_value) is type(lineage_value) and canonical_value == lineage_value


def _pointer_matches(pointer: str, pattern: str) -> bool:
    pointer_parts = pointer.split("/")[1:]
    pattern_parts = pattern.split("/")[1:]
    return len(pointer_parts) == len(pattern_parts) and all(
        pattern_part == "*" or pattern_part == pointer_part
        for pointer_part, pattern_part in zip(pointer_parts, pattern_parts)
    )


def _validate_pointer(pointer: str, *, allow_wildcard: bool) -> None:
    if not isinstance(pointer, str):
        raise ValueError(f"invalid RFC 6901 pointer: {pointer!r}")
    if pointer == "":
        return
    if not pointer.startswith("/"):
        raise ValueError(f"invalid RFC 6901 pointer: {pointer!r}")
    for token in pointer.split("/")[1:]:
        if token == "*" and allow_wildcard:
            continue
        index = 0
        while index < len(token):
            if token[index] != "~":
                index += 1
                continue
            if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
                raise ValueError(f"invalid RFC 6901 escape in pointer: {pointer!r}")
            index += 2


def _join_pointer(parent: str, token: Any) -> str:
    escaped = str(token).replace("~", "~0").replace("/", "~1")
    return f"{parent}/{escaped}"


def _identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _IDENTIFIER_RE.fullmatch(text):
        raise ValueError(f"{field} must be a stable identifier")
    return text


def _optional_identifier(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    return _identifier(value, field)


def _optional_sha256(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    if not _SHA256_RE.fullmatch(text):
        raise ValueError(f"{field} must be 64 lowercase hexadecimal characters")
    return text


def _optional_nonempty(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise ValueError("source_locator cannot be blank")
    return text


def _enum(enum_type: Any, value: Any, field: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc
