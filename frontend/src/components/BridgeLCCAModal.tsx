import React, { useCallback, useEffect, useMemo, useState } from 'react';
import axios from 'axios';
import {
  CartesianGrid, ComposedChart, Line, ReferenceArea, ReferenceLine, ResponsiveContainer,
  Scatter, Tooltip, XAxis, YAxis,
} from 'recharts';
import { Coins, ExternalLink, Loader2, RotateCcw } from 'lucide-react';
import { Modal } from './ui';

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
  gross_present_value_cost: string; remaining_service_life_credit: string;
  feasibility_status: string; feasibility_reasons: string[]; rankable: boolean; cost_complete: boolean;
  minimum_controlling_rating?: number | null; meets_service_threshold: boolean;
  sensitivity: Array<{ dimension: string; value: string; total_present_value_cost: string; minimum_controlling_rating?: number; ending_controlling_rating?: number }>;
  economic_mode: string; scheduled_actions: Array<{
    year: number; treatment_id: string; present_cost: string; cost_unit: string;
    cost_status: string; cost_source: string; included_in?: string | null; service_life_years?: number | null;
  }>;
  applicability_issues: Array<{ treatment_id: string; status: string; reasons: string[] }>;
  annual_ledger: LedgerRow[];
}
interface ChartRow {
  year: number; condition: number | null; annualCost: number; cumulativeCost: number;
  deck: number | null; superstructure: number | null; substructure: number | null; culvert: number | null;
  actionLabel: string; hasAction: boolean;
}
type ComparisonChartRow = { year: number } & Record<string, number | string | null>;
interface LccaResponse {
  scenario_receipt_sha256: string; status: string;
  identity: { data_year: number; snapshot_revision_id: string; canonical_sha256: string };
  facts: {
    bridge_name?: string | null; age_at_snapshot?: number | null; component_ratings: Record<string, number | null>;
    controlling_rating?: number | null; controlling_basis: string; deck_area?: number | null;
    deck_area_method: string; deck_area_status: string; nhs?: boolean | null; nhs_status: string; nhs_source: string;
    deck_material_codes: string[]; span_material_codes: string[]; substructure_material_codes: string[];
  };
  forecast: { model_id: string; evaluation_receipt_id: string };
  analysis: {
    period_status: string; minimum_comparable_years: number; cost_scope: string; excluded_costs: string[];
    effect_assumptions: { preservation_deterioration_reduction: string; duration_basis: string };
    condition_policy: { rehabilitation_trigger: number; minimum_service_rating: number };
  };
  cost_assumptions: {
    library_id: string; version: string; resolved_action_costs: Record<string, string>;
    preservation_cost_interpretation: string;
  };
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
  { id: 'njdot_preventive_cycles', title: 'Minimum preventive maintenance' },
  { id: 'reactive_management', title: 'Condition-triggered rehabilitation' },
  { id: 'deck_and_substructure_rehabilitation', title: 'Scheduled major rehabilitation' },
  { id: 'full_replacement', title: 'Full reconstruction' },
] as const;

const DEFAULT_STRATEGIES = STRATEGIES.map(({ id }) => id);

const NJDOT_TAMP_SOURCE_URL = 'https://dot.nj.gov/transportation/about/asset/pdf/NJTAMP.pdf';
const COST_FIELDS = [
  { id: 'njdot_preservation_package', title: 'Generic preservation project', rate: 206 },
  { id: 'deck_replacement', title: 'Deck replacement', rate: 511 },
  { id: 'superstructure_rehabilitation', title: 'Superstructure rehabilitation', rate: 149 },
  { id: 'substructure_rehabilitation', title: 'Substructure rehabilitation', rate: 124 },
  { id: 'full_replacement', title: 'Full bridge replacement', rate: 2556 },
] as const;

const STRATEGY_COLORS: Record<string, string> = {
  no_action: '#64748b', njdot_preventive_cycles: '#0f766e',
  reactive_management: '#2563eb', condition_triggered_rehabilitation: '#2563eb', deck_and_substructure_rehabilitation: '#d97706',
  full_replacement: '#7c3aed', explicit_schedule: '#0891b2',
};

const money = (value: string | number | null | undefined) => {
  if (value == null || value === '') return '—';
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(Number(value));
};
const compactMoney = (value: string | number) => new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD', notation: 'compact', maximumFractionDigits: 1,
}).format(Number(value));
const label = (value: string) => value.split('_').map((word) => word[0]?.toUpperCase() + word.slice(1)).join(' ');
const strategyLabel = (value: string) => STRATEGIES.find(({ id }) => id === value)?.title || label(value);
const detailText = (error: any) => error?.response?.data?.detail?.message || error?.response?.data?.detail || 'Could not run the annual LCCA simulation.';
const chartKey = (strategyId: string, metric: 'condition' | 'cumulativeCost' | 'annualCost' | 'actionLabel') => `${strategyId}__${metric}`;

const ComparisonTooltip = ({ active, label: year, rows, alternatives }: {
  active?: boolean; label?: number | string; rows: ComparisonChartRow[]; alternatives: Alternative[];
}) => {
  const row = rows.find((item) => item.year === Number(year));
  if (!active || !row) return null;
  return (
    <div className="max-w-sm rounded-xl border border-slate-200 bg-white p-3 text-xs shadow-xl dark:border-slate-700 dark:bg-slate-900">
      <p className="mb-2 font-semibold">{year}</p>
      <div className="space-y-2">
        {alternatives.map((alternative) => {
          const strategyId = alternative.strategy_id;
          const condition = row[chartKey(strategyId, 'condition')];
          const cumulativeCost = row[chartKey(strategyId, 'cumulativeCost')];
          const annualCost = row[chartKey(strategyId, 'annualCost')];
          const action = row[chartKey(strategyId, 'actionLabel')];
          return (
            <div key={strategyId} className="border-t border-slate-100 pt-2 first:border-0 first:pt-0 dark:border-slate-800">
              <p className="flex items-center gap-2 font-semibold"><span className="h-2.5 w-2.5 rounded-full" style={{ background: STRATEGY_COLORS[strategyId] }} />{strategyLabel(strategyId)}</p>
              <p className="mt-0.5 text-slate-600 dark:text-slate-300">Condition {condition == null ? '—' : Number(condition).toFixed(2)} · cumulative {money(cumulativeCost as number)}</p>
              {action && <p className="font-medium" style={{ color: STRATEGY_COLORS[strategyId] }}>Action: {String(action)} · {money(annualCost as number)}</p>}
            </div>
          );
        })}
      </div>
    </div>
  );
};

const strategyDetails = (
  id: string,
  inventoryYear: number,
  conditionThreshold: string,
  rehabilitationYear: number,
  replacementYear: number,
) => {
  if (id === 'njdot_preventive_cycles') return {
    description: 'The minimum recurring NJDOT program: applicable cleaning, sealing, joint, and component-preservation cycles.',
    timing: `First cleaning ${inventoryYear + 2} · sealing ${inventoryYear + 5} · joints ${inventoryYear + 10}`,
  };
  if (id === 'reactive_management' || id === 'condition_triggered_rehabilitation') return {
    description: 'Keeps the preventive baseline and adds component rehabilitation when the modeled condition reaches the trigger.',
    timing: `Preventive cycles + rehabilitation at rating ≤ ${conditionThreshold}`,
  };
  if (id === 'deck_and_substructure_rehabilitation') return {
    description: 'Keeps the preventive baseline and schedules deck replacement with substructure rehabilitation.',
    timing: `Preventive cycles + major rehabilitation in ${rehabilitationYear}`,
  };
  return {
    description: 'Keeps the preventive baseline and schedules full reconstruction, resetting all modeled component conditions.',
    timing: `Preventive cycles + reconstruction in ${replacementYear}`,
  };
};

const StrategyMiniTimeline = ({ id, inventoryYear, horizon, actionYear }: {
  id: string; inventoryYear: number; horizon: number; actionYear?: number;
}) => {
  const endYear = inventoryYear + horizon;
  const xForYear = (year: number) => 10 + Math.max(0, Math.min(1, (year - inventoryYear) / Math.max(1, horizon))) * 120;
  if (id === 'njdot_preventive_cycles') return (
    <svg viewBox="0 0 140 34" className="h-9 w-full" role="img" aria-label="Repeated preventive-maintenance cycles">
      <line x1="10" y1="17" x2="130" y2="17" stroke="#cbd5e1" strokeWidth="2" />
      {[2, 5, 10, 15, 20].filter((offset) => offset <= horizon).map((offset, index) => <circle key={offset} cx={xForYear(inventoryYear + offset)} cy="17" r={index < 3 ? 4 : 3} fill={index === 0 ? '#16a34a' : '#4f46e5'} />)}
    </svg>
  );
  if (id === 'reactive_management' || id === 'condition_triggered_rehabilitation') return (
    <svg viewBox="0 0 140 34" className="h-9 w-full" role="img" aria-label="Condition declines to a trigger and then improves after rehabilitation">
      <path d="M10 7 L76 25 L80 25 L84 9 L130 18" fill="none" stroke="#2563eb" strokeWidth="2.5" />
      <line x1="10" y1="25" x2="130" y2="25" stroke="#ef4444" strokeDasharray="4 3" />
      <circle cx="80" cy="25" r="4" fill="#7c3aed" />
    </svg>
  );
  const markerX = xForYear(actionYear ?? inventoryYear);
  return (
    <svg viewBox="0 0 140 34" className="h-9 w-full" role="img" aria-label={`One scheduled action between ${inventoryYear} and ${endYear}`}>
      <line x1="10" y1="17" x2="130" y2="17" stroke="#cbd5e1" strokeWidth="2" />
      <line x1={markerX} y1="7" x2={markerX} y2="27" stroke="#7c3aed" strokeWidth="2" />
      <circle cx={markerX} cy="17" r="5" fill="#7c3aed" />
    </svg>
  );
};

const BridgeLCCAModal: React.FC<Props> = ({
  stateCode = 34, bridgeName, bridgeNumber, yearBuilt, dataYear, onClose, embedded,
}) => {
  const inventoryYear = Number.isFinite(Number(dataYear)) ? Number(dataYear) : new Date().getFullYear();
  const [horizon, setHorizon] = useState(50);
  const [discountRate, setDiscountRate] = useState('2.5');
  const [conditionThreshold, setConditionThreshold] = useState('4.5');
  const [minimumServiceRating, setMinimumServiceRating] = useState('3.0');
  const [rehabilitationYear, setRehabilitationYear] = useState(inventoryYear + 5);
  const [replacementYear, setReplacementYear] = useState(inventoryYear + 15);
  const [strategies, setStrategies] = useState<string[]>([...DEFAULT_STRATEGIES]);
  const [costs, setCosts] = useState<Record<string, string>>({});
  const [defaultCostsApplied, setDefaultCostsApplied] = useState(false);
  const [generation, setGeneration] = useState<number | null>(null);
  const [data, setData] = useState<LccaResponse | null>(null);
  const [selectedAlternative, setSelectedAlternative] = useState('njdot_preventive_cycles');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    axios.get<{ data_generation: number }>('/api/forecasts/v2/summary')
      .then((response) => setGeneration(response.data.data_generation))
      .catch(() => setError('Could not resolve the current inventory data generation.'));
  }, []);

  const run = useCallback(async (signal?: AbortSignal) => {
    if (generation == null) return;
    setLoading(true); setError(null);
    try {
      const actionCosts = Object.fromEntries(Object.entries(costs).filter(([, value]) => value !== '').map(([key, value]) => [key, value]));
      const response = await axios.post<LccaResponse>('/api/lcca/v2/bridge', {
        state_code: Number(stateCode), bridge_number: bridgeNumber, as_of_year: dataYear || null,
        expected_data_generation: generation, horizon_years: horizon, strategies,
        economic_mode: 'constant_dollar', real_discount_rate: Number(discountRate) / 100,
        condition_threshold: Number(conditionThreshold), action_costs: actionCosts,
        minimum_service_rating: Number(minimumServiceRating),
        rehabilitation_year: rehabilitationYear, replacement_year: replacementYear,
        explicit_actions: [], enable_effect_assumptions: true,
      }, { signal });
      setData(response.data);
      setSelectedAlternative((current) => response.data.alternatives.some((item) => item.strategy_id === current)
        ? current
        : response.data.alternatives[0]?.strategy_id || 'njdot_preventive_cycles');
    } catch (requestError: any) {
      if (axios.isCancel(requestError) || requestError?.code === 'ERR_CANCELED') return;
      setError(detailText(requestError));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [bridgeNumber, conditionThreshold, costs, dataYear, discountRate, generation, horizon, minimumServiceRating, rehabilitationYear, replacementYear, stateCode, strategies]);

  useEffect(() => {
    if (generation == null || !strategies.length) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => void run(controller.signal), 450);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [generation, run, strategies.length]);

  useEffect(() => {
    setRehabilitationYear((year) => Math.max(inventoryYear, Math.min(year, inventoryYear + horizon)));
    setReplacementYear((year) => Math.max(inventoryYear, Math.min(year, inventoryYear + horizon)));
  }, [horizon, inventoryYear]);

  useEffect(() => {
    const resolved = data?.cost_assumptions.resolved_action_costs;
    if (defaultCostsApplied || !resolved) return;
    setCosts(Object.fromEntries(COST_FIELDS.filter(({ id }) => resolved[id] != null).map(({ id }) => [id, resolved[id]])));
    setDefaultCostsApplied(true);
  }, [data?.cost_assumptions.resolved_action_costs, defaultCostsApplied]);

  const toggleStrategy = (strategy: string) => setStrategies((current) => {
    if (!current.includes(strategy)) return [...current, strategy];
    if (current.length === 1) return current;
    return current.filter((item) => item !== strategy);
  });
  const reset = () => {
    setHorizon(50); setDiscountRate('2.5'); setConditionThreshold('4.5'); setMinimumServiceRating('3.0');
    setRehabilitationYear(inventoryYear + 5); setReplacementYear(inventoryYear + 15);
    setStrategies([...DEFAULT_STRATEGIES]);
    const resolved = data?.cost_assumptions.resolved_action_costs || {};
    setCosts(Object.fromEntries(COST_FIELDS.filter(({ id }) => resolved[id] != null).map(({ id }) => [id, resolved[id]])));
    setDefaultCostsApplied(Object.keys(resolved).length > 0);
    setError(null);
  };

  const active = data?.alternatives.find((item) => item.strategy_id === selectedAlternative);
  const activeChartRows = useMemo<ChartRow[]>(() => {
    if (!active) return [];
    let cumulativeCost = 0;
    return active.annual_ledger.map((entry) => {
      const ratings = entry.post_action_states.map((state) => state.expected_rating);
      const annualCost = Number(entry.present_value_cost || 0);
      cumulativeCost += annualCost;
      const componentRatings = Object.fromEntries(entry.post_action_states.map((state) => [state.component, state.expected_rating]));
      return {
        year: entry.year,
        condition: ratings.length ? Math.min(...ratings) : null,
        deck: componentRatings.deck ?? null,
        superstructure: componentRatings.superstructure ?? null,
        substructure: componentRatings.substructure ?? null,
        culvert: componentRatings.culvert ?? null,
        annualCost,
        cumulativeCost,
        actionLabel: entry.selected_action_ids.map(label).join(', '),
        hasAction: entry.selected_action_ids.length > 0,
      };
    });
  }, [active]);
  const actionRows = activeChartRows.filter((row) => row.hasAction);
  const comparisonChartRows = useMemo<ComparisonChartRow[]>(() => {
    if (!data) return [];
    const startYear = data.identity.data_year;
    const initialCondition = data.facts.controlling_rating ?? null;
    const ledgerByStrategy = new Map(
      data.alternatives.map((alternative) => [
        alternative.strategy_id,
        new Map(alternative.annual_ledger.map((row) => [row.year, row])),
      ]),
    );
    const cumulativeCosts: Record<string, number> = {};
    const lastConditions: Record<string, number | null> = {};
    data.alternatives.forEach((alternative) => {
      cumulativeCosts[alternative.strategy_id] = 0;
      lastConditions[alternative.strategy_id] = initialCondition;
    });
    return Array.from({ length: horizon + 1 }, (_, offset) => {
      const year = startYear + offset;
      const chartRow: ComparisonChartRow = { year };
      data.alternatives.forEach((alternative) => {
        const strategyId = alternative.strategy_id;
        const ledgerRow = ledgerByStrategy.get(strategyId)?.get(year);
        const ratings = ledgerRow?.post_action_states.map((state) => state.expected_rating) ?? [];
        if (ratings.length) lastConditions[strategyId] = Math.min(...ratings);
        const annualCost = Number(ledgerRow?.present_value_cost || 0);
        cumulativeCosts[strategyId] += annualCost;
        chartRow[chartKey(strategyId, 'condition')] = lastConditions[strategyId];
        chartRow[chartKey(strategyId, 'annualCost')] = annualCost;
        chartRow[chartKey(strategyId, 'cumulativeCost')] = cumulativeCosts[strategyId];
        chartRow[chartKey(strategyId, 'actionLabel')] = ledgerRow?.selected_action_ids.map(label).join(', ') || '';
      });
      return chartRow;
    });
  }, [data, horizon]);
  const chartTicks = useMemo(() => {
    const startYear = data?.identity.data_year ?? inventoryYear;
    const step = Math.max(1, Math.ceil(horizon / 10));
    const ticks = Array.from({ length: Math.floor(horizon / step) + 1 }, (_, index) => startYear + index * step);
    if (ticks[ticks.length - 1] !== startYear + horizon) ticks.push(startYear + horizon);
    return ticks;
  }, [data?.identity.data_year, horizon, inventoryYear]);
  const lowest = data?.alternatives.filter((item) => item.rankable).reduce<Alternative | null>((best, item) => !best || Number(item.total_present_value_cost) < Number(best.total_present_value_cost) ? item : best, null) || null;
  const scheduleYears = useMemo(() => Array.from({ length: horizon + 1 }, (_, index) => inventoryYear + index), [horizon, inventoryYear]);

  return (
    <Modal open onClose={onClose} inline={embedded} size="lg" title="Annual Forecast-Driven LCCA" icon={<Coins className="w-5 h-5" />}>
      <p className="text-sm text-slate-500 -mt-1 mb-4">{bridgeName || 'Bridge'} — {bridgeNumber}{yearBuilt ? ` · Built ${yearBuilt}` : ''}</p>
      <section className="rounded-2xl border border-slate-200 bg-white p-5 dark:border-slate-700 dark:bg-slate-900/40">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="font-semibold">Strategies</h3>
            <p className="text-xs text-slate-500">Select one or more paths to compare. Results update automatically.</p>
          </div>
          <div className="flex items-center gap-2 text-xs text-slate-500" aria-live="polite">
            {loading && <Loader2 className="h-4 w-4 animate-spin text-indigo-600" />}
            <span>{loading ? 'Updating simulation…' : data ? 'Simulation is up to date' : 'Preparing simulation…'}</span>
            <button onClick={reset} className="ml-1 rounded-lg border border-slate-200 p-2 hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800" title="Reset simulation settings" aria-label="Reset simulation settings"><RotateCcw className="h-4 w-4" /></button>
          </div>
        </div>
        <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {STRATEGIES.map(({ id, title }) => {
            const selected = strategies.includes(id);
            const details = strategyDetails(id, inventoryYear, conditionThreshold, rehabilitationYear, replacementYear);
            const fixedYear = id === 'deck_and_substructure_rehabilitation' ? rehabilitationYear : id === 'full_replacement' ? replacementYear : undefined;
            return (
              <div key={id} className={`rounded-2xl border p-3 transition ${selected ? 'border-indigo-400 bg-indigo-50/70 dark:border-indigo-700 dark:bg-indigo-950/30' : 'border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900/40'}`}>
                <button type="button" aria-pressed={selected} onClick={() => toggleStrategy(id)} className="flex w-full items-start gap-3 text-left">
                  <span className={`mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full text-xs font-bold ${selected ? 'bg-indigo-600 text-white' : 'bg-slate-100 text-transparent dark:bg-slate-800'}`}>✓</span>
                  <span><span className="block text-sm font-semibold text-slate-900 dark:text-white">{title}</span><span className="mt-1 block text-xs leading-5 text-slate-500">{details.description}</span></span>
                </button>
                <div className="mt-2 rounded-xl bg-white/80 px-3 py-2 dark:bg-slate-900/60">
                  <StrategyMiniTimeline id={id} inventoryYear={inventoryYear} horizon={horizon} actionYear={fixedYear} />
                  <p className="text-[11px] font-medium text-slate-600 dark:text-slate-300">{details.timing}</p>
                  {fixedYear != null && (
                    <label className="mt-2 flex items-center justify-between gap-2 text-xs text-slate-500">
                      <span>Action year</span>
                      <select value={fixedYear} onChange={(event) => id === 'deck_and_substructure_rehabilitation' ? setRehabilitationYear(Number(event.target.value)) : setReplacementYear(Number(event.target.value))} className="rounded-lg border border-slate-200 bg-white px-2 py-1.5 font-semibold text-slate-800 dark:border-slate-700 dark:bg-slate-900 dark:text-white">
                        {scheduleYears.map((year) => <option key={year} value={year}>{year}</option>)}
                      </select>
                    </label>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </section>

      <details className="mt-4 rounded-2xl border border-slate-200 bg-slate-50/60 dark:border-slate-700 dark:bg-slate-900/40">
        <summary className="cursor-pointer list-none px-5 py-4 font-semibold">
          Analysis assumptions
          <span className="ml-2 text-xs font-normal text-slate-500">{horizon} years · {discountRate}% discount · trigger {conditionThreshold} · floor {minimumServiceRating}</span>
        </summary>
        <div className="border-t border-slate-200 px-5 pb-5 pt-4 dark:border-slate-700">
          <p className="mb-4 text-xs text-slate-500">Changing any value recalculates the comparison.</p>
          <div className="grid gap-4 md:grid-cols-4">
            <NumberField label="Time horizon" value={String(horizon)} suffix="years" onChange={(value) => setHorizon(Math.max(1, Math.min(100, Number(value) || 1)))} />
            <NumberField label="Discount rate" value={discountRate} suffix="%" step="0.1" onChange={setDiscountRate} />
            <NumberField label="Rehabilitation trigger" value={conditionThreshold} suffix="rating" step="0.1" onChange={setConditionThreshold} />
            <NumberField label="Minimum service floor" value={minimumServiceRating} suffix="rating" step="0.1" onChange={setMinimumServiceRating} />
          </div>
        </div>
      </details>

      <details className="mt-4 rounded-2xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900/40">
        <summary className="cursor-pointer list-none px-5 py-4 font-semibold">Planning costs <span className="ml-2 text-xs font-normal text-slate-500">NJDOT defaults · editable</span></summary>
        <div className="border-t border-slate-200 px-5 pb-5 pt-4 dark:border-slate-700">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500">
            <p>Defaults use NJDOT TAMP Exhibit 5-6 rates × {data?.facts.deck_area ? `${Math.round(data.facts.deck_area).toLocaleString()} ft² deck area` : 'the bridge deck area'}. The $206/ft² value is treated as one generic preservation project every 10 years; listed cycle activities are included in that strategy and are not each charged $206/ft².</p>
            <a href={NJDOT_TAMP_SOURCE_URL} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 font-semibold text-indigo-600">View source <ExternalLink className="h-3 w-3" /></a>
          </div>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">{COST_FIELDS.map(({ id, title, rate }) => <NumberField key={id} label={title} value={costs[id] || ''} suffix="total/project" hint={`$${rate.toLocaleString()}/ft² source rate`} onChange={(value) => setCosts((current) => ({ ...current, [id]: value }))} />)}</div>
        </div>
      </details>

      {loading && !data && <div className="py-14 text-center text-slate-500"><Loader2 className="mx-auto mb-3 h-8 w-8 animate-spin" />Advancing every component one year at a time…</div>}
      {error && !loading && <div className="rounded-2xl bg-red-50 text-red-700 p-4 mt-5 text-sm">{error}</div>}

      {data && <div className="mt-5 space-y-5">
        {data.analysis.period_status !== 'complete' && <div className="rounded-2xl border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">This {horizon}-year view is shorter than the {data.analysis.minimum_comparable_years}-year comparison period. Conditions and costs are shown, but no lowest-cost alternative is identified.</div>}
        <section>
          <div className="mb-3"><h3 className="font-semibold">Results</h3><p className="text-xs text-slate-500">Every selected strategy is plotted below. Click a result card to inspect its action details.</p></div>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {data.alternatives.map((alternative) => <button key={alternative.strategy_id} onClick={() => setSelectedAlternative(alternative.strategy_id)} className={`text-left rounded-2xl border p-4 transition ${selectedAlternative === alternative.strategy_id ? 'border-slate-500 bg-slate-50 shadow-sm dark:border-slate-500 dark:bg-slate-800/70' : 'border-slate-200 hover:border-slate-300 dark:border-slate-700'}`}>
              <p className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-slate-600 dark:text-slate-300"><span className="h-3 w-3 rounded-full" style={{ background: STRATEGY_COLORS[alternative.strategy_id] }} />{strategyLabel(alternative.strategy_id)}</p>
              <p className="mt-1 text-2xl font-bold">{money(alternative.total_present_value_cost)}</p>
              <p className="text-xs text-slate-500">Net agency present value · {alternative.scheduled_actions.length} actions{alternative.scheduled_actions[0] ? ` · first in ${alternative.scheduled_actions[0].year}` : ''}</p>
              {Number(alternative.remaining_service_life_credit) > 0 && <p className="mt-1 text-[11px] text-slate-500">Gross {money(alternative.gross_present_value_cost)} − remaining-life credit {money(alternative.remaining_service_life_credit)}</p>}
              <p className={`mt-2 text-xs font-semibold ${alternative.rankable ? 'text-emerald-700' : alternative.feasibility_status === 'diagnostic' ? 'text-slate-600' : 'text-amber-700'}`}>{label(alternative.feasibility_status)}</p>
              {alternative.feasibility_reasons.slice(0, 1).map((reason) => <p key={reason} className="mt-1 text-[11px] leading-4 text-slate-500">{reason}</p>)}
              {lowest?.strategy_id === alternative.strategy_id && <p className="mt-2 text-xs font-semibold text-emerald-700">Lowest comparable agency cost</p>}
            </button>)}
          </div>
        </section>

        <section className="rounded-2xl border border-slate-200 bg-white p-5 dark:border-slate-700 dark:bg-slate-900/40">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div><h3 className="font-semibold">Condition, cost, and actions</h3><p className="text-xs text-slate-500">All {horizon} future years · every strategy selected above · hover any year for exact values.</p></div>
            <div className="flex flex-wrap gap-x-4 gap-y-2 text-[11px] text-slate-500">
              <span className="inline-flex items-center gap-1.5"><span className="w-6 border-t-2 border-slate-600" />Condition</span>
              <span className="inline-flex items-center gap-1.5"><span className="w-6 border-t-2 border-dashed border-slate-600" />Cumulative present value</span>
              <span className="inline-flex items-center gap-1.5"><span className="text-base leading-none">◆</span>Action year</span>
            </div>
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            {data.alternatives.map((alternative) => <span key={alternative.strategy_id} className="inline-flex items-center gap-2 rounded-full border border-slate-200 px-3 py-1.5 text-xs font-medium dark:border-slate-700"><span className="h-2.5 w-2.5 rounded-full" style={{ background: STRATEGY_COLORS[alternative.strategy_id] }} />{strategyLabel(alternative.strategy_id)}</span>)}
          </div>
          <div className="mt-2 h-[500px] w-full">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={comparisonChartRows} margin={{ top: 22, right: 28, left: 4, bottom: 8 }}>
                <ReferenceArea yAxisId="condition" y1={7} y2={9} fill="#22c55e" fillOpacity={0.055} />
                <ReferenceArea yAxisId="condition" y1={5} y2={7} fill="#eab308" fillOpacity={0.055} />
                <ReferenceArea yAxisId="condition" y1={0} y2={5} fill="#ef4444" fillOpacity={0.04} />
                <CartesianGrid strokeDasharray="3 4" stroke="rgba(148,163,184,0.28)" />
                <XAxis dataKey="year" type="number" domain={[data.identity.data_year, data.identity.data_year + horizon]} ticks={chartTicks} tick={{ fontSize: 11 }} />
                <YAxis yAxisId="condition" orientation="left" domain={[0, 9]} ticks={[0,1,2,3,4,5,6,7,8,9]} allowDecimals={false} tick={{ fontSize: 11 }} width={34} label={{ value: 'Condition rating', angle: -90, position: 'insideLeft', offset: 12, fontSize: 11, fill: '#64748b' }} />
                <YAxis yAxisId="money" orientation="right" domain={[0, 'auto']} tickFormatter={compactMoney} tick={{ fontSize: 11 }} width={58} label={{ value: 'Cumulative present value', angle: 90, position: 'insideRight', offset: 14, fontSize: 11, fill: '#64748b' }} />
                <Tooltip content={<ComparisonTooltip rows={comparisonChartRows} alternatives={data.alternatives} />} />
                <ReferenceLine yAxisId="condition" y={Number(conditionThreshold)} stroke="#dc2626" strokeDasharray="5 4" label={{ value: 'rehab trigger', position: 'insideTopRight', fill: '#dc2626', fontSize: 10 }} />
                <ReferenceLine yAxisId="condition" y={Number(minimumServiceRating)} stroke="#7f1d1d" strokeDasharray="2 4" label={{ value: 'service floor', position: 'insideBottomRight', fill: '#7f1d1d', fontSize: 10 }} />
                {data.alternatives.map((alternative) => {
                  const strategyId = alternative.strategy_id;
                  const color = STRATEGY_COLORS[strategyId] || '#475569';
                  const actionPoints = comparisonChartRows.filter((row) => Boolean(row[chartKey(strategyId, 'actionLabel')])).map((row) => ({ year: row.year, cumulativeCost: Number(row[chartKey(strategyId, 'cumulativeCost')] || 0) }));
                  return <React.Fragment key={strategyId}>
                    <Line yAxisId="condition" type="monotone" dataKey={chartKey(strategyId, 'condition')} stroke={color} strokeWidth={2.8} dot={false} connectNulls isAnimationActive={false} />
                    <Line yAxisId="money" type="monotone" dataKey={chartKey(strategyId, 'cumulativeCost')} stroke={color} strokeWidth={2.2} strokeDasharray="7 5" strokeOpacity={0.88} dot={false} connectNulls isAnimationActive={false} />
                    <Scatter yAxisId="money" data={actionPoints} dataKey="cumulativeCost" fill={color} shape="diamond" isAnimationActive={false} />
                  </React.Fragment>;
                })}
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </section>

        {active && <details className="rounded-2xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900/40">
          <summary className="cursor-pointer list-none px-5 py-4 font-semibold">
            Component condition detail · {strategyLabel(active.strategy_id)}
            <span className="ml-2 text-xs font-normal text-slate-500">Deck, superstructure, substructure, culvert, and controlling minimum</span>
          </summary>
          <div className="h-[344px] w-full border-t border-slate-200 px-5 pb-5 pt-4 dark:border-slate-700">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={activeChartRows} margin={{ top: 16, right: 18, left: 4, bottom: 8 }}>
                <ReferenceArea y1={7} y2={9} fill="#22c55e" fillOpacity={0.055} />
                <ReferenceArea y1={5} y2={7} fill="#eab308" fillOpacity={0.055} />
                <ReferenceArea y1={0} y2={5} fill="#ef4444" fillOpacity={0.04} />
                <CartesianGrid strokeDasharray="3 4" stroke="rgba(148,163,184,0.28)" />
                <XAxis dataKey="year" type="number" domain={[data.identity.data_year, data.identity.data_year + horizon]} ticks={chartTicks} tick={{ fontSize: 11 }} />
                <YAxis domain={[0, 9]} ticks={[0,1,2,3,4,5,6,7,8,9]} allowDecimals={false} tick={{ fontSize: 11 }} width={34} />
                <Tooltip formatter={(value: number, name: string) => [Number(value).toFixed(2), label(name)]} />
                <ReferenceLine y={Number(conditionThreshold)} stroke="#dc2626" strokeDasharray="5 4" />
                <ReferenceLine y={Number(minimumServiceRating)} stroke="#7f1d1d" strokeDasharray="2 4" />
                <Line type="monotone" dataKey="deck" name="deck" stroke="#2563eb" strokeWidth={2.2} dot={false} connectNulls />
                <Line type="monotone" dataKey="superstructure" name="superstructure" stroke="#7c3aed" strokeWidth={2.2} dot={false} connectNulls />
                <Line type="monotone" dataKey="substructure" name="substructure" stroke="#d97706" strokeWidth={2.2} dot={false} connectNulls />
                <Line type="monotone" dataKey="culvert" name="culvert" stroke="#0f766e" strokeWidth={2.2} dot={false} connectNulls />
                <Line type="monotone" dataKey="condition" name="controlling minimum" stroke="#0f172a" strokeWidth={3} strokeDasharray="6 4" dot={false} connectNulls />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </details>}

        {active && actionRows.length > 0 && <section className="rounded-2xl border border-slate-200 bg-slate-50/70 p-4 dark:border-slate-700 dark:bg-slate-900/40"><p className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-slate-700 dark:text-slate-200"><span className="h-2.5 w-2.5 rounded-full" style={{ background: STRATEGY_COLORS[active.strategy_id] }} />Action details · {strategyLabel(active.strategy_id)}</p><div className="mt-2 flex gap-2 overflow-x-auto pb-1">{actionRows.slice(0, 12).map((row) => <span key={row.year} className="shrink-0 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs text-slate-700 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200"><strong>{row.year}</strong> · {row.actionLabel}</span>)}{actionRows.length > 12 && <span className="shrink-0 px-2 py-1.5 text-xs text-slate-500">+{actionRows.length - 12} later action years</span>}</div></section>}

        {active && (active.applicability_issues.length > 0 || active.feasibility_reasons.length > 0) && <section className="rounded-2xl border border-amber-200 bg-amber-50/70 p-4 text-sm text-amber-950">
          <h3 className="font-semibold">Applicability and comparison notes</h3>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-xs">
            {active.feasibility_reasons.map((reason) => <li key={reason}>{reason}</li>)}
            {active.applicability_issues.map((issue) => <li key={`${issue.treatment_id}-${issue.status}`}>{label(issue.treatment_id)}: {label(issue.status)} ({issue.reasons.map(label).join(', ')})</li>)}
          </ul>
        </section>}

        {active && <details className="rounded-2xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900/40"><summary className="cursor-pointer list-none px-5 py-4 font-semibold">Sensitivity</summary><div className="border-t border-slate-200 p-5 dark:border-slate-700"><p className="mb-3 text-xs text-slate-500">Shows discount-rate, ±20% action-cost, and—when applicable—preservation-effect variations.</p><div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">{active.sensitivity.map((point) => <div key={`${point.dimension}-${point.value}`} className="rounded-xl bg-slate-50 p-3 text-sm dark:bg-slate-900"><p className="text-xs text-slate-500">{label(point.dimension)} · {point.dimension === 'discount_rate' || point.dimension === 'preservation_deterioration_reduction' ? `${(Number(point.value) * 100).toFixed(1)}%` : `${(Number(point.value) * 100).toFixed(0)}%`}</p>{point.ending_controlling_rating == null ? <p className="font-semibold">{money(point.total_present_value_cost)}</p> : <><p className="font-semibold">Ending condition {point.ending_controlling_rating.toFixed(2)}</p><p className="text-xs text-slate-500">Minimum {point.minimum_controlling_rating?.toFixed(2)} · cost {money(point.total_present_value_cost)}</p></>}</div>)}</div></div></details>}

        {active && <details className="rounded-2xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900/40"><summary className="cursor-pointer list-none px-5 py-4 font-semibold">Year-by-year actions and costs</summary><div className="border-t border-slate-200 p-5 dark:border-slate-700"><div className="max-h-[430px] overflow-auto rounded-xl border"><table className="min-w-full text-xs"><thead className="sticky top-0 bg-slate-50 dark:bg-slate-900"><tr><th className="text-left p-3">Year</th><th className="text-left p-3">Action</th><th className="text-left p-3">Pre-action condition</th><th className="text-left p-3">Post-action condition</th><th className="text-right p-3">Present value</th></tr></thead><tbody>{active.annual_ledger.map((row) => <tr key={row.year} className="border-t"><td className="p-3">{row.year}</td><td className="p-3">{row.selected_action_ids.map(label).join(', ') || 'None'}</td><td className="p-3">{row.pre_action_states.map((state) => `${label(state.component)} ${state.expected_rating.toFixed(2)}`).join(' · ')}</td><td className="p-3">{row.post_action_states.map((state) => `${label(state.component)} ${state.expected_rating.toFixed(2)}`).join(' · ')}</td><td className="p-3 text-right font-semibold">{money(row.present_value_cost)}</td></tr>)}</tbody></table></div></div></details>}

        <details className="rounded-2xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900/40">
          <summary className="cursor-pointer list-none px-5 py-4 font-semibold">History and model details</summary>
          <div className="space-y-5 border-t border-slate-200 p-5 dark:border-slate-700">
            <div className="grid gap-3 text-sm md:grid-cols-3">
              <Fact title="Inventory year" value={String(data.identity.data_year)} />
              <Fact title="Deck area" value={data.facts.deck_area ? data.facts.deck_area.toLocaleString() : 'Unavailable'} />
              <Fact title="NHS" value={data.facts.nhs == null ? 'Unknown' : data.facts.nhs ? 'Yes' : 'No'} />
              <Fact title="Cost scope" value="Agency costs only" />
              <Fact title="Cost basis" value={`${data.cost_assumptions.library_id} · ${data.cost_assumptions.version}`} />
              <Fact title="Preservation effect" value={`${(Number(data.analysis.effect_assumptions.preservation_deterioration_reduction) * 100).toFixed(0)}% deterioration reduction for the published cycle duration`} />
            </div>
            <div><h4 className="mb-3 font-semibold">Life since construction</h4>{data.life_history.unknown_history_interval && <p className="mb-3 text-xs text-amber-700">History unavailable from {data.life_history.unknown_history_interval[0]} through {data.life_history.unknown_history_interval[1]}.</p>}<div className="max-h-56 space-y-2 overflow-auto">{data.life_history.events.map((event) => <div key={event.event_id} className="flex gap-3 text-sm"><span className="font-mono text-slate-500">{event.event_year}</span><span><strong>{label(event.evidence_class)}</strong> · {label(event.component)}{event.treatment_id ? ` · ${label(event.treatment_id)}` : ''}{event.before_rating != null ? ` · ${event.before_rating}→${event.after_rating}` : ''}</span></div>)}</div></div>
          </div>
        </details>
      </div>}
    </Modal>
  );
};

const NumberField = ({ label: title, value, suffix, hint, step = '1', onChange }: { label: string; value: string; suffix: string; hint?: string; step?: string; onChange: (value: string) => void }) => <label className="block"><span className="text-xs text-slate-500">{title}</span><div className="mt-1 flex items-center gap-2"><input type="number" min="0" step={step} value={value} onChange={(event) => onChange(event.target.value)} className="w-full rounded-xl border bg-white px-3 py-2 dark:bg-slate-900" /><span className="whitespace-nowrap text-xs text-slate-400">{suffix}</span></div>{hint && <span className="mt-1 block text-[11px] text-slate-400">{hint}</span>}</label>;
const Fact = ({ title, value }: { title: string; value: string }) => <div className="rounded-xl bg-slate-50 dark:bg-slate-900 p-3"><p className="text-xs text-slate-500">{title}</p><p className="font-medium break-words">{value}</p></div>;

export default BridgeLCCAModal;
