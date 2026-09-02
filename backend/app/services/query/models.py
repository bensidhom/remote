"""Immutable contracts for BridgeGuard's reviewed query semantic layer.

The classes in this module intentionally contain no database discovery code.
They describe the small, reviewed surface that a query planner or SQL AST
validator may use.  Live SQLite metadata is therefore never copied into an LLM
prompt merely because a table or column happens to exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
from types import MappingProxyType
from typing import Iterable, Mapping


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


def _require_identifier(value: str, label: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase semantic identifier: {value!r}")


def _stable_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class SemanticDataType(str, Enum):
    INTEGER = "integer"
    NUMBER = "number"
    TEXT = "text"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"


class ResourceGrain(str, Enum):
    BRIDGE_SNAPSHOT = "one_row_per_available_bridge_inventory_snapshot"
    CURRENT_BRIDGE = "one_row_per_bridge_in_latest_available_inventory_year"
    BRIDGE_FEATURE = "one_row_per_bridge_feature_snapshot"
    BRIDGE_ROUTE = "one_row_per_bridge_route_snapshot"
    BRIDGE_SPAN = "one_row_per_bridge_span_configuration_snapshot"
    BRIDGE_DECK_SUMMARY = "one_row_per_available_bridge_snapshot_deck_material_summary"
    BRIDGE_ELEMENT = "one_row_per_bridge_element_snapshot"
    BRIDGE_WORK = "one_row_per_bridge_work_record_snapshot"
    POSTING_STATUS = "one_row_per_bridge_posting_status_snapshot"
    BRIDGE_CONDITION = "one_row_per_available_bridge_snapshot_condition"
    COUNTY_INVENTORY = "one_row_per_county_and_snapshot_year"


class JoinCardinality(str, Enum):
    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"


class MetricClassification(str, Enum):
    OPERATIONAL_INVENTORY = "operational_inventory"
    FEDERAL_PERFORMANCE_MEASURE = "federal_performance_measure"


class MetricStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class CatalogIdentity:
    """Version key for one semantic catalog instance.

    ``database_identity`` is a caller-supplied stable identity/fingerprint, not
    a value shown to a model.  Including it prevents two database files with
    equal generation counters from sharing a cache entry.
    """

    database_identity: str
    workspace_id: str
    schema_generation: int
    data_generation: int

    def __post_init__(self) -> None:
        if not self.database_identity.strip():
            raise ValueError("database_identity is required")
        if not self.workspace_id.strip():
            raise ValueError("workspace_id is required")
        if self.schema_generation < 0 or self.data_generation < 0:
            raise ValueError("schema and data generations must be non-negative")

    @property
    def cache_key(self) -> tuple[str, str, int, int]:
        return (
            self.database_identity,
            self.workspace_id,
            self.schema_generation,
            self.data_generation,
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "database_identity": self.database_identity,
            "workspace_id": self.workspace_id,
            "schema_generation": self.schema_generation,
            "data_generation": self.data_generation,
        }


@dataclass(frozen=True, slots=True)
class SemanticColumn:
    field_id: str
    data_type: SemanticDataType
    nullable: bool
    description: str
    physical_column: str | None = None
    unit: str | None = None
    allowed_operators: tuple[str, ...] = (
        "eq",
        "ne",
        "is_null",
        "is_not_null",
    )
    classification: str = "public_bridge_inventory"

    def __post_init__(self) -> None:
        _require_identifier(self.field_id, "field_id")
        if self.physical_column is not None:
            _require_identifier(self.physical_column, "physical_column")
        if not self.description.strip():
            raise ValueError(f"description is required for {self.field_id}")
        object.__setattr__(self, "allowed_operators", tuple(self.allowed_operators))
        if len(set(self.allowed_operators)) != len(self.allowed_operators):
            raise ValueError(f"duplicate operators for {self.field_id}")

    def canonical_payload(self, *, include_physical: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "field_id": self.field_id,
            "data_type": self.data_type.value,
            "nullable": self.nullable,
            "description": self.description,
            "unit": self.unit,
            "allowed_operators": list(self.allowed_operators),
            "classification": self.classification,
        }
        if include_physical:
            result["physical_column"] = self.physical_column
        return result


@dataclass(frozen=True, slots=True)
class SemanticResource:
    resource_id: str
    description: str
    grain: ResourceGrain
    columns: tuple[SemanticColumn, ...]
    time_semantics: str
    scope_fields: tuple[str, ...]
    physical_relations: tuple[str, ...]
    implementation_id: str

    def __post_init__(self) -> None:
        _require_identifier(self.resource_id, "resource_id")
        object.__setattr__(self, "columns", tuple(self.columns))
        object.__setattr__(self, "scope_fields", tuple(self.scope_fields))
        object.__setattr__(self, "physical_relations", tuple(self.physical_relations))
        field_ids = tuple(column.field_id for column in self.columns)
        if not field_ids or len(set(field_ids)) != len(field_ids):
            raise ValueError(f"resource {self.resource_id} must have unique columns")
        unknown_scope = sorted(set(self.scope_fields).difference(field_ids))
        if unknown_scope:
            raise ValueError(
                f"resource {self.resource_id} has unknown scope fields: {unknown_scope}"
            )
        if not self.physical_relations:
            raise ValueError(f"resource {self.resource_id} needs reviewed relations")

    @property
    def column_ids(self) -> tuple[str, ...]:
        return tuple(column.field_id for column in self.columns)

    @property
    def columns_by_id(self) -> Mapping[str, SemanticColumn]:
        return MappingProxyType({column.field_id: column for column in self.columns})

    def canonical_payload(self, *, include_physical: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "resource_id": self.resource_id,
            "description": self.description,
            "grain": self.grain.value,
            "columns": [
                column.canonical_payload(include_physical=include_physical)
                for column in self.columns
            ],
            "time_semantics": self.time_semantics,
            "scope_fields": list(self.scope_fields),
            "implementation_id": self.implementation_id,
        }
        if include_physical:
            result["physical_relations"] = list(self.physical_relations)
        return result


@dataclass(frozen=True, slots=True)
class SemanticJoin:
    join_id: str
    left_resource_id: str
    right_resource_id: str
    left_fields: tuple[str, ...]
    right_fields: tuple[str, ...]
    cardinality: JoinCardinality
    description: str

    def __post_init__(self) -> None:
        _require_identifier(self.join_id, "join_id")
        _require_identifier(self.left_resource_id, "left_resource_id")
        _require_identifier(self.right_resource_id, "right_resource_id")
        object.__setattr__(self, "left_fields", tuple(self.left_fields))
        object.__setattr__(self, "right_fields", tuple(self.right_fields))
        if not self.left_fields or len(self.left_fields) != len(self.right_fields):
            raise ValueError(f"join {self.join_id} has invalid key fields")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "join_id": self.join_id,
            "left_resource_id": self.left_resource_id,
            "right_resource_id": self.right_resource_id,
            "left_fields": list(self.left_fields),
            "right_fields": list(self.right_fields),
            "cardinality": self.cardinality.value,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class SafeFunction:
    function_id: str
    sql_name: str
    min_arguments: int
    max_arguments: int | None
    aggregate: bool
    deterministic: bool
    description: str

    def __post_init__(self) -> None:
        _require_identifier(self.function_id, "function_id")
        if not self.sql_name or not self.sql_name.replace("_", "").isalnum():
            raise ValueError(f"invalid SQL function name: {self.sql_name!r}")
        if self.min_arguments < 0:
            raise ValueError("min_arguments must be non-negative")
        if self.max_arguments is not None and self.max_arguments < self.min_arguments:
            raise ValueError("max_arguments cannot be less than min_arguments")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "function_id": self.function_id,
            "sql_name": self.sql_name.upper(),
            "min_arguments": self.min_arguments,
            "max_arguments": self.max_arguments,
            "aggregate": self.aggregate,
            "deterministic": self.deterministic,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class SemanticCatalog:
    definition_version: str
    identity: CatalogIdentity
    resources: tuple[SemanticResource, ...]
    joins: tuple[SemanticJoin, ...]
    functions: tuple[SafeFunction, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "resources", tuple(self.resources))
        object.__setattr__(self, "joins", tuple(self.joins))
        object.__setattr__(self, "functions", tuple(self.functions))
        resource_ids = tuple(resource.resource_id for resource in self.resources)
        join_ids = tuple(join.join_id for join in self.joins)
        function_ids = tuple(function.function_id for function in self.functions)
        for label, values in (
            ("resource", resource_ids),
            ("join", join_ids),
            ("function", function_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label} identifiers")
        resources = {resource.resource_id: resource for resource in self.resources}
        for join in self.joins:
            if join.left_resource_id not in resources or join.right_resource_id not in resources:
                raise ValueError(f"join {join.join_id} references an unknown resource")
            left = resources[join.left_resource_id]
            right = resources[join.right_resource_id]
            if not set(join.left_fields).issubset(left.column_ids):
                raise ValueError(f"join {join.join_id} has unknown left fields")
            if not set(join.right_fields).issubset(right.column_ids):
                raise ValueError(f"join {join.join_id} has unknown right fields")

    @property
    def resources_by_id(self) -> Mapping[str, SemanticResource]:
        return MappingProxyType(
            {resource.resource_id: resource for resource in self.resources}
        )

    @property
    def functions_by_sql_name(self) -> Mapping[str, SafeFunction]:
        return MappingProxyType(
            {function.sql_name.upper(): function for function in self.functions}
        )

    @property
    def physical_table_allowlist(self) -> frozenset[str]:
        """Approved query relations (semantic views), never implementation tables."""

        return frozenset(resource.resource_id for resource in self.resources)

    @property
    def implementation_relation_inventory(self) -> frozenset[str]:
        """Reviewed dependencies for migrations/audit, not an AST query allowlist."""

        return frozenset(
            relation
            for resource in self.resources
            for relation in resource.physical_relations
        )

    @property
    def query_columns_by_relation(self) -> Mapping[str, frozenset[str]]:
        """Exact semantic-view columns permitted to an expert SQL validator."""

        return MappingProxyType(
            {
                resource.resource_id: frozenset(resource.column_ids)
                for resource in self.resources
            }
        )

    @property
    def sandbox_relation_allowlist(
        self,
    ) -> Mapping[str, frozenset[str] | None]:
        """Semantic views plus reviewed implementation dependencies.

        Generated SQL is independently restricted to ``query_columns_by_relation``.
        The SQLite authorizer also sees reads performed while expanding those
        approved views, including the loaded-inventory fallback CTE, so their
        reviewed physical dependencies must be readable only at this final
        execution boundary.
        """

        relations: dict[str, frozenset[str] | None] = {
            name: columns
            for name, columns in self.query_columns_by_relation.items()
        }
        for relation in self.implementation_relation_inventory:
            relations.setdefault(relation, None)
        return MappingProxyType(relations)

    @property
    def catalog_hash(self) -> str:
        payload = {
            "definition_version": self.definition_version,
            "identity": self.identity.canonical_payload(),
            "resources": [
                resource.canonical_payload(include_physical=True)
                for resource in self.resources
            ],
            "joins": [join.canonical_payload() for join in self.joins],
            "functions": [function.canonical_payload() for function in self.functions],
        }
        return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()

    @property
    def cache_key(self) -> tuple[str, str, int, int, str]:
        return (*self.identity.cache_key, self.catalog_hash)

    def is_allowed_column(self, resource_id: str, field_id: str) -> bool:
        resource = self.resources_by_id.get(resource_id)
        return resource is not None and field_id in resource.column_ids

    def is_allowed_query_column(self, relation: str, column: str) -> bool:
        return column in self.query_columns_by_relation.get(relation, frozenset())

    def is_allowed_function(self, sql_name: str, argument_count: int | None = None) -> bool:
        function = self.functions_by_sql_name.get(sql_name.upper())
        if function is None:
            return False
        if argument_count is None:
            return True
        return argument_count >= function.min_arguments and (
            function.max_arguments is None or argument_count <= function.max_arguments
        )

    def is_allowed_join(
        self,
        left_resource_id: str,
        right_resource_id: str,
        left_fields: Iterable[str],
        right_fields: Iterable[str],
    ) -> bool:
        left = tuple(left_fields)
        right = tuple(right_fields)
        return any(
            (
                join.left_resource_id == left_resource_id
                and join.right_resource_id == right_resource_id
                and join.left_fields == left
                and join.right_fields == right
            )
            or (
                join.left_resource_id == right_resource_id
                and join.right_resource_id == left_resource_id
                and join.left_fields == right
                and join.right_fields == left
            )
            for join in self.joins
        )

    def prompt_payload(self) -> dict[str, object]:
        """Return only reviewed logical names and semantics for model context.

        Database identity, paths, physical table names, and physical column names
        are deliberately absent.  The payload is generated from the static
        definitions above, never from ``sqlite_master`` or ``PRAGMA`` output.
        """

        return {
            "catalog_version": self.definition_version,
            "catalog_hash": self.catalog_hash,
            "schema_generation": self.identity.schema_generation,
            "data_generation": self.identity.data_generation,
            "resources": [
                resource.canonical_payload(include_physical=False)
                for resource in self.resources
            ],
            "joins": [join.canonical_payload() for join in self.joins],
            "functions": [function.canonical_payload() for function in self.functions],
        }

    def prompt_text(self) -> str:
        """Serialize the allowlist deterministically for a query-planning prompt."""

        return _stable_json(self.prompt_payload())


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    metric_id: str
    label: str
    description: str
    source_resource_id: str
    expression: str
    required_columns: tuple[str, ...]
    unit: str
    null_semantics: str
    denominator_semantics: str
    time_semantics: str
    allowed_dimensions: tuple[str, ...]
    classification: MetricClassification
    status: MetricStatus = MetricStatus.ACTIVE
    disabled_reason: str | None = None

    def __post_init__(self) -> None:
        parts = self.metric_id.split(".")
        if len(parts) != 2:
            raise ValueError("metric_id must be namespace.name")
        for part in parts:
            _require_identifier(part, "metric_id component")
        _require_identifier(self.source_resource_id, "source_resource_id")
        object.__setattr__(self, "required_columns", tuple(self.required_columns))
        object.__setattr__(self, "allowed_dimensions", tuple(self.allowed_dimensions))
        if not self.expression.strip():
            raise ValueError(f"metric {self.metric_id} needs an expression")
        if self.status is MetricStatus.DISABLED and not self.disabled_reason:
            raise ValueError(f"disabled metric {self.metric_id} needs a reason")
        if self.status is MetricStatus.ACTIVE and self.disabled_reason is not None:
            raise ValueError(f"active metric {self.metric_id} cannot have disabled_reason")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "metric_id": self.metric_id,
            "label": self.label,
            "description": self.description,
            "source_resource_id": self.source_resource_id,
            "expression": self.expression,
            "required_columns": list(self.required_columns),
            "unit": self.unit,
            "null_semantics": self.null_semantics,
            "denominator_semantics": self.denominator_semantics,
            "time_semantics": self.time_semantics,
            "allowed_dimensions": list(self.allowed_dimensions),
            "classification": self.classification.value,
            "status": self.status.value,
            "disabled_reason": self.disabled_reason,
        }


@dataclass(frozen=True, slots=True)
class MetricRegistry:
    definition_version: str
    metrics: tuple[MetricDefinition, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", tuple(self.metrics))
        ids = tuple(metric.metric_id for metric in self.metrics)
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate metric identifiers")

    @property
    def metrics_by_id(self) -> Mapping[str, MetricDefinition]:
        return MappingProxyType({metric.metric_id: metric for metric in self.metrics})

    @property
    def active_metrics(self) -> tuple[MetricDefinition, ...]:
        return tuple(metric for metric in self.metrics if metric.status is MetricStatus.ACTIVE)

    @property
    def disabled_metrics(self) -> tuple[MetricDefinition, ...]:
        return tuple(metric for metric in self.metrics if metric.status is MetricStatus.DISABLED)

    @property
    def registry_hash(self) -> str:
        payload = {
            "definition_version": self.definition_version,
            "metrics": [metric.canonical_payload() for metric in self.metrics],
        }
        return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()

    def get(self, metric_id: str, *, include_disabled: bool = False) -> MetricDefinition:
        metric = self.metrics_by_id.get(metric_id)
        if metric is None or (metric.status is MetricStatus.DISABLED and not include_disabled):
            raise KeyError(metric_id)
        return metric

    def prompt_payload(self) -> dict[str, object]:
        return {
            "registry_version": self.definition_version,
            "registry_hash": self.registry_hash,
            "metrics": [metric.canonical_payload() for metric in self.metrics],
            "selection_rule": "Only metrics with status=active may be selected.",
        }

    def prompt_text(self) -> str:
        return _stable_json(self.prompt_payload())


__all__ = [
    "CatalogIdentity",
    "JoinCardinality",
    "MetricClassification",
    "MetricDefinition",
    "MetricRegistry",
    "MetricStatus",
    "ResourceGrain",
    "SafeFunction",
    "SemanticCatalog",
    "SemanticColumn",
    "SemanticDataType",
    "SemanticJoin",
    "SemanticResource",
]
