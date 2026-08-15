"""One immutable published bridge/snapshot identity shared across tools.

Map, Ask, report RAG, visual analysis, history, and engineering screens must not
silently resolve different years or revisions for the same bridge.  This
module is a deliberately small, read-only boundary over the canonical revision
store.  It never falls back to staged revisions or the legacy projection.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import sqlite3

from app.services.conversion.canonical_json import canonical_sha256, parse_canonical_json


_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,254}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class BridgeIdentityError(RuntimeError):
    code = "bridge_identity_error"


class InvalidBridgeIdentity(BridgeIdentityError, ValueError):
    code = "invalid_bridge_identity"


class BridgeSnapshotNotFound(BridgeIdentityError):
    code = "published_bridge_snapshot_not_found"


class BridgeIdentityStoreUnavailable(BridgeIdentityError):
    code = "bridge_identity_store_unavailable"


def _token(value: object, label: str) -> str:
    text = str(value).strip()
    if not _TOKEN_RE.fullmatch(text):
        raise InvalidBridgeIdentity(f"{label} is invalid")
    return text


def _integer(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise InvalidBridgeIdentity(f"{label} is invalid")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidBridgeIdentity(f"{label} is invalid") from exc
    if not minimum <= result <= maximum:
        raise InvalidBridgeIdentity(f"{label} is invalid")
    return result


@dataclass(frozen=True, slots=True)
class BridgeSnapshotIdentity:
    workspace_id: str
    state_code: int
    bridge_number: str
    data_year: int
    snapshot_revision_id: str
    revision_number: int
    canonical_sha256: str
    profile_id: str
    rule_bundle_sha256: str
    published_at: str
    data_generation: int
    schema_generation: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_id", _token(self.workspace_id, "workspace_id"))
        object.__setattr__(
            self, "state_code", _integer(self.state_code, "state_code", minimum=1, maximum=99)
        )
        object.__setattr__(self, "bridge_number", _token(self.bridge_number, "bridge_number"))
        object.__setattr__(
            self, "data_year", _integer(self.data_year, "data_year", minimum=1900, maximum=2200)
        )
        object.__setattr__(
            self, "revision_number", _integer(self.revision_number, "revision_number", minimum=1, maximum=2_147_483_647)
        )
        for name in ("snapshot_revision_id", "profile_id"):
            object.__setattr__(self, name, _token(getattr(self, name), name))
        for name in ("canonical_sha256", "rule_bundle_sha256"):
            digest = str(getattr(self, name)).strip().lower()
            if not _SHA256_RE.fullmatch(digest):
                raise InvalidBridgeIdentity(f"{name} is invalid")
            object.__setattr__(self, name, digest)
        published = str(self.published_at).strip()
        if not published:
            raise InvalidBridgeIdentity("published_at is invalid")
        object.__setattr__(self, "published_at", published)
        for name in ("data_generation", "schema_generation"):
            object.__setattr__(
                self,
                name,
                _integer(getattr(self, name), name, minimum=0, maximum=2_147_483_647),
            )

    @property
    def logical_key(self) -> tuple[str, int, str, int]:
        return self.workspace_id, self.state_code, self.bridge_number, self.data_year

    @property
    def cache_key(self) -> tuple[object, ...]:
        return (
            *self.logical_key,
            self.snapshot_revision_id,
            self.canonical_sha256,
            self.data_generation,
        )

    def public_payload(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "state_code": self.state_code,
            "bridge_number": self.bridge_number,
            "data_year": self.data_year,
            "snapshot_revision_id": self.snapshot_revision_id,
            "revision_number": self.revision_number,
            "canonical_sha256": self.canonical_sha256,
            "profile_id": self.profile_id,
            "rule_bundle_sha256": self.rule_bundle_sha256,
            "published_at": self.published_at,
            "data_generation": self.data_generation,
            "schema_generation": self.schema_generation,
            "snapshot_status": "published",
        }


def resolve_published_bridge_identity(
    database_path: str | os.PathLike[str],
    *,
    workspace_id: str,
    state_code: int,
    bridge_number: str,
    data_year: int | None = None,
) -> BridgeSnapshotIdentity:
    """Resolve an exact or per-bridge-latest published identity, fail closed."""

    workspace = _token(workspace_id, "workspace_id")
    state = _integer(state_code, "state_code", minimum=1, maximum=99)
    bridge = _token(bridge_number, "bridge_number")
    year = (
        _integer(data_year, "data_year", minimum=1900, maximum=2200)
        if data_year is not None
        else None
    )
    try:
        path = Path(os.fspath(database_path)).resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise BridgeIdentityStoreUnavailable("Bridge identity store is unavailable.") from exc
    if not path.is_file():
        raise BridgeIdentityStoreUnavailable("Bridge identity store is unavailable.")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA busy_timeout=2000")
        year_clause = "AND ps.data_year=?" if year is not None else ""
        parameters: tuple[object, ...] = (workspace, state, bridge)
        if year is not None:
            parameters += (year,)
        row = connection.execute(
            "SELECT ps.workspace_id,ps.state_code,ps.bridge_number,ps.data_year,"
            "ps.snapshot_revision_id,ps.published_at,sr.revision_number,"
            "sr.canonical_json,sr.canonical_sha256,sr.profile_id,sr.rule_bundle_sha256,"
            "ws.data_generation,ws.schema_generation "
            "FROM published_snapshots ps JOIN snapshot_revisions sr "
            "ON sr.workspace_id=ps.workspace_id AND sr.id=ps.snapshot_revision_id "
            "JOIN workspace_state ws ON ws.workspace_id=ps.workspace_id "
            "WHERE ps.workspace_id=? AND ps.state_code=? AND ps.bridge_number=? "
            f"{year_clause} ORDER BY ps.data_year DESC LIMIT 2",
            parameters,
        ).fetchall()
        if not row:
            raise BridgeSnapshotNotFound("No matching published bridge snapshot was found.")
        selected = row[0]
        stored_digest = str(selected["canonical_sha256"]).strip().lower()
        try:
            canonical_document = parse_canonical_json(str(selected["canonical_json"]))
        except (TypeError, ValueError) as exc:
            raise BridgeIdentityStoreUnavailable(
                "Published bridge revision failed its canonical integrity check."
            ) from exc
        if canonical_sha256(canonical_document) != stored_digest:
            raise BridgeIdentityStoreUnavailable(
                "Published bridge revision failed its canonical integrity check."
            )
        return BridgeSnapshotIdentity(
            workspace_id=str(selected["workspace_id"]),
            state_code=int(selected["state_code"]),
            bridge_number=str(selected["bridge_number"]),
            data_year=int(selected["data_year"]),
            snapshot_revision_id=str(selected["snapshot_revision_id"]),
            revision_number=int(selected["revision_number"]),
            canonical_sha256=stored_digest,
            profile_id=str(selected["profile_id"]),
            rule_bundle_sha256=str(selected["rule_bundle_sha256"]),
            published_at=str(selected["published_at"]),
            data_generation=int(selected["data_generation"]),
            schema_generation=int(selected["schema_generation"]),
        )
    except BridgeIdentityError:
        raise
    except sqlite3.Error as exc:
        raise BridgeIdentityStoreUnavailable("Bridge identity store is unavailable.") from exc
    finally:
        if connection is not None:
            connection.close()


__all__ = [
    "BridgeIdentityError",
    "BridgeIdentityStoreUnavailable",
    "BridgeSnapshotIdentity",
    "BridgeSnapshotNotFound",
    "InvalidBridgeIdentity",
    "resolve_published_bridge_identity",
]
