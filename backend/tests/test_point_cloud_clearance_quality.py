from __future__ import annotations

import numpy as np
import pytest

from app.routers.services.point_cloud import compute_clearance


def _paired_surfaces(*, upper_z: float = 5.0) -> np.ndarray:
    rows = []
    for x in np.linspace(0, 20, 16):
        for y in np.linspace(0, 8, 10):
            for delta in np.linspace(-0.03, 0.03, 6):
                rows.append((x, y, delta))
            for delta in np.linspace(-0.03, 0.03, 6):
                rows.append((x, y, upper_z + delta))
    return np.asarray(rows, dtype=np.float64)


def test_paired_surfaces_return_measurable_local_envelope():
    result = compute_clearance(
        _paired_surfaces(),
        grid=16,
        clean=False,
        under_feature="highway",
    )

    assert result["status"] == "success"
    assert result["clearance_m"] == pytest.approx(5.0, abs=0.01)
    assert result["quality_assessment"]["disposition"] == "measurable"
    assert result["local_surface_envelope"]["candidate_count"] == 160
    assert result["local_surface_envelope"]["controlling_candidate_id"]
    assert result["method"] != "global_fallback"


def test_road_only_cloud_abstains_without_numeric_clearance():
    road = []
    for x in np.linspace(0, 20, 16):
        for y in np.linspace(0, 8, 10):
            for delta in np.linspace(-0.03, 0.03, 6):
                road.append((x, y, delta))
    road = np.asarray(road, dtype=np.float64)
    result = compute_clearance(
        road,
        grid=16,
        clean=False,
        under_feature="highway",
    )

    assert result["status"] == "not_measurable"
    assert result["quality_assessment"]["disposition"] == "not_measurable"
    assert not any(key.endswith("clearance_m") or key.endswith("clearance_ft") for key in result)


def test_unverified_units_or_gravity_cannot_return_metric_number():
    for kwargs in ({"scale_known": False}, {"gravity_verified": False}):
        result = compute_clearance(
            _paired_surfaces(),
            grid=16,
            clean=False,
            under_feature="highway",
            **kwargs,
        )
        assert result["status"] == "not_measurable"
        assert "clearance_m" not in result
        assert result["quality_assessment"]["disposition"] == "not_measurable"


def test_waterway_returns_only_proxy_evidence_not_navigation_number():
    result = compute_clearance(
        _paired_surfaces(),
        grid=16,
        clean=False,
        under_feature="waterway",
    )

    assert result["status"] == "not_measurable"
    assert result["method"] == "water_surface_proxy"
    assert result["reason"] == "navigation_clearance_requires_channel_datum_and_survey_control"
    assert not any("clearance_m" in key or "clearance_ft" in key for key in result)


def test_sparse_overlap_preserves_rejected_candidates_but_not_accepted_number():
    points = _paired_surfaces()
    # Retain only eleven complete columns, below the mandatory twelve-cell gate.
    sparse = points[: 11 * 12]
    result = compute_clearance(
        sparse,
        grid=16,
        clean=False,
        under_feature="highway",
    )

    assert result["status"] == "not_measurable"
    assert "clearance_m" not in result
    assert result["local_surface_envelope"]["candidate_count"] <= 11
