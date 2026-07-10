// Compact, always-available Bridgette chat that lives in the left rail's empty
// space (below the tool list). Streams answers from /api/bridgette/stream and
// persists for the session because HomeSidebar is always mounted. Hidden on the
// narrow icon-only rail (no room) — shown only on the wide (lg) sidebar.
import React, { useRef, useState } from 'react';
import { Bot, Send, Square, Loader2 } from 'lucide-react';

interface Turn { q: string; a?: string; error?: string; loading?: boolean }

const SUGGESTIONS = ['Bridges per county', 'Avg deck rating each year', 'Bridges built before 1970'];

const SidebarBridgette: React.FC = () => {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  const ask = async (qRaw?: string) => {
    const question = (qRaw ?? input).trim();
    if (!question || busy) return;
    setInput('');
    setBusy(true);
    const idx = turns.length;
    setTurns((t) => [...t, { q: question, loading: true }]);
    const controller = new AbortController();
    abortRef.current = controller;
    const update = (patch: Partial<Turn>) => setTurns((t) => t.map((x, i) => (i === idx ? { ...x, ...patch } : x)));
    let answer = '';
    try {
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
          if (evt.type === 'token') { answer += evt.text; update({ loading: false, a: answer }); }
          else if (evt.type === 'stopped') { update({ loading: false, a: answer || 'Stopped.' }); }
          else if (evt.type === 'error') { update({ loading: false, error: evt.detail || 'Request failed' }); }
        }
      }
    } catch (err: any) {
      const stopped = err?.name === 'AbortError' || controller.signal.aborted;
      if (stopped) update({ loading: false, a: answer || 'Stopped.' });
      else update({ loading: false, error: err?.message || 'Request failed' });
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setBusy(false);
      setTimeout(() => endRef.current?.scrollIntoView({ behavior: 'smooth' }), 50);
    }
  };

  const stop = () => {
    abortRef.current?.abort();
    fetch('/api/ask-cancel', { method: 'POST' }).catch(() => { /* best-effort */ });
  };

  return (
    <div className="hidden lg:flex flex-1 min-h-0 flex-col border-t border-slate-200/80">
      <div className="flex items-center gap-1.5 px-3 pt-2.5 pb-1 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
        <Bot className="w-3.5 h-3.5" /> Ask Bridgette
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto ui-scroll px-3 pb-1 space-y-2">
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
            {t.loading && !t.a && (
              <div className="flex items-center gap-1.5 text-slate-400 text-[12px]"><Loader2 className="w-3 h-3 animate-spin" /> thinking…</div>
            )}
            {t.a && (
              <div className="rounded-lg rounded-tl-sm bg-slate-100 text-slate-700 px-2.5 py-1.5 text-[12px] leading-snug whitespace-pre-wrap break-words">{t.a}</div>
            )}
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
