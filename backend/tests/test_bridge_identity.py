from __future__ import annotations

import sqlite3
from uuid import uuid4

import pytest

from app.routers.services.database import ensure_schema
from app.routers.services.db_schema import LATEST_SCHEMA_VERSION
from app.services.conversion.canonical_json import canonical_sha256
from app.services.bridge_identity import (
    BridgeIdentityStoreUnavailable,
    BridgeSnapshotNotFound,
    InvalidBridgeIdentity,
    resolve_published_bridge_identity,
)


WORKSPACE = "identity-workspace"
OTHER_WORKSPACE = "identity-other"
NOW = "2026-08-13T15:00:00Z"


@pytest.fixture
def identity_database(test_workspace):
    path = test_workspace / "identity.sqlite3"
    ensure_schema(str(path))
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        for workspace in (WORKSPACE, OTHER_WORKSPACE):
            connection.execute(
                "INSERT INTO workspaces (id,slug,display_name,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (workspace, workspace, workspace, "active", NOW, NOW),
            )
            connection.execute(
                "INSERT INTO workspace_state "
                "(workspace_id,data_generation,schema_generation,updated_at) VALUES (?,?,?,?)",
                (workspace, 7, LATEST_SCHEMA_VERSION, NOW),
            )
        for year, revision in ((2024, 1), (2025, 2)):
            revision_id = f"revision-{year}"
            connection.execute(
                "INSERT INTO snapshot_revisions "
                "(id,workspace_id,state_code,bridge_number,data_year,revision_number,"
                "lifecycle_state,canonical_json,canonical_sha256,profile_id,"
                "rule_bundle_sha256,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    revision_id, WORKSPACE, 34, "0102151", year, revision,
                    "staged", "{}", canonical_sha256({}), "research-profile",
                    "f" * 64, "editor@example.com", NOW,
                ),
            )
            connection.execute(
                "INSERT INTO published_snapshots "
                "(id,workspace_id,state_code,bridge_number,data_year,snapshot_revision_id,"
                "published_by,published_at) VALUES (?,?,?,?,?,?,?,?)",
                (str(uuid4()), WORKSPACE, 34, "0102151", year, revision_id, "editor@example.com", NOW),
            )
        connection.commit()
    finally:
        connection.close()
    return path


def test_latest_is_per_bridge_and_contains_revision_generation(identity_database):
    identity = resolve_published_bridge_identity(
        identity_database,
        workspace_id=WORKSPACE,
        state_code=34,
        bridge_number="0102151",
    )
    assert identity.data_year == 2025
    assert identity.snapshot_revision_id == "revision-2025"
    assert identity.revision_number == 2
    assert identity.canonical_sha256 == canonical_sha256({})
    assert identity.data_generation == 7
    assert identity.schema_generation == LATEST_SCHEMA_VERSION
    assert identity.public_payload()["snapshot_status"] == "published"
    assert identity.cache_key[-3:] == ("revision-2025", canonical_sha256({}), 7)


def test_exact_year_never_falls_back(identity_database):
    identity = resolve_published_bridge_identity(
        identity_database,
        workspace_id=WORKSPACE,
        state_code=34,
        bridge_number="0102151",
        data_year=2024,
    )
    assert identity.data_year == 2024
    with pytest.raises(BridgeSnapshotNotFound):
        resolve_published_bridge_identity(
            identity_database,
            workspace_id=WORKSPACE,
            state_code=34,
            bridge_number="0102151",
            data_year=2023,
        )


def test_workspace_isolation_is_non_enumerating(identity_database):
    with pytest.raises(BridgeSnapshotNotFound):
        resolve_published_bridge_identity(
            identity_database,
            workspace_id=OTHER_WORKSPACE,
            state_code=34,
            bridge_number="0102151",
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"workspace_id": "bad workspace"},
        {"state_code": 0},
        {"bridge_number": "../secret"},
        {"data_year": 1899},
    ],
)
def test_invalid_identity_fails_before_database(identity_database, overrides):
    arguments = {
        "workspace_id": WORKSPACE,
        "state_code": 34,
        "bridge_number": "0102151",
        "data_year": 2025,
    }
    arguments.update(overrides)
    with pytest.raises(InvalidBridgeIdentity):
        resolve_published_bridge_identity(identity_database, **arguments)


def test_missing_or_malformed_store_is_typed(test_workspace):
    with pytest.raises(BridgeIdentityStoreUnavailable):
        resolve_published_bridge_identity(
            test_workspace / "missing.sqlite3",
            workspace_id=WORKSPACE,
            state_code=34,
            bridge_number="0102151",
        )
    malformed = test_workspace / "malformed.sqlite3"
    sqlite3.connect(malformed).close()
    with pytest.raises(BridgeIdentityStoreUnavailable):
        resolve_published_bridge_identity(
            malformed,
            workspace_id=WORKSPACE,
            state_code=34,
            bridge_number="0102151",
        )


def test_published_revision_hash_is_verified_before_identity_is_returned(identity_database):
    connection = sqlite3.connect(identity_database)
    try:
        trigger_names = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='snapshot_revisions'"
        ).fetchall()
        for (name,) in trigger_names:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute(
            "UPDATE snapshot_revisions SET canonical_sha256=? WHERE id=?",
            ("a" * 64, "revision-2025"),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(BridgeIdentityStoreUnavailable):
        resolve_published_bridge_identity(
            identity_database,
            workspace_id=WORKSPACE,
            state_code=34,
            bridge_number="0102151",
        )
