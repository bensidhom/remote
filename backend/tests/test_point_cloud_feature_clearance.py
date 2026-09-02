from __future__ import annotations

import pytest

from app.services.point_cloud.clearance import (
    BH12_ENVELOPE_WIDTH_M,
    ClearanceMeasurementError,
    ClearanceRegion,
    ClearanceSample,
    RegionType,
    StationEdge,
    build_feature_measurement_workflow,
    measure_highway_clearance,
    navigation_measurement_gate,
    railroad_measurement_gate,
)
from app.services.point_cloud.features import (
    DetectedUnderFeature,
    FeatureBindingStatus,
    FeatureKind,
    bind_detected_feature,
    canonical_features,
)
from app.services.point_cloud.snbi_projection import (
    SnbiClearanceProjectionError,
    project_accepted_highway_clearance,
    project_calculated_highway_clearance,
    round_down_metres_to_tenth_foot,
)


def _document():
    return {
        "Features": [
            {
                "BF01": "H02",
                "BF02": "B",
                "BF03": "Main Street",
                "BH12": 16.1,
                "BH13": 14.2,
            },
            {"BF01": "R01", "BF02": "B", "BF03": "Rail line", "BRR02": 18.0},
            {"BF01": "W01", "BF02": "B", "BF03": "River", "BN01": "Y"},
        ]
    }


def _highway_binding_and_feature():
    features = canonical_features(_document())
    detected = DetectedUnderFeature(
        "roadway-1", FeatureKind.HIGHWAY, 5000, "geometry:roadway-1"
    )
    binding = bind_detected_feature(detected, features)
    feature = next(item for item in features if item.feature_id == "H02")
    return binding, feature


def _regions():
    return (
        ClearanceRegion("left-shoulder", RegionType.SHOULDER, 0.0, 1.0),
        ClearanceRegion("lane-1", RegionType.TRAVELED_LANE, 1.0, 4.5, lane_type="through"),
        ClearanceRegion("lane-2", RegionType.TRAVELED_LANE, 4.5, 8.0, lane_type="through"),
        ClearanceRegion("right-shoulder", RegionType.SHOULDER, 8.0, 9.0),
    )


def _samples():
    clearances = {
        0.5: 3.8,  # attached shoulder restriction controls BH13 only
        1.0: 4.1,
        2.0: 4.3,
        3.0: 4.4,
        4.0: 4.2,
        4.5: 5.1,
        5.0: 5.3,
        6.0: 5.4,
        7.0: 5.2,
        8.0: 4.0,
        8.5: 3.9,
    }
    output = []
    for station_id, edge, longitudinal, adjustment in (
        ("leading", StationEdge.LEADING, 0.0, 0.0),
        ("trailing", StationEdge.TRAILING, 20.0, -0.1),
    ):
        for offset, clearance in clearances.items():
            region_id = (
                "left-shoulder"
                if offset < 1.0
                else "lane-1"
                if offset < 4.5
                else "lane-2"
                if offset <= 8.0
                else "right-shoulder"
            )
            output.append(
                ClearanceSample(
                    sample_id=f"{station_id}-{offset}",
                    station_id=station_id,
                    station_edge=edge,
                    longitudinal_m=longitudinal,
                    lateral_offset_m=offset,
                    region_id=region_id,
                    lower_surface_z_m=0.0,
                    obstruction_z_m=clearance + adjustment,
                    obstruction_id=("attached-sign" if offset == 0.5 else "girder"),
                    evidence_ids=(f"point:{station_id}:{offset}",),
                )
            )
    return tuple(output)


def test_canonical_feature_binding_is_exact_and_type_specific():
    binding, feature = _highway_binding_and_feature()
    assert binding.status is FeatureBindingStatus.BOUND
    assert binding.feature_id == "H02"
    assert binding.feature_source_pointer == "/Features/0"
    assert feature.recorded_bh12_ft == 16.1
    assert feature.recorded_bh13_ft == 14.2
    assert feature.to_dict()["raw"]["BF01"] == "H02"


def test_multiple_compatible_features_require_explicit_review():
    document = _document()
    document["Features"].append({"BF01": "H03", "BF02": "B", "BF03": "Ramp"})
    features = canonical_features(document)
    detected = DetectedUnderFeature("roadway-1", FeatureKind.HIGHWAY, 20, "geom:1")

    ambiguous = bind_detected_feature(detected, features)
    reviewed = bind_detected_feature(detected, features, reviewed_feature_id="H03")

    assert ambiguous.status is FeatureBindingStatus.REVIEW_REQUIRED
    assert ambiguous.candidate_feature_ids == ("H02", "H03")
    assert reviewed.status is FeatureBindingStatus.BOUND
    assert reviewed.feature_id == "H03"
    assert reviewed.reviewer_confirmed is True


def test_crossing_bridge_reference_is_not_measured_on_wrong_bridge():
    document = {"Features": [{"BF01": "H02", "BF02": "B", "BH18": "OTHER"}]}
    features = canonical_features(document)
    detected = DetectedUnderFeature("roadway-1", FeatureKind.HIGHWAY, 20, "geom:1")
    assert bind_detected_feature(detected, features).status is FeatureBindingStatus.NOT_APPLICABLE


def test_brm_style_receipts_reproduce_distinct_bh12_and_bh13():
    binding, feature = _highway_binding_and_feature()
    result = measure_highway_clearance(
        binding=binding,
        feature=feature,
        regions=_regions(),
        samples=_samples(),
    )

    assert BH12_ENVELOPE_WIDTH_M == 3.048
    assert result.bh12_raw_m == pytest.approx(5.0)
    assert result.bh13_raw_m == pytest.approx(3.7)
    assert result.bh13_controlling_sample_id == "trailing-0.5"
    assert result.leading_edge_minimum_m == pytest.approx(3.8)
    assert result.trailing_edge_minimum_m == pytest.approx(3.7)
    assert result.selected_bh12_candidate_id
    selected = next(
        item
        for item in result.bh12_candidates
        if item.candidate_id == result.selected_bh12_candidate_id
    )
    assert selected.end_offset_m - selected.start_offset_m == pytest.approx(3.048)
    assert selected.start_offset_m >= 1.0
    assert selected.end_offset_m <= 8.0
    assert result.to_dict()["regions"][0]["region_type"] == "shoulder"
    graph = result.brm_graph_payload()
    assert [station["station_edge"] for station in graph["stations"]] == [
        "leading",
        "trailing",
    ]
    assert graph["overall_minimum_m"] == pytest.approx(3.7)
    assert result.to_dict()["recorded_comparison"]["diagnostic_only"] is True


def test_measurement_is_invariant_to_region_and_sample_input_order():
    binding, feature = _highway_binding_and_feature()
    forward = measure_highway_clearance(
        binding=binding, feature=feature, regions=_regions(), samples=_samples()
    )
    reverse = measure_highway_clearance(
        binding=binding,
        feature=feature,
        regions=tuple(reversed(_regions())),
        samples=tuple(reversed(_samples())),
    )
    assert forward.bh12_raw_m == reverse.bh12_raw_m
    assert forward.bh13_raw_m == reverse.bh13_raw_m
    assert forward.selected_bh12_candidate_id == reverse.selected_bh12_candidate_id


def test_no_ten_foot_traveled_interval_fails_closed():
    binding, feature = _highway_binding_and_feature()
    narrow = (ClearanceRegion("lane", RegionType.TRAVELED_LANE, 0, 3.0),)
    sample = ClearanceSample(
        "s", "leading", StationEdge.LEADING, 0, 1.5, "lane", 0, 5, "girder", ("p:1",)
    )
    with pytest.raises(ClearanceMeasurementError, match="10-foot"):
        measure_highway_clearance(
            binding=binding, feature=feature, regions=narrow, samples=(sample,)
        )


def test_sparse_points_cannot_fake_coverage_across_ten_foot_envelope():
    binding, feature = _highway_binding_and_feature()
    wide = (ClearanceRegion("lane", RegionType.TRAVELED_LANE, 0, 5.0),)
    sparse = tuple(
        ClearanceSample(
            f"s-{offset}",
            "leading",
            StationEdge.LEADING,
            0,
            offset,
            "lane",
            0,
            5,
            "girder",
            (f"p:{offset}",),
        )
        for offset in (0.0, 5.0)
    )
    with pytest.raises(ClearanceMeasurementError, match="10-foot"):
        measure_highway_clearance(
            binding=binding, feature=feature, regions=wide, samples=sparse
        )


def test_railroad_and_navigation_use_separate_fail_closed_products():
    features = canonical_features(_document())
    railroad = next(item for item in features if item.feature_id == "R01")
    waterway = next(item for item in features if item.feature_id == "W01")
    rail_binding = bind_detected_feature(
        DetectedUnderFeature("rail-1", FeatureKind.RAILROAD, 50, "geom:r"), features
    )
    water_binding = bind_detected_feature(
        DetectedUnderFeature("water-1", FeatureKind.WATERWAY, 50, "geom:w"), features
    )

    rail = railroad_measurement_gate(
        binding=rail_binding, feature=railroad, track_reference_confirmed=False
    )
    water = navigation_measurement_gate(
        binding=water_binding,
        feature=waterway,
        channel_limits_confirmed=False,
        datum_confirmed=False,
        timestamp_confirmed=False,
    )

    assert rail.product == "BRR02" and rail.status == "not_measurable"
    assert water.product == "BN02" and water.status == "not_measurable"
    assert "controlling_datum_required" in water.reason


def test_snbi_projection_is_explicit_and_rounds_down_only_at_acceptance():
    binding, feature = _highway_binding_and_feature()
    result = measure_highway_clearance(
        binding=binding, feature=feature, regions=_regions(), samples=_samples()
    )
    with pytest.raises(SnbiClearanceProjectionError):
        project_accepted_highway_clearance(
            result, accepted=False, accepted_by="reviewer", acceptance_reason="checked"
        )
    projection = project_accepted_highway_clearance(
        result,
        accepted=True,
        accepted_by="reviewer",
        acceptance_reason="field geometry reviewed",
    )

    assert projection.bh12 == "16.4"
    assert projection.bh13 == "12.1"
    assert projection.submission_ready is False
    calculated = project_calculated_highway_clearance(result)
    assert calculated.bh12 == "16.4"
    assert calculated.bh13 == "12.1"
    assert calculated.official is False
    assert str(round_down_metres_to_tenth_foot(30.48)) == "99.9"


def test_active_geometry_automatically_materializes_bh12_and_bh13():
    def plane(plane_id, station, edge, adjustment=0.0):
        samples = []
        for index, offset in enumerate((0.0, 1.0, 2.0, 3.0, 4.0), start=1):
            clearance = 4.5 + 0.1 * offset + adjustment
            samples.append(
                {
                    "sample_id": f"cell-{plane_id}-{index}",
                    "offset": offset,
                    "road_z": 0.0,
                    "soffit_z": clearance,
                    "clearance": clearance,
                }
            )
        return {
            "plane_id": plane_id,
            "station_offset": station,
            "station_edge": edge,
            "samples": samples,
        }

    geometry_result = {
        "roadways": [
            {
                "road_point_count": 400,
                "soffit_point_count": 350,
                "measurement_planes": [
                    plane("plane-leading", 0.0, "leading"),
                    plane("plane-trailing", 20.0, "trailing", -0.2),
                ],
            }
        ]
    }
    workflow = build_feature_measurement_workflow(
        feature_rows=_document()["Features"],
        geometry_result=geometry_result,
        under_feature="highway",
    )

    assert workflow["status"] == "success"
    assert workflow["reason"] is None
    assert workflow["bindings"][0]["feature_id"] == "H02"
    assert workflow["brm_graph_ready"] is True
    assert workflow["bh12_ready"] is True
    assert workflow["bh13_ready"] is True
    measurement = workflow["primary_measurement"]
    assert measurement["bh13_raw_m"] == pytest.approx(4.3)
    assert measurement["bh12_raw_m"] >= measurement["bh13_raw_m"]
    assert measurement["snbi_formatted"]["official"] is False
    assert measurement["automatic_resolution"]["lane_count"] == 1
    assert measurement["brm_graph"]["stations"][0]["station_edge"] == "leading"
    assert measurement["brm_graph"]["stations"][-1]["station_edge"] == "trailing"
    assert measurement["retained_plane_roles"]["plane-leading"] == ["leading"]
    assert "trailing" in measurement["retained_plane_roles"]["plane-trailing"]
    assert all(
        sample["used_by_result"]
        for plane in geometry_result["roadways"][0]["measurement_planes"]
        for sample in plane["samples"]
    )


def test_automatic_workflow_omits_numeric_result_for_ambiguous_features():
    document = _document()
    document["Features"].append({"BF01": "H03", "BF02": "B", "BF03": "Ramp"})
    workflow = build_feature_measurement_workflow(
        feature_rows=document["Features"],
        geometry_result={"roadways": [{"road_point_count": 10, "soffit_point_count": 10}]},
        under_feature="highway",
    )

    assert workflow["status"] == "not_measurable"
    assert workflow["measurements"] == []
    assert workflow["automatic"] is True
