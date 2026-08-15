"""Metadata and loss accounting for synthetic SNBI-to-NBI projections.

An SNBI document is richer than one legacy NBI record, so projecting it to a
headered CSV is not an exact reverse conversion.  This module keeps the policy
decisions out of the serializer and makes every known non-invertible decision
machine-readable.

The legacy state codes below come from FHWA-PD-96-001, *Recording and Coding
Guide for the Structure Inventory and Appraisal of the Nation's Bridges*,
Item 1.  Item 98A explicitly reuses those three-character state codes and uses
``CAN``/``MEX`` for the two international-border cases.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence


LEGACY_PROJECTION_FORMAT = "bridgeguard-legacy-shaped-headered-csv-v1"
LEGACY_PROJECTION_LABEL = (
    "BridgeGuard legacy-shaped CSV projection for review; not an original "
    "NBI source file or FHWA 445-character submission file"
)


# FIPS state/territory code -> the three-character legacy NBI Item 1 code.
# The final character is the historical FHWA region digit (Region 10 is 0).
LEGACY_NBI_STATE_CODES: Dict[str, str] = {
    "01": "014",  # Alabama
    "02": "020",  # Alaska
    "04": "049",  # Arizona
    "05": "056",  # Arkansas
    "06": "069",  # California
    "08": "088",  # Colorado
    "09": "091",  # Connecticut
    "10": "103",  # Delaware
    "11": "113",  # District of Columbia
    "12": "124",  # Florida
    "13": "134",  # Georgia
    "15": "159",  # Hawaii
    "16": "160",  # Idaho
    "17": "175",  # Illinois
    "18": "185",  # Indiana
    "19": "197",  # Iowa
    "20": "207",  # Kansas
    "21": "214",  # Kentucky
    "22": "226",  # Louisiana
    "23": "231",  # Maine
    "24": "243",  # Maryland
    "25": "251",  # Massachusetts
    "26": "265",  # Michigan
    "27": "275",  # Minnesota
    "28": "284",  # Mississippi
    "29": "297",  # Missouri
    "30": "308",  # Montana
    "31": "317",  # Nebraska
    "32": "329",  # Nevada
    "33": "331",  # New Hampshire
    "34": "342",  # New Jersey (retains region code 2)
    "35": "356",  # New Mexico
    "36": "362",  # New York (retains region code 2)
    "37": "374",  # North Carolina
    "38": "388",  # North Dakota
    "39": "395",  # Ohio
    "40": "406",  # Oklahoma
    "41": "410",  # Oregon
    "42": "423",  # Pennsylvania
    "44": "441",  # Rhode Island
    "45": "454",  # South Carolina
    "46": "468",  # South Dakota
    "47": "474",  # Tennessee
    "48": "486",  # Texas
    "49": "498",  # Utah
    "50": "501",  # Vermont
    "51": "513",  # Virginia
    "53": "530",  # Washington
    "54": "543",  # West Virginia
    "55": "555",  # Wisconsin
    "56": "568",  # Wyoming
    "72": "721",  # Puerto Rico
}


@dataclass(frozen=True)
class InverseDecision:
    """One explicit projection decision for a legacy target field."""

    value: Optional[str]
    classification: str
    rule_id: str
    message: str

    def as_manifest_entry(
        self,
        *,
        source_path: str,
        source_field: str,
        legacy_field: Optional[str],
    ) -> Dict[str, Any]:
        return {
            "source_path": source_path,
            "source_field": source_field,
            "legacy_field": legacy_field,
            "classification": self.classification,
            "value_written": self.value,
            "rule_id": self.rule_id,
            "message": self.message,
        }


def legacy_state_code(value: Any, *, allow_country: bool) -> InverseDecision:
    """Reconstruct an Item 1/98A legacy code without copying invalid shapes.

    Numeric SNBI values contain only the two-digit FIPS component.  The region
    character is therefore reconstructed from FHWA's Item 1 table and is
    reported as a representative inverse rather than silently treated as
    source-preserved data.  B.L.08's ``CA`` and ``MX`` values are the explicit
    SNBI forms of the legacy ``CAN`` and ``MEX`` values.
    """

    raw = str(value if value is not None else "").strip().upper()
    target = "Item 98A" if allow_country else "Item 1"
    if not raw:
        return InverseDecision(
            None,
            "unavailable_due_missing_provenance",
            "legacy-state-region-inverse-v1",
            f"{target} was left blank because the SNBI value is absent.",
        )

    if allow_country and raw in {"CA", "MX"}:
        legacy = {"CA": "CAN", "MX": "MEX"}[raw]
        return InverseDecision(
            legacy,
            "exact",
            "snbi-border-country-inverse-v1",
            f"SNBI {raw} was restored to the legacy country code {legacy}.",
        )

    # Leading zeroes are formatting only for a valid numeric FIPS code.  A
    # three-character value is not accepted: that would conceal malformed
    # SNBI by passing an already-legacy-shaped value straight through.
    if not raw.isdigit() or len(raw) > 2:
        return InverseDecision(
            None,
            "omitted_because_no_legacy_representation",
            "legacy-state-region-inverse-v1",
            f"{target} was omitted: {raw!r} is not a supported SNBI FIPS/country code.",
        )

    fips = raw.zfill(2)
    legacy = LEGACY_NBI_STATE_CODES.get(fips)
    if legacy is None:
        return InverseDecision(
            None,
            "omitted_because_no_legacy_representation",
            "legacy-state-region-inverse-v1",
            f"{target} was omitted: no pinned legacy Item 1 code exists for FIPS {fips}.",
        )
    return InverseDecision(
        legacy,
        "representative_inverse_chosen",
        "legacy-state-region-inverse-v1",
        (
            f"FIPS {fips} was expanded to {legacy} using FHWA's legacy Item 1 "
            "state/region table; the region digit was not retained in SNBI."
        ),
    )


def _present(value: Any) -> bool:
    return value is not None and value != ""


def _omitted(
    *, source_path: str, source_field: str, message: str
) -> Dict[str, Any]:
    return InverseDecision(
        None,
        "omitted_because_no_legacy_representation",
        "snbi-no-legacy-transition-v1",
        message,
    ).as_manifest_entry(
        source_path=source_path,
        source_field=source_field,
        legacy_field=None,
    )


def build_legacy_projection_manifest(
    bridges: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, str]],
) -> Dict[str, Any]:
    """Describe known loss/inference for a one-row-per-bridge projection."""

    if len(bridges) != len(rows):
        raise ValueError("projection manifest requires one legacy row per SNBI bridge")

    bridge_manifests = []
    counts: Counter[str] = Counter()
    for bridge_index, (bridge, row) in enumerate(zip(bridges, rows)):
        outcomes: list[Dict[str, Any]] = []

        # A synthetic projection always collapses the repeated SNBI resource
        # model into exactly one type-1 row.  Exact Type 2/A recovery requires
        # the immutable original source records, not inference from SNBI.
        outcomes.append(
            InverseDecision(
                "1",
                "structurally_collapsed",
                "one-type-1-row-projection-v1",
                (
                    "Repeated features/routes are collapsed to one synthetic "
                    "record-type-1 row; no Type 2/A source rows are asserted."
                ),
            ).as_manifest_entry(
                source_path="/Features",
                source_field="Features",
                legacy_field="RECORD_TYPE_005A",
            )
        )

        if _present(bridge.get("BL01")):
            state = legacy_state_code(bridge.get("BL01"), allow_country=False)
            outcomes.append(
                state.as_manifest_entry(
                    source_path="/BL01",
                    source_field="BL01",
                    legacy_field="STATE_CODE_001",
                )
            )

        if _present(bridge.get("BL12")):
            outcomes.append(
                _omitted(
                    source_path="/BL12",
                    source_field="BL12",
                    message=(
                        "B.L.12 (Metropolitan Planning Organization) has no "
                        "legacy NBI transition; Item 12 was not populated."
                    ),
                )
            )

        if _present(bridge.get("BL08")):
            border = legacy_state_code(bridge.get("BL08"), allow_country=True)
            outcomes.append(
                border.as_manifest_entry(
                    source_path="/BL08",
                    source_field="BL08",
                    legacy_field="OTHER_STATE_CODE_098A",
                )
            )

        inspections = bridge.get("Inspections")
        if isinstance(inspections, list):
            for inspection_index, inspection in enumerate(inspections):
                if isinstance(inspection, Mapping) and _present(inspection.get("BIE11")):
                    outcomes.append(
                        _omitted(
                            source_path=f"/Inspections/{inspection_index}/BIE11",
                            source_field="BIE11",
                            message=(
                                "B.IE.11 is an inspection note and has no valid "
                                "transition to legacy Item 75A (Work Proposed)."
                            ),
                        )
                    )

        posting_statuses = bridge.get("PostingStatuses")
        if isinstance(posting_statuses, list):
            for posting_index, posting in enumerate(posting_statuses):
                if isinstance(posting, Mapping) and _present(posting.get("BPS02")):
                    outcomes.append(
                        _omitted(
                            source_path=f"/PostingStatuses/{posting_index}/BPS02",
                            source_field="BPS02",
                            message=(
                                "B.PS.02 is a posting-status date and was not "
                                "misrepresented as legacy Item 90 inspection date."
                            ),
                        )
                    )

        features = bridge.get("Features")
        carried_highway_seen = False
        if isinstance(features, list):
            for feature_index, feature in enumerate(features):
                if not isinstance(feature, Mapping) or not _present(feature.get("BH06")):
                    continue
                is_selected = (
                    str(feature.get("BF02") or "") == "C"
                    and str(feature.get("BF01") or "").upper().startswith("H")
                    and not carried_highway_seen
                )
                if is_selected:
                    carried_highway_seen = True
                raw_lrs = str(feature.get("BH06") or "")
                classification = (
                    "structurally_collapsed"
                    if not is_selected or len(raw_lrs) > 12
                    else "representative_inverse_chosen"
                )
                message = (
                    "Only the first carried-highway feature can populate the "
                    "single legacy row."
                    if not is_selected
                    else (
                        "BH06 was truncated to the legacy 10+2 Item 13A/13B limit."
                        if len(raw_lrs) > 12
                        else (
                            "BH06 was divided using the legacy 10+2 boundary; "
                            "source padding/leading-zero provenance is unavailable."
                        )
                    )
                )
                outcomes.append(
                    InverseDecision(
                        raw_lrs[:12] if is_selected else None,
                        classification,
                        "snbi-bh06-to-nbi-item13-10-plus-2-v1",
                        message,
                    ).as_manifest_entry(
                        source_path=f"/Features/{feature_index}/BH06",
                        source_field="BH06",
                        legacy_field=(
                            "LRS_INV_ROUTE_013A+SUBROUTE_NO_013B"
                            if is_selected
                            else None
                        ),
                    )
                )

        for outcome in outcomes:
            counts[outcome["classification"]] += 1
        bridge_manifests.append(
            {
                "bridge_index": bridge_index,
                "bridge_number": str(bridge.get("BID01") or ""),
                "legacy_row_count": 1,
                "record_types": [str(row.get("RECORD_TYPE_005A") or "")],
                "outcomes": outcomes,
            }
        )

    return {
        "manifest_version": "bridgeguard-legacy-projection-loss-v1",
        "projection_format": LEGACY_PROJECTION_FORMAT,
        "label": LEGACY_PROJECTION_LABEL,
        "exact_reverse": False,
        "bridge_count": len(bridges),
        "legacy_row_count": len(rows),
        "row_policy": "exactly_one_synthetic_record_type_1_row_per_snbi_bridge",
        "classification_counts": dict(sorted(counts.items())),
        "bridges": bridge_manifests,
    }


def projection_metadata(*, bridge_count: int) -> Dict[str, Any]:
    """Stable metadata consumed by APIs/UIs without changing CSV contents."""

    return {
        "kind": "legacy_projection_for_review",
        "format": LEGACY_PROJECTION_FORMAT,
        "label": LEGACY_PROJECTION_LABEL,
        "media_type": "text/csv",
        "suggested_extension": ".csv",
        "official_nbi_445": False,
        "bridge_count": bridge_count,
        "legacy_row_count": bridge_count,
        "row_policy": "exactly_one_synthetic_record_type_1_row_per_snbi_bridge",
    }


__all__ = [
    "InverseDecision",
    "LEGACY_NBI_STATE_CODES",
    "LEGACY_PROJECTION_FORMAT",
    "LEGACY_PROJECTION_LABEL",
    "build_legacy_projection_manifest",
    "legacy_state_code",
    "projection_metadata",
]
