"""Public result and rollout contracts for point-cloud processing.

This module deliberately does not implement geometry.  It freezes the meaning
of every research result before the replacement pipeline is introduced, and it
prevents a protected deployment from silently falling back to the legacy
research implementation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import os
from types import MappingProxyType
from typing import Any, Mapping


RESULT_CONTRACT_VERSION = "bridgeguard-point-cloud-result-v1"
LEGACY_IMPLEMENTATION_VERSION = "legacy-research-v2"
HARDENED_IMPLEMENTATION_VERSION = "hardened-exact-revision-v1"
SCREENING_WARNING = (
    "Screening estimate only — not a survey, accepted SNBI value, or engineering determination."
)


class PointCloudPipelineMode(str, Enum):
    LEGACY_RESEARCH = "legacy_research"
    HARDENED_SHADOW = "hardened_shadow"
    REQUIRED = "required"


class MeasurementDisposition(str, Enum):
    SCREENING_ESTIMATE = "screening_estimate"
    NOT_MEASURABLE = "not_measurable"


class UnitsStatus(str, Enum):
    CLIENT_ASSERTED_UNVERIFIED = "client_asserted_unverified"
    UNKNOWN_OR_RELATIVE = "unknown_or_relative"


class GravityStatus(str, Enum):
    ASSUMED_Z_UP_UNVERIFIED = "assumed_z_up_unverified"


class SourceIdentityStatus(str, Enum):
    SOURCE_HASH_AND_SNAPSHOT_BOUND = "source_hash_and_snapshot_bound"
    SOURCE_HASH_BOUND = "source_hash_bound"
    INLINE_REQUEST_SNAPSHOT_BOUND = "inline_request_snapshot_bound"
    INLINE_REQUEST_UNRETAINED = "inline_request_unretained"


class ReviewStatus(str, Enum):
    UNREVIEWED = "unreviewed"


class PipelineConfigurationError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.public_message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PointCloudResultContract:
    contract_version: str
    pipeline_mode: str
    implementation: str
    disposition: str
    measurement_authority: str
    units_status: str
    gravity_status: str
    source_identity_status: str
    review_status: str
    submission_ready: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _protected_deployment(environ: Mapping[str, str]) -> bool:
    return (
        str(environ.get("BRIDGEGUARD_ENV", "development")).strip().lower()
        == "production"
        or str(
            environ.get("BRIDGEGUARD_DEPLOYMENT_MODE", "research_shared")
        ).strip().lower()
        == "dedicated_customer"
    )


def resolve_point_cloud_pipeline_mode(
    environ: Mapping[str, str] | None = None,
) -> PointCloudPipelineMode:
    """Resolve the server-owned mode and fail closed for protected deployments."""

    values: Mapping[str, str] = (
        MappingProxyType(dict(os.environ)) if environ is None else environ
    )
    protected = _protected_deployment(values)
    configured = str(values.get("POINT_CLOUD_PIPELINE_MODE", "")).strip().lower()
    if not configured:
        return (
            PointCloudPipelineMode.REQUIRED
            if protected
            else PointCloudPipelineMode.LEGACY_RESEARCH
        )
    try:
        selected = PointCloudPipelineMode(configured)
    except ValueError as exc:
        raise PipelineConfigurationError(
            "point_cloud_pipeline_mode_invalid",
            "Point-cloud pipeline mode is invalid.",
        ) from exc
    if protected and selected is not PointCloudPipelineMode.REQUIRED:
        raise PipelineConfigurationError(
            "point_cloud_required_pipeline_enforced",
            "Protected deployments require the hardened point-cloud pipeline.",
        )
    return selected


def require_available_point_cloud_pipeline(
    environ: Mapping[str, str] | None = None,
) -> PointCloudPipelineMode:
    """Return the active implementation selected by the protected rollout gate."""

    return resolve_point_cloud_pipeline_mode(environ)


def _disposition(payload: Mapping[str, Any]) -> MeasurementDisposition:
    return (
        MeasurementDisposition.SCREENING_ESTIMATE
        if str(payload.get("status") or "").strip().lower() == "success"
        else MeasurementDisposition.NOT_MEASURABLE
    )


def annotate_point_cloud_result(
    payload: Mapping[str, Any],
    *,
    pipeline_mode: PointCloudPipelineMode,
    units_status: UnitsStatus,
    gravity_status: GravityStatus,
    source_identity_status: SourceIdentityStatus,
) -> dict[str, Any]:
    """Return a copied JSON-ready result with an immutable semantic contract."""

    result = dict(payload)
    contract = PointCloudResultContract(
        contract_version=RESULT_CONTRACT_VERSION,
        pipeline_mode=pipeline_mode.value,
        implementation=(
            LEGACY_IMPLEMENTATION_VERSION
            if pipeline_mode is PointCloudPipelineMode.LEGACY_RESEARCH
            else HARDENED_IMPLEMENTATION_VERSION
        ),
        disposition=_disposition(payload).value,
        measurement_authority="research_screening_not_survey_or_snbi",
        units_status=units_status.value,
        gravity_status=gravity_status.value,
        source_identity_status=source_identity_status.value,
        review_status=ReviewStatus.UNREVIEWED.value,
        submission_ready=False,
    )
    result["result_contract"] = contract.to_dict()
    warnings = [str(item) for item in result.get("warnings") or []]
    if SCREENING_WARNING not in warnings:
        warnings.append(SCREENING_WARNING)
    result["warnings"] = warnings
    return result


__all__ = [
    "GravityStatus",
    "HARDENED_IMPLEMENTATION_VERSION",
    "MeasurementDisposition",
    "PipelineConfigurationError",
    "PointCloudPipelineMode",
    "PointCloudResultContract",
    "RESULT_CONTRACT_VERSION",
    "ReviewStatus",
    "SCREENING_WARNING",
    "SourceIdentityStatus",
    "UnitsStatus",
    "annotate_point_cloud_result",
    "require_available_point_cloud_pipeline",
    "resolve_point_cloud_pipeline_mode",
]
