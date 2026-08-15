"""Immutable persistence for versioned forecast models and hindcast receipts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3
from types import MappingProxyType
import uuid

from app.routers.services.db_migrations import migrate_database
from app.services.conversion.canonical_json import canonical_json_text
from app.services.forecasting_v2.estimator import AnnualMarkovModel, ForecastModelConfig
from app.services.forecasting_v2.evaluation import HindcastReport
from app.services.forecasting_v2.models import ForecastQualification


_NAMESPACE = uuid.UUID("caa20cd2-0cf2-4de0-adae-7889e3228285")
MODEL_SERIALIZATION_VERSION = "annual-markov-model-v1"


class ForecastStoreError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class ForecastPackageRecord:
    workspace_id: str
    dataset_id: str
    model_version_id: str
    model_id: str
    evaluation_run_id: str
    evaluation_receipt_id: str
    qualification: ForecastQualification
    release_qualified: bool


def _identifier(kind: str, workspace_id: str, digest: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{kind}:{workspace_id}:{digest}"))


def _connect(database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database_path), timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def serialize_annual_markov_model(model: AnnualMarkovModel) -> str:
    payload = {
        "serialization_version": MODEL_SERIALIZATION_VERSION,
        "model_id": model.model_id,
        "source_receipt_sha256": model.source_receipt_sha256,
        "workspace_id": model.workspace_id,
        "state_code": model.state_code,
        "through_year": model.through_year,
        "config": model.config.canonical_payload(),
        "statistics": asdict(model.statistics),
        "transition_counts": [
            {"key": list(key), "matrix": [list(row) for row in matrix]}
            for key, matrix in sorted(model.transition_counts.items(), key=lambda item: repr(item[0]))
        ],
    }
    return canonical_json_text(payload)


def deserialize_annual_markov_model(serialized: str) -> AnnualMarkovModel:
    try:
        payload = json.loads(serialized)
        if payload.get("serialization_version") != MODEL_SERIALIZATION_VERSION:
            raise ValueError("unsupported model serialization version")
        config = ForecastModelConfig(**payload["config"])
        from app.services.forecasting_v2.estimator import ForecastTrainingStatistics

        statistics = ForecastTrainingStatistics(**payload["statistics"])
        counts: dict[tuple[object, ...], tuple[tuple[int, ...], ...]] = {}
        for entry in payload["transition_counts"]:
            key = tuple(entry["key"])
            matrix = tuple(tuple(int(value) for value in row) for row in entry["matrix"])
            if len(matrix) != 10 or any(len(row) != 10 for row in matrix):
                raise ValueError("transition matrix must be 10 by 10")
            if any(value < 0 for row in matrix for value in row):
                raise ValueError("transition counts must be non-negative")
            if key in counts:
                raise ValueError("duplicate transition cohort key")
            counts[key] = matrix
        model = AnnualMarkovModel(
            model_id=str(payload["model_id"]),
            source_receipt_sha256=str(payload["source_receipt_sha256"]),
            workspace_id=str(payload["workspace_id"]),
            state_code=int(payload["state_code"]),
            through_year=int(payload["through_year"]),
            config=config,
            statistics=statistics,
            transition_counts=MappingProxyType(counts),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ForecastStoreError(
            "forecast_model_invalid",
            "Stored forecast model is invalid",
            status=500,
        ) from exc
    if serialize_annual_markov_model(model) != serialized:
        raise ForecastStoreError(
            "forecast_model_noncanonical",
            "Stored forecast model is not canonical",
            status=500,
        )
    return model


def _evaluation_summary(report: HindcastReport) -> str:
    return canonical_json_text(
        {
            "origins": list(report.origins),
            "target_rows": report.summary.target_rows,
            "bridge_count": report.summary.bridge_count,
            "candidate_mae": report.summary.candidate_mae,
            "persistence_mae": report.summary.persistence_mae,
            "mae_skill": report.summary.mae_skill,
            "mean_bias": report.summary.mean_bias,
        }
    )


def _evaluation_rows(report: HindcastReport) -> str:
    return canonical_json_text([row.canonical_payload() for row in report.rows])


def persist_forecast_package(
    database_path: str | Path,
    *,
    model: AnnualMarkovModel,
    evaluation: HindcastReport,
    projection_specification_sha256: str,
    created_by: str,
    created_at: str,
) -> ForecastPackageRecord:
    """Atomically persist one immutable model and its exact hindcast evidence."""

    workspace_id = model.workspace_id.strip()
    actor = str(created_by).strip()
    timestamp = str(created_at).strip()
    projection_hash = str(projection_specification_sha256).strip().lower()
    if not workspace_id or not actor or not timestamp:
        raise ForecastStoreError("forecast_package_invalid", "Workspace, actor, and timestamp are required", status=422)
    if len(projection_hash) != 64:
        raise ForecastStoreError("forecast_package_invalid", "Projection specification hash is invalid", status=422)
    model_json = serialize_annual_markov_model(model)
    configuration_sha256 = __import__("hashlib").sha256(
        canonical_json_text(model.config.canonical_payload()).encode("utf-8")
    ).hexdigest()
    dataset_digest = __import__("hashlib").sha256(
        canonical_json_text(
            {
                "workspace_id": workspace_id,
                "source_receipt_sha256": model.source_receipt_sha256,
                "through_year": model.through_year,
                "projection_specification_sha256": projection_hash,
                "configuration_sha256": configuration_sha256,
            }
        ).encode("utf-8")
    ).hexdigest()
    dataset_id = _identifier("forecast-dataset", workspace_id, dataset_digest)
    model_version_id = _identifier("forecast-model", workspace_id, model.model_id)
    evaluation_run_id = _identifier(
        "forecast-evaluation", workspace_id, evaluation.evaluation_receipt_id
    )
    qualification = (
        ForecastQualification.RELEASED
        if evaluation.release_qualified
        else ForecastQualification.RESEARCH_UNVALIDATED
    )
    statistics_json = canonical_json_text(asdict(model.statistics))
    summary_json = _evaluation_summary(evaluation)
    rows_json = _evaluation_rows(evaluation)

    migrate_database(database_path)
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        workspace = connection.execute(
            "SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)
        ).fetchone()
        if workspace is None:
            raise ForecastStoreError("workspace_not_found", "Workspace not found", status=404)

        dataset_values = (
            dataset_id,
            workspace_id,
            model.source_receipt_sha256,
            model.through_year,
            projection_hash,
            configuration_sha256,
            statistics_json,
            actor,
            timestamp,
        )
        connection.execute(
            "INSERT OR IGNORE INTO forecast_datasets "
            "(id,workspace_id,source_receipt_sha256,through_year,"
            "projection_specification_sha256,configuration_sha256,statistics_json,"
            "created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            dataset_values,
        )
        stored_dataset = connection.execute(
            "SELECT id,workspace_id,source_receipt_sha256,through_year,"
            "projection_specification_sha256,configuration_sha256,statistics_json,"
            "created_by,created_at FROM forecast_datasets WHERE id=? AND workspace_id=?",
            (dataset_id, workspace_id),
        ).fetchone()
        if stored_dataset is None or tuple(stored_dataset) != dataset_values:
            raise ForecastStoreError("forecast_dataset_conflict", "Forecast dataset identity conflict")

        model_values = (
            model_version_id,
            workspace_id,
            dataset_id,
            model.model_id,
            model.config.version,
            model_json,
            qualification.value,
            actor,
            timestamp,
        )
        connection.execute(
            "INSERT OR IGNORE INTO forecast_model_versions "
            "(id,workspace_id,forecast_dataset_id,model_id,engine_version,model_json,"
            "qualification,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            model_values,
        )
        stored_model = connection.execute(
            "SELECT id,workspace_id,forecast_dataset_id,model_id,engine_version,model_json,"
            "qualification,created_by,created_at FROM forecast_model_versions "
            "WHERE id=? AND workspace_id=?",
            (model_version_id, workspace_id),
        ).fetchone()
        if stored_model is None or tuple(stored_model) != model_values:
            raise ForecastStoreError("forecast_model_conflict", "Forecast model identity conflict")

        evaluation_values = (
            evaluation_run_id,
            workspace_id,
            model_version_id,
            evaluation.evaluation_receipt_id,
            evaluation.release_policy_id,
            int(evaluation.release_qualified),
            summary_json,
            rows_json,
            actor,
            timestamp,
        )
        connection.execute(
            "INSERT OR IGNORE INTO forecast_evaluation_runs "
            "(id,workspace_id,forecast_model_version_id,evaluation_receipt_id,"
            "release_policy_id,release_qualified,summary_json,rows_json,created_by,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            evaluation_values,
        )
        stored_evaluation = connection.execute(
            "SELECT id,workspace_id,forecast_model_version_id,evaluation_receipt_id,"
            "release_policy_id,release_qualified,summary_json,rows_json,created_by,created_at "
            "FROM forecast_evaluation_runs WHERE id=? AND workspace_id=?",
            (evaluation_run_id, workspace_id),
        ).fetchone()
        if stored_evaluation is None or tuple(stored_evaluation) != evaluation_values:
            raise ForecastStoreError("forecast_evaluation_conflict", "Forecast evaluation identity conflict")

        existing_rows = connection.execute(
            "SELECT COUNT(*) FROM forecast_hindcast_predictions "
            "WHERE workspace_id=? AND forecast_evaluation_run_id=?",
            (workspace_id, evaluation_run_id),
        ).fetchone()[0]
        if existing_rows not in (0, len(evaluation.rows)):
            raise ForecastStoreError("forecast_hindcast_conflict", "Hindcast row count conflict")
        if existing_rows == 0:
            for ordinal, row in enumerate(evaluation.rows):
                connection.execute(
                    "INSERT INTO forecast_hindcast_predictions "
                    "(id,workspace_id,forecast_evaluation_run_id,row_ordinal,bridge_number,"
                    "component,origin_year,origin_revision_id,target_year,target_revision_id,"
                    "lead_years,model_id,predicted_distribution_json,predicted_rating,"
                    "observed_rating,absolute_error,signed_error,persistence_rating,"
                    "persistence_absolute_error,evidence_stratum,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        _identifier("hindcast-row", workspace_id, f"{evaluation.evaluation_receipt_id}:{ordinal}"),
                        workspace_id,
                        evaluation_run_id,
                        ordinal,
                        row.bridge_number,
                        row.component,
                        row.origin_year,
                        row.origin_revision_id,
                        row.target_year,
                        row.target_revision_id,
                        row.lead_years,
                        row.model_id,
                        canonical_json_text(list(row.predicted_distribution)),
                        row.predicted_rating,
                        row.observed_rating,
                        row.absolute_error,
                        row.signed_error,
                        row.persistence_rating,
                        row.persistence_absolute_error,
                        row.evidence_stratum,
                        timestamp,
                    ),
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return ForecastPackageRecord(
        workspace_id=workspace_id,
        dataset_id=dataset_id,
        model_version_id=model_version_id,
        model_id=model.model_id,
        evaluation_run_id=evaluation_run_id,
        evaluation_receipt_id=evaluation.evaluation_receipt_id,
        qualification=qualification,
        release_qualified=evaluation.release_qualified,
    )


def load_forecast_model(
    database_path: str | Path,
    *,
    workspace_id: str,
    model_version_id: str,
) -> AnnualMarkovModel:
    connection = _connect(database_path)
    try:
        row = connection.execute(
            "SELECT model_json FROM forecast_model_versions WHERE workspace_id=? AND id=?",
            (str(workspace_id).strip(), str(model_version_id).strip()),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ForecastStoreError("forecast_model_not_found", "Forecast model not found", status=404)
    return deserialize_annual_markov_model(str(row["model_json"]))
