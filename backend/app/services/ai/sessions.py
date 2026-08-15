"""Private, bounded, workspace-scoped conversation sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import sqlite3
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4


MAX_SESSION_TITLE_CHARACTERS = 120
MAX_TURN_CHARACTERS = 16_000
MAX_TURNS_PER_SESSION = 40
MAX_SESSIONS_PER_OWNER = 25
DEFAULT_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 90


class SessionNotFound(LookupError):
    """Also used for cross-owner requests to keep lookup non-enumerating."""


class SessionLimitExceeded(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _uuid(value: str, field_name: str) -> str:
    text = str(value or "").strip().lower()
    try:
        parsed = UUID(text)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a canonical UUID") from exc
    if str(parsed) != text:
        raise ValueError(f"{field_name} must be a canonical UUID")
    return text


def _token(value: object, field_name: str, maximum: int = 255) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        raise ValueError(f"{field_name} is invalid")
    return text


@dataclass(frozen=True, slots=True)
class ChatSession:
    id: str
    workspace_id: str
    owner_id: str
    title: str
    created_at: str
    updated_at: str
    retention_expires_at: str
    turn_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "retention_expires_at": self.retention_expires_at,
            "turn_count": self.turn_count,
            "privacy": "private",
        }


@dataclass(frozen=True, slots=True)
class ChatTurn:
    id: str
    session_id: str
    role: Literal["user", "assistant"]
    content: str
    request_id: str | None
    cited_revision: str | None
    provenance_status: Literal["user_input", "unverified_model_output", "verified_answer"]
    evidence_receipt_sha256: str | None
    verification_receipt_sha256: str | None
    token_count: int
    created_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "content": self.content,
            "request_id": self.request_id,
            "cited_revision": self.cited_revision,
            "provenance_status": self.provenance_status,
            "evidence_receipt_sha256": self.evidence_receipt_sha256,
            "verification_receipt_sha256": self.verification_receipt_sha256,
            "token_count": self.token_count,
            "created_at": self.created_at,
        }


def _connect(database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database_path), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextmanager
def _opened_connection(database_path: str | Path):
    connection = _connect(database_path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _purge_expired(
    connection: sqlite3.Connection,
    now_iso: str,
    *,
    workspace_id: str | None = None,
) -> int:
    """Purge expired private sessions while honoring central legal holds.

    The retention table is additive and may be absent during early migrations
    or focused unit tests.  Once present, every opportunistic purge path uses
    it, so an API read cannot bypass a hold placed by operations.
    """

    has_policies = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='retention_resource_policies'"
    ).fetchone() is not None
    clauses = ["retention_expires_at<=?"]
    params: list[object] = [now_iso]
    if workspace_id is not None:
        clauses.append("workspace_id=?")
        params.append(workspace_id)
    if has_policies:
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM retention_resource_policies p "
            "WHERE p.workspace_id=chat_sessions.workspace_id "
            "AND p.resource_type='chat_session' "
            "AND p.resource_id=chat_sessions.id AND p.legal_hold=1)"
        )
    cursor = connection.execute(
        "DELETE FROM chat_sessions WHERE " + " AND ".join(clauses),
        tuple(params),
    )
    return max(0, int(cursor.rowcount))


def purge_expired_sessions(
    database_path: str | Path,
    *,
    workspace_id: str | None = None,
    now: datetime | None = None,
) -> int:
    """Callable maintenance sweep; it does not depend on incoming API calls."""

    workspace = _token(workspace_id, "workspace_id") if workspace_id is not None else None
    now_iso = _iso((now or _utc_now()).astimezone(timezone.utc))
    with _opened_connection(database_path) as connection:
        return _purge_expired(connection, now_iso, workspace_id=workspace)


def _session_from_row(row: sqlite3.Row) -> ChatSession:
    return ChatSession(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]),
        owner_id=str(row["owner_id"]),
        title=str(row["title"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        retention_expires_at=str(row["retention_expires_at"]),
        turn_count=int(row["turn_count"]),
    )


def create_session(
    database_path: str | Path,
    *,
    workspace_id: str,
    owner_id: str,
    title: str = "New conversation",
    retention_days: int = DEFAULT_RETENTION_DAYS,
    now: datetime | None = None,
) -> ChatSession:
    workspace = _token(workspace_id, "workspace_id")
    owner = _token(str(owner_id).lower(), "owner_id")
    clean_title = _token(title, "title", MAX_SESSION_TITLE_CHARACTERS)
    if not 1 <= int(retention_days) <= MAX_RETENTION_DAYS:
        raise ValueError(f"retention_days must be between 1 and {MAX_RETENTION_DAYS}")
    instant = (now or _utc_now()).astimezone(timezone.utc)
    now_iso = _iso(instant)
    expiry = _iso(instant + timedelta(days=int(retention_days)))
    session_id = str(uuid4())
    with _opened_connection(database_path) as connection:
        _purge_expired(connection, now_iso)
        count = int(connection.execute(
            "SELECT COUNT(*) FROM chat_sessions WHERE workspace_id=? AND owner_id=?",
            (workspace, owner),
        ).fetchone()[0])
        if count >= MAX_SESSIONS_PER_OWNER:
            raise SessionLimitExceeded("private session limit reached")
        connection.execute(
            "INSERT INTO chat_sessions "
            "(id,workspace_id,owner_id,title,created_at,updated_at,retention_expires_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (session_id, workspace, owner, clean_title, now_iso, now_iso, expiry),
        )
    return get_session(
        database_path,
        workspace_id=workspace,
        owner_id=owner,
        session_id=session_id,
        now=instant,
    )


def list_sessions(
    database_path: str | Path,
    *,
    workspace_id: str,
    owner_id: str,
    limit: int = 25,
    now: datetime | None = None,
) -> tuple[ChatSession, ...]:
    workspace = _token(workspace_id, "workspace_id")
    owner = _token(str(owner_id).lower(), "owner_id")
    bounded_limit = max(1, min(int(limit), MAX_SESSIONS_PER_OWNER))
    now_iso = _iso(now or _utc_now())
    with _opened_connection(database_path) as connection:
        _purge_expired(connection, now_iso)
        rows = connection.execute(
            "SELECT s.*, COUNT(t.id) AS turn_count FROM chat_sessions s "
            "LEFT JOIN chat_turns t ON t.workspace_id=s.workspace_id "
            "AND t.owner_id=s.owner_id AND t.session_id=s.id "
            "WHERE s.workspace_id=? AND s.owner_id=? "
            "GROUP BY s.id ORDER BY s.updated_at DESC,s.id LIMIT ?",
            (workspace, owner, bounded_limit),
        ).fetchall()
    return tuple(_session_from_row(row) for row in rows)


def get_session(
    database_path: str | Path,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    now: datetime | None = None,
) -> ChatSession:
    workspace = _token(workspace_id, "workspace_id")
    owner = _token(str(owner_id).lower(), "owner_id")
    canonical = _uuid(session_id, "session_id")
    now_iso = _iso(now or _utc_now())
    with _opened_connection(database_path) as connection:
        _purge_expired(connection, now_iso)
        row = connection.execute(
            "SELECT s.*, COUNT(t.id) AS turn_count FROM chat_sessions s "
            "LEFT JOIN chat_turns t ON t.workspace_id=s.workspace_id "
            "AND t.owner_id=s.owner_id AND t.session_id=s.id "
            "WHERE s.workspace_id=? AND s.owner_id=? AND s.id=? GROUP BY s.id",
            (workspace, owner, canonical),
        ).fetchone()
    if row is None:
        raise SessionNotFound("session not found")
    return _session_from_row(row)


def _add_turn(
    database_path: str | Path,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    role: Literal["user", "assistant"],
    content: str,
    request_id: str | None = None,
    cited_revision: str | None = None,
    provenance_status: Literal[
        "user_input", "unverified_model_output", "verified_answer"
    ] = "user_input",
    evidence_receipt_sha256: str | None = None,
    verification_receipt_sha256: str | None = None,
    now: datetime | None = None,
) -> ChatTurn:
    workspace = _token(workspace_id, "workspace_id")
    owner = _token(str(owner_id).lower(), "owner_id")
    canonical_session = _uuid(session_id, "session_id")
    if role not in {"user", "assistant"}:
        raise ValueError("role must be user or assistant")
    clean_content = _token(content, "content", MAX_TURN_CHARACTERS)
    canonical_request = _uuid(request_id, "request_id") if request_id else None
    revision = _token(cited_revision, "cited_revision") if cited_revision else None
    evidence_receipt = (
        _token(evidence_receipt_sha256, "evidence_receipt_sha256", 64)
        if evidence_receipt_sha256 else None
    )
    verification_receipt = (
        _token(verification_receipt_sha256, "verification_receipt_sha256", 64)
        if verification_receipt_sha256 else None
    )
    for field_name, digest in (
        ("evidence_receipt_sha256", evidence_receipt),
        ("verification_receipt_sha256", verification_receipt),
    ):
        if digest is not None and (
            len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(f"{field_name} is invalid")
    if provenance_status not in {
        "user_input", "unverified_model_output", "verified_answer",
    }:
        raise ValueError("provenance_status is invalid")
    if role == "user" and (
        provenance_status != "user_input"
        or revision is not None
        or evidence_receipt is not None
        or verification_receipt is not None
    ):
        raise ValueError("user turns cannot claim assistant provenance")
    if role == "assistant" and provenance_status == "user_input":
        raise ValueError("assistant turns require explicit model provenance")
    if role == "assistant" and provenance_status == "unverified_model_output":
        if evidence_receipt is None or verification_receipt is not None:
            raise ValueError("unverified assistant turns require only an evidence receipt")
    if role == "assistant" and provenance_status == "verified_answer":
        if revision is None or verification_receipt is None:
            raise ValueError("verified assistant turns require revision and verification receipt")
    instant = (now or _utc_now()).astimezone(timezone.utc)
    now_iso = _iso(instant)
    turn_id = str(uuid4())
    token_count = max(1, (len(clean_content) + 3) // 4)
    with _opened_connection(database_path) as connection:
        _purge_expired(connection, now_iso)
        session = connection.execute(
            "SELECT 1 FROM chat_sessions WHERE workspace_id=? AND owner_id=? AND id=?",
            (workspace, owner, canonical_session),
        ).fetchone()
        if session is None:
            raise SessionNotFound("session not found")
        count = int(connection.execute(
            "SELECT COUNT(*) FROM chat_turns WHERE workspace_id=? AND owner_id=? AND session_id=?",
            (workspace, owner, canonical_session),
        ).fetchone()[0])
        if count >= MAX_TURNS_PER_SESSION:
            raise SessionLimitExceeded("session turn limit reached")
        connection.execute(
            "INSERT INTO chat_turns "
            "(id,workspace_id,owner_id,session_id,role,content,request_id,cited_revision,"
            "provenance_status,evidence_receipt_sha256,verification_receipt_sha256,"
            "token_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                turn_id, workspace, owner, canonical_session, role,
                clean_content, canonical_request, revision, provenance_status,
                evidence_receipt, verification_receipt, token_count, now_iso,
            ),
        )
        connection.execute(
            "UPDATE chat_sessions SET updated_at=? WHERE workspace_id=? AND owner_id=? AND id=?",
            (now_iso, workspace, owner, canonical_session),
        )
    return ChatTurn(
        id=turn_id,
        session_id=canonical_session,
        role=role,
        content=clean_content,
        request_id=canonical_request,
        cited_revision=revision,
        provenance_status=provenance_status,
        evidence_receipt_sha256=evidence_receipt,
        verification_receipt_sha256=verification_receipt,
        token_count=token_count,
        created_at=now_iso,
    )


def list_turns(
    database_path: str | Path,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    limit: int = MAX_TURNS_PER_SESSION,
    now: datetime | None = None,
) -> tuple[ChatTurn, ...]:
    # Authorize through the exact same private lookup before reading turns.
    get_session(
        database_path,
        workspace_id=workspace_id,
        owner_id=owner_id,
        session_id=session_id,
        now=now,
    )
    workspace = _token(workspace_id, "workspace_id")
    owner = _token(str(owner_id).lower(), "owner_id")
    canonical = _uuid(session_id, "session_id")
    bounded_limit = max(1, min(int(limit), MAX_TURNS_PER_SESSION))
    with _opened_connection(database_path) as connection:
        rows = connection.execute(
            "SELECT * FROM (SELECT * FROM chat_turns WHERE workspace_id=? "
            "AND owner_id=? AND session_id=? ORDER BY created_at DESC,id DESC LIMIT ?) "
            "ORDER BY created_at,id",
            (workspace, owner, canonical, bounded_limit),
        ).fetchall()
    return tuple(
        ChatTurn(
            id=str(row["id"]), session_id=str(row["session_id"]),
            role=str(row["role"]), content=str(row["content"]),
            request_id=row["request_id"], cited_revision=row["cited_revision"],
            provenance_status=str(row["provenance_status"]),
            evidence_receipt_sha256=row["evidence_receipt_sha256"],
            verification_receipt_sha256=row["verification_receipt_sha256"],
            token_count=int(row["token_count"]), created_at=str(row["created_at"]),
        )
        for row in rows
    )


def add_user_turn(
    database_path: str | Path,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    content: str,
    request_id: str | None = None,
    reserve_assistant_turn: bool = False,
    now: datetime | None = None,
) -> ChatTurn:
    if reserve_assistant_turn:
        workspace = _token(workspace_id, "workspace_id")
        owner = _token(str(owner_id).lower(), "owner_id")
        canonical_session = _uuid(session_id, "session_id")
        with _opened_connection(database_path) as connection:
            count = int(connection.execute(
                "SELECT COUNT(*) FROM chat_turns WHERE workspace_id=? "
                "AND owner_id=? AND session_id=?",
                (workspace, owner, canonical_session),
            ).fetchone()[0])
        if count >= MAX_TURNS_PER_SESSION - 1:
            raise SessionLimitExceeded(
                "session needs room for both the user and assistant turn"
            )
    return _add_turn(
        database_path,
        workspace_id=workspace_id,
        owner_id=owner_id,
        session_id=session_id,
        role="user",
        content=content,
        request_id=request_id,
        provenance_status="user_input",
        now=now,
    )


def add_unverified_assistant_turn(
    database_path: str | Path,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    content: str,
    request_id: str,
    evidence_receipt_sha256: str,
    cited_revision: str | None = None,
    now: datetime | None = None,
) -> ChatTurn:
    """Internal append for model output that has not passed answer verification.

    ``cited_revision`` records the inventory revision used when one was
    resolved, but never upgrades the answer to verified provenance.
    """
    return _add_turn(
        database_path,
        workspace_id=workspace_id,
        owner_id=owner_id,
        session_id=session_id,
        role="assistant",
        content=content,
        request_id=request_id,
        cited_revision=cited_revision,
        provenance_status="unverified_model_output",
        evidence_receipt_sha256=evidence_receipt_sha256,
        now=now,
    )


def add_verified_assistant_turn(
    database_path: str | Path,
    *,
    owner_id: str,
    session_id: str,
    content: str,
    request_id: str,
    bridge_identity,
    verification_receipt_sha256: str,
    now: datetime | None = None,
) -> ChatTurn:
    """Internal append that derives revision scope from a resolved identity."""
    try:
        from ..bridge_identity import BridgeSnapshotIdentity
    except ImportError:  # pragma: no cover
        from services.bridge_identity import BridgeSnapshotIdentity
    if not isinstance(bridge_identity, BridgeSnapshotIdentity):
        raise ValueError("bridge_identity must be a resolved published identity")
    return _add_turn(
        database_path,
        workspace_id=bridge_identity.workspace_id,
        owner_id=owner_id,
        session_id=session_id,
        role="assistant",
        content=content,
        request_id=request_id,
        cited_revision=bridge_identity.snapshot_revision_id,
        provenance_status="verified_answer",
        verification_receipt_sha256=verification_receipt_sha256,
        now=now,
    )


def delete_session(
    database_path: str | Path,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
) -> None:
    workspace = _token(workspace_id, "workspace_id")
    owner = _token(str(owner_id).lower(), "owner_id")
    canonical = _uuid(session_id, "session_id")
    with _opened_connection(database_path) as connection:
        cursor = connection.execute(
            "DELETE FROM chat_sessions WHERE workspace_id=? AND owner_id=? AND id=?",
            (workspace, owner, canonical),
        )
    if cursor.rowcount != 1:
        raise SessionNotFound("session not found")


SESSION_POLICY = {
    "privacy": "private_owner_only",
    "default_retention_days": DEFAULT_RETENTION_DAYS,
    "maximum_retention_days": MAX_RETENTION_DAYS,
    "maximum_sessions_per_owner": MAX_SESSIONS_PER_OWNER,
    "maximum_turns_per_session": MAX_TURNS_PER_SESSION,
    "maximum_turn_characters": MAX_TURN_CHARACTERS,
    "earlier_turns_enter_prompts": False,
    "export": "GET session returns bounded turns",
    "deletion": "DELETE permanently removes the session and its turns",
    "redaction": "No automatic redaction; do not store secrets or raw image bytes",
    "sharing": "not_supported",
}


__all__ = [
    "ChatSession",
    "ChatTurn",
    "SESSION_POLICY",
    "SessionLimitExceeded",
    "SessionNotFound",
    "add_user_turn",
    "add_unverified_assistant_turn",
    "add_verified_assistant_turn",
    "create_session",
    "delete_session",
    "get_session",
    "list_sessions",
    "list_turns",
    "purge_expired_sessions",
]
