"""Versioned deterministic proposal contract for bridge point-cloud regions."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from app.services.point_cloud.vlm_protocol import ComponentClass


SEGMENTATION_ONTOLOGY_VERSION = "bridgeguard-point-cloud-components-v1"

_STRUCTURAL_CLASSES = frozenset(
    {
        ComponentClass.DECK_SLAB,
        ComponentClass.GIRDER_BEAM,
        ComponentClass.DIAPHRAGM_BRACING,
        ComponentClass.BEARING,
        ComponentClass.PIER_COLUMN,
        ComponentClass.ABUTMENT_WINGWALL,
        ComponentClass.PARAPET_RAILING_BARRIER,
    }
)

_RESTRICTION_MARKERS = (
    "sign",
    "signal",
    "utility",
    "pipe",
    "conduit",
    "cable",
    "brace",
    "bracing",
    "lighting",
    "gantry",
    "restriction",
)


class SegmentationContractError(ValueError):
    code = "point_cloud_segmentation_contract_error"


@dataclass(frozen=True, slots=True)
class DeterministicRegionEvidence:
    detached_from_structure: bool = False
    statistical_noise: bool = False
    connected_to_structure: bool = False
    within_bridge_footprint: bool = False
    measurement_obstruction: bool = False
    safety_relevant: bool = False
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.reasons) > 20 or any(
            not isinstance(reason, str) or not reason.strip() or len(reason) > 160
            for reason in self.reasons
        ):
            raise SegmentationContractError("Deterministic evidence reasons are invalid.")

    @property
    def protected(self) -> bool:
        return bool(
            self.connected_to_structure
            or self.measurement_obstruction
            or self.safety_relevant
        )

    @property
    def removal_eligible(self) -> bool:
        return bool(
            (self.detached_from_structure or self.statistical_noise)
            and not self.protected
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "detached_from_structure": self.detached_from_structure,
            "statistical_noise": self.statistical_noise,
            "connected_to_structure": self.connected_to_structure,
            "within_bridge_footprint": self.within_bridge_footprint,
            "measurement_obstruction": self.measurement_obstruction,
            "safety_relevant": self.safety_relevant,
            "protected": self.protected,
            "removal_eligible": self.removal_eligible,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class CandidateRegion:
    candidate_id: int
    proposed_class: ComponentClass
    point_count: int
    evidence: DeterministicRegionEvidence
    proposal_source: str
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.candidate_id < 0 or self.point_count < 1:
            raise SegmentationContractError("Candidate ID or point count is invalid.")
        if not isinstance(self.proposed_class, ComponentClass):
            raise SegmentationContractError("Candidate class is outside the ontology.")
        if not self.proposal_source.strip() or len(self.proposal_source) > 100:
            raise SegmentationContractError("Candidate proposal source is invalid.")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def protected(self) -> bool:
        return bool(
            self.proposed_class in _STRUCTURAL_CLASSES
            or self.proposed_class
            in {ComponentClass.UNDER_SURFACE, ComponentClass.ATTACHED_RESTRICTION}
            or self.evidence.protected
        )

    @property
    def removal_eligible(self) -> bool:
        return self.evidence.removal_eligible and not self.protected

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "proposed_class": self.proposed_class.value,
            "point_count": self.point_count,
            "proposal_source": self.proposal_source,
            "protected": self.protected,
            "removal_eligible": self.removal_eligible,
            "deterministic_evidence": self.evidence.to_dict(),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SegmentationProposalSet:
    ontology_version: str
    candidates: tuple[CandidateRegion, ...]
    proposed_point_count: int

    def __post_init__(self) -> None:
        if self.ontology_version != SEGMENTATION_ONTOLOGY_VERSION:
            raise SegmentationContractError("Segmentation ontology version is invalid.")
        ids = [candidate.candidate_id for candidate in self.candidates]
        if len(ids) != len(set(ids)):
            raise SegmentationContractError("Segmentation candidate IDs are duplicated.")
        if self.proposed_point_count != sum(
            candidate.point_count for candidate in self.candidates
        ):
            raise SegmentationContractError("Segmentation proposal point count is inconsistent.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ontology_version": self.ontology_version,
            "candidate_count": len(self.candidates),
            "proposed_point_count": self.proposed_point_count,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


def _proposed_class(component: Mapping[str, Any]) -> ComponentClass:
    kind = str(component.get("kind") or "").strip().casefold()
    text = " ".join(
        str(component.get(key) or "") for key in ("key", "label", "name", "kind")
    ).casefold()
    if any(marker in text for marker in ("diaphragm", "cross frame", "bracing", "truss")):
        return ComponentClass.DIAPHRAGM_BRACING
    if "bearing" in text:
        return ComponentClass.BEARING
    if "abutment" in text or "wingwall" in text or "wing wall" in text:
        return ComponentClass.ABUTMENT_WINGWALL
    if any(marker in text for marker in ("parapet", "railing", "guardrail", "barrier")):
        return ComponentClass.PARAPET_RAILING_BARRIER
    if any(marker in text for marker in _RESTRICTION_MARKERS):
        return ComponentClass.ATTACHED_RESTRICTION
    if "ground" in text or "road surface" in text:
        return ComponentClass.UNDER_SURFACE
    if "soffit" in text or "underside" in text:
        return ComponentClass.GIRDER_BEAM
    if "deck" in text:
        return ComponentClass.DECK_SLAB
    if "substructure" in text or "pier" in text or "abutment" in text:
        return ComponentClass.PIER_COLUMN
    if "noise" in text or "vegetation" in text:
        return ComponentClass.NONSTRUCTURAL_CLUTTER
    return {
        "slab": ComponentClass.DECK_SLAB,
        "beam": ComponentClass.GIRDER_BEAM,
        "edge": ComponentClass.PARAPET_RAILING_BARRIER,
        "column": ComponentClass.PIER_COLUMN,
        "surface": ComponentClass.UNDER_SURFACE,
        "unknown": ComponentClass.UNKNOWN_STRUCTURE,
    }.get(kind, ComponentClass.UNKNOWN_STRUCTURE)


def candidate_from_component(
    component: Mapping[str, Any],
    *,
    detached_fraction: float = 0.0,
    noise_fraction: float = 0.0,
) -> CandidateRegion:
    """Project one deterministic fitted component into the shared ontology."""

    try:
        candidate_id = int(component["id"])
        point_count = int(component["point_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SegmentationContractError("Component proposal identity is invalid.") from exc
    if not 0.0 <= float(detached_fraction) <= 1.0 or not 0.0 <= float(noise_fraction) <= 1.0:
        raise SegmentationContractError("Component proposal evidence fraction is invalid.")
    proposed = _proposed_class(component)
    structural = proposed in _STRUCTURAL_CLASSES
    under_surface = proposed is ComponentClass.UNDER_SURFACE
    attached_restriction = proposed is ComponentClass.ATTACHED_RESTRICTION
    evidence = DeterministicRegionEvidence(
        detached_from_structure=float(detached_fraction) >= 0.8,
        statistical_noise=float(noise_fraction) >= 0.8,
        connected_to_structure=structural or attached_restriction,
        within_bridge_footprint=True,
        measurement_obstruction=under_surface or attached_restriction,
        safety_relevant=structural or attached_restriction,
        reasons=tuple(
            reason
            for condition, reason in (
                (float(detached_fraction) >= 0.8, "detached_region_proposal"),
                (float(noise_fraction) >= 0.8, "statistical_noise_proposal"),
                (structural, "deterministic_structural_geometry"),
                (under_surface, "measurement_surface"),
                (attached_restriction, "attached_restriction_candidate"),
            )
            if condition
        ),
    )
    return CandidateRegion(
        candidate_id=candidate_id,
        proposed_class=proposed,
        point_count=point_count,
        evidence=evidence,
        proposal_source="deterministic_geometry_v1",
        metadata={
            "kind": str(component.get("kind") or "")[:50],
            "key": str(component.get("key") or "")[:80],
            "detached_fraction": round(float(detached_fraction), 6),
            "noise_fraction": round(float(noise_fraction), 6),
        },
    )


def proposal_set(candidates: Sequence[CandidateRegion]) -> SegmentationProposalSet:
    values = tuple(candidates)
    return SegmentationProposalSet(
        ontology_version=SEGMENTATION_ONTOLOGY_VERSION,
        candidates=values,
        proposed_point_count=sum(candidate.point_count for candidate in values),
    )


__all__ = [
    "CandidateRegion",
    "DeterministicRegionEvidence",
    "SEGMENTATION_ONTOLOGY_VERSION",
    "SegmentationContractError",
    "SegmentationProposalSet",
    "candidate_from_component",
    "proposal_set",
]
