from __future__ import annotations

import hashlib
import html
import io
import json
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from app.services.conversion.official_rule_coverage import (
    FHWA_SNBI_VALIDATION_V1_1_ARTIFACT_ID,
    FHWA_SNBI_VALIDATION_V1_1_HEADERS,
    FHWA_SNBI_VALIDATION_V1_1_SOURCE_URL,
    FHWA_XLSX_MEDIA_TYPE,
    OfficialRuleCoverageError,
    assess_registered_fhwa_v1_1_source,
    assess_fhwa_v1_1_source,
    build_pending_official_rule_coverage,
    build_pending_reviewed_rule_inventory,
    build_pending_validation_coverage,
    import_fhwa_v1_1_workbook,
    manifest_from_json,
    verify_validation_coverage_inventory,
)
from app.services.conversion.rule_coverage import CoverageIntent, RuleKind
from app.services.conversion.pinned_schema import (
    SchemaExecutionStatus,
    execute_pinned_schema,
)
from app.services.conversion.validation_profiles import (
    CoverageRegistry,
    CoverageRow,
    CoverageStatus,
)
from app.standards.fhwa_registry import (
    DEFAULT_REGISTRY_PATH,
    RuleBundleNotReadyError,
    load_rule_bundle_registry,
)


PROFILE_ID = "submission_2026_test"
BUNDLE_ID = "submission_2026_test_bundle"
REGISTRY_VERSION = "fhwa_snbi_validation_v1_1_test"

RULE_ROWS = (
    (
        "Error",
        "BID01",
        "BID01",
        "Bridge ID required",
        "Bridge Number",
        "Bridge ID is required.",
        "Supply the bridge identifier.",
        "Text",
    ),
    (
        "Error",
        "BW01",
        "BW01",
        "Year built range",
        "Year Built",
        "Year built is outside the permitted range.",
        "Use a four-digit year.",
        "1900 through current year",
    ),
)


def _xlsx_bytes(
    rows=RULE_ROWS,
    *,
    header=FHWA_SNBI_VALIDATION_V1_1_HEADERS,
    row_numbers: tuple[int, ...] | None = None,
    formula_cell: str | None = None,
    extra_i_value: str | None = None,
) -> bytes:
    row_numbers = row_numbers or tuple(range(10, 10 + len(rows)))
    strings: list[str] = []
    string_index: dict[str, int] = {}

    def shared_index(value: str) -> int:
        if value not in string_index:
            string_index[value] = len(strings)
            strings.append(value)
        return string_index[value]

    def cell(reference: str, value: str) -> str:
        if formula_cell == reference:
            return f'<c r="{reference}"><f>1+1</f><v>2</v></c>'
        index = shared_index(value)
        return f'<c r="{reference}" t="s"><v>{index}</v></c>'

    xml_rows = []
    header_cells = "".join(
        cell(f"{chr(ord('A') + index)}9", value)
        for index, value in enumerate(header)
    )
    xml_rows.append(f'<row r="9">{header_cells}</row>')
    for row_number, values in zip(row_numbers, rows):
        cells = "".join(
            cell(f"{chr(ord('A') + index)}{row_number}", value)
            for index, value in enumerate(values)
        )
        if extra_i_value is not None:
            cells += cell(f"I{row_number}", extra_i_value)
        xml_rows.append(f'<row r="{row_number}">{cells}</row>')

    shared_strings = "".join(
        f"<si><t>{html.escape(value)}</t></si>" for value in strings
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="SNBI Data Validation Rules" sheetId="1" '
        'r:id="rId1"/></sheets></workbook>'
    )
    relationships_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    )
    worksheet_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(xml_rows)}</sheetData></worksheet>"
    )
    shared_strings_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{len(strings)}" uniqueCount="{len(strings)}">'
        f"{shared_strings}</sst>"
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships_xml)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet_xml)
        archive.writestr("xl/sharedStrings.xml", shared_strings_xml)
    return output.getvalue()


def _registry(
    workspace: Path,
    workbook_bytes: bytes,
    *,
    verified: bool = True,
    source_url: str = FHWA_SNBI_VALIDATION_V1_1_SOURCE_URL,
    media_type: str = FHWA_XLSX_MEDIA_TYPE,
):
    workspace.mkdir(parents=True, exist_ok=True)
    workbook_path = workspace / "SNBI_Data_Validation_Rules.xlsx"
    workbook_path.write_bytes(workbook_bytes)
    digest = hashlib.sha256(workbook_bytes).hexdigest()
    artifact = {
        "id": FHWA_SNBI_VALIDATION_V1_1_ARTIFACT_ID,
        "title": "SNBI Data Submittal Validation Logic version 1.1",
        "role": "validation_logic",
        "authority": "FHWA",
        "legal_status": "operational",
        "state": "vendored_verified" if verified else "remote_unverified",
        "source_url": source_url,
        "published_on": None,
        "published_label": "Version 1.1",
        "updated_on": "2025-12-30",
        "media_type": media_type,
        "local_path": workbook_path.name if verified else None,
        "sha256": digest if verified else None,
        "notes": ["Test fixture."],
    }
    registry_payload = {
        "format_version": 1,
        "registry_id": "official_rule_coverage_test",
        "observed_on": "2026-08-13",
        "artifacts": [artifact],
        "bundles": [
            {
                "id": BUNDLE_ID,
                "title": "Test submission bundle",
                "profile_kind": "submission",
                "legal_status": "operational_submission",
                "compliance_use": (
                    "submission_support_draft_external_verification_required"
                ),
                "implementation_status": "registry_only",
                "enabled": True,
                "effective_submission_years": [2026],
                "artifact_ids": [FHWA_SNBI_VALIDATION_V1_1_ARTIFACT_ID],
                "inherits": [],
                "disabled_reason": None,
                "notes": [],
            }
        ],
    }
    registry_path = workspace / "registry.json"
    registry_path.write_text(json.dumps(registry_payload), encoding="utf-8")
    return load_rule_bundle_registry(registry_path), workbook_path


def _import(workspace: Path, workbook_bytes: bytes = b""):
    payload = workbook_bytes or _xlsx_bytes()
    registry, workbook_path = _registry(workspace, payload)
    manifest = import_fhwa_v1_1_workbook(
        workbook_path,
        registry=registry,
        profile_id=PROFILE_ID,
        rule_bundle_id=BUNDLE_ID,
        registry_version=REGISTRY_VERSION,
    )
    return manifest, registry, workbook_path


def test_remote_unverified_workbook_fails_before_opening_candidate(
    test_workspace: Path,
):
    registry, _ = _registry(test_workspace, _xlsx_bytes(), verified=False)
    readiness = assess_fhwa_v1_1_source(registry, rule_bundle_id=BUNDLE_ID)

    assert readiness.ready is False
    assert "validation_artifact_not_vendored_verified" in readiness.blocker_codes
    assert "validation_artifact_local_path_missing" in readiness.blocker_codes
    assert "validation_artifact_sha256_missing" in readiness.blocker_codes
    with pytest.raises(OfficialRuleCoverageError, match="not_vendored_verified"):
        import_fhwa_v1_1_workbook(
            test_workspace / "candidate-that-does-not-exist.xlsx",
            registry=registry,
            profile_id=PROFILE_ID,
            rule_bundle_id=BUNDLE_ID,
            registry_version=REGISTRY_VERSION,
        )


def test_checked_in_registry_reports_official_workbook_source_as_ready():
    readiness = assess_registered_fhwa_v1_1_source()

    assert readiness.ready is True
    assert readiness.artifact_id == FHWA_SNBI_VALIDATION_V1_1_ARTIFACT_ID
    assert readiness.artifact_sha256 == (
        "765776e38facb1e42a528230342317b261ba47e64676ecde418f57ee7d0e2232"
    )
    assert readiness.blocker_codes == ()


def test_repository_validation_source_is_exactly_vendored_and_importable():
    registry = load_rule_bundle_registry()
    artifact = registry.artifact(FHWA_SNBI_VALIDATION_V1_1_ARTIFACT_ID)
    readiness = assess_fhwa_v1_1_source(
        registry, rule_bundle_id="submission_2026_v04_v1_1"
    )

    assert readiness.ready is True
    assert readiness.blocker_codes == ()
    assert artifact.local_path == "artifacts/SNBI.Data.Validation.Rules.v1.1.xlsx"
    assert artifact.sha256 == (
        "765776e38facb1e42a528230342317b261ba47e64676ecde418f57ee7d0e2232"
    )
    assert DEFAULT_REGISTRY_PATH.is_file()


def test_checked_in_reviewed_inventory_is_deterministic_complete_and_pending():
    registry = load_rule_bundle_registry()
    artifact = registry.artifact(FHWA_SNBI_VALIDATION_V1_1_ARTIFACT_ID)
    workbook = DEFAULT_REGISTRY_PATH.parent / str(artifact.local_path)
    manifest = import_fhwa_v1_1_workbook(
        workbook,
        registry=registry,
        profile_id="submission_2026_v04_v1_1",
        rule_bundle_id="submission_2026_v04_v1_1",
        registry_version="fhwa_registry_2026_08_13_validation_v1_1",
    )
    expected = build_pending_reviewed_rule_inventory(
        manifest,
        submission_blocker_codes=(
            "unimplemented_official_rules",
            "unsupported_schema_dialect",
            "unverified_governing_artifacts",
            "external_fhwa_nbtis_verification_required",
        ),
    )
    inventory_path = (
        DEFAULT_REGISTRY_PATH.parent / "derived" / "reviewed-rule-coverage.json"
    )
    checked_in = json.loads(inventory_path.read_text(encoding="utf-8"))

    assert checked_in == expected
    assert checked_in["row_count"] == 382
    assert checked_in["source_manifest_sha256"] == (
        "52ec324db607b1703c688242fb54a2f37d765c175f0274bc525502b4c2b119d5"
    )
    assert checked_in["inventory_complete"] is True
    assert checked_in["execution_complete"] is False
    assert checked_in["official_validation"] is False
    assert checked_in["submission_ready"] is False
    assert all(row["coverage_status"] == "unimplemented" for row in checked_in["rows"])
    assert all(
        row["coverage_intent"] == "pending_implementation"
        and row["pointer_patterns"] == [""]
        and row["test_ids"] == []
        for row in checked_in["rows"]
    )


def test_submission_profile_stays_fail_closed_after_source_byte_acceptance():
    registry = load_rule_bundle_registry()

    with pytest.raises(RuleBundleNotReadyError):
        registry.select_submission_year(2026)
    assert set(registry.unverified_artifact_ids("submission_2026_v04_v1_1")) == {
        "ecfr_23_cfr_650_317",
        "implementation_memo_may_2022",
        "snbi_errata1_march_2024",
        "submission_updates_memo_sep_2024",
    }

    schema = execute_pinned_schema(
        {},
        registry_path=DEFAULT_REGISTRY_PATH,
        bundle_id="submission_2026_v04_v1_1",
        artifact_id="snbi_schema_v04_july_2024",
    )
    assert schema.status is SchemaExecutionStatus.UNSUPPORTED_DIALECT
    assert schema.schema_executed is False


def test_import_is_deterministic_and_accounts_for_each_physical_row(
    test_workspace: Path,
):
    manifest, registry, workbook_path = _import(test_workspace)
    repeated = import_fhwa_v1_1_workbook(
        workbook_path,
        registry=registry,
        profile_id=PROFILE_ID,
        rule_bundle_id=BUNDLE_ID,
        registry_version=REGISTRY_VERSION,
    )

    assert manifest.row_count == 2
    assert (manifest.first_data_row, manifest.last_data_row) == (10, 11)
    assert [row.source_row for row in manifest.rows] == [10, 11]
    assert len({row.coverage_id for row in manifest.rows}) == 2
    assert manifest.to_json() == repeated.to_json()
    assert manifest.sha256 == repeated.sha256
    assert manifest_from_json(manifest.to_json()) == manifest


def test_imported_rows_are_pending_in_both_existing_coverage_models(
    test_workspace: Path,
):
    manifest, _, _ = _import(test_workspace)

    layered = build_pending_validation_coverage(manifest)
    detailed = build_pending_official_rule_coverage(manifest)
    verification = verify_validation_coverage_inventory(manifest, layered)

    assert [row.rule_id for row in layered.rows] == [
        row.coverage_id for row in manifest.rows
    ]
    assert all(row.status is CoverageStatus.UNIMPLEMENTED for row in layered.rows)
    assert all("row_sha256=" in row.source_reference for row in layered.rows)
    assert all(
        row.rule_kind is RuleKind.VALIDATION
        and row.intent is CoverageIntent.PENDING_IMPLEMENTATION
        and row.artifact_sha256 == manifest.artifact_sha256
        for row in detailed.declarations
    )
    assert verification.inventory_complete is True
    assert verification.execution_complete is False
    assert verification.blocker_codes == ("unimplemented_official_rules",)


def test_inventory_verifier_detects_missing_unknown_and_changed_source(
    test_workspace: Path,
):
    manifest, _, _ = _import(test_workspace)
    pending = build_pending_validation_coverage(manifest)
    changed_first = replace(pending.rows[0], source_reference="wrong source")
    unknown = CoverageRow(
        rule_id="not_an_official_inventory_row",
        status=CoverageStatus.UNIMPLEMENTED,
        source_reference="unknown",
        reason="Not reviewed.",
    )
    incomplete = CoverageRegistry(
        profile_id=pending.profile_id,
        registry_version=pending.registry_version,
        rows=(changed_first, unknown),
    )

    verification = verify_validation_coverage_inventory(manifest, incomplete)

    assert verification.inventory_complete is False
    assert verification.execution_complete is False
    assert set(verification.blocker_codes) == {
        "coverage_source_reference_mismatch",
        "missing_coverage_rows",
        "unknown_coverage_rows",
        "unimplemented_official_rules",
    }


def test_row_content_change_changes_row_identity_and_manifest_hash(
    test_workspace: Path,
):
    first, _, _ = _import(test_workspace / "first", _xlsx_bytes())
    changed_rows = list(RULE_ROWS)
    changed_rows[1] = tuple(
        "Use a reviewed year range." if index == 6 else value
        for index, value in enumerate(changed_rows[1])
    )
    second, _, _ = _import(
        test_workspace / "second", _xlsx_bytes(tuple(changed_rows))
    )

    assert first.rows[0] == second.rows[0]
    assert first.rows[1].coverage_id != second.rows[1].coverage_id
    assert first.rows[1].row_sha256 != second.rows[1].row_sha256
    assert first.sha256 != second.sha256


@pytest.mark.parametrize(
    ("workbook_bytes", "message"),
    [
        (
            _xlsx_bytes(row_numbers=(10, 12)),
            "blank row 11 interrupts",
        ),
        (
            _xlsx_bytes(formula_cell="D10"),
            "formula cell D10",
        ),
        (
            _xlsx_bytes(extra_i_value="silently ignored rule text"),
            "outside the reviewed A:H layout",
        ),
        (
            _xlsx_bytes(
                header=("Wrong Header",) + FHWA_SNBI_VALIDATION_V1_1_HEADERS[1:]
            ),
            "header row does not match",
        ),
    ],
)
def test_ambiguous_or_lossy_workbook_shapes_fail_closed(
    test_workspace: Path,
    workbook_bytes: bytes,
    message: str,
):
    registry, workbook_path = _registry(test_workspace, workbook_bytes)
    with pytest.raises(OfficialRuleCoverageError, match=message):
        import_fhwa_v1_1_workbook(
            workbook_path,
            registry=registry,
            profile_id=PROFILE_ID,
            rule_bundle_id=BUNDLE_ID,
            registry_version=REGISTRY_VERSION,
        )


def test_import_rejects_candidate_bytes_not_named_by_registry(
    test_workspace: Path,
):
    registry, _ = _registry(test_workspace, _xlsx_bytes())
    candidate = test_workspace / "different.xlsx"
    candidate.write_bytes(_xlsx_bytes((RULE_ROWS[0],)))

    with pytest.raises(OfficialRuleCoverageError, match="SHA-256"):
        import_fhwa_v1_1_workbook(
            candidate,
            registry=registry,
            profile_id=PROFILE_ID,
            rule_bundle_id=BUNDLE_ID,
            registry_version=REGISTRY_VERSION,
        )


def test_source_url_and_media_type_are_exact_readiness_gates(
    test_workspace: Path,
):
    registry, _ = _registry(
        test_workspace,
        _xlsx_bytes(),
        verified=False,
        source_url="https://www.fhwa.dot.gov/bridge/snbi/data_validation_logic.cfm",
        media_type="text/html",
    )

    readiness = assess_fhwa_v1_1_source(registry, rule_bundle_id=BUNDLE_ID)

    assert "validation_artifact_source_url_mismatch" in readiness.blocker_codes
    assert "validation_artifact_media_type_mismatch" in readiness.blocker_codes
