"""Inventory or recoverably quarantine legacy point-cloud current pointers."""

from __future__ import annotations

import argparse
import json

from app.services.point_cloud.rollout import (
    inventory_legacy_processed_pointers,
    quarantine_legacy_processed_pointers,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-inventory-sha256")
    args = parser.parse_args()
    if args.apply:
        if not args.expected_inventory_sha256:
            parser.error("--expected-inventory-sha256 is required with --apply")
        result = quarantine_legacy_processed_pointers(
            args.data_root,
            args.database,
            args.workspace_id,
            expected_inventory_sha256=args.expected_inventory_sha256,
        ).to_dict()
    else:
        result = inventory_legacy_processed_pointers(
            args.data_root, args.database, args.workspace_id
        ).to_dict()
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
