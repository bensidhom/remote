"""Focused regression tests for the provenance-aware NBI transition engine.

These tests intentionally exercise the strict ``SourceRecord`` objects rather
than the lossy dictionary adapter.  The legacy mapper is allowed to remain as a
rollback implementation, but its synthetic defaults must not leak from the new
research transition profile.
"""

from __future__ import annotations

import hashlib
import unittest
from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from app.services.conversion.nbi_parser import (
    LegacyBridgeKey,
    LegacyBridgeRecords,
    RecordType,
    SourceRecord,
)
from app.services.conversion.transition_engine import map_bridge_with_lineage


def _record(
    *,
    fields: Mapping[str, Any] | None = None,
    record_type: RecordType = RecordType.OVER,
    structure_number: str = "000123",
    state: str = "34",
    raw_item_001: str = "342",
    agency: str = "NJDOT",
    line_number: int = 1,
    source_order: int = 1,
) -> SourceRecord:
    """Build an immutable source record with stable, realistic provenance."""

    raw_item_008 = structure_number.ljust(15)
    values: dict[str, Any] = {
        "STATE_CODE_001": raw_item_001,
        "STRUCTURE_NUMBER_008": raw_item_008,
        "RECORD_TYPE_005A": record_type.value,
        "SERVICE_ON_042A": "1",
        "SERVICE_UND_042B": "",
        "BRIDGE_LEN_IND_112": "Y",
    }
    values.update(dict(fields or {}))
    raw = (
        f"{agency}|{raw_item_001}|{raw_item_008}|{record_type.value}|"
        f"{line_number}|{sorted(values.items())}"
    ).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    key = LegacyBridgeKey(
        source_agency_context=agency,
        normalized_state=state,
        raw_structure_number=raw_item_008,
    )
    return SourceRecord(
        source_order=source_order,
        line_number=line_number,
        byte_offset=(line_number - 1) * 446,
        raw_sha256=digest,
        source_record_id=f"record-{digest[:20]}",
        record_type=record_type,
        bridge_key=key,
        raw_item_001=raw_item_001,
        raw_item_008=raw_item_008,
        fields=MappingProxyType(values),
        conformant=True,
    )


def _group(
    over: SourceRecord,
    *,
    unders: tuple[SourceRecord, ...] = (),
) -> LegacyBridgeRecords:
    type_twos = tuple(
        item for item in unders if item.record_type is RecordType.UNDER
    )
    type_as = tuple(
        item for item in unders if item.record_type is RecordType.UNDER_ALTERNATE
    )
    primary = type_twos[0] if type_twos else type_as[0] if type_as else None
    return LegacyBridgeRecords(
        key=over.bridge_key,
        over=over,
        duplicate_overs=(),
        unders=unders,
        primary_under=primary,
        secondary_unders=tuple(item for item in unders if item is not primary),
    )


def _map(
    fields: Mapping[str, Any] | None = None,
    *,
    target_bid01: str = "123",
    unders: tuple[SourceRecord, ...] = (),
):
    return map_bridge_with_lineage(
        _group(_record(fields=fields), unders=unders),
        target_bid01=target_bid01,
        profile_id="research_historical_import",
    )


def _find_feature(document: Mapping[str, Any], feature_type: str) -> Mapping[str, Any]:
    for feature in document.get("Features", ()):
        if feature.get("BF01") == feature_type:
            return feature
    raise AssertionError(f"feature {feature_type!r} was not produced: {document!r}")


def _member(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value[name]
    return getattr(value, name)


def _optional_member(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _json_pointer_leaves(value: Any, pointer: str = "") -> set[str]:
    """Return RFC 6901-style pointers for every scalar document leaf."""

    if isinstance(value, Mapping):
        leaves: set[str] = set()
        for key, child in value.items():
            token = str(key).replace("~", "~0").replace("/", "~1")
            leaves.update(_json_pointer_leaves(child, f"{pointer}/{token}"))
        return leaves
    if isinstance(value, (list, tuple)):
        leaves = set()
        for index, child in enumerate(value):
            leaves.update(_json_pointer_leaves(child, f"{pointer}/{index}"))
        return leaves
    return {pointer or "/"}


def _all_nested_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, Mapping):
        keys.update(str(key) for key in value)
        for child in value.values():
            keys.update(_all_nested_keys(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            keys.update(_all_nested_keys(child))
    return keys


class NullAndDefaultSemanticsTests(unittest.TestCase):
    def test_blank_and_invalid_numeric_sources_are_absent_not_zero(self):
        result = _map(
            {
                "COUNTY_CODE_003": " ",
                "PLACE_CODE_004": "not-a-number",
                "DEGREES_SKEW_034": "X",
                "TRAFFIC_LANES_ON_028A": "",
                "ADT_029": "bad",
                "PERCENT_ADT_TRUCK_109": "15",
                "YEAR_ADT_030": " ",
            }
        )
        document = result.document
        self.assertNotIn("BL02", document)
        self.assertNotIn("BL03", document)
        self.assertNotIn("BG11", document)
        carried = _find_feature(document, "H01")
        for tag in ("BH08", "BH09", "BH10", "BH11"):
            with self.subTest(tag=tag):
                self.assertNotIn(tag, carried)

    def test_true_zero_is_preserved_for_fields_where_zero_is_a_value(self):
        result = _map(
            {
                "DEGREES_SKEW_034": "00",
                "ADT_029": "000000",
            }
        )
        self.assertEqual(result.document["BG11"], 0)
        self.assertEqual(_find_feature(result.document, "H01")["BH09"], 0)

    def test_unsupported_defaults_and_generated_rows_are_not_emitted(self):
        result = _map(
            {
                "SERVICE_UND_042B": "5",
                "NAVIGATION_038": "",
                "MAIN_UNIT_SPANS_045": "",
                "APPR_SPANS_046": "",
                "STRUCTURE_TYPE_043B": "",
                "YEAR_BUILT_027": "1990",
                "DATE_OF_INSPECT_090": "01012024",
                "SPEC_INSPECT_092C": "Y12",
                "SPEC_LAST_DATE_093C": "01012024",
            }
        )
        document = result.document
        self.assertNotIn("BID03", document)
        self.assertNotIn("SpanSets", document)
        self.assertNotIn("SubstructureSets", document)
        self.assertNotIn("BAP05", document)
        self.assertFalse(
            any(work.get("BW03") == "BR1" for work in document.get("Works", ()))
        )
        waterway = _find_feature(document, "W01")
        self.assertNotIn("BN01", waterway)
        nested_keys = _all_nested_keys(document.get("Inspections", ()))
        self.assertTrue({"BIE03", "BIE04", "BIE07"}.isdisjoint(nested_keys))


class KnownForwardRuleRegressionTests(unittest.TestCase):
    def test_invalid_rating_method_never_selects_a_fallback_divisor(self):
        result = _map(
            {
                "INVENTORY_RATING_066": "32.67",
                "INV_RATING_METH_065": "X",
            }
        )

        self.assertNotIn("BLR05", result.document)
        review = result.review_lineage["/BLR05"]
        self.assertEqual(review.status.value, "invalid_source")
        self.assertEqual(
            {source.tag for source in review.source_fields},
            {"INVENTORY_RATING_066", "INV_RATING_METH_065"},
        )

    def test_present_source_with_no_transition_is_review_required_not_blank(self):
        result = _map({"RAILINGS_036A": "1"})
        review = result.review_lineage["/BC05"]

        self.assertEqual(review.category.value, "no_transition")
        self.assertEqual(review.status.value, "review_required")
        self.assertEqual(review.source_fields[0].parsed.value, "1")

    def test_item90_requires_explicit_annual_history_review_until_reducer_is_wired(self):
        result = _map({"DATE_OF_INSPECT_090": "0124"})

        self.assertNotIn("Inspections", result.document)
        review = result.review_lineage["/Inspections"]
        self.assertEqual(review.status.value, "review_required")
        self.assertIn(
            "DATE_OF_INSPECT_090",
            {source.tag for source in review.source_fields},
        )
        self.assertTrue(
            any(
                issue.code == "inspection_annual_history_required"
                and issue.target_path == "/Inspections"
                for issue in result.issues
            )
        )

    def test_fractional_dms_keeps_decimal_lineage_without_rejecting_the_bridge(self):
        result = _map(
            {
                # 40 degrees, 00 minutes, 30.00 seconds -> 40.008 degrees
                # after the transition rule's thousandth-degree rounding.
                "LAT_016": "40003000",
            }
        )

        self.assertEqual(result.document["BL05"], 40.008)
        self.assertEqual(result.lineage["/BL05"].value, Decimal("40.008"))

    def test_kilopoint_conversion_has_exact_unit_lineage(self):
        result = _map({"KILOPOINT_011": "001609"})
        carried = _find_feature(result.document, "H01")
        transition = result.lineage["/Features/0/BH07"]

        self.assertEqual(carried["BH07"], 1)
        self.assertEqual(transition.value, Decimal("1.000"))
        self.assertIsNotNone(transition.unit_conversion)

    def test_detour_conversion_has_exact_unit_lineage(self):
        result = _map({"DETOUR_KILOS_019": "10"})
        carried = _find_feature(result.document, "H01")
        transition = result.lineage["/Features/0/BH17"]

        self.assertEqual(carried["BH17"], 6)
        self.assertEqual(transition.value, Decimal("6"))
        self.assertIsNotNone(transition.unit_conversion)

    def test_h02_unverified_bh12_rule_is_a_review_value_not_a_mapper_crash(self):
        under = _record(
            record_type=RecordType.UNDER,
            line_number=2,
            source_order=2,
            fields={
                "SERVICE_UND_042B": "1",
                "MIN_VERT_CLR_010": "3.1",
            },
        )
        result = _map({"SERVICE_UND_042B": "1"}, unders=(under,))
        under_highway = _find_feature(result.document, "H02")
        review = result.review_lineage["/Features/1/BH12"]

        self.assertNotIn("BH12", under_highway)
        self.assertEqual(review.status.value, "review_required")
        self.assertIsNotNone(review.unit_conversion)

    def test_blank_item99_without_border_evidence_does_not_assert_no_border(self):
        result = _map(
            {
                "OTHR_STATE_STRUC_NO_099": "",
                "OTHER_STATE_CODE_098A": "",
                "OTHER_STATE_PCNT_098B": "",
            }
        )
        self.assertNotIn("BL07", result.document)
        self.assertFalse(
            any(
                _optional_member(issue, "target_path") == "/BL07"
                for issue in result.issues
            )
        )

    def test_blank_item99_with_item98_evidence_is_an_explicit_conflict(self):
        result = _map(
            {
                "OTHR_STATE_STRUC_NO_099": "",
                "OTHER_STATE_CODE_098A": "362",
                "OTHER_STATE_PCNT_098B": "50",
            }
        )
        self.assertNotIn("BL07", result.document)
        matching = [
            issue
            for issue in result.issues
            if _optional_member(issue, "code") == "border_evidence_conflict"
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(_member(matching[0], "target_path"), "/BL07")

    def test_bc11_is_populated_only_by_an_actual_item113_mapping(self):
        without_item113 = _map(
            {"SERVICE_UND_042B": "1", "SCOUR_CRITICAL_113": ""}
        )
        self.assertNotIn("BC11", without_item113.document)

        with_item113 = _map(
            {"SERVICE_UND_042B": "1", "SCOUR_CRITICAL_113": "4"}
        )
        self.assertEqual(with_item113.document["BC11"], "MO-T")

    def test_inspection_requirement_flags_are_direct_and_never_default_to_n(self):
        blank = _map(
            {
                "SERVICE_UND_042B": "5",
                "FRACTURE_092A": "",
                "UNDWATER_LOOK_SEE_092B": "",
            }
        )
        self.assertNotIn("BIR01", blank.document)
        self.assertNotIn("BIR03", blank.document)

        direct = _map(
            {
                # Item 92B is direct evidence even though Item 42B does not
                # describe a waterway crossing.
                "SERVICE_UND_042B": "1",
                "FRACTURE_092A": "N24",
                "UNDWATER_LOOK_SEE_092B": "Y12",
            }
        )
        self.assertEqual(direct.document["BIR01"], "N")
        self.assertEqual(direct.document["BIR03"], "Y")

    def test_no_under_record_does_not_clone_carried_route_into_h02(self):
        result = _map(
            {
                "SERVICE_UND_042B": "1",
                "FEATURES_DESC_006A": "ROAD BELOW",
                "FUNCTIONAL_CLASS_026": "01",
                "ROUTE_PREFIX_005B": "3",
                "SERVICE_LEVEL_005C": "1",
                "ROUTE_NUMBER_005D": "00123",
                "DIRECTION_005E": "0",
                "ADT_029": "012345",
                "PERCENT_ADT_TRUCK_109": "10",
                "YEAR_ADT_030": "2022",
            }
        )
        under_highway = _find_feature(result.document, "H02")
        self.assertNotIn("Routes", under_highway)
        for tag in (
            "BH01",
            "BH02",
            "BH03",
            "BH04",
            "BH05",
            "BH06",
            "BH07",
            "BH09",
            "BH10",
            "BH11",
        ):
            with self.subTest(tag=tag):
                self.assertNotIn(tag, under_highway)

    def test_caller_selected_bid01_is_the_only_identifier_emitted(self):
        result = _map(target_bid01="NJDOT-123")
        self.assertEqual(result.document["BID01"], "NJDOT-123")
        self.assertNotIn("BID03", result.document)


class LineageAndCoverageContractTests(unittest.TestCase):
    def test_rule_lineage_binds_exact_vendored_crosswalk_row_without_execution_claim(self):
        result = _map({"DECK_COND_058": "5"})

        lineage = result.lineage["/BC01"].to_lineage_dict()
        self.assertEqual(
            lineage["rule_source_binding_status"],
            "exact_vendored_crosswalk_reference",
        )
        self.assertEqual(len(lineage["rule_source_references"]), 1)
        reference = lineage["rule_source_references"][0]
        self.assertEqual(
            reference["source_artifact_id"], "nbi_snbi_crosswalk_dec_2023"
        )
        self.assertEqual(
            reference["source_artifact_sha256"],
            "9b81e0b7c9d808cbee65e8a5436f759e5d68b56665fb416ec51e69c40717d4fc",
        )
        self.assertEqual(
            reference["source_locator"], "Crosswalk for Over Records, row 127"
        )
        self.assertEqual(
            reference["field_locator"], "'Crosswalk for Over Records'!B127"
        )
        self.assertEqual(
            reference["legacy_item_locator"],
            "'Crosswalk for Over Records'!E127",
        )
        self.assertEqual(
            reference["rule_locator"], "'Crosswalk for Over Records'!I127"
        )
        self.assertEqual(reference["legacy_item_id"], "58")
        self.assertEqual(reference["source_field_tags"], ["DECK_COND_058"])
        self.assertFalse(reference["execution_verified"])
        self.assertEqual(
            reference["authority_status"],
            "informational_verified_source_bytes_not_executed",
        )

    def test_transition_result_is_deeply_immutable(self):
        result = _map()

        with self.assertRaises(TypeError):
            result.document["BL01"] = 99
        with self.assertRaises(TypeError):
            result.lineage["/BL01"] = result.lineage["/BID01"]
        with self.assertRaises(TypeError):
            result.review_lineage["/forged"] = result.lineage["/BID01"]
        with self.assertRaises(TypeError):
            result.coverage.status_counts["forged"] = 1

        features = result.document["Features"]
        self.assertFalse(
            hasattr(features, "append"),
            "nested repeated rows must not remain mutable lists",
        )
        with self.assertRaises(TypeError):
            features[0]["BF01"] = "forged"

    def test_coverage_names_the_source_record_count_without_claiming_eligibility(self):
        coverage = _map().coverage.to_dict()

        self.assertEqual(coverage["source_record_count"], 1)
        self.assertNotIn("eligible_source_count", coverage)

    def test_missing_and_invalid_service_codes_create_review_evidence(self):
        cases = (
            (
                {"SERVICE_ON_042A": "", "SERVICE_UND_042B": ""},
                "null_source",
                "feature_service_code_missing",
            ),
            (
                {"SERVICE_ON_042A": "Z", "SERVICE_UND_042B": "?"},
                "invalid_source",
                "feature_service_code_invalid",
            ),
        )

        for fields, expected_status, expected_issue_code in cases:
            with self.subTest(fields=fields):
                result = _map(fields)
                relevant_reviews = [
                    transition
                    for transition in result.review_lineage.values()
                    if {
                        source.tag
                        for source in transition.source_fields
                    }
                    & {"SERVICE_ON_042A", "SERVICE_UND_042B"}
                ]
                self.assertTrue(relevant_reviews)
                self.assertTrue(
                    any(
                        transition.status.value == expected_status
                        for transition in relevant_reviews
                    )
                )
                self.assertTrue(
                    any(
                        issue.code == expected_issue_code
                        and issue.target_path == "/Features"
                        for issue in result.issues
                    )
                )

    def test_synthesized_repeated_rows_have_semantic_assumption_lineage(self):
        result = _map({"ROUTE_NUMBER_005D": "00123"})

        for path in (
            "/Features/0/BF01",
            "/Features/0/Routes/0/BRT01",
        ):
            with self.subTest(path=path):
                transition = result.lineage[path]
                self.assertEqual(transition.status.value, "assumed")
                assumption_id = transition.to_lineage_dict().get("assumption_id")
                self.assertIsInstance(assumption_id, str)
                self.assertTrue(assumption_id.strip())

    def test_every_document_leaf_has_lineage_and_coverage_reconciles(self):
        result = _map(
            {
                "COUNTY_CODE_003": "001",
                "PLACE_CODE_004": "01234",
                "DEGREES_SKEW_034": "00",
                "ADT_029": "000125",
                "YEAR_ADT_030": "2022",
                "SCOUR_CRITICAL_113": "4",
            },
            target_bid01="123",
        )
        leaf_paths = _json_pointer_leaves(result.document)
        self.assertEqual(leaf_paths, set(result.lineage))

        coverage = result.coverage
        document_count = _member(coverage, "document_leaf_count")
        lineage_count = _member(coverage, "lineage_leaf_count")
        internal_count = _member(coverage, "internal_derived_leaf_count")
        uncovered_count = _member(coverage, "uncovered_leaf_count")
        self.assertEqual(document_count, len(leaf_paths))
        self.assertEqual(lineage_count, len(result.lineage))
        self.assertEqual(document_count, lineage_count)
        self.assertEqual(uncovered_count, 0)
        self.assertGreaterEqual(internal_count, 0)
        self.assertLessEqual(internal_count, lineage_count)

        status_counts = _member(coverage, "status_counts")
        self.assertEqual(sum(status_counts.values()), lineage_count)
        category_counts = _member(coverage, "category_counts")
        self.assertEqual(sum(category_counts.values()), lineage_count)

    def test_research_profile_is_explicitly_not_submission_ready(self):
        result = _map()
        self.assertFalse(result.submission_ready)


if __name__ == "__main__":
    unittest.main()
