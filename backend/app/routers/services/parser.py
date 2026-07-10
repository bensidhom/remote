# backend/app/routers/services/parser.py

from collections import defaultdict
import csv
import itertools
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, DefaultDict, Any
import re

from .constants import field_names

# Key column names that identify a valid NBI TXT header row
_NBI_HEADER_KEYS = frozenset({'STATE_CODE_001', 'STRUCTURE_NUMBER_008', 'RECORD_TYPE_005A'})

# ── Fixed-width NBI format support ────────────────────────────────────────────
# FHWA NBI fixed-width format (no header, no commas), 445-character records.
# Positions are 0-indexed (official FHWA spec is 1-indexed; subtract 1 from start,
# keep end as-is). Verified against official record format at fhwa.dot.gov/bridge/nbi/format.cfm
# and 2024 Maryland NBI data.
#
# Official layout (0-indexed):
#  [0:3]    STATE_CODE_001 (3 chars: 2-digit state + 1-char filler)
#  [3:18]   STRUCTURE_NUMBER_008 (15)
#  [18:19]  RECORD_TYPE_005A
#  [19:20]  ROUTE_PREFIX_005B
#  [20:21]  SERVICE_LEVEL_005C
#  [21:26]  ROUTE_NUMBER_005D (5)
#  [26:27]  DIRECTION_005E
#  [27:29]  HIGHWAY_DISTRICT_002 (2)
#  [29:32]  COUNTY_CODE_003 (3)
#  [32:37]  PLACE_CODE_004 (5)
#  [37:61]  FEATURES_DESC_006A (24)
#  [61:62]  CRITICAL_FACILITY_006B
#  [62:80]  FACILITY_CARRIED_007 (18)
#  [80:105] LOCATION_009 (25)
#  [105:109] MIN_VERT_CLR_010 (4)
#  [109:116] KILOPOINT_011 (7)
#  [116:117] BASE_HWY_NETWORK_012 (1)
#  [117:127] LRS_INV_ROUTE_013A (10)
#  [127:129] SUBROUTE_NO_013B (2)
#  [129:137] LAT_016 (8)
#  [137:146] LONG_017 (9)
#  [146:149] DETOUR_KILOS_019 (3)
#  [149:150] TOLL_020 (1)
#  [150:152] MAINTENANCE_021 (2)
#  [152:154] OWNER_022 (2)
#  [154:156] FUNCTIONAL_CLASS_026 (2)
#  [156:160] YEAR_BUILT_027 (4)
#  [160:162] TRAFFIC_LANES_ON_028A (2)
#  [162:164] TRAFFIC_LANES_UND_028B (2)
#  ... continuing at official positions through [444]

_NBI_FIXED_POSITIONS: List[tuple] = [
    # (field_name, 0-indexed-start, 0-indexed-end-exclusive)
    ('STATE_CODE_001',            0,   2),   # chars 1-2 of the 3-char field
    ('STRUCTURE_NUMBER_008',      3,  18),
    ('RECORD_TYPE_005A',         18,  19),
    ('ROUTE_PREFIX_005B',        19,  20),
    ('SERVICE_LEVEL_005C',       20,  21),
    ('ROUTE_NUMBER_005D',        21,  26),
    ('DIRECTION_005E',           26,  27),
    ('HIGHWAY_DISTRICT_002',     27,  29),
    ('COUNTY_CODE_003',          29,  32),
    ('PLACE_CODE_004',           32,  37),
    ('FEATURES_DESC_006A',       37,  61),
    ('CRITICAL_FACILITY_006B',   61,  62),
    ('FACILITY_CARRIED_007',     62,  80),
    ('LOCATION_009',             80, 105),
    ('MIN_VERT_CLR_010',        105, 109),
    ('KILOPOINT_011',           109, 116),
    ('BASE_HWY_NETWORK_012',    116, 117),
    ('LRS_INV_ROUTE_013A',      117, 127),
    ('SUBROUTE_NO_013B',        127, 129),
    ('LAT_016',                 129, 137),
    ('LONG_017',                137, 146),
    ('DETOUR_KILOS_019',        146, 149),
    ('TOLL_020',                149, 150),
    ('MAINTENANCE_021',         150, 152),
    ('OWNER_022',               152, 154),
    ('FUNCTIONAL_CLASS_026',    154, 156),
    ('YEAR_BUILT_027',          156, 160),
    ('TRAFFIC_LANES_ON_028A',   160, 162),
    ('TRAFFIC_LANES_UND_028B',  162, 164),
    ('ADT_029',                 164, 170),
    ('YEAR_ADT_030',            170, 174),
    ('DESIGN_LOAD_031',         174, 175),
    ('APPR_WIDTH_MT_032',       175, 179),
    ('MEDIAN_CODE_033',         179, 180),
    ('DEGREES_SKEW_034',        180, 182),
    ('STRUCTURE_FLARED_035',    182, 183),
    ('RAILINGS_036A',           183, 184),
    ('TRANSITIONS_036B',        184, 185),
    ('APPR_RAIL_036C',          185, 186),
    ('APPR_RAIL_END_036D',      186, 187),
    ('HISTORY_037',             187, 188),
    ('NAVIGATION_038',          188, 189),
    ('NAV_VERT_CLR_MT_039',     189, 193),
    ('NAV_HORR_CLR_MT_040',     193, 198),
    ('OPEN_CLOSED_POSTED_041',  198, 199),
    ('SERVICE_ON_042A',         199, 200),
    ('SERVICE_UND_042B',        200, 201),
    ('STRUCTURE_KIND_043A',     201, 202),
    ('STRUCTURE_TYPE_043B',     202, 204),
    ('APPR_KIND_044A',          204, 205),
    ('APPR_TYPE_044B',          205, 207),
    ('MAIN_UNIT_SPANS_045',     207, 210),
    ('APPR_SPANS_046',          210, 214),
    ('HORR_CLR_MT_047',         214, 217),
    ('MAX_SPAN_LEN_MT_048',     217, 222),
    ('STRUCTURE_LEN_MT_049',    222, 228),
    ('LEFT_CURB_MT_050A',       228, 231),
    ('RIGHT_CURB_MT_050B',      231, 234),
    ('ROADWAY_WIDTH_MT_051',    234, 238),
    ('DECK_WIDTH_MT_052',       238, 242),
    ('VERT_CLR_OVER_MT_053',    242, 246),
    ('VERT_CLR_UND_REF_054A',   246, 247),
    ('VERT_CLR_UND_054B',       247, 251),
    ('LAT_UND_REF_055A',        251, 252),
    ('LAT_UND_MT_055B',         252, 255),
    ('LEFT_LAT_UND_MT_056',     255, 258),
    ('DECK_COND_058',           258, 259),
    ('SUPERSTRUCTURE_COND_059', 259, 260),
    ('SUBSTRUCTURE_COND_060',   260, 261),
    ('CHANNEL_COND_061',        261, 262),
    ('CULVERT_COND_062',        262, 263),
    ('OPR_RATING_METH_063',     263, 264),
    ('OPERATING_RATING_064',    264, 267),
    ('INV_RATING_METH_065',     267, 268),
    ('INVENTORY_RATING_066',    268, 271),
    ('STRUCTURAL_EVAL_067',     271, 272),
    ('DECK_GEOMETRY_EVAL_068',  272, 273),
    ('UNDCLRENCE_EVAL_069',     273, 274),
    ('POSTING_EVAL_070',        274, 275),
    ('WATERWAY_EVAL_071',       275, 276),
    ('APPR_ROAD_EVAL_072',      276, 277),
    ('WORK_PROPOSED_075A',      277, 279),
    ('WORK_DONE_BY_075B',       279, 280),
    ('IMP_LEN_MT_076',          280, 286),
    ('DATE_OF_INSPECT_090',     286, 290),
    ('INSPECT_FREQ_MONTHS_091', 290, 292),
    ('FRACTURE_092A',           292, 295),
    ('UNDWATER_LOOK_SEE_092B',  295, 298),
    ('SPEC_INSPECT_092C',       298, 301),
    ('FRACTURE_LAST_DATE_093A', 301, 305),
    ('UNDWATER_LAST_DATE_093B', 305, 309),
    ('SPEC_LAST_DATE_093C',     309, 313),
    ('BRIDGE_IMP_COST_094',     313, 319),
    ('ROADWAY_IMP_COST_095',    319, 325),
    ('TOTAL_IMP_COST_096',      325, 331),
    ('YEAR_OF_IMP_097',         331, 335),
    ('OTHER_STATE_CODE_098A',   335, 338),
    ('OTHER_STATE_PCNT_098B',   338, 340),
    ('OTHR_STATE_STRUC_NO_099', 340, 355),
    ('STRAHNET_HIGHWAY_100',    355, 356),
    ('PARALLEL_STRUCTURE_101',  356, 357),
    ('TRAFFIC_DIRECTION_102',   357, 358),
    ('TEMP_STRUCTURE_103',      358, 359),
    ('HIGHWAY_SYSTEM_104',      359, 360),
    ('FEDERAL_LANDS_105',       360, 361),
    ('YEAR_RECONSTRUCTED_106',  361, 365),
    ('DECK_STRUCTURE_TYPE_107', 365, 366),
    ('SURFACE_TYPE_108A',       366, 367),
    ('MEMBRANE_TYPE_108B',      367, 368),
    ('DECK_PROTECTION_108C',    368, 369),
    ('PERCENT_ADT_TRUCK_109',   369, 371),
    ('NATIONAL_NETWORK_110',    371, 372),
    ('PIER_PROTECTION_111',     372, 373),
    ('BRIDGE_LEN_IND_112',      373, 374),
    ('SCOUR_CRITICAL_113',      374, 375),
    ('FUTURE_ADT_114',          375, 381),
    ('YEAR_OF_FUTURE_ADT_115',  381, 385),
    ('MIN_NAV_CLR_MT_116',      385, 389),
    ('FED_AGENCY',              389, 390),
    ('SUBMITTED_BY',            390, 392),
    # 392-432: Washington HQ use (not mapped)
    ('BRIDGE_CONDITION',        433, 434),
    ('LOWEST_RATING',           434, 435),
    ('DECK_AREA',               435, 445),
]


def _is_fixed_width_nbi(first_line: str) -> bool:
    """
    Return True if the line looks like a fixed-width NBI record rather than CSV.

    Heuristics:
    - No commas in the first 200 characters (CSV files always have many)
    - First two characters are decimal digits (state code, e.g. '24' for MD)
    - Characters at positions 3-17 are alphanumeric (structure number field)
    - Character at position 18 is '1', '2', 'A', 'B', 'C', or ' ' (record type)
    """
    if ',' in first_line[:200]:
        return False
    if len(first_line) < 40:
        return False
    state = first_line[0:2]
    if not state.isdigit():
        return False
    struct = first_line[3:18].strip()
    if not struct or not re.match(r'^[A-Z0-9 _-]+$', struct, re.IGNORECASE):
        return False
    rec_type = first_line[18] if len(first_line) > 18 else ''
    if rec_type not in ('1', '2', 'A', 'B', 'C', 'D', ' ', '3', '4', '5'):
        return False
    return True


def _parse_fixed_width_record(line: str) -> Dict[str, Any]:
    """Parse one line of a fixed-width NBI file into a field dict."""
    bridge: Dict[str, Any] = {}

    def _get(start: int, end: int) -> Any:
        raw = line[start:end] if end <= len(line) else line[start:] if start < len(line) else ''
        v = raw.strip()
        return v if v else None

    for name, start, end in _NBI_FIXED_POSITIONS:
        bridge[name] = _get(start, end)

    return bridge


def _get_pymupdf_module():
    """Import PyMuPDF using either the new or legacy module name."""
    try:
        import pymupdf as fitz_module
        return fitz_module
    except ImportError:
        try:
            import fitz as fitz_module
            return fitz_module
        except ImportError as exc:
            raise RuntimeError(
                'PyMuPDF is required for PDF parsing. Install backend dependencies with: pip install -r backend/requirements.txt'
            ) from exc


def parse_nbi_txt(txt_path: Path) -> DefaultDict[str, Dict[str, Any]]:
    """
    Parse NBI TXT/CSV file into a dictionary of bridges.
    Each bridge has 'over' and 'unders' records.

    Supports three file layouts:
    1. CSV with a named NBI header row  → columns mapped by name.
    2. CSV without a header             → columns mapped by positional field_names.
    3. Fixed-width (no commas)          → columns mapped by empirically confirmed
                                          byte offsets (_NBI_FIXED_CONFIRMED +
                                          _NBI_FIXED_REMAINING).

    Layout 3 covers FHWA database-extract files (e.g. most modern state NBI
    downloads) where the structure number appears at bytes 3-17, record type at
    byte 18, coordinates at bytes 129-145, and year_built at bytes 156-159.
    """
    bridges: DefaultDict[str, Dict[str, Any]] = defaultdict(lambda: {'over': None, 'unders': []})

    try:
        with open(txt_path, 'r', encoding='utf-8', errors='ignore') as f:
            raw_lines = f.readlines()

        if not raw_lines:
            print("No data found in TXT file.")
            return bridges

        first_line = raw_lines[0].rstrip('\r\n')

        # ── Detect file format ────────────────────────────────────────────────
        first_row_csv = [c.strip() for c in next(csv.reader([first_line]))]

        if _NBI_HEADER_KEYS.intersection(first_row_csv):
            # Layout 1: CSV with named header
            fmt = 'csv-header'
            col_names = first_row_csv
            data_lines = raw_lines[1:]
            print(f"NBI header detected with {len(col_names)} columns.")
        elif _is_fixed_width_nbi(first_line):
            # Layout 3: fixed-width (no commas, no named header)
            fmt = 'fixed-width'
            data_lines = raw_lines
            print(f"Fixed-width NBI format detected (no commas). "
                  f"Using empirical byte offsets for {len(raw_lines)} records.")
        else:
            # Layout 2: CSV without header — use positional field_names
            fmt = 'csv-positional'
            col_names = field_names
            data_lines = raw_lines
            print(f"No NBI header found; using positional field_names ({len(col_names)} columns).")

        skipped_count = 0

        # ── Parse records ─────────────────────────────────────────────────────
        if fmt == 'fixed-width':
            for line in data_lines:
                line = line.rstrip('\r\n')
                if not line.strip():
                    continue
                bridge = _parse_fixed_width_record(line)

                struc = bridge.get('STRUCTURE_NUMBER_008')
                if not struc:
                    skipped_count += 1
                    continue

                rec_type = bridge.get('RECORD_TYPE_005A') or ''
                if rec_type == '1':
                    bridges[struc]['over'] = bridge
                else:
                    bridges[struc]['unders'].append(bridge)
        else:
            n_cols = len(col_names)
            reader = csv.reader(data_lines)
            for row in reader:
                row = [c.strip() for c in row]
                if len(row) < n_cols:
                    row = row + [None] * (n_cols - len(row))

                bridge: Dict[str, Any] = {}
                for col_name, value in zip(col_names, row):
                    bridge[col_name] = value if value else None

                struc = bridge.get('STRUCTURE_NUMBER_008')
                if not struc:
                    skipped_count += 1
                    continue
                struc = struc.strip()

                rec_type = bridge.get('RECORD_TYPE_005A')
                if rec_type == '1':
                    bridges[struc]['over'] = bridge
                else:
                    bridges[struc]['unders'].append(bridge)

        if skipped_count:
            print(f"Skipped {skipped_count} rows with missing STRUCTURE_NUMBER_008.")
        print(f"Successfully parsed {len(bridges)} bridges from TXT file.")
        return bridges

    except Exception as e:
        print(f"Error parsing NBI TXT file: {e}")
        return bridges


def parse_elements_xml(xml_path: Path) -> DefaultDict[str, List[Dict[str, str]]]:
    """
    Parse Element XML file and return elements grouped by structure number.
    """
    elements_by_struc: DefaultDict[str, List[Dict[str, str]]] = defaultdict(list)

    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()

        print(f"XML Root tag: {root.tag}")

        count = 0

        for fhwaed in root.findall('.//FHWAED'):
            struc_text = fhwaed.findtext('STRUCNUM', '').strip()
            if not struc_text:
                continue

            struc = struc_text
            count += 1

            el = {
                'BE01': fhwaed.findtext('EN', ''),
                'BE02': fhwaed.findtext('EPN', '0'),
                'BE03': fhwaed.findtext('TOTALQTY', '0'),
                'BCS01': fhwaed.findtext('CS1', '0'),
                'BCS02': fhwaed.findtext('CS2', '0'),
                'BCS03': fhwaed.findtext('CS3', '0'),
                'BCS04': fhwaed.findtext('CS4', '0')
            }

            elements_by_struc[struc].append(el)

        print(f"Parsed elements for {len(elements_by_struc)} bridges "
              f"({count} element records total) from XML.")
        return elements_by_struc

    except ET.ParseError as e:
        print(f"XML Parse Error: {e}")
        return elements_by_struc
    except FileNotFoundError:
        print(f"XML file not found: {xml_path}")
        return elements_by_struc
    except Exception as e:
        print(f"Unexpected error parsing XML: {e}")
        return elements_by_struc


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract plain text from PDF bytes using PyMuPDF."""
    text_parts: List[str] = []
    fitz = _get_pymupdf_module()
    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    try:
        for page in doc:
            text_parts.append(page.get_text("text") or "")
    finally:
        doc.close()
    return "\n".join(text_parts)


def _find_first(patterns: List[str], text: str) -> str | None:
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if m:
            return (m.group(1) or '').strip()
    return None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(str(value).strip()))
    except Exception:
        return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except Exception:
        return None


# ── Docling JSON extraction helpers ──────────────────────────────────────────

def _parse_element_condition_table(grid: list) -> List[Dict[str, Any]]:
    """Parse SNBI element condition state rows from a Docling table grid."""
    elements: List[Dict[str, Any]] = []
    if len(grid) < 2:
        return elements

    header = [c.get('text', '').strip() for c in grid[0]]

    try:
        cs1_idx = header.index('Condition State 1')
        cs2_idx = header.index('Condition State 2')
    except ValueError:
        return elements

    cs3_idx = header.index('Condition State 3') if 'Condition State 3' in header else None
    cs4_idx = header.index('Condition State 4') if 'Condition State 4' in header else None
    total_idx = header.index('Total Quantity') if 'Total Quantity' in header else None
    units_idx = header.index('Units') if 'Units' in header else None
    env_idx = header.index('Environment') if 'Environment' in header else None

    last_env: str | None = None
    last_units: str | None = None

    for row in grid[1:]:
        cells = [c.get('text', '').strip() for c in row]
        if not any(cells):
            continue

        name = cells[0] if cells else ''

        # Inherit environment/units from parent element rows (sub-defect rows leave these blank)
        if env_idx is not None and env_idx < len(cells) and cells[env_idx]:
            last_env = cells[env_idx]
        if units_idx is not None and units_idx < len(cells) and cells[units_idx]:
            last_units = cells[units_idx]

        if not name:
            continue

        m = re.match(r'^(\d{1,4})-(.+)$', name)
        if not m:
            continue

        elem_id = int(m.group(1))
        elem_name = m.group(2).strip()

        entry: Dict[str, Any] = {
            'element_id': elem_id,
            'element_name': elem_name,
            'is_defect_code': elem_id >= 1000,
            'environment': (
                cells[env_idx] if env_idx is not None and env_idx < len(cells) and cells[env_idx]
                else last_env
            ),
            'units': (
                cells[units_idx] if units_idx is not None and units_idx < len(cells) and cells[units_idx]
                else last_units
            ),
            'total_quantity': (
                _to_float(cells[total_idx])
                if total_idx is not None and total_idx < len(cells) else None
            ),
            'cs1': _to_float(cells[cs1_idx]) if cs1_idx < len(cells) else None,
            'cs2': _to_float(cells[cs2_idx]) if cs2_idx < len(cells) else None,
            'cs3': (
                _to_float(cells[cs3_idx])
                if cs3_idx is not None and cs3_idx < len(cells) else None
            ),
            'cs4': (
                _to_float(cells[cs4_idx])
                if cs4_idx is not None and cs4_idx < len(cells) else None
            ),
        }
        elements.append(entry)

    return elements


# FHWA NBI state FIPS codes used for state detection from text.
# Both spaced ("new jersey") and no-space ("newjersey") variants are included
# because Docling sometimes strips spaces from concatenated PDF words.
_STATE_FIPS: Dict[str, int] = {
    'new jersey': 34, 'newjersey': 34,
    'new york': 36,   'newyork': 36,
    'pennsylvania': 42,
    'maryland': 24,
    'virginia': 51,
    'west virginia': 54, 'westvirginia': 54,
    'connecticut': 9,
    'massachusetts': 25,
    'delaware': 10,
    'rhode island': 44, 'rhodeisland': 44,
    'new hampshire': 33, 'newhampshire': 33,
    'vermont': 50,
    'maine': 23,
    'california': 6,
    'texas': 48,
    'florida': 12,
    'ohio': 39,
    'michigan': 26,
    'illinois': 17,
    'georgia': 13,
    'north carolina': 37, 'northcarolina': 37,
    'washington': 53,
    'arizona': 4,
    'indiana': 18,
    'tennessee': 47,
    'missouri': 29,
    'wisconsin': 55,
    'minnesota': 27,
    'colorado': 8,
    'alabama': 1,
    'louisiana': 22,
    'kentucky': 21,
    'oregon': 41,
    'oklahoma': 40,
    'south carolina': 45, 'southcarolina': 45,
    'iowa': 19,
    'utah': 49,
    'nevada': 32,
    'arkansas': 5,
    'mississippi': 28,
    'kansas': 20,
    'new mexico': 35, 'newmexico': 35,
    'nebraska': 31,
    'idaho': 16,
    'hawaii': 15,
    'alaska': 2,
    'south dakota': 46, 'southdakota': 46,
    'north dakota': 38, 'northdakota': 38,
    'montana': 30,
    'wyoming': 56,
}


def _extract_docling_json(json_bytes: bytes) -> Dict[str, Any]:
    """Extract SNBI fields from a Docling JSON document.

    Handles the DoclingDocument schema (v1.x) produced by the `docling` library.
    Returns a dict of extracted fields; None values indicate not found.
    """
    try:
        data = json.loads(json_bytes.decode('utf-8', errors='replace'))
    except Exception:
        return {}

    # Validate it is a DoclingDocument
    if data.get('schema_name') != 'DoclingDocument':
        return {}

    texts: List[str] = [item.get('text', '').strip() for item in data.get('texts', [])]
    tables: List[Dict] = data.get('tables', [])

    # ── Bridge ID: standalone 7-digit structure number ────────────────────────
    bridge_number: str | None = None
    for t in texts:
        if re.fullmatch(r'\d{7}', t):
            bridge_number = t
            break

    # ── Inspection date: clean MM/DD/YYYY or YYYY-MM-DD pattern ──────────────
    inspection_date: str | None = None
    for t in texts:
        if re.fullmatch(r'\d{2}/\d{2}/\d{4}', t):
            inspection_date = t
            break
        if inspection_date is None and re.fullmatch(r'\d{4}-\d{2}-\d{2}', t):
            inspection_date = t

    # ── Report year ───────────────────────────────────────────────────────────
    report_year: int | None = None
    if inspection_date:
        m_yr = re.search(r'(\d{4})', inspection_date)
        if m_yr:
            report_year = _to_int(m_yr.group(1))

    # ── State code from readable text ─────────────────────────────────────────
    state_code: int | None = None
    for t in texts:
        tl = t.lower()
        for keyword, code in _STATE_FIPS.items():
            if keyword in tl:
                state_code = code
                break
        if state_code is not None:
            break

    # ── Element condition state table ─────────────────────────────────────────
    elements: List[Dict[str, Any]] = []
    for table in tables:
        grid = table.get('data', {}).get('grid', [])
        parsed = _parse_element_condition_table(grid)
        if parsed:
            elements = parsed
            break  # Use the first matching element table

    # ── Condition ratings from 3-column appraisal tables ─────────────────────
    # NBI inspection tables have columns: RATING | COMPONENT | REMARKS
    # The rating is a single digit 0-9; the component name may be garbled.
    # We match the garbled (ROT-3 encoded) component labels as well as plain text.
    _COMP_KEYS: Dict[str, str] = {
        # Plain (for any clean text) and ROT+3 garbled (NJ DOT font encoding)
        'deck': 'deck',             'ghfn': 'deck',
        'superstructure': 'super', 'vxshuvwuxfwxuh': 'super',
        'substructure': 'sub',     'vxevwuxfwxuh': 'sub',
        'culvert': 'culvert',      'fxoyhuw': 'culvert',
    }
    ratings: Dict[str, float | None] = {'deck': None, 'super': None, 'sub': None, 'culvert': None}

    for table in tables:
        grid = table.get('data', {}).get('grid', [])
        if len(grid) < 2 or len(grid[0]) < 3:
            continue
        for row in grid[1:]:
            cells = [c.get('text', '').strip() for c in row]
            if len(cells) < 2:
                continue
            rating_cell = cells[0]
            component_cell = cells[1].lower()
            if not re.fullmatch(r'[0-9]', rating_cell):
                continue
            for key, alias in _COMP_KEYS.items():
                if key in component_cell and ratings[alias] is None:
                    ratings[alias] = float(rating_cell)
                    break

    return {
        'bridge_number': bridge_number,
        'state_code': state_code,
        'inspection_date': inspection_date,
        'report_year': report_year,
        'deck': ratings['deck'],
        'superstructure': ratings['super'],
        'substructure': ratings['sub'],
        'culvert': ratings['culvert'],
        'elements': elements,
    }


def extract_inspection_reports_to_schema(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract key SNBI-like fields from uploaded inspection report files.

    Supported formats:
    - ``.pdf`` — raw PDF bytes, text extracted via pdfminer
    - ``.json`` — Docling JSON (DoclingDocument schema), structured extraction

    Input items must contain:
    - filename: str (can include folder path)
    - content: bytes
    """
    results: List[Dict[str, Any]] = []

    for item in files:
        filename = str(item.get('filename') or 'report.pdf')
        content = item.get('content')
        if not isinstance(content, (bytes, bytearray)):
            continue

        fname_lower = filename.lower()
        is_pdf = fname_lower.endswith('.pdf')
        is_docling_json = fname_lower.endswith('.json')

        if not is_pdf and not is_docling_json:
            continue

        folder = '/'.join(filename.replace('\\', '/').split('/')[:-1])
        file_only = filename.replace('\\', '/').split('/')[-1]

        # ── Docling JSON path ─────────────────────────────────────────────────
        if is_docling_json:
            extracted = _extract_docling_json(bytes(content))
            if not extracted:
                results.append({
                    'report_file': file_only,
                    'report_folder': folder or None,
                    'status': 'error',
                    'detail': 'Not a valid DoclingDocument JSON or failed to parse.',
                })
                continue

            bridge_number = extracted.get('bridge_number')
            state_code    = extracted.get('state_code')
            inspection_date = extracted.get('inspection_date')
            report_year   = extracted.get('report_year')
            deck          = extracted.get('deck')
            superstructure = extracted.get('superstructure')
            substructure  = extracted.get('substructure')
            culvert       = extracted.get('culvert')
            elements      = extracted.get('elements', [])

            schema_fields = {
                'BL01':  state_code,
                'BL02':  None,
                'BL03':  None,
                'BID01': bridge_number,
                'BID02': None,
                'BID03': None,
                'BID04': None,
                'BID05': None,
                'BID06': None,
                'BID07': None,
                'BIE01': None,
                'BIE03': inspection_date,
                'BW01':  report_year,
                'BW04':  None,
                'BC01':  deck,
                'BC02':  superstructure,
                'BC03':  substructure,
                'BC04':  culvert,
                'BC13':  None,
                'BH09':  None,
                'BCL01': None,
            }
            present_count = sum(1 for v in schema_fields.values() if v not in (None, '', []))
            confidence = round(present_count / len(schema_fields), 3)

            results.append({
                'status': 'success',
                'report_folder': folder or None,
                'report_file': file_only,
                'state_code': state_code,
                'bridge_number': bridge_number,
                'bridge_name': None,
                'route_prefix': None,
                'route_number': None,
                'features_desc': None,
                'facility_carried': None,
                'location_desc': None,
                'county': None,
                'highway_district': None,
                'inspection_type': None,
                'report_year': report_year,
                'inspection_date': inspection_date,
                'year_built': None,
                'deck_condition_rating': deck,
                'superstructure_condition_rating': superstructure,
                'substructure_condition_rating': substructure,
                'culvert_condition_rating': culvert,
                'lowest_condition_rating_code': None,
                'annual_average_daily_traffic': None,
                'owner': None,
                'confidence': confidence,
                'schema_fields': schema_fields,
                'elements': elements,
            })
            continue

        # ── PDF path ──────────────────────────────────────────────────────────
        try:
            text = _extract_pdf_text(bytes(content))
        except Exception as e:
            results.append({
                'report_file': file_only,
                'report_folder': folder or None,
                'status': 'error',
                'detail': f'Failed to parse PDF: {e}',
            })
            continue

        state_code = _to_int(_find_first([
            r"\bstate\s*code\s*[:#-]?\s*(\d{1,2})\b",
            r"\bBL01\s*[:#-]?\s*(\d{1,2})\b",
        ], text))
        bridge_number = _find_first([
            r"\bbridge\s*number\s*[:#-]?\s*([A-Za-z0-9\-_/]+)",
            r"\bstructure\s*number\s*[:#-]?\s*([A-Za-z0-9\-_/]+)",
            r"\bBID01\s*[:#-]?\s*([A-Za-z0-9\-_/]+)",
        ], text)
        bridge_name = _find_first([
            r"\bbridge\s*name\s*[:#-]?\s*([^\n\r]+)",
            r"\bstructure\s*name\s*[:#-]?\s*([^\n\r]+)",
            r"\bBID02\s*[:#-]?\s*([^\n\r]+)",
        ], text)
        inspection_date = _find_first([
            r"\binspection\s*date\s*[:#-]?\s*(\d{4}-\d{1,2}-\d{1,2})",
            r"\binspection\s*date\s*[:#-]?\s*(\d{1,2}/\d{1,2}/\d{4})",
            r"\bBIE03\s*[:#-]?\s*([0-9\-/]{8,10})",
        ], text)
        report_year = _to_int(_find_first([
            r"\breport\s*year\s*[:#-]?\s*(\d{4})\b",
            r"\binspection\s*year\s*[:#-]?\s*(\d{4})\b",
            r"\b(20\d{2}|19\d{2})\b",
        ], inspection_date or text or file_only))

        deck = _to_float(_find_first([
            r"\bdeck\s*condition\s*(?:rating)?\s*[:#-]?\s*([0-9](?:\.[0-9])?)\b",
            r"\bBC01\s*[:#-]?\s*([0-9](?:\.[0-9])?)\b",
        ], text))
        superstructure = _to_float(_find_first([
            r"\bsuperstructure\s*condition\s*(?:rating)?\s*[:#-]?\s*([0-9](?:\.[0-9])?)\b",
            r"\bBC02\s*[:#-]?\s*([0-9](?:\.[0-9])?)\b",
        ], text))
        substructure = _to_float(_find_first([
            r"\bsubstructure\s*condition\s*(?:rating)?\s*[:#-]?\s*([0-9](?:\.[0-9])?)\b",
            r"\bBC03\s*[:#-]?\s*([0-9](?:\.[0-9])?)\b",
        ], text))
        culvert = _to_float(_find_first([
            r"\bculvert\s*condition\s*(?:rating)?\s*[:#-]?\s*([0-9](?:\.[0-9])?)\b",
            r"\bBC04\s*[:#-]?\s*([0-9](?:\.[0-9])?)\b",
        ], text))
        lowest = _to_float(_find_first([
            r"\blowest\s*condition\s*(?:rating)?\s*[:#-]?\s*([0-9](?:\.[0-9])?)\b",
            r"\bBC13\s*[:#-]?\s*([0-9](?:\.[0-9])?)\b",
        ], text))
        owner = _find_first([
            r"\bowner\s*[:#-]?\s*([^\n\r]+)",
            r"\bBCL01\s*[:#-]?\s*([^\n\r]+)",
        ], text)
        _aadt_raw = _find_first([
            r"\bAADT\s*[:#-]?\s*([0-9,]+(?:\.[0-9]+)?)",
            r"\bannual\s+average\s+daily\s+traffic\s*[:#-]?\s*([0-9,]+(?:\.[0-9]+)?)",
            r"\bBH09\s*[:#-]?\s*([0-9,]+(?:\.[0-9]+)?)",
        ], text)
        aadt = _to_float(_aadt_raw.replace(',', '') if _aadt_raw else None)

        # ── Additional identity / location fields ─────────────────────────────
        route_prefix = _find_first([
            r"\broute\s*(?:prefix|signing)?\s*[:#-]?\s*(I|US|SR|SRT|CR|PR|OTHER)\b",
            r"\b(I|US|SR|SRT|CR|PR)\s*[-\s]*\d",
            r"\bBID03\s*[:#-]?\s*([^\n\r]{1,20})",
        ], text)
        route_number = _find_first([
            r"\b(?:I|US|SR|SRT|RT|RTE|HWY|ROUTE|ROAD)\s*[-#]?\s*(\d{1,4}[A-Z]?)\b",
            r"\broute\s*(?:number|no\.?|#)?\s*[:#-]?\s*(\d{1,4}[A-Z]?)\b",
            r"\bBID04\s*[:#-]?\s*([^\n\r]{1,30})",
        ], text)
        features_desc = _find_first([
            r"\bfeatures?\s*(?:intersected|description|desc)?\s*[:#-]?\s*([^\n\r]{3,80})",
            r"\bover\s*[:#-]?\s*([^\n\r]{3,80})",
            r"\bBID05\s*[:#-]?\s*([^\n\r]{3,80})",
        ], text)
        facility_carried = _find_first([
            r"\bfacility\s+carried\s*[:#-]?\s*([^\n\r]{3,80})",
            r"\bcarries?\s*[:#-]?\s*([^\n\r]{3,80})",
            r"\bBID06\s*[:#-]?\s*([^\n\r]{3,80})",
        ], text)
        location_desc = _find_first([
            r"\blocation\s*[:#-]?\s*([^\n\r]{3,80})",
            r"\bBID07\s*[:#-]?\s*([^\n\r]{3,80})",
        ], text)
        county = _find_first([
            r"\bcounty\s*[:#-]?\s*([A-Za-z\s]{3,40})(?:\s+county|\s*,|\s*\n|\s*$)",
            r"\bBL03\s*[:#-]?\s*([^\n\r]{1,40})",
        ], text)
        highway_district = _find_first([
            r"\bhighway\s+district\s*[:#-]?\s*(\d{1,3})\b",
            r"\bdistrict\s*[:#-]?\s*(\d{1,3})\b",
            r"\bBL02\s*[:#-]?\s*([^\n\r]{1,20})",
        ], text)
        inspection_type = _find_first([
            r"\binspection\s+type\s*[:#-]?\s*([^\n\r]{2,40})",
            r"\btype\s+of\s+inspection\s*[:#-]?\s*([^\n\r]{2,40})",
            r"\bBIE01\s*[:#-]?\s*([^\n\r]{1,40})",
        ], text)
        year_built = _to_int(_find_first([
            r"\byear\s*(?:built|constructed|of\s+construction)\s*[:#-]?\s*((?:18|19|20)\d{2})\b",
            r"\bbuilt\s*in\s*((?:18|19|20)\d{2})\b",
            r"\bBW04\s*[:#-]?\s*(\d{4})\b",
        ], text))

        schema_fields = {
            'BL01':  state_code,
            'BL02':  highway_district,
            'BL03':  county,
            'BID01': bridge_number,
            'BID02': bridge_name,
            'BID03': route_prefix,
            'BID04': route_number,
            'BID05': features_desc,
            'BID06': facility_carried,
            'BID07': location_desc,
            'BIE01': inspection_type,
            'BIE03': inspection_date,
            'BW01':  report_year,
            'BW04':  year_built,
            'BC01':  deck,
            'BC02':  superstructure,
            'BC03':  substructure,
            'BC04':  culvert,
            'BC13':  lowest,
            'BH09':  aadt,
            'BCL01': owner,
        }

        present_count = sum(1 for v in schema_fields.values() if v not in (None, '', []))
        confidence = round(present_count / len(schema_fields), 3)

        results.append({
            'status': 'success',
            'report_folder': folder or None,
            'report_file': file_only,
            'state_code': state_code,
            'bridge_number': bridge_number,
            'bridge_name': bridge_name,
            'route_prefix': route_prefix,
            'route_number': route_number,
            'features_desc': features_desc,
            'facility_carried': facility_carried,
            'location_desc': location_desc,
            'county': county,
            'highway_district': highway_district,
            'inspection_type': inspection_type,
            'report_year': report_year,
            'inspection_date': inspection_date,
            'year_built': year_built,
            'deck_condition_rating': deck,
            'superstructure_condition_rating': superstructure,
            'substructure_condition_rating': substructure,
            'culvert_condition_rating': culvert,
            'lowest_condition_rating_code': lowest,
            'annual_average_daily_traffic': aadt,
            'owner': owner,
            'confidence': confidence,
            'schema_fields': schema_fields,
        })

    return results