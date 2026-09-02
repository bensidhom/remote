"""SNBI-formatted projections of raw point-cloud clearance measurements."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.point_cloud.clearance import HighwayClearanceMeasurement


SNBI_CLEARANCE_PROJECTION_VERSION = "bridgeguard-snbi-clearance-projection-v1"
_FEET_PER_METRE = Decimal("3.280839895013123")


class SnbiClearanceProjectionError(ValueError):
    pass


def round_down_metres_to_tenth_foot(value_m: float) -> Decimal:
    value = Decimal(str(value_m))
    if not value.is_finite() or value < 0:
        raise SnbiClearanceProjectionError("clearance must be finite and nonnegative")
    feet = value * _FEET_PER_METRE
    if feet >= Decimal("100"):
        return Decimal("99.9")
    return feet.quantize(Decimal("0.1"), rounding=ROUND_FLOOR)


@dataclass(frozen=True, slots=True)
class AcceptedHighwayClearanceProjection:
    feature_id: str
    bh12: str
    bh13: str
    source_profile_version: str
    accepted_by: str
    acceptance_reason: str
    projection_version: str = SNBI_CLEARANCE_PROJECTION_VERSION
    submission_ready: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CalculatedHighwayClearanceProjection:
    feature_id: str
    bh12: str
    bh13: str
    source_profile_version: str
    projection_version: str = SNBI_CLEARANCE_PROJECTION_VERSION
    official: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def project_calculated_highway_clearance(
    measurement: "HighwayClearanceMeasurement",
) -> CalculatedHighwayClearanceProjection:
    """Format an automatic result without representing it as official inventory."""

    return CalculatedHighwayClearanceProjection(
        feature_id=measurement.feature_id,
        bh12=format(round_down_metres_to_tenth_foot(measurement.bh12_raw_m), ".1f"),
        bh13=format(round_down_metres_to_tenth_foot(measurement.bh13_raw_m), ".1f"),
        source_profile_version=measurement.profile_version,
    )


def project_accepted_highway_clearance(
    measurement: "HighwayClearanceMeasurement",
    *,
    accepted: bool,
    accepted_by: str,
    acceptance_reason: str,
) -> AcceptedHighwayClearanceProjection:
    if not accepted:
        raise SnbiClearanceProjectionError("human acceptance is required")
    actor = accepted_by.strip()
    reason = acceptance_reason.strip()
    if not actor or not reason:
        raise SnbiClearanceProjectionError("acceptance actor and reason are required")
    calculated = project_calculated_highway_clearance(measurement)
    return AcceptedHighwayClearanceProjection(
        feature_id=measurement.feature_id,
        bh12=calculated.bh12,
        bh13=calculated.bh13,
        source_profile_version=measurement.profile_version,
        accepted_by=actor,
        acceptance_reason=reason,
    )


__all__ = [
    "SNBI_CLEARANCE_PROJECTION_VERSION",
    "AcceptedHighwayClearanceProjection",
    "CalculatedHighwayClearanceProjection",
    "SnbiClearanceProjectionError",
    "project_calculated_highway_clearance",
    "project_accepted_highway_clearance",
    "round_down_metres_to_tenth_foot",
]
