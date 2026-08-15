"""Coordinate, unit, gravity, and registration contracts for point clouds.

The functions in this module are deliberately independent from any file parser.
They preserve source/world coordinates as float64 and create a reversible local
analysis frame.  Nothing here guesses units or an up direction.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
from typing import Iterable, Sequence

import numpy as np


class CoordinateContractError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.public_message = message


class LinearUnit(str, Enum):
    METRE = "metre"
    INTERNATIONAL_FOOT = "international_foot"
    US_SURVEY_FOOT = "us_survey_foot"
    UNKNOWN = "unknown"


class FrameKind(str, Enum):
    PROJECTED_CRS = "projected_crs"
    ENGINEERING_LOCAL = "engineering_local"
    SCANNER_LOCAL = "scanner_local"
    RECONSTRUCTION_RELATIVE = "reconstruction_relative"
    UNKNOWN = "unknown"


class GravitySource(str, Enum):
    SOURCE_METADATA = "source_metadata"
    CONTROL_REGISTRATION = "control_registration"
    EDITOR_REVIEWED = "editor_reviewed"
    ASSUMED = "assumed"
    UNKNOWN = "unknown"


_UNIT_TO_METRES = {
    LinearUnit.METRE: 1.0,
    LinearUnit.INTERNATIONAL_FOOT: 0.3048,
    LinearUnit.US_SURVEY_FOOT: 1200.0 / 3937.0,
}


def _tuple3(values: Iterable[float], *, field: str) -> tuple[float, float, float]:
    result = tuple(float(value) for value in values)
    if len(result) != 3 or not all(math.isfinite(value) for value in result):
        raise CoordinateContractError(
            "invalid_coordinate_vector", f"{field} must contain three finite values."
        )
    return result  # type: ignore[return-value]


@dataclass(frozen=True)
class CoordinateFrame:
    kind: FrameKind
    horizontal_unit: LinearUnit
    vertical_unit: LinearUnit
    up_vector: tuple[float, float, float]
    gravity_source: GravitySource
    crs: str | None = None
    metadata_source: str | None = None
    review_evidence_id: str | None = None

    def __post_init__(self) -> None:
        up = _tuple3(self.up_vector, field="up_vector")
        norm = math.sqrt(sum(value * value for value in up))
        if norm <= 1e-12:
            raise CoordinateContractError(
                "invalid_up_vector", "The point-cloud up vector must be non-zero."
            )
        object.__setattr__(self, "up_vector", tuple(value / norm for value in up))
        crs = (self.crs or "").strip() or None
        object.__setattr__(self, "crs", crs)
        metadata = (self.metadata_source or "").strip() or None
        object.__setattr__(self, "metadata_source", metadata)
        evidence = (self.review_evidence_id or "").strip() or None
        object.__setattr__(self, "review_evidence_id", evidence)
        if self.kind is FrameKind.PROJECTED_CRS and crs is None:
            raise CoordinateContractError(
                "crs_required", "A projected coordinate frame requires a CRS identifier."
            )
        if self.gravity_source is GravitySource.EDITOR_REVIEWED and evidence is None:
            raise CoordinateContractError(
                "gravity_review_evidence_required",
                "An editor-reviewed up direction requires an evidence receipt.",
            )

    @property
    def metric_ready(self) -> bool:
        return (
            self.horizontal_unit in _UNIT_TO_METRES
            and self.vertical_unit in _UNIT_TO_METRES
            and self.kind not in {FrameKind.RECONSTRUCTION_RELATIVE, FrameKind.UNKNOWN}
            and self.gravity_source
            in {
                GravitySource.SOURCE_METADATA,
                GravitySource.CONTROL_REGISTRATION,
                GravitySource.EDITOR_REVIEWED,
            }
        )

    def require_metric_ready(self) -> None:
        missing: list[str] = []
        if self.horizontal_unit not in _UNIT_TO_METRES:
            missing.append("horizontal_unit")
        if self.vertical_unit not in _UNIT_TO_METRES:
            missing.append("vertical_unit")
        if self.kind in {FrameKind.RECONSTRUCTION_RELATIVE, FrameKind.UNKNOWN}:
            missing.append("coordinate_frame")
        if self.gravity_source not in {
            GravitySource.SOURCE_METADATA,
            GravitySource.CONTROL_REGISTRATION,
            GravitySource.EDITOR_REVIEWED,
        }:
            missing.append("gravity")
        if missing:
            raise CoordinateContractError(
                "metric_frame_unverified",
                "Metric processing requires verified " + ", ".join(missing) + ".",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "horizontal_unit": self.horizontal_unit.value,
            "vertical_unit": self.vertical_unit.value,
            "up_vector": list(self.up_vector),
            "gravity_source": self.gravity_source.value,
            "crs": self.crs,
            "metadata_source": self.metadata_source,
            "review_evidence_id": self.review_evidence_id,
            "metric_ready": self.metric_ready,
        }


@dataclass(frozen=True)
class RegistrationReceipt:
    selected_scan_indices: tuple[int, ...]
    reference_scan_index: int
    rms_metres: float
    maximum_residual_metres: float
    method: str
    transform_sha256: str
    reviewed: bool

    def __post_init__(self) -> None:
        indices = tuple(int(value) for value in self.selected_scan_indices)
        if not indices or len(set(indices)) != len(indices) or any(value < 0 for value in indices):
            raise CoordinateContractError(
                "invalid_scan_selection", "Selected scan indices must be unique non-negative integers."
            )
        object.__setattr__(self, "selected_scan_indices", indices)
        if self.reference_scan_index not in indices:
            raise CoordinateContractError(
                "invalid_registration_reference", "The registration reference must be in the selected scan set."
            )
        for value, label in (
            (self.rms_metres, "rms_metres"),
            (self.maximum_residual_metres, "maximum_residual_metres"),
        ):
            if not math.isfinite(float(value)) or float(value) < 0:
                raise CoordinateContractError(
                    "invalid_registration_residual", f"{label} must be a finite non-negative value."
                )
        if len(self.transform_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.transform_sha256
        ):
            raise CoordinateContractError(
                "invalid_registration_hash", "Registration transform hash must be lowercase SHA-256."
            )

    def require_accepted(self, *, maximum_rms_metres: float = 0.025) -> None:
        if not self.reviewed:
            raise CoordinateContractError(
                "registration_unreviewed", "Multi-scan registration requires explicit review."
            )
        if self.rms_metres > maximum_rms_metres:
            raise CoordinateContractError(
                "registration_rms_exceeded",
                "Multi-scan registration RMS exceeds the accepted processing threshold.",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "selected_scan_indices": list(self.selected_scan_indices),
            "reference_scan_index": self.reference_scan_index,
            "rms_metres": self.rms_metres,
            "maximum_residual_metres": self.maximum_residual_metres,
            "method": self.method,
            "transform_sha256": self.transform_sha256,
            "reviewed": self.reviewed,
        }


def _rotation_aligning_up_to_z(up_vector: Sequence[float]) -> np.ndarray:
    source = np.asarray(up_vector, dtype=np.float64)
    source /= np.linalg.norm(source)
    target = np.asarray((0.0, 0.0, 1.0), dtype=np.float64)
    cross = np.cross(source, target)
    sine = float(np.linalg.norm(cross))
    cosine = float(np.dot(source, target))
    if sine <= 1e-15:
        if cosine > 0:
            return np.eye(3, dtype=np.float64)
        # Deterministic 180-degree rotation about X for a down-pointing vector.
        return np.diag((1.0, -1.0, -1.0)).astype(np.float64)
    skew = np.asarray(
        ((0.0, -cross[2], cross[1]), (cross[2], 0.0, -cross[0]), (-cross[1], cross[0], 0.0)),
        dtype=np.float64,
    )
    return np.eye(3, dtype=np.float64) + skew + (skew @ skew) * ((1.0 - cosine) / (sine * sine))


@dataclass(frozen=True)
class ReversibleCoordinateTransform:
    source_to_metres: tuple[float, float, float]
    metric_origin: tuple[float, float, float]
    rotation_to_analysis: tuple[tuple[float, float, float], ...]
    transform_sha256: str

    @classmethod
    def from_world_points(
        cls, points: np.ndarray, frame: CoordinateFrame
    ) -> "ReversibleCoordinateTransform":
        frame.require_metric_ready()
        world = _finite_xyz(points)
        scale = np.asarray(
            (
                _UNIT_TO_METRES[frame.horizontal_unit],
                _UNIT_TO_METRES[frame.horizontal_unit],
                _UNIT_TO_METRES[frame.vertical_unit],
            ),
            dtype=np.float64,
        )
        metric = world * scale
        # A midpoint is stable for large offsets and independent of point order.
        origin = (metric.min(axis=0) + metric.max(axis=0)) / 2.0
        rotation = _rotation_aligning_up_to_z(frame.up_vector)
        payload = {
            "source_to_metres": scale.tolist(),
            "metric_origin": origin.tolist(),
            "rotation_to_analysis": rotation.tolist(),
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()
        return cls(
            source_to_metres=tuple(float(value) for value in scale),
            metric_origin=tuple(float(value) for value in origin),
            rotation_to_analysis=tuple(tuple(float(value) for value in row) for row in rotation),
            transform_sha256=digest,
        )

    def to_local(self, world_points: np.ndarray) -> np.ndarray:
        world = _finite_xyz(world_points)
        scale = np.asarray(self.source_to_metres, dtype=np.float64)
        origin = np.asarray(self.metric_origin, dtype=np.float64)
        rotation = np.asarray(self.rotation_to_analysis, dtype=np.float64)
        local = ((world * scale) - origin) @ rotation.T
        return np.asarray(local, dtype=np.float64)

    def to_world(self, local_points: np.ndarray) -> np.ndarray:
        local = _finite_xyz(local_points)
        scale = np.asarray(self.source_to_metres, dtype=np.float64)
        origin = np.asarray(self.metric_origin, dtype=np.float64)
        rotation = np.asarray(self.rotation_to_analysis, dtype=np.float64)
        world = ((local @ rotation) + origin) / scale
        return np.asarray(world, dtype=np.float64)

    def to_dict(self) -> dict[str, object]:
        return {
            "source_to_metres": list(self.source_to_metres),
            "metric_origin": list(self.metric_origin),
            "rotation_to_analysis": [list(row) for row in self.rotation_to_analysis],
            "transform_sha256": self.transform_sha256,
        }


def _finite_xyz(points: np.ndarray) -> np.ndarray:
    array = np.asarray(points)
    if array.dtype != np.float64:
        array = array.astype(np.float64)
    if array.ndim != 2 or array.shape[1] != 3 or not len(array):
        raise CoordinateContractError(
            "invalid_point_shape", "Point coordinates must be a non-empty float64 (N,3) array."
        )
    if not np.isfinite(array).all():
        raise CoordinateContractError(
            "nonfinite_coordinates", "Point coordinates must all be finite."
        )
    return np.ascontiguousarray(array, dtype=np.float64)


def registration_hash(transforms: Sequence[np.ndarray]) -> str:
    canonical: list[list[list[float]]] = []
    for transform in transforms:
        matrix = np.asarray(transform, dtype=np.float64)
        if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
            raise CoordinateContractError(
                "invalid_registration_transform", "Registration transforms must be finite 4x4 matrices."
            )
        canonical.append(matrix.tolist())
    return hashlib.sha256(
        json.dumps(canonical, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()

