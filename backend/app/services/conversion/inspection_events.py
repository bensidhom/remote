"""Order-independent reconstruction of SNBI inspection events.

The legacy Coding Guide stores inspection dates as month/year values on annual
inventory rows.  A single row cannot distinguish the first annual occurrence
from later routine occurrences, so this reducer operates across annual history.

This module is intentionally isolated from the production mapper.  It defines
small immutable input/output contracts locally so it does not depend on another
conversion workstream while that work is still being migrated.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import re
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

if TYPE_CHECKING:
    from .nbi_parser import SourceRecord


TransitionScalar = Union[str, int]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
INSPECTION_TRANSFORM_VERSION = "inspection-events-transition-v1"


class InspectionIssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class InspectionResultStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    EMPTY = "empty"


class InspectionEventStatus(str, Enum):
    EMITTED = "emitted"
    DEDUPLICATED = "deduplicated"


class InspectionEventKind(str, Enum):
    ROUTINE = "routine"
    NSTM = "nstm"
    UNDERWATER = "underwater"
    SPECIAL = "special"


class TwoDigitYearPolicy(str, Enum):
    """Century policy for legacy MMYY values.

    ``MODERN_ONLY`` is safe by default: it interprets only 2000-2099 dates that
    are not later than their inventory year.  Any value that might mean a
    pre-2000 date is rejected.  ``INVENTORY_CONTEXT_COMPAT`` is an explicit
    compatibility choice and selects the latest matching two-digit year not
    later than the inventory year.
    """

    MODERN_ONLY = "modern_2000_2099_only"
    INVENTORY_CONTEXT_COMPAT = "inventory_context_compatibility"


@dataclass(frozen=True, order=True)
class InspectionBridgeIdentity:
    """Immutable source identity; raw structure-number padding is retained."""

    source_agency_context: str
    normalized_state: str
    raw_structure_number: str

    def __post_init__(self) -> None:
        if not self.source_agency_context.strip():
            raise ValueError("source_agency_context is required")
        if not self.normalized_state.strip():
            raise ValueError("normalized_state is required")
        if not self.raw_structure_number.strip():
            raise ValueError("raw_structure_number is required")


@dataclass(frozen=True)
class InspectionSourceRecord:
    """Minimal immutable adapter contract for an annual legacy source row."""

    source_record_id: str
    bridge_identity: InspectionBridgeIdentity
    inventory_year: int
    fields: Mapping[str, Optional[str]]
    source_order: int = 0

    def __post_init__(self) -> None:
        if not self.source_record_id.strip():
            raise ValueError("source_record_id is required")
        if isinstance(self.inventory_year, bool) or not isinstance(
            self.inventory_year, int
        ):
            raise TypeError("inventory_year must be an integer")
        if not 1900 <= self.inventory_year <= 2199:
            raise ValueError("inventory_year must be between 1900 and 2199")
        if isinstance(self.source_order, bool) or not isinstance(self.source_order, int):
            raise TypeError("source_order must be an integer")
        copied: Dict[str, Optional[str]] = {}
        for key, value in self.fields.items():
            if not isinstance(key, str):
                raise TypeError("inspection source field names must be strings")
            copied[key] = None if value is None else str(value)
        object.__setattr__(self, "fields", MappingProxyType(copied))


@dataclass(frozen=True)
class InspectionRuleBundle:
    """Pinned transition behavior supplied explicitly to the reducer."""

    bundle_id: str = "fhwa-nbi-to-snbi-inspection-events-v1"
    two_digit_year_policy: TwoDigitYearPolicy = TwoDigitYearPolicy.MODERN_ONLY
    proxy_day: int = 1
    initial_rule_id: str = "fhwa-item90-first-annual-occurrence-v1"
    routine_rule_id: str = "fhwa-item90-later-annual-occurrence-v1"
    routine_date_rule_id: str = "fhwa-item90-mmyy-proxy-day-v1"
    routine_interval_rule_id: str = "fhwa-item91-interval-v1"
    nstm_rule_id: str = "fhwa-items92a-93a-nstm-event-v1"
    underwater_rule_id: str = "fhwa-items92b-93b-underwater-event-v1"
    special_rule_id: str = "fhwa-items92c-93c-special-event-v1"
    duplicate_rule_id: str = "fhwa-consecutive-annual-same-mmyy-dedup-v1"
    pre2000_compatibility_rule_id: str = (
        "fhwa-mmyy-inventory-context-century-compatibility-v1"
    )

    def __post_init__(self) -> None:
        if not self.bundle_id.strip():
            raise ValueError("bundle_id is required")
        try:
            year_policy = (
                self.two_digit_year_policy
                if isinstance(self.two_digit_year_policy, TwoDigitYearPolicy)
                else TwoDigitYearPolicy(self.two_digit_year_policy)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("two_digit_year_policy is invalid") from exc
        if self.proxy_day != 1:
            raise ValueError(
                "legacy MMYY compatibility currently permits proxy_day=1 only"
            )
        object.__setattr__(self, "two_digit_year_policy", year_policy)


@dataclass(frozen=True)
class InspectionSourceValue:
    source_record_id: str
    source_field: str
    raw_value: Optional[str]


@dataclass(frozen=True)
class InspectionTransitionValue:
    value: TransitionScalar
    target_tag: str
    rule_id: str
    source_record_ids: Tuple[str, ...]
    source_values: Tuple[InspectionSourceValue, ...]
    generated: bool = False
    proxy_components: Tuple[str, ...] = ()
    category: str = "derived"
    transform_version: str = INSPECTION_TRANSFORM_VERSION
    rounding_policy: str = "exact"
    status: str = "populated"
    messages: Tuple[str, ...] = ()


@dataclass(frozen=True)
class InspectionIssue:
    code: str
    severity: InspectionIssueSeverity
    message: str
    bridge_identity: InspectionBridgeIdentity
    inventory_year: Optional[int] = None
    source_record_ids: Tuple[str, ...] = ()
    source_field: Optional[str] = None
    raw_value: Optional[str] = None
    rule_id: Optional[str] = None


@dataclass(frozen=True)
class InspectionEvent:
    event_kind: InspectionEventKind
    status: InspectionEventStatus
    first_inventory_year: int
    selected_inventory_year: int
    selected_source_record_id: str
    source_record_ids: Tuple[str, ...]
    rule_ids: Tuple[str, ...]
    values: Mapping[str, InspectionTransitionValue]
    issues: Tuple[InspectionIssue, ...] = ()

    def to_snbi_dict(self) -> Dict[str, TransitionScalar]:
        """Materialize only supported transitions; no values are invented."""

        return {tag: transition.value for tag, transition in self.values.items()}


@dataclass(frozen=True)
class InspectionEventResult:
    bridge_identity: InspectionBridgeIdentity
    rule_bundle_id: str
    status: InspectionResultStatus
    inventory_years: Tuple[int, ...]
    source_record_ids: Tuple[str, ...]
    records_supplied: int
    records_accepted: int
    events: Tuple[InspectionEvent, ...]
    issues: Tuple[InspectionIssue, ...]


@dataclass(frozen=True)
class _ParsedDate:
    raw: str
    month: int
    year: int
    snbi_date: str
    used_legacy_policy: bool

    @property
    def mmyy_key(self) -> Tuple[int, int]:
        return self.year, self.month


@dataclass(frozen=True)
class _Candidate:
    kind: InspectionEventKind
    record: InspectionSourceRecord
    date_field: str
    date: _ParsedDate
    event_type: str
    event_rule_id: str
    interval_field: Optional[str]
    interval: Optional[int]
    issues: Tuple[InspectionIssue, ...] = ()


@dataclass(frozen=True)
class _Occurrence:
    kind: InspectionEventKind
    candidates: Tuple[_Candidate, ...]
    selected: _Candidate
    first_inventory_year: int
    issues: Tuple[InspectionIssue, ...]


_ROUTINE_DATE = "DATE_OF_INSPECT_090"
_ROUTINE_INTERVAL = "INSPECT_FREQ_MONTHS_091"

# Item 92 must explicitly make its paired Item 93 applicable before an event is
# emitted.  A date alone is not evidence that the inspection requirement applies.
_FEATURE_RULES: Tuple[
    Tuple[
        InspectionEventKind,
        str,
        str,
        str,
        str,
    ],
    ...,
] = (
    (
        InspectionEventKind.NSTM,
        "FRACTURE_092A",
        "FRACTURE_LAST_DATE_093A",
        "4",
        "nstm_rule_id",
    ),
    (
        InspectionEventKind.UNDERWATER,
        "UNDWATER_LOOK_SEE_092B",
        "UNDWATER_LAST_DATE_093B",
        "3",
        "underwater_rule_id",
    ),
    (
        InspectionEventKind.SPECIAL,
        "SPEC_INSPECT_092C",
        "SPEC_LAST_DATE_093C",
        "7",
        "special_rule_id",
    ),
)


def build_inspection_events(
    records_by_bridge_and_year: Mapping[
        InspectionBridgeIdentity,
        Mapping[int, Sequence[InspectionSourceRecord]],
    ],
    rule_bundle: InspectionRuleBundle,
) -> Tuple[InspectionEventResult, ...]:
    """Reduce annual source history into deterministic inspection events.

    Input grouping is validated instead of trusted.  A record whose immutable
    identity or inventory year differs from its group is excluded and reported.
    Results are sorted by bridge identity, so mapping/sequence insertion order
    cannot affect the output.
    """

    if not isinstance(rule_bundle, InspectionRuleBundle):
        raise TypeError("rule_bundle must be an InspectionRuleBundle")
    if not isinstance(records_by_bridge_and_year, Mapping):
        raise TypeError("records_by_bridge_and_year must be a mapping")

    results: List[InspectionEventResult] = []
    for identity in sorted(records_by_bridge_and_year):
        if not isinstance(identity, InspectionBridgeIdentity):
            raise TypeError("bridge grouping keys must be InspectionBridgeIdentity")
        years = records_by_bridge_and_year[identity]
        if not isinstance(years, Mapping):
            raise TypeError("each bridge group must map inventory years to records")
        results.append(_reduce_bridge(identity, years, rule_bundle))
    return tuple(results)


def inspection_source_from_nbi_record(
    source_record: "SourceRecord",
    *,
    inventory_year: int,
    artifact_sha256: Optional[str] = None,
) -> InspectionSourceRecord:
    """Adapt the hardened NBI parser record without dropping provenance.

    The annual inventory year is explicit because it belongs to the source-file
    context, not necessarily to one stable parser field.  Importing the parser
    type inside the function keeps this reducer independently importable and
    prevents a future package-initialization cycle.
    """

    from .nbi_parser import SourceRecord

    if not isinstance(source_record, SourceRecord):
        raise TypeError("source_record must be nbi_parser.SourceRecord")
    key = source_record.bridge_key
    source_record_id = source_record.source_record_id
    if artifact_sha256 is not None:
        normalized_sha256 = str(artifact_sha256).strip().lower()
        if _SHA256_RE.fullmatch(normalized_sha256) is None:
            raise ValueError("artifact_sha256 must be 64 lowercase hexadecimal characters")
        source_record_id = hashlib.sha256(
            (
                "inspection-source-v1\x00"
                f"{normalized_sha256}\x00{inventory_year}\x00"
                f"{source_record.source_record_id}"
            ).encode("ascii")
        ).hexdigest()
    return InspectionSourceRecord(
        source_record_id=source_record_id,
        bridge_identity=InspectionBridgeIdentity(
            source_agency_context=key.source_agency_context,
            normalized_state=key.normalized_state,
            raw_structure_number=key.raw_structure_number,
        ),
        inventory_year=inventory_year,
        fields=source_record.fields,
        source_order=source_record.source_order,
    )


def build_inspection_event(
    records_by_bridge_and_year: Mapping[
        InspectionBridgeIdentity,
        Mapping[int, Sequence[InspectionSourceRecord]],
    ],
    rule_bundle: InspectionRuleBundle,
) -> Tuple[InspectionEventResult, ...]:
    """Compatibility alias using the singular wording from early plans."""

    return build_inspection_events(records_by_bridge_and_year, rule_bundle)


def _reduce_bridge(
    identity: InspectionBridgeIdentity,
    years: Mapping[int, Sequence[InspectionSourceRecord]],
    rules: InspectionRuleBundle,
) -> InspectionEventResult:
    issues: List[InspectionIssue] = []
    accepted: List[InspectionSourceRecord] = []
    supplied = 0

    for grouped_year in sorted(years):
        if isinstance(grouped_year, bool) or not isinstance(grouped_year, int):
            raise TypeError("inventory-year grouping keys must be integers")
        records = years[grouped_year]
        if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
            raise TypeError("each inventory-year group must contain a sequence")
        for record in records:
            supplied += 1
            if not isinstance(record, InspectionSourceRecord):
                raise TypeError("annual groups may contain only InspectionSourceRecord")
            if record.bridge_identity != identity:
                issues.append(
                    InspectionIssue(
                        code="group_identity_mismatch",
                        severity=InspectionIssueSeverity.ERROR,
                        message=(
                            "source record bridge identity differs from its immutable "
                            "bridge group"
                        ),
                        bridge_identity=identity,
                        inventory_year=grouped_year,
                        source_record_ids=(record.source_record_id,),
                    )
                )
                continue
            if record.inventory_year != grouped_year:
                issues.append(
                    InspectionIssue(
                        code="group_inventory_year_mismatch",
                        severity=InspectionIssueSeverity.ERROR,
                        message=(
                            "source record inventory year differs from its annual group"
                        ),
                        bridge_identity=identity,
                        inventory_year=grouped_year,
                        source_record_ids=(record.source_record_id,),
                    )
                )
                continue
            accepted.append(record)

    accepted.sort(key=_record_sort_key)
    seen_source_ids: Dict[str, InspectionSourceRecord] = {}
    unique_records: List[InspectionSourceRecord] = []
    for record in accepted:
        prior = seen_source_ids.get(record.source_record_id)
        if prior is not None:
            issues.append(
                InspectionIssue(
                    code="duplicate_source_record_id",
                    severity=InspectionIssueSeverity.ERROR,
                    message="duplicate source_record_id was excluded",
                    bridge_identity=identity,
                    inventory_year=record.inventory_year,
                    source_record_ids=(record.source_record_id,),
                )
            )
            continue
        seen_source_ids[record.source_record_id] = record
        unique_records.append(record)

    candidates: List[_Candidate] = []
    for record in unique_records:
        routine, routine_issues = _routine_candidate(record, rules)
        issues.extend(routine_issues)
        if routine is not None:
            candidates.append(routine)

        for feature_rule in _FEATURE_RULES:
            candidate, feature_issues = _feature_candidate(record, feature_rule, rules)
            issues.extend(feature_issues)
            if candidate is not None:
                candidates.append(candidate)

    occurrences = _deduplicate_candidates(candidates, identity, rules)
    events: List[InspectionEvent] = []

    routine_occurrences = sorted(
        (item for item in occurrences if item.kind is InspectionEventKind.ROUTINE),
        key=_occurrence_sort_key,
    )
    for index, occurrence in enumerate(routine_occurrences):
        event_type = "1" if index == 0 else "2"
        event_rule = rules.initial_rule_id if index == 0 else rules.routine_rule_id
        event = _materialize_event(occurrence, event_type, event_rule, rules)
        events.append(event)
        issues.extend(event.issues)

    for occurrence in sorted(
        (item for item in occurrences if item.kind is not InspectionEventKind.ROUTINE),
        key=_occurrence_sort_key,
    ):
        selected = occurrence.selected
        event = _materialize_event(
            occurrence,
            selected.event_type,
            selected.event_rule_id,
            rules,
        )
        events.append(event)
        issues.extend(event.issues)

    events.sort(key=_event_sort_key)
    errors = any(issue.severity is InspectionIssueSeverity.ERROR for issue in issues)
    quality_issues = any(
        issue.severity
        in {InspectionIssueSeverity.WARNING, InspectionIssueSeverity.ERROR}
        for issue in issues
    )
    if events and quality_issues:
        status = InspectionResultStatus.PARTIAL
    elif events:
        status = InspectionResultStatus.COMPLETE
    elif errors:
        status = InspectionResultStatus.FAILED
    else:
        status = InspectionResultStatus.EMPTY

    return InspectionEventResult(
        bridge_identity=identity,
        rule_bundle_id=rules.bundle_id,
        status=status,
        inventory_years=tuple(sorted({record.inventory_year for record in unique_records})),
        source_record_ids=tuple(record.source_record_id for record in unique_records),
        records_supplied=supplied,
        records_accepted=len(unique_records),
        events=tuple(events),
        issues=tuple(_deduplicate_issues(issues)),
    )


def _routine_candidate(
    record: InspectionSourceRecord,
    rules: InspectionRuleBundle,
) -> Tuple[Optional[_Candidate], Tuple[InspectionIssue, ...]]:
    raw = record.fields.get(_ROUTINE_DATE)
    if raw is None or not raw.strip():
        issue = InspectionIssue(
            code="item90_missing",
            severity=InspectionIssueSeverity.WARNING,
            message="Item 90 inspection date is missing; no annual routine event emitted",
            bridge_identity=record.bridge_identity,
            inventory_year=record.inventory_year,
            source_record_ids=(record.source_record_id,),
            source_field=_ROUTINE_DATE,
            raw_value=raw,
            rule_id=rules.routine_date_rule_id,
        )
        return None, (issue,)

    parsed, date_issue = _parse_mmyy(
        raw,
        record,
        _ROUTINE_DATE,
        rules.routine_date_rule_id,
        rules,
    )
    if parsed is None:
        return None, (date_issue,) if date_issue is not None else ()

    interval, interval_issue = _parse_interval(
        record.fields.get(_ROUTINE_INTERVAL),
        record,
        _ROUTINE_INTERVAL,
        rules.routine_interval_rule_id,
        required=False,
    )
    candidate_issue_list = []
    if date_issue is not None:
        candidate_issue_list.append(date_issue)
    if interval_issue is not None:
        candidate_issue_list.append(interval_issue)
    candidate_issues = tuple(candidate_issue_list)
    candidate = _Candidate(
        kind=InspectionEventKind.ROUTINE,
        record=record,
        date_field=_ROUTINE_DATE,
        date=parsed,
        event_type="",
        event_rule_id="",
        interval_field=_ROUTINE_INTERVAL if interval is not None else None,
        interval=interval,
        issues=candidate_issues,
    )
    return candidate, candidate_issues


def _feature_candidate(
    record: InspectionSourceRecord,
    feature_rule: Tuple[InspectionEventKind, str, str, str, str],
    rules: InspectionRuleBundle,
) -> Tuple[Optional[_Candidate], Tuple[InspectionIssue, ...]]:
    kind, flag_field, date_field, event_type, rule_attribute = feature_rule
    event_rule_id = str(getattr(rules, rule_attribute))
    raw_flag = record.fields.get(flag_field)
    raw_date = record.fields.get(date_field)
    issues: List[InspectionIssue] = []

    applicability, interval, flag_issue = _parse_item92(
        raw_flag, record, flag_field, event_rule_id
    )
    if flag_issue is not None:
        issues.append(flag_issue)

    date_supplied = raw_date is not None and bool(raw_date.strip())
    if applicability is False:
        if date_supplied:
            issues.append(
                InspectionIssue(
                    code="inapplicable_item93_ignored",
                    severity=InspectionIssueSeverity.WARNING,
                    message=(
                        f"{date_field} was supplied while {flag_field} marks the "
                        "inspection not applicable; no event emitted"
                    ),
                    bridge_identity=record.bridge_identity,
                    inventory_year=record.inventory_year,
                    source_record_ids=(record.source_record_id,),
                    source_field=date_field,
                    raw_value=raw_date,
                    rule_id=event_rule_id,
                )
            )
        return None, tuple(issues)

    if applicability is None:
        if date_supplied and flag_issue is None:
            issues.append(
                InspectionIssue(
                    code="item92_required_for_item93",
                    severity=InspectionIssueSeverity.ERROR,
                    message=(
                        f"{date_field} cannot create an event without an applicable "
                        f"{flag_field} value"
                    ),
                    bridge_identity=record.bridge_identity,
                    inventory_year=record.inventory_year,
                    source_record_ids=(record.source_record_id,),
                    source_field=flag_field,
                    raw_value=raw_flag,
                    rule_id=event_rule_id,
                )
            )
        return None, tuple(issues)

    # Applicable Item 92 requires its paired Item 93 date.
    if not date_supplied:
        issues.append(
            InspectionIssue(
                code="applicable_item93_missing",
                severity=InspectionIssueSeverity.ERROR,
                message=(
                    f"{flag_field} requires an inspection, but {date_field} is missing"
                ),
                bridge_identity=record.bridge_identity,
                inventory_year=record.inventory_year,
                source_record_ids=(record.source_record_id,),
                source_field=date_field,
                raw_value=raw_date,
                rule_id=event_rule_id,
            )
        )
        return None, tuple(issues)

    parsed_date, date_issue = _parse_mmyy(
        raw_date or "",
        record,
        date_field,
        event_rule_id,
        rules,
    )
    if date_issue is not None:
        issues.append(date_issue)
    if parsed_date is None:
        return None, tuple(issues)

    return (
        _Candidate(
            kind=kind,
            record=record,
            date_field=date_field,
            date=parsed_date,
            event_type=event_type,
            event_rule_id=event_rule_id,
            interval_field=flag_field,
            interval=interval,
            issues=tuple(issues),
        ),
        tuple(issues),
    )


def _parse_mmyy(
    raw: str,
    record: InspectionSourceRecord,
    source_field: str,
    rule_id: str,
    rules: InspectionRuleBundle,
) -> Tuple[Optional[_ParsedDate], Optional[InspectionIssue]]:
    stripped = raw.strip()
    if len(stripped) != 4 or not stripped.isascii() or not stripped.isdigit():
        return None, InspectionIssue(
            code="inspection_date_invalid_format",
            severity=InspectionIssueSeverity.ERROR,
            message=f"{source_field} must use exactly four ASCII digits in MMYY form",
            bridge_identity=record.bridge_identity,
            inventory_year=record.inventory_year,
            source_record_ids=(record.source_record_id,),
            source_field=source_field,
            raw_value=raw,
            rule_id=rule_id,
        )

    month = int(stripped[:2])
    short_year = int(stripped[2:])
    if not 1 <= month <= 12:
        return None, InspectionIssue(
            code="inspection_date_invalid_month",
            severity=InspectionIssueSeverity.ERROR,
            message=f"{source_field} month must be between 01 and 12",
            bridge_identity=record.bridge_identity,
            inventory_year=record.inventory_year,
            source_record_ids=(record.source_record_id,),
            source_field=source_field,
            raw_value=raw,
            rule_id=rule_id,
        )

    modern_year = 2000 + short_year
    used_legacy_policy = False
    if modern_year <= record.inventory_year:
        year = modern_year
    elif rules.two_digit_year_policy is TwoDigitYearPolicy.INVENTORY_CONTEXT_COMPAT:
        century = (record.inventory_year // 100) * 100
        year = century + short_year
        if year > record.inventory_year:
            year -= 100
        used_legacy_policy = year < 2000
    else:
        return None, InspectionIssue(
            code="pre2000_date_policy_required",
            severity=InspectionIssueSeverity.ERROR,
            message=(
                f"{source_field}={stripped} cannot be interpreted as a non-future "
                "2000-2099 date; an explicit pre-2000 compatibility policy is required"
            ),
            bridge_identity=record.bridge_identity,
            inventory_year=record.inventory_year,
            source_record_ids=(record.source_record_id,),
            source_field=source_field,
            raw_value=raw,
            rule_id=rule_id,
        )

    if year < 1900 or year > record.inventory_year:
        return None, InspectionIssue(
            code="inspection_date_out_of_range",
            severity=InspectionIssueSeverity.ERROR,
            message=(
                f"{source_field} resolved to {year}, outside the supported source "
                "history or later than the inventory year"
            ),
            bridge_identity=record.bridge_identity,
            inventory_year=record.inventory_year,
            source_record_ids=(record.source_record_id,),
            source_field=source_field,
            raw_value=raw,
            rule_id=rule_id,
        )

    return (
        _ParsedDate(
            raw=raw,
            month=month,
            year=year,
            snbi_date=f"{year:04d}{month:02d}{rules.proxy_day:02d}",
            used_legacy_policy=used_legacy_policy,
        ),
        (
            InspectionIssue(
                code="pre2000_compatibility_applied",
                severity=InspectionIssueSeverity.INFO,
                message=(
                    f"{source_field} used the explicit inventory-context "
                    "compatibility rule to resolve its two-digit year"
                ),
                bridge_identity=record.bridge_identity,
                inventory_year=record.inventory_year,
                source_record_ids=(record.source_record_id,),
                source_field=source_field,
                raw_value=raw,
                rule_id=rules.pre2000_compatibility_rule_id,
            )
            if used_legacy_policy
            else None
        ),
    )


def _parse_item92(
    raw: Optional[str],
    record: InspectionSourceRecord,
    source_field: str,
    rule_id: str,
) -> Tuple[Optional[bool], Optional[int], Optional[InspectionIssue]]:
    if raw is None or not raw.strip():
        return None, None, None
    stripped = raw.strip().upper()
    if stripped == "N" or (
        len(stripped) == 3 and stripped.startswith("N") and stripped[1:].isdigit()
    ):
        return False, None, None
    if len(stripped) == 3 and stripped.startswith("Y") and stripped[1:].isdigit():
        return True, int(stripped[1:]), None
    return None, None, InspectionIssue(
        code="item92_invalid",
        severity=InspectionIssueSeverity.ERROR,
        message=(
            f"{source_field} must be N/Nxx or an applicable Y followed by a "
            "two-digit interval"
        ),
        bridge_identity=record.bridge_identity,
        inventory_year=record.inventory_year,
        source_record_ids=(record.source_record_id,),
        source_field=source_field,
        raw_value=raw,
        rule_id=rule_id,
    )


def _parse_interval(
    raw: Optional[str],
    record: InspectionSourceRecord,
    source_field: str,
    rule_id: str,
    *,
    required: bool,
) -> Tuple[Optional[int], Optional[InspectionIssue]]:
    if raw is None or not raw.strip():
        if not required:
            return None, None
        return None, InspectionIssue(
            code="inspection_interval_missing",
            severity=InspectionIssueSeverity.ERROR,
            message=f"{source_field} is required",
            bridge_identity=record.bridge_identity,
            inventory_year=record.inventory_year,
            source_record_ids=(record.source_record_id,),
            source_field=source_field,
            raw_value=raw,
            rule_id=rule_id,
        )
    stripped = raw.strip()
    if not stripped.isascii() or not stripped.isdigit():
        return None, InspectionIssue(
            code="inspection_interval_invalid",
            severity=InspectionIssueSeverity.ERROR,
            message=f"{source_field} must contain nonnegative integer months",
            bridge_identity=record.bridge_identity,
            inventory_year=record.inventory_year,
            source_record_ids=(record.source_record_id,),
            source_field=source_field,
            raw_value=raw,
            rule_id=rule_id,
        )
    return int(stripped), None


def _deduplicate_candidates(
    candidates: Iterable[_Candidate],
    identity: InspectionBridgeIdentity,
    rules: InspectionRuleBundle,
) -> Tuple[_Occurrence, ...]:
    grouped: Dict[Tuple[InspectionEventKind, Tuple[int, int]], List[_Candidate]] = {}
    for candidate in candidates:
        grouped.setdefault((candidate.kind, candidate.date.mmyy_key), []).append(candidate)

    occurrences: List[_Occurrence] = []
    for group_key in sorted(grouped, key=lambda value: (value[0].value, value[1])):
        ordered = sorted(grouped[group_key], key=lambda item: _record_sort_key(item.record))
        runs: List[List[_Candidate]] = []
        for candidate in ordered:
            if not runs:
                runs.append([candidate])
                continue
            previous_year = runs[-1][-1].record.inventory_year
            if candidate.record.inventory_year <= previous_year + 1:
                runs[-1].append(candidate)
            else:
                runs.append([candidate])

        for run in runs:
            selected = max(run, key=lambda item: _record_sort_key(item.record))
            occurrence_issues: List[InspectionIssue] = []
            if len(run) > 1:
                source_ids = tuple(item.record.source_record_id for item in run)
                occurrence_issues.append(
                    InspectionIssue(
                        code="duplicate_annual_mmyy_deduplicated",
                        severity=InspectionIssueSeverity.INFO,
                        message=(
                            "the same inspection MMYY appeared in the same or "
                            "consecutive annual files; the most recent source was "
                            "selected and all lineages retained"
                        ),
                        bridge_identity=identity,
                        inventory_year=selected.record.inventory_year,
                        source_record_ids=source_ids,
                        source_field=selected.date_field,
                        raw_value=selected.date.raw,
                        rule_id=rules.duplicate_rule_id,
                    )
                )
            occurrences.append(
                _Occurrence(
                    kind=selected.kind,
                    candidates=tuple(run),
                    selected=selected,
                    first_inventory_year=min(
                        item.record.inventory_year for item in run
                    ),
                    issues=tuple(occurrence_issues),
                )
            )
    return tuple(occurrences)


def _materialize_event(
    occurrence: _Occurrence,
    event_type: str,
    event_rule_id: str,
    rules: InspectionRuleBundle,
) -> InspectionEvent:
    selected = occurrence.selected
    lineage = tuple(
        candidate.record.source_record_id for candidate in occurrence.candidates
    )
    date_sources = tuple(
        InspectionSourceValue(
            source_record_id=candidate.record.source_record_id,
            source_field=candidate.date_field,
            raw_value=candidate.record.fields.get(candidate.date_field),
        )
        for candidate in occurrence.candidates
    )
    values: Dict[str, InspectionTransitionValue] = {
        "BIE01": InspectionTransitionValue(
            value=event_type,
            target_tag="BIE01",
            rule_id=event_rule_id,
            source_record_ids=lineage,
            source_values=date_sources,
        ),
        "BIE02": InspectionTransitionValue(
            value=selected.date.snbi_date,
            target_tag="BIE02",
            rule_id=(
                rules.routine_date_rule_id
                if selected.kind is InspectionEventKind.ROUTINE
                else selected.event_rule_id
            ),
            source_record_ids=lineage,
            source_values=date_sources,
            generated=True,
            status="generated",
            messages=(
                "The legacy MMYY value has no day; day 01 is an explicit generated proxy.",
            ),
            proxy_components=(
                ("day", "century")
                if selected.date.used_legacy_policy
                else ("day",)
            ),
        ),
    }

    if selected.interval is not None and selected.interval_field is not None:
        interval_sources = tuple(
            InspectionSourceValue(
                source_record_id=candidate.record.source_record_id,
                source_field=candidate.interval_field or selected.interval_field,
                raw_value=candidate.record.fields.get(
                    candidate.interval_field or selected.interval_field
                ),
            )
            for candidate in occurrence.candidates
        )
        values["BIE05"] = InspectionTransitionValue(
            value=selected.interval,
            target_tag="BIE05",
            rule_id=(
                rules.routine_interval_rule_id
                if selected.kind is InspectionEventKind.ROUTINE
                else selected.event_rule_id
            ),
            source_record_ids=lineage,
            source_values=interval_sources,
            category="direct",
        )

    rule_ids = [event_rule_id, values["BIE02"].rule_id]
    if "BIE05" in values:
        rule_ids.append(values["BIE05"].rule_id)
    if occurrence.issues:
        rule_ids.append(rules.duplicate_rule_id)
    if selected.date.used_legacy_policy:
        rule_ids.append(rules.pre2000_compatibility_rule_id)
    event_issues = list(occurrence.issues)
    for candidate in occurrence.candidates:
        event_issues.extend(candidate.issues)

    return InspectionEvent(
        event_kind=selected.kind,
        status=(
            InspectionEventStatus.DEDUPLICATED
            if len(occurrence.candidates) > 1
            else InspectionEventStatus.EMITTED
        ),
        first_inventory_year=occurrence.first_inventory_year,
        selected_inventory_year=selected.record.inventory_year,
        selected_source_record_id=selected.record.source_record_id,
        source_record_ids=lineage,
        rule_ids=tuple(dict.fromkeys(rule_ids)),
        values=MappingProxyType(values),
        issues=tuple(_deduplicate_issues(event_issues)),
    )


def _record_sort_key(record: InspectionSourceRecord) -> Tuple[int, int, str]:
    return record.inventory_year, record.source_order, record.source_record_id


def _occurrence_sort_key(
    occurrence: _Occurrence,
) -> Tuple[int, int, int, str, str]:
    selected = occurrence.selected
    return (
        occurrence.first_inventory_year,
        selected.date.year,
        selected.date.month,
        selected.kind.value,
        selected.record.source_record_id,
    )


def _event_sort_key(event: InspectionEvent) -> Tuple[int, str, str, str]:
    return (
        event.first_inventory_year,
        str(event.values["BIE02"].value),
        event.event_kind.value,
        event.selected_source_record_id,
    )


def _deduplicate_issues(issues: Iterable[InspectionIssue]) -> List[InspectionIssue]:
    unique: List[InspectionIssue] = []
    seen = set()
    for issue in issues:
        key = (
            issue.code,
            issue.severity,
            issue.message,
            issue.bridge_identity,
            issue.inventory_year,
            issue.source_record_ids,
            issue.source_field,
            issue.raw_value,
            issue.rule_id,
        )
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    unique.sort(
        key=lambda issue: (
            issue.inventory_year if issue.inventory_year is not None else -1,
            issue.source_record_ids,
            issue.code,
        )
    )
    return unique


__all__ = [
    "InspectionBridgeIdentity",
    "InspectionEvent",
    "InspectionEventKind",
    "InspectionEventResult",
    "InspectionEventStatus",
    "InspectionIssue",
    "InspectionIssueSeverity",
    "InspectionResultStatus",
    "InspectionRuleBundle",
    "InspectionSourceRecord",
    "InspectionSourceValue",
    "InspectionTransitionValue",
    "INSPECTION_TRANSFORM_VERSION",
    "TwoDigitYearPolicy",
    "build_inspection_event",
    "build_inspection_events",
    "inspection_source_from_nbi_record",
]
