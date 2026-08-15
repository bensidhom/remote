// Compact, always-available Bridgette chat that lives in the left rail's empty
// space (below the tool list). Streams answers from its sidebar-only endpoint and
// persists for the session because HomeSidebar is always mounted. Hidden on the
// narrow icon-only rail (no room) — shown only on the wide (lg) sidebar.
import React, { useEffect, useRef, useState } from 'react';
import { Bot, Send, Square, Loader2 } from 'lucide-react';
import {
  isSidebarMapRequest,
  requestShowBridgeOnMap,
  type ShowBridgeMatch,
} from '../lib/mapBridgeEvents';
import {
  bridgetteStreamPath,
  cancelBridgetteRequest,
  newBridgetteRequestId,
  readBridgetteStream,
} from '../lib/bridgetteStream';
import { useDataGenerationEpoch } from '../lib/dataGeneration';
import { BridgetteQueryDetails } from './BridgetteEvidence';

interface Turn {
  q: string;
  a?: string;
  error?: string;
  loading?: boolean;
  decision?: 'clarification' | 'abstention';
  evidence?: Record<string, unknown>;
  sql?: string;
  rows?: unknown[][];
  columns?: string[];
  mapMatches?: ShowBridgeMatch[];
  stale?: boolean;
}

const SUGGESTIONS = ['Show bridge 0102151 on the map', 'Bridges per county', 'Avg deck rating each year'];

const SidebarBridgette: React.FC = () => {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const activeRequestIdRef = useRef<string | null>(null);
  const dataGenerationEpoch = useDataGenerationEpoch();

  useEffect(() => {
    if (dataGenerationEpoch === 0) return;
    const requestId = activeRequestIdRef.current;
    if (requestId) void cancelBridgetteRequest(requestId).catch(() => { /* best-effort */ });
    abortRef.current?.abort();
    abortRef.current = null;
    activeRequestIdRef.current = null;
    setBusy(false);
    setTurns((current) => current.map((turn) => ({
      ...turn,
      stale: true,
      loading: false,
      a: turn.loading && !turn.a ? 'Stopped because the published inventory changed.' : turn.a,
    })));
  }, [dataGenerationEpoch]);

  const ask = async (qRaw?: string) => {
    const question = (qRaw ?? input).trim();
    if (!question || busy) return;
    setInput('');
    setBusy(true);
    const idx = turns.length;
    setTurns((t) => [...t, { q: question, loading: true }]);
    const controller = new AbortController();
    abortRef.current = controller;
    let requestId: string | null = null;
    const update = (patch: Partial<Turn>) => {
      if (abortRef.current !== controller) return;
      if (requestId !== null && activeRequestIdRef.current !== requestId) return;
      setTurns((t) => t.map((x, i) => (i === idx ? { ...x, ...patch } : x)));
    };
    let answer = '';
    try {
      if (isSidebarMapRequest(question)) {
        const result = await requestShowBridgeOnMap(question, { signal: controller.signal });
        if (result.status === 'shown' || result.status === 'ambiguous') {
          update({
            loading: false,
            a: result.message,
            mapMatches: result.matches,
            evidence: result.resolution as unknown as Record<string, unknown> | undefined,
          });
        } else if (result.status === 'not_found' || result.status === 'stopped') {
          update({ loading: false, a: result.message });
        } else {
          update({ loading: false, error: result.message });
        }
        return;
      }
      requestId = newBridgetteRequestId();
      activeRequestIdRef.current = requestId;
      const fd = new FormData();
      fd.append('question', question);
      const response = await fetch(bridgetteStreamPath('sidebar'), {
        method: 'POST',
        body: fd,
        signal: controller.signal,
        headers: { 'X-Request-ID': requestId },
      });
      await readBridgetteStream(response, requestId, (event) => {
        if (activeRequestIdRef.current !== requestId) return;
        switch (event.type) {
          case 'accepted':
            update({ loading: true });
            break;
          case 'evidence':
            update({
              loading: false,
              evidence: event,
              sql: typeof event.sql === 'string' ? event.sql : undefined,
              rows: Array.isArray(event.rows) ? event.rows : undefined,
              columns: Array.isArray(event.columns) ? event.columns : undefined,
            });
            break;
          case 'clarification':
          case 'abstention':
            answer = typeof event.message === 'string' ? event.message : 'More information is needed.';
            update({ loading: false, a: answer, decision: event.type, evidence: event });
            break;
          case 'token':
            if (typeof event.content === 'string') answer += event.content;
            update({ loading: false, a: answer });
            break;
          case 'done':
            update({
              loading: false,
              a: answer || (typeof event.content === 'string' ? event.content : undefined),
            });
            break;
          case 'stopped':
            update({ loading: false, a: answer || 'Stopped.' });
            break;
          case 'error':
            update({ loading: false, error: event.detail || 'Request failed' });
            break;
        }
      });
    } catch (err: any) {
      const stopped = err?.name === 'AbortError' || controller.signal.aborted;
      if (stopped) update({ loading: false, a: answer || 'Stopped.' });
      else update({ loading: false, error: err?.message || 'Request failed' });
    } finally {
      if (requestId === null) {
        if (abortRef.current === controller) abortRef.current = null;
        setBusy(false);
      } else if (activeRequestIdRef.current === requestId) {
        activeRequestIdRef.current = null;
        abortRef.current = null;
        setBusy(false);
      }
      setTimeout(() => endRef.current?.scrollIntoView({ behavior: 'smooth' }), 50);
    }
  };

  const stop = () => {
    const requestId = activeRequestIdRef.current;
    if (requestId) cancelBridgetteRequest(requestId).catch(() => { /* best-effort */ });
    abortRef.current?.abort();
  };

  return (
    <div className="hidden lg:flex flex-1 min-h-0 flex-col border-t border-slate-200/80">
      <div className="flex items-center gap-1.5 px-3 pt-2.5 pb-1 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
        <Bot className="w-3.5 h-3.5" /> Ask Bridgette
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto ui-scroll px-3 pb-1 space-y-2">
        {turns.some((turn) => turn.stale) && (
          <div role="status" className="rounded-lg border border-amber-300 bg-amber-50 px-2 py-1.5 text-[11px] text-amber-800">
            Inventory changed; earlier answers are outdated.
          </div>
        )}
        {turns.length === 0 && (
          <div className="mt-1 space-y-1.5">
            <p className="text-[12px] text-slate-400 leading-snug">Ask about New Jersey bridges:</p>
            {SUGGESTIONS.map((s) => (
              <button
                key={s}
                onClick={() => ask(s)}
                className="block w-full text-left text-[12px] rounded-lg px-2.5 py-1.5 bg-slate-50 hover:bg-slate-100 text-slate-600 transition-colors"
              >
                {s}
              </button>
            ))}
          </div>
        )}
        {turns.map((t, i) => (
          <div key={i} className="space-y-1">
            <div className="rounded-lg rounded-tr-sm bg-brand-600 text-white px-2.5 py-1.5 text-[12px] leading-snug">{t.q}</div>
            {t.stale && <div className="text-[10px] font-semibold uppercase tracking-wide text-amber-600">Outdated inventory answer</div>}
            {t.loading && !t.a && (
              <div className="flex items-center gap-1.5 text-slate-400 text-[12px]"><Loader2 className="w-3 h-3 animate-spin" /> thinking…</div>
            )}
            {t.a && (
              <div className="rounded-lg rounded-tl-sm bg-slate-100 text-slate-700 px-2.5 py-1.5 text-[12px] leading-snug whitespace-pre-wrap break-words">{t.a}</div>
            )}
            {t.mapMatches && t.mapMatches.length > 0 && (
              <div className="space-y-1" role="list" aria-label="Matching bridges">
                {t.mapMatches.map((match) => (
                  <button
                    key={match.snapshot_id}
                    role="listitem"
                    onClick={() => ask(`Show bridge ${match.bridge_number} on the map`)}
                    className="block w-full rounded-md border border-brand-300 px-2 py-1.5 text-left text-[11px] text-brand-700 hover:bg-brand-50 focus:outline-none focus:ring-2 focus:ring-brand-400"
                    aria-label={`Show bridge ${match.bridge_number}, ${match.label}, inventory year ${match.snapshot_year}`}
                  >
                    <span className="block font-semibold">{match.bridge_number} · {match.label}</span>
                    <span className="block text-slate-500">{match.county_name ? `${match.county_name} County · ` : ''}Inventory year {match.snapshot_year}</span>
                  </button>
                ))}
              </div>
            )}
            {t.decision && Array.isArray(t.evidence?.options) && (
              <div className="flex flex-wrap gap-1.5">
                {(t.evidence.options as Array<{ id?: string; label?: string }>).map((option) => (
                  <button
                    key={String(option.id ?? option.label)}
                    onClick={() => ask(`${t.q} — ${String(option.label ?? option.id)}`)}
                    className="rounded-md border border-brand-300 px-2 py-1 text-left text-[11px] text-brand-600 hover:bg-brand-50"
                  >
                    {String(option.label ?? option.id)}
                  </button>
                ))}
              </div>
            )}
            <BridgetteQueryDetails evidence={t.evidence} sql={t.sql} rows={t.rows} columns={t.columns} stale={t.stale} compact />
            {t.error && <div className="rounded-lg bg-red-50 text-red-600 px-2.5 py-1.5 text-[12px]">{t.error}</div>}
          </div>
        ))}
        <div ref={endRef} />
      </div>

      <div className="shrink-0 p-2 flex items-end gap-1.5">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(); } }}
          rows={1}
          placeholder="Ask Bridgette…"
          className="flex-1 min-w-0 resize-none rounded-xl border border-slate-300 bg-white px-2.5 py-1.5 text-[12px] text-slate-800 focus:outline-none focus:ring-2 focus:ring-brand-400 max-h-24"
        />
        {busy ? (
          <button onClick={stop} title="Stop" className="grid place-items-center w-8 h-8 rounded-lg bg-red-500 hover:bg-red-600 text-white shrink-0 transition-colors">
            <Square className="w-3.5 h-3.5" />
          </button>
        ) : (
          <button onClick={() => ask()} disabled={!input.trim()} title="Send" className="grid place-items-center w-8 h-8 rounded-lg bg-brand-600 hover:bg-brand-700 text-white shrink-0 disabled:opacity-40 transition-colors">
            <Send className="w-3.5 h-3.5" />
          </button>
        )}
      </div>
    </div>
  );
};

export default SidebarBridgette;
