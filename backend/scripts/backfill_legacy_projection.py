"""Run the two-phase legacy-projection backfill with a compact receipt.

This command never modifies the source database.  ``dry-run`` inventories the
exact source bytes and prints the manifest precondition required by ``apply``.
``apply`` creates or updates only the explicit destination and can publish all
reconstructed snapshots atomically.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.services.conversion.revision_backfill import (
    LegacyBackfillError,
    LegacyBackfillRequest,
    backfill_legacy_projection,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("dry-run", "apply"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--actor-id", required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--receipt", type=Path)
    return parser


def _receipt(result: object) -> dict[str, object]:
    manifest = result.manifest
    years = sorted({snapshot.data_year for snapshot in manifest.snapshots})
    receipt: dict[str, object] = {
        "format_version": manifest.format_version,
        "manifest_sha256": manifest.manifest_sha256,
        "source_database_sha256": manifest.source_database_sha256,
        "source_database_size": manifest.source_database_size,
        "source_schema_sha256": manifest.source_schema_sha256,
        "source_user_version": manifest.source_user_version,
        "quick_check": manifest.quick_check,
        "foreign_key_violation_count": len(manifest.foreign_key_violations),
        "snapshot_count": len(manifest.snapshots),
        "first_year": years[0] if years else None,
        "last_year": years[-1] if years else None,
        "year_count": len(years),
        "table_counts": dict(manifest.table_counts),
        "dry_run": result.dry_run,
        "applied": result.applied,
        "no_op": result.no_op,
        "staged_revision_count": len(result.staged_revision_ids),
        "published_revision_count": len(result.published_revision_ids),
    }
    return receipt


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.mode == "apply" and not args.expected_manifest_sha256:
        raise SystemExit("apply requires --expected-manifest-sha256")
    if args.mode == "dry-run" and (args.publish or args.expected_manifest_sha256):
        raise SystemExit("dry-run does not accept apply-only options")

    request = LegacyBackfillRequest(
        source_db_path=args.source,
        destination_db_path=args.destination,
        workspace_id=args.workspace_id,
        actor_id=args.actor_id,
        profile_id=args.profile_id,
        request_id=args.request_id,
        dry_run=args.mode == "dry-run",
        publish=args.publish,
        in_place=False,
        expected_manifest_sha256=args.expected_manifest_sha256,
    )
    try:
        result = backfill_legacy_projection(request)
    except LegacyBackfillError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "message": str(exc)}))
        return 1

    receipt = _receipt(result)
    receipt_text = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    if args.receipt is not None:
        destination = args.receipt.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(receipt_text + "\n", encoding="utf-8")
    print(receipt_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
