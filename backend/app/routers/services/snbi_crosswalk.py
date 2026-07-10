"""Conservative NBI-to-SNBI value crosswalk helpers.

These helpers intentionally return ``None`` when a legacy NBI value cannot be
mapped to an SNBI code with high confidence. That is safer than carrying old
NBI codes into SNBI fields where they fail FHWA code-list validation.
"""

from typing import Any, Optional

from .snbi_rules import (
    BRIDGE_RAILING_CODES,
    DECK_MATERIAL_CODES,
    DECK_MEMBRANE_CODES,
    DECK_REINFORCING_CODES,
    DESIGN_LOAD_CODES,
    FEDERAL_LAND_CODES,
    FUNCTIONAL_CLASS_CODES,
    HISTORIC_CODES,
    LOAD_RATING_METHOD_CODES,
    NAVIGATION_CODES,
    NAVIGATION_PROTECTION_CODES,
    NHFN_CODES,
    NHS_CODES,
    OWNER_MAINTENANCE_CODES,
    POSTING_STATUS_CODES,
    ROUTE_DIRECTION_CODES,
    ROUTE_SUFFIX_CODES,
    ROUTE_TYPE_CODES,
    SCOUR_CONDITION_CODES,
    SCOUR_VULNERABILITY_CODES,
    SERVICE_TYPE_CODES,
    SPAN_MATERIAL_CODES,
    SPAN_TYPE_CODES,
    STRAHNET_CODES,
    TOLL_CODES,
    URBAN_CODE_CODES,
    WEARING_SURFACE_CODES,
    clean_code,
)


def _clean(value: Any) -> str:
    return str(value or "").strip().upper()


def blank_if_unknown(value: Any) -> Optional[str]:
    text = _clean(value)
    if not text or text in {"*", "N/A", "NA"}:
        return None
    return text


def map_lookup(value: Any, table: dict[str, Optional[str]], width: Optional[int] = None) -> Optional[str]:
    text = clean_code(value, width=width)
    if not text:
        return None
    return table.get(text)


def normalize_snbi_date(value: Any) -> Optional[str]:
    """Normalize CG MMYY/MMDDYYYY-style dates to SNBI YYYYMMDD."""
    raw = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(raw) == 3:
        raw = "0" + raw
    if len(raw) == 4:
        mm = raw[:2]
        yy = raw[2:]
        if not (1 <= int(mm) <= 12):
            return None
        century = "20" if int(yy) < 80 else "19"
        return f"{century}{yy}{mm}01"
    if len(raw) == 8:
        first4 = int(raw[:4])
        if 1900 <= first4 <= 2100:
            yyyy, mm, dd = raw[:4], raw[4:6], raw[6:8]
        else:
            mm, dd, yyyy = raw[:2], raw[2:4], raw[4:8]
        if not (1 <= int(mm) <= 12 and 1 <= int(dd) <= 31):
            return None
        return f"{yyyy}{mm}{dd}"
    return None


def yes_no_from_0_1(value: Any) -> Optional[str]:
    text = _clean(value)
    if text in {"0", "N", "NO"}:
        return "N"
    if text in {"1", "Y", "YES"}:
        return "Y"
    return None


def route_type_from_nbi_prefix(value: Any) -> Optional[str]:
    """NBI route prefix to SNBI B.RT.04 when meanings align."""
    return map_lookup(value, ROUTE_TYPE_CODES)


def route_service_from_nbi_service_level(value: Any) -> Optional[str]:
    """NBI route service level to SNBI B.RT.05 when meanings align."""
    return map_lookup(value, SERVICE_TYPE_CODES)


def inspection_type_from_legacy(value: Any) -> Optional[str]:
    """Map common legacy inspection markers to SNBI B.IE.01 codes.

    Per FHWA: 1=Initial, 2=Routine, 3=Underwater, 4=NSTM, 7=Special.
    """
    text = _clean(value)
    if text in {"I", "INITIAL", "1"}:
        return "1"
    if text in {"R", "ROUTINE", "2"}:
        return "2"
    if text in {"U", "UNDERWATER", "3"}:
        return "3"
    if text in {"F", "FRACTURE", "NSTM", "4"}:
        return "4"
    if text in {"S", "SPECIAL", "7"}:
        return "7"
    return None


def approach_alignment_from_legacy_eval(value: Any) -> Optional[str]:
    """NBI Item 72 to SNBI B.AP.01: 7-9 -> G, 5-6 -> F, 0-4 -> P."""
    text = _clean(value)
    if not text or not text[0].isdigit():
        return None
    rating = int(text[0])
    if rating >= 7:
        return "G"
    if rating >= 5:
        return "F"
    return "P"


def strahnet_from_legacy(value: Any) -> Optional[str]:
    return map_lookup(value, STRAHNET_CODES)


def national_highway_freight_from_legacy(value: Any) -> Optional[str]:
    return map_lookup(value, NHFN_CODES)


def navigation_flag_from_legacy(value: Any) -> Optional[str]:
    return map_lookup(value, NAVIGATION_CODES)


def owner_maintenance_from_legacy(value: Any) -> Optional[str]:
    return map_lookup(value, OWNER_MAINTENANCE_CODES, width=2)


def federal_land_from_legacy(value: Any) -> Optional[str]:
    return map_lookup(value, FEDERAL_LAND_CODES)


def historic_from_legacy(value: Any) -> Optional[str]:
    return map_lookup(value, HISTORIC_CODES)


def toll_from_legacy(value: Any) -> Optional[str]:
    return map_lookup(value, TOLL_CODES)


def bridge_railing_from_legacy(value: Any) -> Optional[str]:
    return map_lookup(value, BRIDGE_RAILING_CODES)


def span_material_from_legacy(value: Any) -> Optional[str]:
    return map_lookup(value, SPAN_MATERIAL_CODES)


def span_continuity_from_legacy(kind: Any, span_type: Any, total_spans: int = 0) -> Optional[str]:
    kind_text = clean_code(kind)
    type_text = clean_code(span_type, width=2)
    if kind_text in {"2", "4", "6"}:
        return "C-T"
    if type_text == "07":
        return "6"
    if type_text in {"11", "12"} and total_spans > 0:
        return "2"
    if type_text in {"13", "14"}:
        return "2"
    if type_text == "19":
        return "7-T"
    if type_text:
        return "1"
    return None


def span_type_from_legacy(value: Any) -> Optional[str]:
    return map_lookup(value, SPAN_TYPE_CODES, width=2)


def deck_material_from_legacy(deck_type: Any, span_type: Any = None) -> Optional[str]:
    deck = clean_code(deck_type)
    if deck == "5":
        # 107=5 (steel plate/orthotropic): S04 only for orthotropic decks
        # (43B=08); otherwise the S-T temporary code, not a firm SX.
        return "S04" if clean_code(span_type, width=2) == "08" else "S-T"
    return map_lookup(deck, DECK_MATERIAL_CODES)


def wearing_surface_from_legacy(value: Any, deck_type: Any = None) -> Optional[str]:
    if clean_code(deck_type) == "N":
        return None
    return map_lookup(value, WEARING_SURFACE_CODES)


def deck_protective_from_legacy(membrane: Any, reinforcing: Any = None, deck_type: Any = None) -> Optional[str]:
    if clean_code(deck_type) == "N":
        return None
    membrane_code = clean_code(membrane)
    reinforcing_code = clean_code(reinforcing)
    if membrane_code in DECK_MEMBRANE_CODES:
        return DECK_MEMBRANE_CODES[membrane_code]
    if membrane_code == "0" and reinforcing_code == "6":
        return "A03"
    if membrane_code == "0" and reinforcing_code == "7":
        return "A01"
    if membrane_code == "0":
        return "0"
    return None


def deck_reinforcing_from_legacy(value: Any, deck_type: Any = None) -> Optional[str]:
    if clean_code(deck_type) == "N":
        return None
    return map_lookup(value, DECK_REINFORCING_CODES)


def functional_class_from_legacy(value: Any) -> Optional[str]:
    return map_lookup(value, FUNCTIONAL_CLASS_CODES, width=2)


def urban_code_from_legacy(value: Any) -> Optional[str]:
    return map_lookup(value, URBAN_CODE_CODES, width=2)


def nhs_from_legacy(value: Any) -> Optional[str]:
    return map_lookup(value, NHS_CODES)


def route_direction_from_legacy(value: Any) -> Optional[str]:
    return map_lookup(value, ROUTE_DIRECTION_CODES)


def route_suffix_from_legacy(value: Any) -> Optional[str]:
    """NBI Item 5E numeric directional suffix to the B.RT.02 letter."""
    return map_lookup(value, ROUTE_SUFFIX_CODES)


def navigation_protection_from_legacy(value: Any) -> Optional[str]:
    return map_lookup(value, NAVIGATION_PROTECTION_CODES)


def design_load_from_legacy(value: Any) -> Optional[str]:
    return map_lookup(value, DESIGN_LOAD_CODES)


def load_rating_method_from_legacy(value: Any) -> Optional[str]:
    return map_lookup(value, LOAD_RATING_METHOD_CODES)


def posting_status_from_legacy(value: Any) -> Optional[str]:
    return map_lookup(value, POSTING_STATUS_CODES)


def scour_vulnerability_from_legacy(value: Any) -> Optional[str]:
    return map_lookup(value, SCOUR_VULNERABILITY_CODES)


def scour_condition_from_legacy(value: Any) -> Optional[str]:
    return map_lookup(value, SCOUR_CONDITION_CODES)


def overtopping_from_legacy(waterway_eval: Any, functional_class: Any = None) -> Optional[str]:
    """NBI Item 71 + Item 26 to SNBI B.AP.02 per the FHWA two-key table."""
    eval_code = clean_code(waterway_eval)
    if not eval_code or eval_code == "N":
        return None
    fc = clean_code(functional_class, width=2)
    minor_or_local = fc in {"08", "09", "19"}
    if eval_code in {"9", "8", "5"}:
        return "1"
    if eval_code == "7":
        return "VLM-T"
    if eval_code == "6":
        return "1" if minor_or_local else "VLM-T"
    if eval_code == "4":
        return "5" if minor_or_local else "1"
    if eval_code == "3":
        if minor_or_local:
            return "6"
        if fc in {"01", "11", "12"}:
            return "1"
        return "5"
    if eval_code in {"2", "0"}:
        return "HVH-T"
    return None


def scour_poa_from_legacy(value: Any) -> Optional[str]:
    """NBI Item 113 to SNBI B.AP.04: POA not required for 4/5/8/9, else required."""
    text = clean_code(value)
    if not text or text == "N":
        return None
    if text in {"4", "5", "8", "9"}:
        return "0"
    return "Y-T"


def work_code_from_legacy(value: Any, fallback: Optional[str] = None) -> Optional[str]:
    """Only return SNBI work codes when already in SNBI-like form."""
    text = _clean(value or fallback)
    if not text:
        return None
    if text == "0":
        return "0"
    allowed_prefixes = ("BR", "IP", "DK", "SP", "SB", "JT", "RA", "CH", "CP", "OT")
    if any(text.startswith(prefix) for prefix in allowed_prefixes):
        return text
    return None


def remove_blank_values(obj: Any) -> Any:
    """Recursively drop None/blank values from SNBI payload fragments."""
    if isinstance(obj, list):
        return [item for item in (remove_blank_values(x) for x in obj) if item not in ({}, [], None)]
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            cleaned = remove_blank_values(value)
            if cleaned in (None, "", [], {}):
                continue
            out[key] = cleaned
        return out
    return obj
