"""KPI computations over exact yearly inventory snapshots."""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from threading import Event, Lock
from typing import Any, Iterable

from app.services.query.metrics import compute_controlling_condition

from .registry import (
    ACCEPTABLE_DEFINITION_VERSION,
    AGE_CONDITION_DEFINITION_VERSION,
    DEFAULT_KPI_REGISTRY,
    OPERATIONAL_DEFINITION_VERSION,
)


FUTURE_CONDITION_DEFINITION_VERSION = "bridgeguard-inventory-transition-v2"
_CONDITION_ORDER = ("Good", "Fair", "Poor")
_DETAILED_CONDITION_ORDER = ("Good", "Upper Fair", "Lower Fair", "Poor")
_SNAPSHOT_CACHE_SIZE = 4


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
    approach_width: float | None
    reported_area: float | None
    deck_condition_class: str | None
    superstructure_condition_class: str | None
    substructure_condition_class: str | None
    culvert_condition_class: str | None
    condition_rating: float | None
    condition_class: str
    condition_invalid: bool


_LoadResult = tuple[int, int, int, tuple[_Snapshot, ...], dict[str, object]]
_LoadCacheKey = tuple[str, str, int, int, int, int]
_snapshot_cache: OrderedDict[_LoadCacheKey, _LoadResult] = OrderedDict()
_snapshot_cache_inflight: dict[_LoadCacheKey, Event] = {}
_snapshot_cache_lock = Lock()


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


def _rounded_deck_area_product(length: float, width: float) -> float:
    """Match SQLite ROUND(..., 1) used by the governed BG16 query view."""

    return float(
        Decimal.from_float(length * width).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        )
    )


def _component_condition_class(value: object) -> str | None:
    rating = _number(value)
    if rating is None or not 0 <= rating <= 9:
        return None
    if rating >= 7:
        return "Good"
    if rating >= 5:
        return "Fair"
    return "Poor"


def _detailed_condition_class(value: object) -> str | None:
    """Split the official Fair class into its rating-6 and rating-5 bands."""

    rating = _number(value)
    if rating is None or not 0 <= rating <= 9:
        return None
    if rating >= 7:
        return "Good"
    if rating >= 6:
        return "Upper Fair"
    if rating >= 5:
        return "Lower Fair"
    return "Poor"


def _deck_area(snapshot: _Snapshot) -> tuple[float | None, str]:
    if snapshot.reported_area is not None and snapshot.reported_area > 0:
        return snapshot.reported_area, "reported_bg15"
    if snapshot.total_length is not None and snapshot.total_length > 0:
        if snapshot.width is not None and snapshot.width > 0:
            return _rounded_deck_area_product(
                snapshot.total_length, snapshot.width
            ), "derived_bg02_times_bg05"
        if snapshot.approach_width is not None and snapshot.approach_width > 0:
            return _rounded_deck_area_product(
                snapshot.total_length, snapshot.approach_width
            ), "derived_bg02_times_bg09"
    return None, "missing_or_invalid_geometry"


def _revision_set(rows: Iterable[_Snapshot]) -> str:
    payload = json.dumps(
        sorted((row.revision_id, row.canonical_sha256) for row in rows),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _loaded_identity(row: sqlite3.Row) -> tuple[str, str]:
    revision_id = (
        f"loaded-inventory:{int(row['state_code'])}:"
        f"{str(row['bridge_number'])}:{int(row['data_year'])}"
    )
    payload = {key: row[key] for key in row.keys()}
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    return revision_id, digest


def _load_cache_identity(
    database_path: str | os.PathLike[str],
    *,
    workspace_id: str,
    state_code: int,
    year: int | None,
) -> _LoadCacheKey:
    database = Path(database_path).resolve()
    connection = _readonly(database)
    try:
        selected = (
            connection.execute(
                "SELECT MAX(data_year) FROM bridges WHERE state_code=?",
                (int(state_code),),
            ).fetchone()[0]
            if year is None
            else int(year)
        )
        if selected is None:
            raise KpiComputationError("loaded KPI population was not found")
        generation = connection.execute(
            "SELECT data_generation,schema_generation FROM workspace_state WHERE workspace_id=?",
            (str(workspace_id).strip(),),
        ).fetchone()
        if generation is None:
            raise KpiComputationError("workspace state was not found")
    except KpiComputationError:
        raise
    except sqlite3.Error as exc:
        raise KpiComputationError("loaded KPI projection could not be read") from exc
    finally:
        connection.close()
    return (
        str(database),
        str(workspace_id).strip(),
        int(state_code),
        int(selected),
        int(generation[0]),
        int(generation[1]),
    )


def clear_kpi_snapshot_cache() -> None:
    """Clear the bounded in-process cache, primarily for tests and maintenance."""

    with _snapshot_cache_lock:
        _snapshot_cache.clear()


def _load_uncached(
    database_path: str | os.PathLike[str],
    *,
    workspace_id: str,
    state_code: int,
    year: int | None,
) -> _LoadResult:
    workspace = str(workspace_id).strip()
    state = int(state_code)
    if not workspace:
        raise KpiComputationError("workspace is required")
    connection = _readonly(database_path)
    try:
        if year is None:
            selected = connection.execute(
                "SELECT MAX(data_year) FROM bridges WHERE state_code=?",
                (state,),
            ).fetchone()[0]
            if selected is None:
                raise KpiComputationError("loaded KPI population was not found")
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
            "SELECT b.* FROM bridges b WHERE b.state_code=? AND b.data_year=? "
            "ORDER BY b.bridge_number",
            (state, selected_year),
        ).fetchall()
        if not rows:
            raise KpiComputationError("loaded KPI population was not found")
        county_names = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT county_code,county_name FROM ref_counties WHERE state_code=?",
                (state,),
            ).fetchall()
        }
        traffic_rows = connection.execute(
            "SELECT f.bridge_number,f.annual_average_daily_traffic "
            "FROM features f WHERE f.state_code=? AND f.data_year=? "
            "AND f.feature_type LIKE 'H%' AND f.feature_location='C' "
            "ORDER BY f.bridge_number,f.feature_type,f.id",
            (state, selected_year),
        ).fetchall()
    except KpiComputationError:
        raise
    except sqlite3.Error as exc:
        raise KpiComputationError("loaded KPI projection could not be read") from exc
    finally:
        connection.close()

    snapshots: list[_Snapshot] = []
    for row in rows:
        revision_id, canonical_sha256 = _loaded_identity(row)
        condition = compute_controlling_condition(
            bc01=row["deck_condition_rating"],
            bc02=row["superstructure_condition_rating"],
            bc03=row["substructure_condition_rating"],
            bc04=row["culvert_condition_rating"],
            bc13=row["lowest_condition_rating_code"],
        )
        snapshots.append(
            _Snapshot(
                revision_id=revision_id,
                canonical_sha256=canonical_sha256,
                bridge_number=str(row["bridge_number"]),
                county_code=_integer(row["county_code"]),
                maintenance_code=_text(row["maintenance_responsibility"]),
                year_built=_integer(row["year_built"]),
                nbis_length=_number(row["nbis_bridge_length"]),
                total_length=_number(row["total_bridge_length"]),
                width=_number(row["bridge_width_out_to_out"]),
                approach_width=(
                    _number(row["approach_roadway_width"])
                    if "approach_roadway_width" in row.keys()
                    else None
                ),
                reported_area=_number(row["calculated_deck_area"]),
                deck_condition_class=_component_condition_class(row["deck_condition_rating"]),
                superstructure_condition_class=_component_condition_class(row["superstructure_condition_rating"]),
                substructure_condition_class=_component_condition_class(row["substructure_condition_rating"]),
                culvert_condition_class=_component_condition_class(row["culvert_condition_rating"]),
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


def _load(
    database_path: str | os.PathLike[str],
    *,
    workspace_id: str,
    state_code: int,
    year: int | None,
) -> _LoadResult:
    """Load one immutable inventory projection per generation and share it.

    Concurrent KPI requests for the same year wait for the first load instead
    of scanning and interpreting the same bridge rows independently.
    """

    for _attempt in range(2):
        key = _load_cache_identity(
            database_path,
            workspace_id=workspace_id,
            state_code=state_code,
            year=year,
        )
        while True:
            with _snapshot_cache_lock:
                cached = _snapshot_cache.get(key)
                if cached is not None:
                    _snapshot_cache.move_to_end(key)
                    return cached
                pending = _snapshot_cache_inflight.get(key)
                if pending is None:
                    pending = Event()
                    _snapshot_cache_inflight[key] = pending
                    leader = True
                else:
                    leader = False
            if not leader:
                pending.wait(timeout=30.0)
                continue
            try:
                loaded = _load_uncached(
                    key[0],
                    workspace_id=key[1],
                    state_code=key[2],
                    year=key[3],
                )
                if loaded[:3] != (key[3], key[4], key[5]):
                    break
                with _snapshot_cache_lock:
                    _snapshot_cache[key] = loaded
                    _snapshot_cache.move_to_end(key)
                    while len(_snapshot_cache) > _SNAPSHOT_CACHE_SIZE:
                        _snapshot_cache.popitem(last=False)
                return loaded
            finally:
                with _snapshot_cache_lock:
                    completed = _snapshot_cache_inflight.pop(key, None)
                    if completed is not None:
                        completed.set()
    raise KpiComputationError("KPI data changed while the snapshot was loading")


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
    area_methods = {
        "reported_bg15": 0,
        "derived_bg02_times_bg05": 0,
        "derived_bg02_times_bg09": 0,
        "missing_or_invalid_geometry": 0,
    }
    aadt_complete = 0
    aadt_ambiguous = 0
    high_traffic = 0
    component_counts = {
        "Deck": {"field": "B.C.01", "Good": 0, "Fair": 0, "Poor": 0, "rated_bridges": 0},
        "Superstructure": {"field": "B.C.02", "Good": 0, "Fair": 0, "Poor": 0, "rated_bridges": 0},
        "Substructure": {"field": "B.C.03", "Good": 0, "Fair": 0, "Poor": 0, "rated_bridges": 0},
        "Culvert": {"field": "B.C.04", "Good": 0, "Fair": 0, "Poor": 0, "rated_bridges": 0},
    }
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
        for component, condition_class in (
            ("Deck", row.deck_condition_class),
            ("Superstructure", row.superstructure_condition_class),
            ("Substructure", row.substructure_condition_class),
            ("Culvert", row.culvert_condition_class),
        ):
            if condition_class is not None:
                component_counts[component][condition_class] += 1
                component_counts[component]["rated_bridges"] += 1

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
    state_area_revision_ids = {row.revision_id for row in state_area_population}
    state_area_rows = [
        (row, area)
        for row, area, _method in areas
        if row.revision_id in state_area_revision_ids
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
        "component_condition_counts": [
            {
                "component": component,
                **component_counts[component],
                "not_applicable_or_unrated": total - int(component_counts[component]["rated_bridges"]),
            }
            for component in ("Deck", "Superstructure", "Substructure", "Culvert")
        ],
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
        "kind": "observed_loaded_inventory",
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


def _age_for_inventory_cycle(selected_year: int, year_built: int | None) -> int | None:
    """Use the supplied chart convention: data year N represents CY N-1."""

    if year_built is None:
        return None
    age = selected_year - 1 - year_built
    return age if 0 <= age <= 300 else None


def _age_at_inventory_year(selected_year: int, year_built: int | None) -> int | None:
    """Return the comparison age measured directly against inventory year N."""

    if year_built is None:
        return None
    age = selected_year - year_built
    return age if 0 <= age <= 300 else None


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
    inventory_year_buckets: dict[str, dict[str, float]] = {
        key: {"Good": 0.0, "Upper Fair": 0.0, "Lower Fair": 0.0, "Poor": 0.0, "Unknown": 0.0}
        for key in _age_bin_order()
    }
    excluded = {
        "missing_or_invalid_age_count": 0,
        "missing_or_invalid_geometry_count": 0,
        "unknown_condition_area_ft2": 0.0,
    }
    for row in selected:
        reporting_age = _age_for_inventory_cycle(selected_year, row.year_built)
        inventory_age = _age_at_inventory_year(selected_year, row.year_built)
        if reporting_age is None:
            excluded["missing_or_invalid_age_count"] += 1
        if reporting_age is None and inventory_age is None:
            continue
        area, _method = _deck_area(row)
        if area is None:
            if reporting_age is not None:
                excluded["missing_or_invalid_geometry_count"] += 1
            continue
        rating = row.condition_rating
        if rating is None:
            condition = "Unknown"
            if reporting_age is not None:
                excluded["unknown_condition_area_ft2"] += area
        elif rating >= 7:
            condition = "Good"
        elif rating >= 6:
            condition = "Upper Fair"
        elif rating >= 5:
            condition = "Lower Fair"
        else:
            condition = "Poor"
        if reporting_age is not None:
            buckets[_age_bin(reporting_age)][condition] += area
        if inventory_age is not None:
            inventory_year_buckets[_age_bin(inventory_age)][condition] += area

    def area_series(source: dict[str, dict[str, float]]) -> list[dict[str, object]]:
        result = []
        for age_bin in _age_bin_order():
            values = source[age_bin]
            result.append(
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
        return result

    series = area_series(buckets)
    inventory_year_series = area_series(inventory_year_buckets)
    return {
        "kind": "observed_loaded_inventory",
        "state_code": int(state_code),
        "year": selected_year,
        "age_reference_year": selected_year - 1,
        "reporting_date": "June 15",
        "inventory_age_reference_year": selected_year,
        "population": population_key,
        "include_unknown": bool(include_unknown),
        "data_generation": data_generation,
        "schema_generation": schema_generation,
        "revision_set_sha256": _revision_set(rows),
        "definition_version": AGE_CONDITION_DEFINITION_VERSION,
        "measure_definition": DEFAULT_KPI_REGISTRY.get("deck_area_by_age_condition").as_dict(),
        "series": series,
        "inventory_year_series": inventory_year_series,
        "excluded": {key: _rounded(value) if isinstance(value, float) else value for key, value in excluded.items()},
    }


def compute_age_condition_counts(
    *,
    database_path: str | os.PathLike[str],
    workspace_id: str,
    state_code: int,
    year: int | None = None,
    population: str = "statewide",
) -> dict[str, object]:
    """Count bridges by prior-calendar-year age and controlling SNBI condition.

    This is intentionally a bridge-count chart.  It does not mix deck area
    into the y-axis, so values remain whole bridge records.
    """

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
    buckets: dict[str, dict[str, int]] = {
        key: {condition: 0 for condition in _DETAILED_CONDITION_ORDER}
        for key in _age_bin_order()
    }
    inventory_year_buckets: dict[str, dict[str, int]] = {
        key: {condition: 0 for condition in _DETAILED_CONDITION_ORDER}
        for key in _age_bin_order()
    }
    excluded = {
        "missing_or_invalid_age_count": 0,
        "missing_condition_count": 0,
    }
    for row in selected:
        reporting_age = _age_for_inventory_cycle(selected_year, row.year_built)
        inventory_age = _age_at_inventory_year(selected_year, row.year_built)
        if reporting_age is None:
            excluded["missing_or_invalid_age_count"] += 1
        if reporting_age is None and inventory_age is None:
            continue
        condition = _detailed_condition_class(row.condition_rating)
        if condition not in _DETAILED_CONDITION_ORDER:
            if reporting_age is not None:
                excluded["missing_condition_count"] += 1
            continue
        if reporting_age is not None:
            buckets[_age_bin(reporting_age)][str(condition)] += 1
        if inventory_age is not None:
            inventory_year_buckets[_age_bin(inventory_age)][str(condition)] += 1

    def count_series(source: dict[str, dict[str, int]]) -> list[dict[str, object]]:
        return [
            {
                "age_bin": age_bin,
                **source[age_bin],
                "Fair": source[age_bin]["Upper Fair"] + source[age_bin]["Lower Fair"],
                "total_bridges": sum(source[age_bin].values()),
            }
            for age_bin in _age_bin_order()
        ]

    series = count_series(buckets)
    inventory_year_series = count_series(inventory_year_buckets)
    return {
        "kind": "observed_loaded_inventory",
        "state_code": int(state_code),
        "year": selected_year,
        "age_reference_year": selected_year - 1,
        "reporting_date": "June 15",
        "inventory_age_reference_year": selected_year,
        "population": population_key,
        "data_generation": data_generation,
        "schema_generation": schema_generation,
        "revision_set_sha256": _revision_set(rows),
        "definition_version": AGE_CONDITION_DEFINITION_VERSION,
        "series": series,
        "inventory_year_series": inventory_year_series,
        "excluded": excluded,
    }


def _condition_from_database_row(row: sqlite3.Row) -> str | None:
    condition = compute_controlling_condition(
        bc01=row["deck_condition_rating"],
        bc02=row["superstructure_condition_rating"],
        bc03=row["substructure_condition_rating"],
        bc04=row["culvert_condition_rating"],
        bc13=row["lowest_condition_rating_code"],
    )
    return _detailed_condition_class(condition.rating)


def _transition_probabilities(
    counts: dict[str, dict[str, int]],
    *,
    allow_improvement: bool,
) -> dict[str, dict[str, float]]:
    level = {"Good": 3, "Upper Fair": 2, "Lower Fair": 1, "Poor": 0}
    result: dict[str, dict[str, float]] = {}
    for source in _DETAILED_CONDITION_ORDER:
        eligible = {
            target: int(counts[source][target])
            for target in _DETAILED_CONDITION_ORDER
            if allow_improvement or level[target] <= level[source]
        }
        total = sum(eligible.values())
        if total <= 0:
            result[source] = {
                target: (1.0 if target == source else 0.0)
                for target in _DETAILED_CONDITION_ORDER
            }
        else:
            result[source] = {
                target: (eligible.get(target, 0) / total)
                for target in _DETAILED_CONDITION_ORDER
            }
    return result


def _project_condition_counts(
    current: dict[str, float],
    matrix: dict[str, dict[str, float]],
    *,
    start_year: int,
    horizon_years: int,
) -> list[dict[str, object]]:
    distribution = {
        condition: float(current[condition])
        for condition in _DETAILED_CONDITION_ORDER
    }
    series: list[dict[str, object]] = []
    for offset in range(0, int(horizon_years) + 1):
        upper_fair = distribution["Upper Fair"]
        lower_fair = distribution["Lower Fair"]
        series.append(
            {
                "year": int(start_year) + offset,
                **{
                    condition: _rounded(distribution[condition])
                    for condition in _DETAILED_CONDITION_ORDER
                },
                "Fair": _rounded(upper_fair + lower_fair),
                "total_bridges": _rounded(sum(distribution.values())),
                "poor_bridge_count": _rounded(distribution["Poor"]),
            }
        )
        if offset == int(horizon_years):
            break
        distribution = {
            target: math.fsum(
                distribution[source] * matrix[source][target]
                for source in _DETAILED_CONDITION_ORDER
            )
            for target in _DETAILED_CONDITION_ORDER
        }
    return series


def _project_age_condition_counts(
    current_by_age: dict[int, dict[str, float]],
    matrix: dict[str, dict[str, float]],
    *,
    start_year: int,
    horizon_years: int,
) -> list[dict[str, object]]:
    """Age each starting cohort while applying the scenario transition matrix."""

    cohorts = {
        int(age): {
            condition: float(distribution[condition])
            for condition in _DETAILED_CONDITION_ORDER
        }
        for age, distribution in current_by_age.items()
    }
    result: list[dict[str, object]] = []
    for offset in range(0, int(horizon_years) + 1):
        buckets = {
            age_bin: {condition: 0.0 for condition in _DETAILED_CONDITION_ORDER}
            for age_bin in _age_bin_order()
        }
        for starting_age, distribution in cohorts.items():
            bucket = buckets[_age_bin(starting_age + offset)]
            for condition in _DETAILED_CONDITION_ORDER:
                bucket[condition] += distribution[condition]
        age_bins = []
        for age_bin in _age_bin_order():
            values = buckets[age_bin]
            age_bins.append(
                {
                    "age_bin": age_bin,
                    **{
                        condition: _rounded(values[condition])
                        for condition in _DETAILED_CONDITION_ORDER
                    },
                    "Fair": _rounded(values["Upper Fair"] + values["Lower Fair"]),
                    "total_bridges": _rounded(sum(values.values())),
                }
            )
        result.append(
            {
                "year": int(start_year) + offset,
                "rated_with_known_age": _rounded(
                    sum(float(row["total_bridges"] or 0.0) for row in age_bins)
                ),
                "age_bins": age_bins,
            }
        )
        if offset == int(horizon_years):
            break
        cohorts = {
            age: {
                target: math.fsum(
                    distribution[source] * matrix[source][target]
                    for source in _DETAILED_CONDITION_ORDER
                )
                for target in _DETAILED_CONDITION_ORDER
            }
            for age, distribution in cohorts.items()
        }
    return result


def compute_future_condition_scenarios(
    *,
    database_path: str | os.PathLike[str],
    workspace_id: str,
    state_code: int,
    year: int | None = None,
    horizon_years: int = 10,
) -> dict[str, object]:
    """Project aggregate condition counts from observed annual SNBI changes.

    The result is generated on demand.  It requires no saved forecast package
    or publication record: the selected year's rated bridges provide the
    starting population and consecutive yearly rows provide the transition
    evidence.
    """

    horizon = int(horizon_years)
    if not 1 <= horizon <= 30:
        raise KpiComputationError("horizon_years must be between 1 and 30")
    selected_year, data_generation, schema_generation, current_rows, _auxiliary = _load(
        database_path,
        workspace_id=workspace_id,
        state_code=state_code,
        year=year,
    )
    current_counts = {condition: 0.0 for condition in _DETAILED_CONDITION_ORDER}
    current_by_age: dict[int, dict[str, float]] = defaultdict(
        lambda: {condition: 0.0 for condition in _DETAILED_CONDITION_ORDER}
    )
    current_unrated = 0
    current_rated_missing_age = 0
    for row in current_rows:
        condition = _detailed_condition_class(row.condition_rating)
        if condition not in current_counts:
            current_unrated += 1
            continue
        current_counts[str(condition)] += 1.0
        if row.year_built is None or not 0 <= selected_year - row.year_built <= 300:
            current_rated_missing_age += 1
            continue
        current_by_age[selected_year - row.year_built][str(condition)] += 1.0

    connection = _readonly(database_path)
    try:
        history_rows = connection.execute(
            "SELECT bridge_number,data_year,deck_condition_rating,"
            "superstructure_condition_rating,substructure_condition_rating,"
            "culvert_condition_rating,lowest_condition_rating_code "
            "FROM bridges WHERE state_code=? AND data_year<=? "
            "ORDER BY bridge_number,data_year",
            (int(state_code), selected_year),
        ).fetchall()
    except sqlite3.Error as exc:
        raise KpiComputationError("loaded condition history could not be read") from exc
    finally:
        connection.close()

    transition_counts = {
        source: {target: 0 for target in _DETAILED_CONDITION_ORDER}
        for source in _DETAILED_CONDITION_ORDER
    }
    previous: dict[str, tuple[int, str | None]] = {}
    transition_intervals = 0
    for row in history_rows:
        bridge_number = str(row["bridge_number"])
        data_year = int(row["data_year"])
        condition = _condition_from_database_row(row)
        prior = previous.get(bridge_number)
        if (
            prior is not None
            and data_year == prior[0] + 1
            and prior[1] in _DETAILED_CONDITION_ORDER
            and condition in _DETAILED_CONDITION_ORDER
        ):
            transition_counts[str(prior[1])][str(condition)] += 1
            transition_intervals += 1
        previous[bridge_number] = (data_year, condition)
    if transition_intervals <= 0:
        raise KpiComputationError("consecutive yearly condition history was not found")

    historical_matrix = _transition_probabilities(transition_counts, allow_improvement=True)
    no_rehabilitation_matrix = _transition_probabilities(transition_counts, allow_improvement=False)
    return {
        "kind": "projected_loaded_inventory",
        "state_code": int(state_code),
        "year": selected_year,
        "horizon_years": horizon,
        "data_generation": data_generation,
        "schema_generation": schema_generation,
        "revision_set_sha256": _revision_set(current_rows),
        "definition_version": FUTURE_CONDITION_DEFINITION_VERSION,
        "source_transition_intervals": transition_intervals,
        "current_unrated_count": current_unrated,
        "current_rated_missing_or_invalid_age_count": current_rated_missing_age,
        "scenarios": [
            {
                "scenario_id": "historical_trend",
                "label": "Historical trend",
                "description": "Observed yearly changes, including improvements recorded after bridge work.",
                "series": _project_condition_counts(
                    current_counts,
                    historical_matrix,
                    start_year=selected_year,
                    horizon_years=horizon,
                ),
                "age_series": _project_age_condition_counts(
                    current_by_age,
                    historical_matrix,
                    start_year=selected_year,
                    horizon_years=horizon,
                ),
            },
            {
                "scenario_id": "no_rehabilitation",
                "label": "No rehabilitation",
                "description": "Uses the observed deterioration rates but does not allow condition improvements.",
                "series": _project_condition_counts(
                    current_counts,
                    no_rehabilitation_matrix,
                    start_year=selected_year,
                    horizon_years=horizon,
                ),
                "age_series": _project_age_condition_counts(
                    current_by_age,
                    no_rehabilitation_matrix,
                    start_year=selected_year,
                    horizon_years=horizon,
                ),
            },
        ],
    }
