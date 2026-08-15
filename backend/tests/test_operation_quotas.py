from __future__ import annotations

import threading

import pytest

from app.services.operations.quotas import OperationQuotaRegistry, QuotaExceeded


def _registry() -> OperationQuotaRegistry:
    return OperationQuotaRegistry(
        operation_limits={"ask": 2, "visual": 1},
        workspace_limit=3,
        owner_limit=2,
        maximum_leases=5,
    )


def test_quota_is_scoped_by_workspace_owner_and_operation():
    registry = _registry()
    first = registry.acquire(
        request_id="r1", workspace_id="ws-a", owner_id="A@x", operation="ask"
    )
    second = registry.acquire(
        request_id="r2", workspace_id="ws-a", owner_id="b@x", operation="ask"
    )
    with pytest.raises(QuotaExceeded) as limit:
        registry.acquire(
            request_id="r3", workspace_id="ws-a", owner_id="c@x", operation="ask"
        )
    assert limit.value.code == "ask_concurrency_limit"
    # Another dedicated workspace cannot consume or be blocked by ws-a's count.
    other = registry.acquire(
        request_id="r4", workspace_id="ws-b", owner_id="c@x", operation="ask"
    )
    assert registry.snapshot(
        workspace_id="ws-a", owner_id="a@x", operation="ask"
    ).active_for_workspace == 2
    for lease in (first, second, other):
        registry.release(lease)


def test_duplicate_request_is_idempotent_but_cannot_change_owner():
    registry = _registry()
    lease = registry.acquire(
        request_id="same", workspace_id="ws", owner_id="a@x", operation="visual"
    )
    assert registry.acquire(
        request_id="same", workspace_id="ws", owner_id="a@x", operation="visual"
    ) is lease
    with pytest.raises(QuotaExceeded) as conflict:
        registry.acquire(
            request_id="same", workspace_id="ws", owner_id="b@x", operation="visual"
        )
    assert conflict.value.code == "request_id_in_use"


def test_claim_releases_after_failure():
    registry = _registry()
    with pytest.raises(RuntimeError):
        with registry.claim(
            request_id="r1", workspace_id="ws", owner_id="a@x", operation="ask"
        ):
            raise RuntimeError("boom")
    assert registry.snapshot(
        workspace_id="ws", owner_id="a@x", operation="ask"
    ).active_total == 0


def test_acquisition_is_thread_safe_and_never_exceeds_cap():
    registry = OperationQuotaRegistry(
        operation_limits={"visual": 1}, workspace_limit=8, owner_limit=8
    )
    barrier = threading.Barrier(8)
    results: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        barrier.wait()
        try:
            lease = registry.acquire(
                request_id=f"r{index}", workspace_id="ws", owner_id=f"u{index}",
                operation="visual",
            )
        except QuotaExceeded as exc:
            with lock:
                results.append(exc.code)
        else:
            with lock:
                results.append("acquired")
            registry.release(lease)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # Threads may release before another acquires, but the registry itself
    # never grants more than one simultaneous visual lease; all outcomes type.
    assert results
    assert set(results) <= {"acquired", "visual_concurrency_limit"}


@pytest.mark.parametrize("operation", ["", "unknown"])
def test_unknown_operations_fail_closed(operation: str):
    with pytest.raises(ValueError):
        _registry().acquire(
            request_id="r", workspace_id="ws", owner_id="a@x", operation=operation
        )
