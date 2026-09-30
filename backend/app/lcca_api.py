"""Exact-revision, forecast-driven annual LCCA API."""

from __future__ import annotations

from decimal import Decimal
import os
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import revision_api
from .services.analytics.projection import AnalyticalProjectionError, load_bridge_analysis_history
from .services.analytics.treatments import DEFAULT_BRIDGE_TREATMENT_LIBRARY
from .services.forecasting_v2.store import ForecastStoreError
from .services.lcca_v2.economics import EconomicAssumptions, EconomicMode
from .services.lcca_v2.scenario import (
    ExplicitAction,
    SUPPORTED_STRATEGIES,
    BridgeLccaComparison,
    compare_bridge_lcca_strategies,
)
from .services.lcca_v2.simulation import SimulationInputError


router = APIRouter(tags=["lcca-v2"])


class ExplicitActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: int = Field(ge=1900, le=2200)
    treatment_id: str = Field(min_length=1, max_length=128)


class LccaSimulationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state_code: int = Field(default=34, ge=1, le=99)
    bridge_number: str = Field(min_length=1, max_length=64)
    as_of_year: int | None = Field(default=None, ge=1900, le=2100)
    expected_data_generation: int = Field(ge=0)
    horizon_years: int = Field(default=100, ge=1, le=100)
    strategies: list[str] = Field(min_length=1, max_length=len(SUPPORTED_STRATEGIES))
    economic_mode: Literal["constant_dollar", "nominal_dollar"] = "constant_dollar"
    real_discount_rate: Decimal = Field(default=Decimal("0.025"), gt=Decimal("-1"), le=Decimal("1"))
    nominal_discount_rate: Decimal | None = Field(default=None, gt=Decimal("-1"), le=Decimal("1"))
    inflation_rate: Decimal | None = Field(default=None, gt=Decimal("-1"), le=Decimal("1"))
    condition_threshold: float = Field(default=4.5, ge=0, le=9)
    minimum_service_rating: float = Field(default=3.0, ge=0, le=9)
    rehabilitation_year: int | None = Field(default=None, ge=1900, le=2200)
    replacement_year: int | None = Field(default=None, ge=1900, le=2200)
    action_costs: dict[str, Decimal] = Field(default_factory=dict)
    explicit_actions: list[ExplicitActionBody] = Field(default_factory=list, max_length=200)
    enable_effect_assumptions: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "enable_effect_assumptions",
            "enable_research_effect_assumptions",
        ),
    )


def _released_forecast_required() -> bool:
    return (
        os.getenv("BRIDGEGUARD_DEPLOYMENT_MODE", "research_shared").strip().lower()
        == "dedicated_customer"
        or os.getenv("BRIDGEGUARD_REQUIRE_COMMERCIAL_READY", "0").strip().lower()
        in {"1", "true", "yes", "on"}
    )


def _state_payload(state) -> dict[str, object]:
    return {
        "component": state.component,
        "expected_rating": state.expected_rating,
        "most_likely_rating": state.most_likely_rating,
        "probabilities": list(state.distribution),
    }


def _comparison_payload(comparison: BridgeLccaComparison, history) -> dict[str, object]:
    facts = comparison.facts
    return {
        "scenario_receipt_sha256": comparison.scenario_receipt_sha256,
        "status": "complete",
        "identity": {
            "state_code": facts.state_code,
            "bridge_number": facts.bridge_number,
            "data_year": facts.data_year,
            "snapshot_revision_id": facts.snapshot_revision_id,
            "canonical_sha256": facts.canonical_sha256,
        },
        "facts": {
            "bridge_name": facts.bridge_name,
            "age_at_snapshot": facts.age_at_snapshot,
            "component_ratings": dict(facts.component_ratings),
            "controlling_rating": facts.controlling_rating,
            "controlling_basis": facts.controlling_basis,
            "deck_area": facts.geometry.deck_area,
            "deck_area_method": facts.geometry.method,
            "deck_area_status": facts.geometry.status,
            "nhs": facts.nhs.value,
            "nhs_status": facts.nhs.status,
            "nhs_source": facts.nhs.source,
            "deck_material_codes": list(facts.deck_material_codes),
            "span_material_codes": list(facts.span_material_codes),
            "substructure_material_codes": list(facts.substructure_material_codes),
        },
        "forecast": {
            "model_version_id": comparison.model_version_id,
            "model_id": comparison.model_id,
            "evaluation_run_id": comparison.evaluation_run_id,
            "evaluation_receipt_id": comparison.evaluation_receipt_id,
        },
        "analysis": {
            "period_status": comparison.analysis_period_status,
            "minimum_comparable_years": 25,
            "cost_scope": comparison.cost_scope,
            "excluded_costs": [
                "work_zone_user_delay",
                "vehicle_operating_costs",
                "crash_costs",
            ],
            "effect_assumptions": {
                "preservation_deterioration_reduction": "0.20",
                "duration_basis": "published treatment cycle",
                "editable_in_future": True,
            },
            "condition_policy": {
                "rehabilitation_trigger": comparison.rehabilitation_trigger,
                "minimum_service_rating": comparison.minimum_service_rating,
            },
        },
        "cost_assumptions": {
            "library_id": comparison.cost_library_id,
            "version": comparison.cost_library_version,
            "resolved_action_costs": {
                key: str(value)
                for key, value in sorted(comparison.resolved_action_costs.items())
            },
            "preservation_cost_interpretation": "generic_project_package",
        },
        "treatment_library": {
            "library_id": comparison.treatment_library_id,
            "sha256": comparison.treatment_library_sha256,
            "effect_evidence": comparison.effect_evidence,
            "source_title": DEFAULT_BRIDGE_TREATMENT_LIBRARY.source_title,
            "treatments": [
                {
                    **item.canonical_payload(),
                    "source_url": (
                        "https://www.nj.gov/transportation/about/asset/pdf/NJ_2019_TAMP_FHWA.pdf"
                        if "NJDOT" in item.source_reference
                        else "https://www.fhwa.dot.gov/bridge/preservation/guide/guide.pdf"
                    ),
                }
                for item in DEFAULT_BRIDGE_TREATMENT_LIBRARY.treatments
            ],
        },
        "life_history": {
            "construction_year": history.life_history.construction_year,
            "unknown_history_interval": history.life_history.unknown_history_interval,
            "events": [
                {
                    "event_id": event.event_id,
                    "event_year": event.event_year,
                    "component": event.component,
                    "treatment_id": event.treatment_id,
                    "evidence_class": event.evidence_class.value,
                    "source_record_ids": list(event.source_record_ids),
                    "before_rating": event.before_rating,
                    "after_rating": event.after_rating,
                }
                for event in history.life_history.events
            ],
        },
        "alternatives": [
            {
                "strategy_id": alternative.strategy_id,
                "feasibility_status": alternative.feasibility_status,
                "feasibility_reasons": list(alternative.feasibility_reasons),
                "rankable": alternative.rankable,
                "cost_complete": alternative.cost_complete,
                "minimum_controlling_rating": alternative.minimum_controlling_rating,
                "meets_service_threshold": alternative.meets_service_threshold,
                "sensitivity": list(alternative.sensitivity),
                "gross_present_value_cost": str(alternative.result.gross_present_value_cost),
                "remaining_service_life_credit": str(alternative.result.remaining_service_life_credit),
                "total_present_value_cost": str(alternative.result.total_present_value_cost),
                "equivalent_annual_cost": str(alternative.result.equivalent_annual_cost),
                "economic_mode": alternative.result.economic_mode,
                "scheduled_actions": [
                    {
                        "year": action.year,
                        "treatment_id": action.treatment_id,
                        "present_cost": str(action.present_cost),
                        "quantity": str(action.quantity),
                        "cost_unit": action.cost_unit,
                        "cost_status": action.cost_status,
                        "cost_source": action.cost_source,
                        "included_in": action.included_in,
                        "service_life_years": action.service_life_years,
                    }
                    for action in alternative.scheduled_actions
                ],
                "applicability_issues": list(alternative.applicability_issues),
                "annual_ledger": [
                    {
                        "year": row.year,
                        "pre_action_states": [_state_payload(state) for state in row.pre_action_states],
                        "post_action_states": [_state_payload(state) for state in row.post_action_states],
                        "selected_action_ids": list(row.selected_action_ids),
                        "base_cost": str(row.base_cost),
                        "future_cost": str(row.future_cost),
                        "inflation_factor": str(row.inflation_factor),
                        "discount_factor": str(row.discount_factor),
                        "present_value_cost": str(row.present_value_cost),
                    }
                    for row in alternative.result.annual_ledger
                ],
            }
            for alternative in comparison.alternatives
        ],
    }


@router.post("/api/lcca/v2/bridge")
async def simulate_bridge_lcca(body: LccaSimulationBody):
    try:
        history = await run_in_threadpool(
            load_bridge_analysis_history,
            database_path=revision_api._database_path(),
            workspace_id=revision_api.SHARED_WORKSPACE_ID,
            state_code=body.state_code,
            bridge_number=body.bridge_number,
            through_year=body.as_of_year,
        )
        if history.snapshots[-1].identity.data_generation != body.expected_data_generation:
            raise SimulationInputError("inventory data changed; refresh and retry")
        mode = EconomicMode(body.economic_mode)
        economics = EconomicAssumptions(
            mode=mode,
            base_year=history.snapshots[-1].data_year,
            real_discount_rate=body.real_discount_rate,
            nominal_discount_rate=body.nominal_discount_rate,
            inflation_rate=body.inflation_rate,
        )
        costs = {str(key).strip().lower(): Decimal(value) for key, value in body.action_costs.items()}
        if any(not value.is_finite() or value < 0 for value in costs.values()):
            raise SimulationInputError("action costs must be finite and non-negative")
        comparison = await run_in_threadpool(
            compare_bridge_lcca_strategies,
            revision_api._database_path(),
            history=history,
            strategies=body.strategies,
            horizon_years=body.horizon_years,
            economics=economics,
            action_costs=costs,
            explicit_actions=tuple(
                ExplicitAction(year=item.year, treatment_id=item.treatment_id)
                for item in body.explicit_actions
            ),
            condition_threshold=body.condition_threshold,
            minimum_service_rating=body.minimum_service_rating,
            rehabilitation_year=body.rehabilitation_year,
            replacement_year=body.replacement_year,
            enable_effect_assumptions=body.enable_effect_assumptions,
            require_released_forecast=_released_forecast_required(),
        )
        return JSONResponse(
            content=_comparison_payload(comparison, history),
            headers={"Cache-Control": "no-store"},
        )
    except AnalyticalProjectionError as exc:
        return JSONResponse(
            status_code=404,
            content={"detail": {"code": "lcca_history_not_found", "message": str(exc)}},
            headers={"Cache-Control": "no-store"},
        )
    except ForecastStoreError as exc:
        return JSONResponse(
            status_code=exc.status,
            content={"detail": {"code": exc.code, "message": str(exc)}},
            headers={"Cache-Control": "no-store"},
        )
    except (SimulationInputError, ValueError, KeyError) as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": "lcca_request_invalid", "message": str(exc)}},
            headers={"Cache-Control": "no-store"},
        )
