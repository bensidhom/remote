"""Request-owned cancellation for bounded query execution.

The registry is deliberately in-memory and process-local.  It provides correct
ownership and isolation for the current single-worker deployment, but does not
pretend that an ``interrupt()`` can cross worker processes.  A shared job store
is required before BridgeGuard is run with multiple workers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import sqlite3
import threading
import time
from typing import Callable
from uuid import UUID, uuid4


Clock = Callable[[], float]


class RequestStatus(StrEnum):
    """Lifecycle states retained while a request is active."""

    RUNNING = "running"
    CANCELLING = "cancelling"


class CancelOutcome(StrEnum):
    """Non-enumerating response from an owner-scoped cancel operation."""

    CANCELLED = "cancelled"
    NOT_FOUND = "not_found"


class RequestRegistryError(RuntimeError):
    """Base class for programming/configuration errors in the registry."""


class InvalidRequestId(RequestRegistryError):
    """Raised when a new request ID is not a canonical UUIDv4 string."""


class RequestAlreadyRegistered(RequestRegistryError):
    """Raised if an active request attempts to reuse an existing ID."""


class RequestNotRegistered(RequestRegistryError):
    """Raised by internal attachment operations for an unknown request."""


def new_request_id() -> str:
    """Return an unguessable, canonical request identifier."""

    return str(uuid4())


def validate_request_id(request_id: str) -> str:
    """Validate and normalize a canonical UUIDv4 request ID.

    Accepting only the canonical lower-case representation prevents multiple
    textual aliases from addressing the same registry entry.  UUIDv4 is used
    because these IDs are bearer-visible and must not be sequential.
    """

    if not isinstance(request_id, str):
        raise InvalidRequestId("request_id must be a canonical UUIDv4 string")
    try:
        parsed = UUID(request_id)
    except (ValueError, AttributeError) as exc:
        raise InvalidRequestId("request_id must be a canonical UUIDv4 string") from exc
    if parsed.version != 4 or str(parsed) != request_id:
        raise InvalidRequestId("request_id must be a canonical UUIDv4 string")
    return request_id


def _identity(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


@dataclass(slots=True)
class RequestEntry:
    """Mutable state for exactly one in-flight request."""

    request_id: str
    owner_id: str
    workspace_id: str
    operation: str
    deadline: float
    expires_at: float
    cancellation_token: threading.Event = field(default_factory=threading.Event)
    status: RequestStatus = RequestStatus.RUNNING
    connection: sqlite3.Connection | None = field(default=None, repr=False)

    @property
    def cancelled(self) -> bool:
        return self.cancellation_token.is_set()


class RequestRegistry:
    """Thread-safe registry for request-specific SQLite interruption.

    Cancellation deliberately returns the same ``NOT_FOUND`` outcome when an ID
    is absent, malformed, expired, or owned by another actor/workspace.  This
    makes the endpoint non-enumerating.
    """

    def __init__(self, *, clock: Clock = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: dict[str, RequestEntry] = {}

    def register(
        self,
        *,
        request_id: str,
        owner_id: str,
        workspace_id: str,
        operation: str,
        deadline: float,
        retention_seconds: float = 30.0,
    ) -> RequestEntry:
        """Register one in-flight operation before opening its connection."""

        key = validate_request_id(request_id)
        owner = _identity(owner_id, "owner_id")
        workspace = _identity(workspace_id, "workspace_id")
        operation_name = _identity(operation, "operation")
        now = self._clock()
        if deadline <= now:
            raise ValueError("deadline must be in the future")
        if retention_seconds < 0:
            raise ValueError("retention_seconds cannot be negative")

        with self._lock:
            self._prune_locked(now)
            if key in self._entries:
                raise RequestAlreadyRegistered("request_id is already active")
            entry = RequestEntry(
                request_id=key,
                owner_id=owner,
                workspace_id=workspace,
                operation=operation_name,
                deadline=deadline,
                expires_at=deadline + retention_seconds,
            )
            self._entries[key] = entry
            return entry

    def attach_connection(
        self,
        request_id: str,
        connection: sqlite3.Connection,
    ) -> None:
        """Attach the sole SQLite connection for a registered request."""

        key = validate_request_id(request_id)
        interrupt_now = False
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                raise RequestNotRegistered("request is no longer active")
            if entry.connection is not None and entry.connection is not connection:
                raise RequestRegistryError(
                    "a request may have only one attached SQLite connection"
                )
            entry.connection = connection
            interrupt_now = entry.cancelled
        # sqlite3.Connection.interrupt() is explicitly safe from another thread.
        # Keep it outside the registry lock in case the driver blocks briefly.
        if interrupt_now:
            try:
                connection.interrupt()
            except sqlite3.Error:
                # The executor may have completed and closed between attachment
                # lookup and interruption. The cancellation token is still set.
                pass

    def cancel(
        self,
        *,
        request_id: str,
        owner_id: str,
        workspace_id: str,
    ) -> CancelOutcome:
        """Cancel only a request owned by the exact actor and workspace."""

        try:
            key = validate_request_id(request_id)
        except InvalidRequestId:
            return CancelOutcome.NOT_FOUND
        owner = owner_id.strip() if isinstance(owner_id, str) else ""
        workspace = workspace_id.strip() if isinstance(workspace_id, str) else ""
        connection: sqlite3.Connection | None = None
        with self._lock:
            self._prune_locked(self._clock())
            entry = self._entries.get(key)
            if (
                entry is None
                or not owner
                or not workspace
                or entry.owner_id != owner
                or entry.workspace_id != workspace
            ):
                return CancelOutcome.NOT_FOUND
            entry.cancellation_token.set()
            entry.status = RequestStatus.CANCELLING
            connection = entry.connection

        if connection is not None:
            try:
                connection.interrupt()
            except sqlite3.Error:
                # A simultaneous deterministic cleanup is indistinguishable
                # from a request that finished just before cancellation.
                pass
        return CancelOutcome.CANCELLED

    def finish(self, request_id: str) -> bool:
        """Atomically seal/remove a request.

        The return value is true only when the request was still registered and
        had not been cancelled. Executors can call this immediately before
        returning a result: either cancellation wins and no result is returned,
        or sealing wins and a later cancel sees the same non-enumerating
        ``NOT_FOUND`` response.
        """

        try:
            key = validate_request_id(request_id)
        except InvalidRequestId:
            return False
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is not None:
                entry.connection = None
                return not entry.cancelled
            return False

    def is_active(self, request_id: str) -> bool:
        """Return active state for internal orchestration/tests only."""

        try:
            key = validate_request_id(request_id)
        except InvalidRequestId:
            return False
        with self._lock:
            self._prune_locked(self._clock())
            return key in self._entries

    @property
    def active_count(self) -> int:
        with self._lock:
            self._prune_locked(self._clock())
            return len(self._entries)

    def clear(self) -> None:
        """Cancel and remove every request (test/process-shutdown helper)."""

        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            entry.cancellation_token.set()
            if entry.connection is not None:
                try:
                    entry.connection.interrupt()
                except sqlite3.Error:
                    pass
                entry.connection = None

    def _prune_locked(self, now: float) -> None:
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            entry = self._entries.pop(key)
            entry.cancellation_token.set()
            connection = entry.connection
            entry.connection = None
            if connection is not None:
                try:
                    connection.interrupt()
                except sqlite3.Error:
                    pass


DEFAULT_REQUEST_REGISTRY = RequestRegistry()


__all__ = [
    "CancelOutcome",
    "DEFAULT_REQUEST_REGISTRY",
    "InvalidRequestId",
    "RequestAlreadyRegistered",
    "RequestEntry",
    "RequestNotRegistered",
    "RequestRegistry",
    "RequestRegistryError",
    "RequestStatus",
    "new_request_id",
    "validate_request_id",
]
