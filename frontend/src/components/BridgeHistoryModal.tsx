// frontend/src/components/BridgeHistoryModal.tsx
// Bridge condition history + maintenance-aware forecast overlay. Observed ratings
// are solid lines; the baseline forecast (no future work) is dashed and strictly
// degrading. A future-year selector simulates a maintenance event and overlays a
// "what-if" line that bumps up at the chosen year then keeps degrading.
//
// Lines are drawn with a tiny per-metric vertical offset so deck/super/sub stay
// visible when their ratings coincide; tooltips always show the TRUE rating.
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import axios from 'axios';
import {
  ComposedChart, Area, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine,
} from 'recharts';

// Integer condition-rating ticks (0..9) so the axis reads every grade.
const RATING_TICKS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9];
import { Loader2, History, Sparkles, Wrench } from 'lucide-react';
import { Modal, Button } from './ui';

interface HistoryPoint { year: number; deck: number | null; superstructure: number | null; substructure: number | null; source?: string; }
interface HistoryEvent { year: number; event_type: string; label: string; }
interface BridgeHistoryData {
  bridge_name?: string;
  year_built?: number;
  deck_condition_rating?: number | string | null;
  superstructure_condition_rating?: number | string | null;
  substructure_condition_rating?: number | string | null;
  historical_points?: HistoryPoint[];
  history_events?: HistoryEvent[];
  condition_trend?: { years: number[]; deck: Array<number | null>; superstructure: Array<number | null>; substructure: Array<number | null> };
}

interface ForecastPoint { year: number; yhat: number; yhat_lower?: number; yhat_upper?: number; confidence?: string; trend_direction?: string; }
interface SimPoint { year: number; yhat: number; }
interface MetricSeries { observed?: Array<{ year: number; value: number }>; forecast?: ForecastPoint[]; simulated?: SimPoint[]; bump?: number; bump_source?: 'learned' | 'default'; }
interface BridgeForecast { run_id?: number; engine?: string | null; metrics?: Record<string, MetricSeries>; }

interface Props {
  stateCode: number | string;
  bridgeNumber: string;
  bridgeName?: string;
  onClose: () => void;
  embedded?: boolean;
}

interface ChartRow {
  year: number;
  deck?: number | null; superstructure?: number | null; substructure?: number | null;
  deck_f?: number | null; super_f?: number | null; sub_f?: number | null;
  deck_lo?: number | null; deck_hi?: number | null;
  deck_band?: [number, number] | null;
  deck_s?: number | null; super_s?: number | null; sub_s?: number | null;
}

// Visual-only vertical offsets so coincident lines don't hide each other.
const OFFSET = { deck: 0.07, superstructure: 0, substructure: -0.07 } as const;
const off = (v: number | null | undefined, k: keyof typeof OFFSET) => (v == null ? null : v + OFFSET[k]);

// Map each Line's display name back to its offset so the tooltip can show the
// true rating rather than the offset plotting value.
const NAME_OFFSET: Record<string, number> = {
  Deck: OFFSET.deck, Superstructure: OFFSET.superstructure, Substructure: OFFSET.substructure,
  'Deck (forecast)': OFFSET.deck, 'Super (forecast)': OFFSET.superstructure, 'Sub (forecast)': OFFSET.substructure,
  'Deck (with work)': OFFSET.deck, 'Super (with work)': OFFSET.superstructure, 'Sub (with work)': OFFSET.substructure,
  'Deck lower': OFFSET.deck, 'Deck upper': OFFSET.deck,
};
const tipFormatter = (value: any, name: string) => {
  if (value == null) return ['—', name];
  // Confidence band arrives as a [lower, upper] pair — show it as a range.
  if (Array.isArray(value)) {
    const ofs = OFFSET.deck;
    const [lo, hi] = value.map((v) => Number(v) - ofs);
    return [`${lo.toFixed(1)} – ${hi.toFixed(1)}`, name];
  }
  const t = Number(value) - (NAME_OFFSET[name] ?? 0);
  return [Number.isInteger(t) ? String(t) : t.toFixed(2), name];
};

const ratingColor = (val: number | string | null | undefined) => {
  const n = Number(val);
  if (isNaN(n) || val == null) return '#94a3b8';
  if (n >= 7) return '#22c55e';
  if (n >= 5) return '#eab308';
  return '#ef4444';
};
const ratingLabel = (val: number | string | null | undefined) => {
  const n = Number(val);
  if (isNaN(n) || val == null) return 'N/A';
  if (n >= 7) return `Good (${n})`;
  if (n >= 5) return `Fair (${n})`;
  return `Poor (${n})`;
};

const DEFAULT_HORIZON = 10;

// Chart components (color encodes which member; line style encodes the scenario).
const LEGEND_COMPONENTS = [
  { label: 'Deck', color: '#22c55e' },
  { label: 'Superstructure', color: '#eab308' },
  { label: 'Substructure', color: '#ef4444' },
] as const;

// A short SVG sample of a line style, used in the custom legend.
const LineSample: React.FC<{ dash?: string }> = ({ dash }) => (
  <svg width="28" height="10" className="shrink-0" aria-hidden>
    <line x1="1" y1="5" x2="27" y2="5" stroke="#64748b" strokeWidth="2.5" strokeLinecap="round" strokeDasharray={dash} />
  </svg>
);

// Visual styling per event type (shared by the chart markers + timeline).
const EVENT_STYLE: Record<string, { color: string; label: string }> = {
  work: { color: '#10b981', label: 'Work' },
  posting: { color: '#f59e0b', label: 'Posting' },
  inspection: { color: '#6366f1', label: 'Inspection' },
  milestone: { color: '#8b5cf6', label: 'Milestone' },
};
const eventStyle = (type: string) => EVENT_STYLE[type] || { color: '#6366f1', label: type };

const BridgeHistoryModal: React.FC<Props> = ({ stateCode, bridgeNumber, bridgeName, onClose, embedded }) => {
  const [data, setData] = useState<BridgeHistoryData | null>(null);
  const [forecast, setForecast] = useState<BridgeForecast | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [training, setTraining] = useState(false);
  const [simYear, setSimYear] = useState<number | ''>('');
  const [simForecast, setSimForecast] = useState<BridgeForecast | null>(null);
  const [simLoading, setSimLoading] = useState(false);
  const [horizonYears, setHorizonYears] = useState(DEFAULT_HORIZON);
  const autoRanRef = useRef<string | null>(null);
  const appliedHorizonRef = useRef<number>(DEFAULT_HORIZON);

  const loadForecast = useCallback(async (): Promise<BridgeForecast | null> => {
    try {
      const res = await axios.get<BridgeForecast>(`/api/forecasts/bridge/${stateCode}/${bridgeNumber}`);
      const fc = res.data && res.data.metrics ? res.data : null;
      setForecast(fc);
      return fc;
    } catch {
      setForecast(null);
      return null;
    }
  }, [stateCode, bridgeNumber]);

  const generateForecast = useCallback(async (horizon: number) => {
    setTraining(true);
    appliedHorizonRef.current = horizon;
    try {
      await axios.post(`/api/forecasts/train-bridge/${stateCode}/${bridgeNumber}?horizon_years=${horizon}`);
      await loadForecast();
    } catch {
      /* surfaced via empty forecast */
    } finally {
      setTraining(false);
    }
  }, [stateCode, bridgeNumber, loadForecast]);

  useEffect(() => {
    const key = `${stateCode}-${bridgeNumber}`;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setSimYear('');
    setSimForecast(null);
    (async () => {
      try {
        const res = await axios.get<BridgeHistoryData>(`/api/bridge-history/${stateCode}/${bridgeNumber}`);
        if (!cancelled) setData(res.data);
      } catch {
        if (!cancelled) setError('Failed to load bridge history');
      } finally {
        if (!cancelled) setLoading(false);
      }
      // Auto-run the forecast on open (no button click). Retrain once per bridge
      // per session if there are no predictions yet OR the stored forecast is not
      // net-degrading (i.e. it's stale data from an older model — the current
      // model always projects a decline when no future work is assumed).
      const fc = await loadForecast();
      const metricsArr = fc?.metrics ? Object.values(fc.metrics) : [];
      const hasPoints = metricsArr.some((m) => (m.forecast || []).length > 0);
      const netDegrading = hasPoints && metricsArr.every((m) => {
        const f = m.forecast || [];
        if (f.length < 2) return true;
        return f[f.length - 1].yhat < f[0].yhat - 1e-6;
      });
      if ((!hasPoints || !netDegrading) && !cancelled && autoRanRef.current !== key) {
        autoRanRef.current = key;
        await generateForecast(horizonYears);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stateCode, bridgeNumber]);

  const runSimulation = useCallback(async (year: number, horizon: number) => {
    setSimLoading(true);
    try {
      const res = await axios.get<BridgeForecast>(
        `/api/forecasts/bridge/${stateCode}/${bridgeNumber}/simulate?maintenance_year=${year}&horizon_years=${horizon}`
      );
      setSimForecast(res.data && res.data.metrics ? res.data : null);
    } catch {
      setSimForecast(null);
    } finally {
      setSimLoading(false);
    }
  }, [stateCode, bridgeNumber]);

  const commitHorizon = useCallback(() => {
    if (horizonYears === appliedHorizonRef.current) return;
    generateForecast(horizonYears);
    if (simYear !== '') runSimulation(Number(simYear), horizonYears);
  }, [horizonYears, simYear, generateForecast, runSimulation]);

  const observed: HistoryPoint[] = useMemo(() => (
    data?.historical_points && data.historical_points.length > 0
      ? [...data.historical_points].sort((a, b) => a.year - b.year)
      : data?.condition_trend && data.condition_trend.years.length > 0
        ? data.condition_trend.years.map((yr, i) => ({
            year: yr,
            deck: data.condition_trend!.deck[i],
            superstructure: data.condition_trend!.superstructure[i],
            substructure: data.condition_trend!.substructure[i],
          }))
        : []
  ), [data]);

  const fc = forecast?.metrics;
  const deckFc = fc?.deck?.forecast || [];
  const superFc = fc?.superstructure?.forecast || [];
  const subFc = fc?.substructure?.forecast || [];
  const forecastStart = [deckFc, superFc, subFc].flatMap((a) => a.map((p) => p.year)).reduce((m, y) => Math.min(m, y), Infinity);

  const forecastYears = useMemo(() => {
    const ys = new Set<number>();
    [deckFc, superFc, subFc].forEach((arr) => arr.forEach((p) => ys.add(p.year)));
    return Array.from(ys).sort((a, b) => a - b);
  }, [deckFc, superFc, subFc]);

  const simMetrics = simForecast?.metrics;

  // Merge observed + forecast + simulation into one year-keyed series (offsets applied).
  const series = useMemo(() => {
    const rows = new Map<number, ChartRow>();
    const setRow = (year: number, patch: Partial<ChartRow>) =>
      rows.set(year, { ...(rows.get(year) || { year }), year, ...patch });

    for (const p of observed) {
      setRow(p.year, { deck: off(p.deck, 'deck'), superstructure: off(p.superstructure, 'superstructure'), substructure: off(p.substructure, 'substructure') });
    }
    for (const p of deckFc) {
      const lo = off(p.yhat_lower, 'deck');
      const hi = off(p.yhat_upper, 'deck');
      setRow(p.year, {
        deck_f: off(p.yhat, 'deck'), deck_lo: lo, deck_hi: hi,
        deck_band: (lo != null && hi != null) ? [lo, hi] : null,
      });
    }
    for (const p of superFc) setRow(p.year, { super_f: off(p.yhat, 'superstructure') });
    for (const p of subFc) setRow(p.year, { sub_f: off(p.yhat, 'substructure') });
    for (const p of (simMetrics?.deck?.simulated || [])) setRow(p.year, { deck_s: off(p.yhat, 'deck') });
    for (const p of (simMetrics?.superstructure?.simulated || [])) setRow(p.year, { super_s: off(p.yhat, 'superstructure') });
    for (const p of (simMetrics?.substructure?.simulated || [])) setRow(p.year, { sub_s: off(p.yhat, 'substructure') });

    // Seed the forecast & simulation lines at the last observed year so they
    // connect smoothly to the observed history.
    if (observed.length && Number.isFinite(forecastStart)) {
      const last = observed[observed.length - 1];
      const boundary = rows.get(last.year);
      if (boundary) {
        if (deckFc.length && last.deck != null) {
          const v = off(last.deck, 'deck');
          boundary.deck_f = v;
          if (v != null) boundary.deck_band = [v, v];  // connect the band to the last observed point
        }
        if (superFc.length && last.superstructure != null) boundary.super_f = off(last.superstructure, 'superstructure');
        if (subFc.length && last.substructure != null) boundary.sub_f = off(last.substructure, 'substructure');
        if (simMetrics?.deck?.simulated?.length && last.deck != null) boundary.deck_s = off(last.deck, 'deck');
        if (simMetrics?.superstructure?.simulated?.length && last.superstructure != null) boundary.super_s = off(last.superstructure, 'superstructure');
        if (simMetrics?.substructure?.simulated?.length && last.substructure != null) boundary.sub_s = off(last.substructure, 'substructure');
      }
    }
    return Array.from(rows.values()).sort((a, b) => a.year - b.year);
  }, [observed, deckFc, superFc, subFc, simMetrics, forecastStart]);

  const hasForecast = deckFc.length + superFc.length + subFc.length > 0;

  // The same maintenance/posting event repeats in every yearly snapshot, so
  // collapse to unique events for a clean timeline.
  const dedupedEvents = useMemo(() => {
    const seen = new Set<string>();
    const out: HistoryEvent[] = [];
    for (const ev of (data?.history_events || [])) {
      const key = `${ev.year}|${ev.event_type}|${(ev.label || '').trim().toLowerCase()}`;
      if (!seen.has(key)) { seen.add(key); out.push(ev); }
    }
    return out.sort((a, b) => a.year - b.year);
  }, [data]);

  return (
    <Modal open onClose={onClose} inline={embedded} size="lg" title="Bridge History & Forecast" icon={<History className="w-5 h-5" />}>
      <p className="text-sm text-slate-500 dark:text-slate-400 -mt-1 mb-5">
        {bridgeName || 'Bridge'} · {bridgeNumber}{data?.year_built ? ` · Built ${data.year_built}` : ''}
      </p>

      {loading && (
        <div className="text-center py-12">
          <Loader2 className="w-9 h-9 text-brand-500 mx-auto animate-spin" />
          <p className="text-slate-500 dark:text-slate-400 mt-3">Loading history…</p>
        </div>
      )}
      {error && <p className="text-red-500 text-center py-8">{error}</p>}

      {data && !loading && (
        <div className="space-y-6">
          {/* Current ratings */}
          <div className="flex gap-3 flex-wrap">
            {[
              { label: 'Deck', val: data.deck_condition_rating },
              { label: 'Superstructure', val: data.superstructure_condition_rating },
              { label: 'Substructure', val: data.substructure_condition_rating },
            ].map(({ label, val }) => (
              <div key={label} className="flex-1 min-w-[180px] rounded-xl px-4 py-3 bg-slate-50 dark:bg-slate-800/60" style={{ borderLeft: `4px solid ${ratingColor(val)}` }}>
                <div className="text-[11px] uppercase tracking-wider text-slate-500 dark:text-slate-400 mb-1">{label}</div>
                <div className="font-bold text-base" style={{ color: ratingColor(val) }}>{ratingLabel(val)}</div>
              </div>
            ))}
          </div>

          {/* Forecast control row */}
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <h3 className="font-semibold text-slate-700 dark:text-slate-200">Condition Trend &amp; {horizonYears}-yr Forecast</h3>
            <div className="flex items-center gap-2">
              {training && (
                <span className="text-xs text-slate-500 dark:text-slate-400 flex items-center gap-1">
                  <Loader2 className="w-3.5 h-3.5 animate-spin" /> Training…
                </span>
              )}
              <Button size="sm" variant="secondary" icon={<Sparkles className="w-3.5 h-3.5" />} onClick={() => generateForecast(horizonYears)} disabled={training}>
                {training ? 'Training…' : 'Retrain'}
              </Button>
            </div>
          </div>

          {/* Forecast horizon slider */}
          <div className="flex items-center gap-3 flex-wrap text-sm">
            <span className="font-medium text-slate-600 dark:text-slate-300 whitespace-nowrap">
              Forecast horizon: <span className="font-bold">{horizonYears}</span> year{horizonYears === 1 ? '' : 's'}
            </span>
            <input
              type="range"
              min={1}
              max={50}
              step={1}
              value={horizonYears}
              onChange={(e) => setHorizonYears(Number(e.target.value))}
              onPointerUp={commitHorizon}
              onKeyUp={commitHorizon}
              disabled={training}
              className="flex-1 min-w-[160px] accent-brand-500"
            />
          </div>

          {/* Maintenance simulation control */}
          {hasForecast && forecastYears.length > 0 && (
            <div className="flex items-center gap-3 flex-wrap text-sm bg-slate-50 dark:bg-slate-800/60 rounded-xl px-4 py-3">
              <span className="flex items-center gap-1.5 font-medium text-slate-600 dark:text-slate-300">
                <Wrench className="w-4 h-4" /> Simulate maintenance in
              </span>
              <select
                value={simYear}
                onChange={(e) => {
                  const v = e.target.value ? Number(e.target.value) : '';
                  setSimYear(v);
                  if (v) runSimulation(v, horizonYears); else setSimForecast(null);
                }}
                className="rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-900 px-3 py-1.5 text-slate-800 dark:text-slate-100"
              >
                <option value="">— no simulation —</option>
                {forecastYears.map((y) => <option key={y} value={y}>{y}</option>)}
              </select>
              {simLoading && <Loader2 className="w-4 h-4 animate-spin text-brand-500" />}
            </div>
          )}

          {/* Chart */}
          {series.length > 1 ? (
            <div>
            <ResponsiveContainer width="100%" height={320}>
              <ComposedChart data={series}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.25)" />
                <XAxis dataKey="year" tick={{ fontSize: 12, fill: '#94a3b8' }} />
                <YAxis domain={[0, 9]} ticks={RATING_TICKS} interval={0} tick={{ fontSize: 12, fill: '#94a3b8' }} allowDecimals={false} />
                <Tooltip contentStyle={{ borderRadius: 12, border: '1px solid rgba(148,163,184,0.3)' }} formatter={tipFormatter} />
                {/* Shaded deck forecast confidence band (drawn behind the lines). */}
                <Area type="monotone" dataKey="deck_band" name="Deck confidence range" stroke="#22c55e" strokeOpacity={0.35} strokeWidth={1} fill="#22c55e" fillOpacity={0.18} connectNulls legendType="none" isAnimationActive={false} />
                {Number.isFinite(forecastStart) && (
                  <ReferenceLine x={forecastStart} stroke="#6366f1" strokeDasharray="4 4" label={{ value: 'forecast', position: 'top', fontSize: 10, fill: '#6366f1' }} />
                )}
                {simYear !== '' && (
                  <ReferenceLine x={simYear} stroke="#0ea5e9" strokeDasharray="2 2" label={{ value: 'work', position: 'top', fontSize: 10, fill: '#0ea5e9' }} />
                )}
                {dedupedEvents.slice(0, 12).map((ev, i) => (
                  <ReferenceLine key={i} x={ev.year} stroke={eventStyle(ev.event_type).color} strokeOpacity={0.5} strokeDasharray="4 3" ifOverflow="extendDomain" />
                ))}
                {/* Observed (solid) */}
                <Line type="monotone" dataKey="deck" stroke="#22c55e" strokeWidth={2.5} strokeOpacity={0.9} name="Deck" dot={{ r: 3 }} connectNulls />
                <Line type="monotone" dataKey="superstructure" stroke="#eab308" strokeWidth={2.5} strokeOpacity={0.9} name="Superstructure" dot={{ r: 4 }} connectNulls />
                <Line type="monotone" dataKey="substructure" stroke="#ef4444" strokeWidth={2.5} strokeOpacity={0.9} name="Substructure" dot={{ r: 3 }} connectNulls />
                {/* Baseline forecast (dashed) */}
                <Line type="monotone" dataKey="deck_f" stroke="#22c55e" strokeWidth={2} strokeDasharray="6 4" name="Deck (forecast)" dot={false} connectNulls />
                <Line type="monotone" dataKey="super_f" stroke="#eab308" strokeWidth={2} strokeDasharray="6 4" name="Super (forecast)" dot={false} connectNulls />
                <Line type="monotone" dataKey="sub_f" stroke="#ef4444" strokeWidth={2} strokeDasharray="6 4" name="Sub (forecast)" dot={false} connectNulls />
                {/* Deck confidence edges (solid, clearer than before) */}
                <Line type="monotone" dataKey="deck_lo" stroke="#16a34a" strokeOpacity={0.6} strokeWidth={1.25} name="Deck lower" dot={false} connectNulls legendType="none" />
                <Line type="monotone" dataKey="deck_hi" stroke="#16a34a" strokeOpacity={0.6} strokeWidth={1.25} name="Deck upper" dot={false} connectNulls legendType="none" />
                {/* Maintenance simulation (dotted) */}
                <Line type="monotone" dataKey="deck_s" stroke="#16a34a" strokeWidth={2} strokeDasharray="1 3" name="Deck (with work)" dot={{ r: 2 }} connectNulls />
                <Line type="monotone" dataKey="super_s" stroke="#ca8a04" strokeWidth={2} strokeDasharray="1 3" name="Super (with work)" dot={{ r: 2 }} connectNulls />
                <Line type="monotone" dataKey="sub_s" stroke="#dc2626" strokeWidth={2} strokeDasharray="1 3" name="Sub (with work)" dot={{ r: 2 }} connectNulls />
              </ComposedChart>
            </ResponsiveContainer>

            {/* Custom legend: colour = which member, line style = which scenario */}
            <div className="mt-3 flex flex-wrap items-center gap-x-6 gap-y-2 rounded-xl bg-slate-50 dark:bg-slate-800/60 px-4 py-2.5 text-xs">
              <div className="flex items-center gap-3">
                <span className="font-semibold text-slate-500 dark:text-slate-400">Member</span>
                {LEGEND_COMPONENTS.map((c) => (
                  <span key={c.label} className="flex items-center gap-1.5">
                    <span className="inline-block w-3 h-3 rounded-full" style={{ background: c.color }} />
                    <span className="text-slate-600 dark:text-slate-300">{c.label}</span>
                  </span>
                ))}
              </div>
              <div className="flex items-center gap-3">
                <span className="font-semibold text-slate-500 dark:text-slate-400">Line</span>
                <span className="flex items-center gap-1.5"><LineSample /><span className="text-slate-600 dark:text-slate-300">Observed</span></span>
                {hasForecast && (
                  <span className="flex items-center gap-1.5"><LineSample dash="6 4" /><span className="text-slate-600 dark:text-slate-300">Forecast</span></span>
                )}
                {simYear !== '' && simForecast && (
                  <span className="flex items-center gap-1.5"><LineSample dash="1 3" /><span className="text-slate-600 dark:text-slate-300">With maintenance</span></span>
                )}
              </div>
            </div>
            </div>
          ) : (
            <div className="text-center py-6 px-4 rounded-xl bg-slate-50 dark:bg-slate-800/60 text-slate-500 dark:text-slate-400 text-sm">
              Not enough data points to show a trend. Load multiple report years to build a forecast.
            </div>
          )}

          {/* Maintenance & event timeline (deduplicated) — horizontal timeline */}
          {dedupedEvents.length > 0 && (
            <div>
              <h4 className="font-semibold text-sm text-slate-600 dark:text-slate-300 mb-3">Maintenance &amp; Event Timeline</h4>
              <div className="overflow-x-auto ui-scroll pb-2">
                <div className="flex min-w-min">
                  {dedupedEvents.map((ev, i) => {
                    const { color, label: typeLabel } = eventStyle(ev.event_type);
                    return (
                      <div key={i} className="flex w-32 shrink-0 flex-col items-center px-1">
                        {/* Above the line: year + type badge */}
                        <div className="flex h-12 flex-col items-center justify-end gap-1">
                          <span className="text-sm font-bold text-slate-800 dark:text-slate-100">{ev.year}</span>
                          <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded" style={{ background: `${color}22`, color }}>{typeLabel}</span>
                        </div>
                        {/* The line + the event dot sitting on it (connects across items) */}
                        <div className="relative flex h-4 w-full items-center justify-center">
                          <div className="absolute inset-x-0 top-1/2 h-0.5 -translate-y-1/2 bg-slate-200 dark:bg-slate-700" />
                          <span className="relative z-10 h-3.5 w-3.5 rounded-full border-2 border-white dark:border-slate-900 shadow-sm" style={{ background: color }} />
                        </div>
                        {/* Below the line: event label */}
                        <p className="mt-1.5 text-center text-xs leading-snug text-slate-600 dark:text-slate-300">{ev.label}</p>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </Modal>
  );
};

export default BridgeHistoryModal;
