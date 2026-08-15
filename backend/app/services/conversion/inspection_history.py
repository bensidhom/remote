"""Strict multi-artifact annual NBI inspection-history preflight.

One legacy NBI row cannot establish whether an Item 90 date represents the
first or a later annual occurrence.  This module validates an explicit,
contiguous annual-source manifest, binds every parsed row to immutable artifact
bytes, and prepares reducer output for attachment to the selected annual
conversion.  It performs no database writes and never changes the single-file
converter's behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .eligibility import BID01Policy
from .inspection_events import (
    InspectionEventKind,
    InspectionEventResult,
    InspectionIssue,
    InspectionIssueSeverity,
    InspectionResultStatus,
    InspectionRuleBundle,
    InspectionTransitionValue,
    TwoDigitYearPolicy,
    build_inspection_events,
    inspection_source_from_nbi_record,
)
from .nbi_parser import LegacyBridgeKey, NBIFormat, IssuePolicy, ParseResult
from .source_artifacts import SourceArtifactError, SourceArtifactStore


HISTORY_MANIFEST_VERSION = "bridgeguard-nbi-annual-history-v1"
HISTORY_ENGINE_VERSION = "strict-inspection-history-attachment-v1"
MAX_HISTORY_MANIFEST_BYTES = 64 * 1024
MAX_ANNUAL_SOURCES = 50
MIN_ANNUAL_SOURCES = 2
MAX_HISTORY_RECORDS = 1_000_000
SUPPORTED_RULE_BUNDLE_ID = InspectionRuleBundle().bundle_id


class InspectionHistoryError(ValueError):
    """Typed, path-free request or preflight failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 422,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.status_code = int(status_code)
        self.details = dict(details or {})

    def public_dict(self) -> Dict[str, Any]:
        value: Dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.details:
            value["details"] = dict(self.details)
        return value


@dataclass(frozen=True, slots=True)
class AnnualSourceSpec:
    part_name: str
    inventory_year: int


@dataclass(frozen=True, slots=True)
class InspectionHistoryManifest:
    manifest_version: str
    source_agency_context: str
    encoding: str
    input_format: NBIFormat
    issue_policy: IssuePolicy
    bid01_policy: BID01Policy
    rule_bundle_id: str
    history_start_year: int
    history_end_year: int
    target_inventory_year: int
    annual_sources: Tuple[AnnualSourceSpec, ...]
    csv_delimiter: Optional[str] = None
    csv_quotechar: Optional[str] = None
    csv_escapechar: Optional[str] = None


@dataclass(frozen=True, slots=True)
class ParsedAnnualSource:
    spec: AnnualSourceSpec
    artifact: Mapping[str, Any]
    parse_result: ParseResult

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact", MappingProxyType(dict(self.artifact)))


@dataclass(frozen=True)
class PreparedInspectionHistory:
    manifest: InspectionHistoryManifest
    sources: Tuple[ParsedAnnualSource, ...]
    results_by_bridge: Mapping[LegacyBridgeKey, InspectionEventResult]
    target_bridge_keys: Tuple[LegacyBridgeKey, ...]
    ready_target_keys: Tuple[LegacyBridgeKey, ...]
    blocking_issues: Mapping[LegacyBridgeKey, Tuple[Dict[str, Any], ...]]
    source_records: Mapping[str, Mapping[str, Any]]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "results_by_bridge", MappingProxyType(dict(self.results_by_bridge))
        )
        object.__setattr__(
            self, "blocking_issues", MappingProxyType(dict(self.blocking_issues))
        )
        object.__setattr__(
            self,
            "source_records",
            MappingProxyType(
                {
                    key: MappingProxyType(dict(value))
                    for key, value in self.source_records.items()
                }
            ),
        )

    @property
    def history_ready(self) -> bool:
        return bool(self.target_bridge_keys) and (
            self.ready_target_keys == self.target_bridge_keys
        )

    @property
    def target_source(self) -> ParsedAnnualSource:
        for source in self.sources:
            if source.spec.inventory_year == self.manifest.target_inventory_year:
                return source
        raise RuntimeError("validated target annual source is missing")

    def attachment_results(self) -> Mapping[LegacyBridgeKey, InspectionEventResult]:
        return MappingProxyType(
            {key: self.results_by_bridge[key] for key in self.target_bridge_keys}
        )

    def public_dict(self) -> Dict[str, Any]:
        bridges = []
        for key in sorted(self.results_by_bridge):
            result = self.results_by_bridge[key]
            extra_issues = self.blocking_issues.get(key, ())
            bridges.append(_result_dict(result, extra_issues=extra_issues))

        severity_counts = {"info": 0, "warning": 0, "error": 0}
        event_kind_counts = {kind.value: 0 for kind in InspectionEventKind}
        value_count = 0
        deduplicated_event_count = 0
        for result in self.results_by_bridge.values():
            for issue in result.issues:
                severity_counts[issue.severity.value] += 1
            for issue in self.blocking_issues.get(_legacy_key(result), ()):
                severity = str(issue.get("severity") or "error")
                severity_counts[severity] = severity_counts.get(severity, 0) + 1
            for event in result.events:
                event_kind_counts[event.event_kind.value] += 1
                value_count += len(event.values)
                if event.status.value == "deduplicated":
                    deduplicated_event_count += 1

        return {
            "format": "bridgeguard-inspection-history-preflight-v1",
            "engine": HISTORY_ENGINE_VERSION,
            "status": "ready" if self.history_ready else "blocked",
            "history_ready": self.history_ready,
            "submission_ready": False,
            "submission_label": (
                "Research annual-history transition evidence; not an FHWA "
                "submission determination"
            ),
            "rule_bundle_id": self.manifest.rule_bundle_id,
            "history_scope": {
                "start_inventory_year": self.manifest.history_start_year,
                "end_inventory_year": self.manifest.history_end_year,
                "target_inventory_year": self.manifest.target_inventory_year,
                "coverage": "explicit_contiguous_annual_sources",
                "first_occurrence_semantics": (
                    "first occurrence within the supplied contiguous annual history"
                ),
                "pre_2000_compatibility_enabled": False,
            },
            "sources": [_source_dict(source) for source in self.sources],
            "source_records": {
                key: dict(value) for key, value in sorted(self.source_records.items())
            },
            "bridges": bridges,
            "coverage": {
                "source_artifact_count": len(self.sources),
                "inventory_year_count": len(self.sources),
                "bridge_identity_count": len(self.results_by_bridge),
                "target_bridge_count": len(self.target_bridge_keys),
                "ready_target_bridge_count": len(self.ready_target_keys),
                "blocked_target_bridge_count": (
                    len(self.target_bridge_keys) - len(self.ready_target_keys)
                ),
                "event_count": sum(event_kind_counts.values()),
                "event_kind_counts": event_kind_counts,
                "deduplicated_event_count": deduplicated_event_count,
                "transition_value_count": value_count,
                "issue_severity_counts": severity_counts,
                "not_invented_tags": ["BIE03", "BIE04", "BIE07"],
            },
        }


def parse_inspection_history_manifest(raw: str) -> InspectionHistoryManifest:
    if not isinstance(raw, str):
        raise InspectionHistoryError(
            "invalid_history_manifest", "The history manifest must be JSON text.",
            status_code=400,
        )
    if len(raw.encode("utf-8")) > MAX_HISTORY_MANIFEST_BYTES:
        raise InspectionHistoryError(
            "history_manifest_too_large",
            "The history manifest exceeds the bounded metadata limit.",
            status_code=413,
            details={"max_bytes": MAX_HISTORY_MANIFEST_BYTES},
        )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InspectionHistoryError(
            "invalid_history_manifest", "The history manifest is not valid JSON.",
            status_code=400,
        ) from exc
    if not isinstance(value, dict):
        raise InspectionHistoryError(
            "invalid_history_manifest", "The history manifest must be a JSON object.",
            status_code=400,
        )

    required = {
        "manifest_version",
        "source_agency_context",
        "encoding",
        "input_format",
        "issue_policy",
        "bid01_policy",
        "rule_bundle_id",
        "history_start_year",
        "history_end_year",
        "target_inventory_year",
        "annual_sources",
    }
    allowed = required | {"csv_dialect"}
    missing = sorted(required.difference(value))
    unknown = sorted(set(value).difference(allowed))
    if missing or unknown:
        raise InspectionHistoryError(
            "invalid_history_manifest_shape",
            "The history manifest has missing or unsupported fields.",
            status_code=400,
            details={"missing": missing, "unknown": unknown},
        )
    if value["manifest_version"] != HISTORY_MANIFEST_VERSION:
        raise InspectionHistoryError(
            "unsupported_history_manifest_version",
            "Select the supported annual-history manifest version.",
            status_code=409,
            details={"supported": HISTORY_MANIFEST_VERSION},
        )
    if value["rule_bundle_id"] != SUPPORTED_RULE_BUNDLE_ID:
        raise InspectionHistoryError(
            "inspection_rule_bundle_not_implemented",
            "The requested inspection-history rule bundle is not executable.",
            status_code=409,
            details={"supported": SUPPORTED_RULE_BUNDLE_ID},
        )

    agency = str(value["source_agency_context"] or "").strip()
    if not agency or len(agency) > 100:
        raise InspectionHistoryError(
            "invalid_source_agency_context",
            "source_agency_context must contain 1 to 100 characters.",
            status_code=400,
        )
    try:
        input_format = NBIFormat(value["input_format"])
        issue_policy = IssuePolicy(value["issue_policy"])
        bid01_policy = BID01Policy(value["bid01_policy"])
    except (TypeError, ValueError) as exc:
        raise InspectionHistoryError(
            "unsupported_history_parser_option",
            "Select implemented parser, issue, and BID01 policies.",
            status_code=400,
            details={
                "input_formats": [item.value for item in NBIFormat],
                "issue_policies": [item.value for item in IssuePolicy],
                "bid01_policies": [item.value for item in BID01Policy],
            },
        ) from exc

    start = _year(value["history_start_year"], "history_start_year")
    end = _year(value["history_end_year"], "history_end_year")
    target = _year(value["target_inventory_year"], "target_inventory_year")
    if end <= start:
        raise InspectionHistoryError(
            "insufficient_annual_history",
            "Annual inspection reconstruction requires at least two inventory years.",
            status_code=400,
        )
    expected_years = tuple(range(start, end + 1))
    if len(expected_years) > MAX_ANNUAL_SOURCES:
        raise InspectionHistoryError(
            "annual_history_limit_exceeded",
            "The requested annual history exceeds the source-count limit.",
            status_code=413,
            details={"max_sources": MAX_ANNUAL_SOURCES},
        )
    if target not in expected_years:
        raise InspectionHistoryError(
            "target_inventory_year_outside_history",
            "target_inventory_year must be within the declared annual history.",
            status_code=400,
        )

    raw_sources = value["annual_sources"]
    if not isinstance(raw_sources, list) or not (
        MIN_ANNUAL_SOURCES <= len(raw_sources) <= MAX_ANNUAL_SOURCES
    ):
        raise InspectionHistoryError(
            "invalid_annual_sources",
            "annual_sources must contain between 2 and 50 file declarations.",
            status_code=400,
        )
    sources = []
    part_names = set()
    years = set()
    for index, raw_source in enumerate(raw_sources):
        if not isinstance(raw_source, dict) or set(raw_source) != {
            "part_name", "inventory_year"
        }:
            raise InspectionHistoryError(
                "invalid_annual_source",
                "Each annual source requires only part_name and inventory_year.",
                status_code=400,
                details={"source_index": index},
            )
        raw_name = str(raw_source["part_name"] or "")
        try:
            part_name = SourceArtifactStore.sanitize_original_filename(raw_name)
        except SourceArtifactError as exc:
            raise InspectionHistoryError(
                "invalid_annual_source_name",
                "An annual source part_name is invalid.",
                status_code=400,
                details={"source_index": index},
            ) from exc
        if part_name != raw_name:
            raise InspectionHistoryError(
                "noncanonical_annual_source_name",
                "Annual source part_name values must already use their canonical safe form.",
                status_code=400,
                details={"source_index": index},
            )
        inventory_year = _year(raw_source["inventory_year"], "inventory_year")
        if part_name in part_names or inventory_year in years:
            raise InspectionHistoryError(
                "duplicate_annual_source",
                "Each part_name and inventory year must identify exactly one artifact.",
                status_code=400,
            )
        part_names.add(part_name)
        years.add(inventory_year)
        sources.append(AnnualSourceSpec(part_name, inventory_year))
    if years != set(expected_years):
        raise InspectionHistoryError(
            "noncontiguous_annual_history",
            "Upload exactly one immutable annual source for every declared inventory year.",
            status_code=400,
            details={
                "expected_years": list(expected_years),
                "provided_years": sorted(years),
            },
        )

    delimiter = quotechar = escapechar = None
    csv_dialect = value.get("csv_dialect")
    csv_formats = {
        NBIFormat.FHWA_DOWNLOAD_CSV,
        NBIFormat.NAMED_CSV,
        NBIFormat.LEGACY_POSITIONAL_CSV,
    }
    if input_format in csv_formats:
        if not isinstance(csv_dialect, dict) or set(csv_dialect).difference(
            {"delimiter", "quotechar", "escapechar"}
        ) or not {"delimiter", "quotechar"}.issubset(csv_dialect):
            raise InspectionHistoryError(
                "history_csv_dialect_required",
                "CSV annual history requires an explicit bounded dialect.",
                status_code=400,
            )
        delimiter = csv_dialect["delimiter"]
        quotechar = csv_dialect["quotechar"]
        escapechar = csv_dialect.get("escapechar")
    elif csv_dialect is not None:
        raise InspectionHistoryError(
            "unexpected_history_csv_dialect",
            "csv_dialect is permitted only for CSV annual sources.",
            status_code=400,
        )

    return InspectionHistoryManifest(
        manifest_version=HISTORY_MANIFEST_VERSION,
        source_agency_context=agency,
        encoding=str(value["encoding"] or "").strip(),
        input_format=input_format,
        issue_policy=issue_policy,
        bid01_policy=bid01_policy,
        rule_bundle_id=SUPPORTED_RULE_BUNDLE_ID,
        history_start_year=start,
        history_end_year=end,
        target_inventory_year=target,
        annual_sources=tuple(sorted(sources, key=lambda item: item.inventory_year)),
        csv_delimiter=delimiter,
        csv_quotechar=quotechar,
        csv_escapechar=escapechar,
    )


def prepare_inspection_history(
    manifest: InspectionHistoryManifest,
    parsed_sources: Sequence[ParsedAnnualSource],
    *,
    allowed_state_codes: Sequence[str],
) -> PreparedInspectionHistory:
    if not isinstance(manifest, InspectionHistoryManifest):
        raise TypeError("manifest must be InspectionHistoryManifest")
    sources = tuple(sorted(parsed_sources, key=lambda item: item.spec.inventory_year))
    expected = {(item.part_name, item.inventory_year) for item in manifest.annual_sources}
    supplied = {(item.spec.part_name, item.spec.inventory_year) for item in sources}
    if supplied != expected or len(sources) != len(manifest.annual_sources):
        raise InspectionHistoryError(
            "annual_source_cardinality_mismatch",
            "Uploaded annual files do not match the immutable manifest declarations.",
            status_code=400,
        )
    total_records = sum(source.parse_result.records_seen for source in sources)
    if total_records > MAX_HISTORY_RECORDS:
        raise InspectionHistoryError(
            "annual_history_record_limit_exceeded",
            "Combined annual sources exceed the bounded record limit.",
            status_code=413,
            details={"max_records": MAX_HISTORY_RECORDS},
        )

    allowed_states = {
        f"{int(str(value).strip()):02d}" for value in allowed_state_codes
        if str(value).strip().isdigit() and 1 <= int(str(value).strip()) <= 99
    }
    if not allowed_states:
        raise InspectionHistoryError(
            "empty_workspace_state_policy",
            "The workspace must allow at least one state code.",
            status_code=500,
        )

    grouped: Dict[LegacyBridgeKey, Dict[int, list[Any]]] = {}
    source_records: Dict[str, Dict[str, Any]] = {}
    target_bridge_keys = set()
    for source in sources:
        result = source.parse_result
        artifact_sha = str(source.artifact.get("sha256") or "")
        if result.source_sha256 != artifact_sha:
            raise InspectionHistoryError(
                "annual_source_hash_mismatch",
                "Parsed annual bytes do not match their immutable artifact receipt.",
                status_code=500,
            )
        if result.source_agency_context != manifest.source_agency_context:
            raise InspectionHistoryError(
                "annual_source_agency_mismatch",
                "Parsed annual source agency does not match the manifest.",
                status_code=500,
            )
        for key, bridge_group in result.bridges.items():
            if bridge_group.duplicate_overs:
                raise InspectionHistoryError(
                    "ambiguous_annual_over_record",
                    "An annual source contains duplicate Type 1 records for one immutable bridge identity.",
                    details={
                        "inventory_year": source.spec.inventory_year,
                        "bridge_identity": _key_dict(key),
                    },
                )
            if bridge_group.over is None:
                continue
            if key.normalized_state not in allowed_states:
                continue
            adapted = inspection_source_from_nbi_record(
                bridge_group.over,
                inventory_year=source.spec.inventory_year,
                artifact_sha256=artifact_sha,
            )
            grouped.setdefault(key, {}).setdefault(source.spec.inventory_year, []).append(
                adapted
            )
            source_records[adapted.source_record_id] = {
                "artifact_id": source.artifact.get("artifact_id"),
                "artifact_sha256": artifact_sha,
                "part_name": source.spec.part_name,
                "inventory_year": source.spec.inventory_year,
                "parser_source_record_id": bridge_group.over.source_record_id,
                "line_number": bridge_group.over.line_number,
                "byte_offset": bridge_group.over.byte_offset,
                "raw_sha256": bridge_group.over.raw_sha256,
                "record_type": bridge_group.over.record_type.value,
            }
            if source.spec.inventory_year == manifest.target_inventory_year:
                target_bridge_keys.add(key)

    if not target_bridge_keys:
        raise InspectionHistoryError(
            "target_history_has_no_convertible_bridges",
            "The target annual source has no allowed Type 1 bridge records.",
        )

    rule_bundle = InspectionRuleBundle(
        bundle_id=manifest.rule_bundle_id,
        # The endpoint intentionally has no switch for the unverified pre-2000
        # compatibility rule.  Ambiguous dates remain explicit reducer errors.
        two_digit_year_policy=TwoDigitYearPolicy.MODERN_ONLY,
    )
    reducer_input = {}
    for key, years in grouped.items():
        identity = next(iter(next(iter(years.values())))).bridge_identity
        reducer_input[identity] = years
    results = build_inspection_events(reducer_input, rule_bundle)
    results_by_key = {_legacy_key(result): result for result in results}

    ready = []
    blocking: Dict[LegacyBridgeKey, Tuple[Dict[str, Any], ...]] = {}
    for key in sorted(target_bridge_keys):
        result = results_by_key.get(key)
        if result is None:
            raise InspectionHistoryError(
                "inspection_history_projection_mismatch",
                "A target bridge could not be matched to its annual-history result.",
                status_code=500,
            )
        bridge_issues = []
        if len(result.inventory_years) < MIN_ANNUAL_SOURCES:
            bridge_issues.append(
                _blocking_issue(
                    "insufficient_bridge_history",
                    "At least two explicit annual occurrences are required before initial/routine events can be attached.",
                    key,
                    result.source_record_ids,
                )
            )
        if not any(
            event.event_kind is InspectionEventKind.ROUTINE for event in result.events
        ):
            bridge_issues.append(
                _blocking_issue(
                    "routine_inspection_history_missing",
                    "No valid Item 90 occurrence is available for this target bridge.",
                    key,
                    result.source_record_ids,
                )
            )
        if result.status in {InspectionResultStatus.FAILED, InspectionResultStatus.EMPTY}:
            bridge_issues.append(
                _blocking_issue(
                    "inspection_history_reducer_blocked",
                    "The inspection-history reducer did not produce usable events.",
                    key,
                    result.source_record_ids,
                )
            )
        if any(
            issue.severity is InspectionIssueSeverity.ERROR for issue in result.issues
        ):
            bridge_issues.append(
                _blocking_issue(
                    "inspection_history_has_errors",
                    "Reducer errors must be resolved before history can be attached.",
                    key,
                    result.source_record_ids,
                )
            )
        if bridge_issues:
            blocking[key] = tuple(bridge_issues)
        else:
            ready.append(key)

    return PreparedInspectionHistory(
        manifest=manifest,
        sources=sources,
        results_by_bridge=results_by_key,
        target_bridge_keys=tuple(sorted(target_bridge_keys)),
        ready_target_keys=tuple(sorted(ready)),
        blocking_issues=blocking,
        source_records=source_records,
    )


def _year(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1900 <= value <= 2199:
        raise InspectionHistoryError(
            "invalid_inventory_year",
            f"{field} must be an integer between 1900 and 2199.",
            status_code=400,
        )
    return value


def _legacy_key(result: InspectionEventResult) -> LegacyBridgeKey:
    identity = result.bridge_identity
    return LegacyBridgeKey(
        identity.source_agency_context,
        identity.normalized_state,
        identity.raw_structure_number,
    )


def _key_dict(key: LegacyBridgeKey) -> Dict[str, str]:
    return {
        "source_agency_context": key.source_agency_context,
        "normalized_state": key.normalized_state,
        "raw_structure_number": key.raw_structure_number,
    }


def _blocking_issue(
    code: str,
    message: str,
    key: LegacyBridgeKey,
    source_record_ids: Sequence[str],
) -> Dict[str, Any]:
    return {
        "code": code,
        "severity": "error",
        "message": message,
        "bridge_identity": _key_dict(key),
        "source_record_ids": list(source_record_ids),
        "rule_id": SUPPORTED_RULE_BUNDLE_ID,
    }


def _source_dict(source: ParsedAnnualSource) -> Dict[str, Any]:
    result = source.parse_result
    return {
        "part_name": source.spec.part_name,
        "inventory_year": source.spec.inventory_year,
        "artifact": dict(source.artifact),
        "parser": {
            "format": result.input_format.value,
            "encoding": result.encoding,
            "source_sha256": result.source_sha256,
            "bytes_read": result.bytes_read,
            "records_seen": result.records_seen,
            "records_accepted": len(result.records),
            "records_quarantined": len(result.quarantined_records),
            "bridge_groups": len(result.bridges),
        },
    }


def _result_dict(
    result: InspectionEventResult,
    *,
    extra_issues: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    inspections = []
    lineage: Dict[str, Any] = {}
    events = []
    for index, event in enumerate(result.events):
        row = event.to_snbi_dict()
        inspections.append(row)
        for tag, value in event.values.items():
            lineage[f"/Inspections/{index}/{tag}"] = _transition_dict(value)
        events.append(
            {
                "event_kind": event.event_kind.value,
                "status": event.status.value,
                "first_inventory_year": event.first_inventory_year,
                "selected_inventory_year": event.selected_inventory_year,
                "selected_source_record_id": event.selected_source_record_id,
                "source_record_ids": list(event.source_record_ids),
                "rule_ids": list(event.rule_ids),
                "values": row,
                "issues": [_issue_dict(issue) for issue in event.issues],
            }
        )
    issue_values = [_issue_dict(issue) for issue in result.issues]
    issue_values.extend(dict(issue) for issue in extra_issues)
    return {
        "bridge_identity": {
            "source_agency_context": result.bridge_identity.source_agency_context,
            "normalized_state": result.bridge_identity.normalized_state,
            "raw_structure_number": result.bridge_identity.raw_structure_number,
        },
        "rule_bundle_id": result.rule_bundle_id,
        "status": result.status.value,
        "inventory_years": list(result.inventory_years),
        "source_record_ids": list(result.source_record_ids),
        "records_supplied": result.records_supplied,
        "records_accepted": result.records_accepted,
        "Inspections": inspections,
        "lineage": lineage,
        "events": events,
        "issues": issue_values,
        "coverage": {
            "inventory_year_count": len(result.inventory_years),
            "event_count": len(result.events),
            "transition_value_count": sum(len(item.values) for item in result.events),
            "generated_proxy_value_count": sum(
                1
                for event in result.events
                for value in event.values.values()
                if value.generated
            ),
            "issue_count": len(issue_values),
        },
    }


def _transition_dict(value: InspectionTransitionValue) -> Dict[str, Any]:
    return {
        "value": value.value,
        "target_tag": value.target_tag,
        "rule_id": value.rule_id,
        "category": value.category,
        "source_record_ids": list(value.source_record_ids),
        "source_values": [
            {
                "source_record_id": source.source_record_id,
                "source_field": source.source_field,
                "raw_value": source.raw_value,
            }
            for source in value.source_values
        ],
        "transform_version": value.transform_version,
        "rounding_policy": value.rounding_policy,
        "status": value.status,
        "messages": list(value.messages),
        "generated": value.generated,
        "proxy_components": list(value.proxy_components),
    }


def _issue_dict(issue: InspectionIssue) -> Dict[str, Any]:
    return {
        "code": issue.code,
        "severity": issue.severity.value,
        "message": issue.message,
        "bridge_identity": {
            "source_agency_context": issue.bridge_identity.source_agency_context,
            "normalized_state": issue.bridge_identity.normalized_state,
            "raw_structure_number": issue.bridge_identity.raw_structure_number,
        },
        "inventory_year": issue.inventory_year,
        "source_record_ids": list(issue.source_record_ids),
        "source_field": issue.source_field,
        "raw_value": issue.raw_value,
        "rule_id": issue.rule_id,
    }


__all__ = [
    "AnnualSourceSpec",
    "HISTORY_ENGINE_VERSION",
    "HISTORY_MANIFEST_VERSION",
    "InspectionHistoryError",
    "InspectionHistoryManifest",
    "MAX_ANNUAL_SOURCES",
    "MAX_HISTORY_RECORDS",
    "ParsedAnnualSource",
    "PreparedInspectionHistory",
    "SUPPORTED_RULE_BUNDLE_ID",
    "parse_inspection_history_manifest",
    "prepare_inspection_history",
]
