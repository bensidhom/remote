"""Multi-alternative annual LCCA over the exact loaded bridge history."""

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
    ForecastModelConfig,
    TreatmentEffectModel,
    build_annual_markov_model,
)
from app.services.forecasting_v2.models import ComponentState, ForecastQualification
from app.services.forecasting_v2.store import ForecastStoreError, deserialize_annual_markov_model
from app.services.lcca_v2.economics import EconomicAssumptions
from app.services.lcca_v2.costs import (
    DEFAULT_NJDOT_PLANNING_COSTS,
    default_total_action_costs,
)
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
    "reactive_management",
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
    feasibility_status: str
    feasibility_reasons: tuple[str, ...]
    rankable: bool
    cost_complete: bool
    minimum_controlling_rating: float | None
    meets_service_threshold: bool
    sensitivity: tuple[dict[str, object], ...]


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
    analysis_period_status: str
    cost_scope: str
    cost_library_id: str
    cost_library_version: str
    resolved_action_costs: Mapping[str, Decimal]
    rehabilitation_trigger: float
    minimum_service_rating: float
    alternatives: tuple[StrategyResult, ...]


_SERVICE_LIFE_YEARS = {
    "njdot_preservation_package": 10,
    "deck_replacement": 40,
    "superstructure_rehabilitation": 25,
    "substructure_rehabilitation": 25,
    "full_replacement": 75,
}
_GENERIC_PRESERVATION_INTERVAL_YEARS = 10
_DEFAULT_PRESERVATION_DETERIORATION_REDUCTION = 0.20


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
    *,
    provided_cost_ids: set[str],
    included_in: str | None = None,
) -> ScheduledAction:
    definition = library.get(action_id)
    if included_in:
        cost = Decimal("0")
        cost_status = "included"
        cost_source = f"included in {included_in} cost model"
    elif action_id in costs:
        cost = costs[action_id]
        cost_status = "provided" if action_id in provided_cost_ids else "default"
        cost_source = (
            "user override"
            if cost_status == "provided"
            else DEFAULT_NJDOT_PLANNING_COSTS.get(action_id).source_reference
        )
    else:
        cost = Decimal("0")
        cost_status = "unavailable"
        cost_source = "no reviewed cost was available"
    return ScheduledAction(
        year=year,
        treatment_id=action_id,
        present_cost=cost,
        quantity=Decimal("1"),
        cost_unit=definition.cost_unit,
        cost_status=cost_status,
        cost_source=cost_source,
        included_in=included_in,
        service_life_years=_SERVICE_LIFE_YEARS.get(action_id),
    )


def _preventive_schedule(
    *,
    facts: BridgeLccaFacts,
    start_year: int,
    horizon_years: int,
    costs: Mapping[str, Decimal],
    provided_cost_ids: set[str],
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
            selected.append(
                _scheduled(
                    year,
                    treatment.treatment_id,
                    costs,
                    library,
                    provided_cost_ids=provided_cost_ids,
                    included_in="njdot_preservation_package",
                )
            )
    bearing = library.get("bearing_lubrication")
    for year in range(start_year + 4, start_year + horizon_years + 1, 4):
        if any(item.year == year for item in selected):
            selected.append(
                _scheduled(
                    year,
                    bearing.treatment_id,
                    costs,
                    library,
                    provided_cost_ids=provided_cost_ids,
                    included_in="njdot_preservation_package",
                )
            )
    if selected:
        for year in range(
            start_year + _GENERIC_PRESERVATION_INTERVAL_YEARS,
            start_year + horizon_years + 1,
            _GENERIC_PRESERVATION_INTERVAL_YEARS,
        ):
            selected.append(
                _scheduled(
                    year,
                    "njdot_preservation_package",
                    costs,
                    library,
                    provided_cost_ids=provided_cost_ids,
                )
            )
    return tuple(sorted(selected, key=lambda item: (item.year, item.treatment_id))), tuple(issues)


def _effect_models(
    *,
    actions: Iterable[str],
    components: Iterable[str],
    library: TreatmentLibrary,
    enabled: bool,
    preservation_deterioration_reduction: float = _DEFAULT_PRESERVATION_DETERIORATION_REDUCTION,
) -> tuple[TreatmentEffectModel, ...]:
    action_ids = set(actions)
    if not action_ids:
        return ()
    if not enabled:
        raise SimulationInputError(
            "selected strategies require treatment-effect assumptions; enable them or supply reviewed effects"
        )
    effects: list[TreatmentEffectModel] = []
    component_set = set(components)
    for action_id in sorted(action_ids):
        definition = library.get(action_id)
        if (
            definition.cycle_years is not None
            and definition.component in component_set
        ):
            effects.append(
                TreatmentEffectModel.protective(
                    treatment_id=action_id,
                    component=definition.component,
                    duration_years=definition.cycle_years,
                    deterioration_reduction=preservation_deterioration_reduction,
                    evidence_class="configurable_preservation_assumption",
                )
            )
            continue
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
                    evidence_class="default_effect_assumption",
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
    provided_cost_ids: set[str],
    library: TreatmentLibrary,
    baseline_actions: tuple[ScheduledAction, ...] = (),
) -> tuple[ScheduledAction, ...]:
    action_for = {
        "deck": "deck_replacement",
        "superstructure": "superstructure_rehabilitation",
        "substructure": "substructure_rehabilitation",
    }
    current = {state.component: state for state in states}
    last_action: dict[str, int] = {}
    last_replacement_year = -10_000
    schedule: list[ScheduledAction] = []
    for year in range(start_year + 1, start_year + horizon_years + 1):
        active_baseline_actions = tuple(
            (action.treatment_id, action.year)
            for action in baseline_actions
            if action.year < year
        )
        for component in sorted(current):
            current[component] = provider.transition_with_active_treatments(
                year=year,
                component=component,
                state=current[component],
                active_actions=active_baseline_actions,
            ).state
        candidates = sorted(
            (
                component
                for component in action_for
                if component in current and current[component].expected_rating <= threshold
            ),
            key=lambda component: (current[component].expected_rating, component),
        )
        if (
            candidates
            and min(current[component].expected_rating for component in candidates) <= 2.0
            and year - last_replacement_year
            >= library.get("full_replacement").minimum_spacing_years
        ):
            for component in sorted(current):
                current[component] = provider.transition(
                    year=year,
                    component=component,
                    state=current[component],
                    action_id="full_replacement",
                ).state
                last_action[component] = year
            last_replacement_year = year
            schedule.append(
                _scheduled(
                    year,
                    "full_replacement",
                    costs,
                    library,
                    provided_cost_ids=provided_cost_ids,
                )
            )
            continue
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
            schedule.append(
                _scheduled(
                    year,
                    action_id,
                    costs,
                    library,
                    provided_cost_ids=provided_cost_ids,
                )
            )
    return tuple(schedule)


def _merge_schedules(*schedules: Iterable[ScheduledAction]) -> tuple[ScheduledAction, ...]:
    """Combine baseline and added work without retaining redundant actions."""

    merged: dict[tuple[int, str], ScheduledAction] = {}
    for schedule in schedules:
        for action in schedule:
            merged.setdefault((action.year, action.treatment_id), action)
    replacement_years = {
        action.year
        for action in merged.values()
        if action.treatment_id == "full_replacement"
    }
    if replacement_years:
        merged = {
            key: action
            for key, action in merged.items()
            if action.year not in replacement_years
            or action.treatment_id == "full_replacement"
        }
    return tuple(sorted(merged.values(), key=lambda item: (item.year, item.treatment_id)))


def _sensitivity_results(
    *,
    result: AnnualLccaResult,
    schedule: tuple[ScheduledAction, ...],
    start_year: int,
    horizon_years: int,
    base_rate: Decimal,
) -> tuple[dict[str, object], ...]:
    end_year = start_year + horizon_years

    def value(rate: Decimal, factor: Decimal) -> Decimal:
        gross = sum(
            (
                row.base_cost
                * factor
                / ((Decimal("1") + rate) ** (row.year - start_year))
                for row in result.annual_ledger
            ),
            start=Decimal("0"),
        )
        terminal_base = Decimal("0")
        latest: dict[str, ScheduledAction] = {}
        for action in schedule:
            if action.service_life_years and action.cost_status in {"provided", "default"}:
                latest[action.treatment_id] = action
        for action in latest.values():
            assert action.service_life_years is not None
            remaining = max(
                0,
                action.service_life_years - max(0, end_year - action.year),
            )
            terminal_base += (
                action.present_cost
                * action.quantity
                * Decimal(remaining)
                / Decimal(action.service_life_years)
            )
        credit = (
            terminal_base
            * factor
            / ((Decimal("1") + rate) ** horizon_years)
        )
        return gross - credit

    points: list[dict[str, object]] = []
    rates = tuple(dict.fromkeys((Decimal("0.015"), base_rate, Decimal("0.04"))))
    for rate in rates:
        points.append(
            {
                "dimension": "discount_rate",
                "value": str(rate),
                "total_present_value_cost": str(value(rate, Decimal("1"))),
            }
        )
    for factor in (Decimal("0.8"), Decimal("1"), Decimal("1.2")):
        points.append(
            {
                "dimension": "action_cost_factor",
                "value": str(factor),
                "total_present_value_cost": str(value(base_rate, factor)),
            }
        )
    return tuple(points)


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
    minimum_service_rating: float = 3.0,
    rehabilitation_year: int | None = None,
    replacement_year: int | None = None,
    enable_effect_assumptions: bool = False,
    require_released_forecast: bool = False,
    treatments: TreatmentLibrary = DEFAULT_BRIDGE_TREATMENT_LIBRARY,
) -> BridgeLccaComparison:
    strategy_ids = tuple(dict.fromkeys(str(item).strip().lower() for item in strategies))
    if not strategy_ids or any(item not in SUPPORTED_STRATEGIES for item in strategy_ids):
        raise SimulationInputError("one or more LCCA strategies are unsupported")
    horizon = int(horizon_years)
    if horizon < 1 or horizon > 100:
        raise SimulationInputError("LCCA horizon must be 1 through 100 years")
    service_floor = float(minimum_service_rating)
    if not 0 <= service_floor <= 9:
        raise SimulationInputError("minimum service rating must be between 0 and 9")
    if service_floor > float(condition_threshold):
        raise SimulationInputError(
            "minimum service rating cannot exceed the rehabilitation trigger"
        )
    snapshot = history.snapshots[-1]
    if economics.base_year != snapshot.data_year:
        raise SimulationInputError("economic base year must match the exact as-of snapshot")
    states = _component_states(history)
    if not states:
        raise SimulationInputError("no valid component states are available")
    facts = bridge_lcca_facts(snapshot)
    provided_costs = {
        str(key).strip().lower(): Decimal(value)
        for key, value in action_costs.items()
    }
    # Preserve requests from the previous UI, where the generic $206/ft² rate
    # was incorrectly keyed as bridge cleaning.
    if (
        "bridge_cleaning" in provided_costs
        and "njdot_preservation_package" not in provided_costs
    ):
        provided_costs["njdot_preservation_package"] = provided_costs.pop(
            "bridge_cleaning"
        )
    provided_cost_ids = set(provided_costs)
    resolved_costs = default_total_action_costs(facts.geometry.deck_area)
    resolved_costs.update(provided_costs)
    final_year = snapshot.data_year + horizon

    def scheduled_year(requested: int | None, strategy: str) -> int:
        year = snapshot.data_year if requested is None else int(requested)
        if year < snapshot.data_year or year > final_year:
            raise SimulationInputError(
                f"{strategy} year must be between {snapshot.data_year} and {final_year}"
            )
        return year

    rehabilitation_action_year = (
        scheduled_year(rehabilitation_year, "rehabilitation")
        if "deck_and_substructure_rehabilitation" in strategy_ids
        else snapshot.data_year
    )
    replacement_action_year = (
        scheduled_year(replacement_year, "replacement")
        if "full_replacement" in strategy_ids
        else snapshot.data_year
    )
    try:
        package = _latest_package(database_path, history.workspace_id)
        qualification = ForecastQualification(str(package["qualification"]))
        model = deserialize_annual_markov_model(str(package["model_json"]))
        model_version_id = str(package["model_version_id"])
        evaluation_run_id = str(package["evaluation_run_id"])
        evaluation_receipt_id = str(package["evaluation_receipt_id"])
    except ForecastStoreError as exc:
        if exc.code != "forecast_package_not_found" or require_released_forecast:
            raise
        model = build_annual_markov_model(
            [history],
            through_year=snapshot.data_year,
            config=ForecastModelConfig(minimum_cohort_exposure=1),
        )
        qualification = ForecastQualification.RESEARCH_UNVALIDATED
        model_version_id = f"on-demand-{model.model_id}"
        evaluation_run_id = "on-demand-loaded-inventory"
        evaluation_receipt_id = f"loaded-inventory-on-demand:{model.source_receipt_sha256}"
    if require_released_forecast and qualification is not ForecastQualification.RELEASED:
        raise SimulationInputError("the selected forecast package is not approved for LCCA")

    schedules: dict[str, tuple[ScheduledAction, ...]] = {}
    issues: dict[str, tuple[dict[str, object], ...]] = {}
    components = {state.component for state in states}
    preventive_baseline, preventive_issues = _preventive_schedule(
        facts=facts,
        start_year=snapshot.data_year,
        horizon_years=horizon,
        costs=resolved_costs,
        provided_cost_ids=provided_cost_ids,
        library=treatments,
        available_components=components,
    )
    schedules["no_action"] = ()
    issues["no_action"] = ()
    if "njdot_preventive_cycles" in strategy_ids:
        schedules["njdot_preventive_cycles"] = preventive_baseline
        issues["njdot_preventive_cycles"] = preventive_issues
    if "deck_and_substructure_rehabilitation" in strategy_ids:
        major_actions = tuple(
            _scheduled(
                rehabilitation_action_year,
                action_id,
                resolved_costs,
                treatments,
                provided_cost_ids=provided_cost_ids,
            )
            for action_id in ("deck_replacement", "substructure_rehabilitation")
            if treatments.get(action_id).component in components
        )
        schedules["deck_and_substructure_rehabilitation"] = _merge_schedules(
            preventive_baseline,
            major_actions,
        )
        issues["deck_and_substructure_rehabilitation"] = preventive_issues
    if "full_replacement" in strategy_ids:
        schedules["full_replacement"] = _merge_schedules(
            preventive_baseline,
            (
                _scheduled(
                    replacement_action_year,
                    "full_replacement",
                    resolved_costs,
                    treatments,
                    provided_cost_ids=provided_cost_ids,
                ),
            ),
        )
        issues["full_replacement"] = preventive_issues
    if "explicit_schedule" in strategy_ids:
        schedules["explicit_schedule"] = tuple(
            _scheduled(
                int(item.year),
                item.treatment_id,
                resolved_costs,
                treatments,
                provided_cost_ids=provided_cost_ids,
            )
            for item in explicit_actions
        )
        issues["explicit_schedule"] = ()

    preliminary_actions = {
        action.treatment_id for schedule in schedules.values() for action in schedule
    }
    triggered_strategy_ids = {
        item
        for item in strategy_ids
        if item in {"reactive_management", "condition_triggered_rehabilitation"}
    }
    if triggered_strategy_ids:
        preliminary_actions.update(
            {
                "deck_replacement",
                "superstructure_rehabilitation",
                "substructure_rehabilitation",
                "full_replacement",
            }
        )
    effects = _effect_models(
        actions=preliminary_actions,
        components=components,
        library=treatments,
        enabled=enable_effect_assumptions,
    )
    provider = BridgeAnnualForecastProvider(
        model=model,
        bridge_snapshot=snapshot,
        evaluation_receipt_id=evaluation_receipt_id,
        qualification=qualification,
        treatment_effects=effects,
    )
    for triggered_strategy_id in triggered_strategy_ids:
        triggered_actions = _condition_schedule(
            provider=provider,
            states=states,
            start_year=snapshot.data_year,
            horizon_years=horizon,
            threshold=float(condition_threshold),
            costs=resolved_costs,
            provided_cost_ids=provided_cost_ids,
            library=treatments,
            baseline_actions=preventive_baseline,
        )
        schedules[triggered_strategy_id] = _merge_schedules(
            preventive_baseline,
            triggered_actions,
        )
        issues[triggered_strategy_id] = preventive_issues

    results: list[StrategyResult] = []
    analysis_period_status = "complete" if horizon >= 25 else "limited"
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
                forecast_evaluation_receipt_id=evaluation_receipt_id,
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
        modeled_ratings = [
            state.expected_rating
            for row in result.annual_ledger
            for state in row.post_action_states
        ]
        minimum_controlling_rating = (
            min(
                min(state.expected_rating for state in row.post_action_states)
                for row in result.annual_ledger
                if row.post_action_states
            )
            if modeled_ratings
            else facts.controlling_rating
        )
        meets_service_threshold = bool(
            minimum_controlling_rating is not None
            and minimum_controlling_rating >= service_floor
        )
        unavailable_costs = tuple(
            action.treatment_id
            for action in schedule
            if action.cost_status == "unavailable"
        )
        strategy_issues = issues.get(strategy_id, ())
        unevaluable = any(
            str(item.get("status")) == "not_evaluable" for item in strategy_issues
        )
        incompatible = any(
            str(item.get("status")) == "incompatible" for item in strategy_issues
        )
        reasons: list[str] = []
        if strategy_id == "no_action":
            feasibility_status = "diagnostic"
            reasons.append("No work is scheduled, so this forecast is not a service-equivalent cost alternative.")
        elif unavailable_costs:
            feasibility_status = "data_insufficient"
            reasons.append(
                "Cost unavailable for: " + ", ".join(sorted(set(unavailable_costs)))
            )
        elif not schedule and unevaluable:
            feasibility_status = "data_insufficient"
            reasons.append("No treatment could be scheduled with the available evidence.")
        elif not schedule and incompatible:
            feasibility_status = "not_applicable"
            reasons.append("No preventive treatment is applicable to the current bridge condition and materials.")
        elif not meets_service_threshold:
            feasibility_status = "fails_service_threshold"
            reasons.append(
                f"The modeled controlling condition falls below the {service_floor:g} service floor."
            )
        else:
            feasibility_status = "feasible"
        if analysis_period_status == "limited" and strategy_id != "no_action":
            reasons.append("The analysis period is shorter than 25 years, so no cost ranking is issued.")
        rankable = feasibility_status == "feasible" and analysis_period_status == "complete"
        sensitivity = list(
            _sensitivity_results(
                result=result,
                schedule=schedule,
                start_year=snapshot.data_year,
                horizon_years=horizon,
                base_rate=economics.real_discount_rate,
            )
        )
        if strategy_id == "njdot_preventive_cycles" and enable_effect_assumptions:
            for reduction in (0.10, 0.20, 0.30):
                if math.isclose(
                    reduction,
                    _DEFAULT_PRESERVATION_DETERIORATION_REDUCTION,
                ):
                    sensitivity_result = result
                else:
                    sensitivity_provider = BridgeAnnualForecastProvider(
                        model=model,
                        bridge_snapshot=snapshot,
                        evaluation_receipt_id=evaluation_receipt_id,
                        qualification=qualification,
                        treatment_effects=_effect_models(
                            actions=preliminary_actions,
                            components=components,
                            library=treatments,
                            enabled=True,
                            preservation_deterioration_reduction=reduction,
                        ),
                    )
                    sensitivity_result = simulate_annual_lcca(
                        request=AnnualLccaRequest(
                            identity=snapshot.identity,
                            bridge_id=f"{snapshot.state_code}:{snapshot.bridge_number}",
                            start_year=snapshot.data_year,
                            horizon_years=horizon,
                            discount_rate=economics.real_discount_rate,
                            forecast_model_id=model.model_id,
                            forecast_evaluation_receipt_id=evaluation_receipt_id,
                            initial_states=states,
                            alternative=LccaAlternative(
                                alternative_id=strategy_id,
                                scheduled_actions=schedule,
                            ),
                            require_released_forecast=require_released_forecast,
                            economic_assumptions=economics,
                        ),
                        forecast=sensitivity_provider,
                        treatments=treatments,
                    )
                sensitivity_minimum = min(
                    min(state.expected_rating for state in row.post_action_states)
                    for row in sensitivity_result.annual_ledger
                    if row.post_action_states
                )
                ending_states = sensitivity_result.annual_ledger[-1].post_action_states
                sensitivity.append(
                    {
                        "dimension": "preservation_deterioration_reduction",
                        "value": str(reduction),
                        "total_present_value_cost": str(
                            sensitivity_result.total_present_value_cost
                        ),
                        "minimum_controlling_rating": sensitivity_minimum,
                        "ending_controlling_rating": min(
                            state.expected_rating for state in ending_states
                        ),
                    }
                )
        results.append(
            StrategyResult(
                strategy_id=strategy_id,
                result=result,
                scheduled_actions=schedule,
                applicability_issues=strategy_issues,
                feasibility_status=feasibility_status,
                feasibility_reasons=tuple(reasons),
                rankable=rankable,
                cost_complete=not unavailable_costs,
                minimum_controlling_rating=minimum_controlling_rating,
                meets_service_threshold=meets_service_threshold,
                sensitivity=tuple(sensitivity),
            )
        )
    receipt_payload = {
        "snapshot_revision_id": snapshot.snapshot_revision_id,
        "canonical_sha256": snapshot.canonical_sha256,
        "model_id": model.model_id,
        "evaluation_receipt_id": evaluation_receipt_id,
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
        "condition_policy": {
            "rehabilitation_trigger": float(condition_threshold),
            "minimum_service_rating": service_floor,
        },
        "effect_evidence": "default_effect_assumptions" if enable_effect_assumptions else "none",
    }
    receipt = hashlib.sha256(canonical_json_text(receipt_payload).encode("utf-8")).hexdigest()
    return BridgeLccaComparison(
        scenario_receipt_sha256=receipt,
        facts=facts,
        model_version_id=model_version_id,
        model_id=model.model_id,
        evaluation_run_id=evaluation_run_id,
        evaluation_receipt_id=evaluation_receipt_id,
        forecast_qualification=qualification,
        treatment_library_id=treatments.library_id,
        treatment_library_sha256=treatments.library_sha256,
        effect_evidence="default_effect_assumptions" if enable_effect_assumptions else "none",
        analysis_period_status=analysis_period_status,
        cost_scope="agency_costs_only",
        cost_library_id=DEFAULT_NJDOT_PLANNING_COSTS.library_id,
        cost_library_version=DEFAULT_NJDOT_PLANNING_COSTS.version,
        resolved_action_costs=resolved_costs,
        rehabilitation_trigger=float(condition_threshold),
        minimum_service_rating=service_floor,
        alternatives=tuple(results),
    )
