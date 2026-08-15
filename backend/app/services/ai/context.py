"""Typed, owner-scoped lifecycle for AI work.

This registry is intentionally process-local.  It is safe for the current
single-worker deployment and makes that limitation observable in evidence;
multi-worker deployments must replace it with shared coordination before they
claim cross-worker cancellation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import math
import threading
import time
from typing import Callable, Mapping
from uuid import UUID

from ..bridge_identity import BridgeSnapshotIdentity
from ..operations.quotas import (
    DEFAULT_OPERATION_QUOTAS,
    OperationLease,
    OperationQuotaRegistry,
    QuotaExceeded,
)


AI_EVENT_SCHEMA_VERSION = "ai-event-v1"
MAX_AI_EVIDENCE_BYTES = 64 * 1024


class AiAnalysisMode(str, Enum):
    TEXT = "text"
    VISUAL = "visual"


class AiRequestStatus(str, Enum):
    ACTIVE = "active"
    CANCELLATION_REQUESTED = "cancellation_requested"
    COMPLETED = "completed"
    STOPPED = "stopped"
    ERROR = "error"


class AiRequestCancelled(RuntimeError):
    """Raised when one request's owned cancellation token is set."""


def canonical_request_id(value: str) -> str:
    text = str(value or "").strip().lower()
    try:
        parsed = UUID(text)
    except (ValueError, AttributeError) as exc:
        raise ValueError("request_id must be a canonical UUIDv4") from exc
    if parsed.version != 4 or str(parsed) != text:
        raise ValueError("request_id must be a canonical UUIDv4")
    return text


def canonical_optional_uuid(value: str | None, field_name: str) -> str | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip().lower()
    try:
        parsed = UUID(text)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a canonical UUID") from exc
    if str(parsed) != text:
        raise ValueError(f"{field_name} must be a canonical UUID")
    return text


def _required_token(value: object, field_name: str, *, maximum: int = 255) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or any(ord(ch) < 32 for ch in text):
        raise ValueError(f"{field_name} is invalid")
    return text


def _bounded_json_object(value: Mapping[str, object]) -> dict[str, object]:
    try:
        encoded = json.dumps(
            dict(value), ensure_ascii=True, allow_nan=False,
            separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("evidence must be a finite JSON object") from exc
    if len(encoded) > MAX_AI_EVIDENCE_BYTES:
        raise ValueError("evidence exceeds the bounded evidence size")
    return json.loads(encoded.decode("utf-8"))


@dataclass(frozen=True, slots=True)
class VisualAnalysisLimits:
    """Server-owned caps. Clients never select or increase these values."""

    max_images: int = 8
    max_estimated_input_tokens: int = 32_000
    max_estimated_cost_usd: float = 0.25

    def __post_init__(self) -> None:
        if not 1 <= self.max_images <= 32:
            raise ValueError("max_images must be between 1 and 32")
        if not 256 <= self.max_estimated_input_tokens <= 250_000:
            raise ValueError("max_estimated_input_tokens is invalid")
        if (
            not math.isfinite(self.max_estimated_cost_usd)
            or not 0 < self.max_estimated_cost_usd <= 25
        ):
            raise ValueError("max_estimated_cost_usd is invalid")


@dataclass(frozen=True, slots=True)
class AiRequestContext:
    request_id: str
    owner_id: str
    workspace_id: str
    conversation_id: str | None
    mode: AiAnalysisMode
    operation: str
    state_code: int | None
    bridge_number: str | None
    data_year: int | None
    bridge_identity: BridgeSnapshotIdentity | None
    deadline_monotonic: float
    cancellation_token: threading.Event = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", canonical_request_id(self.request_id))
        object.__setattr__(self, "owner_id", _required_token(self.owner_id.lower(), "owner_id"))
        object.__setattr__(self, "workspace_id", _required_token(self.workspace_id, "workspace_id"))
        object.__setattr__(
            self,
            "conversation_id",
            canonical_optional_uuid(self.conversation_id, "conversation_id"),
        )
        object.__setattr__(self, "mode", AiAnalysisMode(self.mode))
        object.__setattr__(self, "operation", _required_token(self.operation, "operation", maximum=64))
        if self.state_code is not None and not 1 <= int(self.state_code) <= 99:
            raise ValueError("state_code is invalid")
        if self.bridge_number is not None:
            object.__setattr__(
                self,
                "bridge_number",
                _required_token(self.bridge_number, "bridge_number", maximum=64),
            )
        if self.data_year is not None and not 1900 <= int(self.data_year) <= 2200:
            raise ValueError("data_year is invalid")
        if self.bridge_identity is not None:
            identity = self.bridge_identity
            if not isinstance(identity, BridgeSnapshotIdentity):
                raise ValueError("bridge_identity must be a resolved published identity")
            if (
                identity.workspace_id != self.workspace_id
                or identity.state_code != self.state_code
                or identity.bridge_number != self.bridge_number
                or identity.data_year != self.data_year
            ):
                raise ValueError("bridge_identity does not match request scope")
        if not math.isfinite(self.deadline_monotonic):
            raise ValueError("deadline is invalid")


@dataclass(slots=True)
class _RegistryEntry:
    context: AiRequestContext
    status: AiRequestStatus
    created_at_unix: float
    expires_at_monotonic: float
    evidence: dict[str, object]
    quota_lease: OperationLease


class AiRequestRegistry:
    """Bounded in-memory registry with non-enumerating owner checks."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 900,
        max_entries: int = 2048,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        quota_registry: OperationQuotaRegistry | None = None,
    ) -> None:
        if ttl_seconds < 30 or max_entries < 1:
            raise ValueError("registry bounds are invalid")
        self._ttl = float(ttl_seconds)
        self._max_entries = int(max_entries)
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        # Registries are isolated by default (including in focused tests).  The
        # production singleton below explicitly receives the shared process
        # quota registry so unrelated registry instances cannot leak capacity.
        self._quota_registry = quota_registry or OperationQuotaRegistry()
        self._lock = threading.RLock()
        self._entries: dict[str, _RegistryEntry] = {}

    def _purge_locked(self, now: float) -> None:
        for request_id in tuple(self._entries):
            if self._entries[request_id].expires_at_monotonic <= now:
                expired = self._entries.pop(request_id, None)
                if expired is not None:
                    self._quota_registry.release(expired.quota_lease)

    def register(
        self,
        *,
        request_id: str,
        owner_id: str,
        workspace_id: str,
        conversation_id: str | None,
        mode: AiAnalysisMode | str,
        operation: str,
        state_code: int | None = None,
        bridge_number: str | None = None,
        data_year: int | None = None,
        bridge_identity: BridgeSnapshotIdentity | None = None,
        timeout_seconds: float = 300,
    ) -> AiRequestContext:
        if not 1 <= float(timeout_seconds) <= 1800:
            raise ValueError("timeout_seconds is invalid")
        now = self._monotonic()
        context = AiRequestContext(
            request_id=request_id,
            owner_id=owner_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            mode=AiAnalysisMode(mode),
            operation=operation,
            state_code=state_code,
            bridge_number=bridge_number,
            data_year=data_year,
            bridge_identity=bridge_identity,
            deadline_monotonic=now + float(timeout_seconds),
            cancellation_token=threading.Event(),
        )
        quota_operation = (
            "visual" if context.mode is AiAnalysisMode.VISUAL
            else "rag" if (
                "rag" in context.operation.lower()
                or "report" in context.operation.lower()
                or context.operation.lower().startswith("document_")
            )
            else "ask"
        )
        with self._lock:
            self._purge_locked(now)
            if context.request_id in self._entries:
                raise ValueError("request_id is already registered")
            if len(self._entries) >= self._max_entries:
                raise QuotaExceeded("ai_request_capacity")
            quota_lease = self._quota_registry.acquire(
                request_id=context.request_id,
                workspace_id=context.workspace_id,
                owner_id=context.owner_id,
                operation=quota_operation,
            )
            self._entries[context.request_id] = _RegistryEntry(
                context=context,
                status=AiRequestStatus.ACTIVE,
                created_at_unix=self._wall_clock(),
                expires_at_monotonic=now + self._ttl,
                evidence={
                    "coordination": "single_worker_in_memory",
                    "event_schema_version": AI_EVENT_SCHEMA_VERSION,
                    "visual_processing_ran": False,
                },
                quota_lease=quota_lease,
            )
        return context

    def cancel(self, *, request_id: str, owner_id: str, workspace_id: str) -> bool:
        try:
            canonical = canonical_request_id(request_id)
        except ValueError:
            return False
        owner = str(owner_id or "").strip().lower()
        workspace = str(workspace_id or "").strip()
        now = self._monotonic()
        with self._lock:
            self._purge_locked(now)
            entry = self._entries.get(canonical)
            if (
                entry is None
                or entry.context.owner_id != owner
                or entry.context.workspace_id != workspace
                or entry.status is not AiRequestStatus.ACTIVE
            ):
                return False
            entry.status = AiRequestStatus.CANCELLATION_REQUESTED
            entry.context.cancellation_token.set()
            entry.evidence["cancellation_requested"] = True
            return True

    def update_evidence(
        self,
        context: AiRequestContext,
        evidence: Mapping[str, object],
    ) -> None:
        safe = _bounded_json_object(evidence)
        with self._lock:
            entry = self._entries.get(context.request_id)
            if entry is None or entry.context is not context:
                raise RuntimeError("AI request context is no longer registered")
            merged = dict(entry.evidence)
            merged.update(safe)
            entry.evidence = _bounded_json_object(merged)

    def finish(
        self,
        context: AiRequestContext,
        *,
        status: AiRequestStatus | str,
        evidence: Mapping[str, object] | None = None,
    ) -> None:
        terminal = AiRequestStatus(status)
        if (
            terminal is AiRequestStatus.COMPLETED
            and context.cancellation_token.is_set()
        ):
            # Cancellation wins the race with a provider's late success.  A
            # response that completed after the exact request was stopped must
            # not be reported as a successful owned operation.
            terminal = AiRequestStatus.STOPPED
        if terminal not in {
            AiRequestStatus.COMPLETED,
            AiRequestStatus.STOPPED,
            AiRequestStatus.ERROR,
        }:
            raise ValueError("finish requires a terminal status")
        with self._lock:
            entry = self._entries.get(context.request_id)
            if entry is None or entry.context is not context:
                return
            if entry.status in {
                AiRequestStatus.COMPLETED,
                AiRequestStatus.STOPPED,
                AiRequestStatus.ERROR,
            }:
                return
            if evidence:
                try:
                    merged = dict(entry.evidence)
                    merged.update(_bounded_json_object(evidence))
                    entry.evidence = _bounded_json_object(merged)
                except (TypeError, ValueError):
                    # Terminal cleanup is more important than optional
                    # evidence.  Keep a small, non-sensitive marker and always
                    # release the quota lease even if a provider returned an
                    # unexpectedly large or non-JSON value.
                    entry.evidence = {
                        "coordination": "single_worker_in_memory",
                        "event_schema_version": AI_EVENT_SCHEMA_VERSION,
                        "terminal_evidence_rejected": True,
                        "visual_processing_ran": False,
                    }
            if terminal is AiRequestStatus.STOPPED:
                # A transport disconnect or task cancellation is also an
                # exact request cancellation signal for any worker thread that
                # outlives the coroutine awaiting it.
                context.cancellation_token.set()
            entry.status = terminal
            entry.expires_at_monotonic = self._monotonic() + self._ttl
            self._quota_registry.release(entry.quota_lease)

    def evidence(
        self,
        *,
        request_id: str,
        owner_id: str,
        workspace_id: str,
    ) -> dict[str, object] | None:
        try:
            canonical = canonical_request_id(request_id)
        except ValueError:
            return None
        owner = str(owner_id or "").strip().lower()
        workspace = str(workspace_id or "").strip()
        now = self._monotonic()
        with self._lock:
            self._purge_locked(now)
            entry = self._entries.get(canonical)
            if (
                entry is None
                or entry.context.owner_id != owner
                or entry.context.workspace_id != workspace
            ):
                return None
            context = entry.context
            return {
                "event_schema_version": AI_EVENT_SCHEMA_VERSION,
                "request_id": context.request_id,
                "conversation_id": context.conversation_id,
                "operation": context.operation,
                "analysis_mode": context.mode.value,
                "status": entry.status.value,
                "state_code": context.state_code,
                "bridge_number": context.bridge_number,
                "data_year": context.data_year,
                "bridge_snapshot": (
                    context.bridge_identity.public_payload()
                    if context.bridge_identity is not None
                    else None
                ),
                "created_at_unix": entry.created_at_unix,
                "evidence": json.loads(json.dumps(entry.evidence)),
            }


def check_ai_request_cancelled(context: AiRequestContext) -> None:
    if context.cancellation_token.is_set():
        raise AiRequestCancelled("AI request cancelled")
    if time.monotonic() >= context.deadline_monotonic:
        context.cancellation_token.set()
        raise AiRequestCancelled("AI request deadline exceeded")


DEFAULT_AI_REQUEST_REGISTRY = AiRequestRegistry(
    quota_registry=DEFAULT_OPERATION_QUOTAS,
)


__all__ = [
    "AI_EVENT_SCHEMA_VERSION",
    "DEFAULT_AI_REQUEST_REGISTRY",
    "AiAnalysisMode",
    "AiRequestCancelled",
    "AiRequestContext",
    "AiRequestRegistry",
    "AiRequestStatus",
    "VisualAnalysisLimits",
    "canonical_optional_uuid",
    "canonical_request_id",
    "check_ai_request_cancelled",
]
