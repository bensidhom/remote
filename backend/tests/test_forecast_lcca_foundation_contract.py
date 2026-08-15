from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from app.services.analytics.identity import AnalysisSnapshotIdentity
from app.services.analytics.life_history import (
    BridgeLifeHistory,
    EvidenceClass,
    LifeHistoryEvent,
)
from app.services.analytics.treatments import (
    DEFAULT_NJDOT_TREATMENT_LIBRARY,
    TreatmentApplicability,
)
from app.services.forecasting_v2.models import (
    ComponentState,
    ForecastQualification,
    ForecastTransition,
)
from app.services.lcca_v2.simulation import (
    AnnualLccaRequest,
    LccaAlternative,
    ScheduledAction,
    SimulationInputError,
    simulate_annual_lcca,
)
from app.services.lcca_v2.economics import (
    EconomicAssumptions,
    EconomicMode,
    discount_cost,
    equivalent_annual_cost,
)


def _identity() -> AnalysisSnapshotIdentity:
    return AnalysisSnapshotIdentity(
        workspace_id="workspace-a",
        state_code=34,
        data_year=2024,
        revision_set_sha256="a" * 64,
        data_generation=7,
        schema_generation=8,
        specification_sha256="b" * 64,
    )


def test_analysis_identity_is_complete_frozen_and_hashable():
    identity = _identity()
    assert identity.cache_key == (
        "workspace-a",
        34,
        2024,
        "a" * 64,
        7,
        8,
        "b" * 64,
    )
    assert hash(identity)
    with pytest.raises(FrozenInstanceError):
        identity.data_year = 2025  # type: ignore[misc]


def test_life_history_keeps_observed_inferred_and_simulated_events_distinct():
    events = (
        LifeHistoryEvent(
            event_id="inspection-2020",
            event_year=2020,
            component="deck",
            treatment_id=None,
            evidence_class=EvidenceClass.CONDITION_CHANGE_CANDIDATE,
            source_record_ids=("record-2020",),
        ),
        LifeHistoryEvent(
            event_id="work-2022",
            event_year=2022,
            component="deck",
            treatment_id="deck_crack_seal",
            evidence_class=EvidenceClass.OBSERVED_WORK,
            source_record_ids=("work-order-17",),
        ),
        LifeHistoryEvent(
            event_id="scenario-2026",
            event_year=2026,
            component="deck",
            treatment_id="deck_seal",
            evidence_class=EvidenceClass.SIMULATED_WORK,
            source_record_ids=(),
        ),
    )
    history = BridgeLifeHistory.build(
        bridge_id="34:0101150",
        construction_year=1980,
        latest_observed_year=2024,
        events=events,
    )
    assert history.events == events
    assert history.observed_events == (events[1],)
    assert history.condition_change_candidates == (events[0],)
    assert history.simulated_events == (events[2],)
    assert history.unknown_history_interval == (1980, 2019)


def test_authoritative_work_requires_source_evidence():
    with pytest.raises(ValueError, match="source_record_ids"):
        LifeHistoryEvent(
            event_id="unsupported-claim",
            event_year=2022,
            component="deck",
            treatment_id="deck_crack_seal",
            evidence_class=EvidenceClass.OBSERVED_WORK,
            source_record_ids=(),
        )


def test_life_history_accepts_documented_pre_1800_inventory_work():
    event = LifeHistoryEvent(
        event_id="inventory-work:1798",
        event_year=1798,
        component="bridge",
        treatment_id="br1",
        evidence_class=EvidenceClass.INVENTORY_REPORTED_WORK,
        source_record_ids=("revision-1798",),
    )

    history = BridgeLifeHistory.build(
        bridge_id="34-1105151",
        construction_year=1798,
        latest_observed_year=2025,
        events=(event,),
    )

    assert history.inventory_reported_events == (event,)


def test_njdot_treatment_profile_has_published_cycles_and_component_scope():
    library = DEFAULT_NJDOT_TREATMENT_LIBRARY
    assert library.library_id == "njdot_preventive_maintenance_2016_tamp"
    assert library.get("bridge_cleaning").cycle_years == 2
    assert library.get("bearing_lubrication").cycle_years == 4
    assert library.get("deck_seal").cycle_years == 5
    assert library.get("joint_repair_replace").cycle_years == 10
    assert library.get("substructure_concrete_repair").cycle_years == 10
    assert library.get("scour_repair").cycle_years is None
    assert library.get("scour_repair").trigger_kind.value == "as_needed"


def test_treatment_compatibility_fails_closed_for_wrong_material_or_missing_evidence():
    deck_seal = DEFAULT_NJDOT_TREATMENT_LIBRARY.get("deck_seal")
    wrong_material = deck_seal.evaluate(
        component="deck",
        material_code="ST",
        condition_rating=7,
        element_evidence_available=True,
        concurrent_work=False,
    )
    assert wrong_material.status is TreatmentApplicability.INCOMPATIBLE
    assert "material" in wrong_material.reasons

    missing_elements = DEFAULT_NJDOT_TREATMENT_LIBRARY.get("deck_crack_seal").evaluate(
        component="deck",
        material_code="CR-T",
        condition_rating=7,
        element_evidence_available=False,
        concurrent_work=False,
    )
    assert missing_elements.status is TreatmentApplicability.NOT_EVALUABLE
    assert "element_evidence" in missing_elements.reasons


class RecordingForecast:
    model_id = "forecast-v2"
    evaluation_receipt_id = "hindcast-receipt-v2"
    qualification = ForecastQualification.RELEASED

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str | None]] = []

    def transition(
        self,
        *,
        year: int,
        component: str,
        state: ComponentState,
        action_id: str | None,
    ) -> ForecastTransition:
        self.calls.append((year, component, action_id))
        if action_id == "deck_seal":
            distribution = (0, 0, 0, 0, 0, 0, 0, 1, 0, 0)
        else:
            current = state.most_likely_rating
            next_rating = max(0, current - 1)
            distribution = tuple(1 if i == next_rating else 0 for i in range(10))
        return ForecastTransition(
            component=component,
            year=year,
            action_id=action_id,
            distribution=distribution,
            model_id=self.model_id,
            evaluation_receipt_id=self.evaluation_receipt_id,
            fallback_level="test",
        )


def test_lcca_is_an_annual_forecast_driven_simulation():
    forecast = RecordingForecast()
    request = AnnualLccaRequest(
        identity=_identity(),
        bridge_id="34:0101150",
        start_year=2024,
        horizon_years=3,
        discount_rate=Decimal("0.03"),
        forecast_model_id=forecast.model_id,
        forecast_evaluation_receipt_id=forecast.evaluation_receipt_id,
        initial_states=(
            ComponentState.certain("deck", 8),
            ComponentState.certain("superstructure", 7),
        ),
        alternative=LccaAlternative(
            alternative_id="scheduled",
            scheduled_actions=(ScheduledAction(year=2026, treatment_id="deck_seal"),),
        ),
    )
    result = simulate_annual_lcca(
        request=request,
        forecast=forecast,
        treatments=DEFAULT_NJDOT_TREATMENT_LIBRARY,
    )

    assert [row.year for row in result.annual_ledger] == [2025, 2026, 2027]
    assert len(result.annual_ledger) == request.horizon_years
    assert forecast.calls == [
        (2025, "deck", None),
        (2025, "superstructure", None),
        (2026, "deck", None),
        (2026, "superstructure", None),
        (2026, "deck", "deck_seal"),
        (2027, "deck", None),
        (2027, "superstructure", None),
    ]
    assert result.forecast_model_id == forecast.model_id
    assert result.forecast_evaluation_receipt_id == forecast.evaluation_receipt_id
    assert result.annual_ledger[1].selected_action_ids == ("deck_seal",)


@pytest.mark.parametrize(
    ("qualification", "receipt"),
    [
        (ForecastQualification.RESEARCH_UNVALIDATED, "hindcast-receipt-v2"),
        (ForecastQualification.RELEASED, ""),
    ],
)
def test_commercial_lcca_rejects_unqualified_or_unreceipted_forecast(
    qualification: ForecastQualification,
    receipt: str,
):
    forecast = RecordingForecast()
    forecast.qualification = qualification
    forecast.evaluation_receipt_id = receipt
    request = AnnualLccaRequest(
        identity=_identity(),
        bridge_id="34:0101150",
        start_year=2024,
        horizon_years=1,
        discount_rate=Decimal("0.03"),
        forecast_model_id=forecast.model_id,
        forecast_evaluation_receipt_id=receipt,
        initial_states=(ComponentState.certain("deck", 8),),
        alternative=LccaAlternative(alternative_id="no_action"),
        require_released_forecast=True,
    )
    with pytest.raises(SimulationInputError, match="forecast"):
        simulate_annual_lcca(
            request=request,
            forecast=forecast,
            treatments=DEFAULT_NJDOT_TREATMENT_LIBRARY,
        )


def test_forecast_identity_mismatch_is_rejected_before_first_year():
    forecast = RecordingForecast()
    request = AnnualLccaRequest(
        identity=_identity(),
        bridge_id="34:0101150",
        start_year=2024,
        horizon_years=1,
        discount_rate=Decimal("0.03"),
        forecast_model_id="different-model",
        forecast_evaluation_receipt_id=forecast.evaluation_receipt_id,
        initial_states=(ComponentState.certain("deck", 8),),
        alternative=LccaAlternative(alternative_id="no_action"),
    )
    with pytest.raises(SimulationInputError, match="model"):
        simulate_annual_lcca(
            request=request,
            forecast=forecast,
            treatments=DEFAULT_NJDOT_TREATMENT_LIBRARY,
        )
    assert forecast.calls == []


def test_year_zero_action_and_cost_are_retained_in_the_annual_ledger():
    forecast = RecordingForecast()
    request = AnnualLccaRequest(
        identity=_identity(),
        bridge_id="34:0101150",
        start_year=2024,
        horizon_years=2,
        discount_rate=Decimal("0.03"),
        forecast_model_id=forecast.model_id,
        forecast_evaluation_receipt_id=forecast.evaluation_receipt_id,
        initial_states=(ComponentState.certain("deck", 6),),
        alternative=LccaAlternative(
            alternative_id="initial-deck-work",
            scheduled_actions=(
                ScheduledAction(
                    year=2024,
                    treatment_id="deck_seal",
                    present_cost=Decimal("1000"),
                ),
            ),
        ),
    )
    result = simulate_annual_lcca(
        request=request,
        forecast=forecast,
        treatments=DEFAULT_NJDOT_TREATMENT_LIBRARY,
    )

    assert [row.year for row in result.annual_ledger] == [2024, 2025, 2026]
    assert result.annual_ledger[0].selected_action_ids == ("deck_seal",)
    assert result.annual_ledger[0].present_value_cost == Decimal("1000")
    assert result.total_present_value_cost == Decimal("1000")
    assert result.equivalent_annual_cost == equivalent_annual_cost(
        Decimal("1000"), Decimal("0.03"), 2
    )


def test_constant_and_nominal_economics_cannot_be_mixed():
    with pytest.raises(ValueError, match="constant-dollar"):
        EconomicAssumptions(
            mode=EconomicMode.CONSTANT_DOLLAR,
            base_year=2024,
            real_discount_rate=Decimal("0.025"),
            inflation_rate=Decimal("0.02"),
        )
    assumptions = EconomicAssumptions(
        mode=EconomicMode.NOMINAL_DOLLAR,
        base_year=2024,
        real_discount_rate=Decimal("0.025"),
        inflation_rate=Decimal("0.02"),
        nominal_discount_rate=Decimal("0.0455"),
    )
    discounted = discount_cost(
        Decimal("100"), funding_year=2025, assumptions=assumptions
    )
    assert discounted.future_cost == Decimal("102.00")
    assert discounted.present_value == pytest.approx(
        Decimal("102") / Decimal("1.0455")
    )
