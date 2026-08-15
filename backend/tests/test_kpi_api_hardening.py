from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from app.kpi_api import MAX_PROGRAM_KPI_BYTES, router


ROOT = Path(__file__).resolve().parents[2]


def test_program_approval_note_is_a_strict_json_body_not_a_query_parameter():
    app = FastAPI()
    app.include_router(router)
    operation = app.openapi()["paths"][
        "/api/analytics/kpis/program/sources/{source_id}/approve"
    ]["post"]
    assert "requestBody" in operation
    assert [item["name"] for item in operation.get("parameters", [])] == ["source_id"]

    frontend = (ROOT / "frontend/src/pages/Kpis.tsx").read_text(encoding="utf-8")
    assert "{ review_note: reviewNote.trim() }" in frontend
    assert "params: { review_note" not in frontend


def test_program_import_is_bounded_before_multipart_spooling_and_kpi_header_is_exposed():
    main = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")
    assert MAX_PROGRAM_KPI_BYTES == 5 * 1024 * 1024
    assert '"/api/analytics/kpis/program/import": MAX_PROGRAM_KPI_BYTES + (2 * 1024 * 1024)' in main
    assert '"X-BridgeGuard-KPI-Definition"' in main

