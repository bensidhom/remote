"""Non-destructive canonical SNBI document normalization.

This module is intentionally *not* an FHWA schema validator.  The governing
FHWA JSON Schema artifacts have not yet been vendored and hash-verified, so all
results remain unavailable/unverified and never submission-ready.  The module
provides a strict shape boundary and complete diagnostics without silently
discarding source content.
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


class SchemaStatus(str, Enum):
    UNAVAILABLE_UNVERIFIED = "unavailable_unverified"


class DiagnosticSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class CanonicalResourceLimitError(ValueError):
    """Raised before copying when a document exceeds configured safety limits."""

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(f"{code} at {path or '/'}: {message}")
        self.code = code
        self.path = path
        self.message = message


@dataclass(frozen=True)
class NormalizationLimits:
    max_depth: int = 32
    max_nodes: int = 100_000
    max_string_length: int = 1_000_000
    max_array_items: int = 100_000
    max_object_members: int = 100_000

    def validate(self) -> None:
        for name in (
            "max_depth",
            "max_nodes",
            "max_string_length",
            "max_array_items",
            "max_object_members",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class UnknownTag:
    path: str
    tag: str
    official_looking: bool
    severity: DiagnosticSeverity
    value: Any
    message: str


@dataclass(frozen=True)
class WrongContainerType:
    path: str
    expected: str
    actual: str
    severity: DiagnosticSeverity
    value: Any


@dataclass(frozen=True)
class Coercion:
    path: str
    kind: str
    original_value: Any
    normalized_value: Any


@dataclass(frozen=True)
class RejectedValue:
    path: str
    code: str
    severity: DiagnosticSeverity
    value: Any
    message: str


@dataclass(frozen=True)
class DuplicateLogicalChildKey:
    container_path: str
    logical_key_fields: Tuple[str, ...]
    logical_key_values: Tuple[Any, ...]
    first_path: str
    duplicate_path: str
    severity: DiagnosticSeverity


@dataclass(frozen=True)
class CanonicalNormalizationResult:
    original_document: Any
    normalized_document: Dict[str, Any]
    unknown_tags: Tuple[UnknownTag, ...]
    wrong_container_types: Tuple[WrongContainerType, ...]
    coercions: Tuple[Coercion, ...]
    rejected_values: Tuple[RejectedValue, ...]
    duplicate_logical_child_keys: Tuple[DuplicateLogicalChildKey, ...]
    profile_id: str
    schema_status: SchemaStatus
    submission_ready: bool
    node_count: int

    @property
    def has_errors(self) -> bool:
        return bool(
            self.unknown_tags
            or self.wrong_container_types
            or self.rejected_values
            or self.duplicate_logical_child_keys
        )


TOP_LEVEL_FIELDS = frozenset(
    {
        "BL01", "BID01", "BID02", "BID03", "BL02", "BL03", "BL04", "BL05",
        "BL06", "BL07", "BL08", "BL09", "BL10", "BL11", "BL12", "BCL01",
        "BCL02", "BCL03", "BCL04", "BCL05", "BCL06", "BRH01", "BRH02",
        "BG01", "BG02", "BG03", "BG04", "BG05", "BG06", "BG07", "BG08",
        "BG09", "BG10", "BG11", "BG12", "BG13", "BG14", "BG15", "BG16",
        "BLR01", "BLR02", "BLR03", "BLR04", "BLR05", "BLR06", "BLR07",
        "BLR08", "BIR01", "BIR02", "BIR03", "BIR04", "BC01", "BC02",
        "BC03", "BC04", "BC05", "BC06", "BC07", "BC08", "BC09", "BC10",
        "BC11", "BC12", "BC13", "BC14", "BC15", "BAP01", "BAP02", "BAP03",
        "BAP04", "BAP05", "BW01",
    }
)


@dataclass(frozen=True)
class _ArraySpec:
    fields: frozenset[str]
    logical_key: Tuple[str, ...]
    allows_routes: bool = False


ARRAY_SPECS: Mapping[str, _ArraySpec] = {
    "Elements": _ArraySpec(
        frozenset({"BE01", "BE02", "BE03", "BCS01", "BCS02", "BCS03", "BCS04"}),
        ("BE01", "BE02"),
    ),
    "Features": _ArraySpec(
        frozenset(
            {
                "BF01", "BF02", "BF03", "BH01", "BH02", "BH03", "BH04", "BH05",
                "BH06", "BH07", "BH08", "BH09", "BH10", "BH11", "BH12", "BH13",
                "BH14", "BH15", "BH16", "BH17", "BH18", "BRR01", "BRR02", "BRR03",
                "BN01", "BN02", "BN03", "BN04", "BN05", "BN06",
            }
        ),
        ("BF01",),
        allows_routes=True,
    ),
    "Inspections": _ArraySpec(
        frozenset(
            {
                "BIE01", "BIE02", "BIE03", "BIE04", "BIE05", "BIE06", "BIE07",
                "BIE08", "BIE09", "BIE10", "BIE11", "BIE12",
            }
        ),
        ("BIE01", "BIE02"),
    ),
    "PostingEvaluations": _ArraySpec(
        frozenset({"BEP01", "BEP02", "BEP03", "BEP04"}),
        ("BEP01",),
    ),
    "PostingStatuses": _ArraySpec(
        frozenset({"BPS01", "BPS02"}),
        ("BPS02",),
    ),
    "SpanSets": _ArraySpec(
        frozenset(
            {
                "BSP01", "BSP02", "BSP03", "BSP04", "BSP05", "BSP06", "BSP07",
                "BSP08", "BSP09", "BSP10", "BSP11", "BSP12", "BSP13",
            }
        ),
        ("BSP01",),
    ),
    "SubstructureSets": _ArraySpec(
        frozenset({"BSB01", "BSB02", "BSB03", "BSB04", "BSB05", "BSB06", "BSB07"}),
        ("BSB01",),
    ),
    "Works": _ArraySpec(frozenset({"BW02", "BW03"}), ("BW02",)),
}

ROUTE_SPEC = _ArraySpec(
    frozenset({"BRT01", "BRT02", "BRT03", "BRT04", "BRT05"}),
    ("BRT01",),
)

_NAMESPACE_RE = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9+.-]*:[^\s]+|(?:[A-Za-z0-9-]+\.)+[A-Za-z0-9_-]+)$"
)


def normalize_canonical_document(
    document: Any,
    *,
    profile_id: str,
    limits: NormalizationLimits = NormalizationLimits(),
) -> CanonicalNormalizationResult:
    """Normalize one canonical bridge document without mutating or losing input."""

    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError("profile_id must be a non-empty string")
    limits.validate()
    node_count = _preflight(document, limits)
    original = copy.deepcopy(document)
    normalizer = _Normalizer(profile_id.strip())
    normalized = normalizer.normalize_root(document)
    return CanonicalNormalizationResult(
        original_document=original,
        normalized_document=normalized,
        unknown_tags=tuple(normalizer.unknown_tags),
        wrong_container_types=tuple(normalizer.wrong_container_types),
        coercions=tuple(normalizer.coercions),
        rejected_values=tuple(normalizer.rejected_values),
        duplicate_logical_child_keys=tuple(normalizer.duplicate_keys),
        profile_id=profile_id.strip(),
        schema_status=SchemaStatus.UNAVAILABLE_UNVERIFIED,
        submission_ready=False,
        node_count=node_count,
    )


class _Normalizer:
    def __init__(self, profile_id: str) -> None:
        self.profile_id = profile_id
        self.unknown_tags: List[UnknownTag] = []
        self.wrong_container_types: List[WrongContainerType] = []
        self.coercions: List[Coercion] = []
        self.rejected_values: List[RejectedValue] = []
        self.duplicate_keys: List[DuplicateLogicalChildKey] = []

    def normalize_root(self, value: Any) -> Dict[str, Any]:
        if not isinstance(value, dict):
            self._wrong_container("", "object", value)
            self._reject("", "root_not_object", value, "canonical bridge document must be an object")
            return {}

        output: Dict[str, Any] = {}
        for key, child in value.items():
            path = _join_pointer("", key)
            if not isinstance(key, str):
                self._reject(path, "non_string_object_key", child, "JSON object keys must be strings")
            elif key in TOP_LEVEL_FIELDS:
                accepted, normalized = self._normalize_scalar(child, path)
                if accepted:
                    output[key] = normalized
            elif key in ARRAY_SPECS:
                accepted, normalized = self._normalize_array(child, path, ARRAY_SPECS[key])
                if accepted:
                    output[key] = normalized
            elif key == "extensions":
                accepted, normalized = self._normalize_extensions(child, path)
                if accepted:
                    output[key] = normalized
            else:
                self._unknown(key, path, child)
        return output

    def _normalize_array(
        self, value: Any, path: str, spec: _ArraySpec
    ) -> Tuple[bool, List[Dict[str, Any]]]:
        if not isinstance(value, list):
            self._wrong_container(path, "array", value)
            self._reject(path, "array_type_mismatch", value, "container must be an array; objects are never wrapped")
            return False, []

        output: List[Dict[str, Any]] = []
        first_keys: Dict[Tuple[Any, ...], str] = {}
        for index, row in enumerate(value):
            row_path = _join_pointer(path, str(index))
            if not isinstance(row, dict):
                self._wrong_container(row_path, "object", row)
                self._reject(row_path, "array_item_type_mismatch", row, "array item must be an object")
                continue
            normalized_row = self._normalize_row(row, row_path, spec)
            output.append(normalized_row)
            logical_key = _logical_key(normalized_row, spec.logical_key)
            if logical_key is None:
                continue
            first_path = first_keys.get(logical_key)
            if first_path is None:
                first_keys[logical_key] = row_path
            else:
                self.duplicate_keys.append(
                    DuplicateLogicalChildKey(
                        container_path=path,
                        logical_key_fields=spec.logical_key,
                        logical_key_values=logical_key,
                        first_path=first_path,
                        duplicate_path=row_path,
                        severity=DiagnosticSeverity.ERROR,
                    )
                )
        return True, output

    def _normalize_row(
        self, row: Mapping[Any, Any], path: str, spec: _ArraySpec
    ) -> Dict[str, Any]:
        output: Dict[str, Any] = {}
        for key, value in row.items():
            child_path = _join_pointer(path, key)
            if not isinstance(key, str):
                self._reject(child_path, "non_string_object_key", value, "JSON object keys must be strings")
            elif key in spec.fields:
                accepted, normalized = self._normalize_scalar(value, child_path)
                if accepted:
                    output[key] = normalized
            elif key == "Routes" and spec.allows_routes:
                accepted, normalized = self._normalize_array(value, child_path, ROUTE_SPEC)
                if accepted:
                    output[key] = normalized
            elif key == "extensions":
                accepted, normalized = self._normalize_extensions(value, child_path)
                if accepted:
                    output[key] = normalized
            else:
                self._unknown(str(key), child_path, value)
        return output

    def _normalize_scalar(self, value: Any, path: str) -> Tuple[bool, Any]:
        if isinstance(value, (dict, list)):
            self._wrong_container(path, "JSON scalar", value)
            self._reject(path, "scalar_type_mismatch", value, "official tag value must be a JSON scalar")
            return False, None
        if isinstance(value, float) and not math.isfinite(value):
            self._reject(path, "non_finite_number", value, "NaN and infinity are not valid JSON numbers")
            return False, None
        if not isinstance(value, (str, int, float, bool, type(None))):
            self._reject(path, "non_json_scalar", value, "value is not a JSON scalar")
            return False, None
        if isinstance(value, str):
            normalized = value.strip()
            if normalized != value:
                self.coercions.append(Coercion(path, "trim_string", copy.deepcopy(value), normalized))
            if normalized == "":
                self.coercions.append(Coercion(path, "blank_string_to_null", normalized, None))
                return True, None
            return True, normalized
        return True, value

    def _normalize_extensions(
        self, value: Any, path: str
    ) -> Tuple[bool, Dict[str, Any]]:
        if not isinstance(value, dict):
            self._wrong_container(path, "object", value)
            self._reject(path, "extensions_type_mismatch", value, "extensions must be an object keyed by namespace")
            return False, {}
        output: Dict[str, Any] = {}
        for namespace, extension_value in value.items():
            namespace_path = _join_pointer(path, namespace)
            if not isinstance(namespace, str) or not _NAMESPACE_RE.fullmatch(namespace):
                self._reject(
                    namespace_path,
                    "invalid_extension_namespace",
                    extension_value,
                    "extension key must be a URI-style or reverse-domain namespace",
                )
                continue
            invalid = _first_non_json_value(extension_value, namespace_path)
            if invalid is not None:
                invalid_path, invalid_value = invalid
                self._reject(invalid_path, "non_json_extension_value", invalid_value, "extension contains a non-JSON value")
                continue
            output[namespace] = copy.deepcopy(extension_value)
        return True, output

    def _unknown(self, key: str, path: str, value: Any) -> None:
        official = key.startswith("B")
        message = (
            "unknown official-looking SNBI tag"
            if official
            else "unrecognized field must be placed under a namespaced extensions object"
        )
        self.unknown_tags.append(
            UnknownTag(path, key, official, DiagnosticSeverity.ERROR, copy.deepcopy(value), message)
        )

    def _wrong_container(self, path: str, expected: str, value: Any) -> None:
        self.wrong_container_types.append(
            WrongContainerType(
                path=path,
                expected=expected,
                actual=_json_type_name(value),
                severity=DiagnosticSeverity.ERROR,
                value=copy.deepcopy(value),
            )
        )

    def _reject(self, path: str, code: str, value: Any, message: str) -> None:
        self.rejected_values.append(
            RejectedValue(path, code, DiagnosticSeverity.ERROR, copy.deepcopy(value), message)
        )


def _preflight(value: Any, limits: NormalizationLimits) -> int:
    node_count = 0
    ancestors: set[int] = set()

    def visit(node: Any, path: str, depth: int) -> None:
        nonlocal node_count
        if depth > limits.max_depth:
            raise CanonicalResourceLimitError(
                "max_depth_exceeded", path, f"depth exceeds {limits.max_depth}"
            )
        node_count += 1
        if node_count > limits.max_nodes:
            raise CanonicalResourceLimitError(
                "max_nodes_exceeded", path, f"node count exceeds {limits.max_nodes}"
            )
        if isinstance(node, str) and len(node) > limits.max_string_length:
            raise CanonicalResourceLimitError(
                "max_string_length_exceeded",
                path,
                f"string length exceeds {limits.max_string_length}",
            )
        if isinstance(node, dict):
            if len(node) > limits.max_object_members:
                raise CanonicalResourceLimitError(
                    "max_object_members_exceeded",
                    path,
                    f"object member count exceeds {limits.max_object_members}",
                )
            identity = id(node)
            if identity in ancestors:
                raise CanonicalResourceLimitError("cyclic_document", path, "cyclic objects are not valid JSON")
            ancestors.add(identity)
            try:
                for key, child in node.items():
                    if isinstance(key, str) and len(key) > limits.max_string_length:
                        raise CanonicalResourceLimitError(
                            "max_string_length_exceeded",
                            _join_pointer(path, key),
                            f"object key length exceeds {limits.max_string_length}",
                        )
                    visit(child, _join_pointer(path, key), depth + 1)
            finally:
                ancestors.remove(identity)
        elif isinstance(node, list):
            if len(node) > limits.max_array_items:
                raise CanonicalResourceLimitError(
                    "max_array_items_exceeded",
                    path,
                    f"array item count exceeds {limits.max_array_items}",
                )
            identity = id(node)
            if identity in ancestors:
                raise CanonicalResourceLimitError("cyclic_document", path, "cyclic arrays are not valid JSON")
            ancestors.add(identity)
            try:
                for index, child in enumerate(node):
                    visit(child, _join_pointer(path, str(index)), depth + 1)
            finally:
                ancestors.remove(identity)

    visit(value, "", 0)
    return node_count


def _first_non_json_value(value: Any, path: str) -> Optional[Tuple[str, Any]]:
    if isinstance(value, float) and not math.isfinite(value):
        return path, value
    if isinstance(value, (str, int, float, bool, type(None))):
        return None
    if isinstance(value, list):
        for index, child in enumerate(value):
            invalid = _first_non_json_value(child, _join_pointer(path, str(index)))
            if invalid is not None:
                return invalid
        return None
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                return _join_pointer(path, key), child
            invalid = _first_non_json_value(child, _join_pointer(path, key))
            if invalid is not None:
                return invalid
        return None
    return path, value


def _logical_key(
    row: Mapping[str, Any], fields: Sequence[str]
) -> Optional[Tuple[Any, ...]]:
    values: List[Any] = []
    for field in fields:
        value = row.get(field)
        if value is None:
            return None
        values.append(value)
    return tuple(values)


def _join_pointer(parent: str, token: Any) -> str:
    text = str(token).replace("~", "~0").replace("/", "~1")
    return f"{parent}/{text}"


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__
