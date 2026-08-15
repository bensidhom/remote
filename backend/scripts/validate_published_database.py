"""Validate a published research database before an atomic OCI cutover."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import uuid

from app.routers.services.db_schema import LATEST_SCHEMA_VERSION
from app.services.analytics.projection import load_bridge_analysis_history
from app.services.conversion.revision_store import get_workspace_state
from app.services.forecasting_v2.runs import create_bridge_forecast_run
from app.services.kpis_v2.repository import (
    compute_age_condition_area,
    compute_operational_kpis,
)
from app.services.query.service import (
    SemanticQueryRequest,
    ServerQueryIdentity,
    execute_semantic_plan_query,
)
from app.services.query.sql_ast import QueryScope, TimeMode


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--state-code", type=int, default=34)
    parser.add_argument("--bridge-number", default="0161151")
    parser.add_argument("--actor-id", required=True)
    parser.add_argument("--receipt", type=Path)
    return parser


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database = args.database.expanduser().resolve(strict=True)
    state = get_workspace_state(str(database), args.workspace_id)
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        bridge_count = int(connection.execute("SELECT COUNT(*) FROM bridges").fetchone()[0])
        revision_count = int(connection.execute("SELECT COUNT(*) FROM snapshot_revisions").fetchone()[0])
        published_count = int(connection.execute("SELECT COUNT(*) FROM published_snapshots").fetchone()[0])
        forecast_packages = int(connection.execute("SELECT COUNT(*) FROM forecast_evaluation_runs").fetchone()[0])
    finally:
        connection.close()
    if quick_check != "ok" or foreign_key_violations:
        raise SystemExit("database integrity validation failed")
    if user_version != LATEST_SCHEMA_VERSION or state.schema_generation != LATEST_SCHEMA_VERSION:
        raise SystemExit("database schema generation is not current")
    if not bridge_count or revision_count != bridge_count or published_count != bridge_count:
        raise SystemExit("published revision counts do not reconcile with bridge snapshots")
    if forecast_packages < 1:
        raise SystemExit("no evaluated forecast package is available")

    kpis = compute_operational_kpis(
        database_path=database,
        workspace_id=args.workspace_id,
        state_code=args.state_code,
    )
    age = compute_age_condition_area(
        database_path=database,
        workspace_id=args.workspace_id,
        state_code=args.state_code,
    )
    request = SemanticQueryRequest(
        request_id=str(uuid.uuid4()),
        identity=ServerQueryIdentity(
            actor_id=args.actor_id,
            role="admin",
            workspace_id=args.workspace_id,
            surface="release-validation",
        ),
        scope=QueryScope(
            workspace_id=args.workspace_id,
            state_code=args.state_code,
            time_mode=TimeMode.LATEST,
        ),
        question="What percentage of bridges have concrete decks?",
        database_identity="oci-published-candidate",
        schema_generation=state.schema_generation,
        data_generation=state.data_generation,
        model_id="deterministic-release-validation",
        max_rows=10,
    )
    evidence = execute_semantic_plan_query(
        database_path=database,
        request=request,
        generate_plan=lambda _request: {
            "intent": "aggregate",
            "resource_id": "v_current_bridge_deck_material_summary",
            "metric_ids": [
                "inventory.current_concrete_deck_bridge_count",
                "inventory.current_concrete_deck_bridge_percent",
            ],
            "time_scope": {"mode": "latest"},
        },
    )
    if evidence.row_count != 1 or len(evidence.rows[0]) != 2:
        raise SystemExit("Bridgette deck-material query did not return one aggregate row")

    history = load_bridge_analysis_history(
        database_path=database,
        workspace_id=args.workspace_id,
        state_code=args.state_code,
        bridge_number=args.bridge_number,
    )
    forecast = create_bridge_forecast_run(
        database,
        history=history,
        horizon_years=10,
        idempotency_key=f"release-validation:{args.bridge_number}:10",
        created_by=args.actor_id,
        created_at=_now(),
        expected_data_generation=state.data_generation,
        require_released=False,
    )
    receipt = {
        "format": "bridgeguard-published-database-validation-v1",
        "database_size": database.stat().st_size,
        "quick_check": quick_check,
        "foreign_key_violation_count": foreign_key_violations,
        "user_version": user_version,
        "workspace_id": args.workspace_id,
        "data_generation": state.data_generation,
        "schema_generation": state.schema_generation,
        "bridge_snapshot_count": bridge_count,
        "revision_count": revision_count,
        "published_count": published_count,
        "kpi_year": kpis["year"],
        "kpi_total_bridges": kpis["values"]["total_bridges"],
        "state_maintained_acceptable_percent": kpis["values"]["state_maintained_20ft_acceptable"]["percent_classifiable"],
        "age_condition_total_ft2": round(sum(row["total_ft2"] for row in age["series"]), 3),
        "current_concrete_deck_count": evidence.rows[0][0],
        "current_concrete_deck_percent": evidence.rows[0][1],
        "query_elapsed_ms": evidence.elapsed_ms,
        "forecast_bridge_number": forecast.bridge_number,
        "forecast_as_of_year": forecast.as_of_year,
        "forecast_prediction_count": len(forecast.predictions),
        "forecast_qualification": forecast.qualification.value,
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
