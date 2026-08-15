"""Red-first contracts for every canonical PR4 projection child table.

The immediate integrity repair deliberately keeps the legacy composite
snapshot key.  Revision-owned child IDs and ordinals are a PR5 concern.  These
tests instead require the strongest identities that can be expressed safely
in the current projection without rejecting valid legacy evidence (notably a
missing BPS02 or two different Works entries in the same year).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.routers.services.constants import mapping
from app.routers.services.database import ensure_schema, load_json_to_db


STATE_CODE = 34
BRIDGE_NUMBER = "PR4-ALL-CHILDREN"
DATA_YEAR = 2025
SNAPSHOT_COLUMNS = ("state_code", "bridge_number", "data_year")

CHILD_TABLES = (
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

SECTION_TO_TABLE = {
    "Elements": "elements",
    "Features": "features",
    "Routes": "routes",
    "Inspections": "inspections",
    "PostingEvaluations": "posting_evaluations",
    "PostingStatuses": "posting_statuses",
    "SpanSets": "span_sets",
    "SubstructureSets": "substructure_sets",
    "Works": "works",
}

IDENTITY_INSERTS = {
    "features": ({"feature_type": "H01"}, {"feature_type": "H01"}),
    "routes": (
        {"feature_type": "H01", "route_designation": "C01"},
        {"feature_type": "H01", "route_designation": "C01"},
    ),
    "span_sets": (
        {"span_configuration_designation": "S01"},
        {"span_configuration_designation": "S01"},
    ),
    "substructure_sets": (
        {"substructure_configuration_designation": "P01"},
        {"substructure_configuration_designation": "P01"},
    ),
    "elements": (
        {"element_number": "12", "element_parent_number": "0"},
        {"element_number": "12", "element_parent_number": "0"},
    ),
    "inspections": (
        {"inspection_type": "N", "inspection_begin_date": "20250101"},
        {"inspection_type": "N", "inspection_begin_date": "20250101"},
    ),
    "posting_evaluations": (
        {"legal_load_configuration": "A"},
        {"legal_load_configuration": "A"},
    ),
    "posting_statuses": (
        {"load_posting_status": "OPEN", "posting_status_change_date": "20250102"},
        {"load_posting_status": "OPEN", "posting_status_change_date": "20250102"},
    ),
    "works": (
        {"year_work_performed": 2020, "work_performed": "BR1"},
        {"year_work_performed": 2020, "work_performed": "BR1"},
    ),
}


@pytest.fixture
def child_schema_db(test_workspace) -> str:
    database_path = str(test_workspace / "all-child-schema-contract.sqlite3")
    ensure_schema(database_path)
    return database_path


def _connect(database_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _insert_snapshot(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO bridges (state_code, bridge_number, data_year) VALUES (?, ?, ?)",
        (STATE_CODE, BRIDGE_NUMBER, DATA_YEAR),
    )


def _insert_child(
    connection: sqlite3.Connection,
    table: str,
    values: dict[str, Any],
) -> None:
    row = {
        "state_code": STATE_CODE,
        "bridge_number": BRIDGE_NUMBER,
        "data_year": DATA_YEAR,
        **values,
    }
    columns = tuple(row)
    placeholders = ", ".join("?" for _ in columns)
    quoted_columns = ", ".join(f'"{column}"' for column in columns)
    connection.execute(
        f'INSERT INTO "{table}" ({quoted_columns}) VALUES ({placeholders})',
        tuple(row[column] for column in columns),
    )


def _foreign_keys(connection: sqlite3.Connection, table: str):
    grouped: dict[int, list[tuple[Any, ...]]] = {}
    for row in connection.execute(f'PRAGMA foreign_key_list("{table}")'):
        grouped.setdefault(int(row[0]), []).append(tuple(row))
    return [sorted(rows, key=lambda row: int(row[1])) for rows in grouped.values()]


def _official_schema() -> dict[str, Any]:
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "data"
        / "fhwa"
        / "artifacts"
        / "SNBI.Schema.v04.json"
    )
    return json.loads(schema_path.read_text(encoding="utf-8"))


def _section_properties(schema: dict[str, Any], section: str) -> dict[str, Any]:
    bridge_properties = schema["items"]["properties"]
    if section == "Routes":
        return bridge_properties["Features"]["items"]["properties"]["Routes"][
            "items"
        ]["properties"]
    items = bridge_properties[section]["items"]
    # The pinned FHWA v04 schema represents Works.items as a single-element
    # tuple schema while the other child collections use an object schema.
    if isinstance(items, list):
        items = items[0]
    return items["properties"]


def _declared_columns(connection: sqlite3.Connection, table: str) -> dict[str, str]:
    return {
        str(row[1]): str(row[2]).upper()
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    }


class TestAllChildSnapshotOwnership:
    @pytest.mark.parametrize("table", CHILD_TABLES)
    def test_every_child_has_exact_snapshot_cascade_foreign_key(
        self, child_schema_db, table
    ):
        connection = _connect(child_schema_db)
        try:
            matching = []
            for rows in _foreign_keys(connection, table):
                source = tuple(str(row[3]) for row in rows)
                target = tuple(str(row[4]) for row in rows)
                if str(rows[0][2]).lower() == "bridges" and source == SNAPSHOT_COLUMNS:
                    matching.append((rows, target))
        finally:
            connection.close()

        assert len(matching) == 1, (
            f"{table} must have exactly one composite snapshot FK to bridges"
        )
        rows, target = matching[0]
        assert target == SNAPSHOT_COLUMNS
        assert {str(row[5]).upper() for row in rows} == {"CASCADE"}
        assert {str(row[6]).upper() for row in rows} == {"CASCADE"}

    def test_representative_full_load_stays_integral_and_cascades(
        self, child_schema_db
    ):
        payload = {
            "BL01": STATE_CODE,
            "BID01": BRIDGE_NUMBER,
            "BL03": "00123",
            "Features": [
                {
                    "BF01": "H01",
                    "BF02": "1",
                    "Routes": [{"BRT01": "C01", "BRT02": "001"}],
                }
            ],
            "SpanSets": [{"BSP01": "S01", "BSP02": 1}],
            "SubstructureSets": [{"BSB01": "P01", "BSB02": 1}],
            "Elements": [{"BE01": "12", "BE02": "0", "BE03": 100}],
            "Inspections": [{"BIE01": "N", "BIE02": "20250101"}],
            "PostingEvaluations": [{"BEP01": "A", "BEP02": 1.0}],
            "PostingStatuses": [{"BPS01": "OPEN", "BPS02": "20250102"}],
            "Works": [{"BW02": 2020, "BW03": "BR1"}],
        }
        result = load_json_to_db(
            child_schema_db,
            [payload],
            data_year_override=DATA_YEAR,
        )
        assert result[:3] == (1, 0, 0), result

        connection = _connect(child_schema_db)
        try:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

            connection.execute(
                "UPDATE bridges SET bridge_number=? "
                "WHERE state_code=? AND bridge_number=? AND data_year=?",
                ("PR4-CASCADE-RENAMED", STATE_CODE, BRIDGE_NUMBER, DATA_YEAR),
            )
            for table in CHILD_TABLES:
                assert connection.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE bridge_number=?',
                    ("PR4-CASCADE-RENAMED",),
                ).fetchone() == (1,)

            connection.execute(
                "DELETE FROM bridges WHERE state_code=? AND bridge_number=? AND data_year=?",
                (STATE_CODE, "PR4-CASCADE-RENAMED", DATA_YEAR),
            )
            for table in CHILD_TABLES:
                assert connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone() == (0,)
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            connection.rollback()
            connection.close()


class TestSafeLegacyChildIdentity:
    @pytest.mark.parametrize("table", CHILD_TABLES)
    def test_exact_natural_identity_duplicate_is_rejected(self, child_schema_db, table):
        connection = _connect(child_schema_db)
        try:
            _insert_snapshot(connection)
            first, duplicate = IDENTITY_INSERTS[table]
            _insert_child(connection, table, first)
            with pytest.raises(sqlite3.IntegrityError):
                _insert_child(connection, table, duplicate)
        finally:
            connection.rollback()
            connection.close()

    def test_null_posting_date_is_allowed_but_not_a_duplicate_identity(
        self, child_schema_db
    ):
        connection = _connect(child_schema_db)
        try:
            _insert_snapshot(connection)
            _insert_child(
                connection,
                "posting_statuses",
                {"load_posting_status": "OPEN", "posting_status_change_date": None},
            )
            # A different status with the same absent date is distinct evidence.
            _insert_child(
                connection,
                "posting_statuses",
                {"load_posting_status": "POSTED", "posting_status_change_date": None},
            )
            with pytest.raises(sqlite3.IntegrityError):
                _insert_child(
                    connection,
                    "posting_statuses",
                    {"load_posting_status": "OPEN", "posting_status_change_date": None},
                )
        finally:
            connection.rollback()
            connection.close()

    def test_same_work_year_allows_distinct_work_descriptions(self, child_schema_db):
        connection = _connect(child_schema_db)
        try:
            _insert_snapshot(connection)
            _insert_child(
                connection,
                "works",
                {"year_work_performed": 2020, "work_performed": "BR1"},
            )
            _insert_child(
                connection,
                "works",
                {"year_work_performed": 2020, "work_performed": "RECONSTRUCTED"},
            )
            assert connection.execute("SELECT COUNT(*) FROM works").fetchone() == (2,)
        finally:
            connection.rollback()
            connection.close()


class TestOfficialTagAffinities:
    def test_every_child_tag_uses_the_exact_official_json_type_affinity(
        self, child_schema_db
    ):
        schema = _official_schema()
        expected_affinity = {"string": "TEXT", "integer": "INTEGER", "number": "REAL"}
        connection = _connect(child_schema_db)
        try:
            mismatches: list[str] = []
            for section, table in SECTION_TO_TABLE.items():
                live = _declared_columns(connection, table)
                for tag, definition in _section_properties(schema, section).items():
                    # ``Routes`` is the nested collection container in the
                    # Features schema, not an SNBI data element/SQL column.
                    if tag == "Routes":
                        continue
                    column = mapping[tag]
                    expected = expected_affinity[str(definition["type"])]
                    actual = live.get(column)
                    if actual != expected:
                        mismatches.append(
                            f"{table}.{column} ({tag}): {actual!r} != {expected!r}"
                        )
        finally:
            connection.close()

        assert mismatches == [], "official affinity drift:\n" + "\n".join(mismatches)

    def test_bl03_place_code_is_declared_text(self, child_schema_db):
        connection = _connect(child_schema_db)
        try:
            assert _declared_columns(connection, "bridges")[mapping["BL03"]] == "TEXT"
        finally:
            connection.close()
