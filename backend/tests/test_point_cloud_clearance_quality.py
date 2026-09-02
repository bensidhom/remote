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
    assert result["roadways"]
    assert len(result["roadways"][0]["distance_samples"]) >= 12
    assert all(
        sample["soffit_z"] > sample["road_z"]
        for sample in result["roadways"][0]["distance_samples"]
    )
    assert len(result["roadways"][0]["profile_samples"]) == 9
    assert len(result["roadways"][0]["profile_plane_corners"]) == 4
    assert result["roadways"][0]["profile_method"] == "multi_plane_minimum_envelope"
    assert result["roadways"][0]["evaluated_plane_count"] >= 3
    planes = result["roadways"][0]["measurement_planes"]
    assert result["roadways"][0]["retained_plane_count"] == len(planes)
    assert {plane["station_edge"] for plane in planes} == {"leading", "trailing"}
    assert sum(bool(plane["controls_global_minimum"]) for plane in planes) == 1
    assert sum(
        bool(sample["controlling"])
        for sample in result["roadways"][0]["profile_samples"]
    ) == 1
    assert all(
        sample["soffit_z"] > sample["road_z"]
        for sample in result["roadways"][0]["profile_samples"]
    )


def test_vlm_reviewed_component_extent_bounds_profile_but_not_distances():
    points = _paired_surfaces()
    section_hint = points[(points[:, 1] >= 2.0) & (points[:, 1] <= 6.0)]
    result = compute_clearance(
        points,
        grid=16,
        clean=False,
        under_feature="highway",
        section_hint_points=section_hint,
        section_boundary_source="vlm_reviewed_component_extents",
    )

    roadway = result["roadways"][0]
    assert roadway["profile_boundary_source"] == "vlm_reviewed_component_extents"
    assert roadway["profile_boundary_hint_points"] == len(section_hint)
    assert len(roadway["profile_samples"]) == 9
    assert min(sample["y"] for sample in roadway["profile_samples"]) >= 1.5
    assert max(sample["y"] for sample in roadway["profile_samples"]) <= 6.5
    assert min(sample["clearance"] for sample in roadway["profile_samples"]) == pytest.approx(
        roadway["controlling_min_m"], abs=0.01
    )


def test_multiple_sections_are_evaluated_but_only_result_relevant_planes_persist():
    rows = []
    for x in np.linspace(0, 20, 21):
        for y in np.linspace(0, 8, 13):
            # The controlling bridge face changes across the roadway: the near
            # face controls the left half and the far face controls the right.
            left_control = 4.2 if x <= 3.0 and y <= 4.0 else 5.2
            right_control = 4.4 if x >= 17.0 and y > 4.0 else 5.2
            upper_z = min(left_control, right_control)
            for delta in np.linspace(-0.02, 0.02, 6):
                rows.append((x, y, delta))
                rows.append((x, y, upper_z + delta))

    result = compute_clearance(
        np.asarray(rows, dtype=np.float64),
        grid=21,
        clean=False,
        under_feature="highway",
    )

    roadway = result["roadways"][0]
    planes = roadway["measurement_planes"]
    assert result["status"] == "success"
    assert roadway["evaluated_plane_count"] > len(planes)
    assert roadway["retained_plane_count"] == len(planes)
    assert len(planes) >= 2
    assert all(plane["used_sample_count"] > 0 for plane in planes)
    assert sum(plane["controls_global_minimum"] for plane in planes) == 1
    assert {row["plane_id"] for row in roadway["clearance_envelope_samples"]} <= {
        plane["plane_id"] for plane in planes
    }


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
        assert result["roadway_candidates"]
        assert result["roadway_candidates"][0]["distance_samples"]
        assert len(result["roadway_candidates"][0]["profile_samples"]) == 9


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
