from __future__ import annotations

import hashlib

import pytest

from app.query_api import BoundedResultStore


REQUEST_ID = "11111111-1111-4111-8111-111111111111"


def _put(store: BoundedResultStore, *, content: bytes = b"a,b\r\n1,2\r\n"):
    return store.put(
        request_id=REQUEST_ID,
        owner_id="Owner@Example.com",
        workspace_id="workspace-a",
        content=content,
        filename="ignored-provider-name.csv",
        media_type="text/csv",
        row_count=1,
        column_count=2,
        truncated=False,
    )


def test_result_store_is_hash_addressed_idempotent_and_path_free():
    store = BoundedResultStore()
    first = _put(store)
    second = _put(store)

    digest = hashlib.sha256(first.content).hexdigest()
    assert first is second
    assert first.filename == f"bridgette-{digest}.csv"
    metadata = first.metadata()
    assert metadata["sha256"] == digest
    assert metadata["byte_count"] == len(first.content)
    assert "storage" not in metadata and "path" not in metadata
    assert "C:\\" not in str(metadata)


def test_result_lookup_is_non_enumerable_across_owner_workspace_and_malformed_id():
    store = BoundedResultStore()
    expected = _put(store)

    assert store.get(
        request_id=REQUEST_ID,
        owner_id="owner@example.com",
        workspace_id="workspace-a",
    ) == expected
    assert store.get(
        request_id=REQUEST_ID,
        owner_id="other@example.com",
        workspace_id="workspace-a",
    ) is None
    assert store.get(
        request_id=REQUEST_ID,
        owner_id="owner@example.com",
        workspace_id="workspace-b",
    ) is None
    assert store.get(
        request_id="not-a-uuid",
        owner_id="owner@example.com",
        workspace_id="workspace-a",
    ) is None


def test_expiration_and_capacity_eviction_are_deterministic():
    clock = [1.0]
    store = BoundedResultStore(
        max_results=1,
        ttl_seconds=2,
        monotonic=lambda: clock[0],
    )
    _put(store)
    clock[0] = 4.0
    assert store.get(
        request_id=REQUEST_ID,
        owner_id="owner@example.com",
        workspace_id="workspace-a",
    ) is None


def test_capacity_evicts_the_oldest_only_when_a_new_result_is_inserted():
    clock = [1.0]
    store = BoundedResultStore(
        max_results=1,
        ttl_seconds=30,
        monotonic=lambda: clock[0],
    )
    _put(store)
    assert store.get(
        request_id=REQUEST_ID,
        owner_id="owner@example.com",
        workspace_id="workspace-a",
    ) is not None
    clock[0] = 2.0
    store.put(
        request_id="22222222-2222-4222-8222-222222222222",
        owner_id="owner@example.com",
        workspace_id="workspace-a",
        content=b"a\r\n2\r\n",
        filename="result.csv",
        media_type="text/csv",
        row_count=1,
        column_count=1,
        truncated=False,
    )
    assert store.get(
        request_id=REQUEST_ID,
        owner_id="owner@example.com",
        workspace_id="workspace-a",
    ) is None


@pytest.mark.parametrize(
    "change",
    (
        {"content": b""},
        {"media_type": "text/html"},
        {"row_count": 201},
        {"column_count": 41},
    ),
)
def test_unbounded_or_unapproved_results_fail_closed(change):
    store = BoundedResultStore()
    kwargs = {
        "request_id": REQUEST_ID,
        "owner_id": "owner@example.com",
        "workspace_id": "workspace-a",
        "content": b"a\r\n1\r\n",
        "filename": "result.csv",
        "media_type": "text/csv",
        "row_count": 1,
        "column_count": 1,
        "truncated": False,
    }
    kwargs.update(change)
    with pytest.raises(ValueError):
        store.put(**kwargs)
