"""Conservative fusion of deterministic region proposals and typed VLM review."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from types import MappingProxyType
from typing import Any, Mapping

from app.services.point_cloud.segmentation import (
    CandidateRegion,
    SegmentationContractError,
    SegmentationProposalSet,
)
from app.services.point_cloud.vlm_protocol import ComponentClass, MaterialClass


FUSION_PROFILE_VERSION = "bridgeguard-point-cloud-fusion-v1"


class ReviewStatus(str, Enum):
    REVIEWED = "reviewed"
    ABSTAINED = "abstained"
    UNREVIEWED = "unreviewed"


class FusedDisposition(str, Enum):
    KEEP_STRUCTURAL = "keep_structural"
    KEEP_RESTRICTION = "keep_restriction"
    KEEP_MEASUREMENT_SURFACE = "keep_measurement_surface"
    KEEP_UNKNOWN = "keep_unknown"
    REMOVE_CLUTTER = "remove_clutter"


@dataclass(frozen=True, slots=True)
class SemanticReview:
    candidate_id: int
    status: ReviewStatus
    is_bridge: bool | None
    component_class: ComponentClass | None
    material: MaterialClass | None
    confidence: float | None
    evidence: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.candidate_id < 0:
            raise SegmentationContractError("Semantic review candidate ID is invalid.")
        if self.confidence is not None and (
            not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0
        ):
            raise SegmentationContractError("Semantic review confidence is invalid.")
        if len(self.evidence) > 500 or len(self.evidence_ids) > 8:
            raise SegmentationContractError("Semantic review evidence is invalid.")
        if self.status is ReviewStatus.UNREVIEWED and any(
            value is not None
            for value in (self.is_bridge, self.component_class, self.material, self.confidence)
        ):
            raise SegmentationContractError("Unreviewed candidate contains a model verdict.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "status": self.status.value,
            "is_bridge": self.is_bridge,
            "component_class": self.component_class.value if self.component_class else None,
            "material": self.material.value if self.material else None,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True, slots=True)
class FusedCandidate:
    candidate: CandidateRegion
    review: SemanticReview
    disposition: FusedDisposition
    final_class: ComponentClass
    removal_reason: str | None

    @property
    def removed(self) -> bool:
        return self.disposition is FusedDisposition.REMOVE_CLUTTER

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate.candidate_id,
            "point_count": self.candidate.point_count,
            "proposed_class": self.candidate.proposed_class.value,
            "final_class": self.final_class.value,
            "disposition": self.disposition.value,
            "removed": self.removed,
            "removal_reason": self.removal_reason,
            "protected": self.candidate.protected,
            "removal_eligible": self.candidate.removal_eligible,
            "review": self.review.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class FusionResult:
    profile_version: str
    candidates: tuple[FusedCandidate, ...]
    review_requested: bool
    reviewed_point_count: int
    proposed_point_count: int
    review_coverage: float
    minimum_review_coverage: float
    analysis_incomplete: bool
    point_counts_by_disposition: Mapping[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "point_counts_by_disposition",
            MappingProxyType(dict(self.point_counts_by_disposition)),
        )

    @property
    def removed_candidate_ids(self) -> tuple[int, ...]:
        return tuple(item.candidate.candidate_id for item in self.candidates if item.removed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_version": self.profile_version,
            "review_requested": self.review_requested,
            "reviewed_point_count": self.reviewed_point_count,
            "proposed_point_count": self.proposed_point_count,
            "review_coverage": round(self.review_coverage, 6),
            "minimum_review_coverage": self.minimum_review_coverage,
            "analysis_incomplete": self.analysis_incomplete,
            "removed_candidate_ids": list(self.removed_candidate_ids),
            "point_counts_by_disposition": dict(self.point_counts_by_disposition),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


def semantic_review_from_mapping(
    candidate_id: int, value: Mapping[str, Any] | None
) -> SemanticReview:
    if value is None:
        return SemanticReview(
            candidate_id,
            ReviewStatus.UNREVIEWED,
            None,
            None,
            None,
            None,
            "",
            (),
        )
    try:
        if type(value.get("abstained")) is not bool or type(value.get("is_bridge")) is not bool:
            raise TypeError
        abstained = value["abstained"]
        component_class = ComponentClass(str(value["component_class"]))
        material = MaterialClass(str(value.get("material") or "unknown"))
        confidence = float(value["confidence"])
        is_bridge = value["is_bridge"]
        evidence = str(value.get("evidence") or "")[:500]
        raw_evidence_ids = value.get("evidence_ids") or []
        if not isinstance(raw_evidence_ids, list):
            raise TypeError
        evidence_ids = tuple(str(item) for item in raw_evidence_ids)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise TypeError
    except (KeyError, TypeError, ValueError) as exc:
        raise SegmentationContractError("Semantic review mapping is invalid.") from exc
    if abstained != (component_class is ComponentClass.INSUFFICIENT_EVIDENCE):
        raise SegmentationContractError(
            "Semantic review abstention and component class disagree."
        )
    return SemanticReview(
        candidate_id=candidate_id,
        status=ReviewStatus.ABSTAINED if abstained else ReviewStatus.REVIEWED,
        is_bridge=is_bridge,
        component_class=component_class,
        material=material,
        confidence=confidence,
        evidence=evidence,
        evidence_ids=evidence_ids,
    )


def _disposition(
    candidate: CandidateRegion,
    review: SemanticReview,
    *,
    minimum_clutter_confidence: float,
) -> tuple[FusedDisposition, ComponentClass, str | None]:
    if candidate.proposed_class is ComponentClass.ATTACHED_RESTRICTION:
        return (
            FusedDisposition.KEEP_RESTRICTION,
            ComponentClass.ATTACHED_RESTRICTION,
            None,
        )
    if candidate.proposed_class is ComponentClass.UNDER_SURFACE:
        return (
            FusedDisposition.KEEP_MEASUREMENT_SURFACE,
            ComponentClass.UNDER_SURFACE,
            None,
        )
    if candidate.protected:
        return FusedDisposition.KEEP_STRUCTURAL, candidate.proposed_class, None
    if review.status is ReviewStatus.UNREVIEWED or review.status is ReviewStatus.ABSTAINED:
        return FusedDisposition.KEEP_UNKNOWN, ComponentClass.UNKNOWN_STRUCTURE, None
    assert review.component_class is not None
    if review.component_class is ComponentClass.ATTACHED_RESTRICTION:
        return (
            FusedDisposition.KEEP_RESTRICTION,
            ComponentClass.ATTACHED_RESTRICTION,
            None,
        )
    if (
        candidate.removal_eligible
        and review.is_bridge is False
        and review.component_class is ComponentClass.NONSTRUCTURAL_CLUTTER
        and review.confidence is not None
        and review.confidence >= minimum_clutter_confidence
    ):
        return (
            FusedDisposition.REMOVE_CLUTTER,
            ComponentClass.NONSTRUCTURAL_CLUTTER,
            "deterministic_removal_evidence_and_high_confidence_vlm_clutter",
        )
    if review.is_bridge is True and review.component_class is not ComponentClass.NONSTRUCTURAL_CLUTTER:
        return FusedDisposition.KEEP_STRUCTURAL, review.component_class, None
    return FusedDisposition.KEEP_UNKNOWN, ComponentClass.UNKNOWN_STRUCTURE, None


def fuse_component_candidates(
    proposals: SegmentationProposalSet,
    reviews: Mapping[int, Mapping[str, Any]],
    *,
    review_requested: bool,
    minimum_clutter_confidence: float = 0.9,
    minimum_review_coverage: float = 0.95,
) -> FusionResult:
    if not 0.5 <= minimum_clutter_confidence <= 1.0:
        raise SegmentationContractError("Clutter confidence threshold is invalid.")
    if not 0.0 <= minimum_review_coverage <= 1.0:
        raise SegmentationContractError("Review coverage threshold is invalid.")
    proposal_ids = {candidate.candidate_id for candidate in proposals.candidates}
    if set(reviews) - proposal_ids:
        raise SegmentationContractError("Semantic review contains an unknown candidate ID.")
    fused: list[FusedCandidate] = []
    reviewed_points = 0
    counts: dict[str, int] = {}
    for candidate in proposals.candidates:
        review = semantic_review_from_mapping(
            candidate.candidate_id, reviews.get(candidate.candidate_id)
        )
        if review.status is not ReviewStatus.UNREVIEWED:
            reviewed_points += candidate.point_count
        disposition, final_class, reason = _disposition(
            candidate,
            review,
            minimum_clutter_confidence=minimum_clutter_confidence,
        )
        counts[disposition.value] = counts.get(disposition.value, 0) + candidate.point_count
        fused.append(FusedCandidate(candidate, review, disposition, final_class, reason))
    total = proposals.proposed_point_count
    coverage = reviewed_points / total if total else 1.0
    return FusionResult(
        profile_version=FUSION_PROFILE_VERSION,
        candidates=tuple(fused),
        review_requested=bool(review_requested),
        reviewed_point_count=reviewed_points,
        proposed_point_count=total,
        review_coverage=coverage,
        minimum_review_coverage=minimum_review_coverage,
        analysis_incomplete=bool(review_requested and coverage < minimum_review_coverage),
        point_counts_by_disposition=counts,
    )


__all__ = [
    "FUSION_PROFILE_VERSION",
    "FusedCandidate",
    "FusedDisposition",
    "FusionResult",
    "ReviewStatus",
    "SemanticReview",
    "fuse_component_candidates",
    "semantic_review_from_mapping",
]
