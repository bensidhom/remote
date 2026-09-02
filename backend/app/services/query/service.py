"""Fail-closed orchestration for Bridgette's semantic SQL questions.

The service is the only supported path from an LLM-produced statement to the
bounded SQLite executor.  It deliberately keeps four concerns separate:

* a reviewed semantic catalog and rich, provenance-bearing SNBI dictionary are
  supplied to the SQL generator;
* generated SQL is parsed against logical view/field identifiers only;
* trusted workspace, state, bridge, and time scope is injected mechanically;
* every accepted generation attempt receives one terminal, privacy-minimized
  audit event before a result or typed failure is returned.

There is no fallback to the historical ``execute_readonly_sql`` helper.  If a
statement cannot pass this boundary, Bridgette abstains.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from types import MappingProxyType
from typing import Protocol

from .audit import (
    AcceptQueryAuditRequest,
    FinishQueryAuditRequest,
    QueryAuditError,
    QueryAuditOutcome,
    QueryAuditScope,
    ValidatedAstAudit,
    accept_query_attempt,
    finish_query_attempt,
)
from .metrics import DEFAULT_METRIC_REGISTRY
from .models import SemanticCatalog
from .plans import (
    BridgeQueryPlan,
    PlanError,
    PlanIntent,
    PlanLimits,
    PlanParseError,
    PlanEvidence,
    compile_bridge_query_plan,
)
from .reference_data import NEW_JERSEY_COUNTY_PROMPT_ROWS
from .registry import (
    DEFAULT_REQUEST_REGISTRY,
    CancelOutcome,
    RequestRegistry,
    validate_request_id,
)
from .sandbox import (
    DEFAULT_SANDBOX_POLICY,
    QueryCancelled,
    QueryDeadlineExceeded,
    QueryDenied,
    QueryOutcome,
    QueryVmBudgetExceeded,
    SandboxError,
    SandboxPolicy,
    execute_read_query,
)
from .semantic import build_semantic_catalog
from .snbi_dictionary import DEFAULT_SNBI_DICTIONARY, SnbiDictionary
from .snbi_official_information import (
    DEFAULT_SNBI_INFORMATION,
    SnbiInformationalCatalog,
)
from .sql_ast import (
    CatalogTable,
    CompiledSql,
    QueryScope,
    SqlCatalog,
    SqlCompilationError,
    SqlGuardError,
    SqlLimits,
    TableTimeSemantics,
    TimeMode,
    ValidatedSql,
    compile_scoped_sql,
    validate_generated_sql,
)


PROMPT_VERSION = "bridgette-semantic-query-v1"
PLAN_PROMPT_VERSION = "bridge-query-plan-prompt-v2"
MAX_GENERATION_ATTEMPTS = 2
MAX_QUERY_CONTEXT_BYTES = 256 * 1024
# Keep planning prompts bounded while retaining the complete 154-field SNBI
# index plus reviewed value domains (including BSP09 deck-material codes).
# This remains well below the configured model context window and contains no
# bridge-row corpus—only schema, definitions, mappings, units, and code lists.
MAX_PLAN_CONTEXT_BYTES = 192 * 1024
MAX_DETAILED_DICTIONARY_FIELDS = 18
MAX_PLAN_RESPONSE_BYTES = 64 * 1024
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,254}$")
_REASON_RE = re.compile(r"[^a-z0-9_]+")
_WORD_RE = re.compile(r"[a-z]+|[a-z]+\d+|\d+")
_STOP_WORDS = frozenset(
    {
        "about",
        "all",
        "and",
        "are",
        "bridge",
        "bridges",
        "data",
        "field",
        "for",
        "from",
        "how",
        "have",
        "item",
        "many",
        "published",
        "show",
        "snapshot",
        "snapshots",
        "structure",
        "structures",
        "that",
        "the",
        "this",
        "what",
        "which",
        "with",
        "year",
    }
)
_QUESTION_SYNONYMS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "aadt": ("annual", "average", "daily", "traffic"),
        "adt": ("annual", "average", "daily", "traffic"),
        "nhs": ("national", "highway", "system", "designation"),
        "nbi": ("legacy", "source"),
        "lat": ("latitude",),
        "lon": ("longitude",),
        "lng": ("longitude",),
        "super": ("superstructure",),
        "sub": ("substructure",),
    }
)


def _stable_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _token(value: object, label: str) -> str:
    text = str(value).strip()
    if not text or not _TOKEN_RE.fullmatch(text):
        raise ValueError(f"{label} is invalid")
    return text


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class ServerQueryIdentity:
    """Authentication-derived identity; never populate it from model output."""

    actor_id: str
    role: str
    workspace_id: str
    session_id: str | None = None
    surface: str = "bridgette"

    def __post_init__(self) -> None:
        object.__setattr__(self, "actor_id", _token(self.actor_id, "actor_id"))
        object.__setattr__(self, "role", _token(self.role, "role"))
        object.__setattr__(
            self, "workspace_id", _token(self.workspace_id, "workspace_id")
        )
        object.__setattr__(self, "surface", _token(self.surface, "surface"))
        if self.session_id is not None:
            object.__setattr__(
                self, "session_id", _token(self.session_id, "session_id")
            )


@dataclass(frozen=True, slots=True)
class SemanticQueryRequest:
    """One server-authorized Bridgette query."""

    request_id: str
    identity: ServerQueryIdentity
    scope: QueryScope
    question: str
    database_identity: str
    schema_generation: int
    data_generation: int
    model_id: str | None = None
    prompt_version: str = PROMPT_VERSION
    max_rows: int = 100

    def __post_init__(self) -> None:
        validate_request_id(self.request_id)
        if self.scope.workspace_id != self.identity.workspace_id:
            raise ValueError("query scope must match the authenticated workspace")
        question = str(self.question).strip()
        if not question:
            raise ValueError("question is required")
        object.__setattr__(self, "question", question)
        database_identity = str(self.database_identity).strip()
        if not database_identity:
            raise ValueError("database_identity is required")
        object.__setattr__(self, "database_identity", database_identity)
        for field_name in ("schema_generation", "data_generation"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if isinstance(self.max_rows, bool) or not isinstance(self.max_rows, int):
            raise ValueError("max_rows must be a positive integer")
        if self.max_rows < 1:
            raise ValueError("max_rows must be a positive integer")
        object.__setattr__(
            self, "prompt_version", _token(self.prompt_version, "prompt_version")
        )
        if self.model_id is not None:
            object.__setattr__(self, "model_id", _token(self.model_id, "model_id"))


@dataclass(frozen=True, slots=True)
class GenerationFeedback:
    """Safe, value-free feedback supplied for the sole retry."""

    attempt: int
    code: str
    instruction: str


def _plan_repair_instruction(code: str) -> str:
    """Return value-free, validation-specific guidance for the sole plan retry."""

    guidance = {
        "client_scope_override": (
            "Remove every workspace, state, bridge, revision, and inventory-year "
            "filter; the server already injected that scope."
        ),
        "invalid_field_reference": (
            "Replace every field reference with an exact field_id or "
            "resource_id.field_id from semantic_catalog columns."
        ),
        "unknown_resource": (
            "Choose resource_id exactly from semantic_catalog.resources[].id."
        ),
        "unknown_metric": (
            "Choose metric_ids only from active reviewed_metric_registry rows."
        ),
        "metric_resource_mismatch": (
            "Choose a metric whose source_resource exactly equals resource_id."
        ),
        "invalid_order": (
            "Set order.by to an exact selected output field reference or selected "
            "metric id, or return an empty order array."
        ),
        "unknown_order_output": (
            "Order only by a field or metric already present in the plan output."
        ),
        "aggregate_grain_error": (
            "For an aggregate plan, make selected_fields exactly equal dimensions "
            "and avoid joins that multiply the base-resource grain."
        ),
        "intent_shape_mismatch": (
            "For list or lookup use selected_fields with empty metric_ids and "
            "dimensions; for aggregate intents use metrics and approved dimensions."
        ),
        "metric_required": (
            "Select at least one active metric for count, aggregate, trend, or compare."
        ),
        "selected_field_required": (
            "Select at least one reviewed field for a list or lookup plan."
        ),
        "intent_time_mismatch": (
            "Use trend intent for aggregate trend time scope; otherwise use the "
            "server-selected non-trend intent and time mode."
        ),
        "latest_county_requires_current": (
            "Use v_current_county_bridge_inventory for a latest county summary; "
            "keep v_county_bridge_inventory for exact-year or trend requests."
        ),
        "invalid_identifier": (
            "Use exact lowercase identifiers from the reviewed context, without "
            "labels, spaces, aliases, or SNBI tag punctuation."
        ),
        "invalid_plan_json": (
            "Return exactly one JSON object matching the response schema with no prose."
        ),
    }
    specific = guidance.get(
        code,
        "Use only active metrics, reviewed resources and fields, and the "
        "server-selected time mode.",
    )
    return f"Return one smaller strict BridgeQueryPlan. {specific}"


@dataclass(frozen=True, slots=True)
class SqlGenerationRequest:
    """Deterministic input to a model adapter that returns exactly one SELECT."""

    request_id: str
    attempt: int
    question: str
    scope: QueryScope
    context_json: str
    context_sha256: str
    feedback: GenerationFeedback | None = None


class SqlGenerator(Protocol):
    def __call__(self, request: SqlGenerationRequest) -> str: ...


@dataclass(frozen=True, slots=True)
class PlanGenerationRequest:
    """Deterministic input to the model adapter that proposes a typed plan."""

    request_id: str
    attempt: int
    question: str
    scope: QueryScope
    context_json: str
    context_sha256: str
    feedback: GenerationFeedback | None = None


class PlanGenerator(Protocol):
    def __call__(
        self, request: PlanGenerationRequest
    ) -> str | Mapping[str, object] | BridgeQueryPlan: ...


@dataclass(frozen=True, slots=True)
class QueryAstEvidence:
    """Value-free structural evidence returned with a successful query."""

    relations: tuple[str, ...]
    referenced_fields: tuple[str, ...]
    output_fields: tuple[str, ...]
    cte_count: int
    join_count: int
    subquery_count: int
    expression_depth: int
    literal_count: int

    @classmethod
    def from_validated(cls, validated: ValidatedSql) -> "QueryAstEvidence":
        return cls(
            relations=tuple(scan.table for scan in validated.tables),
            referenced_fields=tuple(column.column for column in validated.columns),
            output_fields=validated.output_columns,
            cte_count=validated.cte_count,
            join_count=validated.join_count,
            subquery_count=validated.subquery_count,
            expression_depth=validated.expression_depth,
            literal_count=validated.literal_count,
        )


@dataclass(frozen=True, slots=True)
class SemanticQueryEvidence:
    """Bounded query data plus reproducible, value-free planning evidence."""

    request_id: str
    attempt: int
    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]
    row_count: int
    truncated: bool
    serialized_bytes: int
    elapsed_ms: int
    progress_steps: int
    scope: QueryScope
    ast: QueryAstEvidence
    semantic_catalog_sha256: str
    snbi_dictionary_sha256: str
    prompt_context_sha256: str
    compiled_sql_sha256: str
    snbi_information_sha256: str | None = None
    plan_evidence: PlanEvidence | None = None


class SemanticQueryError(RuntimeError):
    """Typed, client-safe service failure."""

    code = "semantic_query_failed"

    def __init__(
        self,
        message: str,
        *,
        request_id: str,
        attempts: int,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.request_id = request_id
        self.attempts = attempts
        self.details = MappingProxyType(dict(details or {}))


class SemanticQueryAbstained(SemanticQueryError):
    code = "semantic_query_abstained"


class SemanticQueryCancelled(SemanticQueryError):
    code = "semantic_query_cancelled"


class SemanticQueryTimedOut(SemanticQueryError):
    code = "semantic_query_timed_out"


class SemanticQueryExecutionFailed(SemanticQueryError):
    code = "semantic_query_execution_failed"


class SemanticQueryGenerationFailed(SemanticQueryError):
    code = "semantic_query_generation_failed"


class SemanticQueryAuditFailed(SemanticQueryError):
    code = "semantic_query_audit_failed"


def build_sql_catalog(catalog: SemanticCatalog) -> SqlCatalog:
    """Translate reviewed logical resources into the parser's exact allowlist."""

    tables: dict[str, CatalogTable] = {}
    for resource in catalog.resources:
        columns = frozenset(resource.column_ids)
        tables[resource.resource_id] = CatalogTable(
            name=resource.resource_id,
            columns=columns,
            bridge_column="bridge_number" if "bridge_number" in columns else None,
            time_semantics=(
                TableTimeSemantics.CURRENT
                if resource.resource_id.startswith("v_current_")
                else TableTimeSemantics.HISTORY
            ),
        )
    return SqlCatalog(
        tables=tables,
        functions=frozenset(
            function.sql_name.casefold() for function in catalog.functions
        ),
    )


def _logical_snbi_links(
    catalog: SemanticCatalog,
    dictionary: SnbiDictionary,
) -> tuple[dict[str, object], ...]:
    """Map SNBI tags to queryable logical fields without exposing base tables."""

    by_projection: dict[str, list[dict[str, str]]] = {}
    for resource in catalog.resources:
        for column in resource.columns:
            if column.physical_column is None:
                continue
            by_projection.setdefault(column.physical_column, []).append(
                {
                    "resource_id": resource.resource_id,
                    "field_id": column.field_id,
                }
            )

    links: list[dict[str, object]] = []
    for field in dictionary.fields:
        targets = tuple(
            sorted(
                by_projection.get(field.physical_column or "", ()),
                key=lambda item: (item["resource_id"], item["field_id"]),
            )
        )
        links.append(
            {
                "snbi_tag": field.tag,
                "queryable_in_current_catalog": bool(targets),
                "logical_targets": list(targets),
            }
        )
    return tuple(links)


def _terms(value: object) -> frozenset[str]:
    return frozenset(
        token
        for token in _WORD_RE.findall(str(value).casefold().replace("_", " "))
        if len(token) >= 2 and token not in _STOP_WORDS
    )


def _expanded_question_terms(question: str) -> frozenset[str]:
    terms = set(_terms(question))
    for token in tuple(terms):
        terms.update(_QUESTION_SYNONYMS.get(token, ()))
    return frozenset(terms)


def _select_dictionary_details(
    *,
    question: str,
    catalog: SemanticCatalog,
    dictionary: SnbiDictionary,
) -> tuple[str, ...]:
    """Select relevant detailed fields without model calls or live DB state."""

    question_upper = question.upper()
    question_terms = _expanded_question_terms(question)
    links_by_tag = {
        str(item["snbi_tag"]): tuple(item["logical_targets"])
        for item in _logical_snbi_links(catalog, dictionary)
    }
    scored: list[tuple[int, str]] = []
    for field in dictionary.fields:
        score = 0
        if re.search(rf"(?<![A-Z0-9]){re.escape(field.tag)}(?![A-Z0-9])", question_upper):
            score += 1_000
        title_terms = _terms(field.title)
        legacy_terms = frozenset(
            term
            for source in field.legacy_source_fields
            for term in _terms(source)
        )
        transition_terms = _terms(field.transition_note or "")
        logical_terms = frozenset(
            term
            for target in links_by_tag.get(field.tag, ())
            for value in (target["resource_id"], target["field_id"])
            for term in _terms(value)
        )
        score += 20 * len(question_terms.intersection(title_terms))
        score += 16 * len(question_terms.intersection(logical_terms))
        score += 12 * len(question_terms.intersection(legacy_terms))
        score += 4 * len(question_terms.intersection(transition_terms))
        normalized_title = " ".join(_WORD_RE.findall(field.title.casefold()))
        normalized_question = " ".join(_WORD_RE.findall(question.casefold()))
        if normalized_title and normalized_title in normalized_question:
            score += 100
        if score:
            scored.append((score, field.tag))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return tuple(tag for _score, tag in scored[:MAX_DETAILED_DICTIONARY_FIELDS])


def _compact_dictionary_index(
    *,
    catalog: SemanticCatalog,
    dictionary: SnbiDictionary,
) -> tuple[dict[str, object], ...]:
    links_by_tag = {
        str(item["snbi_tag"]): tuple(item["logical_targets"])
        for item in _logical_snbi_links(catalog, dictionary)
    }
    return tuple(
        {
            "tag": field.tag,
            "title": field.title,
            "section_id": field.section_id,
            "container": field.container,
            "json_type": field.json_type,
            "required_in_container": field.required_in_container,
            "unit": field.unit,
            "queryable_in_current_catalog": bool(links_by_tag[field.tag]),
            "logical_targets": list(links_by_tag[field.tag]),
        }
        for field in dictionary.fields
    )


def _columnar_dictionary_index(
    rows: tuple[dict[str, object], ...],
) -> dict[str, object]:
    """Losslessly pack the all-field planner index without repeated JSON keys."""

    columns = (
        "tag",
        "title",
        "section_id",
        "container",
        "json_type",
        "required_in_container",
        "unit",
        "queryable_in_current_catalog",
        "logical_targets",
    )
    return {
        "format": "columnar_rows_v1",
        "field_count": len(rows),
        "columns": list(columns),
        "rows": [[row[column] for column in columns] for row in rows],
    }


def _compact_plan_dictionary_index(
    packed: Mapping[str, object],
) -> dict[str, object]:
    """Keep every SNBI field while removing repeated object/key overhead."""

    columns = packed.get("columns")
    rows = packed.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise ValueError("the packed SNBI dictionary index is invalid")
    positions = {str(name): index for index, name in enumerate(columns)}
    required = (
        "tag",
        "title",
        "container",
        "json_type",
        "unit",
        "logical_targets",
    )
    if any(name not in positions for name in required):
        raise ValueError("the packed SNBI dictionary index is incomplete")
    resource_ids = sorted(
        {
            str(target.get("resource_id", "")).strip()
            for row in rows
            if isinstance(row, list)
            for target in (
                row[positions["logical_targets"]]
                if isinstance(row[positions["logical_targets"]], list)
                else []
            )
            if isinstance(target, Mapping)
            and str(target.get("resource_id", "")).strip()
        }
    )
    resource_positions = {resource_id: index for index, resource_id in enumerate(resource_ids)}
    compact_rows: list[list[object]] = []
    for row in rows:
        if not isinstance(row, list):
            raise ValueError("the packed SNBI dictionary row is invalid")
        targets = row[positions["logical_targets"]]
        compact_targets: list[list[object]] = []
        if isinstance(targets, list):
            for target in targets:
                if isinstance(target, Mapping):
                    resource_id = str(target.get("resource_id", "")).strip()
                    field_id = str(target.get("field_id", "")).strip()
                    if resource_id and field_id:
                        compact_targets.append(
                            [resource_positions[resource_id], field_id]
                        )
        compact_rows.append(
            [
                row[positions["tag"]],
                row[positions["title"]],
                row[positions["container"]],
                row[positions["json_type"]],
                row[positions["unit"]],
                compact_targets,
            ]
        )
    return {
        "format": "columnar_rows_v2",
        "field_count": len(compact_rows),
        "resource_ids": resource_ids,
        "columns": [
            "tag",
            "title",
            "container",
            "json_type",
            "unit",
            "logical_targets[resource_index,field_id]",
        ],
        "rows": compact_rows,
    }


def _compact_plan_catalog(
    catalog: SemanticCatalog,
    dictionary_index: Mapping[str, object],
) -> dict[str, object]:
    """Serialize the reviewed catalog once, sharing operator sets by index."""

    mapped_targets: set[str] = set()
    rows = dictionary_index.get("rows")
    dictionary_resources = dictionary_index.get("resource_ids")
    if isinstance(rows, list):
        for row in rows:
            if (
                isinstance(row, list)
                and len(row) >= 6
                and isinstance(row[5], list)
                and isinstance(dictionary_resources, list)
            ):
                for target in row[5]:
                    if (
                        isinstance(target, list)
                        and len(target) == 2
                        and isinstance(target[0], int)
                        and 0 <= target[0] < len(dictionary_resources)
                    ):
                        mapped_targets.add(
                            f"{dictionary_resources[target[0]]}.{target[1]}"
                        )

    operator_sets: list[list[str]] = []
    resources: list[dict[str, object]] = []
    for resource in catalog.resources:
        columns: list[list[object]] = []
        for column in resource.columns:
            operators = list(column.allowed_operators)
            if operators not in operator_sets:
                operator_sets.append(operators)
            qualified = f"{resource.resource_id}.{column.field_id}"
            columns.append(
                [
                    column.field_id,
                    column.data_type.value,
                    column.nullable,
                    column.unit,
                    operator_sets.index(operators),
                    column.description,
                ]
            )
        resources.append(
            {
                "id": resource.resource_id,
                "description": resource.description,
                "grain": resource.grain.value,
                "time_semantics": resource.time_semantics,
                "scope_fields": list(resource.scope_fields),
                "columns": columns,
            }
        )
    return {
        "format": "semantic_catalog_compact_v1",
        "catalog_version": catalog.definition_version,
        "catalog_hash": catalog.catalog_hash,
        "column_format": [
            "field_id",
            "type",
            "nullable",
            "unit",
            "operator_set_index",
            "description",
        ],
        "operator_sets": operator_sets,
        "resources": resources,
        "join_format": [
            "id",
            "left_resource",
            "right_resource",
            "left_fields",
            "right_fields",
            "cardinality",
        ],
        "joins": [
            [
                join.join_id,
                join.left_resource_id,
                join.right_resource_id,
                list(join.left_fields),
                list(join.right_fields),
                join.cardinality.value,
            ]
            for join in catalog.joins
        ],
        "function_format": ["id", "min_args", "max_args", "aggregate"],
        "functions": [
            [
                function.function_id,
                function.min_arguments,
                function.max_arguments,
                function.aggregate,
            ]
            for function in catalog.functions
        ],
    }


def _compact_plan_metrics() -> dict[str, object]:
    metrics = DEFAULT_METRIC_REGISTRY
    return {
        "format": "metric_registry_compact_v1",
        "registry_version": metrics.definition_version,
        "registry_hash": metrics.registry_hash,
        "columns": [
            "metric_id",
            "label",
            "description",
            "source_resource",
            "required_columns",
            "unit",
            "status",
            "allowed_dimensions",
        ],
        "rows": [
            [
                metric.metric_id,
                metric.label,
                metric.description,
                metric.source_resource_id,
                list(metric.required_columns),
                metric.unit,
                metric.status.value,
                list(metric.allowed_dimensions),
            ]
            for metric in metrics.metrics
        ],
        "selection_rule": "Only rows with status=active may be selected.",
    }


def build_query_context(
    catalog: SemanticCatalog,
    scope: QueryScope,
    *,
    question: str = "",
    dictionary: SnbiDictionary = DEFAULT_SNBI_DICTIONARY,
    information: SnbiInformationalCatalog = DEFAULT_SNBI_INFORMATION,
    max_bytes: int = MAX_QUERY_CONTEXT_BYTES,
) -> str:
    """Build reviewed model context with a complete index and bounded details.

    Every SNBI field is present in a compact index.  Detailed definitions are
    chosen deterministically from explicit tags plus overlaps with titles,
    logical field names, legacy-source names, and transition notes.  Detail
    entries retain constraints, units/evidence, cardinality, null/temporal
    semantics, code status, legacy inputs, transition evidence, cautions, and
    provenance. The same selected tags receive bounded informational FHWA
    observations; those observations remain explicitly remote-unverified and
    non-validating. Raw projection relations remain excluded.
    """

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")

    scope_payload = {
        "state_code": scope.state_code,
        "bridge_number": scope.bridge_number,
        "time_mode": scope.time_mode.value,
        "data_year": scope.data_year,
        "start_year": scope.start_year,
        "end_year": scope.end_year,
    }
    selected_tags = list(
        _select_dictionary_details(
            question=question,
            catalog=catalog,
            dictionary=dictionary,
        )
    )
    dictionary_index = _compact_dictionary_index(
        catalog=catalog,
        dictionary=dictionary,
    )

    def payload_for(tags: list[str]) -> dict[str, object]:
        return {
        "protocol_version": PROMPT_VERSION,
        "task": (
            "Return exactly one SQLite SELECT statement using only the listed "
            "logical resources and fields. Do not include prose or Markdown."
        ),
        "execution_contract": {
            "scope": (
                "Workspace, state, bridge, and time filters are injected by the "
                "server at every scan; do not attempt to broaden or replace them."
            ),
            "time_routing": {
                "exact": (
                    "Use historical published resources. The exact data_year is "
                    "injected. Never use v_current_bridge_snapshots."
                ),
                "latest": (
                    "Use v_current_bridge_snapshots for bridge-level inventory when "
                    "possible. Historical child resources are reduced to each "
                    "bridge's own latest published year, never a global MAX year."
                ),
                "trend": (
                    "Use historical published resources. The inclusive server "
                    "year range is injected. Never use current-only resources."
                ),
            },
            "selected_server_scope": scope_payload,
            "fail_closed": (
                "If the question cannot be answered from listed queryable fields, "
                "return SELECT NULL AS unavailable_reason FROM an appropriate "
                "listed resource LIMIT 1; never invent a table, field, unit, code "
                "meaning, transition rule, or metric."
            ),
        },
        "semantic_catalog": catalog.prompt_payload(),
        "snbi_dictionary": {
            "dictionary_hash": dictionary.dictionary_hash,
            "complete_compact_index": dictionary_index,
            "detail_selection_method": (
                "deterministic explicit-tag/title/logical-field/legacy-source/"
                "transition-term relevance; score descending then tag ascending"
            ),
            "selected_detailed_tags": tags,
            "selected_details": dictionary.prompt_payload(
                tags,
                include_projection=False,
            ),
        },
        "snbi_informational_enrichment": information.prompt_payload(
            tags,
            max_validation_rules_per_field=8,
        ),
    }

    while True:
        context = _stable_json(payload_for(selected_tags))
        if len(context.encode("utf-8")) <= max_bytes:
            return context
        if not selected_tags:
            raise ValueError(
                "the semantic catalog and complete SNBI field index exceed the prompt budget"
            )
        selected_tags.pop()


def build_plan_context(
    catalog: SemanticCatalog,
    scope: QueryScope,
    *,
    question: str = "",
    dictionary: SnbiDictionary = DEFAULT_SNBI_DICTIONARY,
    official_information: SnbiInformationalCatalog = DEFAULT_SNBI_INFORMATION,
    max_bytes: int = MAX_PLAN_CONTEXT_BYTES,
) -> str:
    """Build the model context for a strict, scope-free ``BridgeQueryPlan``.

    The complete compact SNBI dictionary remains available, while detailed
    entries are trimmed deterministically only when needed to honor the prompt
    budget.  Physical relations, server scope values, and executable SQL are
    never valid plan fields.
    """

    payload = json.loads(
        build_query_context(
            catalog,
            scope,
            question=question,
            dictionary=dictionary,
            # The SQL-oriented source context is compacted further below for
            # typed plan generation; do not apply the smaller final budget
            # before repeated catalog structures have been packed.
            max_bytes=MAX_QUERY_CONTEXT_BYTES,
        )
    )
    payload["protocol_version"] = PLAN_PROMPT_VERSION
    payload["task"] = (
        "Return exactly one JSON BridgeQueryPlan object. Do not return SQL, "
        "Markdown, prose, server scope values, or physical table values. Bridge "
        "identity and data-year fields may be selected as outputs when reviewed."
    )
    dictionary_payload = payload.get("snbi_dictionary", {})
    compact_index = dictionary_payload.get("complete_compact_index", [])
    if isinstance(compact_index, list):
        dictionary_payload["complete_compact_index"] = _columnar_dictionary_index(
            tuple(item for item in compact_index if isinstance(item, dict))
        )
    packed_dictionary = dictionary_payload.get("complete_compact_index")
    if not isinstance(packed_dictionary, Mapping):
        raise ValueError("the complete SNBI field index is unavailable")
    packed_dictionary = _compact_plan_dictionary_index(packed_dictionary)
    dictionary_payload["complete_compact_index"] = packed_dictionary
    payload["plan_contract"] = {
        "closed_object": True,
        "required_fields": ["intent", "resource_id", "time_scope"],
        "optional_fields": [
            "metric_ids",
            "selected_fields",
            "dimensions",
            "filters",
            "order",
            "limit",
        ],
        "intent_values": [item.value for item in PlanIntent],
        "time_scope": {
            "shape": {"mode": scope.time_mode.value},
            "rule": "The mode must equal the server-selected mode; never add year values.",
        },
        "filter_shape": {
            "field_id": "reviewed semantic field identifier",
            "operator": [
                "eq", "ne", "lt", "lte", "gt", "gte", "in", "not_in",
                "between", "contains", "starts_with", "is_null", "is_not_null",
            ],
            "value": "typed scalar/list/null allowed by the selected field and operator",
        },
        "order_shape": {"by": "selected output or metric id", "direction": ["asc", "desc"]},
        "forbidden_root_fields": sorted(
            {
                "sql", "workspace", "workspace_id", "state", "state_code",
                "bridge", "bridge_number", "year", "data_year", "start_year",
                "end_year", "revision_id", "snapshot_revision_id",
            }
        ),
        "server_scope_filter_fields": sorted(
            {
                "workspace_id", "state_code", "bridge_number", "data_year",
                "snapshot_revision_id",
            }
        ),
        "selection_rule": (
            "Reviewed bridge_number and data_year fields may be selected, grouped, "
            "or ordered as outputs. Never use server_scope_filter_fields as filters."
        ),
        "fail_closed": (
            "If no reviewed resource or active metric answers the requested fact, "
            "do not substitute a nearby field; return an invalid empty object so "
            "the server can abstain."
        ),
    }
    payload["reviewed_metric_registry"] = _compact_plan_metrics()
    payload["semantic_catalog"] = _compact_plan_catalog(catalog, packed_dictionary)
    payload["reference_mappings"] = {
        "new_jersey_counties": list(NEW_JERSEY_COUNTY_PROMPT_ROWS),
        "planning_rule": (
            "For a latest/current question asking for results in each county, use "
            "v_current_county_bridge_inventory and return county_name, not only county_code."
        ),
    }
    selected_tags = dictionary_payload.get(
        "selected_detailed_tags", []
    )
    # ``build_query_context`` carries the same informational catalog under its
    # SQL-planner key.  A BridgeQueryPlan prompt uses the plan-specific key
    # below, so retaining both would duplicate tens of kilobytes of identical
    # metadata and, worse, leave the first copy stale when details are trimmed.
    payload.pop("snbi_informational_enrichment", None)
    informational = official_information.prompt_payload(selected_tags)
    # Full source receipts and global coverage counts are audit/provenance data,
    # not query-planning input. Relevant per-field observations remain present.
    for key in ("source_catalog", "q_and_a_notes", "coverage"):
        informational.pop(key, None)
    payload["fhwa_informational_enrichment"] = informational
    payload["execution_contract"]["output"] = (
        "The server validates and compiles the plan, injects trusted scope, and "
        "runs the result through an independent SQL guard and read-only sandbox."
    )
    payload["execution_contract"]["fail_closed"] = (
        "Do not emit SQL or substitute a nearby field. If the request is not "
        "representable by a reviewed plan, return an invalid empty object so "
        "the server's retry or abstention path is triggered."
    )

    selected_details = dictionary_payload.get("selected_details", {})
    details = selected_details.get("fields", [])
    selected = dictionary_payload.get("selected_detailed_tags", [])
    while True:
        context = _stable_json(payload)
        if len(context.encode("utf-8")) <= max_bytes:
            return context
        if not details:
            raise ValueError(
                "the semantic catalog, metric registry, and complete SNBI field index exceed the prompt budget"
            )
        details.pop()
        selected_details["field_count"] = len(details)
        if selected:
            selected.pop()
        # Keep the richer FHWA observations aligned to the same bounded field
        # selection when the prompt must shrink.
        informational = official_information.prompt_payload(selected)
        for key in ("source_catalog", "q_and_a_notes", "coverage"):
            informational.pop(key, None)
        payload["fhwa_informational_enrichment"] = informational


def _parse_generated_plan(
    generated: str | Mapping[str, object] | BridgeQueryPlan,
    *,
    default_limit: int = 50,
) -> BridgeQueryPlan:
    if isinstance(generated, BridgeQueryPlan):
        return generated
    if isinstance(generated, Mapping):
        payload = dict(generated)
        payload.setdefault("limit", default_limit)
        return BridgeQueryPlan.from_payload(payload)
    if not isinstance(generated, str):
        raise PlanParseError(
            "invalid_plan_type", "Plan generator must return JSON, a mapping, or BridgeQueryPlan"
        )
    if len(generated.encode("utf-8")) > MAX_PLAN_RESPONSE_BYTES:
        raise PlanParseError("plan_too_large", "Generated plan exceeds the response budget")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise PlanParseError(
                    "duplicate_plan_key", "Generated plan contains a duplicate JSON key", key=key
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise PlanParseError(
            "invalid_plan_number", "Generated plan contains a non-finite JSON number", value=value
        )

    try:
        payload = json.loads(
            generated,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except PlanError:
        raise
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise PlanParseError("invalid_plan_json", "Generated plan is not strict JSON") from exc
    if isinstance(payload, dict):
        payload.setdefault("limit", default_limit)
    return BridgeQueryPlan.from_payload(payload)


def cancel_semantic_query(
    *,
    request_id: str,
    identity: ServerQueryIdentity,
    registry: RequestRegistry = DEFAULT_REQUEST_REGISTRY,
) -> CancelOutcome:
    """Cancel an active SQLite phase owned by this actor and workspace.

    The process-local registry is attached by :func:`execute_read_query`, so it
    cannot interrupt an upstream model-provider call that is still generating
    SQL. API integration must give the provider its own request-scoped cancel
    token (or disconnect handling) and then use this function for the SQLite
    phase; a global chat-cancel flag is not an acceptable substitute.
    """

    return registry.cancel(
        request_id=request_id,
        owner_id=identity.actor_id,
        workspace_id=identity.workspace_id,
    )


def _audit_scope(scope: QueryScope) -> QueryAuditScope:
    return QueryAuditScope(
        workspace_id=scope.workspace_id,
        state_code=scope.state_code,
        time_mode=scope.time_mode.value,
        bridge_number=scope.bridge_number,
        data_year=scope.data_year,
        start_year=scope.start_year,
        end_year=scope.end_year,
    )


def _enforce_time_routing(
    validated: ValidatedSql,
    catalog: SqlCatalog,
    scope: QueryScope,
) -> None:
    """Prevent current-only rows from silently answering historical questions."""

    if scope.time_mode not in {TimeMode.EXACT, TimeMode.TREND}:
        return
    current = tuple(
        scan.table
        for scan in validated.tables
        if catalog.tables[scan.table.casefold()].time_semantics
        is TableTimeSemantics.CURRENT
    )
    if current:
        raise SqlCompilationError(
            "incompatible_time_resource",
            "Exact and trend questions require historical published resources",
            tables=current,
            time_mode=scope.time_mode.value,
        )


def _reason_code(value: str) -> str:
    reason = _REASON_RE.sub("_", str(value).strip().casefold()).strip("_")
    return reason[:128] or "query_failed"


def _finish_audit(
    database_path: str | Path,
    *,
    handle: object,
    outcome: QueryAuditOutcome,
    started: float,
    monotonic: Callable[[], float],
    now: Callable[[], datetime],
    reason: str,
    validated: ValidatedSql | None = None,
    compiled: CompiledSql | None = None,
    progress_steps: int = 0,
    row_count: int | None = None,
    byte_count: int | None = None,
) -> None:
    duration_ms = max(0, round((monotonic() - started) * 1_000))
    finish_query_attempt(
        database_path,
        FinishQueryAuditRequest(
            handle=handle,  # type: ignore[arg-type]
            outcome=outcome,
            finished_at=now(),
            duration_ms=duration_ms,
            progress_steps=max(0, int(progress_steps)),
            termination_reason=_reason_code(reason),
            validated_ast=(
                ValidatedAstAudit.from_validated_sql(validated)
                if validated is not None
                else None
            ),
            compiled_sql=compiled.sql if compiled is not None else None,
            parameters=compiled.parameters if compiled is not None else None,
            row_count=row_count,
            byte_count=byte_count,
        ),
    )


def execute_semantic_plan_query(
    *,
    database_path: str | Path,
    request: SemanticQueryRequest,
    generate_plan: PlanGenerator,
    semantic_catalog: SemanticCatalog | None = None,
    dictionary: SnbiDictionary = DEFAULT_SNBI_DICTIONARY,
    official_information: SnbiInformationalCatalog = DEFAULT_SNBI_INFORMATION,
    plan_limits: PlanLimits | None = None,
    sql_limits: SqlLimits | None = None,
    sandbox_policy: SandboxPolicy = DEFAULT_SANDBOX_POLICY,
    registry: RequestRegistry = DEFAULT_REQUEST_REGISTRY,
    now: Callable[[], datetime] = _utc_now,
    monotonic: Callable[[], float] = time.monotonic,
) -> SemanticQueryEvidence:
    """Generate, validate, compile, execute, and audit one typed query plan.

    This is Bridgette's production query path.  The model cannot emit SQL or
    carry tenant, state, bridge, year, or revision values.  A first rejected
    plan receives one code-only correction opportunity; a second rejection
    abstains.  Both the plan compiler and independent SQL guard run before the
    read-only sandbox.
    """

    catalog = semantic_catalog or build_semantic_catalog(
        database_identity=request.database_identity,
        workspace_id=request.identity.workspace_id,
        schema_generation=request.schema_generation,
        data_generation=request.data_generation,
    )
    if catalog.identity.workspace_id != request.identity.workspace_id:
        raise ValueError("semantic catalog workspace does not match the request")
    if catalog.identity.schema_generation != request.schema_generation:
        raise ValueError("semantic catalog schema generation does not match the request")
    if catalog.identity.data_generation != request.data_generation:
        raise ValueError("semantic catalog data generation does not match the request")

    context_json = build_plan_context(
        catalog,
        request.scope,
        question=request.question,
        dictionary=dictionary,
        official_information=official_information,
    )
    context_hash = hashlib.sha256(context_json.encode("utf-8")).hexdigest()
    bounded_plan_limit = min(request.max_rows, 200)
    active_plan_limits = plan_limits or PlanLimits(
        max_limit=bounded_plan_limit,
        max_lookup_limit=min(20, bounded_plan_limit),
    )
    active_sql_limits = sql_limits or SqlLimits(max_rows=min(request.max_rows, 200))
    feedback: GenerationFeedback | None = None

    for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
        started = monotonic()
        try:
            handle = accept_query_attempt(
                database_path,
                AcceptQueryAuditRequest(
                    workspace_id=request.identity.workspace_id,
                    request_id=request.request_id,
                    attempt=attempt,
                    actor_id=request.identity.actor_id,
                    role=request.identity.role,
                    session_id=request.identity.session_id,
                    surface=request.identity.surface,
                    question=request.question,
                    scope=_audit_scope(request.scope),
                    schema_generation=request.schema_generation,
                    data_generation=request.data_generation,
                    catalog_sha256=catalog.catalog_hash,
                    accepted_at=now(),
                    model_id=request.model_id,
                    prompt_version=(
                        f"{PLAN_PROMPT_VERSION}/"
                        f"c-{catalog.catalog_hash[:16]}/"
                        f"d-{dictionary.dictionary_hash[:16]}/"
                        f"metrics-{DEFAULT_METRIC_REGISTRY.registry_hash[:16]}/"
                        f"i-{official_information.dataset_sha256[:16]}"
                    ),
                ),
            )
        except QueryAuditError as exc:
            raise SemanticQueryAuditFailed(
                "The query was not run because audit acceptance was unavailable.",
                request_id=request.request_id,
                attempts=attempt - 1,
                details={"failure_code": exc.code},
            ) from exc

        compiled_plan = None
        try:
            generated = generate_plan(
                PlanGenerationRequest(
                    request_id=request.request_id,
                    attempt=attempt,
                    question=request.question,
                    scope=request.scope,
                    context_json=context_json,
                    context_sha256=context_hash,
                    feedback=feedback,
                )
            )
            plan = _parse_generated_plan(
                generated,
                default_limit=min(request.max_rows, 200),
            )
            compiled_plan = compile_bridge_query_plan(
                plan,
                catalog,
                request.scope,
                metric_registry=DEFAULT_METRIC_REGISTRY,
                plan_limits=active_plan_limits,
                sql_limits=active_sql_limits,
            )
            validated = compiled_plan.validated_sql
            compiled = compiled_plan.compiled_sql
        except PlanError as exc:
            try:
                _finish_audit(
                    database_path,
                    handle=handle,
                    outcome=QueryAuditOutcome.REJECTED,
                    started=started,
                    monotonic=monotonic,
                    now=now,
                    reason=exc.code,
                )
            except QueryAuditError as audit_exc:
                raise SemanticQueryAuditFailed(
                    "The query was stopped because terminal audit evidence was unavailable.",
                    request_id=request.request_id,
                    attempts=attempt,
                    details={"failure_code": audit_exc.code},
                ) from audit_exc
            if attempt < MAX_GENERATION_ATTEMPTS:
                feedback = GenerationFeedback(
                    attempt=attempt,
                    code=exc.code,
                    instruction=_plan_repair_instruction(exc.code),
                )
                continue
            raise SemanticQueryAbstained(
                "Bridgette could not produce an approved query plan and abstained.",
                request_id=request.request_id,
                attempts=attempt,
                details={"last_failure_code": exc.code},
            ) from exc
        except Exception as exc:
            try:
                _finish_audit(
                    database_path,
                    handle=handle,
                    outcome=QueryAuditOutcome.FAILED,
                    started=started,
                    monotonic=monotonic,
                    now=now,
                    reason="plan_generation_failed",
                )
            except QueryAuditError as audit_exc:
                raise SemanticQueryAuditFailed(
                    "The query was stopped because terminal audit evidence was unavailable.",
                    request_id=request.request_id,
                    attempts=attempt,
                    details={"failure_code": audit_exc.code},
                ) from audit_exc
            raise SemanticQueryGenerationFailed(
                "Bridgette could not create a query plan.",
                request_id=request.request_id,
                attempts=attempt,
            ) from exc

        try:
            result = execute_read_query(
                database_path=database_path,
                sql=compiled.sql,
                parameters=compiled.parameters,
                request_id=request.request_id,
                owner_id=request.identity.actor_id,
                workspace_id=request.identity.workspace_id,
                allowed_relations=catalog.sandbox_relation_allowlist,
                allowed_functions=tuple(
                    function.sql_name.casefold() for function in catalog.functions
                ),
                policy=sandbox_policy,
                registry=registry,
            )
        except SandboxError as exc:
            if isinstance(exc, QueryCancelled):
                outcome = QueryAuditOutcome.CANCELLED
                error_type: type[SemanticQueryError] = SemanticQueryCancelled
                message = "The query was cancelled."
            elif isinstance(exc, (QueryDeadlineExceeded, QueryVmBudgetExceeded)):
                outcome = QueryAuditOutcome.TIMED_OUT
                error_type = SemanticQueryTimedOut
                message = "The query exceeded its bounded execution budget."
            elif isinstance(exc, QueryDenied):
                outcome = QueryAuditOutcome.REJECTED
                error_type = SemanticQueryAbstained
                message = "The query was denied by the read-only data boundary."
            else:
                outcome = QueryAuditOutcome.FAILED
                error_type = SemanticQueryExecutionFailed
                message = "The bounded query could not be completed."
            try:
                _finish_audit(
                    database_path,
                    handle=handle,
                    outcome=outcome,
                    started=started,
                    monotonic=monotonic,
                    now=now,
                    reason=exc.code,
                    validated=validated,
                    compiled=compiled,
                )
            except QueryAuditError as audit_exc:
                raise SemanticQueryAuditFailed(
                    "The query was stopped because terminal audit evidence was unavailable.",
                    request_id=request.request_id,
                    attempts=attempt,
                    details={"failure_code": audit_exc.code},
                ) from audit_exc
            raise error_type(
                message,
                request_id=request.request_id,
                attempts=attempt,
                details={"failure_code": exc.code},
            ) from exc
        except Exception as exc:
            try:
                _finish_audit(
                    database_path,
                    handle=handle,
                    outcome=QueryAuditOutcome.FAILED,
                    started=started,
                    monotonic=monotonic,
                    now=now,
                    reason="execution_integration_failed",
                    validated=validated,
                    compiled=compiled,
                )
            except QueryAuditError as audit_exc:
                raise SemanticQueryAuditFailed(
                    "The query was stopped because terminal audit evidence was unavailable.",
                    request_id=request.request_id,
                    attempts=attempt,
                    details={"failure_code": audit_exc.code},
                ) from audit_exc
            raise SemanticQueryExecutionFailed(
                "The bounded query could not be completed.",
                request_id=request.request_id,
                attempts=attempt,
                details={"failure_code": "execution_integration_failed"},
            ) from exc

        audit_outcome = (
            QueryAuditOutcome.TRUNCATED
            if result.outcome is QueryOutcome.TRUNCATED
            else QueryAuditOutcome.COMPLETED
        )
        try:
            _finish_audit(
                database_path,
                handle=handle,
                outcome=audit_outcome,
                started=started,
                monotonic=monotonic,
                now=now,
                reason=result.outcome.value,
                validated=validated,
                compiled=compiled,
                progress_steps=result.progress_callbacks,
                row_count=result.row_count,
                byte_count=result.serialized_bytes,
            )
        except QueryAuditError as exc:
            raise SemanticQueryAuditFailed(
                "The result was withheld because terminal audit evidence was unavailable.",
                request_id=request.request_id,
                attempts=attempt,
                details={"failure_code": exc.code},
            ) from exc

        return SemanticQueryEvidence(
            request_id=request.request_id,
            attempt=attempt,
            columns=result.columns,
            rows=result.rows,
            row_count=result.row_count,
            truncated=result.truncated,
            serialized_bytes=result.serialized_bytes,
            elapsed_ms=result.elapsed_ms,
            progress_steps=result.progress_callbacks,
            scope=request.scope,
            ast=QueryAstEvidence.from_validated(validated),
            semantic_catalog_sha256=catalog.catalog_hash,
            snbi_dictionary_sha256=dictionary.dictionary_hash,
            prompt_context_sha256=context_hash,
            compiled_sql_sha256=hashlib.sha256(
                compiled.sql.encode("utf-8")
            ).hexdigest(),
            snbi_information_sha256=official_information.dataset_sha256,
            plan_evidence=compiled_plan.evidence,
        )

    raise AssertionError("semantic plan query attempt loop exhausted unexpectedly")


def execute_semantic_query(
    *,
    database_path: str | Path,
    request: SemanticQueryRequest,
    generate_sql: SqlGenerator,
    semantic_catalog: SemanticCatalog | None = None,
    dictionary: SnbiDictionary = DEFAULT_SNBI_DICTIONARY,
    sql_limits: SqlLimits | None = None,
    sandbox_policy: SandboxPolicy = DEFAULT_SANDBOX_POLICY,
    registry: RequestRegistry = DEFAULT_REQUEST_REGISTRY,
    now: Callable[[], datetime] = _utc_now,
    monotonic: Callable[[], float] = time.monotonic,
) -> SemanticQueryEvidence:
    """Generate, validate, scope, execute, and audit one Bridgette query.

    A structurally rejected first statement receives one value-free correction
    opportunity.  A second rejection produces a typed abstention. Runtime,
    cancellation, deadline, generator, and audit failures are never retried and
    never fall back to a less restrictive executor.
    """

    catalog = semantic_catalog or build_semantic_catalog(
        database_identity=request.database_identity,
        workspace_id=request.identity.workspace_id,
        schema_generation=request.schema_generation,
        data_generation=request.data_generation,
    )
    if catalog.identity.workspace_id != request.identity.workspace_id:
        raise ValueError("semantic catalog workspace does not match the request")
    if catalog.identity.schema_generation != request.schema_generation:
        raise ValueError("semantic catalog schema generation does not match the request")
    if catalog.identity.data_generation != request.data_generation:
        raise ValueError("semantic catalog data generation does not match the request")

    sql_catalog = build_sql_catalog(catalog)
    context_json = build_query_context(
        catalog,
        request.scope,
        question=request.question,
        dictionary=dictionary,
    )
    context_hash = hashlib.sha256(context_json.encode("utf-8")).hexdigest()
    limits = sql_limits or SqlLimits(max_rows=min(request.max_rows, 200))
    feedback: GenerationFeedback | None = None

    for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
        started = monotonic()
        try:
            handle = accept_query_attempt(
                database_path,
                AcceptQueryAuditRequest(
                    workspace_id=request.identity.workspace_id,
                    request_id=request.request_id,
                    attempt=attempt,
                    actor_id=request.identity.actor_id,
                    role=request.identity.role,
                    session_id=request.identity.session_id,
                    surface=request.identity.surface,
                    question=request.question,
                    scope=_audit_scope(request.scope),
                    schema_generation=request.schema_generation,
                    data_generation=request.data_generation,
                    catalog_sha256=catalog.catalog_hash,
                    accepted_at=now(),
                    model_id=request.model_id,
                    prompt_version=(
                        f"{request.prompt_version}/catalog-{catalog.catalog_hash}/"
                        f"dictionary-{dictionary.dictionary_hash}"
                    ),
                ),
            )
        except QueryAuditError as exc:
            raise SemanticQueryAuditFailed(
                "The query was not run because audit acceptance was unavailable.",
                request_id=request.request_id,
                attempts=attempt - 1,
                details={"failure_code": exc.code},
            ) from exc

        validated: ValidatedSql | None = None
        compiled: CompiledSql | None = None
        try:
            generated = generate_sql(
                SqlGenerationRequest(
                    request_id=request.request_id,
                    attempt=attempt,
                    question=request.question,
                    scope=request.scope,
                    context_json=context_json,
                    context_sha256=context_hash,
                    feedback=feedback,
                )
            )
            if not isinstance(generated, str) or not generated.strip():
                raise TypeError("SQL generator must return a non-empty string")
            validated = validate_generated_sql(generated, sql_catalog, limits=limits)
            _enforce_time_routing(validated, sql_catalog, request.scope)
            compiled = compile_scoped_sql(
                validated,
                request.scope,
                limits=limits,
                max_rows=request.max_rows,
            )
        except SqlGuardError as exc:
            try:
                _finish_audit(
                    database_path,
                    handle=handle,
                    outcome=QueryAuditOutcome.REJECTED,
                    started=started,
                    monotonic=monotonic,
                    now=now,
                    reason=exc.code,
                    validated=validated,
                )
            except QueryAuditError as audit_exc:
                raise SemanticQueryAuditFailed(
                    "The query was stopped because terminal audit evidence was unavailable.",
                    request_id=request.request_id,
                    attempts=attempt,
                    details={"failure_code": audit_exc.code},
                ) from audit_exc
            if attempt < MAX_GENERATION_ATTEMPTS:
                feedback = GenerationFeedback(
                    attempt=attempt,
                    code=exc.code,
                    instruction=(
                        "Return one simpler SELECT using only listed logical resources, "
                        "fields, functions, and the selected time semantics."
                    ),
                )
                continue
            raise SemanticQueryAbstained(
                "Bridgette could not produce an approved query and abstained.",
                request_id=request.request_id,
                attempts=attempt,
                details={"last_failure_code": exc.code},
            ) from exc
        except Exception as exc:
            try:
                _finish_audit(
                    database_path,
                    handle=handle,
                    outcome=QueryAuditOutcome.FAILED,
                    started=started,
                    monotonic=monotonic,
                    now=now,
                    reason="sql_generation_failed",
                )
            except QueryAuditError as audit_exc:
                raise SemanticQueryAuditFailed(
                    "The query was stopped because terminal audit evidence was unavailable.",
                    request_id=request.request_id,
                    attempts=attempt,
                    details={"failure_code": audit_exc.code},
                ) from audit_exc
            raise SemanticQueryGenerationFailed(
                "Bridgette could not create a query.",
                request_id=request.request_id,
                attempts=attempt,
            ) from exc

        try:
            result = execute_read_query(
                database_path=database_path,
                sql=compiled.sql,
                parameters=compiled.parameters,
                request_id=request.request_id,
                owner_id=request.identity.actor_id,
                workspace_id=request.identity.workspace_id,
                allowed_relations=catalog.sandbox_relation_allowlist,
                allowed_functions=tuple(
                    function.sql_name.casefold() for function in catalog.functions
                ),
                policy=sandbox_policy,
                registry=registry,
            )
        except SandboxError as exc:
            if isinstance(exc, QueryCancelled):
                outcome = QueryAuditOutcome.CANCELLED
                error_type: type[SemanticQueryError] = SemanticQueryCancelled
                message = "The query was cancelled."
            elif isinstance(exc, (QueryDeadlineExceeded, QueryVmBudgetExceeded)):
                outcome = QueryAuditOutcome.TIMED_OUT
                error_type = SemanticQueryTimedOut
                message = "The query exceeded its bounded execution budget."
            elif isinstance(exc, QueryDenied):
                outcome = QueryAuditOutcome.REJECTED
                error_type = SemanticQueryAbstained
                message = "The query was denied by the read-only data boundary."
            else:
                outcome = QueryAuditOutcome.FAILED
                error_type = SemanticQueryExecutionFailed
                message = "The bounded query could not be completed."
            try:
                _finish_audit(
                    database_path,
                    handle=handle,
                    outcome=outcome,
                    started=started,
                    monotonic=monotonic,
                    now=now,
                    reason=exc.code,
                    validated=validated,
                    compiled=compiled,
                )
            except QueryAuditError as audit_exc:
                raise SemanticQueryAuditFailed(
                    "The query was stopped because terminal audit evidence was unavailable.",
                    request_id=request.request_id,
                    attempts=attempt,
                    details={"failure_code": audit_exc.code},
                ) from audit_exc
            raise error_type(
                message,
                request_id=request.request_id,
                attempts=attempt,
                details={"failure_code": exc.code},
            ) from exc
        except Exception as exc:
            # Preserve the accepted+terminal invariant even for an unexpected
            # executor/registry integration fault. Never expose driver text.
            try:
                _finish_audit(
                    database_path,
                    handle=handle,
                    outcome=QueryAuditOutcome.FAILED,
                    started=started,
                    monotonic=monotonic,
                    now=now,
                    reason="execution_integration_failed",
                    validated=validated,
                    compiled=compiled,
                )
            except QueryAuditError as audit_exc:
                raise SemanticQueryAuditFailed(
                    "The query was stopped because terminal audit evidence was unavailable.",
                    request_id=request.request_id,
                    attempts=attempt,
                    details={"failure_code": audit_exc.code},
                ) from audit_exc
            raise SemanticQueryExecutionFailed(
                "The bounded query could not be completed.",
                request_id=request.request_id,
                attempts=attempt,
                details={"failure_code": "execution_integration_failed"},
            ) from exc

        audit_outcome = (
            QueryAuditOutcome.TRUNCATED
            if result.outcome is QueryOutcome.TRUNCATED
            else QueryAuditOutcome.COMPLETED
        )
        try:
            _finish_audit(
                database_path,
                handle=handle,
                outcome=audit_outcome,
                started=started,
                monotonic=monotonic,
                now=now,
                reason=result.outcome.value,
                validated=validated,
                compiled=compiled,
                progress_steps=result.progress_callbacks,
                row_count=result.row_count,
                byte_count=result.serialized_bytes,
            )
        except QueryAuditError as exc:
            raise SemanticQueryAuditFailed(
                "The result was withheld because terminal audit evidence was unavailable.",
                request_id=request.request_id,
                attempts=attempt,
                details={"failure_code": exc.code},
            ) from exc

        return SemanticQueryEvidence(
            request_id=request.request_id,
            attempt=attempt,
            columns=result.columns,
            rows=result.rows,
            row_count=result.row_count,
            truncated=result.truncated,
            serialized_bytes=result.serialized_bytes,
            elapsed_ms=result.elapsed_ms,
            progress_steps=result.progress_callbacks,
            scope=request.scope,
            ast=QueryAstEvidence.from_validated(validated),
            semantic_catalog_sha256=catalog.catalog_hash,
            snbi_dictionary_sha256=dictionary.dictionary_hash,
            prompt_context_sha256=context_hash,
            compiled_sql_sha256=hashlib.sha256(
                compiled.sql.encode("utf-8")
            ).hexdigest(),
        )

    raise AssertionError("semantic query attempt loop exhausted unexpectedly")


__all__ = [
    "GenerationFeedback",
    "MAX_GENERATION_ATTEMPTS",
    "MAX_DETAILED_DICTIONARY_FIELDS",
    "MAX_PLAN_RESPONSE_BYTES",
    "MAX_QUERY_CONTEXT_BYTES",
    "PLAN_PROMPT_VERSION",
    "PROMPT_VERSION",
    "PlanGenerationRequest",
    "PlanGenerator",
    "QueryAstEvidence",
    "SemanticQueryAbstained",
    "SemanticQueryAuditFailed",
    "SemanticQueryCancelled",
    "SemanticQueryError",
    "SemanticQueryEvidence",
    "SemanticQueryExecutionFailed",
    "SemanticQueryGenerationFailed",
    "SemanticQueryRequest",
    "SemanticQueryTimedOut",
    "ServerQueryIdentity",
    "SqlGenerationRequest",
    "SqlGenerator",
    "build_query_context",
    "build_plan_context",
    "build_sql_catalog",
    "cancel_semantic_query",
    "execute_semantic_query",
    "execute_semantic_plan_query",
]
