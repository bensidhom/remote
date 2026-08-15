"""Validate a server dotenv file's commercial entitlement evidence.

Only blocker codes and the safe public evidence shape are emitted.  Operator
receipt identifiers and dotenv values are never printed.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

from app.commercial_evidence import evaluate_commercial_entitlement_evidence


def _dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print("commercial entitlement evidence failed: evidence_file_required", file=sys.stderr)
        return 2
    try:
        values = _dotenv(Path(arguments[0]))
    except (OSError, UnicodeError):
        print("commercial entitlement evidence failed: evidence_file_unreadable", file=sys.stderr)
        return 2
    evidence = evaluate_commercial_entitlement_evidence(values)
    if not evidence.approved:
        print(
            "commercial entitlement evidence failed: " + ",".join(evidence.blockers),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(evidence.public_dict(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
