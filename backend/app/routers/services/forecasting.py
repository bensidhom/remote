"""Forecast training data and Prophet predictions for bridge deterioration."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .utils import USER_DB
from .datapaths import FORECAST_DB

METRICS: Dict[str, str] = {
    "deck": "deck_condition_rating",
    "superstructure": "superstructure_condition_rating",
    "substructure": "substructure_condition_rating",
    "culvert": "culvert_condition_rating",
}

# Deterioration / maintenance-simulation tuning. Conditions degrade going
# forward (no future work is assumed); a simulated maintenance event bumps the
# rating up by the magnitude the model learned from historical work years, or a
# sensible default when the bridge has no usable historical maintenance signal.
DEFAULT_MAINTENANCE_BUMP = 2.0
MIN_LEARNED_BUMP = 0.25
MAX_LEARNED_BUMP = 3.0
DEFAULT_ANNUAL_DECLINE = 0.10

# Forecasting engine. "markov" = fleet-trained Markov transition model (default,
# bridge-appropriate, covariate-aware); "prophet" = the per-bridge Prophet model.
DEFAULT_ENGINE = "markov"


def _connect(db_path: str = FORECAST_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def init_forecast_db(db_path: str = FORECAST_DB) -> None:
    conn = _connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS forecast_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_timestamp TEXT NOT NULL,
            horizon_years INTEGER NOT NULL,
            status TEXT NOT NULL,
            model_engine TEXT,
            metrics_json TEXT NOT NULL,
            message TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS training_series (
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            bridge_name TEXT,
            data_year INTEGER NOT NULL,
            metric TEXT NOT NULL,
            observed_value REAL NOT NULL,
            maintenance INTEGER NOT NULL DEFAULT 0,
            source_db TEXT NOT NULL,
            PRIMARY KEY (state_code, bridge_number, data_year, metric)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS forecast_predictions (
            run_id INTEGER NOT NULL,
            state_code INTEGER NOT NULL,
            bridge_number TEXT NOT NULL,
            bridge_name TEXT,
            metric TEXT NOT NULL,
            forecast_year INTEGER NOT NULL,
            yhat REAL NOT NULL,
            yhat_lower REAL,
            yhat_upper REAL,
            confidence TEXT NOT NULL,
            observation_count INTEGER NOT NULL,
            trend_direction TEXT NOT NULL,
            model_engine TEXT NOT NULL,
            PRIMARY KEY (run_id, state_code, bridge_number, metric, forecast_year),
            FOREIGN KEY (run_id) REFERENCES forecast_runs(id)
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_training_bridge ON training_series(state_code, bridge_number, metric, data_year)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_predictions_bridge ON forecast_predictions(state_code, bridge_number, metric, forecast_year)")
    # Migrate older forecast DBs that predate the maintenance regressor column.
    existing_cols = {row[1] for row in cur.execute("PRAGMA table_info(training_series)").fetchall()}
    if existing_cols and "maintenance" not in existing_cols:
        cur.execute("ALTER TABLE training_series ADD COLUMN maintenance INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()


def _safe_year(value: Any) -> Optional[int]:
    """Parse a 4-digit calendar year from an int/float/str/date-like value."""
    if value is None:
        return None
    try:
        text = str(value).strip()
        if not text:
            return None
        match = re.search(r"(19|20)\d{2}", text)
        if match:
            year = int(match.group(0))
        else:
            year = int(float(text))
        if 1900 <= year <= 2100:
            return year
    except Exception:
        return None
    return None


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        parsed = float(value)
        if 0 <= parsed <= 9:
            return parsed
    except Exception:
        return None
    return None


def _clamp_rating(value: float) -> float:
    return max(0.0, min(9.0, round(float(value), 3)))


def _collect_work_years(src: sqlite3.Connection) -> Dict[Tuple[int, str], set]:
    """Map (state_code, bridge_number) -> set of years a maintenance/work event
    was performed, unioning the ``works`` table and ``bridges.year_work_performed``.
    Each source is best-effort so a missing column/table never breaks training.
    """
    work_years: Dict[Tuple[int, str], set] = {}

    def _add(state: Any, bridge: Any, raw_year: Any) -> None:
        year = _safe_year(raw_year)
        if year is None or state is None or bridge in (None, ""):
            return
        try:
            key = (int(state), str(bridge))
        except (TypeError, ValueError):
            return
        work_years.setdefault(key, set()).add(year)

    for sql in (
        "SELECT state_code, bridge_number, year_work_performed FROM works",
        "SELECT state_code, bridge_number, year_work_performed FROM bridges WHERE year_work_performed IS NOT NULL",
    ):
        try:
            for row in src.execute(sql).fetchall():
                data = dict(row)
                _add(data.get("state_code"), data.get("bridge_number"), data.get("year_work_performed"))
        except Exception:
            continue
    return work_years


def rebuild_training_db(source_db: str = USER_DB, forecast_db: str = FORECAST_DB) -> Dict[str, Any]:
    init_forecast_db(forecast_db)
    if not os.path.exists(source_db):
        return {"status": "error", "message": "Source SNBI database does not exist.", "inserted": 0}

    src = sqlite3.connect(source_db)
    src.row_factory = sqlite3.Row
    try:
        rows_source = src.execute(
            """
            SELECT state_code, bridge_number, bridge_name, data_year,
                   deck_condition_rating, superstructure_condition_rating,
                   substructure_condition_rating, culvert_condition_rating
            FROM bridges
            WHERE data_year IS NOT NULL
            ORDER BY state_code, bridge_number, data_year
            """
        ).fetchall()
        work_years = _collect_work_years(src)
    finally:
        src.close()

    rows: List[Tuple[Any, ...]] = []
    for row in rows_source:
        row_dict = dict(row)
        try:
            year = int(row_dict["data_year"])
        except Exception:
            continue
        bridge_key = (int(row_dict["state_code"]), str(row_dict["bridge_number"]))
        # Maintenance is recorded per bridge (the works table has no per-component
        # column), so the same 0/1 flag applies to all four metrics for a year.
        maintenance = 1 if year in work_years.get(bridge_key, ()) else 0
        for metric, column in METRICS.items():
            observed = _safe_float(row_dict.get(column))
            if observed is None:
                continue
            rows.append((
                int(row_dict["state_code"]),
                str(row_dict["bridge_number"]),
                row_dict.get("bridge_name"),
                year,
                metric,
                observed,
                maintenance,
                source_db,
            ))

    conn = _connect(forecast_db)
    cur = conn.cursor()
    cur.execute("DELETE FROM training_series")
    if rows:
        cur.executemany(
            """
            INSERT OR REPLACE INTO training_series (
                state_code, bridge_number, bridge_name, data_year, metric, observed_value, maintenance, source_db
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    conn.commit()
    distinct_bridges = cur.execute("SELECT COUNT(DISTINCT state_code || '|' || bridge_number) FROM training_series").fetchone()[0]
    year_range = cur.execute("SELECT MIN(data_year), MAX(data_year) FROM training_series").fetchone()
    conn.close()
    return {
        "status": "success",
        "inserted": len(rows),
        "bridges": distinct_bridges,
        "years": {"min": year_range[0], "max": year_range[1]},
        "metrics": list(METRICS.keys()),
    }


def _confidence(observation_count: int) -> str:
    if observation_count >= 5:
        return "high"
    if observation_count >= 3:
        return "medium"
    return "low"


def _trend(values: List[float]) -> str:
    if len(values) < 2:
        return "flat"
    delta = values[-1] - values[0]
    if delta < -0.25:
        return "deteriorating"
    if delta > 0.25:
        return "improving"
    return "flat"


def _prophet_modules():
    try:
        from prophet import Prophet  # type: ignore
        import pandas as pd
        return Prophet, pd
    except Exception:
        return None, None


def _fit_project_prophet(years, values, maint, horizon, Prophet, pd):
    """Fit Prophet with a 0/1 maintenance regressor and project ``horizon`` years.

    The regressor lets Prophet attribute historical condition *bumps* to recorded
    work years, so the underlying trend reflects natural deterioration. The future
    is predicted with maintenance=0 (no future work assumed).
    Returns (raw_predictions, learned_bump_coef, engine_label).
    """
    n = len(years)
    model_df = pd.DataFrame({
        "ds": pd.to_datetime([f"{y}-01-01" for y in years]),
        "y": [float(v) for v in values],
        "maintenance": [int(m) for m in maint],
    })
    distinct = {int(m) for m in maint}
    interior = any(maint[i] for i in range(1, n - 1)) if n >= 3 else False
    # Only trust the regressor when it is identifiable: it varies, has a bump
    # strictly inside the observed range, and there are enough points.
    use_reg = (len(distinct) >= 2) and interior and (n >= 4)

    def _new_model():
        return Prophet(
            yearly_seasonality=False, weekly_seasonality=False, daily_seasonality=False,
            changepoint_prior_scale=0.03, n_changepoints=max(0, n - 2),
        )

    model = _new_model()
    if use_reg:
        model.add_regressor("maintenance", standardize=False, prior_scale=8.0)
    try:
        model.fit(model_df)
    except Exception:
        if not use_reg:
            raise
        use_reg = False
        model = _new_model()
        model.fit(model_df.drop(columns=["maintenance"]))

    coef = 0.0
    if use_reg:
        try:
            from prophet.utilities import regressor_coefficients
            rc = regressor_coefficients(model)
            match = rc[rc["regressor"] == "maintenance"]
            if len(match):
                coef = float(match["coef"].iloc[0])
        except Exception:
            coef = 0.0

    max_year = years[-1]
    future_years = list(range(max_year + 1, max_year + horizon + 1))
    fut = pd.DataFrame({"ds": pd.to_datetime([f"{y}-01-01" for y in future_years])})
    if use_reg:
        fut["maintenance"] = 0
    fc = model.predict(fut)
    raw = []
    for i, year in enumerate(future_years):
        raw.append((
            year,
            float(fc["yhat"].iloc[i]),
            float(fc["yhat_lower"].iloc[i]) if "yhat_lower" in fc.columns else None,
            float(fc["yhat_upper"].iloc[i]) if "yhat_upper" in fc.columns else None,
        ))
    return raw, coef, "prophet"


def _fit_project_numpy(years, values, maint, horizon):
    """Prophet-free fallback: linear fit on non-maintenance points (so repair
    bumps do not flatten the natural decline), projected forward, never rising."""
    import numpy as np
    n = len(years)
    base_idx = [i for i in range(n) if not maint[i]]
    if len(base_idx) >= 2:
        xs = np.array([years[i] for i in base_idx], dtype=float)
        ys = np.array([values[i] for i in base_idx], dtype=float)
    else:
        xs = np.array(years, dtype=float)
        ys = np.array(values, dtype=float)
    slope, intercept = np.polyfit(xs, ys, 1)
    if slope > 0:
        slope = 0.0  # no future work => never improving
    bumps = [values[i] - values[i - 1] for i in range(1, n) if maint[i] and values[i] > values[i - 1]]
    coef = float(np.mean(bumps)) if bumps else 0.0
    resid = float(np.std(ys - (intercept + slope * xs))) if len(xs) >= 2 else 0.5
    band = max(0.3, resid)
    max_year = years[-1]
    future_years = list(range(max_year + 1, max_year + horizon + 1))
    raw = [(year, intercept + slope * year, intercept + slope * year - band, intercept + slope * year + band)
           for year in future_years]
    return raw, coef, "linear"


def _degrade_baseline(raw, last_value, min_decline: float = DEFAULT_ANNUAL_DECLINE):
    """Build the baseline (no future work) forecast.

    Anchored at the last observed value, it follows Prophet's year-over-year
    *shape* but enforces the business invariant that conditions always degrade
    when no work is done: each year drops by at least ``min_decline``, and by
    more whenever Prophet predicts a steeper decline. (Prophet alone often
    predicts a flat line on sparse/sticky rating histories, which read as
    "the forecast never drops" — this guarantees visible deterioration while
    still letting Prophet drive when it sees real decline.) Prophet's prediction
    interval width is preserved and re-anchored around the enforced value.
    """
    baseline = []
    prev = float(last_value)
    prev_pred = float(last_value)
    for (year, yhat, lo, hi) in raw:
        yc = _clamp_rating(yhat)
        prophet_step = yc - prev_pred                  # Prophet's own YoY move
        step = min(prophet_step, -abs(min_decline))    # never rise; drop >= floor
        val = max(0.0, _clamp_rating(prev + step))
        lo_off = (yc - _clamp_rating(lo)) if lo is not None else None
        hi_off = (_clamp_rating(hi) - yc) if hi is not None else None
        baseline.append({
            "year": int(year),
            "yhat": val,
            "yhat_lower": max(0.0, _clamp_rating(val - lo_off)) if lo_off is not None else None,
            "yhat_upper": _clamp_rating(val + hi_off) if hi_off is not None else None,
        })
        prev = val
        prev_pred = yc
    return baseline


def _baseline_decline_step(last_value, baseline):
    """Average per-year decline implied by the baseline; used to keep degrading
    after a simulated maintenance bump. Always returns a negative step."""
    points = [float(last_value)] + [p["yhat"] for p in baseline]
    diffs = [points[i] - points[i - 1] for i in range(1, len(points))]
    if not diffs:
        return -DEFAULT_ANNUAL_DECLINE
    avg = sum(diffs) / len(diffs)
    return avg if avg < 0 else -DEFAULT_ANNUAL_DECLINE


def _project_bridge_metric(ordered_rows, horizon_years, min_decline: float = DEFAULT_ANNUAL_DECLINE):
    """Fit + project one bridge/metric series using ONLY this bridge's own
    observations. Returns baseline (degrading) predictions plus the learned
    maintenance bump and natural decline step. ``min_decline`` is the fallback
    deterioration floor (typically a fleet-derived rate) applied only when this
    bridge's own history is too flat for Prophet to find a real trend."""
    years = [int(r["data_year"]) for r in ordered_rows]
    values = [float(r["observed_value"]) for r in ordered_rows]
    try:
        maint = [int(r["maintenance"] or 0) for r in ordered_rows]
    except (KeyError, IndexError, TypeError):
        maint = [0] * len(years)
    last_value = values[-1]

    Prophet, pd = _prophet_modules()
    if Prophet is not None and pd is not None:
        try:
            raw, coef, engine = _fit_project_prophet(years, values, maint, horizon_years, Prophet, pd)
        except Exception as exc:
            print(f"[forecast] Prophet fit failed, falling back to linear model: {exc}")
            raw, coef, engine = _fit_project_numpy(years, values, maint, horizon_years)
    else:
        print("[forecast] Prophet not importable; using linear fallback model.")
        raw, coef, engine = _fit_project_numpy(years, values, maint, horizon_years)

    baseline = _degrade_baseline(raw, last_value, min_decline)
    return {
        "baseline": baseline,
        "coef": coef,
        "engine": engine,
        "decline_step": _baseline_decline_step(last_value, baseline),
        "last_value": last_value,
        "n": len(years),
    }


def _fleet_decline_rates(conn) -> Dict[str, float]:
    """Typical annual deterioration per metric across the WHOLE fleet.

    For every bridge/metric with at least two observations we fit a line to its
    non-maintenance points and keep the slope when it is declining; the median
    magnitude per metric is the fleet rate. This is used as the per-bridge
    fallback floor so a bridge whose own short history happens to be flat still
    degrades at the rate its component type typically does (decks deteriorate
    faster than substructures, etc.) — i.e. fleet history informs each bridge.
    """
    import numpy as np
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT state_code, bridge_number, metric, data_year, observed_value, maintenance FROM training_series"
    ).fetchall()
    groups: Dict[Tuple[Any, Any, str], List[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault((r["state_code"], r["bridge_number"], str(r["metric"])), []).append(r)

    per_metric: Dict[str, List[float]] = {m: [] for m in METRICS}
    for (_s, _b, metric), grp in groups.items():
        if metric not in per_metric:
            continue
        pts = [(int(x["data_year"]), float(x["observed_value"])) for x in grp if not int(x["maintenance"] or 0)]
        if len(pts) < 2:
            pts = [(int(x["data_year"]), float(x["observed_value"])) for x in grp]
        if len(pts) < 2:
            continue
        xs = np.array([p[0] for p in pts], dtype=float)
        ys = np.array([p[1] for p in pts], dtype=float)
        if xs.max() == xs.min():
            continue
        slope = float(np.polyfit(xs, ys, 1)[0])
        if slope < 0:
            per_metric[metric].append(-slope)

    out: Dict[str, float] = {}
    for metric, vals in per_metric.items():
        if vals:
            vals.sort()
            median = vals[len(vals) // 2]
            out[metric] = max(0.02, min(1.0, median))
        else:
            out[metric] = DEFAULT_ANNUAL_DECLINE
    return out


def train_prophet_forecasts(
    horizon_years: int = 10,
    forecast_db: str = FORECAST_DB,
    state_code: Optional[int] = None,
    bridge_number: Optional[str] = None,
) -> Dict[str, Any]:
    init_forecast_db(forecast_db)

    conn = _connect(forecast_db)
    cur = conn.cursor()
    run_timestamp = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    scope_message = f"bridge {state_code}/{bridge_number}" if state_code is not None and bridge_number else "all bridges"
    cur.execute(
        "INSERT INTO forecast_runs (run_timestamp, horizon_years, status, model_engine, metrics_json, message) VALUES (?, ?, ?, ?, ?, ?)",
        (run_timestamp, horizon_years, "running", "prophet", json.dumps(list(METRICS.keys())), f"Training {scope_message}"),
    )
    run_id = int(cur.lastrowid)
    conn.commit()

    conn.row_factory = sqlite3.Row
    query = """
        SELECT state_code, bridge_number, bridge_name, data_year, metric, observed_value, maintenance
        FROM training_series
    """
    params: Tuple[Any, ...] = ()
    if state_code is not None and bridge_number:
        query += " WHERE state_code=? AND bridge_number=?"
        params = (state_code, bridge_number)
    query += " ORDER BY state_code, bridge_number, metric, data_year"
    training_rows = conn.execute(query, params).fetchall()
    if not training_rows:
        cur.execute(
            "UPDATE forecast_runs SET status=?, message=? WHERE id=?",
            ("error", "No training rows found. Build the training database first.", run_id),
        )
        conn.commit()
        conn.close()
        return {"status": "error", "run_id": run_id, "message": "No training rows found."}

    # Fleet-wide typical deterioration per metric, used as the fallback floor
    # for bridges whose own history is too flat to imply a rate.
    fleet_rates = _fleet_decline_rates(conn)

    trained = 0
    skipped = 0
    prediction_count = 0
    grouped: Dict[Tuple[int, str, str], List[sqlite3.Row]] = {}
    for row in training_rows:
        grouped.setdefault((int(row["state_code"]), str(row["bridge_number"]), str(row["metric"])), []).append(row)

    for (state_code, bridge_number, metric), group_rows in grouped.items():
        by_year: Dict[int, sqlite3.Row] = {}
        for row in group_rows:
            by_year[int(row["data_year"])] = row
        ordered_rows = [by_year[year] for year in sorted(by_year)]
        if len(ordered_rows) < 3:
            skipped += 1
            continue

        try:
            projection = _project_bridge_metric(
                ordered_rows, horizon_years, fleet_rates.get(metric, DEFAULT_ANNUAL_DECLINE)
            )
        except Exception:
            skipped += 1
            continue
        if not projection["baseline"]:
            skipped += 1
            continue

        observation_count = len(ordered_rows)
        confidence = _confidence(observation_count)
        # Trend reflects the projected (no-future-work) curve, which degrades.
        trend = _trend([projection["last_value"]] + [p["yhat"] for p in projection["baseline"]])
        bridge_names = [row["bridge_name"] for row in ordered_rows if row["bridge_name"]]
        bridge_name = bridge_names[-1] if bridge_names else None
        engine = projection["engine"]
        prediction_rows: List[Tuple[Any, ...]] = []
        for point in projection["baseline"]:
            prediction_rows.append((
                run_id,
                int(state_code),
                str(bridge_number),
                bridge_name,
                metric,
                int(point["year"]),
                _clamp_rating(point["yhat"]),
                point["yhat_lower"],
                point["yhat_upper"],
                confidence,
                observation_count,
                trend,
                engine,
            ))
        if prediction_rows:
            cur.executemany(
                """
                INSERT OR REPLACE INTO forecast_predictions (
                    run_id, state_code, bridge_number, bridge_name, metric, forecast_year, yhat,
                    yhat_lower, yhat_upper, confidence, observation_count, trend_direction, model_engine
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                prediction_rows,
            )
            prediction_count += len(prediction_rows)
            cur.execute(
                "UPDATE forecast_runs SET message=? WHERE id=?",
                (f"Trained {trained + 1} bridge/metric models; skipped {skipped}.", run_id),
            )
            conn.commit()
        trained += 1

    cur.execute(
        "UPDATE forecast_runs SET status=?, message=? WHERE id=?",
        ("success", f"Trained {trained} bridge/metric models; skipped {skipped}.", run_id),
    )
    conn.commit()
    conn.close()
    return {
        "status": "success",
        "run_id": run_id,
        "trained_models": trained,
        "skipped_models": skipped,
        "predictions": prediction_count,
        "horizon_years": horizon_years,
        "engine": "prophet",
    }


def train_markov_forecasts(
    horizon_years: int = 10,
    forecast_db: str = FORECAST_DB,
    source_db: str = USER_DB,
    state_code: Optional[int] = None,
    bridge_number: Optional[str] = None,
) -> Dict[str, Any]:
    """Forecast condition with the fleet-trained Markov transition model.

    Builds (or reuses) the fleet transition matrices, then propagates each target
    bridge's current condition state forward. Writes predictions to the same
    forecast_predictions table as Prophet so the read path is unchanged."""
    from . import markov as MK

    init_forecast_db(forecast_db)
    model = MK.build_markov_model(source_db)

    # Confidence comes from measured accuracy on held-out inspection years
    # (rolling-origin backtest), not from how many rows a bridge has.
    backtest: Dict[str, Any] = {}
    bt_conf: Dict[str, Optional[str]] = {}
    try:
        backtest = backtest_markov(source_db)
        for m in METRICS:
            entry = (backtest.get("per_metric") or {}).get(m) or {}
            bt_conf[m] = backtest_confidence(entry.get("mae"))
    except Exception as exc:
        print(f"[forecast] backtest unavailable: {exc}")

    conn = _connect(forecast_db)
    cur = conn.cursor()
    run_timestamp = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    scope = f"bridge {state_code}/{bridge_number}" if state_code is not None and bridge_number else "all bridges"
    cur.execute(
        "INSERT INTO forecast_runs (run_timestamp, horizon_years, status, model_engine, metrics_json, message) VALUES (?, ?, ?, ?, ?, ?)",
        (run_timestamp, horizon_years, "running", "markov", json.dumps(list(METRICS.keys())),
         f"Markov training {scope}; {model['transitions']} fleet transitions"),
    )
    run_id = int(cur.lastrowid)
    conn.commit()

    if not model["current"]:
        cur.execute(
            "UPDATE forecast_runs SET status=?, message=? WHERE id=?",
            ("error", "No condition observations found. Load bridge data first.", run_id),
        )
        conn.commit()
        conn.close()
        return {"status": "error", "run_id": run_id, "message": "No condition observations found."}

    if state_code is not None and bridge_number:
        targets = [(int(state_code), str(bridge_number))]
    else:
        targets = list(model["current"].keys())

    trained = 0
    skipped = 0
    prediction_count = 0
    for (s, b) in targets:
        cur_metrics = model["current"].get((s, b))
        if not cur_metrics:
            skipped += 1
            continue
        meta = model["meta"].get((s, b), {})
        year_built = meta.get("year_built")
        tb = MK.traffic_band(meta.get("adtt"), meta.get("adt"))
        county = meta.get("county_code")
        bridge_name = meta.get("bridge_name")
        obs_counts = model["obs_counts"].get((s, b), {})
        latest_year = max(yr for (yr, _v) in cur_metrics.values())
        base_age = (latest_year - year_built) if year_built else None

        prediction_rows: List[Tuple[Any, ...]] = []
        for metric, (_yr, rating) in cur_metrics.items():
            future = MK.forecast_metric(model, metric, rating, base_age, tb, horizon_years, latest_year, county=county)
            if not future:
                continue
            obs_n = int(obs_counts.get(metric, 1))
            # Fleet backtest accuracy first; row-count heuristic only when no
            # measured error exists. A single-observation bridge still caps out
            # at "medium" since its own starting state is less certain.
            confidence = bt_conf.get(metric) or ("high" if obs_n >= 4 else "medium" if obs_n >= 2 else "low")
            if obs_n < 2 and confidence == "high":
                confidence = "medium"
            trend = _trend([float(rating)] + [p["yhat"] for p in future])
            for p in future:
                prediction_rows.append((
                    run_id, int(s), str(b), bridge_name, metric, int(p["year"]),
                    _clamp_rating(p["yhat"]),
                    _clamp_rating(p["yhat_lower"]) if p["yhat_lower"] is not None else None,
                    _clamp_rating(p["yhat_upper"]) if p["yhat_upper"] is not None else None,
                    confidence, obs_n, trend, "markov",
                ))
        if prediction_rows:
            cur.executemany(
                """
                INSERT OR REPLACE INTO forecast_predictions (
                    run_id, state_code, bridge_number, bridge_name, metric, forecast_year, yhat,
                    yhat_lower, yhat_upper, confidence, observation_count, trend_direction, model_engine
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                prediction_rows,
            )
            prediction_count += len(prediction_rows)
            trained += 1
        else:
            skipped += 1

    cur.execute(
        "UPDATE forecast_runs SET status=?, message=? WHERE id=?",
        ("success",
         f"Markov: trained {trained} bridges, skipped {skipped}, learned from {model['transitions']} fleet transitions.",
         run_id),
    )
    conn.commit()
    conn.close()
    return {
        "status": "success",
        "run_id": run_id,
        "trained_models": trained,
        "skipped_models": skipped,
        "predictions": prediction_count,
        "horizon_years": horizon_years,
        "engine": "markov",
        "fleet_transitions": model["transitions"],
        "backtest": backtest or None,
    }


def train_condition_forecasts(
    horizon_years: int = 10,
    forecast_db: str = FORECAST_DB,
    state_code: Optional[int] = None,
    bridge_number: Optional[str] = None,
    engine: str = DEFAULT_ENGINE,
    source_db: str = USER_DB,
) -> Dict[str, Any]:
    """Dispatch to the selected forecasting engine (default: Markov)."""
    if engine == "prophet":
        return train_prophet_forecasts(horizon_years, forecast_db, state_code, bridge_number)
    return train_markov_forecasts(horizon_years, forecast_db, source_db, state_code, bridge_number)


def get_forecast_summary(forecast_db: str = FORECAST_DB) -> Dict[str, Any]:
    init_forecast_db(forecast_db)
    conn = _connect(forecast_db)
    try:
        cur = conn.cursor()
        latest_run = cur.execute(
            "SELECT id, run_timestamp, horizon_years, status, model_engine, message FROM forecast_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        training_count = cur.execute("SELECT COUNT(*) FROM training_series").fetchone()[0]
        bridge_count = cur.execute("SELECT COUNT(DISTINCT state_code || '|' || bridge_number) FROM training_series").fetchone()[0]
        prediction_count = cur.execute("SELECT COUNT(*) FROM forecast_predictions").fetchone()[0]
        years = cur.execute("SELECT MIN(data_year), MAX(data_year) FROM training_series").fetchone()
        return {
            "training_rows": training_count,
            "training_bridges": bridge_count,
            "prediction_rows": prediction_count,
            "training_years": {"min": years[0], "max": years[1]},
            "latest_run": {
                "id": latest_run[0],
                "run_timestamp": latest_run[1],
                "horizon_years": latest_run[2],
                "status": latest_run[3],
                "model_engine": latest_run[4],
                "message": latest_run[5],
            } if latest_run else None,
            "metrics": list(METRICS.keys()),
        }
    finally:
        conn.close()


def get_forecast_bridges(limit: int = 500, forecast_db: str = FORECAST_DB) -> List[Dict[str, Any]]:
    init_forecast_db(forecast_db)
    conn = _connect(forecast_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT state_code, bridge_number, MAX(bridge_name) AS bridge_name,
                   COUNT(DISTINCT data_year) AS observation_years
            FROM training_series
            GROUP BY state_code, bridge_number
            ORDER BY state_code, bridge_number
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_bridge_forecast(state_code: int, bridge_number: str, forecast_db: str = FORECAST_DB) -> Dict[str, Any]:
    init_forecast_db(forecast_db)
    conn = _connect(forecast_db)
    conn.row_factory = sqlite3.Row
    try:
        # Resolve the latest run that actually contains THIS bridge. Per-bridge
        # training creates a run holding only that bridge, so keying off the
        # global latest successful run would hide every other bridge's forecast.
        latest_run = conn.execute(
            "SELECT MAX(run_id) FROM forecast_predictions WHERE state_code=? AND bridge_number=?",
            (state_code, bridge_number),
        ).fetchone()[0]
        if latest_run is None:
            latest_run = conn.execute("SELECT MAX(id) FROM forecast_runs WHERE status='success'").fetchone()[0]
        observed_rows = conn.execute(
            """
            SELECT data_year AS year, metric, observed_value AS value
            FROM training_series
            WHERE state_code=? AND bridge_number=?
            ORDER BY data_year, metric
            """,
            (state_code, bridge_number),
        ).fetchall()
        if latest_run:
            pred_rows = conn.execute(
                """
                SELECT forecast_year AS year, metric, yhat, yhat_lower, yhat_upper,
                       confidence, observation_count, trend_direction, model_engine
                FROM forecast_predictions
                WHERE run_id=? AND state_code=? AND bridge_number=?
                ORDER BY forecast_year, metric
                """,
                (latest_run, state_code, bridge_number),
            ).fetchall()
        else:
            pred_rows = []

        by_metric: Dict[str, Dict[str, Any]] = {}
        for metric in METRICS:
            metric_observed = [dict(row) for row in observed_rows if row["metric"] == metric]
            metric_forecast = [dict(row) for row in pred_rows if row["metric"] == metric]
            by_metric[metric] = {"observed": metric_observed, "forecast": metric_forecast}

        engine = pred_rows[0]["model_engine"] if pred_rows else None

        return {
            "state_code": state_code,
            "bridge_number": bridge_number,
            "run_id": latest_run,
            "engine": engine,
            "metrics": by_metric,
        }
    finally:
        conn.close()


def ensure_training_data(
    state_code: int,
    bridge_number: str,
    source_db: str = USER_DB,
    forecast_db: str = FORECAST_DB,
) -> bool:
    """Make sure the bridge has training rows so a forecast can be generated on
    demand (e.g. when the history modal auto-runs). Rebuilds from the main DB if
    the bridge is missing. Returns True when training rows exist afterwards."""
    init_forecast_db(forecast_db)
    conn = _connect(forecast_db)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM training_series WHERE state_code=? AND bridge_number=?",
            (state_code, bridge_number),
        ).fetchone()[0]
    finally:
        conn.close()
    if count == 0:
        rebuild_training_db(source_db, forecast_db)
        conn = _connect(forecast_db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM training_series WHERE state_code=? AND bridge_number=?",
                (state_code, bridge_number),
            ).fetchone()[0]
        finally:
            conn.close()
    return count > 0


def _simulate_markov(
    state_code: int,
    bridge_number: str,
    maintenance_year: int,
    horizon_years: int,
    forecast_db: str,
    source_db: str,
) -> Dict[str, Any]:
    """Maintenance what-if using the Markov model (default engine)."""
    from . import markov as MK

    init_forecast_db(forecast_db)
    conn = _connect(forecast_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT data_year, metric, observed_value
            FROM training_series
            WHERE state_code=? AND bridge_number=?
            ORDER BY metric, data_year
            """,
            (state_code, bridge_number),
        ).fetchall()
    finally:
        conn.close()
    obs_by_metric: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        obs_by_metric.setdefault(str(r["metric"]), []).append(
            {"year": int(r["data_year"]), "value": float(r["observed_value"])}
        )

    model = MK.build_markov_model(source_db)
    key = (int(state_code), str(bridge_number))
    meta = model["meta"].get(key, {})
    year_built = meta.get("year_built")
    tb = MK.traffic_band(meta.get("adtt"), meta.get("adt"))
    county = meta.get("county_code")

    by_metric: Dict[str, Dict[str, Any]] = {}
    for metric in METRICS:
        observed = obs_by_metric.get(metric, [])
        if not observed:
            by_metric[metric] = {"observed": [], "forecast": [], "simulated": [],
                                 "bump": 0.0, "bump_source": "default", "engine": "markov"}
            continue
        latest_year = observed[-1]["year"]
        rating = observed[-1]["value"]
        base_age = (latest_year - year_built) if year_built else None
        baseline = MK.forecast_metric(model, metric, rating, base_age, tb, horizon_years, latest_year, county=county)
        bump, bump_source = MK.recovery_bump(model, metric, county=county)
        simulated = MK.simulate_metric(
            model, metric, rating, base_age, tb, horizon_years, latest_year, maintenance_year, bump, county=county
        )
        by_metric[metric] = {
            "observed": observed,
            "forecast": [{"year": p["year"], "yhat": p["yhat"], "yhat_lower": p["yhat_lower"], "yhat_upper": p["yhat_upper"]} for p in baseline],
            "simulated": simulated,
            "bump": round(float(bump), 2),
            "bump_source": bump_source,
            "engine": "markov",
        }

    return {
        "state_code": state_code,
        "bridge_number": bridge_number,
        "maintenance_year": maintenance_year,
        "horizon_years": horizon_years,
        "engine": "markov",
        "metrics": by_metric,
    }


def simulate_bridge_forecast(
    state_code: int,
    bridge_number: str,
    maintenance_year: int,
    horizon_years: int = 10,
    forecast_db: str = FORECAST_DB,
    engine: str = DEFAULT_ENGINE,
    source_db: str = USER_DB,
) -> Dict[str, Any]:
    """What-if: simulate a maintenance event in ``maintenance_year`` and return
    its effect on the rest of each metric's forecast.

    The baseline (no future work) degrades; at ``maintenance_year`` the rating is
    bumped up by the model's learned maintenance effect (or a default when the
    bridge has no usable historical work signal), then resumes degrading. Computed
    on the fly — nothing is written to the database.
    """
    if engine != "prophet":
        return _simulate_markov(state_code, bridge_number, maintenance_year, horizon_years, forecast_db, source_db)

    init_forecast_db(forecast_db)
    conn = _connect(forecast_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT data_year, metric, observed_value, maintenance, bridge_name
            FROM training_series
            WHERE state_code=? AND bridge_number=?
            ORDER BY metric, data_year
            """,
            (state_code, bridge_number),
        ).fetchall()
        fleet_rates = _fleet_decline_rates(conn)
    finally:
        conn.close()

    grouped: Dict[str, Dict[int, sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["metric"]), {})[int(row["data_year"])] = row

    by_metric: Dict[str, Dict[str, Any]] = {}
    for metric in METRICS:
        year_map = grouped.get(metric, {})
        ordered_rows = [year_map[year] for year in sorted(year_map)]
        observed = [{"year": int(r["data_year"]), "value": float(r["observed_value"])} for r in ordered_rows]
        if len(ordered_rows) < 3:
            by_metric[metric] = {
                "observed": observed, "forecast": [], "simulated": [],
                "bump": 0.0, "bump_source": "default",
            }
            continue

        try:
            projection = _project_bridge_metric(
                ordered_rows, horizon_years, fleet_rates.get(metric, DEFAULT_ANNUAL_DECLINE)
            )
        except Exception:
            by_metric[metric] = {
                "observed": observed, "forecast": [], "simulated": [],
                "bump": 0.0, "bump_source": "default",
            }
            continue

        baseline = projection["baseline"]
        coef = projection["coef"]
        decline_step = projection["decline_step"]
        if coef >= MIN_LEARNED_BUMP:
            effective_bump = min(MAX_LEARNED_BUMP, coef)
            bump_source = "learned"
        else:
            effective_bump = DEFAULT_MAINTENANCE_BUMP
            bump_source = "default"

        simulated: List[Dict[str, Any]] = []
        prev: Optional[float] = None
        for point in baseline:
            year = int(point["year"])
            if year < maintenance_year:
                val = point["yhat"]
            elif year == maintenance_year:
                # Maintenance lifts the rating above the baseline (the only point
                # allowed to rise); cap at the SNBI maximum of 9.
                val = _clamp_rating(min(9.0, point["yhat"] + effective_bump))
            else:
                # Resume natural degradation from the bumped value.
                base = prev if prev is not None else point["yhat"]
                val = _clamp_rating(max(0.0, base + decline_step))
            simulated.append({"year": year, "yhat": val})
            prev = val

        by_metric[metric] = {
            "observed": observed,
            "forecast": baseline,
            "simulated": simulated,
            "bump": round(float(effective_bump), 2),
            "bump_source": bump_source,
            "engine": projection["engine"],
        }

    engine = next((m.get("engine") for m in by_metric.values() if m.get("engine")), None)

    return {
        "state_code": state_code,
        "bridge_number": bridge_number,
        "maintenance_year": maintenance_year,
        "horizon_years": horizon_years,
        "engine": engine,
        "metrics": by_metric,
    }

# ====================== ROLLING-ORIGIN BACKTEST ======================
# Measured forecast error on held-out inspection years. This is the honest
# basis for the confidence shown next to forecasts: it reflects how well the
# fleet model actually predicted the years it did not see, instead of how many
# rows a bridge happens to have.

_BACKTEST_CACHE: Dict[tuple, Dict[str, Any]] = {}


def backtest_markov(source_db: str = USER_DB, holdout_years: int = 1) -> Dict[str, Any]:
    """Rolling-origin evaluation of the Markov engine.

    Trains the fleet transition model only on inspection years <= (latest -
    ``holdout_years``), forecasts every bridge forward from its last training
    observation, and scores the predictions against the actual held-out
    ratings. Returns per-component and overall MAE, bias and the share of
    predictions within +/-1 rating point.
    """
    from . import markov as MK

    holdout_years = max(1, min(5, int(holdout_years)))
    cache_key = (MK._cache_key(source_db), holdout_years)
    if cache_key[0] is not None and cache_key in _BACKTEST_CACHE:
        return _BACKTEST_CACHE[cache_key]

    conn = sqlite3.connect(source_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT state_code, bridge_number, data_year,
                   deck_condition_rating, superstructure_condition_rating,
                   substructure_condition_rating, culvert_condition_rating
            FROM bridges
            WHERE data_year IS NOT NULL
            ORDER BY state_code, bridge_number, data_year
            """
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return {"status": "error", "message": "No inspection data to backtest."}

    max_year = max(int(r["data_year"]) for r in rows)
    cutoff = max_year - holdout_years
    trained = MK.build_markov_model(source_db, max_year=cutoff)
    if not trained["current"]:
        return {"status": "error", "message": f"No training data at or before {cutoff}."}

    metric_cols = {
        "deck": "deck_condition_rating",
        "superstructure": "superstructure_condition_rating",
        "substructure": "substructure_condition_rating",
        "culvert": "culvert_condition_rating",
    }
    by_bridge: Dict[tuple, List[sqlite3.Row]] = {}
    for r in rows:
        try:
            by_bridge.setdefault((int(r["state_code"]), str(r["bridge_number"])), []).append(r)
        except (TypeError, ValueError):
            continue

    scores: Dict[str, Dict[str, float]] = {
        m: {"n": 0, "abs_err": 0.0, "err": 0.0, "within_1": 0} for m in metric_cols
    }
    for key, recs in by_bridge.items():
        meta = trained["meta"].get(key)
        if not meta:
            continue  # bridge has no data before the cutoff
        tb = MK.traffic_band(meta.get("adtt"), meta.get("adt"))
        county = meta.get("county_code")
        year_built = meta.get("year_built")
        for metric, col in metric_cols.items():
            seq = [(int(r["data_year"]), MK._safe_rating(r[col])) for r in recs]
            seq = [(y, v) for y, v in seq if v is not None]
            train_obs = [(y, v) for y, v in seq if y <= cutoff]
            test_obs = [(y, v) for y, v in seq if y > cutoff]
            if not train_obs or not test_obs:
                continue
            y0, r0 = train_obs[-1]
            base_age = (y0 - year_built) if year_built else None
            horizon = max(y for y, _v in test_obs) - y0
            if horizon <= 0:
                continue
            future = MK.forecast_metric(trained, metric, r0, base_age, tb, horizon, y0, county=county)
            pred_by_year = {p["year"]: p["yhat"] for p in future}
            for y, actual in test_obs:
                pred = pred_by_year.get(y)
                if pred is None:
                    continue
                err = float(pred) - float(actual)
                s = scores[metric]
                s["n"] += 1
                s["abs_err"] += abs(err)
                s["err"] += err
                s["within_1"] += 1 if abs(err) <= 1.0 else 0

    per_metric: Dict[str, Any] = {}
    tot_n = tot_abs = tot_err = tot_w1 = 0.0
    for metric, s in scores.items():
        n = s["n"]
        if n == 0:
            per_metric[metric] = {"n": 0}
            continue
        per_metric[metric] = {
            "n": int(n),
            "mae": round(s["abs_err"] / n, 3),
            "bias": round(s["err"] / n, 3),
            "within_1_pct": round(100.0 * s["within_1"] / n, 1),
        }
        tot_n += n
        tot_abs += s["abs_err"]
        tot_err += s["err"]
        tot_w1 += s["within_1"]

    result = {
        "status": "success",
        "engine": "markov",
        "cutoff_year": cutoff,
        "holdout_years": holdout_years,
        "per_metric": per_metric,
        "overall": None if tot_n == 0 else {
            "n": int(tot_n),
            "mae": round(tot_abs / tot_n, 3),
            "bias": round(tot_err / tot_n, 3),
            "within_1_pct": round(100.0 * tot_w1 / tot_n, 1),
        },
    }
    if cache_key[0] is not None:
        _BACKTEST_CACHE.clear()
        _BACKTEST_CACHE[cache_key] = result
    return result


def backtest_confidence(mae: Optional[float]) -> Optional[str]:
    """Map measured backtest MAE (rating points) to a confidence label."""
    if mae is None:
        return None
    if mae <= 0.5:
        return "high"
    if mae <= 1.0:
        return "medium"
    return "low"
