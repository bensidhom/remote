"""Bounded, read-only SQLite execution for untrusted generated queries.

This module is the final execution boundary, not an SQL parser.  A semantic
query compiler or AST validator should reject structurally invalid queries
before calling it; SQLite then independently enforces read-only access,
relation/function allowlists, runtime limits, and request-owned cancellation.
"""

from __future__ import annotations

import base64
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Any

from .registry import DEFAULT_REQUEST_REGISTRY, RequestRegistry


# Full-state aggregates over reviewed current-snapshot views can legitimately
# scan several hundred thousand historical child rows on the production-sized
# SQLite database.  Five seconds remains a strict server-owned deadline while
# allowing those bounded aggregates to complete.
HARD_MAX_DEADLINE_SECONDS = 5.0
HARD_MAX_ROWS = 200
HARD_MAX_RESULT_COLUMNS = 40
HARD_MAX_CELL_BYTES = 16 * 1024
HARD_MAX_SERIALIZED_BYTES = 1024 * 1024


DEFAULT_SAFE_FUNCTIONS = frozenset(
    {
        "abs",
        "avg",
        "cast",
        "coalesce",
        "concat",
        "concat_ws",
        "count",
        "date",
        "datetime",
        "glob",
        "hex",
        "ifnull",
        "instr",
        "julianday",
        "length",
        "like",
        "likely",
        "likelihood",
        "lower",
        "ltrim",
        "max",
        "min",
        "nullif",
        "printf",
        "quote",
        "replace",
        "round",
        "rtrim",
        "strftime",
        "substr",
        "substring",
        "sum",
        "time",
        "total",
        "trim",
        "typeof",
        "unicode",
        "unixepoch",
        "unlikely",
        "upper",
        "zeroblob",
    }
)


class QueryOutcome(StrEnum):
    COMPLETED = "completed"
    TRUNCATED = "truncated"


class SandboxError(RuntimeError):
    """Typed, client-safe query failure."""

    code = "sandbox_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class QueryInputError(SandboxError):
    code = "invalid_query_input"


class QueryDatabaseError(SandboxError):
    code = "query_database_unavailable"


class QueryDenied(SandboxError):
    code = "query_denied"


class QueryCancelled(SandboxError):
    code = "query_cancelled"


class QueryDeadlineExceeded(SandboxError):
    code = "query_deadline_exceeded"


class QueryVmBudgetExceeded(SandboxError):
    code = "query_vm_budget_exceeded"


class QueryColumnLimitExceeded(SandboxError):
    code = "query_column_limit_exceeded"


class QueryCellLimitExceeded(SandboxError):
    code = "query_cell_limit_exceeded"


class QueryByteLimitExceeded(SandboxError):
    code = "query_byte_limit_exceeded"


class QueryExecutionError(SandboxError):
    code = "query_execution_error"


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """Server-owned limits; callers cannot raise them above the hard ceilings."""

    deadline_seconds: float = HARD_MAX_DEADLINE_SECONDS
    busy_timeout_ms: int = 150
    progress_opcode_interval: int = 1_000
    max_progress_callbacks: int = 50_000
    fetch_size: int = 64
    max_rows: int = HARD_MAX_ROWS
    max_result_columns: int = HARD_MAX_RESULT_COLUMNS
    max_cell_bytes: int = HARD_MAX_CELL_BYTES
    max_serialized_bytes: int = HARD_MAX_SERIALIZED_BYTES
    sqlite_max_sql_bytes: int = 32 * 1024
    sqlite_max_expr_depth: int = 50
    # Keep this above the widest approved source schema. Result columns are
    # independently and strictly capped at 40 below.
    sqlite_max_schema_columns: int = 512
    sqlite_max_attached: int = 0
    sqlite_max_compound_selects: int = 4
    sqlite_max_variables: int = 100
    sqlite_max_function_args: int = 16
    sqlite_max_like_pattern_bytes: int = 256
    sqlite_max_value_bytes: int = HARD_MAX_SERIALIZED_BYTES
    sqlite_max_vdbe_ops: int = 250_000

    def __post_init__(self) -> None:
        _bounded("deadline_seconds", self.deadline_seconds, 0, HARD_MAX_DEADLINE_SECONDS)
        _bounded("busy_timeout_ms", self.busy_timeout_ms, 0, 1_000, inclusive_min=True)
        _bounded("progress_opcode_interval", self.progress_opcode_interval, 0, 10_000)
        _bounded("max_progress_callbacks", self.max_progress_callbacks, 0, 100_000)
        _bounded("fetch_size", self.fetch_size, 0, HARD_MAX_ROWS + 1)
        _bounded("max_rows", self.max_rows, 0, HARD_MAX_ROWS)
        _bounded(
            "max_result_columns",
            self.max_result_columns,
            0,
            HARD_MAX_RESULT_COLUMNS,
        )
        _bounded("max_cell_bytes", self.max_cell_bytes, 0, HARD_MAX_CELL_BYTES)
        _bounded(
            "max_serialized_bytes",
            self.max_serialized_bytes,
            0,
            HARD_MAX_SERIALIZED_BYTES,
        )
        _bounded("sqlite_max_sql_bytes", self.sqlite_max_sql_bytes, 63, 32 * 1024)
        _bounded("sqlite_max_expr_depth", self.sqlite_max_expr_depth, 0, 100)
        _bounded(
            "sqlite_max_schema_columns", self.sqlite_max_schema_columns, 39, 512
        )
        if self.sqlite_max_attached != 0:
            raise ValueError("sqlite_max_attached must remain zero")
        _bounded(
            "sqlite_max_compound_selects", self.sqlite_max_compound_selects, 0, 4
        )
        _bounded("sqlite_max_variables", self.sqlite_max_variables, 0, 100)
        _bounded("sqlite_max_function_args", self.sqlite_max_function_args, 0, 16)
        _bounded(
            "sqlite_max_like_pattern_bytes",
            self.sqlite_max_like_pattern_bytes,
            0,
            256,
        )
        _bounded(
            "sqlite_max_value_bytes",
            self.sqlite_max_value_bytes,
            63,
            HARD_MAX_SERIALIZED_BYTES,
        )
        _bounded("sqlite_max_vdbe_ops", self.sqlite_max_vdbe_ops, 99, 250_000)


def _bounded(
    name: str,
    value: float,
    minimum: float,
    maximum: float,
    *,
    inclusive_min: bool = False,
) -> None:
    lower_ok = value >= minimum if inclusive_min else value > minimum
    if not lower_ok or value > maximum:
        boundary = "at least" if inclusive_min else "greater than"
        raise ValueError(f"{name} must be {boundary} {minimum} and at most {maximum}")


DEFAULT_SANDBOX_POLICY = SandboxPolicy()


JsonCell = None | bool | int | float | str | dict[str, str]


@dataclass(frozen=True, slots=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[JsonCell, ...], ...]
    row_count: int
    truncated: bool
    serialized_bytes: int
    elapsed_ms: int
    progress_callbacks: int
    outcome: QueryOutcome


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def execute_read_query(
    *,
    database_path: str | Path,
    sql: str,
    request_id: str,
    owner_id: str,
    workspace_id: str,
    allowed_relations: Mapping[str, Iterable[str] | None],
    parameters: Sequence[object] | Mapping[str, object] = (),
    allowed_functions: Iterable[str] = DEFAULT_SAFE_FUNCTIONS,
    policy: SandboxPolicy = DEFAULT_SANDBOX_POLICY,
    registry: RequestRegistry = DEFAULT_REQUEST_REGISTRY,
) -> QueryResult:
    """Execute one bounded SELECT and return JSON-safe evidence.

    ``allowed_relations`` maps approved table/view names to either an explicit
    column allowlist or ``None`` (all columns exposed by that approved relation).
    Names are case-insensitive but must be simple SQLite identifiers.
    """

    started = time.monotonic()
    _validate_sql(sql, policy)
    relations = _normalize_relations(allowed_relations)
    functions = _normalize_functions(allowed_functions)
    path = _resolve_database(database_path)
    deadline = started + policy.deadline_seconds
    entry = registry.register(
        request_id=request_id,
        owner_id=owner_id,
        workspace_id=workspace_id,
        operation="sqlite_query",
        deadline=deadline,
    )

    connection: sqlite3.Connection | None = None
    cursor: sqlite3.Cursor | None = None
    progress_count = 0
    termination: str | None = None
    denial: str | None = None

    def progress() -> int:
        nonlocal progress_count, termination
        progress_count += 1
        if entry.cancellation_token.is_set():
            termination = "cancelled"
            return 1
        if time.monotonic() >= deadline:
            termination = "deadline"
            return 1
        if progress_count > policy.max_progress_callbacks:
            termination = "vm_budget"
            return 1
        return 0

    def authorize(
        action: int,
        argument_1: str | None,
        argument_2: str | None,
        database_name: str | None,
        source: str | None,
    ) -> int:
        nonlocal denial
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            relation = (argument_1 or "").casefold()
            column = (argument_2 or "").casefold()
            view_source = (source or "").casefold()
            database = (database_name or "").casefold()
            if database not in {"", "main"}:
                denial = "only the approved main database is readable"
                return sqlite3.SQLITE_DENY
            if _is_internal_relation(relation):
                denial = "schema access is not allowed"
                return sqlite3.SQLITE_DENY
            permitted_columns = relations.get(relation)
            if relation in relations and (
                permitted_columns is None
                or not column  # COUNT(*) / SELECT constant FROM approved relation
                or column in permitted_columns
            ):
                return sqlite3.SQLITE_OK
            # SQLite expands an approved view and authorizes its underlying
            # reads with the view name as the source. Direct access to those
            # underlying relations still has no approved source and is denied.
            if view_source in relations:
                return sqlite3.SQLITE_OK
            denial = "relation or column is not approved"
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            function_name = (argument_2 or argument_1 or "").casefold()
            if function_name in functions:
                return sqlite3.SQLITE_OK
            denial = "function is not approved"
            return sqlite3.SQLITE_DENY
        denial = "operation is not read-only"
        return sqlite3.SQLITE_DENY

    try:
        connection = _open_read_only(path, policy)
        registry.attach_connection(request_id, connection)
        connection.set_authorizer(authorize)
        connection.set_progress_handler(progress, policy.progress_opcode_interval)
        if entry.cancellation_token.is_set():
            raise QueryCancelled("The query was cancelled.")

        cursor = connection.execute(sql, parameters)
        description = cursor.description or ()
        if len(description) > policy.max_result_columns:
            raise QueryColumnLimitExceeded(
                f"Query results may contain at most {policy.max_result_columns} columns."
            )
        columns = tuple(str(item[0]) for item in description)
        rows, truncated, serialized_bytes = _fetch_bounded(
            cursor=cursor,
            columns=columns,
            policy=policy,
            progress_state=lambda: (entry.cancelled, time.monotonic() >= deadline),
        )
        if time.monotonic() >= deadline:
            raise QueryDeadlineExceeded("The query exceeded its execution deadline.")
        if not registry.finish(request_id):
            raise QueryCancelled("The query was cancelled.")
        elapsed_ms = max(0, round((time.monotonic() - started) * 1_000))
        return QueryResult(
            columns=columns,
            rows=tuple(rows),
            row_count=len(rows),
            truncated=truncated,
            serialized_bytes=serialized_bytes,
            elapsed_ms=elapsed_ms,
            progress_callbacks=progress_count,
            outcome=QueryOutcome.TRUNCATED if truncated else QueryOutcome.COMPLETED,
        )
    except SandboxError:
        raise
    except (sqlite3.Error, MemoryError) as exc:
        if entry.cancelled or termination == "cancelled":
            raise QueryCancelled("The query was cancelled.") from exc
        if termination == "deadline" or time.monotonic() >= deadline:
            raise QueryDeadlineExceeded("The query exceeded its execution deadline.") from exc
        if termination == "vm_budget":
            raise QueryVmBudgetExceeded("The query exceeded its VM-step budget.") from exc
        if denial is not None:
            raise QueryDenied(denial) from exc
        message = str(exc).casefold()
        if "too many columns" in message:
            raise QueryColumnLimitExceeded(
                f"Query results may contain at most {policy.max_result_columns} columns."
            ) from exc
        if "string or blob too big" in message or isinstance(exc, MemoryError):
            raise QueryByteLimitExceeded("The query exceeded its result-size limit.") from exc
        raise QueryExecutionError("The query could not be executed safely.") from exc
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except sqlite3.Error:
                pass
        if connection is not None:
            try:
                connection.set_progress_handler(None, 0)
                connection.set_authorizer(None)
            except sqlite3.Error:
                pass
            try:
                connection.close()
            except sqlite3.Error:
                pass
        registry.finish(request_id)


def _resolve_database(database_path: str | Path) -> Path:
    try:
        path = Path(database_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as exc:
        raise QueryDatabaseError("The query database is unavailable.") from exc
    if not path.is_file():
        raise QueryDatabaseError("The query database is unavailable.")
    return path


def _open_read_only(path: Path, policy: SandboxPolicy) -> sqlite3.Connection:
    uri = f"{path.as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=policy.busy_timeout_ms / 1_000,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.execute(f"PRAGMA busy_timeout={policy.busy_timeout_ms}")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        try:
            connection.enable_load_extension(False)
        except (AttributeError, sqlite3.Error):
            pass
        _apply_runtime_limits(connection, policy)
        return connection
    except sqlite3.Error as exc:
        try:
            connection.close()  # type: ignore[possibly-undefined]
        except (NameError, sqlite3.Error):
            pass
        raise QueryDatabaseError("The query database is unavailable.") from exc


def _apply_runtime_limits(
    connection: sqlite3.Connection,
    policy: SandboxPolicy,
) -> None:
    configured = (
        ("SQLITE_LIMIT_SQL_LENGTH", policy.sqlite_max_sql_bytes),
        ("SQLITE_LIMIT_EXPR_DEPTH", policy.sqlite_max_expr_depth),
        ("SQLITE_LIMIT_COLUMN", policy.sqlite_max_schema_columns),
        ("SQLITE_LIMIT_ATTACHED", policy.sqlite_max_attached),
        ("SQLITE_LIMIT_COMPOUND_SELECT", policy.sqlite_max_compound_selects),
        ("SQLITE_LIMIT_VARIABLE_NUMBER", policy.sqlite_max_variables),
        ("SQLITE_LIMIT_FUNCTION_ARG", policy.sqlite_max_function_args),
        ("SQLITE_LIMIT_LIKE_PATTERN_LENGTH", policy.sqlite_max_like_pattern_bytes),
        ("SQLITE_LIMIT_LENGTH", policy.sqlite_max_value_bytes),
        ("SQLITE_LIMIT_VDBE_OP", policy.sqlite_max_vdbe_ops),
    )
    setter = getattr(connection, "setlimit", None)
    if setter is None:
        raise QueryDatabaseError("The SQLite runtime does not support safety limits.")
    for constant_name, limit in configured:
        category = getattr(sqlite3, constant_name, None)
        if category is None:
            continue
        try:
            setter(category, limit)
        except (sqlite3.Error, ValueError) as exc:
            raise QueryDatabaseError("The SQLite safety limits could not be applied.") from exc


def _validate_sql(sql: str, policy: SandboxPolicy) -> None:
    if not isinstance(sql, str) or not sql.strip():
        raise QueryInputError("SQL must be a non-empty string.")
    if "\x00" in sql:
        raise QueryInputError("SQL cannot contain NUL bytes.")
    if len(sql.encode("utf-8")) > policy.sqlite_max_sql_bytes:
        raise QueryInputError("SQL exceeds the configured length limit.")
    first = _first_keyword(sql)
    if first not in {"SELECT", "WITH"}:
        raise QueryDenied("Only SELECT queries are allowed.")
    # Some SQLite CLI-only helpers (notably readfile/writefile) may not be
    # registered in the embedded runtime, in which case no FUNCTION authorizer
    # callback occurs. Reject their call syntax explicitly so policy is stable
    # across SQLite builds and installed extensions.
    if re.search(
        r"\b(?:load_extension|readfile|writefile|fts3_tokenizer)\s*\(",
        sql,
        flags=re.IGNORECASE,
    ):
        raise QueryDenied("File and extension functions are not allowed.")


def _first_keyword(sql: str) -> str:
    """Return the first token after whitespace and leading SQL comments."""

    position = 0
    length = len(sql)
    while position < length:
        while position < length and sql[position].isspace():
            position += 1
        if sql.startswith("--", position):
            newline = sql.find("\n", position + 2)
            position = length if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", position):
            end = sql.find("*/", position + 2)
            if end < 0:
                raise QueryInputError("SQL contains an unterminated comment.")
            position = end + 2
            continue
        break
    match = re.match(r"[A-Za-z]+", sql[position:])
    return match.group(0).upper() if match else ""


def _normalize_relations(
    allowed: Mapping[str, Iterable[str] | None],
) -> dict[str, frozenset[str] | None]:
    if not isinstance(allowed, Mapping) or not allowed:
        raise QueryInputError("At least one approved relation is required.")
    normalized: dict[str, frozenset[str] | None] = {}
    for relation, columns in allowed.items():
        name = _safe_identifier(relation, "relation")
        if _is_internal_relation(name):
            raise QueryInputError("SQLite schema relations cannot be approved.")
        if columns is None:
            normalized[name] = None
            continue
        if isinstance(columns, (str, bytes)):
            raise QueryInputError("Approved columns must be an iterable of names.")
        column_set = frozenset(_safe_identifier(column, "column") for column in columns)
        if not column_set:
            raise QueryInputError("An explicit column allowlist cannot be empty.")
        normalized[name] = column_set
    return normalized


def _normalize_functions(allowed: Iterable[str]) -> frozenset[str]:
    if isinstance(allowed, (str, bytes)):
        raise QueryInputError("Approved functions must be an iterable of names.")
    return frozenset(_safe_identifier(name, "function") for name in allowed)


def _safe_identifier(value: object, kind: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise QueryInputError(f"Approved {kind} names must be simple identifiers.")
    return value.casefold()


def _is_internal_relation(name: str) -> bool:
    return name.startswith(("sqlite_", "pragma_")) or name in {
        "dbstat",
        "dbpage",
    }


def _fetch_bounded(
    *,
    cursor: sqlite3.Cursor,
    columns: tuple[str, ...],
    policy: SandboxPolicy,
    progress_state: Any,
) -> tuple[list[tuple[JsonCell, ...]], bool, int]:
    columns_json = _json_bytes(columns)
    serialized_bytes = len(b'{"columns":') + len(columns_json) + len(b',"rows":[') + len(b"]}")
    if serialized_bytes > policy.max_serialized_bytes:
        raise QueryByteLimitExceeded("The query exceeded its result-size limit.")

    rows: list[tuple[JsonCell, ...]] = []
    truncated = False
    while len(rows) <= policy.max_rows:
        cancelled, expired = progress_state()
        if cancelled:
            raise QueryCancelled("The query was cancelled.")
        if expired:
            raise QueryDeadlineExceeded("The query exceeded its execution deadline.")
        remaining = policy.max_rows + 1 - len(rows)
        batch = cursor.fetchmany(min(policy.fetch_size, remaining))
        if not batch:
            break
        for raw_row in batch:
            if len(rows) == policy.max_rows:
                truncated = True
                break
            row = tuple(_json_cell(value, policy.max_cell_bytes) for value in raw_row)
            encoded = _json_bytes(row)
            candidate = serialized_bytes + len(encoded) + (1 if rows else 0)
            if candidate > policy.max_serialized_bytes:
                raise QueryByteLimitExceeded("The query exceeded its result-size limit.")
            rows.append(row)
            serialized_bytes = candidate
        if truncated:
            break
    return rows, truncated, serialized_bytes


def _json_cell(value: object, max_bytes: int) -> JsonCell:
    if value is None or isinstance(value, (bool, int)):
        converted: JsonCell = value
        raw_size = len(str(value).encode("utf-8")) if value is not None else 4
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise QueryExecutionError("The query returned a non-finite numeric value.")
        converted = value
        raw_size = len(repr(value).encode("ascii"))
    elif isinstance(value, str):
        try:
            raw_size = len(value.encode("utf-8"))
        except UnicodeError as exc:
            raise QueryExecutionError("The query returned invalid text.") from exc
        converted = value
    elif isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        raw_size = len(raw)
        converted = {
            "encoding": "base64",
            "data": base64.b64encode(raw).decode("ascii"),
        }
    else:
        raise QueryExecutionError("The query returned an unsupported value type.")
    if raw_size > max_bytes:
        raise QueryCellLimitExceeded(
            f"A query cell exceeded the {max_bytes}-byte result limit."
        )
    return converted


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise QueryExecutionError("The query result could not be serialized safely.") from exc


__all__ = [
    "DEFAULT_SAFE_FUNCTIONS",
    "DEFAULT_SANDBOX_POLICY",
    "HARD_MAX_CELL_BYTES",
    "HARD_MAX_DEADLINE_SECONDS",
    "HARD_MAX_RESULT_COLUMNS",
    "HARD_MAX_ROWS",
    "HARD_MAX_SERIALIZED_BYTES",
    "QueryByteLimitExceeded",
    "QueryCancelled",
    "QueryCellLimitExceeded",
    "QueryColumnLimitExceeded",
    "QueryDatabaseError",
    "QueryDeadlineExceeded",
    "QueryDenied",
    "QueryExecutionError",
    "QueryInputError",
    "QueryOutcome",
    "QueryResult",
    "QueryVmBudgetExceeded",
    "SandboxError",
    "SandboxPolicy",
    "execute_read_query",
]
