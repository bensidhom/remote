from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from . import llm_providers


def _hosted_vlm_generate(
    prompt: str,
    *,
    images_b64: Optional[List[str]] = None,
    timeout: int = 300,
    num_predict: int = 1024,
    system: str = "",
    temperature: float = 0.0,
) -> str:
    """Run point-cloud scene judgement through the configured hosted provider.

    Combine system instructions with the user prompt and let the server-owned
    Cerebras configuration select its vision-capable model.
    """
    full_prompt = f"{system.strip()}\n\n{prompt}" if system.strip() else prompt
    return llm_providers.generate(
        full_prompt,
        images_b64=images_b64,
        timeout=timeout,
        num_predict=num_predict,
        temperature=temperature,
    )


def _b64_bytes(arr: "np.ndarray") -> str:
    """Pack an array's raw bytes as base64 â€” per-point label arrays go to the
    browser this way instead of as multi-MB JSON int lists (which also cost a
    full `.tolist()` + a recursive clean_nan_values walk server-side)."""
    return base64.b64encode(np.ascontiguousarray(arr).tobytes()).decode("ascii")

# Optional accelerators. The geometry below works on numpy alone; scipy gives a
# fast KD-tree for label expansion, and open3d (if ever available on this Python)
# would provide battle-tested RANSAC/DBSCAN. Both are imported defensively so the
# feature degrades gracefully rather than failing to import.
try:  # scipy ships in the backend venv
    from scipy.spatial import cKDTree as _cKDTree  # type: ignore

    _HAS_SCIPY = True
except Exception:  # pragma: no cover
    _cKDTree = None  # type: ignore
    _HAS_SCIPY = False

try:  # open3d has no cp314 wheel today; kept as a forward-looking fast path
    import open3d as _o3d  # type: ignore

    _HAS_OPEN3D = True
except Exception:
    _o3d = None  # type: ignore
    _HAS_OPEN3D = False


def _kdtree_workers() -> int:
    """KD-tree query parallelism. workers=-1 (every core) overlapping with the
    GPU models pushes laptop package power hard enough to trip firmware-level
    shutdowns; leave two cores of headroom by default. Override with
    BRIDGEGUARD_KDTREE_WORKERS (>0)."""
    try:
        w = int(os.getenv("BRIDGEGUARD_KDTREE_WORKERS", "0"))
    except ValueError:
        w = 0
    return w if w > 0 else max(1, (os.cpu_count() or 4) - 2)


KDT_WORKERS = _kdtree_workers()

DEFAULT_MAX_POINTS = 200_000
MAX_RENDER_POINTS = 2_000_000

# Analysis caps â€” clearance/segmentation run on at most this many points; labels
# are then expanded back to every rendered point via nearest-neighbour.
MAX_ANALYSIS_POINTS = 300_000
MAX_STRUCTURAL_ANALYSIS_POINTS = int(os.getenv("PC_STRUCTURAL_MAX_POINTS", "600000"))

# Structural component classes. Kept aligned (id + colour) with the legacy
# client-side legend so the viewer can render them consistently; Qwen may rename
# them open-vocabulary but the ids/colours stay stable.
SEG_GROUND = 0
SEG_SUBSTRUCTURE = 1
SEG_SOFFIT = 2
SEG_DECK = 3
SEG_UNCLASSIFIED = 4
SEG_NOISE = 5  # vegetation / outliers, removed from clearance + structure analysis

DEFAULT_CLASSES: List[Dict[str, Any]] = [
    {"id": SEG_GROUND, "key": "ground", "name": "Ground / Road Surface", "color_rgb": [0.13, 0.82, 0.37], "color_hex": "#20d15f", "is_clearance": True},
    {"id": SEG_SUBSTRUCTURE, "key": "substructure", "name": "Substructure (Piers / Abutments)", "color_rgb": [0.98, 0.45, 0.12], "color_hex": "#fa721f", "is_clearance": False},
    {"id": SEG_SOFFIT, "key": "soffit", "name": "Soffit / Bridge Underside", "color_rgb": [0.22, 0.72, 1.00], "color_hex": "#38b8ff", "is_clearance": True},
    {"id": SEG_DECK, "key": "deck", "name": "Bridge Deck / Superstructure", "color_rgb": [0.98, 0.88, 0.18], "color_hex": "#fae02e", "is_clearance": False},
    {"id": SEG_UNCLASSIFIED, "key": "unclassified", "name": "Unclassified", "color_rgb": [0.45, 0.45, 0.55], "color_hex": "#737380", "is_clearance": False},
    {"id": SEG_NOISE, "key": "noise", "name": "Noise / Vegetation (removed)", "color_rgb": [0.40, 0.26, 0.13], "color_hex": "#66421f", "is_clearance": False},
]

_FT_PER_M = 3.280839895

# Controlled vocabulary for component/material naming, distilled from the NJDOT
# Bridge Element Inspection Manual and the AASHTO Manual for Bridge Element
# Inspection. Injected into the multimodal model's prompt so its open-vocabulary
# names map onto documented bridge elements + materials rather than free text.
BRIDGE_TAXONOMY = (
    "Standard bridge inspection elements (AASHTO / NJDOT Bridge Element Inspection Manual):\n"
    "- DECK / SLAB group: reinforced-concrete deck, prestressed-concrete deck, steel deck, "
    "timber deck; wearing surface (bituminous/asphalt overlay); parapet, barrier, railing, sidewalk.\n"
    "- SUPERSTRUCTURE group: steel girder/beam, prestressed-concrete girder, reinforced-concrete "
    "girder, steel stringer, floor beam, truss, arch, bearings.\n"
    "- SUBSTRUCTURE group: reinforced-concrete pier/column, pier wall, pier cap, "
    "reinforced-concrete abutment, wingwall, masonry/stone substructure, timber pile bent, footing.\n"
    "- APPROACH / SITE: roadway surface under the bridge, embankment/ground, slope protection.\n"
    "Materials seen in reports: reinforced concrete, prestressed/post-tensioned concrete, "
    "structural steel, weathering (Cor-Ten) steel, timber, masonry/stone."
)


class PointCloudError(RuntimeError):
    """Raised when a point cloud cannot be parsed into a preview payload."""


def _require_pye57():
    try:
        import pye57  # type: ignore

        return pye57
    except ImportError as exc:
        raise PointCloudError(
            "E57 support is not installed. Install backend dependency 'pye57'."
        ) from exc


def _scan_count(e57: Any) -> int:
    try:
        return len(e57.image_file.root()["data3D"])
    except Exception:
        return 1


def _read_scan(e57: Any, scan_index: int) -> Dict[str, np.ndarray]:
    attempts = (
        {"colors": True, "intensity": True, "ignore_missing_fields": True},
        {"colors": True, "ignore_missing_fields": True},
        {"intensity": True, "ignore_missing_fields": True},
        {"ignore_missing_fields": True},
        {"colors": True, "intensity": True},
        {"colors": True},
        {"intensity": True},
        {},
    )
    last_error: Optional[Exception] = None
    for kwargs in attempts:
        try:
            return e57.read_scan(scan_index, **kwargs)
        except Exception as exc:
            last_error = exc

    raise PointCloudError(f"Unable to read E57 scan {scan_index}: {last_error}")


def _array_or_none(data: Dict[str, np.ndarray], key: str) -> Optional[np.ndarray]:
    value = data.get(key)
    if value is None:
        return None
    return np.asarray(value)


def _normalize_colors(data: Dict[str, np.ndarray], indices: np.ndarray) -> Optional[np.ndarray]:
    red = _array_or_none(data, "colorRed")
    green = _array_or_none(data, "colorGreen")
    blue = _array_or_none(data, "colorBlue")
    if red is not None and green is not None and blue is not None:
        colors = np.column_stack((red[indices], green[indices], blue[indices])).astype(np.float32)
        max_value = float(np.nanmax(colors)) if colors.size else 0.0
        if max_value > 1.0:
            colors = colors / 255.0
        return np.clip(colors, 0.0, 1.0)

    intensity = _array_or_none(data, "intensity")
    if intensity is not None:
        values = intensity[indices].astype(np.float32)
        finite = np.isfinite(values)
        if finite.any():
            lo = float(np.nanmin(values[finite]))
            hi = float(np.nanmax(values[finite]))
            if hi > lo:
                values = (values - lo) / (hi - lo)
            else:
                values = np.ones_like(values) * 0.75
        else:
            values = np.ones_like(values) * 0.75
        values = np.clip(values, 0.0, 1.0)
        return np.column_stack((values, values, values)).astype(np.float32)

    return None


def _safe_float(value: Any) -> float:
    number = float(value)
    if not np.isfinite(number):
        return 0.0
    return number


def read_e57_preview(
    file_path: str | Path,
    scan_index: int = 0,
    max_points: Optional[int] = None,
    as_arrays: bool = False,
) -> Dict[str, Any]:
    """Read an E57 scan for browser preview rendering. `max_points` falsy (None/0)
    loads EVERY point (no downsampling â€” the default, so nothing is silently trimmed);
    a positive value caps the rendered count (clamped to MAX_RENDER_POINTS).
    `as_arrays=True` keeps `points`/`colors` as numpy arrays (float32 / 0-1 float)
    for the binary response framing instead of JSON-ready lists."""
    pye57 = _require_pye57()
    path = Path(file_path)
    if not path.exists():
        raise PointCloudError(f"Point cloud file does not exist: {path}")

    max_points = max(1_000, min(int(max_points), MAX_RENDER_POINTS)) if max_points else None

    e57 = pye57.E57(str(path))
    total_scans = _scan_count(e57)
    if scan_index < 0 or scan_index >= total_scans:
        raise PointCloudError(f"Scan index {scan_index} is out of range. File has {total_scans} scan(s).")

    data = _read_scan(e57, scan_index)
    x = _array_or_none(data, "cartesianX")
    y = _array_or_none(data, "cartesianY")
    z = _array_or_none(data, "cartesianZ")
    if x is None or y is None or z is None:
        raise PointCloudError("E57 scan does not include cartesianX/cartesianY/cartesianZ fields.")

    points = np.column_stack((x, y, z)).astype(np.float32)
    valid_mask = np.isfinite(points).all(axis=1)
    points = points[valid_mask]
    original_count = int(points.shape[0])
    if original_count == 0:
        raise PointCloudError("E57 scan contains no valid cartesian points.")

    if max_points is not None and original_count > max_points:
        indices = np.linspace(0, original_count - 1, num=max_points, dtype=np.int64)
    else:
        indices = np.arange(original_count, dtype=np.int64)

    sampled = points[indices]
    colors = _normalize_colors(
        {key: np.asarray(value)[valid_mask] for key, value in data.items() if isinstance(value, np.ndarray)},
        indices,
    )

    center = sampled.mean(axis=0)
    centered = sampled - center
    mins = sampled.min(axis=0)
    maxs = sampled.max(axis=0)

    payload: Dict[str, Any] = {
        "status": "success",
        "file_name": path.name,
        "scan_index": scan_index,
        "scan_count": total_scans,
        "original_point_count": original_count,
        "rendered_point_count": int(sampled.shape[0]),
        "downsampled": original_count > sampled.shape[0],
        "bounds": {
            "min": [_safe_float(v) for v in mins],
            "max": [_safe_float(v) for v in maxs],
            "center": [_safe_float(v) for v in center],
        },
        "points": centered.astype(np.float32) if as_arrays else centered.round(5).tolist(),
        "colors": (np.clip(colors, 0, 1) if as_arrays else colors.round(4).tolist())
                  if colors is not None else None,
    }
    return payload


# ============================================================================
#  Geometry analysis â€” vertical clearance & structural segmentation
#  Z-up convention (matches the viewer / CloudCompare). Works on numpy alone;
#  scipy KD-tree accelerates label expansion when present.
# ============================================================================

def _as_xyz(points: Any) -> np.ndarray:
    """Coerce an arbitrary points payload into a finite (N,3) float64 array."""
    arr = np.asarray(points, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[1] < 3:
        raise PointCloudError("Points must be an (N,3) array of [x, y, z].")
    arr = arr[:, :3]
    arr = arr[np.isfinite(arr).all(axis=1)]
    return arr


def _subsample(n: int, cap: int) -> np.ndarray:
    """Even index subsample of [0, n) down to at most `cap` points."""
    if n <= cap:
        return np.arange(n, dtype=np.int64)
    return np.linspace(0, n - 1, num=cap, dtype=np.int64)


def world_points_from_e57(file_path: str | Path, max_points: int = MAX_ANALYSIS_POINTS) -> Dict[str, Any]:
    """Load an E57 scan as world-coordinate points (reuses the preview reader)."""
    preview = read_e57_preview(file_path, max_points=max_points)
    centered = np.asarray(preview["points"], dtype=np.float64)
    center = np.asarray(preview["bounds"]["center"], dtype=np.float64)
    world = centered + center
    colors = preview.get("colors")
    return {
        "points": world,
        "colors": np.asarray(colors, dtype=np.float64) if colors is not None else None,
        "center": center,
    }


def _cell_groups(cell_ids: np.ndarray) -> List[np.ndarray]:
    """Group point indices by cell id. Returns a list of index arrays."""
    order = np.argsort(cell_ids, kind="stable")
    sorted_ids = cell_ids[order]
    if sorted_ids.size == 0:
        return []
    boundaries = np.flatnonzero(np.diff(sorted_ids)) + 1
    return np.split(order, boundaries)


def denoise_mask(pts: np.ndarray, k: int = 12, std_ratio: float = 2.0,
                 surfvar_thresh: float = 0.11, do_statistical: bool = True,
                 do_surfvar: bool = True) -> np.ndarray:
    """Return a boolean mask of NOISE points (sparse outliers + vegetation).

    Two complementary tests, both local:
    - Statistical outlier removal: a point whose mean distance to its k nearest
      neighbours is far above the cloud average (> mean + std_ratioÂ·std) is an
      isolated outlier (stray returns, birds, sensor noise).
    - Surface-variation (scatter): the smallest/total eigenvalue ratio of the local
      covariance. Planar surfaces (deck, road) and linear edges (pier corners) have
      low variation; volumetric scatter (tree canopy, foliage, bushes) is high. This
      is what removes the trees/sidewalk clutter that otherwise reads as substructure.
    `do_statistical`/`do_surfvar` toggle each test independently â€” e.g. when Open3D
    already did statistical-outlier removal, call with do_statistical=False to add
    only the (Open3D-less) vegetation test. Needs scipy; without it returns all-False."""
    n = pts.shape[0]
    noise = np.zeros(n, dtype=bool)
    if not (_HAS_SCIPY and _cKDTree is not None) or n < 200:
        return noise
    tree = _cKDTree(pts)
    kk = min(k, n - 1)
    dists, idx = tree.query(pts, k=kk + 1, workers=KDT_WORKERS)  # includes self at col 0
    if do_statistical:
        mean_d = dists[:, 1:].mean(axis=1)
        thr = mean_d.mean() + std_ratio * mean_d.std()
        noise |= mean_d > thr

    if do_surfvar:
        # Local surface variation via chunked covariance eigenvalues.
        surfvar = np.zeros(n)
        step = 50_000
        for s in range(0, n, step):
            nb = pts[idx[s:s + step]]                   # (m, kk+1, 3)
            c = nb - nb.mean(axis=1, keepdims=True)
            cov = np.einsum("mki,mkj->mij", c, c) / (kk + 1)
            ev = np.linalg.eigvalsh(cov)                # ascending
            surfvar[s:s + step] = ev[:, 0] / (ev.sum(axis=1) + 1e-12)
        noise |= surfvar > surfvar_thresh
    return noise


def mls_smooth(pts: np.ndarray, k: int = 16, iterations: int = 1) -> np.ndarray:
    """Moving-Least-Squares surface smoothing: project each point onto a plane fit
    to its k nearest neighbours (Gaussian-weighted). Smooths surfaces + softens
    edges and pulls noise onto the local surface, while preserving point order/count.
    No-op without scipy."""
    if not (_HAS_SCIPY and _cKDTree is not None) or pts.shape[0] < k:
        return pts
    out = pts.astype(np.float64).copy()
    kk = min(k, out.shape[0] - 1)
    for _ in range(max(1, iterations)):
        tree = _cKDTree(out)
        dist, idx = tree.query(out, k=kk + 1, workers=KDT_WORKERS)
        nb = out[idx]                                  # (n, kk+1, 3)
        # Gaussian weights by neighbour distance (local support ~ mean spacing).
        h = np.maximum(dist[:, 1:].mean(axis=1, keepdims=True), 1e-6)
        w = np.exp(-(dist ** 2) / (h ** 2))            # (n, kk+1)
        w = w / w.sum(axis=1, keepdims=True)
        c = np.einsum("nk,nkd->nd", w, nb)             # weighted centroid
        diff = nb - c[:, None, :]
        cov = np.einsum("nk,nki,nkj->nij", w, diff, diff)
        evals, evecs = np.linalg.eigh(cov)             # ascending
        normals = evecs[:, :, 0]                       # plane normal = smallest-variance dir
        v = out - c
        d = np.einsum("nd,nd->n", v, normals)
        out = out - d[:, None] * normals               # project onto local plane
    return out


def _open3d_outlier_keep(pts: np.ndarray, std_ratio: float = 2.0):
    """Statistical-outlier keep-mask via Open3D (out-of-process, Python-3.12 venv).
    Returns a boolean keep-mask of len(pts), or None if Open3D is unavailable/failed
    so the caller falls back to the numpy denoiser."""
    try:
        from . import open3d_bridge
        if not open3d_bridge.open3d_available():
            return None
        res = open3d_bridge.run_op("remove_outliers", pts, params={
            "nb_neighbors": 20, "std_ratio": std_ratio})
        if not res or "indices" not in res:
            return None
        keep = np.zeros(pts.shape[0], dtype=bool)
        keep[res["indices"]] = True
        return keep
    except Exception:
        return None


def clean_and_smooth_preview(points: Any, colors: Any = None, origin: Any = None,
                             remove_outliers: bool = True, iterations: int = 1,
                             max_points: int = MAX_ANALYSIS_POINTS,
                             engine: str = "auto", as_arrays: bool = False) -> Dict[str, Any]:
    """Remove outliers/vegetation then MLS-smooth a cloud, returning a viewer preview
    payload (centred points + bounds + colours), same shape as read_e57_preview.
    `origin` (the client's centre) is added back so the result stays in world units.
    `engine`: "auto" uses Open3D's statistical-outlier removal when available (the
    numpy vegetation test always runs), "native" forces the pure numpy/scipy path."""
    pts = _as_xyz(points)
    n_in = int(pts.shape[0])
    if n_in < 30:
        return {"status": "insufficient_points", "point_count": n_in}
    cols = np.asarray(colors, dtype=np.float64) if colors is not None else None
    if cols is not None and cols.shape[0] != n_in:
        cols = None
    if origin is not None:
        pts = pts + np.asarray(origin, dtype=np.float64)[:3]

    if n_in > max_points:
        idx = _subsample(n_in, max_points)
        pts = pts[idx]
        if cols is not None:
            cols = cols[idx]

    removed = 0
    used_open3d = False
    if remove_outliers:
        # Open3D does statistical-outlier removal; the numpy surface-variation test
        # (vegetation/scatter â€” no Open3D equivalent) always runs on top.
        o3d_keep = _open3d_outlier_keep(pts) if engine in ("auto", "open3d") else None
        if o3d_keep is not None:
            used_open3d = True
            noise = ~o3d_keep
            noise |= denoise_mask(pts, do_statistical=False, do_surfvar=True)
        else:
            noise = denoise_mask(pts)
        if int((~noise).sum()) >= 30:
            keep = ~noise
            removed = int(noise.sum())
            pts = pts[keep]
            if cols is not None:
                cols = cols[keep]

    smoothed = mls_smooth(pts, iterations=iterations)

    center = smoothed.mean(axis=0)
    centered = smoothed - center
    mins, maxs = smoothed.min(axis=0), smoothed.max(axis=0)
    return {
        "status": "success",
        "source": "smoothed",
        "engine": "open3d+numpy" if used_open3d else "numpy",
        "original_point_count": n_in,
        "rendered_point_count": int(centered.shape[0]),
        "noise_removed": removed,
        "downsampled": n_in > centered.shape[0],
        "bounds": {
            "min": [_safe_float(v) for v in mins],
            "max": [_safe_float(v) for v in maxs],
            "center": [_safe_float(v) for v in center],
        },
        "points": centered.astype(np.float32) if as_arrays else centered.round(5).tolist(),
        "colors": (np.clip(cols, 0, 1) if as_arrays else np.clip(cols, 0, 1).round(4).tolist())
                  if cols is not None else None,
    }


def _label_grid(mask: np.ndarray):
    """Connected components of a boolean grid (8-connectivity). Uses scipy when
    available, else a small union-find fallback so clustering still works."""
    try:
        from scipy.ndimage import label as _lbl
        lab, n = _lbl(mask, structure=np.ones((3, 3)))
        return lab, n
    except Exception:
        # Minimal 8-neighbour flood fill.
        ny, nx = mask.shape
        lab = np.zeros((ny, nx), np.int64)
        cur = 0
        for sy in range(ny):
            for sx in range(nx):
                if not mask[sy, sx] or lab[sy, sx]:
                    continue
                cur += 1
                stack = [(sy, sx)]
                lab[sy, sx] = cur
                while stack:
                    yy, xx = stack.pop()
                    for dy in (-1, 0, 1):
                        for dx in (-1, 0, 1):
                            ay, ax = yy + dy, xx + dx
                            if 0 <= ay < ny and 0 <= ax < nx and mask[ay, ax] and not lab[ay, ax]:
                                lab[ay, ax] = cur
                                stack.append((ay, ax))
        return lab, cur


def _fit_plane(P: np.ndarray) -> Tuple[np.ndarray, float]:
    """Robust best-fit plane z = aÂ·x + bÂ·y + c (least squares + two MAD-reweighted
    passes to shrug off residual outliers). Returns (coeffs[a,b,c], inlier RMS in m).
    Bridges are built from geometric surfaces, so the road and deck soffit are
    well modelled by a plane; measuring from the fitted surface is far more stable
    than per-point statistics."""
    A = np.column_stack([P[:, 0], P[:, 1], np.ones(len(P))])
    zz = P[:, 2]
    coef, *_ = np.linalg.lstsq(A, zz, rcond=None)
    for _ in range(2):
        r = zz - A @ coef
        mad = float(np.median(np.abs(r - np.median(r)))) + 1e-9
        keep = np.abs(r) < 2.5 * mad
        if keep.sum() < 6:
            break
        coef, *_ = np.linalg.lstsq(A[keep], zz[keep], rcond=None)
    rms = float(np.sqrt(np.mean((zz - A @ coef) ** 2)))
    return coef, rms


def _plane_z(coef: np.ndarray, xy: np.ndarray) -> np.ndarray:
    return coef[0] * xy[:, 0] + coef[1] * xy[:, 1] + coef[2]


def _verticality(pts: np.ndarray, k: int = 12) -> np.ndarray:
    """Per-point |normal_z| via local-neighbourhood PCA: ~1 on horizontal surfaces
    (road, deck soffit), ~0 on vertical surfaces (parapets, poles, sign faces,
    barrier panels). Used to fit measurement surfaces to true horizontal geometry
    only, so vertical clutter can't drag the soffit/road planes. All-ones if scipy
    is unavailable (degrades to using every point)."""
    n = pts.shape[0]
    if not (_HAS_SCIPY and _cKDTree is not None) or n < 50:
        return np.ones(n)
    tree = _cKDTree(pts)
    kk = min(k, n - 1)
    _, idx = tree.query(pts, k=kk + 1, workers=KDT_WORKERS)
    out = np.ones(n)
    step = 50_000
    for s in range(0, n, step):
        nb = pts[idx[s:s + step]]
        c = nb - nb.mean(axis=1, keepdims=True)
        cov = np.einsum("mki,mkj->mij", c, c) / (kk + 1)
        evals, evecs = np.linalg.eigh(cov)          # ascending; normal = smallest
        out[s:s + step] = np.abs(evecs[:, :, 0][:, 2])
    return out


def compute_clearance(
    points: Any,
    grid: int = 64,
    db_clearance_ft: Optional[float] = None,
    clean: bool = True,
    scale_known: bool = True,
    under_feature: Optional[str] = None,
) -> Dict[str, Any]:
    """Measure vertical clearance (road/water surface â†’ bridge underside) from a scan.

    Approach (slope-robust, Z-up), aligned with DOT mobile-LiDAR practice (per-cell
    column min-gap) and FHWA (report the minimum over each *traveled way*):

    1. Bin points into an XY grid; in each cell find the vertical air gap between the
       road surface (top of the lower mass) and the soffit (bottom of the overhead
       deck mass) â€” the per-column clearance.
    2. Separate the qualifying cells into distinct **roadways** (connected components),
       so a divided highway or a highway+ramp under one deck is reported per roadway.
    3. Per roadway report the controlling minimum over the traveled way (robust low
       percentile, excluding the fascia/abutment edge) with its location, the
       centre-lane value, and the maximum.
    4. **Waterway** case: if the bridge crosses water (``under_feature='waterway'``)
       or the area under the deck is a LiDAR return-void (water absorbs the laser),
       switch to navigation-clearance semantics â€” soffit to an inferred water level â€”
       and do not attempt roadway/road-centre logic.

    ``under_feature`` ('highway'|'waterway'|None) comes from the bridge's SNBI feature
    record when available; None auto-detects. Photogrammetric clouds (scale unknown)
    report relative units and skip the DB cross-check.
    """
    pts_all = _as_xyz(points)
    n_in = int(pts_all.shape[0])
    if n_in < 50:
        return {"status": "insufficient_points", "point_count": n_in}

    idx = _subsample(n_in, MAX_ANALYSIS_POINTS)
    pts = pts_all[idx]
    noise_removed = 0
    if clean:
        nm = denoise_mask(pts)
        if int((~nm).sum()) >= 50:
            noise_removed = int(nm.sum())
            pts = pts[~nm]
    n = int(pts.shape[0])
    # Per-point horizontality, so the road/soffit surfaces are fitted only to true
    # horizontal geometry (not parapets, poles, sign panels, barrier faces).
    vert = _verticality(pts)

    x, y, z = pts[:, 0], pts[:, 1], pts[:, 2]
    z_lo, z_hi = np.percentile(z, [1.0, 99.0])
    z_span = float(z_hi - z_lo) or 1.0
    ground_z_global = float(np.percentile(z, 2.0))
    min_gap = max(1.2, 0.18 * z_span)

    x_lo, x_hi = np.percentile(x, [1.0, 99.0])
    y_lo, y_hi = np.percentile(y, [1.0, 99.0])
    x_rng = float(x_hi - x_lo) or 1.0
    y_rng = float(y_hi - y_lo) or 1.0
    gxf = np.clip(((x - x_lo) / x_rng * grid).astype(np.int64), 0, grid - 1)
    gyf = np.clip(((y - y_lo) / y_rng * grid).astype(np.int64), 0, grid - 1)
    cell = gyf * grid + gxf

    def cell_xy(ci: int) -> Tuple[float, float]:
        return (float(x_lo + ((ci % grid) + 0.5) / grid * x_rng),
                float(y_lo + ((ci // grid) + 0.5) / grid * y_rng))

    # Per-cell min/max Z â†’ deck-overhead and ground-below flags for waterway detection.
    cell_zmin = np.full(grid * grid, np.inf)
    cell_zmax = np.full(grid * grid, -np.inf)
    np.minimum.at(cell_zmin, cell, z)
    np.maximum.at(cell_zmax, cell, z)
    has_deck = cell_zmax > (z_lo + 0.45 * z_span)
    has_ground = cell_zmin < (z_lo + 0.15 * z_span)
    water_candidate = has_deck & ~has_ground
    deck_cells = int(has_deck.sum())
    void_frac = float(water_candidate.sum() / max(1, deck_cells))
    wc_grid = water_candidate.reshape(grid, grid)
    _, _nw = _label_grid(wc_grid)
    # size of the largest contiguous void
    lab_w, nw = _label_grid(wc_grid)
    biggest_void = max((int((lab_w == i).sum()) for i in range(1, nw + 1)), default=0)
    auto_water = void_frac > 0.35 and biggest_void > max(40, int(0.05 * deck_cells))
    is_waterway = (under_feature == "waterway") or (under_feature is None and auto_water)

    # Robust per-cell road/soffit/clearance via the largest vertical gap. Also tag
    # each point as road (below the gap) or soffit (above) so the road and deck
    # surfaces can be plane-fitted per roadway.
    clr_grid = np.full(grid * grid, np.nan)
    road_grid = np.full(grid * grid, np.nan)
    sof_grid = np.full(grid * grid, np.nan)
    pt_class = np.zeros(n, dtype=np.int8)   # 0=unclassified, 1=road, 2=soffit
    sections: List[Dict[str, float]] = []
    for g in _cell_groups(cell):
        if g.size < 10:
            continue
        zc = np.sort(z[g]); gaps = np.diff(zc); k = int(np.argmax(gaps))
        if gaps[k] < min_gap:
            continue
        below, above = zc[: k + 1], zc[k + 1:]
        if below.size < 4 or above.size < 4:
            continue
        road_surf = float(np.percentile(below, 95.0))   # top shell of the road returns
        soffit = float(np.percentile(above, 5.0))        # bottom shell of the deck/soffit returns
        clr = soffit - road_surf
        if clr < 0.5:
            continue
        ci = int(cell[g[0]])
        clr_grid[ci] = clr; road_grid[ci] = road_surf; sof_grid[ci] = soffit
        thr = float(zc[k])                               # gap lower edge
        road_band = max(0.05, min(0.35, 0.025 * z_span))
        soffit_band = max(0.05, min(0.45, 0.030 * z_span))
        zg = z[g]
        # Fit road from the TOP of the lower mass and soffit from the BOTTOM of
        # the overhead mass. Using every point below/above the air gap lets curbs,
        # embankments, parapets or deck-top returns bias the planes.
        pt_class[g[(zg <= thr) & (zg >= road_surf - road_band)]] = 1
        pt_class[g[(zg > thr) & (zg <= soffit + soffit_band)]] = 2
        cx, cy = cell_xy(ci)
        sections.append({"x": round(cx, 3), "y": round(cy, 3), "clearance_m": round(clr, 3)})

    valid = ~np.isnan(clr_grid)
    nvalid = int(valid.sum())
    waterway_info = {
        "is_waterway": bool(is_waterway),
        "void_fraction_under_deck": round(void_frac, 3),
        "source": "bridge_feature" if under_feature in ("highway", "waterway") else "point_cloud_void",
    }

    # ---- Waterway: navigation clearance to an inferred reference water level ----
    if is_waterway:
        deck_sof = sof_grid[has_deck & ~np.isnan(sof_grid)]
        soffit_z = float(np.percentile(deck_sof, 10.0)) if deck_sof.size else float(np.percentile(z, 90.0))
        # Reference water level â‰ˆ the surrounding ground/bank returns (median of the
        # lowest returns), a proxy for the datum until a gage/datum is provided.
        water_level = ground_z_global
        nav_clr = max(0.0, soffit_z - water_level)
        out: Dict[str, Any] = {
            "status": "success",
            "method": "waterway_navigation",
            "confidence": "medium" if deck_sof.size >= 20 else "low",
            "scale_known": bool(scale_known),
            "under_feature": "waterway",
            "waterway": waterway_info,
            "point_count": n,
            "noise_removed": noise_removed,
            "water_level_z": round(water_level, 4),
            "soffit_z": round(soffit_z, 4),
            "clearance_m": round(nav_clr, 3),
            "min_clearance_m": round(nav_clr, 3),
            "typical_clearance_m": round(nav_clr, 3),
            "road_center": None,
            "clearance_line": None,
            "cross_sections": sections[:200],
            "note": ("Waterway under the bridge: navigation vertical clearance is measured "
                     "from the soffit to an inferred reference water level (LiDAR returns "
                     "no data on water). Confirm against the controlling gage datum."),
        }
        if scale_known:
            out["clearance_ft"] = round(nav_clr * _FT_PER_M, 2)
            out["min_clearance_ft"] = out["clearance_ft"]
        else:
            out["scale_note"] = ("Reconstruction has no absolute scale; values are relative "
                                 "units. Calibrate against a known dimension.")
        return out

    # ---- Highway: separate roadways and measure each traveled way ----
    roadways: List[Dict[str, Any]] = []
    if nvalid:
        # Close small gaps (pier shadows block the scanner and punch holes through a
        # roadway's cells) BEFORE connected components, otherwise one traveled way
        # splits into 2-3 phantom "roadways". Components are found on the dilated
        # mask; stats still use only the original valid cells.
        vgrid = valid.reshape(grid, grid)
        dil = vgrid.copy()
        for _ in range(2):
            d2 = dil.copy()
            d2[1:, :] |= dil[:-1, :]; d2[:-1, :] |= dil[1:, :]
            d2[:, 1:] |= dil[:, :-1]; d2[:, :-1] |= dil[:, 1:]
            dil = d2
        lab_d, ncomp = _label_grid(dil)
        lab = np.where(vgrid, lab_d, 0)
        min_cells = max(12, int(0.04 * nvalid))
        for i in range(1, ncomp + 1):
            comp = (lab == i)
            csz = int(comp.sum())
            if csz < min_cells:
                continue
            cyi, cxi = np.where(comp)
            cids = cyi * grid + cxi
            cell_set = set(int(c) for c in cids)
            in_rw = np.fromiter((int(c) in cell_set for c in cell), bool, count=n)
            # Fit the surfaces to HORIZONTAL points only, so parapets / barrier
            # panels / sign posts / poles cannot pull the soffit or road plane.
            road_sel = in_rw & (pt_class == 1) & (vert > 0.5)
            sof_sel = in_rw & (pt_class == 2) & (vert > 0.5)
            if int(road_sel.sum()) < 30:      # fall back to all road points if sparse
                road_sel = in_rw & (pt_class == 1)
            if int(sof_sel.sum()) < 30:
                sof_sel = in_rw & (pt_class == 2)
            road_pts = pts[road_sel]
            sof_pts = pts[sof_sel]
            if len(road_pts) < 30 or len(sof_pts) < 30:
                continue
            # Fit the two geometric surfaces and measure clearance BETWEEN THEM.
            road_coef, road_rms = _fit_plane(road_pts)
            sof_coef, sof_rms = _fit_plane(sof_pts)
            # A real traveled way has a near-planar road surface; a bad fit means
            # embankment/edge clutter, not a roadway â†’ reject.
            if road_rms > 0.6:
                continue
            cxw = (x_lo + ((cids % grid) + 0.5) / grid * x_rng)
            cyw = (y_lo + ((cids // grid) + 0.5) / grid * y_rng)
            xy = np.column_stack([cxw, cyw])
            clr = _plane_z(sof_coef, xy) - _plane_z(road_coef, xy)   # from fitted surfaces
            # Transverse axis (PCA) â†’ restrict the minimum to the traveled way.
            cc = np.column_stack([cxw - cxw.mean(), cyw - cyw.mean()])
            _, evec = np.linalg.eigh(cc.T @ cc)
            v = cc @ evec[:, 0]
            travel = np.abs(v) <= np.percentile(np.abs(v), 85)
            idx_t = np.where(travel)[0] if travel.sum() >= 8 else np.arange(len(clr))
            loc = int(idx_t[np.argmin(clr[idx_t])])          # controlling (lowest) point
            # A poor soffit fit (arch/complex underside) â†’ note lower confidence.
            surf_ok = sof_rms <= 1.0

            def _pt_xy(cxv, cyv):
                p1 = np.array([[cxv, cyv]])
                return [round(float(cxv), 3), round(float(cyv), 3),
                        round(float(_plane_z(road_coef, p1)[0]), 4),
                        round(float(_plane_z(sof_coef, p1)[0]), 4)]

            def _pt(i):
                return _pt_xy(float(cxw[i]), float(cyw[i]))

            # Centre of the traveled way = the roadway centroid, evaluated on the
            # fitted planes (deterministic â€” doesn't hinge on a single cell).
            cx0, cy0 = float(cxw.mean()), float(cyw.mean())
            center_point = _pt_xy(cx0, cy0)
            center_m = center_point[3] - center_point[2]
            # Bounded polygons (convex hull of the roadway's cells, not the bbox â€”
            # a bbox quad vastly overdraws a diagonal roadway) for plotting.
            corners = _hull_poly(cxw, cyw, tol=0.8)
            roadways.append({
                "cell_count": csz,
                "controlling_min_m": round(float(clr[idx_t].min()), 3),
                "center_m": round(float(center_m), 3),
                "max_m": round(float(clr[idx_t].max()), 3),
                "median_m": round(float(np.median(clr[idx_t])), 3),
                "surface_fit": {"road_rms_m": round(road_rms, 3), "soffit_rms_m": round(sof_rms, 3),
                                "good_soffit": bool(surf_ok)},
                "min_location": _pt(loc)[:2],
                "min_road_z": _pt(loc)[2], "min_soffit_z": _pt(loc)[3],
                "center_point": center_point,
                "controlling_point": _pt(loc),
                "road_plane_corners": [[round(float(c[0]), 3), round(float(c[1]), 3),
                                        round(float(_plane_z(road_coef, c[None])[0]), 4)] for c in corners],
                "soffit_plane_corners": [[round(float(c[0]), 3), round(float(c[1]), 3),
                                          round(float(_plane_z(sof_coef, c[None])[0]), 4)] for c in corners],
            })
        roadways.sort(key=lambda r: -r["cell_count"])

    if roadways:
        primary = roadways[0]
        clearance_m = primary["center_m"]
        min_clr = primary["controlling_min_m"]
        typ_clr = primary["median_m"]
        rcx, rcy = primary["min_location"]
        road_center = [round(rcx, 3), round(rcy, 3)]
        clearance_line = {
            "ground": [rcx, rcy, primary["min_road_z"]],
            "soffit": [rcx, rcy, primary["min_soffit_z"]],
        }
        ground_z = primary["min_road_z"]
        soffit_z = primary["min_soffit_z"]
        method = "fitted_surfaces"
        good = primary.get("surface_fit", {}).get("good_soffit", False)
        confidence = "high" if (good and primary["cell_count"] >= 30) else "medium"
    else:
        # No clear road under a deck â€” global split: lowest mass = ground, overhead = deck.
        ground_z = ground_z_global
        overhead = z[z > ground_z + 0.40 * z_span]
        soffit_z = float(np.percentile(overhead, 5.0)) if overhead.size >= 10 else float(np.percentile(z, 95.0))
        min_clr = typ_clr = float(soffit_z - ground_z)
        clearance_m = min_clr
        road_center = None
        clearance_line = None
        method = "global_fallback"
        confidence = "low"

    clearance_m = max(0.0, clearance_m)
    clearance_ft = clearance_m * _FT_PER_M

    out = {
        "status": "success",
        "method": method,
        "confidence": confidence,
        "scale_known": bool(scale_known),
        "under_feature": "highway" if under_feature == "highway" else "highway_or_unknown",
        "waterway": waterway_info,
        "point_count": n,
        "noise_removed": noise_removed,
        "ground_z": round(ground_z, 4),
        "soffit_z": round(soffit_z, 4),
        "clearance_m": round(clearance_m, 3),
        "min_clearance_m": round(max(0.0, min_clr), 3),
        "typical_clearance_m": round(max(0.0, typ_clr), 3),
        "roadways": roadways,
        "roadway_count": len(roadways),
        "road_center": road_center,
        "clearance_line": clearance_line,
        "cross_sections": sections[:200],
    }

    if not scale_known:
        out["scale_note"] = (
            "Reconstruction has no absolute scale; values are relative units. "
            "Calibrate against a known dimension before using as feet/meters."
        )
        return out

    out["clearance_ft"] = round(clearance_ft, 2)
    out["min_clearance_ft"] = round(max(0.0, min_clr) * _FT_PER_M, 2)

    # DB cross-check compares the CONTROLLING minimum to the recorded minimum
    # vertical clearance (SNBI B.H.13 / NBI Item 53), per FHWA semantics.
    if db_clearance_ft is not None and np.isfinite(db_clearance_ft) and db_clearance_ft > 0:
        db_ft = float(db_clearance_ft)
        meas_min_ft = max(0.0, min_clr) * _FT_PER_M
        out["db_clearance_ft"] = round(db_ft, 2)
        out["db_clearance_m"] = round(db_ft / _FT_PER_M, 3)
        out["db_delta_ft"] = round(meas_min_ft - db_ft, 2)
        tol = max(1.0, 0.08 * db_ft)
        out["db_match"] = bool(abs(meas_min_ft - db_ft) <= tol)

    return out


# â”€â”€ Unified one-step bridge processing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Component label ids used by analyze_bridge.
_C_OFF, _C_DECK, _C_SUB, _C_ROAD, _C_GIRDER, _C_PARAPET, _C_UNKNOWN = 0, 1, 2, 3, 4, 5, 6
_C_META = {
    _C_DECK: ("deck", "Deck / superstructure", "slab", "#6366f1"),
    _C_SUB:  ("substructure", "Substructure (piers / abutments)", "column", "#ef4444"),
    _C_ROAD: ("under_surface", "Under bridge (road / ground)", "surface", "#f59e0b"),
    _C_GIRDER: ("girder", "Girders / beams / soffit ribs", "beam", "#0ea5e9"),
    _C_PARAPET: ("parapet", "Parapet / railing / barrier", "edge", "#a855f7"),
    _C_UNKNOWN: ("unknown_structure", "Unknown bridge structure", "unknown", "#10b981"),
}


def _robust_extent(vals: np.ndarray) -> Tuple[float, float]:
    """Extent trimmed to where point SUPPORT ends: the outermost bins
    (range/200 wide) still holding >= max(3, 0.15%) of the points. A dense
    edge (a deck end) keeps its true extent; a sparse tail (below-ground
    reflection streaks swept into a pier) is cut where its density dies.
    A plain percentile can do neither â€” it always shaves dense components
    and never reaches deep enough into a heavy tail (measured 0413152:
    min/max gave a 58 m-tall 'pier' box at 13% coverage)."""
    lo, hi = float(vals.min()), float(vals.max())
    if hi - lo < 1e-9 or len(vals) < 50:
        return lo, hi
    h, e = np.histogram(vals, bins=200)
    good = np.where(h >= max(3, int(0.0015 * len(vals))))[0]
    if good.size == 0:
        return lo, hi
    return float(e[good[0]]), float(e[good[-1] + 1])


def _obb_world(pts_xyz: np.ndarray, u_ax, v_ax, cxy) -> Dict[str, Any]:
    """Oriented bounding box in the bridge frame â†’ 8 world corners + dims + a
    coverage estimate (fraction of the largest face's grid cells that hold a
    point â€” how completely the scan covers the idealised volume)."""
    d = pts_xyz[:, :2] - cxy
    du = d @ u_ax; dv = d @ v_ax; zz = pts_xyz[:, 2]
    u1, u2 = _robust_extent(du)
    v1, v2 = _robust_extent(dv)
    z1, z2 = _robust_extent(zz)
    corners = []
    for uu in (u1, u2):
        for vv in (v1, v2):
            for z in (z1, z2):
                corners.append([round(float(cxy[0] + uu * u_ax[0] + vv * v_ax[0]), 3),
                                round(float(cxy[1] + uu * u_ax[1] + vv * v_ax[1]), 3),
                                round(z, 3)])
    dims = [round(u2 - u1, 2), round(v2 - v1, 2), round(z2 - z1, 2)]
    # coverage over the two largest dimensions
    order = np.argsort([u2 - u1, v2 - v1, z2 - z1])[::-1]
    axes_vals = [du, dv, zz]
    a, b = axes_vals[order[0]], axes_vals[order[1]]
    G = 40
    ai = np.clip(((a - a.min()) / ((a.max() - a.min()) or 1) * (G - 1)).astype(int), 0, G - 1)
    bi = np.clip(((b - b.min()) / ((b.max() - b.min()) or 1) * (G - 1)).astype(int), 0, G - 1)
    occ = np.zeros((G, G), bool); occ[ai, bi] = True
    return {"corners": corners, "dims_m": dims, "coverage_pct": round(100.0 * occ.mean(), 1)}


_RENDER_PALETTE = ["#6366f1", "#ef4444", "#f59e0b", "#10b981", "#a855f7", "#0ea5e9", "#e11d48", "#84cc16"]


# â”€â”€ Tight geometry: footprint isolation + fitted surface / prism meshes â”€â”€â”€â”€â”€â”€
def _uv(pts_xy: np.ndarray, cxy, u_ax, v_ax) -> Tuple[np.ndarray, np.ndarray]:
    """World XY â†’ bridge-frame (u=along-span, v=transverse)."""
    d = pts_xy - cxy
    return d @ u_ax, d @ v_ax


def _footprint_mask(u: np.ndarray, v: np.ndarray, sel: np.ndarray, ncell: int = 90,
                    dilation: int = 1, opening: int = 0):
    """Occupancy grid over the (u,v) extent of the SELECTED (deck-band) points; keep
    only the largest 8-connected blob and dilate one ring â†’ the bridge's true
    footprint. This is what lets us drop side vegetation and keep only what is
    directly under the deck (a rectangular u/v crop cannot). ``opening`` erodes
    that many rings BEFORE picking the blob (then dilates them back), which cuts
    thin 1-2-cell tendrils â€” railings/fences on the under-passing road that reach
    deck-band height would otherwise ride into the footprint. Returns a packed
    tuple consumed by ``_in_footprint``."""
    su, sv = u[sel], v[sel]
    u0, u1 = float(su.min()), float(su.max())
    v0, v1 = float(sv.min()), float(sv.max())
    span = max(u1 - u0, v1 - v0) or 1.0
    cell = span / ncell
    gu = max(4, int(np.ceil((u1 - u0) / cell)) + 1)
    gv = max(4, int(np.ceil((v1 - v0) / cell)) + 1)
    ci = np.clip(((su - u0) / cell).astype(int), 0, gu - 1)
    cj = np.clip(((sv - v0) / cell).astype(int), 0, gv - 1)
    occ = np.zeros((gu, gv), bool); occ[ci, cj] = True
    total_dilation = max(1, dilation)
    if opening > 0:
        ero = occ.copy()
        for _ in range(opening):
            prev = ero.copy()
            ero[1:, :] &= prev[:-1, :]; ero[:-1, :] &= prev[1:, :]
            ero[:, 1:] &= prev[:, :-1]; ero[:, :-1] &= prev[:, 1:]
            ero[0, :] = False; ero[-1, :] = False; ero[:, 0] = False; ero[:, -1] = False
        if ero.any():                                  # never open away the whole deck
            occ = ero
            total_dilation += opening                  # give the erosion back
    lab, ncomp = _label_grid(occ)
    if ncomp >= 1:
        big = int(np.argmax([(lab == i).sum() for i in range(1, ncomp + 1)])) + 1
        occ = lab == big
    dil = occ.copy()                                   # dilate to close gaps / keep edges
    for _ in range(total_dilation):
        prev = dil.copy()
        dil[1:, :] |= prev[:-1, :]; dil[:-1, :] |= prev[1:, :]
        dil[:, 1:] |= prev[:, :-1]; dil[:, :-1] |= prev[:, 1:]
    return (dil, u0, v0, cell, gu, gv)


def _in_footprint(u: np.ndarray, v: np.ndarray, fp) -> np.ndarray:
    mask, u0, v0, cell, gu, gv = fp
    inb = (u >= u0) & (u < u0 + gu * cell) & (v >= v0) & (v < v0 + gv * cell)
    ci = np.clip(((u - u0) / cell).astype(int), 0, gu - 1)
    cj = np.clip(((v - v0) / cell).astype(int), 0, gv - 1)
    return inb & mask[ci, cj]


def _surface_mesh(u, v, z, cxy, u_ax, v_ax, side="top", ncell=64) -> Dict[str, Any]:
    """Triangulated heightfield over occupied (u,v) cells â€” a real fitted surface
    that follows the cloud, not a bounding box. ``side`` picks the per-cell height:
    'top' = max z (deck top), 'bottom' = min z (soffit / deck underside), 'mid' =
    mean z (road/ground). Only fully-occupied quads become faces, so genuine holes
    in the scan stay open (useful QA). Returns world verts, faces, coverage %, and
    the RMS deviation from the best-fit plane (a smoothing/outlier metric)."""
    u0, u1 = float(u.min()), float(u.max()); v0, v1 = float(v.min()), float(v.max())
    span = max(u1 - u0, v1 - v0) or 1.0
    cell = span / ncell
    gu = max(2, int(np.ceil((u1 - u0) / cell)) + 1)
    gv = max(2, int(np.ceil((v1 - v0) / cell)) + 1)
    ci = np.clip(((u - u0) / cell).astype(int), 0, gu - 1)
    cj = np.clip(((v - v0) / cell).astype(int), 0, gv - 1)
    flat = ci * gv + cj; ntot = gu * gv
    cnt = np.bincount(flat, minlength=ntot)
    occ = cnt > 0
    if side == "top":
        zc = np.full(ntot, -np.inf); np.maximum.at(zc, flat, z)
    elif side == "bottom":
        zc = np.full(ntot, np.inf); np.minimum.at(zc, flat, z)
    else:
        zs = np.bincount(flat, weights=z, minlength=ntot)
        zc = np.where(cnt > 0, zs / np.maximum(cnt, 1), 0.0)
    occ_idx = np.where(occ)[0]
    vid = -np.ones(ntot, int); vid[occ_idx] = np.arange(len(occ_idx))
    ii = occ_idx // gv; jj = occ_idx % gv
    uu = u0 + (ii + 0.5) * cell; vv = v0 + (jj + 0.5) * cell
    xw = cxy[0] + uu * u_ax[0] + vv * v_ax[0]
    yw = cxy[1] + uu * u_ax[1] + vv * v_ax[1]
    verts = np.column_stack([xw, yw, zc[occ_idx]]).round(3)
    faces: List[List[int]] = []
    for a in occ_idx.tolist():
        i, j = divmod(a, gv)
        b, c, d = a + gv, a + 1, a + gv + 1
        if i + 1 < gu and j + 1 < gv and occ[b] and occ[c] and occ[d]:
            faces.append([int(vid[a]), int(vid[b]), int(vid[d])])
            faces.append([int(vid[a]), int(vid[d]), int(vid[c])])
    _, rms = _fit_plane(np.column_stack([u, v, z]))
    return {"vertices": verts.tolist(), "faces": faces,
            "coverage_pct": round(100.0 * occ.mean(), 1), "fit_rms_m": round(rms, 3)}


def _slab_mesh(u, v, z, cxy, u_ax, v_ax, ncell=64) -> Dict[str, Any]:
    """CLOSED slab: per-cell top + bottom heightfields over one shared occupancy
    grid, stitched with side walls along the boundary edges â€” a watertight tight
    volume that hugs the deck (no see-through gap between the two sheets)."""
    u0, u1 = float(u.min()), float(u.max()); v0, v1 = float(v.min()), float(v.max())
    span = max(u1 - u0, v1 - v0) or 1.0
    cell = span / ncell
    gu = max(2, int(np.ceil((u1 - u0) / cell)) + 1)
    gv = max(2, int(np.ceil((v1 - v0) / cell)) + 1)
    ci = np.clip(((u - u0) / cell).astype(int), 0, gu - 1)
    cj = np.clip(((v - v0) / cell).astype(int), 0, gv - 1)
    flat = ci * gv + cj; ntot = gu * gv
    cnt = np.bincount(flat, minlength=ntot)
    occ = cnt > 0
    ztop = np.full(ntot, -np.inf); np.maximum.at(ztop, flat, z)
    zbot = np.full(ntot, np.inf); np.minimum.at(zbot, flat, z)
    occ_idx = np.where(occ)[0]
    vid = -np.ones(ntot, int); vid[occ_idx] = np.arange(len(occ_idx))
    ii = occ_idx // gv; jj = occ_idx % gv
    uu = u0 + (ii + 0.5) * cell; vv = v0 + (jj + 0.5) * cell
    xw = cxy[0] + uu * u_ax[0] + vv * v_ax[0]
    yw = cxy[1] + uu * u_ax[1] + vv * v_ax[1]
    nvt = len(occ_idx)
    verts = np.vstack([np.column_stack([xw, yw, ztop[occ_idx]]),
                       np.column_stack([xw, yw, zbot[occ_idx]])]).round(3)
    faces: List[List[int]] = []
    edge_cnt: Dict[Tuple[int, int], int] = {}
    for a in occ_idx.tolist():
        i, j = divmod(a, gv)
        b, c, d = a + gv, a + 1, a + gv + 1
        if i + 1 < gu and j + 1 < gv and occ[b] and occ[c] and occ[d]:
            va, vb, vc, vd = int(vid[a]), int(vid[b]), int(vid[c]), int(vid[d])
            faces.append([va, vb, vd]); faces.append([va, vd, vc])          # top
            faces.append([va + nvt, vd + nvt, vb + nvt])                    # bottom (flipped)
            faces.append([va + nvt, vc + nvt, vd + nvt])
            for e in ((va, vb), (vb, vd), (vd, vc), (vc, va), (va, vd)):
                k = (min(e), max(e)); edge_cnt[k] = edge_cnt.get(k, 0) + 1
    # boundary edges (appear in exactly one top quad) â†’ vertical side walls
    for (p, q), c2 in edge_cnt.items():
        if c2 == 1:
            faces.append([p, q, q + nvt]); faces.append([p, q + nvt, p + nvt])
    _, rms = _fit_plane(np.column_stack([u, v, z]))
    return {"vertices": verts.tolist(), "faces": faces,
            "coverage_pct": round(100.0 * occ.mean(), 1), "fit_rms_m": round(rms, 3)}


def _hull_poly(u: np.ndarray, v: np.ndarray, tol: float = 0.08) -> np.ndarray:
    """Simplified convex-hull polygon of the (u,v) footprint â€” near-collinear
    vertices are dropped so edges come out straight (CAD-style), not jittery."""
    uv = np.column_stack([u, v])
    try:
        from scipy.spatial import ConvexHull  # type: ignore
        poly = uv[ConvexHull(uv).vertices]
    except Exception:
        poly = np.array([[u.min(), v.min()], [u.max(), v.min()],
                         [u.max(), v.max()], [u.min(), v.max()]])
    m = len(poly)
    keep = []
    for i in range(m):
        a, b, c = poly[i - 1], poly[i], poly[(i + 1) % m]
        ac = c - a
        d = abs((b - a)[0] * ac[1] - (b - a)[1] * ac[0]) / (float(np.hypot(*ac)) + 1e-9)
        if d > tol:
            keep.append(i)
    return poly[keep] if len(keep) >= 3 else poly


def _poly_solid(poly: np.ndarray, coef_lo, coef_hi, cxy, u_ax, v_ax) -> Dict[str, Any]:
    """Idealised CAD-style solid: a convex polygon extruded between two fitted
    planes (z = aÂ·u + bÂ·v + c each). Every face is planar and every edge straight â€”
    this is the display volume; measurements come from the fitted planes."""
    poly = np.asarray(poly, np.float64)
    m = len(poly)
    zlo = _plane_z(np.asarray(coef_lo, np.float64), poly)
    zhi = _plane_z(np.asarray(coef_hi, np.float64), poly)
    verts: List[List[float]] = []
    for zz in (zlo, zhi):
        for k in range(m):
            pu, pv = poly[k]
            verts.append([round(float(cxy[0] + pu * u_ax[0] + pv * v_ax[0]), 3),
                          round(float(cxy[1] + pu * u_ax[1] + pv * v_ax[1]), 3),
                          round(float(zz[k]), 3)])
    faces: List[List[int]] = []
    for i in range(m):                                   # side walls
        j = (i + 1) % m
        faces.append([i, j, m + j]); faces.append([i, m + j, m + i])
    for i in range(1, m - 1):                            # bottom + top caps
        faces.append([0, i + 1, i]); faces.append([m, m + i, m + i + 1])
    return {"vertices": verts, "faces": faces}


def _station_series(u_pts, u1, u2, K, sample):
    """Per-station robust values along the span + gap fill + light smoothing â€”
    produces the clean profile a CAD loft would use."""
    st = np.linspace(u1, u2, K + 1)
    half = (st[1] - st[0]) * 0.75 if K > 0 else (u2 - u1)
    out = []
    for us in st:
        m = np.abs(u_pts - us) <= half
        if int(m.sum()) < 12:
            m = np.abs(u_pts - us) <= half * 2.5
        out.append(sample(m) if int(m.sum()) >= 4 else None)
    valid = [o for o in out if o is not None]
    if not valid:                                        # degenerate: sample everything once
        out = [sample(np.ones(len(u_pts), bool))] * (K + 1)
        valid = out
    cols = len(valid[0])
    arrs = [np.array([o[c] if o is not None else np.nan for o in out]) for c in range(cols)]
    for arr in arrs:
        idx = np.where(~np.isnan(arr))[0]
        if idx.size == 0:
            arr[:] = 0.0
            continue
        arr[:] = np.interp(np.arange(K + 1), idx, arr[idx])
        if K >= 2:
            arr[1:-1] = (arr[:-2] + 2.0 * arr[1:-1] + arr[2:]) / 4.0
    return st, arrs


def _loft_solid(st, sections, cxy, u_ax, v_ax) -> Dict[str, Any]:
    """CAD-style lofted solid: an m-cornered cross-section swept along the span
    stations. sections[k] = list of (v, z) corners (same m at every station,
    counter-clockwise). Side walls between stations + end caps â€” planar facets,
    straight edges, follows curvature and skew like a CAD sweep."""
    K = len(st) - 1
    m = len(sections[0])
    verts: List[List[float]] = []
    for k in range(K + 1):
        for (pv, pz) in sections[k]:
            verts.append([round(float(cxy[0] + st[k] * u_ax[0] + pv * v_ax[0]), 3),
                          round(float(cxy[1] + st[k] * u_ax[1] + pv * v_ax[1]), 3),
                          round(float(pz), 3)])
    faces: List[List[int]] = []
    for k in range(K):
        a0, b0 = k * m, (k + 1) * m
        for c in range(m):
            c2 = (c + 1) % m
            faces.append([a0 + c, b0 + c, b0 + c2])
            faces.append([a0 + c, b0 + c2, a0 + c2])
    for i in range(1, m - 1):                            # end caps
        faces.append([0, i, i + 1])
        faces.append([K * m, K * m + i + 1, K * m + i])
    return {"vertices": verts, "faces": faces}


def _deck_loft(u_pts, v_pts, z_pts, cxy, u_ax, v_ax):
    """Idealised deck: rectangular section (soffitâ†’top) lofted along the span.
    Returns (mesh, st, z_soffit_series, rms_to_soffit) â€” the soffit series is
    reused to top-out the piers and as the fit-quality (smoothing/outlier) QA."""
    u1, u2 = float(u_pts.min()), float(u_pts.max())
    K = int(np.clip(round((u2 - u1) / 8.0), 2, 24))

    def _dense_extent(vv):
        """Transverse extent of the DENSE mass at this station â€” the deck itself.
        Raw percentiles get inflated by sparse trees/gantries at the edges; the
        largest contiguous run of well-populated 0.5 m bins does not."""
        lo, hi = float(vv.min()), float(vv.max())
        nb = max(4, int(np.ceil((hi - lo) / 0.5)))
        cnt, edges = np.histogram(vv, bins=nb, range=(lo, hi))
        thr = max(3.0, 0.08 * float(cnt.max()))
        good = cnt >= thr
        best_s = best_e = cur_s = -1; best_len = 0
        for i, g in enumerate(good):
            if g and cur_s < 0:
                cur_s = i
            if (not g or i == len(good) - 1) and cur_s >= 0:
                end = i if g else i - 1
                if end - cur_s + 1 > best_len:
                    best_len, best_s, best_e = end - cur_s + 1, cur_s, end
                cur_s = -1
        if best_len <= 0:
            return float(np.percentile(vv, 2)), float(np.percentile(vv, 98))
        return float(edges[best_s]), float(edges[best_e + 1])

    def sample(m):
        vv, zz = v_pts[m], z_pts[m]
        q1, q3 = np.percentile(zz, 25), np.percentile(zz, 75)
        zb = float(np.median(zz[zz <= q1])) if (zz <= q1).any() else float(zz.min())
        zt = float(np.median(zz[zz >= q3])) if (zz >= q3).any() else float(zz.max())
        vlo, vhi = _dense_extent(vv)
        return (vlo, vhi, zb, zt)

    st, (vlo, vhi, zbt, ztp) = _station_series(u_pts, u1, u2, K, sample)
    ztp = np.maximum(ztp, zbt + 0.2)                     # never degenerate
    sections = [[(vlo[k], zbt[k]), (vhi[k], zbt[k]), (vhi[k], ztp[k]), (vlo[k], ztp[k])]
                for k in range(K + 1)]
    mesh = _loft_solid(st, sections, cxy, u_ax, v_ax)
    # QA: how well the swept soffit matches the actual underside returns
    soff_pts = z_pts <= np.percentile(z_pts, 25)
    resid = z_pts[soff_pts] - np.interp(u_pts[soff_pts], st, zbt)
    rms = float(np.sqrt(np.mean(resid ** 2))) if soff_pts.any() else 0.0
    return mesh, st, zbt, round(rms, 3)


def _ribbon_loft(u_pts, v_pts, z_pts, cxy, u_ax, v_ax, thickness=0.15):
    """Idealised under-bridge surface: a thin plate lofted along the span (follows
    the road/ground curvature but with straight CAD edges)."""
    u1, u2 = float(u_pts.min()), float(u_pts.max())
    K = int(np.clip(round((u2 - u1) / 8.0), 2, 24))

    def sample(m):
        vv, zz = v_pts[m], z_pts[m]
        return (float(np.percentile(vv, 2)), float(np.percentile(vv, 98)),
                float(np.median(zz)))

    st, (vlo, vhi, zmd) = _station_series(u_pts, u1, u2, K, sample)
    sections = [[(vlo[k], zmd[k] - thickness), (vhi[k], zmd[k] - thickness),
                 (vhi[k], zmd[k]), (vlo[k], zmd[k])] for k in range(K + 1)]
    mesh = _loft_solid(st, sections, cxy, u_ax, v_ax)
    resid = z_pts - np.interp(u_pts, st, zmd)
    return mesh, round(float(np.sqrt(np.mean(resid ** 2))), 3)


def _prism_mesh(u, v, z, cxy, u_ax, v_ax,
                z_bottom: Optional[float] = None, z_top: Optional[float] = None) -> Dict[str, Any]:
    """Vertical prism whose cross-section is the convex hull of the cluster's (u,v)
    points, extruded between its base and top elevations â€” tight to a real pier /
    column, not an axis-aligned box. ``z_bottom``/``z_top`` extend the prism to the
    surfaces it physically connects (ground below, soffit above) so the fitted
    volumes touch instead of floating with air gaps where the scan is occluded.
    Returns world verts, faces, coverage %."""
    z0, z1 = float(np.percentile(z, 1)), float(np.percentile(z, 99))
    if z_bottom is not None:
        z0 = min(z0, float(z_bottom))
    if z_top is not None:
        z1 = max(z1, float(z_top))
    uv = np.column_stack([u, v])
    # Tighten to the dense component core. A few sparse panel/noise points can
    # otherwise inflate the convex hull into a wide, empty CAD box.
    if len(u) >= 30:
        try:
            ctr = uv.mean(axis=0)
            _, ev = np.linalg.eigh((uv - ctr).T @ (uv - ctr))
            a0, a1 = ev[:, 1], ev[:, 0]
            duv = uv - ctr
            s = duv @ a0
            t = duv @ a1
            s0, s1 = np.percentile(s, [3.0, 97.0])
            t0, t1 = np.percentile(t, [5.0, 95.0])
            corners = np.array([[s0, t0], [s1, t0], [s1, t1], [s0, t1]])
            poly = ctr + corners[:, :1] * a0 + corners[:, 1:] * a1
        except Exception:
            poly = None
    else:
        poly = None
    try:
        if poly is None:
            from scipy.spatial import ConvexHull  # type: ignore
            poly = uv[ConvexHull(uv).vertices]
    except Exception:
        poly = np.array([[u.min(), v.min()], [u.max(), v.min()],
                         [u.max(), v.max()], [u.min(), v.max()]])
    m = len(poly)
    verts: List[List[float]] = []
    for zz in (z0, z1):
        for pu, pv in poly:
            verts.append([round(float(cxy[0] + pu * u_ax[0] + pv * v_ax[0]), 3),
                          round(float(cxy[1] + pu * u_ax[1] + pv * v_ax[1]), 3),
                          round(zz, 3)])
    faces: List[List[int]] = []
    for i in range(m):                                   # side walls
        j = (i + 1) % m
        faces.append([i, j, m + j]); faces.append([i, m + j, m + i])
    for i in range(1, m - 1):                            # bottom + top caps
        faces.append([0, i + 1, i]); faces.append([m, m + i, m + i + 1])
    # coverage: fraction of the hull's (u,v) grid cells that hold a point
    G = 24
    gi = np.clip(((u - u.min()) / ((u.max() - u.min()) or 1) * (G - 1)).astype(int), 0, G - 1)
    gj = np.clip(((v - v.min()) / ((v.max() - v.min()) or 1) * (G - 1)).astype(int), 0, G - 1)
    occ = np.zeros((G, G), bool); occ[gi, gj] = True
    return {"vertices": verts, "faces": faces, "coverage_pct": round(100.0 * occ.mean(), 1),
            "fit_rms_m": None}


def _piecewise_prism_mesh(u, v, z, cxy, u_ax, v_ax,
                          z_bottom: Optional[float] = None,
                          z_top: Optional[float] = None,
                          max_segments: int = 10,
                          min_points: int = 80) -> Dict[str, Any]:
    """Represent one logical component as concatenated simple prisms."""
    n = int(len(u))
    if n < max(min_points * 2, 160):
        mesh = _prism_mesh(u, v, z, cxy, u_ax, v_ax, z_bottom, z_top)
        mesh["primitive_count"] = 1
        return mesh

    uv = np.column_stack([u, v])
    ctr = uv.mean(axis=0)
    try:
        _, ev = np.linalg.eigh((uv - ctr).T @ (uv - ctr))
        ax = ev[:, 1]
    except Exception:
        ax = np.array([1.0, 0.0])
    along = (uv - ctr) @ ax
    span = float(np.ptp(along))
    if span <= 1e-6:
        mesh = _prism_mesh(u, v, z, cxy, u_ax, v_ax, z_bottom, z_top)
        mesh["primitive_count"] = 1
        return mesh

    seg_count = int(np.clip(round(span / 3.0), 2, max_segments))
    edges = np.linspace(float(along.min()), float(along.max()), seg_count + 1)
    parts: List[np.ndarray] = []
    for i in range(seg_count):
        lo, hi = edges[i], edges[i + 1]
        m = ((along >= lo) & (along <= hi)) if i == seg_count - 1 else ((along >= lo) & (along < hi))
        if int(m.sum()) >= min_points:
            parts.append(m)

    if len(parts) <= 1:
        mesh = _prism_mesh(u, v, z, cxy, u_ax, v_ax, z_bottom, z_top)
        mesh["primitive_count"] = 1
        return mesh

    vertices: List[List[float]] = []
    faces: List[List[int]] = []
    weighted_coverage = 0.0
    total_pts = 0
    for m in parts:
        sub = _prism_mesh(u[m], v[m], z[m], cxy, u_ax, v_ax, z_bottom, z_top)
        off = len(vertices)
        vertices.extend(sub["vertices"])
        faces.extend([[int(a + off), int(b + off), int(c + off)] for a, b, c in sub["faces"]])
        pc = int(m.sum())
        total_pts += pc
        weighted_coverage += float(sub.get("coverage_pct", 0.0)) * pc

    return {
        "vertices": vertices,
        "faces": faces,
        "coverage_pct": round(weighted_coverage / max(1, total_pts), 1),
        "fit_rms_m": None,
        "primitive_count": len(parts),
        "primitive_type": "piecewise_prism",
    }


# â”€â”€ SAM render-and-lift cleaning â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_SAM_MODEL = None            # cached ultralytics SAM instance (loads once)


def _sam_weights_path() -> Optional[str]:
    env = os.getenv("SAM_WEIGHTS")
    if env and Path(env).exists():
        return env
    backend = Path(__file__).resolve().parents[3]
    for rel in ("mobile_sam.pt", "app/mobile_sam.pt", "sam2.1_b.pt"):
        cand = backend / rel
        if cand.exists():
            return str(cand)
    return None


def _splat_view(vx, vy, depth, height, res: int = 900, r: int = 2):
    """Dense z-buffered orthographic splat of the cloud â€” a solid image SAM can
    segment (scatter renders are too sparse for it). Returns (rgb uint8 image,
    per-pixel point-index map for exact back-projection)."""
    vx = np.asarray(vx, np.float64); vy = np.asarray(vy, np.float64)
    xr = float(vx.max() - vx.min()) or 1.0
    yr = float(vy.max() - vy.min()) or 1.0
    scale = (res - 20) / max(xr, yr)
    W = int(xr * scale) + 20; H = int(yr * scale) + 20
    px = ((vx - vx.min()) * scale + 10).astype(np.int32)
    py = (H - 1 - ((vy - vy.min()) * scale + 10)).astype(np.int32)
    order = np.argsort(depth)[::-1]                      # nearest written last â†’ wins
    t = (height - height.min()) / (float(height.max() - height.min()) or 1.0)
    try:
        from matplotlib import cm
        colors = (cm.turbo(t)[:, :3] * 255).astype(np.uint8)
    except Exception:
        g = (t * 255).astype(np.uint8)
        colors = np.stack([g, 255 - g, np.full_like(g, 128)], 1)
    idx_map = np.full((H, W), -1, np.int32)
    img = np.full((H, W, 3), 245, np.uint8)
    po, xo, yo = order, px[order], py[order]
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            xx = np.clip(xo + dx, 0, W - 1); yy = np.clip(yo + dy, 0, H - 1)
            idx_map[yy, xx] = po
            img[yy, xx] = colors[po]
    return img, idx_map


def _sam_refine_labels(pts: np.ndarray, u: np.ndarray, v: np.ndarray,
                       labels: np.ndarray) -> Tuple[np.ndarray, np.ndarray, bool]:
    """Rigorous cleaning via SAM instance coherence (render-and-lift, the recipe
    published for bridge clouds): splat the cloud from four views, auto-mask each
    with SAM, back-project every mask to its exact 3-D points, and majority-vote
    out points whose SAM instance is dominantly off-structure â€” trees, poles,
    panels and vehicles that bleed INTO the geometric deck/substructure bands get
    removed with their instance instead of surviving on elevation alone.
    Returns (refined labels, removed mask, sam_used)."""
    global _SAM_MODEL
    wpath = _sam_weights_path()
    if wpath is None:
        return labels, np.zeros(len(labels), bool), False
    try:
        from ultralytics import SAM as _SAM  # lazy: torch import is heavy
        if _SAM_MODEL is None:
            _SAM_MODEL = _SAM(wpath)
    except Exception as exc:
        print(f"[analyze_bridge] SAM unavailable: {exc}")
        return labels, np.zeros(len(labels), bool), False

    z = pts[:, 2]
    c = 0.7071
    views = [
        (u, v, -z),                                       # plan (top-down)
        (u, z, v),                                        # elevation (side)
        (v, z, u),                                        # end elevation
        (u, z * c + v * c, v * c - z * c),                # oblique
    ]
    votes = np.zeros(len(labels), np.int16)
    views_ok = 0
    import tempfile
    try:
        from matplotlib import image as _mpimg
    except Exception:
        _mpimg = None
    for vx, vy, vd in views:
        img, idx_map = _splat_view(vx, vy, vd, z)
        try:
            if _mpimg is not None:
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                    fname = tf.name
                _mpimg.imsave(fname, img)
                res = _SAM_MODEL(fname, device=0 if _cuda_ok() else "cpu", verbose=False)
                os.unlink(fname)
            else:
                res = _SAM_MODEL(img[..., ::-1], device=0 if _cuda_ok() else "cpu", verbose=False)
            masks = res[0].masks
            data = masks.data.cpu().numpy().astype(bool) if masks is not None else None
        except Exception as exc:
            print(f"[analyze_bridge] SAM view failed: {exc}")
            if _is_cuda_error(exc):
                _mark_cuda_poisoned(exc)
                break              # dead context â€” the remaining views cannot succeed
            continue
        views_ok += 1
        if data is None:
            continue
        for k in range(data.shape[0]):
            mk = data[k]
            if mk.shape != idx_map.shape:                # ultralytics letterboxing
                sy = mk.shape[0] / idx_map.shape[0]; sx = mk.shape[1] / idx_map.shape[1]
                yy, xx = np.where(idx_map >= 0)
                sel = mk[(yy * sy).astype(int).clip(0, mk.shape[0] - 1),
                         (xx * sx).astype(int).clip(0, mk.shape[1] - 1)]
                pidx = np.unique(idx_map[yy[sel], xx[sel]])
            else:
                pidx = np.unique(idx_map[mk])
                pidx = pidx[pidx >= 0]
            if pidx.size < 50:
                continue
            off_frac = float((labels[pidx] == _C_OFF).mean())
            if off_frac >= 0.85:                          # high confidence: one view is enough
                votes[pidx[labels[pidx] != _C_OFF]] += 2
            elif off_frac >= 0.60 and pidx.size < max(3000, int(0.02 * len(labels))):
                votes[pidx[labels[pidx] != _C_OFF]] += 1
    removed = votes >= 2                                  # agreed by â‰¥2 views
    out = labels.copy()
    out[removed] = _C_OFF
    # Honest flag: True only when at least one view actually ran â€” a loaded model
    # whose every inference failed (e.g. after a driver reset) cleaned nothing.
    return out, removed, views_ok > 0


def _prune_label_fragments(labels: np.ndarray, u: np.ndarray, v: np.ndarray,
                           *, grid: int = 72) -> Tuple[np.ndarray, int]:
    """Remove small disconnected label islands before fitting volumes.

    SAM/VLM cleaning is intentionally stronger on fragments than on the main deck
    or main under-surface. This keeps tiny approach strips, poles, signs and
    partial faces from biasing lofts and pier clustering while preserving large
    connected structural/surface regions.
    """
    out = labels.copy()
    removed = 0
    for cls, frac, cap, floor in (
        (_C_DECK, 0.025, 3500, 180),
        (_C_ROAD, 0.030, 3000, 180),
        (_C_SUB,  0.020, 1800, 120),
    ):
        idx = np.where(out == cls)[0]
        if idx.size < floor * 2:
            continue
        uu, vv = u[idx], v[idx]
        du = float(uu.max() - uu.min()) or 1.0
        dv = float(vv.max() - vv.min()) or 1.0
        # Never grid finer than the point density (â‰¥ ~3 pts per occupied cell on
        # average) â€” a fixed fine grid disconnects genuinely-connected surfaces
        # on sparse/decimated clouds and shreds them into "fragments".
        g = max(12, min(grid, int(np.sqrt(idx.size / 3.0))))
        gi = np.clip(((uu - uu.min()) / du * (g - 1)).astype(int), 0, g - 1)
        gj = np.clip(((vv - vv.min()) / dv * (g - 1)).astype(int), 0, g - 1)
        occ = np.zeros((g, g), bool)
        occ[gi, gj] = True
        lab, ncomp = _label_grid(occ)
        if ncomp <= 1:
            continue
        point_comp = lab[gi, gj]
        sizes = np.bincount(point_comp, minlength=ncomp + 1)
        if len(sizes) <= 1:
            continue
        main = int(np.argmax(sizes[1:])) + 1
        small_limit = max(floor, min(cap, int(frac * idx.size)))
        drop_comp = np.zeros(ncomp + 1, bool)
        for comp_id in range(1, ncomp + 1):
            if comp_id == main:
                continue
            sz = int(sizes[comp_id])
            if sz <= small_limit:
                drop_comp[comp_id] = True
        if drop_comp.any():
            drop_idx = idx[drop_comp[point_comp]]
            out[drop_idx] = _C_OFF
            removed += int(drop_idx.size)
    return out, removed


def _remove_detached_panels(labels: np.ndarray, u: np.ndarray, v: np.ndarray,
                            deck_mask: np.ndarray, *, grid: int = 96) -> Tuple[np.ndarray, int]:
    """Drop long disconnected panels that sit away from the deck body.

    Real side walls/abutments usually touch or closely flank the deck footprint in
    plan view. Detached construction panels, fences, or scan strips form separate
    islands with a clear grid-cell gap from the deck. This removes those islands
    before volume fitting so they cannot become "unknown bridge structure".
    """
    out = labels.copy()
    candidates = np.isin(out, [_C_SUB, _C_GIRDER, _C_PARAPET, _C_UNKNOWN])
    idx = np.where(candidates)[0]
    deck_idx = np.where(deck_mask)[0]
    if idx.size < 80 or deck_idx.size < 80:
        return out, 0

    if _HAS_SCIPY and _cKDTree is not None:
        from scipy.sparse import coo_matrix  # type: ignore
        from scipy.sparse.csgraph import connected_components  # type: ignore

        uv_idx = np.column_stack([u[idx], v[idx]])
        span = max(float(np.ptp(u[deck_idx])), float(np.ptp(v[deck_idx])), 1.0)
        eps = max(0.18, span / 80.0)
        tree = _cKDTree(uv_idx)
        pairs = tree.query_pairs(eps, output_type="ndarray")
        if pairs.size:
            rows = np.concatenate([pairs[:, 0], pairs[:, 1], np.arange(len(idx))])
            cols = np.concatenate([pairs[:, 1], pairs[:, 0], np.arange(len(idx))])
        else:
            rows = cols = np.arange(len(idx))
        graph = coo_matrix((np.ones(len(rows), dtype=np.uint8), (rows, cols)), shape=(len(idx), len(idx)))
        ncomp, comp_lab = connected_components(graph, directed=False)

        deck_tree = _cKDTree(np.column_stack([u[deck_idx], v[deck_idx]]))
        removed = 0
        for comp_id in range(ncomp):
            part_local = np.where(comp_lab == comp_id)[0]
            if part_local.size < 120:
                continue
            part = idx[part_local]
            d, _ = deck_tree.query(np.column_stack([u[part], v[part]]), k=1, workers=KDT_WORKERS)
            touches_deck = bool(np.nanmin(d) <= eps * 3.0)
            if not touches_deck:
                out[part] = _C_OFF
                removed += int(part.size)
        return out, removed

    uu_all = np.concatenate([u[idx], u[deck_idx]])
    vv_all = np.concatenate([v[idx], v[deck_idx]])
    u0, u1 = float(uu_all.min()), float(uu_all.max())
    v0, v1 = float(vv_all.min()), float(vv_all.max())
    du = float(u1 - u0) or 1.0
    dv = float(v1 - v0) or 1.0

    def cells(point_idx: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        gi = np.clip(((u[point_idx] - u0) / du * (grid - 1)).astype(int), 0, grid - 1)
        gj = np.clip(((v[point_idx] - v0) / dv * (grid - 1)).astype(int), 0, grid - 1)
        return gi, gj

    di, dj = cells(deck_idx)
    deck_occ = np.zeros((grid, grid), bool)
    deck_occ[di, dj] = True
    deck_near = deck_occ.copy()
    for _ in range(3):
        prev = deck_near.copy()
        deck_near[1:, :] |= prev[:-1, :]; deck_near[:-1, :] |= prev[1:, :]
        deck_near[:, 1:] |= prev[:, :-1]; deck_near[:, :-1] |= prev[:, 1:]

    ci, cj = cells(idx)
    occ = np.zeros((grid, grid), bool)
    occ[ci, cj] = True
    lab, ncomp = _label_grid(occ)
    point_comp = lab[ci, cj]
    removed = 0
    for comp_id in range(1, ncomp + 1):
        part = idx[point_comp == comp_id]
        if part.size < 120:
            continue
        pi, pj = cells(part)
        touches_deck = bool(deck_near[pi, pj].any())
        if not touches_deck:
            out[part] = _C_OFF
            removed += int(part.size)
    return out, removed


def _promote_column_cells(labels: np.ndarray, sub_cand: np.ndarray,
                          u: np.ndarray, v: np.ndarray, z: np.ndarray,
                          road_surface: float, soffit0: float,
                          *, cell: float = 0.8) -> None:
    """Promote pier/wall columns to _C_SUB by plan-view cell occupancy in-place.

    Local-normal verticality misses most of a rough concrete shaft (scan noise â†’
    ~10-20%% pass rate), which dumps real piers into the unknown catch-all. A
    column is unmistakable in plan view instead: its cell is filled over most of
    the roadâ†’soffit height. Mirrors the per-cell vertical-extent detector that
    the standalone segmenter uses."""
    gap = float(soffit0 - road_surface) if road_surface is not None else 0.0
    if gap < 1.0:
        return
    cand_idx = np.where(sub_cand)[0]
    if cand_idx.size < 50:
        return
    ki = np.floor(u[cand_idx] / cell).astype(np.int64)
    kj = np.floor(v[cand_idx] / cell).astype(np.int64)
    key = (ki - ki.min()) * (kj.max() - kj.min() + 1) + (kj - kj.min())
    for grp in _cell_groups(key):
        if grp.size < 25:
            continue
        zg = z[cand_idx[grp]]
        z5, z95 = np.percentile(zg, [5.0, 95.0])
        if (z95 - z5) >= 0.45 * gap and z95 >= soffit0 - 0.35 * gap:
            labels[cand_idx[grp]] = _C_SUB


def _label_uv_clusters(mask: np.ndarray, u: np.ndarray, v: np.ndarray,
                       *, grid: int = 60, min_points: int = 150,
                       max_clusters: int = 12) -> List[np.ndarray]:
    """Cluster an analysis label in bridge-frame plan view and return point masks.

    This is deliberately geometry-first: Qwen may name the pieces, but the clusters
    are measured from the scan. Small pieces are kept in structural mode because
    railings, bearings, diaphragms and girder strips can be real bridge parts.
    """
    idx = np.where(mask)[0]
    if idx.size < min_points:
        return []
    uu, vv = u[idx], v[idx]
    du = float(uu.max() - uu.min()) or 1.0
    dv = float(vv.max() - vv.min()) or 1.0
    # Density-aware grid (like _prune_label_fragments): finer than the point
    # spacing would split one connected member into many bogus clusters.
    g = max(12, min(grid, int(np.sqrt(idx.size / 3.0))))
    gi = np.clip(((uu - uu.min()) / du * (g - 1)).astype(int), 0, g - 1)
    gj = np.clip(((vv - vv.min()) / dv * (g - 1)).astype(int), 0, g - 1)
    occ = np.zeros((g, g), bool)
    occ[gi, gj] = True
    lab, ncomp = _label_grid(occ)
    comps: List[Tuple[int, np.ndarray]] = []
    point_comp = lab[gi, gj]
    for comp_id in range(1, ncomp + 1):
        part_idx = idx[point_comp == comp_id]
        if part_idx.size >= min_points:
            fm = np.zeros(len(mask), bool)
            fm[part_idx] = True
            comps.append((int(part_idx.size), fm))
    comps.sort(key=lambda x: x[0], reverse=True)
    return [fm for _, fm in comps[:max_clusters]]


# â”€â”€ Kind-shaped templates (scan-to-BIM style) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Each template fits the parametric shape its element KIND implies (cylinder
# pier, I/box girder sweep, parapet wall) and returns None unless the data
# clearly supports it â€” the generic prism stays as the honest fallback. Gates
# err toward fallback: a drive-by scan that saw only a girder's bottom flange
# must not grow an invented web.

def _fit_plan_circle(pu: np.ndarray, pv: np.ndarray):
    """KÃ¥sa algebraic circle fit on plan points â†’ (cu, cv, r, rms) or None."""
    try:
        A = np.column_stack([pu, pv, np.ones(len(pu))])
        sol, *_ = np.linalg.lstsq(A, pu ** 2 + pv ** 2, rcond=None)
    except Exception:
        return None
    cu, cv = float(sol[0] / 2), float(sol[1] / 2)
    r2 = float(sol[2]) + cu ** 2 + cv ** 2
    if r2 <= 0:
        return None
    r = float(np.sqrt(r2))
    rms = float(np.sqrt(np.mean((np.hypot(pu - cu, pv - cv) - r) ** 2)))
    return cu, cv, r, rms


def _plan_rect_rms(pu: np.ndarray, pv: np.ndarray) -> float:
    """RMS distance of plan points to the OUTLINE of the PCA-aligned rectangle
    at robust extents â€” the competing hypothesis to a circular column (a scan
    sees a pier's surface, so plan points lie on the cross-section boundary)."""
    uv = np.column_stack([pu, pv])
    ctr = uv.mean(axis=0)
    _, ev = np.linalg.eigh((uv - ctr).T @ (uv - ctr))
    s = (uv - ctr) @ ev[:, 1]
    t = (uv - ctr) @ ev[:, 0]
    a = max(float(np.percentile(np.abs(s), 92)), 0.05)
    b = max(float(np.percentile(np.abs(t), 92)), 0.05)
    d = np.minimum(np.abs(np.abs(s) - a), np.abs(np.abs(t) - b))
    return float(np.sqrt(np.mean(d ** 2)))


def _cylinder_mesh(cu: float, cv: float, r: float, zb: float, zt: float,
                   cxy, u_ax, v_ax, n: int = 28) -> Dict[str, Any]:
    ang = np.linspace(0, 2 * np.pi, n, endpoint=False)
    ring = np.column_stack([cu + r * np.cos(ang), cv + r * np.sin(ang)])
    verts: List[List[float]] = []
    for zz in (zb, zt):
        for pu, pv in ring:
            verts.append([round(float(cxy[0] + pu * u_ax[0] + pv * v_ax[0]), 3),
                          round(float(cxy[1] + pu * u_ax[1] + pv * v_ax[1]), 3),
                          round(float(zz), 3)])
    faces: List[List[int]] = []
    for i in range(n):
        j = (i + 1) % n
        faces.append([i, j, n + j]); faces.append([i, n + j, n + i])
    for i in range(1, n - 1):
        faces.append([0, i + 1, i]); faces.append([n, n + i, n + i + 1])
    return {"vertices": verts, "faces": faces}


def _pier_template_mesh(u, v, z, cxy, u_ax, v_ax,
                        z_bottom: Optional[float] = None,
                        z_top: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Circular columns become true cylinders â€” selected only when the circle
    hypothesis is tight AND clearly beats the rectangle. Everything else falls
    back to the piecewise prism (returns None)."""
    if len(u) < 120:
        return None
    circ = _fit_plan_circle(u, v)
    if not circ:
        return None
    cu, cv, r, crms = circ
    if not (0.2 <= r <= 3.0) or crms > 0.09 or crms >= 0.75 * _plan_rect_rms(u, v):
        return None
    zb, zt = float(np.percentile(z, 1)), float(np.percentile(z, 99))
    if z_bottom is not None:
        zb = min(zb, float(z_bottom))
    if z_top is not None:
        zt = max(zt, float(z_top))
    mesh = _cylinder_mesh(cu, cv, r, zb, zt, cxy, u_ax, v_ax)
    mesh["fit_rms_m"] = round(crms, 3)
    mesh["primitive_type"] = "cylinder"
    mesh["primitive_count"] = 1
    return mesh


def _girder_profile_mesh(u, v, z, cxy, u_ax, v_ax) -> Optional[Dict[str, Any]]:
    """I / box girder: find top+bottom flange bands in the grade-corrected z
    distribution, measure flange/web widths, sweep the profile along the span.
    Requires BOTH flanges clearly present and a tight profile fit."""
    n = len(u)
    if n < 600:
        return None
    u1, u2 = _robust_extent(u)
    if u2 - u1 < 4.0:
        return None
    K = int(np.clip(round((u2 - u1) / 6.0), 2, 20))
    st, (vc, zc) = _station_series(u, u1, u2, K,
                                   lambda m: (float(np.median(v[m])), float(np.median(z[m]))))
    dv = v - np.interp(u, st, vc)              # grade/skew-corrected section coords
    dz = z - np.interp(u, st, zc)
    zspan = float(np.percentile(dz, 99) - np.percentile(dz, 1))
    if not (0.35 <= zspan <= 4.0):
        return None
    nb = max(9, int(zspan / 0.05))
    h, e = np.histogram(dz, bins=nb)
    third = nb // 3
    bi = int(np.argmax(h[:third]))
    ti = int(2 * third + np.argmax(h[2 * third:]))
    bz, tz = 0.5 * (e[bi] + e[bi + 1]), 0.5 * (e[ti] + e[ti + 1])
    if tz - bz < 0.3:
        return None
    bot = np.abs(dz - bz) <= 0.10
    top = np.abs(dz - tz) <= 0.10
    if bot.sum() < 0.08 * n or top.sum() < 0.08 * n:  # both flanges must be SEEN
        return None

    def _w(mask, pct=90.0):
        vv = dv[mask] - float(np.median(dv[mask]))
        return 2.0 * float(np.percentile(np.abs(vv), pct))

    bw, tw = max(_w(bot), 0.15), max(_w(top), 0.15)
    mid = (dz > bz + 0.15) & (dz < tz - 0.15)
    ww = max(_w(mid, 80.0) if mid.sum() >= 30 else 0.3 * min(bw, tw), 0.04)
    tfb = float(np.clip(2 * np.percentile(np.abs(dz[bot] - bz), 90), 0.08, 0.4))
    tft = float(np.clip(2 * np.percentile(np.abs(dz[top] - tz), 90), 0.08, 0.4))
    if ww >= 0.6 * min(bw, tw):                # wide "web" â†’ closed box girder
        hb = max(bw, tw) / 2
        prof = [(-hb, bz - tfb / 2), (hb, bz - tfb / 2),
                (hb, tz + tft / 2), (-hb, tz + tft / 2)]
        ptype = "box_girder"
    else:
        hb, ht, hw = bw / 2, tw / 2, ww / 2
        zb0, zb1 = bz - tfb / 2, bz + tfb / 2
        zt0, zt1 = tz - tft / 2, tz + tft / 2
        prof = [(-hb, zb0), (hb, zb0), (hb, zb1), (hw, zb1), (hw, zt0), (ht, zt0),
                (ht, zt1), (-ht, zt1), (-ht, zt0), (-hw, zt0), (-hw, zb1), (-hb, zb1)]
        ptype = "i_girder"

    # profile fit quality: RMS distance of every point to the section outline
    P = np.column_stack([dv, dz])
    d2min = np.full(n, np.inf)
    for a, b_ in zip(prof, prof[1:] + prof[:1]):
        a = np.asarray(a, float); ab = np.asarray(b_, float) - a
        L2 = float(ab @ ab) or 1e-9
        t = np.clip(((P - a) @ ab) / L2, 0.0, 1.0)
        dd = P - (a + t[:, None] * ab[None, :])
        d2min = np.minimum(d2min, np.einsum("ij,ij->i", dd, dd))
    rms = float(np.sqrt(np.mean(d2min)))
    if rms > 0.22:                             # profile does not actually fit â†’ prism
        return None
    sections = [[(vc[k] + pv, zc[k] + pz) for pv, pz in prof] for k in range(K + 1)]
    mesh = _loft_solid(st, sections, cxy, u_ax, v_ax)
    mesh["fit_rms_m"] = round(rms, 3)
    mesh["primitive_type"] = ptype
    mesh["primitive_count"] = 1
    return mesh


def _wall_mesh(u, v, z, cxy, u_ax, v_ax) -> Optional[Dict[str, Any]]:
    """Parapet/railing as a thin vertical wall swept along the span â€” a
    per-station centreline with robust thickness/height instead of a blocky
    prism. None (â†’ prism) when the cluster is not actually thin."""
    if len(u) < 200:
        return None
    u1, u2 = _robust_extent(u)
    if u2 - u1 < 3.0:
        return None
    K = int(np.clip(round((u2 - u1) / 6.0), 2, 24))
    st, (vc, zb, zt) = _station_series(u, u1, u2, K, lambda m: (
        float(np.median(v[m])), float(np.percentile(z[m], 5)), float(np.percentile(z[m], 95))))
    dv = v - np.interp(u, st, vc)
    th = max(0.08, 2 * float(np.percentile(np.abs(dv), 88)))
    if th > 0.8:
        return None
    zt = np.maximum(zt, zb + 0.3)
    sections = [[(vc[k] - th / 2, zb[k]), (vc[k] + th / 2, zb[k]),
                 (vc[k] + th / 2, zt[k]), (vc[k] - th / 2, zt[k])] for k in range(K + 1)]
    mesh = _loft_solid(st, sections, cxy, u_ax, v_ax)
    mesh["fit_rms_m"] = round(float(np.sqrt(np.mean(dv ** 2))), 3)
    mesh["primitive_type"] = "wall"
    mesh["primitive_count"] = 1
    return mesh


def _emit_cluster_solids(mask: np.ndarray, label_key: int, key_prefix: str,
                         label_prefix: str, kind: str, color: str,
                         pts: np.ndarray, u: np.ndarray, v: np.ndarray, z: np.ndarray,
                         cxy, u_ax, v_ax, emit,
                         *, min_points: int = 150, max_clusters: int = 12) -> int:
    count = 0
    for fm in _label_uv_clusters(mask, u, v, min_points=min_points, max_clusters=max_clusters):
        count += 1
        mesh = None
        try:  # kind-shaped template first, generic prism as the honest fallback
            if kind == "beam":
                mesh = _girder_profile_mesh(u[fm], v[fm], z[fm], cxy, u_ax, v_ax)
            elif kind == "edge":
                mesh = _wall_mesh(u[fm], v[fm], z[fm], cxy, u_ax, v_ax)
        except Exception as exc:
            print(f"[analyze_bridge] {kind} template fit failed ({exc}) â€” prism fallback")
            mesh = None
        if mesh is None:
            mesh = _piecewise_prism_mesh(u[fm], v[fm], z[fm], cxy, u_ax, v_ax,
                                         max_segments=8, min_points=max(40, min_points // 2))
        _emit = emit(fm, f"{key_prefix}_{count}", f"{label_prefix} #{count}", kind, color, mesh)
        _ = _emit
    return count


_CUDA_POISONED = False


def _is_cuda_error(exc: BaseException) -> bool:
    return "cuda" in str(exc).lower()


def _mark_cuda_poisoned(exc: BaseException) -> None:
    """A CUDA async error (e.g. a WDDM TDR driver reset) is sticky: every later
    CUDA call in this process fails until restart. Record it once and steer the
    pipeline to its CPU/geometry fallbacks instead of hammering a dead context."""
    global _CUDA_POISONED
    if not _CUDA_POISONED:
        _CUDA_POISONED = True
        print(f"[analyze_bridge] CUDA context poisoned ({exc}); GPU stages "
              "disabled until the backend restarts")


def _cuda_ok() -> bool:
    if _CUDA_POISONED:
        return False
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


# â”€â”€ SAM 3 text-prompted concept removal â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_SAM3 = None                 # cached (processor, model); ~45 s to load once

# Concepts that are never part of the bridge or the surface it spans. Each is
# asked by NAME on photo-like shaded renders â€” SAM 3's promptable-concept mode â€”
# so clutter is removed because the model recognised it, not because of where it
# happens to sit in elevation.
_SAM3_REMOVE = ["tree", "vegetation", "bush", "light pole", "utility pole",
                "traffic sign", "vehicle", "truck"]


def _sam3_dir() -> Optional[str]:
    env = os.getenv("SAM3_DIR")
    if env and Path(env).exists():
        return env
    cand = Path(__file__).resolve().parents[3] / "models" / "sam3-hf"
    return str(cand) if (cand / "model.safetensors").exists() else None


def _splat_shaded(vx, vy, depth, res: int = 1000, r: int = 2):
    """Depth-buffered splat â†’ screen-space hillshade. Produces a photo-like grey
    render â€” SAM 3's concept detector finds nothing on false-colour splats but
    works on these."""
    vx = np.asarray(vx, np.float64); vy = np.asarray(vy, np.float64)
    xr = float(vx.max() - vx.min()) or 1.0
    yr = float(vy.max() - vy.min()) or 1.0
    s = (res - 20) / max(xr, yr)
    W = int(xr * s) + 20; H = int(yr * s) + 20
    px = ((vx - vx.min()) * s + 10).astype(np.int32)
    py = (H - 1 - ((vy - vy.min()) * s + 10)).astype(np.int32)
    order = np.argsort(depth)[::-1]
    zbuf = np.full((H, W), np.nan, np.float32)
    idx_map = np.full((H, W), -1, np.int32)
    po, xo, yo = order, px[order], py[order]
    dv = np.asarray(depth, np.float32)[order]
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            xx = np.clip(xo + dx, 0, W - 1); yy = np.clip(yo + dy, 0, H - 1)
            zbuf[yy, xx] = dv; idx_map[yy, xx] = po
    filled = ~np.isnan(zbuf)
    zs = np.where(filled, zbuf, np.nanmedian(zbuf))
    gy, gx = np.gradient(zs.astype(np.float64))
    grads = np.abs(np.concatenate([gx[filled], gy[filled]])) if filled.any() else np.array([1.0])
    scale = 2.0 / (float(np.percentile(grads, 90)) + 1e-6)
    nx, ny = -gx * scale, -gy * scale
    nl = np.sqrt(nx * nx + ny * ny + 1.0)
    L = np.array([0.4, 0.5, 0.75]); L = L / np.linalg.norm(L)
    shade = np.clip((nx * L[0] + ny * L[1] + L[2]) / nl, 0, 1)
    g = (60 + 180 * shade).astype(np.uint8)
    img = np.stack([g, g, g], -1)
    img[~filled] = 235
    return img, idx_map


def _release_gpu_models() -> None:
    """Drop the cached SAM/SAM3 models and free their CUDA memory. Called once the
    SAM stages are done. This remains useful for freeing local GPU memory before
    the hosted Cerebras judgment and for avoiding memory pressure on small hosts.
    Models lazily reload on the next run (~45s for SAM3); set
    BRIDGEGUARD_KEEP_GPU_MODELS=1 to keep them resident instead."""
    global _SAM_MODEL, _SAM3
    if os.getenv("BRIDGEGUARD_KEEP_GPU_MODELS"):
        return
    _SAM_MODEL = None
    _SAM3 = None
    try:
        import gc
        import torch
        gc.collect()
        if torch.cuda.is_available():
            # On a poisoned context these re-raise the sticky error; record it
            # (so the pipeline stops re-touching CUDA) instead of hiding it.
            try:
                torch.cuda.synchronize()
                torch.cuda.empty_cache()
            except Exception as exc:
                _mark_cuda_poisoned(exc)
                print(f"[analyze_bridge] GPU memory NOT freed ({exc}); "
                      "restart the backend to recover VRAM")
    except Exception:
        pass


def _sam3_concept_mask(render_pts: np.ndarray, ur: np.ndarray, vr: np.ndarray,
                       pts: np.ndarray) -> Tuple[np.ndarray, bool]:
    """Ask SAM 3 for the remove-concepts by name on three shaded views and return
    the mask of ``pts`` to remove. Label-free â€” this runs BEFORE the geometric
    frame/bands are computed, so orientation, footprint and elevation bands are
    derived from an already-cleaned cloud. The RENDERS come from a denser
    subsample (``render_pts``): below ~500k points the shaded splats stop looking
    photo-like and SAM 3 finds nothing. High-confidence detections act alone;
    low-confidence ones need a second view to agree."""
    global _SAM3
    d = _sam3_dir()
    if d is None or not _cuda_ok():
        return np.zeros(len(pts), bool), False
    try:
        import torch
        from PIL import Image
        if _SAM3 is None:
            from transformers import Sam3Processor, Sam3Model
            proc = Sam3Processor.from_pretrained(d)
            model = Sam3Model.from_pretrained(d, dtype=torch.bfloat16).to("cuda").eval()
            _SAM3 = (proc, model)
    except Exception as exc:
        print(f"[analyze_bridge] SAM3 unavailable: {exc}")
        return np.zeros(len(pts), bool), False
    proc, model = _SAM3

    zr = render_pts[:, 2]
    c = 0.7071
    views = [(ur, zr, vr), (ur, zr * c + vr * c, vr * c - zr * c), (ur, vr, -zr)]
    votes = np.zeros(len(render_pts), np.float32)
    ok_infer = 0
    aborted = False
    for vx, vy, vd in views:
        img, idx_map = _splat_shaded(vx, vy, vd)
        pil = Image.fromarray(img)
        for prompt in _SAM3_REMOVE:
            try:
                inputs = proc(images=pil, text=prompt, return_tensors="pt").to("cuda")
                with torch.no_grad():
                    out = model(**inputs)
                res = proc.post_process_instance_segmentation(
                    out, threshold=0.25, mask_threshold=0.5,
                    target_sizes=inputs.get("original_sizes").tolist())[0]
                masks = res.get("masks")
                scores = res.get("scores")
                masks_np = ([masks[k].cpu().numpy().astype(bool) for k in range(len(masks))]
                            if masks is not None else [])
            except Exception as exc:
                print(f"[analyze_bridge] SAM3 '{prompt}' failed: {exc}")
                if _is_cuda_error(exc):
                    _mark_cuda_poisoned(exc)
                    aborted = True     # dead context â€” abandon all remaining prompts/views
                    break
                continue
            ok_infer += 1
            for k, mk in enumerate(masks_np):
                if mk.shape != idx_map.shape:
                    continue
                pidx = np.unique(idx_map[mk]); pidx = pidx[pidx >= 0]
                if pidx.size < 50:
                    continue
                sc = float(scores[k]) if scores is not None else 0.3
                votes[pidx] += 1.0 if sc >= 0.35 else 0.5   # confident â†’ one view suffices
        if aborted:
            break
    ok = ok_infer > 0                  # honest: True only if any inference actually ran
    rem_render = votes >= 1.0
    if not rem_render.any() or not (_HAS_SCIPY and _cKDTree is not None):
        return np.zeros(len(pts), bool), ok
    # map render-space detections onto the analysis points by proximity
    tree = _cKDTree(render_pts[rem_render])
    dist, _ = tree.query(pts, k=1, distance_upper_bound=0.3, workers=KDT_WORKERS)
    removed = np.isfinite(dist)
    # sanity cap: a hallucinated concept must not gut the scan
    if removed.sum() > 0.5 * len(pts):
        return np.zeros(len(pts), bool), ok
    return removed, ok


def _vlm_clutter_triage(pts: np.ndarray) -> Tuple[np.ndarray, bool]:
    """Clutter cleaning by GEOMETRY-PROPOSES / VLM-DISPOSES (2026-07-06 design).

    Measured on this corpus: VLMs are unreliable at DRAWING boxes on splat
    renders (run-to-run variance up to deleting the whole bridge) but reliable
    at JUDGING regions proposed to them (every judge verdict in the bake-off was
    correct). So clutter candidates are proposed deterministically â€” connected
    elevated objects above a local ground estimate: trees, poles, vehicles,
    buildings, and the bridge itself â€” rendered colour-coded, and ONE VLM call
    the local vision engine says which objects
    are bridge vs clutter. Runs BEFORE the frame derivation, so a removed tree
    line can no longer hijack the deck-band histogram.

    Guards: the most PLANAR cluster (largest single-z-mode mass) is never
    removable â€” the deck always survives a bad verdict; a verdict removing >85%%
    of the elevated mass is treated as a hallucination and ignored."""
    n = len(pts)
    if n < 5000:
        return np.zeros(n, bool), False
    x, y, z = pts[:, 0], pts[:, 1], pts[:, 2]

    # local ground = per-cell LOW PERCENTILE of cells with enough returns (a raw
    # min chases sub-ground reflection noise and drags the whole ground surface
    # metres down), median-filled from neighbours under canopy
    cell = 1.5
    gi = ((x - x.min()) / cell).astype(np.int64)
    gj = ((y - y.min()) / cell).astype(np.int64)
    GU, GV = int(gi.max()) + 1, int(gj.max()) + 1
    if GU * GV > 4_000_000:
        return np.zeros(n, bool), False
    ground = np.full((GU, GV), np.nan)
    cell_ids = gi * GV + gj
    for grp in _cell_groups(cell_ids):
        if grp.size >= 5:
            cid = int(cell_ids[grp[0]])
            ground[cid // GV, cid % GV] = float(np.percentile(z[grp], 10.0))
    for _ in range(4):                            # fill holes from valid neighbours
        if not np.isnan(ground).any():
            break
        gpad = np.pad(ground, 1, constant_values=np.nan)
        stack = np.stack([gpad[a:a + GU, b:b + GV] for a in range(3) for b in range(3)])
        with np.errstate(all="ignore"):
            filled = np.nanmedian(stack, axis=0)
        ground = np.where(np.isnan(ground), filled, ground)
    gval = ground[gi, gj]
    h = np.where(np.isfinite(gval), z - gval, 0.0)
    elevated = h > 1.2
    if int(elevated.sum()) < 2000:
        return np.zeros(n, bool), False

    # plan-view connected objects of the elevated mass. FIXED ~0.9 m cells (an
    # auto-scaled fine grid connects everything on dense scans) + one erosion
    # round to cut thin canopy-overhang links between trees and the structure;
    # eroded-edge points stay unproposed (kept â€” conservative).
    idx = np.where(elevated)[0]
    xx, yy = x[idx], y[idx]
    ocell = 0.9
    ci = ((xx - xx.min()) / ocell).astype(np.int64)
    cj = ((yy - yy.min()) / ocell).astype(np.int64)
    OU, OV = int(ci.max()) + 1, int(cj.max()) + 1
    if OU * OV > 4_000_000:
        return np.zeros(n, bool), False
    occ = np.zeros((OU, OV), bool)
    occ[ci, cj] = True
    ero = occ.copy()
    prev = ero.copy()
    ero[1:, :] &= prev[:-1, :]; ero[:-1, :] &= prev[1:, :]
    ero[:, 1:] &= prev[:, :-1]; ero[:, :-1] &= prev[:, 1:]
    ero[0, :] = False; ero[-1, :] = False; ero[:, 0] = False; ero[:, -1] = False
    use = ero if ero.sum() >= 12 else occ
    lab, ncomp = _label_grid(use)
    point_comp = lab[ci, cj]                      # 0 for eroded-away points
    sizes = np.bincount(point_comp, minlength=ncomp + 1)
    order = [cid for cid in np.argsort(sizes)[::-1] if cid != 0 and sizes[cid] >= 400]
    order = order[:len(_RENDER_PALETTE)]         # one distinct colour per object
    if len(order) < 2:
        return np.zeros(n, bool), False          # one object = the bridge itself

    comp_ids = np.zeros(n, np.int64)
    render_classes: List[Dict[str, Any]] = []
    stats: List[str] = []
    planar_ratio = {}
    for k, cid in enumerate(order, start=1):
        pidx = idx[point_comp == cid]
        comp_ids[pidx] = k
        hexc = _RENDER_PALETTE[k - 1]
        render_classes.append({"id": k, "name": f"object {k}", "color_hex": hexc,
                               "color_rgb": [int(hexc[i:i + 2], 16) / 255.0 for i in (1, 3, 5)],
                               "count": int(pidx.size)})
        zc = z[pidx]
        hist, _edges = np.histogram(zc, bins=max(4, int(np.ptp(zc) / 0.5) + 1))
        # a deck slab concentrates its z mass in one 0.5 m bin; diffuse canopy
        # cannot â€” this is the absolute deck signature, not a relative rank
        # (the most planar object in an all-trees scene is still a tree)
        planar_ratio[k] = float(hist.max()) / max(1, pidx.size)
        stats.append(f"- id {k} (colour {hexc}): {pidx.size:,} pts, plan extent "
                     f"{np.ptp(x[pidx]):.0f} x {np.ptp(y[pidx]):.0f} m, "
                     f"height {zc.min():.1f}..{zc.max():.1f}")

    imgs = _render_segment_views(pts, comp_ids, render_classes)
    if not imgs:
        return np.zeros(n, bool), False
    system = (
        "You are the clutter-triage stage of an automated bridge point-cloud "
        "pipeline. The scan contains ONE bridge plus surroundings. Geometry has "
        "split the ELEVATED objects in the scene into candidate objects, each "
        "shown in one colour (grey = ground / unassigned, always kept). Your job: "
        "decide which coloured objects are part of the bridge (deck, "
        "superstructure, girders, piers, abutments, parapets) and which are NOT "
        "bridge (trees/vegetation, poles, sign gantries, vehicles, buildings, "
        "fences). A bridge is a long linear/planar elevated structure; vegetation "
        "is organic, irregular and scattered. Objects can be parts of the same "
        "bridge (spans, approaches). When genuinely unsure, keep."
    )
    prompt = (
        "The scan is shown from three angles (oblique, side, top). Candidate objects:\n"
        + "\n".join(stats) +
        '\n\nFor EACH id: is_bridge=true or false + a short label. Respond ONLY with '
        'JSON: {"regions":[{"id":1,"is_bridge":true,"label":"bridge deck"}, ...]}'
    )
    try:
        import json as _json
        raw = _hosted_vlm_generate(
            prompt,
            images_b64=imgs,
            timeout=300,
            num_predict=1200,
            system=system,
            temperature=0.0,
        ) or ""
        s, e = raw.find("{"), raw.rfind("}")
        data = _json.loads(raw[s:e + 1]) if 0 <= s < e else {}
    except Exception as exc:
        print(f"[analyze_bridge] clutter triage VLM call failed: {exc}")
        return np.zeros(n, bool), False
    verdict = {}
    for r in data.get("regions", []):
        try:
            verdict[int(r["id"])] = (bool(r.get("is_bridge", True)),
                                     str(r.get("label", "")).strip())
        except Exception:
            continue
    if not verdict:
        return np.zeros(n, bool), False
    removed = np.zeros(n, bool)
    for k in planar_ratio:
        keep, vlabel = verdict.get(k, (True, ""))
        if keep or planar_ratio[k] >= 0.35:
            if not keep:
                print(f"[analyze_bridge] triage: object {k} judged '{vlabel}' but has "
                      f"deck-like z concentration ({planar_ratio[k]:.2f}) â€” kept")
            continue
        removed |= comp_ids == k
        print(f"[analyze_bridge] triage: removing object {k} ({vlabel}, "
              f"{int((comp_ids == k).sum()):,} pts)")
    if removed.sum() > 0.9 * n:                  # gutting verdict â†’ hallucination
        return np.zeros(n, bool), True
    return removed, True


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ Qwen octree carve â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Coarse-to-fine space carving with the VLM as the only remover (2026-07-07
# design). Space is cut into an axis-aligned cube lattice; each round the VLM
# judges colour-coded cubes (the region-JUDGING task the bake-off measured
# reliable â€” never box drawing, measured unstable): 'bridge' keeps the cube
# and stops, 'clutter' removes it, 'mixed' subdivides it for the next, finer
# round. The recursion is the ambiguity handler â€” a cube that is 70% tree and
# 30% pier never gets a destructive verdict, it gets split until its pieces
# are clean. Geometry NEVER removes points here: empty cubes simply don't
# exist in the lattice, tiny/over-budget cubes are kept unjudged, and the
# guards below can only rescue, not remove. Every render, raw response and
# decision is written to an audit folder so each removal can be reviewed.

_CARVE_MIN_JUDGE_PTS = 40      # smaller cubes are invisible in a render â†’ kept unjudged
_CARVE_MAX_JUDGED_PER_LEVEL = 192  # 24 batches of 8; the rest are kept unjudged (audited)
_CARVE_PTS_PER_CUBE_RENDER = 5000  # stratified floor so small cubes stay visible
_CARVE_JUDGE_EDGE_M = 4.0      # no verdicts above this cube edge. Measured on the
                               # Bridge_5/0413152 runs (2026-07-07): every removal
                               # the VLM got right was on â‰¤4 m cubes; every verdict
                               # on bigger cubes was either 'subdivide' (no-op) or a
                               # keep-final that froze canopy hiding inside a
                               # ground/deck-dominated mixture. Bigger cubes descend
                               # by pure bookkeeping â€” no calls, empty space still free.


def _render_carve_views(pts: np.ndarray, slot_ids: np.ndarray,
                        classes: List[Dict[str, Any]],
                        crop_lo: Optional[np.ndarray] = None,
                        crop_hi: Optional[np.ndarray] = None,
                        max_pts: int = 45_000) -> List[str]:
    """Carve-specific render: coloured cube points over faint grey context.
    Sampling is stratified per cube (a 500-point cube inside a 300k cloud
    vanishes under uniform sampling), and an optional crop box zooms the
    context onto the cubes under review so deep-level cubes keep a readable
    apparent size."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from io import BytesIO
    except Exception as exc:  # pragma: no cover
        print(f"[point_cloud] matplotlib render unavailable: {exc}")
        return []

    keep = np.ones(len(pts), bool)
    if crop_lo is not None and crop_hi is not None:
        inside = np.all((pts >= crop_lo) & (pts <= crop_hi), axis=1)
        keep = inside | (slot_ids > 0)           # cubes always drawn whole
    sel_parts: List[np.ndarray] = []
    for c in classes:
        sidx = np.where((slot_ids == c["id"]) & keep)[0]
        if sidx.size > _CARVE_PTS_PER_CUBE_RENDER:
            sidx = sidx[_subsample(sidx.size, _CARVE_PTS_PER_CUBE_RENDER)]
        sel_parts.append(sidx)
    ctx = np.where((slot_ids == 0) & keep)[0]
    budget = max(5000, max_pts - sum(p.size for p in sel_parts))
    if ctx.size > budget:
        ctx = ctx[_subsample(ctx.size, budget)]
    sel_parts.append(ctx)
    sel = np.concatenate(sel_parts)
    p = pts[sel]
    lab = slot_ids[sel]

    color_lut = {c["id"]: c["color_rgb"] for c in classes}
    cols = np.empty((len(p), 4))
    sizes = np.full(len(p), 2.2)
    for i, l in enumerate(lab):
        rgb = color_lut.get(int(l))
        if rgb is None:
            # context must stay clearly READABLE: with near-invisible context
            # the VLM loses the bridge frame and labels foliage blobs "pier
            # cap" from the z-stats alone (measured on Bridge_5 run 5)
            cols[i] = (0.45, 0.45, 0.45, 0.55)
            sizes[i] = 1.4
        else:
            cols[i] = (*rgb, 0.95)
            sizes[i] = 2.6

    out: List[str] = []
    for _name, elev, azim in [("oblique", 20, -60), ("side", 6, 0), ("top", 88, -90)]:
        fig = plt.figure(figsize=(5.2, 4.2), dpi=110)
        ax = fig.add_subplot(111, projection="3d")
        ax.scatter(p[:, 0], p[:, 1], p[:, 2], c=cols, s=sizes, marker=".", linewidths=0)
        ax.view_init(elev=elev, azim=azim)
        ax.set_axis_off()
        try:
            ax.set_box_aspect((np.ptp(p[:, 0]) or 1, np.ptp(p[:, 1]) or 1, np.ptp(p[:, 2]) or 1))
        except Exception:
            pass
        buf = BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
        plt.close(fig)
        out.append(base64.b64encode(buf.getvalue()).decode("ascii"))
    return out


def _qwen_octree_carve(pts: np.ndarray,
                       max_levels: Optional[int] = None) -> Tuple[np.ndarray, bool, Dict[str, Any]]:
    """Returns (remove_mask, stage_ran, audit_info). Conservative by
    construction: any cube the VLM never sees, never resolves, or errors on is
    KEPT. See the block comment above for the design contract."""
    import json as _json
    import time

    n = len(pts)
    audit: Dict[str, Any] = {"mode": "carve", "levels": [], "vlm_calls": 0,
                             "audit_dir": None, "notes": []}
    if n < 5000:
        return np.zeros(n, bool), False, audit
    if max_levels is None:
        max_levels = max(1, min(8, int(os.getenv("PC_CARVE_LEVELS", "5"))))

    audit_dir: Optional[Path] = None
    try:
        from .datapaths import DATA_DIR
        audit_dir = Path(DATA_DIR) / "pointcloud_carve" / time.strftime("%Y%m%d_%H%M%S")
        audit_dir.mkdir(parents=True, exist_ok=True)
        audit["audit_dir"] = str(audit_dir)
    except Exception as exc:
        audit["notes"].append(f"audit folder unavailable ({exc}) â€” decisions not persisted")

    def _save(name: str, data: bytes) -> None:
        if audit_dir is not None:
            try:
                (audit_dir / name).write_bytes(data)
            except Exception:
                pass

    lo = pts.min(axis=0)
    extent = np.maximum(pts.max(axis=0) - lo, 1e-6)
    edge0 = float(extent.max()) / 3.0            # level-0 cubes â‰ˆ a third of the long axis

    def _bucket(idx: np.ndarray, edge: float) -> Dict[int, np.ndarray]:
        """Lattice cells (global origin, so children tile their parent exactly)."""
        dims = np.maximum((extent / edge).astype(np.int64) + 1, 1)
        ijk = ((pts[idx] - lo) / edge).astype(np.int64)
        keys = (ijk[:, 0] * dims[1] + ijk[:, 1]) * dims[2] + ijk[:, 2]
        order = np.argsort(keys, kind="stable")
        out: Dict[int, np.ndarray] = {}
        for grp in np.split(order, np.where(np.diff(keys[order]) != 0)[0] + 1):
            if grp.size:
                out[int(keys[grp[0]])] = idx[grp]
        return out

    def _cube_bounds(key: int, edge: float) -> Tuple[np.ndarray, np.ndarray]:
        dims = np.maximum((extent / edge).astype(np.int64) + 1, 1)
        k, iz = divmod(key, int(dims[2]))
        ix, iy = divmod(k, int(dims[1]))
        c_lo = lo + np.array([ix, iy, iz]) * edge
        return c_lo, c_lo + edge

    remove_mask = np.zeros(n, bool)
    active = _bucket(np.arange(n), edge0)        # empty cubes never exist â€” zero-threshold skip
    calls_failed = 0

    system = (
        "You are the space-carving clean-up stage of an automated highway-bridge "
        "point-cloud pipeline. The scan contains ONE bridge plus its surroundings. "
        "Space has been divided into axis-aligned cubes; the images show SOME of "
        "those cubes, each rendered in one colour. Grey points are context only â€” "
        "they are not under review and are kept regardless. For EACH coloured cube "
        "id, judge its contents:\n"
        "- 'bridge': entirely bridge structure (deck, girders, piers, abutments, "
        "parapets, railings, cables) OR the surface directly beneath the bridge "
        "that it spans (road, ground, rail, water and banks) â€” kept, final.\n"
        "- 'clutter': entirely NOT bridge â€” trees/vegetation, poles, sign "
        "gantries, buildings, vehicles, fences, floating scan noise. The whole "
        "cube is permanently REMOVED.\n"
        "- 'mixed': contains both, or too unclear to call â€” the cube will be "
        "split into smaller cubes and judged again at finer detail.\n"
        "Say 'clutter' ONLY when you are confident the entire cube holds no "
        "bridge structure and no under-bridge surface; if any part might be "
        "bridge, say 'mixed'. But do not rubber-stamp either: a cube that is "
        "clearly all vegetation must be 'clutter', not 'mixed' forever.\n\n"
        "The coloured cubes are PRE-FILTERED: flat sheet-like cubes (deck "
        "surface, ground, water banks) were already identified by geometry, "
        "kept, and are NOT shown to you â€” they appear only as grey context. "
        "What you are judging is volumetric content: vertical or thick bridge "
        "members (piers, abutments, girders, parapet walls, railings) versus "
        "vegetation, poles, vehicles and other clutter. Decide on the visual "
        "evidence in the images, not on plausible height ranges: man-made "
        "members have planar faces, straight edges and regular outlines; "
        "vegetation is organic, scattered and irregular. An organic scattered "
        "blob is 'clutter' even if its height would fit a pier or deck. If "
        "several parallel bridges or spans are present, ALL bridge structures "
        "count as bridge â€” never remove a second span as 'unrelated structure'."
    )

    def _zconc(idx: np.ndarray) -> float:
        """Fraction of a cube's points in its densest 0.25 m z-bin. A scanned
        surface (deck top, soffit, ground) is a SHEET â€” returns concentrate
        within centimetres â€” while even dense canopy fills volume. Measured on
        Bridge_5: at 0.5 m bins a dense 2.5 m crown cube passes 0.35 and 150
        canopy cubes slipped through the pre-keep; 0.25 m bins separate the
        two populations cleanly."""
        zc = pts[idx, 2]
        zh, _ = np.histogram(zc, bins=max(1, int(np.ptp(zc) / 0.25)))
        return float(zh.max()) / max(1, idx.size)

    def _zextent(idx: np.ndarray) -> float:
        zc = pts[idx, 2]
        return float(np.percentile(zc, 95) - np.percentile(zc, 5))

    judge_edge = float(os.getenv("PC_CARVE_JUDGE_EDGE", str(_CARVE_JUDGE_EDGE_M)))
    start_level = 0
    while edge0 / (2 ** start_level) > judge_edge and start_level < 6:
        start_level += 1
    audit["judge_edge_m"] = round(edge0 / (2 ** start_level), 2)

    for level in range(start_level + max_levels):
        if not active:
            break
        edge = edge0 / (2 ** level)
        if level < start_level:
            # pre-descend: too coarse for verdicts â€” split and go finer (free)
            audit["levels"].append({"level": level, "edge_m": round(edge, 2),
                                    "cubes": len(active), "pre_descend": True})
            active = _bucket(np.concatenate(list(active.values())), edge / 2.0)
            continue
        cubes = sorted(active.items(), key=lambda kv: kv[1].size, reverse=True)
        judged = [(k, v) for k, v in cubes if v.size >= _CARVE_MIN_JUDGE_PTS]
        skipped_small = len(cubes) - len(judged)
        # THIN slab-signature cubes can never be removed (the rescue vetoes any
        # clutter verdict), so judging them wastes budget â€” keep them without a
        # call. Thinness is required: a flat dense ground plane makes a TALL
        # ground+canopy cube pass the concentration test alone, and that is
        # exactly the mixture that must go to the VLM (measured on Bridge_5).
        slab_keys = {k for k, v in judged
                     if v.size >= 100 and _zconc(v) >= 0.45 and _zextent(v) <= 2.0}
        slab_kept = len(slab_keys)
        judged = [(k, v) for k, v in judged if k not in slab_keys]
        over_budget = judged[_CARVE_MAX_JUDGED_PER_LEVEL:]
        judged = judged[:_CARVE_MAX_JUDGED_PER_LEVEL]
        lvl_info = {"level": level, "edge_m": round(edge, 2), "cubes": len(cubes),
                    "judged": len(judged), "slab_kept": slab_kept,
                    "kept_unjudged": skipped_small + len(over_budget),
                    "removed_cubes": 0, "removed_points": 0, "subdivided": 0,
                    "kept_bridge": 0, "guard_rescued": 0}
        if over_budget:
            audit["notes"].append(
                f"level {level}: {len(over_budget)} cubes over the "
                f"{_CARVE_MAX_JUDGED_PER_LEVEL}-cube budget kept unjudged")
        next_active: Dict[int, np.ndarray] = {}

        for b in range(0, len(judged), len(_RENDER_PALETTE)):
            batch = judged[b:b + len(_RENDER_PALETTE)]
            slot_ids = np.zeros(n, np.int64)
            classes: List[Dict[str, Any]] = []
            stats: List[str] = []
            planar: Dict[int, float] = {}
            for s, (key, idx) in enumerate(batch, start=1):
                slot_ids[idx] = s
                hexc = _RENDER_PALETTE[s - 1]
                classes.append({"id": s, "color_rgb": [int(hexc[i:i + 2], 16) / 255.0
                                                       for i in (1, 3, 5)]})
                zc = pts[idx, 2]
                planar[s] = _zconc(idx)
                c_lo, c_hi = _cube_bounds(key, edge)
                stats.append(
                    f"- id {s} (colour {hexc}): {idx.size:,} pts, centre "
                    f"({(c_lo[0] + c_hi[0]) / 2:.0f}, {(c_lo[1] + c_hi[1]) / 2:.0f}) m, "
                    f"z {zc.min():.1f}..{zc.max():.1f} m, z-concentration {planar[s]:.2f}")
            crop_lo = crop_hi = None
            if level > start_level:              # zoom-as-you-descend: keep apparent size up
                b_lo = np.min([_cube_bounds(k, edge)[0] for k, _ in batch], axis=0)
                b_hi = np.max([_cube_bounds(k, edge)[1] for k, _ in batch], axis=0)
                pad = max(2.0 * edge, 0.15 * float((b_hi - b_lo).max()))
                crop_lo, crop_hi = b_lo - pad, b_hi + pad
            imgs = _render_carve_views(pts, slot_ids, classes, crop_lo, crop_hi)
            if not imgs:
                audit["notes"].append(f"level {level} batch {b // 8}: render failed â€” cubes kept")
                continue
            prompt = (
                f"Views: oblique, side, top. Carve round {level - start_level + 1} "
                f"of {max_levels}, cube edge {edge:.1f} m. Cubes under review:\n"
                + "\n".join(stats) +
                '\n\nRespond ONLY with JSON: {"cubes":[{"id":1,"verdict":"bridge",'
                '"label":"deck slab"}, ...]} â€” one entry per id, verdict one of '
                'bridge|clutter|mixed.'
            )
            def _parse(txt: str) -> Dict[str, Any]:
                # raw_decode: models sometimes emit a second JSON object or
                # prose after the first â€” take the first complete object
                s0 = txt.find("{")
                if s0 < 0:
                    return {}
                try:
                    return _json.JSONDecoder().raw_decode(txt[s0:])[0]
                except Exception:
                    return {}

            raw, data = "", {}
            try:
                audit["vlm_calls"] += 1
                raw = _hosted_vlm_generate(
                    prompt,
                    images_b64=imgs,
                    timeout=300,
                    num_predict=1200,
                    system=system,
                    temperature=0.0,
                ) or ""
                data = _parse(raw)
            except Exception as exc:
                calls_failed += 1
                audit["notes"].append(f"level {level} batch {b // 8}: VLM call failed "
                                      f"({exc}) â€” cubes kept")
                data = {}
            if not data.get("cubes"):
                audit["notes"].append(f"level {level} batch {b // 8}: no parseable "
                                      "verdicts â€” cubes kept")
            verdicts: Dict[int, Tuple[str, str]] = {}
            for r in data.get("cubes", []):
                try:
                    v = str(r.get("verdict", "")).strip().lower()
                    if v in ("bridge", "keep"):
                        v = "bridge"
                    elif v in ("clutter", "remove"):
                        v = "clutter"
                    else:
                        v = "mixed"
                    verdicts[int(r["id"])] = (v, str(r.get("label", "")).strip())
                except Exception:
                    continue

            batch_log = []
            for s, (key, idx) in enumerate(batch, start=1):
                v, vlabel = verdicts.get(s, ("keep-unjudged", ""))
                decision = v
                if v == "clutter":
                    # slab rescue (triage-proven): a cube whose z mass sits in one
                    # 0.5 m bin is deck/ground-like â€” never removed on one verdict
                    if planar[s] >= 0.35 and idx.size >= 1500:
                        decision = "guard-rescued"
                        lvl_info["guard_rescued"] += 1
                        print(f"[carve] L{level} cube {s} judged '{vlabel}' but deck-like "
                              f"(z-conc {planar[s]:.2f}) â€” kept")
                    else:
                        remove_mask[idx] = True
                        lvl_info["removed_cubes"] += 1
                        lvl_info["removed_points"] += int(idx.size)
                        print(f"[carve] L{level}: removing cube {s} ({vlabel}, {idx.size:,} pts)")
                elif v == "mixed" and level + 1 < start_level + max_levels:
                    next_active.update(_bucket(idx, edge / 2.0))
                    lvl_info["subdivided"] += 1
                elif v == "bridge":
                    lvl_info["kept_bridge"] += 1
                # 'mixed' at the last level and failed/unjudged cubes: kept as-is
                c_lo, c_hi = _cube_bounds(key, edge)
                batch_log.append({"id": s, "points": int(idx.size),
                                  "bounds": [c_lo.round(2).tolist(), c_hi.round(2).tolist()],
                                  "z_concentration": round(planar[s], 3),
                                  "verdict": v, "label": vlabel, "decision": decision})
            for vi, img in enumerate(imgs):
                _save(f"L{level}_b{b // 8}_view{vi}.png", base64.b64decode(img))
            _save(f"L{level}_b{b // 8}_verdict.json", _json.dumps(
                {"cubes": batch_log, "raw_response": raw[:4000]}, indent=1).encode("utf-8"))

        audit["levels"].append(lvl_info)
        active = next_active

    stage_ran = audit["vlm_calls"] > 0 and calls_failed < audit["vlm_calls"]
    removed = int(remove_mask.sum())
    audit["removed_points"] = removed
    if removed > 0.9 * n:                        # gutting verdict â†’ hallucination
        audit["notes"].append(f"verdicts would remove {removed:,}/{n:,} points â€” "
                              "treated as hallucination, nothing removed")
        remove_mask = np.zeros(n, bool)
        audit["removed_points"] = 0
    elif removed > 0.5 * n:
        # Legitimate on canopy-dominant scans (Bridge_5 class), so not blocked â€”
        # but flagged for human review of the audit renders.
        audit["review_recommended"] = True
        audit["notes"].append(f"carve removed {removed:,}/{n:,} points (>50%) â€” "
                              "review the audit renders")
    _save("summary.json", _json.dumps(audit, indent=1).encode("utf-8"))
    return remove_mask, stage_ran, audit


def _drop_approach_cells(fp, u, v, z, soffit0, z_span):
    """A bridge deck has an AIR GAP beneath it; an approach roadway on grade is
    solid fill all the way up. Deck-band cells whose under-fill reaches the band
    are 'solid'; large solid regions attached to the footprint boundary are the
    approaches / embankment on grade â†’ cut from the footprint. Small interior
    solid blobs are piers â†’ kept. The largest remaining blob is the true deck."""
    mask, u0, v0, cell, gu, gv = fp
    ci = np.clip(((u - u0) / cell).astype(int), 0, gu - 1)
    cj = np.clip(((v - v0) / cell).astype(int), 0, gv - 1)
    flat = ci * gv + cj
    below = z < (soffit0 - 0.02 * z_span)
    zb = np.full(gu * gv, -np.inf)
    if below.any():
        np.maximum.at(zb, flat[below], z[below])
    gap_min = max(1.2, 0.10 * z_span)
    solid = (zb > (soffit0 - gap_min)).reshape(gu, gv) & mask
    if not solid.any():
        return fp
    lab_s, ns = _label_grid(solid)
    area = int(mask.sum())
    drop = np.zeros_like(mask)
    for i in range(1, ns + 1):
        compi = lab_s == i
        sz = int(compi.sum())
        dil = compi.copy()
        dil[1:, :] |= compi[:-1, :]; dil[:-1, :] |= compi[1:, :]
        dil[:, 1:] |= compi[:, :-1]; dil[:, :-1] |= compi[:, 1:]
        touches = bool((dil & ~mask).any()) or bool(compi[0, :].any()) or bool(compi[-1, :].any()) \
            or bool(compi[:, 0].any()) or bool(compi[:, -1].any())
        if touches and sz > 0.05 * area:
            drop |= compi
    if not drop.any():
        return fp
    mask2 = mask & ~drop
    lab2, n2 = _label_grid(mask2)
    if n2 >= 1:
        big = int(np.argmax([(lab2 == i).sum() for i in range(1, n2 + 1)])) + 1
        mask2 = lab2 == big
    if int(mask2.sum()) < max(20, 0.15 * area):
        return fp                                      # degenerate cut â†’ keep original
    return (mask2, u0, v0, cell, gu, gv)


def _canon_label(raw: str, kind: str) -> str:
    """Normalise the VLM's free-text label to a small canonical vocabulary so the
    same element type never shows up under two different names."""
    s = (raw or "").lower()
    if kind == "beam" or any(w in s for w in ("girder", "beam", "stringer", "rib", "diaphragm", "cross frame")):
        return "girder / beam"
    if kind == "edge" or any(w in s for w in ("parapet", "railing", "rail", "barrier", "curb", "sidewalk")):
        return "parapet / railing / barrier"
    if any(w in s for w in ("pier", "abutment", "column", "substructure", "bent", "footing", "pile")):
        return "pier / abutment"
    if any(w in s for w in ("deck", "girder", "superstructure", "span", "beam")):
        return "deck / superstructure"
    if any(w in s for w in ("water", "river", "stream", "channel")):
        return "waterway under the bridge"
    if any(w in s for w in ("road", "ground", "pavement", "surface", "beneath", "under", "rail")):
        return "surface under the bridge"
    return {
        "slab": "deck / superstructure",
        "surface": "surface under the bridge",
        "beam": "girder / beam",
        "edge": "parapet / railing / barrier",
        "unknown": "unknown bridge structure",
    }.get(kind, "pier / abutment")


_LAST_JUDGE_MODEL: Optional[str] = None    # which VLM produced the last judgement


def _qwen_judge_components(pts: np.ndarray, comp_ids: np.ndarray,
                           render_classes: List[Dict[str, Any]],
                           structural: bool = False,
                           bridge_context: Optional[Dict[str, Any]] = None) -> Optional[Dict[int, Dict[str, Any]]]:
    """Show geometrically-segmented components to the local vision engine from
    three angles and ask it, per region, whether it is a genuine
    bridge structural component or non-structural clutter to exclude (barrier/
    parapet, sign, pole, vegetation, vehicle, noise) â€” plus a short label. This is
    the VLM "scene judgement" layer on top of geometry. ``structural`` switches the
    briefing to the digital-twin goal, where parapets/railings mounted ON the
    bridge are wanted members, not clutter. Returns {id: {keep, label}} or None if
    the model is unavailable (pipeline then keeps all geometry)."""
    present = [c for c in render_classes if c.get("count", 0) > 0]
    if not present:
        return None
    import json as _json
    imgs = _render_segment_views(pts, comp_ids, render_classes)
    if not imgs:
        return None
    if structural:
        goal = (
            "WHY THIS MATTERS: after your judgement, the pipeline fits an idealised "
            "CAD-style solid to each kept component to build a structural model "
            "(digital twin) of the bridge, and measures the vertical clearance "
            "beneath it. Every genuine bridge member must survive; anything that is "
            "not part of the bridge corrupts the model.\n\n"
            "WHAT TO KEEP: every element OF THE BRIDGE â€” deck / superstructure, "
            "girders / beams / diaphragms, piers, columns, abutments, bearings, AND "
            "parapets / railings / barriers MOUNTED ON the bridge (they are bridge "
            "members here, not clutter) â€” plus the single surface DIRECTLY BENEATH "
            "the bridge that it spans (road, ground, railway, or water).\n\n"
            "WHAT TO EXCLUDE: things that are not part of the structure â€” trees/"
            "vegetation, free-standing light or utility poles, sign gantries beside "
            "the bridge, passing vehicles, and scan noise from surrounding terrain."
        )
    else:
        goal = (
            "WHY THIS MATTERS: after your judgement, the pipeline fits geometric surfaces "
            "to each kept component and measures the vertical clearance between the "
            "underside of the bridge (deck soffit) and whatever the bridge crosses over. "
            "Any clutter left in â€” roadside barriers/parapet panels, sign gantries, light "
            "or utility poles, trees/vegetation, passing vehicles, or scan noise from the "
            "surrounding terrain â€” corrupts the fitted surfaces and produces a wrong "
            "clearance. So clutter must be excluded, but real structure must be kept.\n\n"
            "WHAT TO KEEP: (1) the bridge structure itself â€” deck / superstructure, "
            "girders, piers, columns, abutments, bearings; and (2) the single surface "
            "DIRECTLY BENEATH the bridge that it spans â€” the road, ground, railway, or "
            "water. These are exactly the things we measure between."
        )
    system_base = (
        "You are the scene-understanding stage of an automated highway-bridge "
        "inspection pipeline. A bridge has been scanned into a 3-D point cloud "
        "(LiDAR / photogrammetry). The pipeline has already cleaned the cloud and "
        "split it into candidate components by geometry; your job is to judge which "
        "components are really part of the bridge and which are clutter to remove.\n\n"
        f"{goal}\n\n"
    )
    # Conservative local judgement avoids dropping genuine structural regions.
    tail_conservative = (
        "BE CONSERVATIVE: only mark a region as non-bridge when you are confident it "
        "is clutter. When unsure, keep it â€” a missed piece of structure is worse than "
        "a little extra. The largest deck-like region and the surface under the bridge "
        "are essentially always bridge."
    )
    legend = "\n".join(f'- id {c["id"]}: colour {c["color_hex"]} (geometry guess: {c["name"]})' for c in present)
    context_block = ""
    if bridge_context:
        try:
            compact_context = _json.dumps(bridge_context, ensure_ascii=True, separators=(",", ":"))[:12000]
            context_block = (
                "\n\nBRIDGE-SPECIFIC EVIDENCE (inventory/report prior, not a substitute for the scan):\n"
                f"{compact_context}\n"
                "Use this evidence to choose specific component names and materials. Geometry wins if "
                "the evidence conflicts with the visible region. Never invent an expected component "
                "when no corresponding region is visible; never reject a visible structural region "
                "merely because it is absent from the inventory."
            )
        except Exception:
            context_block = ""
    prompt = (
        "The 3-D scan is shown from three angles (oblique, side elevation, top). "
        "Points are coloured by the geometric segmentation:\n"
        f"{legend}{context_block}\n\n"
        "For EACH id decide is_bridge=true (genuine bridge structure OR the road/"
        "ground/water directly under the bridge) or is_bridge=false (non-structural "
        "clutter to exclude). Give a specific component label, material when supported, "
        "confidence from 0 to 1, and a short evidence statement. "
        "Respond ONLY with JSON: "
        '{"regions":[{"id":1,"is_bridge":true,"label":"west reinforced-concrete abutment",'
        '"material":"reinforced concrete","confidence":0.92,'
        '"evidence":"vertical end support consistent with inventory"}, ...]}'
    )
    global _LAST_JUDGE_MODEL
    data = None
    # Retry once within the local engine before keeping the unreviewed result.
    for attempt in range(2):
        system = system_base + tail_conservative
        try:
            raw = _hosted_vlm_generate(
                prompt,
                images_b64=imgs,
                timeout=300,
                num_predict=3000,
                system=system,
                temperature=0.0,
            ) or ""
            _LAST_JUDGE_MODEL = llm_providers.generation_model()
        except Exception as exc:
            print(f"[analyze_bridge] hosted VLM judgement call failed: {exc}")
            if attempt == 0:
                continue                          # one same-provider retry
            return None
        s, e = raw.find("{"), raw.rfind("}")
        if s >= 0 and e > s:
            try:
                data = _json.loads(raw[s:e + 1])
                break
            except Exception:
                pass
        print(f"[analyze_bridge] VLM judgement unparseable (attempt {attempt + 1})")
    if data is None:
        return None
    out: Dict[int, Dict[str, Any]] = {}
    for r in data.get("regions", []):
        try:
            confidence = r.get("confidence")
            try:
                confidence = max(0.0, min(1.0, float(confidence)))
            except (TypeError, ValueError):
                confidence = None
            out[int(r["id"])] = {
                "keep": bool(r.get("is_bridge", True)),
                "label": str(r.get("label", "")).strip(),
                "material": str(r.get("material", "")).strip(),
                "confidence": confidence,
                "evidence": str(r.get("evidence", "")).strip(),
            }
        except Exception:
            continue
    return out or None


def analyze_bridge(points: Any, scale_known: bool = True, under_feature: Optional[str] = None,
                   use_vlm: bool = True, use_sam: bool = True,
                   model_mode: str = "clearance",
                   bridge_context: Optional[Dict[str, Any]] = None,
                   use_local_sam: bool = False) -> Dict[str, Any]:
    """One-step bridge scan processing: clean â†’ orient â†’ isolate the bridge (and
    only what is under it) â†’ segment into components â†’ fit a geometric volume to
    each â†’ measure clearance between the fitted surfaces. Returns one payload with
    every output the dashboard needs. Off-structure points (trees, panels, side
    clutter, approaches beyond the deck) are dropped."""
    pts_all = _as_xyz(points)
    structural_mode = (model_mode or "clearance").strip().lower() in ("structural", "model", "cad", "digital_twin")
    n_in = int(pts_all.shape[0])
    if n_in < 200:
        return {"status": "insufficient_points", "point_count": n_in}

    analysis_cap = MAX_STRUCTURAL_ANALYSIS_POINTS if structural_mode else MAX_ANALYSIS_POINTS
    pts = pts_all[_subsample(n_in, analysis_cap)]
    analysis_sample_points = int(pts.shape[0])
    # Cleaning mode. 'carve' (default, 2026-07-07): the Qwen octree carve is the
    # ONLY remover â€” statistical outlier removal is OFF in the removal path (it
    # makes millions of silent per-point decisions and its failure modes hit
    # bridges specifically: cables, railings, truss members, sparse deck
    # undersides). Noise lands in cubes the VLM judges and audits instead.
    # PC_USE_TRIAGE=1 / PC_USE_SAM3=1 restore the previous stages (both keep
    # their original SOR prepass) for comparison.
    clean_mode = ("sam3" if os.getenv("PC_USE_SAM3", "0") == "1"
                  else "triage" if os.getenv("PC_USE_TRIAGE", "0") == "1" else "carve")
    if clean_mode == "carve":
        nm = np.zeros(len(pts), bool)
    else:
        # Structural modelling needs thin vertical members to survive into SAM/Qwen.
        # The surface-variation vegetation test is helpful for clearance, but it can
        # also mark pier corners, abutment faces, and side walls as scatter.
        nm = (
            denoise_mask(pts, std_ratio=3.0, do_surfvar=False)
            if structural_mode
            else denoise_mask(pts)
        )
    removed_noise = int(nm.sum())
    if int((~nm).sum()) >= 100:
        pts = pts[~nm]
    after_initial_cleanup_points = int(pts.shape[0])

    # â”€â”€ STAGE 1: SAM 3 concept cleaning FIRST, before any geometry â”€â”€
    # Clutter recognised BY NAME (tree, pole, sign, vehicleâ€¦) on photo-like shaded
    # renders is removed up front, so the frame/footprint/bands below are derived
    # from an already-clean cloud (a tree-line can otherwise skew all of them).
    hosted_cleaning_used = False
    sam_used = False
    warnings: List[str] = []
    sam3_removed_coords = np.empty((0, 3))
    # â”€â”€ Clutter triage: geometry proposes elevated objects, the VLM judges them
    # locally. This replaces detector-style approaches that measured worse on
    # the validation corpus. PC_USE_SAM3=1 restores the older path for comparison.
    carve_audit: Optional[Dict[str, Any]] = None
    if use_sam:
        try:
            if structural_mode:
                # Digital-twin mode is recall-first: derive geometry from the
                # complete scan so sparse piers, abutments, bearings, railings and
                # soffit members cannot disappear before component review.
                rm3, s3_used = np.zeros(len(pts), bool), False
                warnings.append(
                    "Conservative structural mode: aggressive pre-geometry AI "
                    "cleaning was skipped to preserve every possible bridge member."
                )
            elif clean_mode == "sam3":
                if n_in > len(pts):
                    pts_r = pts_all[_subsample(n_in, 600_000)]
                    pts_r = pts_r[~denoise_mask(pts_r)]
                else:
                    pts_r = pts
                cxy0 = pts[:, :2].mean(axis=0)
                _, ev0 = np.linalg.eigh((pts[:, :2] - cxy0).T @ (pts[:, :2] - cxy0))
                d0r = pts_r[:, :2] - cxy0
                rm3, s3_used = _sam3_concept_mask(pts_r, d0r @ ev0[:, 1], d0r @ ev0[:, 0], pts)
            elif clean_mode in ("triage", "sam3"):
                # sam3 falls through to triage in structural mode.
                rm3, s3_used = _vlm_clutter_triage(pts)
            elif scale_known:
                rm3, s3_used, carve_audit = _qwen_octree_carve(pts)
            else:
                # every carve constant (4 m judge edge, 0.25 m sheet bins, 2 m
                # thinness) is metric â€” meaningless on an unscaled cloud
                rm3, s3_used = np.zeros(len(pts), bool), False
                warnings.append("Octree carve skipped: this scan has no metric "
                                "scale, and the carve's cube sizes and slab "
                                "tests are metre-based.")
            hosted_cleaning_used = s3_used
            if not s3_used and not structural_mode:
                warnings.append("AI clutter cleaning did not run â€” clutter "
                                "(trees, poles, signs, vehicles) was not removed.")
            if carve_audit and carve_audit.get("review_recommended"):
                warnings.append(
                    "The carve stage removed more than half of the scan points. "
                    "That is normal for heavily vegetated sites, but worth a look: "
                    "every removal decision (renders + verdicts) is saved in "
                    f"{carve_audit.get('audit_dir') or 'the carve audit folder'}. "
                    "The original file is untouched.")
            if rm3.any() and int((~rm3).sum()) >= 5000:
                sam3_removed_coords = pts[rm3]
                pts = pts[~rm3]
        except Exception as exc:
            print(f"[analyze_bridge] clutter cleaning skipped: {exc}")
            warnings.append("AI clutter cleaning failed â€” clutter was not removed.")

    after_sam3_points = int(pts.shape[0])
    n = int(pts.shape[0])
    x, y, z = pts[:, 0], pts[:, 1], pts[:, 2]
    z_lo, z_hi = np.percentile(z, [1, 99]); z_span = float(z_hi - z_lo) or 1.0
    # Drive-over / deck-surface-only captures have no under-bridge geometry at
    # all â€” every elevation-band decision below would be fabricated from a
    # couple of metres of road crown. Say so instead of reporting a fantasy
    # sub-metre "clearance" with a straight face.
    deck_only_scan = scale_known and z_span < 3.0
    if deck_only_scan:
        if (under_feature or "").startswith("water"):
            # SNBI says this bridge crosses water: no under-deck returns is the
            # EXPECTED signature (water absorbs the laser), not a bad capture â€”
            # but without the soffit there is still nothing to measure against.
            warnings.append(
                "This bridge crosses a waterway (SNBI feature record) and the scan "
                "has almost no vertical extent â€” the deck underside / soffit was "
                "not captured (water returns no LiDAR), so navigation clearance "
                "cannot be measured from this file. A scan taken from the banks "
                "that captures the soffit is needed.")
        else:
            warnings.append(
                "Scan has almost no vertical extent (looks like a deck-surface-only "
                "drive-over capture, no under-bridge geometry) â€” components and "
                "clearance are not meaningful for this file.")

    # â”€â”€ STAGE 2: bridge frame on the cleaned cloud â”€â”€
    # PCA on the horizontal footprint (u = along span, v = transverse)
    cxy = np.array([x.mean(), y.mean()])
    d0 = np.column_stack([x - cxy[0], y - cxy[1]])
    _, evec = np.linalg.eigh(d0.T @ d0)
    u_ax, v_ax = evec[:, 1], evec[:, 0]
    u = d0 @ u_ax; v = d0 @ v_ax

    # deck band = dominant elevated horizontal mass.
    # KNOWN LIMITATION (Bridge_5-class scans): a dense tree canopy over a small
    # low bridge can outvote the deck in this histogram and hijack the band â€”
    # needs a planarity-aware region approach, not a histogram tweak (a
    # "highest horizontal mode" variant was tried 2026-07-05 and regressed
    # sloped multi-level decks like 0413152).
    vert = _verticality(pts)          # used by the road/ground estimator below
    hist, edges = np.histogram(z, bins=80); centers = 0.5 * (edges[:-1] + edges[1:])
    upper = centers > (z_lo + 0.4 * z_span)
    deck_peak = float(centers[upper][np.argmax(hist[upper])]) if upper.any() else float(z_hi)
    deck_top = deck_peak + 0.10 * z_span
    band = (z > deck_peak - 0.18 * z_span) & (z < deck_top)
    soffit0 = float(np.percentile(z[band], 5)) if band.sum() > 50 else deck_peak - 0.1 * z_span

    # Bridge FOOTPRINT from the deck-band mass â€” a connected polygon, not a u/v
    # rectangle. Only what falls inside it (and under it) is the bridge + what it
    # spans; side vegetation and off-structure ground fall outside and are dropped.
    dband = (z >= soffit0) & (z <= deck_top)
    fp = _footprint_mask(u, v, dband, dilation=3 if structural_mode else 1,
                         opening=2 if structural_mode else 0) if dband.sum() > 50 else None
    if fp is not None and not structural_mode:
        fp = _drop_approach_cells(fp, u, v, z, soffit0, z_span)
    in_fp = _in_footprint(u, v, fp) if fp is not None else np.ones(n, bool)

    labels = np.full(n, _C_OFF, np.int8)
    deck_mass = in_fp & (z >= soffit0) & (z <= deck_top)
    labels[deck_mass] = _C_DECK   # above deck_top (poles/signs) stay OFF
    if structural_mode and deck_mass.sum() > 50:
        deck_span = max(0.2, deck_top - soffit0)
        lower_deck = deck_mass & (z <= soffit0 + 0.42 * deck_span)
        upper_edge = deck_mass & (z >= soffit0 + 0.58 * deck_span)
        if lower_deck.sum() > 200:
            labels[lower_deck] = _C_GIRDER
        if upper_edge.sum() > 200:
            edge_v = np.percentile(np.abs(v[upper_edge]), 82)
            parapet = upper_edge & (np.abs(v) >= edge_v)
            if parapet.sum() > 120:
                labels[parapet] = _C_PARAPET
    below = in_fp & (z < soffit0)
    road_lo = -np.inf
    road_hi = soffit0
    road_surface = None
    if below.sum() > 50:
        # Road/ground = the LOWEST dense HORIZONTAL mass under the deck. A blind
        # percentile of every below-point lands mid-air on the piers whenever
        # walls/columns dominate the under-deck returns (tall piers, gentle
        # structural-mode denoise), which mislabels the road and breaks clearance.
        horiz_below = below & (vert >= 0.55)
        zb = z[horiz_below] if int(horiz_below.sum()) >= 200 else z[below]
        hb, eb = np.histogram(zb, bins=60)
        cb = 0.5 * (eb[:-1] + eb[1:])
        strong = np.where(hb >= max(6, int(0.25 * hb.max())))[0]
        road_surface = float(cb[strong[0]]) if strong.size else float(np.percentile(zb, 20))
        near_road = zb[np.abs(zb - road_surface) <= max(0.4, 0.05 * z_span)]
        if near_road.size >= 50:
            road_surface = float(np.percentile(near_road, 75))   # top of the road returns
        road_band = max(0.08, min(0.60, 0.06 * z_span))
        road_lo = road_surface - road_band
        road_hi = road_surface + max(0.04, min(0.20, 0.02 * z_span))
        road = below & (z >= road_lo) & (z <= road_hi)
        labels[road] = _C_ROAD
        sub_cand = below & ~road
        if np.isfinite(road_lo):
            # same physical floor as the full-cloud labelling: below-ground
            # reflection streaks under a pier's plan position must not become
            # 20 m-deep pier component masks (they set the fitted dims)
            sub_cand &= z >= road_lo - 2.5
        if sub_cand.sum() > 50:
            # Substructure = VERTICAL surfaces only (pier walls, columns, abutment
            # faces). Embankment slopes are planar-but-tilted and horizontal ground
            # fringes are flat â€” without this gate they merge into one giant hull
            # that looks nothing like a structural element.
            labels[sub_cand & (vert < 0.45)] = _C_SUB
            if structural_mode:
                _promote_column_cells(labels, sub_cand, u, v, z,
                                      road_surface, soffit0)
                labels[sub_cand & (labels == _C_OFF)] = _C_UNKNOWN

    # SAM instance cleaning (2nd pass): clutter whose instance is dominantly
    # off-structure (panels/objects bleeding into the elevation bands) is removed
    # before fitting, so the idealised solids fit only genuine structure.
    labels_before_detached = labels
    labels, detached_removed = _remove_detached_panels(labels, u, v, deck_mass)
    detached_removed_coords = pts[(labels_before_detached != _C_OFF) & (labels == _C_OFF)]

    sam_removed = np.zeros(n, bool)
    fragment_removed = 0
    if use_local_sam and not structural_mode:
        # Optional local MobileSAM purity vote. Hosted deployments leave this off;
        # the hosted VLM carve above remains enabled independently via ``use_sam``.
        try:
            labels, sam_removed, s_used = _sam_refine_labels(pts, u, v, labels)
            sam_used = sam_used or s_used
            if not s_used:
                warnings.append("SAM instance cleaning did not run â€” labels come "
                                "from elevation geometry only.")
        except Exception as exc:
            print(f"[analyze_bridge] SAM refine skipped: {exc}")
            warnings.append("SAM instance cleaning failed â€” labels come from "
                            "elevation geometry only.")

    labels_before_fragments = labels
    labels, fragment_removed = _prune_label_fragments(labels, u, v)
    fragment_removed_coords = pts[(labels_before_fragments != _C_OFF) & (labels == _C_OFF)]

    components: List[Dict[str, Any]] = []
    comp_ids = np.zeros(n, np.int64)                 # per-point component id (0 = none)
    render_classes: List[Dict[str, Any]] = []        # distinct-colour classes for the VLM render
    piers_uv: Dict[int, Tuple[float, ...]] = {}   # cid -> (cu, cv, radius, ru, rv) for full-cloud labelling
    deck_cid = [0]; road_cid = [0]
    _next = [1]

    def _emit(mask, key, label, kind, color, mesh):
        cid = _next[0]; _next[0] += 1
        comp_ids[mask] = cid
        vol = _obb_world(pts[mask], u_ax, v_ax, cxy)
        cov = mesh.get("coverage_pct", vol["coverage_pct"]) if mesh else vol["coverage_pct"]
        comp = {"id": cid, "key": key, "label": label, "kind": kind, "color": color,
                "point_count": int(mask.sum()), **vol}
        comp["coverage_pct"] = cov
        if mesh:
            comp["mesh"] = {"vertices": mesh["vertices"], "faces": mesh["faces"]}
            comp["fit_rms_m"] = mesh.get("fit_rms_m")
            if mesh.get("primitive_count"):
                comp["primitive_count"] = int(mesh["primitive_count"])
                comp["primitive_type"] = mesh.get("primitive_type", "prism")
        components.append(comp)
        # distinct palette colour PER REGION for the VLM render only (so the model
        # can reference regions unambiguously); the UI colour stays per kind.
        hexc = _RENDER_PALETTE[(cid - 1) % len(_RENDER_PALETTE)]
        rgb = [int(hexc[i:i + 2], 16) / 255.0 for i in (1, 3, 5)]
        render_classes.append({"id": cid, "name": label, "color_hex": hexc,
                               "color_rgb": rgb, "count": int(mask.sum())})
        return cid

    # DECK: idealised CAD-style loft â€” rectangular section swept along the span
    # (follows curvature/skew with planar facets + straight edges).
    dm = labels == _C_DECK
    deck_st = deck_soffit = None
    if dm.sum() >= 50:
        deck_mesh, deck_st, deck_soffit, deck_rms = _deck_loft(u[dm], v[dm], z[dm], cxy, u_ax, v_ax)
        deck_mesh["coverage_pct"] = _obb_world(pts[dm], u_ax, v_ax, cxy)["coverage_pct"]
        deck_mesh["fit_rms_m"] = deck_rms
        k, l, kd, col = _C_META[_C_DECK]
        deck_cid[0] = _emit(dm, k, l, kd, col, deck_mesh)
    # UNDER-BRIDGE surface (road / ground / water): thin idealised plate loft
    rm = labels == _C_ROAD
    if rm.sum() >= 50:
        rmesh, road_rms = _ribbon_loft(u[rm], v[rm], z[rm], cxy, u_ax, v_ax)
        rmesh["coverage_pct"] = _obb_world(pts[rm], u_ax, v_ax, cxy)["coverage_pct"]
        rmesh["fit_rms_m"] = road_rms
        k, l, kd, col = _C_META[_C_ROAD]
        if (under_feature or "").startswith("water"):
            l = "Under bridge (waterway / banks)"   # SNBI: crosses water, not a road
        road_cid[0] = _emit(rm, k, l, kd, col, rmesh)
    if structural_mode:
        gm = labels == _C_GIRDER
        if gm.sum() >= 80:
            k, l, kd, col = _C_META[_C_GIRDER]
            _emit_cluster_solids(gm, _C_GIRDER, k, l, kd, col, pts, u, v, z, cxy, u_ax, v_ax,
                                 _emit, min_points=90, max_clusters=10)
        pm = labels == _C_PARAPET
        if pm.sum() >= 80:
            k, l, kd, col = _C_META[_C_PARAPET]
            _emit_cluster_solids(pm, _C_PARAPET, k, l, kd, col, pts, u, v, z, cxy, u_ax, v_ax,
                                 _emit, min_points=80, max_clusters=8)
    # SUBSTRUCTURE â†’ cluster into individual piers/abutments. Each becomes a clean
    # vertical prism (simplified straight-edged cross-section) spanning from the
    # ground under it up to the deck soffit â€” CAD-crisp and no air gaps.
    subm = labels == _C_SUB
    if subm.sum() >= 50:
        su, sv = u[subm], v[subm]
        G = 48
        gi = np.clip(((su - su.min()) / ((su.max() - su.min()) or 1) * (G - 1)).astype(int), 0, G - 1)
        gj = np.clip(((sv - sv.min()) / ((sv.max() - sv.min()) or 1) * (G - 1)).astype(int), 0, G - 1)
        occ = np.zeros((G, G), bool); occ[gi, gj] = True
        lab, ncomp = _label_grid(occ)
        sub_idx = np.where(subm)[0]
        piers = 0
        key, label, kind, kcol = _C_META[_C_SUB]

        def _emit_pier(fm, key_i, label_i):
            cu, cv = float(u[fm].mean()), float(v[fm].mean())
            rad = float(np.percentile(np.hypot(u[fm] - cu, v[fm] - cv), 90))
            ru = float(np.percentile(np.abs(u[fm] - cu), 95))
            rv = float(np.percentile(np.abs(v[fm] - cv), 95))
            zb = float(np.percentile(z[fm], 2))
            if rm.sum() > 0:                              # ground under this pier
                near = rm & (np.hypot(u - cu, v - cv) < max(rad * 2.0, 3.0))
                if near.sum() >= 10:
                    zb = min(zb, float(np.percentile(z[near], 50)))
            zt = float(np.percentile(z[fm], 98))
            if deck_st is not None:                       # top out at the swept soffit
                zt = max(zt, float(np.interp(cu, deck_st, deck_soffit)))
            try:  # circular drum â†’ true cylinder; anything else â†’ piecewise prism
                pmesh = _pier_template_mesh(u[fm], v[fm], z[fm], cxy, u_ax, v_ax,
                                            z_bottom=zb, z_top=zt)
            except Exception:
                pmesh = None
            if pmesh is None:
                pmesh = _piecewise_prism_mesh(
                    u[fm], v[fm], z[fm], cxy, u_ax, v_ax,
                    z_bottom=zb, z_top=zt, max_segments=12, min_points=80,
                )
                pmesh["fit_rms_m"] = None
            pmesh["coverage_pct"] = _obb_world(pts[fm], u_ax, v_ax, cxy)["coverage_pct"]
            cid = _emit(fm, key_i, label_i, kind, kcol, pmesh)
            piers_uv[cid] = (cu, cv, rad, ru, rv)

        for i in range(1, ncomp + 1):
            cellmask = lab[gi, gj] == i
            if cellmask.sum() < 300:      # drop small fragments (noise, partial faces)
                continue
            piers += 1
            fm = np.zeros(n, bool); fm[sub_idx[cellmask]] = True
            _emit_pier(fm, f"{key}_{piers}", f"{label} #{piers}")
        if piers == 0:  # one connected mass (abutment block)
            _emit_pier(subm, key, label)
    if structural_mode:
        um = labels == _C_UNKNOWN
        if um.sum() >= 120:
            k, l, kd, col = _C_META[_C_UNKNOWN]
            _emit_cluster_solids(um, _C_UNKNOWN, k, l, kd, col, pts, u, v, z, cxy, u_ax, v_ax,
                                 _emit, min_points=120, max_clusters=10)

    if use_local_sam:
        _release_gpu_models()

    # VLM scene judgement: qwen names each component and flags any that are actually
    # non-structural clutter (barrier/parapet, sign, pole, tree, vehicle) â†’ dropped.
    vlm_used = False
    if use_vlm and components:
        judged = _qwen_judge_components(pts, comp_ids, render_classes,
                                        structural=structural_mode,
                                        bridge_context=bridge_context)
        if judged:
            vlm_used = True
            kept: List[Dict[str, Any]] = []
            _CLUTTER_WORDS = ("tree", "vegetat", "bush", "foliage", "pole", "sign",
                              "gantry", "barrier", "parapet", "guardrail", "fence",
                              "vehicle", "car", "truck", "noise", "clutter", "wire",
                              "cable", "light", "lamp")
            for comp in components:
                j = judged.get(comp["id"])
                raw_label = (j.get("label") or "") if j else ""
                if raw_label:
                    # Normalise the model's free-text label to the canonical vocab so
                    # identical elements never show different names.
                    comp["vlm_label"] = _canon_label(raw_label, comp["kind"])
                    comp["vlm_specific_label"] = raw_label
                if j and j.get("material"):
                    comp["vlm_material"] = j["material"]
                if j and j.get("confidence") is not None:
                    comp["vlm_confidence"] = j["confidence"]
                if j and j.get("evidence"):
                    comp["vlm_evidence"] = j["evidence"]
                # The deck and the under-bridge road/ground are geometrically
                # certain and measurement-critical â€” never let the VLM drop them.
                # A substructure component is only dropped when the verdict is
                # CONSISTENT: keep=false AND the label itself names clutter (tree,
                # pole, sign, vehicleâ€¦). "Not bridge" but labelled "pier" is a
                # contradiction â†’ keep (guards against judgement variance).
                clutter_words = _CLUTTER_WORDS
                if comp["kind"] in ("beam", "edge"):
                    # Girder/parapet solids are deliberately-modelled bridge members
                    # (structural mode). The judge naming one by its own vocabulary
                    # ("parapet", "railing", "barrier"â€¦) is confirmation, not
                    # clutter â€” only genuinely foreign labels (tree, vehicleâ€¦) may
                    # still drop it.
                    clutter_words = tuple(w for w in _CLUTTER_WORDS
                                          if w not in ("parapet", "railing", "guardrail",
                                                       "barrier", "rail"))
                is_clutter = any(w in raw_label.lower() for w in clutter_words)
                large_protected = (
                    comp["kind"] in ("slab", "surface")
                    and comp.get("point_count", 0) >= max(5000, int(0.05 * n))
                )
                structural_protected = structural_mode and comp["kind"] in (
                    "slab", "surface", "column", "beam", "edge"
                )
                if j and not j["keep"] and is_clutter and not large_protected and not structural_protected:
                    comp["vlm_verdict"] = f"excluded as non-structural ({raw_label.strip()})"
                    comp_ids[comp_ids == comp["id"]] = 0
                    piers_uv.pop(comp["id"], None)
                    continue
                kept.append(comp)
            components = kept
    if use_vlm and not vlm_used:
        warnings.append("AI component review (qwen) unavailable â€” components are "
                        "unreviewed geometric fits.")
    if _CUDA_POISONED:
        warnings.append("A GPU driver error occurred â€” restart the backend to "
                        "restore AI cleaning and review.")

    # Label EVERY input point with its component id (0 = removed) using the derived
    # model, so the viewer colours the exact cloud in ONE pass â€” no separate segment
    # step â€” and hides the dropped noise/vegetation.
    kept_ids = {c["id"] for c in components}
    dc = deck_cid[0] if deck_cid[0] in kept_ids else 0
    rc = road_cid[0] if road_cid[0] in kept_ids else 0
    piers_keep = {cid: uv for cid, uv in piers_uv.items() if cid in kept_ids}
    labels_full = _label_full_cloud(pts_all, cxy, u_ax, v_ax, fp,
                                    soffit0, deck_top, road_lo, road_hi, dc, rc, piers_keep)
    if structural_mode and _HAS_SCIPY and _cKDTree is not None:
        analysis_keep = comp_ids > 0
        if analysis_keep.any():
            tree_model = _cKDTree(pts[analysis_keep])
            d_model, nn_model = tree_model.query(pts_all, k=1, distance_upper_bound=0.35, workers=KDT_WORKERS)
            src_comp = comp_ids[analysis_keep]
            hit = np.isfinite(d_model)
            labels_full[hit] = src_comp[nn_model[hit]]
    # Carve the SAM-removed clutter out of the full cloud too (the geometric model
    # alone would re-admit it): any point near a removed subsample point goes.
    carve_parts = [
        arr for arr in (sam3_removed_coords, detached_removed_coords, fragment_removed_coords)
        if len(arr)
    ]
    if sam_removed.any():
        carve_parts.append(pts[sam_removed])
    carve = np.vstack(carve_parts) if carve_parts else np.empty((0, 3))
    if len(carve) and _HAS_SCIPY and _cKDTree is not None:
        tree = _cKDTree(carve)
        keep_idx = np.where(labels_full > 0)[0]
        if keep_idx.size:
            d, _ = tree.query(pts_all[keep_idx], k=1, distance_upper_bound=0.25, workers=KDT_WORKERS)
            labels_full[keep_idx[np.isfinite(d)]] = 0
    kept_pts = int((labels_full > 0).sum())

    # The viewer works at KIND level (one colour + one name per element type) so
    # identical elements are never split across names/colours in the legend.
    kind_map = {
        "slab": _C_DECK,
        "surface": _C_ROAD,
        "column": _C_SUB,
        "beam": _C_GIRDER,
        "edge": _C_PARAPET,
        "unknown": _C_UNKNOWN,
    }
    comp_to_kind = {c["id"]: kind_map.get(c["kind"], _C_UNKNOWN) for c in components}
    kind_lab = np.zeros(len(labels_full), np.int64)
    for cid, kk in comp_to_kind.items():
        kind_lab[labels_full == cid] = kk
    classes = []
    for kc in (_C_DECK, _C_SUB, _C_ROAD, _C_GIRDER, _C_PARAPET, _C_UNKNOWN):
        if not any(k == kc for k in comp_to_kind.values()):
            continue
        _, kname, _, kcolh = _C_META[kc]
        if kc == _C_ROAD and (under_feature or "").startswith("water"):
            kname = "Under bridge (waterway / banks)"
        classes.append({"id": kc, "name": kname, "color_hex": kcolh,
                        "color_rgb": [int(kcolh[i:i + 2], 16) / 255.0 for i in (1, 3, 5)]})

    # Poisson surface of the KEPT bridge points â€” smoothing + meshing embedded in
    # the same pass (runs in the Open3D venv out-of-process; skipped if absent).
    # The mesh doubles as QA: bumps/holes show outliers and scan gaps directly.
    surface = None
    try:
        from .open3d_bridge import run_op, open3d_available
        if open3d_available():
            keep_sub = pts[comp_ids > 0]
            if len(keep_sub) >= 2000:
                voxel = max(0.06, z_span / 250.0)
                r1 = run_op("voxel_downsample", keep_sub, params={"voxel": voxel})
                dpts = r1["array"][:, :3] if r1 and "array" in r1 else keep_sub
                # depth 8 (not 9): one octree level â‰ˆ half the Poisson memory/CPU
                # burst, and the mesh is decimated to 60k tris regardless.
                r2 = run_op("poisson_mesh", dpts,
                            params={"depth": 8, "density_quantile": 0.10,
                                    "target_tris": 60000}, timeout=300)
                if r2 and r2.get("vertices") is not None and len(r2.get("faces", [])) > 0:
                    surface = {"vertices": np.round(np.asarray(r2["vertices"]), 3).tolist(),
                               "faces": np.asarray(r2["faces"], np.int64).tolist(),
                               "vertex_count": int(len(r2["vertices"])),
                               "face_count": int(len(r2["faces"]))}
    except Exception as exc:
        print(f"[analyze_bridge] poisson surface skipped: {exc}")

    # Clearance should use the same cleaned measurement surfaces that the viewer
    # shows. Otherwise a clutter point removed from fitting can still pull the
    # clearance line to the wrong spot. Girders are part of the overhead mass â€”
    # their bottom flange is the controlling soffit on flat girder bridges â€” so
    # they must stay in the clearance points even though they are fitted as
    # separate solids in structural mode. Substructure stays in too: a pier/wall
    # then fills its grid cell top-to-bottom and the per-cell gap logic skips it,
    # instead of the pier TOP (inside the deck band) masquerading as a low soffit
    # over an artificially emptied column of air.
    clearance_mask = ((labels == _C_DECK) | (labels == _C_GIRDER)
                      | (labels == _C_ROAD) | (labels == _C_SUB))
    clearance_pts = pts[clearance_mask] if int(clearance_mask.sum()) >= 200 else pts
    clearance = compute_clearance(clearance_pts, 64, None, False, scale_known, under_feature)
    if deck_only_scan and isinstance(clearance, dict):
        clearance["confidence"] = "low"
        clearance["deck_only_scan"] = True

    return {
        "status": "success",
        "model_mode": "structural" if structural_mode else "clearance",
        "scale_known": bool(scale_known),
        "under_feature": clearance.get("under_feature"),
        "waterway": clearance.get("waterway"),
        "frame": {"center": [round(float(cxy[0]), 3), round(float(cxy[1]), 3)],
                  "along_axis": [round(float(u_ax[0]), 4), round(float(u_ax[1]), 4)],
                  "transverse_axis": [round(float(v_ax[0]), 4), round(float(v_ax[1]), 4)]},
        "cleaning": {"input_points": n_in, "kept_points": kept_pts,
                     "removed_noise": int(n_in - kept_pts),
                     "sam_removed": int(sam_removed.sum()) + int(len(sam3_removed_coords)),
                     "fragment_removed": int(fragment_removed),
                     "detached_removed": int(detached_removed),
                     "clean_mode": clean_mode,
                     "carve": carve_audit},   # levels, calls, audit_dir, notes (or None)
        "stage_counts": {
            "input_points": n_in,
            "analysis_sample_points": analysis_sample_points,
            "after_initial_cleanup_points": after_initial_cleanup_points,
            "after_sam3_cleanup_points": after_sam3_points,
            "analysis_labeled_points": int((comp_ids > 0).sum()),
            "full_cloud_kept_points": kept_pts,
        },
        "components": components,
        "component_count": len(components),
        "vlm_used": vlm_used,
        "vlm_model": _LAST_JUDGE_MODEL if vlm_used else None,
        "label_context": bridge_context or None,
        "hosted_cleaning_used": hosted_cleaning_used,
        "sam_used": sam_used,
        "labels_b64u8": _b64_bytes(kind_lab.astype(np.uint8)),  # per INPUT point, order-aligned (kind ids)
        "classes": classes,
        "surface": surface,                                 # Poisson mesh of the kept bridge (or None)
        "clearance": clearance,
        "warnings": warnings,       # degraded-mode notices (SAM/qwen failures) for the UI
        "notes": [
            ("Structural model result: clutter is removed, but bridge-like members are "
             "kept as separate fitted solids (deck slab, girder/soffit bands, parapet/"
             "railing edges, supports, unknown structure, and under-surface). Clearance "
             "is still measured only from the cleaned road/ground to the cleaned deck/"
             "girder underside."
             if structural_mode else
             "Clearance result: cleaned, isolated to the bridge footprint and only what "
             "is directly beneath it, segmented into measurement components; clearance "
             "measured between the fitted road/ground and soffit surfaces."),
        ],
    }


def _label_full_cloud(pts_all, cxy, u_ax, v_ax, fp, soffit0, deck_top, road_lo, road_hi,
                      deck_cid, road_cid, piers_keep) -> np.ndarray:
    """Assign every input point to a component id using the model derived from the
    analysis subsample. 0 = removed (outside footprint, above deck, or unassigned)."""
    u, v = _uv(pts_all[:, :2], cxy, u_ax, v_ax)
    z = pts_all[:, 2]
    lab = np.zeros(len(pts_all), np.int64)
    in_fp = _in_footprint(u, v, fp) if fp is not None else np.ones(len(pts_all), bool)
    if deck_cid:
        lab[in_fp & (z >= soffit0) & (z <= deck_top)] = deck_cid
    below = in_fp & (z < soffit0)
    road = below & (z >= road_lo) & (z <= road_hi)
    if road_cid:
        lab[road] = road_cid
    # Physical floor: a pier cannot extend meaningfully below the under-bridge
    # surface. Without this, below-ground reflection streaks under a pier's
    # plan position are labelled pier all the way down (0413152: 50 m+ tails).
    # 2.5 m margin tolerates embankment bases and stream beds; points below it
    # are left UNLABELLED (hidden as noise) â€” nothing is deleted.
    sub = below & ~road
    if np.isfinite(road_lo):
        sub &= z >= road_lo - 2.5
    if piers_keep and sub.any():
        cids = list(piers_keep.keys())
        cents = np.array([[piers_keep[c][0], piers_keep[c][1]] for c in cids])
        rads = np.array([piers_keep[c][2] for c in cids])
        ru = np.array([piers_keep[c][3] if len(piers_keep[c]) > 3 else piers_keep[c][2] for c in cids])
        rv = np.array([piers_keep[c][4] if len(piers_keep[c]) > 4 else piers_keep[c][2] for c in cids])
        su, sv = u[sub], v[sub]
        du = np.abs(su[:, None] - cents[None, :, 0])
        dv = np.abs(sv[:, None] - cents[None, :, 1])
        d2 = du ** 2 + dv ** 2
        nn = d2.argmin(axis=1)
        dmin = np.sqrt(d2[np.arange(len(su)), nn])
        keep = dmin <= np.maximum(rads[nn] * 1.6, 0.5)   # stray points beyond any pier â†’ removed
        radial_keep = dmin <= np.maximum(rads[nn] * 1.8, 0.5)
        box_keep = (
            (du[np.arange(len(su)), nn] <= np.maximum(ru[nn] * 1.35, 0.45))
            & (dv[np.arange(len(su)), nn] <= np.maximum(rv[nn] * 1.35, 0.45))
        )
        keep = radial_keep | box_keep
        sub_idx = np.where(sub)[0]
        cid_arr = np.array(cids)
        lab[sub_idx[keep]] = cid_arr[nn[keep]]
    return lab


def _segment_geometric(pts: np.ndarray, grid: int = 56) -> Tuple[np.ndarray, Dict[str, float]]:
    """Classify points into ground / substructure / soffit / deck via Z-banding +
    per-cell vertical-variance (column detection). Mirrors the proven client
    heuristic but runs on numpy for the server-side pipeline. Returns (labels,
    level info)."""
    n = pts.shape[0]
    labels = np.full(n, SEG_UNCLASSIFIED, dtype=np.int64)
    z = pts[:, 2]
    z_lo, z_hi = np.percentile(z, [1.0, 99.0])
    z_span = float(z_hi - z_lo) or 1.0

    # Histogram peaks â†’ ground top / deck bottom / soffit centre.
    bins = 400
    hist, edges = np.histogram(z, bins=bins, range=(z_lo, z_hi))
    centers = (edges[:-1] + edges[1:]) / 2.0
    # Gaussian smoothing
    radius = 6
    kernel = np.exp(-np.linspace(-2.5, 2.5, 2 * radius + 1) ** 2)
    kernel /= kernel.sum()
    smooth = np.convolve(hist.astype(np.float64), kernel, mode="same")
    avg = smooth.mean() or 1.0
    peaks: List[float] = []
    for b in range(3, bins - 3):
        w = smooth[b]
        if (w > smooth[b - 1] and w > smooth[b + 1] and w > smooth[b - 2]
                and w > smooth[b + 2] and w > avg * 0.25):
            peaks.append(float(centers[b]))
    peaks.sort()

    bw = z_span / bins * 14.0
    ground_top = z_lo + z_span * 0.10
    deck_bottom = z_lo + z_span * 0.88
    soffit_ctr = None
    if peaks:
        ground_top = peaks[0] + bw
    if len(peaks) >= 2:
        deck_bottom = peaks[-1] - bw
    if len(peaks) >= 3:
        mids = [p for p in peaks if 0.20 < (p - z_lo) / z_span < 0.82]
        if mids:
            soffit_ctr = mids[len(mids) // 2]

    # 2D grid for local vertical spread (piers/abutments show high std).
    x, y = pts[:, 0], pts[:, 1]
    x_lo, x_hi = np.percentile(x, [1.0, 99.0])
    y_lo, y_hi = np.percentile(y, [1.0, 99.0])
    x_rng = float(x_hi - x_lo) or 1.0
    y_rng = float(y_hi - y_lo) or 1.0
    cgx = np.clip(((x - x_lo) / x_rng * grid).astype(np.int64), 0, grid - 1)
    cgy = np.clip(((y - y_lo) / y_rng * grid).astype(np.int64), 0, grid - 1)
    cell = cgy * grid + cgx
    ncell = grid * grid
    csum = np.bincount(cell, weights=z, minlength=ncell)
    csum2 = np.bincount(cell, weights=z * z, minlength=ncell)
    ccnt = np.bincount(cell, minlength=ncell).astype(np.float64)
    cmean = np.divide(csum, ccnt, out=np.zeros_like(csum), where=ccnt > 0)
    cvar = np.divide(csum2, ccnt, out=np.zeros_like(csum2), where=ccnt > 0) - cmean ** 2
    cstd = np.sqrt(np.clip(cvar, 0, None))
    pstd = cstd[cell]

    is_ground = z <= ground_top
    is_deck = z >= deck_bottom
    labels[is_ground] = SEG_GROUND
    labels[is_deck] = SEG_DECK
    if soffit_ctr is not None:
        is_soffit = (~is_ground) & (~is_deck) & (np.abs(z - soffit_ctr) <= bw * 1.6)
        labels[is_soffit] = SEG_SOFFIT
    mid = (~is_ground) & (~is_deck) & (labels == SEG_UNCLASSIFIED)
    labels[mid & (pstd > z_span * 0.08)] = SEG_SUBSTRUCTURE

    info = {"ground_top": float(ground_top), "deck_bottom": float(deck_bottom),
            "soffit_ctr": float(soffit_ctr) if soffit_ctr is not None else float("nan"),
            "z_span": z_span}
    return labels, info


def _expand_labels(sample_pts: np.ndarray, sample_labels: np.ndarray, all_pts: np.ndarray) -> np.ndarray:
    """Propagate labels computed on a subsample to every point via nearest-neighbour."""
    if sample_pts.shape[0] == all_pts.shape[0]:
        return sample_labels
    if _HAS_SCIPY and _cKDTree is not None:
        tree = _cKDTree(sample_pts)
        _, idx = tree.query(all_pts, k=1, workers=KDT_WORKERS)
        return sample_labels[idx]
    # numpy fallback (chunked brute-force) â€” only hit if scipy is unavailable.
    out = np.empty(all_pts.shape[0], dtype=sample_labels.dtype)
    step = 4096
    for s in range(0, all_pts.shape[0], step):
        chunk = all_pts[s:s + step]
        d = ((chunk[:, None, :] - sample_pts[None, :, :]) ** 2).sum(-1)
        out[s:s + step] = sample_labels[d.argmin(1)]
    return out


def _render_segment_views(pts: np.ndarray, labels: np.ndarray, classes: List[Dict[str, Any]],
                          max_pts: int = 40_000) -> List[str]:
    """Render the segmented cloud from a few canonical views to base64 PNGs for the
    multimodal model. Uses matplotlib (Agg) so it works headless on Windows."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from io import BytesIO
        import base64
    except Exception as exc:  # pragma: no cover
        print(f"[point_cloud] matplotlib render unavailable: {exc}")
        return []

    idx = _subsample(pts.shape[0], max_pts)
    p = pts[idx]
    lab = labels[idx]
    color_lut = {c["id"]: c["color_rgb"] for c in classes}
    cols = np.array([color_lut.get(int(l), [0.5, 0.5, 0.5]) for l in lab])

    views = [("oblique", 20, -60), ("side", 6, 0), ("top", 88, -90)]
    out: List[str] = []
    for _name, elev, azim in views:
        fig = plt.figure(figsize=(5.2, 4.2), dpi=110)
        ax = fig.add_subplot(111, projection="3d")
        ax.scatter(p[:, 0], p[:, 1], p[:, 2], c=cols, s=1.2, marker=".", linewidths=0)
        ax.view_init(elev=elev, azim=azim)
        ax.set_axis_off()
        try:
            ax.set_box_aspect((np.ptp(p[:, 0]) or 1, np.ptp(p[:, 1]) or 1, np.ptp(p[:, 2]) or 1))
        except Exception:
            pass
        buf = BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
        plt.close(fig)
        out.append(base64.b64encode(buf.getvalue()).decode("ascii"))
    return out


def _name_segments_with_qwen(pts: np.ndarray, labels: np.ndarray,
                             classes: List[Dict[str, Any]],
                             report_context: str = "") -> List[Dict[str, Any]]:
    """Show the segmented render to the on-device Qwen multimodal model and ask it
    to map each region to a documented bridge element + material. Grounded in the
    AASHTO/NJDOT element taxonomy (and the bridge's own inspection-report text when
    available) so names match inspection vocabulary, not free text. Class
    ids/colours are preserved; only display names + materials are enriched."""
    # Don't ask the model to name the noise/vegetation bucket â€” it isn't structure.
    present = [c for c in classes if c.get("count", 0) > 0 and c["id"] != SEG_NOISE]
    if not present:
        return classes
    images = _render_segment_views(pts, labels, classes)
    if not images:
        return classes
    import json as _json

    legend = "\n".join(
        f'- id {c["id"]}: shown in colour {c["color_hex"]} (currently "{c["name"]}")'
        for c in present
    )
    report_block = ""
    if report_context.strip():
        report_block = (
            "\nThis specific bridge's inspection report mentions the following "
            "components/materials â€” prefer these when they match what you see:\n"
            f"{report_context.strip()[:1500]}\n"
        )
    prompt = (
        "You are a bridge inspector labelling a 3-D LiDAR scan. The images show the "
        "SAME scan from three angles (oblique, side elevation, top); points are "
        "coloured by a geometric segmentation into structural regions:\n"
        f"{legend}\n\n"
        f"{BRIDGE_TAXONOMY}\n"
        f"{report_block}\n"
        "For each colour/region id, map it to the closest standard element NAME from "
        "the taxonomy above (e.g. 'reinforced-concrete deck', 'steel girder', "
        "'reinforced-concrete pier', 'abutment', 'roadway/ground under bridge') and "
        "give the most likely MATERIAL (reinforced concrete, prestressed concrete, "
        "structural steel, timber, masonry, or unknown), judged from shape and colour. "
        "Material is an estimate, not a measurement. Respond ONLY with JSON of the form: "
        '{"regions":[{"id":0,"name":"...","material":"..."}, ...]}'
    )
    try:
        raw = _hosted_vlm_generate(
            prompt,
            images_b64=images,
            timeout=300,
            num_predict=400,
            temperature=0.0,
        ) or ""
    except Exception as exc:
        print(f"[point_cloud] qwen naming call failed: {exc}")
        return classes

    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return classes
    try:
        data = _json.loads(raw[start:end + 1])
        by_id = {int(r["id"]): r for r in data.get("regions", []) if "id" in r}
    except Exception:
        return classes

    _generic = {"unclassified", "unknown", "none", "n/a", ""}
    for c in classes:
        r = by_id.get(int(c["id"]))
        if not r:
            continue
        name = str(r.get("name", "")).strip()
        material = str(r.get("material", "")).strip()
        # Only override the informative default when Qwen is actually confident.
        if name and name.lower() not in _generic:
            c["name"] = name[:60]
        if material and material.lower() not in _generic:
            c["material"] = material[:30]
    return classes


def _refine_instances_with_sam(pts: np.ndarray, labels: np.ndarray) -> Optional[Dict[str, Any]]:
    """OPTIONAL SAM3D-style instance refinement (default off). Renders an
    orthographic side elevation with an exact point-index z-buffer, runs MobileSAM
    'segment everything', and back-projects masks to per-point instance ids. Returns
    None if ultralytics/weights are unavailable. The structural `labels` are left
    untouched; this only adds instance separation metadata."""
    try:
        from ultralytics import SAM  # type: ignore
    except Exception as exc:
        print(f"[point_cloud] SAM unavailable: {exc}")
        return None

    # Orthographic side elevation: image axes = (along-bridge X, height Z), depth = Y.
    x, y, z = pts[:, 0], pts[:, 1], pts[:, 2]
    W = H = 512
    x_lo, x_hi = np.percentile(x, [0.5, 99.5])
    z_lo, z_hi = np.percentile(z, [0.5, 99.5])
    xr = float(x_hi - x_lo) or 1.0
    zr = float(z_hi - z_lo) or 1.0
    px = np.clip(((x - x_lo) / xr * (W - 1)).astype(np.int64), 0, W - 1)
    pz = np.clip(((z_hi - z) / zr * (H - 1)).astype(np.int64), 0, H - 1)  # flip: top=high Z

    # z-buffer: keep the nearest point (smallest Y = closest to camera) per pixel.
    index_buf = np.full((H, W), -1, dtype=np.int64)
    depth_buf = np.full((H, W), np.inf)
    order = np.argsort(y)  # nearest first
    for i in order:
        r, ccol = pz[i], px[i]
        if y[i] < depth_buf[r, ccol]:
            depth_buf[r, ccol] = y[i]
            index_buf[r, ccol] = i

    img = np.full((H, W, 3), 255, dtype=np.uint8)
    filled = index_buf >= 0
    img[filled] = [60, 90, 140]
    try:
        # Anchor the weights next to the app package; a bare relative path
        # resolves against the process cwd and silently downloads a second
        # copy wherever the backend happens to be started from.
        weights = Path(__file__).resolve().parents[2] / "mobile_sam.pt"
        model = SAM(str(weights) if weights.exists() else "mobile_sam.pt")
        res = model(img, verbose=False)
    except Exception as exc:
        print(f"[point_cloud] SAM inference failed: {exc}")
        return None

    instances = np.full(pts.shape[0], -1, dtype=np.int64)
    n_inst = 0
    try:
        masks = res[0].masks
        if masks is not None:
            for inst_id, m in enumerate(masks.data.cpu().numpy()):
                mask = m.astype(bool)
                if mask.shape != (H, W):
                    continue
                pix = mask & filled
                pidx = index_buf[pix]
                pidx = pidx[pidx >= 0]
                if pidx.size >= 30:
                    instances[pidx] = inst_id
                    n_inst += 1
    except Exception as exc:
        print(f"[point_cloud] SAM mask back-projection failed: {exc}")
        return None

    return {"instances": instances.tolist(), "instance_count": int(n_inst)}


def segment_structure(
    points: Any,
    colors: Any = None,
    use_qwen: bool = True,
    use_sam: bool = False,
    clean: bool = True,
    report_context: str = "",
) -> Dict[str, Any]:
    """Segment a bridge scan into structural components + (optionally) name them.

    Cleaning (default on): vegetation/outliers are split off into a Noise class and
    excluded from the structural classification and the model renders. Geometric
    backbone: Z-banding + per-cell vertical variance â†’ ground / substructure / soffit
    / deck. Qwen (optional, default on): maps each region to an AASHTO/NJDOT element
    name + material, grounded by `report_context` when given. SAM (optional, default
    off): instance separation on an orthographic render. Returns per-point `labels`
    aligned to the input order plus a dynamic `classes` list."""
    pts_all = _as_xyz(points)
    n = int(pts_all.shape[0])
    if n < 20:
        return {"status": "insufficient_points", "point_count": n}

    sample_idx = _subsample(n, MAX_ANALYSIS_POINTS)
    sample = pts_all[sample_idx]

    # Clean first: noise/vegetation â†’ SEG_NOISE; classify only the kept structure.
    noise = denoise_mask(sample) if clean else np.zeros(sample.shape[0], dtype=bool)
    keep = ~noise
    sample_labels = np.full(sample.shape[0], SEG_NOISE, dtype=np.int64)
    if int(keep.sum()) >= 20:
        sub_labels, _info = _segment_geometric(sample[keep])
        sample_labels[keep] = sub_labels
    else:
        sample_labels[:] = SEG_UNCLASSIFIED  # too little left to clean meaningfully

    labels = _expand_labels(sample, sample_labels, pts_all)

    classes = [dict(c) for c in DEFAULT_CLASSES]
    counts = np.bincount(labels, minlength=len(classes))
    for c in classes:
        c["count"] = int(counts[c["id"]]) if c["id"] < counts.size else 0

    result: Dict[str, Any] = {
        "status": "success",
        "point_count": n,
        "noise_removed": int((labels == SEG_NOISE).sum()),
        "labels_b64u8": _b64_bytes(labels.astype(np.uint8)),
        "classes": classes,
        "named_by_qwen": False,
        "sam_used": False,
    }

    # The cleaned structure (no vegetation) drives both SAM and the Qwen renders.
    sub_pts = sample[keep]
    sub_only_labels = sample_labels[keep]

    if use_sam and sub_pts.shape[0] >= 50:
        sam_out = _refine_instances_with_sam(sub_pts, sub_only_labels)
        if sam_out is not None:
            inst_sample = np.full(sample.shape[0], -1, dtype=np.int64)
            inst_sample[keep] = np.asarray(sam_out["instances"], dtype=np.int64)
            inst_full = _expand_labels(sample, inst_sample, pts_all)
            result["instances_b64i32"] = _b64_bytes(inst_full.astype(np.int32))
            result["instance_count"] = sam_out["instance_count"]
            result["sam_used"] = True

    if use_sam:
        _release_gpu_models()

    if use_qwen and sub_pts.shape[0] >= 50:
        try:
            classes = _name_segments_with_qwen(sub_pts, sub_only_labels, classes, report_context)
            result["classes"] = classes
            result["named_by_qwen"] = any(c.get("material") for c in classes)
        except Exception as exc:
            print(f"[point_cloud] qwen naming skipped: {exc}")

    return result
