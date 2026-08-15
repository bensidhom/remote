"""Safe, idempotent scaffolding for one dedicated customer data plane."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import uuid

from .deployment import DEPLOYMENT_POLICY_VERSION, MARKER_FILENAME


_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
DEDICATED_DATA_DIRECTORIES = (
    "attachments",
    "backups",
    "conversion_sources",
    "exports",
    "imports",
    "pointcloud_carve",
    "rag_storage",
    "reference",
    "samples",
    "temp",
)


def _has_symlink_component(path: Path) -> bool:
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


@dataclass(frozen=True, slots=True)
class DedicatedLayoutReceipt:
    customer_slug: str
    workspace_id: str
    deployment_id: str
    created: bool
    marker_sha256: str
    directory_count: int

    def public_dict(self) -> dict[str, object]:
        return {
            "customer_slug": self.customer_slug,
            "workspace_id": self.workspace_id,
            "deployment_id": self.deployment_id,
            "created": self.created,
            "marker_sha256": self.marker_sha256,
            "directory_count": self.directory_count,
        }


def _token(value: str, name: str) -> str:
    raw = str(value or "").strip()
    text = raw.lower()
    if raw != text:
        raise ValueError(f"{name} must be a lowercase deployment identifier")
    if not _IDENTIFIER.fullmatch(text):
        raise ValueError(f"{name} must be a lowercase deployment identifier")
    return text


def _marker_bytes(
    *,
    customer_slug: str,
    workspace_id: str,
    deployment_id: str,
) -> bytes:
    payload = {
        "customer_slug": customer_slug,
        "deployment_id": deployment_id,
        "format": DEPLOYMENT_POLICY_VERSION,
        "mode": "dedicated_customer",
        "workspace_id": workspace_id,
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def provision_dedicated_layout(
    data_root: str | Path,
    *,
    customer_slug: str,
    workspace_id: str,
) -> DedicatedLayoutReceipt:
    """Create only an empty, customer-specific persistent layout.

    Existing nonempty roots without an exact marker are rejected.  An exact
    rerun is a no-op, while reuse for another customer fails closed.
    """

    customer = _token(customer_slug, "customer_slug")
    workspace = _token(workspace_id, "workspace_id")
    requested_root = Path(data_root).expanduser()
    if ".." in requested_root.parts:
        raise ValueError("data_root must not contain parent-directory traversal")
    if not requested_root.is_absolute():
        raise ValueError("data_root must be an absolute customer-specific directory")
    if _has_symlink_component(requested_root):
        raise ValueError("data_root and its existing parents must not be symlinks")
    root = requested_root.resolve(strict=False)
    if root == Path(root.anchor):
        raise ValueError("data_root must be an absolute customer-specific directory")
    root.mkdir(parents=True, exist_ok=True)
    if _has_symlink_component(requested_root):
        raise ValueError("data_root and its existing parents must not be symlinks")
    for name in DEDICATED_DATA_DIRECTORIES:
        existing_path = root / name
        if _has_symlink_component(existing_path):
            raise RuntimeError(f"dedicated data layout symlink is not allowed: {name}")
    marker_path = root / MARKER_FILENAME
    created = False
    if marker_path.is_symlink():
        raise RuntimeError("dedicated deployment marker must not be a symlink")
    if marker_path.exists():
        try:
            marker_raw = marker_path.read_bytes()
            marker = json.loads(marker_raw.decode("utf-8"))
            deployment_id = str(marker.get("deployment_id", ""))
            if str(uuid.UUID(deployment_id)) != deployment_id:
                raise ValueError("non-canonical deployment id")
            expected = _marker_bytes(
                customer_slug=customer,
                workspace_id=workspace,
                deployment_id=deployment_id,
            )
        except (AttributeError, OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            raise RuntimeError("dedicated data root belongs to another deployment") from None
        if not marker_path.is_file() or marker_raw != expected:
            raise RuntimeError("dedicated data root belongs to another deployment")
    else:
        unexpected = [
            item for item in root.iterdir()
            if item.name not in DEDICATED_DATA_DIRECTORIES
        ]
        populated = [
            item for item in root.iterdir()
            if item.name in DEDICATED_DATA_DIRECTORIES
            and item.is_dir()
            and any(item.iterdir())
        ]
        if unexpected or populated:
            raise RuntimeError("refusing to adopt an unmarked nonempty data root")
        deployment_id = str(uuid.uuid4())
        expected = _marker_bytes(
            customer_slug=customer,
            workspace_id=workspace,
            deployment_id=deployment_id,
        )
        temp = root / f".{MARKER_FILENAME}.tmp"
        with temp.open("xb") as stream:
            stream.write(expected)
            stream.flush()
            import os
            os.fsync(stream.fileno())
        temp.replace(marker_path)
        created = True
    for name in DEDICATED_DATA_DIRECTORIES:
        path = root / name
        if _has_symlink_component(path):
            raise RuntimeError(f"dedicated data layout symlink is not allowed: {name}")
        if path.exists() and not path.is_dir():
            raise RuntimeError(f"dedicated data layout collision: {name}")
        path.mkdir(exist_ok=True)
    import hashlib
    return DedicatedLayoutReceipt(
        customer_slug=customer,
        workspace_id=workspace,
        deployment_id=deployment_id,
        created=created,
        marker_sha256=hashlib.sha256(expected).hexdigest(),
        directory_count=len(DEDICATED_DATA_DIRECTORIES),
    )


__all__ = [
    "DEDICATED_DATA_DIRECTORIES",
    "DedicatedLayoutReceipt",
    "provision_dedicated_layout",
]
