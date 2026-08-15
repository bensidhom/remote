"""Era-specific FHWA TPM bridge-condition diagnostic.

This implementation is intentionally labeled diagnostic.  It records exact
population and exclusion decisions and does not claim to reproduce FHWA's
prepared dataset or official submission result.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

from app.services.query.metrics import compute_controlling_condition


class FhwaTpmDiagnosticError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FhwaTpmProfile:
    profile_id: str
    first_year: int
    last_year: int | None
    area_rule: str
    source_title: str
    source_url: str

    def applies(self, year: int) -> bool:
        return year >= self.first_year and (self.last_year is None or year <= self.last_year)

    def as_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "first_year": self.first_year,
            "last_year": self.last_year,
            "area_rule": self.area_rule,
            "source_title": self.source_title,
            "source_url": self.source_url,
        }


PROFILES = (
    FhwaTpmProfile(
        "fhwa-hif-18-023-coding-guide-v1",
        1900,
        2025,
        "positive total bridge length multiplied by positive bridge width out-to-out",
        "FHWA-HIF-18-023 — Computation Procedure for the Bridge Condition Measures",
        "https://www.fhwa.dot.gov/tpm/guidance/hif18023.pdf",
    ),
    FhwaTpmProfile(
        "fhwa-hif-25-010-transition-v1",
        2026,
        2027,
        "BG02 multiplied by positive BG05, otherwise BG02 multiplied by positive BG09",
        "FHWA-HIF-25-010 — TPM Bridge Condition Measures",
        "https://www.fhwa.dot.gov/tpm/guidance/hif25010.pdf",
    ),
    FhwaTpmProfile(
        "fhwa-hif-25-010-snbi-v1",
        2028,
        None,
        "positive BG15 irregular deck area, otherwise BG02 multiplied by positive BG05",
        "FHWA-HIF-25-010 — TPM Bridge Condition Measures",
        "https://www.fhwa.dot.gov/tpm/guidance/hif25010.pdf",
    ),
)


def _profile(year: int) -> FhwaTpmProfile:
    for candidate in PROFILES:
        if candidate.applies(year):
            return candidate
    raise FhwaTpmDiagnosticError("no FHWA TPM profile is available for the selected year")


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _positive(value: Any) -> Decimal | None:
    parsed = _decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


def _area(row: sqlite3.Row, profile: FhwaTpmProfile) -> tuple[Decimal | None, str]:
    length = _positive(row["total_bridge_length"])
    width = _positive(row["bridge_width_out_to_out"])
    if profile.profile_id.endswith("coding-guide-v1"):
        if length is None or width is None:
            return None, "missing_total_length_or_deck_width"
        raw = length * width
        return raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP), "bg02_times_bg05"
    if profile.profile_id.endswith("transition-v1"):
        selected_width = width or _positive(row["approach_roadway_width"])
        if length is None or selected_width is None:
            return None, "missing_bg02_or_bg05_bg09"
        method = "bg02_times_bg05" if width is not None else "bg02_times_bg09"
        return (length * selected_width).quantize(Decimal("1"), rounding=ROUND_HALF_UP), method
    irregular = _positive(row["irregular_deck_area"])
    if irregular is not None:
        return irregular.quantize(Decimal("1"), rounding=ROUND_HALF_UP), "reported_bg15"
    if length is None or width is None:
        return None, "missing_bg15_or_bg02_bg05"
    return (length * width).quantize(Decimal("1"), rounding=ROUND_HALF_UP), "bg02_times_bg05"


def _percentage(numerator: Decimal, denominator: Decimal) -> str | None:
    if denominator <= 0:
        return None
    return format(
        (numerator * Decimal("100") / denominator).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        ),
        "f",
    )


def compute_fhwa_tpm_diagnostic(
    *,
    database_path: str | os.PathLike[str],
    workspace_id: str,
    state_code: int,
    year: int,
) -> dict[str, object]:
    selected_year = int(year)
    profile = _profile(selected_year)
    database = Path(database_path).resolve()
    if not database.is_file():
        raise FhwaTpmDiagnosticError("analytical database is unavailable")
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    try:
        generation = connection.execute(
            "SELECT data_generation,schema_generation FROM workspace_state WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()
        rows = connection.execute(
            "SELECT ps.snapshot_revision_id,sr.canonical_sha256,b.* "
            "FROM published_snapshots ps JOIN snapshot_revisions sr "
            "ON sr.workspace_id=ps.workspace_id AND sr.id=ps.snapshot_revision_id "
            "AND sr.state_code=ps.state_code AND sr.bridge_number=ps.bridge_number AND sr.data_year=ps.data_year "
            "JOIN bridges b ON b.state_code=ps.state_code AND b.bridge_number=ps.bridge_number AND b.data_year=ps.data_year "
            "WHERE ps.workspace_id=? AND ps.state_code=? AND ps.data_year=? ORDER BY ps.bridge_number",
            (workspace_id, int(state_code), selected_year),
        ).fetchall()
        feature_rows = connection.execute(
            "SELECT f.bridge_number,f.feature_type,f.feature_location,f.nhs_designation "
            "FROM published_snapshots ps JOIN features f ON f.state_code=ps.state_code "
            "AND f.bridge_number=ps.bridge_number AND f.data_year=ps.data_year "
            "WHERE ps.workspace_id=? AND ps.state_code=? AND ps.data_year=? "
            "AND f.feature_type LIKE 'H%' ORDER BY f.bridge_number,f.feature_type,f.id",
            (workspace_id, int(state_code), selected_year),
        ).fetchall()
    except sqlite3.Error as exc:
        raise FhwaTpmDiagnosticError("FHWA TPM diagnostic projection could not be read") from exc
    finally:
        connection.close()
    if generation is None or not rows:
        raise FhwaTpmDiagnosticError("published diagnostic population was not found")

    features: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for feature in feature_rows:
        features[str(feature["bridge_number"])].append(feature)
    exclusions: Counter[str] = Counter()
    area_methods: Counter[str] = Counter()
    class_areas = {"Good": Decimal("0"), "Fair": Decimal("0"), "Poor": Decimal("0")}
    class_counts = {"Good": 0, "Fair": 0, "Poor": 0}
    included: list[tuple[str, str]] = []
    qualifying_feature_count = 0
    ramp_feature_count = 0
    border_bridge_count = 0

    for row in rows:
        bridge_number = str(row["bridge_number"])
        nbis_length = _positive(row["nbis_bridge_length"])
        if nbis_length is None or nbis_length <= Decimal("20"):
            exclusions["not_nbis_length_over_20ft"] += 1
            continue
        qualifying = [
            feature for feature in features.get(bridge_number, ())
            if str(feature["feature_location"] or "").upper() == "C"
            and str(feature["nhs_designation"] or "").upper() == "Y"
        ]
        if not qualifying:
            exclusions["no_qualifying_carried_nhs_highway_feature"] += 1
            continue
        qualifying_feature_count += len(qualifying)
        ramp_feature_count += sum(
            1 for feature in qualifying if str(feature["feature_type"] or "").upper() != "H01"
        )
        border_number = str(row["border_bridge_number"] or "").strip().upper()
        if border_number and border_number != "N":
            border_bridge_count += 1
            lead_state = str(row["border_bridge_designated_lead_state"] or "").strip()
            if not lead_state:
                exclusions["border_lead_state_missing"] += 1
                continue
            if lead_state not in {str(int(state_code)), str(int(state_code)).zfill(2)}:
                exclusions["border_other_lead_state"] += 1
                continue
        condition = compute_controlling_condition(
            bc01=row["deck_condition_rating"],
            bc02=row["superstructure_condition_rating"],
            bc03=row["substructure_condition_rating"],
            bc04=row["culvert_condition_rating"],
            bc13=row["lowest_condition_rating_code"],
        )
        if condition.rating is None:
            exclusions["condition_missing_or_invalid"] += 1
            continue
        deck_area, method = _area(row, profile)
        if deck_area is None or deck_area <= 0:
            exclusions[method] += 1
            continue
        category = "Good" if condition.rating >= 7 else "Fair" if condition.rating >= 5 else "Poor"
        class_areas[category] += deck_area
        class_counts[category] += 1
        area_methods[method] += 1
        included.append((str(row["snapshot_revision_id"]), str(row["canonical_sha256"])))

    total_area = sum(class_areas.values(), Decimal("0"))
    revision_payload = json.dumps(
        sorted((str(row["snapshot_revision_id"]), str(row["canonical_sha256"])) for row in rows),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "kind": "fhwa_tpm_diagnostic",
        "diagnostic_only": True,
        "official_validation": False,
        "submission_ready": False,
        "label": "FHWA TPM Diagnostic — not an FHWA submission",
        "workspace_id": workspace_id,
        "state_code": int(state_code),
        "year": selected_year,
        "data_generation": int(generation[0]),
        "schema_generation": int(generation[1]),
        "revision_set_sha256": hashlib.sha256(revision_payload).hexdigest(),
        "definition_version": profile.profile_id,
        "profile": profile.as_dict(),
        "population": {
            "published_bridge_count": len(rows),
            "included_bridge_count": len(included),
            "qualifying_feature_count": qualifying_feature_count,
            "qualifying_ramp_feature_count": ramp_feature_count,
            "border_bridge_candidate_count": border_bridge_count,
        },
        "areas_ft2": {key.lower(): format(value, "f") for key, value in class_areas.items()},
        "bridge_counts": {key.lower(): value for key, value in class_counts.items()},
        "total_applicable_deck_area_ft2": format(total_area, "f"),
        "good_deck_area_percent": _percentage(class_areas["Good"], total_area),
        "poor_deck_area_percent": _percentage(class_areas["Poor"], total_area),
        "fair_deck_area_percent": _percentage(class_areas["Fair"], total_area),
        "area_method_counts": dict(sorted(area_methods.items())),
        "exclusions": dict(sorted(exclusions.items())),
        "warnings": [
            "This diagnostic has not been independently validated against FHWA prepared data.",
            "It must not be represented as an official FHWA computation or submission.",
        ],
    }


__all__ = ["PROFILES", "FhwaTpmDiagnosticError", "FhwaTpmProfile", "compute_fhwa_tpm_diagnostic"]
