from __future__ import annotations

import asyncio
from io import BytesIO
import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request
from starlette.datastructures import UploadFile

from app import main as main_module
from app.auth import AuthUser
from app.services.ai.context import AiRequestRegistry, AiRequestStatus
from app.services.bridge_identity import BridgeSnapshotIdentity
from app.services.operations.quotas import OperationQuotaRegistry


OWNER = "lifecycle@example.com"
WORKSPACE = main_module.SHARED_WORKSPACE_ID


class RecordingRegistry(AiRequestRegistry):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.finish_calls: list[tuple[object, AiRequestStatus]] = []

    def finish(self, context, *, status, evidence=None):
        self.finish_calls.append((context, AiRequestStatus(status)))
        return super().finish(context, status=status, evidence=evidence)


def _request(path: str, *, request_id: str | None = None) -> Request:
    request = Request({
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [
            (b"x-request-id", (request_id or str(uuid4())).encode("ascii")),
        ],
    })
    request.state.user = AuthUser(OWNER, "viewer")
    return request


def _identity() -> BridgeSnapshotIdentity:
    return BridgeSnapshotIdentity(
        workspace_id=WORKSPACE,
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


def _owned_status(registry: RecordingRegistry) -> str:
    context = registry.finish_calls[0][0]
    evidence = registry.evidence(
        request_id=context.request_id,
        owner_id=context.owner_id,
        workspace_id=context.workspace_id,
    )
    assert evidence is not None
    return str(evidence["status"])


def _assert_finished_once(registry: RecordingRegistry, status: str) -> None:
    assert len(registry.finish_calls) == 1
    assert _owned_status(registry) == status


def test_quota_exhaustion_is_429_with_retry_after_and_no_internal_code():
    quotas = OperationQuotaRegistry(
        operation_limits={"ask": 1, "rag": 1, "visual": 1},
        workspace_limit=10,
        owner_limit=10,
    )
    registry = RecordingRegistry(quota_registry=quotas)
    active = registry.register(
        request_id=str(uuid4()),
        owner_id=OWNER,
        workspace_id=WORKSPACE,
        conversation_id=None,
        mode="text",
        operation="database_ask",
    )

    async def exercise():
        with patch.object(main_module, "DEFAULT_AI_REQUEST_REGISTRY", registry):
            return await main_module.db_rag(
                request=_request("/api/db-rag"),
                question="count bridges",
                model="",
                state_code=None,
                bridge_number=None,
                strict_grounding=False,
                output_format="text",
                target_fields="",
                selected_sources=None,
                analysis_mode="text",
                conversation_id=None,
            )

    with pytest.raises(HTTPException) as rejected:
        asyncio.run(exercise())
    assert rejected.value.status_code == 429
    assert rejected.value.headers == {"Retry-After": "1"}
    assert rejected.value.detail == "AI request capacity is temporarily exhausted."
    assert "concurrency_limit" not in rejected.value.detail
    assert registry.finish_calls == []
    registry.finish(active, status=AiRequestStatus.STOPPED)


def test_oversized_terminal_evidence_cannot_leak_a_quota_lease():
    quotas = OperationQuotaRegistry(
        operation_limits={"ask": 1},
        workspace_limit=1,
        owner_limit=1,
    )
    registry = AiRequestRegistry(quota_registry=quotas)
    context = registry.register(
        request_id=str(uuid4()),
        owner_id=OWNER,
        workspace_id=WORKSPACE,
        conversation_id=None,
        mode="text",
        operation="database_ask",
    )
    registry.finish(
        context,
        status=AiRequestStatus.ERROR,
        evidence={"oversized": "x" * (128 * 1024)},
    )
    snapshot = quotas.snapshot(
        workspace_id=WORKSPACE,
        owner_id=OWNER,
        operation="ask",
    )
    assert snapshot.active_total == 0
    owned = registry.evidence(
        request_id=context.request_id,
        owner_id=OWNER,
        workspace_id=WORKSPACE,
    )
    assert owned is not None
    assert owned["status"] == "error"
    assert owned["evidence"]["terminal_evidence_rejected"] is True


def test_exact_cancellation_wins_a_race_with_late_provider_success():
    registry = AiRequestRegistry()
    context = registry.register(
        request_id=str(uuid4()),
        owner_id=OWNER,
        workspace_id=WORKSPACE,
        conversation_id=None,
        mode="text",
        operation="database_ask",
    )
    assert registry.cancel(
        request_id=context.request_id,
        owner_id=OWNER,
        workspace_id=WORKSPACE,
    )
    registry.finish(context, status=AiRequestStatus.COMPLETED)
    owned = registry.evidence(
        request_id=context.request_id,
        owner_id=OWNER,
        workspace_id=WORKSPACE,
    )
    assert owned is not None
    assert owned["status"] == "stopped"


def test_each_nonstream_ai_surface_finishes_once_on_provider_error():
    async def exercise():
        # Standalone document upload/query.
        for route_name in ("vision_rag", "vision_query"):
            registry = RecordingRegistry()
            provider_name = (
                "process_vision_rag" if route_name == "vision_rag"
                else "process_vision_query_only"
            )
            with (
                patch.object(main_module, "DEFAULT_AI_REQUEST_REGISTRY", registry),
                patch.object(
                    main_module,
                    provider_name,
                    new=AsyncMock(side_effect=RuntimeError("provider secret")),
                ),
            ):
                if route_name == "vision_rag":
                    response = await main_module.vision_rag(
                        request=_request("/api/vision-rag"),
                        file=UploadFile(BytesIO(b"%PDF-1.7"), filename="report.pdf"),
                        question="summarize",
                        model="",
                        strict_grounding=False,
                        output_format="text",
                        target_fields="",
                        analysis_mode="text",
                        defect_segmentation_mode="balanced",
                        conversation_id=None,
                    )
                else:
                    response = await main_module.vision_query(
                        request=_request("/api/vision-query"),
                        question="summarize",
                        model="",
                        strict_grounding=False,
                        output_format="text",
                        target_fields="",
                        analysis_mode="text",
                        defect_segmentation_mode="balanced",
                        doc_hash="a" * 64,
                        conversation_id=None,
                    )
            assert response.status_code == 500
            assert b"provider secret" not in response.body
            _assert_finished_once(registry, "error")

        # Database-wide Ask.
        registry = RecordingRegistry()
        with (
            patch.object(main_module, "DEFAULT_AI_REQUEST_REGISTRY", registry),
            patch.object(
                main_module,
                "process_db_rag_with_files",
                side_effect=RuntimeError("database provider secret"),
            ),
        ):
            response = await main_module.db_rag(
                request=_request("/api/db-rag"),
                question="count bridges",
                model="",
                state_code=None,
                bridge_number=None,
                strict_grounding=False,
                output_format="text",
                target_fields="",
                selected_sources=None,
                analysis_mode="text",
                conversation_id=None,
            )
        assert response.status_code == 500
        assert b"database provider secret" not in response.body
        _assert_finished_once(registry, "error")

        # Explicit bridge visual investigation.
        registry = RecordingRegistry()
        identity = _identity()
        with (
            patch.object(main_module, "DEFAULT_AI_REQUEST_REGISTRY", registry),
            patch.object(
                main_module,
                "_published_identity_evidence",
                return_value=({"status": "resolved"}, 2025, identity),
            ),
            patch.object(
                main_module,
                "process_bridge_visual_investigation",
                new=AsyncMock(side_effect=RuntimeError("visual provider secret")),
            ),
        ):
            with pytest.raises(HTTPException) as visual_error:
                await main_module.bridge_visual_investigation(
                    request=_request("/api/bridge-visual-investigation"),
                    query="find spalling",
                    state_code=34,
                    bridge_number="A-1",
                    selected_sources=None,
                    defect_segmentation_mode="fast",
                    analysis_mode="visual",
                    conversation_id=None,
                    data_year=2025,
                    snapshot_revision_id="revision-1",
                )
        assert visual_error.value.status_code == 500
        assert "visual provider secret" not in visual_error.value.detail
        _assert_finished_once(registry, "error")

    asyncio.run(exercise())


def test_async_task_cancellation_releases_once_and_stays_stopped():
    registry = RecordingRegistry()

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()

    async def exercise():
        with (
            patch.object(main_module, "DEFAULT_AI_REQUEST_REGISTRY", registry),
            patch.object(main_module, "process_vision_query_only", new=cancelled),
        ):
            with pytest.raises(asyncio.CancelledError):
                await main_module.vision_query(
                    request=_request("/api/vision-query"),
                    question="summarize",
                    model="",
                    strict_grounding=False,
                    output_format="text",
                    target_fields="",
                    analysis_mode="text",
                    defect_segmentation_mode="balanced",
                    doc_hash="a" * 64,
                    conversation_id=None,
                )

    asyncio.run(exercise())
    _assert_finished_once(registry, "stopped")
    assert registry.finish_calls[0][0].cancellation_token.is_set()


def test_internal_http_error_is_sanitized_and_released_once():
    registry = RecordingRegistry()

    async def rejected(*args, **kwargs):
        raise HTTPException(status_code=503, detail="upstream host and credential secret")

    async def exercise():
        with (
            patch.object(main_module, "DEFAULT_AI_REQUEST_REGISTRY", registry),
            patch.object(main_module, "process_vision_query_only", new=rejected),
        ):
            with pytest.raises(HTTPException) as failure:
                await main_module.vision_query(
                    request=_request("/api/vision-query"),
                    question="summarize",
                    model="",
                    strict_grounding=False,
                    output_format="text",
                    target_fields="",
                    analysis_mode="text",
                    defect_segmentation_mode="balanced",
                    doc_hash="a" * 64,
                    conversation_id=None,
                )
        assert failure.value.status_code == 500
        assert failure.value.detail == "Document query could not complete."

    asyncio.run(exercise())
    _assert_finished_once(registry, "error")


def test_stream_close_immediately_after_first_event_releases_once_as_stopped():
    registry = RecordingRegistry()

    async def exercise():
        with patch.object(main_module, "DEFAULT_AI_REQUEST_REGISTRY", registry):
            response = await main_module.db_rag_stream(
                request=_request("/api/db-rag/stream"),
                question="count bridges",
                model="",
                state_code=None,
                bridge_number=None,
                strict_grounding=False,
                output_format="text",
                target_fields="",
                selected_sources=None,
                analysis_mode="text",
                conversation_id=None,
            )
            iterator = response.body_iterator
            first = await iterator.__anext__()
            assert json.loads(first)["type"] == "accepted"
            await iterator.aclose()

    asyncio.run(exercise())
    _assert_finished_once(registry, "stopped")
    assert registry.finish_calls[0][0].cancellation_token.is_set()


def test_stream_provider_error_is_generic_and_releases_once():
    registry = RecordingRegistry()

    async def exercise():
        with (
            patch.object(main_module, "DEFAULT_AI_REQUEST_REGISTRY", registry),
            patch.object(
                main_module,
                "process_db_rag_with_files",
                side_effect=RuntimeError("stream provider secret"),
            ),
        ):
            response = await main_module.db_rag_stream(
                request=_request("/api/db-rag/stream"),
                question="count bridges",
                model="",
                state_code=None,
                bridge_number=None,
                strict_grounding=False,
                output_format="text",
                target_fields="",
                selected_sources=None,
                analysis_mode="text",
                conversation_id=None,
            )
            events = [json.loads(item) async for item in response.body_iterator]
            assert [item["type"] for item in events] == ["accepted", "error"]
            assert events[-1]["detail"] == "Ask could not complete."
            assert "stream provider secret" not in json.dumps(events)

    asyncio.run(exercise())
    _assert_finished_once(registry, "error")
