"""Immutable, local-only registry for FHWA SNBI rule-bundle metadata.

This module deliberately does not download standards artifacts.  An artifact can
only be treated as verified when it is present below the registry directory and
its declared SHA-256 digest matches its bytes.  Remote links are provenance
metadata, not executable inputs.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit


REGISTRY_FORMAT_VERSION = 1
DEFAULT_REGISTRY_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "fhwa" / "registry.json"
)

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RegistryError(RuntimeError):
    """Base error for invalid registry data or unsafe bundle selection."""


class UnknownRuleBundleError(RegistryError):
    """Raised when a requested bundle identifier is not registered."""


class DisabledRuleBundleError(RegistryError):
    """Raised when code attempts to select a deliberately disabled bundle."""


class RuleBundleNotReadyError(RegistryError):
    """Raised when a bundle's governing artifacts are not locally verified."""


class ArtifactState(str, Enum):
    """The only supported trust states for a standards artifact."""

    VENDORED_VERIFIED = "vendored_verified"
    VENDORED_UNVERIFIED = "vendored_unverified"
    REMOTE_UNVERIFIED = "remote_unverified"
    UNAVAILABLE = "unavailable"


class ArtifactLegalStatus(str, Enum):
    REGULATORY_IBR = "regulatory_ibr"
    OPERATIONAL = "operational"
    INFORMATIONAL_PENDING_RULEMAKING = "informational_pending_rulemaking"
    LEGACY_REFERENCE = "legacy_reference"
    PENDING_PUBLICATION = "pending_publication"


class ProfileKind(str, Enum):
    REGULATORY_BASELINE = "regulatory_baseline"
    SUBMISSION = "submission"
    INFORMATIONAL_PREVIEW = "informational_preview"
    RESEARCH_IMPORT = "research_import"
    NATIVE_INGESTION = "native_ingestion"


class BundleLegalStatus(str, Enum):
    REGULATORY_IBR = "regulatory_ibr"
    OPERATIONAL_SUBMISSION = "operational_submission"
    INFORMATIONAL_PENDING_RULEMAKING = "informational_pending_rulemaking"
    INTERNAL_NON_SUBMISSION = "internal_non_submission"
    DISABLED_PENDING_PUBLICATION = "disabled_pending_publication"


class ComplianceUse(str, Enum):
    REGULATORY_BASELINE_COMPARISON = "regulatory_baseline_comparison"
    SUBMISSION_SUPPORT_DRAFT = (
        "submission_support_draft_external_verification_required"
    )
    INFORMATIONAL_ONLY = "informational_only"
    RESEARCH_ONLY = "research_only_not_for_submission"
    NATIVE_VALIDATION_ONLY = "native_ingestion_validation_only"
    DISABLED = "disabled"


class ImplementationStatus(str, Enum):
    """PR0 registers profiles but intentionally does not execute their rules."""

    REGISTRY_ONLY = "registry_only"


@dataclass(frozen=True)
class ArtifactReference:
    id: str
    title: str
    role: str
    authority: str
    legal_status: ArtifactLegalStatus
    state: ArtifactState
    source_url: Optional[str]
    published_on: Optional[date]
    published_label: Optional[str]
    updated_on: Optional[date]
    retrieved_on: Optional[date]
    reviewed_on: Optional[date]
    reviewed_by: Optional[str]
    parser_version: Optional[str]
    compiler_version: Optional[str]
    supersedes: Tuple[str, ...]
    superseded_by: Tuple[str, ...]
    media_type: Optional[str]
    local_path: Optional[str]
    sha256: Optional[str]
    notes: Tuple[str, ...]


@dataclass(frozen=True)
class RuleBundle:
    id: str
    title: str
    profile_kind: ProfileKind
    legal_status: BundleLegalStatus
    compliance_use: ComplianceUse
    implementation_status: ImplementationStatus
    enabled: bool
    effective_submission_years: Tuple[int, ...]
    artifact_ids: Tuple[str, ...]
    inherits: Tuple[str, ...]
    disabled_reason: Optional[str]
    notes: Tuple[str, ...]


@dataclass(frozen=True)
class RuleBundleRegistry:
    format_version: int
    registry_id: str
    observed_on: date
    artifacts: Mapping[str, ArtifactReference]
    bundles: Mapping[str, RuleBundle]

    def artifact(self, artifact_id: str) -> ArtifactReference:
        try:
            return self.artifacts[artifact_id]
        except KeyError as exc:
            raise RegistryError(f"unknown FHWA artifact: {artifact_id}") from exc

    def bundle(self, bundle_id: str) -> RuleBundle:
        """Return bundle metadata, including metadata for disabled bundles."""

        try:
            return self.bundles[bundle_id]
        except KeyError as exc:
            raise UnknownRuleBundleError(
                f"unknown FHWA rule bundle: {bundle_id}"
            ) from exc

    def artifacts_for(self, bundle_id: str) -> Tuple[ArtifactReference, ...]:
        """Return direct and inherited artifacts once, in deterministic order."""

        ordered_ids = []
        seen_artifacts = set()
        visited_bundles = set()

        def visit(current_id: str) -> None:
            if current_id in visited_bundles:
                return
            visited_bundles.add(current_id)
            current = self.bundle(current_id)
            for parent_id in current.inherits:
                visit(parent_id)
            for artifact_id in current.artifact_ids:
                if artifact_id not in seen_artifacts:
                    seen_artifacts.add(artifact_id)
                    ordered_ids.append(artifact_id)

        visit(bundle_id)
        return tuple(self.artifact(artifact_id) for artifact_id in ordered_ids)

    def unverified_artifact_ids(self, bundle_id: str) -> Tuple[str, ...]:
        return tuple(
            artifact.id
            for artifact in self.artifacts_for(bundle_id)
            if artifact.state is not ArtifactState.VENDORED_VERIFIED
        )

    def select(
        self, bundle_id: str, *, require_verified_artifacts: bool = True
    ) -> RuleBundle:
        """Fail closed when selecting a bundle for execution.

        Call :meth:`bundle` for metadata-only display.  Runtime callers should use
        this method and keep ``require_verified_artifacts`` at its safe default.
        """

        bundle = self.bundle(bundle_id)
        if not bundle.enabled:
            reason = bundle.disabled_reason or "disabled by registry policy"
            raise DisabledRuleBundleError(f"{bundle.id} is disabled: {reason}")

        if require_verified_artifacts:
            missing = self.unverified_artifact_ids(bundle.id)
            if missing:
                joined = ", ".join(missing)
                raise RuleBundleNotReadyError(
                    f"{bundle.id} is metadata-only until these local artifacts "
                    f"are verified: {joined}"
                )
        return bundle

    def select_submission_year(
        self, year: int, *, require_verified_artifacts: bool = True
    ) -> RuleBundle:
        candidates = [
            bundle
            for bundle in self.bundles.values()
            if bundle.profile_kind is ProfileKind.SUBMISSION
            and year in bundle.effective_submission_years
        ]
        if not candidates:
            raise UnknownRuleBundleError(
                f"no FHWA submission rule bundle is registered for {year}"
            )
        if len(candidates) != 1:
            raise RegistryError(
                f"multiple FHWA submission rule bundles are registered for {year}"
            )
        return self.select(
            candidates[0].id,
            require_verified_artifacts=require_verified_artifacts,
        )


def load_rule_bundle_registry(
    path: Optional[Path] = None,
) -> RuleBundleRegistry:
    """Load and validate a registry from local disk without network access."""

    registry_path = Path(path or DEFAULT_REGISTRY_PATH).resolve()
    try:
        raw_bytes = registry_path.read_bytes()
    except OSError as exc:
        raise RegistryError(f"cannot read FHWA registry: {registry_path}") from exc

    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryError(f"invalid UTF-8 JSON registry: {registry_path}") from exc

    root = _mapping(payload, "registry")
    _require_keys(
        root,
        {"format_version", "registry_id", "observed_on", "artifacts", "bundles"},
        "registry",
    )
    _reject_unknown_keys(
        root,
        {
            "$schema",
            "format_version",
            "registry_id",
            "observed_on",
            "artifacts",
            "bundles",
        },
        "registry",
    )
    format_version = _integer(root.get("format_version"), "registry.format_version")
    if format_version != REGISTRY_FORMAT_VERSION:
        raise RegistryError(
            f"unsupported FHWA registry format_version {format_version}; "
            f"expected {REGISTRY_FORMAT_VERSION}"
        )

    registry_id = _identifier(root.get("registry_id"), "registry.registry_id")
    observed_on = _date(root.get("observed_on"), "registry.observed_on")

    artifact_values = _sequence(root.get("artifacts"), "registry.artifacts")
    artifacts = {}
    for index, value in enumerate(artifact_values):
        context = f"registry.artifacts[{index}]"
        artifact = _parse_artifact(value, context, registry_path.parent)
        if artifact.id in artifacts:
            raise RegistryError(f"duplicate FHWA artifact id: {artifact.id}")
        artifacts[artifact.id] = artifact

    bundle_values = _sequence(root.get("bundles"), "registry.bundles")
    bundles = {}
    for index, value in enumerate(bundle_values):
        context = f"registry.bundles[{index}]"
        bundle = _parse_bundle(value, context)
        if bundle.id in bundles:
            raise RegistryError(f"duplicate FHWA rule bundle id: {bundle.id}")
        bundles[bundle.id] = bundle

    if not artifacts:
        raise RegistryError("FHWA registry must contain at least one artifact")
    if not bundles:
        raise RegistryError("FHWA registry must contain at least one rule bundle")

    _validate_bundle_graph(bundles, artifacts)
    _validate_artifact_supersession(artifacts)
    _validate_submission_year_index(bundles)

    return RuleBundleRegistry(
        format_version=format_version,
        registry_id=registry_id,
        observed_on=observed_on,
        artifacts=MappingProxyType(dict(artifacts)),
        bundles=MappingProxyType(dict(bundles)),
    )


def _parse_artifact(
    value: Any, context: str, registry_directory: Path
) -> ArtifactReference:
    item = _mapping(value, context)
    _require_keys(
        item,
        {
            "id",
            "title",
            "role",
            "authority",
            "legal_status",
            "state",
            "source_url",
            "published_on",
            "published_label",
            "updated_on",
            "media_type",
            "local_path",
            "sha256",
            "notes",
        },
        context,
    )
    _reject_unknown_keys(
        item,
        {
            "id",
            "title",
            "role",
            "authority",
            "legal_status",
            "state",
            "source_url",
            "published_on",
            "published_label",
            "updated_on",
            "retrieved_on",
            "reviewed_on",
            "reviewed_by",
            "parser_version",
            "compiler_version",
            "supersedes",
            "superseded_by",
            "media_type",
            "local_path",
            "sha256",
            "notes",
        },
        context,
    )

    artifact_id = _identifier(item.get("id"), f"{context}.id")
    state = _enum(ArtifactState, item.get("state"), f"{context}.state")
    source_url = _optional_https_url(
        item.get("source_url"), f"{context}.source_url"
    )
    local_path = _optional_string(item.get("local_path"), f"{context}.local_path")
    sha256 = _optional_string(item.get("sha256"), f"{context}.sha256")
    reviewed_on = _optional_date(item.get("reviewed_on"), f"{context}.reviewed_on")
    reviewed_by = _optional_string(item.get("reviewed_by"), f"{context}.reviewed_by")
    if (reviewed_on is None) != (reviewed_by is None):
        raise RegistryError(
            f"{context}.reviewed_on and reviewed_by must be declared together"
        )

    if sha256 is not None and not _SHA256_RE.fullmatch(sha256):
        raise RegistryError(f"{context}.sha256 must be 64 lowercase hex characters")

    if state is ArtifactState.VENDORED_VERIFIED:
        if not local_path or not sha256:
            raise RegistryError(
                f"{context} vendored_verified requires local_path and sha256"
            )
        artifact_path = _safe_local_artifact_path(
            registry_directory, local_path, context
        )
        if not artifact_path.is_file():
            raise RegistryError(f"{context} vendored artifact is missing: {local_path}")
        actual_digest = _sha256_file(artifact_path)
        if actual_digest != sha256:
            raise RegistryError(
                f"{context} SHA-256 mismatch for vendored artifact {local_path}"
            )
    elif state is ArtifactState.VENDORED_UNVERIFIED:
        if not local_path:
            raise RegistryError(
                f"{context} vendored_unverified requires local_path"
            )
        if sha256 is not None:
            raise RegistryError(
                f"{context} may not claim a hash while vendored_unverified"
            )
        artifact_path = _safe_local_artifact_path(
            registry_directory, local_path, context
        )
        if not artifact_path.is_file():
            raise RegistryError(f"{context} vendored artifact is missing: {local_path}")
    elif state is ArtifactState.REMOTE_UNVERIFIED:
        if source_url is None:
            raise RegistryError(
                f"{context} remote_unverified requires an HTTPS source_url"
            )
        if local_path is not None or sha256 is not None:
            raise RegistryError(
                f"{context} remote_unverified may not declare local_path or sha256"
            )
    elif state is ArtifactState.UNAVAILABLE:
        if local_path is not None or sha256 is not None:
            raise RegistryError(
                f"{context} unavailable may not declare local_path or sha256"
            )

    return ArtifactReference(
        id=artifact_id,
        title=_nonempty_string(item.get("title"), f"{context}.title"),
        role=_identifier(item.get("role"), f"{context}.role"),
        authority=_nonempty_string(item.get("authority"), f"{context}.authority"),
        legal_status=_enum(
            ArtifactLegalStatus,
            item.get("legal_status"),
            f"{context}.legal_status",
        ),
        state=state,
        source_url=source_url,
        published_on=_optional_date(
            item.get("published_on"), f"{context}.published_on"
        ),
        published_label=_optional_string(
            item.get("published_label"), f"{context}.published_label"
        ),
        updated_on=_optional_date(item.get("updated_on"), f"{context}.updated_on"),
        retrieved_on=_optional_date(
            item.get("retrieved_on"), f"{context}.retrieved_on"
        ),
        reviewed_on=reviewed_on,
        reviewed_by=reviewed_by,
        parser_version=_optional_string(
            item.get("parser_version"), f"{context}.parser_version"
        ),
        compiler_version=_optional_string(
            item.get("compiler_version"), f"{context}.compiler_version"
        ),
        supersedes=_identifier_tuple(
            item.get("supersedes", []), f"{context}.supersedes"
        ),
        superseded_by=_identifier_tuple(
            item.get("superseded_by", []), f"{context}.superseded_by"
        ),
        media_type=_optional_string(item.get("media_type"), f"{context}.media_type"),
        local_path=local_path,
        sha256=sha256,
        notes=_string_tuple(item.get("notes", []), f"{context}.notes"),
    )


def _parse_bundle(value: Any, context: str) -> RuleBundle:
    item = _mapping(value, context)
    _require_keys(
        item,
        {
            "id",
            "title",
            "profile_kind",
            "legal_status",
            "compliance_use",
            "implementation_status",
            "enabled",
            "effective_submission_years",
            "artifact_ids",
            "inherits",
            "disabled_reason",
            "notes",
        },
        context,
    )
    _reject_unknown_keys(
        item,
        {
            "id",
            "title",
            "profile_kind",
            "legal_status",
            "compliance_use",
            "implementation_status",
            "enabled",
            "effective_submission_years",
            "artifact_ids",
            "inherits",
            "disabled_reason",
            "notes",
        },
        context,
    )

    enabled = _boolean(item.get("enabled"), f"{context}.enabled")
    disabled_reason = _optional_string(
        item.get("disabled_reason"), f"{context}.disabled_reason"
    )
    if enabled and disabled_reason is not None:
        raise RegistryError(f"{context} is enabled but has disabled_reason")
    if not enabled and disabled_reason is None:
        raise RegistryError(f"{context} is disabled and requires disabled_reason")

    years = _integer_tuple(
        item.get("effective_submission_years"),
        f"{context}.effective_submission_years",
    )
    if years != tuple(sorted(years)):
        raise RegistryError(
            f"{context}.effective_submission_years must be sorted ascending"
        )
    if any(year < 2000 or year > 2200 for year in years):
        raise RegistryError(
            f"{context}.effective_submission_years contains an invalid year"
        )

    return RuleBundle(
        id=_identifier(item.get("id"), f"{context}.id"),
        title=_nonempty_string(item.get("title"), f"{context}.title"),
        profile_kind=_enum(
            ProfileKind, item.get("profile_kind"), f"{context}.profile_kind"
        ),
        legal_status=_enum(
            BundleLegalStatus,
            item.get("legal_status"),
            f"{context}.legal_status",
        ),
        compliance_use=_enum(
            ComplianceUse,
            item.get("compliance_use"),
            f"{context}.compliance_use",
        ),
        implementation_status=_enum(
            ImplementationStatus,
            item.get("implementation_status"),
            f"{context}.implementation_status",
        ),
        enabled=enabled,
        effective_submission_years=years,
        artifact_ids=_identifier_tuple(
            item.get("artifact_ids"), f"{context}.artifact_ids"
        ),
        inherits=_identifier_tuple(item.get("inherits"), f"{context}.inherits"),
        disabled_reason=disabled_reason,
        notes=_string_tuple(item.get("notes", []), f"{context}.notes"),
    )


def _validate_bundle_graph(
    bundles: Mapping[str, RuleBundle], artifacts: Mapping[str, ArtifactReference]
) -> None:
    for bundle in bundles.values():
        for artifact_id in bundle.artifact_ids:
            if artifact_id not in artifacts:
                raise RegistryError(
                    f"{bundle.id} references unknown artifact {artifact_id}"
                )
        for parent_id in bundle.inherits:
            if parent_id not in bundles:
                raise RegistryError(
                    f"{bundle.id} inherits unknown bundle {parent_id}"
                )
            if parent_id == bundle.id:
                raise RegistryError(f"{bundle.id} may not inherit itself")

    visiting = set()
    visited = set()

    def visit(bundle_id: str) -> None:
        if bundle_id in visiting:
            raise RegistryError(f"cycle in FHWA bundle inheritance at {bundle_id}")
        if bundle_id in visited:
            return
        visiting.add(bundle_id)
        for parent_id in bundles[bundle_id].inherits:
            visit(parent_id)
        visiting.remove(bundle_id)
        visited.add(bundle_id)

    for bundle_id in bundles:
        visit(bundle_id)


def _validate_artifact_supersession(
    artifacts: Mapping[str, ArtifactReference],
) -> None:
    """Require local, reciprocal supersession links when they are declared."""

    for artifact in artifacts.values():
        for relation, expected_backlink in (
            (artifact.supersedes, "superseded_by"),
            (artifact.superseded_by, "supersedes"),
        ):
            for other_id in relation:
                if other_id == artifact.id:
                    raise RegistryError(f"{artifact.id} may not supersede itself")
                other = artifacts.get(other_id)
                if other is None:
                    raise RegistryError(
                        f"{artifact.id} references unknown supersession artifact {other_id}"
                    )
                if artifact.id not in getattr(other, expected_backlink):
                    raise RegistryError(
                        f"{artifact.id} and {other_id} have non-reciprocal "
                        "supersession metadata"
                    )


def _validate_submission_year_index(bundles: Mapping[str, RuleBundle]) -> None:
    owners = {}
    for bundle in bundles.values():
        if bundle.profile_kind is not ProfileKind.SUBMISSION:
            if bundle.effective_submission_years:
                raise RegistryError(
                    f"non-submission bundle {bundle.id} may not own submission years"
                )
            continue
        if not bundle.effective_submission_years:
            raise RegistryError(
                f"submission bundle {bundle.id} requires an effective submission year"
            )
        for year in bundle.effective_submission_years:
            previous = owners.setdefault(year, bundle.id)
            if previous != bundle.id:
                raise RegistryError(
                    f"submission year {year} is assigned to both {previous} and "
                    f"{bundle.id}"
                )


def _safe_local_artifact_path(root: Path, relative: str, context: str) -> Path:
    if "\\" in relative or ":" in relative:
        raise RegistryError(f"{context}.local_path must use a safe relative POSIX path")
    pure_path = PurePosixPath(relative)
    if pure_path.is_absolute() or not pure_path.parts or ".." in pure_path.parts:
        raise RegistryError(f"{context}.local_path must stay below the registry directory")
    root = root.resolve()
    candidate = root.joinpath(*pure_path.parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RegistryError(
            f"{context}.local_path escapes the registry directory"
        ) from exc
    return candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise RegistryError(f"{context} must be an object")
    return value


def _sequence(value: Any, context: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise RegistryError(f"{context} must be an array")
    return value


def _reject_unknown_keys(
    value: Mapping[str, Any], allowed: set, context: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise RegistryError(f"{context} contains unknown keys: {', '.join(unknown)}")


def _require_keys(
    value: Mapping[str, Any], required: set, context: str
) -> None:
    missing = sorted(required - set(value))
    if missing:
        raise RegistryError(f"{context} is missing required keys: {', '.join(missing)}")


def _nonempty_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{context} must be a non-empty string")
    return value


def _optional_string(value: Any, context: str) -> Optional[str]:
    if value is None:
        return None
    return _nonempty_string(value, context)


def _identifier(value: Any, context: str) -> str:
    result = _nonempty_string(value, context)
    if not _IDENTIFIER_RE.fullmatch(result):
        raise RegistryError(f"{context} must be a lowercase snake-case identifier")
    return result


def _boolean(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise RegistryError(f"{context} must be a boolean")
    return value


def _integer(value: Any, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RegistryError(f"{context} must be an integer")
    return value


def _date(value: Any, context: str) -> date:
    raw = _nonempty_string(value, context)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise RegistryError(f"{context} must be an ISO date (YYYY-MM-DD)") from exc


def _optional_date(value: Any, context: str) -> Optional[date]:
    if value is None:
        return None
    return _date(value, context)


def _optional_https_url(value: Any, context: str) -> Optional[str]:
    if value is None:
        return None
    result = _nonempty_string(value, context)
    parsed = urlsplit(result)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username is not None:
        raise RegistryError(f"{context} must be an absolute HTTPS URL")
    return result


def _enum(enum_type: Any, value: Any, context: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(member.value for member in enum_type)
        raise RegistryError(f"{context} must be one of: {allowed}") from exc


def _string_tuple(value: Any, context: str) -> Tuple[str, ...]:
    values = _sequence(value, context)
    result = tuple(
        _nonempty_string(item, f"{context}[{index}]")
        for index, item in enumerate(values)
    )
    if len(result) != len(set(result)):
        raise RegistryError(f"{context} may not contain duplicates")
    return result


def _identifier_tuple(value: Any, context: str) -> Tuple[str, ...]:
    values = _sequence(value, context)
    result = tuple(
        _identifier(item, f"{context}[{index}]")
        for index, item in enumerate(values)
    )
    if len(result) != len(set(result)):
        raise RegistryError(f"{context} may not contain duplicates")
    return result


def _integer_tuple(value: Any, context: str) -> Tuple[int, ...]:
    values = _sequence(value, context)
    result = tuple(
        _integer(item, f"{context}[{index}]")
        for index, item in enumerate(values)
    )
    if len(result) != len(set(result)):
        raise RegistryError(f"{context} may not contain duplicates")
    return result
