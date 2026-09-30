"""One-year-step LCCA orchestration over a qualified forecast provider.

This module deliberately has no deterioration or recovery constants.  Every
component/year transition, including a treatment effect, comes from the bound
forecast provider.  Cost and trigger engines will be layered onto this ledger
without changing that boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from app.services.analytics.identity import AnalysisSnapshotIdentity
from app.services.analytics.treatments import TreatmentLibrary
from app.services.forecasting_v2.models import (
    ComponentState,
    ForecastQualification,
    ForecastTransition,
    ForecastTransitionProvider,
)
from app.services.lcca_v2.economics import (
    EconomicAssumptions,
    EconomicMode,
    discount_cost,
    equivalent_annual_cost,
)


class SimulationInputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ScheduledAction:
    year: int
    treatment_id: str
    present_cost: Decimal = Decimal("0")
    quantity: Decimal = Decimal("1")
    cost_unit: str = "lump_sum"
    cost_status: str = "provided"
    cost_source: str = "request"
    included_in: str | None = None
    service_life_years: int | None = None

    def __post_init__(self) -> None:
        treatment = str(self.treatment_id).strip().lower()
        if not treatment:
            raise ValueError("treatment_id is required")
        cost = Decimal(self.present_cost)
        quantity = Decimal(self.quantity)
        unit = str(self.cost_unit).strip()
        cost_status = str(self.cost_status).strip().lower()
        cost_source = str(self.cost_source).strip()
        if not cost.is_finite() or cost < 0 or not quantity.is_finite() or quantity < 0:
            raise ValueError("scheduled action cost and quantity must be finite and non-negative")
        if not unit:
            raise ValueError("cost_unit is required")
        if cost_status not in {"provided", "default", "included", "unavailable"}:
            raise ValueError("scheduled action cost_status is invalid")
        if not cost_source:
            raise ValueError("scheduled action cost_source is required")
        if self.service_life_years is not None and int(self.service_life_years) <= 0:
            raise ValueError("service_life_years must be positive")
        object.__setattr__(self, "year", int(self.year))
        object.__setattr__(self, "treatment_id", treatment)
        object.__setattr__(self, "present_cost", cost)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "cost_unit", unit)
        object.__setattr__(self, "cost_status", cost_status)
        object.__setattr__(self, "cost_source", cost_source)
        object.__setattr__(
            self,
            "included_in",
            str(self.included_in).strip().lower() if self.included_in else None,
        )
        if self.service_life_years is not None:
            object.__setattr__(self, "service_life_years", int(self.service_life_years))


@dataclass(frozen=True, slots=True)
class LccaAlternative:
    alternative_id: str
    scheduled_actions: tuple[ScheduledAction, ...] = ()

    def __post_init__(self) -> None:
        alternative_id = str(self.alternative_id).strip().lower()
        if not alternative_id:
            raise ValueError("alternative_id is required")
        actions = tuple(sorted(tuple(self.scheduled_actions), key=lambda item: (item.year, item.treatment_id)))
        identities = tuple((item.year, item.treatment_id) for item in actions)
        if len(identities) != len(set(identities)):
            raise ValueError("scheduled action year/treatment pairs must be unique")
        object.__setattr__(self, "alternative_id", alternative_id)
        object.__setattr__(self, "scheduled_actions", actions)


@dataclass(frozen=True, slots=True)
class AnnualLccaRequest:
    identity: AnalysisSnapshotIdentity
    bridge_id: str
    start_year: int
    horizon_years: int
    discount_rate: Decimal
    forecast_model_id: str
    forecast_evaluation_receipt_id: str
    initial_states: tuple[ComponentState, ...]
    alternative: LccaAlternative
    require_released_forecast: bool = True
    economic_assumptions: EconomicAssumptions | None = None

    def __post_init__(self) -> None:
        bridge_id = str(self.bridge_id).strip()
        model_id = str(self.forecast_model_id).strip()
        receipt = str(self.forecast_evaluation_receipt_id).strip()
        start = int(self.start_year)
        horizon = int(self.horizon_years)
        rate = Decimal(self.discount_rate)
        states = tuple(self.initial_states)
        if not bridge_id:
            raise ValueError("bridge_id is required")
        if not model_id:
            raise ValueError("forecast_model_id is required")
        if start != self.identity.data_year:
            raise ValueError("start_year must match the analytical snapshot year")
        if horizon < 1 or horizon > 200:
            raise ValueError("horizon_years must be between 1 and 200")
        if not rate.is_finite() or rate < Decimal("-0.99"):
            raise ValueError("discount_rate must be finite and greater than -0.99")
        if not states:
            raise ValueError("initial_states are required")
        components = tuple(state.component for state in states)
        if len(components) != len(set(components)):
            raise ValueError("initial state components must be unique")
        object.__setattr__(self, "bridge_id", bridge_id)
        object.__setattr__(self, "start_year", start)
        object.__setattr__(self, "horizon_years", horizon)
        object.__setattr__(self, "discount_rate", rate)
        object.__setattr__(self, "forecast_model_id", model_id)
        object.__setattr__(self, "forecast_evaluation_receipt_id", receipt)
        object.__setattr__(self, "initial_states", states)


@dataclass(frozen=True, slots=True)
class AnnualLedgerRow:
    year: int
    pre_action_states: tuple[ComponentState, ...]
    post_action_states: tuple[ComponentState, ...]
    selected_action_ids: tuple[str, ...]
    base_cost: Decimal = Decimal("0")
    future_cost: Decimal = Decimal("0")
    inflation_factor: Decimal = Decimal("1")
    discount_factor: Decimal = Decimal("1")
    present_value_cost: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class AnnualLccaResult:
    identity: AnalysisSnapshotIdentity
    bridge_id: str
    alternative_id: str
    forecast_model_id: str
    forecast_evaluation_receipt_id: str
    treatment_library_id: str
    treatment_library_sha256: str
    annual_ledger: tuple[AnnualLedgerRow, ...]
    gross_present_value_cost: Decimal
    remaining_service_life_credit: Decimal
    total_present_value_cost: Decimal
    equivalent_annual_cost: Decimal
    economic_mode: str


def _validated_transition(
    transition: ForecastTransition,
    *,
    year: int,
    component: str,
    action_id: str | None,
    model_id: str,
    receipt_id: str,
) -> ComponentState:
    if transition.year != year:
        raise SimulationInputError("forecast returned a mismatched transition year")
    if transition.component != component:
        raise SimulationInputError("forecast returned a mismatched component")
    if transition.action_id != action_id:
        raise SimulationInputError("forecast returned a mismatched action")
    if transition.model_id != model_id:
        raise SimulationInputError("forecast returned a mismatched model")
    if transition.evaluation_receipt_id != receipt_id:
        raise SimulationInputError("forecast returned a mismatched evaluation receipt")
    return transition.state


def _ordered_states(states: dict[str, ComponentState]) -> tuple[ComponentState, ...]:
    return tuple(states[key] for key in sorted(states))


def simulate_annual_lcca(
    *,
    request: AnnualLccaRequest,
    forecast: ForecastTransitionProvider,
    treatments: TreatmentLibrary,
) -> AnnualLccaResult:
    """Advance every component exactly one year, then apply that year's actions."""

    if str(forecast.model_id).strip() != request.forecast_model_id:
        raise SimulationInputError("forecast model identity mismatch")
    if (
        str(forecast.evaluation_receipt_id).strip()
        != request.forecast_evaluation_receipt_id
    ):
        raise SimulationInputError("forecast evaluation receipt mismatch")
    if not request.forecast_evaluation_receipt_id:
        raise SimulationInputError("forecast evaluation receipt is required")
    if (
        request.require_released_forecast
        and forecast.qualification is not ForecastQualification.RELEASED
    ):
        raise SimulationInputError("forecast is not released for LCCA")

    actions_by_year: dict[int, tuple[ScheduledAction, ...]] = {}
    for action in request.alternative.scheduled_actions:
        if action.year < request.start_year or action.year > request.start_year + request.horizon_years:
            raise SimulationInputError("scheduled action is outside the annual simulation period")
        try:
            treatments.get(action.treatment_id)
        except KeyError as exc:
            raise SimulationInputError(str(exc)) from exc
        actions_by_year[action.year] = actions_by_year.get(action.year, ()) + (action,)

    assumptions = request.economic_assumptions or EconomicAssumptions(
        mode=EconomicMode.CONSTANT_DOLLAR,
        base_year=request.start_year,
        real_discount_rate=request.discount_rate,
    )
    if assumptions.base_year != request.start_year:
        raise SimulationInputError("economic base year must match the simulation start year")

    current = {state.component: state for state in request.initial_states}
    ledger: list[AnnualLedgerRow] = []

    def apply_actions(
        year: int,
        pre_action: dict[str, ComponentState],
    ) -> tuple[dict[str, ComponentState], tuple[str, ...], Decimal, Decimal, Decimal, Decimal, Decimal]:
        post_action = dict(pre_action)
        selected: list[str] = []
        base_cost = Decimal("0")
        future_cost = Decimal("0")
        present_value = Decimal("0")
        inflation_factor = Decimal("1")
        discount_factor = Decimal("1")
        for scheduled in actions_by_year.get(year, ()):
            treatment = treatments.get(scheduled.treatment_id)
            component = treatment.component
            if treatment.treatment_id == "full_replacement":
                for affected_component in sorted(post_action):
                    transition = forecast.transition(
                        year=year,
                        component=affected_component,
                        state=post_action[affected_component],
                        action_id=treatment.treatment_id,
                    )
                    post_action[affected_component] = _validated_transition(
                        transition,
                        year=year,
                        component=affected_component,
                        action_id=treatment.treatment_id,
                        model_id=request.forecast_model_id,
                        receipt_id=request.forecast_evaluation_receipt_id,
                    )
            elif component in post_action:
                transition = forecast.transition(
                    year=year,
                    component=component,
                    state=post_action[component],
                    action_id=treatment.treatment_id,
                )
                post_action[component] = _validated_transition(
                    transition,
                    year=year,
                    component=component,
                    action_id=treatment.treatment_id,
                    model_id=request.forecast_model_id,
                    receipt_id=request.forecast_evaluation_receipt_id,
                )
            elif component not in {"bridge", "bearing", "parapet"}:
                raise SimulationInputError(
                    f"treatment component is unavailable for this bridge: {component}"
                )
            selected.append(treatment.treatment_id)
            discounted = discount_cost(
                scheduled.present_cost * scheduled.quantity,
                funding_year=year,
                assumptions=assumptions,
            )
            base_cost += discounted.base_cost
            future_cost += discounted.future_cost
            present_value += discounted.present_value
            inflation_factor = discounted.inflation_factor
            discount_factor = discounted.discount_factor
        return (
            post_action,
            tuple(selected),
            base_cost,
            future_cost,
            inflation_factor,
            discount_factor,
            present_value,
        )

    if request.start_year in actions_by_year:
        (
            current,
            selected,
            base_cost,
            future_cost,
            inflation_factor,
            discount_factor,
            present_value,
        ) = apply_actions(request.start_year, current)
        ledger.append(
            AnnualLedgerRow(
                year=request.start_year,
                pre_action_states=_ordered_states({state.component: state for state in request.initial_states}),
                post_action_states=_ordered_states(current),
                selected_action_ids=selected,
                base_cost=base_cost,
                future_cost=future_cost,
                inflation_factor=inflation_factor,
                discount_factor=discount_factor,
                present_value_cost=present_value,
            )
        )

    for year in range(request.start_year + 1, request.start_year + request.horizon_years + 1):
        pre_action: dict[str, ComponentState] = {}
        active_actions = tuple(
            (action.treatment_id, action.year)
            for action in request.alternative.scheduled_actions
            if action.year < year
        )
        for component in sorted(current):
            protected_transition = getattr(
                forecast, "transition_with_active_treatments", None
            )
            if callable(protected_transition):
                transition = protected_transition(
                    year=year,
                    component=component,
                    state=current[component],
                    active_actions=active_actions,
                )
            else:
                transition = forecast.transition(
                    year=year,
                    component=component,
                    state=current[component],
                    action_id=None,
                )
            pre_action[component] = _validated_transition(
                transition,
                year=year,
                component=component,
                action_id=None,
                model_id=request.forecast_model_id,
                receipt_id=request.forecast_evaluation_receipt_id,
            )

        (
            post_action,
            selected,
            base_cost,
            future_cost,
            inflation_factor,
            discount_factor,
            present_value,
        ) = apply_actions(year, pre_action)

        ledger.append(
            AnnualLedgerRow(
                year=year,
                pre_action_states=_ordered_states(pre_action),
                post_action_states=_ordered_states(post_action),
                selected_action_ids=selected,
                base_cost=base_cost,
                future_cost=future_cost,
                inflation_factor=inflation_factor,
                discount_factor=discount_factor,
                present_value_cost=present_value,
            )
        )
        current = post_action

    total_present_value_cost = sum(
        (row.present_value_cost for row in ledger),
        start=Decimal("0"),
    )
    end_year = request.start_year + request.horizon_years
    latest_service_actions: dict[str, ScheduledAction] = {}
    for action in request.alternative.scheduled_actions:
        if action.service_life_years is not None and action.cost_status in {"provided", "default"}:
            latest_service_actions[action.treatment_id] = action
    remaining_service_life_credit = Decimal("0")
    for action in latest_service_actions.values():
        assert action.service_life_years is not None
        used_years = max(0, end_year - action.year)
        remaining_years = max(0, action.service_life_years - used_years)
        if remaining_years <= 0:
            continue
        undiscounted_credit = (
            action.present_cost
            * action.quantity
            * Decimal(remaining_years)
            / Decimal(action.service_life_years)
        )
        remaining_service_life_credit += discount_cost(
            undiscounted_credit,
            funding_year=end_year,
            assumptions=assumptions,
        ).present_value
    net_present_value_cost = total_present_value_cost - remaining_service_life_credit
    return AnnualLccaResult(
        identity=request.identity,
        bridge_id=request.bridge_id,
        alternative_id=request.alternative.alternative_id,
        forecast_model_id=request.forecast_model_id,
        forecast_evaluation_receipt_id=request.forecast_evaluation_receipt_id,
        treatment_library_id=treatments.library_id,
        treatment_library_sha256=treatments.library_sha256,
        annual_ledger=tuple(ledger),
        gross_present_value_cost=total_present_value_cost,
        remaining_service_life_credit=remaining_service_life_credit,
        total_present_value_cost=net_present_value_cost,
        equivalent_annual_cost=equivalent_annual_cost(
            net_present_value_cost,
            assumptions.real_discount_rate,
            request.horizon_years,
        ),
        economic_mode=assumptions.mode.value,
    )
