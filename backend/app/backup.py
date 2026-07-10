"""Create a consistent local backup of BridgeGuard's persistent data."""

from __future__ import annotations

import argparse
import sqlite3
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from .routers.services.datapaths import DATA_DIR


def _backup_database(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_db:
        with sqlite3.connect(destination) as backup_db:
            source_db.backup(backup_db)


def create_backup(output_root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = output_root / stamp
    target.mkdir(parents=True, exist_ok=False)
    data_dir = Path(DATA_DIR)
    _backup_database(data_dir / "snbi_bridges.db", target / "snbi_bridges.db")
    _backup_database(data_dir / "snbi_forecasts.db", target / "snbi_forecasts.db")
    with tarfile.open(target / "files.tar.gz", "w:gz") as archive:
        for name in ("attachments", "rag_storage", "exports"):
            source = data_dir / name
            if source.exists():
                archive.add(source, arcname=name, recursive=True)
    return target


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(DATA_DIR) / "backups")
    args = parser.parse_args()
    result = create_backup(args.output)
    print(result)


if __name__ == "__main__":
    main()

