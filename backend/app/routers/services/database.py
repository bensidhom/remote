# backend/app/routers/services/database.py

import sqlite3
import pandas as pd
import json
import os
import re
from datetime import datetime
from typing import List, Dict, Any, Optional
from pathlib import Path

from .constants import mapping, BRIDGE_EXCLUDE_KEYS, STATE_MAP, REVERSE_MAPPING
from .editor_metadata import build_editor_metadata
from .snbi_schema import NESTED_ARRAY_SPECS
from .snbi_rules import CODE_LISTS
from .utils import safe_insert, USER_DB, SAVE_DIR, resource_path, init_db_if_needed


def _extract_year_from_value(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        s = str(value).strip()
        if not s:
            return None
        if len(s) >= 4 and s[:4].isdigit():
            y = int(s[:4])
            if 1900 <= y <= 2100:
                return y
        if len(s) >= 4 and s[-4:].isdigit():
            y = int(s[-4:])
            if 1900 <= y <= 2100:
                return y
        if '/' in s:
            parts = s.split('/')
            for p in parts:
                if len(p) == 4 and p.isdigit():
                    y = int(p)
                    if 1900 <= y <= 2100:
                        return y
        m = re.search(r'(19\d{2}|20\d{2}|2100)', s)
        if m:
            y = int(m.group(1))
            if 1900 <= y <= 2100:
                return y
    except Exception:
        return None
    return None


def _to_float_or_none(value: Any) -> Optional[float]:
    try:
        if value is None or value == '':
            return None
        return float(value)
    except Exception:
        return None


def _extract_snbi_snapshot_year(bridge_data: Dict[str, Any]) -> Optional[int]:
    # Preferred source from mapped Date of Inspection, then inspection rows.
    for candidate in (
        bridge_data.get('BLR03'),
        bridge_data.get('BPS02'),
    ):
        y = _extract_year_from_value(candidate)
        if y is not None:
            return y

    for insp in (bridge_data.get('Inspections') or []):
        if not isinstance(insp, dict):
            continue
        for k in ('BIE02', 'BIE01'):
            y = _extract_year_from_value(insp.get(k))
            if y is not None:
                return y

    return None


def _migrate_add_year_support(conn: sqlite3.Connection, cursor: sqlite3.Cursor):
    """
    Migrate existing databases to the multi-year schema.

    - bridges: adds ``data_year INTEGER`` column + changes UNIQUE constraint to
      ``(state_code, bridge_number, data_year)`` by doing a full table rebuild.
    - features: adds ``data_year INTEGER`` column via ALTER TABLE (no constraint change).
    """
    current_year = datetime.now().year

    # ── bridges ──────────────────────────────────────────────────────────────
    cursor.execute("PRAGMA table_info(bridges)")
    bridge_col_names = [row[1] for row in cursor.fetchall()]

    if 'data_year' not in bridge_col_names:
        print("[DB migration] Adding data_year to bridges table …")
        # Turn off FK enforcement during rebuild
        conn.execute("PRAGMA foreign_keys = OFF")

        # 1. Rename old table
        cursor.execute("ALTER TABLE bridges RENAME TO _bridges_migration_bak")

        # 2. Create the new bridges table (schema already reflects the updated DDL)
        cursor.execute('''
            CREATE TABLE bridges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                state_code INTEGER NOT NULL,
                bridge_number TEXT NOT NULL,
                bridge_name TEXT,
                previous_bridge_number TEXT,
                county_code INTEGER,
                place_code INTEGER,
                highway_agency_district TEXT,
                latitude REAL,
                longitude REAL,
                border_bridge_number TEXT,
                border_bridge_state_or_country_code TEXT,
                border_bridge_inspection_responsibility TEXT,
                border_bridge_designated_lead_state TEXT,
                bridge_location TEXT,
                metropolitan_planning_organization TEXT,
                owner TEXT,
                maintenance_responsibility TEXT,
                federal_or_tribal_land_access TEXT,
                historical_significance TEXT,
                toll TEXT,
                emergency_evacuation_designation TEXT,
                bridge_railings TEXT,
                transitions TEXT,
                nbis_bridge_length REAL,
                total_bridge_length REAL,
                maximum_span_length REAL,
                minimum_span_length REAL,
                bridge_width_out_to_out REAL,
                bridge_width_curb_to_curb REAL,
                left_curb_or_sidewalk_width REAL,
                right_curb_or_sidewalk_width REAL,
                approach_roadway_width REAL,
                bridge_median TEXT,
                skew REAL,
                curved_bridge TEXT,
                maximum_bridge_height REAL,
                sidehill_bridge TEXT,
                irregular_deck_area TEXT,
                calculated_deck_area REAL,
                designed_load TEXT,
                designed_method TEXT,
                load_rating_date TEXT,
                load_rating_method TEXT,
                inventory_load_rating_factor REAL,
                operating_load_rating_factor REAL,
                controlling_legal_load_rating_factor REAL,
                routine_permit_loads TEXT,
                nstm_inspection_required TEXT,
                fatigue_details TEXT,
                underwater_inspection_required TEXT,
                complex_feature TEXT,
                deck_condition_rating REAL,
                superstructure_condition_rating REAL,
                substructure_condition_rating REAL,
                culvert_condition_rating REAL,
                bridge_railings_condition_rating REAL,
                bridge_railings_transitions_condition_rating REAL,
                bridge_bearings_condition_rating REAL,
                bridge_joints_condition_rating REAL,
                channel_condition_rating REAL,
                channel_protection_condition_rating REAL,
                scour_condition_rating REAL,
                bridge_condition_classification TEXT,
                lowest_condition_rating_code REAL,
                nstm_inspection_condition TEXT,
                underwater_inspection_condition TEXT,
                approach_roadway_alignment TEXT,
                overtopping_likelihood TEXT,
                scour_vulnerability TEXT,
                scour_plan_of_action TEXT,
                seismic_vulnerability TEXT,
                year_built INTEGER,
                element_number TEXT,
                element_parent_number TEXT,
                element_total_quantity REAL,
                element_quantity_condition_state_one REAL,
                element_quantity_condition_state_two REAL,
                element_quantity_condition_state_three REAL,
                element_quantity_condition_state_four REAL,
                functional_classification TEXT,
                urban_code TEXT,
                nhs_designation TEXT,
                national_highway_freight_network TEXT,
                strahnet_designation TEXT,
                lrs_route_id TEXT,
                lrs_mile_point REAL,
                lanes_on_highway REAL,
                annual_average_daily_traffic REAL,
                annual_average_daily_truck_traffic REAL,
                year_of_annual_average_daily_traffic INTEGER,
                highway_max_usable_vertical_clearance REAL,
                highway_min_vertical_clearance REAL,
                highway_min_horizontal_clearance_left REAL,
                highway_min_horizontal_clearance_right REAL,
                highway_max_usable_surface_width REAL,
                bypass_detour_length REAL,
                crossing_bridge_number TEXT,
                railroad_service_type TEXT,
                railroad_min_vertical_clearance REAL,
                railroad_min_horizontal_offset REAL,
                navigable_waterway TEXT,
                navigable_min_vertical_clearance REAL,
                movable_bridge_max_navigation_vertical_clearance REAL,
                navigation_channel_width REAL,
                navigation_channel_min_horizontal_clearance REAL,
                substructure_navigation_protection TEXT,
                route_designation TEXT,
                route_number TEXT,
                route_direction TEXT,
                route_type TEXT,
                service_type TEXT,
                inspection_type TEXT,
                inspection_begin_date TEXT,
                inspection_completion_date TEXT,
                nationally_certified_bridge_inspector TEXT,
                inspection_interval INTEGER,
                inspection_due_date TEXT,
                risk_based_inspection_interval_method TEXT,
                inspection_quality_control_date TEXT,
                inspection_quality_assurance_date TEXT,
                inspection_data_update_date TEXT,
                inspection_note TEXT,
                inspection_equipment TEXT,
                legal_load_configuration TEXT,
                legal_load_rating_factor REAL,
                posting_type TEXT,
                posting_value TEXT,
                posting_status_change_date TEXT,
                load_posting_status TEXT,
                span_configuration_designation TEXT,
                number_of_spans INTEGER,
                number_of_beam_lines INTEGER,
                span_material TEXT,
                span_continuity TEXT,
                span_type TEXT,
                span_protective_system TEXT,
                deck_interaction TEXT,
                deck_material_and_type TEXT,
                wearing_surface TEXT,
                deck_protective_system TEXT,
                deck_reinforcing_protective_system TEXT,
                deck_stay_in_place_forms TEXT,
                substructure_configuration_designation TEXT,
                substructure_material TEXT,
                substructure_type TEXT,
                substructure_protective_system TEXT,
                foundation_type TEXT,
                foundation_protective_system TEXT,
                year_work_performed INTEGER,
                work_performed TEXT,
                data_year INTEGER,
                UNIQUE(state_code, bridge_number, data_year)
            )
        ''')

        # 3. Copy old data; derive data_year from inspection_completion_date
        old_col_list = ', '.join(bridge_col_names)
        year_expr = (
            f"CASE "
            f"WHEN inspection_completion_date IS NOT NULL "
            f"  AND CAST(SUBSTR(inspection_completion_date, 1, 4) AS INTEGER) BETWEEN 1900 AND 2100 "
            f"THEN CAST(SUBSTR(inspection_completion_date, 1, 4) AS INTEGER) "
            f"ELSE {current_year} "
            f"END"
        )
        cursor.execute(
            f"INSERT INTO bridges ({old_col_list}, data_year) "
            f"SELECT {old_col_list}, {year_expr} FROM _bridges_migration_bak"
        )

        # 4. Drop backup and restore FK enforcement
        cursor.execute("DROP TABLE _bridges_migration_bak")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()
        print(f"[DB migration] bridges table migrated ({cursor.rowcount} rows).")

    # ── features ─────────────────────────────────────────────────────────────
    cursor.execute("PRAGMA table_info(features)")
    feature_col_names = [row[1] for row in cursor.fetchall()]
    if 'data_year' not in feature_col_names:
        cursor.execute("ALTER TABLE features ADD COLUMN data_year INTEGER")
        conn.commit()
        print("[DB migration] data_year column added to features table.")

    # ── child SNBI tables ─────────────────────────────────────────────────────
    # A single SQLite DB stores one row set per selected inventory year. Child
    # tables need the same snapshot key so elements/routes/spans/works can be
    # loaded independently for 2024, 2025, etc. without creating duplicate rows.
    for table_name in ("routes", "span_sets", "elements", "works"):
        cursor.execute(f"PRAGMA table_info({table_name})")
        col_names = [row[1] for row in cursor.fetchall()]
        if "data_year" not in col_names:
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN data_year INTEGER")
            cursor.execute(
                f"UPDATE {table_name} SET data_year = ? WHERE data_year IS NULL",
                (current_year,),
            )
            conn.commit()
            print(f"[DB migration] data_year column added to {table_name} table.")

    # ── bridges.fhwa_omit ────────────────────────────────────────────────────
    # FHWA's SNBI crosswalk says bridges with Item 42A in {2, 3, 9, 0, null}
    # are not highway bridges and should be omitted from the transition. The
    # app keeps them but marks them so the UI can color/annotate them.
    cursor.execute("PRAGMA table_info(bridges)")
    bridge_col_names = [row[1] for row in cursor.fetchall()]
    if "fhwa_omit" not in bridge_col_names:
        cursor.execute("ALTER TABLE bridges ADD COLUMN fhwa_omit INTEGER DEFAULT 0")
        try:
            # Backfill from the stored original NBI rows so bridges converted
            # before this column existed are flagged without a re-conversion.
            cursor.execute(
                """
                UPDATE bridges SET fhwa_omit = 1
                WHERE EXISTS (
                    SELECT 1 FROM bridge_source_records s
                    WHERE s.state_code = bridges.state_code
                      AND s.bridge_number = bridges.bridge_number
                      AND s.data_year = bridges.data_year
                      AND s.source_name = 'NBI TXT'
                      AND TRIM(COALESCE(json_extract(s.source_payload_json, '$.SERVICE_ON_042A'), ''))
                          IN ('', '0', '2', '3', '9')
                )
                """
            )
        except sqlite3.Error as e:
            print(f"[DB migration] fhwa_omit backfill skipped: {e}")
        conn.commit()
        print("[DB migration] fhwa_omit column added to bridges table.")


def init_db(db_path: str = USER_DB):
    """Initialize database - create tables and copy initial DB if needed."""
    init_db_if_needed()  # This is from utils.py

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    cursor = conn.cursor()

    # Create tables if they don't exist
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bridges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            bridge_name TEXT,
            previous_bridge_number TEXT,
            county_code INTEGER,
            place_code INTEGER,
            highway_agency_district TEXT,
            latitude REAL,
            longitude REAL,
            border_bridge_number TEXT,
            border_bridge_state_or_country_code TEXT,
            border_bridge_inspection_responsibility TEXT,
            border_bridge_designated_lead_state TEXT,
            bridge_location TEXT,
            metropolitan_planning_organization TEXT,
            owner TEXT,
            maintenance_responsibility TEXT,
            federal_or_tribal_land_access TEXT,
            historical_significance TEXT,
            toll TEXT,
            emergency_evacuation_designation TEXT,
            bridge_railings TEXT,
            transitions TEXT,
            nbis_bridge_length REAL,
            total_bridge_length REAL,
            maximum_span_length REAL,
            minimum_span_length REAL,
            bridge_width_out_to_out REAL,
            bridge_width_curb_to_curb REAL,
            left_curb_or_sidewalk_width REAL,
            right_curb_or_sidewalk_width REAL,
            approach_roadway_width REAL,
            bridge_median TEXT,
            skew REAL,
            curved_bridge TEXT,
            maximum_bridge_height REAL,
            sidehill_bridge TEXT,
            irregular_deck_area TEXT,
            calculated_deck_area REAL,
            designed_load TEXT,
            designed_method TEXT,
            load_rating_date TEXT,
            load_rating_method TEXT,
            inventory_load_rating_factor REAL,
            operating_load_rating_factor REAL,
            controlling_legal_load_rating_factor REAL,
            routine_permit_loads TEXT,
            nstm_inspection_required TEXT,
            fatigue_details TEXT,
            underwater_inspection_required TEXT,
            complex_feature TEXT,
            deck_condition_rating REAL,
            superstructure_condition_rating REAL,
            substructure_condition_rating REAL,
            culvert_condition_rating REAL,
            bridge_railings_condition_rating REAL,
            bridge_railings_transitions_condition_rating REAL,
            bridge_bearings_condition_rating REAL,
            bridge_joints_condition_rating REAL,
            channel_condition_rating REAL,
            channel_protection_condition_rating REAL,
            scour_condition_rating REAL,
            bridge_condition_classification TEXT,
            lowest_condition_rating_code REAL,
            nstm_inspection_condition TEXT,
            underwater_inspection_condition TEXT,
            approach_roadway_alignment TEXT,
            overtopping_likelihood TEXT,
            scour_vulnerability TEXT,
            scour_plan_of_action TEXT,
            seismic_vulnerability TEXT,
            year_built INTEGER,
            element_number TEXT,
            element_parent_number TEXT,
            element_total_quantity REAL,
            element_quantity_condition_state_one REAL,
            element_quantity_condition_state_two REAL,
            element_quantity_condition_state_three REAL,
            element_quantity_condition_state_four REAL,
            functional_classification TEXT,
            urban_code TEXT,
            nhs_designation TEXT,
            national_highway_freight_network TEXT,
            strahnet_designation TEXT,
            lrs_route_id TEXT,
            lrs_mile_point REAL,
            lanes_on_highway REAL,
            annual_average_daily_traffic REAL,
            annual_average_daily_truck_traffic REAL,
            year_of_annual_average_daily_traffic INTEGER,
            highway_max_usable_vertical_clearance REAL,
            highway_min_vertical_clearance REAL,
            highway_min_horizontal_clearance_left REAL,
            highway_min_horizontal_clearance_right REAL,
            highway_max_usable_surface_width REAL,
            bypass_detour_length REAL,
            crossing_bridge_number TEXT,
            railroad_service_type TEXT,
            railroad_min_vertical_clearance REAL,
            railroad_min_horizontal_offset REAL,
            navigable_waterway TEXT,
            navigable_min_vertical_clearance REAL,
            movable_bridge_max_navigation_vertical_clearance REAL,
            navigation_channel_width REAL,
            navigation_channel_min_horizontal_clearance REAL,
            substructure_navigation_protection TEXT,
            route_designation TEXT,
            route_number TEXT,
            route_direction TEXT,
            route_type TEXT,
            service_type TEXT,
            inspection_type TEXT,
            inspection_begin_date TEXT,
            inspection_completion_date TEXT,
            nationally_certified_bridge_inspector TEXT,
            inspection_interval INTEGER,
            inspection_due_date TEXT,
            risk_based_inspection_interval_method TEXT,
            inspection_quality_control_date TEXT,
            inspection_quality_assurance_date TEXT,
            inspection_data_update_date TEXT,
            inspection_note TEXT,
            inspection_equipment TEXT,
            legal_load_configuration TEXT,
            legal_load_rating_factor REAL,
            posting_type TEXT,
            posting_value TEXT,
            posting_status_change_date TEXT,
            load_posting_status TEXT,
            span_configuration_designation TEXT,
            number_of_spans INTEGER,
            number_of_beam_lines INTEGER,
            span_material TEXT,
            span_continuity TEXT,
            span_type TEXT,
            span_protective_system TEXT,
            deck_interaction TEXT,
            deck_material_and_type TEXT,
            wearing_surface TEXT,
            deck_protective_system TEXT,
            deck_reinforcing_protective_system TEXT,
            deck_stay_in_place_forms TEXT,
            substructure_configuration_designation TEXT,
            substructure_material TEXT,
            substructure_type TEXT,
            substructure_protective_system TEXT,
            foundation_type TEXT,
            foundation_protective_system TEXT,
            year_work_performed INTEGER,
            work_performed TEXT,
            fhwa_omit INTEGER DEFAULT 0,
            data_year INTEGER,
            UNIQUE(state_code, bridge_number, data_year)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS features (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            feature_type TEXT,
            feature_location TEXT,
            feature_name TEXT,
            functional_classification TEXT,
            urban_code TEXT,
            nhs_designation TEXT,
            national_highway_freight_network TEXT,
            strahnet_designation TEXT,
            lrs_route_id TEXT,
            lrs_mile_point REAL,
            lanes_on_highway REAL,
            annual_average_daily_traffic REAL,
            annual_average_daily_truck_traffic REAL,
            year_of_annual_average_daily_traffic INTEGER,
            highway_max_usable_vertical_clearance REAL,
            highway_min_vertical_clearance REAL,
            highway_min_horizontal_clearance_left REAL,
            highway_min_horizontal_clearance_right REAL,
            highway_max_usable_surface_width REAL,
            bypass_detour_length REAL,
            crossing_bridge_number TEXT,
            data_year INTEGER,
            FOREIGN KEY (state_code, bridge_number, data_year) REFERENCES bridges (state_code, bridge_number, data_year)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            feature_type TEXT,
            route_designation TEXT,
            route_number TEXT,
            route_direction TEXT,
            route_type TEXT,
            service_type TEXT,
            data_year INTEGER,
            FOREIGN KEY (state_code, bridge_number, data_year) REFERENCES bridges (state_code, bridge_number, data_year)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS span_sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            span_configuration_designation TEXT,
            number_of_spans INTEGER,
            number_of_beam_lines INTEGER,
            span_material TEXT,
            span_continuity TEXT,
            span_type TEXT,
            span_protective_system TEXT,
            deck_interaction TEXT,
            deck_material_and_type TEXT,
            wearing_surface TEXT,
            deck_protective_system TEXT,
            deck_reinforcing_protective_system TEXT,
            deck_stay_in_place_forms TEXT,
            data_year INTEGER,
            FOREIGN KEY (state_code, bridge_number, data_year) REFERENCES bridges (state_code, bridge_number, data_year)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS elements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            element_number TEXT,
            element_parent_number TEXT,
            element_total_quantity REAL,
            element_quantity_condition_state_one REAL,
            element_quantity_condition_state_two REAL,
            element_quantity_condition_state_three REAL,
            element_quantity_condition_state_four REAL,
            data_year INTEGER,
            FOREIGN KEY (state_code, bridge_number, data_year) REFERENCES bridges (state_code, bridge_number, data_year)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS works (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            year_work_performed INTEGER,
            work_performed TEXT,
            data_year INTEGER,
            FOREIGN KEY (state_code, bridge_number, data_year) REFERENCES bridges (state_code, bridge_number, data_year)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS substructure_sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            substructure_configuration_designation TEXT,
            number_of_substructure_units INTEGER,
            substructure_material TEXT,
            substructure_type TEXT,
            substructure_protective_system TEXT,
            foundation_type TEXT,
            foundation_protective_system TEXT,
            data_year INTEGER,
            FOREIGN KEY (state_code, bridge_number, data_year) REFERENCES bridges (state_code, bridge_number, data_year)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS inspections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            inspection_type TEXT,
            inspection_begin_date TEXT,
            inspection_completion_date TEXT,
            nationally_certified_bridge_inspector TEXT,
            inspection_interval INTEGER,
            inspection_due_date TEXT,
            risk_based_inspection_interval_method TEXT,
            inspection_quality_control_date TEXT,
            inspection_quality_assurance_date TEXT,
            inspection_data_update_date TEXT,
            inspection_note TEXT,
            inspection_equipment TEXT,
            data_year INTEGER,
            FOREIGN KEY (state_code, bridge_number, data_year) REFERENCES bridges (state_code, bridge_number, data_year)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS posting_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            legal_load_configuration TEXT,
            legal_load_rating_factor REAL,
            posting_type TEXT,
            posting_value TEXT,
            data_year INTEGER,
            FOREIGN KEY (state_code, bridge_number, data_year) REFERENCES bridges (state_code, bridge_number, data_year)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS posting_statuses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            load_posting_status TEXT,
            posting_status_change_date TEXT,
            data_year INTEGER,
            FOREIGN KEY (state_code, bridge_number, data_year) REFERENCES bridges (state_code, bridge_number, data_year)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS inspection_report_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            state_code INTEGER,
            bridge_number TEXT,
            bridge_name TEXT,
            report_folder TEXT,
            report_file TEXT,
            report_year INTEGER,
            inspection_date TEXT,
            deck_condition_rating REAL,
            superstructure_condition_rating REAL,
            substructure_condition_rating REAL,
            culvert_condition_rating REAL,
            lowest_condition_rating_code REAL,
            annual_average_daily_traffic REAL,
            owner TEXT,
            confidence REAL,
            extracted_payload_json TEXT,
            extracted_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bridge_field_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            data_year INTEGER,
            table_name TEXT NOT NULL,
            row_index INTEGER,
            tag TEXT NOT NULL,
            source_name TEXT,
            source_value TEXT,
            note TEXT,
            is_temporary INTEGER DEFAULT 0,
            is_default INTEGER DEFAULT 0
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bridge_source_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            data_year INTEGER,
            source_name TEXT NOT NULL,
            source_payload_json TEXT NOT NULL,
            UNIQUE(state_code, bridge_number, data_year, source_name)
        )
    ''')

    for table_name in (
        "features", "routes", "span_sets", "substructure_sets", "elements",
        "inspections", "posting_evaluations", "posting_statuses", "works",
        "bridge_field_metadata",
        "bridge_source_records",
    ):
        cursor.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table_name}_bridge_year "
            f"ON {table_name}(state_code, bridge_number, data_year)"
        )

    _migrate_add_year_support(conn, cursor)

    conn.commit()
    conn.close()
    print(f"Database initialized at: {db_path}")


def ensure_schema(db_path: str = USER_DB):
    """Create any tables missing from an existing user database."""
    init_db(db_path)


def get_connection(db_path: str = USER_DB):
    """Get database connection."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def save_inspection_report_extractions(db_path: str, rows: List[Dict[str, Any]]) -> int:
    """Persist extracted inspection report rows for traceable history and timeline usage."""
    if not rows:
        return 0

    conn = get_connection(db_path)
    cur = conn.cursor()
    inserted = 0
    try:
        for row in rows:
            payload_json = json.dumps(row.get('schema_fields', {}), ensure_ascii=True)
            cur.execute(
                """
                INSERT INTO inspection_report_history (
                    state_code, bridge_number, bridge_name, report_folder, report_file,
                    report_year, inspection_date,
                    deck_condition_rating, superstructure_condition_rating, substructure_condition_rating,
                    culvert_condition_rating, lowest_condition_rating_code,
                    annual_average_daily_traffic, owner,
                    confidence, extracted_payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get('state_code'),
                    row.get('bridge_number'),
                    row.get('bridge_name'),
                    row.get('report_folder'),
                    row.get('report_file'),
                    row.get('report_year'),
                    row.get('inspection_date'),
                    row.get('deck_condition_rating'),
                    row.get('superstructure_condition_rating'),
                    row.get('substructure_condition_rating'),
                    row.get('culvert_condition_rating'),
                    row.get('lowest_condition_rating_code'),
                    row.get('annual_average_daily_traffic'),
                    row.get('owner'),
                    row.get('confidence'),
                    payload_json,
                ),
            )
            inserted += 1
        conn.commit()
        return inserted
    finally:
        conn.close()


def get_inspection_report_extractions(db_path: str, limit: int = 100) -> List[Dict[str, Any]]:
    """Return recent extracted inspection report rows for UI preview/history."""
    conn = get_connection(db_path)
    try:
        df = pd.read_sql_query(
            """
            SELECT id, state_code, bridge_number, bridge_name, report_folder, report_file,
                   report_year, inspection_date, confidence, extracted_at,
                   lowest_condition_rating_code, deck_condition_rating,
                   superstructure_condition_rating, substructure_condition_rating,
                   culvert_condition_rating
            FROM inspection_report_history
            ORDER BY extracted_at DESC, id DESC
            LIMIT ?
            """,
            conn,
            params=(max(1, min(limit, 1000)),),
        )
        df = df.where(pd.notna(df), None)
        return df.to_dict(orient='records')
    finally:
        conn.close()


def validate_inspection_report_records(db_path: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Validate extracted report rows before applying to DB/history."""
    conn = get_connection(db_path)
    cur = conn.cursor()
    results: List[Dict[str, Any]] = []
    summary = {
        'total': len(rows),
        'valid': 0,
        'bridge_exists': 0,
        'recommended_snapshot_updates': 0,
        'history_only': 0,
        'rejected': 0,
    }

    try:
        for row in rows:
            state_code = row.get('state_code')
            bridge_number = row.get('bridge_number')
            report_year = row.get('report_year')
            inspection_date = row.get('inspection_date')

            has_identity = state_code is not None and bridge_number not in (None, '')
            condition_values = [
                row.get('deck_condition_rating'),
                row.get('superstructure_condition_rating'),
                row.get('substructure_condition_rating'),
                row.get('culvert_condition_rating'),
                row.get('lowest_condition_rating_code'),
            ]
            condition_count = sum(1 for v in condition_values if v not in (None, '', []))

            existing_bridge = None
            current_snapshot_year = None
            if has_identity:
                cur.execute(
                    """
                    SELECT bridge_name, inspection_completion_date
                    FROM bridges
                    WHERE state_code=? AND bridge_number=?
                    """,
                    (state_code, bridge_number),
                )
                existing_bridge = cur.fetchone()
                if existing_bridge and existing_bridge[1]:
                    current_snapshot_year = _extract_year_from_value(existing_bridge[1])

            report_year_value = _extract_year_from_value(report_year) or _extract_year_from_value(inspection_date)

            reasons: List[str] = []
            action = 'reject'
            is_valid = False

            if not has_identity:
                reasons.append('Missing bridge identity fields (state_code and bridge_number).')
            elif existing_bridge is None:
                reasons.append('Bridge not found in current bridges table; keep as historical-only record.')
                action = 'history_only'
                is_valid = True
            elif condition_count == 0:
                reasons.append('No condition ratings found; keep as historical-only record.')
                action = 'history_only'
                is_valid = True
            elif report_year_value is not None and current_snapshot_year is not None and report_year_value < current_snapshot_year:
                reasons.append('Report year is older than current snapshot; keep as historical-only record.')
                action = 'history_only'
                is_valid = True
            else:
                action = 'history_and_snapshot'
                is_valid = True

            if is_valid:
                summary['valid'] += 1
            else:
                summary['rejected'] += 1

            if existing_bridge is not None:
                summary['bridge_exists'] += 1

            if action == 'history_and_snapshot':
                summary['recommended_snapshot_updates'] += 1
            elif action == 'history_only':
                summary['history_only'] += 1

            results.append({
                'report_file': row.get('report_file'),
                'report_folder': row.get('report_folder'),
                'state_code': state_code,
                'bridge_number': bridge_number,
                'bridge_name': row.get('bridge_name') or (existing_bridge[0] if existing_bridge else None),
                'report_year': report_year_value,
                'inspection_date': inspection_date,
                'confidence': row.get('confidence'),
                'condition_fields_found': condition_count,
                'bridge_exists': existing_bridge is not None,
                'current_snapshot_year': current_snapshot_year,
                'recommended_action': action,
                'is_valid': is_valid,
                'reasons': reasons,
                'payload': row,
            })

        return {'summary': summary, 'rows': results}
    finally:
        conn.close()


def apply_validated_inspection_report_records(
    db_path: str,
    rows: List[Dict[str, Any]],
    update_bridge_snapshot: bool = False,
) -> Dict[str, Any]:
    """Apply validated report rows to historical table and optionally current bridge snapshot."""
    validation = validate_inspection_report_records(db_path, rows)
    validated_rows = validation['rows']

    history_candidates = [r['payload'] for r in validated_rows if r.get('is_valid')]
    history_saved = save_inspection_report_extractions(db_path, history_candidates) if history_candidates else 0

    bridge_updates = 0
    if update_bridge_snapshot:
        conn = get_connection(db_path)
        cur = conn.cursor()
        try:
            for row in validated_rows:
                if row.get('recommended_action') != 'history_and_snapshot':
                    continue

                payload = row.get('payload') or {}
                state_code = payload.get('state_code')
                bridge_number = payload.get('bridge_number')
                if state_code is None or not bridge_number:
                    continue

                update_fields = {
                    'deck_condition_rating': payload.get('deck_condition_rating'),
                    'superstructure_condition_rating': payload.get('superstructure_condition_rating'),
                    'substructure_condition_rating': payload.get('substructure_condition_rating'),
                    'culvert_condition_rating': payload.get('culvert_condition_rating'),
                    'lowest_condition_rating_code': payload.get('lowest_condition_rating_code'),
                    'owner': payload.get('owner'),
                    'annual_average_daily_traffic': payload.get('annual_average_daily_traffic'),
                    'inspection_completion_date': payload.get('inspection_date'),
                }
                set_parts = [f"{k}=?" for k, v in update_fields.items() if v not in (None, '')]
                if not set_parts:
                    continue
                values = [v for v in update_fields.values() if v not in (None, '')]
                values.extend([state_code, bridge_number])

                cur.execute(
                    f"UPDATE bridges SET {', '.join(set_parts)} WHERE state_code=? AND bridge_number=?",
                    values,
                )
                if cur.rowcount > 0:
                    bridge_updates += 1

            conn.commit()
        finally:
            conn.close()

    return {
        'history_saved': history_saved,
        'bridge_snapshots_updated': bridge_updates,
        'validation_summary': validation['summary'],
    }


# ====================== LOADER ======================
def load_json_to_db(db_path: str, data: List[Dict], data_year_override: Optional[int] = None) -> tuple:
    """Load SNBI JSON data into SQLite database.

    Returns a 4-tuple: (inserted_new, already_existed, skipped, skipped_reasons)
    - inserted_new:    rows actually written to the database for the first time.
    - already_existed: rows whose (state_code, bridge_number) already existed
                       (silently ignored by INSERT OR IGNORE).
    - skipped:         rows that could not be processed at all (missing keys,
                       mapping errors, etc.).
    - skipped_reasons: human-readable explanation for each skipped row (capped at
                       50) so the loader UI can show exactly why a bridge dropped.
    """
    # The DB file can be deleted while the app is running. A fresh sqlite
    # connection would recreate an empty file, so ensure the schema exists here.
    init_db(db_path)
    conn = get_connection(db_path)
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA temp_store = MEMORY;")
    cursor = conn.cursor()
    table_columns_cache: Dict[str, set] = {}

    def _filter_to_table_columns(table_name: str, row: Dict[str, Any]) -> Dict[str, Any]:
        if table_name not in table_columns_cache:
            cursor.execute(f"PRAGMA table_info({table_name})")
            table_columns_cache[table_name] = {col[1] for col in cursor.fetchall()}
        allowed = table_columns_cache[table_name]
        return {k: v for k, v in row.items() if k in allowed}

    inserted_new = 0
    already_existed = 0
    skipped = 0
    skipped_reasons = []

    try:
        for idx, bridge_data in enumerate(data):
            state_code = bridge_data.get('BL01')
            bridge_number = bridge_data.get('BID01')

            # Use explicit None checks instead of falsy checks (0 is a valid state code!)
            if state_code is None or state_code == '' or bridge_number is None or bridge_number == '':
                skipped += 1
                if len(skipped_reasons) < 50:
                    skipped_reasons.append(
                        f"Record {idx}: missing required identifier — "
                        f"BL01 (state) = {state_code!r}, BID01 (bridge number) = {bridge_number!r}"
                    )
                continue

            # The inventory year selected in the Loader is authoritative. SNBI
            # date fields often contain inspection dates from prior years, so
            # using them as the database snapshot year would mis-file uploads.
            snapshot_year = data_year_override or _extract_snbi_snapshot_year(bridge_data)
            data_year = snapshot_year or datetime.now().year

            # ── 1. Main bridge table ─────────────────────────────────────
            bridge_insert = {}
            for key, value in bridge_data.items():
                if key in mapping:
                    col = mapping[key]
                    # You may want to dynamically get columns, but for speed we insert what matches
                    bridge_insert[col] = value

            bridge_insert['state_code'] = state_code
            bridge_insert['bridge_number'] = bridge_number
            bridge_insert['data_year'] = data_year

            # FHWA crosswalk: Item 42A in {2, 3, 9, 0, null} marks a
            # non-highway bridge that FHWA says to omit from the transition.
            # Keep the record but flag it for the UI.
            _src = bridge_data.get('_SourceData')
            _nbi_src = _src.get('nbi') if isinstance(_src, dict) else None
            if isinstance(_nbi_src, dict):
                _service_on = str(_nbi_src.get('SERVICE_ON_042A') or '').strip()
                bridge_insert['fhwa_omit'] = 1 if _service_on in {'', '0', '2', '3', '9'} else 0

            bridge_insert = _filter_to_table_columns('bridges', bridge_insert)

            try:
                safe_insert(cursor, 'bridges', bridge_insert)
                if cursor.rowcount == 1:
                    inserted_new += 1
                else:
                    # Row already exists for this state/bridge/year. Refresh the
                    # complete main bridge snapshot from the incoming JSON so
                    # same-year reloads are deterministic.
                    update_cols = {
                        k: v for k, v in bridge_insert.items()
                        if k not in {'id', 'state_code', 'bridge_number', 'data_year'}
                    }
                    if update_cols:
                        set_clause = ', '.join(f"{k} = ?" for k in update_cols)
                        cursor.execute(
                            f"UPDATE bridges SET {set_clause} "
                            f"WHERE state_code = ? AND bridge_number = ? AND data_year = ?",
                            list(update_cols.values()) + [state_code, bridge_number, data_year],
                        )
                    already_existed += 1
            except Exception as e:
                print(f"Warning: Failed to insert bridge {state_code}/{bridge_number}: {e}")
                skipped += 1
                if len(skipped_reasons) < 50:
                    skipped_reasons.append(
                        f"Record {idx}: bridge {state_code}/{bridge_number} insert failed — {e}"
                    )
                continue

            for child_table in (
                "features", "routes", "span_sets", "substructure_sets",
                "elements", "inspections", "posting_evaluations",
                "posting_statuses", "works",
            ):
                cursor.execute(
                    f"DELETE FROM {child_table} WHERE state_code = ? AND bridge_number = ? AND data_year = ?",
                    (state_code, bridge_number, data_year),
                )

            cursor.execute(
                "DELETE FROM bridge_field_metadata WHERE state_code = ? AND bridge_number = ? AND data_year = ?",
                (state_code, bridge_number, data_year),
            )
            cursor.execute(
                "DELETE FROM bridge_source_records WHERE state_code = ? AND bridge_number = ? AND data_year = ?",
                (state_code, bridge_number, data_year),
            )

            source_data = bridge_data.get('_SourceData') if isinstance(bridge_data.get('_SourceData'), dict) else {}
            nbi_source = source_data.get('nbi') if isinstance(source_data, dict) else None
            if isinstance(nbi_source, dict):
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO bridge_source_records (
                        state_code, bridge_number, data_year, source_name, source_payload_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        state_code,
                        bridge_number,
                        data_year,
                        'NBI TXT',
                        json.dumps(nbi_source, ensure_ascii=True, default=str),
                    ),
                )

            # ── 2. Features + Routes ─────────────────────────────────────
            for feat in bridge_data.get('Features', []):
                feat_insert = {'state_code': state_code, 'bridge_number': bridge_number, 'data_year': data_year}
                for k, v in feat.items():
                    if k in mapping and k != 'Routes':
                        feat_insert[mapping[k]] = v
                feat_insert = _filter_to_table_columns('features', feat_insert)
                safe_insert(cursor, 'features', feat_insert)

                # Routes
                for route in feat.get('Routes', []):
                    route_insert = {
                        'state_code': state_code,
                        'bridge_number': bridge_number,
                        'feature_type': feat.get('BF01'),
                        'data_year': data_year,
                    }
                    for rk, rv in route.items():
                        if rk in mapping:
                            route_insert[mapping[rk]] = rv
                    route_insert = _filter_to_table_columns('routes', route_insert)
                    safe_insert(cursor, 'routes', route_insert)

            # ── 3. Span Sets ─────────────────────────────────────────────
            for span in bridge_data.get('SpanSets', []):
                span_insert = {'state_code': state_code, 'bridge_number': bridge_number, 'data_year': data_year}
                for k, v in span.items():
                    if k in mapping:
                        span_insert[mapping[k]] = v
                span_insert = _filter_to_table_columns('span_sets', span_insert)
                safe_insert(cursor, 'span_sets', span_insert)

            # ── 3b. Substructure Sets ────────────────────────────────────
            for sub in bridge_data.get('SubstructureSets', []):
                sub_insert = {'state_code': state_code, 'bridge_number': bridge_number, 'data_year': data_year}
                for k, v in sub.items():
                    if k in mapping:
                        sub_insert[mapping[k]] = v
                sub_insert = _filter_to_table_columns('substructure_sets', sub_insert)
                safe_insert(cursor, 'substructure_sets', sub_insert)

            # ── 4. Elements ──────────────────────────────────────────────
            for elem in bridge_data.get('Elements', []):
                elem_insert = {'state_code': state_code, 'bridge_number': bridge_number, 'data_year': data_year}
                for k, v in elem.items():
                    if k in mapping:
                        elem_insert[mapping[k]] = v
                if 'element_parent_number' not in elem_insert:
                    elem_insert['element_parent_number'] = '0'
                elem_insert = _filter_to_table_columns('elements', elem_insert)
                safe_insert(cursor, 'elements', elem_insert)

            # ── 5. Works ─────────────────────────────────────────────────
            for work in bridge_data.get('Works', []):
                work_insert = {'state_code': state_code, 'bridge_number': bridge_number, 'data_year': data_year}
                for k, v in work.items():
                    if k in mapping:
                        work_insert[mapping[k]] = v
                work_insert = _filter_to_table_columns('works', work_insert)
                safe_insert(cursor, 'works', work_insert)

            # ── 5b. Inspections / Posting ────────────────────────────────
            for insp in bridge_data.get('Inspections', []):
                insp_insert = {'state_code': state_code, 'bridge_number': bridge_number, 'data_year': data_year}
                for k, v in insp.items():
                    if k in mapping:
                        insp_insert[mapping[k]] = v
                insp_insert = _filter_to_table_columns('inspections', insp_insert)
                safe_insert(cursor, 'inspections', insp_insert)

            for pe in bridge_data.get('PostingEvaluations', []):
                pe_insert = {'state_code': state_code, 'bridge_number': bridge_number, 'data_year': data_year}
                for k, v in pe.items():
                    if k in mapping:
                        pe_insert[mapping[k]] = v
                pe_insert = _filter_to_table_columns('posting_evaluations', pe_insert)
                safe_insert(cursor, 'posting_evaluations', pe_insert)

            for ps in bridge_data.get('PostingStatuses', []):
                ps_insert = {'state_code': state_code, 'bridge_number': bridge_number, 'data_year': data_year}
                for k, v in ps.items():
                    if k in mapping:
                        ps_insert[mapping[k]] = v
                ps_insert = _filter_to_table_columns('posting_statuses', ps_insert)
                safe_insert(cursor, 'posting_statuses', ps_insert)

            # ── 6. Historical snapshot row (for year-over-year KPI evolution) ──
            # snapshot_year and data_year were already extracted at the top of this loop
            deck = _to_float_or_none(bridge_data.get('BC01'))
            superstructure = _to_float_or_none(bridge_data.get('BC02'))
            substructure = _to_float_or_none(bridge_data.get('BC03'))
            culvert = _to_float_or_none(bridge_data.get('BC04'))
            lowest = _to_float_or_none(bridge_data.get('BC13'))

            if lowest is None:
                vals = [v for v in (deck, superstructure, substructure, culvert) if v is not None]
                lowest = min(vals) if vals else None

            if data_year is not None and lowest is not None:
                cursor.execute(
                    """
                    SELECT 1
                    FROM inspection_report_history
                    WHERE state_code=? AND bridge_number=? AND report_year=? AND report_file='JSON_LOAD'
                    LIMIT 1
                    """,
                    (state_code, bridge_number, data_year),
                )
                exists = cursor.fetchone() is not None

                if not exists:
                    cursor.execute(
                        """
                        INSERT INTO inspection_report_history (
                            state_code, bridge_number, bridge_name, report_folder, report_file,
                            report_year, inspection_date,
                            deck_condition_rating, superstructure_condition_rating,
                            substructure_condition_rating, culvert_condition_rating,
                            lowest_condition_rating_code, confidence, extracted_payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            state_code,
                            bridge_number,
                            bridge_data.get('BID02') or bridge_insert.get('bridge_name'),
                            'JSON_IMPORT',
                            'JSON_LOAD',
                            data_year,
                            str(bridge_data.get('BLR03') or ''),
                            deck,
                            superstructure,
                            substructure,
                            culvert,
                            lowest,
                            1.0,
                            json.dumps({'source': 'load_json_to_db', 'data_year': data_year}, ensure_ascii=True),
                        ),
                    )

        conn.commit()
        print(
            f"DB load complete: {inserted_new} newly inserted, "
            f"{already_existed} already existed, {skipped} skipped"
        )
        if skipped_reasons:
            print(f"Skipped reasons: {skipped_reasons}")
        return inserted_new, already_existed, skipped, skipped_reasons

    except Exception as e:
        conn.rollback()
        print(f"Error loading JSON to DB: {e}")
        import traceback
        traceback.print_exc()
        return 0, 0, len(data), [f"Fatal load error: {e}"]
    finally:
        conn.close()


# ====================== EXPLORER & QUERY HELPERS ======================
# User-defined custom tables live OUTSIDE the SNBI schema — never exported/imported
# as SNBI, never year-deleted, and hidden from the generic table explorer.
CUSTOM_TABLE_NAMES = {'custom_tables', 'custom_table_rows'}


def get_all_tables(db_path: str = USER_DB) -> List[str]:
    """Return list of all tables in the database (excluding user custom tables)."""
    ensure_schema(db_path)
    conn = get_connection(db_path)
    df = pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table';", conn)
    conn.close()
    return [t for t in df['name'].tolist() if t not in CUSTOM_TABLE_NAMES]


def get_table_data(db_path: str, table_name: str, state_code: Optional[int] = None, bridge_number: Optional[str] = None, limit: Optional[int] = None, year: Optional[int] = None) -> List[Dict]:
    """Get data from a specific table, optionally filtered by state_code, bridge_number, year and limited."""
    ensure_schema(db_path)
    conn = get_connection(db_path)
    try:
        # table_name is interpolated into SQL below; only accept real tables.
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table_name,),
        ).fetchone()
        if exists is None:
            raise ValueError(f"Unknown table: {table_name}")

        # Build WHERE clause dynamically
        where_conditions = []
        params = []

        # Get column names once
        cols = pd.read_sql_query(f"PRAGMA table_info({table_name})", conn)
        col_names = cols["name"].tolist()

        if state_code is not None and "state_code" in col_names:
            where_conditions.append("state_code = ?")
            params.append(state_code)

        if bridge_number is not None and "bridge_number" in col_names:
            where_conditions.append("bridge_number = ?")
            params.append(bridge_number)

        if year is not None and "data_year" in col_names:
            where_conditions.append("data_year = ?")
            params.append(year)
        elif year is None and table_name == "bridges" and "data_year" in col_names:
            # Default to most recent year to avoid returning duplicate rows across years
            where_conditions.append(
                "data_year = (SELECT MAX(data_year) FROM bridges WHERE data_year IS NOT NULL)"
            )
        
        where_clause = " AND ".join(where_conditions) if where_conditions else ""
        if where_clause:
            where_clause = f"WHERE {where_clause}"
        
        limit_clause = f" LIMIT {limit}" if limit and limit > 0 else ""
        query = f"SELECT * FROM {table_name} {where_clause}{limit_clause}"
        
        df = pd.read_sql_query(query, conn, params=params if params else None)
        
        # Replace NaN with None for JSON serialization (using pandas methods)
        df = df.where(pd.notna(df), None)
        return df.to_dict(orient="records")
    finally:
        conn.close()


def get_bridge_history(db_path: str, state_code: int, bridge_number: str) -> Optional[Dict]:
    """Get historical data for a specific bridge with timeline events and forecast context."""
    conn = get_connection(db_path)
    try:
        def _safe_year(value: Any) -> Optional[int]:
            if value is None:
                return None
            try:
                s = str(value).strip()
                if not s:
                    return None
                if len(s) >= 4 and s[:4].isdigit():
                    y = int(s[:4])
                    if 1900 <= y <= 2100:
                        return y
                if '-' in s:
                    y = int(s.split('-')[0])
                    if 1900 <= y <= 2100:
                        return y
            except Exception:
                return None
            return None

        def _safe_rating(value: Any) -> Optional[float]:
            try:
                if value is None or value == '':
                    return None
                return float(value)
            except Exception:
                return None

        # Get main bridge data (most recent year if multiple are loaded)
        bridge_df = pd.read_sql_query(
            """SELECT * FROM bridges WHERE state_code = ? AND bridge_number = ?
               ORDER BY data_year DESC LIMIT 1""",
            conn, params=(state_code, bridge_number)
        )
        if bridge_df.empty:
            return None

        bridge_data = bridge_df.to_dict(orient="records")[0]
        latest_data_year = bridge_data.get('data_year')

        # Get features
        if latest_data_year is not None:
            features_df = pd.read_sql_query(
                "SELECT * FROM features WHERE state_code = ? AND bridge_number = ? AND data_year = ?",
                conn, params=(state_code, bridge_number, latest_data_year)
            )
        else:
            features_df = pd.read_sql_query(
                "SELECT * FROM features WHERE state_code = ? AND bridge_number = ?",
                conn, params=(state_code, bridge_number)
            )
        bridge_data['features'] = features_df.to_dict(orient="records")

        # Get elements
        if latest_data_year is not None:
            elements_df = pd.read_sql_query(
                "SELECT * FROM elements WHERE state_code = ? AND bridge_number = ? AND data_year = ?",
                conn, params=(state_code, bridge_number, latest_data_year)
            )
        else:
            elements_df = pd.read_sql_query(
                "SELECT * FROM elements WHERE state_code = ? AND bridge_number = ?",
                conn, params=(state_code, bridge_number)
            )
        bridge_data['elements'] = elements_df.to_dict(orient="records")

        current_year = datetime.now().year
        tables_df = pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table'", conn)
        table_names = set(tables_df['name'].tolist())

        events: List[Dict[str, Any]] = []
        observation_candidates: List[Dict[str, Any]] = []

        snapshot_df = pd.read_sql_query(
            """
            SELECT data_year, inspection_completion_date,
                   deck_condition_rating, superstructure_condition_rating,
                   substructure_condition_rating, culvert_condition_rating,
                   lowest_condition_rating_code
            FROM bridges
            WHERE state_code = ? AND bridge_number = ? AND data_year IS NOT NULL
            ORDER BY data_year
            """,
            conn,
            params=(state_code, bridge_number),
        )
        snapshot_ratings_by_year: Dict[int, Dict[str, Optional[float]]] = {}
        for _, r in snapshot_df.iterrows():
            year = _safe_year(r.get('data_year'))
            if year is None:
                continue
            snapshot_ratings_by_year[year] = {
                'deck': _safe_rating(r.get('deck_condition_rating')),
                'superstructure': _safe_rating(r.get('superstructure_condition_rating')),
                'substructure': _safe_rating(r.get('substructure_condition_rating')),
                'culvert': _safe_rating(r.get('culvert_condition_rating')),
                'lowest': _safe_rating(r.get('lowest_condition_rating_code')),
            }
            # NOTE: yearly data snapshots feed the condition chart (below) but are
            # NOT added to the event timeline — the timeline only shows real
            # DB/SNBI events (works, inspections, postings, milestones).
            observation_candidates.append({'year': year, 'source': 'bridges.data_year'})

        # Primary bridge dates
        bridge_date_fields = [
            ('inspection_begin_date', 'Inspection Start', 'inspection'),
            ('inspection_completion_date', 'Inspection Complete', 'inspection'),
            ('inspection_due_date', 'Inspection Due', 'inspection_due'),
            ('inspection_data_update_date', 'Inspection Data Updated', 'inspection'),
            ('load_rating_date', 'Load Rating Update', 'rating'),
            ('posting_status_change_date', 'Posting Status Change', 'posting'),
            ('year_built', 'Bridge Built', 'milestone'),
            ('year_work_performed', 'Work Performed', 'work'),
        ]

        for field, label, etype in bridge_date_fields:
            year = _safe_year(bridge_data.get(field))
            if year is None:
                continue
            events.append({
                'year': year,
                'event_type': etype,
                'label': label,
                'source': 'bridges',
            })
            if etype in {'inspection', 'rating'}:
                observation_candidates.append({'year': year, 'source': 'bridges'})

        # Works timeline
        if 'works' in table_names:
            works_df = pd.read_sql_query(
                "SELECT year_work_performed, work_performed FROM works WHERE state_code=? AND bridge_number=?",
                conn,
                params=(state_code, bridge_number),
            )
            for _, w in works_df.iterrows():
                year = _safe_year(w.get('year_work_performed'))
                if year is None:
                    continue
                events.append({
                    'year': year,
                    'event_type': 'work',
                    'label': f"Work: {w.get('work_performed') or 'Maintenance'}",
                    'source': 'works',
                })

        # Optional inspections child table (if present in user DB)
        if 'inspections' in table_names:
            insp_df = pd.read_sql_query(
                "SELECT * FROM inspections WHERE state_code=? AND bridge_number=?",
                conn,
                params=(state_code, bridge_number),
            )
            for _, r in insp_df.iterrows():
                year = _safe_year(r.get('inspection_completion_date')) or _safe_year(r.get('inspection_begin_date'))
                if year is None:
                    continue
                events.append({
                    'year': year,
                    'event_type': 'inspection',
                    'label': f"Inspection: {r.get('inspection_type') or 'Routine'}",
                    'source': 'inspections',
                })
                observation_candidates.append({'year': year, 'source': 'inspections'})

        # Optional posting_statuses table (if present)
        if 'posting_statuses' in table_names:
            ps_df = pd.read_sql_query(
                "SELECT * FROM posting_statuses WHERE state_code=? AND bridge_number=?",
                conn,
                params=(state_code, bridge_number),
            )
            for _, r in ps_df.iterrows():
                year = _safe_year(r.get('posting_status_change_date'))
                if year is None:
                    continue
                events.append({
                    'year': year,
                    'event_type': 'posting',
                    'label': f"Posting: {r.get('load_posting_status') or 'Status Update'}",
                    'source': 'posting_statuses',
                })

        # Persisted inspection report extractions
        if 'inspection_report_history' in table_names:
            rep_df = pd.read_sql_query(
                """
                SELECT report_year, inspection_date, report_file,
                       deck_condition_rating, superstructure_condition_rating,
                       substructure_condition_rating, culvert_condition_rating,
                       lowest_condition_rating_code
                FROM inspection_report_history
                WHERE state_code = ? AND bridge_number = ?
                ORDER BY COALESCE(report_year, 0), id
                """,
                conn,
                params=(state_code, bridge_number),
            )

            for _, r in rep_df.iterrows():
                year = _safe_year(r.get('report_year')) or _safe_year(r.get('inspection_date'))
                if year is None:
                    continue

                # Report/JSON-load extractions feed the condition chart as a data
                # fallback but are NOT shown as timeline events (the timeline only
                # lists real DB/SNBI events).
                observation_candidates.append({'year': year, 'source': 'inspection_report_history'})

        report_ratings_by_year: Dict[int, Dict[str, Optional[float]]] = {}
        if 'inspection_report_history' in table_names:
            rep_points_df = pd.read_sql_query(
                """
                SELECT report_year, inspection_date,
                       deck_condition_rating, superstructure_condition_rating,
                       substructure_condition_rating
                FROM inspection_report_history
                WHERE state_code = ? AND bridge_number = ?
                ORDER BY id DESC
                """,
                conn,
                params=(state_code, bridge_number),
            )
            for _, r in rep_points_df.iterrows():
                year = _safe_year(r.get('report_year')) or _safe_year(r.get('inspection_date'))
                if year is None:
                    continue
                if year in report_ratings_by_year:
                    continue
                report_ratings_by_year[year] = {
                    'deck': _safe_rating(r.get('deck_condition_rating')),
                    'superstructure': _safe_rating(r.get('superstructure_condition_rating')),
                    'substructure': _safe_rating(r.get('substructure_condition_rating')),
                }

        # Build multi-year historical points from actual loaded snapshots first.
        # Fall back to report history, then derived points only when no real
        # yearly upload exists for that year.
        observed_years = sorted({x['year'] for x in observation_candidates if isinstance(x.get('year'), int)})
        if not observed_years:
            fallback_year = _safe_year(bridge_data.get('inspection_completion_date')) or current_year
            observed_years = [fallback_year]
        if not snapshot_ratings_by_year and current_year not in observed_years:
            observed_years.append(current_year)
        observed_years = sorted(set(observed_years))

        current_deck = _safe_rating(bridge_data.get('deck_condition_rating'))
        current_super = _safe_rating(bridge_data.get('superstructure_condition_rating'))
        current_sub = _safe_rating(bridge_data.get('substructure_condition_rating'))

        def _backcast(current_rating: Optional[float], year: int) -> Optional[float]:
            if current_rating is None:
                return None
            delta = max(0, current_year - year)
            # Backcast gently when only sparse historical condition points exist.
            return round(min(9.0, current_rating + delta * 0.08), 2)

        historical_points: List[Dict[str, Any]] = []
        for y in observed_years:
            snapshot_values = snapshot_ratings_by_year.get(y, {})
            report_values = report_ratings_by_year.get(y, {})
            has_snapshot = y in snapshot_ratings_by_year
            has_report = y in report_ratings_by_year
            historical_points.append({
                'year': y,
                'deck': (
                    snapshot_values.get('deck')
                    if snapshot_values.get('deck') is not None
                    else report_values.get('deck')
                    if report_values.get('deck') is not None
                    else _backcast(current_deck, y)
                ),
                'superstructure': (
                    snapshot_values.get('superstructure')
                    if snapshot_values.get('superstructure') is not None
                    else report_values.get('superstructure')
                    if report_values.get('superstructure') is not None
                    else _backcast(current_super, y)
                ),
                'substructure': (
                    snapshot_values.get('substructure')
                    if snapshot_values.get('substructure') is not None
                    else report_values.get('substructure')
                    if report_values.get('substructure') is not None
                    else _backcast(current_sub, y)
                ),
                'source': 'snapshot' if has_snapshot else ('report' if has_report else 'derived'),
            })

        observation_count = len(set(x['year'] for x in observation_candidates if isinstance(x.get('year'), int)))
        if observation_count >= 4:
            confidence = 'high'
        elif observation_count >= 2:
            confidence = 'medium'
        else:
            confidence = 'low'

        bridge_data['inspection_year'] = observed_years[-1]
        bridge_data['history_events'] = sorted(events, key=lambda e: (e.get('year') or 0, str(e.get('label') or '')))
        bridge_data['historical_points'] = historical_points
        bridge_data['forecast_context'] = {
            'observation_count': observation_count,
            'confidence': confidence,
            'sources': sorted(set(x.get('source') for x in observation_candidates if x.get('source'))),
        }

        # Backward compatibility for existing frontend consumers.
        bridge_data['condition_trend'] = {
            'deck': [p.get('deck') for p in historical_points],
            'superstructure': [p.get('superstructure') for p in historical_points],
            'substructure': [p.get('substructure') for p in historical_points],
            'years': [p.get('year') for p in historical_points],
        }

        return bridge_data
    finally:
        conn.close()


def get_bridges_for_map(
    db_path: str = USER_DB,
    state_code: Optional[int] = None,
    county_code: Optional[int] = None,
    year: Optional[int] = None,
    limit: Optional[int] = None,
) -> List[Dict]:
    """Get bridge locations for GIS map with key inspection data.

    The map should not render every loaded bridge/year snapshot at once. This
    query supports server-side filters and defaults to the latest snapshot per
    bridge when no explicit year is requested.
    """
    conn = get_connection(db_path)
    params: List[Any] = []
    filters = [
        "latitude IS NOT NULL",
        "longitude IS NOT NULL",
        "latitude BETWEEN 17 AND 72",
        "longitude BETWEEN -180 AND -65",
    ]
    if state_code is not None:
        filters.append("state_code = ?")
        params.append(state_code)
    if county_code is not None:
        filters.append("county_code = ?")
        params.append(county_code)
    if year is not None:
        filters.append("data_year = ?")
        params.append(year)
    else:
        filters.append(
            """(data_year IS NULL OR data_year = (
              SELECT MAX(b2.data_year) FROM bridges b2
              WHERE b2.state_code = b.state_code AND b2.bridge_number = b.bridge_number
          ))"""
        )

    limit_clause = ""
    if limit is not None and limit > 0:
        limit_clause = "LIMIT ?"
        params.append(limit)

    # Derived display name for map cards only (Editor keeps SNBI B.ID.02 as-is):
    # "<carried feature> over <below feature>" from the features table, which holds
    # legacy Item 7 (facility carried) and Item 6A (features intersected) text.
    name_subqueries = '''
               (SELECT NULLIF(TRIM(f.feature_name), '') FROM features f
                 WHERE f.state_code = b.state_code AND f.bridge_number = b.bridge_number
                   AND f.data_year IS b.data_year AND f.feature_location = 'C'
                   AND NULLIF(TRIM(f.feature_name), '') IS NOT NULL
                 ORDER BY f.feature_type LIMIT 1) AS carried_name,
               (SELECT NULLIF(TRIM(f.feature_name), '') FROM features f
                 WHERE f.state_code = b.state_code AND f.bridge_number = b.bridge_number
                   AND f.data_year IS b.data_year AND f.feature_location = 'B'
                   AND NULLIF(TRIM(f.feature_name), '') IS NOT NULL
                 ORDER BY f.feature_type LIMIT 1) AS below_name,
    '''
    df = pd.read_sql_query(f'''
        SELECT {name_subqueries}
               bridge_name, bridge_number, bridge_location, latitude, longitude, state_code,
               county_code, data_year, year_built,
               deck_condition_rating,
               superstructure_condition_rating,
               substructure_condition_rating,
               culvert_condition_rating,
               bridge_condition_classification,
               lowest_condition_rating_code,
               channel_condition_rating,
               scour_condition_rating,
               bridge_railings_condition_rating,
               bridge_bearings_condition_rating,
               bridge_joints_condition_rating,
               total_bridge_length,
               bridge_width_out_to_out,
               owner,
               maintenance_responsibility,
               load_rating_date,
               scour_vulnerability,
               overtopping_likelihood
        FROM bridges b
        WHERE {' AND '.join(filters)}
        ORDER BY state_code, county_code, bridge_number
        {limit_clause}
    ''', conn, params=params)
    conn.close()
    # Replace NaN with None for JSON serialization
    df = df.where(pd.notna(df), None)
    records = df.to_dict(orient="records")
    for rec in records:
        carried = (rec.pop("carried_name", None) or "").strip()
        below = (rec.pop("below_name", None) or "").strip()
        if carried and below:
            rec["display_name"] = f"{carried} over {below}"
        else:
            rec["display_name"] = carried or below or None
    return records


def get_map_counties(db_path: str = USER_DB, state_code: Optional[int] = None, year: Optional[int] = None) -> List[Dict]:
    """Return counties with bridge counts for the current/latest map snapshot."""
    conn = get_connection(db_path)
    try:
        params: List[Any] = []
        filters = ["county_code IS NOT NULL"]
        if state_code is not None:
            filters.append("state_code = ?")
            params.append(state_code)
        if year is not None:
            filters.append("data_year = ?")
            params.append(year)
        else:
            filters.append(
                """(data_year IS NULL OR data_year = (
                    SELECT MAX(b2.data_year) FROM bridges b2
                    WHERE b2.state_code = b.state_code AND b2.bridge_number = b.bridge_number
                ))"""
            )
        df = pd.read_sql_query(
            f"""
            SELECT state_code, county_code, COUNT(*) AS bridge_count
            FROM bridges b
            WHERE {' AND '.join(filters)}
            GROUP BY state_code, county_code
            ORDER BY state_code, county_code
            """,
            conn,
            params=params,
        )
        df = df.where(pd.notna(df), None)
        return df.to_dict(orient="records")
    finally:
        conn.close()


def get_loaded_years(db_path: str = USER_DB) -> List[int]:
    """Return all distinct data_year values loaded into the bridges table, newest first."""
    conn = get_connection(db_path)
    try:
        df = pd.read_sql_query(
            "SELECT DISTINCT data_year FROM bridges WHERE data_year IS NOT NULL ORDER BY data_year DESC",
            conn,
        )
        return df['data_year'].tolist()
    finally:
        conn.close()


# ====================== KPIS ======================
def get_state_kpis(db_path: str = USER_DB, year: Optional[int] = None) -> Dict:
    """Calculate KPIs by state."""
    current_year = datetime.now().year

    conn = get_connection(db_path)

    # Determine the effective year filter
    if year is None:
        row = conn.execute(
            "SELECT MAX(data_year) FROM bridges WHERE data_year IS NOT NULL"
        ).fetchone()
        effective_year = row[0] if row and row[0] else None
    else:
        effective_year = year

    year_filter = f"WHERE b.data_year = {effective_year}" if effective_year is not None else ""
    feat_year_join = (
        f"AND (f.data_year = {effective_year} OR f.data_year IS NULL)"
        if effective_year is not None
        else ""
    )

    query = f"""
    SELECT 
        b.state_code,
        b.bridge_number,
        b.year_built,
        b.deck_condition_rating,
        b.superstructure_condition_rating,
        b.substructure_condition_rating,
        b.culvert_condition_rating,
        b.calculated_deck_area,
        b.total_bridge_length,
        b.bridge_width_out_to_out,
        MAX(f.annual_average_daily_traffic) as aadt
    FROM bridges b
    LEFT JOIN features f 
        ON b.state_code = f.state_code AND b.bridge_number = f.bridge_number {feat_year_join}
    {year_filter}
    GROUP BY b.state_code, b.bridge_number
    """
    df = pd.read_sql_query(query, conn)
    conn.close()

    # Convert ratings to numeric
    for col in ['deck_condition_rating', 'superstructure_condition_rating', 
                'substructure_condition_rating', 'culvert_condition_rating']:
        df[col] = pd.to_numeric(df[col], errors='coerce')

    # Calculate lowest condition
    condition_cols = ['deck_condition_rating', 'superstructure_condition_rating', 'substructure_condition_rating']
    df['lowest_condition'] = df[condition_cols].min(axis=1, skipna=True)
    
    df['lowest_condition'] = df.apply(
        lambda row: row['culvert_condition_rating'] 
        if pd.isna(row['lowest_condition']) and not pd.isna(row['culvert_condition_rating'])
        else row['lowest_condition'], axis=1
    )

    def classify_condition(rating):
        if pd.isna(rating):
            return 'Unknown'
        if rating >= 7:
            return 'Good'
        elif rating >= 5:
            return 'Fair'
        else:
            return 'Poor'

    df['condition_class'] = df['lowest_condition'].apply(classify_condition)

    # Deck area fallback
    df['deck_area'] = df['calculated_deck_area'].fillna(
        df['total_bridge_length'] * df['bridge_width_out_to_out']
    ).fillna(0)

    df['state_name'] = df['state_code'].map(STATE_MAP).fillna('Unknown')

    # Aggregate
    kpi_df = df.groupby(['state_code', 'state_name']).agg(
        total_bridges=('bridge_number', 'count'),
        good_bridges=('condition_class', lambda x: (x == 'Good').sum()),
        fair_bridges=('condition_class', lambda x: (x == 'Fair').sum()),
        poor_bridges=('condition_class', lambda x: (x == 'Poor').sum()),
        avg_age=('year_built', lambda x: current_year - x.mean() if x.notna().any() else None),
        total_deck_area=('deck_area', 'sum'),
        high_traffic_bridges=('aadt', lambda x: (x > 10000).sum()),
        old_bridges=('year_built', lambda x: (x < 1980).sum())
    ).reset_index()

    kpi_df['pct_good'] = (kpi_df['good_bridges'] / kpi_df['total_bridges'] * 100).round(1)
    kpi_df['pct_fair'] = (kpi_df['fair_bridges'] / kpi_df['total_bridges'] * 100).round(1)
    kpi_df['pct_poor'] = (kpi_df['poor_bridges'] / kpi_df['total_bridges'] * 100).round(1)
    kpi_df['pct_old'] = (kpi_df['old_bridges'] / kpi_df['total_bridges'] * 100).round(1)

    # Replace NaN with None for JSON serialization
    kpi_df = kpi_df.where(pd.notna(kpi_df), None)
    return kpi_df.to_dict(orient="records")


NJ_COUNTY_NAMES = {
    1: 'Atlantic', 3: 'Bergen', 5: 'Burlington', 7: 'Camden', 9: 'Cape May',
    11: 'Cumberland', 13: 'Essex', 15: 'Gloucester', 17: 'Hudson', 19: 'Hunterdon',
    21: 'Mercer', 23: 'Middlesex', 25: 'Monmouth', 27: 'Morris', 29: 'Ocean',
    31: 'Passaic', 33: 'Salem', 35: 'Somerset', 37: 'Sussex', 39: 'Union', 41: 'Warren',
}


def get_county_kpis(db_path: str = USER_DB, state_code: int = 34, year: Optional[int] = None) -> Dict:
    """KPIs grouped by county for a single state (default New Jersey)."""
    current_year = datetime.now().year
    conn = get_connection(db_path)
    if year is None:
        row = conn.execute(
            "SELECT MAX(data_year) FROM bridges WHERE state_code = ? AND data_year IS NOT NULL",
            (int(state_code),),
        ).fetchone()
        effective_year = row[0] if row and row[0] else None
    else:
        effective_year = year
    conds = [f"b.state_code = {int(state_code)}"]
    if effective_year is not None:
        conds.append(f"b.data_year = {int(effective_year)}")
    where = "WHERE " + " AND ".join(conds)
    query = f"""
    SELECT b.county_code, b.bridge_number, b.year_built,
           b.deck_condition_rating, b.superstructure_condition_rating,
           b.substructure_condition_rating, b.culvert_condition_rating
    FROM bridges b
    {where}
    GROUP BY b.county_code, b.bridge_number
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    if df.empty:
        return []
    for col in ['deck_condition_rating', 'superstructure_condition_rating',
                'substructure_condition_rating', 'culvert_condition_rating']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    cc = ['deck_condition_rating', 'superstructure_condition_rating', 'substructure_condition_rating']
    df['lowest_condition'] = df[cc].min(axis=1, skipna=True)
    df['lowest_condition'] = df.apply(
        lambda r: r['culvert_condition_rating']
        if pd.isna(r['lowest_condition']) and not pd.isna(r['culvert_condition_rating'])
        else r['lowest_condition'], axis=1)

    def _cls(r):
        if pd.isna(r):
            return 'Unknown'
        if r >= 7:
            return 'Good'
        if r >= 5:
            return 'Fair'
        return 'Poor'

    df['condition_class'] = df['lowest_condition'].apply(_cls)
    kpi = df.groupby('county_code').agg(
        total_bridges=('bridge_number', 'count'),
        good_bridges=('condition_class', lambda x: (x == 'Good').sum()),
        fair_bridges=('condition_class', lambda x: (x == 'Fair').sum()),
        poor_bridges=('condition_class', lambda x: (x == 'Poor').sum()),
        avg_age=('year_built', lambda x: current_year - x.mean() if x.notna().any() else None),
    ).reset_index()
    kpi['county_code'] = kpi['county_code'].fillna(0).astype(int)
    kpi['county_name'] = kpi['county_code'].map(NJ_COUNTY_NAMES).fillna(
        kpi['county_code'].apply(lambda c: f"County {int(c):03d}"))
    kpi['pct_good'] = (kpi['good_bridges'] / kpi['total_bridges'] * 100).round(1)
    kpi['pct_fair'] = (kpi['fair_bridges'] / kpi['total_bridges'] * 100).round(1)
    kpi['pct_poor'] = (kpi['poor_bridges'] / kpi['total_bridges'] * 100).round(1)
    kpi = kpi.where(pd.notna(kpi), None)
    return kpi.to_dict(orient="records")


def delete_bridges_by_year(db_path: str, state_code: int, data_year: int) -> int:
    """Delete all rows for a state + data year across every table that carries both
    state_code and data_year (bridges + child tables). Returns bridges deleted."""
    conn = get_connection(db_path)
    cur = conn.cursor()
    n = cur.execute(
        "SELECT COUNT(*) FROM bridges WHERE state_code = ? AND data_year = ?",
        (int(state_code), int(data_year)),
    ).fetchone()[0]
    tables = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    for tbl in tables:
        if tbl.startswith('sqlite_'):
            continue
        cols = [r[1] for r in cur.execute(f"PRAGMA table_info({tbl})").fetchall()]
        if 'state_code' in cols and 'data_year' in cols:
            cur.execute(f"DELETE FROM {tbl} WHERE state_code = ? AND data_year = ?",
                        (int(state_code), int(data_year)))
    conn.commit()
    conn.close()
    return n


def delete_single_bridge(db_path: str, state_code: int, bridge_number: str, data_year: int) -> int:
    """Delete ONE bridge's snapshot for a single year — from the SNBI tables only
    (those keyed by state_code + bridge_number + data_year). Leaves custom tables and
    non-year-scoped local tables (attachments, etc.) untouched. Returns bridges deleted."""
    conn = get_connection(db_path)
    cur = conn.cursor()
    n = cur.execute(
        "SELECT COUNT(*) FROM bridges WHERE state_code=? AND bridge_number=? AND data_year=?",
        (int(state_code), str(bridge_number), int(data_year)),
    ).fetchone()[0]
    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    for tbl in tables:
        if tbl.startswith('sqlite_') or tbl in CUSTOM_TABLE_NAMES:
            continue
        cols = [r[1] for r in cur.execute(f"PRAGMA table_info({tbl})").fetchall()]
        if {'state_code', 'bridge_number', 'data_year'} <= set(cols):
            cur.execute(f"DELETE FROM {tbl} WHERE state_code=? AND bridge_number=? AND data_year=?",
                        (int(state_code), str(bridge_number), int(data_year)))
    conn.commit()
    conn.close()
    return n


# ====================== USER CUSTOM TABLES (separate from SNBI) ======================
def _ensure_custom_tables_schema(db_path: str) -> None:
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS custom_tables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            state_code INTEGER NOT NULL,
            bridge_number TEXT,
            name TEXT NOT NULL,
            columns_json TEXT NOT NULL,
            color TEXT,
            scope TEXT NOT NULL DEFAULT 'bridge',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS custom_table_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            custom_table_id INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            cells_json TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.commit()
    finally:
        conn.close()


def create_custom_table(db_path: str, state_code: int, bridge_number, name: str,
                        columns: list, color: str, scope: str) -> int:
    """Create a custom-table DEFINITION. scope='bridge' → tied to one bridge;
    scope='state' → applies to every bridge in the state (bridge_number stored NULL),
    each bridge keeping its own rows."""
    _ensure_custom_tables_schema(db_path)
    bn = None if scope == 'state' else (str(bridge_number) if bridge_number is not None else None)
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO custom_tables (state_code, bridge_number, name, columns_json, color, scope) "
            "VALUES (?,?,?,?,?,?)",
            (int(state_code), bn, name, json.dumps(list(columns or [])), color, scope))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_custom_tables_for_bridge(db_path: str, state_code: int, bridge_number: str) -> List[Dict]:
    """Custom tables visible for a bridge: its own per-bridge tables + the state-wide
    definitions for its state. Rows are always scoped to THIS bridge."""
    _ensure_custom_tables_schema(db_path)
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        defs = cur.execute(
            "SELECT id, name, columns_json, color, scope FROM custom_tables "
            "WHERE (state_code=? AND bridge_number=?) OR (state_code=? AND scope='state') "
            "ORDER BY id",
            (int(state_code), str(bridge_number), int(state_code))).fetchall()
        out: List[Dict] = []
        for tid, name, cols, color, scope in defs:
            rows = cur.execute(
                "SELECT id, cells_json FROM custom_table_rows "
                "WHERE custom_table_id=? AND bridge_number=? ORDER BY id",
                (tid, str(bridge_number))).fetchall()
            out.append({
                'id': tid, 'name': name, 'color': color, 'scope': scope,
                'columns': json.loads(cols or '[]'),
                'rows': [{'id': rid, 'cells': json.loads(cj or '{}')} for rid, cj in rows],
            })
        return out
    finally:
        conn.close()


def add_custom_row(db_path: str, custom_table_id: int, bridge_number: str, cells: dict) -> int:
    _ensure_custom_tables_schema(db_path)
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO custom_table_rows (custom_table_id, bridge_number, cells_json) VALUES (?,?,?)",
            (int(custom_table_id), str(bridge_number), json.dumps(cells or {})))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def update_custom_row(db_path: str, row_id: int, cells: dict) -> int:
    _ensure_custom_tables_schema(db_path)
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE custom_table_rows SET cells_json=? WHERE id=?",
                    (json.dumps(cells or {}), int(row_id)))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def delete_custom_row(db_path: str, row_id: int) -> int:
    _ensure_custom_tables_schema(db_path)
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM custom_table_rows WHERE id=?", (int(row_id),))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def delete_custom_table(db_path: str, custom_table_id: int) -> int:
    """Delete a custom-table definition and ALL its rows (across every bridge)."""
    _ensure_custom_tables_schema(db_path)
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM custom_table_rows WHERE custom_table_id=?", (int(custom_table_id),))
        cur.execute("DELETE FROM custom_tables WHERE id=?", (int(custom_table_id),))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def get_bridge_report_files(state_code, bridge_number, db_path: str = USER_DB) -> List[Dict]:
    """Return inspection-report files attached to a bridge (id, filename, filepath, …)."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT id, filename, filepath, size_bytes, uploaded_at FROM bridge_attachments "
            "WHERE state_code=? AND bridge_number=? AND attachment_type='inspection_report' "
            "ORDER BY id DESC",
            (str(state_code), str(bridge_number)),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    return [
        {"id": r[0], "filename": r[1], "filepath": r[2], "size_bytes": r[3], "uploaded_at": r[4]}
        for r in rows
    ]


def get_state_condition_history(db_path: str, state_code: int) -> List[Dict[str, Any]]:
    """Aggregate year-over-year state condition metrics from loaded SNBI snapshots."""
    conn = get_connection(db_path)
    try:
        df = pd.read_sql_query(
            """
            SELECT data_year,
                   lowest_condition_rating_code,
                   deck_condition_rating,
                   superstructure_condition_rating,
                   substructure_condition_rating,
                   culvert_condition_rating
            FROM bridges
            WHERE state_code = ?
              AND data_year IS NOT NULL
            """,
            conn,
            params=(state_code,),
        )

        if df.empty:
            return []

        for col in [
            'lowest_condition_rating_code',
            'deck_condition_rating',
            'superstructure_condition_rating',
            'substructure_condition_rating',
            'culvert_condition_rating',
        ]:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        df['year'] = pd.to_numeric(df['data_year'], errors='coerce').astype('Int64')
        df = df[df['year'].notna()]
        if df.empty:
            return []

        df['derived_lowest'] = df[['deck_condition_rating', 'superstructure_condition_rating', 'substructure_condition_rating', 'culvert_condition_rating']].min(axis=1, skipna=True)
        df['lowest'] = df['lowest_condition_rating_code'].fillna(df['derived_lowest'])
        df = df[df['lowest'].notna()]
        if df.empty:
            return []

        def _classify(r: float) -> str:
            if r >= 7:
                return 'Good'
            if r >= 5:
                return 'Fair'
            return 'Poor'

        df['class'] = df['lowest'].apply(_classify)

        grouped = df.groupby('year').agg(
            total=('lowest', 'count'),
            poor=('class', lambda x: int((x == 'Poor').sum())),
            fair=('class', lambda x: int((x == 'Fair').sum())),
            good=('class', lambda x: int((x == 'Good').sum())),
            avg_lowest_condition=('lowest', 'mean'),
        ).reset_index()

        grouped['pct_poor'] = (grouped['poor'] / grouped['total'] * 100).round(1)
        grouped['avg_lowest_condition'] = grouped['avg_lowest_condition'].round(2)
        grouped = grouped.sort_values('year')

        grouped = grouped.where(pd.notna(grouped), None)
        return grouped.to_dict(orient='records')
    finally:
        conn.close()


# ====================== EDITOR HELPERS ======================
CHILD_TABLES = [
    "features", "span_sets", "substructure_sets", "elements",
    "inspections", "posting_evaluations", "posting_statuses", "works"
]


def update_bridge_id(db_path: str, old_state: int, old_bridge: str, new_state: int, new_bridge: str):
    """Update bridge ID across all related tables."""
    conn = get_connection(db_path)
    cur = conn.cursor()
    try:
        cur.execute("BEGIN")
        for table in CHILD_TABLES:
            cur.execute(f"""
                UPDATE {table}
                SET state_code=?, bridge_number=?
                WHERE state_code=? AND bridge_number=?
            """, (new_state, new_bridge, old_state, old_bridge))

        cur.execute("""
            UPDATE bridges
            SET state_code=?, bridge_number=?
            WHERE state_code=? AND bridge_number=?
        """, (new_state, new_bridge, old_state, old_bridge))

        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def build_snbi_bridge_json(db_path: str, state_code: int, bridge_number: str) -> Dict:
    """Export a single bridge as SNBI JSON format."""
    conn = get_connection(db_path)
    cur = conn.cursor()

    # Main bridge
    cur.execute("SELECT * FROM bridges WHERE state_code=? AND bridge_number=?", (state_code, bridge_number))
    bridge_row = dict(zip([d[0] for d in cur.description], cur.fetchone() or {}))

    bridge_json = {REVERSE_MAPPING.get(col, col): val for col, val in bridge_row.items() if val is not None}

    # Features + Routes (simplified version - expand as needed)
    cur.execute("SELECT * FROM features WHERE state_code=? AND bridge_number=?", (state_code, bridge_number))
    features = []
    for f in cur.fetchall():
        fdict = dict(zip([d[0] for d in cur.description], f))
        feature_json = {REVERSE_MAPPING.get(col, col): val for col, val in fdict.items() if val is not None}
        features.append(feature_json)

    if features:
        bridge_json["Features"] = features

    # Elements
    cur.execute("SELECT * FROM elements WHERE state_code=? AND bridge_number=?", (state_code, bridge_number))
    elements = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
    if elements:
        bridge_json["Elements"] = elements

    conn.close()
    return bridge_json


def save_table_data(db_path: str, table: str, state_code: int, bridge_number: str, df: pd.DataFrame):
    """Save edited dataframe back to a child table."""
    conn = get_connection(db_path)
    cur = conn.cursor()
    try:
        cur.execute(f"DELETE FROM {table} WHERE state_code=? AND bridge_number=?", (state_code, bridge_number))
        df.to_sql(table, conn, if_exists="append", index=False)
        conn.commit()
    finally:
        conn.close()


def get_bridge_details(db_path: str, state_code: int, bridge_number: str) -> Dict:
    """Get detailed information for a specific bridge."""
    conn = get_connection(db_path)
    cur = conn.cursor()

    # Get main bridge data
    cur.execute("SELECT * FROM bridges WHERE state_code=? AND bridge_number=?", (state_code, bridge_number))
    bridge_row = cur.fetchone()
    
    if not bridge_row:
        conn.close()
        return None
    
    bridge_data = dict(zip([d[0] for d in cur.description], bridge_row))
    
    # Get features
    cur.execute("SELECT * FROM features WHERE state_code=? AND bridge_number=?", (state_code, bridge_number))
    features = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
    
    # Get elements
    cur.execute("SELECT * FROM elements WHERE state_code=? AND bridge_number=?", (state_code, bridge_number))
    elements = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
    
    conn.close()
    
    # Combine data
    result = {REVERSE_MAPPING.get(col, col): val for col, val in bridge_data.items() if val is not None}
    if features:
        result["Features"] = features
    if elements:
        result["Elements"] = elements
        
    return result


def get_bridge_field_metadata(db_path: str, state_code: int, bridge_number: str, year: Optional[int] = None) -> Dict[str, Any]:
    """Return editor-only metadata computed on demand for one bridge.

    Static SNBI-to-NBI mapping/default rules live in ``editor_metadata.py``.
    Only the compact original NBI row is persisted per bridge, so loading a
    full state does not write one metadata row for every visible field.
    """
    ensure_schema(db_path)
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bridge_source_records'")
        if not cur.fetchone():
            return {"by_tag": {}, "by_table": {}}

        params: List[Any] = [state_code, bridge_number]
        if year is not None:
            year_clause = "AND data_year = ?"
            params.append(year)
        else:
            year_clause = """
                AND data_year = (
                    SELECT MAX(data_year)
                    FROM bridge_source_records
                    WHERE state_code = ? AND bridge_number = ?
                )
            """
            params.extend([state_code, bridge_number])

        cur.execute(
            f"""
            SELECT source_payload_json
            FROM bridge_source_records
            WHERE state_code = ? AND bridge_number = ?
              AND source_name = 'NBI TXT'
            {year_clause}
            LIMIT 1
            """,
            params,
        )
        row = cur.fetchone()
        old_nbi = json.loads(row[0]) if row and row[0] else {}
    finally:
        conn.close()

    bridge_payload = [
        bridge for bridge in export_all_bridges_snbi(db_path, state_code, year=year)
        if str(bridge.get("BID01", "")) == str(bridge_number)
    ]
    if not bridge_payload:
        return {"by_tag": {}, "by_table": {}, "bridge_notes": []}

    rows = build_editor_metadata(bridge_payload[0], old_nbi if isinstance(old_nbi, dict) else {})
    result: Dict[str, Any] = {"by_tag": {}, "by_table": {}, "bridge_notes": []}

    if isinstance(old_nbi, dict) and old_nbi:
        service_on = str(old_nbi.get('SERVICE_ON_042A') or '').strip()
        if service_on in {'', '0', '2', '3', '9'}:
            kind = {
                '2': 'railroad', '3': 'pedestrian/trail', '9': 'building or plaza',
                '0': 'other service',
            }.get(service_on, 'unspecified service (Item 42A blank)')
            result["bridge_notes"].append({
                "type": "fhwa_omit",
                "note": (
                    f"Legacy Item 42A codes this as a non-highway bridge ({kind}). "
                    "The FHWA SNBI crosswalk states such records should be omitted "
                    "from the NBI transition. It is kept here for completeness - "
                    "review before including it in an FHWA submission."
                ),
            })
    for item in rows:
        table_name = item.get("table") or "bridges"
        tag = item.get("tag")
        row_index = item.get("row_index")
        if not tag:
            continue
        if table_name == "bridges":
            result["by_tag"].setdefault(tag, []).append(item)
        else:
            table_bucket = result["by_table"].setdefault(table_name, {})
            row_bucket = table_bucket.setdefault(str(row_index if row_index is not None else 0), {})
            row_bucket.setdefault(tag, []).append(item)
    return result


def export_all_bridges_snbi(db_path: str, state_code: int, year: Optional[int] = None) -> List[Dict]:
    """Bulk-export bridges for a state as SNBI-coded dicts.

    ``data_year`` is internal DB metadata. It is used only to choose the
    snapshot to export and is never emitted as an SNBI JSON key.
    """
    conn = get_connection(db_path)
    cur = conn.cursor()

    def _fetch(table: str) -> List[Dict]:
        try:
            cur.execute(f"PRAGMA table_info({table})")
            col_names = {row[1] for row in cur.fetchall()}
            if "data_year" in col_names:
                if year is not None:
                    cur.execute(
                        f"SELECT * FROM {table} WHERE state_code=? AND data_year=?",
                        (state_code, year),
                    )  # noqa: S608
                elif table == "bridges":
                    cur.execute(
                        """
                        SELECT *
                        FROM bridges b
                        WHERE state_code=?
                          AND (data_year IS NULL OR data_year = (
                              SELECT MAX(b2.data_year)
                              FROM bridges b2
                              WHERE b2.state_code = b.state_code
                                AND b2.bridge_number = b.bridge_number
                          ))
                        """,
                        (state_code,),
                    )
                else:
                    cur.execute(
                        f"""
                        SELECT t.*
                        FROM {table} t
                        JOIN bridges b
                          ON b.state_code = t.state_code
                         AND b.bridge_number = t.bridge_number
                         AND b.data_year = t.data_year
                        WHERE t.state_code=?
                          AND (t.data_year IS NULL OR t.data_year = (
                              SELECT MAX(b2.data_year)
                              FROM bridges b2
                              WHERE b2.state_code = t.state_code
                                AND b2.bridge_number = t.bridge_number
                          ))
                        """,
                        (state_code,),
                    )  # noqa: S608
            else:
                cur.execute(f"SELECT * FROM {table} WHERE state_code=?", (state_code,))  # noqa: S608
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception:
            return []

    def _remap(rows: List[Dict], allowed_tags: Optional[set] = None) -> List[Dict]:
        """Apply REVERSE_MAPPING, drop blanks, keep only tags valid for the schema."""
        out = []
        for row in rows:
            mapped: Dict[str, Any] = {}
            for col, val in row.items():
                if col not in REVERSE_MAPPING or val is None:
                    continue
                tag = REVERSE_MAPPING[col]
                if allowed_tags is not None and tag not in allowed_tags:
                    continue
                cleaned = val.strip().strip("'\"").strip() if isinstance(val, str) else val
                if cleaned in ("", None):
                    continue
                mapped[tag] = cleaned
            if mapped:
                out.append(mapped)
        return out

    try:
        bridges_raw    = _fetch("bridges")
        features_raw   = _fetch("features")
        routes_raw     = _fetch("routes")
        span_sets_raw  = _fetch("span_sets")
        sub_sets_raw   = _fetch("substructure_sets")
        elements_raw   = _fetch("elements")
        inspections_raw = _fetch("inspections")
        posting_evals_raw = _fetch("posting_evaluations")
        posting_status_raw = _fetch("posting_statuses")
        works_raw      = _fetch("works")
    finally:
        conn.close()

    def _group(rows: List[Dict]) -> Dict[str, list]:
        d: Dict[str, list] = {}
        for r in rows:
            bn = str(r.get('bridge_number', ''))
            d.setdefault(bn, []).append(r)
        return d

    features_by_bridge  = _group(features_raw)
    routes_by_bridge    = _group(routes_raw)
    span_sets_by_bridge = _group(span_sets_raw)
    sub_sets_by_bridge  = _group(sub_sets_raw)
    elements_by_bridge  = _group(elements_raw)
    inspections_by_bridge = _group(inspections_raw)
    posting_evals_by_bridge = _group(posting_evals_raw)
    posting_status_by_bridge = _group(posting_status_raw)
    works_by_bridge     = _group(works_raw)

    def _apply_fhwa_export_defaults(snbi: Dict[str, Any]) -> None:
        """Repair known legacy/default values before writing FHWA-facing JSON."""
        def _to_float(value: Any) -> Optional[float]:
            try:
                if value in (None, ""):
                    return None
                return float(value)
            except Exception:
                return None

        def _truncate_hundredth(value: float) -> float:
            return int(value * 100) / 100

        def _normalize_code_values(obj: Any) -> None:
            if isinstance(obj, dict):
                for key, value in list(obj.items()):
                    if isinstance(value, (dict, list)):
                        _normalize_code_values(value)
                    elif key in CODE_LISTS and isinstance(value, (int, float)):
                        obj[key] = str(int(value)) if float(value).is_integer() else str(value)
            elif isinstance(obj, list):
                for item in obj:
                    _normalize_code_values(item)

        _normalize_code_values(snbi)

        posting_evals = snbi.get("PostingEvaluations") if isinstance(snbi.get("PostingEvaluations"), list) else []
        legal_load_factors = [
            factor for factor in (_to_float(row.get("BEP02")) for row in posting_evals if isinstance(row, dict))
            if factor is not None
        ]
        if legal_load_factors:
            snbi["BLR07"] = _truncate_hundredth(min(legal_load_factors))
        elif snbi.get("BLR07") in (None, "") and snbi.get("BLR06") not in (None, ""):
            # Transition fallback from the SNBI commentary: BLR06 may be reported
            # when all legal loads are enveloped by the operating rating.
            snbi["BLR07"] = snbi["BLR06"]

        if snbi.get("BAP05") in (None, "", "U"):
            snbi["BAP05"] = "0"
        if snbi.get("BLR08") in (None, ""):
            snbi["BLR08"] = "N"
        if snbi.get("BG12") in (None, ""):
            snbi["BG12"] = "N"
        if snbi.get("BG13") in (None, "", 0, "0"):
            snbi["BG13"] = 1
        if snbi.get("BG14") in (None, ""):
            snbi["BG14"] = "N"

        features = snbi.get("Features") if isinstance(snbi.get("Features"), list) else []
        has_waterway = any(isinstance(feature, dict) and str(feature.get("BF01") or "").startswith("W") for feature in features)
        if has_waterway and str(snbi.get("BC09") or "").upper() == "N":
            snbi["BC09"] = "0"
        for feature in features:
            if not isinstance(feature, dict):
                continue
            feature_type = str(feature.get("BF01") or "")
            if feature_type.startswith("W") and not feature.get("BN01"):
                feature["BN01"] = "N"
            if not feature_type.startswith("W"):
                for tag in ("BN01", "BN02", "BN03", "BN04", "BN05", "BN06"):
                    feature.pop(tag, None)
            if feature.get("BN01") in {"N", "U"}:
                for tag in ("BN02", "BN03", "BN04", "BN05", "BN06"):
                    feature.pop(tag, None)
            if feature_type.startswith("H"):
                for route in feature.get("Routes", []) if isinstance(feature.get("Routes"), list) else []:
                    if isinstance(route, dict) and not route.get("BRT03"):
                        route["BRT03"] = "2-T"

        substructures = snbi.get("SubstructureSets") if isinstance(snbi.get("SubstructureSets"), list) else []
        for idx, sub in enumerate(substructures):
            if not isinstance(sub, dict):
                continue
            if not sub.get("BSB01") or str(sub.get("BSB01")).startswith("SS"):
                sub["BSB01"] = "P01" if idx == 0 else f"P{idx + 1:02d}"
            if sub.get("BSB02") in (None, "", 0, "0"):
                sub["BSB02"] = 1
            if sub.get("BSB03") in (None, "", "X-T"):
                sub["BSB03"] = "C01"
            if sub.get("BSB04") in (None, ""):
                sub["BSB04"] = "P01" if str(sub.get("BSB01") or "").startswith("P") else "A01"
            if sub.get("BSB05") in (None, ""):
                sub["BSB05"] = "0"
            if sub.get("BSB06") in (None, ""):
                sub["BSB06"] = "X"
            if sub.get("BSB07") in (None, ""):
                sub["BSB07"] = "0"

        spans = snbi.get("SpanSets") if isinstance(snbi.get("SpanSets"), list) else []
        for span in spans:
            if not isinstance(span, dict):
                continue
            if span.get("BSP03") in (None, "", 0, "0"):
                span["BSP03"] = 1
            if span.get("BSP07") in (None, ""):
                span["BSP07"] = "0"
            if str(span.get("BSP09") or "") == "0":
                span.pop("BSP08", None)
                span.pop("BSP13", None)
            else:
                if span.get("BSP08") in (None, ""):
                    span["BSP08"] = "0"
                if span.get("BSP13") in (None, ""):
                    span["BSP13"] = "0"
        has_steel = any(isinstance(span, dict) and str(span.get("BSP04") or "").startswith("S") for span in spans)
        has_steel = has_steel or any(isinstance(sub, dict) and str(sub.get("BSB03") or "").startswith("S") for sub in substructures)
        if not has_steel:
            snbi.pop("BIR02", None)
        if str(snbi.get("BIR01") or "").upper() != "Y":
            snbi.pop("BC14", None)
        elif not snbi.get("BC14"):
            snbi["BC14"] = "N"
        if str(snbi.get("BIR03") or "").upper() == "Y" and not snbi.get("BC15"):
            snbi["BC15"] = "N"
        if str(snbi.get("BIR03") or "").upper() != "Y":
            snbi.pop("BC15", None)

        works = snbi.get("Works") if isinstance(snbi.get("Works"), list) else []
        for work in works:
            if isinstance(work, dict) and not work.get("BW03"):
                work["BW03"] = "0"

        if not snbi.get("BG05"):
            fallback_width = snbi.get("BG06") or snbi.get("BG09")
            if fallback_width:
                snbi["BG05"] = fallback_width

    result = []
    for bridge_data in bridges_raw:
        bn = str(bridge_data.get('bridge_number', ''))

        # Main bridge row — apply REVERSE_MAPPING and drop blank/whitespace values
        snbi: Dict[str, Any] = {}
        for col, val in bridge_data.items():
            if col not in REVERSE_MAPPING or val is None:
                continue
            cleaned = val.strip().strip("'\"").strip() if isinstance(val, str) else val
            if cleaned in ("", None):
                continue
            snbi[REVERSE_MAPPING[col]] = cleaned

        # Features — remap to SNBI then nest routes under their parent feature
        raw_feats = features_by_bridge.get(bn, [])
        if raw_feats:
            mapped_feats = _remap(raw_feats, NESTED_ARRAY_SPECS["Features"]["fields"] - {"Routes"})
            # Map raw routes for this bridge, group by feature_type (→ BF01 after remap)
            raw_routes = routes_by_bridge.get(bn, [])
            routes_by_feat: Dict[str, list] = {}
            for raw_route in raw_routes:
                ft = str(raw_route.get('feature_type', ''))
                if ft:
                    mapped_route = _remap([raw_route], NESTED_ARRAY_SPECS["Routes"]["fields"])
                    if mapped_route:
                        routes_by_feat.setdefault(ft, []).append(mapped_route[0])
            for feat in mapped_feats:
                ft = str(feat.get('BF01', ''))
                if ft in routes_by_feat:
                    feat['Routes'] = routes_by_feat.pop(ft)
            # Attach any orphan routes as minimal feature shells
            for ft, orphans in routes_by_feat.items():
                mapped_feats.append({'BF01': ft, 'Routes': orphans})
            snbi['Features'] = mapped_feats

        # SpanSets
        raw_spans = span_sets_by_bridge.get(bn, [])
        if raw_spans:
            snbi['SpanSets'] = _remap(raw_spans, NESTED_ARRAY_SPECS["SpanSets"]["fields"])

        raw_subs = sub_sets_by_bridge.get(bn, [])
        if raw_subs:
            snbi['SubstructureSets'] = _remap(raw_subs, NESTED_ARRAY_SPECS["SubstructureSets"]["fields"])

        # Elements
        raw_elems = elements_by_bridge.get(bn, [])
        if raw_elems:
            snbi['Elements'] = _remap(raw_elems, NESTED_ARRAY_SPECS["Elements"]["fields"])

        raw_inspections = inspections_by_bridge.get(bn, [])
        if raw_inspections:
            snbi['Inspections'] = _remap(raw_inspections, NESTED_ARRAY_SPECS["Inspections"]["fields"])

        raw_posting_evals = posting_evals_by_bridge.get(bn, [])
        if raw_posting_evals:
            snbi['PostingEvaluations'] = _remap(raw_posting_evals, NESTED_ARRAY_SPECS["PostingEvaluations"]["fields"])

        raw_posting_statuses = posting_status_by_bridge.get(bn, [])
        if raw_posting_statuses:
            snbi['PostingStatuses'] = _remap(raw_posting_statuses, NESTED_ARRAY_SPECS["PostingStatuses"]["fields"])

        # Works
        raw_works = works_by_bridge.get(bn, [])
        if raw_works:
            snbi['Works'] = _remap(raw_works, NESTED_ARRAY_SPECS["Works"]["fields"])

        _apply_fhwa_export_defaults(snbi)
        result.append(snbi)

    return result


def get_available_years(db_path: str, state_code: int) -> List[int]:
    """Return distinct report_years loaded for a state, sorted descending."""
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT report_year
            FROM inspection_report_history
            WHERE state_code = ? AND report_year IS NOT NULL
            ORDER BY report_year DESC
            """,
            (state_code,),
        )
        return [row[0] for row in cur.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def get_table_schema(db_path: str, table_name: str) -> List[Dict]:
    """Return column info for a table via PRAGMA table_info."""
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        # Validate table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        if not cur.fetchone():
            return []
        cur.execute(f"PRAGMA table_info({table_name})")
        cols = cur.fetchall()
        return [
            {"cid": c[0], "name": c[1], "type": c[2], "notnull": bool(c[3]), "default": c[4], "pk": bool(c[5])}
            for c in cols
        ]
    finally:
        conn.close()


def insert_table_row(db_path: str, table_name: str, data: Dict[str, Any]) -> int:
    """Insert a row into ANY table. Returns the new rowid."""
    conn = get_connection(db_path)
    cur = conn.cursor()
    try:
        # Validate table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        if not cur.fetchone():
            raise ValueError(f"Table '{table_name}' does not exist")

        # Only keep columns that actually exist
        cur.execute(f"PRAGMA table_info({table_name})")
        valid_cols = {row[1] for row in cur.fetchall()}
        filtered = {k: v for k, v in data.items() if k in valid_cols}

        if not filtered:
            raise ValueError("No valid columns provided")

        columns = ', '.join(filtered.keys())
        placeholders = ', '.join(['?'] * len(filtered))
        cur.execute(f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders})", list(filtered.values()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_table_row(db_path: str, table_name: str, row_id: int, data: Dict[str, Any]) -> int:
    """Update a row by rowid in any table. Returns number of rows affected."""
    conn = get_connection(db_path)
    cur = conn.cursor()
    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        if not cur.fetchone():
            raise ValueError(f"Table '{table_name}' does not exist")

        cur.execute(f"PRAGMA table_info({table_name})")
        valid_cols = {row[1] for row in cur.fetchall()}
        filtered = {k: v for k, v in data.items() if k in valid_cols and k != 'rowid'}

        if not filtered:
            return 0

        set_clause = ", ".join([f"{col} = ?" for col in filtered.keys()])
        params = list(filtered.values()) + [row_id]
        cur.execute(f"UPDATE {table_name} SET {set_clause} WHERE rowid = ?", params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def delete_table_row(db_path: str, table_name: str, row_id: int) -> int:
    """Delete a row by rowid from any table. Returns number of rows affected."""
    conn = get_connection(db_path)
    cur = conn.cursor()
    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        if not cur.fetchone():
            raise ValueError(f"Table '{table_name}' does not exist")

        cur.execute(f"DELETE FROM {table_name} WHERE rowid = ?", (row_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def clear_all_data(db_path: str) -> Dict[str, int]:
    """Delete all rows from all data tables and reset autoincrement sequences."""
    tables = [
        'works', 'elements', 'span_sets', 'routes', 'features',
        'inspection_report_history', 'bridges',
    ]
    deleted: Dict[str, int] = {}
    conn = get_connection(db_path)
    cur = conn.cursor()
    try:
        cur.execute("PRAGMA foreign_keys = OFF")
        for table in tables:
            cur.execute(f"DELETE FROM {table}")
            deleted[table] = cur.rowcount
            # Reset autoincrement counter
            cur.execute(
                "DELETE FROM sqlite_sequence WHERE name = ?", (table,)
            )
        cur.execute("PRAGMA foreign_keys = ON")
        conn.commit()
    finally:
        conn.close()
    return deleted


def execute_readonly_sql(db_path: str, sql: str) -> Dict:
    """Execute a read-only SQL query. Only SELECT statements are allowed."""
    sql_stripped = sql.strip().upper()
    if not sql_stripped.startswith("SELECT") and not sql_stripped.startswith("PRAGMA") and not sql_stripped.startswith("EXPLAIN"):
        raise ValueError("Only SELECT, PRAGMA, and EXPLAIN statements are allowed")

    # Open the database in read-only mode so writes are refused by the engine
    # itself rather than relying on the statement-prefix check above.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    try:
        df = pd.read_sql_query(sql, conn)
        df = df.where(pd.notna(df), None)
        return {"columns": df.columns.tolist(), "rows": df.to_dict(orient="records"), "count": len(df)}
    finally:
        conn.close()


def update_bridge_main(db_path: str, state_code: int, bridge_number: str, updates: Dict[str, Any]) -> int:
    """Update editable fields on the main bridges table for one bridge."""
    if not updates:
        return 0

    conn = get_connection(db_path)
    cur = conn.cursor()
    try:
        cols_df = pd.read_sql_query("PRAGMA table_info(bridges)", conn)
        bridge_columns = set(cols_df["name"].tolist())

        # Accept both DB column names and SNBI keys (mapped via constants.mapping)
        normalized: Dict[str, Any] = {}
        for key, value in updates.items():
            if key in {"id", "state_code", "bridge_number", "Features", "Elements", "SpanSets", "Works"}:
                continue

            db_col = mapping.get(key, key)
            if db_col in bridge_columns:
                normalized[db_col] = value

        if not normalized:
            return 0

        set_clause = ", ".join([f"{col} = ?" for col in normalized.keys()])
        params = list(normalized.values()) + [state_code, bridge_number]

        cur.execute(
            f"UPDATE bridges SET {set_clause} WHERE state_code = ? AND bridge_number = ?",
            params,
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()