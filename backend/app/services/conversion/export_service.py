"""Deterministic, revision-scoped exports for canonical SNBI snapshots.

This module is intentionally the only high-level reverse/export boundary.  It
does not accept an arbitrary browser document and it never treats a generated
legacy-shaped row as the original NBI evidence.  The four supported products
are deliberately distinct:

* a canonical SNBI profile package;
* byte-for-byte recovery of linked immutable source artifacts;
* a labelled, lossy legacy projection with field-level loss accounting; and
* a human-readable review report.

Every output is assembled in memory.  Filesystem paths and artifact storage
URIs are neither returned nor copied into manifests.  ``exported_at`` is part
of the request evidence, so no wall-clock read can make a repeated export
different.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.routers.services.constants import field_names
from app.routers.services.reverse_mapper import snbi_to_nbi_row
from app.services.spreadsheet_csv import spreadsheet_safe_value
from app.services.conversion.canonical_json import (
    CANONICAL_FORMAT,
    CanonicalJsonError,
    canonical_json_bytes,
    canonical_sha256,
)
from app.services.conversion.legacy_projection import (
    build_legacy_projection_manifest,
)
from app.services.conversion.revision_store import (
    RevisionStoreError,
    SnapshotRevisionRecord,
    get_snapshot_revision,
)
from app.services.conversion.source_artifacts import (
    SourceArtifactError,
    SourceArtifactStore,
)


_MANIFEST_FORMAT = "bridgeguard-export-manifest-v1"
_LOSS_REPORT_FORMAT = "bridgeguard-legacy-loss-report-v1"
_EXPORT_KINDS = frozenset(
    {
        "canonical_profile",
        "restore_original",
        "legacy_projection",
        "human_report",
    }
)
_PROFILE_BY_KIND = {
    "canonical_profile": "research_snbi_export_v1",
    "restore_original": "exact_source_restore_v1",
    "legacy_projection": "legacy_nbi_review_v1",
    "human_report": "human_review_report_v1",
}
_LOSS_CATEGORIES = frozenset(
    {
        "exact",
        "within_numeric_tolerance",
        "representative_inverse_chosen",
        "structurally_collapsed",
        "omitted_no_legacy_representation",
        "generated_application_value",
        "unavailable_missing_provenance",
    }
)
_LEGACY_CLASSIFICATION = {
    "exact": "exact",
    "within_numeric_tolerance": "within_numeric_tolerance",
    "representative_inverse_chosen": "representative_inverse_chosen",
    "structurally_collapsed": "structurally_collapsed",
    "omitted_because_no_legacy_representation": (
        "omitted_no_legacy_representation"
    ),
    "unavailable_due_missing_provenance": "unavailable_missing_provenance",
    "generated_application_value": "generated_application_value",
}
_HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MEDIA_TYPE_RE = re.compile(
    r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$"
)
_SOURCE_ARTIFACT_URI_RE = re.compile(
    r"^source-artifact://([0-9a-fA-F-]{36})$"
)
_ORIGINAL_LEGACY_ARTIFACT_KINDS = frozenset(
    {"legacy_nbi_source", "legacy_element_xml_source"}
)
_PASSING_VALIDATION_STATUSES = frozenset(
    {"passed", "passed_with_warnings", "valid", "success"}
)
_PROVENANCE_FORMAT = "bridgeguard-revision-export-provenance-v1"
_PRIVATE_EVIDENCE_KEYS = frozenset(
    {
        "actor_id",
        "created_by",
        "database_path",
        "idempotency_key",
        "local_path",
        "published_by",
        "request_id",
        "source_url",
        "storage_uri",
    }
)


class ExportServiceError(Exception):
    """A path-free, typed failure suitable for direct HTTP translation."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)
        self.details = dict(details or {})

    def to_public_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            value["details"] = dict(self.details)
        return value


@dataclass(frozen=True, slots=True)
class RevisionExportRequest:
    workspace_id: str
    snapshot_revision_id: str
    export_kind: str
    profile_id: str
    agency_id: str
    actor_id: str
    exported_at: str


@dataclass(frozen=True, slots=True)
class ExportArtifact:
    filename: str
    media_type: str
    content: bytes
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class RevisionExport:
    export_id: str
    workspace_id: str
    snapshot_revision_id: str
    export_kind: str
    profile_id: str
    artifacts: tuple[ExportArtifact, ...]
    manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _LinkedSourceArtifact:
    artifact_id: str
    sha256: str
    byte_size: int
    original_filename: str | None
    media_type: str | None
    artifact_kind: str
    storage_uri: str | None
    first_source_ordinal: int


def _required_text(value: Any, field: str, *, maximum: int = 512) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ExportServiceError(
            422, "invalid_export_request", f"{field} must be a non-blank string"
        )
    if len(value) > maximum or any(ord(character) < 0x20 for character in value):
        raise ExportServiceError(
            422, "invalid_export_request", f"{field} is invalid"
        )
    return value


def _validate_request(request: RevisionExportRequest) -> None:
    if not isinstance(request, RevisionExportRequest):
        raise ExportServiceError(
            422,
            "invalid_export_request",
            "request must be a RevisionExportRequest",
        )
    _required_text(request.workspace_id, "workspace_id")
    _required_text(request.snapshot_revision_id, "snapshot_revision_id")
    _required_text(request.agency_id, "agency_id", maximum=128)
    _required_text(request.actor_id, "actor_id")
    _required_text(request.exported_at, "exported_at", maximum=64)
    if request.export_kind not in _EXPORT_KINDS:
        raise ExportServiceError(
            422,
            "unsupported_export_kind",
            "The requested export kind is not supported.",
            details={"supported": sorted(_EXPORT_KINDS)},
        )
    required_profile = _PROFILE_BY_KIND[request.export_kind]
    if request.profile_id != required_profile:
        raise ExportServiceError(
            422,
            "unsupported_export_profile",
            "The selected profile is not valid for this export kind.",
            details={"required_profile": required_profile},
        )
    try:
        parsed = datetime.fromisoformat(request.exported_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExportServiceError(
            422,
            "invalid_exported_at",
            "exported_at must be an ISO 8601 timestamp with a time-zone offset.",
        ) from exc
    if parsed.tzinfo is None:
        raise ExportServiceError(
            422,
            "invalid_exported_at",
            "exported_at must include a time-zone offset.",
        )


def _artifact(filename: str, media_type: str, content: bytes) -> ExportArtifact:
    if (
        not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
    ):
        raise ExportServiceError(
            500, "unsafe_export_filename", "An export filename was unsafe."
        )
    if not _MEDIA_TYPE_RE.fullmatch(media_type):
        raise ExportServiceError(
            500, "invalid_export_media_type", "An export media type was invalid."
        )
    body = bytes(content)
    return ExportArtifact(
        filename=filename,
        media_type=media_type,
        content=body,
        size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
    )


def _safe_token(value: Any, *, fallback: str, maximum: int = 72) -> str:
    token = _SAFE_TOKEN_RE.sub("-", str(value)).strip("._-")
    return (token[:maximum] or fallback).lower()


def _filename_base(revision: SnapshotRevisionRecord) -> str:
    bridge = _safe_token(revision.bridge_number, fallback="bridge")
    state = _safe_token(revision.state_code, fallback="state", maximum=8)
    year = _safe_token(revision.data_year, fallback="year", maximum=8)
    return f"bridgeguard_{state}_{bridge}_{year}"


def _file_entry(artifact: ExportArtifact) -> dict[str, Any]:
    return {
        "filename": artifact.filename,
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
    }


def _sanitize_evidence(value: Any) -> Any:
    """Remove persistence paths and stored actor/request identities recursively."""

    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_evidence(child)
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
            if str(key) not in _PRIVATE_EVIDENCE_KEYS
        }
    if isinstance(value, (tuple, list)):
        return [_sanitize_evidence(child) for child in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def _revision_batch_id(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    revision_id: str,
) -> str | None:
    """Find the originating conversion batch through a bounded parent chain."""

    current = revision_id
    seen: set[str] = set()
    for _ in range(128):
        if current in seen:
            raise ExportServiceError(
                409,
                "revision_ancestry_cycle",
                "Revision ancestry could not be resolved safely.",
            )
        seen.add(current)
        row = connection.execute(
            "SELECT conversion_batch_id,parent_revision_id FROM snapshot_revisions "
            "WHERE workspace_id=? AND id=?",
            (workspace_id, current),
        ).fetchone()
        if row is None:
            return None
        if row["conversion_batch_id"]:
            return str(row["conversion_batch_id"])
        if not row["parent_revision_id"]:
            return None
        current = str(row["parent_revision_id"])
    raise ExportServiceError(
        409,
        "revision_ancestry_limit",
        "Revision ancestry exceeded the safe evidence limit.",
    )


def _eligibility_evidence(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    batch_id: str | None,
    source_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if batch_id is None:
        decisions: list[dict[str, Any]] = []
    else:
        rows = connection.execute(
            "SELECT ed.decision,ed.reason_code,ed.reason_detail,ed.evidence_json,"
            "sr.record_ordinal,sr.source_role,sr.record_identity,sr.raw_record_sha256 "
            "FROM eligibility_decisions ed JOIN source_records sr "
            "ON sr.workspace_id=ed.workspace_id AND sr.id=ed.source_record_id "
            "WHERE ed.workspace_id=? AND ed.conversion_batch_id=? "
            "ORDER BY sr.record_ordinal,sr.raw_record_sha256,ed.id",
            (workspace_id, batch_id),
        ).fetchall()
        decisions = []
        for row in rows:
            try:
                detail = json.loads(str(row["evidence_json"] or "{}"))
            except json.JSONDecodeError:
                detail = {"evidence_status": "invalid_stored_json"}
            decisions.append(
                {
                    "decision": str(row["decision"]),
                    "reason_code": str(row["reason_code"]),
                    "reason_detail": (
                        str(row["reason_detail"])
                        if row["reason_detail"] is not None
                        else None
                    ),
                    "source_record": {
                        "ordinal": int(row["record_ordinal"]),
                        "role": str(row["source_role"]),
                        "identity": str(row["record_identity"]),
                        "sha256": str(row["raw_record_sha256"]),
                    },
                    "evidence": _sanitize_evidence(detail),
                }
            )
    persistence_exclusions = [
        value
        for value in decisions
        if str(value["decision"]).lower() not in {"accepted", "no_change"}
    ]
    transition_eligibility: list[dict[str, Any]] = []
    for source in source_records:
        if not isinstance(source, Mapping):
            continue
        candidates: list[Any] = [source]
        payload = source.get("payload")
        if isinstance(payload, Mapping):
            candidates.append(payload)
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            source_data = candidate.get("_SourceData")
            eligibility = (
                source_data.get("eligibility")
                if isinstance(source_data, Mapping)
                else candidate.get("eligibility")
            )
            if not isinstance(eligibility, Mapping):
                continue
            sanitized = _sanitize_evidence(eligibility)
            if sanitized not in transition_eligibility:
                transition_eligibility.append(sanitized)
    transition_eligibility.sort(key=canonical_sha256)
    transition_exclusions = [
        value
        for value in transition_eligibility
        if value.get("has_federally_eligible_record") is False
        or (
            str(value.get("decision") or "").lower()
            in {"excluded", "ineligible", "rejected"}
        )
    ]
    exclusions = [
        *(
            {"scope": "persistence", **value}
            for value in persistence_exclusions
        ),
        *(
            {"scope": "federal_transition", "evidence": value}
            for value in transition_exclusions
        ),
    ]
    return {
        "scope": "revision_sources_and_originating_conversion_batch",
        "originating_conversion_batch_id": batch_id,
        "decision_count": len(decisions),
        "exclusion_count": len(exclusions),
        "decisions": decisions,
        "federal_transition_eligibility": transition_eligibility,
        "exclusions": exclusions,
        "sha256": canonical_sha256(
            {
                "decisions": decisions,
                "federal_transition_eligibility": transition_eligibility,
                "exclusions": exclusions,
            }
        ),
    }


def _revision_export_provenance(
    database_path: str,
    revision: SnapshotRevisionRecord,
) -> dict[str, Any]:
    connection = _read_only_connection(database_path)
    try:
        batch_id = _revision_batch_id(
            connection,
            workspace_id=revision.workspace_id,
            revision_id=revision.revision_id,
        )
        eligibility = _eligibility_evidence(
            connection,
            workspace_id=revision.workspace_id,
            batch_id=batch_id,
            source_records=revision.source_records,
        )
        published = connection.execute(
            "SELECT published_at FROM published_snapshots WHERE workspace_id=? "
            "AND snapshot_revision_id=?",
            (revision.workspace_id, revision.revision_id),
        ).fetchone()
        last_publish = connection.execute(
            "SELECT occurred_at FROM mutation_audit WHERE workspace_id=? "
            "AND entity_id=? AND action='publish_revision' "
            "ORDER BY occurred_at DESC,id DESC LIMIT 1",
            (revision.workspace_id, revision.revision_id),
        ).fetchone()
        last_archive = connection.execute(
            "SELECT occurred_at FROM mutation_audit WHERE workspace_id=? "
            "AND entity_id=? AND action='archive_revision' "
            "ORDER BY occurred_at DESC,id DESC LIMIT 1",
            (revision.workspace_id, revision.revision_id),
        ).fetchone()
    except sqlite3.Error as exc:
        raise ExportServiceError(
            500,
            "export_repository_error",
            "Revision provenance could not be read.",
        ) from exc
    finally:
        connection.close()

    lineage = _sanitize_evidence(list(revision.lineage))
    validation = _sanitize_evidence(dict(revision.validation or {}))
    # The full manifest is carried once under ``governing_rule_bundle`` below;
    # retaining a second copy inside validation would inflate every export.
    validation.pop("rule_bundle_manifest", None)
    validation["official_validation"] = False
    validation["external_validation"] = False
    validation["submission_ready"] = False
    status = str(validation.get("status") or "").lower()
    validation_owner = validation.get("validation_owner")
    canonical_matches = validation.get("canonical_sha256") == revision.content_hash
    internal_gate_eligible = (
        validation_owner == "server"
        and status in _PASSING_VALIDATION_STATUSES
        and canonical_matches
    )
    if published is not None or last_publish is not None:
        gate_result = (
            "passed"
            if internal_gate_eligible
            else "historical_publication_gate_unverified"
        )
    elif not internal_gate_eligible:
        gate_result = "failed"
    else:
        gate_result = "not_invoked"
    publication_gate = {
        "result": gate_result,
        "currently_published": published is not None,
        "published_at": (
            str(published["published_at"])
            if published
            else str(last_publish["occurred_at"])
            if last_publish
            else None
        ),
        "last_archived_at": (
            str(last_archive["occurred_at"]) if last_archive else None
        ),
        "validation_owner_is_server": validation_owner == "server",
        "validation_status": status or None,
        "validation_canonical_sha256_matches": canonical_matches,
        "internal_application_gate_only": True,
        "external_validation": False,
        "official_fhwa_nbtis_validation": False,
        "submission_ready": False,
    }
    rule_ids = sorted(
        {
            str(value.get("rule_id"))
            for value in lineage
            if isinstance(value, Mapping) and value.get("rule_id")
        }
    )
    bundle_manifest = _sanitize_evidence(revision.rule_bundle_manifest or {})
    governing_bundle = {
        "rule_bundle_id": revision.rule_bundle_id,
        "rule_bundle_sha256": revision.rule_bundle_sha256,
        "manifest_status": (
            "persisted_deterministic_profile_manifest"
            if bundle_manifest
            else "legacy_manifest_unavailable"
        ),
        "manifest": bundle_manifest,
        "manifest_sha256": (
            canonical_sha256(bundle_manifest) if bundle_manifest else None
        ),
        "applied_rule_ids": rule_ids,
        "applied_rule_ids_sha256": canonical_sha256(rule_ids),
    }
    return {
        "format": _PROVENANCE_FORMAT,
        "external_validation": False,
        "official_validation": False,
        "submission_ready": False,
        "validation": validation,
        "validation_sha256": canonical_sha256(validation),
        "eligibility_and_exclusions": eligibility,
        "lineage": {
            "record_count": len(lineage),
            "records": lineage,
            "sha256": canonical_sha256(lineage),
        },
        "governing_rule_bundle": governing_bundle,
        "publication_gate": publication_gate,
    }


def _base_manifest(
    request: RevisionExportRequest,
    revision: SnapshotRevisionRecord,
    payloads: Sequence[ExportArtifact],
    *,
    provenance: Mapping[str, Any],
    additions: Mapping[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    files = [_file_entry(value) for value in sorted(payloads, key=lambda item: item.filename)]
    identity = {
        "format": _MANIFEST_FORMAT,
        "workspace_id": request.workspace_id,
        "snapshot_revision_id": request.snapshot_revision_id,
        "snapshot_sha256": revision.content_hash,
        "export_kind": request.export_kind,
        "profile_id": request.profile_id,
        "agency_id": request.agency_id,
        "actor_id": request.actor_id,
        "exported_at": request.exported_at,
        "files": files,
        "provenance": dict(provenance),
        "manifest_additions": dict(additions or {}),
    }
    export_id = f"exp-{canonical_sha256(identity)}"
    manifest: dict[str, Any] = {
        "format": _MANIFEST_FORMAT,
        "export_id": export_id,
        "export_kind": request.export_kind,
        "profile_id": request.profile_id,
        "workspace_id": request.workspace_id,
        "snapshot_revision_id": request.snapshot_revision_id,
        "snapshot_sha256": revision.content_hash,
        "state_code": revision.state_code,
        "bridge_number": revision.bridge_number,
        "inventory_year": revision.data_year,
        "revision_number": revision.revision_number,
        "agency_id": request.agency_id,
        "actor_id": request.actor_id,
        "exported_at": request.exported_at,
        "files": files,
        "external_validation": False,
        "official_validation": False,
        "submission_ready": False,
        "publication_gate": dict(provenance["publication_gate"]),
        "provenance": dict(provenance),
    }
    if additions:
        manifest.update(dict(additions))
    return export_id, manifest


def _finish_export(
    request: RevisionExportRequest,
    revision: SnapshotRevisionRecord,
    payloads: Sequence[ExportArtifact],
    *,
    manifest_suffix: str,
    provenance: Mapping[str, Any],
    manifest_additions: Mapping[str, Any] | None = None,
) -> RevisionExport:
    filenames = [artifact.filename for artifact in payloads]
    if len(filenames) != len(set(filenames)):
        raise ExportServiceError(
            409,
            "duplicate_export_filename",
            "Linked evidence produced duplicate export filenames.",
        )
    export_id, manifest = _base_manifest(
        request,
        revision,
        payloads,
        provenance=provenance,
        additions=manifest_additions,
    )
    manifest_name = f"{_filename_base(revision)}_{manifest_suffix}.manifest.json"
    occupied = set(filenames)
    if manifest_name in occupied:
        manifest_name = f"{_filename_base(revision)}_{export_id[4:16]}.manifest.json"
    manifest_artifact = _artifact(
        manifest_name, "application/json", canonical_json_bytes(manifest)
    )
    artifacts = tuple(sorted((*payloads, manifest_artifact), key=lambda item: item.filename))
    return RevisionExport(
        export_id=export_id,
        workspace_id=request.workspace_id,
        snapshot_revision_id=request.snapshot_revision_id,
        export_kind=request.export_kind,
        profile_id=request.profile_id,
        artifacts=artifacts,
        manifest=manifest,
    )


def _verify_revision_integrity(revision: SnapshotRevisionRecord) -> None:
    try:
        canonical = canonical_json_bytes(revision.canonical_document)
        digest = hashlib.sha256(canonical).hexdigest()
    except CanonicalJsonError as exc:
        raise ExportServiceError(
            409,
            "revision_integrity_error",
            "The stored canonical revision is not valid canonical JSON.",
        ) from exc
    if (
        not _HEX_SHA256_RE.fullmatch(revision.content_hash)
        or digest != revision.content_hash
        or canonical.decode("utf-8") != revision.canonical_json
    ):
        raise ExportServiceError(
            409,
            "revision_integrity_error",
            "The stored canonical revision failed its integrity check.",
        )


def _canonical_profile_export(
    request: RevisionExportRequest,
    revision: SnapshotRevisionRecord,
    provenance: Mapping[str, Any],
) -> RevisionExport:
    body = canonical_json_bytes([revision.canonical_document])
    payload = _artifact(
        f"{_filename_base(revision)}_snbi.json", "application/json", body
    )
    return _finish_export(
        request,
        revision,
        (payload,),
        manifest_suffix="canonical",
        provenance=provenance,
        manifest_additions={
            "representation": "canonical_snbi_json",
            "canonical_format": CANONICAL_FORMAT,
            "bridge_count": 1,
        },
    )


def _read_only_connection(database_path: str) -> sqlite3.Connection:
    try:
        resolved = Path(database_path).resolve(strict=True)
        if not resolved.is_file():
            raise OSError("not a file")
        connection = sqlite3.connect(
            f"{resolved.as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection
    except (OSError, sqlite3.Error) as exc:
        raise ExportServiceError(
            500,
            "export_repository_error",
            "Revision evidence could not be read.",
        ) from exc


def _linked_source_artifacts(
    database_path: str,
    *,
    workspace_id: str,
    revision_id: str,
) -> tuple[_LinkedSourceArtifact, ...]:
    connection = _read_only_connection(database_path)
    try:
        rows = connection.execute(
            "SELECT sa.id, sa.sha256, sa.byte_size, sa.original_filename, "
            "sa.media_type, sa.artifact_kind, sa.storage_uri, "
            "MIN(srs.source_ordinal) AS first_source_ordinal "
            "FROM snapshot_revision_sources srs "
            "JOIN source_records sr "
            "ON sr.workspace_id=srs.workspace_id AND sr.id=srs.source_record_id "
            "JOIN source_artifacts sa "
            "ON sa.workspace_id=sr.workspace_id AND sa.id=sr.source_artifact_id "
            "WHERE srs.workspace_id=? AND srs.snapshot_revision_id=? "
            "AND sr.workspace_id=? AND sa.workspace_id=? "
            "GROUP BY sa.id, sa.sha256, sa.byte_size, sa.original_filename, "
            "sa.media_type, sa.artifact_kind, sa.storage_uri "
            "ORDER BY first_source_ordinal, sa.id",
            (workspace_id, revision_id, workspace_id, workspace_id),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ExportServiceError(
            500,
            "export_repository_error",
            "Revision evidence could not be read.",
        ) from exc
    finally:
        connection.close()
    return tuple(
        _LinkedSourceArtifact(
            artifact_id=str(row["id"]),
            sha256=str(row["sha256"]),
            byte_size=int(row["byte_size"]),
            original_filename=(
                str(row["original_filename"])
                if row["original_filename"] is not None
                else None
            ),
            media_type=(
                str(row["media_type"]) if row["media_type"] is not None else None
            ),
            artifact_kind=str(row["artifact_kind"]),
            storage_uri=(
                str(row["storage_uri"]) if row["storage_uri"] is not None else None
            ),
            first_source_ordinal=int(row["first_source_ordinal"]),
        )
        for row in rows
    )


def _deduplicated_filename(
    filename: str, *, artifact_id: str, occupied: set[str]
) -> str:
    if filename not in occupied:
        occupied.add(filename)
        return filename
    path = Path(filename)
    stem = path.stem or "source"
    suffix = path.suffix
    candidate = f"{stem}__{artifact_id[:12]}{suffix}"
    counter = 2
    while candidate in occupied:
        candidate = f"{stem}__{artifact_id[:12]}_{counter}{suffix}"
        counter += 1
    occupied.add(candidate)
    return candidate


def _source_integrity_error() -> ExportServiceError:
    return ExportServiceError(
        409,
        "source_artifact_integrity_error",
        "Linked source evidence failed its integrity check.",
    )


def _restore_original_export(
    database_path: str,
    artifact_store: SourceArtifactStore,
    request: RevisionExportRequest,
    revision: SnapshotRevisionRecord,
    provenance: Mapping[str, Any],
) -> RevisionExport:
    linked = _linked_source_artifacts(
        database_path,
        workspace_id=request.workspace_id,
        revision_id=request.snapshot_revision_id,
    )
    restorable: list[_LinkedSourceArtifact] = []
    for value in linked:
        if value.storage_uri is None:
            # Inline/reconstructed evidence has no immutable original bytes.
            continue
        match = _SOURCE_ARTIFACT_URI_RE.fullmatch(value.storage_uri)
        if match is None or match.group(1).lower() != value.artifact_id.lower():
            raise _source_integrity_error()
        restorable.append(value)
    if not restorable:
        raise ExportServiceError(
            422,
            "original_source_unavailable",
            "No immutable original source is linked to this revision.",
        )

    # A strict converter revision can link both the uploaded conversion JSON
    # envelope and the original NBI/element artifacts cited by its embedded
    # source evidence.  When originals exist, restoring the envelope instead
    # would be exact bytes but the wrong product.  Retain the envelope only as
    # a fallback for revisions that have no linked original legacy artifact.
    original_legacy = [
        value
        for value in restorable
        if value.artifact_kind in _ORIGINAL_LEGACY_ARTIFACT_KINDS
    ]
    if original_legacy:
        restorable = original_legacy

    payloads: list[ExportArtifact] = []
    source_manifest: list[dict[str, Any]] = []
    occupied: set[str] = set()
    for linked_artifact in restorable:
        if (
            not _HEX_SHA256_RE.fullmatch(linked_artifact.sha256)
            or linked_artifact.byte_size < 1
        ):
            raise _source_integrity_error()
        try:
            receipt = artifact_store.get(linked_artifact.artifact_id)
            content = artifact_store.read_bytes(linked_artifact.artifact_id)
        except (SourceArtifactError, ValueError) as exc:
            raise _source_integrity_error() from exc
        if (
            receipt.artifact_id != linked_artifact.artifact_id
            or receipt.sha256 != linked_artifact.sha256
            or receipt.size_bytes != linked_artifact.byte_size
            or receipt.original_filename != linked_artifact.original_filename
            or len(content) != linked_artifact.byte_size
            or hashlib.sha256(content).hexdigest() != linked_artifact.sha256
        ):
            raise _source_integrity_error()
        media_type = linked_artifact.media_type or "application/octet-stream"
        if not _MEDIA_TYPE_RE.fullmatch(media_type):
            raise _source_integrity_error()
        filename = _deduplicated_filename(
            receipt.original_filename,
            artifact_id=receipt.artifact_id,
            occupied=occupied,
        )
        payloads.append(_artifact(filename, media_type, content))
        source_manifest.append(
            {
                "artifact_id": receipt.artifact_id,
                "filename": filename,
                "sha256": receipt.sha256,
                "size_bytes": receipt.size_bytes,
            }
        )

    return _finish_export(
        request,
        revision,
        payloads,
        manifest_suffix="original-source",
        provenance=provenance,
        manifest_additions={
            "exact_reverse": True,
            "representation": "original_source_bytes",
            "source_artifacts": source_manifest,
        },
    )


def _csv_bytes(row: Mapping[str, Any]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(field_names),
        lineterminator="\r\n",
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerow(
        {
            field: spreadsheet_safe_value(row.get(field, ""))
            for field in field_names
        }
    )
    return buffer.getvalue().encode("utf-8")


def _source_field_evidence(
    source_records: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[list[str], list[str]]]:
    evidence: dict[str, tuple[list[str], list[str]]] = {}
    mutable: dict[str, tuple[list[str], list[str]]] = {}
    for index, record in enumerate(source_records):
        if not isinstance(record, Mapping):
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        for field, raw_value in payload.items():
            if type(field) is not str:
                continue
            pointers, values = mutable.setdefault(field, ([], []))
            escaped = field.replace("~", "~0").replace("/", "~1")
            pointers.append(f"/source_records/{index}/payload/{escaped}")
            values.append("" if raw_value is None else str(raw_value))
    for field, (pointers, values) in mutable.items():
        evidence[field] = (sorted(set(pointers)), values)
    return evidence


def _numeric_equivalent(left: str, right: str) -> bool:
    try:
        left_number = float(left)
        right_number = float(right)
    except (TypeError, ValueError):
        return False
    return math.isfinite(left_number) and math.isfinite(right_number) and math.isclose(
        left_number, right_number, rel_tol=0.005, abs_tol=0.1
    )


def _base_loss_entries(
    revision: SnapshotRevisionRecord, row: Mapping[str, str]
) -> dict[str, dict[str, Any]]:
    original = _source_field_evidence(revision.source_records)
    entries: dict[str, dict[str, Any]] = {}
    for field in field_names:
        projected = str(row.get(field) or "")
        pointers, source_values = original.get(field, ([], []))
        if field == "RECORD_TYPE_005A":
            category = "structurally_collapsed"
            reason_code = "type_1_only_projection"
            detail = (
                "The review projection emits one synthetic Type 1 row; "
                "original Type 2/A hierarchy is recoverable only from immutable sources."
            )
        elif source_values and projected in source_values:
            category = "exact"
            reason_code = "source_value_preserved"
            detail = "The projected text matches linked source-record evidence."
        elif source_values and any(
            _numeric_equivalent(projected, source) for source in source_values
        ):
            category = "within_numeric_tolerance"
            reason_code = "numeric_round_trip_tolerance"
            detail = "The projected number is within the documented inverse tolerance."
        elif projected:
            category = "representative_inverse_chosen"
            reason_code = "deterministic_best_effort_inverse"
            detail = (
                "A deterministic inverse was selected; exact legacy formatting or "
                "source provenance is not available from canonical SNBI alone."
            )
        else:
            category = "unavailable_missing_provenance"
            reason_code = "target_value_unavailable"
            detail = "No defensible value was available for this legacy target field."
        entries[field] = {
            "target_field": field,
            "category": category,
            "reason_code": reason_code,
            "source_pointers": pointers,
            "value_written": projected if projected else None,
            "detail": detail,
        }
    return entries


def _apply_known_loss_decisions(
    entries: dict[str, dict[str, Any]],
    bridge: Mapping[str, Any],
    row: Mapping[str, str],
) -> None:
    policy = build_legacy_projection_manifest([bridge], [row])
    outcomes = policy.get("bridges", [{}])[0].get("outcomes", [])
    for outcome in outcomes:
        if not isinstance(outcome, Mapping):
            continue
        source_path = str(outcome.get("source_path") or "")
        legacy_field = outcome.get("legacy_field")
        targets: list[str]
        if legacy_field == "LRS_INV_ROUTE_013A+SUBROUTE_NO_013B":
            targets = ["LRS_INV_ROUTE_013A", "SUBROUTE_NO_013B"]
        elif isinstance(legacy_field, str) and legacy_field in entries:
            targets = [legacy_field]
        elif source_path:
            targets = [f"SNBI:{source_path}"]
        else:
            continue
        category = _LEGACY_CLASSIFICATION.get(
            str(outcome.get("classification") or ""),
            "unavailable_missing_provenance",
        )
        if category not in _LOSS_CATEGORIES:
            category = "unavailable_missing_provenance"
        for target in targets:
            if target == "RECORD_TYPE_005A":
                # The public report uses a stable reason code consumed by the
                # review UI and distinguishes restoration from projection.
                entries[target] = {
                    "target_field": target,
                    "category": "structurally_collapsed",
                    "reason_code": "type_1_only_projection",
                    "source_pointers": ["/Features"],
                    "value_written": "1",
                    "detail": (
                        "Only a synthetic Type 1 row is emitted; Type 2/A "
                        "hierarchy is not asserted by this projection."
                    ),
                }
                continue
            entries[target] = {
                "target_field": target,
                "category": category,
                "reason_code": str(outcome.get("rule_id") or "projection_policy"),
                "source_pointers": [source_path] if source_path else [],
                "value_written": outcome.get("value_written"),
                "detail": str(outcome.get("message") or "Projection policy applied."),
            }


def _nested_resource_losses(
    entries: dict[str, dict[str, Any]], bridge: Mapping[str, Any]
) -> None:
    for resource in (
        "Elements",
        "Features",
        "Inspections",
        "PostingEvaluations",
        "PostingStatuses",
        "SpanSets",
        "SubstructureSets",
        "Works",
    ):
        values = bridge.get(resource)
        if not isinstance(values, list) or not values:
            continue
        key = f"SNBI:/{resource}"
        if key in entries:
            continue
        entries[key] = {
            "target_field": key,
            "category": "structurally_collapsed",
            "reason_code": "repeated_resource_not_representable",
            "source_pointers": [f"/{resource}"],
            "value_written": None,
            "detail": (
                f"Repeated {resource} data cannot be represented completely in "
                "one legacy Type 1 review row; it remains in the canonical revision."
            ),
        }


def _legacy_projection_export(
    request: RevisionExportRequest,
    revision: SnapshotRevisionRecord,
    provenance: Mapping[str, Any],
) -> RevisionExport:
    bridge = revision.canonical_document
    if not isinstance(bridge, Mapping):
        raise ExportServiceError(
            409,
            "revision_integrity_error",
            "The canonical revision is not a bridge object.",
        )
    row = snbi_to_nbi_row(dict(bridge))
    entries = _base_loss_entries(revision, row)
    _apply_known_loss_decisions(entries, bridge, row)
    _nested_resource_losses(entries, bridge)
    ordered_entries = sorted(entries.values(), key=lambda item: item["target_field"])
    loss_report = {
        "format": _LOSS_REPORT_FORMAT,
        "exact_reverse": False,
        "output_label": "legacy_projection_for_review",
        "representation": "headered_csv",
        "snapshot_revision_id": revision.revision_id,
        "entries": ordered_entries,
    }
    base = _filename_base(revision)
    csv_artifact = _artifact(
        f"{base}_legacy-projection.csv", "text/csv", _csv_bytes(row)
    )
    loss_artifact = _artifact(
        f"{base}_legacy-projection.loss.json",
        "application/json",
        canonical_json_bytes(loss_report),
    )
    return _finish_export(
        request,
        revision,
        (csv_artifact, loss_artifact),
        manifest_suffix="legacy-projection",
        provenance=provenance,
        manifest_additions={
            "exact_reverse": False,
            "output_label": "legacy_projection_for_review",
            "representation": "headered_csv",
            "loss_report_format": _LOSS_REPORT_FORMAT,
        },
    )


def _human_report_export(
    request: RevisionExportRequest,
    revision: SnapshotRevisionRecord,
    provenance: Mapping[str, Any],
) -> RevisionExport:
    # The canonical JSON is included verbatim in the review report so nested
    # inspection, posting, element, span, substructure, route, and work data do
    # not disappear from this human-facing representation.
    canonical = canonical_json_bytes(revision.canonical_document).decode("utf-8")
    lines = [
        "BridgeGuard human review report",
        "=================================",
        "",
        "Review-only output. This file is not an original NBI source or an FHWA submission.",
        f"Workspace: {revision.workspace_id}",
        f"Snapshot revision: {revision.revision_id}",
        f"State: {revision.state_code}",
        f"Bridge: {revision.bridge_number}",
        f"Inventory year: {revision.data_year}",
        f"Revision number: {revision.revision_number}",
        f"Canonical SHA-256: {revision.content_hash}",
        f"Export profile: {request.profile_id}",
        f"Agency: {request.agency_id}",
        f"Requested by: {request.actor_id}",
        f"Exported at: {request.exported_at}",
        "",
        "Canonical SNBI document (RFC 8785 JSON):",
        canonical,
        "",
    ]
    body = "\n".join(lines).encode("utf-8")
    payload = _artifact(
        f"{_filename_base(revision)}_human-report.txt", "text/plain", body
    )
    return _finish_export(
        request,
        revision,
        (payload,),
        manifest_suffix="human-report",
        provenance=provenance,
        manifest_additions={
            "exact_reverse": False,
            "output_label": "human_readable_review",
            "representation": "plain_text",
        },
    )


def export_snapshot_revision(
    database_path: str,
    artifact_store: SourceArtifactStore,
    request: RevisionExportRequest,
) -> RevisionExport:
    """Export one immutable revision under an explicit deterministic profile."""

    _validate_request(request)
    if not isinstance(artifact_store, SourceArtifactStore):
        raise ExportServiceError(
            500,
            "artifact_store_unavailable",
            "The immutable source artifact store is unavailable.",
        )
    try:
        revision = get_snapshot_revision(
            database_path,
            request.workspace_id,
            request.snapshot_revision_id,
        )
    except RevisionStoreError as exc:
        raise ExportServiceError(exc.status, exc.code, exc.message) from exc
    except (sqlite3.Error, OSError) as exc:
        raise ExportServiceError(
            500,
            "export_repository_error",
            "The requested revision could not be read.",
        ) from exc
    _verify_revision_integrity(revision)
    provenance = _revision_export_provenance(database_path, revision)

    if request.export_kind == "canonical_profile":
        return _canonical_profile_export(request, revision, provenance)
    if request.export_kind == "restore_original":
        return _restore_original_export(
            database_path, artifact_store, request, revision, provenance
        )
    if request.export_kind == "legacy_projection":
        return _legacy_projection_export(request, revision, provenance)
    if request.export_kind == "human_report":
        return _human_report_export(request, revision, provenance)
    # ``_validate_request`` makes this unreachable, but retaining a fail-closed
    # guard prevents a future enum/profile edit from selecting the wrong path.
    raise ExportServiceError(
        422, "unsupported_export_kind", "The requested export kind is not supported."
    )


__all__ = [
    "ExportArtifact",
    "ExportServiceError",
    "RevisionExport",
    "RevisionExportRequest",
    "export_snapshot_revision",
]
