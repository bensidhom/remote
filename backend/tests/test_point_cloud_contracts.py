from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import HTTPException

from app.routers import point_cloud_api
from app.services.point_cloud.contracts import (
    GravityStatus,
    MeasurementDisposition,
    PipelineConfigurationError,
    PointCloudPipelineMode,
    ReviewStatus,
    SourceIdentityStatus,
    UnitsStatus,
    annotate_point_cloud_result,
    require_available_point_cloud_pipeline,
    resolve_point_cloud_pipeline_mode,
)


def test_contract_types_are_frozen_and_serialize_honest_research_status():
    annotated = annotate_point_cloud_result(
        {"status": "success", "clearance_m": 4.21},
        pipeline_mode=PointCloudPipelineMode.LEGACY_RESEARCH,
        units_status=UnitsStatus.CLIENT_ASSERTED_UNVERIFIED,
        gravity_status=GravityStatus.ASSUMED_Z_UP_UNVERIFIED,
        source_identity_status=SourceIdentityStatus.SOURCE_HASH_AND_SNAPSHOT_BOUND,
    )

    contract = annotated["result_contract"]
    assert annotated["status"] == "success"
    assert annotated["clearance_m"] == 4.21
    assert contract == {
        "contract_version": "bridgeguard-point-cloud-result-v1",
        "pipeline_mode": "legacy_research",
        "implementation": "legacy-research-v2",
        "disposition": "screening_estimate",
        "measurement_authority": "research_screening_not_survey_or_snbi",
        "units_status": "client_asserted_unverified",
        "gravity_status": "assumed_z_up_unverified",
        "source_identity_status": "source_hash_and_snapshot_bound",
        "review_status": "unreviewed",
        "submission_ready": False,
    }
    assert "Screening estimate only" in annotated["warnings"][-1]

    with pytest.raises((FrozenInstanceError, AttributeError)):
        MeasurementDisposition.SCREENING_ESTIMATE.value = "accepted"  # type: ignore[misc]


def test_non_success_result_is_not_measurable_and_does_not_gain_numeric_claims():
    annotated = annotate_point_cloud_result(
        {"status": "insufficient_points", "point_count": 12},
        pipeline_mode=PointCloudPipelineMode.LEGACY_RESEARCH,
        units_status=UnitsStatus.UNKNOWN_OR_RELATIVE,
        gravity_status=GravityStatus.ASSUMED_Z_UP_UNVERIFIED,
        source_identity_status=SourceIdentityStatus.INLINE_REQUEST_UNRETAINED,
    )

    assert annotated["result_contract"]["disposition"] == "not_measurable"
    assert annotated["result_contract"]["review_status"] == ReviewStatus.UNREVIEWED.value
    assert "clearance_m" not in annotated


@pytest.mark.parametrize(
    ("environment", "deployment_mode", "configured", "expected"),
    [
        ("test", "research_shared", None, PointCloudPipelineMode.LEGACY_RESEARCH),
        ("development", "research_shared", "hardened_shadow", PointCloudPipelineMode.HARDENED_SHADOW),
        ("production", "dedicated_customer", None, PointCloudPipelineMode.REQUIRED),
        ("production", "research_shared", None, PointCloudPipelineMode.REQUIRED),
        ("development", "dedicated_customer", None, PointCloudPipelineMode.REQUIRED),
    ],
)
def test_pipeline_mode_defaults_fail_closed_for_protected_deployments(
    environment, deployment_mode, configured, expected
):
    env = {
        "BRIDGEGUARD_ENV": environment,
        "BRIDGEGUARD_DEPLOYMENT_MODE": deployment_mode,
    }
    if configured is not None:
        env["POINT_CLOUD_PIPELINE_MODE"] = configured
    assert resolve_point_cloud_pipeline_mode(env) is expected


@pytest.mark.parametrize("configured", ["legacy_research", "hardened_shadow"])
def test_production_or_dedicated_cannot_enable_a_nonrequired_pipeline(configured):
    with pytest.raises(PipelineConfigurationError) as caught:
        resolve_point_cloud_pipeline_mode(
            {
                "BRIDGEGUARD_ENV": "production",
                "BRIDGEGUARD_DEPLOYMENT_MODE": "dedicated_customer",
                "POINT_CLOUD_PIPELINE_MODE": configured,
            }
        )
    assert caught.value.code == "point_cloud_required_pipeline_enforced"


def test_invalid_pipeline_mode_fails_closed():
    with pytest.raises(PipelineConfigurationError) as caught:
        resolve_point_cloud_pipeline_mode(
            {
                "BRIDGEGUARD_ENV": "development",
                "BRIDGEGUARD_DEPLOYMENT_MODE": "research_shared",
                "POINT_CLOUD_PIPELINE_MODE": "fallback_to_anything",
            }
        )
    assert caught.value.code == "point_cloud_pipeline_mode_invalid"


def test_hardened_modes_are_available_after_durable_pipeline_lands():
    selected = require_available_point_cloud_pipeline(
        {
            "POINT_CLOUD_PIPELINE_MODE": "required",
            "BRIDGEGUARD_ENV": "production",
            "BRIDGEGUARD_DEPLOYMENT_MODE": "dedicated_customer",
        }
    )
    assert selected is PointCloudPipelineMode.REQUIRED


def test_point_cloud_health_exposes_only_safe_rollout_state(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("BRIDGEGUARD_ENV", "test")
    monkeypatch.setenv("BRIDGEGUARD_DEPLOYMENT_MODE", "research_shared")
    monkeypatch.setenv("POINT_CLOUD_PIPELINE_MODE", "legacy_research")
    payload = point_cloud_api.point_cloud_health()
    assert payload["pipeline_mode"] == "legacy_research"
    assert payload["pipeline_available"] is True
    assert payload["parser_ready"] is payload["e57_supported"]
    assert payload["durable_workflow_available"] is payload["e57_supported"]
    assert payload["multimodal_model_ready"] is True
    assert "path" not in str(payload).lower()

    monkeypatch.setenv("BRIDGEGUARD_ENV", "production")
    monkeypatch.setenv("BRIDGEGUARD_DEPLOYMENT_MODE", "dedicated_customer")
    monkeypatch.setenv("POINT_CLOUD_PIPELINE_MODE", "required")
    blocked = point_cloud_api.point_cloud_health()
    assert blocked["status"] == "degraded"
    assert blocked["pipeline_mode"] == "required"
    assert blocked["pipeline_available"] is True
    assert blocked["parser_ready"] is blocked["e57_supported"]
    assert blocked["durable_workflow_available"] is blocked["e57_supported"]
    assert blocked["multimodal_model_ready"] is True
    assert blocked["bridge_synchronous_routes_retired"] is True
    assert blocked["evaluation_required"] is True
    assert blocked["evaluation_release_ready"] is False
    assert blocked["evaluation_error_code"] == "point_cloud_evaluation_receipt_required"


def test_point_cloud_info_discovers_only_accepted_durable_result(monkeypatch):
    row = (7, "opaque-source", "bridge.e57", 1234, "2026-08-14T12:00:00Z", "a" * 64)
    monkeypatch.setattr(point_cloud_api, "_raw_cloud_row", lambda *_args: row)
    monkeypatch.setattr(
        point_cloud_api,
        "_load_processed",
        lambda *_args: {"persistence": {"processed_point_count": 99}},
    )
    monkeypatch.setattr(
        point_cloud_api, "_source_identity", lambda _row: ("7", "a" * 64)
    )
    monkeypatch.setattr(point_cloud_api, "_load_source_receipt", lambda _row: None)
    monkeypatch.setattr(point_cloud_api, "_public_source_receipt", lambda _value: None)
    monkeypatch.setattr(
        point_cloud_api,
        "get_latest_published_point_cloud_result",
        lambda *_args, **_kwargs: SimpleNamespace(
            data_year=2026, run_id="123e4567-e89b-42d3-a456-426614174000"
        ),
    )

    payload = point_cloud_api.point_cloud_info("34", "0101150")
    assert payload["accepted_result_exists"] is True
    assert payload["accepted_result_data_year"] == 2026
    assert payload["legacy_processed_exists"] is True
    assert "processed_exists" not in payload


def test_point_cloud_source_capabilities_are_truthful_and_path_free():
    payload = point_cloud_api.point_cloud_source_capabilities()
    assert payload["contract_version"] == "bridgeguard-point-cloud-source-capabilities-v1"
    assert payload["source_attachment_extensions"] == [".e57"]
    assert payload["metric_ingest_extensions"] in ([], [".e57"])
    assert payload["browser_visualization_is_not_metric_ingestion"] is True
    formats = {item["format"]: item for item in payload["formats"]}
    assert formats["las"]["source_ingest_allowed"] is False
    assert formats["laz"]["source_ingest_allowed"] is False
    assert formats["ply"]["source_ingest_allowed"] is False
    assert formats["npz_internal"]["source_ingest_allowed"] is False
    assert "path" not in str(payload).lower()


def test_bridge_synchronous_processing_routes_are_retired_without_fallback(monkeypatch):
    monkeypatch.delenv("BRIDGEGUARD_POINT_CLOUD_LEGACY_BRIDGE_ROUTES", raising=False)
    monkeypatch.setenv("BRIDGEGUARD_DEPLOYMENT_MODE", "research_shared")
    with pytest.raises(HTTPException) as caught:
        point_cloud_api._require_durable_bridge_processing()
    assert caught.value.status_code == 410
    assert caught.value.detail["code"] == "point_cloud_bridge_route_retired"
    for call in (
        lambda: point_cloud_api.processed_analysis("34", "0101150"),
        lambda: point_cloud_api.processed_preview("34", "0101150"),
        lambda: point_cloud_api.download_processed("34", "0101150"),
    ):
        with pytest.raises(HTTPException) as legacy_read:
            call()
        assert legacy_read.value.status_code == 410

    monkeypatch.setenv("BRIDGEGUARD_POINT_CLOUD_LEGACY_BRIDGE_ROUTES", "true")
    point_cloud_api._require_durable_bridge_processing()
    monkeypatch.setenv("BRIDGEGUARD_DEPLOYMENT_MODE", "dedicated_customer")
    with pytest.raises(HTTPException) as caught:
        point_cloud_api._require_durable_bridge_processing()
    assert caught.value.status_code == 410


def test_saved_session_result_has_an_explicit_unreviewed_read_surface(monkeypatch):
    monkeypatch.setattr(
        point_cloud_api,
        "_load_processed",
        lambda *_args: {"result_kind": "saved_session_preview", "status": "success"},
    )
    monkeypatch.setattr(
        point_cloud_api,
        "_processed_preview",
        lambda *_args, **_kwargs: {
            "status": "success",
            "rendered_point_count": 3,
            "points": [[0.0, 0.0, 0.0]],
        },
    )

    result = point_cloud_api.saved_session_analysis("34", "0101150")
    preview = point_cloud_api.saved_session_preview("34", "0101150")

    assert json.loads(result.body)["result_kind"] == "saved_session_preview"
    assert json.loads(preview.body)["rendered_point_count"] == 3
    assert result.headers["cache-control"] == "no-store"
    assert preview.headers["x-content-type-options"] == "nosniff"


def test_ad_hoc_bridge_preview_is_saved_against_the_current_source(monkeypatch):
    points = np.asarray([[0.0, 0.0, 0.0], [1.0, 0.0, 1.0]], dtype=np.float32)
    source_row = (7, "opaque-source", "bridge.e57", 1234, "now", "a" * 64)
    saved: dict[str, object] = {}
    loaded_pack_payload = {
        "schema_version": "bridgeguard-point-cloud-loaded-inventory-context-v1",
        "trusted_structured_context": {"trust": "loaded_yearly_inventory"},
    }
    loaded_pack_json = json.dumps(
        loaded_pack_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    loaded_pack_sha256 = hashlib.sha256(loaded_pack_json.encode("utf-8")).hexdigest()

    async def cloud_payload(_request):
        return points, None, {
            "save_result": "true",
            "source_sha256": "a" * 64,
            "bridge_context_state_code": "34",
            "bridge_context_bridge_number": "0101150",
            "scale_known": "true",
            "use_vlm": "false",
            "use_ai_cleaning": "false",
        }

    def save_processed(state_code, bridge_number, world_points, result, **kwargs):
        saved.update(
            state_code=state_code,
            bridge_number=bridge_number,
            point_count=len(world_points),
            source_row=kwargs.get("source_row"),
            profile=kwargs.get("analysis_profile"),
            knowledge_pack_json=kwargs.get("knowledge_pack_json"),
        )
        return {"saved": True, "processed_point_count": 2}

    monkeypatch.setattr(point_cloud_api, "_cloud_payload", cloud_payload)
    monkeypatch.setattr(point_cloud_api, "_require_points", lambda *_args, **_kwargs: points)
    monkeypatch.setattr(point_cloud_api, "_raw_cloud_row", lambda *_args: source_row)
    monkeypatch.setattr(point_cloud_api, "_source_identity", lambda _row: (7, "a" * 64))
    monkeypatch.setattr(
        point_cloud_api,
        "get_loaded_bridge_snbi_snapshot",
        lambda *_args, **_kwargs: {"data_year": 2025, "document": {"Features": []}},
    )
    monkeypatch.setattr(
        point_cloud_api,
        "build_loaded_inventory_prompt_context",
        lambda *_args, **_kwargs: {
            **loaded_pack_payload,
            "knowledge_pack_sha256": loaded_pack_sha256,
        },
    )
    monkeypatch.setattr(point_cloud_api, "_canonical_under_feature_kind", lambda _doc: None)
    monkeypatch.setattr(
        point_cloud_api,
        "analyze_bridge",
        lambda *_args, **_kwargs: {
            "status": "success",
            "labels_b64u8": base64.b64encode(bytes([1, 1])).decode("ascii"),
            "classes": [{"id": 1, "color_rgb": [0.2, 0.4, 0.8]}],
        },
    )
    monkeypatch.setattr(point_cloud_api, "_save_processed", save_processed)

    result = asyncio.run(point_cloud_api._run_analysis(object()))

    assert result["result_kind"] == "saved_session_preview"
    assert result["persistence"]["saved"] is True
    assert saved["state_code"] == "34"
    assert saved["bridge_number"] == "0101150"
    assert saved["source_row"] == source_row
    assert saved["profile"]["persistence_mode"] == "saved_session_preview"
    assert saved["profile"]["knowledge_pack_sha256"] == loaded_pack_sha256
    assert saved["knowledge_pack_json"] == loaded_pack_json
