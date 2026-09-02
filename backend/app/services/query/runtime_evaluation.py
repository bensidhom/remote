"""Executable, deterministic Bridgette runtime and sandbox release benchmark.

The existing :mod:`evaluation` suite protects interpretation and golden-plan
contracts without opening a database.  This companion gate deliberately does
the opposite: it materializes reviewed semantic relations in a brand-new
SQLite file, compiles typed plans through the production compiler, executes
them through the scoped read-only sandbox, and verifies the returned answer
evidence.  The database is disposable and this module has no option that can
target an application/customer database.

No language-model provider or network adapter is called.  Provider quality,
availability, entitlement, and latency remain a separate optional evaluation;
passing this gate must not be represented as provider certification.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .answers import (
    EvidenceTable,
    parse_structured_narrative,
    render_verified_answer,
    verify_structured_narrative,
)
from .models import SemanticCatalog, SemanticDataType
from .plans import BridgeQueryPlan, PlanParseError, compile_bridge_query_plan
from .registry import CancelOutcome, RequestRegistry, new_request_id
from .sandbox import (
    QueryByteLimitExceeded,
    QueryCancelled,
    QueryDeadlineExceeded,
    QueryVmBudgetExceeded,
    SandboxPolicy,
    execute_read_query,
)
from .semantic import build_semantic_catalog
from .service import (
    SemanticQueryRequest,
    ServerQueryIdentity,
    build_sql_catalog,
    execute_semantic_plan_query,
)
from .sql_ast import QueryScope, validate_generated_sql


RUNTIME_FIXTURE_SCHEMA_VERSION = 1
RUNTIME_RECEIPT_SCHEMA_VERSION = 1
RUNTIME_SUITE_VERSION = "bridgeguard-bridgette-runtime-v1"
MAX_RUNTIME_FIXTURE_BYTES = 2 * 1024 * 1024
DEFAULT_RUNTIME_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "query"
    / "bridgette_runtime_benchmark_v1.json"
)

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "suite_version",
        "fixture_hash",
        "catalog_identity",
        "thresholds",
        "stress_row_count",
        "relations",
        "cases",
    }
)
_IDENTITY_KEYS = frozenset(
    {
        "database_identity",
        "workspace_id",
        "schema_generation",
        "data_generation",
    }
)
_THRESHOLD_KEYS = frozenset(
    {
        "minimum_case_pass_rate",
        "minimum_security_pass_rate",
        "maximum_case_elapsed_ms",
    }
)
_CASE_KEYS = frozenset(
    {
        "id",
        "category",
        "question",
        "server_scope",
        "plan",
        "expected_columns",
        "expected_rows",
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


class RuntimeBenchmarkError(RuntimeError):
    """Typed configuration/fixture failure for the runtime release gate."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _stable_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeBenchmarkError(
            "fixture_not_canonical", "Runtime benchmark data is not canonical JSON"
        ) from exc


def calculate_runtime_fixture_hash(payload: Mapping[str, object]) -> str:
    """Return the fixture SHA-256 without its self-referential hash field."""

    if not isinstance(payload, Mapping):
        raise RuntimeBenchmarkError(
            "invalid_fixture_type", "Runtime fixture must be a JSON object"
        )
    unsigned = {str(key): value for key, value in payload.items() if key != "fixture_hash"}
    return hashlib.sha256(_stable_json(unsigned).encode("utf-8")).hexdigest()


def _reject_duplicate_keys(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeBenchmarkError(
                "duplicate_fixture_key", f"Duplicate runtime fixture key: {key}"
            )
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> object:
    raise RuntimeBenchmarkError(
        "nonfinite_fixture_number", f"Non-finite fixture number is forbidden: {value}"
    )


def _strict_keys(
    payload: Mapping[str, object], allowed: frozenset[str], context: str
) -> None:
    unknown = sorted(set(payload).difference(allowed))
    if unknown:
        raise RuntimeBenchmarkError(
            "unknown_fixture_field",
            f"{context} has unknown fields: {', '.join(unknown)}",
        )


def _required_text(payload: Mapping[str, object], key: str, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeBenchmarkError(
            "invalid_fixture_field", f"{context}.{key} must be non-blank text"
        )
    return value.strip()


def _json_value(value: object, *, context: str) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise RuntimeBenchmarkError(
                "invalid_fixture_value", f"{context} contains a non-finite number"
            )
        return value
    raise RuntimeBenchmarkError(
        "invalid_fixture_value", f"{context} must contain only JSON scalar values"
    )


@dataclass(frozen=True, slots=True)
class RuntimeThresholds:
    minimum_case_pass_rate: float
    minimum_security_pass_rate: float
    maximum_case_elapsed_ms: int

    def __post_init__(self) -> None:
        for field_name in (
            "minimum_case_pass_rate",
            "minimum_security_pass_rate",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RuntimeBenchmarkError(
                    "invalid_threshold", f"{field_name} must be numeric"
                )
            normalized = float(value)
            if not 0 <= normalized <= 1:
                raise RuntimeBenchmarkError(
                    "invalid_threshold", f"{field_name} must be between zero and one"
                )
            object.__setattr__(self, field_name, normalized)
        if (
            isinstance(self.maximum_case_elapsed_ms, bool)
            or not isinstance(self.maximum_case_elapsed_ms, int)
            or not 1 <= self.maximum_case_elapsed_ms <= 60_000
        ):
            raise RuntimeBenchmarkError(
                "invalid_threshold", "maximum_case_elapsed_ms is invalid"
            )

    def public_dict(self) -> dict[str, object]:
        return {
            "minimum_case_pass_rate": self.minimum_case_pass_rate,
            "minimum_security_pass_rate": self.minimum_security_pass_rate,
            "maximum_case_elapsed_ms": self.maximum_case_elapsed_ms,
        }


@dataclass(frozen=True, slots=True)
class RuntimeCatalogIdentity:
    database_identity: str
    workspace_id: str
    schema_generation: int
    data_generation: int


@dataclass(frozen=True, slots=True)
class RuntimeCase:
    case_id: str
    category: str
    question: str
    scope: QueryScope
    plan_payload: Mapping[str, object]
    expected_columns: tuple[str, ...]
    expected_rows: tuple[tuple[object, ...], ...]


@dataclass(frozen=True, slots=True)
class RuntimeFixture:
    fixture_hash: str
    identity: RuntimeCatalogIdentity
    thresholds: RuntimeThresholds
    stress_row_count: int
    relations: Mapping[str, tuple[Mapping[str, object], ...]]
    cases: tuple[RuntimeCase, ...]


def _scope(payload: object) -> QueryScope:
    if not isinstance(payload, Mapping):
        raise RuntimeBenchmarkError(
            "invalid_scope", "Every runtime case requires a server scope object"
        )
    _strict_keys(payload, _SCOPE_KEYS, "server_scope")
    required = {"workspace_id", "state_code", "time_mode"}
    if not required.issubset(payload):
        raise RuntimeBenchmarkError(
            "invalid_scope", "Runtime server scope is incomplete"
        )
    try:
        return QueryScope(
            workspace_id=payload["workspace_id"],
            state_code=payload["state_code"],
            time_mode=payload["time_mode"],
            bridge_number=payload.get("bridge_number"),
            data_year=payload.get("data_year"),
            start_year=payload.get("start_year"),
            end_year=payload.get("end_year"),
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeBenchmarkError(
            "invalid_scope", "Runtime case contains an invalid server scope"
        ) from exc


def load_runtime_fixture(path: str | Path | None = None) -> RuntimeFixture:
    """Load the bounded, hash-sealed runtime fixture."""

    fixture_path = Path(path) if path is not None else DEFAULT_RUNTIME_FIXTURE_PATH
    try:
        raw = fixture_path.read_bytes()
    except OSError as exc:
        raise RuntimeBenchmarkError(
            "fixture_read_failed", "Runtime benchmark fixture could not be read"
        ) from exc
    if not raw or len(raw) > MAX_RUNTIME_FIXTURE_BYTES:
        raise RuntimeBenchmarkError(
            "fixture_size_invalid", "Runtime benchmark fixture is empty or too large"
        )
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except RuntimeBenchmarkError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeBenchmarkError(
            "fixture_json_invalid", "Runtime fixture must be valid UTF-8 JSON"
        ) from exc
    if not isinstance(payload, Mapping):
        raise RuntimeBenchmarkError(
            "invalid_fixture_type", "Runtime fixture must be a JSON object"
        )
    _strict_keys(payload, _TOP_LEVEL_KEYS, "fixture")
    if set(payload) != set(_TOP_LEVEL_KEYS):
        raise RuntimeBenchmarkError(
            "missing_fixture_field", "Runtime fixture fields are incomplete"
        )
    if payload["schema_version"] != RUNTIME_FIXTURE_SCHEMA_VERSION:
        raise RuntimeBenchmarkError(
            "unsupported_schema_version", "Unsupported runtime fixture schema"
        )
    if payload["suite_version"] != RUNTIME_SUITE_VERSION:
        raise RuntimeBenchmarkError(
            "unsupported_suite_version", "Unsupported runtime benchmark suite"
        )
    fixture_hash = payload["fixture_hash"]
    if (
        not isinstance(fixture_hash, str)
        or len(fixture_hash) != 64
        or any(char not in "0123456789abcdef" for char in fixture_hash)
    ):
        raise RuntimeBenchmarkError(
            "invalid_fixture_hash", "Runtime fixture hash must be lowercase SHA-256"
        )
    if calculate_runtime_fixture_hash(payload) != fixture_hash:
        raise RuntimeBenchmarkError(
            "fixture_hash_mismatch", "Runtime fixture hash does not match its contents"
        )

    identity_payload = payload["catalog_identity"]
    if not isinstance(identity_payload, Mapping):
        raise RuntimeBenchmarkError(
            "invalid_catalog_identity", "catalog_identity must be an object"
        )
    _strict_keys(identity_payload, _IDENTITY_KEYS, "catalog_identity")
    if set(identity_payload) != set(_IDENTITY_KEYS):
        raise RuntimeBenchmarkError(
            "invalid_catalog_identity", "catalog_identity is incomplete"
        )
    generations: list[int] = []
    for key in ("schema_generation", "data_generation"):
        value = identity_payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeBenchmarkError(
                "invalid_catalog_identity", f"catalog_identity.{key} is invalid"
            )
        generations.append(value)
    identity = RuntimeCatalogIdentity(
        database_identity=_required_text(
            identity_payload, "database_identity", "catalog_identity"
        ),
        workspace_id=_required_text(identity_payload, "workspace_id", "catalog_identity"),
        schema_generation=generations[0],
        data_generation=generations[1],
    )

    thresholds_payload = payload["thresholds"]
    if not isinstance(thresholds_payload, Mapping):
        raise RuntimeBenchmarkError(
            "invalid_threshold", "Runtime thresholds must be an object"
        )
    _strict_keys(thresholds_payload, _THRESHOLD_KEYS, "thresholds")
    if set(thresholds_payload) != set(_THRESHOLD_KEYS):
        raise RuntimeBenchmarkError(
            "invalid_threshold", "Runtime thresholds are incomplete"
        )
    thresholds = RuntimeThresholds(
        minimum_case_pass_rate=thresholds_payload["minimum_case_pass_rate"],
        minimum_security_pass_rate=thresholds_payload[
            "minimum_security_pass_rate"
        ],
        maximum_case_elapsed_ms=thresholds_payload["maximum_case_elapsed_ms"],
    )
    stress_rows = payload["stress_row_count"]
    if (
        isinstance(stress_rows, bool)
        or not isinstance(stress_rows, int)
        or not 128 <= stress_rows <= 2_000
    ):
        raise RuntimeBenchmarkError(
            "invalid_stress_row_count", "stress_row_count must be between 128 and 2000"
        )

    relations_payload = payload["relations"]
    if not isinstance(relations_payload, Mapping) or not relations_payload:
        raise RuntimeBenchmarkError(
            "invalid_relations", "Runtime fixture relations must be a non-empty object"
        )
    relations: dict[str, tuple[Mapping[str, object], ...]] = {}
    for relation_name, raw_rows in relations_payload.items():
        if not isinstance(relation_name, str) or not relation_name.startswith("v_"):
            raise RuntimeBenchmarkError(
                "invalid_relation", "Runtime fixture relation name is invalid"
            )
        if not isinstance(raw_rows, list) or len(raw_rows) > 10_000:
            raise RuntimeBenchmarkError(
                "invalid_relation_rows", f"{relation_name} rows are invalid"
            )
        reviewed_rows: list[Mapping[str, object]] = []
        for row_index, raw_row in enumerate(raw_rows):
            if not isinstance(raw_row, Mapping):
                raise RuntimeBenchmarkError(
                    "invalid_relation_row", f"{relation_name}[{row_index}] is not an object"
                )
            reviewed = {
                str(key): _json_value(
                    value, context=f"{relation_name}[{row_index}].{key}"
                )
                for key, value in raw_row.items()
            }
            reviewed_rows.append(MappingProxyType(reviewed))
        relations[relation_name] = tuple(reviewed_rows)

    cases_payload = payload["cases"]
    if not isinstance(cases_payload, list) or not 1 <= len(cases_payload) <= 200:
        raise RuntimeBenchmarkError(
            "invalid_cases", "Runtime fixture requires between one and 200 cases"
        )
    cases: list[RuntimeCase] = []
    identifiers: set[str] = set()
    for raw_case in cases_payload:
        if not isinstance(raw_case, Mapping):
            raise RuntimeBenchmarkError("invalid_case", "Every runtime case must be an object")
        _strict_keys(raw_case, _CASE_KEYS, "case")
        if set(raw_case) != set(_CASE_KEYS):
            raise RuntimeBenchmarkError("invalid_case", "Runtime case is incomplete")
        case_id = _required_text(raw_case, "id", "case")
        category = _required_text(raw_case, "category", "case")
        if case_id in identifiers:
            raise RuntimeBenchmarkError("duplicate_case_id", "Runtime case ids must be unique")
        identifiers.add(case_id)
        plan = raw_case["plan"]
        columns = raw_case["expected_columns"]
        rows = raw_case["expected_rows"]
        if not isinstance(plan, Mapping):
            raise RuntimeBenchmarkError("invalid_plan", f"{case_id} plan must be an object")
        if (
            not isinstance(columns, list)
            or not columns
            or any(not isinstance(item, str) or not item for item in columns)
        ):
            raise RuntimeBenchmarkError(
                "invalid_expected_columns", f"{case_id} expected columns are invalid"
            )
        if not isinstance(rows, list) or len(rows) > 200:
            raise RuntimeBenchmarkError(
                "invalid_expected_rows", f"{case_id} expected rows are invalid"
            )
        reviewed_expected_rows: list[tuple[object, ...]] = []
        for row_index, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != len(columns):
                raise RuntimeBenchmarkError(
                    "invalid_expected_rows", f"{case_id} row width is invalid"
                )
            reviewed_expected_rows.append(
                tuple(
                    _json_value(value, context=f"{case_id}.expected_rows[{row_index}]")
                    for value in row
                )
            )
        cases.append(
            RuntimeCase(
                case_id=case_id,
                category=category,
                question=_required_text(raw_case, "question", "case"),
                scope=_scope(raw_case["server_scope"]),
                plan_payload=MappingProxyType(dict(plan)),
                expected_columns=tuple(columns),
                expected_rows=tuple(reviewed_expected_rows),
            )
        )
    return RuntimeFixture(
        fixture_hash=fixture_hash,
        identity=identity,
        thresholds=thresholds,
        stress_row_count=stress_rows,
        relations=MappingProxyType(relations),
        cases=tuple(cases),
    )


@dataclass(frozen=True, slots=True)
class RuntimeCaseReceipt:
    case_id: str
    category: str
    question_sha256: str
    passed: bool
    code: str
    row_count: int
    serialized_bytes: int
    elapsed_ms: int
    sql_elapsed_ms: int
    progress_callbacks: int
    evidence_coverage: float
    plan_hash: str | None
    result_sha256: str | None
    answer_sha256: str | None

    def public_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "question_sha256": self.question_sha256,
            "passed": self.passed,
            "code": self.code,
            "row_count": self.row_count,
            "serialized_bytes": self.serialized_bytes,
            "elapsed_ms": self.elapsed_ms,
            "sql_elapsed_ms": self.sql_elapsed_ms,
            "progress_callbacks": self.progress_callbacks,
            "evidence_coverage": self.evidence_coverage,
            "plan_hash": self.plan_hash,
            "result_sha256": self.result_sha256,
            "answer_sha256": self.answer_sha256,
        }


@dataclass(frozen=True, slots=True)
class RuntimeProbeReceipt:
    probe_id: str
    category: str
    passed: bool
    code: str
    elapsed_ms: int

    def public_dict(self) -> dict[str, object]:
        return {
            "probe_id": self.probe_id,
            "category": self.category,
            "passed": self.passed,
            "code": self.code,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass(frozen=True, slots=True)
class RuntimeBenchmarkReceipt:
    fixture_hash: str
    catalog_hash: str
    metric_registry_hash: str
    thresholds: RuntimeThresholds
    total_cases: int
    passed_cases: int
    case_pass_rate: float
    total_security_probes: int
    passed_security_probes: int
    security_pass_rate: float
    release_passed: bool
    cases: tuple[RuntimeCaseReceipt, ...]
    security_probes: tuple[RuntimeProbeReceipt, ...]

    def public_dict(self) -> dict[str, object]:
        return {
            "receipt_schema_version": RUNTIME_RECEIPT_SCHEMA_VERSION,
            "suite_version": RUNTIME_SUITE_VERSION,
            "fixture_hash": self.fixture_hash,
            "catalog_hash": self.catalog_hash,
            "metric_registry_hash": self.metric_registry_hash,
            "thresholds": self.thresholds.public_dict(),
            "total_cases": self.total_cases,
            "passed_cases": self.passed_cases,
            "case_pass_rate": self.case_pass_rate,
            "total_security_probes": self.total_security_probes,
            "passed_security_probes": self.passed_security_probes,
            "security_pass_rate": self.security_pass_rate,
            "release_passed": self.release_passed,
            "cases": [case.public_dict() for case in self.cases],
            "security_probes": [probe.public_dict() for probe in self.security_probes],
            "provider_evaluation": {
                "executed": False,
                "release_gate": False,
                "limitation": (
                    "No external model or network is used; provider generation quality, "
                    "availability, entitlement, and latency require a separate optional evaluation."
                ),
            },
        }


def _sqlite_type(data_type: SemanticDataType) -> str:
    if data_type in {SemanticDataType.INTEGER, SemanticDataType.BOOLEAN}:
        return "INTEGER"
    if data_type is SemanticDataType.NUMBER:
        return "REAL"
    return "TEXT"


def _quote(identifier: str) -> str:
    if not identifier or not identifier.replace("_", "").isalnum():
        raise RuntimeBenchmarkError("invalid_identifier", "Unsafe benchmark identifier")
    return f'"{identifier}"'


def _materialize_fixture_database(
    database_path: Path,
    *,
    fixture: RuntimeFixture,
    catalog: SemanticCatalog,
) -> None:
    if database_path.exists():
        raise RuntimeBenchmarkError(
            "database_target_exists", "Disposable benchmark database target already exists"
        )
    catalog_relations = catalog.resources_by_id
    unknown_relations = sorted(set(fixture.relations).difference(catalog_relations))
    if unknown_relations:
        raise RuntimeBenchmarkError(
            "unknown_fixture_relation",
            f"Fixture contains non-catalog relations: {', '.join(unknown_relations)}",
        )
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA foreign_keys=ON")
        for resource in catalog.resources:
            definitions = ", ".join(
                f"{_quote(column.field_id)} {_sqlite_type(column.data_type)}"
                for column in resource.columns
            )
            connection.execute(f"CREATE TABLE {_quote(resource.resource_id)} ({definitions})")
            rows = fixture.relations.get(resource.resource_id, ())
            allowed_columns = set(resource.column_ids)
            for row_index, row in enumerate(rows):
                unknown = sorted(set(row).difference(allowed_columns))
                if unknown:
                    raise RuntimeBenchmarkError(
                        "unknown_fixture_column",
                        f"{resource.resource_id}[{row_index}] has unknown columns: "
                        + ", ".join(unknown),
                    )
                columns = tuple(row)
                placeholders = ",".join("?" for _ in columns)
                connection.execute(
                    f"INSERT INTO {_quote(resource.resource_id)} "
                    f"({','.join(_quote(column) for column in columns)}) "
                    f"VALUES ({placeholders})",
                    tuple(row[column] for column in columns),
                )
            indexed = tuple(
                field
                for field in ("workspace_id", "state_code", "data_year", "bridge_number")
                if field in allowed_columns
            )
            if indexed:
                connection.execute(
                    f"CREATE INDEX {_quote('idx_bench_' + resource.resource_id[2:])} "
                    f"ON {_quote(resource.resource_id)} "
                    f"({','.join(_quote(column) for column in indexed)})"
                )

        published = catalog.resources_by_id["v_published_bridge_snapshots"]
        stress_columns = (
            "workspace_id",
            "snapshot_revision_id",
            "state_code",
            "bridge_number",
            "data_year",
            "published_at",
            "bridge_name",
        )
        if not set(stress_columns).issubset(published.column_ids):
            raise RuntimeBenchmarkError(
                "catalog_contract_changed", "Published semantic relation lost required columns"
            )
        connection.executemany(
            "INSERT INTO v_published_bridge_snapshots "
            "(workspace_id,snapshot_revision_id,state_code,bridge_number,data_year,"
            "published_at,bridge_name) VALUES (?,?,?,?,?,?,?)",
            (
                (
                    "stress-workspace",
                    f"stress-revision-{index:04d}",
                    34,
                    f"S{index:04d}",
                    2024,
                    "2024-07-01T00:00:00Z",
                    f"Stress bridge {index:04d}",
                )
                for index in range(fixture.stress_row_count)
            ),
        )
        connection.commit()
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or quick_check[0] != "ok":
            raise RuntimeBenchmarkError(
                "fixture_database_invalid", "Disposable benchmark database failed quick_check"
            )
    finally:
        connection.close()


def _column_units(compiled: object) -> dict[str, str | None]:
    outputs = getattr(getattr(compiled, "evidence"), "outputs")
    return {str(item["alias"]): item.get("unit") for item in outputs}


def _answer_round_trip(evidence: EvidenceTable) -> tuple[float, str]:
    def forbidden_provider(_request: object) -> object:
        raise AssertionError("runtime benchmark must not call a narrative provider")

    answer = render_verified_answer(evidence, generate_narrative=forbidden_provider)
    # Re-run the public structured-claim verifier with neutral prose.  Feeding
    # the deterministic Markdown renderer back into a model-output parser would
    # incorrectly interpret digits in stable cell IDs (for example ``r0.bc04``)
    # as prose claims.  The renderer and verifier are separate boundaries, so
    # exercise both without weakening either one's rules.
    cells = evidence.cells
    narrative_payload = {
        "answer_markdown": "Every reported value below is bound to cited query evidence.",
        "claims": [
            {
                "text": "Value is supported by the cited evidence cell.",
                "value": cell.value,
                "unit": (
                    cell.unit
                    if isinstance(cell.value, (int, float))
                    and not isinstance(cell.value, bool)
                    else None
                ),
                "evidence_cells": [cell_id],
                "operation": "direct",
            }
            for cell_id, cell in cells.items()
        ],
        "caveats": list(answer.caveats),
    }
    verified = verify_structured_narrative(
        parse_structured_narrative(narrative_payload), evidence
    )
    claim_cells = {
        cell_id for claim in verified.claims for cell_id in claim.evidence_cells
    }
    evidence_cells = set(cells)
    coverage = len(claim_cells) / len(evidence_cells) if evidence_cells else 1.0
    answer_hash = hashlib.sha256(
        _stable_json(
            {
                "rendered_answer": answer.answer_markdown,
                "answer_mode": answer.mode.value,
                "verified_claims": narrative_payload,
            }
        ).encode("utf-8")
    ).hexdigest()
    return coverage, answer_hash


def _run_case(
    case: RuntimeCase,
    *,
    database_path: Path,
    catalog: SemanticCatalog,
) -> tuple[RuntimeCaseReceipt, tuple[tuple[object, ...], ...]]:
    started = time.monotonic()
    plan_hash: str | None = None
    try:
        plan = BridgeQueryPlan.from_payload(case.plan_payload)
        plan_hash = plan.plan_hash
        if plan.canonical_payload() != dict(case.plan_payload):
            raise RuntimeBenchmarkError(
                "noncanonical_plan", "Runtime case plan must be the full canonical plan"
            )
        compiled = compile_bridge_query_plan(plan, catalog, case.scope)
        if compiled.evidence.catalog_hash != catalog.catalog_hash:
            raise RuntimeBenchmarkError(
                "catalog_evidence_mismatch", "Compiled plan carries stale catalog evidence"
            )
        if compiled.evidence.scope["workspace_id"] != case.scope.workspace_id:
            raise RuntimeBenchmarkError(
                "scope_evidence_mismatch", "Compiled plan lost server workspace scope"
            )
        result = execute_read_query(
            database_path=database_path,
            sql=compiled.compiled_sql.sql,
            parameters=compiled.compiled_sql.parameters,
            request_id=new_request_id(),
            owner_id=f"benchmark-{case.category}",
            workspace_id=case.scope.workspace_id,
            allowed_relations=catalog.sandbox_relation_allowlist,
            allowed_functions=(
                function.sql_name.casefold() for function in catalog.functions
            ),
            policy=SandboxPolicy(
                deadline_seconds=2,
                max_rows=200,
                max_serialized_bytes=1024 * 1024,
            ),
            registry=RequestRegistry(),
        )
        if result.columns != case.expected_columns:
            raise RuntimeBenchmarkError(
                "column_mismatch", "Runtime result columns differ from reviewed fixture"
            )
        if result.rows != case.expected_rows:
            raise RuntimeBenchmarkError(
                "result_mismatch", "Runtime result rows differ from reviewed fixture"
            )
        if "PRIVATE" in _stable_json(result.rows) or "Must never leak" in _stable_json(
            result.rows
        ):
            raise RuntimeBenchmarkError(
                "workspace_leak", "A scoped runtime result exposed another workspace"
            )
        evidence = EvidenceTable.from_query_evidence(
            result, column_units=_column_units(compiled)
        )
        coverage, answer_hash = _answer_round_trip(evidence)
        if coverage != 1.0:
            raise RuntimeBenchmarkError(
                "incomplete_answer_evidence", "Deterministic answer did not cite every result cell"
            )
        elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
        if elapsed_ms > 60_000:
            raise RuntimeBenchmarkError(
                "case_runtime_unbounded", "Runtime case exceeded the hard benchmark bound"
            )
        result_hash = hashlib.sha256(
            _stable_json({"columns": result.columns, "rows": result.rows}).encode("utf-8")
        ).hexdigest()
        return (
            RuntimeCaseReceipt(
                case_id=case.case_id,
                category=case.category,
                question_sha256=hashlib.sha256(
                    case.question.encode("utf-8")
                ).hexdigest(),
                passed=True,
                code="passed",
                row_count=result.row_count,
                serialized_bytes=result.serialized_bytes,
                elapsed_ms=elapsed_ms,
                sql_elapsed_ms=result.elapsed_ms,
                progress_callbacks=result.progress_callbacks,
                evidence_coverage=coverage,
                plan_hash=plan_hash,
                result_sha256=result_hash,
                answer_sha256=answer_hash,
            ),
            result.rows,
        )
    except Exception as exc:
        return (
            RuntimeCaseReceipt(
                case_id=case.case_id,
                category=case.category,
                question_sha256=hashlib.sha256(
                    case.question.encode("utf-8")
                ).hexdigest(),
                passed=False,
                code=str(getattr(exc, "code", type(exc).__name__)),
                row_count=0,
                serialized_bytes=0,
                elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
                sql_elapsed_ms=0,
                progress_callbacks=0,
                evidence_coverage=0.0,
                plan_hash=plan_hash,
                result_sha256=None,
                answer_sha256=None,
            ),
            (),
        )


def _probe(
    probe_id: str,
    category: str,
    expected_code: str,
    callback: Any,
) -> RuntimeProbeReceipt:
    started = time.monotonic()
    try:
        observed = str(callback())
        passed = observed == expected_code
        code = observed
    except Exception as exc:
        code = str(getattr(exc, "code", type(exc).__name__))
        passed = code == expected_code
    return RuntimeProbeReceipt(
        probe_id=probe_id,
        category=category,
        passed=passed,
        code=code,
        elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
    )


def _stress_plan(catalog: SemanticCatalog) -> tuple[object, QueryScope]:
    scope = QueryScope("stress-workspace", 34, "exact", data_year=2024)
    stress_catalog = build_semantic_catalog(
        database_identity=catalog.identity.database_identity,
        workspace_id="stress-workspace",
        schema_generation=catalog.identity.schema_generation,
        data_generation=catalog.identity.data_generation,
    )
    plan = BridgeQueryPlan.from_payload(
        {
            "dimensions": [],
            "filters": [],
            "intent": "list",
            "limit": 200,
            "metric_ids": [],
            "order": [{"by": "bridge_number", "direction": "asc"}],
            "resource_id": "v_published_bridge_snapshots",
            "selected_fields": ["bridge_number", "bridge_name"],
            "time_scope": {"mode": "exact"},
        }
    )
    return compile_bridge_query_plan(plan, stress_catalog, scope), scope


def _cross_join_sql() -> str:
    return (
        "SELECT SUM(a.state_code * b.state_code * c.state_code) "
        "FROM v_published_bridge_snapshots a "
        "CROSS JOIN v_published_bridge_snapshots b "
        "CROSS JOIN v_published_bridge_snapshots c "
        "WHERE a.workspace_id=? AND b.workspace_id=? AND c.workspace_id=?"
    )


def _run_security_probes(
    *,
    database_path: Path,
    fixture: RuntimeFixture,
    catalog: SemanticCatalog,
    case_rows: Mapping[str, tuple[tuple[object, ...], ...]],
) -> tuple[RuntimeProbeReceipt, ...]:
    probes: list[RuntimeProbeReceipt] = []
    sql_catalog = build_sql_catalog(catalog)

    def hostile_plan() -> str:
        try:
            BridgeQueryPlan.from_payload(
                {
                    "intent": "list",
                    "resource_id": "v_published_bridge_snapshots",
                    "selected_fields": ["bridge_number"],
                    "time_scope": {"mode": "exact"},
                    "sql": "DROP TABLE v_published_bridge_snapshots",
                }
            )
        except PlanParseError as exc:
            return exc.code
        return "accepted"

    probes.append(
        _probe("hostile.plan_sql_field", "scope_security", "unknown_plan_field", hostile_plan)
    )
    probes.append(
        _probe(
            "hostile.drop_sql",
            "scope_security",
            "forbidden_statement",
            lambda: validate_generated_sql(
                "DROP TABLE v_published_bridge_snapshots", sql_catalog
            ),
        )
    )
    excessive_join_sql = (
        "SELECT a.bridge_number FROM v_published_bridge_snapshots a "
        "JOIN v_bridge_condition b ON a.workspace_id=b.workspace_id "
        "JOIN v_bridge_features c ON a.workspace_id=c.workspace_id "
        "JOIN v_bridge_routes d ON a.workspace_id=d.workspace_id "
        "JOIN v_bridge_elements e ON a.workspace_id=e.workspace_id "
        "JOIN v_bridge_works f ON a.workspace_id=f.workspace_id"
    )
    probes.append(
        _probe(
            "hostile.excessive_joins",
            "scope_security",
            "join_limit_exceeded",
            lambda: validate_generated_sql(excessive_join_sql, sql_catalog),
        )
    )
    probes.append(
        _probe(
            "hostile.recursive_cte",
            "scope_security",
            "recursive_cte",
            lambda: validate_generated_sql(
                "WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x) "
                "SELECT n FROM x",
                sql_catalog,
            ),
        )
    )

    compiled_stress, stress_scope = _stress_plan(catalog)

    def row_cap() -> str:
        result = execute_read_query(
            database_path=database_path,
            sql=compiled_stress.compiled_sql.sql,
            parameters=compiled_stress.compiled_sql.parameters,
            request_id=new_request_id(),
            owner_id="row-cap-owner",
            workspace_id=stress_scope.workspace_id,
            allowed_relations=catalog.sandbox_relation_allowlist,
            allowed_functions=(function.sql_name for function in catalog.functions),
            policy=SandboxPolicy(max_rows=7, fetch_size=4),
            registry=RequestRegistry(),
        )
        return "row_cap_enforced" if result.truncated and result.row_count == 7 else "row_cap_failed"

    probes.append(_probe("sandbox.row_cap", "resource_caps", "row_cap_enforced", row_cap))

    def byte_cap() -> str:
        case = next(item for item in fixture.cases if item.case_id == "data.prompt_injection_is_text")
        compiled = compile_bridge_query_plan(
            BridgeQueryPlan.from_payload(case.plan_payload), catalog, case.scope
        )
        try:
            execute_read_query(
                database_path=database_path,
                sql=compiled.compiled_sql.sql,
                parameters=compiled.compiled_sql.parameters,
                request_id=new_request_id(),
                owner_id="byte-cap-owner",
                workspace_id=case.scope.workspace_id,
                allowed_relations=catalog.sandbox_relation_allowlist,
                allowed_functions=(function.sql_name for function in catalog.functions),
                policy=SandboxPolicy(max_cell_bytes=1024, max_serialized_bytes=64),
                registry=RequestRegistry(),
            )
        except QueryByteLimitExceeded:
            return "query_byte_limit_exceeded"
        return "accepted"

    probes.append(
        _probe(
            "sandbox.byte_cap", "resource_caps", "query_byte_limit_exceeded", byte_cap
        )
    )

    def vm_cap() -> str:
        try:
            execute_read_query(
                database_path=database_path,
                sql=_cross_join_sql(),
                parameters=("stress-workspace",) * 3,
                request_id=new_request_id(),
                owner_id="vm-cap-owner",
                workspace_id="stress-workspace",
                allowed_relations=catalog.sandbox_relation_allowlist,
                allowed_functions=(function.sql_name for function in catalog.functions),
                policy=SandboxPolicy(
                    deadline_seconds=2,
                    progress_opcode_interval=100,
                    max_progress_callbacks=1,
                ),
                registry=RequestRegistry(),
            )
        except QueryVmBudgetExceeded:
            return "query_vm_budget_exceeded"
        return "accepted"

    probes.append(
        _probe(
            "sandbox.vm_cap", "resource_caps", "query_vm_budget_exceeded", vm_cap
        )
    )

    def deadline_cap() -> str:
        try:
            execute_read_query(
                database_path=database_path,
                sql=_cross_join_sql(),
                parameters=("stress-workspace",) * 3,
                request_id=new_request_id(),
                owner_id="deadline-owner",
                workspace_id="stress-workspace",
                allowed_relations=catalog.sandbox_relation_allowlist,
                allowed_functions=(function.sql_name for function in catalog.functions),
                policy=SandboxPolicy(
                    deadline_seconds=0.001,
                    progress_opcode_interval=100,
                    max_progress_callbacks=100_000,
                ),
                registry=RequestRegistry(),
            )
        except QueryDeadlineExceeded:
            return "query_deadline_exceeded"
        return "accepted"

    probes.append(
        _probe(
            "sandbox.deadline_cap",
            "resource_caps",
            "query_deadline_exceeded",
            deadline_cap,
        )
    )

    def cancellation() -> str:
        registry = RequestRegistry()
        request_id = new_request_id()
        captured: dict[str, object] = {}

        def run() -> None:
            try:
                execute_read_query(
                    database_path=database_path,
                    sql=_cross_join_sql(),
                    parameters=("stress-workspace",) * 3,
                    request_id=request_id,
                    owner_id="cancellation-owner",
                    workspace_id="stress-workspace",
                    allowed_relations=catalog.sandbox_relation_allowlist,
                    allowed_functions=(function.sql_name for function in catalog.functions),
                    policy=SandboxPolicy(
                        deadline_seconds=2,
                        progress_opcode_interval=100,
                        max_progress_callbacks=100_000,
                    ),
                    registry=registry,
                )
            except BaseException as exc:  # captured from the benchmark thread
                captured["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        wait_until = time.monotonic() + 1
        while not registry.is_active(request_id) and time.monotonic() < wait_until:
            time.sleep(0.001)
        if not registry.is_active(request_id):
            return "request_not_observed"
        if (
            registry.cancel(
                request_id=request_id,
                owner_id="other-owner",
                workspace_id="stress-workspace",
            )
            is not CancelOutcome.NOT_FOUND
        ):
            return "cross_owner_cancelled"
        if (
            registry.cancel(
                request_id=request_id,
                owner_id="cancellation-owner",
                workspace_id="other-workspace",
            )
            is not CancelOutcome.NOT_FOUND
        ):
            return "cross_workspace_cancelled"
        if (
            registry.cancel(
                request_id=request_id,
                owner_id="cancellation-owner",
                workspace_id="stress-workspace",
            )
            is not CancelOutcome.CANCELLED
        ):
            return "owner_cancel_failed"
        thread.join(timeout=2)
        if thread.is_alive():
            registry.clear()
            return "cancellation_timeout"
        if not isinstance(captured.get("error"), QueryCancelled):
            return "wrong_cancellation_result"
        return "request_owned_cancellation"

    probes.append(
        _probe(
            "sandbox.request_owned_cancellation",
            "isolation",
            "request_owned_cancellation",
            cancellation,
        )
    )

    def concurrent_users() -> str:
        plan = BridgeQueryPlan.from_payload(
            {
                "dimensions": [],
                "filters": [],
                "intent": "count",
                "limit": 1,
                "metric_ids": ["inventory.bridge_count"],
                "order": [],
                "resource_id": "v_published_bridge_snapshots",
                "selected_fields": [],
                "time_scope": {"mode": "exact"},
            }
        )
        registry = RequestRegistry()

        def run(index: int) -> tuple[str, tuple[tuple[object, ...], ...]]:
            workspace = "eval-workspace" if index % 2 == 0 else "other-workspace"
            scoped_catalog = build_semantic_catalog(
                database_identity=fixture.identity.database_identity,
                workspace_id=workspace,
                schema_generation=fixture.identity.schema_generation,
                data_generation=fixture.identity.data_generation,
            )
            scope = QueryScope(workspace, 34, "exact", data_year=2024)
            compiled = compile_bridge_query_plan(plan, scoped_catalog, scope)
            result = execute_read_query(
                database_path=database_path,
                sql=compiled.compiled_sql.sql,
                parameters=compiled.compiled_sql.parameters,
                request_id=new_request_id(),
                owner_id=f"concurrent-owner-{index}",
                workspace_id=workspace,
                allowed_relations=scoped_catalog.sandbox_relation_allowlist,
                allowed_functions=(
                    function.sql_name for function in scoped_catalog.functions
                ),
                registry=registry,
            )
            return workspace, result.rows

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = tuple(executor.map(run, range(8)))
        if registry.active_count:
            return "registry_leak"
        for workspace, rows in results:
            expected = ((6,),) if workspace == "eval-workspace" else ((1,),)
            if rows != expected:
                return "concurrent_scope_leak"
        return "concurrent_scope_isolated"

    probes.append(
        _probe(
            "sandbox.concurrent_users",
            "isolation",
            "concurrent_scope_isolated",
            concurrent_users,
        )
    )

    def stale_generation() -> str:
        request = SemanticQueryRequest(
            request_id=new_request_id(),
            identity=ServerQueryIdentity(
                actor_id="stale-owner",
                role="viewer",
                workspace_id=fixture.identity.workspace_id,
            ),
            scope=QueryScope(
                fixture.identity.workspace_id, 34, "exact", data_year=2024
            ),
            question="Count bridges.",
            database_identity=fixture.identity.database_identity,
            schema_generation=fixture.identity.schema_generation,
            data_generation=fixture.identity.data_generation + 1,
        )
        try:
            execute_semantic_plan_query(
                database_path=database_path,
                request=request,
                semantic_catalog=catalog,
                generate_plan=lambda _request: (_ for _ in ()).throw(
                    AssertionError("stale catalog must fail before generation")
                ),
            )
        except ValueError as exc:
            if "data generation does not match" in str(exc):
                return "stale_data_generation_rejected"
            return "wrong_stale_error"
        return "accepted"

    probes.append(
        _probe(
            "evidence.stale_generation",
            "revision_identity",
            "stale_data_generation_rejected",
            stale_generation,
        )
    )

    def revision_identity() -> str:
        exact = case_rows.get("revision.exact_snapshot", ())
        current = case_rows.get("current.per_bridge_latest", ())
        if not exact or exact[0][2] != "rev-a-2024":
            return "exact_revision_missing"
        if not current or current[0][2] != "rev-a-2025":
            return "current_revision_missing"
        newer_catalog = build_semantic_catalog(
            database_identity=fixture.identity.database_identity,
            workspace_id=fixture.identity.workspace_id,
            schema_generation=fixture.identity.schema_generation,
            data_generation=fixture.identity.data_generation + 1,
        )
        if newer_catalog.catalog_hash == catalog.catalog_hash:
            return "generation_not_bound_to_catalog"
        return "revision_and_generation_bound"

    probes.append(
        _probe(
            "evidence.revision_identity",
            "revision_identity",
            "revision_and_generation_bound",
            revision_identity,
        )
    )
    return tuple(probes)


def run_runtime_benchmark(
    fixture_path: str | Path | None = None,
    *,
    work_directory: str | Path | None = None,
) -> RuntimeBenchmarkReceipt:
    """Run the disposable DB release gate and return a versioned receipt.

    ``work_directory`` selects only where a newly-created temporary directory
    is placed.  There is intentionally no ``database_path`` argument, so this
    API cannot be pointed at an application database.
    """

    fixture = load_runtime_fixture(fixture_path)
    base_path = Path(tempfile.gettempdir()).resolve()
    if work_directory is not None:
        candidate = Path(work_directory).resolve()
        if not candidate.is_dir():
            raise RuntimeBenchmarkError(
                "invalid_work_directory", "Benchmark work directory must already exist"
            )
        base_path = candidate
    catalog = build_semantic_catalog(
        database_identity=fixture.identity.database_identity,
        workspace_id=fixture.identity.workspace_id,
        schema_generation=fixture.identity.schema_generation,
        data_generation=fixture.identity.data_generation,
    )
    # ``TemporaryDirectory`` applies a restrictive ACL on some Windows hosts
    # that can make the directory inaccessible to the creating process.  An
    # ordinary, unpredictable child directory retains the same safe ownership
    # while remaining usable by SQLite.  Cleanup validates the exact parent
    # and name prefix before removing only this disposable child.
    temporary = base_path / f"bridgeguard-bridgette-runtime-{uuid4().hex}"
    temporary.mkdir(parents=False, exist_ok=False)
    try:
        database_path = temporary / "benchmark.sqlite3"
        _materialize_fixture_database(database_path, fixture=fixture, catalog=catalog)
        case_results: list[RuntimeCaseReceipt] = []
        case_rows: dict[str, tuple[tuple[object, ...], ...]] = {}
        for case in fixture.cases:
            receipt, rows = _run_case(case, database_path=database_path, catalog=catalog)
            if (
                receipt.passed
                and receipt.elapsed_ms > fixture.thresholds.maximum_case_elapsed_ms
            ):
                receipt = RuntimeCaseReceipt(
                    case_id=receipt.case_id,
                    category=receipt.category,
                    question_sha256=receipt.question_sha256,
                    passed=False,
                    code="case_latency_threshold_exceeded",
                    row_count=receipt.row_count,
                    serialized_bytes=receipt.serialized_bytes,
                    elapsed_ms=receipt.elapsed_ms,
                    sql_elapsed_ms=receipt.sql_elapsed_ms,
                    progress_callbacks=receipt.progress_callbacks,
                    evidence_coverage=receipt.evidence_coverage,
                    plan_hash=receipt.plan_hash,
                    result_sha256=receipt.result_sha256,
                    answer_sha256=receipt.answer_sha256,
                )
            case_results.append(receipt)
            case_rows[case.case_id] = rows
        probes = _run_security_probes(
            database_path=database_path,
            fixture=fixture,
            catalog=catalog,
            case_rows=case_rows,
        )
    finally:
        resolved = temporary.resolve()
        if (
            resolved.parent != base_path
            or not resolved.name.startswith("bridgeguard-bridgette-runtime-")
        ):
            raise RuntimeBenchmarkError(
                "unsafe_cleanup_target", "Refusing to remove an unexpected benchmark path"
            )
        shutil.rmtree(resolved)

    passed_cases = sum(case.passed for case in case_results)
    total_cases = len(case_results)
    case_rate = passed_cases / total_cases
    passed_probes = sum(probe.passed for probe in probes)
    total_probes = len(probes)
    security_rate = passed_probes / total_probes
    release_passed = (
        case_rate >= fixture.thresholds.minimum_case_pass_rate
        and security_rate >= fixture.thresholds.minimum_security_pass_rate
    )
    from .metrics import DEFAULT_METRIC_REGISTRY

    return RuntimeBenchmarkReceipt(
        fixture_hash=fixture.fixture_hash,
        catalog_hash=catalog.catalog_hash,
        metric_registry_hash=DEFAULT_METRIC_REGISTRY.registry_hash,
        thresholds=fixture.thresholds,
        total_cases=total_cases,
        passed_cases=passed_cases,
        case_pass_rate=case_rate,
        total_security_probes=total_probes,
        passed_security_probes=passed_probes,
        security_pass_rate=security_rate,
        release_passed=release_passed,
        cases=tuple(case_results),
        security_probes=probes,
    )


__all__ = [
    "DEFAULT_RUNTIME_FIXTURE_PATH",
    "RUNTIME_FIXTURE_SCHEMA_VERSION",
    "RUNTIME_RECEIPT_SCHEMA_VERSION",
    "RUNTIME_SUITE_VERSION",
    "RuntimeBenchmarkError",
    "RuntimeBenchmarkReceipt",
    "RuntimeCaseReceipt",
    "RuntimeFixture",
    "RuntimeProbeReceipt",
    "RuntimeThresholds",
    "calculate_runtime_fixture_hash",
    "load_runtime_fixture",
    "run_runtime_benchmark",
]
