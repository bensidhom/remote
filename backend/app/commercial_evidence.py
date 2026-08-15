"""Syntactic gate for operator-recorded commercial entitlement evidence.

The application cannot determine whether a contract, software licence, DPA,
or legal review is substantively sufficient.  It can, however, refuse a paid
deployment until the operator records references to those external decisions
and binds the declared AI provider/model to the runtime configuration.

Receipt identifiers are deliberately never retained on the returned object or
included in public readiness output.  They may identify internal legal,
procurement, or security records and must remain server-side.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping


COMMERCIAL_EVIDENCE_POLICY_VERSION = (
    "bridgeguard-commercial-entitlement-evidence-v1"
)
GENERATION_PROVIDER_ID = "cerebras"
PDF_ENGINE_DISPOSITIONS = frozenset(
    {
        "permissive_replacement",
        "commercial_license",
        "approved_agpl_compliance",
    }
)
MAP_SERVICE_MODES = frozenset({"arcgis_premium", "arcgis_sdk_keyless_osm"})

_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{1,127}$")
_RECEIPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$")
_PLACEHOLDERS = frozenset(
    {
        "none",
        "n-a",
        "na",
        "pending",
        "placeholder",
        "tbd",
        "todo",
        "unknown",
    }
)
_PLACEHOLDER_PREFIXES = (
    "dummy-",
    "example-",
    "replace-",
    "sample-",
    "test-",
    "your-",
)


@dataclass(frozen=True, slots=True)
class CommercialEntitlementEvidence:
    """Safe readiness facts; raw receipt identifiers are intentionally absent."""

    version: str
    pdf_engine_disposition: str | None
    pdf_engine_receipt_recorded: bool
    ai_provider_identity_matched: bool
    ai_model_identity_matched: bool
    ai_provider_entitlement_receipt_recorded: bool
    ai_dpa_receipt_recorded: bool
    map_service_mode: str | None
    map_service_receipt_recorded: bool
    cloudflare_service_receipt_recorded: bool
    oss_notices_receipt_recorded: bool
    sbom_receipt_recorded: bool
    blockers: tuple[str, ...]

    @property
    def approved(self) -> bool:
        return not self.blockers

    def public_dict(self) -> dict[str, object]:
        """Return only safe booleans/enums, never receipt IDs or env values."""

        return {
            "version": self.version,
            "pdf_engine_disposition": self.pdf_engine_disposition,
            "pdf_engine_receipt_recorded": self.pdf_engine_receipt_recorded,
            "ai_provider_identity_matched": self.ai_provider_identity_matched,
            "ai_model_identity_matched": self.ai_model_identity_matched,
            "ai_provider_entitlement_receipt_recorded": (
                self.ai_provider_entitlement_receipt_recorded
            ),
            "ai_dpa_receipt_recorded": self.ai_dpa_receipt_recorded,
            "map_service_mode": self.map_service_mode,
            "map_service_receipt_recorded": self.map_service_receipt_recorded,
            "cloudflare_service_receipt_recorded": (
                self.cloudflare_service_receipt_recorded
            ),
            "oss_notices_receipt_recorded": self.oss_notices_receipt_recorded,
            "sbom_receipt_recorded": self.sbom_receipt_recorded,
            "approved": self.approved,
            "operator_record_only": True,
            "legal_clearance_claimed": False,
        }


def _receipt_recorded(
    environ: Mapping[str, str],
    *,
    key: str,
    label: str,
    blockers: list[str],
) -> bool:
    value = str(environ.get(key, "")).strip()
    if not value:
        blockers.append(f"{label}_missing")
        return False
    lowered = value.lower()
    if (
        not _RECEIPT_ID.fullmatch(value)
        or lowered in _PLACEHOLDERS
        or lowered.startswith(_PLACEHOLDER_PREFIXES)
    ):
        blockers.append(f"{label}_invalid")
        return False
    return True


def evaluate_commercial_entitlement_evidence(
    environ: Mapping[str, str],
) -> CommercialEntitlementEvidence:
    """Validate recorded evidence without interpreting its legal sufficiency."""

    blockers: list[str] = []

    raw_disposition = str(
        environ.get("BRIDGEGUARD_PDF_ENGINE_DISPOSITION", "")
    ).strip().lower()
    if not raw_disposition:
        blockers.append("pdf_engine_disposition_missing")
        pdf_disposition: str | None = None
    elif raw_disposition not in PDF_ENGINE_DISPOSITIONS:
        blockers.append("pdf_engine_disposition_invalid")
        pdf_disposition = None
    else:
        pdf_disposition = raw_disposition
    pdf_receipt = _receipt_recorded(
        environ,
        key="BRIDGEGUARD_PDF_ENGINE_RECEIPT_ID",
        label="pdf_engine_receipt_id",
        blockers=blockers,
    )

    provider_id = str(environ.get("BRIDGEGUARD_AI_PROVIDER_ID", "")).strip().lower()
    if not provider_id:
        blockers.append("ai_provider_id_missing")
        provider_valid = False
    elif not _PROVIDER_ID.fullmatch(provider_id):
        blockers.append("ai_provider_id_invalid")
        provider_valid = False
    else:
        provider_valid = True
    provider_matched = provider_valid and provider_id == GENERATION_PROVIDER_ID
    if provider_valid and not provider_matched:
        blockers.append("ai_provider_runtime_mismatch")

    model_id = str(environ.get("BRIDGEGUARD_AI_MODEL_ID", "")).strip()
    if not model_id:
        blockers.append("ai_model_id_missing")
        model_valid = False
    elif not _MODEL_ID.fullmatch(model_id):
        blockers.append("ai_model_id_invalid")
        model_valid = False
    else:
        model_valid = True
    runtime_model = str(environ.get("CEREBRAS_MODEL", "")).strip()
    if not runtime_model:
        blockers.append("ai_runtime_model_missing")
        model_matched = False
    else:
        model_matched = model_valid and model_id == runtime_model
        if model_valid and not model_matched:
            blockers.append("ai_model_runtime_mismatch")

    provider_receipt = _receipt_recorded(
        environ,
        key="BRIDGEGUARD_AI_PROVIDER_ENTITLEMENT_RECEIPT_ID",
        label="ai_provider_entitlement_receipt_id",
        blockers=blockers,
    )
    dpa_receipt = _receipt_recorded(
        environ,
        key="BRIDGEGUARD_AI_DPA_RECEIPT_ID",
        label="ai_dpa_receipt_id",
        blockers=blockers,
    )

    raw_map_mode = str(
        environ.get("BRIDGEGUARD_MAP_SERVICE_MODE", "")
    ).strip().lower()
    if not raw_map_mode:
        blockers.append("map_service_mode_missing")
        map_mode: str | None = None
    elif raw_map_mode not in MAP_SERVICE_MODES:
        blockers.append("map_service_mode_invalid")
        map_mode = None
    else:
        map_mode = raw_map_mode
    map_receipt = _receipt_recorded(
        environ,
        key="BRIDGEGUARD_MAP_SERVICE_RECEIPT_ID",
        label="map_service_receipt_id",
        blockers=blockers,
    )
    cloudflare_receipt = _receipt_recorded(
        environ,
        key="BRIDGEGUARD_CLOUDFLARE_SERVICE_RECEIPT_ID",
        label="cloudflare_service_receipt_id",
        blockers=blockers,
    )
    oss_notices_receipt = _receipt_recorded(
        environ,
        key="BRIDGEGUARD_OSS_NOTICES_RECEIPT_ID",
        label="oss_notices_receipt_id",
        blockers=blockers,
    )
    sbom_receipt = _receipt_recorded(
        environ,
        key="BRIDGEGUARD_SBOM_RECEIPT_ID",
        label="sbom_receipt_id",
        blockers=blockers,
    )

    return CommercialEntitlementEvidence(
        version=COMMERCIAL_EVIDENCE_POLICY_VERSION,
        pdf_engine_disposition=pdf_disposition,
        pdf_engine_receipt_recorded=pdf_receipt,
        ai_provider_identity_matched=provider_matched,
        ai_model_identity_matched=model_matched,
        ai_provider_entitlement_receipt_recorded=provider_receipt,
        ai_dpa_receipt_recorded=dpa_receipt,
        map_service_mode=map_mode,
        map_service_receipt_recorded=map_receipt,
        cloudflare_service_receipt_recorded=cloudflare_receipt,
        oss_notices_receipt_recorded=oss_notices_receipt,
        sbom_receipt_recorded=sbom_receipt,
        blockers=tuple(dict.fromkeys(blockers)),
    )


__all__ = [
    "COMMERCIAL_EVIDENCE_POLICY_VERSION",
    "GENERATION_PROVIDER_ID",
    "MAP_SERVICE_MODES",
    "PDF_ENGINE_DISPOSITIONS",
    "CommercialEntitlementEvidence",
    "evaluate_commercial_entitlement_evidence",
]
