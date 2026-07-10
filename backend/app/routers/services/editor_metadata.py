"""Editor-only metadata for SNBI fields.

This data is not part of the FHWA SNBI schema. It records where mapped values
came from and which values were temporary/default fills so the Editor can make
that visible without leaking the metadata into exports.
"""

from typing import Any, Dict, List, Optional, Set

from .snbi_rules import TEMPORARY_CODES
from .snbi_schema import NESTED_ARRAY_SPECS, TOP_LEVEL_FIELDS


MAIN_SOURCE_FIELDS: Dict[str, List[str]] = {
    "BL01": ["STATE_CODE_001"],
    "BID01": ["STRUCTURE_NUMBER_008"],
    "BID02": [],
    "BID03": [],
    "BL02": ["COUNTY_CODE_003"],
    "BL03": ["PLACE_CODE_004"],
    "BL04": ["HIGHWAY_DISTRICT_002"],
    "BL05": ["LAT_016", "STATE_CODE_001"],
    "BL06": ["LONG_017", "STATE_CODE_001"],
    "BL07": ["OTHR_STATE_STRUC_NO_099"],
    "BL08": ["OTHER_STATE_CODE_098A"],
    "BL09": ["OTHER_STATE_PCNT_098B"],
    "BL11": ["LOCATION_009"],
    "BCL01": ["OWNER_022"],
    "BCL02": ["MAINTENANCE_021"],
    "BCL03": ["FEDERAL_LANDS_105"],
    "BCL04": ["HISTORY_037"],
    "BCL05": ["TOLL_020"],
    "BCL06": [],
    "BRH01": ["RAILINGS_036A"],
    "BRH02": ["TRANSITIONS_036B"],
    "BG01": ["STRUCTURE_LEN_MT_049"],
    "BG02": ["STRUCTURE_LEN_MT_049"],
    "BG03": ["MAX_SPAN_LEN_MT_048"],
    "BG04": ["MAX_SPAN_LEN_MT_048", "MAIN_UNIT_SPANS_045", "APPR_SPANS_046"],
    "BG05": ["DECK_WIDTH_MT_052", "ROADWAY_WIDTH_MT_051", "APPR_WIDTH_MT_032"],
    "BG06": ["ROADWAY_WIDTH_MT_051"],
    "BG07": ["LEFT_CURB_MT_050A"],
    "BG08": ["RIGHT_CURB_MT_050B"],
    "BG09": ["APPR_WIDTH_MT_032"],
    "BG10": ["MEDIAN_CODE_033"],
    "BG11": ["DEGREES_SKEW_034"],
    "BG12": [],
    "BG13": [],
    "BG14": [],
    "BLR01": ["DESIGN_LOAD_031"],
    "BLR02": [],
    "BLR03": [],
    "BLR04": ["OPR_RATING_METH_063"],
    "BLR05": ["INVENTORY_RATING_066", "INV_RATING_METH_065"],
    "BLR06": ["OPERATING_RATING_064", "OPR_RATING_METH_063"],
    "BLR07": [],
    "BIR01": ["FRACTURE_092A"],
    "BIR02": [],
    "BIR03": ["UNDWATER_LOOK_SEE_092B"],
    "BIR04": [],
    "BC01": ["DECK_COND_058"],
    "BC02": ["SUPERSTRUCTURE_COND_059"],
    "BC03": ["SUBSTRUCTURE_COND_060"],
    "BC04": ["CULVERT_COND_062"],
    "BC05": ["RAILINGS_036A"],
    "BC06": ["TRANSITIONS_036B"],
    "BC07": [],
    "BC08": [],
    "BC09": ["CHANNEL_COND_061"],
    "BC10": [],
    "BC11": ["SCOUR_CRITICAL_113"],
    "BC14": [],
    "BC15": [],
    "BAP01": ["APPR_ROAD_EVAL_072"],
    "BAP02": ["WATERWAY_EVAL_071", "FUNCTIONAL_CLASS_026"],
    "BAP03": ["SCOUR_CRITICAL_113"],
    "BAP04": ["SCOUR_CRITICAL_113"],
    "BAP05": [],
    "BW01": ["YEAR_BUILT_027"],
}

FEATURE_SOURCE_FIELDS: Dict[str, List[str]] = {
    "BF01": ["SERVICE_ON_042A", "SERVICE_UND_042B"],
    "BF02": ["SERVICE_ON_042A", "SERVICE_UND_042B"],
    "BF03": ["FACILITY_CARRIED_007", "FEATURES_DESC_006A"],
    "BH01": ["FUNCTIONAL_CLASS_026"],
    "BH02": ["FUNCTIONAL_CLASS_026"],
    "BH03": ["HIGHWAY_SYSTEM_104"],
    "BH04": ["NATIONAL_NETWORK_110"],
    "BH05": ["STRAHNET_HIGHWAY_100"],
    "BH06": ["LRS_INV_ROUTE_013A", "SUBROUTE_NO_013B", "ROUTE_NUMBER_005D", "DIRECTION_005E"],
    "BH07": ["KILOPOINT_011"],
    "BH08": ["TRAFFIC_LANES_ON_028A"],
    "BH09": ["ADT_029"],
    "BH10": ["ADT_029", "PERCENT_ADT_TRUCK_109"],
    "BH11": ["YEAR_ADT_030"],
    "BH12": ["MIN_VERT_CLR_010"],
    "BH13": ["VERT_CLR_OVER_MT_053", "VERT_CLR_UND_REF_054A", "VERT_CLR_UND_054B"],
    "BH14": ["LAT_UND_REF_055A", "LEFT_LAT_UND_MT_056"],
    "BH15": ["LAT_UND_REF_055A", "LAT_UND_MT_055B"],
    "BH16": ["HORR_CLR_MT_047"],
    "BH17": ["DETOUR_KILOS_019"],
    "BRR01": [],
    "BRR02": ["VERT_CLR_UND_REF_054A", "VERT_CLR_UND_054B"],
    "BRR03": ["LAT_UND_REF_055A", "LAT_UND_MT_055B"],
    "BN01": ["NAVIGATION_038"],
    "BN02": ["NAV_VERT_CLR_MT_039", "MIN_NAV_CLR_MT_116", "STRUCTURE_TYPE_043B"],
    "BN03": ["NAV_VERT_CLR_MT_039", "STRUCTURE_TYPE_043B"],
    "BN04": ["NAV_HORR_CLR_MT_040"],
    "BN05": [],
    "BN06": ["PIER_PROTECTION_111"],
}

ROUTE_SOURCE_FIELDS: Dict[str, List[str]] = {
    "BRT01": [],
    "BRT02": ["ROUTE_NUMBER_005D", "DIRECTION_005E"],
    "BRT03": ["TRAFFIC_DIRECTION_102"],
    "BRT04": ["ROUTE_PREFIX_005B"],
    "BRT05": ["SERVICE_LEVEL_005C"],
}

SPAN_SOURCE_FIELDS: Dict[str, List[str]] = {
    "BSP01": ["STRUCTURE_TYPE_043B", "APPR_TYPE_044B"],
    "BSP02": ["MAIN_UNIT_SPANS_045", "APPR_SPANS_046"],
    "BSP03": [],
    "BSP04": ["STRUCTURE_KIND_043A", "APPR_KIND_044A"],
    "BSP05": ["STRUCTURE_KIND_043A", "STRUCTURE_TYPE_043B", "APPR_TYPE_044B"],
    "BSP06": ["STRUCTURE_TYPE_043B", "APPR_TYPE_044B"],
    "BSP07": [],
    "BSP09": ["DECK_STRUCTURE_TYPE_107"],
    "BSP10": ["SURFACE_TYPE_108A"],
    "BSP11": ["MEMBRANE_TYPE_108B", "DECK_PROTECTION_108C"],
    "BSP12": ["DECK_PROTECTION_108C"],
    "BSP13": [],
}

SUBSTRUCTURE_SOURCE_FIELDS: Dict[str, List[str]] = {
    "BSB01": ["MAIN_UNIT_SPANS_045"],
    "BSB02": ["MAIN_UNIT_SPANS_045", "APPR_SPANS_046"],
    "BSB03": ["STRUCTURE_KIND_043A"],
    "BSB04": [],
    "BSB05": [],
    "BSB06": [],
    "BSB07": [],
}

INSPECTION_SOURCE_FIELDS: Dict[str, List[str]] = {
    "BIE01": ["DATE_OF_INSPECT_090", "FRACTURE_LAST_DATE_093A", "UNDWATER_LAST_DATE_093B", "SPEC_LAST_DATE_093C"],
    "BIE02": ["DATE_OF_INSPECT_090"],
    "BIE03": ["DATE_OF_INSPECT_090"],
    "BIE04": [],
    "BIE05": ["INSPECT_FREQ_MONTHS_091"],
    "BIE07": [],
    "BIE11": [],
}

POSTING_STATUS_SOURCE_FIELDS: Dict[str, List[str]] = {
    "BPS01": ["OPEN_CLOSED_POSTED_041"],
    "BPS02": [],
}

WORK_SOURCE_FIELDS: Dict[str, List[str]] = {
    "BW02": ["YEAR_RECONSTRUCTED_106", "YEAR_BUILT_027"],
    "BW03": ["YEAR_RECONSTRUCTED_106"],
}

DEFAULT_NOTES: Dict[str, str] = {
    "BID03": "Default fill: report 0 when no previous bridge number exists.",
    "BL07": "Default fill: report N when no border crossing exists.",
    "BCL06": "No legacy source; code Y/N from emergency-route designations.",
    "BG12": "Default fill: N unless the bridge is known to be horizontally curved. Confirm with plans/GIS/design data.",
    "BG13": "No legacy source; measure maximum bridge height from geometry/design data.",
    "BG14": "Default fill: N unless the bridge is known to be a sidehill bridge. Confirm with site/geometry data.",
    "BAP05": "Default fill: 0 means seismic evaluation not completed. Replace with N or A-D when seismic evaluation data is available.",
    "BLR02": "No legacy source; code the design method from plans/rating reports.",
    "BLR07": "SNBI rule: report the lowest controlling legal-load rating factor truncated to the hundredth. If legal-load factors were not calculated and legal loads are enveloped by the operating rating, BLR06 may be used as a transition fallback.",
    "BLR08": "Default fill: N when routine permit load classification is unavailable. Confirm against permit-load/rating policy.",
    "BC14": "No legacy source; rate NSTM condition after the next NSTM inspection.",
    "BC15": "No legacy source; rate underwater condition after the next underwater inspection.",
    "BSP03": "No legacy source; enter the actual number of beam/stringer lines.",
    "BSP07": "No legacy source; enter the span protective system if any.",
    "BSP08": "No legacy source; enter deck interaction from design data.",
    "BSP13": "No legacy source; enter stay-in-place form use.",
    "BSB01": "Default fill: synthesized A##/P## substructure designation. Confirm against actual abutment/pier/wall grouping.",
    "BSB02": "Derived fill: estimated from NBI span counts. Confirm actual number of substructure units in this set.",
    "BSB03": "Default fill: X (unknown) because legacy NBI has no substructure material. Enter the actual material.",
    "BSB04": "Default fill: P01 when no direct substructure type exists in legacy NBI. Confirm actual substructure type.",
    "BSB05": "Default fill: 0 substructure protective system when unavailable. Confirm actual protective system.",
    "BSB06": "Default fill: X foundation type when unavailable. Confirm actual foundation type.",
    "BSB07": "Default fill: 0 foundation protective system when unavailable. Confirm actual foundation protective system.",
    "BN01": "Derived from NBI navigation item; default N when not navigable.",
    "BN03": "Defaulted from BN02 when movable span navigation clearance is required and no separate value exists.",
    "BN05": "Fallback to BN04/0 when minimum horizontal clearance is missing but required for navigable waterway.",
    "BN06": "Default fill: 0 when navigation protection source is missing.",
    "BRR01": "Default fill: F when legacy NBI only identifies a railroad under feature without a detailed railroad service subtype.",
    "BIE01": "Default fill: routine inspection event from legacy inspection date.",
    "BIE04": "Default fill: N when certified inspector identifier is not available.",
    "BIE07": "Default fill: N for non-risk-based inspection interval.",
    "BW03": "Default fill: 0 when no reportable work code is available.",
}

TRANSITION_NOTES: Dict[str, str] = {
    "BAP01": "From NBI Item 72 (Approach Roadway Alignment). Recoded per the FHWA code-mapping table.",
    "BAP02": "From NBI Items 71 + 26 per the FHWA two-key table (uses functional class to resolve 71 = 3/4/6). VLM-T and HVH-T are temporary codes to refine.",
    "BAP03": "From NBI Item 113, recoded per FHWA: 9->A, 8/5/4->AB-T, 7->BCE-T, 3/2/1/0->CD-T, U->U, T/6->0. Temporary codes must be resolved by a scour appraisal.",
    "BAP04": "From NBI Item 113 per FHWA: 4/5/8/9 -> 0 (POA not required); other values -> Y-T (POA required; confirm and refine).",
    "BAP05": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BC01": "From NBI Item 58 (Deck). Direct transition.",
    "BC02": "From NBI Item 59 (Superstructure). Direct transition.",
    "BC03": "From NBI Item 60 (Substructure). Direct transition.",
    "BC04": "From NBI Item 62 (Culvert). Direct transition.",
    "BC05": "From NBI Item 36A. FHWA: direct transition only if N (no railing); numeric legacy codes were safety-feature ratings, not condition ratings, and do not transition.",
    "BC06": "From NBI Item 36B. FHWA: direct transition only if N (no transition feature); numeric legacy codes do not transition.",
    "BC07": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BC08": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BC09": "From NBI Item 61 (Channel and Channel Protetion). Direct transition.",
    "BC10": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BC11": "From NBI Item 113 (Scour Critical Bridges), recoded per FHWA: 9->9, 8/7/5/3->MI-T, 4->MO-T, 2->MA-T, 1->1, 0->0; U/T/6 -> no value (scour appraisal needed).",
    "BC12": "From NBI Item CAT10 (Bridge Condition). Direct transition.",
    "BC13": "From NBI Item CAT23 (Lowest Condition Code). Direct transition.",
    "BC14": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BC15": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BCL01": "From NBI Item 22 (Owner). Recoded per the FHWA code-mapping table.",
    "BCL02": "From NBI Item 21 (Maintenance Responsibility). Recoded per the FHWA code-mapping table.",
    "BCL03": "From NBI Item 105 (Federal Lands Highway). Recoded per the FHWA code-mapping table.",
    "BCL04": "From NBI Item 37 (Historical Significance). Recoded per the FHWA code-mapping table.",
    "BCL05": "From NBI Item 20 (Toll). Recoded per the FHWA code-mapping table.",
    "BCL06": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BCS01": "From NBI Item CS1 (Element Quantity Condition State One). Direct transition.",
    "BCS02": "From NBI Item CS2 (Element Quantity Condition State Two). Direct transition.",
    "BCS03": "From NBI Item CS3 (Element Quantity Condition State Three). Direct transition.",
    "BCS04": "From NBI Item CS4 (Element Quantity Condition State Four). Direct transition.",
    "BE01": "From NBI Item EN (Element Number). Direct transition.",
    "BE02": "From NBI Item EPN (Element Parent Number). Direct transition.",
    "BE03": "From NBI Item TOTALQTY (Element Total Quantity). Direct transition.",
    "BEP01": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BEP02": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BEP03": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BEP04": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BF01": "Derived from Items 42A/42B per FHWA: carried H01 (+R01/P01 for 42A=4/5); below H02/R/P/W01/F01/X01 by 42B, with R02/P02 when the bridge also carries that mode.",
    "BF02": "From NBI Item 42A/42B (Type of Service). Recoded per the FHWA code-mapping table.",
    "BF03": "From NBI Item 6A/7 (Features Intersected / Facility Carried by Structure). Recoded per the FHWA code-mapping table.",
    "BG01": "From NBI Item 49 (Structure Length). Convert from metric to nearest tenth of a foot when Item 49 is greater than 9.1 meters. Otherwise no value.",
    "BG02": "From NBI Item 49 (Structure Length). Convert from metric to nearest tenth of a foot.",
    "BG03": "From NBI Item 48 (Length of Maximum Span). Convert from metric to nearest tenth of a foot.",
    "BG04": "From NBI Item 48 (Length of Maximum Span). Convert from metric to nearest tenth of a foot if Item 45 = 1 and Item 46 = 0. Otherwise, no value.",
    "BG05": "From NBI Item 52 (Deck Width, Out-to-Out). Convert from metric to nearest tenth of a foot. Null when Item 52 = 0.",
    "BG06": "From NBI Item 51 (Bridge Roadway Width, Curb-to-Curb). Convert from metric to nearest tenth of a foot.",
    "BG07": "From NBI Item 50A (Left curb or sidewalk width). Convert from metric to nearest tenth of a foot.",
    "BG08": "From NBI Item 50B (Right curb or sidewalk width). Convert from metric to nearest tenth of a foot.",
    "BG09": "From NBI Item 32 (Approach Roadway Width). Convert from metric to nearest tenth of a foot.",
    "BG10": "From NBI Item 33 (Bridge Median). Direct transition.",
    "BG11": "From NBI Item 34 (Skew). Direct transition.",
    "BG12": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BG13": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BG14": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BG15": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BG16": "Multiply Item 49 by Item 52 when Item 52 > 0. Otherwise multiply Item 49 by Item 32. Convert from metric to nearest tenth of a square foot.",
    "BH01": "From NBI Item 26 (Functional Classification of Inventory Route). Recoded per the FHWA code-mapping table. Assign to H01 feature.",
    "BH02": "From NBI Item 26 (Functional Classification of Inventory Route). Recoded per the FHWA code-mapping table. Assign to all H features.",
    "BH03": "From NBI Item 104 (Highway System of the Inventory Route). Recoded per the FHWA code-mapping table. Assign to H01 feature.",
    "BH04": "From NBI Item 110 (Designated National Network). Recoded per the FHWA code-mapping table. Assign to H01 feature.",
    "BH05": "From NBI Item 100 (STRAHNET Highway Designation). Recoded per the FHWA code-mapping table. Assign to H01 feature.",
    "BH06": "From NBI Items 13A+13B combined; values reducing to 0 are dropped. New Jersey keeps leading zeros (FHWA-documented exception).",
    "BH07": "From NBI Item 11 (Kilometerpoint). Convert from metric to nearest thousandth of a mile. Assign to H01 feature.",
    "BH08": "From NBI Item 28A (Lanes on Structure). Direct transition. Assign to H01 feature.",
    "BH09": "From NBI Item 29 (Average Daily Traffic). Direct transition. Trim leading zeroes. Assign to H01 feature.",
    "BH10": "From NBI Item 109 (Average Daily Truck Traffic). Multiply Item 109 (percentage) by Item 29. Assign to H01 feature.",
    "BH11": "From NBI Item 30 (Year of Average Daily Traffic). Direct transition. Assign to H01 feature.",
    "BH12": "From NBI Item 10 (Inventory Route, Minimum Vertical Clearance). Convert from metric to nearest tenth of a foot (round down). Use 99.9 for values of 30 meters or greater. Assign to H01 feature.",
    "BH13": "Carried highway: from Item 53. Under highway: from Item 54B only when reference feature 54A = H. Converted to feet, rounded down.",
    "BH14": "From Item 56 only when reference feature 55A = H; no value when 56 = 99.9 m. Converted to feet, rounded down.",
    "BH15": "From Item 55B only when reference feature 55A = H. Converted to feet, rounded down.",
    "BH16": "From NBI Item 47 (Inventory Route, Total Horizontal Clearance). Convert from metric to nearest tenth of a foot (round down). Use 99.9 for values of 30 meters or greater. Assign to H01 feature.",
    "BH17": "From NBI Item 19 (Bypass, Detour Length). 199 converts to 999. Otherwise convert from metric to nearest mile. Assign to H01 feature.",
    "BH18": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BID01": "From NBI Item 8 (Structure Number). Trimmed of leading/trailing spaces.",
    "BID02": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BID03": "Populate this field using the history of structure number changes that resides in the NBI from Structure Number Change submittals. Trim leading and trailing spaces and leading zeroes in accordance with the user's selection for Item B.ID.01.",
    "BIE01": "Inspection type per FHWA: 1 Initial, 2 Routine, 3 Underwater, 4 NSTM, 7 Special. Routine events come from Item 90; NSTM/underwater/special events from Items 92/93.",
    "BIE02": "From NBI Item 90/93 (Inspection Date / Critical Feature Inspection Date). Recoded per the FHWA code-mapping table.",
    "BIE03": "No FHWA transition; the converter uses the begin date as completion date because legacy Item 90/93 dates record when the inspection was performed.",
    "BIE04": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BIE05": "From NBI Item 91/92 (Designated Inspection Frequency / Critical Feature Inspection). Recoded per the FHWA code-mapping table.",
    "BIE06": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BIE07": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BIE08": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BIE09": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BIE10": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BIE11": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BIE12": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BIR01": "From NBI Item 92A (Critical Feature Inspection- Fracture Critical Details). Direct transition.",
    "BIR02": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BIR03": "From NBI Item 92B (Critical Feature Inspection - Underwater Inspection). Direct transition.",
    "BIR04": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BL01": "From NBI Item 1 (State Code). Trim leading zero, drop 3rd digit.",
    "BL02": "From NBI Item 3 (County (Parish) Code). Direct transition.",
    "BL03": "From NBI Item 4 (Place Code). Direct transition.",
    "BL04": "From NBI Item 2 (Highway Agency District). Trim leading zeroes.",
    "BL05": "From NBI Item 16 (Latitude). Convert to decimal degrees from degrees/minutes/seconds. Add negative if Item 1 = 60.",
    "BL06": "From NBI Item 17 (Longitude). Convert to decimal degrees from degrees/minutes/seconds. Add negative for all records except if Item 1 = 64, 66, 68, 69, or 70.",
    "BL07": "From NBI Item 99 (Border Bridge Structure Number). Trim leading and trailing spaces and leading zeroes.",
    "BL08": "From NBI Item 98A (Neighboring State Code). Trim leading zero, drop 3rd digit, change CAN to CA, MEX to MX.",
    "BL09": "From NBI Item 98B (Percent Responsibility). Recoded per the FHWA code-mapping table. Coding Guide uses FUNDING responsibility.",
    "BL10": "From NBI Item 1 (State Code). Trim leading zero, drop 3rd digit apply to bridges where Coding Guide Item 98A is not null.",
    "BL11": "From NBI Item 9 (Location). Direct transition.",
    "BL12": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BLR01": "From NBI Item 31 (Design Load). Recoded per the FHWA code-mapping table.",
    "BLR02": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BLR03": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BLR04": "From NBI Item 63 (Method Used To Determine Operating Rating). Recoded per the FHWA code-mapping table.",
    "BLR05": "From NBI Item 66 divided by 32.4 (rating-factor methods 6/7/8/D/E/F) or 32.67 tons per the FHWA transition memo.",
    "BLR06": "From NBI Item 64 divided by 32.4 (rating-factor methods 6/7/8/D/E/F) or 32.67 tons per the FHWA transition memo.",
    "BLR07": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BLR08": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BN01": "From NBI Item 38 (Navigation Control). Recoded per the FHWA code-mapping table. Assign value to W01 feature.",
    "BN02": "From NBI Item 39/43B/116 (Navigation Vertical Clearance / Type of Design/Construction / Minimum Navigation Vertical Clearance Vertical Lift Bridge). Recoded per the FHWA code-mapping table. Convert from metric to nearest tenth of a foot (round down). Assign value to W01 feature only if Item 38 = 1. Otherwise no value.",
    "BN03": "From NBI Item 39/43B (Navigation Vertical Clearance / Type of Design/Construction). Convert Item 39 from metric to nearest tenth of a foot (round down). Assign value to W01 feature only if Item 43B = 15 and Item 38 = 1. Otherwise no value.",
    "BN04": "From NBI Item 40 (Navigation Horizontal Clearance). Convert from metric to nearest tenth of a foot (round down). Assign value to W01 feature only if Item 38 = 1. Otherwise no value.",
    "BN05": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BN06": "From NBI Item 111 (Pier or Abutment Protection (for Navigation)). Recoded per the FHWA code-mapping table. Assign value to W01 feature only if Item 38 = 1. Otherwise no value.",
    "BPS01": "From NBI Item 41 (Structure Open, Posted, or Closed to Traffic). Recoded per the FHWA code-mapping table.",
    "BPS02": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BRH01": "From NBI Item 36A (Traffic Safety Features). Recoded per the FHWA code-mapping table.",
    "BRH02": "From NBI Item 36B (Traffic Safety Features). Recoded per the FHWA code-mapping table.",
    "BRR01": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BRR02": "From Item 54B only when reference feature 54A = R. Converted to feet, rounded down.",
    "BRR03": "From Item 55B only when reference feature 55A = R. Converted to feet, rounded down.",
    "BRT01": "R01. Assign to H01 feature.",
    "BRT02": "From Items 5D+5E: route number with leading zeros trimmed plus directional letter (1=N, 2=E, 3=S, 4=W).",
    "BRT03": "From NBI Item 102 (Direction of Traffic). Recoded per the FHWA code-mapping table. Assign to R01 route on H01 feature.",
    "BRT04": "From NBI Item 5B (Route Signing Prefix). Direct transition except 8 goes to X. Assign to R01 route on H01 feature.",
    "BRT05": "From NBI Item 5C (Designated Level of Service). Direct transition except 0 goes to X. Assign to R01 route on H01 feature.",
    "BSB01": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly. A single placeholder set with unknown (X) materials is emitted for refinement in the Editor.",
    "BSB02": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly. Estimated as spans + 1; confirm the actual number of substructure units.",
    "BSB03": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BSB04": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BSB05": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BSB06": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BSB07": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BSP01": "From NBI Item 43/44 (Structure Type, Main / Structure Types, Approach Spans). Recoded per the FHWA code-mapping table. Item 43 data will partially transition to a main or culvert span configuration data set. Item 44 data when present will partially transition to an approach or culvert span configuration data set. SNBI culvert extension and widening span configurations cannot be transitioned. Multiple main span or approach span configurations cannot be transitioned.",
    "BSP02": "From NBI Item 45/46 (Number of Spans in Main Unit / Number of Approach Spans). Use Item 45 value for M01 or C01, use Item 46 value for A01 or C02. SNBI culvert extension and widening span configurations cannot be transitioned. Multiple main span or approach span configurations cannot be transitioned.",
    "BSP03": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BSP04": "From NBI Item 43A/44A (Structure Type, Main / Structure Types, Approach Spans). Recoded per the FHWA code-mapping table. Use Item 43A value for M01 or C01, use Item 44A value for A01 or C02.",
    "BSP05": "From NBI Item 43/44 (Structure Type, Main / Structure Types, Approach Spans). Recoded per the FHWA code-mapping table. Use Item 43A/B value for M01 or C01, use Item 44A/B value for A01 or C02.",
    "BSP06": "From NBI Item 43B/44B (Structure Type, Main / Structure Types, Approach Spans). Recoded per the FHWA code-mapping table. Use Item 43B value for M01 or C01, use Item 44B value for A01 or C02.",
    "BSP07": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BSP08": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BSP09": "From NBI Item 107 (Deck Structure Type). Recoded per the FHWA code-mapping table. Use for all span sets.",
    "BSP10": "From NBI Item 108A (Type of Wearing Surface). Recoded per the FHWA code-mapping table. Use for all span sets. Also reference Item 107.",
    "BSP11": "From NBI Item 108B (Type of Membrane). Recoded per the FHWA code-mapping table. Use for all span sets. Also reference Item 107.",
    "BSP12": "From NBI Item 108C (Deck Protection). Recoded per the FHWA code-mapping table. Use for all span sets. Also reference Item 107.",
    "BSP13": "No legacy NBI source - FHWA: cannot be populated from Coding Guide data; collect and code directly.",
    "BW01": "From NBI Item 27 (Year Built). Direct transition.",
    "BW02": "From NBI Item 106 (Year Reconstructed). Direct transition if > 0. Otherwise no value.",
    "BW03": "FHWA transition writes the placeholder RECONSTRUCTED for Item 106 years (not an SNBI code - replace with the actual SNBI work codes). BR1 rows record original construction at year built.",
}


REVIEW_RULES: Dict[str, str] = {
    "BG12": "SNBI B.G.12 reports whether the bridge has horizontally curved alignment.",
    "BG13": "SNBI B.G.13 reports maximum bridge height in whole feet from top of deck to low point.",
    "BG14": "SNBI B.G.14 reports whether the bridge is a sidehill bridge.",
    "BLR08": "SNBI B.LR.08 reports routine permit load classification A, B, C, or N based on permit-load policy/rating analysis.",
    "BAP05": "SNBI B.AP.05 reports seismic vulnerability: 0 if evaluation not completed, N if no evaluation required, or A-D after evaluation.",
    "BSP03": "SNBI B.SP.03 reports the number of beam/stringer lines in the span configuration.",
    "BSP07": "SNBI B.SP.07 reports span protective system; 0 means none.",
    "BSP08": "SNBI B.SP.08 reports deck interaction for the span configuration.",
    "BSP13": "SNBI B.SP.13 reports deck stay-in-place forms; do not report when B.SP.09 is 0.",
    "BSB01": "SNBI B.SB.01 identifies each substructure set using A## for abutments, P## for piers/bents, or W## for walls.",
    "BSB02": "SNBI B.SB.02 reports the number of substructure units represented by the substructure set.",
    "BSB03": "SNBI B.SB.03 reports the predominant substructure material.",
    "BSB04": "SNBI B.SB.04 reports the substructure type/configuration.",
    "BSB05": "SNBI B.SB.05 reports the substructure protective system; 0 means none.",
    "BSB06": "SNBI B.SB.06 reports the foundation type.",
    "BSB07": "SNBI B.SB.07 reports the foundation protective system; 0 means none.",
}


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().strip("'\"").strip()
    return text or None


def _source_value(old: Dict[str, Any], fields: List[str]) -> Optional[str]:
    parts = []
    for field in fields:
        value = _clean(old.get(field))
        if value is not None:
            parts.append(f"{field}={value}")
    return "; ".join(parts) if parts else None


def _is_temp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return value in TEMPORARY_CODES or value.endswith("-T")


def _add_meta(
    rows: List[Dict[str, Any]],
    table_name: str,
    row_index: Optional[int],
    tag: str,
    value: Any,
    old: Dict[str, Any],
    source_fields: List[str],
    note: Optional[str] = None,
) -> None:
    source_value = _source_value(old, source_fields)
    requires_review = tag in REVIEW_RULES
    is_default = tag in DEFAULT_NOTES and (requires_review or not source_fields or source_value is None or note)
    is_temporary = _is_temp(value)
    transition = TRANSITION_NOTES.get(tag)
    if not (source_value or is_default or is_temporary or requires_review or transition):
        return
    rows.append({
        "table": table_name,
        "row_index": row_index,
        "tag": tag,
        "source_name": "NBI TXT" if source_value else None,
        "source_value": source_value,
        "note": note or DEFAULT_NOTES.get(tag),
        "is_temporary": is_temporary,
        "is_default": is_default,
        "requires_review": requires_review,
        "rule": REVIEW_RULES.get(tag),
        "transition": transition,
    })


def _missing_note_row(table_name: str, row_index: Optional[int], tag: str) -> Dict[str, Any]:
    """Metadata row for a field the converter left blank, explaining why."""
    return {
        "table": table_name,
        "row_index": row_index,
        "tag": tag,
        "source_name": None,
        "source_value": None,
        "note": DEFAULT_NOTES.get(tag),
        "is_temporary": False,
        "is_default": False,
        "requires_review": tag in REVIEW_RULES,
        "rule": REVIEW_RULES.get(tag),
        "transition": TRANSITION_NOTES.get(tag),
    }


def _add_row_metadata(
    rows: List[Dict[str, Any]],
    table_name: str,
    row_index: int,
    row: Dict[str, Any],
    old: Dict[str, Any],
    field_map: Dict[str, List[str]],
    all_fields: Optional[Set[str]] = None,
) -> None:
    for tag, value in row.items():
        if tag == "Routes":
            continue
        _add_meta(rows, table_name, row_index, tag, value, old, field_map.get(tag, []))
    # Explain blank fields that FHWA says cannot be populated from legacy data.
    if all_fields:
        for tag in sorted(all_fields - set(row.keys())):
            if TRANSITION_NOTES.get(tag, "").startswith("No legacy NBI source"):
                rows.append(_missing_note_row(table_name, row_index, tag))


def _num(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _add_blr07_metadata(rows: List[Dict[str, Any]], snbi: Dict[str, Any], old: Dict[str, Any]) -> None:
    rows[:] = [row for row in rows if not (row.get("table") == "bridges" and row.get("tag") == "BLR07")]

    posting_evals = snbi.get("PostingEvaluations") if isinstance(snbi.get("PostingEvaluations"), list) else []
    legal_load_factors = [
        factor for factor in (_num(row.get("BEP02")) for row in posting_evals if isinstance(row, dict))
        if factor is not None
    ]
    if legal_load_factors:
        source_value = f"min(BEP02)={int(min(legal_load_factors) * 100) / 100:.2f}"
        rows.append({
            "table": "bridges",
            "row_index": None,
            "tag": "BLR07",
            "source_name": "PostingEvaluations",
            "source_value": source_value,
            "note": "Book logic applied: BLR07 is the lowest reported legal-load rating factor from BEP02, truncated to the hundredth.",
            "is_temporary": False,
            "is_default": False,
            "requires_review": False,
            "rule": "Report the lowest controlling legal-load rating factor for AASHTO/FHWA/State/Federal/Tribal legal loads. Do not use factors based on traffic restrictions.",
        })
        return

    blr06_source = _source_value(old, MAIN_SOURCE_FIELDS.get("BLR06", []))
    rows.append({
        "table": "bridges",
        "row_index": None,
        "tag": "BLR07",
        "source_name": "NBI TXT" if blr06_source else None,
        "source_value": blr06_source,
        "note": "Transition fallback: BLR07 is using BLR06 because NBI/NBIBE does not include per-legal-load rating factors. Engineer should confirm legal loads are enveloped by the operating rating.",
        "is_temporary": False,
        "is_default": True,
        "requires_review": True,
        "rule": "If no legal-load factors were calculated, BLR06 may be reported only when all legal loads are enveloped by the operating rating per SNBI commentary.",
    })


def build_editor_metadata(snbi: Dict[str, Any], old: Dict[str, Any], elements: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    present_tags: Set[str] = set()
    for tag, value in snbi.items():
        if isinstance(value, (list, dict)):
            continue
        if value not in (None, ""):
            present_tags.add(tag)
        _add_meta(rows, "bridges", None, tag, value, old, MAIN_SOURCE_FIELDS.get(tag, []))

    # Blank bridge-level fields still get their FHWA transition note so the
    # Editor can explain why the converter left them empty.
    for tag in sorted(TOP_LEVEL_FIELDS - present_tags):
        if TRANSITION_NOTES.get(tag):
            rows.append(_missing_note_row("bridges", None, tag))

    feature_fields = NESTED_ARRAY_SPECS["Features"]["fields"] - {"Routes"}
    for idx, feature in enumerate(snbi.get("Features") or []):
        if not isinstance(feature, dict):
            continue
        _add_row_metadata(rows, "features", idx, feature, old, FEATURE_SOURCE_FIELDS, all_fields=feature_fields)
        for route_idx, route in enumerate(feature.get("Routes") or []):
            if isinstance(route, dict):
                _add_row_metadata(rows, "routes", route_idx, route, old, ROUTE_SOURCE_FIELDS)

    for table, array_name, field_map in (
        ("span_sets", "SpanSets", SPAN_SOURCE_FIELDS),
        ("substructure_sets", "SubstructureSets", SUBSTRUCTURE_SOURCE_FIELDS),
        ("inspections", "Inspections", INSPECTION_SOURCE_FIELDS),
        ("posting_statuses", "PostingStatuses", POSTING_STATUS_SOURCE_FIELDS),
        ("works", "Works", WORK_SOURCE_FIELDS),
    ):
        all_fields = NESTED_ARRAY_SPECS[array_name]["fields"]
        for idx, row in enumerate(snbi.get(array_name) or []):
            if isinstance(row, dict):
                _add_row_metadata(rows, table, idx, row, old, field_map, all_fields=all_fields)

    _add_blr07_metadata(rows, snbi, old)

    for idx, element in enumerate(elements or snbi.get("Elements") or []):
        if not isinstance(element, dict):
            continue
        for tag, value in element.items():
            if tag.startswith("BE") or tag.startswith("BCS"):
                rows.append({
                    "table": "elements",
                    "row_index": idx,
                    "tag": tag,
                    "source_name": "NBIBE XML",
                    "source_value": f"{tag}={value}",
                    "note": "Original element value from NBIBE XML.",
                    "is_temporary": _is_temp(value),
                    "is_default": False,
                })

    return rows
