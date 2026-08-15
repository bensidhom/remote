#!/usr/bin/env python3
"""Run the source-read-only BridgeGuard production migration rehearsal."""

from __future__ import annotations

import argparse
from pathlib import Path

from app.services.operations.migration_rehearsal import run_migration_rehearsal


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--shadow", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--app-revision", default="migration-rehearsal")
    args = parser.parse_args()
    receipt = run_migration_rehearsal(
        args.source,
        args.shadow,
        app_revision=args.app_revision,
    )
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(receipt.to_json() + "\n", encoding="utf-8", newline="\n")
    print(receipt.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
