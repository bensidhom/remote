"""Cerebras generation plus a pinned, non-Ollama Nomic embedding service.

Text and multimodal completion are served only by the configured Cerebras
endpoint. Embeddings are served by Hugging Face Text Embeddings Inference (TEI)
running ``nomic-ai/nomic-embed-text-v1.5`` at a pinned Hub revision.
"""

import os
import json
import urllib.request
import urllib.error
import threading
from datetime import datetime, timedelta

import numpy as np

from .datapaths import DATA_DIR

CEREBRAS = "cerebras"
TEI = "tei"

# ── Model defaults ───────────────────────────────────────────────────
_CEREBRAS_MODEL_DEFAULT = "gemma-4-31b"
_SERVER_MODEL_OVERRIDES = frozenset({"gemma-4-31b", "gpt-oss-120b"})
_CEREBRAS_VISION_MODEL_DEFAULT = "gemma-4-31b"
_CEREBRAS_MODEL_CAPABILITIES = {
    "gemma-4-31b": {
        "text": True,
        "vision": True,
        "json_schema": True,
    },
    "gpt-oss-120b": {
        "text": True,
        "vision": False,
        "json_schema": True,
    },
}
_TEI_EMBED_MODEL_DEFAULT = "nomic-ai/nomic-embed-text-v1.5"
_TEI_EMBED_REVISION_DEFAULT = "20d1e6057a2e1fa08b45123e41ce9381fc338f0e"
_TEI_RUNTIME_VERSION_DEFAULT = "1.9.3"
_TEI_INPUT_POLICY = "nomic-v1.5-search-prefixes-v1"
_TEI_NORMALIZE = True
_TEI_TRUNCATE = True
_TEI_TRUNCATION_DIRECTION = "right"
_TEI_DTYPE = "float32"
_TEI_POOLING = "mean"
_CEREBRAS_MODEL_LIMITS = {
    "gemma-4-31b": {
        "context": 65536,
        "requests": {"minute": 5, "hour": 150, "day": 2400},
        "tokens": {"minute": None, "hour": 1000000, "day": 1000000},
    },
}


def _embedding_dimension(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


def embedding_profile() -> dict:
    """Return the active, restart-scoped embedding-space identity.

    Provider, exact revision, input-prefix policy, and dimension are
    inseparable. Persist all of them beside every vector matrix.
    """
    provider = TEI
    model = (
        os.getenv("TEI_EMBED_MODEL", _TEI_EMBED_MODEL_DEFAULT).strip()
        or _TEI_EMBED_MODEL_DEFAULT
    )
    revision = (
        os.getenv("TEI_EMBED_REVISION", _TEI_EMBED_REVISION_DEFAULT).strip()
        or _TEI_EMBED_REVISION_DEFAULT
    )
    runtime_version = (
        os.getenv("TEI_RUNTIME_VERSION", _TEI_RUNTIME_VERSION_DEFAULT).strip()
        or _TEI_RUNTIME_VERSION_DEFAULT
    )
    return {
        "provider": provider,
        "model": model,
        "revision": revision,
        "runtime_version": runtime_version,
        "dimension": _embedding_dimension("TEI_EMBED_DIM", 768),
        "input_policy": _TEI_INPUT_POLICY,
        "normalize": _TEI_NORMALIZE,
        "truncate": _TEI_TRUNCATE,
        "truncation_direction": _TEI_TRUNCATION_DIRECTION,
        "dtype": _TEI_DTYPE,
        "pooling": _TEI_POOLING,
    }


def embedding_profile_matches(value) -> bool:
    """Strict cache-profile comparison; legacy/missing metadata is unsafe."""
    if not isinstance(value, dict):
        return False
    active = embedding_profile()
    try:
        cached_dim = int(value.get("dimension"))
    except (TypeError, ValueError):
        return False
    return (
        str(value.get("provider") or "").lower() == active["provider"]
        and str(value.get("model") or "") == active["model"]
        and str(value.get("revision") or "") == active["revision"]
        and str(value.get("runtime_version") or "") == active["runtime_version"]
        and cached_dim == active["dimension"]
        and str(value.get("input_policy") or "") == active["input_policy"]
        and value.get("normalize") is active["normalize"]
        and value.get("truncate") is active["truncate"]
        and str(value.get("truncation_direction") or "") == active["truncation_direction"]
        and str(value.get("dtype") or "") == active["dtype"]
        and str(value.get("pooling") or "") == active["pooling"]
    )


# Compatibility export used by existing callers. Environment changes are
# restart-scoped; use embedding_profile() for the authoritative active values.
EMBED_DIM = embedding_profile()["dimension"]

# Cache one Cerebras SDK client per api-key and timeout. The SDK applies its
# timeout at client construction, so reusing a client built for a different
# operation would silently ignore the caller's deadline.
_cerebras_client = None
_cerebras_client_key = None
_cerebras_client_timeout = None
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


def generation_model() -> str:
    """Return the server-controlled Cerebras model used by every generation call."""
    return _cerebras_model_name()


def vision_model() -> str:
    """Return the server-controlled model reserved for multimodal work."""

    return (
        os.getenv("CEREBRAS_VISION_MODEL", _CEREBRAS_VISION_MODEL_DEFAULT).strip()
        or _CEREBRAS_VISION_MODEL_DEFAULT
    )


def generation_model_capabilities(model: str | None = None) -> dict:
    selected = str(model or generation_model()).strip()
    capabilities = _CEREBRAS_MODEL_CAPABILITIES.get(selected)
    if capabilities is None:
        return {
            "model": selected,
            "known": False,
            "text": False,
            "vision": False,
            "json_schema": False,
        }
    return {"model": selected, "known": True, **capabilities}


def require_generation_capability(capability: str, *, model: str | None = None) -> dict:
    if capability not in {"text", "vision", "json_schema"}:
        raise RuntimeError("unsupported generation capability check")
    capabilities = generation_model_capabilities(model)
    if not capabilities.get(capability):
        raise RuntimeError(
            f"configured model {capabilities['model']!r} does not have reviewed {capability} capability"
        )
    return capabilities


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
    cerebras_usage = (daily.get("by_provider") or {}).get(CEREBRAS) or {
        "requests": 0,
        "tokens": 0,
        "estimated_tokens": 0,
    }

    provider = CEREBRAS
    model = _cerebras_model_name()
    context_window = None
    rate_limits = {"requests": {"minute": None, "hour": None, "day": None},
                   "tokens": {"minute": None, "hour": None, "day": None}}
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

    limits = {
        "tokens": rate_limits["tokens"]["day"],
        "requests": rate_limits["requests"]["day"],
    }
    token_pct = None
    request_pct = None
    if limits["tokens"]:
        token_pct = min(100, round((int(cerebras_usage.get("tokens") or 0) / limits["tokens"]) * 100, 1))
    if limits["requests"]:
        request_pct = min(100, round((int(cerebras_usage.get("requests") or 0) / limits["requests"]) * 100, 1))
    exhausted = (
        (limits["tokens"] and int(cerebras_usage.get("tokens") or 0) >= limits["tokens"])
        or (limits["requests"] and int(cerebras_usage.get("requests") or 0) >= limits["requests"])
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
        "provider_usage": cerebras_usage,
    }


def status() -> dict:
    """Snapshot of provider configuration for the settings UI."""
    embed_profile = embedding_profile()
    return {
        "provider": CEREBRAS,
        "cerebras_key_set": bool(os.getenv("CEREBRAS_API_KEY", "").strip()),
        "cerebras_model": generation_model(),
        "cerebras_model_capabilities": generation_model_capabilities(),
        "cerebras_vision_model": vision_model(),
        "cerebras_vision_model_capabilities": generation_model_capabilities(
            vision_model()
        ),
        "embedding_provider": embed_profile["provider"],
        "embed_model": embed_profile["model"],
        "embed_dim": embed_profile["dimension"],
        "embed_revision": embed_profile["revision"],
    }


# ── Cerebras (SDK) ───────────────────────────────────────────────────
def _get_cerebras_client(timeout: int):
    """Lazily build (and cache by api-key) a Cerebras SDK client. Imported here
    so the module stays importable without cerebras-cloud-sdk installed."""
    global _cerebras_client, _cerebras_client_key, _cerebras_client_timeout
    key = os.environ.get("CEREBRAS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("CEREBRAS_API_KEY is not set")
    with _cerebras_lock:
        requested_timeout = float(timeout)
        if (
            _cerebras_client is None
            or _cerebras_client_key != key
            or _cerebras_client_timeout != requested_timeout
        ):
            from cerebras.cloud.sdk import Cerebras
            # BridgeGuard already owns its narrow retry policy for explicit 429
            # responses in ``_cerebras_call``. Disable the SDK's implicit
            # transport retries so a 120-second request cannot become 360s.
            _cerebras_client = Cerebras(
                api_key=key,
                timeout=requested_timeout,
                max_retries=0,
            )
            _cerebras_client_key = key
            _cerebras_client_timeout = requested_timeout
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


def _selected_generation_model(server_model_override=None) -> str:
    selected_model = generation_model()
    if server_model_override is not None:
        candidate = str(server_model_override).strip()
        if candidate not in _SERVER_MODEL_OVERRIDES:
            raise RuntimeError("unsupported server-controlled Cerebras model override")
        selected_model = candidate
    return selected_model


def _cerebras_create_kwargs(
    model,
    messages,
    num_predict,
    temperature,
    stream,
    response_format=None,
    server_model_override=None,
):
    # The server owns model selection; legacy/client model hints remain ignored.
    # A narrow, allowlisted override is available only to trusted server call
    # sites such as Bridgette, so browser-supplied model names cannot route API
    # traffic to arbitrary provider models.
    selected_model = _selected_generation_model(server_model_override)
    kwargs = {
        "messages": messages,
        "model": selected_model,
        "max_completion_tokens": num_predict,
        "temperature": 0.0 if temperature is None else temperature,
        "top_p": 1,
        "stream": stream,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
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
    """Create a completion, retrying only recognized transient failures."""
    import time
    last = None
    for attempt in range(retries):
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as e:  # noqa: BLE001 — inspect for rate-limit shape
            last = e
            name = type(e).__name__
            status = getattr(e, "status_code", None)
            transient_name = name in {
                "APIConnectionError",
                "APITimeoutError",
                "ConnectError",
                "ReadError",
                "RemoteProtocolError",
            }
            transient_status = isinstance(status, int) and (
                status in {408, 409, 425, 429} or 500 <= status <= 599
            )
            if (
                transient_name
                or transient_status
                or "RateLimit" in name
                or "too_many_requests" in str(e)
            ):
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


def _typed_messages_have_images(messages) -> bool:
    found = False
    if not isinstance(messages, list) or not messages:
        raise RuntimeError("typed generation messages are invalid")
    for message in messages:
        if not isinstance(message, dict) or set(message) != {"role", "content"}:
            raise RuntimeError("typed generation message shape is invalid")
        if message["role"] not in {"system", "user", "assistant"}:
            raise RuntimeError("typed generation message role is invalid")
        content = message["content"]
        if isinstance(content, str):
            if not content.strip():
                raise RuntimeError("typed generation message is empty")
            continue
        if not isinstance(content, list) or not content:
            raise RuntimeError("typed generation content is invalid")
        for item in content:
            if not isinstance(item, dict):
                raise RuntimeError("typed generation content item is invalid")
            kind = item.get("type")
            if kind == "text":
                if set(item) != {"type", "text"} or not str(item["text"]).strip():
                    raise RuntimeError("typed generation text item is invalid")
            elif kind == "image_url":
                image_url = item.get("image_url")
                if (
                    set(item) != {"type", "image_url"}
                    or not isinstance(image_url, dict)
                    or set(image_url) - {"url", "detail"}
                    or not str(image_url.get("url") or "").startswith(
                        ("data:image/png;base64,", "data:image/jpeg;base64,")
                    )
                ):
                    raise RuntimeError("typed generation image item is invalid")
                found = True
            else:
                raise RuntimeError("typed generation content type is unsupported")
    return found


def _estimate_typed_messages(messages) -> int:
    text_items = []
    image_count = 0
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            text_items.append(content)
        else:
            for item in content:
                if item.get("type") == "text":
                    text_items.append(str(item.get("text") or ""))
                elif item.get("type") == "image_url":
                    image_count += 1
    return _estimate_tokens(text_items) + 750 * image_count


def generate_messages(
    messages,
    *,
    timeout=300,
    num_predict=1024,
    temperature=0.0,
    response_format=None,
    server_model_override=None,
) -> dict:
    """Typed text/multimodal completion with model and usage receipt.

    Browser callers cannot choose ``server_model_override``; this entry point is
    intended for trusted server orchestration such as point-cloud review.
    """

    has_images = _typed_messages_have_images(messages)
    selected_model = _selected_generation_model(server_model_override)
    require_generation_capability("vision" if has_images else "text", model=selected_model)
    if response_format is not None:
        require_generation_capability("json_schema", model=selected_model)
    client = _get_cerebras_client(timeout)
    completion = _cerebras_call(
        client,
        _cerebras_create_kwargs(
            None,
            messages,
            num_predict,
            temperature,
            False,
            response_format=response_format,
            server_model_override=selected_model,
        ),
    )
    usage = _usage_from_obj(completion)
    try:
        text = _message_text(completion.choices[0].message)
    except Exception:
        text = ""
    estimated = _estimate_typed_messages(messages) + _estimate_tokens(text)
    _record_usage(
        CEREBRAS,
        "images" if has_images else "qa",
        estimated_tokens=estimated,
        usage=usage,
    )
    return {
        "text": text,
        "provider": CEREBRAS,
        "model": selected_model,
        "usage": usage,
        "has_images": has_images,
    }


def _cerebras_stream(
    prompt,
    images_b64=None,
    timeout=300,
    num_predict=1024,
    model=None,
    temperature=0.0,
    response_format=None,
    server_model_override=None,
):
    client = _get_cerebras_client(timeout)
    messages = _cerebras_messages(prompt, images_b64)
    estimated_in = _estimate_tokens(prompt) + (750 * len(images_b64 or []))
    stream_obj = _cerebras_call(
        client,
        _cerebras_create_kwargs(
            model,
            messages,
            num_predict,
            temperature,
            True,
            response_format=response_format,
            server_model_override=server_model_override,
        ),
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


# ── Public text/vision entry points (Cerebras only) ──────────────────
def generate(prompt, images_b64=None, timeout=300, num_predict=1024,
             model=None, temperature=0.0) -> str:
    """Text or image completion via the configured Cerebras endpoint."""
    return _cerebras_generate(prompt, images_b64=images_b64, timeout=timeout,
                              num_predict=num_predict, model=model, temperature=temperature)


def stream(
    prompt,
    images_b64=None,
    timeout=300,
    num_predict=1024,
    model=None,
    response_format=None,
    server_model_override=None,
):
    """Stream Cerebras text/image completion; closing aborts generation."""
    yield from _cerebras_stream(
        prompt,
        images_b64=images_b64,
        timeout=timeout,
        num_predict=num_predict,
        model=model,
        response_format=response_format,
        server_model_override=server_model_override,
    )


# ── Embeddings (pinned Nomic v1.5 via Hugging Face TEI) ──────────────
# Every index/cache records the complete vector-shaping profile and rejects
# legacy or mismatched matrices.
def _validated_embedding_matrix(vectors, item_count: int, profile: dict) -> np.ndarray:
    """Materialize and validate provider output before it reaches any cache."""
    try:
        matrix = np.asarray(vectors, dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"{profile['provider']} embeddings were not a rectangular float matrix"
        ) from exc
    expected = (item_count, profile["dimension"])
    if matrix.ndim != 2 or matrix.shape != expected:
        raise RuntimeError(
            f"{profile['provider']} embedding shape mismatch for {profile['model']}: "
            f"expected {expected}, got {tuple(matrix.shape)}"
        )
    if not np.isfinite(matrix).all():
        raise RuntimeError(f"{profile['provider']} embeddings contained NaN or infinity")
    return matrix


def _tei_base_url() -> str:
    return (os.getenv("TEI_EMBED_BASE_URL") or "http://127.0.0.1:8080").rstrip("/")


def _tei_headers() -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    key = os.getenv("TEI_EMBED_API_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _tei_embed(
    items: list,
    input_type: str,
    profile: dict,
    *,
    record_usage: bool = True,
) -> np.ndarray:
    prefix = "search_document: " if input_type == "passage" else "search_query: "
    batch_size = _embedding_dimension("TEI_EMBED_BATCH", 16)
    vectors = []
    for start in range(0, len(items), batch_size):
        batch = [prefix + (text if text.strip() else " ") for text in items[start:start + batch_size]]
        payload = {
            "inputs": batch,
            "truncate": profile["truncate"],
            "truncation_direction": profile["truncation_direction"],
            "normalize": profile["normalize"],
            "dimensions": profile["dimension"],
        }
        req = urllib.request.Request(
            f"{_tei_base_url()}/embed",
            data=json.dumps(payload).encode("utf-8"),
            headers=_tei_headers(),
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                rows = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"TEI embeddings request failed: {exc}") from exc
        if not isinstance(rows, list):
            raise RuntimeError("TEI embeddings returned an invalid response")
        vectors.extend(rows)
        if record_usage:
            _record_usage(
                TEI,
                "embedding",
                estimated_tokens=_estimate_tokens(batch),
            )
    return _validated_embedding_matrix(vectors, len(items), profile)


def verify_embedding_backend(timeout: int = 30) -> dict:
    """Verify TEI serves the pinned model revision and produces 768-D vectors."""
    profile = embedding_profile()
    req = urllib.request.Request(f"{_tei_base_url()}/info", headers=_tei_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            info = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"TEI readiness check failed: {exc}") from exc
    model_id = str(info.get("model_id") or info.get("modelId") or "")
    model_sha = str(info.get("model_sha") or info.get("modelSha") or "")
    if model_id != profile["model"]:
        raise RuntimeError(
            f"TEI model mismatch: expected {profile['model']!r}, got {model_id!r}"
        )
    if model_sha != profile["revision"]:
        raise RuntimeError(
            f"TEI revision mismatch: expected {profile['revision']!r}, got {model_sha!r}"
        )
    runtime_version = str(info.get("version") or "")
    if runtime_version != profile["runtime_version"]:
        raise RuntimeError(
            "TEI runtime mismatch: "
            f"expected {profile['runtime_version']!r}, got {runtime_version!r}"
        )
    model_dtype = str(info.get("model_dtype") or "")
    if model_dtype != profile["dtype"]:
        raise RuntimeError(
            f"TEI dtype mismatch: expected {profile['dtype']!r}, got {model_dtype!r}"
        )
    model_type = info.get("model_type") or {}
    pooling = str(((model_type.get("embedding") or {}).get("pooling")) or "")
    if pooling != profile["pooling"]:
        raise RuntimeError(
            f"TEI pooling mismatch: expected {profile['pooling']!r}, got {pooling!r}"
        )
    probe = _tei_embed(
        ["BridgeGuard embedding readiness probe"],
        "query",
        profile,
        record_usage=False,
    )
    return {
        "provider": TEI,
        "model": model_id,
        "revision": model_sha,
        "runtime_version": runtime_version,
        "dimension": int(probe.shape[1]),
    }


def embed(texts, input_type: str = "passage") -> np.ndarray:
    """Embed text in the pinned Nomic vector space.

    Indexed content gets Nomic's ``search_document:`` prefix and questions get
    ``search_query:``. The distinction is part of the persisted profile.
    """
    input_type = str(input_type or "").strip().lower()
    if input_type not in {"passage", "query"}:
        raise ValueError("input_type must be 'passage' or 'query'")
    profile = embedding_profile()
    items = [str(t) if t is not None else "" for t in (texts or [])]
    if not items:
        return np.zeros((0, profile["dimension"]), dtype=np.float32)
    return _tei_embed(items, input_type, profile)


def embed_passages(texts) -> np.ndarray:
    return embed(texts, input_type="passage")


def embed_query(text: str) -> np.ndarray:
    return embed([text], input_type="query")
