from __future__ import annotations

import csv
import io
import sqlite3

import pytest

from app.services.conversion.revision_store import ensure_workspace, initialize_revision_store
from app.services.conversion.source_artifacts import SourceArtifactStore
from app.services.kpis_v2.program import (
    EXPECTED_HEADERS,
    ProgramKpiError,
    approve_program_source,
    list_program_kpis,
    persist_program_kpis,
    preview_program_kpis,
)


def _csv(rows: list[list[object]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(EXPECTED_HEADERS)
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _database(tmp_path):
    path = tmp_path / "bridgeguard.db"
    initialize_revision_store(str(path), app_revision="test")
    ensure_workspace(str(path), "workspace-a", "Workspace A")
    return path


def test_program_csv_preview_is_strict_and_deterministic():
    data = _csv(
        [
            ["inhouse_bridge_inspections_count", 2026, "actual", "798", "inspections", "NJDOT", "FY2026!H48"],
            ["planned_projects_awarded_percent", 2026, "target", "95.0", "percent", "NJDOT", "FY2026!I51"],
        ]
    )
    first = preview_program_kpis(data, filename="program.csv")
    second = preview_program_kpis(data, filename="program.csv")
    assert first == second
    assert first.ready
    assert len(first.rows) == 2
    assert first.rows[0].value.as_tuple().digits == (7, 9, 8)
    assert first.import_sha256 == second.import_sha256


@pytest.mark.parametrize(
    "row,code",
    [
        (["unknown", 2026, "actual", "1", "bridges", "NJDOT", "Sheet!A1"], "unknown_measure"),
        (["bridges_completed_count", 2026, "actual", "1", "percent", "NJDOT", "Sheet!A1"], "unit_mismatch"),
        (["bridges_completed_count", 2026, "actual", "=1+1", "bridges", "NJDOT", "Sheet!A1"], "invalid_value"),
    ],
)
def test_program_preview_reports_typed_row_errors(row, code):
    preview = preview_program_kpis(_csv([row]), filename="program.csv")
    assert not preview.ready
    assert preview.issues[0].code == code


def test_program_values_require_independent_immutable_approval(tmp_path):
    database = _database(tmp_path)
    data = _csv(
        [
            ["inhouse_bridge_inspections_count", 2026, "actual", "798", "inspections", "NJDOT", "FY2026!H48"],
            ["inhouse_bridge_inspections_count", 2027, "target", "898", "inspections", "NJDOT", "FY2027!M48"],
        ]
    )
    preview = preview_program_kpis(data, filename="program.csv")
    store = SourceArtifactStore(tmp_path / "artifacts", max_bytes=1024 * 1024)
    artifact = store.ingest(data, original_filename="program.csv")

    created = persist_program_kpis(
        database,
        workspace_id="workspace-a",
        preview=preview,
        artifact=artifact,
        actor_id="preparer@example.com",
    )
    assert created["status"] == "pending_review"
    assert list_program_kpis(database, workspace_id="workspace-a")["values"] == []

    with pytest.raises(ProgramKpiError, match="preparer cannot approve"):
        approve_program_source(
            database,
            workspace_id="workspace-a",
            source_id=str(created["source_id"]),
            reviewer_id="preparer@example.com",
            review_note="Checked source values.",
        )

    approved = approve_program_source(
        database,
        workspace_id="workspace-a",
        source_id=str(created["source_id"]),
        reviewer_id="reviewer@example.com",
        review_note="Compared every row with the approved workbook.",
    )
    assert approved["status"] == "approved"
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT data_generation FROM workspace_state WHERE workspace_id='workspace-a'"
        ).fetchone() == (1,)
    finally:
        connection.close()
    report = list_program_kpis(database, workspace_id="workspace-a")
    assert [row["period_year"] for row in report["values"]] == [2026, 2027]
    assert [row["value_decimal"] for row in report["values"]] == ["798", "898"]

    replay = persist_program_kpis(
        database,
        workspace_id="workspace-a",
        preview=preview,
        artifact=artifact,
        actor_id="preparer@example.com",
    )
    assert replay["outcome"] == "noop"
    assert replay["status"] == "approved"
