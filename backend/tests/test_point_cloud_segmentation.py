from __future__ import annotations

import base64

import numpy as np
import pytest

from app.routers.services import point_cloud as point_cloud_service
from app.services.point_cloud.fusion import fuse_component_candidates
from app.services.point_cloud.segmentation import (
    SEGMENTATION_ONTOLOGY_VERSION,
    candidate_from_component,
    proposal_set,
)
from app.services.point_cloud.vlm_protocol import ComponentClass


@pytest.mark.parametrize(
    ("kind", "label", "expected"),
    [
        ("slab", "deck slab", ComponentClass.DECK_SLAB),
        ("beam", "steel girder", ComponentClass.GIRDER_BEAM),
        ("beam", "cross frame bracing", ComponentClass.DIAPHRAGM_BRACING),
        ("unknown", "bearing assembly", ComponentClass.BEARING),
        ("column", "pier column", ComponentClass.PIER_COLUMN),
        ("column", "abutment wingwall", ComponentClass.ABUTMENT_WINGWALL),
        ("edge", "parapet railing", ComponentClass.PARAPET_RAILING_BARRIER),
        ("surface", "road under bridge", ComponentClass.UNDER_SURFACE),
        ("unknown", "attached utility pipe", ComponentClass.ATTACHED_RESTRICTION),
        ("unknown", "unresolved region", ComponentClass.UNKNOWN_STRUCTURE),
    ],
)
def test_deterministic_component_mapping_uses_closed_ontology(kind, label, expected):
    candidate = candidate_from_component(
        {"id": 1, "point_count": 30, "kind": kind, "label": label}
    )
    assert candidate.proposed_class is expected


def test_fusion_semantics_are_invariant_to_candidate_order():
    first = candidate_from_component(
        {"id": 1, "point_count": 70, "kind": "slab", "label": "deck"}
    )
    second = candidate_from_component(
        {"id": 2, "point_count": 30, "kind": "unknown", "label": "unknown"},
        detached_fraction=1.0,
    )
    reviews = {
        1: {
            "is_bridge": True,
            "component_class": "deck_slab",
            "material": "concrete",
            "confidence": 1.0,
            "evidence": "deck",
            "evidence_ids": [],
            "abstained": False,
        },
        2: {
            "is_bridge": False,
            "component_class": "nonstructural_clutter",
            "material": "unknown",
            "confidence": 0.99,
            "evidence": "detached vegetation",
            "evidence_ids": [],
            "abstained": False,
        },
    }
    forward = fuse_component_candidates(
        proposal_set([first, second]), reviews, review_requested=True
    )
    reverse = fuse_component_candidates(
        proposal_set([second, first]), reviews, review_requested=True
    )

    assert set(forward.removed_candidate_ids) == set(reverse.removed_candidate_ids) == {2}
    assert dict(forward.point_counts_by_disposition) == dict(reverse.point_counts_by_disposition)
    assert forward.review_coverage == reverse.review_coverage == 1.0


def _restriction_fixture():
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
    labels[:40] = point_cloud_service._C_DECK
    return points, labels


def test_attached_restriction_proposal_is_translation_and_order_invariant():
    points, labels = _restriction_fixture()
    footprint = np.ones(len(points), dtype=bool)
    original = point_cloud_service._attached_restriction_mask(
        points, labels, labels == point_cloud_service._C_DECK, footprint
    )
    permutation = np.arange(len(points))[::-1]
    translated = points[permutation] + np.asarray([1000.0, -2000.0, 50.0])
    moved = point_cloud_service._attached_restriction_mask(
        translated,
        labels[permutation],
        labels[permutation] == point_cloud_service._C_DECK,
        footprint,
    )
    restored = np.zeros_like(moved)
    restored[permutation] = moved

    assert np.array_equal(original, restored)


def test_attached_restriction_proposal_survives_density_duplication():
    points, labels = _restriction_fixture()
    baseline = point_cloud_service._attached_restriction_mask(
        points,
        labels,
        labels == point_cloud_service._C_DECK,
        np.ones(len(points), dtype=bool),
    )
    dense_points = np.repeat(points, 2, axis=0)
    dense_labels = np.repeat(labels, 2)
    dense = point_cloud_service._attached_restriction_mask(
        dense_points,
        dense_labels,
        dense_labels == point_cloud_service._C_DECK,
        np.ones(len(dense_points), dtype=bool),
    )

    assert np.array_equal(baseline, dense.reshape(-1, 2).all(axis=1))


def test_compatibility_segmenter_exposes_shared_fusion_schema_without_deleting_noise():
    rng = np.random.default_rng(8)
    points = rng.normal(size=(400, 3))
    result = point_cloud_service.segment_structure(
        points, use_qwen=False, use_sam=False, clean=True
    )
    labels = np.frombuffer(base64.b64decode(result["labels_b64u8"]), dtype=np.uint8)

    assert len(labels) == len(points)
    assert result["segmentation_ontology_version"] == SEGMENTATION_ONTOLOGY_VERSION
    assert result["noise_removed"] == 0
    assert result["noise_proposed"] == int((labels == point_cloud_service.SEG_NOISE).sum())
    assert result["segmentation_fusion"]["profile_version"].endswith("fusion-v1")
    assert all(
        item.get("segmentation_ontology_version") == SEGMENTATION_ONTOLOGY_VERSION
        for item in result["classes"]
        if item.get("count")
    )
