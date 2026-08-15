from __future__ import annotations

import csv
import io
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import reverse_convert


def test_public_reverse_endpoint_exposes_labeled_projection_and_loss_manifest():
    payload = [
        {
            "BL01": 34,
            "BID01": "API-REVERSE-1",
            "BL08": "36",
            "BL12": "Example MPO",
            "Features": [
                {
                    "BF01": "H01",
                    "BF02": "C",
                    "BH06": "ABCDEFGHIJ12",
                }
            ],
            "Inspections": [
                {
                    "BIE01": "1",
                    "BIE02": "20260313",
                    "BIE11": "Review note only",
                }
            ],
        }
    ]
    http_app = FastAPI()
    http_app.post("/api/reverse-convert")(reverse_convert)

    with TestClient(http_app) as client:
        response = client.post(
            "/api/reverse-convert",
            files={
                "file": (
                    "bridge.json",
                    json.dumps(payload).encode("utf-8"),
                    "application/json",
                )
            },
        )

    assert response.status_code == 200
    body = response.json()
    # Original response keys remain available to existing clients.
    assert {
        "status",
        "flat_json",
        "text",
        "nbi_csv",
        "elements_xml",
        "count",
        "warnings",
    } <= body.keys()
    metadata = body["legacy_projection"]
    assert metadata["kind"] == "legacy_projection_for_review"
    assert metadata["media_type"] == "text/csv"
    assert metadata["suggested_extension"] == ".csv"
    assert metadata["official_nbi_445"] is False
    assert "review" in metadata["label"].lower()

    rows = list(csv.DictReader(io.StringIO(body["nbi_csv"])))
    assert len(rows) == 1
    assert rows[0]["RECORD_TYPE_005A"] == "1"
    assert rows[0]["BASE_HWY_NETWORK_012"] == ""
    assert rows[0]["WORK_PROPOSED_075A"] == ""

    manifest = body["loss_manifest"]
    assert manifest["exact_reverse"] is False
    assert manifest["row_policy"] == (
        "exactly_one_synthetic_record_type_1_row_per_snbi_bridge"
    )
    paths = {
        outcome["source_path"]
        for outcome in manifest["bridges"][0]["outcomes"]
    }
    assert "/BL12" in paths
    assert "/Inspections/0/BIE11" in paths

