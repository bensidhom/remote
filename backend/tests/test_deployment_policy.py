from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.services.operations.deployment import (
    DEPLOYMENT_POLICY_VERSION,
    MARKER_FILENAME,
    DeploymentMode,
    DeploymentPolicyError,
    evaluate_deployment_policy,
    require_deployment_policy,
)


def _dedicated_env(root: Path) -> dict[str, str]:
    return {
        "BRIDGEGUARD_ENV": "production",
        "BRIDGEGUARD_DEPLOYMENT_MODE": "dedicated_customer",
        "BRIDGEGUARD_WORKSPACE_ID": "njdot-production",
        "BRIDGEGUARD_CUSTOMER_SLUG": "njdot",
        "BRIDGEGUARD_WORKER_COUNT": "1",
        "BRIDGEGUARD_AUTH_MODE": "cloudflare",
        "CLOUDFLARE_ACCESS_TEAM_DOMAIN": "example.cloudflareaccess.com",
        "CLOUDFLARE_ACCESS_AUD": "audience",
        "BRIDGEGUARD_DATA_DIR": str(root),
        "RAG_WORKING_DIR": str(root / "rag_storage"),
        "SNBI_TEMP_DIR": str(root / "temp"),
        "BRIDGEGUARD_OWNER_EMAIL": "owner@example.com",
        "BRIDGEGUARD_PDF_ENGINE_DISPOSITION": "commercial_license",
        "BRIDGEGUARD_PDF_ENGINE_RECEIPT_ID": "PDF-LICENSE-2026-001",
        "BRIDGEGUARD_AI_PROVIDER_ID": "cerebras",
        "BRIDGEGUARD_AI_MODEL_ID": "gemma-4-31b",
        "CEREBRAS_MODEL": "gemma-4-31b",
        "BRIDGEGUARD_AI_PROVIDER_ENTITLEMENT_RECEIPT_ID": "AI-ORDER-2026-001",
        "BRIDGEGUARD_AI_DPA_RECEIPT_ID": "AI-DPA-2026-001",
        "BRIDGEGUARD_MAP_SERVICE_MODE": "arcgis_sdk_keyless_osm",
        "BRIDGEGUARD_MAP_SERVICE_RECEIPT_ID": "MAP-REVIEW-2026-001",
        "BRIDGEGUARD_CLOUDFLARE_SERVICE_RECEIPT_ID": "EDGE-ORDER-2026-001",
        "BRIDGEGUARD_OSS_NOTICES_RECEIPT_ID": "OSS-NOTICES-2026-001",
        "BRIDGEGUARD_SBOM_RECEIPT_ID": "SBOM-RELEASE-2026-001",
    }


def _write_marker(root: Path, **overrides: object) -> None:
    payload = {
        "format": DEPLOYMENT_POLICY_VERSION,
        "mode": "dedicated_customer",
        "workspace_id": "njdot-production",
        "customer_slug": "njdot",
        "deployment_id": "55bb873d-7020-40a7-a2ef-451bdf268efd",
        **overrides,
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER_FILENAME).write_text(
        json.dumps(payload, sort_keys=True), encoding="utf-8"
    )


def test_research_mode_is_approved_but_never_commercial_multicustomer(tmp_path: Path):
    policy = evaluate_deployment_policy(
        environ={
            "BRIDGEGUARD_ENV": "development",
            "BRIDGEGUARD_DEPLOYMENT_MODE": "research_shared",
            "BRIDGEGUARD_WORKSPACE_ID": "shared-research-workspace",
            "BRIDGEGUARD_WORKER_COUNT": "1",
            "BRIDGEGUARD_AUTH_MODE": "password",
        },
        data_directory=tmp_path,
    )
    assert policy.approved
    assert policy.mode is DeploymentMode.RESEARCH_SHARED
    assert not policy.commercial_multi_customer_ready
    assert "research_workspace_not_for_unrelated_paying_customers" in policy.warnings


def test_shared_multitenant_mode_is_explicitly_rejected(tmp_path: Path):
    policy = evaluate_deployment_policy(
        environ={
            "BRIDGEGUARD_DEPLOYMENT_MODE": "shared_multitenant",
            "BRIDGEGUARD_WORKSPACE_ID": "tenant-a",
            "BRIDGEGUARD_WORKER_COUNT": "1",
        },
        data_directory=tmp_path,
    )
    assert "shared_multitenant_not_implemented" in policy.blockers


def test_dedicated_customer_requires_exact_immutable_identity_marker(tmp_path: Path):
    env = _dedicated_env(tmp_path)
    missing = evaluate_deployment_policy(environ=env)
    assert "dedicated_deployment_marker_missing" in missing.blockers

    _write_marker(tmp_path, workspace_id="another-customer")
    mismatch = evaluate_deployment_policy(environ=env)
    assert "dedicated_deployment_marker_mismatch" in mismatch.blockers

    _write_marker(tmp_path)
    approved = evaluate_deployment_policy(environ=env)
    assert approved.approved
    assert approved.marker_verified
    assert approved.data_root_isolated
    assert approved.commercial_multi_customer_ready


@pytest.mark.parametrize("environment", ["development", "test"])
def test_dedicated_customer_is_not_commercial_outside_production(
    tmp_path: Path, environment: str
):
    env = _dedicated_env(tmp_path)
    env["BRIDGEGUARD_ENV"] = environment
    _write_marker(tmp_path)
    policy = evaluate_deployment_policy(environ=env)
    assert policy.approved
    assert not policy.commercial_multi_customer_ready


def test_unknown_environment_fails_closed(tmp_path: Path):
    env = _dedicated_env(tmp_path)
    env["BRIDGEGUARD_ENV"] = "prodution"
    _write_marker(tmp_path)
    policy = evaluate_deployment_policy(environ=env)
    assert "environment_invalid" in policy.blockers
    assert not policy.commercial_multi_customer_ready


@pytest.mark.parametrize(
    ("updates", "blocker"),
    [
        ({"BRIDGEGUARD_AUTH_DISABLED": "true"}, "production_auth_disabled"),
        ({"BRIDGEGUARD_AUTH_MODE": "password"}, "production_password_mode_not_approved"),
        ({"BRIDGEGUARD_WORKER_COUNT": "2"}, "single_worker_coordination_required"),
        ({"BRIDGEGUARD_WORKSPACE_ID": "shared-research-workspace"}, "dedicated_workspace_must_be_customer_specific"),
        ({"BRIDGEGUARD_CUSTOMER_SLUG": ""}, "dedicated_customer_slug_required"),
    ],
)
def test_dedicated_gate_rejects_unsafe_settings(tmp_path: Path, updates, blocker):
    env = _dedicated_env(tmp_path)
    env.update(updates)
    _write_marker(tmp_path)
    assert blocker in evaluate_deployment_policy(environ=env).blockers


def test_require_commercial_fails_for_research_workspace(tmp_path: Path):
    with pytest.raises(DeploymentPolicyError) as captured:
        require_deployment_policy(
            environ={
                "BRIDGEGUARD_DEPLOYMENT_MODE": "research_shared",
                "BRIDGEGUARD_WORKSPACE_ID": "shared-research-workspace",
                "BRIDGEGUARD_WORKER_COUNT": "1",
            },
            data_directory=tmp_path,
            require_commercial=True,
        )
    assert "commercial_dedicated_deployment_required" in captured.value.blockers


def test_public_evidence_contains_no_data_path_or_secret(tmp_path: Path):
    env = _dedicated_env(tmp_path)
    env["CEREBRAS_API_KEY"] = "super-secret"
    env["BRIDGEGUARD_PDF_ENGINE_RECEIPT_ID"] = "SENSITIVE-PDF-REVIEW-991"
    env["BRIDGEGUARD_AI_PROVIDER_ENTITLEMENT_RECEIPT_ID"] = (
        "SENSITIVE-PROCUREMENT-772"
    )
    env["BRIDGEGUARD_AI_DPA_RECEIPT_ID"] = "SENSITIVE-DPA-663"
    env["BRIDGEGUARD_MAP_SERVICE_RECEIPT_ID"] = "SENSITIVE-MAP-554"
    env["BRIDGEGUARD_CLOUDFLARE_SERVICE_RECEIPT_ID"] = "SENSITIVE-EDGE-445"
    env["BRIDGEGUARD_OSS_NOTICES_RECEIPT_ID"] = "SENSITIVE-NOTICES-336"
    env["BRIDGEGUARD_SBOM_RECEIPT_ID"] = "SENSITIVE-SBOM-227"
    _write_marker(tmp_path)
    policy = evaluate_deployment_policy(environ=env)
    rendered = json.dumps(policy.public_dict())
    assert str(tmp_path) not in rendered
    assert "super-secret" not in rendered
    assert "SENSITIVE" not in rendered
    evidence = policy.public_dict()["commercial_entitlement_evidence"]
    assert evidence == {
        "version": "bridgeguard-commercial-entitlement-evidence-v1",
        "pdf_engine_disposition": "commercial_license",
        "pdf_engine_receipt_recorded": True,
        "ai_provider_identity_matched": True,
        "ai_model_identity_matched": True,
        "ai_provider_entitlement_receipt_recorded": True,
        "ai_dpa_receipt_recorded": True,
        "map_service_mode": "arcgis_sdk_keyless_osm",
        "map_service_receipt_recorded": True,
        "cloudflare_service_receipt_recorded": True,
        "oss_notices_receipt_recorded": True,
        "sbom_receipt_recorded": True,
        "approved": True,
        "operator_record_only": True,
        "legal_clearance_claimed": False,
    }


@pytest.mark.parametrize(
    ("key", "blocker"),
    [
        ("BRIDGEGUARD_PDF_ENGINE_DISPOSITION", "pdf_engine_disposition_missing"),
        ("BRIDGEGUARD_PDF_ENGINE_RECEIPT_ID", "pdf_engine_receipt_id_missing"),
        ("BRIDGEGUARD_AI_PROVIDER_ID", "ai_provider_id_missing"),
        ("BRIDGEGUARD_AI_MODEL_ID", "ai_model_id_missing"),
        ("CEREBRAS_MODEL", "ai_runtime_model_missing"),
        (
            "BRIDGEGUARD_AI_PROVIDER_ENTITLEMENT_RECEIPT_ID",
            "ai_provider_entitlement_receipt_id_missing",
        ),
        ("BRIDGEGUARD_AI_DPA_RECEIPT_ID", "ai_dpa_receipt_id_missing"),
        ("BRIDGEGUARD_MAP_SERVICE_MODE", "map_service_mode_missing"),
        ("BRIDGEGUARD_MAP_SERVICE_RECEIPT_ID", "map_service_receipt_id_missing"),
        (
            "BRIDGEGUARD_CLOUDFLARE_SERVICE_RECEIPT_ID",
            "cloudflare_service_receipt_id_missing",
        ),
        ("BRIDGEGUARD_OSS_NOTICES_RECEIPT_ID", "oss_notices_receipt_id_missing"),
        ("BRIDGEGUARD_SBOM_RECEIPT_ID", "sbom_receipt_id_missing"),
    ],
)
def test_production_dedicated_gate_requires_every_entitlement_record(
    tmp_path: Path, key: str, blocker: str
):
    env = _dedicated_env(tmp_path)
    env.pop(key)
    _write_marker(tmp_path)
    policy = evaluate_deployment_policy(environ=env)
    assert blocker in policy.blockers
    assert not policy.commercial_multi_customer_ready
    with pytest.raises(DeploymentPolicyError) as captured:
        require_deployment_policy(environ=env, data_directory=tmp_path)
    assert blocker in captured.value.blockers


@pytest.mark.parametrize(
    ("updates", "blocker"),
    [
        (
            {"BRIDGEGUARD_PDF_ENGINE_DISPOSITION": "unreviewed"},
            "pdf_engine_disposition_invalid",
        ),
        (
            {"BRIDGEGUARD_PDF_ENGINE_RECEIPT_ID": "../../legal/approval.pdf"},
            "pdf_engine_receipt_id_invalid",
        ),
        (
            {"BRIDGEGUARD_AI_PROVIDER_ENTITLEMENT_RECEIPT_ID": "replace-me"},
            "ai_provider_entitlement_receipt_id_invalid",
        ),
        (
            {"BRIDGEGUARD_AI_DPA_RECEIPT_ID": "pending"},
            "ai_dpa_receipt_id_invalid",
        ),
        (
            {"BRIDGEGUARD_AI_PROVIDER_ID": "another-provider"},
            "ai_provider_runtime_mismatch",
        ),
        (
            {"BRIDGEGUARD_AI_MODEL_ID": "another-model"},
            "ai_model_runtime_mismatch",
        ),
        (
            {"BRIDGEGUARD_MAP_SERVICE_MODE": "public-osm-without-provider-review"},
            "map_service_mode_invalid",
        ),
        (
            {"BRIDGEGUARD_MAP_SERVICE_RECEIPT_ID": "example-map-receipt"},
            "map_service_receipt_id_invalid",
        ),
        (
            {"BRIDGEGUARD_CLOUDFLARE_SERVICE_RECEIPT_ID": "test-cloudflare"},
            "cloudflare_service_receipt_id_invalid",
        ),
        (
            {"BRIDGEGUARD_OSS_NOTICES_RECEIPT_ID": "todo"},
            "oss_notices_receipt_id_invalid",
        ),
        (
            {"BRIDGEGUARD_SBOM_RECEIPT_ID": "../../sbom.json"},
            "sbom_receipt_id_invalid",
        ),
    ],
)
def test_entitlement_gate_rejects_placeholders_paths_and_runtime_mismatches(
    tmp_path: Path, updates: dict[str, str], blocker: str
):
    env = _dedicated_env(tmp_path)
    env.update(updates)
    _write_marker(tmp_path)
    policy = evaluate_deployment_policy(environ=env)
    assert blocker in policy.blockers
    assert not policy.commercial_entitlement_evidence.approved


@pytest.mark.parametrize(
    "disposition",
    ["permissive_replacement", "commercial_license", "approved_agpl_compliance"],
)
def test_each_reviewable_pdf_disposition_can_pass_the_record_gate(
    tmp_path: Path, disposition: str
):
    env = _dedicated_env(tmp_path)
    env["BRIDGEGUARD_PDF_ENGINE_DISPOSITION"] = disposition
    _write_marker(tmp_path)
    policy = evaluate_deployment_policy(environ=env)
    assert policy.approved
    assert policy.commercial_entitlement_evidence.pdf_engine_disposition == disposition


@pytest.mark.parametrize("mode", ["arcgis_premium", "arcgis_sdk_keyless_osm"])
def test_each_reviewable_map_mode_can_pass_the_record_gate(
    tmp_path: Path, mode: str
):
    env = _dedicated_env(tmp_path)
    env["BRIDGEGUARD_MAP_SERVICE_MODE"] = mode
    _write_marker(tmp_path)
    policy = evaluate_deployment_policy(environ=env)
    assert policy.approved
    assert policy.commercial_entitlement_evidence.map_service_mode == mode


def test_research_mode_does_not_require_commercial_entitlement_records(tmp_path: Path):
    policy = evaluate_deployment_policy(
        environ={
            "BRIDGEGUARD_ENV": "production",
            "BRIDGEGUARD_DEPLOYMENT_MODE": "research_shared",
            "BRIDGEGUARD_WORKSPACE_ID": "shared-research-workspace",
            "BRIDGEGUARD_WORKER_COUNT": "1",
            "BRIDGEGUARD_AUTH_MODE": "cloudflare",
            "CLOUDFLARE_ACCESS_TEAM_DOMAIN": "example.cloudflareaccess.com",
            "CLOUDFLARE_ACCESS_AUD": "audience",
        },
        data_directory=tmp_path,
    )
    assert policy.approved
    assert not policy.commercial_entitlement_evidence.approved
    assert not any("receipt" in blocker for blocker in policy.blockers)


@pytest.mark.parametrize(
    ("key", "value_factory", "blocker"),
    [
        ("BRIDGEGUARD_DATA_DIR", lambda root: root.parent / "other", "dedicated_data_dir_outside_customer_root"),
        ("RAG_WORKING_DIR", lambda root: root.parent / "rag", "rag_working_dir_outside_customer_root"),
        ("SNBI_TEMP_DIR", lambda root: root.parent / "temp", "snbi_temp_dir_outside_customer_root"),
        ("RAG_WORKING_DIR", lambda root: root / "cache" / ".." / "rag", "rag_working_dir_traversal"),
    ],
)
def test_dedicated_storage_paths_cannot_escape_customer_root(
    tmp_path: Path, key: str, value_factory, blocker: str
):
    root = tmp_path / "customer"
    env = _dedicated_env(root)
    env[key] = str(value_factory(root))
    _write_marker(root)
    policy = evaluate_deployment_policy(environ=env, data_directory=root)
    assert blocker in policy.blockers
    assert not policy.storage_paths_verified
    assert not policy.commercial_multi_customer_ready


@pytest.mark.parametrize(
    ("key", "blocker"),
    [
        ("BRIDGEGUARD_DATA_DIR", "dedicated_data_dir_missing"),
        ("RAG_WORKING_DIR", "rag_working_dir_missing"),
        ("SNBI_TEMP_DIR", "snbi_temp_dir_missing"),
        ("BRIDGEGUARD_OWNER_EMAIL", "dedicated_role_users_missing"),
    ],
)
def test_dedicated_gate_requires_explicit_stores_and_role_users(
    tmp_path: Path, key: str, blocker: str
):
    env = _dedicated_env(tmp_path)
    env.pop(key)
    _write_marker(tmp_path)
    assert blocker in evaluate_deployment_policy(environ=env).blockers


def test_dedicated_gate_rejects_symlinked_root_and_cache(tmp_path: Path):
    target = tmp_path / "target"
    env = _dedicated_env(target)
    _write_marker(target)
    root_link = tmp_path / "customer-link"
    try:
        root_link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    linked_env = _dedicated_env(root_link)
    linked = evaluate_deployment_policy(environ=linked_env, data_directory=root_link)
    assert "data_root_symlink" in linked.blockers
    assert not linked.commercial_multi_customer_ready

    safe_root = tmp_path / "safe-customer"
    _write_marker(safe_root)
    outside_cache = tmp_path / "outside-cache"
    outside_cache.mkdir()
    (safe_root / "rag_storage").symlink_to(outside_cache, target_is_directory=True)
    cache_env = _dedicated_env(safe_root)
    cached = evaluate_deployment_policy(environ=cache_env)
    assert "rag_working_dir_symlink" in cached.blockers
    assert "rag_working_dir_outside_customer_root" in cached.blockers


def test_dedicated_gate_rejects_domain_wide_viewer_grants(tmp_path: Path):
    env = _dedicated_env(tmp_path)
    env["BRIDGEGUARD_ALLOWED_DOMAINS"] = "agency.example"
    _write_marker(tmp_path)
    policy = evaluate_deployment_policy(environ=env)
    assert "dedicated_allowed_domains_not_permitted" in policy.blockers
    assert not policy.commercial_multi_customer_ready


def test_datapaths_import_rejects_escape_before_creating_outside_directory(
    tmp_path: Path,
):
    root = tmp_path / "customer"
    _write_marker(root)
    outside_temp = tmp_path / "outside-temp"
    env = {
        **os.environ,
        **_dedicated_env(root),
        "SNBI_TEMP_DIR": str(outside_temp),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
    }
    completed = subprocess.run(
        [sys.executable, "-c", "import app.routers.services.datapaths"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert "snbi_temp_dir_outside_customer_root" in completed.stderr
    assert not outside_temp.exists()
