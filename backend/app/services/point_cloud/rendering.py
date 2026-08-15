"""Deterministic, receipt-bearing renders for point-cloud VLM review."""

from __future__ import annotations

import hashlib
from io import BytesIO
import math
import re
from typing import Any, Mapping, Sequence

import numpy as np
from PIL import Image, ImageDraw

from app.services.point_cloud.vlm_protocol import VlmImage, VlmProtocolError


RENDER_PROFILE_VERSION = "bridgeguard-point-cloud-render-v1"
DEFAULT_WIDTH = 720
DEFAULT_HEIGHT = 560
MAX_RENDER_POINTS = 45_000

# Okabe-Ito plus high-contrast extensions. The renderer assigns these by
# sorted candidate ID, never by caller-provided colour, so IDs cannot collapse
# onto the same colour and replays are stable.
_PALETTE = (
    (0, 114, 178),
    (230, 159, 0),
    (0, 158, 115),
    (213, 94, 0),
    (204, 121, 167),
    (86, 180, 233),
    (240, 228, 66),
    (0, 0, 0),
    (148, 103, 189),
    (140, 86, 75),
    (227, 119, 194),
    (127, 127, 127),
)

_VIEWS = (
    ("oblique", 24.0, -55.0),
    ("side", 4.0, 0.0),
    ("top", 90.0, -90.0),
)


def _finite_points(points: Any) -> np.ndarray:
    try:
        array = np.asarray(points, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise VlmProtocolError("Render points are not a numeric matrix.") from exc
    if array.ndim != 2 or array.shape[1] < 3 or len(array) < 1:
        raise VlmProtocolError("Render points must have shape (N,3+).")
    xyz = np.ascontiguousarray(array[:, :3], dtype=np.float64)
    if not np.isfinite(xyz).all():
        raise VlmProtocolError("Render points contain non-finite values.")
    return xyz


def _labels(labels: Any, count: int) -> np.ndarray:
    try:
        array = np.asarray(labels, dtype=np.int64)
    except (TypeError, ValueError) as exc:
        raise VlmProtocolError("Render labels are invalid.") from exc
    if array.ndim != 1 or len(array) != count or np.any(array < 0):
        raise VlmProtocolError("Render labels do not align with the point cloud.")
    return array


def _camera(elevation: float, azimuth: float) -> tuple[np.ndarray, dict[str, Any]]:
    elev = math.radians(elevation)
    azim = math.radians(azimuth)
    view = np.asarray(
        [math.cos(elev) * math.cos(azim), math.cos(elev) * math.sin(azim), math.sin(elev)],
        dtype=np.float64,
    )
    right = np.asarray([-math.sin(azim), math.cos(azim), 0.0], dtype=np.float64)
    if abs(elevation) >= 89.999:
        right = np.asarray([1.0, 0.0, 0.0], dtype=np.float64)
    right /= np.linalg.norm(right)
    up = np.cross(view, right)
    up /= np.linalg.norm(up)
    matrix = np.stack([right, up, view])
    receipt = {
        "projection": "orthographic",
        "elevation_degrees": elevation,
        "azimuth_degrees": azimuth,
        "world_to_camera_rotation": [
            [round(float(value), 12) for value in row] for row in matrix
        ],
    }
    return matrix, receipt


def _even_sample(indices: np.ndarray, limit: int) -> np.ndarray:
    if len(indices) <= limit:
        return indices
    positions = np.linspace(0, len(indices) - 1, num=limit, dtype=np.int64)
    return indices[positions]


def _stratified_indices(
    labels: np.ndarray,
    candidate_ids: tuple[int, ...],
    *,
    max_points: int,
    context_mask: np.ndarray,
) -> np.ndarray:
    if max_points < max(500, len(candidate_ids) * 64):
        raise VlmProtocolError("Render point budget is too small for its candidates.")
    per_candidate = max(64, min(5_000, max_points // max(2, len(candidate_ids) + 1)))
    selected: list[np.ndarray] = []
    for identifier in candidate_ids:
        selected.append(_even_sample(np.flatnonzero(labels == identifier), per_candidate))
    used = sum(len(item) for item in selected)
    context_budget = max(0, max_points - used)
    context = _even_sample(np.flatnonzero(context_mask), context_budget)
    selected.append(context)
    combined = np.concatenate(selected) if selected else context
    if len(combined) > max_points:
        combined = _even_sample(combined, max_points)
    return np.sort(combined, kind="stable")


def _content_bounds(values: np.ndarray) -> tuple[float, float]:
    low, high = np.percentile(values, [0.2, 99.8])
    if not math.isfinite(float(low)) or not math.isfinite(float(high)):
        raise VlmProtocolError("Render projection bounds are invalid.")
    if high - low < 1e-9:
        low -= 0.5
        high += 0.5
    pad = (high - low) * 0.04
    return float(low - pad), float(high + pad)


def _encode_png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()


def _render_one(
    points: np.ndarray,
    labels: np.ndarray,
    candidate_ids: tuple[int, ...],
    *,
    view_name: str,
    elevation: float,
    azimuth: float,
    purpose: str,
    width: int,
    height: int,
    source_colors: np.ndarray | None,
) -> VlmImage:
    rotation, camera = _camera(elevation, azimuth)
    centered = points - np.median(points, axis=0)
    projected = centered @ rotation.T
    x_low, x_high = _content_bounds(projected[:, 0])
    y_low, y_high = _content_bounds(projected[:, 1])
    margin = 38
    x_pixels = np.clip(
        np.rint(
            margin
            + (projected[:, 0] - x_low)
            / (x_high - x_low)
            * (width - 2 * margin - 1)
        ),
        0,
        width - 1,
    ).astype(np.int32)
    y_pixels = np.clip(
        np.rint(
            height
            - margin
            - (projected[:, 1] - y_low)
            / (y_high - y_low)
            * (height - 2 * margin - 1)
        ),
        0,
        height - 1,
    ).astype(np.int32)

    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    order = np.argsort(projected[:, 2], kind="stable")
    palette = {
        identifier: _PALETTE[index % len(_PALETTE)]
        for index, identifier in enumerate(candidate_ids)
    }
    # Context first and in grey/source colour; candidate points are drawn after
    # it so small structural candidates remain visible.
    for index in order:
        identifier = int(labels[index])
        if identifier in palette:
            continue
        if source_colors is None:
            colour = (165, 165, 165)
        else:
            colour = tuple(int(value) for value in source_colors[index])
        draw.point((int(x_pixels[index]), int(y_pixels[index])), fill=colour)
    for index in order:
        identifier = int(labels[index])
        colour = palette.get(identifier)
        if colour is None:
            continue
        x, y = int(x_pixels[index]), int(y_pixels[index])
        draw.rectangle((x - 1, y - 1, x + 1, y + 1), fill=colour)

    for identifier in candidate_ids:
        mask = labels == identifier
        if not np.any(mask):
            continue
        x = int(np.median(x_pixels[mask]))
        y = int(np.median(y_pixels[mask]))
        text = str(identifier)
        box = draw.textbbox((x, y), text)
        draw.rectangle(
            (box[0] - 3, box[1] - 2, box[2] + 3, box[3] + 2),
            fill=(255, 255, 255),
            outline=palette[identifier],
            width=2,
        )
        draw.text((x, y), text, fill=(0, 0, 0))

    png = _encode_png(canvas)
    camera.update(
        {
            "render_profile": RENDER_PROFILE_VERSION,
            "point_count": int(len(points)),
            "candidate_ids": list(candidate_ids),
            "palette_rgb": {
                str(identifier): list(palette[identifier])
                for identifier in candidate_ids
            },
            "projection_bounds": {
                "x": [x_low, x_high],
                "y": [y_low, y_high],
            },
        }
    )
    return VlmImage(
        image_id=f"{purpose}-{view_name}",
        mime_type="image/png",
        data=png,
        sha256=hashlib.sha256(png).hexdigest(),
        purpose=purpose,
        view_name=view_name,
        width=width,
        height=height,
        camera_receipt=camera,
    )


def render_candidate_views(
    points: Any,
    labels: Any,
    candidate_ids: Sequence[int],
    *,
    purpose: str,
    crop_low: Sequence[float] | None = None,
    crop_high: Sequence[float] | None = None,
    source_colors: Any = None,
    intensity: Any = None,
    max_points: int = MAX_RENDER_POINTS,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
) -> tuple[VlmImage, ...]:
    """Render deterministic orthographic evidence with stratified candidates."""

    xyz = _finite_points(points)
    label_array = _labels(labels, len(xyz))
    ids = tuple(sorted({int(value) for value in candidate_ids}))
    if not ids or any(value < 0 for value in ids):
        raise VlmProtocolError("Render candidate IDs are invalid.")
    if any(not np.any(label_array == value) for value in ids):
        raise VlmProtocolError("Every render candidate ID must have points.")
    if len(ids) > len(_PALETTE):
        raise VlmProtocolError("Render contains more candidates than unique colours.")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,40}", purpose):
        raise VlmProtocolError("Render purpose is invalid.")
    if not (320 <= width <= 1600 and 240 <= height <= 1200):
        raise VlmProtocolError("Render dimensions are outside reviewed bounds.")
    max_points = min(MAX_RENDER_POINTS, int(max_points))

    context_mask = ~np.isin(label_array, ids)
    if crop_low is not None or crop_high is not None:
        if crop_low is None or crop_high is None:
            raise VlmProtocolError("Render crop requires both bounds.")
        low = np.asarray(crop_low, dtype=np.float64)
        high = np.asarray(crop_high, dtype=np.float64)
        if low.shape != (3,) or high.shape != (3,) or not np.all(low < high):
            raise VlmProtocolError("Render crop bounds are invalid.")
        context_mask &= np.all((xyz >= low) & (xyz <= high), axis=1)

    colors = None
    if source_colors is not None:
        raw_colors = np.asarray(source_colors)
        if raw_colors.ndim != 2 or raw_colors.shape[0] != len(xyz) or raw_colors.shape[1] < 3:
            raise VlmProtocolError("Source colours do not align with render points.")
        raw_colors = np.asarray(raw_colors[:, :3], dtype=np.float64)
        if not np.isfinite(raw_colors).all():
            raise VlmProtocolError("Source colours contain non-finite values.")
        if float(raw_colors.max(initial=0.0)) <= 1.0:
            raw_colors *= 255.0
        colors = np.clip(np.rint(raw_colors), 0, 255).astype(np.uint8)

    intensity_values = None
    if intensity is not None:
        raw_intensity = np.asarray(intensity, dtype=np.float64).reshape(-1)
        if len(raw_intensity) != len(xyz) or not np.isfinite(raw_intensity).all():
            raise VlmProtocolError("Source intensity does not align with render points.")
        low, high = np.percentile(raw_intensity, [1.0, 99.0])
        if float(high - low) > 1e-12:
            intensity_values = np.clip(
                np.rint((raw_intensity - low) / (high - low) * 255.0),
                0,
                255,
            ).astype(np.uint8)

    selected = _stratified_indices(
        label_array,
        ids,
        max_points=max_points,
        context_mask=context_mask,
    )
    selected_points = xyz[selected]
    selected_labels = label_array[selected]
    selected_colors = colors[selected] if colors is not None else None
    selected_intensity = (
        intensity_values[selected] if intensity_values is not None else None
    )
    rendered = tuple(
        _render_one(
            selected_points,
            selected_labels,
            ids,
            view_name=view_name,
            elevation=elevation,
            azimuth=azimuth,
            purpose=purpose,
            width=width,
            height=height,
            source_colors=selected_colors,
        )
        for view_name, elevation, azimuth in _VIEWS
    )
    if selected_colors is not None:
        source_view = _render_one(
            selected_points,
            np.zeros(len(selected_points), dtype=np.int64),
            (),
            view_name="source-rgb-oblique",
            elevation=_VIEWS[0][1],
            azimuth=_VIEWS[0][2],
            purpose=purpose,
            width=width,
            height=height,
            source_colors=selected_colors,
        )
        rendered = (*rendered, source_view)
    if selected_intensity is not None:
        grayscale = np.repeat(selected_intensity[:, None], 3, axis=1)
        intensity_view = _render_one(
            selected_points,
            np.zeros(len(selected_points), dtype=np.int64),
            (),
            view_name="source-intensity-oblique",
            elevation=_VIEWS[0][1],
            azimuth=_VIEWS[0][2],
            purpose=purpose,
            width=width,
            height=height,
            source_colors=grayscale,
        )
        rendered = (*rendered, intensity_view)
    return rendered


__all__ = [
    "DEFAULT_HEIGHT",
    "DEFAULT_WIDTH",
    "MAX_RENDER_POINTS",
    "RENDER_PROFILE_VERSION",
    "render_candidate_views",
]
