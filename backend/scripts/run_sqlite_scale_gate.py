"""Run BridgeGuard's disposable production SQLite release gate."""

from __future__ import annotations

import argparse
import json

from app.services.operations.sqlite_scale_gate import (
    DEFAULT_GATE_TIMEOUT_SECONDS,
    DEFAULT_WRITER_ITERATIONS,
    run_sqlite_scale_gate,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Exercise two writers, twenty readers, bounded busy behavior, WAL "
            "checkpoints, cancellation, and online-backup restore using a "
            "new disposable database. This command cannot target customer data."
        )
    )
    parser.add_argument(
        "--writer-iterations",
        type=int,
        default=DEFAULT_WRITER_ITERATIONS,
        help=f"transactions per writer (default: {DEFAULT_WRITER_ITERATIONS})",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_GATE_TIMEOUT_SECONDS,
        help=(
            "fail-closed total gate bound "
            f"(default: {DEFAULT_GATE_TIMEOUT_SECONDS:g})"
        ),
    )
    args = parser.parse_args()
    receipt = run_sqlite_scale_gate(
        writer_iterations=args.writer_iterations,
        gate_timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(receipt.public_dict(), sort_keys=True))


if __name__ == "__main__":
    main()
