import os
import sqlite3
from pathlib import Path

import pytest

from app.routers.services import datapaths


def test_application_paths_use_the_isolated_test_root():
    test_root = Path(os.environ["BRIDGEGUARD_DATA_DIR"]).resolve().parent
    configured_paths = (
        Path(datapaths.DATA_DIR),
        Path(datapaths.USER_DB),
        Path(datapaths.FORECAST_DB),
        Path(datapaths.ATTACHMENTS_DIR),
        Path(datapaths.RAG_WORKING_DIR),
        Path(datapaths.TEMP_DIR),
    )

    assert "backend" in test_root.parts
    assert ".test-artifacts" in test_root.parts
    assert all(
        path.resolve() == test_root or test_root in path.resolve().parents
        for path in configured_paths
    )


def test_sqlite_guard_rejects_the_production_data_tree():
    production_database = (
        Path(__file__).resolve().parents[1] / "data" / "snbi_bridges.db"
    )

    with pytest.raises(AssertionError, match="production data tree"):
        sqlite3.connect(production_database)


def test_workspace_is_outside_the_test_source_tree(test_workspace):
    test_source = Path(__file__).resolve().parent

    assert test_source not in test_workspace.resolve().parents
    probe = test_workspace / "writable.txt"
    probe.write_text("isolated", encoding="utf-8")
    assert probe.read_text(encoding="utf-8") == "isolated"
