"""Fleet-trained Markov deterioration model for bridge condition forecasting.

Why Markov (vs. Prophet): bridge condition is a small set of discrete ratings
(0-9) observed only every 1-2 years, with no seasonality and step-ups only when
work is done. The industry-standard model (AASHTOWare BrM / Pontis) is a Markov
chain on the condition state. This module estimates annual state-transition
probabilities from year-over-year inspection changes across the WHOLE fleet,
stratified by the covariates that actually drive deterioration:

  * bridge AGE  (current year - year_built)
  * truck TRAFFIC (annual_average_daily_truck_traffic, falling back to ADT)

Each bridge's forecast then propagates its current condition state forward
through its cohort's transition matrix, so:
  * the curve always degrades (no future work assumed),
  * the rate reflects how fast *similar* bridges (same age/traffic) actually
    deteriorate — learned from all bridges, not a single per-bridge fit,
  * it works even for bridges with only one inspection (Prophet needed >=3).

Improvements (work) are excluded from the deterioration transitions and instead
used to estimate the maintenance "recovery" bump for the what-if simulation.
"""

from __future__ import annotations

import os
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from .utils import USER_DB


METRIC_COLUMNS: Dict[str, str] = {
    "deck": "deck_condition_rating",
    "superstructure": "superstructure_condition_rating",
    "substructure": "substructure_condition_rating",
    "culvert": "culvert_condition_rating",
}

N_STATES = 10                  # condition ratings 0..9
MIN_COHORT_EXPOSURE = 25.0     # transition-years needed to trust a cohort cell
MIN_RECOVERY_SAMPLES = 3       # observed repairs needed to trust a county recovery bump
DEFAULT_DROP_PROB = 0.10       # annual P(drop one level) when the fleet has no data
DEFAULT_RECOVERY_BUMP = 2.0    # rating points a repair restores when none observed


def _safe_year(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        text = str(value).strip()
        match = re.search(r"(19|20)\d{2}", text)
        year = int(match.group(0)) if match else int(float(text))
        if 1900 <= year <= 2100:
            return year
    except Exception:
        return None
    return None


def _safe_rating(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        f = float(value)
        if 0 <= f <= 9:
            return f
    except Exception:
        return None
    return None


def _age_band(age: Optional[float]) -> Optional[str]:
    if age is None:
        return None
    if age < 0:
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


def traffic_band(adtt: Any, adt: Any = None) -> Optional[str]:
    """Heavier truck traffic deteriorates decks/joints faster. Use ADTT if known,
    otherwise total ADT, otherwise no traffic stratification."""
    v = None
    try:
        if adtt not in (None, ""):
            v = float(adtt)
            return "low" if v < 250 else "med" if v < 1000 else "high"
    except Exception:
        pass
    try:
        if adt not in (None, ""):
            v = float(adt)
            return "low" if v < 5000 else "med" if v < 20000 else "high"
    except Exception:
        pass
    return None


def _slot_magnitudes(total_drop: int, years: int) -> List[int]:
    """Distribute an interval's total rating drop into per-year magnitudes.

    Inspections are 1-2 years apart, so a multi-year interval cannot localize
    when the drop happened; spreading it as evenly as possible is the least
    biased attribution (a 3-level drop over 2 years -> one year of 2 and one
    of 1; a 1-level drop over 2 years -> one drop-year and one stay-year).
    """
    if years <= 0:
        return []
    q, r = divmod(max(0, total_drop), years)
    return [q + 1] * r + [q] * (years - r)


def _work_years(conn: sqlite3.Connection) -> Dict[Tuple[int, str], set]:
    out: Dict[Tuple[int, str], set] = {}

    def _add(s, b, y):
        yy = _safe_year(y)
        if yy is None or s is None or b in (None, ""):
            return
        try:
            out.setdefault((int(s), str(b)), set()).add(yy)
        except (TypeError, ValueError):
            pass

    for sql in (
        "SELECT state_code, bridge_number, year_work_performed FROM works",
        "SELECT state_code, bridge_number, year_work_performed FROM bridges WHERE year_work_performed IS NOT NULL",
    ):
        try:
            for row in conn.execute(sql).fetchall():
                d = dict(row)
                _add(d.get("state_code"), d.get("bridge_number"), d.get("year_work_performed"))
        except Exception:
            continue
    return out


# Simple in-process cache so opening many bridges doesn't rescan the fleet each
# time. A few slots so a backtest (which trains on a truncated year range)
# doesn't evict the full model.
_CACHE: Dict[tuple, Dict[str, Any]] = {}
_CACHE_SLOTS = 4


def _cache_key(source_db: str, max_year: Optional[int] = None) -> Optional[tuple]:
    try:
        conn = sqlite3.connect(source_db)
        try:
            n, mx = conn.execute("SELECT COUNT(*), MAX(data_year) FROM bridges").fetchone()
        finally:
            conn.close()
        return (source_db, max_year, n, mx)
    except Exception:
        return None


def build_markov_model(source_db: str = USER_DB, use_cache: bool = True, max_year: Optional[int] = None) -> Dict[str, Any]:
    """Estimate the fleet transition model. ``max_year`` restricts training to
    inspection years <= that year (used by the rolling-origin backtest)."""
    key = _cache_key(source_db, max_year)
    if use_cache and key is not None and key in _CACHE:
        return _CACHE[key]

    model = {
        "agg": {},          # (metric, age_band, traffic_band) -> {state: [drops, exposure_years]}
        "recovery": {m: None for m in METRIC_COLUMNS},
        "current": {},      # (state, bridge) -> {metric: (year, rating)}
        "obs_counts": {},   # (state, bridge) -> {metric: count}
        "meta": {},         # (state, bridge) -> {year_built, adtt, adt, bridge_name}
        "transitions": 0,
        "cohorts": 0,
    }
    if not os.path.exists(source_db):
        return model

    conn = sqlite3.connect(source_db)
    conn.row_factory = sqlite3.Row
    try:
        sql = """
            SELECT state_code, bridge_number, bridge_name, county_code, data_year, year_built,
                   annual_average_daily_traffic AS adt,
                   annual_average_daily_truck_traffic AS adtt,
                   deck_condition_rating, superstructure_condition_rating,
                   substructure_condition_rating, culvert_condition_rating
            FROM bridges
            WHERE data_year IS NOT NULL
        """
        params: List[Any] = []
        if max_year is not None:
            sql += " AND data_year <= ?"
            params.append(int(max_year))
        sql += " ORDER BY state_code, bridge_number, data_year"
        rows = conn.execute(sql, params).fetchall()
        work_years = _work_years(conn)
    finally:
        conn.close()

    by_bridge: Dict[Tuple[int, str], List[sqlite3.Row]] = {}
    for r in rows:
        try:
            key_b = (int(r["state_code"]), str(r["bridge_number"]))
        except (TypeError, ValueError):
            continue
        by_bridge.setdefault(key_b, []).append(r)

    agg = model["agg"]
    # Recovery samples nested by scope: a county_code, or None for statewide.
    recovery_samples: Dict[Optional[int], Dict[str, List[float]]] = {None: {m: [] for m in METRIC_COLUMNS}}

    # Transition cells are keyed (metric, county, age_band, tb). county=None is
    # the statewide cohort; a county's own cells are preferred when populated and
    # the statewide cells are the fallback (see _drop_dist). Each cell holds
    # exposure years plus a histogram of per-year drop magnitudes, so genuine
    # multi-level annual drops (scour, impact, rapid deck failure) survive into
    # the transition matrix instead of being clamped to one level per year.
    def _add_transition(metric: str, county: Optional[int], age_band: Optional[str], tb: Optional[str], state: int, total_drop: int, years: int):
        keys = [(metric, None, age_band, tb), (metric, None, age_band, None), (metric, None, None, None)]
        if county is not None:
            keys += [(metric, county, age_band, tb), (metric, county, age_band, None), (metric, county, None, None)]
        magnitudes = _slot_magnitudes(total_drop, years)
        for ck in keys:
            cell = agg.setdefault(ck, {}).setdefault(state, {"exposure": 0.0, "mag": {}})
            cell["exposure"] += years
            for m in magnitudes:
                if m > 0:
                    cell["mag"][m] = cell["mag"].get(m, 0.0) + 1.0

    for (s, b), recs in by_bridge.items():
        recs.sort(key=lambda x: int(x["data_year"]))
        year_built = _safe_year(recs[-1]["year_built"])
        adtt = next((r["adtt"] for r in reversed(recs) if r["adtt"] not in (None, "")), None)
        adt = next((r["adt"] for r in reversed(recs) if r["adt"] not in (None, "")), None)
        tb = traffic_band(adtt, adt)
        name = next((r["bridge_name"] for r in reversed(recs) if r["bridge_name"]), None)
        county: Optional[int] = None
        try:
            cc = recs[-1]["county_code"]
            county = int(cc) if cc not in (None, "") else None
        except (TypeError, ValueError):
            county = None
        model["meta"][(s, b)] = {"year_built": year_built, "adtt": adtt, "adt": adt, "bridge_name": name, "county_code": county}
        wy = work_years.get((s, b), set())

        for metric, col in METRIC_COLUMNS.items():
            seq = [(int(r["data_year"]), _safe_rating(r[col])) for r in recs]
            seq = [(y, v) for y, v in seq if v is not None]
            if not seq:
                continue
            model["current"].setdefault((s, b), {})[metric] = seq[-1]
            model["obs_counts"].setdefault((s, b), {})[metric] = len(seq)
            for k in range(1, len(seq)):
                y1, r1 = seq[k - 1]
                y2, r2 = seq[k]
                dt = y2 - y1
                if dt <= 0:
                    continue
                worked = any(y1 < wk <= y2 for wk in wy)
                if r2 > r1 or worked:
                    # Only credit recovery to intervals with a recorded work
                    # event; unexplained rating increases are re-rating noise,
                    # not maintenance, and belong in neither estimate.
                    if worked and r2 > r1:
                        gain = r2 - r1
                        recovery_samples[None][metric].append(gain)
                        if county is not None:
                            recovery_samples.setdefault(county, {m: [] for m in METRIC_COLUMNS})[metric].append(gain)
                    continue  # not a natural-deterioration transition
                age1 = (y1 - year_built) if year_built else None
                _add_transition(metric, county, _age_band(age1), tb, int(round(r1)), int(round(r1 - r2)), int(dt))
                model["transitions"] += 1

    model["recovery"] = {
        scope: {m: {"mean": (sum(v) / len(v) if v else None), "n": len(v)} for m, v in metrics.items()}
        for scope, metrics in recovery_samples.items()
    }
    model["cohorts"] = sum(1 for k in agg if k[1] is not None)

    if use_cache and key is not None:
        if len(_CACHE) >= _CACHE_SLOTS:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = model
    return model


def _dist_from_cell(cell: Dict[str, Any]) -> Dict[int, float]:
    exposure = cell["exposure"]
    dist = {m: w / exposure for m, w in cell["mag"].items() if w > 0}
    total = sum(dist.values())
    if total > 1.0:  # cannot happen with _slot_magnitudes, but stay safe
        dist = {m: p / total for m, p in dist.items()}
    return dist


def _drop_dist(agg: Dict, metric: str, county: Optional[int], age_band: Optional[str], tb: Optional[str], state: int) -> Dict[int, float]:
    """Annual distribution of drop magnitudes from ``state`` as {levels: prob}.

    Prefers the bridge's own COUNTY (most specific cohort first), then falls
    back to the statewide cohort, so a county with thin data borrows the
    state rate."""
    order: List[Tuple] = []
    if county is not None:
        order += [(metric, county, age_band, tb), (metric, county, age_band, None), (metric, county, None, None)]
    order += [(metric, None, age_band, tb), (metric, None, age_band, None), (metric, None, None, None)]
    for ck in order:
        cell = agg.get(ck, {}).get(state)
        if cell and cell["exposure"] >= MIN_COHORT_EXPOSURE:
            return _dist_from_cell(cell)
    cell = agg.get((metric, None, None, None), {}).get(state)
    if cell and cell["exposure"] > 0:
        return _dist_from_cell(cell)
    return {1: DEFAULT_DROP_PROB}


def expected_annual_drop(
    model: Dict[str, Any],
    metric: str,
    state: float,
    age: Optional[float],
    tb: Optional[str],
    county: Optional[int] = None,
) -> float:
    """Expected condition-rating drop over one year from ``state`` for the
    bridge's cohort (age band + traffic band, county-preferred)."""
    s = max(0, min(9, int(round(state))))
    if s == 0:
        return 0.0
    drops = _drop_dist(model["agg"], metric, county, _age_band(age), tb, s)
    return sum(m * p for m, p in drops.items())


def forecast_metric(
    model: Dict[str, Any],
    metric: str,
    start_rating: float,
    base_age: Optional[float],
    tb: Optional[str],
    horizon: int,
    start_year: int,
    county: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Propagate the condition-state distribution forward ``horizon`` years using
    the bridge's COUNTY transition rates (statewide fallback when thin).

    Returns one row per future year with the expected rating (yhat) and a
    one-sigma band derived from the state distribution. The age band advances
    each year, so deterioration accelerates as the bridge ages.
    """
    agg = model["agg"]
    dist = [0.0] * N_STATES
    r0 = max(0, min(9, int(round(start_rating))))
    dist[r0] = 1.0
    out: List[Dict[str, Any]] = []
    for step in range(1, horizon + 1):
        ab = _age_band((base_age + step) if base_age is not None else None)
        new = [0.0] * N_STATES
        for i in range(N_STATES):
            w = dist[i]
            if w <= 0:
                continue
            if i == 0:
                new[0] += w  # absorbing at the worst state
            else:
                drops = _drop_dist(agg, metric, county, ab, tb, i)
                p_total = min(1.0, sum(drops.values()))
                new[i] += w * (1.0 - p_total)
                for magnitude, p in drops.items():
                    new[max(0, i - magnitude)] += w * p
        dist = new
        mean = sum(i * dist[i] for i in range(N_STATES))
        var = sum((i - mean) ** 2 * dist[i] for i in range(N_STATES))
        std = var ** 0.5
        out.append({
            "year": int(start_year + step),
            "yhat": round(mean, 3),
            "yhat_lower": round(max(0.0, mean - std), 3),
            "yhat_upper": round(min(9.0, mean + std), 3),
        })
    return out


def recovery_bump(model: Dict[str, Any], metric: str, county: Optional[int] = None) -> Tuple[float, str]:
    """Maintenance bump magnitude: the observed average post-work rating gain for
    this component. Prefers the bridge's COUNTY (when it has enough recorded
    repairs), falls back to statewide, then a default when none has been seen."""
    rec = model.get("recovery", {})
    candidates: List[Tuple[Optional[int], int]] = []
    if county is not None:
        candidates.append((county, MIN_RECOVERY_SAMPLES))
    candidates.append((None, 1))
    for scope, min_n in candidates:
        entry = rec.get(scope, {}).get(metric)
        if entry and entry.get("mean") is not None and entry.get("n", 0) >= min_n and entry["mean"] >= 0.25:
            return round(min(4.0, float(entry["mean"])), 2), "learned"
    return DEFAULT_RECOVERY_BUMP, "default"


def simulate_metric(
    model: Dict[str, Any],
    metric: str,
    start_rating: float,
    base_age: Optional[float],
    tb: Optional[str],
    horizon: int,
    start_year: int,
    maintenance_year: int,
    bump: float,
    county: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Baseline degradation until ``maintenance_year``, a repair bump that year,
    then Markov degradation from the restored state for the remaining horizon."""
    baseline = forecast_metric(model, metric, start_rating, base_age, tb, horizon, start_year, county=county)
    simulated: List[Dict[str, Any]] = []
    pre_value = start_rating
    for point in baseline:
        if point["year"] < maintenance_year:
            simulated.append({"year": point["year"], "yhat": point["yhat"]})
            pre_value = point["yhat"]
        elif point["year"] == maintenance_year:
            pre_value = point["yhat"]
            break
    bumped = min(9.0, pre_value + bump)
    simulated.append({"year": int(maintenance_year), "yhat": round(bumped, 3)})
    remaining = (start_year + horizon) - maintenance_year
    if remaining > 0:
        age_at_work = (base_age + (maintenance_year - start_year)) if base_age is not None else None
        post = forecast_metric(model, metric, bumped, age_at_work, tb, remaining, maintenance_year, county=county)
        simulated.extend({"year": p["year"], "yhat": p["yhat"]} for p in post)
    return simulated
