import React, { useEffect, useMemo, useState } from 'react';
import axios from 'axios';
import createPlotlyComponent from 'react-plotly.js/factory';
import Plotly from 'plotly.js-basic-dist-min';
import type { Data, Layout } from 'plotly.js';
import { BrainCircuit, Loader2, RefreshCw, Search, TrendingDown } from 'lucide-react';
import { SectionHeader, CountUp } from '../components/ui';

const Plot = createPlotlyComponent(Plotly);

interface ForecastSummary {
  training_rows: number;
  training_bridges: number;
  prediction_rows: number;
  training_years: { min: number | null; max: number | null };
  latest_run?: {
    id: number;
    run_timestamp: string;
    horizon_years: number;
    status: string;
    model_engine: string;
    message?: string;
  } | null;
  metrics: string[];
}

interface ForecastBridge {
  state_code: number;
  bridge_number: string;
  bridge_name?: string | null;
  observation_years: number;
}

interface MetricSeries {
  observed: Array<{ year: number; value: number }>;
  forecast: Array<{
    year: number;
    yhat: number;
    yhat_lower?: number | null;
    yhat_upper?: number | null;
    confidence: 'high' | 'medium' | 'low';
    observation_count: number;
    trend_direction: string;
  }>;
}

interface BridgeForecast {
  state_code: number;
  bridge_number: string;
  run_id?: number | null;
  metrics: Record<string, MetricSeries>;
}

const METRIC_LABELS: Record<string, string> = {
  deck: 'Deck',
  superstructure: 'Superstructure',
  substructure: 'Substructure',
  culvert: 'Culvert',
};

const COLORS: Record<string, string> = {
  deck: '#22c55e',
  superstructure: '#eab308',
  substructure: '#ef4444',
  culvert: '#2563eb',
};

// Tiny per-metric vertical offset + distinct marker so lines that share the same
// rating stay visible instead of hiding each other. The true rating is shown in
// the hover (via customdata) and in the prediction table below.
const METRIC_OFFSET: Record<string, number> = {
  deck: 0.08,
  superstructure: 0,
  substructure: -0.08,
  culvert: 0.16,
};
const METRIC_SYMBOL: Record<string, string> = {
  deck: 'circle',
  superstructure: 'square',
  substructure: 'triangle-up',
  culvert: 'diamond',
};

const Forecasts: React.FC = () => {
  const [summary, setSummary] = useState<ForecastSummary | null>(null);
  const [bridges, setBridges] = useState<ForecastBridge[]>([]);
  const [selectedBridge, setSelectedBridge] = useState<ForecastBridge | null>(null);
  const [forecast, setForecast] = useState<BridgeForecast | null>(null);
  const [horizonYears, setHorizonYears] = useState(10);
  const [search, setSearch] = useState('');
  const [loadingSummary, setLoadingSummary] = useState(false);
  const [working, setWorking] = useState(false);
  const [loadingForecast, setLoadingForecast] = useState(false);
  const [message, setMessage] = useState<{ type: 'success' | 'error' | 'info'; text: string } | null>(null);

  const fetchSummary = async () => {
    setLoadingSummary(true);
    try {
      const [summaryRes, bridgesRes] = await Promise.all([
        axios.get<ForecastSummary>('/api/forecasts/summary'),
        axios.get<{ bridges: ForecastBridge[] }>('/api/forecasts/bridges?limit=1000'),
      ]);
      setSummary(summaryRes.data);
      setBridges(bridgesRes.data.bridges || []);
    } catch (err: any) {
      setMessage({ type: 'error', text: err.response?.data?.detail || 'Failed to load forecast summary.' });
    } finally {
      setLoadingSummary(false);
    }
  };

  useEffect(() => {
    fetchSummary();
  }, []);

  const filteredBridges = useMemo(() => {
    const term = search.trim().toLowerCase();
    if (!term) return bridges.slice(0, 100);
    return bridges
      .filter((bridge) =>
        `${bridge.state_code} ${bridge.bridge_number} ${bridge.bridge_name || ''}`.toLowerCase().includes(term)
      )
      .slice(0, 100);
  }, [bridges, search]);

  const firstForecastYear = useMemo(() => {
    if (!forecast) return null;
    const years = Object.values(forecast.metrics || {})
      .flatMap((metric) => metric.forecast || [])
      .map((point) => point.year);
    return years.length ? Math.min(...years) : null;
  }, [forecast]);

  const plotlyTraces = useMemo<Data[]>(() => {
    if (!forecast) return [];
    const traces: Data[] = [];

    Object.entries(forecast.metrics || {}).forEach(([metric, series]) => {
      const label = METRIC_LABELS[metric] || metric;
      const color = COLORS[metric] || '#64748b';
      const ofs = METRIC_OFFSET[metric] ?? 0;
      const symbol = METRIC_SYMBOL[metric] || 'circle';
      const observed = [...(series.observed || [])].sort((a, b) => a.year - b.year);
      const predicted = [...(series.forecast || [])].sort((a, b) => a.year - b.year);

      if (observed.length) {
        traces.push({
          type: 'scatter',
          mode: 'lines+markers',
          name: `${label} observed`,
          x: observed.map((point) => point.year),
          y: observed.map((point) => point.value + ofs),
          customdata: observed.map((point) => point.value),
          line: { color, width: 3 },
          marker: { size: 8, symbol },
          hovertemplate: `${label} observed<br>Year %{x}<br>Rating %{customdata:.2f}<extra></extra>`,
        });
      }

      if (predicted.length) {
        const lower = predicted.map((point) => (point.yhat_lower ?? point.yhat) + ofs);
        const upper = predicted.map((point) => (point.yhat_upper ?? point.yhat) + ofs);
        traces.push({
          type: 'scatter',
          mode: 'lines',
          name: `${label} lower`,
          x: predicted.map((point) => point.year),
          y: lower,
          line: { color, width: 1.25, dash: 'dot' },
          opacity: 0.6,
          showlegend: false,
          hoverinfo: 'skip',
        });
        traces.push({
          type: 'scatter',
          mode: 'lines',
          name: `${label} confidence`,
          x: predicted.map((point) => point.year),
          y: upper,
          fill: 'tonexty',
          fillcolor: `${color}40`,
          line: { color, width: 1.25, dash: 'dot' },
          opacity: 0.6,
          showlegend: false,
          hoverinfo: 'skip',
        });
        traces.push({
          type: 'scatter',
          mode: 'lines+markers',
          name: `${label} future prediction`,
          x: predicted.map((point) => point.year),
          y: predicted.map((point) => point.yhat + ofs),
          customdata: predicted.map((point) => point.yhat),
          line: { color, width: 3, dash: 'dash' },
          marker: { size: 8, symbol },
          hovertemplate: `${label} prediction<br>Year %{x}<br>Rating %{customdata:.2f}<extra></extra>`,
        });
      }
    });

    return traces;
  }, [forecast]);

  const plotlyLayout = useMemo<Partial<Layout>>(() => {
    const shapes: Partial<Layout>['shapes'] = firstForecastYear
      ? [{
          type: 'line',
          x0: firstForecastYear,
          x1: firstForecastYear,
          y0: 0,
          y1: 9,
          line: { color: '#64748b', dash: 'dot', width: 2 },
        }]
      : [];

    return {
      autosize: true,
      height: 460,
      margin: { l: 55, r: 30, t: 30, b: 60 },
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      hovermode: 'x unified',
      xaxis: {
        title: { text: 'Year' },
        dtick: 1,
        tickformat: 'd',
        gridcolor: '#e5e7eb',
      },
      yaxis: {
        title: { text: 'Condition Rating' },
        range: [0, 9],
        dtick: 1,
        gridcolor: '#e5e7eb',
      },
      legend: {
        orientation: 'h',
        y: -0.2,
      },
      shapes,
      annotations: firstForecastYear
        ? [{
            x: firstForecastYear,
            y: 9,
            xanchor: 'left',
            yanchor: 'bottom',
            text: 'Future predictions start',
            showarrow: false,
            font: { color: '#64748b', size: 12 },
          }]
        : [],
    };
  }, [firstForecastYear]);

  const futurePredictionRows = useMemo(() => {
    if (!forecast) return [];
    return Object.entries(forecast.metrics || {}).flatMap(([metric, series]) =>
      (series.forecast || []).map((point) => ({
        metric,
        year: point.year,
        yhat: point.yhat,
        yhatLower: point.yhat_lower,
        yhatUpper: point.yhat_upper,
        confidence: point.confidence,
        trend: point.trend_direction,
      }))
    ).sort((a, b) => a.year - b.year || a.metric.localeCompare(b.metric));
  }, [forecast]);

  const loadForecast = async (bridge: ForecastBridge) => {
    setSelectedBridge(bridge);
    setForecast(null);
    setLoadingForecast(true);
    try {
      const res = await axios.get<BridgeForecast>(
        `/api/forecasts/bridge/${bridge.state_code}/${encodeURIComponent(bridge.bridge_number)}`
      );
      setForecast(res.data);
    } catch (err: any) {
      setMessage({ type: 'error', text: err.response?.data?.detail || 'Failed to load bridge forecast.' });
    } finally {
      setLoadingForecast(false);
    }
  };

  const buildTrainingDb = async () => {
    setWorking(true);
    setMessage({ type: 'info', text: 'Building forecast training database from loaded bridge snapshots...' });
    try {
      const res = await axios.post('/api/forecasts/build-training-db');
      setMessage({
        type: 'success',
        text: `Training DB built: ${res.data.inserted || 0} observations across ${res.data.bridges || 0} bridges.`,
      });
      await fetchSummary();
    } catch (err: any) {
      setMessage({ type: 'error', text: err.response?.data?.detail || 'Failed to build training database.' });
    } finally {
      setWorking(false);
    }
  };

  const trainForecasts = async () => {
    setWorking(true);
    setMessage({ type: 'info', text: 'Training forecasts for all bridges…' });
    try {
      const res = await axios.post(`/api/forecasts/train?horizon_years=${horizonYears}`);
      setMessage({
        type: 'success',
        text: `Forecast run ${res.data.run_id}: trained ${res.data.trained_models} models and wrote ${res.data.predictions} predictions.`,
      });
      await fetchSummary();
      if (selectedBridge) await loadForecast(selectedBridge);
    } catch (err: any) {
      const detail = err.response?.data?.detail || err.response?.data?.message || 'Failed to train forecasts.';
      setMessage({ type: 'error', text: detail });
    } finally {
      setWorking(false);
    }
  };

  const trainSelectedBridge = async () => {
    if (!selectedBridge) {
      setMessage({ type: 'error', text: 'Select a bridge before training a bridge forecast.' });
      return;
    }
    setWorking(true);
    setMessage({
      type: 'info',
      text: `Training ${horizonYears} future years for bridge ${selectedBridge.bridge_number}...`,
    });
    try {
      const res = await axios.post(
        `/api/forecasts/train-bridge/${selectedBridge.state_code}/${encodeURIComponent(selectedBridge.bridge_number)}?horizon_years=${horizonYears}`
      );
      setMessage({
        type: 'success',
        text: `Bridge forecast run ${res.data.run_id}: trained ${res.data.trained_models} metric models and wrote ${res.data.predictions} future predictions.`,
      });
      await fetchSummary();
      await loadForecast(selectedBridge);
    } catch (err: any) {
      const detail = err.response?.data?.detail || err.response?.data?.message || 'Failed to train selected bridge forecast.';
      setMessage({ type: 'error', text: detail });
    } finally {
      setWorking(false);
    }
  };

  const selectedMetricInfo = forecast
    ? Object.entries(forecast.metrics || {}).map(([metric, series]) => ({
        metric,
        observationCount: series.observed?.length || 0,
        forecastCount: series.forecast?.length || 0,
        confidence: series.forecast?.[0]?.confidence || 'low',
        trend: series.forecast?.[0]?.trend_direction || 'unknown',
      }))
    : [];

  return (
    <div className="space-y-8">
      <SectionHeader
        icon={<BrainCircuit className="w-6 h-6" />}
        title="Bridge Forecasts"
        subtitle="Prophet deterioration forecasts built from loaded yearly SNBI snapshots"
      />

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
        <div className="glass rounded-3xl p-6 shadow-sm border border-gray-200 dark:border-gray-700">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Forecast Database</h2>
            <button
              onClick={fetchSummary}
              disabled={loadingSummary}
              className="p-2 rounded-xl hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-500"
              title="Refresh"
            >
              {loadingSummary ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
            </button>
          </div>
          <div className="grid grid-cols-2 gap-3 text-sm">
            <div className="rounded-2xl bg-slate-50 dark:bg-slate-900/60 p-4">
              <p className="text-slate-500">Training Rows</p>
              <p className="text-2xl font-bold text-slate-900 dark:text-white"><CountUp to={summary?.training_rows ?? 0} /></p>
            </div>
            <div className="rounded-2xl bg-slate-50 dark:bg-slate-900/60 p-4">
              <p className="text-slate-500">Bridges</p>
              <p className="text-2xl font-bold text-slate-900 dark:text-white"><CountUp to={summary?.training_bridges ?? 0} /></p>
            </div>
            <div className="rounded-2xl bg-slate-50 dark:bg-slate-900/60 p-4">
              <p className="text-slate-500">Predictions</p>
              <p className="text-2xl font-bold text-slate-900 dark:text-white"><CountUp to={summary?.prediction_rows ?? 0} /></p>
            </div>
            <div className="rounded-2xl bg-gray-50 dark:bg-gray-900 p-4">
              <p className="text-gray-500">Years</p>
              <p className="text-2xl font-bold text-gray-900 dark:text-white">
                {summary?.training_years?.min ? `${summary.training_years.min}-${summary.training_years.max}` : 'None'}
              </p>
            </div>
          </div>
          {summary?.latest_run && (
            <div className="mt-4 rounded-2xl bg-indigo-50 dark:bg-indigo-900/30 p-4 text-sm text-indigo-800 dark:text-indigo-200">
              Latest run #{summary.latest_run.id}: {summary.latest_run.status} · {summary.latest_run.message}
            </div>
          )}
        </div>

        <div className="glass rounded-3xl p-6 shadow-sm border border-gray-200 dark:border-gray-700">
          <h2 className="text-lg font-semibold mb-4 text-gray-900 dark:text-white">Build And Train</h2>
          <label className="block text-sm text-gray-600 dark:text-gray-300 mb-2">Future Prediction Years</label>
          <select
            value={horizonYears}
            onChange={(e) => setHorizonYears(Number(e.target.value))}
            className="w-full rounded-2xl border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-4 py-3 text-gray-900 dark:text-white mb-3"
          >
            {[1, 3, 5, 10, 15, 20, 30, 50].map((years) => (
              <option key={years} value={years}>Predict {years} future years</option>
            ))}
          </select>
          <input
            type="number"
            min={1}
            max={50}
            value={horizonYears}
            onChange={(e) => setHorizonYears(Math.max(1, Math.min(50, Number(e.target.value) || 1)))}
            className="w-full rounded-2xl border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-4 py-3 text-gray-900 dark:text-white mb-2"
          />
          <div className="mb-4" />
          <div className="flex flex-col gap-3">
            <button
              onClick={buildTrainingDb}
              disabled={working}
              className="rounded-2xl bg-indigo-600 hover:bg-indigo-700 text-white px-5 py-3 font-semibold disabled:opacity-50"
            >
              {working ? 'Working...' : 'Build Training DB'}
            </button>
            <button
              onClick={trainForecasts}
              disabled={working || (summary?.training_rows ?? 0) === 0}
              className="rounded-2xl bg-emerald-600 hover:bg-emerald-700 text-white px-5 py-3 font-semibold disabled:opacity-50"
            >
              Train All Bridges For {horizonYears} Years
            </button>
            <button
              onClick={trainSelectedBridge}
              disabled={working || !selectedBridge || (summary?.training_rows ?? 0) === 0}
              className="rounded-2xl bg-blue-600 hover:bg-blue-700 text-white px-5 py-3 font-semibold disabled:opacity-50"
            >
              Train Selected Bridge For {horizonYears} Years
            </button>
          </div>
        </div>

        <div className="glass rounded-3xl p-6 shadow-sm border border-gray-200 dark:border-gray-700">
          <h2 className="text-lg font-semibold mb-4 text-gray-900 dark:text-white">Bridge Selection</h2>
          <div className="relative mb-3">
            <Search className="absolute left-4 top-3.5 w-4 h-4 text-gray-400" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search bridge number or name"
              className="w-full rounded-2xl border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 pl-11 pr-4 py-3 text-gray-900 dark:text-white"
            />
          </div>
          <div className="max-h-72 overflow-auto space-y-2">
            {filteredBridges.map((bridge) => (
              <button
                key={`${bridge.state_code}-${bridge.bridge_number}`}
                onClick={() => loadForecast(bridge)}
                className={`w-full text-left rounded-2xl px-4 py-3 border transition ${
                  selectedBridge?.bridge_number === bridge.bridge_number && selectedBridge?.state_code === bridge.state_code
                    ? 'border-indigo-500 bg-indigo-50 dark:bg-indigo-900/30'
                    : 'border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-900'
                }`}
              >
                <p className="font-semibold text-gray-900 dark:text-white">{bridge.bridge_number}</p>
                <p className="text-xs text-gray-500">{bridge.bridge_name || 'Unnamed'} · {bridge.observation_years} observed years</p>
              </button>
            ))}
            {filteredBridges.length === 0 && <p className="text-sm text-gray-500">No forecast training rows found yet.</p>}
          </div>
        </div>
      </div>

      {message && (
        <div className={`rounded-2xl p-4 text-sm ${
          message.type === 'success'
            ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300'
            : message.type === 'info'
              ? 'bg-blue-50 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300'
              : 'bg-red-50 text-red-700 dark:bg-red-900/30 dark:text-red-300'
        }`}>
          {message.text}
        </div>
      )}

      <div className="glass rounded-3xl p-6 shadow-sm border border-gray-200 dark:border-gray-700">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h2 className="text-xl font-semibold text-gray-900 dark:text-white">Observed And Forecasted Deterioration</h2>
            <p className="text-sm text-gray-500">
              {selectedBridge ? `${selectedBridge.bridge_number} · ${selectedBridge.bridge_name || 'Unnamed bridge'}` : 'Select a bridge to view forecasts.'}
            </p>
          </div>
          {loadingForecast && <Loader2 className="w-5 h-5 animate-spin text-indigo-600" />}
        </div>

        {forecast && plotlyTraces.length > 0 ? (
          <>
            <div className="rounded-2xl overflow-hidden border border-gray-100 dark:border-gray-700">
              <Plot
                data={plotlyTraces}
                layout={plotlyLayout}
                config={{ responsive: true, displaylogo: false }}
                style={{ width: '100%', height: '460px' }}
                useResizeHandler
              />
            </div>
            <div className="grid grid-cols-1 md:grid-cols-4 gap-3 mt-6">
              {selectedMetricInfo.map((info) => (
                <div key={info.metric} className="rounded-2xl bg-gray-50 dark:bg-gray-900 p-4">
                  <p className="font-semibold text-gray-900 dark:text-white flex items-center gap-2">
                    <TrendingDown className="w-4 h-4" />
                    {METRIC_LABELS[info.metric]}
                  </p>
                </div>
              ))}
            </div>
            <div className="mt-6">
              <h3 className="text-lg font-semibold text-gray-900 dark:text-white mb-3">Future Prediction Values</h3>
              {futurePredictionRows.length > 0 ? (
                <div className="overflow-auto rounded-2xl border border-gray-200 dark:border-gray-700">
                  <table className="min-w-full text-sm">
                    <thead className="bg-gray-50 dark:bg-gray-900 text-gray-600 dark:text-gray-300">
                      <tr>
                        <th className="text-left px-4 py-3">Year</th>
                        <th className="text-left px-4 py-3">Metric</th>
                        <th className="text-right px-4 py-3">Predicted Rating</th>
                        <th className="text-right px-4 py-3">Lower</th>
                        <th className="text-right px-4 py-3">Upper</th>
                        <th className="text-left px-4 py-3">Confidence</th>
                        <th className="text-left px-4 py-3">Trend</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
                      {futurePredictionRows.map((row) => (
                        <tr key={`${row.metric}-${row.year}`} className="text-gray-800 dark:text-gray-100">
                          <td className="px-4 py-3">{row.year}</td>
                          <td className="px-4 py-3">{METRIC_LABELS[row.metric] || row.metric}</td>
                          <td className="px-4 py-3 text-right font-semibold">{row.yhat.toFixed(2)}</td>
                          <td className="px-4 py-3 text-right">{row.yhatLower == null ? '-' : row.yhatLower.toFixed(2)}</td>
                          <td className="px-4 py-3 text-right">{row.yhatUpper == null ? '-' : row.yhatUpper.toFixed(2)}</td>
                          <td className="px-4 py-3 capitalize">{row.confidence}</td>
                          <td className="px-4 py-3 capitalize">{row.trend}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div className="rounded-2xl bg-amber-50 dark:bg-amber-900/30 p-4 text-sm text-amber-800 dark:text-amber-200">
                  This bridge has observed history but no future prediction rows. Train forecasts with at least 3 loaded years for a bridge metric.
                </div>
              )}
            </div>
          </>
        ) : (
          <div className="py-16 text-center text-gray-500">
            {selectedBridge ? 'No forecast predictions for this bridge yet. Train forecasts first.' : 'Select a bridge after building the training database.'}
          </div>
        )}
      </div>
    </div>
  );
};

export default Forecasts;
