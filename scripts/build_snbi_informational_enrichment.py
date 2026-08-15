#!/usr/bin/env python3
"""Build BridgeGuard's offline, informational SNBI knowledge artifact.

This development-only tool observes official FHWA publications supplied as
local files.  It deliberately does not download them and does not promote them
to BridgeGuard's ``vendored_verified`` rule registry.  The generated JSON is a
source-attributed aid for question interpretation, never executable validation
logic or evidence that an NBI submission passed FHWA/NBTIS checks.

Example::

    python scripts/build_snbi_informational_enrichment.py \
      --spec snbi_march_2022_publication.pdf \
      --crosswalk datacrosswalk.xlsx \
      --code-mapping codemapping.xlsx \
      --validation SNBI_Data_Validation_Rules.xlsx \
      --output backend/app/data/fhwa/derived/snbi_field_information_v1.json
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

import pandas as pd
import pdfplumber
from pypdf import PdfReader


OBSERVED_ON = "2026-08-13"
DATASET_VERSION = "bridgeguard-snbi-informational-enrichment-v1"

SOURCE_METADATA = {
    "snbi_march_2022": {
        "title": "Specifications for the National Bridge Inventory, FHWA-HIF-22-017",
        "authority": "FHWA",
        "artifact_state": "remote_unverified_observation",
        "legal_status": "regulatory_ibr",
        "source_url": "https://www.fhwa.dot.gov/bridge/snbi/snbi_march_2022_publication.pdf",
    },
    "nbi_snbi_crosswalk_dec_2023": {
        "title": "Data Crosswalk for Over Records and Under Records",
        "authority": "FHWA",
        "artifact_state": "remote_unverified_observation",
        "legal_status": "operational_transition_guidance",
        "source_url": "https://www.fhwa.dot.gov/bridge/snbi/datacrosswalk.cfm",
    },
    "nbi_snbi_code_mapping": {
        "title": "Detailed Code Mapping for Individual Data Items",
        "authority": "FHWA",
        "artifact_state": "remote_unverified_observation",
        "legal_status": "operational_transition_guidance",
        "source_url": "https://www.fhwa.dot.gov/bridge/snbi/codemapping.cfm",
    },
    "validation_logic_v1_1_dec_2025": {
        "title": "SNBI Data Submittal Validation Logic version 1.1",
        "authority": "FHWA",
        "artifact_state": "remote_unverified_observation",
        "legal_status": "operational_submission_guidance",
        "source_url": "https://www.fhwa.dot.gov/bridge/snbi/data_validation_logic.cfm",
    },
    "snbi_qanda_feb_2023": {
        "title": "Questions and Answers on the Specification for the National Bridge Inventory",
        "authority": "FHWA",
        "artifact_state": "remote_unverified_observation",
        "legal_status": "informational_nonbinding",
        "source_url": "https://www.fhwa.dot.gov/bridge/snbi/qanda.cfm",
    },
}

FREQUENCY_MEANINGS = {
    "I": "Initial: record initially and update when necessary; it normally does not change at every inspection.",
    "EI": "Each inspection: verify or update during every applicable inspection.",
    "C": "Calculated: FHWA calculates and stores the value; agencies do not report it.",
}

UNIT_BY_TAG = {
    "BL05": "decimal_degree",
    "BL06": "decimal_degree",
    **{tag: "foot" for tag in (
        "BG01", "BG02", "BG03", "BG04", "BG05", "BG06", "BG07", "BG08",
        "BG09", "BG13", "BH12", "BH13", "BH14", "BH15", "BH16", "BRR02",
        "BRR03", "BN02", "BN03", "BN04", "BN05",
    )},
    "BG11": "degree",
    "BG15": "square_foot",
    "BG16": "square_foot",
    "BH07": "mile",
    "BH08": "lane_count",
    "BH09": "vehicle_per_day",
    "BH10": "truck_per_day",
    "BH11": "calendar_year",
    "BH17": "mile",
    "BLR03": "calendar_date_yyyymmdd",
    "BLR05": "dimensionless_rating_factor",
    "BLR06": "dimensionless_rating_factor",
    "BLR07": "dimensionless_rating_factor",
    "BPS02": "calendar_date_yyyymmdd",
    "BEP02": "dimensionless_rating_factor",
    "BEP04": "us_ton",
    "BIE02": "calendar_date_yyyymmdd",
    "BIE03": "calendar_date_yyyymmdd",
    "BIE05": "month",
    "BIE06": "calendar_date_yyyymmdd",
    "BIE08": "calendar_date_yyyymmdd",
    "BIE09": "calendar_date_yyyymmdd",
    "BIE10": "calendar_date_yyyymmdd",
    "BE03": "element_defined_quantity_unit",
    "BCS01": "element_defined_quantity_unit",
    "BCS02": "element_defined_quantity_unit",
    "BCS03": "element_defined_quantity_unit",
    "BCS04": "element_defined_quantity_unit",
    "BW01": "calendar_year",
    "BW02": "calendar_year",
}

COUNT_TAGS = {"BSP02", "BSP03", "BSB02"}
ORDINAL_CONDITION_TAGS = {f"BC{number:02d}" for number in range(1, 16)}

CONDITION_CODE_MEANINGS = {
    "N": "Not applicable; the component does not exist.",
    "9": "Excellent",
    "8": "Very good",
    "7": "Good",
    "6": "Satisfactory",
    "5": "Fair",
    "4": "Poor",
    "3": "Serious",
    "2": "Critical",
    "1": "Imminent failure",
    "0": "Failed",
}

QA_NOTES = (
    {
        "qa_id": "T-2",
        "scope": "transition",
        "summary": (
            "Yes means every legacy value has a defined transition; Partial means only some values, "
            "temporary codes, or assumptions transition; No means the SNBI value remains unpopulated; "
            "Calculate means the value is derived. Every transitioned value still requires review."
        ),
        "source_id": "snbi_qanda_feb_2023",
    },
    {
        "qa_id": "T-3",
        "scope": "temporal",
        "summary": (
            "FHWA describes transitioned data as a 2026-2027 bridge to a complete, verified SNBI-based "
            "dataset for the March 15, 2028 submittal; temporary transition output is not native SNBI evidence."
        ),
        "source_id": "snbi_qanda_feb_2023",
    },
    {
        "qa_id": "F-1/F-2",
        "scope": "submission_format",
        "summary": "SNBI submission uses JSON and combines bridge and element data in the submission schema.",
        "source_id": "snbi_qanda_feb_2023",
    },
    {
        "qa_id": "BB-1..BB-6",
        "scope": "border_bridges",
        "summary": (
            "A lead State supplies the full record; a neighboring State supplies only the prescribed "
            "abbreviated fields and highway features, using matching BF01 identifiers."
        ),
        "source_id": "snbi_qanda_feb_2023",
    },
    {
        "qa_id": "S4F-1",
        "scope": "feature_cardinality",
        "summary": (
            "Use a distinct W## for each unique waterway; multiple branches of one waterway remain one "
            "feature, while F## represents a separate flood-stage relief channel."
        ),
        "source_id": "snbi_qanda_feb_2023",
    },
)

_ITEM_HEADER = re.compile(
    r"Format\s+(.+?)\s+Frequency\s+(I|EI|C)\s+Item ID\s+(B\.[A-Z.]+\d+)",
    re.IGNORECASE,
)
_TAG = re.compile(r"\bB([A-Z]{1,3})(\d{2})\b")
_DOTTED_TAG = re.compile(r"\bB\.([A-Z]{1,3})\.(\d{1,2})\b")


def _stable_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text(value: object, *, limit: int = 420) -> str | None:
    if value is None or pd.isna(value):
        return None
    normalized = " ".join(str(value).replace("\u00a0", " ").split())
    if not normalized or normalized.casefold() == "nan":
        return None
    if len(normalized) <= limit:
        return normalized
    shortened = normalized[: limit - 1].rsplit(" ", 1)[0]
    return f"{shortened}…"


def _tag(value: object) -> str | None:
    text = _text(value, limit=80)
    if not text:
        return None
    compact = re.sub(r"[^A-Za-z0-9]", "", text).upper()
    return compact if re.fullmatch(r"B[A-Z]{1,3}\d{2}", compact) else None


# Keep generated narrative ASCII-clean even when a Windows shell decoded the
# original typographic ellipsis through a legacy code page.
_base_text = _text


def _text(value: object, *, limit: int = 420) -> str | None:
    normalized = _base_text(value, limit=limit)
    return normalized.replace("\u00e2\u20ac\u00a6", "...") if normalized else normalized


def _iter_outline(items: Iterable[object]) -> Iterable[object]:
    for item in items:
        if isinstance(item, list):
            yield from _iter_outline(item)
        else:
            yield item


def _pdf_items(reader: PdfReader) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for destination in _iter_outline(reader.outline):
        try:
            page_index = reader.get_destination_page_number(destination)
        except Exception:
            continue
        page_text = " ".join((reader.pages[page_index].extract_text() or "").split())
        match = _ITEM_HEADER.search(page_text)
        if not match:
            continue
        normalized_tag = match.group(3).replace(".", "").upper()
        items.append(
            {
                "tag": normalized_tag,
                "title": str(getattr(destination, "title", normalized_tag)).strip(),
                "pdf_page": page_index + 1,
                "publication_page": page_index - 18,
                "format": " ".join(match.group(1).split()),
                "frequency": match.group(2).upper(),
            }
        )
    items.sort(key=lambda item: int(item["pdf_page"]))
    tags = [str(item["tag"]) for item in items]
    if len(items) != 154 or len(set(tags)) != 154:
        raise RuntimeError(
            f"expected 154 unique SNBI PDF item destinations, found {len(items)} / {len(set(tags))}"
        )
    for index, item in enumerate(items):
        # B.W.03 continues through its examples on PDF page 331. Page 332
        # starts Appendix A and is not part of the field specification.
        next_page = int(items[index + 1]["pdf_page"]) if index + 1 < len(items) else 332
        item["end_pdf_page"] = next_page - 1
    return items


def _normalize_pdf_text(value: str) -> str:
    value = value.replace("\u00a0", " ").replace("\u2011", "-")
    value = re.sub(r"(?<=\w)-\n(?=\w)", "-", value)
    return " ".join(value.replace("\n", " ").split())


def _selected_notes(raw: str, *, title: str, commentary: bool) -> list[str]:
    if not raw:
        return []
    # Code tables are captured structurally and should not bloat narrative context.
    raw = raw.split("Code Description", 1)[0]
    normalized = _normalize_pdf_text(raw)
    if not normalized:
        return []
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", normalized)
    cues = (
        "only",
        "when",
        "do not",
        "must",
        "measure",
        "round",
        "truncate",
        "update",
        "intended",
        "does not",
        "not sufficient",
        "calculated",
    )
    selected: list[str] = []
    for sentence in sentences:
        sentence = sentence.strip(" -")
        if not sentence or sentence.startswith(("http://", "https://")):
            continue
        lowered = sentence.casefold()
        if not selected or any(cue in lowered for cue in cues):
            # A short paraphrasing prefix keeps the artifact compact and avoids
            # presenting a long copied paragraph as BridgeGuard-authored prose.
            sentence = re.sub(r"^Report\s+", "Value requirement: ", sentence)
            sentence = re.sub(r"^Do not report\s+", "Omit ", sentence)
            sentence = re.sub(r"^This item\s+", f"{title} ", sentence)
            note = _text(sentence, limit=220)
            if note and note not in selected:
                selected.append(note)
        if len(selected) >= (3 if commentary else 5):
            break
    return selected


_CODE_START = re.compile(r"^([A-Z0-9][A-Z0-9./|#-]{0,14})\s+(.+)$")
_CODE_STOP = (
    "codes continued",
    "code continued",
    "do not report",
    "report this",
    "report the",
    "when ",
    "for ",
    "refer ",
    "use ",
    "example",
    "table ",
)


# The State Code table is printed as three code/description pairs per line in
# the PDF. Restrict parsing to its published code set so numbers inside prose
# can never be interpreted as codes.
_STATE_FIPS_CODES = frozenset(
    {
        "1", "2", "4", "5", "6", "8", "9", "10", "11", "12", "13", "15",
        "16", "17", "18", "19", "20", "21", "22", "23", "24", "25", "26",
        "27", "28", "29", "30", "31", "32", "33", "34", "35", "36", "37",
        "38", "39", "40", "41", "42", "44", "45", "46", "47", "48", "49",
        "50", "51", "53", "54", "55", "56", "60", "64", "66", "68", "69",
        "70", "72", "74", "78",
    }
)


_ROADSIDE_HARDWARE_CODE_MEANINGS = {
    "N": "Not applicable; roadside hardware is not required.",
    "MYY1..MYY6": "Crash-tested successfully under AASHTO MASH for test level 1 through 6.",
    "3501..3506": "Crash-tested successfully under NCHRP Report 350 for test level 1 through 6.",
    "2301..2303": "Crash-tested successfully under NCHRP Report 230 for test level 1 through 3.",
    "2391..2393": "Crash-tested successfully under NCHRP Report 239 for test level 1 through 3.",
    "891..893": "Crash-tested successfully under the 1989 AASHTO bridge-railing guide for test level 1 through 3.",
    "X": "Crash-tested successfully under other criteria.",
    "AYY": "Not crash-tested, but meets AASHTO Standard Specifications for Highway Bridges.",
    "SYY": "Not crash-tested, but meets approved agency standards.",
    "I": "Not crash-tested and does not meet approved agency standards.",
    "0": "Required roadside hardware is not present.",
}


_WORK_CODE_MEANINGS = {
    "BR1": "Bridge replaced.",
    "IP1": "Bridge widened.",
    "IP2": "Bridge raised.",
    "IP3": "Bridge strengthened by retrofit.",
    "IP4": "Seismic retrofit.",
    "DK1": "Deck replaced.",
    "SP1": "Superstructure replaced.",
    "SB1": "Substructure replaced.",
    "DK2": "Major deck rehabilitation.",
    "SP2": "Major superstructure rehabilitation.",
    "SB2": "Major substructure rehabilitation.",
    "CU2": "Major culvert rehabilitation.",
    "DK3": "Minor deck rehabilitation.",
    "SP3": "Minor superstructure rehabilitation.",
    "SB3": "Minor substructure rehabilitation.",
    "CU3": "Minor culvert rehabilitation.",
    "DK4": "Deck overlaid.",
    "CU4": "Culvert overlaid.",
    "DK5": "Deck sealed.",
    "SP5": "Superstructure sealed.",
    "SB5": "Substructure sealed.",
    "CU5": "Culvert sealed.",
    "SP6": "New or replaced superstructure coating.",
    "SB6": "New or replaced substructure coating.",
    "CU6": "New or replaced culvert coating.",
    "SP7": "Superstructure coating preserved.",
    "SB7": "Substructure coating preserved.",
    "CU7": "Culvert coating preserved.",
    "BG1": "Bearings installed or replaced.",
    "JT1": "Deck joints installed or replaced.",
    "RT1": "Bridge railings or transitions installed or replaced.",
    "SC1": "Scour countermeasures installed or replaced.",
    "CP1": "Channel protection installed or replaced.",
    "BG2": "Bearings repaired.",
    "JT2": "Deck joints repaired.",
    "RT2": "Bridge railings or transitions repaired.",
    "SC2": "Scour countermeasures repaired.",
    "CP2": "Channel protection repaired.",
    "CH1": "Channel condition improved.",
}


def _state_code_meanings(raw: str) -> dict[str, str]:
    output: dict[str, str] = {}
    active_code: str | None = None
    codes = "|".join(sorted(_STATE_FIPS_CODES, key=lambda value: (-len(value), value)))
    matcher = re.compile(rf"(?<!\S)({codes})(?=\s)")
    for line in raw.splitlines():
        matches = list(matcher.finditer(line))
        if not matches:
            if active_code and line.strip():
                output[active_code] = f"{output[active_code]} {line.strip()}"
            continue
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
            description = line[match.end():end].strip()
            if description:
                active_code = match.group(1)
                output[active_code] = description
    return {code: _text(description, limit=160) or "" for code, description in output.items()}


def _code_meanings(raw: str) -> dict[str, str]:
    output: dict[str, str] = {}
    raw = raw.replace("Code\nDescription", "Code Description")
    if "Code Description" not in raw:
        return output
    for block in raw.split("Code Description")[1:]:
        current: str | None = None
        for original in block.splitlines():
            line = " ".join(original.split()).strip()
            if not line:
                continue
            lowered = line.casefold()
            if any(lowered.startswith(prefix) for prefix in _CODE_STOP):
                break
            match = _CODE_START.match(line)
            if match and (
                not match.group(1).isalpha()
                or (
                    len(match.group(1)) <= 5
                    and match.group(1).upper() == match.group(1)
                )
            ):
                code, description = match.groups()
                if code not in {"AN", "N", "YYYYMMDD"}:
                    output[code] = description
                    current = code
                    continue
            if current and len(output[current]) < 180:
                output[current] = f"{output[current]} {line}"
        # Each specification block contains at most one code table.
    return {key: _text(value, limit=200) or "" for key, value in output.items()}


def _table_sections(table: list[list[str | None]]) -> tuple[list[str], list[str]]:
    specification: list[str] = []
    commentary: list[str] = []
    mode: str | None = None
    split_index: int | None = None
    for row in table:
        cells = [cell or "" for cell in row]
        joined = " ".join(cells)
        lowered = joined.casefold()
        if "example" in lowered and "specification" not in lowered and "commentary" not in lowered:
            mode = None
            continue
        spec_positions = [i for i, cell in enumerate(cells) if "specification" in cell.casefold()]
        comment_positions = [i for i, cell in enumerate(cells) if "commentary" in cell.casefold()]
        if spec_positions and comment_positions:
            mode = "split"
            split_index = (spec_positions[0] + comment_positions[0]) // 2
            continue
        if spec_positions:
            mode = "specification"
            continue
        if comment_positions:
            mode = "commentary"
            continue
        if mode == "split" and split_index is not None:
            left = "\n".join(cell for i, cell in enumerate(cells) if i <= split_index and cell)
            right = "\n".join(cell for i, cell in enumerate(cells) if i > split_index and cell)
            if left:
                specification.append(left)
            if right:
                commentary.append(right)
        elif mode == "specification":
            value = "\n".join(cell for cell in cells if cell)
            if value:
                specification.append(value)
        elif mode == "commentary":
            value = "\n".join(cell for cell in cells if cell)
            if value:
                commentary.append(value)
    return specification, commentary


def _pdf_knowledge(pdf_path: Path, items: list[dict[str, object]]) -> None:
    with pdfplumber.open(pdf_path) as pdf:
        for item in items:
            specification_parts: list[str] = []
            commentary_parts: list[str] = []
            for page_number in range(int(item["pdf_page"]), int(item["end_pdf_page"]) + 1):
                page = pdf.pages[page_number - 1]
                page_text = page.extract_text() or ""
                # An un-bookmarked subsection introduction can appear before
                # the next item destination. It is not evidence for the prior
                # item, so stop rather than leaking its shared tables backward.
                if page_number != int(item["pdf_page"]) and re.match(
                    r"(?:SUBSECTION\b|APPENDIX\b)",
                    page_text.lstrip(),
                    re.IGNORECASE,
                ):
                    break
                for table in page.extract_tables():
                    spec, commentary = _table_sections(table)
                    specification_parts.extend(spec)
                    commentary_parts.extend(commentary)
            raw_spec = "\n".join(specification_parts)
            raw_commentary = "\n".join(commentary_parts)
            item["reporting_notes"] = _selected_notes(
                raw_spec,
                title=str(item["title"]),
                commentary=False,
            )
            item["interpretive_notes"] = _selected_notes(
                raw_commentary,
                title=str(item["title"]),
                commentary=True,
            )
            if item["tag"] == "BL01":
                item["code_meanings"] = _state_code_meanings(raw_spec)
            elif item["tag"] in {"BRH01", "BRH02"}:
                # Both fields reference the shared Table 6 immediately before
                # their item pages; keep the same published domain for each.
                item["code_meanings"] = dict(_ROADSIDE_HARDWARE_CODE_MEANINGS)
            elif item["tag"] == "BW03":
                item["code_meanings"] = dict(_WORK_CODE_MEANINGS)
            else:
                item["code_meanings"] = _code_meanings(raw_spec)


def _sheet_tags(sheet_name: str) -> tuple[str, ...]:
    prefix = re.search(r"B\.([A-Z]{1,3})\.", sheet_name)
    if not prefix:
        return ()
    section = prefix.group(1)
    numbers = re.findall(r"\d{2}", sheet_name)
    return tuple(f"B{section}{number}" for number in numbers)


def _code_mapping(code_path: Path, known_tags: set[str]) -> dict[str, dict[str, str]]:
    output: dict[str, dict[str, str]] = defaultdict(dict)
    workbook = pd.ExcelFile(code_path)
    for sheet in workbook.sheet_names:
        tags = tuple(tag for tag in _sheet_tags(sheet) if tag in known_tags)
        if not tags:
            continue
        frame = pd.read_excel(code_path, sheet_name=sheet, header=None, dtype=object)
        for row_index, row in frame.iterrows():
            values = [_text(value, limit=200) for value in row.tolist()]
            try:
                value_index = values.index("SNBI Value")
                description_index = values.index("SNBI Description")
            except ValueError:
                continue
            for next_index in range(row_index + 1, len(frame)):
                code = _text(frame.iat[next_index, value_index], limit=40)
                description = _text(frame.iat[next_index, description_index], limit=200)
                if code == "SNBI Value" or description == "SNBI Description":
                    break
                if not code and not description:
                    break
                if not code or not description or code.casefold().startswith("no value"):
                    continue
                for tag in tags:
                    output[tag].setdefault(code, description)
    return dict(output)


def _transition_rows(
    crosswalk_path: Path,
    known_tags: set[str],
) -> dict[str, list[dict[str, object]]]:
    output: dict[str, list[dict[str, object]]] = defaultdict(list)
    for sheet_name, record_kind, extra_column in (
        ("Crosswalk for Over Records", "over", "Additional Notes"),
        ("Crosswalk for Under Records", "under", "Notes for the Team"),
    ):
        frame = pd.read_excel(crosswalk_path, sheet_name=sheet_name, header=1, dtype=object)
        for row_index, row in frame.iterrows():
            tag = _tag(row.get("Data Tag"))
            if tag not in known_tags:
                continue
            legacy_id = _text(row.get("1995 Coding Guide ID"), limit=80)
            legacy_name = _text(row.get("1995 Coding Guide Item Name/Description"), limit=180)
            legacy_format = _text(
                row.get("1995 Coding Guide Format (as shown in Appendix E)"),
                limit=100,
            )
            output[tag].append(
                {
                    "record_kind": record_kind,
                    "category": (_text(row.get("Clean Transition?"), limit=40) or "unavailable").casefold(),
                    "legacy_item_id": legacy_id,
                    "legacy_item_name": legacy_name,
                    "legacy_format": legacy_format,
                    "rule_summary": _text(row.get("Transition Notes for Developer")),
                    "additional_note": _text(row.get(extra_column)),
                    "source_id": "nbi_snbi_crosswalk_dec_2023",
                    "source_locator": f"{sheet_name}, row {int(row_index) + 3}",
                }
            )
    return dict(output)


def _tags_in_rule(values: Iterable[object], known_tags: set[str]) -> set[str]:
    output: set[str] = set()
    combined = " ".join(_text(value, limit=1000) or "" for value in values)
    for match in _TAG.finditer(combined.upper()):
        candidate = f"B{match.group(1)}{match.group(2)}"
        if candidate in known_tags:
            output.add(candidate)
    for match in _DOTTED_TAG.finditer(combined.upper()):
        candidate = f"B{match.group(1)}{int(match.group(2)):02d}"
        if candidate in known_tags:
            output.add(candidate)
    return output


def _validation_rows(
    validation_path: Path,
    known_tags: set[str],
) -> dict[str, list[dict[str, object]]]:
    raw = pd.read_excel(
        validation_path,
        sheet_name="SNBI Data Validation Rules",
        header=None,
        dtype=object,
    )
    header_row = next(
        index
        for index, row in raw.iterrows()
        if any(_text(value, limit=80) == "Data Tag" for value in row.tolist())
    )
    frame = pd.read_excel(
        validation_path,
        sheet_name="SNBI Data Validation Rules",
        header=header_row,
        dtype=object,
    )
    output: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row_index, row in frame.iterrows():
        severity = _text(row.get("Error Type"), limit=80)
        rule_id = _text(row.get("Rule Name"), limit=100)
        description = _text(row.get("Error Description"), limit=260)
        explanation = _text(row.get("Explanation"), limit=260)
        value_range = _text(row.get("Range of Values"), limit=180)
        if not any((severity, rule_id, description, explanation, value_range)):
            continue
        tags = _tags_in_rule(row.tolist(), known_tags)
        direct_tag = _tag(row.get("Data Tag"))
        if direct_tag in known_tags:
            tags.add(direct_tag)
        record = {
            "severity": severity,
            "rule_id": rule_id,
            "condition_summary": description,
            "explanation_summary": explanation if explanation != description else None,
            "allowed_or_expected_values": value_range,
            "source_id": "validation_logic_v1_1_dec_2025",
            "source_locator": (
                f"SNBI Data Validation Rules, row {int(row_index) + header_row + 2}"
            ),
            "authority_status": "informational_remote_observation_not_executed",
        }
        for tag in sorted(tags):
            linked_record = {
                **record,
                "relationship": (
                    "direct_target" if tag == direct_tag else "cross_field_reference"
                ),
            }
            if linked_record not in output[tag]:
                output[tag].append(linked_record)
    return dict(output)


def _unit(tag: str) -> str | None:
    if tag in UNIT_BY_TAG:
        return UNIT_BY_TAG[tag]
    if tag in COUNT_TAGS:
        return "count"
    if tag in ORDINAL_CONDITION_TAGS:
        return "ordinal_condition_code"
    return None


def _applicability_cautions(
    validation_notes: Iterable[Mapping[str, object]],
) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    cues = ("when ", "if ", "required", "do not report", "not valid", "must be reported")
    for note in validation_notes:
        combined = " ".join(
            str(note.get(key) or "")
            for key in ("explanation_summary", "condition_summary")
        ).strip()
        if not combined or not any(cue in combined.casefold() for cue in cues):
            continue
        summary = _text(combined, limit=260)
        if not summary:
            continue
        candidate = {
            "summary": summary,
            "rule_id": str(note.get("rule_id") or "unavailable"),
            "source_id": str(note["source_id"]),
            "source_locator": str(note["source_locator"]),
        }
        if candidate not in output:
            output.append(candidate)
        if len(output) >= 5:
            break
    return output


def build(
    *,
    spec_path: Path,
    crosswalk_path: Path,
    code_mapping_path: Path,
    validation_path: Path,
) -> dict[str, object]:
    reader = PdfReader(spec_path)
    items = _pdf_items(reader)
    _pdf_knowledge(spec_path, items)
    known_tags = {str(item["tag"]) for item in items}
    code_mapping = _code_mapping(code_mapping_path, known_tags)
    transitions = _transition_rows(crosswalk_path, known_tags)
    validations = _validation_rows(validation_path, known_tags)

    for item in items:
        tag = str(item["tag"])
        codes = dict(item.pop("code_meanings"))
        code_meaning_source_ids = ["snbi_march_2022"] if codes else []
        codes.update(code_mapping.get(tag, {}))
        if tag in code_mapping:
            code_meaning_source_ids.append("nbi_snbi_code_mapping")
        if tag in {f"BC{number:02d}" for number in range(1, 8)}:
            codes.update(CONDITION_CODE_MEANINGS)
        item["frequency_meaning"] = FREQUENCY_MEANINGS[str(item["frequency"])]
        item["specification_locator"] = f"publication page {item['publication_page']}"
        item["unit"] = _unit(tag)
        item["unit_source_id"] = "snbi_march_2022" if item["unit"] else None
        item["code_meanings"] = dict(sorted(codes.items()))
        item["code_meaning_source_ids"] = sorted(set(code_meaning_source_ids))
        temporary_codes = sorted(
            code
            for code, description in codes.items()
            if code.endswith("-T") or description.casefold().startswith("temp")
        )
        item["code_profile_cautions"] = (
            [
                {
                    "codes": temporary_codes,
                    "summary": (
                        "Temporary transition codes are aids for the 2026-2027 transition "
                        "period; do not treat them as permanent native-SNBI domain values."
                    ),
                    "source_ids": [
                        "nbi_snbi_code_mapping",
                        "snbi_qanda_feb_2023",
                    ],
                }
            ]
            if temporary_codes
            else []
        )
        item["transition"] = transitions.get(tag, [])
        item["validation_notes"] = validations.get(tag, [])
        item["applicability_cautions"] = _applicability_cautions(item["validation_notes"])
        item["temporal_caution_ids"] = ["T-3"]
        item["source_ids"] = sorted(
            {
                "snbi_march_2022",
                "snbi_qanda_feb_2023",
                *(record["source_id"] for record in item["transition"]),
                *(record["source_id"] for record in item["validation_notes"]),
                *( ["nbi_snbi_code_mapping"] if tag in code_mapping else [] ),
            }
        )
        item["knowledge_status"] = "informational_only_remote_sources_not_compliance_verified"

    source_paths = {
        "snbi_march_2022": spec_path,
        "nbi_snbi_crosswalk_dec_2023": crosswalk_path,
        "nbi_snbi_code_mapping": code_mapping_path,
        "validation_logic_v1_1_dec_2025": validation_path,
    }
    sources: list[dict[str, object]] = []
    for source_id, metadata in SOURCE_METADATA.items():
        source = {"source_id": source_id, **metadata, "observed_on": OBSERVED_ON}
        source["observed_sha256"] = (
            _sha256(source_paths[source_id]) if source_id in source_paths else None
        )
        source["verification_boundary"] = (
            "The digest identifies bytes observed during enrichment generation only. "
            "The source remains remote_unverified and cannot enable an official validation profile."
        )
        sources.append(source)

    payload: dict[str, object] = {
        "dataset_version": DATASET_VERSION,
        "observed_on": OBSERVED_ON,
        "authority_boundary": (
            "Informational, source-attributed FHWA field knowledge for question interpretation. "
            "It does not execute the FHWA validation workbook, certify a transition, replace the "
            "governing specification/profile, or show acceptance by FHWA/NBTIS. Remote observations "
            "remain remote_unverified even when an observation digest is recorded."
        ),
        "may_satisfy_official_validation": False,
        "runtime_network_required": False,
        "field_count": len(items),
        "fields": items,
        "q_and_a_notes": list(QA_NOTES),
        "sources": sources,
        "coverage": {
            "format_and_frequency_fields": sum(bool(item["format"] and item["frequency"]) for item in items),
            "reporting_note_fields": sum(bool(item["reporting_notes"]) for item in items),
            "interpretive_note_fields": sum(bool(item["interpretive_notes"]) for item in items),
            "unit_fields": sum(bool(item["unit"]) for item in items),
            "code_meaning_fields": sum(bool(item["code_meanings"]) for item in items),
            "transition_fields": sum(bool(item["transition"]) for item in items),
            "validation_note_fields": sum(bool(item["validation_notes"]) for item in items),
            "validation_rule_links": sum(len(item["validation_notes"]) for item in items),
            "applicability_caution_fields": sum(bool(item["applicability_cautions"]) for item in items),
            "temporary_code_caution_fields": sum(bool(item["code_profile_cautions"]) for item in items),
        },
    }
    payload["dataset_sha256"] = hashlib.sha256(
        _stable_json(payload).encode("utf-8")
    ).hexdigest()
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--crosswalk", required=True, type=Path)
    parser.add_argument("--code-mapping", required=True, type=Path)
    parser.add_argument("--validation", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    for path in (args.spec, args.crosswalk, args.code_mapping, args.validation):
        if not path.is_file():
            parser.error(f"source file does not exist: {path}")
    payload = build(
        spec_path=args.spec,
        crosswalk_path=args.crosswalk,
        code_mapping_path=args.code_mapping,
        validation_path=args.validation,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(encoded, encoding="utf-8", newline="\n")
    temporary.replace(args.output)
    print(json.dumps(payload["coverage"], sort_keys=True))
    print(f"dataset_sha256={payload['dataset_sha256']}")


if __name__ == "__main__":
    main()
