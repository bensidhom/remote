from __future__ import annotations

import asyncio
import io
import json

import pytest
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.testclient import TestClient

from app import main as main_module
from app.main import convert_inspection_history_v2, convert_v2
from app.services.conversion.inspection_history import (
    HISTORY_MANIFEST_VERSION,
    InspectionHistoryError,
    SUPPORTED_RULE_BUNDLE_ID,
    parse_inspection_history_manifest,
)


def _fixed_record(
    *,
    structure: str = "HISTORY-BRIDGE",
    inspection_date: str = "1221",
    interval: str = "12",
) -> bytes:
    record = bytearray(b" " * 445)
    record[0:3] = b"342"
    record[3:18] = structure.encode("ascii").ljust(15, b" ")
    record[18:19] = b"1"
    record[156:160] = b"1999"
    record[199:200] = b"1"
    record[222:228] = b" 010.0"
    record[286:290] = inspection_date.encode("ascii")
    record[290:292] = interval.encode("ascii")
    record[373:374] = b"Y"
    return bytes(record)


def _manifest(
    sources: list[tuple[str, int]],
    *,
    target_year: int | None = None,
) -> str:
    years = [year for _, year in sources]
    return json.dumps(
        {
            "manifest_version": HISTORY_MANIFEST_VERSION,
            "source_agency_context": "NJDOT",
            "encoding": "ascii",
            "input_format": "fhwa_fixed_445",
            "issue_policy": "reject",
            "bid01_policy": "preserve",
            "rule_bundle_id": SUPPORTED_RULE_BUNDLE_ID,
            "history_start_year": min(years),
            "history_end_year": max(years),
            "target_inventory_year": target_year or max(years),
            "annual_sources": [
                {"part_name": name, "inventory_year": year}
                for name, year in sources
            ],
        },
        separators=(",", ":"),
    )


def _upload(name: str, body: bytes) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(body))


def _run(manifest: str, files: list[UploadFile]):
    return asyncio.run(
        convert_inspection_history_v2(manifest=manifest, annual_files=files)
    )


def test_multi_year_endpoint_attaches_reducer_before_canonical_validation(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(main_module, "DATA_DIR", str(tmp_path))
    # Identical source bytes intentionally prove that explicit year context is
    # part of record lineage rather than being collapsed by parser IDs or SHA.
    source = _fixed_record()
    manifest = _manifest([("annual-2021.txt", 2021), ("annual-2022.txt", 2022)])

    result = _run(
        manifest,
        [
            _upload("annual-2022.txt", source),
            _upload("annual-2021.txt", source),
        ],
    )

    assert result["status"] == "success"
    assert result["submission_ready"] is False
    assert result["inspection_history"]["history_ready"] is True
    assert result["compatibility"]["inspection_history_attached"] is True
    assert result["converted"] == 1

    bridge = result["bridges"][0]
    assert bridge["Inspections"] == [
        {"BIE01": "1", "BIE02": "20211201", "BIE05": 12}
    ]
    assert "/Inspections" not in bridge["_TransitionReview"]
    assert not any(
        issue["code"] == "inspection_annual_history_required"
        for issue in bridge["_TransitionIssues"]
    )
    date_lineage = bridge["_TransitionLineage"]["/Inspections/0/BIE02"]
    assert date_lineage["status"] == "generated"
    assert len(date_lineage["source_record_ids"]) == 2
    assert len(set(date_lineage["source_record_ids"])) == 2
    assert "day 01" in date_lineage["messages"][0]
    assert set(bridge["_SourceData"]["inspection_history"]["source_records"]) == set(
        date_lineage["source_record_ids"]
    )

    evidence = result["inspection_history"]
    bridge_evidence = evidence["bridges"][0]
    assert bridge_evidence["events"][0]["status"] == "deduplicated"
    assert bridge_evidence["lineage"]["/Inspections/0/BIE02"][
        "proxy_components"
    ] == ["day"]
    record_years = {
        evidence["source_records"][source_id]["inventory_year"]
        for source_id in date_lineage["source_record_ids"]
    }
    assert record_years == {2021, 2022}
    assert evidence["coverage"]["not_invented_tags"] == [
        "BIE03",
        "BIE04",
        "BIE07",
    ]


def test_multipart_http_contract_accepts_repeated_annual_file_parts(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(main_module, "DATA_DIR", str(tmp_path))
    http_app = FastAPI()
    http_app.post("/api/convert-v2/inspection-history")(
        convert_inspection_history_v2
    )
    manifest = _manifest([("annual-2021.txt", 2021), ("annual-2022.txt", 2022)])
    source = _fixed_record()
    with TestClient(http_app) as client:
        response = client.post(
            "/api/convert-v2/inspection-history",
            data={"manifest": manifest},
            files=[
                ("annual_files", ("annual-2022.txt", source, "text/plain")),
                ("annual_files", ("annual-2021.txt", source, "text/plain")),
            ],
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["inspection_history"]["history_ready"] is True
    assert payload["bridges"][0]["Inspections"][0]["BIE01"] == "1"


def test_distinct_annual_dates_attach_initial_then_routine_in_year_order(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(main_module, "DATA_DIR", str(tmp_path))
    result = _run(
        _manifest([("annual-2021.txt", 2021), ("annual-2022.txt", 2022)]),
        [
            _upload("annual-2022.txt", _fixed_record(inspection_date="1122")),
            _upload("annual-2021.txt", _fixed_record(inspection_date="1220")),
        ],
    )

    assert result["bridges"][0]["Inspections"] == [
        {"BIE01": "1", "BIE02": "20201201", "BIE05": 12},
        {"BIE01": "2", "BIE02": "20221101", "BIE05": 12},
    ]


def test_target_bridge_with_only_one_annual_occurrence_blocks_conversion(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(main_module, "DATA_DIR", str(tmp_path))
    old = _fixed_record(structure="OLDER")
    target = b"\n".join(
        [_fixed_record(structure="OLDER"), _fixed_record(structure="NEW-BRIDGE")]
    )

    result = _run(
        _manifest([("annual-2021.txt", 2021), ("annual-2022.txt", 2022)]),
        [
            _upload("annual-2021.txt", old),
            _upload("annual-2022.txt", target),
        ],
    )

    assert result["status"] == "blocked"
    assert result["converted"] == 0
    assert result["bridges"] == []
    assert result["inspection_history"]["history_ready"] is False
    blocked = next(
        item
        for item in result["inspection_history"]["bridges"]
        if item["bridge_identity"]["raw_structure_number"].strip() == "NEW-BRIDGE"
    )
    assert "insufficient_bridge_history" in {
        issue["code"] for issue in blocked["issues"]
    }


def test_manifest_requires_exact_contiguous_file_year_cardinality():
    raw = json.loads(
        _manifest([("annual-2021.txt", 2021), ("annual-2022.txt", 2022)])
    )
    raw["history_end_year"] = 2023

    with pytest.raises(InspectionHistoryError) as raised:
        parse_inspection_history_manifest(json.dumps(raw))

    assert getattr(raised.value, "code", None) == "noncontiguous_annual_history"


def test_ambiguous_pre_2000_date_stays_blocked_without_endpoint_override(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(main_module, "DATA_DIR", str(tmp_path))
    result = _run(
        _manifest([("annual-1998.txt", 1998), ("annual-1999.txt", 1999)]),
        [
            _upload("annual-1998.txt", _fixed_record(inspection_date="1297")),
            _upload("annual-1999.txt", _fixed_record(inspection_date="1298")),
        ],
    )

    assert result["status"] == "blocked"
    evidence = result["inspection_history"]
    assert evidence["history_scope"]["pre_2000_compatibility_enabled"] is False
    assert "pre2000_date_policy_required" in {
        issue["code"]
        for bridge in evidence["bridges"]
        for issue in bridge["issues"]
    }


def test_single_file_converter_still_refuses_to_guess_inspection_events(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(main_module, "DATA_DIR", str(tmp_path))
    result = asyncio.run(
        convert_v2(
            txt_file=_upload("single.txt", _fixed_record()),
            xml_file=None,
            source_agency_context="NJDOT",
            encoding="ascii",
            input_format="fhwa_fixed_445",
            issue_policy="reject",
            csv_delimiter=None,
            csv_quotechar=None,
            csv_escapechar=None,
            conversion_profile="research_historical_import",
            bid01_policy="preserve",
            claimed_element_schema=None,
        )
    )

    bridge = result["bridges"][0]
    assert bridge["Inspections"] == []
    assert "/Inspections" in bridge["_TransitionReview"]
    assert any(
        issue["code"] == "inspection_annual_history_required"
        for issue in bridge["_TransitionIssues"]
    )


def test_upload_part_names_must_match_manifest_before_artifact_ingest(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(main_module, "DATA_DIR", str(tmp_path))
    with pytest.raises(HTTPException) as raised:
        _run(
            _manifest([("annual-2021.txt", 2021), ("annual-2022.txt", 2022)]),
            [
                _upload("annual-2021.txt", _fixed_record()),
                _upload("wrong-name.txt", _fixed_record()),
            ],
        )

    assert raised.value.status_code == 400
    assert raised.value.detail["code"] == "annual_source_cardinality_mismatch"
    assert not (tmp_path / "conversion_sources").exists()
