#!/usr/bin/env python3
"""Run Bridgette question banks through the real authenticated HTTP API.

This evaluator uses the same ``/api/bridgette/stream`` NDJSON endpoint as the
browser.  It does not import the planner, call a model directly, or write to a
bridge database.  Authentication is read from environment variables and is
never included in the receipt.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import sys
import time
from typing import Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from uuid import uuid4


RUNNER_VERSION = "bridgette-live-api-evaluator-v1"
DEFAULT_BANKS = (
    Path("backend/app/data/query/bridgette_snbi_question_bank_v1.json"),
    Path("backend/app/data/query/bridgette_deck_area_question_bank_v1.json"),
    Path("backend/app/data/query/bridgette_complex_question_bank_v1.json"),
)
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def _validated_endpoint(base_url: str) -> tuple[str, str]:
    """Return a safe API endpoint and a credential-free receipt origin."""

    parsed = urlsplit(str(base_url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base-url must be an absolute http or https URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base-url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("base-url must not contain a query string or fragment")
    clean_path = parsed.path.rstrip("/")
    origin = urlunsplit((parsed.scheme, parsed.netloc, clean_path, "", ""))
    return origin + "/api/bridgette/stream", origin


def _stable_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(value: object) -> str:
    if isinstance(value, bytes):
        data = value
    else:
        data = _stable_json(value).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class LiveCase:
    case_id: str
    bank: str
    category: str
    question: str
    request_scope: Mapping[str, object]
    expected_behavior: str
    must_include: tuple[str, ...]
    any_of: tuple[tuple[str, ...], ...]
    must_not_include: tuple[str, ...]


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _normalized_case(raw: Mapping[str, object], *, bank: str) -> LiveCase:
    semantics = raw.get("expected_semantics")
    if isinstance(semantics, dict):
        must_include = _strings(semantics.get("must_include"))
        any_of = tuple(
            _strings(group)
            for group in semantics.get("any_of", [])
            if isinstance(group, list)
        )
        must_not_include = _strings(semantics.get("must_not_include"))
    else:
        tokens: list[str] = []
        target_resource = raw.get("target_resource")
        target_metric = raw.get("target_metric")
        if target_resource:
            tokens.append(str(target_resource))
        if target_metric:
            tokens.append(str(target_metric))
        tokens.extend(_strings(raw.get("target_fields")))
        filters = raw.get("target_filters")
        if isinstance(filters, list):
            for item in filters:
                if isinstance(item, dict):
                    for key in ("field_id", "value"):
                        if item.get(key) is not None:
                            tokens.append(str(item[key]))
        must_include = tuple(dict.fromkeys(tokens))
        any_of = ()
        must_not_include = ()
    scope = raw.get("request_scope")
    if not isinstance(scope, dict):
        raise ValueError(f"{raw.get('id')} has no request_scope")
    behavior = str(raw.get("expected_behavior", "answer"))
    if behavior not in {"answer", "clarification", "abstention"}:
        raise ValueError(f"{raw.get('id')} has invalid expected behavior")
    return LiveCase(
        case_id=str(raw["id"]),
        bank=bank,
        category=str(raw.get("category", "uncategorized")),
        question=str(raw["question"]),
        request_scope=dict(scope),
        expected_behavior=behavior,
        must_include=must_include,
        any_of=any_of,
        must_not_include=must_not_include,
    )


def load_cases(paths: Iterable[Path]) -> tuple[LiveCase, ...]:
    output: list[LiveCase] = []
    ids: set[str] = set()
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        cases = payload.get("cases")
        if not isinstance(cases, list):
            raise ValueError(f"{path} does not contain a cases list")
        bank = str(payload.get("suite_version", path.stem))
        for raw in cases:
            if not isinstance(raw, dict):
                raise ValueError(f"{path} contains a non-object case")
            case = _normalized_case(raw, bank=bank)
            if case.case_id in ids:
                raise ValueError(f"duplicate case ID across banks: {case.case_id}")
            ids.add(case.case_id)
            output.append(case)
    return tuple(output)


def _bank_receipts(paths: Iterable[Path]) -> list[dict[str, object]]:
    receipts: list[dict[str, object]] = []
    for path in paths:
        source = path.read_bytes()
        payload = json.loads(source.decode("utf-8"))
        receipts.append(
            {
                "path": str(path),
                "suite_version": payload.get("suite_version"),
                "declared_bank_sha256": payload.get("bank_sha256"),
                "file_sha256": hashlib.sha256(source).hexdigest(),
            }
        )
    return receipts


def _form_payload(case: LiveCase) -> bytes:
    scope = case.request_scope
    data: dict[str, str] = {
        "question": case.question,
        "time_mode": str(scope.get("time_mode", "latest")),
    }
    if scope.get("data_year") is not None:
        data["year"] = str(scope["data_year"])
    elif scope.get("year") is not None:
        data["year"] = str(scope["year"])
    for key in ("start_year", "end_year"):
        if scope.get(key) is not None:
            data[key] = str(scope[key])
    return urlencode(data).encode("utf-8")


def _headers(*, request_id: str, cookie: str, bearer_token: str) -> dict[str, str]:
    values = {
        "Accept": "application/x-ndjson",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Request-ID": request_id,
        "User-Agent": RUNNER_VERSION,
    }
    if cookie:
        values["Cookie"] = cookie
    if bearer_token:
        values["Authorization"] = f"Bearer {bearer_token}"
    return values


def _parse_events(content: bytes, *, request_id: str) -> tuple[dict[str, object], ...]:
    events: list[dict[str, object]] = []
    for line in content.decode("utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if not isinstance(event, dict):
            raise ValueError("stream event is not an object")
        if event.get("request_id") != request_id:
            raise ValueError("stream returned an unrelated request ID")
        events.append(event)
    if not events:
        raise ValueError("stream returned no events")
    if events[-1].get("type") not in {"done", "stopped", "error"}:
        raise ValueError("stream ended without a terminal event")
    return tuple(events)


def _semantic_evidence(events: tuple[dict[str, object], ...]) -> tuple[dict[str, object], ...]:
    return tuple(event for event in events if event.get("type") == "evidence")


def _score_case(case: LiveCase, events: tuple[dict[str, object], ...]) -> dict[str, object]:
    event_types = tuple(str(event.get("type")) for event in events)
    evidence = _semantic_evidence(events)
    if evidence:
        observed_behavior = "answer"
    elif "clarification" in event_types:
        observed_behavior = "clarification"
    elif "abstention" in event_types:
        observed_behavior = "abstention"
    else:
        observed_behavior = "error"

    # Inspect only server evidence and decision payloads. The user question is
    # deliberately excluded so an expected term cannot satisfy itself.
    scorable_events = tuple(
        event
        for event in events
        if event.get("type") in {"evidence", "clarification", "abstention", "error"}
    )
    searchable = _stable_json(scorable_events).casefold()
    missing = tuple(
        token for token in case.must_include if token.casefold() not in searchable
    )
    missing_groups = tuple(
        group
        for group in case.any_of
        if not any(token.casefold() in searchable for token in group)
    )
    forbidden = tuple(
        token for token in case.must_not_include if token.casefold() in searchable
    )
    behavior_passed = observed_behavior == case.expected_behavior
    semantic_passed = not missing and not missing_groups and not forbidden
    evidence_verified = True
    database_evidence_passed = True
    scope_passed = True
    if case.expected_behavior == "answer":
        latest = evidence[-1] if evidence else {}
        verification = latest.get("answer_verification")
        evidence_verified = bool(
            isinstance(verification, dict)
            and verification.get("status") == "verified"
        )
        database_evidence_passed = evidence_verified or bool(
            latest.get("answer_mode") == "guarded_raw_sql_fallback"
            and latest.get("answer_status") == "provisional"
            and latest.get("query_language") == "guarded_raw_sql"
        )
        observed_scope = latest.get("scope")
        expected_scope = case.request_scope
        allowed_time_modes = {
            "latest": {"latest", "latest_loaded_inventory"},
            "exact": {"exact", "exact_inventory_year"},
            "trend": {"trend", "group_by_inventory_year"},
        }
        expected_time_mode = str(expected_scope.get("time_mode", "latest"))
        scope_passed = bool(
            isinstance(observed_scope, dict)
            and observed_scope.get("time_mode")
            in allowed_time_modes.get(expected_time_mode, {expected_time_mode})
        )
        if scope_passed and expected_scope.get("data_year") is not None:
            scope_passed = observed_scope.get("year") == expected_scope["data_year"]
        if scope_passed and expected_scope.get("year") is not None:
            scope_passed = observed_scope.get("year") == expected_scope["year"]
        for key in ("start_year", "end_year"):
            if scope_passed and expected_scope.get(key) is not None:
                scope_passed = observed_scope.get(key) == expected_scope[key]
    if case.expected_behavior != "answer":
        # Clarification/abstention correctness is carried primarily by the
        # disposition. Required orientation terms still matter when supplied;
        # forbidden evidence always matters.
        semantic_passed = not missing and not forbidden
    passed = (
        behavior_passed
        and semantic_passed
        and database_evidence_passed
        and scope_passed
    )

    evidence_summary: dict[str, object] | None = None
    if evidence:
        latest = evidence[-1]
        raw_sql = latest.get("sql")
        sql_identifiers = (
            sorted(set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", raw_sql)))[:160]
            if isinstance(raw_sql, str)
            else []
        )
        evidence_summary = {
            "answer_mode": latest.get("answer_mode"),
            "answer_status": latest.get("answer_status"),
            "query_language": latest.get("query_language"),
            "columns": latest.get("columns"),
            "count": latest.get("count"),
            "truncated": latest.get("truncated"),
            "scope": latest.get("scope"),
            "query_plan": latest.get("query_plan"),
            "metric_definitions": latest.get("metric_definitions"),
            "catalog_version": latest.get("catalog_version"),
            "catalog_sha256": latest.get("catalog_sha256"),
            "semantic_retrieval_version": latest.get("semantic_retrieval_version"),
            "semantic_retrieval_sha256": latest.get("semantic_retrieval_sha256"),
            "answer_verification": latest.get("answer_verification"),
            "fallback": latest.get("fallback"),
            "sql_identifiers": sql_identifiers,
        }
    decision_summary: dict[str, object] | None = None
    for event in reversed(events):
        if event.get("type") in {"clarification", "abstention", "error"}:
            decision_summary = {
                "type": event.get("type"),
                "code": event.get("code"),
                "reason": event.get("reason"),
                "message": event.get("message") or event.get("detail"),
                "options": event.get("options"),
            }
            break
    return {
        "passed": passed,
        "expected_behavior": case.expected_behavior,
        "observed_behavior": observed_behavior,
        "behavior_passed": behavior_passed,
        "semantic_passed": semantic_passed,
        "evidence_verified": evidence_verified,
        "database_evidence_passed": database_evidence_passed,
        "scope_passed": scope_passed,
        "missing_terms": list(missing),
        "missing_any_of_groups": [list(group) for group in missing_groups],
        "forbidden_terms_observed": list(forbidden),
        "event_types": list(event_types),
        "evidence": evidence_summary,
        "decision": decision_summary,
    }


def _run_case(
    case: LiveCase,
    *,
    endpoint: str,
    cookie: str,
    bearer_token: str,
    timeout_seconds: float,
    retries: int,
    ssl_context: ssl.SSLContext,
) -> dict[str, object]:
    started = time.perf_counter()
    last_error: str | None = None
    response_status: int | None = None
    for attempt in range(1, retries + 2):
        request_id = str(uuid4())
        request = Request(
            endpoint,
            data=_form_payload(case),
            headers=_headers(
                request_id=request_id,
                cookie=cookie,
                bearer_token=bearer_token,
            ),
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds, context=ssl_context) as response:
                response_status = int(response.status)
                content = response.read(MAX_RESPONSE_BYTES + 1)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise ValueError("API response exceeded the evaluation byte limit")
                events = _parse_events(content, request_id=request_id)
            score = _score_case(case, events)
            return {
                "case_id": case.case_id,
                "bank": case.bank,
                "category": case.category,
                "question_sha256": _sha256(case.question.encode("utf-8")),
                "request_scope": dict(case.request_scope),
                "attempts": attempt,
                "http_status": response_status,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "response_sha256": _sha256(content),
                **score,
            }
        except HTTPError as exc:
            response_status = int(exc.code)
            last_error = f"HTTP {exc.code}: {exc.reason}"
            retryable = exc.code == 429 or 500 <= exc.code <= 599
            if not retryable or attempt > retries:
                break
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            try:
                delay = min(30.0, max(0.25, float(retry_after)))
            except (TypeError, ValueError):
                delay = min(30.0, 2.0 ** (attempt - 1))
            time.sleep(delay)
        except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt > retries:
                break
            time.sleep(min(30.0, 2.0 ** (attempt - 1)))
    return {
        "case_id": case.case_id,
        "bank": case.bank,
        "category": case.category,
        "question_sha256": _sha256(case.question.encode("utf-8")),
        "request_scope": dict(case.request_scope),
        "attempts": retries + 1,
        "http_status": response_status,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "passed": False,
        "expected_behavior": case.expected_behavior,
        "observed_behavior": "transport_error",
        "behavior_passed": False,
        "semantic_passed": False,
        "error": last_error or "unknown transport error",
    }


def _baseline_regressions(results: list[dict[str, object]], path: Path | None) -> list[str]:
    if path is None:
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    baseline = {
        str(item.get("case_id")): bool(item.get("passed"))
        for item in payload.get("results", [])
        if isinstance(item, dict)
    }
    return sorted(
        str(item["case_id"])
        for item in results
        if baseline.get(str(item["case_id"])) is True and not item.get("passed")
    )


def run(arguments: argparse.Namespace) -> dict[str, object]:
    cases = list(load_cases(arguments.bank))
    selected_case_ids = tuple(dict.fromkeys(getattr(arguments, "case_id", None) or ()))
    if selected_case_ids:
        available = {case.case_id for case in cases}
        missing = sorted(set(selected_case_ids).difference(available))
        if missing:
            raise ValueError(f"unknown evaluation case ids: {', '.join(missing)}")
        selected = set(selected_case_ids)
        cases = [case for case in cases if case.case_id in selected]
    if arguments.case_prefix:
        cases = [case for case in cases if case.case_id.startswith(arguments.case_prefix)]
    if arguments.offset:
        cases = cases[arguments.offset :]
    if arguments.limit is not None:
        cases = cases[: arguments.limit]
    if not cases:
        raise ValueError("no evaluation cases were selected")

    cookie = os.getenv(arguments.cookie_env, "").strip()
    bearer_token = os.getenv(arguments.bearer_env, "").strip()
    endpoint, endpoint_origin = _validated_endpoint(arguments.base_url)
    ssl_context = ssl.create_default_context()
    if arguments.insecure_tls:
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    results: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=arguments.concurrency) as executor:
        futures = {
            executor.submit(
                _run_case,
                case,
                endpoint=endpoint,
                cookie=cookie,
                bearer_token=bearer_token,
                timeout_seconds=arguments.timeout,
                retries=arguments.retries,
                ssl_context=ssl_context,
            ): case.case_id
            for case in cases
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            status = "PASS" if result.get("passed") else "FAIL"
            print(f"{status} {result['case_id']} {result['elapsed_ms']} ms", flush=True)
    order = {case.case_id: index for index, case in enumerate(cases)}
    results.sort(key=lambda item: order[str(item["case_id"])])
    passed = sum(bool(item.get("passed")) for item in results)
    verified_answers = sum(
        bool(item.get("passed"))
        and item.get("expected_behavior") == "answer"
        and bool(item.get("evidence_verified"))
        for item in results
    )
    provisional_answers = sum(
        bool(item.get("passed"))
        and item.get("expected_behavior") == "answer"
        and not bool(item.get("evidence_verified"))
        for item in results
    )
    expected_answers = sum(case.expected_behavior == "answer" for case in cases)
    category_rates: dict[str, float] = {}
    for category in sorted({case.category for case in cases}):
        selected = [item for item in results if item["category"] == category]
        category_rates[category] = sum(bool(item.get("passed")) for item in selected) / len(selected)
    regressions = _baseline_regressions(results, arguments.baseline)
    security_results = [
        item
        for item in results
        if item["category"] == "ambiguity_unsupported_security"
    ]
    security_rate = (
        sum(bool(item.get("passed")) for item in security_results) / len(security_results)
        if security_results
        else 1.0
    )
    overall_rate = passed / len(results)
    release_passed = (
        overall_rate >= arguments.minimum_pass_rate
        and security_rate == 1.0
        and not regressions
    )
    return {
        "receipt_version": 1,
        "runner_version": RUNNER_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "endpoint_origin": endpoint_origin,
        "authentication_present": bool(cookie or bearer_token),
        "authentication_values_recorded": False,
        "bank_paths": [str(path) for path in arguments.bank],
        "bank_receipts": _bank_receipts(arguments.bank),
        "selection_offset": arguments.offset,
        "selection_case_ids": list(selected_case_ids),
        "case_count": len(results),
        "passed_count": passed,
        "pass_rate": overall_rate,
        "verified_answer_count": verified_answers,
        "verified_answer_rate": (
            verified_answers / expected_answers if expected_answers else 1.0
        ),
        "provisional_answer_count": provisional_answers,
        "minimum_pass_rate": arguments.minimum_pass_rate,
        "category_pass_rates": category_rates,
        "security_pass_rate": security_rate,
        "baseline_regressions": regressions,
        "release_passed": release_passed,
        "results": results,
    }


def parser() -> argparse.ArgumentParser:
    output = argparse.ArgumentParser()
    output.add_argument("--base-url", default="http://127.0.0.1:8000")
    output.add_argument("--bank", type=Path, action="append", default=None)
    output.add_argument("--output", type=Path, required=True)
    output.add_argument("--baseline", type=Path)
    output.add_argument("--case-prefix")
    output.add_argument("--case-id", action="append")
    output.add_argument("--offset", type=int, default=0)
    output.add_argument("--limit", type=int)
    output.add_argument("--concurrency", type=int, default=1, choices=range(1, 5))
    output.add_argument("--timeout", type=float, default=360.0)
    output.add_argument("--retries", type=int, default=2, choices=range(0, 6))
    output.add_argument("--minimum-pass-rate", type=float, default=0.97)
    output.add_argument("--cookie-env", default="BRIDGETTE_EVAL_COOKIE")
    output.add_argument("--bearer-env", default="BRIDGETTE_EVAL_BEARER_TOKEN")
    output.add_argument("--insecure-tls", action="store_true")
    return output


def main() -> int:
    arguments = parser().parse_args()
    if arguments.bank is None:
        arguments.bank = list(DEFAULT_BANKS)
    if not 0.0 <= arguments.minimum_pass_rate <= 1.0:
        raise ValueError("minimum-pass-rate must be between 0 and 1")
    if arguments.offset < 0:
        raise ValueError("offset must be zero or greater")
    receipt = run(arguments)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(receipt, allow_nan=False, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"{receipt['passed_count']}/{receipt['case_count']} passed "
        f"({receipt['pass_rate']:.1%}); release_passed={receipt['release_passed']}"
    )
    return 0 if receipt["release_passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("evaluation interrupted", file=sys.stderr)
        raise SystemExit(130)
