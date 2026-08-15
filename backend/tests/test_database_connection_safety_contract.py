"""Red-first contracts for PR4 SQLite connection and readiness safety.

These tests intentionally use disposable databases.  The Python runtime used by
the local test environment currently embeds SQLite 3.50.4, so tests never claim
that its WAL implementation is safe: production-mode WAL is exercised only as
an expected fail-closed path, and ordinary integration checks switch their
temporary database back to DELETE journalling before authoritative writes.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.routers.services import database, sqlite_runtime
from app.routers.services.db_schema import LATEST_SCHEMA_VERSION


MAX_BUSY_TIMEOUT_MS = 30_000


def _pragma_int(connection: sqlite3.Connection, name: str) -> int:
    row = connection.execute(f"PRAGMA {name}").fetchone()
    assert row is not None, f"PRAGMA {name} returned no value"
    return int(row[0])


def _application_tables(db_path: Path) -> set[str]:
    if not db_path.exists():
        return set()
    connection = sqlite3.connect(str(db_path))
    try:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
    finally:
        connection.close()


def _force_delete_journal(db_path: Path) -> None:
    """Keep integration tests off the affected local WAL implementation."""

    connection = sqlite3.connect(str(db_path))
    try:
        mode = str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0])
    finally:
        connection.close()
    assert mode.lower() == "delete"


def _response_payload(response: Any) -> tuple[int, dict[str, Any]]:
    """Normalize FastAPI plain-dict and JSONResponse return values."""

    if isinstance(response, dict):
        return 200, response
    return int(response.status_code), json.loads(response.body)


def test_init_db_fails_closed_before_schema_writes_for_unsafe_production_wal(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """An affected runtime may not create/migrate a production WAL database."""

    db_path = test_workspace / "unsafe-production.db"
    monkeypatch.setenv("BRIDGEGUARD_ENV", "production")
    monkeypatch.delenv("BRIDGEGUARD_SQLITE_WAL_FIX_BACKPORT", raising=False)
    monkeypatch.delenv("BRIDGEGUARD_SQLITE_WAL_FIX_REFERENCE", raising=False)
    monkeypatch.setattr(
        sqlite_runtime.sqlite3,
        "sqlite_version_info",
        (3, 50, 4),
    )
    monkeypatch.setattr(database, "init_db_if_needed", lambda: None)

    raised: RuntimeError | None = None
    try:
        database.init_db(str(db_path))
    except RuntimeError as exc:
        raised = exc

    # Assert ordering, not merely that startup eventually noticed the issue.
    assert _application_tables(db_path) == set(), (
        "init_db performed application schema writes before rejecting the "
        "unsafe WAL runtime"
    )
    assert raised is not None
    assert "Unsafe SQLite WAL runtime" in str(raised)


def test_lifespan_honors_explicit_delete_mode_before_database_initialization(
    monkeypatch: pytest.MonkeyPatch,
):
    """Local DELETE mode must not be mislabeled and rejected as WAL."""

    from app import main

    observed: list[str] = []
    monkeypatch.setenv("BRIDGEGUARD_ENV", "development")
    monkeypatch.setenv("BRIDGEGUARD_SQLITE_JOURNAL_MODE", "delete")
    monkeypatch.setattr(main, "require_safe_sqlite_wal_runtime", observed.append)
    monkeypatch.setattr(
        main,
        "require_deployment_policy",
        lambda **_kwargs: type(
            "Policy", (), {"mode": main.DeploymentMode.RESEARCH_SHARED}
        )(),
    )
    monkeypatch.setattr(main, "init_db", lambda _path: (_ for _ in ()).throw(
        RuntimeError("stop after journal-mode gate")
    ))

    async def enter_lifespan() -> None:
        context = main.lifespan(main.app)
        await context.__aenter__()

    with pytest.raises(RuntimeError, match="stop after journal-mode gate"):
        asyncio.run(enter_lifespan())

    assert observed == ["delete"]


def test_every_database_connection_enforces_bounded_defensive_pragmas(
    test_workspace: Path,
):
    db_path = test_workspace / "connection-policy.db"

    # Opening more than once guards against a one-time bootstrap side effect;
    # the policy belongs to every connection.
    for _ in range(2):
        connection = database.get_connection(str(db_path))
        try:
            assert _pragma_int(connection, "foreign_keys") == 1
            assert _pragma_int(connection, "trusted_schema") == 0
            busy_timeout_ms = _pragma_int(connection, "busy_timeout")
            assert 0 < busy_timeout_ms <= MAX_BUSY_TIMEOUT_MS
        finally:
            connection.close()


def test_authoritative_load_uses_full_sync_and_acquires_writer_lock_first(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    db_path = test_workspace / "authoritative-load.db"
    database.ensure_schema(str(db_path))
    _force_delete_journal(db_path)

    trace: list[str] = []
    real_connection = sqlite3.connect(str(db_path), check_same_thread=False)
    real_connection.set_trace_callback(trace.append)

    # The schema is already initialized and deliberately in DELETE mode.  This
    # isolates the write-policy contract from startup/WAL selection and lets us
    # inspect the exact connection used by the authoritative load.
    monkeypatch.setattr(database, "ensure_schema", lambda _path: None)
    monkeypatch.setattr(database, "get_connection", lambda _path: real_connection)

    try:
        result = database.load_json_to_db(
            str(db_path),
            [{"BL01": 34, "BID01": "PR4-CONNECTION-SAFETY"}],
            data_year_override=2025,
        )
    finally:
        # The loader owns and normally closes the returned connection. Close
        # again on every failure path so Windows never retains a test handle.
        try:
            real_connection.close()
        except sqlite3.Error:
            pass

    assert result[:3] == (1, 0, 0), result
    statements = [" ".join(statement.upper().split()) for statement in trace]
    full_sync_indexes = [
        index
        for index, statement in enumerate(statements)
        if statement in {"PRAGMA SYNCHRONOUS = FULL;", "PRAGMA SYNCHRONOUS = FULL"}
    ]
    begin_indexes = [
        index
        for index, statement in enumerate(statements)
        if statement in {"BEGIN IMMEDIATE", "BEGIN IMMEDIATE;"}
    ]
    assert full_sync_indexes, f"authoritative load did not request FULL sync: {trace!r}"
    assert begin_indexes, f"authoritative load did not acquire BEGIN IMMEDIATE: {trace!r}"
    assert full_sync_indexes[0] < begin_indexes[0]
    assert not any("SYNCHRONOUS = NORMAL" in statement for statement in statements)


def test_readiness_reports_connection_integrity_and_migration_state(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Readiness must expose actionable state without paths or record data."""

    db_path = test_workspace / "readiness.db"
    database.ensure_schema(str(db_path))
    _force_delete_journal(db_path)

    # Importing main is intentionally delayed until the isolated test paths from
    # conftest are active.  Embedding readiness is unrelated to this contract.
    from app import main
    from app.routers.services import llm_providers

    monkeypatch.setattr(main, "USER_DB", str(db_path))
    monkeypatch.setattr(llm_providers, "verify_embedding_backend", lambda timeout=5: None)

    status_code, payload = _response_payload(main.ready())

    assert status_code == 200, payload
    assert payload["status"] == "ready"
    sqlite_details = payload["details"]["sqlite"]

    expected_diagnostic_fields = {
        "runtime_version",
        "journal_mode",
        "wal_enabled",
        "wal_fix_present",
        "approved",
        "fix_source",
        "reason",
        "foreign_keys",
        "trusted_schema",
        "synchronous",
        "busy_timeout_ms",
        "wal_checkpoint",
        "quick_check",
        "user_version",
        "latest_schema_version",
        "migration_current",
    }
    assert expected_diagnostic_fields <= set(sqlite_details)
    assert sqlite_details["approved"] is True
    assert str(sqlite_details["journal_mode"]).lower() == "delete"
    assert sqlite_details["foreign_keys"] in (True, 1)
    assert sqlite_details["trusted_schema"] in (False, 0)
    assert 0 < int(sqlite_details["busy_timeout_ms"]) <= MAX_BUSY_TIMEOUT_MS
    assert str(sqlite_details["synchronous"]).lower() in {"full", "2"}
    assert sqlite_details["quick_check"] == "ok"
    assert int(sqlite_details["user_version"]) == LATEST_SCHEMA_VERSION
    assert int(sqlite_details["latest_schema_version"]) == LATEST_SCHEMA_VERSION
    assert sqlite_details["migration_current"] is True

    # Diagnostics are operational metadata only: never echo DB paths or rows.
    serialized = json.dumps(payload, sort_keys=True)
    assert str(db_path) not in serialized
    assert "PR4-CONNECTION-SAFETY" not in serialized
