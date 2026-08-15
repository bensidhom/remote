"""Authenticated HTTP surface for AI request evidence and private sessions."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .services.ai.context import AiRequestRegistry
from .services.ai.sessions import (
    MAX_RETENTION_DAYS,
    MAX_SESSION_TITLE_CHARACTERS,
    MAX_TURN_CHARACTERS,
    SESSION_POLICY,
    SessionLimitExceeded,
    SessionNotFound,
    add_user_turn,
    create_session,
    delete_session,
    get_session,
    list_sessions,
    list_turns,
)


class CreateSessionBody(BaseModel):
    title: str = Field(default="New conversation", min_length=1, max_length=MAX_SESSION_TITLE_CHARACTERS)
    retention_days: int = Field(default=30, ge=1, le=MAX_RETENTION_DAYS)


class CreateTurnBody(BaseModel):
    model_config = {"extra": "forbid"}

    content: str = Field(min_length=1, max_length=MAX_TURN_CHARACTERS)
    request_id: str | None = None


def _owner(request: Request) -> str:
    user = getattr(request.state, "user", None)
    owner = str(getattr(user, "email", "")).strip().lower()
    if not owner:
        raise HTTPException(status_code=401, detail="Authentication required")
    return owner


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Resource not found")


def create_ai_router(
    *,
    database_path: str | Path,
    workspace_id: str,
    registry: AiRequestRegistry,
) -> APIRouter:
    router = APIRouter(prefix="/api/ai", tags=["ai"])

    @router.post("/requests/{request_id}/cancel", status_code=202)
    def cancel_request(request_id: str, request: Request):
        if not registry.cancel(
            request_id=request_id,
            owner_id=_owner(request),
            workspace_id=workspace_id,
        ):
            raise _not_found()
        return {
            "event_schema_version": "ai-event-v1",
            "request_id": request_id,
            "status": "cancellation_requested",
        }

    @router.get("/requests/{request_id}/evidence")
    def request_evidence(request_id: str, request: Request):
        result = registry.evidence(
            request_id=request_id,
            owner_id=_owner(request),
            workspace_id=workspace_id,
        )
        if result is None:
            raise _not_found()
        return result

    @router.post("/sessions", status_code=201)
    def new_session(body: CreateSessionBody, request: Request):
        try:
            session = create_session(
                database_path,
                workspace_id=workspace_id,
                owner_id=_owner(request),
                title=body.title,
                retention_days=body.retention_days,
            )
        except (ValueError, SessionLimitExceeded) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"session": session.as_dict(), "policy": SESSION_POLICY}

    @router.get("/sessions")
    def sessions(request: Request, limit: int = 25):
        return {
            "sessions": [
                session.as_dict()
                for session in list_sessions(
                    database_path,
                    workspace_id=workspace_id,
                    owner_id=_owner(request),
                    limit=limit,
                )
            ],
            "policy": SESSION_POLICY,
        }

    @router.get("/sessions/{session_id}")
    def session_detail(session_id: str, request: Request):
        owner = _owner(request)
        try:
            session = get_session(
                database_path,
                workspace_id=workspace_id,
                owner_id=owner,
                session_id=session_id,
            )
            turns = list_turns(
                database_path,
                workspace_id=workspace_id,
                owner_id=owner,
                session_id=session_id,
            )
        except (SessionNotFound, ValueError):
            raise _not_found()
        return {
            "session": session.as_dict(),
            "turns": [turn.as_dict() for turn in turns],
            "policy": SESSION_POLICY,
        }

    @router.post("/sessions/{session_id}/turns", status_code=201)
    def new_turn(session_id: str, body: CreateTurnBody, request: Request):
        try:
            turn = add_user_turn(
                database_path,
                workspace_id=workspace_id,
                owner_id=_owner(request),
                session_id=session_id,
                content=body.content,
                request_id=body.request_id,
            )
        except SessionNotFound:
            raise _not_found()
        except (ValueError, SessionLimitExceeded) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"turn": turn.as_dict()}

    @router.delete("/sessions/{session_id}", status_code=204)
    def remove_session(session_id: str, request: Request):
        try:
            delete_session(
                database_path,
                workspace_id=workspace_id,
                owner_id=_owner(request),
                session_id=session_id,
            )
        except (SessionNotFound, ValueError):
            raise _not_found()
        return Response(status_code=204)

    return router


__all__ = ["create_ai_router"]
