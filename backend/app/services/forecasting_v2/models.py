"""Immutable annual forecast inputs and outputs shared with LCCA."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Protocol, runtime_checkable


_PROBABILITY_TOLERANCE = 1e-9


def _validated_distribution(values: tuple[float, ...] | tuple[int, ...]) -> tuple[float, ...]:
    distribution = tuple(float(value) for value in values)
    if len(distribution) != 10:
        raise ValueError("condition distribution must contain ratings 0 through 9")
    if any(not math.isfinite(value) or value < 0.0 for value in distribution):
        raise ValueError("condition probabilities must be finite and non-negative")
    total = math.fsum(distribution)
    if abs(total - 1.0) > _PROBABILITY_TOLERANCE:
        raise ValueError("condition probabilities must sum to one")
    return distribution


class ForecastQualification(str, Enum):
    RESEARCH_UNVALIDATED = "research_unvalidated"
    RELEASED = "released"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class ComponentState:
    component: str
    distribution: tuple[float, ...]

    def __post_init__(self) -> None:
        component = str(self.component).strip().lower()
        if not component:
            raise ValueError("component is required")
        object.__setattr__(self, "component", component)
        object.__setattr__(self, "distribution", _validated_distribution(self.distribution))

    @classmethod
    def certain(cls, component: str, rating: int) -> "ComponentState":
        normalized = int(rating)
        if normalized < 0 or normalized > 9:
            raise ValueError("rating must be between 0 and 9")
        return cls(
            component=component,
            distribution=tuple(1.0 if index == normalized else 0.0 for index in range(10)),
        )

    @property
    def expected_rating(self) -> float:
        return math.fsum(index * value for index, value in enumerate(self.distribution))

    @property
    def most_likely_rating(self) -> int:
        return max(range(10), key=lambda index: (self.distribution[index], index))


@dataclass(frozen=True, slots=True)
class ForecastTransition:
    component: str
    year: int
    action_id: str | None
    distribution: tuple[float, ...]
    model_id: str
    evaluation_receipt_id: str
    fallback_level: str

    def __post_init__(self) -> None:
        component = str(self.component).strip().lower()
        model_id = str(self.model_id).strip()
        receipt = str(self.evaluation_receipt_id).strip()
        fallback = str(self.fallback_level).strip()
        if not component or not model_id or not receipt or not fallback:
            raise ValueError(
                "component, model_id, evaluation_receipt_id, and fallback_level are required"
            )
        if int(self.year) < 1900 or int(self.year) > 2300:
            raise ValueError("transition year is outside the supported range")
        action = str(self.action_id).strip().lower() if self.action_id is not None else None
        if action == "":
            action = None
        object.__setattr__(self, "component", component)
        object.__setattr__(self, "year", int(self.year))
        object.__setattr__(self, "action_id", action)
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "evaluation_receipt_id", receipt)
        object.__setattr__(self, "fallback_level", fallback)
        object.__setattr__(self, "distribution", _validated_distribution(self.distribution))

    @property
    def state(self) -> ComponentState:
        return ComponentState(self.component, self.distribution)


@runtime_checkable
class ForecastTransitionProvider(Protocol):
    model_id: str
    evaluation_receipt_id: str
    qualification: ForecastQualification

    def transition(
        self,
        *,
        year: int,
        component: str,
        state: ComponentState,
        action_id: str | None,
    ) -> ForecastTransition: ...
