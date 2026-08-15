"""Feature-bound raw clearance measurement products.

This module models the lane/region/station information visible in AASHTOWare
BrM 7.2 while keeping FHWA BH12 and BH13 computations distinct.  Values remain
raw metres here; rounding and SNBI sentinels belong to the accepted projection
boundary.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import math
from typing import Iterable, Mapping, Sequence

from app.services.point_cloud.features import (
    FEATURE_BINDING_VERSION,
    CanonicalFeature,
    DetectedUnderFeature,
    FeatureBinding,
    FeatureBindingStatus,
    FeatureKind,
    bind_detected_feature,
    canonical_features,
)


CLEARANCE_PROFILE_VERSION = "bridgeguard-feature-clearance-v1"
BH12_ENVELOPE_WIDTH_M = 3.048


class ClearanceMeasurementError(ValueError):
    pass


class RegionType(str, Enum):
    LEFT_EDGE_COPE = "left_edge_cope"
    SHOULDER = "shoulder"
    TRAVELED_LANE = "traveled_lane"
    AUXILIARY_TURN_LANE = "auxiliary_turn_lane"
    MEDIAN = "median"
    RIGHT_EDGE_COPE = "right_edge_cope"


class StationEdge(str, Enum):
    LEADING = "leading"
    INTERMEDIATE = "intermediate"
    TRAILING = "trailing"


@dataclass(frozen=True, slots=True)
class ClearanceRegion:
    region_id: str
    region_type: RegionType
    start_offset_m: float
    end_offset_m: float
    direction: str | None = None
    lane_type: str | None = None
    include_in_bh13: bool = True

    def __post_init__(self) -> None:
        if not self.region_id or not all(
            math.isfinite(float(value))
            for value in (self.start_offset_m, self.end_offset_m)
        ):
            raise ClearanceMeasurementError("clearance region is invalid")
        if self.end_offset_m <= self.start_offset_m:
            raise ClearanceMeasurementError("clearance region width must be positive")

    @property
    def bh12_eligible(self) -> bool:
        return self.region_type in {
            RegionType.TRAVELED_LANE,
            RegionType.AUXILIARY_TURN_LANE,
        }

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["region_type"] = self.region_type.value
        result["bh12_eligible"] = self.bh12_eligible
        return result


@dataclass(frozen=True, slots=True)
class ClearanceSample:
    sample_id: str
    station_id: str
    station_edge: StationEdge
    longitudinal_m: float
    lateral_offset_m: float
    region_id: str
    lower_surface_z_m: float
    obstruction_z_m: float
    obstruction_id: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        values = (
            self.longitudinal_m,
            self.lateral_offset_m,
            self.lower_surface_z_m,
            self.obstruction_z_m,
        )
        if not self.sample_id or not self.station_id or not self.obstruction_id:
            raise ClearanceMeasurementError("clearance sample identity is incomplete")
        if not all(math.isfinite(float(value)) for value in values):
            raise ClearanceMeasurementError("clearance sample values must be finite")
        if self.obstruction_z_m < self.lower_surface_z_m:
            raise ClearanceMeasurementError("clearance sample elevations are inverted")
        if not self.evidence_ids:
            raise ClearanceMeasurementError("clearance sample requires evidence")

    @property
    def vertical_clearance_m(self) -> float:
        return self.obstruction_z_m - self.lower_surface_z_m

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["station_edge"] = self.station_edge.value
        result["evidence_ids"] = list(self.evidence_ids)
        result["vertical_clearance_m"] = self.vertical_clearance_m
        return result


@dataclass(frozen=True, slots=True)
class Bh12EnvelopeCandidate:
    candidate_id: str
    start_offset_m: float
    end_offset_m: float
    minimum_clearance_m: float
    controlling_sample_id: str
    station_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["station_ids"] = list(self.station_ids)
        return result


@dataclass(frozen=True, slots=True)
class HighwayClearanceMeasurement:
    feature_id: str
    feature_source_pointer: str
    regions: tuple[ClearanceRegion, ...]
    samples: tuple[ClearanceSample, ...]
    bh12_candidates: tuple[Bh12EnvelopeCandidate, ...]
    selected_bh12_candidate_id: str
    bh12_raw_m: float
    bh13_raw_m: float
    bh13_controlling_sample_id: str
    overall_minimum_m: float
    overall_maximum_m: float
    leading_edge_minimum_m: float | None
    trailing_edge_minimum_m: float | None
    recorded_bh12_ft: float | None
    recorded_bh13_ft: float | None
    profile_version: str = CLEARANCE_PROFILE_VERSION

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["regions"] = [region.to_dict() for region in self.regions]
        result["samples"] = [sample.to_dict() for sample in self.samples]
        result["bh12_candidates"] = [item.to_dict() for item in self.bh12_candidates]
        result["brm_graph"] = self.brm_graph_payload()
        result["recorded_comparison"] = {
            "bh12_recorded_ft": self.recorded_bh12_ft,
            "bh12_raw_candidate_ft": self.bh12_raw_m * 3.280839895013123,
            "bh12_delta_ft": (
                None
                if self.recorded_bh12_ft is None
                else self.bh12_raw_m * 3.280839895013123 - self.recorded_bh12_ft
            ),
            "bh13_recorded_ft": self.recorded_bh13_ft,
            "bh13_raw_candidate_ft": self.bh13_raw_m * 3.280839895013123,
            "bh13_delta_ft": (
                None
                if self.recorded_bh13_ft is None
                else self.bh13_raw_m * 3.280839895013123 - self.recorded_bh13_ft
            ),
            "diagnostic_only": True,
        }
        return result

    def brm_graph_payload(self) -> dict[str, object]:
        """Return the exact ordered region/point model needed to draw the graph."""

        stations: dict[str, dict[str, object]] = {}
        for sample in self.samples:
            station = stations.setdefault(
                sample.station_id,
                {
                    "station_id": sample.station_id,
                    "station_edge": sample.station_edge.value,
                    "longitudinal_m": sample.longitudinal_m,
                    "measurement_points": [],
                },
            )
            station["measurement_points"].append(sample.to_dict())
        return {
            "profile_version": self.profile_version,
            "feature_id": self.feature_id,
            "regions": [region.to_dict() for region in self.regions],
            "stations": [stations[key] for key in sorted(stations)],
            "overall_minimum_m": self.overall_minimum_m,
            "overall_maximum_m": self.overall_maximum_m,
            "leading_edge_minimum_m": self.leading_edge_minimum_m,
            "trailing_edge_minimum_m": self.trailing_edge_minimum_m,
            "bh12_selected_envelope_id": self.selected_bh12_candidate_id,
            "bh13_controlling_sample_id": self.bh13_controlling_sample_id,
        }


def _validate_regions(regions: Sequence[ClearanceRegion]) -> dict[str, ClearanceRegion]:
    ordered = sorted(regions, key=lambda item: (item.start_offset_m, item.end_offset_m))
    if not ordered:
        raise ClearanceMeasurementError("clearance regions are required")
    output: dict[str, ClearanceRegion] = {}
    previous_end = -math.inf
    for region in ordered:
        if region.region_id in output:
            raise ClearanceMeasurementError("clearance region IDs must be unique")
        if region.start_offset_m < previous_end - 1e-9:
            raise ClearanceMeasurementError("clearance regions must not overlap")
        previous_end = region.end_offset_m
        output[region.region_id] = region
    return output


def _contiguous_eligible_intervals(
    regions: Sequence[ClearanceRegion],
) -> tuple[tuple[float, float], ...]:
    eligible = sorted(
        (region for region in regions if region.bh12_eligible),
        key=lambda item: item.start_offset_m,
    )
    output: list[list[float]] = []
    for region in eligible:
        if output and math.isclose(output[-1][1], region.start_offset_m, abs_tol=1e-8):
            output[-1][1] = region.end_offset_m
        else:
            output.append([region.start_offset_m, region.end_offset_m])
    return tuple((start, end) for start, end in output)


def measure_highway_clearance(
    *,
    binding: FeatureBinding,
    feature: CanonicalFeature,
    regions: Sequence[ClearanceRegion],
    samples: Sequence[ClearanceSample],
    maximum_sample_spacing_m: float = 1.25,
) -> HighwayClearanceMeasurement:
    """Compute BH12 and BH13 independently from exact region/station receipts."""

    if (
        binding.status is not FeatureBindingStatus.BOUND
        or binding.feature_id != feature.feature_id
        or feature.kind is not FeatureKind.HIGHWAY
        or feature.crossing_bridge_number
    ):
        raise ClearanceMeasurementError("highway feature binding is not measurable")
    if not math.isfinite(maximum_sample_spacing_m) or maximum_sample_spacing_m <= 0:
        raise ClearanceMeasurementError("maximum sample spacing must be positive")
    by_region = _validate_regions(regions)
    if not samples:
        raise ClearanceMeasurementError("clearance samples are required")
    sample_ids: set[str] = set()
    station_ids = {sample.station_id for sample in samples}
    for sample in samples:
        if sample.sample_id in sample_ids:
            raise ClearanceMeasurementError("clearance sample IDs must be unique")
        sample_ids.add(sample.sample_id)
        region = by_region.get(sample.region_id)
        if region is None or not (
            region.start_offset_m - 1e-9
            <= sample.lateral_offset_m
            <= region.end_offset_m + 1e-9
        ):
            raise ClearanceMeasurementError("sample is outside its declared region")

    candidates: list[Bh12EnvelopeCandidate] = []
    candidate_index = 1
    for interval_start, interval_end in _contiguous_eligible_intervals(regions):
        if interval_end - interval_start + 1e-9 < BH12_ENVELOPE_WIDTH_M:
            continue
        starts = {interval_start, interval_end - BH12_ENVELOPE_WIDTH_M}
        for sample in samples:
            if interval_start <= sample.lateral_offset_m <= interval_end:
                starts.add(
                    min(
                        interval_end - BH12_ENVELOPE_WIDTH_M,
                        max(interval_start, sample.lateral_offset_m),
                    )
                )
                starts.add(
                    min(
                        interval_end - BH12_ENVELOPE_WIDTH_M,
                        max(interval_start, sample.lateral_offset_m - BH12_ENVELOPE_WIDTH_M),
                    )
                )
        for start in sorted(starts):
            end = start + BH12_ENVELOPE_WIDTH_M
            inside = tuple(
                sample
                for sample in samples
                if start - 1e-9 <= sample.lateral_offset_m <= end + 1e-9
                and by_region[sample.region_id].bh12_eligible
            )
            if not inside or {item.station_id for item in inside} != station_ids:
                continue
            station_coverage_ok = True
            for station_id in station_ids:
                offsets = sorted(
                    item.lateral_offset_m
                    for item in inside
                    if item.station_id == station_id
                )
                if (
                    not offsets
                    or offsets[0] - start > maximum_sample_spacing_m
                    or end - offsets[-1] > maximum_sample_spacing_m
                    or any(
                        right - left > maximum_sample_spacing_m
                        for left, right in zip(offsets, offsets[1:])
                    )
                ):
                    station_coverage_ok = False
                    break
            if not station_coverage_ok:
                continue
            controlling = min(
                inside,
                key=lambda item: (item.vertical_clearance_m, item.sample_id),
            )
            candidates.append(
                Bh12EnvelopeCandidate(
                    candidate_id=f"bh12-envelope-{candidate_index:04d}",
                    start_offset_m=float(start),
                    end_offset_m=float(end),
                    minimum_clearance_m=controlling.vertical_clearance_m,
                    controlling_sample_id=controlling.sample_id,
                    station_ids=tuple(sorted(station_ids)),
                )
            )
            candidate_index += 1
    if not candidates:
        raise ClearanceMeasurementError("no feasible 10-foot traveled-way envelope")
    selected = max(
        candidates,
        key=lambda item: (item.minimum_clearance_m, -item.start_offset_m),
    )
    bh13_samples = tuple(
        sample for sample in samples if by_region[sample.region_id].include_in_bh13
    )
    if not bh13_samples:
        raise ClearanceMeasurementError("no BH13-applicable samples")
    bh13_control = min(
        bh13_samples,
        key=lambda item: (item.vertical_clearance_m, item.sample_id),
    )
    values = tuple(sample.vertical_clearance_m for sample in samples)

    def edge_minimum(edge: StationEdge) -> float | None:
        edge_values = [
            sample.vertical_clearance_m
            for sample in samples
            if sample.station_edge is edge
        ]
        return min(edge_values) if edge_values else None

    return HighwayClearanceMeasurement(
        feature_id=feature.feature_id,
        feature_source_pointer=feature.source_pointer,
        regions=tuple(sorted(regions, key=lambda item: item.start_offset_m)),
        samples=tuple(sorted(samples, key=lambda item: (item.longitudinal_m, item.lateral_offset_m, item.sample_id))),
        bh12_candidates=tuple(candidates),
        selected_bh12_candidate_id=selected.candidate_id,
        bh12_raw_m=selected.minimum_clearance_m,
        bh13_raw_m=bh13_control.vertical_clearance_m,
        bh13_controlling_sample_id=bh13_control.sample_id,
        overall_minimum_m=min(values),
        overall_maximum_m=max(values),
        leading_edge_minimum_m=edge_minimum(StationEdge.LEADING),
        trailing_edge_minimum_m=edge_minimum(StationEdge.TRAILING),
        recorded_bh12_ft=feature.recorded_bh12_ft,
        recorded_bh13_ft=feature.recorded_bh13_ft,
    )


@dataclass(frozen=True, slots=True)
class NonHighwayMeasurementDisposition:
    feature_id: str
    product: str
    status: str
    reason: str
    profile_version: str = CLEARANCE_PROFILE_VERSION

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def railroad_measurement_gate(
    *,
    binding: FeatureBinding,
    feature: CanonicalFeature,
    track_reference_confirmed: bool,
) -> NonHighwayMeasurementDisposition:
    if binding.status is not FeatureBindingStatus.BOUND or feature.kind is not FeatureKind.RAILROAD:
        raise ClearanceMeasurementError("railroad feature binding is not valid")
    return NonHighwayMeasurementDisposition(
        feature.feature_id,
        "BRR02",
        "review_required" if not track_reference_confirmed else "measurement_profile_pending",
        "track_reference_required" if not track_reference_confirmed else "railroad_geometry_profile_not_implemented",
    )


def navigation_measurement_gate(
    *,
    binding: FeatureBinding,
    feature: CanonicalFeature,
    channel_limits_confirmed: bool,
    datum_confirmed: bool,
    timestamp_confirmed: bool,
) -> NonHighwayMeasurementDisposition:
    if binding.status is not FeatureBindingStatus.BOUND or feature.kind is not FeatureKind.WATERWAY:
        raise ClearanceMeasurementError("waterway feature binding is not valid")
    missing = []
    if feature.navigable is not True:
        missing.append("navigable_waterway_required")
    if not channel_limits_confirmed:
        missing.append("channel_limits_required")
    if not datum_confirmed:
        missing.append("controlling_datum_required")
    if not timestamp_confirmed:
        missing.append("measurement_timestamp_required")
    return NonHighwayMeasurementDisposition(
        feature.feature_id,
        "BN02",
        "not_measurable" if missing else "measurement_profile_pending",
        ",".join(missing) if missing else "navigation_survey_profile_not_implemented",
    )


def build_feature_measurement_workflow(
    *,
    feature_rows: Sequence[Mapping[str, object]],
    geometry_result: Mapping[str, object],
    under_feature: str | None,
) -> dict[str, object]:
    """Describe the active feature-binding/reviewer gate for raw geometry.

    The current automated geometry does not infer lane/shoulder boundaries.  It
    therefore binds each detected candidate to canonical data when possible but
    requires explicit region/station review before BH12/BH13 can be calculated.
    """

    features = canonical_features({"Features": list(feature_rows)})
    kind = {
        "highway": FeatureKind.HIGHWAY,
        "railroad": FeatureKind.RAILROAD,
        "waterway": FeatureKind.WATERWAY,
        "pathway": FeatureKind.PEDESTRIAN,
    }.get(str(under_feature or "").strip().lower())
    if kind is None:
        return {
            "status": "review_required",
            "reason": "under_feature_type_ambiguous",
            "bindings": [],
            "feature_binding_version": FEATURE_BINDING_VERSION,
            "clearance_profile_version": CLEARANCE_PROFILE_VERSION,
        }
    roadway_rows = geometry_result.get("roadways")
    if not isinstance(roadway_rows, list):
        roadway_rows = geometry_result.get("roadway_candidates")
    if not isinstance(roadway_rows, list) or not roadway_rows:
        roadway_rows = [{}]
    bindings = []
    for index, row in enumerate(roadway_rows, start=1):
        point_count = 1
        if isinstance(row, Mapping):
            point_count = max(
                1,
                int(row.get("road_point_count") or 0)
                + int(row.get("soffit_point_count") or 0),
            )
        detected = DetectedUnderFeature(
            detected_id=f"detected-{kind.value}-{index}",
            kind=kind,
            point_count=point_count,
            geometry_receipt_id=f"geometry:{kind.value}:{index}",
        )
        binding = bind_detected_feature(detected, features)
        bindings.append(binding.to_dict())
    unresolved = [
        binding for binding in bindings if binding["status"] != "bound"
    ]
    if unresolved:
        status = "review_required"
        reason = "canonical_feature_binding_required"
    elif kind is FeatureKind.HIGHWAY:
        status = "review_required"
        reason = "lane_shoulder_and_station_review_required"
    elif kind is FeatureKind.RAILROAD:
        status = "review_required"
        reason = "track_reference_review_required"
    elif kind is FeatureKind.WATERWAY:
        status = "not_measurable"
        reason = "channel_datum_timestamp_and_survey_control_required"
    else:
        status = "not_measurable"
        reason = "measurement_profile_not_available"
    return {
        "status": status,
        "reason": reason,
        "bindings": bindings,
        "feature_binding_version": FEATURE_BINDING_VERSION,
        "clearance_profile_version": CLEARANCE_PROFILE_VERSION,
        "brm_graph_ready": False,
        "bh12_ready": False,
        "bh13_ready": False,
    }


__all__ = [
    "BH12_ENVELOPE_WIDTH_M",
    "CLEARANCE_PROFILE_VERSION",
    "Bh12EnvelopeCandidate",
    "ClearanceMeasurementError",
    "ClearanceRegion",
    "ClearanceSample",
    "HighwayClearanceMeasurement",
    "NonHighwayMeasurementDisposition",
    "RegionType",
    "StationEdge",
    "measure_highway_clearance",
    "build_feature_measurement_workflow",
    "navigation_measurement_gate",
    "railroad_measurement_gate",
]
