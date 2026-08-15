from __future__ import annotations

import gzip
import io
from dataclasses import replace
from decimal import Decimal

import pytest

from app.services.conversion.element_xml import (
    BridgeCandidate,
    ElementXmlLimits,
    FieldPresence,
    IssueSeverity,
    LEGACY_ELEMENT_SCHEMA_ID,
    MatchStatus,
    match_element_records,
    parse_element_xml,
)


def _record(
    *,
    bridge: str = "000123",
    state: str | None = "34",
    agency: str | None = None,
    en: str = "12",
    epn: str | None = "0",
    total: str | None = "10",
    conditions: tuple[str | None, str | None, str | None, str | None] = (
        "1",
        "2",
        "3",
        "4",
    ),
) -> str:
    def tag(name: str, value: str | None) -> str:
        return "" if value is None else f"<{name}>{value}</{name}>"

    return (
        "<FHWAED>"
        f"<STRUCNUM>{bridge}</STRUCNUM>"
        f"{tag('STATE', state)}"
        f"{tag('AGENCY', agency)}"
        f"<EN>{en}</EN>"
        f"{tag('EPN', epn)}"
        f"{tag('TOTALQTY', total)}"
        f"{tag('CS1', conditions[0])}"
        f"{tag('CS2', conditions[1])}"
        f"{tag('CS3', conditions[2])}"
        f"{tag('CS4', conditions[3])}"
        "</FHWAED>"
    )


def _document(*records: str) -> bytes:
    return ("<BridgeElements>" + "".join(records) + "</BridgeElements>").encode()


def _codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


def test_namespace_aware_parse_retains_identity_raw_text_and_path():
    xml = b"""\
    <be:BridgeElements xmlns:be="urn:fhwa:elements">
      <be:FHWAED>
        <be:STRUCNUM> 000123 </be:STRUCNUM>
        <be:STATE_CODE>034</be:STATE_CODE>
        <be:AGENCY_CODE> 01 </be:AGENCY_CODE>
        <be:EN>12</be:EN><be:EPN>0</be:EPN>
        <be:TOTALQTY>10.0</be:TOTALQTY>
        <be:CS1>1</be:CS1><be:CS2>2</be:CS2>
        <be:CS3>3</be:CS3><be:CS4>4</be:CS4>
      </be:FHWAED>
    </be:BridgeElements>
    """

    result = parse_element_xml(
        io.BytesIO(xml), allowed_namespaces={"urn:fhwa:elements"}
    )

    assert result.element_file_supplied is True
    assert result.element_file_parsed is True
    assert result.element_records == 1
    assert result.records_seen == 1
    assert result.source_bytes == len(xml)
    record = result.records[0]
    assert record.record_index == 1
    assert record.record_path.endswith("/FHWAED[1]")
    assert record.line_number is None
    assert record.namespace_uri == "urn:fhwa:elements"
    assert record.bridge_identity.structure_number_raw == " 000123 "
    assert record.bridge_identity.structure_number == "000123"
    assert record.bridge_identity.state == "34"
    assert record.bridge_identity.agency == "1"
    assert record.fields["EN"].value == Decimal("12")


def test_missing_and_present_empty_values_are_not_defaulted_to_zero():
    xml = b"""\
    <BridgeElements><FHWAED>
      <STRUCNUM>1</STRUCNUM><EN>12</EN><EPN>   </EPN><CS1/>
    </FHWAED></BridgeElements>
    """

    result = parse_element_xml(xml)

    assert result.element_file_parsed is True
    record = result.records[0]
    assert record.fields["EPN"].presence is FieldPresence.EMPTY
    assert record.fields["EPN"].raw_text == "   "
    assert record.fields["CS1"].presence is FieldPresence.EMPTY
    assert record.fields["CS1"].raw_text == ""
    assert record.fields["TOTALQTY"].presence is FieldPresence.MISSING
    assert record.fields["TOTALQTY"].raw_text is None
    assert record.fields["CS4"].presence is FieldPresence.MISSING
    assert record.to_snbi_fields() == {
        "BE01": "12",
        "BE02": "   ",
        "BE03": None,
        "BCS01": "",
        "BCS02": None,
        "BCS03": None,
        "BCS04": None,
    }


@pytest.mark.parametrize(
    ("xml", "expected_code"),
    [
        (
            b'<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]>'
            b"<BridgeElements>&secret;</BridgeElements>",
            "doctype_forbidden",
        ),
        (
            b"<BridgeElements>&custom;</BridgeElements>",
            "entity_reference_forbidden",
        ),
        (
            b'<BridgeElements xmlns:xi="http://www.w3.org/2001/XInclude">'
            b'<xi:include href="file:///etc/passwd"/></BridgeElements>',
            "external_reference_forbidden",
        ),
    ],
)
def test_dtd_entity_and_external_references_are_rejected_before_parse(
    xml: bytes, expected_code: str
):
    result = parse_element_xml(xml)

    assert result.element_file_parsed is False
    assert result.records == ()
    assert expected_code in _codes(result)
    assert any(issue.severity is IssueSeverity.FATAL for issue in result.issues)


def test_malformed_xml_discards_any_partial_records():
    xml = b"<BridgeElements>" + _record().encode() + b"<FHWAED>"

    result = parse_element_xml(xml)

    assert result.element_file_parsed is False
    assert result.element_records == 0
    assert result.records == ()
    assert result.records_seen == 2
    assert "malformed_xml" in _codes(result)


def test_duplicate_element_key_and_parent_relationships_are_issues():
    duplicate = _record(en="12", epn="99")
    cycle_one = _record(en="20", epn="21")
    cycle_two = _record(en="21", epn="20")
    result = parse_element_xml(_document(duplicate, duplicate, cycle_one, cycle_two))

    assert result.element_file_parsed is True
    assert result.element_records == 4
    assert "duplicate_element_key" in _codes(result)
    assert "element_parent_missing" in _codes(result)
    assert "element_parent_cycle" in _codes(result)


def test_same_element_number_may_have_distinct_valid_parents():
    parent_deck = _record(en="12", epn="0")
    parent_girder = _record(en="107", epn="0")
    deck_protection = _record(en="515", epn="12")
    girder_protection = _record(en="515", epn="107")

    result = parse_element_xml(
        _document(parent_deck, parent_girder, deck_protection, girder_protection)
    )

    assert result.element_file_parsed is True
    assert result.element_records == 4
    assert "duplicate_element_key" not in _codes(result)
    assert "element_parent_missing" not in _codes(result)


def test_finite_nonnegative_field_rules_and_condition_total_checks():
    invalid = _record(
        en="1.5",
        epn="-1",
        total="NaN",
        conditions=("-1", "2", "3", "4"),
    )
    exceeds = _record(en="13", total="2", conditions=("1", "1", "1", "1"))
    below = _record(en="14", total="10", conditions=("1", "1", "1", "1"))

    result = parse_element_xml(_document(invalid, exceeds, below))

    codes = _codes(result)
    assert result.element_file_parsed is True
    assert "numeric_not_integer" in codes
    assert "numeric_negative" in codes
    assert "numeric_not_finite" in codes
    assert "condition_sum_exceeds_total" in codes
    assert "condition_sum_differs_total" in codes


@pytest.mark.parametrize(
    ("limits", "xml", "expected_code"),
    [
        (
            ElementXmlLimits(max_depth=3),
            b"<a><b><c><d/></c></b></a>",
            "depth_limit_exceeded",
        ),
        (
            ElementXmlLimits(max_nodes=2),
            b"<a><b/><c/></a>",
            "node_limit_exceeded",
        ),
        (
            ElementXmlLimits(max_records=1),
            _document(_record(), _record(en="13")),
            "record_limit_exceeded",
        ),
        (
            ElementXmlLimits(max_text_chars=5),
            b"<a>123456</a>",
            "text_limit_exceeded",
        ),
    ],
)
def test_structural_resource_limits_fail_closed(limits, xml, expected_code):
    result = parse_element_xml(xml, limits=limits)

    assert result.element_file_parsed is False
    assert result.records == ()
    assert expected_code in _codes(result)


def test_source_and_gzip_expansion_limits_are_independent():
    xml = _document(_record())
    too_small_source = replace(
        ElementXmlLimits(),
        max_source_bytes=len(xml) - 1,
        max_expanded_bytes=len(xml) * 2,
    )
    source_result = parse_element_xml(xml, limits=too_small_source)

    compressed = gzip.compress(xml)
    too_small_expansion = replace(
        ElementXmlLimits(),
        max_source_bytes=len(compressed) + 1,
        max_expanded_bytes=len(xml) - 1,
    )
    expansion_result = parse_element_xml(compressed, limits=too_small_expansion)

    assert "source_size_limit_exceeded" in _codes(source_result)
    assert "expanded_size_limit_exceeded" in _codes(expansion_result)
    assert source_result.records == expansion_result.records == ()


def test_path_and_file_like_inputs_report_source_and_record_counts(test_workspace):
    xml = _document(_record())
    source_path = test_workspace / "elements.xml"
    source_path.write_bytes(xml)

    path_result = parse_element_xml(source_path)
    stream_result = parse_element_xml(io.BytesIO(xml))

    assert path_result.source_name == "elements.xml"
    assert str(test_workspace) not in (path_result.source_name or "")
    assert path_result.source_bytes == len(xml)
    assert path_result.element_records == 1
    assert stream_result.source_name is None
    assert stream_result.element_records == 1


def test_unknown_claimed_schema_fails_closed():
    xml = _document(_record())

    caller_claim = parse_element_xml(xml, claimed_schema="fhwa-elements-v2025")
    document_claim = parse_element_xml(
        b"""\
        <BridgeElements xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
          xsi:noNamespaceSchemaLocation="fhwa-elements.xsd">
        </BridgeElements>
        """
    )

    for result in (caller_claim, document_claim):
        assert result.element_file_parsed is False
        assert result.records == ()
        assert "xml_schema_unsupported" in _codes(result)


def _official_xsd_document(*, declared: bool = False, omit_state: bool = False) -> bytes:
    schema_claim = (
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xsi:noNamespaceSchemaLocation="https://www.fhwa.dot.gov/bridge/nbi/140725_a1.xsd"'
        if declared
        else ""
    )
    state = "" if omit_state else "<STATE>34</STATE>"
    return (
        f"<FHWAElement{schema_claim}><FHWAED>"
        f"{state}<STRUCNUM>000123</STRUCNUM><EN>12</EN>"
        "<TOTALQTY>10</TOTALQTY><CS1>1</CS1><CS2>2</CS2>"
        "<CS3>3</CS3><CS4>4</CS4></FHWAED></FHWAElement>"
    ).encode()


@pytest.mark.parametrize("document_declares", [False, True])
def test_pinned_official_element_xsd_is_executed(document_declares: bool):
    result = parse_element_xml(
        _official_xsd_document(declared=document_declares),
        claimed_schema=None if document_declares else LEGACY_ELEMENT_SCHEMA_ID,
    )

    assert result.element_file_parsed is True
    assert result.element_records == 1
    assert result.records[0].bridge_identity.structure_number == "000123"
    assert "xml_schema_validated" in _codes(result)
    assert "xml_schema_validation_error" not in _codes(result)


def test_pinned_official_element_xsd_rejects_invalid_structure_before_projection():
    result = parse_element_xml(
        _official_xsd_document(omit_state=True),
        claimed_schema=LEGACY_ELEMENT_SCHEMA_ID,
    )

    assert result.element_file_parsed is False
    assert result.records == ()
    assert "xml_schema_validation_error" in _codes(result)
    assert any(issue.severity is IssueSeverity.FATAL for issue in result.issues)


def test_schema_claim_must_be_on_document_root():
    xml = (
        '<FHWAElement><FHWAED xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:noNamespaceSchemaLocation="https://www.fhwa.dot.gov/bridge/nbi/140725_a1.xsd">'
        "<STATE>34</STATE><STRUCNUM>1</STRUCNUM><EN>1</EN>"
        "<TOTALQTY>4</TOTALQTY><CS1>1</CS1><CS2>1</CS2>"
        "<CS3>1</CS3><CS4>1</CS4></FHWAED></FHWAElement>"
    ).encode()

    result = parse_element_xml(xml)

    assert result.element_file_parsed is False
    assert "xml_schema_declaration_not_allowed" in _codes(result)


def test_padding_difference_matches_when_the_candidate_is_unique():
    parsed = parse_element_xml(_document(_record(bridge="00000123", state=None)))

    joined = match_element_records(
        parsed,
        [BridgeCandidate(candidate_id="bridge-a", structure_number_raw="123")],
    )

    assert joined.matched == 1
    assert joined.unmatched == 0
    assert joined.ambiguous == 0
    assert joined.matches[0].status is MatchStatus.MATCHED
    assert joined.records_by_candidate["bridge-a"][0].record_index == 1


def test_bridge_number_without_state_is_ambiguous_across_states():
    parsed = parse_element_xml(_document(_record(bridge="000123", state=None)))

    joined = match_element_records(
        parsed,
        [
            BridgeCandidate("nj", "123", state_raw="34"),
            BridgeCandidate("pa", "000123", state_raw="42"),
        ],
    )

    assert joined.matched == 0
    assert joined.unmatched == 0
    assert joined.ambiguous == 1
    assert joined.matches[0].status is MatchStatus.AMBIGUOUS
    assert joined.matches[0].candidate_ids == ("nj", "pa")
    assert "element_bridge_ambiguous" in _codes(joined)


def test_state_and_agency_composite_identity_selects_the_valid_bridge():
    parsed = parse_element_xml(
        _document(_record(bridge="000123", state="034", agency="001"))
    )

    joined = match_element_records(
        parsed,
        [
            BridgeCandidate("wrong-state", "123", state_raw="42", agency_raw="1"),
            BridgeCandidate("wrong-agency", "123", state_raw="34", agency_raw="2"),
            BridgeCandidate("correct", "123", state_raw="34", agency_raw="01"),
        ],
    )

    assert joined.matched == 1
    assert joined.unmatched == 0
    assert joined.ambiguous == 0
    assert joined.matches[0].candidate_ids == ("correct",)
    assert set(joined.records_by_candidate) == {"correct"}


def test_legacy_three_digit_state_region_matches_two_digit_fips():
    parsed = parse_element_xml(
        _document(_record(bridge="000123", state="342", agency="001"))
    )
    joined = match_element_records(
        parsed,
        [BridgeCandidate("nj", "123", state_raw="34", agency_raw="1")],
    )
    assert parsed.records[0].bridge_identity.state == "34"
    assert joined.matched == 1


def test_namespace_mismatch_does_not_spoof_a_known_local_name():
    xml = b"""\
    <BridgeElements xmlns:good="urn:good" xmlns:evil="urn:evil">
      <good:FHWAED>
        <evil:STRUCNUM>123</evil:STRUCNUM>
        <good:EN>12</good:EN>
      </good:FHWAED>
    </BridgeElements>
    """

    result = parse_element_xml(xml)

    assert result.element_file_parsed is True
    assert result.records[0].fields["STRUCNUM"].presence is FieldPresence.MISSING
    assert "field_namespace_mismatch" in _codes(result)
    assert "required_field_missing" in _codes(result)
