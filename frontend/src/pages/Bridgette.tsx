// frontend/src/pages/Bridgette.tsx
// Bridgette — natural-language Q&A over the New Jersey bridge database (text-to-SQL).
import React, { useEffect, useRef, useState } from 'react';
import { Bot, Send, Loader2, User, Square } from 'lucide-react';
import { SectionHeader, Button } from '../components/ui';
import {
  isMapRequest,
  requestShowBridgeOnMap,
  type ShowBridgeMatch,
} from '../lib/mapBridgeEvents';
import {
  bridgetteStreamPath,
  cancelBridgetteRequest,
  clarificationDraft,
  newBridgetteRequestId,
  readBridgetteStream,
  type BridgetteClarificationOption,
} from '../lib/bridgetteStream';
import { useDataGenerationEpoch } from '../lib/dataGeneration';
import { BridgetteAnswerStatus, BridgetteQueryDetails } from '../components/BridgetteEvidence';

interface Turn {
  question: string;
  answer?: string;
  sql?: string;
  rows?: unknown[][];
  columns?: string[];
  count?: number;
  evidence?: Record<string, unknown>;
  decision?: 'clarification' | 'abstention';
  error?: string;
  loading?: boolean;
  mapMatches?: ShowBridgeMatch[];
  stale?: boolean;
}

const SUGGESTIONS = [
  'How many bridges are in each county?',
  'Which 10 bridges have the lowest deck condition rating?',
  'What is the average superstructure rating in Bergen County?',
  'How many bridges were built before 1970?',
];

const Bridgette: React.FC = () => {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
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
      answer: turn.loading && !turn.answer ? 'Stopped because the published bridge inventory changed.' : turn.answer,
    })));
  }, [dataGenerationEpoch]);

  const ask = async (qRaw: string) => {
    const question = qRaw.trim();
    if (!question || busy) return;
    setInput('');
    setBusy(true);
    const idx = turns.length;
    setTurns((t) => [...t, { question, loading: true }]);
    const controller = new AbortController();
    const requestId = newBridgetteRequestId();
    abortRef.current = controller;
    activeRequestIdRef.current = requestId;
    const update = (patch: Partial<Turn>) => {
      if (activeRequestIdRef.current !== requestId) return;
      setTurns((t) => t.map((turn, i) => (i === idx ? { ...turn, ...patch } : turn)));
    };
    let answer = '';
    try {
      if (isMapRequest(question)) {
        const result = await requestShowBridgeOnMap(question, { signal: controller.signal });
        if (result.status === 'shown' || result.status === 'ambiguous') {
          update({
            loading: false,
            answer: result.message,
            mapMatches: result.matches,
            evidence: result.resolution as unknown as Record<string, unknown> | undefined,
          });
        } else if (result.status === 'not_found' || result.status === 'stopped') {
          update({ loading: false, answer: result.message });
        } else {
          update({ loading: false, error: result.message });
        }
        return;
      }
      const fd = new FormData();
      fd.append('question', question);
      const response = await fetch(bridgetteStreamPath('full'), {
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
              sql: typeof event.sql === 'string' ? event.sql : undefined,
              rows: Array.isArray(event.rows) ? event.rows : undefined,
              columns: Array.isArray(event.columns) ? event.columns : undefined,
              count: typeof event.count === 'number' ? event.count : undefined,
              evidence: event,
            });
            break;
          case 'clarification':
            setInput((current) => current.trim() ? current : clarificationDraft(question));
            setTimeout(() => inputRef.current?.focus(), 0);
            answer = typeof event.message === 'string' ? event.message : 'More information is needed.';
            update({ loading: false, answer, decision: event.type, evidence: event });
            break;
          case 'abstention':
            answer = typeof event.message === 'string' ? event.message : 'More information is needed.';
            update({ loading: false, answer, decision: event.type, evidence: event });
            break;
          case 'token':
            if (typeof event.content === 'string') answer += event.content;
            update({ loading: false, answer });
            break;
          case 'done':
            update({
              loading: false,
              answer: answer || (typeof event.content === 'string' ? event.content : undefined),
            });
            break;
          case 'stopped':
            update({ loading: false, answer: answer || 'Stopped.' });
            break;
          case 'error':
            update({ loading: false, error: event.detail || 'Request failed' });
            break;
        }
      });
    } catch (err: any) {
      const stopped = err?.name === 'AbortError' || controller.signal.aborted;
      if (stopped) update({ loading: false, answer: answer || 'Stopped.' });
      else update({ loading: false, error: err?.message || 'Request failed' });
    } finally {
      if (activeRequestIdRef.current === requestId) {
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
    <div className="flex flex-col h-full min-h-[480px]">
      <SectionHeader
        icon={<Bot className="w-6 h-6" />}
        title="Bridgette"
        subtitle="Ask anything about New Jersey bridges — answered live from the database"
      />

      <div className="flex-1 overflow-auto ui-scroll space-y-5 pr-1">
        {turns.some((turn) => turn.stale) && (
          <div role="status" className="rounded-xl border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800">
            The published inventory changed. Earlier answers are marked outdated; ask again for current evidence.
          </div>
        )}
        {turns.length === 0 && (
          <div className="space-y-3">
            <p className="text-sm text-slate-500 dark:text-slate-400">Try one of these:</p>
            <div className="grid sm:grid-cols-2 gap-2">
              {SUGGESTIONS.map((s) => (
                <button key={s} onClick={() => ask(s)} className="text-left text-sm px-4 py-3 rounded-xl glass border border-slate-200/70 dark:border-slate-700/70 hover:border-brand-400 transition-colors">
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}

        {turns.map((t, i) => (
          <div key={i} className="space-y-2">
            {/* Question */}
            <div className="flex items-start gap-2 justify-end">
              <div className="max-w-[80%] rounded-2xl rounded-tr-sm bg-brand-600 text-white px-4 py-2.5 text-sm">{t.question}</div>
              <span className="grid place-items-center w-8 h-8 rounded-full bg-slate-200 dark:bg-slate-700 text-slate-500 shrink-0"><User className="w-4 h-4" /></span>
            </div>
            {/* Answer */}
            <div className="flex items-start gap-2">
              <span className="grid place-items-center w-8 h-8 rounded-full bg-brand-500/15 text-brand-500 shrink-0"><Bot className="w-4 h-4" /></span>
              <div className="max-w-[85%] min-w-0 space-y-2">
                {t.stale && <div className="text-[11px] font-semibold uppercase tracking-wide text-amber-600">Outdated inventory answer</div>}
                {t.loading && <div className="flex items-center gap-2 text-slate-500 text-sm"><Loader2 className="w-4 h-4 animate-spin" /> Querying the database…</div>}
                {t.error && <div className="rounded-2xl bg-red-500/10 text-red-500 px-4 py-2.5 text-sm">{t.error}</div>}
                <BridgetteAnswerStatus evidence={t.evidence} />
                {t.answer && (
                  <div className="rounded-2xl rounded-tl-sm glass border border-slate-200/70 dark:border-slate-700/70 px-4 py-2.5 text-sm text-slate-800 dark:text-slate-100 whitespace-pre-wrap">{t.answer}</div>
                )}
                {t.mapMatches && t.mapMatches.length > 0 && (
                  <div className="grid gap-2" role="list" aria-label="Matching bridges">
                    {t.mapMatches.map((match) => (
                      <button
                        key={match.snapshot_id}
                        role="listitem"
                        onClick={() => ask(`Show bridge ${match.bridge_number} on the map`)}
                        className="rounded-xl border border-brand-300 px-3 py-2 text-left text-sm text-brand-700 hover:bg-brand-50 focus:outline-none focus:ring-2 focus:ring-brand-400"
                        aria-label={`Show bridge ${match.bridge_number}, ${match.label}, inventory year ${match.snapshot_year}`}
                      >
                        <span className="block font-semibold">{match.bridge_number} · {match.label}</span>
                        <span className="block text-xs text-slate-500">{match.county_name ? `${match.county_name} County · ` : ''}Inventory year {match.snapshot_year}</span>
                      </button>
                    ))}
                  </div>
                )}
                {t.decision && Array.isArray(t.evidence?.options) && (
                  <div className="flex flex-wrap gap-2">
                    {(t.evidence.options as BridgetteClarificationOption[]).map((option) => (
                      <button
                        key={String(option.id ?? option.label)}
                        onClick={() => {
                          setInput(clarificationDraft(t.question, option));
                          setTimeout(() => inputRef.current?.focus(), 0);
                        }}
                        className="text-xs px-3 py-1.5 rounded-lg border border-brand-300 text-brand-600 hover:bg-brand-50 dark:hover:bg-brand-500/10"
                      >
                        {String(option.label ?? option.id)}
                      </button>
                    ))}
                  </div>
                )}
                <BridgetteQueryDetails evidence={t.evidence} sql={t.sql} rows={t.rows} columns={t.columns} stale={t.stale} />
              </div>
            </div>
          </div>
        ))}
        <div ref={endRef} />
      </div>

      {/* Composer */}
      <div className="shrink-0 mt-4 flex items-end gap-2">
        <textarea
          ref={inputRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(input); } }}
          rows={1}
          placeholder="Ask Bridgette about New Jersey bridges…"
          className="flex-1 resize-none rounded-2xl border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 px-4 py-3 text-sm text-slate-800 dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-400"
        />
        {busy ? (
          <Button variant="danger" size="lg" icon={<Square className="w-4 h-4" />} onClick={stop}>
            Stop
          </Button>
        ) : (
          <Button variant="primary" size="lg" icon={<Send className="w-4 h-4" />} disabled={!input.trim()} onClick={() => ask(input)}>
            Ask
          </Button>
        )}
      </div>
    </div>
  );
};

export default Bridgette;
