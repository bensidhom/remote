// frontend/src/components/BridgeReportsModal.tsx
import React, { useState, useEffect, useRef, useCallback } from 'react';
import { Upload, Download, Trash2, FileText, Loader2 } from 'lucide-react';
import { Modal } from './ui';

interface Report { id: number; filename: string; size_bytes: number; uploaded_at: string; }
interface BridgeReportsModalProps {
  stateCode: string | number;
  bridgeNumber: string;
  bridgeName?: string;
  onClose: () => void;
  embedded?: boolean;
}

function fmtBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}
function fmtDate(ts: string): string {
  if (!ts) return '';
  const d = new Date(ts.includes('T') ? ts : ts.replace(' ', 'T') + 'Z');
  return isNaN(d.getTime()) ? ts : d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

const ACCEPTED = '.pdf,.doc,.docx,.xlsx,.xls,.txt,.csv,.png,.jpg,.jpeg';

const BridgeReportsModal: React.FC<BridgeReportsModalProps> = ({ stateCode, bridgeNumber, bridgeName, onClose, embedded }) => {
  const [reports, setReports] = useState<Report[]>([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [status, setStatus] = useState('');
  const [dragging, setDragging] = useState(false);
  const [ingest, setIngest] = useState<{ stage: string; pct: number } | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const sc = String(stateCode);
  const bn = String(bridgeNumber);

  const pollIngestion = useCallback(() => {
    if (pollRef.current) clearInterval(pollRef.current);
    setIngest({ stage: 'Starting…', pct: 2 });
    pollRef.current = setInterval(async () => {
      try {
        const r = await fetch('/api/vision-progress');
        const d = await r.json();
        const stage = d.stage || '';
        const pct = d.total_steps ? Math.min(100, Math.round((d.step / d.total_steps) * 100)) : 5;
        setIngest({ stage: stage || 'Indexing…', pct });
        if (stage === 'Ready' || (d.total_steps && d.step >= d.total_steps)) {
          if (pollRef.current) clearInterval(pollRef.current);
          setIngest({ stage: 'Indexed ✓', pct: 100 });
          setTimeout(() => setIngest(null), 2500);
        }
      } catch { /* keep polling */ }
    }, 1200);
  }, []);

  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  const fetchReports = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`/api/bridges/${sc}/${bn}/reports`);
      const data = await res.json();
      setReports(data.reports ?? []);
    } catch { setStatus('Failed to load reports.'); }
    finally { setLoading(false); }
  }, [sc, bn]);

  useEffect(() => { fetchReports(); }, [fetchReports]);

  const uploadFile = useCallback(async (file: File) => {
    setUploading(true);
    setStatus(`Uploading "${file.name}"…`);
    try {
      const fd = new FormData();
      fd.append('file', file);
      const res = await fetch(`/api/bridges/${sc}/${bn}/reports`, { method: 'POST', body: fd });
      if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.detail ?? `Upload failed (${res.status})`); }
      const data = await res.json().catch(() => ({}));
      setStatus(`Uploaded "${file.name}" successfully.`);
      await fetchReports();
      if (data.ingestion_started) pollIngestion(); // show extract → embed → indexed
    } catch (err) { setStatus(`Error: ${err instanceof Error ? err.message : String(err)}`); }
    finally { setUploading(false); }
  }, [sc, bn, fetchReports, pollIngestion]);

  const handleFileInput = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) uploadFile(file);
    e.target.value = '';
  };
  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault(); setDragging(false);
    const file = e.dataTransfer.files?.[0];
    if (file) uploadFile(file);
  };
  const handleDownload = (report: Report) => {
    const a = document.createElement('a');
    a.href = `/api/bridges/${sc}/${bn}/reports/${report.id}`;
    a.download = report.filename;
    a.click();
  };
  const handleDelete = async (report: Report) => {
    if (!window.confirm(`Delete "${report.filename}"?`)) return;
    try {
      const res = await fetch(`/api/bridges/${sc}/${bn}/reports/${report.id}`, { method: 'DELETE' });
      if (!res.ok) throw new Error('Delete failed');
      setStatus(`Deleted "${report.filename}".`);
      setReports((prev) => prev.filter((r) => r.id !== report.id));
    } catch (err) { setStatus(`Error: ${err instanceof Error ? err.message : String(err)}`); }
  };

  return (
    <Modal open onClose={onClose} inline={embedded} size="md" title="Inspection Reports" icon={<FileText className="w-5 h-5" />}>
      <p className="text-sm text-slate-500 dark:text-slate-400 -mt-1 mb-4">{bridgeName || `Bridge ${bridgeNumber}`} · ID {bridgeNumber}</p>

      {/* Drop zone */}
      <div
        onDrop={handleDrop}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onClick={() => fileInputRef.current?.click()}
        className={`rounded-2xl border-2 border-dashed px-4 py-6 text-center cursor-pointer transition-colors ${dragging ? 'border-emerald-500 bg-emerald-50 dark:bg-emerald-950/30' : 'border-slate-300 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-800/50'}`}
      >
        <Upload className="w-6 h-6 text-emerald-500 mx-auto mb-1.5" />
        <div className="font-semibold text-sm text-slate-700 dark:text-slate-200">{uploading ? 'Uploading…' : 'Drop a report here or click to upload'}</div>
        <div className="text-[11px] text-slate-400 mt-1">PDF · DOC · DOCX · XLSX · PNG · JPG · TXT</div>
        <input ref={fileInputRef} type="file" accept={ACCEPTED} className="hidden" onChange={handleFileInput} />
      </div>
      {status && <div className={`mt-2 text-xs font-medium ${status.startsWith('Error') ? 'text-red-500' : 'text-emerald-600 dark:text-emerald-400'}`}>{status}</div>}

      {ingest && (
        <div className="mt-3 rounded-xl border border-brand-300/50 dark:border-brand-700/50 bg-brand-500/5 px-4 py-3">
          <div className="flex items-center justify-between text-xs mb-1.5">
            <span className="font-medium text-brand-600 dark:text-brand-300">{ingest.stage}</span>
            <span className="text-slate-400">{ingest.pct}%</span>
          </div>
          <div className="h-1.5 rounded-full bg-slate-200 dark:bg-slate-700 overflow-hidden">
            <div className="h-full bg-brand-500 rounded-full transition-all duration-500" style={{ width: `${ingest.pct}%` }} />
          </div>
        </div>
      )}

      {/* Report list */}
      <div className="mt-4 space-y-2">
        {loading ? (
          <div className="flex items-center justify-center py-8"><Loader2 className="w-6 h-6 text-emerald-500 animate-spin" /></div>
        ) : reports.length === 0 ? (
          <div className="text-center py-8 text-slate-400 text-sm">No reports uploaded yet.</div>
        ) : (
          reports.map((report) => (
            <div key={report.id} className="flex items-center gap-3 px-3 py-2.5 rounded-xl border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800/50">
              <FileText className="w-4.5 h-4.5 text-sky-500 shrink-0" />
              <div className="flex-1 min-w-0">
                <div className="text-sm font-semibold text-slate-800 dark:text-slate-100 truncate">{report.filename}</div>
                <div className="text-[11px] text-slate-400 mt-0.5">{fmtBytes(report.size_bytes)} · {fmtDate(report.uploaded_at)}</div>
              </div>
              <button onClick={() => handleDownload(report)} title="Download" className="inline-flex items-center gap-1 text-xs font-semibold text-brand-600 dark:text-brand-300 px-2.5 py-1.5 rounded-lg bg-brand-500/10 hover:bg-brand-500/20 transition-colors">
                <Download className="w-3.5 h-3.5" /> Download
              </button>
              <button onClick={() => handleDelete(report)} title="Delete" className="grid place-items-center w-8 h-8 rounded-lg text-red-500 bg-red-500/10 hover:bg-red-500/20 transition-colors">
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            </div>
          ))
        )}
      </div>
    </Modal>
  );
};

export default BridgeReportsModal;
