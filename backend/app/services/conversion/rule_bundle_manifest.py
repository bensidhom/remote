"""Deterministic provenance manifest for one BridgeGuard standards profile.

The manifest is deliberately metadata-only.  It binds an application profile
to the exact local FHWA registry bytes, the registry's direct and inherited
source artifacts, and the versions of BridgeGuard code that can apply or
validate transition rules.  It does *not* claim that a registered artifact was
executed, that FHWA validation occurred, or that a revision is submission
ready.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.standards.fhwa_registry import (
    DEFAULT_REGISTRY_PATH,
    ArtifactReference,
    RuleBundleRegistry,
    load_rule_bundle_registry,
)

from .canonical_json import canonical_sha256
from .canonical_validation import CANONICAL_VALIDATION_ADAPTER_VERSION
from .revision_validation import REVISION_VALIDATOR_VERSION
from .transition_engine import ENGINE_VERSION
from .transition_values import TRANSFORM_PRIMITIVES_VERSION
from .validation_profiles import VALIDATION_ORCHESTRATOR_VERSION


RULE_BUNDLE_MANIFEST_FORMAT = "bridgeguard-rule-bundle-manifest-v1"
RULE_SOURCE_BINDING_VERSION = "fhwa-crosswalk-source-binding-v1"


@dataclass(frozen=True, slots=True)
class RuleBundleManifest:
    """An immutable manifest and its canonical digest."""

    bundle_id: str
    sha256: str
    manifest: dict[str, Any]


@lru_cache(maxsize=1)
def _default_registry_and_digest() -> tuple[RuleBundleRegistry, str]:
    registry = load_rule_bundle_registry()
    digest = hashlib.sha256(DEFAULT_REGISTRY_PATH.read_bytes()).hexdigest()
    return registry, digest


def _artifact_entry(value: ArtifactReference) -> dict[str, Any]:
    """Return source identity without a host filesystem path or mutable URL."""

    return {
        "artifact_id": value.id,
        "title": value.title,
        "role": value.role,
        "authority": value.authority,
        "legal_status": value.legal_status.value,
        "trust_state": value.state.value,
        "sha256": value.sha256,
        "published_on": value.published_on.isoformat() if value.published_on else None,
        "published_label": value.published_label,
        "updated_on": value.updated_on.isoformat() if value.updated_on else None,
        "parser_version": value.parser_version,
        "compiler_version": value.compiler_version,
    }


def build_rule_bundle_manifest(
    profile_id: str,
    *,
    registry: RuleBundleRegistry | None = None,
) -> RuleBundleManifest:
    """Build a canonical, profile-specific standards/application manifest.

    ``registry.bundle`` is intentionally used instead of ``registry.select``:
    research storage must preserve the exact profile metadata even when one or
    more sources are remote/unverified.  The resulting authority boundary is
    explicit and fail-closed.
    """

    profile = str(profile_id).strip()
    if not profile:
        raise ValueError("profile_id is required")
    if registry is None:
        selected_registry, registry_sha256 = _default_registry_and_digest()
    else:
        selected_registry = registry
        # A supplied registry is primarily a test seam.  Its dataclasses do not
        # retain a source path, so the default registry-byte digest must not be
        # misattributed to it.  Hash the complete parsed identity instead.
        registry_sha256 = canonical_sha256(
            {
                "registry_id": selected_registry.registry_id,
                "format_version": selected_registry.format_version,
                "observed_on": selected_registry.observed_on.isoformat(),
                "artifacts": [
                    _artifact_entry(value)
                    for value in sorted(
                        selected_registry.artifacts.values(), key=lambda item: item.id
                    )
                ],
                "bundles": [
                    {
                        "id": value.id,
                        "profile_kind": value.profile_kind.value,
                        "legal_status": value.legal_status.value,
                        "compliance_use": value.compliance_use.value,
                        "implementation_status": value.implementation_status.value,
                        "enabled": value.enabled,
                        "effective_submission_years": list(
                            value.effective_submission_years
                        ),
                        "artifact_ids": list(value.artifact_ids),
                        "inherits": list(value.inherits),
                    }
                    for value in sorted(
                        selected_registry.bundles.values(), key=lambda item: item.id
                    )
                ],
            }
        )
    bundle = selected_registry.bundle(profile)
    artifacts = tuple(selected_registry.artifacts_for(bundle.id))
    application_versions = {
        "transition_engine": ENGINE_VERSION,
        "transition_primitives": TRANSFORM_PRIMITIVES_VERSION,
        "crosswalk_source_binding": RULE_SOURCE_BINDING_VERSION,
        "canonical_validation_adapter": CANONICAL_VALIDATION_ADAPTER_VERSION,
        "validation_orchestrator": VALIDATION_ORCHESTRATOR_VERSION,
        "revision_validator": REVISION_VALIDATOR_VERSION,
    }
    manifest: dict[str, Any] = {
        "format": RULE_BUNDLE_MANIFEST_FORMAT,
        "profile_id": profile,
        "rule_bundle_id": bundle.id,
        "profile_kind": bundle.profile_kind.value,
        "legal_status": bundle.legal_status.value,
        "compliance_use": bundle.compliance_use.value,
        "implementation_status": bundle.implementation_status.value,
        "enabled": bundle.enabled,
        "effective_submission_years": list(bundle.effective_submission_years),
        "artifact_registry": {
            "registry_id": selected_registry.registry_id,
            "format_version": selected_registry.format_version,
            "observed_on": selected_registry.observed_on.isoformat(),
            "sha256": registry_sha256,
        },
        "source_artifacts": [_artifact_entry(value) for value in artifacts],
        "rule_application_versions": application_versions,
        "rule_application_sha256": canonical_sha256(application_versions),
        "authority_boundary": {
            "artifact_registration_only": True,
            "verified_source_bytes_are_not_verified_rule_execution": True,
            "external_validation": False,
            "official_fhwa_nbtis_validation": False,
            "submission_ready": False,
        },
    }
    return RuleBundleManifest(
        bundle_id=bundle.id,
        sha256=canonical_sha256(manifest),
        manifest=manifest,
    )


__all__ = [
    "RULE_BUNDLE_MANIFEST_FORMAT",
    "RULE_SOURCE_BINDING_VERSION",
    "RuleBundleManifest",
    "build_rule_bundle_manifest",
]
