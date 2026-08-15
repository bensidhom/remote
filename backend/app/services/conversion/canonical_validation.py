"""BridgeGuard-owned validation checks for a normalized canonical document.

This module adapts :class:`CanonicalNormalizationResult` to the layered
orchestrator in :mod:`validation_profiles`.  It deliberately does *not* import
or call the legacy SNBI validator.  Pinned-schema results come only from the
fail-closed registry-backed executor; the executable-rule layer stays
incomplete until independently tested rule executors are integrated.

The completed layers here are internal BridgeGuard checks only: canonical JSON
resource/shape diagnostics, exact application-projection round trips,
transition lineage reconciliation, and workspace authorization policy.  No
result produced by this adapter is an FHWA or NBTIS validation result.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .canonical_schema import CanonicalNormalizationResult
from .pinned_schema import PinnedSchemaResult, SchemaExecutionStatus
from .transition_engine import (
    TransitionCoverage,
    TransitionIssue,
    TransitionIssueSeverity,
)
from .transition_values import TransitionValue
from .validation_profiles import (
    EvidenceRef,
    LayerResult,
    ValidationIssue,
    ValidationLayer,
    ValidationProfile,
    ValidationProfileKind,
    ValidationSeverity,
)


CANONICAL_VALIDATION_ADAPTER_VERSION = "canonical-validation-adapter-v1"
_SIMPLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")


@dataclass(frozen=True, slots=True)
class ProjectionIntegritySnapshot:
    """Evidence returned by a BridgeGuard application projection round trip.

    ``round_trip_document`` is the canonical document reconstructed from the
    application's own persistence projection.  An omitted document means the
    check was not executed; it never means that the projection passed.
    """

    round_trip_document: Mapping[str, Any] | None
    missing_json_pointers: tuple[str, ...] = ()
    orphan_record_ids: tuple[str, ...] = ()
    duplicate_record_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.round_trip_document is not None and not isinstance(
            self.round_trip_document, Mapping
        ):
            raise TypeError("round_trip_document must be a mapping or None")
        pointers = tuple(str(value) for value in self.missing_json_pointers)
        if any(not value.startswith("/") for value in pointers):
            raise ValueError("missing_json_pointers must contain RFC 6901 pointers")
        orphan_ids = _nonblank_tuple(self.orphan_record_ids, "orphan record ID")
        duplicate_keys = _nonblank_tuple(
            self.duplicate_record_keys, "duplicate record key"
        )
        object.__setattr__(self, "missing_json_pointers", pointers)
        object.__setattr__(self, "orphan_record_ids", orphan_ids)
        object.__setattr__(self, "duplicate_record_keys", duplicate_keys)


@dataclass(frozen=True, slots=True)
class WorkspacePolicyContext:
    """Immutable authorization facts used by the BridgeGuard workspace layer."""

    workspace_id: str
    authorized_profile_ids: tuple[str, ...]
    authorized_bridge_ids: tuple[str, ...]
    authorized_state_codes: tuple[str, ...] = ()
    require_approval: bool = False
    approval_evidence_id: str | None = None

    def __post_init__(self) -> None:
        workspace_id = _nonblank(self.workspace_id, "workspace_id")
        profiles = _nonblank_tuple(
            self.authorized_profile_ids, "authorized profile ID"
        )
        bridges = _nonblank_tuple(
            self.authorized_bridge_ids, "authorized bridge ID"
        )
        states = _nonblank_tuple(
            self.authorized_state_codes, "authorized state code"
        )
        if not profiles:
            raise ValueError("authorized_profile_ids cannot be empty")
        if not bridges:
            raise ValueError("authorized_bridge_ids cannot be empty")
        if len(profiles) != len(set(profiles)):
            raise ValueError("authorized_profile_ids cannot contain duplicates")
        if len(bridges) != len(set(bridges)):
            raise ValueError("authorized_bridge_ids cannot contain duplicates")
        if len(states) != len(set(states)):
            raise ValueError("authorized_state_codes cannot contain duplicates")
        if not isinstance(self.require_approval, bool):
            raise TypeError("require_approval must be a boolean")
        approval = (
            _nonblank(self.approval_evidence_id, "approval_evidence_id")
            if self.approval_evidence_id is not None
            else None
        )
        object.__setattr__(self, "workspace_id", workspace_id)
        object.__setattr__(self, "authorized_profile_ids", profiles)
        object.__setattr__(self, "authorized_bridge_ids", bridges)
        object.__setattr__(self, "authorized_state_codes", states)
        object.__setattr__(self, "approval_evidence_id", approval)


class CanonicalDocumentValidationAdapter:
    """Concrete adapter from canonical normalization to layered validation.

    Constructor inputs are snapshotted into deterministic JSON records so a
    caller mutating its dictionaries after construction cannot change the
    document digest or the evidence emitted during the validation run.
    """

    def __init__(
        self,
        *,
        document_id: str,
        normalization: CanonicalNormalizationResult,
        projection: ProjectionIntegritySnapshot,
        workspace_policy: WorkspacePolicyContext,
        transition_lineage: Mapping[str, TransitionValue] | None = None,
        transition_issues: Sequence[TransitionIssue] = (),
        transition_coverage: TransitionCoverage | None = None,
        pinned_schema_result: PinnedSchemaResult | None = None,
    ) -> None:
        if not isinstance(normalization, CanonicalNormalizationResult):
            raise TypeError("normalization must be a CanonicalNormalizationResult")
        if not isinstance(projection, ProjectionIntegritySnapshot):
            raise TypeError("projection must be a ProjectionIntegritySnapshot")
        if not isinstance(workspace_policy, WorkspacePolicyContext):
            raise TypeError("workspace_policy must be a WorkspacePolicyContext")

        normalized_id = _nonblank(document_id, "document_id")
        if not _SIMPLE_ID_RE.fullmatch(normalized_id):
            raise ValueError(
                "document_id must use only letters, digits, dot, underscore, colon, or hyphen"
            )

        canonical_bytes = _canonical_json_bytes(normalization.normalized_document)
        canonical_document = json.loads(canonical_bytes.decode("utf-8"))
        resource_record = _resource_record(normalization)

        lineage = transition_lineage or {}
        if not isinstance(lineage, Mapping):
            raise TypeError("transition_lineage must be a mapping")
        lineage_records: list[tuple[str, dict[str, Any]]] = []
        for path, value in lineage.items():
            if not isinstance(path, str):
                raise TypeError("transition lineage paths must be strings")
            if not isinstance(value, TransitionValue):
                raise TypeError("transition_lineage values must be TransitionValue values")
            lineage_records.append((path, value.to_lineage_dict()))
        lineage_records.sort(key=lambda item: item[0])

        issue_records: list[dict[str, Any]] = []
        for issue in transition_issues:
            if not isinstance(issue, TransitionIssue):
                raise TypeError("transition_issues must contain TransitionIssue values")
            issue_records.append(issue.to_dict())
        issue_records.sort(key=_canonical_sort_key)

        if transition_coverage is not None and not isinstance(
            transition_coverage, TransitionCoverage
        ):
            raise TypeError("transition_coverage must be a TransitionCoverage or None")
        if pinned_schema_result is not None and not isinstance(
            pinned_schema_result, PinnedSchemaResult
        ):
            raise TypeError("pinned_schema_result must be a PinnedSchemaResult or None")

        projection_document = None
        if projection.round_trip_document is not None:
            projection_bytes = _canonical_json_bytes(projection.round_trip_document)
            projection_document = json.loads(projection_bytes.decode("utf-8"))

        self._document_id = normalized_id
        self._document_sha256 = hashlib.sha256(canonical_bytes).hexdigest()
        self._canonical_document = canonical_document
        self._resource_record = resource_record
        self._projection_document = projection_document
        self._projection_missing = tuple(sorted(projection.missing_json_pointers))
        self._projection_orphans = tuple(sorted(projection.orphan_record_ids))
        self._projection_duplicates = tuple(sorted(projection.duplicate_record_keys))
        self._workspace = workspace_policy
        self._lineage_records = tuple(lineage_records)
        self._transition_issue_records = tuple(issue_records)
        self._transition_coverage = (
            _json_snapshot(transition_coverage.to_dict())
            if transition_coverage is not None
            else None
        )
        self._pinned_schema_result = (
            _json_snapshot(pinned_schema_result.to_dict())
            if pinned_schema_result is not None
            else None
        )

    @property
    def document_id(self) -> str:
        return self._document_id

    @property
    def document_sha256(self) -> str:
        return self._document_sha256

    def validate_layer(
        self,
        layer: ValidationLayer,
        profile: ValidationProfile,
    ) -> LayerResult:
        if not isinstance(profile, ValidationProfile):
            raise TypeError("profile must be a ValidationProfile")
        try:
            selected_layer = ValidationLayer(layer)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unsupported validation layer: {layer!r}") from exc

        handlers = {
            ValidationLayer.JSON_RESOURCE: self._validate_json_resource,
            ValidationLayer.PINNED_SCHEMA: self._validate_pinned_schema,
            ValidationLayer.PROJECTION_INTEGRITY: self._validate_projection,
            ValidationLayer.OFFICIAL_RULES: self._official_rules_unavailable,
            ValidationLayer.TRANSITION_QA: self._validate_transition,
            ValidationLayer.WORKSPACE_POLICY: self._validate_workspace,
        }
        return handlers[selected_layer](profile)

    def _validate_json_resource(self, profile: ValidationProfile) -> LayerResult:
        payload = {
            "canonical_sha256": self.document_sha256,
            "resource": self._resource_record,
        }
        evidence = self._evidence(
            ValidationLayer.JSON_RESOURCE,
            profile,
            payload,
            status="executed",
        )
        issues = _IssueCollector(ValidationLayer.JSON_RESOURCE, evidence)

        if not isinstance(self._canonical_document, dict):
            issues.add(
                "BG-CANONICAL-ROOT",
                ValidationSeverity.ERROR,
                "BridgeGuard canonical normalization did not produce an object root.",
                json_pointer="",
            )
        if self._resource_record["node_count"] < 1:
            issues.add(
                "BG-CANONICAL-RESOURCE-COUNT",
                ValidationSeverity.ERROR,
                "Canonical resource accounting must include at least the root node.",
            )

        for record in self._resource_record["unknown_tags"]:
            issues.add(
                "BG-CANONICAL-UNKNOWN-TAG",
                _diagnostic_severity(record["severity"]),
                f"{record['message']}: {record['tag']}",
                json_pointer=record["path"],
            )
        for record in self._resource_record["wrong_container_types"]:
            issues.add(
                "BG-CANONICAL-CONTAINER-TYPE",
                _diagnostic_severity(record["severity"]),
                (
                    "BridgeGuard canonical shape expected "
                    f"{record['expected']} but received {record['actual']}."
                ),
                json_pointer=record["path"],
            )
        for record in self._resource_record["rejected_values"]:
            issues.add(
                f"BG-CANONICAL-REJECTED-{_rule_token(record['code'])}",
                _diagnostic_severity(record["severity"]),
                record["message"],
                json_pointer=record["path"],
            )
        for record in self._resource_record["duplicate_logical_child_keys"]:
            issues.add(
                "BG-CANONICAL-DUPLICATE-LOGICAL-KEY",
                _diagnostic_severity(record["severity"]),
                (
                    "BridgeGuard found duplicate logical child keys at "
                    f"{record['first_path']} and {record['duplicate_path']}."
                ),
                json_pointer=record["duplicate_path"],
            )
        for record in self._resource_record["coercions"]:
            issues.add(
                "BG-CANONICAL-COERCION",
                ValidationSeverity.INFO,
                f"BridgeGuard applied the explicit {record['kind']} normalization.",
                json_pointer=record["path"],
            )
        if self._resource_record["submission_ready"]:
            issues.add(
                "BG-CANONICAL-READINESS-INVARIANT",
                ValidationSeverity.CRITICAL,
                (
                    "The normalization result asserted submission readiness while "
                    "its pinned schema remains unavailable/unverified."
                ),
            )

        return self._result(
            ValidationLayer.JSON_RESOURCE,
            profile,
            completed=True,
            issues=issues.items,
            evidence=evidence,
        )

    def _validate_pinned_schema(self, profile: ValidationProfile) -> LayerResult:
        execution = self._pinned_schema_result
        if execution is None:
            execution = {
                "status": "unavailable_unverified",
                "schema_executed": False,
                "valid": False,
                "issues": [],
                "detail": (
                    "No registry-backed pinned-schema execution result was supplied."
                ),
            }
        status = str(execution["status"])
        completed = bool(execution.get("schema_executed"))
        payload = {
            "canonical_sha256": self.document_sha256,
            "normalization_schema_status": self._resource_record["schema_status"],
            "execution_status": status,
            "schema_executed": completed,
            "execution": execution,
        }
        evidence = self._evidence(
            ValidationLayer.PINNED_SCHEMA,
            profile,
            payload,
            status=(
                f"executed_{status}"
                if completed
                else status
            ),
            extra_attributes={
                key: execution.get(key)
                for key in (
                    "bundle_id",
                    "artifact_id",
                    "dialect",
                    "schema_sha256",
                )
                if execution.get(key) is not None
            },
        )
        issues = _IssueCollector(ValidationLayer.PINNED_SCHEMA, evidence)
        if not completed:
            detail = str(execution.get("detail") or "No schema validation was performed.")
            dialect = execution.get("dialect")
            dialect_note = (
                f" Declared dialect: {dialect}." if dialect else ""
            )
            issues.add(
                "BG-PINNED-SCHEMA-AVAILABILITY",
                ValidationSeverity.CRITICAL,
                (
                    f"Pinned schema execution did not run ({status}): {detail}."
                    f"{dialect_note} "
                    "No schema validation was performed."
                ),
            )
        elif status == SchemaExecutionStatus.INVALID.value:
            schema_issues = execution.get("issues") or []
            for schema_issue in schema_issues:
                keyword = _rule_token(str(schema_issue.get("keyword") or "schema"))
                issues.add(
                    f"BG-PINNED-SCHEMA-{keyword}",
                    ValidationSeverity.ERROR,
                    str(schema_issue.get("message") or "Pinned schema validation failed."),
                    json_pointer=str(schema_issue.get("instance_pointer") or ""),
                )
            if not schema_issues:
                issues.add(
                    "BG-PINNED-SCHEMA-INVALID",
                    ValidationSeverity.ERROR,
                    "Pinned schema validation failed without a reported schema issue.",
                )
        elif status != SchemaExecutionStatus.VALID.value:
            # Defensive invariant: only VALID and INVALID may claim execution.
            issues.add(
                "BG-PINNED-SCHEMA-EXECUTION-INVARIANT",
                ValidationSeverity.CRITICAL,
                f"Pinned schema status {status!r} incorrectly claimed execution.",
            )
            completed = False
        return self._result(
            ValidationLayer.PINNED_SCHEMA,
            profile,
            completed=completed,
            issues=issues.items,
            evidence=evidence,
        )

    def _validate_projection(self, profile: ValidationProfile) -> LayerResult:
        projected_sha256 = (
            canonical_document_sha256(self._projection_document)
            if self._projection_document is not None
            else None
        )
        payload = {
            "canonical_sha256": self.document_sha256,
            "round_trip_sha256": projected_sha256,
            "missing_json_pointers": list(self._projection_missing),
            "orphan_record_ids": list(self._projection_orphans),
            "duplicate_record_keys": list(self._projection_duplicates),
        }
        completed = self._projection_document is not None
        evidence = self._evidence(
            ValidationLayer.PROJECTION_INTEGRITY,
            profile,
            payload,
            status="executed" if completed else "unavailable",
        )
        issues = _IssueCollector(ValidationLayer.PROJECTION_INTEGRITY, evidence)
        if not completed:
            issues.add(
                "BG-PROJECTION-EVIDENCE",
                ValidationSeverity.ERROR,
                "No BridgeGuard projection round-trip document was supplied.",
            )
        elif projected_sha256 != self.document_sha256:
            issues.add(
                "BG-PROJECTION-ROUND-TRIP",
                ValidationSeverity.ERROR,
                "The BridgeGuard persistence projection did not round-trip exactly.",
            )
        for pointer in self._projection_missing:
            issues.add(
                "BG-PROJECTION-MISSING-PATH",
                ValidationSeverity.ERROR,
                "The BridgeGuard projection omitted a canonical path.",
                json_pointer=pointer,
            )
        for orphan in self._projection_orphans:
            issues.add(
                "BG-PROJECTION-ORPHAN",
                ValidationSeverity.ERROR,
                f"The BridgeGuard projection contains orphan record {orphan!r}.",
            )
        for key in self._projection_duplicates:
            issues.add(
                "BG-PROJECTION-DUPLICATE",
                ValidationSeverity.ERROR,
                f"The BridgeGuard projection contains duplicate record key {key!r}.",
            )
        return self._result(
            ValidationLayer.PROJECTION_INTEGRITY,
            profile,
            completed=completed,
            issues=issues.items,
            evidence=evidence,
        )

    def _official_rules_unavailable(self, profile: ValidationProfile) -> LayerResult:
        payload = {
            "canonical_sha256": self.document_sha256,
            "requested_rule_bundle_id": profile.rule_bundle_id,
            "execution_status": "unavailable_unverified",
            "rules_executed": False,
        }
        evidence = self._evidence(
            ValidationLayer.OFFICIAL_RULES,
            profile,
            payload,
            status="unavailable_unverified",
        )
        issues = _IssueCollector(ValidationLayer.OFFICIAL_RULES, evidence)
        issues.add(
            "BG-EXECUTABLE-RULES-AVAILABILITY",
            ValidationSeverity.CRITICAL,
            (
                "The requested external rule bundle has no verified executable "
                "implementation in this adapter; no external rules were executed."
            ),
        )
        return self._result(
            ValidationLayer.OFFICIAL_RULES,
            profile,
            completed=False,
            issues=issues.items,
            evidence=evidence,
        )

    def _validate_transition(self, profile: ValidationProfile) -> LayerResult:
        lineage_payload = {
            path: record for path, record in self._lineage_records
        }
        payload = {
            "canonical_sha256": self.document_sha256,
            "lineage": lineage_payload,
            "issues": list(self._transition_issue_records),
            "coverage": self._transition_coverage,
        }
        transition_expected = profile.kind is ValidationProfileKind.RESEARCH_IMPORT
        has_transition_input = bool(
            self._lineage_records
            or self._transition_issue_records
            or self._transition_coverage is not None
        )
        completed = (
            self._transition_coverage is not None
            if has_transition_input
            else not transition_expected
        )
        evidence = self._evidence(
            ValidationLayer.TRANSITION_QA,
            profile,
            payload,
            status="executed" if completed else "incomplete",
        )
        issues = _IssueCollector(ValidationLayer.TRANSITION_QA, evidence)

        if transition_expected and not has_transition_input:
            issues.add(
                "BG-TRANSITION-EVIDENCE",
                ValidationSeverity.ERROR,
                "The research-import profile requires transition lineage and coverage evidence.",
            )
        elif has_transition_input and self._transition_coverage is None:
            issues.add(
                "BG-TRANSITION-COVERAGE",
                ValidationSeverity.ERROR,
                "Transition input was supplied without a reconciliation coverage record.",
            )
        elif not has_transition_input:
            issues.add(
                "BG-TRANSITION-NOT-APPLICABLE",
                ValidationSeverity.INFO,
                "No NBI-to-SNBI transition data was supplied for this canonical document.",
            )

        leaves = _json_pointer_leaves(self._canonical_document)
        lineage_paths = {path for path, _record in self._lineage_records}
        for path, record in self._lineage_records:
            if not path.startswith("/"):
                issues.add(
                    "BG-TRANSITION-LINEAGE-POINTER",
                    ValidationSeverity.ERROR,
                    f"Transition lineage path {path!r} is not an RFC 6901 pointer.",
                )
                continue
            if path not in leaves:
                issues.add(
                    "BG-TRANSITION-LINEAGE-EXTRA",
                    ValidationSeverity.ERROR,
                    "Transition lineage references a path absent from the canonical document.",
                    json_pointer=path,
                )
            final_token = _pointer_final_token(path)
            if record["target_tag"] != final_token:
                issues.add(
                    "BG-TRANSITION-TARGET-TAG",
                    ValidationSeverity.ERROR,
                    (
                        f"Lineage target tag {record['target_tag']!r} does not match "
                        f"the canonical leaf {final_token!r}."
                    ),
                    json_pointer=path,
                )

        missing = sorted(leaves - lineage_paths)
        if transition_expected or has_transition_input:
            for path in missing:
                issues.add(
                    "BG-TRANSITION-LINEAGE-MISSING",
                    ValidationSeverity.ERROR,
                    "A canonical leaf has no transition lineage record.",
                    json_pointer=path,
                )

        for record in self._transition_issue_records:
            pointer = record.get("target_path")
            if pointer is not None and not str(pointer).startswith("/"):
                issues.add(
                    "BG-TRANSITION-ISSUE-POINTER",
                    ValidationSeverity.ERROR,
                    "A transition issue contained an invalid target pointer.",
                )
                pointer = None
            issues.add(
                f"BG-TRANSITION-{_rule_token(record['code'])}",
                _transition_severity(record["severity"]),
                record["message"],
                json_pointer=pointer,
            )

        if self._transition_coverage is not None:
            self._assess_transition_coverage(
                self._transition_coverage,
                leaves=leaves,
                lineage_paths=lineage_paths,
                issues=issues,
            )

        return self._result(
            ValidationLayer.TRANSITION_QA,
            profile,
            completed=completed,
            issues=issues.items,
            evidence=evidence,
        )

    def _assess_transition_coverage(
        self,
        coverage: Mapping[str, Any],
        *,
        leaves: set[str],
        lineage_paths: set[str],
        issues: "_IssueCollector",
    ) -> None:
        expected = {
            "document_leaf_count": len(leaves),
            "lineage_leaf_count": len(lineage_paths),
            "uncovered_leaf_count": len(leaves - lineage_paths),
        }
        for name, expected_value in expected.items():
            if coverage[name] != expected_value:
                issues.add(
                    "BG-TRANSITION-COVERAGE-RECONCILIATION",
                    ValidationSeverity.ERROR,
                    (
                        f"Transition coverage {name}={coverage[name]!r} does not "
                        f"match the calculated value {expected_value}."
                    ),
                )
        for name, value in coverage.items():
            if name in {"status_counts", "category_counts"}:
                if any(
                    isinstance(count, bool)
                    or not isinstance(count, int)
                    or count < 0
                    for count in value.values()
                ):
                    issues.add(
                        "BG-TRANSITION-COVERAGE-NONNEGATIVE",
                        ValidationSeverity.ERROR,
                        f"Transition coverage {name} contains an invalid count.",
                    )
                elif sum(value.values()) != len(lineage_paths):
                    issues.add(
                        "BG-TRANSITION-COVERAGE-TOTAL",
                        ValidationSeverity.ERROR,
                        f"Transition coverage {name} does not total the lineage count.",
                    )
            elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
                issues.add(
                    "BG-TRANSITION-COVERAGE-NONNEGATIVE",
                    ValidationSeverity.ERROR,
                    f"Transition coverage {name} must be a nonnegative integer.",
                )
        if coverage["internal_derived_leaf_count"] > len(lineage_paths):
            issues.add(
                "BG-TRANSITION-COVERAGE-DERIVED",
                ValidationSeverity.ERROR,
                "Internal-derived lineage count exceeds the total lineage count.",
            )
        if coverage["mapping_exception_count"]:
            issues.add(
                "BG-TRANSITION-MAPPING-EXCEPTION",
                ValidationSeverity.ERROR,
                "Transition coverage reports one or more mapping exceptions.",
            )
        if coverage["populated_temporary_count"]:
            issues.add(
                "BG-TRANSITION-TEMPORARY",
                ValidationSeverity.WARNING,
                "Transition coverage includes temporary values requiring review.",
            )
        if coverage["assumed_generated_count"]:
            issues.add(
                "BG-TRANSITION-ASSUMED-GENERATED",
                ValidationSeverity.WARNING,
                "Transition coverage includes assumed or generated values requiring review.",
            )

    def _validate_workspace(self, profile: ValidationProfile) -> LayerResult:
        bridge_id = self._canonical_document.get("BID01")
        state_code = self._canonical_document.get("BL01")
        payload = {
            "canonical_sha256": self.document_sha256,
            "workspace_id": self._workspace.workspace_id,
            "normalization_profile_id": self._resource_record["profile_id"],
            "requested_profile_id": profile.profile_id,
            "bridge_id": bridge_id,
            "state_code": state_code,
            "authorized_profile_ids": list(self._workspace.authorized_profile_ids),
            "authorized_bridge_ids": list(self._workspace.authorized_bridge_ids),
            "authorized_state_codes": list(self._workspace.authorized_state_codes),
            "require_approval": self._workspace.require_approval,
            "approval_evidence_id": self._workspace.approval_evidence_id,
        }
        evidence = self._evidence(
            ValidationLayer.WORKSPACE_POLICY,
            profile,
            payload,
            status="executed",
        )
        issues = _IssueCollector(ValidationLayer.WORKSPACE_POLICY, evidence)
        if self._resource_record["profile_id"] != profile.profile_id:
            issues.add(
                "BG-WORKSPACE-NORMALIZATION-PROFILE",
                ValidationSeverity.ERROR,
                "The normalization profile does not match the requested validation profile.",
            )
        if profile.profile_id not in self._workspace.authorized_profile_ids:
            issues.add(
                "BG-WORKSPACE-PROFILE-AUTHORIZATION",
                ValidationSeverity.ERROR,
                "The workspace is not authorized for the requested validation profile.",
            )
        if bridge_id is None:
            issues.add(
                "BG-WORKSPACE-BRIDGE-IDENTITY",
                ValidationSeverity.ERROR,
                "The canonical document has no BID01 bridge identifier for workspace scoping.",
                json_pointer="/BID01",
            )
        elif str(bridge_id) not in self._workspace.authorized_bridge_ids:
            issues.add(
                "BG-WORKSPACE-BRIDGE-AUTHORIZATION",
                ValidationSeverity.ERROR,
                "The canonical bridge is outside the workspace authorization scope.",
                json_pointer="/BID01",
            )
        if self._workspace.authorized_state_codes:
            if state_code is None:
                issues.add(
                    "BG-WORKSPACE-STATE-SCOPE",
                    ValidationSeverity.ERROR,
                    "The canonical document has no BL01 value required by workspace state scope.",
                    json_pointer="/BL01",
                )
            elif str(state_code) not in self._workspace.authorized_state_codes:
                issues.add(
                    "BG-WORKSPACE-STATE-AUTHORIZATION",
                    ValidationSeverity.ERROR,
                    "The canonical bridge state is outside the workspace authorization scope.",
                    json_pointer="/BL01",
                )
        if self._workspace.require_approval and not self._workspace.approval_evidence_id:
            issues.add(
                "BG-WORKSPACE-APPROVAL",
                ValidationSeverity.ERROR,
                "Workspace policy requires explicit approval evidence for this revision.",
            )
        return self._result(
            ValidationLayer.WORKSPACE_POLICY,
            profile,
            completed=True,
            issues=issues.items,
            evidence=evidence,
        )

    def _evidence(
        self,
        layer: ValidationLayer,
        profile: ValidationProfile,
        payload: Mapping[str, Any],
        *,
        status: str,
        extra_attributes: Mapping[str, Any] | None = None,
    ) -> EvidenceRef:
        envelope = {
            "adapter_version": CANONICAL_VALIDATION_ADAPTER_VERSION,
            "document_sha256": self.document_sha256,
            "layer": layer.value,
            "profile": {
                "profile_id": profile.profile_id,
                "kind": profile.kind.value,
                "rule_bundle_id": profile.rule_bundle_id,
            },
            "payload": payload,
        }
        digest = hashlib.sha256(_canonical_json_bytes(envelope)).hexdigest()
        attributes: dict[str, str] = {
            "adapter_version": CANONICAL_VALIDATION_ADAPTER_VERSION,
            "document_sha256": self.document_sha256,
            "layer": layer.value,
            "profile_id": profile.profile_id,
            "status": status,
        }
        if extra_attributes:
            for key, value in extra_attributes.items():
                normalized_key = _nonblank(key, "evidence attribute key")
                if normalized_key in attributes:
                    raise ValueError(
                        f"duplicate evidence attribute key: {normalized_key}"
                    )
                attributes[normalized_key] = str(value)
        return EvidenceRef(
            kind="bridgeguard_validation_input",
            evidence_id=(
                f"{layer.value}-{self.document_sha256[:12]}-{digest[:12]}"
            ),
            sha256=digest,
            attributes=tuple(sorted(attributes.items())),
        )

    def _result(
        self,
        layer: ValidationLayer,
        profile: ValidationProfile,
        *,
        completed: bool,
        issues: tuple[ValidationIssue, ...],
        evidence: EvidenceRef,
    ) -> LayerResult:
        return LayerResult(
            layer=layer,
            profile_id=profile.profile_id,
            input_sha256=self.document_sha256,
            validator_id=f"bridgeguard_{layer.value}",
            validator_version=CANONICAL_VALIDATION_ADAPTER_VERSION,
            completed=completed,
            issues=issues,
            evidence=(evidence,),
        )


class _IssueCollector:
    def __init__(self, layer: ValidationLayer, evidence: EvidenceRef) -> None:
        self._layer = layer
        self._evidence = evidence
        self._items: list[ValidationIssue] = []

    @property
    def items(self) -> tuple[ValidationIssue, ...]:
        return tuple(self._items)

    def add(
        self,
        rule_id: str,
        severity: ValidationSeverity,
        message: str,
        *,
        json_pointer: str | None = None,
    ) -> None:
        ordinal = len(self._items)
        seed = {
            "layer": self._layer.value,
            "ordinal": ordinal,
            "rule_id": rule_id,
            "severity": severity.value,
            "message": message,
            "json_pointer": json_pointer,
        }
        suffix = hashlib.sha256(_canonical_json_bytes(seed)).hexdigest()[:20]
        self._items.append(
            ValidationIssue(
                issue_id=f"{self._layer.value}-{ordinal:04d}-{suffix}",
                rule_id=rule_id,
                severity=severity,
                message=message,
                json_pointer=json_pointer,
                evidence_keys=(self._evidence.key,),
            )
        )


def canonical_document_sha256(
    value: CanonicalNormalizationResult | Mapping[str, Any],
) -> str:
    """Return the deterministic SHA-256 of canonical JSON UTF-8 bytes."""

    document = (
        value.normalized_document
        if isinstance(value, CanonicalNormalizationResult)
        else value
    )
    if not isinstance(document, Mapping):
        raise TypeError("canonical document must be a mapping")
    return hashlib.sha256(_canonical_json_bytes(document)).hexdigest()


def _resource_record(result: CanonicalNormalizationResult) -> dict[str, Any]:
    records = {
        "profile_id": result.profile_id,
        "schema_status": result.schema_status.value,
        "submission_ready": result.submission_ready,
        "node_count": result.node_count,
        "unknown_tags": [
            {
                "path": item.path,
                "tag": item.tag,
                "official_looking": item.official_looking,
                "severity": item.severity.value,
                "message": item.message,
            }
            for item in result.unknown_tags
        ],
        "wrong_container_types": [
            {
                "path": item.path,
                "expected": item.expected,
                "actual": item.actual,
                "severity": item.severity.value,
            }
            for item in result.wrong_container_types
        ],
        "coercions": [
            {"path": item.path, "kind": item.kind} for item in result.coercions
        ],
        "rejected_values": [
            {
                "path": item.path,
                "code": item.code,
                "severity": item.severity.value,
                "message": item.message,
            }
            for item in result.rejected_values
        ],
        "duplicate_logical_child_keys": [
            {
                "container_path": item.container_path,
                "logical_key_fields": list(item.logical_key_fields),
                "logical_key_values": [_json_safe_scalar(value) for value in item.logical_key_values],
                "first_path": item.first_path,
                "duplicate_path": item.duplicate_path,
                "severity": item.severity.value,
            }
            for item in result.duplicate_logical_child_keys
        ],
    }
    for key in (
        "unknown_tags",
        "wrong_container_types",
        "coercions",
        "rejected_values",
        "duplicate_logical_child_keys",
    ):
        records[key].sort(key=_canonical_sort_key)
    return _json_snapshot(records)


def _json_pointer_leaves(value: Any, pointer: str = "") -> set[str]:
    if isinstance(value, Mapping):
        leaves: set[str] = set()
        for key, child in value.items():
            token = str(key).replace("~", "~0").replace("/", "~1")
            leaves.update(_json_pointer_leaves(child, f"{pointer}/{token}"))
        return leaves
    if isinstance(value, list):
        leaves = set()
        for index, child in enumerate(value):
            leaves.update(_json_pointer_leaves(child, f"{pointer}/{index}"))
        return leaves
    return {pointer or "/"}


def _pointer_final_token(pointer: str) -> str:
    return pointer.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("value is not deterministic canonical JSON") from exc


def _json_snapshot(value: Any) -> Any:
    return json.loads(_canonical_json_bytes(value).decode("utf-8"))


def _canonical_sort_key(value: Any) -> str:
    return _canonical_json_bytes(value).decode("utf-8")


def _json_safe_scalar(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            return repr(value)
        return value
    return str(value)


def _diagnostic_severity(value: str) -> ValidationSeverity:
    if value == "warning":
        return ValidationSeverity.WARNING
    return ValidationSeverity.ERROR


def _transition_severity(value: str) -> ValidationSeverity:
    try:
        severity = TransitionIssueSeverity(value)
    except ValueError:
        return ValidationSeverity.ERROR
    return {
        TransitionIssueSeverity.INFO: ValidationSeverity.INFO,
        TransitionIssueSeverity.WARNING: ValidationSeverity.WARNING,
        TransitionIssueSeverity.ERROR: ValidationSeverity.ERROR,
    }[severity]


def _rule_token(value: Any) -> str:
    token = re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(value)).strip("-")
    if not token:
        return "UNSPECIFIED"
    return token[:120]


def _nonblank(value: Any, name: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{name} cannot be blank")
    return text


def _nonblank_tuple(values: Sequence[Any], name: str) -> tuple[str, ...]:
    return tuple(_nonblank(value, name) for value in values)
