"""Create and verify a quiesced BridgeGuard customer-data backup.

The release workflow stops every writer before invoking this module.  A
current backup is nevertheless self-validating: it binds the deployment
marker, database binding, schema versions, and attachment catalog to the
payload checksums in its manifest.  Older formats remain readable for an
operator-led recovery, but are never accepted as a current release backup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tarfile
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from .routers.services.datapaths import DATA_DIR
from .routers.services.db_migrations import MIGRATIONS
from .routers.services.db_schema import (
    LATEST_SCHEMA_VERSION,
    SchemaDriftError,
    assert_schema_matches,
)
from .services.operations.jobs import JOB_SCHEMA_VERSION


CURRENT_BACKUP_FORMAT = "bridgeguard-backup-v6"
LEGACY_BACKUP_FORMATS = frozenset(
    {
        "bridgeguard-backup-v1",
        "bridgeguard-backup-v2",
        "bridgeguard-backup-v3",
        "bridgeguard-backup-v4",
        "bridgeguard-backup-v5",
    }
)
DEPLOYMENT_MARKER_FORMAT = "bridgeguard-deployment-policy-v2"
DEPLOYMENT_MARKER_FILENAME = "bridgeguard-deployment.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

BACKUP_DIRECTORIES = (
    "attachments",
    "conversion_sources",
    "rag_storage",
    "exports",
    "imports",
    "reference",
    "samples",
    "pointcloud_carve",
)
BACKUP_FILES = (
    "ai_provider.json",
    "api_usage.json",
    DEPLOYMENT_MARKER_FILENAME,
)
BACKUP_DATABASES = (
    "snbi_bridges.db",
    "snbi_forecasts.db",
    "operation_jobs.db",
)

_FORECAST_COLUMNS: dict[str, frozenset[str]] = {
    "forecast_runs": frozenset(
        {
            "id",
            "run_timestamp",
            "horizon_years",
            "status",
            "model_engine",
            "metrics_json",
            "message",
        }
    ),
    "training_series": frozenset(
        {
            "state_code",
            "bridge_number",
            "bridge_name",
            "data_year",
            "metric",
            "observed_value",
            "maintenance",
            "source_db",
        }
    ),
    "forecast_predictions": frozenset(
        {
            "run_id",
            "state_code",
            "bridge_number",
            "bridge_name",
            "metric",
            "forecast_year",
            "yhat",
            "yhat_lower",
            "yhat_upper",
            "confidence",
            "observation_count",
            "trend_direction",
            "model_engine",
        }
    ),
}
_JOB_COLUMNS = frozenset(
    {
        "id",
        "workspace_id",
        "owner_id",
        "operation",
        "idempotency_key",
        "payload_json",
        "payload_sha256",
        "status",
        "attempts",
        "max_attempts",
        "progress_json",
        "result_json",
        "error_code",
        "created_at",
        "updated_at",
        "available_at",
        "lease_owner",
        "lease_expires_at",
        "cancellation_requested_at",
    }
)


def _backup_database(source: Path, destination: Path) -> None:
    if not source.is_file() or source.is_symlink():
        raise RuntimeError(f"required backup database is missing or unsafe: {source.name}")
    with closing(sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)) as source_db:
        with closing(sqlite3.connect(destination)) as backup_db:
            source_db.backup(backup_db)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_record(path: Path) -> dict[str, int | str]:
    return {"bytes": path.stat().st_size, "sha256": _sha256(path)}


def _tables(connection: sqlite3.Connection) -> frozenset[str]:
    return frozenset(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    )


def _columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    # Callers pass constants or names read from sqlite_master, never user input.
    quoted = '"' + table.replace('"', '""') + '"'
    return frozenset(str(row[1]) for row in connection.execute(f"PRAGMA table_info({quoted})"))


def _basic_database_health(path: Path) -> tuple[sqlite3.Connection, dict[str, object]]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"required backup database is missing or unsafe: {path.name}")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"file:{path.resolve().as_posix()}?mode=ro", uri=True
        )
        connection.execute("PRAGMA query_only=ON")
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        foreign_key_issues = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    except Exception:
        if connection is not None:
            connection.close()
        raise
    if quick != "ok" or foreign_key_issues:
        connection.close()
        raise RuntimeError(f"database backup is not healthy: {path.name}")
    return connection, {
        "present": True,
        "quick_check": quick,
        "foreign_key_issues": foreign_key_issues,
        "user_version": user_version,
    }


def _canonical_uuid(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    try:
        canonical = str(UUID(text))
    except (ValueError, AttributeError) as exc:
        raise RuntimeError(f"{label} is missing or invalid") from exc
    if text != canonical:
        raise RuntimeError(f"{label} is not canonical")
    return canonical


def _deployment_identity(marker_path: Path) -> dict[str, str]:
    if not marker_path.is_file() or marker_path.is_symlink():
        raise RuntimeError("current backup requires a regular deployment marker")
    raw = marker_path.read_bytes()
    if not raw or len(raw) > 16 * 1024:
        raise RuntimeError("deployment marker is empty or oversized")
    try:
        marker = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("deployment marker is invalid JSON") from exc
    if not isinstance(marker, dict):
        raise RuntimeError("deployment marker must be a JSON object")
    expected_keys = {
        "format",
        "mode",
        "deployment_id",
        "customer_slug",
        "workspace_id",
    }
    if set(marker) != expected_keys:
        raise RuntimeError("deployment marker fields are not the exact current contract")
    if marker.get("format") != DEPLOYMENT_MARKER_FORMAT:
        raise RuntimeError("deployment marker format is not current")
    if marker.get("mode") != "dedicated_customer":
        raise RuntimeError("current release backup requires a dedicated deployment marker")
    customer_slug = str(marker.get("customer_slug") or "").strip()
    workspace_id = str(marker.get("workspace_id") or "").strip()
    if not customer_slug or not workspace_id:
        raise RuntimeError("deployment marker has no customer/workspace identity")
    identity = {
        "format": DEPLOYMENT_MARKER_FORMAT,
        "mode": "dedicated_customer",
        "deployment_id": _canonical_uuid(
            marker.get("deployment_id"), label="deployment marker deployment_id"
        ),
        "customer_slug": customer_slug,
        "workspace_id": workspace_id,
        "marker_sha256": hashlib.sha256(raw).hexdigest(),
    }
    return identity


def _research_identity() -> dict[str, str]:
    """Return the deliberately unbound identity used by research deployments.

    Research deployments share an operator-managed data root and are not
    customer installations.  They therefore must not require or fabricate a
    dedicated-customer marker/database binding merely to take a safe backup.
    """

    workspace_id = str(
        os.getenv("BRIDGEGUARD_WORKSPACE_ID", "shared-research-workspace")
    ).strip()
    if not workspace_id:
        raise RuntimeError("research backup requires a workspace identifier")
    return {
        "format": "bridgeguard-research-backup-identity-v1",
        "mode": "research_shared",
        "deployment_id": "unbound-research",
        "customer_slug": "",
        "workspace_id": workspace_id,
        "marker_sha256": "",
    }


def _backup_identity(data_dir: Path) -> dict[str, str]:
    mode = str(os.getenv("BRIDGEGUARD_DEPLOYMENT_MODE", "research_shared")).strip()
    if mode == "research_shared":
        return _research_identity()
    if mode == "dedicated_customer":
        return _deployment_identity(data_dir / DEPLOYMENT_MARKER_FILENAME)
    raise RuntimeError("unsupported backup deployment mode")


def _assert_environment_identity(identity: dict[str, str]) -> None:
    expected = {
        "BRIDGEGUARD_DEPLOYMENT_MODE": identity["mode"],
        "BRIDGEGUARD_WORKSPACE_ID": identity["workspace_id"],
    }
    if identity["mode"] == "dedicated_customer":
        expected["BRIDGEGUARD_CUSTOMER_SLUG"] = identity["customer_slug"]
    for key, marker_value in expected.items():
        configured = str(os.getenv(key, "")).strip()
        if configured and configured != marker_value:
            raise RuntimeError(f"deployment marker does not match {key}")


def _current_database_health(
    root: Path, identity: dict[str, str]
) -> dict[str, dict[str, object]]:
    health: dict[str, dict[str, object]] = {}

    main_path = root / "snbi_bridges.db"
    connection, evidence = _basic_database_health(main_path)
    try:
        if evidence["user_version"] != LATEST_SCHEMA_VERSION:
            raise RuntimeError("snbi_bridges.db user_version is not current")
        tables = _tables(connection)
        required = {
            "schema_migrations",
            "workspaces",
            "deployment_bindings",
            "bridge_attachments",
        }
        if not required.issubset(tables):
            raise RuntimeError("snbi_bridges.db is missing required release tables")
        attachment_columns = {
            "id",
            "workspace_id",
            "filepath",
            "size_bytes",
            "sha256",
        }
        if not attachment_columns.issubset(
            _columns(connection, "bridge_attachments")
        ):
            raise RuntimeError("snbi_bridges.db attachment catalog schema is incomplete")
        try:
            assert_schema_matches(connection)
        except SchemaDriftError as exc:
            raise RuntimeError("snbi_bridges.db schema does not match the release") from exc
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
            raise RuntimeError("snbi_bridges.db migration history does not match the release")
        bindings = connection.execute(
            "SELECT deployment_id,customer_slug,workspace_id,marker_sha256 "
            "FROM deployment_bindings ORDER BY deployment_id"
        ).fetchall()
        expected_binding = (
            identity["deployment_id"],
            identity["customer_slug"],
            identity["workspace_id"],
            identity["marker_sha256"],
        )
        if len(bindings) != 1 or tuple(bindings[0]) != expected_binding:
            raise RuntimeError("database deployment binding does not match the marker")
        workspaces = tuple(
            str(row[0])
            for row in connection.execute("SELECT id FROM workspaces ORDER BY id")
        )
        if workspaces != (identity["workspace_id"],):
            raise RuntimeError("database workspace catalog does not match the marker")
        workspace_tables_checked: list[str] = []
        for table in sorted(tables):
            columns = _columns(connection, table)
            if "workspace_id" not in columns:
                continue
            workspace_tables_checked.append(table)
            quoted = '"' + table.replace('"', '""') + '"'
            foreign_rows = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {quoted} WHERE workspace_id IS NULL "
                    "OR typeof(workspace_id)<>'text' OR workspace_id<>?",
                    (identity["workspace_id"],),
                ).fetchone()[0]
            )
            if foreign_rows:
                raise RuntimeError(
                    f"database workspace rows do not match the marker: {table}"
                )
        evidence["schema"] = "bridgeguard-main-v10"
        evidence["required_tables"] = sorted(required)
        evidence["workspace_tables_checked"] = workspace_tables_checked
    finally:
        connection.close()
    health["snbi_bridges.db"] = evidence

    forecast_path = root / "snbi_forecasts.db"
    connection, evidence = _basic_database_health(forecast_path)
    try:
        if evidence["user_version"] != 0:
            raise RuntimeError("snbi_forecasts.db user_version is unexpected")
        tables = _tables(connection)
        for table, expected_columns in _FORECAST_COLUMNS.items():
            if table not in tables or not expected_columns.issubset(
                _columns(connection, table)
            ):
                raise RuntimeError(f"snbi_forecasts.db schema is missing {table}")
        evidence["schema"] = "bridgeguard-forecast-v1"
        evidence["required_tables"] = sorted(_FORECAST_COLUMNS)
    finally:
        connection.close()
    health["snbi_forecasts.db"] = evidence

    jobs_path = root / "operation_jobs.db"
    connection, evidence = _basic_database_health(jobs_path)
    try:
        if evidence["user_version"] != JOB_SCHEMA_VERSION:
            raise RuntimeError("operation_jobs.db user_version is not current")
        tables = _tables(connection)
        if "operation_jobs" not in tables or not _JOB_COLUMNS.issubset(
            _columns(connection, "operation_jobs")
        ):
            raise RuntimeError("operation_jobs.db schema is not current")
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        if not {
            "idx_operation_jobs_claim",
            "idx_operation_jobs_owner",
        }.issubset(indexes) or "trg_operation_jobs_payload_immutable" not in triggers:
            raise RuntimeError("operation_jobs.db indexes/triggers are not current")
        foreign_workspaces = int(
            connection.execute(
                "SELECT COUNT(*) FROM operation_jobs WHERE workspace_id IS NULL "
                "OR typeof(workspace_id)<>'text' OR workspace_id<>?",
                (identity["workspace_id"],),
            ).fetchone()[0]
        )
        if foreign_workspaces:
            raise RuntimeError("operation job workspace does not match the deployment")
        evidence["schema"] = f"bridgeguard-operation-jobs-v{JOB_SCHEMA_VERSION}"
        evidence["required_tables"] = ["operation_jobs"]
    finally:
        connection.close()
    health["operation_jobs.db"] = evidence
    return health


def _research_database_health(
    root: Path, database_names: tuple[str, ...]
) -> dict[str, dict[str, object]]:
    """Capture integrity evidence without requiring a migrated customer DB."""

    health: dict[str, dict[str, object]] = {}
    for name in database_names:
        connection, evidence = _basic_database_health(root / name)
        try:
            evidence["tables"] = sorted(_tables(connection))
        finally:
            connection.close()
        health[name] = evidence
    return health


def _database_names_for_backup(
    root: Path, identity: dict[str, str]
) -> tuple[str, ...]:
    if identity["mode"] == "dedicated_customer":
        return BACKUP_DATABASES
    required = BACKUP_DATABASES[:2]
    for name in required:
        if not (root / name).is_file():
            raise RuntimeError(f"required research backup database is missing: {name}")
    optional = tuple(name for name in BACKUP_DATABASES[2:] if (root / name).is_file())
    return (*required, *optional)


def _backup_database_health(
    root: Path,
    identity: dict[str, str],
    database_names: tuple[str, ...],
) -> dict[str, dict[str, object]]:
    if identity["mode"] == "dedicated_customer":
        return _current_database_health(root, identity)
    return _research_database_health(root, database_names)


def _safe_attachment_path(root: Path, stored: object) -> tuple[Path, str]:
    value = str(stored or "").strip()
    if not value or "\x00" in value:
        raise RuntimeError("attachment catalog contains an empty or invalid filepath")
    root_resolved = root.resolve(strict=True)
    candidate = Path(value)
    try:
        lexical_relative = (
            candidate.relative_to(root_resolved)
            if candidate.is_absolute()
            else candidate
        )
    except ValueError as exc:
        raise RuntimeError("attachment catalog path is outside the data root") from exc
    if ".." in lexical_relative.parts or not lexical_relative.parts:
        raise RuntimeError("attachment catalog path contains traversal")
    current = root_resolved
    for part in lexical_relative.parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError("attachment catalog path contains a symlink")
    try:
        resolved = current.resolve(strict=True)
        relative = resolved.relative_to(root_resolved)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError("attachment catalog path is missing or outside the data root") from exc
    if not relative.parts or relative.parts[0] != "attachments":
        raise RuntimeError("attachment catalog path is outside the attachments store")
    if not resolved.is_file() or resolved.is_symlink():
        raise RuntimeError("attachment catalog path is not a regular file")
    return resolved, PurePosixPath(*relative.parts).as_posix()


def _attachment_evidence(root: Path, identity: dict[str, str]) -> dict[str, object]:
    database = root / "snbi_bridges.db"
    with closing(
        sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True)
    ) as connection:
        rows = connection.execute(
            "SELECT id,workspace_id,filepath,size_bytes,sha256 "
            "FROM bridge_attachments ORDER BY id"
        ).fetchall()
    records: list[dict[str, object]] = []
    referenced: set[str] = set()
    for row_id, workspace_id, filepath, expected_size, expected_sha in rows:
        if workspace_id != identity["workspace_id"]:
            raise RuntimeError("attachment workspace does not match the deployment")
        path, portable = _safe_attachment_path(root, filepath)
        if portable in referenced:
            raise RuntimeError("attachment catalog contains a duplicate filepath")
        referenced.add(portable)
        if not isinstance(expected_size, int) or expected_size < 0:
            raise RuntimeError("attachment catalog contains an invalid size")
        digest = str(expected_sha or "").strip().lower()
        if not _SHA256.fullmatch(digest):
            raise RuntimeError("attachment catalog contains an invalid sha256")
        actual = _file_record(path)
        if actual != {"bytes": expected_size, "sha256": digest}:
            raise RuntimeError("attachment file size/hash does not match its catalog row")
        records.append(
            {
                "id": int(row_id),
                "path": portable,
                "bytes": expected_size,
                "sha256": digest,
            }
        )

    # Derived point-cloud products and report parsing assets may intentionally
    # live below attachments without their own catalog row.  They are archived,
    # but the durable catalog contract is directional: every row must resolve
    # to exactly one confined regular source file with matching size/hash.
    canonical = json.dumps(
        records, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "count": len(records),
        "catalog_sha256": hashlib.sha256(canonical).hexdigest(),
        "total_bytes": sum(int(record["bytes"]) for record in records),
    }


def _point_cloud_run_evidence(root: Path, identity: dict[str, str]) -> dict[str, object]:
    """Reconcile every durable candidate/publication with immutable artifact bytes."""

    database = root / "snbi_bridges.db"
    workspace_id = identity["workspace_id"]
    workspace_token = "ws-" + hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:24]
    point_root = root / "attachments" / "point_clouds" / workspace_token
    with closing(
        sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        runs = connection.execute(
            "SELECT id,status,result_sha256,knowledge_pack_sha256,progress_json "
            "FROM point_cloud_runs WHERE workspace_id=? ORDER BY id",
            (workspace_id,),
        ).fetchall()
        publication_rows = connection.execute(
            "SELECT run_id,review_id FROM published_point_cloud_results "
            "WHERE workspace_id=? ORDER BY run_id",
            (workspace_id,),
        ).fetchall()
        publications = {str(row["run_id"]): str(row["review_id"]) for row in publication_rows}
        if len(publications) != len(publication_rows):
            raise RuntimeError("point-cloud publication rows are duplicated")
        records: list[dict[str, object]] = []
        total_artifact_bytes = 0
        for run in runs:
            run_id = str(run["id"])
            status = str(run["status"])
            counts = {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE workspace_id=? AND run_id=?",
                        (workspace_id, run_id),
                    ).fetchone()[0]
                )
                for table in (
                    "point_cloud_run_sources",
                    "point_cloud_vlm_calls",
                    "point_cloud_components",
                    "point_cloud_measurement_candidates",
                    "point_cloud_reviews",
                )
            }
            record: dict[str, object] = {
                "run_id": run_id,
                "status": status,
                "published": run_id in publications,
                "evidence_counts": counts,
            }
            if status == "succeeded":
                try:
                    progress = json.loads(str(run["progress_json"]))
                    generation = str(progress["source_generation"])
                    result_sha256 = str(run["result_sha256"] or "")
                    knowledge_sha256 = str(run["knowledge_pack_sha256"] or "")
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise RuntimeError("point-cloud run artifact receipt is invalid") from exc
                if not _SHA256.fullmatch(result_sha256) or not _SHA256.fullmatch(knowledge_sha256):
                    raise RuntimeError("point-cloud run artifact digest is invalid")
                matches = list(point_root.glob(f"*/processed/{generation}")) if point_root.is_dir() else []
                if len(matches) != 1 or matches[0].is_symlink() or not matches[0].is_dir():
                    raise RuntimeError("point-cloud run artifact generation is missing or ambiguous")
                generation_root = matches[0].resolve(strict=True)
                try:
                    generation_root.relative_to(root.resolve(strict=True))
                except ValueError as exc:
                    raise RuntimeError("point-cloud run artifact is outside the data root") from exc
                analysis_path = generation_root / "analysis.json"
                raw = analysis_path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != result_sha256:
                    raise RuntimeError("point-cloud run manifest digest mismatch")
                try:
                    analysis = json.loads(raw)
                    artifact_sha = analysis["persistence"]["artifact_sha256"]
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise RuntimeError("point-cloud run manifest is invalid") from exc
                artifact_records: dict[str, dict[str, object]] = {}
                for name, filename in (
                    ("npz", "processed_cloud.npz"),
                    ("ply", "processed_cloud.ply"),
                    ("knowledge_pack", "knowledge_pack.json"),
                ):
                    path = generation_root / filename
                    expected = str(artifact_sha.get(name) or "")
                    if name == "knowledge_pack":
                        expected = knowledge_sha256
                    if not path.is_file() or path.is_symlink() or not _SHA256.fullmatch(expected):
                        raise RuntimeError("point-cloud run artifact is missing")
                    actual = _file_record(path)
                    if actual["sha256"] != expected:
                        raise RuntimeError("point-cloud run artifact digest mismatch")
                    total_artifact_bytes += int(actual["bytes"])
                    artifact_records[name] = actual
                total_artifact_bytes += len(raw)
                record.update(
                    {
                        "source_generation": generation,
                        "result_sha256": result_sha256,
                        "artifacts": artifact_records,
                    }
                )
            elif run_id in publications:
                raise RuntimeError("non-succeeded point-cloud run is published")
            records.append(record)
        pending_retention = int(
            connection.execute(
                "SELECT COUNT(*) FROM point_cloud_retention_outbox "
                "WHERE workspace_id=? AND status!='complete'",
                (workspace_id,),
            ).fetchone()[0]
        )
    canonical = json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "run_count": len(records),
        "published_count": len(publications),
        "pending_retention_count": pending_retention,
        "artifact_bytes": total_artifact_bytes,
        "catalog_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def _database_health(path: Path) -> dict[str, object]:
    """Legacy health evidence retained for v1-v3 recovery verification."""

    if not path.is_file():
        return {"present": False}
    connection, evidence = _basic_database_health(path)
    connection.close()
    return evidence


def _research_storage_evidence(root: Path) -> dict[str, object]:
    """Hash the small research config files copied into the release backup.

    Large research attachments, RAG caches, and point-cloud derivatives remain
    mounted in place.  Recompressing those immutable stores on every research
    rollout can saturate a small VM for an hour and is not a deployment gate.
    Dedicated-customer backups continue to archive and reconcile all stores.
    """

    records: list[dict[str, object]] = []
    for name in BACKUP_FILES:
        if name == DEPLOYMENT_MARKER_FILENAME:
            continue
        path = root / name
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"backup source file is unsafe: {name}")
        record = _file_record(path)
        records.append(
            {"path": name, "bytes": record["bytes"], "sha256": record["sha256"]}
        )
    canonical = json.dumps(records, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return {
        "file_count": len(records),
        "total_bytes": sum(int(record["bytes"]) for record in records),
        "catalog_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def create_backup(output_root: Path) -> Path:
    data_dir = Path(DATA_DIR).resolve(strict=True)
    identity = _backup_identity(data_dir)
    _assert_environment_identity(identity)
    dedicated = identity["mode"] == "dedicated_customer"
    database_names = _database_names_for_backup(data_dir, identity)
    database_health = _backup_database_health(data_dir, identity, database_names)
    attachments_before = _attachment_evidence(data_dir, identity) if dedicated else None
    point_cloud_runs_before = (
        _point_cloud_run_evidence(data_dir, identity) if dedicated else None
    )
    storage_before = None if dedicated else _research_storage_evidence(data_dir)

    output_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = output_root / stamp
    staging = output_root / f".{stamp}.incomplete-{uuid4().hex}"
    staging.mkdir(parents=False, exist_ok=False)
    try:
        for database_name in database_names:
            _backup_database(data_dir / database_name, staging / database_name)
        with tarfile.open(staging / "files.tar.gz", "w:gz") as archive:
            archive_directories = BACKUP_DIRECTORIES if dedicated else ()
            archive_files = (
                BACKUP_FILES
                if dedicated
                else tuple(
                    name
                    for name in BACKUP_FILES
                    if name != DEPLOYMENT_MARKER_FILENAME
                )
            )
            for name in archive_directories:
                source = data_dir / name
                if source.exists():
                    if source.is_symlink():
                        raise RuntimeError(f"backup source directory is a symlink: {name}")
                    if any(item.is_symlink() for item in source.rglob("*")):
                        raise RuntimeError(
                            f"backup source directory contains a symlink: {name}"
                        )
                    archive.add(source, arcname=name, recursive=True)
            for name in archive_files:
                source = data_dir / name
                if source.is_file():
                    if source.is_symlink():
                        raise RuntimeError(f"backup source file is a symlink: {name}")
                    archive.add(source, arcname=name, recursive=False)
        if dedicated:
            shutil.copy2(
                data_dir / DEPLOYMENT_MARKER_FILENAME,
                staging / DEPLOYMENT_MARKER_FILENAME,
            )
            if _attachment_evidence(data_dir, identity) != attachments_before:
                raise RuntimeError("attachment catalog changed during the backup")
            if _point_cloud_run_evidence(data_dir, identity) != point_cloud_runs_before:
                raise RuntimeError("point-cloud run evidence changed during the backup")
            copied_identity = _deployment_identity(
                staging / DEPLOYMENT_MARKER_FILENAME
            )
            if copied_identity != identity:
                raise RuntimeError("copied deployment marker identity changed")
        elif _research_storage_evidence(data_dir) != storage_before:
            raise RuntimeError("research filesystem changed during the backup")
        copied_health = _backup_database_health(
            staging, identity, database_names
        )
        if copied_health != database_health:
            raise RuntimeError("copied database health evidence changed")

        payload_files = {
            path.name: _file_record(path)
            for path in staging.iterdir()
            if path.is_file() and path.name != "manifest.json"
        }
        required_payloads = {*database_names, "files.tar.gz"}
        if dedicated:
            required_payloads.add(DEPLOYMENT_MARKER_FILENAME)
        if not required_payloads.issubset(payload_files):
            raise RuntimeError("current backup is missing a required payload")
        manifest = {
            "format": CURRENT_BACKUP_FORMAT,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "deployment": identity,
            "database_payloads": list(database_names),
            "embedding_profile": {
                "provider": "tei",
                "model": os.getenv(
                    "TEI_EMBED_MODEL", "nomic-ai/nomic-embed-text-v1.5"
                ),
                "revision": os.getenv(
                    "TEI_EMBED_REVISION",
                    "20d1e6057a2e1fa08b45123e41ce9381fc338f0e",
                ),
                "runtime_version": os.getenv("TEI_RUNTIME_VERSION", "1.9.3"),
                "dimension": int(os.getenv("TEI_EMBED_DIM", "768")),
            },
            "files": payload_files,
            "database_health": copied_health,
            "attachments": attachments_before,
            "point_cloud_runs": point_cloud_runs_before,
            "research_storage": storage_before,
            "research_unarchived_directories": (
                None if dedicated else list(BACKUP_DIRECTORIES)
            ),
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        # Windows scanners can retain a just-closed SQLite/archive handle for a
        # few milliseconds.  Publish remains one same-filesystem atomic rename;
        # retry only that transient permission error and never merge/overwrite.
        for attempt in range(5):
            try:
                staging.replace(target)
                break
            except PermissionError:
                if attempt == 4 or target.exists():
                    raise
                time.sleep(0.05 * (attempt + 1))
        return target
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_backup(target: Path, *, require_current: bool = False) -> dict:
    manifest_path = target / "manifest.json"
    try:
        if (
            not manifest_path.is_file()
            or manifest_path.is_symlink()
            or manifest_path.stat().st_size > 1024 * 1024
        ):
            raise RuntimeError("backup manifest is missing, linked, or oversized")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except RuntimeError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("backup manifest is missing or invalid") from exc
    if not isinstance(manifest, dict):
        raise RuntimeError("backup manifest must be a JSON object")
    backup_format = manifest.get("format")
    supported = LEGACY_BACKUP_FORMATS | {CURRENT_BACKUP_FORMAT}
    if backup_format not in supported:
        raise RuntimeError("unsupported or missing BridgeGuard backup format")
    if require_current and backup_format != CURRENT_BACKUP_FORMAT:
        raise RuntimeError("legacy backup is not eligible for release reuse")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise RuntimeError("backup manifest has no payload files")
    for name, expected in files.items():
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or "/" in name
            or "\\" in name
            or ":" in name
        ):
            raise RuntimeError("backup manifest contains an unsafe payload name")
        path = target / name
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"backup payload is missing: {name}")
        actual = _file_record(path)
        if actual != expected:
            raise RuntimeError(f"backup payload failed checksum validation: {name}")

    if backup_format in {"bridgeguard-backup-v2", "bridgeguard-backup-v3"}:
        expected_health = manifest.get("database_health")
        if not isinstance(expected_health, dict):
            raise RuntimeError("backup manifest has no database health evidence")
        required_databases = (
            BACKUP_DATABASES
            if backup_format == "bridgeguard-backup-v3"
            else ("snbi_bridges.db", "snbi_forecasts.db")
        )
        for name in required_databases:
            if _database_health(target / name) != expected_health.get(name):
                raise RuntimeError(f"backup database health mismatch: {name}")

    if backup_format == "bridgeguard-backup-v3":
        deployment = manifest.get("deployment")
        if not isinstance(deployment, dict) or not deployment.get("workspace_id"):
            raise RuntimeError("backup manifest has no deployment identity")
        if deployment.get("mode") == "dedicated_customer":
            if not deployment.get("customer_slug"):
                raise RuntimeError("dedicated backup has no customer identity")
            if DEPLOYMENT_MARKER_FILENAME not in files:
                raise RuntimeError("dedicated backup has no deployment marker")

    if backup_format == CURRENT_BACKUP_FORMAT:
        deployment = manifest.get("deployment")
        if not isinstance(deployment, dict):
            raise RuntimeError("backup manifest has no deployment scope")
        mode = deployment.get("mode")
        raw_database_names = manifest.get("database_payloads")
        if (
            not isinstance(raw_database_names, list)
            or not raw_database_names
            or any(name not in BACKUP_DATABASES for name in raw_database_names)
            or len(set(raw_database_names)) != len(raw_database_names)
            or not set(BACKUP_DATABASES[:2]).issubset(raw_database_names)
        ):
            raise RuntimeError("backup manifest database payload list is invalid")
        database_names = tuple(str(name) for name in raw_database_names)
        required_payloads = {*database_names, "files.tar.gz"}
        if mode == "dedicated_customer":
            required_payloads.add(DEPLOYMENT_MARKER_FILENAME)
        if not required_payloads.issubset(files):
            raise RuntimeError("current backup is missing a required payload")
        if mode == "dedicated_customer":
            identity = _deployment_identity(target / DEPLOYMENT_MARKER_FILENAME)
            if deployment != identity:
                raise RuntimeError(
                    "backup manifest deployment identity does not match its marker"
                )
        elif mode == "research_shared":
            expected_keys = {
                "format",
                "mode",
                "deployment_id",
                "customer_slug",
                "workspace_id",
                "marker_sha256",
            }
            if (
                set(deployment) != expected_keys
                or deployment.get("format")
                != "bridgeguard-research-backup-identity-v1"
                or deployment.get("deployment_id") != "unbound-research"
                or deployment.get("customer_slug") != ""
                or not str(deployment.get("workspace_id") or "").strip()
                or deployment.get("marker_sha256") != ""
            ):
                raise RuntimeError("research backup identity is invalid")
            identity = {str(key): str(value) for key, value in deployment.items()}
        else:
            raise RuntimeError("backup deployment mode is unsupported")
        health = _backup_database_health(target, identity, database_names)
        if manifest.get("database_health") != health:
            raise RuntimeError("backup database health/schema evidence mismatch")
        if mode == "dedicated_customer":
            attachment_manifest = manifest.get("attachments")
            if not isinstance(attachment_manifest, dict):
                raise RuntimeError("backup manifest has no attachment evidence")
            point_cloud_manifest = manifest.get("point_cloud_runs")
            if not isinstance(point_cloud_manifest, dict):
                raise RuntimeError("backup manifest has no point-cloud run evidence")
        elif not isinstance(manifest.get("research_storage"), dict):
            raise RuntimeError("backup manifest has no research storage evidence")
        elif manifest.get("research_unarchived_directories") != list(
            BACKUP_DIRECTORIES
        ):
            raise RuntimeError("research backup storage scope is invalid")
    return manifest


def _safe_archive_members(archive: tarfile.TarFile, root: Path) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    if len(members) > 250_000:
        raise RuntimeError("backup archive contains too many entries")
    allowed_roots = set(BACKUP_DIRECTORIES) | set(BACKUP_FILES)
    seen: set[str] = set()
    for member in members:
        portable = PurePosixPath(member.name)
        if (
            not member.name
            or portable.is_absolute()
            or ".." in portable.parts
            or "\\" in member.name
            or any(":" in part for part in portable.parts)
        ):
            raise RuntimeError("backup archive contains an unsafe path")
        normalized = portable.as_posix()
        if normalized in seen:
            raise RuntimeError("backup archive contains a duplicate path")
        seen.add(normalized)
        if not portable.parts or portable.parts[0] not in allowed_roots:
            raise RuntimeError("backup archive contains an unexpected payload root")
        destination = (root / Path(*portable.parts)).resolve()
        try:
            destination.relative_to(root.resolve())
        except ValueError as exc:
            raise RuntimeError("backup archive contains an unsafe path") from exc
        if member.issym() or member.islnk() or member.isdev():
            raise RuntimeError("backup archive contains an unsafe member type")
    return members


def verify_restore_drill(
    target: Path,
    *,
    workspace_root: Path | None = None,
    require_current: bool = False,
) -> dict[str, object]:
    """Restore a backup into a temporary clean root and verify it read-only."""

    manifest = verify_backup(target, require_current=require_current)
    base = (
        Path(workspace_root)
        if workspace_root is not None
        else target.parent / ".restore-drills"
    )
    base.mkdir(parents=True, exist_ok=True)
    root = base / (
        f"drill-{os.getpid()}-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    )
    root.mkdir(parents=False, exist_ok=False)
    try:
        current = manifest.get("format") == CURRENT_BACKUP_FORMAT
        if current:
            database_names = tuple(manifest["database_payloads"])
        elif manifest.get("format") in {
            "bridgeguard-backup-v3",
            "bridgeguard-backup-v4",
            "bridgeguard-backup-v5",
        }:
            database_names = BACKUP_DATABASES
        else:
            database_names = ("snbi_bridges.db", "snbi_forecasts.db")
        for database_name in database_names:
            source = target / database_name
            if current and not source.is_file():
                raise RuntimeError(f"restore drill is missing required database: {database_name}")
            if source.is_file():
                shutil.copy2(source, root / database_name)
        marker_source = target / DEPLOYMENT_MARKER_FILENAME
        dedicated = (
            current
            and manifest.get("deployment", {}).get("mode") == "dedicated_customer"
        )
        if dedicated and not marker_source.is_file():
            raise RuntimeError("restore drill is missing the deployment marker")
        if marker_source.is_file():
            shutil.copy2(marker_source, root / marker_source.name)
        archive_path = target / "files.tar.gz"
        if current and not archive_path.is_file():
            raise RuntimeError("restore drill is missing the filesystem archive")
        extracted_files = 0
        if archive_path.is_file():
            with tarfile.open(archive_path, "r:gz") as archive:
                members = _safe_archive_members(archive, root)
                archive.extractall(root, filter="data")
                extracted_files = sum(1 for item in members if item.isfile())

        if current:
            if dedicated:
                restored_identity = _deployment_identity(
                    root / DEPLOYMENT_MARKER_FILENAME
                )
                if restored_identity != manifest["deployment"]:
                    raise RuntimeError(
                        "restored deployment identity does not match the manifest"
                    )
            else:
                restored_identity = manifest["deployment"]
            database_health = _backup_database_health(
                root, restored_identity, database_names
            )
            if database_health != manifest["database_health"]:
                raise RuntimeError("restored database health/schema evidence mismatch")
            if dedicated:
                attachment_evidence = _attachment_evidence(root, restored_identity)
                if attachment_evidence != manifest["attachments"]:
                    raise RuntimeError("restored attachments do not match the manifest")
                point_cloud_evidence = _point_cloud_run_evidence(root, restored_identity)
                if point_cloud_evidence != manifest["point_cloud_runs"]:
                    raise RuntimeError("restored point-cloud runs do not match the manifest")
            elif _research_storage_evidence(root) != manifest["research_storage"]:
                raise RuntimeError("restored research files do not match the manifest")
        else:
            database_health = {
                name: _database_health(root / name) for name in database_names
            }
        return {
            "format": "bridgeguard-restore-drill-v2",
            "source_backup_format": manifest["format"],
            "database_health": database_health,
            "extracted_file_count": extracted_files,
            "verified": True,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(DATA_DIR) / "backups")
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--verify-current", type=Path)
    parser.add_argument("--restore-drill", type=Path)
    parser.add_argument("--restore-drill-current", type=Path)
    args = parser.parse_args()
    if args.verify:
        print(json.dumps(verify_backup(args.verify), indent=2, sort_keys=True))
        return
    if args.verify_current:
        print(
            json.dumps(
                verify_backup(args.verify_current, require_current=True),
                indent=2,
                sort_keys=True,
            )
        )
        return
    if args.restore_drill:
        print(
            json.dumps(
                verify_restore_drill(args.restore_drill), indent=2, sort_keys=True
            )
        )
        return
    if args.restore_drill_current:
        print(
            json.dumps(
                verify_restore_drill(
                    args.restore_drill_current, require_current=True
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return
    result = create_backup(args.output)
    verify_backup(result, require_current=True)
    print(result)


if __name__ == "__main__":
    main()
