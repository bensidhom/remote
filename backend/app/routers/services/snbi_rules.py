"""Official SNBI transition maps and validation metadata.

Values here are based on FHWA SNBI Data Crosswalk, Detailed Code Mapping, and
Data Validation Logic v1.1. Only deterministic local rules are encoded here;
external checks such as HPMS/LRS lookup remain advisory in the validator.
"""

from typing import Any, Dict, Optional, Set


TEMPORARY_CODES: Set[str] = {
    "1-T", "2-T", "7-T", "A-T", "AD-T", "AI-T", "AT-T", "BM-T", "BS-T",
    "C-T", "CP-T", "CR-T", "F-T", "GB-T", "GC-T", "GT-T", "I0-T",
    "L-T", "M-T", "MA-T", "MI-T", "MO-T", "MX-T", "P-T", "PA-T",
    "PP-T", "PR-T", "S-T", "T-T", "X-T", "Y-T", "Z-T", "T-U",
    "VLM-T", "HVH-T", "AB-T", "BCE-T", "CD-T",
}


OWNER_MAINTENANCE_CODES: Dict[str, str] = {
    "01": "S01", "02": "L01", "03": "L02", "04": "L03",
    "11": "S02", "12": "L04", "21": "SX", "25": "LX",
    "26": "P", "27": "R", "31": "S03", "32": "L05",
    "40": "T", "54": "F08", "55": "F09", "56": "F07",
    "57": "F03", "58": "F05", "59": "D05", "60": "FX",
    "61": "I", "62": "FL01", "63": "FL04", "64": "FL07",
    "66": "FL05", "67": "F06", "68": "FL02", "69": "FL03",
    "70": "FL06", "72": "D01", "73": "D03", "74": "D02",
    "75": "F04", "76": "T", "77": "D04", "78": "F01",
    "79": "F02", "80": "U",
}

FEDERAL_LAND_CODES = {
    "0": "N", "1": "BIA", "2": "USFS", "3": "BLM",
    "4": "BIA|USFS", "5": "BIA|BLM", "6": "BLM|USFS",
    "9": "BIA|BLM|USFS",
}

HISTORIC_CODES = {"1": "1", "2": "2", "3": "T", "4": "7", "5": "N"}
TOLL_CODES = {"1": "1", "2": "3", "3": "N", "4": "4", "5": "2"}
BRIDGE_RAILING_CODES = {"0": "I0-T", "1": "Y-T", "N": "N"}
SPAN_MATERIAL_CODES = {
    "0": "X-T", "1": "CR-T", "2": "CR-T", "3": "S-T",
    "4": "S-T", "5": "CP-T", "6": "CP-T", "7": "T-T",
    "8": "M-T", "9": "AI-T",
}
SPAN_TYPE_CODES = {
    "00": "X-T", "01": "S-T", "02": "GB-T", "03": "G09",
    "04": "GT-T", "05": "BM-T", "06": "BS-T", "07": "F-T",
    "08": "S-T", "09": "T01", "10": "T-T", "11": "AD-T",
    "12": "AT-T", "13": "L01", "14": "L-T", "15": "M01",
    "16": "M02", "17": "M03", "19": "C-T", "20": "Z-T",
    "21": "B04", "22": "GC-T",
}
DECK_MATERIAL_CODES = {
    "1": "CR-T", "2": "CP-T", "3": "S01", "4": "S02",
    "6": "S05", "7": "A01", "8": "T-T", "9": "X-T",
    "N": "0",
}
WEARING_SURFACE_CODES = {
    "0": "0", "1": "C01", "2": "C02", "3": "C-T", "4": "C04",
    "5": "P-T", "6": "B01", "7": "T01", "8": "E01", "9": "X-T",
}
DECK_MEMBRANE_CODES = {"1": "M01", "2": "M02", "3": "MX-T", "8": "MU", "9": "MX-T"}
DECK_REINFORCING_CODES = {
    "0": "0", "1": "C01", "2": "C02", "3": "C-T",
    "4": "S-T", "6": "0", "7": "0", "8": "X-T", "9": "X-T",
}
FUNCTIONAL_CLASS_CODES = {
    "01": "1", "02": "3", "06": "4", "07": "5", "08": "6", "09": "7",
    "11": "1", "12": "2", "14": "3", "16": "4", "17": "C-T", "19": "7",
}
URBAN_CODE_CODES = {
    "01": "99999", "02": "99999", "06": "99999", "07": "99999", "08": "99999", "09": "99999",
    "11": "T-U", "12": "T-U", "14": "T-U", "16": "T-U", "17": "T-U", "19": "T-U",
}
NHS_CODES = {"0": "N", "1": "Y"}
NHFN_CODES = {"0": "N", "1": "1-T"}
STRAHNET_CODES = {"0": "N", "1": "1", "2": "1", "3": "2"}
ROUTE_DIRECTION_CODES = {"1": "1-T", "2": "2-T", "3": "2-T"}
# NBI Item 5E directional suffix appended to B.RT.02 (0 -> no suffix).
ROUTE_SUFFIX_CODES = {"1": "N", "2": "E", "3": "S", "4": "W"}
# NBI Item 111 -> B.N.06 (null -> no value).
NAVIGATION_PROTECTION_CODES = {"1": "1-T", "2": "2", "3": "3", "4": "4", "5": "5"}
ROUTE_TYPE_CODES = {"1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6", "7": "7", "8": "X"}
SERVICE_TYPE_CODES = {"0": "X", "1": "1", "2": "2", "3": "3", "4": "4", "6": "6", "7": "7", "8": "8"}
NAVIGATION_CODES = {"0": "N", "1": "Y", "N": None}
DESIGN_LOAD_CODES = {
    "0": "U", "1": "H10", "2": "H15", "3": "HS15", "4": "H20",
    "5": "HS20", "6": "HS20M", "8": "RR", "9": "HS20Plus",
    "A": "HL93", "B": "HL93Plus", "C": "X",
}
LOAD_RATING_METHOD_CODES = {
    "0": "EJ", "1": "LFR", "2": "ASR", "3": "LRFR", "4": "LT",
    "5": "N", "6": "LFR", "7": "ASR", "8": "LRFR",
    "A": "AR", "B": "AR", "C": "AR", "D": "AR", "E": "AR", "F": "AR",
}
POSTING_STATUS_CODES = {
    "A": "PO", "B": "PA-T", "D": "SO", "E": "TO", "G": "N",
    "K": "C", "P": "PP-T", "R": "PR-T",
}
# NBI Item 113 -> B.AP.03 per FHWA Detailed Code Mapping tab "B.C.11 and B.AP.03".
SCOUR_VULNERABILITY_CODES = {
    "N": None, "U": "U", "T": "0",
    "9": "A", "8": "AB-T", "7": "BCE-T", "6": "0",
    "5": "AB-T", "4": "AB-T", "3": "CD-T", "2": "CD-T",
    "1": "CD-T", "0": "CD-T",
}
# NBI Item 113 -> B.C.11 per FHWA Detailed Code Mapping tab "B.C.11 and B.AP.03".
SCOUR_CONDITION_CODES = {
    "N": "N", "U": None, "T": None,
    "9": "9", "8": "MI-T", "7": "MI-T", "6": None,
    "5": "MI-T", "4": "MO-T", "3": "MI-T", "2": "MA-T",
    "1": "1", "0": "0",
}


CODE_LISTS: Dict[str, Set[str]] = {
    "BCL01": set(OWNER_MAINTENANCE_CODES.values()) | {"FL0X", "DX"},
    "BCL02": set(OWNER_MAINTENANCE_CODES.values()) | {"FL0X", "DX"},
    "BCL03": set(FEDERAL_LAND_CODES.values()) | {"NPS", "USACE", "USBR", "USFWS", "X"},
    "BCL04": {"1", "2", "3", "4", "5", "6", "7", "N", "T"},
    "BCL05": {"1", "2", "3", "4", "N"},
    "BCL06": {"Y", "N"},
    "BG10": {"0", "1", "2", "3"},
    "BG12": {"Y", "N"},
    "BG14": {"Y", "N"},
    "BSP01": {"M##", "A##", "C##", "V##", "W##"},
    "BSP04": set(SPAN_MATERIAL_CODES.values()) | {
        "A01", "C01", "C02", "C03", "C04", "C05", "CX", "F01", "F02", "F03", "FX",
        "I01", "I02", "M01", "M02", "P01", "PX", "S01", "S02", "S03", "S04",
        "S05", "SX", "T01", "T02", "T03", "T04", "TX", "X",
    },
    "BSP05": {"1", "2", "3", "4", "5", "6", "7", "C-T", "7-T"},
    "BSP06": set(SPAN_TYPE_CODES.values()) | {
        "A01", "A02", "A03", "A04", "A05", "B01", "B02", "B03", "F01", "F02",
        "F03", "F04", "G01", "G02", "G03", "G04", "G05", "G06", "G07",
        "G08", "G10", "GX", "L02", "L03", "LX", "MX", "P01", "P02",
        "S01", "S02", "T02", "T03", "X01", "X02", "X03", "X",
    },
    "BSP07": {"0", "C01", "C02", "C03", "CX", "P01", "P02", "PX", "S01", "S02", "SX", "X"},
    "BSP08": {"0", "IM", "CU", "NI", "NA"},
    "BSP13": {"0", "1", "2", "3", "4", "X"},
    "BSP09": set(DECK_MATERIAL_CODES.values()) | {
        "C01", "C02", "C03", "C04", "C05", "CX", "F01", "F02", "F03", "FX",
        "S04", "S-T", "SX", "T01", "T02", "T03", "T04", "TX", "X",
    },
    "BSP10": set(WEARING_SURFACE_CODES.values()) | {"C03", "C05", "C06", "C07", "CX", "CU", "P01", "P02", "PX", "S01"},
    "BSP11": set(DECK_MEMBRANE_CODES.values()) | {"0", "A01", "A02", "A03", "A04", "A05", "AX", "C01", "C02", "C03", "CX", "M03", "MX", "P01", "X"},
    "BSP12": set(DECK_REINFORCING_CODES.values()) | {"C03", "CX", "R01", "R02", "R03", "R04", "R05", "R06", "R07", "RX", "S01", "S02", "SX", "X"},
    "BH01": {"1", "2", "3", "4", "5", "6", "7", "C-T"},
    "BH03": {"N", "Y"},
    "BH04": {"1", "2", "3", "4", "N", "1-T"},
    "BH05": {"1", "2", "N"},
    "BN01": {"N", "Y", "U"},
    "BN06": {"0", "1", "2", "3", "4", "5", "1-T"},
    "BRT01": {"R##"},
    "BRT03": {"NB", "EB", "SB", "WB", "NS", "EW", "1-T", "2-T"},
    "BRT04": {"1", "2", "3", "4", "5", "6", "7", "X"},
    "BRT05": {"1", "2", "3", "4", "6", "7", "8", "X"},
    "BLR01": set(DESIGN_LOAD_CODES.values()),
    "BLR02": {"ASD", "LFD", "LRFD", "N"},
    "BLR04": set(LOAD_RATING_METHOD_CODES.values()),
    "BLR08": {"A", "B", "C", "N"},
    "BIR01": {"N", "Y"},
    "BIR02": {"N", "Y"},
    "BIR03": {"N", "Y"},
    "BIR04": {"N", "Y"},
    "BIE01": {"1", "2", "3", "4", "5", "6", "7", "8", "9"},
    "BIE07": {"N", "Y", "1", "2", "3"},
    "BEP03": {"G", "A", "D", "T", "C", "S", "L", "V", "X"},
    "BPS01": {"N", "PO", "PA", "PP", "PR", "PD", "PM", "C", "TO", "TA", "TP", "TR", "TD", "TM", "SO", "SA", "SD", "SM", "PA-T", "PP-T", "PR-T"},
    "BW03": {
        # RECONSTRUCTED is FHWA's transition placeholder for Item 106 values;
        # not an SNBI code, users refine it later.
        "RECONSTRUCTED",
        "0", "BR1", "IP1", "IP2", "IP3", "IP4",
        "DK1", "DK2", "DK3", "DK4", "DK5", "DK6", "DK7",
        "SP1", "SP2", "SP3", "SP5", "SP6", "SP7",
        "SB1", "SB2", "SB3", "SB5", "SB6", "SB7",
        "CU2", "CU3", "CU4", "CU5", "CU6", "CU7",
        "BG1", "BG2", "JT1", "JT2", "RT1", "RT2",
        "SC1", "SC2", "CP1", "CP2", "CH1",
    },
    "BC01": {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "N"},
    "BC02": {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "N"},
    "BC03": {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "N"},
    "BC04": {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "N"},
    "BC05": {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "N"},
    "BC06": {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "N"},
    "BC07": {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "N"},
    "BC08": {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "N"},
    "BC09": {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "N"},
    "BC10": {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "N"},
    "BC11": {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "N", "MA-T", "MI-T", "MO-T"},
    "BAP01": {"G", "F", "P"},
    "BAP02": {"0", "1", "2", "3", "4", "5", "6", "VLM-T", "HVH-T"},
    "BAP03": {"0", "A", "B", "C", "D", "E", "U", "AB-T", "BCE-T", "CD-T"},
    "BAP04": {"0", "N", "Y", "Y-T"},
    "BAP05": {"0", "A", "B", "C", "D", "N"},
    "BC14": {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "N"},
    "BC15": {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "N"},
    "BSB01": {"A##", "P##", "W##"},
    "BSB03": {"0", "A01", "C01", "C02", "C03", "C04", "C05", "CX", "E01", "F01", "F02", "F03", "FX", "I01", "I02", "M01", "M02", "P01", "PX", "S01", "S02", "S03", "S04", "S05", "S06", "SX", "T01", "T02", "T03", "T04", "TX", "X"},
    "BSB04": {"A##", "B##", "AX", "P##", "PX", "W##", "WX", "C##", "CX", "X"},
    "BSB05": {"0", "C01", "C02", "C03", "CX", "P01", "P02", "PX", "S01", "S02", "SX", "X"},
    "BSB06": {"S01", "S02", "S03", "SX", "D01", "D02", "DX", "P01", "P02", "PX", "MX", "X"},
    "BSB07": {"0", "C01", "C02", "C03", "CX", "P01", "P02", "PX", "S01", "S02", "SX", "X"},
}


FIELD_GUIDANCE: Dict[str, Dict[str, str]] = {
    "BIE02": {"expected": "YYYYMMDD", "hint": "Use a valid inspection begin date such as 20230701."},
    "BIE03": {"expected": "YYYYMMDD", "hint": "Use a valid inspection completion date such as 20230701."},
    "BLR03": {"expected": "YYYYMMDD", "hint": "Use a valid load rating date, or leave blank if unavailable."},
    "BAP05": {"expected": "0, N, A, B, C, or D", "hint": "Use 0 when seismic evaluation is not completed, N when no seismic evaluation is required, or A-D after an evaluation."},
    "BG12": {"expected": "Y or N", "hint": "Y if the bridge has a horizontally curved alignment; N otherwise."},
    "BG13": {"expected": "Whole feet", "hint": "Maximum bridge height (top of deck minus low point in feet)."},
    "BG14": {"expected": "Y or N", "hint": "Y for sidehill bridges (bridge over a slope on a hillside); N otherwise."},
    "BCL06": {"expected": "Y or N", "hint": "Y if designated emergency evacuation route; N otherwise."},
    "BLR02": {"expected": "ASD, LFD, LRFD, or N", "hint": "Design specification used: ASD (pre-1979), LFD (1979-2000), LRFD (post-2000)."},
    "BLR07": {"expected": "Numeric factor truncated to hundredth", "hint": "Use the lowest legal-load rating factor. BLR06 is only a fallback when legal loads are enveloped by the operating rating."},
    "BLR08": {"expected": "A, B, C, or N", "hint": "Routine permit loads classification per AASHTO."},
    "BID03": {"expected": '0 or previous bridge number', "hint": "Report 0 when no previous bridge number exists."},
    "BSP03": {"expected": "Whole number", "hint": "Number of beam lines or stringer lines in this span set."},
    "BSP07": {"expected": "0 or code", "hint": "Span protective system code, 0 if none."},
    "BSB01": {"expected": "Substructure designation (SS01..)", "hint": "Use SS01, SS02, ... for each substructure set."},
    "BL07": {"expected": 'Bridge number, "N", or "0"', "hint": 'Use "N" when there is no border crossing, "0" when the bordering country has no bridge number.'},
    "BH06": {"expected": "LRS Route ID up to 120 chars", "hint": "Use the state's Linear Reference System ID. Fallback synthesized from route number if unavailable."},
    "BIE04": {"expected": "Certified inspector identifier", "hint": "Required for inspections requiring a team leader; default 'N' when not applicable."},
}


def clean_code(value: Any, width: Optional[int] = None) -> str:
    text = str(value or "").strip().upper()
    if width and text and text.isdigit():
        text = text.zfill(width)
    return text

