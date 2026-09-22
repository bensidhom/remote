"""Operational bridge metrics and one controlling-condition definition.

The active metrics are BridgeGuard inventory summaries.  They must not be
presented as FHWA Transportation Performance Management (TPM) measures.  The
two federal-shaped definitions remain explicit but disabled so a model cannot
silently substitute the easier operational calculation for a regulated one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Mapping

from .models import (
    MetricClassification,
    MetricDefinition,
    MetricRegistry,
    MetricStatus,
    SemanticCatalog,
)


METRIC_REGISTRY_VERSION = "bridgeguard-metrics-v4-safe-joined-dimensions"


class ConditionBasis(str, Enum):
    REPORTED_BC13 = "reported_bc13"
    PRIMARY_MINIMUM = "derived_primary_min"
    CULVERT_FALLBACK = "culvert_only"
    APPLICABLE_MINIMUM = "derived_applicable_min"
    UNAVAILABLE = "unknown"


class ConditionClass(str, Enum):
    GOOD = "Good"
    FAIR = "Fair"
    POOR = "Poor"


@dataclass(frozen=True, slots=True)
class ConditionInput:
    field: str
    rating: float


@dataclass(frozen=True, slots=True)
class ControllingCondition:
    rating: float | None
    condition_class: ConditionClass | None
    basis: ConditionBasis
    basis_fields: tuple[str, ...]
    conflict: bool
    conflict_reasons: tuple[str, ...]
    valid_inputs: tuple[ConditionInput, ...]
    invalid_fields: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "rating": self.rating,
            "condition_class": (
                self.condition_class.value if self.condition_class is not None else None
            ),
            "basis": self.basis.value,
            "basis_fields": list(self.basis_fields),
            "conflict": self.conflict,
            "conflict_reasons": list(self.conflict_reasons),
            "valid_inputs": [
                {"field": item.field, "rating": item.rating}
                for item in self.valid_inputs
            ],
            "invalid_fields": list(self.invalid_fields),
        }


_UNSET = object()
_CONDITION_FIELDS = {
    "BC01": "deck_condition_rating",
    "BC02": "superstructure_condition_rating",
    "BC03": "substructure_condition_rating",
    "BC04": "culvert_condition_rating",
    "BC13": "lowest_condition_rating_code",
}


def _record_value(record: Mapping[str, object] | None, field: str) -> object:
    if record is None:
        return None
    if field in record:
        return record[field]
    return record.get(_CONDITION_FIELDS[field])


def _rating_state(value: object) -> tuple[str, float | None]:
    """Classify a source value as absent, valid, or invalid.

    Ratings are numeric 0 through 9.  SNBI's ``N`` code means the component is
    not applicable, so it is treated as absent.  That distinction is important
    for culverts: BC01--BC03 are normally ``N`` and BC04 is the applicable
    condition rating.  Other alphabetic codes, booleans, infinities, NaN, and
    out-of-range numbers remain invalid rather than silently activating a
    fallback.
    """

    if value is None or (isinstance(value, str) and not value.strip()):
        return "absent", None
    if isinstance(value, str) and value.strip().upper() == "N":
        return "absent", None
    if isinstance(value, bool):
        return "invalid", None
    try:
        rating = float(value)
    except (TypeError, ValueError, OverflowError):
        return "invalid", None
    if not math.isfinite(rating) or not 0.0 <= rating <= 9.0:
        return "invalid", None
    return "valid", rating


def _condition_class(rating: float | None) -> ConditionClass | None:
    if rating is None:
        return None
    if rating >= 7.0:
        return ConditionClass.GOOD
    if rating >= 5.0:
        return ConditionClass.FAIR
    return ConditionClass.POOR


def compute_controlling_condition(
    record: Mapping[str, object] | None = None,
    *,
    bc01: object = _UNSET,
    bc02: object = _UNSET,
    bc03: object = _UNSET,
    bc04: object = _UNSET,
    bc13: object = _UNSET,
) -> ControllingCondition:
    """Return the single operational condition result used across features.

    FHWA defines B.C.12 and B.C.13 from the lowest applicable value among
    B.C.01--B.C.04.  ``N`` means not applicable and is ignored.  A normal
    bridge usually has B.C.04=N; a culvert commonly has B.C.01--B.C.03=N.
    B.C.13 is calculated by FHWA, so it is used only when no component rating
    is available and is otherwise checked against the component minimum.
    """

    supplied = {
        "BC01": _record_value(record, "BC01") if bc01 is _UNSET else bc01,
        "BC02": _record_value(record, "BC02") if bc02 is _UNSET else bc02,
        "BC03": _record_value(record, "BC03") if bc03 is _UNSET else bc03,
        "BC04": _record_value(record, "BC04") if bc04 is _UNSET else bc04,
        "BC13": _record_value(record, "BC13") if bc13 is _UNSET else bc13,
    }
    states = {field: _rating_state(value) for field, value in supplied.items()}
    invalid_fields = tuple(
        field for field in ("BC01", "BC02", "BC03", "BC04", "BC13")
        if states[field][0] == "invalid"
    )
    conflicts = [f"invalid_rating:{field}" for field in invalid_fields]
    valid_inputs = tuple(
        ConditionInput(field, float(states[field][1]))
        for field in ("BC01", "BC02", "BC03", "BC04", "BC13")
        if states[field][0] == "valid"
    )

    primary = {
        field: float(states[field][1])
        for field in ("BC01", "BC02", "BC03")
        if states[field][0] == "valid"
    }
    applicable = dict(primary)
    if states["BC04"][0] == "valid":
        applicable["BC04"] = float(states["BC04"][1])
    bc13_rating = (
        float(states["BC13"][1]) if states["BC13"][0] == "valid" else None
    )

    if applicable:
        rating = min(applicable.values())
        basis_fields = tuple(
            field
            for field in ("BC01", "BC02", "BC03", "BC04")
            if field in applicable
            and math.isclose(applicable[field], rating, rel_tol=0.0, abs_tol=1e-9)
        )
        if primary and "BC04" in applicable:
            basis = ConditionBasis.APPLICABLE_MINIMUM
        elif primary:
            basis = ConditionBasis.PRIMARY_MINIMUM
        else:
            basis = ConditionBasis.CULVERT_FALLBACK
        if bc13_rating is not None and not math.isclose(
            bc13_rating, rating, rel_tol=0.0, abs_tol=1e-9
        ):
            conflicts.append("bc13_disagrees_with_applicable_minimum")
    elif bc13_rating is not None:
        rating = bc13_rating
        basis = ConditionBasis.REPORTED_BC13
        basis_fields = ("BC13",)
    else:
        rating = None
        basis = ConditionBasis.UNAVAILABLE
        basis_fields = ()

    return ControllingCondition(
        rating=rating,
        condition_class=_condition_class(rating),
        basis=basis,
        basis_fields=basis_fields,
        conflict=bool(conflicts),
        conflict_reasons=tuple(conflicts),
        valid_inputs=valid_inputs,
        invalid_fields=invalid_fields,
    )


# Alias with the same semantics for callers that prefer a noun-oriented name.
controlling_condition = compute_controlling_condition


_SNAPSHOT_DIMENSIONS = (
    "v_published_bridge_snapshots.state_code",
    "v_published_bridge_snapshots.data_year",
    "v_published_bridge_snapshots.county_code",
    "v_published_bridge_snapshots.owner",
    "v_published_bridge_snapshots.maintenance_responsibility",
    "v_county_bridge_inventory.county_name",
    "v_current_county_bridge_inventory.county_name",
)
_CONDITION_DIMENSIONS = (
    "v_bridge_condition.state_code",
    "v_bridge_condition.data_year",
    "v_bridge_condition.condition_class",
    "v_published_bridge_snapshots.data_year",
    "v_published_bridge_snapshots.county_code",
    "v_published_bridge_snapshots.owner",
    "v_published_bridge_snapshots.maintenance_responsibility",
)
_FEATURE_DIMENSIONS = (
    "v_carried_highway_features.state_code",
    "v_carried_highway_features.data_year",
    "v_carried_highway_features.nhs_designation",
    "v_carried_highway_features.functional_classification",
)
_DECK_MATERIAL_DIMENSIONS = (
    "v_bridge_deck_material_summary.state_code",
    "v_bridge_deck_material_summary.data_year",
    "v_bridge_deck_material_summary.has_concrete_deck",
    "v_bridge_deck_material_summary.has_known_deck_material",
)
_CURRENT_DECK_MATERIAL_DIMENSIONS = (
    "v_current_bridge_deck_material_summary.state_code",
    "v_current_bridge_deck_material_summary.data_year",
    "v_current_bridge_deck_material_summary.has_concrete_deck",
    "v_current_bridge_deck_material_summary.has_known_deck_material",
)


DEFAULT_METRIC_REGISTRY = MetricRegistry(
    definition_version=METRIC_REGISTRY_VERSION,
    metrics=(
        MetricDefinition(
            metric_id="inventory.bridge_count",
            label="Available bridge count",
            description="Count of governed or loaded-legacy bridge snapshots in the selected time scope.",
            source_resource_id="v_published_bridge_snapshots",
            expression="COUNT(*)",
            required_columns=(),
            unit="bridges",
            null_semantics="Every source row is counted; governed rows are used when present, otherwise loaded legacy inventory is used.",
            denominator_semantics="Not a ratio; grain guarantees one row per bridge and exact data year.",
            time_semantics="Explicit year is exact; latest is one global available inventory year for the state.",
            allowed_dimensions=_SNAPSHOT_DIMENSIONS,
            classification=MetricClassification.OPERATIONAL_INVENTORY,
        ),
        MetricDefinition(
            metric_id="inventory.concrete_deck_bridge_count",
            label="Concrete-deck bridge count",
            description=(
                "Available bridge snapshots having at least one span-set BSP09 code in "
                "C01, C02, C03, C04, C05, CX, CP-T, or CR-T."
            ),
            source_resource_id="v_bridge_deck_material_summary",
            expression="SUM(has_concrete_deck)",
            required_columns=("has_concrete_deck",),
            unit="bridges",
            null_semantics="Every available bridge summary is 1 or 0; missing and no-deck material are not concrete.",
            denominator_semantics="Not a ratio; one summary row exists per available bridge snapshot.",
            time_semantics="Explicit year is exact; latest is one global available inventory year.",
            allowed_dimensions=_DECK_MATERIAL_DIMENSIONS,
            classification=MetricClassification.OPERATIONAL_INVENTORY,
        ),
        MetricDefinition(
            metric_id="inventory.concrete_deck_bridge_percent",
            label="Concrete-deck bridge percentage",
            description=(
                "Percentage of selected available bridge snapshots having at least one "
                "concrete BSP09 deck-material code."
            ),
            source_resource_id="v_bridge_deck_material_summary",
            expression="100.0 * SUM(has_concrete_deck) / NULLIF(COUNT(*), 0)",
            required_columns=("has_concrete_deck",),
            unit="percent",
            null_semantics="Missing, unknown, and code 0 deck material remain in the denominator and not the numerator.",
            denominator_semantics="All selected available bridge snapshots; zero selected rows produce null.",
            time_semantics="Explicit year is exact; latest is one global available inventory year.",
            allowed_dimensions=_DECK_MATERIAL_DIMENSIONS,
            classification=MetricClassification.OPERATIONAL_INVENTORY,
        ),
        MetricDefinition(
            metric_id="inventory.current_concrete_deck_bridge_count",
            label="Current concrete-deck bridge count",
            description=(
                "Bridges in the latest available year having at least one span-set BSP09 code in "
                "C01, C02, C03, C04, C05, CX, CP-T, or CR-T."
            ),
            source_resource_id="v_current_bridge_deck_material_summary",
            expression="SUM(has_concrete_deck)",
            required_columns=("has_concrete_deck",),
            unit="bridges",
            null_semantics="Every latest-year bridge summary is 1 or 0; missing and no-deck material are not concrete.",
            denominator_semantics="Not a ratio; one summary row exists per bridge in the latest available year.",
            time_semantics="Every bridge contributes from the same global latest available inventory year.",
            allowed_dimensions=_CURRENT_DECK_MATERIAL_DIMENSIONS,
            classification=MetricClassification.OPERATIONAL_INVENTORY,
        ),
        MetricDefinition(
            metric_id="inventory.current_concrete_deck_bridge_percent",
            label="Current concrete-deck bridge percentage",
            description=(
                "Percentage of bridges in the latest available year having at least one concrete "
                "BSP09 deck-material code."
            ),
            source_resource_id="v_current_bridge_deck_material_summary",
            expression="100.0 * SUM(has_concrete_deck) / NULLIF(COUNT(*), 0)",
            required_columns=("has_concrete_deck",),
            unit="percent",
            null_semantics="Missing, unknown, and code 0 deck material remain in the denominator and not the numerator.",
            denominator_semantics="All bridges in the latest available year; zero selected rows produce null.",
            time_semantics="Every bridge contributes from the same global latest available inventory year.",
            allowed_dimensions=_CURRENT_DECK_MATERIAL_DIMENSIONS,
            classification=MetricClassification.OPERATIONAL_INVENTORY,
        ),
        MetricDefinition(
            metric_id="inventory.good_bridge_count",
            label="Operational Good bridge count",
            description="Available snapshots whose deterministic controlling class is Good (rating 7 through 9).",
            source_resource_id="v_bridge_condition",
            expression="SUM(CASE WHEN condition_class = 'Good' THEN 1 ELSE 0 END)",
            required_columns=("condition_class",),
            unit="bridges",
            null_semantics="Unknown controlling ratings are not counted as Good.",
            denominator_semantics="Not a percentage; use bridge_count separately when a share is requested.",
            time_semantics="Condition is evaluated at each exact data year; latest is one global state inventory year.",
            allowed_dimensions=_CONDITION_DIMENSIONS,
            classification=MetricClassification.OPERATIONAL_INVENTORY,
        ),
        MetricDefinition(
            metric_id="inventory.fair_bridge_count",
            label="Operational Fair bridge count",
            description="Available snapshots whose deterministic controlling class is Fair (rating 5 through 6.999...).",
            source_resource_id="v_bridge_condition",
            expression="SUM(CASE WHEN condition_class = 'Fair' THEN 1 ELSE 0 END)",
            required_columns=("condition_class",),
            unit="bridges",
            null_semantics="Unknown controlling ratings are not counted as Fair.",
            denominator_semantics="Not a percentage; use bridge_count separately when a share is requested.",
            time_semantics="Condition is evaluated at each exact data year; latest is one global state inventory year.",
            allowed_dimensions=_CONDITION_DIMENSIONS,
            classification=MetricClassification.OPERATIONAL_INVENTORY,
        ),
        MetricDefinition(
            metric_id="inventory.poor_bridge_count",
            label="Operational Poor bridge count",
            description="Available snapshots whose deterministic controlling class is Poor (rating below 5).",
            source_resource_id="v_bridge_condition",
            expression="SUM(CASE WHEN condition_class = 'Poor' THEN 1 ELSE 0 END)",
            required_columns=("condition_class",),
            unit="bridges",
            null_semantics="Unknown controlling ratings are not counted as Poor.",
            denominator_semantics="Not a percentage; use bridge_count separately when a share is requested.",
            time_semantics="Condition is evaluated at each exact data year; latest is one global state inventory year.",
            allowed_dimensions=_CONDITION_DIMENSIONS,
            classification=MetricClassification.OPERATIONAL_INVENTORY,
        ),
        MetricDefinition(
            metric_id="inventory.unknown_condition_count",
            label="Unknown operational condition count",
            description="Available snapshots lacking a valid controlling condition rating.",
            source_resource_id="v_bridge_condition",
            expression="SUM(CASE WHEN controlling_rating IS NULL THEN 1 ELSE 0 END)",
            required_columns=("controlling_rating",),
            unit="bridges",
            null_semantics="A null controlling rating is the event being counted.",
            denominator_semantics="Not a ratio; expose alongside classified counts to make missingness visible.",
            time_semantics="Missingness is evaluated at each exact available data year.",
            allowed_dimensions=_CONDITION_DIMENSIONS,
            classification=MetricClassification.OPERATIONAL_INVENTORY,
        ),
        MetricDefinition(
            metric_id="inventory.good_bridge_percent",
            label="Operational Good bridge percentage",
            description="Good-class snapshots as a percentage of every selected available snapshot.",
            source_resource_id="v_bridge_condition",
            expression="100.0 * SUM(CASE WHEN condition_class = 'Good' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0)",
            required_columns=("condition_class",),
            unit="percent",
            null_semantics="Unknown controlling ratings remain in the denominator and not in the Good numerator.",
            denominator_semantics="All selected available bridge snapshots, including unknown condition; zero rows produce null.",
            time_semantics="Condition is evaluated at each exact data year; latest is one global state inventory year.",
            allowed_dimensions=_CONDITION_DIMENSIONS,
            classification=MetricClassification.OPERATIONAL_INVENTORY,
        ),
        MetricDefinition(
            metric_id="inventory.fair_bridge_percent",
            label="Operational Fair bridge percentage",
            description="Fair-class snapshots as a percentage of every selected available snapshot.",
            source_resource_id="v_bridge_condition",
            expression="100.0 * SUM(CASE WHEN condition_class = 'Fair' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0)",
            required_columns=("condition_class",),
            unit="percent",
            null_semantics="Unknown controlling ratings remain in the denominator and not in the Fair numerator.",
            denominator_semantics="All selected available bridge snapshots, including unknown condition; zero rows produce null.",
            time_semantics="Condition is evaluated at each exact data year; latest is one global state inventory year.",
            allowed_dimensions=_CONDITION_DIMENSIONS,
            classification=MetricClassification.OPERATIONAL_INVENTORY,
        ),
        MetricDefinition(
            metric_id="inventory.poor_bridge_percent",
            label="Operational Poor bridge percentage",
            description="Poor-class snapshots as a percentage of every selected available snapshot.",
            source_resource_id="v_bridge_condition",
            expression="100.0 * SUM(CASE WHEN condition_class = 'Poor' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0)",
            required_columns=("condition_class",),
            unit="percent",
            null_semantics="Unknown controlling ratings remain in the denominator and not in the Poor numerator.",
            denominator_semantics="All selected available bridge snapshots, including unknown condition; zero rows produce null.",
            time_semantics="Condition is evaluated at each exact data year; latest is one global state inventory year.",
            allowed_dimensions=_CONDITION_DIMENSIONS,
            classification=MetricClassification.OPERATIONAL_INVENTORY,
        ),
        MetricDefinition(
            metric_id="inventory.average_age_at_snapshot",
            label="Average age at inventory snapshot",
            description="Mean of data_year minus year_built for plausible known construction years.",
            source_resource_id="v_published_bridge_snapshots",
            expression="AVG(CASE WHEN year_built <= data_year THEN data_year - year_built END)",
            required_columns=("data_year", "year_built"),
            unit="years",
            null_semantics="Unknown or future year_built values are excluded, never coerced to zero.",
            denominator_semantics="Number of selected snapshots with a plausible known year_built.",
            time_semantics="Age is anchored to each row's data_year, not today's year.",
            allowed_dimensions=_SNAPSHOT_DIMENSIONS,
            classification=MetricClassification.OPERATIONAL_INVENTORY,
        ),
        MetricDefinition(
            metric_id="inventory.average_current_age",
            label="Average age as of requested calendar year",
            description="Mean age relative to a server-validated explicit as_of_year parameter.",
            source_resource_id="v_current_bridge_snapshots",
            expression="AVG(CASE WHEN year_built <= :as_of_year THEN :as_of_year - year_built END)",
            required_columns=("year_built",),
            unit="years",
            null_semantics="Unknown or future year_built values are excluded, never coerced to zero.",
            denominator_semantics="Current snapshots with a plausible known year_built.",
            time_semantics="Requires explicit as_of_year; the model may not substitute data_year or SQLite now().",
            allowed_dimensions=tuple(
                dimension.replace("v_published_bridge_snapshots", "v_current_bridge_snapshots")
                for dimension in _SNAPSHOT_DIMENSIONS
            ),
            classification=MetricClassification.OPERATIONAL_INVENTORY,
        ),
        MetricDefinition(
            metric_id="inventory.average_daily_traffic",
            label="Average carried-highway AADT",
            description="Mean AADT across selected available H01 carried-highway observations.",
            source_resource_id="v_carried_highway_features",
            expression="AVG(annual_average_daily_traffic)",
            required_columns=("annual_average_daily_traffic",),
            unit="vehicles_per_day",
            null_semantics="Unknown AADT is excluded and is not treated as zero.",
            denominator_semantics="Number of selected H01 feature observations with known AADT, not number of all bridges.",
            time_semantics="Inventory data_year and traffic observation year remain separately available.",
            allowed_dimensions=_FEATURE_DIMENSIONS,
            classification=MetricClassification.OPERATIONAL_INVENTORY,
        ),
        MetricDefinition(
            metric_id="inventory.total_bridge_length",
            label="Total reported bridge length",
            description="Sum of known total bridge length across selected available snapshots.",
            source_resource_id="v_published_bridge_snapshots",
            expression="SUM(total_bridge_length)",
            required_columns=("total_bridge_length",),
            unit="feet",
            null_semantics="Unknown lengths are excluded; report their count separately when material.",
            denominator_semantics="Not a ratio; one length contribution per selected bridge snapshot.",
            time_semantics="Explicit year is exact; latest selects the global latest available state inventory year.",
            allowed_dimensions=_SNAPSHOT_DIMENSIONS,
            classification=MetricClassification.OPERATIONAL_INVENTORY,
        ),
        MetricDefinition(
            metric_id="inventory.total_deck_area",
            label="Total calculated deck area",
            description="Sum of known governed BG16 deck area.",
            source_resource_id="v_published_bridge_snapshots",
            expression="SUM(calculated_deck_area)",
            required_columns=("calculated_deck_area",),
            unit="square_feet",
            null_semantics="Unknown deck areas are excluded; they are never treated as zero.",
            denominator_semantics="Not a ratio; one deck-area contribution per selected bridge snapshot.",
            time_semantics="Explicit year is exact; latest selects the global latest available state inventory year.",
            allowed_dimensions=_SNAPSHOT_DIMENSIONS,
            classification=MetricClassification.OPERATIONAL_INVENTORY,
        ),
        MetricDefinition(
            metric_id="inventory.known_deck_area_count",
            label="Bridge snapshots with known deck area",
            description="Count of selected bridge snapshots with a known governed BG16 deck area.",
            source_resource_id="v_published_bridge_snapshots",
            expression="COUNT(calculated_deck_area)",
            required_columns=("calculated_deck_area",),
            unit="bridges",
            null_semantics="Only non-null governed deck areas are counted; unknown areas are excluded rather than treated as zero.",
            denominator_semantics="Not a ratio; compare with inventory.bridge_count to report deck-area coverage.",
            time_semantics="Explicit year is exact; latest selects the global latest available state inventory year.",
            allowed_dimensions=_SNAPSHOT_DIMENSIONS,
            classification=MetricClassification.OPERATIONAL_INVENTORY,
        ),
        MetricDefinition(
            metric_id="inventory.average_deck_area",
            label="Average calculated deck area",
            description="Mean governed BG16 deck area across selected bridge snapshots.",
            source_resource_id="v_published_bridge_snapshots",
            expression="AVG(calculated_deck_area)",
            required_columns=("calculated_deck_area",),
            unit="square_feet",
            null_semantics="Unknown deck areas are excluded; they are never treated as zero.",
            denominator_semantics="Selected bridge snapshots with a known governed BG16 deck area.",
            time_semantics="Explicit year is exact; latest selects the global latest available state inventory year.",
            allowed_dimensions=_SNAPSHOT_DIMENSIONS,
            classification=MetricClassification.OPERATIONAL_INVENTORY,
        ),
        MetricDefinition(
            metric_id="federal.good_deck_area_percent",
            label="Federal NHS bridge deck area in Good condition",
            description="Placeholder for the regulated federal TPM bridge-condition measure.",
            source_resource_id="v_published_bridge_snapshots",
            expression="DISABLED_FEDERAL_TPM",
            required_columns=("calculated_deck_area", "nhs_designation"),
            unit="percent",
            null_semantics="Not implemented; no federal result may be calculated from current operational fields.",
            denominator_semantics="Would require the validated NHS deck-area denominator and all applicable federal eligibility rules.",
            time_semantics="Would require the federally defined performance period and reporting snapshot.",
            allowed_dimensions=("v_published_bridge_snapshots.state_code", "v_published_bridge_snapshots.data_year"),
            classification=MetricClassification.FEDERAL_PERFORMANCE_MEASURE,
            status=MetricStatus.DISABLED,
            disabled_reason=(
                "BridgeGuard has not implemented and validated the full federal NHS eligibility, "
                "deck-area weighting, condition, exclusions, and reporting-period rules."
            ),
        ),
        MetricDefinition(
            metric_id="federal.poor_deck_area_percent",
            label="Federal NHS bridge deck area in Poor condition",
            description="Placeholder for the regulated federal TPM bridge-condition measure.",
            source_resource_id="v_published_bridge_snapshots",
            expression="DISABLED_FEDERAL_TPM",
            required_columns=("calculated_deck_area", "nhs_designation"),
            unit="percent",
            null_semantics="Not implemented; no federal result may be calculated from current operational fields.",
            denominator_semantics="Would require the validated NHS deck-area denominator and all applicable federal eligibility rules.",
            time_semantics="Would require the federally defined performance period and reporting snapshot.",
            allowed_dimensions=("v_published_bridge_snapshots.state_code", "v_published_bridge_snapshots.data_year"),
            classification=MetricClassification.FEDERAL_PERFORMANCE_MEASURE,
            status=MetricStatus.DISABLED,
            disabled_reason=(
                "BridgeGuard has not implemented and validated the full federal NHS eligibility, "
                "deck-area weighting, condition, exclusions, and reporting-period rules."
            ),
        ),
    ),
)


def validate_metric_registry(
    catalog: SemanticCatalog,
    registry: MetricRegistry = DEFAULT_METRIC_REGISTRY,
) -> tuple[str, ...]:
    """Return stable definition errors against the reviewed semantic catalog."""

    errors: list[str] = []
    resources = catalog.resources_by_id
    for metric in registry.metrics:
        resource = resources.get(metric.source_resource_id)
        if resource is None:
            errors.append(f"{metric.metric_id}: unknown source {metric.source_resource_id}")
            continue
        for column in metric.required_columns:
            if column not in resource.column_ids:
                errors.append(f"{metric.metric_id}: unknown source column {column}")
        for dimension in metric.allowed_dimensions:
            try:
                resource_id, field_id = dimension.split(".", 1)
            except ValueError:
                errors.append(f"{metric.metric_id}: invalid dimension {dimension}")
                continue
            if not catalog.is_allowed_column(resource_id, field_id):
                errors.append(f"{metric.metric_id}: unknown dimension {dimension}")
    return tuple(sorted(errors))


__all__ = [
    "ConditionBasis",
    "ConditionClass",
    "ConditionInput",
    "ControllingCondition",
    "DEFAULT_METRIC_REGISTRY",
    "METRIC_REGISTRY_VERSION",
    "compute_controlling_condition",
    "controlling_condition",
    "validate_metric_registry",
]
