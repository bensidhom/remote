from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "deploy" / "oci" / "compose.yaml"
PREFLIGHT_FILE = REPO_ROOT / "deploy" / "oci" / "preflight.sh"
VERIFY_FILE = REPO_ROOT / "deploy" / "oci" / "verify.sh"
DEPLOY_WORKFLOW_FILE = REPO_ROOT / ".github" / "workflows" / "deploy-oci.yml"
IDENTITY_KEYS = (
    "BRIDGEGUARD_DEPLOYMENT_MODE",
    "BRIDGEGUARD_WORKSPACE_ID",
    "BRIDGEGUARD_CUSTOMER_SLUG",
    "BRIDGEGUARD_WORKER_COUNT",
)


def _environment_block(text: str) -> str:
    start = text.index("    environment:\n", text.index("  app:\n"))
    end = text.index("    volumes:\n", start)
    return text[start:end]


def test_compose_forces_research_mode_but_not_workspace_identity():
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    environment = _environment_block(text)
    for key in IDENTITY_KEYS[1:]:
        assert f"      {key}:" not in environment

    assert "      BRIDGEGUARD_DEPLOYMENT_MODE: research_shared" in environment
    assert '      BRIDGEGUARD_REQUIRE_COMMERCIAL_READY: "false"' in environment
    assert "      BRIDGEGUARD_QUERY_MODE: hybrid" in environment

    assert "      BRIDGEGUARD_DATA_DIR: /data" in environment
    assert "      RAG_WORKING_DIR: /data/rag_storage" in environment
    assert "      SNBI_TEMP_DIR: /data/temp" in environment

    ai_position = text.index("${BRIDGEGUARD_AI_ENV_FILE")
    customer_position = text.index("${BRIDGEGUARD_ENV_FILE")
    assert ai_position < customer_position


def test_preflight_labels_research_and_does_not_require_commercial_receipts():
    script = PREFLIGHT_FILE.read_text(encoding="utf-8")
    assert "check_commercial_entitlements.py" not in script
    assert "research deployment; commercial entitlement gate not applicable" in script
    assert "app environment must declare customer and workspace identity" not in script
    assert "dedicated deployment marker" not in script
    assert "database/customer binding mismatch" not in script
    assert "database migration generation mismatch" not in script


def test_deploy_uses_the_exact_database_without_a_publication_cutover():
    workflow = DEPLOY_WORKFLOW_FILE.read_text(encoding="utf-8")

    backup_index = workflow.index("sh deploy/oci/backup.sh")
    restart_index = workflow.index("-d --remove-orphans embeddings app", backup_index)
    assert "publish-legacy-data.sh" not in workflow
    assert backup_index < restart_index
    cleanup_function = workflow[workflow.index("cleanup_deploy()") :]
    recovery_index = cleanup_function.index("compose up -d --remove-orphans embeddings app")
    cleanup_index = cleanup_function.index("cleanup_compose_env", recovery_index)
    assert recovery_index < cleanup_index


def test_deployment_verification_enforces_planner_and_loaded_database():
    script = VERIFY_FILE.read_text(encoding="utf-8")

    assert "mode == 'hybrid'" in script
    assert "SELECT COUNT(*) FROM bridges" in script
    assert "SELECT DISTINCT data_year FROM v_current_bridge_snapshots" in script
    assert "semantic_count == loaded" in script
    assert "current_years == [latest]" in script


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose is unavailable")
def test_docker_compose_forces_research_policy_and_preserves_workspace_values(
    tmp_path: Path,
):
    app_env = tmp_path / "customer.env"
    app_env.write_text(
        "\n".join(
            (
                "BRIDGEGUARD_DEPLOYMENT_MODE=dedicated_customer",
                "BRIDGEGUARD_WORKSPACE_ID=agency-production",
                "BRIDGEGUARD_CUSTOMER_SLUG=agency",
                "BRIDGEGUARD_WORKER_COUNT=1",
                "BRIDGEGUARD_REQUIRE_COMMERCIAL_READY=true",
                "BRIDGEGUARD_OWNER_EMAIL=owner@agency.example",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    ai_env = tmp_path / "ai.env"
    # The customer file is intentionally later and must win duplicate keys.
    ai_env.write_text("BRIDGEGUARD_WORKSPACE_ID=wrong-ai-workspace\n", encoding="utf-8")
    tunnel_env = tmp_path / "cloudflare.env"
    tunnel_env.write_text("TUNNEL_TOKEN=test-only\n", encoding="utf-8")

    environ = {
        **os.environ,
        "BRIDGEGUARD_ENV_FILE": str(app_env),
        "BRIDGEGUARD_AI_ENV_FILE": str(ai_env),
        "CLOUDFLARE_ENV_FILE": str(tunnel_env),
        "BRIDGEGUARD_DATA_DIR_HOST": str(tmp_path / "customer-data"),
        "BRIDGEGUARD_MODELS_DIR": str(tmp_path / "models"),
        # These hostile host-shell values caught the former ${VAR:-default}
        # precedence bug in the service-level environment block.
        "BRIDGEGUARD_DEPLOYMENT_MODE": "research_shared",
        "BRIDGEGUARD_WORKSPACE_ID": "host-shell-wrong",
        "BRIDGEGUARD_CUSTOMER_SLUG": "host-shell-wrong",
        "BRIDGEGUARD_WORKER_COUNT": "9",
        "BRIDGEGUARD_REQUIRE_COMMERCIAL_READY": "false",
    }
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "config",
            "--format",
            "json",
        ],
        cwd=REPO_ROOT,
        env=environ,
        capture_output=True,
        text=True,
        check=True,
    )
    rendered = json.loads(completed.stdout)
    app_environment = rendered["services"]["app"]["environment"]
    assert app_environment["BRIDGEGUARD_DEPLOYMENT_MODE"] == "research_shared"
    assert app_environment["BRIDGEGUARD_WORKSPACE_ID"] == "agency-production"
    assert app_environment["BRIDGEGUARD_CUSTOMER_SLUG"] == "agency"
    assert str(app_environment["BRIDGEGUARD_WORKER_COUNT"]) == "1"
    assert str(app_environment["BRIDGEGUARD_REQUIRE_COMMERCIAL_READY"]).lower() == "false"
    assert app_environment["BRIDGEGUARD_DATA_DIR"] == "/data"
    assert app_environment["RAG_WORKING_DIR"] == "/data/rag_storage"
    assert app_environment["SNBI_TEMP_DIR"] == "/data/temp"
    data_mount = next(
        mount for mount in rendered["services"]["app"]["volumes"]
        if mount["target"] == "/data"
    )
    assert Path(data_mount["source"]).resolve() == (tmp_path / "customer-data").resolve()
