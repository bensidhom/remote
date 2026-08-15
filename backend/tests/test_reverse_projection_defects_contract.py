"""Focused red-first regressions for known SNBI-to-NBI projection defects.

These assertions exercise the existing low-level projector independently of
the PR6 export facade.  A best-effort projection may remain lossy, but it must
not invent transitions that FHWA explicitly says do not exist, and its few
invertible formatting rules must be correct.
"""

from __future__ import annotations

import pytest

from app.routers.services.reverse_mapper import snbi_to_nbi_row


def test_bl12_has_no_item_12_transition_and_is_not_projected():
    row = snbi_to_nbi_row({"BL12": "MPO-123"})

    assert row["BASE_HWY_NETWORK_012"] == ""


def test_bie11_has_no_item_75a_transition_and_is_not_projected():
    row = snbi_to_nbi_row(
        {"Inspections": [{"BIE01": "1", "BIE11": "inspection note"}]}
    )

    assert row["WORK_PROPOSED_075A"] == ""


def test_bh06_restores_the_legacy_item_13a_13b_ten_plus_two_split():
    row = snbi_to_nbi_row(
        {
            "Features": [
                {"BF01": "H01", "BF02": "C", "BH06": "0123456789AB"}
            ]
        }
    )

    assert row["LRS_INV_ROUTE_013A"] == "0123456789"
    assert row["SUBROUTE_NO_013B"] == "AB"


@pytest.mark.parametrize(
    ("snbi_border", "legacy_item_98a"),
    (("CA", "CAN"), ("MX", "MEX"), ("36", "362")),
)
def test_bl08_restores_country_tokens_or_the_registered_region_digit(
    snbi_border: str,
    legacy_item_98a: str,
):
    row = snbi_to_nbi_row({"BL08": snbi_border})

    assert row["OTHER_STATE_CODE_098A"] == legacy_item_98a

