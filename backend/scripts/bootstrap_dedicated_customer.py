"""One-shot database bootstrap for a provisioned dedicated customer root."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.services.operations.deployment_database import (
    bootstrap_dedicated_database,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Initialize, migrate, and immutably bind an empty marker-backed "
            "dedicated-customer database. Existing unbound data is rejected."
        )
    )
    parser.add_argument("--data-root", type=Path, required=True)
    args = parser.parse_args()
    receipt = bootstrap_dedicated_database(args.data_root)
    print(json.dumps(receipt.public_dict(), sort_keys=True))


if __name__ == "__main__":
    main()
