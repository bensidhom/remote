"""Deterministic geometry primitives for the hardened point-cloud pipeline.

The routines in this module do not classify bridge components and do not call a
model.  They operate only on registered coordinates and preserve enough local
evidence for a reviewer to reproduce a surface-gap candidate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Iterable, Sequence

import numpy as np


GEOMETRY_PROFILE_VERSION = "bridgeguard-point-cloud-geometry-v1"


class GeometryEvidenceError(ValueError):
    """Raised when geometry evidence cannot satisfy the declared contract."""


def _finite_vector(value: Sequence[float], *, size: int, name: str) -> np.ndarray:
    vector = np.asarray(value, dtype=np.float64).reshape(-1)
    if vector.shape != (size,) or not np.isfinite(vector).all():
        raise GeometryEvidenceError(f"{name} must contain {size} finite values")
    return vector


def _canonical_axis_sign(axis: np.ndarray) -> np.ndarray:
    """Resolve the otherwise arbitrary PCA sign deterministically."""

    pivot = int(np.argmax(np.abs(axis)))
    return -axis if axis[pivot] < 0 else axis


@dataclass(frozen=True, slots=True)
class GravityAlignedFrame:
    center: tuple[float, float, float]
    along_axis: tuple[float, float, float]
    transverse_axis: tuple[float, float, float]
    up_axis: tuple[float, float, float]
    point_count: int
    profile_version: str = GEOMETRY_PROFILE_VERSION

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def gravity_aligned_frame(
    points: np.ndarray,
    *,
    up_vector: Sequence[float] = (0.0, 0.0, 1.0),
) -> GravityAlignedFrame:
    """Return a plan frame after fixing gravity.

    PCA is applied only to coordinates projected onto the plane perpendicular
    to the reviewed up vector.  It therefore cannot redefine gravity from the
    longest point-cloud direction.
    """

    array = np.asarray(points, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] < 3 or len(array) < 3:
        raise GeometryEvidenceError("at least three XYZ points are required")
    xyz = array[:, :3]
    if not np.isfinite(xyz).all():
        raise GeometryEvidenceError("geometry points must be finite")
    up = _finite_vector(up_vector, size=3, name="up_vector")
    norm = float(np.linalg.norm(up))
    if norm <= 1e-12:
        raise GeometryEvidenceError("up_vector must be nonzero")
    up /= norm
    center = xyz.mean(axis=0)
    centered = xyz - center
    plan = centered - np.outer(centered @ up, up)
    covariance = plan.T @ plan
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    if float(np.max(eigenvalues)) <= 1e-12:
        raise GeometryEvidenceError("plan footprint has no stable principal axis")
    along = eigenvectors[:, int(np.argmax(eigenvalues))]
    along -= up * float(along @ up)
    along_norm = float(np.linalg.norm(along))
    if along_norm <= 1e-12:
        raise GeometryEvidenceError("plan footprint has no stable principal axis")
    along = _canonical_axis_sign(along / along_norm)
    transverse = np.cross(up, along)
    transverse /= float(np.linalg.norm(transverse))
    transverse = _canonical_axis_sign(transverse)
    # Recompute along after the transverse sign choice so the frame remains
    # right-handed while retaining deterministic signs.
    along = np.cross(transverse, up)
    along /= float(np.linalg.norm(along))
    return GravityAlignedFrame(
        center=tuple(float(item) for item in center),
        along_axis=tuple(float(item) for item in along),
        transverse_axis=tuple(float(item) for item in transverse),
        up_axis=tuple(float(item) for item in up),
        point_count=int(len(xyz)),
    )


@dataclass(frozen=True, slots=True)
class LocalSurfaceCandidate:
    candidate_id: str
    x: float
    y: float
    lower_z: float
    upper_z: float
    vertical_gap: float

    def __post_init__(self) -> None:
        values = (self.x, self.y, self.lower_z, self.upper_z, self.vertical_gap)
        if not all(math.isfinite(float(value)) for value in values):
            raise GeometryEvidenceError("surface candidate values must be finite")
        if self.upper_z < self.lower_z or self.vertical_gap < 0:
            raise GeometryEvidenceError("surface candidate has inverted elevations")
        expected = self.upper_z - self.lower_z
        if not math.isclose(expected, self.vertical_gap, rel_tol=0, abs_tol=1e-8):
            raise GeometryEvidenceError("surface candidate gap is inconsistent")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LocalSurfaceEnvelope:
    candidates: tuple[LocalSurfaceCandidate, ...]
    controlling_candidate_id: str | None
    minimum_gap: float | None
    median_gap: float | None
    maximum_gap: float | None
    profile_version: str = GEOMETRY_PROFILE_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_version": self.profile_version,
            "candidate_count": len(self.candidates),
            "controlling_candidate_id": self.controlling_candidate_id,
            "minimum_gap": self.minimum_gap,
            "median_gap": self.median_gap,
            "maximum_gap": self.maximum_gap,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


def local_surface_envelope(
    rows: Iterable[Sequence[float]],
    *,
    maximum_candidates: int = 512,
) -> LocalSurfaceEnvelope:
    """Build a deterministic local lower-to-upper surface envelope.

    Each row is ``(x, y, lower_z, upper_z)``.  Values are ordered by location,
    assigned stable IDs, and retained rather than collapsed into one global
    fitted plane.  The bounded list is intended as review evidence, not an SNBI
    projection.
    """

    if maximum_candidates < 1:
        raise GeometryEvidenceError("maximum_candidates must be positive")
    normalized: list[tuple[float, float, float, float]] = []
    for raw in rows:
        values = _finite_vector(raw, size=4, name="surface_candidate")
        x, y, lower_z, upper_z = (float(value) for value in values)
        if upper_z < lower_z:
            raise GeometryEvidenceError("surface candidate has inverted elevations")
        normalized.append((x, y, lower_z, upper_z))
    normalized.sort(key=lambda row: (row[0], row[1], row[2], row[3]))
    normalized = normalized[:maximum_candidates]
    candidates = tuple(
        LocalSurfaceCandidate(
            candidate_id=f"surface-cell-{index:04d}",
            x=row[0],
            y=row[1],
            lower_z=row[2],
            upper_z=row[3],
            vertical_gap=row[3] - row[2],
        )
        for index, row in enumerate(normalized, start=1)
    )
    if not candidates:
        return LocalSurfaceEnvelope((), None, None, None, None)
    gaps = np.asarray([candidate.vertical_gap for candidate in candidates])
    controlling_index = int(np.argmin(gaps))
    return LocalSurfaceEnvelope(
        candidates=candidates,
        controlling_candidate_id=candidates[controlling_index].candidate_id,
        minimum_gap=float(gaps[controlling_index]),
        median_gap=float(np.median(gaps)),
        maximum_gap=float(np.max(gaps)),
    )


__all__ = [
    "GEOMETRY_PROFILE_VERSION",
    "GeometryEvidenceError",
    "GravityAlignedFrame",
    "LocalSurfaceCandidate",
    "LocalSurfaceEnvelope",
    "gravity_aligned_frame",
    "local_surface_envelope",
]
