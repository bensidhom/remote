from __future__ import annotations

import hashlib
import math
import struct
from decimal import Decimal
from types import MappingProxyType

import pytest

from app.services.conversion.canonical_json import (
    CANONICAL_FORMAT,
    CanonicalJsonError,
    CanonicalJsonLimits,
    canonical_json_bytes,
    canonical_json_text,
    canonical_sha256,
    canonical_snapshot,
    parse_canonical_json,
)


def _binary64(hex_bits: str) -> float:
    return struct.unpack(">d", bytes.fromhex(hex_bits))[0]


def _error_code(exc_info: pytest.ExceptionInfo[CanonicalJsonError]) -> str:
    return exc_info.value.code


def test_format_identifier_documents_the_versioned_safe_numeric_profile():
    assert CANONICAL_FORMAT == "rfc8785-jcs-snbi-safe-integer-v1"


def test_rfc8785_section_3_2_2_primitive_serialization_vector():
    value = {
        "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 1e-27],
        "string": "€$\x0f\nA'B\"\\\\\"/",
        "literals": [None, True, False],
    }

    assert canonical_json_text(value) == (
        '{"literals":[null,true,false],'
        '"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
        '"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
    )


def test_rfc8785_section_3_2_3_utf16_property_sorting_vector():
    value = {
        "€": "Euro Sign",
        "\r": "Carriage Return",
        "דּ": "Hebrew Letter Dalet With Dagesh",
        "1": "One",
        "😀": "Emoji: Grinning Face",
        "\x80": "Control",
        "ö": "Latin Small Letter O With Diaeresis",
    }

    assert list(parse_canonical_json(canonical_json_bytes(value)).values()) == [
        "Carriage Return",
        "One",
        "Control",
        "Latin Small Letter O With Diaeresis",
        "Euro Sign",
        "Emoji: Grinning Face",
        "Hebrew Letter Dalet With Dagesh",
    ]


@pytest.mark.parametrize(
    "hex_bits,expected",
    [
        ("0000000000000000", "0"),
        ("8000000000000000", "0"),
        ("0000000000000001", "5e-324"),
        ("8000000000000001", "-5e-324"),
        ("7fefffffffffffff", "1.7976931348623157e+308"),
        ("ffefffffffffffff", "-1.7976931348623157e+308"),
        ("4340000000000000", "9007199254740992"),
        ("c340000000000000", "-9007199254740992"),
        ("4430000000000000", "295147905179352830000"),
        ("44b52d02c7e14af5", "9.999999999999997e+22"),
        ("44b52d02c7e14af6", "1e+23"),
        ("44b52d02c7e14af7", "1.0000000000000001e+23"),
        ("444b1ae4d6e2ef4e", "999999999999999700000"),
        ("444b1ae4d6e2ef4f", "999999999999999900000"),
        ("444b1ae4d6e2ef50", "1e+21"),
        ("3eb0c6f7a0b5ed8c", "9.999999999999997e-7"),
        ("3eb0c6f7a0b5ed8d", "0.000001"),
        ("41b3de4355555553", "333333333.3333332"),
        ("41b3de4355555554", "333333333.33333325"),
        ("41b3de4355555555", "333333333.3333333"),
        ("41b3de4355555556", "333333333.3333334"),
        ("41b3de4355555557", "333333333.33333343"),
        ("becbf647612f3696", "-0.0000033333333333333333"),
        ("43143ff3c1cb0959", "1424953923781206.2"),
    ],
)
def test_rfc8785_appendix_b_finite_number_vectors(hex_bits: str, expected: str):
    assert canonical_json_text(_binary64(hex_bits)) == expected


def test_key_order_and_input_whitespace_are_hash_insignificant():
    left = parse_canonical_json(' { "z" : 2, "a" : {"y":4,"x":3} } ')
    right = parse_canonical_json('{"a":{"x":3,"y":4},"z":2}')
    expected = b'{"a":{"x":3,"y":4},"z":2}'

    assert canonical_json_bytes(left) == expected
    assert canonical_json_bytes(right) == expected
    assert canonical_sha256(left) == canonical_sha256(right)
    assert canonical_sha256(left) == hashlib.sha256(expected).hexdigest()


def test_array_order_is_significant_to_bytes_and_hashes():
    assert canonical_json_bytes({"items": [1, 2]}) != canonical_json_bytes(
        {"items": [2, 1]}
    )
    assert canonical_sha256({"items": [1, 2]}) != canonical_sha256(
        {"items": [2, 1]}
    )


def test_distinct_json_primitive_types_do_not_collide():
    values = [None, False, 0, "0", [], {}]
    digests = {canonical_sha256(value) for value in values}

    assert len(digests) == len(values)
    # JCS deliberately treats all JSON numbers as binary64 numbers; it does not
    # retain a language-specific int/float spelling distinction.
    assert canonical_sha256(1) == canonical_sha256(1.0)


def test_string_escaping_is_minimal_lowercase_and_utf8():
    text = '€/\b\t\n\f\r\x00\x1f"\\'

    assert canonical_json_bytes(text) == (
        b'"\xe2\x82\xac/\\b\\t\\n\\f\\r\\u0000\\u001f\\"\\\\"'
    )


def test_unicode_is_not_normalized_before_hashing():
    assert canonical_sha256("é") != canonical_sha256("e\u0301")


@pytest.mark.parametrize(
    "payload",
    [
        '{"a":1,"a":2}',
        '{"outer":{"x":1,"x":2}}',
        b'{"escaped":1,"\\u0065scaped":2}',
    ],
)
def test_parser_rejects_duplicate_object_names(payload: str | bytes):
    with pytest.raises(CanonicalJsonError) as exc_info:
        parse_canonical_json(payload)

    assert _error_code(exc_info) == "duplicate_key"


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_serializer_rejects_nonfinite_numbers_with_pointer(value: float):
    with pytest.raises(CanonicalJsonError) as exc_info:
        canonical_json_bytes({"number": value})

    assert _error_code(exc_info) == "non_finite_number"
    assert exc_info.value.json_pointer == "/number"


@pytest.mark.parametrize("payload", ["NaN", "Infinity", "-Infinity", "1e400"])
def test_parser_rejects_nonfinite_numbers(payload: str):
    with pytest.raises(CanonicalJsonError) as exc_info:
        parse_canonical_json(payload)

    assert _error_code(exc_info) == "non_finite_number"


@pytest.mark.parametrize(
    "value,code",
    [
        (Decimal("1.25"), "unsupported_type"),
        ((1 << 53), "unsafe_integer"),
        (-(1 << 53), "unsafe_integer"),
        ({1: "not a JSON object key"}, "object_key_not_string"),
    ],
)
def test_unsupported_values_fail_closed(value, code: str):
    with pytest.raises(CanonicalJsonError) as exc_info:
        canonical_json_bytes(value)

    assert _error_code(exc_info) == code


def test_parser_round_trips_canonical_integer_shaped_binary64_values():
    text = "295147905179352830000"
    parsed = parse_canonical_json(text)

    assert type(parsed) is float
    assert canonical_json_text(parsed) == text


def test_parser_rejects_unsafe_integer_that_would_be_silently_rounded():
    with pytest.raises(CanonicalJsonError) as exc_info:
        parse_canonical_json(str((1 << 53) + 1))

    assert _error_code(exc_info) == "unsafe_integer"


@pytest.mark.parametrize(
    "value",
    ["\ud800", "\udfff", {"\ud800": "bad key"}, {"value": "\udfff"}],
)
def test_lone_surrogates_are_rejected(value):
    with pytest.raises(CanonicalJsonError) as exc_info:
        canonical_json_bytes(value)

    assert _error_code(exc_info) == "invalid_unicode"


def test_parser_rejects_lone_surrogate_escape_and_invalid_utf8():
    with pytest.raises(CanonicalJsonError) as surrogate_error:
        parse_canonical_json('"\\ud800"')
    with pytest.raises(CanonicalJsonError) as utf8_error:
        parse_canonical_json(b'"\xff"')

    assert _error_code(surrogate_error) == "invalid_unicode"
    assert _error_code(utf8_error) == "invalid_utf8"


def test_depth_limit_counts_nested_containers_and_preflights_parser():
    limits = CanonicalJsonLimits(max_depth=2, max_nodes=20, max_bytes=100)

    assert canonical_json_text({"allowed": [1]}, limits) == '{"allowed":[1]}'
    with pytest.raises(CanonicalJsonError) as encode_error:
        canonical_json_bytes({"too": [[1]]}, limits)
    with pytest.raises(CanonicalJsonError) as parse_error:
        parse_canonical_json('{"too":[[1]]}', limits)

    assert _error_code(encode_error) == "max_depth_exceeded"
    assert encode_error.value.json_pointer == "/too/0"
    assert _error_code(parse_error) == "max_depth_exceeded"


def test_node_limit_counts_root_and_member_values():
    limits = CanonicalJsonLimits(max_depth=10, max_nodes=3, max_bytes=100)

    assert canonical_json_text({"a": 1, "b": 2}, limits) == '{"a":1,"b":2}'
    with pytest.raises(CanonicalJsonError) as exc_info:
        canonical_json_bytes({"a": 1, "b": 2, "c": 3}, limits)

    assert _error_code(exc_info) == "max_nodes_exceeded"
    assert exc_info.value.json_pointer == "/c"


def test_byte_limit_applies_to_input_and_escaped_canonical_output():
    limits = CanonicalJsonLimits(max_depth=10, max_nodes=10, max_bytes=5)

    assert canonical_json_bytes("é", limits) == b'"\xc3\xa9"'
    with pytest.raises(CanonicalJsonError) as output_error:
        canonical_json_bytes("\x00", limits)
    with pytest.raises(CanonicalJsonError) as input_error:
        parse_canonical_json("  null", limits)

    assert _error_code(output_error) == "max_bytes_exceeded"
    assert _error_code(input_error) == "max_bytes_exceeded"


def test_cycles_are_rejected_with_the_reentry_pointer():
    value: list[object] = []
    value.append(value)

    with pytest.raises(CanonicalJsonError) as exc_info:
        canonical_json_bytes(value)

    assert _error_code(exc_info) == "circular_reference"
    assert exc_info.value.json_pointer == "/0"


def test_snapshot_is_detached_immutable_and_hash_stable():
    original = {"rows": [{"value": 1.0}], "enabled": True}
    snapshot = canonical_snapshot(original)
    before = canonical_sha256(snapshot)

    assert isinstance(snapshot, MappingProxyType)
    assert isinstance(snapshot["rows"], tuple)
    assert isinstance(snapshot["rows"][0], MappingProxyType)
    assert type(snapshot["rows"][0]["value"]) is float
    with pytest.raises(TypeError):
        snapshot["enabled"] = False
    with pytest.raises(TypeError):
        snapshot["rows"][0]["value"] = 2.0

    original["enabled"] = False
    original["rows"][0]["value"] = 99.0
    original["rows"].append({"value": 2.0})

    assert canonical_sha256(snapshot) == before
    assert canonical_json_text(snapshot) == (
        '{"enabled":true,"rows":[{"value":1}]}'
    )


def test_parser_accepts_utf8_bytes_and_returns_ordinary_json_containers():
    value = parse_canonical_json(b'{"array":[true,null,"\xe2\x82\xac"]}')

    assert value == {"array": [True, None, "€"]}
    assert type(value) is dict
    assert type(value["array"]) is list


def test_invalid_json_has_a_stable_error_code_and_location_message():
    with pytest.raises(CanonicalJsonError) as exc_info:
        parse_canonical_json('{"missing":}')

    assert _error_code(exc_info) == "invalid_json"
    assert "line 1" in exc_info.value.message
    assert "column" in exc_info.value.message


@pytest.mark.parametrize(
    "kwargs,error_type",
    [
        ({"max_depth": -1}, ValueError),
        ({"max_depth": 257}, ValueError),
        ({"max_nodes": 0}, ValueError),
        ({"max_bytes": 0}, ValueError),
        ({"max_depth": True}, TypeError),
    ],
)
def test_limit_configuration_is_itself_bounded(kwargs, error_type):
    with pytest.raises(error_type):
        CanonicalJsonLimits(**kwargs)
