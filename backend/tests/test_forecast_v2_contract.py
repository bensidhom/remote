from __future__ import annotations

from dataclasses import replace
import json

import pytest

from app.services.analytics.identity import AnalysisSnapshotIdentity
from app.services.analytics.life_history import BridgeLifeHistory, EvidenceClass, LifeHistoryEvent
from app.services.analytics.projection import (
    ANALYTICAL_SPECIFICATION_SHA256,
    BridgeAnalysisHistory,
    BridgeAnalysisSnapshot,
    CarriedFeature,
    CarriedFeatureEvidence,
    ConditionEvidence,
    FeatureSelectionStatus,
)
from app.services.forecasting_v2.estimator import (
    BridgeAnnualForecastProvider,
    ForecastInputError,
    ForecastModelConfig,
    TreatmentEffectModel,
    build_annual_markov_model,
)
from app.services.forecasting_v2.evaluation import evaluate_historical_hindcasts
from app.services.forecasting_v2.models import ComponentState, ForecastQualification
from app.services.forecasting_v2.store import (
    load_forecast_model,
    persist_forecast_package,
    serialize_annual_markov_model,
)
from app.services.forecasting_v2.runs import (
    create_bridge_forecast_run,
    load_bridge_forecast_run,
)
from app.services.lcca_v2.economics import EconomicAssumptions, EconomicMode
from app.services.lcca_v2.scenario import compare_bridge_lcca_strategies
from decimal import Decimal
from app.services.conversion.revision_store import ensure_workspace


def _snapshot(bridge: str, year: int, rating: int, *, traffic: int = 1000) -> BridgeAnalysisSnapshot:
    revision = f"revision-{bridge}-{year}"
    identity = AnalysisSnapshotIdentity(
        workspace_id="workspace-a",
        state_code=34,
        data_year=year,
        revision_set_sha256=(f"{year % 256:02x}" * 32),
        data_generation=1,
        schema_generation=8,
        specification_sha256=ANALYTICAL_SPECIFICATION_SHA256,
    )
    return BridgeAnalysisSnapshot(
        identity=identity,
        snapshot_revision_id=revision,
        canonical_sha256=(f"{(year + 1) % 256:02x}" * 32),
        published_at=f"{year}-12-31T00:00:00Z",
        state_code=34,
        bridge_number=bridge,
        data_year=year,
        bridge_name=bridge,
        county_code=1,
        owner_code="1",
        maintenance_responsibility_code="1",
        year_built=1980,
        age_at_snapshot=year - 1980,
        total_bridge_length=100.0,
        bridge_width_out_to_out=30.0,
        calculated_deck_area=3000.0,
        root_nhs_designation="Y",
        condition=ConditionEvidence(
            bc01=float(rating),
            bc02=float(rating),
            bc03=float(rating),
            bc04=None,
            bc13=float(rating),
            controlling_rating=float(rating),
            condition_class="Good" if rating >= 7 else "Fair" if rating >= 5 else "Poor",
            basis="reported_bc13",
            conflict=False,
            conflict_reasons=(),
            invalid_fields=(),
        ),
        carried_features=CarriedFeatureEvidence(
            status=FeatureSelectionStatus.SELECTED,
            candidates=(CarriedFeature("H01", "C", "Y", traffic * 10, traffic, year),),
            selected=CarriedFeature("H01", "C", "Y", traffic * 10, traffic, year),
        ),
        span_materials=(),
        substructure_materials=(),
    )


def _history(
    bridge: str,
    observations: tuple[tuple[int, int], ...],
    *,
    work_years: tuple[int, ...] = (),
) -> BridgeAnalysisHistory:
    snapshots = tuple(_snapshot(bridge, year, rating) for year, rating in observations)
    events: list[LifeHistoryEvent] = [
        LifeHistoryEvent(
            event_id=f"observation:{bridge}:{snapshot.data_year}",
            event_year=snapshot.data_year,
            component="bridge",
            treatment_id=None,
            evidence_class=EvidenceClass.INVENTORY_OBSERVATION,
            source_record_ids=(snapshot.snapshot_revision_id,),
        )
        for snapshot in snapshots
    ]
    events.extend(
        LifeHistoryEvent(
            event_id=f"work:{bridge}:{year}",
            event_year=year,
            component="bridge",
            treatment_id="generic_rehabilitation",
            evidence_class=EvidenceClass.INVENTORY_REPORTED_WORK,
            source_record_ids=(f"work-record:{bridge}:{year}",),
        )
        for year in work_years
    )
    return BridgeAnalysisHistory(
        workspace_id="workspace-a",
        state_code=34,
        bridge_number=bridge,
        snapshots=snapshots,
        life_history=BridgeLifeHistory.build(
            bridge_id=f"34:{bridge}",
            construction_year=1980,
            latest_observed_year=snapshots[-1].data_year,
            events=events,
        ),
    )


def _fleet() -> tuple[BridgeAnalysisHistory, ...]:
    return (
        _history("A", ((2020, 8), (2021, 7), (2022, 6))),
        _history("B", ((2020, 8), (2021, 8), (2022, 7))),
        _history("C", ((2020, 6), (2021, 8), (2022, 8)), work_years=(2021,)),
    )


def test_model_uses_only_no_action_non_improving_intervals_and_annual_mass():
    model = build_annual_markov_model(
        _fleet(),
        through_year=2021,
        config=ForecastModelConfig(minimum_cohort_exposure=1),
    )
    assert model.statistics.used_intervals == 2
    assert model.statistics.excluded_improvement_intervals == 1
    assert model.statistics.excluded_work_intervals == 1

    distribution, evidence = model.transition_distribution(
        component="deck",
        rating=8,
        state_code=34,
        county_code=1,
        age=41,
        traffic_band="medium",
    )
    assert sum(distribution) == pytest.approx(1.0)
    assert all(distribution[index] == 0 for index in range(9) if index > 8)
    assert evidence.exposure >= 1
    assert evidence.fallback_level

    absorbing, _ = model.transition_distribution(
        component="deck",
        rating=0,
        state_code=34,
        county_code=1,
        age=80,
        traffic_band=None,
    )
    assert absorbing == (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def test_future_observation_cannot_change_past_origin_model_identity():
    original = _fleet()
    changed_future = list(original)
    changed_snapshot = replace(
        changed_future[0].snapshots[-1],
        condition=replace(
            changed_future[0].snapshots[-1].condition,
            bc01=2.0,
            bc02=2.0,
            bc03=2.0,
            bc13=2.0,
            controlling_rating=2.0,
            condition_class="Poor",
        ),
    )
    changed_future[0] = replace(
        changed_future[0],
        snapshots=changed_future[0].snapshots[:-1] + (changed_snapshot,),
    )
    first = build_annual_markov_model(original, through_year=2021)
    second = build_annual_markov_model(tuple(changed_future), through_year=2021)
    assert first.model_id == second.model_id
    assert first.source_receipt_sha256 == second.source_receipt_sha256


def test_hindcast_rows_bind_prediction_to_exact_future_ground_truth_revision():
    report = evaluate_historical_hindcasts(
        _fleet(),
        origins=(2021,),
        config=ForecastModelConfig(minimum_cohort_exposure=1),
    )
    rows = tuple(row for row in report.rows if row.component == "deck")
    assert rows
    assert {row.target_year for row in rows} == {2022}
    assert {row.lead_years for row in rows} == {1}
    assert all(row.target_revision_id.endswith("-2022") for row in rows)
    assert all(row.observed_rating is not None for row in rows)
    assert all(len(row.predicted_distribution) == 10 for row in rows)
    assert report.evaluation_receipt_id
    assert report.summary.target_rows == len(report.rows)
    assert report.summary.candidate_mae >= 0
    assert report.summary.persistence_mae >= 0


def test_hindcast_prediction_is_unchanged_when_only_future_ground_truth_changes():
    original = _fleet()
    changed = list(original)
    target = changed[1].snapshots[-1]
    changed[1] = replace(
        changed[1],
        snapshots=changed[1].snapshots[:-1]
        + (
            replace(
                target,
                condition=replace(
                    target.condition,
                    bc01=4.0,
                    bc02=4.0,
                    bc03=4.0,
                    bc13=4.0,
                    controlling_rating=4.0,
                    condition_class="Poor",
                ),
            ),
        ),
    )
    first = evaluate_historical_hindcasts(original, origins=(2021,))
    second = evaluate_historical_hindcasts(tuple(changed), origins=(2021,))
    first_row = next(row for row in first.rows if row.bridge_number == "B" and row.component == "deck")
    second_row = next(row for row in second.rows if row.bridge_number == "B" and row.component == "deck")
    assert first_row.model_id == second_row.model_id
    assert first_row.predicted_distribution == second_row.predicted_distribution
    assert first_row.observed_rating != second_row.observed_rating


def test_bridge_provider_rejects_unknown_treatment_effect_and_uses_reviewed_effect():
    history = _fleet()[0]
    model = build_annual_markov_model(_fleet(), through_year=2021)
    provider = BridgeAnnualForecastProvider(
        model=model,
        bridge_snapshot=history.snapshots[1],
        evaluation_receipt_id="evaluation-v1",
        qualification=ForecastQualification.RELEASED,
    )
    state = ComponentState.certain("deck", 7)
    with pytest.raises(ForecastInputError, match="treatment effect"):
        provider.transition(year=2022, component="deck", state=state, action_id="deck_seal")

    effect = TreatmentEffectModel.deterministic_gain(
        treatment_id="deck_seal",
        component="deck",
        gain=1,
        evidence_class="reviewed_engineer_assumption",
    )
    provider = BridgeAnnualForecastProvider(
        model=model,
        bridge_snapshot=history.snapshots[1],
        evaluation_receipt_id="evaluation-v1",
        qualification=ForecastQualification.RELEASED,
        treatment_effects=(effect,),
    )
    transition = provider.transition(
        year=2022,
        component="deck",
        state=state,
        action_id="deck_seal",
    )
    assert transition.state.most_likely_rating == 8
    assert transition.action_id == "deck_seal"


def test_forecast_package_is_immutable_idempotent_and_round_trips(tmp_path):
    database_path = tmp_path / "forecast.sqlite3"
    ensure_workspace(str(database_path), "workspace-a", "Workspace A")
    model = build_annual_markov_model(
        _fleet(),
        through_year=2021,
        config=ForecastModelConfig(minimum_cohort_exposure=1),
    )
    evaluation = evaluate_historical_hindcasts(
        _fleet(),
        origins=(2021,),
        config=ForecastModelConfig(minimum_cohort_exposure=1),
    )
    kwargs = {
        "model": model,
        "evaluation": evaluation,
        "projection_specification_sha256": ANALYTICAL_SPECIFICATION_SHA256,
        "created_by": "analyst@example.com",
        "created_at": "2026-08-14T12:00:00Z",
    }
    first = persist_forecast_package(database_path, **kwargs)
    second = persist_forecast_package(database_path, **kwargs)

    assert first == second
    restored = load_forecast_model(
        database_path,
        workspace_id="workspace-a",
        model_version_id=first.model_version_id,
    )
    assert restored.model_id == model.model_id
    assert serialize_annual_markov_model(restored) == serialize_annual_markov_model(model)

    import sqlite3

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM forecast_datasets").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM forecast_model_versions").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM forecast_evaluation_runs").fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM forecast_hindcast_predictions"
        ).fetchone() == (len(evaluation.rows),)
        rows_receipt = json.loads(
            connection.execute(
                "SELECT rows_json FROM forecast_evaluation_runs"
            ).fetchone()[0]
        )
        assert rows_receipt["format"] == "relational-hindcast-rows-v1"
        assert rows_receipt["row_count"] == len(evaluation.rows)
        assert len(rows_receipt["rows_sha256"]) == 64
    finally:
        connection.close()


def test_bridge_forecast_run_is_annual_revision_bound_and_idempotent(tmp_path):
    database_path = tmp_path / "forecast-run.sqlite3"
    ensure_workspace(str(database_path), "workspace-a", "Workspace A")
    history = _fleet()[0]
    snapshot = history.snapshots[-1]
    model = build_annual_markov_model(
        _fleet(),
        through_year=2021,
        config=ForecastModelConfig(minimum_cohort_exposure=1),
    )
    evaluation = evaluate_historical_hindcasts(
        _fleet(), origins=(2021,), config=ForecastModelConfig(minimum_cohort_exposure=1)
    )
    persist_forecast_package(
        database_path,
        model=model,
        evaluation=evaluation,
        projection_specification_sha256=ANALYTICAL_SPECIFICATION_SHA256,
        created_by="analyst@example.com",
        created_at="2026-08-14T12:00:00Z",
    )

    import json
    import sqlite3

    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "UPDATE workspace_state SET data_generation=1 WHERE workspace_id='workspace-a'"
        )
        connection.execute(
            "INSERT INTO snapshot_revisions "
            "(id,workspace_id,state_code,bridge_number,data_year,revision_number,"
            "lifecycle_state,canonical_json,canonical_sha256,profile_id,"
            "rule_bundle_sha256,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                snapshot.snapshot_revision_id,
                "workspace-a",
                34,
                "A",
                snapshot.data_year,
                1,
                "published",
                json.dumps({"BL01": 34, "BID01": "A"}),
                snapshot.canonical_sha256,
                "research",
                "d" * 64,
                "analyst@example.com",
                "2026-08-14T12:00:00Z",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    first = create_bridge_forecast_run(
        database_path,
        history=history,
        horizon_years=3,
        idempotency_key="forecast-A-3",
        created_by="analyst@example.com",
        created_at="2026-08-14T12:01:00Z",
        expected_data_generation=1,
    )
    second = create_bridge_forecast_run(
        database_path,
        history=history,
        horizon_years=3,
        idempotency_key="forecast-A-3",
        created_by="analyst@example.com",
        created_at="2026-08-14T12:01:00Z",
        expected_data_generation=1,
    )
    restored = load_bridge_forecast_run(
        database_path, workspace_id="workspace-a", run_id=first.run_id
    )

    assert first.run_id == second.run_id == restored.run_id
    assert first.snapshot_revision_id == snapshot.snapshot_revision_id
    assert {point.year for point in first.predictions} == {2023, 2024, 2025}
    assert len(first.predictions) == 9  # Three observed components across three annual steps.
    assert all(sum(point.post_action_distribution) == pytest.approx(1.0) for point in first.predictions)
    assert restored.predictions == first.predictions
    assert restored.observed == first.observed

    comparison = compare_bridge_lcca_strategies(
        database_path,
        history=history,
        strategies=("no_action", "full_replacement"),
        horizon_years=3,
        economics=EconomicAssumptions(
            mode=EconomicMode.CONSTANT_DOLLAR,
            base_year=snapshot.data_year,
            real_discount_rate=Decimal("0.025"),
        ),
        action_costs={"full_replacement": Decimal("1000000")},
        enable_research_effect_assumptions=True,
    )
    assert comparison.model_id == model.model_id
    assert [item.strategy_id for item in comparison.alternatives] == [
        "no_action",
        "full_replacement",
    ]
    replacement = comparison.alternatives[1]
    assert replacement.result.annual_ledger[0].year == snapshot.data_year
    assert replacement.result.annual_ledger[0].selected_action_ids == ("full_replacement",)
    assert replacement.result.total_present_value_cost == Decimal("1000000")

    scheduled = compare_bridge_lcca_strategies(
        database_path,
        history=history,
        strategies=("deck_and_substructure_rehabilitation", "full_replacement"),
        horizon_years=3,
        economics=EconomicAssumptions(
            mode=EconomicMode.CONSTANT_DOLLAR,
            base_year=snapshot.data_year,
            real_discount_rate=Decimal("0.025"),
        ),
        action_costs={
            "deck_replacement": Decimal("500000"),
            "substructure_rehabilitation": Decimal("250000"),
            "full_replacement": Decimal("1000000"),
        },
        rehabilitation_year=snapshot.data_year + 1,
        replacement_year=snapshot.data_year + 2,
        enable_research_effect_assumptions=True,
    )
    rehabilitation, future_replacement = scheduled.alternatives
    assert {action.year for action in rehabilitation.scheduled_actions} == {
        snapshot.data_year + 1
    }
    rehab_row = next(
        row
        for row in rehabilitation.result.annual_ledger
        if row.year == snapshot.data_year + 1
    )
    assert rehab_row.selected_action_ids == (
        "deck_replacement",
        "substructure_rehabilitation",
    )
    assert future_replacement.scheduled_actions[0].year == snapshot.data_year + 2
    replacement_row = next(
        row
        for row in future_replacement.result.annual_ledger
        if row.year == snapshot.data_year + 2
    )
    assert replacement_row.selected_action_ids == ("full_replacement",)
