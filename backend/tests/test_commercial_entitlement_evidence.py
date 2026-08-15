from __future__ import annotations

import json
from pathlib import Path

from app.commercial_evidence import evaluate_commercial_entitlement_evidence
from scripts.check_commercial_entitlements import main


def _approved_values() -> dict[str, str]:
    return {
        "BRIDGEGUARD_PDF_ENGINE_DISPOSITION": "permissive_replacement",
        "BRIDGEGUARD_PDF_ENGINE_RECEIPT_ID": "LEGAL-PDF-2026-014",
        "BRIDGEGUARD_AI_PROVIDER_ID": "cerebras",
        "BRIDGEGUARD_AI_MODEL_ID": "gemma-4-31b",
        "CEREBRAS_MODEL": "gemma-4-31b",
        "BRIDGEGUARD_AI_PROVIDER_ENTITLEMENT_RECEIPT_ID": "PROCURE-AI-2026-009",
        "BRIDGEGUARD_AI_DPA_RECEIPT_ID": "PRIVACY-DPA-2026-007",
        "BRIDGEGUARD_MAP_SERVICE_MODE": "arcgis_sdk_keyless_osm",
        "BRIDGEGUARD_MAP_SERVICE_RECEIPT_ID": "MAP-POLICY-2026-006",
        "BRIDGEGUARD_CLOUDFLARE_SERVICE_RECEIPT_ID": "EDGE-ORDER-2026-005",
        "BRIDGEGUARD_OSS_NOTICES_RECEIPT_ID": "OSS-NOTICES-2026-004",
        "BRIDGEGUARD_SBOM_RECEIPT_ID": "SBOM-RELEASE-2026-003",
    }


def test_safe_evidence_object_does_not_retain_receipt_identifiers():
    values = _approved_values()
    evidence = evaluate_commercial_entitlement_evidence(values)
    assert evidence.approved
    rendered_object = repr(evidence)
    rendered_public = json.dumps(evidence.public_dict())
    for key in (
        "BRIDGEGUARD_PDF_ENGINE_RECEIPT_ID",
        "BRIDGEGUARD_AI_PROVIDER_ENTITLEMENT_RECEIPT_ID",
        "BRIDGEGUARD_AI_DPA_RECEIPT_ID",
        "BRIDGEGUARD_MAP_SERVICE_RECEIPT_ID",
        "BRIDGEGUARD_CLOUDFLARE_SERVICE_RECEIPT_ID",
        "BRIDGEGUARD_OSS_NOTICES_RECEIPT_ID",
        "BRIDGEGUARD_SBOM_RECEIPT_ID",
    ):
        assert values[key] not in rendered_object
        assert values[key] not in rendered_public
    assert evidence.public_dict()["legal_clearance_claimed"] is False


def test_preflight_cli_reports_only_safe_evidence(tmp_path: Path, capsys):
    values = _approved_values()
    values["CEREBRAS_API_KEY"] = "provider-secret-must-not-print"
    env_file = tmp_path / "customer.env"
    env_file.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )
    assert main([str(env_file)]) == 0
    captured = capsys.readouterr()
    assert "provider-secret-must-not-print" not in captured.out
    assert "LEGAL-PDF-2026-014" not in captured.out
    assert json.loads(captured.out)["approved"] is True
    assert captured.err == ""


def test_preflight_cli_failure_emits_codes_not_values(tmp_path: Path, capsys):
    values = _approved_values()
    values["BRIDGEGUARD_AI_DPA_RECEIPT_ID"] = "super-secret/path"
    env_file = tmp_path / "customer.env"
    env_file.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )
    assert main([str(env_file)]) == 2
    captured = capsys.readouterr()
    assert "ai_dpa_receipt_id_invalid" in captured.err
    assert "super-secret/path" not in captured.err
    assert str(env_file) not in captured.err
