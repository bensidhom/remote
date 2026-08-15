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
import asyncio
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


class DocumentLimitError(RuntimeError):
    """Raised before a document can exceed a server-owned resource bound."""

    def __init__(self, message: str, *, code: str = "document_limit_exceeded"):
        super().__init__(message)
        self.code = code
        self.status_code = 413


class DocumentFormatError(ValueError):
    """Raised when the parser input is not an accepted PDF."""

    def __init__(self, message: str, *, code: str = "invalid_pdf"):
        super().__init__(message)
        self.code = code
        self.status_code = 415


def cancel_processing():
    """Reject the retired process-global cancellation operation."""
    raise RuntimeError("global document cancellation is retired; use a request token")


def _check_cancelled(cancellation=None):
    """Raise when the supplied request token (or legacy token) is cancelled."""
    if cancellation is None:
        return
    token = cancellation
    if bool(getattr(token, "is_set", lambda: False)()):
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


def _bounded_env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


# These are intentionally server-owned; request parameters cannot increase them.
PDF_MAX_PAGES = _bounded_env_int(
    "BRIDGEGUARD_PDF_MAX_PAGES", 300, minimum=1, maximum=2_000,
)
PDF_MAX_INPUT_BYTES = _bounded_env_int(
    "BRIDGEGUARD_DOCUMENT_MAX_UPLOAD_BYTES", 50 * 1024 * 1024,
    minimum=1024, maximum=250 * 1024 * 1024,
)
PDF_MAX_EMBEDDED_IMAGES = _bounded_env_int(
    "BRIDGEGUARD_PDF_MAX_EMBEDDED_IMAGES", 512, minimum=1, maximum=2_000,
)
PDF_MAX_PAGE_PIXELS = _bounded_env_int(
    "BRIDGEGUARD_PDF_MAX_PAGE_PIXELS", 24_000_000,
    minimum=1_000_000, maximum=100_000_000,
)
PDF_MAX_TOTAL_RENDER_PIXELS = _bounded_env_int(
    "BRIDGEGUARD_PDF_MAX_TOTAL_RENDER_PIXELS", 500_000_000,
    minimum=1_000_000, maximum=2_000_000_000,
)
PDF_MAX_EXTRACTED_TEXT_CHARS = _bounded_env_int(
    "BRIDGEGUARD_PDF_MAX_EXTRACTED_TEXT_CHARS", 5_000_000,
    minimum=10_000, maximum=25_000_000,
)
PDF_MAX_DERIVATIVE_BYTES = _bounded_env_int(
    "BRIDGEGUARD_PDF_MAX_DERIVATIVE_BYTES", 256 * 1024 * 1024,
    minimum=1024 * 1024, maximum=1024 * 1024 * 1024,
)
DERIVATIVE_RESPONSE_MAX_IMAGES = _bounded_env_int(
    "BRIDGEGUARD_DERIVATIVE_RESPONSE_MAX_IMAGES", 8, minimum=1, maximum=32,
)
DERIVATIVE_RESPONSE_MAX_BYTES = _bounded_env_int(
    "BRIDGEGUARD_DERIVATIVE_RESPONSE_MAX_BYTES", 12 * 1024 * 1024,
    minimum=256 * 1024, maximum=64 * 1024 * 1024,
)
DERIVATIVE_RESPONSE_MAX_SINGLE_BYTES = _bounded_env_int(
    "BRIDGEGUARD_DERIVATIVE_RESPONSE_MAX_SINGLE_BYTES", 6 * 1024 * 1024,
    minimum=64 * 1024, maximum=32 * 1024 * 1024,
)
QUERY_CONTEXT_MAX_CHARS = _bounded_env_int(
    "BRIDGEGUARD_DOCUMENT_QUERY_CONTEXT_MAX_CHARS", 160_000,
    minimum=8_000, maximum=1_000_000,
)

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
_active_session_hashes: Dict[str, str] = {}


def _document_scope_key(
    owner_id: str | None,
    workspace_id: str | None,
) -> str | None:
    if owner_id is None and workspace_id is None:
        return None
    if owner_id is None or workspace_id is None:
        raise ValueError("owner_id and workspace_id must be supplied together")
    owner = str(owner_id).strip().lower()
    if not owner or len(owner) > 255 or any(ord(char) < 32 for char in owner):
        raise ValueError("owner_id is invalid")
    workspace = str(workspace_id).strip()
    if (
        not workspace
        or len(workspace) > 255
        or any(ord(char) < 32 for char in workspace)
    ):
        raise ValueError("workspace_id is invalid")
    return hashlib.sha256(
        f"vision-document-scope-v1\0{workspace}\0{owner}".encode("utf-8")
    ).hexdigest()


def _entry_owned_by(
    entry: dict | None,
    owner_id: str | None,
    workspace_id: str | None,
) -> bool:
    """Fail closed for HTTP callers while retaining unscoped internal tests."""
    if entry is None:
        return False
    scope_key = _document_scope_key(owner_id, workspace_id)
    if scope_key is None:
        return True
    scopes = entry.get("document_scope_sha256s")
    return isinstance(scopes, list) and scope_key in scopes


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
            "document_scope_sha256s": sorted({
                str(scope).strip().lower()
                for scope in (v.get("document_scope_sha256s") or [])
                if _re.fullmatch(r"[0-9a-f]{64}", str(scope).strip().lower())
            }),
        }
    _MANIFEST_PATH.write_text(json.dumps(meta))


def _file_hash(file_path: str, cancellation=None) -> str:
    h = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 16), b''):
            _check_cancelled(cancellation)
            h.update(chunk)
    _check_cancelled(cancellation)
    return h.hexdigest()


def _validate_pdf_input(file_path: str) -> None:
    path = Path(file_path)
    if path.suffix.lower() != ".pdf":
        raise DocumentFormatError(
            "Document ingestion accepts PDF files only.",
            code="unsupported_document_type",
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise DocumentFormatError("The PDF cannot be read.", code="unreadable_pdf") from exc
    if size <= 0:
        raise DocumentFormatError("The PDF is empty.", code="empty_document")
    if size > PDF_MAX_INPUT_BYTES:
        raise DocumentLimitError(
            f"PDF exceeds the {PDF_MAX_INPUT_BYTES}-byte parser limit.",
            code="document_too_large",
        )
    try:
        with path.open("rb") as stream:
            magic = stream.read(8)
    except OSError as exc:
        raise DocumentFormatError("The PDF cannot be read.", code="unreadable_pdf") from exc
    if not magic.startswith(b"%PDF-"):
        raise DocumentFormatError(
            "The file does not have a valid PDF signature.",
            code="invalid_pdf_signature",
        )


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


def _validate_pdf_page_count(pdf) -> int:
    if bool(getattr(pdf, "needs_pass", False)):
        raise DocumentFormatError(
            "Password-protected PDFs are not supported.", code="encrypted_pdf",
        )
    page_count = len(pdf)
    if page_count < 1:
        raise DocumentFormatError("PDF contains no pages.", code="pdf_empty")
    if page_count > PDF_MAX_PAGES:
        raise DocumentLimitError(
            f"PDF has {page_count} pages; the limit is {PDF_MAX_PAGES}.",
            code="pdf_page_limit",
        )
    return page_count


def _reserve_render_pixels(
    width: float,
    height: float,
    consumed: int,
    *,
    label: str,
) -> int:
    pixels = max(0, int(width)) * max(0, int(height))
    if pixels > PDF_MAX_PAGE_PIXELS:
        raise DocumentLimitError(
            f"{label} requires {pixels} rendered pixels; the per-image limit is "
            f"{PDF_MAX_PAGE_PIXELS}.",
            code="pdf_pixel_limit",
        )
    total = consumed + pixels
    if total > PDF_MAX_TOTAL_RENDER_PIXELS:
        raise DocumentLimitError(
            "PDF derivative rendering exceeds the total pixel limit.",
            code="pdf_total_pixel_limit",
        )
    return total


def _encoded_base64_size(raw_bytes: int) -> int:
    return 4 * ((max(0, int(raw_bytes)) + 2) // 3)


def _bounded_image_payload(
    path: Path,
    *,
    mime: str,
    remaining_encoded_bytes: int = DERIVATIVE_RESPONSE_MAX_BYTES,
) -> tuple[Optional[dict], int]:
    """Return one bounded base64 image and its encoded-byte charge."""
    try:
        raw_size = path.stat().st_size
    except OSError:
        return None, 0
    encoded_size = _encoded_base64_size(raw_size)
    if (
        raw_size <= 0
        or raw_size > DERIVATIVE_RESPONSE_MAX_SINGLE_BYTES
        or encoded_size > max(0, remaining_encoded_bytes)
    ):
        return None, 0
    try:
        data = path.read_bytes()
    except OSError:
        return None, 0
    # Re-check after reading in case the file changed between stat and read.
    encoded_size = _encoded_base64_size(len(data))
    if (
        len(data) > DERIVATIVE_RESPONSE_MAX_SINGLE_BYTES
        or encoded_size > max(0, remaining_encoded_bytes)
    ):
        return None, 0
    return {
        "filename": path.name,
        "mime": mime,
        "data": base64.b64encode(data).decode("ascii"),
    }, encoded_size


def _cleanup_partial_document_derivatives(doc_hash: str) -> None:
    if not _re.fullmatch(r"[0-9a-f]{64}", str(doc_hash or "")):
        return
    root = Path(RAG_WORKING_DIR).resolve()
    for category in ("extracted_images", "page_images"):
        category_root = (root / category).resolve()
        target = (category_root / doc_hash[:16]).resolve()
        if target.parent == category_root and target.exists():
            shutil.rmtree(target)


def _extract_text_blocks_with_bbox(page, cancellation=None) -> List[dict]:
    blocks: List[dict] = []
    try:
        _check_cancelled(cancellation)
        data = page.get_text("dict")
        for b in data.get("blocks", []):
            _check_cancelled(cancellation)
            if b.get("type") != 0:
                continue
            lines = []
            for ln in b.get("lines", []):
                _check_cancelled(cancellation)
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
        _check_cancelled(cancellation)
    except CancelledError:
        raise
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


def _nearest_caption_for_rect(
    text_blocks: List[dict], rect, cancellation=None,
) -> tuple[Optional[str], Optional[int]]:
    """Find a likely caption nearest/below image rect and parse photo number if present."""
    if rect is None or not text_blocks:
        return None, None

    candidates = []
    rx0, ry0, rx1, ry1 = rect.x0, rect.y0, rect.x1, rect.y1
    for b in text_blocks:
        _check_cancelled(cancellation)
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


def _extract_text_and_images(file_path: str, doc_hash: str, cancellation=None) -> tuple:
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
    extracted_chars = 0
    rendered_pixels = 0
    derivative_bytes = 0

    try:
        pdf = pymupdf.open(file_path)
    except Exception as exc:
        raise DocumentFormatError(
            "The PDF could not be opened.", code="unreadable_pdf",
        ) from exc
    try:
        page_count = _validate_pdf_page_count(pdf)
        for page_num in range(page_count):
            _check_cancelled(cancellation)
            page = pdf[page_num]
            pg = page_num + 1
            seen_instances = set()
            page_image_counter = 0

            # ── Extract text ──
            text = page.get_text("text").strip()
            extracted_chars += len(text)
            if extracted_chars > PDF_MAX_EXTRACTED_TEXT_CHARS:
                raise DocumentLimitError(
                    "PDF extracted text exceeds the configured safety limit.",
                    code="pdf_text_limit",
                )
            pages.append({"page": pg, "text": text if text else f"[Page {pg}: no text content]"})
            _check_cancelled(cancellation)
            text_blocks = _extract_text_blocks_with_bbox(page, cancellation)

            # ── Extract embedded images by placement rects (prevents index drift) ──
            img_list = page.get_images(full=True)
            if len(img_list) > PDF_MAX_EMBEDDED_IMAGES:
                raise DocumentLimitError(
                    f"PDF page {pg} advertises more images than the configured limit.",
                    code="pdf_image_limit",
                )
            for img_idx, img_info in enumerate(img_list):
                _check_cancelled(cancellation)
                xref = img_info[0]
                try:
                    rects = page.get_image_rects(xref)
                    if not rects:
                        rects = [None]

                    for rect_idx, rect in enumerate(rects):
                        _check_cancelled(cancellation)
                        if rect is not None:
                            # Dedupe repeated placements.
                            key = (xref, round(rect.x0, 1), round(rect.y0, 1), round(rect.x1, 1), round(rect.y1, 1))
                            if key in seen_instances:
                                continue
                            seen_instances.add(key)

                            if rect.width < 80 or rect.height < 80:
                                continue

                            if len(extracted_images) >= PDF_MAX_EMBEDDED_IMAGES:
                                raise DocumentLimitError(
                                    "PDF contains more extractable images than the configured limit.",
                                    code="pdf_image_limit",
                                )
                            rendered_pixels = _reserve_render_pixels(
                                rect.width * 2,
                                rect.height * 2,
                                rendered_pixels,
                                label=f"PDF page {pg} image",
                            )
                            pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=rect, alpha=False)
                            _check_cancelled(cancellation)
                            out_path = images_dir / f"page_{pg:03d}_img_{img_idx:02d}_inst_{rect_idx:02d}.png"
                            pix.save(str(out_path))
                            derivative_bytes += out_path.stat().st_size
                            if derivative_bytes > PDF_MAX_DERIVATIVE_BYTES:
                                raise DocumentLimitError(
                                    "PDF extracted derivatives exceed the configured byte limit.",
                                    code="pdf_derivative_limit",
                                )
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
                            if len(extracted_images) >= PDF_MAX_EMBEDDED_IMAGES:
                                raise DocumentLimitError(
                                    "PDF contains more extractable images than the configured limit.",
                                    code="pdf_image_limit",
                                )
                            rendered_pixels = _reserve_render_pixels(
                                w, h, rendered_pixels, label=f"PDF page {pg} image",
                            )
                            derivative_bytes += len(img_data)
                            if derivative_bytes > PDF_MAX_DERIVATIVE_BYTES:
                                raise DocumentLimitError(
                                    "PDF extracted derivatives exceed the configured byte limit.",
                                    code="pdf_derivative_limit",
                                )
                            out_path = images_dir / f"page_{pg:03d}_img_{img_idx:02d}_inst_{rect_idx:02d}.{ext}"
                            out_path.write_bytes(img_data)
                            bbox = None

                        caption, photo_number = (
                            _nearest_caption_for_rect(text_blocks, rect, cancellation)
                            if rect is not None else (None, None)
                        )
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
                except (CancelledError, DocumentLimitError):
                    raise
                except Exception as exc:
                    print(f"  [extract] Failed to extract image xref={xref} page {pg}: {exc}")

            # ── Caption-driven fallback splitting for scanned pages with merged visuals ──
            if page_image_counter <= 1:
                caption_blocks = []
                for block in text_blocks:
                    _check_cancelled(cancellation)
                    if _parse_photo_number(block.get("text", "")) is not None:
                        caption_blocks.append(block)
                caption_blocks.sort(key=lambda b: b["bbox"][1])
                if len(caption_blocks) >= 2:
                    page_rect = page.rect
                    segment_top = page_rect.y0 + 5
                    for cap_idx, cap in enumerate(caption_blocks):
                        _check_cancelled(cancellation)
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
                            if len(extracted_images) >= PDF_MAX_EMBEDDED_IMAGES:
                                raise DocumentLimitError(
                                    "PDF contains more extractable images than the configured limit.",
                                    code="pdf_image_limit",
                                )
                            rendered_pixels = _reserve_render_pixels(
                                clip.width * 2,
                                clip.height * 2,
                                rendered_pixels,
                                label=f"PDF page {pg} caption image",
                            )
                            pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=clip, alpha=False)
                            _check_cancelled(cancellation)
                            out_path = images_dir / f"page_{pg:03d}_caption_{cap_idx:02d}.png"
                            pix.save(str(out_path))
                            derivative_bytes += out_path.stat().st_size
                            if derivative_bytes > PDF_MAX_DERIVATIVE_BYTES:
                                raise DocumentLimitError(
                                    "PDF extracted derivatives exceed the configured byte limit.",
                                    code="pdf_derivative_limit",
                                )
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
                        except (CancelledError, DocumentLimitError):
                            raise
                        except Exception:
                            pass

                        segment_top = cap_y1 + 6
        _check_cancelled(cancellation)
    except (CancelledError, DocumentLimitError, DocumentFormatError):
        raise
    except Exception as exc:
        raise DocumentFormatError(
            "The PDF content could not be parsed.", code="unreadable_pdf",
        ) from exc
    finally:
        pdf.close()

    return pages, extracted_images


# ── PDF → page images (PyMuPDF) — for "show page" requests ──────────
def _render_pdf_pages(
    file_path: str,
    doc_hash: str,
    dpi: int = 150,
    cancellation=None,
) -> List[str]:
    """Render each PDF page as a PNG image. Returns list of image paths."""
    pymupdf = _get_pymupdf_module()

    images_dir = Path(RAG_WORKING_DIR) / 'page_images' / doc_hash[:16]
    images_dir.mkdir(parents=True, exist_ok=True)

    image_paths: List[str] = []
    rendered_pixels = 0
    derivative_bytes = 0
    try:
        pdf = pymupdf.open(file_path)
    except Exception as exc:
        raise DocumentFormatError(
            "The PDF could not be opened.", code="unreadable_pdf",
        ) from exc
    try:
        page_count = _validate_pdf_page_count(pdf)
        for page_num in range(page_count):
            _check_cancelled(cancellation)
            page = pdf[page_num]
            scale = dpi / 72
            rendered_pixels = _reserve_render_pixels(
                page.rect.width * scale,
                page.rect.height * scale,
                rendered_pixels,
                label=f"PDF page {page_num + 1}",
            )
            mat = pymupdf.Matrix(dpi / 72, dpi / 72)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            _check_cancelled(cancellation)
            out_path = images_dir / f"page_{page_num + 1:03d}.png"
            pix.save(str(out_path))
            derivative_bytes += out_path.stat().st_size
            if derivative_bytes > PDF_MAX_DERIVATIVE_BYTES:
                raise DocumentLimitError(
                    "PDF rendered pages exceed the configured byte limit.",
                    code="pdf_derivative_limit",
                )
            image_paths.append(str(out_path))
        _check_cancelled(cancellation)
    except (CancelledError, DocumentLimitError, DocumentFormatError):
        raise
    except Exception as exc:
        raise DocumentFormatError(
            "The PDF pages could not be rendered.", code="unreadable_pdf",
        ) from exc
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
def _split_page_chunks(
    pages: List[dict],
    chunk_size: int = None,
    overlap: int = None,
    cancellation=None,
) -> List[dict]:
    if chunk_size is None:
        chunk_size = CHUNK_SIZE
    if overlap is None:
        overlap = CHUNK_OVERLAP
    chunks: List[dict] = []
    for pg in pages:
        _check_cancelled(cancellation)
        text = pg["text"]
        if text.startswith("[Page") and "no text content]" in text:
            continue
        words = text.split()
        if not words:
            continue
        start = 0
        while start < len(words):
            _check_cancelled(cancellation)
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


def _compute_embeddings(chunks: List[dict], cancellation=None) -> Optional[np.ndarray]:
    if not chunks:
        return None
    texts = [c["text"] for c in chunks]
    all_embs = []
    batch_size = 16
    for i in range(0, len(texts), batch_size):
        _check_cancelled(cancellation)
        batch = texts[i:i + batch_size]
        emb = _embed(batch, input_type="passage")
        _check_cancelled(cancellation)
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
        payload, _ = _bounded_image_payload(fpath, mime="image/png")
        return payload
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
        ext = (target.get("ext") or "png").lower()
        mime = f"image/{ext}" if ext != "jpg" else "image/jpeg"
        payload, _ = _bounded_image_payload(fpath, mime=mime)
        if payload is None:
            return None
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
        payload["label"] = label
        return payload
    except Exception:
        return None


def get_all_page_images_b64(doc_hash: str = None, *, cancellation=None) -> List[dict]:
    """Get a server-bounded prefix of page images as base64."""
    global _ingested
    if not _ingested:
        _ingested = _load_manifest()
    entry = _ingested.get(doc_hash) if doc_hash else None
    if not entry and _ingested:
        entry = list(_ingested.values())[-1]
    if not entry:
        return []

    results = []
    remaining = DERIVATIVE_RESPONSE_MAX_BYTES
    for fpath_str in entry.get("page_images", []):
        _check_cancelled(cancellation)
        if len(results) >= DERIVATIVE_RESPONSE_MAX_IMAGES or remaining <= 0:
            break
        fpath = Path(fpath_str)
        if not fpath.exists():
            continue
        try:
            payload, charged = _bounded_image_payload(
                fpath, mime="image/png", remaining_encoded_bytes=remaining,
            )
            if payload is not None:
                results.append(payload)
                remaining -= charged
        except Exception:
            continue
    return results


def get_all_extracted_images_b64(
    doc_hash: str = None,
    limit: int = DERIVATIVE_RESPONSE_MAX_IMAGES,
    *,
    cancellation=None,
) -> List[dict]:
    """Get a bounded prefix of extracted images as base64 with labels."""
    global _ingested
    if not _ingested:
        _ingested = _load_manifest()
    entry = _ingested.get(doc_hash) if doc_hash else None
    if not entry and _ingested:
        entry = list(_ingested.values())[-1]
    if not entry:
        return []

    results = []
    remaining = DERIVATIVE_RESPONSE_MAX_BYTES
    effective_limit = max(1, min(int(limit), DERIVATIVE_RESPONSE_MAX_IMAGES))
    for seq, img in enumerate(entry.get('extracted_images', []), start=1):
        _check_cancelled(cancellation)
        if len(results) >= effective_limit or remaining <= 0:
            break
        fpath = Path(img.get('path', ''))
        if not fpath.exists():
            continue
        try:
            ext = (img.get('ext') or 'png').lower()
            mime = f"image/{ext}" if ext != 'jpg' else 'image/jpeg'
            payload, charged = _bounded_image_payload(
                fpath, mime=mime, remaining_encoded_bytes=remaining,
            )
            if payload is None:
                continue
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
            payload['caption'] = label
            payload['label'] = label
            results.append(payload)
            remaining -= charged
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


async def _reindex_cached_entry(doc_hash: str, entry: dict, cancellation=None) -> bool:
    """Reuse parsed content but never vectors from an unknown/different profile."""
    chunks = entry.get("chunks") or []
    descriptions = entry.get("image_descriptions") or []
    entry["kg_ready"] = False

    text_embeddings = await asyncio.to_thread(
        _compute_embeddings, chunks, cancellation,
    ) if chunks else None
    image_embeddings = await asyncio.to_thread(
        _compute_embeddings,
        [{"page": 0, "text": text} for text in descriptions],
        cancellation,
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
async def ingest_document(
    file_path: str,
    *,
    analysis_mode: str = "text",
    cancellation=None,
    owner_id: str | None = None,
    workspace_id: str | None = None,
) -> dict:
    """
    Hybrid ingestion pipeline:
      1. Extract text directly from PDF (instant, no LLM)
      2. Extract embedded images (photos, diagrams)
      3. Render page images for display
      4. Build KG from actual text
      5. Compute embeddings for text chunks
      6. Describe extracted images with vision model (only real images, not every page)
    """
    mode = str(analysis_mode or "").strip().lower()
    if mode not in {"text", "visual"}:
        raise ValueError("analysis_mode must be text or visual")
    _check_cancelled(cancellation)

    global _ingested, _active_session_hash, _active_session_hashes
    scope_key = _document_scope_key(owner_id, workspace_id)

    if not _ingested:
        _ingested = _load_manifest()

    await asyncio.to_thread(_validate_pdf_input, file_path)
    _check_cancelled(cancellation)
    doc_hash = await asyncio.to_thread(_file_hash, file_path, cancellation)
    _check_cancelled(cancellation)
    fname = Path(file_path).name

    # Check cache
    if doc_hash in _ingested:
        entry = _ingested[doc_hash]
        if entry.get("pages"):
            if scope_key is not None:
                scopes = {
                    str(value).strip().lower()
                    for value in (entry.get("document_scope_sha256s") or [])
                    if _re.fullmatch(r"[0-9a-f]{64}", str(value).strip().lower())
                }
                scopes.add(scope_key)
                entry["document_scope_sha256s"] = sorted(scopes)
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
                reindexed = await _reindex_cached_entry(
                    doc_hash, entry, cancellation=cancellation,
                )
            _active_session_hash = doc_hash  # mark as active even on cache hit
            if scope_key is not None:
                _active_session_hashes[scope_key] = doc_hash
                _save_manifest()
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
                "analysis_mode": mode,
                "visual_processing_ran": False,
                "inspected_images": [],
            }

    # ── Step 1: Extract text + embedded images (fast, no LLM) ──
    _reset_progress()
    _update_progress("Extracting text and images", 0, 5)  # placeholder total until images known
    _check_cancelled(cancellation)

    print(f"[rag_engine] Extracting text and images from {fname} ...")
    try:
        pages, extracted_images = await asyncio.to_thread(
            _extract_text_and_images, file_path, doc_hash, cancellation,
        )
    except Exception:
        await asyncio.to_thread(_cleanup_partial_document_derivatives, doc_hash)
        raise
    text_pages = [p for p in pages if not p["text"].startswith("[Page")]
    print(f"[rag_engine] Extracted text from {len(text_pages)}/{len(pages)} pages, "
          f"found {len(extracted_images)} embedded images")

    # Dynamic total: 4 fixed steps + one step per image for accurate ETA
    total_steps = 4 + max(1, len(extracted_images))

    # ── Step 2: Render page images for display ──
    _update_progress("Rendering page images", 1, total_steps)
    _check_cancelled(cancellation)
    print(f"[rag_engine] Rendering {len(pages)} page images for display ...")
    try:
        page_images = await asyncio.to_thread(
            _render_pdf_pages, file_path, doc_hash, 150, cancellation,
        )
    except Exception:
        await asyncio.to_thread(_cleanup_partial_document_derivatives, doc_hash)
        raise

    # ── Step 3: Build text chunks + embeddings ──
    # Estimate: ~0.5s per chunk, capped 15–120s
    try:
        chunks = await asyncio.to_thread(
            _split_page_chunks, pages, None, None, cancellation,
        )
    except Exception:
        await asyncio.to_thread(_cleanup_partial_document_derivatives, doc_hash)
        raise
    est_embed_s = max(15.0, min(120.0, len(chunks) * 0.5))
    _embed_stop = threading.Event()
    _embed_hb = _start_eta_heartbeat("Computing text embeddings", est_embed_s, _embed_stop)
    _update_progress("Computing text embeddings", 2, total_steps, eta_override=est_embed_s)
    _check_cancelled(cancellation)
    print(f"[rag_engine] Computing embeddings for {len(chunks)} text chunks ...")
    try:
        emb = await asyncio.to_thread(_compute_embeddings, chunks, cancellation)
    except CancelledError:
        await asyncio.to_thread(_cleanup_partial_document_derivatives, doc_hash)
        raise
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
    try:
        _check_cancelled(cancellation)
    except CancelledError:
        await asyncio.to_thread(_cleanup_partial_document_derivatives, doc_hash)
        raise
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

    try:
        _check_cancelled(cancellation)
    except CancelledError:
        await asyncio.to_thread(_cleanup_partial_document_derivatives, doc_hash)
        raise

    # ── Step 5: Describe extracted images with vision model (ONLY actual images) ──
    _check_cancelled(cancellation)
    image_descriptions: List[str] = []
    image_embeddings = None
    inspected_images: List[dict] = []
    if extracted_images and mode == "visual":
        max_images = max(1, min(int(os.getenv("VISION_REQUEST_MAX_IMAGES", "8")), 8))
        visual_images = extracted_images[:max_images]
        n_images = len(visual_images)
        # Estimate ~25s per image for the first-image ETA seed
        est_per_image_s = 25.0
        print(f"[rag_engine] Describing {n_images} extracted images with vision model ...")
        image_phase_start = time.time()
        for i, img_info in enumerate(visual_images):
            _check_cancelled(cancellation)
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
            inspected_images.append({
                "image_id": image_id,
                "page": pg,
                "photo_number": photo_number,
            })
            print(f"  [vision] Describing image {i + 1}/{len(visual_images)} (page {pg}) ...")
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
                _check_cancelled(cancellation)
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
            image_embeddings = await asyncio.to_thread(
                _compute_embeddings,
                [{"page": 0, "text": d} for d in image_descriptions],
                cancellation,
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
        "document_scope_sha256s": [scope_key] if scope_key is not None else [],
    }
    _ingested[doc_hash] = parsed
    _active_session_hash = doc_hash
    if scope_key is not None:
        _active_session_hashes[scope_key] = doc_hash
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
        "analysis_mode": mode,
        "visual_processing_ran": bool(inspected_images),
        "inspected_images": inspected_images,
    }


async def query_document(
    question: str,
    doc_hash: str = None,
    model: str = None,
    analysis_mode: str = "text",
    defect_segmentation_mode: str = 'balanced',
    cancellation=None,
    owner_id: str | None = None,
    workspace_id: str | None = None,
) -> dict:
    """
    LangGraph-style query routing:
      - Text questions → KG hybrid first, embedding fallback
      - Vision questions → find relevant images, send to vision model
    """
    mode = str(analysis_mode or "").strip().lower()
    if mode not in {"text", "visual"}:
        raise ValueError("analysis_mode must be text or visual")
    _check_cancelled(cancellation)

    global _ingested
    scope_key = _document_scope_key(owner_id, workspace_id)
    if not _ingested:
        _ingested = _load_manifest()
    if not _ingested:
        return {"context": "", "sources": [], "chunks": [], "images": []}

    entry = _ingested.get(doc_hash) if doc_hash else None
    if scope_key is not None and not _entry_owned_by(entry, owner_id, workspace_id):
        active = _active_session_hashes.get(scope_key)
        doc_hash = active if active and active in _ingested else None
        entry = _ingested.get(doc_hash) if doc_hash else None
    if not entry and scope_key is None:
        doc_hash = list(_ingested.keys())[-1]
        entry = _ingested[doc_hash]
    if not entry or not _entry_owned_by(entry, owner_id, workspace_id):
        return {
            "context": "", "sources": [], "chunks": [], "images": [],
            "analysis_mode": mode, "visual_processing_ran": False,
        }

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
            "vision_used": False,
            "analysis_mode": mode,
            "visual_processing_ran": False,
        }

    # ── Route the query ──
    route = classify_query(question, has_images=has_images)
    print(f"[rag_engine] Query route: {route} - \"{question[:80]}\"")

    # ── Vision path: send relevant images to vision model ──
    if route == 'vision' and mode == "visual":
        # First try extracted (embedded) images
        max_images = max(1, min(int(os.getenv("VISION_REQUEST_MAX_IMAGES", "8")), 8))
        relevant_imgs = get_relevant_extracted_images(
            question, doc_hash, k=max_images,
        )[:max_images]
        if relevant_imgs:
            answers = []
            return_images = []
            defect_analysis = detect_vision_analysis_request(question)
            for img_info in relevant_imgs:
                _check_cancelled(cancellation)
                img_path = Path(img_info["path"])
                if not img_path.exists():
                    continue
                ans = _query_page_image(str(img_path), question)
                _check_cancelled(cancellation)
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
                        _check_cancelled(cancellation)
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
                    "analysis_mode": mode,
                    "visual_processing_ran": True,
                }

        # Fallback: use page images if no extracted images found
        page_imgs = get_relevant_page_images(question, doc_hash)[:max_images]
        if page_imgs:
            result = await query_page_with_vision(
                question, page_imgs, cancellation=cancellation,
            )
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
                    "analysis_mode": mode,
                    "visual_processing_ran": True,
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
                "analysis_mode": mode,
                "visual_processing_ran": False,
            }

    # ── Embedding fallback ──
    emb = _validated_cached_matrix(
        entry.get("embeddings"),
        len(chunks),
        entry.get("embedding_profile"),
    )
    entry["embeddings"] = emb

    relevant = await asyncio.to_thread(
        _get_relevant, question, chunks, 8, emb,
    )
    _check_cancelled(cancellation)
    if _is_broad_question(question):
        relevant = chunks

    bounded_relevant = []
    used_chars = 0
    for chunk in relevant:
        _check_cancelled(cancellation)
        text = str(chunk.get("text") or "")
        remaining = QUERY_CONTEXT_MAX_CHARS - used_chars
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[:remaining]
        bounded_relevant.append({**chunk, "text": text})
        used_chars += len(text)
    relevant = bounded_relevant

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
        "analysis_mode": mode,
        "visual_processing_ran": False,
    }


async def query_page_with_vision(
    question: str,
    page_images: List[str],
    *,
    cancellation=None,
) -> dict:
    """Send page images directly to the vision model for visual analysis."""
    answers = []
    return_images = []
    for img_path in page_images:
        _check_cancelled(cancellation)
        p = Path(img_path)
        if not p.exists():
            continue
        ans = _query_page_image(str(p), question)
        _check_cancelled(cancellation)
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


def get_last_doc_hash(
    owner_id: str | None = None,
    workspace_id: str | None = None,
) -> Optional[str]:
    global _ingested, _active_session_hash, _active_session_hashes
    scope_key = _document_scope_key(owner_id, workspace_id)
    if scope_key is not None:
        active = _active_session_hashes.get(scope_key)
        if active and _entry_owned_by(
            _ingested.get(active), owner_id, workspace_id,
        ):
            return active
        return None
    # Prefer the session-scoped hash so we never silently return a stale manifest doc.
    if _active_session_hash:
        return _active_session_hash
    if not _ingested:
        _ingested = _load_manifest()
    if _ingested:
        return list(_ingested.keys())[-1]
    return None


def resolve_doc_hash(
    preferred_doc_hash: Optional[str] = None,
    *,
    owner_id: str | None = None,
    workspace_id: str | None = None,
) -> Optional[str]:
    global _ingested, _active_session_hash, _active_session_hashes
    scope_key = _document_scope_key(owner_id, workspace_id)
    if not _ingested:
        _ingested = _load_manifest()
    if (
        preferred_doc_hash
        and preferred_doc_hash in _ingested
        and _entry_owned_by(_ingested[preferred_doc_hash], owner_id, workspace_id)
    ):
        return preferred_doc_hash
    if scope_key is not None:
        active = _active_session_hashes.get(scope_key)
        if active and _entry_owned_by(
            _ingested.get(active), owner_id, workspace_id,
        ):
            return active
        return None
    # Only fall back to manifest if a document was actually uploaded this session.
    if _active_session_hash and _active_session_hash in _ingested:
        return _active_session_hash
    # No active session — do not auto-use stale persisted docs.
    return None


def reset_rag(
    *,
    owner_id: str | None = None,
    workspace_id: str | None = None,
):
    """Remove one owner's document claims, or all state for internal maintenance."""
    global _ingested, _lightrag_cache, _active_session_hash, _active_session_hashes
    scope_key = _document_scope_key(owner_id, workspace_id)
    if scope_key is not None:
        if not _ingested:
            _ingested = _load_manifest()
        for entry in _ingested.values():
            scopes = {
                str(value).strip().lower()
                for value in (entry.get("document_scope_sha256s") or [])
                if _re.fullmatch(r"[0-9a-f]{64}", str(value).strip().lower())
            }
            scopes.discard(scope_key)
            entry["document_scope_sha256s"] = sorted(scopes)
        _active_session_hashes.pop(scope_key, None)
        _save_manifest()
        return
    _ingested = {}
    _lightrag_cache = {}
    _active_session_hash = None
    _active_session_hashes = {}
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
