// frontend/src/pages/Bridgette.tsx
// Bridgette — natural-language Q&A over the New Jersey bridge database (text-to-SQL).
import React, { useRef, useState } from 'react';
import axios from 'axios';
import { Bot, Send, Loader2, Database, User, Square } from 'lucide-react';
import { SectionHeader, Button } from '../components/ui';

interface Turn {
  question: string;
  answer?: string;
  sql?: string;
  rows?: Record<string, unknown>[];
  columns?: string[];
  count?: number;
  error?: string;
  loading?: boolean;
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
  const abortRef = useRef<AbortController | null>(null);

  const ask = async (qRaw: string) => {
    const question = qRaw.trim();
    if (!question || busy) return;
    setInput('');
    setBusy(true);
    const idx = turns.length;
    setTurns((t) => [...t, { question, loading: true }]);
    const controller = new AbortController();
    abortRef.current = controller;
    const update = (patch: Partial<Turn>) =>
      setTurns((t) => t.map((turn, i) => (i === idx ? { ...turn, ...patch } : turn)));
    let answer = '';
    try {
      // Stream the answer token-by-token (NDJSON events: meta → token… → done).
      const fd = new FormData();
      fd.append('question', question);
      const res = await fetch('/api/bridgette/stream', { method: 'POST', body: fd, signal: controller.signal });
      if (!res.ok || !res.body) throw new Error(`Request failed (${res.status})`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let nl: number;
        while ((nl = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          let evt: any;
          try { evt = JSON.parse(line); } catch { continue; }
          if (evt.type === 'meta') {
            update({ loading: false, sql: evt.sql, rows: evt.rows, columns: evt.columns, count: evt.count });
          } else if (evt.type === 'token') {
            answer += evt.text;
            update({ loading: false, answer });
          } else if (evt.type === 'stopped') {
            update({ loading: false, answer: answer || 'Stopped.' });
          } else if (evt.type === 'error') {
            update({ loading: false, error: evt.detail || 'Request failed' });
          }
        }
      }
    } catch (err: any) {
      const stopped = err?.name === 'AbortError' || controller.signal.aborted;
      if (stopped) update({ loading: false, answer: answer || 'Stopped.' });
      else update({ loading: false, error: err?.message || 'Request failed' });
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setBusy(false);
      setTimeout(() => endRef.current?.scrollIntoView({ behavior: 'smooth' }), 50);
    }
  };

  // Stop both the client request AND the server-side LLM generation.
  const stop = () => {
    abortRef.current?.abort();
    axios.post('/api/ask-cancel').catch(() => { /* best-effort */ });
  };

  return (
    <div className="flex flex-col h-full min-h-[480px]">
      <SectionHeader
        icon={<Bot className="w-6 h-6" />}
        title="Bridgette"
        subtitle="Ask anything about New Jersey bridges — answered live from the database"
      />

      <div className="flex-1 overflow-auto ui-scroll space-y-5 pr-1">
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
                {t.loading && <div className="flex items-center gap-2 text-slate-500 text-sm"><Loader2 className="w-4 h-4 animate-spin" /> Querying the database…</div>}
                {t.error && <div className="rounded-2xl bg-red-500/10 text-red-500 px-4 py-2.5 text-sm">{t.error}</div>}
                {t.answer && (
                  <div className="rounded-2xl rounded-tl-sm glass border border-slate-200/70 dark:border-slate-700/70 px-4 py-2.5 text-sm text-slate-800 dark:text-slate-100 whitespace-pre-wrap">{t.answer}</div>
                )}
                {t.sql && (
                  <details className="text-xs">
                    <summary className="cursor-pointer text-slate-400 hover:text-brand-500 inline-flex items-center gap-1"><Database className="w-3.5 h-3.5" /> SQL{typeof t.count === 'number' ? ` · ${t.count} row(s)` : ''}</summary>
                    <pre className="mt-1 p-3 rounded-xl bg-slate-900 text-slate-200 overflow-auto"><code>{t.sql}</code></pre>
                    {t.rows && t.rows.length > 0 && t.columns && (
                      <div className="mt-2 overflow-auto max-h-64 rounded-xl border border-slate-200/70 dark:border-slate-700/70">
                        <table className="text-xs w-full">
                          <thead className="bg-slate-100 dark:bg-slate-800 sticky top-0">
                            <tr>{t.columns.map((c) => <th key={c} className="px-2.5 py-1.5 text-left font-semibold text-slate-600 dark:text-slate-300">{c}</th>)}</tr>
                          </thead>
                          <tbody>
                            {t.rows.slice(0, 100).map((r, ri) => (
                              <tr key={ri} className="border-t border-slate-200/60 dark:border-slate-700/60">
                                {t.columns!.map((c) => <td key={c} className="px-2.5 py-1.5 text-slate-700 dark:text-slate-300">{String(r[c] ?? '')}</td>)}
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </details>
                )}
              </div>
            </div>
          </div>
        ))}
        <div ref={endRef} />
      </div>

      {/* Composer */}
      <div className="shrink-0 mt-4 flex items-end gap-2">
        <textarea
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
