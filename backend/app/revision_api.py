"""Privacy-safe HTTP adapter for immutable canonical bridge revisions.

The adapter deliberately owns the public contract while the revision store
owns transactions and persistence.  Workspace and actor identities are always
derived by the server; neither can be selected by an HTTP client.
"""

from __future__ import annotations

import copy
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Literal
import zipfile
from collections.abc import Callable, Mapping

from fastapi import APIRouter, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.services.conversion.canonical_json import (
    CanonicalJsonError,
    CanonicalJsonLimits,
    canonical_sha256,
    parse_canonical_json,
)
from app.services.conversion.source_artifacts import (
    ArtifactErrorCode,
    SourceArtifactError,
    SourceArtifactStore,
)
from app.services.conversion.revision_validation import (
    RESEARCH_PROFILE,
    derive_server_validation,
)
from app.services.conversion.export_service import (
    ExportServiceError,
    RevisionExport,
    RevisionExportRequest,
    export_snapshot_revision,
)
from app.services.conversion.revision_store import (
    ArchivePublishedSnapshotRequest,
    CanonicalRevisionInput,
    PatchSnapshotRevisionRequest,
    PublishSnapshotRevisionRequest,
    RevisionInputError,
    RevisionStoreError,
    SnapshotRevisionRecord,
    StageCanonicalBatchRequest,
    archive_published_snapshot,
    ensure_workspace,
    get_published_snapshot,
    list_published_snapshots,
    get_snapshot_history,
    get_snapshot_revision,
    get_workspace_state,
    initialize_revision_store,
    patch_snapshot_revision,
    publish_snapshot_revision,
    stage_canonical_batch,
)


SHARED_WORKSPACE_ID = os.getenv(
    "BRIDGEGUARD_WORKSPACE_ID", "shared-research-workspace"
).strip()
SHARED_WORKSPACE_NAME = os.getenv(
    "BRIDGEGUARD_WORKSPACE_NAME", "BridgeGuard research"
).strip()
if not SHARED_WORKSPACE_ID or not SHARED_WORKSPACE_NAME:
    raise RuntimeError("BridgeGuard workspace identity must not be blank")

router = APIRouter(prefix="/api/revisions", tags=["canonical-revisions"])

MAX_CONVERSION_BYTES = min(
    int(os.getenv("BRIDGEGUARD_MAX_CONVERSION_UPLOAD_MB", "50")) * 1024 * 1024,
    64 * 1024 * 1024,
)
MAX_UPLOAD_RECORDS = 10_000

_database_path_provider: Callable[[], str] | None = None


def configure_database_path(provider: Callable[[], str] | None) -> None:
    """Override database-path resolution, primarily for application wiring/tests."""

    global _database_path_provider
    _database_path_provider = provider


def _database_path() -> str:
    if _database_path_provider is not None:
        return str(_database_path_provider())

    # Resolve the application global at request time.  Besides avoiding an
    # import cycle, this preserves test isolation when ``main.USER_DB`` is
    # monkeypatched after the router has been imported.
    try:
        from app import main as app_main

        return str(app_main.USER_DB)
    except (AttributeError, ImportError):
        from app.routers.services.datapaths import USER_DB

        return str(USER_DB)


def _artifact_root() -> Path:
    configured = globals().get("DATA_DIR")
    if configured is not None:
        return Path(configured) / "conversion_sources"
    from app.routers.services.datapaths import DATA_DIR as configured_data_dir

    return Path(configured_data_dir) / "conversion_sources"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RevisionInputBody(_StrictModel):
    canonical_document: dict[str, Any]
    data_year: int | None = None
    source_records: list[dict[str, Any]] = Field(default_factory=list)
    lineage: list[dict[str, Any]] = Field(default_factory=list)


class StageBody(_StrictModel):
    profile_id: str = Field(min_length=1, max_length=128)
    atomic_mode: Literal["batch"]
    expected_data_generation: int = Field(ge=0)
    data_year: int | None = None
    bridges: list[RevisionInputBody] = Field(min_length=1, max_length=10_000)


class PublishBody(_StrictModel):
    reason: str = Field(min_length=1, max_length=2_000)
    expected_data_generation: int = Field(ge=0)
    reviewed_research_warnings: bool = False


class PatchBody(_StrictModel):
    patch: Any
    reason: str = Field(min_length=1, max_length=2_000)
    expected_data_generation: int = Field(ge=0)


class ArchiveBody(_StrictModel):
    reason: str = Field(min_length=1, max_length=2_000)
    expected_data_generation: int = Field(ge=0)


class ExportBody(_StrictModel):
    export_kind: Literal[
        "canonical_profile",
        "restore_original",
        "legacy_projection",
        "human_report",
    ]
    profile_id: str = Field(min_length=1, max_length=128)
    agency_id: str = Field(min_length=1, max_length=128)
    exported_at: str = Field(min_length=1, max_length=64)


def _prepare_store() -> tuple[str, int]:
    database_path = _database_path()
    initialize_revision_store(database_path, app_revision="pr5-revision-http")
    ensure_workspace(database_path, SHARED_WORKSPACE_ID, SHARED_WORKSPACE_NAME)
    state = get_workspace_state(database_path, SHARED_WORKSPACE_ID)
    return database_path, state.data_generation


def _actor(request: Request) -> str:
    user = getattr(request.state, "user", None)
    email = str(getattr(user, "email", "")).strip().lower()
    if not email:
        raise RevisionInputError(401, "actor_required", "Authenticated actor is required")
    return email


def _header_value(value: str | None, *, code: str, label: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise RevisionInputError(400, code, f"{label} is required")
    if len(clean) > 256 or "\r" in clean or "\n" in clean:
        raise RevisionInputError(400, f"invalid_{code}", f"{label} is invalid")
    return clean


def _precondition_headers(
    idempotency_key: str | None,
    if_match: str | None,
    request_id: str | None,
) -> tuple[str, str, str]:
    if not str(idempotency_key or "").strip() or not str(if_match or "").strip():
        raise RevisionInputError(
            428,
            "precondition_required",
            "Idempotency-Key and a strong If-Match value are required",
        )
    key = _header_value(
        idempotency_key, code="idempotency_key_required", label="Idempotency-Key"
    )
    request_token = _header_value(
        request_id, code="request_id_required", label="X-Request-ID"
    )
    match = _parse_strong_etag(if_match)
    return key, match, request_token


def _parse_strong_etag(value: str | None) -> str:
    raw = str(value or "").strip()
    if raw.startswith("W/") or "," in raw or len(raw) < 2:
        raise RevisionInputError(400, "invalid_if_match", "If-Match must be one strong ETag")
    if not (raw.startswith('"') and raw.endswith('"')):
        raise RevisionInputError(400, "invalid_if_match", "If-Match must be quoted")
    token = raw[1:-1]
    if not token or '"' in token or "\r" in token or "\n" in token:
        raise RevisionInputError(400, "invalid_if_match", "If-Match is invalid")
    return token


def _etag(token: str) -> str:
    safe = str(token).replace('"', "")
    return f'"{safe}"'


def _version_headers(generation: int, *, etag: str | None = None) -> dict[str, str]:
    headers = {"X-BridgeGuard-Data-Version": str(generation)}
    if etag is not None:
        headers["ETag"] = _etag(etag)
    return headers


def _encode_snapshot_cursor(
    generation: int, key: tuple[int, int, str]
) -> str:
    payload = json.dumps(
        {"v": 1, "generation": generation, "key": list(key)},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_snapshot_cursor(
    value: str | None,
) -> tuple[int | None, tuple[int, int, str] | None]:
    if value is None:
        return None, None
    raw = str(value).strip()
    if not raw or len(raw) > 512 or any(character.isspace() for character in raw):
        raise RevisionInputError(
            422, "invalid_snapshot_cursor", "Snapshot cursor is invalid"
        )
    try:
        padding = "=" * (-len(raw) % 4)
        decoded = base64.b64decode(
            raw + padding,
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(decoded.decode("ascii"))
        key = payload["key"]
        generation = payload["generation"]
        if (
            payload.get("v") != 1
            or type(generation) is not int
            or generation < 0
            or not isinstance(key, list)
            or len(key) != 3
            or type(key[0]) is not int
            or type(key[1]) is not int
            or type(key[2]) is not str
            or not key[2]
        ):
            raise ValueError("invalid cursor payload")
        return generation, (key[0], key[1], key[2])
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise RevisionInputError(
            422, "invalid_snapshot_cursor", "Snapshot cursor is invalid"
        ) from exc


def _error_payload(
    exc: RevisionStoreError | ExportServiceError,
) -> dict[str, Any]:
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.details:
        detail["details"] = exc.details
    return {"detail": detail}


def _error_response(
    exc: RevisionStoreError | ExportServiceError,
    *,
    database_path: str | None = None,
    generation: int | None = None,
) -> JSONResponse:
    if generation is None and database_path is not None:
        try:
            generation = get_workspace_state(
                database_path, SHARED_WORKSPACE_ID
            ).data_generation
        except RevisionStoreError:
            generation = None
    headers = _version_headers(generation) if generation is not None else None
    return JSONResponse(
        status_code=exc.status,
        content=_error_payload(exc),
        headers=headers,
    )


def _deterministic_export_zip(result: RevisionExport) -> bytes:
    """Package export artifacts without wall-clock or host metadata."""

    output = io.BytesIO()
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_STORED,
        allowZip64=True,
        strict_timestamps=True,
    ) as archive:
        archive.comment = b""
        for artifact in sorted(result.artifacts, key=lambda item: item.filename):
            info = zipfile.ZipInfo(
                filename=artifact.filename,
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.extra = b""
            info.comment = b""
            archive.writestr(info, artifact.content)
    return output.getvalue()


def _export_download_filename(result: RevisionExport) -> str:
    # Export identifiers are content-derived lowercase SHA-256 tokens.  Keep a
    # second fail-closed filter here because this value reaches an HTTP header.
    safe_id = "".join(
        character
        for character in result.export_id.lower()
        if character in "abcdefghijklmnopqrstuvwxyz0123456789-"
    )
    if not safe_id:
        safe_id = "export"
    return f"bridgeguard-{result.export_kind}-{safe_id[:24]}.zip"


def _synthetic_native_source(
    document: Mapping[str, Any],
    *,
    state_code: int,
    bridge_number: str,
    data_year: int,
) -> dict[str, Any]:
    digest = canonical_sha256(
        {
            "state_code": state_code,
            "bridge_number": bridge_number,
            "data_year": data_year,
            "canonical_document": document,
        }
    )
    return {
        "source_artifact_id": f"native-json-{digest[:32]}",
        "source_record_id": f"native-json-record-{digest[:32]}",
        "record_kind": "native_snbi",
        "record_pointer": "",
        "payload": dict(document),
    }


def _server_validation(
    document: Mapping[str, Any],
    *,
    profile_id: str,
    data_year: int,
    source_records: tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    """Server-owned seam kept explicit for auditing and focused tests."""

    return derive_server_validation(
        document,
        profile_id=profile_id,
        data_year=data_year,
        source_records=source_records,
    )


def _stage_server_documents(
    *,
    database_path: str,
    actor_id: str,
    request_id: str,
    idempotency_key: str,
    expected_data_generation: int,
    profile_id: str,
    data_year: int | None,
    items: list[RevisionInputBody],
    artifact_receipts: tuple[Mapping[str, Any], ...] = (),
    write_mode: Literal["create_only", "replace_snapshot"] = "replace_snapshot",
    request_metadata: Mapping[str, Any] | None = None,
    expected_latest_by_key: Mapping[tuple[int, str, int], str | None] | None = None,
    project_to_inventory: bool = False,
):
    """Build server validation/evidence and atomically stage canonical documents."""

    if profile_id not in {RESEARCH_PROFILE, "native_snbi"}:
        raise RevisionInputError(
            422,
            "unsupported_validation_profile",
            "Only research_historical_import and native_snbi are available for revision staging",
        )
    revisions: list[CanonicalRevisionInput] = []
    for item in items:
        document = item.canonical_document
        if "BL01" not in document or "BID01" not in document:
            raise RevisionInputError(
                422,
                "logical_key_required",
                "canonical_document must contain BL01 and BID01",
            )
        item_year = item.data_year if item.data_year is not None else data_year
        if item_year is None:
            raise RevisionInputError(422, "data_year_required", "data_year is required")
        state_code = int(document["BL01"])
        bridge_number = str(document["BID01"]).strip()
        sources = tuple(item.source_records)
        if not sources:
            sources = (
                _synthetic_native_source(
                    document,
                    state_code=state_code,
                    bridge_number=bridge_number,
                    data_year=item_year,
                ),
            )
        validation = _server_validation(
            document,
            profile_id=profile_id,
            data_year=item_year,
            source_records=sources,
        )
        revisions.append(
            CanonicalRevisionInput(
                state_code=state_code,
                bridge_number=bridge_number,
                data_year=item_year,
                canonical_document=document,
                source_records=sources,
                lineage=tuple(item.lineage),
                validation=validation,
                artifact_receipts=artifact_receipts,
                enforce_latest_precondition=expected_latest_by_key is not None,
                expected_latest_content_hash=(
                    expected_latest_by_key.get(
                        (state_code, bridge_number, int(item_year))
                    )
                    if expected_latest_by_key is not None
                    else None
                ),
            )
        )
    return stage_canonical_batch(
        database_path,
        StageCanonicalBatchRequest(
            workspace_id=SHARED_WORKSPACE_ID,
            profile_id=profile_id,
            actor_id=actor_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
            expected_data_generation=expected_data_generation,
            atomic_mode="batch",
            revisions=tuple(revisions),
            write_mode=write_mode,
            request_metadata=request_metadata,
            project_to_inventory=project_to_inventory,
        ),
    )


def stage_server_documents(
    *,
    database_path: str,
    actor_id: str,
    request_id: str,
    idempotency_key: str,
    expected_data_generation: int,
    profile_id: str,
    data_year: int | None,
    items: list[Mapping[str, Any]],
    artifact_receipts: tuple[Mapping[str, Any], ...] = (),
    write_mode: Literal["create_only", "replace_snapshot"] = "replace_snapshot",
    request_metadata: Mapping[str, Any] | None = None,
    expected_latest_by_key: Mapping[tuple[int, str, int], str | None] | None = None,
    project_to_inventory: bool = False,
):
    """Internal adapter used by non-HTTP compatibility ingestion paths."""

    try:
        parsed_items = [RevisionInputBody.model_validate(dict(item)) for item in items]
    except Exception as exc:
        raise RevisionInputError(
            422, "invalid_revision_input", "Revision input is invalid"
        ) from exc
    return _stage_server_documents(
        database_path=database_path,
        actor_id=actor_id,
        request_id=request_id,
        idempotency_key=idempotency_key,
        expected_data_generation=expected_data_generation,
        profile_id=profile_id,
        data_year=data_year,
        items=parsed_items,
        artifact_receipts=artifact_receipts,
        write_mode=write_mode,
        request_metadata=request_metadata,
        expected_latest_by_key=expected_latest_by_key,
        project_to_inventory=project_to_inventory,
    )


def _public_source_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Return only immutable, path-free source metadata."""

    return {
        "artifact_id": str(receipt.get("artifact_id") or ""),
        "sha256": str(receipt.get("sha256") or ""),
        "size_bytes": int(receipt.get("size_bytes") or 0),
        "original_filename": str(receipt.get("original_filename") or ""),
        "created_at": str(receipt.get("created_at") or ""),
        "status": str(receipt.get("status") or "accepted"),
    }


def _public_stage_result(
    database_path: str,
    result: Any,
    *,
    source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    revisions: list[dict[str, Any]] = []
    for item in result.revisions:
        stored = get_snapshot_revision(
            database_path, SHARED_WORKSPACE_ID, item.revision_id
        )
        validation = dict(stored.validation or {})
        issues = list(validation.pop("issues", []) or [])
        revisions.append(
            {
                "revision_id": item.revision_id,
                "state_code": item.state_code,
                "bridge_number": item.bridge_number,
                "data_year": item.data_year,
                "revision_number": item.revision_number,
                "content_hash": item.content_hash,
                "outcome": "created" if item.created else "noop",
                "created_at": item.created_at,
                "diff": list(item.diff),
                "validation_status": validation.get("status"),
                "validation_summary": validation,
                "validation_issues": issues,
            }
        )
    payload: dict[str, Any] = {
        "status": "staged",
        "batch_id": result.batch_id,
        "data_generation": result.data_generation,
        "revisions": revisions,
    }
    if source is not None:
        payload["source"] = _public_source_receipt(source)
    return payload


def _bounded_upload_digest(upload: UploadFile) -> tuple[str, int]:
    """Hash an upload without accepting it or retaining its bytes in memory."""

    digest = hashlib.sha256()
    size = 0
    upload.file.seek(0)
    while True:
        chunk = upload.file.read(256 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_CONVERSION_BYTES:
            raise SourceArtifactError(
                ArtifactErrorCode.UPLOAD_TOO_LARGE,
                "The upload exceeds the configured byte limit.",
                details={"max_bytes": MAX_CONVERSION_BYTES},
            )
        digest.update(chunk)
    upload.file.seek(0)
    if size == 0:
        raise SourceArtifactError(
            ArtifactErrorCode.EMPTY_UPLOAD,
            "The upload contains no bytes.",
        )
    return digest.hexdigest(), size


def _upload_replay(
    database_path: str,
    *,
    idempotency_key: str,
    actor_id: str,
    sha256: str,
    size_bytes: int,
    data_year: int,
) -> tuple[Any, dict[str, Any]] | None:
    """Return an exact multipart replay or reject reuse with changed inputs."""

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        batch = connection.execute(
            "SELECT id, actor_id, counts_json FROM conversion_batches "
            "WHERE workspace_id=? AND idempotency_key=?",
            (SHARED_WORKSPACE_ID, idempotency_key),
        ).fetchone()
        if batch is None:
            return None
        evidence = connection.execute(
            "SELECT DISTINCT sa.id AS artifact_id, sa.sha256, sa.byte_size, "
            "sa.original_filename, sr.data_year "
            "FROM eligibility_decisions ed "
            "JOIN source_records sr ON sr.workspace_id=ed.workspace_id "
            "AND sr.id=ed.source_record_id "
            "JOIN source_artifacts sa ON sa.workspace_id=sr.workspace_id "
            "AND sa.id=sr.source_artifact_id "
            "WHERE ed.workspace_id=? AND ed.conversion_batch_id=? "
            "AND sa.artifact_kind='immutable_json_upload' "
            "AND sr.source_role IN ('json_upload_record', 'nbi_json_upload_record') "
            "ORDER BY sr.record_ordinal, sa.id LIMIT 1",
            (SHARED_WORKSPACE_ID, str(batch["id"])),
        ).fetchone()
        if (
            evidence is None
            or str(batch["actor_id"]) != actor_id
            or str(evidence["sha256"]) != sha256
            or int(evidence["byte_size"]) != size_bytes
            or int(evidence["data_year"]) != data_year
        ):
            raise RevisionInputError(
                409,
                "idempotency_payload_conflict",
                "The idempotency key was already used for a different upload",
            )
        stored = json.loads(str(batch["counts_json"]))["result"]
        from app.services.conversion.revision_store import _stage_result_from_payload

        result = _stage_result_from_payload(stored)
        receipt = SourceArtifactStore(
            _artifact_root(), max_bytes=MAX_CONVERSION_BYTES
        ).get(str(evidence["artifact_id"]))
        source = receipt.to_public_dict()
        return result, source
    finally:
        connection.close()


def _strict_uploaded_documents(raw: bytes) -> list[dict[str, Any]]:
    try:
        parsed = parse_canonical_json(
            raw,
            CanonicalJsonLimits(
                max_depth=64,
                max_nodes=1_000_000,
                max_bytes=MAX_CONVERSION_BYTES,
            ),
        )
    except CanonicalJsonError as exc:
        code = {
            "duplicate_key": "duplicate_json_key",
            "non_finite_number": "invalid_json_constant",
        }.get(exc.code, "invalid_json")
        raise RevisionInputError(400, code, exc.message) from exc
    if isinstance(parsed, Mapping) and isinstance(parsed.get("bridges"), list):
        parsed = parsed["bridges"]
    elif isinstance(parsed, Mapping):
        parsed = [parsed]
    if not isinstance(parsed, list) or not parsed:
        raise RevisionInputError(
            400, "bridge_array_required", "JSON must contain at least one bridge object"
        )
    if len(parsed) > MAX_UPLOAD_RECORDS:
        raise RevisionInputError(
            413,
            "record_limit_exceeded",
            f"A batch may contain at most {MAX_UPLOAD_RECORDS} bridge records",
        )
    if not all(isinstance(item, Mapping) for item in parsed):
        raise RevisionInputError(
            400, "bridge_object_required", "Each bridge entry must be an object"
        )
    return [dict(item) for item in parsed]


def _embedded_source_receipt(
    store: SourceArtifactStore,
    claimed: Mapping[str, Any],
    *,
    artifact_kind: str,
    media_type: str,
) -> dict[str, Any]:
    """Resolve converter-cited evidence without trusting JSON metadata.

    The strict converter stores source bytes before parsing and embeds a
    path-free receipt in ``_SourceData``.  A later Loader upload may link that
    already-accepted artifact, but only after the content store independently
    resolves the opaque ID and every integrity-bearing claim agrees.
    """

    artifact_id = str(claimed.get("artifact_id") or "")
    if not artifact_id:
        raise RevisionInputError(
            422,
            "embedded_source_artifact_invalid",
            "Embedded source evidence is missing its artifact identity",
        )
    try:
        receipt = store.get(artifact_id)
    except SourceArtifactError as exc:
        raise RevisionInputError(
            422,
            "embedded_source_artifact_unavailable",
            "Embedded source evidence is not available in immutable storage",
        ) from exc
    comparisons = {
        "job_id": receipt.job_id,
        "sha256": receipt.sha256,
        "size_bytes": receipt.size_bytes,
        "original_filename": receipt.original_filename,
        "created_at": receipt.created_at,
        "status": receipt.status,
    }
    for field, actual in comparisons.items():
        if field not in claimed or str(claimed.get(field)) != str(actual):
            raise RevisionInputError(
                422,
                "embedded_source_artifact_mismatch",
                "Embedded source evidence does not match immutable storage",
                details={"field": field},
            )
    return {
        **receipt.to_public_dict(),
        "media_type": media_type,
        "artifact_kind": artifact_kind,
    }


def _nbi_source_media_type(filename: str) -> str:
    return "text/csv" if str(filename).lower().endswith(".csv") else "text/plain"


def _converter_source_evidence(
    store: SourceArtifactStore,
    source_data: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return trusted original receipts and per-record revision evidence."""

    if not isinstance(source_data, Mapping):
        return [], []
    receipts: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []

    nbi_claim = source_data.get("artifact")
    if isinstance(nbi_claim, Mapping):
        nbi_receipt = _embedded_source_receipt(
            store,
            nbi_claim,
            artifact_kind="legacy_nbi_source",
            media_type=_nbi_source_media_type(
                str(nbi_claim.get("original_filename") or "")
            ),
        )
        receipts.append(nbi_receipt)
        record_values: list[tuple[str, Any]] = [
            ("nbi_over_record", source_data.get("over_record"))
        ]
        under_values = source_data.get("under_records")
        if isinstance(under_values, list):
            record_values.extend(("nbi_under_record", value) for value in under_values)
        for fallback_ordinal, (record_kind, raw_record) in enumerate(record_values):
            if not isinstance(raw_record, Mapping):
                continue
            source_record_id = str(raw_record.get("source_record_id") or "")
            if not source_record_id:
                raise RevisionInputError(
                    422,
                    "embedded_source_record_invalid",
                    "Embedded NBI record evidence is missing its identity",
                )
            source_order = raw_record.get("source_order")
            if not isinstance(source_order, int) or isinstance(source_order, bool):
                source_order = fallback_ordinal
            records.append(
                {
                    "source_artifact_id": nbi_receipt["artifact_id"],
                    "source_record_id": source_record_id,
                    "record_kind": record_kind,
                    "record_pointer": f"/records/{source_order}",
                    "payload": dict(raw_record),
                }
            )

    element_claim = source_data.get("element_artifact")
    if isinstance(element_claim, Mapping) and element_claim:
        element_receipt = _embedded_source_receipt(
            store,
            element_claim,
            artifact_kind="legacy_element_xml_source",
            media_type="application/xml",
        )
        receipts.append(element_receipt)
        indices = source_data.get("element_record_indices")
        if isinstance(indices, list):
            for record_index in indices:
                if not isinstance(record_index, int) or isinstance(record_index, bool):
                    raise RevisionInputError(
                        422,
                        "embedded_element_record_invalid",
                        "Embedded element record evidence has an invalid index",
                    )
                records.append(
                    {
                        "source_artifact_id": element_receipt["artifact_id"],
                        "source_record_id": (
                            f"element:{element_receipt['sha256']}:{record_index}"
                        ),
                        "record_kind": "legacy_element_record",
                        "record_pointer": f"/FHWAED/{record_index}",
                        "payload": {"record_index": record_index},
                    }
                )
    return receipts, records


def _merge_patch_preview(target: Any, patch: Any) -> Any:
    """Apply RFC 7396 for validation before the revision-store transaction."""

    if not isinstance(patch, Mapping):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, Mapping) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(str(key), None)
        else:
            result[str(key)] = _merge_patch_preview(result.get(str(key)), value)
    return result


def _batch_key_exists(database_path: str, idempotency_key: str) -> bool:
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute(
            "SELECT 1 FROM conversion_batches WHERE workspace_id=? AND idempotency_key=?",
            (SHARED_WORKSPACE_ID, idempotency_key),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def _operation_request_identity(
    *,
    operation: str,
    idempotency_key: str,
    request_token: str,
    payload: Mapping[str, Any],
) -> tuple[str, str]:
    """Create a privacy-safe idempotency identity stored in mutation audit."""

    key_hash = canonical_sha256({"idempotency_key": idempotency_key})
    payload_hash = canonical_sha256(payload)
    prefix = f"revision-http-v1:{operation}:{key_hash}:"
    return prefix, f"{prefix}{payload_hash}:{request_token}"


def _idempotency_audit(
    database_path: str,
    *,
    action: str,
    prefix: str,
    service_request_id: str,
) -> sqlite3.Row | None:
    """Return an exact replay row and reject key reuse with changed inputs."""

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM mutation_audit WHERE workspace_id=? AND action=? "
            "AND request_id LIKE ? ORDER BY occurred_at, id LIMIT 1",
            (SHARED_WORKSPACE_ID, action, f"{prefix}%"),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    expected_payload_hash = service_request_id[len(prefix) :].split(":", 1)[0]
    stored_payload_hash = str(row["request_id"])[len(prefix) :].split(":", 1)[0]
    if stored_payload_hash != expected_payload_hash:
        raise RevisionInputError(
            409,
            "idempotency_payload_conflict",
            "The idempotency key was already used for a different payload",
        )
    return row


def _public_revision(
    record: SnapshotRevisionRecord, *, status: str | None = None
) -> dict[str, Any]:
    """Return the active document without provenance paths or audit identities."""

    validation = dict(record.validation or {})
    validation_issues = list(validation.pop("issues", []) or [])
    return {
        "revision_id": record.revision_id,
        "state_code": record.state_code,
        "bridge_number": record.bridge_number,
        "data_year": record.data_year,
        "revision_number": record.revision_number,
        "status": status or record.lifecycle_state,
        "content_hash": record.content_hash,
        "profile_id": record.profile_id,
        "canonical_document": record.canonical_document,
        "validation": validation,
        "validation_issues": validation_issues,
        "created_at": record.created_at,
    }


def _public_history_revision(record: SnapshotRevisionRecord) -> dict[str, Any]:
    validation = record.validation or {}
    public_validation = {
        key: value for key, value in validation.items() if key != "issues"
    }
    return {
        "revision_id": record.revision_id,
        "revision_number": record.revision_number,
        "status": record.lifecycle_state,
        "content_hash": record.content_hash,
        "parent_revision_id": record.parent_revision_id,
        "profile_id": record.profile_id,
        "validation_status": validation.get("status"),
        "validation": public_validation,
        "created_at": record.created_at,
    }


def _assert_publishable(
    record: SnapshotRevisionRecord, *, reviewed_research_warnings: bool
) -> None:
    validation = record.validation or {}
    if validation.get("validation_owner") != "server":
        raise RevisionInputError(
            422,
            "untrusted_validation_evidence",
            "Only server-owned validation evidence can authorize publication",
        )
    issues = validation.get("issues") or []
    severities = {
        str(issue.get("severity", "")).strip().lower()
        for issue in issues
        if isinstance(issue, Mapping)
    }
    if severities.intersection({"critical", "error", "fatal"}):
        raise RevisionInputError(
            422,
            "critical_validation_issues",
            "Revision has critical validation issues and cannot be published",
        )
    has_warnings = bool(severities.intersection({"warning", "warn"}))
    profile = str(validation.get("profile_id") or record.profile_id).lower()
    if has_warnings and "research" in profile and not reviewed_research_warnings:
        raise RevisionInputError(
            422,
            "research_review_required",
            "Research-profile warnings require explicit editor review",
        )


def _assert_etag_matches(current: SnapshotRevisionRecord | None, supplied: str) -> None:
    expected = "none" if current is None else current.content_hash
    if supplied != expected:
        raise RevisionInputError(409, "stale_revision", "Published revision changed")


@router.post("/stage-upload")
def stage_revision_upload(
    request: Request,
    file: UploadFile = File(...),
    data_year: int | None = Form(default=None),
    expected_data_generation: int | None = Form(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    request_id: str | None = Header(default=None, alias="X-Request-ID"),
):
    """Preserve exact JSON bytes, validate on the server, and stage only."""

    database_path: str | None = None
    generation: int | None = None
    try:
        key = _header_value(
            idempotency_key,
            code="idempotency_key_required",
            label="Idempotency-Key",
        )
        request_token = _header_value(
            request_id, code="request_id_required", label="X-Request-ID"
        )
        if data_year is None:
            raise RevisionInputError(422, "data_year_required", "data_year is required")
        if expected_data_generation is None:
            raise RevisionInputError(
                422,
                "expected_data_generation_required",
                "expected_data_generation is required",
            )
        if not 1990 <= data_year <= 2100:
            raise RevisionInputError(
                422, "invalid_data_year", "data_year must be between 1990 and 2100"
            )

        database_path, generation = _prepare_store()
        actor_id = _actor(request)
        sha256, size_bytes = _bounded_upload_digest(file)
        replay = _upload_replay(
            database_path,
            idempotency_key=key,
            actor_id=actor_id,
            sha256=sha256,
            size_bytes=size_bytes,
            data_year=data_year,
        )
        if replay is not None:
            result, source = replay
            payload = _public_stage_result(
                database_path, result, source=source
            )
            return JSONResponse(
                status_code=200,
                content=payload,
                headers=_version_headers(
                    result.data_generation, etag=result.request_hash
                ),
            )
        if generation != expected_data_generation:
            raise RevisionInputError(
                409,
                "stale_data_generation",
                "The workspace data generation changed; refresh and retry",
            )

        file.file.seek(0)
        raw = file.file.read(MAX_CONVERSION_BYTES + 1)
        file.file.seek(0)
        if len(raw) != size_bytes:
            raise RevisionInputError(
                409, "upload_changed", "The upload changed while it was being staged"
            )
        documents = _strict_uploaded_documents(raw)

        store = SourceArtifactStore(
            _artifact_root(), max_bytes=MAX_CONVERSION_BYTES
        )
        receipt = store.ingest(
            file.file,
            original_filename=file.filename or "bridges.json",
        )
        if receipt.sha256 != sha256 or receipt.size_bytes != size_bytes:
            raise RevisionInputError(
                409, "upload_changed", "The upload changed while it was being stored"
            )

        artifact_receipts: dict[str, dict[str, Any]] = {
            receipt.artifact_id: {
                **receipt.to_public_dict(),
                "media_type": "application/json",
                "artifact_kind": "immutable_json_upload",
            }
        }
        items: list[dict[str, Any]] = []
        for index, source_document in enumerate(documents):
            canonical_document = {
                key_name: value
                for key_name, value in source_document.items()
                if not str(key_name).startswith("_")
            }
            lineage_raw = source_document.get("_TransitionLineage")
            lineage: list[dict[str, Any]] = []
            if isinstance(lineage_raw, Mapping):
                for pointer, value in sorted(lineage_raw.items()):
                    if isinstance(value, Mapping):
                        normalized_lineage = {
                            **dict(value),
                            "target_pointer": value.get("target_pointer") or pointer,
                        }
                        source_record_ids = value.get("source_record_ids")
                        if (
                            not normalized_lineage.get("source_record_id")
                            and isinstance(source_record_ids, list)
                            and source_record_ids
                        ):
                            for source_record_id in source_record_ids:
                                lineage.append(
                                    {
                                        **normalized_lineage,
                                        "source_record_id": source_record_id,
                                    }
                                )
                        else:
                            lineage.append(normalized_lineage)
            elif isinstance(lineage_raw, list):
                lineage = [dict(value) for value in lineage_raw if isinstance(value, Mapping)]

            source_data = source_document.get("_SourceData")
            record_kind = "json_upload_record"
            if isinstance(source_data, Mapping) and (
                source_data.get("over_record") is not None
                or source_data.get("nbi") is not None
            ):
                record_kind = "nbi_json_upload_record"
            original_receipts, original_records = _converter_source_evidence(
                store, source_data
            )
            for original_receipt in original_receipts:
                artifact_receipts[str(original_receipt["artifact_id"])] = original_receipt
            items.append(
                {
                    "canonical_document": canonical_document,
                    "data_year": data_year,
                    "source_records": [
                        {
                            "source_artifact_id": receipt.artifact_id,
                            "source_record_id": f"{receipt.artifact_id}:{index}",
                            "record_kind": record_kind,
                            "record_pointer": f"/{index}",
                            "payload": source_document,
                        },
                        *original_records,
                    ],
                    "lineage": lineage,
                }
            )

        result = stage_server_documents(
            database_path=database_path,
            actor_id=actor_id,
            request_id=request_token,
            idempotency_key=key,
            expected_data_generation=expected_data_generation,
            profile_id=RESEARCH_PROFILE,
            data_year=data_year,
            items=items,
            artifact_receipts=tuple(artifact_receipts.values()),
        )
        payload = _public_stage_result(
            database_path, result, source=receipt.to_public_dict()
        )
        return JSONResponse(
            status_code=201,
            content=payload,
            headers=_version_headers(result.data_generation, etag=result.request_hash),
        )
    except SourceArtifactError as exc:
        status = 413 if exc.code == ArtifactErrorCode.UPLOAD_TOO_LARGE.value else 400
        error = RevisionInputError(status, exc.code, str(exc), details=exc.details)
        return _error_response(error, database_path=database_path, generation=generation)
    except RevisionStoreError as exc:
        return _error_response(exc, database_path=database_path, generation=generation)
    except (TypeError, ValueError) as exc:
        error = RevisionInputError(422, "invalid_upload", str(exc))
        return _error_response(error, database_path=database_path, generation=generation)


def _publish_response_from_audit(
    audit: sqlite3.Row,
    candidate: SnapshotRevisionRecord,
) -> JSONResponse:
    metadata = json.loads(str(audit["metadata_json"]))
    generation = int(metadata["data_generation"])
    logical_key = metadata.get("logical_key") or [
        candidate.state_code,
        candidate.bridge_number,
        candidate.data_year,
    ]
    return JSONResponse(
        status_code=200,
        content={
            "status": "published",
            "revision_id": candidate.revision_id,
            "previous_revision_id": metadata.get("previous_revision_id"),
            "state_code": int(logical_key[0]),
            "bridge_number": str(logical_key[1]),
            "data_year": int(logical_key[2]),
            "data_generation": generation,
            "published_at": str(audit["occurred_at"]),
        },
        headers=_version_headers(generation, etag=candidate.content_hash),
    )


def _archive_response_from_audit(audit: sqlite3.Row) -> JSONResponse:
    metadata = json.loads(str(audit["metadata_json"]))
    result = metadata.get("result")
    if not isinstance(result, Mapping):
        raise RevisionInputError(
            409,
            "request_replay_unavailable",
            "The archive completed but its replay result is unavailable",
        )
    generation = int(result["data_generation"])
    return JSONResponse(
        status_code=200,
        content={
            "status": "archived",
            "revision_id": str(result["revision_id"]),
            "state_code": int(result["state_code"]),
            "bridge_number": str(result["bridge_number"]),
            "data_year": int(result["data_year"]),
            "data_generation": generation,
            "archived_at": str(result["archived_at"]),
        },
        headers=_version_headers(
            generation,
            etag=str(audit["before_sha256"]),
        ),
    )


def _patch_response_from_audit(
    database_path: str,
    audit: sqlite3.Row,
    *,
    data_generation: int,
) -> JSONResponse:
    metadata = json.loads(str(audit["metadata_json"]))
    record = get_snapshot_revision(
        database_path, SHARED_WORKSPACE_ID, str(audit["entity_id"])
    )
    return JSONResponse(
        status_code=201,
        content={
            "status": "staged",
            "revision_id": record.revision_id,
            "previous_revision_id": metadata.get("previous_revision_id"),
            "state_code": record.state_code,
            "bridge_number": record.bridge_number,
            "data_year": record.data_year,
            "revision_number": record.revision_number,
            "content_hash": record.content_hash,
            "data_generation": data_generation,
            "diff": metadata.get("diff") or [],
            "validation": {
                key: value
                for key, value in dict(record.validation or {}).items()
                if key != "issues"
            },
            "validation_issues": list(
                (record.validation or {}).get("issues") or []
            ),
            "created_at": record.created_at,
        },
        headers=_version_headers(data_generation, etag=record.content_hash),
    )


@router.post("/stage")
def stage_revisions(
    body: StageBody,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    request_id: str | None = Header(default=None, alias="X-Request-ID"),
):
    database_path: str | None = None
    generation: int | None = None
    try:
        key = _header_value(
            idempotency_key,
            code="idempotency_key_required",
            label="Idempotency-Key",
        )
        request_token = _header_value(
            request_id, code="request_id_required", label="X-Request-ID"
        )
        database_path, generation = _prepare_store()
        existed = _batch_key_exists(database_path, key)
        result = _stage_server_documents(
            database_path=database_path,
            actor_id=_actor(request),
            request_id=request_token,
            idempotency_key=key,
            expected_data_generation=body.expected_data_generation,
            profile_id=body.profile_id,
            data_year=body.data_year,
            items=list(body.bridges),
        )
        response_revisions: list[dict[str, Any]] = []
        for item in result.revisions:
            stored = get_snapshot_revision(
                database_path, SHARED_WORKSPACE_ID, item.revision_id
            )
            validation = dict(stored.validation or {})
            issues = list(validation.pop("issues", []) or [])
            response_revisions.append(
                {
                    "revision_id": item.revision_id,
                    "state_code": item.state_code,
                    "bridge_number": item.bridge_number,
                    "data_year": item.data_year,
                    "revision_number": item.revision_number,
                    "content_hash": item.content_hash,
                    "outcome": "created" if item.created else "noop",
                    "created_at": item.created_at,
                    "diff": list(item.diff),
                    "validation_status": validation.get("status"),
                    "validation_summary": validation,
                    "validation_issues": issues,
                }
            )
        payload = {
            "status": "staged",
            "batch_id": result.batch_id,
            "data_generation": result.data_generation,
            "revisions": response_revisions,
        }
        return JSONResponse(
            status_code=200 if existed else 201,
            content=payload,
            headers=_version_headers(result.data_generation, etag=result.request_hash),
        )
    except RevisionStoreError as exc:
        return _error_response(exc, database_path=database_path, generation=generation)
    except (TypeError, ValueError) as exc:
        error = RevisionInputError(422, "invalid_logical_key", str(exc))
        return _error_response(error, database_path=database_path, generation=generation)


@router.post("/{revision_id}/publish")
def publish_revision(
    revision_id: str,
    body: PublishBody,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
    request_id: str | None = Header(default=None, alias="X-Request-ID"),
):
    database_path: str | None = None
    generation: int | None = None
    try:
        key, supplied_etag, request_token = _precondition_headers(
            idempotency_key, if_match, request_id
        )
        database_path, generation = _prepare_store()
        actor = _actor(request)
        idempotency_prefix, service_request_id = _operation_request_identity(
            operation="publish",
            idempotency_key=key,
            request_token=request_token,
            payload={
                "actor_id": actor,
                "revision_id": revision_id,
                "if_match": supplied_etag,
                "body": body.model_dump(mode="json"),
            },
        )
        replay = _idempotency_audit(
            database_path,
            action="publish_revision",
            prefix=idempotency_prefix,
            service_request_id=service_request_id,
        )
        candidate = get_snapshot_revision(
            database_path, SHARED_WORKSPACE_ID, revision_id
        )
        if replay is not None:
            return _publish_response_from_audit(replay, candidate)
        current = get_published_snapshot(
            database_path,
            SHARED_WORKSPACE_ID,
            candidate.state_code,
            candidate.bridge_number,
            candidate.data_year,
        )
        _assert_etag_matches(current, supplied_etag)
        _assert_publishable(
            candidate,
            reviewed_research_warnings=body.reviewed_research_warnings,
        )
        publish_snapshot_revision(
            database_path,
            PublishSnapshotRevisionRequest(
                workspace_id=SHARED_WORKSPACE_ID,
                revision_id=revision_id,
                actor_id=actor,
                request_id=service_request_id,
                reason=body.reason,
                expected_revision_id=current.revision_id if current is not None else None,
                expected_data_generation=body.expected_data_generation,
            ),
        )
        audit = _idempotency_audit(
            database_path,
            action="publish_revision",
            prefix=idempotency_prefix,
            service_request_id=service_request_id,
        )
        if audit is None:  # pragma: no cover - a committed publish always audits
            raise RevisionInputError(500, "audit_missing", "Publication audit is missing")
        return _publish_response_from_audit(audit, candidate)
    except RevisionStoreError as exc:
        return _error_response(exc, database_path=database_path, generation=generation)


@router.post(
    "/snapshots/{state_code}/{bridge_number}/{data_year}/{revision_id}/archive"
)
def archive_revision(
    state_code: int,
    bridge_number: str,
    data_year: int,
    revision_id: str,
    body: ArchiveBody,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
    request_id: str | None = Header(default=None, alias="X-Request-ID"),
):
    """Unpublish one exact active revision while retaining immutable evidence."""

    database_path: str | None = None
    generation: int | None = None
    try:
        key, supplied_etag, request_token = _precondition_headers(
            idempotency_key, if_match, request_id
        )
        database_path, generation = _prepare_store()
        actor = _actor(request)
        idempotency_prefix, service_request_id = _operation_request_identity(
            operation="archive",
            idempotency_key=key,
            request_token=request_token,
            payload={
                "actor_id": actor,
                "state_code": state_code,
                "bridge_number": bridge_number,
                "data_year": data_year,
                "revision_id": revision_id,
                "if_match": supplied_etag,
                "body": body.model_dump(mode="json"),
            },
        )
        replay = _idempotency_audit(
            database_path,
            action="archive_revision",
            prefix=idempotency_prefix,
            service_request_id=service_request_id,
        )
        if replay is not None:
            return _archive_response_from_audit(replay)

        candidate = get_snapshot_revision(
            database_path, SHARED_WORKSPACE_ID, revision_id
        )
        if (
            candidate.state_code != state_code
            or candidate.bridge_number != bridge_number
            or candidate.data_year != data_year
        ):
            raise RevisionInputError(404, "revision_not_found", "Revision not found")
        current = get_published_snapshot(
            database_path,
            SHARED_WORKSPACE_ID,
            state_code,
            bridge_number,
            data_year,
        )
        if current is None:
            raise RevisionInputError(
                409,
                "snapshot_not_published",
                "The requested revision is not currently published",
            )
        if current.revision_id != revision_id:
            raise RevisionInputError(
                409, "stale_revision", "Published revision changed"
            )
        _assert_etag_matches(current, supplied_etag)

        archive_published_snapshot(
            database_path,
            ArchivePublishedSnapshotRequest(
                workspace_id=SHARED_WORKSPACE_ID,
                state_code=state_code,
                bridge_number=bridge_number,
                data_year=data_year,
                revision_id=revision_id,
                actor_id=actor,
                request_id=service_request_id,
                reason=body.reason,
                expected_data_generation=body.expected_data_generation,
            ),
        )
        audit = _idempotency_audit(
            database_path,
            action="archive_revision",
            prefix=idempotency_prefix,
            service_request_id=service_request_id,
        )
        if audit is None:  # pragma: no cover - committed archive always audits
            raise RevisionInputError(500, "audit_missing", "Archive audit is missing")
        return _archive_response_from_audit(audit)
    except RevisionStoreError as exc:
        return _error_response(exc, database_path=database_path, generation=generation)


@router.patch("/snapshots/{state_code}/{bridge_number}/{data_year}")
def patch_revision(
    state_code: int,
    bridge_number: str,
    data_year: int,
    body: PatchBody,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
    request_id: str | None = Header(default=None, alias="X-Request-ID"),
):
    database_path: str | None = None
    generation: int | None = None
    try:
        key, supplied_etag, request_token = _precondition_headers(
            idempotency_key, if_match, request_id
        )
        database_path, generation = _prepare_store()
        actor = _actor(request)
        idempotency_prefix, service_request_id = _operation_request_identity(
            operation="patch",
            idempotency_key=key,
            request_token=request_token,
            payload={
                "actor_id": actor,
                "state_code": state_code,
                "bridge_number": bridge_number,
                "data_year": data_year,
                "if_match": supplied_etag,
                "body": body.model_dump(mode="json"),
            },
        )
        replay = _idempotency_audit(
            database_path,
            action="patch_revision",
            prefix=idempotency_prefix,
            service_request_id=service_request_id,
        )
        if replay is not None:
            return _patch_response_from_audit(
                database_path,
                replay,
                data_generation=body.expected_data_generation,
            )
        current = get_published_snapshot(
            database_path,
            SHARED_WORKSPACE_ID,
            state_code,
            bridge_number,
            data_year,
        )
        if current is None:
            raise RevisionInputError(
                404, "published_snapshot_not_found", "Published snapshot not found"
            )
        _assert_etag_matches(current, supplied_etag)
        merged_document = _merge_patch_preview(current.canonical_document, body.patch)
        if not isinstance(merged_document, Mapping):
            raise RevisionInputError(
                422,
                "invalid_canonical_document",
                "Patch result must be an object",
            )
        validation = _server_validation(
            merged_document,
            profile_id=current.profile_id,
            data_year=data_year,
            source_records=tuple(current.source_records),
        )
        result = patch_snapshot_revision(
            database_path,
            PatchSnapshotRevisionRequest(
                workspace_id=SHARED_WORKSPACE_ID,
                state_code=state_code,
                bridge_number=bridge_number,
                data_year=data_year,
                patch=body.patch,
                actor_id=actor,
                request_id=service_request_id,
                reason=body.reason,
                expected_revision_id=current.revision_id,
                expected_data_generation=body.expected_data_generation,
                validation=validation,
            ),
        )
        audit = _idempotency_audit(
            database_path,
            action="patch_revision",
            prefix=idempotency_prefix,
            service_request_id=service_request_id,
        )
        if audit is None:
            # Semantic no-ops create neither a revision nor a mutation audit.
            return JSONResponse(
                status_code=200,
                content={
                    "status": "staged",
                    "revision_id": result.revision_id,
                    "previous_revision_id": result.previous_revision_id,
                    "state_code": result.state_code,
                    "bridge_number": result.bridge_number,
                    "data_year": result.data_year,
                    "revision_number": result.revision_number,
                    "content_hash": result.content_hash,
                    "data_generation": result.data_generation,
                    "diff": list(result.diff),
                    "validation": {
                        key: value
                        for key, value in dict(validation).items()
                        if key != "issues"
                    },
                    "validation_issues": list(validation.get("issues") or []),
                    "created_at": result.created_at,
                },
                headers=_version_headers(
                    result.data_generation, etag=result.content_hash
                ),
            )
        return _patch_response_from_audit(
            database_path,
            audit,
            data_generation=result.data_generation,
        )
    except RevisionStoreError as exc:
        return _error_response(exc, database_path=database_path, generation=generation)


@router.post("/{revision_id}/exports")
def export_revision(
    revision_id: str,
    body: ExportBody,
    request: Request,
):
    """Download one immutable revision as a deterministic ZIP package."""

    database_path: str | None = None
    generation: int | None = None
    try:
        if request.query_params:
            raise RevisionInputError(
                422,
                "export_parameters_not_selectable",
                "Workspace, actor, and other export query parameters are not accepted.",
            )
        database_path, generation = _prepare_store()
        actor_id = _actor(request)
        artifact_store = SourceArtifactStore(
            _artifact_root(), max_bytes=MAX_CONVERSION_BYTES
        )
        result = export_snapshot_revision(
            database_path,
            artifact_store,
            RevisionExportRequest(
                workspace_id=SHARED_WORKSPACE_ID,
                snapshot_revision_id=revision_id,
                export_kind=body.export_kind,
                profile_id=body.profile_id,
                agency_id=body.agency_id,
                actor_id=actor_id,
                exported_at=body.exported_at,
            ),
        )
        archive = _deterministic_export_zip(result)
        headers = _version_headers(generation)
        headers.update(
            {
                "Content-Disposition": (
                    f'attachment; filename="{_export_download_filename(result)}"'
                ),
                "X-BridgeGuard-Export-ID": result.export_id,
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
            }
        )
        return Response(
            status_code=200,
            content=archive,
            media_type="application/zip",
            headers=headers,
        )
    except ExportServiceError as exc:
        return _error_response(
            exc, database_path=database_path, generation=generation
        )
    except SourceArtifactError as exc:
        error = ExportServiceError(
            500,
            "artifact_store_unavailable",
            "The immutable source artifact store is unavailable.",
            details={"artifact_code": exc.code},
        )
        return _error_response(
            error, database_path=database_path, generation=generation
        )
    except RevisionStoreError as exc:
        return _error_response(exc, database_path=database_path, generation=generation)


@router.get("/snapshots")
def published_revision_list(
    state_code: int | None = Query(default=None, ge=0),
    data_year: int | None = Query(default=None, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None, max_length=512),
    workspace_id: str | None = None,
):
    """List exact active revision pointers through a bounded viewer-safe API."""

    database_path: str | None = None
    generation: int | None = None
    try:
        if workspace_id is not None:
            raise RevisionInputError(
                422,
                "workspace_not_selectable",
                "Workspace selection is not available on this endpoint",
            )
        database_path, _ = _prepare_store()
        cursor_generation, after = _decode_snapshot_cursor(cursor)
        page = list_published_snapshots(
            database_path,
            SHARED_WORKSPACE_ID,
            state_code=state_code,
            data_year=data_year,
            limit=limit,
            after=after,
            expected_data_generation=cursor_generation,
        )
        generation = page.data_generation
        snapshots = [
            {
                "revision_id": item.revision_id,
                "state_code": item.state_code,
                "bridge_number": item.bridge_number,
                "bridge_name": item.bridge_name,
                "data_year": item.data_year,
                "revision_number": item.revision_number,
                "content_hash": item.content_hash,
                "profile_id": item.profile_id,
                "validation_status": item.validation_status,
                "created_at": item.created_at,
                "published_at": item.published_at,
            }
            for item in page.items
        ]
        next_cursor = (
            _encode_snapshot_cursor(page.data_generation, page.next_key)
            if page.next_key is not None
            else None
        )
        content = {
            "workspace_id": SHARED_WORKSPACE_ID,
            "data_generation": page.data_generation,
            "schema_generation": page.schema_generation,
            "filters": {"state_code": state_code, "data_year": data_year},
            "limit": limit,
            "count": len(snapshots),
            "total_count": page.total_count,
            "next_cursor": next_cursor,
            "available_data_years": list(page.available_data_years),
            "available_years_truncated": page.available_years_truncated,
            "snapshots": snapshots,
        }
        listing_etag = canonical_sha256(
            {
                "data_generation": page.data_generation,
                "filters": content["filters"],
                "cursor": cursor,
                "next_cursor": next_cursor,
                "snapshots": snapshots,
            }
        )
        return JSONResponse(
            status_code=200,
            content=content,
            headers=_version_headers(page.data_generation, etag=listing_etag),
        )
    except RevisionStoreError as exc:
        return _error_response(exc, database_path=database_path, generation=generation)


@router.get("/state")
def revision_state(workspace_id: str | None = None):
    """Return the concurrency token for the one server-owned research workspace."""

    database_path: str | None = None
    generation: int | None = None
    try:
        if workspace_id is not None:
            raise RevisionInputError(
                422,
                "workspace_not_selectable",
                "Workspace selection is not available on this endpoint",
            )
        database_path, generation = _prepare_store()
        state = get_workspace_state(database_path, SHARED_WORKSPACE_ID)
        return JSONResponse(
            status_code=200,
            content={
                "workspace_id": SHARED_WORKSPACE_ID,
                "data_generation": state.data_generation,
                "schema_generation": state.schema_generation,
            },
            headers=_version_headers(state.data_generation),
        )
    except RevisionStoreError as exc:
        return _error_response(exc, database_path=database_path, generation=generation)


@router.get("/snapshots/{state_code}/{bridge_number}/{data_year}/history")
def revision_history(
    state_code: int,
    bridge_number: str,
    data_year: int,
    workspace_id: str | None = None,
):
    database_path: str | None = None
    generation: int | None = None
    try:
        if workspace_id is not None:
            raise RevisionInputError(
                422,
                "workspace_not_selectable",
                "Workspace selection is not available on this endpoint",
            )
        database_path, generation = _prepare_store()
        records = get_snapshot_history(
            database_path,
            SHARED_WORKSPACE_ID,
            state_code,
            bridge_number,
            data_year,
        )
        revisions = [_public_history_revision(item) for item in reversed(records)]
        history_hash = canonical_sha256(
            [item["content_hash"] for item in revisions]
        )
        return JSONResponse(
            status_code=200,
            content={
                "state_code": state_code,
                "bridge_number": bridge_number,
                "data_year": data_year,
                "data_generation": generation,
                "revisions": revisions,
            },
            headers=_version_headers(generation, etag=history_hash),
        )
    except RevisionStoreError as exc:
        return _error_response(exc, database_path=database_path, generation=generation)


@router.get("/snapshots/{state_code}/{bridge_number}/{data_year}")
def published_revision(
    state_code: int,
    bridge_number: str,
    data_year: int,
    workspace_id: str | None = None,
):
    database_path: str | None = None
    generation: int | None = None
    try:
        if workspace_id is not None:
            raise RevisionInputError(
                422,
                "workspace_not_selectable",
                "Workspace selection is not available on this endpoint",
            )
        database_path, generation = _prepare_store()
        record = get_published_snapshot(
            database_path,
            SHARED_WORKSPACE_ID,
            state_code,
            bridge_number,
            data_year,
        )
        if record is None:
            raise RevisionInputError(
                404, "published_snapshot_not_found", "Published snapshot not found"
            )
        return JSONResponse(
            status_code=200,
            content=_public_revision(record, status="published"),
            headers=_version_headers(generation, etag=record.content_hash),
        )
    except RevisionStoreError as exc:
        return _error_response(exc, database_path=database_path, generation=generation)


__all__ = [
    "SHARED_WORKSPACE_ID",
    "configure_database_path",
    "router",
    "stage_server_documents",
]
