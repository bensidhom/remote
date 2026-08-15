import React, { useEffect, useMemo, useState } from 'react';
import axios from 'axios';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, PieChart, Pie, Cell, Legend,
} from 'recharts';
import { BarChart3, Database, Download, FileCheck2, Loader2, ShieldAlert, Upload } from 'lucide-react';
import { SectionHeader } from '../components/ui';
import { useDataGenerationEpoch } from '../lib/dataGeneration';

const GOOD = '#15803d';
const UPPER_FAIR = '#d6a700';
const LOWER_FAIR = '#f2c94c';
const POOR = '#c24135';
const UNKNOWN = '#94a3b8';
const NJ_STATE_CODE = 34;

type Tab = 'inventory' | 'acceptable' | 'age-area' | 'program' | 'fhwa' | 'scenario';

interface ConditionCounts { Good: number; Fair: number; Poor: number; Unknown: number }
interface CountyCondition extends ConditionCounts { county: string }
interface OperationalValues {
  total_bridges: number;
  condition_counts: ConditionCounts;
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
interface AgeAreaRow {
  age_bin: string;
  Good: number;
  'Upper Fair': number;
  'Lower Fair': number;
  Poor: number;
  Unknown?: number;
  total_ft2: number;
}
interface AgeAreaResponse {
  year: number;
  population: string;
  data_generation: number;
  schema_generation: number;
  revision_set_sha256: string;
  definition_version: string;
  series: AgeAreaRow[];
  excluded: Record<string, number>;
}
interface ProgramValue {
  measure_id: string;
  definition_version: string;
  period_year: number;
  value_kind: string;
  value_decimal: string;
  unit: string;
  organization: string;
  import_sha256: string;
  reviewed_at: string;
}
interface ProgramResponse { data_generation: number; schema_generation: number; definition_version: string; values: ProgramValue[] }
interface FhwaResponse {
  label: string; definition_version: string; data_generation: number; revision_set_sha256: string;
  profile: { area_rule: string; source_title: string; source_url: string };
  population: Record<string, number>; areas_ft2: Record<string, string>;
  bridge_counts: Record<string, number>; total_applicable_deck_area_ft2: string;
  good_deck_area_percent: string | null; fair_deck_area_percent: string | null; poor_deck_area_percent: string | null;
  area_method_counts: Record<string, number>; exclusions: Record<string, number>; warnings: string[];
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
  { id: 'age-area', label: 'Deck area by age' },
  { id: 'program', label: 'Program delivery' },
  { id: 'fhwa', label: 'FHWA TPM diagnostic' },
  { id: 'scenario', label: 'Future scenarios' },
];

const DefinitionStrip: React.FC<{ report: OperationalResponse }> = ({ report }) => (
  <div className="grid gap-3 rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 text-xs text-slate-600 md:grid-cols-4">
    <div><span className="block font-semibold text-slate-900">Observed year</span>{report.year}</div>
    <div><span className="block font-semibold text-slate-900">Published revisions</span>{report.revision_set_sha256.slice(0, 12)}…</div>
    <div><span className="block font-semibold text-slate-900">Metric definition</span>{report.definition_version}</div>
    <div><span className="block font-semibold text-slate-900">Data generation</span>{report.data_generation}</div>
  </div>
);

const Kpis: React.FC = () => {
  const [tab, setTab] = useState<Tab>('inventory');
  const [years, setYears] = useState<number[]>([]);
  const [year, setYear] = useState<number | null>(null);
  const [report, setReport] = useState<OperationalResponse | null>(null);
  const [ageReport, setAgeReport] = useState<AgeAreaResponse | null>(null);
  const [programReport, setProgramReport] = useState<ProgramResponse | null>(null);
  const [fhwaReport, setFhwaReport] = useState<FhwaResponse | null>(null);
  const [programRefresh, setProgramRefresh] = useState(0);
  const [pendingProgramSource, setPendingProgramSource] = useState('');
  const [reviewNote, setReviewNote] = useState('');
  const [programMessage, setProgramMessage] = useState('');
  const [programBusy, setProgramBusy] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [agePopulation, setAgePopulation] = useState<'statewide' | 'state_maintained'>('statewide');
  const [includeUnknown, setIncludeUnknown] = useState(true);
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
        setError(reason instanceof Error ? reason.message : 'Published KPI data could not be loaded.');
      }
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [year, dataGenerationEpoch]);

  useEffect(() => {
    if (year == null) return;
    const controller = new AbortController();
    axios.get<AgeAreaResponse>('/api/analytics/kpis/age-condition-area', {
      params: { state_code: NJ_STATE_CODE, year, population: agePopulation, include_unknown: includeUnknown },
      signal: controller.signal,
    }).then((ages) => setAgeReport(ages.data))
      .catch(() => { if (!controller.signal.aborted) setAgeReport(null); });
    return () => controller.abort();
  }, [year, agePopulation, includeUnknown, dataGenerationEpoch]);

  useEffect(() => {
    if (year == null) return;
    const controller = new AbortController();
    axios.get<ProgramResponse>('/api/analytics/kpis/program', { signal: controller.signal })
      .then((response) => setProgramReport(response.data))
      .catch(() => { if (!controller.signal.aborted) setProgramReport(null); });
    axios.get<FhwaResponse>('/api/analytics/kpis/fhwa-tpm', {
      params: { state_code: NJ_STATE_CODE, year }, signal: controller.signal,
    }).then((response) => setFhwaReport(response.data))
      .catch(() => { if (!controller.signal.aborted) setFhwaReport(null); });
    return () => controller.abort();
  }, [year, dataGenerationEpoch, programRefresh]);

  const donut = useMemo(() => report ? [
    { name: 'Good', value: report.values.condition_counts.Good, fill: GOOD },
    { name: 'Fair', value: report.values.condition_counts.Fair, fill: UPPER_FAIR },
    { name: 'Poor', value: report.values.condition_counts.Poor, fill: POOR },
    { name: 'Unknown', value: report.values.condition_counts.Unknown, fill: UNKNOWN },
  ].filter((item) => item.value > 0) : [], [report]);

  const definitionTitles = useMemo(() => new Map(
    (report?.registry?.definitions || []).map((item) => [item.measure_id, item.title]),
  ), [report]);

  const importProgramFile = async (file: File | null) => {
    if (!file) return;
    setProgramBusy(true); setProgramMessage('');
    try {
      const body = new FormData(); body.append('file', file);
      const response = await axios.post('/api/analytics/kpis/program/import', body);
      setPendingProgramSource(String(response.data.source_id || ''));
      setProgramMessage(`Imported ${response.data.value_count || 0} values. A different authenticated reviewer must approve this source.`);
    } catch (reason: any) {
      setProgramMessage(reason?.response?.data?.detail?.message || 'Program KPI import failed validation.');
    } finally { setProgramBusy(false); }
  };

  const approveProgramSource = async () => {
    if (!pendingProgramSource || !reviewNote.trim()) return;
    setProgramBusy(true); setProgramMessage('');
    try {
      await axios.post(
        `/api/analytics/kpis/program/sources/${encodeURIComponent(pendingProgramSource)}/approve`,
        { review_note: reviewNote.trim() },
      );
      setProgramMessage('Program source approved and published to the fiscal-year table.');
      setPendingProgramSource(''); setReviewNote(''); setProgramRefresh((value) => value + 1);
    } catch (reason: any) {
      setProgramMessage(reason?.response?.data?.detail?.message || 'Program KPI approval failed.');
    } finally { setProgramBusy(false); }
  };

  const exportPackage = async () => {
    if (year == null || tab === 'scenario') return;
    const exportKinds: Partial<Record<Tab, string>> = {
      inventory: 'operational', acceptable: 'acceptable_condition', 'age-area': 'age_condition_area', program: 'program', fhwa: 'fhwa_tpm',
    };
    const exportKind = exportKinds[tab];
    if (!exportKind) return;
    setExporting(true);
    try {
      const response = await axios.post('/api/analytics/kpis/exports', {
        export_kind: exportKind, state_code: NJ_STATE_CODE, year,
        population: agePopulation, include_unknown: includeUnknown,
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
          title="Bridge performance ledger"
          subtitle="Observed inventory, NJDOT-style measures, program records, and federal diagnostics stay separate."
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
          <button type="button" onClick={() => void exportPackage()} disabled={year == null || tab === 'scenario' || exporting} className="inline-flex items-center gap-2 rounded-xl bg-slate-900 px-4 py-2 text-sm font-semibold text-white disabled:opacity-40">
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
        <div className="py-24 text-center text-slate-500"><Loader2 className="mx-auto mb-3 h-8 w-8 animate-spin text-brand-600" />Loading exact published KPIs…</div>
      ) : error ? (
        <div className="rounded-2xl border border-amber-300 bg-amber-50 p-6 text-amber-900">
          <p className="font-semibold">Published KPI population is unavailable</p><p className="mt-1 text-sm">{error}</p>
        </div>
      ) : !report ? (
        <div className="py-24 text-center text-slate-500">Publish bridge revisions in Loader before calculating observed KPIs.</div>
      ) : (
        <>
          <DefinitionStrip report={report} />

          {tab === 'inventory' && (
            <div className="space-y-6">
              <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
                {[
                  ['Published bridges', number(report.values.total_bridges), `${number(report.values.classifiable_bridges)} classifiable`],
                  ['Average snapshot age', `${number(report.values.average_age_at_snapshot, 1)} yr`, `${number(report.values.age_missing_or_invalid_count)} missing/invalid`],
                  ['Valid deck area', area(report.values.total_valid_deck_area_ft2), `${number(report.values.deck_area_missing_count)} missing geometry`],
                  ['High traffic', number(report.values.high_traffic_bridge_count), `single carried AADT ≥ ${number(report.values.high_traffic_threshold)}`],
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
                  <h2 className="font-semibold text-slate-900">Condition counts</h2>
                  <p className="mt-1 text-xs text-slate-500">Unknown remains visible and is not added to Fair.</p>
                  <ResponsiveContainer width="100%" height={260}>
                    <PieChart><Pie data={donut} dataKey="value" nameKey="name" innerRadius={58} outerRadius={92} paddingAngle={2}>{donut.map((item) => <Cell key={item.name} fill={item.fill} />)}</Pie><Tooltip /><Legend /></PieChart>
                  </ResponsiveContainer>
                </section>
                <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
                  <h2 className="font-semibold text-slate-900">County inventory by condition</h2>
                  <ResponsiveContainer width="100%" height={Math.max(320, report.values.county_condition_counts.length * 25)}>
                    <BarChart data={report.values.county_condition_counts} layout="vertical" margin={{ left: 28, right: 12 }}>
                      <CartesianGrid strokeDasharray="3 3" horizontal={false} />
                      <XAxis type="number" /><YAxis type="category" dataKey="county" width={100} tick={{ fontSize: 11 }} />
                      <Tooltip /><Legend />
                      <Bar dataKey="Good" stackId="condition" fill={GOOD} />
                      <Bar dataKey="Fair" stackId="condition" fill={UPPER_FAIR} />
                      <Bar dataKey="Poor" stackId="condition" fill={POOR} />
                      <Bar dataKey="Unknown" stackId="condition" fill={UNKNOWN} />
                    </BarChart>
                  </ResponsiveContainer>
                </section>
              </div>
              <div className="rounded-2xl border border-slate-200 bg-white p-5 text-sm text-slate-600">
                <span className="font-semibold text-slate-900">Completeness:</span> {number(report.values.carried_aadt_single_candidate_count)} bridges have one usable carried-feature AADT; {number(report.values.carried_aadt_ambiguous_count)} are ambiguous and {number(report.values.carried_aadt_missing_count)} are missing.
              </div>
            </div>
          )}

          {tab === 'acceptable' && (
            <div className="space-y-5">
              <div className="rounded-2xl border border-amber-300 bg-amber-50 px-5 py-4 text-sm text-amber-950">
                <p className="font-semibold">Candidate NJDOT operational definition</p>
                <p className="mt-1">State-maintained means B.CL.02 = S01 (State Transportation Department). The denominator policy still requires NJDOT program-owner approval.</p>
              </div>
              <div className="grid gap-5 lg:grid-cols-2">
                {[
                  {
                    title: 'Bridges ≥20 ft in acceptable condition',
                    value: percent(report.values.state_maintained_20ft_acceptable.percent_classifiable),
                    numerator: number(report.values.state_maintained_20ft_acceptable.acceptable_numerator),
                    denominator: number(report.values.state_maintained_20ft_acceptable.classifiable_denominator),
                    unknown: `${number(report.values.state_maintained_20ft_acceptable.unknown_condition_count)} unknown condition`,
                    alternate: `${percent(report.values.state_maintained_20ft_acceptable.percent_all_eligible)} if Unknown remains in denominator`,
                  },
                  {
                    title: 'Deck area in acceptable condition',
                    value: percent(report.values.state_maintained_deck_area_acceptable.percent_classifiable_area),
                    numerator: area(report.values.state_maintained_deck_area_acceptable.acceptable_area_numerator_ft2),
                    denominator: area(report.values.state_maintained_deck_area_acceptable.valid_classifiable_area_denominator_ft2),
                    unknown: `${area(report.values.state_maintained_deck_area_acceptable.unknown_condition_area_ft2)} unknown condition area`,
                    alternate: `${percent(report.values.state_maintained_deck_area_acceptable.percent_all_valid_area)} if Unknown area remains in denominator`,
                  },
                ].map((item) => (
                  <article key={item.title} className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
                    <p className="text-sm font-semibold text-slate-700">{item.title}</p>
                    <p className="mt-3 text-5xl font-bold tracking-tight text-slate-950">{item.value}</p>
                    <dl className="mt-6 grid grid-cols-2 gap-3 text-sm">
                      <div className="rounded-xl bg-emerald-50 p-3"><dt className="text-emerald-800">Numerator</dt><dd className="mt-1 font-bold text-emerald-950">{item.numerator}</dd></div>
                      <div className="rounded-xl bg-slate-100 p-3"><dt className="text-slate-600">Denominator</dt><dd className="mt-1 font-bold text-slate-950">{item.denominator}</dd></div>
                    </dl>
                    <p className="mt-4 text-xs text-slate-500">{item.unknown}. {item.alternate}.</p>
                  </article>
                ))}
              </div>
            </div>
          )}

          {tab === 'age-area' && ageReport && (
            <div className="space-y-5">
              <div className="flex flex-wrap items-center gap-3">
                <select value={agePopulation} onChange={(event) => setAgePopulation(event.target.value as typeof agePopulation)} className="rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm">
                  <option value="statewide">Statewide published inventory</option>
                  <option value="state_maintained">State-maintained (B.CL.02 = S01)</option>
                </select>
                <label className="inline-flex items-center gap-2 text-sm text-slate-700"><input type="checkbox" checked={includeUnknown} onChange={(event) => setIncludeUnknown(event.target.checked)} /> Show Unknown condition</label>
              </div>
              <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
                <h2 className="font-semibold text-slate-900">Deck area by snapshot age and condition</h2>
                <p className="mt-1 text-xs text-slate-500">Chart unit: thousand square feet. Age is {ageReport.year} minus B.W.01.</p>
                <ResponsiveContainer width="100%" height={430}>
                  <BarChart data={ageReport.series.map((row) => ({ ...row, Good: row.Good / 1000, 'Upper Fair': row['Upper Fair'] / 1000, 'Lower Fair': row['Lower Fair'] / 1000, Poor: row.Poor / 1000, Unknown: (row.Unknown || 0) / 1000 }))} margin={{ left: 8, right: 12, top: 24 }}>
                    <CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="age_bin" angle={-45} textAnchor="end" height={70} /><YAxis label={{ value: 'Deck area (thousand ft²)', angle: -90, position: 'insideLeft' }} /><Tooltip /><Legend />
                    <Bar dataKey="Good" stackId="area" fill={GOOD} />
                    <Bar dataKey="Upper Fair" stackId="area" fill={UPPER_FAIR} />
                    <Bar dataKey="Lower Fair" stackId="area" fill={LOWER_FAIR} />
                    <Bar dataKey="Poor" stackId="area" fill={POOR} />
                    {includeUnknown && <Bar dataKey="Unknown" stackId="area" fill={UNKNOWN} />}
                  </BarChart>
                </ResponsiveContainer>
              </section>
              <p className="text-xs text-slate-500">Excluded: {number(ageReport.excluded.missing_or_invalid_age_count)} missing/invalid age; {number(ageReport.excluded.missing_or_invalid_geometry_count)} missing/invalid geometry. Unknown condition area: {area(ageReport.excluded.unknown_condition_area_ft2)}.</p>
            </div>
          )}

          {tab === 'program' && (
            <div className="space-y-5">
              <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-slate-200 bg-white p-5">
                <div><h2 className="font-semibold text-slate-900">Governed fiscal-year program data</h2><p className="mt-1 text-sm text-slate-600">These values come from approved program files, never from SNBI inventory fields.</p></div>
                <div className="flex gap-2">
                  <a href="/api/analytics/kpis/program/template" className="inline-flex items-center gap-2 rounded-xl border border-slate-300 px-3 py-2 text-sm font-semibold text-slate-700"><Download className="h-4 w-4" />Template</a>
                  <label className="inline-flex cursor-pointer items-center gap-2 rounded-xl bg-brand-600 px-3 py-2 text-sm font-semibold text-white">
                    {programBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />} Import source
                    <input type="file" accept=".csv,.xlsx" className="sr-only" disabled={programBusy} onChange={(event) => { void importProgramFile(event.target.files?.[0] || null); event.target.value = ''; }} />
                  </label>
                </div>
              </div>
              {pendingProgramSource && (
                <div className="rounded-2xl border border-amber-300 bg-amber-50 p-5">
                  <p className="font-semibold text-amber-950">Independent review required</p>
                  <p className="mt-1 break-all text-xs text-amber-800">Source {pendingProgramSource}</p>
                  <div className="mt-3 flex flex-col gap-2 sm:flex-row"><input value={reviewNote} onChange={(event) => setReviewNote(event.target.value)} placeholder="Reviewer note describing the source check" className="min-w-0 flex-1 rounded-xl border border-amber-300 bg-white px-3 py-2 text-sm" /><button type="button" disabled={programBusy || !reviewNote.trim()} onClick={() => void approveProgramSource()} className="rounded-xl bg-amber-800 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">Approve as different reviewer</button></div>
                </div>
              )}
              {programMessage && <p className="rounded-xl bg-slate-100 px-4 py-3 text-sm text-slate-700">{programMessage}</p>}
              {!programReport || programReport.values.length === 0 ? (
                <PendingPanel icon={<FileCheck2 className="h-6 w-6" />} title="No approved program values" text="Import the strict template, then have a different authenticated user review and approve it. Missing fiscal-year values remain blank, never zero." />
              ) : (
                <div className="overflow-x-auto rounded-2xl border border-slate-200 bg-white shadow-sm">
                  <table className="min-w-full text-sm"><thead className="bg-slate-50"><tr><th className="px-4 py-3 text-left">Measure</th><th className="px-4 py-3 text-left">FY</th><th className="px-4 py-3 text-left">Value kind</th><th className="px-4 py-3 text-right">Value</th><th className="px-4 py-3 text-left">Unit</th><th className="px-4 py-3 text-left">Organization</th></tr></thead>
                    <tbody className="divide-y divide-slate-100">{programReport.values.map((value, index) => <tr key={`${value.import_sha256}-${index}`}><td className="px-4 py-3 font-medium text-slate-900">{definitionTitles.get(value.measure_id) || value.measure_id}</td><td className="px-4 py-3">{value.period_year}</td><td className="px-4 py-3 capitalize">{value.value_kind}</td><td className="px-4 py-3 text-right font-semibold">{number(Number(value.value_decimal), 2)}</td><td className="px-4 py-3">{value.unit}</td><td className="px-4 py-3">{value.organization}</td></tr>)}</tbody>
                  </table>
                </div>
              )}
            </div>
          )}
          {tab === 'fhwa' && (
            !fhwaReport ? <PendingPanel icon={<ShieldAlert className="h-6 w-6" />} title="FHWA TPM diagnostic unavailable" text="This year does not yet have a complete published NHS diagnostic population." /> :
            <div className="space-y-5">
              <div className="rounded-2xl border border-red-300 bg-red-50 p-5 text-red-950"><p className="font-semibold">{fhwaReport.label}</p><p className="mt-1 text-sm">It is not official validation and has not been compared with FHWA prepared data.</p></div>
              <div className="grid gap-4 sm:grid-cols-3">
                {[['Good deck area', fhwaReport.good_deck_area_percent, GOOD], ['Fair deck area', fhwaReport.fair_deck_area_percent, UPPER_FAIR], ['Poor deck area', fhwaReport.poor_deck_area_percent, POOR]].map(([label, value, color]) => <div key={label} className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm" style={{ borderTopColor: String(color), borderTopWidth: 4 }}><p className="text-sm font-semibold text-slate-700">{label}</p><p className="mt-2 text-4xl font-bold text-slate-950">{value == null ? '—' : `${value}%`}</p></div>)}
              </div>
              <div className="grid gap-5 lg:grid-cols-2"><div className="rounded-2xl border border-slate-200 bg-white p-5"><h3 className="font-semibold text-slate-900">Applicable population</h3><dl className="mt-3 space-y-2 text-sm">{Object.entries(fhwaReport.population).map(([key, value]) => <div key={key} className="flex justify-between gap-4"><dt className="text-slate-600">{key.replace(/_/g, ' ')}</dt><dd className="font-semibold">{number(value)}</dd></div>)}</dl></div><div className="rounded-2xl border border-slate-200 bg-white p-5"><h3 className="font-semibold text-slate-900">Exclusions</h3><dl className="mt-3 space-y-2 text-sm">{Object.entries(fhwaReport.exclusions).map(([key, value]) => <div key={key} className="flex justify-between gap-4"><dt className="text-slate-600">{key.replace(/_/g, ' ')}</dt><dd className="font-semibold">{number(value)}</dd></div>)}</dl></div></div>
              <div className="rounded-2xl border border-slate-200 bg-slate-50 p-5 text-sm text-slate-700"><p><span className="font-semibold">Profile:</span> {fhwaReport.definition_version}</p><p className="mt-1"><span className="font-semibold">Deck area:</span> {fhwaReport.profile.area_rule}</p><a className="mt-2 inline-block font-semibold text-brand-700 underline" href={fhwaReport.profile.source_url} target="_blank" rel="noreferrer">{fhwaReport.profile.source_title}</a></div>
            </div>
          )}
          {tab === 'scenario' && <PendingPanel icon={<Database className="h-6 w-6" />} title="Select a forecast scenario first" text="Future Good/Fair/Poor probabilities and intervention demand will only appear after selecting an immutable forecast and LCCA scenario. They will never be mixed with observed inventory." />}
        </>
      )}
    </div>
  );
};

const PendingPanel: React.FC<{ icon: React.ReactNode; title: string; text: string }> = ({ icon, title, text }) => (
  <div className="rounded-2xl border border-dashed border-slate-300 bg-slate-50 p-10 text-center">
    <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-white text-slate-600 shadow-sm">{icon}</div>
    <h2 className="mt-4 font-semibold text-slate-900">{title}</h2>
    <p className="mx-auto mt-2 max-w-2xl text-sm text-slate-600">{text}</p>
  </div>
);

export default Kpis;
