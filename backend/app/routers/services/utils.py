# backend/app/routers/services/utils.py

import sys
import os
import math
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Optional

def resource_path(relative_path: str) -> str:
    """
    Get absolute path to resource, works for both dev and PyInstaller bundled exe.
    """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        # Running as normal Python script
        base_path = os.path.abspath(".")
    
    return os.path.join(base_path, relative_path)


# Global paths — centralized under the repo-relative data/ folder (datapaths.py
# creates the directories). Re-exported here so existing `from .utils import
# USER_DB, SAVE_DIR` imports keep working.
from .datapaths import USER_DB, SAVE_DIR  # noqa: E402,F401


# ====================== UTILITY CONVERSION FUNCTIONS ======================

def convert_dms_to_decimal(dms_str: Optional[str]) -> Optional[float]:
    """Convert DMS (degrees minutes seconds) string to decimal degrees."""
    if not dms_str or len(str(dms_str)) < 6:
        return None
    try:
        dms = float(dms_str)
        degrees = int(dms // 1000000)
        minutes = int((dms % 1000000) // 10000)
        seconds = (dms % 10000) / 100
        decimal = degrees + minutes / 60 + seconds / 3600
        return round(decimal, 6)
    except (ValueError, TypeError):
        return None


def meters_to_feet(meters_str: Optional[Any], round_down: bool = False, cap99: bool = False) -> Optional[float]:
    """Convert meters to feet with optional rounding and capping."""
    if not meters_str:
        return None
    try:
        m = float(meters_str)
        if cap99 and m >= 30:
            return 99.9
        ft = m / 0.3048
        if round_down:
            ft = math.floor(ft * 10) / 10
        else:
            ft = round(ft, 1)
        return ft
    except (ValueError, TypeError):
        return None


def km_to_mile(km_str: Optional[Any]) -> Optional[float]:
    """Convert kilometers to miles."""
    if not km_str:
        return None
    try:
        km = float(km_str) / 1000
        mile = km / 1.60934
        return round(mile, 3)
    except (ValueError, TypeError):
        return None


def get_rating_factor(old: dict, rating_key: str, meth_key: str, is_legacy: bool = True) -> Optional[float]:
    """Calculate load rating factor from NBI values."""
    meth = old.get(meth_key, '')
    try:
        rating = float(old.get(rating_key, 0))
    except (ValueError, TypeError):
        return None

    if meth in ['6', '7', '8', 'D', 'E', 'F']:
        div = 32.4 if is_legacy else 1
    else:
        div = 32.67
    return round(rating / div, 2)


def determine_primary_under(unders: list) -> tuple:
    """Determine primary under record and secondary ones."""
    primary = None
    secondaries = []

    for u in unders:
        if not isinstance(u, dict):
            continue
        rec_type = u.get('RECORD_TYPE_005A')
        if rec_type == '2':
            primary = u
        elif rec_type == 'A':
            if primary is None:
                primary = u
            else:
                secondaries.append(u)
        else:
            secondaries.append(u)

    secondaries.sort(key=lambda x: x.get('RECORD_TYPE_005A', ''))
    return primary, secondaries


# ====================== SAFE CONVERSION HELPERS ======================

def safe_str(value: Any, default: str = '') -> str:
    """Safely convert value to stripped string."""
    return str(value or default).strip()


def safe_int(value: Any, default: int = 0) -> int:
    """Safely convert value to integer."""
    try:
        return int(float(value or default))
    except (ValueError, TypeError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    """Safely convert value to float."""
    try:
        return float(value or default)
    except (ValueError, TypeError):
        return default


# ====================== DATABASE HELPER ======================

def safe_insert(cursor, table: str, data: dict):
    """Safely insert data into SQLite table with error handling."""
    if not data:
        return
    try:
        columns = ', '.join(data.keys())
        placeholders = ', '.join(['?'] * len(data))
        values = list(data.values())
        cursor.execute(f"INSERT OR IGNORE INTO {table} ({columns}) VALUES ({placeholders})", values)
    except sqlite3.Error as e:
        print(f"Error inserting into {table}: {e}")
        print(f"Data keys: {list(data.keys())}")


# ====================== INITIAL DATABASE SETUP ======================

def init_db_if_needed():
    """Copy initial database to user folder if it doesn't exist."""
    if not os.path.exists(USER_DB):
        bundled_db = resource_path("bridges.db")
        if os.path.exists(bundled_db):
            shutil.copy(bundled_db, USER_DB)
            print(f"Copied initial database to {USER_DB}")
        else:
            print("Warning: bridges.db not found in bundled resources.")