from __future__ import annotations

import csv
import io

import pytest

from app.routers.services.reverse_mapper import (
    bridges_to_nbi_csv,
    convert_snbi_to_outputs,
    snbi_to_nbi_row,
)
from app.services.conversion.legacy_projection import legacy_state_code


def _bridge(**overrides):
    bridge = {
        "BL01": 34,
        "BID01": "TEST-001",
        "Features": [],
        "Inspections": [],
        "PostingStatuses": [],
    }
    bridge.update(overrides)
    return bridge


def _manifest_outcomes(result):
    return result["loss_manifest"]["bridges"][0]["outcomes"]


def test_no_transition_fields_do_not_populate_unrelated_legacy_items():
    bridge = _bridge(
        BL12="Some Metropolitan Planning Organization",
        Inspections=[
            {
                "BIE01": "1",
                "BIE02": "20260313",
                "BIE05": 24,
                "BIE11": "Inspect the expansion joint again next cycle.",
            }
        ],
    )

    row = snbi_to_nbi_row(bridge)

    assert row["BASE_HWY_NETWORK_012"] == ""
    assert row["WORK_PROPOSED_075A"] == ""
    # Legitimate inspection fields still project.
    assert row["DATE_OF_INSPECT_090"] == "0326"
    assert row["INSPECT_FREQ_MONTHS_091"] == "24"

    outcomes = _manifest_outcomes(convert_snbi_to_outputs([bridge]))
    omitted = {
        item["source_path"]: item for item in outcomes
        if item["classification"] == "omitted_because_no_legacy_representation"
    }
    assert omitted["/BL12"]["legacy_field"] is None
    assert omitted["/Inspections/0/BIE11"]["legacy_field"] is None
    assert "Item 75A" in omitted["/Inspections/0/BIE11"]["message"]


def test_bh06_uses_the_legacy_ten_plus_two_character_boundary():
    bridge = _bridge(
        Features=[
            {
                "BF01": "H01",
                "BF02": "C",
                "BH06": "ABCDEFGHIJ12",
            }
        ]
    )

    row = snbi_to_nbi_row(bridge)

    assert row["LRS_INV_ROUTE_013A"] == "ABCDEFGHIJ"
    assert row["SUBROUTE_NO_013B"] == "12"

    outcomes = _manifest_outcomes(convert_snbi_to_outputs([bridge]))
    lrs = next(item for item in outcomes if item["source_field"] == "BH06")
    assert lrs["classification"] == "representative_inverse_chosen"
    assert "10+2" in lrs["message"]


def test_bh06_overflow_is_bounded_and_reported_as_structural_loss():
    bridge = _bridge(
        Features=[
            {
                "BF01": "H01",
                "BF02": "C",
                "BH06": "ABCDEFGHIJ12EXTRA",
            }
        ]
    )

    row = snbi_to_nbi_row(bridge)

    assert row["LRS_INV_ROUTE_013A"] == "ABCDEFGHIJ"
    assert row["SUBROUTE_NO_013B"] == "12"
    outcomes = _manifest_outcomes(convert_snbi_to_outputs([bridge]))
    lrs = next(item for item in outcomes if item["source_field"] == "BH06")
    assert lrs["classification"] == "structurally_collapsed"
    assert "truncated" in lrs["message"]


@pytest.mark.parametrize(
    ("snbi_value", "legacy_value", "classification"),
    [
        ("36", "362", "representative_inverse_chosen"),
        (36, "362", "representative_inverse_chosen"),
        ("CA", "CAN", "exact"),
        ("MX", "MEX", "exact"),
    ],
)
def test_bl08_inverse_restores_country_or_pinned_state_region_code(
    snbi_value, legacy_value, classification
):
    decision = legacy_state_code(snbi_value, allow_country=True)
    row = snbi_to_nbi_row(_bridge(BL08=snbi_value))

    assert decision.value == legacy_value
    assert decision.classification == classification
    assert row["OTHER_STATE_CODE_098A"] == legacy_value


@pytest.mark.parametrize("invalid", ["362", "ZZ", "99", "USA"])
def test_bl08_does_not_copy_unsupported_or_already_legacy_shaped_values(invalid):
    decision = legacy_state_code(invalid, allow_country=True)
    bridge = _bridge(BL08=invalid)

    assert decision.value is None
    assert decision.classification == "omitted_because_no_legacy_representation"
    assert snbi_to_nbi_row(bridge)["OTHER_STATE_CODE_098A"] == ""
    border = next(
        item for item in _manifest_outcomes(convert_snbi_to_outputs([bridge]))
        if item["source_field"] == "BL08"
    )
    assert border["value_written"] is None
    assert border["classification"] == "omitted_because_no_legacy_representation"


def test_state_code_uses_pinned_legacy_item_1_region_table():
    assert snbi_to_nbi_row(_bridge(BL01=34))["STATE_CODE_001"] == "342"
    assert snbi_to_nbi_row(_bridge(BL01=2))["STATE_CODE_001"] == "020"


def test_projection_is_labeled_headered_csv_with_one_type_1_row_per_bridge():
    bridges = [_bridge(BID01="A"), _bridge(BID01="B")]

    result = convert_snbi_to_outputs(bridges)
    rows = list(csv.DictReader(io.StringIO(result["nbi_csv"])))

    assert len(rows) == 2
    assert [row["RECORD_TYPE_005A"] for row in rows] == ["1", "1"]
    assert result["legacy_projection"] == {
        "kind": "legacy_projection_for_review",
        "format": "bridgeguard-legacy-shaped-headered-csv-v1",
        "label": (
            "BridgeGuard legacy-shaped CSV projection for review; not an "
            "original NBI source file or FHWA 445-character submission file"
        ),
        "media_type": "text/csv",
        "suggested_extension": ".csv",
        "official_nbi_445": False,
        "bridge_count": 2,
        "legacy_row_count": 2,
        "row_policy": "exactly_one_synthetic_record_type_1_row_per_snbi_bridge",
    }
    assert result["loss_manifest"]["exact_reverse"] is False
    assert result["loss_manifest"]["legacy_row_count"] == 2
    assert all(
        item["record_types"] == ["1"]
        for item in result["loss_manifest"]["bridges"]
    )
    # Existing callers retain the original payload keys/functions.
    assert bridges_to_nbi_csv(bridges) == result["nbi_csv"]
    assert {"flat_json", "text", "nbi_csv", "elements_xml", "count"} <= result.keys()


def test_nested_posting_status_is_inverse_mapped_and_date_is_not_item_90():
    row = snbi_to_nbi_row(
        _bridge(PostingStatuses=[{"BPS01": "PO", "BPS02": "20260201"}])
    )

    assert row["OPEN_CLOSED_POSTED_041"] == "A"
    assert row["DATE_OF_INSPECT_090"] == ""


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("=1+1", "'=1+1"),
        ("  +SUM(1,2)", "'  +SUM(1,2)"),
        ("\t-2+3", "'\t-2+3"),
        ("\n@IMPORTXML(x)", "'\n@IMPORTXML(x)"),
        ('=HYPERLINK("https://invalid.example","deck, joint")\r\nnext',
         '\'=HYPERLINK("https://invalid.example","deck, joint")\r\nnext'),
        ("bridge-42", "bridge-42"),
    ],
)
def test_reverse_projection_csv_neutralizes_formula_text_without_data_loss(
    location, expected
):
    content = bridges_to_nbi_csv([_bridge(BL11=location)])

    assert content.endswith("\r\n")
    rows = list(csv.DictReader(io.StringIO(content, newline="")))
    assert len(rows) == 1
    assert rows[0]["LOCATION_009"] == expected
