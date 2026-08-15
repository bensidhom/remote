"""Durable forecast training and exact-revision annual forecast HTTP API."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import revision_api
from .services.analytics.projection import (
    ANALYTICAL_SPECIFICATION_SHA256,
    AnalyticalProjectionError,
    load_bridge_analysis_history,
    load_state_analysis_histories,
)
from .services.conversion.revision_store import get_workspace_state
from .services.forecasting_v2.estimator import ForecastModelConfig, build_annual_markov_model
from .services.forecasting_v2.evaluation import (
    ForecastReleasePolicy,
    evaluate_historical_hindcasts,
)
from .services.forecasting_v2.runs import (
    BridgeForecastRun,
    create_bridge_forecast_run,
    load_bridge_forecast_run,
)
from .services.forecasting_v2.store import ForecastStoreError, persist_forecast_package
from .services.operations.jobs import (
    JobStoreError,
    OperationJob,
    enqueue_job,
    get_job,
    request_job_cancellation,
)


router = APIRouter(tags=["forecast-v2"])
FORECAST_TRAIN_OPERATION = "forecast_train_evaluate"


class ForecastApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class ForecastTrainingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state_code: int = Field(default=34, ge=1, le=99)
    through_year: int = Field(ge=1990, le=2100)
    origins: list[int] = Field(min_length=1, max_length=20)
    expected_data_generation: int = Field(ge=0)
    minimum_cohort_exposure: int = Field(default=25, ge=1, le=10_000)
    release_minimum_target_rows: int = Field(default=100, ge=1, le=10_000_000)
    release_minimum_mae_skill: float = Field(default=0.0, ge=-1.0, le=1.0)
    release_maximum_candidate_mae: float = Field(default=1.5, ge=0.0, le=9.0)


class BridgeForecastBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state_code: int = Field(default=34, ge=1, le=99)
    bridge_number: str = Field(min_length=1, max_length=64)
    as_of_year: int | None = Field(default=None, ge=1900, le=2100)
    horizon_years: int = Field(default=10, ge=1, le=100)
    expected_data_generation: int = Field(ge=0)
    mode: Literal["no_action"] = "no_action"


def _database_path() -> str:
    return revision_api._database_path()


def _job_database_path() -> Path:
    return Path(_database_path()).parent / "operation_jobs.db"


def _actor(request: Request) -> str:
    user = getattr(request.state, "user", None)
    email = str(getattr(user, "email", "") or "").strip().lower()
    return email or "local-research-user"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _error(exc: ForecastApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"detail": {"code": exc.code, "message": exc.message}},
        headers={"Cache-Control": "no-store"},
    )


def _key(value: str | None) -> str:
    key = str(value or "").strip()
    if not key or len(key) > 255 or "\r" in key or "\n" in key:
        raise ForecastApiError(400, "idempotency_key_required", "Idempotency-Key is required")
    return key


def _public_job(job: OperationJob) -> dict[str, object]:
    return {
        "job_id": job.id,
        "operation": job.operation,
        "status": job.status,
        "progress": job.progress,
        "result": job.result,
        "error_code": job.error_code,
        "attempts": job.attempts,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "cancellation_requested": job.cancellation_requested_at is not None,
    }


def _run_payload(run: BridgeForecastRun) -> dict[str, object]:
    metrics: dict[str, dict[str, list[dict[str, object]]]] = {}
    for component, year, value, revision_id in run.observed:
        metrics.setdefault(component, {"observed": [], "forecast": []})["observed"].append(
            {"year": year, "value": value, "snapshot_revision_id": revision_id}
        )
    for point in run.predictions:
        metrics.setdefault(point.component, {"observed": [], "forecast": []})["forecast"].append(
            {
                "year": point.year,
                "yhat": point.expected_rating,
                "p10": point.lower_rating,
                "p50": point.median_rating,
                "p90": point.upper_rating,
                "standard_deviation": point.standard_deviation,
                "probabilities": list(point.post_action_distribution),
                "fallback_level": point.fallback_level,
                "action_id": point.action_id,
            }
        )
    return {
        "run_id": run.run_id,
        "workspace_id": run.workspace_id,
        "state_code": run.state_code,
        "bridge_number": run.bridge_number,
        "snapshot_revision_id": run.snapshot_revision_id,
        "as_of_year": run.as_of_year,
        "horizon_years": run.horizon_years,
        "forecast_mode": run.forecast_mode,
        "model_version_id": run.model_version_id,
        "model_id": run.model_id,
        "evaluation_run_id": run.evaluation_run_id,
        "evaluation_receipt_id": run.evaluation_receipt_id,
        "qualification": run.qualification.value,
        "source_data_generation": run.source_data_generation,
        "metrics": metrics,
    }


def _assert_job_not_cancelled(job: OperationJob, job_database_path: str | Path) -> None:
    current = get_job(
        job_database_path,
        workspace_id=job.workspace_id,
        owner_id=job.owner_id,
        job_id=job.id,
    )
    if current.cancellation_requested_at is not None:
        raise ForecastApiError(409, "forecast_job_cancelled", "Forecast job was cancelled")


def process_forecast_training_job(
    *,
    job: OperationJob,
    database_path: str | Path,
    job_database_path: str | Path,
) -> dict[str, object]:
    """Worker entry point; publish a package only after a second generation check."""

    if job.operation != FORECAST_TRAIN_OPERATION:
        raise ForecastApiError(500, "forecast_job_invalid", "Unsupported forecast job")
    payload = job.payload
    expected_generation = int(payload["expected_data_generation"])
    state = get_workspace_state(str(database_path), job.workspace_id)
    if state.data_generation != expected_generation:
        raise ForecastApiError(409, "stale_data_generation", "Published data changed; create a new job")
    _assert_job_not_cancelled(job, job_database_path)
    histories = load_state_analysis_histories(
        database_path=database_path,
        workspace_id=job.workspace_id,
        state_code=int(payload["state_code"]),
        through_year=int(payload["through_year"]),
    )
    _assert_job_not_cancelled(job, job_database_path)
    config = ForecastModelConfig(
        minimum_cohort_exposure=int(payload["minimum_cohort_exposure"])
    )
    model = build_annual_markov_model(
        histories,
        through_year=int(payload["through_year"]),
        config=config,
    )
    evaluation = evaluate_historical_hindcasts(
        histories,
        origins=tuple(int(value) for value in payload["origins"]),
        config=config,
        release_policy=ForecastReleasePolicy(
            minimum_target_rows=int(payload["release_minimum_target_rows"]),
            minimum_mae_skill=float(payload["release_minimum_mae_skill"]),
            maximum_candidate_mae=float(payload["release_maximum_candidate_mae"]),
        ),
    )
    _assert_job_not_cancelled(job, job_database_path)
    if get_workspace_state(str(database_path), job.workspace_id).data_generation != expected_generation:
        raise ForecastApiError(409, "stale_data_generation", "Published data changed before forecast publication")
    package = persist_forecast_package(
        database_path,
        model=model,
        evaluation=evaluation,
        projection_specification_sha256=ANALYTICAL_SPECIFICATION_SHA256,
        created_by=job.owner_id,
        created_at=_now(),
    )
    return {
        "dataset_id": package.dataset_id,
        "model_version_id": package.model_version_id,
        "model_id": package.model_id,
        "evaluation_run_id": package.evaluation_run_id,
        "evaluation_receipt_id": package.evaluation_receipt_id,
        "qualification": package.qualification.value,
        "release_qualified": package.release_qualified,
        "training_histories": model.statistics.bridge_histories,
        "used_intervals": model.statistics.used_intervals,
        "hindcast_rows": evaluation.summary.target_rows,
        "candidate_mae": evaluation.summary.candidate_mae,
        "persistence_mae": evaluation.summary.persistence_mae,
        "mae_skill": evaluation.summary.mae_skill,
        "source_data_generation": expected_generation,
    }


@router.post("/api/forecast-jobs", status_code=202)
async def create_forecast_job(
    body: ForecastTrainingBody,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    try:
        origins = sorted(set(int(value) for value in body.origins))
        if any(origin >= body.through_year for origin in origins):
            raise ForecastApiError(422, "forecast_origins_invalid", "Every evaluation origin must precede through_year")
        job = await run_in_threadpool(
            enqueue_job,
            _job_database_path(),
            workspace_id=revision_api.SHARED_WORKSPACE_ID,
            owner_id=_actor(request),
            operation=FORECAST_TRAIN_OPERATION,
            idempotency_key=_key(idempotency_key),
            payload={**body.model_dump(), "origins": origins},
            max_attempts=2,
        )
        return JSONResponse(status_code=202, content=_public_job(job), headers={"Cache-Control": "no-store"})
    except ForecastApiError as exc:
        return _error(exc)
    except JobStoreError as exc:
        return _error(ForecastApiError(exc.status, exc.code, "Forecast job could not be queued"))


@router.get("/api/forecast-jobs/{job_id}")
def get_forecast_job(job_id: str, request: Request):
    try:
        job = get_job(
            _job_database_path(),
            workspace_id=revision_api.SHARED_WORKSPACE_ID,
            owner_id=_actor(request),
            job_id=job_id,
        )
        return JSONResponse(content=_public_job(job), headers={"Cache-Control": "no-store"})
    except JobStoreError as exc:
        return _error(ForecastApiError(exc.status, exc.code, "Forecast job not found"))


@router.post("/api/forecast-jobs/{job_id}/cancel", status_code=202)
def cancel_forecast_job(job_id: str, request: Request):
    try:
        job = request_job_cancellation(
            _job_database_path(),
            workspace_id=revision_api.SHARED_WORKSPACE_ID,
            owner_id=_actor(request),
            job_id=job_id,
        )
        return JSONResponse(status_code=202, content=_public_job(job), headers={"Cache-Control": "no-store"})
    except JobStoreError as exc:
        return _error(ForecastApiError(exc.status, exc.code, "Forecast job not found"))


@router.post("/api/forecasts/v2/bridge")
async def create_bridge_forecast(
    body: BridgeForecastBody,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    try:
        history = await run_in_threadpool(
            load_bridge_analysis_history,
            database_path=_database_path(),
            workspace_id=revision_api.SHARED_WORKSPACE_ID,
            state_code=body.state_code,
            bridge_number=body.bridge_number,
            through_year=body.as_of_year,
        )
        run = await run_in_threadpool(
            create_bridge_forecast_run,
            _database_path(),
            history=history,
            horizon_years=body.horizon_years,
            idempotency_key=_key(idempotency_key),
            created_by=_actor(request),
            created_at=_now(),
            expected_data_generation=body.expected_data_generation,
            require_released=False,
        )
        return JSONResponse(content=_run_payload(run), headers={"Cache-Control": "no-store"})
    except (ForecastStoreError, AnalyticalProjectionError) as exc:
        if isinstance(exc, ForecastStoreError):
            return _error(ForecastApiError(exc.status, exc.code, str(exc)))
        return _error(ForecastApiError(404, "forecast_history_not_found", str(exc)))
    except ForecastApiError as exc:
        return _error(exc)


@router.get("/api/forecasts/v2/runs/{run_id}")
def get_forecast_run(run_id: str):
    try:
        run = load_bridge_forecast_run(
            _database_path(), workspace_id=revision_api.SHARED_WORKSPACE_ID, run_id=run_id
        )
        return JSONResponse(content=_run_payload(run), headers={"Cache-Control": "no-store"})
    except ForecastStoreError as exc:
        return _error(ForecastApiError(exc.status, exc.code, str(exc)))


__all__ = [
    "FORECAST_TRAIN_OPERATION",
    "process_forecast_training_job",
    "router",
]


@router.get("/api/forecasts/v2/summary")
def forecast_v2_summary():
    connection = sqlite3.connect(_database_path())
    connection.row_factory = sqlite3.Row
    try:
        state = connection.execute(
            "SELECT data_generation,schema_generation FROM workspace_state WHERE workspace_id=?",
            (revision_api.SHARED_WORKSPACE_ID,),
        ).fetchone()
        years = connection.execute(
            "SELECT DISTINCT data_year FROM published_snapshots WHERE workspace_id=? "
            "ORDER BY data_year",
            (revision_api.SHARED_WORKSPACE_ID,),
        ).fetchall()
        counts = connection.execute(
            "SELECT COUNT(*) AS observations,COUNT(DISTINCT bridge_number) AS bridges "
            "FROM published_snapshots WHERE workspace_id=?",
            (revision_api.SHARED_WORKSPACE_ID,),
        ).fetchone()
        latest = connection.execute(
            "SELECT m.id AS model_version_id,m.model_id,m.engine_version,m.qualification,"
            "m.created_at,e.id AS evaluation_run_id,e.evaluation_receipt_id,"
            "e.release_qualified,e.summary_json FROM forecast_model_versions m "
            "JOIN forecast_evaluation_runs e ON e.workspace_id=m.workspace_id "
            "AND e.forecast_model_version_id=m.id WHERE m.workspace_id=? "
            "ORDER BY e.created_at DESC,e.id DESC LIMIT 1",
            (revision_api.SHARED_WORKSPACE_ID,),
        ).fetchone()
    finally:
        connection.close()
    return JSONResponse(
        content={
            "data_generation": int(state["data_generation"]) if state else 0,
            "schema_generation": int(state["schema_generation"]) if state else 0,
            "published_observations": int(counts["observations"]) if counts else 0,
            "published_bridges": int(counts["bridges"]) if counts else 0,
            "available_years": [int(row["data_year"]) for row in years],
            "latest_package": (
                {
                    "model_version_id": str(latest["model_version_id"]),
                    "model_id": str(latest["model_id"]),
                    "engine_version": str(latest["engine_version"]),
                    "qualification": str(latest["qualification"]),
                    "created_at": str(latest["created_at"]),
                    "evaluation_run_id": str(latest["evaluation_run_id"]),
                    "evaluation_receipt_id": str(latest["evaluation_receipt_id"]),
                    "release_qualified": bool(latest["release_qualified"]),
                    "evaluation": json.loads(str(latest["summary_json"])),
                }
                if latest
                else None
            ),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/forecasts/v2/bridges")
def forecast_v2_bridges(limit: int = 1000):
    maximum = max(1, min(int(limit), 5000))
    connection = sqlite3.connect(_database_path())
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "WITH observations AS ("
            "SELECT state_code,bridge_number,COUNT(DISTINCT data_year) AS observation_years,"
            "MAX(data_year) AS latest_year FROM published_snapshots "
            "WHERE workspace_id=? GROUP BY state_code,bridge_number) "
            "SELECT o.state_code,o.bridge_number,o.observation_years,o.latest_year,b.bridge_name "
            "FROM observations o LEFT JOIN bridges b ON b.state_code=o.state_code "
            "AND b.bridge_number=o.bridge_number AND b.data_year=o.latest_year "
            "ORDER BY o.bridge_number LIMIT ?",
            (revision_api.SHARED_WORKSPACE_ID, maximum),
        ).fetchall()
    finally:
        connection.close()
    return JSONResponse(
        content={"bridges": [dict(row) for row in rows]},
        headers={"Cache-Control": "no-store"},
    )
