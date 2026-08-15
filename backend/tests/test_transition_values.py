from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from app.services.conversion.transition_values import (
    H01_BH12_RULE_ID,
    CapDomain,
    CapMode,
    CapSemantics,
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
    lineage_to_json,
    parse_decimal,
    parse_dms,
    parse_integer,
    quantize_decimal,
)


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_decimal_parser_preserves_blank_without_a_default(raw):
    parsed = parse_decimal(raw)

    assert parsed.raw == raw
    assert parsed.value is None
    assert parsed.status is ParsedStatus.BLANK


@pytest.mark.parametrize(
    "raw",
    [
        "not-a-number",
        True,
        object(),
        "NaN",
        "Infinity",
        "-Infinity",
        Decimal("NaN"),
        Decimal("Infinity"),
        float("nan"),
        float("inf"),
        -float("inf"),
    ],
)
def test_decimal_parser_rejects_malformed_and_nonfinite_values(raw):
    parsed = parse_decimal(raw)

    assert parsed.value is None
    assert parsed.status is ParsedStatus.INVALID


def test_numeric_parser_preserves_valid_zero_and_range_status():
    zero_decimal = parse_decimal("0", minimum="0", maximum="10")
    zero_integer = parse_integer(0, minimum=0, maximum=10)
    out_of_range = parse_decimal("10.5", minimum="0", maximum="10")

    assert zero_decimal == ParsedScalar("0", Decimal("0"), ParsedStatus.VALID)
    assert zero_integer == ParsedScalar(0, 0, ParsedStatus.VALID)
    assert out_of_range.value == Decimal("10.5")
    assert out_of_range.status is ParsedStatus.OUT_OF_RANGE


def test_integer_parser_distinguishes_fractional_invalid_and_range_error():
    fractional = parse_integer("7.25", minimum=0, maximum=10)
    too_large = parse_integer("11", minimum=0, maximum=10)

    assert fractional.value is None
    assert fractional.status is ParsedStatus.INVALID
    assert too_large.value == 11
    assert too_large.status is ParsedStatus.OUT_OF_RANGE


@pytest.mark.parametrize(
    ("value", "policy", "expected"),
    [
        ("2.5", RoundingPolicy.HALF_UP_WHOLE, Decimal("3")),
        ("-2.5", RoundingPolicy.HALF_UP_WHOLE, Decimal("-3")),
        ("1.25", RoundingPolicy.HALF_UP_TENTH, Decimal("1.3")),
        ("-1.25", RoundingPolicy.HALF_UP_TENTH, Decimal("-1.3")),
        ("1.235", RoundingPolicy.HALF_UP_HUNDREDTH, Decimal("1.24")),
        ("1.2345", RoundingPolicy.HALF_UP_THOUSANDTH, Decimal("1.235")),
        ("-1.2345", RoundingPolicy.HALF_UP_THOUSANDTH, Decimal("-1.235")),
        ("1.2300", RoundingPolicy.EXACT, Decimal("1.2300")),
    ],
)
def test_decimal_rounding_midpoints_are_explicit_and_symmetric(
    value, policy, expected
):
    assert quantize_decimal(value, policy) == expected


def test_round_down_policy_is_restricted_to_the_named_h01_bh12_rule():
    with pytest.raises(ValueError, match="named H01 BH12"):
        quantize_decimal(
            Decimal("10.19"),
            RoundingPolicy.ROUND_DOWN_TENTH,
            rule_id="unrelated-rule",
        )

    assert quantize_decimal(
        Decimal("10.19"),
        RoundingPolicy.ROUND_DOWN_TENTH,
        rule_id=H01_BH12_RULE_ID,
    ) == Decimal("10.1")


def test_numeric_transform_rejects_binary_float_arithmetic():
    with pytest.raises(TypeError, match="Decimal"):
        quantize_decimal(1.25, RoundingPolicy.HALF_UP_TENTH)

    with pytest.raises(TypeError, match="Decimal"):
        TransitionValue(
            value=1.25,
            target_tag="TEST",
            rule_id="test-rule",
            category=TransitionCategory.DIRECT,
        )


def test_direct_transition_maps_source_status_without_zero_or_n_defaults():
    common = {
        "target_tag": "BW01",
        "rule_id": "direct-item-27-v1",
        "source_tag": "YEAR_BUILT_027",
        "source_record_ids": ("record-1",),
    }
    zero = direct_decimal(parse_decimal("0"), **common)
    blank = direct_decimal(parse_decimal(""), **common)
    invalid = direct_decimal(parse_decimal("bad"), **common)
    out_of_range = direct_decimal(
        parse_decimal("1899", minimum="1900"), **common
    )

    assert zero.value == Decimal("0")
    assert zero.status is TransitionStatus.POPULATED
    assert blank.value is None
    assert blank.status is TransitionStatus.NULL_SOURCE
    assert invalid.value is None
    assert invalid.status is TransitionStatus.INVALID_SOURCE
    assert out_of_range.value is None
    assert out_of_range.status is TransitionStatus.INVALID_SOURCE
    assert blank.source_fields[0].parsed.status is ParsedStatus.BLANK


def test_ordinary_unit_conversion_uses_half_up_not_h01_round_down():
    meters_to_feet = UnitConversion(
        source_unit="m",
        target_unit="ft",
        constant=Decimal("3.280839895013123359580052493"),
        definition="international foot: 1 ft = exactly 0.3048 m",
    )
    ordinary = convert_unit(
        parse_decimal("3.1", minimum="0"),
        target_tag="EXAMPLE",
        rule_id="ordinary-half-up-v1",
        source_tag="SOURCE_METERS",
        conversion=meters_to_feet,
        rounding_policy=RoundingPolicy.HALF_UP_TENTH,
    )
    h01 = convert_h01_bh12_m_to_ft(
        parse_decimal("3.1", minimum="0"),
        source_record_ids=("over-record",),
    )

    assert ordinary.value == Decimal("10.2")
    assert ordinary.rounding_policy is RoundingPolicy.HALF_UP_TENTH
    assert h01.value == Decimal("10.1")
    assert h01.rule_id == H01_BH12_RULE_ID
    assert h01.rounding_policy is RoundingPolicy.ROUND_DOWN_TENTH
    assert h01.target_tag == "BH12"
    assert h01.category is TransitionCategory.UNIT
    assert h01.source_record_ids == ("over-record",)


def test_h01_bh12_cap_and_true_zero_are_explicit():
    capped = convert_h01_bh12_m_to_ft(parse_decimal("30", minimum="0"))
    over_cap = convert_h01_bh12_m_to_ft(parse_decimal("31", minimum="0"))
    below_cap = convert_h01_bh12_m_to_ft(parse_decimal("29.999", minimum="0"))
    zero = convert_h01_bh12_m_to_ft(parse_decimal("0", minimum="0"))

    assert capped.value == Decimal("99.9")
    assert over_cap.value == Decimal("99.9")
    assert any("Cap applied" in item for item in capped.messages)
    assert below_cap.value == Decimal("98.4")
    assert zero.value == Decimal("0.0")
    assert zero.status is TransitionStatus.POPULATED
    assert capped.cap_semantics is not None
    assert capped.cap_semantics.domain is CapDomain.SOURCE
    assert capped.cap_semantics.maximum == Decimal("30")


@pytest.mark.parametrize(
    ("mode", "expected_value", "expected_status"),
    [
        (CapMode.CLAMP, Decimal("10.0"), TransitionStatus.POPULATED),
        (CapMode.NULL, None, TransitionStatus.REVIEW_REQUIRED),
        (CapMode.REVIEW_REQUIRED, None, TransitionStatus.REVIEW_REQUIRED),
    ],
)
def test_target_cap_semantics_are_recorded(mode, expected_value, expected_status):
    cap = CapSemantics(
        maximum=Decimal("10"),
        mode=mode,
        reason="test target ceiling",
        domain=CapDomain.TARGET,
    )
    transition = convert_unit(
        parse_decimal("6"),
        target_tag="CAP01",
        rule_id="cap-test-v1",
        source_tag="SOURCE",
        conversion=UnitConversion(
            source_unit="source",
            target_unit="target",
            constant=Decimal("2"),
            definition="test-only exact two-to-one conversion",
        ),
        rounding_policy=RoundingPolicy.HALF_UP_TENTH,
        cap=cap,
    )

    assert transition.value == expected_value
    assert transition.status is expected_status
    assert transition.cap_semantics == cap
    assert any("test target ceiling" in item for item in transition.messages)


def test_target_domain_cap_converts_a_source_domain_replacement():
    transition = convert_unit(
        parse_decimal("6"),
        target_tag="CAP01",
        rule_id="cross-domain-cap-test-v1",
        source_tag="SOURCE",
        conversion=UnitConversion(
            source_unit="source",
            target_unit="target",
            constant=Decimal("2"),
            definition="test-only exact two-to-one conversion",
        ),
        cap=CapSemantics(
            maximum=Decimal("10"),
            mode=CapMode.CLAMP,
            reason="test target ceiling with source-domain replacement",
            domain=CapDomain.TARGET,
            replacement=Decimal("5"),
            replacement_domain=CapDomain.SOURCE,
        ),
    )

    assert transition.value == Decimal("10")


@pytest.mark.parametrize(
    ("raw", "latitude", "status"),
    [
        ("40603000", True, ParsedStatus.OUT_OF_RANGE),
        ("40076000", True, ParsedStatus.OUT_OF_RANGE),
        ("91000000", True, ParsedStatus.OUT_OF_RANGE),
        ("90000100", True, ParsedStatus.OUT_OF_RANGE),
        ("181000000", False, ParsedStatus.OUT_OF_RANGE),
        ("4007300", True, ParsedStatus.INVALID),
        ("040073000", True, ParsedStatus.INVALID),
        ("40AA3000", True, ParsedStatus.INVALID),
        ("40073000E", True, ParsedStatus.INVALID),
        ("074123000N", False, ParsedStatus.INVALID),
        ("-074123000E", False, ParsedStatus.INVALID),
        (None, True, ParsedStatus.BLANK),
        ("", False, ParsedStatus.BLANK),
    ],
)
def test_dms_parser_rejects_invalid_ranges_and_axis_markers(raw, latitude, status):
    assert parse_dms(raw, latitude=latitude).status is status


def test_dms_conversion_validates_geography_and_uses_decimal_rounding():
    latitude = dms_to_decimal(
        parse_dms("40073000N", latitude=True),
        latitude=True,
    )
    longitude = dms_to_decimal(
        parse_dms("074123000W", latitude=False),
        latitude=False,
    )
    south = dms_to_decimal(
        parse_dms("40073000S", latitude=True),
        latitude=True,
    )
    north_pole = dms_to_decimal(
        parse_dms("90000000", latitude=True),
        latitude=True,
    )
    date_line = dms_to_decimal(
        parse_dms("180000000", latitude=False),
        latitude=False,
    )

    assert latitude == ParsedScalar(
        "40073000N", Decimal("40.125"), ParsedStatus.VALID
    )
    assert longitude.value == Decimal("-74.208")
    assert longitude.status is ParsedStatus.VALID
    assert south.value == Decimal("-40.125")
    assert north_pole.value == Decimal("90.000")
    assert date_line.value == Decimal("180.000")


def test_dms_conversion_propagates_blank_invalid_and_out_of_range_status():
    blank = dms_to_decimal(parse_dms("", latitude=True), latitude=True)
    invalid = dms_to_decimal(parse_dms("bad", latitude=True), latitude=True)
    out_of_range = dms_to_decimal(
        parse_dms("91000000", latitude=True), latitude=True
    )

    assert blank.status is ParsedStatus.BLANK and blank.value is None
    assert invalid.status is ParsedStatus.INVALID and invalid.value is None
    assert out_of_range.status is ParsedStatus.OUT_OF_RANGE
    assert out_of_range.value == Decimal("91")


def test_transition_lineage_serialization_is_complete_json_safe_and_deterministic():
    parsed = parse_decimal("3.100")
    transition = convert_h01_bh12_m_to_ft(
        parsed,
        source_record_ids=("record-b", "record-a"),
        transform_version="fhwa-crosswalk-2026.1",
    )

    first = lineage_to_json(transition)
    second = lineage_to_json(transition)
    decoded = json.loads(first)

    assert first == second
    assert decoded["value"] == "10.1"
    assert decoded["target_tag"] == "BH12"
    assert decoded["rule_id"] == H01_BH12_RULE_ID
    assert decoded["category"] == "unit"
    assert decoded["source_record_ids"] == ["record-b", "record-a"]
    assert decoded["source_fields"] == [
        {
            "tag": "MIN_VERT_CLR_010",
            "raw": "3.100",
            "parsed": {"value": "3.1", "status": "valid"},
        }
    ]
    assert decoded["transform_version"] == "fhwa-crosswalk-2026.1"
    assert decoded["rounding_policy"] == "round_down_tenth"
    assert decoded["status"] == "populated"
    assert decoded["unit_conversion"]["source_unit"] == "m"
    assert decoded["unit_conversion"]["target_unit"] == "ft"
    assert decoded["unit_conversion"]["constant"].startswith("3.280839")
    assert decoded["cap_semantics"]["reason"]
    assert isinstance(decoded["messages"], list)
    assert "NaN" not in first and "Infinity" not in first


def test_parsed_and_transition_values_are_immutable():
    parsed = parse_decimal("1")
    transition = direct_decimal(
        parsed,
        target_tag="TEST",
        rule_id="test-v1",
        source_tag="SOURCE",
    )

    with pytest.raises(FrozenInstanceError):
        parsed.value = Decimal("2")
    with pytest.raises(FrozenInstanceError):
        transition.messages = ("changed",)


def test_transition_supports_all_required_categories_and_statuses():
    source = SourceField.from_parsed("SOURCE", parse_decimal("1"))
    for category in TransitionCategory:
        for status in TransitionStatus:
            value = None if status in {
                TransitionStatus.NULL_SOURCE,
                TransitionStatus.INVALID_SOURCE,
            } else Decimal("1")
            unit = (
                UnitConversion("m", "ft", Decimal("1"), "test identity")
                if category is TransitionCategory.UNIT
                else None
            )
            transition = TransitionValue(
                value=value,
                target_tag="TEST",
                rule_id=f"test-{category.value}-{status.value}",
                category=category,
                source_record_ids=("record-1",),
                source_fields=(source,),
                status=status,
                assumption_id=(
                    "test-assumption"
                    if status is TransitionStatus.ASSUMED
                    else None
                ),
                unit_conversion=unit,
            )
            assert transition.category is category
            assert transition.status is status


def test_finite_validation_applies_to_metadata_and_serialization():
    with pytest.raises(ValueError, match="finite"):
        UnitConversion("m", "ft", Decimal("NaN"), "invalid")
    with pytest.raises(ValueError, match="finite"):
        CapSemantics(
            maximum=Decimal("Infinity"),
            reason="invalid",
        )
    with pytest.raises(ValueError, match="finite"):
        TransitionValue(
            value=Decimal("NaN"),
            target_tag="TEST",
            rule_id="invalid-value",
            category=TransitionCategory.DIRECT,
        )
    assert decimal_to_json(Decimal("-0.000")) == "0"
