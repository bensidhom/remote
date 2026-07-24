import React, { useEffect, useState } from 'react';
import axios from 'axios';
import { Settings, Cloud, Brain, Eye, Layers, Trash2 } from 'lucide-react';
import { Modal } from './ui';

interface SettingsDialogProps {
  open: boolean;
  onClose: () => void;
}

const inputClass =
  'w-full bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-2xl px-4 py-3 text-sm text-gray-900 dark:text-white focus:outline-none focus:ring-2 focus:ring-indigo-500';

const SettingsDialog: React.FC<SettingsDialogProps> = ({ open, onClose }) => {
  const [resetLoading, setResetLoading] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [statusType, setStatusType] = useState<'success' | 'error'>('success');

  // RAG settings (read from backend)
  const [embeddingModel, setEmbeddingModel] = useState('nomic-ai/nomic-embed-text-v1.5');
  const [embeddingProvider, setEmbeddingProvider] = useState('tei');
  const [embeddingDim, setEmbeddingDim] = useState(768);
  const [embeddingRevision, setEmbeddingRevision] = useState('');
  const [visionModel, setVisionModel] = useState('Cerebras');
  const [chunkSize, setChunkSize] = useState(500);
  const [chunkOverlap, setChunkOverlap] = useState(100);

  useEffect(() => {
    if (open) {
      loadRagSettings();
    }
  }, [open]);

  const loadRagSettings = async () => {
    try {
      const res = await axios.get('/api/rag-settings');
      if (res.data) {
        setEmbeddingModel(res.data.embed_model || 'nomic-ai/nomic-embed-text-v1.5');
        setEmbeddingProvider(res.data.embedding_provider || 'tei');
        setEmbeddingDim(Number(res.data.embed_dim) || 768);
        setEmbeddingRevision(res.data.embed_revision || '');
        setVisionModel(res.data.vision_model || 'Cerebras');
        setChunkSize(res.data.chunk_size || 500);
        setChunkOverlap(res.data.chunk_overlap || 100);
      }
    } catch {
      // Defaults are fine
    }
  };

  const saveRagSettings = async () => {
    try {
      await axios.post('/api/rag-settings', {
        chunk_size: chunkSize,
        chunk_overlap: chunkOverlap,
      });
      setStatusType('success');
      setStatusMessage('Settings saved.');
      setTimeout(() => setStatusMessage(null), 2000);
    } catch {
      setStatusType('error');
      setStatusMessage('Failed to save settings.');
    }
  };

  const resetIndex = async () => {
    setResetLoading(true);
    setStatusMessage(null);
    try {
      const response = await axios.post('/api/vision-reset');
      if (response.data.status === 'success') {
        setStatusType('success');
        setStatusMessage(response.data.message || 'Index cleared.');
      } else {
        throw new Error(response.data.detail || 'Reset failed');
      }
    } catch (error: any) {
      setStatusType('error');
      setStatusMessage(error?.response?.data?.detail || error?.message || 'Failed to reset index.');
    } finally {
      setResetLoading(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} size="md" title="Settings" icon={<Settings className="w-5 h-5" />}>
        <div className="space-y-6">
          {/* ── AI Provider (fixed, hosted, multimodal) ─ */}
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Cloud className="w-4 h-4 text-indigo-500" />
              <label className="text-sm font-semibold text-gray-700 dark:text-gray-300">AI Provider</label>
            </div>
            <div className="flex items-center gap-3 rounded-2xl border border-indigo-200 bg-indigo-50 px-4 py-3 text-indigo-800 dark:border-indigo-800 dark:bg-indigo-950/30 dark:text-indigo-200">
              <Cloud className="h-5 w-5" />
              <div>
                <div className="text-sm font-semibold">Cerebras</div>
                <div className="text-[11px] opacity-75">Server-controlled text and vision generation</div>
              </div>
            </div>
            <p className="text-xs text-gray-400">
              Cerebras handles chat, report-image understanding, and point-cloud cleanup/segmentation judgments.
            </p>
          </div>

          {/* ── Divider ───────────────────────────────── */}
          <div className="border-t border-gray-200 dark:border-gray-700" />

          {/* ── Embedding Model (RAG) ─────────────────── */}
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Brain className="w-4 h-4 text-violet-500" />
              <label className="text-sm font-semibold text-gray-700 dark:text-gray-300">Embedding Model</label>
            </div>
            <input
              type="text"
              value={embeddingModel}
              readOnly
              className={`${inputClass} cursor-not-allowed opacity-75`}
            />
            <p className="text-xs text-gray-400">
              {embeddingProvider.toUpperCase()} · {embeddingDim} dimensions
              {embeddingRevision ? ` · revision ${embeddingRevision.slice(0, 12)}` : ''}. Old parsed
              files are reusable, but vectors are rebuilt if this pinned profile changes.
            </p>
          </div>

          {/* ── Vision Indexing Model (RAG) ───────────── */}
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Eye className="w-4 h-4 text-green-500" />
              <label className="text-sm font-semibold text-gray-700 dark:text-gray-300">Vision Indexing Model</label>
            </div>
            <input
              type="text"
              value={visionModel}
              readOnly
              className={`${inputClass} cursor-not-allowed opacity-75`}
            />
            <p className="text-xs text-gray-400">Model used for extracted image analysis during indexing and segmentation.</p>
          </div>

          {/* ── Chunk Settings ────────────────────────── */}
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Layers className="w-4 h-4 text-amber-500" />
              <label className="text-sm font-semibold text-gray-700 dark:text-gray-300">Chunk Settings</label>
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="text-xs text-gray-500 dark:text-gray-400">Chunk Size</label>
                <input
                  type="number"
                  value={chunkSize}
                  onChange={(e) => setChunkSize(Math.max(100, parseInt(e.target.value) || 500))}
                  min={100}
                  max={5000}
                  className="mt-1 w-full bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-2xl px-4 py-3 text-sm text-gray-900 dark:text-white focus:outline-none focus:ring-2 focus:ring-amber-500"
                />
              </div>
              <div>
                <label className="text-xs text-gray-500 dark:text-gray-400">Chunk Overlap</label>
                <input
                  type="number"
                  value={chunkOverlap}
                  onChange={(e) => setChunkOverlap(Math.max(0, parseInt(e.target.value) || 100))}
                  min={0}
                  max={1000}
                  className="mt-1 w-full bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-2xl px-4 py-3 text-sm text-gray-900 dark:text-white focus:outline-none focus:ring-2 focus:ring-amber-500"
                />
              </div>
            </div>
            <p className="text-xs text-gray-400">Controls how documents are split for indexing.</p>
          </div>

          {/* Save RAG Settings */}
          <button
            onClick={saveRagSettings}
            className="w-full rounded-2xl bg-indigo-600 hover:bg-indigo-700 text-white px-4 py-3 text-sm font-semibold transition-colors"
          >
            Save Pipeline Settings
          </button>

          {/* ── Divider ───────────────────────────────── */}
          <div className="border-t border-gray-200 dark:border-gray-700" />

          {/* ── Reset Index ───────────────────────────── */}
          <div className="space-y-2">
            <button
              type="button"
              onClick={resetIndex}
              disabled={resetLoading}
              className="w-full flex items-center justify-center gap-2 rounded-2xl border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400 px-4 py-3 text-sm font-medium hover:bg-red-100 dark:hover:bg-red-900/40 transition-colors disabled:opacity-60"
            >
              <Trash2 className="w-4 h-4" />
              {resetLoading ? 'Clearing…' : 'Reset Document Index'}
            </button>
            <p className="text-xs text-gray-400">Clear cached documents so the next upload re-processes from scratch.</p>
          </div>

          {/* Status */}
          {statusMessage && (
            <div className={`rounded-2xl p-3 text-sm ${statusType === 'success' ? 'bg-green-50 dark:bg-green-900/30 text-green-700 dark:text-green-300' : 'bg-red-50 dark:bg-red-900/30 text-red-700 dark:text-red-300'}`}>
              {statusMessage}
            </div>
          )}
        </div>
    </Modal>
  );
};

export default SettingsDialog;
