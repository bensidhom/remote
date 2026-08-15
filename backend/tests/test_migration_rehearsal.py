from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
from pathlib import Path
import sqlite3

import pytest

from app.routers.services.database import init_db
from app.routers.services.db_schema import LATEST_SCHEMA_VERSION
from app.services.operations.migration_rehearsal import (
    DEFAULT_TABLES,
    MigrationRehearsalError,
    run_migration_rehearsal,
)


def _legacy(path: Path) -> Path:
    init_db(str(path), journal_mode_override="delete")
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO bridges "
            "(state_code,bridge_number,data_year,bridge_name,place_code) "
            "VALUES (34,'A-001',2025,'Alpha','00123')"
        )
        connection.execute(
            "INSERT INTO features "
            "(state_code,bridge_number,data_year,feature_type,feature_location) "
            "VALUES (34,'A-001',2025,'H01','C')"
        )
        connection.commit()
    finally:
        connection.close()
    return path


def test_rehearsal_is_read_only_source_and_preserves_projection(test_workspace: Path):
    source = _legacy(test_workspace / "source.sqlite")
    shadow = test_workspace / "shadow.sqlite"
    source_before = source.read_bytes()

    receipt = run_migration_rehearsal(source, shadow, app_revision="test")

    assert source.read_bytes() == source_before
    assert receipt.source_database_sha256 == hashlib.sha256(source_before).hexdigest()
    assert receipt.source_user_version == LATEST_SCHEMA_VERSION
    assert receipt.shadow_user_version == LATEST_SCHEMA_VERSION
    assert receipt.target_schema_version == LATEST_SCHEMA_VERSION
    assert receipt.preserved is True
    assert receipt.schema_drift == ()
    assert receipt.migration_applied_versions == ()
    assert tuple(item.table for item in receipt.table_receipts) == DEFAULT_TABLES
    assert all(item.preserved for item in receipt.table_receipts)
    assert shadow.is_file()
    with pytest.raises(FrozenInstanceError):
        receipt.shadow_user_version = 0  # type: ignore[misc]


def test_existing_shadow_and_same_path_fail_before_source_mutation(test_workspace: Path):
    source = _legacy(test_workspace / "source.sqlite")
    before = source.read_bytes()
    with pytest.raises(MigrationRehearsalError) as collision:
        run_migration_rehearsal(source, source)
    assert collision.value.code == "path_collision"

    shadow = test_workspace / "shadow.sqlite"
    shadow.write_bytes(b"do-not-overwrite")
    with pytest.raises(MigrationRehearsalError) as existing:
        run_migration_rehearsal(source, shadow)
    assert existing.value.code == "shadow_exists"
    assert shadow.read_bytes() == b"do-not-overwrite"
    assert source.read_bytes() == before


def test_failure_removes_only_disposable_shadow(test_workspace: Path):
    source = _legacy(test_workspace / "source.sqlite")
    connection = sqlite3.connect(source)
    try:
        connection.execute("DROP TABLE routes")
        connection.commit()
    finally:
        connection.close()
    shadow = test_workspace / "shadow.sqlite"
    with pytest.raises(MigrationRehearsalError) as raised:
        run_migration_rehearsal(source, shadow)
    assert raised.value.code == "projection_schema_unsupported"
    assert not shadow.exists()


def test_large_source_hash_is_streamed(test_workspace: Path, monkeypatch):
    source = _legacy(test_workspace / "source.sqlite")
    shadow = test_workspace / "shadow.sqlite"

    def forbidden_read_bytes(_self):
        raise AssertionError("production rehearsal must stream database bytes")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)
    receipt = run_migration_rehearsal(source, shadow)
    assert receipt.preserved is True
