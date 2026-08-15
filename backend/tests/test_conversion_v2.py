from __future__ import annotations

import asyncio
import hashlib
import io

import pytest
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.testclient import TestClient

from app.main import convert_v2
from app.services.conversion.element_xml import LEGACY_ELEMENT_SCHEMA_ID
from app.services.conversion import pipeline as conversion_pipeline


def _fixed_record(
    *, state: str = "342", structure: str = "TEST-BRIDGE", record_type: str = "1"
) -> bytes:
    record = bytearray(b" " * 445)
    record[0:3] = state.encode("ascii")
    record[3:18] = structure.encode("ascii").ljust(15, b" ")
    record[18:19] = record_type.encode("ascii")
    record[156:160] = b"1999"
    record[199:200] = b"1"
    record[222:228] = b" 010.0"
    record[373:374] = b"Y"
    return bytes(record)


def _upload(name: str, body: bytes) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(body))


def _run(nbi: bytes, **overrides):
    options = {
        "txt_file": _upload("bridges.txt", nbi),
        "xml_file": None,
        "source_agency_context": "NJDOT",
        "encoding": "ascii",
        "input_format": "fhwa_fixed_445",
        "issue_policy": "reject",
        "csv_delimiter": None,
        "csv_quotechar": None,
        "csv_escapechar": None,
        "conversion_profile": "research_historical_import",
        "bid01_policy": "preserve",
        "claimed_element_schema": None,
    }
    options.update(overrides)
    return asyncio.run(convert_v2(**options))


def _resolve_pointer(document, pointer: str):
    current = document
    for raw_token in pointer.lstrip("/").split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        current = current[int(token)] if isinstance(current, list) else current[token]
    return current


def test_strict_endpoint_preserves_source_artifact_and_legacy_response_shape():
    source = _fixed_record()
    result = _run(source)

    assert result["status"] == "success"
    assert result["engine"] == "strict-ingestion-canonical-validation-v3"
    assert result["submission_ready"] is False
    assert result["converted"] == 1
    assert result["dropped_non_nj"] == 0
    assert result["parser"]["bridge_groups"] == 1
    assert result["sources"]["nbi"]["sha256"] == hashlib.sha256(source).hexdigest()
    bridge = result["bridges"][0]
    assert bridge["BL01"] == 34
    assert bridge["BID01"] == "TEST-BRIDGE"
    assert bridge["Features"][0]["BF01"] == "H01"
    assert bridge["_SourceData"]["over_record"]["raw_item_001"] == "342"
    assert bridge["_SourceData"]["artifact"]["artifact_id"]
    assert bridge["_TransitionLineage"]["/BID01"]["rule_id"] == "agency-selected-bid01-policy-v1"
    assert bridge["_TransitionCoverage"]["uncovered_leaf_count"] == 0
    for pointer in bridge["_TransitionLineage"]:
        _resolve_pointer(bridge, pointer)
    assert bridge["_SourceData"]["eligibility"]["has_federally_eligible_record"] is True
    assert "parity_errors" in result  # temporary frontend compatibility
    assert result["compatibility"]["parity_fields_deprecated"] is True


def test_multipart_http_contract_requires_and_accepts_explicit_parser_metadata():
    http_app = FastAPI()
    http_app.post("/api/convert-v2")(convert_v2)
    with TestClient(http_app) as client:
        response = client.post(
            "/api/convert-v2",
            files={"txt_file": ("bridges.txt", _fixed_record(), "text/plain")},
            data={
                "source_agency_context": "NJDOT",
                "encoding": "ascii",
                "input_format": "fhwa_fixed_445",
                "issue_policy": "reject",
                "conversion_profile": "research_historical_import",
                "bid01_policy": "preserve",
            },
        )
    assert response.status_code == 200
    assert response.json()["parser"]["format"] == "fhwa_fixed_445"


def test_multipart_http_contract_accepts_fhwa_download_csv_profile():
    source = (
        b"STATE_CODE_001,STRUCTURE_NUMBER_008,RECORD_TYPE_005A,"
        b"YEAR_BUILT_027,SERVICE_ON_042A,STRUCTURE_LEN_MT_049,"
        b"BRIDGE_LEN_IND_112,DATE_LAST_UPDATE\n"
        b"34,0101150,1,1955,1,10.0,Y,20250331\n"
    )
    result = _run(
        source,
        input_format="fhwa_download_csv",
        csv_delimiter=",",
        csv_quotechar='"',
    )

    assert result["parser"]["format"] == "fhwa_download_csv"
    assert result["converted"] == 1
    bridge = result["bridges"][0]
    assert bridge["BL01"] == 34
    assert bridge["BID01"] == "0101150"
    assert bridge["_SourceData"]["over_record"]["raw_item_001"] == "34"


def test_multipart_http_contract_does_not_default_conversion_profile():
    http_app = FastAPI()
    http_app.post("/api/convert-v2")(convert_v2)
    with TestClient(http_app) as client:
        response = client.post(
            "/api/convert-v2",
            files={"txt_file": ("bridges.txt", _fixed_record(), "text/plain")},
            data={
                "source_agency_context": "NJDOT",
                "encoding": "ascii",
                "input_format": "fhwa_fixed_445",
                "issue_policy": "reject",
                "bid01_policy": "preserve",
            },
        )

    assert response.status_code == 422


def test_all_state_duplicate_item8_is_separated_before_workspace_filter():
    source = _fixed_record(state="342", structure="SAME")
    source += b"\n" + _fixed_record(state="422", structure="SAME")
    result = _run(source)

    assert result["parser"]["bridge_groups"] == 2
    assert result["converted"] == 1
    assert result["dropped_disallowed_state"] == 1
    assert result["bridges"][0]["BL01"] == 34


def test_bid01_projection_is_independent_of_under_before_over_source_order():
    source = _fixed_record(record_type="2")
    source += b"\n" + _fixed_record(record_type="1")

    result = _run(source)

    assert result["converted"] == 1
    assert result["skipped"] == 0
    assert result["bridges"][0]["BID01"] == "TEST-BRIDGE"


def test_transition_engine_exception_rejects_the_whole_conversion(monkeypatch):
    def reject_bridge(*args, **kwargs):
        raise RuntimeError("forced mapper failure")

    monkeypatch.setattr(
        conversion_pipeline,
        "map_bridge_with_lineage",
        reject_bridge,
    )

    with pytest.raises(HTTPException) as raised:
        _run(_fixed_record())

    assert raised.value.status_code == 422
    assert raised.value.detail["code"] == "transition_engine_error"


def test_element_xml_is_strictly_matched_and_attached():
    xml = b"""<?xml version="1.0"?>
    <BridgeElements>
      <FHWAED><STRUCNUM>TEST-BRIDGE</STRUCNUM><STATE>342</STATE>
        <AGENCY>NJDOT</AGENCY><EN>12</EN><EPN>0</EPN><TOTALQTY>10</TOTALQTY>
        <CS1>1</CS1><CS2>2</CS2><CS3>3</CS3><CS4>4</CS4>
      </FHWAED>
    </BridgeElements>"""
    result = _run(
        _fixed_record(),
        xml_file=_upload("elements.xml", xml),
    )

    assert result["elements_from_xml"] is True
    assert result["elements"]["matched"] == 1
    assert result["elements"]["ambiguous"] == 0
    assert result["bridges"][0]["Elements"][0]["BE01"] == "12"
    assert result["sources"]["elements"]["sha256"] == hashlib.sha256(xml).hexdigest()
    bridge = result["bridges"][0]
    expected_element_source_id = f"element:{hashlib.sha256(xml).hexdigest()}:1"
    assert bridge["_TransitionLineage"]["/Elements/0/BE01"][
        "source_record_ids"
    ] == [expected_element_source_id]
    assert expected_element_source_id != bridge["_SourceData"]["over_record"][
        "source_record_id"
    ]
    reference_ids = {
        value["source_artifact_id"]
        for value in bridge["_TransitionLineage"]["/Elements/0/BE01"][
            "rule_source_references"
        ]
    }
    assert "nbi_snbi_crosswalk_dec_2023" in reference_ids
    assert "legacy_element_xsd_v1_2013" not in reference_ids


def test_parse_failure_is_422_and_returns_immutable_source_receipt():
    with pytest.raises(HTTPException) as raised:
        _run(b"too short")
    assert raised.value.status_code == 422
    assert raised.value.detail["code"] == "nbi_parse_rejected"
    assert raised.value.detail["issue"]["code"] == "fixed_record_length"
    assert raised.value.detail["source"]["artifact_id"]


def test_quarantine_policy_returns_stable_bad_record_reference():
    result = _run(
        _fixed_record() + b"\nshort",
        issue_policy="quarantine",
    )
    assert result["converted"] == 1
    assert result["parser"]["records_quarantined"] == 1
    quarantined = result["parser"]["quarantined_records"][0]
    assert len(quarantined["source_record_id"]) == 64
    assert quarantined["issue_codes"] == ["fixed_record_length"]


def test_bid01_policy_is_required_and_collision_checked():
    with pytest.raises(HTTPException) as missing:
        _run(_fixed_record(), bid01_policy=None)
    assert missing.value.status_code == 400
    assert missing.value.detail["code"] == "bid01_policy_required"

    source = _fixed_record(structure="0001")
    source += b"\n" + _fixed_record(structure="001")
    with pytest.raises(HTTPException) as collision:
        _run(source, bid01_policy="trim_all_leading_zeros")
    assert collision.value.status_code == 422
    assert collision.value.detail["code"] == "bid01_policy_conflict"


def test_bid01_selected_policy_changes_output_but_keeps_raw_provenance():
    result = _run(
        _fixed_record(structure="000123"),
        bid01_policy="trim_all_but_one",
    )
    bridge = result["bridges"][0]
    assert bridge["BID01"] == "0123"
    assert bridge["_SourceData"]["over_record"]["raw_item_008"].strip() == "000123"
    assert result["bid01_preview"]["changed_count"] == 1


def test_unimplemented_submission_profile_fails_closed():
    with pytest.raises(HTTPException) as raised:
        _run(_fixed_record(), conversion_profile="submission_2026_v04_v1_1")
    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "conversion_profile_not_implemented"
    assert raised.value.detail["sources"]["nbi"]["artifact_id"]


def test_claimed_element_schema_fails_closed():
    xml = b"<BridgeElements/>"
    with pytest.raises(HTTPException) as raised:
        _run(
            _fixed_record(),
            xml_file=_upload("elements.xml", xml),
            claimed_element_schema="fhwa-elements-v1",
        )
    assert raised.value.status_code == 422
    assert raised.value.detail["code"] == "element_xml_rejected"
    assert (
        raised.value.detail["details"]["issues"][0]["code"]
        == "xml_schema_unsupported"
    )


def test_claimed_official_element_schema_is_executed_before_conversion():
    xml = b"""<?xml version="1.0"?>
    <FHWAElement><FHWAED>
      <STATE>34</STATE><STRUCNUM>TEST-BRIDGE</STRUCNUM><EN>12</EN>
      <TOTALQTY>10</TOTALQTY><CS1>1</CS1><CS2>2</CS2><CS3>3</CS3><CS4>4</CS4>
    </FHWAED></FHWAElement>"""

    result = _run(
        _fixed_record(),
        xml_file=_upload("elements.xml", xml),
        claimed_element_schema=LEGACY_ELEMENT_SCHEMA_ID,
    )

    assert result["converted"] == 1
    assert result["bridges"][0]["Elements"][0]["BE01"] == "12"
    assert "xml_schema_validated" in {
        issue["code"] for issue in result["elements"]["issues"]
    }
    lineage = result["bridges"][0]["_TransitionLineage"]["/Elements/0/BE01"]
    references = {
        reference["source_artifact_id"]: reference
        for reference in lineage["rule_source_references"]
    }
    schema = references["legacy_element_xsd_v1_2013"]
    assert schema["source_artifact_sha256"] == (
        "193cda71b64933e2d5316e7d193d6f780c9479a875ea3ebcb21dcaee36f0a95f"
    )
    assert schema["source_locator"].endswith("#line=46")
    assert schema["field_locator"].endswith("xs:element[@name='EN']")
    assert schema["reference_kind"] == "xml_schema_validation"
    assert schema["execution_verified"] is True
    assert schema["authority_status"] == (
        "bridgeguard_xsd_execution_verified_not_official_validation"
    )
    crosswalk = references["nbi_snbi_crosswalk_dec_2023"]
    assert crosswalk["field_locator"] == "'Crosswalk for Over Records'!B153"
    assert crosswalk["execution_verified"] is False
