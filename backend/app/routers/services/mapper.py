# backend/app/routers/services/mapper.py

from typing import Dict, Any, List, Optional
from .utils import (
    safe_str,
    safe_int,
    safe_float,
    convert_dms_to_decimal,
    meters_to_feet,
    km_to_mile,
    get_rating_factor,
    determine_primary_under
)
from .snbi_crosswalk import (
    approach_alignment_from_legacy_eval,
    bridge_railing_from_legacy,
    deck_material_from_legacy,
    deck_protective_from_legacy,
    deck_reinforcing_from_legacy,
    design_load_from_legacy,
    federal_land_from_legacy,
    functional_class_from_legacy,
    historic_from_legacy,
    inspection_type_from_legacy,
    load_rating_method_from_legacy,
    national_highway_freight_from_legacy,
    navigation_flag_from_legacy,
    navigation_protection_from_legacy,
    nhs_from_legacy,
    normalize_snbi_date,
    overtopping_from_legacy,
    owner_maintenance_from_legacy,
    posting_status_from_legacy,
    remove_blank_values,
    route_direction_from_legacy,
    route_service_from_nbi_service_level,
    route_suffix_from_legacy,
    route_type_from_nbi_prefix,
    scour_condition_from_legacy,
    scour_poa_from_legacy,
    scour_vulnerability_from_legacy,
    span_continuity_from_legacy,
    span_material_from_legacy,
    span_type_from_legacy,
    strahnet_from_legacy,
    toll_from_legacy,
    urban_code_from_legacy,
    wearing_surface_from_legacy,
)


def _normalize_date8(value: Any) -> Optional[str]:
    return normalize_snbi_date(value)


def _clean_text(value: Any) -> Optional[str]:
    text = safe_str(value).strip()
    if not text:
        return None
    return text.strip().strip("'\"").strip() or None


def map_bridge_to_snbi(old: Dict[str, Any], unders: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Map legacy NBI record to SNBI structure.
    Fully defensive against missing or invalid data.
    """
    if not old or not isinstance(old, dict):
        print("Warning: map_bridge_to_snbi received invalid 'old' (None or not dict)")
        return {}

    snbi: Dict[str, Any] = {}

    # ── Basic Location and Identification ──────────────────────────────────────
    snbi['BL01'] = safe_int(old.get('STATE_CODE_001'), 0)
    snbi['BID01'] = _clean_text(old.get('STRUCTURE_NUMBER_008'))
    snbi['BL02'] = safe_int(old.get('COUNTY_CODE_003'), 0)
    snbi['BL03'] = safe_int(old.get('PLACE_CODE_004'), 0)
    snbi['BL04'] = safe_str(old.get('HIGHWAY_DISTRICT_002')).lstrip('0')
    snbi['BL11'] = _clean_text(old.get('LOCATION_009'))
    # B.ID.02 (bridge name) has no legacy source; Item 6A names below-features.

    # B.W.01 is a direct transition: preserve a reported four-digit Item 27
    # value, but do not invent year 0 when the legacy source is blank/invalid.
    year_built_raw = _clean_text(old.get('YEAR_BUILT_027'))
    year_built = safe_int(year_built_raw, -1) if year_built_raw else -1
    snbi['BW01'] = year_built if 0 < year_built <= 9999 else None

    snbi['BCL01'] = owner_maintenance_from_legacy(old.get('OWNER_022'))
    snbi['BCL02'] = owner_maintenance_from_legacy(old.get('MAINTENANCE_021'))
    snbi['BCL03'] = federal_land_from_legacy(old.get('FEDERAL_LANDS_105'))
    snbi['BCL04'] = historic_from_legacy(old.get('HISTORY_037'))
    snbi['BCL05'] = toll_from_legacy(old.get('TOLL_020'))
    # B.CL.06 (emergency evacuation route) has no legacy source.
    snbi['BL12'] = None
    # SNBI code 0 = "no previous bridge number exists" -- true for legacy data.
    snbi['BID03'] = '0'

    snbi['BC01'] = safe_str(old.get('DECK_COND_058'))
    snbi['BC02'] = safe_str(old.get('SUPERSTRUCTURE_COND_059'))
    snbi['BC03'] = safe_str(old.get('SUBSTRUCTURE_COND_060'))
    snbi['BC04'] = safe_str(old.get('CULVERT_COND_062'))
    # B.C.05/06 transition from Items 36A/36B only when N (feature not present);
    # legacy numeric 36A/36B are safety-feature adequacy codes, not condition
    # ratings. B.C.07/08 (bearings/joints) have no legacy NBI source.
    snbi['BC05'] = 'N' if safe_str(old.get('RAILINGS_036A')).strip().upper() == 'N' else None
    snbi['BC06'] = 'N' if safe_str(old.get('TRANSITIONS_036B')).strip().upper() == 'N' else None

    snbi['BRH01'] = bridge_railing_from_legacy(old.get('RAILINGS_036A'))
    snbi['BRH02'] = bridge_railing_from_legacy(old.get('TRANSITIONS_036B'))
    snbi['BG11'] = safe_int(old.get('DEGREES_SKEW_034'), 0)
    # B.G.12 (curved bridge), B.G.13 (max bridge height) and B.G.14 (sidehill
    # bridge) have no legacy source.

    # ── Latitude / Longitude ───────────────────────────────────────────────────
    # Signs per FHWA: latitude negative only for American Samoa (state 60);
    # longitude negative for all records except the Pacific territories
    # (64 FSM, 66 Guam, 68 Marshall Is., 69 N. Mariana Is., 70 Palau).
    state_fips = safe_str(old.get('STATE_CODE_001')).strip().zfill(2)[:2]
    lat_raw = safe_str(old.get('LAT_016'))
    if lat_raw:
        lat_dec = convert_dms_to_decimal(lat_raw)
        if lat_dec is not None:
            snbi['BL05'] = -lat_dec if state_fips == '60' else lat_dec

    lon_raw = safe_str(old.get('LONG_017'))
    if lon_raw:
        lon_dec = convert_dms_to_decimal(lon_raw)
        if lon_dec is not None:
            snbi['BL06'] = lon_dec if state_fips in {'64', '66', '68', '69', '70'} else -lon_dec

    # ── Border Bridge Info ─────────────────────────────────────────────────────
    snbi['BL07'] = _clean_text(old.get('OTHR_STATE_STRUC_NO_099')) or 'N'
    # Item 98A is 2-digit state FIPS + FHWA region digit: drop the 3rd digit,
    # trim leading zeros, CAN -> CA, MEX -> MX.
    border_state = safe_str(old.get('OTHER_STATE_CODE_098A')).strip().upper()
    if border_state == 'CAN':
        border_state = 'CA'
    elif border_state == 'MEX':
        border_state = 'MX'
    elif border_state.isdigit():
        if len(border_state) >= 3:
            border_state = border_state[:2]
        border_state = border_state.lstrip('0')
    snbi['BL08'] = border_state
    # Item 98B percent responsibility -> B.L.09 code: 99 -> 0 (none),
    # 00 -> 2 (full), anything else -> 1 (shared).
    pct_raw = safe_str(old.get('OTHER_STATE_PCNT_098B')).strip()
    if pct_raw and pct_raw.isdigit():
        pct = pct_raw.zfill(2)
        snbi['BL09'] = '0' if pct == '99' else ('2' if pct == '00' else '1')
    else:
        snbi['BL09'] = None

    if border_state:
        snbi['BL10'] = snbi['BL01']   # Designated lead state = own state

    # ── Geometry & Dimensions ──────────────────────────────────────────────────
    struct_len = safe_float(old.get('STRUCTURE_LEN_MT_049'))
    max_span = safe_float(old.get('MAX_SPAN_LEN_MT_048'))
    deck_width = safe_float(old.get('DECK_WIDTH_MT_052'))
    rway_width = safe_float(old.get('ROADWAY_WIDTH_MT_051'))
    appr_width = safe_float(old.get('APPR_WIDTH_MT_032'))

    snbi['BG02'] = meters_to_feet(struct_len)
    snbi['BG03'] = meters_to_feet(max_span)

    if struct_len and struct_len > 9.1:
        snbi['BG01'] = snbi['BG02']

    main_spans = safe_int(old.get('MAIN_UNIT_SPANS_045'))
    appr_spans = safe_int(old.get('APPR_SPANS_046'))

    if main_spans == 1 and appr_spans == 0:
        snbi['BG04'] = snbi['BG03']

    # B.G.05 transitions from Item 52 only; FHWA: null when Item 52 = 0.
    snbi['BG05'] = meters_to_feet(deck_width) if deck_width and deck_width > 0 else None
    snbi['BG06'] = meters_to_feet(rway_width)
    snbi['BG07'] = meters_to_feet(old.get('LEFT_CURB_MT_050A'))
    snbi['BG08'] = meters_to_feet(old.get('RIGHT_CURB_MT_050B'))
    snbi['BG09'] = meters_to_feet(appr_width)
    snbi['BG10'] = safe_str(old.get('MEDIAN_CODE_033'))

    # ── Span Sets ──────────────────────────────────────────────────────────────
    snbi['SpanSets'] = []
    structure_type_b = safe_str(old.get('STRUCTURE_TYPE_043B'))

    deck_type = old.get('DECK_STRUCTURE_TYPE_107')
    surface_type = old.get('SURFACE_TYPE_108A')
    membrane_type = old.get('MEMBRANE_TYPE_108B')
    deck_protection = old.get('DECK_PROTECTION_108C')
    total_spans = main_spans + appr_spans

    if main_spans > 0 and structure_type_b != '00':
        main_config = 'M01' if structure_type_b != '19' else 'C01'
        main_deck_material = deck_material_from_legacy(deck_type, structure_type_b)
        # BSP03/07/08/13 (member count, protective systems) have no legacy
        # source and are left for the Editor.
        snbi['SpanSets'].append({
            'BSP01': main_config,
            'BSP02': main_spans,
            'BSP04': span_material_from_legacy(old.get('STRUCTURE_KIND_043A')),
            'BSP05': span_continuity_from_legacy(old.get('STRUCTURE_KIND_043A'), structure_type_b, total_spans),
            'BSP06': span_type_from_legacy(structure_type_b),
            'BSP09': main_deck_material,
            'BSP10': wearing_surface_from_legacy(surface_type, deck_type),
            'BSP11': deck_protective_from_legacy(membrane_type, deck_protection, deck_type),
            'BSP12': deck_reinforcing_from_legacy(deck_protection, deck_type),
        })

    if appr_spans > 0:
        appr_config = 'A01' if structure_type_b != '19' else 'C02'
        appr_deck_material = deck_material_from_legacy(deck_type, old.get('APPR_TYPE_044B'))
        snbi['SpanSets'].append({
            'BSP01': appr_config,
            'BSP02': appr_spans,
            'BSP04': span_material_from_legacy(old.get('APPR_KIND_044A')),
            'BSP05': span_continuity_from_legacy(old.get('APPR_KIND_044A'), old.get('APPR_TYPE_044B'), total_spans),
            'BSP06': span_type_from_legacy(old.get('APPR_TYPE_044B')),
            'BSP09': appr_deck_material,
            'BSP10': wearing_surface_from_legacy(surface_type, deck_type),
            'BSP11': deck_protective_from_legacy(membrane_type, deck_protection, deck_type),
            'BSP12': deck_reinforcing_from_legacy(deck_protection, deck_type),
        })

    if not snbi['SpanSets']:
        snbi['SpanSets'].append({
            'BSP01': 'M01',
            'BSP02': 1,
            'BSP04': 'X-T',
            'BSP05': '1',
            'BSP06': 'X-T',
        })

    # ── Substructure Sets ──────────────────────────────────────────────────────
    # At least one substructure set is required unless every span is pipe
    # (P01/P02). NBI has no substructure crosswalk, so a single set with
    # unknown ("X") materials is emitted for the user to refine in the Editor.
    snbi['SubstructureSets'] = [{
        'BSB01': 'P01' if main_spans > 1 else 'A01',
        'BSB02': max(1, main_spans + appr_spans + 1),
        'BSB03': 'X',
        'BSB04': 'X',
        'BSB06': 'X',
    }]

    # ── Load Rating ────────────────────────────────────────────────────────────
    # B.LR.02 (design method), B.LR.03 (rating date), B.LR.07 (legal load
    # rating factor) and B.LR.08 (permit class) have no legacy source.
    snbi['BLR01'] = design_load_from_legacy(old.get('DESIGN_LOAD_031'))
    snbi['BLR04'] = load_rating_method_from_legacy(old.get('OPR_RATING_METH_063'))
    snbi['BLR05'] = get_rating_factor(old, 'INVENTORY_RATING_066', 'INV_RATING_METH_065')
    snbi['BLR06'] = get_rating_factor(old, 'OPERATING_RATING_064', 'OPR_RATING_METH_063')

    # ── Posting & Condition Flags ──────────────────────────────────────────────
    snbi['BPS01'] = posting_status_from_legacy(old.get('OPEN_CLOSED_POSTED_041'))

    waterway_crossing = safe_str(old.get('SERVICE_UND_042B')) in {'5', '6', '7', '8'}

    def _yn(raw: Any) -> Optional[str]:
        text = safe_str(raw).upper()[:1]
        if text == 'Y':
            return 'Y'
        if text in {'N', '0'}:
            return 'N'
        return None

    # B.IR.02 (fatigue details) and B.IR.04 (complex feature) have no legacy
    # source; Items 92A/92C feed B.IR.01 and the inspection events instead.
    snbi['BIR01'] = _yn(old.get('FRACTURE_092A')) or 'N'
    snbi['BIR03'] = (_yn(old.get('UNDWATER_LOOK_SEE_092B')) or 'N') if waterway_crossing else None

    # B.C.09 is a direct transition of Item 61 (0-9 or N); B.C.10 (channel
    # protection) has no legacy source -- Item 71 feeds B.AP.02 instead.
    channel_condition = safe_str(old.get('CHANNEL_COND_061')).strip().upper()
    snbi['BC09'] = channel_condition if channel_condition in set("0123456789N") else None
    # Item 113 drives all three scour items (B.C.11/B.AP.03/B.AP.04); 113=N means
    # "not over waterway" and yields BC11=N with the appraisal items left null.
    scour_113 = safe_str(old.get('SCOUR_CRITICAL_113')).strip().upper()
    snbi['BC11'] = scour_condition_from_legacy(scour_113) or ('N' if not waterway_crossing else None)
    # BC12/BC13 and BG16 are calculated by FHWA and should not be reported.
    # B.C.14/15 (NSTM/underwater condition ratings) have no legacy source.

    # ── Approach & Waterway Eval ───────────────────────────────────────────────
    snbi['BAP01'] = approach_alignment_from_legacy_eval(old.get('APPR_ROAD_EVAL_072'))
    snbi['BAP02'] = overtopping_from_legacy(old.get('WATERWAY_EVAL_071'), old.get('FUNCTIONAL_CLASS_026'))
    snbi['BAP03'] = scour_vulnerability_from_legacy(scour_113)
    snbi['BAP04'] = scour_poa_from_legacy(scour_113)
    snbi['BAP05'] = '0'

    # ── Posting Statuses / Evaluations ───────────────────────────────────────
    # B.PS.02 (posting change date) has no legacy source; the inspection date
    # is not when the posting changed.
    snbi['PostingStatuses'] = []
    posting_status = posting_status_from_legacy(old.get('OPEN_CLOSED_POSTED_041'))
    if posting_status:
        snbi['PostingStatuses'].append({'BPS01': posting_status})

    snbi['PostingEvaluations'] = []
    posting_eval = None

    # ── Inspections ───────────────────────────────────────────────────────────
    snbi['Inspections'] = []
    inspection_date = _normalize_date8(old.get('DATE_OF_INSPECT_090'))
    if inspection_date:
        # BIE03: legacy Item 90 records when the inspection was performed, so
        # the begin date doubles as the completion date. BIE04 keeps the app's
        # documented "N = not applicable" default because the validator
        # requires a value; Item 75A (proposed work) does not feed B.IE.11.
        snbi['Inspections'].append({
            'BIE01': inspection_type_from_legacy('R'),
            'BIE02': inspection_date,
            'BIE03': inspection_date,
            'BIE04': 'N',
            'BIE05': safe_int(old.get('INSPECT_FREQ_MONTHS_091'), 0),
        })

    def _interval_from_92(raw: Any) -> Optional[int]:
        # Items 92A/B/C are "YXX": required flag + interval in months.
        text = safe_str(raw).strip().upper()
        if text.startswith('Y') and text[1:].strip().isdigit():
            return int(text[1:])
        return None

    # NSTM/underwater/special inspection events transition from Items 92/93
    # whenever a last-inspection date exists (interval only when 92x = YXX).
    for flag_field, date_field, bie01 in (
        ('FRACTURE_092A', 'FRACTURE_LAST_DATE_093A', '4'),
        ('UNDWATER_LOOK_SEE_092B', 'UNDWATER_LAST_DATE_093B', '3'),
        ('SPEC_INSPECT_092C', 'SPEC_LAST_DATE_093C', '7'),
    ):
        event_date = _normalize_date8(old.get(date_field))
        if not event_date:
            continue
        # BIE03 has no legacy source; legacy 90/93x dates record when the
        # inspection was performed, so begin date doubles as completion date.
        # BIE04 keeps the app's documented "N = not applicable" default.
        event = {'BIE01': bie01, 'BIE02': event_date, 'BIE03': event_date, 'BIE04': 'N'}
        if bie01 == '7':
            # Special inspections use no risk-based interval method.
            event['BIE07'] = 'N'
        interval = _interval_from_92(old.get(flag_field))
        if interval is not None:
            event['BIE05'] = interval
        snbi['Inspections'].append(event)

    # ── Work History ───────────────────────────────────────────────────────────
    # Per FHWA only Item 106 transitions ("RECONSTRUCTED" placeholder, users
    # refine later). Item 97 is a *proposed* improvement year, not performed
    # work. The original construction is recorded as a BR1 event at year built.
    snbi['Works'] = []
    if snbi.get('BW01'):
        snbi['Works'].append({'BW02': snbi['BW01'], 'BW03': 'BR1'})
    year_recon = safe_int(old.get('YEAR_RECONSTRUCTED_106'))
    if year_recon > 0:
        snbi['Works'].append({'BW02': year_recon, 'BW03': 'RECONSTRUCTED'})

    # ── Features ───────────────────────────────────────────────────────────────
    # Feature records derive from Items 42A/42B per the FHWA B.F.01,02,03 tab.
    # Carried: 42A 1/6/7/8 -> H01; 4 -> H01+R01; 5 -> H01+P01.
    # Below: 42B 1 -> H02; 2 -> R; 3 -> P; 4 -> H02+R; 5 -> W01; 6 -> H02+W01;
    # 7 -> R+W01; 8 -> H02+W01+R; 9 -> F01; 0 -> X01. Railroad below is R02
    # when the bridge also carries a railroad (42A=4), else R01; same for
    # trail P02/P01 when 42A=5.
    snbi['Features'] = []
    service_on = safe_str(old.get('SERVICE_ON_042A'))
    carries_railroad = service_on == '4'
    carries_trail = service_on == '5'
    below_feature_name = _clean_text(old.get('FEATURES_DESC_006A'))

    def _route_from(record: Dict[str, Any]) -> Dict[str, Any]:
        route_num = safe_str(record.get('ROUTE_NUMBER_005D')).lstrip('0') or '0'
        suffix = route_suffix_from_legacy(record.get('DIRECTION_005E'))
        if route_num != '0' and suffix:
            route_num += suffix
        return {
            'BRT01': 'R01',
            'BRT02': route_num,
            'BRT03': route_direction_from_legacy(record.get('TRAFFIC_DIRECTION_102')),
            'BRT04': route_type_from_nbi_prefix(record.get('ROUTE_PREFIX_005B')),
            'BRT05': route_service_from_nbi_service_level(record.get('SERVICE_LEVEL_005C')),
        }

    def _lrs_route_id(record: Dict[str, Any]) -> Optional[str]:
        combined = (safe_str(record.get('LRS_INV_ROUTE_013A')) +
                    safe_str(record.get('SUBROUTE_NO_013B'))).strip()
        if not combined.strip('0 '):
            return None
        # FHWA: trim leading zeros -- except New Jersey, whose HPMS Route IDs
        # keep them.
        if state_fips != '34':
            combined = combined.lstrip('0').strip()
        return combined[:12] or None

    def _detour_miles(record: Dict[str, Any]) -> Optional[int]:
        if safe_str(record.get('DETOUR_KILOS_019')) == '199':
            return 999
        detour_km = safe_float(record.get('DETOUR_KILOS_019'))
        return round(detour_km / 1.60934) if detour_km else None

    if service_on in {'1', '4', '5', '6', '7', '8'}:
        feat = {
            'BF01': 'H01',
            'BF02': 'C',
            'BF03': _clean_text(old.get('FACILITY_CARRIED_007')),
            'BH01': functional_class_from_legacy(old.get('FUNCTIONAL_CLASS_026')),
            'BH02': urban_code_from_legacy(old.get('FUNCTIONAL_CLASS_026')),
            'BH03': nhs_from_legacy(old.get('HIGHWAY_SYSTEM_104')),
            'BH04': national_highway_freight_from_legacy(old.get('NATIONAL_NETWORK_110')),
            'BH05': strahnet_from_legacy(old.get('STRAHNET_HIGHWAY_100')),
            'BH06': _lrs_route_id(old),
            'BH07': km_to_mile(old.get('KILOPOINT_011')),
            'BH08': safe_int(old.get('TRAFFIC_LANES_ON_028A')),
            'BH09': safe_int(old.get('ADT_029')),
            'BH10': safe_int(old.get('ADT_029')) * safe_int(old.get('PERCENT_ADT_TRUCK_109')) // 100,
            'BH11': safe_int(old.get('YEAR_ADT_030')),
            'BH12': meters_to_feet(old.get('MIN_VERT_CLR_010'), round_down=True, cap99=True),
            'BH13': meters_to_feet(old.get('VERT_CLR_OVER_MT_053'), round_down=True, cap99=True),
            'BH16': meters_to_feet(old.get('HORR_CLR_MT_047'), round_down=True, cap99=True),
            'BH17': _detour_miles(old),
            'Routes': [_route_from(old)],
        }
        snbi['Features'].append(feat)
        if carries_railroad:
            snbi['Features'].append({
                'BF01': 'R01', 'BF02': 'C',
                'BF03': _clean_text(old.get('FACILITY_CARRIED_007')),
            })
        if carries_trail:
            snbi['Features'].append({
                'BF01': 'P01', 'BF02': 'C',
                'BF03': _clean_text(old.get('FACILITY_CARRIED_007')),
            })

    # ── Features below the bridge (Item 42B) ──────────────────────────────────
    service_und = safe_str(old.get('SERVICE_UND_042B'))
    highway_under = service_und in {'1', '4', '6', '8'}
    railroad_under = service_und in {'2', '4', '7', '8'}
    ref_54a = safe_str(old.get('VERT_CLR_UND_REF_054A')).upper()
    ref_55a = safe_str(old.get('LAT_UND_REF_055A')).upper()

    if highway_under:
        primary_under, secondary_unders = determine_primary_under(unders) if unders else (None, [])
        under_records = [
            u for u in ([primary_under] if primary_under else []) + secondary_unders
            if isinstance(u, dict)
        ]
        if not under_records:
            # No dedicated under record: the over record still carries the
            # under-route identification items.
            under_records = [old]
        h_num = 2
        for u in under_records:
            feat = {
                'BF01': f'H{h_num:02d}',
                'BF02': 'B',
                'BF03': _clean_text(u.get('FEATURES_DESC_006A')) or below_feature_name,
                'BH01': functional_class_from_legacy(u.get('FUNCTIONAL_CLASS_026')),
                'BH02': urban_code_from_legacy(u.get('FUNCTIONAL_CLASS_026')),
                'BH03': nhs_from_legacy(u.get('HIGHWAY_SYSTEM_104')),
                'BH04': national_highway_freight_from_legacy(u.get('NATIONAL_NETWORK_110')),
                'BH05': strahnet_from_legacy(u.get('STRAHNET_HIGHWAY_100')),
                'BH06': _lrs_route_id(u),
                'BH07': km_to_mile(u.get('KILOPOINT_011')),
                'BH08': safe_int(u.get('TRAFFIC_LANES_UND_028B')),
                'BH09': safe_int(u.get('ADT_029')),
                'BH10': safe_int(u.get('ADT_029')) * safe_int(u.get('PERCENT_ADT_TRUCK_109')) // 100,
                'BH11': safe_int(u.get('YEAR_ADT_030')),
                'BH12': meters_to_feet(u.get('MIN_VERT_CLR_010'), round_down=True, cap99=True),
                'BH16': meters_to_feet(u.get('HORR_CLR_MT_047'), round_down=True, cap99=True),
                'BH17': _detour_miles(u),
            }
            if h_num == 2:
                # Underclearances measured on the over record apply to the
                # primary under highway only when the reference feature is H.
                if ref_54a == 'H':
                    feat['BH13'] = meters_to_feet(old.get('VERT_CLR_UND_054B'), round_down=True, cap99=True)
                if ref_55a == 'H':
                    left_lateral = safe_float(old.get('LEFT_LAT_UND_MT_056'))
                    if left_lateral and abs(left_lateral - 99.9) > 1e-9:
                        feat['BH14'] = meters_to_feet(left_lateral, round_down=True, cap99=True)
                    feat['BH15'] = meters_to_feet(old.get('LAT_UND_MT_055B'), round_down=True, cap99=True)
            feat['Routes'] = [_route_from(u)]
            snbi['Features'].append(feat)
            h_num += 1

    if railroad_under:
        rr_name = below_feature_name
        for u in unders or []:
            if isinstance(u, dict) and safe_str(u.get('SERVICE_UND_042B')) in {'2', '4', '7', '8'}:
                rr_name = _clean_text(u.get('FEATURES_DESC_006A')) or rr_name
                break
        rr_feat = {
            'BF01': 'R02' if carries_railroad else 'R01',
            'BF02': 'B',
            'BF03': rr_name,
        }
        # B.RR.01 (railroad service type) has no legacy source; clearances
        # transition only when the reference feature letter is R.
        if ref_54a == 'R':
            rr_feat['BRR02'] = meters_to_feet(old.get('VERT_CLR_UND_054B'), round_down=True, cap99=True)
        if ref_55a == 'R':
            rr_feat['BRR03'] = meters_to_feet(old.get('LAT_UND_MT_055B'), round_down=True, cap99=True)
        snbi['Features'].append(rr_feat)

    if service_und == '3':
        snbi['Features'].append({
            'BF01': 'P02' if carries_trail else 'P01', 'BF02': 'B',
            'BF03': below_feature_name,
        })
    if service_und == '9':
        snbi['Features'].append({'BF01': 'F01', 'BF02': 'B', 'BF03': below_feature_name})
    if service_und == '0':
        snbi['Features'].append({'BF01': 'X01', 'BF02': 'B', 'BF03': below_feature_name})

    if waterway_crossing:
        # Navigation items (38/39/40/111/116) live on the over record and
        # attach to the single W01 feature; clearances round down per FHWA.
        nav_flag = navigation_flag_from_legacy(old.get('NAVIGATION_038')) or 'N'
        water_feature = {
            'BF01': 'W01',
            'BF02': 'B',
            'BF03': below_feature_name,
            'BN01': nav_flag,
        }
        if nav_flag == 'Y':
            is_lift_bridge = structure_type_b == '15'
            water_feature['BN02'] = (
                meters_to_feet(old.get('MIN_NAV_CLR_MT_116'), round_down=True)
                if is_lift_bridge
                else meters_to_feet(old.get('NAV_VERT_CLR_MT_039'), round_down=True)
            )
            if is_lift_bridge:
                water_feature['BN03'] = meters_to_feet(old.get('NAV_VERT_CLR_MT_039'), round_down=True)
            water_feature['BN04'] = meters_to_feet(old.get('NAV_HORR_CLR_MT_040'), round_down=True)
            # B.N.05 has no legacy source.
            water_feature['BN06'] = navigation_protection_from_legacy(old.get('PIER_PROTECTION_111'))
        snbi['Features'].append(water_feature)

    return remove_blank_values(snbi)
