import React, { useEffect, useMemo, useRef, useState } from 'react';
import axios from 'axios';
import createPlotlyComponent from 'react-plotly.js/factory';
import Plotly from 'plotly.js-basic-dist-min';
import type { Data, Layout } from 'plotly.js';
import { BrainCircuit, Loader2, RefreshCw, Search, ShieldCheck, Square } from 'lucide-react';
import { CountUp, SectionHeader } from '../components/ui';

const Plot = createPlotlyComponent(Plotly);

type Message = { type: 'success' | 'error' | 'info'; text: string };

interface EvaluationSummary {
  origins: number[];
  target_rows: number;
  bridge_count: number;
  candidate_mae: number;
  persistence_mae: number;
  mae_skill: number;
  mean_bias: number;
}

interface ForecastSummary {
  data_generation: number;
  schema_generation: number;
  published_observations: number;
  published_bridges: number;
  available_years: number[];
  latest_package: null | {
    model_version_id: string;
    model_id: string;
    engine_version: string;
    qualification: string;
    created_at: string;
    evaluation_run_id: string;
    evaluation_receipt_id: string;
    release_qualified: boolean;
    evaluation: EvaluationSummary;
  };
}

interface ForecastBridge {
  state_code: number;
  bridge_number: string;
  bridge_name?: string | null;
  observation_years: number;
  latest_year: number;
}

interface ForecastPoint {
  year: number;
  yhat: number;
  p10: number;
  p50: number;
  p90: number;
  standard_deviation: number;
  probabilities: number[];
  fallback_level: string;
  action_id?: string | null;
}

interface MetricSeries {
  observed: Array<{ year: number; value: number; snapshot_revision_id: string }>;
  forecast: ForecastPoint[];
}

interface BridgeForecast {
  run_id: string;
  state_code: number;
  bridge_number: string;
  snapshot_revision_id: string;
  as_of_year: number;
  horizon_years: number;
  forecast_mode: string;
  model_version_id: string;
  model_id: string;
  evaluation_run_id: string;
  evaluation_receipt_id: string;
  qualification: string;
  source_data_generation: number;
  metrics: Record<string, MetricSeries>;
}

interface ForecastJob {
  job_id: string;
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';
  progress?: { phase?: string; completed?: number; total?: number };
  result?: Record<string, unknown> | null;
  error_code?: string | null;
}

const LABELS: Record<string, string> = {
  deck: 'Deck', superstructure: 'Superstructure', substructure: 'Substructure', culvert: 'Culvert',
};
const COLORS: Record<string, string> = {
  deck: '#16a34a', superstructure: '#d97706', substructure: '#dc2626', culvert: '#2563eb',
};

const detailText = (error: any, fallback: string) => {
  const detail = error?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (detail?.message) return detail.message;
  return fallback;
};

const Forecasts: React.FC = () => {
  const [summary, setSummary] = useState<ForecastSummary | null>(null);
  const [bridges, setBridges] = useState<ForecastBridge[]>([]);
  const [selectedBridge, setSelectedBridge] = useState<ForecastBridge | null>(null);
  const [forecast, setForecast] = useState<BridgeForecast | null>(null);
  const [horizonYears, setHorizonYears] = useState(10);
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(false);
  const [job, setJob] = useState<ForecastJob | null>(null);
  const [message, setMessage] = useState<Message | null>(null);
  const stopPolling = useRef(false);

  const refresh = async () => {
    setLoading(true);
    try {
      const [summaryResponse, bridgeResponse] = await Promise.all([
        axios.get<ForecastSummary>('/api/forecasts/v2/summary'),
        axios.get<{ bridges: ForecastBridge[] }>('/api/forecasts/v2/bridges?limit=5000'),
      ]);
      setSummary(summaryResponse.data);
      setBridges(bridgeResponse.data.bridges || []);
    } catch (error: any) {
      setMessage({ type: 'error', text: detailText(error, 'Could not load published forecast data.') });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { refresh(); return () => { stopPolling.current = true; }; }, []);

  const filteredBridges = useMemo(() => {
    const term = search.trim().toLowerCase();
    return bridges.filter((bridge) => !term || `${bridge.bridge_number} ${bridge.bridge_name || ''}`.toLowerCase().includes(term)).slice(0, 250);
  }, [bridges, search]);

  const pollJob = async (jobId: string) => {
    stopPolling.current = false;
    for (let attempt = 0; attempt < 900 && !stopPolling.current; attempt += 1) {
      const response = await axios.get<ForecastJob>(`/api/forecast-jobs/${jobId}`);
      setJob(response.data);
      if (['succeeded', 'failed', 'cancelled'].includes(response.data.status)) {
        if (response.data.status === 'succeeded') {
          setMessage({ type: 'success', text: 'The annual Markov model and historical hindcast receipt were stored.' });
          await refresh();
        } else {
          setMessage({ type: 'error', text: `Forecast job ${response.data.status}: ${response.data.error_code || 'unknown error'}.` });
        }
        return;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
  };

  const trainAndEvaluate = async () => {
    if (!summary?.available_years?.length) return;
    const throughYear = Math.max(...summary.available_years);
    const origins = summary.available_years.filter((year) => year < throughYear).slice(-6);
    if (!origins.length) {
      setMessage({ type: 'error', text: 'At least two published inventory years are required for historical validation.' });
      return;
    }
    setLoading(true);
    setMessage({ type: 'info', text: 'Queued annual model fitting and rolling-origin ground-truth evaluation.' });
    try {
      const response = await axios.post<ForecastJob>(
        '/api/forecast-jobs',
        {
          state_code: 34,
          through_year: throughYear,
          origins,
          expected_data_generation: summary.data_generation,
          minimum_cohort_exposure: 25,
          release_minimum_target_rows: 100,
          release_minimum_mae_skill: 0,
          release_maximum_candidate_mae: 1.5,
        },
        { headers: { 'Idempotency-Key': crypto.randomUUID() } },
      );
      setJob(response.data);
      void pollJob(response.data.job_id);
    } catch (error: any) {
      setMessage({ type: 'error', text: detailText(error, 'Could not queue the forecast job.') });
    } finally {
      setLoading(false);
    }
  };

  const cancelJob = async () => {
    if (!job?.job_id) return;
    stopPolling.current = true;
    try {
      const response = await axios.post<ForecastJob>(`/api/forecast-jobs/${job.job_id}/cancel`);
      setJob(response.data);
      setMessage({ type: 'info', text: 'Cancellation was requested.' });
    } catch (error: any) {
      setMessage({ type: 'error', text: detailText(error, 'Could not cancel the forecast job.') });
    }
  };

  const loadForecast = async (bridge: ForecastBridge) => {
    if (!summary) return;
    setSelectedBridge(bridge);
    setForecast(null);
    setLoading(true);
    try {
      const response = await axios.post<BridgeForecast>(
        '/api/forecasts/v2/bridge',
        {
          state_code: bridge.state_code,
          bridge_number: bridge.bridge_number,
          as_of_year: bridge.latest_year,
          horizon_years: horizonYears,
          expected_data_generation: summary.data_generation,
          mode: 'no_action',
        },
        { headers: { 'Idempotency-Key': crypto.randomUUID() } },
      );
      setForecast(response.data);
    } catch (error: any) {
      setMessage({ type: 'error', text: detailText(error, 'Could not create the exact-revision forecast.') });
    } finally {
      setLoading(false);
    }
  };

  const traces = useMemo<Data[]>(() => {
    if (!forecast) return [];
    return Object.entries(forecast.metrics).flatMap(([component, series]) => {
      const color = COLORS[component] || '#64748b';
      const label = LABELS[component] || component;
      const forecastYears = series.forecast.map((point) => point.year);
      return [
        {
          type: 'scatter', mode: 'lines+markers', name: `${label} observed`,
          x: series.observed.map((point) => point.year), y: series.observed.map((point) => point.value),
          line: { color, width: 3 }, marker: { size: 8 }, connectgaps: false,
          customdata: series.observed.map((point) => point.snapshot_revision_id),
          hovertemplate: `${label} observed<br>%{x}: %{y:.1f}<br>Revision %{customdata}<extra></extra>`,
        },
        {
          type: 'scatter', mode: 'lines', name: `${label} P10`, x: forecastYears,
          y: series.forecast.map((point) => point.p10), line: { color, dash: 'dot', width: 1 },
          hoverinfo: 'skip', showlegend: false,
        },
        {
          type: 'scatter', mode: 'lines', name: `${label} P10–P90`, x: forecastYears,
          y: series.forecast.map((point) => point.p90), line: { color, dash: 'dot', width: 1 },
          fill: 'tonexty', fillcolor: `${color}30`, hoverinfo: 'skip', showlegend: false,
        },
        {
          type: 'scatter', mode: 'lines+markers', name: `${label} expected`, x: forecastYears,
          y: series.forecast.map((point) => point.yhat), line: { color, width: 3, dash: 'dash' },
          customdata: series.forecast.map((point) => point.fallback_level),
          hovertemplate: `${label} expected<br>%{x}: %{y:.2f}<br>Cohort %{customdata}<extra></extra>`,
        },
      ] as Data[];
    });
  }, [forecast]);

  const layout = useMemo<Partial<Layout>>(() => ({
    height: 470, autosize: true, margin: { l: 55, r: 30, t: 25, b: 70 },
    paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)', hovermode: 'x unified',
    xaxis: { title: { text: 'Inventory / forecast year' }, dtick: 1, tickformat: 'd', gridcolor: '#e5e7eb' },
    yaxis: { title: { text: 'Condition rating' }, range: [0, 9], dtick: 1, gridcolor: '#e5e7eb' },
    legend: { orientation: 'h', y: -0.22 },
  }), []);

  const points = forecast ? Object.entries(forecast.metrics).flatMap(([component, series]) => series.forecast.map((point) => ({ component, ...point }))) : [];
  const evaluation = summary?.latest_package?.evaluation;

  return (
    <div className="space-y-7">
      <SectionHeader
        icon={<BrainCircuit className="w-6 h-6" />}
        title="Bridge Forecasts"
        subtitle="Annual action-aware Markov v2 · published snapshots only · LCCA deterioration source"
      />

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-5">
        <section className="glass rounded-3xl p-6 border border-gray-200 dark:border-gray-700">
          <div className="flex justify-between items-center mb-4">
            <h2 className="font-semibold text-lg">Published source</h2>
            <button onClick={refresh} className="p-2 rounded-xl hover:bg-gray-100" aria-label="Refresh forecast data">
              <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            </button>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Stat label="Bridge-years" value={summary?.published_observations || 0} />
            <Stat label="Bridges" value={summary?.published_bridges || 0} />
            <Stat label="Data generation" value={summary?.data_generation || 0} />
            <div className="rounded-2xl bg-slate-50 dark:bg-slate-900 p-4">
              <p className="text-xs text-slate-500">Years</p>
              <p className="font-bold text-lg">{summary?.available_years?.length ? `${summary.available_years[0]}–${summary.available_years[summary.available_years.length - 1]}` : 'None'}</p>
            </div>
          </div>
        </section>

        <section className="glass rounded-3xl p-6 border border-gray-200 dark:border-gray-700">
          <h2 className="font-semibold text-lg mb-4">Model and validation</h2>
          {summary?.latest_package ? (
            <div className="space-y-2 text-sm">
              <p className="flex items-center gap-2"><ShieldCheck className="w-4 h-4 text-emerald-600" /> {summary.latest_package.engine_version}</p>
              <p><span className="text-gray-500">Status:</span> {summary.latest_package.qualification.split('_').join(' ')}</p>
              <p><span className="text-gray-500">Ground-truth rows:</span> {evaluation?.target_rows.toLocaleString()}</p>
              <p><span className="text-gray-500">Candidate MAE:</span> {evaluation?.candidate_mae.toFixed(3)}</p>
              <p><span className="text-gray-500">Persistence MAE:</span> {evaluation?.persistence_mae.toFixed(3)}</p>
              <p><span className="text-gray-500">MAE skill:</span> {evaluation ? `${(evaluation.mae_skill * 100).toFixed(1)}%` : '—'}</p>
              {!summary.latest_package.release_qualified && <p className="text-amber-700">Research-unvalidated: LCCA commercial release remains blocked.</p>}
            </div>
          ) : <p className="text-sm text-gray-500">No v2 model package has been fitted.</p>}
          <button onClick={trainAndEvaluate} disabled={loading || !summary?.available_years?.length} className="mt-5 w-full rounded-2xl bg-indigo-600 text-white px-4 py-3 font-semibold disabled:opacity-50">
            Fit + historical validation
          </button>
          {job && ['queued', 'running'].includes(job.status) && (
            <button onClick={cancelJob} className="mt-2 w-full rounded-2xl border border-red-300 text-red-700 px-4 py-2 flex justify-center gap-2"><Square className="w-4 h-4" /> Cancel job</button>
          )}
          {job && <p className="mt-2 text-xs text-gray-500">{job.status} · {job.progress?.phase || 'waiting'}</p>}
        </section>

        <section className="glass rounded-3xl p-6 border border-gray-200 dark:border-gray-700">
          <h2 className="font-semibold text-lg mb-3">Bridge and horizon</h2>
          <select value={horizonYears} onChange={(event) => setHorizonYears(Number(event.target.value))} className="w-full rounded-2xl border px-4 py-3 bg-white dark:bg-gray-900 mb-3">
            {[1, 3, 5, 10, 15, 20, 30, 50, 100].map((years) => <option key={years} value={years}>{years} annual steps</option>)}
          </select>
          <div className="relative mb-3"><Search className="absolute left-4 top-3.5 w-4 h-4 text-gray-400" /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search bridge" className="w-full rounded-2xl border pl-11 pr-4 py-3 bg-white dark:bg-gray-900" /></div>
          <div className="max-h-52 overflow-auto space-y-2">
            {filteredBridges.map((bridge) => <button key={`${bridge.state_code}-${bridge.bridge_number}`} onClick={() => loadForecast(bridge)} className={`w-full text-left rounded-xl border px-3 py-2 ${selectedBridge?.bridge_number === bridge.bridge_number ? 'border-indigo-500 bg-indigo-50 dark:bg-indigo-900/30' : 'border-gray-200 dark:border-gray-700'}`}><p className="font-semibold">{bridge.bridge_number}</p><p className="text-xs text-gray-500">{bridge.bridge_name || 'Unnamed'} · {bridge.observation_years} years · latest {bridge.latest_year}</p></button>)}
          </div>
        </section>
      </div>

      {message && <div className={`rounded-2xl p-4 text-sm ${message.type === 'error' ? 'bg-red-50 text-red-700' : message.type === 'success' ? 'bg-emerald-50 text-emerald-700' : 'bg-blue-50 text-blue-700'}`}>{message.text}</div>}

      <section className="glass rounded-3xl p-6 border border-gray-200 dark:border-gray-700">
        <div className="mb-4"><h2 className="font-semibold text-xl">Observed history and no-action forecast</h2><p className="text-sm text-gray-500">{forecast ? `${forecast.bridge_number} · as of ${forecast.as_of_year} · exact revision ${forecast.snapshot_revision_id}` : 'Choose a bridge after a v2 model is available.'}</p></div>
        {loading && <div className="py-14 flex justify-center"><Loader2 className="animate-spin" /></div>}
        {!loading && forecast && traces.length > 0 && <><Plot data={traces} layout={layout} config={{ responsive: true, displaylogo: false }} style={{ width: '100%', height: '470px' }} useResizeHandler /><div className="grid md:grid-cols-3 gap-3 mt-4 text-xs"><Evidence label="Forecast mode" value={forecast.forecast_mode} /><Evidence label="Model / evaluation" value={`${forecast.model_id.slice(0, 10)}… / ${forecast.evaluation_receipt_id.slice(0, 10)}…`} /><Evidence label="Uncertainty" value="Full 0–9 distribution; shaded band is P10–P90" /></div></>}
        {!loading && !forecast && <p className="py-14 text-center text-gray-500">No forecast selected.</p>}
      </section>

      {forecast && <section className="glass rounded-3xl p-6 border border-gray-200 dark:border-gray-700"><h2 className="font-semibold text-xl mb-3">Annual forecast evidence</h2><div className="overflow-auto rounded-2xl border"><table className="min-w-full text-sm"><thead className="bg-gray-50 dark:bg-gray-900"><tr><th className="text-left px-4 py-3">Year</th><th className="text-left px-4 py-3">Component</th><th className="text-right px-4 py-3">Expected</th><th className="text-right px-4 py-3">P10</th><th className="text-right px-4 py-3">P50</th><th className="text-right px-4 py-3">P90</th><th className="text-left px-4 py-3">Cohort fallback</th></tr></thead><tbody>{points.map((point) => <tr key={`${point.component}-${point.year}`} className="border-t"><td className="px-4 py-3">{point.year}</td><td className="px-4 py-3">{LABELS[point.component] || point.component}</td><td className="px-4 py-3 text-right font-semibold">{point.yhat.toFixed(2)}</td><td className="px-4 py-3 text-right">{point.p10}</td><td className="px-4 py-3 text-right">{point.p50}</td><td className="px-4 py-3 text-right">{point.p90}</td><td className="px-4 py-3">{point.fallback_level}</td></tr>)}</tbody></table></div></section>}
    </div>
  );
};

const Stat = ({ label, value }: { label: string; value: number }) => <div className="rounded-2xl bg-slate-50 dark:bg-slate-900 p-4"><p className="text-xs text-slate-500">{label}</p><p className="text-2xl font-bold"><CountUp to={value} /></p></div>;
const Evidence = ({ label, value }: { label: string; value: string }) => <div className="rounded-xl bg-slate-50 dark:bg-slate-900 p-3"><p className="text-gray-500">{label}</p><p className="font-medium break-all">{value}</p></div>;

export default Forecasts;
