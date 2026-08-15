from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.operations.deployment import evaluate_deployment_policy
from app.services.operations.provisioning import provision_dedicated_layout


def test_provision_is_exact_idempotent_and_satisfies_dedicated_marker(tmp_path: Path):
    root = tmp_path / "customers" / "njdot"
    first = provision_dedicated_layout(
        root, customer_slug="njdot", workspace_id="njdot-production"
    )
    second = provision_dedicated_layout(
        root, customer_slug="njdot", workspace_id="njdot-production"
    )
    assert first.created is True
    assert second.created is False
    assert first.marker_sha256 == second.marker_sha256
    assert first.directory_count == 10
    policy = evaluate_deployment_policy(
        environ={
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
        },
        data_directory=root,
    )
    assert policy.commercial_multi_customer_ready


def test_provision_rejects_cross_customer_marker_and_unmarked_content(tmp_path: Path):
    root = tmp_path / "customer"
    provision_dedicated_layout(root, customer_slug="one", workspace_id="one-prod")
    with pytest.raises(RuntimeError, match="another deployment"):
        provision_dedicated_layout(root, customer_slug="two", workspace_id="two-prod")

    unmarked = tmp_path / "unmarked"
    unmarked.mkdir()
    (unmarked / "unknown.db").write_text("not ours", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unmarked nonempty"):
        provision_dedicated_layout(
            unmarked, customer_slug="three", workspace_id="three-prod"
        )


@pytest.mark.parametrize("value", ["", "../escape", "Upper", "has space", "a" * 65])
def test_provision_rejects_invalid_identifiers(tmp_path: Path, value: str):
    with pytest.raises(ValueError):
        provision_dedicated_layout(
            tmp_path / "root", customer_slug=value, workspace_id="valid-workspace"
        )


def test_receipt_and_marker_do_not_expose_root_path(tmp_path: Path):
    root = tmp_path / "sensitive-location" / "customer"
    receipt = provision_dedicated_layout(
        root, customer_slug="agency", workspace_id="agency-prod"
    )
    assert str(root) not in json.dumps(receipt.public_dict())


def test_provision_rejects_symlink_customer_root_and_layout_entries(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    linked_root = tmp_path / "linked-customer"
    try:
        linked_root.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    with pytest.raises(ValueError, match="symlink"):
        provision_dedicated_layout(
            linked_root, customer_slug="agency", workspace_id="agency-prod"
        )

    root = tmp_path / "customer"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "attachments").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlink"):
        provision_dedicated_layout(
            root, customer_slug="agency", workspace_id="agency-prod"
        )


def test_provision_rejects_parent_traversal_even_when_it_resolves_in_scope(tmp_path: Path):
    with pytest.raises(ValueError, match="traversal"):
        provision_dedicated_layout(
            tmp_path / "customer" / ".." / "customer",
            customer_slug="agency",
            workspace_id="agency-prod",
        )
