import React, { useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import axios from 'axios';
import { Activity, AlertTriangle, Image, LogOut, Lock, MessageSquare, Network } from 'lucide-react';
import { TOOLS, SIDEBAR_TOOLS, openTool, type ToolId } from '../lib/tools';
import { useAdmin } from '../context/AdminContext';
import SidebarBridgette from './SidebarBridgette';

interface Props {
  onSignOut?: () => void;
  userEmail?: string;
}

// Only the four core tools live on the rail; everything else is reached from the
// bridge card on the map.
const SIDEBAR_ITEMS = SIDEBAR_TOOLS
  .map((id) => TOOLS.find((t) => t.id === id))
  .filter(Boolean) as typeof TOOLS;

/**
 * Left rail — the single primary navigation for the map-first shell (the old top
 * bar is gone). Brand returns to the map; Settings and Sign out live at the
 * bottom. Admin tools are only shown when the server-issued role allows them.
 */
const HomeSidebar: React.FC<Props> = ({ onSignOut, userEmail }) => {
  const { isAdmin } = useAdmin();
  const [usage, setUsage] = useState<any>(null);

  const launch = (id: ToolId, admin?: boolean) => {
    if (admin && !isAdmin) return;
    openTool(id);
  };

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const r = await axios.get('/api/api-usage');
        if (!cancelled) setUsage(r.data);
      } catch {
        if (!cancelled) setUsage(null);
      }
    };
    load();
    const id = setInterval(load, 15000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  return (
    <aside className="shrink-0 w-20 lg:w-60 h-full bg-white/80 backdrop-blur-xl border-r border-slate-200 flex flex-col">
      {/* Tools — on the wide rail they take only their height so the Bridgette
          chat can fill the space below; on the narrow rail they keep flex-1. */}
      <nav className="flex-1 lg:flex-none overflow-y-auto ui-scroll py-3 px-2 lg:px-3 flex flex-col gap-1">
        {SIDEBAR_ITEMS.filter(({ admin }) => !admin || isAdmin).map(({ id, label, Icon, accent, admin }) => (
          <motion.button
            key={id}
            onClick={() => launch(id, admin)}
            whileTap={{ scale: 0.98 }}
            className="group flex items-center gap-3 rounded-xl px-2.5 py-2.5 text-left hover:bg-slate-100 transition-colors"
            title={label}
          >
            <span className="grid place-items-center w-10 h-10 rounded-lg shrink-0 transition-transform group-hover:scale-105" style={{ background: `${accent}1f`, color: accent }}>
              <Icon className="w-5 h-5" />
            </span>
            <span className="min-w-0 hidden lg:block">
              <span className="flex items-center gap-1.5 text-sm font-semibold text-slate-700 leading-tight truncate">
                {label}
                {admin && <Lock className="w-3 h-3 text-slate-400 shrink-0" />}
              </span>
            </span>
          </motion.button>
        ))}
      </nav>

      {/* Inline Bridgette chat fills the empty rail space (wide rail only). */}
      <SidebarBridgette />

      {/* Footer: sign out */}
      <div className="shrink-0 border-t border-slate-200/80 p-2 lg:p-3 flex flex-col gap-1">
        <ApiUsagePanel usage={usage} />
        {onSignOut && (
          <button
            onClick={onSignOut}
            className="flex items-center gap-3 rounded-xl px-2.5 py-2 text-slate-600 hover:bg-red-50 hover:text-red-600 transition-colors"
            title={userEmail ? `Sign out — ${userEmail}` : 'Sign out'}
          >
            <span className="grid place-items-center w-10 h-10 shrink-0"><LogOut className="w-5 h-5" /></span>
            <span className="hidden lg:block min-w-0">
              <span className="block text-sm font-medium leading-tight">Sign out</span>
              {userEmail && <span className="block text-[11px] text-slate-400 truncate max-w-[150px]">{userEmail}</span>}
            </span>
          </button>
        )}
      </div>
    </aside>
  );
};

const fmt = (n: number | undefined | null) => {
  const value = Number(n || 0);
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(value);
};

const ApiUsagePanel: React.FC<{ usage: any }> = ({ usage }) => {
  const daily = usage?.usage || {};
  const cats = daily.by_category || {};
  const tokenPct = typeof usage?.percent?.tokens === 'number' ? usage.percent.tokens : null;
  const requestPct = typeof usage?.percent?.requests === 'number' ? usage.percent.requests : null;
  const pct = tokenPct ?? requestPct;
  const label = tokenPct !== null ? 'tokens' : requestPct !== null ? 'requests' : 'tracked';
  const limit = tokenPct !== null ? usage?.limits?.tokens : requestPct !== null ? usage?.limits?.requests : null;
  const used = tokenPct !== null ? daily.tokens : requestPct !== null ? daily.requests : daily.tokens;
  const reset = usage?.reset_at ? new Date(usage.reset_at) : null;
  const resetText = reset ? reset.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : '';
  const model = usage?.model ? String(usage.model).replace(/^.*\//, '') : (usage?.provider || 'hosted');

  return (
    <div className="hidden lg:block rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 mb-1">
      <div className="flex items-center justify-between gap-2">
        <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-slate-600">
          <Activity className="w-3.5 h-3.5" />
          API
        </span>
        <span className="text-[10px] uppercase tracking-wide text-slate-400 truncate max-w-[100px]" title={usage?.model || usage?.provider || 'hosted'}>{model}</span>
      </div>

      <div className="mt-2 grid grid-cols-3 gap-1 text-[10px] text-slate-500">
        <span className="inline-flex items-center gap-1" title="QA requests"><MessageSquare className="w-3 h-3" />{fmt(cats.qa?.requests)}</span>
        <span className="inline-flex items-center gap-1" title="Image requests"><Image className="w-3 h-3" />{fmt(cats.images?.requests)}</span>
        <span className="inline-flex items-center gap-1" title="Embedding batches"><Network className="w-3 h-3" />{fmt(cats.embedding?.requests)}</span>
      </div>

      <div className="mt-2 h-1.5 rounded-full bg-slate-200 overflow-hidden">
        <div
          className={`h-full rounded-full ${usage?.exhausted ? 'bg-red-500' : 'bg-indigo-500'}`}
          style={{ width: `${pct ?? 0}%` }}
        />
      </div>
      <div className="mt-1 flex justify-between text-[10px] text-slate-400">
        <span>{usage?.limits_configured ? `${fmt(used)} / ${fmt(limit)} ${label}` : `${fmt(daily.tokens)} est. tokens`}</span>
        {usage?.limits_configured && resetText && <span>resets {resetText}</span>}
      </div>
      {usage?.exhausted && (
        <div className="mt-1 flex items-start gap-1 text-[10px] text-red-600">
          <AlertTriangle className="w-3 h-3 mt-0.5 shrink-0" />
          <span>Resume after {resetText || 'daily reset'}.</span>
        </div>
      )}
    </div>
  );
};

export default HomeSidebar;
