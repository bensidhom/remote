// frontend/src/pages/Kpis.tsx
// New Jersey bridge KPIs. This build only carries NJ bridges, so there is no
// per-state comparison; per-bridge detail lives on the map card. This page shows
// statewide NJ headline KPIs, the condition breakdown, and per-county analysis.
import React, { useEffect, useMemo, useRef, useState } from 'react';
import axios from 'axios';
import jsPDF from 'jspdf';
import html2canvas from 'html2canvas';
import { SectionHeader, CountUp } from '../components/ui';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, PieChart, Pie, Cell, Legend,
} from 'recharts';
import { BarChart3, Download, Loader2 } from 'lucide-react';

const GOOD = '#22c55e';
const FAIR = '#eab308';
const POOR = '#ef4444';
const UNKNOWN = '#94a3b8';

interface StateKpi {
  state_code: number;
  state_name: string;
  total_bridges: number;
  good_bridges: number;
  fair_bridges: number;
  poor_bridges: number;
  avg_age: number | null;
  pct_good: number;
  pct_fair: number;
  pct_poor: number;
  pct_old?: number;
  old_bridges?: number;
  high_traffic_bridges?: number;
  total_deck_area?: number;
}

interface CountyKpi {
  county_code: number;
  county_name: string;
  total_bridges: number;
  good_bridges: number;
  fair_bridges: number;
  poor_bridges: number;
  avg_age: number | null;
  pct_good: number;
  pct_fair: number;
  pct_poor: number;
}

const NJ_STATE_CODE = 34;
const fmt = (n: number | null | undefined, digits = 0) =>
  n == null || isNaN(Number(n)) ? '—' : Number(n).toLocaleString(undefined, { maximumFractionDigits: digits });

const Kpis: React.FC = () => {
  const [nj, setNj] = useState<StateKpi | null>(null);
  const [counties, setCounties] = useState<CountyKpi[]>([]);
  const [years, setYears] = useState<number[]>([]);
  const [year, setYear] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);

  // Load available data years; default to the latest.
  useEffect(() => {
    axios
      .get<{ years: number[] }>('/api/loaded-years')
      .then((res) => {
        const ys = (res.data.years || []).slice().sort((a, b) => b - a);
        setYears(ys);
        setYear(ys.length ? ys[0] : null);
      })
      .catch(() => { setYears([]); setYear(null); });
  }, []);

  // Fetch statewide + per-county KPIs for the selected year.
  useEffect(() => {
    setLoading(true);
    const params = year != null ? { year } : {};
    Promise.all([
      axios.get<StateKpi[]>('/api/kpis', { params }),
      axios.get<CountyKpi[]>('/api/kpis/county', { params: { state_code: NJ_STATE_CODE, ...params } }),
    ])
      .then(([kpiRes, countyRes]) => {
        const njRow = (kpiRes.data || []).find((s) => s.state_code === NJ_STATE_CODE) || (kpiRes.data || [])[0] || null;
        setNj(njRow);
        setCounties(countyRes.data || []);
      })
      .catch(() => { setNj(null); setCounties([]); })
      .finally(() => setLoading(false));
  }, [year]);

  const unknown = nj ? Math.max(0, nj.total_bridges - nj.good_bridges - nj.fair_bridges - nj.poor_bridges) : 0;

  const donutData = useMemo(() => {
    if (!nj) return [];
    return [
      { name: 'Good', value: nj.good_bridges, color: GOOD },
      { name: 'Fair', value: nj.fair_bridges, color: FAIR },
      { name: 'Poor', value: nj.poor_bridges, color: POOR },
      ...(unknown > 0 ? [{ name: 'Unrated', value: unknown, color: UNKNOWN }] : []),
    ].filter((d) => d.value > 0);
  }, [nj, unknown]);

  const countyChart = useMemo(
    () =>
      [...counties]
        .sort((a, b) => b.total_bridges - a.total_bridges)
        .map((c) => ({
          county: c.county_name,
          Good: c.good_bridges,
          Fair: c.fair_bridges,
          Poor: c.poor_bridges,
        })),
    [counties],
  );

  const countyTable = useMemo(
    () => [...counties].sort((a, b) => b.poor_bridges - a.poor_bridges || b.total_bridges - a.total_bridges),
    [counties],
  );

  const printRef = useRef<HTMLDivElement>(null);
  const [exporting, setExporting] = useState(false);

  // HTML-to-PDF: snapshot the actual KPI dashboard (cards, charts, table, colors)
  // and lay it across A4 pages — so the PDF looks like the page, not a plain table.
  const exportPdf = async () => {
    const el = printRef.current;
    if (!el) return;
    setExporting(true);
    try {
      const canvas = await html2canvas(el, { scale: 2, backgroundColor: '#ffffff', useCORS: true });
      const pdf = new jsPDF('p', 'mm', 'a4');
      const margin = 8;
      const pageW = pdf.internal.pageSize.getWidth();
      const pageH = pdf.internal.pageSize.getHeight();
      const imgW = pageW - margin * 2;
      const imgH = (canvas.height * imgW) / canvas.width;
      const imgData = canvas.toDataURL('image/png');
      const usableH = pageH - margin * 2;
      let heightLeft = imgH;
      let position = margin;
      pdf.addImage(imgData, 'PNG', margin, position, imgW, imgH);
      heightLeft -= usableH;
      while (heightLeft > 0) {
        position -= usableH; // shift the tall image up for the next page slice
        pdf.addPage();
        pdf.addImage(imgData, 'PNG', margin, position, imgW, imgH);
        heightLeft -= usableH;
      }
      pdf.save(`nj-bridge-kpis-${year != null ? year : 'Latest'}.pdf`);
    } finally {
      setExporting(false);
    }
  };

  const cards = nj
    ? [
        { label: 'Total Bridges', value: nj.total_bridges, sub: 'New Jersey', color: '#0f172a' },
        { label: 'Good', value: nj.good_bridges, sub: `${nj.pct_good}% of NJ`, color: GOOD },
        { label: 'Fair', value: nj.fair_bridges, sub: `${nj.pct_fair}% of NJ`, color: FAIR },
        { label: 'Poor', value: nj.poor_bridges, sub: `${nj.pct_poor}% of NJ`, color: POOR },
        { label: 'Average Age', value: Math.round(nj.avg_age ?? 0), sub: 'years', color: '#0f172a', suffix: ' yr' },
        { label: 'Built Before 1980', value: nj.old_bridges ?? 0, sub: nj.pct_old != null ? `${nj.pct_old}% of NJ` : '', color: '#7c3aed' },
        { label: 'High Traffic', value: nj.high_traffic_bridges ?? 0, sub: 'AADT > 10,000', color: '#2563eb' },
        { label: 'Counties', value: counties.length, sub: 'reporting', color: '#0891b2' },
      ]
    : [];

  return (
    <div className="space-y-8">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <SectionHeader
          icon={<BarChart3 className="w-6 h-6" />}
          title="New Jersey Bridge KPIs"
          subtitle={`Statewide condition & inventory${year != null ? ` · ${year}` : ''}`}
        />
        <div className="flex items-center gap-2">
          {years.length > 0 && (
            <select
              value={year ?? ''}
              onChange={(e) => setYear(e.target.value ? Number(e.target.value) : null)}
              className="rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800"
            >
              {years.map((y) => <option key={y} value={y}>{y}</option>)}
            </select>
          )}
          <button
            onClick={exportPdf}
            disabled={!nj || exporting}
            className="inline-flex items-center gap-2 rounded-xl bg-brand-600 hover:bg-brand-700 text-white px-4 py-2 text-sm font-medium disabled:opacity-50"
          >
            {exporting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
            {exporting ? 'Exporting…' : 'Export PDF'}
          </button>
        </div>
      </div>

      {loading ? (
        <div className="py-24 text-center text-slate-500">
          <Loader2 className="w-8 h-8 animate-spin mx-auto mb-3 text-brand-500" />
          Loading New Jersey KPIs…
        </div>
      ) : !nj || nj.total_bridges === 0 ? (
        <div className="py-24 text-center text-slate-500">
          No New Jersey bridge data loaded{year != null ? ` for ${year}` : ''}. Load bridges from the Loader to see KPIs.
        </div>
      ) : (
        <div ref={printRef} className="space-y-8 bg-white p-1">
          {/* Title (shown in the exported PDF) */}
          <div>
            <h2 className="text-xl font-bold text-slate-900">New Jersey Bridge KPIs</h2>
            <p className="text-sm text-slate-500">Statewide condition &amp; inventory{year != null ? ` · ${year}` : ' · Latest'}</p>
          </div>

          {/* Headline KPI cards */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            {cards.map((c) => (
              <div key={c.label} className="rounded-2xl bg-white border border-slate-200 shadow-sm p-5" style={{ borderTop: `3px solid ${c.color}` }}>
                <p className="text-[11px] uppercase tracking-wider text-slate-500 mb-1">{c.label}</p>
                <p className="text-3xl font-bold text-slate-900">
                  <CountUp to={c.value} />{(c as any).suffix || ''}
                </p>
                {c.sub && <p className="text-xs text-slate-400 mt-1">{c.sub}</p>}
              </div>
            ))}
          </div>

          {/* Condition breakdown */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <div className="rounded-3xl bg-white border border-slate-200 shadow-sm p-6">
              <h2 className="text-lg font-semibold text-slate-800 mb-2">Condition Breakdown</h2>
              <ResponsiveContainer width="100%" height={260}>
                <PieChart>
                  <Pie data={donutData} dataKey="value" nameKey="name" innerRadius={62} outerRadius={95} paddingAngle={2}>
                    {donutData.map((d) => <Cell key={d.name} fill={d.color} />)}
                  </Pie>
                  <Tooltip formatter={(v: any, n: any) => [`${fmt(v)} bridges`, n]} />
                  <Legend />
                </PieChart>
              </ResponsiveContainer>
            </div>

            {/* Per-county stacked bars */}
            <div className="rounded-3xl bg-white border border-slate-200 shadow-sm p-6 lg:col-span-2">
              <h2 className="text-lg font-semibold text-slate-800 mb-2">Bridges by County &amp; Condition</h2>
              <ResponsiveContainer width="100%" height={Math.max(260, countyChart.length * 26)}>
                <BarChart data={countyChart} layout="vertical" margin={{ left: 24, right: 16, top: 8, bottom: 8 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.25)" horizontal={false} />
                  <XAxis type="number" tick={{ fontSize: 11, fill: '#64748b' }} />
                  <YAxis type="category" dataKey="county" width={84} tick={{ fontSize: 11, fill: '#475569' }} />
                  <Tooltip />
                  <Legend />
                  <Bar dataKey="Good" stackId="c" fill={GOOD} />
                  <Bar dataKey="Fair" stackId="c" fill={FAIR} />
                  <Bar dataKey="Poor" stackId="c" fill={POOR} radius={[0, 4, 4, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>

          {/* County table */}
          <div className="rounded-3xl bg-white border border-slate-200 shadow-sm overflow-hidden">
            <h2 className="text-lg font-semibold text-slate-800 px-6 pt-6 pb-3">Counties — sorted by poor bridges</h2>
            <div className="overflow-x-auto">
              <table className="min-w-full text-sm">
                <thead className="bg-slate-50 text-slate-600">
                  <tr>
                    <th className="text-left px-6 py-3 font-medium">County</th>
                    <th className="text-right px-4 py-3 font-medium">Total</th>
                    <th className="text-right px-4 py-3 font-medium">Good</th>
                    <th className="text-right px-4 py-3 font-medium">Fair</th>
                    <th className="text-right px-4 py-3 font-medium">Poor</th>
                    <th className="text-right px-4 py-3 font-medium">Poor %</th>
                    <th className="text-right px-6 py-3 font-medium">Avg Age</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {countyTable.map((c) => (
                    <tr key={c.county_code} className="text-slate-700 hover:bg-slate-50">
                      <td className="px-6 py-3 font-medium text-slate-800">{c.county_name}</td>
                      <td className="px-4 py-3 text-right">{fmt(c.total_bridges)}</td>
                      <td className="px-4 py-3 text-right" style={{ color: GOOD }}>{fmt(c.good_bridges)}</td>
                      <td className="px-4 py-3 text-right" style={{ color: '#b45309' }}>{fmt(c.fair_bridges)}</td>
                      <td className="px-4 py-3 text-right font-semibold" style={{ color: POOR }}>{fmt(c.poor_bridges)}</td>
                      <td className="px-4 py-3 text-right">{c.pct_poor}%</td>
                      <td className="px-6 py-3 text-right">{c.avg_age != null ? fmt(c.avg_age) : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default Kpis;
