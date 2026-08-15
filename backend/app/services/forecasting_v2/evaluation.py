"""Past-origin hindcasts scored against exact later published observations."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Iterable

from app.services.analytics.life_history import EvidenceClass
from app.services.analytics.projection import BridgeAnalysisHistory, BridgeAnalysisSnapshot
from app.services.forecasting_v2.estimator import (
    BridgeAnnualForecastProvider,
    ForecastInputError,
    ForecastModelConfig,
    build_annual_markov_model,
)
from app.services.forecasting_v2.models import ComponentState, ForecastQualification


_COMPONENT_ATTRIBUTES = {
    "deck": "bc01",
    "superstructure": "bc02",
    "substructure": "bc03",
    "culvert": "bc04",
}


@dataclass(frozen=True, slots=True)
class ForecastReleasePolicy:
    policy_id: str = "forecast-release-policy-v1"
    minimum_target_rows: int = 100
    minimum_mae_skill: float = 0.0
    maximum_candidate_mae: float = 1.5


@dataclass(frozen=True, slots=True)
class HindcastRow:
    workspace_id: str
    state_code: int
    bridge_number: str
    component: str
    origin_year: int
    origin_revision_id: str
    target_year: int
    target_revision_id: str
    lead_years: int
    model_id: str
    predicted_distribution: tuple[float, ...]
    predicted_rating: float
    observed_rating: float
    absolute_error: float
    signed_error: float
    persistence_rating: float
    persistence_absolute_error: float
    evidence_stratum: str

    def canonical_payload(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "state_code": self.state_code,
            "bridge_number": self.bridge_number,
            "component": self.component,
            "origin_year": self.origin_year,
            "origin_revision_id": self.origin_revision_id,
            "target_year": self.target_year,
            "target_revision_id": self.target_revision_id,
            "lead_years": self.lead_years,
            "model_id": self.model_id,
            "predicted_distribution": list(self.predicted_distribution),
            "predicted_rating": self.predicted_rating,
            "observed_rating": self.observed_rating,
            "absolute_error": self.absolute_error,
            "signed_error": self.signed_error,
            "persistence_rating": self.persistence_rating,
            "persistence_absolute_error": self.persistence_absolute_error,
            "evidence_stratum": self.evidence_stratum,
        }


@dataclass(frozen=True, slots=True)
class HindcastSummary:
    target_rows: int
    bridge_count: int
    candidate_mae: float
    persistence_mae: float
    mae_skill: float
    mean_bias: float


@dataclass(frozen=True, slots=True)
class HindcastReport:
    evaluation_receipt_id: str
    origins: tuple[int, ...]
    rows: tuple[HindcastRow, ...]
    summary: HindcastSummary
    release_policy_id: str
    release_qualified: bool


def _rating(snapshot: BridgeAnalysisSnapshot, component: str) -> float | None:
    value = getattr(snapshot.condition, _COMPONENT_ATTRIBUTES[component])
    if value is None or not math.isfinite(value) or not 0 <= value <= 9:
        return None
    return float(value)


def _evidence_stratum(
    history: BridgeAnalysisHistory,
    *,
    origin_year: int,
    target_year: int,
) -> str:
    interval = tuple(
        event
        for event in history.life_history.events
        if origin_year < event.event_year <= target_year
    )
    if any(event.evidence_class is EvidenceClass.OBSERVED_WORK for event in interval):
        return "observed_work"
    if any(
        event.evidence_class is EvidenceClass.INVENTORY_REPORTED_WORK
        for event in interval
    ):
        return "inventory_reported_work"
    if any(
        event.evidence_class is EvidenceClass.CONDITION_CHANGE_CANDIDATE
        for event in interval
    ):
        return "condition_change_candidate"
    return "no_recorded_work"


def _expected(distribution: tuple[float, ...]) -> float:
    return math.fsum(index * value for index, value in enumerate(distribution))


def evaluate_historical_hindcasts(
    histories: Iterable[BridgeAnalysisHistory],
    *,
    origins: Iterable[int],
    config: ForecastModelConfig = ForecastModelConfig(),
    release_policy: ForecastReleasePolicy = ForecastReleasePolicy(),
) -> HindcastReport:
    """Refit at every past origin and score only exact later observations."""

    history_tuple = tuple(histories)
    origin_tuple = tuple(sorted({int(origin) for origin in origins}))
    if not history_tuple or not origin_tuple:
        raise ForecastInputError("hindcast histories and origins are required")
    rows: list[HindcastRow] = []
    for origin in origin_tuple:
        model = build_annual_markov_model(
            history_tuple,
            through_year=origin,
            config=config,
        )
        for history in sorted(history_tuple, key=lambda item: item.bridge_number):
            available = tuple(snapshot for snapshot in history.snapshots if snapshot.data_year <= origin)
            targets = tuple(snapshot for snapshot in history.snapshots if snapshot.data_year > origin)
            if not available:
                continue
            origin_snapshot = available[-1]
            provider = BridgeAnnualForecastProvider(
                model=model,
                bridge_snapshot=origin_snapshot,
                evaluation_receipt_id="hindcast-pending",
                qualification=ForecastQualification.RESEARCH_UNVALIDATED,
            )
            for component in _COMPONENT_ATTRIBUTES:
                origin_rating = _rating(origin_snapshot, component)
                if origin_rating is None:
                    continue
                state = ComponentState.certain(component, int(round(origin_rating)))
                current_year = origin_snapshot.data_year
                for target in targets:
                    target_rating = _rating(target, component)
                    while current_year < target.data_year:
                        current_year += 1
                        transition = provider.transition(
                            year=current_year,
                            component=component,
                            state=state,
                            action_id=None,
                        )
                        state = transition.state
                    if target_rating is None:
                        continue
                    predicted = _expected(state.distribution)
                    signed_error = predicted - target_rating
                    persistence_error = abs(origin_rating - target_rating)
                    rows.append(
                        HindcastRow(
                            workspace_id=history.workspace_id,
                            state_code=history.state_code,
                            bridge_number=history.bridge_number,
                            component=component,
                            origin_year=origin,
                            origin_revision_id=origin_snapshot.snapshot_revision_id,
                            target_year=target.data_year,
                            target_revision_id=target.snapshot_revision_id,
                            lead_years=target.data_year - origin_snapshot.data_year,
                            model_id=model.model_id,
                            predicted_distribution=state.distribution,
                            predicted_rating=predicted,
                            observed_rating=target_rating,
                            absolute_error=abs(signed_error),
                            signed_error=signed_error,
                            persistence_rating=origin_rating,
                            persistence_absolute_error=persistence_error,
                            evidence_stratum=_evidence_stratum(
                                history,
                                origin_year=origin_snapshot.data_year,
                                target_year=target.data_year,
                            ),
                        )
                    )
    ordered_rows = tuple(
        sorted(
            rows,
            key=lambda row: (
                row.origin_year,
                row.workspace_id,
                row.state_code,
                row.bridge_number,
                row.component,
                row.target_year,
                row.target_revision_id,
            ),
        )
    )
    if not ordered_rows:
        raise ForecastInputError("hindcast produced no exact ground-truth rows")
    candidate_mae = math.fsum(row.absolute_error for row in ordered_rows) / len(ordered_rows)
    persistence_mae = (
        math.fsum(row.persistence_absolute_error for row in ordered_rows) / len(ordered_rows)
    )
    mae_skill = (
        (persistence_mae - candidate_mae) / persistence_mae
        if persistence_mae > 0
        else (0.0 if candidate_mae == 0 else -1.0)
    )
    summary = HindcastSummary(
        target_rows=len(ordered_rows),
        bridge_count=len({(row.workspace_id, row.state_code, row.bridge_number) for row in ordered_rows}),
        candidate_mae=candidate_mae,
        persistence_mae=persistence_mae,
        mae_skill=mae_skill,
        mean_bias=math.fsum(row.signed_error for row in ordered_rows) / len(ordered_rows),
    )
    receipt_payload = {
        "origins": list(origin_tuple),
        "config": config.canonical_payload(),
        "release_policy": {
            "policy_id": release_policy.policy_id,
            "minimum_target_rows": release_policy.minimum_target_rows,
            "minimum_mae_skill": release_policy.minimum_mae_skill,
            "maximum_candidate_mae": release_policy.maximum_candidate_mae,
        },
        "rows": [row.canonical_payload() for row in ordered_rows],
    }
    receipt = hashlib.sha256(
        json.dumps(
            receipt_payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    qualified = (
        summary.target_rows >= release_policy.minimum_target_rows
        and summary.mae_skill > release_policy.minimum_mae_skill
        and summary.candidate_mae <= release_policy.maximum_candidate_mae
    )
    return HindcastReport(
        evaluation_receipt_id=receipt,
        origins=origin_tuple,
        rows=ordered_rows,
        summary=summary,
        release_policy_id=release_policy.policy_id,
        release_qualified=qualified,
    )
