"""Typed, deterministic BridgeQueryPlan validation and SQL compilation.

The model-facing plan contains only reviewed semantic identifiers and typed
filter values.  It cannot carry workspace, state, bridge, year, SQL, table, or
parameter overrides.  Server-owned :class:`~.sql_ast.QueryScope` is supplied
only to :func:`compile_bridge_query_plan`, after the plan has been validated.

Compilation deliberately produces one logical SELECT and then sends that SQL
through the independent generated-SQL validator and scoped compiler.  This
keeps the plan compiler from becoming a bypass around the PR7 SQL boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .metrics import DEFAULT_METRIC_REGISTRY
from .models import (
    JoinCardinality,
    MetricDefinition,
    MetricRegistry,
    MetricStatus,
    ResourceGrain,
    SemanticCatalog,
    SemanticColumn,
    SemanticDataType,
    SemanticJoin,
    SemanticResource,
)
from .sql_ast import (
    CatalogTable,
    CompiledSql,
    QueryScope,
    SqlCatalog,
    SqlLimits,
    TableTimeSemantics,
    TimeMode,
    ValidatedSql,
    compile_scoped_sql,
    validate_generated_sql,
)


_SEMANTIC_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_METRIC_ID = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_ROOT_FIELDS = frozenset(
    {
        "intent",
        "resource_id",
        "metric_ids",
        "selected_fields",
        "dimensions",
        "filters",
        "time_scope",
        "order",
        "limit",
    }
)
_FORBIDDEN_SCOPE_KEYS = frozenset(
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
_SERVER_SCOPE_FIELDS = frozenset(
    {
        "workspace_id",
        "state_code",
        "bridge_number",
        "data_year",
        "snapshot_revision_id",
    }
)
_TEXT_WILDCARDS = frozenset({"%", "_", "*", "?"})


class PlanIntent(str, Enum):
    COUNT = "count"
    LIST = "list"
    AGGREGATE = "aggregate"
    TREND = "trend"
    LOOKUP = "lookup"
    COMPARE = "compare"


class PlanOperator(str, Enum):
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    IN = "in"
    NOT_IN = "not_in"
    BETWEEN = "between"
    CONTAINS = "contains"
    STARTS_WITH = "starts_with"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


class OrderDirection(str, Enum):
    ASC = "asc"
    DESC = "desc"


class PlanError(ValueError):
    """Typed plan parse, validation, or compilation failure."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = MappingProxyType(dict(details))

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }


class PlanParseError(PlanError):
    """The model payload does not conform to the BridgeQueryPlan shape."""


class PlanValidationError(PlanError):
    """The typed plan is not valid for the reviewed catalog or registry."""


class PlanCompilationError(PlanError):
    """A valid logical plan cannot be compiled without weakening scope."""


@dataclass(frozen=True, slots=True)
class PlanLimits:
    max_metrics: int = 8
    max_selected_fields: int = 40
    max_dimensions: int = 4
    max_filters: int = 12
    max_order: int = 4
    max_in_values: int = 50
    max_text_chars: int = 256
    max_text_bytes: int = 1_024
    max_numeric_magnitude: float = 1_000_000_000_000_000.0
    max_limit: int = 200
    max_lookup_limit: int = 20

    def __post_init__(self) -> None:
        for name in (
            "max_metrics",
            "max_selected_fields",
            "max_dimensions",
            "max_filters",
            "max_order",
            "max_in_values",
            "max_text_chars",
            "max_text_bytes",
            "max_limit",
            "max_lookup_limit",
        ):
            if isinstance(getattr(self, name), bool) or int(getattr(self, name)) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not math.isfinite(self.max_numeric_magnitude) or self.max_numeric_magnitude <= 0:
            raise ValueError("max_numeric_magnitude must be finite and positive")
        if self.max_lookup_limit > self.max_limit:
            raise ValueError("max_lookup_limit cannot exceed max_limit")


def _enum_value(enum_type: type[Enum], value: object, field_name: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        raise PlanParseError(
            "invalid_plan_type", f"{field_name} must be a string", field=field_name
        )
    try:
        return enum_type(value.strip().lower())
    except ValueError as exc:
        raise PlanParseError(
            "invalid_plan_enum",
            f"Unsupported {field_name}",
            field=field_name,
            value=value,
            allowed=[item.value for item in enum_type],
        ) from exc


def _identifier(value: object, field_name: str, *, metric: bool = False) -> str:
    if not isinstance(value, str):
        raise PlanParseError(
            "invalid_plan_type", f"{field_name} must be a string", field=field_name
        )
    text = value.strip()
    pattern = _METRIC_ID if metric else _SEMANTIC_ID
    if not text or not pattern.fullmatch(text):
        raise PlanParseError(
            "invalid_identifier",
            f"{field_name} must be a lowercase semantic identifier",
            field=field_name,
            value=value,
        )
    return text


def _field_reference(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise PlanParseError(
            "invalid_plan_type", f"{field_name} must be a string", field=field_name
        )
    text = value.strip()
    pieces = text.split(".")
    if len(pieces) not in (1, 2) or any(not _SEMANTIC_ID.fullmatch(piece) for piece in pieces):
        raise PlanParseError(
            "invalid_field_reference",
            f"{field_name} must be field_id or resource_id.field_id",
            field=field_name,
            value=value,
        )
    if len(pieces) == 2 and pieces[0] in {
        "main",
        "temp",
        "sqlite_master",
        "sqlite_schema",
    }:
        raise PlanParseError(
            "invalid_field_reference",
            f"{field_name} cannot use a database schema qualifier",
            field=field_name,
            value=value,
        )
    return text


def _sequence(value: object, field_name: str) -> tuple[object, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise PlanParseError(
            "invalid_plan_type", f"{field_name} must be an array", field=field_name
        )
    return tuple(value)


def _strict_keys(
    payload: Mapping[str, object], allowed: frozenset[str], context: str
) -> None:
    supplied = frozenset(payload)
    scope_keys = sorted(supplied.intersection(_FORBIDDEN_SCOPE_KEYS))
    if scope_keys:
        raise PlanParseError(
            "client_scope_override",
            "Workspace, state, bridge, revision, and year scope are server-owned",
            context=context,
            keys=scope_keys,
        )
    unknown = sorted(supplied.difference(allowed))
    if unknown:
        raise PlanParseError(
            "unknown_plan_field",
            "Plan contains unrecognized fields",
            context=context,
            fields=unknown,
        )


def _freeze_filter_value(value: object) -> object:
    if isinstance(value, list):
        return tuple(_freeze_filter_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_filter_value(item) for item in value)
    if isinstance(value, Mapping):
        raise PlanParseError(
            "invalid_filter_value", "Filter values cannot contain objects"
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise PlanParseError(
            "invalid_filter_value", "Filter values cannot contain NaN or infinity"
        )
    if not isinstance(value, (str, int, float, bool, type(None))):
        raise PlanParseError(
            "invalid_filter_value",
            "Filter values must be JSON scalars or arrays of scalars",
            value_type=type(value).__name__,
        )
    return value


def _json_filter_value(value: object) -> object:
    if isinstance(value, tuple):
        return [_json_filter_value(item) for item in value]
    return value


def _deep_freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _deep_freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze_json(item) for item in value)
    return value


def _deep_thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _deep_thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class PlanTimeScope:
    mode: TimeMode | str

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", _enum_value(TimeMode, self.mode, "time_scope.mode"))

    @classmethod
    def from_payload(cls, payload: object) -> "PlanTimeScope":
        if not isinstance(payload, Mapping):
            raise PlanParseError(
                "invalid_plan_type", "time_scope must be an object containing only mode"
            )
        _strict_keys(payload, frozenset({"mode"}), "time_scope")
        if "mode" not in payload:
            raise PlanParseError("missing_plan_field", "time_scope.mode is required")
        return cls(mode=payload["mode"])

    def canonical_payload(self) -> dict[str, str]:
        return {"mode": self.mode.value}


@dataclass(frozen=True, slots=True)
class PlanFilter:
    field_id: str
    operator: PlanOperator | str
    value: object = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "field_id", _field_reference(self.field_id, "filter.field_id"))
        object.__setattr__(self, "operator", _enum_value(PlanOperator, self.operator, "filter.operator"))
        object.__setattr__(self, "value", _freeze_filter_value(self.value))

    @classmethod
    def from_payload(cls, payload: object) -> "PlanFilter":
        if not isinstance(payload, Mapping):
            raise PlanParseError("invalid_plan_type", "Each filter must be an object")
        _strict_keys(payload, frozenset({"field_id", "operator", "value"}), "filter")
        if "field_id" not in payload or "operator" not in payload:
            raise PlanParseError(
                "missing_plan_field", "Every filter requires field_id and operator"
            )
        return cls(
            field_id=payload["field_id"],
            operator=payload["operator"],
            value=payload.get("value"),
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "field_id": self.field_id,
            "operator": self.operator.value,
            "value": _json_filter_value(self.value),
        }


@dataclass(frozen=True, slots=True)
class PlanOrder:
    by: str
    direction: OrderDirection | str = OrderDirection.ASC

    def __post_init__(self) -> None:
        if not isinstance(self.by, str) or not self.by.strip():
            raise PlanParseError("invalid_order", "order.by must be a non-blank string")
        by = self.by.strip()
        if not (_METRIC_ID.fullmatch(by) or all(_SEMANTIC_ID.fullmatch(piece) for piece in by.split(".")) and len(by.split(".")) in (1, 2)):
            raise PlanParseError(
                "invalid_order", "order.by must name an output field or metric", by=by
            )
        object.__setattr__(self, "by", by)
        object.__setattr__(self, "direction", _enum_value(OrderDirection, self.direction, "order.direction"))

    @classmethod
    def from_payload(cls, payload: object) -> "PlanOrder":
        if not isinstance(payload, Mapping):
            raise PlanParseError("invalid_plan_type", "Each order item must be an object")
        _strict_keys(payload, frozenset({"by", "direction"}), "order")
        if "by" not in payload:
            raise PlanParseError("missing_plan_field", "Every order item requires by")
        return cls(by=payload["by"], direction=payload.get("direction", "asc"))

    def canonical_payload(self) -> dict[str, str]:
        return {"by": self.by, "direction": self.direction.value}


@dataclass(frozen=True, slots=True)
class BridgeQueryPlan:
    intent: PlanIntent | str
    resource_id: str
    metric_ids: tuple[str, ...] | Sequence[str] = ()
    selected_fields: tuple[str, ...] | Sequence[str] = ()
    dimensions: tuple[str, ...] | Sequence[str] = ()
    filters: tuple[PlanFilter, ...] | Sequence[PlanFilter] = ()
    time_scope: PlanTimeScope | Mapping[str, object] = field(
        default_factory=lambda: PlanTimeScope(TimeMode.LATEST)
    )
    order: tuple[PlanOrder, ...] | Sequence[PlanOrder] = ()
    limit: int = 50

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent", _enum_value(PlanIntent, self.intent, "intent"))
        object.__setattr__(self, "resource_id", _identifier(self.resource_id, "resource_id"))
        metric_ids = tuple(
            _identifier(value, "metric_ids", metric=True)
            for value in _sequence(self.metric_ids, "metric_ids")
        )
        selected_fields = tuple(
            _field_reference(value, "selected_fields")
            for value in _sequence(self.selected_fields, "selected_fields")
        )
        dimensions = tuple(
            _field_reference(value, "dimensions")
            for value in _sequence(self.dimensions, "dimensions")
        )
        filters = tuple(
            value if isinstance(value, PlanFilter) else PlanFilter.from_payload(value)
            for value in _sequence(self.filters, "filters")
        )
        order = tuple(
            value if isinstance(value, PlanOrder) else PlanOrder.from_payload(value)
            for value in _sequence(self.order, "order")
        )
        if isinstance(self.time_scope, PlanTimeScope):
            time_scope = self.time_scope
        else:
            time_scope = PlanTimeScope.from_payload(self.time_scope)
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise PlanParseError("invalid_limit", "limit must be an integer")
        object.__setattr__(self, "metric_ids", metric_ids)
        object.__setattr__(self, "selected_fields", selected_fields)
        object.__setattr__(self, "dimensions", dimensions)
        object.__setattr__(self, "filters", filters)
        object.__setattr__(self, "time_scope", time_scope)
        object.__setattr__(self, "order", order)

    @classmethod
    def from_payload(cls, payload: object) -> "BridgeQueryPlan":
        if not isinstance(payload, Mapping):
            raise PlanParseError("invalid_plan_type", "BridgeQueryPlan must be an object")
        _strict_keys(payload, _ROOT_FIELDS, "plan")
        required = {"intent", "resource_id", "time_scope"}
        missing = sorted(required.difference(payload))
        if missing:
            raise PlanParseError(
                "missing_plan_field", "BridgeQueryPlan is missing required fields", fields=missing
            )
        return cls(
            intent=payload["intent"],
            resource_id=payload["resource_id"],
            metric_ids=_sequence(payload.get("metric_ids"), "metric_ids"),
            selected_fields=_sequence(payload.get("selected_fields"), "selected_fields"),
            dimensions=_sequence(payload.get("dimensions"), "dimensions"),
            filters=tuple(
                PlanFilter.from_payload(item)
                for item in _sequence(payload.get("filters"), "filters")
            ),
            time_scope=PlanTimeScope.from_payload(payload["time_scope"]),
            order=tuple(
                PlanOrder.from_payload(item)
                for item in _sequence(payload.get("order"), "order")
            ),
            limit=payload.get("limit", 50),
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "dimensions": list(self.dimensions),
            "filters": [item.canonical_payload() for item in self.filters],
            "intent": self.intent.value,
            "limit": self.limit,
            "metric_ids": list(self.metric_ids),
            "order": [item.canonical_payload() for item in self.order],
            "resource_id": self.resource_id,
            "selected_fields": list(self.selected_fields),
            "time_scope": self.time_scope.canonical_payload(),
        }

    @property
    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def plan_hash(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ResolvedField:
    resource_id: str
    field_id: str
    column: SemanticColumn

    @property
    def qualified_id(self) -> str:
        return f"{self.resource_id}.{self.field_id}"


@dataclass(frozen=True, slots=True)
class ValidatedBridgeQueryPlan:
    plan: BridgeQueryPlan
    resource: SemanticResource
    metrics: tuple[MetricDefinition, ...]
    selected_fields: tuple[ResolvedField, ...]
    dimensions: tuple[ResolvedField, ...]
    filters: tuple[tuple[PlanFilter, ResolvedField, object], ...]
    order: tuple[PlanOrder, ...]
    joined_resources: tuple[SemanticResource, ...]
    joins: tuple[SemanticJoin, ...]
    output_aliases: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_aliases", MappingProxyType(dict(self.output_aliases)))


@dataclass(frozen=True, slots=True)
class PlanEvidence:
    plan_hash: str
    catalog_hash: str
    metric_registry_hash: str
    canonical_plan: Mapping[str, object]
    intent: str
    resource_id: str
    time_mode: str
    scope: Mapping[str, object]
    outputs: tuple[Mapping[str, object], ...]
    joins: tuple[str, ...]
    logical_sql: str
    compiled_tables: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "canonical_plan", _deep_freeze_json(self.canonical_plan))
        object.__setattr__(self, "scope", _deep_freeze_json(self.scope))
        object.__setattr__(
            self,
            "outputs",
            tuple(_deep_freeze_json(item) for item in self.outputs),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "plan_hash": self.plan_hash,
            "catalog_hash": self.catalog_hash,
            "metric_registry_hash": self.metric_registry_hash,
            "canonical_plan": _deep_thaw_json(self.canonical_plan),
            "intent": self.intent,
            "resource_id": self.resource_id,
            "time_mode": self.time_mode,
            "scope": _deep_thaw_json(self.scope),
            "outputs": [_deep_thaw_json(item) for item in self.outputs],
            "joins": list(self.joins),
            "logical_sql": self.logical_sql,
            "compiled_tables": list(self.compiled_tables),
        }


@dataclass(frozen=True, slots=True)
class CompiledBridgeQueryPlan:
    plan: BridgeQueryPlan
    validated_plan: ValidatedBridgeQueryPlan
    logical_sql: str
    validated_sql: ValidatedSql
    compiled_sql: CompiledSql
    evidence: PlanEvidence


def _qualified(reference: str, base_resource_id: str) -> tuple[str, str]:
    if "." in reference:
        resource_id, field_id = reference.split(".", 1)
    else:
        resource_id, field_id = base_resource_id, reference
    return resource_id, field_id


def _resolve_field(
    reference: str,
    base_resource_id: str,
    catalog: SemanticCatalog,
    *,
    context: str,
) -> ResolvedField:
    resource_id, field_id = _qualified(reference, base_resource_id)
    resource = catalog.resources_by_id.get(resource_id)
    if resource is None:
        raise PlanValidationError(
            "unknown_resource", "Field references an unknown resource", resource_id=resource_id, context=context
        )
    column = resource.columns_by_id.get(field_id)
    if column is None:
        raise PlanValidationError(
            "unknown_field",
            "Field is not in the reviewed semantic catalog",
            resource_id=resource_id,
            field_id=field_id,
            context=context,
        )
    return ResolvedField(resource_id, field_id, column)


def _validate_no_duplicates(values: Sequence[str], label: str) -> None:
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise PlanValidationError(
            "duplicate_plan_item", f"{label} contains duplicates", field=label, values=duplicates
        )


def _validate_text(value: object, limits: PlanLimits, *, context: str) -> str:
    if not isinstance(value, str):
        raise PlanValidationError(
            "filter_type_mismatch", "Filter value must be text", context=context
        )
    if not value.strip():
        raise PlanValidationError(
            "blank_filter_value", "Text filter values cannot be blank", context=context
        )
    if len(value) > limits.max_text_chars or len(value.encode("utf-8")) > limits.max_text_bytes:
        raise PlanValidationError(
            "filter_value_too_large", "Text filter value exceeds the configured limit", context=context
        )
    if any(ord(character) < 32 for character in value):
        raise PlanValidationError(
            "unsafe_filter_value", "Text filter values cannot contain control characters", context=context
        )
    return value


def _validate_scalar(
    value: object,
    column: SemanticColumn,
    limits: PlanLimits,
    *,
    context: str,
) -> object:
    data_type = column.data_type
    if data_type is SemanticDataType.INTEGER:
        if isinstance(value, bool) or not isinstance(value, int):
            raise PlanValidationError(
                "filter_type_mismatch", "Filter value must be an integer", context=context
            )
        if abs(value) > limits.max_numeric_magnitude:
            raise PlanValidationError(
                "filter_value_too_large", "Numeric filter value exceeds the configured limit", context=context
            )
        return value
    if data_type is SemanticDataType.NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PlanValidationError(
                "filter_type_mismatch", "Filter value must be numeric", context=context
            )
        numeric = float(value)
        if not math.isfinite(numeric) or abs(numeric) > limits.max_numeric_magnitude:
            raise PlanValidationError(
                "filter_value_too_large", "Numeric filter value is non-finite or too large", context=context
            )
        return value
    if data_type is SemanticDataType.BOOLEAN:
        if not isinstance(value, bool):
            raise PlanValidationError(
                "filter_type_mismatch", "Filter value must be boolean", context=context
            )
        return value
    text = _validate_text(value, limits, context=context)
    try:
        if data_type is SemanticDataType.DATE:
            date.fromisoformat(text)
        elif data_type is SemanticDataType.DATETIME:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PlanValidationError(
            "filter_type_mismatch", "Date/time filter must use ISO-8601", context=context
        ) from exc
    return text


def _validate_filter_value(
    plan_filter: PlanFilter,
    resolved: ResolvedField,
    limits: PlanLimits,
) -> object:
    operator = plan_filter.operator
    context = f"filter:{resolved.qualified_id}"
    if operator.value not in resolved.column.allowed_operators:
        raise PlanValidationError(
            "operator_not_allowed",
            "Filter operator is not approved for this field",
            field_id=resolved.qualified_id,
            operator=operator.value,
            allowed=list(resolved.column.allowed_operators),
        )
    if operator in {PlanOperator.IS_NULL, PlanOperator.IS_NOT_NULL}:
        if plan_filter.value is not None:
            raise PlanValidationError(
                "filter_value_forbidden", "Null checks cannot carry a value", context=context
            )
        if not resolved.column.nullable:
            raise PlanValidationError(
                "null_check_nonnullable", "Null checks are invalid for a non-nullable field", context=context
            )
        return None
    if plan_filter.value is None:
        raise PlanValidationError(
            "missing_filter_value", "This filter operator requires a value", context=context
        )
    if operator in {PlanOperator.IN, PlanOperator.NOT_IN, PlanOperator.BETWEEN}:
        if not isinstance(plan_filter.value, tuple):
            raise PlanValidationError(
                "filter_type_mismatch", "This filter operator requires an array value", context=context
            )
        expected = 2 if operator is PlanOperator.BETWEEN else None
        if expected is not None and len(plan_filter.value) != expected:
            raise PlanValidationError(
                "invalid_filter_cardinality", "BETWEEN requires exactly two values", context=context
            )
        if expected is None and not 1 <= len(plan_filter.value) <= limits.max_in_values:
            raise PlanValidationError(
                "invalid_filter_cardinality",
                "IN filter array is empty or too large",
                context=context,
                limit=limits.max_in_values,
            )
        values = tuple(
            _validate_scalar(value, resolved.column, limits, context=context)
            for value in plan_filter.value
        )
        if operator is PlanOperator.BETWEEN:
            try:
                if values[0] > values[1]:
                    raise PlanValidationError(
                        "invalid_filter_range", "BETWEEN lower bound exceeds upper bound", context=context
                    )
            except TypeError as exc:
                raise PlanValidationError(
                    "filter_type_mismatch", "BETWEEN bounds are not comparable", context=context
                ) from exc
        return values
    scalar = _validate_scalar(plan_filter.value, resolved.column, limits, context=context)
    if operator in {PlanOperator.CONTAINS, PlanOperator.STARTS_WITH}:
        assert isinstance(scalar, str)
        if any(character in scalar for character in _TEXT_WILDCARDS):
            raise PlanValidationError(
                "unsafe_wildcard",
                "Wildcard filter values must be literal text without %, _, *, or ?",
                context=context,
            )
    return scalar


def _find_join(
    catalog: SemanticCatalog,
    base_resource_id: str,
    other_resource_id: str,
) -> SemanticJoin:
    matches = tuple(
        join
        for join in catalog.joins
        if {join.left_resource_id, join.right_resource_id}
        == {base_resource_id, other_resource_id}
    )
    if len(matches) != 1:
        raise PlanValidationError(
            "unavailable_join",
            "Plan fields must use one unambiguous reviewed direct join",
            base_resource_id=base_resource_id,
            resource_id=other_resource_id,
            candidate_count=len(matches),
        )
    return matches[0]


def _join_preserves_base_grain(join: SemanticJoin, base_resource_id: str) -> bool:
    if join.cardinality is JoinCardinality.ONE_TO_ONE:
        return True
    if join.left_resource_id == base_resource_id:
        return join.cardinality is JoinCardinality.MANY_TO_ONE
    return join.cardinality is JoinCardinality.ONE_TO_MANY


def _normalized_key(reference: str, base_resource_id: str) -> str:
    resource_id, field_id = _qualified(reference, base_resource_id)
    return f"{resource_id}.{field_id}"


def validate_bridge_query_plan(
    plan: BridgeQueryPlan,
    catalog: SemanticCatalog,
    *,
    metric_registry: MetricRegistry = DEFAULT_METRIC_REGISTRY,
    scope: QueryScope | None = None,
    limits: PlanLimits | None = None,
) -> ValidatedBridgeQueryPlan:
    """Validate a typed plan against reviewed, versioned definitions."""

    if not isinstance(plan, BridgeQueryPlan):
        raise PlanValidationError(
            "invalid_plan_type", "validate_bridge_query_plan requires BridgeQueryPlan"
        )
    active_limits = limits or PlanLimits()
    if scope is not None and catalog.identity.workspace_id != scope.workspace_id:
        raise PlanValidationError(
            "catalog_scope_mismatch",
            "Semantic catalog identity does not match the server-owned workspace scope",
        )
    if scope is not None and plan.time_scope.mode is not scope.time_mode:
        raise PlanValidationError(
            "time_scope_mismatch",
            "Plan time mode does not match the server-owned request scope",
            plan_mode=plan.time_scope.mode.value,
            server_mode=scope.time_mode.value,
        )
    resource = catalog.resources_by_id.get(plan.resource_id)
    if resource is None:
        raise PlanValidationError(
            "unknown_resource", "Plan resource is not in the semantic catalog", resource_id=plan.resource_id
        )
    if (
        scope is not None
        and scope.bridge_number is not None
        and "bridge_number" not in resource.column_ids
    ):
        raise PlanValidationError(
            "incompatible_bridge_resource",
            "Bridge-scoped requests require a semantic resource with bridge identity",
            resource_id=resource.resource_id,
        )
    counts = {
        "metric_ids": (len(plan.metric_ids), active_limits.max_metrics),
        "selected_fields": (len(plan.selected_fields), active_limits.max_selected_fields),
        "dimensions": (len(plan.dimensions), active_limits.max_dimensions),
        "filters": (len(plan.filters), active_limits.max_filters),
        "order": (len(plan.order), active_limits.max_order),
    }
    for name, (count, maximum) in counts.items():
        if count > maximum:
            raise PlanValidationError(
                "plan_complexity_exceeded",
                "Plan exceeds a configured item limit",
                field=name,
                count=count,
                limit=maximum,
            )
    if plan.limit < 1 or plan.limit > active_limits.max_limit:
        raise PlanValidationError(
            "invalid_limit", "Plan limit is outside the allowed range", limit=active_limits.max_limit
        )
    if plan.intent is PlanIntent.LOOKUP and plan.limit > active_limits.max_lookup_limit:
        raise PlanValidationError(
            "invalid_limit",
            "Lookup plans use the smaller lookup row limit",
            limit=active_limits.max_lookup_limit,
        )

    _validate_no_duplicates(plan.metric_ids, "metric_ids")
    selected = tuple(
        _resolve_field(value, plan.resource_id, catalog, context="selected_fields")
        for value in plan.selected_fields
    )
    dimensions = tuple(
        _resolve_field(value, plan.resource_id, catalog, context="dimensions")
        for value in plan.dimensions
    )
    _validate_no_duplicates([item.qualified_id for item in selected], "selected_fields")
    _validate_no_duplicates([item.qualified_id for item in dimensions], "dimensions")

    metrics: list[MetricDefinition] = []
    for metric_id in plan.metric_ids:
        metric = metric_registry.metrics_by_id.get(metric_id)
        if metric is None:
            raise PlanValidationError(
                "unknown_metric", "Metric is not in the reviewed registry", metric_id=metric_id
            )
        if metric.status is MetricStatus.DISABLED:
            raise PlanValidationError(
                "disabled_metric",
                "Metric is disabled and cannot be selected",
                metric_id=metric_id,
                reason=metric.disabled_reason,
            )
        if metric.source_resource_id != plan.resource_id:
            raise PlanValidationError(
                "metric_resource_mismatch",
                "All metrics must use the plan's declared base resource",
                metric_id=metric_id,
                metric_resource_id=metric.source_resource_id,
                plan_resource_id=plan.resource_id,
            )
        if ":" in metric.expression:
            raise PlanValidationError(
                "metric_requires_server_parameter",
                "Metric requires a trusted server parameter not represented by this plan version",
                metric_id=metric_id,
            )
        metrics.append(metric)

    aggregate_intents = {
        PlanIntent.COUNT,
        PlanIntent.AGGREGATE,
        PlanIntent.TREND,
        PlanIntent.COMPARE,
    }
    if plan.intent in aggregate_intents and not metrics:
        raise PlanValidationError(
            "metric_required", "Aggregate, count, trend, and compare plans require a metric"
        )
    if plan.intent in {PlanIntent.LIST, PlanIntent.LOOKUP}:
        if metrics or dimensions:
            raise PlanValidationError(
                "intent_shape_mismatch", "List and lookup plans cannot contain metrics or dimensions"
            )
        if not selected:
            raise PlanValidationError(
                "selected_field_required", "List and lookup plans require selected fields"
            )
    elif selected and set(item.qualified_id for item in selected) != set(
        item.qualified_id for item in dimensions
    ):
        raise PlanValidationError(
            "aggregate_grain_error",
            "Aggregate selected_fields must exactly match dimensions",
        )
    if plan.intent is PlanIntent.COUNT and any(metric.unit != "bridges" for metric in metrics):
        raise PlanValidationError(
            "intent_metric_mismatch", "Count intent accepts only bridge-count metrics"
        )
    if plan.intent is PlanIntent.COMPARE and not dimensions:
        raise PlanValidationError(
            "dimension_required", "Compare intent requires at least one dimension"
        )
    if plan.intent is PlanIntent.TREND:
        if plan.time_scope.mode is not TimeMode.TREND:
            raise PlanValidationError(
                "intent_time_mismatch", "Trend intent requires trend time scope"
            )
        if f"{plan.resource_id}.data_year" not in {
            item.qualified_id for item in dimensions
        }:
            raise PlanValidationError(
                "trend_year_dimension_required",
                "Trend plans must group by the base resource data_year",
            )
    elif plan.time_scope.mode is TimeMode.TREND and plan.intent not in {
        PlanIntent.LIST,
        PlanIntent.LOOKUP,
    }:
        raise PlanValidationError(
            "intent_time_mismatch", "Trend time scope requires trend intent for aggregate plans"
        )
    if (
        resource.resource_id.startswith("v_current_")
        and plan.time_scope.mode is not TimeMode.LATEST
    ):
        raise PlanValidationError(
            "current_resource_requires_latest",
            "Current-snapshot resources can only be used with latest time mode",
        )
    if resource.grain is ResourceGrain.COUNTY_INVENTORY and plan.time_scope.mode is TimeMode.LATEST:
        raise PlanValidationError(
            "latest_requires_bridge_identity",
            "Exact-year aggregate resources cannot infer per-bridge latest semantics",
        )

    for dimension in dimensions:
        for metric in metrics:
            if dimension.qualified_id not in metric.allowed_dimensions:
                raise PlanValidationError(
                    "metric_dimension_not_allowed",
                    "Dimension is not approved for the selected metric",
                    metric_id=metric.metric_id,
                    dimension=dimension.qualified_id,
                )

    validated_filters: list[tuple[PlanFilter, ResolvedField, object]] = []
    filter_keys: list[str] = []
    for item in plan.filters:
        resolved = _resolve_field(item.field_id, plan.resource_id, catalog, context="filters")
        if resolved.field_id in _SERVER_SCOPE_FIELDS:
            raise PlanValidationError(
                "client_scope_override",
                "Plan filters cannot override server-owned scope",
                field_id=resolved.qualified_id,
            )
        normalized_value = _validate_filter_value(item, resolved, active_limits)
        filter_key = f"{resolved.qualified_id}:{item.operator.value}"
        filter_keys.append(filter_key)
        validated_filters.append((item, resolved, normalized_value))
    _validate_no_duplicates(filter_keys, "filters")

    referenced_resource_ids = {
        item.resource_id
        for item in (*selected, *dimensions, *(entry[1] for entry in validated_filters))
        if item.resource_id != plan.resource_id
    }
    joins: list[SemanticJoin] = []
    joined_resources: list[SemanticResource] = []
    for resource_id in sorted(referenced_resource_ids):
        joined = catalog.resources_by_id[resource_id]
        join = _find_join(catalog, plan.resource_id, resource_id)
        if plan.intent in aggregate_intents and not _join_preserves_base_grain(
            join, plan.resource_id
        ):
            raise PlanValidationError(
                "aggregate_grain_error",
                "Join would multiply base rows and corrupt aggregate metrics",
                join_id=join.join_id,
            )
        joins.append(join)
        joined_resources.append(joined)

    output_aliases: dict[str, str] = {}
    used_aliases: set[str] = set()
    field_outputs = dimensions if plan.intent in aggregate_intents else selected
    for item in field_outputs:
        alias = item.field_id
        if alias in used_aliases:
            alias = f"{item.resource_id}__{item.field_id}"
        if alias in used_aliases:
            raise PlanValidationError(
                "output_alias_collision", "Plan outputs cannot be assigned unique stable aliases"
            )
        output_aliases[item.qualified_id] = alias
        used_aliases.add(alias)
    for metric in metrics:
        alias = f"metric__{metric.metric_id.replace('.', '__')}"
        if alias in used_aliases:
            raise PlanValidationError(
                "output_alias_collision", "Metric output alias collides with a field output"
            )
        output_aliases[metric.metric_id] = alias
        used_aliases.add(alias)

    normalized_order: list[PlanOrder] = []
    order_keys: list[str] = []
    for item in plan.order:
        if item.by in metric_registry.metrics_by_id:
            normalized = item.by
            if normalized not in plan.metric_ids:
                raise PlanValidationError(
                    "unknown_order_output", "Order metric is not selected", by=item.by
                )
        else:
            normalized = _normalized_key(item.by, plan.resource_id)
            if normalized not in output_aliases:
                raise PlanValidationError(
                    "unknown_order_output", "Order field is not a selected output", by=item.by
                )
        order_keys.append(normalized)
        normalized_order.append(PlanOrder(normalized, item.direction))
    _validate_no_duplicates(order_keys, "order")

    return ValidatedBridgeQueryPlan(
        plan=plan,
        resource=resource,
        metrics=tuple(metrics),
        selected_fields=selected,
        dimensions=dimensions,
        filters=tuple(validated_filters),
        order=tuple(normalized_order),
        joined_resources=tuple(joined_resources),
        joins=tuple(joins),
        output_aliases=output_aliases,
    )


def _quote(identifier: str) -> str:
    if not _SEMANTIC_ID.fullmatch(identifier):
        raise PlanCompilationError(
            "invalid_compiler_identifier", "Compiler received an invalid semantic identifier"
        )
    return f'"{identifier}"'


def _field_sql(field: ResolvedField) -> str:
    return f'{_quote(field.resource_id)}.{_quote(field.field_id)}'


def _sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if value is True:
        return "TRUE"
    if value is False:
        return "FALSE"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return repr(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise PlanCompilationError(
        "unrenderable_filter_value", "Validated filter value cannot be rendered"
    )


def _filter_sql(
    item: PlanFilter, resolved: ResolvedField, value: object
) -> str:
    expression = _field_sql(resolved)
    operator = item.operator
    scalar_operators = {
        PlanOperator.EQ: "=",
        PlanOperator.NE: "<>",
        PlanOperator.LT: "<",
        PlanOperator.LTE: "<=",
        PlanOperator.GT: ">",
        PlanOperator.GTE: ">=",
    }
    if operator in scalar_operators:
        return f"{expression} {scalar_operators[operator]} {_sql_literal(value)}"
    if operator in {PlanOperator.IN, PlanOperator.NOT_IN}:
        assert isinstance(value, tuple)
        keyword = "IN" if operator is PlanOperator.IN else "NOT IN"
        return f"{expression} {keyword} (" + ", ".join(_sql_literal(v) for v in value) + ")"
    if operator is PlanOperator.BETWEEN:
        assert isinstance(value, tuple) and len(value) == 2
        return f"{expression} BETWEEN {_sql_literal(value[0])} AND {_sql_literal(value[1])}"
    if operator is PlanOperator.CONTAINS:
        assert isinstance(value, str)
        return f"{expression} LIKE {_sql_literal('%' + value + '%')}"
    if operator is PlanOperator.STARTS_WITH:
        assert isinstance(value, str)
        return f"{expression} LIKE {_sql_literal(value + '%')}"
    if operator is PlanOperator.IS_NULL:
        return f"{expression} IS NULL"
    if operator is PlanOperator.IS_NOT_NULL:
        return f"{expression} IS NOT NULL"
    raise PlanCompilationError(
        "unsupported_operator", "Validated filter operator lacks a compiler implementation"
    )


def _join_sql(base_resource_id: str, join: SemanticJoin) -> str:
    if join.left_resource_id == base_resource_id:
        other = join.right_resource_id
        base_fields = join.left_fields
        other_fields = join.right_fields
    else:
        other = join.left_resource_id
        base_fields = join.right_fields
        other_fields = join.left_fields
    predicates = [
        f'{_quote(base_resource_id)}.{_quote(base_field)} = '
        f'{_quote(other)}.{_quote(other_field)}'
        for base_field, other_field in zip(base_fields, other_fields, strict=True)
    ]
    return f"JOIN {_quote(other)} AS {_quote(other)} ON " + " AND ".join(predicates)


def _build_sql_catalog(catalog: SemanticCatalog) -> SqlCatalog:
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
        functions=frozenset(function.sql_name.casefold() for function in catalog.functions),
    )


def _logical_sql(validated: ValidatedBridgeQueryPlan) -> tuple[str, tuple[dict[str, object], ...]]:
    plan = validated.plan
    aggregate = plan.intent in {
        PlanIntent.COUNT,
        PlanIntent.AGGREGATE,
        PlanIntent.TREND,
        PlanIntent.COMPARE,
    }
    output_parts: list[str] = []
    outputs: list[dict[str, object]] = []
    field_outputs = validated.dimensions if aggregate else validated.selected_fields
    for item in field_outputs:
        alias = validated.output_aliases[item.qualified_id]
        output_parts.append(f"{_field_sql(item)} AS {_quote(alias)}")
        outputs.append(
            {
                "kind": "dimension" if aggregate else "field",
                "id": item.qualified_id,
                "alias": alias,
                "data_type": item.column.data_type.value,
                "unit": item.column.unit,
            }
        )
    for metric in validated.metrics:
        alias = validated.output_aliases[metric.metric_id]
        output_parts.append(f"{metric.expression} AS {_quote(alias)}")
        outputs.append(
            {
                "kind": "metric",
                "id": metric.metric_id,
                "alias": alias,
                "unit": metric.unit,
                "null_semantics": metric.null_semantics,
                "denominator_semantics": metric.denominator_semantics,
            }
        )
    if not output_parts:
        raise PlanCompilationError("empty_projection", "Plan produced no logical outputs")
    clauses = [
        "SELECT " + ", ".join(output_parts),
        f"FROM {_quote(plan.resource_id)} AS {_quote(plan.resource_id)}",
    ]
    clauses.extend(_join_sql(plan.resource_id, join) for join in validated.joins)
    if validated.filters:
        clauses.append(
            "WHERE "
            + " AND ".join(
                _filter_sql(plan_filter, field, value)
                for plan_filter, field, value in validated.filters
            )
        )
    if aggregate and validated.dimensions:
        clauses.append("GROUP BY " + ", ".join(_field_sql(item) for item in validated.dimensions))
    if validated.order:
        clauses.append(
            "ORDER BY "
            + ", ".join(
                f'{_quote(validated.output_aliases[item.by])} {item.direction.value.upper()}'
                for item in validated.order
            )
        )
    clauses.append(f"LIMIT {plan.limit}")
    return " ".join(clauses), tuple(outputs)


def _scope_evidence(scope: QueryScope) -> dict[str, object]:
    return {
        "workspace_id": scope.workspace_id,
        "state_code": scope.state_code,
        "bridge_number": scope.bridge_number,
        "time_mode": scope.time_mode.value,
        "data_year": scope.data_year,
        "start_year": scope.start_year,
        "end_year": scope.end_year,
    }


def compile_bridge_query_plan(
    plan: BridgeQueryPlan,
    catalog: SemanticCatalog,
    scope: QueryScope,
    *,
    metric_registry: MetricRegistry = DEFAULT_METRIC_REGISTRY,
    plan_limits: PlanLimits | None = None,
    sql_limits: SqlLimits | None = None,
) -> CompiledBridgeQueryPlan:
    """Validate and compile a plan through the independent scoped SQL guard."""

    validated_plan = validate_bridge_query_plan(
        plan,
        catalog,
        metric_registry=metric_registry,
        scope=scope,
        limits=plan_limits,
    )
    logical_sql, outputs = _logical_sql(validated_plan)
    sql_catalog = _build_sql_catalog(catalog)
    active_sql_limits = sql_limits or SqlLimits()
    try:
        validated_sql = validate_generated_sql(
            logical_sql, sql_catalog, limits=active_sql_limits
        )
        compiled_sql = compile_scoped_sql(
            validated_sql,
            scope,
            limits=active_sql_limits,
            max_rows=plan.limit,
        )
    except PlanError:
        raise
    except Exception as exc:
        raise PlanCompilationError(
            "sql_guard_rejected_plan",
            "The independent SQL guard rejected the compiled logical plan",
            guard_code=getattr(exc, "code", type(exc).__name__),
        ) from exc
    evidence = PlanEvidence(
        plan_hash=plan.plan_hash,
        catalog_hash=catalog.catalog_hash,
        metric_registry_hash=metric_registry.registry_hash,
        canonical_plan=plan.canonical_payload(),
        intent=plan.intent.value,
        resource_id=plan.resource_id,
        time_mode=plan.time_scope.mode.value,
        scope=_scope_evidence(scope),
        outputs=outputs,
        joins=tuple(join.join_id for join in validated_plan.joins),
        logical_sql=logical_sql,
        compiled_tables=tuple(scan.table for scan in validated_sql.tables),
    )
    return CompiledBridgeQueryPlan(
        plan=plan,
        validated_plan=validated_plan,
        logical_sql=logical_sql,
        validated_sql=validated_sql,
        compiled_sql=compiled_sql,
        evidence=evidence,
    )


__all__ = [
    "BridgeQueryPlan",
    "CompiledBridgeQueryPlan",
    "OrderDirection",
    "PlanCompilationError",
    "PlanError",
    "PlanEvidence",
    "PlanFilter",
    "PlanIntent",
    "PlanLimits",
    "PlanOperator",
    "PlanOrder",
    "PlanParseError",
    "PlanTimeScope",
    "PlanValidationError",
    "ResolvedField",
    "ValidatedBridgeQueryPlan",
    "compile_bridge_query_plan",
    "validate_bridge_query_plan",
]
