import os
import re
import json
import asyncio
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd
from fastapi import UploadFile
from PIL import Image

from .database import get_connection, USER_DB
from . import llm_providers

# Hosted-API build: no local document AI. PDF text comes from pypdf only (no
# docling/PyTorch); image "OCR" is skipped (image understanding goes through the
# vision LLM via describe_image). There are NO top-level imports of docling,
# pytesseract, torch, or ultralytics anywhere in this module.
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None


SUPPORTED_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff'}
SUPPORTED_DOCUMENT_EXTENSIONS = {'.pdf'}
from .datapaths import DATA_DIR, TEMP_DIR  # centralized persistent data folder
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# Legacy model arguments are accepted for API compatibility, but the server's
# CEREBRAS_MODEL setting is authoritative for text and vision generation.
SQL_MODEL = os.getenv('BRIDGE_SQL_MODEL', '')        # '' = use the caller's (main) model
ANSWER_MODEL = os.getenv('BRIDGE_ANSWER_MODEL', '')  # '' = use the caller's (main) model


async def save_upload_to_temp(upload_file: UploadFile) -> Path:
    target_path = TEMP_DIR / Path(upload_file.filename).name
    contents = await upload_file.read()
    target_path.write_bytes(contents)
    return target_path


def extract_text_from_pdf(pdf_path: Path) -> str:
    """Extract text from a PDF using pypdf only. Returns '' if pypdf is
    unavailable or yields nothing (hosted build has no docling/OCR fallback)."""
    if PdfReader is None:
        return ""

    text_parts: List[str] = []
    try:
        reader = PdfReader(str(pdf_path))
        for page in reader.pages:
            page_text = page.extract_text() or ''
            text_parts.append(page_text)
    except Exception as exc:
        raise RuntimeError(f"Unable to read PDF file: {exc}")

    return '\n\n'.join(text_parts).strip()


def extract_text_from_image(image_path: Path) -> str:
    """No local OCR in the hosted build — image understanding goes through the
    vision LLM (describe_image). Return '' for the OCR-text slot."""
    return ''


def describe_image(image_path: Path) -> str:
    """Describe an image with the configured Cerebras vision endpoint."""
    _prompt = (
        "Describe this image in detail. Include what you see: objects, text, charts, "
        "diagrams, labels, numbers, structural elements, colors, and any relevant information. "
        "Be thorough and factual."
    )
    try:
        from .rag_engine import _resize_image_b64
        desc = llm_providers.generate(_prompt, images_b64=[_resize_image_b64(str(image_path))],
                                      timeout=180, num_predict=1024)
        if (desc or '').strip():
            return desc
        raise RuntimeError('empty response')
    except Exception as exc:
        try:
            with Image.open(image_path) as img:
                width, height = img.size
                return f"Image: {width}x{height} pixels. Hosted vision analysis failed: {exc}"
        except Exception:
            return f"Image uploaded but hosted vision analysis failed: {exc}"


def _query_image_with_vision_model(image_path: Path, question: str) -> Optional[str]:
    """Send an image and question to Cerebras. Return None on failure."""
    try:
        from .rag_engine import _resize_image_b64
        ans = llm_providers.generate(question, images_b64=[_resize_image_b64(str(image_path))],
                                     timeout=180, num_predict=1024)
        return ans if (ans or '').strip() else None
    except Exception as exc:
        print(f"[vision] Hosted direct image query failed: {exc}")
        return None


def extract_text_from_file(file_path: Path) -> Tuple[str, str]:
    suffix = file_path.suffix.lower()
    if suffix in SUPPORTED_DOCUMENT_EXTENSIONS:
        return extract_text_from_pdf(file_path), 'pdf'
    if suffix in SUPPORTED_IMAGE_EXTENSIONS:
        return extract_text_from_image(file_path), 'image'
    # Fallback: try PDF first, then treat as image (OCR is a no-op in this build)
    if PdfReader is not None:
        try:
            return extract_text_from_pdf(file_path), 'pdf'
        except Exception:
            pass
    try:
        return extract_text_from_image(file_path), 'image'
    except Exception:
        pass
    raise RuntimeError(f"Unsupported file type '{suffix}' and no extraction backend is available.")


def normalize_text(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip()


def tokenize(text: str) -> List[str]:
    return [token for token in re.findall(r"[a-zA-Z0-9]+", text.lower()) if token]


def split_text_into_chunks(text: str, chunk_size: int = 1000, overlap: int = 200) -> List[str]:
    words = text.split()
    if not words:
        return []

    chunks = []
    start = 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunk = ' '.join(words[start:end])
        chunks.append(chunk)
        if end == len(words):
            break
        start = max(end - overlap, 0)
    return chunks


def score_chunk(question: str, chunk: str) -> int:
    query_tokens = set(tokenize(question))
    chunk_tokens = set(tokenize(chunk))
    common = query_tokens.intersection(chunk_tokens)
    return len(common)


def get_relevant_chunks(question: str, chunks: List[str], max_chunks: int = 4) -> List[str]:
    scored = [(score_chunk(question, chunk), chunk) for chunk in chunks]
    scored.sort(key=lambda item: item[0], reverse=True)
    filtered = [chunk for score, chunk in scored if score > 0]
    if not filtered and chunks:
        filtered = chunks[:max_chunks]
    return filtered[:max_chunks]


# ── Chat/Ask cancellation ────────────────────────────────────────────────────
# A user "Stop" must actually halt the hosted generation, not just free the UI.
# We stream the provider's tokens and, when a stop is requested, close the
# generator mid-stream — which closes the underlying HTTP response and aborts
# the hosted generation. `begin_chat()` arms this for a request (so
# get_cerebras_answer streams + checks the flag); plain callers (e.g. Vision
# document processing) keep the simple blocking path.
# NOTE: a single global flag — fine for this single-user desktop app.
_chat_cancel_event = threading.Event()
_chat_active = threading.Event()


class ChatCancelled(Exception):
    """Raised when an in-flight chat/ask generation is stopped by the user."""


def begin_chat() -> None:
    """Arm cancellable streaming for the current chat/ask request."""
    _chat_cancel_event.clear()
    _chat_active.set()


def end_chat() -> None:
    """Disarm cancellable streaming once the request is done."""
    _chat_active.clear()
    _chat_cancel_event.clear()


def cancel_chat() -> None:
    """Signal that the in-flight chat/ask generation should stop."""
    _chat_cancel_event.set()


def _stream_answer_tokens(model: str, prompt: str, timeout: int = 300):
    """Yield answer tokens from Cerebras. The `model` argument is accepted for
    signature stability but the server chooses CEREBRAS_MODEL. Checks the cancel
    flag between chunks and aborts the
    in-flight generation on user Stop (ChatCancelled) by closing the stream."""
    gen = llm_providers.stream(prompt, num_predict=1024, timeout=timeout)
    try:
        for chunk in gen:
            if _chat_cancel_event.is_set():
                raise ChatCancelled("Generation stopped by user.")
            yield chunk
    finally:
        gen.close()  # closes the underlying HTTP response → aborts generation


async def _aiter_answer_tokens(model: str, prompt: str, timeout: int = 300):
    """Async wrapper over the blocking provider token stream
    (_stream_answer_tokens): runs the blocking reads on a worker thread and hands
    tokens to the event loop via a queue, so the server stays responsive (other
    requests load) AND tokens flush as they arrive. Re-raises ChatCancelled on
    user stop."""
    import threading
    loop = asyncio.get_running_loop()
    q: "asyncio.Queue" = asyncio.Queue()

    def _worker():
        try:
            for chunk in _stream_answer_tokens(model, prompt, timeout=timeout):
                loop.call_soon_threadsafe(q.put_nowait, ('t', chunk))
            loop.call_soon_threadsafe(q.put_nowait, ('done', None))
        except ChatCancelled:
            loop.call_soon_threadsafe(q.put_nowait, ('stopped', None))
        except Exception as exc:  # surface as an error event
            loop.call_soon_threadsafe(q.put_nowait, ('error', str(exc)))

    threading.Thread(target=_worker, daemon=True).start()
    while True:
        kind, val = await q.get()
        if kind == 't':
            yield val
        elif kind == 'done':
            return
        elif kind == 'stopped':
            raise ChatCancelled("Generation stopped by user.")
        else:  # 'error'
            raise RuntimeError(val or "streaming failed")


def get_cerebras_answer(prompt: str, model: str = '') -> str:
    """Generate with Cerebras, retrying once.

    When cancellation is armed, stream so a user Stop aborts the request.
    """
    last_exc = None
    for _attempt in range(2):
        try:
            if _chat_active.is_set():
                return ''.join(_stream_answer_tokens(model, prompt))
            return llm_providers.generate(prompt, num_predict=1024, timeout=300)
        except ChatCancelled:
            raise
        except Exception as exc:
            last_exc = exc
    raise RuntimeError(
        f"Cerebras request failed: {last_exc}. Check the network, configured model, "
        "and CEREBRAS_API_KEY."
    )


# ─── Bridgette: NJ bridge text-to-SQL agent ──────────────────────────────────
_BRIDGETTE_SCHEMA_CACHE: dict = {'text': None}

# NJ county FIPS (3-digit, odd) → name, mirrored from the map UI. Materialized as
# a `counties` lookup table so the SQL agent can return names, not numeric codes.
NJ_COUNTY_NAMES = {
    1: 'Atlantic', 3: 'Bergen', 5: 'Burlington', 7: 'Camden', 9: 'Cape May',
    11: 'Cumberland', 13: 'Essex', 15: 'Gloucester', 17: 'Hudson', 19: 'Hunterdon',
    21: 'Mercer', 23: 'Middlesex', 25: 'Monmouth', 27: 'Morris', 29: 'Ocean',
    31: 'Passaic', 33: 'Salem', 35: 'Somerset', 37: 'Sussex', 39: 'Union', 41: 'Warren',
}


def _current_date_context() -> str:
    now = datetime.now().astimezone()
    return (
        f"Current local date and time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
        f"Current year: {now.year}."
    )


def _ensure_counties_table(db_path: str) -> None:
    """Create + populate a NJ county_code → county_name lookup table so the SQL
    agent can JOIN it and answer with county names instead of numeric codes."""
    import sqlite3 as _sq
    conn = _sq.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS counties (state_code INTEGER, county_code INTEGER, "
            "county_name TEXT, PRIMARY KEY (state_code, county_code))"
        )
        rows = [(34, code, name) for code, name in NJ_COUNTY_NAMES.items()] + [(34, 0, 'Unknown')]
        conn.executemany(
            "INSERT OR REPLACE INTO counties (state_code, county_code, county_name) VALUES (?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _bridgette_schema(db_path: str) -> str:
    """Compact schema text for the main bridge tables (cached), with notes that
    make the agent year-aware and county-name-aware."""
    if _BRIDGETTE_SCHEMA_CACHE['text']:
        return _BRIDGETTE_SCHEMA_CACHE['text']
    _ensure_counties_table(db_path)
    conn = get_connection(db_path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
        focus = [t for t in tables if t in
                 ('bridges', 'features', 'routes', 'span_sets', 'elements', 'works', 'posting_statuses', 'counties')]
        focus = focus or tables[:6]
        parts = []
        for t in focus:
            cols = [row[1] for row in conn.execute(f"PRAGMA table_info({t})").fetchall()]
            parts.append(f"Table {t}({', '.join(cols)})")
        text = "\n".join(parts)
        text += (
            "\n\nNOTES:"
            "\n- The `bridges` table stores YEARLY SNAPSHOTS: one row per bridge per `data_year`. "
            "For simple current counts/lists/rankings, filter to the latest snapshot with "
            "`data_year = (SELECT MAX(data_year) FROM bridges)`. For questions about condition ratings or "
            "numbers over time, SELECT `data_year` and ORDER BY `data_year` (GROUP BY `data_year` for aggregates) "
            "so results are reported per year."
            "\n- IMPORTANT: because each bridge repeats once per year, COUNTING bridges must NOT double-count "
            "snapshots — use COUNT(DISTINCT bridge_number), or filter to a single `data_year` (e.g. the latest). "
            "A plain COUNT(*) over all years overcounts by ~18x."
            "\n- IMPORTANT: to LIST or RANK individual bridges (oldest/newest, longest, highest traffic, top-N), "
            "first restrict to ONE row per bridge — add `WHERE data_year = (SELECT MAX(data_year) FROM bridges)` "
            "(or GROUP BY bridge_number) BEFORE `ORDER BY ... LIMIT N`. Otherwise the query returns repeated yearly "
            "snapshots of the SAME bridge instead of N distinct bridges. Always SELECT `bridge_number` so rows are "
            "identifiable, and JOIN `counties` for the county name."
            "\n- `county_code` is a numeric code. To name counties, JOIN `counties` "
            "(ON bridges.state_code = counties.state_code AND bridges.county_code = counties.county_code) "
            "and SELECT `county_name` — answer with county NAMES, never the numeric code."
        )
    finally:
        conn.close()
    _BRIDGETTE_SCHEMA_CACHE['text'] = text
    return text


def _extract_sql(text: str) -> str:
    t = (text or '').strip()
    m = re.search(r"```(?:sql)?\s*(.*?)```", t, re.DOTALL | re.IGNORECASE)
    if m:
        t = m.group(1).strip()
    idx = t.upper().find('SELECT')
    if idx >= 0:
        t = t[idx:]
    return t.strip().rstrip(';').strip()


def _ensure_limit(sql: str, n: int = 500) -> str:
    """Append a row cap only if the query has no LIMIT already. Uses a word-boundary
    match so a LIMIT after a newline (e.g. multi-line SQL ending in `\\nLIMIT 1`) is
    detected — avoids producing invalid `... LIMIT 1 LIMIT 500`."""
    if re.search(r'\blimit\b', sql, re.IGNORECASE):
        return sql
    return f"{sql} LIMIT {n}"


def _preview_rows_for_prompt(rows: list, limit: int = 40) -> tuple[list, str]:
    """Keep prompt evidence compact without hiding newest rows at the tail."""
    if len(rows) <= limit:
        return rows, f"all {len(rows)} rows"
    head_count = max(1, limit // 2)
    tail_count = max(1, limit - head_count)
    omitted = len(rows) - head_count - tail_count
    preview = (
        rows[:head_count]
        + [{"_omitted_rows_between_head_and_tail": omitted}]
        + rows[-tail_count:]
    )
    return preview, f"first {head_count} and last {tail_count} of {len(rows)} rows"


def bridgette_answer(question: str, model: str = '', db_path: Optional[str] = None) -> dict:
    """Answer an NL question about NJ bridges: generate SQL → execute (read-only) → summarize."""
    from .database import execute_readonly_sql, USER_DB as _DB
    dbp = db_path or _DB
    schema = _bridgette_schema(dbp)

    begin_chat()  # arm cancellable streaming for this request
    try:
        sql_prompt = (
            "You are Bridgette, a data analyst for the New Jersey bridge database (all rows are state_code = 34).\n"
            f"{_current_date_context()}\n"
            "Write ONE read-only SQLite SELECT query that answers the question.\n"
            "Rules: SELECT only (never modify data); use only the tables/columns below; return ONLY the SQL. "
            "For simple current count/list/rank questions, filter to the latest snapshot with "
            "`data_year = (SELECT MAX(data_year) FROM bridges)`. Only GROUP BY `data_year` when the user asks "
            "for a trend, history, or values by year. Always alias aggregate columns with clear names. "
            "For age/current-date questions, calculate age against the current year/date above, not the latest data_year. "
            "For county questions JOIN `counties` and SELECT `county_name`, never the numeric code.\n\n"
            f"Schema:\n{schema}\n\nQuestion: {question}\nSQL:"
        )
        # Generate SQL with the fast small model; if it doesn't run, retry once
        # with the capable model. Fast on the happy path, correct on the rare miss.
        def _try_sql(m):
            s = _extract_sql(get_cerebras_answer(sql_prompt, model=m))
            if not s.upper().startswith('SELECT'):
                return s, None
            s = _ensure_limit(s, 500)
            try:
                return s, execute_readonly_sql(dbp, s)
            except Exception:
                return s, None

        sql_model = SQL_MODEL or model
        sql, result = _try_sql(sql_model)
        if result is None and sql_model != model:
            sql, result = _try_sql(model)  # small SQL model failed → retry with the capable model
        if result is None:
            return {"answer": "I couldn't form a query that runs for that question.", "sql": sql or '', "rows": [], "columns": [], "count": 0}

        rows = result.get('rows', [])
        cols = result.get('columns', [])
        preview_rows, preview_label = _preview_rows_for_prompt(rows, limit=40)
        preview = json.dumps(preview_rows, default=str)
        summary_prompt = (
            "You are Bridgette, a New Jersey bridge data assistant. Answer using ONLY these query results; "
            f"{_current_date_context()} "
            "do not invent numbers. The SQL WHERE clause is part of the evidence: if it filters on a field "
            "(for example `year_built < 1970`), the returned counts/rows already satisfy that filter even if that "
            "field is not selected as a result column. If the user asked a simple 'how many' question and results "
            "span multiple data_year values, answer with the latest data_year value unless the user explicitly "
            "asked for a trend/by-year list. Pay special attention to the last rows because ordered SQL often puts "
            "the newest years there. "
            "If asked for age or a current-time answer, compute it relative to the current date/time above. "
            "(a short per-year list is fine). Use county names, not numeric codes.\n\n"
            f"Question: {question}\nSQL used: {sql}\nColumns: {cols}\n"
            f"Results (JSON, {preview_label}): {preview}\nAnswer:"
        )
        try:
            answer = get_cerebras_answer(summary_prompt, model=ANSWER_MODEL or model).strip()
        except ChatCancelled:
            raise  # propagate user stop; do not fall back to a canned answer
        except Exception:
            answer = f"Returned {len(rows)} row(s)."
        return {"answer": answer, "sql": sql, "rows": rows, "columns": cols, "count": len(rows)}
    finally:
        end_chat()


def bridgette_answer_stream(question: str, model: str = '', db_path: Optional[str] = None):
    """Streaming Bridgette: same SQL→execute→summarize flow as bridgette_answer,
    but yields events so the UI shows the answer appear token-by-token:
      {'type':'meta', sql, rows, columns, count}  (once, before the answer)
      {'type':'token', text}                      (repeated, the streamed answer)
      {'type':'stopped'} | {'type':'done'}        (terminal)
    The SQL step isn't streamed (it's internal/fast); only the summary streams."""
    from .database import execute_readonly_sql, USER_DB as _DB
    dbp = db_path or _DB
    schema = _bridgette_schema(dbp)
    begin_chat()
    try:
        sql_prompt = (
            "You are Bridgette, a data analyst for the New Jersey bridge database (all rows are state_code = 34).\n"
            f"{_current_date_context()}\n"
            "Write ONE read-only SQLite SELECT query that answers the question.\n"
            "Rules: SELECT only (never modify data); use only the tables/columns below; return ONLY the SQL. "
            "For simple current count/list/rank questions, filter to the latest snapshot with "
            "`data_year = (SELECT MAX(data_year) FROM bridges)`. Only GROUP BY `data_year` when the user asks "
            "for a trend, history, or values by year. Always alias aggregate columns with clear names. "
            "For age/current-date questions, calculate age against the current year/date above, not the latest data_year. "
            "For county questions JOIN `counties` and SELECT `county_name`, never the numeric code.\n\n"
            f"Schema:\n{schema}\n\nQuestion: {question}\nSQL:"
        )

        def _try_sql(m):
            s = _extract_sql(get_cerebras_answer(sql_prompt, model=m))
            if not s.upper().startswith('SELECT'):
                return s, None
            s = _ensure_limit(s, 500)
            try:
                return s, execute_readonly_sql(dbp, s)
            except Exception:
                return s, None

        sql_model = SQL_MODEL or model
        sql, result = _try_sql(sql_model)
        if result is None and sql_model != model:
            sql, result = _try_sql(model)
        if result is None:
            yield {'type': 'meta', 'sql': sql or '', 'rows': [], 'columns': [], 'count': 0}
            yield {'type': 'token', 'text': "I couldn't form a query that runs for that question."}
            yield {'type': 'done'}
            return

        rows = result.get('rows', [])
        cols = result.get('columns', [])
        yield {'type': 'meta', 'sql': sql, 'rows': rows, 'columns': cols, 'count': len(rows)}

        preview_rows, preview_label = _preview_rows_for_prompt(rows, limit=40)
        preview = json.dumps(preview_rows, default=str)
        summary_prompt = (
            "You are Bridgette, a New Jersey bridge data assistant. Answer using ONLY these query results; "
            f"{_current_date_context()} "
            "do not invent numbers. The SQL WHERE clause is part of the evidence: if it filters on a field "
            "(for example `year_built < 1970`), the returned counts/rows already satisfy that filter even if that "
            "field is not selected as a result column. If the user asked a simple 'how many' question and results "
            "span multiple data_year values, answer with the latest data_year value unless the user explicitly "
            "asked for a trend/by-year list. Pay special attention to the last rows because ordered SQL often puts "
            "the newest years there. "
            "If asked for age or a current-time answer, compute it relative to the current date/time above. "
            "(a short per-year list is fine). Use county names, not numeric codes.\n\n"
            f"Question: {question}\nSQL used: {sql}\nColumns: {cols}\n"
            f"Results (JSON, {preview_label}): {preview}\nAnswer:"
        )
        try:
            for chunk in _stream_answer_tokens(ANSWER_MODEL or model, summary_prompt):
                yield {'type': 'token', 'text': chunk}
        except ChatCancelled:
            yield {'type': 'stopped'}
            return
        yield {'type': 'done'}
    finally:
        end_chat()


def bridge_sql_context(question: str, state_code, bridge_number: str, model: str = '') -> tuple:
    """Bridgette-style DB evidence for ONE bridge: generate a read-only SELECT
    scoped to this bridge, execute it, and return (compact_context_text, sql).

    This replaces the old flatten-every-row RAG context — the LLM gets exact query
    results instead of ~80k chars of dumped columns, so answers are accurate and
    the prompt stays small. Returns ('', '') if no usable SQL/results."""
    from .database import execute_readonly_sql, USER_DB as _DB
    schema = _bridgette_schema(_DB)
    sql_prompt = (
        "You are a data analyst for the New Jersey bridge database (state_code = 34).\n"
        f"{_current_date_context()}\n"
        f"Write ONE read-only SQLite SELECT that answers the question for the SINGLE bridge "
        f"with bridge_number = '{bridge_number}' (state_code = {int(state_code)}). Always filter to "
        "that bridge. SELECT only; use only the tables/columns below; return ONLY the SQL. "
        "Select ONLY the few columns needed to answer (do NOT use SELECT *) — keeps results small. "
        "For simple current count/list/rank questions, filter to the latest snapshot with "
        "`data_year = (SELECT MAX(data_year) FROM bridges)`. Only GROUP BY `data_year` when the user asks "
        "for a trend, history, or values by year. Always alias aggregate columns with clear names. "
        "For age/current-date questions, calculate age against the current year/date above, not the latest data_year. "
        "For county questions JOIN `counties` and SELECT `county_name`, never the numeric code.\n\n"
        f"Schema:\n{schema}\n\nQuestion: {question}\nSQL:"
    )
    # Fast small model first; fall back to the capable model if its SQL won't run.
    def _try_sql(m):
        s = _extract_sql(get_cerebras_answer(sql_prompt, model=m))
        if not s.upper().startswith('SELECT'):
            return s, None
        s = _ensure_limit(s, 200)
        try:
            return s, execute_readonly_sql(_DB, s)
        except Exception:
            return s, None

    sql_model = SQL_MODEL or model
    sql, result = _try_sql(sql_model)
    if result is None and sql_model != model:
        sql, result = _try_sql(model)  # small SQL model failed → retry with the capable model
    if result is None:
        return '', (sql or '')
    rows = result.get('rows', [])
    cols = result.get('columns', [])
    if not rows:
        return f"SQL used: {sql}\n(No matching rows.)", sql
    preview_rows, preview_label = _preview_rows_for_prompt(rows, limit=60)
    preview = json.dumps(preview_rows, default=str)
    ctx = (
        f"SQL used: {sql}\nColumns: {cols}\n"
        f"Results ({preview_label}; total {len(rows)} row(s)): {preview}"
    )
    return ctx[:6000], sql


def _safe_json_from_model_output(raw_text: str) -> dict:
    """Try to parse model JSON output. If invalid, preserve raw text in a safe wrapper."""
    text = (raw_text or '').strip()
    if not text:
        return {
            'answer': '',
            'extracted_fields': {},
            'confidence': 'low',
            'citations': [],
            'missing_fields': [],
            'raw_text': '',
        }

    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass

    # Try fenced code block recovery
    code_match = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", text, flags=re.IGNORECASE)
    if code_match:
        try:
            payload = json.loads(code_match.group(1))
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass

    return {
        'answer': text,
        'extracted_fields': {},
        'confidence': 'low',
        'citations': [],
        'missing_fields': [],
        'raw_text': text,
    }


def build_prompt(
    context: str,
    question: str,
    metadata: dict = None,
    strict_grounding: bool = False,
    output_format: str = 'text',
    target_fields: str = '',
) -> str:
    meta_line = ""
    if metadata:
        parts = []
        if metadata.get("total_pages"):
            parts.append(f"The document has {metadata['total_pages']} pages")
        if metadata.get("total_images"):
            parts.append(f"The document contains exactly {metadata['total_images']} images/figures")
        if metadata.get("total_tables"):
            parts.append(f"The database contains exactly {metadata['total_tables']} tables: {metadata.get('table_names', '')}")
        if metadata.get("table_details"):
            for td in metadata['table_details']:
                parts.append(f"Table '{td['name']}' has {td['columns']} columns and {td['rows']} rows")
        if parts:
            meta_line = f"\nIMPORTANT FACTS: {'. '.join(parts)}. Use these exact numbers when asked about counts.\n"
    strict_block = ""
    if strict_grounding:
        strict_block = (
            "\nSTRICT GROUNDING RULES:\n"
            "1) Answer only from Context/IMPORTANT FACTS.\n"
            "2) If a value is not present, write exactly: NOT_FOUND.\n"
            "3) Do not infer missing numbers, dates, or IDs.\n"
            "4) Prefer exact field/value pairs when available.\n"
        )

    format_block = ""
    if output_format == 'json':
        schema_hint = target_fields.strip() if target_fields.strip() else "BL01,BID01,BID02,BIE03,BC01,BC02,BC03,BC04,BC13,BH09,BCL01"
        format_block = (
            "\nOUTPUT FORMAT:\n"
            "Return ONLY valid JSON object with this shape and no extra text:\n"
            "{\n"
            "  \"answer\": \"string\",\n"
            "  \"extracted_fields\": {\"field\": \"value\"},\n"
            "  \"confidence\": \"high|medium|low\",\n"
            "  \"citations\": [\"short source snippet or table/row reference\"],\n"
            "  \"missing_fields\": [\"field names not found\"]\n"
            "}\n"
            f"Prioritize these fields when applicable: {schema_hint}.\n"
        )

    return (
        "You are BridgeGuard, a bridge infrastructure database assistant.\n"
        f"{_current_date_context()}\n"
        "Rules:\n"
        "1. Answer DIRECTLY and CONCISELY — no preamble, no 'To answer this question...', no thinking-out-loud.\n"
        "2. Use the IMPORTANT FACTS and Context below as your only source of truth.\n"
        "3. State facts directly: 'There is 1 state in the database: New Jersey (state_code = 34).' NOT 'Upon reviewing...'\n"
        "4. Do NOT explain your reasoning or mention column names unless specifically asked.\n"
        "5. When asked about counts, use the IMPORTANT FACTS — do not count manually.\n"
        "6. If information is genuinely not available, say so in one sentence.\n"
        "7. For age/current-date questions, compute relative to the current date/time above; do not use `data_year` as 'today'.\n"
        f"{meta_line}{strict_block}{format_block}\n"
        f"Context:\n{context}\n\nQuestion: {question}\nAnswer:"
    )


def flatten_db_row_text(table: str, row: dict) -> str:
    fields = []
    for key, value in row.items():
        if value is None or value == '':
            continue
        fields.append(f"{key}={value}")
    return f"Table: {table} | " + ", ".join(fields)


def get_db_context(question: str, state_code: Optional[int] = None, bridge_number: Optional[str] = None, max_rows_per_table: int = 30) -> tuple:
    """Returns (context_str, metadata_dict) for the database."""
    conn = get_connection(USER_DB)
    try:
        tables_df = pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';", conn)
        table_names = tables_df['name'].tolist()

        # ── Build a schema summary so the LLM always knows the full DB structure ──
        schema_lines = [f"DATABASE OVERVIEW: The database contains exactly {len(table_names)} tables: {', '.join(table_names)}."]
        table_details = []
        for table in table_names:
            columns_info = pd.read_sql_query(f"PRAGMA table_info({table})", conn)
            col_names = columns_info['name'].tolist()
            count_row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            row_count = count_row[0] if count_row else 0
            schema_lines.append(
                f"Table '{table}': {len(col_names)} columns ({', '.join(col_names[:15])}{'...' if len(col_names) > 15 else ''}), {row_count} rows."
            )
            table_details.append({'name': table, 'columns': len(col_names), 'rows': row_count})
        schema_summary = '\n'.join(schema_lines)

        # Metadata for IMPORTANT FACTS in prompt
        db_metadata = {
            'total_tables': len(table_names),
            'table_names': ', '.join(table_names),
            'table_details': table_details,
        }

        # ── Gather sample rows from each table ──
        rows = []
        for table in table_names:
            columns_df = pd.read_sql_query(f"PRAGMA table_info({table})", conn)
            col_names = columns_df['name'].tolist()
            query = f"SELECT * FROM {table}"
            filters = []
            params = []

            if state_code is not None and 'state_code' in col_names:
                filters.append('state_code = ?')
                params.append(state_code)
            if bridge_number and 'bridge_number' in col_names:
                filters.append('bridge_number = ?')
                params.append(bridge_number)
            if filters:
                query += ' WHERE ' + ' AND '.join(filters)
            query += ' LIMIT ?'
            params.append(max_rows_per_table)

            df = pd.read_sql_query(query, conn, params=params)
            if df.empty:
                continue

            df = df.where(pd.notna(df), None)
            for _, row in df.iterrows():
                rows.append(flatten_db_row_text(table, row.to_dict()))

        if not rows:
            # Still return schema summary even if no data rows match filters
            return schema_summary, db_metadata

        full_text = normalize_text('\n\n'.join(rows))
        # chunk_size is in WORDS — keep chunks small so the selected context stays
        # compact. Large chunks here previously produced ~80k chars of DB context,
        # which (combined with report excerpts) gave a ~28k-token prompt that made
        # the local model's prefill crawl whenever both DB + files were queried.
        chunks = split_text_into_chunks(full_text, chunk_size=160, overlap=40)
        relevant = get_relevant_chunks(question, chunks, max_chunks=6)
        # Prepend the schema summary so the LLM always has accurate structural info,
        # then hard-cap the total so a data-rich bridge can't bloat the prompt.
        context = schema_summary + '\n\n---\n\n' + '\n\n---\n\n'.join(relevant)
        return context[:12000], db_metadata
    finally:
        conn.close()


def process_db_rag(
    question: str,
    model: str = '',
    state_code: Optional[int] = None,
    bridge_number: Optional[str] = None,
    strict_grounding: bool = False,
    output_format: str = 'text',
    target_fields: str = '',
) -> dict:
    context, db_metadata = get_db_context(question, state_code=state_code, bridge_number=bridge_number)
    if not context:
        raise RuntimeError(
            'No matching database content was found for the requested scope. ' 
            'Please make sure the JSON data has been loaded, or adjust state and bridge filters.'
        )

    try:
        answer = get_cerebras_answer(
            build_prompt(
                context,
                question,
                metadata=db_metadata,
                strict_grounding=strict_grounding,
                output_format=output_format,
                target_fields=target_fields,
            ),
            model=model,
        )
        model_used = llm_providers.generation_model()
    except Exception as exc:
        raise RuntimeError(f"Cerebras is not available: {exc}. Check the network and CEREBRAS_API_KEY.")

    # Build short source labels from the table names in each chunk
    raw_chunks = context.split('\n\n---\n\n')
    seen_tables = []
    for chunk in raw_chunks:
        for part in chunk.split('Table: '):
            if '|' in part:
                tname = part.split('|')[0].strip()
                if tname and tname not in seen_tables:
                    seen_tables.append(tname)
    sources = [f"Table: {t}" for t in seen_tables] if seen_tables else ['Database']

    parsed_json = _safe_json_from_model_output(answer) if output_format == 'json' else None

    return {
        'status': 'success',
        'question': question,
        'answer': answer,
        'json_answer': parsed_json,
        'source_chunks': sources,
        'model_used': model_used,
        'source': 'database',
        'strict_grounding': strict_grounding,
        'output_format': output_format,
        'scope': {
            'state_code': state_code,
            'bridge_number': bridge_number,
        }
    }


# ─── Per-bridge files-RAG (retrieval over a bridge's uploaded report files) ──
_BRIDGE_REPORTS_CACHE: dict = {}
# Serialize ingestion per bridge: rapid multi-file uploads (each fires a
# background ingest) and a concurrent first "Ask" must not run overlapping
# ingests that race on the same on-disk cache file.
_BRIDGE_INGEST_LOCKS: dict = {}
_BRIDGE_INGEST_LOCKS_GUARD = threading.Lock()


def _bridge_ingest_lock(state_code, bridge_number) -> threading.Lock:
    k = (str(state_code), str(bridge_number))
    with _BRIDGE_INGEST_LOCKS_GUARD:
        lk = _BRIDGE_INGEST_LOCKS.get(k)
        if lk is None:
            lk = threading.Lock()
            _BRIDGE_INGEST_LOCKS[k] = lk
    return lk


def _bridge_cache_dir() -> Path:
    """Where per-bridge ingest results (chunks + embeddings) are persisted."""
    try:
        from .rag_engine import RAG_WORKING_DIR
        base = Path(RAG_WORKING_DIR)
    except Exception:
        base = Path(tempfile.gettempdir()) / 'BridgeGuard'
    d = base / 'bridge_reports'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _relative_data_path(value) -> str:
    """Persist cache asset paths relative to the portable data directory."""
    try:
        return Path(value).resolve().relative_to(Path(DATA_DIR).resolve()).as_posix()
    except (TypeError, ValueError):
        return str(value)


def _resolve_data_path(value) -> str:
    path = Path(str(value))
    return str(path if path.is_absolute() else Path(DATA_DIR) / path)


def _cache_paths_relative(value):
    if isinstance(value, list):
        return [_cache_paths_relative(item) for item in value]
    if isinstance(value, dict):
        return {
            key: (
                _relative_data_path(item)
                if key == "path" and item
                else _cache_paths_relative(item)
            )
            for key, item in value.items()
        }
    return value


def _cache_paths_resolved(value):
    if isinstance(value, list):
        return [_cache_paths_resolved(item) for item in value]
    if isinstance(value, dict):
        return {
            key: (
                _resolve_data_path(item)
                if key == "path" and item
                else _cache_paths_resolved(item)
            )
            for key, item in value.items()
        }
    return value


def _sig_str(sig) -> str:
    return '|'.join(f"{_relative_data_path(p)}:{m}" for p, m in sig)


def _bridge_cache_key(state_code, bridge_number, sig):
    profile = llm_providers.embedding_profile()
    return (
        str(state_code),
        str(bridge_number),
        sig,
        profile["provider"],
        profile["model"],
        profile["dimension"],
    )


def _existing_report_files(state_code, bridge_number) -> list:
    """Accept both portable DATA_DIR-relative and legacy absolute DB paths."""
    from .database import get_bridge_report_files

    files = []
    for item in get_bridge_report_files(state_code, bridge_number):
        raw = item.get("filepath")
        if not raw:
            continue
        path = Path(str(raw))
        resolved = path if path.is_absolute() else Path(DATA_DIR) / path
        if resolved.exists():
            copy = dict(item)
            copy["filepath"] = str(resolved)
            files.append(copy)
    return files


def _load_bridge_cache(state_code, bridge_number, sig):
    """Return a previously-ingested result from disk iff the file signature matches.

    This makes image description (the slow vision-model pass) a one-time cost that
    survives backend restarts — not recomputed on every launch / first Ask."""
    fp = _bridge_cache_dir() / f"{state_code}_{bridge_number}.json"
    if not fp.exists():
        return None
    try:
        import numpy as np
        data = json.loads(fp.read_text(encoding='utf-8'))
        if data.get('sig') != _sig_str(sig):
            return None
        chunks = _cache_paths_resolved(data.get('chunks') or [])
        # Cache schema upgrade: older caches stored image chunks without the source
        # image path, so the chat could not render a gallery. Force a re-ingest so
        # image chunks are rebuilt with their on-disk path.
        if any(c.get('kind') == 'image' and not c.get('path') for c in chunks):
            return None
        # Self-heal: if EVERY image description failed (e.g. the old palette-PNG
        # JPEG bug, or the vision model was down at ingest), rebuild so descriptions
        # — and therefore semantic image search — actually work.
        img_chunks = [c for c in chunks if c.get('kind') == 'image']
        if img_chunks and all(
            ('[vision description unavailable]' in (c.get('text') or '')
             or '[description failed]' in (c.get('text') or '')
             or '[no description]' in (c.get('text') or ''))
            for c in img_chunks
        ):
            return None
        emb = data.get('embeddings')
        needs_reembedding = bool(chunks)
        matrix = None
        if emb:
            try:
                candidate = np.asarray(emb, dtype=np.float32)
            except (TypeError, ValueError):
                candidate = None
            if (
                candidate is not None
                and candidate.ndim == 2
                and candidate.shape[0] == len(chunks)
                and candidate.shape[1] == llm_providers.embedding_profile()["dimension"]
                and np.isfinite(candidate).all()
                and llm_providers.embedding_profile_matches(data.get("embedding_profile"))
            ):
                matrix = candidate
                needs_reembedding = False
        return {
            'chunks': chunks,
            'embeddings': matrix,
            'embedding_profile': data.get("embedding_profile"),
            'needs_reembedding': needs_reembedding,
            'image_count': data.get('image_count', 0),
            'page_images': _cache_paths_resolved(data.get('page_images') or []),
        }
    except Exception:
        return None


def _save_bridge_cache(state_code, bridge_number, sig, result) -> None:
    fp = _bridge_cache_dir() / f"{state_code}_{bridge_number}.json"
    try:
        emb = result.get('embeddings')
        payload = {
            'sig': _sig_str(sig),
            'chunks': _cache_paths_relative(result.get('chunks') or []),
            'embeddings': emb.tolist() if emb is not None else None,
            'embedding_profile': (
                llm_providers.embedding_profile() if emb is not None else None
            ),
            'image_count': result.get('image_count', 0),
            'page_images': _cache_paths_relative(result.get('page_images') or []),
        }
        fp.write_text(json.dumps(payload), encoding='utf-8')
    except Exception as exc:
        print(f"[vision] failed to persist bridge cache: {exc}")


# Office formats are zip containers; embedded raster images live under */media/.
_OFFICE_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff'}


def _extract_office_images(filepath: str, out_dir: Path) -> List[dict]:
    """Pull embedded raster images out of a docx/xlsx/pptx (zip) package."""
    import zipfile
    out: List[dict] = []
    try:
        with zipfile.ZipFile(filepath) as z:
            names = sorted(n for n in z.namelist()
                           if '/media/' in n.lower() and Path(n).suffix.lower() in _OFFICE_IMAGE_EXTS)
            for i, n in enumerate(names):
                try:
                    data = z.read(n)
                    if len(data) < 4096:  # skip bullets/icons
                        continue
                    ext = Path(n).suffix.lower().lstrip('.')
                    op = out_dir / f"media_{i:03d}.{ext}"
                    op.write_bytes(data)
                    try:
                        with Image.open(op) as im:
                            w, h = im.size
                        if w < 80 or h < 80:
                            op.unlink(missing_ok=True)
                            continue
                    except Exception:
                        op.unlink(missing_ok=True)
                        continue
                    out.append({'path': str(op), 'page': None, 'caption': None,
                                'photo_number': None, 'ext': ext})
                except Exception:
                    continue
    except Exception as exc:
        print(f"[vision] office image extraction failed for {filepath}: {exc}")
    return out


def _extract_file_assets(filepath: str, doc_hash: str):
    """Return (text, images, page_images) for ANY supported document type.

      text         plain text (PDF via pypdf; Office best-effort)
      images       [{path, page, caption, photo_number, ext}] embedded figures/photos
      page_images  [{path, page}] full-page renders (PDF only)
    """
    from .rag_engine import (
        _extract_text_and_images, _render_pdf_pages, RAG_WORKING_DIR,
    )
    suffix = Path(filepath).suffix.lower()
    images_dir = Path(RAG_WORKING_DIR) / 'extracted_images' / doc_hash[:16]
    images_dir.mkdir(parents=True, exist_ok=True)

    text, images, page_images = '', [], []

    if suffix == '.pdf':
        try:
            pages, images = _extract_text_and_images(filepath, doc_hash)
            text = '\n\n'.join(p.get('text', '') for p in pages
                               if not str(p.get('text', '')).startswith('[Page'))
        except Exception as exc:
            print(f"[vision] pdf extraction failed for {filepath}: {exc}")
        try:
            for idx, pp in enumerate(_render_pdf_pages(filepath, doc_hash)):
                page_images.append({'path': pp, 'page': idx + 1})
        except Exception as exc:
            print(f"[vision] page render failed for {filepath}: {exc}")

    elif suffix in SUPPORTED_IMAGE_EXTENSIONS:
        images = [{'path': str(filepath), 'page': None, 'caption': None,
                   'photo_number': None, 'ext': suffix.lstrip('.')}]
        try:
            text = extract_text_from_image(Path(filepath))
        except Exception:
            text = ''

    else:
        # Word / Excel / PowerPoint: no docling in the hosted build. Text
        # extraction is best-effort (pypdf/none); embedded images still come from
        # the Office zip package and are described by the vision LLM.
        try:
            text, _ = extract_text_from_file(Path(filepath))
        except Exception:
            text = ''
        if suffix in {'.docx', '.xlsx', '.xlsm', '.pptx'}:
            images = _extract_office_images(filepath, images_dir)

    return text, images, page_images


def ingest_bridge_reports(state_code, bridge_number, report_progress: bool = False) -> dict:
    """Index a bridge's report files (cached by file-set signature). Serialized
    per bridge so concurrent uploads / first-Ask never run overlapping ingests."""
    with _bridge_ingest_lock(state_code, bridge_number):
        return _ingest_bridge_reports_impl(state_code, bridge_number, report_progress)


def bridge_reports_index_status(state_code, bridge_number) -> dict:
    """Report whether a bridge's CURRENT report file-set is already
    parsed+embedded (cached), WITHOUT triggering a parse. Lets the UI show a
    'Ready' vs 'Indexing…' badge so the user knows a file is queryable before
    asking. Returns {file_count, ready, indexing}."""
    files = _existing_report_files(state_code, bridge_number)
    if not files:
        return {'file_count': 0, 'ready': True, 'indexing': False}
    sig = tuple(sorted((f['filepath'], int(os.path.getmtime(f['filepath']))) for f in files))
    key = _bridge_cache_key(state_code, bridge_number, sig)
    if key in _BRIDGE_REPORTS_CACHE:
        return {'file_count': len(files), 'ready': True, 'indexing': False}
    disk = _load_bridge_cache(state_code, bridge_number, sig)
    if disk is not None and not disk.get('needs_reembedding'):
        _BRIDGE_REPORTS_CACHE[key] = disk  # warm memory cache for the upcoming Ask
        return {'file_count': len(files), 'ready': True, 'indexing': False}
    return {'file_count': len(files), 'ready': False, 'indexing': True}


def _ingest_bridge_reports_impl(state_code, bridge_number, report_progress: bool = False) -> dict:
    """Extract → describe images → chunk → embed a bridge's uploaded report files.

    For PDFs this indexes BOTH:
      • text — via pypdf (PyMuPDF supplies the page text during extraction), and
      • embedded images/photos — via PyMuPDF: each figure/inspection photo is pulled
        out (with its caption/photo number), described by the vision model, and the
        description is embedded as a searchable chunk. That is what lets the agent
        answer questions about pictures. Cached by file mtimes.

    When report_progress is True, stages are published via rag_engine's progress
    machinery (polled by the frontend through /api/vision-progress)."""
    from .rag_engine import (
        _split_page_chunks, _compute_embeddings, _resize_image_b64, _file_hash,
    )

    def _prog(stage, step, total):
        if report_progress:
            try:
                from .rag_engine import _update_progress
                _update_progress(stage, step, total)
            except Exception:
                pass

    files = _existing_report_files(state_code, bridge_number)
    if not files:
        _prog('Ready', 1, 1)
        return {'chunks': [], 'embeddings': None}

    sig = tuple(sorted((f['filepath'], int(os.path.getmtime(f['filepath']))) for f in files))
    key = _bridge_cache_key(state_code, bridge_number, sig)
    if key in _BRIDGE_REPORTS_CACHE:
        _prog('Ready', 1, 1)
        return _BRIDGE_REPORTS_CACHE[key]

    # Survives restarts: if a prior ingest for this exact file set is on disk, reuse it.
    disk = _load_bridge_cache(state_code, bridge_number, sig)
    if disk is not None:
        if disk.get('needs_reembedding'):
            _prog('Updating report embeddings', 0, 1)
            disk['embeddings'] = _compute_embeddings(disk.get('chunks') or [])
            disk['embedding_profile'] = (
                llm_providers.embedding_profile()
                if disk.get('embeddings') is not None else None
            )
            disk['needs_reembedding'] = disk.get('embeddings') is None
            # Replace the disk cache only after a complete, validated matrix was
            # produced. On provider outage retain the old profile for rollback
            # and serve this request via lexical retrieval.
            if not disk['needs_reembedding']:
                _save_bridge_cache(state_code, bridge_number, sig, disk)
        if not disk.get('needs_reembedding'):
            _BRIDGE_REPORTS_CACHE[key] = disk
        _prog('Ready', 1, 1)
        return disk

    # ── Phase 1: extract text and images for EVERY file (any format) ──
    # PDF → PyMuPDF text+images+page renders; Office → best-effort text + zip-media
    # images; standalone images → vision LLM. This is what lets the agent answer
    # about pictures in Word/Excel/PDF alike.
    per_file = []  # [(file_meta, text, images, page_images), ...]
    for f in files:
        try:
            dh = _file_hash(f['filepath'])
            text, images, page_imgs = _extract_file_assets(f['filepath'], dh)
        except Exception as exc:
            print(f"[vision] asset extraction failed for {f['filename']}: {exc}")
            text, images, page_imgs = '', [], []
        per_file.append((f, text, images, page_imgs))

    n_images = sum(len(im) for _, _, im, _ in per_file)
    total = len(files) + n_images + 1  # text per file + describe each image + embed
    step = 0
    chunks: list = []
    all_page_images: list = []

    # ── Phase 2: build text chunks, then describe & index each image ──
    for f, text, images, page_imgs in per_file:
        step += 1
        _prog(f"Extracting text ({step}/{len(files)})", step, total)
        if (text or '').strip():
            for ch in _split_page_chunks([{'page': 1, 'text': text}]):
                chunks.append({'text': ch['text'], 'page': ch.get('page', 1),
                               'source': f['filename'], 'kind': 'text'})
        for pi in page_imgs:
            all_page_images.append({'path': pi.get('path'), 'page': pi.get('page'),
                                    'source': f['filename']})

        for i, img in enumerate(images):
            step += 1
            _prog(f"Describing image {i + 1}/{len(images)} — {f['filename']}", step, total)
            desc = ''
            try:
                b64 = _resize_image_b64(img['path'])
                desc_prompt = (
                    "Describe this image from a bridge inspection / technical report in detail. "
                    "Include any visible labels, measurements, conditions, defects, materials, "
                    "and structural elements."
                )
                desc = llm_providers.generate(desc_prompt, images_b64=[b64],
                                              timeout=300, num_predict=512) or ''
            except Exception as exc:
                print(f"[vision] describe image failed ({f['filename']} p{img.get('page')}): {exc}")
            page = img.get('page')
            label_parts = [f"Image on page {page}" if page else f"Image from {f['filename']}"]
            if img.get('photo_number') is not None:
                label_parts.append(f"Photo {img['photo_number']}")
            if img.get('caption'):
                label_parts.append(f"Caption: {img['caption']}")
            body = desc.strip() or "[vision description unavailable]"
            chunks.append({'text': f"{' | '.join(label_parts)} — Description: {body}",
                           'page': page or 1, 'source': f['filename'], 'kind': 'image',
                           'path': img.get('path'), 'ext': img.get('ext', 'png'),
                           'photo_number': img.get('photo_number'),
                           'caption': img.get('caption')})

    _prog('Embedding & building index', total - 1, total)
    new_embeddings = _compute_embeddings(chunks) if chunks else None
    result = {'chunks': chunks, 'embeddings': new_embeddings,
              'embedding_profile': llm_providers.embedding_profile() if new_embeddings is not None else None,
              'image_count': n_images, 'page_images': all_page_images}
    _BRIDGE_REPORTS_CACHE[key] = result
    _save_bridge_cache(state_code, bridge_number, sig, result)
    _prog('Ready', total, total)
    return result


def process_db_rag_with_files(
    question: str,
    model: str = '',
    state_code: Optional[int] = None,
    bridge_number: Optional[str] = None,
    strict_grounding: bool = False,
    output_format: str = 'text',
    target_fields: str = '',
    selected_sources: Optional[list] = None,
) -> dict:
    """Per-bridge Ask: answer from the DB row AND the bridge's uploaded report files.

    selected_sources (optional): a list of source names to restrict to — 'Database'
    and/or specific report filenames. None means use everything.
    """
    from .rag_engine import _get_relevant
    import base64 as _b64
    from pathlib import Path as _Path

    use_db = selected_sources is None or 'Database' in selected_sources
    db_context, db_metadata = '', {}
    if use_db:
        try:
            db_context, db_metadata = get_db_context(question, state_code=state_code, bridge_number=bridge_number)
        except Exception:
            db_context, db_metadata = '', {}

    files_context = ''
    report_sources: list = []
    return_images: list = []
    if state_code is not None and bridge_number is not None:
        try:
            ing = ingest_bridge_reports(state_code, bridge_number)
            all_chunks = ing.get('chunks') or []
            embeddings = ing.get('embeddings')
            # embeddings are aligned (same order/length) to the full chunk list, so we
            # rank over subsets by slicing the matching embedding rows.
            aligned = embeddings is not None and len(embeddings) == len(all_chunks)
            keep = set(selected_sources) if selected_sources is not None else None

            def _src_ok(c):
                return keep is None or c.get('source') in keep

            def _rank(is_image: bool, k: int):
                idxs = [i for i, c in enumerate(all_chunks)
                        if _src_ok(c) and (c.get('kind') == 'image') == is_image]
                if not idxs:
                    return []
                sub = [all_chunks[i] for i in idxs]
                emb = embeddings[idxs] if aligned else None
                return _get_relevant(question, sub, k=min(k, len(sub)), embeddings=emb)

            # Separate budgets so image-heavy reports always surface their figures
            # instead of being crowded out by a single shared top-k.
            text_ranked = _rank(is_image=False, k=6)
            image_ranked = _rank(is_image=True, k=8)

            parts = []
            for ch in text_ranked:
                src = ch.get('source', 'report')
                if src not in report_sources:
                    report_sources.append(src)
                parts.append(f"[Inspection report: {src}]\n{ch['text']}")

            for ch in image_ranked:
                src = ch.get('source', 'report')
                if src not in report_sources:
                    report_sources.append(src)
                parts.append(f"[Inspection report image: {src}]\n{ch['text']}")
                p = _Path(ch.get('path') or '')
                if not p.exists():
                    continue
                try:
                    raw = p.read_bytes()
                    ext = (ch.get('ext') or 'png').lower()
                    mime = f"image/{ext}" if ext != 'jpg' else 'image/jpeg'
                    label_bits = []
                    if ch.get('photo_number') is not None:
                        label_bits.append(f"Photo {ch['photo_number']}")
                    if ch.get('page'):
                        label_bits.append(f"page {ch['page']}")
                    label = ' | '.join(label_bits) or src
                    return_images.append({
                        'filename': p.name, 'mime': mime,
                        'data': _b64.b64encode(raw).decode('utf-8'),
                        'label': f"{label} — {src}",
                    })
                except Exception:
                    pass
            files_context = '\n\n---\n\n'.join(parts)
        except Exception as exc:
            print(f"[files-rag] {exc}")

    if not db_context and not files_context:
        raise RuntimeError('No database row or uploaded files were found for this bridge to answer from.')

    combined = db_context or ''
    if files_context:
        combined += '\n\n' + ('=' * 40) + '\nINSPECTION REPORT EXCERPTS:\n' + files_context

    answer = get_cerebras_answer(
        build_prompt(combined, question, metadata=db_metadata, strict_grounding=strict_grounding,
                     output_format=output_format, target_fields=target_fields),
        model=model,
    )
    parsed_json = _safe_json_from_model_output(answer) if output_format == 'json' else None
    sources = (['Database'] if db_context else []) + [f"Report: {s}" for s in report_sources]
    return {
        'status': 'success', 'question': question, 'answer': answer, 'json_answer': parsed_json,
        'source_chunks': sources or ['Database'], 'model_used': llm_providers.generation_model(),
        'source': 'db+files' if files_context else 'database',
        'strict_grounding': strict_grounding, 'output_format': output_format,
        'reports_used': report_sources,
        'images': return_images,
        'scope': {'state_code': state_code, 'bridge_number': bridge_number},
    }


async def process_bridge_ask(
    question: str,
    model: str = '',
    state_code: Optional[int] = None,
    bridge_number: Optional[str] = None,
    strict_grounding: bool = False,
    output_format: str = 'text',
    target_fields: str = '',
    selected_sources: Optional[list] = None,
    force_defect_segmentation: bool = False,
    defect_segmentation_mode: str = 'balanced',
    defer_answer: bool = False,
) -> dict:
    """Per-bridge multimodal Ask — the full Vision-style pipeline over ALL of a
    bridge's report files (any format), aggregated, blended with the bridge's
    database record for text questions.

    Routes: show page N · show photo N · show/list all images · list pages ·
    count images · visual/defect analysis · text Q&A (reports + DB record)."""
    import base64 as _b64
    from .rag_engine import (
        _get_relevant, classify_query,
        detect_page_request, detect_photo_request,
        detect_list_images_request, detect_list_pages_request,
        detect_count_images_request,
    )

    use_db = selected_sources is None or 'Database' in selected_sources
    # Files are involved unless the user picked ONLY the Database. DB-only skips
    # report ingestion entirely (lean SQL→answer path) — faster, and avoids the
    # parse/embed work. Ingestion runs OFF the event loop so the app stays responsive.
    files_selected = selected_sources is None or any(s != 'Database' for s in (selected_sources or []))

    ing = {'chunks': [], 'embeddings': None, 'image_count': 0, 'page_images': []}
    if files_selected and state_code is not None and bridge_number is not None:
        try:
            ing = await asyncio.to_thread(ingest_bridge_reports, state_code, bridge_number) or ing
        except Exception as exc:
            print(f"[bridge-ask] ingest failed: {exc}")

    all_chunks = ing.get('chunks') or []
    embeddings = ing.get('embeddings')
    page_images = ing.get('page_images') or []
    aligned = embeddings is not None and len(embeddings) == len(all_chunks)

    def _src_ok(name):
        return selected_sources is None or name in selected_sources

    img_idx = [i for i, c in enumerate(all_chunks)
               if c.get('kind') == 'image' and _src_ok(c.get('source'))]
    txt_idx = [i for i, c in enumerate(all_chunks)
               if c.get('kind') != 'image' and _src_ok(c.get('source'))]
    img_chunks = [all_chunks[i] for i in img_idx]
    sel_pages = [p for p in page_images if _src_ok(p.get('source'))]

    reports_used: list = []
    for c in all_chunks:
        s = c.get('source')
        if s and _src_ok(s) and s not in reports_used:
            reports_used.append(s)

    def _label(c):
        cap = re.sub(r'^Description:\s*', '', (c.get('caption') or '').strip(), flags=re.IGNORECASE)
        lbl = cap[:100] if cap else 'Image'
        if c.get('photo_number') is not None:
            lbl = f"Photo {c['photo_number']}: {lbl}" if cap else f"Photo {c['photo_number']}"
        if c.get('page'):
            lbl += f" (p{c['page']})"
        return f"{lbl} — {c.get('source')}"

    def _encode(path, ext='png', label=None):
        if not path:
            return None
        p = Path(path)
        if not p.exists():
            return None
        try:
            raw = p.read_bytes()
            e = (ext or 'png').lower()
            mime = f"image/{e}" if e != 'jpg' else 'image/jpeg'
            return {'filename': p.name, 'mime': mime,
                    'data': _b64.b64encode(raw).decode('utf-8'), 'label': label or p.name}
        except Exception:
            return None

    def _rank(idxs, k):
        if not idxs:
            return []
        sub = [all_chunks[i] for i in idxs]
        emb = embeddings[idxs] if aligned else None
        return _get_relevant(question, sub, k=min(k, len(sub)), embeddings=emb)

    def _resp(answer, images=None, sources=None, vision=False):
        parsed = _safe_json_from_model_output(answer) if output_format == 'json' else None
        return {
            'status': 'success', 'question': question, 'answer': answer,
            'json_answer': parsed,
            'source_chunks': sources or (reports_used or ['Database']),
            'model_used': llm_providers.generation_model(),
            'document_type': 'multimodal',
            'images': images or [],
            'reports_used': reports_used,
            'indexed': bool(all_chunks),
            'pages': len(sel_pages),
            'images_count': len(img_chunks),
            'strict_grounding': strict_grounding,
            'output_format': output_format,
            'scope': {'state_code': state_code, 'bridge_number': bridge_number},
        }

    route = classify_query(question, has_images=bool(img_chunks))

    # ── show page N (PDF page renders) ──
    pg = detect_page_request(question)
    if pg is not None:
        imgs = [im for im in (_encode(p['path'], 'png', f"Page {pg} — {p.get('source')}")
                              for p in sel_pages if p.get('page') == pg) if im]
        if imgs:
            return _resp(f"Here is page {pg} from this bridge's report(s).", images=imgs, sources=[f"Page {pg}"])
        return _resp(f"Page {pg} was not found (page renders are available for PDF reports).")

    # ── show photo N ──
    ph = detect_photo_request(question)
    if ph is not None:
        match = [c for c in img_chunks if c.get('photo_number') == ph]
        if not match and 1 <= ph <= len(img_chunks):
            match = [img_chunks[ph - 1]]
        imgs = [im for im in (_encode(c.get('path'), c.get('ext'), _label(c)) for c in match) if im]
        if imgs:
            return _resp(f"Here is photo {ph}.", images=imgs, sources=[f"Photo {ph}"], vision=True)
        return _resp(f"Photo {ph} was not found in this bridge's reports.")

    # ── list all pages ──
    if detect_list_pages_request(question):
        imgs = [im for im in (_encode(p['path'], 'png', f"Page {p.get('page')} — {p.get('source')}")
                              for p in sel_pages) if im]
        if imgs:
            return _resp(f"This bridge's PDF report(s) have {len(imgs)} page(s).", images=imgs, sources=['All pages'])
        return _resp("No page renders are available (only PDF reports are rendered to pages).")

    # ── show / list all images ──
    if route == 'list_images' or detect_list_images_request(question):
        imgs = [im for im in (_encode(c.get('path'), c.get('ext'), _label(c)) for c in img_chunks) if im]
        if imgs:
            return _resp(f"Found {len(imgs)} image(s) across this bridge's report(s).", images=imgs, sources=['Extracted images'])
        return _resp("No embedded images were found in this bridge's reports.")

    # ── count images ──
    if detect_count_images_request(question):
        return _resp(f"Detected {len(img_chunks)} embedded image(s) across {len(reports_used)} report file(s).", vision=True)

    # ── visual / defect analysis ──
    # Deliberately absent from normal Ask. Raw-image analysis and defect
    # detection are available only through /api/bridge-visual-investigation,
    # which the bridge UI calls only when its visual-investigation checkbox is
    # checked. This keeps visual mode request-scoped instead of inferring it
    # from the wording of a question.

    # ── text (report text/tables + DB record) ──
    # The report retrieval (embeddings) and the DB SQL-gen (an LLM call) are
    # INDEPENDENT, so when both sources are selected we run them CONCURRENTLY —
    # the file ranking overlaps the SQL generation instead of waiting for it. Both
    # run off the event loop so /api/ask-cancel stays responsive.
    def _build_files_context():
        # Text + TABLES are the only normal-Ask report sources. Image-description
        # chunks are intentionally excluded: checking visual investigation is the
        # sole action that authorizes a raw-image/VLM inspection.
        parts = []
        for c in _rank(txt_idx, k=6):
            parts.append(f"[Report text/table: {c.get('source')}]\n{c['text'][:2200]}")
        return '\n\n---\n\n'.join(parts)

    async def _build_db_context():
        # Bridgette-style: write + run SQL scoped to this bridge; fall back to the
        # compact flatten-context only if SQL produced nothing usable.
        if not (use_db and state_code is not None and bridge_number is not None):
            return '', '', {}
        try:
            ctx, sql = await asyncio.to_thread(bridge_sql_context, question, state_code, bridge_number, model)
        except Exception:
            ctx, sql = '', ''
        meta = {}
        if not ctx:
            try:
                ctx, meta = await asyncio.to_thread(get_db_context, question, state_code, bridge_number)
            except Exception:
                ctx, meta = '', {}
        return ctx, sql, meta

    files_context, (db_context, db_sql, db_metadata) = await asyncio.gather(
        asyncio.to_thread(_build_files_context),
        _build_db_context(),
    )

    combined = db_context or ''
    if files_context:
        combined += '\n\n' + ('=' * 40) + '\nINSPECTION REPORT EXCERPTS:\n' + files_context
    if not combined.strip():
        raise RuntimeError('No database record or report content was found for this bridge to answer from.')

    prompt = build_prompt(combined, question, metadata=db_metadata, strict_grounding=strict_grounding,
                          output_format=output_format, target_fields=target_fields)
    sources = (['Database'] if db_context else []) + [f"Report: {s}" for s in reports_used]

    # Streaming caller: hand back the prompt instead of generating, so it can
    # stream the answer token-by-token (and still emit these sources + images).
    if defer_answer:
        return {'_defer': True, 'prompt': prompt, 'answer_model': ANSWER_MODEL or model,
                'sources': sources or ['Database'], 'reports_used': reports_used,
                'images': [], 'output_format': output_format}

    # Run the (blocking, streaming) generation off the event loop so a parallel
    # /api/ask-cancel request can be served and actually stop it mid-generation.
    answer = await asyncio.to_thread(get_cerebras_answer, prompt, ANSWER_MODEL or model)
    return _resp(answer, images=[], sources=sources or ['Database'])


async def process_bridge_visual_investigation(
    query: str,
    state_code: Optional[int] = None,
    bridge_number: Optional[str] = None,
    selected_sources: Optional[list] = None,
    defect_segmentation_mode: str = 'fast',
) -> dict:
    """Run on-demand bbox detection over report images for one bridge.

    The UI exposes this as one checkbox. Internally, image-description
    embeddings choose likely candidates first; when that signal is weak or
    unavailable, the route falls back to scanning the available report images.
    """
    from .rag_engine import (
        _get_relevant,
        _embed,
        _cosine_similarity,
        _detect_defects_with_bboxes,
        _refine_defect_detections_hybrid,
        _annotate_image_with_detections,
    )

    if state_code is None or bridge_number is None:
        raise RuntimeError('A bridge is required for visual investigation.')

    ing = await asyncio.to_thread(ingest_bridge_reports, state_code, bridge_number)
    all_chunks = ing.get('chunks') or []
    embeddings = ing.get('embeddings')
    aligned = embeddings is not None and len(embeddings) == len(all_chunks)
    keep = set(selected_sources) if selected_sources is not None else None

    def _src_ok(name):
        return keep is None or name in keep

    img_idx = [i for i, c in enumerate(all_chunks)
               if c.get('kind') == 'image' and _src_ok(c.get('source'))]
    if not img_idx:
        return {
            'status': 'success',
            'query': query,
            'answer': 'No extracted report images are available for this bridge.',
            'images': [],
            'detections': [],
            'checked_images': 0,
            'candidate_strategy': 'none',
        }

    sub = [all_chunks[i] for i in img_idx]
    emb = embeddings[img_idx] if aligned else None
    candidate_limit = int(os.getenv('VISUAL_INVESTIGATION_CANDIDATES', '8') or 8)
    candidate_limit = max(1, min(candidate_limit, len(sub)))
    fallback_limit = int(os.getenv('VISUAL_INVESTIGATION_FALLBACK_MAX', '0') or 0)
    if fallback_limit > 0:
        fallback_limit = max(1, min(fallback_limit, len(sub)))

    ranked = []
    candidate_strategy = 'fallback_all'
    if aligned and emb is not None:
        try:
            q_emb = _embed([query])
            sims = _cosine_similarity(q_emb[0], emb) if q_emb is not None else None
            if sims is not None:
                floor = float(os.getenv('VISUAL_INVESTIGATION_MIN_SIM', '0.32'))
                order = sims.argsort()[::-1]
                strong = [int(i) for i in order if float(sims[int(i)]) >= floor]
                if strong:
                    ranked = [sub[i] for i in strong[:candidate_limit]]
                    candidate_strategy = 'embedding_candidates'
                else:
                    ranked = [sub[int(i)] for i in order]
                    candidate_strategy = 'embedding_weak_fallback_all'
            else:
                ranked = _get_relevant(query, sub, k=len(sub), embeddings=emb)
                candidate_strategy = 'embedding_unavailable_fallback_all'
        except Exception:
            ranked = _get_relevant(query, sub, k=len(sub), embeddings=emb)
            candidate_strategy = 'embedding_error_fallback_all'
    else:
        ranked = _get_relevant(query, sub, k=len(sub), embeddings=emb)

    if not ranked:
        ranked = sub
        candidate_strategy = 'fallback_all'

    if candidate_strategy != 'embedding_candidates' and fallback_limit > 0:
        ranked = ranked[:fallback_limit]

    def _label(c):
        cap = re.sub(r'^Description:\s*', '', (c.get('caption') or '').strip(), flags=re.IGNORECASE)
        lbl = cap[:100] if cap else 'Image'
        if c.get('photo_number') is not None:
            lbl = f"Photo {c['photo_number']}: {lbl}" if cap else f"Photo {c['photo_number']}"
        if c.get('page'):
            lbl += f" (p{c['page']})"
        return f"{lbl} - {c.get('source')}"

    images = []
    detections_summary = []
    for c in ranked:
        path = c.get('path')
        if not path or not Path(path).exists():
            continue
        label = _label(c)
        try:
            dets = _detect_defects_with_bboxes(path, query)
            if dets:
                dets = _refine_defect_detections_hybrid(
                    path,
                    dets,
                    segmentation_mode=defect_segmentation_mode,
                )
        except Exception as exc:
            print(f"[visual-investigation] detection failed for {path}: {exc}")
            dets = []

        annotated = _annotate_image_with_detections(path, dets) if dets else None
        if annotated:
            annotated['label'] = f"{label} | detection overlay"
            annotated['detections'] = dets
            images.append(annotated)
        else:
            continue

        detections_summary.append({
            'label': label,
            'source': c.get('source'),
            'page': c.get('page'),
            'photo_number': c.get('photo_number'),
            'detections': dets,
        })

    detected_count = sum(len(x.get('detections') or []) for x in detections_summary)
    if detected_count:
        answer = (
            f"Checked {len(ranked)} report image(s) for '{query}' and found "
            f"{detected_count} detection(s) in {len(images)} image(s)."
        )
    else:
        answer = f"Checked {len(ranked)} report image(s) for '{query}' and found no visible match."
    return {
        'status': 'success',
        'query': query,
        'answer': answer,
        'images': images,
        'detections': detections_summary,
        'checked_images': len(ranked),
        'candidate_strategy': candidate_strategy,
        'scope': {'state_code': state_code, 'bridge_number': bridge_number},
    }


async def process_bridge_ask_stream(question: str, model: str = '', state_code=None,
                                    bridge_number=None, strict_grounding: bool = False,
                                    output_format: str = 'text', target_fields: str = '',
                                    selected_sources=None, force_defect_segmentation: bool = False,
                                    defect_segmentation_mode: str = 'balanced'):
    """Streaming per-bridge Ask. Yields events:
      {'type':'meta', sources, reports_used, images}  then
      {'type':'token', text} …  then  {'type':'done'} / {'type':'stopped'}.
    Image/page/vision routes (short, image-based — no long generation) come back
    as a single {'type':'final', response} event instead of streaming."""
    begin_chat()
    try:
        r = await process_bridge_ask(
            question, model=model, state_code=state_code, bridge_number=bridge_number,
            strict_grounding=strict_grounding, output_format=output_format, target_fields=target_fields,
            selected_sources=selected_sources, force_defect_segmentation=force_defect_segmentation,
            defect_segmentation_mode=defect_segmentation_mode, defer_answer=True,
        )
        if not isinstance(r, dict) or not r.get('_defer'):
            yield {'type': 'final', 'response': r}  # image/page/vision route
            return
        yield {'type': 'meta', 'sources': r.get('sources'), 'reports_used': r.get('reports_used'), 'images': r.get('images') or []}
        try:
            async for chunk in _aiter_answer_tokens(r['answer_model'], r['prompt']):
                yield {'type': 'token', 'text': chunk}
        except ChatCancelled:
            yield {'type': 'stopped'}
            return
        yield {'type': 'done'}
    finally:
        end_chat()


async def process_vision_query_only(
    question: str,
    model: str = '',
    strict_grounding: bool = False,
    output_format: str = 'text',
    target_fields: str = '',
    force_defect_segmentation: bool = False,
    defect_segmentation_mode: str = 'balanced',
    doc_hash: str | None = None,
) -> dict:
    """Query an already-ingested document without re-uploading.
    Uses LangGraph-style routing: text → KG/embeddings, vision → vision model."""
    from .rag_engine import (
        query_document, resolve_doc_hash, classify_query,
        detect_page_request, detect_photo_request, detect_list_images_request,
        detect_list_pages_request,
        get_all_page_images_b64, get_document_page_count,
        get_page_image_b64, get_photo_image_b64, get_extracted_image_count,
        get_all_extracted_images_b64, get_relevant_extracted_images,
    )

    doc_hash = resolve_doc_hash(doc_hash)
    if not doc_hash:
        raise RuntimeError('No document has been ingested yet. Please upload a file first.')

    has_images = get_extracted_image_count(doc_hash) > 0
    _total_pages = get_document_page_count(doc_hash)
    _total_images = get_extracted_image_count(doc_hash)
    # Common fields so every response keeps the frontend in sync.
    _index_fields = {
        'indexed': True,
        'doc_hash': doc_hash,
        'pages': _total_pages,
        'images_count': _total_images,
    }
    route = classify_query(question, has_images=has_images)

    # ── Handle "show page N" requests ──
    if route == 'show_page':
        requested_idx = detect_page_request(question)
        total = get_document_page_count(doc_hash)
        if total == 0:
            return {
                'status': 'success',
                'question': question,
                'answer': 'No page images were found for this document.',
                'source_chunks': [],
                'model_used': llm_providers.generation_model(),
                'document_type': 'multimodal',
                'images': [],
                'cached': True,
                **_index_fields,
            }
        if requested_idx is None or requested_idx < 1 or requested_idx > total:
            return {
                'status': 'success',
                'question': question,
                'answer': f'Page {requested_idx} is out of range. This document has {total} page{"s" if total != 1 else ""} (1–{total}).',
                'source_chunks': [],
                'model_used': llm_providers.generation_model(),
                'document_type': 'multimodal',
                'images': [],
                'cached': True,
                **_index_fields,
            }
        img = get_page_image_b64(requested_idx, doc_hash)
        return {
            'status': 'success',
            'question': question,
            'answer': f'Here is page {requested_idx} of {total} from the document.',
            'source_chunks': [f'Page {requested_idx}'],
            'model_used': llm_providers.generation_model(),
            'document_type': 'multimodal',
            'images': [img] if img else [],
            'cached': True,
            **_index_fields,
        }

    # ── Handle "show photo N" requests (photo numbering in report captions) ──
    if route == 'show_photo':
        photo_idx = detect_photo_request(question)
        if photo_idx is None:
            return {
                'status': 'success',
                'question': question,
                'answer': 'No photo number was detected in your question. Try e.g. "show photo 5".',
                'source_chunks': [],
                'model_used': llm_providers.generation_model(),
                'document_type': 'multimodal',
                'images': [],
                'cached': True,
                **_index_fields,
            }

        img = get_photo_image_b64(photo_idx, doc_hash)
        if not img:
            return {
                'status': 'success',
                'question': question,
                'answer': f'Photo {photo_idx} was not found in extracted report images. Try "show page N" or re-ingest the document.',
                'source_chunks': [],
                'model_used': llm_providers.generation_model(),
                'document_type': 'multimodal',
                'images': [],
                'cached': True,
                **_index_fields,
            }

        label = img.get('label') or f'Photo {photo_idx}'
        return {
            'status': 'success',
            'question': question,
            'answer': f'Here is {label} from the report extraction.',
            'source_chunks': [label],
            'model_used': llm_providers.generation_model(),
            'document_type': 'multimodal',
            'images': [img],
            'cached': True,
            **_index_fields,
        }

    # ── Handle "show all pages" requests ──
    if route == 'list_pages' or detect_list_pages_request(question):
        all_imgs = get_all_page_images_b64(doc_hash)
        if not all_imgs:
            return {
                'status': 'success',
                'question': question,
                'answer': 'No page images were found for this document.',
                'source_chunks': [],
                'model_used': llm_providers.generation_model(),
                'document_type': 'multimodal',
                'images': [],
                'cached': True,
                **_index_fields,
            }
        return {
            'status': 'success',
            'question': question,
            'answer': f'This document has {len(all_imgs)} page{"s" if len(all_imgs) != 1 else ""}.',
            'source_chunks': ['All pages'],
            'model_used': llm_providers.generation_model(),
            'document_type': 'multimodal',
            'images': all_imgs,
            'cached': True,
            **_index_fields,
        }

    # ── Handle "show all images/photos/figures" requests ──
    if route == 'list_images' or detect_list_images_request(question):
        extracted_imgs = get_all_extracted_images_b64(doc_hash)
        if not extracted_imgs:
            return {
                'status': 'success',
                'question': question,
                'answer': 'No extracted embedded images were found. Try "show all pages" to inspect scanned page visuals.',
                'source_chunks': [],
                'model_used': llm_providers.generation_model(),
                'document_type': 'multimodal',
                'images': [],
                'cached': True,
                **_index_fields,
            }
        return {
            'status': 'success',
            'question': question,
            'answer': f'Found {len(extracted_imgs)} extracted image{"s" if len(extracted_imgs) != 1 else ""} (photos/figures).',
            'source_chunks': ['Extracted images'],
            'model_used': llm_providers.generation_model(),
            'document_type': 'multimodal',
            'images': extracted_imgs,
            'cached': True,
            **_index_fields,
        }

    # ── Handle "show [named drawing/sketch/figure]" — display best matching image, no analysis ──
    if route == 'show_named_image':
        import base64
        from pathlib import Path
        import re as _re
        matched_imgs = get_relevant_extracted_images(question, doc_hash, k=1)
        if not matched_imgs:
            return {
                'status': 'success',
                'question': question,
                'answer': 'No matching image was found for that description. Try "show all images" to browse available figures.',
                'source_chunks': [],
                'model_used': llm_providers.generation_model(),
                'document_type': 'multimodal',
                'images': [],
                'cached': True,
                **_index_fields,
            }
        img_meta = matched_imgs[0]
        fpath = Path(img_meta.get('path', ''))
        if fpath.exists():
            data = fpath.read_bytes()
            b64 = base64.b64encode(data).decode('utf-8')
            ext = (img_meta.get('ext') or 'png').lower()
            mime = f'image/{ext}' if ext != 'jpg' else 'image/jpeg'
            raw_cap = img_meta.get('caption') or ''
            label = _re.sub(r'^Description:\s*', '', raw_cap.strip(), flags=_re.IGNORECASE)[:120] if raw_cap else fpath.name
            if img_meta.get('page'):
                label += f" (page {img_meta['page']})"
            b64_img = {'filename': fpath.name, 'mime': mime, 'data': b64, 'label': label}
        else:
            b64_img = None
        return {
            'status': 'success',
            'question': question,
            'answer': f'Here is the best matching figure: {label}' if b64_img else 'Image file not found on disk.',
            'source_chunks': [label],
            'model_used': llm_providers.generation_model(),
            'document_type': 'multimodal',
            'images': [b64_img] if b64_img else [],
            'cached': True,
            **_index_fields,
        }

    # ── Unified query (handles both text and vision routes internally) ──
    retrieval = await query_document(
        question,
        doc_hash=doc_hash,
        model=model,
        force_defect_segmentation=force_defect_segmentation,
        defect_segmentation_mode=defect_segmentation_mode,
    )
    context = retrieval.get('context', '')
    images = retrieval.get('images', [])
    meta = {'total_pages': retrieval.get('total_pages', 0)}

    if not context:
        raise RuntimeError('No relevant context found in the ingested document.')

    if retrieval.get('vision_used') or retrieval.get('kg_used'):
        answer = context
    else:
        answer = get_cerebras_answer(
            build_prompt(
                context,
                question,
                metadata=meta,
                strict_grounding=strict_grounding,
                output_format=output_format,
                target_fields=target_fields,
            ),
            model=model,
        )

    parsed_json = _safe_json_from_model_output(answer) if output_format == 'json' else None

    return {
        'status': 'success',
        'question': question,
        'answer': answer,
        'json_answer': parsed_json,
        'source_chunks': retrieval.get('sources', []),
        'model_used': llm_providers.generation_model(),
        'document_type': 'multimodal',
        'images': images,
        'cached': True,
        'indexed': True,
        'doc_hash': doc_hash,
        'pages': get_document_page_count(doc_hash),
        'images_count': get_extracted_image_count(doc_hash),
        'strict_grounding': strict_grounding,
        'output_format': output_format,
    }


async def process_vision_rag(
    file: UploadFile,
    question: str,
    model: str = '',
    strict_grounding: bool = False,
    output_format: str = 'text',
    target_fields: str = '',
    force_defect_segmentation: bool = False,
    defect_segmentation_mode: str = 'balanced',
) -> dict:
    file_path = await save_upload_to_temp(file)
    indexed_doc_hash = None
    indexed_pages = 0
    indexed_images = 0
    rag_error_message = None

    # ---------- Hybrid RAG engine (text + image) ----------
    try:
        from .rag_engine import (
            ingest_document, query_document, classify_query,
            get_last_doc_hash as _glh, get_extracted_image_count, get_document_page_count,
        )

        ingest_result = await ingest_document(str(file_path))
        indexed_doc_hash = _glh()
        if indexed_doc_hash:
            indexed_pages = get_document_page_count(indexed_doc_hash)
            indexed_images = get_extracted_image_count(indexed_doc_hash)

        # ── Route query through hybrid pipeline ──
        retrieval = await query_document(
            question,
            doc_hash=indexed_doc_hash,
            model=model,
            force_defect_segmentation=force_defect_segmentation,
            defect_segmentation_mode=defect_segmentation_mode,
        )
        context = retrieval.get('context', '')
        images = retrieval.get('images', [])
        meta = {'total_pages': retrieval.get('total_pages', 0)}

        if not context:
            raise RuntimeError('No content could be extracted from the document.')

        if retrieval.get('vision_used') or retrieval.get('kg_used'):
            answer = context
        else:
            answer = get_cerebras_answer(
                build_prompt(
                    context,
                    question,
                    metadata=meta,
                    strict_grounding=strict_grounding,
                    output_format=output_format,
                    target_fields=target_fields,
                ),
                model=model,
            )

        parsed_json = _safe_json_from_model_output(answer) if output_format == 'json' else None

        return {
            'status': 'success',
            'question': question,
            'answer': answer,
            'json_answer': parsed_json,
            'source_chunks': retrieval.get('sources', []),
            'model_used': llm_providers.generation_model(),
            'document_type': 'multimodal',
            'images': images,
            'cached': ingest_result.get('cached', False),
            'indexed': True,
            'doc_hash': indexed_doc_hash,
            'pages': indexed_pages,
            'images_count': indexed_images,
            'strict_grounding': strict_grounding,
            'output_format': output_format,
        }
    except Exception as rag_err:
        # Let cancellation propagate immediately — do not fall back to basic pipeline.
        from .rag_engine import CancelledError as _CancelledError
        if isinstance(rag_err, _CancelledError):
            raise
        rag_error_message = str(rag_err)
        print(f"Vision engine failed, falling back to basic pipeline: {rag_err}")
        import traceback; traceback.print_exc()

    if indexed_doc_hash is None:
        try:
            from .rag_engine import get_last_doc_hash as _glh, get_document_page_count, get_extracted_image_count
            indexed_doc_hash = _glh()
            if indexed_doc_hash:
                indexed_pages = get_document_page_count(indexed_doc_hash)
                indexed_images = get_extracted_image_count(indexed_doc_hash)
        except Exception:
            indexed_doc_hash = None

    # ---------- Fallback: original text-only pipeline ----------
    try:
        extracted_text, file_type = extract_text_from_file(file_path)
    except Exception as exc:
        raise RuntimeError(str(exc))

    if file_type == 'image':
        # Try direct vision model query — sends the actual image to the LLM
        answer = _query_image_with_vision_model(file_path, question)
        if answer:
            return {
                'status': 'success',
                'question': question,
                'answer': answer,
                'source_chunks': ['Image (visual analysis)'],
                'model_used': llm_providers.generation_model(),
                'document_type': 'image',
                'images': [],
            }

        # Fallback: describe + OCR text
        image_description = describe_image(file_path)
        cleaned_text = normalize_text(extracted_text) if extracted_text else ''
        if cleaned_text:
            context = (
                f"Image description:\n{image_description}\n\n"
                f"OCR text extracted from the image:\n{cleaned_text}"
            )
        else:
            context = f"Image description:\n{image_description}"
    else:
        normalized_text = normalize_text(extracted_text)
        if not normalized_text:
            raise RuntimeError('No text could be extracted from the uploaded file.')
        chunks = split_text_into_chunks(normalized_text, chunk_size=600, overlap=120)
        relevant = get_relevant_chunks(question, chunks, max_chunks=4)
        context = '\n\n---\n\n'.join(relevant)

    try:
        answer = get_cerebras_answer(
            build_prompt(
                context,
                question,
                strict_grounding=strict_grounding,
                output_format=output_format,
                target_fields=target_fields,
            ),
            model=model,
        )
        model_used = llm_providers.generation_model()
    except Exception as exc:
        raise RuntimeError(f"Cerebras is not available: {exc}. Check the network and CEREBRAS_API_KEY.")

    parsed_json = _safe_json_from_model_output(answer) if output_format == 'json' else None

    return {
        'status': 'success',
        'question': question,
        'answer': answer,
        'json_answer': parsed_json,
        'source_chunks': ['Document'] if file_type == 'pdf' else ['Image'],
        'model_used': model_used,
        'document_type': file_type,
        'images': [],
        'indexed': indexed_doc_hash is not None,
        'doc_hash': indexed_doc_hash,
        'pages': indexed_pages,
        'images_count': indexed_images,
        'rag_fallback': True,
        'rag_error': rag_error_message,
        'strict_grounding': strict_grounding,
        'output_format': output_format,
    }
