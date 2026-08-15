"""Fail-closed deployment identity and isolation policy.

BridgeGuard does not currently claim that its legacy projection tables and
filesystem objects are safe for unrelated tenants in one process.  A paid
customer is therefore isolated by a dedicated application, database, data
root, cache, and tunnel.  This module makes that boundary machine-checkable
instead of leaving it as an operator convention.

The module is deliberately read-only.  A deployment marker is created by the
provisioning workflow, never opportunistically by application startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import re
from typing import Mapping
import uuid
import hashlib

try:
    from ...commercial_evidence import (
        CommercialEntitlementEvidence,
        evaluate_commercial_entitlement_evidence,
    )
except ImportError:  # pragma: no cover - direct backend/app execution fallback
    from commercial_evidence import (  # type: ignore
        CommercialEntitlementEvidence,
        evaluate_commercial_entitlement_evidence,
    )


DEPLOYMENT_POLICY_VERSION = "bridgeguard-deployment-policy-v2"
DEFAULT_RESEARCH_WORKSPACE_ID = "shared-research-workspace"
MARKER_FILENAME = "bridgeguard-deployment.json"
_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


class DeploymentMode(str, Enum):
    RESEARCH_SHARED = "research_shared"
    DEDICATED_CUSTOMER = "dedicated_customer"
    SHARED_MULTITENANT = "shared_multitenant"


class DeploymentPolicyError(RuntimeError):
    """Raised before startup when an asserted deployment boundary is unsafe."""

    def __init__(self, blockers: tuple[str, ...]):
        self.blockers = blockers
        super().__init__("; ".join(blockers))


@dataclass(frozen=True, slots=True)
class DeploymentPolicy:
    version: str
    environment: str
    mode: DeploymentMode
    workspace_id: str
    customer_slug: str | None
    deployment_id: str | None
    marker_sha256: str | None
    worker_count: int
    auth_mode: str
    data_root_isolated: bool
    marker_verified: bool
    storage_paths_verified: bool
    coordination: str
    commercial_entitlement_evidence: CommercialEntitlementEvidence
    commercial_multi_customer_ready: bool
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def approved(self) -> bool:
        return not self.blockers

    def public_dict(self) -> dict[str, object]:
        """Return readiness evidence without filesystem paths or secrets."""

        return {
            "version": self.version,
            "environment": self.environment,
            "mode": self.mode.value,
            "workspace_id": self.workspace_id,
            "customer_slug": self.customer_slug,
            "deployment_id": self.deployment_id,
            "worker_count": self.worker_count,
            "auth_mode": self.auth_mode,
            "data_root_isolated": self.data_root_isolated,
            "marker_verified": self.marker_verified,
            "storage_paths_verified": self.storage_paths_verified,
            "coordination": self.coordination,
            "commercial_entitlement_evidence": (
                self.commercial_entitlement_evidence.public_dict()
            ),
            "commercial_multi_customer_ready": self.commercial_multi_customer_ready,
            "approved": self.approved,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
        }


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in _TRUE_VALUES


def _identifier(value: object, field: str, blockers: list[str]) -> str:
    text = str(value or "").strip().lower()
    if not _IDENTIFIER.fullmatch(text):
        blockers.append(f"{field}_invalid")
    return text


def _safe_data_root(path: Path, blockers: list[str]) -> Path:
    expanded = path.expanduser()
    if ".." in expanded.parts:
        blockers.append("data_root_traversal")
    if _path_has_symlink_component(expanded):
        blockers.append("data_root_symlink")
    try:
        resolved = expanded.resolve(strict=False)
    except (OSError, RuntimeError):
        blockers.append("data_root_invalid")
        return path
    anchor = Path(resolved.anchor) if resolved.anchor else None
    if not resolved.is_absolute() or (anchor is not None and resolved == anchor):
        blockers.append("data_root_too_broad")
    return resolved


def _path_has_symlink_component(path: Path) -> bool:
    """Return true when any existing component of *path* is a symlink.

    Dedicated roots and stores deliberately do not accept even an in-root
    symlink.  Besides making the isolation evidence simpler, this prevents a
    store from being redirected after an operator reviewed only its lexical
    path.
    """

    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    current = Path(candidate.anchor)
    parts = candidate.parts[1:] if candidate.anchor else candidate.parts
    for part in parts:
        current = current / part
        is_junction = getattr(current, "is_junction", None)
        if current.is_symlink() or (callable(is_junction) and is_junction()):
            return True
    return False


def _path_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _dedicated_storage_path(
    env: Mapping[str, str],
    *,
    key: str,
    label: str,
    root: Path,
    blockers: list[str],
) -> Path | None:
    raw = str(env.get(key, "")).strip()
    if not raw:
        blockers.append(f"{label}_missing")
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        blockers.append(f"{label}_not_absolute")
    if ".." in path.parts:
        blockers.append(f"{label}_traversal")
    if _path_has_symlink_component(path):
        blockers.append(f"{label}_symlink")
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        blockers.append(f"{label}_invalid")
        return None
    if not _path_within(root, resolved):
        blockers.append(f"{label}_outside_customer_root")
    return resolved


def _configured_role_users(env: Mapping[str, str]) -> tuple[set[str], set[str]]:
    """Return (all explicit users, explicit administrators)."""

    values: dict[str, set[str]] = {}
    for key in (
        "BRIDGEGUARD_OWNER_EMAIL",
        "BRIDGEGUARD_ADMIN_EMAILS",
        "BRIDGEGUARD_EDITOR_EMAILS",
        "BRIDGEGUARD_ALLOWED_EMAILS",
    ):
        values[key] = {
            item.strip().lower()
            for item in str(env.get(key, "")).split(",")
            if item.strip()
        }
    all_users = set().union(*values.values())
    administrators = values["BRIDGEGUARD_OWNER_EMAIL"] | values["BRIDGEGUARD_ADMIN_EMAILS"]
    return all_users, administrators


def _read_marker(path: Path) -> tuple[dict[str, object], str] | None:
    try:
        raw = path.read_bytes()
        if not raw or len(raw) > 16 * 1024:
            return None
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    return value, hashlib.sha256(raw).hexdigest()


def _canonical_deployment_id(value: object) -> str | None:
    text = str(value or "").strip()
    try:
        return text if str(uuid.UUID(text)) == text else None
    except ValueError:
        return None


def evaluate_deployment_policy(
    *,
    environ: Mapping[str, str] | None = None,
    data_directory: str | Path | None = None,
) -> DeploymentPolicy:
    """Evaluate the server-owned deployment boundary without changing state."""

    env = os.environ if environ is None else environ
    blockers: list[str] = []
    warnings: list[str] = []
    environment = str(env.get("BRIDGEGUARD_ENV", "development")).strip().lower()
    if environment not in {"development", "test", "production"}:
        blockers.append("environment_invalid")
    raw_mode = str(env.get("BRIDGEGUARD_DEPLOYMENT_MODE", "research_shared")).strip().lower()
    try:
        mode = DeploymentMode(raw_mode)
    except ValueError:
        blockers.append("deployment_mode_invalid")
        mode = DeploymentMode.RESEARCH_SHARED

    workspace_default = DEFAULT_RESEARCH_WORKSPACE_ID
    workspace_id = _identifier(
        env.get("BRIDGEGUARD_WORKSPACE_ID", workspace_default),
        "workspace_id",
        blockers,
    )
    raw_customer = str(env.get("BRIDGEGUARD_CUSTOMER_SLUG", "")).strip().lower()
    customer_slug = _identifier(raw_customer, "customer_slug", blockers) if raw_customer else None
    try:
        worker_count = int(str(env.get("BRIDGEGUARD_WORKER_COUNT", "1")).strip())
    except ValueError:
        worker_count = 0
    if worker_count != 1:
        blockers.append("single_worker_coordination_required")

    auth_disabled = _truthy(env.get("BRIDGEGUARD_AUTH_DISABLED"))
    auth_mode = "disabled" if auth_disabled else str(
        env.get("BRIDGEGUARD_AUTH_MODE", "password")
    ).strip().lower()
    if auth_mode not in {"disabled", "password", "cloudflare"}:
        blockers.append("auth_mode_invalid")
    if environment == "production" and auth_disabled:
        blockers.append("production_auth_disabled")
    if (
        environment == "production"
        and auth_mode == "password"
        and not _truthy(env.get("BRIDGEGUARD_EMERGENCY_PASSWORD_APPROVED"))
    ):
        blockers.append("production_password_mode_not_approved")
    if environment == "production" and auth_mode == "cloudflare":
        if not str(env.get("CLOUDFLARE_ACCESS_TEAM_DOMAIN", "")).strip():
            blockers.append("cloudflare_team_domain_missing")
        if not str(env.get("CLOUDFLARE_ACCESS_AUD", "")).strip():
            blockers.append("cloudflare_audience_missing")

    root = _safe_data_root(
        Path(data_directory or env.get("BRIDGEGUARD_DATA_DIR", "backend/data")),
        blockers,
    )
    marker_verified = False
    deployment_id: str | None = None
    marker_sha256: str | None = None
    data_root_isolated = False
    storage_paths_verified = False
    marker_path = root / MARKER_FILENAME
    commercial_evidence = evaluate_commercial_entitlement_evidence(env)
    if marker_path.is_symlink():
        blockers.append("dedicated_deployment_marker_symlink")
        marker = None
    else:
        marker_receipt = _read_marker(marker_path)
        marker = marker_receipt[0] if marker_receipt is not None else None
        marker_sha256 = marker_receipt[1] if marker_receipt is not None else None

    if mode is DeploymentMode.SHARED_MULTITENANT:
        blockers.append("shared_multitenant_not_implemented")
    elif mode is DeploymentMode.DEDICATED_CUSTOMER:
        # A production dedicated instance is the paid launch boundary.  These
        # are syntactic references to operator-controlled review records, not a
        # claim that the application validated their legal sufficiency.
        if environment == "production":
            blockers.extend(commercial_evidence.blockers)
        if customer_slug is None:
            blockers.append("dedicated_customer_slug_required")
        if workspace_id == DEFAULT_RESEARCH_WORKSPACE_ID:
            blockers.append("dedicated_workspace_must_be_customer_specific")
        explicit_users, explicit_administrators = _configured_role_users(env)
        if not explicit_users:
            blockers.append("dedicated_role_users_missing")
        if not explicit_administrators:
            blockers.append("dedicated_admin_user_missing")
        if any("@" not in email for email in explicit_users):
            blockers.append("dedicated_role_email_invalid")
        if str(env.get("BRIDGEGUARD_ALLOWED_DOMAINS", "")).strip():
            blockers.append("dedicated_allowed_domains_not_permitted")

        configured_data_root = _dedicated_storage_path(
            env,
            key="BRIDGEGUARD_DATA_DIR",
            label="dedicated_data_dir",
            root=root,
            blockers=blockers,
        )
        rag_root = _dedicated_storage_path(
            env,
            key="RAG_WORKING_DIR",
            label="rag_working_dir",
            root=root,
            blockers=blockers,
        )
        temp_root = _dedicated_storage_path(
            env,
            key="SNBI_TEMP_DIR",
            label="snbi_temp_dir",
            root=root,
            blockers=blockers,
        )
        if configured_data_root is not None and configured_data_root != root:
            blockers.append("dedicated_data_dir_mismatch")

        derived_paths = {
            "attachments_dir": root / "attachments",
            "backups_dir": root / "backups",
            "conversion_sources_dir": root / "conversion_sources",
            "exports_dir": root / "exports",
            "imports_dir": root / "imports",
            "point_cloud_attachments_dir": root / "attachments" / "point_clouds",
            "point_cloud_cache_dir": root / "pointcloud_carve",
            "legacy_report_cache_dir": root / "rag_storage" / "bridge_reports",
            "reference_dir": root / "reference",
            "samples_dir": root / "samples",
        }
        if rag_root is not None:
            derived_paths["report_cache_dir"] = rag_root / "bridge_reports"
        derived_paths_safe = True
        for label, path in derived_paths.items():
            if ".." in path.parts:
                blockers.append(f"{label}_traversal")
                derived_paths_safe = False
            if _path_has_symlink_component(path):
                blockers.append(f"{label}_symlink")
                derived_paths_safe = False
            try:
                resolved_path = path.resolve(strict=False)
            except (OSError, RuntimeError):
                blockers.append(f"{label}_invalid")
                derived_paths_safe = False
                continue
            if not _path_within(root, resolved_path):
                blockers.append(f"{label}_outside_customer_root")
                derived_paths_safe = False

        if marker is None:
            blockers.append("dedicated_deployment_marker_missing")
        else:
            deployment_id = _canonical_deployment_id(marker.get("deployment_id"))
            expected = {
                "format": DEPLOYMENT_POLICY_VERSION,
                "mode": DeploymentMode.DEDICATED_CUSTOMER.value,
                "workspace_id": workspace_id,
                "customer_slug": customer_slug,
            }
            marker_verified = (
                deployment_id is not None
                and set(marker) == {*expected, "deployment_id"}
                and all(marker.get(key) == value for key, value in expected.items())
            )
            if not marker_verified:
                blockers.append("dedicated_deployment_marker_mismatch")
        data_root_isolated = marker_verified
        storage_blocker_prefixes = (
            "data_root_",
            "dedicated_data_dir_",
            "rag_working_dir_",
            "snbi_temp_dir_",
            *(f"{label}_" for label in derived_paths),
        )
        storage_paths_verified = (
            configured_data_root == root
            and rag_root is not None
            and temp_root is not None
            and _path_within(root, rag_root)
            and _path_within(root, temp_root)
            and derived_paths_safe
            and not any(blocker.startswith(storage_blocker_prefixes) for blocker in blockers)
        )
    else:
        if customer_slug:
            warnings.append("customer_slug_ignored_in_research_mode")
        warnings.append("research_workspace_not_for_unrelated_paying_customers")
        data_root_isolated = False

    commercial_ready = (
        not blockers
        and environment == "production"
        and mode is DeploymentMode.DEDICATED_CUSTOMER
        and marker_verified
        and storage_paths_verified
        and auth_mode == "cloudflare"
        and worker_count == 1
        and commercial_evidence.approved
    )
    if mode is DeploymentMode.DEDICATED_CUSTOMER and not commercial_ready:
        warnings.append("dedicated_customer_commercial_gate_not_satisfied")

    return DeploymentPolicy(
        version=DEPLOYMENT_POLICY_VERSION,
        environment=environment,
        mode=mode,
        workspace_id=workspace_id,
        customer_slug=customer_slug,
        deployment_id=deployment_id,
        marker_sha256=marker_sha256,
        worker_count=worker_count,
        auth_mode=auth_mode,
        data_root_isolated=data_root_isolated,
        marker_verified=marker_verified,
        storage_paths_verified=storage_paths_verified,
        coordination="single_worker_in_memory",
        commercial_entitlement_evidence=commercial_evidence,
        commercial_multi_customer_ready=commercial_ready,
        blockers=tuple(dict.fromkeys(blockers)),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def require_deployment_policy(
    *,
    environ: Mapping[str, str] | None = None,
    data_directory: str | Path | None = None,
    require_commercial: bool = False,
) -> DeploymentPolicy:
    policy = evaluate_deployment_policy(environ=environ, data_directory=data_directory)
    blockers = list(policy.blockers)
    if require_commercial and not policy.commercial_multi_customer_ready:
        blockers.append("commercial_dedicated_deployment_required")
    if blockers:
        raise DeploymentPolicyError(tuple(dict.fromkeys(blockers)))
    return policy


__all__ = [
    "DEFAULT_RESEARCH_WORKSPACE_ID",
    "DEPLOYMENT_POLICY_VERSION",
    "MARKER_FILENAME",
    "DeploymentMode",
    "DeploymentPolicy",
    "DeploymentPolicyError",
    "evaluate_deployment_policy",
    "require_deployment_policy",
]
