"""Reproducible, model-independent point-cloud measurement quality gates."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import math
from typing import Iterable


QUALITY_PROFILE_VERSION = "bridgeguard-point-cloud-quality-thresholds-v1"


class QualityDisposition(str, Enum):
    MEASURABLE = "measurable"
    REVIEW_REQUIRED = "review_required"
    NOT_MEASURABLE = "not_measurable"


@dataclass(frozen=True, slots=True)
class QualityIndicator:
    indicator_id: str
    value: float | int | bool | None
    unit: str | None
    threshold: str
    passed: bool
    mandatory: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class QualityAssessment:
    disposition: QualityDisposition
    indicators: tuple[QualityIndicator, ...]
    reasons: tuple[str, ...]
    out_of_distribution_flags: tuple[str, ...]
    calibration_status: str = "deterministic_thresholds_not_empirically_calibrated"
    profile_version: str = QUALITY_PROFILE_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_version": self.profile_version,
            "disposition": self.disposition.value,
            "calibration_status": self.calibration_status,
            "indicators": [indicator.to_dict() for indicator in self.indicators],
            "reasons": list(self.reasons),
            "out_of_distribution_flags": list(self.out_of_distribution_flags),
        }


def _finite(value: float, *, name: str) -> float:
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be finite")
    return converted


def assess_clearance_quality(
    *,
    point_count: int,
    candidate_cell_count: int,
    possible_cell_count: int,
    overlap_fraction: float,
    density_points_per_square_unit: float,
    lower_surface_point_count: int,
    upper_surface_point_count: int,
    lower_surface_rms: float | None,
    upper_surface_rms: float | None,
    component_completeness: float,
    units_verified: bool,
    gravity_verified: bool,
    out_of_distribution_flags: Iterable[str] = (),
) -> QualityAssessment:
    """Evaluate deterministic evidence without model-derived confidence.

    Mandatory failures mean no numeric measurement may be returned.  Marginal
    coverage or fit evidence yields ``review_required``.  The initial thresholds
    are explicit engineering-screening thresholds; the calibration status makes
    clear that a sealed empirical corpus has not yet calibrated them.
    """

    if point_count < 0 or candidate_cell_count < 0 or possible_cell_count < 0:
        raise ValueError("quality counts must be nonnegative")
    overlap = _finite(overlap_fraction, name="overlap_fraction")
    density = _finite(density_points_per_square_unit, name="density")
    completeness = _finite(component_completeness, name="component_completeness")
    if not 0.0 <= overlap <= 1.0 or not 0.0 <= completeness <= 1.0:
        raise ValueError("quality fractions must be within [0, 1]")

    indicators: list[QualityIndicator] = []

    def add(
        indicator_id: str,
        value: float | int | bool | None,
        unit: str | None,
        threshold: str,
        passed: bool,
        mandatory: bool,
        reason: str,
    ) -> None:
        indicators.append(
            QualityIndicator(
                indicator_id,
                value,
                unit,
                threshold,
                bool(passed),
                mandatory,
                None if passed else reason,
            )
        )

    add("units_verified", units_verified, None, "true", units_verified, True, "units_unverified")
    add("gravity_verified", gravity_verified, None, "true", gravity_verified, True, "gravity_unverified")
    add("point_count", point_count, "points", ">= 100", point_count >= 100, True, "insufficient_points")
    add(
        "candidate_cell_count",
        candidate_cell_count,
        "cells",
        ">= 12",
        candidate_cell_count >= 12,
        True,
        "insufficient_surface_overlap_cells",
    )
    add(
        "lower_surface_point_count",
        lower_surface_point_count,
        "points",
        ">= 30",
        lower_surface_point_count >= 30,
        True,
        "lower_surface_missing_or_sparse",
    )
    add(
        "upper_surface_point_count",
        upper_surface_point_count,
        "points",
        ">= 30",
        upper_surface_point_count >= 30,
        True,
        "upper_surface_missing_or_sparse",
    )
    add("overlap_fraction", overlap, "fraction", ">= 0.02", overlap >= 0.02, True, "surface_overlap_missing")

    add("coverage_fraction", overlap, "fraction", ">= 0.08", overlap >= 0.08, False, "low_surface_coverage")
    add("density", density, "points/square-unit", ">= 2", density >= 2.0, False, "low_point_density")
    add(
        "component_completeness",
        completeness,
        "fraction",
        ">= 0.05",
        completeness >= 0.05,
        False,
        "low_component_completeness",
    )
    for name, value in (("lower_surface_rms", lower_surface_rms), ("upper_surface_rms", upper_surface_rms)):
        available = value is not None and math.isfinite(float(value))
        passed = available and float(value) <= 0.75
        add(name, None if value is None else float(value), "coordinate-unit", "<= 0.75", passed, False, f"{name}_high_or_unavailable")

    mandatory_failures = [
        indicator.reason
        for indicator in indicators
        if indicator.mandatory and not indicator.passed and indicator.reason
    ]
    review_failures = [
        indicator.reason
        for indicator in indicators
        if not indicator.mandatory and not indicator.passed and indicator.reason
    ]
    flags = tuple(sorted({str(item) for item in out_of_distribution_flags if str(item)}))
    if mandatory_failures:
        disposition = QualityDisposition.NOT_MEASURABLE
        reasons = tuple(mandatory_failures)
    elif review_failures or flags:
        disposition = QualityDisposition.REVIEW_REQUIRED
        reasons = tuple(review_failures) + flags
    else:
        disposition = QualityDisposition.MEASURABLE
        reasons = ()
    return QualityAssessment(disposition, tuple(indicators), reasons, flags)


__all__ = [
    "QUALITY_PROFILE_VERSION",
    "QualityAssessment",
    "QualityDisposition",
    "QualityIndicator",
    "assess_clearance_quality",
]
