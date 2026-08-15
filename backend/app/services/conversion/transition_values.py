"""Typed values and exact numeric primitives for versioned NBI transitions.

The legacy mapper remains unchanged. This module provides immutable lineage and
``Decimal``-only helpers that new profile-specific rules can adopt one field at
a time without conflating blank, invalid, out-of-range, and true zero values.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence


TRANSFORM_PRIMITIVES_VERSION = "transition-primitives-v1"
H01_BH12_RULE_ID = "fhwa-crosswalk-h01-bh12-item10-round-down-v1"


class ParsedStatus(str, Enum):
    VALID = "valid"
    BLANK = "blank"
    INVALID = "invalid"
    OUT_OF_RANGE = "out_of_range"


class TransitionCategory(str, Enum):
    DIRECT = "direct"
    CODE_MAP = "code_map"
    UNIT = "unit"
    DERIVED = "derived"
    STRUCTURAL = "structural"
    TEMPORARY = "temporary"
    NO_TRANSITION = "no_transition"
    APP_PROJECTION = "app_projection"


class TransitionStatus(str, Enum):
    POPULATED = "populated"
    NULL_SOURCE = "null_source"
    INVALID_SOURCE = "invalid_source"
    TEMPORARY = "temporary"
    ASSUMED = "assumed"
    GENERATED = "generated"
    REVIEW_REQUIRED = "review_required"


class RoundingPolicy(str, Enum):
    EXACT = "exact"
    HALF_UP_WHOLE = "half_up_whole"
    HALF_UP_TENTH = "half_up_tenth"
    HALF_UP_HUNDREDTH = "half_up_hundredth"
    HALF_UP_THOUSANDTH = "half_up_thousandth"
    ROUND_DOWN_TENTH = "round_down_tenth"


class CapMode(str, Enum):
    CLAMP = "clamp"
    NULL = "null"
    REVIEW_REQUIRED = "review_required"


class CapDomain(str, Enum):
    SOURCE = "source"
    TARGET = "target"


@dataclass(frozen=True, slots=True)
class DMSCoordinate:
    degrees: int
    minutes: int
    seconds: Decimal
    sign: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.degrees, bool) or not isinstance(self.degrees, int):
            raise TypeError("degrees must be an integer")
        if isinstance(self.minutes, bool) or not isinstance(self.minutes, int):
            raise TypeError("minutes must be an integer")
        if self.sign not in {-1, 1}:
            raise ValueError("sign must be -1 or 1")
        _require_finite_decimal(self.seconds, field="seconds")


@dataclass(frozen=True, slots=True)
class ParsedScalar:
    raw: Any
    value: Decimal | int | str | DMSCoordinate | None
    status: ParsedStatus

    def __post_init__(self) -> None:
        status = _coerce_enum(self.status, ParsedStatus, "status")
        object.__setattr__(self, "status", status)
        if isinstance(self.value, Decimal):
            _require_finite_decimal(self.value, field="value")
        if status in {ParsedStatus.BLANK, ParsedStatus.INVALID} and self.value is not None:
            raise ValueError(f"{status.value} parsed values must be null")
        if status in {ParsedStatus.VALID, ParsedStatus.OUT_OF_RANGE} and self.value is None:
            raise ValueError(f"{status.value} parsed values must retain a value")

    def to_lineage_dict(self) -> dict[str, Any]:
        return {
            "raw": _json_safe(self.raw),
            "value": _json_safe(self.value),
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class SourceField:
    tag: str
    raw: Any
    parsed: ParsedScalar

    def __post_init__(self) -> None:
        tag = str(self.tag).strip()
        if not tag:
            raise ValueError("source field tag is required")
        if not isinstance(self.parsed, ParsedScalar):
            raise TypeError("parsed must be a ParsedScalar")
        object.__setattr__(self, "tag", tag)

    @classmethod
    def from_parsed(cls, tag: str, parsed: ParsedScalar) -> "SourceField":
        return cls(tag=tag, raw=parsed.raw, parsed=parsed)

    def to_lineage_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "raw": _json_safe(self.raw),
            "parsed": {
                "value": _json_safe(self.parsed.value),
                "status": self.parsed.status.value,
            },
        }


@dataclass(frozen=True, slots=True)
class TransitionSourceReference:
    """Exact source-byte and row/field locator for a research transition.

    This is provenance for the source material consulted by the application;
    ``execution_verified`` is intentionally always false because matching a
    transition to a reviewed workbook row is not FHWA certification.
    """

    source_artifact_id: str
    source_artifact_sha256: str
    source_locator: str
    field_locator: str
    legacy_item_locator: str
    rule_locator: str
    target_tag: str
    legacy_item_id: str | None
    record_kind: str | None
    source_field_tags: tuple[str, ...]
    derived_dataset_version: str
    derived_dataset_sha256: str
    authority_status: str = "informational_verified_source_bytes_not_executed"
    reference_kind: str = "transition_crosswalk"
    execution_verified: bool = False

    def __post_init__(self) -> None:
        required = {
            "source_artifact_id": self.source_artifact_id,
            "source_artifact_sha256": self.source_artifact_sha256,
            "source_locator": self.source_locator,
            "field_locator": self.field_locator,
            "legacy_item_locator": self.legacy_item_locator,
            "rule_locator": self.rule_locator,
            "target_tag": self.target_tag,
            "derived_dataset_version": self.derived_dataset_version,
            "derived_dataset_sha256": self.derived_dataset_sha256,
            "authority_status": self.authority_status,
            "reference_kind": self.reference_kind,
        }
        for name, raw in required.items():
            value = str(raw).strip()
            if not value:
                raise ValueError(f"{name} is required")
            object.__setattr__(self, name, value)
        for name in ("source_artifact_sha256", "derived_dataset_sha256"):
            value = getattr(self, name)
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"{name} must be a lowercase SHA-256 value")
        if not isinstance(self.execution_verified, bool):
            raise TypeError("execution_verified must be a boolean")
        legacy_item_id = (
            str(self.legacy_item_id).strip()
            if self.legacy_item_id is not None
            else None
        )
        record_kind = (
            str(self.record_kind).strip() if self.record_kind is not None else None
        )
        source_field_tags = tuple(str(value).strip() for value in self.source_field_tags)
        if any(not value for value in source_field_tags):
            raise ValueError("source_field_tags cannot contain blanks")
        object.__setattr__(self, "legacy_item_id", legacy_item_id)
        object.__setattr__(self, "record_kind", record_kind)
        object.__setattr__(self, "source_field_tags", source_field_tags)

    def to_lineage_dict(self) -> dict[str, Any]:
        return {
            "source_artifact_id": self.source_artifact_id,
            "source_artifact_sha256": self.source_artifact_sha256,
            "source_locator": self.source_locator,
            "field_locator": self.field_locator,
            "legacy_item_locator": self.legacy_item_locator,
            "rule_locator": self.rule_locator,
            "target_tag": self.target_tag,
            "legacy_item_id": self.legacy_item_id,
            "record_kind": self.record_kind,
            "source_field_tags": list(self.source_field_tags),
            "derived_dataset_version": self.derived_dataset_version,
            "derived_dataset_sha256": self.derived_dataset_sha256,
            "authority_status": self.authority_status,
            "reference_kind": self.reference_kind,
            "execution_verified": self.execution_verified,
        }


@dataclass(frozen=True, slots=True)
class UnitConversion:
    source_unit: str
    target_unit: str
    constant: Decimal
    definition: str

    def __post_init__(self) -> None:
        source_unit = str(self.source_unit).strip()
        target_unit = str(self.target_unit).strip()
        definition = str(self.definition).strip()
        constant = _decimal_literal(self.constant, field="constant")
        if not source_unit or not target_unit or not definition:
            raise ValueError("unit names and conversion definition are required")
        if constant <= 0:
            raise ValueError("unit conversion constant must be positive")
        object.__setattr__(self, "source_unit", source_unit)
        object.__setattr__(self, "target_unit", target_unit)
        object.__setattr__(self, "definition", definition)
        object.__setattr__(self, "constant", constant)

    def to_lineage_dict(self) -> dict[str, str]:
        return {
            "source_unit": self.source_unit,
            "target_unit": self.target_unit,
            "constant": decimal_to_json(self.constant),
            "definition": self.definition,
        }


@dataclass(frozen=True, slots=True)
class CapSemantics:
    """An explicit threshold action.

    ``minimum_inclusive``/``maximum_inclusive`` mean the cap triggers at the
    equal boundary. A replacement declares whether that number is expressed in
    source or target units; it is never inferred from the threshold domain.
    """

    minimum: Decimal | None = None
    maximum: Decimal | None = None
    mode: CapMode = CapMode.CLAMP
    reason: str = ""
    domain: CapDomain = CapDomain.TARGET
    minimum_inclusive: bool = False
    maximum_inclusive: bool = False
    replacement: Decimal | None = None
    replacement_domain: CapDomain | None = None

    def __post_init__(self) -> None:
        mode = _coerce_enum(self.mode, CapMode, "mode")
        domain = _coerce_enum(self.domain, CapDomain, "domain")
        minimum = (
            _decimal_literal(self.minimum, field="minimum")
            if self.minimum is not None
            else None
        )
        maximum = (
            _decimal_literal(self.maximum, field="maximum")
            if self.maximum is not None
            else None
        )
        replacement = (
            _decimal_literal(self.replacement, field="replacement")
            if self.replacement is not None
            else None
        )
        replacement_domain = (
            _coerce_enum(
                self.replacement_domain,
                CapDomain,
                "replacement_domain",
            )
            if self.replacement_domain is not None
            else None
        )
        reason = str(self.reason).strip()
        if minimum is None and maximum is None:
            raise ValueError("a cap requires a minimum or maximum")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("cap minimum cannot exceed maximum")
        if not reason:
            raise ValueError("cap reason is required")
        if replacement is not None and mode is not CapMode.CLAMP:
            raise ValueError("a replacement is allowed only for clamp caps")
        if (replacement is None) != (replacement_domain is None):
            raise ValueError(
                "replacement and replacement_domain must be declared together"
            )
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "domain", domain)
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)
        object.__setattr__(self, "replacement", replacement)
        object.__setattr__(self, "replacement_domain", replacement_domain)
        object.__setattr__(self, "reason", reason)

    def to_lineage_dict(self) -> dict[str, Any]:
        return {
            "minimum": _json_safe(self.minimum),
            "maximum": _json_safe(self.maximum),
            "mode": self.mode.value,
            "reason": self.reason,
            "domain": self.domain.value,
            "minimum_inclusive": self.minimum_inclusive,
            "maximum_inclusive": self.maximum_inclusive,
            "replacement": _json_safe(self.replacement),
            "replacement_domain": (
                self.replacement_domain.value
                if self.replacement_domain is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class TransitionValue:
    value: Decimal | int | str | bool | None
    target_tag: str
    rule_id: str
    category: TransitionCategory
    source_record_ids: tuple[str, ...] = ()
    source_fields: tuple[SourceField, ...] = ()
    transform_version: str = TRANSFORM_PRIMITIVES_VERSION
    rounding_policy: RoundingPolicy = RoundingPolicy.EXACT
    status: TransitionStatus = TransitionStatus.POPULATED
    messages: tuple[str, ...] = ()
    assumption_id: str | None = None
    unit_conversion: UnitConversion | None = None
    cap_semantics: CapSemantics | None = None
    source_references: tuple[TransitionSourceReference, ...] = ()

    def __post_init__(self) -> None:
        target_tag = str(self.target_tag).strip()
        rule_id = str(self.rule_id).strip()
        transform_version = str(self.transform_version).strip()
        if not target_tag or not rule_id or not transform_version:
            raise ValueError("target_tag, rule_id, and transform_version are required")
        if isinstance(self.value, Decimal):
            _require_finite_decimal(self.value, field="value")
        if isinstance(self.value, float):
            raise TypeError("TransitionValue numeric values must use Decimal, not float")
        category = _coerce_enum(self.category, TransitionCategory, "category")
        rounding = _coerce_enum(
            self.rounding_policy, RoundingPolicy, "rounding_policy"
        )
        status = _coerce_enum(self.status, TransitionStatus, "status")
        source_record_ids = tuple(str(item).strip() for item in self.source_record_ids)
        source_fields = tuple(self.source_fields)
        messages = tuple(str(item) for item in self.messages)
        assumption_id = (
            str(self.assumption_id).strip()
            if self.assumption_id is not None
            else None
        )
        if any(not item for item in source_record_ids):
            raise ValueError("source record IDs cannot be blank")
        if any(not isinstance(item, SourceField) for item in source_fields):
            raise TypeError("source_fields must contain SourceField values")
        source_references = tuple(self.source_references)
        if any(
            not isinstance(item, TransitionSourceReference)
            for item in source_references
        ):
            raise TypeError(
                "source_references must contain TransitionSourceReference values"
            )
        if status in {
            TransitionStatus.NULL_SOURCE,
            TransitionStatus.INVALID_SOURCE,
        } and self.value is not None:
            raise ValueError(f"{status.value} transition values must be null")
        if status is TransitionStatus.POPULATED and self.value is None:
            raise ValueError("populated transition values cannot be null")
        if category is TransitionCategory.UNIT and self.unit_conversion is None:
            raise ValueError("unit transitions require unit conversion metadata")
        if status is TransitionStatus.ASSUMED and not assumption_id:
            raise ValueError("assumed transition values require assumption_id")
        if status is not TransitionStatus.ASSUMED and assumption_id is not None:
            raise ValueError("assumption_id is only valid for assumed transitions")
        object.__setattr__(self, "target_tag", target_tag)
        object.__setattr__(self, "rule_id", rule_id)
        object.__setattr__(self, "transform_version", transform_version)
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "rounding_policy", rounding)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "source_record_ids", source_record_ids)
        object.__setattr__(self, "source_fields", source_fields)
        object.__setattr__(self, "messages", messages)
        object.__setattr__(self, "assumption_id", assumption_id)
        object.__setattr__(self, "source_references", source_references)

    def to_lineage_dict(self) -> dict[str, Any]:
        return {
            "value": _json_safe(self.value),
            "target_tag": self.target_tag,
            "rule_id": self.rule_id,
            "category": self.category.value,
            "source_record_ids": list(self.source_record_ids),
            "source_fields": [item.to_lineage_dict() for item in self.source_fields],
            "transform_version": self.transform_version,
            "rounding_policy": self.rounding_policy.value,
            "status": self.status.value,
            "messages": list(self.messages),
            "assumption_id": self.assumption_id,
            "unit_conversion": (
                self.unit_conversion.to_lineage_dict()
                if self.unit_conversion is not None
                else None
            ),
            "cap_semantics": (
                self.cap_semantics.to_lineage_dict()
                if self.cap_semantics is not None
                else None
            ),
            "rule_source_binding_status": (
                "exact_vendored_crosswalk_reference"
                if any(
                    item.reference_kind == "transition_crosswalk"
                    for item in self.source_references
                )
                else "no_crosswalk_field_reference"
            ),
            "rule_source_references": [
                item.to_lineage_dict() for item in self.source_references
            ],
        }


def parse_decimal(
    raw: Any,
    *,
    minimum: Decimal | int | str | None = None,
    maximum: Decimal | int | str | None = None,
) -> ParsedScalar:
    """Parse an exact finite decimal without providing a numeric default."""

    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ParsedScalar(raw=raw, value=None, status=ParsedStatus.BLANK)
    if isinstance(raw, bool):
        return ParsedScalar(raw=raw, value=None, status=ParsedStatus.INVALID)
    try:
        if isinstance(raw, Decimal):
            value = raw
        elif isinstance(raw, (str, int, float)):
            value = Decimal(str(raw).strip())
        else:
            return ParsedScalar(raw=raw, value=None, status=ParsedStatus.INVALID)
    except (InvalidOperation, ValueError):
        return ParsedScalar(raw=raw, value=None, status=ParsedStatus.INVALID)
    if not value.is_finite():
        return ParsedScalar(raw=raw, value=None, status=ParsedStatus.INVALID)

    lower = _optional_boundary(minimum, field="minimum")
    upper = _optional_boundary(maximum, field="maximum")
    if lower is not None and upper is not None and lower > upper:
        raise ValueError("minimum cannot exceed maximum")
    status = (
        ParsedStatus.OUT_OF_RANGE
        if (lower is not None and value < lower)
        or (upper is not None and value > upper)
        else ParsedStatus.VALID
    )
    return ParsedScalar(raw=raw, value=value, status=status)


def parse_integer(
    raw: Any,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> ParsedScalar:
    """Parse an integer while preserving blank, invalid, range, and zero states."""

    parsed = parse_decimal(raw)
    if parsed.status in {ParsedStatus.BLANK, ParsedStatus.INVALID}:
        return parsed
    assert isinstance(parsed.value, Decimal)
    integral = parsed.value.to_integral_value()
    if parsed.value != integral:
        return ParsedScalar(raw=raw, value=None, status=ParsedStatus.INVALID)
    value = int(integral)
    status = (
        ParsedStatus.OUT_OF_RANGE
        if (minimum is not None and value < minimum)
        or (maximum is not None and value > maximum)
        else ParsedStatus.VALID
    )
    return ParsedScalar(raw=raw, value=value, status=status)


def quantize_decimal(
    value: Decimal | int | str,
    policy: RoundingPolicy,
    *,
    rule_id: str | None = None,
) -> Decimal:
    """Apply an explicit rounding policy using Decimal arithmetic only."""

    decimal_value = _decimal_literal(value, field="value")
    policy = _coerce_enum(policy, RoundingPolicy, "policy")
    if policy is RoundingPolicy.EXACT:
        return decimal_value
    if policy is RoundingPolicy.HALF_UP_WHOLE:
        return decimal_value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if policy is RoundingPolicy.HALF_UP_TENTH:
        return decimal_value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if policy is RoundingPolicy.HALF_UP_HUNDREDTH:
        return decimal_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if policy is RoundingPolicy.HALF_UP_THOUSANDTH:
        return decimal_value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    if policy is RoundingPolicy.ROUND_DOWN_TENTH:
        if rule_id != H01_BH12_RULE_ID:
            raise ValueError(
                "round-down is restricted to the named H01 BH12 transition rule"
            )
        return decimal_value.quantize(Decimal("0.1"), rounding=ROUND_FLOOR)
    raise AssertionError(f"unhandled rounding policy: {policy}")


def direct_decimal(
    parsed: ParsedScalar,
    *,
    target_tag: str,
    rule_id: str,
    source_tag: str,
    source_record_ids: Sequence[str] = (),
    rounding_policy: RoundingPolicy = RoundingPolicy.EXACT,
    transform_version: str = TRANSFORM_PRIMITIVES_VERSION,
) -> TransitionValue:
    """Build a direct numeric transition with complete parsed-source lineage."""

    return _numeric_transition(
        parsed,
        target_tag=target_tag,
        rule_id=rule_id,
        source_tag=source_tag,
        source_record_ids=source_record_ids,
        category=TransitionCategory.DIRECT,
        rounding_policy=rounding_policy,
        transform_version=transform_version,
    )


def convert_unit(
    parsed: ParsedScalar,
    *,
    target_tag: str,
    rule_id: str,
    source_tag: str,
    conversion: UnitConversion,
    source_record_ids: Sequence[str] = (),
    rounding_policy: RoundingPolicy = RoundingPolicy.EXACT,
    cap: CapSemantics | None = None,
    transform_version: str = TRANSFORM_PRIMITIVES_VERSION,
) -> TransitionValue:
    """Convert a parsed value with explicit units, constant, rounding, and cap."""

    if not isinstance(conversion, UnitConversion):
        raise TypeError("conversion must be UnitConversion")
    source_field = SourceField.from_parsed(source_tag, parsed)
    base = {
        "target_tag": target_tag,
        "rule_id": rule_id,
        "category": TransitionCategory.UNIT,
        "source_record_ids": tuple(source_record_ids),
        "source_fields": (source_field,),
        "transform_version": transform_version,
        "rounding_policy": rounding_policy,
        "unit_conversion": conversion,
        "cap_semantics": cap,
    }
    if parsed.status is ParsedStatus.BLANK:
        return TransitionValue(
            value=None,
            status=TransitionStatus.NULL_SOURCE,
            messages=("Source value is blank; no numeric default was applied.",),
            **base,
        )
    if parsed.status in {ParsedStatus.INVALID, ParsedStatus.OUT_OF_RANGE}:
        return TransitionValue(
            value=None,
            status=TransitionStatus.INVALID_SOURCE,
            messages=(f"Source value status is {parsed.status.value}.",),
            **base,
        )
    if not isinstance(parsed.value, (Decimal, int)) or isinstance(parsed.value, bool):
        raise TypeError("unit conversion requires a numeric ParsedScalar")
    source_value = _decimal_literal(parsed.value, field="parsed value")
    source_cap = _apply_cap(source_value, cap, domain=CapDomain.SOURCE)
    cap_messages = [source_cap.message] if source_cap.message else []
    if source_cap.review_required:
        return TransitionValue(
            value=None,
            status=TransitionStatus.REVIEW_REQUIRED,
            messages=(source_cap.message,),
            **base,
        )
    if source_cap.value is None:
        return TransitionValue(
            value=None,
            status=TransitionStatus.REVIEW_REQUIRED,
            messages=(source_cap.message,),
            **base,
        )
    if source_cap.replacement_is_target:
        target_value = source_cap.value
    else:
        target_value = source_cap.value * conversion.constant
        target_cap = _apply_cap(target_value, cap, domain=CapDomain.TARGET)
        if target_cap.review_required or target_cap.value is None:
            return TransitionValue(
                value=None,
                status=TransitionStatus.REVIEW_REQUIRED,
                messages=(target_cap.message,),
                **base,
            )
        # A replacement may be expressed in source units even when the
        # threshold itself is evaluated in target units. Convert it exactly;
        # never reinterpret the declared replacement domain.
        if (
            cap is not None
            and cap.replacement is not None
            and cap.replacement_domain is CapDomain.SOURCE
            and target_cap.message
        ):
            target_value = target_cap.value * conversion.constant
        else:
            target_value = target_cap.value
        if target_cap.message:
            cap_messages.append(target_cap.message)
    target_value = quantize_decimal(
        target_value, rounding_policy, rule_id=rule_id
    )
    return TransitionValue(
        value=target_value,
        status=TransitionStatus.POPULATED,
        messages=tuple(cap_messages),
        **base,
    )


def convert_h01_bh12_m_to_ft(
    parsed: ParsedScalar,
    *,
    source_record_ids: Sequence[str] = (),
    transform_version: str = TRANSFORM_PRIMITIVES_VERSION,
) -> TransitionValue:
    """Apply only the published H01/Item 10 BH12 round-down rule.

    This helper intentionally makes no claim about the H02 under-record rule.
    """

    return convert_unit(
        parsed,
        target_tag="BH12",
        rule_id=H01_BH12_RULE_ID,
        source_tag="MIN_VERT_CLR_010",
        conversion=METERS_TO_FEET,
        source_record_ids=source_record_ids,
        rounding_policy=RoundingPolicy.ROUND_DOWN_TENTH,
        cap=H01_BH12_CAP,
        transform_version=transform_version,
    )


def parse_dms(raw: Any, *, latitude: bool) -> ParsedScalar:
    """Parse legacy compact DMS (DDMMSSss or DDDMMSSss) with bounds checks."""

    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ParsedScalar(raw=raw, value=None, status=ParsedStatus.BLANK)
    if isinstance(raw, bool):
        return ParsedScalar(raw=raw, value=None, status=ParsedStatus.INVALID)
    text = str(raw).strip().upper()
    hemisphere = ""
    if text and text[-1:] in {"N", "S", "E", "W"}:
        hemisphere = text[-1]
        text = text[:-1]
    sign = 1
    explicit_sign = ""
    if text.startswith(("+", "-")):
        explicit_sign, text = text[0], text[1:]
        sign = -1 if explicit_sign == "-" else 1
    if not text.isdigit():
        return ParsedScalar(raw=raw, value=None, status=ParsedStatus.INVALID)
    degree_digits = 2 if latitude else 3
    if len(text) != degree_digits + 6:
        return ParsedScalar(raw=raw, value=None, status=ParsedStatus.INVALID)
    if latitude and hemisphere in {"E", "W"}:
        return ParsedScalar(raw=raw, value=None, status=ParsedStatus.INVALID)
    if not latitude and hemisphere in {"N", "S"}:
        return ParsedScalar(raw=raw, value=None, status=ParsedStatus.INVALID)
    hemisphere_sign = -1 if hemisphere in {"S", "W"} else 1
    if hemisphere and explicit_sign and sign != hemisphere_sign:
        return ParsedScalar(raw=raw, value=None, status=ParsedStatus.INVALID)
    if hemisphere:
        sign = hemisphere_sign

    degrees = int(text[:degree_digits])
    minutes = int(text[degree_digits : degree_digits + 2])
    seconds = Decimal(text[degree_digits + 2 :]) / Decimal("100")
    limit = 90 if latitude else 180
    coordinate = DMSCoordinate(degrees, minutes, seconds, sign)
    status = (
        ParsedStatus.OUT_OF_RANGE
        if minutes >= 60
        or seconds >= Decimal("60")
        or degrees > limit
        or (degrees == limit and (minutes != 0 or seconds != 0))
        else ParsedStatus.VALID
    )
    return ParsedScalar(raw=raw, value=coordinate, status=status)


def dms_to_decimal(
    parsed: ParsedScalar,
    *,
    latitude: bool,
    rounding_policy: RoundingPolicy = RoundingPolicy.HALF_UP_THOUSANDTH,
) -> ParsedScalar:
    """Convert a parsed DMS coordinate to bounded signed decimal degrees."""

    if parsed.status in {ParsedStatus.BLANK, ParsedStatus.INVALID}:
        return ParsedScalar(raw=parsed.raw, value=None, status=parsed.status)
    if not isinstance(parsed.value, DMSCoordinate):
        raise TypeError("dms_to_decimal requires the output of parse_dms")
    if parsed.status is ParsedStatus.OUT_OF_RANGE:
        return ParsedScalar(
            raw=parsed.raw,
            value=Decimal(parsed.value.sign)
            * (
                Decimal(parsed.value.degrees)
                + Decimal(parsed.value.minutes) / Decimal("60")
                + parsed.value.seconds / Decimal("3600")
            ),
            status=ParsedStatus.OUT_OF_RANGE,
        )
    decimal_degrees = Decimal(parsed.value.sign) * (
        Decimal(parsed.value.degrees)
        + Decimal(parsed.value.minutes) / Decimal("60")
        + parsed.value.seconds / Decimal("3600")
    )
    limit = Decimal("90") if latitude else Decimal("180")
    if abs(decimal_degrees) > limit:
        return ParsedScalar(
            raw=parsed.raw,
            value=decimal_degrees,
            status=ParsedStatus.OUT_OF_RANGE,
        )
    return ParsedScalar(
        raw=parsed.raw,
        value=quantize_decimal(decimal_degrees, rounding_policy),
        status=ParsedStatus.VALID,
    )


def decimal_to_json(value: Decimal) -> str:
    """Return one canonical, exponent-free finite decimal string."""

    value = _decimal_literal(value, field="value")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if rendered in {"", "-0"}:
        return "0"
    return rendered


def lineage_to_json(value: TransitionValue | ParsedScalar) -> str:
    """Serialize lineage deterministically without float/NaN ambiguity."""

    if not isinstance(value, (TransitionValue, ParsedScalar)):
        raise TypeError("lineage value must be TransitionValue or ParsedScalar")
    return json.dumps(
        value.to_lineage_dict(),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True, slots=True)
class _CapResult:
    value: Decimal | None
    message: str = ""
    review_required: bool = False
    replacement_is_target: bool = False


def _numeric_transition(
    parsed: ParsedScalar,
    *,
    target_tag: str,
    rule_id: str,
    source_tag: str,
    source_record_ids: Sequence[str],
    category: TransitionCategory,
    rounding_policy: RoundingPolicy,
    transform_version: str,
) -> TransitionValue:
    source_field = SourceField.from_parsed(source_tag, parsed)
    base = {
        "target_tag": target_tag,
        "rule_id": rule_id,
        "category": category,
        "source_record_ids": tuple(source_record_ids),
        "source_fields": (source_field,),
        "transform_version": transform_version,
        "rounding_policy": rounding_policy,
    }
    if parsed.status is ParsedStatus.BLANK:
        return TransitionValue(
            value=None,
            status=TransitionStatus.NULL_SOURCE,
            messages=("Source value is blank; no numeric default was applied.",),
            **base,
        )
    if parsed.status in {ParsedStatus.INVALID, ParsedStatus.OUT_OF_RANGE}:
        return TransitionValue(
            value=None,
            status=TransitionStatus.INVALID_SOURCE,
            messages=(f"Source value status is {parsed.status.value}.",),
            **base,
        )
    if not isinstance(parsed.value, (Decimal, int)) or isinstance(parsed.value, bool):
        raise TypeError("numeric transition requires a numeric ParsedScalar")
    value = quantize_decimal(parsed.value, rounding_policy, rule_id=rule_id)
    return TransitionValue(value=value, status=TransitionStatus.POPULATED, **base)


def _apply_cap(
    value: Decimal,
    cap: CapSemantics | None,
    *,
    domain: CapDomain,
) -> _CapResult:
    if cap is None or cap.domain is not domain:
        return _CapResult(value)
    below = cap.minimum is not None and (
        value < cap.minimum or (cap.minimum_inclusive and value == cap.minimum)
    )
    above = cap.maximum is not None and (
        value > cap.maximum or (cap.maximum_inclusive and value == cap.maximum)
    )
    if not below and not above:
        return _CapResult(value)
    message = f"Cap applied: {cap.reason}"
    if cap.mode is CapMode.NULL:
        return _CapResult(None, message)
    if cap.mode is CapMode.REVIEW_REQUIRED:
        return _CapResult(None, message, review_required=True)
    if cap.replacement is not None:
        return _CapResult(
            cap.replacement,
            message,
            replacement_is_target=cap.replacement_domain is CapDomain.TARGET,
        )
    boundary = cap.minimum if below else cap.maximum
    assert boundary is not None
    return _CapResult(boundary, message)


def _decimal_literal(value: Decimal | int | str, *, field: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field} must use Decimal, integer, or decimal text")
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} is not a decimal") from exc
    return _require_finite_decimal(decimal_value, field=field)


def _optional_boundary(
    value: Decimal | int | str | None, *, field: str
) -> Decimal | None:
    return None if value is None else _decimal_literal(value, field=field)


def _require_finite_decimal(value: Decimal, *, field: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    return value


def _coerce_enum(value: Any, enum_type: type[Enum], field: str):
    try:
        return value if isinstance(value, enum_type) else enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        return decimal_to_json(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        return repr(value)
    if isinstance(value, bytes):
        return {"encoding": "hex", "value": value.hex()}
    if isinstance(value, DMSCoordinate):
        return {
            "degrees": value.degrees,
            "minutes": value.minutes,
            "seconds": decimal_to_json(value.seconds),
            "sign": value.sign,
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    raise TypeError(f"lineage value is not JSON-safe: {type(value).__name__}")


METERS_TO_FEET = UnitConversion(
    source_unit="m",
    target_unit="ft",
    constant=Decimal("3.280839895013123359580052493"),
    definition="international foot: 1 ft = exactly 0.3048 m",
)

H01_BH12_CAP = CapSemantics(
    maximum=Decimal("30"),
    mode=CapMode.CLAMP,
    reason="FHWA H01 BH12 reports 99.9 ft when NBI Item 10 is at least 30 m",
    domain=CapDomain.SOURCE,
    maximum_inclusive=True,
    replacement=Decimal("99.9"),
    replacement_domain=CapDomain.TARGET,
)
