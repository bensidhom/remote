import React, { useEffect, useMemo, useState } from 'react';
import axios from 'axios';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, PieChart, Pie, Cell, Legend,
} from 'recharts';
import { BarChart3, Download, Loader2 } from 'lucide-react';
import { SectionHeader } from '../components/ui';
import { useDataGenerationEpoch } from '../lib/dataGeneration';

const GOOD = '#15803d';
const UPPER_FAIR = '#d6a700';
const LOWER_FAIR = '#f2c94c';
const POOR = '#c24135';
const NJ_STATE_CODE = 34;

type Tab = 'inventory' | 'acceptable' | 'age-count' | 'fhwa' | 'scenario';

interface ConditionCounts { Good: number; Fair: number; Poor: number; Unknown: number }
interface CountyCondition extends ConditionCounts { county: string }
interface OperationalValues {
  total_bridges: number;
  condition_counts: ConditionCounts;
  component_condition_counts: Array<{
    component: string;
    field: string;
    Good: number;
    Fair: number;
    Poor: number;
    rated_bridges: number;
    not_applicable_or_unrated: number;
  }>;
  condition_percent_of_all: Record<string, number | null>;
  classifiable_bridges: number;
  average_age_at_snapshot: number | null;
  age_known_count: number;
  age_missing_or_invalid_count: number;
  older_than_years: number;
  older_bridge_count: number;
  total_valid_deck_area_ft2: number | null;
  deck_area_known_count: number;
  deck_area_missing_count: number;
  deck_area_method_counts: Record<string, number>;
  carried_aadt_single_candidate_count: number;
  carried_aadt_ambiguous_count: number;
  carried_aadt_missing_count: number;
  high_traffic_threshold: number;
  high_traffic_bridge_count: number;
  county_counts: Record<string, number>;
  county_condition_counts: CountyCondition[];
  state_maintained_policy: {
    field: string; allowed_codes: string[]; meaning: string; status: string;
  };
  state_maintained_20ft_acceptable: {
    all_state_maintained_count: number;
    eligible_count: number;
    classifiable_denominator: number;
    acceptable_numerator: number;
    unknown_condition_count: number;
    missing_or_below_length_count: number;
    percent_classifiable: number | null;
    percent_all_eligible: number | null;
    definition_version: string;
  };
  state_maintained_deck_area_acceptable: {
    valid_classifiable_area_denominator_ft2: number | null;
    acceptable_area_numerator_ft2: number | null;
    unknown_condition_area_ft2: number | null;
    missing_geometry_bridge_count: number;
    below_or_missing_nbis_length_count: number;
    percent_classifiable_area: number | null;
    percent_all_valid_area: number | null;
    definition_version: string;
  };
}
interface OperationalResponse {
  kind: string;
  state_code: number;
  year: number;
  data_generation: number;
  schema_generation: number;
  revision_set_sha256: string;
  definition_version: string;
  values: OperationalValues;
  warnings: string[];
  registry?: { definitions: Array<{ measure_id: string; title: string; unit: string }> };
}
interface AgeCountRow {
  age_bin: string;
  Good: number;
  'Upper Fair': number;
  'Lower Fair': number;
  Fair: number;
  Poor: number;
  total_bridges: number;
}
interface AgeCountResponse {
  year: number;
  population: string;
  data_generation: number;
  schema_generation: number;
  revision_set_sha256: string;
  definition_version: string;
  series: AgeCountRow[];
  excluded: Record<string, number>;
}
interface AgeAreaRow {
  age_bin: string;
  Good: number;
  'Upper Fair': number;
  'Lower Fair': number;
  Poor: number;
  total_ft2: number;
}
interface AgeAreaResponse {
  year: number;
  population: string;
  include_unknown: boolean;
  data_generation: number;
  schema_generation: number;
  revision_set_sha256: string;
  definition_version: string;
  series: AgeAreaRow[];
  excluded: Record<string, number>;
}
interface FhwaResponse {
  label: string; definition_version: string; data_generation: number; revision_set_sha256: string;
  profile: { area_rule: string; source_title: string; source_url: string };
  population: Record<string, number>; areas_ft2: Record<string, string>;
  bridge_counts: Record<string, number>; total_applicable_deck_area_ft2: string;
  good_deck_area_percent: string | null; fair_deck_area_percent: string | null; poor_deck_area_percent: string | null;
  area_method_counts: Record<string, number>; exclusions: Record<string, number>; warnings: string[];
}
interface FutureConditionRow {
  year: number;
  Good: number;
  'Upper Fair': number;
  'Lower Fair': number;
  Fair: number;
  Poor: number;
  total_bridges: number;
  poor_bridge_count: number;
}
interface FutureAgeConditionRow extends AgeCountRow {}
interface FutureAgeConditionYear {
  year: number;
  rated_with_known_age: number;
  age_bins: FutureAgeConditionRow[];
}
interface FutureConditionScenario {
  scenario_id: 'historical_trend' | 'no_rehabilitation';
  label: string;
  description: string;
  series: FutureConditionRow[];
  age_series: FutureAgeConditionYear[];
}
interface FutureConditionResponse {
  year: number;
  horizon_years: number;
  data_generation: number;
  source_transition_intervals: number;
  current_unrated_count: number;
  current_rated_missing_or_invalid_age_count: number;
  scenarios: FutureConditionScenario[];
}

const number = (value: number | null | undefined, digits = 0) =>
  value == null || Number.isNaN(Number(value))
    ? '—'
    : Number(value).toLocaleString(undefined, { maximumFractionDigits: digits });
const percent = (value: number | null | undefined) => value == null ? '—' : `${number(value, 1)}%`;
const area = (value: number | null | undefined) => value == null ? '—' : `${number(value / 1000, 1)}k ft²`;

const tabs: { id: Tab; label: string }[] = [
  { id: 'inventory', label: 'Operational inventory' },
  { id: 'acceptable', label: 'Acceptable condition' },
  { id: 'age-count', label: 'Age by condition' },
  { id: 'fhwa', label: 'NHS deck condition' },
  { id: 'scenario', label: 'Future scenarios' },
];

const Kpis: React.FC = () => {
  const [tab, setTab] = useState<Tab>('inventory');
  const [years, setYears] = useState<number[]>([]);
  const [year, setYear] = useState<number | null>(null);
  const [report, setReport] = useState<OperationalResponse | null>(null);
  const [ageReport, setAgeReport] = useState<AgeCountResponse | null>(null);
  const [ageAreaReport, setAgeAreaReport] = useState<AgeAreaResponse | null>(null);
  const [ageMetric, setAgeMetric] = useState<'count' | 'deck_area'>('count');
  const [ageAreaLoading, setAgeAreaLoading] = useState(false);
  const [ageAreaError, setAgeAreaError] = useState('');
  const [fhwaReport, setFhwaReport] = useState<FhwaResponse | null>(null);
  const [futureReport, setFutureReport] = useState<FutureConditionResponse | null>(null);
  const [futureScenarioId, setFutureScenarioId] = useState<FutureConditionScenario['scenario_id']>('historical_trend');
  const [futureLoading, setFutureLoading] = useState(false);
  const [futureError, setFutureError] = useState('');
  const [exporting, setExporting] = useState(false);
  const [agePopulation, setAgePopulation] = useState<'statewide' | 'state_maintained'>('statewide');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const dataGenerationEpoch = useDataGenerationEpoch();

  useEffect(() => {
    const controller = new AbortController();
    axios.get<{ years: number[] }>('/api/analytics/kpis/years', {
      params: { state_code: NJ_STATE_CODE }, signal: controller.signal,
    }).then((response) => {
      const next = response.data.years || [];
      setYears(next);
      setYear((current) => current != null && next.includes(current) ? current : (next[0] ?? null));
    }).catch(() => {
      if (!controller.signal.aborted) { setYears([]); setYear(null); }
    });
    return () => controller.abort();
  }, [dataGenerationEpoch]);

  useEffect(() => {
    if (year == null) { setLoading(false); return; }
    const controller = new AbortController();
    setLoading(true);
    setError('');
    axios.get<OperationalResponse>('/api/analytics/kpis/operational', {
      params: { state_code: NJ_STATE_CODE, year }, signal: controller.signal,
    }).then((operational) => {
      const headerGeneration = Number(operational.headers['x-bridgeguard-data-version']);
      if (headerGeneration !== operational.data.data_generation) {
        throw new Error('KPI data changed while the dashboard was loading. Refresh and try again.');
      }
      setReport(operational.data);
    }).catch((reason) => {
      if (!controller.signal.aborted) {
        setReport(null);
        setError(reason instanceof Error ? reason.message : 'Inventory KPI data could not be loaded.');
      }
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [year, dataGenerationEpoch]);

  useEffect(() => {
    if (year == null) return;
    const controller = new AbortController();
    axios.get<AgeCountResponse>('/api/analytics/kpis/age-condition-counts', {
      params: { state_code: NJ_STATE_CODE, year, population: agePopulation },
      signal: controller.signal,
    }).then((ages) => setAgeReport(ages.data))
      .catch(() => { if (!controller.signal.aborted) setAgeReport(null); });
    return () => controller.abort();
  }, [year, agePopulation, dataGenerationEpoch]);

  useEffect(() => {
    if (year == null) return;
    const controller = new AbortController();
    setAgeAreaLoading(true);
    setAgeAreaError('');
    axios.get<AgeAreaResponse>('/api/analytics/kpis/age-condition-area', {
      params: { state_code: NJ_STATE_CODE, year, population: agePopulation, include_unknown: false },
      signal: controller.signal,
    }).then((ages) => setAgeAreaReport(ages.data))
      .catch(() => {
        if (!controller.signal.aborted) {
          setAgeAreaReport(null);
          setAgeAreaError('Deck-area-by-age data could not be loaded.');
        }
      })
      .finally(() => { if (!controller.signal.aborted) setAgeAreaLoading(false); });
    return () => controller.abort();
  }, [year, agePopulation, dataGenerationEpoch]);

  useEffect(() => {
    if (year == null) return;
    const controller = new AbortController();
    axios.get<FhwaResponse>('/api/analytics/kpis/fhwa-tpm', {
      params: { state_code: NJ_STATE_CODE, year }, signal: controller.signal,
    }).then((response) => setFhwaReport(response.data))
      .catch(() => { if (!controller.signal.aborted) setFhwaReport(null); });
    return () => controller.abort();
  }, [year, dataGenerationEpoch]);

  useEffect(() => {
    if (year == null || tab !== 'scenario') return;
    const controller = new AbortController();
    setFutureLoading(true);
    setFutureError('');
    axios.get<FutureConditionResponse>('/api/analytics/kpis/future-condition', {
      params: { state_code: NJ_STATE_CODE, year, horizon_years: 10 },
      signal: controller.signal,
    }).then((response) => setFutureReport(response.data))
      .catch((reason) => {
        if (!controller.signal.aborted) {
          setFutureReport(null);
          setFutureError(reason?.response?.data?.detail?.message || 'Future condition projection could not be calculated.');
        }
      })
      .finally(() => { if (!controller.signal.aborted) setFutureLoading(false); });
    return () => controller.abort();
  }, [year, tab, dataGenerationEpoch]);

  const donut = useMemo(() => report ? [
    { name: 'Good', value: report.values.condition_counts.Good, fill: GOOD },
    { name: 'Fair', value: report.values.condition_counts.Fair, fill: UPPER_FAIR },
    { name: 'Poor', value: report.values.condition_counts.Poor, fill: POOR },
  ].filter((item) => item.value > 0) : [], [report]);

  const selectedFutureScenario = useMemo(
    () => futureReport?.scenarios.find((item) => item.scenario_id === futureScenarioId) || futureReport?.scenarios[0] || null,
    [futureReport, futureScenarioId],
  );

  const exportPackage = async () => {
    if (year == null) return;
    const exportKinds: Partial<Record<Tab, string>> = {
      inventory: 'operational', acceptable: 'acceptable_condition', 'age-count': ageMetric === 'deck_area' ? 'age_condition_area' : 'age_condition_counts', fhwa: 'fhwa_tpm', scenario: 'future_condition',
    };
    const exportKind = exportKinds[tab];
    if (!exportKind) return;
    setExporting(true);
    try {
      const response = await axios.post('/api/analytics/kpis/exports', {
        export_kind: exportKind, state_code: NJ_STATE_CODE, year,
        population: agePopulation, include_unknown: false, horizon_years: 10,
      }, { responseType: 'blob' });
      const disposition = String(response.headers['content-disposition'] || '');
      const match = disposition.match(/filename="([^"]+)"/);
      const url = URL.createObjectURL(response.data);
      const anchor = document.createElement('a'); anchor.href = url;
      anchor.download = match?.[1] || `bridgeguard-${exportKind}-${year}.zip`;
      document.body.appendChild(anchor); anchor.click(); anchor.remove(); URL.revokeObjectURL(url);
    } finally { setExporting(false); }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <SectionHeader
          icon={<BarChart3 className="h-6 w-6" />}
          title="Bridge performance"
          subtitle="Counts and condition measures calculated from the selected SNBI inventory year."
        />
        <div className="flex items-center gap-2">
          <select
            value={year ?? ''}
            onChange={(event) => setYear(event.target.value ? Number(event.target.value) : null)}
            className="rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm font-semibold text-slate-800"
            aria-label="Inventory year"
          >
            {years.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
          <button type="button" onClick={() => void exportPackage()} disabled={year == null || exporting} className="inline-flex items-center gap-2 rounded-xl bg-slate-900 px-4 py-2 text-sm font-semibold text-white disabled:opacity-40">
            {exporting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />} Export package
          </button>
        </div>
      </div>

      <div className="overflow-x-auto border-b border-slate-200">
        <div className="flex min-w-max gap-1" role="tablist" aria-label="KPI views">
          {tabs.map((item) => (
            <button
              key={item.id}
              type="button"
              role="tab"
              aria-selected={tab === item.id}
              onClick={() => setTab(item.id)}
              className={`border-b-2 px-4 py-3 text-sm font-semibold transition ${tab === item.id ? 'border-brand-600 text-brand-700' : 'border-transparent text-slate-500 hover:text-slate-800'}`}
            >{item.label}</button>
          ))}
        </div>
      </div>

      {loading ? (
        <div className="py-24 text-center text-slate-500"><Loader2 className="mx-auto mb-3 h-8 w-8 animate-spin text-brand-600" />Loading exact inventory KPIs…</div>
      ) : error ? (
        <div className="rounded-2xl border border-amber-300 bg-amber-50 p-6 text-amber-900">
          <p className="font-semibold">Inventory KPI population is unavailable</p><p className="mt-1 text-sm">{error}</p>
        </div>
      ) : !report ? (
        <div className="py-24 text-center text-slate-500">Load bridge inventory rows before calculating observed KPIs.</div>
      ) : (
        <>
          {tab === 'inventory' && (
            <div className="space-y-6">
              <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
                {[
                  ['Bridges', number(report.values.total_bridges), `Inventory year ${report.year}`],
                  ['Average bridge age', `${number(report.values.average_age_at_snapshot, 1)} yr`, 'Based on SNBI year built'],
                  ['Deck area', area(report.values.total_valid_deck_area_ft2), 'Reported or calculated from SNBI dimensions'],
                  ['High-traffic bridges', number(report.values.high_traffic_bridge_count), `Carried AADT ≥ ${number(report.values.high_traffic_threshold)}`],
                ].map(([label, value, note]) => (
                  <div key={label} className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
                    <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">{label}</p>
                    <p className="mt-2 text-3xl font-bold text-slate-950">{value}</p>
                    <p className="mt-2 text-xs text-slate-500">{note}</p>
                  </div>
                ))}
              </div>
              <div className="grid gap-6 xl:grid-cols-[340px_1fr]">
                <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
                  <h2 className="font-semibold text-slate-900">Overall bridge condition</h2>
                  <p className="mt-1 text-xs text-slate-500">Lowest applicable rating among Deck, Superstructure, Substructure, and Culvert.</p>
                  <ResponsiveContainer width="100%" height={260}>
                    <PieChart><Pie data={donut} dataKey="value" nameKey="name" innerRadius={58} outerRadius={92} paddingAngle={2}>{donut.map((item) => <Cell key={item.name} fill={item.fill} />)}</Pie><Tooltip formatter={(value) => number(Number(value))} /><Legend /></PieChart>
                  </ResponsiveContainer>
                </section>
                <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
                  <h2 className="font-semibold text-slate-900">County inventory by condition</h2>
                  <ResponsiveContainer width="100%" height={Math.max(320, report.values.county_condition_counts.length * 25)}>
                    <BarChart data={report.values.county_condition_counts} layout="vertical" margin={{ left: 28, right: 12 }}>
                      <CartesianGrid strokeDasharray="3 3" horizontal={false} />
                      <XAxis type="number" allowDecimals={false} /><YAxis type="category" dataKey="county" width={100} tick={{ fontSize: 11 }} />
                      <Tooltip formatter={(value) => number(Number(value))} /><Legend />
                      <Bar dataKey="Good" stackId="condition" fill={GOOD} />
                      <Bar dataKey="Fair" stackId="condition" fill={UPPER_FAIR} />
                      <Bar dataKey="Poor" stackId="condition" fill={POOR} />
                    </BarChart>
                  </ResponsiveContainer>
                </section>
              </div>
              <section className="space-y-4">
                <div><h2 className="font-semibold text-slate-900">Condition by component</h2><p className="mt-1 text-xs text-slate-500">Each plot includes only bridges where that SNBI component rating applies.</p></div>
                <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
                  {report.values.component_condition_counts.map((component) => {
                    const data = [
                      { name: 'Good', value: component.Good, fill: GOOD },
                      { name: 'Fair', value: component.Fair, fill: UPPER_FAIR },
                      { name: 'Poor', value: component.Poor, fill: POOR },
                    ].filter((item) => item.value > 0);
                    return <article key={component.component} className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
                      <h3 className="font-semibold text-slate-900">{component.component}</h3>
                      <p className="text-xs text-slate-500">{component.field} · {number(component.rated_bridges)} bridges</p>
                      <ResponsiveContainer width="100%" height={210}>
                        <PieChart><Pie data={data} dataKey="value" nameKey="name" innerRadius={42} outerRadius={70}>{data.map((item) => <Cell key={item.name} fill={item.fill} />)}</Pie><Tooltip formatter={(value) => number(Number(value))} /><Legend /></PieChart>
                      </ResponsiveContainer>
                    </article>;
                  })}
                </div>
              </section>
            </div>
          )}

          {tab === 'acceptable' && (
            <div className="space-y-5">
              <p className="text-sm text-slate-600">State-maintained bridges (B.CL.02 = S01), using the controlling SNBI condition rating.</p>
              <div className="grid gap-5 lg:grid-cols-2">
                {[
                  {
                    title: 'Bridges ≥20 ft in acceptable condition',
                    value: percent(report.values.state_maintained_20ft_acceptable.percent_classifiable),
                    numerator: number(report.values.state_maintained_20ft_acceptable.acceptable_numerator),
                    denominator: number(report.values.state_maintained_20ft_acceptable.classifiable_denominator),
                  },
                  {
                    title: 'Deck area in acceptable condition',
                    value: percent(report.values.state_maintained_deck_area_acceptable.percent_classifiable_area),
                    numerator: area(report.values.state_maintained_deck_area_acceptable.acceptable_area_numerator_ft2),
                    denominator: area(report.values.state_maintained_deck_area_acceptable.valid_classifiable_area_denominator_ft2),
                  },
                ].map((item) => (
                  <article key={item.title} className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
                    <p className="text-sm font-semibold text-slate-700">{item.title}</p>
                    <p className="mt-3 text-5xl font-bold tracking-tight text-slate-950">{item.value}</p>
                    <dl className="mt-6 grid grid-cols-2 gap-3 text-sm">
                      <div className="rounded-xl bg-emerald-50 p-3"><dt className="text-emerald-800">Numerator</dt><dd className="mt-1 font-bold text-emerald-950">{item.numerator}</dd></div>
                      <div className="rounded-xl bg-slate-100 p-3"><dt className="text-slate-600">Denominator</dt><dd className="mt-1 font-bold text-slate-950">{item.denominator}</dd></div>
                    </dl>
                  </article>
                ))}
              </div>
            </div>
          )}

          {tab === 'age-count' && (
            <div className="space-y-5">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <select value={agePopulation} onChange={(event) => setAgePopulation(event.target.value as typeof agePopulation)} className="rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm">
                  <option value="statewide">Statewide loaded inventory</option>
                  <option value="state_maintained">State-maintained (B.CL.02 = S01)</option>
                </select>
                <div className="inline-flex rounded-xl border border-slate-300 bg-white p-1" role="group" aria-label="Age chart measure">
                  <button type="button" aria-pressed={ageMetric === 'count'} onClick={() => setAgeMetric('count')} className={`rounded-lg px-3 py-1.5 text-sm font-semibold ${ageMetric === 'count' ? 'bg-brand-600 text-white' : 'text-slate-600 hover:text-slate-900'}`}>Bridge count</button>
                  <button type="button" aria-pressed={ageMetric === 'deck_area'} onClick={() => setAgeMetric('deck_area')} className={`rounded-lg px-3 py-1.5 text-sm font-semibold ${ageMetric === 'deck_area' ? 'bg-brand-600 text-white' : 'text-slate-600 hover:text-slate-900'}`}>Deck area (ft²)</button>
                </div>
              </div>
              {ageMetric === 'deck_area' && ageAreaLoading ? (
                <div className="py-20 text-center text-slate-500"><Loader2 className="mx-auto mb-3 h-7 w-7 animate-spin text-brand-600" />Loading deck area by age…</div>
              ) : ageMetric === 'deck_area' && ageAreaError ? (
                <div className="rounded-2xl border border-amber-300 bg-amber-50 p-6 text-amber-900">{ageAreaError}</div>
              ) : ageMetric === 'count' && !ageReport ? (
                <div className="py-20 text-center text-slate-500">Bridge counts by age are not available for this inventory year.</div>
              ) : (
                <>
                  <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
                    <h2 className="font-semibold text-slate-900">{ageMetric === 'count' ? 'Bridge count' : 'Deck area'} by age and condition</h2>
                    <p className="mt-1 text-xs text-slate-500">Age is inventory year {(ageMetric === 'count' ? ageReport?.year : ageAreaReport?.year) ?? year} minus the SNBI year built. {ageMetric === 'deck_area' ? 'Each bar sums bridge deck area in square feet. ' : ''}The official Fair class is split into Upper Fair (rating 6) and Lower Fair (rating 5).</p>
                    <ResponsiveContainer width="100%" height={430}>
                      <BarChart data={ageMetric === 'count' ? ageReport?.series : ageAreaReport?.series} margin={{ left: 8, right: 12, top: 24 }}>
                        <CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="age_bin" angle={-45} textAnchor="end" height={70} /><YAxis allowDecimals={ageMetric === 'deck_area'} tickFormatter={ageMetric === 'deck_area' ? (value) => number(Number(value) / 1000) : undefined} label={{ value: ageMetric === 'count' ? 'Number of bridges' : 'Deck area (thousand ft²)', angle: -90, position: 'insideLeft' }} /><Tooltip formatter={(value) => ageMetric === 'count' ? number(Number(value)) : `${number(Number(value), 1)} ft²`} /><Legend />
                        <Bar dataKey="Good" stackId="age-condition" fill={GOOD} />
                        <Bar dataKey="Upper Fair" stackId="age-condition" fill={UPPER_FAIR} />
                        <Bar dataKey="Lower Fair" stackId="age-condition" fill={LOWER_FAIR} />
                        <Bar dataKey="Poor" stackId="age-condition" fill={POOR} />
                      </BarChart>
                    </ResponsiveContainer>
                  </section>
                  {ageMetric === 'count' && (ageReport?.excluded.missing_or_invalid_age_count || 0) > 0 && <p className="text-xs text-slate-500">{number(ageReport?.excluded.missing_or_invalid_age_count)} records without a usable year built are not included in this age chart.</p>}
                  {ageMetric === 'deck_area' && ageAreaReport && (
                    <div className="space-y-1 text-xs text-slate-500">
                      {ageAreaReport.excluded.missing_or_invalid_age_count > 0 && <p>{number(ageAreaReport.excluded.missing_or_invalid_age_count)} records without a usable year built are not included in this age chart.</p>}
                      {ageAreaReport.excluded.missing_or_invalid_geometry_count > 0 && <p>{number(ageAreaReport.excluded.missing_or_invalid_geometry_count)} records without usable deck geometry are not included.</p>}
                      {ageAreaReport.excluded.unknown_condition_area_ft2 > 0 && <p>{area(ageAreaReport.excluded.unknown_condition_area_ft2)} with an unknown condition is not included in the condition bars.</p>}
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          {tab === 'fhwa' && (
            !fhwaReport ? <div className="py-20 text-center text-slate-500">NHS condition values are not available for this inventory year.</div> :
            <div className="space-y-5">
              <p className="text-sm text-slate-600">NHS deck area condition calculated directly from the selected SNBI inventory.</p>
              <div className="grid gap-4 sm:grid-cols-3">
                {[['Good deck area', fhwaReport.good_deck_area_percent, GOOD], ['Fair deck area', fhwaReport.fair_deck_area_percent, UPPER_FAIR], ['Poor deck area', fhwaReport.poor_deck_area_percent, POOR]].map(([label, value, color]) => <div key={label} className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm" style={{ borderTopColor: String(color), borderTopWidth: 4 }}><p className="text-sm font-semibold text-slate-700">{label}</p><p className="mt-2 text-4xl font-bold text-slate-950">{value == null ? '—' : `${value}%`}</p></div>)}
              </div>
            </div>
          )}
          {tab === 'scenario' && (
            futureLoading ? <div className="py-24 text-center text-slate-500"><Loader2 className="mx-auto mb-3 h-8 w-8 animate-spin text-brand-600" />Calculating future conditions from yearly SNBI history…</div> :
            futureError ? <div className="rounded-2xl border border-amber-300 bg-amber-50 p-6 text-amber-900">{futureError}</div> :
            selectedFutureScenario ? (
              <div className="space-y-5">
                <div className="flex flex-wrap gap-2">
                  {futureReport?.scenarios.map((item) => (
                    <button key={item.scenario_id} type="button" onClick={() => setFutureScenarioId(item.scenario_id)} className={`rounded-xl px-4 py-2 text-sm font-semibold ${selectedFutureScenario.scenario_id === item.scenario_id ? 'bg-brand-600 text-white' : 'border border-slate-300 bg-white text-slate-700'}`}>{item.label}</button>
                  ))}
                </div>
                <p className="text-sm text-slate-600">{selectedFutureScenario.description} Calculated automatically from yearly SNBI condition changes through {futureReport?.year}.</p>
                <div className="grid gap-4 sm:grid-cols-2">
                  <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm"><p className="text-sm font-semibold text-slate-700">Poor bridges now</p><p className="mt-2 text-4xl font-bold text-slate-950">{number(Math.round(selectedFutureScenario.series[0]?.Poor || 0))}</p></div>
                  <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm"><p className="text-sm font-semibold text-slate-700">Projected poor bridges in {selectedFutureScenario.series[selectedFutureScenario.series.length - 1]?.year}</p><p className="mt-2 text-4xl font-bold text-slate-950">{number(Math.round(selectedFutureScenario.series[selectedFutureScenario.series.length - 1]?.Poor || 0))}</p></div>
                </div>
                <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
                  <h2 className="font-semibold text-slate-900">Projected bridge condition</h2>
                  <ResponsiveContainer width="100%" height={420}>
                    <BarChart data={selectedFutureScenario.series.map((row) => ({ ...row, Good: Math.round(row.Good), 'Upper Fair': Math.round(row['Upper Fair']), 'Lower Fair': Math.round(row['Lower Fair']), Poor: Math.round(row.Poor) }))} margin={{ left: 8, right: 12, top: 24 }}>
                      <CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="year" /><YAxis allowDecimals={false} label={{ value: 'Number of bridges', angle: -90, position: 'insideLeft' }} /><Tooltip formatter={(value) => number(Number(value))} /><Legend />
                      <Bar dataKey="Good" stackId="condition" fill={GOOD} />
                      <Bar dataKey="Upper Fair" stackId="condition" fill={UPPER_FAIR} />
                      <Bar dataKey="Lower Fair" stackId="condition" fill={LOWER_FAIR} />
                      <Bar dataKey="Poor" stackId="condition" fill={POOR} />
                    </BarChart>
                  </ResponsiveContainer>
                </section>
                <section className="space-y-4">
                  <div>
                    <h2 className="font-semibold text-slate-900">Age decomposition by scenario</h2>
                    <p className="mt-1 text-xs text-slate-500">The same bridges age forward with the selected scenario. Fair is shown as rating 6 and rating 5 so deterioration within the Fair class remains visible.</p>
                  </div>
                  <div className="grid gap-5 xl:grid-cols-2">
                    {[selectedFutureScenario.age_series[0], selectedFutureScenario.age_series[selectedFutureScenario.age_series.length - 1]].filter(Boolean).map((ageYear, index) => (
                      <article key={`${selectedFutureScenario.scenario_id}-${ageYear.year}`} className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
                        <h3 className="font-semibold text-slate-900">{index === 0 ? 'Current' : 'Projected'} age decomposition — {ageYear.year}</h3>
                        <p className="mt-1 text-xs text-slate-500">{number(Math.round(ageYear.rated_with_known_age))} rated bridges with a usable year built.</p>
                        <ResponsiveContainer width="100%" height={360}>
                          <BarChart data={ageYear.age_bins.map((row) => ({ ...row, Good: Math.round(row.Good), 'Upper Fair': Math.round(row['Upper Fair']), 'Lower Fair': Math.round(row['Lower Fair']), Poor: Math.round(row.Poor) }))} margin={{ left: 4, right: 8, top: 20 }}>
                            <CartesianGrid strokeDasharray="3 3" />
                            <XAxis dataKey="age_bin" angle={-45} textAnchor="end" height={68} tick={{ fontSize: 10 }} />
                            <YAxis allowDecimals={false} label={{ value: 'Number of bridges', angle: -90, position: 'insideLeft' }} />
                            <Tooltip formatter={(value) => number(Number(value))} /><Legend />
                            <Bar dataKey="Good" stackId="age-condition" fill={GOOD} />
                            <Bar dataKey="Upper Fair" stackId="age-condition" fill={UPPER_FAIR} />
                            <Bar dataKey="Lower Fair" stackId="age-condition" fill={LOWER_FAIR} />
                            <Bar dataKey="Poor" stackId="age-condition" fill={POOR} />
                          </BarChart>
                        </ResponsiveContainer>
                      </article>
                    ))}
                  </div>
                  {(futureReport?.current_rated_missing_or_invalid_age_count || 0) > 0 && <p className="text-xs text-slate-500">{number(futureReport?.current_rated_missing_or_invalid_age_count)} rated bridges without a usable year built remain in scenario totals but are excluded from the age plots.</p>}
                </section>
              </div>
            ) : <div className="py-20 text-center text-slate-500">Future condition data is not available.</div>
          )}
        </>
      )}
    </div>
  );
};

export default Kpis;
