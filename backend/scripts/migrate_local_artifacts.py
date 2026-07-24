"""Safely merge portable BridgeGuard artifacts from a desktop data directory.

The inventory database is never replaced. Report/point-cloud attachment rows are
merged transactionally, and legacy vector matrices are removed from copied RAG
caches so their parsed chunks can be re-embedded by the pinned TEI profile.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath


DIRECTORIES = (
    Path("attachments/reports"),
    Path("attachments/point_clouds"),
    Path("rag_storage/bridge_reports"),
    Path("rag_storage/page_images"),
    Path("rag_storage/extracted_images"),
)
FILES = (
    Path("imports/nj_real_bridge_data_manifest.json"),
    Path("imports/nj_point_cloud_processing.jsonl"),
    Path("reference/bridge_component_priors.json"),
    Path("reference/PSC Girders.xlsx"),
)
ATTACHMENT_TYPES = {"inspection_report", "point_cloud"}
TARGET_EMBEDDING_PROFILE = {
    "provider": "tei",
    "model": "nomic-ai/nomic-embed-text-v1.5",
    "revision": "e9b6763023c676ca8431644204f50c2b100d9aab",
    "runtime_version": "1.9.3",
    "dimension": 768,
    "input_policy": "nomic-v1.5-search-prefixes-v1",
    "normalize": True,
    "truncate": True,
    "truncation_direction": "right",
    "dtype": "float32",
    "pooling": "mean",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file(source: Path, destination: Path, stats: dict) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if (
            destination.stat().st_size == source.stat().st_size
            and _sha256(destination) == _sha256(source)
        ):
            stats["already_present"] += 1
            return
        stats["conflicts"].append(str(destination))
        return
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.copying-",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        if _sha256(temporary) != _sha256(source):
            raise OSError(f"checksum mismatch while copying {source}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    stats["files_copied"] += 1
    stats["bytes_copied"] += source.stat().st_size


def copy_portable_artifacts(source_root: Path, destination_root: Path) -> dict:
    stats = {
        "files_copied": 0,
        "bytes_copied": 0,
        "already_present": 0,
        "conflicts": [],
    }
    for relative in DIRECTORIES:
        source = source_root / relative
        if not source.is_dir():
            continue
        for item in source.rglob("*"):
            if item.is_file():
                _copy_file(item, destination_root / item.relative_to(source_root), stats)
    for relative in FILES:
        source = source_root / relative
        if source.is_file():
            _copy_file(source, destination_root / relative, stats)
    return stats


def invalidate_legacy_report_vectors(destination_root: Path) -> dict:
    """Keep parsed chunks/assets but force every report cache through TEI once."""
    cache_root = destination_root / "rag_storage" / "bridge_reports"
    result = {"caches": 0, "chunks": 0, "vectors_removed": 0}
    if not cache_root.is_dir():
        return result
    for cache_file in cache_root.glob("*.json"):
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("chunks"), list):
            continue
        if payload.get("embedding_profile") == TARGET_EMBEDDING_PROFILE:
            continue
        vectors = payload.get("embeddings")
        if isinstance(vectors, list):
            result["vectors_removed"] += len(vectors)
        payload["embeddings"] = None
        payload["image_embeddings"] = None
        payload["embedding_profile"] = None
        payload.pop("embedding_model", None)
        payload.pop("embedding_dim", None)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=cache_file.parent,
            prefix=f".{cache_file.name}.updating-",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
                json.dump(
                    payload,
                    temporary,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, cache_file)
        finally:
            Path(temporary_name).unlink(missing_ok=True)
        result["caches"] += 1
        result["chunks"] += len(payload["chunks"])
    return result


def _portable_path(raw_path: str, source_root: Path) -> Path | None:
    raw = str(raw_path)
    path = Path(raw)
    relative = None
    if path.is_absolute():
        try:
            relative = path.resolve().relative_to(source_root.resolve())
        except ValueError:
            pass
    candidate_parts = list(relative.parts) if relative is not None else list(path.parts)
    if "\\" in raw:
        candidate_parts = list(PureWindowsPath(raw).parts)
    lower_parts = [part.lower() for part in candidate_parts]
    try:
        attachment_index = lower_parts.index("attachments")
    except ValueError:
        return None
    return Path("attachments", *candidate_parts[attachment_index + 1 :])


def merge_attachment_rows(source_root: Path, destination_root: Path) -> dict:
    source_db = source_root / "snbi_bridges.db"
    destination_db = destination_root / "snbi_bridges.db"
    if not source_db.is_file() or not destination_db.is_file():
        raise FileNotFoundError("Both source and destination snbi_bridges.db files are required")

    backup_dir = destination_root / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_file = backup_dir / f"bridge_attachments_before_local_merge_{stamp}.json"
    database_backup = backup_dir / f"snbi_bridges_before_local_merge_{stamp}.db"

    with closing(sqlite3.connect(destination_db)) as live_database:
        with closing(sqlite3.connect(database_backup)) as backup_database:
            live_database.backup(backup_database)

    inserted = 0
    skipped = 0
    source_uri = f"file:{source_db.resolve().as_posix()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(source_uri, uri=True)) as source, closing(
        sqlite3.connect(destination_db)
    ) as destination:
        destination.row_factory = sqlite3.Row
        current = [
            dict(row)
            for row in destination.execute(
                "SELECT * FROM bridge_attachments ORDER BY id"
            ).fetchall()
        ]
        backup_file.write_text(json.dumps(current, indent=2, default=str), encoding="utf-8")

        source.row_factory = sqlite3.Row
        rows = source.execute(
            """
            SELECT state_code, bridge_number, attachment_type, filename,
                   filepath, size_bytes, uploaded_at
            FROM bridge_attachments
            WHERE attachment_type IN ('inspection_report', 'point_cloud')
            ORDER BY id
            """
        ).fetchall()
        destination.execute("BEGIN IMMEDIATE")
        for row in rows:
            if row["attachment_type"] not in ATTACHMENT_TYPES:
                continue
            relative = _portable_path(row["filepath"], source_root)
            if relative is None or not (destination_root / relative).is_file():
                skipped += 1
                continue
            portable = relative.as_posix()
            duplicate = destination.execute(
                """
                SELECT 1 FROM bridge_attachments
                WHERE state_code=? AND bridge_number=? AND attachment_type=?
                  AND filename=? AND filepath=?
                LIMIT 1
                """,
                (
                    str(row["state_code"]),
                    str(row["bridge_number"]),
                    row["attachment_type"],
                    row["filename"],
                    portable,
                ),
            ).fetchone()
            if duplicate:
                skipped += 1
                continue
            destination.execute(
                """
                INSERT INTO bridge_attachments
                    (state_code, bridge_number, attachment_type, filename,
                     filepath, size_bytes, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(row["state_code"]),
                    str(row["bridge_number"]),
                    row["attachment_type"],
                    row["filename"],
                    portable,
                    row["size_bytes"],
                    row["uploaded_at"],
                ),
            )
            inserted += 1
        destination.commit()
    return {
        "inserted": inserted,
        "skipped": skipped,
        "backup": str(backup_file),
        "database_backup": str(database_backup),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path, help="Desktop BridgeGuard data directory")
    parser.add_argument("destination", type=Path, help="Server BridgeGuard data directory")
    args = parser.parse_args()

    source = args.source.resolve()
    destination = args.destination.resolve()
    if source == destination:
        raise SystemExit("Source and destination must be different")
    if not source.is_dir() or not destination.is_dir():
        raise SystemExit("Source and destination must both exist")

    copied = copy_portable_artifacts(source, destination)
    if copied["conflicts"]:
        print(json.dumps({"copy": copied}, indent=2))
        raise SystemExit(
            "Migration stopped before database changes because destination "
            "files conflict with the source"
        )
    invalidated = invalidate_legacy_report_vectors(destination)
    attachments = merge_attachment_rows(source, destination)
    print(
        json.dumps(
            {
                "source": str(source),
                "destination": str(destination),
                "copy": copied,
                "rag_cache": invalidated,
                "attachments": attachments,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
