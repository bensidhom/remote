"""Strict, provenance-preserving parser for legacy FHWA NBI records.

The parser is intentionally isolated from the legacy production parser.  It
accepts bytes or a binary stream, performs no lossy decoding, and never chooses
the last duplicate record implicitly.  Callers must explicitly identify the
source agency and text encoding.
"""

from __future__ import annotations

import codecs
import csv
import hashlib
from collections import defaultdict
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import (
    Any,
    BinaryIO,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)


FIXED_RECORD_BYTES = 445
UTF8_BOM = codecs.BOM_UTF8
_REQUIRED_CSV_HEADERS = frozenset(
    {"STATE_CODE_001", "STRUCTURE_NUMBER_008", "RECORD_TYPE_005A"}
)
_SUPPORTED_ENCODINGS = frozenset({"ascii", "utf-8", "utf-8-sig", "iso8859-1", "cp1252"})


class NBIFormat(str, Enum):
    FHWA_FIXED_445 = "fhwa_fixed_445"
    NAMED_CSV = "named_csv"
    LEGACY_POSITIONAL_CSV = "legacy_positional_csv"


class IssuePolicy(str, Enum):
    REJECT = "reject"
    QUARANTINE = "quarantine"
    ACCEPT_WITH_ISSUE = "accept_with_issue"


class IssueSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    REVIEW = "review"


class RecordType(str, Enum):
    OVER = "1"
    UNDER = "2"
    UNDER_ALTERNATE = "A"


class NBIParseError(ValueError):
    """A file or record was rejected under the selected strict policy."""

    def __init__(self, issue: "ParseIssue") -> None:
        super().__init__(f"{issue.code}: {issue.message}")
        self.issue = issue


class LegacyAdapterCollisionError(ValueError):
    """The old structure-number-only result shape would merge distinct keys."""


@dataclass(frozen=True, order=True)
class LegacyBridgeKey:
    source_agency_context: str
    normalized_state: str
    raw_structure_number: str


@dataclass(frozen=True)
class ParseIssue:
    code: str
    message: str
    severity: IssueSeverity
    line_number: Optional[int] = None
    byte_offset: Optional[int] = None
    raw_sha256: Optional[str] = None
    source_record_id: Optional[str] = None
    bridge_key: Optional[LegacyBridgeKey] = None


@dataclass(frozen=True)
class SourceRecord:
    source_order: int
    line_number: int
    byte_offset: int
    raw_sha256: str
    source_record_id: str
    record_type: RecordType
    bridge_key: LegacyBridgeKey
    raw_item_001: str
    raw_item_008: str
    fields: Mapping[str, Optional[str]]
    conformant: bool
    issue_codes: Tuple[str, ...] = ()
    csv_extra_values: Tuple[str, ...] = ()


@dataclass(frozen=True)
class QuarantinedRecord:
    line_number: int
    byte_offset: int
    raw_sha256: str
    source_record_id: str
    issue_codes: Tuple[str, ...]
    bridge_key: Optional[LegacyBridgeKey] = None


@dataclass(frozen=True)
class LegacyBridgeRecords:
    key: LegacyBridgeKey
    over: Optional[SourceRecord]
    duplicate_overs: Tuple[SourceRecord, ...]
    unders: Tuple[SourceRecord, ...]
    primary_under: Optional[SourceRecord]
    secondary_unders: Tuple[SourceRecord, ...]


@dataclass(frozen=True)
class ParseResult:
    input_format: NBIFormat
    encoding: str
    source_agency_context: str
    source_sha256: str
    bytes_read: int
    records_seen: int
    records: Tuple[SourceRecord, ...]
    quarantined_records: Tuple[QuarantinedRecord, ...]
    bridges: Mapping[LegacyBridgeKey, LegacyBridgeRecords]
    issues: Tuple[ParseIssue, ...]
    csv_header: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CsvDialect:
    delimiter: str = ","
    quotechar: str = '"'
    escapechar: Optional[str] = None
    doublequote: bool = True
    skipinitialspace: bool = False

    def reader_kwargs(self) -> Dict[str, Any]:
        if len(self.delimiter) != 1:
            raise ValueError("CSV delimiter must be exactly one character")
        if len(self.quotechar) != 1:
            raise ValueError("CSV quotechar must be exactly one character")
        if self.escapechar is not None and len(self.escapechar) != 1:
            raise ValueError("CSV escapechar must be exactly one character")
        return {
            "delimiter": self.delimiter,
            "quotechar": self.quotechar,
            "escapechar": self.escapechar,
            "doublequote": self.doublequote,
            "skipinitialspace": self.skipinitialspace,
            "strict": True,
        }


@dataclass(frozen=True)
class ParserLimits:
    max_bytes: int = 64 * 1024 * 1024
    max_records: int = 1_000_000
    max_line_bytes: int = 1024 * 1024

    def validate(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if self.max_records <= 0:
            raise ValueError("max_records must be positive")
        if self.max_line_bytes < FIXED_RECORD_BYTES:
            raise ValueError(
                f"max_line_bytes must be at least {FIXED_RECORD_BYTES}"
            )


@dataclass(frozen=True)
class _RawLine:
    data: bytes
    line_number: int
    byte_offset: int


# Official legacy FHWA 445-byte layout.  Offsets are byte offsets, not Unicode
# character offsets.  Item 1 intentionally retains all three bytes.
NBI_FIXED_POSITIONS: Tuple[Tuple[str, int, int], ...] = (
    ("STATE_CODE_001", 0, 3),
    ("STRUCTURE_NUMBER_008", 3, 18),
    ("RECORD_TYPE_005A", 18, 19),
    ("ROUTE_PREFIX_005B", 19, 20),
    ("SERVICE_LEVEL_005C", 20, 21),
    ("ROUTE_NUMBER_005D", 21, 26),
    ("DIRECTION_005E", 26, 27),
    ("HIGHWAY_DISTRICT_002", 27, 29),
    ("COUNTY_CODE_003", 29, 32),
    ("PLACE_CODE_004", 32, 37),
    ("FEATURES_DESC_006A", 37, 61),
    ("CRITICAL_FACILITY_006B", 61, 62),
    ("FACILITY_CARRIED_007", 62, 80),
    ("LOCATION_009", 80, 105),
    ("MIN_VERT_CLR_010", 105, 109),
    ("KILOPOINT_011", 109, 116),
    ("BASE_HWY_NETWORK_012", 116, 117),
    ("LRS_INV_ROUTE_013A", 117, 127),
    ("SUBROUTE_NO_013B", 127, 129),
    ("LAT_016", 129, 137),
    ("LONG_017", 137, 146),
    ("DETOUR_KILOS_019", 146, 149),
    ("TOLL_020", 149, 150),
    ("MAINTENANCE_021", 150, 152),
    ("OWNER_022", 152, 154),
    ("FUNCTIONAL_CLASS_026", 154, 156),
    ("YEAR_BUILT_027", 156, 160),
    ("TRAFFIC_LANES_ON_028A", 160, 162),
    ("TRAFFIC_LANES_UND_028B", 162, 164),
    ("ADT_029", 164, 170),
    ("YEAR_ADT_030", 170, 174),
    ("DESIGN_LOAD_031", 174, 175),
    ("APPR_WIDTH_MT_032", 175, 179),
    ("MEDIAN_CODE_033", 179, 180),
    ("DEGREES_SKEW_034", 180, 182),
    ("STRUCTURE_FLARED_035", 182, 183),
    ("RAILINGS_036A", 183, 184),
    ("TRANSITIONS_036B", 184, 185),
    ("APPR_RAIL_036C", 185, 186),
    ("APPR_RAIL_END_036D", 186, 187),
    ("HISTORY_037", 187, 188),
    ("NAVIGATION_038", 188, 189),
    ("NAV_VERT_CLR_MT_039", 189, 193),
    ("NAV_HORR_CLR_MT_040", 193, 198),
    ("OPEN_CLOSED_POSTED_041", 198, 199),
    ("SERVICE_ON_042A", 199, 200),
    ("SERVICE_UND_042B", 200, 201),
    ("STRUCTURE_KIND_043A", 201, 202),
    ("STRUCTURE_TYPE_043B", 202, 204),
    ("APPR_KIND_044A", 204, 205),
    ("APPR_TYPE_044B", 205, 207),
    ("MAIN_UNIT_SPANS_045", 207, 210),
    ("APPR_SPANS_046", 210, 214),
    ("HORR_CLR_MT_047", 214, 217),
    ("MAX_SPAN_LEN_MT_048", 217, 222),
    ("STRUCTURE_LEN_MT_049", 222, 228),
    ("LEFT_CURB_MT_050A", 228, 231),
    ("RIGHT_CURB_MT_050B", 231, 234),
    ("ROADWAY_WIDTH_MT_051", 234, 238),
    ("DECK_WIDTH_MT_052", 238, 242),
    ("VERT_CLR_OVER_MT_053", 242, 246),
    ("VERT_CLR_UND_REF_054A", 246, 247),
    ("VERT_CLR_UND_054B", 247, 251),
    ("LAT_UND_REF_055A", 251, 252),
    ("LAT_UND_MT_055B", 252, 255),
    ("LEFT_LAT_UND_MT_056", 255, 258),
    ("DECK_COND_058", 258, 259),
    ("SUPERSTRUCTURE_COND_059", 259, 260),
    ("SUBSTRUCTURE_COND_060", 260, 261),
    ("CHANNEL_COND_061", 261, 262),
    ("CULVERT_COND_062", 262, 263),
    ("OPR_RATING_METH_063", 263, 264),
    ("OPERATING_RATING_064", 264, 267),
    ("INV_RATING_METH_065", 267, 268),
    ("INVENTORY_RATING_066", 268, 271),
    ("STRUCTURAL_EVAL_067", 271, 272),
    ("DECK_GEOMETRY_EVAL_068", 272, 273),
    ("UNDCLRENCE_EVAL_069", 273, 274),
    ("POSTING_EVAL_070", 274, 275),
    ("WATERWAY_EVAL_071", 275, 276),
    ("APPR_ROAD_EVAL_072", 276, 277),
    ("WORK_PROPOSED_075A", 277, 279),
    ("WORK_DONE_BY_075B", 279, 280),
    ("IMP_LEN_MT_076", 280, 286),
    ("DATE_OF_INSPECT_090", 286, 290),
    ("INSPECT_FREQ_MONTHS_091", 290, 292),
    ("FRACTURE_092A", 292, 295),
    ("UNDWATER_LOOK_SEE_092B", 295, 298),
    ("SPEC_INSPECT_092C", 298, 301),
    ("FRACTURE_LAST_DATE_093A", 301, 305),
    ("UNDWATER_LAST_DATE_093B", 305, 309),
    ("SPEC_LAST_DATE_093C", 309, 313),
    ("BRIDGE_IMP_COST_094", 313, 319),
    ("ROADWAY_IMP_COST_095", 319, 325),
    ("TOTAL_IMP_COST_096", 325, 331),
    ("YEAR_OF_IMP_097", 331, 335),
    ("OTHER_STATE_CODE_098A", 335, 338),
    ("OTHER_STATE_PCNT_098B", 338, 340),
    ("OTHR_STATE_STRUC_NO_099", 340, 355),
    ("STRAHNET_HIGHWAY_100", 355, 356),
    ("PARALLEL_STRUCTURE_101", 356, 357),
    ("TRAFFIC_DIRECTION_102", 357, 358),
    ("TEMP_STRUCTURE_103", 358, 359),
    ("HIGHWAY_SYSTEM_104", 359, 360),
    ("FEDERAL_LANDS_105", 360, 361),
    ("YEAR_RECONSTRUCTED_106", 361, 365),
    ("DECK_STRUCTURE_TYPE_107", 365, 366),
    ("SURFACE_TYPE_108A", 366, 367),
    ("MEMBRANE_TYPE_108B", 367, 368),
    ("DECK_PROTECTION_108C", 368, 369),
    ("PERCENT_ADT_TRUCK_109", 369, 371),
    ("NATIONAL_NETWORK_110", 371, 372),
    ("PIER_PROTECTION_111", 372, 373),
    ("BRIDGE_LEN_IND_112", 373, 374),
    ("SCOUR_CRITICAL_113", 374, 375),
    ("FUTURE_ADT_114", 375, 381),
    ("YEAR_OF_FUTURE_ADT_115", 381, 385),
    ("MIN_NAV_CLR_MT_116", 385, 389),
    ("FED_AGENCY", 389, 390),
    ("SUBMITTED_BY", 390, 392),
    ("BRIDGE_CONDITION", 433, 434),
    ("LOWEST_RATING", 434, 435),
    ("DECK_AREA", 435, 445),
)

LEGACY_POSITIONAL_COLUMNS: Tuple[str, ...] = tuple(
    field_name for field_name, _start, _end in NBI_FIXED_POSITIONS
)


def parse_nbi(
    source: Union[bytes, bytearray, BinaryIO],
    *,
    source_agency_context: str,
    encoding: str,
    input_format: Optional[NBIFormat] = None,
    issue_policy: IssuePolicy = IssuePolicy.REJECT,
    limits: ParserLimits = ParserLimits(),
    csv_dialect: Optional[CsvDialect] = None,
    positional_columns: Sequence[str] = LEGACY_POSITIONAL_COLUMNS,
    allow_unknown_named_columns: bool = False,
) -> ParseResult:
    """Parse legacy NBI bytes using explicit provenance and safety settings.

    ``input_format=None`` enables conservative detection.  Detection succeeds
    only when exactly one format is supported by the observed sample; it never
    falls back to positional CSV merely because another format did not match.
    """

    agency = source_agency_context.strip() if isinstance(source_agency_context, str) else ""
    if not agency:
        raise ValueError("source_agency_context must be a non-empty string")
    limits.validate()
    normalized_encoding = _normalize_encoding(encoding)
    policy = IssuePolicy(issue_policy)
    selected_format = NBIFormat(input_format) if input_format is not None else None

    raw_input = _read_bounded(source, limits.max_bytes)
    source_digest = hashlib.sha256(raw_input).hexdigest()
    payload, initial_offset = _apply_bom_policy(raw_input, normalized_encoding)
    raw_lines = _split_raw_lines(payload, initial_offset, limits.max_line_bytes)
    if not any(_strip_line_ending(line.data).strip() for line in raw_lines):
        raise NBIParseError(
            ParseIssue(
                code="empty_input",
                message="NBI input does not contain a record",
                severity=IssueSeverity.ERROR,
                byte_offset=initial_offset,
            )
        )

    columns = _validate_positional_columns(positional_columns)
    if selected_format is None:
        selected_format = detect_nbi_format(
            raw_lines,
            encoding=normalized_encoding,
            csv_dialect=csv_dialect,
            positional_columns=columns,
        )

    if selected_format is NBIFormat.FHWA_FIXED_445:
        records, quarantined, issues, records_seen = _parse_fixed_records(
            raw_lines,
            agency=agency,
            encoding=normalized_encoding,
            issue_policy=policy,
            max_records=limits.max_records,
        )
        csv_header: Tuple[str, ...] = ()
    else:
        if csv_dialect is None:
            raise NBIParseError(
                ParseIssue(
                    code="csv_dialect_required",
                    message="CSV input requires an explicit CsvDialect",
                    severity=IssueSeverity.ERROR,
                )
            )
        records, quarantined, issues, records_seen, csv_header = _parse_csv_records(
            raw_lines,
            input_format=selected_format,
            agency=agency,
            encoding=normalized_encoding,
            issue_policy=policy,
            max_records=limits.max_records,
            dialect=csv_dialect,
            positional_columns=columns,
            allow_unknown_named_columns=allow_unknown_named_columns,
        )

    grouped_records, records, duplicate_quarantine, group_issues = _group_records(
        records, policy
    )
    quarantined.extend(duplicate_quarantine)
    issues.extend(group_issues)

    return ParseResult(
        input_format=selected_format,
        encoding=normalized_encoding,
        source_agency_context=agency,
        source_sha256=source_digest,
        bytes_read=len(raw_input),
        records_seen=records_seen,
        records=tuple(records),
        quarantined_records=tuple(quarantined),
        bridges=MappingProxyType(grouped_records),
        issues=tuple(issues),
        csv_header=csv_header,
    )


def detect_nbi_format(
    raw_lines: Sequence[_RawLine],
    *,
    encoding: str,
    csv_dialect: Optional[CsvDialect],
    positional_columns: Sequence[str],
) -> NBIFormat:
    """Conservatively classify a small sample or raise ``ambiguous_format``."""

    sample = [
        _strip_line_ending(line.data)
        for line in raw_lines
        if _strip_line_ending(line.data).strip()
    ][:5]
    candidates: List[NBIFormat] = []

    fixed_candidate = bool(sample) and all(
        len(record) == FIXED_RECORD_BYTES
        and record.isascii()
        and record[0:3].isdigit()
        and bool(record[3:18].strip())
        and record[18:19] in {b"1", b"2", b"A"}
        for record in sample
    )
    if fixed_candidate:
        candidates.append(NBIFormat.FHWA_FIXED_445)

    if csv_dialect is not None and sample:
        try:
            first_text = sample[0].decode(encoding, errors="strict")
            first_row = next(csv.reader([first_text], **csv_dialect.reader_kwargs()))
        except (UnicodeDecodeError, csv.Error, StopIteration, ValueError):
            first_row = []
        normalized_header = tuple(cell.strip() for cell in first_row)
        if _REQUIRED_CSV_HEADERS.issubset(normalized_header):
            candidates.append(NBIFormat.NAMED_CSV)
        elif (
            len(first_row) == len(positional_columns)
            and csv_dialect.delimiter in first_text
        ):
            candidates.append(NBIFormat.LEGACY_POSITIONAL_CSV)

    if len(candidates) != 1:
        candidate_text = ", ".join(candidate.value for candidate in candidates) or "none"
        raise NBIParseError(
            ParseIssue(
                code="ambiguous_format",
                message=(
                    "NBI format could not be selected uniquely "
                    f"(candidates: {candidate_text}); specify input_format explicitly"
                ),
                severity=IssueSeverity.ERROR,
                line_number=raw_lines[0].line_number if raw_lines else None,
                byte_offset=raw_lines[0].byte_offset if raw_lines else None,
            )
        )
    return candidates[0]


def to_legacy_bridge_dict(
    result: ParseResult,
) -> Dict[str, Dict[str, Any]]:
    """Explicit adapter for callers that still require the old result shape.

    The legacy mapping keys only by trimmed Item 8.  This adapter refuses to
    merge records when that key occurs in multiple state/agency contexts.
    """

    output: Dict[str, Dict[str, Any]] = {}
    owners: Dict[str, LegacyBridgeKey] = {}
    for key, bridge in iter_legacy_bridge_records(result):
        legacy_key = key.raw_structure_number.strip()
        previous = owners.get(legacy_key)
        if previous is not None and previous != key:
            raise LegacyAdapterCollisionError(
                f"legacy key {legacy_key!r} belongs to both {previous} and {key}"
            )
        owners[legacy_key] = key
        output[legacy_key] = bridge
    return output


def iter_legacy_bridge_records(
    result: ParseResult,
) -> Tuple[Tuple[LegacyBridgeKey, Dict[str, Any]], ...]:
    """Project records for the legacy mapper without discarding composite keys."""

    def legacy_fields(record: Optional[SourceRecord]) -> Optional[Dict[str, Optional[str]]]:
        if record is None:
            return None
        projected = dict(record.fields)
        # The established mapper expects the old parser's two-character State
        # FIPS value. Keep the full SSF Item 1 only in the evidentiary record.
        projected["STATE_CODE_001"] = record.bridge_key.normalized_state
        return projected

    return tuple(
        (
            key,
            {
                "over": legacy_fields(group.over),
                "unders": [legacy_fields(record) for record in group.unders],
            },
        )
        for key, group in result.bridges.items()
    )


def parse_nbi_txt_compat(
    path: Path,
    *,
    source_agency_context: str,
    encoding: str,
    input_format: Optional[NBIFormat] = None,
    issue_policy: IssuePolicy = IssuePolicy.REJECT,
    limits: ParserLimits = ParserLimits(),
    csv_dialect: Optional[CsvDialect] = None,
    positional_columns: Sequence[str] = LEGACY_POSITIONAL_COLUMNS,
) -> Dict[str, Dict[str, Any]]:
    """Path-based compatibility adapter; the core parser remains byte-only."""

    with Path(path).open("rb") as stream:
        result = parse_nbi(
            stream,
            source_agency_context=source_agency_context,
            encoding=encoding,
            input_format=input_format,
            issue_policy=issue_policy,
            limits=limits,
            csv_dialect=csv_dialect,
            positional_columns=positional_columns,
        )
    return to_legacy_bridge_dict(result)


def _read_bounded(
    source: Union[bytes, bytearray, BinaryIO], max_bytes: int
) -> bytes:
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
        if len(data) > max_bytes:
            raise NBIParseError(
                ParseIssue(
                    code="input_too_large",
                    message=f"input exceeds the {max_bytes}-byte limit",
                    severity=IssueSeverity.ERROR,
                    byte_offset=max_bytes,
                )
            )
        return data
    read = getattr(source, "read", None)
    if not callable(read):
        raise TypeError("source must be bytes, bytearray, or a binary stream")
    chunks: List[bytes] = []
    total = 0
    while True:
        requested = min(1024 * 1024, max_bytes - total + 1)
        chunk = read(requested)
        if not isinstance(chunk, (bytes, bytearray)):
            raise TypeError("source stream must return bytes")
        if not chunk:
            break
        chunk = bytes(chunk)
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise NBIParseError(
                ParseIssue(
                    code="input_too_large",
                    message=f"input exceeds the {max_bytes}-byte limit",
                    severity=IssueSeverity.ERROR,
                    byte_offset=max_bytes,
                )
            )
    return b"".join(chunks)


def _normalize_encoding(encoding: str) -> str:
    if not isinstance(encoding, str) or not encoding.strip():
        raise ValueError("encoding must be explicitly provided")
    try:
        canonical = codecs.lookup(encoding).name
    except LookupError as exc:
        raise ValueError(f"unknown encoding: {encoding}") from exc
    aliases = {
        "utf-8": "utf-8",
        "utf-8-sig": "utf-8-sig",
        "ascii": "ascii",
        "iso8859-1": "iso8859-1",
        "cp1252": "cp1252",
    }
    canonical = aliases.get(canonical, canonical)
    if canonical not in _SUPPORTED_ENCODINGS:
        raise NBIParseError(
            ParseIssue(
                code="unsupported_multibyte_encoding",
                message=(
                    f"encoding {encoding!r} is not supported because deterministic "
                    "byte offsets require an ASCII-compatible encoding"
                ),
                severity=IssueSeverity.ERROR,
            )
        )
    return canonical


def _apply_bom_policy(data: bytes, encoding: str) -> Tuple[bytes, int]:
    if data.startswith(UTF8_BOM):
        if encoding != "utf-8-sig":
            raise NBIParseError(
                ParseIssue(
                    code="unexpected_bom",
                    message="UTF-8 BOM requires encoding='utf-8-sig'",
                    severity=IssueSeverity.ERROR,
                    byte_offset=0,
                )
            )
        return data[len(UTF8_BOM) :], len(UTF8_BOM)
    return data, 0


def _split_raw_lines(
    data: bytes, initial_offset: int, max_line_bytes: int
) -> Tuple[_RawLine, ...]:
    lines: List[_RawLine] = []
    start = 0
    line_number = 1
    index = 0

    def add(raw_line: bytes, offset: int, number: int) -> None:
        body = _strip_line_ending(raw_line)
        if len(body) > max_line_bytes:
            raise NBIParseError(
                ParseIssue(
                    code="line_too_large",
                    message=f"line exceeds the {max_line_bytes}-byte limit",
                    severity=IssueSeverity.ERROR,
                    line_number=number,
                    byte_offset=offset,
                    raw_sha256=hashlib.sha256(body).hexdigest(),
                )
            )
        lines.append(_RawLine(raw_line, number, offset))

    while index < len(data):
        if data[index] == 10:  # LF
            end = index + 1
        elif data[index] == 13:  # CR or CRLF
            end = index + 2 if index + 1 < len(data) and data[index + 1] == 10 else index + 1
        else:
            index += 1
            continue
        add(data[start:end], initial_offset + start, line_number)
        start = end
        line_number += 1
        index = end

    if start < len(data):
        add(data[start:], initial_offset + start, line_number)
    return tuple(lines)


def _strip_line_ending(raw_line: bytes) -> bytes:
    if raw_line.endswith(b"\r\n"):
        return raw_line[:-2]
    if raw_line.endswith((b"\n", b"\r")):
        return raw_line[:-1]
    return raw_line


def _validate_positional_columns(columns: Sequence[str]) -> Tuple[str, ...]:
    result = tuple(columns)
    if not result or any(not isinstance(value, str) or not value for value in result):
        raise ValueError("positional_columns must contain non-empty string names")
    folded = [value.casefold() for value in result]
    if len(folded) != len(set(folded)):
        raise ValueError("positional_columns may not contain duplicate names")
    if not _REQUIRED_CSV_HEADERS.issubset(result):
        raise ValueError(
            "positional_columns must include STATE_CODE_001, "
            "STRUCTURE_NUMBER_008, and RECORD_TYPE_005A"
        )
    return result


def _parse_fixed_records(
    raw_lines: Sequence[_RawLine],
    *,
    agency: str,
    encoding: str,
    issue_policy: IssuePolicy,
    max_records: int,
) -> Tuple[List[SourceRecord], List[QuarantinedRecord], List[ParseIssue], int]:
    records: List[SourceRecord] = []
    quarantined: List[QuarantinedRecord] = []
    issues: List[ParseIssue] = []
    records_seen = 0

    for raw_line in raw_lines:
        raw = _strip_line_ending(raw_line.data)
        if not raw.strip():
            issues.append(
                _issue_for_raw(
                    "blank_record",
                    "blank physical line was skipped",
                    IssueSeverity.WARNING,
                    raw_line,
                    raw,
                )
            )
            continue
        records_seen += 1
        _enforce_record_limit(records_seen, max_records, raw_line, raw)
        issue_codes: List[str] = []
        parse_bytes = raw

        if len(raw) != FIXED_RECORD_BYTES:
            issue = _issue_for_raw(
                "fixed_record_length",
                (
                    f"official fixed-width record is {len(raw)} bytes; "
                    f"exactly {FIXED_RECORD_BYTES} bytes are required excluding CR/LF"
                ),
                IssueSeverity.ERROR,
                raw_line,
                raw,
            )
            issues.append(issue)
            if issue_policy is IssuePolicy.REJECT:
                raise NBIParseError(issue)
            if issue_policy is IssuePolicy.QUARANTINE or len(raw) < 19:
                quarantined.append(_quarantine(raw_line, raw, (issue.code,)))
                continue
            # Explicit accept_with_issue is the only mode that may expose a
            # non-conformant record.  Original bytes and the issue remain intact.
            parse_bytes = raw[:FIXED_RECORD_BYTES].ljust(FIXED_RECORD_BYTES, b" ")
            issue_codes.append(issue.code)

        try:
            raw_item_001 = parse_bytes[0:3].decode(encoding, errors="strict")
            raw_item_008 = parse_bytes[3:18].decode(encoding, errors="strict")
            fields = {
                name: _clean_cell(parse_bytes[start:end].decode(encoding, errors="strict"))
                for name, start, end in NBI_FIXED_POSITIONS
            }
        except UnicodeDecodeError as exc:
            issue = _issue_for_raw(
                "decode_error",
                f"record cannot be decoded strictly as {encoding}: {exc}",
                IssueSeverity.ERROR,
                raw_line,
                raw,
            )
            issues.append(issue)
            if issue_policy is IssuePolicy.REJECT:
                raise NBIParseError(issue) from exc
            quarantined.append(
                _quarantine(raw_line, raw, tuple(issue_codes + [issue.code]))
            )
            continue

        built = _build_source_record(
            fields=fields,
            raw_item_001=raw_item_001,
            raw_item_008=raw_item_008,
            raw_record_type=parse_bytes[18:19].decode("ascii"),
            agency=agency,
            source_order=records_seen,
            raw_line=raw_line,
            raw=raw,
            issue_policy=issue_policy,
            issues=issues,
            quarantined=quarantined,
            issue_codes=issue_codes,
        )
        if built is not None:
            records.append(built)

    return records, quarantined, issues, records_seen


def _parse_csv_records(
    raw_lines: Sequence[_RawLine],
    *,
    input_format: NBIFormat,
    agency: str,
    encoding: str,
    issue_policy: IssuePolicy,
    max_records: int,
    dialect: CsvDialect,
    positional_columns: Tuple[str, ...],
    allow_unknown_named_columns: bool,
) -> Tuple[
    List[SourceRecord],
    List[QuarantinedRecord],
    List[ParseIssue],
    int,
    Tuple[str, ...],
]:
    decoded_lines: List[str] = []
    for raw_line in raw_lines:
        try:
            decoded_lines.append(raw_line.data.decode(encoding, errors="strict"))
        except UnicodeDecodeError as exc:
            raise NBIParseError(
                _issue_for_raw(
                    "decode_error",
                    f"CSV cannot be decoded strictly as {encoding}: {exc}",
                    IssueSeverity.ERROR,
                    raw_line,
                    _strip_line_ending(raw_line.data),
                )
            ) from exc

    reader = csv.reader(decoded_lines, **dialect.reader_kwargs())
    issues: List[ParseIssue] = []
    quarantined: List[QuarantinedRecord] = []
    records: List[SourceRecord] = []
    records_seen = 0
    header: Tuple[str, ...] = ()
    physical_cursor = 0

    if input_format is NBIFormat.NAMED_CSV:
        try:
            header_row = next(reader)
        except StopIteration as exc:
            raise NBIParseError(
                ParseIssue(
                    code="missing_csv_header",
                    message="named CSV is missing its header row",
                    severity=IssueSeverity.ERROR,
                )
            ) from exc
        except csv.Error as exc:
            raise NBIParseError(
                ParseIssue(
                    code="malformed_csv",
                    message=f"CSV header is malformed: {exc}",
                    severity=IssueSeverity.ERROR,
                    line_number=1,
                    byte_offset=raw_lines[0].byte_offset if raw_lines else 0,
                )
            ) from exc
        physical_cursor = reader.line_num
        header = tuple(cell.strip() for cell in header_row)
        if not header or any(not name for name in header):
            raise NBIParseError(
                ParseIssue(
                    code="empty_csv_header",
                    message="named CSV contains an empty header name",
                    severity=IssueSeverity.ERROR,
                    line_number=1,
                    byte_offset=raw_lines[0].byte_offset if raw_lines else 0,
                )
            )
        folded = [name.casefold() for name in header]
        duplicates = sorted(
            {header[index] for index, name in enumerate(folded) if folded.count(name) > 1}
        )
        if duplicates:
            raise NBIParseError(
                ParseIssue(
                    code="duplicate_csv_headers",
                    message=f"duplicate CSV headers: {', '.join(duplicates)}",
                    severity=IssueSeverity.ERROR,
                    line_number=1,
                    byte_offset=raw_lines[0].byte_offset if raw_lines else 0,
                )
            )
        missing = sorted(_REQUIRED_CSV_HEADERS - set(header))
        if missing:
            raise NBIParseError(
                ParseIssue(
                    code="missing_required_headers",
                    message=f"named CSV is missing required headers: {', '.join(missing)}",
                    severity=IssueSeverity.ERROR,
                    line_number=1,
                    byte_offset=raw_lines[0].byte_offset if raw_lines else 0,
                )
            )
        unknown = tuple(name for name in header if name not in LEGACY_POSITIONAL_COLUMNS)
        if unknown:
            issue = ParseIssue(
                code="unknown_header_columns",
                message=f"named CSV contains unknown columns: {', '.join(unknown)}",
                severity=(
                    IssueSeverity.WARNING
                    if allow_unknown_named_columns
                    else IssueSeverity.ERROR
                ),
                line_number=1,
                byte_offset=raw_lines[0].byte_offset if raw_lines else 0,
            )
            issues.append(issue)
            if not allow_unknown_named_columns:
                raise NBIParseError(issue)
        columns = header
    else:
        columns = positional_columns

    while True:
        start_physical = physical_cursor
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            line_index = min(start_physical, max(len(raw_lines) - 1, 0))
            raw_line = raw_lines[line_index]
            raw = _strip_line_ending(raw_line.data)
            issue = _issue_for_raw(
                "malformed_csv",
                f"CSV row is malformed: {exc}",
                IssueSeverity.ERROR,
                raw_line,
                raw,
            )
            if issue_policy is IssuePolicy.REJECT:
                raise NBIParseError(issue) from exc
            issues.append(issue)
            quarantined.append(_quarantine(raw_line, raw, (issue.code,)))
            break

        physical_cursor = reader.line_num
        spanned_lines = raw_lines[start_physical:physical_cursor]
        if not spanned_lines:
            continue
        raw_line = spanned_lines[0]
        raw = _strip_line_ending(b"".join(line.data for line in spanned_lines))
        if not row or not any(cell.strip() for cell in row):
            issues.append(
                _issue_for_raw(
                    "blank_record",
                    "blank CSV record was skipped",
                    IssueSeverity.WARNING,
                    raw_line,
                    raw,
                )
            )
            continue

        records_seen += 1
        _enforce_record_limit(records_seen, max_records, raw_line, raw)
        issue_codes: List[str] = []
        conformant = True
        extras: Tuple[str, ...] = ()
        expected_width = len(columns)
        if len(row) != expected_width:
            code = "csv_row_too_short" if len(row) < expected_width else "csv_row_too_long"
            issue = _issue_for_raw(
                code,
                f"CSV row has {len(row)} columns; expected exactly {expected_width}",
                IssueSeverity.ERROR,
                raw_line,
                raw,
            )
            issues.append(issue)
            if issue_policy is IssuePolicy.REJECT:
                raise NBIParseError(issue)
            if issue_policy is IssuePolicy.QUARANTINE:
                quarantined.append(_quarantine(raw_line, raw, (issue.code,)))
                continue
            conformant = False
            issue_codes.append(issue.code)
            if len(row) > expected_width:
                extras = tuple(row[expected_width:])
            row = row[:expected_width] + [""] * max(0, expected_width - len(row))

        raw_values = dict(zip(columns, row))
        fields = {name: _clean_cell(value) for name, value in raw_values.items()}
        raw_item_001 = raw_values.get("STATE_CODE_001", "")
        raw_item_008 = raw_values.get("STRUCTURE_NUMBER_008", "")
        raw_record_type = raw_values.get("RECORD_TYPE_005A", "")
        built = _build_source_record(
            fields=fields,
            raw_item_001=raw_item_001,
            raw_item_008=raw_item_008,
            raw_record_type=raw_record_type,
            agency=agency,
            source_order=records_seen,
            raw_line=raw_line,
            raw=raw,
            issue_policy=issue_policy,
            issues=issues,
            quarantined=quarantined,
            issue_codes=issue_codes,
            conformant=conformant,
            csv_extra_values=extras,
        )
        if built is not None:
            records.append(built)

    return records, quarantined, issues, records_seen, header


def _build_source_record(
    *,
    fields: Mapping[str, Optional[str]],
    raw_item_001: str,
    raw_item_008: str,
    raw_record_type: str,
    agency: str,
    source_order: int,
    raw_line: _RawLine,
    raw: bytes,
    issue_policy: IssuePolicy,
    issues: List[ParseIssue],
    quarantined: List[QuarantinedRecord],
    issue_codes: List[str],
    conformant: bool = True,
    csv_extra_values: Tuple[str, ...] = (),
) -> Optional[SourceRecord]:
    raw_digest = hashlib.sha256(raw).hexdigest()
    source_record_id = _source_record_id(raw_line, raw)
    try:
        normalized_state = _normalize_state(raw_item_001)
    except ValueError as exc:
        issue = ParseIssue(
            code="invalid_state_code",
            message=str(exc),
            severity=IssueSeverity.ERROR,
            line_number=raw_line.line_number,
            byte_offset=raw_line.byte_offset,
            raw_sha256=raw_digest,
            source_record_id=source_record_id,
        )
        issues.append(issue)
        if issue_policy is IssuePolicy.REJECT:
            raise NBIParseError(issue) from exc
        quarantined.append(
            _quarantine(raw_line, raw, tuple(issue_codes + [issue.code]))
        )
        return None

    if not raw_item_008.strip():
        issue = ParseIssue(
            code="missing_structure_number",
            message="Item 8 structure number is blank",
            severity=IssueSeverity.ERROR,
            line_number=raw_line.line_number,
            byte_offset=raw_line.byte_offset,
            raw_sha256=raw_digest,
            source_record_id=source_record_id,
        )
        issues.append(issue)
        if issue_policy is IssuePolicy.REJECT:
            raise NBIParseError(issue)
        quarantined.append(
            _quarantine(raw_line, raw, tuple(issue_codes + [issue.code]))
        )
        return None

    key = LegacyBridgeKey(
        source_agency_context=agency,
        normalized_state=normalized_state,
        raw_structure_number=raw_item_008,
    )
    try:
        record_type = RecordType(raw_record_type.strip().upper())
    except ValueError as exc:
        issue = ParseIssue(
            code="unknown_record_type",
            message=(
                f"record type {raw_record_type!r} is unsupported; only 1, 2, and A "
                "are recognized"
            ),
            severity=IssueSeverity.ERROR,
            line_number=raw_line.line_number,
            byte_offset=raw_line.byte_offset,
            raw_sha256=raw_digest,
            source_record_id=source_record_id,
            bridge_key=key,
        )
        issues.append(issue)
        if issue_policy is IssuePolicy.REJECT:
            raise NBIParseError(issue) from exc
        quarantined.append(
            _quarantine(
                raw_line,
                raw,
                tuple(issue_codes + [issue.code]),
                bridge_key=key,
            )
        )
        return None

    return SourceRecord(
        source_order=source_order,
        line_number=raw_line.line_number,
        byte_offset=raw_line.byte_offset,
        raw_sha256=raw_digest,
        source_record_id=source_record_id,
        record_type=record_type,
        bridge_key=key,
        raw_item_001=raw_item_001,
        raw_item_008=raw_item_008,
        fields=MappingProxyType(dict(fields)),
        conformant=conformant and not issue_codes,
        issue_codes=tuple(issue_codes),
        csv_extra_values=csv_extra_values,
    )


def _group_records(
    source_records: Sequence[SourceRecord], issue_policy: IssuePolicy
) -> Tuple[
    Dict[LegacyBridgeKey, LegacyBridgeRecords],
    List[SourceRecord],
    List[QuarantinedRecord],
    List[ParseIssue],
]:
    by_key: Dict[LegacyBridgeKey, List[SourceRecord]] = defaultdict(list)
    for record in source_records:
        by_key[record.bridge_key].append(record)

    groups: Dict[LegacyBridgeKey, LegacyBridgeRecords] = {}
    accepted: List[SourceRecord] = []
    quarantined: List[QuarantinedRecord] = []
    issues: List[ParseIssue] = []

    for key in sorted(by_key):
        records = sorted(by_key[key], key=lambda record: record.source_order)
        overs = [record for record in records if record.record_type is RecordType.OVER]
        unders = [record for record in records if record.record_type is not RecordType.OVER]
        selected_over = overs[0] if overs else None
        duplicate_overs: Tuple[SourceRecord, ...] = ()

        if len(overs) > 1:
            first = overs[0]
            for duplicate in overs[1:]:
                issue = ParseIssue(
                    code="duplicate_over_record",
                    message=(
                        "duplicate Type 1 record detected; the earliest source record "
                        f"at line {first.line_number} remains selected"
                    ),
                    severity=IssueSeverity.ERROR,
                    line_number=duplicate.line_number,
                    byte_offset=duplicate.byte_offset,
                    raw_sha256=duplicate.raw_sha256,
                    source_record_id=duplicate.source_record_id,
                    bridge_key=key,
                )
                issues.append(issue)
                if issue_policy is IssuePolicy.REJECT:
                    raise NBIParseError(issue)
                if issue_policy is IssuePolicy.QUARANTINE:
                    quarantined.append(
                        QuarantinedRecord(
                            line_number=duplicate.line_number,
                            byte_offset=duplicate.byte_offset,
                            raw_sha256=duplicate.raw_sha256,
                            source_record_id=duplicate.source_record_id,
                            issue_codes=(issue.code,),
                            bridge_key=key,
                        )
                    )
                else:
                    duplicate_overs += (
                        replace(
                            duplicate,
                            conformant=False,
                            issue_codes=duplicate.issue_codes + (issue.code,),
                        ),
                    )

        type_twos = [
            record for record in unders if record.record_type is RecordType.UNDER
        ]
        type_as = [
            record
            for record in unders
            if record.record_type is RecordType.UNDER_ALTERNATE
        ]
        primary_under: Optional[SourceRecord] = None
        if type_twos:
            primary_under = type_twos[0]
            if len(type_twos) > 1:
                issues.append(
                    ParseIssue(
                        code="multiple_type2_under_records",
                        message=(
                            "multiple Type 2 under records require review; the earliest "
                            "Type 2 is selected deterministically"
                        ),
                        severity=IssueSeverity.REVIEW,
                        line_number=type_twos[1].line_number,
                        byte_offset=type_twos[1].byte_offset,
                        raw_sha256=type_twos[1].raw_sha256,
                        source_record_id=type_twos[1].source_record_id,
                        bridge_key=key,
                    )
                )
            if type_as and type_as[0].source_order < primary_under.source_order:
                issues.append(
                    ParseIssue(
                        code="type2_preferred_over_earlier_type_a",
                        message=(
                            "Type 2 is selected as primary under record even though a "
                            "Type A record occurs earlier; review feature assignment"
                        ),
                        severity=IssueSeverity.REVIEW,
                        line_number=primary_under.line_number,
                        byte_offset=primary_under.byte_offset,
                        raw_sha256=primary_under.raw_sha256,
                        source_record_id=primary_under.source_record_id,
                        bridge_key=key,
                    )
                )
        elif type_as:
            primary_under = type_as[0]
            issues.append(
                ParseIssue(
                    code="type_a_primary_under_fallback",
                    message=(
                        "no Type 2 under record exists; earliest Type A is selected and "
                        "requires review"
                    ),
                    severity=IssueSeverity.REVIEW,
                    line_number=primary_under.line_number,
                    byte_offset=primary_under.byte_offset,
                    raw_sha256=primary_under.raw_sha256,
                    source_record_id=primary_under.source_record_id,
                    bridge_key=key,
                )
            )
        secondary_unders = tuple(
            record for record in unders if record is not primary_under
        )

        groups[key] = LegacyBridgeRecords(
            key=key,
            over=selected_over,
            duplicate_overs=duplicate_overs,
            unders=tuple(unders),
            primary_under=primary_under,
            secondary_unders=secondary_unders,
        )

        if selected_over is not None:
            accepted.append(selected_over)
        if issue_policy is IssuePolicy.ACCEPT_WITH_ISSUE:
            accepted.extend(duplicate_overs)
        accepted.extend(unders)

    accepted.sort(key=lambda record: record.source_order)
    return groups, accepted, quarantined, issues


def _normalize_state(raw_state: str) -> str:
    value = raw_state.strip()
    if len(value) != 3 or not value.isdigit():
        raise ValueError(
            f"legacy Item 1 {raw_state!r} must contain exactly three decimal digits"
        )
    # Legacy Item 1 is SSF: two-digit State FIPS plus a one-digit FHWA region.
    # BL01 transition drops the region digit and removes any leading zero.
    state_fips = int(value[:2])
    if state_fips < 1 or state_fips > 99:
        raise ValueError(
            f"legacy Item 1 {raw_state!r} has an invalid two-digit State FIPS"
        )
    return f"{state_fips:02d}"


def _clean_cell(value: str) -> Optional[str]:
    cleaned = value.strip()
    return cleaned if cleaned else None


def _issue_for_raw(
    code: str,
    message: str,
    severity: IssueSeverity,
    raw_line: _RawLine,
    raw: bytes,
) -> ParseIssue:
    return ParseIssue(
        code=code,
        message=message,
        severity=severity,
        line_number=raw_line.line_number,
        byte_offset=raw_line.byte_offset,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        source_record_id=_source_record_id(raw_line, raw),
    )


def _quarantine(
    raw_line: _RawLine,
    raw: bytes,
    issue_codes: Tuple[str, ...],
    *,
    bridge_key: Optional[LegacyBridgeKey] = None,
) -> QuarantinedRecord:
    return QuarantinedRecord(
        line_number=raw_line.line_number,
        byte_offset=raw_line.byte_offset,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        source_record_id=_source_record_id(raw_line, raw),
        issue_codes=issue_codes,
        bridge_key=bridge_key,
    )


def _source_record_id(raw_line: _RawLine, raw: bytes) -> str:
    """Return a stable ID for one physical record in identical source bytes."""

    raw_digest = hashlib.sha256(raw).hexdigest()
    identity = f"nbi-record-v1\x00{raw_line.byte_offset}\x00{raw_digest}".encode("ascii")
    return hashlib.sha256(identity).hexdigest()


def _enforce_record_limit(
    records_seen: int, max_records: int, raw_line: _RawLine, raw: bytes
) -> None:
    if records_seen > max_records:
        raise NBIParseError(
            _issue_for_raw(
                "record_limit_exceeded",
                f"input exceeds the {max_records}-record limit",
                IssueSeverity.ERROR,
                raw_line,
                raw,
            )
        )
