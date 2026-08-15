from __future__ import annotations

import hashlib
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3

import pytest

from app.routers.services.db_schema import LATEST_SCHEMA_VERSION
from app.services.operations.deployment import evaluate_deployment_policy
from app.services.operations.deployment_database import (
    DeploymentDatabaseBindingError,
    bootstrap_dedicated_database,
    verify_dedicated_database_binding,
)
from app.services.operations.provisioning import provision_dedicated_layout


def _environment(
    root: Path,
    *,
    customer: str = "agency-a",
    workspace: str = "agency-a-production",
) -> dict[str, str]:
    return {
        "BRIDGEGUARD_ENV": "production",
        "BRIDGEGUARD_DEPLOYMENT_MODE": "dedicated_customer",
        "BRIDGEGUARD_WORKSPACE_ID": workspace,
        "BRIDGEGUARD_CUSTOMER_SLUG": customer,
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


def _provision_and_bootstrap(
    root: Path,
    *,
    customer: str = "agency-a",
    workspace: str = "agency-a-production",
):
    provision_dedicated_layout(
        root,
        customer_slug=customer,
        workspace_id=workspace,
    )
    environment = _environment(root, customer=customer, workspace=workspace)
    receipt = bootstrap_dedicated_database(root, environ=environment)
    return environment, receipt


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_empty_provision_bootstrap_is_bound_migrated_and_idempotent(tmp_path: Path):
    root = tmp_path / "customer-a"
    environment, first = _provision_and_bootstrap(root)
    database = root / "snbi_bridges.db"

    assert first.created is True
    assert first.workspace_id == "agency-a-production"
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (
            LATEST_SCHEMA_VERSION,
        )
        assert connection.execute(
            "SELECT customer_slug,workspace_id FROM deployment_bindings"
        ).fetchone() == ("agency-a", "agency-a-production")
        assert connection.execute("SELECT id FROM workspaces").fetchall() == [
            ("agency-a-production",)
        ]

    before_digest = _sha256(database)
    before_mtime = database.stat().st_mtime_ns
    second = bootstrap_dedicated_database(root, environ=environment)
    assert second.created is False
    assert second.deployment_id == first.deployment_id
    assert _sha256(database) == before_digest
    assert database.stat().st_mtime_ns == before_mtime


def test_database_binding_is_singleton_and_immutable(tmp_path: Path):
    root = tmp_path / "customer-a"
    _environment_values, receipt = _provision_and_bootstrap(root)
    database = root / "snbi_bridges.db"
    with closing(sqlite3.connect(database)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE deployment_bindings SET customer_slug='changed'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM deployment_bindings")
        with pytest.raises(sqlite3.IntegrityError, match="already exists"):
            connection.execute(
                "INSERT INTO deployment_bindings "
                "(deployment_id,customer_slug,workspace_id,marker_sha256,bound_at) "
                "VALUES ('00000000-0000-4000-8000-000000000000','agency-a',?,?,?)",
                (receipt.workspace_id, receipt.marker_sha256, "2026-08-13T00:00:00Z"),
            )


def test_wrong_customer_database_is_rejected_without_mutation(tmp_path: Path):
    source = tmp_path / "customer-a"
    _provision_and_bootstrap(source)
    destination = tmp_path / "customer-b"
    provision_dedicated_layout(
        destination,
        customer_slug="agency-b",
        workspace_id="agency-b-production",
    )
    copied = destination / "snbi_bridges.db"
    shutil.copy2(source / "snbi_bridges.db", copied)
    policy = evaluate_deployment_policy(
        environ=_environment(
            destination,
            customer="agency-b",
            workspace="agency-b-production",
        ),
        data_directory=destination,
    )
    before = (_sha256(copied), copied.stat().st_mtime_ns)

    with pytest.raises(
        DeploymentDatabaseBindingError,
        match="dedicated_database_binding_mismatch",
    ):
        verify_dedicated_database_binding(copied, policy)

    assert (_sha256(copied), copied.stat().st_mtime_ns) == before


@pytest.mark.parametrize(
    ("workspace_value", "error"),
    [
        (None, "dedicated_database_null_workspace:bridge_attachments"),
        ("agency-b-production", "dedicated_database_foreign_workspace:bridge_attachments"),
    ],
)
def test_attachment_workspace_contamination_is_never_adopted(
    tmp_path: Path,
    workspace_value: str | None,
    error: str,
):
    root = tmp_path / "customer-a"
    environment, _receipt = _provision_and_bootstrap(root)
    database = root / "snbi_bridges.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "INSERT INTO bridge_attachments "
            "(state_code,bridge_number,attachment_type,filename,filepath,workspace_id) "
            "VALUES ('34','1','inspection_report','r.pdf','attachments/r.pdf',?)",
            (workspace_value,),
        )
        connection.commit()
    policy = evaluate_deployment_policy(environ=environment, data_directory=root)
    before = _sha256(database)

    with pytest.raises(DeploymentDatabaseBindingError, match=error):
        verify_dedicated_database_binding(database, policy)

    assert _sha256(database) == before


def test_bootstrap_rejects_nonempty_provisioned_layout_without_creating_database(
    tmp_path: Path,
):
    root = tmp_path / "customer-a"
    provision_dedicated_layout(
        root,
        customer_slug="agency-a",
        workspace_id="agency-a-production",
    )
    (root / "imports" / "research.csv").write_text("legacy", encoding="utf-8")

    with pytest.raises(
        DeploymentDatabaseBindingError,
        match="dedicated_bootstrap_layout_not_empty",
    ):
        bootstrap_dedicated_database(root, environ=_environment(root))

    assert not (root / "snbi_bridges.db").exists()
