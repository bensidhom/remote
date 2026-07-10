"""Deterministic SNBI validation checks used by the app.

This is not a full FHWA validation engine. It implements the high-confidence
checks we can evaluate locally from SNBI JSON: critical dataset presence,
required identifiers, selected code-list checks, and the major posting safety
rules from FHWA SNBI Data Validation Logic v1.1.
"""

from collections import Counter
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional
import re

from .snbi_rules import CODE_LISTS, FIELD_GUIDANCE, TEMPORARY_CODES


Issue = Dict[str, Any]


NUMERIC_FIELDS = {
    "BL01", "BL02", "BL03", "BL05", "BL06", "BG01", "BG02", "BG03", "BG04", "BG05", "BG06", "BG07",
    "BG08", "BG09", "BG11", "BG13", "BG15", "BG16", "BLR05", "BLR06", "BLR07",
    "BN02", "BN03", "BN04", "BN05", "BSP02", "BSP03", "BSB02", "BE03", "BCS01",
    "BCS02", "BCS03", "BCS04", "BW02",
}
INTEGER_FIELDS = {"BL01", "BL02", "BL03", "BG11", "BG13", "BH08", "BH09", "BH10", "BH11", "BH17", "BSP02", "BSP03", "BSB02", "BIE05", "BW01", "BW02"}
DATE_FIELDS = {"BLR03", "BIE02", "BIE03", "BIE06", "BIE08", "BIE09", "BIE10", "BPS02"}
DO_NOT_REPORT_FIELDS = {"BG16", "BC12", "BC13", "BIE06"}

TABLE_BY_ARRAY = {
    "Features": "features",
    "Routes": "routes",
    "SpanSets": "span_sets",
    "SubstructureSets": "substructure_sets",
    "Elements": "elements",
    "Inspections": "inspections",
    "PostingEvaluations": "posting_evaluations",
    "PostingStatuses": "posting_statuses",
    "Works": "works",
}

REQUIRED_MAIN = {
    "BID01": ("critical", "Bridge Number is required", "Text up to 15 characters"),
    "BL01": ("critical", "State Code is required", "Valid numeric state FIPS code"),
    "BL05": ("critical", "Latitude is required", "Numeric decimal degrees"),
    "BL06": ("critical", "Longitude is required", "Numeric decimal degrees"),
    "BCL01": ("critical", "Owner code is null or not valid", "SNBI owner code such as S01, L01, FL04"),
    "BCL02": ("critical", "Maintenance responsibility code is null or not valid", "Same code domain as BCL01"),
    "BL07": ("error", 'BL07 Border Bridge Number is null. Report "N" if no border crossing exists or "0" if the bordering country does not have a bridge number', 'Bridge number, "N", or "0"'),
    "BCL06": ("error", "Emergency evacuation designation code is null or not valid", "Y or N"),
    "BG02": ("critical", "Total bridge length is required", "Numeric feet with one decimal"),
    "BG05": ("critical", "Bridge width out-to-out is required", "Numeric feet with one decimal"),
    "BG12": ("error", "Curved bridge code is null or not valid", "Valid SNBI curved bridge code"),
    "BG13": ("error", "Maximum bridge height is null or not numeric", "Whole feet"),
    "BG14": ("error", "Sidehill bridge code is null or not valid. Must be Y or N", "Y or N"),
    "BAP05": ("error", "Seismic vulnerability code is null or not valid", "Valid SNBI seismic vulnerability code"),
    "BLR02": ("error", "Design method is null or not valid", "ASD, LFD, LRFD, or N"),
    "BLR08": ("error", "Routine Permit Loads is null", "A, B, C, or N"),
}

# SNBI-only main fields that legacy NBI-sourced data cannot populate. They are
# skipped for bridges flagged as legacy (older NBI-only years) so converting an
# old NBI file does not generate a wall of critical/error issues for fields that
# do not exist in the NBI specification. FHWA phases SNBI requirements in over
# 2026-2028; legacy NBI rows are validated only on the universally meaningful
# identifiers/geometry plus the numeric/date/code and posting-safety checks.
LEGACY_SKIP_MAIN = {
    "BCL01", "BCL02", "BL07", "BCL06", "BG12", "BG13", "BG14", "BAP05", "BLR02", "BLR08",
}

# SNBI structural datasets. A bridge with none of these populated is treated as
# legacy/NBI when the caller does not pass an explicit legacy flag.
SNBI_DATASET_KEYS = (
    "Features", "SpanSets", "SubstructureSets", "PostingStatuses",
    "PostingEvaluations", "Works", "Inspections", "Elements",
)


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _bridge_is_legacy(bridge: Dict[str, Any]) -> bool:
    """Fallback legacy detection when the caller does not pass legacy_flags.

    A bridge converted from NBI carries ``_SourceData.nbi`` (the preferred
    signal, evaluated by the caller). As a fallback we treat a bridge with no
    populated SNBI structural dataset as legacy.
    """
    source = bridge.get("_SourceData")
    if isinstance(source, dict) and isinstance(source.get("nbi"), dict):
        return True
    for key in SNBI_DATASET_KEYS:
        value = bridge.get(key)
        if isinstance(value, list) and any(isinstance(item, dict) and item for item in value):
            return False
    return True


def _num(value: Any) -> Optional[float]:
    try:
        if _blank(value):
            return None
        return float(value)
    except Exception:
        return None


def _pattern_match(value: Any, pattern: str) -> bool:
    text = str(value)
    if "#" in pattern:
        regex = "^" + re.escape(pattern).replace(r"\#", r"\d") + "$"
        return re.match(regex, text) is not None
    return text == pattern


def _valid_code(tag: str, value: Any) -> bool:
    if tag not in CODE_LISTS or _blank(value):
        return True
    text = str(value).strip()
    if "|" in text:
        return all(
            any(_pattern_match(part, pattern) for pattern in CODE_LISTS[tag])
            for part in text.split("|")
            if part
        )
    return any(_pattern_match(text, pattern) for pattern in CODE_LISTS[tag])


def _iter_tag_values(obj: Any, table: Optional[str] = None, row_index: Optional[int] = None) -> Iterable[tuple[str, Any, Optional[str], Optional[int]]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            child_table = TABLE_BY_ARRAY.get(key, table)
            if isinstance(value, list):
                for idx, item in enumerate(value):
                    yield from _iter_tag_values(item, child_table, idx)
            elif isinstance(value, dict):
                yield from _iter_tag_values(value, child_table, row_index)
            else:
                yield key, value, table, row_index
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            yield from _iter_tag_values(item, table, idx)


def _expected_for_tag(tag: str) -> str:
    if tag in FIELD_GUIDANCE:
        return FIELD_GUIDANCE[tag].get("expected", "")
    if tag in CODE_LISTS:
        values = sorted(CODE_LISTS[tag])
        return ", ".join(values[:20]) + (" ..." if len(values) > 20 else "")
    if tag in DATE_FIELDS:
        return "YYYYMMDD"
    if tag in NUMERIC_FIELDS:
        return "Numeric"
    return ""


def _hint_for_tag(tag: str) -> str:
    if tag in FIELD_GUIDANCE:
        return FIELD_GUIDANCE[tag].get("hint", "")
    if tag in TEMPORARY_CODES:
        return "Temporary codes are accepted for 2026 and 2027 and must be replaced by 2028."
    if tag in CODE_LISTS:
        return "Use one of the valid SNBI code-list values."
    if tag in DATE_FIELDS:
        return "Use an eight digit SNBI date in YYYYMMDD format."
    return ""


def _add(
    issues: List[Issue],
    issue_type: str,
    bridge_id: str,
    rule: str,
    tag: str,
    message: str,
    *,
    table: Optional[str] = None,
    row_index: Optional[int] = None,
    value: Any = None,
    expected: Optional[str] = None,
    hint: Optional[str] = None,
) -> None:
    issues.append({
        "type": issue_type,
        "bridge": bridge_id,
        "rule": rule,
        "tag": tag,
        "message": message,
        "table": table,
        "row_index": row_index,
        "value": value,
        "expected": expected if expected is not None else _expected_for_tag(tag),
        "editable_hint": hint if hint is not None else _hint_for_tag(tag),
        "source": "FHWA SNBI Data Validation Logic v1.1 / Data Crosswalk",
    })


def _has_value(row: Dict[str, Any], key: str) -> bool:
    return not _blank(row.get(key))


def _valid_date(value: Any) -> bool:
    if _blank(value):
        return True
    text = str(value).strip()
    if not re.match(r"^\d{8}$", text):
        return False
    try:
        datetime.strptime(text, "%Y%m%d")
        return True
    except ValueError:
        return False


def _is_integer(value: Any) -> bool:
    if _blank(value):
        return True
    try:
        return float(value).is_integer()
    except Exception:
        return False


def _init_bridge_index(result: Dict[str, Any], bridge_id: str) -> Dict[str, Any]:
    return result.setdefault("by_bridge", {}).setdefault(bridge_id, {
        "summary": {"critical": 0, "error": 0, "flag": 0, "safety": 0, "total": 0},
        "by_tag": {},
        "by_table": {},
    })


def _index_issues(issues: List[Issue]) -> Dict[str, Any]:
    result: Dict[str, Any] = {"by_bridge": {}}
    for issue in issues:
        bridge_bucket = _init_bridge_index(result, str(issue.get("bridge") or ""))
        issue_type = issue.get("type", "error")
        bridge_bucket["summary"][issue_type] = bridge_bucket["summary"].get(issue_type, 0) + 1
        bridge_bucket["summary"]["total"] += 1
        tag = issue.get("tag") or ""
        table = issue.get("table")
        row_index = issue.get("row_index")
        if table:
            table_bucket = bridge_bucket["by_table"].setdefault(table, {})
            row_bucket = table_bucket.setdefault(str(row_index if row_index is not None else 0), {})
            row_bucket.setdefault(tag, []).append(issue)
        else:
            bridge_bucket["by_tag"].setdefault(tag, []).append(issue)
    return result


def validate_snbi_payload(
    payload: List[Dict[str, Any]],
    issue_limit: int = 250,
    submission_year: Optional[int] = None,
    legacy_flags: Optional[List[bool]] = None,
) -> Dict[str, Any]:
    issues: List[Issue] = []
    if not isinstance(payload, list):
        return {
            "summary": {"critical": 1, "error": 0, "flag": 0, "safety": 0, "total": 1},
            "issues": [{"type": "critical", "bridge": "", "rule": "PAYLOAD", "tag": "", "message": "SNBI payload must be a list"}],
        }

    legacy_count = 0
    for idx, bridge in enumerate(payload):
        if not isinstance(bridge, dict):
            _add(issues, "critical", f"Bridge[{idx}]", "BRIDGE_OBJECT", "", "Bridge row is not an object")
            continue

        # Legacy (older NBI-only) bridges are validated leniently: the SNBI
        # structural datasets and SNBI-only main fields do not exist in NBI, so
        # enforcing them would flood old years with false criticals.
        if legacy_flags is not None and idx < len(legacy_flags):
            is_legacy = bool(legacy_flags[idx])
        else:
            is_legacy = _bridge_is_legacy(bridge)
        if is_legacy:
            legacy_count += 1

        bridge_id = str(bridge.get("BID01") or f"Bridge[{idx}]")
        for tag, (level, message, expected) in REQUIRED_MAIN.items():
            if is_legacy and tag in LEGACY_SKIP_MAIN:
                continue
            if _blank(bridge.get(tag)):
                _add(issues, level, bridge_id, f"{tag}-required", tag, message, value=bridge.get(tag), expected=expected)

        # Legacy NBI-only rows are validated only on the universal identifiers and
        # geometry above. The SNBI-specific structural datasets, conditional field
        # rules, code lists, date formats and posting-safety checks below assume
        # the SNBI specification and do not apply to pre-SNBI NBI data, so they are
        # skipped here to avoid flooding older years with inapplicable errors.
        if is_legacy:
            continue

        features = bridge.get("Features") if isinstance(bridge.get("Features"), list) else []
        spans = bridge.get("SpanSets") if isinstance(bridge.get("SpanSets"), list) else []
        substructures = bridge.get("SubstructureSets") if isinstance(bridge.get("SubstructureSets"), list) else []
        posting_statuses = bridge.get("PostingStatuses") if isinstance(bridge.get("PostingStatuses"), list) else []
        posting_evals = bridge.get("PostingEvaluations") if isinstance(bridge.get("PostingEvaluations"), list) else []
        works = bridge.get("Works") if isinstance(bridge.get("Works"), list) else []
        elements = bridge.get("Elements") if isinstance(bridge.get("Elements"), list) else []

        if not features or not any(_has_value(f, "BF01") for f in features if isinstance(f, dict)):
            _add(issues, "critical", bridge_id, "BF", "BF01", "At least one Features dataset with BF01 is required", table="features", row_index=0)
        if not spans or not any(_has_value(s, "BSP01") for s in spans if isinstance(s, dict)):
            _add(issues, "critical", bridge_id, "BSP", "BSP01", "At least one SpanSets dataset with BSP01 is required", table="span_sets", row_index=0)
        if not posting_statuses or not any(_has_value(p, "BPS01") for p in posting_statuses if isinstance(p, dict)):
            _add(issues, "critical", bridge_id, "BPS", "BPS01", "At least one PostingStatuses dataset with BPS01 is required", table="posting_statuses", row_index=0)

        all_pipe_spans = bool(spans) and all(str(s.get("BSP06") or "") in {"P01", "P02"} for s in spans if isinstance(s, dict))
        if not all_pipe_spans and (not substructures or not any(_has_value(s, "BSB01") for s in substructures if isinstance(s, dict))):
            _add(issues, "critical", bridge_id, "BSB", "BSB01", "SubstructureSets are required unless all spans are pipe P01/P02", table="substructure_sets", row_index=0)
        if not works or not any(_has_value(w, "BW02") for w in works if isinstance(w, dict)):
            _add(issues, "critical", bridge_id, "BW", "BW02", "A Work Events dataset is required for the data year", table="works", row_index=0)

        has_waterway = any(isinstance(f, dict) and str(f.get("BF01") or "").startswith("W") for f in features)
        has_waterway_or_relief = any(isinstance(f, dict) and str(f.get("BF01") or "").startswith(("W", "F")) for f in features)
        if has_waterway:
            for tag in ("BC09", "BC10", "BC11"):
                value = bridge.get(tag)
                if _blank(value):
                    _add(issues, "error", bridge_id, f"{tag}-waterway-required", tag, f"{tag} is required for bridges with a W## waterway feature", value=value)
                elif tag == "BC09" and str(value).upper() == "N":
                    _add(issues, "error", bridge_id, f"{tag}-waterway-range", tag, f"{tag} must be 0 through 9 when a W## waterway feature exists", value=value, expected="0 through 9")
        else:
            for tag in ("BC09", "BC10", "BC11"):
                if not _blank(bridge.get(tag)) and str(bridge.get(tag)).upper() != "N":
                    _add(
                        issues,
                        "error",
                        bridge_id,
                        f"{tag}-no-waterway",
                        tag,
                        f'{tag} should be "N" when the bridge has no waterway or relief waterway feature',
                        value=bridge.get(tag),
                        expected='N when no W## waterway feature exists',
                    )
        if has_waterway_or_relief:
            for tag in ("BAP02", "BAP03", "BAP04"):
                if _blank(bridge.get(tag)):
                    _add(
                        issues,
                        "error",
                        bridge_id,
                        f"{tag}-waterway-required",
                        tag,
                        f"{tag} is required when the bridge crosses a waterway or relief waterway feature",
                        value=bridge.get(tag),
                    )
        else:
            for tag in ("BAP02", "BAP03", "BAP04"):
                if not _blank(bridge.get(tag)):
                    _add(
                        issues,
                        "flag",
                        bridge_id,
                        f"{tag}-no-waterway",
                        tag,
                        f"Do not report {tag} when the bridge does not cross over a waterway",
                        value=bridge.get(tag),
                        expected="Blank unless the bridge has a waterway feature",
                    )
        if str(bridge.get("BIR01") or "").upper() == "Y" and _blank(bridge.get("BC14")):
            _add(
                issues,
                "error",
                bridge_id,
                "BC14-required",
                "BC14",
                "NSTM inspection condition code is not valid",
                value=bridge.get("BC14"),
                expected="Valid NSTM inspection condition when NSTM inspection is required",
            )
        if str(bridge.get("BIR01") or "").upper() != "Y" and not _blank(bridge.get("BC14")):
            _add(
                issues,
                "flag",
                bridge_id,
                "BC14-not-required",
                "BC14",
                "Do not report BC14 NSTM Inspection Condition for bridges that do not require NSTM inspection",
                value=bridge.get("BC14"),
                expected="Blank when BIR01 is not Y",
            )
        if str(bridge.get("BIR03") or "").upper() == "Y" and _blank(bridge.get("BC15")):
            _add(
                issues,
                "error",
                bridge_id,
                "BC15-required",
                "BC15",
                "Underwater Inspection Condition code is not valid",
                value=bridge.get("BC15"),
                expected="Valid underwater inspection condition when BIR03 is Y",
            )
        if str(bridge.get("BIR03") or "").upper() != "Y" and not _blank(bridge.get("BC15")):
            _add(
                issues,
                "flag",
                bridge_id,
                "BC15-not-required",
                "BC15",
                "Do not report BC15 Underwater Inspection Condition when underwater inspection is not required",
                value=bridge.get("BC15"),
                expected="Blank when BIR03 is not Y",
            )
        if _blank(bridge.get("BID03")):
            _add(
                issues,
                "flag",
                bridge_id,
                "BID03-default",
                "BID03",
                "Previous bridge number is null. Report 0 if no previous bridge number",
                value=bridge.get("BID03"),
                expected='Bridge number or "0"',
            )
        if has_waterway_or_relief and str(bridge.get("BIR03") or "").upper() not in {"N", "Y"}:
            _add(
                issues,
                "error",
                bridge_id,
                "BIR03-required",
                "BIR03",
                "Underwater Inspection Required must be Y or N for waterway bridges",
                value=bridge.get("BIR03"),
                expected="Y or N",
            )
        if not has_waterway_or_relief and not _blank(bridge.get("BIR03")):
            _add(
                issues,
                "flag",
                bridge_id,
                "BIR03-no-waterway",
                "BIR03",
                "Do not report BIR03 Underwater Inspection Required for bridges without a waterway feature",
                value=bridge.get("BIR03"),
                expected="Blank when no waterway feature exists",
            )
        bg13 = _num(bridge.get("BG13"))
        if bg13 is not None and bg13 <= 0:
            _add(
                issues,
                "error",
                bridge_id,
                "BG13-positive",
                "BG13",
                "Maximum bridge height must be a positive whole-foot value",
                value=bridge.get("BG13"),
                expected="Positive whole feet",
            )
        has_steel_members = any(isinstance(span, dict) and str(span.get("BSP04") or "").startswith("S") for span in spans)
        has_steel_members = has_steel_members or any(isinstance(sub, dict) and str(sub.get("BSB03") or "").startswith("S") for sub in substructures)
        if has_steel_members and _blank(bridge.get("BIR02")):
            _add(
                issues,
                "error",
                bridge_id,
                "BIR02-required",
                "BIR02",
                "Fatigue Details code is null or not valid for bridges with steel members",
                value=bridge.get("BIR02"),
                expected="Y or N",
            )
        if not has_steel_members and not _blank(bridge.get("BIR02")):
            _add(
                issues,
                "flag",
                bridge_id,
                "BIR02-not-required",
                "BIR02",
                "Do not report BIR02 Fatigue Details for bridges that do not have steel superstructure and/or steel substructure",
                value=bridge.get("BIR02"),
                expected="Blank when no steel members exist",
            )

        for s_idx, span in enumerate(spans):
            if not isinstance(span, dict):
                continue
            span_messages = {
                "BSP01": "Span configuration designation is required for each span set",
                "BSP02": "Number of spans is required for each span set",
                "BSP03": "Number of beam lines is required for each span set",
                "BSP04": "Span material is required for each span set",
                "BSP05": "Span continuity is required for each span set",
                "BSP06": "Span type is required for each span set",
                "BSP07": "Span protective system is required for each span set; report 0 if none",
                "BSP09": "Deck material and type is required for each span set",
            }
            for tag in ("BSP01", "BSP02", "BSP03", "BSP04", "BSP05", "BSP06", "BSP07", "BSP09"):
                if _blank(span.get(tag)):
                    level = "critical" if tag == "BSP01" else "error"
                    _add(issues, level, bridge_id, f"{tag}-required", tag, span_messages[tag], table="span_sets", row_index=s_idx, value=span.get(tag))
            if span.get("BSP09") == "0":
                for tag in ("BSP08", "BSP10", "BSP11", "BSP13"):
                    if not _blank(span.get(tag)):
                        _add(issues, "error", bridge_id, f"{tag}-not-allowed", tag, f"Do not report {tag} when BSP09 is 0", table="span_sets", row_index=s_idx, value=span.get(tag))
            else:
                for tag in ("BSP08", "BSP13"):
                    if _blank(span.get(tag)):
                        _add(issues, "error", bridge_id, f"{tag}-required", tag, f"{tag} is required for span sets when deck material/type is reported", table="span_sets", row_index=s_idx, value=span.get(tag))

        for sub_idx, sub in enumerate(substructures):
            if not isinstance(sub, dict):
                continue
            bsb_messages = {
                "BSB01": "Substructure configuration designation is required for each substructure set",
                "BSB02": "Number of substructure units is required for each substructure set",
                "BSB03": "Substructure material is required for each substructure set",
                "BSB04": "Substructure type is required for each substructure set",
                "BSB05": "Substructure protective system is required for each substructure set; report 0 if none",
                "BSB06": "Foundation type is required for each substructure set",
                "BSB07": "Foundation protective system is required for each substructure set; report 0 if none",
            }
            for tag in ("BSB01", "BSB02", "BSB03", "BSB04", "BSB05", "BSB06", "BSB07"):
                if _blank(sub.get(tag)):
                    _add(
                        issues,
                        "error",
                        bridge_id,
                        f"{tag}-required",
                        tag,
                        bsb_messages[tag],
                        table="substructure_sets",
                        row_index=sub_idx,
                        value=sub.get(tag),
                    )

        for ps_idx, status in enumerate(posting_statuses):
            if not isinstance(status, dict):
                continue
            if _blank(status.get("BPS01")):
                _add(issues, "critical", bridge_id, "BPS01-required", "BPS01", "Load Posting Status is required", table="posting_statuses", row_index=ps_idx, value=status.get("BPS01"))
            if not _blank(status.get("BPS02")) and not _valid_date(status.get("BPS02")):
                _add(issues, "error", bridge_id, "BPS02-date", "BPS02", "Posting status change date must be YYYYMMDD", table="posting_statuses", row_index=ps_idx, value=status.get("BPS02"))

        for insp_idx, inspection in enumerate(bridge.get("Inspections", []) if isinstance(bridge.get("Inspections"), list) else []):
            if not isinstance(inspection, dict):
                continue
            for tag in ("BIE01", "BIE02", "BIE03", "BIE05"):
                if _blank(inspection.get(tag)):
                    level = "critical" if tag in {"BIE01", "BIE02", "BIE03"} else "error"
                    _add(issues, level, bridge_id, f"{tag}-required", tag, f"{tag} is required for inspection events", table="inspections", row_index=insp_idx, value=inspection.get(tag))
            if str(inspection.get("BIE01") or "") not in {"7"} and _blank(inspection.get("BIE04")):
                _add(
                    issues,
                    "error",
                    bridge_id,
                    "BIE04-required",
                    "BIE04",
                    "Nationally certified bridge inspector is null when a team leader is required",
                    table="inspections",
                    row_index=insp_idx,
                    value=inspection.get("BIE04"),
                    expected="Certified inspector/team leader identifier when required",
                )
            if str(inspection.get("BIE01") or "") in {"1", "5", "7", "8", "9"} and _blank(inspection.get("BIE07")):
                _add(
                    issues,
                    "flag",
                    bridge_id,
                    "BIE07-default",
                    "BIE07",
                    "BIE07 should be N for initial, damage, in-depth, special, service, or scour monitoring inspection types",
                    table="inspections",
                    row_index=insp_idx,
                    value=inspection.get("BIE07"),
                    expected="N",
                )

        for work_idx, work in enumerate(works):
            if not isinstance(work, dict):
                continue
            for tag in ("BW02", "BW03"):
                if _blank(work.get(tag)):
                    level = "critical" if tag == "BW02" else "error"
                    _add(issues, level, bridge_id, f"{tag}-required", tag, f"{tag} is required for work events", table="works", row_index=work_idx, value=work.get(tag))

        for f_idx, feature in enumerate(features):
            if not isinstance(feature, dict):
                continue
            ft = str(feature.get("BF01") or "")
            if ft.startswith("H") and not any(_has_value(r, "BRT01") for r in feature.get("Routes", []) if isinstance(r, dict)):
                _add(issues, "critical", bridge_id, "BRT", "BRT01", f"Highway feature {ft or f_idx} requires at least one route", table="features", row_index=f_idx)
            if ft.startswith("W") and _blank(feature.get("BN01")):
                _add(issues, "critical", bridge_id, "BN", "BN01", f"Waterway feature {ft or f_idx} requires BN01", table="features", row_index=f_idx)
            if ft.startswith("W"):
                bn01 = str(feature.get("BN01") or "").upper()
                if bn01 == "Y":
                    for tag in ("BN02", "BN04", "BN05", "BN06"):
                        if _blank(feature.get(tag)):
                            _add(
                                issues,
                                "error",
                                bridge_id,
                                f"{tag}-navigable-required",
                                tag,
                                f"{tag} is required when BN01 is Y for waterway feature {ft or f_idx}",
                                table="features",
                                row_index=f_idx,
                                value=feature.get(tag),
                            )
                    if any(isinstance(span, dict) and str(span.get("BSP06") or "").startswith("M") for span in spans) and _blank(feature.get("BN03")):
                        _add(
                            issues,
                            "error",
                            bridge_id,
                            "BN03-movable-required",
                            "BN03",
                            f"BN03 is required when BN01 is Y and a span type begins with M for waterway feature {ft or f_idx}",
                            table="features",
                            row_index=f_idx,
                            value=feature.get("BN03"),
                        )
                elif bn01 in {"N", "U"}:
                    for tag in ("BN02", "BN03", "BN04", "BN05", "BN06"):
                        if not _blank(feature.get(tag)):
                            _add(
                                issues,
                                "flag",
                                bridge_id,
                                f"{tag}-not-navigable",
                                tag,
                                f"Do not report {tag} when BN01 is {bn01}",
                                table="features",
                                row_index=f_idx,
                                value=feature.get(tag),
                                expected="Only BN01 is reported when the waterway is not navigable or navigability is unknown",
                            )
                for tag, maximum in (("BN02", 999.9), ("BN03", 999.9), ("BN04", 9999.9), ("BN05", 9999.9)):
                    numeric = _num(feature.get(tag))
                    if numeric is not None and numeric > maximum:
                        _add(
                            issues,
                            "critical",
                            bridge_id,
                            f"{tag}-max",
                            tag,
                            f"{tag} exceeds {maximum}",
                            table="features",
                            row_index=f_idx,
                            value=feature.get(tag),
                            expected=f"Numeric value not greater than {maximum}",
                        )
            elif any(not _blank(feature.get(tag)) for tag in ("BN01", "BN02", "BN03", "BN04", "BN05", "BN06")):
                _add(
                    issues,
                    "flag",
                    bridge_id,
                    "BN-non-waterway",
                    "BN01",
                    f"Do not report navigation fields on non-waterway feature {ft or f_idx}",
                    table="features",
                    row_index=f_idx,
                    expected="Navigation fields only belong on W## features",
                )
            if ft.startswith("H") and feature.get("BH03") == "Y" and not elements:
                _add(issues, "critical", bridge_id, "BE", "BE01", "NHS highway bridge requires element data", table="elements", row_index=0)
            if ft.startswith("H"):
                for tag in ("BH01", "BH02", "BH03", "BH04", "BH05", "BH06", "BH08", "BH09", "BH11", "BH12", "BH16", "BH17"):
                    if _blank(feature.get(tag)):
                        level = "critical" if tag in {"BH01", "BH03", "BH08"} and feature.get("BF02") == "C" else "error"
                        _add(issues, level, bridge_id, f"{tag}-required", tag, f"{tag} is required for highway features", table="features", row_index=f_idx, value=feature.get(tag))
                for r_idx, route in enumerate(feature.get("Routes", []) if isinstance(feature.get("Routes"), list) else []):
                    if isinstance(route, dict):
                        for tag in ("BRT01", "BRT02", "BRT03", "BRT04", "BRT05"):
                            if _blank(route.get(tag)):
                                _add(issues, "error", bridge_id, f"{tag}-required", tag, f"{tag} is required for highway routes", table="routes", row_index=r_idx, value=route.get(tag))

        bps01 = None
        if posting_statuses and isinstance(posting_statuses[0], dict):
            bps01 = posting_statuses[0].get("BPS01")
        blr06 = _num(bridge.get("BLR06"))
        if blr06 is not None and blr06 < 0.1 and bps01 != "C":
            _add(issues, "safety", bridge_id, "BLR06-PS01-Check", "BLR06", "Operating load rating factor less than 0.1 should be closed")
        for tag in ("BC01", "BC02", "BC03", "BC04"):
            rating = _num(bridge.get(tag))
            if rating is not None and rating < 2 and bps01 != "C":
                _add(issues, "safety", bridge_id, f"{tag}-PS01-Check", tag, "Condition rating less than 2 should be closed")
        blr07 = _num(bridge.get("BLR07"))
        if blr07 is None and blr06 is not None:
            _add(
                issues,
                "error",
                bridge_id,
                "BLR07-required-when-rated",
                "BLR07",
                "Controlling Legal Load Rating Factor is required when a rating analysis is reported",
                value=bridge.get("BLR07"),
                expected="Lowest legal-load rating factor, or BLR06 fallback only when legal loads are enveloped",
            )
        if blr07 is not None and blr07 < 1.0 and bps01 not in {"PP", "TP", "SP", "PR", "TR", "SR", "C"} and not posting_evals:
            _add(issues, "safety", bridge_id, "BLR07-PS01-Check", "BLR07", "Legal load factor below 1.0 requires posting status/evaluation")

        if posting_evals:
            legal_load_factors = [
                factor for factor in (_num(row.get("BEP02")) for row in posting_evals if isinstance(row, dict))
                if factor is not None
            ]
            if legal_load_factors and blr07 is not None:
                expected_blr07 = int(min(legal_load_factors) * 100) / 100
                if abs(blr07 - expected_blr07) > 0.0001:
                    _add(
                        issues,
                        "error",
                        bridge_id,
                        "BLR07-min-BEP02",
                        "BLR07",
                        "BLR07 must be the lowest reported legal-load rating factor from PostingEvaluations",
                        value=bridge.get("BLR07"),
                        expected=f"{expected_blr07:.2f}",
                    )

        for tag, value, table, row_index in _iter_tag_values(bridge):
            if tag in DO_NOT_REPORT_FIELDS and not _blank(value):
                _add(issues, "flag", bridge_id, f"{tag}-do-not-report", tag, f"Do not report {tag}; FHWA calculates or derives this value", table=table, row_index=row_index, value=value)
            if tag in NUMERIC_FIELDS and not _blank(value) and _num(value) is None:
                _add(issues, "error", bridge_id, f"{tag}-numeric", tag, f"{tag} must be numeric", table=table, row_index=row_index, value=value)
            if tag in INTEGER_FIELDS and not _blank(value) and not _is_integer(value):
                _add(issues, "error", bridge_id, f"{tag}-integer", tag, f"{tag} must be a whole number", table=table, row_index=row_index, value=value)
            if tag in DATE_FIELDS and not _valid_date(value):
                _add(issues, "critical" if tag in {"BIE02", "BIE03"} else "error", bridge_id, f"{tag}-date", tag, f"{tag} must be a valid YYYYMMDD date", table=table, row_index=row_index, value=value)
            if tag in CODE_LISTS and not _valid_code(tag, value):
                _add(issues, "error", bridge_id, f"{tag}-code", tag, f"{tag} value {value!r} is not in the supported SNBI code list", table=table, row_index=row_index, value=value)
            if submission_year and submission_year >= 2028 and isinstance(value, str) and value in TEMPORARY_CODES:
                _add(issues, "error", bridge_id, f"{tag}-temporary-expired", tag, f"{tag} uses temporary code {value!r}, which is not accepted for 2028+", table=table, row_index=row_index, value=value)

    counts = Counter(issue["type"] for issue in issues)
    summary = {
        "critical": counts.get("critical", 0),
        "error": counts.get("error", 0),
        "flag": counts.get("flag", 0),
        "safety": counts.get("safety", 0),
        "total": len(issues),
        "bridges_checked": len(payload),
        "legacy_bridges": legacy_count,
    }
    indexed = _index_issues(issues)
    return {
        "summary": summary,
        "issues": issues[:issue_limit],
        "truncated": len(issues) > issue_limit,
        **indexed,
    }
