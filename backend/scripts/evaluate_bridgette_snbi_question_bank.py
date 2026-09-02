"""Run the SNBI question bank through Bridgette's production plan pipeline.

This is an integration evaluator, not a mocked contract test. It calls the
configured Bridgette provider, compiles and executes accepted plans against a
real local bridge database, and records the emitted behavior and evidence.
Provider credentials are loaded from backend/.env but are never printed or
written to the evaluation report.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any
from uuid import uuid4


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
DEFAULT_BANK = (
    BACKEND_ROOT
    / "app"
    / "data"
    / "query"
    / "bridgette_snbi_question_bank_v1.json"
)
DEFAULT_DATABASE = (
    REPOSITORY_ROOT / "tmp" / "local-bridgette" / "data" / "snbi_bridges.db"
)
DEFAULT_DEPENDENCIES = REPOSITORY_ROOT / "tmp" / "bridgette-eval-deps"
DEFAULT_OUTPUT = (
    REPOSITORY_ROOT
    / "tmp"
    / "local-bridgette"
    / "bridgette-snbi-question-bank-results.json"
)
WORKSPACE_ID = "shared-research-workspace"
STATE_CODE = 34


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _configure_imports(dependencies: Path, data_dir: Path, query_mode: str) -> None:
    _load_dotenv(BACKEND_ROOT / ".env")
    os.environ["BRIDGEGUARD_QUERY_MODE"] = query_mode
    os.environ["BRIDGEGUARD_DATA_DIR"] = str(data_dir)
    os.environ.setdefault("BRIDGEGUARD_SQLITE_JOURNAL_MODE", "delete")
    if dependencies.is_dir():
        sys.path.insert(0, str(dependencies))
    sys.path.insert(0, str(BACKEND_ROOT))


@dataclass
class EvaluationRequest:
    request_id: str
    question: str
    scope: Any
    model: str = "ignored-client-model"
    surface: str = "full"
    row_limit: int = 200
    actor_id: str = "bridgette-evaluator"
    actor_role: str = "viewer"
    cancellation_token: threading.Event = threading.Event()


def _scope_from_case(case: dict[str, Any], query_scope_type: type) -> Any:
    supplied = case["request_scope"]
    mode = supplied["time_mode"]
    kwargs: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "state_code": STATE_CODE,
        "time_mode": mode,
    }
    if mode == "exact":
        kwargs["data_year"] = supplied["year"]
    elif mode == "trend":
        kwargs["start_year"] = supplied["start_year"]
        kwargs["end_year"] = supplied["end_year"]
    return query_scope_type(**kwargs)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _classify(events: list[dict[str, Any]]) -> tuple[str, str]:
    for event in events:
        if event.get("type") == "clarification":
            return "clarification", str(event.get("code") or "clarification")
        if event.get("type") == "abstention":
            return "abstention", str(event.get("code") or "abstention")
    if any(event.get("type") == "evidence" for event in events):
        return "answer", "ready_for_planning"
    return "error", "missing_terminal_event"


def _target_alignment(
    case: dict[str, Any], evidence: dict[str, Any] | None
) -> dict[str, Any]:
    if evidence is None:
        return {
            "checked": False,
            "resource_match": None,
            "metric_match": None,
        }
    plan = evidence.get("query_plan")
    if not isinstance(plan, dict):
        return {
            "checked": False,
            "resource_match": None,
            "metric_match": None,
        }
    canonical_plan = plan.get("canonical_plan")
    if isinstance(canonical_plan, dict):
        plan = canonical_plan
    expected_resource = case.get("target_resource")
    actual_resource = plan.get("resource_id")
    expected_metric = case.get("target_metric")
    actual_metrics = plan.get("metric_ids")
    if not isinstance(actual_metrics, list):
        actual_metrics = []
    return {
        "checked": True,
        "expected_resource": expected_resource,
        "actual_resource": actual_resource,
        "resource_match": (
            None if expected_resource is None else actual_resource == expected_resource
        ),
        "expected_metric": expected_metric,
        "actual_metrics": actual_metrics,
        "metric_match": (
            None if expected_metric is None else expected_metric in actual_metrics
        ),
    }


def _evaluate_case(
    case: dict[str, Any],
    *,
    database: Path,
    query_scope_type: type,
    stream_query: Any,
) -> dict[str, Any]:
    started = time.monotonic()
    request = EvaluationRequest(
        request_id=str(uuid4()),
        question=case["question"],
        scope=_scope_from_case(case, query_scope_type),
        cancellation_token=threading.Event(),
    )
    events: list[dict[str, Any]] = []
    error: dict[str, Any] | None = None
    try:
        events = [
            _json_safe(event)
            for event in stream_query(
                database_path=database,
                workspace_id=WORKSPACE_ID,
                request=request,
            )
        ]
        actual_behavior, actual_code = _classify(events)
    except Exception as exc:  # Integration report must retain the failed case.
        actual_behavior = "error"
        actual_code = str(getattr(exc, "code", None) or type(exc).__name__)
        error = {
            "type": type(exc).__name__,
            "code": getattr(exc, "code", None),
            "message": str(exc)[:1000],
            "details": _json_safe(getattr(exc, "details", None)),
        }

    evidence = next(
        (event for event in events if event.get("type") == "evidence"),
        None,
    )
    token_text = "".join(
        str(event.get("content") or "")
        for event in events
        if event.get("type") == "token"
    )
    expected_behavior = case["expected_behavior"]
    expected_code = case["expected_code"]
    return {
        "id": case["id"],
        "category": case["category"],
        "question": case["question"],
        "request_scope": case["request_scope"],
        "expected_behavior": expected_behavior,
        "actual_behavior": actual_behavior,
        "behavior_match": actual_behavior == expected_behavior,
        "expected_code": expected_code,
        "actual_code": actual_code,
        "code_match": actual_code == expected_code,
        "duration_seconds": round(time.monotonic() - started, 3),
        "event_types": [str(event.get("type")) for event in events],
        "target_alignment": _target_alignment(case, evidence),
        "evidence": evidence,
        "answer_markdown": token_text[:20_000] or None,
        "decision_event": next(
            (
                event
                for event in events
                if event.get("type") in {"clarification", "abstention"}
            ),
            None,
        ),
        "error": error,
    }


def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    behavior_counts: dict[str, int] = {}
    mismatch_ids: list[str] = []
    code_mismatch_ids: list[str] = []
    resource_mismatch_ids: list[str] = []
    metric_mismatch_ids: list[str] = []
    for result in results:
        behavior = result["actual_behavior"]
        behavior_counts[behavior] = behavior_counts.get(behavior, 0) + 1
        if not result["behavior_match"]:
            mismatch_ids.append(result["id"])
        if not result["code_match"]:
            code_mismatch_ids.append(result["id"])
        alignment = result["target_alignment"]
        if alignment.get("resource_match") is False:
            resource_mismatch_ids.append(result["id"])
        if alignment.get("metric_match") is False:
            metric_mismatch_ids.append(result["id"])
    return {
        "case_count": len(results),
        "behavior_counts": behavior_counts,
        "behavior_match_count": len(results) - len(mismatch_ids),
        "behavior_mismatch_count": len(mismatch_ids),
        "behavior_mismatch_ids": mismatch_ids,
        "code_mismatch_ids": code_mismatch_ids,
        "resource_mismatch_ids": resource_mismatch_ids,
        "metric_mismatch_ids": metric_mismatch_ids,
        "total_duration_seconds": round(
            sum(float(result["duration_seconds"]) for result in results), 3
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bank", type=Path, default=DEFAULT_BANK)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--dependencies", type=Path, default=DEFAULT_DEPENDENCIES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=100)
    parser.add_argument("--delay-seconds", type=float, default=0.0)
    parser.add_argument(
        "--query-mode",
        choices=("plan_required", "expert_sql_fallback", "raw_schema"),
        default="plan_required",
    )
    args = parser.parse_args()

    _configure_imports(
        args.dependencies.resolve(),
        args.database.resolve().parent,
        args.query_mode,
    )
    from app.services.query.http_service import stream_bridgette_semantic_query
    from app.services.query.sql_ast import QueryScope

    bank = json.loads(args.bank.read_text(encoding="utf-8"))
    cases = bank["cases"]
    if args.start < 1 or args.end > len(cases) or args.start > args.end:
        raise ValueError("start/end must select a valid one-based case range")

    selected = cases[args.start - 1 : args.end]
    results: list[dict[str, Any]] = []
    run_started = datetime.now(timezone.utc).isoformat()
    for offset, case in enumerate(selected, start=1):
        result = _evaluate_case(
            case,
            database=args.database.resolve(),
            query_scope_type=QueryScope,
            stream_query=stream_bridgette_semantic_query,
        )
        results.append(result)
        print(
            f"{case['id']} expected={case['expected_behavior']} "
            f"actual={result['actual_behavior']} code={result['actual_code']} "
            f"match={result['behavior_match']} seconds={result['duration_seconds']}",
            flush=True,
        )
        if args.delay_seconds > 0 and offset < len(selected):
            time.sleep(args.delay_seconds)

    payload = {
        "schema_version": 1,
        "suite_version": bank["suite_version"],
        "run_started_at": run_started,
        "run_finished_at": datetime.now(timezone.utc).isoformat(),
        "database": str(args.database.resolve()),
        "workspace_id": WORKSPACE_ID,
        "state_code": STATE_CODE,
        "query_mode": args.query_mode,
        "model": os.getenv("BRIDGETTE_CEREBRAS_MODEL", ""),
        "selected_range": {"start": args.start, "end": args.end},
        "summary": _summary(results),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(json.dumps(payload["summary"], indent=2), flush=True)
    print(f"report={args.output.resolve()}", flush=True)
    return 0 if payload["summary"]["behavior_mismatch_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
