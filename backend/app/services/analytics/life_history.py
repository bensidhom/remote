"""Typed bridge-life history that never equates inferred and observed work."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class EvidenceClass(str, Enum):
    INVENTORY_OBSERVATION = "inventory_observation"
    OBSERVED_WORK = "observed_work"
    INVENTORY_REPORTED_WORK = "inventory_reported_work"
    CONDITION_CHANGE_CANDIDATE = "condition_change_candidate"
    SIMULATED_WORK = "simulated_work"


@dataclass(frozen=True, slots=True)
class LifeHistoryEvent:
    event_id: str
    event_year: int
    component: str
    treatment_id: str | None
    evidence_class: EvidenceClass
    source_record_ids: tuple[str, ...] = ()
    before_rating: int | None = None
    after_rating: int | None = None

    def __post_init__(self) -> None:
        event_id = str(self.event_id).strip()
        component = str(self.component).strip().lower()
        if not event_id:
            raise ValueError("event_id is required")
        if not component:
            raise ValueError("component is required")
        if int(self.event_year) < 1800 or int(self.event_year) > 2300:
            raise ValueError("event_year is outside the supported range")
        sources = tuple(str(value).strip() for value in self.source_record_ids)
        if any(not value for value in sources) or len(set(sources)) != len(sources):
            raise ValueError("source_record_ids must be unique nonblank values")
        if self.evidence_class in {
            EvidenceClass.INVENTORY_OBSERVATION,
            EvidenceClass.OBSERVED_WORK,
            EvidenceClass.INVENTORY_REPORTED_WORK,
            EvidenceClass.CONDITION_CHANGE_CANDIDATE,
        } and not sources:
            raise ValueError("source_record_ids are required for evidentiary events")
        if self.evidence_class is EvidenceClass.SIMULATED_WORK and sources:
            raise ValueError("simulated work cannot claim source_record_ids")
        for label, rating in (
            ("before_rating", self.before_rating),
            ("after_rating", self.after_rating),
        ):
            if rating is not None and (int(rating) < 0 or int(rating) > 9):
                raise ValueError(f"{label} must be between 0 and 9")
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "event_year", int(self.event_year))
        object.__setattr__(self, "component", component)
        object.__setattr__(self, "source_record_ids", sources)


@dataclass(frozen=True, slots=True)
class BridgeLifeHistory:
    bridge_id: str
    construction_year: int
    latest_observed_year: int
    events: tuple[LifeHistoryEvent, ...]
    unknown_history_interval: tuple[int, int] | None

    @classmethod
    def build(
        cls,
        *,
        bridge_id: str,
        construction_year: int,
        latest_observed_year: int,
        events: Iterable[LifeHistoryEvent],
    ) -> "BridgeLifeHistory":
        normalized_bridge_id = str(bridge_id).strip()
        if not normalized_bridge_id:
            raise ValueError("bridge_id is required")
        construction = int(construction_year)
        latest = int(latest_observed_year)
        if construction > latest:
            raise ValueError("construction_year cannot be after latest_observed_year")
        ordered = tuple(sorted(tuple(events), key=lambda event: (event.event_year, event.event_id)))
        if len({event.event_id for event in ordered}) != len(ordered):
            raise ValueError("event_id values must be unique")
        for event in ordered:
            if event.event_year < construction:
                raise ValueError("events cannot predate construction")
            if (
                event.evidence_class is not EvidenceClass.SIMULATED_WORK
                and event.event_year > latest
            ):
                raise ValueError("evidentiary events cannot be after latest_observed_year")
            if (
                event.evidence_class is EvidenceClass.SIMULATED_WORK
                and event.event_year <= latest
            ):
                raise ValueError("simulated work must be after latest_observed_year")
        evidentiary = tuple(
            event
            for event in ordered
            if event.evidence_class is not EvidenceClass.SIMULATED_WORK
        )
        if evidentiary and evidentiary[0].event_year > construction:
            unknown: tuple[int, int] | None = (
                construction,
                evidentiary[0].event_year - 1,
            )
        elif not evidentiary:
            unknown = (construction, latest)
        else:
            unknown = None
        return cls(
            bridge_id=normalized_bridge_id,
            construction_year=construction,
            latest_observed_year=latest,
            events=ordered,
            unknown_history_interval=unknown,
        )

    @property
    def observed_events(self) -> tuple[LifeHistoryEvent, ...]:
        return tuple(
            event
            for event in self.events
            if event.evidence_class is EvidenceClass.OBSERVED_WORK
        )

    @property
    def inspection_events(self) -> tuple[LifeHistoryEvent, ...]:
        return tuple(
            event
            for event in self.events
            if event.evidence_class is EvidenceClass.INVENTORY_OBSERVATION
        )

    @property
    def inventory_reported_events(self) -> tuple[LifeHistoryEvent, ...]:
        return tuple(
            event
            for event in self.events
            if event.evidence_class is EvidenceClass.INVENTORY_REPORTED_WORK
        )

    @property
    def condition_change_candidates(self) -> tuple[LifeHistoryEvent, ...]:
        return tuple(
            event
            for event in self.events
            if event.evidence_class is EvidenceClass.CONDITION_CHANGE_CANDIDATE
        )

    @property
    def simulated_events(self) -> tuple[LifeHistoryEvent, ...]:
        return tuple(
            event
            for event in self.events
            if event.evidence_class is EvidenceClass.SIMULATED_WORK
        )
