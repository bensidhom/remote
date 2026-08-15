// frontend/src/components/BridgeDetailModal.tsx
// Full-screen per-bridge popup: one place for editing, files,
// history+forecast, life-cycle cost, and a bridge-scoped RAG chat.
import React, { Suspense, lazy, useState, useEffect, useRef } from 'react';
import axios from 'axios';
import { Edit3, FileText, Box, History, Coins, Bot, Send, Loader2, Square, Table2, Plus, Trash2, ScanSearch, ZoomIn, ZoomOut, X } from 'lucide-react';
import type { PopupBridgeRef } from './ArcgisMap';
import { Modal, Button } from './ui';
import BridgeHistoryModal from './BridgeHistoryModal';
import BridgeReportsModal from './BridgeReportsModal';
import BridgeLCCAModal from './BridgeLCCAModal';
import Editor from '../pages/Editor';

const PointCloud = lazy(() => import('../pages/PointCloud'));

type Tab = 'history' | 'files' | 'pointcloud' | 'lcca' | 'rag' | 'custom' | 'edit';
type ReportImage = { data: string; mime: string; label?: string; filename?: string; detections?: { label?: string; confidence?: number; bbox?: number[] }[] };
type PrivateSession = { id: string; title: string };
type PrivateTurn = {
  role: 'user' | 'assistant';
  content: string;
  cited_revision?: string | null;
  provenance_status: 'user_input' | 'unverified_model_output' | 'verified_answer';
};
type AskEvidence = {
  analysis_mode?: 'text' | 'visual';
  visual_processing_ran?: boolean;
  model_id?: string;
  inspected_count?: number;
  estimated_cost_usd?: number;
  privacy?: string;
  evidence_receipt_sha256?: string;
  provenance_status?: 'unverified_model_output' | 'verified_answer';
  cited_revision?: string;
};
const TABS: { id: Tab; label: string; Icon: typeof History }[] = [
  { id: 'history', label: 'History', Icon: History },
  { id: 'files', label: 'Files', Icon: FileText },
  { id: 'pointcloud', label: 'Point Cloud', Icon: Box },
  { id: 'lcca', label: 'LCCA', Icon: Coins },
  { id: 'rag', label: 'Ask', Icon: Bot },
  { id: 'custom', label: 'Custom', Icon: Table2 },
  { id: 'edit', label: 'Edit', Icon: Edit3 },
];

const BridgeDetailModal: React.FC<{ bridge: PopupBridgeRef; onClose: () => void }> = ({ bridge, onClose }) => {
  const [tab, setTab] = useState<Tab>('history');
  const noop = () => {};
  const sc = bridge.state_code;
  const bn = bridge.bridge_number;

  return (
    <Modal open onClose={onClose} size="full" padded={false} title={`Bridge ${bn}`} icon={<Box className="w-5 h-5" />}>
      <div className="flex flex-col h-full">
        <p className="px-6 pt-1 pb-3 text-sm text-slate-500 dark:text-slate-400">
          {bridge.bridge_name ? `${bridge.bridge_name} · ` : ''}New Jersey · ID {bn}
        </p>

        {/* Tab bar */}
        <div className="px-4 flex gap-1 border-b border-slate-200/70 dark:border-slate-700/70 overflow-x-auto">
          {TABS.map(({ id, label, Icon }) => (
            <button
              key={id}
              onClick={() => setTab(id)}
              className={`inline-flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 -mb-px transition-colors whitespace-nowrap ${
                tab === id
                  ? 'border-brand-500 text-brand-600 dark:text-brand-300'
                  : 'border-transparent text-slate-500 hover:text-slate-800 dark:hover:text-slate-200'
              }`}
            >
              <Icon className="w-4 h-4" /> {label}
            </button>
          ))}
        </div>

        {/* Tab body */}
        <div className="flex-1 overflow-auto ui-scroll p-4">
          {tab === 'history' && <BridgeHistoryModal embedded stateCode={sc} bridgeNumber={bn} bridgeName={bridge.bridge_name} dataYear={bridge.data_year} onClose={noop} />}
          {tab === 'files' && <BridgeReportsModal embedded stateCode={sc} bridgeNumber={bn} bridgeName={bridge.bridge_name} onClose={noop} />}
          {tab === 'pointcloud' && (
            <div className="-m-4 h-[calc(100%+2rem)] min-h-[620px]">
              <Suspense fallback={<div className="p-8 text-center text-slate-500">Loading point-cloud viewer…</div>}>
                <PointCloud bridgeStateCode={sc} bridgeNumber={bn} bridgeName={bridge.bridge_name} dataYear={bridge.data_year} />
              </Suspense>
            </div>
          )}
          {tab === 'lcca' && (
            <BridgeLCCAModal
              embedded
              stateCode={sc}
              bridgeName={bridge.bridge_name}
              bridgeNumber={bn}
              yearBuilt={bridge.year_built}
              dataYear={bridge.data_year}
              onClose={noop}
            />
          )}
          {tab === 'rag' && (
            <BridgeRagTab
              stateCode={sc}
              bridgeNumber={bn}
              dataYear={bridge.data_year}
              snapshotRevisionId={bridge.snapshot_revision_id}
            />
          )}
          {tab === 'custom' && <BridgeCustomTablesTab stateCode={sc} bridgeNumber={bn} />}
          {tab === 'edit' && (
            <div className="space-y-5">
              <Editor initialStateCode={Number(sc)} initialBridgeNumber={bn} />
              <DeleteRecordControl sc={sc} bn={bn} onDeleted={onClose} />
            </div>
          )}
        </div>
      </div>
    </Modal>
  );
};

const ImageZoomModal: React.FC<{ image: ReportImage | null; onClose: () => void }> = ({ image, onClose }) => {
  const [zoom, setZoom] = useState(1);
  useEffect(() => { setZoom(1); }, [image]);
  if (!image) return null;
  return (
    <div className="fixed inset-0 z-[80] bg-slate-950/85 flex flex-col" onClick={onClose}>
      <div className="shrink-0 flex items-center justify-between gap-3 px-4 py-3 text-white">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold">{image.label || image.filename || 'Report image'}</div>
          {image.filename && <div className="truncate text-xs text-slate-300">{image.filename}</div>}
        </div>
        <div className="flex items-center gap-1.5" onClick={(e) => e.stopPropagation()}>
          <button type="button" onClick={() => setZoom((z) => Math.max(0.5, Number((z - 0.25).toFixed(2))))} className="p-2 rounded-lg hover:bg-white/10" title="Zoom out">
            <ZoomOut className="w-4 h-4" />
          </button>
          <span className="w-14 text-center text-xs tabular-nums">{Math.round(zoom * 100)}%</span>
          <button type="button" onClick={() => setZoom((z) => Math.min(4, Number((z + 0.25).toFixed(2))))} className="p-2 rounded-lg hover:bg-white/10" title="Zoom in">
            <ZoomIn className="w-4 h-4" />
          </button>
          <button type="button" onClick={onClose} className="p-2 rounded-lg hover:bg-white/10" title="Close">
            <X className="w-4 h-4" />
          </button>
        </div>
      </div>
      <div className="flex-1 overflow-auto p-4" onClick={(e) => e.stopPropagation()}>
        <div className="min-h-full flex items-center justify-center">
          <img
            src={`data:${image.mime};base64,${image.data}`}
            alt={image.label || image.filename || 'Report image'}
            style={{ transform: `scale(${zoom})`, transformOrigin: 'center center' }}
            className="max-w-full max-h-[78vh] object-contain transition-transform"
          />
        </div>
      </div>
    </div>
  );
};

/** Bridge-scoped RAG chat (reuses /api/db-rag with bridge context). */
const BridgeRagTab: React.FC<{
  stateCode: string | number;
  bridgeNumber: string;
  dataYear?: string | number;
  snapshotRevisionId?: string | null;
}> = ({ stateCode, bridgeNumber, dataYear, snapshotRevisionId }) => {
  const [q, setQ] = useState('');
  const [history, setHistory] = useState<{ q: string; a?: string; sources?: string[]; images?: ReportImage[]; loading?: boolean; evidence?: AskEvidence }[]>([]);
  const [selectedImage, setSelectedImage] = useState<ReportImage | null>(null);
  const [allSources, setAllSources] = useState<string[]>(['Database']);
  const [selected, setSelected] = useState<Set<string>>(new Set(['Database']));
  const [useVisual, setUseVisual] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const activeRequestIdRef = useRef<string | null>(null);
  const activeVisualJobIdRef = useRef<string | null>(null);
  const [sessions, setSessions] = useState<PrivateSession[]>([]);
  const [sessionId, setSessionId] = useState('');
  // Real busy state held for the WHOLE request — not derived from per-turn
  // `loading`, which clears on the first streamed token (so Stop would vanish
  // mid-answer for typed/text questions).
  const [busy, setBusy] = useState(false);
  // Report-index readiness so the user can SEE a file is parsed+embedded (ready to
  // query) vs still indexing — opening this tab also warms the index in the backend.
  const [reportStatus, setReportStatus] = useState<{ ready: boolean; indexing: boolean; file_count: number } | null>(null);

  // Load the bridge's files so the user can choose which sources to query.
  useEffect(() => {
    axios.get(`/api/bridges/${stateCode}/${bridgeNumber}/reports`)
      .then((r) => {
        const files: string[] = (r.data?.reports || []).map((x: any) => x.filename);
        const srcs = ['Database', ...files];
        setAllSources(srcs);
        setSelected(new Set(srcs)); // default: all
      })
      .catch(() => { /* keep just Database */ });
  }, [stateCode, bridgeNumber]);

  useEffect(() => {
    axios.get('/api/ai/sessions')
      .then((response) => {
        const loaded = (response.data?.sessions || []) as PrivateSession[];
        setSessions(loaded);
        if (loaded.length > 0) setSessionId((current) => current || loaded[0].id);
      })
      .catch(() => { /* private ephemeral mode remains available */ });
  }, []);

  useEffect(() => {
    let cancelled = false;
    if (!sessionId) return () => { cancelled = true; };
    axios.get(`/api/ai/sessions/${encodeURIComponent(sessionId)}`)
      .then((response) => {
        if (cancelled) return;
        const turns = (response.data?.turns || []) as PrivateTurn[];
        const restored: { q: string; a?: string; evidence?: AskEvidence }[] = [];
        for (const turn of turns) {
          if (turn.role === 'user') {
            restored.push({ q: turn.content });
          } else {
            const target = [...restored].reverse().find((entry) => entry.a === undefined);
            if (target) {
              target.a = turn.content;
              target.evidence = {
                provenance_status: turn.provenance_status === 'verified_answer'
                  ? 'verified_answer'
                  : 'unverified_model_output',
                cited_revision: turn.cited_revision || undefined,
              };
            }
          }
        }
        setHistory(restored);
      })
      .catch(() => { if (!cancelled) setHistory([]); });
    return () => { cancelled = true; };
  }, [sessionId]);

  const newPrivateSession = async () => {
    try {
      const response = await axios.post('/api/ai/sessions', {
        title: `Bridge ${bridgeNumber}`,
        retention_days: 30,
      });
      const created = response.data?.session as PrivateSession;
      if (created?.id) {
        setSessions((current) => [created, ...current]);
        setSessionId(created.id);
      }
    } catch { /* keep private browser-only history */ }
  };

  // Poll report-index readiness; keep polling while it's still indexing.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let cancelled = false;
    const poll = async () => {
      try {
        const r = await axios.get(`/api/bridges/${stateCode}/${bridgeNumber}/reports-status`);
        if (cancelled) return;
        setReportStatus(r.data);
        if (r.data?.file_count > 0 && !r.data?.ready) timer = setTimeout(poll, 2500);
      } catch { /* ignore */ }
    };
    poll();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [stateCode, bridgeNumber]);

  const toggle = (s: string) => setSelected((prev) => {
    const next = new Set(prev);
    next.has(s) ? next.delete(s) : next.add(s);
    return next;
  });

  const ask = async (override?: string) => {
    const question = (override ?? q).trim();
    if (!question || busy) return;
    if (override === undefined) setQ('');
    setBusy(true);
    const idx = history.length;
    setHistory((h) => [...h, { q: question, loading: true }]);
    const controller = new AbortController();
    abortRef.current = controller;
    const requestId = crypto.randomUUID();
    activeRequestIdRef.current = requestId;
    const requestedVisual = useVisual;
    const update = (patch: Record<string, unknown>) =>
      setHistory((h) => h.map((t, i) => (i === idx ? { ...t, ...patch } : t)));
    let answer = '';
    try {
      if (requestedVisual) {
        const fileSources = allSources.filter((s) => s !== 'Database');
        const selectedFileSources = [...selected].filter((s) => s !== 'Database');
        const fd = new FormData();
        fd.append('query', question);
        fd.append('state_code', String(stateCode));
        fd.append('bridge_number', String(bridgeNumber));
        if (dataYear !== undefined && dataYear !== null) fd.append('data_year', String(dataYear));
        if (snapshotRevisionId) fd.append('snapshot_revision_id', snapshotRevisionId);
        fd.append('analysis_mode', 'visual');
        if (sessionId) fd.append('conversation_id', sessionId);
        if (selectedFileSources.length < fileSources.length) {
          fd.append('selected_sources', JSON.stringify(selectedFileSources));
        }
        const r = await axios.post('/api/bridge-visual-investigation', fd, {
          signal: controller.signal,
          headers: {
            'X-Request-ID': requestId,
            'Idempotency-Key': requestId,
          },
        });
        if (activeRequestIdRef.current !== requestId) return;
        const visualJobId = String(r.data?.job?.job_id || '');
        if (!visualJobId) throw new Error('Visual job was not accepted.');
        activeVisualJobIdRef.current = visualJobId;

        let terminalStatus = '';
        for (let pollCount = 0; pollCount < 1200; pollCount += 1) {
          if (controller.signal.aborted) throw new DOMException('Stopped', 'AbortError');
          const statusResponse = await axios.get(
            `/api/bridge-visual-investigation/jobs/${encodeURIComponent(visualJobId)}`,
            { signal: controller.signal },
          );
          if (activeRequestIdRef.current !== requestId) return;
          const job = statusResponse.data?.job || {};
          terminalStatus = String(job.status || '');
          if (terminalStatus === 'succeeded') break;
          if (terminalStatus === 'cancelled') {
            update({ loading: false, a: 'Stopped.' });
            return;
          }
          if (terminalStatus === 'failed') {
            throw new Error(String(job.error_code || 'Visual investigation failed.'));
          }
          await new Promise<void>((resolve, reject) => {
            const onAbort = () => {
              window.clearTimeout(timer);
              reject(new DOMException('Stopped', 'AbortError'));
            };
            const timer = window.setTimeout(() => {
              controller.signal.removeEventListener('abort', onAbort);
              resolve();
            }, 1000);
            controller.signal.addEventListener('abort', onAbort, { once: true });
          });
        }
        if (terminalStatus !== 'succeeded') {
          throw new Error('Visual investigation timed out while waiting for its durable job.');
        }
        const resultResponse = await axios.get(
          `/api/bridge-visual-investigation/jobs/${encodeURIComponent(visualJobId)}/result`,
          { signal: controller.signal },
        );
        if (
          activeRequestIdRef.current !== requestId
          || resultResponse.data?.request_id !== requestId
        ) return;
        const d = resultResponse.data || {};
        const visual = d.visual_evidence || {};
        update({
          loading: false,
          a: d.answer || 'Visual investigation complete.',
          sources: d.detections?.map((x: any) => x.source).filter(Boolean) || [],
          images: d.images || [],
          evidence: {
            analysis_mode: 'visual',
            visual_processing_ran: Boolean(visual.visual_processing_ran),
            provenance_status: sessionId ? 'unverified_model_output' : undefined,
            cited_revision: d.bridge_snapshot?.identity?.snapshot_revision_id,
            model_id: visual.model_id,
            inspected_count: Number(visual.usage?.actual_images_inspected || 0),
            estimated_cost_usd: Number(visual.usage?.estimated_cost_usd || 0),
            evidence_receipt_sha256: d.evidence_receipt_sha256,
            privacy: visual.privacy?.raw_report_images_may_be_sent_to_configured_model
              ? 'Raw report images may be sent to the configured model.'
              : undefined,
          },
        });
        return;
      }

      const fd = new FormData();
      fd.append('question', question);
      fd.append('state_code', String(stateCode));
      fd.append('bridge_number', String(bridgeNumber));
      if (dataYear !== undefined && dataYear !== null) fd.append('data_year', String(dataYear));
      if (snapshotRevisionId) fd.append('snapshot_revision_id', snapshotRevisionId);
      fd.append('analysis_mode', 'text');
      if (sessionId) fd.append('conversation_id', sessionId);
      // Only send a filter when a strict subset is chosen (so "all" keeps embedding ranking).
      if (selected.size < allSources.length) fd.append('selected_sources', JSON.stringify([...selected]));
      // Stream the answer (NDJSON: meta → token… → done; image routes send 'final').
      const res = await fetch('/api/db-rag/stream', {
        method: 'POST', body: fd, signal: controller.signal,
        headers: { 'X-Request-ID': requestId },
      });
      if (!res.ok || !res.body) throw new Error(`Request failed (${res.status})`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      let citedRevision: string | undefined;
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
          if (activeRequestIdRef.current !== requestId || evt.request_id !== requestId) continue;
          if (evt.type === 'accepted') {
            citedRevision = evt.bridge_snapshot?.identity?.snapshot_revision_id;
            continue;
          } else if (evt.type === 'evidence') {
            update({
              loading: false,
              sources: evt.reports_used || [],
              images: evt.images || [],
              evidence: sessionId ? {
                analysis_mode: 'text',
                provenance_status: 'unverified_model_output',
                cited_revision: citedRevision,
              } : undefined,
            });
          } else if (evt.type === 'token') {
            answer += evt.text;
            update({ loading: false, a: answer });
          } else if (evt.type === 'final') {
            const d = evt.response || {};
            const a = d.answer ?? (typeof d === 'string' ? d : JSON.stringify(d));
            update({ loading: false, a, sources: d.reports_used || [], images: d.images || [] });
          } else if (evt.type === 'stopped') {
            update({ loading: false, a: answer || 'Stopped.' });
          } else if (evt.type === 'error') {
            update({ loading: false, a: `Error: ${evt.detail || 'failed'}` });
          }
        }
      }
    } catch (err: any) {
      const stopped = err?.name === 'AbortError' || controller.signal.aborted;
      update({ loading: false, a: stopped ? (answer || 'Stopped.') : `Error: ${err?.message || 'failed'}` });
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      if (activeRequestIdRef.current === requestId) activeRequestIdRef.current = null;
      activeVisualJobIdRef.current = null;
      if (requestedVisual) setUseVisual(false);
      setBusy(false);
    }
  };

  // Stop both the client request AND the server-side LLM generation.
  const stop = () => {
    const visualJobId = activeVisualJobIdRef.current;
    abortRef.current?.abort();
    const requestId = activeRequestIdRef.current;
    if (visualJobId) {
      axios.post(
        `/api/bridge-visual-investigation/jobs/${encodeURIComponent(visualJobId)}/cancel`,
      ).catch(() => { /* best-effort durable cancellation */ });
    } else if (requestId) {
      axios.post(`/api/ai/requests/${encodeURIComponent(requestId)}/cancel`).catch(() => { /* best-effort */ });
    }
  };

  return (
    <div className="flex flex-col h-full max-w-3xl mx-auto">
      {/* Source selector — Database + each uploaded file */}
      <div className="shrink-0 mb-3 rounded-xl border border-slate-200/70 dark:border-slate-700/70 p-3">
        <div className="flex items-center justify-between mb-1.5">
          <div className="flex items-center gap-2">
            <span className="text-xs font-semibold text-slate-500 dark:text-slate-400">Query sources</span>
            {reportStatus && reportStatus.file_count > 0 && (
              reportStatus.ready ? (
                <span className="inline-flex items-center gap-1 text-[10px] font-semibold px-1.5 py-0.5 rounded-full bg-emerald-500/10 text-emerald-600 dark:text-emerald-400">
                  ✓ reports ready
                </span>
              ) : (
                <span className="inline-flex items-center gap-1 text-[10px] font-semibold px-1.5 py-0.5 rounded-full bg-amber-500/10 text-amber-600 dark:text-amber-400">
                  <span className="w-2 h-2 rounded-full border-2 border-amber-500 border-t-transparent animate-spin" />
                  indexing reports…
                </span>
              )
            )}
          </div>
          <div className="flex gap-2 text-[11px]">
            <button onClick={() => setSelected(new Set(allSources))} className="text-brand-500 hover:underline">All</button>
            <button onClick={() => setSelected(new Set())} className="text-slate-400 hover:underline">None</button>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          {allSources.map((s) => (
            <label key={s} className={`inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-lg border cursor-pointer transition-colors ${selected.has(s) ? 'border-brand-400 bg-brand-500/10 text-brand-600 dark:text-brand-300' : 'border-slate-300 dark:border-slate-700 text-slate-500'}`}>
              <input type="checkbox" checked={selected.has(s)} onChange={() => toggle(s)} className="accent-brand-500 w-3.5 h-3.5" />
              {s === 'Database' ? 'Bridge database' : s}
            </label>
          ))}
        </div>
      </div>

      <div className="shrink-0 mb-3 rounded-xl border border-slate-200/70 dark:border-slate-700/70 p-3">
        <div className="flex flex-wrap items-center gap-2 mb-3">
          <label className="text-xs font-semibold text-slate-500" htmlFor="bridge-private-session">Private session</label>
          <select id="bridge-private-session" value={sessionId} onChange={(event) => setSessionId(event.target.value)} className="min-w-48 rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs">
            <option value="">Browser-only (ephemeral)</option>
            {sessions.map((session) => <option key={session.id} value={session.id}>{session.title}</option>)}
          </select>
          <button type="button" onClick={newPrivateSession} className="text-xs text-brand-600 hover:underline">New private session</button>
        </div>
        {sessionId && (
          <p className="mb-3 text-[11px] text-slate-500">
            Saved privately for 30 days. Ordinary model answers are explicitly labeled unverified; the cited published revision and evidence receipt are retained when available.
          </p>
        )}
        <div className="flex flex-wrap items-center gap-3">
          <label className={`inline-flex items-center gap-2 text-sm text-slate-600 dark:text-slate-300 ${busy ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'}`}>
            <input type="checkbox" checked={useVisual} disabled={busy} onChange={(e) => setUseVisual(e.target.checked)} className="accent-brand-500 w-4 h-4" />
            <ScanSearch className="w-4 h-4" />
            Use visual investigation
          </label>
          <span className="text-xs text-slate-500">
            {useVisual
              ? 'One request only: up to 8 likely images; raw images may be sent to the configured model; estimated cap $0.25.'
              : 'Unchecked: uses database and report text only.'}
          </span>
        </div>
      </div>

      <div className="flex-1 overflow-auto ui-scroll space-y-3">
        {history.length === 0 && <p className="text-sm text-slate-500 dark:text-slate-400">Ask about this bridge, its database record, and report text or tables. To inspect cracks or other visible defects, check Use visual investigation first. You can still request “show all images” or “show page 3” without running an inspection.</p>}
        {history.map((t, i) => (
          <div key={i} className="space-y-1.5">
            <div className="text-right"><span className="inline-block rounded-2xl bg-brand-600 text-white px-3.5 py-2 text-sm">{t.q}</span></div>
            <div>{t.loading ? <span className="inline-flex items-center gap-2 text-slate-500 text-sm"><Loader2 className="w-4 h-4 animate-spin" /> Thinking…</span> : <span className="inline-block rounded-2xl glass border border-slate-200/70 dark:border-slate-700/70 px-3.5 py-2 text-sm whitespace-pre-wrap">{t.a}</span>}</div>
            {t.images && t.images.length > 0 && (
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 mt-1">
                {t.images.map((img, k) => (
                  <figure key={k} onClick={() => setSelectedImage(img)} className="rounded-lg overflow-hidden border border-slate-200/70 dark:border-slate-700/70 bg-slate-50 dark:bg-slate-800/50 cursor-zoom-in">
                    <img src={`data:${img.mime};base64,${img.data}`} alt={img.label || img.filename || `image ${k + 1}`} className="w-full h-28 object-cover" loading="lazy" />
                    {img.label && <figcaption className="px-1.5 py-1 text-[10px] text-slate-500 dark:text-slate-400 truncate" title={img.label}>{img.label}</figcaption>}
                  </figure>
                ))}
              </div>
            )}
            {t.sources && t.sources.length > 0 && (
              <div className="flex items-center gap-1.5 flex-wrap text-[11px] text-slate-400 mt-1">
                <FileText className="w-3 h-3" /> from:
                {t.sources.map((s) => <span key={s} className="px-1.5 py-0.5 rounded bg-slate-500/10">{s}</span>)}
              </div>
            )}
            {t.evidence && (
              <div className="mt-1 text-[11px] text-slate-500">
                {t.evidence.provenance_status === 'verified_answer'
                  ? 'Verified answer'
                  : t.evidence.provenance_status === 'unverified_model_output'
                    ? 'Unverified model output'
                    : t.evidence.analysis_mode === 'visual' ? 'Visual evidence' : 'Text evidence'}
                {t.evidence.cited_revision ? ` Â· revision ${t.evidence.cited_revision}` : ''}
                {typeof t.evidence.inspected_count === 'number' ? ` · ${t.evidence.inspected_count} image(s) inspected` : ''}
                {t.evidence.model_id ? ` · ${t.evidence.model_id}` : ''}
                {typeof t.evidence.estimated_cost_usd === 'number' ? ` · estimated $${t.evidence.estimated_cost_usd.toFixed(4)}` : ''}
                {t.evidence.privacy ? ` · ${t.evidence.privacy}` : ''}
              </div>
            )}
          </div>
        ))}
      </div>
      <div className="flex flex-wrap gap-1.5 mt-2">
        {['Show all images', 'Show all pages', 'Count images', 'Summarize the reports'].map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => ask(s)}
            disabled={busy}
            className="text-[11px] px-2 py-1 rounded-full border border-slate-300 dark:border-slate-700 text-slate-500 hover:border-brand-400 hover:text-brand-600 dark:hover:text-brand-300 transition-colors disabled:opacity-50"
          >
            {s}
          </button>
        ))}
      </div>
      <div className="flex items-end gap-2 mt-3">
        <textarea value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(); } }} rows={1} placeholder="Ask about this bridge…" className="flex-1 resize-none rounded-2xl border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-brand-400" />
        {busy ? (
          <Button variant="danger" icon={<Square className="w-4 h-4" />} onClick={stop}>Stop</Button>
        ) : (
          <Button variant="primary" icon={<Send className="w-4 h-4" />} onClick={() => ask()} disabled={!q.trim()}>Ask</Button>
        )}
      </div>
      <ImageZoomModal image={selectedImage} onClose={() => setSelectedImage(null)} />
    </div>
  );
};

/** Custom (non-SNBI) tables — user-defined name/columns/rows, colour-coded so they
 *  never look like SNBI data. Scope is set at creation: this bridge, or all bridges
 *  in the state (shared structure, per-bridge rows). Stored separately and excluded
 *  from SNBI load/download. */
interface CustomTableT { id: number; name: string; columns: string[]; color: string; scope: string; rows: { id: number; cells: Record<string, string> }[] }
const CT_COLORS = ['#8b5cf6', '#f59e0b', '#10b981', '#ef4444', '#0ea5e9', '#ec4899', '#14b8a6', '#6366f1'];

const BridgeCustomTablesTab: React.FC<{ stateCode: string | number; bridgeNumber: string }> = ({ stateCode, bridgeNumber }) => {
  const [tables, setTables] = useState<CustomTableT[]>([]);
  const [loading, setLoading] = useState(true);
  const [showAdd, setShowAdd] = useState(false);
  const [name, setName] = useState('');
  const [cols, setCols] = useState<string[]>(['', '']);
  const [color, setColor] = useState(CT_COLORS[0]);
  const [scope, setScope] = useState<'bridge' | 'state'>('bridge');
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const errMsg = (e: any, fallback: string) =>
    (e?.response?.data?.detail && String(e.response.data.detail)) || fallback;

  const load = async () => {
    setLoading(true);
    try {
      const r = await axios.get(`/api/bridges/${stateCode}/${bridgeNumber}/custom-tables`);
      setTables(r.data?.tables || []);
    } catch { /* keep */ } finally { setLoading(false); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [stateCode, bridgeNumber]);

  const createTable = async () => {
    const columns = cols.map((c) => c.trim()).filter(Boolean);
    if (!name.trim() || columns.length === 0) return;
    setSaving(true); setErr(null);
    try {
      await axios.post('/api/custom-tables', {
        state_code: Number(stateCode), bridge_number: String(bridgeNumber),
        name: name.trim(), columns, color, scope,
      });
      await load();
      // Reset the form only after a confirmed save, so a failure never
      // silently discards the title the user just typed.
      setShowAdd(false); setName(''); setCols(['', '']); setColor(CT_COLORS[0]); setScope('bridge');
    } catch (e) {
      setErr(errMsg(e, 'Could not create the table — your title was kept, please try again.'));
    } finally { setSaving(false); }
  };

  const addRow = async (t: CustomTableT) => {
    const cells: Record<string, string> = {}; t.columns.forEach((c) => { cells[c] = ''; });
    setErr(null);
    try {
      await axios.post(`/api/custom-tables/${t.id}/rows`, { bridge_number: String(bridgeNumber), cells });
      await load();
    } catch (e) { setErr(errMsg(e, 'Could not add a row.')); }
  };
  const saveCell = (rowId: number, cells: Record<string, string>) =>
    axios.put(`/api/custom-tables/rows/${rowId}`, { cells })
      .catch((e) => setErr(errMsg(e, 'A cell edit could not be saved.')));
  const deleteRow = async (rowId: number) => {
    setErr(null);
    try { await axios.delete(`/api/custom-tables/rows/${rowId}`); await load(); }
    catch (e) { setErr(errMsg(e, 'Could not delete the row.')); }
  };
  const deleteTable = async (id: number) => {
    if (!window.confirm('Delete this table and all of its rows?')) return;
    setErr(null);
    try { await axios.delete(`/api/custom-tables/${id}`); await load(); }
    catch (e) { setErr(errMsg(e, 'Could not delete the table.')); }
  };
  const onCell = (ti: number, ri: number, col: string, val: string) =>
    setTables((prev) => prev.map((t, i) => i !== ti ? t : { ...t, rows: t.rows.map((r, j) => j !== ri ? r : { ...r, cells: { ...r.cells, [col]: val } }) }));

  return (
    <div className="max-w-4xl mx-auto space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-slate-700 dark:text-slate-200">Custom tables</h3>
          <p className="text-xs text-slate-400">Your own tables — kept separate from SNBI data (not exported/imported).</p>
        </div>
        {!showAdd && (
          <Button variant="primary" icon={<Plus className="w-4 h-4" />} onClick={() => { setErr(null); setShowAdd(true); }}>Add table</Button>
        )}
      </div>

      {err && (
        <div className="rounded-lg border border-red-300 dark:border-red-800 bg-red-50 dark:bg-red-900/30 px-3 py-2 text-sm text-red-700 dark:text-red-300">
          {err}
        </div>
      )}

      {showAdd && (
        <div className="rounded-xl border border-slate-200 dark:border-slate-700 p-4 space-y-3">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Table name (e.g. Extra Notes)"
            className="w-full rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-2 text-sm" />
          <div className="space-y-1.5">
            <span className="text-xs font-semibold text-slate-500">Columns</span>
            {cols.map((c, i) => (
              <div key={i} className="flex gap-2">
                <input value={c} onChange={(e) => setCols((p) => p.map((x, j) => j === i ? e.target.value : x))}
                  placeholder={`Column ${i + 1}`} className="flex-1 rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-1.5 text-sm" />
                {cols.length > 1 && <button onClick={() => setCols((p) => p.filter((_, j) => j !== i))} className="text-slate-400 hover:text-red-500"><Trash2 className="w-4 h-4" /></button>}
              </div>
            ))}
            <button onClick={() => setCols((p) => [...p, ''])} className="text-xs text-brand-500 hover:underline inline-flex items-center gap-1"><Plus className="w-3 h-3" /> add column</button>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs font-semibold text-slate-500">Colour</span>
            {CT_COLORS.map((c) => (
              <button key={c} onClick={() => setColor(c)} style={{ background: c }}
                className={`w-5 h-5 rounded-full ${color === c ? 'ring-2 ring-offset-1 ring-slate-400' : ''}`} />
            ))}
          </div>
          <div className="flex items-center gap-4 text-sm">
            <span className="text-xs font-semibold text-slate-500">Apply to</span>
            <label className="inline-flex items-center gap-1.5 cursor-pointer"><input type="radio" checked={scope === 'bridge'} onChange={() => setScope('bridge')} /> This bridge</label>
            <label className="inline-flex items-center gap-1.5 cursor-pointer"><input type="radio" checked={scope === 'state'} onChange={() => setScope('state')} /> All bridges in the state</label>
          </div>
          <div className="flex gap-2 justify-end pt-1">
            <Button variant="secondary" onClick={() => setShowAdd(false)}>Cancel</Button>
            <Button variant="primary" onClick={createTable} disabled={saving || !name.trim() || cols.every((c) => !c.trim())}>{saving ? 'Creating…' : 'Create table'}</Button>
          </div>
        </div>
      )}

      {loading ? (
        <p className="text-sm text-slate-400">Loading…</p>
      ) : tables.length === 0 && !showAdd ? (
        <p className="text-sm text-slate-400">No custom tables yet. Click "Add table" to create one.</p>
      ) : (
        tables.map((t, ti) => (
          <div key={t.id} className="rounded-xl border bg-white dark:bg-slate-900 overflow-hidden"
            style={{ borderColor: t.color + '66', borderLeft: `4px solid ${t.color}` }}>
            <div className="flex items-center justify-between px-3 py-2" style={{ background: t.color + '14' }}>
              <div className="flex items-center gap-2">
                <span className="w-2.5 h-2.5 rounded-full" style={{ background: t.color }} />
                <span className="text-sm font-semibold" style={{ color: t.color }}>{t.name}</span>
                {t.scope === 'state' && <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-slate-500/10 text-slate-500">state-wide</span>}
              </div>
              <button onClick={() => deleteTable(t.id)} className="text-slate-400 hover:text-red-500" title="Delete table"><Trash2 className="w-4 h-4" /></button>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs text-slate-500 border-b border-slate-200 dark:border-slate-700">
                    {t.columns.map((c) => <th key={c} className="px-3 py-1.5 font-semibold">{c}</th>)}
                    <th className="px-2 w-8" />
                  </tr>
                </thead>
                <tbody>
                  {t.rows.map((r, ri) => (
                    <tr key={r.id} className="border-b border-slate-100 dark:border-slate-800">
                      {t.columns.map((c) => (
                        <td key={c} className="px-2 py-1">
                          <input value={r.cells[c] ?? ''}
                            onChange={(e) => onCell(ti, ri, c, e.target.value)}
                            onBlur={() => saveCell(r.id, { ...r.cells })}
                            className="w-full rounded px-2 py-1 text-sm bg-transparent focus:bg-slate-50 dark:focus:bg-slate-800 focus:outline-none" />
                        </td>
                      ))}
                      <td className="px-2 text-center">
                        <button onClick={() => deleteRow(r.id)} className="text-slate-300 hover:text-red-500"><Trash2 className="w-3.5 h-3.5" /></button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <button onClick={() => addRow(t)} className="text-xs text-brand-500 hover:underline inline-flex items-center gap-1 px-3 py-2"><Plus className="w-3 h-3" /> add row</button>
          </div>
        ))
      )}
    </div>
  );
};

/** Danger zone: delete a single bridge's snapshot for one year (SNBI data only). */
const DeleteRecordControl: React.FC<{ sc: string | number; bn: string; onDeleted: () => void }> = ({ sc, bn, onDeleted }) => {
  const [years, setYears] = useState<number[]>([]);
  const [year, setYear] = useState<number | ''>('');
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  useEffect(() => {
    axios.get(`/api/bridges/${sc}/${bn}/years`).then((r) => {
      const ys: number[] = r.data?.years || [];
      setYears(ys); if (ys.length) setYear(ys[0]);
    }).catch(() => {});
  }, [sc, bn]);
  const del = async () => {
    if (year === '') return;
    if (!window.confirm(`Delete bridge ${bn}'s ${year} record? This removes that year's SNBI snapshot (custom tables and files are kept).`)) return;
    setBusy(true);
    try {
      const r = await axios.delete(`/api/bridges/${sc}/${bn}/year/${year}`);
      setMsg(`Deleted ${r.data?.deleted ?? 0} record(s) for ${year}.`);
      setTimeout(onDeleted, 1200);
    } catch (e: any) {
      setMsg(e?.response?.data?.detail || 'Delete failed.');
    } finally { setBusy(false); }
  };
  return (
    <div className="rounded-xl border border-red-200 dark:border-red-900/50 bg-red-50/40 dark:bg-red-950/20 p-4">
      <h4 className="text-sm font-semibold text-red-600 dark:text-red-400">Delete a year's record</h4>
      <p className="text-xs text-slate-500 mt-0.5 mb-2">Removes this bridge's SNBI snapshot for one data year. Custom tables and uploaded files are kept.</p>
      <div className="flex items-center gap-2">
        <select value={year} onChange={(e) => setYear(e.target.value ? Number(e.target.value) : '')}
          className="rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-1.5 text-sm">
          {years.length === 0 && <option value="">No years</option>}
          {years.map((y) => <option key={y} value={y}>{y}</option>)}
        </select>
        <Button variant="secondary" icon={<Trash2 className="w-4 h-4" />} onClick={del} disabled={busy || year === ''}>
          {busy ? 'Deleting…' : 'Delete record'}
        </Button>
        {msg && <span className="text-xs text-slate-500">{msg}</span>}
      </div>
    </div>
  );
};

export default BridgeDetailModal;
