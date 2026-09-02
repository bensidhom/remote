"""Read-only analytical projection over exact yearly inventory snapshots.

The authoritative identity is ``(state_code, bridge_number, data_year)`` in
``snbi_bridges.db``.  Forecasting, LCCA, history, and KPI consumers therefore
read the same rows as the rest of the application and do not require a second
publication-pointer table.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable

from app.services.analytics.identity import AnalysisSnapshotIdentity
from app.services.analytics.life_history import (
    BridgeLifeHistory,
    EvidenceClass,
    LifeHistoryEvent,
)
from app.services.query.metrics import compute_controlling_condition


ANALYTICAL_PROJECTION_VERSION = "bridgeguard-analytics-projection-v1"
ANALYTICAL_SPECIFICATION_SHA256 = hashlib.sha256(
    ANALYTICAL_PROJECTION_VERSION.encode("ascii")
).hexdigest()


class AnalyticalProjectionError(RuntimeError):
    pass


class FeatureSelectionStatus(str, Enum):
    MISSING = "missing"
    SELECTED = "selected"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class CarriedFeature:
    feature_type: str
    feature_location: str | None
    nhs_designation: str | None
    aadt: int | None
    adtt: int | None
    traffic_year: int | None


@dataclass(frozen=True, slots=True)
class CarriedFeatureEvidence:
    status: FeatureSelectionStatus
    candidates: tuple[CarriedFeature, ...]
    selected: CarriedFeature | None


@dataclass(frozen=True, slots=True)
class SpanMaterialEvidence:
    designation: str | None
    span_material_code: str | None
    span_type_code: str | None
    deck_material_code: str | None
    wearing_surface_code: str | None


@dataclass(frozen=True, slots=True)
class SubstructureMaterialEvidence:
    designation: str | None
    material_code: str | None
    type_code: str | None


@dataclass(frozen=True, slots=True)
class ConditionEvidence:
    bc01: float | None
    bc02: float | None
    bc03: float | None
    bc04: float | None
    bc13: float | None
    controlling_rating: float | None
    condition_class: str
    basis: str
    conflict: bool
    conflict_reasons: tuple[str, ...]
    invalid_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BridgeAnalysisSnapshot:
    identity: AnalysisSnapshotIdentity
    snapshot_revision_id: str
    canonical_sha256: str
    published_at: str
    state_code: int
    bridge_number: str
    data_year: int
    bridge_name: str | None
    county_code: int | None
    owner_code: str | None
    maintenance_responsibility_code: str | None
    year_built: int | None
    age_at_snapshot: int | None
    total_bridge_length: float | None
    bridge_width_out_to_out: float | None
    calculated_deck_area: float | None
    root_nhs_designation: str | None
    condition: ConditionEvidence
    carried_features: CarriedFeatureEvidence
    span_materials: tuple[SpanMaterialEvidence, ...]
    substructure_materials: tuple[SubstructureMaterialEvidence, ...]


@dataclass(frozen=True, slots=True)
class BridgeAnalysisHistory:
    workspace_id: str
    state_code: int
    bridge_number: str
    snapshots: tuple[BridgeAnalysisSnapshot, ...]
    life_history: BridgeLifeHistory


def _readonly_connection(database_path: str | os.PathLike[str]) -> sqlite3.Connection:
    resolved = Path(database_path).resolve()
    if not resolved.is_file():
        raise AnalyticalProjectionError("analytical database is unavailable")
    try:
        connection = sqlite3.connect(
            f"file:{resolved.as_posix()}?mode=ro",
            uri=True,
            timeout=2.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        return connection
    except sqlite3.Error as exc:
        raise AnalyticalProjectionError("analytical database could not be opened") from exc


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _revision_set_sha256(revision_id: str, canonical_sha256: str) -> str:
    payload = json.dumps(
        [[revision_id, canonical_sha256]],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _loaded_snapshot_revision_id(state_code: int, bridge_number: str, year: int) -> str:
    return f"loaded-inventory:{int(state_code)}:{str(bridge_number)}:{int(year)}"


def _loaded_snapshot_sha256(row: sqlite3.Row) -> str:
    excluded = {"data_generation", "schema_generation"}
    payload = {key: row[key] for key in row.keys() if key not in excluded}
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _condition(row: sqlite3.Row) -> ConditionEvidence:
    result = compute_controlling_condition(
        bc01=row["deck_condition_rating"],
        bc02=row["superstructure_condition_rating"],
        bc03=row["substructure_condition_rating"],
        bc04=row["culvert_condition_rating"],
        bc13=row["lowest_condition_rating_code"],
    )
    return ConditionEvidence(
        bc01=_optional_float(row["deck_condition_rating"]),
        bc02=_optional_float(row["superstructure_condition_rating"]),
        bc03=_optional_float(row["substructure_condition_rating"]),
        bc04=_optional_float(row["culvert_condition_rating"]),
        bc13=_optional_float(row["lowest_condition_rating_code"]),
        controlling_rating=result.rating,
        condition_class=(result.condition_class.value if result.condition_class else "Unknown"),
        basis=result.basis.value,
        conflict=result.conflict,
        conflict_reasons=result.conflict_reasons,
        invalid_fields=result.invalid_fields,
    )


def _group_rows(rows: Iterable[sqlite3.Row]) -> dict[int, list[sqlite3.Row]]:
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(int(row["data_year"]), []).append(row)
    return grouped


def _load_children(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    state_code: int,
    bridge_number: str,
    through_year: int | None,
) -> tuple[
    dict[int, list[sqlite3.Row]],
    dict[int, list[sqlite3.Row]],
    dict[int, list[sqlite3.Row]],
    dict[int, list[sqlite3.Row]],
]:
    del workspace_id  # Workspace authorization is verified by the parent query.
    year_clause = " AND data_year<=?" if through_year is not None else ""
    common_params: tuple[object, ...] = (
        state_code,
        bridge_number,
        *((through_year,) if through_year is not None else ()),
    )
    features = connection.execute(
        "SELECT * FROM features WHERE state_code=? AND bridge_number=?"
        + year_clause
        + " AND feature_type LIKE 'H%' AND feature_location='C' "
        "ORDER BY data_year, feature_type, id",
        common_params,
    ).fetchall()
    spans = connection.execute(
        "SELECT * FROM span_sets WHERE state_code=? AND bridge_number=?"
        + year_clause
        + " ORDER BY data_year, span_configuration_designation, id",
        common_params,
    ).fetchall()
    substructures = connection.execute(
        "SELECT * FROM substructure_sets WHERE state_code=? AND bridge_number=?"
        + year_clause
        + " ORDER BY data_year, substructure_configuration_designation, id",
        common_params,
    ).fetchall()
    works = connection.execute(
        "SELECT *, 'loaded-inventory:' || CAST(state_code AS TEXT) || ':' || "
        "bridge_number || ':' || CAST(data_year AS TEXT) AS snapshot_revision_id "
        "FROM works WHERE state_code=? AND bridge_number=?"
        + year_clause
        + " ORDER BY data_year, year_work_performed, work_performed, id",
        common_params,
    ).fetchall()
    return (
        _group_rows(features),
        _group_rows(spans),
        _group_rows(substructures),
        _group_rows(works),
    )


def _features(rows: Iterable[sqlite3.Row]) -> CarriedFeatureEvidence:
    candidates = tuple(
        CarriedFeature(
            feature_type=str(row["feature_type"]),
            feature_location=_optional_text(row["feature_location"]),
            nhs_designation=_optional_text(row["nhs_designation"]),
            aadt=_optional_int(row["annual_average_daily_traffic"]),
            adtt=_optional_int(row["annual_average_daily_truck_traffic"]),
            traffic_year=_optional_int(row["year_of_annual_average_daily_traffic"]),
        )
        for row in rows
    )
    if not candidates:
        return CarriedFeatureEvidence(FeatureSelectionStatus.MISSING, (), None)
    if len(candidates) == 1:
        return CarriedFeatureEvidence(FeatureSelectionStatus.SELECTED, candidates, candidates[0])
    return CarriedFeatureEvidence(FeatureSelectionStatus.AMBIGUOUS, candidates, None)


def _work_events(works_by_year: dict[int, list[sqlite3.Row]]) -> tuple[LifeHistoryEvent, ...]:
    grouped: dict[tuple[int, str], set[str]] = {}
    for rows in works_by_year.values():
        for row in rows:
            work_year = _optional_int(row["year_work_performed"])
            work_code = _optional_text(row["work_performed"])
            if work_year is None or work_code is None:
                continue
            grouped.setdefault((work_year, work_code), set()).add(str(row["snapshot_revision_id"]))
    events: list[LifeHistoryEvent] = []
    for (work_year, work_code), revisions in sorted(grouped.items()):
        digest = hashlib.sha256(f"{work_year}:{work_code}".encode("utf-8")).hexdigest()[:20]
        events.append(
            LifeHistoryEvent(
                event_id=f"inventory-work:{digest}",
                event_year=work_year,
                component="bridge",
                treatment_id=work_code.lower().replace(" ", "_"),
                evidence_class=EvidenceClass.INVENTORY_REPORTED_WORK,
                source_record_ids=tuple(sorted(revisions)),
            )
        )
    return tuple(events)


def _history_events(
    snapshots: tuple[BridgeAnalysisSnapshot, ...],
    works_by_year: dict[int, list[sqlite3.Row]],
) -> tuple[LifeHistoryEvent, ...]:
    events: list[LifeHistoryEvent] = [
        LifeHistoryEvent(
            event_id=f"inventory-observation:{snapshot.snapshot_revision_id}",
            event_year=snapshot.data_year,
            component="bridge",
            treatment_id=None,
            evidence_class=EvidenceClass.INVENTORY_OBSERVATION,
            source_record_ids=(snapshot.snapshot_revision_id,),
        )
        for snapshot in snapshots
    ]
    events.extend(_work_events(works_by_year))
    components = (
        ("deck", "bc01"),
        ("superstructure", "bc02"),
        ("substructure", "bc03"),
        ("culvert", "bc04"),
    )
    for previous, current in zip(snapshots, snapshots[1:]):
        for component, attribute in components:
            before = getattr(previous.condition, attribute)
            after = getattr(current.condition, attribute)
            if before is None or after is None or after <= before:
                continue
            events.append(
                LifeHistoryEvent(
                    event_id=(
                        f"condition-change:{previous.snapshot_revision_id}:"
                        f"{current.snapshot_revision_id}:{component}"
                    ),
                    event_year=current.data_year,
                    component=component,
                    treatment_id=None,
                    evidence_class=EvidenceClass.CONDITION_CHANGE_CANDIDATE,
                    source_record_ids=(
                        previous.snapshot_revision_id,
                        current.snapshot_revision_id,
                    ),
                    before_rating=int(before),
                    after_rating=int(after),
                )
            )
    return tuple(events)


def load_bridge_analysis_history(
    *,
    database_path: str | os.PathLike[str],
    workspace_id: str,
    state_code: int,
    bridge_number: str,
    through_year: int | None = None,
) -> BridgeAnalysisHistory:
    """Load one bridge's exact annual inventory history without mutating SQLite."""

    workspace = str(workspace_id).strip()
    bridge = str(bridge_number).strip()
    if not workspace or not bridge:
        raise AnalyticalProjectionError("workspace and bridge identity are required")
    state = int(state_code)
    cutoff = int(through_year) if through_year is not None else None
    connection = _readonly_connection(database_path)
    try:
        year_clause = " AND b.data_year<=?" if cutoff is not None else ""
        params: tuple[object, ...] = (
            workspace,
            state,
            bridge,
            *((cutoff,) if cutoff is not None else ()),
        )
        rows = connection.execute(
            "SELECT ws.data_generation, ws.schema_generation, b.* "
            "FROM workspace_state ws CROSS JOIN bridges b "
            "WHERE ws.workspace_id=? AND b.state_code=? AND b.bridge_number=?"
            + year_clause
            + " ORDER BY b.data_year",
            params,
        ).fetchall()
        if not rows:
            raise AnalyticalProjectionError("loaded bridge history was not found")
        features_by_year, spans_by_year, subs_by_year, works_by_year = _load_children(
            connection,
            workspace_id=workspace,
            state_code=state,
            bridge_number=bridge,
            through_year=cutoff,
        )
    except AnalyticalProjectionError:
        raise
    except sqlite3.Error as exc:
        raise AnalyticalProjectionError("analytical projection could not be read") from exc
    finally:
        connection.close()

    snapshots: list[BridgeAnalysisSnapshot] = []
    for row in rows:
        year = int(row["data_year"])
        revision_id = _loaded_snapshot_revision_id(state, bridge, year)
        canonical_sha = _loaded_snapshot_sha256(row)
        year_built = _optional_int(row["year_built"])
        snapshots.append(
            BridgeAnalysisSnapshot(
                identity=AnalysisSnapshotIdentity(
                    workspace_id=workspace,
                    state_code=state,
                    data_year=year,
                    revision_set_sha256=_revision_set_sha256(revision_id, canonical_sha),
                    data_generation=int(row["data_generation"]),
                    schema_generation=int(row["schema_generation"]),
                    specification_sha256=ANALYTICAL_SPECIFICATION_SHA256,
                ),
                snapshot_revision_id=revision_id,
                canonical_sha256=canonical_sha,
                published_at="loaded-inventory",
                state_code=state,
                bridge_number=bridge,
                data_year=year,
                bridge_name=_optional_text(row["bridge_name"]),
                county_code=_optional_int(row["county_code"]),
                owner_code=_optional_text(row["owner"]),
                maintenance_responsibility_code=_optional_text(
                    row["maintenance_responsibility"]
                ),
                year_built=year_built,
                age_at_snapshot=(year - year_built if year_built is not None and year >= year_built else None),
                total_bridge_length=_optional_float(row["total_bridge_length"]),
                bridge_width_out_to_out=_optional_float(row["bridge_width_out_to_out"]),
                calculated_deck_area=_optional_float(row["calculated_deck_area"]),
                root_nhs_designation=_optional_text(row["nhs_designation"]),
                condition=_condition(row),
                carried_features=_features(features_by_year.get(year, ())),
                span_materials=tuple(
                    SpanMaterialEvidence(
                        designation=_optional_text(item["span_configuration_designation"]),
                        span_material_code=_optional_text(item["span_material"]),
                        span_type_code=_optional_text(item["span_type"]),
                        deck_material_code=_optional_text(item["deck_material_and_type"]),
                        wearing_surface_code=_optional_text(item["wearing_surface"]),
                    )
                    for item in spans_by_year.get(year, ())
                ),
                substructure_materials=tuple(
                    SubstructureMaterialEvidence(
                        designation=_optional_text(item["substructure_configuration_designation"]),
                        material_code=_optional_text(item["substructure_material"]),
                        type_code=_optional_text(item["substructure_type"]),
                    )
                    for item in subs_by_year.get(year, ())
                ),
            )
        )
    immutable_snapshots = tuple(snapshots)
    construction_candidates = tuple(
        snapshot.year_built for snapshot in immutable_snapshots if snapshot.year_built is not None
    )
    construction_year = min(construction_candidates) if construction_candidates else immutable_snapshots[0].data_year
    life_history = BridgeLifeHistory.build(
        bridge_id=f"{state}:{bridge}",
        construction_year=construction_year,
        latest_observed_year=immutable_snapshots[-1].data_year,
        events=_history_events(immutable_snapshots, works_by_year),
    )
    return BridgeAnalysisHistory(
        workspace_id=workspace,
        state_code=state,
        bridge_number=bridge,
        snapshots=immutable_snapshots,
        life_history=life_history,
    )


def list_loaded_bridge_keys(
    *,
    database_path: str | os.PathLike[str],
    workspace_id: str,
    state_code: int,
    through_year: int | None = None,
    minimum_observation_years: int = 2,
    limit: int = 50_000,
) -> tuple[str, ...]:
    """List bounded bridge IDs that have enough exact inventory observations."""

    workspace = str(workspace_id).strip()
    state = int(state_code)
    cutoff = int(through_year) if through_year is not None else None
    minimum = int(minimum_observation_years)
    maximum = int(limit)
    if not workspace or minimum < 1 or maximum < 1 or maximum > 50_000:
        raise AnalyticalProjectionError("loaded bridge-key request is invalid")
    connection = _readonly_connection(database_path)
    try:
        year_clause = " AND data_year<=?" if cutoff is not None else ""
        parameters: tuple[object, ...] = (
            state,
            *((cutoff,) if cutoff is not None else ()),
            minimum,
            maximum + 1,
        )
        workspace_exists = connection.execute(
            "SELECT 1 FROM workspace_state WHERE workspace_id=?", (workspace,)
        ).fetchone()
        if workspace_exists is None:
            raise AnalyticalProjectionError("workspace state was not found")
        rows = connection.execute(
            "SELECT bridge_number,COUNT(DISTINCT data_year) AS observation_years "
            "FROM bridges WHERE state_code=?"
            + year_clause
            + " GROUP BY bridge_number HAVING COUNT(DISTINCT data_year)>=? "
            "ORDER BY bridge_number LIMIT ?",
            parameters,
        ).fetchall()
    except sqlite3.Error as exc:
        raise AnalyticalProjectionError("loaded bridge keys could not be read") from exc
    finally:
        connection.close()
    if len(rows) > maximum:
        raise AnalyticalProjectionError("loaded forecast population exceeds the configured bound")
    return tuple(str(row["bridge_number"]) for row in rows)


def load_state_analysis_histories(
    *,
    database_path: str | os.PathLike[str],
    workspace_id: str,
    state_code: int,
    through_year: int | None = None,
    minimum_observation_years: int = 2,
    limit: int = 50_000,
) -> tuple[BridgeAnalysisHistory, ...]:
    """Materialize a deterministic bounded training population.

    Each bridge is still resolved through ``load_bridge_analysis_history`` so
    every child row and revision uses the same audited projection rules.
    """

    keys = list_loaded_bridge_keys(
        database_path=database_path,
        workspace_id=workspace_id,
        state_code=state_code,
        through_year=through_year,
        minimum_observation_years=minimum_observation_years,
        limit=limit,
    )
    if not keys:
        raise AnalyticalProjectionError("no loaded forecast training population was found")
    return tuple(
        load_bridge_analysis_history(
            database_path=database_path,
            workspace_id=workspace_id,
            state_code=state_code,
            bridge_number=bridge_number,
            through_year=through_year,
        )
        for bridge_number in keys
    )
