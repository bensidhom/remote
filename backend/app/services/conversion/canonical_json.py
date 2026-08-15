"""Bounded canonical JSON for BridgeGuard/SNBI integrity records.

``CANONICAL_FORMAT`` identifies the bytes produced by this module.  Version 1
implements RFC 8785 (JCS) for the JSON value domain used by BridgeGuard:

* null, booleans, Unicode strings, arrays, and string-keyed objects;
* IEEE-754 binary64 finite floats; and
* programmatic integers in the interoperable I-JSON safe range
  (``+/- (2**53 - 1)``).

The programmatic safe-integer restriction is intentionally narrower than the
full set of binary64 values.  Python integers outside that range are rejected
rather than silently rounded.  The parser can still round-trip a JCS canonical
integer-shaped representation of a larger binary64 value; it returns that
number as ``float`` and rejects a noncanonical token that would require silent
rounding.  Values needing greater integer/decimal precision must be represented
as JSON strings.  ``Decimal`` and other non-JSON numeric classes are likewise
rejected.  Within this documented domain, output uses the JCS ECMAScript number
form, UTF-16 property ordering, minimal string escaping, and UTF-8 encoding
without whitespace.  Strings are preserved as-is; Unicode normalization is
neither performed nor permitted to happen implicitly.

Every public entry point applies explicit depth, node, and byte bounds.  The
parser additionally rejects duplicate object names, invalid UTF-8, lone
surrogates, and non-finite numbers.  ``canonical_snapshot`` returns a detached,
recursively immutable copy suitable for retaining the exact value that was
hashed.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


CANONICAL_FORMAT = "rfc8785-jcs-snbi-safe-integer-v1"

_MAX_SAFE_INTEGER = (1 << 53) - 1
_HARD_MAX_DEPTH = 256
_HARD_MAX_NODES = 1_000_000
_HARD_MAX_BYTES = 64 * 1024 * 1024


def _validate_limit(name: str, value: int, minimum: int, maximum: int) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


class CanonicalJsonError(ValueError):
    """A fail-closed canonicalization or parsing error.

    ``code`` is a stable machine-readable identifier.  ``message`` is intended
    for operators, and ``json_pointer`` identifies the affected value using an
    RFC 6901 pointer when the location is known.  The empty string denotes the
    document root.
    """

    def __init__(
        self,
        code: str,
        message: str,
        json_pointer: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.json_pointer = json_pointer
        location = (
            f" at {json_pointer or '<root>'}" if json_pointer is not None else ""
        )
        super().__init__(f"{code}: {message}{location}")


@dataclass(frozen=True, slots=True)
class CanonicalJsonLimits:
    """Resource limits shared by parsing, serialization, and snapshots.

    ``max_depth`` counts nested object/array containers (a root container has
    depth one).  ``max_nodes`` counts the root and every array element/object
    value; property names are covered by ``max_bytes`` instead.  ``max_bytes``
    bounds UTF-8 input bytes when parsing and canonical UTF-8 output bytes for
    every operation.
    """

    max_depth: int = 64
    max_nodes: int = 100_000
    max_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        _validate_limit("max_depth", self.max_depth, 0, _HARD_MAX_DEPTH)
        _validate_limit("max_nodes", self.max_nodes, 1, _HARD_MAX_NODES)
        _validate_limit("max_bytes", self.max_bytes, 1, _HARD_MAX_BYTES)


DEFAULT_LIMITS = CanonicalJsonLimits()


def canonical_json_bytes(
    value: Any,
    limits: CanonicalJsonLimits = DEFAULT_LIMITS,
) -> bytes:
    """Return the versioned JCS representation of ``value`` as UTF-8 bytes."""

    checked_limits = _require_limits(limits)
    encoder = _CanonicalEncoder(checked_limits)
    encoder.encode(value, pointer="", container_depth=0)
    return b"".join(encoder.chunks)


def canonical_json_text(
    value: Any,
    limits: CanonicalJsonLimits = DEFAULT_LIMITS,
) -> str:
    """Return the canonical JSON representation as Unicode text."""

    return canonical_json_bytes(value, limits).decode("utf-8")


def canonical_sha256(
    value: Any,
    limits: CanonicalJsonLimits = DEFAULT_LIMITS,
) -> str:
    """Return the lowercase SHA-256 hex digest of the canonical UTF-8 bytes."""

    return hashlib.sha256(canonical_json_bytes(value, limits)).hexdigest()


def parse_canonical_json(
    text_or_bytes: str | bytes,
    limits: CanonicalJsonLimits = DEFAULT_LIMITS,
) -> Any:
    """Parse and validate JSON for subsequent canonicalization.

    The input need not already be in canonical form: insignificant whitespace
    and object member order are accepted.  Duplicate object names are rejected
    before a mapping can discard them.  The returned value uses ordinary
    ``dict``/``list`` containers; use :func:`canonical_snapshot` when a detached
    immutable value is required.
    """

    checked_limits = _require_limits(limits)
    text = _decode_input(text_or_bytes, checked_limits)
    _preflight_nesting(text, checked_limits)

    def reject_constant(token: str) -> Any:
        raise CanonicalJsonError(
            "non_finite_number",
            f"JSON number {token!r} is not finite",
        )

    def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CanonicalJsonError(
                    "duplicate_key",
                    f"duplicate object property {key!r}",
                )
            result[key] = value
        return result

    def parse_integer(token: str) -> int | float:
        integer = int(token)
        if -_MAX_SAFE_INTEGER <= integer <= _MAX_SAFE_INTEGER:
            return integer
        try:
            binary64 = float(token)
        except (OverflowError, ValueError) as exc:
            raise CanonicalJsonError(
                "unsafe_integer",
                "integer is not representable in the supported number domain",
            ) from exc
        # Large binary64 values can have a canonical decimal representation
        # without a decimal point (RFC 8785 Appendix B includes several).  Only
        # accept that exact representation; otherwise Python's arbitrary-size
        # integer parsing would conceal a lossy binary64 conversion.
        if not math.isfinite(binary64) or _serialize_binary64(binary64, "") != token:
            raise CanonicalJsonError(
                "unsafe_integer",
                (
                    "integer is outside the supported I-JSON safe range and "
                    "is not a canonical binary64 representation"
                ),
            )
        return binary64

    try:
        value = json.loads(
            text,
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_constant,
            parse_int=parse_integer,
        )
    except CanonicalJsonError:
        raise
    except json.JSONDecodeError as exc:
        raise CanonicalJsonError(
            "invalid_json",
            f"{exc.msg} (line {exc.lineno}, column {exc.colno})",
        ) from exc
    except (OverflowError, RecursionError, ValueError) as exc:
        raise CanonicalJsonError("invalid_json", str(exc)) from exc

    # Validation also applies the node/depth/output-byte limits and rejects
    # unsupported Python numeric values produced by the parser.
    canonical_json_bytes(value, checked_limits)
    return value


def canonical_snapshot(
    value: Any,
    limits: CanonicalJsonLimits = DEFAULT_LIMITS,
) -> Any:
    """Return a detached, recursively immutable snapshot of a JSON value.

    Objects become read-only ``MappingProxyType`` mappings and arrays become
    tuples.  Primitive JSON values retain their Python types.  Canonicalization
    of the returned snapshot is byte-for-byte identical to canonicalization of
    the captured input value.
    """

    checked_limits = _require_limits(limits)
    freezer = _SnapshotBuilder(checked_limits)
    frozen = freezer.freeze(value, pointer="", container_depth=0)
    canonical_json_bytes(frozen, checked_limits)
    return frozen


class _CanonicalEncoder:
    def __init__(self, limits: CanonicalJsonLimits) -> None:
        self.limits = limits
        self.chunks: list[bytes] = []
        self.byte_count = 0
        self.node_count = 0
        self.active_container_ids: set[int] = set()

    def encode(self, value: Any, *, pointer: str, container_depth: int) -> None:
        self.node_count += 1
        if self.node_count > self.limits.max_nodes:
            raise CanonicalJsonError(
                "max_nodes_exceeded",
                f"JSON value exceeds max_nodes={self.limits.max_nodes}",
                pointer,
            )

        if value is None:
            self._write_ascii("null", pointer)
            return
        if type(value) is bool:
            self._write_ascii("true" if value else "false", pointer)
            return
        if type(value) is int:
            if not -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER:
                raise CanonicalJsonError(
                    "unsafe_integer",
                    (
                        "integer is outside the supported I-JSON safe range "
                        f"[-{_MAX_SAFE_INTEGER}, {_MAX_SAFE_INTEGER}]; encode "
                        "higher-precision numbers as strings"
                    ),
                    pointer,
                )
            self._write_ascii(str(value), pointer)
            return
        if type(value) is float:
            self._write_ascii(_serialize_binary64(value, pointer), pointer)
            return
        if type(value) is str:
            self._write(_quote_string(value, pointer), pointer)
            return
        if isinstance(value, Mapping):
            depth = self._enter_container(value, pointer, container_depth)
            try:
                self._encode_object(value, pointer=pointer, container_depth=depth)
            finally:
                self.active_container_ids.remove(id(value))
            return
        if isinstance(value, (list, tuple)):
            depth = self._enter_container(value, pointer, container_depth)
            try:
                self._encode_array(value, pointer=pointer, container_depth=depth)
            finally:
                self.active_container_ids.remove(id(value))
            return
        raise CanonicalJsonError(
            "unsupported_type",
            f"{type(value).__name__} is not in the supported JSON value domain",
            pointer,
        )

    def _enter_container(
        self,
        value: Any,
        pointer: str,
        container_depth: int,
    ) -> int:
        depth = container_depth + 1
        if depth > self.limits.max_depth:
            raise CanonicalJsonError(
                "max_depth_exceeded",
                f"JSON value exceeds max_depth={self.limits.max_depth}",
                pointer,
            )
        marker = id(value)
        if marker in self.active_container_ids:
            raise CanonicalJsonError(
                "circular_reference",
                "JSON arrays and objects cannot contain circular references",
                pointer,
            )
        self.active_container_ids.add(marker)
        return depth

    def _encode_object(
        self,
        value: Mapping[Any, Any],
        *,
        pointer: str,
        container_depth: int,
    ) -> None:
        try:
            items = list(value.items())
        except Exception as exc:
            raise CanonicalJsonError(
                "invalid_mapping",
                "object items could not be captured deterministically",
                pointer,
            ) from exc

        seen: set[str] = set()
        checked_items: list[tuple[str, Any, bytes]] = []
        for key, child in items:
            if type(key) is not str:
                raise CanonicalJsonError(
                    "object_key_not_string",
                    "JSON object property names must be strings",
                    pointer,
                )
            sort_key = _utf16_sort_key(key, pointer)
            if key in seen:
                raise CanonicalJsonError(
                    "duplicate_key",
                    f"duplicate object property {key!r}",
                    pointer,
                )
            seen.add(key)
            checked_items.append((key, child, sort_key))
        checked_items.sort(key=lambda item: item[2])

        self._write_ascii("{", pointer)
        for index, (key, child, _sort_key) in enumerate(checked_items):
            child_pointer = _join_pointer(pointer, key)
            if index:
                self._write_ascii(",", pointer)
            self._write(_quote_string(key, child_pointer), child_pointer)
            self._write_ascii(":", child_pointer)
            self.encode(
                child,
                pointer=child_pointer,
                container_depth=container_depth,
            )
        self._write_ascii("}", pointer)

    def _encode_array(
        self,
        value: list[Any] | tuple[Any, ...],
        *,
        pointer: str,
        container_depth: int,
    ) -> None:
        # Capture mutable arrays so their iteration length/order cannot change
        # partway through this serialization.
        values = tuple(value)
        self._write_ascii("[", pointer)
        for index, child in enumerate(values):
            child_pointer = _join_pointer(pointer, str(index))
            if index:
                self._write_ascii(",", pointer)
            self.encode(
                child,
                pointer=child_pointer,
                container_depth=container_depth,
            )
        self._write_ascii("]", pointer)

    def _write_ascii(self, text: str, pointer: str) -> None:
        self._append(text.encode("ascii"), pointer)

    def _write(self, text: str, pointer: str) -> None:
        try:
            encoded = text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise CanonicalJsonError(
                "invalid_unicode",
                "JSON strings cannot contain lone UTF-16 surrogates",
                pointer,
            ) from exc
        self._append(encoded, pointer)

    def _append(self, chunk: bytes, pointer: str) -> None:
        byte_count = self.byte_count + len(chunk)
        if byte_count > self.limits.max_bytes:
            raise CanonicalJsonError(
                "max_bytes_exceeded",
                f"canonical JSON exceeds max_bytes={self.limits.max_bytes}",
                pointer,
            )
        self.byte_count = byte_count
        self.chunks.append(chunk)


class _SnapshotBuilder:
    def __init__(self, limits: CanonicalJsonLimits) -> None:
        self.limits = limits
        self.node_count = 0
        self.active_container_ids: set[int] = set()

    def freeze(self, value: Any, *, pointer: str, container_depth: int) -> Any:
        self.node_count += 1
        if self.node_count > self.limits.max_nodes:
            raise CanonicalJsonError(
                "max_nodes_exceeded",
                f"JSON value exceeds max_nodes={self.limits.max_nodes}",
                pointer,
            )

        if value is None or type(value) in (bool, int, float, str):
            return value
        if isinstance(value, Mapping):
            depth = self._enter_container(value, pointer, container_depth)
            try:
                try:
                    items = list(value.items())
                except Exception as exc:
                    raise CanonicalJsonError(
                        "invalid_mapping",
                        "object items could not be captured deterministically",
                        pointer,
                    ) from exc
                copied: dict[str, Any] = {}
                for key, child in items:
                    if type(key) is not str:
                        raise CanonicalJsonError(
                            "object_key_not_string",
                            "JSON object property names must be strings",
                            pointer,
                        )
                    if key in copied:
                        raise CanonicalJsonError(
                            "duplicate_key",
                            f"duplicate object property {key!r}",
                            pointer,
                        )
                    child_pointer = _join_pointer(pointer, key)
                    copied[key] = self.freeze(
                        child,
                        pointer=child_pointer,
                        container_depth=depth,
                    )
                return MappingProxyType(copied)
            finally:
                self.active_container_ids.remove(id(value))
        if isinstance(value, (list, tuple)):
            depth = self._enter_container(value, pointer, container_depth)
            try:
                values = tuple(value)
                return tuple(
                    self.freeze(
                        child,
                        pointer=_join_pointer(pointer, str(index)),
                        container_depth=depth,
                    )
                    for index, child in enumerate(values)
                )
            finally:
                self.active_container_ids.remove(id(value))
        raise CanonicalJsonError(
            "unsupported_type",
            f"{type(value).__name__} is not in the supported JSON value domain",
            pointer,
        )

    def _enter_container(
        self,
        value: Any,
        pointer: str,
        container_depth: int,
    ) -> int:
        depth = container_depth + 1
        if depth > self.limits.max_depth:
            raise CanonicalJsonError(
                "max_depth_exceeded",
                f"JSON value exceeds max_depth={self.limits.max_depth}",
                pointer,
            )
        marker = id(value)
        if marker in self.active_container_ids:
            raise CanonicalJsonError(
                "circular_reference",
                "JSON arrays and objects cannot contain circular references",
                pointer,
            )
        self.active_container_ids.add(marker)
        return depth


def _serialize_binary64(value: float, pointer: str) -> str:
    if not math.isfinite(value):
        raise CanonicalJsonError(
            "non_finite_number",
            "JSON numbers must be finite IEEE-754 binary64 values",
            pointer,
        )
    if value == 0.0:
        # ECMAScript JSON.stringify and RFC 8785 both canonicalize -0 to 0.
        return "0"

    rendered = repr(value).lower()
    sign = ""
    if rendered.startswith("-"):
        sign = "-"
        rendered = rendered[1:]

    if "e" not in rendered:
        if rendered.endswith(".0"):
            rendered = rendered[:-2]
        return sign + rendered

    mantissa, exponent_text = rendered.split("e", 1)
    exponent = int(exponent_text)
    digits = mantissa.replace(".", "")

    # ECMAScript uses fixed notation for decimal exponents -6 through 20.
    if -6 <= exponent < 21:
        decimal_position = exponent + 1
        if decimal_position <= 0:
            body = "0." + ("0" * -decimal_position) + digits
        elif decimal_position >= len(digits):
            body = digits + ("0" * (decimal_position - len(digits)))
        else:
            body = digits[:decimal_position] + "." + digits[decimal_position:]
        return sign + body

    scientific_mantissa = digits[0]
    if len(digits) > 1:
        scientific_mantissa += "." + digits[1:]
    exponent_sign = "+" if exponent >= 0 else "-"
    return f"{sign}{scientific_mantissa}e{exponent_sign}{abs(exponent)}"


def _quote_string(value: str, pointer: str) -> str:
    parts = ['"']
    short_escapes = {
        0x08: "\\b",
        0x09: "\\t",
        0x0A: "\\n",
        0x0C: "\\f",
        0x0D: "\\r",
    }
    for character in value:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise CanonicalJsonError(
                "invalid_unicode",
                "JSON strings cannot contain lone UTF-16 surrogates",
                pointer,
            )
        if character == '"':
            parts.append('\\"')
        elif character == "\\":
            parts.append("\\\\")
        elif codepoint in short_escapes:
            parts.append(short_escapes[codepoint])
        elif codepoint <= 0x1F:
            parts.append(f"\\u{codepoint:04x}")
        else:
            parts.append(character)
    parts.append('"')
    return "".join(parts)


def _utf16_sort_key(value: str, pointer: str) -> bytes:
    try:
        return value.encode("utf-16-be")
    except UnicodeEncodeError as exc:
        raise CanonicalJsonError(
            "invalid_unicode",
            "JSON strings cannot contain lone UTF-16 surrogates",
            pointer,
        ) from exc


def _decode_input(
    text_or_bytes: str | bytes,
    limits: CanonicalJsonLimits,
) -> str:
    if type(text_or_bytes) is bytes:
        raw = text_or_bytes
        if len(raw) > limits.max_bytes:
            raise CanonicalJsonError(
                "max_bytes_exceeded",
                f"JSON input exceeds max_bytes={limits.max_bytes}",
            )
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CanonicalJsonError(
                "invalid_utf8",
                "JSON byte input must be valid UTF-8",
            ) from exc
    if type(text_or_bytes) is str:
        try:
            raw = text_or_bytes.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise CanonicalJsonError(
                "invalid_unicode",
                "JSON text cannot contain lone UTF-16 surrogates",
            ) from exc
        if len(raw) > limits.max_bytes:
            raise CanonicalJsonError(
                "max_bytes_exceeded",
                f"JSON input exceeds max_bytes={limits.max_bytes}",
            )
        return text_or_bytes
    raise CanonicalJsonError(
        "invalid_input_type",
        "parse_canonical_json expects str or bytes input",
    )


def _preflight_nesting(text: str, limits: CanonicalJsonLimits) -> None:
    """Reject excessive container nesting before invoking the JSON parser."""

    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > limits.max_depth:
                raise CanonicalJsonError(
                    "max_depth_exceeded",
                    f"JSON value exceeds max_depth={limits.max_depth}",
                )
        elif character in "]}":
            # Syntax validity remains the JSON parser's responsibility.
            depth = max(0, depth - 1)


def _join_pointer(pointer: str, token: str) -> str:
    escaped = token.replace("~", "~0").replace("/", "~1")
    return f"{pointer}/{escaped}"


def _require_limits(limits: CanonicalJsonLimits) -> CanonicalJsonLimits:
    if not isinstance(limits, CanonicalJsonLimits):
        raise TypeError("limits must be a CanonicalJsonLimits instance")
    return limits


__all__ = [
    "CANONICAL_FORMAT",
    "CanonicalJsonError",
    "CanonicalJsonLimits",
    "DEFAULT_LIMITS",
    "canonical_json_bytes",
    "canonical_json_text",
    "canonical_sha256",
    "canonical_snapshot",
    "parse_canonical_json",
]
