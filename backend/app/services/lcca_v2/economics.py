"""Explicit constant-dollar and nominal-dollar LCCA economics."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class EconomicMode(str, Enum):
    CONSTANT_DOLLAR = "constant_dollar"
    NOMINAL_DOLLAR = "nominal_dollar"


@dataclass(frozen=True, slots=True)
class EconomicAssumptions:
    mode: EconomicMode
    base_year: int
    real_discount_rate: Decimal
    nominal_discount_rate: Decimal | None = None
    inflation_rate: Decimal | None = None
    source_reference: str = "user-entered planning assumption"

    def __post_init__(self) -> None:
        real = Decimal(self.real_discount_rate)
        nominal = (
            Decimal(self.nominal_discount_rate)
            if self.nominal_discount_rate is not None
            else None
        )
        inflation = Decimal(self.inflation_rate) if self.inflation_rate is not None else None
        if not real.is_finite() or real <= Decimal("-1"):
            raise ValueError("real discount rate must be finite and greater than -1")
        if self.mode is EconomicMode.CONSTANT_DOLLAR:
            if nominal is not None or inflation not in (None, Decimal("0")):
                raise ValueError("constant-dollar mode cannot include nominal inflation inputs")
        else:
            if nominal is None or inflation is None:
                raise ValueError("nominal-dollar mode requires nominal and inflation rates")
            if not nominal.is_finite() or not inflation.is_finite():
                raise ValueError("nominal economics must be finite")
            relationship = (Decimal("1") + real) * (Decimal("1") + inflation)
            if abs(relationship - (Decimal("1") + nominal)) > Decimal("0.0005"):
                raise ValueError("real, nominal, and inflation rates fail the relationship check")
        object.__setattr__(self, "real_discount_rate", real)
        object.__setattr__(self, "nominal_discount_rate", nominal)
        object.__setattr__(self, "inflation_rate", inflation)


@dataclass(frozen=True, slots=True)
class DiscountedCost:
    base_cost: Decimal
    future_cost: Decimal
    inflation_factor: Decimal
    discount_factor: Decimal
    present_value: Decimal


def discount_cost(
    base_cost: Decimal,
    *,
    funding_year: int,
    assumptions: EconomicAssumptions,
) -> DiscountedCost:
    cost = Decimal(base_cost)
    if not cost.is_finite() or cost < 0:
        raise ValueError("cost must be finite and non-negative")
    elapsed = int(funding_year) - int(assumptions.base_year)
    if elapsed < 0:
        raise ValueError("funding year cannot precede the economic base year")
    if assumptions.mode is EconomicMode.CONSTANT_DOLLAR:
        inflation_factor = Decimal("1")
        future_cost = cost
        discount_factor = (Decimal("1") + assumptions.real_discount_rate) ** elapsed
    else:
        assert assumptions.inflation_rate is not None
        assert assumptions.nominal_discount_rate is not None
        inflation_factor = (Decimal("1") + assumptions.inflation_rate) ** elapsed
        future_cost = cost * inflation_factor
        discount_factor = (Decimal("1") + assumptions.nominal_discount_rate) ** elapsed
    return DiscountedCost(
        base_cost=cost,
        future_cost=future_cost,
        inflation_factor=inflation_factor,
        discount_factor=discount_factor,
        present_value=future_cost / discount_factor,
    )


def equivalent_annual_cost(net_present_value: Decimal, rate: Decimal, years: int) -> Decimal:
    amount = Decimal(net_present_value)
    normalized_rate = Decimal(rate)
    period = int(years)
    if period <= 0:
        raise ValueError("analysis period must be positive")
    if normalized_rate == 0:
        return amount / period
    factor = normalized_rate * (Decimal("1") + normalized_rate) ** period
    denominator = (Decimal("1") + normalized_rate) ** period - Decimal("1")
    return amount * factor / denominator
