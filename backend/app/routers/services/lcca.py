"""Forecast-driven life-cycle cost analysis (LCCA) for a single bridge.

This couples the fleet-trained Markov deterioration forecast (see markov.py) to an
FHWA-style LCCA:

  * The controlling condition (min of deck/super/sub/culvert) degrades at the
    rate the Markov model calibrates for this bridge's age/traffic cohort.
  * Routine maintenance cost each year scales with condition (worse = costlier).
  * A major rehab applies the forecast's learned "recovery" bump and resets the
    deterioration clock; a replacement resets condition to new.
  * All costs are discounted to present value (true NPV).

It then sweeps the rehab year to find the least-NPV intervention and compares it
to running to failure (do-nothing), so the UI can recommend "rehab in YYYY saves
$X vs. do-nothing".
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from .utils import USER_DB
from . import markov as MK


DESIGN_LIFE = 75
REHAB_SERVICE_LIFE = 25          # years a major rehab is expected to add
DISCOUNT_RATE = 0.03
REPLACE_THRESHOLD = 3.0          # controlling rating at/below which replacement is forced
POOR_THRESHOLD = 4.9             # "Poor" condition
# --- Unit costs ($/ft² of deck area) -----------------------------------------
# Grounded in published agency cost data rather than guesses:
#   * Replacement: FHWA National Bridge Inventory "Bridge Replacement Unit Costs"
#     2024 (collected 2025) for NEW JERSEY — NHS $840/ft², non-NHS $775/ft².
#     The NJ figures already embed NJ prevailing wages, labor, materials and site
#     conditions (they are NJ-submitted actual project costs), which is exactly
#     why they run ~2x the national average ($429 NHS / $383 non-NHS). We blend
#     to one NJ rate because the loaded SNBI data does not populate nhs_designation.
#     https://www.fhwa.dot.gov/bridge/nbi/sd2024.cfm
#   * Major rehab: ~40% of replacement — a deck replacement + superstructure
#     repairs + joints/bearings/repaint. Consistent with WisDOT Bureau of
#     Structures unit costs (deck replace $60, concrete overlay $46, repaint $38
#     per ft²) scaled to NJ's cost level, and the common 30–50%-of-replacement
#     rule of thumb. (WisDOT Cost Estimate Calculations, 2025.)
#   * Routine maintenance: cyclical preservation (periodic overlays, washing,
#     sealing, sealing, inspection, minor repairs) amortized per year.
COST_PER_SQFT_REPLACE = 800.0    # NJ blended (FHWA NBI 2024: NHS $840 / non-NHS $775)
NJ_REPLACE_PER_SQFT_NHS = 840.0      # FHWA NBI 2024 — New Jersey, NHS bridges
NJ_REPLACE_PER_SQFT_NON_NHS = 775.0  # FHWA NBI 2024 — New Jersey, non-NHS bridges
COST_PER_SQFT_REHAB = 300.0      # ≈40% of replacement; major rehab incl. deck + super
ANNUAL_MAINT_PER_SQFT = 1.50     # cyclical preservation amortized (base; scales with condition)
DEFAULT_REPLACE = 4_000_000.0    # fallback when deck area is unknown
DEFAULT_REHAB = 1_500_000.0
DEFAULT_ANNUAL_BASE = 12_000.0
USER_COST_FACTOR = 1.30          # +30% indirect/user delay costs (display only)

# Maintenance policy: rehab the bridge once it degrades into Poor condition,
# rather than deferring to the replacement cliff. Operating a Poor structure
# carries a risk/user-cost penalty that escalates the longer it is left below
# the Poor threshold — this is what makes a timely rehab economically rational.
REHAB_TRIGGER = POOR_THRESHOLD   # intervene when the controlling rating hits Poor
POOR_PENALTY_PER_SQFT = 1.0      # $/ft²/yr base risk & user cost while Poor (≈4× routine)
POOR_PENALTY_ESCALATION = 0.25   # penalty grows 25%/yr for each consecutive Poor year
DEFAULT_POOR_PENALTY = 20_000.0  # fallback when deck area is unknown
DEFAULT_HORIZON = 40
CALIBRATION_HORIZON = 12         # years of Markov projection used to estimate the rate


def _num(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _bridge_facts(state_code: int, bridge_number: str, source_db: str) -> Optional[Dict[str, Any]]:
    """Latest-year inventory facts needed for the cost model."""
    conn = sqlite3.connect(source_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT data_year, year_built, calculated_deck_area, total_bridge_length,
                   bridge_width_out_to_out
            FROM bridges
            WHERE state_code=? AND bridge_number=? AND data_year IS NOT NULL
            ORDER BY data_year DESC LIMIT 1
            """,
            (int(state_code), str(bridge_number)),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    d = dict(row)
    deck_area = _num(d.get("calculated_deck_area"))
    if not deck_area:
        length = _num(d.get("total_bridge_length"))
        width = _num(d.get("bridge_width_out_to_out"))
        deck_area = (length * width) if (length and width) else 0.0
    return {
        "year_built": MK._safe_year(d.get("year_built")),
        "deck_area": round(deck_area or 0.0, 1),
    }


def _calibrate(state_code: int, bridge_number: str, source_db: str) -> Optional[Dict[str, Any]]:
    """Use the Markov model to derive the current controlling rating, the
    calibrated annual decline, and the learned maintenance recovery bump."""
    model = MK.build_markov_model(source_db)
    key = (int(state_code), str(bridge_number))
    current = model["current"].get(key)
    if not current:
        return None
    meta = model["meta"].get(key, {})
    year_built = meta.get("year_built")
    tb = MK.traffic_band(meta.get("adtt"), meta.get("adt"))
    county = meta.get("county_code")
    latest_year = max(yr for (yr, _v) in current.values())
    base_age = (latest_year - year_built) if year_built else None

    # Controlling rating today = the worst current component.
    current_controlling = min(float(r) for (_y, r) in current.values())

    # Project each metric a short way (using this bridge's COUNTY transition
    # rates, statewide fallback) and take the per-year min to get the controlling
    # trajectory, then average the year-over-year drop = calibrated annual rate.
    per_metric: Dict[str, List[float]] = {}
    for metric, (_yr, rating) in current.items():
        fut = MK.forecast_metric(model, metric, rating, base_age, tb, CALIBRATION_HORIZON, latest_year, county=county)
        if fut:
            per_metric[metric] = [p["yhat"] for p in fut]
    decline = MK.DEFAULT_DROP_PROB
    if per_metric:
        n = min(len(v) for v in per_metric.values())
        controlling = [min(per_metric[m][i] for m in per_metric) for i in range(n)]
        series = [current_controlling] + controlling
        diffs = [series[i - 1] - series[i] for i in range(1, len(series)) if series[i - 1] >= series[i]]
        if diffs:
            decline = sum(diffs) / len(diffs)
    decline = max(0.02, round(decline, 4))

    # Age- and state-aware annual decline for the currently controlling
    # component, straight from the Markov transition matrix, so the LCCA
    # walk keeps the fleet's age acceleration instead of a flat average.
    controlling_metric = min(current.items(), key=lambda kv: float(kv[1][1]))[0]

    def drop_rate(rating: float, age: Optional[float]) -> float:
        return max(0.02, MK.expected_annual_drop(model, controlling_metric, rating, age, tb, county=county))

    bump, bump_source = MK.recovery_bump(model, "deck", county=county)
    return {
        "current_controlling": round(current_controlling, 2),
        "base_age": base_age,
        "decline": decline,
        "drop_rate": drop_rate,
        "controlling_metric": controlling_metric,
        "bump": float(bump),
        "bump_source": bump_source,
        "start_year": int(latest_year),
        "county_code": county,
        "transitions": model["transitions"],
    }


def _simulate_strategy(
    current_controlling: float,
    drop_rate,
    bump: float,
    horizon: int,
    rehab_trigger: Optional[float],
    base_age: Optional[float] = None,
) -> Tuple[List[float], List[int], List[int]]:
    """Walk the controlling rating year by year.

    ``drop_rate(rating, age)`` supplies the Markov expected annual decline, so
    deterioration accelerates with age and worsening state. Under a maintenance
    policy (``rehab_trigger`` set) the bridge is rehabbed every time it degrades
    to the trigger rating while still above the replacement floor — the recovery
    bump is applied and it degrades again, so a long horizon can hold several
    rehabs. With no policy (``rehab_trigger=None``) only forced replacements
    occur. A replacement (rating hits the floor) resets the structure to new and
    restarts its age clock. Returns (rating_series, rehab_years, replacement_years).
    """
    series: List[float] = []
    rehab_years: List[int] = []
    replace_years: List[int] = []
    cur = float(current_controlling)
    replaced_at: Optional[int] = None
    for t in range(1, horizon + 1):
        if replaced_at is not None:
            age: Optional[float] = float(t - replaced_at)
        elif base_age is not None:
            age = float(base_age + t)
        else:
            age = None
        cur = max(0.0, cur - drop_rate(cur, age))
        if rehab_trigger is not None and REPLACE_THRESHOLD < cur <= rehab_trigger:
            cur = min(9.0, cur + bump)
            rehab_years.append(t)
        if cur <= REPLACE_THRESHOLD:
            replace_years.append(t)
            cur = 9.0  # replaced — a new structure
            replaced_at = t
        series.append(round(cur, 2))
    return series, rehab_years, replace_years


def _salvage_credit(
    rehab_years: List[int], replace_years: List[int], horizon: int,
    costs: Dict[str, float], discount_rate: float,
) -> float:
    """FHWA-style residual (salvage) value at the end of the analysis period:
    the last capital activity keeps value proportional to its remaining service
    life, discounted back from the horizon year."""
    credit = 0.0
    if replace_years:
        used = horizon - replace_years[-1]
        remaining = max(0.0, DESIGN_LIFE - used) / DESIGN_LIFE
        credit += costs["replace"] * remaining
    rehabs_after_last_replace = [
        t for t in rehab_years if not replace_years or t > replace_years[-1]
    ]
    if rehabs_after_last_replace:
        used = horizon - rehabs_after_last_replace[-1]
        remaining = max(0.0, REHAB_SERVICE_LIFE - used) / REHAB_SERVICE_LIFE
        credit += costs["rehab"] * remaining
    return credit / (1.0 + discount_rate) ** horizon


def _npv(series: List[float], rehab_years: List[int], replace_years: List[int], costs: Dict[str, float], discount_rate: float = DISCOUNT_RATE) -> float:
    r = discount_rate
    total = 0.0
    poor_streak = 0
    for t in range(1, len(series) + 1):
        rating = series[t - 1]
        routine = costs["annual_base"] * (10.0 - rating) / 5.0
        total += routine / (1.0 + r) ** t
        # Risk/user-cost penalty for operating a structurally Poor bridge; it
        # escalates with each consecutive year left below the Poor threshold.
        if rating < POOR_THRESHOLD:
            poor_streak += 1
            severity = POOR_THRESHOLD - rating
            penalty = costs["poor_penalty"] * severity * (1.0 + POOR_PENALTY_ESCALATION * (poor_streak - 1))
            total += penalty / (1.0 + r) ** t
        else:
            poor_streak = 0
    for t in rehab_years:
        total += costs["rehab"] / (1.0 + r) ** t
    for t in replace_years:
        total += costs["replace"] / (1.0 + r) ** t
    return total


def _cumulative_cost_series(
    series: List[float], rehab_years: List[int], replace_years: List[int],
    costs: Dict[str, float], discount_rate: float,
) -> List[float]:
    """Per-year running total of discounted cost (routine + Poor penalty +
    rehab/replacement capital), so the money spent can be plotted over time.
    Capital events show up as step jumps; the final value is the strategy NPV
    before the salvage credit."""
    r = discount_rate
    rehab_set, replace_set = set(rehab_years), set(replace_years)
    cum = 0.0
    poor_streak = 0
    out: List[float] = []
    for t in range(1, len(series) + 1):
        rating = series[t - 1]
        yearly = costs["annual_base"] * (10.0 - rating) / 5.0
        if rating < POOR_THRESHOLD:
            poor_streak += 1
            severity = POOR_THRESHOLD - rating
            yearly += costs["poor_penalty"] * severity * (1.0 + POOR_PENALTY_ESCALATION * (poor_streak - 1))
        else:
            poor_streak = 0
        if t in rehab_set:
            yearly += costs["rehab"]
        if t in replace_set:
            yearly += costs["replace"]
        cum += yearly / (1.0 + r) ** t
        out.append(round(cum))
    return out


def compute_bridge_lcca(
    state_code: int, bridge_number: str, source_db: str = USER_DB, horizon_years: int = DEFAULT_HORIZON,
    nhs: Optional[bool] = None,
    replace_per_sqft: Optional[float] = None,
    rehab_per_sqft: Optional[float] = None,
    annual_maint_per_sqft: Optional[float] = None,
    poor_penalty_per_sqft: Optional[float] = None,
    discount_rate: Optional[float] = None,
) -> Dict[str, Any]:
    """Forecast-driven LCCA for one bridge.

    All cost assumptions are editable via the optional ``*_per_sqft`` overrides;
    when omitted they fall back to the published NJ defaults. ``nhs`` selects the
    FHWA NBI New Jersey replacement unit cost (True=NHS $840, False=non-NHS $775,
    None=blended $800) unless ``replace_per_sqft`` is given explicitly.
    """
    horizon = max(5, min(100, int(horizon_years)))
    facts = _bridge_facts(state_code, bridge_number, source_db)
    calib = _calibrate(state_code, bridge_number, source_db)
    if calib is None:
        return {"status": "error", "message": "No condition data to forecast for this bridge."}

    # Resolve the effective unit costs: explicit override > NHS selection > default.
    if replace_per_sqft is not None and replace_per_sqft > 0:
        eff_replace_psf = float(replace_per_sqft)
    elif nhs is True:
        eff_replace_psf = NJ_REPLACE_PER_SQFT_NHS
    elif nhs is False:
        eff_replace_psf = NJ_REPLACE_PER_SQFT_NON_NHS
    else:
        eff_replace_psf = COST_PER_SQFT_REPLACE
    eff_rehab_psf = float(rehab_per_sqft) if (rehab_per_sqft is not None and rehab_per_sqft > 0) else COST_PER_SQFT_REHAB
    eff_maint_psf = float(annual_maint_per_sqft) if (annual_maint_per_sqft is not None and annual_maint_per_sqft >= 0) else ANNUAL_MAINT_PER_SQFT
    eff_penalty_psf = float(poor_penalty_per_sqft) if (poor_penalty_per_sqft is not None and poor_penalty_per_sqft >= 0) else POOR_PENALTY_PER_SQFT
    eff_discount = float(discount_rate) if (discount_rate is not None and discount_rate > 0) else DISCOUNT_RATE

    deck_area = (facts or {}).get("deck_area") or 0.0
    costs = {
        "replace": round(deck_area * eff_replace_psf) if deck_area > 0 else DEFAULT_REPLACE,
        "rehab": round(deck_area * eff_rehab_psf) if deck_area > 0 else DEFAULT_REHAB,
        "annual_base": round(deck_area * eff_maint_psf) if deck_area > 0 else DEFAULT_ANNUAL_BASE,
        "poor_penalty": round(deck_area * eff_penalty_psf) if deck_area > 0 else DEFAULT_POOR_PENALTY,
    }

    cur = calib["current_controlling"]
    drop_rate = calib["drop_rate"]
    bump = calib["bump"]
    start_year = calib["start_year"]
    base_age = calib.get("base_age")

    # Do-nothing (replacements still occur when the bridge fails).
    dn_series, dn_rehab, dn_replace = _simulate_strategy(cur, drop_rate, bump, horizon, None, base_age)
    dn_salvage = _salvage_credit(dn_rehab, dn_replace, horizon, costs, eff_discount)
    dn_npv = _npv(dn_series, dn_rehab, dn_replace, costs, eff_discount) - dn_salvage

    # Condition-triggered policy: rehab each time the bridge degrades into Poor
    # (≤4.9) rather than deferring to the replacement cliff at 3.0.
    rec_series, rec_rehab, rec_replace = _simulate_strategy(cur, drop_rate, bump, horizon, REHAB_TRIGGER, base_age)
    rec_salvage = _salvage_credit(rec_rehab, rec_replace, horizon, costs, eff_discount)
    rec_npv = _npv(rec_series, rec_rehab, rec_replace, costs, eff_discount) - rec_salvage
    recommend = (
        {"npv": rec_npv, "series": rec_series, "rehab_years": rec_rehab, "replace_years": rec_replace}
        if (rec_rehab and rec_npv < dn_npv - 1.0)
        else None
    )

    def _first(years: List[int]) -> Optional[int]:
        return start_year + years[0] if years else None

    # Years until the do-nothing curve crosses Poor / replacement.
    years_to_poor = next((t for t, v in enumerate(dn_series, 1) if v <= POOR_THRESHOLD), None)

    chart = [
        {
            "year": start_year + t,
            "do_nothing": dn_series[t - 1],
            "intervention": recommend["series"][t - 1] if recommend else None,
        }
        for t in range(1, horizon + 1)
    ]

    # Cumulative discounted spend per year, for the money view of the same
    # two strategies. The intervention line is only shown when a rehab is
    # actually recommended (mirrors the condition chart).
    dn_cost = _cumulative_cost_series(dn_series, dn_rehab, dn_replace, costs, eff_discount)
    rec_cost = _cumulative_cost_series(rec_series, rec_rehab, rec_replace, costs, eff_discount)
    cost_chart = [
        {
            "year": start_year + t,
            "do_nothing": dn_cost[t - 1],
            "intervention": rec_cost[t - 1] if recommend else None,
        }
        for t in range(1, horizon + 1)
    ]

    return {
        "status": "success",
        "state_code": int(state_code),
        "bridge_number": str(bridge_number),
        "engine": "markov",
        "start_year": start_year,
        "horizon_years": horizon,
        "discount_rate": eff_discount,
        "nhs": nhs,
        "parameters": {
            "current_controlling_rating": cur,
            "annual_decline_rate": round(calib["decline"], 3),
            "controlling_metric": calib.get("controlling_metric"),
            "recovery_bump": round(bump, 2),
            "recovery_source": calib["bump_source"],
            "deck_area": deck_area,
            "age": (start_year - facts["year_built"]) if facts and facts.get("year_built") else None,
            "year_built": (facts or {}).get("year_built"),
            "years_to_poor": years_to_poor,
            "county_code": calib.get("county_code"),
            "fleet_transitions": calib["transitions"],
        },
        "unit_costs": {
            "replace": costs["replace"],
            "rehab": costs["rehab"],
            "annual_base": costs["annual_base"],
            "per_sqft_replace": eff_replace_psf,
            "per_sqft_rehab": eff_rehab_psf,
            "per_sqft_annual_maint": eff_maint_psf,
            "per_sqft_poor_penalty": eff_penalty_psf,
        },
        "do_nothing": {
            "npv": round(dn_npv),
            "npv_with_user_costs": round(dn_npv * USER_COST_FACTOR),
            "salvage_credit": round(dn_salvage),
            "first_replacement_year": _first(dn_replace),
            "replacement_count": len(dn_replace),
        },
        "recommended": None if recommend is None else {
            "rehab_year": start_year + recommend["rehab_years"][0],
            "rehab_years": [start_year + t for t in recommend["rehab_years"]],
            "npv": round(recommend["npv"]),
            "npv_with_user_costs": round(recommend["npv"] * USER_COST_FACTOR),
            "salvage_credit": round(rec_salvage),
            "savings": round(dn_npv - recommend["npv"]),
            "first_replacement_year": _first(recommend["replace_years"]),
            "replacement_count": len(recommend["replace_years"]),
        },
        "chart": chart,
        "cost_chart": cost_chart,
    }
