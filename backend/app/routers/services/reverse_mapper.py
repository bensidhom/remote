# backend/app/routers/services/reverse_mapper.py

"""
Reverse converter: SNBI JSON -> NBI TXT (CSV) + Elements XML + flat JSON + text report.
Inverts the mapping done by mapper.py (map_bridge_to_snbi).
"""

import csv
import io
import xml.etree.ElementTree as ET
from typing import Dict, Any, List, Optional
from app.services.spreadsheet_csv import spreadsheet_safe_value
from .constants import mapping, field_names
from .snbi_rules import (
    BRIDGE_RAILING_CODES,
    DECK_MATERIAL_CODES,
    DECK_MEMBRANE_CODES,
    DECK_REINFORCING_CODES,
    DESIGN_LOAD_CODES,
    FEDERAL_LAND_CODES,
    HISTORIC_CODES,
    LOAD_RATING_METHOD_CODES,
    NAVIGATION_PROTECTION_CODES,
    NHFN_CODES,
    NHS_CODES,
    OWNER_MAINTENANCE_CODES,
    POSTING_STATUS_CODES,
    ROUTE_TYPE_CODES,
    SERVICE_TYPE_CODES,
    SPAN_MATERIAL_CODES,
    SPAN_TYPE_CODES,
    STRAHNET_CODES,
    TOLL_CODES,
    WEARING_SURFACE_CODES,
)

try:
    from ...services.conversion.legacy_projection import (
        build_legacy_projection_manifest,
        legacy_state_code,
        projection_metadata,
    )
except ImportError:  # pragma: no cover - legacy direct-module launch mode
    from services.conversion.legacy_projection import (
        build_legacy_projection_manifest,
        legacy_state_code,
        projection_metadata,
    )


def _invert(table: Dict[str, Optional[str]], overrides: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Invert a forward NBI->SNBI code table; first NBI key wins on collisions."""
    inv: Dict[str, str] = {}
    for nbi_code, snbi_code in table.items():
        if snbi_code is not None and snbi_code not in inv:
            inv[snbi_code] = nbi_code
    if overrides:
        inv.update(overrides)
    return inv


# Inverse code tables (SNBI -> legacy NBI), derived from the forward tables so
# they cannot drift. Overrides map permanent SNBI codes that the forward
# transition never emits back to their closest legacy code.
_INV_OWNER = _invert(OWNER_MAINTENANCE_CODES)
_INV_FEDERAL_LANDS = _invert(FEDERAL_LAND_CODES)
_INV_HISTORIC = _invert(HISTORIC_CODES)
_INV_TOLL = _invert(TOLL_CODES)
_INV_RAILING = _invert(BRIDGE_RAILING_CODES)
_INV_POSTING = _invert(POSTING_STATUS_CODES, overrides={
    'PO': 'A', 'PA': 'B', 'SO': 'D', 'TO': 'E', 'N': 'G', 'C': 'K',
    'PP': 'P', 'TP': 'P', 'SP': 'P', 'PR': 'R', 'TR': 'R', 'SR': 'R',
})
_INV_DESIGN_LOAD = _invert(DESIGN_LOAD_CODES)
_INV_RATING_METHOD = _invert(LOAD_RATING_METHOD_CODES)
_INV_NHS = _invert(NHS_CODES)
_INV_NHFN = _invert(NHFN_CODES)
_INV_STRAHNET = _invert(STRAHNET_CODES)
_INV_ROUTE_TYPE = _invert(ROUTE_TYPE_CODES)
_INV_SERVICE_TYPE = _invert(SERVICE_TYPE_CODES)
_INV_SPAN_MATERIAL = _invert(SPAN_MATERIAL_CODES, overrides={
    'S01': '3', 'S02': '3', 'S03': '3', 'S04': '3', 'S05': '3', 'SX': '3',
    'C01': '1', 'C02': '1', 'C03': '1', 'C04': '1', 'C05': '1', 'CX': '1',
    'P01': '5', 'PX': '5', 'T01': '7', 'T02': '7', 'T03': '7', 'T04': '7', 'TX': '7',
    'M01': '8', 'M02': '8', 'A01': '9', 'I01': '0', 'I02': '0', 'X': '0',
})
_INV_SPAN_TYPE = _invert(SPAN_TYPE_CODES)
_INV_DECK_MATERIAL = _invert(DECK_MATERIAL_CODES, overrides={'S04': '5', 'S-T': '5'})
_INV_WEARING_SURFACE = _invert(WEARING_SURFACE_CODES)
_INV_MEMBRANE = _invert(DECK_MEMBRANE_CODES, overrides={'0': '0'})
_INV_REINFORCING = _invert(DECK_REINFORCING_CODES)
_INV_NAV_PROTECTION = _invert(NAVIGATION_PROTECTION_CODES)
# B.AP.01 G/F/P spans NBI 7-9 / 5-6 / 0-4; representative values.
_INV_APPROACH_ALIGNMENT = {'G': '8', 'F': '5', 'P': '2'}
# Functional class needs the urban/rural split (B.H.02) to invert Item 26.
_INV_FUNCTIONAL_RURAL = {'1': '01', '3': '02', '4': '06', '5': '07', '6': '08', '7': '09'}
_INV_FUNCTIONAL_URBAN = {'1': '11', '2': '12', '3': '14', '4': '16', 'C-T': '17', '7': '19'}


# Build reverse lookup: SNBI code → human-readable name
SNBI_TO_NAME = {k: v for k, v in mapping.items()}

# Nested array keys in SNBI format
NESTED_KEYS = ['Features', 'SpanSets', 'SubstructureSets', 'Elements',
               'Works', 'Inspections', 'PostingEvaluations', 'PostingStatuses']


def snbi_to_flat_json(bridge: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert an SNBI bridge dict (with codes like BL01, BC01, etc.)
    into a flat, human-readable JSON dict using the mapping names.
    Nested arrays (Features, SpanSets, Elements, etc.) are expanded inline.
    """
    flat: Dict[str, Any] = {}

    for key, value in bridge.items():
        if key in NESTED_KEYS:
            continue
        name = SNBI_TO_NAME.get(key, key)
        if value is not None and value != '':
            flat[name] = value

    # Flatten nested arrays
    if 'SpanSets' in bridge and bridge['SpanSets']:
        spans = []
        for i, ss in enumerate(bridge['SpanSets']):
            span = {}
            for k, v in ss.items():
                name = SNBI_TO_NAME.get(k, k)
                if v is not None and v != '':
                    span[name] = v
            spans.append(span)
        flat['span_sets'] = spans

    if 'Features' in bridge and bridge['Features']:
        features = []
        for feat in bridge['Features']:
            f = {}
            for k, v in feat.items():
                if k == 'Routes':
                    routes = []
                    for r in (v or []):
                        route = {}
                        for rk, rv in r.items():
                            rname = SNBI_TO_NAME.get(rk, rk)
                            if rv is not None and rv != '':
                                route[rname] = rv
                        routes.append(route)
                    f['routes'] = routes
                else:
                    name = SNBI_TO_NAME.get(k, k)
                    if v is not None and v != '':
                        f[name] = v
            features.append(f)
        flat['features'] = features

    if 'Elements' in bridge and bridge['Elements']:
        elements = []
        for el in bridge['Elements']:
            e = {}
            for k, v in el.items():
                name = SNBI_TO_NAME.get(k, k)
                if v is not None and v != '':
                    e[name] = v
            elements.append(e)
        flat['elements'] = elements

    if 'Works' in bridge and bridge['Works']:
        works = []
        for w in bridge['Works']:
            wd = {}
            for k, v in w.items():
                name = SNBI_TO_NAME.get(k, k)
                if v is not None and v != '':
                    wd[name] = v
            works.append(wd)
        flat['works'] = works

    return flat


def snbi_to_text(bridge: Dict[str, Any]) -> str:
    """
    Convert an SNBI bridge dict to a human-readable text report.
    """
    lines: List[str] = []
    name = bridge.get('BID01', 'Unknown')
    state = bridge.get('BL01', '')
    lines.append(f"{'='*60}")
    lines.append(f"BRIDGE REPORT: {name} (State: {state})")
    lines.append(f"{'='*60}")
    lines.append("")

    # Group fields by category
    categories = {
        'Location & Identification': ['BL01', 'BID01', 'BID02', 'BID03', 'BL02', 'BL03', 'BL04', 'BL05', 'BL06', 'BL07', 'BL08', 'BL09', 'BL10', 'BL11', 'BL12'],
        'Classification': ['BCL01', 'BCL02', 'BCL03', 'BCL04', 'BCL05', 'BCL06'],
        'Railings & Safety': ['BRH01', 'BRH02'],
        'Geometry & Dimensions': ['BG01', 'BG02', 'BG03', 'BG04', 'BG05', 'BG06', 'BG07', 'BG08', 'BG09', 'BG10', 'BG11', 'BG12', 'BG13', 'BG14', 'BG15', 'BG16'],
        'Load Rating': ['BLR01', 'BLR02', 'BLR03', 'BLR04', 'BLR05', 'BLR06', 'BLR07', 'BLR08'],
        'Inspection Requirements': ['BIR01', 'BIR02', 'BIR03', 'BIR04'],
        'Condition Ratings': ['BC01', 'BC02', 'BC03', 'BC04', 'BC05', 'BC06', 'BC07', 'BC08', 'BC09', 'BC10', 'BC11', 'BC12', 'BC13', 'BC14', 'BC15'],
        'Assessment': ['BAP01', 'BAP02', 'BAP03', 'BAP04', 'BAP05'],
        'Construction': ['BW01'],
        'Posting': ['BPS01', 'BPS02'],
    }

    for cat_name, keys in categories.items():
        cat_values = []
        for k in keys:
            v = bridge.get(k)
            if v is not None and v != '':
                label = SNBI_TO_NAME.get(k, k)
                cat_values.append((label, v))
        if cat_values:
            lines.append(f"--- {cat_name} ---")
            for label, val in cat_values:
                lines.append(f"  {label}: {val}")
            lines.append("")

    # Span Sets
    if bridge.get('SpanSets'):
        lines.append("--- Span Sets ---")
        for i, ss in enumerate(bridge['SpanSets'], 1):
            lines.append(f"  Span Set {i}:")
            for k, v in ss.items():
                if v is not None and v != '':
                    label = SNBI_TO_NAME.get(k, k)
                    lines.append(f"    {label}: {v}")
        lines.append("")

    # Features
    if bridge.get('Features'):
        lines.append("--- Features ---")
        for i, feat in enumerate(bridge['Features'], 1):
            lines.append(f"  Feature {i}:")
            for k, v in feat.items():
                if k == 'Routes':
                    for j, r in enumerate(v or [], 1):
                        lines.append(f"    Route {j}:")
                        for rk, rv in r.items():
                            if rv is not None and rv != '':
                                rlabel = SNBI_TO_NAME.get(rk, rk)
                                lines.append(f"      {rlabel}: {rv}")
                elif v is not None and v != '':
                    label = SNBI_TO_NAME.get(k, k)
                    lines.append(f"    {label}: {v}")
        lines.append("")

    # Elements
    if bridge.get('Elements'):
        lines.append("--- Elements ---")
        for i, el in enumerate(bridge['Elements'], 1):
            parts = []
            for k, v in el.items():
                if v is not None and v != '':
                    label = SNBI_TO_NAME.get(k, k)
                    parts.append(f"{label}={v}")
            lines.append(f"  Element {i}: {', '.join(parts)}")
        lines.append("")

    # Works
    if bridge.get('Works'):
        lines.append("--- Work History ---")
        for w in bridge['Works']:
            for k, v in w.items():
                if v is not None and v != '':
                    label = SNBI_TO_NAME.get(k, k)
                    lines.append(f"  {label}: {v}")
        lines.append("")

    return '\n'.join(lines)


def _feet_to_meters(val: Any) -> Optional[str]:
    """Convert a feet value (float/str) back to meters, rounded to 3 decimal places."""
    try:
        f = float(val)
        if f <= 0:
            return None
        return str(round(f * 0.3048, 3))
    except (TypeError, ValueError):
        return None


def _decimal_to_dms_int(decimal_deg: float) -> int:
    """Convert decimal degrees to packed DDMMSSCC integer (NBI format: DDMMSSSS)."""
    decimal_deg = abs(decimal_deg)
    degrees = int(decimal_deg)
    minutes_f = (decimal_deg - degrees) * 60
    minutes = int(minutes_f)
    seconds = (minutes_f - minutes) * 60
    return degrees * 1000000 + minutes * 10000 + round(seconds * 100)


def _ft_to_m_str(val: Any, decimals: int = 2) -> Optional[str]:
    """Convert feet to meters string, or None if invalid."""
    if val is None:
        return None
    try:
        f = float(val)
        if f < 0:
            return None
        return f'{round(f * 0.3048, decimals):.{decimals}f}'
    except (TypeError, ValueError):
        return None


# Direct SNBI code -> NBI CSV column name mapping (best-effort reverse)
# Fields ending in _MT expect meters; geometry fields stored as feet in SNBI
# are converted back via _SNBI_TO_NBI_FT (feet→meters) below.
_SNBI_TO_NBI = {
    'BID01': 'STRUCTURE_NUMBER_008',
    'BL02': 'COUNTY_CODE_003',
    'BL03': 'PLACE_CODE_004',
    'BL04': 'HIGHWAY_DISTRICT_002',
    'BL11': 'LOCATION_009',
    # BL12 (Metropolitan Planning Organization) has no legacy transition.
    # BL05/BL06 (lat/lon) stored as decimal in SNBI; converted back to DMS int below
    'BL07': 'OTHR_STATE_STRUC_NO_099',
    # BL08 needs country expansion or the legacy Item 1 state/region table.
    # BL09 codes (0/1/2) map back to Item 98B percentages below.
    'BW01': 'YEAR_BUILT_027',
    'BC01': 'DECK_COND_058',
    'BC02': 'SUPERSTRUCTURE_COND_059',
    'BC03': 'SUBSTRUCTURE_COND_060',
    'BC04': 'CULVERT_COND_062',
    # BC05-BC08 (railing/transition/bearing/joint condition ratings) have no NBI
    # home: legacy Items 67-70 were appraisal ratings and are discontinued.
    'BC09': 'CHANNEL_COND_061',
    # BC11/BAP03 are recoded during forward transition; Item 113 is reconstructed
    # jointly from both via _scour_113_from_snbi below.
    'BC12': 'BRIDGE_CONDITION',
    'BC13': 'LOWEST_RATING',
    'BG10': 'MEDIAN_CODE_033',
    'BG11': 'DEGREES_SKEW_034',
    # Load rating factors: stored as decimal in SNBI, converted back to NBI tons below
    # (BLR05 and BLR06 handled separately after the main loop)
    'BIR01': 'FRACTURE_092A',
    'BIR03': 'UNDWATER_LOOK_SEE_092B',
    # BAP02 is recoded from Items 71+26 during forward transition; Item 71 is
    # reconstructed via _WATERWAY_71_FROM_BAP02 below (lossy best effort).
}

# SNBI fields whose values were recoded during forward transition: translate
# back through the inverse code tables instead of writing SNBI codes verbatim.
_SNBI_TO_NBI_CODED: Dict[str, tuple] = {
    'BCL01': ('OWNER_022', _INV_OWNER),
    'BCL02': ('MAINTENANCE_021', _INV_OWNER),
    'BCL03': ('FEDERAL_LANDS_105', _INV_FEDERAL_LANDS),
    'BCL04': ('HISTORY_037', _INV_HISTORIC),
    'BCL05': ('TOLL_020', _INV_TOLL),
    'BRH01': ('RAILINGS_036A', _INV_RAILING),
    'BRH02': ('TRANSITIONS_036B', _INV_RAILING),
    'BLR01': ('DESIGN_LOAD_031', _INV_DESIGN_LOAD),
    'BLR04': ('OPR_RATING_METH_063', _INV_RATING_METHOD),
    'BPS01': ('OPEN_CLOSED_POSTED_041', _INV_POSTING),
    'BAP01': ('APPR_ROAD_EVAL_072', _INV_APPROACH_ALIGNMENT),
}

# Best-effort inverse of the FHWA Item 71+26 -> B.AP.02 table. Several NBI
# codes collapse to one SNBI value; ambiguous values resolve to the most
# common legacy code.
_WATERWAY_71_FROM_BAP02 = {
    '1': '8', 'VLM-T': '7', 'HVH-T': '2',
    '5': '3', '6': '3', '0': '9', '2': '7', '3': '7', '4': '7',
}

# Geometry fields stored as feet in SNBI that map back to meters NBI fields
_SNBI_TO_NBI_FT = {
    'BG02': 'STRUCTURE_LEN_MT_049',
    'BG03': 'MAX_SPAN_LEN_MT_048',
    'BG05': 'DECK_WIDTH_MT_052',
    'BG06': 'ROADWAY_WIDTH_MT_051',
    'BG07': 'LEFT_CURB_MT_050A',
    'BG08': 'RIGHT_CURB_MT_050B',
    'BG09': 'APPR_WIDTH_MT_032',
}


def _date8_to_mmyy(date8: Any) -> Optional[str]:
    """SNBI YYYYMMDD -> legacy NBI MMYY (e.g. 20230701 -> 0723)."""
    text = str(date8 or '')
    if len(text) == 8 and text.isdigit():
        return text[4:6] + text[2:4]
    return None


def _scour_113_from_snbi(bap03: Any, bc11: Any) -> Optional[str]:
    """Best-effort inverse of the FHWA Item 113 -> B.C.11/B.AP.03 transition.

    The forward table is lossy (113 values 5 and 8 collapse to the same pair,
    as do T and 6); ambiguous pairs resolve to the more common NBI code.
    """
    ap = str(bap03 or '').strip().upper()
    c = str(bc11 or '').strip().upper()
    if not ap:
        return 'N' if c == 'N' else None
    if ap == 'U':
        return 'U'
    if ap == 'A':
        return '9'
    if ap == 'BCE-T':
        return '7'
    if ap == 'AB-T':
        return '4' if c == 'MO-T' else '8'
    if ap == 'CD-T':
        return {'MA-T': '2', '1': '1', '0': '0'}.get(c, '3')
    if ap == '0':
        return '6'
    # Permanent SNBI appraisal codes entered after transition.
    return {'B': '7', 'C': '3', 'D': '3', 'E': '6'}.get(ap)


def snbi_to_nbi_row(bridge: Dict[str, Any]) -> Dict[str, str]:
    """Convert one SNBI bridge dict back to a flat dict keyed by NBI field names."""
    row: Dict[str, str] = {f: '' for f in field_names}
    row['RECORD_TYPE_005A'] = '1'  # main span record

    for snbi_key, nbi_field in _SNBI_TO_NBI.items():
        val = bridge.get(snbi_key)
        if val is not None and val != '':
            row[nbi_field] = str(val)

    # SNBI retains the two-digit FIPS value but not the historical FHWA region
    # character required by legacy Item 1/98A.  Reconstruct only values present
    # in the pinned Item 1 table; do not pass malformed three-character values
    # through as if they were valid SNBI.
    state_decision = legacy_state_code(bridge.get('BL01'), allow_country=False)
    if state_decision.value is not None:
        row['STATE_CODE_001'] = state_decision.value
    border_decision = legacy_state_code(bridge.get('BL08'), allow_country=True)
    if border_decision.value is not None:
        row['OTHER_STATE_CODE_098A'] = border_decision.value

    for snbi_key, (nbi_field, inverse) in _SNBI_TO_NBI_CODED.items():
        val = bridge.get(snbi_key)
        if val is not None and val != '':
            back = inverse.get(str(val).strip().upper())
            if back is not None:
                row[nbi_field] = back

    scour_113 = _scour_113_from_snbi(bridge.get('BAP03'), bridge.get('BC11'))
    if scour_113:
        row['SCOUR_CRITICAL_113'] = scour_113

    bap02 = str(bridge.get('BAP02') or '').strip().upper()
    if bap02:
        row['WATERWAY_EVAL_071'] = _WATERWAY_71_FROM_BAP02.get(bap02, '')

    # B.L.09 code -> Item 98B percent (1 = shared is indeterminate; use 50).
    bl09 = str(bridge.get('BL09') or '').strip()
    if bl09:
        row['OTHER_STATE_PCNT_098B'] = {'0': '99', '2': '00', '1': '50'}.get(bl09, '')

    # Lat/lon: stored as decimal degrees in SNBI; NBI expects packed DDMMSSCC integers.
    bl05 = bridge.get('BL05')
    if bl05 is not None and bl05 != '':
        try:
            row['LAT_016'] = f'{_decimal_to_dms_int(float(bl05)):08d}'
        except (TypeError, ValueError):
            pass
    bl06 = bridge.get('BL06')
    if bl06 is not None and bl06 != '':
        try:
            # BL06 is negative for US West longitude; NBI stores absolute DMS value
            row['LONG_017'] = f'{_decimal_to_dms_int(abs(float(bl06))):09d}'
        except (TypeError, ValueError):
            pass

    # Geometry fields: convert feet back to meters, preserving up to 1 decimal place
    for snbi_key, nbi_field in _SNBI_TO_NBI_FT.items():
        val = bridge.get(snbi_key)
        if val is not None and val != '':
            try:
                m = round(float(val) * 0.3048, 1)
                if m >= 0:
                    # Format: drop trailing '.0' for whole numbers (e.g. 2028.0 → '2028')
                    row[nbi_field] = f'{m:.1f}'.rstrip('0').rstrip('.')
            except (TypeError, ValueError):
                pass

    # Convert load rating factors (decimal) back to NBI ton values. The
    # forward divisor (32.4 for rating-factor methods 6/7/8/D/E/F, else 32.67)
    # is not recoverable from SNBI method codes, which collapse 1/6, 2/7 and
    # 3/8; 32.67 matches the common tons-based methods (~0.8% off otherwise).
    for snbi_key, nbi_field in (('BLR05', 'INVENTORY_RATING_066'), ('BLR06', 'OPERATING_RATING_064')):
        val = bridge.get(snbi_key)
        if val is None or val == '':
            continue
        try:
            tons = round(float(val) * 32.67)
        except (TypeError, ValueError):
            continue
        if tons > 0:
            row[nbi_field] = str(min(tons, 9999))

    # Recover year built (BR1 event) and year reconstructed (RECONSTRUCTED
    # event) from Works. Item 97 (proposed improvement year) never transitions
    # into Works, so it is not reconstructed here.
    for w in (bridge.get('Works') or []):
        if not isinstance(w, dict):
            continue
        desc = str(w.get('BW03', '')).upper()
        yr = w.get('BW02')
        if not yr:
            continue
        if desc == 'BR1' and not row.get('YEAR_BUILT_027'):
            row['YEAR_BUILT_027'] = str(yr)
        elif desc == 'RECONSTRUCTED' and not row.get('YEAR_RECONSTRUCTED_106'):
            row['YEAR_RECONSTRUCTED_106'] = str(yr)

    # Pull posting status from PostingStatuses when provided. B.PS.02 is a
    # posting-status date and has no defensible transition to Item 90.
    posting_statuses = bridge.get('PostingStatuses') or []
    if posting_statuses:
        ps0 = posting_statuses[0] or {}
        if not row.get('OPEN_CLOSED_POSTED_041'):
            v = ps0.get('BPS01')
            if v is not None and v != '':
                inverse = _INV_POSTING.get(str(v).strip().upper())
                if inverse is not None:
                    row['OPEN_CLOSED_POSTED_041'] = inverse

    # Pull inspection events back into Items 90/91 (routine) and 92/93 (NSTM,
    # underwater, special) keyed on the SNBI inspection type BIE01.
    inspections = bridge.get('Inspections') or []
    for insp in inspections:
        if not isinstance(insp, dict):
            continue
        itype = str(insp.get('BIE01') or '')
        date8 = str(insp.get('BIE02') or '')
        interval = insp.get('BIE05')
        if itype in {'1', '2'}:
            if not row.get('DATE_OF_INSPECT_090') and date8:
                row['DATE_OF_INSPECT_090'] = date8
            # BIE05 may be an integer 0; guard against the `0 or ''` pitfall
            if not row.get('INSPECT_FREQ_MONTHS_091') and interval is not None:
                row['INSPECT_FREQ_MONTHS_091'] = str(interval)
            # B.IE.11 is an inspection note, not Item 75A Work Proposed. It is
            # intentionally omitted and accounted for in the loss manifest.
        elif itype in {'3', '4', '7'}:
            date_field, flag_field = {
                '4': ('FRACTURE_LAST_DATE_093A', 'FRACTURE_092A'),
                '3': ('UNDWATER_LAST_DATE_093B', 'UNDWATER_LOOK_SEE_092B'),
                '7': ('SPEC_LAST_DATE_093C', 'SPEC_INSPECT_092C'),
            }[itype]
            if not row.get(date_field) and date8:
                row[date_field] = _date8_to_mmyy(date8) or date8
            # Items 92A/B/C are "YXX" (flag + interval months); BIR01/03/04 set
            # the bare flag, the matching event's BIE05 supplies the interval.
            if interval is not None and str(interval).isdigit():
                flag = str(row.get(flag_field) or '').strip().upper()
                if not flag or flag == 'Y':
                    row[flag_field] = f'Y{int(interval):02d}'

    # Recover feature-level NBI columns. Feature records mirror the FHWA
    # derivation: carried H01(+R01/P01) from Item 42A; below H02/R/P/W01/F01/X01
    # from Item 42B.
    carries_rail = carries_trail = False
    under_kinds = set()
    seen_h_under = False
    for feat in (bridge.get('Features') or []):
        if not isinstance(feat, dict):
            continue
        bf01 = str(feat.get('BF01', ''))
        bf02 = str(feat.get('BF02', ''))
        kind = bf01[:1].upper()

        if bf02 == 'C':
            if kind == 'R':
                carries_rail = True
            elif kind == 'P':
                carries_trail = True
            elif kind == 'H':
                if not row.get('FACILITY_CARRIED_007'):
                    row['FACILITY_CARRIED_007'] = str(feat.get('BF03', ''))
                if not row.get('FUNCTIONAL_CLASS_026'):
                    # Item 26 needs the urban/rural split carried by BH02.
                    bh01 = str(feat.get('BH01') or '')
                    is_rural = str(feat.get('BH02') or '') == '99999'
                    fc_inv = _INV_FUNCTIONAL_RURAL if is_rural else _INV_FUNCTIONAL_URBAN
                    row['FUNCTIONAL_CLASS_026'] = fc_inv.get(bh01, '')
                if not row.get('ADT_029'):
                    row['ADT_029'] = str(feat.get('BH09', ''))
                if not row.get('TRAFFIC_LANES_ON_028A'):
                    row['TRAFFIC_LANES_ON_028A'] = str(feat.get('BH08', ''))
                if not row.get('YEAR_ADT_030'):
                    row['YEAR_ADT_030'] = str(feat.get('BH11', ''))
                if not row.get('HIGHWAY_SYSTEM_104'):
                    v = _INV_NHS.get(str(feat.get('BH03') or ''))
                    if v: row['HIGHWAY_SYSTEM_104'] = v
                if not row.get('NATIONAL_NETWORK_110'):
                    v = _INV_NHFN.get(str(feat.get('BH04') or ''))
                    if v: row['NATIONAL_NETWORK_110'] = v
                if not row.get('STRAHNET_HIGHWAY_100'):
                    v = _INV_STRAHNET.get(str(feat.get('BH05') or ''))
                    if v: row['STRAHNET_HIGHWAY_100'] = v
                # BH06 = 10-character Item 13A + 2-character Item 13B.
                if not row.get('LRS_INV_ROUTE_013A'):
                    bh06 = str(feat.get('BH06') or '')
                    if bh06:
                        row['LRS_INV_ROUTE_013A'] = bh06[:10]
                        row['SUBROUTE_NO_013B'] = bh06[10:12]
                # BH07 = miles; KILOPOINT_011 is raw NBI integer (km_to_mile divided by 1000 first)
                if not row.get('KILOPOINT_011'):
                    bh07 = feat.get('BH07')
                    if bh07 is not None and bh07 != '':
                        try:
                            row['KILOPOINT_011'] = str(round(float(bh07) * 1609.34))
                        except (TypeError, ValueError):
                            pass
                # BH12 = min vertical clearance (feet) → MIN_VERT_CLR_010 (meters)
                if not row.get('MIN_VERT_CLR_010'):
                    v = _ft_to_m_str(feat.get('BH12'))
                    if v: row['MIN_VERT_CLR_010'] = v
                # BH13 on the carried highway = Item 53 (over-roadway clearance)
                if not row.get('VERT_CLR_OVER_MT_053'):
                    v = _ft_to_m_str(feat.get('BH13'))
                    if v: row['VERT_CLR_OVER_MT_053'] = v
                # BH16 = horizontal clearance (feet) → HORR_CLR_MT_047 (meters)
                if not row.get('HORR_CLR_MT_047'):
                    v = _ft_to_m_str(feat.get('BH16'))
                    if v: row['HORR_CLR_MT_047'] = v
                # BH17 = detour in miles → DETOUR_KILOS_019 in km (raw int)
                if not row.get('DETOUR_KILOS_019'):
                    bh17 = feat.get('BH17')
                    if bh17 is not None and bh17 != '':
                        try:
                            km = round(float(bh17) * 1.60934)
                            if km > 0: row['DETOUR_KILOS_019'] = str(km)
                        except (TypeError, ValueError):
                            pass
                if not row.get('PERCENT_ADT_TRUCK_109'):
                    bh10 = feat.get('BH10')
                    bh09 = feat.get('BH09')
                    if bh10 is not None and bh09:
                        try:
                            pct = round(float(bh10) / float(bh09) * 100)
                            row['PERCENT_ADT_TRUCK_109'] = str(pct)
                        except (TypeError, ValueError, ZeroDivisionError):
                            pass
                # Routes
                for route in (feat.get('Routes') or []):
                    if not row.get('ROUTE_NUMBER_005D'):
                        brt02 = str(route.get('BRT02') or '')
                        # BRT02 = route number + directional letter suffix
                        import re as _re
                        m = _re.match(r'^(\d+)([A-Za-z]*)$', brt02)
                        if m:
                            row['ROUTE_NUMBER_005D'] = m.group(1)
                            suffix = m.group(2).upper()
                            if suffix and not row.get('DIRECTION_005E'):
                                row['DIRECTION_005E'] = {'N': '1', 'E': '2', 'S': '3', 'W': '4'}.get(suffix, '')
                        else:
                            row['ROUTE_NUMBER_005D'] = brt02
                    if not row.get('TRAFFIC_DIRECTION_102'):
                        v = route.get('BRT03')
                        if v is not None and v != '':
                            row['TRAFFIC_DIRECTION_102'] = {'1-T': '1', '2-T': '2', 'NB': '1', 'EB': '1', 'SB': '1', 'WB': '1', 'NS': '2', 'EW': '2'}.get(str(v), '')
                    if not row.get('ROUTE_PREFIX_005B'):
                        v = _INV_ROUTE_TYPE.get(str(route.get('BRT04') or ''))
                        if v: row['ROUTE_PREFIX_005B'] = v
                    if not row.get('SERVICE_LEVEL_005C'):
                        v = _INV_SERVICE_TYPE.get(str(route.get('BRT05') or ''))
                        if v: row['SERVICE_LEVEL_005C'] = v
                    break  # only first route for top-level NBI fields

        elif bf02 == 'B':
            under_kinds.add(kind)
            if not row.get('FEATURES_DESC_006A'):
                v = feat.get('BF03')
                if v is not None and v != '':
                    row['FEATURES_DESC_006A'] = str(v)
            if kind == 'H' and not seen_h_under:
                seen_h_under = True
                # Primary under highway: clearances live on the main record
                # with reference letter H.
                if not row.get('TRAFFIC_LANES_UND_028B'):
                    v = feat.get('BH08')
                    if v is not None and v != '': row['TRAFFIC_LANES_UND_028B'] = str(v)
                if feat.get('BH13') is not None and not row.get('VERT_CLR_UND_054B'):
                    v = _ft_to_m_str(feat.get('BH13'))
                    if v:
                        row['VERT_CLR_UND_REF_054A'] = 'H'
                        row['VERT_CLR_UND_054B'] = v
                if feat.get('BH15') is not None and not row.get('LAT_UND_MT_055B'):
                    v = _ft_to_m_str(feat.get('BH15'))
                    if v:
                        row['LAT_UND_REF_055A'] = 'H'
                        row['LAT_UND_MT_055B'] = v
                if feat.get('BH14') is not None and not row.get('LEFT_LAT_UND_MT_056'):
                    v = _ft_to_m_str(feat.get('BH14'))
                    if v: row['LEFT_LAT_UND_MT_056'] = v
            elif kind == 'R':
                if feat.get('BRR02') is not None and not row.get('VERT_CLR_UND_054B'):
                    v = _ft_to_m_str(feat.get('BRR02'))
                    if v:
                        row['VERT_CLR_UND_REF_054A'] = 'R'
                        row['VERT_CLR_UND_054B'] = v
                if feat.get('BRR03') is not None and not row.get('LAT_UND_MT_055B'):
                    v = _ft_to_m_str(feat.get('BRR03'))
                    if v:
                        row['LAT_UND_REF_055A'] = 'R'
                        row['LAT_UND_MT_055B'] = v
            elif kind == 'W':
                if not row.get('NAVIGATION_038'):
                    v = str(feat.get('BN01') or '')
                    if v:
                        row['NAVIGATION_038'] = {'Y': '1', 'N': '0'}.get(v, v)
                # BN03 exists only for lift bridges (43B=15), where BN02 came
                # from Item 116 and BN03 from Item 39; otherwise BN02 is Item 39.
                if feat.get('BN03') is not None and feat.get('BN03') != '':
                    if not row.get('NAV_VERT_CLR_MT_039'):
                        v = _ft_to_m_str(feat.get('BN03'))
                        if v: row['NAV_VERT_CLR_MT_039'] = v
                    if not row.get('MIN_NAV_CLR_MT_116'):
                        v = _ft_to_m_str(feat.get('BN02'))
                        if v: row['MIN_NAV_CLR_MT_116'] = v
                elif not row.get('NAV_VERT_CLR_MT_039'):
                    v = _ft_to_m_str(feat.get('BN02'))
                    if v: row['NAV_VERT_CLR_MT_039'] = v
                if not row.get('NAV_HORR_CLR_MT_040'):
                    v = _ft_to_m_str(feat.get('BN04'))
                    if v: row['NAV_HORR_CLR_MT_040'] = v
                if not row.get('PIER_PROTECTION_111'):
                    v = _INV_NAV_PROTECTION.get(str(feat.get('BN06') or ''))
                    if v: row['PIER_PROTECTION_111'] = v

    # Item 42A from carried features; Item 42B from the set of below features.
    if not row.get('SERVICE_ON_042A'):
        if carries_rail:
            row['SERVICE_ON_042A'] = '4'
        elif carries_trail:
            row['SERVICE_ON_042A'] = '5'
        elif any(str(f.get('BF02')) == 'C' for f in (bridge.get('Features') or []) if isinstance(f, dict)):
            row['SERVICE_ON_042A'] = '1'
        else:
            row['SERVICE_ON_042A'] = '0'
    if not row.get('SERVICE_UND_042B'):
        h, r, w = 'H' in under_kinds, 'R' in under_kinds, 'W' in under_kinds
        if h and w and r:
            code = '8'
        elif r and w:
            code = '7'
        elif h and w:
            code = '6'
        elif w:
            code = '5'
        elif h and r:
            code = '4'
        elif 'P' in under_kinds and not h and not r:
            code = '3'
        elif r:
            code = '2'
        elif h:
            code = '1'
        elif 'F' in under_kinds:
            code = '9'
        else:
            code = '0'
        row['SERVICE_UND_042B'] = code

    # Pull span data from first SpanSet (main spans) and second (approach spans).
    span_sets = bridge.get('SpanSets') or []
    for i, ss in enumerate(span_sets):
        config = str(ss.get('BSP01', ''))
        # C01 = main culvert spans, C02 = approach culvert spans; A* = approach bridge spans
        is_approach = config.startswith('A') or config == 'C02'
        if not is_approach:
            if not row.get('MAIN_UNIT_SPANS_045'):
                row['MAIN_UNIT_SPANS_045'] = str(ss.get('BSP02', ''))
            if not row.get('STRUCTURE_KIND_043A'):
                row['STRUCTURE_KIND_043A'] = _INV_SPAN_MATERIAL.get(str(ss.get('BSP04') or ''), '')
            if not row.get('STRUCTURE_TYPE_043B'):
                row['STRUCTURE_TYPE_043B'] = _INV_SPAN_TYPE.get(str(ss.get('BSP06') or ''), '')
            if not row.get('DECK_STRUCTURE_TYPE_107'):
                row['DECK_STRUCTURE_TYPE_107'] = _INV_DECK_MATERIAL.get(str(ss.get('BSP09') or ''), '')
            if not row.get('SURFACE_TYPE_108A'):
                row['SURFACE_TYPE_108A'] = _INV_WEARING_SURFACE.get(str(ss.get('BSP10') or ''), '')
            bsp11 = str(ss.get('BSP11') or '')
            if not row.get('MEMBRANE_TYPE_108B'):
                # A03/A01 encode membrane 0 combined with reinforcing 6/7.
                if bsp11 in {'A03', 'A01'}:
                    row['MEMBRANE_TYPE_108B'] = '0'
                else:
                    row['MEMBRANE_TYPE_108B'] = _INV_MEMBRANE.get(bsp11, '')
            if not row.get('DECK_PROTECTION_108C'):
                if bsp11 == 'A03':
                    row['DECK_PROTECTION_108C'] = '6'
                elif bsp11 == 'A01':
                    row['DECK_PROTECTION_108C'] = '7'
                else:
                    row['DECK_PROTECTION_108C'] = _INV_REINFORCING.get(str(ss.get('BSP12') or ''), '')
        else:
            if not row.get('APPR_SPANS_046'):
                row['APPR_SPANS_046'] = str(ss.get('BSP02', ''))
            if not row.get('APPR_KIND_044A'):
                row['APPR_KIND_044A'] = _INV_SPAN_MATERIAL.get(str(ss.get('BSP04') or ''), '')
            if not row.get('APPR_TYPE_044B'):
                row['APPR_TYPE_044B'] = _INV_SPAN_TYPE.get(str(ss.get('BSP06') or ''), '')

    # Convert inspection date from SNBI YYYYMMDD back to NBI MMYY.
    date_full = row.get('DATE_OF_INSPECT_090', '')
    mmyy = _date8_to_mmyy(date_full)
    if mmyy:
        row['DATE_OF_INSPECT_090'] = mmyy

    return row


def _legacy_rows_to_csv(rows: List[Dict[str, str]]) -> str:
    """Serialize already-projected legacy-shaped rows as headered CSV."""
    buf = io.StringIO(newline='')
    writer = csv.DictWriter(buf, fieldnames=field_names, lineterminator='\r\n')
    writer.writeheader()
    writer.writerows(
        {
            field: spreadsheet_safe_value(row.get(field, ''))
            for field in field_names
        }
        for row in rows
    )
    return buf.getvalue()


def bridges_to_nbi_csv(bridges: List[Dict[str, Any]]) -> str:
    """Generate a review-only legacy-shaped CSV, never an NBI 445 TXT file."""
    return _legacy_rows_to_csv([snbi_to_nbi_row(bridge) for bridge in bridges])


def bridges_to_elements_xml(bridges: List[Dict[str, Any]]) -> str:
    """Generate NBI Elements XML from a list of SNBI bridge dicts."""
    root = ET.Element('BridgeElements')

    for bridge in bridges:
        struc_num = str(bridge.get('BID01', '')).strip()
        elements = bridge.get('Elements') or []
        for el in elements:
            fhwaed = ET.SubElement(root, 'FHWAED')
            ET.SubElement(fhwaed, 'STRUCNUM').text = struc_num
            ET.SubElement(fhwaed, 'EN').text = str(el.get('BE01', ''))
            ET.SubElement(fhwaed, 'EPN').text = str(el.get('BE02', '0'))
            ET.SubElement(fhwaed, 'TOTALQTY').text = str(el.get('BE03', '0'))
            ET.SubElement(fhwaed, 'CS1').text = str(el.get('BCS01', '0'))
            ET.SubElement(fhwaed, 'CS2').text = str(el.get('BCS02', '0'))
            ET.SubElement(fhwaed, 'CS3').text = str(el.get('BCS03', '0'))
            ET.SubElement(fhwaed, 'CS4').text = str(el.get('BCS04', '0'))

    ET.indent(root, space='  ')
    return ET.tostring(root, encoding='unicode', xml_declaration=False)


def convert_snbi_to_outputs(bridges: List[Dict[str, Any]]) -> Dict[str, Any]:
    flat_list = []
    text_parts = []

    for bridge in bridges:
        flat_list.append(snbi_to_flat_json(bridge))
        text_parts.append(snbi_to_text(bridge))

    legacy_rows = [snbi_to_nbi_row(bridge) for bridge in bridges]
    metadata = projection_metadata(bridge_count=len(bridges))
    loss_manifest = build_legacy_projection_manifest(bridges, legacy_rows)

    return {
        'flat_json': flat_list,
        'text': '\n\n'.join(text_parts),
        # Backward-compatible key. The accompanying metadata makes clear that
        # this is a headered, synthetic legacy projection rather than NBI TXT.
        'nbi_csv': _legacy_rows_to_csv(legacy_rows),
        'elements_xml': bridges_to_elements_xml(bridges),
        'count': len(bridges),
        'legacy_projection': metadata,
        'loss_manifest': loss_manifest,
    }
