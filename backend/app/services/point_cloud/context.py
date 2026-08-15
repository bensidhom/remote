"""Exact published bridge context for point-cloud processing.

This module is deliberately read-only.  It resolves one publication through the
shared bridge-identity boundary, reads only that immutable canonical revision,
and verifies the stored JSON against the identity digest before exposing it to a
point-cloud knowledge-pack builder.  It never falls back to staged revisions or
the legacy projection tables.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Any, Mapping

from app.services.bridge_identity import (
    BridgeIdentityError,
    BridgeSnapshotIdentity,
    resolve_published_bridge_identity,
)
from app.services.conversion.canonical_json import canonical_sha256, parse_canonical_json


class PointCloudContextError(RuntimeError):
    code = "point_cloud_context_error"


class PublishedSnapshotRequired(PointCloudContextError):
    code = "published_snapshot_required"


class PublishedSnapshotIntegrityError(PointCloudContextError):
    code = "published_snapshot_integrity_error"


class DuplicateCanonicalIdentity(PointCloudContextError):
    code = "duplicate_canonical_identity"


_ARRAY_IDENTITIES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "Features": ("BF01",),
        "SpanSets": ("BSP01",),
        "SubstructureSets": ("BSB01",),
        "Elements": ("BE01", "BE02"),
    }
)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _validate_array_identities(document: Mapping[str, Any]) -> None:
    for container, identity_fields in _ARRAY_IDENTITIES.items():
        rows = document.get(container, [])
        if rows is None:
            continue
        if not isinstance(rows, list):
            raise PublishedSnapshotIntegrityError(
                f"Published canonical {container} must be an array."
            )
        seen: set[tuple[str, ...]] = set()
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise PublishedSnapshotIntegrityError(
                    f"Published canonical {container}[{index}] must be an object."
                )
            identity = tuple(str(row.get(tag, "")).strip() for tag in identity_fields)
            if not all(identity):
                raise PublishedSnapshotIntegrityError(
                    f"Published canonical {container}[{index}] has an incomplete identity."
                )
            if identity in seen:
                raise DuplicateCanonicalIdentity(
                    f"Published canonical {container} has a duplicate logical identity."
                )
            seen.add(identity)


@dataclass(frozen=True, slots=True)
class PointCloudCanonicalContext:
    identity: BridgeSnapshotIdentity
    canonical_json: str
    _canonical_document: Mapping[str, Any]

    @property
    def canonical_document(self) -> dict[str, Any]:
        """Return a detached JSON-compatible copy for bounded projection."""

        return _thaw(self._canonical_document)


def resolve_point_cloud_canonical_context(
    database_path: str | os.PathLike[str],
    *,
    workspace_id: str,
    state_code: int,
    bridge_number: str,
    data_year: int | None = None,
) -> PointCloudCanonicalContext:
    """Resolve and verify one exact published canonical bridge revision."""

    try:
        identity = resolve_published_bridge_identity(
            database_path,
            workspace_id=workspace_id,
            state_code=state_code,
            bridge_number=bridge_number,
            data_year=data_year,
        )
    except BridgeIdentityError as exc:
        raise PublishedSnapshotRequired(
            "An exact published bridge snapshot is required for bridge-scoped point-cloud processing."
        ) from exc

    try:
        path = Path(os.fspath(database_path)).resolve(strict=True)
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2.0
        )
    except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
        raise PublishedSnapshotIntegrityError(
            "The published bridge snapshot could not be read."
        ) from exc
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA busy_timeout=2000")
        row = connection.execute(
            "SELECT workspace_id,state_code,bridge_number,data_year,revision_number,"
            "canonical_json,canonical_sha256,profile_id,rule_bundle_sha256 "
            "FROM snapshot_revisions WHERE workspace_id=? AND id=?",
            (identity.workspace_id, identity.snapshot_revision_id),
        ).fetchone()
    except sqlite3.Error as exc:
        raise PublishedSnapshotIntegrityError(
            "The published bridge snapshot could not be read."
        ) from exc
    finally:
        connection.close()

    if row is None:
        raise PublishedSnapshotIntegrityError(
            "The published bridge snapshot revision is unavailable."
        )
    exact = (
        str(row["workspace_id"]) == identity.workspace_id
        and int(row["state_code"]) == identity.state_code
        and str(row["bridge_number"]) == identity.bridge_number
        and int(row["data_year"]) == identity.data_year
        and int(row["revision_number"]) == identity.revision_number
        and str(row["canonical_sha256"]).lower() == identity.canonical_sha256
        and str(row["profile_id"]) == identity.profile_id
        and str(row["rule_bundle_sha256"]).lower() == identity.rule_bundle_sha256
    )
    if not exact:
        raise PublishedSnapshotIntegrityError(
            "The published identity and canonical revision do not match."
        )
    canonical_text = str(row["canonical_json"])
    try:
        document = parse_canonical_json(canonical_text)
    except (TypeError, ValueError) as exc:
        raise PublishedSnapshotIntegrityError(
            "The published canonical bridge JSON is invalid."
        ) from exc
    if not isinstance(document, dict) or canonical_sha256(document) != identity.canonical_sha256:
        raise PublishedSnapshotIntegrityError(
            "The published canonical bridge JSON failed its integrity check."
        )
    if str(document.get("BID01", "")).strip() != identity.bridge_number:
        raise PublishedSnapshotIntegrityError(
            "The canonical bridge number does not match the published identity."
        )
    try:
        canonical_state = int(document.get("BL01"))
    except (TypeError, ValueError) as exc:
        raise PublishedSnapshotIntegrityError(
            "The canonical state code is invalid."
        ) from exc
    if canonical_state != identity.state_code:
        raise PublishedSnapshotIntegrityError(
            "The canonical state code does not match the published identity."
        )
    _validate_array_identities(document)
    return PointCloudCanonicalContext(
        identity=identity,
        canonical_json=canonical_text,
        _canonical_document=_freeze(document),
    )


__all__ = [
    "DuplicateCanonicalIdentity",
    "PointCloudCanonicalContext",
    "PointCloudContextError",
    "PublishedSnapshotIntegrityError",
    "PublishedSnapshotRequired",
    "resolve_point_cloud_canonical_context",
]
