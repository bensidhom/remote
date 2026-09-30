"""Versioned jurisdiction-specific planning cost inputs."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
from types import MappingProxyType
from typing import Mapping

from app.services.conversion.canonical_json import canonical_json_text


@dataclass(frozen=True, slots=True)
class ActionCost:
    action_id: str
    unit: str
    unit_cost: Decimal
    currency: str
    real_base_year: int
    jurisdiction: str
    source_reference: str
    review_status: str

    def __post_init__(self) -> None:
        cost = Decimal(self.unit_cost)
        if not cost.is_finite() or cost < 0:
            raise ValueError("unit cost must be finite and non-negative")
        for field in (self.action_id, self.unit, self.currency, self.jurisdiction, self.source_reference, self.review_status):
            if not str(field).strip():
                raise ValueError("cost identity and provenance are required")
        object.__setattr__(self, "action_id", self.action_id.strip().lower())
        object.__setattr__(self, "unit_cost", cost)


@dataclass(frozen=True, slots=True)
class CostLibrary:
    library_id: str
    version: str
    costs: tuple[ActionCost, ...]

    def __post_init__(self) -> None:
        keys = tuple(item.action_id for item in self.costs)
        if not self.library_id.strip() or not self.version.strip() or len(keys) != len(set(keys)):
            raise ValueError("cost library identity must be nonempty and action IDs unique")

    @property
    def by_action(self) -> Mapping[str, ActionCost]:
        return MappingProxyType({item.action_id: item for item in self.costs})

    @property
    def sha256(self) -> str:
        return hashlib.sha256(
            canonical_json_text(
                {
                    "library_id": self.library_id,
                    "version": self.version,
                    "costs": [
                        {
                            "action_id": item.action_id,
                            "unit": item.unit,
                            "unit_cost": str(item.unit_cost),
                            "currency": item.currency,
                            "real_base_year": item.real_base_year,
                            "jurisdiction": item.jurisdiction,
                            "source_reference": item.source_reference,
                            "review_status": item.review_status,
                        }
                        for item in self.costs
                    ],
                }
            ).encode("utf-8")
        ).hexdigest()

    def get(self, action_id: str) -> ActionCost:
        try:
            return self.by_action[str(action_id).strip().lower()]
        except KeyError as exc:
            raise KeyError(f"cost is unavailable for action: {action_id}") from exc


NJDOT_TAMP_2022_URL = "https://dot.nj.gov/transportation/about/asset/pdf/NJTAMP.pdf"


# Exhibit 5-6 reports project-level planning rates per square foot of deck area.
# The $206 preservation value is a generic BrM preservation treatment, not a
# bridge-cleaning unit rate and not a price for every activity in Exhibit 5-7.
DEFAULT_NJDOT_PLANNING_COSTS = CostLibrary(
    library_id="njdot_tamp_2022_bridge_planning_costs",
    version="2022-exhibit-5-6-v1",
    costs=(
        ActionCost(
            action_id="njdot_preservation_package",
            unit="deck_area",
            unit_cost=Decimal("206"),
            currency="USD",
            real_base_year=2022,
            jurisdiction="New Jersey",
            source_reference="NJDOT 2022 TAMP Exhibit 5-6 generic preservation treatment",
            review_status="published_planning_rate",
        ),
        ActionCost(
            action_id="deck_replacement",
            unit="deck_area",
            unit_cost=Decimal("511"),
            currency="USD",
            real_base_year=2022,
            jurisdiction="New Jersey",
            source_reference="NJDOT 2022 TAMP Exhibit 5-6 full deck replacement",
            review_status="published_planning_rate",
        ),
        ActionCost(
            action_id="superstructure_rehabilitation",
            unit="deck_area",
            unit_cost=Decimal("149"),
            currency="USD",
            real_base_year=2022,
            jurisdiction="New Jersey",
            source_reference="NJDOT 2022 TAMP Exhibit 5-6 superstructure rehabilitation",
            review_status="published_planning_rate",
        ),
        ActionCost(
            action_id="substructure_rehabilitation",
            unit="deck_area",
            unit_cost=Decimal("124"),
            currency="USD",
            real_base_year=2022,
            jurisdiction="New Jersey",
            source_reference="NJDOT 2022 TAMP Exhibit 5-6 substructure rehabilitation",
            review_status="published_planning_rate",
        ),
        ActionCost(
            action_id="full_replacement",
            unit="deck_area",
            unit_cost=Decimal("2556"),
            currency="USD",
            real_base_year=2022,
            jurisdiction="New Jersey",
            source_reference="NJDOT 2022 TAMP Exhibit 5-6 reconstruction",
            review_status="published_planning_rate",
        ),
    ),
)


def default_total_action_costs(deck_area: float | None) -> dict[str, Decimal]:
    """Return total event costs only when the deck-area quantity is available."""

    if deck_area is None:
        return {}
    quantity = Decimal(str(deck_area))
    if not quantity.is_finite() or quantity <= 0:
        return {}
    return {
        item.action_id: (item.unit_cost * quantity).quantize(Decimal("1"))
        for item in DEFAULT_NJDOT_PLANNING_COSTS.costs
    }
