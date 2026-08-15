from __future__ import annotations

import numpy as np
import pytest

from app.services.point_cloud.rendering import (
    RENDER_PROFILE_VERSION,
    render_candidate_views,
)
from app.services.point_cloud.vlm_protocol import VlmProtocolError


def _scene():
    axis = np.linspace(-5.0, 5.0, 500)
    points = np.column_stack(
        [
            axis,
            np.sin(axis),
            np.where(axis < 0, 2.0, 5.0),
        ]
    )
    labels = np.where(axis < 0, 4, 9).astype(np.int64)
    return points, labels


def test_render_is_byte_deterministic_receipted_png_with_three_camera_views():
    points, labels = _scene()
    first = render_candidate_views(
        points, labels, (4, 9), purpose="component-review", max_points=500
    )
    second = render_candidate_views(
        points, labels, (4, 9), purpose="component-review", max_points=500
    )

    assert [image.sha256 for image in first] == [image.sha256 for image in second]
    assert [image.data for image in first] == [image.data for image in second]
    assert [image.view_name for image in first] == ["oblique", "side", "top"]
    assert all(image.mime_type == "image/png" for image in first)
    assert all(image.data.startswith(b"\x89PNG\r\n\x1a\n") for image in first)
    assert all(
        image.camera_receipt["render_profile"] == RENDER_PROFILE_VERSION
        for image in first
    )
    assert all(
        image.camera_receipt["projection"] == "orthographic" for image in first
    )


def test_render_palette_is_unique_and_small_component_is_never_sampled_away():
    rng = np.random.default_rng(42)
    context = rng.normal(size=(20_000, 3))
    tiny = np.asarray([[20.0, 0.0, 1.0], [20.1, 0.1, 1.1]])
    points = np.vstack([context, tiny])
    labels = np.concatenate(
        [np.ones(len(context), dtype=np.int64), np.full(len(tiny), 2, dtype=np.int64)]
    )
    images = render_candidate_views(
        points, labels, (1, 2), purpose="tiny-component", max_points=700
    )

    for image in images:
        palette = image.camera_receipt["palette_rgb"]
        assert palette["1"] != palette["2"]
        assert image.camera_receipt["candidate_ids"] == [1, 2]


def test_source_rgb_and_crop_are_bounded_and_change_receipt_hash():
    points, labels = _scene()
    colors = np.tile(np.asarray([[0.2, 0.4, 0.8]]), (len(points), 1))
    plain = render_candidate_views(
        points, labels, (4, 9), purpose="rgb-review", max_points=500
    )
    colored = render_candidate_views(
        points,
        labels,
        (4, 9),
        purpose="rgb-review",
        source_colors=colors,
        crop_low=(-6, -2, 0),
        crop_high=(6, 2, 6),
        max_points=500,
    )
    assert [item.sha256 for item in plain] != [item.sha256 for item in colored]
    assert colored[-1].view_name == "source-rgb-oblique"


def test_useful_intensity_adds_a_receipted_grayscale_view():
    points, labels = _scene()
    intensity = np.linspace(10.0, 500.0, len(points))
    images = render_candidate_views(
        points,
        labels,
        (4, 9),
        purpose="intensity-review",
        intensity=intensity,
        max_points=500,
    )

    assert images[-1].view_name == "source-intensity-oblique"
    assert images[-1].mime_type == "image/png"
    assert images[-1].camera_receipt["candidate_ids"] == []


@pytest.mark.parametrize(
    ("points", "labels", "ids"),
    [
        ([[0, 0, float("nan")]], [1], [1]),
        ([[0, 0, 0]], [1, 2], [1]),
        ([[0, 0, 0]], [1], [2]),
        ([[0, 0, 0]], [0], [-1]),
    ],
)
def test_render_rejects_nonfinite_misaligned_missing_or_context_ids(points, labels, ids):
    with pytest.raises(VlmProtocolError):
        render_candidate_views(points, labels, ids, purpose="invalid-render")
