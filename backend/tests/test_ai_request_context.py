from __future__ import annotations

import threading
from uuid import uuid4

import pytest

from app.services.ai.context import (
    AiRequestCancelled,
    AiRequestRegistry,
    check_ai_request_cancelled,
)
from app.services.bridge_identity import BridgeSnapshotIdentity


def _register(registry: AiRequestRegistry, *, owner: str = "a@example.com"):
    return registry.register(
        request_id=str(uuid4()),
        owner_id=owner,
        workspace_id="workspace-a",
        conversation_id=None,
        mode="text",
        operation="bridge_ask",
        state_code=34,
        bridge_number="A-1",
    )


def test_cancel_is_exact_owner_and_workspace_scoped():
    registry = AiRequestRegistry()
    first = _register(registry, owner="a@example.com")
    second = _register(registry, owner="b@example.com")

    assert registry.cancel(
        request_id=first.request_id,
        owner_id="b@example.com",
        workspace_id="workspace-a",
    ) is False
    assert registry.cancel(
        request_id=first.request_id,
        owner_id="a@example.com",
        workspace_id="workspace-b",
    ) is False
    assert not first.cancellation_token.is_set()
    assert not second.cancellation_token.is_set()

    assert registry.cancel(
        request_id=first.request_id,
        owner_id="a@example.com",
        workspace_id="workspace-a",
    ) is True
    assert first.cancellation_token.is_set()
    assert not second.cancellation_token.is_set()
    with pytest.raises(AiRequestCancelled):
        check_ai_request_cancelled(first)


def test_evidence_read_is_non_enumerating_and_privacy_bounded():
    registry = AiRequestRegistry()
    context = _register(registry)
    registry.update_evidence(
        context,
        {"visual_processing_ran": False, "sources": ["report.pdf:p2"]},
    )

    assert registry.evidence(
        request_id=context.request_id,
        owner_id="other@example.com",
        workspace_id="workspace-a",
    ) is None
    assert registry.evidence(
        request_id="not-a-request-id",
        owner_id="a@example.com",
        workspace_id="workspace-a",
    ) is None
    owned = registry.evidence(
        request_id=context.request_id,
        owner_id="a@example.com",
        workspace_id="workspace-a",
    )
    assert owned is not None
    assert owned["event_schema_version"] == "ai-event-v1"
    assert owned["analysis_mode"] == "text"
    assert owned["evidence"]["visual_processing_ran"] is False
    assert "owner_id" not in owned
    assert "workspace_id" not in owned


def test_duplicate_ids_and_capacity_fail_closed():
    registry = AiRequestRegistry(max_entries=1)
    context = _register(registry)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            request_id=context.request_id,
            owner_id="a@example.com",
            workspace_id="workspace-a",
            conversation_id=None,
            mode="text",
            operation="bridge_ask",
        )
    with pytest.raises(RuntimeError, match="capacity"):
        _register(registry)


def test_context_rejects_invalid_mode_scope_and_uuid():
    registry = AiRequestRegistry()
    with pytest.raises(ValueError):
        registry.register(
            request_id="predictable",
            owner_id="a@example.com",
            workspace_id="workspace-a",
            conversation_id=None,
            mode="text",
            operation="ask",
        )
    with pytest.raises(ValueError):
        registry.register(
            request_id=str(uuid4()),
            owner_id="a@example.com",
            workspace_id="workspace-a",
            conversation_id=None,
            mode="visual-ish",
            operation="ask",
        )


def test_cancelled_token_is_request_owned_threading_event():
    registry = AiRequestRegistry()
    context = _register(registry)
    assert isinstance(context.cancellation_token, threading.Event)


def test_context_pins_exact_published_identity_and_rejects_scope_mismatch():
    identity = BridgeSnapshotIdentity(
        workspace_id="workspace-a",
        state_code=34,
        bridge_number="A-1",
        data_year=2025,
        snapshot_revision_id="revision-1",
        revision_number=1,
        canonical_sha256="a" * 64,
        profile_id="profile-1",
        rule_bundle_sha256="b" * 64,
        published_at="2026-08-13T12:00:00Z",
        data_generation=1,
        schema_generation=7,
    )
    registry = AiRequestRegistry()
    context = registry.register(
        request_id=str(uuid4()),
        owner_id="a@example.com",
        workspace_id="workspace-a",
        conversation_id=None,
        mode="text",
        operation="bridge_ask",
        state_code=34,
        bridge_number="A-1",
        data_year=2025,
        bridge_identity=identity,
    )
    assert context.bridge_identity is identity

    with pytest.raises(ValueError, match="does not match"):
        registry.register(
            request_id=str(uuid4()),
            owner_id="a@example.com",
            workspace_id="workspace-a",
            conversation_id=None,
            mode="text",
            operation="bridge_ask",
            state_code=34,
            bridge_number="OTHER",
            data_year=2025,
            bridge_identity=identity,
        )
