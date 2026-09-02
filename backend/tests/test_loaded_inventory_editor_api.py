from __future__ import annotations

import json
import sqlite3

from app import main
from app.routers.services.database import init_db, load_json_to_db
from app.services.conversion.revision_store import ensure_workspace


def test_editor_reads_and_replaces_one_exact_yearly_inventory_document(tmp_path, monkeypatch):
    database = tmp_path / "inventory.sqlite3"
    init_db(str(database))
    ensure_workspace(str(database), main.SHARED_WORKSPACE_ID, "Shared workspace")
    inserted, existed, skipped, reasons = load_json_to_db(
        str(database),
        [{
            "BL01": 34,
            "BID01": "EDIT-1",
            "BID02": "Original",
            "BC01": "6",
            "BC02": "5",
            "BC03": "7",
            "BC04": "N",
            "Features": [{"BF01": "H01"}],
            "SpanSets": [{"BSP01": "M01"}],
            "_SourceData": {
                "nbi": {"STATE_CODE_001": "34", "STRUCTURE_NUMBER_008": "EDIT-1"}
            },
        }],
        data_year_override=2025,
    )
    assert (inserted, existed, skipped, reasons) == (1, 0, 0, [])
    monkeypatch.setattr(main, "USER_DB", str(database))

    loaded = main.get_loaded_bridge_document(34, "EDIT-1", 2025)
    payload = json.loads(bytes(loaded.body))
    assert payload["snapshot_id"] == "loaded-inventory:34:EDIT-1:2025"
    assert payload["etag"] == loaded.headers["etag"]
    assert payload["canonical_document"]["BID02"] == "Original"
    assert payload["canonical_document"]["BC01"] == "6"
    assert payload["validation"]["summary"]["legacy_bridges"] == 1
    assert not {
        "BCL06-required",
        "BG12-required",
        "BC01-code",
        "BC02-code",
        "BC03-code",
    }.intersection(issue["rule"] for issue in payload["validation"]["issues"])

    updated = dict(payload["canonical_document"])
    updated["BID02"] = "Updated"
    saved = main.replace_loaded_bridge_document(
        34,
        "EDIT-1",
        2025,
        {
            "canonical_document": updated,
            "expected_data_generation": payload["data_generation"],
        },
        if_match=loaded.headers["etag"],
    )
    saved_payload = json.loads(bytes(saved.body))
    assert saved_payload["canonical_document"]["BID02"] == "Updated"
    assert saved_payload["etag"] == saved.headers["etag"]
    assert saved.headers["etag"] != loaded.headers["etag"]

    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT bridge_name FROM bridges WHERE state_code=34 AND bridge_number='EDIT-1' AND data_year=2025"
        ).fetchone()
        generation = connection.execute(
            "SELECT data_generation FROM workspace_state WHERE workspace_id=?",
            (main.SHARED_WORKSPACE_ID,),
        ).fetchone()
    finally:
        connection.close()
    assert row == ("Updated",)
    assert generation == (1,)
