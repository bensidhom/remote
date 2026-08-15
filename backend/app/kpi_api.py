"""Publication-scoped operational KPI HTTP API."""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, File, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import revision_api
from .services.kpis_v2.repository import (
    KpiComputationError,
    compute_age_condition_area,
    compute_operational_kpis,
)
from .services.kpis_v2.registry import ACCEPTABLE_DEFINITION_VERSION
from .services.kpis_v2.program import (
    EXPECTED_HEADERS,
    MAX_BYTES as MAX_PROGRAM_BYTES,
    ProgramKpiError,
    approve_program_source,
    list_program_kpis,
    persist_program_kpis,
    preview_program_kpis,
)
from .services.kpis_v2.fhwa_tpm import (
    FhwaTpmDiagnosticError,
    compute_fhwa_tpm_diagnostic,
)
from .services.conversion.source_artifacts import SourceArtifactError, SourceArtifactStore
from .services.kpis_v2.export import build_kpi_export


router = APIRouter(tags=["analytics-kpis-v2"])
MAX_PROGRAM_KPI_BYTES = MAX_PROGRAM_BYTES


class KpiExportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    export_kind: str = Field(min_length=1, max_length=64)
    state_code: int = Field(default=34, ge=1, le=99)
    year: int = Field(ge=1900, le=2100)
    population: str = Field(default="statewide", max_length=64)
    include_unknown: bool = True


class ProgramApprovalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_note: str = Field(min_length=1, max_length=500)


def _actor(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return str(getattr(user, "email", "") or "local-research-user").strip().lower()


def _response(payload: dict[str, object]) -> JSONResponse:
    canonical = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return JSONResponse(
        content=payload,
        headers={
            "Cache-Control": "no-store",
            "ETag": f'"{digest}"',
            "X-BridgeGuard-Data-Version": str(payload["data_generation"]),
            "X-BridgeGuard-KPI-Definition": str(payload["definition_version"]),
        },
    )


def _error(exc: KpiComputationError) -> JSONResponse:
    message = str(exc)
    status = 404 if "not found" in message else 422
    return JSONResponse(
        status_code=status,
        content={"detail": {"code": "kpi_population_unavailable", "message": message}},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/analytics/kpis/operational")
async def operational_kpis(
    state_code: int = Query(default=34, ge=1, le=99),
    year: int | None = Query(default=None, ge=1900, le=2100),
    older_than_years: int = Query(default=45, ge=0, le=300),
    high_traffic_threshold: int = Query(default=25_000, ge=0, le=10_000_000),
):
    try:
        payload = await run_in_threadpool(
            compute_operational_kpis,
            database_path=revision_api._database_path(),
            workspace_id=revision_api.SHARED_WORKSPACE_ID,
            state_code=state_code,
            year=year,
            older_than_years=older_than_years,
            high_traffic_threshold=high_traffic_threshold,
        )
        return _response(payload)
    except KpiComputationError as exc:
        return _error(exc)


@router.get("/api/analytics/kpis/years")
async def published_kpi_years(
    state_code: int = Query(default=34, ge=1, le=99),
):
    import sqlite3
    from pathlib import Path

    database = Path(revision_api._database_path()).resolve()
    if not database.is_file():
        return JSONResponse(
            status_code=404,
            content={"detail": {"code": "kpi_population_unavailable", "message": "Analytical database is unavailable."}},
            headers={"Cache-Control": "no-store"},
        )
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=2.0)
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute(
            "SELECT ws.data_generation,ws.schema_generation FROM workspace_state ws WHERE ws.workspace_id=?",
            (revision_api.SHARED_WORKSPACE_ID,),
        ).fetchone()
        years = [
            int(item[0])
            for item in connection.execute(
                "SELECT DISTINCT data_year FROM published_snapshots "
                "WHERE workspace_id=? AND state_code=? ORDER BY data_year DESC",
                (revision_api.SHARED_WORKSPACE_ID, int(state_code)),
            ).fetchall()
        ]
    except sqlite3.Error:
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": "kpi_population_unavailable", "message": "Published KPI years could not be read."}},
            headers={"Cache-Control": "no-store"},
        )
    finally:
        try:
            connection.close()
        except UnboundLocalError:
            pass
    if row is None:
        return JSONResponse(
            status_code=404,
            content={"detail": {"code": "kpi_population_unavailable", "message": "Workspace state was not found."}},
            headers={"Cache-Control": "no-store"},
        )
    return _response(
        {
            "years": years,
            "state_code": int(state_code),
            "data_generation": int(row[0]),
            "schema_generation": int(row[1]),
            "definition_version": "bridgeguard-operational-v1",
        }
    )


@router.get("/api/analytics/kpis/acceptable-condition")
async def acceptable_condition_kpis(
    state_code: int = Query(default=34, ge=1, le=99),
    year: int | None = Query(default=None, ge=1900, le=2100),
    definition_version: str = Query(default=ACCEPTABLE_DEFINITION_VERSION),
):
    if definition_version != ACCEPTABLE_DEFINITION_VERSION:
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "kpi_definition_unavailable",
                    "message": "The requested acceptable-condition definition is not registered.",
                }
            },
            headers={"Cache-Control": "no-store"},
        )
    try:
        payload = await run_in_threadpool(
            compute_operational_kpis,
            database_path=revision_api._database_path(),
            workspace_id=revision_api.SHARED_WORKSPACE_ID,
            state_code=state_code,
            year=year,
        )
        values = payload["values"]
        assert isinstance(values, dict)
        focused = {
            **{key: value for key, value in payload.items() if key != "values"},
            "definition_version": ACCEPTABLE_DEFINITION_VERSION,
            "values": {
                "state_maintained_policy": values["state_maintained_policy"],
                "bridge_count": values["state_maintained_20ft_acceptable"],
                "deck_area": values["state_maintained_deck_area_acceptable"],
            },
        }
        return _response(focused)
    except KpiComputationError as exc:
        return _error(exc)


@router.get("/api/analytics/kpis/age-condition-area")
async def age_condition_area_kpis(
    state_code: int = Query(default=34, ge=1, le=99),
    year: int | None = Query(default=None, ge=1900, le=2100),
    population: str = Query(default="statewide"),
    include_unknown: bool = Query(default=True),
):
    try:
        payload = await run_in_threadpool(
            compute_age_condition_area,
            database_path=revision_api._database_path(),
            workspace_id=revision_api.SHARED_WORKSPACE_ID,
            state_code=state_code,
            year=year,
            population=population,
            include_unknown=include_unknown,
        )
        return _response(payload)
    except KpiComputationError as exc:
        return _error(exc)


@router.get("/api/analytics/kpis/program/template")
async def program_kpi_template():
    body = (",".join(EXPECTED_HEADERS) + "\r\n").encode("utf-8")
    return Response(
        content=body,
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="bridgeguard-program-kpis-v1.csv"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/api/analytics/kpis/program")
async def approved_program_kpis():
    try:
        payload = await run_in_threadpool(
            list_program_kpis,
            revision_api._database_path(),
            workspace_id=revision_api.SHARED_WORKSPACE_ID,
        )
        return _response(payload)
    except ProgramKpiError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": exc.code, "message": str(exc)}},
            headers={"Cache-Control": "no-store"},
        )


@router.post("/api/analytics/kpis/program/import", status_code=201)
async def import_program_kpis(request: Request, file: UploadFile = File(...)):
    try:
        data = await file.read(MAX_PROGRAM_BYTES + 1)
        if len(data) > MAX_PROGRAM_BYTES:
            raise ProgramKpiError("source_too_large", "The program KPI source exceeds the byte limit.")
        preview = preview_program_kpis(
            data,
            filename=file.filename or "program-kpis.csv",
            media_type=file.content_type,
        )
        if not preview.ready:
            return JSONResponse(
                status_code=422,
                content={"detail": {"code": "program_kpi_preflight_failed", "message": "Program KPI source contains blocking issues", "preview": preview.as_dict()}},
                headers={"Cache-Control": "no-store"},
            )
        store = SourceArtifactStore(
            revision_api._artifact_root() / "kpi_program_sources",
            max_bytes=MAX_PROGRAM_BYTES,
        )
        artifact = await run_in_threadpool(
            store.ingest,
            data,
            original_filename=preview.filename,
        )
        result = await run_in_threadpool(
            persist_program_kpis,
            revision_api._database_path(),
            workspace_id=revision_api.SHARED_WORKSPACE_ID,
            preview=preview,
            artifact=artifact,
            actor_id=_actor(request),
        )
        return JSONResponse(
            status_code=201 if result["outcome"] == "created" else 200,
            content={"source": artifact.to_public_dict(), "preview": preview.as_dict(), **result},
            headers={"Cache-Control": "no-store"},
        )
    except ProgramKpiError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": exc.code, "message": str(exc)}},
            headers={"Cache-Control": "no-store"},
        )
    except SourceArtifactError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": exc.to_public_dict()},
            headers={"Cache-Control": "no-store"},
        )
    finally:
        await file.close()


@router.post("/api/analytics/kpis/program/sources/{source_id}/approve")
async def approve_program_kpis(
    request: Request,
    source_id: str,
    body: ProgramApprovalBody,
):
    try:
        result = await run_in_threadpool(
            approve_program_source,
            revision_api._database_path(),
            workspace_id=revision_api.SHARED_WORKSPACE_ID,
            source_id=source_id,
            reviewer_id=_actor(request),
            review_note=body.review_note,
        )
        return JSONResponse(content=result, headers={"Cache-Control": "no-store"})
    except ProgramKpiError as exc:
        status = 404 if exc.code == "program_source_not_found" else 409
        return JSONResponse(
            status_code=status,
            content={"detail": {"code": exc.code, "message": str(exc)}},
            headers={"Cache-Control": "no-store"},
        )


@router.get("/api/analytics/kpis/fhwa-tpm")
async def fhwa_tpm_diagnostic(
    state_code: int = Query(default=34, ge=1, le=99),
    year: int = Query(ge=1900, le=2100),
):
    try:
        payload = await run_in_threadpool(
            compute_fhwa_tpm_diagnostic,
            database_path=revision_api._database_path(),
            workspace_id=revision_api.SHARED_WORKSPACE_ID,
            state_code=state_code,
            year=year,
        )
        return _response(payload)
    except FhwaTpmDiagnosticError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": "fhwa_tpm_diagnostic_unavailable", "message": str(exc)}},
            headers={"Cache-Control": "no-store"},
        )


@router.post("/api/analytics/kpis/exports")
async def export_kpis(body: KpiExportBody):
    try:
        kind = body.export_kind.strip().lower()
        if kind in {"operational", "acceptable_condition"}:
            report = await run_in_threadpool(
                compute_operational_kpis,
                database_path=revision_api._database_path(),
                workspace_id=revision_api.SHARED_WORKSPACE_ID,
                state_code=body.state_code,
                year=body.year,
            )
        elif kind == "age_condition_area":
            report = await run_in_threadpool(
                compute_age_condition_area,
                database_path=revision_api._database_path(),
                workspace_id=revision_api.SHARED_WORKSPACE_ID,
                state_code=body.state_code,
                year=body.year,
                population=body.population,
                include_unknown=body.include_unknown,
            )
        elif kind == "program":
            report = await run_in_threadpool(
                list_program_kpis,
                revision_api._database_path(),
                workspace_id=revision_api.SHARED_WORKSPACE_ID,
            )
        elif kind == "fhwa_tpm":
            report = await run_in_threadpool(
                compute_fhwa_tpm_diagnostic,
                database_path=revision_api._database_path(),
                workspace_id=revision_api.SHARED_WORKSPACE_ID,
                state_code=body.state_code,
                year=body.year,
            )
        else:
            raise ValueError("unsupported KPI export kind")
        export_id, content, _manifest = await run_in_threadpool(
            build_kpi_export, report, export_kind=kind
        )
        return Response(
            content=content,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="bridgeguard-{kind}-{body.year}-{export_id[:12]}.zip"',
                "X-BridgeGuard-Export-ID": export_id,
                "X-BridgeGuard-Data-Version": str(report["data_generation"]),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except (KpiComputationError, ProgramKpiError, FhwaTpmDiagnosticError, ValueError) as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": "kpi_export_unavailable", "message": str(exc)}},
            headers={"Cache-Control": "no-store"},
        )


__all__ = ["MAX_PROGRAM_KPI_BYTES", "router"]
