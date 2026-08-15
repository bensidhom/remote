import sqlite3

import pytest

from app.routers.services.database import (
    ensure_schema,
    export_all_bridges_snbi,
    get_bridge_details,
    update_bridge_main,
)
from app.routers.services.mapper import map_bridge_to_snbi
from app.routers.services.snbi_validation import validate_snbi_payload


STATE_CODE = 34
BRIDGE_NUMBER = "01HML58"


class TestBridgeSnapshotScoping:
    @pytest.fixture(autouse=True)
    def isolated_database(self, test_workspace):
        self.db_file = test_workspace / "snapshot.db"
        self.db_path = str(self.db_file)
        ensure_schema(self.db_path)

        conn = sqlite3.connect(self.db_path)
        try:
            conn.executemany(
                """
                INSERT INTO bridges (
                    state_code, bridge_number, bridge_name, data_year,
                    year_built, channel_condition_rating
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (STATE_CODE, BRIDGE_NUMBER, "2023 snapshot", 2023, 1977, "6"),
                    (STATE_CODE, BRIDGE_NUMBER, "2024 snapshot", 2024, 1939, "7"),
                ],
            )
            conn.executemany(
                """
                INSERT INTO features (
                    state_code, bridge_number, data_year,
                    feature_type, feature_location, feature_name
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (STATE_CODE, BRIDGE_NUMBER, 2023, "W01", "B", "2023 waterway"),
                    (STATE_CODE, BRIDGE_NUMBER, 2024, "W01", "B", "2024 waterway"),
                ],
            )
            conn.executemany(
                """
                INSERT INTO elements (
                    state_code, bridge_number, data_year,
                    element_number, element_total_quantity
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (STATE_CODE, BRIDGE_NUMBER, 2023, "12", 100),
                    (STATE_CODE, BRIDGE_NUMBER, 2024, "13", 200),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        yield

    def test_year_scoped_get_returns_only_matching_snapshot(self):
        snapshot = get_bridge_details(
            self.db_path,
            STATE_CODE,
            BRIDGE_NUMBER,
            year=2023,
        )

        assert snapshot is not None
        assert snapshot["data_year"] == 2023
        assert snapshot["BW01"] == 1977
        assert int(snapshot["BC09"]) == 6
        assert [
            feature["feature_name"] for feature in snapshot["Features"]
        ] == ["2023 waterway"]
        assert [
            element["element_number"] for element in snapshot["Elements"]
        ] == ["12"]
        assert (
            all(feature["data_year"] == 2023 for feature in snapshot["Features"])
        )
        assert (
            all(element["data_year"] == 2023 for element in snapshot["Elements"])
        )

    def test_get_without_year_uses_latest_bridge_snapshot(self):
        snapshot = get_bridge_details(
            self.db_path,
            STATE_CODE,
            BRIDGE_NUMBER,
        )

        assert snapshot is not None
        assert snapshot["data_year"] == 2024
        assert snapshot["BW01"] == 1939
        assert int(snapshot["BC09"]) == 7
        assert [
            feature["feature_name"] for feature in snapshot["Features"]
        ] == ["2024 waterway"]

    def test_save_updates_only_selected_year_and_data_year_is_immutable(self):
        updated = update_bridge_main(
            self.db_path,
            STATE_CODE,
            BRIDGE_NUMBER,
            {
                "data_year": 2023,
                "BL01": 99,
                "BID01": "CHANGED",
                "BW01": 1978,
                "BC09": "5",
            },
            year=2023,
        )
        assert updated == 1

        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                """
                SELECT data_year, year_built, channel_condition_rating
                FROM bridges
                WHERE state_code = ? AND bridge_number = ?
                ORDER BY data_year
                """,
                (STATE_CODE, BRIDGE_NUMBER),
            ).fetchall()
        finally:
            conn.close()

        assert rows == [(2023, 1978, 5.0), (2024, 1939, 7.0)]
        conn = sqlite3.connect(self.db_path)
        try:
            changed_identity_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM bridges
                WHERE state_code = 99 OR bridge_number = 'CHANGED'
                """
            ).fetchone()[0]
        finally:
            conn.close()
        assert changed_identity_count == 0

        with pytest.raises(ValueError, match="does not match"):
            update_bridge_main(
                self.db_path,
                STATE_CODE,
                BRIDGE_NUMBER,
                {"data_year": 2099, "BW01": 2000},
                year=2023,
            )

        conn = sqlite3.connect(self.db_path)
        try:
            years = conn.execute(
                """
                SELECT data_year
                FROM bridges
                WHERE state_code = ? AND bridge_number = ?
                ORDER BY data_year
                """,
                (STATE_CODE, BRIDGE_NUMBER),
            ).fetchall()
        finally:
            conn.close()
        assert years == [(2023,), (2024,)]

    def test_export_preserves_invalid_direct_value_for_review(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                UPDATE bridges
                SET channel_condition_rating = 'N'
                WHERE state_code = ? AND bridge_number = ? AND data_year = 2023
                """,
                (STATE_CODE, BRIDGE_NUMBER),
            )
            conn.commit()
        finally:
            conn.close()

        exported = export_all_bridges_snbi(self.db_path, STATE_CODE, year=2023)
        bridge = next(row for row in exported if row["BID01"] == BRIDGE_NUMBER)
        assert bridge["BC09"] == "N"

        validation = validate_snbi_payload([bridge], submission_year=2026)
        bc09_issues = validation["by_bridge"][BRIDGE_NUMBER]["by_tag"]["BC09"]
        assert (
            any(issue["rule"] == "BC09-waterway-range" for issue in bc09_issues)
        )


class TestDirectTransitionMapping:
    def test_bw01_and_bc09_preserve_valid_nbi_values(self):
        mapped = map_bridge_to_snbi(
            {
                "STATE_CODE_001": "34",
                "STRUCTURE_NUMBER_008": BRIDGE_NUMBER,
                "YEAR_BUILT_027": "1977",
                "CHANNEL_COND_061": "6",
            },
            [],
        )

        assert mapped["BW01"] == 1977
        assert mapped["BC09"] == "6"

    @pytest.mark.parametrize("invalid_year", [None, "", "not-a-year"])
    def test_blank_or_invalid_item_27_does_not_become_year_zero(
        self, invalid_year
    ):
        mapped = map_bridge_to_snbi(
            {
                "STATE_CODE_001": "34",
                "STRUCTURE_NUMBER_008": BRIDGE_NUMBER,
                "YEAR_BUILT_027": invalid_year,
            },
            [],
        )
        assert "BW01" not in mapped
