from __future__ import annotations

import random

import pytest

from app.services.conversion.inspection_events import (
    InspectionBridgeIdentity,
    InspectionEventKind,
    InspectionEventStatus,
    InspectionResultStatus,
    InspectionRuleBundle,
    InspectionSourceRecord,
    TwoDigitYearPolicy,
    build_inspection_events,
    inspection_source_from_nbi_record,
)
from app.services.conversion.nbi_parser import (
    LegacyBridgeKey,
    RecordType,
    SourceRecord,
)


BRIDGE = InspectionBridgeIdentity("NJDOT", "34", "0000001")
RULES = InspectionRuleBundle()


def _source(
    source_id: str,
    year: int,
    *,
    source_order: int = 0,
    bridge: InspectionBridgeIdentity = BRIDGE,
    **fields: str,
) -> InspectionSourceRecord:
    return InspectionSourceRecord(
        source_record_id=source_id,
        bridge_identity=bridge,
        inventory_year=year,
        source_order=source_order,
        fields=fields,
    )


def _reduce(*records: InspectionSourceRecord, rules=RULES):
    grouped = {}
    for record in records:
        grouped.setdefault(record.inventory_year, []).append(record)
    return build_inspection_events({BRIDGE: grouped}, rules)[0]


def _events(result, kind: InspectionEventKind):
    return [event for event in result.events if event.event_kind is kind]


def _codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


def test_first_and_later_annual_occurrences_become_initial_then_routine():
    first = _source(
        "row-2022", 2022, DATE_OF_INSPECT_090="1022", INSPECT_FREQ_MONTHS_091="24"
    )
    later = _source(
        "row-2023", 2023, DATE_OF_INSPECT_090="0923", INSPECT_FREQ_MONTHS_091="12"
    )

    result = _reduce(later, first)
    routine = _events(result, InspectionEventKind.ROUTINE)

    assert result.status is InspectionResultStatus.COMPLETE
    assert [event.to_snbi_dict() for event in routine] == [
        {"BIE01": "1", "BIE02": "20221001", "BIE05": 24},
        {"BIE01": "2", "BIE02": "20230901", "BIE05": 12},
    ]
    assert routine[0].values["BIE02"].generated is True
    assert routine[0].values["BIE02"].proxy_components == ("day",)
    assert routine[0].values["BIE02"].source_values[0].raw_value == "1022"
    assert set(routine[0].to_snbi_dict()) == {"BIE01", "BIE02", "BIE05"}
    assert "BIE03" not in routine[0].values
    assert "BIE04" not in routine[0].values
    assert "BIE07" not in routine[0].values


def test_duplicate_mmyy_in_consecutive_years_selects_latest_and_keeps_lineage():
    older = _source(
        "older", 2022, source_order=10, DATE_OF_INSPECT_090="1221", INSPECT_FREQ_MONTHS_091="12"
    )
    newer = _source(
        "newer", 2023, source_order=1, DATE_OF_INSPECT_090="1221", INSPECT_FREQ_MONTHS_091="24"
    )

    result = _reduce(newer, older)
    event = _events(result, InspectionEventKind.ROUTINE)[0]

    assert len(_events(result, InspectionEventKind.ROUTINE)) == 1
    assert event.status is InspectionEventStatus.DEDUPLICATED
    assert event.first_inventory_year == 2022
    assert event.selected_inventory_year == 2023
    assert event.selected_source_record_id == "newer"
    assert event.source_record_ids == ("older", "newer")
    assert event.to_snbi_dict() == {
        "BIE01": "1",
        "BIE02": "20211201",
        "BIE05": 24,
    }
    assert event.values["BIE05"].source_record_ids == ("older", "newer")
    assert "duplicate_annual_mmyy_deduplicated" in _codes(result)


def test_same_mmyy_after_an_inventory_gap_is_not_deduplicated():
    first = _source("first", 2021, DATE_OF_INSPECT_090="1220")
    after_gap = _source("after-gap", 2023, DATE_OF_INSPECT_090="1220")

    routine = _events(_reduce(after_gap, first), InspectionEventKind.ROUTINE)

    assert len(routine) == 2
    assert [event.values["BIE01"].value for event in routine] == ["1", "2"]
    assert all(event.status is InspectionEventStatus.EMITTED for event in routine)


def test_most_recent_source_order_wins_within_one_inventory_year():
    earlier_row = _source(
        "line-10", 2023, source_order=10, DATE_OF_INSPECT_090="0123", INSPECT_FREQ_MONTHS_091="12"
    )
    later_row = _source(
        "line-20", 2023, source_order=20, DATE_OF_INSPECT_090="0123", INSPECT_FREQ_MONTHS_091="18"
    )

    event = _events(
        _reduce(later_row, earlier_row), InspectionEventKind.ROUTINE
    )[0]

    assert event.selected_source_record_id == "line-20"
    assert event.values["BIE05"].value == 18
    assert event.source_record_ids == ("line-10", "line-20")


def test_missing_and_invalid_item90_are_issues_and_create_no_routine_event():
    missing = _source("missing", 2023)
    invalid_format = _source("format", 2023, DATE_OF_INSPECT_090="1/23")
    invalid_month = _source("month", 2023, DATE_OF_INSPECT_090="1322")

    result = _reduce(missing, invalid_format, invalid_month)

    assert result.status is InspectionResultStatus.FAILED
    assert _events(result, InspectionEventKind.ROUTINE) == []
    assert {
        "item90_missing",
        "inspection_date_invalid_format",
        "inspection_date_invalid_month",
    } <= _codes(result)
    assert all(issue.source_record_ids for issue in result.issues)


@pytest.mark.parametrize(
    ("kind", "flag_field", "date_field", "expected_bie01"),
    [
        (
            InspectionEventKind.NSTM,
            "FRACTURE_092A",
            "FRACTURE_LAST_DATE_093A",
            "4",
        ),
        (
            InspectionEventKind.UNDERWATER,
            "UNDWATER_LOOK_SEE_092B",
            "UNDWATER_LAST_DATE_093B",
            "3",
        ),
        (
            InspectionEventKind.SPECIAL,
            "SPEC_INSPECT_092C",
            "SPEC_LAST_DATE_093C",
            "7",
        ),
    ],
)
def test_applicable_item92_and_paired_item93_create_feature_event_only(
    kind, flag_field, date_field, expected_bie01
):
    source = _source(
        "feature",
        2024,
        DATE_OF_INSPECT_090="0124",
        **{flag_field: "Y12", date_field: "1223"},
    )

    result = _reduce(source)
    feature = _events(result, kind)[0]

    assert feature.to_snbi_dict() == {
        "BIE01": expected_bie01,
        "BIE02": "20231201",
        "BIE05": 12,
    }
    assert feature.values["BIE02"].generated is True
    assert feature.values["BIE02"].proxy_components == ("day",)
    assert "BIE03" not in feature.values
    assert "BIE04" not in feature.values
    assert "BIE07" not in feature.values


@pytest.mark.parametrize("not_applicable", ["N", "N00", "N12"])
def test_not_applicable_item92_never_emits_event_even_if_item93_has_date(
    not_applicable,
):
    source = _source(
        "not-applicable",
        2024,
        DATE_OF_INSPECT_090="0124",
        FRACTURE_092A=not_applicable,
        FRACTURE_LAST_DATE_093A="1223",
    )

    result = _reduce(source)

    assert _events(result, InspectionEventKind.NSTM) == []
    assert "inapplicable_item93_ignored" in _codes(result)


def test_item93_date_without_item92_applicability_never_invents_event():
    source = _source(
        "date-only",
        2024,
        DATE_OF_INSPECT_090="0124",
        UNDWATER_LAST_DATE_093B="1223",
    )

    result = _reduce(source)

    assert _events(result, InspectionEventKind.UNDERWATER) == []
    assert "item92_required_for_item93" in _codes(result)


@pytest.mark.parametrize(
    ("flag", "date", "expected_code"),
    [
        ("Y12", None, "applicable_item93_missing"),
        ("Y12", "abcd", "inspection_date_invalid_format"),
        ("YES", "1223", "item92_invalid"),
        ("Y1", "1223", "item92_invalid"),
    ],
)
def test_invalid_or_incomplete_item92_93_pairs_do_not_emit_feature_event(
    flag, date, expected_code
):
    fields = {
        "DATE_OF_INSPECT_090": "0124",
        "SPEC_INSPECT_092C": flag,
    }
    if date is not None:
        fields["SPEC_LAST_DATE_093C"] = date
    result = _reduce(_source("special-invalid", 2024, **fields))

    assert _events(result, InspectionEventKind.SPECIAL) == []
    assert expected_code in _codes(result)


def test_item92_zero_interval_is_sourced_not_defaulted_or_discarded():
    source = _source(
        "zero-interval",
        2024,
        DATE_OF_INSPECT_090="0124",
        FRACTURE_092A="Y00",
        FRACTURE_LAST_DATE_093A="1223",
    )

    event = _events(_reduce(source), InspectionEventKind.NSTM)[0]

    assert event.values["BIE05"].value == 0
    assert event.values["BIE05"].source_values[0].raw_value == "Y00"


def test_pre_2000_date_requires_explicit_compatibility_policy():
    source = _source("legacy", 1999, DATE_OF_INSPECT_090="1298")

    default_result = _reduce(source)
    compatibility_result = _reduce(
        source,
        rules=InspectionRuleBundle(
            two_digit_year_policy=TwoDigitYearPolicy.INVENTORY_CONTEXT_COMPAT
        ),
    )

    assert _events(default_result, InspectionEventKind.ROUTINE) == []
    assert "pre2000_date_policy_required" in _codes(default_result)
    legacy_event = _events(
        compatibility_result, InspectionEventKind.ROUTINE
    )[0]
    assert legacy_event.values["BIE02"].value == "19981201"
    assert legacy_event.values["BIE02"].proxy_components == ("day", "century")
    assert "pre2000_compatibility_applied" in _codes(compatibility_result)
    assert (
        compatibility_result.events[0].rule_ids[-1]
        == "fhwa-mmyy-inventory-context-century-compatibility-v1"
    )


def test_rule_bundle_coerces_serialized_year_policy_to_the_enum():
    rules = InspectionRuleBundle(
        two_digit_year_policy=TwoDigitYearPolicy.INVENTORY_CONTEXT_COMPAT.value
    )

    assert rules.two_digit_year_policy is TwoDigitYearPolicy.INVENTORY_CONTEXT_COMPAT


def test_future_modern_date_is_not_silently_reinterpreted_without_policy():
    source = _source("ambiguous", 2001, DATE_OF_INSPECT_090="1299")

    result = _reduce(source)

    assert result.events == ()
    assert "pre2000_date_policy_required" in _codes(result)


def test_pairing_is_field_specific_and_crossed_92_93_values_do_not_create_events():
    source = _source(
        "crossed",
        2024,
        DATE_OF_INSPECT_090="0124",
        FRACTURE_092A="Y12",
        UNDWATER_LAST_DATE_093B="1223",
    )

    result = _reduce(source)

    assert _events(result, InspectionEventKind.NSTM) == []
    assert _events(result, InspectionEventKind.UNDERWATER) == []
    assert "applicable_item93_missing" in _codes(result)
    assert "item92_required_for_item93" in _codes(result)


def test_output_is_order_independent_for_year_maps_and_record_sequences():
    records = [
        _source("r-2021", 2021, source_order=1, DATE_OF_INSPECT_090="1220"),
        _source("r-2022", 2022, source_order=2, DATE_OF_INSPECT_090="1220"),
        _source(
            "r-2023",
            2023,
            source_order=3,
            DATE_OF_INSPECT_090="1123",
            FRACTURE_092A="Y24",
            FRACTURE_LAST_DATE_093A="1023",
        ),
    ]
    canonical = {
        BRIDGE: {
            2021: [records[0]],
            2022: [records[1]],
            2023: [records[2]],
        }
    }
    shuffled_records = list(records)
    random.Random(41).shuffle(shuffled_records)
    shuffled = {BRIDGE: {record.inventory_year: [record] for record in shuffled_records}}

    assert build_inspection_events(canonical, RULES) == build_inspection_events(
        shuffled, RULES
    )


def test_group_identity_and_year_are_validated_instead_of_trusted():
    other = InspectionBridgeIdentity("NJDOT", "34", "0000002")
    wrong_identity = _source(
        "wrong-bridge", 2023, bridge=other, DATE_OF_INSPECT_090="0123"
    )
    wrong_year = _source("wrong-year", 2022, DATE_OF_INSPECT_090="0122")

    result = build_inspection_events(
        {BRIDGE: {2023: [wrong_identity, wrong_year]}}, RULES
    )[0]

    assert result.records_supplied == 2
    assert result.records_accepted == 0
    assert result.events == ()
    assert result.status is InspectionResultStatus.FAILED
    assert {"group_identity_mismatch", "group_inventory_year_mismatch"} <= _codes(
        result
    )


def test_input_fields_are_copied_into_immutable_source_record():
    fields = {"DATE_OF_INSPECT_090": "0123"}
    record = InspectionSourceRecord("copy", BRIDGE, 2023, fields)
    fields["DATE_OF_INSPECT_090"] = "0223"

    result = _reduce(record)

    assert result.events[0].values["BIE02"].value == "20230101"
    with pytest.raises(TypeError):
        record.fields["DATE_OF_INSPECT_090"] = "0323"


def test_rule_ids_and_source_ids_are_present_on_every_emitted_value():
    result = _reduce(
        _source(
            "lineage",
            2024,
            DATE_OF_INSPECT_090="0124",
            FRACTURE_092A="Y12",
            FRACTURE_LAST_DATE_093A="1223",
        )
    )

    for event in result.events:
        assert event.rule_ids
        assert event.source_record_ids == ("lineage",)
        for tag, value in event.values.items():
            assert value.target_tag == tag
            assert value.rule_id
            assert value.source_record_ids == ("lineage",)
            assert value.source_values


def test_nbi_source_record_adapter_preserves_identity_fields_and_provenance():
    source = SourceRecord(
        source_order=17,
        line_number=42,
        byte_offset=1000,
        raw_sha256="a" * 64,
        source_record_id="annual-file:42",
        record_type=RecordType.OVER,
        bridge_key=LegacyBridgeKey("NJDOT", "34", "000001234567890"),
        raw_item_001="034",
        raw_item_008="000001234567890",
        fields={
            "DATE_OF_INSPECT_090": "0124",
            "INSPECT_FREQ_MONTHS_091": "12",
        },
        conformant=True,
    )

    adapted = inspection_source_from_nbi_record(source, inventory_year=2024)

    assert adapted.source_record_id == "annual-file:42"
    assert adapted.source_order == 17
    assert adapted.inventory_year == 2024
    assert adapted.bridge_identity == InspectionBridgeIdentity(
        "NJDOT", "34", "000001234567890"
    )
    assert adapted.fields == source.fields
    event = _events(
        build_inspection_events(
            {adapted.bridge_identity: {2024: [adapted]}}, RULES
        )[0],
        InspectionEventKind.ROUTINE,
    )[0]
    assert event.source_record_ids == ("annual-file:42",)


def test_nbi_source_record_adapter_requires_explicit_valid_year_and_type():
    with pytest.raises(TypeError, match="nbi_parser.SourceRecord"):
        inspection_source_from_nbi_record(object(), inventory_year=2024)

    source = SourceRecord(
        source_order=1,
        line_number=1,
        byte_offset=0,
        raw_sha256="b" * 64,
        source_record_id="record-1",
        record_type=RecordType.OVER,
        bridge_key=LegacyBridgeKey("NJDOT", "34", "1"),
        raw_item_001="34",
        raw_item_008="1",
        fields={},
        conformant=True,
    )
    with pytest.raises(ValueError, match="inventory_year"):
        inspection_source_from_nbi_record(source, inventory_year=1899)
