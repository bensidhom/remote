from __future__ import annotations

import numpy as np
import pytest

from app.services.point_cloud.geometry import (
    GeometryEvidenceError,
    gravity_aligned_frame,
    local_surface_envelope,
)
from app.services.point_cloud.quality import (
    QualityDisposition,
    assess_clearance_quality,
)


def _bridge_cloud() -> np.ndarray:
    x, y = np.meshgrid(np.linspace(-10, 10, 21), np.linspace(-3, 3, 9))
    z = 0.1 * x
    return np.column_stack([x.ravel(), y.ravel(), z.ravel()])


def test_gravity_is_fixed_before_plan_pca_and_frame_is_orthonormal():
    frame = gravity_aligned_frame(_bridge_cloud(), up_vector=(0, 0, 2))
    along = np.asarray(frame.along_axis)
    transverse = np.asarray(frame.transverse_axis)
    up = np.asarray(frame.up_axis)

    assert np.allclose(up, [0, 0, 1])
    assert abs(float(along @ up)) < 1e-12
    assert abs(float(transverse @ up)) < 1e-12
    assert abs(float(along @ transverse)) < 1e-12
    assert np.allclose(np.linalg.norm(along), 1)
    assert abs(along[0]) > abs(along[1])


def test_gravity_frame_axes_are_translation_and_order_invariant():
    points = _bridge_cloud()
    original = gravity_aligned_frame(points)
    transformed = gravity_aligned_frame(points[::-1] + [1000, -400, 25])

    assert np.allclose(original.along_axis, transformed.along_axis)
    assert np.allclose(original.transverse_axis, transformed.transverse_axis)
    assert np.allclose(
        np.asarray(transformed.center) - np.asarray(original.center),
        [1000, -400, 25],
    )


def test_gravity_frame_rejects_unstable_or_nonfinite_input():
    with pytest.raises(GeometryEvidenceError):
        gravity_aligned_frame(np.zeros((3, 3)))
    bad = _bridge_cloud()
    bad[0, 0] = np.nan
    with pytest.raises(GeometryEvidenceError):
        gravity_aligned_frame(bad)


def test_local_envelope_preserves_piecewise_candidates_and_controlling_location():
    envelope = local_surface_envelope(
        [
            (2, 0, 0.2, 5.0),
            (0, 0, 0.0, 5.2),
            (1, 0, 0.1, 4.6),
        ]
    )

    assert [item.x for item in envelope.candidates] == [0, 1, 2]
    assert envelope.minimum_gap == pytest.approx(4.5)
    assert envelope.controlling_candidate_id == "surface-cell-0002"
    assert envelope.maximum_gap == pytest.approx(5.2)


def test_local_envelope_rejects_inverted_and_nonfinite_surfaces():
    with pytest.raises(GeometryEvidenceError):
        local_surface_envelope([(0, 0, 5, 4)])
    with pytest.raises(GeometryEvidenceError):
        local_surface_envelope([(0, 0, 0, float("inf"))])


def _assessment(**overrides):
    values = {
        "point_count": 5000,
        "candidate_cell_count": 80,
        "possible_cell_count": 100,
        "overlap_fraction": 0.8,
        "density_points_per_square_unit": 50.0,
        "lower_surface_point_count": 500,
        "upper_surface_point_count": 500,
        "lower_surface_rms": 0.03,
        "upper_surface_rms": 0.08,
        "component_completeness": 0.4,
        "units_verified": True,
        "gravity_verified": True,
    }
    values.update(overrides)
    return assess_clearance_quality(**values)


def test_quality_measurable_requires_every_mandatory_gate():
    result = _assessment()
    assert result.disposition is QualityDisposition.MEASURABLE
    assert not result.reasons
    assert result.calibration_status == "deterministic_thresholds_not_empirically_calibrated"


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"units_verified": False}, "units_unverified"),
        ({"gravity_verified": False}, "gravity_unverified"),
        ({"candidate_cell_count": 11}, "insufficient_surface_overlap_cells"),
        ({"lower_surface_point_count": 29}, "lower_surface_missing_or_sparse"),
        ({"upper_surface_point_count": 29}, "upper_surface_missing_or_sparse"),
        ({"overlap_fraction": 0.019}, "surface_overlap_missing"),
    ],
)
def test_quality_mandatory_failure_abstains(override, reason):
    result = _assessment(**override)
    assert result.disposition is QualityDisposition.NOT_MEASURABLE
    assert reason in result.reasons


@pytest.mark.parametrize(
    "override",
    [
        {"overlap_fraction": 0.079},
        {"density_points_per_square_unit": 1.9},
        {"component_completeness": 0.049},
        {"upper_surface_rms": 0.751},
        {"out_of_distribution_flags": ("extreme_occlusion",)},
    ],
)
def test_quality_marginal_evidence_requires_review(override):
    assert _assessment(**override).disposition is QualityDisposition.REVIEW_REQUIRED


def test_quality_serialization_is_deterministic_and_complete():
    first = _assessment().to_dict()
    second = _assessment().to_dict()
    assert first == second
    assert {item["indicator_id"] for item in first["indicators"]} >= {
        "coverage_fraction",
        "density",
        "component_completeness",
        "lower_surface_rms",
        "upper_surface_rms",
    }
