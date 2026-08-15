"""Fit, hindcast, and persist one research forecast package."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from app.services.analytics.projection import (
    ANALYTICAL_SPECIFICATION_SHA256,
    load_state_analysis_histories,
)
from app.services.forecasting_v2.estimator import (
    ForecastModelConfig,
    build_annual_markov_model,
)
from app.services.forecasting_v2.evaluation import (
    ForecastReleasePolicy,
    evaluate_historical_hindcasts,
)
from app.services.forecasting_v2.store import persist_forecast_package


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--state-code", type=int, default=34)
    parser.add_argument("--through-year", type=int, required=True)
    parser.add_argument("--origins", required=True, help="Comma-separated hindcast origins")
    parser.add_argument("--created-by", required=True)
    parser.add_argument("--minimum-cohort-exposure", type=int, default=25)
    parser.add_argument("--receipt", type=Path)
    return parser


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    origins = tuple(
        sorted({int(value.strip()) for value in args.origins.split(",") if value.strip()})
    )
    if not origins or any(year >= args.through_year for year in origins):
        raise SystemExit("origins must be nonempty and precede through-year")

    histories = load_state_analysis_histories(
        database_path=args.database,
        workspace_id=args.workspace_id,
        state_code=args.state_code,
        through_year=args.through_year,
    )
    config = ForecastModelConfig(
        minimum_cohort_exposure=args.minimum_cohort_exposure
    )
    model = build_annual_markov_model(
        histories,
        through_year=args.through_year,
        config=config,
    )
    evaluation = evaluate_historical_hindcasts(
        histories,
        origins=origins,
        config=config,
        release_policy=ForecastReleasePolicy(
            minimum_target_rows=100,
            minimum_mae_skill=0.0,
            maximum_candidate_mae=1.5,
        ),
    )
    package = persist_forecast_package(
        args.database,
        model=model,
        evaluation=evaluation,
        projection_specification_sha256=ANALYTICAL_SPECIFICATION_SHA256,
        created_by=args.created_by,
        created_at=_now(),
    )
    receipt = {
        "format": "bridgeguard-published-forecast-training-v1",
        "workspace_id": args.workspace_id,
        "state_code": args.state_code,
        "through_year": args.through_year,
        "origins": list(origins),
        "history_count": len(histories),
        "snapshot_count": sum(len(history.snapshots) for history in histories),
        "training_histories": model.statistics.bridge_histories,
        "used_intervals": model.statistics.used_intervals,
        "model_id": package.model_id,
        "model_version_id": package.model_version_id,
        "evaluation_run_id": package.evaluation_run_id,
        "evaluation_receipt_id": package.evaluation_receipt_id,
        "hindcast_rows": evaluation.summary.target_rows,
        "candidate_mae": evaluation.summary.candidate_mae,
        "persistence_mae": evaluation.summary.persistence_mae,
        "mae_skill": evaluation.summary.mae_skill,
        "qualification": package.qualification.value,
        "release_qualified": package.release_qualified,
    }
    serialized = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    if args.receipt is not None:
        destination = args.receipt.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    sys.exit(main())
