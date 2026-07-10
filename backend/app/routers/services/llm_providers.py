"""
Hosted LLM providers for the API-only web build (no local inference).

Two providers, switchable at runtime:

  • cerebras (DEFAULT) — Cerebras Cloud SDK. Text AND vision (multimodal,
    OpenAI-style image_url content). Env: CEREBRAS_API_KEY,
    model from CEREBRAS_MODEL (default 'gemma-4-31b').
  • nvidia — the hosted NVIDIA/Qwen API (OpenAI chat-completions shape) at
    https://integrate.api.nvidia.com/v1/chat/completions. Text AND vision.
    Env: NVIDIA_API_KEY, model NVIDIA_VLM_MODEL (default 'qwen/qwen3.5-397b-a17b').

Routing:
  • generate() / stream() (text OR image) → follow the current provider toggle.
    Both providers accept images, so image calls go to whichever provider is
    active — NVIDIA is NOT forced.
  • embed() → ALWAYS NVIDIA. Cerebras embeddings aren't established, and mixing
    embedding models inside one vector store corrupts it, so embeddings are
    pinned to a single NVIDIA model regardless of the text/vision toggle.

Only httpx-free stdlib urllib is used for the NVIDIA HTTP calls (matching the
rest of the backend); Cerebras goes through its official SDK. No heavy deps.
"""

import os
import json
import urllib.request
import urllib.error
import threading
from datetime import datetime, timedelta

import numpy as np

from .datapaths import DATA_DIR

# ── Provider identifiers ─────────────────────────────────────────────
CEREBRAS = "cerebras"
NVIDIA = "nvidia"
_VALID = (CEREBRAS, NVIDIA)
_DEFAULT_PROVIDER = CEREBRAS

# ── Endpoints ────────────────────────────────────────────────────────
NVIDIA_CHAT_URL = os.getenv("NVIDIA_API_URL", "https://integrate.api.nvidia.com/v1/chat/completions")
NVIDIA_EMBED_URL = os.getenv("NVIDIA_EMBED_URL", "https://integrate.api.nvidia.com/v1/embeddings")

# ── Model defaults ───────────────────────────────────────────────────
_CEREBRAS_MODEL_DEFAULT = "gemma-4-31b"
_NVIDIA_VLM_MODEL_DEFAULT = "qwen/qwen3.5-397b-a17b"
_NVIDIA_EMBED_MODEL_DEFAULT = "nvidia/nv-embedqa-e5-v5"
_CEREBRAS_MODEL_LIMITS = {
    "gemma-4-31b": {
        "context": 65536,
        "requests": {"minute": 5, "hour": 150, "day": 2400},
        "tokens": {"minute": 30000, "hour": 1000000, "day": 1000000},
    },
}

# Embedding vector dimension for the default NVIDIA embed model. Exported so the
# RAG engine can size LightRAG's EmbeddingFunc to match.
EMBED_DIM = int(os.getenv("NVIDIA_EMBED_DIM", "1024"))

# Where the runtime provider toggle is persisted. Honors BRIDGEGUARD_DATA_DIR
# via datapaths.DATA_DIR so a packaged / read-only-repo deploy still has a
# writable location.
_PROVIDER_FILE = os.path.join(str(DATA_DIR), "ai_provider.json")

# Cache one Cerebras SDK client per api-key (its constructor spins up an HTTP
# client; recreating it per call is wasteful).
_cerebras_client = None
_cerebras_client_key = None
_cerebras_lock = threading.Lock()

_USAGE_FILE = os.path.join(str(DATA_DIR), "api_usage.json")
_usage_lock = threading.Lock()


def _today_key() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _next_reset_iso() -> str:
    now = datetime.now().astimezone()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.isoformat()


def _empty_usage_day() -> dict:
    return {
        "requests": 0,
        "tokens": 0,
        "estimated_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "by_provider": {},
        "by_category": {
            "qa": {"requests": 0, "tokens": 0, "estimated_tokens": 0},
            "images": {"requests": 0, "tokens": 0, "estimated_tokens": 0},
            "embedding": {"requests": 0, "tokens": 0, "estimated_tokens": 0},
        },
    }


def _load_usage() -> dict:
    try:
        with open(_USAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_usage(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_USAGE_FILE), exist_ok=True)
        with open(_USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        print(f"[llm_providers] could not save API usage: {exc}")


def _estimate_tokens(*texts) -> int:
    chars = 0
    for text in texts:
        if isinstance(text, str):
            chars += len(text)
        elif isinstance(text, (list, tuple)):
            chars += sum(len(x) for x in text if isinstance(x, str))
    return max(1, int(chars / 4))


def _usage_from_obj(obj) -> dict:
    usage = getattr(obj, "usage", None)
    if usage is None and isinstance(obj, dict):
        usage = obj.get("usage")
    if usage is None:
        return {}

    def get(name):
        if isinstance(usage, dict):
            return usage.get(name)
        return getattr(usage, name, None)

    prompt = get("prompt_tokens") or get("input_tokens") or 0
    completion = get("completion_tokens") or get("output_tokens") or 0
    total = get("total_tokens") or ((prompt or 0) + (completion or 0))
    return {
        "prompt_tokens": int(prompt or 0),
        "completion_tokens": int(completion or 0),
        "tokens": int(total or 0),
    }


def _positive_int(value, default=None):
    if value is None or value == "":
        return default
    try:
        n = int(value)
        return n if n > 0 else default
    except Exception:
        return default


def _env_positive_int(name: str, default=None):
    return _positive_int(os.getenv(name, "").strip(), default)


def _cerebras_model_name() -> str:
    return os.getenv("CEREBRAS_MODEL", _CEREBRAS_MODEL_DEFAULT).strip() or _CEREBRAS_MODEL_DEFAULT


def _nvidia_chat_model_name() -> str:
    return os.getenv("NVIDIA_VLM_MODEL", _NVIDIA_VLM_MODEL_DEFAULT).strip() or _NVIDIA_VLM_MODEL_DEFAULT


def _record_usage(provider: str, category: str, estimated_tokens: int = 0,
                  usage: dict = None, requests: int = 1) -> None:
    usage = usage or {}
    tokens = int(usage.get("tokens") or estimated_tokens or 0)
    estimated = int(estimated_tokens or 0)
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    category = category if category in {"qa", "images", "embedding"} else "qa"

    with _usage_lock:
        data = _load_usage()
        day = _today_key()
        daily = data.setdefault(day, _empty_usage_day())
        daily["requests"] = int(daily.get("requests") or 0) + requests
        daily["tokens"] = int(daily.get("tokens") or 0) + tokens
        daily["estimated_tokens"] = int(daily.get("estimated_tokens") or 0) + estimated
        daily["prompt_tokens"] = int(daily.get("prompt_tokens") or 0) + prompt
        daily["completion_tokens"] = int(daily.get("completion_tokens") or 0) + completion

        by_provider = daily.setdefault("by_provider", {})
        p = by_provider.setdefault(provider, {"requests": 0, "tokens": 0, "estimated_tokens": 0})
        p["requests"] = int(p.get("requests") or 0) + requests
        p["tokens"] = int(p.get("tokens") or 0) + tokens
        p["estimated_tokens"] = int(p.get("estimated_tokens") or 0) + estimated

        by_category = daily.setdefault("by_category", {})
        c = by_category.setdefault(category, {"requests": 0, "tokens": 0, "estimated_tokens": 0})
        c["requests"] = int(c.get("requests") or 0) + requests
        c["tokens"] = int(c.get("tokens") or 0) + tokens
        c["estimated_tokens"] = int(c.get("estimated_tokens") or 0) + estimated

        _save_usage(data)


def usage_snapshot() -> dict:
    """Local daily hosted-API usage tracked by this app.

    Provider dashboards may have stricter account-level limits. Cerebras
    gemma-4-31b has built-in defaults; env vars can override any limit.
    """
    with _usage_lock:
        data = _load_usage()
    day = _today_key()
    daily = data.get(day) or _empty_usage_day()

    provider = get_provider()
    model = _cerebras_model_name() if provider == CEREBRAS else _nvidia_chat_model_name()
    context_window = None
    rate_limits = {"requests": {"minute": None, "hour": None, "day": None},
                   "tokens": {"minute": None, "hour": None, "day": None}}
    if provider == CEREBRAS:
        defaults = _CEREBRAS_MODEL_LIMITS.get(model, {})
        default_requests = defaults.get("requests") or {}
        default_tokens = defaults.get("tokens") or {}
        context_window = defaults.get("context")
        rate_limits = {
            "requests": {
                "minute": _env_positive_int("CEREBRAS_MINUTE_REQUEST_LIMIT", default_requests.get("minute")),
                "hour": _env_positive_int("CEREBRAS_HOURLY_REQUEST_LIMIT", default_requests.get("hour")),
                "day": _env_positive_int("CEREBRAS_DAILY_REQUEST_LIMIT", default_requests.get("day")),
            },
            "tokens": {
                "minute": _env_positive_int("CEREBRAS_MINUTE_TOKEN_LIMIT", default_tokens.get("minute")),
                "hour": _env_positive_int("CEREBRAS_HOURLY_TOKEN_LIMIT", default_tokens.get("hour")),
                "day": _env_positive_int("CEREBRAS_DAILY_TOKEN_LIMIT", default_tokens.get("day")),
            },
        }
    elif provider == NVIDIA:
        rate_limits = {
            "requests": {
                "minute": _env_positive_int("NVIDIA_MINUTE_REQUEST_LIMIT"),
                "hour": _env_positive_int("NVIDIA_HOURLY_REQUEST_LIMIT"),
                "day": _env_positive_int("NVIDIA_DAILY_REQUEST_LIMIT"),
            },
            "tokens": {
                "minute": _env_positive_int("NVIDIA_MINUTE_TOKEN_LIMIT"),
                "hour": _env_positive_int("NVIDIA_HOURLY_TOKEN_LIMIT"),
                "day": _env_positive_int("NVIDIA_DAILY_TOKEN_LIMIT"),
            },
        }

    limits = {
        "tokens": rate_limits["tokens"]["day"],
        "requests": rate_limits["requests"]["day"],
    }
    token_pct = None
    request_pct = None
    if limits["tokens"]:
        token_pct = min(100, round((int(daily.get("tokens") or 0) / limits["tokens"]) * 100, 1))
    if limits["requests"]:
        request_pct = min(100, round((int(daily.get("requests") or 0) / limits["requests"]) * 100, 1))
    exhausted = (
        (limits["tokens"] and int(daily.get("tokens") or 0) >= limits["tokens"])
        or (limits["requests"] and int(daily.get("requests") or 0) >= limits["requests"])
    )
    return {
        "date": day,
        "provider": provider,
        "model": model,
        "context_window": context_window,
        "local_only": True,
        "limits_configured": bool(limits["tokens"] or limits["requests"]),
        "limits": limits,
        "rate_limits": rate_limits,
        "percent": {"tokens": token_pct, "requests": request_pct},
        "reset_at": _next_reset_iso(),
        "exhausted": exhausted,
        "message": (
            f"Configured daily API limit reached. Resume after {_next_reset_iso()}."
            if exhausted else
            "Usage is tracked locally by this app; provider dashboard limits may differ."
        ),
        "usage": daily,
    }


# ── Provider selection ───────────────────────────────────────────────
def get_provider() -> str:
    """Return the active provider: 'cerebras' or 'nvidia'.

    Order: BRIDGEGUARD_AI_PROVIDER env → data/ai_provider.json → default
    'cerebras'. Any legacy/unknown value (e.g. the old 'remote'/'local') maps
    to the default 'cerebras'."""
    envp = os.getenv("BRIDGEGUARD_AI_PROVIDER", "").strip().lower()
    if envp in _VALID:
        return envp
    try:
        with open(_PROVIDER_FILE, "r", encoding="utf-8") as f:
            p = str(json.load(f).get("provider", "")).strip().lower()
        if p in _VALID:
            return p
    except Exception:
        pass
    return _DEFAULT_PROVIDER


def set_provider(name: str) -> str:
    """Validate and persist the provider choice. Raises ValueError on an
    invalid name. Writes are wrapped in try/except so a read-only filesystem
    never crashes the switch (the choice still takes effect for env-based reads
    / in-process, it just won't survive a restart)."""
    name = str(name or "").strip().lower()
    if name not in _VALID:
        raise ValueError("provider must be 'cerebras' or 'nvidia'")
    try:
        os.makedirs(os.path.dirname(_PROVIDER_FILE), exist_ok=True)
        with open(_PROVIDER_FILE, "w", encoding="utf-8") as f:
            json.dump({"provider": name}, f)
    except Exception as exc:
        print(f"[llm_providers] could not persist provider choice: {exc}")
    print(f"[llm_providers] provider set to {name}")
    return name


def status() -> dict:
    """Snapshot of provider configuration for the settings UI."""
    return {
        "provider": get_provider(),
        "cerebras_key_set": bool(os.getenv("CEREBRAS_API_KEY", "").strip()),
        "nvidia_key_set": bool(os.getenv("NVIDIA_API_KEY", "").strip()),
        "cerebras_model": os.getenv("CEREBRAS_MODEL", _CEREBRAS_MODEL_DEFAULT),
        "nvidia_model": os.getenv("NVIDIA_VLM_MODEL", _NVIDIA_VLM_MODEL_DEFAULT),
        "embed_model": os.getenv("NVIDIA_EMBED_MODEL", _NVIDIA_EMBED_MODEL_DEFAULT),
        "embed_dim": EMBED_DIM,
    }


# ── Cerebras (SDK) ───────────────────────────────────────────────────
def _get_cerebras_client(timeout: int):
    """Lazily build (and cache by api-key) a Cerebras SDK client. Imported here
    so the module stays importable without cerebras-cloud-sdk installed."""
    global _cerebras_client, _cerebras_client_key
    key = os.environ.get("CEREBRAS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("CEREBRAS_API_KEY is not set")
    with _cerebras_lock:
        if _cerebras_client is None or _cerebras_client_key != key:
            from cerebras.cloud.sdk import Cerebras
            _cerebras_client = Cerebras(api_key=key, timeout=float(timeout))
            _cerebras_client_key = key
        return _cerebras_client


def _cerebras_messages(prompt: str, images_b64=None) -> list:
    """Build the OpenAI-style messages array. With images, use the multimodal
    content list (text + image_url data-URLs); without, a plain string."""
    if images_b64:
        content = [{"type": "text", "text": prompt}]
        for b64 in images_b64:
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        return [{"role": "user", "content": content}]
    return [{"role": "user", "content": prompt}]


def _cerebras_create_kwargs(model, messages, num_predict, temperature, stream):
    kwargs = {
        "messages": messages,
        "model": model or os.getenv("CEREBRAS_MODEL", _CEREBRAS_MODEL_DEFAULT),
        "max_completion_tokens": num_predict,
        "temperature": 0.0 if temperature is None else temperature,
        "top_p": 1,
        "stream": stream,
    }
    # reasoning_effort is a Cerebras-specific knob (see reference usage). It is
    # OFF by default: on reasoning models (e.g. gemma-4-31b) the hidden reasoning
    # consumes the token budget and leaves message.content EMPTY when num_predict
    # is modest — which many call sites use. Opt back in with
    # CEREBRAS_REASONING_EFFORT=low|medium|high (and give it a large num_predict).
    effort = os.getenv("CEREBRAS_REASONING_EFFORT", "").strip().lower()
    if effort and effort not in ("none", "off", "0"):
        kwargs["reasoning_effort"] = effort
    return kwargs


def _message_text(msg) -> str:
    """Answer text from a chat message, falling back to a reasoning field so we
    never surface a blank answer when a reasoning model split its output."""
    text = getattr(msg, "content", None) or ""
    if not text.strip():
        for attr in ("reasoning_content", "reasoning"):
            alt = getattr(msg, attr, None)
            if isinstance(alt, str) and alt.strip():
                return alt
    return text


def _cerebras_call(client, kwargs, retries=3):
    """Create a completion, retrying transient 429/queue errors with backoff."""
    import time
    last = None
    for attempt in range(retries):
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as e:  # noqa: BLE001 — inspect for rate-limit shape
            last = e
            name = type(e).__name__
            status = getattr(e, "status_code", None)
            if status == 429 or "RateLimit" in name or "too_many_requests" in str(e):
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
            raise
    raise last


def _cerebras_generate(prompt, images_b64=None, timeout=300, num_predict=1024,
                       model=None, temperature=0.0) -> str:
    client = _get_cerebras_client(timeout)
    messages = _cerebras_messages(prompt, images_b64)
    estimated = _estimate_tokens(prompt) + (750 * len(images_b64 or []))
    completion = _cerebras_call(
        client, _cerebras_create_kwargs(model, messages, num_predict, temperature, False)
    )
    usage = _usage_from_obj(completion)
    try:
        text = _message_text(completion.choices[0].message)
    except Exception:
        text = ""
    _record_usage(CEREBRAS, "images" if images_b64 else "qa",
                  estimated_tokens=estimated + _estimate_tokens(text), usage=usage)
    return text


def _cerebras_stream(prompt, images_b64=None, timeout=300, num_predict=1024,
                     model=None, temperature=0.0):
    client = _get_cerebras_client(timeout)
    messages = _cerebras_messages(prompt, images_b64)
    estimated_in = _estimate_tokens(prompt) + (750 * len(images_b64 or []))
    stream_obj = _cerebras_call(
        client, _cerebras_create_kwargs(model, messages, num_predict, temperature, True)
    )
    out_chunks = []
    try:
        for chunk in stream_obj:
            try:
                delta = chunk.choices[0].delta
                text = getattr(delta, "content", None)
            except Exception:
                text = None
            if isinstance(text, str) and text:
                out_chunks.append(text)
                yield text
    finally:
        # Closing the generator (caller's gen.close()) lands here → close the
        # underlying stream so the hosted generation is aborted (a user "Stop").
        try:
            stream_obj.close()
        except Exception:
            pass
        _record_usage(CEREBRAS, "images" if images_b64 else "qa",
                      estimated_tokens=estimated_in + _estimate_tokens("".join(out_chunks)))


# ── NVIDIA (hosted API, OpenAI chat-completions shape, stdlib urllib) ─
def _nvidia_default_model(images_b64=None) -> str:
    """Text stages may point at a cheaper NVIDIA_TEXT_MODEL; image stages must
    use the vision-capable NVIDIA_VLM_MODEL."""
    vlm = os.getenv("NVIDIA_VLM_MODEL", _NVIDIA_VLM_MODEL_DEFAULT)
    if images_b64:
        return vlm
    return os.getenv("NVIDIA_TEXT_MODEL", "").strip() or vlm


def _nvidia_payload(prompt, images_b64, num_predict, temperature, model, stream):
    content = [{"type": "text", "text": prompt}]
    for b64 in images_b64 or []:
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    messages = [{"role": "user", "content": content if images_b64 else prompt}]
    # NVIDIA_VLM_THINK=1 lets the hosted model reason before answering (no local
    # cost; the JSON extractor tolerates surrounding thinking). The token budget
    # must then cover the reasoning too.
    think = os.getenv("NVIDIA_VLM_THINK", "0") == "1"
    return {
        "model": model or _nvidia_default_model(images_b64),
        "messages": messages,
        "stream": stream,
        "max_tokens": max(num_predict, 8192) if think else num_predict,
        # app policy: temperature 0 everywhere → repeatable output
        "temperature": 0.0 if temperature is None else temperature,
        "chat_template_kwargs": {"enable_thinking": think},
    }


def _nvidia_generate(prompt, images_b64=None, timeout=300, num_predict=1024,
                     model=None, temperature=0.0) -> str:
    """Non-streaming NVIDIA hosted call. Images are inline JPEG data-URLs (keep
    them small — the hosted API rejects large payloads)."""
    key = os.getenv("NVIDIA_API_KEY", "").strip()
    if not key:
        raise RuntimeError("NVIDIA_API_KEY is not set")
    payload = _nvidia_payload(prompt, images_b64, num_predict, temperature, model, False)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(NVIDIA_CHAT_URL, data=data, headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {key}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    msg = (body.get("choices") or [{}])[0].get("message") or {}
    text = msg.get("content", "") or ""
    estimated = _estimate_tokens(prompt, text) + (750 * len(images_b64 or []))
    _record_usage(NVIDIA, "images" if images_b64 else "qa",
                  estimated_tokens=estimated, usage=_usage_from_obj(body))
    return text


def _nvidia_stream(prompt, images_b64=None, timeout=300, num_predict=1024,
                   model=None, temperature=0.0):
    """Streaming NVIDIA hosted call: yields answer-text chunks (OpenAI-style SSE
    `data:` lines). Closing the generator closes the HTTP response, which aborts
    the hosted generation — that is how a user "Stop" cancels a remote chat."""
    key = os.getenv("NVIDIA_API_KEY", "").strip()
    if not key:
        raise RuntimeError("NVIDIA_API_KEY is not set")
    payload = _nvidia_payload(prompt, images_b64, num_predict, temperature, model, True)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(NVIDIA_CHAT_URL, data=data, headers={
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {key}",
    })
    resp = urllib.request.urlopen(req, timeout=timeout)
    out_chunks = []
    try:
        for raw in resp:
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line.startswith("data:"):
                continue
            line = line[len("data:"):].strip()
            if line == "[DONE]":
                break
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            delta = (obj.get("choices") or [{}])[0].get("delta") or {}
            chunk = delta.get("content")
            if isinstance(chunk, str) and chunk:
                out_chunks.append(chunk)
                yield chunk
    finally:
        try:
            resp.close()  # closing the socket aborts the hosted generation
        except Exception:
            pass
        _record_usage(NVIDIA, "images" if images_b64 else "qa",
                      estimated_tokens=_estimate_tokens(prompt, "".join(out_chunks)) + (750 * len(images_b64 or [])))


# ── Public text/vision entry points (provider-routed) ────────────────
def generate(prompt, images_b64=None, timeout=300, num_predict=1024,
             model=None, temperature=0.0) -> str:
    """Text or image completion via the CURRENT provider. Both providers accept
    images, so image calls follow the toggle (NVIDIA is not forced)."""
    if get_provider() == NVIDIA:
        return _nvidia_generate(prompt, images_b64=images_b64, timeout=timeout,
                                num_predict=num_predict, model=model, temperature=temperature)
    return _cerebras_generate(prompt, images_b64=images_b64, timeout=timeout,
                              num_predict=num_predict, model=model, temperature=temperature)


def stream(prompt, images_b64=None, timeout=300, num_predict=1024, model=None):
    """Streaming text/image completion via the CURRENT provider. Yields text
    chunks; closing the generator aborts the hosted generation (user Stop)."""
    if get_provider() == NVIDIA:
        yield from _nvidia_stream(prompt, images_b64=images_b64, timeout=timeout,
                                  num_predict=num_predict, model=model)
    else:
        yield from _cerebras_stream(prompt, images_b64=images_b64, timeout=timeout,
                                    num_predict=num_predict, model=model)


# ── Embeddings (ALWAYS NVIDIA) ───────────────────────────────────────
# Pinned to NVIDIA regardless of the text/vision toggle: Cerebras embeddings
# aren't established, and mixing embedding models within one vector store
# corrupts similarity search. Keep every embedding — LightRAG's KG and the
# pure-python fallback retrieval — on this single NVIDIA model.
def embed(texts) -> np.ndarray:
    """Embed a list of strings via NVIDIA NIM embeddings. Returns an
    (N, EMBED_DIM) float32 array. Raises RuntimeError on failure (callers that
    want a soft fallback wrap this in try/except)."""
    key = os.getenv("NVIDIA_API_KEY", "").strip()
    if not key:
        raise RuntimeError("NVIDIA_API_KEY is not set")
    model = os.getenv("NVIDIA_EMBED_MODEL", _NVIDIA_EMBED_MODEL_DEFAULT)
    items = list(texts or [])
    if not items:
        return np.zeros((0, EMBED_DIM), dtype=np.float32)

    all_vecs = []
    batch_size = int(os.getenv("NVIDIA_EMBED_BATCH", "32"))
    for i in range(0, len(items), batch_size):
        batch = [(t[:2000] if t else " ") for t in items[i:i + batch_size]]
        payload = {
            "input": batch,
            "model": model,
            # NVIDIA NIM requires an input_type; 'passage' for stored documents.
            "input_type": "passage",
            "encoding_format": "float",
            "truncate": "END",
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(NVIDIA_EMBED_URL, data=data, headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise RuntimeError(f"NVIDIA embeddings request failed: {exc}")
        rows = body.get("data") or []
        _record_usage(NVIDIA, "embedding",
                      estimated_tokens=_estimate_tokens(batch),
                      usage=_usage_from_obj(body))
        # Preserve request order (NVIDIA returns an 'index' per row).
        rows = sorted(rows, key=lambda r: r.get("index", 0))
        for r in rows:
            all_vecs.append(r.get("embedding") or [])
    if not all_vecs:
        raise RuntimeError("NVIDIA embeddings returned no vectors")
    return np.array(all_vecs, dtype=np.float32)
