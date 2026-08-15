"""SQLite runtime safety checks for BridgeGuard's WAL databases.

SQLite documents a WAL-reset race in upstream releases 3.7.0 through 3.51.2.
The upstream fixes are in 3.51.3 and the 3.50.7 / 3.44.6 backports.  This
module deliberately uses an allow-list: an unrelated version branch is not
assumed to contain the fix merely because its version number is higher than a
backported branch.

Distributions sometimes backport fixes without changing ``sqlite_version``.
Such a build may be approved only with both an explicit environment flag and
a non-empty vendor/reference identifier so the exception is auditable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
import sqlite3
from typing import Mapping, Sequence


_TRUE_VALUES = {"1", "true", "yes", "on"}
_UPSTREAM_FIXED = (3, 51, 3)
_FIXED_BACKPORTS = {
    (3, 50): 7,
    (3, 44): 6,
}


@dataclass(frozen=True)
class SQLiteWalRuntimeStatus:
    runtime_version: str
    journal_mode: str
    wal_enabled: bool
    wal_fix_present: bool
    approved: bool
    fix_source: str
    reason: str

    def public_dict(self) -> dict[str, object]:
        """Return diagnostics that contain no database path or record data."""

        return asdict(self)


def _version_tuple(version: Sequence[int] | str) -> tuple[int, int, int]:
    if isinstance(version, str):
        pieces = version.strip().split(".")
        if len(pieces) < 3:
            raise ValueError(f"SQLite version must contain major.minor.patch: {version!r}")
        try:
            return tuple(int(piece) for piece in pieces[:3])  # type: ignore[return-value]
        except ValueError as exc:
            raise ValueError(f"Invalid SQLite version: {version!r}") from exc

    if len(version) < 3:
        raise ValueError("SQLite version must contain major, minor, and patch")
    return int(version[0]), int(version[1]), int(version[2])


def upstream_wal_fix_present(version: Sequence[int] | str) -> bool:
    """Return whether *version* is an upstream release with the WAL fix."""

    parsed = _version_tuple(version)
    if parsed >= _UPSTREAM_FIXED:
        return True
    minimum_patch = _FIXED_BACKPORTS.get(parsed[:2])
    return minimum_patch is not None and parsed[2] >= minimum_patch


def inspect_sqlite_wal_runtime(
    journal_mode: str,
    *,
    version: Sequence[int] | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> SQLiteWalRuntimeStatus:
    """Classify the active SQLite runtime for a database journal mode.

    ``BRIDGEGUARD_SQLITE_WAL_FIX_BACKPORT`` is intentionally insufficient on
    its own.  ``BRIDGEGUARD_SQLITE_WAL_FIX_REFERENCE`` must identify the vendor
    advisory/package revision or internal approval record.
    """

    active_version = _version_tuple(version or sqlite3.sqlite_version_info)
    runtime_version = ".".join(str(value) for value in active_version)
    normalized_mode = str(journal_mode or "unknown").strip().lower()
    wal_enabled = normalized_mode == "wal"
    source_env = os.environ if environ is None else environ
    vendor_flag = str(source_env.get("BRIDGEGUARD_SQLITE_WAL_FIX_BACKPORT", "")).strip().lower()
    vendor_reference = str(source_env.get("BRIDGEGUARD_SQLITE_WAL_FIX_REFERENCE", "")).strip()
    upstream_fixed = upstream_wal_fix_present(active_version)
    vendor_fixed = vendor_flag in _TRUE_VALUES and bool(vendor_reference)
    fix_present = upstream_fixed or vendor_fixed

    if upstream_fixed:
        fix_source = "upstream_release"
    elif vendor_fixed:
        # The reference is required as an operator-side audit record, but is not
        # echoed by public readiness diagnostics because environment values can
        # contain deployment-sensitive identifiers.
        fix_source = "vendor_backport"
    else:
        fix_source = "none"

    if not wal_enabled:
        approved = True
        reason = "WAL is not enabled for this database."
    elif fix_present:
        approved = True
        reason = "SQLite WAL runtime contains an approved WAL-reset fix."
    elif vendor_flag in _TRUE_VALUES and not vendor_reference:
        approved = False
        reason = "Vendor backport flag requires BRIDGEGUARD_SQLITE_WAL_FIX_REFERENCE."
    else:
        approved = False
        reason = (
            "SQLite WAL runtime is in an unapproved version branch; use SQLite "
            "3.51.3+ or an approved 3.50.7+/3.44.6+ backport."
        )

    return SQLiteWalRuntimeStatus(
        runtime_version=runtime_version,
        journal_mode=normalized_mode,
        wal_enabled=wal_enabled,
        wal_fix_present=fix_present,
        approved=approved,
        fix_source=fix_source,
        reason=reason,
    )


def require_safe_sqlite_wal_runtime(journal_mode: str) -> SQLiteWalRuntimeStatus:
    """Raise when the active process uses an unapproved SQLite WAL runtime."""

    status = inspect_sqlite_wal_runtime(journal_mode)
    if not status.approved:
        raise RuntimeError(
            f"Unsafe SQLite WAL runtime {status.runtime_version}: {status.reason}"
        )
    return status
