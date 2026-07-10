from typing import Any, Dict, List, Set, Tuple

# Scalar bridge-level SNBI keys (non-array fields)
TOP_LEVEL_FIELDS = {
    'BL01', 'BID01', 'BID02', 'BID03', 'BL02', 'BL03', 'BL04', 'BL05', 'BL06', 'BL07', 'BL08', 'BL09', 'BL10',
    'BL11', 'BL12', 'BCL01', 'BCL02', 'BCL03', 'BCL04', 'BCL05', 'BCL06', 'BRH01', 'BRH02', 'BG01', 'BG02',
    'BG03', 'BG04', 'BG05', 'BG06', 'BG07', 'BG08', 'BG09', 'BG10', 'BG11', 'BG12', 'BG13', 'BG14', 'BG15',
    'BG16', 'BLR01', 'BLR02', 'BLR03', 'BLR04', 'BLR05', 'BLR06', 'BLR07', 'BLR08', 'BIR01', 'BIR02', 'BIR03',
    'BIR04', 'BC01', 'BC02', 'BC03', 'BC04', 'BC05', 'BC06', 'BC07', 'BC08', 'BC09', 'BC10', 'BC11', 'BC12',
    'BC13', 'BC14', 'BC15', 'BAP01', 'BAP02', 'BAP03', 'BAP04', 'BAP05', 'BW01'
}

NESTED_ARRAY_SPECS: Dict[str, Dict[str, Any]] = {
    'Elements': {
        'fields': {'BE01', 'BE02', 'BE03', 'BCS01', 'BCS02', 'BCS03', 'BCS04'},
        'required': {'BE01', 'BE02'},
    },
    'Features': {
        'fields': {
            'BF01', 'BF02', 'BF03', 'BH01', 'BH02', 'BH03', 'BH04', 'BH05', 'BH06', 'BH07', 'BH08', 'BH09',
            'BH10', 'BH11', 'BH12', 'BH13', 'BH14', 'BH15', 'BH16', 'BH17', 'BH18', 'BRR01', 'BRR02', 'BRR03',
            'BN01', 'BN02', 'BN03', 'BN04', 'BN05', 'BN06', 'Routes'
        },
        'required': {'BF01'},
    },
    'Routes': {
        'fields': {'BRT01', 'BRT02', 'BRT03', 'BRT04', 'BRT05'},
        'required': {'BRT01'},
    },
    'Inspections': {
        'fields': {'BIE01', 'BIE02', 'BIE03', 'BIE04', 'BIE05', 'BIE06', 'BIE07', 'BIE08', 'BIE09', 'BIE10', 'BIE11', 'BIE12'},
        'required': {'BIE01', 'BIE02'},
    },
    'PostingEvaluations': {
        'fields': {'BEP01', 'BEP02', 'BEP03', 'BEP04'},
        'required': {'BEP01'},
    },
    'PostingStatuses': {
        'fields': {'BPS01', 'BPS02'},
        'required': {'BPS02'},
    },
    'SpanSets': {
        'fields': {'BSP01', 'BSP02', 'BSP03', 'BSP04', 'BSP05', 'BSP06', 'BSP07', 'BSP08', 'BSP09', 'BSP10', 'BSP11', 'BSP12', 'BSP13'},
        'required': {'BSP01'},
    },
    'SubstructureSets': {
        'fields': {'BSB01', 'BSB02', 'BSB03', 'BSB04', 'BSB05', 'BSB06', 'BSB07'},
        'required': {'BSB01'},
    },
    'Works': {
        'fields': {'BW02', 'BW03'},
        'required': {'BW02'},
    },
}

ALL_NESTED_ARRAY_NAMES = ['Elements', 'Features', 'Inspections', 'PostingEvaluations', 'PostingStatuses', 'SpanSets', 'SubstructureSets', 'Works']

SCHEMA_CODES: Set[str] = set(TOP_LEVEL_FIELDS)
for spec_name, spec in NESTED_ARRAY_SPECS.items():
    if spec_name == 'Routes':
        SCHEMA_CODES.update(spec['fields'])
    else:
        SCHEMA_CODES.update({k for k in spec['fields'] if k != 'Routes'})

SECTION_CODES: Dict[str, Set[str]] = {
    'Bridge': set(TOP_LEVEL_FIELDS),
    'Elements': set(NESTED_ARRAY_SPECS['Elements']['fields']),
    'Features': {k for k in NESTED_ARRAY_SPECS['Features']['fields'] if k != 'Routes'},
    'Routes': set(NESTED_ARRAY_SPECS['Routes']['fields']),
    'Inspections': set(NESTED_ARRAY_SPECS['Inspections']['fields']),
    'PostingEvaluations': set(NESTED_ARRAY_SPECS['PostingEvaluations']['fields']),
    'PostingStatuses': set(NESTED_ARRAY_SPECS['PostingStatuses']['fields']),
    'SpanSets': set(NESTED_ARRAY_SPECS['SpanSets']['fields']),
    'SubstructureSets': set(NESTED_ARRAY_SPECS['SubstructureSets']['fields']),
    'Works': set(NESTED_ARRAY_SPECS['Works']['fields']),
}


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == '')


def _ensure_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def _normalize_item(item: Dict[str, Any], fields: set, required: set, context: str) -> Tuple[Dict[str, Any], List[str]]:
    out: Dict[str, Any] = {}
    errors: List[str] = []

    for key, value in item.items():
        if key in fields and not _is_blank(value):
            out[key] = value

    missing = [k for k in required if _is_blank(out.get(k))]
    if missing:
        errors.append(f"{context}: missing required fields {missing}")

    return out, errors


def normalize_snbi_bridge(raw_bridge: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    errors: List[str] = []
    bridge: Dict[str, Any] = {}

    # Keep only known top-level scalar keys.
    for key in TOP_LEVEL_FIELDS:
        value = raw_bridge.get(key)
        if not _is_blank(value):
            bridge[key] = value

    if _is_blank(bridge.get('BL01')):
        errors.append('Bridge is missing required BL01')
    if _is_blank(bridge.get('BID01')):
        errors.append('Bridge is missing required BID01')

    # Backward compatibility: if old payload has BPS fields at top-level, move to PostingStatuses.
    top_level_ps: Dict[str, Any] = {}
    for key in ('BPS01', 'BPS02'):
        if not _is_blank(raw_bridge.get(key)):
            top_level_ps[key] = raw_bridge[key]

    for arr_name in ALL_NESTED_ARRAY_NAMES:
        spec = NESTED_ARRAY_SPECS[arr_name]
        rows = _ensure_list(raw_bridge.get(arr_name))

        if arr_name == 'PostingStatuses' and top_level_ps and not rows and not _is_blank(top_level_ps.get('BPS02')):
            rows = [top_level_ps]

        normalized_rows: List[Dict[str, Any]] = []

        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                errors.append(f"{arr_name}[{idx}] is not an object")
                continue

            normalized_row, row_errors = _normalize_item(row, spec['fields'], spec['required'], f"{arr_name}[{idx}]")

            if arr_name == 'Features':
                routes = _ensure_list(row.get('Routes'))
                norm_routes: List[Dict[str, Any]] = []
                route_spec = NESTED_ARRAY_SPECS['Routes']
                for route_idx, route in enumerate(routes):
                    if not isinstance(route, dict):
                        errors.append(f"Features[{idx}].Routes[{route_idx}] is not an object")
                        continue
                    norm_route, route_errors = _normalize_item(
                        route,
                        route_spec['fields'],
                        route_spec['required'],
                        f"Features[{idx}].Routes[{route_idx}]"
                    )
                    errors.extend(route_errors)
                    if norm_route:
                        norm_routes.append(norm_route)
                if norm_routes:
                    normalized_row['Routes'] = norm_routes

            errors.extend(row_errors)
            if normalized_row:
                normalized_rows.append(normalized_row)

        bridge[arr_name] = normalized_rows

    return bridge, errors


def normalize_snbi_payload(payload: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    normalized: List[Dict[str, Any]] = []
    all_errors: List[str] = []

    for idx, bridge in enumerate(payload):
        if not isinstance(bridge, dict):
            all_errors.append(f"Bridge[{idx}] is not an object")
            continue
        norm_bridge, errors = normalize_snbi_bridge(bridge)
        for err in errors:
            all_errors.append(f"Bridge[{idx}]: {err}")
        normalized.append(norm_bridge)

    return normalized, all_errors


def collect_payload_codes(payload: List[Dict[str, Any]]) -> Set[str]:
    seen: Set[str] = set()
    for bridge in payload:
        if not isinstance(bridge, dict):
            continue
        for key, value in bridge.items():
            if key in TOP_LEVEL_FIELDS and not _is_blank(value):
                seen.add(key)
            if key in ALL_NESTED_ARRAY_NAMES:
                rows = value if isinstance(value, list) else []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    for row_key, row_value in row.items():
                        if row_key == 'Routes' and isinstance(row_value, list):
                            for route in row_value:
                                if not isinstance(route, dict):
                                    continue
                                for route_key, route_value in route.items():
                                    if not _is_blank(route_value):
                                        seen.add(route_key)
                        elif not _is_blank(row_value):
                            seen.add(row_key)
    return seen


def summarize_payload_coverage(payload: List[Dict[str, Any]]) -> Dict[str, Any]:
    seen = collect_payload_codes(payload)
    missing = sorted(list(SCHEMA_CODES - seen))
    present = sorted(list(seen))
    return {
        'total_schema_codes': len(SCHEMA_CODES),
        'present_code_count': len(seen),
        'missing_code_count': len(missing),
        'present_codes': present,
        'missing_codes': missing,
    }


def summarize_payload_coverage_by_section(payload: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    seen = collect_payload_codes(payload)
    report: Dict[str, Dict[str, Any]] = {}
    for section, codes in SECTION_CODES.items():
        present = sorted([code for code in codes if code in seen])
        missing = sorted([code for code in codes if code not in seen])
        report[section] = {
            'total_codes': len(codes),
            'present_code_count': len(present),
            'missing_code_count': len(missing),
            'present_codes': present,
            'missing_codes': missing,
        }
    return report
