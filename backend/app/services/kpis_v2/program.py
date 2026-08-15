"""Strict program-KPI file parsing, approval, persistence, and reads."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
from typing import Any, Iterable
from uuid import UUID, uuid5
from xml.etree import ElementTree
import zipfile

from app.services.conversion.source_artifacts import ArtifactReceipt

from .registry import DEFAULT_KPI_REGISTRY, KpiDefinition


PROGRAM_TEMPLATE_VERSION = "bridgeguard-program-kpi-template-v1"
PROGRAM_DEFINITION_VERSION = "bridgeguard-program-kpi-v1"
EXPECTED_HEADERS = (
    "measure_id",
    "fiscal_year",
    "value_kind",
    "value",
    "unit",
    "organization",
    "source_locator",
)
VALUE_KINDS = frozenset({"actual", "budgeted", "revised", "target", "estimate"})
MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 10_000
_UUID_NAMESPACE = UUID("9124a798-02de-5b4e-b828-09a5e46bd4da")
_CELL_RE = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")


class ProgramKpiError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ProgramKpiIssue:
    severity: str
    code: str
    message: str
    source_locator: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "source_locator": self.source_locator,
        }


@dataclass(frozen=True, slots=True)
class ProgramKpiRow:
    measure_id: str
    fiscal_year: int
    value_kind: str
    value: Decimal
    unit: str
    organization: str
    source_locator: str
    row_number: int

    def identity(self) -> tuple[str, int, str, str]:
        return self.measure_id, self.fiscal_year, self.value_kind, self.organization

    def as_dict(self) -> dict[str, object]:
        return {
            "measure_id": self.measure_id,
            "fiscal_year": self.fiscal_year,
            "value_kind": self.value_kind,
            "value": format(self.value, "f"),
            "unit": self.unit,
            "organization": self.organization,
            "source_locator": self.source_locator,
            "row_number": self.row_number,
        }


@dataclass(frozen=True, slots=True)
class ProgramKpiPreview:
    filename: str
    media_type: str
    source_sha256: str
    import_sha256: str
    rows: tuple[ProgramKpiRow, ...]
    issues: tuple[ProgramKpiIssue, ...]

    @property
    def ready(self) -> bool:
        return bool(self.rows) and not any(issue.severity == "error" for issue in self.issues)

    def as_dict(self) -> dict[str, object]:
        return {
            "template_version": PROGRAM_TEMPLATE_VERSION,
            "filename": self.filename,
            "media_type": self.media_type,
            "source_sha256": self.source_sha256,
            "import_sha256": self.import_sha256,
            "ready": self.ready,
            "row_count": len(self.rows),
            "rows": [row.as_dict() for row in self.rows],
            "issues": [issue.as_dict() for issue in self.issues],
        }


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _column_index(letters: str) -> int:
    result = 0
    for char in letters:
        result = result * 26 + ord(char) - 64
    return result - 1


def _xlsx_rows(data: bytes) -> list[list[str]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ProgramKpiError("invalid_xlsx", "The Excel source is not a valid XLSX file.") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > 4096 or sum(item.file_size for item in infos) > 64 * 1024 * 1024:
            raise ProgramKpiError("xlsx_too_complex", "The Excel source exceeds safe workbook limits.")
        for item in infos:
            path = PurePosixPath(item.filename)
            if item.flag_bits & 0x1 or path.is_absolute() or ".." in path.parts:
                raise ProgramKpiError("invalid_xlsx", "The Excel source contains an unsafe member.")
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(node.itertext()) for node in root if node.tag.endswith("}si")]
        sheet_name = "xl/worksheets/sheet1.xml"
        if sheet_name not in archive.namelist():
            raise ProgramKpiError("xlsx_sheet_missing", "The Excel template must use its first worksheet.")
        root = ElementTree.fromstring(archive.read(sheet_name))
        rows: list[list[str]] = []
        for row_node in (node for node in root.iter() if node.tag.endswith("}row")):
            cells: dict[int, str] = {}
            for cell in (node for node in row_node if node.tag.endswith("}c")):
                reference = str(cell.attrib.get("r") or "")
                match = _CELL_RE.fullmatch(reference)
                if not match:
                    raise ProgramKpiError("invalid_xlsx_cell", "The Excel source contains an invalid cell reference.")
                if any(child.tag.endswith("}f") for child in cell):
                    raise ProgramKpiError("xlsx_formula_rejected", "Program KPI imports do not execute or trust spreadsheet formulas.")
                kind = cell.attrib.get("t")
                value_node = next((child for child in cell if child.tag.endswith("}v")), None)
                if kind == "inlineStr":
                    value = "".join(cell.itertext())
                else:
                    raw = "" if value_node is None or value_node.text is None else value_node.text
                    if kind == "s":
                        try:
                            value = shared[int(raw)]
                        except (ValueError, IndexError) as exc:
                            raise ProgramKpiError("invalid_xlsx_shared_string", "The Excel source contains an invalid shared string.") from exc
                    else:
                        value = raw
                cells[_column_index(match.group(1))] = value
            width = max(cells, default=-1) + 1
            rows.append([cells.get(index, "") for index in range(width)])
            if len(rows) > MAX_ROWS + 1:
                raise ProgramKpiError("too_many_rows", "The program KPI source exceeds the row limit.")
        return rows


def _csv_rows(data: bytes) -> list[list[str]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ProgramKpiError("invalid_encoding", "CSV program sources must be UTF-8.") from exc
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as exc:
        raise ProgramKpiError("invalid_csv", "The CSV program source is malformed.") from exc
    if len(rows) > MAX_ROWS + 1:
        raise ProgramKpiError("too_many_rows", "The program KPI source exceeds the row limit.")
    return rows


def preview_program_kpis(
    data: bytes,
    *,
    filename: str,
    media_type: str | None = None,
) -> ProgramKpiPreview:
    payload = bytes(data)
    if not payload:
        raise ProgramKpiError("empty_source", "The program KPI source is empty.")
    if len(payload) > MAX_BYTES:
        raise ProgramKpiError("source_too_large", "The program KPI source exceeds the byte limit.")
    safe_name = Path(str(filename)).name
    suffix = Path(safe_name).suffix.lower()
    if suffix == ".csv":
        raw_rows = _csv_rows(payload)
        resolved_media = "text/csv"
    elif suffix == ".xlsx":
        raw_rows = _xlsx_rows(payload)
        resolved_media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        raise ProgramKpiError("unsupported_source_type", "Program KPI sources must be .csv or .xlsx.")
    if not raw_rows:
        raise ProgramKpiError("empty_source", "The program KPI source has no header row.")
    headers = tuple(str(value).strip() for value in raw_rows[0])
    if len(headers) != len(set(headers)):
        raise ProgramKpiError("duplicate_headers", "The program KPI source contains duplicate headers.")
    if headers != EXPECTED_HEADERS:
        raise ProgramKpiError("invalid_headers", "The program KPI template headers or order are invalid.")

    issues: list[ProgramKpiIssue] = []
    parsed: list[ProgramKpiRow] = []
    identities: set[tuple[str, int, str, str]] = set()
    for row_number, raw in enumerate(raw_rows[1:], 2):
        if not any(str(value).strip() for value in raw):
            continue
        values = list(raw) + [""] * (len(EXPECTED_HEADERS) - len(raw))
        if len(raw) > len(EXPECTED_HEADERS) and any(str(value).strip() for value in raw[len(EXPECTED_HEADERS):]):
            issues.append(ProgramKpiIssue("error", "extra_columns", "Unexpected populated columns.", f"row {row_number}"))
            continue
        record = dict(zip(EXPECTED_HEADERS, (str(value).strip() for value in values)))
        try:
            definition = DEFAULT_KPI_REGISTRY.get(record["measure_id"])
        except KeyError:
            issues.append(ProgramKpiIssue("error", "unknown_measure", "Measure ID is not registered.", f"row {row_number}"))
            continue
        if definition.definition_version != PROGRAM_DEFINITION_VERSION:
            issues.append(ProgramKpiIssue("error", "inventory_measure_not_allowed", "Only governed program measures are allowed in this template.", f"row {row_number}"))
            continue
        try:
            fiscal_year = int(record["fiscal_year"])
        except ValueError:
            fiscal_year = 0
        if not 1900 <= fiscal_year <= 2200:
            issues.append(ProgramKpiIssue("error", "invalid_fiscal_year", "Fiscal year must be between 1900 and 2200.", f"row {row_number}"))
            continue
        kind = record["value_kind"].lower()
        if kind not in VALUE_KINDS:
            issues.append(ProgramKpiIssue("error", "invalid_value_kind", "Value kind must be actual, budgeted, revised, target, or estimate.", f"row {row_number}"))
            continue
        try:
            value = Decimal(record["value"])
        except InvalidOperation:
            value = Decimal("NaN")
        if not value.is_finite():
            issues.append(ProgramKpiIssue("error", "invalid_value", "Value must be a finite decimal.", f"row {row_number}"))
            continue
        if record["unit"] != definition.unit:
            issues.append(ProgramKpiIssue("error", "unit_mismatch", f"Unit must be {definition.unit} for {definition.measure_id}.", f"row {row_number}"))
            continue
        organization = record["organization"]
        locator = record["source_locator"]
        if not organization or len(organization) > 120 or not locator or len(locator) > 250:
            issues.append(ProgramKpiIssue("error", "missing_source_context", "Organization and bounded source locator are required.", f"row {row_number}"))
            continue
        row = ProgramKpiRow(definition.measure_id, fiscal_year, kind, value, definition.unit, organization, locator, row_number)
        if row.identity() in identities:
            issues.append(ProgramKpiIssue("error", "duplicate_program_value", "Duplicate measure/year/kind/organization value.", f"row {row_number}"))
            continue
        identities.add(row.identity())
        parsed.append(row)
    if not parsed and not issues:
        issues.append(ProgramKpiIssue("error", "no_values", "The program KPI source contains no data rows."))
    source_sha = hashlib.sha256(payload).hexdigest()
    import_sha = hashlib.sha256(
        _canonical({
            "template_version": PROGRAM_TEMPLATE_VERSION,
            "source_sha256": source_sha,
            "rows": [row.as_dict() for row in parsed],
            "issues": [issue.as_dict() for issue in issues],
        })
    ).hexdigest()
    return ProgramKpiPreview(safe_name, media_type or resolved_media, source_sha, import_sha, tuple(parsed), tuple(issues))


def _id(kind: str, *parts: object) -> str:
    return str(uuid5(_UUID_NAMESPACE, ":".join([kind, *(str(part) for part in parts)])))


def _definition_payload(definition: KpiDefinition) -> tuple[str, str]:
    payload = _canonical(definition.as_dict()).decode("utf-8")
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def persist_program_kpis(
    database_path: str | os.PathLike[str],
    *,
    workspace_id: str,
    preview: ProgramKpiPreview,
    artifact: ArtifactReceipt,
    actor_id: str,
    reviewer_id: str | None = None,
    reviewed_at: str | None = None,
) -> dict[str, object]:
    workspace = str(workspace_id).strip()
    actor = str(actor_id).strip().lower()
    reviewer = str(reviewer_id or "").strip().lower()
    if not preview.ready:
        raise ProgramKpiError("preview_not_ready", "Program KPI preview contains blocking issues.")
    if not workspace or not actor:
        raise ProgramKpiError("actor_required", "An authenticated preparer is required.")
    if reviewer and actor == reviewer:
        raise ProgramKpiError("independent_review_required", "A different authenticated reviewer is required.")
    if artifact.sha256 != preview.source_sha256:
        raise ProgramKpiError("source_digest_mismatch", "Stored source bytes do not match the preview.")
    timestamp = reviewed_at or datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    source_id = _id("program-source", workspace, preview.import_sha256)
    connection = sqlite3.connect(str(Path(database_path).resolve()), timeout=5.0)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT s.id,CASE WHEN a.decision='approved' THEN 'approved' ELSE s.approval_status END "
            "FROM kpi_program_sources s LEFT JOIN kpi_program_approvals a "
            "ON a.workspace_id=s.workspace_id AND a.source_id=s.id "
            "WHERE s.workspace_id=? AND s.import_sha256=?",
            (workspace, preview.import_sha256),
        ).fetchone()
        if existing is not None:
            connection.rollback()
            return {"source_id": str(existing[0]), "status": str(existing[1]), "outcome": "noop", "import_sha256": preview.import_sha256}
        connection.execute(
            "INSERT INTO source_artifacts (id,workspace_id,sha256,byte_size,original_filename,media_type,artifact_kind,storage_uri,source_system,created_by,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (artifact.artifact_id, workspace, artifact.sha256, artifact.size_bytes, artifact.original_filename, preview.media_type, "kpi_program_source", f"source-artifact://{artifact.artifact_id}", "program_kpi_upload", actor, artifact.created_at),
        )
        connection.execute(
            "INSERT INTO kpi_program_sources (id,workspace_id,source_artifact_id,source_type,source_locator,import_sha256,approval_status,actor_id,reviewer_id,reviewed_at,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, workspace, artifact.artifact_id, "file_import", preview.filename, preview.import_sha256, "pending_review", actor, None, None, timestamp),
        )
        definition_ids: dict[str, str] = {}
        period_ids: dict[int, str] = {}
        for row in preview.rows:
            definition = DEFAULT_KPI_REGISTRY.get(row.measure_id)
            definition_id = _id("kpi-definition", workspace, row.measure_id, definition.definition_version)
            definition_json, definition_sha = _definition_payload(definition)
            connection.execute(
                "INSERT OR IGNORE INTO kpi_measure_definitions (id,workspace_id,measure_id,definition_version,definition_json,definition_sha256,approval_status,approved_by,approved_at,created_by,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (definition_id, workspace, row.measure_id, definition.definition_version, definition_json, definition_sha, "approved", reviewer, timestamp, actor, timestamp),
            )
            stored = connection.execute(
                "SELECT definition_sha256 FROM kpi_measure_definitions WHERE workspace_id=? AND id=?",
                (workspace, definition_id),
            ).fetchone()
            if stored is None or str(stored[0]) != definition_sha:
                raise ProgramKpiError("definition_conflict", "Stored KPI definition does not match the current registry.")
            definition_ids[row.measure_id] = definition_id
            period_id = _id("kpi-period", workspace, "fiscal_year", row.fiscal_year)
            connection.execute(
                "INSERT OR IGNORE INTO kpi_reporting_periods (id,workspace_id,period_type,period_year,status,created_by,created_at) VALUES (?,?,?,?,?,?,?)",
                (period_id, workspace, "fiscal_year", row.fiscal_year, "closed_source_recorded", actor, timestamp),
            )
            period_ids[row.fiscal_year] = period_id
            connection.execute(
                "INSERT INTO kpi_program_values (id,workspace_id,source_id,measure_definition_id,reporting_period_id,value_kind,value_decimal,unit,organization,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (_id("kpi-value", source_id, *row.identity()), workspace, source_id, definition_id, period_id, row.value_kind, format(row.value, "f"), row.unit, row.organization, timestamp),
            )
        for ordinal, issue in enumerate(preview.issues):
            connection.execute(
                "INSERT INTO kpi_program_issues (id,workspace_id,source_id,issue_ordinal,severity,code,message,source_locator,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (_id("kpi-issue", source_id, ordinal), workspace, source_id, ordinal, issue.severity, issue.code, issue.message, issue.source_locator, timestamp),
            )
        if reviewer:
            connection.execute(
                "INSERT INTO kpi_program_approvals (id,workspace_id,source_id,decision,reviewer_id,reviewed_at,review_note) VALUES (?,?,?,?,?,?,?)",
                (_id("kpi-approval", workspace, source_id), workspace, source_id, "approved", reviewer, timestamp, "Independent review completed during governed import."),
            )
        connection.commit()
    except ProgramKpiError:
        connection.rollback()
        raise
    except sqlite3.Error as exc:
        connection.rollback()
        raise ProgramKpiError("program_store_failure", "Program KPI values could not be persisted.") from exc
    finally:
        connection.close()
    return {"source_id": source_id, "status": "approved" if reviewer else "pending_review", "outcome": "created", "import_sha256": preview.import_sha256, "value_count": len(preview.rows)}


def approve_program_source(
    database_path: str | os.PathLike[str],
    *,
    workspace_id: str,
    source_id: str,
    reviewer_id: str,
    review_note: str,
) -> dict[str, object]:
    reviewer = str(reviewer_id).strip().lower()
    note = str(review_note).strip()
    if not reviewer or not note or len(note) > 500:
        raise ProgramKpiError("review_evidence_required", "Reviewer and bounded review note are required.")
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    connection = sqlite3.connect(str(Path(database_path).resolve()), timeout=5.0)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        source = connection.execute(
            "SELECT actor_id FROM kpi_program_sources WHERE workspace_id=? AND id=?",
            (workspace_id, source_id),
        ).fetchone()
        if source is None:
            raise ProgramKpiError("program_source_not_found", "Program KPI source was not found.")
        if reviewer == str(source[0]).strip().lower():
            raise ProgramKpiError("independent_review_required", "The preparer cannot approve the same program source.")
        existing = connection.execute(
            "SELECT decision,reviewer_id FROM kpi_program_approvals WHERE workspace_id=? AND source_id=?",
            (workspace_id, source_id),
        ).fetchone()
        if existing is not None:
            if tuple(existing) == ("approved", reviewer):
                connection.rollback()
                return {"source_id": source_id, "status": "approved", "outcome": "noop"}
            raise ProgramKpiError("approval_conflict", "Program KPI source already has a different immutable review decision.")
        connection.execute(
            "INSERT INTO kpi_program_approvals (id,workspace_id,source_id,decision,reviewer_id,reviewed_at,review_note) VALUES (?,?,?,?,?,?,?)",
            (_id("kpi-approval", workspace_id, source_id), workspace_id, source_id, "approved", reviewer, timestamp, note),
        )
        connection.execute(
            "UPDATE workspace_state SET data_generation=data_generation+1,updated_at=? WHERE workspace_id=?",
            (timestamp, workspace_id),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise ProgramKpiError("workspace_not_found", "Workspace state was not found.")
        connection.commit()
    except ProgramKpiError:
        connection.rollback()
        raise
    except sqlite3.Error as exc:
        connection.rollback()
        raise ProgramKpiError("approval_store_failure", "Program KPI approval could not be stored.") from exc
    finally:
        connection.close()
    return {"source_id": source_id, "status": "approved", "outcome": "created"}


def list_program_kpis(
    database_path: str | os.PathLike[str],
    *,
    workspace_id: str,
) -> dict[str, object]:
    connection = sqlite3.connect(f"file:{Path(database_path).resolve().as_posix()}?mode=ro", uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        generation = connection.execute(
            "SELECT data_generation,schema_generation FROM workspace_state WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()
        rows = connection.execute(
            "SELECT d.measure_id,d.definition_version,p.period_year,v.value_kind,v.value_decimal,v.unit,v.organization,s.import_sha256,a.reviewed_at "
            "FROM kpi_program_values v JOIN kpi_program_sources s ON s.workspace_id=v.workspace_id AND s.id=v.source_id "
            "JOIN kpi_program_approvals a ON a.workspace_id=s.workspace_id AND a.source_id=s.id AND a.decision='approved' "
            "JOIN kpi_measure_definitions d ON d.workspace_id=v.workspace_id AND d.id=v.measure_definition_id "
            "JOIN kpi_reporting_periods p ON p.workspace_id=v.workspace_id AND p.id=v.reporting_period_id "
            "WHERE v.workspace_id=? "
            "ORDER BY p.period_year,d.measure_id,v.value_kind,v.organization",
            (workspace_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ProgramKpiError("program_read_failure", "Approved program KPI values could not be read.") from exc
    finally:
        connection.close()
    if generation is None:
        raise ProgramKpiError("workspace_not_found", "Workspace state was not found.")
    return {
        "kind": "approved_program_source",
        "definition_version": PROGRAM_DEFINITION_VERSION,
        "data_generation": int(generation[0]),
        "schema_generation": int(generation[1]),
        "values": [dict(row) for row in rows],
    }


__all__ = [
    "EXPECTED_HEADERS",
    "MAX_BYTES",
    "PROGRAM_TEMPLATE_VERSION",
    "ProgramKpiError",
    "ProgramKpiIssue",
    "ProgramKpiPreview",
    "ProgramKpiRow",
    "approve_program_source",
    "list_program_kpis",
    "persist_program_kpis",
    "preview_program_kpis",
]
