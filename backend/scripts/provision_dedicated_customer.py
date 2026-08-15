"""Provision an empty dedicated BridgeGuard customer data plane."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.services.operations.provisioning import provision_dedicated_layout


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--customer-slug", required=True)
    parser.add_argument("--workspace-id", required=True)
    args = parser.parse_args()
    receipt = provision_dedicated_layout(
        args.data_root,
        customer_slug=args.customer_slug,
        workspace_id=args.workspace_id,
    )
    print(json.dumps(receipt.public_dict(), sort_keys=True))


if __name__ == "__main__":
    main()
