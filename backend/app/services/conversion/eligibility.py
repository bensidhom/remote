"""Pure eligibility and BID01 policy decisions for legacy NBI conversion.

These rules are internal, review-stage decisions.  They are deliberately not
registered as executable FHWA submission bundles and perform no mapping, file
export, database write, or workspace/state filtering.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.services.conversion.nbi_parser import (
    LegacyBridgeKey,
    LegacyBridgeRecords,
    RecordType,
    SourceRecord,
)


ELIGIBLE_ITEM_42A_VALUES = frozenset({"1", "4", "5", "6", "7", "8"})
VALID_ITEM_42A_VALUES = frozenset(str(value) for value in range(10))
VALID_ITEM_112_VALUES = frozenset({"Y", "N"})


class EligibilityProfile(str, Enum):
    SUBMISSION_TRANSITION_REVIEW = "submission_transition_review"
    RESEARCH_HISTORICAL_IMPORT = "research_historical_import"


class InternalRuleStatus(str, Enum):
    INTERNAL_UNVERIFIED = "internal_unverified_not_submission_ready"


class EligibilityRule(str, Enum):
    OVER_ITEM42A_ITEM112_V1 = "internal_over_item42a_item112_v1"
    UNDER_REQUIRES_ELIGIBLE_OVER_V1 = "internal_under_requires_eligible_over_v1"
    DUPLICATE_OVER_REVIEW_V1 = "internal_duplicate_over_review_v1"


class EligibilityReason(str, Enum):
    ELIGIBLE_HIGHWAY_NBIS = "eligible_highway_nbis"
    ELIGIBLE_UNDER_VIA_OVER = "eligible_under_via_over"
    SERVICE_ON_EXCLUDED = "service_on_excluded"
    NOT_NBIS_LENGTH = "not_nbis_length"
    MISSING_REQUIRED_SOURCE = "missing_required_source"
    INVALID_SERVICE_ON = "invalid_service_on"
    INVALID_NBIS_LENGTH = "invalid_nbis_length"
    UNDER_WITHOUT_ELIGIBLE_OVER = "under_without_eligible_over"
    DUPLICATE_OVER_NOT_SELECTED = "duplicate_over_not_selected"
    RESEARCH_ONLY = "research_only"


class ReviewAction(str, Enum):
    SUPPLY_ITEM_42A = "supply_missing_item_42a"
    SUPPLY_ITEM_112 = "supply_missing_item_112"
    CORRECT_ITEM_42A = "correct_invalid_item_42a"
    CORRECT_ITEM_112 = "correct_invalid_item_112"
    PROVIDE_ELIGIBLE_OVER = "provide_or_correct_eligible_over_record"
    RESOLVE_DUPLICATE_OVER = "resolve_duplicate_over_record"


@dataclass(frozen=True)
class SourceValueReference:
    source_record_id: str
    field_name: str
    exact_source_value: Optional[str]
    line_number: int
    byte_offset: int
    raw_sha256: str


@dataclass(frozen=True)
class EligibilityDecision:
    """Eligibility is federal-transition eligibility, not retention.

    ``retained_by_profile`` is separate so research imports can retain a record
    while truthfully recording that it is not federally transition-eligible.
    """

    eligible: bool
    retained_by_profile: bool
    profile_id: EligibilityProfile
    rule_id: EligibilityRule
    rule_status: InternalRuleStatus
    source_record_id: str
    bridge_key: LegacyBridgeKey
    record_type: RecordType
    normalized_item_42a: Optional[str]
    normalized_item_112: Optional[str]
    source_references: Tuple[SourceValueReference, ...]
    reason_codes: Tuple[EligibilityReason, ...]
    warnings: Tuple[str, ...]
    review_actions: Tuple[ReviewAction, ...]


@dataclass(frozen=True)
class BridgeEligibilityResult:
    bridge_key: LegacyBridgeKey
    profile_id: EligibilityProfile
    rule_status: InternalRuleStatus
    decisions: Tuple[EligibilityDecision, ...]

    @property
    def retained_by_profile(self) -> bool:
        return any(decision.retained_by_profile for decision in self.decisions)

    @property
    def has_federally_eligible_record(self) -> bool:
        return any(decision.eligible for decision in self.decisions)


def evaluate_bridge_eligibility(
    group: LegacyBridgeRecords,
    *,
    profile: EligibilityProfile,
) -> BridgeEligibilityResult:
    """Evaluate selected over, duplicate-over, and under records deterministically."""

    selected_profile = EligibilityProfile(profile)
    decisions: List[EligibilityDecision] = []
    over_decision: Optional[EligibilityDecision] = None

    if group.over is not None:
        over_decision = _evaluate_selected_over(group.over, selected_profile)
        decisions.append(over_decision)

    for duplicate in group.duplicate_overs:
        decisions.append(_evaluate_duplicate_over(duplicate, selected_profile))

    for under in group.unders:
        decisions.append(
            _evaluate_under(
                under,
                selected_profile,
                selected_over=group.over,
                over_decision=over_decision,
            )
        )

    decisions.sort(key=lambda decision: _decision_source_order(decision, group))
    return BridgeEligibilityResult(
        bridge_key=group.key,
        profile_id=selected_profile,
        rule_status=InternalRuleStatus.INTERNAL_UNVERIFIED,
        decisions=tuple(decisions),
    )


def evaluate_many_bridge_groups(
    groups: Iterable[LegacyBridgeRecords],
    *,
    profile: EligibilityProfile,
) -> Mapping[LegacyBridgeKey, BridgeEligibilityResult]:
    """Return an order-independent immutable mapping keyed by full bridge identity."""

    selected_profile = EligibilityProfile(profile)
    evaluated: Dict[LegacyBridgeKey, BridgeEligibilityResult] = {}
    for group in groups:
        if group.key in evaluated:
            raise ValueError(f"duplicate bridge group supplied: {group.key}")
        evaluated[group.key] = evaluate_bridge_eligibility(
            group, profile=selected_profile
        )
    return MappingProxyType(dict(sorted(evaluated.items())))


def source_record_id(record: SourceRecord) -> str:
    """Return the immutable parser-assigned source record identifier."""

    if not record.source_record_id:
        raise ValueError("SourceRecord is missing its parser-assigned source_record_id")
    return record.source_record_id


def _evaluate_selected_over(
    record: SourceRecord, profile: EligibilityProfile
) -> EligibilityDecision:
    raw_42a = record.fields.get("SERVICE_ON_042A")
    raw_112 = record.fields.get("BRIDGE_LEN_IND_112")
    normalized_42a = _normalize_code(raw_42a)
    normalized_112 = _normalize_code(raw_112)
    reasons: List[EligibilityReason] = []
    warnings: List[str] = []
    actions: List[ReviewAction] = []

    if normalized_42a is None:
        reasons.append(EligibilityReason.MISSING_REQUIRED_SOURCE)
        warnings.append("Item 42A is missing")
        actions.append(ReviewAction.SUPPLY_ITEM_42A)
    elif normalized_42a not in VALID_ITEM_42A_VALUES:
        reasons.append(EligibilityReason.INVALID_SERVICE_ON)
        warnings.append(f"Item 42A value {raw_42a!r} is outside the legacy code set")
        actions.append(ReviewAction.CORRECT_ITEM_42A)
    elif normalized_42a not in ELIGIBLE_ITEM_42A_VALUES:
        reasons.append(EligibilityReason.SERVICE_ON_EXCLUDED)

    if normalized_112 is None:
        if EligibilityReason.MISSING_REQUIRED_SOURCE not in reasons:
            reasons.append(EligibilityReason.MISSING_REQUIRED_SOURCE)
        warnings.append("Item 112 is missing")
        actions.append(ReviewAction.SUPPLY_ITEM_112)
    elif normalized_112 not in VALID_ITEM_112_VALUES:
        reasons.append(EligibilityReason.INVALID_NBIS_LENGTH)
        warnings.append(f"Item 112 value {raw_112!r} must be Y or N")
        actions.append(ReviewAction.CORRECT_ITEM_112)
    elif normalized_112 != "Y":
        reasons.append(EligibilityReason.NOT_NBIS_LENGTH)

    eligible = (
        normalized_42a in ELIGIBLE_ITEM_42A_VALUES
        and normalized_112 == "Y"
    )
    if eligible:
        reasons.append(EligibilityReason.ELIGIBLE_HIGHWAY_NBIS)

    return _decision(
        record=record,
        profile=profile,
        rule=EligibilityRule.OVER_ITEM42A_ITEM112_V1,
        eligible=eligible,
        normalized_42a=normalized_42a,
        normalized_112=normalized_112,
        references=(
            _source_reference(record, "SERVICE_ON_042A", raw_42a),
            _source_reference(record, "BRIDGE_LEN_IND_112", raw_112),
        ),
        reasons=reasons,
        warnings=warnings,
        actions=actions,
    )


def _evaluate_under(
    record: SourceRecord,
    profile: EligibilityProfile,
    *,
    selected_over: Optional[SourceRecord],
    over_decision: Optional[EligibilityDecision],
) -> EligibilityDecision:
    if selected_over is not None and over_decision is not None:
        normalized_42a = over_decision.normalized_item_42a
        normalized_112 = over_decision.normalized_item_112
        references = over_decision.source_references
    else:
        normalized_42a = None
        normalized_112 = None
        references = ()

    if over_decision is not None and over_decision.eligible:
        eligible = True
        reasons = [EligibilityReason.ELIGIBLE_UNDER_VIA_OVER]
        warnings: List[str] = []
        actions: List[ReviewAction] = []
    else:
        eligible = False
        reasons = [EligibilityReason.UNDER_WITHOUT_ELIGIBLE_OVER]
        warnings = [
            "under record cannot transition without a selected eligible Type 1 over record"
        ]
        actions = [ReviewAction.PROVIDE_ELIGIBLE_OVER]

    return _decision(
        record=record,
        profile=profile,
        rule=EligibilityRule.UNDER_REQUIRES_ELIGIBLE_OVER_V1,
        eligible=eligible,
        normalized_42a=normalized_42a,
        normalized_112=normalized_112,
        references=references,
        reasons=reasons,
        warnings=warnings,
        actions=actions,
    )


def _evaluate_duplicate_over(
    record: SourceRecord, profile: EligibilityProfile
) -> EligibilityDecision:
    raw_42a = record.fields.get("SERVICE_ON_042A")
    raw_112 = record.fields.get("BRIDGE_LEN_IND_112")
    return _decision(
        record=record,
        profile=profile,
        rule=EligibilityRule.DUPLICATE_OVER_REVIEW_V1,
        eligible=False,
        normalized_42a=_normalize_code(raw_42a),
        normalized_112=_normalize_code(raw_112),
        references=(
            _source_reference(record, "SERVICE_ON_042A", raw_42a),
            _source_reference(record, "BRIDGE_LEN_IND_112", raw_112),
        ),
        reasons=[EligibilityReason.DUPLICATE_OVER_NOT_SELECTED],
        warnings=["duplicate Type 1 over record is not eligible for transition"],
        actions=[ReviewAction.RESOLVE_DUPLICATE_OVER],
    )


def _decision(
    *,
    record: SourceRecord,
    profile: EligibilityProfile,
    rule: EligibilityRule,
    eligible: bool,
    normalized_42a: Optional[str],
    normalized_112: Optional[str],
    references: Sequence[SourceValueReference],
    reasons: Sequence[EligibilityReason],
    warnings: Sequence[str],
    actions: Sequence[ReviewAction],
) -> EligibilityDecision:
    reason_values = list(reasons)
    retained = eligible
    if profile is EligibilityProfile.RESEARCH_HISTORICAL_IMPORT:
        retained = True
        if EligibilityReason.RESEARCH_ONLY not in reason_values:
            reason_values.append(EligibilityReason.RESEARCH_ONLY)
    return EligibilityDecision(
        eligible=eligible,
        retained_by_profile=retained,
        profile_id=profile,
        rule_id=rule,
        rule_status=InternalRuleStatus.INTERNAL_UNVERIFIED,
        source_record_id=source_record_id(record),
        bridge_key=record.bridge_key,
        record_type=record.record_type,
        normalized_item_42a=normalized_42a,
        normalized_item_112=normalized_112,
        source_references=tuple(references),
        reason_codes=tuple(reason_values),
        warnings=tuple(warnings),
        review_actions=tuple(actions),
    )


def _source_reference(
    record: SourceRecord, field_name: str, exact_value: Optional[str]
) -> SourceValueReference:
    return SourceValueReference(
        source_record_id=source_record_id(record),
        field_name=field_name,
        exact_source_value=exact_value,
        line_number=record.line_number,
        byte_offset=record.byte_offset,
        raw_sha256=record.raw_sha256,
    )


def _normalize_code(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip().upper()
    return normalized or None


def _decision_source_order(
    decision: EligibilityDecision, group: LegacyBridgeRecords
) -> int:
    for record in (
        ((group.over,) if group.over is not None else ())
        + group.duplicate_overs
        + group.unders
    ):
        if source_record_id(record) == decision.source_record_id:
            return record.source_order
    raise ValueError(f"decision source record is absent from group: {decision.source_record_id}")


class BID01Policy(str, Enum):
    PRESERVE = "preserve"
    TRIM_ALL_LEADING_ZEROS = "trim_all_leading_zeros"
    TRIM_ALL_BUT_ONE = "trim_all_but_one"


@dataclass(frozen=True)
class BID01Candidate:
    bridge_key: LegacyBridgeKey
    source_record_id: str
    raw_identifier: str


@dataclass(frozen=True)
class BID01PreviewEntry:
    bridge_key: LegacyBridgeKey
    source_record_id: str
    raw_identifier: str
    source_identifier: str
    target_identifier: str
    changed: bool


@dataclass(frozen=True)
class BID01Collision:
    source_agency_context: str
    normalized_state: str
    target_identifier: str
    entries: Tuple[BID01PreviewEntry, ...]


@dataclass(frozen=True)
class BID01PolicyPreview:
    policy: BID01Policy
    entries: Tuple[BID01PreviewEntry, ...]
    changed_entries: Tuple[BID01PreviewEntry, ...]
    collisions: Tuple[BID01Collision, ...]
    invalid_entries: Tuple[BID01PreviewEntry, ...]

    @property
    def can_apply(self) -> bool:
        return not self.collisions and not self.invalid_entries


class BID01PolicyApplicationError(ValueError):
    def __init__(self, preview: BID01PolicyPreview) -> None:
        super().__init__(
            "BID01 policy cannot be applied because the preview contains "
            "collisions or invalid target identifiers"
        )
        self.preview = preview


def bid01_candidates_from_groups(
    groups: Iterable[LegacyBridgeRecords],
) -> Tuple[BID01Candidate, ...]:
    """Build one immutable BID01 candidate per bridge without applying policy."""

    candidates: List[BID01Candidate] = []
    seen = set()
    for group in groups:
        if group.key in seen:
            raise ValueError(f"duplicate bridge group supplied: {group.key}")
        seen.add(group.key)
        records = (
            ((group.over,) if group.over is not None else ())
            + group.duplicate_overs
            + group.unders
        )
        if not records:
            raise ValueError(f"bridge group contains no source records: {group.key}")
        # BID01 belongs to the selected Type 1 record. File order is evidence,
        # not identity: an under record may appear before its over record.
        source = group.over or min(records, key=lambda record: record.source_order)
        candidates.append(
            BID01Candidate(
                bridge_key=group.key,
                source_record_id=source_record_id(source),
                raw_identifier=group.key.raw_structure_number,
            )
        )
    return tuple(sorted(candidates, key=_candidate_sort_key))


def preview_bid01_policy(
    candidates: Iterable[BID01Candidate],
    *,
    policy: BID01Policy,
) -> BID01PolicyPreview:
    """Preview every change and detect collisions inside agency/state scope."""

    selected_policy = BID01Policy(policy)
    entries: List[BID01PreviewEntry] = []
    seen_source_records = set()
    for candidate in candidates:
        if candidate.source_record_id in seen_source_records:
            raise ValueError(
                f"duplicate BID01 source_record_id: {candidate.source_record_id}"
            )
        seen_source_records.add(candidate.source_record_id)
        source_identifier = candidate.raw_identifier.strip()
        target = _apply_identifier_policy(source_identifier, selected_policy)
        entries.append(
            BID01PreviewEntry(
                bridge_key=candidate.bridge_key,
                source_record_id=candidate.source_record_id,
                raw_identifier=candidate.raw_identifier,
                source_identifier=source_identifier,
                target_identifier=target,
                changed=source_identifier != target,
            )
        )
    entries.sort(key=_preview_entry_sort_key)

    invalid = tuple(
        entry
        for entry in entries
        if not entry.target_identifier or len(entry.target_identifier) > 15
    )
    by_target: Dict[Tuple[str, str, str], List[BID01PreviewEntry]] = {}
    for entry in entries:
        if not entry.target_identifier:
            continue
        scope = (
            entry.bridge_key.source_agency_context,
            entry.bridge_key.normalized_state,
            entry.target_identifier,
        )
        by_target.setdefault(scope, []).append(entry)

    collisions: List[BID01Collision] = []
    for (agency, state, target), scoped_entries in sorted(by_target.items()):
        if len(scoped_entries) > 1:
            collisions.append(
                BID01Collision(
                    source_agency_context=agency,
                    normalized_state=state,
                    target_identifier=target,
                    entries=tuple(scoped_entries),
                )
            )

    all_entries = tuple(entries)
    return BID01PolicyPreview(
        policy=selected_policy,
        entries=all_entries,
        changed_entries=tuple(entry for entry in all_entries if entry.changed),
        collisions=tuple(collisions),
        invalid_entries=invalid,
    )


def apply_bid01_policy(
    candidates: Iterable[BID01Candidate],
    *,
    policy: BID01Policy,
) -> Mapping[str, str]:
    """Apply only after a collision-free preview; raw candidates are untouched."""

    preview = preview_bid01_policy(candidates, policy=policy)
    if not preview.can_apply:
        raise BID01PolicyApplicationError(preview)
    return MappingProxyType(
        {entry.source_record_id: entry.target_identifier for entry in preview.entries}
    )


def _apply_identifier_policy(identifier: str, policy: BID01Policy) -> str:
    if policy is BID01Policy.PRESERVE:
        return identifier
    if policy is BID01Policy.TRIM_ALL_LEADING_ZEROS:
        return identifier.lstrip("0")
    if not identifier.startswith("0"):
        return identifier
    return "0" + identifier.lstrip("0")


def _candidate_sort_key(candidate: BID01Candidate) -> Tuple[str, str, str, str]:
    return (
        candidate.bridge_key.source_agency_context,
        candidate.bridge_key.normalized_state,
        candidate.raw_identifier,
        candidate.source_record_id,
    )


def _preview_entry_sort_key(
    entry: BID01PreviewEntry,
) -> Tuple[str, str, str, str]:
    return (
        entry.bridge_key.source_agency_context,
        entry.bridge_key.normalized_state,
        entry.raw_identifier,
        entry.source_record_id,
    )
