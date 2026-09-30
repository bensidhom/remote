"""Annual, action-aware Markov condition forecast fitted from published history."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from types import MappingProxyType
from typing import Iterable, Mapping

from app.services.analytics.life_history import EvidenceClass
from app.services.analytics.projection import BridgeAnalysisHistory, BridgeAnalysisSnapshot
from app.services.forecasting_v2.models import (
    ComponentState,
    ForecastQualification,
    ForecastTransition,
)


_COMPONENT_ATTRIBUTES = {
    "deck": "bc01",
    "superstructure": "bc02",
    "substructure": "bc03",
    "culvert": "bc04",
}


class ForecastInputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ForecastModelConfig:
    minimum_cohort_exposure: int = 25
    prior_stay_weight: float = 9.0
    prior_drop_one_weight: float = 1.0
    default_annual_drop_probability: float = 0.10
    maximum_annual_drop: int = 4
    version: str = "annual-markov-v2"

    def __post_init__(self) -> None:
        if int(self.minimum_cohort_exposure) < 1:
            raise ValueError("minimum_cohort_exposure must be positive")
        if self.prior_stay_weight < 0 or self.prior_drop_one_weight < 0:
            raise ValueError("prior weights must be non-negative")
        if not 0 <= self.default_annual_drop_probability <= 1:
            raise ValueError("default_annual_drop_probability must be between zero and one")
        if int(self.maximum_annual_drop) < 1 or int(self.maximum_annual_drop) > 9:
            raise ValueError("maximum_annual_drop must be between 1 and 9")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "minimum_cohort_exposure": self.minimum_cohort_exposure,
            "prior_stay_weight": self.prior_stay_weight,
            "prior_drop_one_weight": self.prior_drop_one_weight,
            "default_annual_drop_probability": self.default_annual_drop_probability,
            "maximum_annual_drop": self.maximum_annual_drop,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class ForecastTrainingStatistics:
    bridge_histories: int
    used_intervals: int
    excluded_work_intervals: int
    excluded_improvement_intervals: int
    component_transition_years: int
    trained_through_year: int


@dataclass(frozen=True, slots=True)
class TransitionEvidence:
    fallback_level: str
    exposure: int
    cohort_key: tuple[object, ...] | None
    smoothed: bool


def traffic_band(adtt: int | None, aadt: int | None) -> str | None:
    if adtt is not None:
        return "low" if adtt < 250 else "medium" if adtt < 1000 else "high"
    if aadt is not None:
        return "low" if aadt < 5000 else "medium" if aadt < 20000 else "high"
    return None


def _age_band(age: int | None) -> str | None:
    if age is None or age < 0:
        return None
    if age < 20:
        return "0-19"
    if age < 40:
        return "20-39"
    if age < 60:
        return "40-59"
    if age < 80:
        return "60-79"
    return "80+"


def _rating(snapshot: BridgeAnalysisSnapshot, component: str) -> int | None:
    value = getattr(snapshot.condition, _COMPONENT_ATTRIBUTES[component])
    if value is None or not math.isfinite(value) or not 0 <= value <= 9:
        return None
    return int(round(value))


def _slot_magnitudes(total_drop: int, years: int, maximum: int) -> tuple[int, ...]:
    if years <= 0:
        return ()
    bounded = min(maximum * years, max(0, total_drop))
    quotient, remainder = divmod(bounded, years)
    return tuple(quotient + 1 if index < remainder else quotient for index in range(years))


def _specific_keys(
    *,
    state_code: int,
    county_code: int | None,
    component: str,
    age_band: str | None,
    traffic: str | None,
) -> tuple[tuple[str, tuple[object, ...]], ...]:
    ordered: list[tuple[str, tuple[object, ...]]] = []
    if county_code is not None:
        ordered.extend(
            (
                ("county_age_traffic", (state_code, county_code, component, age_band, traffic)),
                ("county_age", (state_code, county_code, component, age_band, None)),
                ("county", (state_code, county_code, component, None, None)),
            )
        )
    ordered.extend(
        (
            ("state_age_traffic", (state_code, None, component, age_band, traffic)),
            ("state_age", (state_code, None, component, age_band, None)),
            ("state", (state_code, None, component, None, None)),
        )
    )
    unique: list[tuple[str, tuple[object, ...]]] = []
    seen: set[tuple[object, ...]] = set()
    for label, key in ordered:
        if key not in seen:
            unique.append((label, key))
            seen.add(key)
    return tuple(unique)


@dataclass(frozen=True, slots=True)
class AnnualMarkovModel:
    model_id: str
    source_receipt_sha256: str
    workspace_id: str
    state_code: int
    through_year: int
    config: ForecastModelConfig
    statistics: ForecastTrainingStatistics
    transition_counts: Mapping[tuple[object, ...], tuple[tuple[int, ...], ...]]

    def transition_distribution(
        self,
        *,
        component: str,
        rating: int,
        state_code: int,
        county_code: int | None,
        age: int | None,
        traffic_band: str | None,
    ) -> tuple[tuple[float, ...], TransitionEvidence]:
        normalized_component = str(component).strip().lower()
        if normalized_component not in _COMPONENT_ATTRIBUTES:
            raise ForecastInputError("unknown forecast component")
        current = int(rating)
        if current < 0 or current > 9:
            raise ForecastInputError("condition rating must be between 0 and 9")
        if int(state_code) != self.state_code:
            raise ForecastInputError("state is outside the fitted forecast model")
        if current == 0:
            return (
                (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                TransitionEvidence("absorbing", 0, None, False),
            )
        selected: tuple[int, ...] | None = None
        selected_key: tuple[object, ...] | None = None
        selected_label = "default"
        selected_exposure = 0
        fallback_cell: tuple[int, ...] | None = None
        fallback_key: tuple[object, ...] | None = None
        fallback_label = "default"
        fallback_exposure = 0
        for label, key in _specific_keys(
            state_code=state_code,
            county_code=county_code,
            component=normalized_component,
            age_band=_age_band(age),
            traffic=traffic_band,
        ):
            matrix = self.transition_counts.get(key)
            if matrix is None:
                continue
            counts = matrix[current]
            exposure = sum(counts)
            if exposure > fallback_exposure:
                fallback_cell, fallback_key, fallback_label, fallback_exposure = (
                    counts,
                    key,
                    label,
                    exposure,
                )
            if exposure >= self.config.minimum_cohort_exposure:
                selected, selected_key, selected_label, selected_exposure = (
                    counts,
                    key,
                    label,
                    exposure,
                )
                break
        if selected is None and fallback_cell is not None:
            selected, selected_key, selected_label, selected_exposure = (
                fallback_cell,
                fallback_key,
                f"thin_{fallback_label}",
                fallback_exposure,
            )
        if selected is None:
            drop_probability = self.config.default_annual_drop_probability
            distribution = [0.0] * 10
            distribution[current] = 1.0 - drop_probability
            distribution[max(0, current - 1)] += drop_probability
            return tuple(distribution), TransitionEvidence("default", 0, None, True)

        weights = [float(value) for value in selected]
        weights[current] += self.config.prior_stay_weight
        weights[max(0, current - 1)] += self.config.prior_drop_one_weight
        total = math.fsum(weights)
        distribution = tuple(value / total for value in weights)
        return distribution, TransitionEvidence(
            selected_label,
            selected_exposure,
            selected_key,
            bool(self.config.prior_stay_weight or self.config.prior_drop_one_weight),
        )


def _source_receipt(
    histories: tuple[BridgeAnalysisHistory, ...],
    *,
    through_year: int,
) -> str:
    entries = []
    for history in sorted(histories, key=lambda item: (item.workspace_id, item.state_code, item.bridge_number)):
        for snapshot in history.snapshots:
            if snapshot.data_year <= through_year:
                entries.append(
                    (
                        history.workspace_id,
                        history.state_code,
                        history.bridge_number,
                        snapshot.data_year,
                        snapshot.snapshot_revision_id,
                        snapshot.canonical_sha256,
                    )
                )
    return hashlib.sha256(
        json.dumps(entries, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_annual_markov_model(
    histories: Iterable[BridgeAnalysisHistory],
    *,
    through_year: int,
    config: ForecastModelConfig = ForecastModelConfig(),
) -> AnnualMarkovModel:
    history_tuple = tuple(histories)
    if not history_tuple:
        raise ForecastInputError("at least one bridge history is required")
    workspaces = {history.workspace_id for history in history_tuple}
    states = {history.state_code for history in history_tuple}
    if len(workspaces) != 1 or len(states) != 1:
        raise ForecastInputError("forecast training requires one workspace and state")
    cutoff = int(through_year)
    counts: dict[tuple[object, ...], list[list[int]]] = {}
    used_intervals = 0
    excluded_work = 0
    excluded_improvement = 0
    component_years = 0

    for history in history_tuple:
        snapshots = tuple(snapshot for snapshot in history.snapshots if snapshot.data_year <= cutoff)
        for previous, current in zip(snapshots, snapshots[1:]):
            years = current.data_year - previous.data_year
            if years <= 0:
                continue
            interval_events = tuple(
                event
                for event in history.life_history.events
                if previous.data_year < event.event_year <= current.data_year
            )
            has_work = any(
                event.evidence_class
                in {EvidenceClass.OBSERVED_WORK, EvidenceClass.INVENTORY_REPORTED_WORK}
                for event in interval_events
            )
            ratings = {
                component: (_rating(previous, component), _rating(current, component))
                for component in _COMPONENT_ATTRIBUTES
            }
            has_improvement = any(
                before is not None and after is not None and after > before
                for before, after in ratings.values()
            )
            if has_work:
                excluded_work += 1
            if has_improvement:
                excluded_improvement += 1
            if has_work or has_improvement:
                continue
            used_intervals += 1
            selected_feature = previous.carried_features.selected
            band = traffic_band(
                selected_feature.adtt if selected_feature else None,
                selected_feature.aadt if selected_feature else None,
            )
            for component, (before, after) in ratings.items():
                if before is None or after is None or after > before:
                    continue
                magnitudes = _slot_magnitudes(
                    before - after,
                    years,
                    config.maximum_annual_drop,
                )
                running_state = before
                for offset, magnitude in enumerate(magnitudes):
                    target = max(0, running_state - magnitude)
                    age = (
                        previous.age_at_snapshot + offset
                        if previous.age_at_snapshot is not None
                        else None
                    )
                    for _label, key in _specific_keys(
                        state_code=history.state_code,
                        county_code=previous.county_code,
                        component=component,
                        age_band=_age_band(age),
                        traffic=band,
                    ):
                        matrix = counts.setdefault(key, [[0] * 10 for _ in range(10)])
                        matrix[running_state][target] += 1
                    running_state = target
                    component_years += 1

    frozen_counts = MappingProxyType(
        {
            key: tuple(tuple(row) for row in matrix)
            for key, matrix in sorted(counts.items(), key=lambda item: repr(item[0]))
        }
    )
    receipt = _source_receipt(history_tuple, through_year=cutoff)
    model_payload = {
        "source_receipt_sha256": receipt,
        "through_year": cutoff,
        "workspace_id": next(iter(workspaces)),
        "state_code": next(iter(states)),
        "config": config.canonical_payload(),
        "counts": [
            [list(key), [list(row) for row in matrix]]
            for key, matrix in frozen_counts.items()
        ],
    }
    model_id = hashlib.sha256(
        json.dumps(
            model_payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return AnnualMarkovModel(
        model_id=model_id,
        source_receipt_sha256=receipt,
        workspace_id=next(iter(workspaces)),
        state_code=next(iter(states)),
        through_year=cutoff,
        config=config,
        statistics=ForecastTrainingStatistics(
            bridge_histories=len(history_tuple),
            used_intervals=used_intervals,
            excluded_work_intervals=excluded_work,
            excluded_improvement_intervals=excluded_improvement,
            component_transition_years=component_years,
            trained_through_year=cutoff,
        ),
        transition_counts=frozen_counts,
    )


@dataclass(frozen=True, slots=True)
class TreatmentEffectModel:
    treatment_id: str
    component: str
    transition_matrix: tuple[tuple[float, ...], ...]
    evidence_class: str
    duration_years: int = 0
    deterioration_reduction: float = 0.0

    @classmethod
    def deterministic_gain(
        cls,
        *,
        treatment_id: str,
        component: str,
        gain: int,
        evidence_class: str,
    ) -> "TreatmentEffectModel":
        amount = int(gain)
        if amount < 0 or amount > 9:
            raise ValueError("treatment gain must be between 0 and 9")
        matrix = tuple(
            tuple(1.0 if target == min(9, source + amount) else 0.0 for target in range(10))
            for source in range(10)
        )
        return cls(
            treatment_id=str(treatment_id).strip().lower(),
            component=str(component).strip().lower(),
            transition_matrix=matrix,
            evidence_class=str(evidence_class).strip(),
        )

    @classmethod
    def protective(
        cls,
        *,
        treatment_id: str,
        component: str,
        duration_years: int,
        deterioration_reduction: float,
        evidence_class: str,
    ) -> "TreatmentEffectModel":
        return cls(
            treatment_id=str(treatment_id).strip().lower(),
            component=str(component).strip().lower(),
            transition_matrix=tuple(
                tuple(1.0 if target == source else 0.0 for target in range(10))
                for source in range(10)
            ),
            evidence_class=str(evidence_class).strip(),
            duration_years=int(duration_years),
            deterioration_reduction=float(deterioration_reduction),
        )

    def __post_init__(self) -> None:
        if not self.treatment_id or not self.component or not self.evidence_class:
            raise ValueError("treatment effect identity and evidence are required")
        if int(self.duration_years) < 0:
            raise ValueError("treatment effect duration cannot be negative")
        if not 0 <= float(self.deterioration_reduction) <= 1:
            raise ValueError("deterioration reduction must be between zero and one")
        if len(self.transition_matrix) != 10:
            raise ValueError("treatment transition matrix must have ten rows")
        for row in self.transition_matrix:
            if len(row) != 10 or any(value < 0 or not math.isfinite(value) for value in row):
                raise ValueError("invalid treatment transition matrix")
            if not math.isclose(math.fsum(row), 1.0, abs_tol=1e-9):
                raise ValueError("treatment transition rows must sum to one")

    def apply(self, state: ComponentState) -> tuple[float, ...]:
        result = [0.0] * 10
        for source, weight in enumerate(state.distribution):
            for target, probability in enumerate(self.transition_matrix[source]):
                result[target] += weight * probability
        return tuple(result)

    def protects(self, *, action_year: int, forecast_year: int) -> bool:
        return (
            self.duration_years > 0
            and int(action_year) < int(forecast_year)
            and int(forecast_year) <= int(action_year) + self.duration_years
        )


class BridgeAnnualForecastProvider:
    def __init__(
        self,
        *,
        model: AnnualMarkovModel,
        bridge_snapshot: BridgeAnalysisSnapshot,
        evaluation_receipt_id: str,
        qualification: ForecastQualification,
        treatment_effects: Iterable[TreatmentEffectModel] = (),
    ) -> None:
        if bridge_snapshot.identity.workspace_id != model.workspace_id:
            raise ForecastInputError("bridge workspace does not match forecast model")
        if bridge_snapshot.state_code != model.state_code:
            raise ForecastInputError("bridge state does not match forecast model")
        self.model = model
        self.bridge_snapshot = bridge_snapshot
        self.model_id = model.model_id
        self.evaluation_receipt_id = str(evaluation_receipt_id).strip()
        self.qualification = qualification
        effects = tuple(treatment_effects)
        self._effects = {(item.treatment_id, item.component): item for item in effects}
        if len(self._effects) != len(effects):
            raise ForecastInputError("duplicate treatment effect models")

    def transition(
        self,
        *,
        year: int,
        component: str,
        state: ComponentState,
        action_id: str | None,
    ) -> ForecastTransition:
        normalized_component = str(component).strip().lower()
        if normalized_component != state.component:
            raise ForecastInputError("component state mismatch")
        if int(year) < self.bridge_snapshot.data_year or (
            int(year) == self.bridge_snapshot.data_year and action_id is None
        ):
            raise ForecastInputError(
                "no-action forecast transitions must be after the as-of year; "
                "only an explicit initial action may occur at the as-of year"
            )
        if not self.evaluation_receipt_id:
            raise ForecastInputError("forecast evaluation receipt is required")
        if action_id is not None:
            key = (str(action_id).strip().lower(), normalized_component)
            effect = self._effects.get(key)
            if effect is None:
                raise ForecastInputError("treatment effect is unavailable")
            distribution = effect.apply(state)
            fallback = f"treatment:{effect.evidence_class}"
        else:
            selected_feature = self.bridge_snapshot.carried_features.selected
            band = traffic_band(
                selected_feature.adtt if selected_feature else None,
                selected_feature.aadt if selected_feature else None,
            )
            age = (
                int(year) - self.bridge_snapshot.year_built
                if self.bridge_snapshot.year_built is not None
                else None
            )
            distribution_buffer = [0.0] * 10
            evidence_levels: set[str] = set()
            for rating, weight in enumerate(state.distribution):
                if weight <= 0:
                    continue
                row, evidence = self.model.transition_distribution(
                    component=normalized_component,
                    rating=rating,
                    state_code=self.bridge_snapshot.state_code,
                    county_code=self.bridge_snapshot.county_code,
                    age=age,
                    traffic_band=band,
                )
                evidence_levels.add(evidence.fallback_level)
                for target, probability in enumerate(row):
                    distribution_buffer[target] += weight * probability
            distribution = tuple(distribution_buffer)
            fallback = "+".join(sorted(evidence_levels)) or "default"
        return ForecastTransition(
            component=normalized_component,
            year=int(year),
            action_id=action_id,
            distribution=distribution,
            model_id=self.model_id,
            evaluation_receipt_id=self.evaluation_receipt_id,
            fallback_level=fallback,
        )

    def transition_with_active_treatments(
        self,
        *,
        year: int,
        component: str,
        state: ComponentState,
        active_actions: Iterable[tuple[str, int]],
    ) -> ForecastTransition:
        """Apply preservation as a temporary reduction in deterioration mass.

        The baseline Markov model remains unchanged. Protective actions blend
        part of the one-year deterioration distribution back toward the
        pre-transition state for their documented duration.
        """

        baseline = self.transition(
            year=year,
            component=component,
            state=state,
            action_id=None,
        )
        reductions: list[float] = []
        evidence: list[str] = []
        for action_id, action_year in active_actions:
            effect = self._effects.get((str(action_id).strip().lower(), component))
            if effect is None or not effect.protects(
                action_year=int(action_year), forecast_year=int(year)
            ):
                continue
            reductions.append(effect.deterioration_reduction)
            evidence.append(effect.evidence_class)
        if not reductions:
            return baseline
        retained_share = 1.0
        for reduction in reductions:
            retained_share *= 1.0 - reduction
        combined_reduction = 1.0 - retained_share
        distribution = tuple(
            (1.0 - combined_reduction) * baseline.distribution[index]
            + combined_reduction * state.distribution[index]
            for index in range(10)
        )
        return ForecastTransition(
            component=baseline.component,
            year=baseline.year,
            action_id=None,
            distribution=distribution,
            model_id=baseline.model_id,
            evaluation_receipt_id=baseline.evaluation_receipt_id,
            fallback_level=(
                baseline.fallback_level
                + "+protected:"
                + "+".join(sorted(set(evidence)))
            ),
        )
