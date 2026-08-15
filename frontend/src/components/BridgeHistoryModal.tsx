import React, { useCallback, useEffect, useMemo, useState } from 'react';
import axios from 'axios';
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { History, Loader2, RefreshCw, ShieldCheck } from 'lucide-react';
import { Button, Modal } from './ui';
import { useDataGenerationEpoch } from '../lib/dataGeneration';

const RATING_TICKS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9];
const DEFAULT_HORIZON = 10;

interface ForecastPoint {
  year: number;
  yhat: number;
  p10: number;
  p50: number;
  p90: number;
  standard_deviation: number;
  fallback_level: string;
  action_id?: string | null;
}

interface ObservedPoint {
  year: number;
  value: number;
  snapshot_revision_id: string;
}

interface MetricSeries {
  observed: ObservedPoint[];
  forecast: ForecastPoint[];
}

interface BridgeForecast {
  run_id: string;
  state_code: number;
  bridge_number: string;
  snapshot_revision_id: string;
  as_of_year: number;
  horizon_years: number;
  model_version_id: string;
  model_id: string;
  evaluation_run_id: string;
  evaluation_receipt_id: string;
  qualification: string;
  source_data_generation: number;
  metrics: Record<string, MetricSeries>;
}

interface Props {
  stateCode: number | string;
  bridgeNumber: string;
  bridgeName?: string;
  dataYear?: number | string | null;
  onClose: () => void;
  embedded?: boolean;
}

interface ChartRow {
  year: number;
  deck?: number;
  superstructure?: number;
  substructure?: number;
  deckForecast?: number;
  superstructureForecast?: number;
  substructureForecast?: number;
  deckPredictionRange?: [number, number];
}

const ratingColor = (value: number | null | undefined) => {
  if (value == null || !Number.isFinite(value)) return '#94a3b8';
  if (value >= 7) return '#16a34a';
  if (value >= 5) return '#ca8a04';
  return '#dc2626';
};

const ratingLabel = (value: number | null | undefined) => {
  if (value == null || !Number.isFinite(value)) return 'Unknown';
  const className = value >= 7 ? 'Good' : value >= 5 ? 'Fair' : 'Poor';
  return `${className} (${value.toFixed(1)})`;
};

const latestObserved = (series: MetricSeries | undefined): number | null => {
  const rows = [...(series?.observed || [])].sort((left, right) => left.year - right.year);
  return rows.length ? Number(rows[rows.length - 1].value) : null;
};

const BridgeHistoryModal: React.FC<Props> = ({
  stateCode,
  bridgeNumber,
  bridgeName,
  dataYear,
  onClose,
  embedded,
}) => {
  const [forecast, setForecast] = useState<BridgeForecast | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [horizonYears, setHorizonYears] = useState(DEFAULT_HORIZON);
  const dataGenerationEpoch = useDataGenerationEpoch();

  const loadForecast = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const stateResponse = await axios.get<{ data_generation: number }>('/api/revisions/state');
      const generation = Number(stateResponse.data.data_generation);
      if (!Number.isSafeInteger(generation) || generation < 0) {
        throw new Error('The published data generation is invalid.');
      }
      const requestedYear = dataYear == null || dataYear === '' ? null : Number(dataYear);
      if (requestedYear != null && (!Number.isSafeInteger(requestedYear) || requestedYear < 1900)) {
        throw new Error('The selected inventory year is invalid.');
      }
      const key = [
        'history-v2',
        String(stateCode),
        bridgeNumber,
        requestedYear ?? 'latest',
        horizonYears,
        generation,
      ].join(':');
      const response = await axios.post<BridgeForecast>(
        '/api/forecasts/v2/bridge',
        {
          state_code: Number(stateCode),
          bridge_number: bridgeNumber,
          as_of_year: requestedYear,
          horizon_years: horizonYears,
          expected_data_generation: generation,
          mode: 'no_action',
        },
        { headers: { 'Idempotency-Key': key } },
      );
      setForecast(response.data);
    } catch (reason: any) {
      setForecast(null);
      setError(
        reason?.response?.data?.detail?.message ||
        reason?.response?.data?.detail ||
        reason?.message ||
        'The exact-revision forecast could not be loaded.',
      );
    } finally {
      setLoading(false);
    }
  }, [stateCode, bridgeNumber, dataYear, horizonYears]);

  useEffect(() => {
    void loadForecast();
  }, [loadForecast, dataGenerationEpoch]);

  const chartRows = useMemo(() => {
    const rows = new Map<number, ChartRow>();
    const update = (year: number, patch: Partial<ChartRow>) => {
      rows.set(year, { ...(rows.get(year) || { year }), year, ...patch });
    };
    const metrics = forecast?.metrics || {};
    for (const point of metrics.deck?.observed || []) update(point.year, { deck: point.value });
    for (const point of metrics.superstructure?.observed || []) update(point.year, { superstructure: point.value });
    for (const point of metrics.substructure?.observed || []) update(point.year, { substructure: point.value });
    for (const point of metrics.deck?.forecast || []) {
      update(point.year, {
        deckForecast: point.yhat,
        deckPredictionRange: [point.p10, point.p90],
      });
    }
    for (const point of metrics.superstructure?.forecast || []) update(point.year, { superstructureForecast: point.yhat });
    for (const point of metrics.substructure?.forecast || []) update(point.year, { substructureForecast: point.yhat });

    // Connect each forecast to the final observed point without changing the
    // stored values or presenting the expected path as an observation.
    for (const [component, target] of [
      ['deck', 'deckForecast'],
      ['superstructure', 'superstructureForecast'],
      ['substructure', 'substructureForecast'],
    ] as const) {
      const observed = [...(metrics[component]?.observed || [])].sort((a, b) => a.year - b.year);
      if (!observed.length || !(metrics[component]?.forecast || []).length) continue;
      const last = observed[observed.length - 1];
      update(last.year, { [target]: last.value });
      if (component === 'deck') update(last.year, { deckPredictionRange: [last.value, last.value] });
    }
    return [...rows.values()].sort((left, right) => left.year - right.year);
  }, [forecast]);

  const metrics = forecast?.metrics || {};
  const currentRatings = [
    { label: 'Deck', value: latestObserved(metrics.deck) },
    { label: 'Superstructure', value: latestObserved(metrics.superstructure) },
    { label: 'Substructure', value: latestObserved(metrics.substructure) },
  ];
  const firstForecastYear = Math.min(
    ...Object.values(metrics).flatMap((series) => series.forecast.map((point) => point.year)),
  );

  return (
    <Modal open onClose={onClose} inline={embedded} size="lg" title="Bridge History & Forecast" icon={<History className="h-5 w-5" />}>
      <div className="space-y-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="font-semibold text-slate-900 dark:text-white">{bridgeName || `Bridge ${bridgeNumber}`}</p>
            <p className="text-sm text-slate-500">Bridge {bridgeNumber}{dataYear ? ` · selected inventory year ${dataYear}` : ' · latest published year'}</p>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs font-semibold text-slate-500" htmlFor="history-horizon">Horizon</label>
            <select
              id="history-horizon"
              value={horizonYears}
              onChange={(event) => setHorizonYears(Number(event.target.value))}
              className="rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-900"
            >
              {[1, 3, 5, 10, 15, 20, 30, 50].map((years) => <option key={years} value={years}>{years} years</option>)}
            </select>
            <Button size="sm" variant="secondary" icon={<RefreshCw className="h-4 w-4" />} onClick={() => void loadForecast()} disabled={loading}>
              Refresh
            </Button>
          </div>
        </div>

        {loading && (
          <div className="py-12 text-center text-slate-500">
            <Loader2 className="mx-auto h-9 w-9 animate-spin text-brand-500" />
            <p className="mt-3">Loading the exact published history and annual forecast…</p>
          </div>
        )}
        {error && !loading && (
          <div className="rounded-2xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
            <p className="font-semibold">Forecast unavailable</p>
            <p className="mt-1">{error}</p>
            <p className="mt-2 text-xs">Fit and historically validate a v2 model from the Forecasts page, then retry.</p>
          </div>
        )}

        {forecast && !loading && (
          <>
            <div className="grid gap-3 sm:grid-cols-3">
              {currentRatings.map(({ label, value }) => (
                <div key={label} className="rounded-2xl border border-slate-200 bg-slate-50 p-4 dark:border-slate-700 dark:bg-slate-900/60" style={{ borderLeftColor: ratingColor(value), borderLeftWidth: 4 }}>
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">{label}</p>
                  <p className="mt-1 font-bold" style={{ color: ratingColor(value) }}>{ratingLabel(value)}</p>
                </div>
              ))}
            </div>

            <div className="grid gap-3 rounded-2xl border border-indigo-100 bg-indigo-50/60 p-4 text-xs text-slate-700 dark:border-indigo-900 dark:bg-indigo-950/20 dark:text-slate-300 md:grid-cols-2">
              <p><span className="font-semibold">Exact source:</span> {forecast.as_of_year} · revision {forecast.snapshot_revision_id}</p>
              <p><span className="font-semibold">Forecast package:</span> {forecast.model_version_id}</p>
              <p><span className="font-semibold">Historical evaluation:</span> {forecast.evaluation_receipt_id}</p>
              <p className="flex items-center gap-1.5"><ShieldCheck className="h-4 w-4 text-indigo-600" /><span className="font-semibold">Status:</span> {forecast.qualification.split('_').join(' ')}</p>
            </div>

            {chartRows.length > 1 ? (
              <div className="rounded-2xl border border-slate-200 p-3 dark:border-slate-700">
                <ResponsiveContainer width="100%" height={340}>
                  <ComposedChart data={chartRows}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.25)" />
                    <XAxis dataKey="year" tick={{ fontSize: 12, fill: '#64748b' }} />
                    <YAxis domain={[0, 9]} ticks={RATING_TICKS} interval={0} allowDecimals={false} tick={{ fontSize: 12, fill: '#64748b' }} />
                    <Tooltip contentStyle={{ borderRadius: 12, border: '1px solid #cbd5e1' }} />
                    <Area dataKey="deckPredictionRange" name="Deck p10–p90 prediction range" stroke="#16a34a" fill="#22c55e" fillOpacity={0.14} connectNulls />
                    {Number.isFinite(firstForecastYear) && <ReferenceLine x={firstForecastYear} stroke="#4f46e5" strokeDasharray="4 4" label={{ value: 'forecast', position: 'top', fill: '#4f46e5', fontSize: 10 }} />}
                    <Line dataKey="deck" name="Deck observed" stroke="#16a34a" strokeWidth={2.5} dot={{ r: 3 }} connectNulls />
                    <Line dataKey="superstructure" name="Superstructure observed" stroke="#ca8a04" strokeWidth={2.5} dot={{ r: 3 }} connectNulls />
                    <Line dataKey="substructure" name="Substructure observed" stroke="#dc2626" strokeWidth={2.5} dot={{ r: 3 }} connectNulls />
                    <Line dataKey="deckForecast" name="Deck expected" stroke="#16a34a" strokeWidth={2} strokeDasharray="6 4" dot={false} connectNulls />
                    <Line dataKey="superstructureForecast" name="Superstructure expected" stroke="#ca8a04" strokeWidth={2} strokeDasharray="6 4" dot={false} connectNulls />
                    <Line dataKey="substructureForecast" name="Substructure expected" stroke="#dc2626" strokeWidth={2} strokeDasharray="6 4" dot={false} connectNulls />
                  </ComposedChart>
                </ResponsiveContainer>
                <p className="mt-2 text-xs text-slate-500">Solid lines are published observations. Dashed lines are the annual no-action expected path; the shaded band is a model prediction range, not a confidence guarantee.</p>
              </div>
            ) : (
              <div className="rounded-2xl bg-slate-50 p-6 text-center text-sm text-slate-500 dark:bg-slate-900/60">Not enough published observations are available to plot this bridge.</div>
            )}

            <div className="rounded-2xl border border-sky-200 bg-sky-50 p-4 text-sm text-sky-900 dark:border-sky-900 dark:bg-sky-950/20 dark:text-sky-200">
              Rehabilitation and maintenance simulations are in the LCCA tab, where each yearly action is checked against bridge materials, a named treatment library, cost evidence, and this same forecast package.
            </div>
          </>
        )}
      </div>
    </Modal>
  );
};

export default BridgeHistoryModal;
