"""Immutable database/customer binding for dedicated deployments.

Normal application startup is read-only until :func:`verify_dedicated_database_binding`
has proved that the marker, database binding, workspace catalog, and every
workspace-bearing table agree.  Only the explicit bootstrap function may
create the first binding, and it only operates on an empty provisioned root.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3
from typing import Mapping
from urllib.parse import quote

from app.routers.services.db_migrations import MIGRATIONS
from app.routers.services.db_schema import (
    LATEST_SCHEMA_VERSION,
    SchemaDriftError,
    assert_schema_matches,
)

from .deployment import (
    DeploymentMode,
    DeploymentPolicy,
    require_deployment_policy,
)
from .provisioning import DEDICATED_DATA_DIRECTORIES


DATABASE_FILENAME = "snbi_bridges.db"
_BINDING_TABLE = "deployment_bindings"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DeploymentDatabaseBindingError(RuntimeError):
    """Raised when a database cannot be proven to belong to its marker."""

    def __init__(self, blockers: tuple[str, ...]):
        self.blockers = tuple(dict.fromkeys(blockers))
        super().__init__("; ".join(self.blockers))


@dataclass(frozen=True, slots=True)
class DeploymentDatabaseReceipt:
    deployment_id: str
    customer_slug: str
    workspace_id: str
    marker_sha256: str
    inspected_workspace_tables: int
    created: bool = False

    def public_dict(self) -> dict[str, object]:
        return {
            "deployment_id": self.deployment_id,
            "customer_slug": self.customer_slug,
            "workspace_id": self.workspace_id,
            "marker_sha256": self.marker_sha256,
            "inspected_workspace_tables": self.inspected_workspace_tables,
            "created": self.created,
        }


def _quoted_identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise DeploymentDatabaseBindingError(("database_identifier_invalid",))
    return f'"{value}"'


def _readonly_connection(path: Path) -> sqlite3.Connection:
    encoded = quote(path.resolve().as_posix(), safe="/:")
    connection = sqlite3.connect(f"file:{encoded}?mode=ro", uri=True, timeout=5.0)
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA trusted_schema = OFF")
    return connection


def _required_policy_identity(
    policy: DeploymentPolicy,
) -> tuple[str, str, str, str]:
    blockers: list[str] = []
    if policy.mode is not DeploymentMode.DEDICATED_CUSTOMER:
        blockers.append("dedicated_database_requires_dedicated_mode")
    if not policy.marker_verified:
        blockers.append("dedicated_deployment_marker_unverified")
    if not policy.customer_slug:
        blockers.append("dedicated_customer_slug_missing")
    if not policy.deployment_id:
        blockers.append("dedicated_deployment_id_missing")
    if not policy.marker_sha256:
        blockers.append("dedicated_marker_digest_missing")
    if blockers:
        raise DeploymentDatabaseBindingError(tuple(blockers))
    return (
        policy.deployment_id or "",
        policy.customer_slug or "",
        policy.workspace_id,
        policy.marker_sha256 or "",
    )


def verify_dedicated_database_binding(
    database_path: str | Path,
    policy: DeploymentPolicy,
    *,
    created: bool = False,
) -> DeploymentDatabaseReceipt:
    """Read-only proof that a dedicated database belongs to ``policy``.

    The check intentionally runs before migrations or legacy attachment
    adoption.  NULL workspace rows are contamination, not values startup may
    infer on behalf of a paying customer.
    """

    deployment_id, customer_slug, workspace_id, marker_sha256 = (
        _required_policy_identity(policy)
    )
    path = Path(database_path).expanduser()
    blockers: list[str] = []
    if path.is_symlink():
        blockers.append("dedicated_database_symlink")
    if not path.is_file():
        blockers.append("dedicated_database_missing")
    if blockers:
        raise DeploymentDatabaseBindingError(tuple(blockers))

    inspected = 0
    connection: sqlite3.Connection | None = None
    try:
        connection = _readonly_connection(path)
        tables = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        )
        table_set = set(tables)
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if user_version != LATEST_SCHEMA_VERSION:
            blockers.append("dedicated_database_schema_version_mismatch")
        if "schema_migrations" in table_set:
            history = tuple(
                (int(row[0]), str(row[1]))
                for row in connection.execute(
                    "SELECT version,checksum FROM schema_migrations ORDER BY version"
                ).fetchall()
            )
            expected_history = tuple(
                (migration.version, migration.checksum) for migration in MIGRATIONS
            )
            if history != expected_history:
                blockers.append("dedicated_database_migration_history_mismatch")
        else:
            blockers.append("dedicated_database_migration_history_missing")
        try:
            assert_schema_matches(connection)
        except SchemaDriftError:
            blockers.append("dedicated_database_schema_drift")
        if _BINDING_TABLE not in table_set:
            blockers.append("dedicated_database_binding_missing")
        else:
            rows = connection.execute(
                "SELECT deployment_id, customer_slug, workspace_id, marker_sha256 "
                "FROM deployment_bindings ORDER BY deployment_id"
            ).fetchall()
            expected = (deployment_id, customer_slug, workspace_id, marker_sha256)
            if len(rows) != 1:
                blockers.append("dedicated_database_binding_count_invalid")
            elif tuple(rows[0]) != expected:
                blockers.append("dedicated_database_binding_mismatch")

        if "workspaces" not in table_set:
            blockers.append("dedicated_database_workspaces_missing")
        else:
            workspaces = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT id FROM workspaces ORDER BY id"
                ).fetchall()
            )
            if workspaces != (workspace_id,):
                blockers.append("dedicated_database_workspace_catalog_mismatch")

        if "bridge_attachments" not in table_set:
            blockers.append("dedicated_database_attachments_missing")

        for table_name in tables:
            quoted = _quoted_identifier(table_name)
            columns = {
                str(row[1])
                for row in connection.execute(
                    f"PRAGMA table_info({quoted})"
                ).fetchall()
            }
            if table_name == "bridge_attachments" and "workspace_id" not in columns:
                blockers.append("dedicated_database_attachment_workspace_missing")
            if "workspace_id" not in columns:
                continue
            inspected += 1
            null_count = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {quoted} WHERE workspace_id IS NULL"
                ).fetchone()[0]
            )
            if null_count:
                blockers.append(f"dedicated_database_null_workspace:{table_name}")
            foreign_count = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {quoted} "
                    "WHERE workspace_id IS NOT NULL "
                    "AND (typeof(workspace_id) <> 'text' OR workspace_id <> ?)",
                    (workspace_id,),
                ).fetchone()[0]
            )
            if foreign_count:
                blockers.append(f"dedicated_database_foreign_workspace:{table_name}")
    except DeploymentDatabaseBindingError:
        raise
    except sqlite3.Error as exc:
        raise DeploymentDatabaseBindingError(
            (f"dedicated_database_unreadable:{type(exc).__name__}",)
        ) from exc
    finally:
        if connection is not None:
            connection.close()

    if blockers:
        raise DeploymentDatabaseBindingError(tuple(blockers))
    return DeploymentDatabaseReceipt(
        deployment_id=deployment_id,
        customer_slug=customer_slug,
        workspace_id=workspace_id,
        marker_sha256=marker_sha256,
        inspected_workspace_tables=inspected,
        created=created,
    )


def _assert_empty_provisioned_root(root: Path) -> None:
    expected = {*DEDICATED_DATA_DIRECTORIES, "bridgeguard-deployment.json"}
    actual = {item.name for item in root.iterdir()}
    if actual != expected:
        raise DeploymentDatabaseBindingError(("dedicated_bootstrap_layout_not_empty",))
    for name in DEDICATED_DATA_DIRECTORIES:
        path = root / name
        if path.is_symlink() or not path.is_dir() or any(path.iterdir()):
            raise DeploymentDatabaseBindingError(("dedicated_bootstrap_layout_not_empty",))


def _create_attachment_catalog(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE bridge_attachments (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            state_code      TEXT    NOT NULL,
            bridge_number   TEXT    NOT NULL,
            attachment_type TEXT    NOT NULL,
            filename        TEXT    NOT NULL,
            filepath        TEXT    NOT NULL,
            size_bytes      INTEGER,
            sha256          TEXT,
            workspace_id    TEXT,
            uploaded_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def bootstrap_dedicated_database(
    data_root: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> DeploymentDatabaseReceipt:
    """Initialize and bind one newly provisioned, empty customer database.

    A valid existing binding makes this an idempotent read-only operation.  An
    existing unbound or mismatched database is never adopted.
    """

    root = Path(data_root).expanduser().resolve(strict=False)
    policy = require_deployment_policy(environ=environ, data_directory=root)
    _required_policy_identity(policy)
    target = root / DATABASE_FILENAME

    if target.exists() or target.is_symlink():
        return verify_dedicated_database_binding(target, policy, created=False)

    _assert_empty_provisioned_root(root)
    lock_path = root / ".bridgeguard-database-bootstrap.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise DeploymentDatabaseBindingError(("dedicated_bootstrap_in_progress",)) from exc

    created_target = False
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(str(os.getpid()))
            stream.flush()
            os.fsync(stream.fileno())

        # Imports are deferred so read-only startup verification does not pull
        # in the legacy database module or trigger any of its initialization.
        from app.routers.services.database import init_db
        from app.services.conversion.revision_store import ensure_workspace

        # From here onward the target is exclusively owned by this bootstrap;
        # remove it on any partial initialization failure.
        created_target = True
        init_db(
            str(target),
            allow_bundled_seed=False,
            journal_mode_override="delete",
        )
        ensure_workspace(
            str(target),
            policy.workspace_id,
            f"BridgeGuard dedicated customer: {policy.customer_slug}",
        )
        deployment_id, customer_slug, workspace_id, marker_sha256 = (
            _required_policy_identity(policy)
        )
        connection = sqlite3.connect(str(target), timeout=30.0)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            _create_attachment_catalog(connection)
            connection.execute(
                "INSERT INTO deployment_bindings "
                "(deployment_id, customer_slug, workspace_id, marker_sha256, bound_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    deployment_id,
                    customer_slug,
                    workspace_id,
                    marker_sha256,
                    datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return verify_dedicated_database_binding(target, policy, created=True)
    except Exception:
        if created_target:
            for suffix in ("", "-wal", "-shm", "-journal"):
                (Path(str(target) + suffix)).unlink(missing_ok=True)
        raise
    finally:
        lock_path.unlink(missing_ok=True)


__all__ = [
    "DATABASE_FILENAME",
    "DeploymentDatabaseBindingError",
    "DeploymentDatabaseReceipt",
    "bootstrap_dedicated_database",
    "verify_dedicated_database_binding",
]
