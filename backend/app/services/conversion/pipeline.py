"""Strict ingestion, eligibility, ID policy, and field-lineage orchestration.

The executable engine remains a research-only projection. Operational FHWA
profiles stay disabled until their registered artifacts are locally verified
and compiled into tested rule bundles.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Optional

from .element_xml import (
    BridgeCandidate,
    ElementJoinResult,
    ElementParseResult,
    IssueSeverity as ElementIssueSeverity,
    match_element_records,
)
from .nbi_parser import (
    LegacyBridgeKey,
    ParseIssue,
    ParseResult,
    SourceRecord,
)
from .inspection_events import (
    InspectionEventResult,
    InspectionIssueSeverity,
    InspectionTransitionValue,
)
from .eligibility import (
    BID01Policy,
    EligibilityProfile,
    apply_bid01_policy,
    bid01_candidates_from_groups,
    evaluate_many_bridge_groups,
    preview_bid01_policy,
)
from .transition_engine import map_bridge_with_lineage
from .transition_engine import TransitionCoverage, TransitionIssue
from .transition_values import (
    ParsedScalar,
    ParsedStatus,
    SourceField,
    TransitionCategory,
    TransitionStatus,
    TransitionValue,
)
from .transition_source_refs import (
    bind_transition_source_references,
    element_schema_source_reference,
)
from .canonical_schema import (
    CanonicalNormalizationResult,
    normalize_canonical_document,
)
from .canonical_validation import (
    CanonicalDocumentValidationAdapter,
    ProjectionIntegritySnapshot,
    WorkspacePolicyContext,
    canonical_document_sha256,
)
from .pinned_schema import ValidationTarget, execute_pinned_schema
from .validation_profiles import (
    BRIDGEGUARD_VALIDATION_LABEL,
    NOT_OFFICIAL_DISCLAIMER,
    CoverageRegistry,
    PublishPolicy,
    ValidationProfile,
    ValidationProfileKind,
    evaluate_validation,
)
from ...standards.fhwa_registry import DEFAULT_REGISTRY_PATH, load_rule_bundle_registry

try:
    from ...routers.services.snbi_schema import (
        summarize_payload_coverage,
        summarize_payload_coverage_by_section,
    )
except ImportError:  # pragma: no cover - direct backend/app script compatibility
    from routers.services.snbi_schema import (
        summarize_payload_coverage,
        summarize_payload_coverage_by_section,
    )


RESEARCH_PROFILE = "research_historical_import"
PIPELINE_VERSION = "strict-ingestion-canonical-validation-v3"
_PINNED_SCHEMA_ARTIFACT_ID = "snbi_schema_v04_july_2024"

_RESEARCH_VALIDATION_PROFILE = ValidationProfile(
    profile_id=RESEARCH_PROFILE,
    kind=ValidationProfileKind.RESEARCH_IMPORT,
    rule_bundle_id=RESEARCH_PROFILE,
    publish_policy=PublishPolicy.ADVISORY,
    expected_rule_ids=(),
    publication_enabled=False,
    require_executable_bundle=True,
)
_RESEARCH_VALIDATION_COVERAGE = CoverageRegistry(
    profile_id=RESEARCH_PROFILE,
    rows=(),
    registry_version="research-validation-coverage-v1",
)
_CANONICAL_ARRAY_FIELDS = (
    "Elements",
    "Features",
    "Inspections",
    "PostingEvaluations",
    "PostingStatuses",
    "SpanSets",
    "SubstructureSets",
    "Works",
)


def _thaw_tree(value: Any) -> Any:
    """Convert an immutable transition snapshot to ordinary JSON containers."""

    if isinstance(value, Mapping):
        return {str(key): _thaw_tree(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw_tree(child) for child in value]
    return value


class StrictConversionError(ValueError):
    """A typed, path-free conversion failure suitable for an API 4xx."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 422,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = dict(details or {})

    def public_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.details:
            payload["details"] = self.details
        return payload


@dataclass(frozen=True)
class StrictConversionOptions:
    conversion_profile: str
    allowed_state_codes: tuple[str, ...] = ("34",)
    bid01_policy: Optional[BID01Policy] = None

    def normalized_allowed_states(self) -> frozenset[str]:
        normalized = set()
        for value in self.allowed_state_codes:
            text = str(value).strip()
            if not text.isdigit() or not 1 <= int(text) <= 99:
                raise StrictConversionError(
                    "invalid_workspace_state_policy",
                    "Allowed state codes must be one- or two-digit FIPS values.",
                    status_code=500,
                )
            normalized.add(f"{int(text):02d}")
        if not normalized:
            raise StrictConversionError(
                "empty_workspace_state_policy",
                "The workspace must allow at least one state code.",
                status_code=500,
            )
        return frozenset(normalized)


def convert_strict_parse_results(
    nbi_result: ParseResult,
    *,
    nbi_artifact: Mapping[str, Any],
    element_result: Optional[ElementParseResult] = None,
    element_artifact: Optional[Mapping[str, Any]] = None,
    inspection_history_results: Optional[
        Mapping[LegacyBridgeKey, InspectionEventResult]
    ] = None,
    inspection_history_context: Optional[Mapping[str, Any]] = None,
    options: StrictConversionOptions,
) -> Dict[str, Any]:
    """Map strict parser results through the provenance-aware research engine."""

    if options.conversion_profile != RESEARCH_PROFILE:
        raise StrictConversionError(
            "conversion_profile_not_implemented",
            (
                f"{options.conversion_profile!r} is registered but its executable "
                "rules are not implemented; only the explicitly selected research "
                "historical-import profile is currently executable"
            ),
            status_code=409,
        )
    allowed_states = options.normalized_allowed_states()
    element_join = _join_elements(nbi_result, element_result)
    history_results = dict(inspection_history_results or {})

    if options.bid01_policy is None:
        raise StrictConversionError(
            "bid01_policy_required",
            "Select an explicit BID01 leading-zero policy; no default is permitted.",
            status_code=400,
            details={"supported": [item.value for item in BID01Policy]},
        )
    try:
        selected_bid01_policy = BID01Policy(options.bid01_policy)
    except ValueError as exc:
        raise StrictConversionError(
            "unsupported_bid01_policy",
            "Select one supported BID01 leading-zero policy.",
            status_code=400,
            details={"supported": [item.value for item in BID01Policy]},
        ) from exc
    groups = tuple(nbi_result.bridges.values())
    bid01_candidates = bid01_candidates_from_groups(groups)
    bid01_preview = preview_bid01_policy(
        bid01_candidates, policy=selected_bid01_policy
    )
    if not bid01_preview.can_apply:
        raise StrictConversionError(
            "bid01_policy_conflict",
            "The selected BID01 policy creates a collision or empty identifier.",
            details=_bid01_preview_dict(bid01_preview),
        )
    bid01_by_record = apply_bid01_policy(
        bid01_candidates, policy=selected_bid01_policy
    )
    eligibility = evaluate_many_bridge_groups(
        groups, profile=EligibilityProfile.RESEARCH_HISTORICAL_IMPORT
    )

    if element_result is not None and (
        not element_result.element_file_parsed
        or any(
            issue.severity in {ElementIssueSeverity.ERROR, ElementIssueSeverity.FATAL}
            for issue in element_result.issues
        )
    ):
        raise StrictConversionError(
            "element_xml_rejected",
            "The supplied element XML failed strict parsing or validation.",
            details={"issues": [_element_issue(item) for item in element_result.issues]},
        )
    if element_join is not None and (
        element_join.unmatched or element_join.ambiguous
    ):
        raise StrictConversionError(
            "element_join_rejected",
            "Every parsed element record must match one bridge unambiguously.",
            details={
                "matched": element_join.matched,
                "unmatched": element_join.unmatched,
                "ambiguous": element_join.ambiguous,
                "issues": [_element_issue(item) for item in element_join.issues],
            },
        )
    element_schema_validated = bool(
        element_result is not None
        and any(
            issue.code == "xml_schema_validated"
            for issue in element_result.issues
        )
    )

    pipeline_issues: list[Dict[str, Any]] = []
    mapped: list[Dict[str, Any]] = []
    provenance_by_target: Dict[tuple[str, str], Dict[str, Any]] = {}
    lineage_by_target: Dict[tuple[str, str], Dict[str, Any]] = {}
    review_lineage_by_target: Dict[tuple[str, str], Dict[str, Any]] = {}
    transition_issues_by_target: Dict[tuple[str, str], list[Dict[str, Any]]] = {}
    transition_coverage_by_target: Dict[tuple[str, str], Dict[str, Any]] = {}
    transition_lineage_values_by_target: Dict[
        tuple[str, str], Dict[str, TransitionValue]
    ] = {}
    transition_issue_values_by_target: Dict[
        tuple[str, str], tuple[TransitionIssue, ...]
    ] = {}
    transition_coverage_values_by_target: Dict[
        tuple[str, str], TransitionCoverage
    ] = {}
    skipped = 0
    dropped_disallowed_state = 0

    candidate_for_key = _candidate_ids(nbi_result)
    for key, group in nbi_result.bridges.items():
        source_record_ids = [
            item.source_record_id
            for item in ((group.over,) + group.unders)
            if item is not None
        ]
        if key.normalized_state not in allowed_states:
            dropped_disallowed_state += 1
            pipeline_issues.append(
                _pipeline_issue(
                    "state_not_allowed_by_workspace",
                    "info",
                    "Record retained in the source artifact but excluded by workspace state policy.",
                    key,
                    source_record_ids,
                )
            )
            continue
        if group.over is None:
            skipped += 1
            pipeline_issues.append(
                _pipeline_issue(
                    "missing_over_record",
                    "error",
                    "Bridge group has no Type 1 over record and was not mapped.",
                    key,
                    source_record_ids,
                )
            )
            continue
        selected_source = group.over
        if selected_source is None:
            skipped += 1
            continue
        target_bid01 = bid01_by_record.get(selected_source.source_record_id)
        if target_bid01 is None:
            raise StrictConversionError(
                "bid01_projection_mismatch",
                "BID01 policy output could not be matched to the selected over record.",
            )
        try:
            transition = map_bridge_with_lineage(
                group,
                target_bid01=target_bid01,
                profile_id=options.conversion_profile,
            )
            snbi = _thaw_tree(transition.document)
            # A conversion result is a complete canonical bridge snapshot.
            # Preserve explicit empty child collections so downstream
            # replacement semantics never have to infer omission intent.
            for array_name in _CANONICAL_ARRAY_FIELDS:
                snbi.setdefault(array_name, [])
        except Exception as exc:
            raise StrictConversionError(
                "transition_engine_error",
                "The transition engine rejected the conversion batch.",
                details={
                    "bridge_key": {
                        "source_agency_context": key.source_agency_context,
                        "normalized_state": key.normalized_state,
                        "raw_structure_number": key.raw_structure_number,
                    },
                    "source_record_ids": source_record_ids,
                    "failure_type": type(exc).__name__,
                },
            ) from exc

        history_result = history_results.get(key)
        inspection_lineage: Dict[str, TransitionValue] = {}
        transition_review = dict(transition.review_lineage)
        transition_issues = tuple(transition.issues)
        if inspection_history_results is not None:
            if history_result is None:
                raise StrictConversionError(
                    "inspection_history_missing_for_target_bridge",
                    "Every converted target bridge requires a prepared annual-history result.",
                    details={
                        "bridge_key": {
                            "source_agency_context": key.source_agency_context,
                            "normalized_state": key.normalized_state,
                            "raw_structure_number": key.raw_structure_number,
                        }
                    },
                )
            identity = history_result.bridge_identity
            if (
                identity.source_agency_context != key.source_agency_context
                or identity.normalized_state != key.normalized_state
                or identity.raw_structure_number != key.raw_structure_number
            ):
                raise StrictConversionError(
                    "inspection_history_identity_mismatch",
                    "Prepared inspection history does not match the immutable target identity.",
                    status_code=500,
                )
            if any(
                issue.severity is InspectionIssueSeverity.ERROR
                for issue in history_result.issues
            ):
                raise StrictConversionError(
                    "inspection_history_has_errors",
                    "Reducer errors must be resolved before annual history is attached.",
                )
            snbi["Inspections"] = [
                event.to_snbi_dict() for event in history_result.events
            ]
            for event_index, event in enumerate(history_result.events):
                for tag, value in event.values.items():
                    inspection_lineage[f"/Inspections/{event_index}/{tag}"] = (
                        _inspection_transition_value(value)
                    )
            transition_review.pop("/Inspections", None)
            transition_issues = tuple(
                issue
                for issue in transition_issues
                if issue.code != "inspection_annual_history_required"
            )
        if not snbi:
            skipped += 1
            pipeline_issues.append(
                _pipeline_issue(
                    "legacy_mapper_empty_result",
                    "error",
                    "Legacy mapper produced no bridge document.",
                    key,
                    source_record_ids,
                )
            )
            continue

        candidate_id = candidate_for_key[key]
        assigned_elements = (
            element_join.records_by_candidate.get(candidate_id, ())
            if element_join is not None
            else ()
        )
        element_lineage: Dict[str, TransitionValue] = {}
        if assigned_elements:
            element_rows: list[Dict[str, Any]] = []
            for element_index, element_record in enumerate(assigned_elements):
                element_source_id = (
                    f"element:{str((element_artifact or {}).get('sha256') or 'unverified')}:"
                    f"{element_record.record_index}"
                )
                row = {
                    tag: value
                    for tag, value in element_record.to_snbi_fields().items()
                    if value is not None and str(value).strip() != ""
                }
                element_rows.append(row)
                for target_tag, value in row.items():
                    source_tag = {
                        "BE01": "EN", "BE02": "EPN", "BE03": "TOTALQTY",
                        "BCS01": "CS1", "BCS02": "CS2", "BCS03": "CS3", "BCS04": "CS4",
                    }[target_tag]
                    field = element_record.fields[source_tag]
                    element_lineage[f"/Elements/{element_index}/{target_tag}"] = TransitionValue(
                        value=str(value),
                        target_tag=target_tag,
                        rule_id=f"legacy-element-{source_tag.lower()}-direct-v1",
                        category=TransitionCategory.DIRECT,
                        source_record_ids=(element_source_id,),
                        source_fields=(
                            SourceField(
                                tag=source_tag,
                                raw=field.raw_text,
                                parsed=ParsedScalar(
                                    raw=field.raw_text,
                                    value=str(value),
                                    status=ParsedStatus.VALID,
                                ),
                            ),
                        ),
                        status=TransitionStatus.POPULATED,
                        source_references=(
                            (
                                element_schema_source_reference(
                                    target_tag=target_tag,
                                    source_tag=source_tag,
                                ),
                            )
                            if element_schema_validated
                            else ()
                        ),
                    )
            snbi["Elements"] = element_rows

        target = (str(snbi.get("BL01") or ""), str(snbi.get("BID01") or ""))
        if not all(target) or target in provenance_by_target:
            raise StrictConversionError(
                "target_identity_collision",
                "Strict source identities map to a missing or duplicate legacy target identity.",
                details={"state_code": target[0], "bridge_number": target[1]},
            )
        provenance_by_target[target] = {
            "nbi": (
                {
                    **dict(selected_source.fields),
                    "STATE_CODE_001": selected_source.bridge_key.normalized_state,
                }
                if selected_source is not None
                else None
            ),
            "profile": options.conversion_profile,
            "pipeline_version": PIPELINE_VERSION,
            "artifact": _safe_artifact_metadata(nbi_artifact),
            "over_record": _source_record_ref(group.over),
            "under_records": [_source_record_ref(item) for item in group.unders],
            "element_artifact": (
                _safe_artifact_metadata(element_artifact)
                if element_artifact is not None
                else None
            ),
            "element_record_indices": [item.record_index for item in assigned_elements],
            "eligibility": _eligibility_result_dict(eligibility[key]),
            "bid01_policy": selected_bid01_policy.value,
            "inspection_history": (
                _inspection_history_context_for_bridge(
                    inspection_history_context or {}, history_result
                )
                if history_result is not None
                else None
            ),
        }
        combined_lineage = dict(transition.lineage)
        combined_lineage.update(element_lineage)
        combined_lineage.update(inspection_lineage)
        combined_lineage = {
            path: bind_transition_source_references(value)
            for path, value in combined_lineage.items()
        }
        lineage_by_target[target] = {
            path: value.to_lineage_dict()
            for path, value in combined_lineage.items()
        }
        transition_lineage_values_by_target[target] = combined_lineage
        review_lineage_by_target[target] = {
            path: value.to_lineage_dict()
            for path, value in transition_review.items()
        }
        transition_issue_values_by_target[target] = transition_issues
        transition_issues_by_target[target] = [
            issue.to_dict() for issue in transition_issues
        ]
        transition_coverage = transition.coverage.to_dict()
        if history_result is not None:
            removed_review = transition.review_lineage.get("/Inspections")
            if (
                removed_review is not None
                and removed_review.category is TransitionCategory.NO_TRANSITION
            ):
                transition_coverage["no_transition_count"] = max(
                    0, int(transition_coverage["no_transition_count"]) - 1
                )
            added = len(inspection_lineage)
            transition_coverage["document_leaf_count"] += added
            transition_coverage["lineage_leaf_count"] += added
            transition_coverage["internal_derived_leaf_count"] += sum(
                1
                for value in inspection_lineage.values()
                if value.category
                in {
                    TransitionCategory.DERIVED,
                    TransitionCategory.STRUCTURAL,
                    TransitionCategory.APP_PROJECTION,
                }
            )
            transition_coverage["populated_permanent_count"] += sum(
                1
                for value in inspection_lineage.values()
                if value.status is TransitionStatus.POPULATED
            )
            transition_coverage["assumed_generated_count"] += sum(
                1
                for value in inspection_lineage.values()
                if value.status in {
                    TransitionStatus.ASSUMED,
                    TransitionStatus.GENERATED,
                }
            )
            transition_coverage["source_record_count"] = len(
                {
                    source_id
                    for value in combined_lineage.values()
                    for source_id in value.source_record_ids
                }
            )
            status_counts = dict(transition_coverage["status_counts"])
            category_counts = dict(transition_coverage["category_counts"])
            for value in inspection_lineage.values():
                status_counts[value.status.value] = (
                    status_counts.get(value.status.value, 0) + 1
                )
                category_counts[value.category.value] = (
                    category_counts.get(value.category.value, 0) + 1
                )
            transition_coverage["status_counts"] = status_counts
            transition_coverage["category_counts"] = category_counts
        if element_lineage:
            added = len(element_lineage)
            transition_coverage["document_leaf_count"] += added
            transition_coverage["lineage_leaf_count"] += added
            transition_coverage["populated_permanent_count"] += added
            status_counts = dict(transition_coverage["status_counts"])
            status_counts[TransitionStatus.POPULATED.value] = (
                status_counts.get(TransitionStatus.POPULATED.value, 0) + added
            )
            transition_coverage["status_counts"] = status_counts
            category_counts = dict(transition_coverage["category_counts"])
            category_counts[TransitionCategory.DIRECT.value] = (
                category_counts.get(TransitionCategory.DIRECT.value, 0) + added
            )
            transition_coverage["category_counts"] = category_counts
        transition_coverage_by_target[target] = transition_coverage
        transition_coverage_values_by_target[target] = (
            _transition_coverage_from_dict(transition_coverage)
        )
        mapped.append(snbi)

    normalized: list[Dict[str, Any]] = []
    normalization_results: Dict[
        tuple[str, str], CanonicalNormalizationResult
    ] = {}
    canonical_normalization_documents: list[Dict[str, Any]] = []
    validation_reports: list[Dict[str, Any]] = []
    standards_registry = load_rule_bundle_registry()
    for mapped_bridge in mapped:
        try:
            normalization = normalize_canonical_document(
                mapped_bridge,
                profile_id=options.conversion_profile,
            )
        except Exception as exc:
            raise StrictConversionError(
                "canonical_normalization_failed",
                "The canonical document boundary rejected the conversion batch.",
                details={"failure_type": type(exc).__name__},
            ) from exc
        bridge = normalization.normalized_document
        target = (str(bridge.get("BL01") or ""), str(bridge.get("BID01") or ""))
        source_data = provenance_by_target.get(target)
        if source_data is None:
            raise StrictConversionError(
                "provenance_projection_mismatch",
                "Canonical output could not be matched to its source provenance.",
            )
        normalization_results[target] = normalization
        document_id = _canonical_document_id(target, normalization)
        canonical_report = _canonical_normalization_report(
            document_id, target, normalization
        )
        canonical_normalization_documents.append(canonical_report)

        pinned_schema_result = execute_pinned_schema(
            bridge,
            registry_path=DEFAULT_REGISTRY_PATH,
            bundle_id=_RESEARCH_VALIDATION_PROFILE.rule_bundle_id,
            artifact_id=_PINNED_SCHEMA_ARTIFACT_ID,
            target=ValidationTarget.BRIDGE,
        )

        validation_adapter = CanonicalDocumentValidationAdapter(
            document_id=document_id,
            normalization=normalization,
            # Persistence round-trip belongs to PR4/PR5.  Absence is explicit
            # and fail-closed; conversion must never claim it passed.
            projection=ProjectionIntegritySnapshot(round_trip_document=None),
            workspace_policy=WorkspacePolicyContext(
                workspace_id=os.getenv(
                    "BRIDGEGUARD_WORKSPACE_ID", "shared-research-workspace"
                ).strip(),
                authorized_profile_ids=(options.conversion_profile,),
                authorized_bridge_ids=(target[1],),
                authorized_state_codes=tuple(sorted(allowed_states)),
                require_approval=False,
            ),
            transition_lineage=transition_lineage_values_by_target[target],
            transition_issues=transition_issue_values_by_target[target],
            transition_coverage=transition_coverage_values_by_target[target],
            pinned_schema_result=pinned_schema_result,
        )
        validation_report = evaluate_validation(
            validation_adapter,
            profile=_RESEARCH_VALIDATION_PROFILE,
            standards_registry=standards_registry,
            coverage_registry=_RESEARCH_VALIDATION_COVERAGE,
        ).to_dict()
        validation_reports.append(validation_report)

        bridge["_SourceData"] = source_data
        bridge["_TransitionLineage"] = lineage_by_target[target]
        bridge["_TransitionReview"] = review_lineage_by_target[target]
        bridge["_TransitionIssues"] = transition_issues_by_target[target]
        bridge["_TransitionCoverage"] = transition_coverage_by_target[target]
        bridge["_CanonicalNormalization"] = canonical_report
        bridge["_ValidationReport"] = validation_report
        normalized.append(bridge)

    normalization_issues = [
        _normalization_compatibility_message(document, diagnostic)
        for document in canonical_normalization_documents
        for diagnostic in document["diagnostics"]
        if diagnostic["severity"] in {"warning", "error", "critical"}
    ]
    validation = _validation_response(validation_reports)
    diagnostics = _conversion_diagnostics(
        canonical_normalization_documents=canonical_normalization_documents,
        validation_reports=validation_reports,
        parser_issues=[_nbi_issue(item) for item in nbi_result.issues],
        pipeline_issues=pipeline_issues,
        transition_issues_by_target=transition_issues_by_target,
    )
    return {
        "status": "success",
        "engine": PIPELINE_VERSION,
        "conversion_profile": options.conversion_profile,
        "submission_ready": False,
        "submission_label": "BridgeGuard research import - not an FHWA submission file",
        "bridges": normalized,
        "converted": len(normalized),
        "skipped": skipped,
        "dropped_non_nj": dropped_disallowed_state,
        "dropped_disallowed_state": dropped_disallowed_state,
        "elements_from_xml": element_result is not None,
        # Retain current response names for the un-migrated frontend, but mark
        # them deprecated because these are normalization issues, not parity.
        "parity_errors": normalization_issues,
        "parity_ok": len(normalization_issues) == 0,
        "normalization_issues": normalization_issues,
        "canonical_normalization": {
            "profile_id": options.conversion_profile,
            "submission_ready": False,
            "documents": canonical_normalization_documents,
        },
        "coverage": summarize_payload_coverage(normalized),
        "section_coverage": summarize_payload_coverage_by_section(normalized),
        "transition_coverage": _aggregate_transition_coverage(
            transition_coverage_by_target.values()
        ),
        "eligibility": _aggregate_eligibility(eligibility.values()),
        "bid01_preview": _bid01_preview_dict(bid01_preview),
        "validation": validation,
        "diagnostics": diagnostics,
        "sources": {
            "nbi": _safe_artifact_metadata(nbi_artifact),
            "elements": (
                _safe_artifact_metadata(element_artifact)
                if element_artifact is not None
                else None
            ),
        },
        "parser": {
            "format": nbi_result.input_format.value,
            "encoding": nbi_result.encoding,
            "source_sha256": nbi_result.source_sha256,
            "bytes_read": nbi_result.bytes_read,
            "records_seen": nbi_result.records_seen,
            "records_accepted": len(nbi_result.records),
            "records_quarantined": len(nbi_result.quarantined_records),
            "bridge_groups": len(nbi_result.bridges),
            "issues": [_nbi_issue(item) for item in nbi_result.issues],
            "quarantined_records": [
                {
                    "source_record_id": item.source_record_id,
                    "line_number": item.line_number,
                    "byte_offset": item.byte_offset,
                    "raw_sha256": item.raw_sha256,
                    "issue_codes": list(item.issue_codes),
                }
                for item in nbi_result.quarantined_records
            ],
        },
        "elements": _element_summary(element_result, element_join),
        "pipeline_issues": pipeline_issues,
        "compatibility": {
            "legacy_response_fields": True,
            "parity_fields_deprecated": True,
            "legacy_mapper_unversioned": False,
            "transition_lineage": True,
            "legacy_normalizer_used": False,
            "legacy_validator_used": False,
            "inspection_history_attached": inspection_history_results is not None,
        },
    }


def _inspection_transition_value(
    value: InspectionTransitionValue,
) -> TransitionValue:
    """Adapt reducer lineage to the shared conversion-lineage contract."""

    source_fields = []
    for source in value.source_values:
        raw = source.raw_value
        parsed = ParsedScalar(
            raw=raw,
            value=(str(raw) if raw is not None else None),
            status=(ParsedStatus.VALID if raw is not None else ParsedStatus.BLANK),
        )
        source_fields.append(SourceField.from_parsed(source.source_field, parsed))
    try:
        category = TransitionCategory(value.category)
        status = TransitionStatus(value.status)
    except ValueError as exc:
        raise StrictConversionError(
            "inspection_history_lineage_invalid",
            "Inspection-history lineage uses an unsupported category or status.",
            status_code=500,
        ) from exc
    return TransitionValue(
        value=value.value,
        target_tag=value.target_tag,
        rule_id=value.rule_id,
        category=category,
        source_record_ids=value.source_record_ids,
        source_fields=tuple(source_fields),
        transform_version=value.transform_version,
        status=status,
        messages=value.messages,
    )


def _inspection_history_context_for_bridge(
    context: Mapping[str, Any],
    result: InspectionEventResult,
) -> Dict[str, Any]:
    """Keep per-bridge evidence complete without copying every batch artifact."""

    record_index = context.get("source_records")
    if not isinstance(record_index, Mapping):
        record_index = {}
    return {
        "rule_bundle_id": context.get("rule_bundle_id"),
        "history_scope": dict(context.get("history_scope") or {}),
        "source_records": {
            source_id: dict(record_index[source_id])
            for source_id in result.source_record_ids
            if source_id in record_index and isinstance(record_index[source_id], Mapping)
        },
    }


def _candidate_ids(result: ParseResult) -> Dict[LegacyBridgeKey, str]:
    output: Dict[LegacyBridgeKey, str] = {}
    used = set()
    for key in result.bridges:
        raw = "\x1f".join(
            (key.source_agency_context, key.normalized_state, key.raw_structure_number)
        ).encode("utf-8")
        candidate_id = hashlib.sha256(raw).hexdigest()
        if candidate_id in used:  # cryptographic collision guard, not expected
            raise StrictConversionError(
                "candidate_identity_collision", "Bridge candidate identity collided."
            )
        used.add(candidate_id)
        output[key] = candidate_id
    return output


def _join_elements(
    nbi_result: ParseResult, element_result: Optional[ElementParseResult]
) -> Optional[ElementJoinResult]:
    if element_result is None:
        return None
    candidate_ids = _candidate_ids(nbi_result)
    candidates = []
    for key, group in nbi_result.bridges.items():
        identity_record = group.over or (group.unders[0] if group.unders else None)
        candidates.append(
            BridgeCandidate(
                candidate_id=candidate_ids[key],
                structure_number_raw=key.raw_structure_number,
                state_raw=(
                    identity_record.raw_item_001
                    if identity_record is not None
                    else key.normalized_state
                ),
                agency_raw=key.source_agency_context,
            )
        )
    return match_element_records(element_result, candidates)


def _safe_artifact_metadata(value: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if value is None:
        return {}
    allowed = {
        "artifact_id",
        "job_id",
        "sha256",
        "size_bytes",
        "original_filename",
        "created_at",
        "status",
    }
    return {key: value[key] for key in allowed if key in value}


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _eligibility_result_dict(result: Any) -> Dict[str, Any]:
    return {
        "profile_id": _enum_value(result.profile_id),
        "rule_status": _enum_value(result.rule_status),
        "retained_by_profile": result.retained_by_profile,
        "has_federally_eligible_record": result.has_federally_eligible_record,
        "decisions": [
            {
                "eligible": decision.eligible,
                "retained_by_profile": decision.retained_by_profile,
                "profile_id": _enum_value(decision.profile_id),
                "rule_id": _enum_value(decision.rule_id),
                "rule_status": _enum_value(decision.rule_status),
                "source_record_id": decision.source_record_id,
                "record_type": _enum_value(decision.record_type),
                "normalized_item_42a": decision.normalized_item_42a,
                "normalized_item_112": decision.normalized_item_112,
                "reason_codes": [_enum_value(item) for item in decision.reason_codes],
                "warnings": list(decision.warnings),
                "review_actions": [_enum_value(item) for item in decision.review_actions],
                "source_references": [
                    {
                        "source_record_id": reference.source_record_id,
                        "field_name": reference.field_name,
                        "exact_source_value": reference.exact_source_value,
                        "line_number": reference.line_number,
                        "byte_offset": reference.byte_offset,
                        "raw_sha256": reference.raw_sha256,
                    }
                    for reference in decision.source_references
                ],
            }
            for decision in result.decisions
        ],
    }


def _aggregate_eligibility(results: Iterable[Any]) -> Dict[str, Any]:
    values = list(results)
    return {
        "bridge_groups": len(values),
        "federally_eligible_bridge_groups": sum(
            1 for item in values if item.has_federally_eligible_record
        ),
        "research_retained_bridge_groups": sum(
            1 for item in values if item.retained_by_profile
        ),
        "rule_status": "internal_unverified_not_submission_ready",
    }


def _bid01_preview_dict(preview: Any) -> Dict[str, Any]:
    def entry_dict(entry: Any) -> Dict[str, Any]:
        return {
            "source_record_id": entry.source_record_id,
            "raw_identifier": entry.raw_identifier,
            "source_identifier": entry.source_identifier,
            "target_identifier": entry.target_identifier,
            "changed": entry.changed,
            "source_agency_context": entry.bridge_key.source_agency_context,
            "normalized_state": entry.bridge_key.normalized_state,
        }

    return {
        "policy": _enum_value(preview.policy),
        "can_apply": preview.can_apply,
        "entry_count": len(preview.entries),
        "changed_count": len(preview.changed_entries),
        "entries": [entry_dict(item) for item in preview.entries],
        "invalid_entries": [entry_dict(item) for item in preview.invalid_entries],
        "collisions": [
            {
                "source_agency_context": collision.source_agency_context,
                "normalized_state": collision.normalized_state,
                "target_identifier": collision.target_identifier,
                "entries": [entry_dict(item) for item in collision.entries],
            }
            for collision in preview.collisions
        ],
    }


def _aggregate_transition_coverage(
    coverages: Iterable[Mapping[str, Any]],
) -> Dict[str, Any]:
    values = list(coverages)
    scalar_keys = {
        "document_leaf_count",
        "lineage_leaf_count",
        "internal_derived_leaf_count",
        "uncovered_leaf_count",
        "source_record_count",
        "populated_permanent_count",
        "populated_temporary_count",
        "null_source_count",
        "invalid_source_count",
        "no_transition_count",
        "assumed_generated_count",
        "mapping_exception_count",
        "reviewed_verified_count",
    }
    aggregated = {
        key: sum(int(item.get(key, 0)) for item in values)
        for key in sorted(scalar_keys)
    }
    for counter_name in ("status_counts", "category_counts"):
        counter: Dict[str, int] = {}
        for item in values:
            for name, count in dict(item.get(counter_name, {})).items():
                counter[name] = counter.get(name, 0) + int(count)
        aggregated[counter_name] = counter
    aggregated["bridge_count"] = len(values)
    return aggregated


def _transition_coverage_from_dict(value: Mapping[str, Any]) -> TransitionCoverage:
    """Rehydrate immutable coverage after element-lineage reconciliation."""

    return TransitionCoverage(
        document_leaf_count=int(value["document_leaf_count"]),
        lineage_leaf_count=int(value["lineage_leaf_count"]),
        internal_derived_leaf_count=int(value["internal_derived_leaf_count"]),
        uncovered_leaf_count=int(value["uncovered_leaf_count"]),
        status_counts=dict(value["status_counts"]),
        category_counts=dict(value["category_counts"]),
        source_record_count=int(value["source_record_count"]),
        populated_permanent_count=int(value["populated_permanent_count"]),
        populated_temporary_count=int(value["populated_temporary_count"]),
        null_source_count=int(value["null_source_count"]),
        invalid_source_count=int(value["invalid_source_count"]),
        no_transition_count=int(value["no_transition_count"]),
        assumed_generated_count=int(value["assumed_generated_count"]),
        mapping_exception_count=int(value["mapping_exception_count"]),
        reviewed_verified_count=int(value["reviewed_verified_count"]),
    )


def _canonical_document_id(
    target: tuple[str, str], normalization: CanonicalNormalizationResult
) -> str:
    seed = (
        f"{target[0]}\x1f{target[1]}\x1f"
        f"{canonical_document_sha256(normalization)}"
    ).encode("utf-8")
    return f"bridge-{hashlib.sha256(seed).hexdigest()[:32]}"


def _canonical_normalization_report(
    document_id: str,
    target: tuple[str, str],
    result: CanonicalNormalizationResult,
) -> Dict[str, Any]:
    diagnostics: list[Dict[str, Any]] = []
    diagnostics.extend(
        {
            "code": "unknown_tag",
            "severity": item.severity.value,
            "json_pointer": item.path,
            "message": item.message,
            "tag": item.tag,
            "official_looking": item.official_looking,
        }
        for item in result.unknown_tags
    )
    diagnostics.extend(
        {
            "code": "wrong_container_type",
            "severity": item.severity.value,
            "json_pointer": item.path,
            "message": f"Expected {item.expected}; received {item.actual}.",
            "expected": item.expected,
            "actual": item.actual,
        }
        for item in result.wrong_container_types
    )
    diagnostics.extend(
        {
            "code": "coercion",
            "severity": "info",
            "json_pointer": item.path,
            "message": f"Applied explicit normalization: {item.kind}.",
            "kind": item.kind,
        }
        for item in result.coercions
    )
    diagnostics.extend(
        {
            "code": item.code,
            "severity": item.severity.value,
            "json_pointer": item.path,
            "message": item.message,
        }
        for item in result.rejected_values
    )
    diagnostics.extend(
        {
            "code": "duplicate_logical_child_key",
            "severity": item.severity.value,
            "json_pointer": item.duplicate_path,
            "message": (
                "Duplicate logical child key also appears at "
                f"{item.first_path}."
            ),
            "container_path": item.container_path,
            "logical_key_fields": list(item.logical_key_fields),
        }
        for item in result.duplicate_logical_child_keys
    )
    diagnostics.sort(
        key=lambda item: (
            str(item.get("json_pointer") or ""),
            str(item.get("code") or ""),
            str(item.get("message") or ""),
        )
    )
    return {
        "document_id": document_id,
        "document_sha256": canonical_document_sha256(result),
        "state_code": target[0],
        "bridge_number": target[1],
        "profile_id": result.profile_id,
        "schema_status": result.schema_status.value,
        "submission_ready": False,
        "node_count": result.node_count,
        "diagnostics": diagnostics,
    }


def _normalization_compatibility_message(
    document: Mapping[str, Any], diagnostic: Mapping[str, Any]
) -> str:
    identity = f"{document.get('state_code')}/{document.get('bridge_number')}"
    pointer = str(diagnostic.get("json_pointer") or "/")
    return (
        f"{identity} {pointer}: {diagnostic.get('code')} - "
        f"{diagnostic.get('message')}"
    )


def _validation_response(reports: list[Dict[str, Any]]) -> Dict[str, Any]:
    blockers = [
        blocker
        for report in reports
        for blocker in report["decision"]["blockers"]
    ]
    warnings = [
        warning
        for report in reports
        for warning in report["decision"]["warnings"]
    ]
    severity_counts = {
        "critical": 0,
        "error": 0,
        "warning": 0,
        "info": 0,
    }
    incomplete_layers = 0
    for report in reports:
        for layer in report["layers"]:
            if not layer["completed"]:
                incomplete_layers += 1
            for issue in layer["issues"]:
                severity = str(issue.get("severity") or "").lower()
                if severity in severity_counts:
                    severity_counts[severity] += 1
    return {
        "validation_label": BRIDGEGUARD_VALIDATION_LABEL,
        "official_validation": False,
        "disclaimer": NOT_OFFICIAL_DISCLAIMER,
        "submission_ready": bool(reports)
        and all(report["decision"]["allowed"] for report in reports),
        "report_count": len(reports),
        "summary": {
            **severity_counts,
            "blockers": len(blockers),
            "warnings": len(warnings),
            "incomplete_layers": incomplete_layers,
        },
        "reports": reports,
    }


def _conversion_diagnostics(
    *,
    canonical_normalization_documents: list[Dict[str, Any]],
    validation_reports: list[Dict[str, Any]],
    parser_issues: list[Dict[str, Any]],
    pipeline_issues: list[Dict[str, Any]],
    transition_issues_by_target: Mapping[
        tuple[str, str], list[Dict[str, Any]]
    ],
) -> Dict[str, Any]:
    issues: list[Dict[str, Any]] = []

    def add(
        stage: str,
        code: Any,
        severity: Any,
        message: Any,
        *,
        json_pointer: Any = None,
        document_id: Any = None,
    ) -> None:
        issues.append(
            {
                "stage": stage,
                "code": str(code or "unspecified"),
                "severity": str(severity or "error").lower(),
                "message": str(message or "No diagnostic message was supplied."),
                "json_pointer": (
                    str(json_pointer) if json_pointer is not None else None
                ),
                "document_id": (
                    str(document_id) if document_id is not None else None
                ),
            }
        )

    for item in parser_issues:
        add(
            "parser", item.get("code"), item.get("severity"), item.get("message")
        )
    for item in pipeline_issues:
        add(
            "pipeline", item.get("code"), item.get("severity"), item.get("message")
        )
    for target, target_issues in transition_issues_by_target.items():
        for item in target_issues:
            add(
                "transition",
                item.get("code"),
                item.get("severity"),
                item.get("message"),
                json_pointer=item.get("target_path"),
                document_id=f"{target[0]}/{target[1]}",
            )
    for document in canonical_normalization_documents:
        for item in document["diagnostics"]:
            add(
                "canonical_normalization",
                item.get("code"),
                item.get("severity"),
                item.get("message"),
                json_pointer=item.get("json_pointer"),
                document_id=document["document_id"],
            )
    for report in validation_reports:
        for layer in report["layers"]:
            for item in layer["issues"]:
                add(
                    "validation",
                    item.get("rule_id"),
                    item.get("severity"),
                    item.get("message"),
                    json_pointer=item.get("json_pointer"),
                    document_id=report["document_id"],
                )
    issues.sort(
        key=lambda item: (
            item["stage"],
            str(item["document_id"] or ""),
            str(item["json_pointer"] or ""),
            item["code"],
            item["message"],
        )
    )
    manifest_seed = json.dumps(
        {
            "documents": [
                item["document_sha256"]
                for item in canonical_normalization_documents
            ],
            "issues": issues,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(manifest_seed).hexdigest()
    return {
        "format": "bridgeguard-conversion-diagnostics-v1",
        "media_type": "application/json",
        "filename": f"bridgeguard-conversion-diagnostics-{digest[:12]}.json",
        "sha256": digest,
        "issues": issues,
        "canonical_normalization": canonical_normalization_documents,
        "validation_reports": validation_reports,
    }


def _source_record_ref(record: Optional[SourceRecord]) -> Optional[Dict[str, Any]]:
    if record is None:
        return None
    return {
        "source_order": record.source_order,
        "line_number": record.line_number,
        "byte_offset": record.byte_offset,
        "raw_sha256": record.raw_sha256,
        "source_record_id": record.source_record_id,
        "record_type": record.record_type.value,
        "raw_item_001": record.raw_item_001,
        "raw_item_008": record.raw_item_008,
        "conformant": record.conformant,
        "issue_codes": list(record.issue_codes),
    }


def _pipeline_issue(
    code: str,
    severity: str,
    message: str,
    key: LegacyBridgeKey,
    source_record_ids: Iterable[str] = (),
) -> Dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "source_record_ids": list(source_record_ids),
        "bridge_key": {
            "source_agency_context": key.source_agency_context,
            "normalized_state": key.normalized_state,
            "raw_structure_number": key.raw_structure_number,
        },
    }


def _nbi_issue(issue: ParseIssue) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "code": issue.code,
        "message": issue.message,
        "severity": issue.severity.value,
        "line_number": issue.line_number,
        "byte_offset": issue.byte_offset,
        "raw_sha256": issue.raw_sha256,
        "source_record_id": issue.source_record_id,
    }
    if issue.bridge_key is not None:
        payload["bridge_key"] = {
            "source_agency_context": issue.bridge_key.source_agency_context,
            "normalized_state": issue.bridge_key.normalized_state,
            "raw_structure_number": issue.bridge_key.raw_structure_number,
        }
    return payload


def _element_issue(issue: Any) -> Dict[str, Any]:
    return {
        "code": issue.code,
        "severity": issue.severity.value,
        "message": issue.message,
        "record_index": issue.record_index,
        "record_path": issue.record_path,
        "field_name": issue.field_name,
        "line_number": issue.line_number,
        "column_number": issue.column_number,
    }


def _element_summary(
    result: Optional[ElementParseResult], joined: Optional[ElementJoinResult]
) -> Dict[str, Any]:
    if result is None:
        return {
            "element_file_supplied": False,
            "element_file_parsed": False,
            "element_records": 0,
            "matched": 0,
            "unmatched": 0,
            "ambiguous": 0,
            "issues": [],
        }
    return {
        "element_file_supplied": result.element_file_supplied,
        "element_file_parsed": result.element_file_parsed,
        "source_bytes": result.source_bytes,
        "expanded_bytes": result.expanded_bytes,
        "records_seen": result.records_seen,
        "element_records": result.element_records,
        "matched": joined.matched if joined else 0,
        "unmatched": joined.unmatched if joined else 0,
        "ambiguous": joined.ambiguous if joined else 0,
        "issues": [_element_issue(item) for item in (joined.issues if joined else result.issues)],
    }
