"""Fail-closed validation and scoped compilation for model-generated SQLite SQL.

This module intentionally implements a small SELECT grammar with the Python
standard library.  BridgeGuard does not currently ship an AST dependency such
as ``sqlglot``.  The parser therefore accepts only the reviewed subset below;
anything it cannot classify is rejected instead of being forwarded to SQLite.

The public API is split in two steps so callers can audit validation separately
from compilation:

``validate_generated_sql``
    validates one non-compound SELECT against an explicit semantic catalog.

``compile_scoped_sql``
    replaces every catalog table scan with a server-built scoped scan,
    parameterizes user literals, and applies an outer ``LIMIT max_rows + 1``.

No database connection or schema introspection is performed here.  The caller
must provide a reviewed :class:`SqlCatalog`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NUMBER_RE = re.compile(
    r"(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?"
)


class SqlGuardError(ValueError):
    """Base typed failure returned by the generated-SQL guard."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = MappingProxyType(dict(details))

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }


class SqlValidationError(SqlGuardError):
    """The generated statement is outside the approved SELECT subset."""


class SqlCompilationError(SqlGuardError):
    """A validated statement cannot be safely scoped or parameterized."""


class TimeMode(str, Enum):
    """Server-controlled snapshot semantics."""

    EXACT = "exact"
    LATEST = "latest"
    TREND = "trend"


class TableTimeSemantics(str, Enum):
    """How a reviewed semantic resource represents snapshot time."""

    HISTORY = "history"
    CURRENT = "current"


@dataclass(frozen=True)
class CatalogTable:
    """One approved semantic table or view and its scope-bearing columns."""

    name: str
    columns: frozenset[str] | Sequence[str]
    workspace_column: str = "workspace_id"
    state_column: str = "state_code"
    bridge_column: str | None = "bridge_number"
    year_column: str = "data_year"
    time_semantics: TableTimeSemantics | str = TableTimeSemantics.HISTORY

    def __post_init__(self) -> None:
        _require_identifier(self.name, "table name")
        normalized_columns = frozenset(_normalize_identifier(c) for c in self.columns)
        if not normalized_columns:
            raise ValueError(f"Catalog table {self.name!r} must declare columns")
        object.__setattr__(self, "columns", normalized_columns)
        for label, value in (
            ("workspace column", self.workspace_column),
            ("state column", self.state_column),
            ("year column", self.year_column),
        ):
            _require_identifier(value, label)
            if _normalize_identifier(value) not in normalized_columns:
                raise ValueError(
                    f"Catalog table {self.name!r} is missing required {label} {value!r}"
                )
        if self.bridge_column is not None:
            _require_identifier(self.bridge_column, "bridge column")
            if _normalize_identifier(self.bridge_column) not in normalized_columns:
                raise ValueError(
                    f"Catalog table {self.name!r} is missing declared bridge column "
                    f"{self.bridge_column!r}"
                )
        if isinstance(self.time_semantics, TableTimeSemantics):
            time_semantics = self.time_semantics
        else:
            try:
                time_semantics = TableTimeSemantics(str(self.time_semantics).strip().lower())
            except ValueError as exc:
                raise ValueError("time_semantics must be history or current") from exc
        object.__setattr__(self, "time_semantics", time_semantics)


@dataclass(frozen=True)
class SqlCatalog:
    """Explicit allowlist used instead of database introspection."""

    tables: Mapping[str, CatalogTable]
    functions: frozenset[str] | Sequence[str] = field(
        default_factory=lambda: frozenset({"count", "sum", "avg", "min", "max"})
    )

    def __post_init__(self) -> None:
        normalized: dict[str, CatalogTable] = {}
        for supplied_name, table in self.tables.items():
            if not isinstance(table, CatalogTable):
                raise TypeError("SqlCatalog.tables values must be CatalogTable instances")
            key = _normalize_identifier(supplied_name)
            if key != _normalize_identifier(table.name):
                raise ValueError(
                    f"Catalog key {supplied_name!r} does not match table {table.name!r}"
                )
            if key in normalized:
                raise ValueError(f"Duplicate catalog table {supplied_name!r}")
            normalized[key] = table
        if not normalized:
            raise ValueError("SqlCatalog must contain at least one table")
        normalized_functions = frozenset(_normalize_identifier(f) for f in self.functions)
        for function in normalized_functions:
            _require_identifier(function, "function name")
        object.__setattr__(self, "tables", MappingProxyType(normalized))
        object.__setattr__(self, "functions", normalized_functions)

    @classmethod
    def from_columns(
        cls,
        tables: Mapping[str, Iterable[str]],
        *,
        functions: Iterable[str] = ("count", "sum", "avg", "min", "max"),
    ) -> "SqlCatalog":
        return cls(
            tables={
                name: CatalogTable(name=name, columns=frozenset(columns))
                for name, columns in tables.items()
            },
            functions=frozenset(functions),
        )


@dataclass(frozen=True)
class QueryScope:
    """Trusted scope supplied by authentication and request routing."""

    workspace_id: str
    state_code: int | str
    time_mode: TimeMode | str
    bridge_number: str | None = None
    data_year: int | None = None
    start_year: int | None = None
    end_year: int | None = None

    def __post_init__(self) -> None:
        workspace_id = str(self.workspace_id).strip()
        if not workspace_id:
            raise ValueError("workspace_id is required")
        object.__setattr__(self, "workspace_id", workspace_id)
        if isinstance(self.state_code, bool) or not isinstance(self.state_code, (int, str)):
            raise ValueError("state_code must be an integer or non-empty string")
        if isinstance(self.state_code, str):
            state_code = self.state_code.strip()
            if not state_code:
                raise ValueError("state_code is required")
            object.__setattr__(self, "state_code", state_code)
        if isinstance(self.time_mode, TimeMode):
            mode = self.time_mode
        else:
            try:
                mode = TimeMode(str(self.time_mode).strip().lower())
            except ValueError as exc:
                raise ValueError("time_mode must be exact, latest, or trend") from exc
        object.__setattr__(self, "time_mode", mode)
        if self.bridge_number is not None:
            bridge = str(self.bridge_number).strip()
            if not bridge:
                raise ValueError("bridge_number cannot be blank")
            object.__setattr__(self, "bridge_number", bridge)
        if mode is TimeMode.EXACT:
            if self.data_year is None:
                raise ValueError("exact time mode requires data_year")
            _require_year(self.data_year, "data_year")
            if self.start_year is not None or self.end_year is not None:
                raise ValueError("exact time mode cannot include a trend range")
        elif mode is TimeMode.LATEST:
            if any(v is not None for v in (self.data_year, self.start_year, self.end_year)):
                raise ValueError("latest time mode cannot include explicit years")
        else:
            if self.data_year is not None:
                raise ValueError("trend time mode cannot include data_year")
            if self.start_year is None or self.end_year is None:
                raise ValueError("trend time mode requires start_year and end_year")
            _require_year(self.start_year, "start_year")
            _require_year(self.end_year, "end_year")
            if self.start_year > self.end_year:
                raise ValueError("start_year cannot be after end_year")


@dataclass(frozen=True)
class SqlLimits:
    """Syntactic and result ceilings for generated SQL."""

    max_sql_bytes: int = 16_384
    max_ctes: int = 2
    max_joins: int = 4
    max_subqueries: int = 4
    max_result_columns: int = 40
    max_expression_depth: int = 12
    max_parameters: int = 100
    max_rows: int = 200

    def __post_init__(self) -> None:
        for field_name in (
            "max_sql_bytes",
            "max_ctes",
            "max_joins",
            "max_subqueries",
            "max_result_columns",
            "max_expression_depth",
            "max_parameters",
            "max_rows",
        ):
            if int(getattr(self, field_name)) < 1:
                raise ValueError(f"{field_name} must be positive")


@dataclass(frozen=True)
class TableScan:
    table: str
    alias: str


@dataclass(frozen=True)
class ColumnReference:
    column: str
    source_alias: str | None


@dataclass(frozen=True)
class ValidatedSql:
    """Immutable validation result.  It contains no database-derived state."""

    sql: str
    tables: tuple[TableScan, ...]
    columns: tuple[ColumnReference, ...]
    output_columns: tuple[str, ...]
    cte_count: int
    join_count: int
    subquery_count: int
    expression_depth: int
    literal_count: int
    _tokens: tuple["_Token", ...] = field(repr=False, compare=False)
    _scan_replacements: tuple["_ScanReplacement", ...] = field(
        repr=False, compare=False
    )


@dataclass(frozen=True)
class CompiledSql:
    """SQL and positional parameters ready for the bounded executor."""

    sql: str
    parameters: tuple[Any, ...]
    fetch_limit: int
    validated: ValidatedSql
    scope: QueryScope


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    start: int
    end: int
    value: Any = None

    @property
    def key(self) -> str:
        if self.kind not in {"word", "quoted_identifier"}:
            return self.text.upper()
        return str(self.value if self.kind == "quoted_identifier" else self.text).upper()

    @property
    def identifier(self) -> str:
        if self.kind == "quoted_identifier":
            return str(self.value)
        return self.text


@dataclass(frozen=True)
class _Source:
    alias: str
    columns: frozenset[str]
    table: CatalogTable | None


@dataclass(frozen=True)
class _Block:
    output_columns: tuple[str, ...]


@dataclass(frozen=True)
class _ScanReplacement:
    start: int
    end: int
    table: CatalogTable
    alias: str
    ordinal: int


@dataclass(frozen=True)
class _TextReplacement:
    start: int
    end: int
    text: str
    parameters: tuple[Any, ...]


_BANNED_WORDS = {
    "ALTER",
    "ANALYZE",
    "ATTACH",
    "BEGIN",
    "COMMIT",
    "CREATE",
    "DELETE",
    "DETACH",
    "DROP",
    "EXCEPT",
    "EXPLAIN",
    "INSERT",
    "INTERSECT",
    "LOAD_EXTENSION",
    "MERGE",
    "PRAGMA",
    "RECURSIVE",
    "REINDEX",
    "RELEASE",
    "REPLACE",
    "RETURNING",
    "ROLLBACK",
    "SAVEPOINT",
    "TRANSACTION",
    "TRIGGER",
    "UNION",
    "UPDATE",
    "UPSERT",
    "VACUUM",
    "VALUES",
    "VIRTUAL",
}

_UNSUPPORTED_WORDS = {
    "CURRENT_DATE",
    "CURRENT_TIME",
    "CURRENT_TIMESTAMP",
    "FILTER",
    "FULL",
    "MATERIALIZED",
    "NATURAL",
    "NOTHING",
    "OVER",
    "PARTITION",
    "RIGHT",
    "WINDOW",
}

_ALWAYS_FORBIDDEN_FUNCTIONS = {
    "changes",
    "edit",
    "last_insert_rowid",
    "load_extension",
    "random",
    "randomblob",
    "readfile",
    "shell",
    "sqlite_compileoption_get",
    "sqlite_compileoption_used",
    "sqlite_source_id",
    "sqlite_version",
    "total_changes",
    "writefile",
}

_KEYWORDS = {
    "ALL",
    "AND",
    "AS",
    "ASC",
    "BETWEEN",
    "BY",
    "CASE",
    "CAST",
    "COLLATE",
    "CROSS",
    "DESC",
    "DISTINCT",
    "ELSE",
    "END",
    "ESCAPE",
    "EXISTS",
    "FALSE",
    "FROM",
    "GLOB",
    "GROUP",
    "HAVING",
    "IN",
    "INNER",
    "IS",
    "JOIN",
    "LEFT",
    "LIKE",
    "LIMIT",
    "MATCH",
    "NOCASE",
    "NOT",
    "NULL",
    "NULLS",
    "OFFSET",
    "ON",
    "OR",
    "ORDER",
    "OUTER",
    "REGEXP",
    "RIGHT",
    "SELECT",
    "THEN",
    "TRUE",
    "WHEN",
    "WHERE",
    "WITH",
}

_CLAUSE_STARTS = {
    "WHERE",
    "GROUP",
    "HAVING",
    "ORDER",
    "LIMIT",
    "OFFSET",
}

_JOIN_MODIFIERS = {"INNER", "LEFT", "CROSS"}
_TYPE_NAMES = {
    "BLOB",
    "INT",
    "INTEGER",
    "NUMERIC",
    "REAL",
    "TEXT",
}


def _normalize_identifier(value: str) -> str:
    text = str(value).strip()
    _require_identifier(text, "identifier")
    return text.lower()


def _require_identifier(value: str, label: str) -> None:
    if not _IDENTIFIER_RE.fullmatch(str(value)):
        raise ValueError(f"Invalid {label}: {value!r}")
    if str(value).lower().startswith("__bg_"):
        raise ValueError(f"{label} uses BridgeGuard's reserved prefix")


def _require_year(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1800 <= value <= 2200:
        raise ValueError(f"{label} must be an integer between 1800 and 2200")


def _quote_identifier(identifier: str, *, internal: bool = False) -> str:
    if internal:
        if not _IDENTIFIER_RE.fullmatch(identifier):
            raise ValueError(f"Invalid internal identifier: {identifier!r}")
    else:
        _require_identifier(identifier, "identifier")
    return f'"{identifier}"'


def _tokenize(sql: str) -> tuple[_Token, ...]:
    tokens: list[_Token] = []
    i = 0
    length = len(sql)
    while i < length:
        char = sql[i]
        if char.isspace():
            i += 1
            continue
        if sql.startswith("--", i) or sql.startswith("/*", i):
            raise SqlValidationError(
                "comments_forbidden",
                "SQL comments are not accepted in generated queries",
                offset=i,
            )
        if char == ";":
            raise SqlValidationError(
                "multiple_statements",
                "Semicolons are not accepted; submit exactly one SELECT",
                offset=i,
            )
        if char == "'":
            start = i
            i += 1
            value_parts: list[str] = []
            while i < length:
                if sql[i] == "'":
                    if i + 1 < length and sql[i + 1] == "'":
                        value_parts.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                value_parts.append(sql[i])
                i += 1
            else:
                raise SqlValidationError(
                    "unterminated_string", "Unterminated SQL string literal", offset=start
                )
            tokens.append(_Token("string", sql[start:i], start, i, "".join(value_parts)))
            continue
        if char == '"':
            start = i
            i += 1
            value_parts = []
            while i < length:
                if sql[i] == '"':
                    if i + 1 < length and sql[i + 1] == '"':
                        value_parts.append('"')
                        i += 2
                        continue
                    i += 1
                    break
                value_parts.append(sql[i])
                i += 1
            else:
                raise SqlValidationError(
                    "unterminated_identifier",
                    "Unterminated quoted identifier",
                    offset=start,
                )
            value = "".join(value_parts)
            if not _IDENTIFIER_RE.fullmatch(value):
                raise SqlValidationError(
                    "invalid_identifier",
                    "Quoted identifiers must use simple catalog names",
                    identifier=value,
                )
            if value.lower().startswith("__bg_"):
                raise SqlValidationError(
                    "reserved_identifier",
                    "Identifiers beginning __bg_ are reserved by the compiler",
                    identifier=value,
                )
            tokens.append(_Token("quoted_identifier", sql[start:i], start, i, value))
            continue
        if char in "`[":
            raise SqlValidationError(
                "identifier_quoting_forbidden",
                "Only simple or double-quoted identifiers are accepted",
                offset=i,
            )
        if char in "?:@$":
            raise SqlValidationError(
                "caller_parameters_forbidden",
                "Generated SQL cannot supply parameter placeholders",
                offset=i,
            )
        number_match = _NUMBER_RE.match(sql, i)
        if number_match:
            end = number_match.end()
            raw = sql[i:end]
            if end < length and (sql[end].isalnum() or sql[end] == "_"):
                raise SqlValidationError(
                    "invalid_number", "Invalid numeric literal", offset=i
                )
            value: int | float
            try:
                if any(c in raw for c in ".eE"):
                    value = float(raw)
                    if not math.isfinite(value):
                        raise ValueError("non-finite number")
                else:
                    value = int(raw)
            except ValueError as exc:
                raise SqlValidationError(
                    "invalid_number", "Numeric literal is outside the accepted range", offset=i
                ) from exc
            tokens.append(_Token("number", raw, i, end, value))
            i = end
            continue
        if char.isalpha() or char == "_":
            start = i
            i += 1
            while i < length and (sql[i].isalnum() or sql[i] == "_"):
                i += 1
            word = sql[start:i]
            if not _IDENTIFIER_RE.fullmatch(word):
                raise SqlValidationError(
                    "invalid_identifier",
                    "Generated SQL identifiers must use ASCII letters, digits, and underscores",
                    identifier=word,
                )
            if word.lower().startswith("__bg_"):
                raise SqlValidationError(
                    "reserved_identifier",
                    "Identifiers beginning __bg_ are reserved by the compiler",
                    identifier=word,
                )
            tokens.append(_Token("word", word, start, i, word))
            continue
        two = sql[i : i + 2]
        if two in {"<=", ">=", "<>", "!=", "==", "||"}:
            tokens.append(_Token("operator", two, i, i + 2, two))
            i += 2
            continue
        if char in "(),.":
            tokens.append(_Token("punctuation", char, i, i + 1, char))
            i += 1
            continue
        if char in "+-*/%=<>":
            tokens.append(_Token("operator", char, i, i + 1, char))
            i += 1
            continue
        raise SqlValidationError(
            "invalid_character",
            "Character is outside the approved SQL subset",
            offset=i,
            character=char,
        )
    if not tokens:
        raise SqlValidationError("empty_sql", "SQL is required")
    return tuple(tokens)


def _is_identifier(token: _Token) -> bool:
    return token.kind in {"word", "quoted_identifier"}


def _token_name(token: _Token) -> str:
    return token.identifier.lower()


class _Parser:
    def __init__(self, sql: str, tokens: tuple[_Token, ...], catalog: SqlCatalog, limits: SqlLimits):
        self.sql = sql
        self.tokens = tokens
        self.catalog = catalog
        self.limits = limits
        self.matches = self._parenthesis_matches()
        self.cte_count = 0
        self.join_count = 0
        self.subquery_count = 0
        self.max_depth = self._max_depth()
        self.scans: list[TableScan] = []
        self.column_refs: list[ColumnReference] = []
        self.replacements: list[_ScanReplacement] = []
        self._parsed_ranges: dict[tuple[int, int], _Block] = {}

    def _parenthesis_matches(self) -> dict[int, int]:
        stack: list[int] = []
        matches: dict[int, int] = {}
        for index, token in enumerate(self.tokens):
            if token.text == "(":
                stack.append(index)
            elif token.text == ")":
                if not stack:
                    raise SqlValidationError(
                        "unbalanced_parentheses", "Unexpected closing parenthesis"
                    )
                opening = stack.pop()
                matches[opening] = index
                matches[index] = opening
        if stack:
            raise SqlValidationError(
                "unbalanced_parentheses", "SQL contains an unclosed parenthesis"
            )
        return matches

    def _max_depth(self) -> int:
        depth = 0
        maximum = 0
        for token in self.tokens:
            if token.text == "(":
                depth += 1
                maximum = max(maximum, depth)
            elif token.text == ")":
                depth -= 1
        if maximum > self.limits.max_expression_depth:
            raise SqlValidationError(
                "expression_depth_exceeded",
                "SQL expression nesting exceeds the configured limit",
                maximum=maximum,
                limit=self.limits.max_expression_depth,
            )
        return maximum

    def parse(self) -> _Block:
        for token in self.tokens:
            if token.kind == "word" and token.key in _BANNED_WORDS:
                code = "recursive_cte" if token.key == "RECURSIVE" else "forbidden_statement"
                raise SqlValidationError(
                    code,
                    f"{token.text.upper()} is not accepted in generated SQL",
                    token=token.text.upper(),
                )
            if token.kind == "word" and token.key in _UNSUPPORTED_WORDS:
                raise SqlValidationError(
                    "unsupported_syntax",
                    f"{token.text.upper()} is outside the reviewed SQL subset",
                    token=token.text.upper(),
                )
            if token.kind in {"word", "quoted_identifier"}:
                lowered = _token_name(token)
                if (
                    lowered in {"sqlite_master", "sqlite_schema", "sqlite_temp_master", "sqlite_temp_schema"}
                    or lowered.startswith("pragma_")
                    or lowered == "information_schema"
                ):
                    raise SqlValidationError(
                        "schema_access_forbidden",
                        "Schema and pragma tables are never queryable",
                        identifier=token.identifier,
                    )
        block = self._parse_query(0, len(self.tokens), {})
        if not self.replacements:
            raise SqlValidationError(
                "scope_required",
                "Generated SQL must scan at least one approved scoped table",
            )
        if self.cte_count > self.limits.max_ctes:
            raise SqlValidationError(
                "cte_limit_exceeded",
                "SQL contains too many CTEs",
                count=self.cte_count,
                limit=self.limits.max_ctes,
            )
        if self.join_count > self.limits.max_joins:
            raise SqlValidationError(
                "join_limit_exceeded",
                "SQL contains too many joins",
                count=self.join_count,
                limit=self.limits.max_joins,
            )
        if self.subquery_count > self.limits.max_subqueries:
            raise SqlValidationError(
                "subquery_limit_exceeded",
                "SQL contains too many subqueries",
                count=self.subquery_count,
                limit=self.limits.max_subqueries,
            )
        return block

    def _parse_query(
        self,
        start: int,
        end: int,
        inherited_ctes: Mapping[str, tuple[str, ...]],
    ) -> _Block:
        key = (start, end)
        if key in self._parsed_ranges:
            return self._parsed_ranges[key]
        if start >= end:
            raise SqlValidationError("invalid_select", "SELECT body is empty")
        ctes = dict(inherited_ctes)
        index = start
        if self.tokens[index].key == "WITH":
            index += 1
            if index < end and self.tokens[index].key == "RECURSIVE":
                raise SqlValidationError(
                    "recursive_cte", "Recursive CTEs are not accepted"
                )
            while True:
                if index >= end or not _is_identifier(self.tokens[index]):
                    raise SqlValidationError("invalid_cte", "CTE name is required")
                cte_name = _token_name(self.tokens[index])
                if cte_name in ctes or cte_name in self.catalog.tables:
                    raise SqlValidationError(
                        "duplicate_cte", "CTE names must be unique and cannot shadow catalog tables", name=cte_name
                    )
                index += 1
                declared_columns: tuple[str, ...] | None = None
                if index < end and self.tokens[index].text == "(":
                    close = self.matches[index]
                    names = self._split_top_level(index + 1, close)
                    declared: list[str] = []
                    for part_start, part_end in names:
                        if part_end - part_start != 1 or not _is_identifier(self.tokens[part_start]):
                            raise SqlValidationError(
                                "invalid_cte", "CTE column lists must contain simple names"
                            )
                        declared.append(_token_name(self.tokens[part_start]))
                    if len(set(declared)) != len(declared):
                        raise SqlValidationError("invalid_cte", "CTE output names must be unique")
                    declared_columns = tuple(declared)
                    index = close + 1
                if index >= end or self.tokens[index].key != "AS":
                    raise SqlValidationError("invalid_cte", "CTE must use AS (SELECT ...)")
                index += 1
                if index >= end or self.tokens[index].text != "(":
                    raise SqlValidationError("invalid_cte", "CTE body must be parenthesized")
                close = self.matches[index]
                body_start = index + 1
                if body_start >= close:
                    raise SqlValidationError("invalid_cte", "CTE body is empty")
                self.cte_count += 1
                # The current CTE is deliberately absent here: a self-reference
                # is therefore rejected as an unknown/recursive source.
                try:
                    body = self._parse_query(body_start, close, ctes)
                except SqlValidationError as exc:
                    if exc.code == "unknown_table" and exc.details.get("table") == cte_name:
                        raise SqlValidationError(
                            "recursive_cte", "Self-referencing CTEs are not accepted", cte=cte_name
                        ) from exc
                    raise
                columns = declared_columns or body.output_columns
                if declared_columns is not None and len(declared_columns) != len(body.output_columns):
                    raise SqlValidationError(
                        "invalid_cte",
                        "CTE column list does not match its SELECT output",
                        cte=cte_name,
                    )
                if declared_columns is None and any(
                    name.startswith("expression_") for name in columns
                ):
                    raise SqlValidationError(
                        "derived_output_alias_required",
                        "CTE expressions require explicit output aliases or a CTE column list",
                        cte=cte_name,
                    )
                ctes[cte_name] = columns
                index = close + 1
                if index < end and self.tokens[index].text == ",":
                    index += 1
                    continue
                break
        if index >= end or self.tokens[index].key != "SELECT":
            raise SqlValidationError(
                "select_required", "Exactly one SELECT or WITH ... SELECT is required"
            )
        block = self._parse_select(index, end, ctes)
        self._parsed_ranges[key] = block
        return block

    def _parse_select(
        self,
        select_index: int,
        end: int,
        ctes: Mapping[str, tuple[str, ...]],
    ) -> _Block:
        depth = 0
        clause_positions: dict[str, int] = {}
        index = select_index + 1
        while index < end:
            token = self.tokens[index]
            if token.text == "(":
                close = self.matches[index]
                if close >= end:
                    raise SqlValidationError("invalid_select", "Parenthesis crosses SELECT boundary")
                index = close + 1
                continue
            if token.text == ")":
                raise SqlValidationError("invalid_select", "Unexpected parenthesis in SELECT")
            key = token.key
            if token.kind == "word" and key in {"SELECT", "WITH"}:
                raise SqlValidationError(
                    "multiple_statements",
                    "Unexpected second SELECT/WITH outside a reviewed subquery",
                )
            clause: str | None = None
            if key in {"FROM", "WHERE", "HAVING", "LIMIT", "OFFSET"}:
                clause = key
            elif key in {"GROUP", "ORDER"}:
                if index + 1 >= end or self.tokens[index + 1].key != "BY":
                    raise SqlValidationError(
                        "unsupported_syntax", f"{key} must be followed by BY"
                    )
                clause = f"{key} BY"
            if clause:
                if clause in clause_positions:
                    raise SqlValidationError("invalid_select", f"Duplicate {clause} clause")
                clause_positions[clause] = index
            index += 1
        if "FROM" not in clause_positions:
            raise SqlValidationError(
                "scope_required", "SELECT must read an approved scoped table"
            )
        order = ["FROM", "WHERE", "GROUP BY", "HAVING", "ORDER BY", "LIMIT", "OFFSET"]
        seen_positions = [clause_positions[name] for name in order if name in clause_positions]
        if seen_positions != sorted(seen_positions):
            raise SqlValidationError("invalid_select", "SELECT clauses are out of order")
        if "OFFSET" in clause_positions and "LIMIT" not in clause_positions:
            raise SqlValidationError("invalid_select", "OFFSET requires LIMIT")
        for clause_name, clause_index in clause_positions.items():
            content_start = clause_index + (2 if clause_name in {"GROUP BY", "ORDER BY"} else 1)
            next_clause = min(
                (position for position in clause_positions.values() if position > clause_index),
                default=end,
            )
            if content_start >= next_clause:
                raise SqlValidationError(
                    "invalid_select", f"{clause_name} clause cannot be empty"
                )

        projection_start = select_index + 1
        if projection_start < end and self.tokens[projection_start].key in {"DISTINCT", "ALL"}:
            projection_start += 1
        from_index = clause_positions["FROM"]
        if projection_start >= from_index:
            raise SqlValidationError("invalid_select", "SELECT projection is empty")
        projection_parts = self._split_top_level(projection_start, from_index)
        if len(projection_parts) > self.limits.max_result_columns:
            raise SqlValidationError(
                "result_column_limit_exceeded",
                "SELECT returns too many columns",
                count=len(projection_parts),
                limit=self.limits.max_result_columns,
            )
        for clause_name in ("GROUP BY", "ORDER BY"):
            if clause_name not in clause_positions:
                continue
            clause_index = clause_positions[clause_name]
            clause_end = min(
                (position for position in clause_positions.values() if position > clause_index),
                default=end,
            )
            for part_start, part_end in self._split_top_level(clause_index + 2, clause_end):
                core_end = part_end
                if core_end > part_start and self.tokens[core_end - 1].key in {"ASC", "DESC"}:
                    core_end -= 1
                if core_end - part_start == 1 and self.tokens[part_start].kind == "number":
                    raise SqlValidationError(
                        "positional_ordering_forbidden",
                        "GROUP BY and ORDER BY must name expressions, not column positions",
                    )

        from_end = min(
            (position for name, position in clause_positions.items() if name != "FROM"),
            default=end,
        )
        sources, ignored_indexes = self._parse_sources(from_index + 1, from_end, ctes)
        if not sources:
            raise SqlValidationError("scope_required", "FROM must contain an approved table")
        aliases: dict[str, _Source] = {}
        for source in sources:
            alias_key = source.alias.lower()
            if alias_key in aliases:
                raise SqlValidationError(
                    "duplicate_alias", "Each source must have a unique alias", alias=source.alias
                )
            aliases[alias_key] = source

        output_columns: list[str] = []
        output_alias_indexes: set[int] = set()
        for ordinal, (part_start, part_end) in enumerate(projection_parts, start=1):
            output_name, alias_index = self._projection_name(part_start, part_end, ordinal)
            output_columns.append(output_name)
            if alias_index is not None:
                output_alias_indexes.add(alias_index)
            self._check_wildcards(part_start, part_end)
        if len(set(output_columns)) != len(output_columns):
            raise SqlValidationError(
                "duplicate_output_column", "SELECT output names must be unique"
            )

        nested_ranges = self._parse_nested_subqueries(select_index + 1, end, ctes)
        ignored_indexes.update(output_alias_indexes)
        self._validate_identifiers(
            select_index + 1,
            end,
            aliases,
            frozenset(output_columns),
            ignored_indexes,
            nested_ranges,
            clause_positions,
        )
        return _Block(tuple(output_columns))

    def _parse_sources(
        self,
        start: int,
        end: int,
        ctes: Mapping[str, tuple[str, ...]],
    ) -> tuple[list[_Source], set[int]]:
        sources: list[_Source] = []
        ignored: set[int] = set()
        index = start
        expecting_source = True
        join_needs_condition = False
        while index < end:
            token = self.tokens[index]
            if expecting_source:
                if token.key in {"RIGHT", "FULL", "NATURAL"}:
                    raise SqlValidationError(
                        "unsupported_join", f"{token.key} JOIN is outside the reviewed subset"
                    )
                source, index, source_ignored = self._parse_source(index, end, ctes)
                sources.append(source)
                ignored.update(source_ignored)
                expecting_source = False
                continue
            key = token.key
            if token.text == ",":
                if join_needs_condition:
                    raise SqlValidationError("invalid_join", "JOIN requires an ON predicate")
                self.join_count += 1
                expecting_source = True
                index += 1
                continue
            if key in _JOIN_MODIFIERS:
                if join_needs_condition:
                    raise SqlValidationError("invalid_join", "JOIN requires an ON predicate")
                modifier = key
                index += 1
                if modifier == "LEFT" and index < end and self.tokens[index].key == "OUTER":
                    index += 1
                if index >= end or self.tokens[index].key != "JOIN":
                    raise SqlValidationError("invalid_join", f"{modifier} must be followed by JOIN")
                self.join_count += 1
                join_needs_condition = modifier != "CROSS"
                expecting_source = True
                index += 1
                continue
            if key == "JOIN":
                if join_needs_condition:
                    raise SqlValidationError("invalid_join", "JOIN requires an ON predicate")
                self.join_count += 1
                join_needs_condition = True
                expecting_source = True
                index += 1
                continue
            if key == "USING":
                raise SqlValidationError(
                    "unsupported_join", "USING joins are rejected; use an explicit ON predicate"
                )
            if key == "ON":
                if not join_needs_condition:
                    raise SqlValidationError("invalid_join", "ON must follow a non-CROSS JOIN")
                if index + 1 >= end or self.tokens[index + 1].key in {
                    "JOIN",
                    "INNER",
                    "LEFT",
                    "CROSS",
                }:
                    raise SqlValidationError("invalid_join", "ON predicate cannot be empty")
                join_needs_condition = False
                index += 1
                continue
            # ON expressions are validated later.  A parenthesized expression
            # can be skipped as a unit; top-level JOIN tokens remain visible.
            if token.text == "(":
                index = self.matches[index] + 1
            else:
                index += 1
        if expecting_source:
            raise SqlValidationError("invalid_join", "JOIN is missing a source")
        if join_needs_condition:
            raise SqlValidationError("invalid_join", "JOIN requires an ON predicate")
        return sources, ignored

    def _parse_source(
        self,
        index: int,
        end: int,
        ctes: Mapping[str, tuple[str, ...]],
    ) -> tuple[_Source, int, set[int]]:
        ignored: set[int] = set()
        token = self.tokens[index]
        if token.text == "(":
            close = self.matches[index]
            if close >= end:
                raise SqlValidationError("invalid_source", "Source subquery is malformed")
            if index + 1 >= close or self.tokens[index + 1].key not in {"SELECT", "WITH"}:
                raise SqlValidationError(
                    "table_valued_source_forbidden",
                    "FROM parentheses must contain a SELECT subquery",
                )
            self.subquery_count += 1
            block = self._parse_query(index + 1, close, ctes)
            if any(name.startswith("expression_") for name in block.output_columns):
                raise SqlValidationError(
                    "derived_output_alias_required",
                    "Derived-table expressions require explicit output aliases",
                )
            next_index = close + 1
            if next_index < end and self.tokens[next_index].key == "AS":
                ignored.add(next_index)
                next_index += 1
            if next_index >= end or not _is_identifier(self.tokens[next_index]):
                raise SqlValidationError(
                    "alias_required", "Derived table subqueries require an alias"
                )
            alias = self.tokens[next_index].identifier
            ignored.add(next_index)
            return (
                _Source(alias, frozenset(block.output_columns), None),
                next_index + 1,
                ignored,
            )
        if not _is_identifier(token):
            raise SqlValidationError("invalid_source", "FROM/JOIN requires a table name")
        if index + 1 < end and self.tokens[index + 1].text in {".", "("}:
            if self.tokens[index + 1].text == ".":
                raise SqlValidationError(
                    "schema_access_forbidden", "Schema-qualified table names are not accepted"
                )
            raise SqlValidationError(
                "table_valued_source_forbidden", "Table-valued functions are not accepted"
            )
        source_name = _token_name(token)
        ignored.add(index)
        table = self.catalog.tables.get(source_name)
        cte_columns = ctes.get(source_name)
        if table is None and cte_columns is None:
            raise SqlValidationError(
                "unknown_table", "Table or view is not in the semantic catalog", table=source_name
            )
        next_index = index + 1
        alias = token.identifier
        alias_end = token.end
        if next_index < end and self.tokens[next_index].key == "AS":
            ignored.add(next_index)
            next_index += 1
            if next_index >= end or not _is_identifier(self.tokens[next_index]):
                raise SqlValidationError("invalid_alias", "AS must be followed by an alias")
            alias = self.tokens[next_index].identifier
            alias_end = self.tokens[next_index].end
            ignored.add(next_index)
            next_index += 1
        elif (
            next_index < end
            and _is_identifier(self.tokens[next_index])
            and self.tokens[next_index].key not in _KEYWORDS
        ):
            alias = self.tokens[next_index].identifier
            alias_end = self.tokens[next_index].end
            ignored.add(next_index)
            next_index += 1
        if table is not None:
            ordinal = len(self.replacements) + 1
            self.replacements.append(
                _ScanReplacement(token.start, alias_end, table, alias, ordinal)
            )
            self.scans.append(TableScan(table.name, alias))
            columns = table.columns
        else:
            columns = frozenset(cte_columns or ())
        return _Source(alias, frozenset(columns), table), next_index, ignored

    def _parse_nested_subqueries(
        self,
        start: int,
        end: int,
        ctes: Mapping[str, tuple[str, ...]],
    ) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        index = start
        while index < end:
            token = self.tokens[index]
            if token.text != "(":
                index += 1
                continue
            close = self.matches[index]
            if index + 1 < close and self.tokens[index + 1].key in {"SELECT", "WITH"}:
                query_range = (index + 1, close)
                if query_range not in self._parsed_ranges:
                    self.subquery_count += 1
                    self._parse_query(index + 1, close, ctes)
                ranges.append((index, close + 1))
                index = close + 1
                continue
            index += 1
        return ranges

    def _projection_name(
        self, start: int, end: int, ordinal: int
    ) -> tuple[str, int | None]:
        depth = 0
        as_index: int | None = None
        for index in range(start, end):
            token = self.tokens[index]
            if token.text == "(":
                depth += 1
            elif token.text == ")":
                depth -= 1
            elif depth == 0 and token.key == "AS":
                as_index = index
        if as_index is not None:
            if as_index + 2 != end or not _is_identifier(self.tokens[as_index + 1]):
                raise SqlValidationError("invalid_alias", "Output AS requires one simple alias")
            return _token_name(self.tokens[as_index + 1]), as_index + 1
        if end - start == 1 and _is_identifier(self.tokens[start]):
            return _token_name(self.tokens[start]), None
        if (
            end - start == 3
            and _is_identifier(self.tokens[start])
            and self.tokens[start + 1].text == "."
            and _is_identifier(self.tokens[start + 2])
        ):
            return _token_name(self.tokens[start + 2]), None
        # Expressions may be returned without an alias at the outermost level,
        # but derived tables can only refer to an explicit/stable name.
        return f"expression_{ordinal}", None

    def _check_wildcards(self, start: int, end: int) -> None:
        for index in range(start, end):
            token = self.tokens[index]
            if token.text != "*":
                continue
            if self._is_count_star(index):
                continue
            previous = self.tokens[index - 1] if index > start else None
            following = self.tokens[index + 1] if index + 1 < end else None
            arithmetic = (
                previous is not None
                and following is not None
                and previous.text not in {".", "(", ","}
                and following.text not in {")", ","}
            )
            if not arithmetic:
                raise SqlValidationError(
                    "wildcard_forbidden", "Wildcards are allowed only as COUNT(*)"
                )

    def _is_count_star(self, index: int) -> bool:
        if index < 2 or index + 1 >= len(self.tokens):
            return False
        return (
            self.tokens[index - 1].text == "("
            and self.tokens[index - 2].key == "COUNT"
            and self.tokens[index + 1].text == ")"
        )

    def _validate_identifiers(
        self,
        start: int,
        end: int,
        aliases: Mapping[str, _Source],
        output_aliases: frozenset[str],
        ignored_indexes: set[int],
        nested_ranges: Sequence[tuple[int, int]],
        clause_positions: Mapping[str, int],
    ) -> None:
        nested_indexes = {
            index
            for nested_start, nested_end in nested_ranges
            for index in range(nested_start, nested_end)
        }
        index = start
        while index < end:
            token = self.tokens[index]
            if index in nested_indexes or index in ignored_indexes:
                index += 1
                continue
            if token.kind not in {"word", "quoted_identifier"}:
                index += 1
                continue
            key = token.key
            if token.kind == "word" and (key in _KEYWORDS or key in _TYPE_NAMES):
                index += 1
                continue
            if index + 1 < end and self.tokens[index + 1].text == "(":
                function = _token_name(token)
                if function in _ALWAYS_FORBIDDEN_FUNCTIONS:
                    raise SqlValidationError(
                        "unsafe_function",
                        "Function is prohibited even when present in a supplied catalog",
                        function=function,
                    )
                if function not in self.catalog.functions and function != "cast":
                    raise SqlValidationError(
                        "unknown_function",
                        "Function is not in the semantic catalog allowlist",
                        function=function,
                    )
                index += 1
                continue
            if index + 2 < end and self.tokens[index + 1].text == ".":
                if not _is_identifier(self.tokens[index + 2]):
                    raise SqlValidationError("invalid_column", "Qualified column is malformed")
                alias = _token_name(token)
                column = _token_name(self.tokens[index + 2])
                source = aliases.get(alias)
                if source is None:
                    raise SqlValidationError(
                        "unknown_alias", "Column qualifier is not a source alias", alias=alias
                    )
                if column not in source.columns:
                    raise SqlValidationError(
                        "unknown_column",
                        "Column is not available from the qualified source",
                        alias=alias,
                        column=column,
                    )
                self.column_refs.append(ColumnReference(column, alias))
                index += 3
                continue
            if index > start and self.tokens[index - 1].text == ".":
                index += 1
                continue
            name = _token_name(token)
            alias_clause_starts = [
                clause_positions[clause]
                for clause in ("GROUP BY", "HAVING", "ORDER BY")
                if clause in clause_positions
            ]
            alias_clause_start = max(
                (position for position in alias_clause_starts if position < index),
                default=None,
            )
            later_non_alias_clause = min(
                (
                    position
                    for clause, position in clause_positions.items()
                    if clause in {"LIMIT", "OFFSET"} and position < index
                ),
                default=None,
            )
            if (
                name in output_aliases
                and alias_clause_start is not None
                and later_non_alias_clause is None
            ):
                index += 1
                continue
            matching_aliases = [
                alias for alias, source in aliases.items() if name in source.columns
            ]
            if not matching_aliases:
                raise SqlValidationError(
                    "unknown_column", "Column is not in the semantic catalog", column=name
                )
            if len(matching_aliases) > 1:
                raise SqlValidationError(
                    "ambiguous_column",
                    "Unqualified column exists in more than one source",
                    column=name,
                    aliases=matching_aliases,
                )
            self.column_refs.append(ColumnReference(name, matching_aliases[0]))
            index += 1

    def _split_top_level(self, start: int, end: int) -> list[tuple[int, int]]:
        parts: list[tuple[int, int]] = []
        part_start = start
        index = start
        while index < end:
            if self.tokens[index].text == "(":
                index = self.matches[index] + 1
                continue
            if self.tokens[index].text == ",":
                if part_start == index:
                    raise SqlValidationError("invalid_list", "SQL list contains an empty item")
                parts.append((part_start, index))
                part_start = index + 1
            index += 1
        if part_start >= end:
            raise SqlValidationError("invalid_list", "SQL list contains an empty item")
        parts.append((part_start, end))
        return parts


def validate_generated_sql(
    sql: str,
    catalog: SqlCatalog,
    *,
    limits: SqlLimits | None = None,
) -> ValidatedSql:
    """Validate one generated SELECT against an explicit semantic catalog.

    This function is deliberately independent of SQLite.  Successful validation
    does not authorize execution; callers must still use ``compile_scoped_sql``
    and a read-only, authorizer-protected bounded executor.
    """

    if not isinstance(sql, str):
        raise SqlValidationError("invalid_sql_type", "SQL must be a string")
    active_limits = limits or SqlLimits()
    byte_count = len(sql.encode("utf-8"))
    if byte_count > active_limits.max_sql_bytes:
        raise SqlValidationError(
            "sql_bytes_exceeded",
            "Generated SQL exceeds the configured byte limit",
            bytes=byte_count,
            limit=active_limits.max_sql_bytes,
        )
    stripped = sql.strip()
    tokens = _tokenize(stripped)
    parser = _Parser(stripped, tokens, catalog, active_limits)
    block = parser.parse()
    literal_count = sum(
        1
        for token in tokens
        if token.kind in {"string", "number"}
        or (token.kind == "word" and token.key in {"NULL", "TRUE", "FALSE"})
    )
    return ValidatedSql(
        sql=stripped,
        tables=tuple(parser.scans),
        columns=tuple(parser.column_refs),
        output_columns=block.output_columns,
        cte_count=parser.cte_count,
        join_count=parser.join_count,
        subquery_count=parser.subquery_count,
        expression_depth=parser.max_depth,
        literal_count=literal_count,
        _tokens=tokens,
        _scan_replacements=tuple(parser.replacements),
    )


def _scope_scan_sql(
    replacement: _ScanReplacement, scope: QueryScope
) -> tuple[str, tuple[Any, ...]]:
    table = replacement.table
    raw_alias = f"__bg_raw_{replacement.ordinal}"
    raw_q = _quote_identifier(raw_alias, internal=True)
    table_q = _quote_identifier(table.name)
    predicates = [
        f"{raw_q}.{_quote_identifier(table.workspace_column)} = ?",
        f"{raw_q}.{_quote_identifier(table.state_column)} = ?",
    ]
    parameters: list[Any] = [scope.workspace_id, scope.state_code]
    if scope.bridge_number is not None:
        if table.bridge_column is None:
            raise SqlCompilationError(
                "incompatible_bridge_resource",
                "Bridge-scoped queries require a resource with bridge identity",
                table=table.name,
            )
        predicates.append(
            f"{raw_q}.{_quote_identifier(table.bridge_column)} = ?"
        )
        parameters.append(scope.bridge_number)
    if scope.time_mode is TimeMode.EXACT:
        predicates.append(f"{raw_q}.{_quote_identifier(table.year_column)} = ?")
        parameters.append(scope.data_year)
    elif scope.time_mode is TimeMode.TREND:
        if table.time_semantics is TableTimeSemantics.CURRENT:
            raise SqlCompilationError(
                "incompatible_time_resource",
                "Trend queries require a historical semantic resource",
                table=table.name,
                time_mode=scope.time_mode.value,
            )
        predicates.append(
            f"{raw_q}.{_quote_identifier(table.year_column)} BETWEEN ? AND ?"
        )
        parameters.extend((scope.start_year, scope.end_year))
    elif table.time_semantics is TableTimeSemantics.HISTORY:
        latest_alias = f"__bg_latest_{replacement.ordinal}"
        latest_q = _quote_identifier(latest_alias, internal=True)
        latest_predicates = [
            f"{latest_q}.{_quote_identifier(table.workspace_column)} = ?",
            f"{latest_q}.{_quote_identifier(table.state_column)} = ?",
        ]
        latest_parameters: list[Any] = [scope.workspace_id, scope.state_code]
        # Bridgette's default "latest" contract is one server-resolved global
        # inventory year for the state. Every returned bridge therefore comes
        # from the same comparable snapshot year, including bridge-specific
        # questions. An absent bridge in that year must remain an honest zero
        # row result instead of silently falling back to an older record.
        predicates.append(
            f"{raw_q}.{_quote_identifier(table.year_column)} = ("
            f"SELECT MAX({latest_q}.{_quote_identifier(table.year_column)}) "
            f"FROM {table_q} AS {latest_q} WHERE "
            + " AND ".join(latest_predicates)
            + ")"
        )
        parameters.extend(latest_parameters)
    # Project only reviewed catalog columns.  An implementation view may have
    # extra operational fields; SELECT * would authorize and expose them even
    # though generated SQL cannot name them.
    projected_columns = ", ".join(
        f"{raw_q}.{_quote_identifier(column)} AS {_quote_identifier(column)}"
        for column in sorted(table.columns)
    )
    scan_sql = (
        f"(SELECT {projected_columns} FROM {table_q} AS {raw_q} WHERE "
        + " AND ".join(predicates)
        + f") AS {_quote_identifier(replacement.alias)}"
    )
    return scan_sql, tuple(parameters)


def _literal_replacements(tokens: Sequence[_Token]) -> list[_TextReplacement]:
    replacements: list[_TextReplacement] = []
    for token in tokens:
        if token.kind == "string":
            replacements.append(_TextReplacement(token.start, token.end, "?", (token.value,)))
        elif token.kind == "number":
            replacements.append(_TextReplacement(token.start, token.end, "?", (token.value,)))
        elif token.kind == "word" and token.key in {"NULL", "TRUE", "FALSE"}:
            value = None if token.key == "NULL" else token.key == "TRUE"
            replacements.append(_TextReplacement(token.start, token.end, "?", (value,)))
    return replacements


def compile_scoped_sql(
    validated: ValidatedSql,
    scope: QueryScope,
    *,
    limits: SqlLimits | None = None,
    max_rows: int | None = None,
) -> CompiledSql:
    """Compile validated SQL with immutable scope and a hard outer row cap."""

    if not isinstance(validated, ValidatedSql):
        raise SqlCompilationError(
            "unvalidated_sql", "compile_scoped_sql requires a ValidatedSql instance"
        )
    active_limits = limits or SqlLimits()
    if max_rows is None:
        effective_max_rows = active_limits.max_rows
    else:
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
            raise SqlCompilationError("invalid_row_limit", "max_rows must be a positive integer")
        effective_max_rows = min(max_rows, active_limits.max_rows)
    replacements = _literal_replacements(validated._tokens)
    for scan in validated._scan_replacements:
        scan_sql, parameters = _scope_scan_sql(scan, scope)
        replacements.append(
            _TextReplacement(scan.start, scan.end, scan_sql, parameters)
        )
    replacements.sort(key=lambda item: (item.start, item.end))
    cursor = 0
    sql_parts: list[str] = []
    parameters: list[Any] = []
    for replacement in replacements:
        if replacement.start < cursor:
            # This should only be reachable after programmer misuse/corruption of
            # an otherwise immutable ValidatedSql object.
            raise SqlCompilationError(
                "overlapping_replacements", "Validated SQL replacement spans overlap"
            )
        sql_parts.append(validated.sql[cursor : replacement.start])
        sql_parts.append(replacement.text)
        parameters.extend(replacement.parameters)
        cursor = replacement.end
    sql_parts.append(validated.sql[cursor:])
    scoped_inner = "".join(sql_parts)
    fetch_limit = effective_max_rows + 1
    compiled_sql = (
        'SELECT * FROM (' + scoped_inner + ') AS "__bg_bounded_result" LIMIT ?'
    )
    parameters.append(fetch_limit)
    if len(parameters) > active_limits.max_parameters:
        raise SqlCompilationError(
            "parameter_limit_exceeded",
            "Compiled query exceeds the configured parameter limit",
            count=len(parameters),
            limit=active_limits.max_parameters,
        )
    return CompiledSql(
        sql=compiled_sql,
        parameters=tuple(parameters),
        fetch_limit=fetch_limit,
        validated=validated,
        scope=scope,
    )


def validate_and_compile_sql(
    sql: str,
    catalog: SqlCatalog,
    scope: QueryScope,
    *,
    limits: SqlLimits | None = None,
    max_rows: int | None = None,
) -> CompiledSql:
    """Convenience entry point for the complete fail-closed compilation path."""

    active_limits = limits or SqlLimits()
    validated = validate_generated_sql(sql, catalog, limits=active_limits)
    return compile_scoped_sql(
        validated,
        scope,
        limits=active_limits,
        max_rows=max_rows,
    )


__all__ = [
    "CatalogTable",
    "ColumnReference",
    "CompiledSql",
    "QueryScope",
    "SqlCatalog",
    "SqlCompilationError",
    "SqlGuardError",
    "SqlLimits",
    "SqlValidationError",
    "TableScan",
    "TableTimeSemantics",
    "TimeMode",
    "ValidatedSql",
    "compile_scoped_sql",
    "validate_and_compile_sql",
    "validate_generated_sql",
]
