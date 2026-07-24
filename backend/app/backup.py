"""Create a consistent local backup of BridgeGuard's persistent data."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tarfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .routers.services.datapaths import DATA_DIR

BACKUP_DIRECTORIES = (
    "attachments",
    "rag_storage",
    "exports",
    "imports",
    "reference",
    "samples",
    "pointcloud_carve",
)
BACKUP_FILES = ("ai_provider.json", "api_usage.json")


def _backup_database(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    with closing(
        sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    ) as source_db:
        with closing(sqlite3.connect(destination)) as backup_db:
            source_db.backup(backup_db)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_record(path: Path) -> dict:
    return {"bytes": path.stat().st_size, "sha256": _sha256(path)}


def create_backup(output_root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = output_root / stamp
    target.mkdir(parents=True, exist_ok=False)
    data_dir = Path(DATA_DIR)
    _backup_database(data_dir / "snbi_bridges.db", target / "snbi_bridges.db")
    _backup_database(data_dir / "snbi_forecasts.db", target / "snbi_forecasts.db")
    with tarfile.open(target / "files.tar.gz", "w:gz") as archive:
        for name in BACKUP_DIRECTORIES:
            source = data_dir / name
            if source.exists():
                archive.add(source, arcname=name, recursive=True)
        for name in BACKUP_FILES:
            source = data_dir / name
            if source.is_file():
                archive.add(source, arcname=name, recursive=False)
    payload_files = {
        path.name: _file_record(path)
        for path in target.iterdir()
        if path.is_file() and path.name != "manifest.json"
    }
    manifest = {
        "format": "bridgeguard-backup-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
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
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return target


def verify_backup(target: Path) -> dict:
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "bridgeguard-backup-v1":
        raise RuntimeError("unsupported or missing BridgeGuard backup format")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise RuntimeError("backup manifest has no payload files")
    for name, expected in files.items():
        path = target / name
        if not path.is_file():
            raise RuntimeError(f"backup payload is missing: {name}")
        actual = _file_record(path)
        if actual != expected:
            raise RuntimeError(f"backup payload failed checksum validation: {name}")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(DATA_DIR) / "backups")
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify:
        result = verify_backup(args.verify)
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    result = create_backup(args.output)
    verify_backup(result)
    print(result)


if __name__ == "__main__":
    main()
