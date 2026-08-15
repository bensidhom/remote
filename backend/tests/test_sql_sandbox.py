from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
import json

import pytest

from app.services.query.registry import (
    CancelOutcome,
    InvalidRequestId,
    RequestAlreadyRegistered,
    RequestRegistry,
    new_request_id,
    validate_request_id,
)
from app.services.query.sandbox import (
    QueryByteLimitExceeded,
    QueryCancelled,
    QueryCellLimitExceeded,
    QueryColumnLimitExceeded,
    QueryDeadlineExceeded,
    QueryDenied,
    QueryInputError,
    QueryOutcome,
    QueryVmBudgetExceeded,
    SandboxPolicy,
    execute_read_query,
)


def _database(root: Path, *, large: bool = False) -> Path:
    path = root / "sandbox.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE inventory (
                bridge_id TEXT NOT NULL,
                county TEXT NOT NULL,
                secret_note TEXT
            );
            INSERT INTO inventory VALUES
                ('A-1', 'Camden', 'private-a'),
                ('A-2', 'Gloucester', 'private-b'),
                ('A-3', 'Camden', 'private-c');
            CREATE VIEW v_inventory AS
                SELECT bridge_id, county FROM inventory;
            """
        )
        if large:
            connection.execute("CREATE TABLE numbers (n INTEGER NOT NULL)")
            connection.executemany(
                "INSERT INTO numbers(n) VALUES (?)", ((number,) for number in range(500))
            )
        connection.commit()
    finally:
        connection.close()
    return path


def _execute(
    path: Path,
    sql: str,
    *,
    registry: RequestRegistry | None = None,
    policy: SandboxPolicy | None = None,
    allowed_relations=None,
):
    return execute_read_query(
        database_path=path,
        sql=sql,
        request_id=new_request_id(),
        owner_id="user@example.test",
        workspace_id="workspace-a",
        allowed_relations=allowed_relations or {"inventory": None},
        registry=registry or RequestRegistry(),
        policy=policy or SandboxPolicy(),
    )


def test_executes_approved_select_and_returns_bounded_typed_evidence(test_workspace):
    path = _database(test_workspace)

    result = _execute(
        path,
        "SELECT county, COUNT(*) AS bridges "
        "FROM inventory GROUP BY county ORDER BY county",
    )

    assert result.columns == ("county", "bridges")
    assert result.rows == (("Camden", 2), ("Gloucester", 1))
    assert result.row_count == 2
    assert result.outcome is QueryOutcome.COMPLETED
    assert result.truncated is False
    assert 0 < result.serialized_bytes <= 1024 * 1024
    encoded = json.dumps(
        {"columns": result.columns, "rows": result.rows},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert result.serialized_bytes == len(encoded)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM inventory",
        "UPDATE inventory SET county='X'",
        "INSERT INTO inventory VALUES ('X', 'X', 'X')",
        "CREATE TABLE stolen(value TEXT)",
        "DROP TABLE inventory",
        "ATTACH DATABASE ':memory:' AS stolen",
        "PRAGMA table_info(inventory)",
        "EXPLAIN SELECT * FROM inventory",
    ],
)
def test_rejects_writes_schema_mutation_attach_pragma_and_explain(test_workspace, sql):
    path = _database(test_workspace)
    before = path.read_bytes()

    with pytest.raises(QueryDenied):
        _execute(path, sql)

    assert path.read_bytes() == before


def test_authorizer_denies_schema_unapproved_relations_and_columns(test_workspace):
    path = _database(test_workspace)

    with pytest.raises(QueryDenied):
        _execute(path, "SELECT name FROM sqlite_schema")
    with pytest.raises(QueryDenied):
        _execute(
            path,
            "SELECT secret_note FROM inventory",
            allowed_relations={"inventory": {"bridge_id", "county"}},
        )
    with pytest.raises(QueryDenied):
        _execute(path, "SELECT * FROM v_inventory")
    with pytest.raises(QueryInputError):
        _execute(
            path,
            "SELECT * FROM pragma_table_info('inventory')",
            allowed_relations={"pragma_table_info": None},
        )


def test_count_star_works_with_an_explicit_relation_column_allowlist(test_workspace):
    path = _database(test_workspace)
    result = _execute(
        path,
        "SELECT COUNT(*) AS bridge_count FROM inventory",
        allowed_relations={"inventory": {"bridge_id", "county"}},
    )
    assert result.rows == ((3,),)


def test_approved_view_does_not_grant_direct_access_to_its_base_table(test_workspace):
    path = _database(test_workspace)

    result = _execute(
        path,
        "SELECT bridge_id FROM v_inventory ORDER BY bridge_id",
        allowed_relations={"v_inventory": {"bridge_id", "county"}},
    )
    assert result.rows == (("A-1",), ("A-2",), ("A-3",))

    with pytest.raises(QueryDenied):
        _execute(
            path,
            "SELECT bridge_id FROM inventory",
            allowed_relations={"v_inventory": {"bridge_id", "county"}},
        )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT random() FROM inventory",
        "SELECT load_extension('anything') FROM inventory",
        "SELECT readfile('anything') FROM inventory",
        "SELECT * FROM pragma_table_info('inventory')",
    ],
)
def test_denies_functions_outside_explicit_safe_allowlist(test_workspace, sql):
    path = _database(test_workspace)
    with pytest.raises(QueryDenied):
        _execute(path, sql)


def test_row_cap_fetches_only_cap_plus_one_and_marks_truncation(test_workspace):
    path = _database(test_workspace)
    result = _execute(
        path,
        "SELECT bridge_id FROM inventory ORDER BY bridge_id",
        policy=SandboxPolicy(max_rows=2, fetch_size=2),
    )
    assert result.rows == (("A-1",), ("A-2",))
    assert result.row_count == 2
    assert result.truncated is True
    assert result.outcome is QueryOutcome.TRUNCATED


def test_result_column_cell_and_serialized_byte_caps(test_workspace):
    path = _database(test_workspace)

    with pytest.raises(QueryColumnLimitExceeded):
        _execute(
            path,
            "SELECT bridge_id, county, secret_note FROM inventory",
            policy=SandboxPolicy(max_result_columns=2),
        )
    with pytest.raises(QueryCellLimitExceeded):
        _execute(
            path,
            "SELECT secret_note FROM inventory",
            policy=SandboxPolicy(max_cell_bytes=4),
        )
    with pytest.raises(QueryByteLimitExceeded):
        _execute(
            path,
            "SELECT secret_note FROM inventory",
            policy=SandboxPolicy(max_cell_bytes=32, max_serialized_bytes=50),
        )


def test_progress_handler_enforces_wall_clock_deadline(test_workspace):
    path = _database(test_workspace, large=True)
    with pytest.raises(QueryDeadlineExceeded):
        _execute(
            path,
            "SELECT SUM(a.n * b.n * c.n) "
            "FROM numbers a CROSS JOIN numbers b CROSS JOIN numbers c",
            allowed_relations={"numbers": {"n"}},
            policy=SandboxPolicy(
                deadline_seconds=0.01,
                progress_opcode_interval=100,
                max_progress_callbacks=100_000,
            ),
        )


def test_progress_handler_enforces_vm_callback_budget(test_workspace):
    path = _database(test_workspace, large=True)
    with pytest.raises(QueryVmBudgetExceeded):
        _execute(
            path,
            "SELECT SUM(a.n * b.n) FROM numbers a CROSS JOIN numbers b",
            allowed_relations={"numbers": {"n"}},
            policy=SandboxPolicy(
                deadline_seconds=1,
                progress_opcode_interval=100,
                max_progress_callbacks=2,
            ),
        )


def test_cancel_is_request_owner_and_workspace_scoped_and_interrupts_only_target(
    test_workspace,
):
    path = _database(test_workspace, large=True)
    registry = RequestRegistry()
    request_id = new_request_id()
    result: dict[str, object] = {}

    def run_query() -> None:
        try:
            result["value"] = execute_read_query(
                database_path=path,
                sql="SELECT SUM(a.n * b.n * c.n) "
                "FROM numbers a CROSS JOIN numbers b CROSS JOIN numbers c",
                request_id=request_id,
                owner_id="owner@example.test",
                workspace_id="workspace-a",
                allowed_relations={"numbers": {"n"}},
                registry=registry,
            )
        except BaseException as exc:  # captured for assertion in the test thread
            result["error"] = exc

    thread = threading.Thread(target=run_query, daemon=True)
    thread.start()
    limit = time.monotonic() + 1
    while not registry.is_active(request_id) and time.monotonic() < limit:
        time.sleep(0.001)
    assert registry.is_active(request_id)

    assert registry.cancel(
        request_id=request_id,
        owner_id="other@example.test",
        workspace_id="workspace-a",
    ) is CancelOutcome.NOT_FOUND
    assert registry.cancel(
        request_id=request_id,
        owner_id="owner@example.test",
        workspace_id="workspace-b",
    ) is CancelOutcome.NOT_FOUND
    assert registry.cancel(
        request_id=request_id,
        owner_id="owner@example.test",
        workspace_id="workspace-a",
    ) is CancelOutcome.CANCELLED

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert isinstance(result.get("error"), QueryCancelled)
    assert registry.active_count == 0
    assert registry.cancel(
        request_id=request_id,
        owner_id="owner@example.test",
        workspace_id="workspace-a",
    ) is CancelOutcome.NOT_FOUND


def test_registry_rejects_noncanonical_and_duplicate_request_ids():
    registry = RequestRegistry()
    request_id = new_request_id()
    assert validate_request_id(request_id) == request_id
    with pytest.raises(InvalidRequestId):
        validate_request_id(request_id.upper())
    with pytest.raises(InvalidRequestId):
        validate_request_id("123")

    deadline = time.monotonic() + 2
    registry.register(
        request_id=request_id,
        owner_id="owner",
        workspace_id="workspace",
        operation="query",
        deadline=deadline,
    )
    with pytest.raises(RequestAlreadyRegistered):
        registry.register(
            request_id=request_id,
            owner_id="owner",
            workspace_id="workspace",
            operation="query",
            deadline=deadline,
        )
    registry.finish(request_id)


def test_registry_cleanup_is_deterministic_after_success_and_failure(test_workspace):
    path = _database(test_workspace)
    registry = RequestRegistry()

    _execute(path, "SELECT bridge_id FROM inventory", registry=registry)
    assert registry.active_count == 0

    with pytest.raises(QueryDenied):
        _execute(path, "SELECT name FROM sqlite_schema", registry=registry)
    assert registry.active_count == 0


def test_policy_caps_cannot_be_raised_by_a_caller():
    with pytest.raises(ValueError):
        SandboxPolicy(deadline_seconds=5.1)
    with pytest.raises(ValueError):
        SandboxPolicy(max_rows=201)
    with pytest.raises(ValueError):
        SandboxPolicy(max_result_columns=41)
    with pytest.raises(ValueError):
        SandboxPolicy(max_cell_bytes=16 * 1024 + 1)
    with pytest.raises(ValueError):
        SandboxPolicy(max_serialized_bytes=1024 * 1024 + 1)
    with pytest.raises(ValueError):
        SandboxPolicy(sqlite_max_attached=1)


def test_sql_length_and_unterminated_leading_comments_are_rejected(test_workspace):
    path = _database(test_workspace)
    with pytest.raises(QueryInputError):
        _execute(path, "/* never closed")
    with pytest.raises(QueryInputError):
        _execute(
            path,
            "SELECT " + "x" * 100,
            policy=SandboxPolicy(sqlite_max_sql_bytes=64),
        )
