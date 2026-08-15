"""Canonical archive/unpublish lifecycle contracts.

The archive mutation removes only the active publication pointer and its
rebuildable relational projection.  Immutable revisions/evidence and
retention-owned records must survive, while optimistic and authorization
boundaries remain exact.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import revision_api
from app.auth import AuthMiddleware, AuthUser, COOKIE_NAME, issue_session
from app.revision_api import configure_database_path, router
from app.routers.services.database import ensure_schema
from app.services.conversion.revision_store import (
    ArchivePublishedSnapshotRequest,
    RevisionNotFoundError,
    archive_published_snapshot,
    ensure_workspace,
)


WORKSPACE = "shared-research-workspace"
STATE = 34
BRIDGE = "ARCHIVE-0001"
YEAR = 2026

AUTH_ENV = {
    "BRIDGEGUARD_AUTH_DISABLED": "0",
    "BRIDGEGUARD_AUTH_MODE": "password",
    "BRIDGEGUARD_SESSION_SECRET": "archive-contract-secret-longer-than-32-characters",
    "BRIDGEGUARD_PASSWORD": "archive-contract-password",
    "BRIDGEGUARD_ALLOWED_EMAILS": "viewer@example.com",
    "BRIDGEGUARD_EDITOR_EMAILS": "editor@example.com",
    "BRIDGEGUARD_ADMIN_EMAILS": "admin@example.com",
    "BRIDGEGUARD_ALLOWED_DOMAINS": "",
    "BRIDGEGUARD_OWNER_EMAIL": "",
}
ROLE_EMAIL = {
    "viewer": "viewer@example.com",
    "editor": "editor@example.com",
    "admin": "admin@example.com",
}


def _document(name: str = "Archive lifecycle bridge") -> dict:
    return {
        "BL01": STATE,
        "BID01": BRIDGE,
        "BID02": name,
        "BL03": "00123",
        "BW01": 1985,
        "Elements": [],
        "Features": [
            {
                "BF01": "H01",
                "BF02": "1",
                "Routes": [
                    {"BRT01": "C01", "BRT02": "0001", "BRT03": "N"}
                ],
            }
        ],
        "Inspections": [],
        "PostingEvaluations": [],
        "PostingStatuses": [],
        "SpanSets": [],
        "SubstructureSets": [],
        "Works": [],
    }


@pytest.fixture
def archive_harness(test_workspace: Path, monkeypatch: pytest.MonkeyPatch):
    for key, value in AUTH_ENV.items():
        monkeypatch.setenv(key, value)
    database_path = str(test_workspace / "revision-archive.db")
    ensure_schema(database_path)
    configure_database_path(lambda: database_path)
    monkeypatch.setattr(
        revision_api,
        "_server_validation",
        lambda document, *, profile_id, data_year, source_records: {
            "profile_id": profile_id,
            "status": "passed",
            "validation_owner": "server",
            "official_validation": False,
            "official_validation_status": "unavailable",
            "issues": [],
        },
    )
    focused = FastAPI()
    focused.include_router(router)
    focused.add_middleware(AuthMiddleware)
    try:
        with TestClient(focused) as client:
            yield client, database_path
    finally:
        configure_database_path(None)


def _headers(role: str, **extra: str) -> dict[str, str]:
    token = issue_session(AuthUser(email=ROLE_EMAIL[role], role=role))
    return {"Cookie": f"{COOKIE_NAME}={token}", **extra}


def _stage(client: TestClient, *, key: str, name: str = "Archive lifecycle bridge"):
    return client.post(
        "/api/revisions/stage",
        headers=_headers(
            "editor", **{"Idempotency-Key": key, "X-Request-ID": f"request-{key}"}
        ),
        json={
            "profile_id": "research_historical_import",
            "atomic_mode": "batch",
            "expected_data_generation": 0,
            "data_year": YEAR,
            "bridges": [
                {
                    "canonical_document": _document(name),
                    "source_records": [],
                    "lineage": [],
                }
            ],
        },
    )


def _publish(
    client: TestClient,
    revision_id: str,
    *,
    key: str,
    generation: int,
    if_match: str,
    role: str = "editor",
):
    return client.post(
        f"/api/revisions/{revision_id}/publish",
        headers=_headers(
            role,
            **{
                "Idempotency-Key": key,
                "If-Match": if_match,
                "X-Request-ID": f"request-{key}",
            },
        ),
        json={
            "reason": "Reviewed archive contract publication",
            "expected_data_generation": generation,
            "reviewed_research_warnings": False,
        },
    )


def _archive(
    client: TestClient,
    revision_id: str,
    *,
    key: str,
    generation: int,
    if_match: str,
    role: str = "editor",
    reason: str = "Superseded inventory archived after review",
    year: int = YEAR,
):
    return client.post(
        f"/api/revisions/snapshots/{STATE}/{BRIDGE}/{year}/{revision_id}/archive",
        headers=_headers(
            role,
            **{
                "Idempotency-Key": key,
                "If-Match": if_match,
                "X-Request-ID": f"request-{key}",
            },
        ),
        json={"reason": reason, "expected_data_generation": generation},
    )


def _error_code(response) -> str | None:
    detail = response.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


def _scalar(database_path: str, sql: str, parameters: tuple = ()) -> int:
    connection = sqlite3.connect(database_path)
    try:
        return int(connection.execute(sql, parameters).fetchone()[0])
    finally:
        connection.close()


def test_archive_is_audited_idempotent_reversible_and_retains_evidence(
    archive_harness,
):
    client, database_path = archive_harness
    staged = _stage(client, key="archive-stage")
    assert staged.status_code == 201, staged.text
    revision_id = staged.json()["revisions"][0]["revision_id"]
    published = _publish(
        client,
        revision_id,
        key="archive-publish",
        generation=0,
        if_match='"none"',
    )
    assert published.status_code == 200, published.text
    published_etag = published.headers["etag"]

    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "INSERT INTO inspection_report_history "
            "(state_code, bridge_number, report_file, report_year) VALUES (?, ?, ?, ?)",
            (STATE, BRIDGE, "retained-report.pdf", YEAR),
        )
        connection.execute(
            "CREATE TABLE bridge_attachments ("
            "id INTEGER PRIMARY KEY, state_code TEXT NOT NULL, bridge_number TEXT NOT NULL, "
            "attachment_type TEXT NOT NULL, filename TEXT NOT NULL, filepath TEXT NOT NULL, "
            "size_bytes INTEGER, sha256 TEXT, workspace_id TEXT)"
        )
        connection.execute(
            "INSERT INTO bridge_attachments "
            "(state_code, bridge_number, attachment_type, filename, filepath, workspace_id) "
            "VALUES (?, ?, 'point_cloud', 'cloud.ply', 'point_clouds/cloud.ply', ?)",
            (str(STATE), BRIDGE, WORKSPACE),
        )
        connection.commit()
        immutable_before = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in (
                "snapshot_revisions",
                "source_artifacts",
                "source_records",
                "snapshot_revision_sources",
                "field_lineage",
                "validation_runs",
                "validation_issues",
            )
        }
    finally:
        connection.close()

    archived = _archive(
        client,
        revision_id,
        key="archive-once",
        generation=1,
        if_match=published_etag,
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived"
    assert archived.json()["revision_id"] == revision_id
    assert archived.json()["data_generation"] == 2
    assert archived.headers["x-bridgeguard-data-version"] == "2"
    assert archived.headers["etag"] == published_etag

    assert _scalar(database_path, "SELECT COUNT(*) FROM published_snapshots") == 0
    assert _scalar(database_path, "SELECT COUNT(*) FROM bridges") == 0
    assert _scalar(database_path, "SELECT COUNT(*) FROM features") == 0
    assert _scalar(database_path, "SELECT COUNT(*) FROM routes") == 0
    assert _scalar(database_path, "SELECT COUNT(*) FROM inspection_report_history") == 1
    assert _scalar(database_path, "SELECT COUNT(*) FROM bridge_attachments") == 1
    for table, expected in immutable_before.items():
        assert _scalar(database_path, f'SELECT COUNT(*) FROM "{table}"') == expected

    missing = client.get(
        f"/api/revisions/snapshots/{STATE}/{BRIDGE}/{YEAR}",
        headers=_headers("viewer"),
    )
    assert missing.status_code == 404
    assert missing.headers["x-bridgeguard-data-version"] == "2"
    history = client.get(
        f"/api/revisions/snapshots/{STATE}/{BRIDGE}/{YEAR}/history",
        headers=_headers("viewer"),
    )
    assert history.status_code == 200
    assert [item["revision_id"] for item in history.json()["revisions"]] == [revision_id]
    assert history.json()["revisions"][0]["status"] == "archived"

    replay = _archive(
        client,
        revision_id,
        key="archive-once",
        generation=1,
        if_match=published_etag,
    )
    assert replay.status_code == 200
    assert replay.json() == archived.json()
    assert replay.headers["etag"] == archived.headers["etag"]
    assert _scalar(
        database_path,
        "SELECT COUNT(*) FROM mutation_audit WHERE action='archive_revision'",
    ) == 1
    assert _scalar(
        database_path,
        "SELECT data_generation FROM workspace_state WHERE workspace_id=?",
        (WORKSPACE,),
    ) == 2

    changed_replay = _archive(
        client,
        revision_id,
        key="archive-once",
        generation=1,
        if_match=published_etag,
        reason="A different reason must conflict",
    )
    assert changed_replay.status_code == 409
    assert _error_code(changed_replay) == "idempotency_payload_conflict"

    audit_connection = sqlite3.connect(database_path)
    audit_connection.row_factory = sqlite3.Row
    try:
        audit = audit_connection.execute(
            "SELECT * FROM mutation_audit WHERE action='archive_revision'"
        ).fetchone()
        metadata = json.loads(audit["metadata_json"])
        assert audit["actor_id"] == "editor@example.com"
        assert audit["reason"] == "Superseded inventory archived after review"
        assert audit["before_sha256"] == published_etag.strip('"')
        assert audit["after_sha256"] is None
        assert "snapshot_revision" in metadata["retained"]
        assert "point_clouds" in metadata["retained"]
    finally:
        audit_connection.close()

    restored = _publish(
        client,
        revision_id,
        key="archive-restore",
        generation=2,
        if_match='"none"',
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["data_generation"] == 3
    restored_history = client.get(
        f"/api/revisions/snapshots/{STATE}/{BRIDGE}/{YEAR}/history",
        headers=_headers("viewer"),
    )
    assert restored_history.json()["revisions"][0]["status"] == "published"
    assert _scalar(database_path, "SELECT COUNT(*) FROM bridges") == 1
    assert _scalar(database_path, "SELECT COUNT(*) FROM bridge_attachments") == 1
    assert _scalar(database_path, "SELECT COUNT(*) FROM inspection_report_history") == 1


def test_archive_rejects_viewers_missing_or_stale_preconditions_and_wrong_scope(
    archive_harness,
):
    client, database_path = archive_harness
    first = _stage(client, key="archive-boundary-first")
    second = _stage(
        client,
        key="archive-boundary-second",
        name="Unpublished candidate revision",
    )
    assert first.status_code == 201 and second.status_code == 201
    first_id = first.json()["revisions"][0]["revision_id"]
    second_id = second.json()["revisions"][0]["revision_id"]
    published = _publish(
        client,
        first_id,
        key="archive-boundary-publish",
        generation=0,
        if_match='"none"',
    )
    assert published.status_code == 200, published.text
    etag = published.headers["etag"]

    viewer = _archive(
        client,
        first_id,
        key="archive-viewer",
        generation=1,
        if_match=etag,
        role="viewer",
    )
    assert viewer.status_code == 403
    missing = client.post(
        f"/api/revisions/snapshots/{STATE}/{BRIDGE}/{YEAR}/{first_id}/archive",
        headers=_headers("editor"),
        json={"reason": "Missing headers", "expected_data_generation": 1},
    )
    assert missing.status_code == 428
    assert _error_code(missing) == "precondition_required"

    unpublished = _archive(
        client,
        second_id,
        key="archive-unpublished",
        generation=1,
        if_match=etag,
    )
    assert unpublished.status_code == 409
    assert _error_code(unpublished) == "stale_revision"
    wrong_year = _archive(
        client,
        first_id,
        key="archive-wrong-year",
        generation=1,
        if_match=etag,
        year=YEAR - 1,
    )
    assert wrong_year.status_code == 404
    assert _error_code(wrong_year) == "revision_not_found"
    stale_etag = _archive(
        client,
        first_id,
        key="archive-stale-etag",
        generation=1,
        if_match='"not-the-current-hash"',
    )
    assert stale_etag.status_code == 409
    assert _error_code(stale_etag) == "stale_revision"
    stale_generation = _archive(
        client,
        first_id,
        key="archive-stale-generation",
        generation=0,
        if_match=etag,
    )
    assert stale_generation.status_code == 409
    assert _error_code(stale_generation) == "stale_data_generation"

    ensure_workspace(database_path, "another-workspace", "Another workspace")
    with pytest.raises(RevisionNotFoundError) as cross_workspace:
        archive_published_snapshot(
            database_path,
            ArchivePublishedSnapshotRequest(
                workspace_id="another-workspace",
                state_code=STATE,
                bridge_number=BRIDGE,
                data_year=YEAR,
                revision_id=first_id,
                actor_id="admin@example.com",
                request_id="cross-workspace-attempt",
                reason="Must not cross workspace",
                expected_data_generation=0,
            ),
        )
    assert cross_workspace.value.code == "revision_not_found"

    admin = _archive(
        client,
        first_id,
        key="archive-admin",
        generation=1,
        if_match=etag,
        role="admin",
    )
    assert admin.status_code == 200, admin.text
    assert admin.json()["data_generation"] == 2
    repeated_with_new_key = _archive(
        client,
        first_id,
        key="archive-after-unpublish",
        generation=2,
        if_match=etag,
        role="admin",
    )
    assert repeated_with_new_key.status_code == 409
    assert _error_code(repeated_with_new_key) == "snapshot_not_published"
