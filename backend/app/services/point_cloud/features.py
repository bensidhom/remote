"""Canonical SNBI feature parsing and deterministic point-cloud feature binding."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence


FEATURE_BINDING_VERSION = "bridgeguard-point-cloud-feature-binding-v1"


class FeatureBindingError(ValueError):
    pass


class FeatureKind(str, Enum):
    HIGHWAY = "highway"
    RAILROAD = "railroad"
    WATERWAY = "waterway"
    PEDESTRIAN = "pedestrian"
    OTHER = "other"


class FeatureLocation(str, Enum):
    BELOW = "below"
    CARRIED = "carried"
    ABOVE = "above"
    UNKNOWN = "unknown"


class FeatureBindingStatus(str, Enum):
    BOUND = "bound"
    REVIEW_REQUIRED = "review_required"
    NOT_APPLICABLE = "not_applicable"


_LOCATION_BY_CODE = MappingProxyType(
    {
        "B": FeatureLocation.BELOW,
        "C": FeatureLocation.CARRIED,
        "A": FeatureLocation.ABOVE,
    }
)


def _kind(feature_id: str) -> FeatureKind:
    prefix = feature_id[:1].upper()
    if prefix == "H":
        return FeatureKind.HIGHWAY
    if prefix == "R":
        return FeatureKind.RAILROAD
    if prefix == "W":
        return FeatureKind.WATERWAY
    if prefix == "P":
        return FeatureKind.PEDESTRIAN
    return FeatureKind.OTHER


@dataclass(frozen=True, slots=True)
class CanonicalFeature:
    feature_id: str
    kind: FeatureKind
    location: FeatureLocation
    name: str | None
    source_pointer: str
    crossing_bridge_number: str | None
    recorded_bh12_ft: float | None
    recorded_bh13_ft: float | None
    recorded_brr02_ft: float | None
    navigable: bool | None
    raw: Mapping[str, Any]

    def to_dict(self) -> dict[str, object]:
        return {
            "feature_id": self.feature_id,
            "kind": self.kind.value,
            "location": self.location.value,
            "name": self.name,
            "source_pointer": self.source_pointer,
            "crossing_bridge_number": self.crossing_bridge_number,
            "recorded_bh12_ft": self.recorded_bh12_ft,
            "recorded_bh13_ft": self.recorded_bh13_ft,
            "recorded_brr02_ft": self.recorded_brr02_ft,
            "navigable": self.navigable,
            "raw": dict(self.raw),
        }


def _optional_number(value: Any, *, field: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise FeatureBindingError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise FeatureBindingError(f"{field} must be numeric") from exc
    if not number >= 0 or number != number or number in (float("inf"), float("-inf")):
        raise FeatureBindingError(f"{field} must be finite and nonnegative")
    return number


def canonical_features(document: Mapping[str, Any]) -> tuple[CanonicalFeature, ...]:
    rows = document.get("Features", [])
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise FeatureBindingError("Features must be an array")
    output: list[CanonicalFeature] = []
    seen: set[str] = set()
    for index, raw_row in enumerate(rows):
        if not isinstance(raw_row, Mapping):
            raise FeatureBindingError("feature rows must be objects")
        feature_id = str(raw_row.get("BF01") or "").strip().upper()
        if not feature_id or feature_id in seen:
            raise FeatureBindingError("feature identity is missing or duplicated")
        seen.add(feature_id)
        location = _LOCATION_BY_CODE.get(
            str(raw_row.get("BF02") or "").strip().upper(),
            FeatureLocation.UNKNOWN,
        )
        crossing = str(raw_row.get("BH18") or "").strip() or None
        navigable_raw = raw_row.get("BN01")
        navigable = (
            True
            if str(navigable_raw).strip().upper() == "Y"
            else False
            if str(navigable_raw).strip().upper() == "N"
            else None
        )
        output.append(
            CanonicalFeature(
                feature_id=feature_id,
                kind=_kind(feature_id),
                location=location,
                name=str(raw_row.get("BF03") or "").strip() or None,
                source_pointer=f"/Features/{index}",
                crossing_bridge_number=crossing,
                recorded_bh12_ft=_optional_number(raw_row.get("BH12"), field="BH12"),
                recorded_bh13_ft=_optional_number(raw_row.get("BH13"), field="BH13"),
                recorded_brr02_ft=_optional_number(raw_row.get("BRR02"), field="BRR02"),
                navigable=navigable,
                raw=MappingProxyType(dict(raw_row)),
            )
        )
    return tuple(output)


@dataclass(frozen=True, slots=True)
class DetectedUnderFeature:
    detected_id: str
    kind: FeatureKind
    point_count: int
    geometry_receipt_id: str
    route_hint: str | None = None

    def __post_init__(self) -> None:
        if not self.detected_id or self.point_count < 1 or not self.geometry_receipt_id:
            raise FeatureBindingError("detected feature evidence is incomplete")

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["kind"] = self.kind.value
        return result


@dataclass(frozen=True, slots=True)
class FeatureBinding:
    detected_id: str
    status: FeatureBindingStatus
    feature_id: str | None
    feature_source_pointer: str | None
    reason: str | None
    candidate_feature_ids: tuple[str, ...]
    reviewer_confirmed: bool
    version: str = FEATURE_BINDING_VERSION

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["status"] = self.status.value
        result["candidate_feature_ids"] = list(self.candidate_feature_ids)
        return result


def bind_detected_feature(
    detected: DetectedUnderFeature,
    features: Iterable[CanonicalFeature],
    *,
    reviewed_feature_id: str | None = None,
) -> FeatureBinding:
    """Bind geometry to one exact canonical row or require explicit review."""

    compatible = tuple(
        sorted(
            (
                feature
                for feature in features
                if feature.kind is detected.kind
                and feature.location is FeatureLocation.BELOW
                and not feature.crossing_bridge_number
            ),
            key=lambda feature: feature.feature_id,
        )
    )
    ids = tuple(feature.feature_id for feature in compatible)
    if reviewed_feature_id:
        selected = next(
            (feature for feature in compatible if feature.feature_id == reviewed_feature_id),
            None,
        )
        if selected is None:
            raise FeatureBindingError("reviewed feature is not a compatible candidate")
        return FeatureBinding(
            detected.detected_id,
            FeatureBindingStatus.BOUND,
            selected.feature_id,
            selected.source_pointer,
            None,
            ids,
            True,
        )
    if len(compatible) == 1:
        selected = compatible[0]
        return FeatureBinding(
            detected.detected_id,
            FeatureBindingStatus.BOUND,
            selected.feature_id,
            selected.source_pointer,
            None,
            ids,
            False,
        )
    if not compatible:
        return FeatureBinding(
            detected.detected_id,
            FeatureBindingStatus.NOT_APPLICABLE,
            None,
            None,
            "no_compatible_below_feature",
            (),
            False,
        )
    return FeatureBinding(
        detected.detected_id,
        FeatureBindingStatus.REVIEW_REQUIRED,
        None,
        None,
        "multiple_compatible_features",
        ids,
        False,
    )


__all__ = [
    "FEATURE_BINDING_VERSION",
    "CanonicalFeature",
    "DetectedUnderFeature",
    "FeatureBinding",
    "FeatureBindingError",
    "FeatureBindingStatus",
    "FeatureKind",
    "FeatureLocation",
    "bind_detected_feature",
    "canonical_features",
]
