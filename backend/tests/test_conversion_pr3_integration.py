from __future__ import annotations

import asyncio
import io
from dataclasses import replace

from fastapi import UploadFile

from app.main import convert_v2
from app.services.conversion import pipeline as conversion_pipeline


EXPECTED_LAYER_ORDER = [
    "json_resource",
    "pinned_schema",
    "bridgeguard_projection_integrity",
    "executable_official_rules",
    "transition_qa",
    "workspace_policy",
]


def _fixed_record() -> bytes:
    record = bytearray(b" " * 445)
    record[0:3] = b"342"
    record[3:18] = b"TEST-BRIDGE".ljust(15, b" ")
    record[18:19] = b"1"
    record[156:160] = b"1999"
    record[199:200] = b"1"
    record[222:228] = b" 010.0"
    record[373:374] = b"Y"
    return bytes(record)


def _upload(name: str, body: bytes) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(body))


def _run():
    return asyncio.run(
        convert_v2(
            txt_file=_upload("bridges.txt", _fixed_record()),
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


def _resolve_pointer(document, pointer: str):
    current = document
    for raw_token in pointer.lstrip("/").split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        current = current[int(token)] if isinstance(current, list) else current[token]
    return current


def test_strict_pipeline_does_not_call_legacy_normalizer_or_validator(monkeypatch):
    def forbidden_legacy_call(*_args, **_kwargs):
        raise AssertionError("the strict conversion path called legacy validation code")

    # ``raising=False`` keeps this regression guard valid after the old imports
    # are removed from pipeline.py altogether.
    monkeypatch.setattr(
        conversion_pipeline,
        "normalize_snbi_payload",
        forbidden_legacy_call,
        raising=False,
    )
    monkeypatch.setattr(
        conversion_pipeline,
        "validate_snbi_payload",
        forbidden_legacy_call,
        raising=False,
    )

    result = _run()

    bridge = result["bridges"][0]
    assert bridge["Features"] == [{"BF01": "H01", "BF02": "C"}]
    assert bridge["Elements"] == []
    assert bridge["Inspections"] == []
    assert bridge["PostingEvaluations"] == []
    assert bridge["PostingStatuses"] == []
    assert bridge["SpanSets"] == []
    assert bridge["SubstructureSets"] == []
    assert bridge["Works"] == []
    assert bridge["_TransitionLineage"]
    for pointer in bridge["_TransitionLineage"]:
        assert _resolve_pointer(bridge, pointer) is not None


def test_canonical_normalization_reports_rfc6901_pointers_without_data_loss(
    monkeypatch,
):
    real_mapper = conversion_pipeline.map_bridge_with_lineage

    def mapper_with_unknown_tag(*args, **kwargs):
        transition = real_mapper(*args, **kwargs)
        document = dict(transition.document)
        document["NOT_AN_SNBI_TAG"] = "must be diagnosed, never silently retained"
        return replace(transition, document=document)

    monkeypatch.setattr(
        conversion_pipeline,
        "map_bridge_with_lineage",
        mapper_with_unknown_tag,
    )

    result = _run()

    normalization = result["canonical_normalization"]
    assert normalization["profile_id"] == "research_historical_import"
    assert normalization["submission_ready"] is False
    assert normalization["documents"]
    document_report = normalization["documents"][0]
    assert document_report["schema_status"] == "unavailable_unverified"
    diagnostics = document_report["diagnostics"]
    unknown = [item for item in diagnostics if item["code"] == "unknown_tag"]
    assert unknown
    assert unknown[0]["json_pointer"] == "/NOT_AN_SNBI_TAG"
    assert unknown[0]["severity"] in {"error", "critical"}

    bridge = result["bridges"][0]
    assert "NOT_AN_SNBI_TAG" not in bridge
    assert bridge["Features"][0]["BF01"] == "H01"
    assert bridge["_TransitionLineage"]["/Features/0/BF01"]["rule_id"]


def test_conversion_exposes_six_layer_fail_closed_validation_and_issue_download():
    result = _run()

    validation = result["validation"]
    assert validation["validation_label"] == "BridgeGuard validation"
    assert validation["official_validation"] is False
    assert validation["submission_ready"] is False
    assert result["submission_ready"] is False
    assert validation["reports"]

    report = validation["reports"][0]
    assert report["official_validation"] is False
    assert report["decision"]["official_validation"] is False
    assert report["decision"]["allowed"] is False
    assert [layer["layer"] for layer in report["layers"]] == EXPECTED_LAYER_ORDER

    layer_by_name = {layer["layer"]: layer for layer in report["layers"]}
    pinned_schema = layer_by_name["pinned_schema"]
    assert pinned_schema["completed"] is False
    assert pinned_schema["evidence"][0]["attributes"]["status"] == "unsupported_dialect"
    assert pinned_schema["evidence"][0]["attributes"]["artifact_id"] == (
        "snbi_schema_v04_july_2024"
    )
    assert pinned_schema["evidence"][0]["attributes"]["dialect"] == (
        "https://json-schema.org/draft/2024-07/schema"
    )
    assert any(
        issue["rule_id"] == "BG-PINNED-SCHEMA-AVAILABILITY"
        and "draft/2024-07" in issue["message"]
        for issue in pinned_schema["issues"]
    )
    for unavailable_layer in (
        "pinned_schema",
        "bridgeguard_projection_integrity",
        "executable_official_rules",
    ):
        assert layer_by_name[unavailable_layer]["completed"] is False
        assert layer_by_name[unavailable_layer]["issues"]

    blockers = report["decision"]["blockers"]
    incomplete_layers = {
        blocker["layer"]
        for blocker in blockers
        if blocker["code"] == "layer_incomplete"
    }
    assert {
        "pinned_schema",
        "bridgeguard_projection_integrity",
        "executable_official_rules",
    } <= incomplete_layers
    assert any(
        blocker["code"]
        in {"unverified_standards_artifacts", "standards_registry_verification_failed"}
        for blocker in blockers
    )

    diagnostics = result["diagnostics"]
    assert diagnostics["format"] == "bridgeguard-conversion-diagnostics-v1"
    assert diagnostics["media_type"] == "application/json"
    assert diagnostics["filename"].endswith(".json")
    assert diagnostics["issues"]
    for issue in diagnostics["issues"]:
        assert {"stage", "code", "severity", "message", "json_pointer"} <= issue.keys()
    assert any(
        issue["stage"] == "validation"
        and issue["code"] == "BG-PINNED-SCHEMA-AVAILABILITY"
        for issue in diagnostics["issues"]
    )
    assert diagnostics["validation_reports"] == validation["reports"]
