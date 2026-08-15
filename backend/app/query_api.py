"""Request-scoped HTTP protocol for Bridgette semantic queries.

This module owns only the authenticated HTTP boundary and NDJSON framing.  The
query service supplied by :mod:`app.main` owns semantic planning, SQL
validation, scoped compilation, bounded execution, evidence, and answer
generation.  Keeping the boundary small makes it possible to test identity,
scope, event order, and cancellation without a model provider or production
database.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
import hashlib
import json
import logging
import re
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .services.query.registry import (
    CancelOutcome,
    InvalidRequestId,
    RequestRegistry,
    validate_request_id,
)
from .services.query.sandbox import QueryCancelled
from .services.query.service import SemanticQueryCancelled
from .services.query.sql_ast import QueryScope
from .services.operations.quotas import (
    DEFAULT_OPERATION_QUOTAS,
    OperationLease,
    OperationQuotaRegistry,
    QuotaExceeded,
)


logger = logging.getLogger(__name__)


FULL_ROW_LIMIT = 200
SIDEBAR_ROW_LIMIT = 50
MAX_QUESTION_CHARACTERS = 16_000
MAX_RESULT_BYTES = 1 * 1024 * 1024
MAX_RESULT_ROWS = FULL_ROW_LIMIT
MAX_RESULT_COLUMNS = 40
MAX_STORED_RESULTS = 256
RESULT_TTL_SECONDS = 15 * 60
MAX_ACTIVE_PROVIDER_REQUESTS = 256
_FORM_FIELDS = frozenset(
    {"question", "model", "time_mode", "year", "start_year", "end_year"}
)
_EVIDENCE_FIELDS = frozenset(
    {
        "sql",
        "columns",
        "rows",
        "count",
        "truncated",
        "scope",
        "catalog_version",
        "schema_generation",
        "data_generation",
    }
)
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_SAFE_EVENT_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RESULT_MEDIA_TYPES = frozenset(
    {"text/csv", "application/json", "application/vnd.apache.arrow.file"}
)


@dataclass(frozen=True, slots=True)
class BoundedResult:
    """One request-owned, memory-only result artifact.

    These artifacts are deliberately short-lived and process-local. They are
    bounded copies of evidence already returned to the authenticated user, not
    an audit store or a substitute for tenant-scoped durable object storage.
    """

    request_id: str
    owner_id: str
    workspace_id: str
    content: bytes = field(repr=False)
    filename: str
    media_type: str
    row_count: int
    column_count: int
    truncated: bool
    sha256: str
    created_monotonic: float = field(repr=False, compare=False)

    def metadata(self) -> dict[str, object]:
        return {
            "available": True,
            "href": f"/api/bridgette/requests/{self.request_id}/result",
            "filename": self.filename,
            "media_type": self.media_type,
            "row_count": self.row_count,
            "column_count": self.column_count,
            "byte_count": len(self.content),
            "sha256": self.sha256,
            "truncated": self.truncated,
            "bounded": True,
        }


class BoundedResultStore:
    """Thread-safe, non-enumerable short-lived result store.

    Keys include the authenticated owner and workspace. Lookup intentionally
    returns ``None`` for missing, expired, cross-owner, and cross-workspace
    requests so the HTTP boundary cannot be used to enumerate request IDs.
    """

    def __init__(
        self,
        *,
        max_results: int = MAX_STORED_RESULTS,
        ttl_seconds: float = RESULT_TTL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(max_results, bool) or not 1 <= int(max_results) <= 1_024:
            raise ValueError("max_results must be between 1 and 1024")
        if isinstance(ttl_seconds, bool) or not 1 <= float(ttl_seconds) <= 3_600:
            raise ValueError("ttl_seconds must be between 1 and 3600")
        self._max_results = int(max_results)
        self._ttl_seconds = float(ttl_seconds)
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._results: dict[str, BoundedResult] = {}

    def _purge_locked(self, now: float) -> None:
        expired = [
            request_id
            for request_id, result in self._results.items()
            if now - result.created_monotonic >= self._ttl_seconds
        ]
        for request_id in expired:
            self._results.pop(request_id, None)

    def put(
        self,
        *,
        request_id: str,
        owner_id: str,
        workspace_id: str,
        content: bytes,
        filename: str,
        media_type: str,
        row_count: int,
        column_count: int,
        truncated: bool,
    ) -> BoundedResult:
        canonical_request_id = validate_request_id(request_id)
        owner = str(owner_id).strip().lower()
        workspace = str(workspace_id).strip()
        if not owner or not workspace:
            raise ValueError("owner_id and workspace_id are required")
        if not isinstance(content, bytes) or len(content) > MAX_RESULT_BYTES:
            raise ValueError("result content must be bounded bytes")
        if not content:
            raise ValueError("result content cannot be empty")
        if (
            isinstance(row_count, bool)
            or not 0 <= int(row_count) <= MAX_RESULT_ROWS
            or isinstance(column_count, bool)
            or not 1 <= int(column_count) <= MAX_RESULT_COLUMNS
        ):
            raise ValueError("result shape exceeds the bounded evidence contract")
        media = str(media_type).strip().lower()
        if media not in _RESULT_MEDIA_TYPES:
            raise ValueError("result media type is not approved")
        supplied_name = str(filename).strip()
        if not _SAFE_FILENAME.fullmatch(supplied_name):
            raise ValueError("result filename is invalid")
        digest = hashlib.sha256(content).hexdigest()
        suffix = ".csv" if media == "text/csv" else ".json" if media == "application/json" else ".arrow"
        # The store, rather than a provider/model caller, owns the public name.
        public_name = f"bridgette-{digest}{suffix}"
        now = self._monotonic()
        with self._lock:
            self._purge_locked(now)
            existing = self._results.get(canonical_request_id)
            if existing is not None:
                same_payload = (
                    existing.owner_id == owner
                    and existing.workspace_id == workspace
                    and existing.content == content
                    and existing.media_type == media
                    and existing.row_count == int(row_count)
                    and existing.column_count == int(column_count)
                    and existing.truncated == bool(truncated)
                )
                if not same_payload:
                    raise ValueError("request_id already owns a different result")
                return existing
            while len(self._results) >= self._max_results:
                oldest = min(
                    self._results.values(),
                    key=lambda item: (item.created_monotonic, item.request_id),
                )
                self._results.pop(oldest.request_id, None)
            result = BoundedResult(
                request_id=canonical_request_id,
                owner_id=owner,
                workspace_id=workspace,
                content=content,
                filename=public_name,
                media_type=media,
                row_count=int(row_count),
                column_count=int(column_count),
                truncated=bool(truncated),
                sha256=digest,
                created_monotonic=now,
            )
            self._results[canonical_request_id] = result
        return result

    def get(
        self,
        *,
        request_id: str,
        owner_id: str,
        workspace_id: str,
    ) -> BoundedResult | None:
        try:
            canonical_request_id = validate_request_id(request_id)
        except InvalidRequestId:
            return None
        owner = str(owner_id).strip().lower()
        workspace = str(workspace_id).strip()
        now = self._monotonic()
        with self._lock:
            self._purge_locked(now)
            result = self._results.get(canonical_request_id)
            if (
                result is None
                or result.owner_id != owner
                or result.workspace_id != workspace
            ):
                return None
            return result


@dataclass(frozen=True, slots=True)
class BridgetteStreamRequest:
    """Trusted request passed from the HTTP adapter to the query service."""

    request_id: str
    question: str
    model: str
    surface: str
    row_limit: int
    actor_id: str
    actor_role: str
    scope: QueryScope
    cancellation_token: threading.Event = field(repr=False, compare=False)


@dataclass(slots=True)
class _ProviderRequest:
    request_id: str
    actor_id: str
    workspace_id: str
    quota_lease: OperationLease
    cancellation_token: threading.Event = field(default_factory=threading.Event)


class ProviderRequestRegistry:
    """Own cancellation across SQL generation and answer generation.

    SQLite has its own connection-aware registry.  This separate registry is
    intentionally held for the full HTTP stream so the same cancel endpoint
    also signals provider loops without pre-registering (and colliding with)
    the SQLite executor's request ID.
    """

    def __init__(
        self,
        *,
        max_active_requests: int = MAX_ACTIVE_PROVIDER_REQUESTS,
        quota_registry: OperationQuotaRegistry | None = None,
    ) -> None:
        if (
            isinstance(max_active_requests, bool)
            or not 1 <= int(max_active_requests) <= 4_096
        ):
            raise ValueError("max_active_requests must be between 1 and 4096")
        self._max_active_requests = int(max_active_requests)
        # Focused registries are isolated by default.  The production singleton
        # below explicitly shares the process-wide operation quota registry.
        self._quota_registry = quota_registry or OperationQuotaRegistry()
        self._lock = threading.RLock()
        self._requests: dict[str, _ProviderRequest] = {}

    def register(
        self,
        *,
        request_id: str,
        actor_id: str,
        workspace_id: str,
    ) -> _ProviderRequest:
        validate_request_id(request_id)
        actor = str(actor_id).strip().lower()
        workspace = str(workspace_id).strip()
        if not actor or not workspace:
            raise ValueError("actor and workspace are required")
        with self._lock:
            if request_id in self._requests:
                raise ValueError("request_id is already active")
            if len(self._requests) >= self._max_active_requests:
                raise QuotaExceeded("bridgette_request_capacity")
            quota_lease = self._quota_registry.acquire(
                # OperationQuotaRegistry deliberately treats repeat claims for
                # one logical operation as idempotent.  Namespace this HTTP
                # boundary so reusing a public UUID on a legacy Ask endpoint
                # cannot alias (and prematurely release) Bridgette's lease.
                request_id=f"bridgette:{request_id}",
                workspace_id=workspace,
                owner_id=actor,
                operation="ask",
            )
            entry = _ProviderRequest(
                request_id=request_id,
                actor_id=actor,
                workspace_id=workspace,
                quota_lease=quota_lease,
            )
            self._requests[request_id] = entry
        return entry

    def cancel(
        self,
        *,
        request_id: str,
        actor_id: str,
        workspace_id: str,
    ) -> bool:
        try:
            validate_request_id(request_id)
        except InvalidRequestId:
            return False
        with self._lock:
            entry = self._requests.get(request_id)
            if (
                entry is None
                or entry.actor_id != str(actor_id).strip().lower()
                or entry.workspace_id != str(workspace_id).strip()
            ):
                return False
            entry.cancellation_token.set()
            return True

    def finish(self, request_id: str) -> bool:
        with self._lock:
            entry = self._requests.pop(request_id, None)
        if entry is None:
            return False
        self._quota_registry.release(entry.quota_lease)
        return True


DEFAULT_PROVIDER_REQUEST_REGISTRY = ProviderRequestRegistry(
    quota_registry=DEFAULT_OPERATION_QUOTAS,
)
DEFAULT_BOUNDED_RESULT_STORE = BoundedResultStore()


StreamHandler = Callable[
    [BridgetteStreamRequest],
    Iterator[Mapping[str, object]],
]
StreamHandlerGetter = Callable[[], StreamHandler]
RegistryGetter = Callable[[], RequestRegistry]


def _unprocessable(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=message)


def _one_form_value(form: Any, name: str) -> str | None:
    values = form.getlist(name)
    if len(values) > 1:
        raise _unprocessable(f"{name} must be supplied at most once")
    if not values:
        return None
    value = values[0]
    if not isinstance(value, str):
        raise _unprocessable(f"{name} must be text")
    return value


def _optional_year(form: Any, name: str) -> int | None:
    raw = _one_form_value(form, name)
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    if not text.isascii() or not text.isdecimal():
        raise _unprocessable(f"{name} must be an integer year")
    return int(text)


def _request_id(request: Request) -> str:
    values = request.headers.getlist("X-Request-ID")
    if len(values) != 1:
        raise _unprocessable("X-Request-ID is required exactly once")
    try:
        return validate_request_id(values[0])
    except InvalidRequestId as exc:
        raise _unprocessable("X-Request-ID must be a canonical UUIDv4") from exc


async def _trusted_request(
    request: Request,
    *,
    surface: str,
    row_limit: int,
    workspace_id: str,
    state_code: int,
    provider_registry: ProviderRequestRegistry,
) -> tuple[BridgetteStreamRequest, _ProviderRequest]:
    request_id = _request_id(request)
    form = await request.form()
    supplied = {str(key) for key, _value in form.multi_items()}
    unexpected = sorted(supplied.difference(_FORM_FIELDS))
    if unexpected:
        raise _unprocessable(
            "Unsupported form fields: " + ", ".join(unexpected)
        )

    question_raw = _one_form_value(form, "question")
    question = (question_raw or "").strip()
    if not question:
        raise _unprocessable("question is required")
    if len(question) > MAX_QUESTION_CHARACTERS:
        raise _unprocessable(
            f"question may contain at most {MAX_QUESTION_CHARACTERS} characters"
        )

    time_mode = (_one_form_value(form, "time_mode") or "latest").strip().lower()
    try:
        scope = QueryScope(
            workspace_id=workspace_id,
            state_code=state_code,
            time_mode=time_mode,
            data_year=_optional_year(form, "year"),
            start_year=_optional_year(form, "start_year"),
            end_year=_optional_year(form, "end_year"),
        )
    except ValueError as exc:
        raise _unprocessable(str(exc)) from exc

    user = getattr(request.state, "user", None)
    actor_id = str(getattr(user, "email", "")).strip().lower()
    actor_role = str(getattr(user, "role", "")).strip().lower()
    if not actor_id or not actor_role:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        provider_entry = provider_registry.register(
            request_id=request_id,
            actor_id=actor_id,
            workspace_id=workspace_id,
        )
    except QuotaExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail="Too many active Bridgette requests. Try again shortly.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail="A request with this ID is already active",
        ) from exc

    model = (_one_form_value(form, "model") or "").strip()
    return (
        BridgetteStreamRequest(
            request_id=request_id,
            question=question,
            model=model,
            surface=surface,
            row_limit=row_limit,
            actor_id=actor_id,
            actor_role=actor_role,
            scope=scope,
            cancellation_token=provider_entry.cancellation_token,
        ),
        provider_entry,
    )


def _ndjson(event: Mapping[str, object]) -> str:
    return json.dumps(
        dict(event),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ) + "\n"


def _service_event(
    event: Mapping[str, object],
    *,
    request_id: str,
) -> dict[str, object]:
    if not isinstance(event, Mapping):
        raise RuntimeError("Bridgette emitted an invalid event")
    event_type = str(event.get("type", "")).strip().lower()
    if event_type == "evidence":
        missing = sorted(_EVIDENCE_FIELDS.difference(event))
        if missing:
            raise RuntimeError("Bridgette evidence is incomplete")
        rows = event.get("rows")
        columns = event.get("columns")
        count = event.get("count")
        if not isinstance(rows, (list, tuple)) or not isinstance(
            columns, (list, tuple)
        ):
            raise RuntimeError("Bridgette evidence rows and columns are invalid")
        if isinstance(count, bool) or not isinstance(count, int) or count != len(rows):
            raise RuntimeError("Bridgette evidence count is invalid")
        normalized = dict(event)
        normalized["request_id"] = request_id
        normalized["type"] = "evidence"
        return normalized
    if event_type == "token":
        content = event.get("content", event.get("text"))
        if not isinstance(content, str) or not content:
            raise RuntimeError("Bridgette emitted an empty token")
        return {
            "type": "token",
            "request_id": request_id,
            "content": content,
        }
    if event_type in {"clarification", "abstention"}:
        code = event.get("code")
        message = event.get("message")
        if not isinstance(code, str) or not _SAFE_EVENT_CODE.fullmatch(code):
            raise RuntimeError("Bridgette emitted an invalid decision code")
        if not isinstance(message, str) or not message.strip():
            raise RuntimeError("Bridgette emitted an invalid decision message")
        normalized = dict(event)
        normalized["type"] = event_type
        normalized["request_id"] = request_id
        return normalized
    # The adapter, not a provider integration, owns terminal lifecycle events.
    raise RuntimeError("Bridgette emitted an unsupported protocol event")


def _stream_events(
    stream_request: BridgetteStreamRequest,
    *,
    stream_handler_getter: StreamHandlerGetter,
    provider_registry: ProviderRequestRegistry,
) -> Iterator[str]:
    request_id = stream_request.request_id
    evidence_seen = False
    token_seen = False
    decision_seen = False
    try:
        # Keep even the first yield inside the lifecycle guard.  Closing the
        # iterator after only the accepted event (for example, on an HTTP
        # disconnect) must still release the request's quota lease.
        yield _ndjson(
            {
                "type": "accepted",
                "request_id": request_id,
                "surface": stream_request.surface,
                "row_limit": stream_request.row_limit,
            }
        )
        for raw_event in stream_handler_getter()(stream_request):
            if stream_request.cancellation_token.is_set():
                raise QueryCancelled("request cancelled")
            event = _service_event(raw_event, request_id=request_id)
            if event["type"] == "evidence":
                if evidence_seen or token_seen or decision_seen:
                    raise RuntimeError("Bridgette evidence arrived out of order")
                evidence_seen = True
            elif event["type"] == "token":
                if not evidence_seen:
                    raise RuntimeError("Bridgette token arrived before evidence")
                token_seen = True
            else:
                if evidence_seen or token_seen or decision_seen:
                    raise RuntimeError("Bridgette decision arrived out of order")
                decision_seen = True
            yield _ndjson(event)
        if stream_request.cancellation_token.is_set():
            raise QueryCancelled("request cancelled")
        if decision_seen:
            yield _ndjson({"type": "done", "request_id": request_id})
            return
        if not evidence_seen or not token_seen:
            raise RuntimeError("Bridgette did not produce a complete response")
        yield _ndjson({"type": "done", "request_id": request_id})
    except (QueryCancelled, SemanticQueryCancelled):
        yield _ndjson({"type": "stopped", "request_id": request_id})
    except Exception as exc:
        if stream_request.cancellation_token.is_set():
            # The semantic service may wrap a provider-loop cancellation as a
            # typed generation failure after recording its terminal audit row.
            # The request-owned token remains the authoritative HTTP outcome.
            yield _ndjson({"type": "stopped", "request_id": request_id})
            return
        logger.error(
            "Bridgette request failed request_id=%s exception_type=%s",
            request_id,
            type(exc).__name__[:80],
        )
        # Do not expose provider messages, SQL parser internals, database paths,
        # or tenant-specific values through a terminal error event.
        yield _ndjson(
            {
                "type": "error",
                "request_id": request_id,
                "detail": "Bridgette could not complete this request.",
            }
        )
    finally:
        provider_registry.finish(request_id)


def create_bridgette_router(
    *,
    stream_handler_getter: StreamHandlerGetter,
    registry_getter: RegistryGetter,
    workspace_id: str,
    state_code: int = 34,
    provider_registry: ProviderRequestRegistry = DEFAULT_PROVIDER_REQUEST_REGISTRY,
    result_store_getter: Callable[[], BoundedResultStore] = (
        lambda: DEFAULT_BOUNDED_RESULT_STORE
    ),
) -> APIRouter:
    """Create the production router with late-bound, testable dependencies."""

    router = APIRouter(tags=["bridgette"])

    async def stream(request: Request, *, surface: str, row_limit: int):
        stream_request, _provider_entry = await _trusted_request(
            request,
            surface=surface,
            row_limit=row_limit,
            workspace_id=workspace_id,
            state_code=state_code,
            provider_registry=provider_registry,
        )
        try:
            return StreamingResponse(
                _stream_events(
                    stream_request,
                    stream_handler_getter=stream_handler_getter,
                    provider_registry=provider_registry,
                ),
                media_type="application/x-ndjson",
                headers={
                    "X-Request-ID": stream_request.request_id,
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        except Exception:
            # Construction is normally side-effect free, but admission has
            # already acquired capacity, so fail closed if the response object
            # cannot be created.
            provider_registry.finish(stream_request.request_id)
            raise

    @router.post("/api/bridgette/stream")
    async def full_stream(request: Request):
        return await stream(request, surface="full", row_limit=FULL_ROW_LIMIT)

    @router.post("/api/bridgette/sidebar/stream")
    async def sidebar_stream(request: Request):
        return await stream(
            request,
            surface="sidebar",
            row_limit=SIDEBAR_ROW_LIMIT,
        )

    @router.post("/api/bridgette/requests/{request_id}/cancel")
    def cancel(request_id: str, request: Request):
        user = getattr(request.state, "user", None)
        actor_id = str(getattr(user, "email", "")).strip().lower()
        provider_cancelled = provider_registry.cancel(
            request_id=request_id,
            actor_id=actor_id,
            workspace_id=workspace_id,
        )
        sqlite_outcome = registry_getter().cancel(
            request_id=request_id,
            owner_id=actor_id,
            workspace_id=workspace_id,
        )
        if provider_cancelled or sqlite_outcome is CancelOutcome.CANCELLED:
            return JSONResponse(
                status_code=202,
                content={
                    "request_id": request_id,
                    "status": "cancellation_requested",
                },
            )
        # Absent, malformed, completed, and cross-owner IDs are deliberately
        # indistinguishable to prevent request enumeration.
        raise HTTPException(status_code=404, detail="Request not found")

    @router.get("/api/bridgette/requests/{request_id}/result")
    def download_result(request_id: str, request: Request):
        user = getattr(request.state, "user", None)
        actor_id = str(getattr(user, "email", "")).strip().lower()
        result = result_store_getter().get(
            request_id=request_id,
            owner_id=actor_id,
            workspace_id=workspace_id,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="Result not found")
        return Response(
            content=result.content,
            media_type=result.media_type,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Request-ID": result.request_id,
                "X-Content-SHA256": result.sha256,
                "Content-Disposition": f'attachment; filename="{result.filename}"',
            },
        )

    return router


__all__ = [
    "BoundedResult",
    "BoundedResultStore",
    "BridgetteStreamRequest",
    "DEFAULT_BOUNDED_RESULT_STORE",
    "DEFAULT_PROVIDER_REQUEST_REGISTRY",
    "ProviderRequestRegistry",
    "FULL_ROW_LIMIT",
    "MAX_QUESTION_CHARACTERS",
    "MAX_ACTIVE_PROVIDER_REQUESTS",
    "SIDEBAR_ROW_LIMIT",
    "create_bridgette_router",
]
