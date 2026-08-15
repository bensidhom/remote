"""Exact published-revision KPI computations."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable

from app.services.query.metrics import compute_controlling_condition

from .registry import (
    ACCEPTABLE_DEFINITION_VERSION,
    DEFAULT_KPI_REGISTRY,
    OPERATIONAL_DEFINITION_VERSION,
)


class KpiComputationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _Snapshot:
    revision_id: str
    canonical_sha256: str
    bridge_number: str
    county_code: int | None
    maintenance_code: str | None
    year_built: int | None
    nbis_length: float | None
    total_length: float | None
    width: float | None
    reported_area: float | None
    condition_rating: float | None
    condition_class: str
    condition_invalid: bool


def _readonly(path: str | os.PathLike[str]) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise KpiComputationError("analytical database is unavailable")
    connection = sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro", uri=True, timeout=2.0
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    return connection


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number.is_integer() else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _pct(numerator: float | int, denominator: float | int) -> float | None:
    if denominator <= 0:
        return None
    value = Decimal(str(numerator)) * Decimal("100") / Decimal(str(denominator))
    return float(value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _rounded(value: float | None, quantum: str = "0.1") -> float | None:
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal(quantum), rounding=ROUND_HALF_UP))


def _deck_area(snapshot: _Snapshot) -> tuple[float | None, str]:
    if snapshot.reported_area is not None and snapshot.reported_area > 0:
        return snapshot.reported_area, "reported_bg15"
    if (
        snapshot.total_length is not None
        and snapshot.total_length > 0
        and snapshot.width is not None
        and snapshot.width > 0
    ):
        return snapshot.total_length * snapshot.width, "derived_bg02_times_bg05"
    return None, "missing_or_invalid_geometry"


def _revision_set(rows: Iterable[_Snapshot]) -> str:
    payload = json.dumps(
        sorted((row.revision_id, row.canonical_sha256) for row in rows),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load(
    database_path: str | os.PathLike[str],
    *,
    workspace_id: str,
    state_code: int,
    year: int | None,
) -> tuple[int, int, int, tuple[_Snapshot, ...], dict[str, object]]:
    workspace = str(workspace_id).strip()
    state = int(state_code)
    if not workspace:
        raise KpiComputationError("workspace is required")
    connection = _readonly(database_path)
    try:
        if year is None:
            selected = connection.execute(
                "SELECT MAX(data_year) FROM published_snapshots "
                "WHERE workspace_id=? AND state_code=?",
                (workspace, state),
            ).fetchone()[0]
            if selected is None:
                raise KpiComputationError("published KPI population was not found")
            selected_year = int(selected)
        else:
            selected_year = int(year)
        generation = connection.execute(
            "SELECT data_generation,schema_generation FROM workspace_state WHERE workspace_id=?",
            (workspace,),
        ).fetchone()
        if generation is None:
            raise KpiComputationError("workspace state was not found")
        rows = connection.execute(
            "SELECT ps.snapshot_revision_id,sr.canonical_sha256,b.* "
            "FROM published_snapshots ps "
            "JOIN snapshot_revisions sr ON sr.workspace_id=ps.workspace_id "
            "AND sr.id=ps.snapshot_revision_id AND sr.state_code=ps.state_code "
            "AND sr.bridge_number=ps.bridge_number AND sr.data_year=ps.data_year "
            "JOIN bridges b ON b.state_code=ps.state_code "
            "AND b.bridge_number=ps.bridge_number AND b.data_year=ps.data_year "
            "WHERE ps.workspace_id=? AND ps.state_code=? AND ps.data_year=? "
            "ORDER BY ps.bridge_number,ps.snapshot_revision_id",
            (workspace, state, selected_year),
        ).fetchall()
        if not rows:
            raise KpiComputationError("published KPI population was not found")
        county_names = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT county_code,county_name FROM ref_counties WHERE state_code=?",
                (state,),
            ).fetchall()
        }
        traffic_rows = connection.execute(
            "SELECT f.bridge_number,f.annual_average_daily_traffic "
            "FROM published_snapshots ps JOIN features f "
            "ON f.state_code=ps.state_code AND f.bridge_number=ps.bridge_number "
            "AND f.data_year=ps.data_year "
            "WHERE ps.workspace_id=? AND ps.state_code=? AND ps.data_year=? "
            "AND f.feature_type LIKE 'H%' AND f.feature_location='C' "
            "ORDER BY f.bridge_number,f.feature_type,f.id",
            (workspace, state, selected_year),
        ).fetchall()
    except KpiComputationError:
        raise
    except sqlite3.Error as exc:
        raise KpiComputationError("published KPI projection could not be read") from exc
    finally:
        connection.close()

    snapshots: list[_Snapshot] = []
    for row in rows:
        condition = compute_controlling_condition(
            bc01=row["deck_condition_rating"],
            bc02=row["superstructure_condition_rating"],
            bc03=row["substructure_condition_rating"],
            bc04=row["culvert_condition_rating"],
            bc13=row["lowest_condition_rating_code"],
        )
        snapshots.append(
            _Snapshot(
                revision_id=str(row["snapshot_revision_id"]),
                canonical_sha256=str(row["canonical_sha256"]).lower(),
                bridge_number=str(row["bridge_number"]),
                county_code=_integer(row["county_code"]),
                maintenance_code=_text(row["maintenance_responsibility"]),
                year_built=_integer(row["year_built"]),
                nbis_length=_number(row["nbis_bridge_length"]),
                total_length=_number(row["total_bridge_length"]),
                width=_number(row["bridge_width_out_to_out"]),
                reported_area=_number(row["calculated_deck_area"]),
                condition_rating=condition.rating,
                condition_class=(condition.condition_class.value if condition.condition_class else "Unknown"),
                condition_invalid=bool(condition.invalid_fields),
            )
        )
    traffic_values: dict[str, list[float]] = defaultdict(list)
    for row in traffic_rows:
        value = _number(row["annual_average_daily_traffic"])
        if value is not None:
            traffic_values[str(row["bridge_number"])].append(value)
    return (
        selected_year,
        int(generation[0]),
        int(generation[1]),
        tuple(snapshots),
        {
            **{f"county:{key}": value for key, value in county_names.items()},
            **{f"traffic:{key}": tuple(values) for key, values in traffic_values.items()},
        },
    )


def compute_operational_kpis(
    *,
    database_path: str | os.PathLike[str],
    workspace_id: str,
    state_code: int,
    year: int | None = None,
    older_than_years: int = 45,
    high_traffic_threshold: int = 25_000,
) -> dict[str, object]:
    selected_year, data_generation, schema_generation, rows, auxiliary = _load(
        database_path,
        workspace_id=workspace_id,
        state_code=state_code,
        year=year,
    )
    counts = {"Good": 0, "Fair": 0, "Poor": 0, "Unknown": 0}
    county_counts: dict[str, int] = defaultdict(int)
    county_condition_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"Good": 0, "Fair": 0, "Poor": 0, "Unknown": 0}
    )
    ages: list[int] = []
    older = 0
    areas: list[tuple[_Snapshot, float, str]] = []
    area_methods = {"reported_bg15": 0, "derived_bg02_times_bg05": 0, "missing_or_invalid_geometry": 0}
    aadt_complete = 0
    aadt_ambiguous = 0
    high_traffic = 0
    for row in rows:
        counts[row.condition_class] += 1
        county_name = auxiliary.get(f"county:{row.county_code}") if row.county_code is not None else None
        county_counts[str(county_name or "Unknown county")] += 1
        county_condition_counts[str(county_name or "Unknown county")][row.condition_class] += 1
        if row.year_built is not None and 0 <= selected_year - row.year_built <= 300:
            age = selected_year - row.year_built
            ages.append(age)
            older += int(age >= int(older_than_years))
        area, method = _deck_area(row)
        area_methods[method] += 1
        if area is not None:
            areas.append((row, area, method))
        candidates = tuple(auxiliary.get(f"traffic:{row.bridge_number}", ()))
        aadt_complete += int(len(candidates) == 1)
        aadt_ambiguous += int(len(candidates) > 1)
        high_traffic += int(len(candidates) == 1 and candidates[0] >= int(high_traffic_threshold))

    classifiable = counts["Good"] + counts["Fair"] + counts["Poor"]
    total = len(rows)
    state_rows = [
        row
        for row in rows
        if (row.maintenance_code or "").upper() == "S01"
    ]
    length_eligible = [
        row for row in state_rows if row.nbis_length is not None and row.nbis_length >= 20.0
    ]
    length_missing = len(state_rows) - len(length_eligible)
    length_classifiable = [row for row in length_eligible if row.condition_rating is not None]
    length_acceptable = [row for row in length_classifiable if row.condition_rating >= 5]
    length_unknown = len(length_eligible) - len(length_classifiable)

    state_area_population = length_eligible
    state_area_rows = [
        (row, area)
        for row, area, _method in areas
        if row in state_area_population
    ]
    state_area_missing = len(state_area_population) - len(state_area_rows)
    classifiable_area = sum(area for row, area in state_area_rows if row.condition_rating is not None)
    acceptable_area = sum(area for row, area in state_area_rows if row.condition_rating is not None and row.condition_rating >= 5)
    unknown_condition_area = sum(area for row, area in state_area_rows if row.condition_rating is None)

    values = {
        "total_bridges": total,
        "condition_counts": counts,
        "condition_percent_of_all": {key: _pct(value, total) for key, value in counts.items()},
        "condition_percent_of_classifiable": {
            key: (_pct(value, classifiable) if key != "Unknown" else None)
            for key, value in counts.items()
        },
        "classifiable_bridges": classifiable,
        "average_age_at_snapshot": _rounded(sum(ages) / len(ages)) if ages else None,
        "age_known_count": len(ages),
        "age_missing_or_invalid_count": total - len(ages),
        "older_than_years": int(older_than_years),
        "older_bridge_count": older,
        "total_valid_deck_area_ft2": _rounded(sum(area for _row, area, _method in areas)),
        "deck_area_known_count": len(areas),
        "deck_area_missing_count": area_methods["missing_or_invalid_geometry"],
        "deck_area_method_counts": area_methods,
        "carried_aadt_single_candidate_count": aadt_complete,
        "carried_aadt_ambiguous_count": aadt_ambiguous,
        "carried_aadt_missing_count": total - aadt_complete - aadt_ambiguous,
        "high_traffic_threshold": int(high_traffic_threshold),
        "high_traffic_bridge_count": high_traffic,
        "county_counts": dict(sorted(county_counts.items())),
        "county_condition_counts": [
            {"county": county, **county_condition_counts[county]}
            for county in sorted(county_condition_counts)
        ],
        "state_maintained_policy": {
            "field": "BCL02",
            "allowed_codes": ["S01"],
            "meaning": "State Transportation Department",
            "status": "research_candidate_definition_pending_agency_approval",
        },
        "state_maintained_20ft_acceptable": {
            "all_state_maintained_count": len(state_rows),
            "eligible_count": len(length_eligible),
            "classifiable_denominator": len(length_classifiable),
            "acceptable_numerator": len(length_acceptable),
            "unknown_condition_count": length_unknown,
            "missing_or_below_length_count": length_missing,
            "percent_classifiable": _pct(len(length_acceptable), len(length_classifiable)),
            "percent_all_eligible": _pct(len(length_acceptable), len(length_eligible)),
            "definition_version": ACCEPTABLE_DEFINITION_VERSION,
        },
        "state_maintained_deck_area_acceptable": {
            "valid_classifiable_area_denominator_ft2": _rounded(classifiable_area),
            "acceptable_area_numerator_ft2": _rounded(acceptable_area),
            "unknown_condition_area_ft2": _rounded(unknown_condition_area),
            "missing_geometry_bridge_count": state_area_missing,
            "below_or_missing_nbis_length_count": length_missing,
            "percent_classifiable_area": _pct(acceptable_area, classifiable_area),
            "percent_all_valid_area": _pct(acceptable_area, classifiable_area + unknown_condition_area),
            "definition_version": ACCEPTABLE_DEFINITION_VERSION,
        },
    }
    return {
        "kind": "observed_published_inventory",
        "state_code": int(state_code),
        "year": selected_year,
        "data_generation": data_generation,
        "schema_generation": schema_generation,
        "revision_set_sha256": _revision_set(rows),
        "definition_version": OPERATIONAL_DEFINITION_VERSION,
        "registry": DEFAULT_KPI_REGISTRY.as_dict(),
        "values": values,
        "warnings": [
            "NJDOT-style state-maintained measures are candidate operational definitions pending agency approval.",
            "These are observed inventory KPIs, not FHWA TPM measures and not forecast results.",
        ],
    }


def _age_bin(age: int) -> str:
    if age > 100:
        return ">100"
    if age >= 95:
        return "95-100"
    start = (age // 5) * 5
    return f"{start}-{start + 4}"


def _age_bin_order() -> tuple[str, ...]:
    return tuple([f"{start}-{start + 4}" for start in range(0, 95, 5)] + ["95-100", ">100"])


def compute_age_condition_area(
    *,
    database_path: str | os.PathLike[str],
    workspace_id: str,
    state_code: int,
    year: int | None = None,
    population: str = "statewide",
    include_unknown: bool = True,
) -> dict[str, object]:
    selected_year, data_generation, schema_generation, rows, _auxiliary = _load(
        database_path,
        workspace_id=workspace_id,
        state_code=state_code,
        year=year,
    )
    population_key = str(population).strip().lower()
    if population_key not in {"statewide", "state_maintained"}:
        raise KpiComputationError("population must be statewide or state_maintained")
    selected = (
        rows
        if population_key == "statewide"
        else tuple(row for row in rows if (row.maintenance_code or "").upper() == "S01")
    )
    buckets: dict[str, dict[str, float]] = {
        key: {"Good": 0.0, "Upper Fair": 0.0, "Lower Fair": 0.0, "Poor": 0.0, "Unknown": 0.0}
        for key in _age_bin_order()
    }
    excluded = {
        "missing_or_invalid_age_count": 0,
        "missing_or_invalid_geometry_count": 0,
        "unknown_condition_area_ft2": 0.0,
    }
    for row in selected:
        if row.year_built is None or not 0 <= selected_year - row.year_built <= 300:
            excluded["missing_or_invalid_age_count"] += 1
            continue
        area, _method = _deck_area(row)
        if area is None:
            excluded["missing_or_invalid_geometry_count"] += 1
            continue
        rating = row.condition_rating
        if rating is None:
            condition = "Unknown"
            excluded["unknown_condition_area_ft2"] += area
        elif rating >= 7:
            condition = "Good"
        elif rating >= 6:
            condition = "Upper Fair"
        elif rating >= 5:
            condition = "Lower Fair"
        else:
            condition = "Poor"
        buckets[_age_bin(selected_year - row.year_built)][condition] += area
    series = []
    for age_bin in _age_bin_order():
        values = buckets[age_bin]
        series.append(
            {
                "age_bin": age_bin,
                **{
                    key: _rounded(value)
                    for key, value in values.items()
                    if include_unknown or key != "Unknown"
                },
                "total_ft2": _rounded(sum(values.values())),
            }
        )
    return {
        "kind": "observed_published_inventory",
        "state_code": int(state_code),
        "year": selected_year,
        "population": population_key,
        "include_unknown": bool(include_unknown),
        "data_generation": data_generation,
        "schema_generation": schema_generation,
        "revision_set_sha256": _revision_set(rows),
        "definition_version": OPERATIONAL_DEFINITION_VERSION,
        "measure_definition": DEFAULT_KPI_REGISTRY.get("deck_area_by_age_condition").as_dict(),
        "series": series,
        "excluded": {key: _rounded(value) if isinstance(value, float) else value for key, value in excluded.items()},
    }
