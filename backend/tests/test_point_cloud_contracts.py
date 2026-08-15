from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

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
