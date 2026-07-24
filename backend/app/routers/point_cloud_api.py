"""Hosted point-cloud API.

The geometry implementation lives in ``services.point_cloud``.  This router owns
HTTP transport and persistence so the hosted application can expose cleaning and
segmentation without importing desktop-only reconstruction or local GPU models.
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import re
import shutil
import sqlite3
import struct
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response

from .services.datapaths import ATTACHMENTS_DIR, DATA_DIR, TEMP_DIR, USER_DB
from .services.point_cloud import (
    PointCloudError,
    analyze_bridge,
    clean_and_smooth_preview,
    compute_clearance,
    read_e57_preview,
    segment_structure,
    world_points_from_e57,
)

router = APIRouter()

POINT_CLOUD_EXTS = {".las", ".ply", ".e57", ".xyz", ".pcd", ".csv", ".txt", ".npz"}
MAX_PC_BYTES = int(os.getenv("BRIDGEGUARD_MAX_POINT_CLOUD_MB", "500")) * 1024 * 1024
MAX_ANALYSIS_BODY_BYTES = int(os.getenv("BRIDGEGUARD_MAX_POINT_BODY_MB", "128")) * 1024 * 1024
_SAFE_BN_RE = re.compile(r"[^A-Za-z0-9_-]")


def _safe_bridge_id(state_code: str, bridge_number: str) -> str:
    sc = re.sub(r"[^0-9]", "", str(state_code))
    bn = _SAFE_BN_RE.sub("_", str(bridge_number))
    if not sc or not bn:
        raise ValueError("Invalid state_code or bridge_number")
    return f"{sc}_{bn}"


def _safe_upload_name(filename: str | None, fallback: str = "cloud.ply") -> str:
    basename = Path(filename or fallback).name
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", basename).strip("._")
    return safe[:180] or fallback


def _relative_data_path(filepath: str | Path) -> str:
    try:
        return Path(filepath).resolve().relative_to(Path(DATA_DIR).resolve()).as_posix()
    except ValueError:
        return str(filepath)


def _resolve_data_path(filepath: str | Path) -> Path:
    path = Path(str(filepath))
    return path if path.is_absolute() else Path(DATA_DIR) / path


def _processed_paths(state_code: str, bridge_number: str):
    root = Path(ATTACHMENTS_DIR) / "point_clouds" / _safe_bridge_id(state_code, bridge_number) / "processed"
    return root, root / "processed_cloud.npz", root / "processed_cloud.ply", root / "analysis.json"


def _remove_processed(state_code: str, bridge_number: str) -> None:
    try:
        root, _npz, _ply, _analysis = _processed_paths(state_code, bridge_number)
        if root.is_dir():
            shutil.rmtree(root)
    except (OSError, ValueError):
        pass


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, np.ndarray):
        return _json_safe(value.tolist())
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _write_binary_ply(path: Path, points: np.ndarray, colors: Optional[np.ndarray]) -> None:
    xyz = np.asarray(points, dtype=np.float32)[:, :3]
    rgb = None
    if colors is not None:
        rgb = np.clip(np.asarray(colors, dtype=np.float64)[:, :3] * 255.0, 0, 255).astype(np.uint8)
    fields = [("x", "<f4"), ("y", "<f4"), ("z", "<f4")]
    if rgb is not None:
        fields += [("red", "u1"), ("green", "u1"), ("blue", "u1")]
    rows = np.empty(len(xyz), dtype=np.dtype(fields))
    rows["x"], rows["y"], rows["z"] = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    if rgb is not None:
        rows["red"], rows["green"], rows["blue"] = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    header = [
        "ply",
        "format binary_little_endian 1.0",
        f"element vertex {len(rows)}",
        "property float x",
        "property float y",
        "property float z",
    ]
    if rgb is not None:
        header += ["property uchar red", "property uchar green", "property uchar blue"]
    header += ["end_header", ""]
    temp_path = path.with_suffix(".tmp")
    with temp_path.open("wb") as handle:
        handle.write("\n".join(header).encode("ascii"))
        rows.tofile(handle)
    temp_path.replace(path)


def _save_processed(
    state_code: str,
    bridge_number: str,
    world_points: np.ndarray,
    result: Dict[str, Any],
) -> Dict[str, Any]:
    root, npz_path, ply_path, analysis_path = _processed_paths(state_code, bridge_number)
    root.mkdir(parents=True, exist_ok=True)
    encoded = str(result.get("labels_b64u8") or "")
    labels = np.frombuffer(base64.b64decode(encoded), dtype=np.uint8) if encoded else np.empty(0, np.uint8)
    points = np.asarray(world_points, dtype=np.float32)[:, :3]
    if labels.size != points.shape[0]:
        raise ValueError("Analysis labels are not aligned with the source point cloud.")
    kept = labels > 0
    if not np.any(kept):
        raise ValueError("Analysis did not retain any bridge points.")
    cleaned_points = points[kept]
    cleaned_labels = labels[kept]
    palette = {
        int(item.get("id")): item.get("color_rgb", [0.6, 0.6, 0.6])
        for item in result.get("classes", [])
    }
    cleaned_colors = np.asarray(
        [palette.get(int(label), [0.6, 0.6, 0.6]) for label in cleaned_labels],
        dtype=np.float32,
    )

    npz_tmp = npz_path.with_suffix(".tmp.npz")
    np.savez_compressed(
        npz_tmp,
        points=cleaned_points,
        colors=cleaned_colors,
        labels=cleaned_labels,
    )
    npz_tmp.replace(npz_path)
    _write_binary_ply(ply_path, cleaned_points, cleaned_colors)

    saved = _json_safe({k: v for k, v in result.items() if k != "persistence"})
    saved["labels_b64u8"] = base64.b64encode(cleaned_labels.tobytes()).decode("ascii")
    saved["persistence"] = {
        "saved": True,
        "analysis_version": 1,
        "source_point_count": int(points.shape[0]),
        "processed_point_count": int(cleaned_points.shape[0]),
        "point_cloud_file": _relative_data_path(ply_path),
    }
    meta_tmp = analysis_path.with_suffix(".tmp")
    meta_tmp.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    meta_tmp.replace(analysis_path)
    return saved["persistence"]


def _load_processed(state_code: str, bridge_number: str) -> Optional[Dict[str, Any]]:
    try:
        _root, npz_path, ply_path, analysis_path = _processed_paths(state_code, bridge_number)
        if not npz_path.is_file() or not analysis_path.is_file():
            return None
        result = json.loads(analysis_path.read_text(encoding="utf-8"))
        result.setdefault("persistence", {})
        result["persistence"].update(
            {
                "saved": True,
                "point_cloud_file": _relative_data_path(ply_path) if ply_path.is_file() else None,
            }
        )
        return result
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _processed_preview(state_code: str, bridge_number: str, as_arrays: bool = False) -> Optional[Dict]:
    try:
        _root, npz_path, _ply, _analysis = _processed_paths(state_code, bridge_number)
        if not npz_path.is_file():
            return None
        with np.load(npz_path, allow_pickle=False) as saved:
            world = np.asarray(saved["points"], dtype=np.float32)[:, :3]
            colors = np.asarray(saved["colors"], dtype=np.float32)[:, :3] if "colors" in saved.files else None
        if world.size == 0:
            return None
        center = world.mean(axis=0)
        mins, maxs = world.min(axis=0), world.max(axis=0)
        return {
            "status": "success",
            "source": "saved_processed_analysis",
            "original_point_count": int(world.shape[0]),
            "rendered_point_count": int(world.shape[0]),
            "downsampled": False,
            "bounds": {"min": mins.tolist(), "max": maxs.tolist(), "center": center.tolist()},
            "points": (world - center).astype(np.float32) if as_arrays else (world - center).round(5).tolist(),
            "colors": colors if as_arrays else (colors.round(4).tolist() if colors is not None else None),
        }
    except (OSError, ValueError, KeyError):
        return None


def _frame_cloud_response(payload: Dict[str, Any]) -> Response:
    values = dict(payload)
    points = np.ascontiguousarray(np.asarray(values.pop("points"), dtype="<f4"))
    colors = values.pop("colors", None)
    header = json.dumps(
        _json_safe({**values, "n": int(points.shape[0]), "has_colors": colors is not None})
    ).encode("utf-8")
    header += b" " * (-(4 + len(header)) % 4)
    body = struct.pack("<I", len(header)) + header + points.tobytes()
    if colors is not None:
        body += np.clip(np.asarray(colors, dtype=np.float64) * 255.0, 0, 255).astype(np.uint8).tobytes()
    return Response(content=body, media_type="application/octet-stream")


async def _stream_upload(file: UploadFile, destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_suffix(destination.suffix + ".uploading")
    size = 0
    try:
        with temp_path.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_PC_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Point-cloud file exceeds the {MAX_PC_BYTES // (1024 * 1024)} MB upload limit.",
                    )
                handle.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="Uploaded point-cloud file is empty.")
        temp_path.replace(destination)
        return size
    finally:
        temp_path.unlink(missing_ok=True)


async def _read_body_limited(request: Request) -> bytes:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_ANALYSIS_BODY_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Point payload exceeds the {MAX_ANALYSIS_BODY_BYTES // (1024 * 1024)} MB limit.",
            )
    return bytes(raw)


def _param_bool(params: Dict[str, Any], key: str, default: bool) -> bool:
    value = params.get(key)
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _param_origin(params: Dict[str, Any]) -> np.ndarray:
    value = params.get("origin")
    if not value:
        return np.zeros(3, dtype=np.float64)
    if isinstance(value, str):
        try:
            value = [float(part) for part in value.split(",")]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="`origin` must be 'x,y,z'.") from exc
    origin = np.asarray(value, dtype=np.float64)
    if origin.size < 3 or not np.isfinite(origin[:3]).all():
        raise HTTPException(status_code=400, detail="`origin` must contain three finite values.")
    return origin[:3]


async def _cloud_payload(request: Request):
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    raw = await _read_body_limited(request)
    if content_type == "application/octet-stream":
        if len(raw) % 4:
            raise HTTPException(status_code=400, detail="Binary body must be float32-aligned.")
        values = np.frombuffer(raw, dtype="<f4")
        params = dict(request.query_params)
        colors = None
        if _param_bool(params, "has_colors", False):
            if values.size % 6:
                raise HTTPException(status_code=400, detail="xyz+rgb body must hold N*6 float32 values.")
            n_points = values.size // 6
            points = values[: n_points * 3].astype(np.float64).reshape(-1, 3)
            colors = values[n_points * 3 :].astype(np.float64).reshape(-1, 3)
        else:
            if values.size % 3:
                raise HTTPException(status_code=400, detail="xyz body must hold N*3 float32 values.")
            points = values.astype(np.float64).reshape(-1, 3)
        return (points if points.size else None), colors, params

    try:
        body = json.loads(raw or b"{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON point payload.") from exc
    points_value = body.get("points")
    points = np.asarray(points_value, dtype=np.float64) if points_value else None
    if points is not None:
        if points.ndim != 2 or points.shape[1] < 3:
            raise HTTPException(status_code=400, detail="`points` must be an (N,3) array.")
        points = points[:, :3]
    colors_value = body.get("colors")
    colors = np.asarray(colors_value, dtype=np.float64)[:, :3] if colors_value else None
    return points, colors, body


def _require_points(points: Optional[np.ndarray], params: Dict[str, Any], to_world: bool) -> np.ndarray:
    if points is None:
        raise HTTPException(
            status_code=400,
            detail="Request must include points (binary body or JSON `points`).",
        )
    return points + _param_origin(params) if to_world else points


def _raw_cloud_row(state_code: str, bridge_number: str):
    with sqlite3.connect(USER_DB) as conn:
        return conn.execute(
            """
            SELECT filepath, filename, size_bytes, uploaded_at
            FROM bridge_attachments
            WHERE state_code=? AND bridge_number=? AND attachment_type='point_cloud'
            ORDER BY id DESC LIMIT 1
            """,
            (state_code, bridge_number),
        ).fetchone()


async def _analysis_points(
    state_code: str,
    bridge_number: str,
    points: Optional[np.ndarray],
    params: Dict[str, Any],
    to_world: bool,
) -> np.ndarray:
    if points is not None:
        return points + _param_origin(params) if to_world else points
    row = _raw_cloud_row(state_code, bridge_number)
    filepath = _resolve_data_path(row[0]) if row else None
    if row is None or filepath is None or not filepath.is_file():
        raise HTTPException(status_code=400, detail="No points supplied and no saved cloud exists.")
    if not str(row[1] or filepath).lower().endswith(".e57"):
        raise HTTPException(status_code=415, detail="Send points in the request for non-E57 clouds.")
    loaded = await asyncio.to_thread(world_points_from_e57, filepath, 0)
    return loaded["points"]


def _under_feature_kind(state_code: str, bridge_number: str) -> Optional[str]:
    try:
        state = int(state_code)
    except (TypeError, ValueError):
        return None
    try:
        with sqlite3.connect(f"file:{USER_DB}?mode=ro", uri=True, timeout=5) as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT DISTINCT feature_type FROM features
                    WHERE state_code=? AND bridge_number=? AND feature_location='B'
                    """,
                    (state, bridge_number),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = conn.execute(
                    "SELECT DISTINCT feature_type FROM features WHERE state_code=? AND bridge_number=?",
                    (state, bridge_number),
                ).fetchall()
    except sqlite3.Error:
        return None
    kinds = {str(row[0] or "").strip().upper()[:1] for row in rows}
    if {"W", "F"} & kinds:
        return "waterway"
    if "H" in kinds:
        return "highway"
    return None


def _db_clearance_ft(state_code: str, bridge_number: str) -> Optional[float]:
    try:
        state = int(state_code)
        with sqlite3.connect(f"file:{USER_DB}?mode=ro", uri=True, timeout=5) as conn:
            row = conn.execute(
                """
                SELECT highway_min_vertical_clearance FROM bridges
                WHERE state_code=? AND bridge_number=?
                  AND highway_min_vertical_clearance IS NOT NULL
                ORDER BY data_year DESC LIMIT 1
                """,
                (state, bridge_number),
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else None
    except (sqlite3.Error, TypeError, ValueError):
        return None


def _bridge_context(state_code: str, bridge_number: str) -> Dict[str, Any]:
    context: Dict[str, Any] = {
        "bridge_id": {"state_code": state_code, "bridge_number": bridge_number},
        "sources": [],
    }
    try:
        state = int(state_code)
        with sqlite3.connect(f"file:{USER_DB}?mode=ro", uri=True, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT bridge_location, total_bridge_length, maximum_span_length,
                       bridge_width_out_to_out, skew, year_built, data_year
                FROM bridges WHERE state_code=? AND bridge_number=?
                ORDER BY data_year DESC LIMIT 1
                """,
                (state, bridge_number),
            ).fetchone()
            if row:
                context["inventory"] = {key: row[key] for key in row.keys() if row[key] is not None}
                context["sources"].append("bridge database")

            element_rows = conn.execute(
                """
                SELECT element_number, element_parent_number,
                       element_total_quantity,
                       element_quantity_condition_state_one,
                       element_quantity_condition_state_two,
                       element_quantity_condition_state_three,
                       element_quantity_condition_state_four,
                       data_year
                FROM elements
                WHERE state_code=? AND bridge_number=?
                  AND data_year=(
                      SELECT MAX(data_year) FROM elements
                      WHERE state_code=? AND bridge_number=?
                  )
                ORDER BY CAST(element_number AS INTEGER), element_number
                LIMIT 60
                """,
                (state, bridge_number, state, bridge_number),
            ).fetchall()
            if element_rows:
                context["snbi_elements"] = [
                    {key: item[key] for key in item.keys() if item[key] is not None}
                    for item in element_rows
                ]
                context["sources"].append("SNBI elements")

            span_rows = conn.execute(
                """
                SELECT span_configuration_designation, number_of_spans,
                       number_of_beam_lines, span_material, span_continuity,
                       span_type, deck_interaction, deck_material_and_type,
                       wearing_surface, data_year
                FROM span_sets
                WHERE state_code=? AND bridge_number=?
                  AND data_year=(
                      SELECT MAX(data_year) FROM span_sets
                      WHERE state_code=? AND bridge_number=?
                  )
                ORDER BY span_configuration_designation
                LIMIT 30
                """,
                (state, bridge_number, state, bridge_number),
            ).fetchall()
            if span_rows:
                context["snbi_span_sets"] = [
                    {key: item[key] for key in item.keys() if item[key] is not None}
                    for item in span_rows
                ]
                context["sources"].append("SNBI span sets")

            substructure_rows = conn.execute(
                """
                SELECT substructure_configuration_designation,
                       number_of_substructure_units, substructure_material,
                       substructure_type, foundation_type, data_year
                FROM substructure_sets
                WHERE state_code=? AND bridge_number=?
                  AND data_year=(
                      SELECT MAX(data_year) FROM substructure_sets
                      WHERE state_code=? AND bridge_number=?
                  )
                ORDER BY substructure_configuration_designation
                LIMIT 30
                """,
                (state, bridge_number, state, bridge_number),
            ).fetchall()
            if substructure_rows:
                context["snbi_substructure_sets"] = [
                    {key: item[key] for key in item.keys() if item[key] is not None}
                    for item in substructure_rows
                ]
                context["sources"].append("SNBI substructure sets")

            feature_rows = conn.execute(
                """
                SELECT feature_type, feature_location, feature_name,
                       highway_max_usable_vertical_clearance,
                       highway_min_vertical_clearance, data_year
                FROM features
                WHERE state_code=? AND bridge_number=?
                  AND data_year=(
                      SELECT MAX(data_year) FROM features
                      WHERE state_code=? AND bridge_number=?
                  )
                ORDER BY feature_location, feature_type
                LIMIT 20
                """,
                (state, bridge_number, state, bridge_number),
            ).fetchall()
            if feature_rows:
                context["snbi_features"] = [
                    {key: item[key] for key in item.keys() if item[key] is not None}
                    for item in feature_rows
                ]
                context["sources"].append("SNBI features")
    except (sqlite3.Error, TypeError, ValueError):
        pass

    report_cache = (
        Path(DATA_DIR) / "rag_storage" / "bridge_reports" / f"{state_code}_{bridge_number}.json"
    )
    try:
        cache = json.loads(report_cache.read_text(encoding="utf-8"))
        keywords = (
            "span", "girder", "beam", "bearing", "abutment", "pier", "column",
            "deck", "parapet", "wingwall", "diaphragm", "structure type",
        )
        scored = []
        for chunk in cache.get("chunks") or []:
            text = str(chunk.get("text") or "")
            score = sum(text.lower().count(keyword) for keyword in keywords)
            if score:
                scored.append((score, int(chunk.get("page") or 1), text))
        scored.sort(key=lambda item: (-item[0], item[1]))
        if scored:
            context["report_component_evidence"] = [
                {"page": page, "text": text[:1000]}
                for _score, page, text in scored[:6]
            ]
            context["sources"].append("indexed inspection report")
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    # Keep high-signal report and structural-set evidence ahead of long element
    # inventories when the hosted prompt applies its size cap.
    ordered: Dict[str, Any] = {}
    for key in (
        "bridge_id",
        "sources",
        "inventory",
        "report_component_evidence",
        "snbi_span_sets",
        "snbi_substructure_sets",
        "snbi_features",
        "snbi_elements",
    ):
        if key in context:
            ordered[key] = context[key]
    return ordered


def _npz_preview(raw: bytes, max_points: int = 0, as_arrays: bool = False) -> Dict[str, Any]:
    import io

    try:
        with np.load(io.BytesIO(raw), allow_pickle=False) as saved:
            if "points" not in saved.files:
                raise PointCloudError("NPZ requires a numeric 'points' array.")
            world = np.asarray(saved["points"], dtype=np.float32)
            colors = np.asarray(saved["colors"], dtype=np.float32) if "colors" in saved.files else None
    except (OSError, ValueError, KeyError) as exc:
        raise PointCloudError(f"Could not read NPZ point cloud: {exc}") from exc
    if world.ndim != 2 or world.shape[1] < 3:
        raise PointCloudError("NPZ 'points' must have shape (N,3) or (N,3+).")
    world = world[:, :3]
    if colors is not None:
        colors = colors[:, :3] if colors.ndim == 2 and colors.shape[0] == len(world) and colors.shape[1] >= 3 else None
    valid = np.isfinite(world).all(axis=1)
    world = world[valid]
    if colors is not None:
        colors = colors[valid]
        if colors.size and float(np.nanmax(colors)) > 1.0:
            colors = colors / 255.0
        colors = np.clip(colors, 0.0, 1.0)
    if not len(world):
        raise PointCloudError("NPZ contains no finite XYZ points.")
    original_count = int(len(world))
    limit = max(0, min(int(max_points or 0), 2_000_000))
    if limit and len(world) > limit:
        indices = np.linspace(0, len(world) - 1, num=limit, dtype=np.int64)
        world = world[indices]
        if colors is not None:
            colors = colors[indices]
    center = world.mean(axis=0)
    mins, maxs = world.min(axis=0), world.max(axis=0)
    return {
        "status": "success",
        "source": "npz",
        "original_point_count": original_count,
        "rendered_point_count": int(len(world)),
        "downsampled": original_count > len(world),
        "bounds": {"min": mins.tolist(), "max": maxs.tolist(), "center": center.tolist()},
        "points": (world - center).astype(np.float32) if as_arrays else (world - center).round(5).tolist(),
        "colors": colors.astype(np.float32) if as_arrays and colors is not None else (
            colors.round(4).tolist() if colors is not None else None
        ),
    }


@router.post("/api/bridges/{state_code}/{bridge_number}/point-cloud")
async def upload_point_cloud(
    state_code: str,
    bridge_number: str,
    file: UploadFile = File(...),
):
    try:
        bridge_id = _safe_bridge_id(state_code, bridge_number)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ext = Path(file.filename or "").suffix.lower()
    if ext not in POINT_CLOUD_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported type '{ext}'. Allowed: {', '.join(sorted(POINT_CLOUD_EXTS))}",
        )
    cloud_dir = Path(ATTACHMENTS_DIR) / "point_clouds" / bridge_id
    destination = cloud_dir / _safe_upload_name(file.filename, f"cloud{ext}")
    size = await _stream_upload(file, destination)
    old_paths: list[Path] = []
    with sqlite3.connect(USER_DB) as conn:
        old_paths = [
            _resolve_data_path(row[0])
            for row in conn.execute(
                """
                SELECT filepath FROM bridge_attachments
                WHERE state_code=? AND bridge_number=? AND attachment_type='point_cloud'
                """,
                (state_code, bridge_number),
            ).fetchall()
        ]
        conn.execute(
            """
            DELETE FROM bridge_attachments
            WHERE state_code=? AND bridge_number=? AND attachment_type='point_cloud'
            """,
            (state_code, bridge_number),
        )
        conn.execute(
            """
            INSERT INTO bridge_attachments
                (state_code, bridge_number, attachment_type, filename, filepath, size_bytes)
            VALUES (?, ?, 'point_cloud', ?, ?, ?)
            """,
            (state_code, bridge_number, file.filename or destination.name, _relative_data_path(destination), size),
        )
    for old_path in old_paths:
        if old_path != destination:
            old_path.unlink(missing_ok=True)
    _remove_processed(state_code, bridge_number)
    return {"status": "success", "filename": file.filename, "size_bytes": size}


@router.get("/api/bridges/{state_code}/{bridge_number}/point-cloud/info")
def point_cloud_info(state_code: str, bridge_number: str):
    row = _raw_cloud_row(state_code, bridge_number)
    processed = _load_processed(state_code, bridge_number)
    if not row and not processed:
        return {"exists": False, "raw_exists": False, "processed_exists": False}
    return {
        "exists": True,
        "raw_exists": bool(row),
        "processed_exists": bool(processed),
        "filename": row[1] if row else "processed_cloud.ply",
        "size_bytes": row[2] if row else 0,
        "uploaded_at": row[3] if row else None,
        "processed_point_count": (
            processed.get("persistence", {}).get("processed_point_count") if processed else None
        ),
    }


@router.get("/api/bridges/{state_code}/{bridge_number}/point-cloud")
def download_point_cloud(state_code: str, bridge_number: str):
    row = _raw_cloud_row(state_code, bridge_number)
    filepath = _resolve_data_path(row[0]) if row else None
    if row is None or filepath is None or not filepath.is_file():
        raise HTTPException(status_code=404, detail="No point cloud saved for this bridge.")
    return FileResponse(filepath, filename=row[1], media_type="application/octet-stream")


@router.get("/api/bridges/{state_code}/{bridge_number}/point-cloud/preview")
def preview_bridge_point_cloud(
    state_code: str,
    bridge_number: str,
    scan_index: int = 0,
    max_points: int = 0,
    binary: int = 0,
):
    row = _raw_cloud_row(state_code, bridge_number)
    filepath = _resolve_data_path(row[0]) if row else None
    if row is None or filepath is None or not filepath.is_file():
        raise HTTPException(status_code=404, detail="No point cloud saved for this bridge.")
    if not str(row[1] or filepath).lower().endswith(".e57"):
        raise HTTPException(status_code=415, detail="Backend preview supports saved E57 files only.")
    try:
        payload = read_e57_preview(
            filepath,
            scan_index=scan_index,
            max_points=max_points,
            as_arrays=bool(binary),
        )
        return _frame_cloud_response(payload) if binary else _json_safe(payload)
    except PointCloudError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/bridges/{state_code}/{bridge_number}/point-cloud/processed")
def processed_analysis(state_code: str, bridge_number: str):
    result = _load_processed(state_code, bridge_number)
    if result is None:
        raise HTTPException(status_code=404, detail="No processed point-cloud result exists.")
    return result


@router.get("/api/bridges/{state_code}/{bridge_number}/point-cloud/processed/preview")
def processed_preview(state_code: str, bridge_number: str, binary: int = 0):
    payload = _processed_preview(state_code, bridge_number, as_arrays=bool(binary))
    if payload is None:
        raise HTTPException(status_code=404, detail="No processed point cloud exists.")
    return _frame_cloud_response(payload) if binary else _json_safe(payload)


@router.get("/api/bridges/{state_code}/{bridge_number}/point-cloud/processed/download")
def download_processed(state_code: str, bridge_number: str, format: str = "ply"):
    _root, npz_path, ply_path, _analysis = _processed_paths(state_code, bridge_number)
    if format not in {"ply", "npz"}:
        raise HTTPException(status_code=400, detail="format must be 'ply' or 'npz'.")
    path = ply_path if format == "ply" else npz_path
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No processed point cloud exists.")
    return FileResponse(
        path,
        filename=f"{_safe_bridge_id(state_code, bridge_number)}_processed.{format}",
        media_type="application/octet-stream",
    )


@router.delete("/api/bridges/{state_code}/{bridge_number}/point-cloud")
def delete_point_cloud(state_code: str, bridge_number: str):
    row = _raw_cloud_row(state_code, bridge_number)
    filepath = _resolve_data_path(row[0]) if row else None
    with sqlite3.connect(USER_DB) as conn:
        conn.execute(
            """
            DELETE FROM bridge_attachments
            WHERE state_code=? AND bridge_number=? AND attachment_type='point_cloud'
            """,
            (state_code, bridge_number),
        )
    if filepath is not None:
        filepath.unlink(missing_ok=True)
    _remove_processed(state_code, bridge_number)
    return {"status": "success"}


@router.get("/api/point-cloud/health")
def point_cloud_health():
    try:
        import pye57  # type: ignore

        return {
            "status": "ok",
            "e57_supported": True,
            "pye57_version": getattr(pye57, "__version__", None),
        }
    except Exception as exc:
        return {"status": "degraded", "e57_supported": False, "detail": str(exc)}


@router.post("/api/point-cloud/preview")
async def preview_e57(
    file: UploadFile = File(...),
    scan_index: int = Form(0),
    max_points: int = Form(0),
    binary: int = 0,
):
    filename = _safe_upload_name(file.filename, "cloud.e57")
    if not filename.lower().endswith(".e57"):
        raise HTTPException(status_code=400, detail="Please upload an .e57 point-cloud file.")
    temp_path = Path(TEMP_DIR) / f"preview-{os.getpid()}-{id(file)}-{filename}"
    try:
        await _stream_upload(file, temp_path)
        payload = await asyncio.to_thread(
            read_e57_preview,
            temp_path,
            scan_index,
            max_points,
            bool(binary),
        )
        return _frame_cloud_response(payload) if binary else _json_safe(payload)
    except PointCloudError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        temp_path.unlink(missing_ok=True)


@router.post("/api/point-cloud/preview-npz")
async def preview_npz(
    file: UploadFile = File(...),
    max_points: int = Form(0),
    binary: int = 0,
):
    if not (file.filename or "").lower().endswith(".npz"):
        raise HTTPException(status_code=400, detail="Please upload an .npz point-cloud file.")
    raw = await file.read(MAX_PC_BYTES + 1)
    if len(raw) > MAX_PC_BYTES:
        raise HTTPException(status_code=413, detail="NPZ point-cloud file exceeds the upload limit.")
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded NPZ file is empty.")
    try:
        payload = await asyncio.to_thread(_npz_preview, raw, max_points, bool(binary))
        return _frame_cloud_response(payload) if binary else _json_safe(payload)
    except PointCloudError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _run_analysis(
    request: Request,
    state_code: Optional[str] = None,
    bridge_number: Optional[str] = None,
):
    raw_points, _colors, params = await _cloud_payload(request)
    if state_code is not None and bridge_number is not None:
        points = await _analysis_points(state_code, bridge_number, raw_points, params, to_world=True)
        under_feature = _under_feature_kind(state_code, bridge_number)
        context = await asyncio.to_thread(_bridge_context, state_code, bridge_number)
    else:
        points = _require_points(raw_points, params, to_world=True)
        under_feature = params.get("under_feature") or None
        context = None
    try:
        result = await asyncio.to_thread(
            analyze_bridge,
            points,
            _param_bool(params, "scale_known", True),
            under_feature,
            _param_bool(params, "use_vlm", True),
            _param_bool(params, "use_ai_cleaning", _param_bool(params, "use_sam", True)),
            params.get("model_mode") or "clearance",
            context,
            _param_bool(params, "use_local_sam", False),
        )
    except PointCloudError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Point-cloud analysis failed: {exc}") from exc
    if state_code is not None and bridge_number is not None and result.get("status") == "success":
        try:
            result["persistence"] = await asyncio.to_thread(
                _save_processed,
                state_code,
                bridge_number,
                points,
                result,
            )
        except Exception as exc:
            result.setdefault("warnings", []).append(f"Could not save processed result: {exc}")
            result["persistence"] = {"saved": False}
    return _json_safe(result)


@router.post("/api/bridges/{state_code}/{bridge_number}/point-cloud/analyze")
async def analyze_bridge_cloud(state_code: str, bridge_number: str, request: Request):
    return await _run_analysis(request, state_code, bridge_number)


@router.post("/api/point-cloud/analyze")
async def analyze_ad_hoc_cloud(request: Request):
    return await _run_analysis(request)


async def _run_segment(
    request: Request,
    state_code: Optional[str] = None,
    bridge_number: Optional[str] = None,
):
    raw_points, colors, params = await _cloud_payload(request)
    points = (
        await _analysis_points(state_code, bridge_number, raw_points, params, to_world=False)
        if state_code is not None and bridge_number is not None
        else _require_points(raw_points, params, to_world=False)
    )
    try:
        result = await asyncio.to_thread(
            segment_structure,
            points,
            colors,
            _param_bool(params, "use_qwen", True),
            _param_bool(params, "use_local_sam", False),
        )
        return _json_safe(result)
    except PointCloudError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Segmentation failed: {exc}") from exc


@router.post("/api/bridges/{state_code}/{bridge_number}/point-cloud/segment")
async def segment_bridge_cloud(state_code: str, bridge_number: str, request: Request):
    return await _run_segment(request, state_code, bridge_number)


@router.post("/api/point-cloud/segment")
async def segment_ad_hoc_cloud(request: Request):
    return await _run_segment(request)


@router.post("/api/point-cloud/smooth")
async def smooth_cloud(request: Request):
    raw_points, colors, params = await _cloud_payload(request)
    points = _require_points(raw_points, params, to_world=False)
    iterations = max(1, min(4, int(params.get("iterations") or 1)))
    origin = _param_origin(params).tolist() if params.get("origin") else None
    as_binary = _param_bool(params, "binary", False)
    try:
        result = await asyncio.to_thread(
            clean_and_smooth_preview,
            points,
            colors,
            origin,
            True,
            iterations,
            300_000,
            "native",
            as_binary,
        )
        return _frame_cloud_response(result) if as_binary and result.get("status") == "success" else _json_safe(result)
    except PointCloudError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Smoothing failed: {exc}") from exc


async def _run_clearance(
    request: Request,
    state_code: Optional[str] = None,
    bridge_number: Optional[str] = None,
):
    raw_points, _colors, params = await _cloud_payload(request)
    scale_known = _param_bool(params, "scale_known", True)
    if state_code is not None and bridge_number is not None:
        points = await _analysis_points(state_code, bridge_number, raw_points, params, to_world=True)
        under_feature = _under_feature_kind(state_code, bridge_number)
        db_clearance = _db_clearance_ft(state_code, bridge_number) if scale_known else None
    else:
        points = _require_points(raw_points, params, to_world=True)
        under_feature = params.get("under_feature") or None
        raw_db = params.get("db_clearance_ft")
        db_clearance = float(raw_db) if scale_known and raw_db not in (None, "") else None
    try:
        result = await asyncio.to_thread(
            compute_clearance,
            points,
            64,
            db_clearance,
            True,
            scale_known,
            under_feature,
        )
        return _json_safe(result)
    except PointCloudError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Clearance analysis failed: {exc}") from exc


@router.post("/api/bridges/{state_code}/{bridge_number}/point-cloud/clearance")
async def bridge_clearance(state_code: str, bridge_number: str, request: Request):
    return await _run_clearance(request, state_code, bridge_number)


@router.post("/api/point-cloud/clearance")
async def ad_hoc_clearance(request: Request):
    return await _run_clearance(request)
