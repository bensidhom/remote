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
import { History, Loader2, RefreshCw } from 'lucide-react';
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
  source_status?: 'loaded_inventory';
  source_database?: string;
  warnings?: string[];
  metrics: Record<string, MetricSeries>;
}

interface ComparisonSummary {
  compared_years: number[];
  compared_points: number;
  mean_absolute_error: number | null;
  condition_class_matches: number;
  condition_class_accuracy: number | null;
}

interface BridgeBacktest extends BridgeForecast {
  test_start_year: number;
  latest_actual_year: number;
  available_start_years: number[];
  comparison: Record<string, ComparisonSummary>;
}

type HistoryView = 'future' | 'historical-test';

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
  const [historyView, setHistoryView] = useState<HistoryView>('future');
  const [backtestStartYear, setBacktestStartYear] = useState<number | null>(null);
  const [backtest, setBacktest] = useState<BridgeBacktest | null>(null);
  const [backtestLoading, setBacktestLoading] = useState(false);
  const [backtestError, setBacktestError] = useState<string | null>(null);
  const [dataGeneration, setDataGeneration] = useState<number | null>(null);
  const dataGenerationEpoch = useDataGenerationEpoch();

  const loadForecast = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const stateResponse = await axios.get<{ data_generation: number }>('/api/revisions/state');
      const generation = Number(stateResponse.data.data_generation);
      if (!Number.isSafeInteger(generation) || generation < 0) {
        throw new Error('The inventory data generation is invalid.');
      }
      setDataGeneration(generation);
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

  const availableStartYears = useMemo(() => {
    const years = new Set<number>();
    for (const series of Object.values(forecast?.metrics || {})) {
      for (const point of series.observed) years.add(Number(point.year));
    }
    const ordered = [...years].filter(Number.isFinite).sort((left, right) => left - right);
    return ordered.slice(0, -1);
  }, [forecast]);
  const latestObservedYear = useMemo(() => {
    const years = Object.values(forecast?.metrics || {}).flatMap((series) => series.observed.map((point) => Number(point.year)));
    return years.length ? Math.max(...years) : null;
  }, [forecast]);

  useEffect(() => {
    if (!availableStartYears.length) {
      setBacktestStartYear(null);
      return;
    }
    if (backtestStartYear != null && availableStartYears.includes(backtestStartYear)) return;
    const preferredYears = availableStartYears.filter((year) => latestObservedYear != null && year <= latestObservedYear - 5);
    setBacktestStartYear(preferredYears[preferredYears.length - 1] ?? availableStartYears[0]);
  }, [availableStartYears, backtestStartYear, latestObservedYear]);

  const loadBacktest = useCallback(async () => {
    if (backtestStartYear == null || dataGeneration == null) return;
    setBacktestLoading(true);
    setBacktestError(null);
    try {
      const response = await axios.post<BridgeBacktest>(
        '/api/forecasts/v2/bridge/backtest',
        {
          state_code: Number(stateCode),
          bridge_number: bridgeNumber,
          start_year: backtestStartYear,
          expected_data_generation: dataGeneration,
        },
      );
      setBacktest(response.data);
    } catch (reason: any) {
      setBacktest(null);
      setBacktestError(
        reason?.response?.data?.detail?.message ||
        reason?.response?.data?.detail ||
        reason?.message ||
        'The historical forecast test could not be loaded.',
      );
    } finally {
      setBacktestLoading(false);
    }
  }, [backtestStartYear, bridgeNumber, dataGeneration, stateCode]);

  useEffect(() => {
    if (historyView === 'historical-test') void loadBacktest();
  }, [historyView, loadBacktest, dataGenerationEpoch]);

  const displayedForecast = historyView === 'historical-test' ? backtest : forecast;

  const chartRows = useMemo(() => {
    const rows = new Map<number, ChartRow>();
    const update = (year: number, patch: Partial<ChartRow>) => {
      rows.set(year, { ...(rows.get(year) || { year }), year, ...patch });
    };
    const metrics = displayedForecast?.metrics || {};
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

    // Connect each forecast to its as-of observation. During a historical test,
    // later actual values remain untouched so they can be compared honestly.
    for (const [component, target] of [
      ['deck', 'deckForecast'],
      ['superstructure', 'superstructureForecast'],
      ['substructure', 'substructureForecast'],
    ] as const) {
      const observed = [...(metrics[component]?.observed || [])].sort((a, b) => a.year - b.year);
      if (!observed.length || !(metrics[component]?.forecast || []).length) continue;
      const anchor = [...observed].reverse().find((point) => point.year <= Number(displayedForecast?.as_of_year));
      if (!anchor) continue;
      update(anchor.year, { [target]: anchor.value });
      if (component === 'deck') update(anchor.year, { deckPredictionRange: [anchor.value, anchor.value] });
    }
    return [...rows.values()].sort((left, right) => left.year - right.year);
  }, [displayedForecast]);

  const metrics = displayedForecast?.metrics || {};
  const currentRatings = [
    { label: 'Deck', value: latestObserved(metrics.deck) },
    { label: 'Superstructure', value: latestObserved(metrics.superstructure) },
    { label: 'Substructure', value: latestObserved(metrics.substructure) },
  ];
  const firstForecastYear = Math.min(
    ...Object.values(metrics).flatMap((series) => series.forecast.map((point) => point.year)),
  );
  const comparisonRows = Object.entries(backtest?.comparison || {}).filter(([, summary]) => summary.compared_points > 0);

  return (
    <Modal open onClose={onClose} inline={embedded} size="lg" title="Bridge History & Forecast" icon={<History className="h-5 w-5" />}>
      <div className="space-y-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="font-semibold text-slate-900 dark:text-white">{bridgeName || `Bridge ${bridgeNumber}`}</p>
            <p className="text-sm text-slate-500">Bridge {bridgeNumber}{dataYear ? ` · selected inventory year ${dataYear}` : ' · latest available year'}</p>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            <div className="flex rounded-xl bg-slate-100 p-1 dark:bg-slate-800" aria-label="History forecast view">
              <button
                type="button"
                onClick={() => setHistoryView('future')}
                className={`rounded-lg px-3 py-1.5 text-sm font-semibold transition ${historyView === 'future' ? 'bg-white text-indigo-700 shadow-sm dark:bg-slate-700 dark:text-indigo-300' : 'text-slate-600 dark:text-slate-300'}`}
              >
                Future forecast
              </button>
              <button
                type="button"
                onClick={() => setHistoryView('historical-test')}
                disabled={!availableStartYears.length}
                className={`rounded-lg px-3 py-1.5 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-40 ${historyView === 'historical-test' ? 'bg-white text-indigo-700 shadow-sm dark:bg-slate-700 dark:text-indigo-300' : 'text-slate-600 dark:text-slate-300'}`}
              >
                Test a past year
              </button>
            </div>
            {historyView === 'future' ? (
              <>
                <label className="text-xs font-semibold text-slate-500" htmlFor="history-horizon">Horizon</label>
                <select
                  id="history-horizon"
                  value={horizonYears}
                  onChange={(event) => setHorizonYears(Number(event.target.value))}
                  className="rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-900"
                >
                  {[1, 3, 5, 10, 15, 20, 30, 50].map((years) => <option key={years} value={years}>{years} years</option>)}
                </select>
              </>
            ) : (
              <>
                <label className="text-xs font-semibold text-slate-500" htmlFor="history-test-year">Start from</label>
                <select
                  id="history-test-year"
                  value={backtestStartYear ?? ''}
                  onChange={(event) => setBacktestStartYear(Number(event.target.value))}
                  className="rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-900"
                >
                  {availableStartYears.map((year) => <option key={year} value={year}>{year}</option>)}
                </select>
              </>
            )}
            <Button
              size="sm"
              variant="secondary"
              icon={<RefreshCw className="h-4 w-4" />}
              onClick={() => void (historyView === 'historical-test' ? loadBacktest() : loadForecast())}
              disabled={loading || backtestLoading}
            >
              Refresh
            </Button>
          </div>
        </div>

        {(loading || (historyView === 'historical-test' && backtestLoading)) && (
          <div className="py-12 text-center text-slate-500">
            <Loader2 className="mx-auto h-9 w-9 animate-spin text-brand-500" />
            <p className="mt-3">{historyView === 'historical-test' ? 'Running the past-year forecast test…' : 'Loading available bridge history and annual forecast…'}</p>
          </div>
        )}
        {error && !loading && (
          <div className="rounded-2xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
            <p className="font-semibold">Forecast unavailable</p>
            <p className="mt-1">{error}</p>
            <p className="mt-2 text-xs">BridgeGuard could not find yearly inventory history for this bridge.</p>
          </div>
        )}
        {backtestError && historyView === 'historical-test' && !backtestLoading && (
          <div className="rounded-2xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
            <p className="font-semibold">Historical test unavailable</p>
            <p className="mt-1">{backtestError}</p>
          </div>
        )}

        {displayedForecast && !loading && !backtestLoading && (
          <>
            <div className="grid gap-3 sm:grid-cols-3">
              {currentRatings.map(({ label, value }) => (
                <div key={label} className="rounded-2xl border border-slate-200 bg-slate-50 p-4 dark:border-slate-700 dark:bg-slate-900/60" style={{ borderLeftColor: ratingColor(value), borderLeftWidth: 4 }}>
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">{label}</p>
                  <p className="mt-1 font-bold" style={{ color: ratingColor(value) }}>{ratingLabel(value)}</p>
                </div>
              ))}
            </div>

            {historyView === 'historical-test' && backtest && (
              <div className="rounded-2xl border border-indigo-100 bg-indigo-50/60 p-4 text-sm text-slate-700 dark:border-indigo-900 dark:bg-indigo-950/20 dark:text-slate-200">
                <p className="font-semibold text-slate-900 dark:text-white">Historical forecast test: {backtest.test_start_year}–{backtest.latest_actual_year}</p>
                <p className="mt-1">
                  The Markov model uses only inventory information available through {backtest.test_start_year}. It predicts the following years, and the real observations through {backtest.latest_actual_year} are added afterward only for comparison.
                </p>
                {comparisonRows.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-2">
                    {comparisonRows.map(([component, summary]) => (
                      <span key={component} className="rounded-full bg-white px-3 py-1 text-xs shadow-sm dark:bg-slate-800">
                        <span className="font-semibold capitalize">{component}</span>: average error {summary.mean_absolute_error?.toFixed(2) ?? '—'} rating points
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )}

            {chartRows.length > 1 ? (
              <div className="rounded-2xl border border-slate-200 p-3 dark:border-slate-700">
                <ResponsiveContainer width="100%" height={340}>
                  <ComposedChart data={chartRows}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.25)" />
                    <XAxis dataKey="year" tick={{ fontSize: 12, fill: '#64748b' }} />
                    <YAxis domain={[0, 9]} ticks={RATING_TICKS} interval={0} allowDecimals={false} tick={{ fontSize: 12, fill: '#64748b' }} />
                    <Tooltip contentStyle={{ borderRadius: 12, border: '1px solid #cbd5e1' }} />
                    <Area dataKey="deckPredictionRange" name="Deck p10–p90 prediction range" stroke="#16a34a" fill="#22c55e" fillOpacity={0.14} connectNulls />
                    {Number.isFinite(firstForecastYear) && <ReferenceLine x={firstForecastYear} stroke="#4f46e5" strokeDasharray="4 4" label={{ value: historyView === 'historical-test' ? 'test forecast' : 'forecast', position: 'top', fill: '#4f46e5', fontSize: 10 }} />}
                    <Line dataKey="deck" name="Deck observed" stroke="#16a34a" strokeWidth={2.5} dot={{ r: 3 }} connectNulls />
                    <Line dataKey="superstructure" name="Superstructure observed" stroke="#ca8a04" strokeWidth={2.5} dot={{ r: 3 }} connectNulls />
                    <Line dataKey="substructure" name="Substructure observed" stroke="#dc2626" strokeWidth={2.5} dot={{ r: 3 }} connectNulls />
                    <Line dataKey="deckForecast" name="Deck expected" stroke="#16a34a" strokeWidth={2} strokeDasharray="6 4" dot={false} connectNulls />
                    <Line dataKey="superstructureForecast" name="Superstructure expected" stroke="#ca8a04" strokeWidth={2} strokeDasharray="6 4" dot={false} connectNulls />
                    <Line dataKey="substructureForecast" name="Substructure expected" stroke="#dc2626" strokeWidth={2} strokeDasharray="6 4" dot={false} connectNulls />
                  </ComposedChart>
                </ResponsiveContainer>
                <p className="mt-2 text-xs text-slate-500">
                  {historyView === 'historical-test'
                    ? `Solid lines are the real yearly inventory values. Dashed lines are predictions made from the ${backtest?.test_start_year} cutoff; later real values were not used to produce them.`
                    : 'Solid lines are exact yearly inventory observations. Dashed lines are the annual no-action expected path; the shaded band is a model prediction range, not a confidence guarantee.'}
                </p>
              </div>
            ) : (
              <div className="rounded-2xl bg-slate-50 p-6 text-center text-sm text-slate-500 dark:bg-slate-900/60">Not enough available observations are present to plot this bridge.</div>
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
