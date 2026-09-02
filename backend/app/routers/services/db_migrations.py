"""Checksummed, transactional SQLite migrations for the projection schema.

Callers must explicitly invoke :func:`apply_migrations`; importing this module
never opens or modifies a database.  The runner accepts both a genuinely empty
SQLite file and the deployed legacy-v0 ``features`` table.  Migration history
and ``PRAGMA user_version`` are treated as two synchronized witnesses: a
mismatch fails closed instead of guessing which one is authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Callable, Sequence

from .db_schema import (
    AUTHORITATIVE_SCHEMA,
    CHAT_SESSION_TABLES,
    DEPLOYMENT_IDENTITY_TABLES,
    ELEMENT_INTEGER_COLUMNS,
    FEATURE_INTEGER_COLUMNS,
    FEATURE_TRANSITION_COLUMNS,
    FORECAST_ANALYTICS_TABLES,
    KPI_PROGRAM_TABLES,
    POINT_CLOUD_RUN_TABLES,
    FEATURES_IDENTITY_INDEX,
    FEATURES_INDEX,
    FEATURES_TABLE,
    FEATURES_V2_TABLE,
    LEGACY_FEATURES_CREATE_SQL,
    LATEST_SCHEMA_VERSION,
    PROJECTION_CHILD_TABLES,
    QUERY_SEMANTIC_VIEWS,
    QUERY_RESPONSE_AUDIT_TABLES,
    QUERY_SUPPORT_TABLES,
    REF_COUNTIES_TABLE,
    REVISION_STORE_TABLES,
    SCHEMA_MIGRATIONS_CREATE_SQL,
    SCHEMA_MIGRATIONS_TABLE,
    SchemaManifest,
    SchemaDriftError,
    TableSpec,
    assert_legacy_features_compatible,
    assert_schema_matches,
    assert_table_matches,
    create_index_sql,
    quote_identifier,
    table_exists,
    view_exists,
)


class MigrationError(RuntimeError):
    """Base class for migration-registry and execution failures."""


class MigrationStateError(MigrationError):
    """Raised for a history/user-version state that cannot be reconciled."""


class MigrationChecksumError(MigrationStateError):
    """Raised when an applied migration no longer matches its source checksum."""


class MigrationExecutionError(MigrationError):
    """Raised after an individual migration fails and the transaction rolls back."""

    def __init__(self, version: int, name: str, cause: BaseException):
        self.version = version
        self.name = name
        self.cause = cause
        super().__init__(f"Migration {version} ({name}) failed: {cause}")


MigrationCallable = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    descriptor: tuple[str, ...]
    operation: MigrationCallable

    @property
    def checksum(self) -> str:
        canonical = json.dumps(
            {
                "version": self.version,
                "name": self.name,
                "descriptor": list(self.descriptor),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class MigrationResult:
    previous_version: int
    current_version: int
    applied_versions: tuple[int, ...]
    schema_drift: tuple[str, ...] = ()


def _migration_0001_adopt_features(connection: sqlite3.Connection) -> None:
    if not table_exists(connection, "features"):
        connection.execute(LEGACY_FEATURES_CREATE_SQL)
    assert_legacy_features_compatible(connection)

    duplicate = connection.execute(
        "SELECT state_code, bridge_number, data_year, feature_type, COUNT(*) "
        "FROM features "
        "WHERE data_year IS NOT NULL AND feature_type IS NOT NULL "
        "GROUP BY state_code, bridge_number, data_year, feature_type "
        "HAVING COUNT(*) > 1 "
        "ORDER BY state_code, bridge_number, data_year, feature_type "
        "LIMIT 1"
    ).fetchone()
    if duplicate is not None:
        state_code, bridge_number, data_year, feature_type, count = duplicate
        raise MigrationStateError(
            "Cannot add unique feature identity; duplicate rows exist for "
            f"state_code={state_code!r}, bridge_number={bridge_number!r}, "
            f"data_year={data_year!r}, feature_type={feature_type!r}, "
            f"count={count}"
        )

    unexpected_triggers = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'trigger' AND tbl_name = 'features' ORDER BY name"
    ).fetchall()
    if unexpected_triggers:
        raise SchemaDriftError(
            tuple(
                f"features: unexpected trigger {row[0]}"
                for row in unexpected_triggers
            )
        )

    explicit_indexes = {
        str(row[1])
        for row in connection.execute('PRAGMA index_list("features")').fetchall()
        if len(row) < 4 or str(row[3]).lower() == "c"
    }
    allowed_indexes = {FEATURES_INDEX.name, FEATURES_IDENTITY_INDEX.name}
    unexpected_indexes = tuple(sorted(explicit_indexes - allowed_indexes))
    if unexpected_indexes:
        raise SchemaDriftError(
            tuple(
                f"features: unexpected index {name}" for name in unexpected_indexes
            )
        )

    live_column_names = tuple(
        str(row[1])
        for row in connection.execute('PRAGMA table_info("features")').fetchall()
    )
    live_column_set = set(live_column_names)
    retained_columns = tuple(
        column
        for column in FEATURES_V2_TABLE.columns
        if column.name in live_column_set
    )

    def column_sql(column) -> str:
        if column.primary_key_position:
            return f'{quote_identifier(column.name)} INTEGER PRIMARY KEY AUTOINCREMENT'
        nullability = " NOT NULL" if column.not_null else ""
        return (
            f"{quote_identifier(column.name)} {column.declared_type}"
            f"{nullability}"
        )

    definitions = [column_sql(column) for column in retained_columns]
    definitions.append(
        "FOREIGN KEY (\"state_code\", \"bridge_number\", \"data_year\") "
        "REFERENCES \"bridges\" "
        "(\"state_code\", \"bridge_number\", \"data_year\") "
        "ON UPDATE CASCADE ON DELETE CASCADE"
    )
    connection.execute(
        'CREATE TABLE "_features_pr4_new" (\n    '
        + ",\n    ".join(definitions)
        + "\n)"
    )
    column_list = ", ".join(
        quote_identifier(column.name) for column in retained_columns
    )
    if (
        table_exists(connection, "bridges")
        and connection.execute('SELECT 1 FROM "features" LIMIT 1').fetchone()
        is not None
    ):
        connection.execute(
            f'INSERT INTO "_features_pr4_new" ({column_list}) '
            f'SELECT {column_list} FROM "features"'
        )
    connection.execute('DROP TABLE "features"')
    connection.execute('ALTER TABLE "_features_pr4_new" RENAME TO "features"')
    connection.execute(
        f"CREATE INDEX IF NOT EXISTS {quote_identifier(FEATURES_INDEX.name)} "
        f"ON {quote_identifier('features')} "
        f"({', '.join(quote_identifier(column) for column in FEATURES_INDEX.columns)})"
    )
    connection.execute(
        f"CREATE UNIQUE INDEX {quote_identifier(FEATURES_IDENTITY_INDEX.name)} "
        f"ON {quote_identifier('features')} "
        f"({', '.join(quote_identifier(column) for column in FEATURES_IDENTITY_INDEX.columns)})"
    )


def _migration_0002_complete_feature_transition_fields(
    connection: sqlite3.Connection,
) -> None:
    assert_legacy_features_compatible(connection)
    live_columns = {
        str(row[1]): str(row[2]).upper()
        for row in connection.execute('PRAGMA table_info("features")').fetchall()
    }
    for column in FEATURE_TRANSITION_COLUMNS:
        live_type = live_columns.get(column.name)
        if live_type is not None:
            if live_type != column.declared_type:
                raise SchemaDriftError(
                    (
                        f"features.{column.name}: type {live_type!r} "
                        f"!= {column.declared_type!r}",
                    )
                )
            continue
        connection.execute(
            f"ALTER TABLE {quote_identifier('features')} "
            f"ADD COLUMN {quote_identifier(column.name)} {column.declared_type}"
        )


_INTEGER_RETYPE_COLUMNS: dict[str, frozenset[str]] = {
    "features": FEATURE_INTEGER_COLUMNS,
    "elements": ELEMENT_INTEGER_COLUMNS,
}


def _normalise_default(value: object) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).strip().split())


def _live_foreign_key_signatures(
    connection: sqlite3.Connection,
    table_name: str,
) -> set[tuple[tuple[str, ...], str, tuple[str, ...], str, str]]:
    grouped: dict[int, list[tuple[object, ...]]] = {}
    for row in connection.execute(
        f"PRAGMA foreign_key_list({quote_identifier(table_name)})"
    ).fetchall():
        grouped.setdefault(int(row[0]), []).append(tuple(row))
    signatures = set()
    for rows in grouped.values():
        ordered = sorted(rows, key=lambda row: int(row[1]))
        signatures.add(
            (
                tuple(str(row[3]) for row in ordered),
                str(ordered[0][2]),
                tuple(str(row[4]) for row in ordered),
                str(ordered[0][5]).upper(),
                str(ordered[0][6]).upper(),
            )
        )
    return signatures


def _preflight_child_shape(
    connection: sqlite3.Connection,
    table: TableSpec,
) -> None:
    rows = connection.execute(
        f"PRAGMA table_info({quote_identifier(table.name)})"
    ).fetchall()
    live = {str(row[1]): row for row in rows}
    expected_names = tuple(column.name for column in table.columns)
    differences: list[str] = []
    if tuple(str(row[1]) for row in rows) != expected_names:
        missing = sorted(set(expected_names).difference(live))
        extra = sorted(set(live).difference(expected_names))
        differences.extend(f"{table.name}.{name}: missing column" for name in missing)
        differences.extend(f"{table.name}.{name}: unexpected column" for name in extra)
        if not missing and not extra:
            differences.append(f"{table.name}: column order differs from manifest")

    allowed_legacy_real = _INTEGER_RETYPE_COLUMNS.get(table.name, frozenset())
    for ordinal, column in enumerate(table.columns):
        row = live.get(column.name)
        if row is None:
            continue
        live_type = " ".join(str(row[2] or "").upper().split())
        allowed_types = {column.declared_type}
        if column.name in allowed_legacy_real:
            allowed_types.add("REAL")
        if live_type not in allowed_types:
            differences.append(
                f"{table.name}.{column.name}: type {live_type!r} not in "
                f"{tuple(sorted(allowed_types))!r}"
            )
        if bool(row[3]) != column.not_null:
            differences.append(
                f"{table.name}.{column.name}: not-null {bool(row[3])!r} "
                f"!= {column.not_null!r}"
            )
        if int(row[5]) != column.primary_key_position:
            differences.append(
                f"{table.name}.{column.name}: primary-key position "
                f"{int(row[5])} != {column.primary_key_position}"
            )
        if _normalise_default(row[4]) != _normalise_default(column.default_sql):
            differences.append(
                f"{table.name}.{column.name}: default "
                f"{_normalise_default(row[4])!r} != "
                f"{_normalise_default(column.default_sql)!r}"
            )
        if int(row[0]) != ordinal:
            differences.append(
                f"{table.name}.{column.name}: ordinal {int(row[0])} != {ordinal}"
            )

    expected_target_fk = {
        (
            key.columns,
            key.referenced_table,
            key.referenced_columns,
            key.on_update.upper(),
            key.on_delete.upper(),
        )
        for key in table.foreign_keys
    }
    expected_legacy_fk = {
        (columns, parent, targets, "NO ACTION", "NO ACTION")
        for columns, parent, targets, _update, _delete in expected_target_fk
    }
    live_fks = _live_foreign_key_signatures(connection, table.name)
    if live_fks not in (expected_target_fk, expected_legacy_fk):
        differences.append(
            f"{table.name}: foreign keys {sorted(live_fks, key=repr)!r} "
            f"are neither the legacy nor target snapshot relationship"
        )

    trigger_names = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'trigger' AND tbl_name = ? ORDER BY name",
            (table.name,),
        ).fetchall()
    )
    differences.extend(
        f"{table.name}: unexpected trigger {name}" for name in trigger_names
    )

    explicit_indexes = {
        str(row[1])
        for row in connection.execute(
            f"PRAGMA index_list({quote_identifier(table.name)})"
        ).fetchall()
        if len(row) < 4 or str(row[3]).lower() == "c"
    }
    allowed_indexes = {index.name for index in table.indexes}
    differences.extend(
        f"{table.name}: unexpected index {name}"
        for name in sorted(explicit_indexes.difference(allowed_indexes))
    )
    index_rows = connection.execute(
        f"PRAGMA index_list({quote_identifier(table.name)})"
    ).fetchall()
    index_unique = {str(row[1]): bool(row[2]) for row in index_rows}
    for index in table.indexes:
        if index.name not in explicit_indexes:
            continue
        if index_unique.get(index.name) != index.unique:
            differences.append(
                f"{table.name}.{index.name}: unique "
                f"{index_unique.get(index.name)!r} != {index.unique!r}"
            )
        if index.sql_terms:
            live_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND name=?",
                (index.name,),
            ).fetchone()
            normalise = lambda sql: re.sub(  # noqa: E731
                r'[\s"`\[\];]+', "", str(sql or "").lower()
            )
            expected_sql = create_index_sql(table.name, index)
            if live_sql_row is None or normalise(live_sql_row[0]) != normalise(expected_sql):
                differences.append(
                    f"{table.name}.{index.name}: expression index definition differs"
                )
        else:
            live_index_columns = tuple(
                str(row[2])
                for row in connection.execute(
                    f"PRAGMA index_info({quote_identifier(index.name)})"
                ).fetchall()
            )
            if live_index_columns != index.columns:
                differences.append(
                    f"{table.name}.{index.name}: columns "
                    f"{live_index_columns!r} != {index.columns!r}"
                )
    if differences:
        raise SchemaDriftError(differences)


def _identity_contract(
    table: TableSpec,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    identity = next(index for index in table.indexes if index.unique)
    if table.name == "posting_statuses":
        return (
            (
                '"state_code"',
                '"bridge_number"',
                '"data_year"',
                '"load_posting_status"',
                'COALESCE("posting_status_change_date", \'\')',
            ),
            (
                "state_code",
                "bridge_number",
                "data_year",
                "load_posting_status",
            ),
        )
    return (
        tuple(quote_identifier(column) for column in identity.columns),
        identity.columns,
    )


def _preflight_child_identity(
    connection: sqlite3.Connection,
    table: TableSpec,
) -> None:
    group_terms, nonnull_columns = _identity_contract(table)
    where_sql = " AND ".join(
        f"{quote_identifier(column)} IS NOT NULL" for column in nonnull_columns
    )
    group_sql = ", ".join(group_terms)
    row = connection.execute(
        f"SELECT {group_sql}, COUNT(*) FROM {quote_identifier(table.name)} "
        f"WHERE {where_sql} GROUP BY {group_sql} HAVING COUNT(*) > 1 "
        f"ORDER BY {group_sql} LIMIT 1"
    ).fetchone()
    if row is not None:
        raise MigrationStateError(
            f"Cannot add unique identity for {table.name}; duplicate rows "
            f"exist for identity={tuple(row[:-1])!r}, count={int(row[-1])}"
        )


def _preflight_integer_values(
    connection: sqlite3.Connection,
    table: TableSpec,
) -> None:
    for column_name in sorted(_INTEGER_RETYPE_COLUMNS.get(table.name, frozenset())):
        quoted = quote_identifier(column_name)
        row = connection.execute(
            f"SELECT id, {quoted}, typeof({quoted}) "
            f"FROM {quote_identifier(table.name)} "
            f"WHERE {quoted} IS NOT NULL AND "
            f"(typeof({quoted}) NOT IN ('integer', 'real') "
            f"OR {quoted} != CAST({quoted} AS INTEGER)) "
            f"ORDER BY id LIMIT 1"
        ).fetchone()
        if row is not None:
            raise MigrationStateError(
                f"Cannot change {table.name}.{column_name} to INTEGER; "
                f"row id={row[0]!r} has incompatible value={row[1]!r} "
                f"(storage={row[2]!r})"
            )


def _column_definition(column) -> str:
    if column.primary_key_position:
        if column.declared_type == "INTEGER":
            return (
                f'{quote_identifier(column.name)} INTEGER '
                "PRIMARY KEY AUTOINCREMENT"
            )
        nullability = " NOT NULL" if column.not_null else ""
        return (
            f"{quote_identifier(column.name)} {column.declared_type}"
            f"{nullability} PRIMARY KEY"
        )
    sql = f"{quote_identifier(column.name)} {column.declared_type}"
    if column.not_null:
        sql += " NOT NULL"
    if column.default_sql is not None:
        sql += f" DEFAULT {column.default_sql}"
    return sql


def _create_child_table(
    connection: sqlite3.Connection,
    table: TableSpec,
    physical_name: str,
) -> None:
    definitions = [_column_definition(column) for column in table.columns]
    for key in table.foreign_keys:
        definitions.append(
            "FOREIGN KEY ("
            + ", ".join(quote_identifier(column) for column in key.columns)
            + f") REFERENCES {quote_identifier(key.referenced_table)} ("
            + ", ".join(
                quote_identifier(column) for column in key.referenced_columns
            )
            + f") ON UPDATE {key.on_update} ON DELETE {key.on_delete}"
        )
    connection.execute(
        f"CREATE TABLE {quote_identifier(physical_name)} (\n    "
        + ",\n    ".join(definitions)
        + "\n)"
    )


def _create_child_indexes(
    connection: sqlite3.Connection,
    table: TableSpec,
) -> None:
    for index in table.indexes:
        connection.execute(create_index_sql(table.name, index))


def _create_table_triggers(
    connection: sqlite3.Connection,
    table: TableSpec,
) -> None:
    for trigger in table.triggers:
        connection.execute(trigger.sql)


def _rebuild_child_table(
    connection: sqlite3.Connection,
    table: TableSpec,
) -> None:
    if not table_exists(connection, table.name):
        _create_child_table(connection, table, table.name)
        _create_child_indexes(connection, table)
        return

    temporary_name = f"_pr4_v3_{table.name}_new"
    if table_exists(connection, temporary_name):
        raise SchemaDriftError((f"{temporary_name}: unexpected table",))
    _create_child_table(connection, table, temporary_name)

    integer_columns = _INTEGER_RETYPE_COLUMNS.get(table.name, frozenset())
    target_columns = ", ".join(
        quote_identifier(column.name) for column in table.columns
    )
    source_terms = ", ".join(
        (
            f"CAST({quote_identifier(column.name)} AS INTEGER)"
            if column.name in integer_columns
            else quote_identifier(column.name)
        )
        for column in table.columns
    )
    before_count = int(
        connection.execute(
            f"SELECT COUNT(*) FROM {quote_identifier(table.name)}"
        ).fetchone()[0]
    )
    if before_count:
        connection.execute(
            f"INSERT INTO {quote_identifier(temporary_name)} ({target_columns}) "
            f"SELECT {source_terms} FROM {quote_identifier(table.name)}"
        )
    after_count = int(
        connection.execute(
            f"SELECT COUNT(*) FROM {quote_identifier(temporary_name)}"
        ).fetchone()[0]
    )
    if before_count != after_count:
        raise MigrationStateError(
            f"{table.name} row reconciliation failed: "
            f"before={before_count}, staged={after_count}"
        )
    connection.execute(f"DROP TABLE {quote_identifier(table.name)}")
    connection.execute(
        f"ALTER TABLE {quote_identifier(temporary_name)} "
        f"RENAME TO {quote_identifier(table.name)}"
    )
    _create_child_indexes(connection, table)


def _migration_0003_complete_child_integrity(
    connection: sqlite3.Connection,
) -> None:
    # Finish *all* validation before the first rebuild.  The transaction would
    # roll back partial DDL anyway, but this ordering keeps failure evidence
    # tied to the untouched legacy state and avoids unnecessary I/O.
    for table in PROJECTION_CHILD_TABLES:
        if not table_exists(connection, table.name):
            continue
        _preflight_child_shape(connection, table)
        _preflight_child_identity(connection, table)
        _preflight_integer_values(connection, table)

    for table in PROJECTION_CHILD_TABLES:
        _rebuild_child_table(connection, table)

    if table_exists(connection, "bridges"):
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            first = violations[0]
            raise MigrationStateError(
                "Foreign-key reconciliation failed after child rebuild: "
                f"table={first[0]!r}, rowid={first[1]!r}, "
                f"parent={first[2]!r}, fk={first[3]!r}, "
                f"total_violations={len(violations)}"
            )
    else:
        populated = [
            table.name
            for table in PROJECTION_CHILD_TABLES
            if connection.execute(
                f"SELECT 1 FROM {quote_identifier(table.name)} LIMIT 1"
            ).fetchone()
            is not None
        ]
        if populated:
            raise MigrationStateError(
                "Child projection rows exist without the bridges parent table: "
                + ", ".join(populated)
            )


def _migration_0004_create_revision_store(
    connection: sqlite3.Connection,
) -> None:
    # These names are new in v4.  Silently adopting a manually created table
    # would make the migration receipt lie about which DDL was applied.
    collisions = tuple(
        table.name
        for table in REVISION_STORE_TABLES
        if table_exists(connection, table.name)
    )
    if collisions:
        raise SchemaDriftError(
            tuple(
                f"{table_name}: untracked table exists before migration 4"
                for table_name in collisions
            )
        )

    for table in REVISION_STORE_TABLES:
        _create_child_table(connection, table, table.name)
        _create_child_indexes(connection, table)
        _create_table_triggers(connection, table)
        assert_table_matches(connection, table)


_NJ_COUNTIES: tuple[tuple[int, str], ...] = (
    (0, "Unknown"),
    (1, "Atlantic"),
    (3, "Bergen"),
    (5, "Burlington"),
    (7, "Camden"),
    (9, "Cape May"),
    (11, "Cumberland"),
    (13, "Essex"),
    (15, "Gloucester"),
    (17, "Hudson"),
    (19, "Hunterdon"),
    (21, "Mercer"),
    (23, "Middlesex"),
    (25, "Monmouth"),
    (27, "Morris"),
    (29, "Ocean"),
    (31, "Passaic"),
    (33, "Salem"),
    (35, "Somerset"),
    (37, "Sussex"),
    (39, "Union"),
    (41, "Warren"),
)


def _migration_0005_create_query_boundary(
    connection: sqlite3.Connection,
) -> None:
    # Migration 5 must remain frozen to the objects that existed at version 5.
    # Later views live in the authoritative manifest too, but are introduced by
    # their own checksummed migrations.
    version_5_views = tuple(
        view
        for view in QUERY_SEMANTIC_VIEWS
        if view.name
        not in {
            "v_bridge_span_sets",
            "v_bridge_deck_material_summary",
            "v_current_bridge_deck_material_summary",
        }
    )
    collisions = tuple(
        table.name
        for table in QUERY_SUPPORT_TABLES
        if table_exists(connection, table.name)
    )
    view_collisions = tuple(
        view.name
        for view in version_5_views
        if view_exists(connection, view.name)
    )
    if collisions or view_collisions:
        raise SchemaDriftError(
            tuple(
                f"{name}: untracked query object exists before migration 5"
                for name in (*collisions, *view_collisions)
            )
        )

    for table in QUERY_SUPPORT_TABLES:
        _create_child_table(connection, table, table.name)
        _create_child_indexes(connection, table)
        if table.name == REF_COUNTIES_TABLE.name:
            connection.executemany(
                "INSERT INTO ref_counties (state_code, county_code, county_name) "
                "VALUES (34, ?, ?)",
                _NJ_COUNTIES,
            )
        _create_table_triggers(connection, table)
        assert_table_matches(connection, table)

    for view in version_5_views:
        connection.execute(view.sql)

    connection.execute(
        "UPDATE workspace_state SET schema_generation=?",
        (5,),
    )


def _migration_0006_create_query_response_audit(
    connection: sqlite3.Connection,
) -> None:
    collisions = tuple(
        table.name
        for table in QUERY_RESPONSE_AUDIT_TABLES
        if table_exists(connection, table.name)
    )
    if collisions:
        raise SchemaDriftError(
            tuple(
                f"{name}: untracked response-audit object exists before migration 6"
                for name in collisions
            )
        )

    for table in QUERY_RESPONSE_AUDIT_TABLES:
        _create_child_table(connection, table, table.name)
        _create_child_indexes(connection, table)
        _create_table_triggers(connection, table)
        assert_table_matches(connection, table)

    # Schema generation is metadata, not a visible-data revision.  Existing
    # workspaces advance atomically with the migration while data_generation
    # is deliberately unchanged.
    connection.execute(
        "UPDATE workspace_state SET schema_generation=?",
        (6,),
    )


def _migration_0007_create_private_chat_sessions(
    connection: sqlite3.Connection,
) -> None:
    collisions = tuple(
        table.name
        for table in CHAT_SESSION_TABLES
        if table_exists(connection, table.name)
    )
    if collisions:
        raise SchemaDriftError(
            tuple(
                f"{name}: untracked chat-session object exists before migration 7"
                for name in collisions
            )
        )

    for table in CHAT_SESSION_TABLES:
        _create_child_table(connection, table, table.name)
        _create_child_indexes(connection, table)
        assert_table_matches(connection, table)

    connection.execute(
        "UPDATE workspace_state SET schema_generation=?",
        (7,),
    )


def _migration_0008_create_deployment_identity(
    connection: sqlite3.Connection,
) -> None:
    collisions = tuple(
        table.name
        for table in DEPLOYMENT_IDENTITY_TABLES
        if table_exists(connection, table.name)
    )
    if collisions:
        raise SchemaDriftError(
            tuple(
                f"{name}: untracked deployment-identity object exists before migration 8"
                for name in collisions
            )
        )

    for table in DEPLOYMENT_IDENTITY_TABLES:
        _create_child_table(connection, table, table.name)
        _create_child_indexes(connection, table)
        _create_table_triggers(connection, table)
        assert_table_matches(connection, table)

    connection.execute(
        "UPDATE workspace_state SET schema_generation=?",
        (8,),
    )


def _migration_0009_create_forecast_analytics(
    connection: sqlite3.Connection,
) -> None:
    collisions = tuple(
        table.name
        for table in FORECAST_ANALYTICS_TABLES
        if table_exists(connection, table.name)
    )
    if collisions:
        raise SchemaDriftError(
            tuple(
                f"{name}: untracked forecast-analytics object exists before migration 9"
                for name in collisions
            )
        )
    for table in FORECAST_ANALYTICS_TABLES:
        _create_child_table(connection, table, table.name)
        _create_child_indexes(connection, table)
        _create_table_triggers(connection, table)
        assert_table_matches(connection, table)
    connection.execute(
        "UPDATE workspace_state SET schema_generation=?",
        (9,),
    )


def _migration_0010_create_kpi_program_store(
    connection: sqlite3.Connection,
) -> None:
    collisions = tuple(
        table.name for table in KPI_PROGRAM_TABLES if table_exists(connection, table.name)
    )
    if collisions:
        raise SchemaDriftError(
            tuple(
                f"{name}: untracked KPI program object exists before migration 10"
                for name in collisions
            )
        )
    for table in KPI_PROGRAM_TABLES:
        _create_child_table(connection, table, table.name)
        _create_child_indexes(connection, table)
        _create_table_triggers(connection, table)
        assert_table_matches(connection, table)
    connection.execute(
        "UPDATE workspace_state SET schema_generation=?",
        (10,),
    )


def _migration_0011_create_point_cloud_run_store(
    connection: sqlite3.Connection,
) -> None:
    collisions = tuple(
        table.name for table in POINT_CLOUD_RUN_TABLES if table_exists(connection, table.name)
    )
    if collisions:
        raise SchemaDriftError(
            tuple(
                f"{name}: untracked point-cloud run object exists before migration 11"
                for name in collisions
            )
        )
    for table in POINT_CLOUD_RUN_TABLES:
        _create_child_table(connection, table, table.name)
        _create_child_indexes(connection, table)
        _create_table_triggers(connection, table)
        assert_table_matches(connection, table)
    connection.execute(
        "UPDATE workspace_state SET schema_generation=?",
        (11,),
    )


def _migration_0012_create_deck_material_query_views(
    connection: sqlite3.Connection,
) -> None:
    view_names = {"v_bridge_span_sets", "v_bridge_deck_material_summary"}
    collisions = tuple(name for name in sorted(view_names) if view_exists(connection, name))
    if collisions:
        raise SchemaDriftError(
            tuple(
                f"{name}: untracked deck-material query view exists before migration 12"
                for name in collisions
            )
        )
    for view in QUERY_SEMANTIC_VIEWS:
        if view.name in view_names:
            connection.execute(view.sql)
    connection.execute(
        "UPDATE workspace_state SET schema_generation=?",
        (12,),
    )


def _migration_0013_create_current_deck_material_query_view(
    connection: sqlite3.Connection,
) -> None:
    view_name = "v_current_bridge_deck_material_summary"
    if view_exists(connection, view_name):
        raise SchemaDriftError(
            (f"{view_name}: untracked current deck-material view exists before migration 13",)
        )
    view = next(view for view in QUERY_SEMANTIC_VIEWS if view.name == view_name)
    connection.execute(view.sql)
    connection.execute(
        "UPDATE workspace_state SET schema_generation=?",
        (13,),
    )


def _migration_0014_allow_loaded_inventory_in_planning(
    connection: sqlite3.Connection,
) -> None:
    # Rebuild the reviewed query boundary as one coherent dependency graph.
    # The compatibility resource names remain stable for stored plans, while
    # their rows now prefer governed publication pointers and fill any gaps
    # from the loaded bridge projection.
    for view in reversed(QUERY_SEMANTIC_VIEWS):
        connection.execute(f'DROP VIEW IF EXISTS "{view.name}"')
    for view in QUERY_SEMANTIC_VIEWS:
        connection.execute(view.sql)
    connection.execute(
        "UPDATE workspace_state SET schema_generation=?",
        (14,),
    )


def _migration_0015_make_loaded_inventory_authoritative(
    connection: sqlite3.Connection,
) -> None:
    """Rebuild compatibility views from exact yearly inventory rows only."""

    for view in reversed(QUERY_SEMANTIC_VIEWS):
        connection.execute(f'DROP VIEW IF EXISTS "{view.name}"')
    for view in QUERY_SEMANTIC_VIEWS:
        connection.execute(view.sql)
    connection.execute(
        "UPDATE workspace_state SET schema_generation=?",
        (15,),
    )


def _migration_0016_finalize_loaded_inventory_views(
    connection: sqlite3.Connection,
) -> None:
    """Finalize named synthetic identities in the loaded-only view graph."""

    for view in reversed(QUERY_SEMANTIC_VIEWS):
        connection.execute(f'DROP VIEW IF EXISTS "{view.name}"')
    for view in QUERY_SEMANTIC_VIEWS:
        connection.execute(view.sql)
    connection.execute(
        "UPDATE workspace_state SET schema_generation=?",
        (16,),
    )


def _migration_0017_apply_snbi_component_condition_semantics(
    connection: sqlite3.Connection,
) -> None:
    """Rebuild condition views using the minimum applicable B.C.01--B.C.04."""

    for view in reversed(QUERY_SEMANTIC_VIEWS):
        connection.execute(f'DROP VIEW IF EXISTS "{view.name}"')
    for view in QUERY_SEMANTIC_VIEWS:
        connection.execute(view.sql)
    connection.execute(
        "UPDATE workspace_state SET schema_generation=?",
        (17,),
    )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="adopt_features_projection",
        descriptor=(
            "create legacy features table when absent",
            "reject duplicate snapshot feature identities without deduplication",
            "rebuild features with ON UPDATE/DELETE CASCADE and preserve rows/ids",
            "create idx_features_bridge_year(state_code,bridge_number,data_year)",
            "create unique uq_features_snapshot_type(state_code,bridge_number,data_year,feature_type)",
        ),
        operation=_migration_0001_adopt_features,
    ),
    Migration(
        version=2,
        name="complete_feature_transition_fields",
        descriptor=tuple(
            f"add nullable features.{column.name} {column.declared_type}"
            for column in FEATURE_TRANSITION_COLUMNS
        ),
        operation=_migration_0002_complete_feature_transition_fields,
    ),
    Migration(
        version=3,
        name="complete_projection_child_integrity",
        descriptor=(
            "preflight every child shape, natural identity, trigger, index, and integer value",
            "rebuild features with official INTEGER affinities for BH08-BH10 and BH17",
            "rebuild elements with official INTEGER affinities for BE03 and BCS01-BCS04",
            "rebuild routes, span_sets, substructure_sets, elements, inspections, posting_evaluations, posting_statuses, and works",
            "set every child snapshot foreign key to ON UPDATE CASCADE ON DELETE CASCADE",
            "add exact unique identities, including null-safe posting status date and BW02+BW03 works identity",
            "preserve IDs and row counts; reject duplicates, incompatible integer values, or foreign-key drift",
        ),
        operation=_migration_0003_complete_child_integrity,
    ),
    Migration(
        version=4,
        name="create_immutable_revision_store",
        descriptor=(
            "create workspace-scoped UUID revision, provenance, validation, publication, audit, and generation tables",
            "store source artifact metadata and internal storage URI without artifact bytes; keep repeated digest receipts distinct",
            "make source artifact sha256 and byte_size immutable",
            "make source records, conversion batches, eligibility decisions, snapshot revisions, field lineage, validation evidence, and mutation audit append-only",
            "record each revision's ordered multi-record source set in immutable snapshot_revision_sources",
            "keep publication as one mutable pointer per workspace/state/bridge/year while revisions remain immutable",
            "enforce workspace-scoped composite foreign keys and RESTRICT evidence deletion",
            "do not seed a default workspace or backfill legacy bridge rows",
        ),
        operation=_migration_0004_create_revision_store,
    ),
    Migration(
        version=5,
        name="create_bounded_query_boundary",
        descriptor=(
            "create immutable New Jersey county reference data without query-time writes",
            "create append-only privacy-minimized query audit events",
            "create checksummed publication-only bridge, feature, route, condition, county, element, work, and posting semantic views",
            "define latest as the latest published year per workspace/state/bridge rather than one global year",
            "advance workspace schema generations without changing data generations",
        ),
        operation=_migration_0005_create_query_boundary,
    ),
    Migration(
        version=6,
        name="create_immutable_query_response_audit",
        descriptor=(
            "create one immutable privacy-minimized query response audit row per workspace/request",
            "store only a value-redacted canonical BridgeQueryPlan and its checksum",
            "store compiled-query and bounded-evidence checksums, counts, and truncation metadata without SQL or rows",
            "store answer-verification modes, statuses, counts, and schema/data/catalog/dictionary versions",
            "enforce workspace foreign key, request uniqueness, append-only triggers, and workspace-time index",
            "advance workspace schema generations without changing data generations",
        ),
        operation=_migration_0006_create_query_response_audit,
    ),
    Migration(
        version=7,
        name="create_private_chat_sessions",
        descriptor=(
            "create private chat sessions scoped by workspace and authenticated owner",
            "create bounded chat turns with request and cited-revision receipts",
            "label user, unverified model, and verified answer provenance explicitly",
            "store evidence receipts separately from verified-answer receipts",
            "enforce composite owner/session foreign keys and cascade turn deletion",
            "index owner recency, retention expiry, session history, and request receipts",
            "keep sharing disabled and advance schema generation without changing data generation",
        ),
        operation=_migration_0007_create_private_chat_sessions,
    ),
    Migration(
        version=8,
        name="create_immutable_deployment_identity",
        descriptor=(
            "create one immutable deployment binding per database",
            "bind deployment UUID, customer slug, workspace, and exact marker digest",
            "reject a second binding row and all binding updates or deletes",
            "reference the bound workspace and advance schema generation without changing data generation",
            "leave the binding empty until the explicit dedicated-customer bootstrap",
        ),
        operation=_migration_0008_create_deployment_identity,
    ),
    Migration(
        version=9,
        name="create_forecast_analytics",
        descriptor=(
            "create immutable workspace-scoped forecast datasets and model versions",
            "create immutable treatment-effect models with applicability and overlap evidence",
            "persist exact historical-evaluation receipts and release qualification",
            "persist exact origin-to-ground-truth hindcast prediction rows",
            "create immutable source-revision-bound forecast runs, predictions, and issues",
            "enforce append-only evidence and advance schema generation without changing data generation",
        ),
        operation=_migration_0009_create_forecast_analytics,
    ),
    Migration(
        version=10,
        name="create_kpi_program_store",
        descriptor=(
            "create immutable versioned KPI measure definitions and reporting periods",
            "create immutable governed program source receipts with reviewer status",
            "create independent immutable approval decisions for program sources",
            "persist actual, budgeted, revised, target, and estimate values as exact decimals",
            "persist duplicate, missing, unit, and reconciliation issues",
            "enforce workspace-scoped evidence foreign keys and append-only records",
            "advance schema generation without changing data generation",
        ),
        operation=_migration_0010_create_kpi_program_store,
    ),
    Migration(
        version=11,
        name="create_point_cloud_run_store",
        descriptor=(
            "create durable workspace-scoped point-cloud runs bound to one job, source, profile, knowledge pack, and published snapshot",
            "persist immutable ordered source, VLM-call, component, and measurement candidate evidence",
            "persist append-only reviewer decisions and one mutable accepted publication pointer per source/profile",
            "persist durable derivative-retention outbox identity without storing unrestricted prompt text or local paths",
            "enforce workspace-scoped foreign keys and advance schema generation without changing data generation",
        ),
        operation=_migration_0011_create_point_cloud_run_store,
    ),
    Migration(
        version=12,
        name="create_deck_material_query_views",
        descriptor=(
            "create a publication-only span-set query view exposing BSP01 through BSP13",
            "create one publication-only bridge snapshot deck-material summary row",
            "define concrete deck codes as C01-C05, CX, CP-T, and CR-T",
            "expose known-material and concrete-material counts without raw-table access",
            "advance workspace schema generation without changing data generation",
        ),
        operation=_migration_0012_create_deck_material_query_views,
    ),
    Migration(
        version=13,
        name="create_current_deck_material_query_view",
        descriptor=(
            "create a bounded current-snapshot BSP09 deck-material summary view",
            "preserve historical deck-material views for exact-year and trend queries",
            "advance schema generation without changing data generation",
        ),
        operation=_migration_0013_create_current_deck_material_query_view,
    ),
    Migration(
        version=14,
        name="allow_loaded_inventory_in_planning",
        descriptor=(
            "prefer governed published snapshot pointers when the workspace state has them",
            "include all loaded legacy bridge rows when the workspace state has no publication pointers",
            "expose child, condition, county, span, and deck data through the same reviewed semantic boundary",
            "define latest as the single global latest available inventory year for the authorized state",
            "preserve stable semantic resource identifiers and advance schema generation without changing data generation",
        ),
        operation=_migration_0014_allow_loaded_inventory_in_planning,
    ),
    Migration(
        version=15,
        name="make_loaded_inventory_authoritative",
        descriptor=(
            "treat every exact state, bridge, and inventory-year row as an authoritative application snapshot",
            "remove published snapshot pointers from active semantic-view selection",
            "preserve stable semantic resource identifiers for query-plan compatibility",
            "define current data from the latest loaded inventory year and advance schema generation",
        ),
        operation=_migration_0015_make_loaded_inventory_authoritative,
    ),
    Migration(
        version=16,
        name="finalize_loaded_inventory_views",
        descriptor=(
            "rebuild the loaded-only semantic view graph with explicitly named synthetic row identities",
            "keep exact state, bridge, and inventory-year rows authoritative",
            "advance schema generation without changing inventory data",
        ),
        operation=_migration_0016_finalize_loaded_inventory_views,
    ),
    Migration(
        version=17,
        name="apply_snbi_component_condition_semantics",
        descriptor=(
            "treat SNBI N condition codes as not applicable",
            "calculate bridge condition from the minimum applicable B.C.01 through B.C.04 rating",
            "use FHWA-calculated B.C.13 only when component ratings are unavailable",
        ),
        operation=_migration_0017_apply_snbi_component_condition_semantics,
    ),
)


def _validate_registry(migrations: Sequence[Migration]) -> tuple[Migration, ...]:
    ordered = tuple(migrations)
    versions = tuple(migration.version for migration in ordered)
    expected = tuple(range(1, len(ordered) + 1))
    if versions != expected:
        raise MigrationStateError(
            f"Migration registry versions must be contiguous and ordered: "
            f"expected {expected!r}, found {versions!r}"
        )
    names = tuple(migration.name for migration in ordered)
    if len(set(names)) != len(names):
        raise MigrationStateError("Migration registry names must be unique")
    return ordered


def _user_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def _bootstrap_history_table(connection: sqlite3.Connection) -> None:
    if not table_exists(connection, "schema_migrations"):
        connection.execute(SCHEMA_MIGRATIONS_CREATE_SQL)


def _read_history(connection: sqlite3.Connection) -> tuple[tuple[int, str], ...]:
    return tuple(
        (int(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT version, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    )


def _validate_history(
    connection: sqlite3.Connection,
    migrations: Sequence[Migration],
) -> int:
    history = _read_history(connection)
    history_versions = tuple(version for version, _checksum in history)
    expected_prefix = tuple(range(1, len(history) + 1))
    if history_versions != expected_prefix:
        raise MigrationStateError(
            f"schema_migrations must be a contiguous prefix: "
            f"expected {expected_prefix!r}, found {history_versions!r}"
        )
    if len(history) > len(migrations):
        raise MigrationStateError(
            f"Database schema version {history[-1][0]} is newer than this "
            f"application's version {len(migrations)}"
        )
    for version, stored_checksum in history:
        expected_checksum = migrations[version - 1].checksum
        if stored_checksum != expected_checksum:
            raise MigrationChecksumError(
                f"Migration {version} checksum mismatch: stored "
                f"{stored_checksum!r}, expected {expected_checksum!r}"
            )

    recorded_version = history[-1][0] if history else 0
    pragma_version = _user_version(connection)
    if pragma_version != recorded_version:
        raise MigrationStateError(
            f"PRAGMA user_version ({pragma_version}) does not match "
            f"schema_migrations ({recorded_version})"
        )
    return recorded_version


def apply_migrations(
    connection: sqlite3.Connection,
    *,
    app_revision: str = "unknown",
    migrations: Sequence[Migration] = MIGRATIONS,
    manifest: SchemaManifest = AUTHORITATIVE_SCHEMA,
) -> MigrationResult:
    """Apply all pending migrations in one all-or-nothing transaction.

    The supplied connection remains open.  It must not already be inside a
    transaction, because pretending a savepoint can protect PRAGMA state would
    make rollback guarantees dependent on caller behavior.
    """

    registry = _validate_registry(migrations)
    if manifest.version != len(registry):
        raise MigrationStateError(
            f"Manifest version {manifest.version} does not match migration "
            f"registry version {len(registry)}"
        )
    if manifest.version != LATEST_SCHEMA_VERSION and migrations is MIGRATIONS:
        raise MigrationStateError(
            f"Manifest version {manifest.version} does not match supported "
            f"version {LATEST_SCHEMA_VERSION}"
        )
    if connection.in_transaction:
        raise MigrationStateError(
            "Migration connection already has an active transaction"
        )

    previous_version = 0
    applied: list[int] = []
    active_migration: Migration | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        # Read version state only after acquiring the writer lock.  Reading it
        # before BEGIN IMMEDIATE would leave a race in which another process
        # could migrate between preflight and execution.
        previous_version = _user_version(connection)
        history_preexisted = table_exists(connection, "schema_migrations")
        if not history_preexisted and previous_version != 0:
            raise MigrationStateError(
                "PRAGMA user_version is nonzero but schema_migrations is absent"
            )
        _bootstrap_history_table(connection)
        assert_table_matches(connection, SCHEMA_MIGRATIONS_TABLE)
        current_version = _validate_history(connection, registry)

        for migration in registry[current_version:]:
            active_migration = migration
            migration.operation(connection)
            connection.execute(
                "INSERT INTO schema_migrations "
                "(version, checksum, applied_at, app_revision) "
                "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?)",
                (migration.version, migration.checksum, str(app_revision or "unknown")),
            )
            connection.execute(f"PRAGMA user_version = {migration.version}")
            applied.append(migration.version)

        assert_schema_matches(connection, manifest)
        final_version = _validate_history(connection, registry)
        connection.commit()
        return MigrationResult(
            previous_version=previous_version,
            current_version=final_version,
            applied_versions=tuple(applied),
        )
    except Exception as exc:
        connection.rollback()
        if isinstance(exc, (MigrationError, SchemaDriftError)):
            raise
        if active_migration is not None:
            raise MigrationExecutionError(
                active_migration.version,
                active_migration.name,
                exc,
            ) from exc
        raise MigrationExecutionError(0, "migration_preflight", exc) from exc


def migrate_database(
    database_path: str | Path,
    *,
    app_revision: str = "unknown",
    timeout_seconds: float = 30.0,
) -> MigrationResult:
    """Open a database path, apply migrations, and close it deterministically."""

    connection = sqlite3.connect(str(database_path), timeout=timeout_seconds)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        return apply_migrations(connection, app_revision=app_revision)
    finally:
        connection.close()
