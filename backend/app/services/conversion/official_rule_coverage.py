"""Deterministic inventory of the FHWA SNBI validation-rule workbook.

This module does not download standards artifacts and does not infer rules from
documentation prose.  It imports the exact locally-vendored workbook only
after :mod:`app.standards.fhwa_registry` has verified its SHA-256 digest.  Each
physical rule row receives a BridgeGuard coverage identifier and a content
fingerprint; those identifiers are inventory keys, not invented FHWA rule
names.

The resulting manifest accounts for every contiguous, populated row below the
reviewed header.  Imported rows default to ``unimplemented`` /
``pending_implementation`` when adapted to the two existing coverage models.
That deliberately keeps publication fail-closed until a reviewer supplies an
executable implementation, pointer scope, and test evidence for each row.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence
from xml.etree import ElementTree

from app.standards.fhwa_registry import (
    ArtifactState,
    DEFAULT_REGISTRY_PATH,
    RegistryError,
    RuleBundleRegistry,
    load_rule_bundle_registry,
)

from .rule_coverage import (
    CoverageIntent,
    OfficialRuleCoverageRegistry,
    RuleCoverageDeclaration,
    RuleKind,
)
from .validation_profiles import (
    CoverageRegistry,
    CoverageRow,
    CoverageStatus,
)


OFFICIAL_RULE_MANIFEST_FORMAT_VERSION = 1
FHWA_SNBI_VALIDATION_V1_1_ARTIFACT_ID = "validation_logic_v1_1_dec_2025"
FHWA_SNBI_VALIDATION_V1_1_SOURCE_URL = (
    "https://www.fhwa.dot.gov/bridge/snbi/SNBI_Data_Validation_Rules.xlsx"
)
FHWA_SNBI_VALIDATION_V1_1_SHEET = "SNBI Data Validation Rules"
FHWA_SNBI_VALIDATION_V1_1_HEADER_ROW = 9
FHWA_SNBI_VALIDATION_V1_1_HEADERS = (
    "Error Type",
    "SNBI ID",
    "Data Tag",
    "Rule Name",
    "SNBI Item Name",
    "Error Description",
    "Explanation",
    "Range of Values",
)
FHWA_XLSX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_CELL_REFERENCE_RE = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")
_MAX_ZIP_MEMBERS = 4096
_MAX_ZIP_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
_MAX_XML_MEMBER_BYTES = 32 * 1024 * 1024
_MAX_RULE_ROWS = 10_000

PENDING_EXECUTION_REASON = (
    "Official workbook row is inventoried, but no reviewed executable "
    "implementation, pointer scope, or test evidence exists yet."
)


class OfficialRuleCoverageError(ValueError):
    """Raised when official rule evidence cannot be imported exactly."""


@dataclass(frozen=True, slots=True)
class OfficialRuleSourceReadiness:
    artifact_id: str
    bundle_id: str
    source_url: str | None
    artifact_state: str | None
    artifact_sha256: str | None
    ready: bool
    blocker_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "bundle_id": self.bundle_id,
            "source_url": self.source_url,
            "artifact_state": self.artifact_state,
            "artifact_sha256": self.artifact_sha256,
            "ready": self.ready,
            "blocker_codes": list(self.blocker_codes),
        }


@dataclass(frozen=True, slots=True)
class OfficialRuleManifestRow:
    """Compact identity for one physical workbook rule row."""

    source_row: int
    coverage_id: str
    row_sha256: str
    error_type: str
    snbi_id: str
    data_tag: str
    rule_name: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_row, int)
            or isinstance(self.source_row, bool)
            or self.source_row <= FHWA_SNBI_VALIDATION_V1_1_HEADER_ROW
        ):
            raise OfficialRuleCoverageError("source_row must follow the header row")
        _sha256(self.row_sha256, "row_sha256")
        expected_id = _coverage_id(self.source_row, self.row_sha256)
        if self.coverage_id != expected_id:
            raise OfficialRuleCoverageError(
                "coverage_id does not match source_row and row_sha256"
            )
        for name in ("error_type", "snbi_id", "data_tag", "rule_name"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise OfficialRuleCoverageError(f"{name} must be a string")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_row": self.source_row,
            "coverage_id": self.coverage_id,
            "row_sha256": self.row_sha256,
            "error_type": self.error_type,
            "snbi_id": self.snbi_id,
            "data_tag": self.data_tag,
            "rule_name": self.rule_name,
        }


@dataclass(frozen=True, slots=True)
class OfficialRuleCoverageManifest:
    """Versioned, immutable row inventory tied to exact workbook bytes."""

    registry_version: str
    profile_id: str
    rule_bundle_id: str
    artifact_id: str
    artifact_sha256: str
    source_url: str
    sheet_name: str
    header_row: int
    headers: tuple[str, ...]
    rows: tuple[OfficialRuleManifestRow, ...]
    format_version: int = OFFICIAL_RULE_MANIFEST_FORMAT_VERSION

    def __post_init__(self) -> None:
        if self.format_version != OFFICIAL_RULE_MANIFEST_FORMAT_VERSION:
            raise OfficialRuleCoverageError(
                f"unsupported manifest format_version {self.format_version}"
            )
        _identifier(self.registry_version, "registry_version")
        _identifier(self.profile_id, "profile_id")
        _identifier(self.rule_bundle_id, "rule_bundle_id")
        if self.artifact_id != FHWA_SNBI_VALIDATION_V1_1_ARTIFACT_ID:
            raise OfficialRuleCoverageError("unexpected validation artifact_id")
        _sha256(self.artifact_sha256, "artifact_sha256")
        if self.source_url != FHWA_SNBI_VALIDATION_V1_1_SOURCE_URL:
            raise OfficialRuleCoverageError("unexpected validation source_url")
        if self.sheet_name != FHWA_SNBI_VALIDATION_V1_1_SHEET:
            raise OfficialRuleCoverageError("unexpected validation sheet_name")
        if self.header_row != FHWA_SNBI_VALIDATION_V1_1_HEADER_ROW:
            raise OfficialRuleCoverageError("unexpected validation header_row")
        if tuple(self.headers) != FHWA_SNBI_VALIDATION_V1_1_HEADERS:
            raise OfficialRuleCoverageError("unexpected validation headers")
        rows = tuple(self.rows)
        if not rows:
            raise OfficialRuleCoverageError("official rule manifest cannot be empty")
        if len(rows) > _MAX_RULE_ROWS:
            raise OfficialRuleCoverageError("official rule manifest exceeds row limit")
        if any(not isinstance(row, OfficialRuleManifestRow) for row in rows):
            raise OfficialRuleCoverageError(
                "rows must contain OfficialRuleManifestRow values"
            )
        expected_rows = tuple(range(self.header_row + 1, self.header_row + 1 + len(rows)))
        if tuple(row.source_row for row in rows) != expected_rows:
            raise OfficialRuleCoverageError(
                "official workbook rows must be ordered and contiguous"
            )
        coverage_ids = [row.coverage_id for row in rows]
        if len(coverage_ids) != len(set(coverage_ids)):
            raise OfficialRuleCoverageError("duplicate coverage IDs are not allowed")
        object.__setattr__(self, "headers", tuple(self.headers))
        object.__setattr__(self, "rows", rows)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def first_data_row(self) -> int:
        return self.rows[0].source_row

    @property
    def last_data_row(self) -> int:
        return self.rows[-1].source_row

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(self.to_dict())).hexdigest()

    def source_locator(self, row: OfficialRuleManifestRow) -> str:
        if row not in self.rows:
            raise OfficialRuleCoverageError("row is not part of this manifest")
        escaped_sheet = self.sheet_name.replace("'", "''")
        return (
            f"{self.artifact_id}@sha256:{self.artifact_sha256}!"
            f"'{escaped_sheet}'!A{row.source_row}:H{row.source_row}#"
            f"row_sha256={row.row_sha256}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "registry_version": self.registry_version,
            "profile_id": self.profile_id,
            "rule_bundle_id": self.rule_bundle_id,
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
            "source_url": self.source_url,
            "sheet_name": self.sheet_name,
            "header_row": self.header_row,
            "headers": list(self.headers),
            "row_count": self.row_count,
            "first_data_row": self.first_data_row,
            "last_data_row": self.last_data_row,
            "rows": [row.to_dict() for row in self.rows],
        }

    def to_json(self) -> str:
        return _canonical_json_bytes(self.to_dict()).decode("ascii")


@dataclass(frozen=True, slots=True)
class CoverageInventoryVerification:
    manifest_sha256: str
    expected_row_count: int
    observed_row_count: int
    inventory_complete: bool
    execution_complete: bool
    blocker_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_sha256": self.manifest_sha256,
            "expected_row_count": self.expected_row_count,
            "observed_row_count": self.observed_row_count,
            "inventory_complete": self.inventory_complete,
            "execution_complete": self.execution_complete,
            "blocker_codes": list(self.blocker_codes),
        }


def assess_fhwa_v1_1_source(
    registry: RuleBundleRegistry,
    *,
    rule_bundle_id: str,
) -> OfficialRuleSourceReadiness:
    """Assess local import readiness without opening or downloading a workbook."""

    blockers: set[str] = set()
    artifact = None
    try:
        bundle = registry.bundle(rule_bundle_id)
    except (RegistryError, AttributeError, TypeError):
        bundle = None
        blockers.add("rule_bundle_missing")
    if bundle is not None and not bundle.enabled:
        blockers.add("rule_bundle_disabled")

    try:
        artifact = registry.artifact(FHWA_SNBI_VALIDATION_V1_1_ARTIFACT_ID)
    except (RegistryError, AttributeError, TypeError):
        blockers.add("validation_artifact_missing")

    if bundle is not None and artifact is not None:
        try:
            governed_ids = {
                item.id for item in registry.artifacts_for(rule_bundle_id)
            }
        except RegistryError:
            governed_ids = set()
        if artifact.id not in governed_ids:
            blockers.add("validation_artifact_not_in_bundle")

    if artifact is not None:
        if artifact.role != "validation_logic":
            blockers.add("validation_artifact_role_mismatch")
        if artifact.authority != "FHWA":
            blockers.add("validation_artifact_authority_mismatch")
        if artifact.source_url != FHWA_SNBI_VALIDATION_V1_1_SOURCE_URL:
            blockers.add("validation_artifact_source_url_mismatch")
        if artifact.media_type != FHWA_XLSX_MEDIA_TYPE:
            blockers.add("validation_artifact_media_type_mismatch")
        if artifact.state is not ArtifactState.VENDORED_VERIFIED:
            blockers.add("validation_artifact_not_vendored_verified")
        if not artifact.local_path:
            blockers.add("validation_artifact_local_path_missing")
        if not artifact.sha256 or not _SHA256_RE.fullmatch(artifact.sha256):
            blockers.add("validation_artifact_sha256_missing")

    blocker_codes = tuple(sorted(blockers))
    return OfficialRuleSourceReadiness(
        artifact_id=FHWA_SNBI_VALIDATION_V1_1_ARTIFACT_ID,
        bundle_id=rule_bundle_id,
        source_url=artifact.source_url if artifact is not None else None,
        artifact_state=(
            artifact.state.value if artifact is not None else None
        ),
        artifact_sha256=artifact.sha256 if artifact is not None else None,
        ready=not blocker_codes,
        blocker_codes=blocker_codes,
    )


def assess_registered_fhwa_v1_1_source(
    *,
    rule_bundle_id: str = "submission_2026_v04_v1_1",
    registry_path: Path = DEFAULT_REGISTRY_PATH,
) -> OfficialRuleSourceReadiness:
    """Assess the checked-in registry without downloading or opening XLSX data."""

    registry = load_rule_bundle_registry(registry_path)
    return assess_fhwa_v1_1_source(registry, rule_bundle_id=rule_bundle_id)


def import_fhwa_v1_1_workbook(
    workbook_path: Path,
    *,
    registry: RuleBundleRegistry,
    profile_id: str,
    rule_bundle_id: str,
    registry_version: str,
) -> OfficialRuleCoverageManifest:
    """Import exact v1.1 workbook bytes into a deterministic row manifest.

    The registry trust gate is evaluated before ``workbook_path`` is opened.
    This ordering prevents remote/unverified metadata from being converted into
    apparent official coverage merely because an arbitrary local XLSX exists.
    """

    readiness = assess_fhwa_v1_1_source(
        registry, rule_bundle_id=rule_bundle_id
    )
    if not readiness.ready:
        raise OfficialRuleCoverageError(
            "official validation workbook is not importable: "
            + ", ".join(readiness.blocker_codes)
        )
    artifact = registry.artifact(FHWA_SNBI_VALIDATION_V1_1_ARTIFACT_ID)
    expected_sha256 = artifact.sha256
    assert expected_sha256 is not None  # guaranteed by readiness

    path = Path(workbook_path)
    try:
        actual_sha256 = _sha256_file(path)
    except OSError as exc:
        raise OfficialRuleCoverageError(
            f"cannot read official validation workbook: {path}"
        ) from exc
    if actual_sha256 != expected_sha256:
        raise OfficialRuleCoverageError(
            "workbook SHA-256 does not match the verified registry artifact"
        )

    extracted_rows = _extract_rule_rows(path)
    manifest_rows = tuple(
        _manifest_row(source_row, cells)
        for source_row, cells in extracted_rows
    )
    return OfficialRuleCoverageManifest(
        registry_version=registry_version,
        profile_id=profile_id,
        rule_bundle_id=rule_bundle_id,
        artifact_id=artifact.id,
        artifact_sha256=expected_sha256,
        source_url=artifact.source_url or "",
        sheet_name=FHWA_SNBI_VALIDATION_V1_1_SHEET,
        header_row=FHWA_SNBI_VALIDATION_V1_1_HEADER_ROW,
        headers=FHWA_SNBI_VALIDATION_V1_1_HEADERS,
        rows=manifest_rows,
    )


def build_pending_validation_coverage(
    manifest: OfficialRuleCoverageManifest,
) -> CoverageRegistry:
    """Adapt an imported inventory to the layered validation orchestrator.

    Every row is present exactly once and deliberately marked unimplemented.
    """

    reason = PENDING_EXECUTION_REASON
    return CoverageRegistry(
        profile_id=manifest.profile_id,
        registry_version=manifest.registry_version,
        rows=tuple(
            CoverageRow(
                rule_id=row.coverage_id,
                status=CoverageStatus.UNIMPLEMENTED,
                source_reference=manifest.source_locator(row),
                reason=reason,
            )
            for row in manifest.rows
        ),
    )


def build_pending_official_rule_coverage(
    manifest: OfficialRuleCoverageManifest,
) -> OfficialRuleCoverageRegistry:
    """Adapt an imported inventory to ``rule_coverage.py`` fail-closed rows."""

    reason = PENDING_EXECUTION_REASON
    return OfficialRuleCoverageRegistry(
        registry_version=manifest.registry_version,
        profile_id=manifest.profile_id,
        rule_bundle_id=manifest.rule_bundle_id,
        declarations=tuple(
            RuleCoverageDeclaration(
                rule_id=row.coverage_id,
                rule_kind=RuleKind.VALIDATION,
                intent=CoverageIntent.PENDING_IMPLEMENTATION,
                # An empty RFC 6901 pointer denotes the whole document.  It is
                # deliberately non-specific until a reviewer maps this row.
                pointer_patterns=("",),
                artifact_id=manifest.artifact_id,
                artifact_sha256=manifest.artifact_sha256,
                source_locator=manifest.source_locator(row),
                reason=reason,
            )
            for row in manifest.rows
        ),
    )


def build_pending_reviewed_rule_inventory(
    manifest: OfficialRuleCoverageManifest,
    *,
    submission_blocker_codes: Sequence[str] = (),
) -> dict[str, Any]:
    """Build the checked-in human-review inventory for exact workbook rows.

    The inventory records source/layout acceptance separately from executable
    rule coverage.  It is intentionally impossible for this function to mark
    a row implemented: every imported row is emitted as ``unimplemented`` and
    ``pending_implementation`` with no test IDs.
    """

    layered = build_pending_validation_coverage(manifest)
    detailed = build_pending_official_rule_coverage(manifest)
    verification = verify_validation_coverage_inventory(manifest, layered)
    if not verification.inventory_complete or verification.execution_complete:
        raise OfficialRuleCoverageError(
            "pending reviewed inventory must be complete and unexecuted"
        )
    declarations = {row.rule_id: row for row in detailed.declarations}
    coverage_rows = {row.rule_id: row for row in layered.rows}
    blockers = tuple(
        sorted(
            {
                _identifier(value, "submission blocker")
                for value in submission_blocker_codes
            }
        )
    )
    return {
        "format_version": 1,
        "registry_version": manifest.registry_version,
        "profile_id": manifest.profile_id,
        "rule_bundle_id": manifest.rule_bundle_id,
        "artifact_id": manifest.artifact_id,
        "artifact_sha256": manifest.artifact_sha256,
        "source_url": manifest.source_url,
        "sheet_name": manifest.sheet_name,
        "header_row": manifest.header_row,
        "row_count": manifest.row_count,
        "source_manifest_sha256": manifest.sha256,
        "coverage_registry_sha256": layered.sha256,
        "official_coverage_registry_sha256": detailed.sha256,
        "source_bytes_verified": True,
        "source_layout_reviewed": True,
        "inventory_complete": True,
        "execution_complete": False,
        "official_validation": False,
        "submission_ready": False,
        "coverage_blocker_codes": list(verification.blocker_codes),
        "submission_blocker_codes": list(blockers),
        "authority_boundary": (
            "Exact FHWA source bytes and the reviewed A:H row layout are "
            "verified. Rule meaning, executable implementation, FHWA/NBTIS "
            "validation, and submission acceptance are not verified."
        ),
        "rows": [
            {
                **row.to_dict(),
                "source_reference": coverage_rows[
                    row.coverage_id
                ].source_reference,
                "coverage_status": coverage_rows[row.coverage_id].status.value,
                "coverage_intent": declarations[row.coverage_id].intent.value,
                "pointer_patterns": list(
                    declarations[row.coverage_id].pointer_patterns
                ),
                "test_ids": list(declarations[row.coverage_id].test_ids),
                "reason": PENDING_EXECUTION_REASON,
            }
            for row in manifest.rows
        ],
    }


def verify_validation_coverage_inventory(
    manifest: OfficialRuleCoverageManifest,
    coverage: CoverageRegistry,
) -> CoverageInventoryVerification:
    """Verify one-to-one accounting separately from executable completeness."""

    blockers: set[str] = set()
    if coverage.profile_id != manifest.profile_id:
        blockers.add("coverage_profile_mismatch")
    if coverage.registry_version != manifest.registry_version:
        blockers.add("coverage_registry_version_mismatch")

    expected = {row.coverage_id: row for row in manifest.rows}
    observed: dict[str, CoverageRow] = {}
    duplicate_ids: set[str] = set()
    for row in coverage.rows:
        if row.rule_id in observed:
            duplicate_ids.add(row.rule_id)
        else:
            observed[row.rule_id] = row
    if duplicate_ids:
        blockers.add("duplicate_coverage_rows")
    if set(expected) - set(observed):
        blockers.add("missing_coverage_rows")
    if set(observed) - set(expected):
        blockers.add("unknown_coverage_rows")
    for coverage_id in sorted(set(expected).intersection(observed)):
        expected_source = manifest.source_locator(expected[coverage_id])
        if observed[coverage_id].source_reference != expected_source:
            blockers.add("coverage_source_reference_mismatch")

    inventory_blockers = {
        "coverage_profile_mismatch",
        "coverage_registry_version_mismatch",
        "duplicate_coverage_rows",
        "missing_coverage_rows",
        "unknown_coverage_rows",
        "coverage_source_reference_mismatch",
    }
    inventory_complete = not blockers.intersection(inventory_blockers)
    if any(
        row.status is CoverageStatus.UNIMPLEMENTED
        for row in coverage.rows
    ):
        blockers.add("unimplemented_official_rules")
    execution_complete = inventory_complete and (
        "unimplemented_official_rules" not in blockers
    )
    return CoverageInventoryVerification(
        manifest_sha256=manifest.sha256,
        expected_row_count=len(manifest.rows),
        observed_row_count=len(coverage.rows),
        inventory_complete=inventory_complete,
        execution_complete=execution_complete,
        blocker_codes=tuple(sorted(blockers)),
    )


def manifest_from_dict(payload: Mapping[str, Any]) -> OfficialRuleCoverageManifest:
    """Strictly load a manifest; unknown or inconsistent fields are rejected."""

    root = _strict_mapping(
        payload,
        {
            "format_version",
            "registry_version",
            "profile_id",
            "rule_bundle_id",
            "artifact_id",
            "artifact_sha256",
            "source_url",
            "sheet_name",
            "header_row",
            "headers",
            "row_count",
            "first_data_row",
            "last_data_row",
            "rows",
        },
        "manifest",
    )
    raw_rows = root["rows"]
    if not isinstance(raw_rows, list):
        raise OfficialRuleCoverageError("manifest.rows must be an array")
    rows = []
    for index, raw_row in enumerate(raw_rows):
        item = _strict_mapping(
            raw_row,
            {
                "source_row",
                "coverage_id",
                "row_sha256",
                "error_type",
                "snbi_id",
                "data_tag",
                "rule_name",
            },
            f"manifest.rows[{index}]",
        )
        rows.append(OfficialRuleManifestRow(**item))
    manifest = OfficialRuleCoverageManifest(
        format_version=root["format_version"],
        registry_version=root["registry_version"],
        profile_id=root["profile_id"],
        rule_bundle_id=root["rule_bundle_id"],
        artifact_id=root["artifact_id"],
        artifact_sha256=root["artifact_sha256"],
        source_url=root["source_url"],
        sheet_name=root["sheet_name"],
        header_row=root["header_row"],
        headers=tuple(root["headers"]),
        rows=tuple(rows),
    )
    if root["row_count"] != manifest.row_count:
        raise OfficialRuleCoverageError("manifest row_count is inconsistent")
    if root["first_data_row"] != manifest.first_data_row:
        raise OfficialRuleCoverageError("manifest first_data_row is inconsistent")
    if root["last_data_row"] != manifest.last_data_row:
        raise OfficialRuleCoverageError("manifest last_data_row is inconsistent")
    return manifest


def manifest_from_json(raw: str | bytes) -> OfficialRuleCoverageManifest:
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OfficialRuleCoverageError("manifest is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise OfficialRuleCoverageError("manifest root must be an object")
    return manifest_from_dict(payload)


def _manifest_row(
    source_row: int,
    cells: tuple[str, ...],
) -> OfficialRuleManifestRow:
    row_sha256 = hashlib.sha256(
        _canonical_json_bytes(
            {
                "headers": list(FHWA_SNBI_VALIDATION_V1_1_HEADERS),
                "cells": list(cells),
            }
        )
    ).hexdigest()
    return OfficialRuleManifestRow(
        source_row=source_row,
        coverage_id=_coverage_id(source_row, row_sha256),
        row_sha256=row_sha256,
        error_type=cells[0],
        snbi_id=cells[1],
        data_tag=cells[2],
        rule_name=cells[3],
    )


def _extract_rule_rows(path: Path) -> tuple[tuple[int, tuple[str, ...]], ...]:
    try:
        archive = zipfile.ZipFile(path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise OfficialRuleCoverageError("validation artifact is not a valid XLSX file") from exc
    with archive:
        _validate_zip(archive)
        workbook_root = _xml_root(_read_member(archive, "xl/workbook.xml"), "workbook")
        relationships_root = _xml_root(
            _read_member(archive, "xl/_rels/workbook.xml.rels"),
            "workbook relationships",
        )
        relationship_target = _sheet_relationship_target(
            workbook_root,
            relationships_root,
            FHWA_SNBI_VALIDATION_V1_1_SHEET,
        )
        sheet_path = _resolve_xlsx_target(relationship_target)
        shared_strings = _shared_strings(archive)
        sheet_root = _xml_root(_read_member(archive, sheet_path), "worksheet")
        cells_by_row = _worksheet_cells(sheet_root, shared_strings)

    header_cells = cells_by_row.get(FHWA_SNBI_VALIDATION_V1_1_HEADER_ROW, {})
    _reject_populated_columns_after_expected(header_cells, context="header")
    observed_headers = tuple(
        header_cells.get(index, "")
        for index in range(len(FHWA_SNBI_VALIDATION_V1_1_HEADERS))
    )
    if observed_headers != FHWA_SNBI_VALIDATION_V1_1_HEADERS:
        raise OfficialRuleCoverageError(
            "workbook header row does not match the reviewed v1.1 layout"
        )

    populated_data_rows = sorted(
        row_number
        for row_number, cells in cells_by_row.items()
        if row_number > FHWA_SNBI_VALIDATION_V1_1_HEADER_ROW
        and any(value != "" for value in cells.values())
    )
    if not populated_data_rows:
        raise OfficialRuleCoverageError("workbook contains no validation rule rows")
    first_expected = FHWA_SNBI_VALIDATION_V1_1_HEADER_ROW + 1
    last_row = populated_data_rows[-1]
    if last_row - first_expected + 1 > _MAX_RULE_ROWS:
        raise OfficialRuleCoverageError("validation workbook exceeds rule-row limit")

    output = []
    for row_number in range(first_expected, last_row + 1):
        cells = cells_by_row.get(row_number, {})
        _reject_populated_columns_after_expected(
            cells, context=f"data row {row_number}"
        )
        values = tuple(
            cells.get(index, "")
            for index in range(len(FHWA_SNBI_VALIDATION_V1_1_HEADERS))
        )
        if not any(value != "" for value in values):
            raise OfficialRuleCoverageError(
                f"blank row {row_number} interrupts the official rule inventory"
            )
        output.append((row_number, values))
    return tuple(output)


def _validate_zip(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    if len(infos) > _MAX_ZIP_MEMBERS:
        raise OfficialRuleCoverageError("XLSX archive contains too many members")
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise OfficialRuleCoverageError("XLSX archive contains duplicate member names")
    total = 0
    for info in infos:
        if info.flag_bits & 0x1:
            raise OfficialRuleCoverageError("encrypted XLSX members are not supported")
        if "\\" in info.filename:
            raise OfficialRuleCoverageError("unsafe XLSX member path")
        path = PurePosixPath(info.filename)
        if path.is_absolute() or ".." in path.parts:
            raise OfficialRuleCoverageError("unsafe XLSX member path")
        total += info.file_size
        if info.file_size > _MAX_XML_MEMBER_BYTES:
            raise OfficialRuleCoverageError("XLSX member exceeds size limit")
        if (
            info.compress_size > 0
            and info.file_size > 1024 * 1024
            and info.file_size / info.compress_size > 1000
        ):
            raise OfficialRuleCoverageError("XLSX member compression ratio is unsafe")
    if total > _MAX_ZIP_UNCOMPRESSED_BYTES:
        raise OfficialRuleCoverageError("XLSX archive exceeds uncompressed size limit")


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise OfficialRuleCoverageError(f"XLSX is missing required member {name}") from exc
    if info.file_size > _MAX_XML_MEMBER_BYTES:
        raise OfficialRuleCoverageError(f"XLSX member {name} exceeds size limit")
    try:
        return archive.read(info)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise OfficialRuleCoverageError(f"cannot read XLSX member {name}") from exc


def _xml_root(raw: bytes, context: str) -> ElementTree.Element:
    lowered = raw[:4096].lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise OfficialRuleCoverageError(f"{context} XML contains forbidden declarations")
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise OfficialRuleCoverageError(f"invalid {context} XML") from exc


def _sheet_relationship_target(
    workbook_root: ElementTree.Element,
    relationships_root: ElementTree.Element,
    sheet_name: str,
) -> str:
    matching = [
        element
        for element in workbook_root.iter()
        if _local_name(element.tag) == "sheet"
        and element.attrib.get("name") == sheet_name
    ]
    if len(matching) != 1:
        raise OfficialRuleCoverageError(
            "workbook must contain exactly one reviewed validation sheet"
        )
    relationship_id = next(
        (
            value
            for key, value in matching[0].attrib.items()
            if _local_name(key) == "id"
        ),
        None,
    )
    if not relationship_id:
        raise OfficialRuleCoverageError("validation sheet has no relationship ID")
    relationships = [
        element
        for element in relationships_root.iter()
        if _local_name(element.tag) == "Relationship"
        and element.attrib.get("Id") == relationship_id
    ]
    if len(relationships) != 1:
        raise OfficialRuleCoverageError("validation sheet relationship is ambiguous")
    relationship = relationships[0]
    if relationship.attrib.get("TargetMode") == "External":
        raise OfficialRuleCoverageError("external worksheet relationships are forbidden")
    target = relationship.attrib.get("Target")
    if not target:
        raise OfficialRuleCoverageError("validation sheet relationship has no target")
    return target


def _resolve_xlsx_target(target: str) -> str:
    if "\\" in target:
        raise OfficialRuleCoverageError("unsafe worksheet relationship target")
    if target.startswith("/"):
        candidate = PurePosixPath(target[1:])
    else:
        candidate = PurePosixPath("xl") / PurePosixPath(target)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise OfficialRuleCoverageError("unsafe worksheet relationship target")
    if not candidate.parts or candidate.parts[0] != "xl":
        raise OfficialRuleCoverageError("worksheet target must remain below xl/")
    return candidate.as_posix()


def _shared_strings(archive: zipfile.ZipFile) -> tuple[str, ...]:
    try:
        raw = _read_member(archive, "xl/sharedStrings.xml")
    except OfficialRuleCoverageError as exc:
        if "missing required member" in str(exc):
            return ()
        raise
    root = _xml_root(raw, "shared strings")
    output = []
    for element in root:
        if _local_name(element.tag) != "si":
            continue
        text = "".join(
            node.text or ""
            for node in element.iter()
            if _local_name(node.tag) == "t"
        )
        output.append(_normalize_cell_text(text))
    return tuple(output)


def _worksheet_cells(
    root: ElementTree.Element,
    shared_strings: tuple[str, ...],
) -> dict[int, dict[int, str]]:
    rows: dict[int, dict[int, str]] = {}
    for row_element in root.iter():
        if _local_name(row_element.tag) != "row":
            continue
        raw_row = row_element.attrib.get("r")
        try:
            row_number = int(raw_row or "")
        except ValueError as exc:
            raise OfficialRuleCoverageError("worksheet row is missing a numeric index") from exc
        if row_number in rows:
            raise OfficialRuleCoverageError(f"duplicate worksheet row {row_number}")
        cells: dict[int, str] = {}
        for cell in row_element:
            if _local_name(cell.tag) != "c":
                continue
            reference = cell.attrib.get("r")
            if not reference:
                raise OfficialRuleCoverageError("worksheet cell has no reference")
            match = _CELL_REFERENCE_RE.fullmatch(reference)
            if not match or int(match.group(2)) != row_number:
                raise OfficialRuleCoverageError(
                    f"worksheet cell reference {reference!r} is inconsistent"
                )
            column = _column_index(match.group(1))
            if column in cells:
                raise OfficialRuleCoverageError(
                    f"duplicate worksheet cell {reference}"
                )
            cells[column] = _cell_value(cell, shared_strings, reference)
        rows[row_number] = cells
    return rows


def _cell_value(
    cell: ElementTree.Element,
    shared_strings: tuple[str, ...],
    reference: str,
) -> str:
    children = list(cell)
    if any(_local_name(child.tag) == "f" for child in children):
        raise OfficialRuleCoverageError(
            f"formula cell {reference} is not valid immutable rule evidence"
        )
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        text = "".join(
            node.text or ""
            for node in cell.iter()
            if _local_name(node.tag) == "t"
        )
        return _normalize_cell_text(text)
    value_element = next(
        (child for child in children if _local_name(child.tag) == "v"),
        None,
    )
    raw = value_element.text if value_element is not None else ""
    raw = raw or ""
    if cell_type == "s":
        try:
            index = int(raw)
            return shared_strings[index]
        except (ValueError, IndexError) as exc:
            raise OfficialRuleCoverageError(
                f"invalid shared-string index in cell {reference}"
            ) from exc
    if cell_type == "e":
        raise OfficialRuleCoverageError(
            f"error cell {reference} is not valid rule evidence"
        )
    if cell_type not in {None, "n", "str", "b", "d"}:
        raise OfficialRuleCoverageError(
            f"unsupported XLSX cell type {cell_type!r} in {reference}"
        )
    return _normalize_cell_text(raw)


def _reject_populated_columns_after_expected(
    cells: Mapping[int, str],
    *,
    context: str,
) -> None:
    limit = len(FHWA_SNBI_VALIDATION_V1_1_HEADERS)
    if any(index >= limit and value != "" for index, value in cells.items()):
        raise OfficialRuleCoverageError(
            f"{context} contains populated columns outside the reviewed A:H layout"
        )


def _column_index(letters: str) -> int:
    value = 0
    for character in letters:
        value = value * 26 + (ord(character) - ord("A") + 1)
    return value - 1


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _normalize_cell_text(value: str) -> str:
    return unicodedata.normalize(
        "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
    )


def _coverage_id(source_row: int, row_sha256: str) -> str:
    return f"fhwa_snbi_validation_v1_1_r{source_row:04d}_{row_sha256[:12]}"


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise OfficialRuleCoverageError(
            f"{name} must be 64 lowercase hexadecimal characters"
        )
    return value


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise OfficialRuleCoverageError(f"{name} must be an identifier")
    return value


def _strict_mapping(
    value: Any,
    keys: set[str],
    context: str,
) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise OfficialRuleCoverageError(f"{context} must be an object")
    missing = sorted(keys - set(value))
    unknown = sorted(set(value) - keys)
    if missing:
        raise OfficialRuleCoverageError(
            f"{context} is missing keys: {', '.join(missing)}"
        )
    if unknown:
        raise OfficialRuleCoverageError(
            f"{context} has unknown keys: {', '.join(unknown)}"
        )
    return value
