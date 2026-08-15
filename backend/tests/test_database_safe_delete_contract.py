"""Transactional deletion contracts for PR4.

The production database is guarded by ``conftest``.  These tests build a
complete disposable inventory and prove that deletion follows an explicit
ownership allowlist instead of walking arbitrary SQLite tables.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

import pytest

from app.routers.services import database


STATE = 34
OTHER_STATE = 36
YEAR = 2025
OTHER_YEAR = 2024
BRIDGE_A = "DELETE-A"
BRIDGE_B = "DELETE-B"
BRIDGE_PRIOR = "DELETE-A"
BRIDGE_OTHER_STATE = "DELETE-C"

PROJECTION_CHILD_TABLES = (
    "features",
    "routes",
    "span_sets",
    "substructure_sets",
    "elements",
    "inspections",
    "posting_evaluations",
    "posting_statuses",
    "works",
)
PROVENANCE_TABLES = ("bridge_field_metadata", "bridge_source_records")
OWNED_CHILD_TABLES = PROJECTION_CHILD_TABLES + PROVENANCE_TABLES


@pytest.fixture
def populated_db(test_workspace) -> str:
    path = str(test_workspace / "safe-delete.db")
    database.ensure_schema(path)
    conn = sqlite3.connect(path)
    try:
        snapshots = (
            (STATE, BRIDGE_A, YEAR),
            (STATE, BRIDGE_B, YEAR),
            (STATE, BRIDGE_PRIOR, OTHER_YEAR),
            (OTHER_STATE, BRIDGE_OTHER_STATE, YEAR),
        )
        conn.executemany(
            "INSERT INTO bridges (state_code, bridge_number, data_year) VALUES (?, ?, ?)",
            snapshots,
        )
        for state, bridge, year in snapshots:
            conn.execute(
                "INSERT INTO features "
                "(state_code, bridge_number, data_year, feature_type) VALUES (?, ?, ?, ?)",
                (state, bridge, year, "H01"),
            )
            conn.execute(
                "INSERT INTO routes "
                "(state_code, bridge_number, data_year, feature_type, route_designation) "
                "VALUES (?, ?, ?, ?, ?)",
                (state, bridge, year, "H01", "C01"),
            )
            conn.execute(
                "INSERT INTO span_sets "
                "(state_code, bridge_number, data_year, span_configuration_designation) "
                "VALUES (?, ?, ?, ?)",
                (state, bridge, year, "S01"),
            )
            conn.execute(
                "INSERT INTO substructure_sets "
                "(state_code, bridge_number, data_year, substructure_configuration_designation) "
                "VALUES (?, ?, ?, ?)",
                (state, bridge, year, "P01"),
            )
            conn.execute(
                "INSERT INTO elements "
                "(state_code, bridge_number, data_year, element_number, element_parent_number) "
                "VALUES (?, ?, ?, ?, ?)",
                (state, bridge, year, "12", "0"),
            )
            conn.execute(
                "INSERT INTO inspections "
                "(state_code, bridge_number, data_year, inspection_type, inspection_begin_date) "
                "VALUES (?, ?, ?, ?, ?)",
                (state, bridge, year, "N", f"{year}-01-01"),
            )
            conn.execute(
                "INSERT INTO posting_evaluations "
                "(state_code, bridge_number, data_year, legal_load_configuration) "
                "VALUES (?, ?, ?, ?)",
                (state, bridge, year, "A"),
            )
            conn.execute(
                "INSERT INTO posting_statuses "
                "(state_code, bridge_number, data_year, load_posting_status) "
                "VALUES (?, ?, ?, ?)",
                (state, bridge, year, "A"),
            )
            conn.execute(
                "INSERT INTO works "
                "(state_code, bridge_number, data_year, year_work_performed, work_performed) "
                "VALUES (?, ?, ?, ?, ?)",
                (state, bridge, year, year, "R1"),
            )
            conn.execute(
                "INSERT INTO bridge_field_metadata "
                "(state_code, bridge_number, data_year, table_name, tag) "
                "VALUES (?, ?, ?, ?, ?)",
                (state, bridge, year, "bridges", "BL01"),
            )
            conn.execute(
                "INSERT INTO bridge_source_records "
                "(state_code, bridge_number, data_year, source_name, source_payload_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (state, bridge, year, "NBI TXT", "{}"),
            )

        # Derived JSON_LOAD history belongs to the matching imported snapshot.
        # Actual uploaded/extracted inspection reports are independent evidence.
        for state, bridge, year in snapshots:
            conn.execute(
                "INSERT INTO inspection_report_history "
                "(state_code, bridge_number, report_year, report_file) VALUES (?, ?, ?, ?)",
                (state, bridge, year, "JSON_LOAD"),
            )
            conn.execute(
                "INSERT INTO inspection_report_history "
                "(state_code, bridge_number, report_year, report_file) VALUES (?, ?, ?, ?)",
                (state, bridge, year, f"inspection-{state}-{bridge}-{year}.pdf"),
            )

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS custom_tables (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                state_code INTEGER NOT NULL,
                bridge_number TEXT,
                name TEXT NOT NULL,
                columns_json TEXT NOT NULL,
                color TEXT,
                scope TEXT NOT NULL DEFAULT 'bridge'
            );
            CREATE TABLE IF NOT EXISTS custom_table_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                custom_table_id INTEGER NOT NULL,
                bridge_number TEXT NOT NULL,
                cells_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE bridge_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                state_code INTEGER,
                bridge_number TEXT,
                data_year INTEGER,
                filename TEXT
            );
            CREATE TABLE extension_snapshot_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                state_code INTEGER,
                bridge_number TEXT,
                data_year INTEGER,
                payload TEXT
            );
            """
        )
        custom_id = conn.execute(
            "INSERT INTO custom_tables "
            "(state_code, bridge_number, name, columns_json, scope) VALUES (?, ?, ?, ?, ?)",
            (STATE, BRIDGE_A, "User notes", "[]", "bridge"),
        ).lastrowid
        conn.execute(
            "INSERT INTO custom_table_rows (custom_table_id, bridge_number, cells_json) "
            "VALUES (?, ?, ?)",
            (custom_id, BRIDGE_A, '{"note":"preserve"}'),
        )
        conn.execute(
            "INSERT INTO bridge_attachments "
            "(state_code, bridge_number, data_year, filename) VALUES (?, ?, ?, ?)",
            (STATE, BRIDGE_A, YEAR, "inspection.pdf"),
        )
        conn.execute(
            "INSERT INTO extension_snapshot_cache "
            "(state_code, bridge_number, data_year, payload) VALUES (?, ?, ?, ?)",
            (STATE, BRIDGE_A, YEAR, "must survive because it is not owned"),
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _count(path: str, table: str, where: str = "", params=()) -> int:
    conn = sqlite3.connect(path)
    try:
        suffix = f" WHERE {where}" if where else ""
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}{suffix}", params).fetchone()[0])
    finally:
        conn.close()


def _target_count(path: str, table: str, bridge: str, year: int = YEAR) -> int:
    return _count(
        path,
        table,
        "state_code=? AND bridge_number=? AND data_year=?",
        (STATE, bridge, year),
    )


def _assert_external_data_preserved(path: str) -> None:
    assert _count(path, "custom_tables") == 1
    assert _count(path, "custom_table_rows") == 1
    assert _count(path, "bridge_attachments") == 1
    assert _count(path, "extension_snapshot_cache") == 1


def _install_delete_failure(path: str, table: str = "elements") -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            f"""
            CREATE TRIGGER fail_owned_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'induced owned-row delete failure');
            END
            """
        )
        conn.commit()
    finally:
        conn.close()


class TestSnapshotDeleteAllowlist:
    def test_single_snapshot_delete_is_scoped_and_preserves_external_evidence(self, populated_db):
        assert database.delete_single_bridge(populated_db, STATE, BRIDGE_A, YEAR) == 1

        assert _target_count(populated_db, "bridges", BRIDGE_A) == 0
        for table in OWNED_CHILD_TABLES:
            assert _target_count(populated_db, table, BRIDGE_A) == 0
            assert _target_count(populated_db, table, BRIDGE_PRIOR, OTHER_YEAR) == 1
        assert _count(
            populated_db,
            "inspection_report_history",
            "state_code=? AND bridge_number=? AND report_year=? AND report_file='JSON_LOAD'",
            (STATE, BRIDGE_A, YEAR),
        ) == 0
        assert _count(
            populated_db,
            "inspection_report_history",
            "state_code=? AND bridge_number=? AND report_year=? AND report_file<>'JSON_LOAD'",
            (STATE, BRIDGE_A, YEAR),
        ) == 1
        assert _target_count(populated_db, "bridges", BRIDGE_B) == 1
        _assert_external_data_preserved(populated_db)

    def test_state_year_delete_is_scoped_and_preserves_external_evidence(self, populated_db):
        assert database.delete_bridges_by_year(populated_db, STATE, YEAR) == 2

        assert _count(
            populated_db,
            "bridges",
            "state_code=? AND data_year=?",
            (STATE, YEAR),
        ) == 0
        for table in OWNED_CHILD_TABLES:
            assert _count(
                populated_db,
                table,
                "state_code=? AND data_year=?",
                (STATE, YEAR),
            ) == 0
            assert _target_count(populated_db, table, BRIDGE_PRIOR, OTHER_YEAR) == 1
        assert _count(
            populated_db,
            "inspection_report_history",
            "state_code=? AND report_year=? AND report_file='JSON_LOAD'",
            (STATE, YEAR),
        ) == 0
        assert _count(
            populated_db,
            "inspection_report_history",
            "state_code=? AND report_year=? AND report_file<>'JSON_LOAD'",
            (STATE, YEAR),
        ) == 2
        assert _count(
            populated_db,
            "bridges",
            "state_code=? AND data_year=?",
            (OTHER_STATE, YEAR),
        ) == 1
        _assert_external_data_preserved(populated_db)


class TestSnapshotDeleteAtomicity:
    @pytest.mark.parametrize(
        "operation",
        [
            lambda path: database.delete_single_bridge(path, STATE, BRIDGE_A, YEAR),
            lambda path: database.delete_bridges_by_year(path, STATE, YEAR),
        ],
        ids=("single", "state-year"),
    )
    def test_induced_failure_rolls_back_every_owned_and_history_delete(
        self, populated_db, operation: Callable[[str], int]
    ):
        before = {
            table: _count(populated_db, table)
            for table in ("bridges",) + OWNED_CHILD_TABLES + ("inspection_report_history",)
        }
        _install_delete_failure(populated_db)

        with pytest.raises(sqlite3.DatabaseError, match="induced owned-row delete failure"):
            operation(populated_db)

        after = {
            table: _count(populated_db, table)
            for table in ("bridges",) + OWNED_CHILD_TABLES + ("inspection_report_history",)
        }
        assert after == before
        _assert_external_data_preserved(populated_db)


class TestClearAllOwnedData:
    def test_clear_uses_allowlist_preserves_real_reports_and_never_disables_fks(
        self, populated_db, monkeypatch
    ):
        statements: list[str] = []
        real_connect = sqlite3.connect

        def traced_connection(path, *args, **kwargs):
            conn = real_connect(path, *args, **kwargs)
            conn.execute("PRAGMA foreign_keys = ON")
            conn.set_trace_callback(statements.append)
            return conn

        monkeypatch.setattr(database, "get_connection", traced_connection)
        deleted = database.clear_all_data(populated_db)

        assert _count(populated_db, "bridges") == 0
        for table in OWNED_CHILD_TABLES:
            assert _count(populated_db, table) == 0
            assert table in deleted
        assert _count(
            populated_db,
            "inspection_report_history",
            "report_file='JSON_LOAD'",
        ) == 0
        assert _count(
            populated_db,
            "inspection_report_history",
            "report_file<>'JSON_LOAD'",
        ) == 4
        _assert_external_data_preserved(populated_db)
        assert not any("FOREIGN_KEYS" in sql.upper() and "OFF" in sql.upper() for sql in statements)

    def test_clear_induced_failure_rolls_back_everything(self, populated_db):
        before = {
            table: _count(populated_db, table)
            for table in ("bridges",) + OWNED_CHILD_TABLES + ("inspection_report_history",)
        }
        _install_delete_failure(populated_db)

        with pytest.raises(sqlite3.DatabaseError, match="induced owned-row delete failure"):
            database.clear_all_data(populated_db)

        after = {
            table: _count(populated_db, table)
            for table in ("bridges",) + OWNED_CHILD_TABLES + ("inspection_report_history",)
        }
        assert after == before
        _assert_external_data_preserved(populated_db)


def test_fully_cascaded_projection_uses_parent_delete(populated_db):
    """A migrated cascade fires after its parent disappears; manual child-first does not."""

    # ``features`` is the first PR4 table migrated to ON DELETE CASCADE.  Build a
    # small sentinel trigger that rejects a direct child delete while accepting
    # a delete invoked by the parent cascade.  Other legacy children force the
    # general fixture down the compatibility path, so remove their target rows
    # and tables to model a minimal fully migrated projection.
    conn = sqlite3.connect(populated_db)
    try:
        for table in PROJECTION_CHILD_TABLES[1:]:
            conn.execute(f"DROP TABLE {table}")
        conn.execute(
            """
            CREATE TRIGGER reject_manual_feature_delete
            BEFORE DELETE ON features
            WHEN EXISTS (
                SELECT 1 FROM bridges
                WHERE state_code=OLD.state_code
                  AND bridge_number=OLD.bridge_number
                  AND data_year=OLD.data_year
            )
            BEGIN
                SELECT RAISE(ABORT, 'feature was deleted before its parent');
            END
            """
        )
        conn.commit()
    finally:
        conn.close()

    assert database.delete_single_bridge(populated_db, STATE, BRIDGE_A, YEAR) == 1
    assert _target_count(populated_db, "bridges", BRIDGE_A) == 0
    assert _target_count(populated_db, "features", BRIDGE_A) == 0
