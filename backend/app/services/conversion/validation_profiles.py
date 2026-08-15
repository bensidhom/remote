"""Fail-closed, layered validation decisions for canonical bridge revisions.

This module is intentionally independent of the legacy validator and of a
particular future canonical-schema implementation. A canonical implementation
only needs to satisfy :class:`CanonicalValidationAdapter`; this orchestrator
owns layer order, standards-registry verification, rule-coverage accounting,
and the BridgeGuard publication decision.

Passing this gate never means that FHWA or NBTIS validated the document. It is
only a BridgeGuard internal publication decision, and external validation and
agency review remain separate requirements.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Protocol, Sequence, runtime_checkable


VALIDATION_ORCHESTRATOR_VERSION = "bridgeguard-validation-orchestrator-v1"
BRIDGEGUARD_VALIDATION_LABEL = "BridgeGuard validation"
NOT_OFFICIAL_DISCLAIMER = (
    "BridgeGuard validation is not official FHWA or NBTIS validation. "
    "External validation and authorized agency review remain required."
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")


class ValidationLayer(str, Enum):
    JSON_RESOURCE = "json_resource"
    PINNED_SCHEMA = "pinned_schema"
    PROJECTION_INTEGRITY = "bridgeguard_projection_integrity"
    OFFICIAL_RULES = "executable_official_rules"
    TRANSITION_QA = "transition_qa"
    WORKSPACE_POLICY = "workspace_policy"


VALIDATION_LAYER_ORDER = (
    ValidationLayer.JSON_RESOURCE,
    ValidationLayer.PINNED_SCHEMA,
    ValidationLayer.PROJECTION_INTEGRITY,
    ValidationLayer.OFFICIAL_RULES,
    ValidationLayer.TRANSITION_QA,
    ValidationLayer.WORKSPACE_POLICY,
)


class ValidationSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ValidationProfileKind(str, Enum):
    RESEARCH_IMPORT = "research_import"
    NATIVE_SNBI = "native_snbi"
    SUBMISSION = "submission"
    INFORMATIONAL_PREVIEW = "informational_preview"


class PublishPolicy(str, Enum):
    ADVISORY = "advisory"
    REJECT_CRITICAL = "reject_critical"
    PUBLISH_GATE = "publish_gate"


class CoverageStatus(str, Enum):
    IMPLEMENTED_TESTED = "implemented_tested"
    JSON_SCHEMA = "json_schema"
    FHWA_EXTERNAL = "fhwa_external"
    NOT_APPLICABLE = "not_applicable"
    UNIMPLEMENTED = "unimplemented"


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """An immutable reference to evidence bytes or a deterministic record."""

    kind: str
    evidence_id: str
    sha256: str
    attributes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        kind = _identifier(self.kind, "evidence kind")
        evidence_id = _identifier(self.evidence_id, "evidence ID")
        sha256 = _sha256(self.sha256, "evidence sha256")
        attributes = tuple((str(key), str(value)) for key, value in self.attributes)
        keys = [key for key, _value in attributes]
        if any(not key for key in keys) or len(set(keys)) != len(keys):
            raise ValueError("evidence attribute keys must be nonblank and unique")
        if attributes != tuple(sorted(attributes)):
            raise ValueError("evidence attributes must be sorted by key")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "evidence_id", evidence_id)
        object.__setattr__(self, "sha256", sha256)
        object.__setattr__(self, "attributes", attributes)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.evidence_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "evidence_id": self.evidence_id,
            "sha256": self.sha256,
            "attributes": {key: value for key, value in self.attributes},
        }


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    issue_id: str
    rule_id: str
    severity: ValidationSeverity
    message: str
    json_pointer: str | None = None
    evidence_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        issue_id = _identifier(self.issue_id, "issue ID")
        rule_id = _identifier(self.rule_id, "rule ID")
        severity = _enum(self.severity, ValidationSeverity, "severity")
        message = str(self.message).strip()
        if not message:
            raise ValueError("validation issue message is required")
        pointer = self.json_pointer
        if pointer is not None:
            pointer = str(pointer)
            if pointer and not pointer.startswith("/"):
                raise ValueError("json_pointer must be empty or start with '/'")
        evidence_keys = tuple(str(value) for value in self.evidence_keys)
        if any(not value for value in evidence_keys):
            raise ValueError("issue evidence keys cannot be blank")
        object.__setattr__(self, "issue_id", issue_id)
        object.__setattr__(self, "rule_id", rule_id)
        object.__setattr__(self, "severity", severity)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "json_pointer", pointer)
        object.__setattr__(self, "evidence_keys", evidence_keys)

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "rule_id": self.rule_id,
            "severity": self.severity.value,
            "message": self.message,
            "json_pointer": self.json_pointer,
            "evidence_keys": list(self.evidence_keys),
        }


@dataclass(frozen=True, slots=True)
class LayerResult:
    layer: ValidationLayer
    profile_id: str
    input_sha256: str
    validator_id: str
    validator_version: str
    completed: bool
    issues: tuple[ValidationIssue, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        layer = _enum(self.layer, ValidationLayer, "validation layer")
        profile_id = _identifier(self.profile_id, "profile ID")
        input_sha256 = _sha256(self.input_sha256, "layer input sha256")
        validator_id = _identifier(self.validator_id, "validator ID")
        validator_version = _identifier(
            self.validator_version, "validator version"
        )
        if not isinstance(self.completed, bool):
            raise TypeError("completed must be a boolean")
        issues = tuple(self.issues)
        evidence = tuple(self.evidence)
        if any(not isinstance(value, ValidationIssue) for value in issues):
            raise TypeError("issues must contain ValidationIssue values")
        if any(not isinstance(value, EvidenceRef) for value in evidence):
            raise TypeError("evidence must contain EvidenceRef values")
        object.__setattr__(self, "layer", layer)
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "input_sha256", input_sha256)
        object.__setattr__(self, "validator_id", validator_id)
        object.__setattr__(self, "validator_version", validator_version)
        object.__setattr__(self, "issues", issues)
        object.__setattr__(self, "evidence", evidence)

    @property
    def passed(self) -> bool:
        return self.completed and not any(
            issue.severity in {ValidationSeverity.ERROR, ValidationSeverity.CRITICAL}
            for issue in self.issues
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer.value,
            "profile_id": self.profile_id,
            "input_sha256": self.input_sha256,
            "validator_id": self.validator_id,
            "validator_version": self.validator_version,
            "completed": self.completed,
            "passed": self.passed,
            "issues": [issue.to_dict() for issue in self.issues],
            "evidence": [item.to_dict() for item in self.evidence],
        }


@runtime_checkable
class CanonicalValidationAdapter(Protocol):
    """Minimal boundary implemented by a future canonical-schema pipeline."""

    @property
    def document_id(self) -> str: ...

    @property
    def document_sha256(self) -> str: ...

    def validate_layer(
        self,
        layer: ValidationLayer,
        profile: "ValidationProfile",
    ) -> LayerResult: ...


@runtime_checkable
class StandardsRegistryAdapter(Protocol):
    """Subset implemented by ``RuleBundleRegistry`` and test adapters."""

    registry_id: str

    def bundle(self, bundle_id: str) -> Any: ...

    def artifacts_for(self, bundle_id: str) -> Sequence[Any]: ...

    def unverified_artifact_ids(self, bundle_id: str) -> Sequence[str]: ...

    def select(
        self, bundle_id: str, *, require_verified_artifacts: bool = True
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class ValidationProfile:
    profile_id: str
    kind: ValidationProfileKind
    rule_bundle_id: str
    publish_policy: PublishPolicy
    expected_rule_ids: tuple[str, ...]
    publication_enabled: bool
    require_executable_bundle: bool = True

    def __post_init__(self) -> None:
        profile_id = _identifier(self.profile_id, "profile ID")
        kind = _enum(self.kind, ValidationProfileKind, "profile kind")
        bundle_id = _identifier(self.rule_bundle_id, "rule bundle ID")
        publish_policy = _enum(
            self.publish_policy, PublishPolicy, "publish policy"
        )
        expected = tuple(
            _identifier(value, "expected rule ID")
            for value in self.expected_rule_ids
        )
        if len(expected) != len(set(expected)):
            raise ValueError("expected_rule_ids cannot contain duplicates")
        if not isinstance(self.publication_enabled, bool):
            raise TypeError("publication_enabled must be a boolean")
        if not isinstance(self.require_executable_bundle, bool):
            raise TypeError("require_executable_bundle must be a boolean")
        if kind is ValidationProfileKind.RESEARCH_IMPORT and self.publication_enabled:
            raise ValueError("research profiles cannot enable publication")
        if self.publication_enabled and not expected:
            raise ValueError("publishable profiles require expected rule IDs")
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "rule_bundle_id", bundle_id)
        object.__setattr__(self, "publish_policy", publish_policy)
        object.__setattr__(self, "expected_rule_ids", expected)


@dataclass(frozen=True, slots=True)
class CoverageRow:
    rule_id: str
    status: CoverageStatus
    source_reference: str
    reason: str = ""
    test_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        rule_id = _identifier(self.rule_id, "coverage rule ID")
        status = _enum(self.status, CoverageStatus, "coverage status")
        source_reference = str(self.source_reference).strip()
        reason = str(self.reason).strip()
        test_ids = tuple(
            _identifier(value, "coverage test ID") for value in self.test_ids
        )
        if not source_reference:
            raise ValueError("coverage source_reference is required")
        if status is CoverageStatus.IMPLEMENTED_TESTED and not test_ids:
            raise ValueError("implemented_tested rows require test IDs")
        if status in {
            CoverageStatus.FHWA_EXTERNAL,
            CoverageStatus.NOT_APPLICABLE,
            CoverageStatus.UNIMPLEMENTED,
        } and not reason:
            raise ValueError(f"{status.value} coverage rows require a reason")
        if len(test_ids) != len(set(test_ids)):
            raise ValueError("coverage test IDs cannot contain duplicates")
        object.__setattr__(self, "rule_id", rule_id)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "source_reference", source_reference)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "test_ids", test_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "status": self.status.value,
            "source_reference": self.source_reference,
            "reason": self.reason,
            "test_ids": list(self.test_ids),
        }


@dataclass(frozen=True, slots=True)
class CoverageRegistry:
    profile_id: str
    rows: tuple[CoverageRow, ...]
    registry_version: str

    def __post_init__(self) -> None:
        profile_id = _identifier(self.profile_id, "coverage profile ID")
        registry_version = _identifier(
            self.registry_version, "coverage registry version"
        )
        rows = tuple(self.rows)
        if any(not isinstance(value, CoverageRow) for value in rows):
            raise TypeError("coverage rows must contain CoverageRow values")
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "registry_version", registry_version)
        object.__setattr__(self, "rows", rows)

    @property
    def sha256(self) -> str:
        payload = {
            "profile_id": self.profile_id,
            "registry_version": self.registry_version,
            "rows": [row.to_dict() for row in self.rows],
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def evidence(self) -> EvidenceRef:
        return EvidenceRef(
            kind="coverage_registry",
            evidence_id=self.registry_version,
            sha256=self.sha256,
            attributes=(
                ("profile_id", self.profile_id),
                ("row_count", str(len(self.rows))),
            ),
        )


@dataclass(frozen=True, slots=True)
class DecisionReason:
    code: str
    message: str
    layer: ValidationLayer | None = None
    issue_id: str | None = None

    def __post_init__(self) -> None:
        code = _identifier(self.code, "decision code")
        message = str(self.message).strip()
        if not message:
            raise ValueError("decision message is required")
        layer = (
            _enum(self.layer, ValidationLayer, "decision layer")
            if self.layer is not None
            else None
        )
        issue_id = (
            _identifier(self.issue_id, "decision issue ID")
            if self.issue_id is not None
            else None
        )
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "layer", layer)
        object.__setattr__(self, "issue_id", issue_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "layer": self.layer.value if self.layer is not None else None,
            "issue_id": self.issue_id,
        }


@dataclass(frozen=True, slots=True)
class PublishDecision:
    allowed: bool
    blockers: tuple[DecisionReason, ...]
    warnings: tuple[DecisionReason, ...]
    evidence: tuple[EvidenceRef, ...]
    official_validation: bool = field(default=False, init=False)
    validation_label: str = field(
        default=BRIDGEGUARD_VALIDATION_LABEL, init=False
    )
    disclaimer: str = field(default=NOT_OFFICIAL_DISCLAIMER, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise TypeError("allowed must be a boolean")
        blockers = tuple(self.blockers)
        warnings = tuple(self.warnings)
        evidence = tuple(self.evidence)
        if any(not isinstance(value, DecisionReason) for value in blockers):
            raise TypeError("blockers must contain DecisionReason values")
        if any(not isinstance(value, DecisionReason) for value in warnings):
            raise TypeError("warnings must contain DecisionReason values")
        if any(not isinstance(value, EvidenceRef) for value in evidence):
            raise TypeError("evidence must contain EvidenceRef values")
        if self.allowed != (not blockers):
            raise ValueError("allowed must equal the absence of blockers")
        object.__setattr__(self, "blockers", blockers)
        object.__setattr__(self, "warnings", warnings)
        object.__setattr__(self, "evidence", evidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "blockers": [item.to_dict() for item in self.blockers],
            "warnings": [item.to_dict() for item in self.warnings],
            "evidence": [item.to_dict() for item in self.evidence],
            "official_validation": self.official_validation,
            "validation_label": self.validation_label,
            "disclaimer": self.disclaimer,
        }


@dataclass(frozen=True, slots=True)
class ValidationReport:
    document_id: str
    document_sha256: str
    profile_id: str
    orchestrator_version: str
    layers: tuple[LayerResult, ...]
    coverage: tuple[CoverageRow, ...]
    decision: PublishDecision
    official_validation: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        document_id = _identifier(self.document_id, "document ID")
        document_sha256 = _sha256(self.document_sha256, "document sha256")
        profile_id = _identifier(self.profile_id, "profile ID")
        orchestrator_version = _identifier(
            self.orchestrator_version, "orchestrator version"
        )
        layers = tuple(self.layers)
        coverage = tuple(self.coverage)
        if tuple(item.layer for item in layers) != VALIDATION_LAYER_ORDER:
            raise ValueError("validation report layers are missing or out of order")
        object.__setattr__(self, "document_id", document_id)
        object.__setattr__(self, "document_sha256", document_sha256)
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "orchestrator_version", orchestrator_version)
        object.__setattr__(self, "layers", layers)
        object.__setattr__(self, "coverage", coverage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "document_sha256": self.document_sha256,
            "profile_id": self.profile_id,
            "orchestrator_version": self.orchestrator_version,
            "official_validation": self.official_validation,
            "layers": [item.to_dict() for item in self.layers],
            "coverage": [row.to_dict() for row in self.coverage],
            "decision": self.decision.to_dict(),
        }


def evaluate_validation(
    adapter: CanonicalValidationAdapter,
    *,
    profile: ValidationProfile,
    standards_registry: StandardsRegistryAdapter,
    coverage_registry: CoverageRegistry,
) -> ValidationReport:
    """Run all layers in fixed order and produce a fail-closed decision."""

    document_id = _identifier(adapter.document_id, "document ID")
    document_sha256 = _sha256(adapter.document_sha256, "document sha256")
    blockers: list[DecisionReason] = []
    warnings: list[DecisionReason] = []
    evidence: list[EvidenceRef] = []

    registry_evidence, registry_blockers = _verify_standards_registry(
        standards_registry, profile
    )
    evidence.extend(registry_evidence)
    blockers.extend(registry_blockers)

    coverage_blockers, coverage_warnings = _assess_coverage(
        profile, coverage_registry
    )
    blockers.extend(coverage_blockers)
    warnings.extend(coverage_warnings)
    evidence.append(coverage_registry.evidence())

    layer_results: list[LayerResult] = []
    issue_ids: set[str] = set()
    evidence_by_key: dict[str, EvidenceRef] = {
        item.key: item for item in evidence
    }
    for layer in VALIDATION_LAYER_ORDER:
        result, result_blockers = _run_layer(
            adapter,
            layer=layer,
            profile=profile,
            document_sha256=document_sha256,
        )
        layer_results.append(result)
        blockers.extend(result_blockers)

        layer_evidence_keys: set[str] = set()
        for item in result.evidence:
            existing = evidence_by_key.get(item.key)
            if existing is not None and existing != item:
                blockers.append(
                    DecisionReason(
                        code="conflicting_evidence_id",
                        message="Two validation records used one evidence ID with different content.",
                        layer=layer,
                    )
                )
            elif existing is None:
                evidence_by_key[item.key] = item
                evidence.append(item)
            layer_evidence_keys.add(item.key)

        if not result.evidence:
            blockers.append(
                DecisionReason(
                    code="missing_layer_evidence",
                    message="A validation layer returned no immutable evidence.",
                    layer=layer,
                )
            )
        for issue in result.issues:
            if issue.issue_id in issue_ids:
                blockers.append(
                    DecisionReason(
                        code="duplicate_issue_id",
                        message="Validation issue IDs must be unique across layers.",
                        layer=layer,
                        issue_id=issue.issue_id,
                    )
                )
            issue_ids.add(issue.issue_id)
            missing_refs = sorted(set(issue.evidence_keys) - layer_evidence_keys)
            if missing_refs:
                blockers.append(
                    DecisionReason(
                        code="missing_issue_evidence",
                        message="A validation issue references evidence not emitted by its layer.",
                        layer=layer,
                        issue_id=issue.issue_id,
                    )
                )
            _route_issue(profile.publish_policy, layer, issue, blockers, warnings)

    if profile.kind is ValidationProfileKind.RESEARCH_IMPORT:
        blockers.append(
            DecisionReason(
                code="research_profile_non_publishable",
                message="Research-import validation can stage evidence but cannot publish.",
            )
        )
    elif not profile.publication_enabled:
        blockers.append(
            DecisionReason(
                code="profile_publication_disabled",
                message="Publication is disabled for the selected validation profile.",
            )
        )

    # External rows are accounted for, not executed by BridgeGuard. Preserve a
    # visible warning even when the internal publication gate passes.
    if any(row.status is CoverageStatus.FHWA_EXTERNAL for row in coverage_registry.rows):
        warnings.append(
            DecisionReason(
                code="external_fhwa_validation_required",
                message="One or more registered rules require external FHWA/NBTIS evaluation.",
            )
        )

    blockers = _deduplicate_reasons(blockers)
    warnings = _deduplicate_reasons(warnings)
    decision = PublishDecision(
        allowed=not blockers,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
        evidence=tuple(evidence),
    )
    return ValidationReport(
        document_id=document_id,
        document_sha256=document_sha256,
        profile_id=profile.profile_id,
        orchestrator_version=VALIDATION_ORCHESTRATOR_VERSION,
        layers=tuple(layer_results),
        coverage=coverage_registry.rows,
        decision=decision,
    )


def validation_report_json(report: ValidationReport) -> str:
    """Serialize a report deterministically for persistence or hashing."""

    if not isinstance(report, ValidationReport):
        raise TypeError("report must be a ValidationReport")
    return json.dumps(
        report.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _run_layer(
    adapter: CanonicalValidationAdapter,
    *,
    layer: ValidationLayer,
    profile: ValidationProfile,
    document_sha256: str,
) -> tuple[LayerResult, list[DecisionReason]]:
    blockers: list[DecisionReason] = []
    try:
        result = adapter.validate_layer(layer, profile)
    except Exception:
        result = _failed_layer_result(
            layer,
            profile,
            document_sha256,
            code="validator_execution_failed",
            message="The validation layer did not complete.",
        )
        blockers.append(
            DecisionReason(
                code="validator_execution_failed",
                message="A validation layer raised an exception.",
                layer=layer,
            )
        )
        return result, blockers

    if not isinstance(result, LayerResult):
        result = _failed_layer_result(
            layer,
            profile,
            document_sha256,
            code="invalid_layer_result",
            message="The validation layer returned an invalid result type.",
        )
        blockers.append(
            DecisionReason(
                code="invalid_layer_result",
                message="A validation layer returned an invalid result type.",
                layer=layer,
            )
        )
        return result, blockers
    if result.layer is not layer:
        blockers.append(
            DecisionReason(
                code="layer_identity_mismatch",
                message="A validator returned a result for a different layer.",
                layer=layer,
            )
        )
        # Preserve the returned content for audit, but keep the report's layer
        # sequence canonical so a mismatched adapter cannot corrupt the report.
        result = replace(result, layer=layer)
    if result.profile_id != profile.profile_id:
        blockers.append(
            DecisionReason(
                code="layer_profile_mismatch",
                message="A validator result belongs to a different profile.",
                layer=layer,
            )
        )
    if result.input_sha256 != document_sha256:
        blockers.append(
            DecisionReason(
                code="stale_layer_result",
                message="A validator result belongs to different document bytes.",
                layer=layer,
            )
        )
    if not result.completed:
        blockers.append(
            DecisionReason(
                code="layer_incomplete",
                message="A required validation layer is incomplete.",
                layer=layer,
            )
        )
    return result, blockers


def _failed_layer_result(
    layer: ValidationLayer,
    profile: ValidationProfile,
    document_sha256: str,
    *,
    code: str,
    message: str,
) -> LayerResult:
    issue = ValidationIssue(
        issue_id=f"orchestrator-{layer.value}-{code}",
        rule_id=f"orchestrator:{code}",
        severity=ValidationSeverity.CRITICAL,
        message=message,
    )
    return LayerResult(
        layer=layer,
        profile_id=profile.profile_id,
        input_sha256=document_sha256,
        validator_id="orchestrator_failure_adapter",
        validator_version=VALIDATION_ORCHESTRATOR_VERSION,
        completed=False,
        issues=(issue,),
        evidence=(),
    )


def _verify_standards_registry(
    registry: StandardsRegistryAdapter,
    profile: ValidationProfile,
) -> tuple[list[EvidenceRef], list[DecisionReason]]:
    evidence: list[EvidenceRef] = []
    blockers: list[DecisionReason] = []
    try:
        missing = tuple(registry.unverified_artifact_ids(profile.rule_bundle_id))
        artifacts = tuple(registry.artifacts_for(profile.rule_bundle_id))
        if missing:
            blockers.append(
                DecisionReason(
                    code="unverified_standards_artifacts",
                    message="The selected rule bundle has unverified local artifacts.",
                )
            )
        selected = registry.select(
            profile.rule_bundle_id, require_verified_artifacts=True
        )
        if getattr(selected, "id", None) != profile.rule_bundle_id:
            raise ValueError("selected bundle mismatch")
        if not artifacts:
            raise ValueError("rule bundle has no artifacts")
        for artifact in artifacts:
            state = _value(getattr(artifact, "state", None))
            digest = getattr(artifact, "sha256", None)
            artifact_id = getattr(artifact, "id", None)
            if (
                state != "vendored_verified"
                or not isinstance(digest, str)
                or not _SHA256_RE.fullmatch(digest)
                or not isinstance(artifact_id, str)
            ):
                blockers.append(
                    DecisionReason(
                        code="unverified_standards_artifacts",
                        message="The selected rule bundle has unverified local artifacts.",
                    )
                )
                continue
            evidence.append(
                EvidenceRef(
                    kind="standards_artifact",
                    evidence_id=artifact_id,
                    sha256=digest,
                    attributes=(
                        ("bundle_id", profile.rule_bundle_id),
                        ("state", state),
                    ),
                )
            )
        implementation_status = _value(
            getattr(selected, "implementation_status", None)
        )
        if profile.require_executable_bundle and implementation_status in {
            "",
            "registry_only",
            "metadata_only",
            "unimplemented",
        }:
            blockers.append(
                DecisionReason(
                    code="rule_bundle_not_executable",
                    message="The selected standards bundle is not registered as executable.",
                )
            )
    except Exception:
        blockers.append(
            DecisionReason(
                code="standards_registry_verification_failed",
                message="The selected standards bundle could not be verified locally.",
            )
        )
    return evidence, _deduplicate_reasons(blockers)


def _assess_coverage(
    profile: ValidationProfile,
    coverage: CoverageRegistry,
) -> tuple[list[DecisionReason], list[DecisionReason]]:
    blockers: list[DecisionReason] = []
    warnings: list[DecisionReason] = []
    if coverage.profile_id != profile.profile_id:
        blockers.append(
            DecisionReason(
                code="coverage_profile_mismatch",
                message="The rule-coverage registry belongs to a different profile.",
            )
        )
    expected = set(profile.expected_rule_ids)
    seen: set[str] = set()
    duplicates: set[str] = set()
    for row in coverage.rows:
        if row.rule_id in seen:
            duplicates.add(row.rule_id)
        seen.add(row.rule_id)
    missing = expected - seen
    unknown = seen - expected
    if duplicates:
        blockers.append(
            DecisionReason(
                code="duplicate_coverage_rows",
                message="The rule-coverage registry contains duplicate rule rows.",
            )
        )
    if missing:
        blockers.append(
            DecisionReason(
                code="missing_coverage_rows",
                message="Expected official-rule rows are missing from the coverage registry.",
            )
        )
    if unknown:
        blockers.append(
            DecisionReason(
                code="unknown_coverage_rows",
                message="The coverage registry contains rules not declared by the profile.",
            )
        )
    if any(row.status is CoverageStatus.UNIMPLEMENTED for row in coverage.rows):
        blockers.append(
            DecisionReason(
                code="unimplemented_official_rules",
                message="One or more applicable official rules remain unimplemented.",
            )
        )
    if any(row.status is CoverageStatus.FHWA_EXTERNAL for row in coverage.rows):
        warnings.append(
            DecisionReason(
                code="coverage_requires_external_evaluation",
                message="The coverage registry delegates one or more rules to FHWA/NBTIS.",
            )
        )
    return _deduplicate_reasons(blockers), _deduplicate_reasons(warnings)


def _route_issue(
    policy: PublishPolicy,
    layer: ValidationLayer,
    issue: ValidationIssue,
    blockers: list[DecisionReason],
    warnings: list[DecisionReason],
) -> None:
    reason = DecisionReason(
        code=f"validation_{issue.severity.value}",
        message=issue.message,
        layer=layer,
        issue_id=issue.issue_id,
    )
    if issue.severity is ValidationSeverity.CRITICAL:
        blockers.append(reason)
    elif issue.severity is ValidationSeverity.ERROR:
        if policy is PublishPolicy.PUBLISH_GATE:
            blockers.append(reason)
        else:
            warnings.append(reason)
    elif issue.severity is ValidationSeverity.WARNING:
        warnings.append(reason)


def _deduplicate_reasons(values: Sequence[DecisionReason]) -> list[DecisionReason]:
    result: list[DecisionReason] = []
    seen: set[tuple[Any, ...]] = set()
    for value in values:
        key = (value.code, value.message, value.layer, value.issue_id)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "")


def _identifier(value: Any, field_name: str) -> str:
    text = str(value).strip()
    if not _IDENTIFIER_RE.fullmatch(text):
        raise ValueError(f"{field_name} is invalid")
    return text


def _sha256(value: Any, field_name: str) -> str:
    text = str(value)
    if not _SHA256_RE.fullmatch(text):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return text


def _enum(value: Any, enum_type: type[Enum], field_name: str):
    try:
        return value if isinstance(value, enum_type) else enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} is invalid") from exc
