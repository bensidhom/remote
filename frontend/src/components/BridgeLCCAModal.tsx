// frontend/src/components/BridgeLCCAModal.tsx
// Forecast-driven Life-Cycle Cost Analysis. The deterioration curve comes from
// the fleet-trained Markov forecast (not a guess); costs are triggered when the
// controlling condition crosses thresholds and discounted to NPV; and the rehab
// year is optimised to find the least-cost intervention vs. running to failure.
import React, { useEffect, useState } from 'react';
import axios from 'axios';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer, ReferenceLine,
} from 'recharts';
import { Coins, Loader2, TrendingDown, Wrench, ExternalLink, SlidersHorizontal, RotateCcw } from 'lucide-react';
import { Modal, Button } from './ui';

interface Props {
  stateCode?: number | string;
  bridgeName?: string;
  bridgeNumber: string;
  yearBuilt?: string | number;
  onClose: () => void;
  embedded?: boolean;
}

// Public, citable sources behind each cost assumption (opened from the UI).
const SOURCES = {
  replace: 'https://www.fhwa.dot.gov/bridge/nbi/sd2024.cfm',
  rehab: 'https://wisconsindot.gov/Documents/doing-bus/local-gov/astnce-pgms/highway/tools/bos-cost.pdf',
  maint: 'https://www.fhwa.dot.gov/bridge/preservation/guide/guide.pdf',
  discount: 'https://onlinepubs.trb.org/onlinepubs/nchrp/nchrp_rpt_483.pdf',
} as const;

type NhsSel = 'blended' | 'nhs' | 'non_nhs';
interface Draft { replace: string; rehab: string; maint: string; penalty: string; discount: string; }
const EMPTY_DRAFT: Draft = { replace: '', rehab: '', maint: '', penalty: '', discount: '' };

interface LccaResult {
  status: string;
  start_year: number;
  horizon_years: number;
  discount_rate: number;
  parameters: {
    current_controlling_rating: number;
    annual_decline_rate: number;
    recovery_bump: number;
    recovery_source: 'learned' | 'default';
    deck_area: number;
    age: number | null;
    year_built: number | null;
    years_to_poor: number | null;
    fleet_transitions: number;
  };
  nhs?: boolean | null;
  unit_costs: { replace: number; rehab: number; annual_base: number; per_sqft_replace: number; per_sqft_rehab: number; per_sqft_annual_maint?: number; per_sqft_poor_penalty?: number };
  do_nothing: { npv: number; npv_with_user_costs: number; first_replacement_year: number | null; replacement_count: number };
  recommended: null | {
    rehab_year: number; rehab_years?: number[]; npv: number; npv_with_user_costs: number; savings: number;
    first_replacement_year: number | null; replacement_count: number;
  };
  chart: Array<{ year: number; do_nothing: number; intervention: number | null }>;
  cost_chart?: Array<{ year: number; do_nothing: number; intervention: number | null }>;
}

const usd = (n: number | null | undefined) =>
  n == null ? '—' : `$${Math.round(n).toLocaleString()}`;

// Compact money for axis ticks: $1.2M / $850k / $0.
const usdShort = (n: number | null | undefined) => {
  if (n == null) return '';
  const a = Math.abs(n);
  if (a >= 1e6) return `$${(n / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `$${Math.round(n / 1e3)}k`;
  return `$${Math.round(n)}`;
};

const BridgeLCCAModal: React.FC<Props> = ({ stateCode = 34, bridgeName, bridgeNumber, yearBuilt, onClose, embedded }) => {
  const [horizon, setHorizon] = useState(40);
  const [chartView, setChartView] = useState<'condition' | 'cost'>('condition');
  const [nhs, setNhs] = useState<NhsSel>('blended');
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);   // live edits
  const [applied, setApplied] = useState<Draft>(EMPTY_DRAFT); // what's actually sent
  const [data, setData] = useState<LccaResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    const qs = new URLSearchParams({ horizon_years: String(horizon) });
    if (nhs === 'nhs') qs.set('nhs', 'true');
    else if (nhs === 'non_nhs') qs.set('nhs', 'false');
    if (applied.replace) qs.set('replace_per_sqft', applied.replace);
    if (applied.rehab) qs.set('rehab_per_sqft', applied.rehab);
    if (applied.maint !== '') qs.set('annual_maint_per_sqft', applied.maint);
    if (applied.penalty !== '') qs.set('poor_penalty_per_sqft', applied.penalty);
    if (applied.discount) qs.set('discount_rate', String(Number(applied.discount) / 100));
    axios
      .get<LccaResult>(`/api/lcca/bridge/${stateCode}/${encodeURIComponent(bridgeNumber)}?${qs.toString()}`)
      .then((res) => setData(res.data))
      .catch((e) => setError(e.response?.data?.detail || e.response?.data?.message || 'Could not compute LCCA for this bridge.'))
      .finally(() => setLoading(false));
  }, [stateCode, bridgeNumber, horizon, nhs, applied]);

  const p = data?.parameters;
  const rec = data?.recommended;
  const uc = data?.unit_costs;
  const dirty = JSON.stringify(draft) !== JSON.stringify(applied);
  const applyDraft = () => setApplied({ ...draft });
  const resetAssumptions = () => { setDraft(EMPTY_DRAFT); setApplied(EMPTY_DRAFT); setNhs('blended'); };

  return (
    <Modal open onClose={onClose} inline={embedded} size="lg" title="Life-Cycle Cost Analysis" icon={<Coins className="w-5 h-5" />}>
      <p className="text-sm text-slate-500 dark:text-slate-400 -mt-1 mb-5">
        {bridgeName || 'Bridge'} — {bridgeNumber}
        {(p?.year_built || yearBuilt) ? ` · Built ${p?.year_built ?? yearBuilt}${p?.age != null ? ` · Age ${p.age} yrs` : ''}` : ''}
      </p>

      {loading && (
        <div className="py-16 text-center text-slate-500">
          <Loader2 className="w-8 h-8 animate-spin mx-auto mb-3 text-brand-500" />
          Running forecast-driven cost analysis…
        </div>
      )}
      {error && !loading && <p className="text-red-500 text-center py-10">{error}</p>}

      {data && !loading && p && (
        <div className="space-y-6">
          {/* Strategy comparison — the headline */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="rounded-2xl border border-slate-200 dark:border-slate-700 p-5 bg-white dark:bg-slate-900/40">
              <div className="flex items-center gap-2 text-slate-500 text-xs uppercase tracking-wider mb-1">
                <TrendingDown className="w-4 h-4" /> Do nothing (run to failure)
              </div>
              <div className="text-3xl font-bold text-slate-900 dark:text-white">{usd(data.do_nothing.npv)}</div>
              <div className="text-xs text-slate-400 mt-1">
                {data.horizon_years}-yr NPV · {data.do_nothing.replacement_count} replacement(s)
                {data.do_nothing.first_replacement_year ? ` · first ~${data.do_nothing.first_replacement_year}` : ''}
              </div>
            </div>
            <div className={`rounded-2xl border p-5 ${rec ? 'border-emerald-300 dark:border-emerald-700 bg-emerald-50/60 dark:bg-emerald-950/30' : 'border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900/40'}`}>
              <div className="flex items-center gap-2 text-emerald-600 dark:text-emerald-400 text-xs uppercase tracking-wider mb-1">
                <Wrench className="w-4 h-4" /> {rec ? `Recommended — rehab ${rec.rehab_year}` : 'Recommendation'}
              </div>
              {rec ? (
                <>
                  <div className="text-3xl font-bold text-slate-900 dark:text-white">{usd(rec.npv)}</div>
                  <div className="text-xs mt-1">
                    <span className="font-semibold text-emerald-600 dark:text-emerald-400">Saves {usd(rec.savings)}</span>
                    <span className="text-slate-400"> · {data.horizon_years}-yr NPV · replacement delayed to ~{rec.first_replacement_year ?? '—'}</span>
                  </div>
                </>
              ) : (
                <div className="text-sm text-slate-500 dark:text-slate-400 mt-1">
                  No rehab beats do-nothing within {data.horizon_years} yrs.
                </div>
              )}
            </div>
          </div>

          {/* Parameters + costs */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
            <div className="rounded-2xl border border-slate-200 dark:border-slate-700 p-5">
              <h3 className="text-sm font-semibold text-slate-700 dark:text-slate-300 mb-3">Condition inputs</h3>
              <dl className="space-y-2 text-sm">
                <Row k="Controlling rating now" v={`${p.current_controlling_rating}/9`} />
                <Row k="Deterioration rate" v={`${p.annual_decline_rate} pts/yr`} cls="text-amber-600 dark:text-amber-400" />
                <Row k="Years to Poor (≤4.9)" v={p.years_to_poor != null ? `${p.years_to_poor} yrs` : '—'} cls={p.years_to_poor != null && p.years_to_poor < 8 ? 'text-red-500' : ''} />
                <Row k="Rehab recovery" v={`+${p.recovery_bump} pts`} />
                <Row k="Deck area" v={p.deck_area > 0 ? `${p.deck_area.toLocaleString()} ft²` : 'N/A'} />
              </dl>
            </div>
            <div className="rounded-2xl border border-slate-200 dark:border-slate-700 p-5">
              <h3 className="text-sm font-semibold text-slate-700 dark:text-slate-300 mb-3">Cost assumptions (USD)</h3>
              <dl className="space-y-2 text-sm">
                <Row k="Annual maintenance (base)" v={usd(data.unit_costs.annual_base)} cls="text-amber-600 dark:text-amber-400" />
                <Row k={`Major rehab (@$${data.unit_costs.per_sqft_rehab}/ft²)`} v={usd(data.unit_costs.rehab)} cls="text-orange-600 dark:text-orange-400" />
                <Row k={`Replacement (@$${data.unit_costs.per_sqft_replace}/ft²)`} v={usd(data.unit_costs.replace)} cls="text-red-600 dark:text-red-400" />
                <hr className="my-1 border-slate-200 dark:border-slate-700" />
                <Row k="Discount rate" v={`${(data.discount_rate * 100).toFixed(0)}%`} />
                <Row k="Do-nothing incl. user costs" v={usd(data.do_nothing.npv_with_user_costs)} cls="text-slate-500" />
              </dl>
            </div>
          </div>

          {/* Editable assumptions — every cost is adjustable and source-linked */}
          <div className="rounded-2xl border border-slate-200 dark:border-slate-700 p-5">
            <div className="flex items-center justify-between gap-3 flex-wrap mb-1">
              <h3 className="flex items-center gap-2 text-sm font-semibold text-slate-700 dark:text-slate-300">
                <SlidersHorizontal className="w-4 h-4" /> Adjust assumptions
              </h3>
              <div className="flex items-center gap-2">
                <button onClick={resetAssumptions} className="inline-flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-700 dark:hover:text-slate-200">
                  <RotateCcw className="w-3.5 h-3.5" /> Reset
                </button>
                <Button onClick={applyDraft} disabled={!dirty || loading} className="!py-1.5 !px-3 text-xs">Apply</Button>
              </div>
            </div>
            <p className="text-xs text-slate-400 mb-4">Leave a field blank to use the default shown. Click the source link to verify each figure.</p>

            {/* NHS selector → drives the FHWA NJ replacement unit cost */}
            <div className="flex items-center gap-3 flex-wrap mb-4 text-sm">
              <span className="font-medium text-slate-600 dark:text-slate-300">Highway system</span>
              <div className="inline-flex rounded-lg border border-slate-300 dark:border-slate-600 overflow-hidden">
                {([['blended', 'Blended'], ['nhs', 'NHS'], ['non_nhs', 'Non-NHS']] as [NhsSel, string][]).map(([val, label]) => (
                  <button
                    key={val}
                    onClick={() => setNhs(val)}
                    className={`px-3 py-1.5 text-xs font-medium transition-colors ${nhs === val ? 'bg-brand-600 text-white' : 'bg-white dark:bg-slate-900 text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800'}`}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <SourceLink href={SOURCES.replace} label="FHWA NBI NJ unit costs" />
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-3">
              <CostInput label="Replacement" unit="$/ft²" value={draft.replace} placeholder={uc?.per_sqft_replace}
                onChange={(v) => setDraft((d) => ({ ...d, replace: v }))} href={SOURCES.replace} note="FHWA NBI 2024, New Jersey" />
              <CostInput label="Major rehab" unit="$/ft²" value={draft.rehab} placeholder={uc?.per_sqft_rehab}
                onChange={(v) => setDraft((d) => ({ ...d, rehab: v }))} href={SOURCES.rehab} note="≈40% of replacement (WisDOT components)" />
              <CostInput label="Annual maintenance" unit="$/ft²/yr" value={draft.maint} placeholder={uc?.per_sqft_annual_maint}
                onChange={(v) => setDraft((d) => ({ ...d, maint: v }))} href={SOURCES.maint} note="cyclical preservation, FHWA guide" />
              <CostInput label="Poor-condition penalty" unit="$/ft²/yr" value={draft.penalty} placeholder={uc?.per_sqft_poor_penalty}
                onChange={(v) => setDraft((d) => ({ ...d, penalty: v }))} note="risk/user cost while Poor (escalates yearly)" />
              <CostInput label="Discount rate" unit="%" value={draft.discount} placeholder={data ? data.discount_rate * 100 : undefined} step={0.1}
                onChange={(v) => setDraft((d) => ({ ...d, discount: v }))} href={SOURCES.discount} note="NCHRP 483 LCCA guidance" />
            </div>
          </div>

          {/* Condition projection: do-nothing vs recommended intervention */}
          <div className="rounded-2xl border border-slate-200 dark:border-slate-700 p-5">
            <div className="flex items-center justify-between gap-3 flex-wrap mb-3">
              <h3 className="text-sm font-semibold text-slate-700 dark:text-slate-300">
                {chartView === 'cost' ? 'Cumulative Cost & Strategy' : 'Condition Projection & Strategy'}
              </h3>
              <div className="flex items-center gap-3">
                {/* Condition vs. money view */}
                <div className="inline-flex rounded-lg border border-slate-300 dark:border-slate-600 overflow-hidden text-xs">
                  {(['condition', 'cost'] as const).map((v) => (
                    <button
                      key={v}
                      onClick={() => setChartView(v)}
                      className={`px-2.5 py-1 ${chartView === v
                        ? 'bg-brand-500 text-white'
                        : 'bg-white dark:bg-slate-900 text-slate-600 dark:text-slate-300'}`}
                    >
                      {v === 'condition' ? 'Condition' : 'Cost ($)'}
                    </button>
                  ))}
                </div>
                <label className="text-xs text-slate-500 flex items-center gap-2">
                  Horizon
                  <select
                    value={horizon}
                    onChange={(e) => setHorizon(Number(e.target.value))}
                    className="rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-900 px-2 py-1 text-slate-800 dark:text-slate-100"
                  >
                    {[20, 30, 40, 50, 75, 100].map((y) => <option key={y} value={y}>{y} yrs</option>)}
                  </select>
                </label>
              </div>
            </div>
            <ResponsiveContainer width="100%" height={300}>
              {chartView === 'cost' ? (
                <LineChart data={data.cost_chart || []} margin={{ top: 16, right: 12, left: 12, bottom: 5 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.25)" />
                  <XAxis dataKey="year" tick={{ fontSize: 12, fill: '#94a3b8' }} interval="preserveStartEnd" />
                  <YAxis tickFormatter={usdShort} width={64} tick={{ fontSize: 12, fill: '#94a3b8' }} />
                  <Tooltip contentStyle={{ borderRadius: 12, border: '1px solid rgba(148,163,184,0.3)' }} formatter={(v: any) => usd(Number(v))} />
                  <Legend />
                  {rec && (rec.rehab_years?.length ? rec.rehab_years : [rec.rehab_year]).map((ry, i) => (
                    <ReferenceLine key={ry} x={ry} stroke="#10b981" strokeWidth={1.5} strokeDasharray="4 3" label={i === 0 ? { value: `rehab ${ry}`, position: 'insideTopLeft', fontSize: 10, fill: '#10b981' } : undefined} />
                  ))}
                  <Line type="stepAfter" dataKey="do_nothing" name="Do nothing (cumulative)" stroke="#ef4444" strokeWidth={2} dot={false} />
                  {rec && <Line type="stepAfter" dataKey="intervention" name="With rehab (cumulative)" stroke="#10b981" strokeWidth={2} dot={false} connectNulls />}
                </LineChart>
              ) : (
                <LineChart data={data.chart} margin={{ top: 16, right: 12, left: 4, bottom: 5 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.25)" />
                  <XAxis dataKey="year" tick={{ fontSize: 12, fill: '#94a3b8' }} interval="preserveStartEnd" />
                  <YAxis domain={[0, 9]} ticks={[0, 1, 2, 3, 4, 5, 6, 7, 8, 9]} interval={0} tick={{ fontSize: 12, fill: '#94a3b8' }} allowDecimals={false} />
                  <Tooltip contentStyle={{ borderRadius: 12, border: '1px solid rgba(148,163,184,0.3)' }} />
                  <Legend />
                  {/* Threshold labels sit INSIDE the plot so they are never clipped at the right edge. */}
                  <ReferenceLine y={4.9} stroke="#ef4444" strokeDasharray="5 5" label={{ value: 'Poor ≤4', position: 'insideTopRight', fontSize: 10, fill: '#ef4444' }} />
                  <ReferenceLine y={3} stroke="#7f1d1d" strokeDasharray="2 4" label={{ value: 'Replace ≤3', position: 'insideBottomRight', fontSize: 10, fill: '#7f1d1d' }} />
                  {rec && (rec.rehab_years?.length ? rec.rehab_years : [rec.rehab_year]).map((ry, i) => (
                    <ReferenceLine key={ry} x={ry} stroke="#10b981" strokeWidth={1.5} strokeDasharray="4 3" label={i === 0 ? { value: `rehab ${ry}`, position: 'insideTopLeft', fontSize: 10, fill: '#10b981' } : undefined} />
                  ))}
                  <Line type="monotone" dataKey="do_nothing" name="Do nothing" stroke="#ef4444" strokeWidth={2} dot={false} />
                  {rec && <Line type="monotone" dataKey="intervention" name="With rehab" stroke="#10b981" strokeWidth={2} dot={false} connectNulls />}
                </LineChart>
              )}
            </ResponsiveContainer>
            <p className="mt-3 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
              {chartView === 'cost' ? (
                <>The <span className="font-semibold">cumulative discounted spend</span> of each strategy over the horizon. Steps are capital events (rehab or replacement); the ending values are the present-value totals shown above (before the salvage credit). Where the red line ends higher than the green, acting is cheaper over the long run.</>
              ) : (
                <>The <span className="font-semibold text-emerald-600 dark:text-emerald-400">rehab timing is calculated from condition, not manual</span>: a rehab is scheduled whenever the bridge degrades into <span className="font-semibold">Poor (≤4.9)</span>
                {rec ? <> — here, <span className="font-semibold">{(rec.rehab_years?.length ? rec.rehab_years : [rec.rehab_year]).join(', ')}</span></> : ' (doing nothing is cheaper here)'}. NPV includes an escalating risk/user-cost penalty for every year a bridge is left in Poor condition, so deferring is no longer &ldquo;free.&rdquo; Change the horizon above to see how the plan shifts.</>
              )}
            </p>
          </div>
        </div>
      )}
    </Modal>
  );
};

const Row: React.FC<{ k: string; v: string; cls?: string }> = ({ k, v, cls }) => (
  <div className="flex justify-between gap-3">
    <dt className="text-slate-500 dark:text-slate-400">{k}</dt>
    <dd className={`font-semibold text-slate-800 dark:text-slate-100 text-right ${cls || ''}`}>{v}</dd>
  </div>
);

// Small "source ↗" link that opens the citation in the system browser.
const SourceLink: React.FC<{ href: string; label?: string }> = ({ href, label }) => (
  <a
    href={href}
    target="_blank"
    rel="noreferrer noopener"
    className="inline-flex items-center gap-1 text-xs text-brand-600 dark:text-brand-400 hover:underline"
    title={`Open source: ${href}`}
  >
    {label || 'source'} <ExternalLink className="w-3 h-3" />
  </a>
);

// One editable cost assumption: a number input whose placeholder shows the
// current effective (default) value, plus an optional source link + note.
const CostInput: React.FC<{
  label: string; unit: string; value: string; placeholder?: number;
  onChange: (v: string) => void; href?: string; note?: string; step?: number;
}> = ({ label, unit, value, placeholder, onChange, href, note, step }) => (
  <div>
    <div className="flex items-center justify-between gap-2 mb-1">
      <label className="text-sm font-medium text-slate-600 dark:text-slate-300">{label}</label>
      {href && <SourceLink href={href} />}
    </div>
    <div className="flex items-center gap-2">
      <input
        type="number"
        min={0}
        step={step ?? 1}
        value={value}
        placeholder={placeholder != null ? String(Math.round(placeholder * 100) / 100) : ''}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-900 px-3 py-1.5 text-sm text-slate-800 dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-400"
      />
      <span className="text-xs text-slate-400 whitespace-nowrap">{unit}</span>
    </div>
    {note && <p className="text-[11px] text-slate-400 mt-1">{note}</p>}
  </div>
);

export default BridgeLCCAModal;
