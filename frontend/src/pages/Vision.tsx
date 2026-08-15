import React, { useCallback, useEffect, useRef, useState } from 'react';
import axios, { CancelTokenSource } from 'axios';
import { Camera, Upload, Sparkles, User, Bot, Trash2, X, ZoomIn, ZoomOut, Ban, Clock, ChevronLeft, ChevronRight, Paperclip, Send, Settings2, Database, FileText, RotateCcw } from 'lucide-react';
import { privateStorageNamespace } from '../lib/access';

interface ChatImage {
  filename: string;
  mime: string;
  data: string;
  label?: string;
}

interface ChatMessage {
  id: number;
  role: 'user' | 'assistant';
  text: string;
  sources?: string[];
  jsonAnswer?: any;
  images?: ChatImage[];
  timestamp: Date;
}

interface VisionProps {
  onRagActiveChange?: (active: boolean, requestId?: string) => void;
  ownerNamespace: string;
  workspaceNamespace: string;
}

const DOCUMENT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

const Vision: React.FC<VisionProps> = ({ onRagActiveChange, ownerNamespace, workspaceNamespace }) => {
  const [sourceType, setSourceType] = useState<'document' | 'database'>('document');
  const [file, setFile] = useState<File | null>(null);
  const [fileIngested, setFileIngested] = useState(false);
  const [dbStateCode, setDbStateCode] = useState('');
  const [dbBridgeNumber, setDbBridgeNumber] = useState('');
  const [question, setQuestion] = useState('');
  const [chatHistory, setChatHistory] = useState<ChatMessage[]>([]);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [statusType, setStatusType] = useState<'success' | 'error'>('success');
  const [strictGrounding, setStrictGrounding] = useState(true);
  const [outputFormat, setOutputFormat] = useState<'text' | 'json'>('text');
  const [targetFields, setTargetFields] = useState('BL01,BID01,BID02,BIE03,BC01,BC02,BC03,BC04,BC13');
  const [resettingIndex, setResettingIndex] = useState(false);
  const [indexedStatus, setIndexedStatus] = useState<{ indexed: boolean; pages: number; images: number }>({ indexed: false, pages: 0, images: 0 });
  const [checkingIndex, setCheckingIndex] = useState(false);
  const [debugPath, setDebugPath] = useState('idle');
  const [loading, setLoading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [progressText, setProgressText] = useState('');
  const [etaSeconds, setEtaSeconds] = useState<number | null>(null);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [selectedImage, setSelectedImage] = useState<ChatImage | null>(null);
  const [selectedImageZoom, setSelectedImageZoom] = useState(1);
  const [activeDocHash, setActiveDocHash] = useState<string>('');
  const [hasIndexedSession, setHasIndexedSession] = useState(false);
  const chatEndRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const nextId = useRef(1);
  const cancelSourceRef = useRef<CancelTokenSource | null>(null);
  const activeAiRequestRef = useRef<string | null>(null);
  const progressIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  const storagePrefix = `${privateStorageNamespace(ownerNamespace, workspaceNamespace)}vision:`;
  const PERSIST_INDEXED_KEY = `${storagePrefix}indexed_session`;
  const PERSIST_DOC_HASH_KEY = `${storagePrefix}indexed_doc_hash`;
  const PERSIST_DOC_NAME_KEY = `${storagePrefix}indexed_doc_name`;
  const PERSIST_CHAT_KEY = `${storagePrefix}chat_history`;
  /** Save doc state to localStorage so it survives app restarts. */
  const persistDocState = (indexed: boolean, docHash: string, docName?: string) => {
    try {
      if (indexed) {
        localStorage.setItem(PERSIST_INDEXED_KEY, '1');
        if (docHash) localStorage.setItem(PERSIST_DOC_HASH_KEY, docHash);
        if (docName) localStorage.setItem(PERSIST_DOC_NAME_KEY, docName);
      } else {
        localStorage.removeItem(PERSIST_INDEXED_KEY);
        localStorage.removeItem(PERSIST_DOC_HASH_KEY);
        localStorage.removeItem(PERSIST_DOC_NAME_KEY);
        localStorage.removeItem(PERSIST_CHAT_KEY);
      }
    } catch { /* ignore */ }
  };

  /** Persist chat messages to localStorage (images stripped to save space). */
  const persistChat = (messages: ChatMessage[]) => {
    try {
      const slim = messages.map(m => ({
        ...m,
        images: m.images?.map(i => ({ ...i, data: '' })),
      }));
      localStorage.setItem(PERSIST_CHAT_KEY, JSON.stringify(slim));
    } catch { /* ignore */ }
  };

  /** Load chat messages from localStorage. */
  const loadPersistedChat = (): ChatMessage[] => {
    try {
      const raw = localStorage.getItem(PERSIST_CHAT_KEY);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return [];
      return parsed.map((m: any) => ({ ...m, timestamp: new Date(m.timestamp) }));
    } catch { return []; }
  };

  const scrollToBottom = () => {
    setTimeout(() => chatEndRef.current?.scrollIntoView({ behavior: 'smooth' }), 100);
  };

  const refreshIndexStatus = async (preferredDocHash?: string) => {
    setCheckingIndex(true);
    try {
      const docHashToCheck = preferredDocHash || activeDocHash;
      const response = await axios.get('/api/vision-index-status', {
        params: docHashToCheck ? { doc_hash: docHashToCheck } : undefined,
      });
      const data = response.data || {};
      const indexed = Boolean(data.indexed);
      setIndexedStatus({
        indexed,
        pages: Number(data.pages || 0),
        images: Number(data.images || 0),
      });
      setFileIngested(indexed);
      const resolvedHash = indexed ? String(data.doc_hash || docHashToCheck || '') : '';
      setActiveDocHash(resolvedHash);
      setHasIndexedSession(indexed);
      // Sync persisted state with backend truth
      if (indexed) {
        persistDocState(true, resolvedHash);
      } else {
        persistDocState(false, '');
      }
    } catch {
      setIndexedStatus({ indexed: false, pages: 0, images: 0 });
      setFileIngested(false);
      setActiveDocHash('');
      setHasIndexedSession(false);
      persistDocState(false, '');
    } finally {
      setCheckingIndex(false);
    }
  };

  useEffect(() => {
    try {
      const savedIndexed = localStorage.getItem(PERSIST_INDEXED_KEY) === '1';
      const savedDocHash = localStorage.getItem(PERSIST_DOC_HASH_KEY) || '';
      const savedDocName = localStorage.getItem(PERSIST_DOC_NAME_KEY) || '';
      if (savedIndexed) {
        setFileIngested(true);
        setHasIndexedSession(true);
      }
      if (savedDocHash) {
        setActiveDocHash(savedDocHash);
      }
      // Restore chat history from previous session
      const savedChat = loadPersistedChat();
      if (savedChat.length > 0) {
        setChatHistory(savedChat);
        nextId.current = Math.max(...savedChat.map(m => m.id)) + 1;
      }
      // Verify the backend still has this document indexed
      refreshIndexStatus(savedDocHash || undefined).then(() => {
        // If backend says NOT indexed, clear persisted state
        // (refreshIndexStatus already updates React state)
      });
    } catch {
      refreshIndexStatus();
    }
  }, []);

  useEffect(() => {
    if (!selectedImage) {
      return;
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setSelectedImage(null);
        setSelectedImageZoom(1);
        return;
      }
      if (event.key === '+' || event.key === '=') {
        event.preventDefault();
        setSelectedImageZoom((current) => Math.min(4, Number((current + 0.25).toFixed(2))));
        return;
      }
      if (event.key === '-') {
        event.preventDefault();
        setSelectedImageZoom((current) => Math.max(0.5, Number((current - 0.25).toFixed(2))));
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [selectedImage]);

  const formatEta = (seconds: number | null): string => {
    if (seconds === null || seconds <= 0) return '';
    if (seconds < 60) return `~${Math.ceil(seconds)}s remaining`;
    const m = Math.floor(seconds / 60);
    const s = Math.ceil(seconds % 60);
    return `~${m}m ${s}s remaining`;
  };

  const formatElapsed = (seconds: number): string => {
    if (seconds <= 0) return '0s';
    if (seconds < 60) return `${Math.round(seconds)}s`;
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return `${m}m ${s}s`;
  };

  const startProgressPolling = useCallback(() => {
    if (progressIntervalRef.current) clearInterval(progressIntervalRef.current);
    progressIntervalRef.current = setInterval(async () => {
      try {
        const requestId = activeAiRequestRef.current;
        if (!requestId) return;
        const resp = await axios.get(`/api/ai/requests/${encodeURIComponent(requestId)}/evidence`);
        const data = resp.data;
        setProgressText(data.status === 'active' ? 'Processing request…' : String(data.status || ''));
        setEtaSeconds(null);
      } catch {
        // Ignore polling errors
      }
    }, 1500);
  }, []);

  const stopProgressPolling = useCallback(() => {
    if (progressIntervalRef.current) {
      clearInterval(progressIntervalRef.current);
      progressIntervalRef.current = null;
    }
    setEtaSeconds(null);
    setElapsedSeconds(0);
  }, []);

  const handleCancel = useCallback(async () => {
    const requestId = activeAiRequestRef.current;
    try {
      if (requestId) {
        await axios.post(`/api/ai/requests/${encodeURIComponent(requestId)}/cancel`);
      }
    } catch {
      // Ignore cancel request errors
    }
    if (cancelSourceRef.current) {
      cancelSourceRef.current.cancel('User cancelled');
      cancelSourceRef.current = null;
    }
    activeAiRequestRef.current = null;
    stopProgressPolling();
    setLoading(false);
    setProgress(0);
    setProgressText('');
    setStatusType('error');
    setStatusMessage('Processing cancelled.');
  }, [stopProgressPolling]);

  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    if (event.target.files?.length) {
      const selected = event.target.files[0];
      const isPdf = selected.name.toLowerCase().endsWith('.pdf')
        && (!selected.type || selected.type === 'application/pdf');
      if (!isPdf) {
        setFile(null);
        setStatusType('error');
        setStatusMessage('Document Ask accepts PDF files only.');
        event.target.value = '';
        return;
      }
      if (selected.size > DOCUMENT_MAX_UPLOAD_BYTES) {
        setFile(null);
        setStatusType('error');
        setStatusMessage('The PDF exceeds the 50 MB upload limit.');
        event.target.value = '';
        return;
      }
      setFile(selected);
      setFileIngested(false);
      setActiveDocHash('');
      setHasIndexedSession(false);
      setIndexedStatus({ indexed: false, pages: 0, images: 0 });
      setStatusMessage(null);
      persistDocState(false, '');
    }
  };

  const addUserMessage = (text: string): number => {
    const id = nextId.current++;
    setChatHistory((prev) => {
      const updated = [...prev, { id, role: 'user' as const, text, timestamp: new Date() }];
      persistChat(updated);
      return updated;
    });
    scrollToBottom();
    return id;
  };

  const addAssistantMessage = (
    text: string,
    sources?: string[],
    images?: ChatImage[],
    jsonAnswer?: any,
  ) => {
    setChatHistory((prev) => {
      const updated = [
        ...prev,
        { id: nextId.current++, role: 'assistant' as const, text, sources, images, jsonAnswer, timestamp: new Date() },
      ];
      persistChat(updated);
      return updated;
    });
    scrollToBottom();
  };

  const clearChat = () => {
    setChatHistory([]);
    setStatusMessage(null);
    try { localStorage.removeItem(PERSIST_CHAT_KEY); } catch { /* ignore */ }
  };

  const openImageViewer = (image: ChatImage) => {
    setSelectedImage(image);
    setSelectedImageZoom(1);
  };

  const closeImageViewer = () => {
    setSelectedImage(null);
    setSelectedImageZoom(1);
  };

  const adjustImageZoom = (delta: number) => {
    setSelectedImageZoom((current) => Math.max(0.5, Math.min(4, Number((current + delta).toFixed(2)))));
  };

  const submitVisionQuery = async () => {
    if (!question.trim()) {
      setStatusType('error');
      setStatusMessage('Please enter a question for the document.');
      return;
    }

    const q = question.trim();
    addUserMessage(q);
    setQuestion('');
    setLoading(true);
    setStatusMessage(null);

    const callQueryOnly = async (): Promise<{ ok: boolean; noIndexedDoc: boolean; message?: string }> => {
      const requestId = crypto.randomUUID();
      activeAiRequestRef.current = requestId;
      onRagActiveChange?.(true, requestId);
      setDebugPath('document:query-only');
      setProgress(30);
      setProgressText('Querying indexed document...');
      try {
        const formData = new FormData();
        formData.append('question', q);
        formData.append('strict_grounding', String(strictGrounding));
        formData.append('output_format', outputFormat);
        formData.append('target_fields', targetFields);
        formData.append('analysis_mode', 'text');
        if (activeDocHash) {
          formData.append('doc_hash', activeDocHash);
        }

        const response = await axios.post('/api/vision-query', formData, {
          headers: { 'Content-Type': 'multipart/form-data', 'X-Request-ID': requestId },
        });

        const data = response.data;
        if (activeAiRequestRef.current !== requestId || data.request_id !== requestId) {
          return { ok: false, noIndexedDoc: false, message: 'Stale response ignored.' };
        }
        if (data.status === 'success') {
          addAssistantMessage(
            data.answer || 'No answer returned.',
            data.source_chunks || [],
            data.images || [],
            data.json_answer,
          );
          setStatusType('success');
          setStatusMessage(null);
          const indexed = data.indexed !== false;
          setFileIngested(indexed);
          const resolvedDocHash = indexed ? String(data.doc_hash || activeDocHash || '') : '';
          setActiveDocHash(resolvedDocHash);
          setIndexedStatus({
            indexed,
            pages: Number(data.pages || 0),
            images: Number(data.images_count || data.images || 0),
          });
          setHasIndexedSession(indexed);
          persistDocState(indexed, resolvedDocHash);
          return { ok: true, noIndexedDoc: false };
        }

        const msg = data.detail || 'Unexpected response from the backend.';
        addAssistantMessage(msg);
        setStatusType('error');
        setStatusMessage(msg);
        return { ok: false, noIndexedDoc: false, message: msg };
      } catch (error: any) {
        const detail = String(error?.response?.data?.detail || '');
        const noIndexedDoc = detail.toLowerCase().includes('no document has been ingested');
        if (!noIndexedDoc) {
          const msg = detail || 'Failed to query the indexed document. Please try again.';
          addAssistantMessage(msg);
          setStatusType('error');
          setStatusMessage(msg);
        }
        return { ok: false, noIndexedDoc, message: detail };
      } finally {
        if (activeAiRequestRef.current === requestId) activeAiRequestRef.current = null;
        onRagActiveChange?.(false);
      }
    };

    const callUploadAndIngest = async (): Promise<boolean> => {
      if (!file) {
        return false;
      }

      setDebugPath('document:upload+ingest');
      setProgress(5);
      setProgressText('Uploading document...');
      setEtaSeconds(null);
      setElapsedSeconds(0);

      const source = axios.CancelToken.source();
      cancelSourceRef.current = source;
      const requestId = crypto.randomUUID();
      activeAiRequestRef.current = requestId;
      onRagActiveChange?.(true, requestId);

      try {
        const formData = new FormData();
        formData.append('file', file);
        formData.append('question', q);
        formData.append('strict_grounding', String(strictGrounding));
        formData.append('output_format', outputFormat);
        formData.append('target_fields', targetFields);
        formData.append('analysis_mode', 'text');

        let uploadDone = false;
        const response = await axios.post('/api/vision-rag', formData, {
          headers: { 'Content-Type': 'multipart/form-data', 'X-Request-ID': requestId },
          cancelToken: source.token,
          onUploadProgress: (progressEvent) => {
            if (progressEvent.total) {
              const percent = Math.min(15, 5 + Math.round((progressEvent.loaded / progressEvent.total) * 10));
              setProgress(percent);
              setProgressText('Uploading document...');
              if (progressEvent.loaded >= progressEvent.total && !uploadDone) {
                uploadDone = true;
                setProgressText('Processing document...');
                startProgressPolling();
              }
            }
          },
        });

        stopProgressPolling();
        setProgress(90);
        setProgressText('Processing document...');
        const data = response.data;
        if (activeAiRequestRef.current !== requestId || data.request_id !== requestId) return false;
        if (data.status === 'success') {
          const indexed = data.indexed === true;
          setFileIngested(indexed);
          const resolvedDocHash = indexed ? String(data.doc_hash || '') : '';
          setActiveDocHash(resolvedDocHash);
          setIndexedStatus({
            indexed,
            pages: Number(data.pages || 0),
            images: Number(data.images_count || 0),
          });
          setHasIndexedSession(indexed || Boolean(resolvedDocHash));
          persistDocState(indexed, resolvedDocHash, file?.name);
          addAssistantMessage(
            data.answer || 'No answer returned.',
            data.source_chunks || [],
            data.images || [],
            data.json_answer,
          );
          setStatusType('success');
          setStatusMessage(
            indexed
              ? `Document processed with ${data.model_used || 'Cerebras'} mode.`
                : `Processed with ${data.model_used || 'Cerebras'} mode (fallback path, not indexed). ${data.rag_error ? `Reason: ${data.rag_error}` : ''}`
          );
          return true;
        }

        const msg = data.detail || 'Unexpected response from the backend.';
        addAssistantMessage(msg);
        setStatusType('error');
        setStatusMessage(msg);
        return false;
      } catch (error: any) {
        stopProgressPolling();
        if (axios.isCancel(error)) {
          return false;
        }
        const detail = error?.response?.data?.detail || '';
        const cancelled = error?.response?.status === 499 || detail.toLowerCase().includes('cancelled');
        if (cancelled) {
          setStatusType('error');
          setStatusMessage('Processing cancelled.');
          return false;
        }
        const msg = detail || 'Failed to process the file. Please try again.';
        addAssistantMessage(msg);
        setStatusType('error');
        setStatusMessage(msg);
        return false;
      } finally {
        cancelSourceRef.current = null;
        if (activeAiRequestRef.current === requestId) activeAiRequestRef.current = null;
        onRagActiveChange?.(false);
      }
    };

    // Best workflow:
    // 1) Always try query-only first when session/backend suggests an indexed doc.
    // 2) If backend says no indexed doc, fallback to one-time upload+ingest.
    // 3) Never re-upload repeatedly for normal follow-up prompts.
    const canTryQueryOnly = Boolean(activeDocHash) || fileIngested || indexedStatus.indexed || hasIndexedSession;
    if (canTryQueryOnly) {
      const qOnly = await callQueryOnly();
      if (qOnly.ok) {
        stopProgressPolling();
        setProgress(100);
        setProgressText('Complete');
        setLoading(false);
        window.setTimeout(() => { setProgress(0); setProgressText(''); }, 600);
        return;
      }
      if (!qOnly.noIndexedDoc) {
        stopProgressPolling();
        setProgress(100);
        setProgressText('Complete');
        setLoading(false);
        window.setTimeout(() => { setProgress(0); setProgressText(''); }, 600);
        return;
      }
      // If a prior indexed session existed, do not silently re-upload on follow-up prompts.
      if (hasIndexedSession || activeDocHash) {
        if (file) {
          setDebugPath('document:reindex-recovery');
          setStatusType('success');
          setStatusMessage('Indexed session was missing for follow-up. Re-indexing from selected file...');
          await callUploadAndIngest();
          stopProgressPolling();
          setProgress(100);
          setProgressText('Complete');
          setLoading(false);
          window.setTimeout(() => { setProgress(0); setProgressText(''); }, 600);
          return;
        }

        setDebugPath('document:query-only-missing-index');
        stopProgressPolling();
        setLoading(false);
        setStatusType('error');
        setStatusMessage('The indexed document session could not be found for this follow-up. Please select the report file once to re-index.');
        window.setTimeout(() => { setProgress(0); setProgressText(''); }, 600);
        return;
      }

      // No known indexed session exists yet; allow initial upload+ingest.
      setFileIngested(false);
      setActiveDocHash('');
      setHasIndexedSession(false);
      persistDocState(false, '');
    }

    if (!file) {
      setDebugPath('document:error-no-index-no-file');
      setLoading(false);
      setStatusType('error');
      setStatusMessage('No indexed document found. Please upload a report first.');
      return;
    }

    await callUploadAndIngest();
    stopProgressPolling();
    setProgress(100);
    setProgressText('Complete');
    setLoading(false);
    window.setTimeout(() => {
      setProgress(0);
      setProgressText('');
    }, 600);
  };

  const submitDbQuery = async () => {
    if (!question.trim()) {
      setStatusType('error');
      setStatusMessage('Please enter a question for the database.');
      return;
    }

    const q = question.trim();
    setDebugPath('database:rag');
    addUserMessage(q);
    setQuestion('');
    setLoading(true);
    const requestId = crypto.randomUUID();
    activeAiRequestRef.current = requestId;
    onRagActiveChange?.(true, requestId);

    try {
      const formData = new FormData();
      formData.append('question', q);
      formData.append('strict_grounding', String(strictGrounding));
      formData.append('output_format', outputFormat);
      formData.append('target_fields', targetFields);
      formData.append('analysis_mode', 'text');
      if (dbStateCode.trim()) {
        formData.append('state_code', dbStateCode.trim());
      }
      if (dbBridgeNumber.trim()) {
        formData.append('bridge_number', dbBridgeNumber.trim());
      }

      const response = await axios.post('/api/db-rag', formData, {
        headers: { 'Content-Type': 'multipart/form-data', 'X-Request-ID': requestId },
      });

      const data = response.data;
      if (activeAiRequestRef.current !== requestId || data.request_id !== requestId) return;
      if (data.status === 'success') {
        addAssistantMessage(
          data.answer || 'No answer returned.',
          data.source_chunks || [],
          [],
          data.json_answer,
        );
        setStatusType('success');
        setStatusMessage(`Database query processed with ${data.model_used || 'Cerebras'} mode.`);
      } else {
        addAssistantMessage(data.detail || 'Unexpected response from the backend.');
        setStatusType('error');
        setStatusMessage(data.detail || 'Unexpected response from the backend.');
      }
    } catch (error: any) {
      console.error(error);
      const msg = error?.response?.data?.detail || 'Failed to query the database. Please try again.';
      addAssistantMessage(msg);
      setStatusType('error');
      setStatusMessage(msg);
    } finally {
      if (activeAiRequestRef.current === requestId) activeAiRequestRef.current = null;
      onRagActiveChange?.(false);
      setLoading(false);
    }
  };

  const runQuery = () => {
    if (sourceType === 'database') {
      submitDbQuery();
    } else {
      submitVisionQuery();
    }
  };

  const retryMessage = useCallback((text: string) => {
    if (loading) return;
    const q = text.trim();
    if (!q) return;
    if (sourceType === 'database') {
      // Inline retry to avoid stale `question` state closure
      const submitRetry = async () => {
        const requestId = crypto.randomUUID();
        activeAiRequestRef.current = requestId;
        onRagActiveChange?.(true, requestId);
        setDebugPath('database:rag');
        addUserMessage(q);
        setLoading(true);
        try {
          const formData = new FormData();
          formData.append('question', q);
          formData.append('strict_grounding', String(strictGrounding));
          formData.append('output_format', outputFormat);
          formData.append('target_fields', targetFields);
          formData.append('analysis_mode', 'text');
          if (dbStateCode.trim()) formData.append('state_code', dbStateCode.trim());
          if (dbBridgeNumber.trim()) formData.append('bridge_number', dbBridgeNumber.trim());
          const response = await axios.post('/api/db-rag', formData, { headers: { 'Content-Type': 'multipart/form-data', 'X-Request-ID': requestId } });
          const data = response.data;
          if (activeAiRequestRef.current !== requestId || data.request_id !== requestId) return;
          if (data.status === 'success') {
            addAssistantMessage(data.answer || 'No answer returned.', data.source_chunks || [], [], data.json_answer);
          } else {
            addAssistantMessage(data.detail || 'Unexpected response.');
          }
        } catch (err: any) {
          addAssistantMessage(err?.response?.data?.detail || 'Failed to query the database.');
        } finally {
          if (activeAiRequestRef.current === requestId) activeAiRequestRef.current = null;
          onRagActiveChange?.(false);
          setLoading(false);
        }
      };
      submitRetry();
    } else {
      // For document mode: set question then fire submitVisionQuery on next tick
      setQuestion(q);
      window.setTimeout(() => submitVisionQuery(), 0);
    }
  }, [loading, sourceType, strictGrounding, outputFormat, targetFields, dbStateCode, dbBridgeNumber, submitVisionQuery]);

  const removeIndexedDocument = async () => {
    setResettingIndex(true);
    setStatusMessage(null);
    try {
      const response = await axios.post('/api/vision-reset');
      if (response.data?.status === 'success') {
        setFileIngested(false);
        setIndexedStatus({ indexed: false, pages: 0, images: 0 });
        setActiveDocHash('');
        setHasIndexedSession(false);
        setChatHistory([]);
        persistDocState(false, '');
        setStatusType('success');
        setStatusMessage('Index removed. Upload/run again to reprocess this report with the latest extraction logic.');
      } else {
        setStatusType('error');
        setStatusMessage(response.data?.detail || 'Failed to remove index.');
      }
    } catch (error: any) {
      setStatusType('error');
      setStatusMessage(error?.response?.data?.detail || 'Failed to remove index.');
    } finally {
      setResettingIndex(false);
    }
  };

  return (
    <div className="flex flex-col" style={{ height: 'calc(100vh - 9.5rem)' }}>

      <div className="flex flex-1 min-h-0">
        {/* Left sidebar — settings only */}
        <div className={`shrink-0 flex flex-col border-r border-gray-200 dark:border-gray-700 glass overflow-y-auto transition-all duration-200 ${sidebarCollapsed ? 'w-12' : 'w-72'}`}>
          {/* Sidebar toggle button */}
          <div className="flex items-center justify-between px-3 py-2 border-b border-gray-200 dark:border-gray-700 shrink-0">
            {!sidebarCollapsed && (
              <span className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide flex items-center gap-2">
                <Settings2 className="w-3.5 h-3.5" />
                Settings
              </span>
            )}
            <button
              type="button"
              onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
              className="ml-auto p-1 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 transition-colors"
            >
              {sidebarCollapsed ? <ChevronRight className="w-4 h-4" /> : <ChevronLeft className="w-4 h-4" />}
            </button>
          </div>

          {!sidebarCollapsed && (
            <div className="flex-1 px-4 py-3 space-y-4 overflow-y-auto">
              {/* Source type toggle */}
              <div className="space-y-2">
                <label className="block text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">Source</label>
                <div className="grid grid-cols-2 gap-2">
                  {(['document', 'database'] as const).map((type) => (
                    <button
                      key={type}
                      type="button"
                      onClick={() => { setSourceType(type); clearChat(); }}
                      className={`rounded-xl px-3 py-2 text-xs font-semibold transition flex items-center justify-center gap-1.5 ${
                        sourceType === type
                          ? 'bg-green-600 text-white shadow'
                          : 'bg-gray-100 dark:bg-gray-900 text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-700'
                      }`}
                    >
                      {type === 'document' ? <FileText className="w-3.5 h-3.5" /> : <Database className="w-3.5 h-3.5" />}
                      {type === 'document' ? 'Document' : 'Database'}
                    </button>
                  ))}
                </div>
              </div>

              {/* Document: index status */}
              {sourceType === 'document' && (
                <div className="space-y-2">
                  <label className="block text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">Index Status</label>
                  <div className="flex items-center justify-between gap-2">
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold ${
                      indexedStatus.indexed
                        ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300'
                        : 'bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400'
                    }`}>
                      {indexedStatus.indexed ? `Indexed (${indexedStatus.pages}p, ${indexedStatus.images}img)` : 'Not indexed'}
                    </span>
                    <button
                      type="button"
                      onClick={() => refreshIndexStatus()}
                      className="text-xs text-blue-600 dark:text-blue-400 hover:underline shrink-0"
                    >
                      {checkingIndex ? '...' : 'Refresh'}
                    </button>
                  </div>
                  {indexedStatus.indexed && (
                    <button
                      type="button"
                      onClick={removeIndexedDocument}
                      disabled={resettingIndex}
                      className="w-full inline-flex items-center justify-center gap-1.5 rounded-xl bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400 px-3 py-2 text-xs font-semibold hover:bg-red-100 dark:hover:bg-red-900/40 transition-colors disabled:opacity-60"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                      {resettingIndex ? 'Removing...' : 'Remove Index'}
                    </button>
                  )}
                </div>
              )}

              {/* Database filters */}
              {sourceType === 'database' && (
                <div className="space-y-2">
                  <label className="block text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">Filters (optional)</label>
                  <input
                    type="text"
                    value={dbStateCode}
                    onChange={(e) => setDbStateCode(e.target.value)}
                    className="w-full bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-xl px-3 py-2 text-xs text-gray-900 dark:text-white focus:outline-none focus:ring-2 focus:ring-green-500"
                    placeholder="State code (e.g. 34)"
                  />
                  <input
                    type="text"
                    value={dbBridgeNumber}
                    onChange={(e) => setDbBridgeNumber(e.target.value)}
                    className="w-full bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-xl px-3 py-2 text-xs text-gray-900 dark:text-white focus:outline-none focus:ring-2 focus:ring-green-500"
                    placeholder="Bridge number (e.g. 001A)"
                  />
                </div>
              )}

              {/* Analysis options */}
              <div className="space-y-3">
                <label className="block text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">Analysis Options</label>
                <label className="flex items-center gap-2 text-xs text-gray-700 dark:text-gray-300 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={strictGrounding}
                    onChange={(e) => setStrictGrounding(e.target.checked)}
                    className="rounded border-gray-300 text-green-600"
                  />
                  Strict grounding
                </label>
                <div>
                  <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Output format</label>
                  <select
                    value={outputFormat}
                    onChange={(e) => setOutputFormat(e.target.value as 'text' | 'json')}
                    className="w-full bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-xl px-3 py-2 text-xs text-gray-900 dark:text-white"
                  >
                    <option value="text">Text</option>
                    <option value="json">JSON Extraction</option>
                  </select>
                </div>
                <div>
                  <label className="block text-xs text-gray-500 dark:text-gray-400 mb-1">Target fields</label>
                  <input
                    value={targetFields}
                    onChange={(e) => setTargetFields(e.target.value)}
                    className="w-full bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-xl px-3 py-2 text-xs text-gray-900 dark:text-white focus:outline-none focus:ring-2 focus:ring-green-500"
                    placeholder="BL01,BID01,BC01..."
                  />
                </div>
              </div>

              {/* Model info */}
              <div className="pt-2 border-t border-gray-100 dark:border-gray-700">
                <p className="text-xs text-gray-400 dark:text-gray-500">Model: <span className="font-medium text-gray-600 dark:text-gray-300">AI</span></p>
              </div>
            </div>
          )}
        </div>

        {/* Right — chat + composer */}
        <div className="flex-1 flex flex-col min-w-0 bg-gray-50 dark:bg-gray-900">
          {/* Chat header */}
          <div className="shrink-0 flex items-center justify-between px-5 py-2 border-b border-gray-200 dark:border-gray-700 glass">
            <div className="flex items-center gap-2 text-green-600">
              <Sparkles className="w-4 h-4" />
              <span className="text-sm font-semibold">BridgeGuard Chat</span>
            </div>
            {chatHistory.length > 0 && (
              <button type="button" onClick={clearChat} className="text-xs text-gray-400 hover:text-red-500 transition-colors">
                Clear chat
              </button>
            )}
          </div>

          {/* Messages */}
          <div className="flex-1 overflow-y-auto px-5 py-3 space-y-3">
            {chatHistory.length === 0 ? (
              <div className="flex flex-col items-center justify-center h-full text-center text-gray-400 dark:text-gray-500">
                <Sparkles className="w-10 h-10 mb-3 opacity-30" />
                <p className="text-sm">Ask a question to start the conversation.</p>
                <p className="text-xs mt-1 opacity-70">
                  {sourceType === 'document' ? 'Attach a PDF (up to 50 MB) using the paperclip below.' : 'Using loaded database as source.'}
                </p>
              </div>
            ) : (
              chatHistory.map((msg) => (
                <div key={msg.id} className={`flex gap-3 ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                  {msg.role === 'assistant' && (
                    <div className="shrink-0 w-7 h-7 rounded-full bg-green-100 dark:bg-green-900/40 flex items-center justify-center mt-1">
                      <Bot className="w-4 h-4 text-green-600" />
                    </div>
                  )}
                  <div className={`max-w-[85%] space-y-2 ${msg.role === 'user' ? 'order-1' : ''}`}>
                    <div className={`rounded-2xl px-4 py-3 text-sm whitespace-pre-wrap ${
                      msg.role === 'user'
                        ? 'bg-green-600 text-white rounded-br-md'
                        : 'glass text-gray-800 dark:text-gray-100 rounded-bl-md shadow-sm border border-gray-200 dark:border-gray-700'
                    }`}>
                      {msg.text}
                    </div>
                    {msg.sources && msg.sources.length > 0 && (
                      <div className="flex flex-wrap gap-1.5 px-1">
                        {msg.sources.map((source, i) => (
                          <span key={i} className="inline-flex items-center px-2.5 py-1 rounded-lg bg-green-50 dark:bg-green-900/30 text-green-700 dark:text-green-300 text-xs font-medium border border-green-200 dark:border-green-800">
                            {source}
                          </span>
                        ))}
                      </div>
                    )}
                    {msg.images && msg.images.length > 0 && (
                      <div className="grid grid-cols-2 gap-2 px-1">
                        {msg.images.map((img, idx) => (
                          <button key={idx} type="button" onClick={() => openImageViewer(img)}
                            className="rounded-xl overflow-hidden border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-950 text-left hover:border-green-400 dark:hover:border-green-600 transition-colors"
                          >
                            <img src={`data:${img.mime};base64,${img.data}`} alt={img.label || img.filename}
                              className="w-full h-auto max-h-56 object-contain bg-gray-50 dark:bg-gray-900"
                            />
                            <div className="px-2 py-1.5 space-y-0.5">
                              {img.label && <div className="text-xs font-medium text-gray-700 dark:text-gray-200 truncate">{img.label}</div>}
                              <div className="text-xs text-gray-500 dark:text-gray-400 truncate">{img.filename}</div>
                            </div>
                          </button>
                        ))}
                      </div>
                    )}
                    {msg.jsonAnswer && (
                      <div className="px-1">
                        <div className="rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-950 p-3">
                          <div className="text-xs font-semibold text-gray-500 dark:text-gray-400 mb-1">Parsed JSON</div>
                          <pre className="text-xs whitespace-pre-wrap text-gray-700 dark:text-gray-200 overflow-x-auto">
                            {JSON.stringify(msg.jsonAnswer, null, 2)}
                          </pre>
                        </div>
                      </div>
                    )}
                    <div className="flex items-center gap-2 px-1">
                      <span className="text-[10px] text-gray-400 dark:text-gray-500">
                        {msg.timestamp.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                      </span>
                      {msg.role === 'user' && !loading && (
                        <button
                          type="button"
                          onClick={() => retryMessage(msg.text)}
                          title="Retry this message"
                          className="inline-flex items-center gap-1 text-[10px] text-gray-400 hover:text-green-600 dark:hover:text-green-400 transition-colors"
                        >
                          <RotateCcw className="w-3 h-3" />
                          <span>Retry</span>
                        </button>
                      )}
                    </div>
                  </div>
                  {msg.role === 'user' && (
                    <div className="shrink-0 w-7 h-7 rounded-full bg-green-600 flex items-center justify-center mt-1 order-2">
                      <User className="w-4 h-4 text-white" />
                    </div>
                  )}
                </div>
              ))
            )}
            {loading && (
              <div className="flex gap-3">
                <div className="shrink-0 w-7 h-7 rounded-full bg-green-100 dark:bg-green-900/40 flex items-center justify-center mt-1">
                  <Bot className="w-4 h-4 text-green-600" />
                </div>
                <div className="rounded-2xl rounded-bl-md glass border border-gray-200 dark:border-gray-700 px-4 py-3 shadow-sm">
                  <div className="flex gap-1.5">
                    <span className="w-2 h-2 rounded-full bg-gray-400 animate-bounce" style={{ animationDelay: '0ms' }} />
                    <span className="w-2 h-2 rounded-full bg-gray-400 animate-bounce" style={{ animationDelay: '150ms' }} />
                    <span className="w-2 h-2 rounded-full bg-gray-400 animate-bounce" style={{ animationDelay: '300ms' }} />
                  </div>
                </div>
              </div>
            )}
            <div ref={chatEndRef} />
          </div>

          {/* Progress bar — slim strip above composer */}
          {progress > 0 && (
            <div className="shrink-0 px-5 py-2 glass border-t border-gray-200 dark:border-gray-700">
              <div className="flex items-center justify-between text-xs text-gray-600 dark:text-gray-400 mb-1">
                <span className="font-medium">{progressText || 'Processing...'}</span>
                <div className="flex items-center gap-3">
                  {elapsedSeconds > 0 && (
                    <span className="flex items-center gap-1 text-gray-400">
                      <Clock className="w-3 h-3" />
                      {formatElapsed(elapsedSeconds)}
                    </span>
                  )}
                  {etaSeconds !== null && etaSeconds > 0 && (
                    <span className="text-green-600 dark:text-green-400 font-medium">{formatEta(etaSeconds)}</span>
                  )}
                  {loading && (
                    <button type="button" onClick={handleCancel}
                      className="inline-flex items-center gap-1 rounded-lg bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-300 px-2 py-0.5 text-xs font-semibold hover:bg-red-200 transition-colors"
                    >
                      <Ban className="w-3 h-3" />
                      Cancel
                    </button>
                  )}
                  <span>{progress}%</span>
                </div>
              </div>
              <div className="h-1.5 w-full overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
                <div className="h-full bg-green-500 transition-all duration-300" style={{ width: `${progress}%` }} />
              </div>
            </div>
          )}

          {/* Status banner */}
          {statusMessage && (
            <div className={`shrink-0 flex items-center justify-between gap-3 px-5 py-2.5 text-xs font-medium border-t ${
              statusType === 'success'
                ? 'bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300 border-green-200 dark:border-green-800'
                : 'bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300 border-red-200 dark:border-red-800'
            }`}>
              <span>{statusMessage}</span>
              <button type="button" onClick={() => setStatusMessage(null)} className="shrink-0 hover:opacity-70">
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          )}

          {/* Chat composer */}
          <div className="shrink-0 glass border-t border-gray-200 dark:border-gray-700 px-4 py-3">
            {/* File attachment chip */}
            {file && sourceType === 'document' && (
              <div className="flex items-center gap-2 mb-2 flex-wrap">
                <div className="inline-flex items-center gap-1.5 bg-gray-100 dark:bg-gray-700 rounded-xl px-3 py-1.5 text-xs font-medium text-gray-700 dark:text-gray-200 max-w-xs">
                  <FileText className="w-3.5 h-3.5 shrink-0 text-green-600" />
                  <span className="truncate">{file.name}</span>
                  <button type="button" onClick={() => { setFile(null); setFileIngested(false); }} className="ml-1 hover:text-red-500 transition-colors">
                    <X className="w-3 h-3" />
                  </button>
                </div>
                {fileIngested && (
                  <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300">
                    Indexed
                  </span>
                )}
              </div>
            )}

            {/* Input row */}
            <div className="flex items-end gap-2 rounded-2xl border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 px-3 py-2.5">
              {/* Attach file button (document mode only) */}
              {sourceType === 'document' && (
                <label className="shrink-0 cursor-pointer p-1.5 rounded-xl hover:bg-gray-200 dark:hover:bg-gray-700 text-gray-400 hover:text-green-600 transition-colors" title="Attach document">
                  <Paperclip className="w-5 h-5" />
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept=".pdf,application/pdf"
                    onChange={handleFileChange}
                    className="hidden"
                  />
                </label>
              )}

              {/* Textarea */}
              <textarea
                value={question}
                onChange={(e) => {
                  setQuestion(e.target.value);
                  e.target.style.height = 'auto';
                  e.target.style.height = Math.min(e.target.scrollHeight, 160) + 'px';
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey && !loading) {
                    e.preventDefault();
                    runQuery();
                  }
                }}
                rows={1}
                className="flex-1 bg-transparent resize-none text-sm text-gray-900 dark:text-white placeholder-gray-400 focus:outline-none"
                placeholder={sourceType === 'database' ? 'Ask about the loaded database...' : 'Ask about the document...'}
                style={{ minHeight: '24px', maxHeight: '160px' }}
                disabled={loading}
              />

              {/* Cancel / Send */}
              {loading ? (
                <button type="button" onClick={handleCancel}
                  className="shrink-0 p-1.5 rounded-xl bg-red-100 dark:bg-red-900/30 text-red-600 dark:text-red-400 hover:bg-red-200 transition-colors"
                  title="Cancel"
                >
                  <Ban className="w-5 h-5" />
                </button>
              ) : (
                <button type="button" onClick={runQuery}
                  disabled={!question.trim()}
                  className="shrink-0 p-1.5 rounded-xl bg-green-600 text-white hover:bg-green-700 transition-colors disabled:opacity-40"
                  title="Send (Enter)"
                >
                  <Send className="w-5 h-5" />
                </button>
              )}
            </div>
            <p className="mt-1.5 text-[10px] text-gray-400 dark:text-gray-500 text-right">Shift+Enter for new line · Enter to send</p>
          </div>
        </div>
      </div>

      {selectedImage && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm p-4"
          onClick={closeImageViewer}
        >
          <div
            className="w-full max-w-7xl max-h-[92vh] overflow-hidden rounded-3xl border border-white/10 bg-gray-950 shadow-2xl flex flex-col"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="flex items-center justify-between gap-4 border-b border-white/10 px-5 py-3 text-white">
              <div className="min-w-0">
                <div className="truncate text-sm font-semibold">{selectedImage.label || selectedImage.filename}</div>
                {selectedImage.label && (
                  <div className="truncate text-xs text-gray-300">{selectedImage.filename}</div>
                )}
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <button
                  type="button"
                  onClick={() => adjustImageZoom(-0.25)}
                  className="inline-flex items-center justify-center rounded-xl bg-white/10 px-3 py-2 hover:bg-white/20"
                >
                  <ZoomOut className="h-4 w-4" />
                </button>
                <div className="w-14 text-center text-xs text-gray-300">{Math.round(selectedImageZoom * 100)}%</div>
                <button
                  type="button"
                  onClick={() => adjustImageZoom(0.25)}
                  className="inline-flex items-center justify-center rounded-xl bg-white/10 px-3 py-2 hover:bg-white/20"
                >
                  <ZoomIn className="h-4 w-4" />
                </button>
                <button
                  type="button"
                  onClick={closeImageViewer}
                  className="inline-flex items-center justify-center rounded-xl bg-white/10 px-3 py-2 hover:bg-white/20"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            </div>
            <div className="flex-1 overflow-auto bg-black">
              <div className="flex min-h-full min-w-full items-start justify-center p-6">
                <img
                  src={`data:${selectedImage.mime};base64,${selectedImage.data}`}
                  alt={selectedImage.label || selectedImage.filename}
                  className="origin-top object-contain transition-transform duration-150 ease-out"
                  style={{ transform: `scale(${selectedImageZoom})`, maxWidth: '100%', height: 'auto' }}
                />
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default Vision;
