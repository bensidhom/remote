"""Fail-closed execution of a locally pinned JSON Schema artifact.

The executor deliberately accepts a registry *path*, not an arbitrary schema
path.  This makes :class:`~app.standards.fhwa_registry.RuleBundleRegistry` the
only trust root: the selected artifact must belong to the selected bundle, be
marked ``vendored_verified``, and retain the SHA-256 digest enforced while the
registry is loaded.  The digest is checked again after reading to close the
load/read time-of-check-to-time-of-use window.

No dialect fallback is permitted.  In particular, a schema declaring the
currently observed ``draft/2024-07`` URI is reported as unsupported rather
than being interpreted as Draft 2020-12.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from app.standards.fhwa_registry import (
    ArtifactState,
    DisabledRuleBundleError,
    RegistryError,
    RuleBundleRegistry,
    load_rule_bundle_registry,
)

try:  # Production pins jsonschema; fail closed if packaging is incomplete.
    from jsonschema import (
        Draft3Validator,
        Draft4Validator,
        Draft6Validator,
        Draft7Validator,
        Draft201909Validator,
        Draft202012Validator,
    )
    from jsonschema.exceptions import SchemaError

    JSONSCHEMA_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised in the slim local venv.
    Draft3Validator = None  # type: ignore[assignment]
    Draft4Validator = None  # type: ignore[assignment]
    Draft6Validator = None  # type: ignore[assignment]
    Draft7Validator = None  # type: ignore[assignment]
    Draft201909Validator = None  # type: ignore[assignment]
    Draft202012Validator = None  # type: ignore[assignment]
    SchemaError = Exception  # type: ignore[assignment,misc]
    JSONSCHEMA_AVAILABLE = False


class SchemaExecutionStatus(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED_DIALECT = "unsupported_dialect"
    INVALID_SCHEMA = "invalid_schema"
    INVALID_INPUT = "invalid_input"
    RESOURCE_LIMIT_EXCEEDED = "resource_limit_exceeded"
    EXECUTION_ERROR = "execution_error"


class ValidationTarget(str, Enum):
    """How a caller intends the canonical payload to be interpreted."""

    AUTO = "auto"
    BRIDGE = "bridge"
    BATCH = "batch"


@dataclass(frozen=True)
class SchemaExecutionLimits:
    max_schema_bytes: int = 8 * 1024 * 1024
    max_schema_nodes: int = 200_000
    max_instance_nodes: int = 200_000
    max_depth: int = 64
    max_string_length: int = 1_000_000
    max_array_items: int = 100_000
    max_object_members: int = 100_000
    max_issues: int = 1_000
    max_message_length: int = 4_096

    def validate(self) -> None:
        for name in (
            "max_schema_bytes",
            "max_schema_nodes",
            "max_instance_nodes",
            "max_depth",
            "max_string_length",
            "max_array_items",
            "max_object_members",
            "max_issues",
            "max_message_length",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class SchemaValidationIssue:
    instance_pointer: str
    schema_pointer: str
    keyword: str
    message: str


@dataclass(frozen=True)
class PinnedSchemaResult:
    status: SchemaExecutionStatus
    bundle_id: str
    artifact_id: str
    validation_target: ValidationTarget
    dialect: Optional[str]
    schema_sha256: Optional[str]
    issues: Tuple[SchemaValidationIssue, ...]
    issues_truncated: bool
    detail: str

    @property
    def valid(self) -> bool:
        return self.status is SchemaExecutionStatus.VALID

    @property
    def schema_executed(self) -> bool:
        return self.status in {
            SchemaExecutionStatus.VALID,
            SchemaExecutionStatus.INVALID,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "bundle_id": self.bundle_id,
            "artifact_id": self.artifact_id,
            "validation_target": self.validation_target.value,
            "dialect": self.dialect,
            "schema_sha256": self.schema_sha256,
            "valid": self.valid,
            "schema_executed": self.schema_executed,
            "issues": [asdict(issue) for issue in self.issues],
            "reported_issue_count": len(self.issues),
            "issues_truncated": self.issues_truncated,
            "detail": self.detail,
        }


class _ResourceLimitError(ValueError):
    def __init__(self, code: str, pointer: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.pointer = pointer
        self.message = message


class _DuplicateSchemaKey(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(f"duplicate JSON object key in schema: {key}")
        self.key = key


# Strip only an optional terminal fragment marker before lookup.  Never use
# ``validator_for`` on an unknown URI: that API intentionally falls back to a
# latest draft, which is unsafe for governing artifacts.
_DIALECT_FACTORIES = {
    "http://json-schema.org/draft-03/schema": lambda: Draft3Validator,
    "http://json-schema.org/draft-04/schema": lambda: Draft4Validator,
    "http://json-schema.org/draft-06/schema": lambda: Draft6Validator,
    "http://json-schema.org/draft-07/schema": lambda: Draft7Validator,
    "https://json-schema.org/draft/2019-09/schema": lambda: Draft201909Validator,
    "https://json-schema.org/draft/2020-12/schema": lambda: Draft202012Validator,
}


def execute_pinned_schema(
    instance: Any,
    *,
    registry_path: Path,
    bundle_id: str,
    artifact_id: str,
    target: ValidationTarget | str = ValidationTarget.AUTO,
    limits: SchemaExecutionLimits = SchemaExecutionLimits(),
) -> PinnedSchemaResult:
    """Validate one bridge or a bridge batch against a verified local schema.

    Every operational failure is represented in the returned result.  Invalid
    caller configuration (bad limits or target enum) raises ``ValueError`` so a
    programming error cannot be confused with an FHWA validation result.
    """

    limits.validate()
    try:
        validation_target = ValidationTarget(target)
    except (TypeError, ValueError) as exc:
        raise ValueError("target must be auto, bridge, or batch") from exc

    registry_file = Path(registry_path).resolve()
    try:
        registry = load_rule_bundle_registry(registry_file)
        artifact = _select_schema_artifact(registry, bundle_id, artifact_id)
    except (RegistryError, OSError, ValueError) as exc:
        return _result(
            SchemaExecutionStatus.UNAVAILABLE,
            bundle_id,
            artifact_id,
            validation_target,
            detail=f"verified schema artifact unavailable: {_safe_exception(exc)}",
        )

    if artifact.state is not ArtifactState.VENDORED_VERIFIED:
        return _result(
            SchemaExecutionStatus.UNAVAILABLE,
            bundle_id,
            artifact_id,
            validation_target,
            detail="schema artifact is not marked vendored_verified",
        )
    if not artifact.local_path or not artifact.sha256:
        return _result(
            SchemaExecutionStatus.UNAVAILABLE,
            bundle_id,
            artifact_id,
            validation_target,
            detail="verified schema metadata is incomplete",
        )
    if not _is_json_media_type(artifact.media_type):
        return _result(
            SchemaExecutionStatus.UNAVAILABLE,
            bundle_id,
            artifact_id,
            validation_target,
            schema_sha256=artifact.sha256,
            detail="schema artifact media type is not JSON",
        )

    try:
        artifact_path = _artifact_path(
            registry_file.parent, artifact.local_path
        )
        size = artifact_path.stat().st_size
        if size > limits.max_schema_bytes:
            raise _ResourceLimitError(
                "max_schema_bytes_exceeded",
                "",
                f"schema bytes exceed {limits.max_schema_bytes}",
            )
        schema_bytes = artifact_path.read_bytes()
        if len(schema_bytes) > limits.max_schema_bytes:
            raise _ResourceLimitError(
                "max_schema_bytes_exceeded",
                "",
                f"schema bytes exceed {limits.max_schema_bytes}",
            )
    except _ResourceLimitError as exc:
        return _limit_result(
            exc, bundle_id, artifact_id, validation_target, artifact.sha256, limits
        )
    except OSError as exc:
        return _result(
            SchemaExecutionStatus.UNAVAILABLE,
            bundle_id,
            artifact_id,
            validation_target,
            schema_sha256=artifact.sha256,
            detail=f"cannot read verified schema artifact: {_safe_exception(exc)}",
        )

    actual_sha256 = hashlib.sha256(schema_bytes).hexdigest()
    if actual_sha256 != artifact.sha256:
        return _result(
            SchemaExecutionStatus.UNAVAILABLE,
            bundle_id,
            artifact_id,
            validation_target,
            schema_sha256=artifact.sha256,
            detail="schema SHA-256 changed after registry verification",
        )

    try:
        schema = json.loads(
            schema_bytes.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateSchemaKey, ValueError) as exc:
        return _result(
            SchemaExecutionStatus.INVALID_SCHEMA,
            bundle_id,
            artifact_id,
            validation_target,
            schema_sha256=actual_sha256,
            issues=(
                SchemaValidationIssue("", "", "schema_json", _safe_exception(exc)),
            ),
            detail="verified artifact is not strict UTF-8 JSON",
        )

    try:
        _preflight_json(schema, limits, schema_document=True)
    except _ResourceLimitError as exc:
        return _limit_result(
            exc, bundle_id, artifact_id, validation_target, actual_sha256, limits
        )

    if not isinstance(schema, Mapping):
        return _result(
            SchemaExecutionStatus.INVALID_SCHEMA,
            bundle_id,
            artifact_id,
            validation_target,
            schema_sha256=actual_sha256,
            issues=(SchemaValidationIssue("", "", "$schema", "schema root must be an object with a declared dialect"),),
            detail="schema root does not declare an executable dialect",
        )

    declared_dialect = schema.get("$schema")
    if not isinstance(declared_dialect, str) or not declared_dialect.strip():
        return _result(
            SchemaExecutionStatus.UNSUPPORTED_DIALECT,
            bundle_id,
            artifact_id,
            validation_target,
            schema_sha256=actual_sha256,
            issues=(SchemaValidationIssue("", "/$schema", "$schema", "a supported JSON Schema dialect must be declared"),),
            detail="schema dialect is missing",
        )
    dialect_key = declared_dialect[:-1] if declared_dialect.endswith("#") else declared_dialect
    factory = _DIALECT_FACTORIES.get(dialect_key)
    if factory is None:
        return _result(
            SchemaExecutionStatus.UNSUPPORTED_DIALECT,
            bundle_id,
            artifact_id,
            validation_target,
            dialect=declared_dialect,
            schema_sha256=actual_sha256,
            issues=(SchemaValidationIssue("", "/$schema", "$schema", f"unsupported JSON Schema dialect: {declared_dialect}"),),
            detail="schema was not executed because its dialect is unsupported",
        )

    external_ref = _first_external_reference(schema)
    if external_ref is not None:
        pointer, keyword, reference = external_ref
        return _result(
            SchemaExecutionStatus.INVALID_SCHEMA,
            bundle_id,
            artifact_id,
            validation_target,
            dialect=declared_dialect,
            schema_sha256=actual_sha256,
            issues=(SchemaValidationIssue("", pointer, keyword, f"non-local schema reference is forbidden: {reference}"),),
            detail="schema contains a reference that could resolve outside the pinned artifact",
        )

    # Bound and classify the untrusted payload even when the optional validator
    # package is absent.  This preserves the resource boundary independently of
    # deployment packaging and makes shape failures deterministic.
    try:
        _preflight_json(instance, limits, schema_document=False)
        resolved_target = _resolve_target(instance, validation_target)
    except _ResourceLimitError as exc:
        return _limit_result(
            exc,
            bundle_id,
            artifact_id,
            validation_target,
            actual_sha256,
            limits,
            dialect=declared_dialect,
        )
    except ValueError as exc:
        return _result(
            SchemaExecutionStatus.INVALID_INPUT,
            bundle_id,
            artifact_id,
            validation_target,
            dialect=declared_dialect,
            schema_sha256=actual_sha256,
            issues=(SchemaValidationIssue("", "", "payload_shape", str(exc)),),
            detail="canonical payload shape is invalid",
        )

    if not JSONSCHEMA_AVAILABLE:
        return _result(
            SchemaExecutionStatus.UNAVAILABLE,
            bundle_id,
            artifact_id,
            validation_target,
            dialect=declared_dialect,
            schema_sha256=actual_sha256,
            detail="jsonschema execution dependency is unavailable",
        )

    validator_class = factory()
    if validator_class is None:  # Defensive guard for a partial installation.
        return _result(
            SchemaExecutionStatus.UNAVAILABLE,
            bundle_id,
            artifact_id,
            validation_target,
            dialect=declared_dialect,
            schema_sha256=actual_sha256,
            detail="validator for the declared dialect is unavailable",
        )
    try:
        validator_class.check_schema(schema)
    except SchemaError as exc:
        issue = SchemaValidationIssue(
            "",
            _pointer(getattr(exc, "absolute_path", ())),
            str(getattr(exc, "validator", None) or "schema"),
            _bounded_message(str(exc.message), limits.max_message_length),
        )
        return _result(
            SchemaExecutionStatus.INVALID_SCHEMA,
            bundle_id,
            artifact_id,
            validation_target,
            dialect=declared_dialect,
            schema_sha256=actual_sha256,
            issues=(issue,),
            detail="schema failed its declared dialect meta-schema check",
        )

    try:
        validator = validator_class(schema)
        raw_errors = _iter_target_errors(validator, schema, instance, resolved_target)
        issues, truncated = _collect_issues(raw_errors, limits)
    except Exception as exc:  # Referencing failures must never escape as 500s.
        return _result(
            SchemaExecutionStatus.INVALID_SCHEMA,
            bundle_id,
            artifact_id,
            resolved_target,
            dialect=declared_dialect,
            schema_sha256=actual_sha256,
            issues=(SchemaValidationIssue("", "", "schema_reference", _safe_exception(exc, limits.max_message_length)),),
            detail="schema could not be executed without external resolution",
        )

    status = SchemaExecutionStatus.INVALID if issues or truncated else SchemaExecutionStatus.VALID
    return _result(
        status,
        bundle_id,
        artifact_id,
        resolved_target,
        dialect=declared_dialect,
        schema_sha256=actual_sha256,
        issues=issues,
        issues_truncated=truncated,
        detail=(
            "payload satisfies the pinned JSON Schema"
            if status is SchemaExecutionStatus.VALID
            else "payload violates the pinned JSON Schema"
        ),
    )


def _select_schema_artifact(
    registry: RuleBundleRegistry, bundle_id: str, artifact_id: str
):
    bundle = registry.bundle(bundle_id)
    if not bundle.enabled:
        raise DisabledRuleBundleError(
            f"{bundle_id} is disabled: {bundle.disabled_reason or 'registry policy'}"
        )
    permitted = {artifact.id for artifact in registry.artifacts_for(bundle_id)}
    if artifact_id not in permitted:
        raise RegistryError(
            f"artifact {artifact_id} is not part of rule bundle {bundle_id}"
        )
    return registry.artifact(artifact_id)


def _artifact_path(registry_directory: Path, local_path: str) -> Path:
    posix = PurePosixPath(local_path)
    if posix.is_absolute() or ".." in posix.parts or "\\" in local_path:
        raise OSError("unsafe schema artifact path")
    root = registry_directory.resolve()
    candidate = root.joinpath(*posix.parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise OSError("schema artifact path escapes registry directory") from exc
    return candidate


def _is_json_media_type(value: Optional[str]) -> bool:
    if not value:
        return False
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type == "application/json" or media_type.endswith("+json")


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateSchemaKey(key)
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _preflight_json(
    value: Any, limits: SchemaExecutionLimits, *, schema_document: bool
) -> None:
    nodes = 0
    max_nodes = limits.max_schema_nodes if schema_document else limits.max_instance_nodes
    ancestors: set[int] = set()

    def visit(node: Any, pointer: str, depth: int) -> None:
        nonlocal nodes
        if depth > limits.max_depth:
            raise _ResourceLimitError("max_depth_exceeded", pointer, f"depth exceeds {limits.max_depth}")
        nodes += 1
        if nodes > max_nodes:
            name = "max_schema_nodes" if schema_document else "max_instance_nodes"
            raise _ResourceLimitError(f"{name}_exceeded", pointer, f"node count exceeds {max_nodes}")
        if isinstance(node, str):
            if len(node) > limits.max_string_length:
                raise _ResourceLimitError("max_string_length_exceeded", pointer, f"string length exceeds {limits.max_string_length}")
            return
        if node is None or isinstance(node, (bool, int)):
            return
        if isinstance(node, float):
            if not math.isfinite(node):
                raise _ResourceLimitError("non_finite_number", pointer, "NaN and infinity are not valid JSON")
            return
        if isinstance(node, Mapping):
            if len(node) > limits.max_object_members:
                raise _ResourceLimitError("max_object_members_exceeded", pointer, f"object members exceed {limits.max_object_members}")
            identity = id(node)
            if identity in ancestors:
                raise _ResourceLimitError("cyclic_document", pointer, "cyclic objects are not JSON")
            ancestors.add(identity)
            try:
                for key, child in node.items():
                    if not isinstance(key, str):
                        raise _ResourceLimitError("non_string_object_key", pointer, "JSON object keys must be strings")
                    if len(key) > limits.max_string_length:
                        raise _ResourceLimitError("max_string_length_exceeded", _join_pointer(pointer, key), f"object key length exceeds {limits.max_string_length}")
                    visit(child, _join_pointer(pointer, key), depth + 1)
            finally:
                ancestors.remove(identity)
            return
        if isinstance(node, (list, tuple)):
            if len(node) > limits.max_array_items:
                raise _ResourceLimitError("max_array_items_exceeded", pointer, f"array items exceed {limits.max_array_items}")
            identity = id(node)
            if identity in ancestors:
                raise _ResourceLimitError("cyclic_document", pointer, "cyclic arrays are not JSON")
            ancestors.add(identity)
            try:
                for index, child in enumerate(node):
                    visit(child, _join_pointer(pointer, str(index)), depth + 1)
            finally:
                ancestors.remove(identity)
            return
        raise _ResourceLimitError("non_json_value", pointer, f"{type(node).__name__} is not a JSON value")

    visit(value, "", 0)


def _first_external_reference(
    schema: Any, pointer: str = ""
) -> Optional[tuple[str, str, str]]:
    if isinstance(schema, Mapping):
        for key, value in schema.items():
            child_pointer = _join_pointer(pointer, key)
            if key in {"$ref", "$dynamicRef", "$recursiveRef"} and isinstance(value, str):
                # Any fragment-only reference stays inside the already pinned
                # schema resource (JSON Pointer or named anchor).  Everything
                # else could initiate retrieval and is rejected pre-execution.
                if not value.startswith("#"):
                    return child_pointer, key, value
            found = _first_external_reference(value, child_pointer)
            if found is not None:
                return found
    elif isinstance(schema, (list, tuple)):
        for index, value in enumerate(schema):
            found = _first_external_reference(value, _join_pointer(pointer, str(index)))
            if found is not None:
                return found
    return None


def _resolve_target(instance: Any, requested: ValidationTarget) -> ValidationTarget:
    if requested is ValidationTarget.AUTO:
        if isinstance(instance, Mapping):
            return ValidationTarget.BRIDGE
        if isinstance(instance, (list, tuple)):
            return ValidationTarget.BATCH
        raise ValueError("auto target requires an object or array")
    if requested is ValidationTarget.BRIDGE and not isinstance(instance, Mapping):
        raise ValueError("bridge target requires one JSON object")
    if requested is ValidationTarget.BATCH and not isinstance(instance, (list, tuple)):
        raise ValueError("batch target requires a JSON array")
    return requested


def _schema_root_kind(schema: Mapping[str, Any]) -> Optional[ValidationTarget]:
    declared_type = schema.get("type")
    types = {declared_type} if isinstance(declared_type, str) else set(declared_type or ()) if isinstance(declared_type, list) else set()
    if types == {"array"}:
        return ValidationTarget.BATCH
    if types == {"object"}:
        return ValidationTarget.BRIDGE
    if "items" in schema and "properties" not in schema:
        return ValidationTarget.BATCH
    if "properties" in schema or "required" in schema:
        return ValidationTarget.BRIDGE
    return None


def _iter_target_errors(validator, schema, instance, target):
    root_kind = _schema_root_kind(schema)
    if target is ValidationTarget.BATCH and root_kind is ValidationTarget.BRIDGE:
        for index, bridge in enumerate(instance):
            for error in validator.iter_errors(bridge):
                yield index, False, error
        return
    if target is ValidationTarget.BRIDGE and root_kind is ValidationTarget.BATCH:
        for error in validator.iter_errors([instance]):
            yield None, True, error
        return
    for error in validator.iter_errors(instance):
        yield None, False, error


def _collect_issues(raw_errors: Iterable[tuple[Optional[int], bool, Any]], limits: SchemaExecutionLimits):
    collected: list[SchemaValidationIssue] = []
    truncated = False
    for batch_index, unwrap_singleton, error in raw_errors:
        instance_tokens = list(getattr(error, "absolute_path", ()))
        if unwrap_singleton and instance_tokens and instance_tokens[0] == 0:
            instance_tokens = instance_tokens[1:]
        if batch_index is not None:
            instance_tokens.insert(0, batch_index)
        issue = SchemaValidationIssue(
            instance_pointer=_pointer(instance_tokens),
            schema_pointer=_pointer(getattr(error, "absolute_schema_path", ())),
            keyword=str(getattr(error, "validator", None) or "schema"),
            message=_bounded_message(str(getattr(error, "message", error)), limits.max_message_length),
        )
        if len(collected) < limits.max_issues:
            collected.append(issue)
        else:
            truncated = True
            break
    collected.sort(key=lambda item: (item.instance_pointer, item.schema_pointer, item.keyword, item.message))
    return tuple(collected), truncated


def _limit_result(exc, bundle_id, artifact_id, target, schema_sha256, limits, *, dialect=None):
    return _result(
        SchemaExecutionStatus.RESOURCE_LIMIT_EXCEEDED,
        bundle_id,
        artifact_id,
        target,
        dialect=dialect,
        schema_sha256=schema_sha256,
        issues=(SchemaValidationIssue(exc.pointer, "", exc.code, _bounded_message(exc.message, limits.max_message_length)),),
        detail="schema execution resource limit was exceeded",
    )


def _result(
    status,
    bundle_id,
    artifact_id,
    target,
    *,
    dialect=None,
    schema_sha256=None,
    issues=(),
    issues_truncated=False,
    detail,
):
    return PinnedSchemaResult(
        status=status,
        bundle_id=bundle_id,
        artifact_id=artifact_id,
        validation_target=target,
        dialect=dialect,
        schema_sha256=schema_sha256,
        issues=tuple(issues),
        issues_truncated=issues_truncated,
        detail=detail,
    )


def _pointer(tokens: Iterable[Any]) -> str:
    pointer = ""
    for token in tokens:
        pointer = _join_pointer(pointer, token)
    return pointer


def _join_pointer(parent: str, token: Any) -> str:
    escaped = str(token).replace("~", "~0").replace("/", "~1")
    return f"{parent}/{escaped}"


def _bounded_message(message: str, limit: int) -> str:
    if len(message) <= limit:
        return message
    return message[: max(0, limit - 1)] + "…"


def _safe_exception(exc: BaseException, limit: int = 512) -> str:
    message = " ".join(str(exc).split()) or type(exc).__name__
    return _bounded_message(message, limit)


__all__ = [
    "JSONSCHEMA_AVAILABLE",
    "PinnedSchemaResult",
    "SchemaExecutionLimits",
    "SchemaExecutionStatus",
    "SchemaValidationIssue",
    "ValidationTarget",
    "execute_pinned_schema",
]
