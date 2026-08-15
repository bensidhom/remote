"""Multi-alternative annual LCCA driven exclusively by one forecast package."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Iterable, Mapping

from app.services.analytics.projection import BridgeAnalysisHistory
from app.services.analytics.treatments import (
    DEFAULT_BRIDGE_TREATMENT_LIBRARY,
    TreatmentApplicability,
    TreatmentDefinition,
    TreatmentLibrary,
)
from app.services.conversion.canonical_json import canonical_json_text
from app.services.forecasting_v2.estimator import (
    BridgeAnnualForecastProvider,
    TreatmentEffectModel,
)
from app.services.forecasting_v2.models import ComponentState, ForecastQualification
from app.services.forecasting_v2.store import ForecastStoreError, deserialize_annual_markov_model
from app.services.lcca_v2.economics import EconomicAssumptions
from app.services.lcca_v2.facts import BridgeLccaFacts, bridge_lcca_facts
from app.services.lcca_v2.simulation import (
    AnnualLccaRequest,
    AnnualLccaResult,
    LccaAlternative,
    ScheduledAction,
    SimulationInputError,
    simulate_annual_lcca,
)


SUPPORTED_STRATEGIES = (
    "no_action",
    "njdot_preventive_cycles",
    "condition_triggered_rehabilitation",
    "deck_and_substructure_rehabilitation",
    "full_replacement",
    "explicit_schedule",
)


@dataclass(frozen=True, slots=True)
class ExplicitAction:
    year: int
    treatment_id: str


@dataclass(frozen=True, slots=True)
class StrategyResult:
    strategy_id: str
    result: AnnualLccaResult
    scheduled_actions: tuple[ScheduledAction, ...]
    applicability_issues: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class BridgeLccaComparison:
    scenario_receipt_sha256: str
    facts: BridgeLccaFacts
    model_version_id: str
    model_id: str
    evaluation_run_id: str
    evaluation_receipt_id: str
    forecast_qualification: ForecastQualification
    treatment_library_id: str
    treatment_library_sha256: str
    effect_evidence: str
    alternatives: tuple[StrategyResult, ...]


def _connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    return connection


def _latest_package(path: str | Path, workspace_id: str) -> sqlite3.Row:
    connection = _connect(path)
    try:
        row = connection.execute(
            "SELECT m.id AS model_version_id,m.model_id,m.model_json,m.qualification,"
            "e.id AS evaluation_run_id,e.evaluation_receipt_id,e.release_qualified "
            "FROM forecast_model_versions m JOIN forecast_evaluation_runs e "
            "ON e.workspace_id=m.workspace_id AND e.forecast_model_version_id=m.id "
            "WHERE m.workspace_id=? ORDER BY e.created_at DESC,e.id DESC LIMIT 1",
            (workspace_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ForecastStoreError("forecast_package_not_found", "No evaluated forecast package is available", status=404)
    return row


def _component_states(history: BridgeAnalysisHistory) -> tuple[ComponentState, ...]:
    snapshot = history.snapshots[-1]
    fields = {
        "deck": snapshot.condition.bc01,
        "superstructure": snapshot.condition.bc02,
        "substructure": snapshot.condition.bc03,
        "culvert": snapshot.condition.bc04,
    }
    return tuple(
        ComponentState.certain(component, int(round(value)))
        for component, value in fields.items()
        if value is not None and math.isfinite(value) and 0 <= value <= 9
    )


def _material_for(facts: BridgeLccaFacts, component: str) -> str | None:
    values = (
        facts.deck_material_codes
        if component == "deck"
        else facts.span_material_codes
        if component == "superstructure"
        else facts.substructure_material_codes
        if component == "substructure"
        else ()
    )
    return values[0] if len(values) == 1 else None


def _rating_for(facts: BridgeLccaFacts, component: str) -> int | None:
    raw = dict(facts.component_ratings).get(component)
    return int(round(raw)) if raw is not None and 0 <= raw <= 9 else None


def _applicability(
    treatment: TreatmentDefinition,
    *,
    facts: BridgeLccaFacts,
    available_components: set[str],
    concurrent_work: bool,
) -> tuple[bool, dict[str, object] | None]:
    if treatment.component not in available_components and treatment.component not in {
        "bridge", "bearing", "parapet"
    }:
        return False, {
            "treatment_id": treatment.treatment_id,
            "status": "not_evaluable",
            "reasons": ["component_rating"],
        }
    result = treatment.evaluate(
        component=treatment.component,
        material_code=_material_for(facts, treatment.component),
        condition_rating=(
            _rating_for(facts, treatment.component)
            if treatment.component in available_components
            else facts.controlling_rating and int(round(facts.controlling_rating))
        ),
        element_evidence_available=False,
        concurrent_work=concurrent_work,
    )
    if result.status is TreatmentApplicability.APPLICABLE:
        return True, None
    return False, {
        "treatment_id": treatment.treatment_id,
        "status": result.status.value,
        "reasons": list(result.reasons),
    }


def _scheduled(
    year: int,
    action_id: str,
    costs: Mapping[str, Decimal],
    library: TreatmentLibrary,
) -> ScheduledAction:
    definition = library.get(action_id)
    return ScheduledAction(
        year=year,
        treatment_id=action_id,
        present_cost=costs.get(action_id, Decimal("0")),
        quantity=Decimal("1"),
        cost_unit=definition.cost_unit,
    )


def _preventive_schedule(
    *,
    facts: BridgeLccaFacts,
    start_year: int,
    horizon_years: int,
    costs: Mapping[str, Decimal],
    library: TreatmentLibrary,
    available_components: set[str],
) -> tuple[tuple[ScheduledAction, ...], tuple[dict[str, object], ...]]:
    selected: list[ScheduledAction] = []
    issues: list[dict[str, object]] = []
    for treatment in library.treatments:
        if treatment.cycle_years is None or treatment.treatment_id == "bearing_lubrication":
            continue
        applicable, issue = _applicability(
            treatment,
            facts=facts,
            available_components=available_components,
            concurrent_work=False,
        )
        if not applicable:
            if issue:
                issues.append(issue)
            continue
        for year in range(start_year + treatment.cycle_years, start_year + horizon_years + 1, treatment.cycle_years):
            selected.append(_scheduled(year, treatment.treatment_id, costs, library))
    bearing = library.get("bearing_lubrication")
    for year in range(start_year + 4, start_year + horizon_years + 1, 4):
        if any(item.year == year for item in selected):
            selected.append(_scheduled(year, bearing.treatment_id, costs, library))
    return tuple(sorted(selected, key=lambda item: (item.year, item.treatment_id))), tuple(issues)


def _effect_models(
    *,
    actions: Iterable[str],
    components: Iterable[str],
    library: TreatmentLibrary,
    enabled: bool,
) -> tuple[TreatmentEffectModel, ...]:
    action_ids = set(actions)
    if not action_ids:
        return ()
    if not enabled:
        raise SimulationInputError(
            "selected strategies require treatment-effect evidence; enable the explicit research sensitivity or supply reviewed effects"
        )
    effects: list[TreatmentEffectModel] = []
    component_set = set(components)
    for action_id in sorted(action_ids):
        definition = library.get(action_id)
        if action_id == "full_replacement":
            affected = component_set
            gain = 9
        else:
            affected = {definition.component} & component_set
            gain = (
                9
                if action_id == "deck_replacement"
                else 2
                if "rehabilitation" in action_id
                else 0
            )
        for component in sorted(affected):
            effects.append(
                TreatmentEffectModel.deterministic_gain(
                    treatment_id=action_id,
                    component=component,
                    gain=gain,
                    evidence_class="user_enabled_research_sensitivity",
                )
            )
    return tuple(effects)


def _condition_schedule(
    *,
    provider: BridgeAnnualForecastProvider,
    states: tuple[ComponentState, ...],
    start_year: int,
    horizon_years: int,
    threshold: float,
    costs: Mapping[str, Decimal],
    library: TreatmentLibrary,
) -> tuple[ScheduledAction, ...]:
    action_for = {
        "deck": "deck_replacement",
        "superstructure": "superstructure_rehabilitation",
        "substructure": "substructure_rehabilitation",
    }
    current = {state.component: state for state in states}
    last_action: dict[str, int] = {}
    schedule: list[ScheduledAction] = []
    for year in range(start_year + 1, start_year + horizon_years + 1):
        for component in sorted(current):
            current[component] = provider.transition(
                year=year, component=component, state=current[component], action_id=None
            ).state
        candidates = sorted(
            (
                component
                for component in action_for
                if component in current and current[component].expected_rating <= threshold
            ),
            key=lambda component: (current[component].expected_rating, component),
        )
        for component in candidates:
            action_id = action_for[component]
            definition = library.get(action_id)
            if year - last_action.get(component, -10_000) < definition.minimum_spacing_years:
                continue
            current[component] = provider.transition(
                year=year,
                component=component,
                state=current[component],
                action_id=action_id,
            ).state
            last_action[component] = year
            schedule.append(_scheduled(year, action_id, costs, library))
    return tuple(schedule)


def compare_bridge_lcca_strategies(
    database_path: str | Path,
    *,
    history: BridgeAnalysisHistory,
    strategies: Iterable[str],
    horizon_years: int,
    economics: EconomicAssumptions,
    action_costs: Mapping[str, Decimal],
    explicit_actions: Iterable[ExplicitAction] = (),
    condition_threshold: float = 4.5,
    enable_research_effect_assumptions: bool = False,
    require_released_forecast: bool = False,
    treatments: TreatmentLibrary = DEFAULT_BRIDGE_TREATMENT_LIBRARY,
) -> BridgeLccaComparison:
    strategy_ids = tuple(dict.fromkeys(str(item).strip().lower() for item in strategies))
    if not strategy_ids or any(item not in SUPPORTED_STRATEGIES for item in strategy_ids):
        raise SimulationInputError("one or more LCCA strategies are unsupported")
    horizon = int(horizon_years)
    if horizon < 1 or horizon > 100:
        raise SimulationInputError("LCCA horizon must be 1 through 100 years")
    snapshot = history.snapshots[-1]
    if economics.base_year != snapshot.data_year:
        raise SimulationInputError("economic base year must match the exact as-of snapshot")
    states = _component_states(history)
    if not states:
        raise SimulationInputError("no valid component states are available")
    facts = bridge_lcca_facts(snapshot)
    package = _latest_package(database_path, history.workspace_id)
    qualification = ForecastQualification(str(package["qualification"]))
    if require_released_forecast and qualification is not ForecastQualification.RELEASED:
        raise SimulationInputError("forecast package is not released for commercial LCCA")
    model = deserialize_annual_markov_model(str(package["model_json"]))

    schedules: dict[str, tuple[ScheduledAction, ...]] = {}
    issues: dict[str, tuple[dict[str, object], ...]] = {}
    components = {state.component for state in states}
    schedules["no_action"] = ()
    issues["no_action"] = ()
    if "njdot_preventive_cycles" in strategy_ids:
        schedules["njdot_preventive_cycles"], issues["njdot_preventive_cycles"] = _preventive_schedule(
            facts=facts,
            start_year=snapshot.data_year,
            horizon_years=horizon,
            costs=action_costs,
            library=treatments,
            available_components=components,
        )
    if "deck_and_substructure_rehabilitation" in strategy_ids:
        actions = tuple(
            _scheduled(snapshot.data_year, action_id, action_costs, treatments)
            for action_id in ("deck_replacement", "substructure_rehabilitation")
            if treatments.get(action_id).component in components
        )
        schedules["deck_and_substructure_rehabilitation"] = actions
        issues["deck_and_substructure_rehabilitation"] = ()
    if "full_replacement" in strategy_ids:
        schedules["full_replacement"] = (
            _scheduled(snapshot.data_year, "full_replacement", action_costs, treatments),
        )
        issues["full_replacement"] = ()
    if "explicit_schedule" in strategy_ids:
        schedules["explicit_schedule"] = tuple(
            _scheduled(int(item.year), item.treatment_id, action_costs, treatments)
            for item in explicit_actions
        )
        issues["explicit_schedule"] = ()

    preliminary_actions = {
        action.treatment_id for schedule in schedules.values() for action in schedule
    }
    if "condition_triggered_rehabilitation" in strategy_ids:
        preliminary_actions.update(
            {"deck_replacement", "superstructure_rehabilitation", "substructure_rehabilitation"}
        )
    effects = _effect_models(
        actions=preliminary_actions,
        components=components,
        library=treatments,
        enabled=enable_research_effect_assumptions,
    )
    provider = BridgeAnnualForecastProvider(
        model=model,
        bridge_snapshot=snapshot,
        evaluation_receipt_id=str(package["evaluation_receipt_id"]),
        qualification=qualification,
        treatment_effects=effects,
    )
    if "condition_triggered_rehabilitation" in strategy_ids:
        schedules["condition_triggered_rehabilitation"] = _condition_schedule(
            provider=provider,
            states=states,
            start_year=snapshot.data_year,
            horizon_years=horizon,
            threshold=float(condition_threshold),
            costs=action_costs,
            library=treatments,
        )
        issues["condition_triggered_rehabilitation"] = ()

    results: list[StrategyResult] = []
    for strategy_id in strategy_ids:
        schedule = schedules[strategy_id]
        result = simulate_annual_lcca(
            request=AnnualLccaRequest(
                identity=snapshot.identity,
                bridge_id=f"{snapshot.state_code}:{snapshot.bridge_number}",
                start_year=snapshot.data_year,
                horizon_years=horizon,
                discount_rate=economics.real_discount_rate,
                forecast_model_id=model.model_id,
                forecast_evaluation_receipt_id=str(package["evaluation_receipt_id"]),
                initial_states=states,
                alternative=LccaAlternative(
                    alternative_id=strategy_id,
                    scheduled_actions=schedule,
                ),
                require_released_forecast=require_released_forecast,
                economic_assumptions=economics,
            ),
            forecast=provider,
            treatments=treatments,
        )
        results.append(
            StrategyResult(
                strategy_id=strategy_id,
                result=result,
                scheduled_actions=schedule,
                applicability_issues=issues.get(strategy_id, ()),
            )
        )
    receipt_payload = {
        "snapshot_revision_id": snapshot.snapshot_revision_id,
        "canonical_sha256": snapshot.canonical_sha256,
        "model_id": model.model_id,
        "evaluation_receipt_id": str(package["evaluation_receipt_id"]),
        "treatment_library_sha256": treatments.library_sha256,
        "strategies": [
            {
                "strategy_id": item.strategy_id,
                "schedule": [
                    {
                        "year": action.year,
                        "treatment_id": action.treatment_id,
                        "present_cost": str(action.present_cost),
                    }
                    for action in item.scheduled_actions
                ],
                "npv": str(item.result.total_present_value_cost),
                "eac": str(item.result.equivalent_annual_cost),
            }
            for item in results
        ],
        "economics": {
            "mode": economics.mode.value,
            "base_year": economics.base_year,
            "real_discount_rate": str(economics.real_discount_rate),
            "nominal_discount_rate": str(economics.nominal_discount_rate) if economics.nominal_discount_rate is not None else None,
            "inflation_rate": str(economics.inflation_rate) if economics.inflation_rate is not None else None,
        },
        "effect_evidence": "user_enabled_research_sensitivity" if enable_research_effect_assumptions else "none",
    }
    receipt = hashlib.sha256(canonical_json_text(receipt_payload).encode("utf-8")).hexdigest()
    return BridgeLccaComparison(
        scenario_receipt_sha256=receipt,
        facts=facts,
        model_version_id=str(package["model_version_id"]),
        model_id=model.model_id,
        evaluation_run_id=str(package["evaluation_run_id"]),
        evaluation_receipt_id=str(package["evaluation_receipt_id"]),
        forecast_qualification=qualification,
        treatment_library_id=treatments.library_id,
        treatment_library_sha256=treatments.library_sha256,
        effect_evidence=(
            "user_enabled_research_sensitivity"
            if enable_research_effect_assumptions
            else "none"
        ),
        alternatives=tuple(results),
    )
