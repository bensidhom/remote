"""Provenance-aware, research-only NBI to SNBI transition engine.

This module is the PR2 replacement boundary for the permissive legacy mapper.
It intentionally remains a *research* projection until the governing FHWA
artifacts in the local registry are vendored, hashed, and compiled into an
executable submission profile.  Every emitted scalar has a ``TransitionValue``
at its canonical JSON Pointer; unsupported legacy gaps become issues/lineage,
never invented values.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence
from types import MappingProxyType

from .nbi_parser import LegacyBridgeRecords, SourceRecord
from .transition_values import (
    METERS_TO_FEET,
    ParsedScalar,
    ParsedStatus,
    RoundingPolicy,
    SourceField,
    TransitionCategory,
    TransitionStatus,
    TransitionValue,
    UnitConversion,
    convert_h01_bh12_m_to_ft,
    convert_unit,
    decimal_to_json,
    direct_decimal,
    dms_to_decimal,
    parse_decimal,
    parse_dms,
    parse_integer,
)
from .transition_source_refs import bind_transition_source_references

try:
    from ...routers.services.snbi_crosswalk import (
        approach_alignment_from_legacy_eval,
        bridge_railing_from_legacy,
        deck_material_from_legacy,
        deck_protective_from_legacy,
        deck_reinforcing_from_legacy,
        design_load_from_legacy,
        federal_land_from_legacy,
        functional_class_from_legacy,
        historic_from_legacy,
        load_rating_method_from_legacy,
        national_highway_freight_from_legacy,
        navigation_flag_from_legacy,
        navigation_protection_from_legacy,
        nhs_from_legacy,
        overtopping_from_legacy,
        owner_maintenance_from_legacy,
        posting_status_from_legacy,
        route_direction_from_legacy,
        route_service_from_nbi_service_level,
        route_suffix_from_legacy,
        route_type_from_nbi_prefix,
        scour_condition_from_legacy,
        scour_poa_from_legacy,
        scour_vulnerability_from_legacy,
        span_continuity_from_legacy,
        span_material_from_legacy,
        span_type_from_legacy,
        strahnet_from_legacy,
        toll_from_legacy,
        urban_code_from_legacy,
        wearing_surface_from_legacy,
    )
except ImportError:  # pragma: no cover - direct backend/app script compatibility
    from routers.services.snbi_crosswalk import (  # type: ignore
        approach_alignment_from_legacy_eval,
        bridge_railing_from_legacy,
        deck_material_from_legacy,
        deck_protective_from_legacy,
        deck_reinforcing_from_legacy,
        design_load_from_legacy,
        federal_land_from_legacy,
        functional_class_from_legacy,
        historic_from_legacy,
        load_rating_method_from_legacy,
        national_highway_freight_from_legacy,
        navigation_flag_from_legacy,
        navigation_protection_from_legacy,
        nhs_from_legacy,
        overtopping_from_legacy,
        owner_maintenance_from_legacy,
        posting_status_from_legacy,
        route_direction_from_legacy,
        route_service_from_nbi_service_level,
        route_suffix_from_legacy,
        route_type_from_nbi_prefix,
        scour_condition_from_legacy,
        scour_poa_from_legacy,
        scour_vulnerability_from_legacy,
        span_continuity_from_legacy,
        span_material_from_legacy,
        span_type_from_legacy,
        strahnet_from_legacy,
        toll_from_legacy,
        urban_code_from_legacy,
        wearing_surface_from_legacy,
    )


RESEARCH_PROFILE = "research_historical_import"
ENGINE_VERSION = "research-transition-lineage-v1"

THOUSANDTH_KILOMETERS_TO_MILES = UnitConversion(
    source_unit="legacy Item 11 thousandth-km",
    target_unit="mi",
    constant=Decimal("0.001") / Decimal("1.609344"),
    definition="Item 11 thousandths of a kilometer; 1 mi = exactly 1.609344 km",
)
KILOMETERS_TO_MILES = UnitConversion(
    source_unit="km",
    target_unit="mi",
    constant=Decimal("1") / Decimal("1.609344"),
    definition="1 mi = exactly 1.609344 km",
)


class TransitionIssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class TransitionIssue:
    code: str
    severity: TransitionIssueSeverity
    message: str
    target_path: Optional[str] = None
    source_record_ids: tuple[str, ...] = ()
    source_fields: tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "target_path": self.target_path,
            "source_record_ids": list(self.source_record_ids),
            "source_fields": list(self.source_fields),
        }


@dataclass(frozen=True)
class TransitionCoverage:
    document_leaf_count: int
    lineage_leaf_count: int
    internal_derived_leaf_count: int
    uncovered_leaf_count: int
    status_counts: Mapping[str, int]
    category_counts: Mapping[str, int]
    source_record_count: int
    populated_permanent_count: int
    populated_temporary_count: int
    null_source_count: int
    invalid_source_count: int
    no_transition_count: int
    assumed_generated_count: int
    mapping_exception_count: int
    reviewed_verified_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "status_counts", MappingProxyType(dict(self.status_counts)))
        object.__setattr__(self, "category_counts", MappingProxyType(dict(self.category_counts)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "document_leaf_count": self.document_leaf_count,
            "lineage_leaf_count": self.lineage_leaf_count,
            "internal_derived_leaf_count": self.internal_derived_leaf_count,
            "uncovered_leaf_count": self.uncovered_leaf_count,
            "status_counts": dict(self.status_counts),
            "category_counts": dict(self.category_counts),
            "source_record_count": self.source_record_count,
            "populated_permanent_count": self.populated_permanent_count,
            "populated_temporary_count": self.populated_temporary_count,
            "null_source_count": self.null_source_count,
            "invalid_source_count": self.invalid_source_count,
            "no_transition_count": self.no_transition_count,
            "assumed_generated_count": self.assumed_generated_count,
            "mapping_exception_count": self.mapping_exception_count,
            "reviewed_verified_count": self.reviewed_verified_count,
        }


@dataclass(frozen=True)
class TransitionResult:
    document: Mapping[str, Any]
    lineage: Mapping[str, TransitionValue]
    review_lineage: Mapping[str, TransitionValue]
    issues: tuple[TransitionIssue, ...]
    coverage: TransitionCoverage
    profile_id: str
    engine_version: str
    submission_ready: bool = False


class _Builder:
    def __init__(self, group: LegacyBridgeRecords, target_bid01: str) -> None:
        if group.over is None:
            raise ValueError("a selected Type 1 over record is required for mapping")
        if not str(target_bid01).strip():
            raise ValueError("target_bid01 must be selected by an explicit policy")
        self.group = group
        self.over = group.over
        self.target_bid01 = str(target_bid01).strip()
        self.document: Dict[str, Any] = {}
        self.lineage: Dict[str, TransitionValue] = {}
        self.review_lineage: Dict[str, TransitionValue] = {}
        self.issues: list[TransitionIssue] = []

    def build(self) -> TransitionResult:
        self._identity_location()
        self._classification_condition()
        self._geometry()
        self._spans()
        self._load_posting_scour()
        self._works()
        self._features()
        coverage = self._coverage()
        return TransitionResult(
            document=_freeze_tree(self.document),
            lineage=MappingProxyType(dict(self.lineage)),
            review_lineage=MappingProxyType(dict(self.review_lineage)),
            issues=tuple(self.issues),
            coverage=coverage,
            profile_id=RESEARCH_PROFILE,
            engine_version=ENGINE_VERSION,
            submission_ready=False,
        )

    def _identity_location(self) -> None:
        self._emit_value(
            "/BL01", int(self.group.key.normalized_state), "BL01",
            "nbi-item1-state-fips-v1", TransitionCategory.DIRECT,
            self.over, ("STATE_CODE_001",),
        )
        self._emit_value(
            "/BID01", self.target_bid01, "BID01",
            "agency-selected-bid01-policy-v1", TransitionCategory.APP_PROJECTION,
            self.over, ("STRUCTURE_NUMBER_008",),
        )
        self._emit_integer("/BL02", "BL02", self.over, "COUNTY_CODE_003", 0, 999,
                           "nbi-item3-direct-v1")
        self._emit_integer("/BL03", "BL03", self.over, "PLACE_CODE_004", 0, 99999,
                           "nbi-item4-direct-v1")
        self._emit_text("/BL04", "BL04", self.over, "HIGHWAY_DISTRICT_002",
                        "nbi-item2-leading-zero-trim-v1", transform=lambda text: text.lstrip("0"))
        self._emit_text("/BL11", "BL11", self.over, "LOCATION_009", "nbi-item9-direct-v1")
        self._emit_integer("/BW01", "BW01", self.over, "YEAR_BUILT_027", 1, 9999,
                           "nbi-item27-direct-v1")

        self._emit_code("/BCL01", "BCL01", self.over, "OWNER_022", owner_maintenance_from_legacy,
                        "nbi-item22-code-map-v1")
        self._emit_code("/BCL02", "BCL02", self.over, "MAINTENANCE_021", owner_maintenance_from_legacy,
                        "nbi-item21-code-map-v1")
        self._emit_code("/BCL03", "BCL03", self.over, "FEDERAL_LANDS_105", federal_land_from_legacy,
                        "nbi-item105-code-map-v1")
        self._emit_code("/BCL04", "BCL04", self.over, "HISTORY_037", historic_from_legacy,
                        "nbi-item37-code-map-v1")
        self._emit_code("/BCL05", "BCL05", self.over, "TOLL_020", toll_from_legacy,
                        "nbi-item20-code-map-v1")
        self._emit_code("/BRH01", "BRH01", self.over, "RAILINGS_036A", bridge_railing_from_legacy,
                        "nbi-item36a-code-map-v1")
        self._emit_code("/BRH02", "BRH02", self.over, "TRANSITIONS_036B", bridge_railing_from_legacy,
                        "nbi-item36b-code-map-v1")

        self._emit_coordinate("/BL05", "BL05", self.over, "LAT_016", latitude=True)
        self._emit_coordinate("/BL06", "BL06", self.over, "LONG_017", latitude=False)
        self._border()

        for tag, source in (("BC01", "DECK_COND_058"), ("BC02", "SUPERSTRUCTURE_COND_059"),
                            ("BC03", "SUBSTRUCTURE_COND_060"), ("BC04", "CULVERT_COND_062")):
            self._emit_text(f"/{tag}", tag, self.over, source, f"nbi-{source.lower()}-direct-v1")
        for target, source in (("BC05", "RAILINGS_036A"), ("BC06", "TRANSITIONS_036B")):
            raw = self._raw(self.over, source)
            cleaned = self._clean(raw).upper()
            if cleaned == "N":
                self._emit_value(f"/{target}", "N", target, f"nbi-{source.lower()}-n-only-v1",
                                 TransitionCategory.CODE_MAP, self.over, (source,))
            else:
                self._review_null(f"/{target}", target, self.over, (source,),
                                  f"nbi-{source.lower()}-numeric-no-transition-v1",
                                  TransitionCategory.NO_TRANSITION,
                                  review_required=bool(cleaned))
        self._emit_integer("/BG11", "BG11", self.over, "DEGREES_SKEW_034", 0, 99,
                           "nbi-item34-direct-v1")

    def _border(self) -> None:
        item99 = self._clean(self._raw(self.over, "OTHR_STATE_STRUC_NO_099"))
        raw98a = self._clean(self._raw(self.over, "OTHER_STATE_CODE_098A")).upper()
        raw98b = self._clean(self._raw(self.over, "OTHER_STATE_PCNT_098B"))
        has_border_evidence = bool(raw98a or raw98b)
        if item99:
            self._emit_value("/BL07", item99.lstrip("0") or "0", "BL07",
                             "nbi-item99-leading-zero-trim-v1", TransitionCategory.DIRECT,
                             self.over, ("OTHR_STATE_STRUC_NO_099",))
        elif has_border_evidence:
            self._review_null("/BL07", "BL07", self.over,
                              ("OTHR_STATE_STRUC_NO_099", "OTHER_STATE_CODE_098A", "OTHER_STATE_PCNT_098B"),
                              "nbi-border-evidence-conflict-v1", TransitionCategory.DERIVED,
                              message="Items 98A/98B indicate border evidence while Item 99 is blank.")
            self.issues.append(TransitionIssue(
                code="border_evidence_conflict",
                severity=TransitionIssueSeverity.WARNING,
                message="Items 98A/98B indicate a border but Item 99 is blank; BL07 was not asserted.",
                target_path="/BL07",
                source_record_ids=(self.over.source_record_id,),
                source_fields=("OTHR_STATE_STRUC_NO_099", "OTHER_STATE_CODE_098A", "OTHER_STATE_PCNT_098B"),
            ))
        else:
            self._review_null("/BL07", "BL07", self.over, ("OTHR_STATE_STRUC_NO_099",),
                              "nbi-item99-blank-no-assertion-v1", TransitionCategory.DIRECT)
        if raw98a:
            value = "CA" if raw98a == "CAN" else "MX" if raw98a == "MEX" else raw98a
            if value.isdigit():
                value = (value[:2] if len(value) >= 3 else value).lstrip("0")
            if value:
                self._emit_value("/BL08", value, "BL08", "nbi-item98a-border-state-v1",
                                 TransitionCategory.CODE_MAP, self.over, ("OTHER_STATE_CODE_098A",))
                self._emit_value("/BL10", int(self.group.key.normalized_state), "BL10",
                                 "nbi-item98a-lead-state-projection-v1", TransitionCategory.DERIVED,
                                 self.over, ("OTHER_STATE_CODE_098A", "STATE_CODE_001"))
        if raw98b.isdigit():
            pct = raw98b.zfill(2)
            value = "0" if pct == "99" else "2" if pct == "00" else "1"
            self._emit_value("/BL09", value, "BL09", "nbi-item98b-responsibility-v1",
                             TransitionCategory.CODE_MAP, self.over, ("OTHER_STATE_PCNT_098B",))

    def _classification_condition(self) -> None:
        def requirement(raw: Any) -> Optional[str]:
            text = self._clean(raw).upper()
            if not text:
                return None
            head = text[:1]
            return head if head in {"Y", "N"} else None

        self._emit_code("/BIR01", "BIR01", self.over, "FRACTURE_092A", requirement,
                        "nbi-item92a-requirement-direct-v1")
        self._emit_code("/BIR03", "BIR03", self.over, "UNDWATER_LOOK_SEE_092B", requirement,
                        "nbi-item92b-requirement-direct-v1")

        item90 = self._clean(self._raw(self.over, "DATE_OF_INSPECT_090"))
        if item90:
            self._review_null(
                "/Inspections",
                "Inspections",
                self.over,
                ("DATE_OF_INSPECT_090",),
                "nbi-item90-annual-history-required-v1",
                TransitionCategory.NO_TRANSITION,
                message="Item 90 requires annual history before initial and routine inspection events can be distinguished.",
                review_required=True,
            )
            self.issues.append(
                TransitionIssue(
                    code="inspection_annual_history_required",
                    severity=TransitionIssueSeverity.WARNING,
                    message="Inspection events were not projected from one annual record; run the history reducer.",
                    target_path="/Inspections",
                    source_record_ids=(self.over.source_record_id,),
                    source_fields=("DATE_OF_INSPECT_090",),
                )
            )

        channel = self._clean(self._raw(self.over, "CHANNEL_COND_061")).upper()
        if channel in set("0123456789N"):
            self._emit_value("/BC09", channel, "BC09", "nbi-item61-direct-v1",
                             TransitionCategory.DIRECT, self.over, ("CHANNEL_COND_061",))
        else:
            self._review_from_raw("/BC09", "BC09", self.over, "CHANNEL_COND_061", "nbi-item61-direct-v1")
        self._emit_code("/BC11", "BC11", self.over, "SCOUR_CRITICAL_113", scour_condition_from_legacy,
                        "nbi-item113-bc11-code-map-v1")
        self._emit_code("/BAP01", "BAP01", self.over, "APPR_ROAD_EVAL_072",
                        approach_alignment_from_legacy_eval, "nbi-item72-code-map-v1")
        raw71 = self._raw(self.over, "WATERWAY_EVAL_071")
        raw26 = self._raw(self.over, "FUNCTIONAL_CLASS_026")
        value = overtopping_from_legacy(raw71, raw26)
        self._emit_mapped_combination("/BAP02", "BAP02", value, self.over,
                                      ("WATERWAY_EVAL_071", "FUNCTIONAL_CLASS_026"),
                                      "nbi-items71-26-code-map-v1")
        self._emit_code("/BAP03", "BAP03", self.over, "SCOUR_CRITICAL_113",
                        scour_vulnerability_from_legacy, "nbi-item113-bap03-code-map-v1")
        self._emit_code("/BAP04", "BAP04", self.over, "SCOUR_CRITICAL_113",
                        scour_poa_from_legacy, "nbi-item113-bap04-code-map-v1")

    def _geometry(self) -> None:
        for path, target, source, rule in (
            ("/BG02", "BG02", "STRUCTURE_LEN_MT_049", "nbi-item49-feet-v1"),
            ("/BG03", "BG03", "MAX_SPAN_LEN_MT_048", "nbi-item48-feet-v1"),
            ("/BG05", "BG05", "DECK_WIDTH_MT_052", "nbi-item52-feet-positive-v1"),
            ("/BG06", "BG06", "ROADWAY_WIDTH_MT_051", "nbi-item51-feet-v1"),
            ("/BG07", "BG07", "LEFT_CURB_MT_050A", "nbi-item50a-feet-v1"),
            ("/BG08", "BG08", "RIGHT_CURB_MT_050B", "nbi-item50b-feet-v1"),
            ("/BG09", "BG09", "APPR_WIDTH_MT_032", "nbi-item32-feet-v1"),
        ):
            minimum = Decimal("0.0000001") if target == "BG05" else Decimal("0")
            self._emit_unit(path, target, self.over, source, rule, minimum=minimum)
        length = parse_decimal(self._raw(self.over, "STRUCTURE_LEN_MT_049"), minimum="0")
        if length.status is ParsedStatus.VALID and isinstance(length.value, Decimal) and length.value > Decimal("9.1"):
            self._emit_unit("/BG01", "BG01", self.over, "STRUCTURE_LEN_MT_049",
                            "nbi-item49-bg01-greater-9.1m-v1", minimum=Decimal("0"))
        else:
            self._review_transition("/BG01", TransitionValue(
                value=None, target_tag="BG01", rule_id="nbi-item49-bg01-greater-9.1m-v1",
                category=TransitionCategory.DERIVED, source_record_ids=(self.over.source_record_id,),
                source_fields=(SourceField.from_parsed("STRUCTURE_LEN_MT_049", length),),
                status=(TransitionStatus.NULL_SOURCE if length.status is ParsedStatus.BLANK else TransitionStatus.REVIEW_REQUIRED),
                messages=("BG01 is emitted only when Item 49 is greater than 9.1 m in the base research rule.",),
            ))
        self._emit_text("/BG10", "BG10", self.over, "MEDIAN_CODE_033", "nbi-item33-direct-v1")

    def _spans(self) -> None:
        main_count = parse_integer(self._raw(self.over, "MAIN_UNIT_SPANS_045"), minimum=0)
        approach_count = parse_integer(self._raw(self.over, "APPR_SPANS_046"), minimum=0)
        structure_type = self._clean(self._raw(self.over, "STRUCTURE_TYPE_043B")).zfill(2)
        total = sum(value.value for value in (main_count, approach_count)
                    if value.status is ParsedStatus.VALID and isinstance(value.value, int))
        rows: list[Dict[str, Any]] = []
        if main_count.status is ParsedStatus.VALID and isinstance(main_count.value, int) and main_count.value > 0 and structure_type != "00":
            rows.append({})
            idx = len(rows) - 1
            self._emit_nested(rows[idx], f"/SpanSets/{idx}/BSP01", "BSP01",
                              "C01" if structure_type == "19" else "M01", self.over,
                              ("STRUCTURE_TYPE_043B",), "nbi-item43b-main-config-v1", TransitionCategory.STRUCTURAL)
            self._emit_nested(rows[idx], f"/SpanSets/{idx}/BSP02", "BSP02", main_count.value, self.over,
                              ("MAIN_UNIT_SPANS_045",), "nbi-item45-direct-v1", TransitionCategory.DIRECT)
            self._span_codes(rows[idx], idx, main=True, total=total)
        if approach_count.status is ParsedStatus.VALID and isinstance(approach_count.value, int) and approach_count.value > 0:
            rows.append({})
            idx = len(rows) - 1
            self._emit_nested(rows[idx], f"/SpanSets/{idx}/BSP01", "BSP01",
                              "C02" if structure_type == "19" else "A01", self.over,
                              ("APPR_TYPE_044B", "STRUCTURE_TYPE_043B"), "nbi-items44b-43b-approach-config-v1", TransitionCategory.STRUCTURAL)
            self._emit_nested(rows[idx], f"/SpanSets/{idx}/BSP02", "BSP02", approach_count.value, self.over,
                              ("APPR_SPANS_046",), "nbi-item46-direct-v1", TransitionCategory.DIRECT)
            self._span_codes(rows[idx], idx, main=False, total=total)
        if rows:
            self.document["SpanSets"] = rows
        else:
            self._review_null("/SpanSets", "SpanSets", self.over,
                              ("MAIN_UNIT_SPANS_045", "APPR_SPANS_046", "STRUCTURE_TYPE_043B"),
                              "nbi-span-set-insufficient-source-v1", TransitionCategory.NO_TRANSITION,
                              message="No span row was fabricated; source requires review.")
        self._review_null("/SubstructureSets", "SubstructureSets", self.over, (),
                          "nbi-substructure-no-transition-v1", TransitionCategory.NO_TRANSITION,
                          message="Legacy NBI has no supported substructure-set transition.")

    def _span_codes(self, row: Dict[str, Any], idx: int, *, main: bool, total: int) -> None:
        prefix = "/SpanSets/" + str(idx)
        kind_field = "STRUCTURE_KIND_043A" if main else "APPR_KIND_044A"
        type_field = "STRUCTURE_TYPE_043B" if main else "APPR_TYPE_044B"
        raw_kind, raw_type = self._raw(self.over, kind_field), self._raw(self.over, type_field)
        pairs = (
            ("BSP04", span_material_from_legacy(raw_kind), (kind_field,), "span-material"),
            ("BSP05", span_continuity_from_legacy(raw_kind, raw_type, total), (kind_field, type_field), "span-continuity"),
            ("BSP06", span_type_from_legacy(raw_type), (type_field,), "span-type"),
            ("BSP09", deck_material_from_legacy(self._raw(self.over, "DECK_STRUCTURE_TYPE_107"), raw_type),
             ("DECK_STRUCTURE_TYPE_107", type_field), "deck-material"),
            ("BSP10", wearing_surface_from_legacy(self._raw(self.over, "SURFACE_TYPE_108A"), self._raw(self.over, "DECK_STRUCTURE_TYPE_107")),
             ("SURFACE_TYPE_108A", "DECK_STRUCTURE_TYPE_107"), "wearing-surface"),
            ("BSP11", deck_protective_from_legacy(self._raw(self.over, "MEMBRANE_TYPE_108B"), self._raw(self.over, "DECK_PROTECTION_108C"), self._raw(self.over, "DECK_STRUCTURE_TYPE_107")),
             ("MEMBRANE_TYPE_108B", "DECK_PROTECTION_108C", "DECK_STRUCTURE_TYPE_107"), "deck-protection"),
            ("BSP12", deck_reinforcing_from_legacy(self._raw(self.over, "DECK_PROTECTION_108C"), self._raw(self.over, "DECK_STRUCTURE_TYPE_107")),
             ("DECK_PROTECTION_108C", "DECK_STRUCTURE_TYPE_107"), "deck-reinforcing"),
        )
        for target, value, sources, slug in pairs:
            if value is not None:
                self._emit_nested(row, f"{prefix}/{target}", target, value, self.over, sources,
                                  f"nbi-{slug}-code-map-v1", TransitionCategory.CODE_MAP,
                                  temporary=str(value).endswith("-T"))
            else:
                self._review_null(f"{prefix}/{target}", target, self.over, sources,
                                  f"nbi-{slug}-code-map-v1", TransitionCategory.CODE_MAP)

    def _load_posting_scour(self) -> None:
        self._emit_code("/BLR01", "BLR01", self.over, "DESIGN_LOAD_031", design_load_from_legacy,
                        "nbi-item31-code-map-v1")
        self._emit_code("/BLR04", "BLR04", self.over, "OPR_RATING_METH_063", load_rating_method_from_legacy,
                        "nbi-item63-code-map-v1")
        self._emit_rating_factor("/BLR05", "BLR05", "INVENTORY_RATING_066", "INV_RATING_METH_065")
        self._emit_rating_factor("/BLR06", "BLR06", "OPERATING_RATING_064", "OPR_RATING_METH_063")
        posting = posting_status_from_legacy(self._raw(self.over, "OPEN_CLOSED_POSTED_041"))
        if posting:
            rows = [{}]
            self._emit_nested(rows[0], "/PostingStatuses/0/BPS01", "BPS01", posting, self.over,
                              ("OPEN_CLOSED_POSTED_041",), "nbi-item41-code-map-v1", TransitionCategory.CODE_MAP,
                              temporary=str(posting).endswith("-T"))
            self.document["PostingStatuses"] = rows
        else:
            self._review_null("/PostingStatuses", "PostingStatuses", self.over,
                              ("OPEN_CLOSED_POSTED_041",), "nbi-item41-code-map-v1", TransitionCategory.CODE_MAP)

    def _works(self) -> None:
        year = parse_integer(self._raw(self.over, "YEAR_RECONSTRUCTED_106"), minimum=1, maximum=9999)
        if year.status is ParsedStatus.VALID and isinstance(year.value, int):
            rows = [{}]
            self._emit_nested(rows[0], "/Works/0/BW02", "BW02", year.value, self.over,
                              ("YEAR_RECONSTRUCTED_106",), "nbi-item106-work-year-v1", TransitionCategory.DIRECT)
            self._emit_nested(rows[0], "/Works/0/BW03", "BW03", "RECONSTRUCTED", self.over,
                              ("YEAR_RECONSTRUCTED_106",), "nbi-item106-work-type-placeholder-v1",
                              TransitionCategory.TEMPORARY, temporary=True)
            self.document["Works"] = rows
        else:
            self._review_transition("/Works", self._null_numeric("Works", "nbi-item106-work-year-v1",
                                                                 self.over, "YEAR_RECONSTRUCTED_106", year))

    def _features(self) -> None:
        service_on = self._clean(self._raw(self.over, "SERVICE_ON_042A"))
        service_under = self._clean(self._raw(self.over, "SERVICE_UND_042B"))
        valid_service_on = {"1", "4", "5", "6", "7", "8"}
        valid_service_under = {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9"}
        if service_on not in valid_service_on or service_under not in valid_service_under:
            raw_fields = ("SERVICE_ON_042A", "SERVICE_UND_042B")
            any_nonblank = bool(service_on or service_under)
            issue_code = (
                "feature_service_code_invalid"
                if any_nonblank
                else "feature_service_code_missing"
            )
            self._review_null(
                "/Features",
                "Features",
                self.over,
                raw_fields,
                "nbi-items42-feature-service-review-v1",
                TransitionCategory.STRUCTURAL,
                message="Items 42A/42B do not provide a complete supported feature classification.",
                invalid=any_nonblank,
            )
            self.issues.append(
                TransitionIssue(
                    code=issue_code,
                    severity=TransitionIssueSeverity.WARNING,
                    message="Items 42A/42B require review before the feature set is complete.",
                    target_path="/Features",
                    source_record_ids=(self.over.source_record_id,),
                    source_fields=raw_fields,
                )
            )
        rows: list[Dict[str, Any]] = []
        if service_on in {"1", "4", "5", "6", "7", "8"}:
            rows.append({})
            self._feature_identity(rows[0], 0, "H01", "C", self.over, carried=True)
        if service_on == "4":
            rows.append({})
            self._feature_identity(rows[-1], len(rows)-1, "R01", "C", self.over, carried=True, details=False)
        if service_on == "5":
            rows.append({})
            self._feature_identity(rows[-1], len(rows)-1, "P01", "C", self.over, carried=True, details=False)

        if service_under in {"1", "4", "6", "8"}:
            under_records = [
                record
                for record in self.group.unders
                if self._clean(self._raw(record, "SERVICE_UND_042B"))
                in {"1", "4", "6", "8"}
            ]
            if not under_records:
                rows.append({})
                self._feature_identity(rows[-1], len(rows)-1, "H02", "B", self.over,
                                       carried=False, details=False)
            else:
                for number, record in enumerate(under_records, start=2):
                    rows.append({})
                    self._feature_identity(rows[-1], len(rows)-1, f"H{number:02d}", "B", record,
                                           carried=False, details=True)
        if service_under in {"2", "4", "7", "8"}:
            rows.append({})
            feature_type = "R02" if service_on == "4" else "R01"
            self._feature_identity(rows[-1], len(rows)-1, feature_type, "B", self.over,
                                   carried=False, details=False)
            idx = len(rows)-1
            if self._clean(self._raw(self.over, "VERT_CLR_UND_REF_054A")).upper() == "R":
                self._emit_unit(f"/Features/{idx}/BRR02", "BRR02", self.over, "VERT_CLR_UND_054B",
                                "nbi-item54b-rail-clearance-v1", minimum=Decimal("0"), row=rows[idx])
            if self._clean(self._raw(self.over, "LAT_UND_REF_055A")).upper() == "R":
                self._emit_unit(f"/Features/{idx}/BRR03", "BRR03", self.over, "LAT_UND_MT_055B",
                                "nbi-item55b-rail-clearance-v1", minimum=Decimal("0"), row=rows[idx])
        if service_under == "3":
            rows.append({})
            self._feature_identity(rows[-1], len(rows)-1, "P02" if service_on == "5" else "P01", "B",
                                   self.over, carried=False, details=False)
        if service_under == "9":
            rows.append({})
            self._feature_identity(rows[-1], len(rows)-1, "F01", "B", self.over, carried=False, details=False)
        if service_under == "0":
            rows.append({})
            self._feature_identity(rows[-1], len(rows)-1, "X01", "B", self.over, carried=False, details=False)
        if service_under in {"5", "6", "7", "8"}:
            rows.append({})
            idx = len(rows)-1
            self._feature_identity(rows[idx], idx, "W01", "B", self.over, carried=False, details=False)
            nav_raw = self._raw(self.over, "NAVIGATION_038")
            nav = navigation_flag_from_legacy(nav_raw)
            if nav is not None:
                self._emit_nested(rows[idx], f"/Features/{idx}/BN01", "BN01", nav, self.over,
                                  ("NAVIGATION_038",), "nbi-item38-navigation-flag-v1", TransitionCategory.CODE_MAP)
                if nav == "Y":
                    lift = self._clean(self._raw(self.over, "STRUCTURE_TYPE_043B")).zfill(2) == "15"
                    source = "MIN_NAV_CLR_MT_116" if lift else "NAV_VERT_CLR_MT_039"
                    self._emit_unit(f"/Features/{idx}/BN02", "BN02", self.over, source,
                                    "nbi-navigation-vertical-clearance-v1", minimum=Decimal("0"), row=rows[idx])
                    if lift:
                        self._emit_unit(f"/Features/{idx}/BN03", "BN03", self.over, "NAV_VERT_CLR_MT_039",
                                        "nbi-item39-lift-maximum-clearance-v1", minimum=Decimal("0"), row=rows[idx])
                    self._emit_unit(f"/Features/{idx}/BN04", "BN04", self.over, "NAV_HORR_CLR_MT_040",
                                    "nbi-item40-navigation-horizontal-v1", minimum=Decimal("0"), row=rows[idx])
                    protection = navigation_protection_from_legacy(self._raw(self.over, "PIER_PROTECTION_111"))
                    if protection is not None:
                        self._emit_nested(rows[idx], f"/Features/{idx}/BN06", "BN06", protection, self.over,
                                          ("PIER_PROTECTION_111",), "nbi-item111-code-map-v1", TransitionCategory.CODE_MAP,
                                          temporary=str(protection).endswith("-T"))
            else:
                self._review_from_raw(f"/Features/{idx}/BN01", "BN01", self.over, "NAVIGATION_038",
                                      "nbi-item38-navigation-flag-v1")
        if rows:
            self.document["Features"] = rows

    def _feature_identity(self, row: Dict[str, Any], idx: int, feature_type: str, location: str,
                          record: SourceRecord, *, carried: bool, details: bool = True) -> None:
        prefix = f"/Features/{idx}"
        self._emit_nested(row, f"{prefix}/BF01", "BF01", feature_type, record,
                          ("SERVICE_ON_042A" if carried else "SERVICE_UND_042B",),
                          "nbi-items42-feature-type-v1", TransitionCategory.STRUCTURAL,
                          assumed=True,
                          assumption_id=f"feature:{record.source_record_id}:{feature_type}:{location}")
        self._emit_nested(row, f"{prefix}/BF02", "BF02", location, record,
                          ("SERVICE_ON_042A" if carried else "SERVICE_UND_042B",),
                          "nbi-items42-feature-location-v1", TransitionCategory.STRUCTURAL)
        name_field = "FACILITY_CARRIED_007" if carried else "FEATURES_DESC_006A"
        self._emit_text(f"{prefix}/BF03", "BF03", record if details else self.over, name_field,
                        "nbi-feature-name-direct-v1", row=row)
        if not details:
            return
        mappings = (
            ("BH01", "FUNCTIONAL_CLASS_026", functional_class_from_legacy),
            ("BH02", "FUNCTIONAL_CLASS_026", urban_code_from_legacy),
            ("BH03", "HIGHWAY_SYSTEM_104", nhs_from_legacy),
            ("BH04", "NATIONAL_NETWORK_110", national_highway_freight_from_legacy),
            ("BH05", "STRAHNET_HIGHWAY_100", strahnet_from_legacy),
        )
        for target, source, mapper in mappings:
            self._emit_code(f"{prefix}/{target}", target, record, source, mapper,
                            f"nbi-{source.lower()}-code-map-v1", row=row)
        self._emit_lrs(row, prefix, record)
        self._emit_decimal_mile(row, prefix, record)
        lanes_field = "TRAFFIC_LANES_ON_028A" if carried else "TRAFFIC_LANES_UND_028B"
        self._emit_integer(f"{prefix}/BH08", "BH08", record, lanes_field, 0, 99,
                           "nbi-item28-lanes-direct-v1", row=row)
        self._emit_integer(f"{prefix}/BH09", "BH09", record, "ADT_029", 0, None,
                           "nbi-item29-aadt-direct-v1", row=row)
        self._emit_truck_traffic(row, prefix, record)
        self._emit_integer(f"{prefix}/BH11", "BH11", record, "YEAR_ADT_030", 0, 9999,
                           "nbi-item30-direct-v1", row=row)
        if carried:
            tv = convert_h01_bh12_m_to_ft(parse_decimal(self._raw(record, "MIN_VERT_CLR_010"), minimum="0"),
                                           source_record_ids=(record.source_record_id,))
            self._emit_transition(f"{prefix}/BH12", tv, row=row)
            self._emit_unit(f"{prefix}/BH13", "BH13", record, "VERT_CLR_OVER_MT_053",
                            "nbi-item53-clearance-v1", minimum=Decimal("0"), row=row)
        else:
            parsed_bh12 = parse_decimal(
                self._raw(record, "MIN_VERT_CLR_010"), minimum="0"
            )
            self._review_transition(
                f"{prefix}/BH12",
                TransitionValue(
                    value=None,
                    target_tag="BH12",
                    rule_id="nbi-under-item10-rounding-unverified-v1",
                    category=TransitionCategory.UNIT,
                    source_record_ids=(record.source_record_id,),
                    source_fields=(
                        SourceField.from_parsed("MIN_VERT_CLR_010", parsed_bh12),
                    ),
                    rounding_policy=RoundingPolicy.EXACT,
                    status=(
                        TransitionStatus.REVIEW_REQUIRED
                        if parsed_bh12.status is ParsedStatus.VALID
                        else TransitionStatus.NULL_SOURCE
                        if parsed_bh12.status is ParsedStatus.BLANK
                        else TransitionStatus.INVALID_SOURCE
                    ),
                    messages=(
                        "H02 BH12 rounding remains blocked pending a pinned differential fixture.",
                    ),
                    unit_conversion=METERS_TO_FEET,
                ),
            )
        self._emit_unit(f"{prefix}/BH16", "BH16", record, "HORR_CLR_MT_047",
                        "nbi-item47-clearance-v1", minimum=Decimal("0"), row=row)
        self._emit_detour(row, prefix, record)
        self._emit_route(row, prefix, record)

    def _emit_route(self, feature: Dict[str, Any], prefix: str, record: SourceRecord) -> None:
        route_number = self._clean(self._raw(record, "ROUTE_NUMBER_005D"))
        if not route_number:
            self._review_null(f"{prefix}/Routes", "Routes", record,
                              ("ROUTE_NUMBER_005D",), "nbi-item5-route-no-source-v1", TransitionCategory.STRUCTURAL)
            return
        number = route_number.lstrip("0") or "0"
        suffix = route_suffix_from_legacy(self._raw(record, "DIRECTION_005E"))
        if number != "0" and suffix:
            number += suffix
        route: Dict[str, Any] = {}
        route_prefix = f"{prefix}/Routes/0"
        self._emit_nested(route, f"{route_prefix}/BRT01", "BRT01", "R01", record,
                          ("ROUTE_NUMBER_005D",), "nbi-item5-route-row-v1", TransitionCategory.STRUCTURAL,
                          assumed=True,
                          assumption_id=f"route:{record.source_record_id}:R01")
        self._emit_nested(route, f"{route_prefix}/BRT02", "BRT02", number, record,
                          ("ROUTE_NUMBER_005D", "DIRECTION_005E"), "nbi-items5d-5e-route-id-v1", TransitionCategory.DERIVED)
        for target, source, mapper in (
            ("BRT03", "TRAFFIC_DIRECTION_102", route_direction_from_legacy),
            ("BRT04", "ROUTE_PREFIX_005B", route_type_from_nbi_prefix),
            ("BRT05", "SERVICE_LEVEL_005C", route_service_from_nbi_service_level),
        ):
            value = mapper(self._raw(record, source))
            if value is not None:
                self._emit_nested(route, f"{route_prefix}/{target}", target, value, record, (source,),
                                  f"nbi-{source.lower()}-code-map-v1", TransitionCategory.CODE_MAP,
                                  temporary=str(value).endswith("-T"))
            else:
                self._review_from_raw(f"{route_prefix}/{target}", target, record, source,
                                      f"nbi-{source.lower()}-code-map-v1")
        feature["Routes"] = [route]

    def _emit_lrs(self, row: Dict[str, Any], prefix: str, record: SourceRecord) -> None:
        a = self._clean(self._raw(record, "LRS_INV_ROUTE_013A"))
        b = self._clean(self._raw(record, "SUBROUTE_NO_013B"))
        # Item 13 is a 10-character LRS route identifier followed by the
        # two-character subroute.  The strict parser has already removed field
        # padding; never manufacture right-side zeroes while reconstructing it.
        combined = a[:10] + b[:2]
        if not combined.strip("0 "):
            self._review_null(f"{prefix}/BH06", "BH06", record,
                              ("LRS_INV_ROUTE_013A", "SUBROUTE_NO_013B"),
                              "nbi-items13a-13b-lrs-v1", TransitionCategory.DERIVED)
            return
        if record.bridge_key.normalized_state != "34":
            combined = combined.lstrip("0")
        self._emit_nested(row, f"{prefix}/BH06", "BH06", combined, record,
                          ("LRS_INV_ROUTE_013A", "SUBROUTE_NO_013B"),
                          "nbi-items13a-13b-lrs-v1", TransitionCategory.DERIVED)

    def _emit_decimal_mile(self, row: Dict[str, Any], prefix: str, record: SourceRecord) -> None:
        parsed = parse_decimal(self._raw(record, "KILOPOINT_011"), minimum="0")
        transition = convert_unit(
            parsed,
            target_tag="BH07",
            rule_id="nbi-item11-km-to-mile-v1",
            source_tag="KILOPOINT_011",
            conversion=THOUSANDTH_KILOMETERS_TO_MILES,
            source_record_ids=(record.source_record_id,),
            rounding_policy=RoundingPolicy.HALF_UP_THOUSANDTH,
        )
        self._emit_transition(f"{prefix}/BH07", transition, row=row)

    def _emit_truck_traffic(self, row: Dict[str, Any], prefix: str, record: SourceRecord) -> None:
        adt = parse_integer(self._raw(record, "ADT_029"), minimum=0)
        pct = parse_decimal(self._raw(record, "PERCENT_ADT_TRUCK_109"), minimum="0", maximum="100")
        if adt.status is ParsedStatus.VALID and pct.status is ParsedStatus.VALID and isinstance(adt.value, int) and isinstance(pct.value, Decimal):
            value = (Decimal(adt.value) * pct.value / Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            self._emit_value(f"{prefix}/BH10", int(value), "BH10", "nbi-items29-109-truck-aadt-half-up-v1",
                             TransitionCategory.DERIVED, record, ("ADT_029", "PERCENT_ADT_TRUCK_109"), row=row,
                             rounding=RoundingPolicy.HALF_UP_WHOLE)
        else:
            status = TransitionStatus.NULL_SOURCE if ParsedStatus.BLANK in {adt.status, pct.status} else TransitionStatus.INVALID_SOURCE
            tv = TransitionValue(value=None, target_tag="BH10", rule_id="nbi-items29-109-truck-aadt-half-up-v1",
                                 category=TransitionCategory.DERIVED, source_record_ids=(record.source_record_id,),
                                 source_fields=(SourceField.from_parsed("ADT_029", adt), SourceField.from_parsed("PERCENT_ADT_TRUCK_109", pct)),
                                 rounding_policy=RoundingPolicy.HALF_UP_WHOLE, status=status,
                                 messages=("Truck AADT requires valid Item 29 and Item 109; no default was applied.",))
            self._review_transition(f"{prefix}/BH10", tv)

    def _emit_detour(self, row: Dict[str, Any], prefix: str, record: SourceRecord) -> None:
        parsed = parse_decimal(self._raw(record, "DETOUR_KILOS_019"), minimum="0")
        if parsed.status is ParsedStatus.VALID and isinstance(parsed.value, Decimal):
            if parsed.value == Decimal("199"):
                self._emit_value(
                    f"{prefix}/BH17", 999, "BH17", "nbi-item19-199-special-value-v1",
                    TransitionCategory.CODE_MAP, record, ("DETOUR_KILOS_019",), row=row,
                )
            else:
                transition = convert_unit(
                    parsed,
                    target_tag="BH17",
                    rule_id="nbi-item19-detour-miles-v1",
                    source_tag="DETOUR_KILOS_019",
                    conversion=KILOMETERS_TO_MILES,
                    source_record_ids=(record.source_record_id,),
                    rounding_policy=RoundingPolicy.HALF_UP_WHOLE,
                )
                self._emit_transition(f"{prefix}/BH17", transition, row=row)
        else:
            self._review_transition(f"{prefix}/BH17", self._null_numeric("BH17", "nbi-item19-detour-miles-v1",
                                                                          record, "DETOUR_KILOS_019", parsed))

    def _emit_rating_factor(self, path: str, target: str, rating_field: str, method_field: str) -> None:
        rating = parse_decimal(self._raw(self.over, rating_field), minimum="0")
        method = self._clean(self._raw(self.over, method_field)).upper()
        supported_methods = {str(value) for value in range(0, 9)} | {"A", "B", "C", "D", "E", "F"}
        if rating.status is ParsedStatus.VALID and isinstance(rating.value, Decimal) and method in supported_methods:
            divisor = Decimal("32.4") if method in {"6", "7", "8", "D", "E", "F"} else Decimal("32.67")
            value = (rating.value / divisor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            self._emit_value(path, value, target, f"nbi-{rating_field.lower()}-factor-v1",
                             TransitionCategory.DERIVED, self.over, (rating_field, method_field),
                             rounding=RoundingPolicy.HALF_UP_HUNDREDTH)
        else:
            fields = (
                SourceField.from_parsed(rating_field, rating),
                SourceField.from_parsed(method_field, self._parsed_raw(self._raw(self.over, method_field))),
            )
            self._review_transition(path, TransitionValue(
                value=None,
                target_tag=target,
                rule_id=f"nbi-{rating_field.lower()}-factor-v1",
                category=TransitionCategory.DERIVED,
                source_record_ids=(self.over.source_record_id,),
                source_fields=fields,
                rounding_policy=RoundingPolicy.HALF_UP_HUNDREDTH,
                status=(
                    TransitionStatus.NULL_SOURCE
                    if rating.status is ParsedStatus.BLANK or not method
                    else TransitionStatus.INVALID_SOURCE
                ),
                messages=("Rating factor requires both a valid rating and a supported rating method.",),
            ))

    def _emit_coordinate(self, path: str, target: str, record: SourceRecord, source: str, *, latitude: bool) -> None:
        parsed = parse_dms(self._raw(record, source), latitude=latitude)
        decimal = dms_to_decimal(parsed, latitude=latitude, rounding_policy=RoundingPolicy.HALF_UP_THOUSANDTH)
        if decimal.status is ParsedStatus.VALID and isinstance(decimal.value, Decimal):
            value = decimal.value
            state = record.bridge_key.normalized_state
            if latitude and state == "60": value = -abs(value)
            if not latitude and state not in {"64", "66", "68", "69", "70"}: value = -abs(value)
            self._emit_value(path, value, target, "nbi-items16-17-dms-v1",
                             TransitionCategory.DERIVED, record, (source,),
                             rounding=RoundingPolicy.HALF_UP_THOUSANDTH)
        else:
            status = TransitionStatus.NULL_SOURCE if decimal.status is ParsedStatus.BLANK else TransitionStatus.INVALID_SOURCE
            self._review_transition(path, TransitionValue(
                value=None, target_tag=target, rule_id="nbi-items16-17-dms-v1",
                category=TransitionCategory.DERIVED, source_record_ids=(record.source_record_id,),
                source_fields=(SourceField.from_parsed(source, parsed),),
                rounding_policy=RoundingPolicy.HALF_UP_THOUSANDTH, status=status,
                messages=("DMS input was blank, malformed, or outside geographic bounds.",),
            ))

    def _emit_unit(self, path: str, target: str, record: SourceRecord, source: str, rule: str,
                   *, minimum: Optional[Decimal] = None, row: Optional[Dict[str, Any]] = None) -> None:
        parsed = parse_decimal(self._raw(record, source), minimum=minimum)
        tv = convert_unit(parsed, target_tag=target, rule_id=rule, source_tag=source,
                          conversion=METERS_TO_FEET, source_record_ids=(record.source_record_id,),
                          rounding_policy=RoundingPolicy.HALF_UP_TENTH)
        self._emit_transition(path, tv, row=row)

    def _emit_integer(self, path: str, target: str, record: SourceRecord, source: str,
                      minimum: Optional[int], maximum: Optional[int], rule: str,
                      *, row: Optional[Dict[str, Any]] = None) -> None:
        parsed = parse_integer(self._raw(record, source), minimum=minimum, maximum=maximum)
        tv = direct_decimal(parsed, target_tag=target, rule_id=rule, source_tag=source,
                            source_record_ids=(record.source_record_id,))
        self._emit_transition(path, tv, row=row)

    def _emit_text(self, path: str, target: str, record: SourceRecord, source: str, rule: str,
                   *, transform: Optional[Callable[[str], str]] = None, row: Optional[Dict[str, Any]] = None) -> None:
        raw = self._raw(record, source)
        text = self._clean(raw)
        parsed = ParsedScalar(raw=raw, value=(text if text else None),
                              status=ParsedStatus.VALID if text else ParsedStatus.BLANK)
        if text:
            value = transform(text) if transform else text
            if value:
                self._emit_value(path, value, target, rule, TransitionCategory.DIRECT, record, (source,), row=row)
                return
        self._review_transition(path, TransitionValue(
            value=None, target_tag=target, rule_id=rule, category=TransitionCategory.DIRECT,
            source_record_ids=(record.source_record_id,), source_fields=(SourceField.from_parsed(source, parsed),),
            status=TransitionStatus.NULL_SOURCE, messages=("Source is blank; no default was applied.",),
        ))

    def _emit_code(self, path: str, target: str, record: SourceRecord, source: str,
                   mapper: Callable[[Any], Optional[str]], rule: str, *, row: Optional[Dict[str, Any]] = None) -> None:
        raw = self._raw(record, source)
        cleaned = self._clean(raw)
        value = mapper(raw) if cleaned else None
        if value is not None:
            self._emit_value(path, value, target, rule, TransitionCategory.CODE_MAP, record, (source,), row=row,
                             temporary=str(value).endswith("-T"))
        else:
            self._review_from_raw(path, target, record, source, rule,
                                  invalid=bool(cleaned), category=TransitionCategory.CODE_MAP)

    def _emit_mapped_combination(self, path: str, target: str, value: Any, record: SourceRecord,
                                 sources: Sequence[str], rule: str) -> None:
        if value is not None:
            self._emit_value(path, value, target, rule, TransitionCategory.CODE_MAP, record, sources,
                             temporary=str(value).endswith("-T"))
        else:
            cleaned = any(self._clean(self._raw(record, field)) for field in sources)
            self._review_null(path, target, record, sources, rule, TransitionCategory.CODE_MAP,
                              invalid=cleaned)

    def _emit_transition(self, path: str, tv: TransitionValue, *, row: Optional[Dict[str, Any]] = None) -> None:
        tv = bind_transition_source_references(tv)
        if tv.value is None:
            self._review_transition(path, tv)
            return
        value = self._json_number(tv.value)
        container = self.document if row is None else row
        container[tv.target_tag] = value
        self.lineage[path] = tv

    def _emit_value(self, path: str, value: Any, target: str, rule: str, category: TransitionCategory,
                    record: SourceRecord, sources: Sequence[str], *, row: Optional[Dict[str, Any]] = None,
                    temporary: bool = False, rounding: RoundingPolicy = RoundingPolicy.EXACT,
                    assumed: bool = False, assumption_id: Optional[str] = None) -> None:
        parsed_fields = tuple(SourceField.from_parsed(source, self._parsed_raw(self._raw(record, source))) for source in sources)
        tv = TransitionValue(
            value=value, target_tag=target, rule_id=rule, category=category,
            source_record_ids=(record.source_record_id,), source_fields=parsed_fields,
            rounding_policy=rounding,
            status=(
                TransitionStatus.TEMPORARY
                if temporary
                else TransitionStatus.ASSUMED
                if assumed
                else TransitionStatus.POPULATED
            ),
            messages=(
                ("Temporary transition value requires review.",)
                if temporary
                else ("Application assigned a semantic row identity.",)
                if assumed
                else ()
            ),
            assumption_id=assumption_id,
        )
        container = self.document if row is None else row
        container[target] = self._json_number(value)
        self.lineage[path] = bind_transition_source_references(tv)

    def _emit_nested(self, row: Dict[str, Any], path: str, target: str, value: Any,
                     record: SourceRecord, sources: Sequence[str], rule: str,
                     category: TransitionCategory, *, temporary: bool = False,
                     assumed: bool = False, assumption_id: Optional[str] = None) -> None:
        self._emit_value(
            path, value, target, rule, category, record, sources,
            row=row, temporary=temporary, assumed=assumed,
            assumption_id=assumption_id,
        )

    def _review_from_raw(self, path: str, target: str, record: SourceRecord, source: str, rule: str,
                         *, invalid: Optional[bool] = None,
                         category: TransitionCategory = TransitionCategory.DIRECT) -> None:
        raw = self._raw(record, source)
        parsed = self._parsed_raw(raw)
        if invalid is None: invalid = bool(self._clean(raw))
        self._review_transition(path, TransitionValue(
            value=None, target_tag=target, rule_id=rule, category=category,
            source_record_ids=(record.source_record_id,), source_fields=(SourceField.from_parsed(source, parsed),),
            status=TransitionStatus.INVALID_SOURCE if invalid else TransitionStatus.NULL_SOURCE,
            messages=(("Source value is invalid or unmapped.",) if invalid else ("Source is blank; no default was applied.",)),
        ))

    def _review_null(self, path: str, target: str, record: SourceRecord, sources: Sequence[str], rule: str,
                     category: TransitionCategory, *, message: str = "Source is blank; no value was asserted.",
                     invalid: bool = False,
                     unit_conversion: Optional[UnitConversion] = None,
                     review_required: bool = False) -> None:
        fields = tuple(SourceField.from_parsed(source, self._parsed_raw(self._raw(record, source))) for source in sources)
        self._review_transition(path, TransitionValue(
            value=None, target_tag=target, rule_id=rule, category=category,
            source_record_ids=(record.source_record_id,), source_fields=fields,
            status=(
                TransitionStatus.REVIEW_REQUIRED
                if review_required
                else TransitionStatus.INVALID_SOURCE
                if invalid
                else TransitionStatus.NULL_SOURCE
            ),
            messages=(message,),
            unit_conversion=unit_conversion,
        ))

    def _review_transition(self, path: str, tv: TransitionValue) -> None:
        self.review_lineage[path] = bind_transition_source_references(tv)

    def _null_numeric(self, target: str, rule: str, record: SourceRecord, source: str,
                      parsed: ParsedScalar) -> TransitionValue:
        return TransitionValue(
            value=None, target_tag=target, rule_id=rule, category=TransitionCategory.DIRECT,
            source_record_ids=(record.source_record_id,), source_fields=(SourceField.from_parsed(source, parsed),),
            status=TransitionStatus.NULL_SOURCE if parsed.status is ParsedStatus.BLANK else TransitionStatus.INVALID_SOURCE,
            messages=("Numeric source was blank or invalid; no default was applied.",),
        )

    def _coverage(self) -> TransitionCoverage:
        leaves = self._leaf_paths(self.document)
        missing = leaves.difference(self.lineage)
        if missing:
            raise RuntimeError(f"transition engine produced uncovered document leaves: {sorted(missing)}")
        status_counts = Counter(tv.status.value for tv in self.lineage.values())
        category_counts = Counter(tv.category.value for tv in self.lineage.values())
        permanent = sum(1 for tv in self.lineage.values() if tv.status is TransitionStatus.POPULATED)
        temporary = status_counts[TransitionStatus.TEMPORARY.value]
        return TransitionCoverage(
            document_leaf_count=len(leaves), lineage_leaf_count=len(self.lineage),
            internal_derived_leaf_count=sum(1 for tv in self.lineage.values()
                                            if tv.category in {TransitionCategory.DERIVED, TransitionCategory.STRUCTURAL, TransitionCategory.APP_PROJECTION}),
            uncovered_leaf_count=len(missing), status_counts=dict(status_counts), category_counts=dict(category_counts),
            source_record_count=1 + len(self.group.unders), populated_permanent_count=permanent,
            populated_temporary_count=temporary,
            null_source_count=sum(1 for tv in self.review_lineage.values() if tv.status is TransitionStatus.NULL_SOURCE),
            invalid_source_count=sum(1 for tv in self.review_lineage.values() if tv.status is TransitionStatus.INVALID_SOURCE),
            no_transition_count=sum(1 for tv in self.review_lineage.values() if tv.category is TransitionCategory.NO_TRANSITION),
            assumed_generated_count=sum(1 for tv in self.lineage.values() if tv.status in {TransitionStatus.ASSUMED, TransitionStatus.GENERATED}),
            mapping_exception_count=sum(1 for issue in self.issues if issue.severity is TransitionIssueSeverity.ERROR),
            reviewed_verified_count=0,
        )

    @staticmethod
    def _raw(record: SourceRecord, field: str) -> Any:
        return record.fields.get(field)

    @staticmethod
    def _clean(value: Any) -> str:
        return "" if value is None else str(value).strip().strip("'\"").strip()

    @staticmethod
    def _parsed_raw(raw: Any) -> ParsedScalar:
        text = _Builder._clean(raw)
        return ParsedScalar(raw=raw, value=text if text else None,
                            status=ParsedStatus.VALID if text else ParsedStatus.BLANK)

    @staticmethod
    def _json_number(value: Any) -> Any:
        if not isinstance(value, Decimal): return value
        rendered = decimal_to_json(value)
        return int(rendered) if "." not in rendered else float(rendered)

    @classmethod
    def _leaf_paths(cls, value: Any, pointer: str = "") -> set[str]:
        if isinstance(value, Mapping):
            output: set[str] = set()
            for key, child in value.items():
                token = str(key).replace("~", "~0").replace("/", "~1")
                output.update(cls._leaf_paths(child, f"{pointer}/{token}"))
            return output
        if isinstance(value, (list, tuple)):
            output = set()
            for index, child in enumerate(value):
                output.update(cls._leaf_paths(child, f"{pointer}/{index}"))
            return output
        return {pointer or "/"}


def map_bridge_with_lineage(
    group: LegacyBridgeRecords,
    *,
    target_bid01: str,
    profile_id: str = RESEARCH_PROFILE,
) -> TransitionResult:
    """Map one strict bridge group with complete emitted-field lineage.

    Operational/submission profiles intentionally fail closed until their
    governing registry artifacts are locally verified and their rule compiler
    is implemented.
    """

    if profile_id != RESEARCH_PROFILE:
        raise ValueError(
            f"profile {profile_id!r} has no verified executable transition engine"
        )
    return _Builder(group, target_bid01).build()


def _freeze_tree(value: Any) -> Any:
    """Snapshot a result tree so audit evidence cannot change after return."""

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_tree(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_tree(child) for child in value)
    return value
