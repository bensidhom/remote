from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile

from pypdf import PdfReader
import pytest

from app.services.kpis_v2.export import EXPORT_FORMAT, build_kpi_export


@pytest.mark.parametrize(
    "export_kind",
    [
        "operational",
        "acceptable_condition",
        "age_condition_area",
        "age_condition_counts",
        "program",
        "fhwa_tpm",
        "future_condition",
    ],
)
def test_kpi_export_accepts_every_kind_exposed_by_the_dashboard(export_kind):
    export_id, payload, manifest = build_kpi_export(
        {
            "year": 2026,
            "definition_version": "definition-v1",
            "data_generation": 7,
            "revision_set_sha256": "a" * 64,
        },
        export_kind=export_kind,
    )

    assert export_id == hashlib.sha256(payload).hexdigest()
    assert manifest["export_kind"] == export_kind


def test_kpi_export_is_deterministic_reproducible_and_spreadsheet_safe():
    report = {
        "kind": "observed_published_inventory",
        "year": 2026,
        "definition_version": "definition-v1",
        "data_generation": 7,
        "revision_set_sha256": "a" * 64,
        "values": {"count": 5, "hostile": "  =SUM(A1:A2)", "missing": None},
        "exclusions": {"missing": 2},
    }
    first_id, first, first_manifest = build_kpi_export(report, export_kind="operational")
    second_id, second, second_manifest = build_kpi_export(report, export_kind="operational")
    assert first == second
    assert first_id == second_id == hashlib.sha256(first).hexdigest()
    assert first_manifest == second_manifest

    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert archive.namelist() == ["manifest.json", "report.json", "review.pdf", "values.csv"]
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format"] == EXPORT_FORMAT
        assert manifest["revision_set_sha256"] == "a" * 64
        for name, receipt in manifest["artifacts"].items():
            content = archive.read(name)
            assert hashlib.sha256(content).hexdigest() == receipt["sha256"]
            assert len(content) == receipt["size_bytes"]
        csv_rows = list(csv.reader(io.StringIO(archive.read("values.csv").decode("utf-8"))))
        hostile = next(row for row in csv_rows if row[0] == "/values/hostile")
        assert hostile[1] == "'  =SUM(A1:A2)"
        pdf = PdfReader(io.BytesIO(archive.read("review.pdf")))
        assert len(pdf.pages) >= 1
        assert "BridgeGuard KPI report" in (pdf.pages[0].extract_text() or "")
