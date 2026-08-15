"""Append-only, privacy-minimized audit evidence for semantic query attempts.

The query boundary records an ``accepted`` event before any model-generated
SQL is executed and one generic ``terminal`` event afterwards.  The terminal
event's ``outcome`` describes success, rejection, timeout, cancellation, or a
safe execution failure.  A single terminal event type is intentional: the
database uniqueness constraint can then mechanically enforce at-most-one
terminal result for each ``(workspace, request, attempt)``.

This module never stores the question, query parameters, result rows, source
content, or the database path.  Questions are represented only by SHA-256 and
parameter values are replaced by deterministic type markers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from types import MappingProxyType
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .answers import EvidenceTable, MAX_EVIDENCE_BYTES
from .plans import BridgeQueryPlan, PlanError


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,254}$")
_PARAMETER_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SQL_MAX_BYTES = 65_536


class QueryAuditError(RuntimeError):
    """Base class for typed, client-safe audit failures."""

    code = "query_audit_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class QueryAuditInputError(QueryAuditError, ValueError):
    code = "invalid_query_audit_input"


class QueryAuditDatabaseError(QueryAuditError):
    code = "query_audit_database_unavailable"


class AcceptedAuditWriteError(QueryAuditDatabaseError):
    """The fail-closed accepted event could not be persisted."""

    code = "accepted_query_audit_failed"


class TerminalAuditWriteError(QueryAuditDatabaseError):
    """A terminal event could not be persisted."""

    code = "terminal_query_audit_failed"


class QueryResponseAuditWriteError(QueryAuditDatabaseError):
    """The verified response receipt could not be persisted."""

    code = "query_response_audit_failed"


class QueryAuditStateError(QueryAuditError):
    code = "invalid_query_audit_state"


class QueryAuditAttemptExists(QueryAuditStateError):
    code = "query_audit_attempt_exists"


class QueryAuditAcceptedMissing(QueryAuditStateError):
    code = "query_audit_accepted_missing"


class QueryAuditDuplicateTerminal(QueryAuditStateError):
    code = "query_audit_terminal_exists"


class QueryResponseAuditExists(QueryAuditStateError):
    code = "query_response_audit_exists"


class QueryAuditOutcome(str, Enum):
    COMPLETED = "completed"
    TRUNCATED = "truncated"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class QueryAuditScope:
    """Server-derived query scope; never accept this object from an LLM."""

    workspace_id: str
    state_code: int | str
    time_mode: str
    bridge_number: str | None = None
    data_year: int | None = None
    start_year: int | None = None
    end_year: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_id", _token(self.workspace_id, "workspace_id"))
        if isinstance(self.state_code, bool) or not isinstance(self.state_code, (int, str)):
            raise QueryAuditInputError("state_code must be an integer or string")
        if isinstance(self.state_code, str):
            object.__setattr__(self, "state_code", _token(self.state_code, "state_code"))
        mode = str(self.time_mode).strip().lower()
        if mode not in {"exact", "latest", "trend"}:
            raise QueryAuditInputError("time_mode must be exact, latest, or trend")
        object.__setattr__(self, "time_mode", mode)
        if self.bridge_number is not None:
            object.__setattr__(
                self, "bridge_number", _token(self.bridge_number, "bridge_number")
            )
        for name in ("data_year", "start_year", "end_year"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or not 1800 <= value <= 2200
            ):
                raise QueryAuditInputError(f"{name} must be an integer between 1800 and 2200")
        if mode == "exact":
            if self.data_year is None or self.start_year is not None or self.end_year is not None:
                raise QueryAuditInputError("exact scope requires only data_year")
        elif mode == "latest":
            if any(value is not None for value in (self.data_year, self.start_year, self.end_year)):
                raise QueryAuditInputError("latest scope cannot include explicit years")
        else:
            if self.data_year is not None or self.start_year is None or self.end_year is None:
                raise QueryAuditInputError("trend scope requires start_year and end_year")
            if self.start_year > self.end_year:
                raise QueryAuditInputError("start_year cannot be after end_year")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "bridge_number": self.bridge_number,
            "data_year": self.data_year,
            "end_year": self.end_year,
            "start_year": self.start_year,
            "state_code": self.state_code,
            "time_mode": self.time_mode,
            "workspace_id": self.workspace_id,
        }


@dataclass(frozen=True, slots=True)
class ValidatedAstAudit:
    """Value-free structural summary of the reviewed SQL AST."""

    tables: tuple[str, ...]
    columns: tuple[str, ...]
    output_columns: tuple[str, ...]
    cte_count: int
    join_count: int
    subquery_count: int
    expression_depth: int
    literal_count: int
    statement_kind: str = "select"

    def __post_init__(self) -> None:
        for name in ("tables", "columns", "output_columns"):
            values = tuple(str(value).strip().lower() for value in getattr(self, name))
            if any(not _PARAMETER_NAME_RE.fullmatch(value) for value in values):
                raise QueryAuditInputError(f"{name} contains an invalid semantic identifier")
            object.__setattr__(self, name, values)
        for name in (
            "cte_count",
            "join_count",
            "subquery_count",
            "expression_depth",
            "literal_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise QueryAuditInputError(f"{name} must be a non-negative integer")
        if str(self.statement_kind).lower() != "select":
            raise QueryAuditInputError("only SELECT AST evidence is accepted")
        object.__setattr__(self, "statement_kind", "select")

    @classmethod
    def from_validated_sql(cls, validated: object) -> "ValidatedAstAudit":
        """Build a privacy-safe audit summary from ``sql_ast.ValidatedSql``.

        This is deliberately duck typed to keep the evidence layer independent
        of the parser implementation.  Raw SQL and private token fields are
        never copied.
        """

        try:
            tables = tuple(str(item.table) for item in getattr(validated, "tables"))
            columns = tuple(str(item.column) for item in getattr(validated, "columns"))
            output_columns = tuple(str(item) for item in getattr(validated, "output_columns"))
            return cls(
                tables=tables,
                columns=columns,
                output_columns=output_columns,
                cte_count=int(getattr(validated, "cte_count")),
                join_count=int(getattr(validated, "join_count")),
                subquery_count=int(getattr(validated, "subquery_count")),
                expression_depth=int(getattr(validated, "expression_depth")),
                literal_count=int(getattr(validated, "literal_count")),
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise QueryAuditInputError("validated AST evidence is invalid") from exc

    def canonical_payload(self) -> dict[str, object]:
        return {
            "columns": list(self.columns),
            "cte_count": self.cte_count,
            "expression_depth": self.expression_depth,
            "join_count": self.join_count,
            "literal_count": self.literal_count,
            "output_columns": list(self.output_columns),
            "statement_kind": self.statement_kind,
            "subquery_count": self.subquery_count,
            "tables": list(self.tables),
        }


@dataclass(frozen=True, slots=True)
class AcceptQueryAuditRequest:
    workspace_id: str
    request_id: str
    attempt: int
    actor_id: str
    role: str
    session_id: str | None
    surface: str
    question: str
    scope: QueryAuditScope
    schema_generation: int
    data_generation: int
    catalog_sha256: str
    accepted_at: datetime | str
    model_id: str | None = None
    prompt_version: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_id", _token(self.workspace_id, "workspace_id"))
        object.__setattr__(self, "request_id", _token(self.request_id, "request_id"))
        object.__setattr__(self, "actor_id", _token(self.actor_id, "actor_id"))
        object.__setattr__(self, "role", _token(self.role, "role"))
        object.__setattr__(self, "surface", _token(self.surface, "surface"))
        if self.session_id is not None:
            object.__setattr__(self, "session_id", _token(self.session_id, "session_id"))
        if not isinstance(self.question, str) or not self.question.strip():
            raise QueryAuditInputError("question is required")
        if self.scope.workspace_id != self.workspace_id:
            raise QueryAuditInputError("scope workspace does not match the audit workspace")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 1:
            raise QueryAuditInputError("attempt must be a positive integer")
        for name in ("schema_generation", "data_generation"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise QueryAuditInputError(f"{name} must be a non-negative integer")
        object.__setattr__(self, "catalog_sha256", _sha256(self.catalog_sha256, "catalog_sha256"))
        object.__setattr__(self, "accepted_at", _timestamp(self.accepted_at))
        for name in ("model_id", "prompt_version"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _token(value, name))


@dataclass(frozen=True, slots=True)
class QueryAuditHandle:
    audit_id: str
    workspace_id: str
    request_id: str
    attempt: int
    actor_id: str
    role: str
    session_id: str | None
    surface: str
    question_sha256: str
    scope_json: str
    schema_generation: int
    data_generation: int
    catalog_sha256: str
    model_id: str | None
    prompt_version: str | None
    accepted_at: str


@dataclass(frozen=True, slots=True)
class FinishQueryAuditRequest:
    handle: QueryAuditHandle
    outcome: QueryAuditOutcome | str
    finished_at: datetime | str
    duration_ms: int
    progress_steps: int
    termination_reason: str
    validated_ast: ValidatedAstAudit | None = None
    compiled_sql: str | None = None
    parameters: Mapping[str, object] | Sequence[object] | None = None
    row_count: int | None = None
    byte_count: int | None = None

    def __post_init__(self) -> None:
        try:
            outcome = self.outcome if isinstance(self.outcome, QueryAuditOutcome) else QueryAuditOutcome(str(self.outcome))
        except ValueError as exc:
            raise QueryAuditInputError("terminal query outcome is invalid") from exc
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "finished_at", _timestamp(self.finished_at))
        for name in ("duration_ms", "progress_steps", "row_count", "byte_count"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise QueryAuditInputError(f"{name} must be a non-negative integer")
        reason = str(self.termination_reason).strip().lower()
        if not reason or not _PARAMETER_NAME_RE.fullmatch(reason):
            raise QueryAuditInputError("termination_reason must be a safe reason code")
        object.__setattr__(self, "termination_reason", reason)
        if self.compiled_sql is not None:
            sql = self.compiled_sql.strip()
            if not sql or len(sql.encode("utf-8")) > _SQL_MAX_BYTES or "\x00" in sql:
                raise QueryAuditInputError("compiled_sql is invalid")
            if not re.match(r"^(?:SELECT|WITH)\b", sql, re.IGNORECASE):
                raise QueryAuditInputError("compiled_sql must be a scoped SELECT")
            object.__setattr__(self, "compiled_sql", sql)
        if self.parameters is not None and self.compiled_sql is None:
            raise QueryAuditInputError("parameters require compiled_sql")
        if outcome in {QueryAuditOutcome.COMPLETED, QueryAuditOutcome.TRUNCATED}:
            if self.validated_ast is None or self.compiled_sql is None:
                raise QueryAuditInputError("successful outcomes require AST and compiled SQL evidence")
            if self.row_count is None or self.byte_count is None:
                raise QueryAuditInputError("successful outcomes require row and byte counts")


@dataclass(frozen=True, slots=True)
class QueryAuditEvent:
    audit_id: str
    workspace_id: str
    request_id: str
    attempt: int
    event_type: str
    actor_id: str
    role: str
    session_id: str | None
    surface: str
    question_sha256: str
    scope_json: str
    ast_json: str | None
    compiled_sql: str | None
    parameters_json: str | None
    schema_generation: int
    data_generation: int
    catalog_sha256: str
    model_id: str | None
    prompt_version: str | None
    duration_ms: int | None
    progress_steps: int | None
    termination_reason: str | None
    row_count: int | None
    byte_count: int | None
    outcome: str
    created_at: str


QUERY_RESPONSE_AUDIT_VERSION = "query-response-audit-v1"
BRIDGE_QUERY_PLAN_VERSION = "bridge-query-plan-v1"
_RESPONSE_ANSWER_MODES = frozenset(
    {
        "deterministic",
        "verified_narrative",
        "repaired_narrative",
        "deterministic_fallback",
        "abstained",
    }
)
_RESPONSE_VERIFICATION_STATUSES = frozenset(
    {"verified", "fallback", "abstained"}
)
_FORBIDDEN_PLAN_KEYS = frozenset(
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
        "sql",
        "compiled_sql",
        "parameters",
    }
)


@dataclass(frozen=True, slots=True)
class RecordQueryResponseAuditRequest:
    """A response receipt whose content is minimized before persistence.

    ``plan`` may be an already typed :class:`BridgeQueryPlan` or its exact
    canonical payload.  The constructor reparses mappings through the typed
    contract, then removes every filter value.  ``evidence`` is hashed in
    memory; its rows and cells are never included in the stored row.
    """

    workspace_id: str
    request_id: str
    actor_id: str
    surface: str
    plan: BridgeQueryPlan | Mapping[str, object]
    compiled_query_sha256: str
    evidence: EvidenceTable
    answer_verification_version: str
    answer_mode: str
    verification_status: str
    provider_call_count: int
    claim_count: int
    verified_claim_count: int
    rejected_claim_count: int
    fallback_count: int
    schema_generation: int
    data_generation: int
    catalog_version: str
    catalog_sha256: str
    dictionary_version: str
    dictionary_sha256: str
    created_at: datetime | str
    plan_version: str = BRIDGE_QUERY_PLAN_VERSION

    def __post_init__(self) -> None:
        for name in (
            "workspace_id",
            "request_id",
            "actor_id",
            "surface",
            "plan_version",
            "answer_verification_version",
            "catalog_version",
            "dictionary_version",
        ):
            object.__setattr__(self, name, _token(getattr(self, name), name))
        try:
            plan = (
                self.plan
                if isinstance(self.plan, BridgeQueryPlan)
                else BridgeQueryPlan.from_payload(self.plan)
            )
        except (PlanError, TypeError, ValueError) as exc:
            raise QueryAuditInputError("response audit plan is invalid") from exc
        object.__setattr__(self, "plan", plan)
        if not isinstance(self.evidence, EvidenceTable):
            raise QueryAuditInputError("response audit evidence must be an EvidenceTable")
        object.__setattr__(
            self,
            "compiled_query_sha256",
            _sha256(self.compiled_query_sha256, "compiled_query_sha256"),
        )
        for name in ("catalog_sha256", "dictionary_sha256"):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        mode = str(self.answer_mode).strip().lower()
        if mode not in _RESPONSE_ANSWER_MODES:
            raise QueryAuditInputError("answer_mode is not supported")
        object.__setattr__(self, "answer_mode", mode)
        status = str(self.verification_status).strip().lower()
        if status not in _RESPONSE_VERIFICATION_STATUSES:
            raise QueryAuditInputError("verification_status is not supported")
        object.__setattr__(self, "verification_status", status)
        for name in (
            "provider_call_count",
            "claim_count",
            "verified_claim_count",
            "rejected_claim_count",
            "fallback_count",
            "schema_generation",
            "data_generation",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise QueryAuditInputError(f"{name} must be a non-negative integer")
        # One call may produce the typed query plan; a complex verified answer
        # may then use one generation plus one repair.
        if self.provider_call_count > 3:
            raise QueryAuditInputError("provider_call_count exceeds the plan-plus-one-repair policy")
        if self.fallback_count not in (0, 1):
            raise QueryAuditInputError("fallback_count must be zero or one")
        if self.claim_count != self.verified_claim_count + self.rejected_claim_count:
            raise QueryAuditInputError(
                "claim_count must equal verified_claim_count plus rejected_claim_count"
            )
        if status == "verified" and mode == "abstained":
            raise QueryAuditInputError("an abstained answer cannot have verified status")
        if status == "abstained" and mode != "abstained":
            raise QueryAuditInputError("abstained status requires abstained answer_mode")
        if status == "fallback" and self.fallback_count != 1:
            raise QueryAuditInputError("fallback status requires fallback_count=1")
        object.__setattr__(self, "created_at", _timestamp(self.created_at))


@dataclass(frozen=True, slots=True)
class QueryResponseAuditEvent:
    audit_id: str
    workspace_id: str
    request_id: str
    actor_id: str
    surface: str
    plan_version: str
    plan_json: str
    plan_sha256: str
    compiled_query_sha256: str
    evidence_sha256: str
    evidence_row_count: int
    evidence_column_count: int
    evidence_byte_count: int
    evidence_truncated: bool
    answer_verification_version: str
    answer_mode: str
    verification_status: str
    provider_call_count: int
    claim_count: int
    verified_claim_count: int
    rejected_claim_count: int
    fallback_count: int
    schema_generation: int
    data_generation: int
    catalog_version: str
    catalog_sha256: str
    dictionary_version: str
    dictionary_sha256: str
    created_at: str
    record_sha256: str


def question_sha256(question: str) -> str:
    if not isinstance(question, str) or not question.strip():
        raise QueryAuditInputError("question is required")
    return hashlib.sha256(question.encode("utf-8")).hexdigest()


def redact_parameters(parameters: Mapping[str, object] | Sequence[object] | None) -> str | None:
    """Return canonical JSON containing only parameter names/positions and types."""

    if parameters is None:
        return None
    if isinstance(parameters, Mapping):
        entries = []
        for raw_name in sorted(parameters, key=lambda item: str(item)):
            name = str(raw_name)
            if not _PARAMETER_NAME_RE.fullmatch(name):
                raise QueryAuditInputError("parameter name is invalid")
            entries.append({"name": name, "redacted": True, "type": _value_type(parameters[raw_name])})
        return _canonical_json({"kind": "named", "parameters": entries})
    if isinstance(parameters, (str, bytes, bytearray)) or not isinstance(parameters, Sequence):
        raise QueryAuditInputError("parameters must be a mapping or sequence")
    return _canonical_json(
        {
            "kind": "positional",
            "parameters": [
                {"position": index, "redacted": True, "type": _value_type(value)}
                for index, value in enumerate(parameters, start=1)
            ],
        }
    )


def _redacted_filter_value(value: object) -> dict[str, object]:
    if isinstance(value, (tuple, list)):
        item_types = sorted({_plan_value_type(item) for item in value})
        return {
            "item_types": item_types,
            "length": len(value),
            "redacted": True,
            "type": "array",
        }
    return {"redacted": True, "type": _plan_value_type(value)}


def _plan_value_type(value: object) -> str:
    # Dates are permitted by the typed plan API and become ISO strings during
    # canonicalization.  At this boundary their literal text is discarded.
    if isinstance(value, (date, datetime)):
        return "date"
    return _value_type(value)


def _privacy_minimized_plan(plan: BridgeQueryPlan) -> tuple[str, str]:
    payload = plan.canonical_payload()
    filters = payload.get("filters")
    if not isinstance(filters, list):
        raise QueryAuditInputError("canonical plan filters are invalid")
    payload["filters"] = [
        {
            "field_id": item["field_id"],
            "operator": item["operator"],
            "value": _redacted_filter_value(source.value),
        }
        for item, source in zip(filters, plan.filters, strict=True)
        if isinstance(item, Mapping)
    ]
    if len(payload["filters"]) != len(filters):
        raise QueryAuditInputError("canonical plan filters are invalid")
    plan_json = _canonical_json(payload)
    return plan_json, hashlib.sha256(plan_json.encode("utf-8")).hexdigest()


def _evidence_receipt(evidence: EvidenceTable) -> tuple[str, int, int, int, bool]:
    payload = {
        "columns": [
            {
                "column_id": column.column_id,
                "label": column.label,
                "unit": column.unit,
            }
            for column in evidence.columns
        ],
        "rows": [list(row) for row in evidence.rows],
        "truncated": bool(evidence.truncated),
    }
    serialized = _canonical_json(payload).encode("utf-8")
    if len(serialized) > MAX_EVIDENCE_BYTES:
        raise QueryAuditInputError("response audit evidence exceeds the bounded byte limit")
    return (
        hashlib.sha256(serialized).hexdigest(),
        len(evidence.rows),
        len(evidence.columns),
        len(serialized),
        bool(evidence.truncated),
    )


def _response_record_payload(event: QueryResponseAuditEvent) -> dict[str, object]:
    return {
        "actor_id": event.actor_id,
        "answer_mode": event.answer_mode,
        "answer_verification_version": event.answer_verification_version,
        "audit_id": event.audit_id,
        "catalog_sha256": event.catalog_sha256,
        "catalog_version": event.catalog_version,
        "claim_count": event.claim_count,
        "compiled_query_sha256": event.compiled_query_sha256,
        "created_at": event.created_at,
        "data_generation": event.data_generation,
        "dictionary_sha256": event.dictionary_sha256,
        "dictionary_version": event.dictionary_version,
        "evidence_byte_count": event.evidence_byte_count,
        "evidence_column_count": event.evidence_column_count,
        "evidence_row_count": event.evidence_row_count,
        "evidence_sha256": event.evidence_sha256,
        "evidence_truncated": event.evidence_truncated,
        "fallback_count": event.fallback_count,
        "plan_json": event.plan_json,
        "plan_sha256": event.plan_sha256,
        "plan_version": event.plan_version,
        "provider_call_count": event.provider_call_count,
        "rejected_claim_count": event.rejected_claim_count,
        "request_id": event.request_id,
        "schema_generation": event.schema_generation,
        "surface": event.surface,
        "verification_status": event.verification_status,
        "verified_claim_count": event.verified_claim_count,
        "workspace_id": event.workspace_id,
    }


def _response_record_sha256(event: QueryResponseAuditEvent) -> str:
    return hashlib.sha256(
        _canonical_json(_response_record_payload(event)).encode("utf-8")
    ).hexdigest()


def record_query_response_audit(
    database_path: str | os.PathLike[str],
    request: RecordQueryResponseAuditRequest,
) -> QueryResponseAuditEvent:
    """Persist one immutable response receipt or fail closed.

    No question, answer text, SQL, parameter value, evidence row, result path,
    or server-owned scope value is placed in ``query_response_audit``.
    """

    plan_json, plan_sha256 = _privacy_minimized_plan(request.plan)
    (
        evidence_sha256,
        evidence_row_count,
        evidence_column_count,
        evidence_byte_count,
        evidence_truncated,
    ) = _evidence_receipt(request.evidence)
    audit_id = _response_event_id(request.workspace_id, request.request_id)
    unsealed = QueryResponseAuditEvent(
        audit_id=audit_id,
        workspace_id=request.workspace_id,
        request_id=request.request_id,
        actor_id=request.actor_id,
        surface=request.surface,
        plan_version=request.plan_version,
        plan_json=plan_json,
        plan_sha256=plan_sha256,
        compiled_query_sha256=request.compiled_query_sha256,
        evidence_sha256=evidence_sha256,
        evidence_row_count=evidence_row_count,
        evidence_column_count=evidence_column_count,
        evidence_byte_count=evidence_byte_count,
        evidence_truncated=evidence_truncated,
        answer_verification_version=request.answer_verification_version,
        answer_mode=request.answer_mode,
        verification_status=request.verification_status,
        provider_call_count=request.provider_call_count,
        claim_count=request.claim_count,
        verified_claim_count=request.verified_claim_count,
        rejected_claim_count=request.rejected_claim_count,
        fallback_count=request.fallback_count,
        schema_generation=request.schema_generation,
        data_generation=request.data_generation,
        catalog_version=request.catalog_version,
        catalog_sha256=request.catalog_sha256,
        dictionary_version=request.dictionary_version,
        dictionary_sha256=request.dictionary_sha256,
        created_at=str(request.created_at),
        record_sha256="",
    )
    unsealed_values = {
        field: getattr(unsealed, field)
        for field in unsealed.__dataclass_fields__
        if field != "record_sha256"
    }
    event = QueryResponseAuditEvent(
        **unsealed_values,
        record_sha256=_response_record_sha256(unsealed),
    )
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(database_path)
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT 1 FROM query_response_audit WHERE workspace_id=? AND request_id=?",
            (event.workspace_id, event.request_id),
        ).fetchone()
        if existing is not None:
            raise QueryResponseAuditExists("query response audit already exists")
        values = (
            event.audit_id,
            event.workspace_id,
            event.request_id,
            event.actor_id,
            event.surface,
            event.plan_version,
            event.plan_json,
            event.plan_sha256,
            event.compiled_query_sha256,
            event.evidence_sha256,
            event.evidence_row_count,
            event.evidence_column_count,
            event.evidence_byte_count,
            int(event.evidence_truncated),
            event.answer_verification_version,
            event.answer_mode,
            event.verification_status,
            event.provider_call_count,
            event.claim_count,
            event.verified_claim_count,
            event.rejected_claim_count,
            event.fallback_count,
            event.schema_generation,
            event.data_generation,
            event.catalog_version,
            event.catalog_sha256,
            event.dictionary_version,
            event.dictionary_sha256,
            event.created_at,
            event.record_sha256,
        )
        connection.execute(
            "INSERT INTO query_response_audit (id,workspace_id,request_id,actor_id,"
            "surface,plan_version,plan_json,plan_sha256,compiled_query_sha256,"
            "evidence_sha256,evidence_row_count,evidence_column_count,evidence_byte_count,"
            "evidence_truncated,answer_verification_version,answer_mode,verification_status,"
            "provider_call_count,claim_count,verified_claim_count,rejected_claim_count,"
            "fallback_count,schema_generation,data_generation,catalog_version,catalog_sha256,"
            "dictionary_version,dictionary_sha256,created_at,record_sha256) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        connection.commit()
        return event
    except QueryAuditStateError:
        if connection is not None:
            connection.rollback()
        raise
    except (OSError, sqlite3.Error, QueryAuditDatabaseError) as exc:
        if connection is not None:
            connection.rollback()
        raise QueryResponseAuditWriteError(
            "The response was not released because its audit receipt could not be recorded."
        ) from exc
    finally:
        if connection is not None:
            connection.close()


def list_query_response_audit_events(
    database_path: str | os.PathLike[str],
    workspace_id: str,
    *,
    request_id: str | None = None,
) -> tuple[QueryResponseAuditEvent, ...]:
    """List and checksum-verify receipts within one authorized workspace."""

    workspace = _token(workspace_id, "workspace_id")
    request = _token(request_id, "request_id") if request_id is not None else None
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(database_path)
        sql = "SELECT * FROM query_response_audit WHERE workspace_id=?"
        parameters: tuple[object, ...] = (workspace,)
        if request is not None:
            sql += " AND request_id=?"
            parameters += (request,)
        sql += " ORDER BY created_at,id"
        events = tuple(
            _response_event_from_row(row)
            for row in connection.execute(sql, parameters)
        )
        for event in events:
            _verify_response_event(event)
        return events
    except QueryAuditStateError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise QueryAuditDatabaseError("Query response audit evidence is unavailable.") from exc
    finally:
        if connection is not None:
            connection.close()


def accept_query_attempt(
    database_path: str | os.PathLike[str], request: AcceptQueryAuditRequest
) -> QueryAuditHandle:
    """Persist the mandatory pre-execution event or fail closed."""

    question_hash = question_sha256(request.question)
    scope_json = _canonical_json(request.scope.canonical_payload())
    audit_id = _event_id(request.workspace_id, request.request_id, request.attempt, "accepted")
    handle = QueryAuditHandle(
        audit_id=audit_id,
        workspace_id=request.workspace_id,
        request_id=request.request_id,
        attempt=request.attempt,
        actor_id=request.actor_id,
        role=request.role,
        session_id=request.session_id,
        surface=request.surface,
        question_sha256=question_hash,
        scope_json=scope_json,
        schema_generation=request.schema_generation,
        data_generation=request.data_generation,
        catalog_sha256=request.catalog_sha256,
        model_id=request.model_id,
        prompt_version=request.prompt_version,
        accepted_at=str(request.accepted_at),
    )
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(database_path)
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT event_type FROM query_audit WHERE workspace_id=? AND request_id=? AND attempt=? LIMIT 1",
            (handle.workspace_id, handle.request_id, handle.attempt),
        ).fetchone()
        if existing is not None:
            raise QueryAuditAttemptExists("query attempt already has audit evidence")
        connection.execute(
            "INSERT INTO query_audit (id,workspace_id,request_id,attempt,event_type,"
            "actor_id,role,session_id,surface,question_sha256,scope_json,"
            "schema_generation,data_generation,catalog_sha256,model_id,prompt_version,"
            "outcome,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                handle.audit_id, handle.workspace_id, handle.request_id, handle.attempt,
                "accepted", handle.actor_id, handle.role, handle.session_id, handle.surface,
                handle.question_sha256, handle.scope_json, handle.schema_generation,
                handle.data_generation, handle.catalog_sha256, handle.model_id,
                handle.prompt_version, "accepted", handle.accepted_at,
            ),
        )
        connection.commit()
        return handle
    except QueryAuditStateError:
        if connection is not None:
            connection.rollback()
        raise
    except (OSError, sqlite3.Error, QueryAuditDatabaseError) as exc:
        if connection is not None:
            connection.rollback()
        raise AcceptedAuditWriteError("The query was not run because its audit acceptance could not be recorded.") from exc
    finally:
        if connection is not None:
            connection.close()


def finish_query_attempt(
    database_path: str | os.PathLike[str], request: FinishQueryAuditRequest
) -> QueryAuditEvent:
    """Persist exactly one terminal event for a previously accepted attempt."""

    handle = request.handle
    ast_json = (
        _canonical_json(request.validated_ast.canonical_payload())
        if request.validated_ast is not None
        else None
    )
    parameters_json = redact_parameters(request.parameters)
    audit_id = _event_id(handle.workspace_id, handle.request_id, handle.attempt, "terminal")
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(database_path)
        connection.execute("BEGIN IMMEDIATE")
        accepted = connection.execute(
            "SELECT * FROM query_audit WHERE workspace_id=? AND request_id=? "
            "AND attempt=? AND event_type='accepted'",
            (handle.workspace_id, handle.request_id, handle.attempt),
        ).fetchone()
        if accepted is None:
            raise QueryAuditAcceptedMissing("accepted query audit evidence was not found")
        _assert_handle_matches(accepted, handle)
        terminal = connection.execute(
            "SELECT 1 FROM query_audit WHERE workspace_id=? AND request_id=? "
            "AND attempt=? AND event_type='terminal'",
            (handle.workspace_id, handle.request_id, handle.attempt),
        ).fetchone()
        if terminal is not None:
            raise QueryAuditDuplicateTerminal("query attempt already has a terminal event")
        values = (
            audit_id, handle.workspace_id, handle.request_id, handle.attempt, "terminal",
            handle.actor_id, handle.role, handle.session_id, handle.surface,
            handle.question_sha256, handle.scope_json, ast_json, request.compiled_sql,
            parameters_json, handle.schema_generation, handle.data_generation,
            handle.catalog_sha256, handle.model_id, handle.prompt_version,
            request.duration_ms, request.progress_steps, request.termination_reason,
            request.row_count, request.byte_count, request.outcome.value,
            str(request.finished_at),
        )
        connection.execute(
            "INSERT INTO query_audit (id,workspace_id,request_id,attempt,event_type,"
            "actor_id,role,session_id,surface,question_sha256,scope_json,ast_json,"
            "compiled_sql,parameters_json,schema_generation,data_generation,catalog_sha256,"
            "model_id,prompt_version,duration_ms,progress_steps,termination_reason,row_count,"
            "byte_count,outcome,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        connection.commit()
        return QueryAuditEvent(*values)
    except QueryAuditStateError:
        if connection is not None:
            connection.rollback()
        raise
    except (OSError, sqlite3.Error, QueryAuditDatabaseError) as exc:
        if connection is not None:
            connection.rollback()
        raise TerminalAuditWriteError("The terminal query audit event could not be recorded.") from exc
    finally:
        if connection is not None:
            connection.close()


def list_query_audit_events(
    database_path: str | os.PathLike[str],
    workspace_id: str,
    *,
    request_id: str | None = None,
) -> tuple[QueryAuditEvent, ...]:
    """Read audit metadata without exposing questions, values, rows, or paths."""

    workspace = _token(workspace_id, "workspace_id")
    request = _token(request_id, "request_id") if request_id is not None else None
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(database_path)
        sql = "SELECT * FROM query_audit WHERE workspace_id=?"
        parameters: tuple[object, ...] = (workspace,)
        if request is not None:
            sql += " AND request_id=?"
            parameters += (request,)
        sql += " ORDER BY request_id,attempt,CASE event_type WHEN 'accepted' THEN 0 ELSE 1 END,id"
        return tuple(_event_from_row(row) for row in connection.execute(sql, parameters))
    except (OSError, sqlite3.Error) as exc:
        raise QueryAuditDatabaseError("Query audit evidence is unavailable.") from exc
    finally:
        if connection is not None:
            connection.close()


def _assert_handle_matches(row: sqlite3.Row, handle: QueryAuditHandle) -> None:
    expected = {
        "id": handle.audit_id,
        "actor_id": handle.actor_id,
        "role": handle.role,
        "session_id": handle.session_id,
        "surface": handle.surface,
        "question_sha256": handle.question_sha256,
        "scope_json": handle.scope_json,
        "schema_generation": handle.schema_generation,
        "data_generation": handle.data_generation,
        "catalog_sha256": handle.catalog_sha256,
        "model_id": handle.model_id,
        "prompt_version": handle.prompt_version,
        "created_at": handle.accepted_at,
    }
    if any(row[key] != value for key, value in expected.items()):
        raise QueryAuditStateError("accepted query audit evidence does not match its handle")


def _event_from_row(row: sqlite3.Row) -> QueryAuditEvent:
    return QueryAuditEvent(
        audit_id=str(row["id"]), workspace_id=str(row["workspace_id"]),
        request_id=str(row["request_id"]), attempt=int(row["attempt"]),
        event_type=str(row["event_type"]), actor_id=str(row["actor_id"]),
        role=str(row["role"]), session_id=row["session_id"], surface=str(row["surface"]),
        question_sha256=str(row["question_sha256"]), scope_json=str(row["scope_json"]),
        ast_json=row["ast_json"], compiled_sql=row["compiled_sql"],
        parameters_json=row["parameters_json"], schema_generation=int(row["schema_generation"]),
        data_generation=int(row["data_generation"]), catalog_sha256=str(row["catalog_sha256"]),
        model_id=row["model_id"], prompt_version=row["prompt_version"],
        duration_ms=row["duration_ms"], progress_steps=row["progress_steps"],
        termination_reason=row["termination_reason"], row_count=row["row_count"],
        byte_count=row["byte_count"], outcome=str(row["outcome"]), created_at=str(row["created_at"]),
    )


def _response_event_from_row(row: sqlite3.Row) -> QueryResponseAuditEvent:
    return QueryResponseAuditEvent(
        audit_id=str(row["id"]),
        workspace_id=str(row["workspace_id"]),
        request_id=str(row["request_id"]),
        actor_id=str(row["actor_id"]),
        surface=str(row["surface"]),
        plan_version=str(row["plan_version"]),
        plan_json=str(row["plan_json"]),
        plan_sha256=str(row["plan_sha256"]),
        compiled_query_sha256=str(row["compiled_query_sha256"]),
        evidence_sha256=str(row["evidence_sha256"]),
        evidence_row_count=int(row["evidence_row_count"]),
        evidence_column_count=int(row["evidence_column_count"]),
        evidence_byte_count=int(row["evidence_byte_count"]),
        evidence_truncated=bool(row["evidence_truncated"]),
        answer_verification_version=str(row["answer_verification_version"]),
        answer_mode=str(row["answer_mode"]),
        verification_status=str(row["verification_status"]),
        provider_call_count=int(row["provider_call_count"]),
        claim_count=int(row["claim_count"]),
        verified_claim_count=int(row["verified_claim_count"]),
        rejected_claim_count=int(row["rejected_claim_count"]),
        fallback_count=int(row["fallback_count"]),
        schema_generation=int(row["schema_generation"]),
        data_generation=int(row["data_generation"]),
        catalog_version=str(row["catalog_version"]),
        catalog_sha256=str(row["catalog_sha256"]),
        dictionary_version=str(row["dictionary_version"]),
        dictionary_sha256=str(row["dictionary_sha256"]),
        created_at=str(row["created_at"]),
        record_sha256=str(row["record_sha256"]),
    )


def _verify_response_event(event: QueryResponseAuditEvent) -> None:
    if not _SHA256_RE.fullmatch(event.plan_sha256) or not _SHA256_RE.fullmatch(
        event.record_sha256
    ):
        raise QueryAuditStateError("query response audit checksum is invalid")
    if hashlib.sha256(event.plan_json.encode("utf-8")).hexdigest() != event.plan_sha256:
        raise QueryAuditStateError("query response audit plan checksum mismatch")
    try:
        plan_payload = json.loads(event.plan_json)
    except json.JSONDecodeError as exc:
        raise QueryAuditStateError("query response audit plan is not canonical JSON") from exc
    if _canonical_json(plan_payload) != event.plan_json:
        raise QueryAuditStateError("query response audit plan is not canonical JSON")
    if _contains_forbidden_plan_key(plan_payload):
        raise QueryAuditStateError("query response audit plan contains private scope")
    if _response_record_sha256(event) != event.record_sha256:
        raise QueryAuditStateError("query response audit record checksum mismatch")


def _contains_forbidden_plan_key(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).casefold() in _FORBIDDEN_PLAN_KEYS
            or _contains_forbidden_plan_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_plan_key(item) for item in value)
    return False


def _connect(database_path: str | os.PathLike[str]) -> sqlite3.Connection:
    try:
        path = Path(os.fspath(database_path)).resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise QueryAuditDatabaseError("Query audit database is unavailable.") from exc
    if not path.is_file():
        raise QueryAuditDatabaseError("Query audit database is unavailable.")
    connection = sqlite3.connect(str(path), timeout=3.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=3000")
    return connection


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise QueryAuditInputError("audit evidence is not canonical JSON") from exc


def _timestamp(value: datetime | str) -> str:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise QueryAuditInputError("audit timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QueryAuditInputError("audit timestamp must include a timezone")
    utc = parsed.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _token(value: object, label: str) -> str:
    text = str(value).strip()
    if not _TOKEN_RE.fullmatch(text):
        raise QueryAuditInputError(f"{label} is invalid")
    return text


def _sha256(value: object, label: str) -> str:
    text = str(value).strip().lower()
    if not _SHA256_RE.fullmatch(text):
        raise QueryAuditInputError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _value_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise QueryAuditInputError("non-finite query parameters are not accepted")
        return "number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "bytes"
    raise QueryAuditInputError("query parameter type is not supported")


def _event_id(workspace_id: str, request_id: str, attempt: int, event_type: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"bridgeguard:query-audit:v1:{workspace_id}:{request_id}:{attempt}:{event_type}"))


def _response_event_id(workspace_id: str, request_id: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"bridgeguard:query-response-audit:v1:{workspace_id}:{request_id}",
        )
    )


__all__ = [
    "AcceptQueryAuditRequest", "AcceptedAuditWriteError", "FinishQueryAuditRequest",
    "QueryAuditAcceptedMissing", "QueryAuditAttemptExists", "QueryAuditDatabaseError",
    "QueryAuditDuplicateTerminal", "QueryAuditError", "QueryAuditEvent", "QueryAuditHandle",
    "QueryAuditInputError", "QueryAuditOutcome", "QueryAuditScope", "QueryAuditStateError",
    "QueryResponseAuditEvent", "QueryResponseAuditExists", "QueryResponseAuditWriteError",
    "RecordQueryResponseAuditRequest", "TerminalAuditWriteError", "ValidatedAstAudit",
    "accept_query_attempt", "finish_query_attempt", "list_query_audit_events",
    "list_query_response_audit_events", "question_sha256", "record_query_response_audit",
    "redact_parameters",
]
