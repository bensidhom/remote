"""Bounded, provenance-preserving parser for legacy FHWA element XML.

This module intentionally lives beside the legacy converter.  It has no import
from, or side effect on, the production parser until a later migration wires it
in.  XML and all values read from it are treated as untrusted input.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import (
    BinaryIO,
    DefaultDict,
    Dict,
    Iterable,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    TextIO,
    Tuple,
    Union,
)


ElementXmlSource = Union[
    bytes,
    bytearray,
    memoryview,
    str,
    os.PathLike,
    BinaryIO,
    TextIO,
]


class FieldPresence(str, Enum):
    """How a scalar XML field appeared in the source record."""

    MISSING = "missing"
    EMPTY = "empty"
    VALUE = "value"


class IssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"


class MatchStatus(str, Enum):
    MATCHED = "matched"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ElementXmlLimits:
    """Hard resource ceilings applied before and during parsing."""

    # The transport/source limit also bounds a compressed gzip input.
    max_source_bytes: int = 32 * 1024 * 1024
    max_expanded_bytes: int = 64 * 1024 * 1024
    max_nodes: int = 500_000
    max_depth: int = 64
    max_text_chars: int = 16 * 1024 * 1024
    max_field_text_chars: int = 64 * 1024
    max_records: int = 250_000
    max_issues: int = 20_000
    read_chunk_bytes: int = 64 * 1024
    quantity_tolerance: Decimal = Decimal("0.000001")

    def __post_init__(self) -> None:
        integer_limits = (
            self.max_source_bytes,
            self.max_expanded_bytes,
            self.max_nodes,
            self.max_depth,
            self.max_text_chars,
            self.max_field_text_chars,
            self.max_records,
            self.max_issues,
            self.read_chunk_bytes,
        )
        if any(value <= 0 for value in integer_limits):
            raise ValueError("all element XML limits must be positive")
        if self.quantity_tolerance < 0 or not self.quantity_tolerance.is_finite():
            raise ValueError("quantity_tolerance must be finite and nonnegative")


@dataclass(frozen=True)
class ElementParseIssue:
    code: str
    severity: IssueSeverity
    message: str
    record_index: Optional[int] = None
    record_path: Optional[str] = None
    field_name: Optional[str] = None
    raw_text: Optional[str] = None
    line_number: Optional[int] = None
    column_number: Optional[int] = None


@dataclass(frozen=True)
class ElementField:
    """One canonical FHWAED field with raw and typed representations."""

    name: str
    xml_name: Optional[str]
    raw_text: Optional[str]
    presence: FieldPresence
    value: Optional[Union[str, Decimal]]

    @property
    def is_missing(self) -> bool:
        return self.presence is FieldPresence.MISSING

    @property
    def is_empty(self) -> bool:
        return self.presence is FieldPresence.EMPTY


@dataclass(frozen=True)
class BridgeIdentity:
    """Source bridge identity; raw identifier spelling is never discarded."""

    structure_number_raw: Optional[str]
    state_raw: Optional[str] = None
    agency_raw: Optional[str] = None

    @property
    def structure_number(self) -> Optional[str]:
        return _clean_identity(self.structure_number_raw)

    @property
    def state(self) -> Optional[str]:
        return _normalize_state_identity(self.state_raw)

    @property
    def agency(self) -> Optional[str]:
        return _normalize_code(self.agency_raw)


@dataclass(frozen=True)
class ElementRecord:
    record_index: int
    record_path: str
    line_number: Optional[int]
    namespace_uri: Optional[str]
    bridge_identity: BridgeIdentity
    fields: Mapping[str, ElementField]

    @property
    def element_number(self) -> Optional[Decimal]:
        value = self.fields["EN"].value
        return value if isinstance(value, Decimal) else None

    @property
    def parent_element_number(self) -> Optional[Decimal]:
        value = self.fields["EPN"].value
        return value if isinstance(value, Decimal) else None

    def to_snbi_fields(self) -> Dict[str, Optional[str]]:
        """Return legacy target names without manufacturing absent zeroes."""

        mapping = {
            "BE01": "EN",
            "BE02": "EPN",
            "BE03": "TOTALQTY",
            "BCS01": "CS1",
            "BCS02": "CS2",
            "BCS03": "CS3",
            "BCS04": "CS4",
        }
        result: Dict[str, Optional[str]] = {}
        for target, source in mapping.items():
            field_value = self.fields[source]
            result[target] = (
                None
                if field_value.presence is FieldPresence.MISSING
                else field_value.raw_text
            )
        return result


@dataclass(frozen=True)
class ElementParseResult:
    element_file_supplied: bool
    element_file_parsed: bool
    source_name: Optional[str]
    source_bytes: int
    expanded_bytes: int
    records_seen: int
    element_records: int
    records: Tuple[ElementRecord, ...]
    issues: Tuple[ElementParseIssue, ...]

    @property
    def ok(self) -> bool:
        return self.element_file_parsed and not any(
            issue.severity in {IssueSeverity.ERROR, IssueSeverity.FATAL}
            for issue in self.issues
        )


@dataclass(frozen=True)
class BridgeCandidate:
    """A bridge available to receive parsed element records."""

    candidate_id: str
    structure_number_raw: str
    state_raw: Optional[str] = None
    agency_raw: Optional[str] = None


@dataclass(frozen=True)
class ElementRecordMatch:
    record_index: int
    record_path: str
    status: MatchStatus
    candidate_ids: Tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class ElementJoinResult:
    element_file_supplied: bool
    element_file_parsed: bool
    element_records: int
    matched: int
    unmatched: int
    ambiguous: int
    matches: Tuple[ElementRecordMatch, ...]
    records_by_candidate: Mapping[str, Tuple[ElementRecord, ...]]
    issues: Tuple[ElementParseIssue, ...]


_CANONICAL_FIELDS: Tuple[str, ...] = (
    "STRUCNUM",
    "STATE",
    "AGENCY",
    "EN",
    "EPN",
    "TOTALQTY",
    "CS1",
    "CS2",
    "CS3",
    "CS4",
)

_FIELD_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "STRUCNUM": "STRUCNUM",
        "STRUCTURE_NUMBER": "STRUCNUM",
        "STRUCTURE_NUMBER_008": "STRUCNUM",
        "STATE": "STATE",
        "STATE_CODE": "STATE",
        "STATECODE": "STATE",
        "STATE_FIPS": "STATE",
        "STATE_FIPS_CODE": "STATE",
        "STATE_CODE_001": "STATE",
        "AGENCY": "AGENCY",
        "AGENCY_CODE": "AGENCY",
        "AGENCYCODE": "AGENCY",
        "AGENCY_ID": "AGENCY",
        "EN": "EN",
        "EPN": "EPN",
        "TOTALQTY": "TOTALQTY",
        "CS1": "CS1",
        "CS2": "CS2",
        "CS3": "CS3",
        "CS4": "CS4",
    }
)

_INTEGER_FIELDS = frozenset({"EN", "EPN"})
_QUANTITY_FIELDS = frozenset({"TOTALQTY", "CS1", "CS2", "CS3", "CS4"})
_NUMERIC_FIELDS = _INTEGER_FIELDS | _QUANTITY_FIELDS
_LOCAL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
LEGACY_ELEMENT_SCHEMA_ID = "fhwa_element_schema_v1_2013_10_03"
LEGACY_ELEMENT_SCHEMA_SHA256 = (
    "193cda71b64933e2d5316e7d193d6f780c9479a875ea3ebcb21dcaee36f0a95f"
)
_LEGACY_ELEMENT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "fhwa"
    / "artifacts"
    / "FHWAElement.Schema.v1.0.2013-10-03.xsd"
)
_LEGACY_ELEMENT_SCHEMA_CLAIMS = frozenset(
    {
        LEGACY_ELEMENT_SCHEMA_ID,
        "140725_a1.xsd",
        "https://www.fhwa.dot.gov/bridge/nbi/140725_a1.xsd",
    }
)
_SCHEMA_INSTANCE_URI = "http://www.w3.org/2001/XMLSchema-instance"
_SECURITY_PATTERNS: Tuple[Tuple[str, re.Pattern[bytes]], ...] = (
    ("doctype_forbidden", re.compile(br"<!\s*DOCTYPE\b", re.IGNORECASE)),
    ("entity_declaration_forbidden", re.compile(br"<!\s*ENTITY\b", re.IGNORECASE)),
    (
        "external_identifier_forbidden",
        re.compile(br"<![^>]{0,1024}\b(?:SYSTEM|PUBLIC)\s+[\"']", re.IGNORECASE),
    ),
    (
        "external_reference_forbidden",
        re.compile(br"<\?\s*xml-stylesheet\b", re.IGNORECASE),
    ),
    (
        "external_reference_forbidden",
        re.compile(br"www\.w3\.org/2001/XInclude", re.IGNORECASE),
    ),
    (
        "entity_reference_forbidden",
        re.compile(
            br"&(?!(?:amp|lt|gt|apos|quot);|#[0-9]+;|#x[0-9A-Fa-f]+;)"
            br"[A-Za-z_:][A-Za-z0-9_.:-]*;"
        ),
    ),
)


class _AbortParse(Exception):
    def __init__(self, issue: ElementParseIssue):
        super().__init__(issue.message)
        self.issue = issue


@dataclass
class _PreparedSource:
    stream: BinaryIO
    source_name: Optional[str]
    source_bytes: int
    expanded_bytes: int

    def close(self) -> None:
        self.stream.close()


@dataclass
class _ParseProgress:
    """Counters retained even when a fatal error discards partial records."""

    source_bytes: int = 0
    expanded_bytes: int = 0
    records_seen: int = 0


@dataclass
class _PathFrame:
    local_name: str
    namespace_uri: Optional[str]
    path: str
    child_counts: DefaultDict[str, int] = field(
        default_factory=lambda: defaultdict(int)
    )


@dataclass
class _RecordBuilder:
    record_index: int
    path: str
    namespace_uri: Optional[str]
    depth: int
    raw_fields: MutableMapping[str, Tuple[str, str]] = field(default_factory=dict)


class _IssueCollector:
    def __init__(self, maximum: int):
        self.maximum = maximum
        self.items: List[ElementParseIssue] = []

    def add(self, issue: ElementParseIssue) -> None:
        if len(self.items) >= self.maximum:
            raise _AbortParse(
                ElementParseIssue(
                    code="issue_limit_exceeded",
                    severity=IssueSeverity.FATAL,
                    message=f"element XML exceeded the issue limit ({self.maximum})",
                    record_index=issue.record_index,
                    record_path=issue.record_path,
                )
            )
        self.items.append(issue)


class _SecurityScanner:
    """Chunk-aware lexical rejection performed before XML parser creation."""

    def __init__(self) -> None:
        self._tail = b""

    def feed(self, chunk: bytes) -> None:
        # Removing NUL bytes also exposes ASCII markup encoded as UTF-16/32.
        normalized = chunk.replace(b"\x00", b"")
        candidate = self._tail + normalized
        for code, pattern in _SECURITY_PATTERNS:
            if pattern.search(candidate):
                raise _AbortParse(
                    ElementParseIssue(
                        code=code,
                        severity=IssueSeverity.FATAL,
                        message=(
                            "DTD, entity, or external XML references are not "
                            "permitted"
                        ),
                    )
                )
        self._tail = candidate[-2048:]


def parse_element_xml(
    source: Optional[ElementXmlSource],
    *,
    limits: Optional[ElementXmlLimits] = None,
    allowed_namespaces: Optional[Iterable[str]] = None,
    claimed_schema: Optional[str] = None,
) -> ElementParseResult:
    """Parse FHWAED records with hard limits and structured diagnostics.

    ``bytes``, paths, and binary/text file-like objects are accepted.  Gzip is
    recognized by magic bytes and bounded separately before expansion.  A fatal
    parse/security/resource failure never returns partially parsed records.
    """

    configured_limits = limits or ElementXmlLimits()
    namespace_allowlist = (
        None if allowed_namespaces is None else frozenset(allowed_namespaces)
    )
    issues = _IssueCollector(configured_limits.max_issues)

    if source is None:
        issue = ElementParseIssue(
            code="element_file_not_supplied",
            severity=IssueSeverity.INFO,
            message="no element XML file was supplied",
        )
        return ElementParseResult(
            element_file_supplied=False,
            element_file_parsed=False,
            source_name=None,
            source_bytes=0,
            expanded_bytes=0,
            records_seen=0,
            element_records=0,
            records=(),
            issues=(issue,),
        )

    prepared: Optional[_PreparedSource] = None
    progress = _ParseProgress()
    source_name = _source_name(source)

    try:
        prepared = _prepare_source(source, configured_limits, progress)
        source_name = prepared.source_name

        document_claim = _declared_root_schema_claim(prepared.stream)
        caller_claim = _canonical_schema_claim(claimed_schema)
        declared_claim = _canonical_schema_claim(document_claim)
        if caller_claim is not None and declared_claim is not None:
            if caller_claim != declared_claim:
                raise _AbortParse(
                    ElementParseIssue(
                        code="xml_schema_claim_mismatch",
                        severity=IssueSeverity.FATAL,
                        message=(
                            "caller and document XML schema claims do not match"
                        ),
                    )
                )

        schema_validation_required = (
            caller_claim is not None or declared_claim is not None
        )
        if schema_validation_required:
            _validate_pinned_legacy_element_schema(prepared.stream)
            issues.add(
                ElementParseIssue(
                    code="xml_schema_validated",
                    severity=IssueSeverity.INFO,
                    message=(
                        "element XML validated against the pinned FHWA Element "
                        "Data Schema Version 1.0"
                    ),
                )
            )

        records, records_seen = _stream_records(
            prepared.stream,
            configured_limits,
            issues,
            namespace_allowlist,
            progress,
            root_schema_claim_allowed=schema_validation_required,
        )
        _validate_record_set(records, configured_limits, issues)

        if not records:
            issues.add(
                ElementParseIssue(
                    code="no_element_records",
                    severity=IssueSeverity.WARNING,
                    message="the XML parsed successfully but contained no FHWAED records",
                )
            )

        return ElementParseResult(
            element_file_supplied=True,
            element_file_parsed=True,
            source_name=source_name,
            source_bytes=progress.source_bytes,
            expanded_bytes=progress.expanded_bytes,
            records_seen=records_seen,
            element_records=len(records),
            records=tuple(records),
            issues=tuple(issues.items),
        )
    except _AbortParse as exc:
        # A resource/security failure is fail-closed: no partial records escape.
        if exc.issue not in issues.items:
            issues.items.append(exc.issue)
        return ElementParseResult(
            element_file_supplied=True,
            element_file_parsed=False,
            source_name=source_name,
            source_bytes=progress.source_bytes,
            expanded_bytes=progress.expanded_bytes,
            records_seen=progress.records_seen,
            element_records=0,
            records=(),
            issues=tuple(issues.items),
        )
    except ET.ParseError as exc:
        line, column = getattr(exc, "position", (None, None))
        issues.items.append(
            ElementParseIssue(
                code="malformed_xml",
                severity=IssueSeverity.FATAL,
                message=f"malformed element XML: {exc}",
                line_number=line,
                column_number=column,
            )
        )
        return ElementParseResult(
            element_file_supplied=True,
            element_file_parsed=False,
            source_name=source_name,
            source_bytes=progress.source_bytes,
            expanded_bytes=progress.expanded_bytes,
            records_seen=progress.records_seen,
            element_records=0,
            records=(),
            issues=tuple(issues.items),
        )
    except (OSError, TypeError, ValueError) as exc:
        issues.items.append(
            ElementParseIssue(
                code="element_source_error",
                severity=IssueSeverity.FATAL,
                message=f"unable to read element XML: {exc}",
            )
        )
        return ElementParseResult(
            element_file_supplied=True,
            element_file_parsed=False,
            source_name=source_name,
            source_bytes=progress.source_bytes,
            expanded_bytes=progress.expanded_bytes,
            records_seen=progress.records_seen,
            element_records=0,
            records=(),
            issues=tuple(issues.items),
        )
    finally:
        if prepared is not None:
            prepared.close()


def parse_elements_xml_v2(
    source: Optional[ElementXmlSource],
    *,
    limits: Optional[ElementXmlLimits] = None,
    allowed_namespaces: Optional[Iterable[str]] = None,
    claimed_schema: Optional[str] = None,
) -> ElementParseResult:
    """Explicit migration alias for :func:`parse_element_xml`."""

    return parse_element_xml(
        source,
        limits=limits,
        allowed_namespaces=allowed_namespaces,
        claimed_schema=claimed_schema,
    )


def match_element_records(
    parse_result: ElementParseResult,
    candidates: Sequence[BridgeCandidate],
) -> ElementJoinResult:
    """Match records without guessing across duplicate bridge numbers.

    Numeric bridge-number padding is ignored only for candidate discovery.  If
    that normalization produces multiple candidates, state and then agency are
    used when both sides supply them.  Any remaining multiplicity is reported
    as ambiguous rather than resolved by source order.
    """

    by_number: DefaultDict[str, List[BridgeCandidate]] = defaultdict(list)
    for candidate in candidates:
        normalized = normalize_structure_number(candidate.structure_number_raw)
        if normalized is not None:
            by_number[normalized].append(candidate)

    record_matches: List[ElementRecordMatch] = []
    assignments: DefaultDict[str, List[ElementRecord]] = defaultdict(list)
    issues = list(parse_result.issues)
    matched = unmatched = ambiguous = 0

    if not parse_result.element_file_parsed:
        return ElementJoinResult(
            element_file_supplied=parse_result.element_file_supplied,
            element_file_parsed=False,
            element_records=parse_result.element_records,
            matched=0,
            unmatched=0,
            ambiguous=0,
            matches=(),
            records_by_candidate=MappingProxyType({}),
            issues=tuple(issues),
        )

    for record in parse_result.records:
        identity = record.bridge_identity
        normalized_number = normalize_structure_number(
            identity.structure_number_raw
        )
        possible = list(by_number.get(normalized_number or "", ()))
        reason = "bridge number did not match any candidate"

        if possible and identity.state is not None:
            stateful = [
                candidate
                for candidate in possible
                if _normalize_state_identity(candidate.state_raw) is not None
            ]
            if stateful:
                possible = [
                    candidate
                    for candidate in stateful
                    if _normalize_state_identity(candidate.state_raw) == identity.state
                ]
                reason = "state-aware bridge identity did not match any candidate"

        if possible and identity.agency is not None:
            agency_aware = [
                candidate
                for candidate in possible
                if _normalize_code(candidate.agency_raw) is not None
            ]
            if agency_aware:
                possible = [
                    candidate
                    for candidate in agency_aware
                    if _normalize_code(candidate.agency_raw) == identity.agency
                ]
                reason = "agency-aware bridge identity did not match any candidate"

        candidate_ids = tuple(sorted(candidate.candidate_id for candidate in possible))
        if len(possible) == 1:
            matched += 1
            candidate = possible[0]
            assignments[candidate.candidate_id].append(record)
            record_matches.append(
                ElementRecordMatch(
                    record_index=record.record_index,
                    record_path=record.record_path,
                    status=MatchStatus.MATCHED,
                    candidate_ids=(candidate.candidate_id,),
                    reason="unique composite bridge identity match",
                )
            )
        elif not possible:
            unmatched += 1
            record_matches.append(
                ElementRecordMatch(
                    record_index=record.record_index,
                    record_path=record.record_path,
                    status=MatchStatus.UNMATCHED,
                    candidate_ids=(),
                    reason=reason,
                )
            )
            issues.append(
                ElementParseIssue(
                    code="element_bridge_unmatched",
                    severity=IssueSeverity.ERROR,
                    message=reason,
                    record_index=record.record_index,
                    record_path=record.record_path,
                    field_name="STRUCNUM",
                    raw_text=identity.structure_number_raw,
                )
            )
        else:
            ambiguous += 1
            reason = (
                "bridge number matched multiple candidates; composite identity "
                "was insufficient"
            )
            record_matches.append(
                ElementRecordMatch(
                    record_index=record.record_index,
                    record_path=record.record_path,
                    status=MatchStatus.AMBIGUOUS,
                    candidate_ids=candidate_ids,
                    reason=reason,
                )
            )
            issues.append(
                ElementParseIssue(
                    code="element_bridge_ambiguous",
                    severity=IssueSeverity.ERROR,
                    message=f"{reason}: {', '.join(candidate_ids)}",
                    record_index=record.record_index,
                    record_path=record.record_path,
                    field_name="STRUCNUM",
                    raw_text=identity.structure_number_raw,
                )
            )

    immutable_assignments = MappingProxyType(
        {key: tuple(value) for key, value in assignments.items()}
    )
    return ElementJoinResult(
        element_file_supplied=parse_result.element_file_supplied,
        element_file_parsed=parse_result.element_file_parsed,
        element_records=parse_result.element_records,
        matched=matched,
        unmatched=unmatched,
        ambiguous=ambiguous,
        matches=tuple(record_matches),
        records_by_candidate=immutable_assignments,
        issues=tuple(issues),
    )


def normalize_structure_number(raw: Optional[str]) -> Optional[str]:
    """Normalize only safe formatting differences for candidate discovery."""

    cleaned = _clean_identity(raw)
    if cleaned is None:
        return None
    normalized = cleaned.upper()
    if normalized.isdigit():
        return normalized.lstrip("0") or "0"
    return normalized


def _source_name(source: ElementXmlSource) -> Optional[str]:
    if isinstance(source, (str, os.PathLike)):
        return Path(os.fspath(source)).name or None
    name = getattr(source, "name", None)
    if isinstance(name, (str, os.PathLike)):
        return Path(os.fspath(name)).name or None
    return None


def _open_source(source: ElementXmlSource) -> Tuple[Union[BinaryIO, TextIO], bool]:
    if isinstance(source, bytes):
        return io.BytesIO(source), True
    if isinstance(source, (bytearray, memoryview)):
        return io.BytesIO(bytes(source)), True
    if isinstance(source, (str, os.PathLike)):
        return open(Path(source), "rb"), True
    reader = getattr(source, "read", None)
    if not callable(reader):
        raise TypeError("source must be bytes, a path, or a readable file-like object")
    return source, False


def _prepare_source(
    source: ElementXmlSource,
    limits: ElementXmlLimits,
    progress: _ParseProgress,
) -> _PreparedSource:
    source_stream, should_close = _open_source(source)
    raw_spool = tempfile.SpooledTemporaryFile(
        max_size=min(limits.max_source_bytes, 1024 * 1024), mode="w+b"
    )
    source_bytes = 0
    try:
        try:
            while True:
                chunk = source_stream.read(limits.read_chunk_bytes)
                if chunk in (b"", "", None):
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                elif not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise TypeError("file-like source read() must return bytes or text")
                raw_chunk = bytes(chunk)
                source_bytes += len(raw_chunk)
                progress.source_bytes = source_bytes
                if source_bytes > limits.max_source_bytes:
                    raise _AbortParse(
                        ElementParseIssue(
                            code="source_size_limit_exceeded",
                            severity=IssueSeverity.FATAL,
                            message=(
                                "element XML source exceeded "
                                f"{limits.max_source_bytes} bytes"
                            ),
                        )
                    )
                raw_spool.write(raw_chunk)
        finally:
            if should_close:
                source_stream.close()
    except BaseException:
        raw_spool.close()
        raise

    raw_spool.seek(0)
    magic = raw_spool.read(2)
    raw_spool.seek(0)
    if magic == b"\x1f\x8b":
        expanded_spool = tempfile.SpooledTemporaryFile(
            max_size=min(limits.max_expanded_bytes, 1024 * 1024), mode="w+b"
        )
        scanner = _SecurityScanner()
        expanded_bytes = 0
        try:
            with gzip.GzipFile(fileobj=raw_spool, mode="rb") as gzip_stream:
                while True:
                    chunk = gzip_stream.read(limits.read_chunk_bytes)
                    if not chunk:
                        break
                    expanded_bytes += len(chunk)
                    progress.expanded_bytes = expanded_bytes
                    if expanded_bytes > limits.max_expanded_bytes:
                        raise _AbortParse(
                            ElementParseIssue(
                                code="expanded_size_limit_exceeded",
                                severity=IssueSeverity.FATAL,
                                message=(
                                    "expanded element XML exceeded "
                                    f"{limits.max_expanded_bytes} bytes"
                                ),
                            )
                        )
                    scanner.feed(chunk)
                    expanded_spool.write(chunk)
        except _AbortParse:
            expanded_spool.close()
            raise
        except (OSError, EOFError) as exc:
            expanded_spool.close()
            raise _AbortParse(
                ElementParseIssue(
                    code="invalid_gzip_source",
                    severity=IssueSeverity.FATAL,
                    message=f"invalid gzip-compressed element XML: {exc}",
                )
            ) from exc
        finally:
            raw_spool.close()
        expanded_spool.seek(0)
        return _PreparedSource(
            stream=expanded_spool,
            source_name=_source_name(source),
            source_bytes=source_bytes,
            expanded_bytes=expanded_bytes,
        )

    progress.expanded_bytes = source_bytes
    if source_bytes > limits.max_expanded_bytes:
        raw_spool.close()
        raise _AbortParse(
            ElementParseIssue(
                code="expanded_size_limit_exceeded",
                severity=IssueSeverity.FATAL,
                message=(
                    "element XML exceeded " f"{limits.max_expanded_bytes} bytes"
                ),
            )
        )

    scanner = _SecurityScanner()
    try:
        while True:
            chunk = raw_spool.read(limits.read_chunk_bytes)
            if not chunk:
                break
            scanner.feed(chunk)
    except BaseException:
        raw_spool.close()
        raise
    raw_spool.seek(0)
    return _PreparedSource(
        stream=raw_spool,
        source_name=_source_name(source),
        source_bytes=source_bytes,
        expanded_bytes=source_bytes,
    )


def _stream_records(
    stream: BinaryIO,
    limits: ElementXmlLimits,
    issues: _IssueCollector,
    namespace_allowlist: Optional[frozenset],
    progress: _ParseProgress,
    *,
    root_schema_claim_allowed: bool = False,
) -> Tuple[List[ElementRecord], int]:
    parser = ET.XMLPullParser(events=("start", "end"))
    frames: List[_PathFrame] = []
    current_record: Optional[_RecordBuilder] = None
    records: List[ElementRecord] = []
    records_seen = 0
    node_count = 0
    total_text_chars = 0

    def consume_events() -> None:
        nonlocal current_record, records_seen, node_count, total_text_chars
        for event, element in parser.read_events():
            namespace_uri, local_name = _split_qname(element.tag)
            if not _LOCAL_NAME_RE.fullmatch(local_name):
                raise _AbortParse(
                    ElementParseIssue(
                        code="invalid_local_name",
                        severity=IssueSeverity.FATAL,
                        message=f"invalid or oversized XML local name: {local_name!r}",
                    )
                )

            if event == "start":
                node_count += 1
                if node_count > limits.max_nodes:
                    raise _AbortParse(
                        ElementParseIssue(
                            code="node_limit_exceeded",
                            severity=IssueSeverity.FATAL,
                            message=f"element XML exceeded {limits.max_nodes} nodes",
                        )
                    )

                attribute_chars = sum(
                    len(str(key)) + len(str(value))
                    for key, value in element.attrib.items()
                )
                total_text_chars += attribute_chars
                if total_text_chars > limits.max_text_chars:
                    raise _AbortParse(
                        ElementParseIssue(
                            code="text_limit_exceeded",
                            severity=IssueSeverity.FATAL,
                            message=(
                                "element XML exceeded "
                                f"{limits.max_text_chars} text characters"
                            ),
                            record_index=(
                                current_record.record_index if current_record else None
                            ),
                            record_path=(current_record.path if current_record else None),
                        )
                    )
                if _element_claims_schema(element) and not (
                    node_count == 1 and root_schema_claim_allowed
                ):
                    raise _AbortParse(
                        ElementParseIssue(
                            code="xml_schema_declaration_not_allowed",
                            severity=IssueSeverity.FATAL,
                            message=(
                                "XML schema declarations are accepted only on the "
                                "document root and only for the pinned FHWA schema"
                            ),
                            record_index=(
                                current_record.record_index if current_record else None
                            ),
                            record_path=(current_record.path if current_record else None),
                        )
                    )

                if frames:
                    parent = frames[-1]
                    parent.child_counts[local_name] += 1
                    occurrence = parent.child_counts[local_name]
                    path = f"{parent.path}/{local_name}[{occurrence}]"
                else:
                    path = f"/{local_name}[1]"
                frames.append(_PathFrame(local_name, namespace_uri, path))
                depth = len(frames)
                if depth > limits.max_depth:
                    raise _AbortParse(
                        ElementParseIssue(
                            code="depth_limit_exceeded",
                            severity=IssueSeverity.FATAL,
                            message=f"element XML exceeded depth {limits.max_depth}",
                            record_index=(
                                current_record.record_index if current_record else None
                            ),
                            record_path=(current_record.path if current_record else path),
                        )
                    )

                if local_name == "FHWAED":
                    if current_record is not None:
                        raise _AbortParse(
                            ElementParseIssue(
                                code="nested_fhwaed",
                                severity=IssueSeverity.FATAL,
                                message="FHWAED records may not be nested",
                                record_index=current_record.record_index,
                                record_path=path,
                            )
                        )
                    if (
                        namespace_allowlist is not None
                        and (namespace_uri or "") not in namespace_allowlist
                    ):
                        raise _AbortParse(
                            ElementParseIssue(
                                code="namespace_not_allowed",
                                severity=IssueSeverity.FATAL,
                                message=(
                                    "FHWAED namespace is not in the configured "
                                    "allowlist"
                                ),
                                record_path=path,
                            )
                        )
                    records_seen += 1
                    progress.records_seen = records_seen
                    if records_seen > limits.max_records:
                        raise _AbortParse(
                            ElementParseIssue(
                                code="record_limit_exceeded",
                                severity=IssueSeverity.FATAL,
                                message=(
                                    "element XML exceeded "
                                    f"{limits.max_records} FHWAED records"
                                ),
                                record_index=records_seen,
                                record_path=path,
                            )
                        )
                    current_record = _RecordBuilder(
                        record_index=records_seen,
                        path=path,
                        namespace_uri=namespace_uri,
                        depth=depth,
                    )
                elif current_record is not None and depth == current_record.depth + 1:
                    upper_name = local_name.upper()
                    if upper_name in _FIELD_ALIASES and local_name != upper_name:
                        issues.add(
                            ElementParseIssue(
                                code="invalid_field_case",
                                severity=IssueSeverity.ERROR,
                                message=(
                                    f"FHWAED field {local_name!r} must use its "
                                    "declared uppercase local name"
                                ),
                                record_index=current_record.record_index,
                                record_path=current_record.path,
                                field_name=local_name,
                            )
                        )

            else:  # end
                if not frames:
                    raise _AbortParse(
                        ElementParseIssue(
                            code="xml_event_stack_error",
                            severity=IssueSeverity.FATAL,
                            message="unexpected XML end event",
                        )
                    )
                depth = len(frames)
                frame = frames[-1]
                element_text = element.text or ""
                tail_text = element.tail or ""
                total_text_chars += len(element_text) + len(tail_text)
                if total_text_chars > limits.max_text_chars:
                    raise _AbortParse(
                        ElementParseIssue(
                            code="text_limit_exceeded",
                            severity=IssueSeverity.FATAL,
                            message=(
                                "element XML exceeded "
                                f"{limits.max_text_chars} text characters"
                            ),
                            record_index=(
                                current_record.record_index if current_record else None
                            ),
                            record_path=(current_record.path if current_record else frame.path),
                        )
                    )

                if current_record is not None and depth == current_record.depth + 1:
                    canonical = _FIELD_ALIASES.get(local_name)
                    if canonical is not None:
                        if frame.namespace_uri != current_record.namespace_uri:
                            issues.add(
                                ElementParseIssue(
                                    code="field_namespace_mismatch",
                                    severity=IssueSeverity.ERROR,
                                    message=(
                                        f"{local_name} is not in the FHWAED record "
                                        "namespace"
                                    ),
                                    record_index=current_record.record_index,
                                    record_path=current_record.path,
                                    field_name=canonical,
                                    raw_text=element_text,
                                )
                            )
                        elif canonical in current_record.raw_fields:
                            issues.add(
                                ElementParseIssue(
                                    code="duplicate_field",
                                    severity=IssueSeverity.ERROR,
                                    message=f"duplicate {canonical} field; first value retained",
                                    record_index=current_record.record_index,
                                    record_path=current_record.path,
                                    field_name=canonical,
                                    raw_text=element_text,
                                )
                            )
                        elif len(element_text) > limits.max_field_text_chars:
                            raise _AbortParse(
                                ElementParseIssue(
                                    code="field_text_limit_exceeded",
                                    severity=IssueSeverity.FATAL,
                                    message=(
                                        f"{canonical} exceeded "
                                        f"{limits.max_field_text_chars} characters"
                                    ),
                                    record_index=current_record.record_index,
                                    record_path=current_record.path,
                                    field_name=canonical,
                                )
                            )
                        elif len(element):
                            issues.add(
                                ElementParseIssue(
                                    code="nested_scalar_field",
                                    severity=IssueSeverity.ERROR,
                                    message=f"{canonical} must contain scalar text only",
                                    record_index=current_record.record_index,
                                    record_path=current_record.path,
                                    field_name=canonical,
                                    raw_text=element_text,
                                )
                            )
                        else:
                            current_record.raw_fields[canonical] = (
                                local_name,
                                element_text,
                            )

                if (
                    current_record is not None
                    and depth == current_record.depth
                    and local_name == "FHWAED"
                ):
                    records.append(_finalize_record(current_record, issues))
                    current_record = None

                frames.pop()
                element.clear()

    while True:
        chunk = stream.read(limits.read_chunk_bytes)
        if not chunk:
            break
        parser.feed(chunk)
        consume_events()
    parser.close()
    consume_events()
    return records, records_seen


def _finalize_record(
    builder: _RecordBuilder,
    issues: _IssueCollector,
) -> ElementRecord:
    parsed_fields: Dict[str, ElementField] = {}
    for name in _CANONICAL_FIELDS:
        raw_entry = builder.raw_fields.get(name)
        parsed_fields[name] = _parse_field(name, raw_entry, builder, issues)

    structure = parsed_fields["STRUCNUM"]
    state = parsed_fields["STATE"]
    agency = parsed_fields["AGENCY"]
    identity = BridgeIdentity(
        structure_number_raw=structure.raw_text,
        state_raw=state.raw_text,
        agency_raw=agency.raw_text,
    )
    return ElementRecord(
        record_index=builder.record_index,
        record_path=builder.path,
        line_number=None,
        namespace_uri=builder.namespace_uri,
        bridge_identity=identity,
        fields=MappingProxyType(parsed_fields),
    )


def _parse_field(
    name: str,
    raw_entry: Optional[Tuple[str, str]],
    builder: _RecordBuilder,
    issues: _IssueCollector,
) -> ElementField:
    if raw_entry is None:
        if name in {"STRUCNUM", "EN"}:
            issues.add(
                ElementParseIssue(
                    code="required_field_missing",
                    severity=IssueSeverity.ERROR,
                    message=f"required FHWAED field {name} is missing",
                    record_index=builder.record_index,
                    record_path=builder.path,
                    field_name=name,
                )
            )
        return ElementField(
            name=name,
            xml_name=None,
            raw_text=None,
            presence=FieldPresence.MISSING,
            value=None,
        )

    xml_name, raw_text = raw_entry
    stripped = raw_text.strip()
    if not stripped:
        if name in _NUMERIC_FIELDS or name == "STRUCNUM":
            issues.add(
                ElementParseIssue(
                    code="field_empty",
                    severity=(
                        IssueSeverity.ERROR
                        if name in {"STRUCNUM", "EN"}
                        else IssueSeverity.WARNING
                    ),
                    message=f"FHWAED field {name} is present but empty",
                    record_index=builder.record_index,
                    record_path=builder.path,
                    field_name=name,
                    raw_text=raw_text,
                )
            )
        return ElementField(
            name=name,
            xml_name=xml_name,
            raw_text=raw_text,
            presence=FieldPresence.EMPTY,
            value=None,
        )

    if name not in _NUMERIC_FIELDS:
        return ElementField(
            name=name,
            xml_name=xml_name,
            raw_text=raw_text,
            presence=FieldPresence.VALUE,
            value=stripped,
        )

    parsed: Optional[Decimal]
    try:
        parsed = Decimal(stripped)
    except InvalidOperation:
        parsed = None
        code = "numeric_invalid"
        message = f"{name} is not a valid decimal number"
    else:
        code = ""
        message = ""
        if not parsed.is_finite():
            code = "numeric_not_finite"
            message = f"{name} must be finite"
            parsed = None
        elif parsed < 0:
            code = "numeric_negative"
            message = f"{name} must be nonnegative"
            parsed = None
        elif name in _INTEGER_FIELDS and parsed != parsed.to_integral_value():
            code = "numeric_not_integer"
            message = f"{name} must be an integer"
            parsed = None
        elif name == "EN" and parsed == 0:
            code = "element_number_not_positive"
            message = "EN must be a positive integer"
            parsed = None

    if parsed is None:
        issues.add(
            ElementParseIssue(
                code=code,
                severity=IssueSeverity.ERROR,
                message=message,
                record_index=builder.record_index,
                record_path=builder.path,
                field_name=name,
                raw_text=raw_text,
            )
        )

    return ElementField(
        name=name,
        xml_name=xml_name,
        raw_text=raw_text,
        presence=FieldPresence.VALUE,
        value=parsed,
    )


def _validate_record_set(
    records: Sequence[ElementRecord],
    limits: ElementXmlLimits,
    issues: _IssueCollector,
) -> None:
    by_group_and_element: DefaultDict[
        Tuple[Optional[str], Optional[str], Optional[str]],
        DefaultDict[Decimal, List[ElementRecord]],
    ] = defaultdict(lambda: defaultdict(list))
    key_first_seen: Dict[
        Tuple[
            Tuple[Optional[str], Optional[str], Optional[str]],
            Decimal,
            Optional[Decimal],
        ],
        ElementRecord,
    ] = {}
    parents_by_element: DefaultDict[
        Tuple[Tuple[Optional[str], Optional[str], Optional[str]], Decimal],
        set,
    ] = defaultdict(set)

    for record in records:
        _validate_condition_quantities(record, limits, issues)
        group = _record_group(record)
        element_number = record.element_number
        parent_number = record.parent_element_number
        if element_number is None or group[0] is None:
            continue

        by_group_and_element[group][element_number].append(record)
        key = (group, element_number, parent_number)
        first = key_first_seen.get(key)
        if first is not None:
            issues.add(
                ElementParseIssue(
                    code="duplicate_element_key",
                    severity=IssueSeverity.ERROR,
                    message=(
                        "duplicate element key; first seen at record "
                        f"{first.record_index}"
                    ),
                    record_index=record.record_index,
                    record_path=record.record_path,
                    field_name="EN",
                    raw_text=record.fields["EN"].raw_text,
                )
            )
        else:
            key_first_seen[key] = record
        parents_by_element[(group, element_number)].add(parent_number)

    for (group, element_number), parent_values in parents_by_element.items():
        if len(parent_values) > 1:
            records_for_element = by_group_and_element[group][element_number]
            record = records_for_element[-1]
            issues.add(
                ElementParseIssue(
                    code="element_parent_conflict",
                    severity=IssueSeverity.ERROR,
                    message=(
                        f"element {element_number} has multiple parent values "
                        "within one bridge"
                    ),
                    record_index=record.record_index,
                    record_path=record.record_path,
                    field_name="EPN",
                    raw_text=record.fields["EPN"].raw_text,
                )
            )

    for record in records:
        group = _record_group(record)
        element_number = record.element_number
        parent_number = record.parent_element_number
        if (
            group[0] is None
            or element_number is None
            or parent_number is None
            or parent_number == 0
        ):
            continue
        if parent_number == element_number:
            issues.add(
                ElementParseIssue(
                    code="element_self_parent",
                    severity=IssueSeverity.ERROR,
                    message=f"element {element_number} cannot be its own parent",
                    record_index=record.record_index,
                    record_path=record.record_path,
                    field_name="EPN",
                    raw_text=record.fields["EPN"].raw_text,
                )
            )
            continue
        possible_parents = by_group_and_element[group].get(parent_number, [])
        if not possible_parents:
            issues.add(
                ElementParseIssue(
                    code="element_parent_missing",
                    severity=IssueSeverity.WARNING,
                    message=(
                        f"parent element {parent_number} does not exist for this bridge"
                    ),
                    record_index=record.record_index,
                    record_path=record.record_path,
                    field_name="EPN",
                    raw_text=record.fields["EPN"].raw_text,
                )
            )
        elif len(possible_parents) > 1:
            issues.add(
                ElementParseIssue(
                    code="element_parent_ambiguous",
                    severity=IssueSeverity.ERROR,
                    message=(
                        f"parent element {parent_number} has duplicate definitions"
                    ),
                    record_index=record.record_index,
                    record_path=record.record_path,
                    field_name="EPN",
                    raw_text=record.fields["EPN"].raw_text,
                )
            )

    _detect_parent_cycles(by_group_and_element, issues)


def _validate_condition_quantities(
    record: ElementRecord,
    limits: ElementXmlLimits,
    issues: _IssueCollector,
) -> None:
    total = record.fields["TOTALQTY"].value
    states = [record.fields[f"CS{number}"].value for number in range(1, 5)]
    if not isinstance(total, Decimal) or not all(
        isinstance(value, Decimal) for value in states
    ):
        return
    state_total = sum(states, Decimal(0))  # type: ignore[arg-type]
    difference = state_total - total
    if difference > limits.quantity_tolerance:
        issues.add(
            ElementParseIssue(
                code="condition_sum_exceeds_total",
                severity=IssueSeverity.ERROR,
                message=(
                    f"CS1-CS4 sum ({state_total}) exceeds TOTALQTY ({total})"
                ),
                record_index=record.record_index,
                record_path=record.record_path,
                field_name="TOTALQTY",
                raw_text=record.fields["TOTALQTY"].raw_text,
            )
        )
    elif abs(difference) > limits.quantity_tolerance:
        issues.add(
            ElementParseIssue(
                code="condition_sum_differs_total",
                severity=IssueSeverity.WARNING,
                message=(
                    f"CS1-CS4 sum ({state_total}) differs from TOTALQTY ({total})"
                ),
                record_index=record.record_index,
                record_path=record.record_path,
                field_name="TOTALQTY",
                raw_text=record.fields["TOTALQTY"].raw_text,
            )
        )


def _detect_parent_cycles(
    grouped: Mapping[
        Tuple[Optional[str], Optional[str], Optional[str]],
        Mapping[Decimal, List[ElementRecord]],
    ],
    issues: _IssueCollector,
) -> None:
    for element_map in grouped.values():
        graph: Dict[Decimal, Decimal] = {}
        record_by_element: Dict[Decimal, ElementRecord] = {}
        for element_number, element_records in element_map.items():
            if len(element_records) != 1:
                continue
            record = element_records[0]
            parent = record.parent_element_number
            if parent is not None and parent != 0 and parent in element_map:
                graph[element_number] = parent
                record_by_element[element_number] = record

        visited: set = set()
        visiting: set = set()

        def visit(node: Decimal, trail: List[Decimal]) -> None:
            if node in visiting:
                start = trail.index(node) if node in trail else 0
                cycle = trail[start:] + [node]
                record = record_by_element.get(node)
                issues.add(
                    ElementParseIssue(
                        code="element_parent_cycle",
                        severity=IssueSeverity.ERROR,
                        message=(
                            "element parent cycle: "
                            + " -> ".join(str(item) for item in cycle)
                        ),
                        record_index=(record.record_index if record else None),
                        record_path=(record.record_path if record else None),
                        field_name="EPN",
                    )
                )
                return
            if node in visited:
                return
            visiting.add(node)
            parent = graph.get(node)
            if parent is not None:
                visit(parent, trail + [node])
            visiting.discard(node)
            visited.add(node)

        for element_number in tuple(graph):
            if element_number not in visited:
                visit(element_number, [])


def _record_group(
    record: ElementRecord,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    identity = record.bridge_identity
    return (
        normalize_structure_number(identity.structure_number_raw),
        identity.state,
        identity.agency,
    )


def _split_qname(tag: object) -> Tuple[Optional[str], str]:
    if not isinstance(tag, str):
        raise _AbortParse(
            ElementParseIssue(
                code="invalid_xml_tag",
                severity=IssueSeverity.FATAL,
                message="XML tag must be a string QName",
            )
        )
    if tag.startswith("{"):
        end = tag.find("}")
        if end <= 1 or end == len(tag) - 1:
            raise _AbortParse(
                ElementParseIssue(
                    code="invalid_xml_qname",
                    severity=IssueSeverity.FATAL,
                    message=f"invalid expanded XML QName: {tag!r}",
                )
            )
        return tag[1:end], tag[end + 1 :]
    return None, tag


def _declared_root_schema_claim(stream: BinaryIO) -> Optional[str]:
    """Return a root XSD claim without trusting or following its location."""

    parser = ET.XMLPullParser(events=("start",))
    claims: List[str] = []
    try:
        stream.seek(0)
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            parser.feed(chunk)
            for _, element in parser.read_events():
                for raw_name, raw_value in element.attrib.items():
                    namespace_uri, local_name = _split_qname(raw_name)
                    if local_name in {
                        "schemaLocation",
                        "noNamespaceSchemaLocation",
                    } and namespace_uri in {None, _SCHEMA_INSTANCE_URI}:
                        claims.append(str(raw_value).strip())
                if len(claims) > 1:
                    raise _AbortParse(
                        ElementParseIssue(
                            code="xml_schema_claim_ambiguous",
                            severity=IssueSeverity.FATAL,
                            message="the document root contains multiple schema claims",
                        )
                    )
                return claims[0] if claims else None
        return None
    finally:
        stream.seek(0)


def _canonical_schema_claim(claim: Optional[str]) -> Optional[str]:
    if claim is None:
        return None
    if not isinstance(claim, str):
        raise _AbortParse(
            ElementParseIssue(
                code="xml_schema_unsupported",
                severity=IssueSeverity.FATAL,
                message="the XML schema claim must be a supported string identifier",
            )
        )
    normalized = claim.strip()
    if not normalized or len(normalized) > 2048:
        raise _AbortParse(
            ElementParseIssue(
                code="xml_schema_unsupported",
                severity=IssueSeverity.FATAL,
                message="the XML schema claim is empty or oversized",
            )
        )
    if normalized not in _LEGACY_ELEMENT_SCHEMA_CLAIMS:
        raise _AbortParse(
            ElementParseIssue(
                code="xml_schema_unsupported",
                severity=IssueSeverity.FATAL,
                message=(
                    "the claimed XML schema is not the pinned FHWA Element "
                    "Data Schema Version 1.0"
                ),
            )
        )
    return LEGACY_ELEMENT_SCHEMA_ID


@lru_cache(maxsize=1)
def _compiled_legacy_element_schema(verified_sha256: str):
    """Compile only the locally pinned, byte-verified XSD."""

    if verified_sha256 != LEGACY_ELEMENT_SCHEMA_SHA256:
        raise ValueError("unexpected legacy element schema digest")
    try:
        import xmlschema
    except ImportError as exc:  # pragma: no cover - deployment dependency gate
        raise RuntimeError("xmlschema dependency unavailable") from exc
    return xmlschema.XMLSchema(
        _LEGACY_ELEMENT_SCHEMA_PATH,
        allow="local",
        defuse="always",
        use_fallback=False,
    )


def _validate_pinned_legacy_element_schema(stream: BinaryIO) -> None:
    try:
        schema_bytes = _LEGACY_ELEMENT_SCHEMA_PATH.read_bytes()
    except OSError as exc:
        raise _AbortParse(
            ElementParseIssue(
                code="xml_schema_artifact_unavailable",
                severity=IssueSeverity.FATAL,
                message="the pinned FHWA element XSD artifact is unavailable",
            )
        ) from exc

    digest = hashlib.sha256(schema_bytes).hexdigest()
    if digest != LEGACY_ELEMENT_SCHEMA_SHA256:
        raise _AbortParse(
            ElementParseIssue(
                code="xml_schema_artifact_hash_mismatch",
                severity=IssueSeverity.FATAL,
                message="the pinned FHWA element XSD failed its SHA-256 check",
            )
        )

    try:
        import xmlschema

        schema = _compiled_legacy_element_schema(digest)
        stream.seek(0)
        resource = xmlschema.XMLResource(
            stream,
            allow="local",
            defuse="always",
            timeout=2,
        )
        first_error = next(
            schema.iter_errors(
                resource,
                use_defaults=False,
                use_location_hints=False,
            ),
            None,
        )
    except ImportError as exc:  # pragma: no cover - deployment dependency gate
        raise _AbortParse(
            ElementParseIssue(
                code="xml_schema_dependency_unavailable",
                severity=IssueSeverity.FATAL,
                message="the pinned XSD validator dependency is unavailable",
            )
        ) from exc
    except _AbortParse:
        raise
    except Exception as exc:
        raise _AbortParse(
            ElementParseIssue(
                code="xml_schema_validation_failed",
                severity=IssueSeverity.FATAL,
                message=(
                    "the pinned FHWA element XSD validator could not safely "
                    f"process the document ({type(exc).__name__})"
                ),
            )
        ) from exc
    finally:
        stream.seek(0)

    if first_error is not None:
        path = str(getattr(first_error, "path", "") or "")[:256]
        reason = str(getattr(first_error, "reason", "invalid structure"))[:500]
        detail = f" at {path}" if path.startswith("/") else ""
        raise _AbortParse(
            ElementParseIssue(
                code="xml_schema_validation_error",
                severity=IssueSeverity.FATAL,
                message=(
                    "element XML does not conform to the pinned FHWA Element "
                    f"Data Schema Version 1.0{detail}: {reason}"
                ),
            )
        )


def _element_claims_schema(element: ET.Element) -> bool:
    for raw_name in element.attrib:
        namespace_uri, local_name = _split_qname(raw_name)
        if local_name in {"schemaLocation", "noNamespaceSchemaLocation"} and (
            namespace_uri in {None, _SCHEMA_INSTANCE_URI}
        ):
            return True
    return False


def _clean_identity(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    cleaned = raw.strip()
    return cleaned or None


def _normalize_code(raw: Optional[str]) -> Optional[str]:
    cleaned = _clean_identity(raw)
    if cleaned is None:
        return None
    normalized = cleaned.upper()
    if normalized.isdigit():
        return str(int(normalized))
    return normalized


def _normalize_state_identity(raw: Optional[str]) -> Optional[str]:
    """Normalize either legacy SSF Item 1 or a two-digit state FIPS value."""

    cleaned = _clean_identity(raw)
    if cleaned is None:
        return None
    if cleaned.isdigit():
        numeric = int(cleaned)
        valid_fips = {
            1, 2, 4, 5, 6, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18, 19,
            20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33,
            34, 35, 36, 37, 38, 39, 40, 41, 42, 44, 45, 46, 47, 48,
            49, 50, 51, 53, 54, 55, 56, 60, 66, 69, 72, 78,
        }
        if numeric in valid_fips:
            # Accept ordinary FIPS values, including zero-padded XML such as
            # 034 for New Jersey.
            return str(numeric)
        if len(cleaned) == 3 and int(cleaned[:2]) in valid_fips:
            # Legacy Item 1 stores state FIPS in the first two positions and
            # an FHWA region in the third (342 -> New Jersey 34).
            return str(int(cleaned[:2]))
        return str(numeric)
    return cleaned.upper()


__all__ = [
    "BridgeCandidate",
    "BridgeIdentity",
    "ElementField",
    "ElementJoinResult",
    "ElementParseIssue",
    "ElementParseResult",
    "ElementRecord",
    "ElementRecordMatch",
    "ElementXmlLimits",
    "FieldPresence",
    "IssueSeverity",
    "MatchStatus",
    "match_element_records",
    "normalize_structure_number",
    "parse_element_xml",
    "parse_elements_xml_v2",
]
