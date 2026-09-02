"""Red-first contracts for the PR4 relational projection hardening.

Every test in this module uses a disposable database supplied by ``conftest``.
The contracts intentionally exercise the public loader/export functions plus
physical SQLite constraints: a healthy integrity check is not sufficient when
canonical SNBI fields can still be silently discarded.
"""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from typing import Any, Dict

import pytest

from app.routers.services.constants import mapping
from app.routers.services.database import (
    ensure_schema,
    export_all_bridges_snbi,
    get_connection,
    load_json_to_db,
)
from app.routers.services.snbi_schema import ALL_NESTED_ARRAY_NAMES


STATE_CODE = 34
YEAR = 2025
FEATURE_TAGS = (
    "BRR01",
    "BRR02",
    "BRR03",
    "BN01",
    "BN02",
    "BN03",
    "BN04",
    "BN05",
    "BN06",
)


@pytest.fixture
def projection_db(test_workspace) -> str:
    db_path = str(test_workspace / "projection-contract.db")
    ensure_schema(db_path)
    return db_path


def _complete_bridge(bridge_number: str = "PR4-0001") -> Dict[str, Any]:
    bridge: Dict[str, Any] = {
        "BL01": STATE_CODE,
        "BID01": bridge_number,
    }
    for container in ALL_NESTED_ARRAY_NAMES:
        bridge[container] = []
    return bridge


def _load_one(db_path: str, bridge: Dict[str, Any]):
    return load_json_to_db(db_path, [bridge], data_year_override=YEAR)


def _export_one(db_path: str, bridge_number: str = "PR4-0001") -> Dict[str, Any]:
    rows = export_all_bridges_snbi(db_path, STATE_CODE, year=YEAR)
    return next(row for row in rows if row.get("BID01") == bridge_number)


def _table_count(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


class TestVersionedProjectionSchema:
    def test_migrations_are_checksummed_and_advance_user_version(self, projection_db):
        conn = sqlite3.connect(projection_db)
        try:
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            migration_table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            assert migration_table is not None
            rows = conn.execute(
                "SELECT version, checksum, applied_at, app_revision "
                "FROM schema_migrations ORDER BY version"
            ).fetchall()
        finally:
            conn.close()

        assert user_version > 0
        assert rows
        assert [row[0] for row in rows] == sorted({row[0] for row in rows})
        assert all(row[1] and row[2] and row[3] for row in rows)

        # Re-running startup migration must be an idempotent no-op.
        ensure_schema(projection_db)
        conn = sqlite3.connect(projection_db)
        try:
            repeated = conn.execute(
                "SELECT version, checksum, applied_at, app_revision "
                "FROM schema_migrations ORDER BY version"
            ).fetchall()
        finally:
            conn.close()
        assert repeated == rows

    def test_feature_projection_has_all_railroad_and_navigation_columns(self, projection_db):
        conn = sqlite3.connect(projection_db)
        try:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(features)").fetchall()
            }
        finally:
            conn.close()

        expected = {mapping[tag] for tag in FEATURE_TAGS}
        assert expected <= columns, f"missing feature projection columns: {sorted(expected - columns)}"

    def test_child_rows_are_cascade_owned_by_the_snapshot(self, projection_db):
        payload = _complete_bridge()
        payload["Features"] = [{"BF01": "H01", "BF02": "1"}]
        _load_one(projection_db, payload)

        conn = get_connection(projection_db)
        try:
            conn.execute(
                "DELETE FROM bridges WHERE state_code=? AND bridge_number=? AND data_year=?",
                (STATE_CODE, payload["BID01"], YEAR),
            )
            conn.commit()
        finally:
            conn.close()

        assert _table_count(projection_db, "bridges") == 0
        assert _table_count(projection_db, "features") == 0

    def test_duplicate_feature_identity_is_enforced_by_sqlite(self, projection_db):
        conn = get_connection(projection_db)
        try:
            conn.execute(
                "INSERT INTO bridges (state_code, bridge_number, data_year) VALUES (?, ?, ?)",
                (STATE_CODE, "DUP-FEATURE", YEAR),
            )
            row = (STATE_CODE, "DUP-FEATURE", YEAR, "H01")
            conn.execute(
                "INSERT INTO features (state_code, bridge_number, data_year, feature_type) "
                "VALUES (?, ?, ?, ?)",
                row,
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO features (state_code, bridge_number, data_year, feature_type) "
                    "VALUES (?, ?, ?, ?)",
                    row,
                )
        finally:
            conn.rollback()
            conn.close()


class TestLosslessProjectionRoundTrip:
    def test_railroad_and_navigation_fields_survive_load_export(self, projection_db):
        payload = _complete_bridge()
        payload["Features"] = [
            {
                "BF01": "R01",
                "BF02": "2",
                "BRR01": "F",
                "BRR02": 0,
                "BRR03": 12.5,
            },
            {
                "BF01": "W01",
                "BF02": "3",
                "BN01": "Y",
                "BN02": 0,
                "BN03": 55.5,
                "BN04": 0,
                "BN05": 31.2,
                "BN06": "P01",
            },
        ]

        assert _load_one(projection_db, payload)[:3] == (1, 0, 0)
        exported = _export_one(projection_db)
        by_type = {row["BF01"]: row for row in exported["Features"]}

        assert {tag: by_type["R01"][tag] for tag in FEATURE_TAGS[:3]} == {
            tag: payload["Features"][0][tag] for tag in FEATURE_TAGS[:3]
        }
        assert {tag: by_type["W01"][tag] for tag in FEATURE_TAGS[3:]} == {
            tag: payload["Features"][1][tag] for tag in FEATURE_TAGS[3:]
        }

    def test_falsy_values_and_code_strings_are_not_dropped_or_retyped(self, projection_db):
        payload = _complete_bridge("PR4-FALSY")
        payload.update(
            {
                "BL03": "00123",  # Place code is a five-character string in schema v04.
                "BG01": 0,
                # Exercise a falsy Python value on a permissive legacy
                # projection column. BG12 is officially a string code, so
                # this is storage-fidelity evidence, not schema validity.
                "BG12": False,
                "BAP05": "0",  # A code string, not numeric zero.
            }
        )

        _load_one(projection_db, payload)
        exported = _export_one(projection_db, "PR4-FALSY")

        assert exported["BL03"] == "00123"
        assert exported["BG01"] == 0
        assert exported["BG12"] in (False, "0")
        assert exported["BAP05"] == "0" and isinstance(exported["BAP05"], str)

    def test_every_canonical_array_preserves_multiple_rows_and_order(self, projection_db):
        payload = _complete_bridge("PR4-ARRAYS")
        payload.update(
            {
                "Elements": [
                    {"BE01": "12", "BE02": "0", "BE03": 100},
                    {"BE01": "13", "BE02": "0", "BE03": 200},
                ],
                "Features": [
                    {
                        "BF01": "H01",
                        "BF02": "1",
                        "Routes": [
                            {"BRT01": "C01", "BRT02": "001", "BRT03": "N"},
                            {"BRT01": "U01", "BRT02": "002", "BRT03": "S"},
                        ],
                    },
                    {"BF01": "R01", "BF02": "2"},
                ],
                "Inspections": [
                    {"BIE01": "N", "BIE02": "2025-01-01"},
                    {"BIE01": "S", "BIE02": "2025-02-01"},
                ],
                "PostingEvaluations": [
                    {"BEP01": "A", "BEP02": 0},
                    {"BEP01": "B", "BEP02": 1.25},
                ],
                "PostingStatuses": [
                    {"BPS01": "A", "BPS02": "2025-03-01"},
                    {"BPS01": "P", "BPS02": "2025-04-01"},
                ],
                "SpanSets": [
                    {
                        "BSP01": "S01",
                        "BSP02": 1,
                        "BSP03": 2,
                        "BSP04": "C",
                        "BSP05": "S",
                        "BSP06": "01",
                        "BSP07": "0",
                        "BSP08": "0",
                        "BSP09": "1",
                        "BSP13": "0",
                    },
                    {
                        "BSP01": "S02",
                        "BSP02": 2,
                        "BSP03": 3,
                        "BSP04": "P",
                        "BSP05": "C",
                        "BSP06": "02",
                        "BSP07": "0",
                        "BSP08": "0",
                        "BSP09": "1",
                        "BSP13": "0",
                    },
                ],
                "SubstructureSets": [
                    {
                        "BSB01": "P01",
                        "BSB02": 1,
                        "BSB03": "C01",
                        "BSB04": "P01",
                        "BSB05": "0",
                        "BSB06": "X",
                        "BSB07": "0",
                    },
                    {
                        "BSB01": "A01",
                        "BSB02": 2,
                        "BSB03": "C02",
                        "BSB04": "A01",
                        "BSB05": "0",
                        "BSB06": "X",
                        "BSB07": "0",
                    },
                ],
                "Works": [
                    {"BW02": 2019, "BW03": "R1"},
                    {"BW02": 2022, "BW03": "R2"},
                ],
            }
        )

        _load_one(projection_db, payload)
        exported = _export_one(projection_db, "PR4-ARRAYS")

        assert [row["BE01"] for row in exported["Elements"]] == ["12", "13"]
        assert [row["BF01"] for row in exported["Features"]] == ["H01", "R01"]
        assert [row["BRT01"] for row in exported["Features"][0]["Routes"]] == ["C01", "U01"]
        assert [row["BIE02"] for row in exported["Inspections"]] == ["2025-01-01", "2025-02-01"]
        assert [row["BEP01"] for row in exported["PostingEvaluations"]] == ["A", "B"]
        assert [row["BPS02"] for row in exported["PostingStatuses"]] == ["2025-03-01", "2025-04-01"]
        assert [row["BSP01"] for row in exported["SpanSets"]] == ["S01", "S02"]
        assert [row["BSB01"] for row in exported["SubstructureSets"]] == ["P01", "A01"]
        assert [row["BW02"] for row in exported["Works"]] == [2019, 2022]

    def test_complete_same_year_replacement_clears_omitted_scalars_and_children(self, projection_db):
        first = _complete_bridge("PR4-REPLACE")
        first.update(
            {
                "BID02": "Old name",
                "BL11": "Old location",
                "Features": [{"BF01": "H01", "BF02": "1"}],
                "Elements": [{"BE01": "12", "BE02": "0", "BE03": 100}],
            }
        )
        _load_one(projection_db, first)

        replacement = _complete_bridge("PR4-REPLACE")
        replacement["BW01"] = 2001
        _load_one(projection_db, replacement)
        exported = _export_one(projection_db, "PR4-REPLACE")

        assert "BID02" not in exported
        assert "BL11" not in exported
        assert exported["BW01"] == 2001
        for container in ALL_NESTED_ARRAY_NAMES:
            assert exported.get(container, []) == [], f"stale rows remained in {container}"

    def test_export_does_not_fabricate_bn01(self, projection_db):
        payload = _complete_bridge("PR4-NO-BN")
        payload["Features"] = [{"BF01": "W01", "BF02": "3"}]
        _load_one(projection_db, payload)

        exported = _export_one(projection_db, "PR4-NO-BN")

        assert "BN01" not in exported["Features"][0]


class TestFailFastAtomicBehavior:
    @pytest.mark.parametrize(
        ("mutate", "rejected_tag"),
        [
            (lambda row: row.__setitem__("BZZ99", "unknown"), "BZZ99"),
            (lambda row: row.__setitem__("BRT01", "wrong top-level section"), "BRT01"),
            (
                lambda row: row.__setitem__(
                    "Features", [{"BF01": "H01", "BSP01": "wrong feature section"}]
                ),
                "BSP01",
            ),
        ],
    )
    def test_unknown_or_wrong_section_field_rejects_before_any_write(
        self, projection_db, mutate, rejected_tag
    ):
        valid = _complete_bridge("PR4-VALID-FIRST")
        invalid = _complete_bridge("PR4-INVALID")
        mutate(invalid)

        result = load_json_to_db(
            projection_db,
            [valid, invalid],
            data_year_override=YEAR,
        )

        assert result[0] == 0 and result[1] == 0
        assert _table_count(projection_db, "bridges") == 0
        assert any(rejected_tag in reason for reason in result[3])

    def test_induced_child_insert_failure_rolls_back_the_whole_batch(self, projection_db):
        conn = sqlite3.connect(projection_db)
        try:
            conn.execute(
                """
                CREATE TRIGGER induce_feature_failure
                BEFORE INSERT ON features
                WHEN NEW.feature_type = 'FAIL'
                BEGIN
                    SELECT RAISE(ABORT, 'induced child failure');
                END
                """
            )
            conn.commit()
        finally:
            conn.close()

        first = _complete_bridge("PR4-BATCH-1")
        first["Elements"] = [{"BE01": "12", "BE02": "0"}]
        second = _complete_bridge("PR4-BATCH-2")
        second["Features"] = [{"BF01": "FAIL"}]

        result = load_json_to_db(
            projection_db,
            [first, second],
            data_year_override=YEAR,
        )

        assert result[0] == 0 and result[1] == 0
        for table in ("bridges", "features", "elements"):
            assert _table_count(projection_db, table) == 0
        conn = sqlite3.connect(projection_db)
        try:
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            conn.close()


class TestDeterministicVisibleExport:
    def test_export_restores_integer_like_code_values_from_real_columns(self, projection_db):
        bridge = _complete_bridge("PR4-CODE-NORMALIZATION")
        bridge.update({"BC01": "6", "BC02": "5", "BC03": "7", "BC04": "N"})
        _load_one(projection_db, bridge)

        exported = _export_one(projection_db, "PR4-CODE-NORMALIZATION")

        assert exported["BC01"] == "6"
        assert exported["BC02"] == "5"
        assert exported["BC03"] == "7"
        assert exported["BC04"] == "N"

    def test_export_is_stably_ordered_and_byte_deterministic(self, projection_db):
        later = _complete_bridge("PR4-0002")
        earlier = _complete_bridge("PR4-0001")
        load_json_to_db(projection_db, [later, earlier], data_year_override=YEAR)

        first = export_all_bridges_snbi(projection_db, STATE_CODE, year=YEAR)
        second = export_all_bridges_snbi(projection_db, STATE_CODE, year=YEAR)

        assert [row["BID01"] for row in first] == ["PR4-0001", "PR4-0002"]
        encode = lambda value: json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        assert encode(first) == encode(second)

    def test_export_surfaces_child_query_failure(self, projection_db):
        _load_one(projection_db, _complete_bridge("PR4-QUERY-ERROR"))
        conn = sqlite3.connect(projection_db)
        try:
            conn.execute("DROP TABLE features")
            conn.commit()
        finally:
            conn.close()

        with pytest.raises((sqlite3.DatabaseError, RuntimeError), match="features"):
            export_all_bridges_snbi(projection_db, STATE_CODE, year=YEAR)
