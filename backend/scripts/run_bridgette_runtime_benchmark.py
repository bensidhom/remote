"""Run Bridgette's disposable semantic-query runtime release benchmark."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from app.services.query.runtime_evaluation import (
    DEFAULT_RUNTIME_FIXTURE_PATH,
    run_runtime_benchmark,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compile reviewed Bridgette plans and exercise scoped SQLite execution, "
            "answer evidence, hostile-query rejection, resource caps, cancellation, "
            "concurrency, and revision/generation identity in a disposable database. "
            "No external model or network provider is called."
        )
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=DEFAULT_RUNTIME_FIXTURE_PATH,
        help="hash-sealed runtime fixture (defaults to the checked-in v1 fixture)",
    )
    parser.add_argument(
        "--work-directory",
        type=Path,
        default=None,
        help=(
            "existing directory beneath which the disposable database is created; "
            "the temporary database is removed before exit"
        ),
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="pretty-print the versioned JSON receipt",
    )
    args = parser.parse_args(argv)
    receipt = run_runtime_benchmark(
        args.fixture,
        work_directory=args.work_directory,
    )
    print(
        json.dumps(
            receipt.public_dict(),
            allow_nan=False,
            sort_keys=True,
            indent=2 if args.pretty else None,
        )
    )
    return 0 if receipt.release_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

