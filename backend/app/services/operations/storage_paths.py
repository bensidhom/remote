"""Fail-closed filesystem confinement for customer-owned stored paths."""

from __future__ import annotations

import os
from pathlib import Path


class StoragePathError(ValueError):
    """A stored path cannot be proven to remain within its configured root."""


def _is_link_like(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction and is_junction())
    except OSError as exc:
        raise StoragePathError("stored path link metadata is unavailable") from exc


def resolve_confined_path(
    root: str | os.PathLike[str],
    stored_path: str | os.PathLike[str],
    *,
    require_exists: bool = False,
) -> Path:
    """Resolve a relative or legacy absolute path beneath ``root``.

    Explicit traversal and every existing symlink/junction component are
    rejected. This also rechecks the resolved target so a metadata row cannot
    redirect an upload, download, cache read, or deletion outside its customer
    data root.
    """

    raw_text = os.fspath(stored_path)
    if isinstance(raw_text, bytes):
        raw_text = os.fsdecode(raw_text)
    if not raw_text or "\x00" in raw_text:
        raise StoragePathError("stored path is invalid")
    raw = Path(raw_text)
    if any(part == ".." for part in raw.parts):
        raise StoragePathError("stored path traversal is not permitted")

    root_path = Path(root)
    try:
        root_resolved = root_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise StoragePathError("data root is unavailable") from exc
    if _is_link_like(root_path):
        raise StoragePathError("data root must not be a link")

    candidate = raw if raw.is_absolute() else root_resolved / raw
    try:
        lexical = Path(os.path.abspath(os.fspath(candidate)))
        relative = lexical.relative_to(root_resolved)
    except (OSError, ValueError) as exc:
        raise StoragePathError("stored path is outside the data root") from exc

    cursor = root_resolved
    for part in relative.parts:
        cursor = cursor / part
        if cursor.exists() or cursor.is_symlink():
            if _is_link_like(cursor):
                raise StoragePathError("stored path contains a link")

    try:
        resolved = lexical.resolve(strict=require_exists)
        resolved.relative_to(root_resolved)
    except (OSError, RuntimeError, ValueError) as exc:
        raise StoragePathError(
            "stored path is unavailable or outside the data root"
        ) from exc
    if require_exists and not resolved.exists():
        raise StoragePathError("stored path does not exist")
    return resolved


def portable_confined_path(
    root: str | os.PathLike[str],
    path: str | os.PathLike[str],
    *,
    require_exists: bool = False,
) -> str:
    """Return a portable relative path only after the confinement gate."""

    resolved = resolve_confined_path(root, path, require_exists=require_exists)
    return resolved.relative_to(Path(root).resolve(strict=True)).as_posix()
