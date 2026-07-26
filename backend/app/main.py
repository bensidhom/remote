# backend/app/main.py

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.encoders import jsonable_encoder
import uvicorn
import os
import socket
import shutil
import sqlite3
from pathlib import Path
import json
import math
from typing import List, Dict, Optional

try:
    from .auth import AuthMiddleware, router as auth_router
except ImportError:
    from auth import AuthMiddleware, router as auth_router


def _load_dotenv_early() -> None:
    """Load backend/.env (KEY=VALUE lines) into the environment BEFORE the service
    imports below run (importing them triggers datapaths.py). Real environment
    variables always win (setdefault), which is how AWS/EB config overrides .env.
    datapaths.py additionally loads a repo-root .env; either location works.
    Dependency-free on purpose (no python-dotenv needed)."""
    env_path = Path(__file__).resolve().parent.parent / ".env"  # backend/.env
    try:
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    except Exception:
        pass


_load_dotenv_early()

# Point-cloud storage/size settings are read while its router imports, so load
# backend/.env first just like the other environment-backed services below.
try:
    from .routers.point_cloud_api import router as point_cloud_router
except ImportError:
    from routers.point_cloud_api import router as point_cloud_router

# Import services
try:
    from .routers.services.utils import resource_path, init_db_if_needed
    from .routers.services.parser import parse_nbi_txt, parse_elements_xml, extract_inspection_reports_to_schema
    from .routers.services.mapper import map_bridge_to_snbi
    from .routers.services.reverse_mapper import convert_snbi_to_outputs
    from .routers.services.snbi_schema import (
        normalize_snbi_payload,
        summarize_payload_coverage,
        summarize_payload_coverage_by_section,
    )
    from .routers.services.snbi_validation import validate_snbi_payload
    from .routers.services.forecasting import (
        rebuild_training_db,
        train_prophet_forecasts,
        train_condition_forecasts,
        get_forecast_summary,
        get_forecast_bridges,
        get_bridge_forecast,
        ensure_training_data,
        simulate_bridge_forecast,
    )
    from .routers.services.lcca import compute_bridge_lcca
    from .routers.services.constants import STATE_MAP
    from .routers.services.database import (
        init_db,
        load_json_to_db,
        get_bridges_for_map,
        get_map_counties,
        get_state_kpis,
        get_county_kpis,
        delete_bridges_by_year,
        get_state_condition_history,
        get_bridge_details as get_bridge_details_db,
        get_bridge_field_metadata,
        get_all_tables,
        get_table_data,
        get_table_schema,
        get_bridge_history as get_bridge_history_db,
        update_bridge_main,
        insert_table_row,
        update_table_row,
        delete_table_row,
        clear_all_data,
        export_all_bridges_snbi,
        get_available_years,
        get_loaded_years,
        execute_readonly_sql,
        save_inspection_report_extractions,
        get_inspection_report_extractions,
        validate_inspection_report_records,
        apply_validated_inspection_report_records,
        delete_single_bridge,
        create_custom_table, list_custom_tables_for_bridge, add_custom_row,
        update_custom_row, delete_custom_row, delete_custom_table,
    )
    from .routers.services.vision import (
        process_vision_rag,
        process_vision_query_only,
        process_db_rag,
        process_db_rag_with_files,
        process_bridge_ask,
        process_bridge_ask_stream,
        process_bridge_visual_investigation,
        bridgette_answer,
        bridgette_answer_stream,
        begin_chat,
        end_chat,
        cancel_chat,
        ChatCancelled,
    )
    from .routers.services.rag_engine import (
        reset_rag,
        get_last_doc_hash,
        get_document_page_count,
        get_extracted_image_count,
        cancel_processing,
        get_progress,
        CancelledError,
    )
except ImportError:
    # Support running directly as a script with `python main.py`
    from routers.services.utils import resource_path, init_db_if_needed
    from routers.services.parser import parse_nbi_txt, parse_elements_xml, extract_inspection_reports_to_schema
    from routers.services.mapper import map_bridge_to_snbi
    from routers.services.reverse_mapper import convert_snbi_to_outputs
    from routers.services.snbi_schema import (
        normalize_snbi_payload,
        summarize_payload_coverage,
        summarize_payload_coverage_by_section,
    )
    from routers.services.snbi_validation import validate_snbi_payload
    from routers.services.forecasting import (
        rebuild_training_db,
        train_prophet_forecasts,
        train_condition_forecasts,
        get_forecast_summary,
        get_forecast_bridges,
        get_bridge_forecast,
        ensure_training_data,
        simulate_bridge_forecast,
    )
    from routers.services.lcca import compute_bridge_lcca
    from routers.services.constants import STATE_MAP
    from routers.services.database import (
        init_db,
        load_json_to_db,
        get_bridges_for_map,
        get_map_counties,
        get_state_kpis,
        get_county_kpis,
        delete_bridges_by_year,
        get_state_condition_history,
        get_bridge_details as get_bridge_details_db,
        get_bridge_field_metadata,
        get_all_tables,
        get_table_data,
        get_table_schema,
        get_bridge_history as get_bridge_history_db,
        update_bridge_main,
        insert_table_row,
        update_table_row,
        delete_table_row,
        clear_all_data,
        export_all_bridges_snbi,
        get_available_years,
        get_loaded_years,
        execute_readonly_sql,
        save_inspection_report_extractions,
        get_inspection_report_extractions,
        validate_inspection_report_records,
        apply_validated_inspection_report_records,
        delete_single_bridge,
        create_custom_table, list_custom_tables_for_bridge, add_custom_row,
        update_custom_row, delete_custom_row, delete_custom_table,
    )
    from routers.services.vision import (
        process_vision_rag,
        process_vision_query_only,
        process_db_rag,
        process_db_rag_with_files,
        process_bridge_ask,
        process_bridge_ask_stream,
        process_bridge_visual_investigation,
        bridgette_answer,
        bridgette_answer_stream,
        begin_chat,
        end_chat,
        cancel_chat,
        ChatCancelled,
    )
    from routers.services.rag_engine import (
        reset_rag,
        get_last_doc_hash,
        get_document_page_count,
        get_extracted_image_count,
        cancel_processing,
        get_progress,
        CancelledError,
    )


def is_port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def select_backend_host_port() -> tuple[str, int]:
    # Loopback by default for local development. Production gunicorn binds the
    # container interface explicitly and relies on server-enforced auth + TLS.
    host = os.getenv('HOST', '127.0.0.1')
    if os.getenv('PORT'):
        port = int(os.getenv('PORT'))
        if is_port_free(host, port):
            return host, port
        raise RuntimeError(f'Port {port} is already in use on {host}')

    candidates = [5000, 5001, 5002, 8000, 8001, 8002]
    for candidate in candidates:
        if is_port_free(host, candidate):
            return host, candidate
    raise RuntimeError('No available backend port found; all candidate ports are in use.')

from contextlib import asynccontextmanager


def _start_stale_report_reindex() -> None:
    """Re-embed migrated report chunks after TEI becomes ready.

    This runs in one daemon thread and reuses compatible parsed caches, so the
    desktop-to-server migration does not repeat PDF/image extraction.
    """
    if os.getenv("BRIDGEGUARD_REINDEX_STALE_REPORTS", "1").strip().lower() in {
        "0", "false", "no", "off",
    }:
        return

    import threading
    import time

    def worker():
        try:
            try:
                from .routers.services import llm_providers as providers
                from .routers.services.vision import (
                    bridge_reports_index_status,
                    ingest_bridge_reports,
                )
            except ImportError:
                from routers.services import llm_providers as providers
                from routers.services.vision import (
                    bridge_reports_index_status,
                    ingest_bridge_reports,
                )

            for attempt in range(30):
                try:
                    providers.verify_embedding_backend(timeout=10)
                    break
                except Exception as exc:
                    if attempt == 29:
                        print(f"[reindex] TEI unavailable; stale report indexes remain pending: {exc}")
                        return
                    time.sleep(10)

            with sqlite3.connect(f"file:{Path(USER_DB).resolve().as_posix()}?mode=ro", uri=True) as conn:
                bridges = conn.execute(
                    """
                    SELECT DISTINCT state_code, bridge_number
                    FROM bridge_attachments
                    WHERE attachment_type='inspection_report'
                    ORDER BY state_code, bridge_number
                    """
                ).fetchall()
            updated = 0
            for state_code, bridge_number in bridges:
                try:
                    status = bridge_reports_index_status(state_code, bridge_number)
                    if status.get("file_count") and not status.get("ready"):
                        result = ingest_bridge_reports(state_code, bridge_number)
                        if result.get("embeddings") is not None:
                            updated += 1
                except Exception as exc:
                    print(f"[reindex] {state_code}/{bridge_number} failed: {exc}")
            print(f"[reindex] pinned Nomic report indexes ready; rebuilt {updated} cache(s).")
        except Exception as exc:
            print(f"[reindex] background migration failed: {exc}")

    threading.Thread(target=worker, name="report-reindex", daemon=True).start()


# Custom JSON encoder to handle NaN, Inf, etc.
class SafeEncoder(json.JSONEncoder):
    def encode(self, obj):
        if isinstance(obj, float):
            if math.isnan(obj) or math.isinf(obj):
                return None
        return super().encode(obj)
    
    def iterencode(self, obj, _one_shot=False):
        for chunk in super().iterencode(obj, _one_shot):
            yield chunk

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialize the databases. Generation is hosted by Cerebras;
    # embeddings are served by the pinned Nomic TEI sidecar.
    init_db(USER_DB)
    _init_attachments_db()
    print("Database initialized")
    print("[startup] BridgeGuard API ready — Cerebras generation + pinned Nomic TEI embeddings.")
    _start_stale_report_reindex()
    yield
    # Shutdown: the separately managed TEI sidecar owns the embedding model.


app = FastAPI(title="SNBI Bridge API", version="1.0.0", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(point_cloud_router)

# CORS. In the single-server deployment the SPA is served same-origin so CORS is
# not strictly required, but keep it configurable for split (separate-host)
# deployments. Production is same-origin by default; cross-origin access must be
# explicitly enabled with a comma-separated CORS_ORIGINS allowlist.
_cors_env = os.getenv("CORS_ORIGINS", "").strip()
_cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Accept", "Authorization", "Content-Type", "X-Requested-With"],
)
app.add_middleware(AuthMiddleware)


def clean_nan_values(obj):
    """Recursively replace NaN and Inf values with None"""
    if isinstance(obj, dict):
        return {k: clean_nan_values(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_nan_values(item) for item in obj]
    elif isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
    return obj


def _is_legacy_nbi(bridge) -> bool:
    """True when a bridge originated from an NBI record (older NBI-only data).

    Converted/loaded NBI bridges carry ``_SourceData.nbi``. Such rows are
    validated leniently because the SNBI specification (and its structural
    datasets, code lists and date formats) does not apply to legacy NBI data.
    """
    if not isinstance(bridge, dict):
        return False
    source = bridge.get("_SourceData")
    return isinstance(source, dict) and isinstance(source.get("nbi"), dict)


# Centralized data paths (single repo-relative data/ folder; see datapaths.py).
try:
    from .routers.services.datapaths import USER_DB, ATTACHMENTS_DIR, TEMP_DIR
except ImportError:
    from routers.services.datapaths import USER_DB, ATTACHMENTS_DIR, TEMP_DIR

# ====================== BRIDGE ATTACHMENTS (inspection reports) ======================
import re
REPORT_EXTS = {'.pdf', '.doc', '.docx', '.xlsx', '.xls', '.txt', '.csv', '.png', '.jpg', '.jpeg'}
MAX_RPT_BYTES = int(os.getenv("BRIDGEGUARD_MAX_UPLOAD_MB", "50")) * 1024 * 1024
os.makedirs(os.path.join(ATTACHMENTS_DIR, "reports"), exist_ok=True)

_SAFE_BN_RE = re.compile(r'[^A-Za-z0-9_\-]')


def _safe_bridge_id(state_code: str, bridge_number: str) -> str:
    sc = re.sub(r'[^0-9]', '', str(state_code))
    bn = _SAFE_BN_RE.sub('_', str(bridge_number))
    if not sc or not bn:
        raise ValueError("Invalid state_code or bridge_number")
    return f"{sc}_{bn}"


def _safe_upload_name(filename: str | None, fallback: str = "upload") -> str:
    """Strip directories and unsafe characters from an untrusted upload name."""
    basename = Path(filename or fallback).name
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", basename).strip("._")
    return safe[:180] or fallback


async def _read_upload_limited(file: UploadFile, limit: int = MAX_RPT_BYTES) -> bytes:
    """Read an upload without allowing an oversized body to accumulate in RAM."""
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail=f"File too large (max {limit // (1024 * 1024)} MB)")
        chunks.append(chunk)
    return b"".join(chunks)


def _init_attachments_db():
    conn = sqlite3.connect(USER_DB)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS bridge_attachments (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            state_code      TEXT    NOT NULL,
            bridge_number   TEXT    NOT NULL,
            attachment_type TEXT    NOT NULL,
            filename        TEXT    NOT NULL,
            filepath        TEXT    NOT NULL,
            size_bytes      INTEGER,
            uploaded_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


def _portable_attachment_path(filepath: str) -> str:
    """Store paths relative to the persistent data root when possible."""
    try:
        return Path(filepath).resolve().relative_to(Path(USER_DB).parent.resolve()).as_posix()
    except (TypeError, ValueError):
        return str(filepath)


def _resolve_attachment_path(filepath: str) -> Path:
    path = Path(str(filepath))
    return path if path.is_absolute() else Path(USER_DB).parent / path


@app.post("/api/bridges/{state_code}/{bridge_number}/reports")
async def upload_bridge_report(state_code: str, bridge_number: str, file: UploadFile = File(...)):
    """Attach an inspection report file to a specific bridge."""
    try:
        bid = _safe_bridge_id(state_code, bridge_number)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid state_code or bridge_number")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in REPORT_EXTS:
        raise HTTPException(status_code=400, detail=f"Unsupported type '{ext}'. Allowed: {', '.join(sorted(REPORT_EXTS))}")
    content = await _read_upload_limited(file)
    if ext == ".pdf" and not content.startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="The uploaded file is not a valid PDF")
    rpt_dir = os.path.join(ATTACHMENTS_DIR, "reports", bid)
    os.makedirs(rpt_dir, exist_ok=True)
    from datetime import datetime as _dt
    ts = _dt.utcnow().strftime('%Y%m%d_%H%M%S')
    safe_name = _safe_upload_name(file.filename, "report")
    stored_name = f"{ts}_{safe_name}"
    filepath = os.path.join(rpt_dir, stored_name)
    with open(filepath, "wb") as f:
        f.write(content)
    conn = sqlite3.connect(USER_DB)
    c = conn.cursor()
    c.execute("INSERT INTO bridge_attachments (state_code, bridge_number, attachment_type, filename, filepath, size_bytes) VALUES (?,?,?,?,?,?)",
              (state_code, bridge_number, 'inspection_report', file.filename,
               _portable_attachment_path(filepath), len(content)))
    report_id = c.lastrowid
    conn.commit()
    conn.close()
    # Kick off ingestion (extract → embed) in the background with progress so the
    # UI can show a bar; the result is cached for an instant first "Ask".
    try:
        import threading
        from .routers.services.vision import ingest_bridge_reports as _ingest  # type: ignore
    except ImportError:
        from routers.services.vision import ingest_bridge_reports as _ingest
    threading.Thread(
        target=lambda: _ingest(state_code, bridge_number, report_progress=True),
        daemon=True,
    ).start()
    return {"status": "success", "id": report_id, "filename": file.filename, "size_bytes": len(content), "ingestion_started": True}


# Bridges whose report index we've already kicked off warming this process, so
# repeated list calls don't pile up duplicate ingest threads.
_report_warm_started: set = set()


def _warm_bridge_reports(state_code: str, bridge_number: str):
    """Fire-and-forget background ingest so a bridge's reports are parsed/embedded
    BEFORE the user asks (the first Ask otherwise pays the one-time OCR+parse cost).
    Idempotent: ingest_bridge_reports is cache-guarded + per-bridge locked, and we
    only spawn once per bridge per process."""
    key = (str(state_code), str(bridge_number))
    if key in _report_warm_started:
        return
    _report_warm_started.add(key)
    try:
        import threading
        try:
            from .routers.services.vision import ingest_bridge_reports as _ingest  # type: ignore
        except ImportError:
            from routers.services.vision import ingest_bridge_reports as _ingest
        threading.Thread(
            target=lambda: _ingest(state_code, bridge_number, report_progress=True),
            daemon=True,
        ).start()
    except Exception as exc:
        print(f"[warm] report pre-ingest failed to start: {exc}")


@app.get("/api/bridges/{state_code}/{bridge_number}/reports")
def list_bridge_reports(state_code: str, bridge_number: str):
    """List all inspection reports attached to a bridge."""
    conn = sqlite3.connect(USER_DB)
    c = conn.cursor()
    c.execute("SELECT id, filename, size_bytes, uploaded_at FROM bridge_attachments WHERE state_code=? AND bridge_number=? AND attachment_type='inspection_report' ORDER BY id DESC",
              (state_code, bridge_number))
    rows = c.fetchall()
    conn.close()
    # Opening a bridge's reports warms the index in the background so the first Ask
    # is instant instead of triggering a cold parse/OCR mid-question.
    if rows:
        _warm_bridge_reports(state_code, bridge_number)
    return {"reports": [{"id": r[0], "filename": r[1], "size_bytes": r[2], "uploaded_at": r[3]} for r in rows]}


@app.get("/api/bridges/{state_code}/{bridge_number}/reports-status")
def bridge_reports_status(state_code: str, bridge_number: str):
    """Whether this bridge's reports are parsed+embedded and ready to query (no parse
    is triggered just to check). If not ready, kicks off background indexing so the
    UI can show 'Indexing…' then 'Ready'."""
    try:
        from .routers.services.vision import bridge_reports_index_status as _status
    except ImportError:
        from routers.services.vision import bridge_reports_index_status as _status
    try:
        st = _status(state_code, bridge_number)
    except Exception as e:
        return {"file_count": 0, "ready": True, "indexing": False, "error": str(e)}
    if st.get("file_count") and not st.get("ready"):
        _warm_bridge_reports(state_code, bridge_number)  # ensure indexing is running
    return st


@app.get("/api/bridges/{state_code}/{bridge_number}/reports/{report_id}")
def download_bridge_report(state_code: str, bridge_number: str, report_id: int):
    """Download a specific inspection report for a bridge."""
    conn = sqlite3.connect(USER_DB)
    c = conn.cursor()
    c.execute("SELECT filepath, filename FROM bridge_attachments WHERE id=? AND state_code=? AND bridge_number=? AND attachment_type='inspection_report'",
              (report_id, state_code, bridge_number))
    row = c.fetchone()
    conn.close()
    resolved = _resolve_attachment_path(row[0]) if row else None
    if not row or resolved is None or not resolved.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    return FileResponse(resolved, filename=row[1], media_type="application/octet-stream")


@app.delete("/api/bridges/{state_code}/{bridge_number}/reports/{report_id}")
def delete_bridge_report(state_code: str, bridge_number: str, report_id: int):
    """Delete a specific inspection report for a bridge."""
    conn = sqlite3.connect(USER_DB)
    c = conn.cursor()
    c.execute("SELECT filepath FROM bridge_attachments WHERE id=? AND state_code=? AND bridge_number=? AND attachment_type='inspection_report'",
              (report_id, state_code, bridge_number))
    row = c.fetchone()
    resolved = _resolve_attachment_path(row[0]) if row else None
    if resolved is not None and resolved.exists():
        resolved.unlink()
    c.execute("DELETE FROM bridge_attachments WHERE id=? AND state_code=? AND bridge_number=? AND attachment_type='inspection_report'",
              (report_id, state_code, bridge_number))
    conn.commit()
    conn.close()
    return {"status": "success"}


# ====================== CONVERTER ======================
@app.post("/api/convert")
async def convert(txt_file: UploadFile = File(...), xml_file: Optional[UploadFile] = File(None)):
    try:
        # Write uploads under the configured data/temp dir (honors
        # BRIDGEGUARD_DATA_DIR / SNBI_TEMP_DIR) instead of a CWD-relative folder
        # so the app works under gunicorn/EB where the CWD may not be writable.
        tmp_dir = Path(TEMP_DIR)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        txt_path = tmp_dir / _safe_upload_name(txt_file.filename, "bridges.txt")
        xml_path = None

        with open(txt_path, "wb") as f:
            f.write(await _read_upload_limited(txt_file))
        if xml_file and xml_file.filename:
            xml_path = tmp_dir / _safe_upload_name(xml_file.filename, "elements.xml")
            with open(xml_path, "wb") as f:
                f.write(await _read_upload_limited(xml_file))

        bridges_data = parse_nbi_txt(txt_path)
        elements_by_struc = parse_elements_xml(xml_path) if xml_path else {}

        json_bridges: List[Dict] = []
        source_data_by_bridge: Dict[str, Dict] = {}
        skipped = 0

        for struc, bridge_data in bridges_data.items():
            old = bridge_data.get("over")
            if not old:
                skipped += 1
                continue
            unders = bridge_data.get("unders", [])
            try:
                snbi = map_bridge_to_snbi(old, unders)
                if elems := elements_by_struc.get(struc):
                    snbi["Elements"] = elems
                source_data_by_bridge[str(snbi.get("BID01") or struc)] = {"nbi": old}
                json_bridges.append(snbi)
            except Exception as e:
                print(f"Mapping error for {struc}: {e}")
                skipped += 1

        normalized_bridges, parity_errors = normalize_snbi_payload(json_bridges)

        # NJ-only build: keep only New Jersey (state code 34) bridges.
        def _is_nj(v) -> bool:
            try:
                return int(str(v).strip()) == 34
            except (TypeError, ValueError):
                return False
        before_nj = len(normalized_bridges)
        normalized_bridges = [b for b in normalized_bridges if _is_nj(b.get('BL01'))]
        dropped_non_nj = before_nj - len(normalized_bridges)
        if not normalized_bridges:
            txt_path.unlink(missing_ok=True)
            if xml_path:
                xml_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=400,
                detail="This build only converts New Jersey bridges (state code 34). No NJ bridges were found in the upload.",
            )

        for bridge in normalized_bridges:
            bridge_id = str(bridge.get("BID01") or "")
            if bridge_id in source_data_by_bridge:
                bridge["_SourceData"] = source_data_by_bridge[bridge_id]
        coverage = summarize_payload_coverage(normalized_bridges)
        section_coverage = summarize_payload_coverage_by_section(normalized_bridges)
        validation_payload = [
            {k: v for k, v in bridge.items() if not str(k).startswith("_")}
            for bridge in normalized_bridges
        ]
        # Every converted bridge originates from an NBI record, so it is legacy:
        # validate it leniently (skip SNBI-only structural/code/date checks).
        legacy_flags = [_is_legacy_nbi(bridge) for bridge in normalized_bridges]
        validation = validate_snbi_payload(validation_payload, legacy_flags=legacy_flags)

        # Cleanup
        txt_path.unlink(missing_ok=True)
        if xml_path:
            xml_path.unlink(missing_ok=True)

        return {
            "status": "success",
            "bridges": normalized_bridges,
            "converted": len(normalized_bridges),
            "skipped": skipped,
            "dropped_non_nj": dropped_non_nj,
            "elements_from_xml": xml_path is not None,
            "parity_errors": parity_errors,
            "parity_ok": len(parity_errors) == 0,
            "coverage": coverage,
            "section_coverage": section_coverage,
            "validation": validation,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ====================== REVERSE CONVERTER ======================
@app.post("/api/reverse-convert")
async def reverse_convert(file: UploadFile = File(...)):
    """Convert SNBI JSON back to flat JSON and human-readable text."""
    try:
        content = await _read_upload_limited(file)
        if not content:
            raise HTTPException(status_code=400, detail="Uploaded JSON file is empty")

        # Accept UTF-8 BOM files as well.
        decoded = content.decode("utf-8-sig")
        data = json.loads(decoded)

        # Accept both raw bridge arrays and wrapped conversion responses.
        # Supported inputs:
        # 1) [ { ...bridge... }, ... ]
        # 2) { ...bridge... }
        # 3) { "bridges": [ ... ] }
        if isinstance(data, dict) and isinstance(data.get("bridges"), list):
            data = data["bridges"]
        elif isinstance(data, dict):
            data = [data]

        if not isinstance(data, list) or len(data) == 0:
            raise HTTPException(
                status_code=400,
                detail="JSON must be an array of SNBI bridges, a single SNBI bridge object, or an object with a 'bridges' array"
            )

        if not all(isinstance(item, dict) for item in data):
            raise HTTPException(status_code=400, detail="Each bridge entry must be a JSON object")

        normalized_data, parity_errors = normalize_snbi_payload(data)

        result = convert_snbi_to_outputs(normalized_data)
        return {
            "status": "success",
            "flat_json": result["flat_json"],
            "text": result["text"],
            "nbi_csv": result["nbi_csv"],
            "elements_xml": result["elements_xml"],
            "count": result["count"],
            "warnings": parity_errors[:50] if parity_errors else [],
        }
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Invalid file encoding. Please upload UTF-8 JSON.")
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ====================== LOADER ======================
@app.post("/api/load-json")
async def load_json(file: UploadFile = File(...), data_year: Optional[int] = Form(None)):
    try:
        if data_year is not None and not (1990 <= int(data_year) <= 2100):
            raise HTTPException(status_code=400, detail="Data year must be between 1990 and 2100")

        content = await _read_upload_limited(file)
        data = json.loads(content)
        
        # Ensure data is a list
        if not isinstance(data, list):
            raise HTTPException(status_code=400, detail="JSON must be an array of bridges")
        
        if len(data) == 0:
            raise HTTPException(status_code=400, detail="JSON array is empty")
        
        # Validate first bridge has required keys
        first_bridge = data[0]
        print(f"\n{'='*60}")
        print(f"LOADER DEBUG: Validating {len(data)} bridges from uploaded JSON")
        print(f"{'='*60}")
        print(f"First bridge keys: {sorted(list(first_bridge.keys()))}")
        print(f"First bridge BL01 value: {first_bridge.get('BL01')} (type: {type(first_bridge.get('BL01')).__name__})")
        print(f"First bridge BID01 value: {first_bridge.get('BID01')} (type: {type(first_bridge.get('BID01')).__name__})")
        print(f"Full first bridge preview:\n{json.dumps(first_bridge, indent=2, default=str)}")
        print(f"{'='*60}\n")
        
        has_bl01 = 'BL01' in first_bridge
        has_bid01 = 'BID01' in first_bridge
        
        if not has_bl01:
            raise HTTPException(
                status_code=400, 
                detail=f"JSON bridges missing 'BL01' (state_code) field. Found keys: {', '.join(list(first_bridge.keys())[:15])}"
            )
        
        if not has_bid01:
            raise HTTPException(
                status_code=400,
                detail=f"JSON bridges missing 'BID01' (bridge_number) field. Found keys: {', '.join(list(first_bridge.keys())[:15])}"
            )

        # NJ-only build: reject any non-New-Jersey (state code 34) bridges.
        def _is_nj(v) -> bool:
            try:
                return int(str(v).strip()) == 34
            except (TypeError, ValueError):
                return False
        non_nj = [b for b in data if isinstance(b, dict) and not _is_nj(b.get('BL01'))]
        if non_nj:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"This build only accepts New Jersey bridges (state code 34). "
                    f"{len(non_nj)} of {len(data)} record(s) are from other states."
                ),
            )

        dict_bridges = [bridge for bridge in data if isinstance(bridge, dict)]
        validation_payload = [
            {k: v for k, v in bridge.items() if not str(k).startswith("_")}
            for bridge in dict_bridges
        ]
        # NBI-converted uploads carry _SourceData.nbi; those rows are validated
        # leniently so old NBI-only years are not flooded with SNBI-only errors.
        legacy_flags = [_is_legacy_nbi(bridge) for bridge in dict_bridges]
        validation = validate_snbi_payload(
            validation_payload, submission_year=data_year, legacy_flags=legacy_flags
        )
        inserted, already_existed, skip_count, skip_reasons = load_json_to_db(
            USER_DB, data, data_year_override=data_year
        )

        if inserted == 0 and already_existed == 0:
            detail = (
                f"No bridges were loaded. All {len(data)} records were skipped "
                f"(missing BL01/BID01 fields or mapping errors)."
            )
            if skip_reasons:
                detail += " Reasons: " + "; ".join(skip_reasons[:10])
            raise HTTPException(status_code=400, detail=detail)

        if inserted == 0 and already_existed > 0:
            return {
                "status": "conflict",
                "loaded": 0,
                "already_existed": already_existed,
                "skipped": skip_count,
                "skipped_reasons": skip_reasons,
                "message": (
                    f"All {already_existed} bridge(s) are already loaded for data year {data_year}. "
                    f"Delete data year {data_year} first, then reload."
                ),
                "data_year": data_year,
                "validation": validation,
            }

        year_suffix = f" for data year {data_year}" if data_year is not None else ""
        msg = f"Loaded {inserted} new bridge snapshot(s){year_suffix} into the database."
        if already_existed:
            msg += f" ({already_existed} existing snapshot(s) refreshed.)"
        if skip_count:
            msg += f" {skip_count} record(s) were skipped — see details below."
        return {
            "status": "success",
            "loaded": inserted,
            "already_existed": already_existed,
            "skipped": skip_count,
            "skipped_reasons": skip_reasons,
            "data_year": data_year,
            "message": msg,
            "validation": validation,
        }
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in load_json: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error loading JSON: {str(e)}")


@app.post("/api/inspection-reports/extract")
async def extract_inspection_reports(
    files: List[UploadFile] = File(...),
    save_to_history: bool = Form(False),
):
    """Extract schema fields from uploaded inspection PDF folder files."""
    try:
        payloads: List[Dict] = []
        for f in files:
            content = await _read_upload_limited(f)
            payloads.append({'filename': f.filename, 'content': content})

        extracted = extract_inspection_reports_to_schema(payloads)
        successful = [x for x in extracted if x.get('status') == 'success']

        saved = 0
        if save_to_history and successful:
            saved = save_inspection_report_extractions(USER_DB, successful)

        return {
            'status': 'success',
            'total_files': len(files),
            'extracted_count': len(successful),
            'saved_count': saved,
            'records': clean_nan_values(extracted),
        }
    except Exception as e:
        print(f"Inspection report extraction error: {e}")
        return JSONResponse(status_code=500, content={'status': 'error', 'detail': str(e)})


@app.get('/api/inspection-reports/history')
def inspection_report_history(limit: int = 100):
    """List recent extracted inspection report rows."""
    try:
        rows = get_inspection_report_extractions(USER_DB, limit=limit)
        return {'status': 'success', 'count': len(rows), 'rows': clean_nan_values(rows)}
    except Exception as e:
        print(f"Inspection report history error: {e}")
        return JSONResponse(status_code=500, content={'status': 'error', 'detail': str(e)})


@app.post('/api/inspection-reports/validate')
async def validate_inspection_reports(payload: dict):
    """Validate extracted inspection report records before DB apply."""
    try:
        rows = payload.get('records') or []
        if not isinstance(rows, list) or len(rows) == 0:
            return JSONResponse(status_code=400, content={'status': 'error', 'detail': 'records must be a non-empty array'})

        validation = validate_inspection_report_records(USER_DB, rows)
        return {'status': 'success', **clean_nan_values(validation)}
    except Exception as e:
        print(f"Inspection report validation error: {e}")
        return JSONResponse(status_code=500, content={'status': 'error', 'detail': str(e)})


@app.post('/api/inspection-reports/apply')
async def apply_inspection_reports(payload: dict):
    """Apply validated inspection report records to history and optionally bridge snapshot."""
    try:
        rows = payload.get('records') or []
        update_bridge_snapshot = bool(payload.get('update_bridge_snapshot', False))
        if not isinstance(rows, list) or len(rows) == 0:
            return JSONResponse(status_code=400, content={'status': 'error', 'detail': 'records must be a non-empty array'})

        result = apply_validated_inspection_report_records(
            USER_DB,
            rows,
            update_bridge_snapshot=update_bridge_snapshot,
        )
        return {'status': 'success', **clean_nan_values(result)}
    except Exception as e:
        print(f"Inspection report apply error: {e}")
        return JSONResponse(status_code=500, content={'status': 'error', 'detail': str(e)})



@app.post("/api/vision-rag")
async def vision_rag(
    file: UploadFile = File(...),
    question: str = Form(...),
    model: str = Form(''),
    strict_grounding: bool = Form(False),
    output_format: str = Form('text'),
    target_fields: str = Form(''),
    force_defect_segmentation: bool = Form(False),
    defect_segmentation_mode: str = Form('balanced'),
):
    try:
        response = await process_vision_rag(
            file,
            question,
            model=model,
            strict_grounding=strict_grounding,
            output_format=output_format,
            target_fields=target_fields,
            force_defect_segmentation=force_defect_segmentation,
            defect_segmentation_mode=defect_segmentation_mode,
        )
        return response
    except CancelledError:
        return JSONResponse(status_code=499, content={"status": "cancelled", "detail": "Processing cancelled by user."})
    except HTTPException:
        raise
    except Exception as e:
        print(f"Vision RAG error: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(e)})


@app.post("/api/vision-query")
async def vision_query(
    question: str = Form(...),
    model: str = Form(''),
    strict_grounding: bool = Form(False),
    output_format: str = Form('text'),
    target_fields: str = Form(''),
    force_defect_segmentation: bool = Form(False),
    defect_segmentation_mode: str = Form('balanced'),
    doc_hash: str = Form(''),
):
    try:
        response = await process_vision_query_only(
            question,
            model=model,
            strict_grounding=strict_grounding,
            output_format=output_format,
            target_fields=target_fields,
            force_defect_segmentation=force_defect_segmentation,
            defect_segmentation_mode=defect_segmentation_mode,
            doc_hash=doc_hash.strip() or None,
        )
        return response
    except HTTPException:
        raise
    except Exception as e:
        print(f"Vision query error: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(e)})


@app.post("/api/db-rag")
async def db_rag(
    question: str = Form(...),
    model: str = Form(''),
    state_code: int = Form(None),
    bridge_number: str = Form(None),
    strict_grounding: bool = Form(False),
    output_format: str = Form('text'),
    target_fields: str = Form(''),
    selected_sources: str = Form(None),
    force_defect_segmentation: bool = Form(False),
    defect_segmentation_mode: str = Form('balanced'),
):
    import asyncio
    begin_chat()  # arm cancellable streaming so /api/ask-cancel can stop it
    try:
        # Optional source filter (JSON list of 'Database' + filenames). None = all.
        sources = None
        if selected_sources:
            try:
                parsed = json.loads(selected_sources)
                if isinstance(parsed, list):
                    sources = parsed
            except Exception:
                sources = None
        if bridge_number:
            # Per-bridge multimodal Ask: full Vision-style routing over the bridge's
            # report files (any format) blended with its database record.
            response = await process_bridge_ask(
                question,
                model=model,
                state_code=state_code,
                bridge_number=bridge_number,
                strict_grounding=strict_grounding,
                output_format=output_format,
                target_fields=target_fields,
                selected_sources=sources,
                force_defect_segmentation=force_defect_segmentation,
                defect_segmentation_mode=defect_segmentation_mode,
            )
        else:
            # Database-wide Ask (no specific bridge): DB record only. Run off the
            # event loop so a parallel /api/ask-cancel can stop the generation.
            response = await asyncio.to_thread(
                process_db_rag_with_files,
                question,
                model=model,
                state_code=state_code,
                bridge_number=bridge_number,
                strict_grounding=strict_grounding,
                output_format=output_format,
                target_fields=target_fields,
                selected_sources=sources,
            )
        return response
    except ChatCancelled:
        return JSONResponse(status_code=499, content={"status": "cancelled", "detail": "Stopped by user."})
    except HTTPException:
        raise
    except Exception as e:
        print(f"Database RAG error: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(e)})
    finally:
        end_chat()


@app.post('/api/vision-reset')
def vision_reset_index():
    """Clear the RAG index and document cache so the next upload re-processes from scratch."""
    try:
        reset_rag()
        return {'status': 'success', 'message': 'RAG index and document cache cleared.'}
    except Exception as e:
        print(f"Vision reset error: {e}")
        return JSONResponse(status_code=500, content={'status': 'error', 'detail': str(e)})


@app.post('/api/vision-cancel')
def vision_cancel():
    """Cancel any in-progress document processing."""
    try:
        cancel_processing()
        return {'status': 'success', 'message': 'Cancellation requested.'}
    except Exception as e:
        return JSONResponse(status_code=500, content={'status': 'error', 'detail': str(e)})


@app.get('/api/vision-progress')
def vision_progress():
    """Return current processing progress and ETA."""
    try:
        info = get_progress()
        return {'status': 'success', **info}
    except Exception as e:
        return JSONResponse(status_code=500, content={'status': 'error', 'detail': str(e)})


@app.get('/api/vision-index-status')
def vision_index_status(doc_hash: str = ''):
    """Return whether a document is currently indexed in Vision RAG cache."""
    try:
        resolved_hash = None
        try:
            from routers.services.rag_engine import resolve_doc_hash
        except ImportError:
            from .routers.services.rag_engine import resolve_doc_hash

        resolved_hash = resolve_doc_hash(doc_hash.strip() or None)
        if not resolved_hash:
            return {
                'status': 'success',
                'indexed': False,
                'doc_hash': None,
                'pages': 0,
                'images': 0,
            }

        return {
            'status': 'success',
            'indexed': True,
            'doc_hash': resolved_hash,
            'pages': get_document_page_count(resolved_hash),
            'images': get_extracted_image_count(resolved_hash),
        }
    except Exception as e:
        print(f"Vision index status error: {e}")
        return JSONResponse(status_code=500, content={'status': 'error', 'detail': str(e)})


# ====================== FIXED AI PROVIDER STATUS ======================
@app.get('/api/ai-provider')
def get_ai_provider_setting():
    """Compatibility status endpoint for the fixed Cerebras + TEI deployment."""
    try:
        from routers.services import llm_providers
    except ImportError:
        from .routers.services import llm_providers
    cerebras_key_set = bool(os.getenv('CEREBRAS_API_KEY', '').strip())
    embedding = llm_providers.embedding_profile()
    message = 'Cerebras serves text and vision; pinned Nomic TEI serves 768-D embeddings.'
    if not cerebras_key_set:
        message += ' WARNING: CEREBRAS_API_KEY is not set.'
    return {
        'provider': 'cerebras',
        'cerebras_key_set': cerebras_key_set,
        'embedding_provider': embedding['provider'],
        'embedding_model': embedding['model'],
        'embedding_revision': embedding['revision'],
        'embedding_dimension': embedding['dimension'],
        'message': message,
    }


@app.get('/api/api-usage')
def get_api_usage():
    """Local daily hosted API counters for QA, images, and embeddings."""
    try:
        try:
            from routers.services import llm_providers as _providers
        except ImportError:
            from .routers.services import llm_providers as _providers
        return clean_nan_values(_providers.usage_snapshot())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ====================== RAG SETTINGS ======================
@app.get('/api/rag-settings')
def get_rag_settings():
    """Return the current RAG pipeline settings."""
    try:
        from routers.services import llm_providers, rag_engine
    except ImportError:
        from .routers.services import llm_providers, rag_engine
    embedding = llm_providers.embedding_profile()
    return {
        'embedding_provider': embedding['provider'],
        'embed_model': embedding['model'],
        'embed_dim': embedding['dimension'],
        'embed_revision': embedding['revision'],
        'embed_input_policy': embedding['input_policy'],
        'vision_model': rag_engine.VISION_MODEL,
        'llm_model': rag_engine.LLM_MODEL,
        'chunk_size': getattr(rag_engine, 'CHUNK_SIZE', 500),
        'chunk_overlap': getattr(rag_engine, 'CHUNK_OVERLAP', 100),
    }


@app.post('/api/rag-settings')
def update_rag_settings(body: dict):
    """Update runtime-safe RAG settings.

    The embedding profile is restart-scoped because changing it requires a new
    vector namespace and re-index. Ignore the legacy ``embed_model`` field
    rather than letting the UI claim that an unsafe hot switch succeeded.
    """
    try:
        from routers.services import rag_engine
    except ImportError:
        from .routers.services import rag_engine
    if 'chunk_size' in body:
        rag_engine.CHUNK_SIZE = max(100, int(body['chunk_size']))
    if 'chunk_overlap' in body:
        rag_engine.CHUNK_OVERLAP = max(0, int(body['chunk_overlap']))
    return {'status': 'success', 'message': 'Settings updated.'}


# ====================== OTHER USEFUL ENDPOINTS ======================
@app.get("/api/tables")
def get_tables():
    """Get list of all available tables in the database"""
    return get_all_tables(USER_DB)


@app.get("/api/states")
def get_states():
    """Return all state codes and names that currently have data in the bridges table."""
    import sqlite3 as _sql
    conn = _sql.connect(USER_DB, check_same_thread=False)
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT state_code FROM bridges ORDER BY state_code")
        rows = cur.fetchall()
    finally:
        conn.close()
    result = []
    for (code,) in rows:
        name = STATE_MAP.get(code, f"State {code}")
        result.append({"state_code": code, "state_name": name})
    return result


@app.get("/api/table/{table_name}")
def get_table(table_name: str, state_code: int = None, bridge_number: str = None, limit: int = None, year: int = None):
    """Get data from a specific table, optionally filtered by state_code, bridge_number, year and limited."""
    try:
        data = get_table_data(USER_DB, table_name, state_code, bridge_number, limit, year)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return clean_nan_values(data)


# ====================== DB ADMIN ENDPOINTS ======================
@app.get("/api/table-schema/{table_name}")
def table_schema(table_name: str):
    """Get column schema for a table."""
    schema = get_table_schema(USER_DB, table_name)
    if not schema:
        raise HTTPException(status_code=404, detail="Table not found")
    return schema


@app.get("/api/table-rows/{table_name}")
def table_rows(table_name: str, limit: int = 100, offset: int = 0):
    """Get rows from a table with rowid for editing. Supports pagination."""
    import sqlite3 as _sql
    conn = _sql.connect(USER_DB, check_same_thread=False)
    try:
        cur = conn.cursor()
        # Validate table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Table not found")

        # Get total count
        cur.execute(f"SELECT COUNT(*) FROM {table_name}")
        total = cur.fetchone()[0]

        # Get rows WITH rowid
        cur.execute(f"SELECT rowid, * FROM {table_name} LIMIT ? OFFSET ?", (limit, offset))
        columns = [desc[0] for desc in cur.description]
        rows = [dict(zip(columns, row)) for row in cur.fetchall()]

        return clean_nan_values({"rows": rows, "total": total, "columns": columns})
    finally:
        conn.close()


@app.post("/api/table/{table_name}/row")
async def add_table_row(table_name: str, payload: dict):
    """Insert a new row into a table."""
    try:
        new_id = insert_table_row(USER_DB, table_name, payload)
        return {"status": "success", "rowid": new_id}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"status": "error", "detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(e)})


@app.put("/api/table/{table_name}/row/{row_id}")
async def edit_table_row(table_name: str, row_id: int, payload: dict):
    """Update a row by rowid."""
    try:
        updated = update_table_row(USER_DB, table_name, row_id, payload)
        if updated == 0:
            return JSONResponse(status_code=404, content={"status": "error", "detail": "Row not found or no valid columns"})
        return {"status": "success", "updated": updated}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"status": "error", "detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(e)})


@app.delete("/api/table/{table_name}/row/{row_id}")
async def remove_table_row(table_name: str, row_id: int):
    """Delete a row by rowid."""
    try:
        deleted = delete_table_row(USER_DB, table_name, row_id)
        if deleted == 0:
            return JSONResponse(status_code=404, content={"status": "error", "detail": "Row not found"})
        return {"status": "success", "deleted": deleted}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"status": "error", "detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(e)})


@app.delete("/api/clear-data")
async def clear_all_database_data():
    """Delete all rows from all tables (bridges, features, elements, etc.) and reset sequences."""
    try:
        deleted = clear_all_data(USER_DB)
        return {"status": "success", "deleted": deleted}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(e)})


@app.post("/api/sql-query")
async def run_sql_query(payload: dict):
    """Execute a read-only SQL query."""
    sql = payload.get("sql", "").strip()
    if not sql:
        return JSONResponse(status_code=400, content={"status": "error", "detail": "No SQL provided"})
    try:
        result = execute_readonly_sql(USER_DB, sql)
        return clean_nan_values(result)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"status": "error", "detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(e)})


@app.get("/api/loaded-years")
def get_all_loaded_years():
    """Return all distinct data_year values that have been loaded into the bridges table."""
    try:
        years = get_loaded_years(USER_DB)
        return {"years": years}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/available-years/{state_code}")
def get_available_years_for_state(state_code: int):
    """Return distinct inspection report years loaded for a state."""
    try:
        years = get_available_years(USER_DB, state_code)
        return {"years": years}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/export-bridges/{state_code}")
def export_bridges_by_state(state_code: int, year: int = None):
    """Export all bridges for a state as SNBI JSON objects (one dict per bridge)."""
    try:
        data = export_all_bridges_snbi(USER_DB, state_code, year=year)
        return clean_nan_values(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/validate-bridges/{state_code}")
def validate_bridges_by_state(state_code: int, year: int = None):
    """Validate exported SNBI JSON for a state/year without blocking export."""
    try:
        data = export_all_bridges_snbi(USER_DB, state_code, year=year)
        return clean_nan_values(validate_snbi_payload(data, submission_year=year))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/validate-bridge/{state_code}/{bridge_number}")
def validate_single_bridge(state_code: int, bridge_number: str, year: int = None):
    """Validate one exported SNBI bridge and return field-indexed issues."""
    try:
        state_data = export_all_bridges_snbi(USER_DB, state_code, year=year)
        bridge_number_text = str(bridge_number)
        bridge_payload = [
            bridge for bridge in state_data
            if str(bridge.get("BID01", "")) == bridge_number_text
        ]
        if not bridge_payload:
            raise HTTPException(status_code=404, detail="Bridge not found for validation")
        return clean_nan_values(validate_snbi_payload(bridge_payload, issue_limit=1000, submission_year=year))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/bridge-field-metadata/{state_code}/{bridge_number}")
def bridge_field_metadata(state_code: int, bridge_number: str, year: int = None):
    """Return editor-only source/default/temporary metadata for one bridge."""
    try:
        return clean_nan_values(get_bridge_field_metadata(USER_DB, state_code, bridge_number, year=year))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/bridge/{state_code}/{bridge_number}")
def get_bridge_details(state_code: int, bridge_number: str, year: int = None):
    """Get one yearly bridge snapshot, defaulting to the bridge's latest year."""
    data = get_bridge_details_db(USER_DB, state_code, bridge_number, year=year)
    if data is None:
        raise HTTPException(status_code=404, detail="Bridge not found")
    return clean_nan_values(data)


@app.put("/api/bridge/{state_code}/{bridge_number}")
async def update_bridge(state_code: int, bridge_number: str, payload: dict, year: int = None):
    """Persist edits for exactly one immutable yearly bridge snapshot."""
    try:
        updated = update_bridge_main(USER_DB, state_code, bridge_number, payload, year=year)
        if updated == 0:
            return JSONResponse(status_code=400, content={"status": "error", "detail": "No editable fields were provided or bridge was not found."})
        response_year = year if year is not None else payload.get("data_year")
        data = get_bridge_details_db(USER_DB, state_code, bridge_number, year=response_year)
        return {"status": "success", "updated": updated, "data": clean_nan_values(data)}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"status": "error", "detail": str(e)})
    except Exception as e:
        print(f"Bridge update error: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(e)})


@app.get("/api/map-data")
def get_map_data(state_code: int = None, county_code: int = None, year: int = None, limit: int = None):
    data = get_bridges_for_map(
        USER_DB,
        state_code=state_code,
        county_code=county_code,
        year=year,
        limit=limit,
    )
    return clean_nan_values(data)


@app.get("/api/map-counties")
def get_map_county_options(state_code: int = None, year: int = None):
    data = get_map_counties(USER_DB, state_code=state_code, year=year)
    return clean_nan_values(data)


@app.get("/api/kpis")
def get_kpis(year: int = None):
    data = get_state_kpis(USER_DB, year=year)
    return clean_nan_values(data)


@app.get("/api/kpis/county")
def get_kpis_county(state_code: int = 34, year: int = None):
    """Per-county KPIs for a state (default New Jersey)."""
    data = get_county_kpis(USER_DB, state_code=state_code, year=year)
    return clean_nan_values(data)


@app.delete("/api/bridges/by-year")
def delete_bridges_for_year(state_code: int = 34, data_year: int = None):
    """Delete all bridges for a state + data year (NJ-only build defaults to 34)."""
    if data_year is None:
        raise HTTPException(status_code=400, detail="data_year is required")
    deleted = delete_bridges_by_year(USER_DB, state_code=state_code, data_year=data_year)
    return {"status": "success", "deleted": deleted, "state_code": state_code, "data_year": data_year}


@app.get("/api/bridges/{state_code}/{bridge_number}/years")
def bridge_data_years(state_code: int, bridge_number: str):
    """Distinct data years this bridge has a snapshot for (newest first)."""
    conn = sqlite3.connect(USER_DB)
    try:
        rows = conn.execute(
            "SELECT DISTINCT data_year FROM bridges WHERE state_code=? AND bridge_number=? "
            "AND data_year IS NOT NULL ORDER BY data_year DESC",
            (state_code, bridge_number)).fetchall()
    finally:
        conn.close()
    return {"years": [r[0] for r in rows]}


@app.delete("/api/bridges/{state_code}/{bridge_number}/year/{data_year}")
def delete_one_bridge_year(state_code: int, bridge_number: str, data_year: int):
    """Delete a SINGLE bridge's snapshot for one year (SNBI tables only; custom
    tables + attachments are left intact)."""
    deleted = delete_single_bridge(USER_DB, state_code, bridge_number, data_year)
    return {"status": "success", "deleted": deleted, "state_code": state_code,
            "bridge_number": bridge_number, "data_year": data_year}


# ====================== USER CUSTOM TABLES (separate from SNBI) ======================
@app.get("/api/bridges/{state_code}/{bridge_number}/custom-tables")
def get_custom_tables(state_code: int, bridge_number: str):
    """Custom tables visible for this bridge (its own + state-wide), with its rows."""
    return {"tables": list_custom_tables_for_bridge(USER_DB, state_code, bridge_number)}


@app.post("/api/custom-tables")
async def create_custom_table_ep(request: Request):
    """Create a custom table. Body: {state_code, bridge_number, name, columns[], color, scope}."""
    b = await request.json()
    name = (b.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if b.get("state_code") is None:
        raise HTTPException(status_code=400, detail="state_code is required")
    scope = b.get("scope") or "bridge"
    if scope == "bridge" and not b.get("bridge_number"):
        raise HTTPException(status_code=400, detail="bridge_number is required for a per-bridge table")
    tid = create_custom_table(USER_DB, int(b["state_code"]), b.get("bridge_number"),
                              name, b.get("columns") or [], b.get("color") or "#8b5cf6", scope)
    return {"status": "success", "id": tid}


@app.post("/api/custom-tables/{table_id}/rows")
async def add_custom_table_row(table_id: int, request: Request):
    """Add a row to a custom table for a specific bridge. Body: {bridge_number, cells{}}."""
    b = await request.json()
    if not b.get("bridge_number"):
        raise HTTPException(status_code=400, detail="bridge_number is required")
    rid = add_custom_row(USER_DB, table_id, str(b["bridge_number"]), b.get("cells") or {})
    return {"status": "success", "id": rid}


@app.put("/api/custom-tables/rows/{row_id}")
async def edit_custom_table_row(row_id: int, request: Request):
    b = await request.json()
    update_custom_row(USER_DB, row_id, b.get("cells") or {})
    return {"status": "success"}


@app.delete("/api/custom-tables/rows/{row_id}")
def remove_custom_table_row(row_id: int):
    delete_custom_row(USER_DB, row_id)
    return {"status": "success"}


@app.delete("/api/custom-tables/{table_id}")
def remove_custom_table(table_id: int):
    """Delete a custom-table definition and all its rows (across every bridge)."""
    delete_custom_table(USER_DB, table_id)
    return {"status": "success"}


@app.post("/api/bridgette")
def bridgette(question: str = Form(...), model: str = Form('')):
    """Bridgette — natural-language question over the NJ bridge DB (text-to-SQL).

    Sync `def` on purpose: FastAPI runs it in the threadpool so the event loop
    stays free to serve a parallel /api/ask-cancel and actually stop generation.
    """
    if not question.strip():
        raise HTTPException(status_code=400, detail="question is required")
    try:
        # SQL results can contain NaN (NULL numerics) which is not JSON-compliant;
        # sanitize like the other DB endpoints before returning.
        return clean_nan_values(bridgette_answer(question.strip(), model=model))
    except ChatCancelled:
        return JSONResponse(status_code=499, content={"status": "cancelled", "detail": "Stopped by user."})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/bridgette/stream")
def bridgette_stream(question: str = Form(...), model: str = Form('')):
    """Streamed Bridgette: emits newline-delimited JSON events (meta → tokens →
    done) so the UI renders the answer as it generates. Sync `def` → runs in the
    threadpool, keeping the loop free for /api/ask-cancel."""
    if not question.strip():
        raise HTTPException(status_code=400, detail="question is required")

    def gen():
        try:
            for evt in bridgette_answer_stream(question.strip(), model=model):
                yield json.dumps(clean_nan_values(evt)) + "\n"
        except ChatCancelled:
            yield json.dumps({"type": "stopped"}) + "\n"
        except Exception as e:
            yield json.dumps({"type": "error", "detail": str(e)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/api/db-rag/stream")
async def db_rag_stream(
    question: str = Form(...),
    model: str = Form(''),
    state_code: int = Form(None),
    bridge_number: str = Form(None),
    strict_grounding: bool = Form(False),
    output_format: str = Form('text'),
    target_fields: str = Form(''),
    selected_sources: str = Form(None),
    force_defect_segmentation: bool = Form(False),
    defect_segmentation_mode: str = Form('balanced'),
):
    """Streamed per-bridge Ask: NDJSON events (meta → tokens → done), with
    image/page routes returned as a single 'final' event. Falls back to the
    non-streaming DB-wide ask when no bridge is given."""
    sources = None
    if selected_sources:
        try:
            parsed = json.loads(selected_sources)
            if isinstance(parsed, list):
                sources = parsed
        except Exception:
            sources = None

    async def gen():
        try:
            if bridge_number:
                async for evt in process_bridge_ask_stream(
                    question, model=model, state_code=state_code, bridge_number=bridge_number,
                    strict_grounding=strict_grounding, output_format=output_format, target_fields=target_fields,
                    selected_sources=sources, force_defect_segmentation=force_defect_segmentation,
                    defect_segmentation_mode=defect_segmentation_mode,
                ):
                    yield json.dumps(clean_nan_values(evt)) + "\n"
            else:
                import asyncio as _aio
                begin_chat()
                try:
                    resp = await _aio.to_thread(
                        process_db_rag_with_files, question, model=model, state_code=state_code,
                        bridge_number=bridge_number, strict_grounding=strict_grounding,
                        output_format=output_format, target_fields=target_fields, selected_sources=sources)
                finally:
                    end_chat()
                yield json.dumps(clean_nan_values({"type": "final", "response": resp})) + "\n"
        except ChatCancelled:
            yield json.dumps({"type": "stopped"}) + "\n"
        except Exception as e:
            yield json.dumps({"type": "error", "detail": str(e)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/api/bridge-visual-investigation")
async def bridge_visual_investigation(
    query: str = Form(...),
    state_code: int = Form(...),
    bridge_number: str = Form(...),
    selected_sources: str = Form(None),
    defect_segmentation_mode: str = Form('fast'),
):
    """Dedicated report-image detection endpoint.

    Normal Ask stays text/DB/index based; this route performs on-demand VLM bbox
    detection only when Ask explicitly enables visual investigation. Candidate
    images are selected automatically from indexed image descriptions, with a
    fallback scan when the index signal is weak.
    """
    sources = None
    if selected_sources:
        try:
            parsed = json.loads(selected_sources)
            if isinstance(parsed, list):
                sources = parsed
        except Exception:
            sources = None
    try:
        result = await process_bridge_visual_investigation(
            query=query.strip(),
            state_code=state_code,
            bridge_number=bridge_number,
            selected_sources=sources,
            defect_segmentation_mode=defect_segmentation_mode,
        )
        return clean_nan_values(result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/ask-cancel")
def ask_cancel():
    """Stop any in-flight Bridgette / per-bridge Ask generation (server-side)."""
    try:
        cancel_chat()
        return {"status": "success", "message": "Stop requested."}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"status": "error", "detail": str(exc)})


@app.get("/api/state-history/{state_code}")
def get_state_history(state_code: int):
    data = get_state_condition_history(USER_DB, state_code)
    return clean_nan_values(data)


@app.get("/api/bridge-history/{state_code}/{bridge_number}")
def get_bridge_history(state_code: int, bridge_number: str):
    """Get historical data for a specific bridge (currently returns current data, extend for multi-year)."""
    data = get_bridge_history_db(USER_DB, state_code, bridge_number)
    if data is None:
        raise HTTPException(status_code=404, detail="Bridge not found")
    return clean_nan_values(data)


@app.post("/api/forecasts/build-training-db")
def build_forecast_training_db():
    """Rebuild the separate forecast training database from loaded SNBI snapshots."""
    try:
        return clean_nan_values(rebuild_training_db(USER_DB))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/forecasts/train")
def train_forecasts(horizon_years: int = 10):
    """Train Prophet forecasts for bridge condition components."""
    try:
        if horizon_years < 1 or horizon_years > 50:
            raise HTTPException(status_code=400, detail="horizon_years must be between 1 and 50")
        result = train_condition_forecasts(horizon_years=horizon_years)
        if result.get("status") == "error":
            return JSONResponse(status_code=400, content=clean_nan_values(result))
        return clean_nan_values(result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/forecasts/train-bridge/{state_code}/{bridge_number}")
def train_bridge_forecasts(state_code: int, bridge_number: str, horizon_years: int = 10):
    """Train Prophet forecasts only for one bridge so future predictions appear quickly."""
    try:
        if horizon_years < 1 or horizon_years > 50:
            raise HTTPException(status_code=400, detail="horizon_years must be between 1 and 50")
        # Auto-build training rows for this bridge if missing, so the history
        # modal can generate a forecast on open without a manual "Build" step.
        ensure_training_data(state_code, bridge_number)
        result = train_condition_forecasts(
            horizon_years=horizon_years,
            state_code=state_code,
            bridge_number=bridge_number,
        )
        if result.get("status") == "error":
            return JSONResponse(status_code=400, content=clean_nan_values(result))
        return clean_nan_values(result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/lcca/bridge/{state_code}/{bridge_number}")
def bridge_lcca(
    state_code: int, bridge_number: str, horizon_years: int = 40,
    nhs: bool = None,
    replace_per_sqft: float = None,
    rehab_per_sqft: float = None,
    annual_maint_per_sqft: float = None,
    poor_penalty_per_sqft: float = None,
    discount_rate: float = None,
):
    """Forecast-driven life-cycle cost analysis for one bridge: do-nothing vs a
    condition-triggered rehab policy, using the Markov deterioration model. All
    cost assumptions are editable via the optional query params; `nhs` picks the
    FHWA NBI NJ replacement unit cost (NHS vs non-NHS) when no explicit
    `replace_per_sqft` is supplied."""
    try:
        result = compute_bridge_lcca(
            state_code, bridge_number, horizon_years=horizon_years,
            nhs=nhs,
            replace_per_sqft=replace_per_sqft,
            rehab_per_sqft=rehab_per_sqft,
            annual_maint_per_sqft=annual_maint_per_sqft,
            poor_penalty_per_sqft=poor_penalty_per_sqft,
            discount_rate=discount_rate,
        )
        if result.get("status") == "error":
            return JSONResponse(status_code=400, content=clean_nan_values(result))
        return clean_nan_values(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/forecasts/summary")
def forecast_summary():
    """Return forecast training/prediction summary and latest run."""
    try:
        return clean_nan_values(get_forecast_summary())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/forecasts/backtest")
def forecast_backtest(holdout_years: int = 1):
    """Rolling-origin backtest: how well the Markov engine predicted the
    held-out most-recent inspection year(s). MAE/bias/within-1 per component."""
    try:
        from .routers.services.forecasting import backtest_markov
    except ImportError:
        from routers.services.forecasting import backtest_markov
    try:
        return clean_nan_values(backtest_markov(holdout_years=holdout_years))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/forecasts/bridges")
def forecast_bridges(limit: int = 500):
    """Return bridges available in the forecast training database."""
    try:
        return clean_nan_values({"bridges": get_forecast_bridges(limit=limit)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/forecasts/bridge/{state_code}/{bridge_number}")
def bridge_forecast(state_code: int, bridge_number: str):
    """Return observed and forecasted condition components for one bridge."""
    try:
        return clean_nan_values(get_bridge_forecast(state_code, bridge_number))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/forecasts/bridge/{state_code}/{bridge_number}/simulate")
def simulate_bridge(state_code: int, bridge_number: str, maintenance_year: int, horizon_years: int = 10):
    """What-if: simulate a maintenance event in ``maintenance_year`` and return its
    effect on the rest of the forecast (a bump then continued degradation)."""
    try:
        if horizon_years < 1 or horizon_years > 50:
            raise HTTPException(status_code=400, detail="horizon_years must be between 1 and 50")
        ensure_training_data(state_code, bridge_number)
        result = simulate_bridge_forecast(
            state_code, bridge_number, maintenance_year, horizon_years=horizon_years
        )
        # The chosen year must fall inside the projected future window.
        future_years = [
            point["year"]
            for metric in result.get("metrics", {}).values()
            for point in metric.get("forecast", [])
        ]
        if future_years and not (min(future_years) <= maintenance_year <= max(future_years)):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"maintenance_year must be between {min(future_years)} and {max(future_years)} "
                    "(within the forecast horizon)."
                ),
            )
        return clean_nan_values(result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/system-info")
def system_info():
    """Return CPU, RAM, disk usage and database stats. GPU/VRAM telemetry was
    removed — this service runs no local inference (all AI uses hosted APIs)."""
    info: Dict = {}
    info["gpus"] = []  # no local GPU is used; kept for response-shape stability

    # ── CPU & RAM via psutil (guarded; falls back to /proc on Linux) ──
    try:
        import psutil
        info["cpu_percent"] = psutil.cpu_percent(interval=0.3)
        mem = psutil.virtual_memory()
        info["ram_total_gb"] = round(mem.total / (1024**3), 1)
        info["ram_used_gb"] = round(mem.used / (1024**3), 1)
        info["ram_percent"] = mem.percent
    except ImportError:
        # Fallback: read /proc on Linux when psutil is not installed.
        try:
            with open("/proc/meminfo") as f:
                mi = {}
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        mi[parts[0].rstrip(":")] = int(parts[1])
                total = mi.get("MemTotal", 0)
                avail = mi.get("MemAvailable", mi.get("MemFree", 0))
                info["ram_total_gb"] = round(total / (1024**2), 1)
                info["ram_used_gb"] = round((total - avail) / (1024**2), 1)
                info["ram_percent"] = round((total - avail) / total * 100, 1) if total else 0
        except Exception:
            pass
        info.setdefault("cpu_percent", None)
    except Exception as e:
        info["resource_error"] = str(e)[:300]

    # ── Database stats ──
    try:
        db_path = USER_DB
        if os.path.exists(db_path):
            db_size = os.path.getsize(db_path)
            info["db_path"] = db_path
            info["db_type"] = "SQLite"
            info["db_size_bytes"] = db_size
            info["db_size_mb"] = round(db_size / (1024**2), 2)

            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            tables = [r[0] for r in cursor.fetchall()]
            table_info = []
            for t in tables:
                cursor.execute(f'SELECT COUNT(*) FROM "{t}"')
                count = cursor.fetchone()[0]
                table_info.append({"name": t, "rows": count})
            info["tables"] = table_info
            info["table_count"] = len(tables)
            conn.close()

            # Disk usage
            disk = shutil.disk_usage(os.path.dirname(db_path) or ".")
            info["disk_total_gb"] = round(disk.total / (1024**3), 1)
            info["disk_used_gb"] = round(disk.used / (1024**3), 1)
            info["disk_free_gb"] = round(disk.free / (1024**3), 1)
    except Exception as e:
        info["db_error"] = str(e)

    return info


@app.get("/health")
def health():
    return {"status": "ok", "message": "SNBI Bridge API is running"}


@app.get("/ready")
def ready():
    """Readiness probe: storage, database, and pinned TEI backend are usable."""
    checks: Dict[str, bool] = {
        "data_directory": False,
        "database": False,
        "embeddings": False,
    }
    try:
        data_dir = Path(USER_DB).parent
        checks["data_directory"] = data_dir.is_dir() and os.access(data_dir, os.W_OK)
        with sqlite3.connect(f"file:{USER_DB}?mode=ro", uri=True, timeout=2) as conn:
            conn.execute("SELECT 1").fetchone()
        checks["database"] = True
    except Exception:
        pass
    try:
        try:
            from routers.services import llm_providers as _providers
        except ImportError:
            from .routers.services import llm_providers as _providers
        _providers.verify_embedding_backend(timeout=5)
        checks["embeddings"] = True
    except Exception:
        pass
    if not all(checks.values()):
        return JSONResponse(status_code=503, content={"status": "not_ready", "checks": checks})
    return {"status": "ready", "checks": checks}


# ====================== STATIC FRONTEND (single-server deploy) ======================
# One app serves BOTH the JSON API (/api/*, /health) and the built React SPA.
# The dist dir is resolved from FRONTEND_DIST, else the first existing of
# <repo>/backend/static then <repo>/frontend/dist. When present, hashed assets
# are mounted and every non-/api path falls back to index.html (client routing).
def _resolve_frontend_dist() -> Optional[Path]:
    candidates = []
    env_dir = os.getenv("FRONTEND_DIST", "").strip()
    if env_dir:
        candidates.append(Path(env_dir))
    # main.py = <repo>/backend/app/main.py -> parents[2] = <repo>
    repo_root = Path(__file__).resolve().parents[2]
    candidates.append(repo_root / "backend" / "static")
    candidates.append(repo_root / "frontend" / "dist")
    for c in candidates:
        try:
            if c.is_dir() and (c / "index.html").is_file():
                return c
        except Exception:
            continue
    return None


_FRONTEND_DIST = _resolve_frontend_dist()
if _FRONTEND_DIST is not None:
    _INDEX_HTML = _FRONTEND_DIST / "index.html"
    _assets_dir = _FRONTEND_DIST / "assets"
    if _assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        """Serve the SPA. Unknown /api/* paths return 404 JSON; any other path
        returns index.html so the React router handles it client-side."""
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        # Serve real files that live at the dist root (favicon, icons, etc.).
        if full_path:
            candidate = (_FRONTEND_DIST / full_path).resolve()
            try:
                if candidate.is_file() and str(candidate).startswith(str(_FRONTEND_DIST.resolve())):
                    return FileResponse(str(candidate))
            except Exception:
                pass
        return FileResponse(str(_INDEX_HTML))

    print(f"[startup] Serving frontend SPA from {_FRONTEND_DIST}")
else:
    print("[startup] No frontend build found (set FRONTEND_DIST or run build.ps1/build.sh); API-only mode.")


if __name__ == "__main__":
    host, port = select_backend_host_port()
    print(f"Starting SNBI Bridge API on http://{host}:{port}...")
    reload_enabled = os.getenv("SNBI_BACKEND_RELOAD", "0") == "1"
    uvicorn.run(app, host=host, port=port, reload=reload_enabled)
