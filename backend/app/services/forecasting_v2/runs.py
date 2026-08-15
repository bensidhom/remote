"""Source-revision-bound annual forecast run creation and read models."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import sqlite3
import uuid

from app.services.analytics.projection import BridgeAnalysisHistory
from app.services.conversion.canonical_json import canonical_json_text
from app.services.forecasting_v2.estimator import BridgeAnnualForecastProvider
from app.services.forecasting_v2.models import ComponentState, ForecastQualification
from app.services.forecasting_v2.store import (
    ForecastStoreError,
    deserialize_annual_markov_model,
)


_NAMESPACE = uuid.UUID("ed6f2b22-1b89-42c6-bfed-b1f990198479")
_COMPONENT_FIELDS = {
    "deck": "bc01",
    "superstructure": "bc02",
    "substructure": "bc03",
    "culvert": "bc04",
}


@dataclass(frozen=True, slots=True)
class ForecastPoint:
    year: int
    component: str
    pre_action_distribution: tuple[float, ...]
    post_action_distribution: tuple[float, ...]
    expected_rating: float
    standard_deviation: float
    lower_rating: int
    median_rating: int
    upper_rating: int
    fallback_level: str
    action_id: str | None = None


@dataclass(frozen=True, slots=True)
class BridgeForecastRun:
    run_id: str
    workspace_id: str
    state_code: int
    bridge_number: str
    snapshot_revision_id: str
    as_of_year: int
    horizon_years: int
    forecast_mode: str
    model_version_id: str
    model_id: str
    evaluation_run_id: str
    evaluation_receipt_id: str
    qualification: ForecastQualification
    source_data_generation: int
    observed: tuple[tuple[str, int, float, str], ...]
    predictions: tuple[ForecastPoint, ...]


def _connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def _quantile(distribution: tuple[float, ...], probability: float) -> int:
    cumulative = 0.0
    for rating, mass in enumerate(distribution):
        cumulative += mass
        if cumulative + 1e-12 >= probability:
            return rating
    return 9


def _point(*, year: int, component: str, distribution: tuple[float, ...], fallback: str) -> ForecastPoint:
    expected = math.fsum(index * mass for index, mass in enumerate(distribution))
    variance = math.fsum(((index - expected) ** 2) * mass for index, mass in enumerate(distribution))
    return ForecastPoint(
        year=year,
        component=component,
        pre_action_distribution=distribution,
        post_action_distribution=distribution,
        expected_rating=expected,
        standard_deviation=math.sqrt(max(0.0, variance)),
        lower_rating=_quantile(distribution, 0.10),
        median_rating=_quantile(distribution, 0.50),
        upper_rating=_quantile(distribution, 0.90),
        fallback_level=fallback,
    )


def _latest_package(connection: sqlite3.Connection, workspace_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT m.id AS model_version_id,m.model_id,m.model_json,m.qualification,"
        "e.id AS evaluation_run_id,e.evaluation_receipt_id,e.release_qualified "
        "FROM forecast_model_versions m JOIN forecast_evaluation_runs e "
        "ON e.workspace_id=m.workspace_id AND e.forecast_model_version_id=m.id "
        "WHERE m.workspace_id=? ORDER BY e.created_at DESC,e.id DESC LIMIT 1",
        (workspace_id,),
    ).fetchone()
    if row is None:
        raise ForecastStoreError(
            "forecast_package_not_found",
            "No evaluated forecast package is available for this workspace",
            status=404,
        )
    return row


def _observed(history: BridgeAnalysisHistory) -> tuple[tuple[str, int, float, str], ...]:
    rows: list[tuple[str, int, float, str]] = []
    for snapshot in history.snapshots:
        for component, field in _COMPONENT_FIELDS.items():
            value = getattr(snapshot.condition, field)
            if value is not None and math.isfinite(value) and 0 <= value <= 9:
                rows.append((component, snapshot.data_year, float(value), snapshot.snapshot_revision_id))
    return tuple(rows)


def create_bridge_forecast_run(
    database_path: str | Path,
    *,
    history: BridgeAnalysisHistory,
    horizon_years: int,
    idempotency_key: str,
    created_by: str,
    created_at: str,
    expected_data_generation: int,
    require_released: bool = False,
) -> BridgeForecastRun:
    """Create one atomic no-action forecast from the exact latest published revision."""

    horizon = int(horizon_years)
    if horizon < 1 or horizon > 100:
        raise ForecastStoreError("forecast_horizon_invalid", "Forecast horizon must be 1 through 100 years", status=422)
    key = str(idempotency_key).strip()
    actor = str(created_by).strip()
    timestamp = str(created_at).strip()
    if not key or not actor or not timestamp:
        raise ForecastStoreError("forecast_request_invalid", "Idempotency key, actor, and timestamp are required", status=422)
    snapshot = history.snapshots[-1]
    if snapshot.identity.data_generation != int(expected_data_generation):
        raise ForecastStoreError("stale_data_generation", "Published data changed; refresh and retry")

    connection = _connect(database_path)
    try:
        package = _latest_package(connection, history.workspace_id)
        qualification = ForecastQualification(str(package["qualification"]))
        if require_released and qualification is not ForecastQualification.RELEASED:
            raise ForecastStoreError("forecast_not_released", "Forecast package is not released", status=409)
        model = deserialize_annual_markov_model(str(package["model_json"]))
        provider = BridgeAnnualForecastProvider(
            model=model,
            bridge_snapshot=snapshot,
            evaluation_receipt_id=str(package["evaluation_receipt_id"]),
            qualification=qualification,
        )
        initial: dict[str, ComponentState] = {}
        for component, field in _COMPONENT_FIELDS.items():
            value = getattr(snapshot.condition, field)
            if value is not None and math.isfinite(value) and 0 <= value <= 9:
                initial[component] = ComponentState.certain(component, int(round(value)))
        if not initial:
            raise ForecastStoreError("forecast_rating_unavailable", "No valid component rating is available", status=422)

        current = dict(initial)
        points: list[ForecastPoint] = []
        for year in range(snapshot.data_year + 1, snapshot.data_year + horizon + 1):
            for component in sorted(current):
                transition = provider.transition(
                    year=year,
                    component=component,
                    state=current[component],
                    action_id=None,
                )
                points.append(
                    _point(
                        year=year,
                        component=component,
                        distribution=transition.distribution,
                        fallback=transition.fallback_level,
                    )
                )
                current[component] = transition.state

        configuration = {
            "format": "bridgeguard-forecast-run-v2",
            "workspace_id": history.workspace_id,
            "state_code": history.state_code,
            "bridge_number": history.bridge_number,
            "snapshot_revision_id": snapshot.snapshot_revision_id,
            "as_of_year": snapshot.data_year,
            "horizon_years": horizon,
            "forecast_mode": "no_action",
            "model_version_id": str(package["model_version_id"]),
            "evaluation_run_id": str(package["evaluation_run_id"]),
            "source_data_generation": snapshot.identity.data_generation,
            "observed": [list(item) for item in _observed(history)],
        }
        configuration_json = canonical_json_text(configuration)
        run_id = str(uuid.uuid5(_NAMESPACE, f"{history.workspace_id}:{key}"))
        connection.execute("BEGIN IMMEDIATE")
        generation = connection.execute(
            "SELECT data_generation FROM workspace_state WHERE workspace_id=?",
            (history.workspace_id,),
        ).fetchone()
        if generation is None:
            raise ForecastStoreError("workspace_not_found", "Workspace not found", status=404)
        if int(generation[0]) != int(expected_data_generation):
            raise ForecastStoreError("stale_data_generation", "Published data changed; refresh and retry")
        existing = connection.execute(
            "SELECT id,configuration_json FROM forecast_runs WHERE workspace_id=? AND idempotency_key=?",
            (history.workspace_id, key),
        ).fetchone()
        if existing is not None:
            if str(existing["configuration_json"]) != configuration_json:
                raise ForecastStoreError("idempotency_payload_conflict", "Idempotency key was reused with a different forecast request")
            connection.rollback()
            return load_bridge_forecast_run(
                database_path,
                workspace_id=history.workspace_id,
                run_id=str(existing["id"]),
            )
        connection.execute(
            "INSERT INTO forecast_runs "
            "(id,workspace_id,forecast_model_version_id,forecast_evaluation_run_id,"
            "snapshot_revision_id,state_code,bridge_number,start_year,horizon_years,"
            "forecast_mode,configuration_json,idempotency_key,status,created_by,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                history.workspace_id,
                str(package["model_version_id"]),
                str(package["evaluation_run_id"]),
                snapshot.snapshot_revision_id,
                history.state_code,
                history.bridge_number,
                snapshot.data_year,
                horizon,
                "no_action",
                configuration_json,
                key,
                "succeeded",
                actor,
                timestamp,
            ),
        )
        for ordinal, point in enumerate(points):
            distribution_json = canonical_json_text(list(point.post_action_distribution))
            connection.execute(
                "INSERT INTO forecast_predictions "
                "(id,workspace_id,forecast_run_id,prediction_ordinal,forecast_year,component,"
                "action_id,pre_action_distribution_json,post_action_distribution_json,"
                "expected_rating,dispersion_json,quantiles_json,fallback_level,"
                "fallback_evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid5(_NAMESPACE, f"{run_id}:{ordinal}")),
                    history.workspace_id,
                    run_id,
                    ordinal,
                    point.year,
                    point.component,
                    None,
                    distribution_json,
                    distribution_json,
                    point.expected_rating,
                    canonical_json_text({"standard_deviation": point.standard_deviation}),
                    canonical_json_text(
                        {
                            "p10": point.lower_rating,
                            "p50": point.median_rating,
                            "p90": point.upper_rating,
                        }
                    ),
                    point.fallback_level,
                    canonical_json_text({"selected_level": point.fallback_level}),
                    timestamp,
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return BridgeForecastRun(
        run_id=run_id,
        workspace_id=history.workspace_id,
        state_code=history.state_code,
        bridge_number=history.bridge_number,
        snapshot_revision_id=snapshot.snapshot_revision_id,
        as_of_year=snapshot.data_year,
        horizon_years=horizon,
        forecast_mode="no_action",
        model_version_id=str(package["model_version_id"]),
        model_id=model.model_id,
        evaluation_run_id=str(package["evaluation_run_id"]),
        evaluation_receipt_id=str(package["evaluation_receipt_id"]),
        qualification=qualification,
        source_data_generation=snapshot.identity.data_generation,
        observed=_observed(history),
        predictions=tuple(points),
    )


def load_bridge_forecast_run(
    database_path: str | Path,
    *,
    workspace_id: str,
    run_id: str,
) -> BridgeForecastRun:
    connection = _connect(database_path)
    try:
        run = connection.execute(
            "SELECT r.*,m.model_id,m.qualification,e.evaluation_receipt_id "
            "FROM forecast_runs r JOIN forecast_model_versions m "
            "ON m.workspace_id=r.workspace_id AND m.id=r.forecast_model_version_id "
            "JOIN forecast_evaluation_runs e ON e.workspace_id=r.workspace_id "
            "AND e.id=r.forecast_evaluation_run_id WHERE r.workspace_id=? AND r.id=?",
            (str(workspace_id).strip(), str(run_id).strip()),
        ).fetchone()
        if run is None:
            raise ForecastStoreError("forecast_run_not_found", "Forecast run not found", status=404)
        prediction_rows = connection.execute(
            "SELECT * FROM forecast_predictions WHERE workspace_id=? AND forecast_run_id=? "
            "ORDER BY prediction_ordinal",
            (str(workspace_id).strip(), str(run_id).strip()),
        ).fetchall()
    finally:
        connection.close()
    points = tuple(
        ForecastPoint(
            year=int(row["forecast_year"]),
            component=str(row["component"]),
            pre_action_distribution=tuple(json.loads(row["pre_action_distribution_json"])),
            post_action_distribution=tuple(json.loads(row["post_action_distribution_json"])),
            expected_rating=float(row["expected_rating"]),
            standard_deviation=float(json.loads(row["dispersion_json"])["standard_deviation"]),
            lower_rating=int(json.loads(row["quantiles_json"])["p10"]),
            median_rating=int(json.loads(row["quantiles_json"])["p50"]),
            upper_rating=int(json.loads(row["quantiles_json"])["p90"]),
            fallback_level=str(row["fallback_level"]),
            action_id=row["action_id"],
        )
        for row in prediction_rows
    )
    configuration = json.loads(str(run["configuration_json"]))
    observed = tuple(
        (str(item[0]), int(item[1]), float(item[2]), str(item[3]))
        for item in configuration.get("observed", ())
    )
    return BridgeForecastRun(
        run_id=str(run["id"]),
        workspace_id=str(run["workspace_id"]),
        state_code=int(run["state_code"]),
        bridge_number=str(run["bridge_number"]),
        snapshot_revision_id=str(run["snapshot_revision_id"]),
        as_of_year=int(run["start_year"]),
        horizon_years=int(run["horizon_years"]),
        forecast_mode=str(run["forecast_mode"]),
        model_version_id=str(run["forecast_model_version_id"]),
        model_id=str(run["model_id"]),
        evaluation_run_id=str(run["forecast_evaluation_run_id"]),
        evaluation_receipt_id=str(run["evaluation_receipt_id"]),
        qualification=ForecastQualification(str(run["qualification"])),
        source_data_generation=int(configuration["source_data_generation"]),
        observed=observed,
        predictions=points,
    )
