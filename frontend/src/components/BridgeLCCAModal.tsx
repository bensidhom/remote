import React, { useEffect, useMemo, useState } from 'react';
import axios from 'axios';
import {
  CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { Coins, ExternalLink, Loader2, Play, RotateCcw, ShieldAlert } from 'lucide-react';
import { Button, Modal } from './ui';

interface Props {
  stateCode?: number | string;
  bridgeName?: string;
  bridgeNumber: string;
  yearBuilt?: string | number;
  dataYear?: number | string;
  onClose: () => void;
  embedded?: boolean;
}

interface StatePoint { component: string; expected_rating: number; most_likely_rating: number; probabilities: number[]; }
interface LedgerRow {
  year: number; pre_action_states: StatePoint[]; post_action_states: StatePoint[];
  selected_action_ids: string[]; base_cost: string; future_cost: string;
  inflation_factor: string; discount_factor: string; present_value_cost: string;
}
interface Alternative {
  strategy_id: string; total_present_value_cost: string; equivalent_annual_cost: string;
  economic_mode: string; scheduled_actions: Array<{ year: number; treatment_id: string; present_cost: string; cost_unit: string }>;
  applicability_issues: Array<{ treatment_id: string; status: string; reasons: string[] }>;
  annual_ledger: LedgerRow[];
}
interface LccaResponse {
  scenario_receipt_sha256: string; status: string; planning_only: boolean;
  identity: { data_year: number; snapshot_revision_id: string; canonical_sha256: string };
  facts: {
    bridge_name?: string | null; age_at_snapshot?: number | null; component_ratings: Record<string, number | null>;
    controlling_rating?: number | null; controlling_basis: string; deck_area?: number | null;
    deck_area_method: string; deck_area_status: string; nhs?: boolean | null; nhs_status: string; nhs_source: string;
    deck_material_codes: string[]; span_material_codes: string[]; substructure_material_codes: string[];
  };
  forecast: { model_id: string; evaluation_receipt_id: string; qualification: string };
  treatment_library: {
    library_id: string; sha256: string; effect_evidence: string; source_title: string;
    treatments: Array<{ treatment_id: string; title: string; component: string; trigger_kind: string; cycle_years?: number | null; source_reference: string; source_url: string }>;
  };
  life_history: {
    construction_year: number; unknown_history_interval?: [number, number] | null;
    events: Array<{ event_id: string; event_year: number; component: string; treatment_id?: string | null; evidence_class: string; before_rating?: number | null; after_rating?: number | null }>;
  };
  alternatives: Alternative[];
}

const STRATEGIES = [
  ['no_action', 'No capital action'],
  ['njdot_preventive_cycles', 'Typical NJDOT preventive-maintenance cycles'],
  ['condition_triggered_rehabilitation', 'Condition-triggered rehabilitation'],
  ['deck_and_substructure_rehabilitation', 'Deck + substructure rehabilitation now'],
  ['full_replacement', 'Full replacement now'],
] as const;

const COST_FIELDS = [
  ['bridge_cleaning', 'Bridge cleaning event'],
  ['deck_seal', 'Deck sealing event'],
  ['joint_repair_replace', 'Joint repair/replacement event'],
  ['deck_replacement', 'Deck replacement'],
  ['superstructure_rehabilitation', 'Superstructure rehabilitation'],
  ['substructure_rehabilitation', 'Substructure rehabilitation'],
  ['full_replacement', 'Full bridge replacement'],
] as const;

const STRATEGY_COLORS: Record<string, string> = {
  no_action: '#dc2626', njdot_preventive_cycles: '#16a34a',
  condition_triggered_rehabilitation: '#2563eb', deck_and_substructure_rehabilitation: '#d97706',
  full_replacement: '#7c3aed', explicit_schedule: '#0891b2',
};

const money = (value: string | number | null | undefined) => {
  if (value == null || value === '') return '—';
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(Number(value));
};
const label = (value: string) => value.split('_').map((word) => word[0]?.toUpperCase() + word.slice(1)).join(' ');
const detailText = (error: any) => error?.response?.data?.detail?.message || error?.response?.data?.detail || 'Could not run the annual LCCA simulation.';

const BridgeLCCAModal: React.FC<Props> = ({
  stateCode = 34, bridgeName, bridgeNumber, yearBuilt, dataYear, onClose, embedded,
}) => {
  const [horizon, setHorizon] = useState(50);
  const [discountRate, setDiscountRate] = useState('2.5');
  const [conditionThreshold, setConditionThreshold] = useState('4.5');
  const [strategies, setStrategies] = useState<string[]>(['no_action', 'njdot_preventive_cycles', 'condition_triggered_rehabilitation']);
  const [costs, setCosts] = useState<Record<string, string>>({});
  const [enableResearchEffects, setEnableResearchEffects] = useState(false);
  const [generation, setGeneration] = useState<number | null>(null);
  const [data, setData] = useState<LccaResponse | null>(null);
  const [selectedAlternative, setSelectedAlternative] = useState('no_action');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    axios.get<{ data_generation: number }>('/api/forecasts/v2/summary')
      .then((response) => setGeneration(response.data.data_generation))
      .catch(() => setError('Could not resolve the current published data generation.'));
  }, []);

  const run = async () => {
    if (generation == null) return;
    setLoading(true); setError(null);
    try {
      const actionCosts = Object.fromEntries(Object.entries(costs).filter(([, value]) => value !== '').map(([key, value]) => [key, value]));
      const response = await axios.post<LccaResponse>('/api/lcca/v2/bridge', {
        state_code: Number(stateCode), bridge_number: bridgeNumber, as_of_year: dataYear || null,
        expected_data_generation: generation, horizon_years: horizon, strategies,
        economic_mode: 'constant_dollar', real_discount_rate: Number(discountRate) / 100,
        condition_threshold: Number(conditionThreshold), action_costs: actionCosts,
        explicit_actions: [], enable_research_effect_assumptions: enableResearchEffects,
      });
      setData(response.data);
      setSelectedAlternative(response.data.alternatives[0]?.strategy_id || 'no_action');
    } catch (requestError: any) {
      setError(detailText(requestError));
    } finally { setLoading(false); }
  };

  const toggleStrategy = (strategy: string) => setStrategies((current) => current.includes(strategy)
    ? current.filter((item) => item !== strategy)
    : [...current, strategy]);
  const reset = () => {
    setHorizon(50); setDiscountRate('2.5'); setConditionThreshold('4.5');
    setStrategies(['no_action', 'njdot_preventive_cycles', 'condition_triggered_rehabilitation']);
    setCosts({}); setEnableResearchEffects(false); setData(null); setError(null);
  };

  const chartRows = useMemo(() => {
    if (!data) return [];
    const rows = new Map<number, Record<string, number>>();
    data.alternatives.forEach((alternative) => alternative.annual_ledger.forEach((entry) => {
      const current = rows.get(entry.year) || { year: entry.year };
      const ratings = entry.post_action_states.map((state) => state.expected_rating);
      current[alternative.strategy_id] = ratings.length ? Math.min(...ratings) : 0;
      rows.set(entry.year, current);
    }));
    return [...rows.values()].sort((a, b) => a.year - b.year);
  }, [data]);
  const active = data?.alternatives.find((item) => item.strategy_id === selectedAlternative);
  const lowest = data?.alternatives.reduce<Alternative | null>((best, item) => !best || Number(item.total_present_value_cost) < Number(best.total_present_value_cost) ? item : best, null);

  return (
    <Modal open onClose={onClose} inline={embedded} size="lg" title="Annual Forecast-Driven LCCA" icon={<Coins className="w-5 h-5" />}>
      <p className="text-sm text-slate-500 -mt-1 mb-4">{bridgeName || 'Bridge'} — {bridgeNumber}{yearBuilt ? ` · Built ${yearBuilt}` : ''}</p>
      <div className="rounded-2xl border border-amber-300 bg-amber-50 dark:bg-amber-950/30 p-4 text-sm text-amber-900 dark:text-amber-200 flex gap-3">
        <ShieldAlert className="w-5 h-5 shrink-0" /><span>Planning/research comparison. Forecast qualification, treatment-effect evidence, costs, geometry, and source revision remain visible; “lowest cost” is not an engineering recommendation.</span>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5 mt-5">
        <section className="lg:col-span-2 rounded-2xl border border-slate-200 dark:border-slate-700 p-5">
          <h3 className="font-semibold mb-3">Simulation strategies</h3>
          <div className="grid md:grid-cols-2 gap-2">
            {STRATEGIES.map(([id, title]) => <label key={id} className="flex gap-3 rounded-xl border p-3 cursor-pointer"><input type="checkbox" checked={strategies.includes(id)} onChange={() => toggleStrategy(id)} /><span className="text-sm">{title}</span></label>)}
          </div>
          <label className="mt-4 flex gap-3 rounded-xl border border-indigo-200 bg-indigo-50 dark:bg-indigo-950/30 p-3 text-sm">
            <input type="checkbox" checked={enableResearchEffects} onChange={(event) => setEnableResearchEffects(event.target.checked)} />
            <span><strong>Enable illustrative treatment effects.</strong> This explicitly applies research sensitivity assumptions (preservation +0, rehabilitation +2, replacement reset). It is not learned or validated treatment performance.</span>
          </label>
        </section>
        <section className="rounded-2xl border border-slate-200 dark:border-slate-700 p-5 space-y-3">
          <NumberField label="Annual simulation horizon" value={String(horizon)} suffix="years" onChange={(value) => setHorizon(Math.max(1, Math.min(100, Number(value) || 1)))} />
          <NumberField label="Real discount rate" value={discountRate} suffix="%" step="0.1" onChange={setDiscountRate} />
          <NumberField label="Rehabilitation trigger" value={conditionThreshold} suffix="rating" step="0.1" onChange={setConditionThreshold} />
          <div className="flex gap-2"><Button onClick={run} disabled={loading || !strategies.length || generation == null}><Play className="w-4 h-4 mr-2" /> Run yearly simulation</Button><button onClick={reset} className="p-2 rounded-xl border" title="Reset"><RotateCcw className="w-4 h-4" /></button></div>
        </section>
      </div>

      <section className="rounded-2xl border border-slate-200 dark:border-slate-700 p-5 mt-5">
        <h3 className="font-semibold mb-1">Action costs in base-year dollars</h3>
        <p className="text-xs text-slate-500 mb-4">No hidden default cost is used. Blank actions cost $0 and are visibly retained in the ledger for condition-only experimentation.</p>
        <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-3">{COST_FIELDS.map(([id, title]) => <NumberField key={id} label={title} value={costs[id] || ''} suffix="USD/event" onChange={(value) => setCosts((current) => ({ ...current, [id]: value }))} />)}</div>
      </section>

      {loading && <div className="py-14 text-center text-slate-500"><Loader2 className="w-8 h-8 animate-spin mx-auto mb-3" />Advancing every component one year at a time…</div>}
      {error && !loading && <div className="rounded-2xl bg-red-50 text-red-700 p-4 mt-5 text-sm">{error}</div>}

      {data && !loading && <div className="space-y-5 mt-5">
        <section className="rounded-2xl border p-5">
          <div className="grid md:grid-cols-4 gap-3 text-sm">
            <Fact title="Exact source" value={`${data.identity.data_year} · ${data.identity.snapshot_revision_id}`} />
            <Fact title="Deck area" value={data.facts.deck_area ? `${data.facts.deck_area.toLocaleString()} · ${data.facts.deck_area_method}` : `Unavailable · ${data.facts.deck_area_method}`} />
            <Fact title="NHS evidence" value={`${data.facts.nhs == null ? 'Unknown' : data.facts.nhs ? 'Yes' : 'No'} · ${data.facts.nhs_source}`} />
            <Fact title="Forecast" value={`${data.forecast.qualification} · ${data.forecast.model_id.slice(0, 10)}…`} />
          </div>
        </section>

        <section className="grid md:grid-cols-2 xl:grid-cols-3 gap-3">
          {data.alternatives.map((alternative) => <button key={alternative.strategy_id} onClick={() => setSelectedAlternative(alternative.strategy_id)} className={`text-left rounded-2xl border p-4 ${selectedAlternative === alternative.strategy_id ? 'border-indigo-500 bg-indigo-50 dark:bg-indigo-950/30' : 'border-slate-200 dark:border-slate-700'}`}><p className="text-xs uppercase text-slate-500">{label(alternative.strategy_id)}</p><p className="text-2xl font-bold mt-1">{money(alternative.total_present_value_cost)}</p><p className="text-xs text-slate-500">NPV · EAC {money(alternative.equivalent_annual_cost)} · {alternative.scheduled_actions.length} actions</p>{lowest?.strategy_id === alternative.strategy_id && <p className="text-xs text-emerald-700 mt-2">Lowest modeled planning cost</p>}</button>)}
        </section>

        <section className="rounded-2xl border p-5"><h3 className="font-semibold mb-3">Expected controlling-component path</h3><ResponsiveContainer width="100%" height={310}><LineChart data={chartRows}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="year" /><YAxis domain={[0, 9]} ticks={[0,1,2,3,4,5,6,7,8,9]} /><Tooltip /><Legend />{data.alternatives.map((alternative) => <Line key={alternative.strategy_id} dataKey={alternative.strategy_id} name={label(alternative.strategy_id)} stroke={STRATEGY_COLORS[alternative.strategy_id] || '#64748b'} dot={false} connectNulls={false} />)}</LineChart></ResponsiveContainer></section>

        {active && <section className="rounded-2xl border p-5"><h3 className="font-semibold mb-3">Year-by-year ledger · {label(active.strategy_id)}</h3><div className="overflow-auto max-h-[430px] rounded-xl border"><table className="min-w-full text-xs"><thead className="sticky top-0 bg-slate-50 dark:bg-slate-900"><tr><th className="text-left p-3">Year</th><th className="text-left p-3">Action</th><th className="text-left p-3">Pre-action condition</th><th className="text-left p-3">Post-action condition</th><th className="text-right p-3">Base cost</th><th className="text-right p-3">Future cost</th><th className="text-right p-3">Present value</th></tr></thead><tbody>{active.annual_ledger.map((row) => <tr key={row.year} className="border-t"><td className="p-3">{row.year}</td><td className="p-3">{row.selected_action_ids.map(label).join(', ') || 'None'}</td><td className="p-3">{row.pre_action_states.map((state) => `${label(state.component)} ${state.expected_rating.toFixed(2)}`).join(' · ')}</td><td className="p-3">{row.post_action_states.map((state) => `${label(state.component)} ${state.expected_rating.toFixed(2)}`).join(' · ')}</td><td className="p-3 text-right">{money(row.base_cost)}</td><td className="p-3 text-right">{money(row.future_cost)}</td><td className="p-3 text-right font-semibold">{money(row.present_value_cost)}</td></tr>)}</tbody></table></div></section>}

        <section className="rounded-2xl border p-5"><h3 className="font-semibold mb-3">Life since construction</h3>{data.life_history.unknown_history_interval && <p className="text-xs text-amber-700 mb-3">History unavailable from {data.life_history.unknown_history_interval[0]} through {data.life_history.unknown_history_interval[1]}; no maintenance was invented.</p>}<div className="space-y-2 max-h-56 overflow-auto">{data.life_history.events.map((event) => <div key={event.event_id} className="flex gap-3 text-sm"><span className="font-mono text-slate-500">{event.event_year}</span><span><strong>{label(event.evidence_class)}</strong> · {label(event.component)}{event.treatment_id ? ` · ${label(event.treatment_id)}` : ''}{event.before_rating != null ? ` · ${event.before_rating}→${event.after_rating}` : ''}</span></div>)}</div></section>

        <section className="rounded-2xl border p-5 text-xs text-slate-500"><p>Scenario receipt: <span className="font-mono">{data.scenario_receipt_sha256}</span></p><p>Treatment library: {data.treatment_library.source_title} · <span className="font-mono">{data.treatment_library.sha256.slice(0, 16)}…</span></p><div className="flex gap-4 mt-2"><a href="https://www.nj.gov/transportation/about/asset/pdf/NJ_2019_TAMP_FHWA.pdf" target="_blank" rel="noreferrer" className="text-indigo-600 inline-flex gap-1">NJDOT TAMP <ExternalLink className="w-3 h-3" /></a><a href="https://www.fhwa.dot.gov/bridge/preservation/guide/guide.pdf" target="_blank" rel="noreferrer" className="text-indigo-600 inline-flex gap-1">FHWA preservation guide <ExternalLink className="w-3 h-3" /></a></div></section>
      </div>}
    </Modal>
  );
};

const NumberField = ({ label: title, value, suffix, step = '1', onChange }: { label: string; value: string; suffix: string; step?: string; onChange: (value: string) => void }) => <label className="block"><span className="text-xs text-slate-500">{title}</span><div className="flex items-center gap-2 mt-1"><input type="number" min="0" step={step} value={value} onChange={(event) => onChange(event.target.value)} className="w-full rounded-xl border px-3 py-2 bg-white dark:bg-slate-900" /><span className="text-xs text-slate-400 whitespace-nowrap">{suffix}</span></div></label>;
const Fact = ({ title, value }: { title: string; value: string }) => <div className="rounded-xl bg-slate-50 dark:bg-slate-900 p-3"><p className="text-xs text-slate-500">{title}</p><p className="font-medium break-words">{value}</p></div>;

export default BridgeLCCAModal;
