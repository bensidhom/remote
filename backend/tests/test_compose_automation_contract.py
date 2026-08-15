from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_every_runtime_helper_requires_and_uses_one_compose_environment_file():
    for relative in (
        "deploy/oci/preflight.sh",
        "deploy/oci/backup.sh",
        "deploy/oci/wait-ready.sh",
        "deploy/oci/verify.sh",
    ):
        source = _read(relative)
        assert "BRIDGEGUARD_COMPOSE_ENV_FILE" in source, relative
        assert 'docker compose --env-file "$compose_env"' in source, relative
        # Runtime calls go through compose(); the only direct call may be the
        # harmless `docker compose version` feature check in preflight.
        direct = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith("docker compose")
            and "--env-file" not in line
            and "compose version" not in line
        ]
        assert direct == [], (relative, direct)


def test_deploy_workflow_uses_configured_or_legacy_research_compose_environment():
    source = _read(".github/workflows/deploy-oci.yml")
    assert "vars.BRIDGEGUARD_COMPOSE_ENV_FILE" in source
    assert "vars.BRIDGEGUARD_CUSTOMER_SLUG" in source
    assert "'$COMPOSE_ENV_FILE'" in source
    assert 'compose_env_candidate="$2"' in source
    assert 'export BRIDGEGUARD_COMPOSE_ENV_FILE="$compose_env_candidate"' in source
    assert "BRIDGEGUARD_DATA_DIR_HOST=/srv/bridgeguard/data" in source
    assert 'docker compose --env-file "$BRIDGEGUARD_COMPOSE_ENV_FILE"' in source
    assert 'sudo -n env BRIDGEGUARD_COMPOSE_ENV_FILE="$BRIDGEGUARD_COMPOSE_ENV_FILE"' in source
    remote = source[source.index("REMOTE'") :]
    assert "docker compose -f deploy/oci/compose.yaml" not in remote


def test_preflight_checks_rendered_customer_data_mount():
    source = _read("deploy/oci/preflight.sh")
    assert "rendered_data_source" in source
    assert 'm.get("target")=="/data"' in source
    assert "rendered /data bind does not match" in source
