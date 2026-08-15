"""Exact published-revision facts used by the LCCA simulator."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.analytics.projection import BridgeAnalysisSnapshot, FeatureSelectionStatus


@dataclass(frozen=True, slots=True)
class GeometryEvidence:
    deck_area: float | None
    method: str
    source_fields: tuple[str, ...]
    status: str


@dataclass(frozen=True, slots=True)
class NhsEvidence:
    value: bool | None
    status: str
    source: str
    raw_value: str | None


@dataclass(frozen=True, slots=True)
class BridgeLccaFacts:
    workspace_id: str
    state_code: int
    bridge_number: str
    data_year: int
    snapshot_revision_id: str
    canonical_sha256: str
    bridge_name: str | None
    age_at_snapshot: int | None
    component_ratings: tuple[tuple[str, float | None], ...]
    controlling_rating: float | None
    controlling_basis: str
    geometry: GeometryEvidence
    nhs: NhsEvidence
    deck_material_codes: tuple[str, ...]
    span_material_codes: tuple[str, ...]
    substructure_material_codes: tuple[str, ...]


def _yes_no(value: str | None) -> bool | None:
    normalized = str(value or "").strip().upper()
    if normalized in {"Y", "1", "YES"}:
        return True
    if normalized in {"N", "0", "NO"}:
        return False
    return None


def bridge_lcca_facts(snapshot: BridgeAnalysisSnapshot) -> BridgeLccaFacts:
    calculated = snapshot.calculated_deck_area
    if calculated is not None and calculated > 0:
        geometry = GeometryEvidence(
            deck_area=float(calculated),
            method="reported_calculated_deck_area",
            source_fields=("BG12",),
            status="available",
        )
    elif (
        snapshot.total_bridge_length is not None
        and snapshot.total_bridge_length > 0
        and snapshot.bridge_width_out_to_out is not None
        and snapshot.bridge_width_out_to_out > 0
    ):
        geometry = GeometryEvidence(
            deck_area=float(snapshot.total_bridge_length * snapshot.bridge_width_out_to_out),
            method="length_times_out_to_out_width",
            source_fields=("BG02", "BG05"),
            status="derived",
        )
    else:
        geometry = GeometryEvidence(
            deck_area=None,
            method="unavailable",
            source_fields=(),
            status="unavailable",
        )

    feature = snapshot.carried_features.selected
    if snapshot.carried_features.status is FeatureSelectionStatus.SELECTED and feature is not None:
        value = _yes_no(feature.nhs_designation)
        nhs = NhsEvidence(
            value=value,
            status="available" if value is not None else "invalid",
            source="selected_carried_highway_feature",
            raw_value=feature.nhs_designation,
        )
    else:
        nhs = NhsEvidence(
            value=None,
            status=snapshot.carried_features.status.value,
            source="selected_carried_highway_feature",
            raw_value=None,
        )

    return BridgeLccaFacts(
        workspace_id=snapshot.identity.workspace_id,
        state_code=snapshot.state_code,
        bridge_number=snapshot.bridge_number,
        data_year=snapshot.data_year,
        snapshot_revision_id=snapshot.snapshot_revision_id,
        canonical_sha256=snapshot.canonical_sha256,
        bridge_name=snapshot.bridge_name,
        age_at_snapshot=snapshot.age_at_snapshot,
        component_ratings=(
            ("deck", snapshot.condition.bc01),
            ("superstructure", snapshot.condition.bc02),
            ("substructure", snapshot.condition.bc03),
            ("culvert", snapshot.condition.bc04),
        ),
        controlling_rating=snapshot.condition.controlling_rating,
        controlling_basis=snapshot.condition.basis,
        geometry=geometry,
        nhs=nhs,
        deck_material_codes=tuple(
            sorted(
                {
                    item.deck_material_code
                    for item in snapshot.span_materials
                    if item.deck_material_code
                }
            )
        ),
        span_material_codes=tuple(
            sorted(
                {item.span_material_code for item in snapshot.span_materials if item.span_material_code}
            )
        ),
        substructure_material_codes=tuple(
            sorted(
                {item.material_code for item in snapshot.substructure_materials if item.material_code}
            )
        ),
    )
