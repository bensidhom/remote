from __future__ import annotations

from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main as main_module
from app.auth import AuthMiddleware
from app.services.operations.retention import DeletionReceipt


def _route_app() -> FastAPI:
    app = FastAPI()
    app.router.routes.extend(
        route
        for route in main_module.app.router.routes
        if getattr(route, "path", "") in {
            "/api/bridges/{state_code}/{bridge_number}/reports/{report_id}",
            "/api/bridges/{state_code}/{bridge_number}/reports/{report_id}/retention",
        }
    )
    app.add_middleware(AuthMiddleware)
    return app


def _receipt(status: str = "complete") -> DeletionReceipt:
    return DeletionReceipt(
        id="deletion-1", workspace_id="workspace-a", attachment_id=1,
        attachment_type="inspection_report", source_sha256="a" * 64,
        status=status, requested_at="2026-08-13T12:00:00Z",
        source_deleted_at="2026-08-13T12:00:00Z" if status == "complete" else None,
        cache_purged_at="2026-08-13T12:00:00Z" if status == "complete" else None,
        attempts=1, last_error_code=None,
    )


def test_report_delete_requires_authenticated_actor_and_reason(monkeypatch, tmp_path: Path):
    app = _route_app()
    monkeypatch.setenv("BRIDGEGUARD_AUTH_DISABLED", "true")
    monkeypatch.setattr(main_module, "USER_DB", str(tmp_path / "bridge.db"))
    monkeypatch.setattr(main_module, "SHARED_WORKSPACE_ID", "workspace-a")
    with patch.object(main_module, "request_attachment_deletion", return_value=_receipt()) as requested, patch.object(
        main_module, "process_attachment_deletion_outbox", return_value=(_receipt(),)
    ):
        with TestClient(app) as client:
            assert client.delete("/api/bridges/34/0001/reports/1").status_code == 422
            response = client.delete(
                "/api/bridges/34/0001/reports/1", params={"reason": "duplicate"}
            )
        assert response.status_code == 200
        assert response.json()["deletion"]["status"] == "complete"
        assert "path" not in str(response.json()).lower()
        assert requested.call_args.kwargs["actor_id"] == "local@bridgeguard"
        assert requested.call_args.kwargs["reason"] == "duplicate"


def test_report_retention_update_is_audited_and_scoped(monkeypatch, tmp_path: Path):
    database = tmp_path / "bridge.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE bridge_attachments (id INTEGER,workspace_id TEXT,state_code TEXT,"
            "bridge_number TEXT,attachment_type TEXT)"
        )
        connection.execute(
            "INSERT INTO bridge_attachments VALUES (1,'workspace-a','34','0001','inspection_report')"
        )
        connection.commit()
    finally:
        connection.close()
    app = _route_app()
    monkeypatch.setenv("BRIDGEGUARD_AUTH_DISABLED", "true")
    monkeypatch.setattr(main_module, "USER_DB", str(database))
    monkeypatch.setattr(main_module, "SHARED_WORKSPACE_ID", "workspace-a")
    policy = SimpleNamespace(public_dict=lambda: {
        "resource_type": "attachment", "resource_id": "1", "legal_hold": True,
        "retain_until": None,
    })
    with patch.object(main_module, "set_retention_policy", return_value=policy) as updated:
        with TestClient(app) as client:
            response = client.put(
                "/api/bridges/34/0001/reports/1/retention",
                params={"reason": "litigation", "legal_hold": "true"},
            )
        assert response.status_code == 200
        assert updated.call_args.kwargs["actor_id"] == "local@bridgeguard"
        assert updated.call_args.kwargs["reason"] == "litigation"
        assert updated.call_args.kwargs["legal_hold"] is True
