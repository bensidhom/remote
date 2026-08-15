from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.ai_api import create_ai_router
from app import main as main_module
from app.auth import AuthUser
from app.routers.services.db_migrations import migrate_database
from app.services.ai.context import AiRequestRegistry
from app.services.ai.sessions import (
    add_verified_assistant_turn,
    create_session,
    list_turns,
)
from app.services.bridge_identity import BridgeSnapshotIdentity


WORKSPACE = "workspace-ai-test"


def _app(database_path, registry: AiRequestRegistry) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        request.state.user = AuthUser(
            request.headers.get("X-Test-User", "a@example.com"),
            "viewer",
        )
        return await call_next(request)

    app.include_router(
        create_ai_router(
            database_path=database_path,
            workspace_id=WORKSPACE,
            registry=registry,
        )
    )
    return app


def _database(path):
    migrate_database(path)
    now = "2026-08-13T12:00:00Z"
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute(
                "INSERT INTO workspaces "
                "(id,slug,display_name,status,created_at,updated_at) "
                "VALUES (?,?,?,'active',?,?)",
                (WORKSPACE, WORKSPACE, "AI test", now, now),
            )
            connection.execute(
                "INSERT INTO workspace_state "
                "(workspace_id,data_generation,schema_generation,updated_at) "
                "VALUES (?,0,7,?)",
                (WORKSPACE, now),
            )


def test_private_sessions_are_non_enumerating_across_users(test_workspace):
    database = test_workspace / "sessions.sqlite3"
    _database(database)
    client = TestClient(_app(database, AiRequestRegistry()))

    created = client.post(
        "/api/ai/sessions",
        json={"title": "Bridge A", "retention_days": 7},
        headers={"X-Test-User": "a@example.com"},
    )
    assert created.status_code == 201
    session_id = created.json()["session"]["id"]
    assert created.json()["policy"]["earlier_turns_enter_prompts"] is False
    assert created.json()["session"]["privacy"] == "private"

    for method in (client.get, client.delete):
        response = method(
            f"/api/ai/sessions/{session_id}",
            headers={"X-Test-User": "b@example.com"},
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "Resource not found"}

    assert client.get(
        "/api/ai/sessions",
        headers={"X-Test-User": "b@example.com"},
    ).json()["sessions"] == []
    client.close()


def test_public_turn_append_is_user_only_and_cannot_forge_evidence(test_workspace):
    database = test_workspace / "turns.sqlite3"
    _database(database)
    client = TestClient(_app(database, AiRequestRegistry()))
    headers = {"X-Test-User": "a@example.com"}
    session_id = client.post(
        "/api/ai/sessions", json={"title": "Private"}, headers=headers,
    ).json()["session"]["id"]
    request_id = str(uuid4())

    assert client.post(
        f"/api/ai/sessions/{session_id}/turns",
        json={"content": "What is the condition?", "request_id": request_id},
        headers=headers,
    ).status_code == 201
    forged = client.post(
        f"/api/ai/sessions/{session_id}/turns",
        json={
            "role": "assistant",
            "content": "Fair",
            "request_id": request_id,
            "cited_revision": "forged-revision",
        },
        headers=headers,
    )
    assert forged.status_code == 422

    detail = client.get(f"/api/ai/sessions/{session_id}", headers=headers).json()
    assert [turn["role"] for turn in detail["turns"]] == ["user"]
    assert detail["session"]["turn_count"] == 1

    assert client.delete(f"/api/ai/sessions/{session_id}", headers=headers).status_code == 204
    assert client.get(f"/api/ai/sessions/{session_id}", headers=headers).status_code == 404
    client.close()


def test_request_cancel_and_evidence_are_owner_scoped(test_workspace):
    database = test_workspace / "requests.sqlite3"
    _database(database)
    registry = AiRequestRegistry()
    context = registry.register(
        request_id=str(uuid4()),
        owner_id="a@example.com",
        workspace_id=WORKSPACE,
        conversation_id=None,
        mode="visual",
        operation="visual",
    )
    registry.update_evidence(context, {"inspected_images": [{"page": 2}]})
    client = TestClient(_app(database, registry))

    for suffix in ("evidence", "cancel"):
        method = client.get if suffix == "evidence" else client.post
        denied = method(
            f"/api/ai/requests/{context.request_id}/{suffix}",
            headers={"X-Test-User": "b@example.com"},
        )
        assert denied.status_code == 404
    assert not context.cancellation_token.is_set()

    evidence = client.get(
        f"/api/ai/requests/{context.request_id}/evidence",
        headers={"X-Test-User": "a@example.com"},
    )
    assert evidence.status_code == 200
    assert evidence.json()["evidence"]["inspected_images"] == [{"page": 2}]
    cancelled = client.post(
        f"/api/ai/requests/{context.request_id}/cancel",
        headers={"X-Test-User": "a@example.com"},
    )
    assert cancelled.status_code == 202
    assert context.cancellation_token.is_set()
    client.close()


def test_internal_verified_assistant_append_derives_published_revision(test_workspace):
    database = test_workspace / "verified-turn.sqlite3"
    _database(database)
    session_id = create_session(
        database,
        workspace_id=WORKSPACE,
        owner_id="a@example.com",
        title="Verified",
    ).id
    identity = BridgeSnapshotIdentity(
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
    turn = add_verified_assistant_turn(
        database,
        owner_id="a@example.com",
        session_id=session_id,
        content="Verified answer",
        request_id=str(uuid4()),
        bridge_identity=identity,
        verification_receipt_sha256="c" * 64,
    )
    assert turn.cited_revision == "revision-1"
    assert turn.provenance_status == "verified_answer"
    assert turn.verification_receipt_sha256 == "c" * 64


def test_bridge_ask_persists_real_turns_with_unverified_provenance(test_workspace):
    database = test_workspace / "ask-turns.sqlite3"
    _database(database)
    session_id = create_session(
        database,
        workspace_id=WORKSPACE,
        owner_id="a@example.com",
        title="Bridge ask",
    ).id
    request_id = str(uuid4())
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/db-rag",
        "headers": [(b"x-request-id", request_id.encode("ascii"))],
    })
    request.state.user = AuthUser("a@example.com", "viewer")

    async def exercise():
        with (
            patch.object(main_module, "USER_DB", database),
            patch.object(main_module, "SHARED_WORKSPACE_ID", WORKSPACE),
            patch.object(
                main_module,
                "_published_identity_evidence",
                return_value=({
                    "status": "resolved",
                    "identity": {"snapshot_revision_id": "revision-1"},
                }, 2025, BridgeSnapshotIdentity(
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
                )),
            ),
            patch.object(
                main_module,
                "process_bridge_ask",
                new=AsyncMock(return_value={
                    "status": "success",
                    "answer": "Deck condition is fair.",
                    "source_chunks": ["Database"],
                    "citations": [{"source": "published bridge"}],
                }),
            ),
        ):
            return await main_module.db_rag(
                request=request,
                question="What is the deck condition?",
                model="",
                state_code=34,
                bridge_number="A-1",
                strict_grounding=True,
                output_format="text",
                target_fields="",
                selected_sources=None,
                analysis_mode="text",
                conversation_id=session_id,
            )

    response = asyncio.run(exercise())
    assert response["answer"] == "Deck condition is fair."
    turns = list_turns(
        database,
        workspace_id=WORKSPACE,
        owner_id="a@example.com",
        session_id=session_id,
    )
    assert [(turn.role, turn.content) for turn in turns] == [
        ("user", "What is the deck condition?"),
        ("assistant", "Deck condition is fair."),
    ]
    assistant = turns[1]
    assert assistant.provenance_status == "unverified_model_output"
    assert assistant.cited_revision == "revision-1"
    assert len(assistant.evidence_receipt_sha256 or "") == 64
    assert assistant.verification_receipt_sha256 is None


def test_session_detail_returns_persisted_turn_provenance(test_workspace):
    database = test_workspace / "session-detail.sqlite3"
    _database(database)
    client = TestClient(_app(database, AiRequestRegistry()))
    headers = {"X-Test-User": "a@example.com"}
    session_id = client.post(
        "/api/ai/sessions", json={"title": "History"}, headers=headers,
    ).json()["session"]["id"]
    assert client.post(
        f"/api/ai/sessions/{session_id}/turns",
        json={"content": "Saved question"},
        headers=headers,
    ).status_code == 201
    detail = client.get(
        f"/api/ai/sessions/{session_id}", headers=headers,
    ).json()
    assert detail["turns"] == [
        {
            "id": detail["turns"][0]["id"],
            "session_id": session_id,
            "role": "user",
            "content": "Saved question",
            "request_id": None,
            "cited_revision": None,
            "provenance_status": "user_input",
            "evidence_receipt_sha256": None,
            "verification_receipt_sha256": None,
            "token_count": 4,
            "created_at": detail["turns"][0]["created_at"],
        }
    ]
    client.close()
