"""Red-first PR6 contract for deterministic exports and reverse projection.

Locked service API
==================

Production should implement ``app.services.conversion.export_service`` with
the frozen ``RevisionExportRequest``/``RevisionExport``/``ExportArtifact``
values, ``ExportServiceError``, and ``export_snapshot_revision`` imported
below.  The service is deliberately revision-scoped: callers do not submit an
arbitrary browser document and cannot select a workspace through query data.

The four supported ``export_kind`` values are:

* ``canonical_profile`` -- deterministic canonical SNBI JSON plus manifest;
* ``restore_original`` -- byte-for-byte immutable legacy source recovery;
* ``legacy_projection`` -- explicitly lossy, headered CSV plus loss report;
* ``human_report`` -- a review-only plain-text report.

``exported_at`` is request evidence rather than an internal clock read.  Given
the same immutable revision and request, every artifact byte, SHA-256, export
identifier, filename, and manifest member must therefore be identical.

The suite creates only isolated test databases and artifact roots supplied by
``conftest``.  It never opens a production database.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from app.routers.services.database import ensure_schema
from app.services.conversion.revision_store import (
    CanonicalRevisionInput,
    PublishSnapshotRevisionRequest,
    StageCanonicalBatchRequest,
    ensure_workspace,
    get_workspace_state,
    initialize_revision_store,
    publish_snapshot_revision,
    stage_canonical_batch,
)
from app.services.conversion.source_artifacts import SourceArtifactStore
from app.services.spreadsheet_csv import spreadsheet_safe_value


try:
    from app.services.conversion.export_service import (
        ExportArtifact,
        ExportServiceError,
        RevisionExport,
        RevisionExportRequest,
        _csv_bytes,
        export_snapshot_revision,
    )

    _EXPORT_IMPORT_ERROR: BaseException | None = None
except (ImportError, ModuleNotFoundError) as exc:  # Expected initial PR6 state.
    ExportArtifact = None  # type: ignore[assignment,misc]
    ExportServiceError = RuntimeError  # type: ignore[assignment,misc]
    RevisionExport = None  # type: ignore[assignment,misc]
    RevisionExportRequest = None  # type: ignore[assignment,misc]
    export_snapshot_revision = None  # type: ignore[assignment]
    _EXPORT_IMPORT_ERROR = exc


WORKSPACE = "njdot-research"
STATE = 34
YEAR = 2024
BRIDGE = "EXPORT-0001"
AGENCY = "NJDOT"
ACTOR = "editor@dot.nj.gov"
EXPORTED_AT = "2026-08-13T15:30:00.000Z"
ORIGINAL_FILENAME = "NJDOT_2024_NBI.csv"
ORIGINAL_BYTES = (
    b"STATE_CODE_001,STRUCTURE_NUMBER_008,RECORD_TYPE_005A\r\n"
    b"342,EXPORT-0001,1\r\n"
    b"342,EXPORT-0001,2\r\n"
)

LOSS_CATEGORIES = {
    "exact",
    "within_numeric_tolerance",
    "representative_inverse_chosen",
    "structurally_collapsed",
    "omitted_no_legacy_representation",
    "generated_application_value",
    "unavailable_missing_provenance",
}


def _require_export_api() -> None:
    if _EXPORT_IMPORT_ERROR is not None:
        pytest.fail(
            "PR6 export service is not implemented: create "
            "app.services.conversion.export_service with the frozen contract "
            f"documented by this test module ({_EXPORT_IMPORT_ERROR})",
            pytrace=False,
        )


def _canonical_document() -> dict[str, Any]:
    return {
        "BL01": STATE,
        "BID01": BRIDGE,
        "BID02": "Deterministic export bridge",
        "BL03": "00123",
        "BL08": "36",
        "BL12": "SNBI-only MPO value",
        "BW01": 1977,
        "Elements": [
            {"BE01": "12", "BE02": "1", "BE03": 14.5, "BCS01": 14.5}
        ],
        "Features": [
            {
                "BF01": "H01",
                "BF02": "C",
                "BH06": "0123456789AB",
                "Routes": [
                    {"BRT01": "C01", "BRT02": "0001", "BRT03": "N"}
                ],
            }
        ],
        "Inspections": [
            {"BIE01": "1", "BIE02": "20240115", "BIE11": "SNBI note"}
        ],
        "PostingEvaluations": [],
        "PostingStatuses": [],
        "SpanSets": [],
        "SubstructureSets": [],
        "Works": [],
    }


def _validation() -> dict[str, Any]:
    return {
        "profile_id": "research_historical_import",
        "status": "passed_with_warnings",
        "validation_owner": "server",
        "official_validation": False,
        "issues": [],
    }


@pytest.fixture
def export_fixture(test_workspace: Path) -> dict[str, Any]:
    _require_export_api()
    database_path = str(test_workspace / "revision-exports.db")
    artifact_store = SourceArtifactStore(test_workspace / "source-artifacts")
    receipt = artifact_store.ingest(
        ORIGINAL_BYTES,
        original_filename=ORIGINAL_FILENAME,
    )

    ensure_schema(database_path)
    initialize_revision_store(database_path, app_revision="pr6-contract")
    ensure_workspace(database_path, WORKSPACE, "NJDOT research")

    source_record_id = "nbi-over-export-0001"
    revision = CanonicalRevisionInput(
        workspace_id=WORKSPACE,
        state_code=STATE,
        bridge_number=BRIDGE,
        data_year=YEAR,
        canonical_document=_canonical_document(),
        source_records=(
            {
                "source_artifact_id": receipt.artifact_id,
                "source_record_id": source_record_id,
                "record_kind": "nbi_over_record",
                "record_pointer": "/records/0",
                "payload": {
                    "STATE_CODE_001": "342",
                    "STRUCTURE_NUMBER_008": BRIDGE,
                    "RECORD_TYPE_005A": "1",
                },
            },
        ),
        artifact_receipts=(
            {
                **receipt.to_public_dict(),
                "media_type": "text/csv",
                "artifact_kind": "legacy_nbi_source",
            },
        ),
        lineage=(
            {
                "target_pointer": "/BID01",
                "source_artifact_id": receipt.artifact_id,
                "source_record_id": source_record_id,
                "source_pointer": "/payload/STRUCTURE_NUMBER_008",
                "rule_id": "nbi-item8-direct-v1",
                "status": "exact",
            },
        ),
        validation=_validation(),
    )
    staged = stage_canonical_batch(
        database_path,
        StageCanonicalBatchRequest(
            workspace_id=WORKSPACE,
            profile_id="research_historical_import",
            actor_id=ACTOR,
            request_id="stage-export-fixture",
            idempotency_key="stage-export-fixture",
            expected_data_generation=0,
            atomic_mode="batch",
            revisions=(revision,),
        ),
    )
    return {
        "database_path": database_path,
        "artifact_store": artifact_store,
        "receipt": receipt,
        "revision_id": staged.revisions[0].revision_id,
    }


def _request(revision_id: str, export_kind: str, profile_id: str):
    return RevisionExportRequest(
        workspace_id=WORKSPACE,
        snapshot_revision_id=revision_id,
        export_kind=export_kind,
        profile_id=profile_id,
        agency_id=AGENCY,
        actor_id=ACTOR,
        exported_at=EXPORTED_AT,
    )


def _export(fixture: dict[str, Any], export_kind: str, profile_id: str):
    return export_snapshot_revision(
        fixture["database_path"],
        fixture["artifact_store"],
        _request(fixture["revision_id"], export_kind, profile_id),
    )


def _files(result: Any) -> dict[str, Any]:
    return {artifact.filename: artifact for artifact in result.artifacts}


def _only_suffix(result: Any, suffix: str) -> Any:
    matches = [item for item in result.artifacts if item.filename.endswith(suffix)]
    assert len(matches) == 1, [item.filename for item in result.artifacts]
    return matches[0]


def _assert_artifact_integrity(artifact: Any) -> None:
    assert isinstance(artifact.content, bytes)
    assert artifact.size_bytes == len(artifact.content)
    assert artifact.sha256 == hashlib.sha256(artifact.content).hexdigest()
    assert "\\" not in artifact.filename and "/" not in artifact.filename


def test_profile_export_and_manifest_are_byte_deterministic(export_fixture):
    first = _export(
        export_fixture, "canonical_profile", "research_snbi_export_v1"
    )
    second = _export(
        export_fixture, "canonical_profile", "research_snbi_export_v1"
    )

    assert isinstance(first, RevisionExport)
    assert first == second
    assert asdict(first) == asdict(second)
    assert first.export_id == second.export_id
    assert first.export_kind == "canonical_profile"
    assert tuple(item.filename for item in first.artifacts) == tuple(
        sorted(item.filename for item in first.artifacts)
    )
    for artifact in first.artifacts:
        assert isinstance(artifact, ExportArtifact)
        _assert_artifact_integrity(artifact)

    snbi_file = _only_suffix(first, "_snbi.json")
    assert snbi_file.media_type == "application/json"
    assert json.loads(snbi_file.content) == [_canonical_document()]

    manifest_file = _only_suffix(first, ".manifest.json")
    assert manifest_file.media_type == "application/json"
    assert json.loads(manifest_file.content) == first.manifest
    assert first.manifest["format"] == "bridgeguard-export-manifest-v1"
    assert first.manifest["export_id"] == first.export_id
    assert first.manifest["export_kind"] == "canonical_profile"
    assert first.manifest["profile_id"] == "research_snbi_export_v1"
    assert first.manifest["workspace_id"] == WORKSPACE
    assert first.manifest["snapshot_revision_id"] == export_fixture["revision_id"]
    assert first.manifest["state_code"] == STATE
    assert first.manifest["bridge_number"] == BRIDGE
    assert first.manifest["inventory_year"] == YEAR
    assert first.manifest["agency_id"] == AGENCY
    assert first.manifest["actor_id"] == ACTOR
    assert first.manifest["exported_at"] == EXPORTED_AT
    assert first.manifest["external_validation"] is False
    assert first.manifest["official_validation"] is False
    assert first.manifest["submission_ready"] is False
    assert first.manifest["publication_gate"]["result"] == "not_invoked"
    assert first.manifest["publication_gate"]["internal_application_gate_only"] is True

    provenance = first.manifest["provenance"]
    assert provenance["format"] == "bridgeguard-revision-export-provenance-v1"
    assert provenance["external_validation"] is False
    assert provenance["validation"]["status"] == "passed_with_warnings"
    assert provenance["validation"]["canonical_sha256"] == first.manifest["snapshot_sha256"]
    assert provenance["validation_sha256"]
    assert provenance["eligibility_and_exclusions"]["decision_count"] == 1
    assert provenance["eligibility_and_exclusions"]["exclusion_count"] == 0
    assert provenance["eligibility_and_exclusions"]["decisions"][0]["decision"] == "accepted"
    assert provenance["lineage"]["record_count"] == 1
    assert provenance["lineage"]["records"][0]["rule_id"] == "nbi-item8-direct-v1"
    governing = provenance["governing_rule_bundle"]
    assert governing["rule_bundle_id"] == "research_historical_import"
    assert governing["manifest_status"] == "persisted_deterministic_profile_manifest"
    assert len(governing["rule_bundle_sha256"]) == 64
    assert governing["manifest_sha256"] == governing["rule_bundle_sha256"]
    assert governing["applied_rule_ids"] == ["nbi-item8-direct-v1"]
    assert len(governing["applied_rule_ids_sha256"]) == 64
    assert len(governing["manifest"]["rule_application_sha256"]) == 64
    crosswalk = next(
        artifact
        for artifact in governing["manifest"]["source_artifacts"]
        if artifact["artifact_id"] == "nbi_snbi_crosswalk_dec_2023"
    )
    assert crosswalk["sha256"] == (
        "9b81e0b7c9d808cbee65e8a5436f759e5d68b56665fb416ec51e69c40717d4fc"
    )

    payload_manifest = {
        item["filename"]: item for item in first.manifest["files"]
    }
    assert set(payload_manifest) == {snbi_file.filename}
    assert payload_manifest[snbi_file.filename] == {
        "filename": snbi_file.filename,
        "media_type": snbi_file.media_type,
        "size_bytes": snbi_file.size_bytes,
        "sha256": snbi_file.sha256,
    }
    serialized = manifest_file.content.decode("utf-8")
    assert export_fixture["database_path"] not in serialized
    assert "source-artifact://" not in serialized
    for private_key in (
        "storage_uri",
        "local_path",
        "database_path",
        "created_by",
        "published_by",
        "request_id",
        "idempotency_key",
    ):
        assert f'"{private_key}"' not in serialized


def test_restore_original_is_the_only_exact_reverse_and_preserves_source_bytes(
    export_fixture,
):
    result = _export(
        export_fixture, "restore_original", "exact_source_restore_v1"
    )
    files = _files(result)

    assert ORIGINAL_FILENAME in files
    restored = files[ORIGINAL_FILENAME]
    _assert_artifact_integrity(restored)
    assert restored.content == ORIGINAL_BYTES
    assert restored.sha256 == export_fixture["receipt"].sha256
    assert restored.media_type == "text/csv"
    assert result.manifest["exact_reverse"] is True
    assert result.manifest["source_artifacts"] == [
        {
            "artifact_id": export_fixture["receipt"].artifact_id,
            "filename": ORIGINAL_FILENAME,
            "sha256": hashlib.sha256(ORIGINAL_BYTES).hexdigest(),
            "size_bytes": len(ORIGINAL_BYTES),
        }
    ]
    assert all("projection" not in name for name in files)


def test_export_records_a_passed_internal_publication_gate(export_fixture):
    state = get_workspace_state(export_fixture["database_path"], WORKSPACE)
    publish_snapshot_revision(
        export_fixture["database_path"],
        PublishSnapshotRevisionRequest(
            workspace_id=WORKSPACE,
            revision_id=export_fixture["revision_id"],
            actor_id=ACTOR,
            request_id="publish-export-fixture",
            reason="Reviewed for application publication",
            expected_revision_id=None,
            expected_data_generation=state.data_generation,
        ),
    )

    result = _export(
        export_fixture, "canonical_profile", "research_snbi_export_v1"
    )
    gate = result.manifest["publication_gate"]
    assert gate["result"] == "passed"
    assert gate["currently_published"] is True
    assert gate["published_at"]
    assert gate["external_validation"] is False
    assert gate["official_fhwa_nbtis_validation"] is False
    assert gate["submission_ready"] is False


def test_legacy_projection_is_labeled_lossy_and_classifies_every_target_field(
    export_fixture,
):
    result = _export(
        export_fixture, "legacy_projection", "legacy_nbi_review_v1"
    )
    csv_file = _only_suffix(result, "_legacy-projection.csv")
    loss_file = _only_suffix(result, "_legacy-projection.loss.json")

    assert csv_file.media_type == "text/csv"
    assert not csv_file.filename.endswith(".txt")
    assert loss_file.media_type == "application/json"
    assert result.manifest["exact_reverse"] is False
    assert result.manifest["output_label"] == "legacy_projection_for_review"
    assert result.manifest["representation"] == "headered_csv"
    assert "NBI TXT" not in json.dumps(result.manifest)

    report = json.loads(loss_file.content)
    assert report["format"] == "bridgeguard-legacy-loss-report-v1"
    assert report["exact_reverse"] is False
    entries = report["entries"]
    assert entries == sorted(entries, key=lambda item: item["target_field"])
    assert entries
    for entry in entries:
        assert set(entry) >= {
            "target_field",
            "category",
            "reason_code",
            "source_pointers",
        }
        assert entry["category"] in LOSS_CATEGORIES

    reader = csv.DictReader(io.StringIO(csv_file.content.decode("utf-8")))
    assert reader.fieldnames
    reported_fields = {entry["target_field"] for entry in entries}
    assert set(reader.fieldnames) <= reported_fields

    hierarchy = next(
        entry for entry in entries if entry["target_field"] == "RECORD_TYPE_005A"
    )
    assert hierarchy["category"] == "structurally_collapsed"
    assert hierarchy["reason_code"] == "type_1_only_projection"
    assert "Type 1" in hierarchy.get("detail", "")


def test_export_extensions_and_media_types_match_the_actual_representations(
    export_fixture,
):
    projection = _export(
        export_fixture, "legacy_projection", "legacy_nbi_review_v1"
    )
    report = _export(export_fixture, "human_report", "human_review_report_v1")

    expected_suffix = {
        "text/csv": ".csv",
        "application/xml": ".xml",
        "text/plain": ".txt",
        "application/json": ".json",
    }
    for result in (projection, report):
        names = [artifact.filename for artifact in result.artifacts]
        assert len(names) == len(set(names))
        for artifact in result.artifacts:
            assert artifact.filename.endswith(expected_suffix[artifact.media_type])
            _assert_artifact_integrity(artifact)

    human = [item for item in report.artifacts if item.media_type == "text/plain"]
    assert len(human) == 1
    assert human[0].filename.endswith("_human-report.txt")
    assert b"review" in human[0].content.lower()


def test_export_error_contract_is_typed_and_http_ready():
    _require_export_api()
    error = ExportServiceError(422, "original_source_unavailable", "No evidence")
    assert error.status == 422
    assert error.code == "original_source_unavailable"
    assert error.message == "No evidence"


@pytest.mark.parametrize("value", [0, -7, 2.5, True, False, None])
def test_spreadsheet_formula_safety_does_not_coerce_non_text_values(value):
    result = spreadsheet_safe_value(value)

    assert result is value
    assert type(result) is type(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("=1+1", "'=1+1"),
        ("   +SUM(1,2)", "'   +SUM(1,2)"),
        ("\t-2+3", "'\t-2+3"),
        ("\r\n@IMPORTXML(x)", "'\r\n@IMPORTXML(x)"),
        ("bridge-42", "bridge-42"),
        ("  bridge - 42", "  bridge - 42"),
        ("'=@already-literal", "'=@already-literal"),
        ("   ", "   "),
    ],
)
def test_spreadsheet_formula_safety_prefixes_original_text_only(value, expected):
    assert spreadsheet_safe_value(value) == expected


def test_revision_legacy_csv_is_formula_safe_and_preserves_csv_content():
    original = '  =HYPERLINK("https://invalid.example","deck, joint")\r\nnext'
    content = _csv_bytes({"LOCATION_009": original})

    assert content.endswith(b"\r\n")
    rows = list(csv.DictReader(io.StringIO(content.decode("utf-8"), newline="")))
    assert len(rows) == 1
    assert rows[0]["LOCATION_009"] == "'" + original
