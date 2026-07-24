"""
Document RAG engine — Hybrid text + image pipeline.

Pipeline:
  1. PyMuPDF: extract text directly from each PDF page (fast, no LLM)
  2. PyMuPDF: extract embedded images (photos, diagrams, figures) per page
  3. PyMuPDF: render each page as PNG (for "show page N" requests)
  4. LightRAG: build knowledge graph from extracted text
  5. Query routing (LangGraph-style):
     - Text questions → KG hybrid query (with embedding fallback)
     - Image/visual questions → send relevant page images to vision model
     - "Show page N" → return rendered page image

Documents are cached by SHA-256 hash.
"""

import os
import json
import hashlib
import base64
import shutil
import io
import time
import threading
import numpy as np
import re as _re
from contextvars import ContextVar
from pathlib import Path
from typing import Optional, List, Dict

from . import llm_providers

# ── Cancellation + progress tracking ────────────────────────────────
_cancel_event = threading.Event()
_progress_info: Dict[str, object] = {
    "stage": "",
    "step": 0,
    "total_steps": 0,
    "started_at": 0.0,
    "elapsed": 0.0,
    "eta_seconds": None,
}
_progress_lock = threading.Lock()


class CancelledError(Exception):
    pass


def cancel_processing():
    """Signal that the current processing should be cancelled."""
    _cancel_event.set()


def _check_cancelled():
    """Raise CancelledError if cancellation was requested."""
    if _cancel_event.is_set():
        raise CancelledError("Processing cancelled by user.")


def _update_progress(stage: str, step: int, total_steps: int, eta_override: Optional[float] = None):
    """Update progress info for the frontend to poll."""
    now = time.time()
    with _progress_lock:
        started = _progress_info.get("started_at") or now
        elapsed = now - started
        if eta_override is not None:
            eta = eta_override
        elif step > 0 and total_steps > 0:
            per_step = elapsed / step
            remaining = total_steps - step
            eta = round(per_step * remaining, 1)
        else:
            eta = None
        _progress_info.update({
            "stage": stage,
            "step": step,
            "total_steps": total_steps,
            "elapsed": round(elapsed, 1),
            "eta_seconds": eta,
        })


def _start_eta_heartbeat(
    stage: str,
    estimated_seconds: float,
    stop_event: threading.Event,
    tick: float = 2.0,
) -> threading.Thread:
    """
    Spawn a background thread that updates eta_seconds and elapsed every `tick` seconds
    while a long-running step is executing. Uses an exponential floor so ETA never
    drops to 0 before the step actually finishes.
    """
    phase_start = time.time()

    def _run():
        while not stop_event.is_set():
            elapsed_phase = time.time() - phase_start
            remaining = max(4.0, estimated_seconds - elapsed_phase)
            with _progress_lock:
                started = _progress_info.get("started_at") or phase_start
                _progress_info["stage"] = stage
                _progress_info["elapsed"] = round(time.time() - started, 1)
                _progress_info["eta_seconds"] = round(remaining, 1)
            stop_event.wait(tick)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def _reset_progress():
    """Reset progress tracking for a new operation."""
    _cancel_event.clear()
    with _progress_lock:
        _progress_info.update({
            "stage": "",
            "step": 0,
            "total_steps": 0,
            "started_at": time.time(),
            "elapsed": 0.0,
            "eta_seconds": None,
        })


def get_progress() -> dict:
    """Return a snapshot of current processing progress."""
    with _progress_lock:
        snap = dict(_progress_info)
    # Compute elapsed dynamically so it stays current between _update_progress calls
    started = snap.get("started_at", 0.0)
    if started and snap.get("stage"):
        snap["elapsed"] = round(time.time() - started, 1)
    return snap


# ── Model / chunk settings (hosted-API build) ───────────────────────
# Kept as module attributes because /api/rag-settings reads them for display.
# Cerebras owns generation; the pinned TEI profile owns the vector space.
EMBED_MODEL = llm_providers.embedding_profile()["model"]
LLM_MODEL = llm_providers.generation_model()
VISION_MODEL = llm_providers.generation_model()
CHUNK_SIZE = int(os.getenv('CHUNK_SIZE', '500'))
CHUNK_OVERLAP = int(os.getenv('CHUNK_OVERLAP', '100'))
LIGHTRAG_TIKTOKEN_MODEL = os.getenv('LIGHTRAG_TIKTOKEN_MODEL', 'gpt-4o-mini')

# Centralized under the repo-relative data/ folder (see datapaths.py); the
# RAG_WORKING_DIR env override still applies.
from .datapaths import RAG_WORKING_DIR

# ── manifest for caching ────────────────────────────────────────────
_MANIFEST_PATH = Path(RAG_WORKING_DIR) / '_ingested_docs.json'
_ingested: Dict[str, dict] = {}
_lightrag_cache: Dict[str, object] = {}
# Tracks the doc hash for the document actually uploaded in this server session.
# Cleared on server start; only set after a real ingest_document() call.
# Prevents queries from falling back to stale manifest docs across restarts.
_active_session_hash: Optional[str] = None


def _resolve_tiktoken_model_name() -> str:
    """Pick a tiktoken model name that is available at runtime.

    In some packaged (PyInstaller) environments, o200k_base may be unavailable.
    """
    primary = LIGHTRAG_TIKTOKEN_MODEL
    candidates = [
        primary,
        'gpt-4-turbo',
        'gpt-3.5-turbo',
    ]
    try:
        import tiktoken

        for model_name in candidates:
            try:
                tiktoken.encoding_for_model(model_name)
                return model_name
            except Exception:
                continue
    except Exception:
        # If tiktoken itself cannot be imported, let LightRAG raise a useful error.
        pass

    # Last-resort fallback to a broadly supported model token map.
    return 'gpt-3.5-turbo'


def _get_pymupdf_module():
    """Import PyMuPDF using either the new or legacy module name."""
    try:
        import pymupdf as fitz_module
        return fitz_module
    except ImportError:
        try:
            import fitz as fitz_module
            return fitz_module
        except ImportError as exc:
            raise RuntimeError(
                'PyMuPDF is required for hybrid vision indexing. Install dependencies with: pip install -r backend/requirements.txt'
            ) from exc


def _load_manifest() -> Dict[str, dict]:
    if _MANIFEST_PATH.exists():
        try:
            data = json.loads(_MANIFEST_PATH.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _active_embedding_profile() -> dict:
    return dict(llm_providers.embedding_profile())


def _validated_cached_matrix(value, rows: int, cached_profile) -> Optional[np.ndarray]:
    """Return a cache matrix only when profile, rows, and width all match."""
    if not llm_providers.embedding_profile_matches(cached_profile):
        return None
    if value is None:
        return None
    try:
        matrix = np.asarray(value, dtype=np.float32)
    except (TypeError, ValueError):
        return None
    expected_dim = _active_embedding_profile()["dimension"]
    if matrix.ndim != 2 or matrix.shape != (rows, expected_dim):
        return None
    if not np.isfinite(matrix).all():
        return None
    return matrix


def _entry_embeddings_compatible(entry: dict) -> bool:
    profile = entry.get("embedding_profile")
    chunks = entry.get("chunks") or []
    descriptions = entry.get("image_descriptions") or []
    text_ok = not chunks or _validated_cached_matrix(
        entry.get("embeddings"), len(chunks), profile
    ) is not None
    images_ok = not descriptions or _validated_cached_matrix(
        entry.get("image_embeddings"), len(descriptions), profile
    ) is not None
    return text_ok and images_ok


def _save_manifest() -> None:
    _MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    meta = {}
    for h, v in _ingested.items():
        emb = v.get("embeddings")
        img_emb = v.get("image_embeddings")
        meta[h] = {
            "pages": v.get("pages", []),
            "chunks": v.get("chunks", []),
            "page_images": v.get("page_images", []),
            "extracted_images": v.get("extracted_images", []),
            "image_descriptions": v.get("image_descriptions", []),
            "kg_ready": v.get("kg_ready", False),
            "embedding_profile": v.get("embedding_profile"),
            "embeddings": emb.tolist() if isinstance(emb, np.ndarray) else (emb if emb else None),
            "image_embeddings": img_emb.tolist() if isinstance(img_emb, np.ndarray) else (img_emb if img_emb else None),
        }
    _MANIFEST_PATH.write_text(json.dumps(meta))


def _file_hash(file_path: str) -> str:
    h = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 16), b''):
            h.update(chunk)
    return h.hexdigest()


def _resize_image_b64(image_path: str, max_dim: int = 768) -> str:
    """Read an image file, resize if larger than max_dim, return JPEG base64."""
    from PIL import Image
    import io
    img = Image.open(image_path)
    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    # JPEG only supports RGB/L — convert palette ('P'), RGBA, CMYK, LA, etc.
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=85)
    return base64.b64encode(buf.getvalue()).decode('utf-8')


def _resize_bytes_b64(data: bytes, max_dim: int = 768) -> str:
    """Resize raw image bytes and return JPEG base64."""
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(data))
    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    # JPEG only supports RGB/L — convert palette ('P'), RGBA, CMYK, LA, etc.
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=85)
    return base64.b64encode(buf.getvalue()).decode('utf-8')


def _query_page_image(image_path: str, question: str) -> str:
    """Send a page image and question to the configured Cerebras endpoint."""
    try:
        img_b64 = _resize_image_b64(image_path)
        return llm_providers.generate(question, images_b64=[img_b64], timeout=300, num_predict=1024)
    except Exception as exc:
        print(f"  [vision] Failed to query page image: {exc}")
        return ""


def _query_image_bytes(data: bytes, question: str) -> str:
    """Send raw image bytes + question to the active provider's vision model."""
    try:
        img_b64 = _resize_bytes_b64(data)
        return llm_providers.generate(question, images_b64=[img_b64], timeout=300, num_predict=1024)
    except Exception as exc:
        print(f"  [vision] Failed to query image: {exc}")
        return ""


# ── PDF text + image extraction (PyMuPDF) ───────────────────────────
_PHOTO_NUMBER_RE = _re.compile(r'\b(?:photo|figure|fig\.?|image|img)\s*[:#-]?\s*(\d+)\b', _re.IGNORECASE)


def _extract_text_blocks_with_bbox(page) -> List[dict]:
    blocks: List[dict] = []
    try:
        data = page.get_text("dict")
        for b in data.get("blocks", []):
            if b.get("type") != 0:
                continue
            lines = []
            for ln in b.get("lines", []):
                parts = [sp.get("text", "") for sp in ln.get("spans", []) if sp.get("text")]
                if parts:
                    lines.append(''.join(parts))
            text = ' '.join(lines).strip()
            if not text:
                continue
            bbox = b.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            blocks.append({"text": text, "bbox": bbox})
    except Exception:
        pass
    return blocks


def _parse_photo_number(text: str) -> Optional[int]:
    if not text:
        return None
    m = _PHOTO_NUMBER_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _nearest_caption_for_rect(text_blocks: List[dict], rect) -> tuple[Optional[str], Optional[int]]:
    """Find a likely caption nearest/below image rect and parse photo number if present."""
    if rect is None or not text_blocks:
        return None, None

    candidates = []
    rx0, ry0, rx1, ry1 = rect.x0, rect.y0, rect.x1, rect.y1
    for b in text_blocks:
        bx0, by0, bx1, by1 = b["bbox"]
        # Horizontal overlap ratio
        overlap = max(0.0, min(rx1, bx1) - max(rx0, bx0))
        overlap_ratio = overlap / max(1.0, (bx1 - bx0))

        # Prioritize captions just below image, fallback nearby above.
        if by0 >= ry1:
            distance = by0 - ry1
            if distance <= 140 and overlap_ratio >= 0.2:
                candidates.append((distance, 0, b))
        elif by1 <= ry0:
            distance = ry0 - by1
            if distance <= 80 and overlap_ratio >= 0.2:
                candidates.append((distance, 1, b))

    if not candidates:
        return None, None
    candidates.sort(key=lambda x: (x[1], x[0]))
    caption = candidates[0][2]["text"]
    return caption, _parse_photo_number(caption)


def _extract_text_and_images(file_path: str, doc_hash: str) -> tuple:
    """
        Extract text and visual assets from a PDF using PyMuPDF.
    Returns (pages, extracted_images) where:
      - pages: [{"page": N, "text": "..."}, ...]
            - extracted_images: [{"page": N, "index": I, "path": "...", "width": W, "height": H, ...}, ...]
    """
    pymupdf = _get_pymupdf_module()

    images_dir = Path(RAG_WORKING_DIR) / 'extracted_images' / doc_hash[:16]
    images_dir.mkdir(parents=True, exist_ok=True)

    pages: List[dict] = []
    extracted_images: List[dict] = []

    pdf = pymupdf.open(file_path)
    try:
        for page_num in range(len(pdf)):
            page = pdf[page_num]
            pg = page_num + 1
            text_blocks = _extract_text_blocks_with_bbox(page)
            seen_instances = set()
            page_image_counter = 0

            # ── Extract text ──
            text = page.get_text("text").strip()
            pages.append({"page": pg, "text": text if text else f"[Page {pg}: no text content]"})

            # ── Extract embedded images by placement rects (prevents index drift) ──
            img_list = page.get_images(full=True)
            for img_idx, img_info in enumerate(img_list):
                xref = img_info[0]
                try:
                    rects = page.get_image_rects(xref)
                    if not rects:
                        rects = [None]

                    for rect_idx, rect in enumerate(rects):
                        if rect is not None:
                            # Dedupe repeated placements.
                            key = (xref, round(rect.x0, 1), round(rect.y0, 1), round(rect.x1, 1), round(rect.y1, 1))
                            if key in seen_instances:
                                continue
                            seen_instances.add(key)

                            if rect.width < 80 or rect.height < 80:
                                continue

                            pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=rect, alpha=False)
                            out_path = images_dir / f"page_{pg:03d}_img_{img_idx:02d}_inst_{rect_idx:02d}.png"
                            pix.save(str(out_path))
                            w, h, ext = pix.width, pix.height, "png"
                            bbox = [rect.x0, rect.y0, rect.x1, rect.y1]
                        else:
                            base_image = pdf.extract_image(xref)
                            if not base_image or not base_image.get("image"):
                                continue
                            img_data = base_image["image"]
                            ext = base_image.get("ext", "png")
                            w = base_image.get("width", 0)
                            h = base_image.get("height", 0)
                            if w < 80 or h < 80:
                                continue
                            out_path = images_dir / f"page_{pg:03d}_img_{img_idx:02d}_inst_{rect_idx:02d}.{ext}"
                            out_path.write_bytes(img_data)
                            bbox = None

                        caption, photo_number = _nearest_caption_for_rect(text_blocks, rect) if rect is not None else (None, None)
                        image_id = f"P{pg:03d}-I{page_image_counter:03d}"
                        page_image_counter += 1

                        extracted_images.append({
                            "image_id": image_id,
                            "page": pg,
                            "index": img_idx,
                            "instance_index": rect_idx,
                            "xref": xref,
                            "path": str(out_path),
                            "width": w,
                            "height": h,
                            "ext": ext,
                            "bbox": bbox,
                            "caption": caption,
                            "photo_number": photo_number,
                            "source": "embedded_rect" if rect is not None else "embedded_raw",
                        })
                except Exception as exc:
                    print(f"  [extract] Failed to extract image xref={xref} page {pg}: {exc}")

            # ── Caption-driven fallback splitting for scanned pages with merged visuals ──
            if page_image_counter <= 1:
                caption_blocks = [b for b in text_blocks if _parse_photo_number(b.get("text", "")) is not None]
                caption_blocks.sort(key=lambda b: b["bbox"][1])
                if len(caption_blocks) >= 2:
                    page_rect = page.rect
                    segment_top = page_rect.y0 + 5
                    for cap_idx, cap in enumerate(caption_blocks):
                        cap_x0, cap_y0, cap_x1, cap_y1 = cap["bbox"]
                        segment_bottom = cap_y0 - 6
                        if segment_bottom - segment_top < 120:
                            segment_top = cap_y1 + 6
                            continue

                        clip = pymupdf.Rect(page_rect.x0 + 8, segment_top, page_rect.x1 - 8, segment_bottom)
                        if clip.height < 120 or clip.width < 120:
                            segment_top = cap_y1 + 6
                            continue

                        try:
                            pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=clip, alpha=False)
                            out_path = images_dir / f"page_{pg:03d}_caption_{cap_idx:02d}.png"
                            pix.save(str(out_path))
                            image_id = f"P{pg:03d}-C{cap_idx:03d}"
                            extracted_images.append({
                                "image_id": image_id,
                                "page": pg,
                                "index": 1000 + cap_idx,
                                "instance_index": cap_idx,
                                "xref": None,
                                "path": str(out_path),
                                "width": pix.width,
                                "height": pix.height,
                                "ext": "png",
                                "bbox": [clip.x0, clip.y0, clip.x1, clip.y1],
                                "caption": cap.get("text"),
                                "photo_number": _parse_photo_number(cap.get("text", "")),
                                "source": "caption_region",
                            })
                            page_image_counter += 1
                        except Exception:
                            pass

                        segment_top = cap_y1 + 6
    finally:
        pdf.close()

    return pages, extracted_images


# ── PDF → page images (PyMuPDF) — for "show page" requests ──────────
def _render_pdf_pages(file_path: str, doc_hash: str, dpi: int = 150) -> List[str]:
    """Render each PDF page as a PNG image. Returns list of image paths."""
    pymupdf = _get_pymupdf_module()

    images_dir = Path(RAG_WORKING_DIR) / 'page_images' / doc_hash[:16]
    images_dir.mkdir(parents=True, exist_ok=True)

    image_paths: List[str] = []
    pdf = pymupdf.open(file_path)
    try:
        for page_num in range(len(pdf)):
            page = pdf[page_num]
            mat = pymupdf.Matrix(dpi / 72, dpi / 72)
            pix = page.get_pixmap(matrix=mat)
            out_path = images_dir / f"page_{page_num + 1:03d}.png"
            pix.save(str(out_path))
            image_paths.append(str(out_path))
    finally:
        pdf.close()
    return image_paths


# ── LightRAG knowledge graph ────────────────────────────────────────
_KG_EMBED_INPUT_TYPE = ContextVar("kg_embed_input_type", default="passage")


async def _get_lightrag(doc_hash: str, model: str = None):
    llm = model or LLM_MODEL
    embed_profile = _active_embedding_profile()
    profile_token = hashlib.sha256(
        json.dumps(embed_profile, sort_keys=True).encode("utf-8")
    ).hexdigest()[:10]
    key = f"{doc_hash[:16]}_{llm}_{profile_token}"
    if key in _lightrag_cache:
        return _lightrag_cache[key]

    from lightrag import LightRAG
    from lightrag.llm.openai import openai_complete_if_cache
    from lightrag.utils import EmbeddingFunc

    # Never open an arbitrary legacy vector store with a new dimension/model.
    # A profile-specific directory makes model changes a safe fresh index.
    working_dir = str(Path(RAG_WORKING_DIR) / f"kg_{doc_hash[:16]}_{profile_token}")
    Path(working_dir).mkdir(parents=True, exist_ok=True)

    # KG text completion uses Cerebras' OpenAI-compatible endpoint. Embeddings
    # remain independent and are served by the pinned Nomic TEI sidecar.
    _CB_BASE = os.getenv("CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1")
    _CB_KEY = os.getenv("CEREBRAS_API_KEY", "").strip()
    _CB_LLM = llm_providers.generation_model()

    def _kg_backend():
        if not _CB_KEY:
            raise RuntimeError("CEREBRAS_API_KEY is required for RAG completion")
        return _CB_BASE, _CB_KEY, _CB_LLM

    async def _kg_complete(prompt, system_prompt=None, history_messages=None,
                           keyword_extraction=False, **kw):
        base, key_, mdl = _kg_backend()
        return await openai_complete_if_cache(
            mdl, prompt, system_prompt=system_prompt,
            history_messages=history_messages or [], base_url=base, api_key=key_,
            keyword_extraction=keyword_extraction, **kw)

    async def _kg_embed(texts):
        # LightRAG exposes one callback for inserts and lookups. A context-local
        # role keeps concurrent calls from mixing Nomic passage/query prefixes.
        import asyncio as _aio
        return await _aio.to_thread(
            llm_providers.embed,
            list(texts),
            _KG_EMBED_INPUT_TYPE.get(),
        )

    rag = LightRAG(
        working_dir=working_dir,
        llm_model_func=_kg_complete,
        llm_model_name=_kg_backend()[2],
        tiktoken_model_name=_resolve_tiktoken_model_name(),
        llm_model_max_async=1,
        embedding_func=EmbeddingFunc(
            embedding_dim=embed_profile["dimension"],
            max_token_size=8192,
            func=_kg_embed,
        ),
        embedding_batch_num=8,
        embedding_func_max_async=2,
        chunk_token_size=600,
        chunk_overlap_token_size=100,
        max_extract_input_tokens=6000,
        entity_extract_max_gleaning=0,
    )
    await rag.initialize_storages()
    _lightrag_cache[key] = rag
    return rag


async def _build_kg(doc_hash: str, full_text: str) -> bool:
    try:
        rag = await _get_lightrag(doc_hash)
        token = _KG_EMBED_INPUT_TYPE.set("passage")
        try:
            await rag.ainsert(full_text)
        finally:
            _KG_EMBED_INPUT_TYPE.reset(token)
        return True
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"[rag_engine] KG building failed: {exc}")
        return False


_KG_NO_CONTEXT_RE = _re.compile(
    r'\[no.?context\]|not able to provide|no relevant|cannot provide|'
    r'don\'t have (?:enough|specific|any)|no information|insufficient context',
    _re.IGNORECASE,
)

async def _query_kg(question: str, doc_hash: str, model: str = None) -> Optional[str]:
    from lightrag import QueryParam
    try:
        rag = await _get_lightrag(doc_hash, model=model)
        token = _KG_EMBED_INPUT_TYPE.set("query")
        try:
            result = await rag.aquery(question, param=QueryParam(mode="hybrid"))
        finally:
            _KG_EMBED_INPUT_TYPE.reset(token)
        if result and result.strip():
            # LightRAG returns a "[no-context]" boilerplate when it cannot answer.
            # Treat these as a miss so the embedding fallback takes over.
            if _KG_NO_CONTEXT_RE.search(result):
                print(f"[rag_engine] KG returned no-context signal — falling back to embeddings")
                return None
            return result
    except Exception as exc:
        print(f"[rag_engine] KG query failed: {exc}")
    return None


def _extract_json_object(text: str) -> Optional[dict]:
    s = (text or '').strip()
    if not s:
        return None
    try:
        payload = json.loads(s)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass

    m = _re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", s, flags=_re.IGNORECASE)
    if m:
        try:
            payload = json.loads(m.group(1))
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass

    m2 = _re.search(r"(\{[\s\S]*\})", s)
    if m2:
        try:
            payload = json.loads(m2.group(1))
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
    return None


def _detect_defects_with_bboxes(image_path: str, defect_query: str) -> List[dict]:
    """Ask vision model for defect detections with normalized bounding boxes."""
    img_b64 = _resize_image_b64(image_path)
    prompt = (
        "You are a bridge inspection visual detector. "
        f"Find visible defect regions relevant to this request: {defect_query}. "
        "Return only valid JSON object exactly in this schema: "
        "{\"detections\":[{\"label\":\"string\",\"confidence\":0.0,\"bbox\":[x,y,w,h]}]}. "
        "bbox values must be normalized from 0 to 1. "
        "If no defect is visible, return {\"detections\":[]}."
    )
    try:
        raw = llm_providers.generate(prompt, images_b64=[img_b64], timeout=300, num_predict=512)
        payload = _extract_json_object(raw) or {}
        dets = payload.get('detections', []) if isinstance(payload, dict) else []
        clean: List[dict] = []
        for d in dets if isinstance(dets, list) else []:
            if not isinstance(d, dict):
                continue
            bbox = d.get('bbox')
            if not isinstance(bbox, list) or len(bbox) != 4:
                continue
            try:
                x, y, w, h = [float(v) for v in bbox]
            except Exception:
                continue
            x = max(0.0, min(1.0, x))
            y = max(0.0, min(1.0, y))
            w = max(0.0, min(1.0 - x, w))
            h = max(0.0, min(1.0 - y, h))
            if w <= 0.01 or h <= 0.01:
                continue
            clean.append({
                'label': str(d.get('label') or 'defect'),
                'confidence': float(d.get('confidence') or 0.5),
                'bbox': [x, y, w, h],
            })
        return clean
    except Exception as exc:
        print(f"  [vision] Defect bbox detection failed: {exc}")
        return []


def _bbox_iou(a: List[float], b: List[float]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ax2, ay2 = ax + aw, ay + ah
    bx2, by2 = bx + bw, by + bh

    ix1 = max(ax, bx)
    iy1 = max(ay, by)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0

    ua = (aw * ah) + (bw * bh) - inter
    if ua <= 1e-8:
        return 0.0
    return inter / ua


def _anomaly_score_for_bbox(gray_img: np.ndarray, bbox: List[float]) -> float:
    """Compute deterministic texture anomaly score inside a normalized bbox."""
    h, w = gray_img.shape[:2]
    x, y, bw, bh = bbox
    x0 = max(0, min(w - 1, int(x * w)))
    y0 = max(0, min(h - 1, int(y * h)))
    x1 = max(x0 + 1, min(w, int((x + bw) * w)))
    y1 = max(y0 + 1, min(h, int((y + bh) * h)))

    crop = gray_img[y0:y1, x0:x1]
    if crop.size < 64:
        return 0.0

    # Blend edge energy and local contrast as a lightweight CV sanity check.
    gx = np.abs(np.diff(crop, axis=1))
    gy = np.abs(np.diff(crop, axis=0))
    edge_energy = float(gx.mean() + gy.mean()) / 2.0
    contrast = float(np.std(crop))
    return max(0.0, min(1.0, (0.65 * edge_energy) + (0.35 * contrast)))


def _refine_defect_detections_hybrid(
    image_path: str,
    detections: List[dict],
    segmentation_mode: str = 'balanced',
) -> List[dict]:
    """
    Hybrid refinement: vision model proposes detections, deterministic CV validates texture anomaly,
    then NMS removes overlaps.
    """
    if not detections:
        return []

    mode = (segmentation_mode or 'balanced').strip().lower()
    if mode not in {'fast', 'balanced', 'high_precision'}:
        mode = 'balanced'

    try:
        from PIL import Image
        img = Image.open(image_path).convert('L')
        gray = np.asarray(img, dtype=np.float32) / 255.0
    except Exception:
        gray = None

    if mode == 'fast':
        min_conf = 0.20
        min_anom = 0.0
        iou_thr = 0.40
    elif mode == 'high_precision':
        min_conf = 0.45
        min_anom = 0.09
        iou_thr = 0.25
    else:
        min_conf = 0.30
        min_anom = 0.05
        iou_thr = 0.33

    scored: List[dict] = []
    for d in detections:
        bbox = d.get('bbox')
        conf = float(d.get('confidence') or 0.0)
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        anomaly = _anomaly_score_for_bbox(gray, bbox) if gray is not None else 0.0
        if conf < min_conf:
            continue
        if mode != 'fast' and anomaly < min_anom:
            continue

        combined = (0.70 * conf) + (0.30 * anomaly)
        scored.append({
            'label': str(d.get('label') or 'defect'),
            'confidence': conf,
            'bbox': bbox,
            'anomaly_score': round(anomaly, 3),
            '_score': combined,
        })

    # Avoid empty overlays if filter was too strict.
    if not scored:
        top = sorted(detections, key=lambda x: float(x.get('confidence') or 0.0), reverse=True)[:1]
        return [{
            'label': str(t.get('label') or 'defect'),
            'confidence': float(t.get('confidence') or 0.0),
            'bbox': t.get('bbox'),
            'anomaly_score': 0.0,
        } for t in top if isinstance(t.get('bbox'), list) and len(t.get('bbox')) == 4]

    scored.sort(key=lambda d: d['_score'], reverse=True)
    kept: List[dict] = []
    for cand in scored:
        if any(_bbox_iou(cand['bbox'], k['bbox']) > iou_thr for k in kept):
            continue
        kept.append(cand)

    for d in kept:
        d.pop('_score', None)
    return kept


def _annotate_image_with_detections(image_path: str, detections: List[dict]) -> Optional[dict]:
    """Draw bbox overlays on image and return base64 payload."""
    if not detections:
        return None
    try:
        from PIL import Image, ImageDraw
        p = Path(image_path)
        img = Image.open(str(p)).convert('RGB')
        draw = ImageDraw.Draw(img)
        w, h = img.size
        for d in detections:
            x, y, bw, bh = d['bbox']
            x0 = int(x * w)
            y0 = int(y * h)
            x1 = int((x + bw) * w)
            y1 = int((y + bh) * h)
            draw.rectangle([x0, y0, x1, y1], outline=(255, 64, 64), width=3)
            label = f"{d.get('label', 'defect')} ({d.get('confidence', 0):.2f})"
            draw.rectangle([x0, max(0, y0 - 18), min(w, x0 + 180), y0], fill=(255, 64, 64))
            draw.text((x0 + 3, max(0, y0 - 16)), label, fill=(255, 255, 255))

        buf = io.BytesIO()
        img.save(buf, format='PNG')
        b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
        return {
            'filename': f"{p.stem}_annotated.png",
            'mime': 'image/png',
            'data': b64,
        }
    except Exception as exc:
        print(f"  [vision] Annotation rendering failed: {exc}")
        return None


# ── text chunking (embedding fallback) ──────────────────────────────
def _split_page_chunks(pages: List[dict], chunk_size: int = None, overlap: int = None) -> List[dict]:
    if chunk_size is None:
        chunk_size = CHUNK_SIZE
    if overlap is None:
        overlap = CHUNK_OVERLAP
    chunks: List[dict] = []
    for pg in pages:
        text = pg["text"]
        if text.startswith("[Page") and "no text content]" in text:
            continue
        words = text.split()
        if not words:
            continue
        start = 0
        while start < len(words):
            end = min(start + chunk_size, len(words))
            chunks.append({"page": pg["page"], "text": ' '.join(words[start:end])})
            if end == len(words):
                break
            start = max(end - overlap, 0)
    return chunks


# ── embedding helpers ────────────────────────────────────────────────
def _embed(texts: List[str], input_type: str = "query") -> Optional[np.ndarray]:
    """Embed in the active profile, returning None for safe lexical fallback."""
    try:
        vecs = llm_providers.embed(texts, input_type=input_type)
        if vecs is not None and len(vecs):
            return np.array(vecs, dtype=np.float32)
    except Exception as exc:
        print(f"[rag_engine] Embedding failed: {exc}")
    return None


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    a_norm = a / (np.linalg.norm(a) + 1e-10)
    b_norm = b / (np.linalg.norm(b, axis=1, keepdims=True) + 1e-10)
    return b_norm @ a_norm


def _get_relevant(question: str, chunks: List[dict], k: int = 8,
                  embeddings: Optional[np.ndarray] = None) -> List[dict]:
    if embeddings is not None and len(embeddings) == len(chunks):
        try:
            embeddings = np.asarray(embeddings, dtype=np.float32)
        except (TypeError, ValueError):
            embeddings = None
    if embeddings is not None and len(embeddings) == len(chunks):
        q_emb = _embed([question], input_type="query")
        if (
            q_emb is not None
            and q_emb.ndim == 2
            and embeddings.ndim == 2
            and q_emb.shape[1] == embeddings.shape[1]
        ):
            sims = _cosine_similarity(q_emb[0], embeddings)
            top_idx = np.argsort(sims)[::-1][:k]
            return [chunks[i] for i in top_idx]
        if q_emb is not None:
            print("[rag_engine] Cached embedding width is incompatible; using lexical retrieval")
    qtokens = set(tok.lower() for tok in question.split() if tok.isalnum())
    scored = sorted(
        chunks,
        key=lambda c: len(qtokens & set(t.lower() for t in c["text"].split() if t.isalnum())),
        reverse=True,
    )
    return scored[:k] if scored else chunks[:k]


def _compute_embeddings(chunks: List[dict]) -> Optional[np.ndarray]:
    if not chunks:
        return None
    texts = [c["text"] for c in chunks]
    all_embs = []
    batch_size = 16
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        emb = _embed(batch, input_type="passage")
        if emb is None:
            print(f"[rag_engine] Embedding batch {i // batch_size} failed")
            return None
        all_embs.append(emb)
    return np.vstack(all_embs) if all_embs else None


_BROAD_QUESTION_RE = _re.compile(
    r'\b(summar\w*|overview|outline|describe the (?:entire|whole|full)|what is (?:this|the) (?:report|document)|'
    r'how many (?:pages|images|photos|figures|pictures|sections|tables)|'
    r'table of contents|list (?:all|the) (?:sections|topics|chapters|findings)|'
    r'main (?:findings|points|conclusions|topics)|key (?:findings|takeaways|points))\b',
    _re.IGNORECASE,
)


def _is_broad_question(question: str) -> bool:
    return bool(_BROAD_QUESTION_RE.search(question))


# ── Query classification (LangGraph-style router) ───────────────────
_VISION_ANALYSIS_RE = _re.compile(
    r'\b(analyz|inspect|examin|assess|evaluat|identif|detect|diagnos)\w*\s+'
    r'(?:the\s+)?(?:image|photo|picture|figure|fig|defect|crack|corrosion|rust|'
    r'spall|damage|deteriorat|condition|structur)',
    _re.IGNORECASE,
)
_VISION_KEYWORDS_RE = _re.compile(
    r'\b(?:defect|crack|corrosion|rust|spall|spalling|delamination|erosion|'
    r'deteriorat\w*|fracture|pit|hole|wear|fatigue|buckling|distortion|'
    r'what (?:do you|can you) see|describe (?:the|this) (?:image|photo|picture)|'
    r'look at (?:the|this) (?:image|photo|picture)|'
    r'(?:image|photo|picture|figure) (?:on |from )?page)\b',
    _re.IGNORECASE,
)
_IMAGE_CONTENT_RE = _re.compile(
    r'\b(?:photo|picture|image|figure|diagram|chart|graph|drawing|sketch|'
    r'illustration|map|plan|elevation|cross.?section|schematic|blueprint)\b',
    _re.IGNORECASE,
)
_PAGE_REF_RE = _re.compile(r'page\s*(\d+)', _re.IGNORECASE)

_SHOW_PAGE_RE = _re.compile(
    r'(?:show|display|view|see|get|open)\s+(?:me\s+)?(?:page|pg)\s*#?\s*(\d+)',
    _re.IGNORECASE,
)
_SHOW_PHOTO_RE = _re.compile(
    r'(?:show|display|view|see|get|open)\s+(?:me\s+)?(?:photo|picture|image|img|figure|fig)\s*#?\s*(\d+)',
    _re.IGNORECASE,
)
_LIST_IMAGES_RE = _re.compile(
    r'(?:show|display|list|view|see|get)\s+(?:all\s+)?(?:photos|pictures|images|figures)',
    _re.IGNORECASE,
)
_LIST_PAGES_RE = _re.compile(
    r'(?:show|display|list|view|see|get)\s+(?:all\s+)?(?:pages)',
    _re.IGNORECASE,
)
_COUNT_IMAGES_RE = _re.compile(
    r'\b(?:how many|count|number of)\s+(?:photos|pictures|images|figures)\b',
    _re.IGNORECASE,
)
# Matches "show/display/view/see me the Deck Cross Section Sketch" — show by name, not number
_SHOW_NAMED_IMAGE_RE = _re.compile(
    r'^\s*(?:show|display|view|see|get|open)\s+(?:me\s+)?(?:the\s+)?\w',
    _re.IGNORECASE,
)


def classify_query(question: str, has_images: bool = False) -> str:
    """
    LangGraph-style router: classify a question into a processing path.
    Returns: 'show_page' | 'show_photo' | 'show_named_image' | 'list_pages' | 'list_images' | 'vision' | 'text'
    """
    if detect_page_request(question) is not None:
        return 'show_page'
    if detect_photo_request(question) is not None:
        return 'show_photo'
    if detect_list_pages_request(question):
        return 'list_pages'
    if detect_list_images_request(question):
        return 'list_images'
    # "show the Deck Cross Section Sketch" — display named image, no analysis
    if _SHOW_NAMED_IMAGE_RE.search(question) and _IMAGE_CONTENT_RE.search(question):
        return 'show_named_image'
    # Vision path: explicit vision/defect keywords, or asking about images and we have images
    if _VISION_ANALYSIS_RE.search(question) or _VISION_KEYWORDS_RE.search(question):
        return 'vision'
    # Route any image/photo/figure intent to vision path even when no extracted images are present,
    # so fallback page scans can still answer visual questions.
    if _IMAGE_CONTENT_RE.search(question):
        return 'vision'
    return 'text'


def detect_page_request(question: str) -> Optional[int]:
    m = _SHOW_PAGE_RE.search(question)
    return int(m.group(1)) if m else None


def detect_photo_request(question: str) -> Optional[int]:
    m = _SHOW_PHOTO_RE.search(question)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None


def detect_photo_reference(question: str) -> Optional[int]:
    """Return photo/figure reference number when mentioned anywhere in question."""
    m = _PHOTO_NUMBER_RE.search(question)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def detect_list_images_request(question: str) -> bool:
    return bool(_LIST_IMAGES_RE.search(question))


def detect_list_pages_request(question: str) -> bool:
    return bool(_LIST_PAGES_RE.search(question))


def detect_count_images_request(question: str) -> bool:
    return bool(_COUNT_IMAGES_RE.search(question))


def detect_vision_analysis_request(question: str) -> bool:
    return bool(_VISION_ANALYSIS_RE.search(question) or _VISION_KEYWORDS_RE.search(question))


def get_relevant_page_images(question: str, doc_hash: str = None) -> List[str]:
    """Return page image paths relevant to the question."""
    global _ingested
    if not _ingested:
        _ingested = _load_manifest()
    if not _ingested:
        return []

    entry = _ingested.get(doc_hash) if doc_hash else None
    if not entry:
        entry = list(_ingested.values())[-1] if _ingested else None
    if not entry:
        return []

    all_files = entry.get("page_images", [])
    if not all_files:
        return []

    page_match = _PAGE_REF_RE.search(question)
    if page_match:
        target = int(page_match.group(1))
        for f in all_files:
            if f"page_{target:03d}" in f:
                return [f]
        if 1 <= target <= len(all_files):
            return [all_files[target - 1]]

    return all_files[:5]


def get_relevant_extracted_images(question: str, doc_hash: str = None, k: int = 3) -> List[dict]:
    """Return extracted (embedded) images relevant to the question, using embedding similarity."""
    global _ingested
    if not _ingested:
        _ingested = _load_manifest()
    if not _ingested:
        return []

    entry = _ingested.get(doc_hash) if doc_hash else None
    if not entry:
        entry = list(_ingested.values())[-1] if _ingested else None
    if not entry:
        return []

    images = entry.get("extracted_images", [])
    descriptions = entry.get("image_descriptions", [])
    if not images:
        return []

    # If a specific photo is mentioned, resolve by linked photo_number first,
    # then by caption-filtered index (matching how the report numbers its photos).
    photo_ref = detect_photo_reference(question)
    if photo_ref is not None:
        matched = [img for img in images if img.get("photo_number") == photo_ref]
        if matched:
            return matched[:k]
        # Caption-filtered photo index fallback
        photo_imgs = _photo_captioned_images(images)
        if 1 <= photo_ref <= len(photo_imgs):
            return [photo_imgs[photo_ref - 1]]

    # If a specific page is mentioned, filter to that page.
    page_match = _PAGE_REF_RE.search(question)
    if page_match:
        target_page = int(page_match.group(1))
        page_images = [img for img in images if img.get("page") == target_page]
        if page_images:
            return page_images[:k]

    # Use embedding similarity on image descriptions
    img_emb = _validated_cached_matrix(
        entry.get("image_embeddings"),
        len(descriptions),
        entry.get("embedding_profile"),
    )
    if img_emb is not None and descriptions:
        q_emb = _embed([question], input_type="query")
        if q_emb is not None and q_emb.shape[1] == img_emb.shape[1]:
            sims = _cosine_similarity(q_emb[0], img_emb)
            top_idx = np.argsort(sims)[::-1][:k]
            return [images[i] for i in top_idx if i < len(images)]

    # Fallback: return first k images
    return images[:k]


def get_page_image_b64(page_num: int, doc_hash: str = None) -> Optional[dict]:
    """Get a specific page image as base64."""
    global _ingested
    if not _ingested:
        _ingested = _load_manifest()
    if not _ingested:
        return None

    entry = _ingested.get(doc_hash) if doc_hash else None
    if not entry:
        entry = list(_ingested.values())[-1] if _ingested else None
    if not entry:
        return None

    all_files = entry.get("page_images", [])
    if page_num < 1 or page_num > len(all_files):
        return None

    fpath = Path(all_files[page_num - 1])
    if not fpath.exists():
        return None
    try:
        data = fpath.read_bytes()
        b64 = base64.b64encode(data).decode('utf-8')
        return {"filename": fpath.name, "mime": "image/png", "data": b64}
    except Exception:
        return None


def _photo_captioned_images(images: List[dict]) -> List[dict]:
    """Return only images whose captions identify them as inspection photos (contain 'Category:').
    These are the images numbered sequentially in the report as Photo 1, Photo 2, etc."""
    return [img for img in images if 'category' in (img.get('caption') or '').lower()]


def get_photo_image_b64(photo_num: int, doc_hash: str = None) -> Optional[dict]:
    """Get an extracted image mapped to a report photo/figure number."""
    global _ingested
    if not _ingested:
        _ingested = _load_manifest()
    if not _ingested:
        return None

    entry = _ingested.get(doc_hash) if doc_hash else None
    if not entry and _ingested:
        entry = list(_ingested.values())[-1]
    if not entry:
        return None

    images = entry.get("extracted_images", [])
    target = None

    # First try explicit photo_number match from PDF captions
    for img in images:
        if img.get("photo_number") == photo_num:
            target = img
            break

    # Fallback: index within caption-identified photos only
    # (skips logos, headers, etc. that are not numbered report photos)
    if target is None:
        photo_imgs = _photo_captioned_images(images)
        if 1 <= photo_num <= len(photo_imgs):
            target = photo_imgs[photo_num - 1]

    # Last resort: raw positional fallback
    if target is None and 1 <= photo_num <= len(images):
        target = images[photo_num - 1]

    if target is None:
        return None

    fpath = Path(target.get("path", ""))
    if not fpath.exists():
        return None
    try:
        data = fpath.read_bytes()
        b64 = base64.b64encode(data).decode('utf-8')
        ext = (target.get("ext") or "png").lower()
        mime = f"image/{ext}" if ext != "jpg" else "image/jpeg"
        raw_cap = target.get('caption') or ''
        if raw_cap:
            display_cap = _re.sub(r'^Description:\s*', '', raw_cap.strip(), flags=_re.IGNORECASE)
            label = display_cap[:120]
            if target.get('page'):
                label += f" (page {target['page']})"
        else:
            label = f"Photo {photo_num}"
            if target.get('page'):
                label += f" (page {target['page']})"
        return {"filename": fpath.name, "mime": mime, "data": b64, "label": label}
    except Exception:
        return None


def get_all_page_images_b64(doc_hash: str = None) -> List[dict]:
    """Get all page images as base64."""
    global _ingested
    if not _ingested:
        _ingested = _load_manifest()
    entry = _ingested.get(doc_hash) if doc_hash else None
    if not entry and _ingested:
        entry = list(_ingested.values())[-1]
    if not entry:
        return []

    results = []
    for fpath_str in entry.get("page_images", []):
        fpath = Path(fpath_str)
        if not fpath.exists():
            continue
        try:
            data = fpath.read_bytes()
            b64 = base64.b64encode(data).decode('utf-8')
            results.append({"filename": fpath.name, "mime": "image/png", "data": b64})
        except Exception:
            continue
    return results


def get_all_extracted_images_b64(doc_hash: str = None, limit: int = 32) -> List[dict]:
    """Get extracted embedded images (photos/figures) as base64 with labels."""
    global _ingested
    if not _ingested:
        _ingested = _load_manifest()
    entry = _ingested.get(doc_hash) if doc_hash else None
    if not entry and _ingested:
        entry = list(_ingested.values())[-1]
    if not entry:
        return []

    results = []
    for seq, img in enumerate(entry.get('extracted_images', [])[:max(1, min(limit, 200))], start=1):
        fpath = Path(img.get('path', ''))
        if not fpath.exists():
            continue
        try:
            data = fpath.read_bytes()
            b64 = base64.b64encode(data).decode('utf-8')
            ext = (img.get('ext') or 'png').lower()
            mime = f"image/{ext}" if ext != 'jpg' else 'image/jpeg'
            raw_cap = img.get('caption') or ''
            if raw_cap:
                # Strip leading "Description: " prefix for cleaner display
                display_cap = _re.sub(r'^Description:\s*', '', raw_cap.strip(), flags=_re.IGNORECASE)
                label = display_cap[:120]
                if img.get('page'):
                    label += f" | Page {img['page']}"
            else:
                label = f"Image {seq}"
                if img.get('page'):
                    label += f" | Page {img['page']}"
            results.append({
                'filename': fpath.name,
                'mime': mime,
                'data': b64,
                'caption': label,
                'label': label,
            })
        except Exception:
            continue
    return results


def get_document_page_count(doc_hash: str = None) -> int:
    global _ingested
    if not _ingested:
        _ingested = _load_manifest()
    if not _ingested:
        return 0
    entry = _ingested.get(doc_hash) if doc_hash else None
    if not entry and _ingested:
        entry = list(_ingested.values())[-1]
    return len(entry.get("page_images", [])) if entry else 0


def get_extracted_image_count(doc_hash: str = None) -> int:
    global _ingested
    if not _ingested:
        _ingested = _load_manifest()
    if not _ingested:
        return 0
    entry = _ingested.get(doc_hash) if doc_hash else None
    if not entry and _ingested:
        entry = list(_ingested.values())[-1]
    return len(entry.get("extracted_images", [])) if entry else 0


async def _reindex_cached_entry(doc_hash: str, entry: dict) -> bool:
    """Reuse parsed content but never vectors from an unknown/different profile."""
    chunks = entry.get("chunks") or []
    descriptions = entry.get("image_descriptions") or []
    entry["kg_ready"] = False

    text_embeddings = _compute_embeddings(chunks) if chunks else None
    image_embeddings = _compute_embeddings(
        [{"page": 0, "text": text} for text in descriptions]
    ) if descriptions else None
    complete = (
        (not chunks or text_embeddings is not None)
        and (not descriptions or image_embeddings is not None)
    )
    if complete:
        entry["embeddings"] = text_embeddings
        entry["image_embeddings"] = image_embeddings
        entry["embedding_profile"] = _active_embedding_profile()

    text_pages = [
        page for page in (entry.get("pages") or [])
        if not str(page.get("text") or "").startswith("[Page")
    ]
    full_text = "\n\n".join(
        f"=== Page {page.get('page')} ===\n{page.get('text') or ''}"
        for page in text_pages
    )
    if complete and full_text.strip():
        entry["kg_ready"] = await _build_kg(doc_hash, full_text)
    # Commit the new profile atomically only after both vector matrices passed
    # validation. If the provider is unavailable, the old on-disk profile stays
    # intact for rollback while strict query guards use lexical fallback.
    if complete:
        _save_manifest()
    return complete


# ── public API ──────────────────────────────────────────────────────
async def ingest_document(file_path: str) -> dict:
    """
    Hybrid ingestion pipeline:
      1. Extract text directly from PDF (instant, no LLM)
      2. Extract embedded images (photos, diagrams)
      3. Render page images for display
      4. Build KG from actual text
      5. Compute embeddings for text chunks
      6. Describe extracted images with vision model (only real images, not every page)
    """
    global _ingested, _active_session_hash

    if not _ingested:
        _ingested = _load_manifest()

    doc_hash = _file_hash(file_path)
    fname = Path(file_path).name

    # Check cache
    if doc_hash in _ingested:
        entry = _ingested[doc_hash]
        if entry.get("pages"):
            reindexed = False
            reindex_attempted = False
            if _entry_embeddings_compatible(entry):
                entry["embeddings"] = _validated_cached_matrix(
                    entry.get("embeddings"),
                    len(entry.get("chunks") or []),
                    entry.get("embedding_profile"),
                )
                entry["image_embeddings"] = _validated_cached_matrix(
                    entry.get("image_embeddings"),
                    len(entry.get("image_descriptions") or []),
                    entry.get("embedding_profile"),
                )
            else:
                reindex_attempted = True
                print(
                    f"[rag_engine] Cached vectors for {fname} have no compatible "
                    "embedding profile; re-indexing parsed content"
                )
                reindexed = await _reindex_cached_entry(doc_hash, entry)
            _active_session_hash = doc_hash  # mark as active even on cache hit
            print(f"[rag_engine] Cache hit for {fname} - parsed content reused")
            return {
                "status": "success",
                "cached": True,
                "reindexed": reindexed,
                "reindex_attempted": reindex_attempted,
                "message": (
                    f"Document vectors refreshed: {fname}"
                    if reindexed else (
                        f"Parsed document reused; embeddings unavailable: {fname}"
                        if reindex_attempted else f"Document already processed: {fname}"
                    )
                ),
                "pages_found": len(entry.get("page_images", [])),
                "images_found": len(entry.get("extracted_images", [])),
            }

    # ── Step 1: Extract text + embedded images (fast, no LLM) ──
    _reset_progress()
    _update_progress("Extracting text and images", 0, 5)  # placeholder total until images known
    _check_cancelled()

    print(f"[rag_engine] Extracting text and images from {fname} ...")
    pages, extracted_images = _extract_text_and_images(file_path, doc_hash)
    text_pages = [p for p in pages if not p["text"].startswith("[Page")]
    print(f"[rag_engine] Extracted text from {len(text_pages)}/{len(pages)} pages, "
          f"found {len(extracted_images)} embedded images")

    # Dynamic total: 4 fixed steps + one step per image for accurate ETA
    total_steps = 4 + max(1, len(extracted_images))

    # ── Step 2: Render page images for display ──
    _update_progress("Rendering page images", 1, total_steps)
    _check_cancelled()
    print(f"[rag_engine] Rendering {len(pages)} page images for display ...")
    page_images = _render_pdf_pages(file_path, doc_hash)

    # ── Step 3: Build text chunks + embeddings ──
    # Estimate: ~0.5s per chunk, capped 15–120s
    chunks = _split_page_chunks(pages)
    est_embed_s = max(15.0, min(120.0, len(chunks) * 0.5))
    _embed_stop = threading.Event()
    _embed_hb = _start_eta_heartbeat("Computing text embeddings", est_embed_s, _embed_stop)
    _update_progress("Computing text embeddings", 2, total_steps, eta_override=est_embed_s)
    _check_cancelled()
    print(f"[rag_engine] Computing embeddings for {len(chunks)} text chunks ...")
    try:
        emb = _compute_embeddings(chunks)
    finally:
        _embed_stop.set()
        _embed_hb.join(timeout=1.0)

    # ── Step 4: Build KG from actual extracted text ──
    # Estimate: ~1s per 400 chars of text, capped 30–600s
    full_text = '\n\n'.join(
        f"=== Page {p['page']} ===\n{p['text']}"
        for p in text_pages
    )
    est_kg_s = max(30.0, min(600.0, len(full_text) / 400.0))
    _kg_stop = threading.Event()
    _kg_hb = _start_eta_heartbeat("Building knowledge graph", est_kg_s, _kg_stop)
    _update_progress("Building knowledge graph", 3, total_steps, eta_override=est_kg_s)
    _check_cancelled()
    kg_ok = False
    if full_text.strip():
        print(f"[rag_engine] Building knowledge graph from extracted text (~{int(est_kg_s)}s est) ...")
        try:
            kg_ok = await _build_kg(doc_hash, full_text)
        finally:
            _kg_stop.set()
            _kg_hb.join(timeout=1.0)
        print(f"[rag_engine] KG {'built' if kg_ok else 'failed - will use embedding fallback'}")
    else:
        _kg_stop.set()
        _kg_hb.join(timeout=1.0)

    # ── Step 5: Describe extracted images with vision model (ONLY actual images) ──
    _check_cancelled()
    image_descriptions: List[str] = []
    image_embeddings = None
    if extracted_images:
        n_images = len(extracted_images)
        # Estimate ~25s per image for the first-image ETA seed
        est_per_image_s = 25.0
        print(f"[rag_engine] Describing {n_images} extracted images with vision model ...")
        image_phase_start = time.time()
        for i, img_info in enumerate(extracted_images):
            _check_cancelled()
            # Per-image ETA: use actual observed rate after first image, else use seed estimate
            if i > 0:
                per_image_sec = (time.time() - image_phase_start) / i
            else:
                per_image_sec = est_per_image_s
            img_eta: float = round(per_image_sec * (n_images - i), 1)
            _update_progress(
                f"Describing image {i + 1}/{n_images}",
                4 + i,
                total_steps,
                eta_override=img_eta,
            )
            img_path = img_info["path"]
            pg = img_info["page"]
            photo_number = img_info.get("photo_number")
            caption = img_info.get("caption")
            image_id = img_info.get("image_id")
            print(f"  [vision] Describing image {i + 1}/{len(extracted_images)} (page {pg}) ...")
            try:
                img_b64 = _resize_image_b64(img_path)
                _desc_prompt = (
                    "Describe this image from a technical document in detail. "
                    "What does it show? Include any visible labels, measurements, "
                    "conditions, defects, or structural elements."
                )
                desc = llm_providers.generate(_desc_prompt, images_b64=[img_b64],
                                              timeout=300, num_predict=512)
                prefix_parts = [f"ImageID {image_id}", f"Page {pg}"]
                if photo_number is not None:
                    prefix_parts.append(f"Photo {photo_number}")
                if caption:
                    prefix_parts.append(f"Caption: {caption}")
                prefix = " | ".join(prefix_parts)
                image_descriptions.append(f"{prefix} | Description: {desc.strip()}" if desc.strip() else f"{prefix} | Description: [no description]")
            except Exception as exc:
                print(f"  [vision] Failed: {exc}")
                prefix_parts = [f"ImageID {image_id}", f"Page {pg}"]
                if photo_number is not None:
                    prefix_parts.append(f"Photo {photo_number}")
                if caption:
                    prefix_parts.append(f"Caption: {caption}")
                image_descriptions.append(f"{' | '.join(prefix_parts)} | Description: [description failed]")

        # Embed image descriptions for semantic search
        if image_descriptions:
            print(f"[rag_engine] Computing embeddings for {len(image_descriptions)} image descriptions ...")
            image_embeddings = _compute_embeddings(
                [{"page": 0, "text": d} for d in image_descriptions]
            )

    parsed = {
        "pages": pages,
        "chunks": chunks,
        "page_images": page_images,
        "extracted_images": extracted_images,
        "image_descriptions": image_descriptions,
        "kg_ready": kg_ok,
        "embeddings": emb,
        "image_embeddings": image_embeddings,
        "embedding_profile": (
            _active_embedding_profile()
            if (
                (not chunks or emb is not None)
                and (not image_descriptions or image_embeddings is not None)
            )
            else None
        ),
    }
    _ingested[doc_hash] = parsed
    _active_session_hash = doc_hash
    _save_manifest()

    _update_progress("Complete", total_steps, total_steps)
    print(f"[rag_engine] Done - {len(pages)} pages, {len(chunks)} chunks, "
          f"{len(extracted_images)} images, KG={'yes' if kg_ok else 'no'}")
    return {
        "status": "success",
        "cached": False,
        "message": f"Document processed: {fname}",
        "pages_found": len(page_images),
        "images_found": len(extracted_images),
    }


async def query_document(
    question: str,
    doc_hash: str = None,
    model: str = None,
    force_defect_segmentation: bool = False,
    defect_segmentation_mode: str = 'balanced',
) -> dict:
    """
    LangGraph-style query routing:
      - Text questions → KG hybrid first, embedding fallback
      - Vision questions → find relevant images, send to vision model
    """
    global _ingested
    if not _ingested:
        _ingested = _load_manifest()
    if not _ingested:
        return {"context": "", "sources": [], "chunks": [], "images": []}

    entry = _ingested.get(doc_hash) if doc_hash else None
    if not entry:
        doc_hash = list(_ingested.keys())[-1]
        entry = _ingested[doc_hash]

    total_pages = len(entry.get("page_images", []))
    chunks = entry.get("chunks", [])
    has_images = bool(entry.get("extracted_images"))

    # ── Explicit count questions should use deterministic extraction stats ──
    if detect_count_images_request(question):
        extracted_images = entry.get("extracted_images", [])
        extracted_count = len(extracted_images)
        photo_numbers = sorted(
            {img.get("photo_number") for img in extracted_images if isinstance(img.get("photo_number"), int)}
        )

        if extracted_count > 0:
            if photo_numbers:
                context = (
                    f"Detected {extracted_count} extracted image region(s) in this indexed report. "
                    f"Caption-linked photo numbers found: {', '.join(str(n) for n in photo_numbers)}."
                )
            else:
                context = f"Detected {extracted_count} extracted image region(s) in this indexed report."
        else:
            context = (
                "No embedded image regions were extracted from the indexed report. "
                "This does not always mean the report has no photos; they may be flattened/scanned into page images. "
                f"Document pages indexed: {total_pages}."
            )

        return {
            "context": context,
            "sources": ["Image Extraction Stats"],
            "chunks": [context],
            "images": [],
            "total_pages": total_pages,
            "kg_used": False,
            "vision_used": True,
        }

    # ── Route the query ──
    route = classify_query(question, has_images=has_images)
    print(f"[rag_engine] Query route: {route} - \"{question[:80]}\"")

    # ── Vision path: send relevant images to vision model ──
    if route == 'vision':
        # First try extracted (embedded) images
        relevant_imgs = get_relevant_extracted_images(question, doc_hash)
        if relevant_imgs:
            answers = []
            return_images = []
            defect_analysis = force_defect_segmentation or detect_vision_analysis_request(question)
            for img_info in relevant_imgs:
                img_path = Path(img_info["path"])
                if not img_path.exists():
                    continue
                ans = _query_page_image(str(img_path), question)
                if ans:
                    pg = img_info["page"]
                    label_parts = [f"Page {pg}"]
                    if img_info.get("photo_number") is not None:
                        label_parts.append(f"Photo {img_info.get('photo_number')}")
                    if img_info.get("caption"):
                        label_parts.append(str(img_info.get("caption"))[:120])
                    label = " | ".join(label_parts)
                    answers.append(f"**{label}**:\n{ans}")
                    try:
                        raw = img_path.read_bytes()
                        b64 = base64.b64encode(raw).decode('utf-8')
                        ext = img_info.get("ext", "png")
                        mime = f"image/{ext}" if ext != "jpg" else "image/jpeg"
                        return_images.append({"filename": img_path.name, "mime": mime, "data": b64, "label": label})
                    except Exception:
                        pass

                    # Defect segmentation-style overlays for defect-focused questions.
                    if defect_analysis:
                        detections = _detect_defects_with_bboxes(str(img_path), question)
                        if detections:
                            detections = _refine_defect_detections_hybrid(
                                str(img_path),
                                detections,
                                segmentation_mode=defect_segmentation_mode,
                            )
                        if detections:
                            det_summary = ', '.join(
                                f"{d.get('label')} ({d.get('confidence', 0):.2f}, anomaly {d.get('anomaly_score', 0):.2f})"
                                for d in detections
                            )
                            answers.append(
                                f"Detected defect regions on {label} [{defect_segmentation_mode}]: {det_summary}"
                            )
                            annotated = _annotate_image_with_detections(str(img_path), detections)
                            if annotated:
                                annotated['label'] = f"{label} | Segmentation overlay"
                                return_images.append(annotated)
            if answers:
                # Also include text context for a richer answer
                text_context = ""
                if entry.get('kg_ready'):
                    kg_answer = await _query_kg(question, doc_hash, model=model)
                    if kg_answer:
                        text_context = f"\n\n**Text context from document:**\n{kg_answer}"

                combined = '\n\n'.join(answers)
                if text_context:
                    combined += text_context
                return {
                    "context": combined,
                    "sources": ["Visual Analysis"],
                    "chunks": answers,
                    "images": return_images,
                    "total_pages": total_pages,
                    "kg_used": False,
                    "vision_used": True,
                }

        # Fallback: use page images if no extracted images found
        page_imgs = get_relevant_page_images(question, doc_hash)
        if page_imgs:
            result = await query_page_with_vision(question, page_imgs)
            if result.get("answers"):
                combined = '\n\n'.join(result["answers"])
                return {
                    "context": combined,
                    "sources": ["Visual Analysis (page scan)"],
                    "chunks": result["answers"],
                    "images": result.get("images", []),
                    "total_pages": total_pages,
                    "kg_used": False,
                    "vision_used": True,
                }

    # ── Text path: KG hybrid first, embedding fallback ──
    if entry.get('kg_ready'):
        kg_answer = await _query_kg(question, doc_hash, model=model)
        if kg_answer:
            return {
                "context": kg_answer,
                "sources": ["Knowledge Graph"],
                "chunks": [kg_answer],
                "images": [],
                "total_pages": total_pages,
                "kg_used": True,
            }

    # ── Embedding fallback ──
    emb = _validated_cached_matrix(
        entry.get("embeddings"),
        len(chunks),
        entry.get("embedding_profile"),
    )
    entry["embeddings"] = emb

    relevant = _get_relevant(question, chunks, embeddings=emb)
    if _is_broad_question(question):
        relevant = chunks

    context_parts = [f"[Page {c['page']}]\n{c['text']}" for c in relevant]
    context = '\n\n---\n\n'.join(context_parts)

    seen_pages = []
    for c in relevant:
        if c["page"] not in seen_pages:
            seen_pages.append(c["page"])
    sources = [f"Page {p}" for p in sorted(seen_pages)]

    return {
        "context": context,
        "sources": sources,
        "chunks": [c["text"] for c in relevant],
        "images": [],
        "total_pages": total_pages,
        "kg_used": False,
    }


async def query_page_with_vision(question: str, page_images: List[str]) -> dict:
    """Send page images directly to the vision model for visual analysis."""
    answers = []
    return_images = []
    for img_path in page_images:
        p = Path(img_path)
        if not p.exists():
            continue
        ans = _query_page_image(str(p), question)
        if ans:
            page_label = p.stem.replace("page_", "Page ").lstrip("0") or "Page"
            answers.append(f"**{page_label}**:\n{ans}")
            try:
                raw = p.read_bytes()
                b64 = base64.b64encode(raw).decode('utf-8')
                return_images.append({"filename": p.name, "mime": "image/png", "data": b64})
            except Exception:
                pass
    return {"answers": answers, "images": return_images}


async def query_extracted_image(question: str, image_info: dict) -> dict:
    """Send a specific extracted image to the vision model."""
    img_path = Path(image_info["path"])
    if not img_path.exists():
        return {"answer": "", "image": None}
    ans = _query_page_image(str(img_path), question)
    img_data = None
    try:
        raw = img_path.read_bytes()
        b64 = base64.b64encode(raw).decode('utf-8')
        ext = image_info.get("ext", "png")
        mime = f"image/{ext}" if ext != "jpg" else "image/jpeg"
        img_data = {"filename": img_path.name, "mime": mime, "data": b64}
    except Exception:
        pass
    return {"answer": ans, "image": img_data}


def get_last_doc_hash() -> Optional[str]:
    global _ingested, _active_session_hash
    # Prefer the session-scoped hash so we never silently return a stale manifest doc.
    if _active_session_hash:
        return _active_session_hash
    if not _ingested:
        _ingested = _load_manifest()
    if _ingested:
        return list(_ingested.keys())[-1]
    return None


def resolve_doc_hash(preferred_doc_hash: Optional[str] = None) -> Optional[str]:
    global _ingested, _active_session_hash
    if not _ingested:
        _ingested = _load_manifest()
    if preferred_doc_hash and preferred_doc_hash in _ingested:
        return preferred_doc_hash
    # Only fall back to manifest if a document was actually uploaded this session.
    if _active_session_hash and _active_session_hash in _ingested:
        return _active_session_hash
    # No active session — do not auto-use stale persisted docs.
    return None


def reset_rag():
    """Clear all cached documents, page images, and knowledge graphs."""
    global _ingested, _lightrag_cache, _active_session_hash
    _ingested = {}
    _lightrag_cache = {}
    _active_session_hash = None
    p = Path(RAG_WORKING_DIR)
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)


# ── Model pre-warming / keep-alive (no-ops in the hosted build) ─────
# There are NO local models to load, warm, or evict — every AI stage calls a
# hosted API (see llm_providers). These stubs stay so main.py's lifespan hooks
# keep importing/calling them without change.
def start_model_warmer(interval_seconds: int = 240) -> None:
    """No-op: hosted providers hold nothing in local RAM/VRAM to warm."""
    return


def stop_model_warmer() -> None:
    """No-op: nothing local to unload."""
    return
