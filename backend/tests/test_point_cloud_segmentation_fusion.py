from __future__ import annotations

import pytest
import numpy as np

from app.routers.services import point_cloud as point_cloud_service
from app.services.point_cloud.fusion import (
    FusedDisposition,
    fuse_component_candidates,
)
from app.services.point_cloud.segmentation import (
    CandidateRegion,
    DeterministicRegionEvidence,
    SEGMENTATION_ONTOLOGY_VERSION,
    SegmentationContractError,
    candidate_from_component,
    proposal_set,
)
from app.services.point_cloud.vlm_protocol import ComponentClass


def _candidate(
    identifier: int,
    proposed: ComponentClass,
    *,
    detached: bool = False,
    noise: bool = False,
    connected: bool = False,
    obstruction: bool = False,
    safety: bool = False,
    count: int = 100,
) -> CandidateRegion:
    return CandidateRegion(
        candidate_id=identifier,
        proposed_class=proposed,
        point_count=count,
        evidence=DeterministicRegionEvidence(
            detached_from_structure=detached,
            statistical_noise=noise,
            connected_to_structure=connected,
            within_bridge_footprint=True,
            measurement_obstruction=obstruction,
            safety_relevant=safety,
            reasons=("fixture",),
        ),
        proposal_source="fixture",
        metadata={},
    )


def _review(
    *,
    is_bridge: bool = False,
    component_class: str = "nonstructural_clutter",
    confidence: float = 0.99,
    abstained: bool = False,
) -> dict:
    return {
        "is_bridge": is_bridge,
        "component_class": component_class,
        "material": "unknown",
        "confidence": confidence,
        "evidence": "review evidence",
        "evidence_ids": [],
        "abstained": abstained,
    }


def test_component_projection_uses_one_versioned_ontology():
    proposals = proposal_set(
        [
            candidate_from_component(
                {"id": 1, "point_count": 500, "kind": "slab", "label": "deck"}
            ),
            candidate_from_component(
                {
                    "id": 2,
                    "point_count": 25,
                    "kind": "unknown",
                    "label": "attached utility pipe",
                }
            ),
        ]
    )

    assert proposals.ontology_version == SEGMENTATION_ONTOLOGY_VERSION
    assert proposals.candidates[0].proposed_class is ComponentClass.DECK_SLAB
    assert proposals.candidates[1].proposed_class is ComponentClass.ATTACHED_RESTRICTION
    assert all(candidate.protected for candidate in proposals.candidates)


def test_vlm_only_clutter_verdict_cannot_remove_unknown_region():
    proposals = proposal_set(
        [_candidate(1, ComponentClass.UNKNOWN_STRUCTURE, count=80)]
    )
    result = fuse_component_candidates(
        proposals, {1: _review()}, review_requested=True
    )

    assert result.removed_candidate_ids == ()
    assert result.candidates[0].disposition is FusedDisposition.KEEP_UNKNOWN


def test_detached_region_requires_high_confidence_typed_clutter_to_remove():
    candidate = _candidate(
        1, ComponentClass.UNKNOWN_STRUCTURE, detached=True, count=80
    )
    proposals = proposal_set([candidate])

    low = fuse_component_candidates(
        proposals, {1: _review(confidence=0.89)}, review_requested=True
    )
    high = fuse_component_candidates(
        proposals, {1: _review(confidence=0.9)}, review_requested=True
    )

    assert low.removed_candidate_ids == ()
    assert high.removed_candidate_ids == (1,)
    assert high.candidates[0].removal_reason


@pytest.mark.parametrize(
    "candidate",
    [
        _candidate(1, ComponentClass.DECK_SLAB, detached=True),
        _candidate(1, ComponentClass.ATTACHED_RESTRICTION, detached=True),
        _candidate(
            1,
            ComponentClass.UNKNOWN_STRUCTURE,
            detached=True,
            connected=True,
            safety=True,
        ),
        _candidate(
            1,
            ComponentClass.UNDER_SURFACE,
            noise=True,
            obstruction=True,
        ),
    ],
)
def test_structural_attached_safety_and_measurement_candidates_are_protected(candidate):
    result = fuse_component_candidates(
        proposal_set([candidate]), {1: _review()}, review_requested=True
    )
    assert result.removed_candidate_ids == ()
    assert result.candidates[0].disposition is not FusedDisposition.REMOVE_CLUTTER


def test_attached_and_detached_signs_have_different_safe_dispositions():
    attached = _candidate(
        1,
        ComponentClass.ATTACHED_RESTRICTION,
        connected=True,
        obstruction=True,
        safety=True,
    )
    detached = _candidate(
        2, ComponentClass.UNKNOWN_STRUCTURE, detached=True
    )
    result = fuse_component_candidates(
        proposal_set([attached, detached]),
        {1: _review(), 2: _review()},
        review_requested=True,
    )

    assert result.candidates[0].disposition is FusedDisposition.KEEP_RESTRICTION
    assert result.removed_candidate_ids == (2,)


def test_missing_or_abstained_review_keeps_unknown_and_marks_coverage_incomplete():
    proposals = proposal_set(
        [
            _candidate(1, ComponentClass.UNKNOWN_STRUCTURE, detached=True, count=90),
            _candidate(2, ComponentClass.UNKNOWN_STRUCTURE, detached=True, count=10),
        ]
    )
    result = fuse_component_candidates(
        proposals,
        {
            2: _review(
                is_bridge=False,
                component_class="insufficient_evidence",
                confidence=0.1,
                abstained=True,
            )
        },
        review_requested=True,
    )

    assert result.removed_candidate_ids == ()
    assert result.review_coverage == 0.1
    assert result.analysis_incomplete is True
    assert all(
        candidate.disposition is FusedDisposition.KEEP_UNKNOWN
        for candidate in result.candidates
    )


def test_unknown_review_ids_and_duplicate_proposals_fail_closed():
    proposals = proposal_set([_candidate(1, ComponentClass.UNKNOWN_STRUCTURE)])
    with pytest.raises(SegmentationContractError):
        fuse_component_candidates(
            proposals, {99: _review()}, review_requested=True
        )
    with pytest.raises(SegmentationContractError):
        proposal_set(
            [
                _candidate(1, ComponentClass.UNKNOWN_STRUCTURE),
                _candidate(1, ComponentClass.UNKNOWN_STRUCTURE),
            ]
        )

    invalid_boolean = _review()
    invalid_boolean["is_bridge"] = "false"
    with pytest.raises(SegmentationContractError):
        fuse_component_candidates(
            proposals, {1: invalid_boolean}, review_requested=True
        )


def test_point_reason_counts_reconcile_exactly():
    proposals = proposal_set(
        [
            _candidate(1, ComponentClass.UNKNOWN_STRUCTURE, detached=True, count=40),
            _candidate(2, ComponentClass.DECK_SLAB, count=60),
        ]
    )
    result = fuse_component_candidates(
        proposals, {1: _review(), 2: _review(is_bridge=True, component_class="deck_slab")},
        review_requested=True,
    )

    assert sum(result.point_counts_by_disposition.values()) == 100
    assert result.reviewed_point_count == 100
    assert result.analysis_incomplete is False


def test_vlm_boundary_gate_excludes_clutter_and_restrictions_from_measurement():
    points = np.column_stack(
        [np.arange(16, dtype=float), np.zeros(16), np.ones(16)]
    )
    component_ids = np.repeat(np.arange(1, 5), 4)
    components = [
        {"id": 1, "final_class": "deck_slab"},
        {"id": 2, "final_class": "under_surface"},
        {"id": 3, "final_class": "pier_column"},
        {"id": 4, "final_class": "attached_restriction_appurtenance"},
    ]
    reviews = {
        1: {"is_bridge": True, "component_class": "deck_slab", "confidence": 0.95, "abstained": False},
        2: {"is_bridge": True, "component_class": "under_surface", "confidence": 0.95, "abstained": False},
        3: {"is_bridge": False, "component_class": "nonstructural_clutter", "confidence": 0.98, "abstained": False},
        4: {"is_bridge": False, "component_class": "nonstructural_clutter", "confidence": 0.98, "abstained": False},
    }

    measurement_ids, boundary_points, metadata = point_cloud_service._measurement_fusion_gate(
        points, component_ids, components, reviews
    )

    assert measurement_ids == {1, 2}
    assert boundary_points is not None
    assert np.array_equal(boundary_points, points[component_ids == 2])
    assert metadata["boundary_source"] == "vlm_reviewed_component_extents"
    assert metadata["excluded_clutter_component_ids"] == [3, 4]
    assert metadata["excluded_clutter_points"] == 8
    assert metadata["excluded_restriction_component_ids"] == [4]
    assert metadata["excluded_restriction_points"] == 4
    assert metadata["distance_authority"] == "deterministic_geometry_only"

    unreviewed_ids, _, unreviewed_metadata = point_cloud_service._measurement_fusion_gate(
        points, component_ids, components, {}
    )
    assert unreviewed_ids == {1, 2, 3, 4}
    assert unreviewed_metadata["excluded_clutter_points"] == 0
    assert unreviewed_metadata["excluded_restriction_points"] == 0


def test_attached_restriction_geometry_protects_connected_object_not_detached_object():
    structure = np.column_stack(
        [np.zeros(40), np.linspace(0.0, 0.4, 40), np.full(40, 2.0)]
    )
    attached = np.column_stack(
        [np.linspace(0.08, 0.6, 40), np.full(40, 0.2), np.linspace(2.0, 2.4, 40)]
    )
    detached = np.column_stack(
        [np.linspace(5.0, 5.5, 40), np.full(40, 0.2), np.linspace(2.0, 2.4, 40)]
    )
    points = np.vstack([structure, attached, detached])
    labels = np.zeros(len(points), dtype=np.int8)
    labels[: len(structure)] = point_cloud_service._C_DECK
    deck = labels == point_cloud_service._C_DECK
    footprint = np.ones(len(points), dtype=bool)

    mask = point_cloud_service._attached_restriction_mask(
        points, labels, deck, footprint
    )

    assert mask[len(structure) : len(structure) + len(attached)].all()
    assert not mask[-len(detached) :].any()


def test_footprint_keeps_aligned_deck_halves_but_rejects_side_clutter():
    u_left, v_left = np.meshgrid(np.linspace(0.0, 10.0, 25), np.linspace(0.0, 2.0, 6))
    u_right, v_right = np.meshgrid(np.linspace(12.0, 22.0, 25), np.linspace(0.0, 2.0, 6))
    u_clutter, v_clutter = np.meshgrid(np.linspace(1.0, 1.5, 2), np.linspace(9.0, 9.5, 2))
    u = np.concatenate([u_left.ravel(), u_right.ravel(), u_clutter.ravel()])
    v = np.concatenate([v_left.ravel(), v_right.ravel(), v_clutter.ravel()])
    selected = np.ones(len(u), dtype=bool)

    footprint = point_cloud_service._footprint_mask(
        u, v, selected, ncell=60, dilation=1
    )
    inside = point_cloud_service._in_footprint(u, v, footprint)
    left_end = u_left.size
    right_end = left_end + u_right.size

    # Grid upper-bound points sit on the packed half-open boundary, so verify the
    # substantial coverage rather than requiring those exact edge samples.
    assert inside[:left_end].mean() > 0.85
    assert inside[left_end:right_end].mean() > 0.85
    assert not inside[right_end:].any()


def test_active_analysis_never_calls_legacy_pre_geometry_vlm_deleters(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy pre-geometry remover must not run")

    monkeypatch.setattr(point_cloud_service, "_qwen_octree_carve", forbidden)
    monkeypatch.setattr(point_cloud_service, "_vlm_clutter_triage", forbidden)
    monkeypatch.setattr(point_cloud_service, "_sam3_concept_mask", forbidden)
    x, y = np.meshgrid(np.linspace(-10, 10, 30), np.linspace(-4, 4, 12))
    deck = np.column_stack([x.ravel(), y.ravel(), np.full(x.size, 5.0)])
    ground = np.column_stack([x.ravel(), y.ravel(), np.full(x.size, 0.0)])
    points = np.vstack([deck, ground])

    result = point_cloud_service.analyze_bridge(
        points,
        scale_known=True,
        under_feature="highway",
        use_vlm=False,
        use_sam=True,
        model_mode="structural",
        bridge_context={
            "schema_version": "test",
            "knowledge_pack_sha256": "a" * 64,
            "trusted_structured_context": {
                "trust": "loaded_yearly_inventory",
                "scope": {
                    "state_code": 34,
                    "bridge_number": "0415152",
                    "data_year": 2025,
                },
            },
            "_measurement_features": [
                {
                    "BF01": "H02",
                    "BF02": "B",
                    "BF03": "NJ 73",
                    "BH13": 14.4,
                }
            ],
        },
    )

    assert result["cleaning"]["clean_mode"] == "conservative_fusion"
    assert result["cleaning"]["removed_noise"] == 0
    assert result["segmentation_ontology_version"] == SEGMENTATION_ONTOLOGY_VERSION
    assert result["snbi_bh13"] == [
        {
            "feature_id": "H02",
            "feature_name": "NJ 73",
            "relationship_code": "B",
            "relationship": "below_bridge",
            "bh13_ft": 14.4,
            "sentinel_99_9": False,
            "comparison_target": True,
        }
    ]
