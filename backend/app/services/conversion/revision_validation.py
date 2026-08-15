"""Server-owned validation evidence for canonical snapshot revisions.

This boundary deliberately separates *who supplied the bridge document* from
*who asserted its validation state*.  HTTP clients may supply canonical data
and source evidence, but only BridgeGuard creates the validation record stored
with an immutable revision.

The executable checks below are the repository's deterministic, locally
implemented QA rules.  They are not an FHWA/NBTIS acceptance result.  Until a
pinned official schema and complete executable rule inventory are available,
every result retains an explicit review warning and ``official_validation`` is
always false.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ...routers.services.snbi_validation import validate_snbi_payload
from .canonical_schema import normalize_canonical_document


REVISION_VALIDATOR_VERSION = "bridgeguard-revision-validation-v1"
RESEARCH_PROFILE = "research_historical_import"
NATIVE_SNBI_PROFILE = "native_snbi"

_TABLE_POINTERS = {
    "elements": "Elements",
    "features": "Features",
    "inspections": "Inspections",
    "posting_evaluations": "PostingEvaluations",
    "posting_statuses": "PostingStatuses",
    "span_sets": "SpanSets",
    "substructure_sets": "SubstructureSets",
    "works": "Works",
}


def _is_legacy_source(source_records: Sequence[Mapping[str, Any]]) -> bool | None:
    """Return an evidence-backed legacy flag, or ``None`` when it is unknown."""

    if not source_records:
        return None
    kinds = {
        str(record.get("record_kind") or record.get("source_role") or "")
        .strip()
        .lower()
        for record in source_records
    }
    if any(
        kind.startswith("nbi_")
        or kind in {"nbi", "legacy_nbi", "nbi_over_record", "nbi_under_record"}
        for kind in kinds
    ):
        return True
    if any(kind in {"native_snbi", "snbi", "native_json"} for kind in kinds):
        return False
    return None


def _pointer(issue: Mapping[str, Any]) -> str:
    tag = str(issue.get("tag") or "").strip()
    table = str(issue.get("table") or "").strip()
    row_index = issue.get("row_index")
    container = _TABLE_POINTERS.get(table)
    if container:
        path = f"/{container}"
        if isinstance(row_index, int) and not isinstance(row_index, bool):
            path += f"/{row_index}"
        if tag:
            path += f"/{tag}"
        return path
    return f"/{tag}" if tag else ""


def derive_server_validation(
    document: Mapping[str, Any],
    *,
    profile_id: str,
    data_year: int,
    source_records: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Create deterministic, non-official validation evidence for one revision."""

    if profile_id not in {RESEARCH_PROFILE, NATIVE_SNBI_PROFILE}:
        raise ValueError(f"unsupported revision validation profile: {profile_id}")

    normalization = normalize_canonical_document(document, profile_id=profile_id)
    canonical = normalization.normalized_document
    legacy = _is_legacy_source(source_records)
    legacy_flags = None if legacy is None else [legacy]
    local = validate_snbi_payload(
        [canonical],
        submission_year=int(data_year),
        legacy_flags=legacy_flags,
    )

    mapped_issues: list[dict[str, Any]] = []
    for raw in local.get("issues") or ():
        if not isinstance(raw, Mapping):
            continue
        local_type = str(raw.get("type") or "error").strip().lower()
        # Safety findings block application publication until resolved.  A
        # generic data-quality flag remains an editor-review warning.
        severity = {
            "critical": "critical",
            "error": "error",
            "safety": "error",
            "flag": "warning",
        }.get(local_type, "error")
        mapped_issues.append(
            {
                "rule_id": str(raw.get("rule") or "BG-LOCAL-VALIDATION"),
                "severity": severity,
                "json_pointer": _pointer(raw),
                "message": str(raw.get("message") or "BridgeGuard validation issue"),
                "validation_source": "bridgeguard_local_rules",
                "local_issue_type": local_type,
            }
        )

    mapped_issues.append(
        {
            "rule_id": "BG-OFFICIAL-VALIDATION-UNAVAILABLE",
            "severity": "warning",
            "json_pointer": "",
            "message": (
                "Official FHWA/NBTIS validation is unavailable for this revision; "
                "authorized agency review and external validation remain required."
            ),
            "validation_source": "bridgeguard_validation_boundary",
        }
    )
    severity_counts = {
        level: sum(1 for issue in mapped_issues if issue["severity"] == level)
        for level in ("critical", "error", "warning", "info")
    }
    failed = severity_counts["critical"] > 0 or severity_counts["error"] > 0
    local_summary = dict(local.get("summary") or {})
    return {
        "profile_id": profile_id,
        "status": "failed" if failed else "passed_with_warnings",
        "validation_owner": "server",
        "validation_label": "BridgeGuard validation",
        "validator_version": REVISION_VALIDATOR_VERSION,
        "validation_scope": "bridgeguard_local_rules_and_integrity_only",
        "official_validation": False,
        "official_validation_status": "unavailable",
        "submission_ready": False,
        "disclaimer": (
            "BridgeGuard validation is not official FHWA or NBTIS validation. "
            "External validation and authorized agency review remain required."
        ),
        "summary": {
            **severity_counts,
            "total": len(mapped_issues),
            "local_critical": int(local_summary.get("critical") or 0),
            "local_error": int(local_summary.get("error") or 0),
            "local_flag": int(local_summary.get("flag") or 0),
            "local_safety": int(local_summary.get("safety") or 0),
            "legacy_bridges": int(local_summary.get("legacy_bridges") or 0),
        },
        "issues": mapped_issues,
    }


__all__ = [
    "RESEARCH_PROFILE",
    "NATIVE_SNBI_PROFILE",
    "REVISION_VALIDATOR_VERSION",
    "derive_server_validation",
]
