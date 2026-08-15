"""Inventory and recoverably quarantine legacy point-cloud current pointers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
from typing import Any
from uuid import uuid4

from app.services.conversion.canonical_json import canonical_json_text, canonical_sha256


ROLLOUT_VERSION = "bridgeguard-point-cloud-rollout-v1"
_SHA = re.compile(r"^[0-9a-f]{64}$")
_GENERATION = re.compile(r"^pcv2-(?:inline|[1-9][0-9]*)-[0-9a-f]{32}-[0-9a-f]{12}$")


class PointCloudRolloutError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.public_message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class LegacyPointerItem:
    relative_pointer: str
    source_generation: str | None
    manifest_sha256: str | None
    classification: str
    reason: str


@dataclass(frozen=True, slots=True)
class LegacyPointerInventory:
    version: str
    workspace_sha256: str
    item_count: int
    classification_counts: dict[str, int]
    items: tuple[LegacyPointerItem, ...]
    inventory_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "workspace_sha256": self.workspace_sha256,
            "item_count": self.item_count,
            "classification_counts": dict(self.classification_counts),
            "items": [asdict(item) for item in self.items],
            "inventory_sha256": self.inventory_sha256,
        }


@dataclass(frozen=True, slots=True)
class LegacyQuarantineReceipt:
    version: str
    inventory_sha256: str
    moved_count: int
    retained_count: int
    moves: tuple[dict[str, str], ...]
    receipt_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "inventory_sha256": self.inventory_sha256,
            "moved_count": self.moved_count,
            "retained_count": self.retained_count,
            "moves": [dict(item) for item in self.moves],
            "receipt_sha256": self.receipt_sha256,
        }


def _safe_root(data_root: str | Path) -> Path:
    candidate = Path(data_root)
    if candidate.is_symlink():
        raise PointCloudRolloutError("point_cloud_rollout_root_invalid", "Data root is invalid.")
    root = candidate.resolve(strict=True)
    if not root.is_dir():
        raise PointCloudRolloutError("point_cloud_rollout_root_invalid", "Data root is invalid.")
    return root


def _safe_pointer(root: Path, path: Path) -> tuple[Path, str]:
    try:
        lexical = path.absolute()
        lexical_relative = lexical.relative_to(root)
        current = root
        for part in lexical_relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("symlink")
        resolved = path.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PointCloudRolloutError(
            "point_cloud_rollout_pointer_invalid", "A legacy pointer is outside the data root."
        ) from exc
    if not resolved.is_file() or resolved.name != "current.json":
        raise PointCloudRolloutError(
            "point_cloud_rollout_pointer_invalid", "A legacy pointer is invalid."
        )
    return resolved, PurePosixPath(*relative.parts).as_posix()


def _read_pointer(path: Path) -> dict[str, Any] | None:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON member")
            result[key] = item
        return result

    try:
        raw = path.read_bytes()
        if not raw or len(raw) > 128 * 1024:
            return None
        value = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(value, dict):
            return None
        return value
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _run_publications(database_path: str | Path, workspace_id: str) -> tuple[set[str], set[str]]:
    connection = sqlite3.connect(f"file:{Path(database_path).resolve().as_posix()}?mode=ro", uri=True)
    try:
        run_rows = connection.execute(
            "SELECT result_sha256 FROM point_cloud_runs WHERE workspace_id=? AND result_sha256 IS NOT NULL",
            (workspace_id,),
        ).fetchall()
        published_rows = connection.execute(
            "SELECT r.result_sha256 FROM published_point_cloud_results p "
            "JOIN point_cloud_runs r ON r.workspace_id=p.workspace_id AND r.id=p.run_id "
            "WHERE p.workspace_id=?",
            (workspace_id,),
        ).fetchall()
        return (
            {str(row[0]) for row in run_rows if _SHA.fullmatch(str(row[0] or ""))},
            {str(row[0]) for row in published_rows if _SHA.fullmatch(str(row[0] or ""))},
        )
    except sqlite3.Error as exc:
        raise PointCloudRolloutError(
            "point_cloud_rollout_database_invalid", "Point-cloud rollout tables are unavailable."
        ) from exc
    finally:
        connection.close()


def inventory_legacy_processed_pointers(
    data_root: str | Path,
    database_path: str | Path,
    workspace_id: str,
) -> LegacyPointerInventory:
    root = _safe_root(data_root)
    workspace_hash = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()
    token = "ws-" + workspace_hash[:24]
    point_root = root / "attachments" / "point_clouds" / token
    paths = sorted(point_root.glob("*/processed/current.json")) if point_root.is_dir() else []
    if len(paths) > 100_000:
        raise PointCloudRolloutError("point_cloud_rollout_too_large", "Too many legacy pointers.")
    run_hashes, published_hashes = _run_publications(database_path, workspace_id)
    items: list[LegacyPointerItem] = []
    counts: dict[str, int] = {}
    for path in paths:
        resolved, relative = _safe_pointer(root, path)
        value = _read_pointer(resolved)
        generation = str(value.get("source_generation") or "") if value else ""
        manifest = str(value.get("manifest_sha256") or "") if value else ""
        if value is None or value.get("format") != "bridgeguard-point-cloud-pointer-v2":
            classification, reason = "invalid_pointer", "pointer_schema_or_json_invalid"
        elif not _GENERATION.fullmatch(generation) or not _SHA.fullmatch(manifest):
            classification, reason = "invalid_pointer", "pointer_identity_invalid"
        elif manifest in published_hashes:
            classification, reason = "durable_published_compatibility_pointer", "accepted_run_has_legacy_pointer"
        elif manifest in run_hashes:
            classification, reason = "durable_unpublished_compatibility_pointer", "durable_run_has_legacy_pointer"
        else:
            classification, reason = "legacy_unreviewed", "no_durable_run_or_review_receipt"
        counts[classification] = counts.get(classification, 0) + 1
        items.append(
            LegacyPointerItem(
                relative,
                generation if generation else None,
                manifest if _SHA.fullmatch(manifest) else None,
                classification,
                reason,
            )
        )
    base = {
        "version": ROLLOUT_VERSION,
        "workspace_sha256": workspace_hash,
        "item_count": len(items),
        "classification_counts": counts,
        "items": [asdict(item) for item in items],
    }
    return LegacyPointerInventory(
        ROLLOUT_VERSION,
        workspace_hash,
        len(items),
        counts,
        tuple(items),
        canonical_sha256(base),
    )


def quarantine_legacy_processed_pointers(
    data_root: str | Path,
    database_path: str | Path,
    workspace_id: str,
    *,
    expected_inventory_sha256: str,
) -> LegacyQuarantineReceipt:
    inventory = inventory_legacy_processed_pointers(data_root, database_path, workspace_id)
    if inventory.inventory_sha256 != expected_inventory_sha256:
        raise PointCloudRolloutError(
            "point_cloud_rollout_inventory_changed", "Legacy pointer inventory changed."
        )
    root = _safe_root(data_root)
    moves: list[dict[str, str]] = []
    for item in inventory.items:
        if item.classification not in {"legacy_unreviewed", "invalid_pointer"}:
            continue
        source = (root / Path(*PurePosixPath(item.relative_pointer).parts)).resolve(strict=True)
        _safe_pointer(root, source)
        quarantine = source.parent / "legacy-quarantine"
        if quarantine.exists() and (not quarantine.is_dir() or quarantine.is_symlink()):
            raise PointCloudRolloutError(
                "point_cloud_rollout_quarantine_invalid", "Legacy quarantine directory is invalid."
            )
        quarantine.mkdir(parents=False, exist_ok=True)
        suffix = item.manifest_sha256 or hashlib.sha256(source.read_bytes()).hexdigest()
        destination = quarantine / f"current-{suffix[:16]}.json"
        if destination.exists():
            if destination.read_bytes() != source.read_bytes():
                raise PointCloudRolloutError(
                    "point_cloud_rollout_quarantine_conflict", "Legacy quarantine target conflicts."
                )
            source.unlink()
        else:
            os.replace(source, destination)
        moves.append(
            {
                "from": item.relative_pointer,
                "to": PurePosixPath(*destination.relative_to(root).parts).as_posix(),
                "classification": item.classification,
            }
        )
    base = {
        "version": ROLLOUT_VERSION,
        "inventory_sha256": inventory.inventory_sha256,
        "moved_count": len(moves),
        "retained_count": inventory.item_count - len(moves),
        "moves": moves,
    }
    receipt = LegacyQuarantineReceipt(
        ROLLOUT_VERSION,
        inventory.inventory_sha256,
        len(moves),
        inventory.item_count - len(moves),
        tuple(moves),
        canonical_sha256(base),
    )
    reference_root = root / "reference"
    if reference_root.exists() and (reference_root.is_symlink() or not reference_root.is_dir()):
        raise PointCloudRolloutError(
            "point_cloud_rollout_receipt_root_invalid", "Rollout receipt root is invalid."
        )
    reference_root.mkdir(exist_ok=True)
    receipt_root = reference_root / "point_cloud_rollout"
    if receipt_root.exists() and (receipt_root.is_symlink() or not receipt_root.is_dir()):
        raise PointCloudRolloutError(
            "point_cloud_rollout_receipt_root_invalid", "Rollout receipt root is invalid."
        )
    receipt_root.mkdir(exist_ok=True)
    target = receipt_root / f"legacy-quarantine-{receipt.receipt_sha256}.json"
    if not target.exists():
        temp = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temp.open("xb") as handle:
                handle.write((canonical_json_text(receipt.to_dict()) + "\n").encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
    return receipt


__all__ = [
    "LegacyPointerInventory",
    "LegacyPointerItem",
    "LegacyQuarantineReceipt",
    "PointCloudRolloutError",
    "inventory_legacy_processed_pointers",
    "quarantine_legacy_processed_pointers",
]
