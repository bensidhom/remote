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
    # Program Delivery is intentionally absent from the SNBI-derived KPI UI.
    # Keep the server-side body contract above, but do not require a removed
    # approval form or its request payload in the current dashboard.
    assert "review_note: reviewNote.trim()" not in frontend
    assert "/api/analytics/kpis/program/sources/" not in frontend
    assert "params: { review_note" not in frontend


def test_program_import_is_bounded_before_multipart_spooling_and_kpi_header_is_exposed():
    main = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")
    assert MAX_PROGRAM_KPI_BYTES == 5 * 1024 * 1024
    assert '"/api/analytics/kpis/program/import": MAX_PROGRAM_KPI_BYTES + (2 * 1024 * 1024)' in main
    assert '"X-BridgeGuard-KPI-Definition"' in main


def test_age_kpi_ui_offers_count_and_deck_area_from_matching_endpoints():
    frontend = (ROOT / "frontend/src/pages/Kpis.tsx").read_text(encoding="utf-8")

    assert "'/api/analytics/kpis/age-condition-counts'" in frontend
    assert "'/api/analytics/kpis/age-condition-area'" in frontend
    assert "include_unknown: false" in frontend
    assert "ageMetric === 'deck_area' ? 'age_condition_area' : 'age_condition_counts'" in frontend
    assert ">Bridge count</button>" in frontend
    assert ">Deck area (ft²)</button>" in frontend
