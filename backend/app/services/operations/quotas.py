"""Bounded, owner/workspace-scoped concurrent operation quotas.

This in-memory implementation matches BridgeGuard's supported single-worker
deployment.  A multi-worker deployment must replace it with durable shared
coordination; the deployment gate prevents claiming otherwise.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import threading
import time
from typing import Iterator, Mapping


class QuotaExceeded(RuntimeError):
    def __init__(self, code: str, retry_after_seconds: int = 1):
        self.code = code
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class OperationLease:
    request_id: str
    workspace_id: str
    owner_id: str
    operation: str
    acquired_at: float


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    active_total: int
    active_for_workspace: int
    active_for_owner: int
    active_for_operation: int


DEFAULT_OPERATION_LIMITS: Mapping[str, int] = {
    "ask": 2,
    "rag": 2,
    "visual": 1,
    "conversion": 1,
    "validation": 1,
    "report_index": 1,
    "point_cloud": 1,
}


class OperationQuotaRegistry:
    def __init__(
        self,
        *,
        operation_limits: Mapping[str, int] = DEFAULT_OPERATION_LIMITS,
        workspace_limit: int = 8,
        owner_limit: int = 3,
        maximum_leases: int = 256,
        monotonic=time.monotonic,
    ) -> None:
        if workspace_limit < 1 or owner_limit < 1 or maximum_leases < 1:
            raise ValueError("quota limits must be positive")
        limits = {str(key): int(value) for key, value in operation_limits.items()}
        if not limits or any(not key or value < 1 for key, value in limits.items()):
            raise ValueError("operation limits are invalid")
        self._limits = limits
        self._workspace_limit = int(workspace_limit)
        self._owner_limit = int(owner_limit)
        self._maximum_leases = int(maximum_leases)
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._leases: dict[str, OperationLease] = {}

    def acquire(
        self,
        *,
        request_id: str,
        workspace_id: str,
        owner_id: str,
        operation: str,
    ) -> OperationLease:
        request = str(request_id or "").strip()
        workspace = str(workspace_id or "").strip()
        owner = str(owner_id or "").strip().lower()
        kind = str(operation or "").strip()
        if not request or not workspace or not owner or kind not in self._limits:
            raise ValueError("quota identity or operation is invalid")
        with self._lock:
            existing = self._leases.get(request)
            if existing is not None:
                if (
                    existing.workspace_id == workspace
                    and existing.owner_id == owner
                    and existing.operation == kind
                ):
                    return existing
                raise QuotaExceeded("request_id_in_use")
            if len(self._leases) >= self._maximum_leases:
                raise QuotaExceeded("global_operation_capacity")
            workspace_count = sum(
                lease.workspace_id == workspace for lease in self._leases.values()
            )
            owner_count = sum(
                lease.workspace_id == workspace and lease.owner_id == owner
                for lease in self._leases.values()
            )
            operation_count = sum(
                lease.workspace_id == workspace and lease.operation == kind
                for lease in self._leases.values()
            )
            if workspace_count >= self._workspace_limit:
                raise QuotaExceeded("workspace_concurrency_limit")
            if owner_count >= self._owner_limit:
                raise QuotaExceeded("owner_concurrency_limit")
            if operation_count >= self._limits[kind]:
                raise QuotaExceeded(f"{kind}_concurrency_limit")
            lease = OperationLease(
                request_id=request,
                workspace_id=workspace,
                owner_id=owner,
                operation=kind,
                acquired_at=float(self._monotonic()),
            )
            self._leases[request] = lease
            return lease

    def release(self, lease: OperationLease) -> None:
        with self._lock:
            current = self._leases.get(lease.request_id)
            if current == lease:
                self._leases.pop(lease.request_id, None)

    def snapshot(self, *, workspace_id: str, owner_id: str, operation: str) -> QuotaSnapshot:
        workspace = str(workspace_id).strip()
        owner = str(owner_id).strip().lower()
        with self._lock:
            values = tuple(self._leases.values())
        return QuotaSnapshot(
            active_total=len(values),
            active_for_workspace=sum(item.workspace_id == workspace for item in values),
            active_for_owner=sum(
                item.workspace_id == workspace and item.owner_id == owner for item in values
            ),
            active_for_operation=sum(
                item.workspace_id == workspace and item.operation == operation for item in values
            ),
        )

    @contextmanager
    def claim(self, **identity: str) -> Iterator[OperationLease]:
        lease = self.acquire(**identity)
        try:
            yield lease
        finally:
            self.release(lease)


DEFAULT_OPERATION_QUOTAS = OperationQuotaRegistry()


__all__ = [
    "DEFAULT_OPERATION_LIMITS",
    "DEFAULT_OPERATION_QUOTAS",
    "OperationLease",
    "OperationQuotaRegistry",
    "QuotaExceeded",
    "QuotaSnapshot",
]
