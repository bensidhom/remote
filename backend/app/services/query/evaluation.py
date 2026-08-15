"""Offline, versioned release evaluation for Bridgette semantic queries.

The suite is intentionally independent of a language model, network access,
and database execution.  It protects the deterministic boundaries around the
model: pre-query interpretation, the typed ``BridgeQueryPlan`` contract,
reviewed catalog/metric identifiers, server-owned scope compilation, and
evidence-backed answer claims.

The checked-in fixture is SHA-256 sealed.  Changing a golden question, plan,
decision, threshold, or evidence assertion therefore requires an intentional
fixture-hash update that is visible in code review.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .answers import EvidenceTable, parse_structured_narrative, verify_structured_narrative
from .interpretation import InterpretationDecision, interpret_bridge_query
from .metrics import DEFAULT_METRIC_REGISTRY
from .models import MetricRegistry, SemanticCatalog
from .plans import BridgeQueryPlan, PlanError, PlanOperator, compile_bridge_query_plan
from .semantic import build_semantic_catalog
from .sql_ast import QueryScope


EVALUATION_SCHEMA_VERSION = 1
EVALUATION_SUITE_VERSION = "bridgeguard-query-eval-v1"
MAX_FIXTURE_BYTES = 1_048_576
DEFAULT_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "query"
    / "bridge_query_eval_v1.json"
)

_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOP_LEVEL_KEYS = frozenset(
    {"schema_version", "suite_version", "fixture_hash", "thresholds", "cases"}
)
_CASE_KEYS = frozenset(
    {
        "id",
        "category",
        "question",
        "expected_decision",
        "expected_code",
        "bridge_number",
        "server_scope",
        "plan",
        "rejection_probe",
        "answer",
    }
)
_SCOPE_KEYS = frozenset(
    {
        "workspace_id",
        "state_code",
        "time_mode",
        "bridge_number",
        "data_year",
        "start_year",
        "end_year",
    }
)
_FORBIDDEN_PLAN_SCOPE_KEYS = frozenset(
    {
        "workspace",
        "workspace_id",
        "state",
        "state_code",
        "bridge",
        "bridge_number",
        "data_year",
        "year",
        "start_year",
        "end_year",
        "revision_id",
        "snapshot_revision_id",
    }
)


class EvaluationFixtureError(ValueError):
    """A checked-in or caller-supplied evaluation fixture is invalid."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class EvaluationStage(str, Enum):
    INTERPRETATION = "interpretation"
    PLAN = "plan"
    ANSWER = "answer"
    COMPLETE = "complete"


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise EvaluationFixtureError(
            "fixture_not_canonical", "Evaluation fixture is not canonical JSON"
        ) from exc


def calculate_fixture_hash(payload: Mapping[str, object]) -> str:
    """Hash a fixture payload, excluding its self-describing hash field."""

    if not isinstance(payload, Mapping):
        raise EvaluationFixtureError(
            "invalid_fixture_type", "Evaluation fixture must be a JSON object"
        )
    unsigned = {
        str(key): _thaw_json(value)
        for key, value in payload.items()
        if str(key) != "fixture_hash"
    }
    return hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EvalThresholds:
    minimum_overall_pass_rate: float
    minimum_category_pass_rate: float

    def __post_init__(self) -> None:
        for field_name in (
            "minimum_overall_pass_rate",
            "minimum_category_pass_rate",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise EvaluationFixtureError(
                    "invalid_threshold", f"{field_name} must be a number"
                )
            normalized = float(value)
            if not 0.0 <= normalized <= 1.0:
                raise EvaluationFixtureError(
                    "invalid_threshold", f"{field_name} must be between zero and one"
                )
            object.__setattr__(self, field_name, normalized)

    def to_dict(self) -> dict[str, float]:
        return {
            "minimum_overall_pass_rate": self.minimum_overall_pass_rate,
            "minimum_category_pass_rate": self.minimum_category_pass_rate,
        }


@dataclass(frozen=True, slots=True)
class EvalCase:
    case_id: str
    category: str
    question: str
    expected_decision: InterpretationDecision
    expected_code: str
    bridge_number: str | None = None
    server_scope: Mapping[str, object] | None = None
    plan: Mapping[str, object] | None = None
    rejection_probe: Mapping[str, object] | None = None
    answer: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.case_id):
            raise EvaluationFixtureError("invalid_case_id", "Case id is not stable")
        if not _SAFE_ID.fullmatch(self.category):
            raise EvaluationFixtureError("invalid_category", "Case category is not stable")
        if not self.question.strip():
            raise EvaluationFixtureError("invalid_question", "Case question is required")
        if not _SAFE_ID.fullmatch(self.expected_code):
            raise EvaluationFixtureError(
                "invalid_expected_code", "Expected interpretation code is invalid"
            )
        object.__setattr__(self, "question", self.question.strip())
        if self.bridge_number is not None:
            bridge_number = str(self.bridge_number).strip()
            if not bridge_number:
                raise EvaluationFixtureError(
                    "invalid_bridge_number", "bridge_number cannot be blank"
                )
            object.__setattr__(self, "bridge_number", bridge_number)
        for field_name in ("server_scope", "plan", "rejection_probe", "answer"):
            value = getattr(self, field_name)
            if value is not None:
                if not isinstance(value, Mapping):
                    raise EvaluationFixtureError(
                        "invalid_case_field", f"{field_name} must be an object or null"
                    )
                object.__setattr__(self, field_name, _freeze_json(value))

        if self.expected_decision is InterpretationDecision.PROCEED:
            if self.plan is None or self.server_scope is None:
                raise EvaluationFixtureError(
                    "missing_golden_plan",
                    "Every proceed case requires a canonical plan and server scope",
                )
        elif self.plan is not None:
            raise EvaluationFixtureError(
                "unexpected_golden_plan", "Non-proceed cases cannot carry a golden plan"
            )
        if self.answer is not None and self.expected_decision is not InterpretationDecision.PROCEED:
            raise EvaluationFixtureError(
                "unexpected_answer_check", "Only proceed cases can verify answer evidence"
            )


@dataclass(frozen=True, slots=True)
class EvaluationFixture:
    schema_version: int
    suite_version: str
    fixture_hash: str
    thresholds: EvalThresholds
    cases: tuple[EvalCase, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "cases", tuple(self.cases))


@dataclass(frozen=True, slots=True)
class EvalCaseResult:
    case_id: str
    category: str
    passed: bool
    stage: EvaluationStage
    code: str
    detail: str
    plan_hash: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "passed": self.passed,
            "stage": self.stage.value,
            "code": self.code,
            "detail": self.detail,
            "plan_hash": self.plan_hash,
        }


@dataclass(frozen=True, slots=True)
class EvalReport:
    schema_version: int
    suite_version: str
    fixture_hash: str
    thresholds: EvalThresholds
    total_cases: int
    passed_cases: int
    overall_pass_rate: float
    category_pass_rates: Mapping[str, float]
    release_passed: bool
    results: tuple[EvalCaseResult, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "category_pass_rates",
            MappingProxyType(dict(sorted(self.category_pass_rates.items()))),
        )
        object.__setattr__(self, "results", tuple(self.results))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "suite_version": self.suite_version,
            "fixture_hash": self.fixture_hash,
            "thresholds": self.thresholds.to_dict(),
            "total_cases": self.total_cases,
            "passed_cases": self.passed_cases,
            "overall_pass_rate": self.overall_pass_rate,
            "category_pass_rates": dict(self.category_pass_rates),
            "release_passed": self.release_passed,
            "results": [result.to_dict() for result in self.results],
        }


def _reject_duplicate_keys(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise EvaluationFixtureError(
                "duplicate_fixture_key", f"Duplicate JSON key: {key}"
            )
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> object:
    raise EvaluationFixtureError(
        "nonfinite_fixture_number", f"Non-finite JSON number is not allowed: {value}"
    )


def _strict_keys(
    payload: Mapping[str, object], allowed: frozenset[str], context: str
) -> None:
    unknown = sorted(set(payload).difference(allowed))
    if unknown:
        raise EvaluationFixtureError(
            "unknown_fixture_field", f"{context} has unknown fields: {', '.join(unknown)}"
        )


def _required_string(payload: Mapping[str, object], key: str, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EvaluationFixtureError(
            "invalid_fixture_field", f"{context}.{key} must be non-blank text"
        )
    return value.strip()


def _parse_case(payload: object) -> EvalCase:
    if not isinstance(payload, Mapping):
        raise EvaluationFixtureError("invalid_case", "Every case must be an object")
    _strict_keys(payload, _CASE_KEYS, "case")
    required = {"id", "category", "question", "expected_decision", "expected_code"}
    missing = sorted(required.difference(payload))
    if missing:
        raise EvaluationFixtureError(
            "missing_fixture_field", f"Case is missing: {', '.join(missing)}"
        )
    decision_text = _required_string(payload, "expected_decision", "case")
    try:
        decision = InterpretationDecision(decision_text)
    except ValueError as exc:
        raise EvaluationFixtureError(
            "invalid_expected_decision", "Case has an invalid expected decision"
        ) from exc
    bridge_number = payload.get("bridge_number")
    if bridge_number is not None and not isinstance(bridge_number, str):
        raise EvaluationFixtureError(
            "invalid_bridge_number", "case.bridge_number must be text or null"
        )
    return EvalCase(
        case_id=_required_string(payload, "id", "case"),
        category=_required_string(payload, "category", "case"),
        question=_required_string(payload, "question", "case"),
        expected_decision=decision,
        expected_code=_required_string(payload, "expected_code", "case"),
        bridge_number=bridge_number,
        server_scope=payload.get("server_scope"),
        plan=payload.get("plan"),
        rejection_probe=payload.get("rejection_probe"),
        answer=payload.get("answer"),
    )


def load_evaluation_fixture(
    path: str | Path | None = None,
) -> EvaluationFixture:
    """Load and integrity-check a bounded evaluation fixture."""

    fixture_path = Path(path) if path is not None else DEFAULT_FIXTURE_PATH
    try:
        raw_bytes = fixture_path.read_bytes()
    except OSError as exc:
        raise EvaluationFixtureError(
            "fixture_read_failed", "Evaluation fixture could not be read"
        ) from exc
    if not raw_bytes or len(raw_bytes) > MAX_FIXTURE_BYTES:
        raise EvaluationFixtureError(
            "fixture_size_invalid", "Evaluation fixture is empty or too large"
        )
    try:
        payload = json.loads(
            raw_bytes.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except EvaluationFixtureError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EvaluationFixtureError(
            "fixture_json_invalid", "Evaluation fixture must be valid UTF-8 JSON"
        ) from exc
    if not isinstance(payload, Mapping):
        raise EvaluationFixtureError(
            "invalid_fixture_type", "Evaluation fixture must be a JSON object"
        )
    _strict_keys(payload, _TOP_LEVEL_KEYS, "fixture")
    missing = sorted(_TOP_LEVEL_KEYS.difference(payload))
    if missing:
        raise EvaluationFixtureError(
            "missing_fixture_field", f"Fixture is missing: {', '.join(missing)}"
        )

    supplied_hash = payload["fixture_hash"]
    if not isinstance(supplied_hash, str) or not _SHA256.fullmatch(supplied_hash):
        raise EvaluationFixtureError(
            "invalid_fixture_hash", "fixture_hash must be lowercase SHA-256"
        )
    calculated_hash = calculate_fixture_hash(payload)
    if supplied_hash != calculated_hash:
        raise EvaluationFixtureError(
            "fixture_hash_mismatch", "Evaluation fixture hash does not match its contents"
        )

    schema_version = payload["schema_version"]
    if isinstance(schema_version, bool) or schema_version != EVALUATION_SCHEMA_VERSION:
        raise EvaluationFixtureError(
            "unsupported_schema_version", "Unsupported evaluation fixture schema"
        )
    suite_version = payload["suite_version"]
    if suite_version != EVALUATION_SUITE_VERSION:
        raise EvaluationFixtureError(
            "unsupported_suite_version", "Unsupported evaluation suite version"
        )
    thresholds_payload = payload["thresholds"]
    if not isinstance(thresholds_payload, Mapping):
        raise EvaluationFixtureError(
            "invalid_threshold", "Fixture thresholds must be an object"
        )
    threshold_keys = frozenset(
        {"minimum_overall_pass_rate", "minimum_category_pass_rate"}
    )
    _strict_keys(thresholds_payload, threshold_keys, "thresholds")
    if set(thresholds_payload) != set(threshold_keys):
        raise EvaluationFixtureError(
            "missing_fixture_field", "Fixture thresholds are incomplete"
        )
    thresholds = EvalThresholds(
        minimum_overall_pass_rate=thresholds_payload["minimum_overall_pass_rate"],
        minimum_category_pass_rate=thresholds_payload["minimum_category_pass_rate"],
    )
    cases_payload = payload["cases"]
    if not isinstance(cases_payload, list) or not 1 <= len(cases_payload) <= 500:
        raise EvaluationFixtureError(
            "invalid_cases", "Fixture requires between one and 500 cases"
        )
    cases = tuple(_parse_case(item) for item in cases_payload)
    identifiers = tuple(case.case_id for case in cases)
    if len(identifiers) != len(set(identifiers)):
        raise EvaluationFixtureError("duplicate_case_id", "Case ids must be unique")
    return EvaluationFixture(
        schema_version=schema_version,
        suite_version=suite_version,
        fixture_hash=supplied_hash,
        thresholds=thresholds,
        cases=cases,
    )


def _scope_from_payload(payload: Mapping[str, object]) -> QueryScope:
    _strict_keys(payload, _SCOPE_KEYS, "server_scope")
    required = {"workspace_id", "state_code", "time_mode"}
    missing = sorted(required.difference(payload))
    if missing:
        raise EvaluationFixtureError(
            "invalid_server_scope", f"Server scope is missing: {', '.join(missing)}"
        )
    return QueryScope(
        workspace_id=payload["workspace_id"],
        state_code=payload["state_code"],
        time_mode=payload["time_mode"],
        bridge_number=payload.get("bridge_number"),
        data_year=payload.get("data_year"),
        start_year=payload.get("start_year"),
        end_year=payload.get("end_year"),
    )


def _assert_canonical_plan_has_no_scope_values(payload: Mapping[str, object]) -> None:
    forbidden_root = sorted(set(payload).intersection(_FORBIDDEN_PLAN_SCOPE_KEYS))
    if forbidden_root:
        raise EvaluationFixtureError(
            "scope_inside_golden_plan",
            f"Golden plan contains server-owned fields: {', '.join(forbidden_root)}",
        )
    time_scope = payload.get("time_scope")
    if not isinstance(time_scope, Mapping) or set(time_scope) != {"mode"}:
        raise EvaluationFixtureError(
            "scope_inside_golden_plan", "Golden plan time_scope may contain only mode"
        )


def _check_rejection_probe(
    case: EvalCase,
    *,
    catalog: SemanticCatalog,
    metric_registry: MetricRegistry,
    scope: QueryScope | None,
) -> None:
    if case.rejection_probe is None:
        return
    probe = case.rejection_probe
    if set(probe) != {"payload", "expected_error"}:
        raise EvaluationFixtureError(
            "invalid_rejection_probe",
            "Rejection probe requires payload and expected_error only",
        )
    payload = probe["payload"]
    expected_error = probe["expected_error"]
    if not isinstance(payload, Mapping) or not isinstance(expected_error, str):
        raise EvaluationFixtureError(
            "invalid_rejection_probe", "Rejection probe fields have invalid types"
        )
    try:
        probe_plan = BridgeQueryPlan.from_payload(payload)
        if scope is None:
            raise EvaluationFixtureError(
                "rejection_probe_accepted", "Rejection probe unexpectedly parsed"
            )
        compile_bridge_query_plan(
            probe_plan,
            catalog,
            scope,
            metric_registry=metric_registry,
        )
    except PlanError as exc:
        if exc.code != expected_error:
            raise EvaluationFixtureError(
                "wrong_rejection_code",
                f"Rejection probe returned {exc.code}, expected {expected_error}",
            ) from exc
        return
    raise EvaluationFixtureError(
        "rejection_probe_accepted", "Adversarial plan probe was unexpectedly accepted"
    )


def _check_answer(
    answer: Mapping[str, object],
    *,
    output_aliases: tuple[str, ...],
) -> None:
    if set(answer) != {"columns", "column_units", "rows", "narrative"}:
        raise EvaluationFixtureError(
            "invalid_answer_check", "Answer check has unexpected or missing fields"
        )
    columns = answer["columns"]
    units = answer["column_units"]
    rows = answer["rows"]
    narrative = answer["narrative"]
    if not isinstance(columns, (list, tuple)) or isinstance(columns, (str, bytes)):
        raise EvaluationFixtureError("invalid_answer_check", "Answer columns must be an array")
    if tuple(columns) != output_aliases:
        raise EvaluationFixtureError(
            "answer_output_mismatch",
            "Answer evidence columns must exactly match compiled plan outputs",
        )
    if not isinstance(units, Mapping) or not isinstance(rows, (list, tuple)):
        raise EvaluationFixtureError(
            "invalid_answer_check", "Answer units and rows have invalid types"
        )
    if not isinstance(narrative, Mapping):
        raise EvaluationFixtureError(
            "invalid_answer_check", "Answer narrative must be an object"
        )
    evidence = EvidenceTable.from_rows(
        columns,
        rows,
        column_units=units,
    )
    verified = verify_structured_narrative(
        parse_structured_narrative(_thaw_json(narrative)), evidence
    )
    if not verified.claims:
        raise EvaluationFixtureError(
            "missing_verified_claim", "Answer check must verify at least one claim"
        )


def _check_filter_parameterization(plan: BridgeQueryPlan, compiled_sql: object) -> None:
    sql = str(getattr(compiled_sql, "sql"))
    parameters = tuple(getattr(compiled_sql, "parameters"))
    for item in plan.filters:
        values = item.value if isinstance(item.value, tuple) else (item.value,)
        for value in values:
            if not isinstance(value, str):
                continue
            if value and value in sql:
                raise EvaluationFixtureError(
                    "filter_literal_in_compiled_sql",
                    "A user-controlled string filter was emitted as SQL text",
                )
            expected = value
            if item.operator is PlanOperator.CONTAINS:
                expected = f"%{value}%"
            elif item.operator is PlanOperator.STARTS_WITH:
                expected = f"{value}%"
            if expected not in parameters:
                raise EvaluationFixtureError(
                    "filter_parameter_missing",
                    "A user-controlled string filter was not bound as a parameter",
                )


def _evaluate_case(
    case: EvalCase,
    *,
    metric_registry: MetricRegistry,
) -> EvalCaseResult:
    stage = EvaluationStage.INTERPRETATION
    plan_hash: str | None = None
    try:
        scope = (
            _scope_from_payload(case.server_scope)
            if case.server_scope is not None
            else None
        )
        workspace_id = scope.workspace_id if scope is not None else "eval-workspace"
        catalog = build_semantic_catalog(
            database_identity="offline-evaluation",
            workspace_id=workspace_id,
            schema_generation=1,
            data_generation=1,
        )
        state_code = scope.state_code if scope is not None else 34
        bridge_number = case.bridge_number
        if bridge_number is None and scope is not None:
            bridge_number = scope.bridge_number
        interpretation = interpret_bridge_query(
            case.question,
            bridge_number=bridge_number,
            state_code=state_code,
            catalog=catalog,
            metrics=metric_registry,
        )
        if interpretation.decision is not case.expected_decision:
            raise EvaluationFixtureError(
                "decision_mismatch",
                f"Expected {case.expected_decision.value}, got {interpretation.decision.value}",
            )
        if interpretation.code != case.expected_code:
            raise EvaluationFixtureError(
                "interpretation_code_mismatch",
                f"Expected {case.expected_code}, got {interpretation.code}",
            )

        stage = EvaluationStage.PLAN
        compiled = None
        if case.expected_decision is InterpretationDecision.PROCEED:
            assert case.plan is not None and scope is not None
            _assert_canonical_plan_has_no_scope_values(case.plan)
            plan = BridgeQueryPlan.from_payload(case.plan)
            canonical = plan.canonical_payload()
            if canonical != _thaw_json(case.plan):
                raise EvaluationFixtureError(
                    "noncanonical_golden_plan",
                    "Proceed case does not contain the full canonical plan representation",
                )
            compiled = compile_bridge_query_plan(
                plan,
                catalog,
                scope,
                metric_registry=metric_registry,
            )
            plan_hash = plan.plan_hash
            if compiled.evidence.plan_hash != plan_hash:
                raise EvaluationFixtureError(
                    "plan_hash_mismatch", "Compiled evidence has the wrong plan hash"
                )
            if compiled.evidence.catalog_hash != catalog.catalog_hash:
                raise EvaluationFixtureError(
                    "catalog_hash_mismatch", "Compiled evidence has the wrong catalog hash"
                )
            if compiled.evidence.metric_registry_hash != metric_registry.registry_hash:
                raise EvaluationFixtureError(
                    "registry_hash_mismatch", "Compiled evidence has the wrong metric hash"
                )
            if compiled.evidence.scope["workspace_id"] != scope.workspace_id:
                raise EvaluationFixtureError(
                    "compiled_scope_mismatch", "Compiler did not preserve workspace scope"
                )
            if scope.workspace_id in compiled.logical_sql:
                raise EvaluationFixtureError(
                    "scope_literal_in_logical_sql",
                    "Workspace scope leaked into model-independent logical SQL",
                )
            _check_filter_parameterization(plan, compiled.compiled_sql)

        _check_rejection_probe(
            case,
            catalog=catalog,
            metric_registry=metric_registry,
            scope=scope,
        )

        stage = EvaluationStage.ANSWER
        if case.answer is not None:
            assert compiled is not None
            aliases = tuple(str(item["alias"]) for item in compiled.evidence.outputs)
            _check_answer(case.answer, output_aliases=aliases)

        return EvalCaseResult(
            case_id=case.case_id,
            category=case.category,
            passed=True,
            stage=EvaluationStage.COMPLETE,
            code="passed",
            detail="All deterministic checks passed.",
            plan_hash=plan_hash,
        )
    except Exception as exc:
        return EvalCaseResult(
            case_id=case.case_id,
            category=case.category,
            passed=False,
            stage=stage,
            code=str(getattr(exc, "code", type(exc).__name__)),
            detail=str(getattr(exc, "message", str(exc)))[:512],
            plan_hash=plan_hash,
        )


def run_evaluation(
    fixture_path: str | Path | None = None,
    *,
    metric_registry: MetricRegistry = DEFAULT_METRIC_REGISTRY,
    thresholds: EvalThresholds | None = None,
) -> EvalReport:
    """Run the deterministic release gate and return a machine-readable report.

    This function opens only the versioned fixture.  It does not call a model,
    access the network, connect to a database, or write any state.
    """

    fixture = load_evaluation_fixture(fixture_path)
    active_thresholds = thresholds or fixture.thresholds
    results = tuple(
        _evaluate_case(case, metric_registry=metric_registry) for case in fixture.cases
    )
    passed_cases = sum(result.passed for result in results)
    total_cases = len(results)
    overall_rate = passed_cases / total_cases
    grouped: dict[str, list[bool]] = {}
    for result in results:
        grouped.setdefault(result.category, []).append(result.passed)
    category_rates = {
        category: sum(values) / len(values)
        for category, values in sorted(grouped.items())
    }
    release_passed = (
        overall_rate >= active_thresholds.minimum_overall_pass_rate
        and all(
            rate >= active_thresholds.minimum_category_pass_rate
            for rate in category_rates.values()
        )
    )
    return EvalReport(
        schema_version=fixture.schema_version,
        suite_version=fixture.suite_version,
        fixture_hash=fixture.fixture_hash,
        thresholds=active_thresholds,
        total_cases=total_cases,
        passed_cases=passed_cases,
        overall_pass_rate=overall_rate,
        category_pass_rates=category_rates,
        release_passed=release_passed,
        results=results,
    )


def main() -> int:
    report = run_evaluation()
    print(json.dumps(report.to_dict(), allow_nan=False, sort_keys=True, indent=2))
    return 0 if report.release_passed else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a CLI by release tooling.
    raise SystemExit(main())


__all__ = [
    "DEFAULT_FIXTURE_PATH",
    "EVALUATION_SCHEMA_VERSION",
    "EVALUATION_SUITE_VERSION",
    "EvalCase",
    "EvalCaseResult",
    "EvalReport",
    "EvalThresholds",
    "EvaluationFixture",
    "EvaluationFixtureError",
    "EvaluationStage",
    "calculate_fixture_hash",
    "load_evaluation_fixture",
    "run_evaluation",
]
