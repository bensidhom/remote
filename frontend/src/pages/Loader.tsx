// Queue an immutable server-authoritative preflight, then explicitly commit
// canonical SNBI snapshots as staged revisions. Publication remains separate.
import React, { useEffect, useRef, useState } from 'react';
import axios from 'axios';
import { AlertCircle, CheckCircle, Database, Download, FileJson, Loader2 } from 'lucide-react';
import { SectionHeader } from '../components/ui';
import { refreshDataGeneration, useDataGenerationEpoch } from '../lib/dataGeneration';

const MIN_DATA_YEAR = 1990;
const MAX_DATA_YEAR = 2100;
const CURRENT_YEAR = new Date().getFullYear();
const MAX_RECORDS = 10_000;
const MAX_FILE_BYTES = 50 * 1024 * 1024;
const PREFLIGHT_PAGE_SIZE = 25;

type JsonObject = Record<string, unknown>;

interface ValidationIssue {
  severity?: string;
  code?: string;
  message?: string;
  json_pointer?: string;
  path?: string;
}

interface RevisionDiff {
  op?: string;
  path?: string;
  old_value?: unknown;
  new_value?: unknown;
}

interface StagedRevision {
  revision_id: string;
  state_code: number;
  bridge_number: string;
  data_year: number;
  revision_number: number;
  content_hash: string;
  outcome: string;
  created_at?: string;
  validation_status?: string;
  validation_summary?: Record<string, unknown>;
  validation_issues?: ValidationIssue[];
  validation?: {
    status?: string;
    summary?: Record<string, unknown>;
    issues?: ValidationIssue[];
  };
  diff?: RevisionDiff[];
  published?: boolean;
}

interface StageResponse {
  batch_id: string;
  data_generation: number;
  revisions: StagedRevision[];
}

type ImportProfile = 'native_snbi' | 'research_historical_import';
type ImportAction = 'create_only' | 'replace_snapshot';
type ValidationPolicy = 'advisory' | 'reject_critical';
type ImportStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';

interface PreflightDiff {
  change_count?: number;
  changes?: RevisionDiff[];
  truncated?: boolean;
}

interface PreflightBridge {
  index?: number | null;
  logical_key?: {
    state_code?: number | null;
    bridge_number?: string | null;
    data_year?: number | null;
  } | null;
  valid?: boolean;
  projected_action?: string;
  validation_status?: string | null;
  content_hash?: string | null;
  base_content_hash?: string | null;
  issue_counts?: Record<string, unknown>;
  issues?: ValidationIssue[];
  eligibility?: {
    status?: string;
    retained_by_profile?: boolean | null;
    profile_id?: string | null;
  };
  diff?: PreflightDiff;
}

interface ImportPreflight {
  valid: boolean;
  data_generation: number;
  submission?: {
    inventory_year?: number;
    state_code?: number;
    agency_id?: string;
    profile_id?: ImportProfile;
  };
  manifest_conflicts?: Array<Record<string, unknown>>;
  summary?: Record<string, unknown>;
  field_loss_check?: {
    status?: string;
    mode?: string;
    unknown_fields?: number | null;
    projection_schema_errors?: string[];
    canonical_is_authoritative?: boolean;
    extension_documents_retained_canonical_only?: number;
  };
  proposed_actions?: {
    stage_only?: boolean;
    publish?: boolean;
    replace_snapshot_count?: number;
  };
  estimated_growth?: Record<string, unknown>;
  bridges?: PreflightBridge[];
}

interface ImportJobResponse {
  import_id: string;
  status: ImportStatus;
  attempts?: number;
  max_attempts?: number;
  progress?: {
    phase?: string;
    completed?: number;
    total?: number;
  };
  error_code?: string;
  preflight_digest?: string;
  preflight?: ImportPreflight;
  idempotent_replay?: boolean;
}

interface ApiErrorDetails {
  code?: string;
  message: string;
}

type ExportKind = 'canonical_profile' | 'restore_original' | 'legacy_projection' | 'human_report';

const EXPORT_PROFILE: Record<ExportKind, string> = {
  canonical_profile: 'research_snbi_export_v1',
  restore_original: 'exact_source_restore_v1',
  legacy_projection: 'legacy_nbi_review_v1',
  human_report: 'human_review_report_v1',
};

const EXPORT_LABEL: Record<ExportKind, string> = {
  canonical_profile: 'Canonical package',
  restore_original: 'Restore originals',
  legacy_projection: 'Legacy review package',
  human_report: 'Human report',
};

const isObject = (value: unknown): value is JsonObject =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const newOperationId = () => {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0'));
  return `${hex.slice(0, 4).join('')}-${hex.slice(4, 6).join('')}-${hex.slice(6, 8).join('')}-${hex.slice(8, 10).join('')}-${hex.slice(10).join('')}`;
};

const apiErrorDetails = (error: unknown, fallback: string): ApiErrorDetails => {
  if (!axios.isAxiosError(error)) return { message: fallback };
  const detail = error.response?.data?.detail;
  if (typeof detail === 'string') return { message: detail };
  if (isObject(detail) && typeof detail.message === 'string') {
    return {
      code: typeof detail.code === 'string' ? detail.code : undefined,
      message: detail.message,
    };
  }
  return { message: fallback };
};

const errorMessage = (error: unknown, fallback: string) =>
  apiErrorDetails(error, fallback).message;

const waitForNextPoll = (signal: AbortSignal, delayMs = 1_000) =>
  new Promise<void>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, delayMs);
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new DOMException('Polling cancelled', 'AbortError'));
    };
    signal.addEventListener('abort', onAbort, { once: true });
  });

const validationDetails = (revision: StagedRevision) => ({
  status: revision.validation_status ?? revision.validation?.status ?? 'not reported',
  summary: revision.validation_summary ?? revision.validation?.summary ?? {},
  issues: revision.validation_issues ?? revision.validation?.issues ?? [],
});

const summaryText = (summary: Record<string, unknown>) => {
  const entries = Object.entries(summary).filter(([, value]) =>
    ['string', 'number', 'boolean'].includes(typeof value),
  );
  return entries.length > 0
    ? entries.map(([key, value]) => `${key.replace(/_/g, ' ')}: ${String(value)}`).join(' | ')
    : 'No validation counts reported.';
};

const Loader: React.FC = () => {
  const [jsonFile, setJsonFile] = useState<File | null>(null);
  const [selectedDataYear, setSelectedDataYear] = useState<number>(CURRENT_YEAR);
  const [stateCode, setStateCode] = useState<number>(34);
  const [agencyId, setAgencyId] = useState('NJDOT');
  const [profileId, setProfileId] = useState<ImportProfile>('native_snbi');
  const [action, setAction] = useState<ImportAction>('create_only');
  const [validationPolicy, setValidationPolicy] = useState<ValidationPolicy>('reject_critical');
  const [overrideReason, setOverrideReason] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [loadedYears, setLoadedYears] = useState<number[]>([]);
  const [dataGeneration, setDataGeneration] = useState<number | null>(null);
  const [importJob, setImportJob] = useState<ImportJobResponse | null>(null);
  const [preflightStale, setPreflightStale] = useState(false);
  const [committing, setCommitting] = useState(false);
  const [preflightPage, setPreflightPage] = useState(0);
  const [stagedRevisions, setStagedRevisions] = useState<StagedRevision[]>([]);
  const [reviewedWarnings, setReviewedWarnings] = useState<Record<string, boolean>>({});
  const [publishing, setPublishing] = useState<Record<string, boolean>>({});
  const [publishErrors, setPublishErrors] = useState<Record<string, string>>({});
  const [exporting, setExporting] = useState<Record<string, boolean>>({});
  const [exportErrors, setExportErrors] = useState<Record<string, string>>({});
  const importKeys = useRef(new Map<string, string>());
  const commitKeys = useRef(new Map<string, string>());
  const publishKeys = useRef<Record<string, string>>({});
  const pollControllerRef = useRef<AbortController | null>(null);
  const activeImportIdRef = useRef<string | null>(null);
  const dataGenerationEpoch = useDataGenerationEpoch();

  const fetchLoadedYears = async (signal?: AbortSignal) => {
    try {
      const response = await axios.get('/api/loaded-years', { signal });
      setLoadedYears((response.data?.years || []).slice().sort((a: number, b: number) => b - a));
    } catch {
      // Published-year badges are informative; revision operations remain available.
    }
  };

  const fetchRevisionState = async (signal?: AbortSignal) => {
    const response = await axios.get('/api/revisions/state', { signal });
    const generation = Number(response.data?.data_generation);
    if (!Number.isInteger(generation) || generation < 0) {
      throw new Error('The server returned an invalid data generation.');
    }
    setDataGeneration(generation);
    return generation;
  };

  useEffect(() => {
    const controller = new AbortController();
    void fetchLoadedYears(controller.signal);
    void fetchRevisionState(controller.signal)
      .then((generation) => {
        const reviewedGeneration = importJob?.preflight?.data_generation;
        if (Number.isInteger(reviewedGeneration) && reviewedGeneration !== generation) {
          setPreflightStale(true);
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setError('The revision service is not available. Please try again shortly.');
        }
      });
    return () => controller.abort();
  }, [dataGenerationEpoch, importJob?.preflight?.data_generation]);

  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    if (!(file.type === 'application/json' || file.name.toLowerCase().endsWith('.json'))) {
      setError('Please select a JSON file.');
      return;
    }
    if (file.size > MAX_FILE_BYTES) {
      setError('The JSON file is larger than the 50 MB browser staging limit.');
      return;
    }
    pollControllerRef.current?.abort();
    pollControllerRef.current = null;
    activeImportIdRef.current = null;
    localStorage.removeItem('bridgeguard_active_import_id');
    setJsonFile(file);
    setImportJob(null);
    setPreflightStale(false);
    setPreflightPage(0);
    setStagedRevisions([]);
    setReviewedWarnings({});
    setPublishErrors({});
    setExportErrors({});
    setError('');
    setMessage('');
  };

  const parseSelectedFile = async () => {
    if (!jsonFile) throw new Error('Please select a JSON file first.');
    let parsed: unknown;
    try {
      parsed = JSON.parse(await jsonFile.text());
    } catch {
      throw new Error('The selected file is not valid JSON.');
    }

    const records = Array.isArray(parsed)
      ? parsed
      : isObject(parsed) && Array.isArray(parsed.bridges)
        ? parsed.bridges
        : null;
    if (!records) throw new Error('Expected a JSON array or a Converter response with a bridges array.');
    if (records.length === 0) throw new Error('The JSON file does not contain any bridge records.');
    if (records.length > MAX_RECORDS) {
      throw new Error(`A batch may contain at most ${MAX_RECORDS.toLocaleString()} bridge records.`);
    }

    records.forEach((record, index) => {
      if (!isObject(record)) throw new Error(`Bridge record ${index + 1} must be a JSON object.`);
      if (record.BL01 === undefined || typeof record.BID01 !== 'string' || !record.BID01.trim()) {
        throw new Error(`Bridge record ${index + 1} must contain BL01 and a non-empty BID01.`);
      }
    });
    return records.length;
  };

  const pollImport = async (importId: string, controller: AbortController) => {
    activeImportIdRef.current = importId;
    pollControllerRef.current?.abort();
    pollControllerRef.current = controller;
    try {
      while (!controller.signal.aborted && activeImportIdRef.current === importId) {
        const response = await axios.get<ImportJobResponse>(
          `/api/imports/${encodeURIComponent(importId)}`,
          { params: { include_bridges: true }, signal: controller.signal },
        );
        const job = response.data;
        if (activeImportIdRef.current !== importId) return;
        setImportJob(job);
        if (job.status === 'succeeded') {
          if (!job.preflight || typeof job.preflight_digest !== 'string') {
            throw new Error('The server completed the job without a verifiable preflight receipt.');
          }
          const submission = job.preflight.submission;
          if (typeof submission?.inventory_year === 'number') setSelectedDataYear(submission.inventory_year);
          if (typeof submission?.state_code === 'number') setStateCode(submission.state_code);
          if (typeof submission?.agency_id === 'string') setAgencyId(submission.agency_id);
          if (submission?.profile_id === 'native_snbi' || submission?.profile_id === 'research_historical_import') {
            setProfileId(submission.profile_id);
          }
          const generation = await fetchRevisionState(controller.signal);
          const stale = job.preflight.data_generation !== generation;
          setPreflightStale(stale);
          setLoading(false);
          setMessage(
            stale
              ? 'Preflight completed, but the inventory changed afterward. Run a new preflight before committing.'
              : `Server preflight completed for ${job.preflight.summary?.total ?? 0} bridge record${job.preflight.summary?.total === 1 ? '' : 's'}.`,
          );
          return;
        }
        if (job.status === 'failed' || job.status === 'cancelled') {
          setLoading(false);
          setError(
            job.status === 'cancelled'
              ? 'The durable import preflight was cancelled.'
              : `The durable import preflight failed${job.error_code ? ` (${job.error_code})` : ''}.`,
          );
          return;
        }
        await waitForNextPoll(controller.signal);
      }
    } catch (pollError) {
      if (controller.signal.aborted || axios.isCancel(pollError)) return;
      setLoading(false);
      setError(
        pollError instanceof Error && !axios.isAxiosError(pollError)
          ? pollError.message
          : errorMessage(pollError, 'The durable import status could not be read.'),
      );
    }
  };

  useEffect(() => {
    const importId = localStorage.getItem('bridgeguard_active_import_id');
    if (!importId) return undefined;
    const controller = new AbortController();
    setLoading(true);
    void pollImport(importId, controller);
    return () => controller.abort();
    // Resume exactly one owner-scoped durable job after navigation/reload.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => () => {
    pollControllerRef.current?.abort();
    activeImportIdRef.current = null;
  }, []);

  const cancelWatching = () => {
    const importId = activeImportIdRef.current;
    pollControllerRef.current?.abort();
    pollControllerRef.current = null;
    activeImportIdRef.current = null;
    setLoading(false);
    if (importId) {
      setMessage(`Stopped watching import ${importId}. The durable server job can be resumed after reload.`);
    }
  };

  const handleStage = async () => {
    if (!jsonFile) {
      setError('Please select a JSON file first.');
      return;
    }
    if (!Number.isInteger(selectedDataYear) || selectedDataYear < MIN_DATA_YEAR || selectedDataYear > MAX_DATA_YEAR) {
      setError(`Please choose a data year between ${MIN_DATA_YEAR} and ${MAX_DATA_YEAR}.`);
      return;
    }
    if (!Number.isInteger(stateCode) || stateCode < 1 || stateCode > 99) {
      setError('Please choose a state code from 1 through 99.');
      return;
    }
    if (!agencyId.trim()) {
      setError('Agency ID is required.');
      return;
    }

    pollControllerRef.current?.abort();
    setLoading(true);
    setError('');
    setMessage('');
    setImportJob(null);
    setPreflightStale(false);
    setPreflightPage(0);
    setStagedRevisions([]);
    try {
      // This browser check is only immediate UX. The untouched file goes to the
      // server, whose normalization, validation, evidence, and diff are authoritative.
      await parseSelectedFile();
      const operationSignature = [
        jsonFile.name,
        jsonFile.size,
        jsonFile.lastModified,
        selectedDataYear,
        stateCode,
        agencyId.trim(),
        profileId,
      ].join(':');
      let idempotencyKey = importKeys.current.get(operationSignature);
      if (!idempotencyKey) {
        idempotencyKey = newOperationId();
        importKeys.current.set(operationSignature, idempotencyKey);
      }
      const formData = new FormData();
      formData.append('file', jsonFile, jsonFile.name);
      formData.append('inventory_year', String(selectedDataYear));
      formData.append('state_code', String(stateCode));
      formData.append('agency_id', agencyId.trim());
      formData.append('profile_id', profileId);
      const response = await axios.post<ImportJobResponse>(
        '/api/imports',
        formData,
        {
          headers: {
            'Idempotency-Key': idempotencyKey,
            'X-Request-ID': newOperationId(),
          },
        },
      );
      const importId = response.data?.import_id;
      if (!importId) throw new Error('The server did not return a durable import ID.');
      setImportJob(response.data);
      localStorage.setItem('bridgeguard_active_import_id', importId);
      setMessage(`Queued durable preflight ${importId}. Server checks are authoritative.`);
      const controller = new AbortController();
      await pollImport(importId, controller);
    } catch (stageError) {
      if (axios.isCancel(stageError)) return;
      setError(
        stageError instanceof Error && !axios.isAxiosError(stageError)
          ? stageError.message
          : errorMessage(stageError, 'The durable import could not be queued.'),
      );
      setLoading(false);
    }
  };

  const commitImport = async () => {
    const job = importJob;
    const preflight = job?.preflight;
    if (!job || job.status !== 'succeeded' || !preflight || !job.preflight_digest) {
      setError('Wait for a complete server preflight before committing.');
      return;
    }
    if (!preflight.valid) {
      setError('This server preflight is invalid and cannot be committed.');
      return;
    }
    if (preflightStale) {
      setError('The inventory changed after this review. Run a new preflight before committing.');
      return;
    }
    if ((preflight.manifest_conflicts?.length ?? 0) > 0 && !overrideReason.trim()) {
      setError('Explain why the server-authoritative metadata should override the embedded manifest.');
      return;
    }

    setCommitting(true);
    setError('');
    setMessage('');
    try {
      const commitSignature = [
        job.import_id,
        job.preflight_digest,
        action,
        validationPolicy,
        overrideReason.trim(),
      ].join(':');
      let idempotencyKey = commitKeys.current.get(commitSignature);
      if (!idempotencyKey) {
        idempotencyKey = newOperationId();
        commitKeys.current.set(commitSignature, idempotencyKey);
      }
      const response = await axios.post<StageResponse>(
        `/api/imports/${encodeURIComponent(job.import_id)}/commit`,
        {
          preflight_digest: job.preflight_digest,
          expected_data_generation: preflight.data_generation,
          action,
          validation_policy: validationPolicy,
          atomic_mode: 'batch',
          override_reason: overrideReason.trim() || null,
        },
        {
          headers: {
            'Idempotency-Key': idempotencyKey,
            'X-Request-ID': newOperationId(),
          },
        },
      );
      const revisions = response.data?.revisions ?? [];
      setStagedRevisions(revisions);
      setDataGeneration(Number(response.data?.data_generation ?? preflight.data_generation));
      localStorage.removeItem('bridgeguard_active_import_id');
      activeImportIdRef.current = null;
      setMessage(
        `Committed ${revisions.length} bridge revision${revisions.length === 1 ? '' : 's'} to staging only. Nothing was published.`,
      );
      await fetchRevisionState();
      refreshDataGeneration();
    } catch (commitError) {
      const details = apiErrorDetails(commitError, 'The reviewed import could not be committed.');
      if (details.code === 'stale_data_generation' || details.code === 'stale_snapshot_preflight') {
        setPreflightStale(true);
        void fetchRevisionState().catch(() => undefined);
        setError('The inventory changed after preflight. No revisions were staged; run a new preflight.');
      } else {
        setError(details.message);
      }
    } finally {
      setCommitting(false);
    }
  };

  const publishRevision = async (revision: StagedRevision) => {
    setPublishing((current) => ({ ...current, [revision.revision_id]: true }));
    setPublishErrors((current) => ({ ...current, [revision.revision_id]: '' }));
    try {
      let etag = '"none"';
      try {
        const current = await axios.get(
          `/api/revisions/snapshots/${revision.state_code}/${encodeURIComponent(revision.bridge_number)}/${revision.data_year}`,
        );
        const currentHash = current.data?.content_hash;
        if (typeof current.headers.etag === 'string' && current.headers.etag) {
          etag = current.headers.etag;
        } else if (typeof currentHash === 'string' && currentHash) {
          etag = `"${currentHash}"`;
        } else {
          throw new Error('The published snapshot did not include a strong ETag.');
        }
      } catch (lookupError) {
        if (!axios.isAxiosError(lookupError) || lookupError.response?.status !== 404) throw lookupError;
      }

      const generation = await fetchRevisionState();
      const idempotencyKey =
        publishKeys.current[revision.revision_id] ||
        (publishKeys.current[revision.revision_id] = newOperationId());
      const response = await axios.post(
        `/api/revisions/${encodeURIComponent(revision.revision_id)}/publish`,
        {
          reason: `Publish reviewed loader batch for data year ${revision.data_year}`,
          expected_data_generation: generation,
          reviewed_research_warnings: Boolean(reviewedWarnings[revision.revision_id]),
        },
        {
          headers: {
            'Idempotency-Key': idempotencyKey,
            'If-Match': etag,
            'X-Request-ID': newOperationId(),
          },
        },
      );
      setDataGeneration(Number(response.data?.data_generation ?? generation));
      setStagedRevisions((current) =>
        current.map((item) =>
          item.revision_id === revision.revision_id ? { ...item, published: true } : item,
        ),
      );
      localStorage.setItem('db_last_updated', Date.now().toString());
      fetchLoadedYears();
      refreshDataGeneration();
    } catch (publishError) {
      setPublishErrors((current) => ({
        ...current,
        [revision.revision_id]: errorMessage(
          publishError,
          'This revision could not be published. Refresh and retry after resolving any conflict.',
        ),
      }));
    } finally {
      setPublishing((current) => ({ ...current, [revision.revision_id]: false }));
    }
  };

  const exportRevision = async (revision: StagedRevision, kind: ExportKind) => {
    const operation = `${revision.revision_id}:${kind}`;
    setExporting((current) => ({ ...current, [operation]: true }));
    setExportErrors((current) => ({ ...current, [revision.revision_id]: '' }));
    try {
      const response = await axios.post(
        `/api/revisions/${encodeURIComponent(revision.revision_id)}/exports`,
        {
          export_kind: kind,
          profile_id: EXPORT_PROFILE[kind],
          agency_id: 'NJDOT',
          exported_at: new Date().toISOString(),
        },
        { responseType: 'blob' },
      );
      const disposition = String(response.headers['content-disposition'] ?? '');
      const filenameMatch = disposition.match(/filename="([^"]+)"/i);
      const filename = filenameMatch?.[1] ?? `bridgeguard-${kind}-${revision.bridge_number}-${revision.data_year}.zip`;
      const url = URL.createObjectURL(response.data as Blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    } catch (exportError) {
      setExportErrors((current) => ({
        ...current,
        [revision.revision_id]: errorMessage(
          exportError,
          kind === 'restore_original'
            ? 'No immutable original source is linked to this revision.'
            : 'The revision export could not be generated.',
        ),
      }));
    } finally {
      setExporting((current) => ({ ...current, [operation]: false }));
    }
  };

  const resetForm = () => {
    pollControllerRef.current?.abort();
    pollControllerRef.current = null;
    activeImportIdRef.current = null;
    localStorage.removeItem('bridgeguard_active_import_id');
    setJsonFile(null);
    setImportJob(null);
    setPreflightStale(false);
    setPreflightPage(0);
    setOverrideReason('');
    setStagedRevisions([]);
    setReviewedWarnings({});
    setPublishErrors({});
    setExportErrors({});
    setError('');
    setMessage('');
  };

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <SectionHeader icon={<Database className="w-6 h-6" />} title="Durable bridge import" />

      <div className="rounded-3xl bg-white border border-slate-200 shadow-sm p-6 space-y-5">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-sm font-semibold text-slate-800">Immutable two-phase review workflow</p>
            <p className="text-xs text-slate-500 mt-1">
              The server retains exact bytes, preflights them asynchronously, and stages only after your explicit commit. Publication is separate.
            </p>
          </div>
          {dataGeneration !== null && (
            <span className="shrink-0 rounded-full bg-slate-100 px-3 py-1 text-xs font-medium text-slate-600">
              Data version {dataGeneration}
            </span>
          )}
        </div>

        <label className="flex flex-col items-center justify-center gap-3 rounded-2xl border-2 border-dashed border-slate-300 hover:border-brand-400 hover:bg-slate-50 transition-colors cursor-pointer py-10 px-6 text-center">
          <FileJson className="w-10 h-10 text-slate-400" />
          <span className="text-sm font-semibold text-slate-700">
            {jsonFile ? jsonFile.name : 'Click to select an SNBI JSON file'}
          </span>
          <span className="text-xs text-slate-400">
            JSON array or Converter output, up to {MAX_RECORDS.toLocaleString()} bridges
          </span>
          <input type="file" accept=".json,application/json" onChange={handleFileChange} disabled={loading || committing} className="hidden" />
        </label>

        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <div>
            <label htmlFor="inventory-year" className="block text-sm font-medium text-slate-700 mb-1.5">
              Inventory year
            </label>
            <input
              id="inventory-year"
              type="number"
              min={MIN_DATA_YEAR}
              max={MAX_DATA_YEAR}
              value={selectedDataYear}
              onChange={(event) => setSelectedDataYear(Number(event.target.value))}
              disabled={loading || committing}
              className="w-full rounded-xl border border-slate-300 bg-white px-4 py-2.5 text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500"
            />
          </div>
          <div>
            <label htmlFor="state-code" className="block text-sm font-medium text-slate-700 mb-1.5">
              State code
            </label>
            <input
              id="state-code"
              type="number"
              min={1}
              max={99}
              value={stateCode}
              onChange={(event) => setStateCode(Number(event.target.value))}
              disabled={loading || committing}
              className="w-full rounded-xl border border-slate-300 bg-white px-4 py-2.5 text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500"
            />
          </div>
          <div>
            <label htmlFor="agency-id" className="block text-sm font-medium text-slate-700 mb-1.5">
              Agency ID
            </label>
            <input
              id="agency-id"
              type="text"
              maxLength={128}
              value={agencyId}
              onChange={(event) => setAgencyId(event.target.value)}
              disabled={loading || committing}
              className="w-full rounded-xl border border-slate-300 bg-white px-4 py-2.5 text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500"
            />
          </div>
          <div>
            <label htmlFor="import-profile" className="block text-sm font-medium text-slate-700 mb-1.5">
              Import profile
            </label>
            <select
              id="import-profile"
              value={profileId}
              onChange={(event) => setProfileId(event.target.value as ImportProfile)}
              disabled={loading || committing}
              className="w-full rounded-xl border border-slate-300 bg-white px-4 py-2.5 text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500"
            >
              <option value="native_snbi">Native SNBI</option>
              <option value="research_historical_import">Historical NBI transition</option>
            </select>
          </div>
          <div>
            <label htmlFor="import-action" className="block text-sm font-medium text-slate-700 mb-1.5">
              Commit action
            </label>
            <select
              id="import-action"
              value={action}
              onChange={(event) => setAction(event.target.value as ImportAction)}
              disabled={loading || committing}
              className="w-full rounded-xl border border-slate-300 bg-white px-4 py-2.5 text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500"
            >
              <option value="create_only">Create only</option>
              <option value="replace_snapshot">Add revision to existing snapshot</option>
            </select>
          </div>
          <div>
            <label htmlFor="validation-policy" className="block text-sm font-medium text-slate-700 mb-1.5">
              Validation policy
            </label>
            <select
              id="validation-policy"
              value={validationPolicy}
              onChange={(event) => setValidationPolicy(event.target.value as ValidationPolicy)}
              disabled={loading || committing}
              className="w-full rounded-xl border border-slate-300 bg-white px-4 py-2.5 text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500"
            >
              <option value="reject_critical">Reject critical/error issues</option>
              <option value="advisory">Advisory review</option>
            </select>
          </div>
          <div>
            <label htmlFor="atomic-mode" className="block text-sm font-medium text-slate-700 mb-1.5">
              Atomic mode
            </label>
            <input
              id="atomic-mode"
              type="text"
              value="Entire batch (all or nothing)"
              readOnly
              aria-readonly="true"
              className="w-full rounded-xl border border-slate-200 bg-slate-100 px-4 py-2.5 text-slate-600"
            />
          </div>
        </div>

        <div className="flex flex-col gap-2 sm:flex-row">
          <button
            onClick={handleStage}
            disabled={!jsonFile || loading || committing || selectedDataYear < MIN_DATA_YEAR || selectedDataYear > MAX_DATA_YEAR || stateCode < 1 || stateCode > 99 || !agencyId.trim()}
            className="flex-1 inline-flex items-center justify-center gap-2 rounded-xl bg-brand-600 hover:bg-brand-700 text-white font-semibold py-2.5 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {loading ? <Loader2 className="w-5 h-5 animate-spin" /> : <Database className="w-5 h-5" />}
            {loading ? 'Running server preflight...' : 'Run server preflight'}
          </button>
          {loading && (
            <button
              type="button"
              onClick={cancelWatching}
              className="rounded-xl border border-slate-300 bg-white px-5 py-2.5 text-sm font-semibold text-slate-700 hover:bg-slate-50"
            >
              Stop watching
            </button>
          )}
        </div>

        {importJob && (
          <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-xs text-slate-600">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-semibold text-slate-800">Import {importJob.import_id}</span>
              <span className="rounded-full bg-white px-2.5 py-1 font-semibold uppercase tracking-wide">
                {importJob.status}
              </span>
            </div>
            <p className="mt-2">
              Phase: {importJob.progress?.phase ?? importJob.status}
              {Number.isFinite(importJob.progress?.completed) && Number.isFinite(importJob.progress?.total)
                ? ` | ${importJob.progress?.completed}/${importJob.progress?.total}`
                : ''}
              {typeof importJob.attempts === 'number' ? ` | attempt ${importJob.attempts}/${importJob.max_attempts ?? '?'}` : ''}
            </p>
          </div>
        )}
      </div>

      {error && (
        <div className="rounded-2xl bg-red-50 border border-red-200 p-4 flex gap-3">
          <AlertCircle className="w-5 h-5 text-red-600 mt-0.5 shrink-0" />
          <p className="text-sm text-red-700">{error}</p>
        </div>
      )}

      {message && (
        <div className={`rounded-2xl border p-4 flex items-center gap-3 ${preflightStale ? 'bg-amber-50 border-amber-200' : 'bg-emerald-50 border-emerald-200'}`}>
          {preflightStale
            ? <AlertCircle className="w-5 h-5 text-amber-600 shrink-0" />
            : <CheckCircle className="w-5 h-5 text-emerald-600 shrink-0" />}
          <p className={`text-sm font-medium ${preflightStale ? 'text-amber-800' : 'text-emerald-800'}`}>{message}</p>
          <button onClick={resetForm} className="ml-auto text-sm rounded-lg px-3 py-1.5 bg-white/80 hover:bg-white text-slate-700">
            Select another
          </button>
        </div>
      )}

      {importJob?.status === 'succeeded' && importJob.preflight && (
        <section className="rounded-3xl bg-white border border-slate-200 shadow-sm p-6 space-y-5">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <h2 className="text-base font-semibold text-slate-900">Server-authoritative preflight</h2>
              <p className="mt-1 text-xs text-slate-500">
                Receipt {importJob.preflight_digest?.slice(0, 16)}… | reviewed against data version {importJob.preflight.data_generation}
              </p>
            </div>
            <span className={`rounded-full px-3 py-1 text-xs font-semibold ${importJob.preflight.valid && !preflightStale ? 'bg-emerald-100 text-emerald-700' : 'bg-red-100 text-red-700'}`}>
              {preflightStale ? 'stale review' : importJob.preflight.valid ? 'committable' : 'blocked'}
            </span>
          </div>

          <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
            <p className="text-xs font-semibold text-slate-700">Inventory and action summary</p>
            <p className="mt-1 text-xs text-slate-600">{summaryText(importJob.preflight.summary ?? {})}</p>
            <p className="mt-2 text-xs text-slate-500">
              Submission: state {importJob.preflight.submission?.state_code ?? 'unknown'} | year {importJob.preflight.submission?.inventory_year ?? 'unknown'} | agency {importJob.preflight.submission?.agency_id ?? 'unknown'} | profile {importJob.preflight.submission?.profile_id ?? 'unknown'}
            </p>
          </div>

          <div className={`rounded-xl border p-3 ${importJob.preflight.field_loss_check?.status === 'passed' ? 'border-emerald-200 bg-emerald-50' : 'border-red-200 bg-red-50'}`}>
            <p className="text-xs font-semibold text-slate-800">
              Field-loss check: {importJob.preflight.field_loss_check?.status ?? 'not reported'}
            </p>
            <p className="mt-1 text-xs text-slate-600">
              Canonical data is authoritative. Unknown fields: {importJob.preflight.field_loss_check?.unknown_fields ?? 'not run'} | canonical-only extension documents: {importJob.preflight.field_loss_check?.extension_documents_retained_canonical_only ?? 0}
            </p>
            {(importJob.preflight.field_loss_check?.projection_schema_errors?.length ?? 0) > 0 && (
              <ul className="mt-2 space-y-1 text-xs text-red-700">
                {importJob.preflight.field_loss_check?.projection_schema_errors?.slice(0, 20).map((issue, index) => (
                  <li key={`field-loss-${index}`}>{issue}</li>
                ))}
              </ul>
            )}
          </div>

          {(importJob.preflight.manifest_conflicts?.length ?? 0) > 0 && (
            <div className="rounded-xl border border-amber-200 bg-amber-50 p-3">
              <p className="text-xs font-semibold text-amber-900">Embedded manifest conflicts</p>
              <ul className="mt-2 max-h-28 space-y-1 overflow-auto font-mono text-xs text-amber-900">
                {importJob.preflight.manifest_conflicts?.slice(0, 20).map((conflict, index) => (
                  <li key={`manifest-conflict-${index}`}>{JSON.stringify(conflict)}</li>
                ))}
              </ul>
              <label htmlFor="override-reason" className="mt-3 block text-xs font-semibold text-amber-900">
                Required override reason
              </label>
              <textarea
                id="override-reason"
                value={overrideReason}
                onChange={(event) => setOverrideReason(event.target.value)}
                maxLength={2_000}
                rows={3}
                className="mt-1 w-full rounded-lg border border-amber-300 bg-white px-3 py-2 text-sm text-slate-800 focus:outline-none focus:ring-2 focus:ring-amber-500"
              />
            </div>
          )}

          <div className="space-y-3">
            <div className="flex items-center justify-between gap-3">
              <h3 className="text-sm font-semibold text-slate-800">Per-bridge review</h3>
              <span className="text-xs text-slate-500">Actions, eligibility, issues, and bounded field diffs</span>
            </div>
            {(importJob.preflight.bridges ?? [])
              .slice(preflightPage * PREFLIGHT_PAGE_SIZE, (preflightPage + 1) * PREFLIGHT_PAGE_SIZE)
              .map((bridge, pageIndex) => {
              const bridgeIndex = (preflightPage * PREFLIGHT_PAGE_SIZE) + pageIndex;
              const logicalKey = bridge.logical_key;
              const issues = bridge.issues ?? [];
              const diff = bridge.diff;
              return (
                <article key={`${logicalKey?.bridge_number ?? 'invalid'}-${bridge.index ?? bridgeIndex}`} className="rounded-xl border border-slate-200 p-4 space-y-3">
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                    <div>
                      <p className="text-sm font-semibold text-slate-900">
                        {logicalKey?.bridge_number ?? `Invalid record ${bridgeIndex + 1}`}
                      </p>
                      <p className="mt-1 text-xs text-slate-500">
                        {logicalKey?.state_code != null ? `State ${logicalKey.state_code} | ` : ''}
                        {logicalKey?.data_year ?? selectedDataYear}
                        {bridge.validation_status ? ` | validation ${bridge.validation_status}` : ''}
                      </p>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <span className="rounded-full bg-brand-50 px-2.5 py-1 text-xs font-semibold text-brand-700">
                        action: {bridge.projected_action ?? 'unknown'}
                      </span>
                      <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-semibold text-slate-700">
                        eligibility: {bridge.eligibility?.status ?? 'unknown'}
                      </span>
                    </div>
                  </div>

                  <div className="grid gap-3 sm:grid-cols-2">
                    <div className="rounded-lg bg-slate-50 p-3">
                      <p className="text-xs font-semibold text-slate-700">Validation issues ({issues.length})</p>
                      {issues.length > 0 ? (
                        <ul className="mt-2 max-h-32 space-y-1 overflow-auto text-xs text-slate-700">
                          {issues.slice(0, 30).map((issue, index) => (
                            <li key={`${issue.code ?? 'issue'}-${index}`}>
                              {issue.severity ? `${issue.severity.toUpperCase()} ` : ''}
                              {issue.code ? `${issue.code}: ` : ''}
                              {issue.message ?? 'Validation issue'}
                              {(issue.json_pointer || issue.path) ? ` (${issue.json_pointer || issue.path})` : ''}
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <p className="mt-1 text-xs text-slate-500">No server issues reported.</p>
                      )}
                    </div>
                    <div className="rounded-lg bg-slate-50 p-3">
                      <p className="text-xs font-semibold text-slate-700">
                        Field diff ({diff?.change_count ?? 0} changes{diff?.truncated ? ', truncated' : ''})
                      </p>
                      {(diff?.changes?.length ?? 0) > 0 ? (
                        <ul className="mt-2 max-h-32 space-y-1 overflow-auto font-mono text-xs text-slate-600">
                          {diff?.changes?.map((change, index) => (
                            <li key={`${change.path ?? '/'}-${index}`}>{change.op ?? 'change'} {change.path ?? '/'}</li>
                          ))}
                        </ul>
                      ) : (
                        <p className="mt-1 text-xs text-slate-500">
                          {bridge.projected_action === 'insert' ? 'New logical snapshot; there is no previous revision.' : 'No canonical field changes.'}
                        </p>
                      )}
                    </div>
                  </div>
                </article>
              );
            })}
            {(importJob.preflight.bridges?.length ?? 0) > PREFLIGHT_PAGE_SIZE && (
              <div className="flex items-center justify-between gap-3 rounded-xl bg-slate-50 px-3 py-2 text-xs text-slate-600">
                <span>
                  Showing {(preflightPage * PREFLIGHT_PAGE_SIZE) + 1}–{Math.min((preflightPage + 1) * PREFLIGHT_PAGE_SIZE, importJob.preflight.bridges?.length ?? 0)} of {importJob.preflight.bridges?.length ?? 0}
                </span>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => setPreflightPage((page) => Math.max(0, page - 1))}
                    disabled={preflightPage === 0}
                    className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 font-semibold disabled:opacity-50"
                  >
                    Previous
                  </button>
                  <button
                    type="button"
                    onClick={() => setPreflightPage((page) => page + 1)}
                    disabled={(preflightPage + 1) * PREFLIGHT_PAGE_SIZE >= (importJob.preflight.bridges?.length ?? 0)}
                    className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 font-semibold disabled:opacity-50"
                  >
                    Next
                  </button>
                </div>
              </div>
            )}
          </div>

          {action === 'create_only' && (importJob.preflight.proposed_actions?.replace_snapshot_count ?? 0) > 0 && (
            <p className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
              This preflight contains {importJob.preflight.proposed_actions?.replace_snapshot_count} existing snapshot change(s). Choose “Add revision to existing snapshot” or the atomic commit will fail without writing anything.
            </p>
          )}

          <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
            <p className="flex-1 text-xs text-slate-500">
              Commit is all-or-nothing and creates staged revisions only. It does not publish or activate bridge data.
            </p>
            <button
              type="button"
              onClick={commitImport}
              disabled={committing || preflightStale || !importJob.preflight.valid || ((importJob.preflight.manifest_conflicts?.length ?? 0) > 0 && !overrideReason.trim())}
              className="inline-flex items-center justify-center gap-2 rounded-xl bg-brand-600 px-5 py-2.5 text-sm font-semibold text-white hover:bg-brand-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {committing && <Loader2 className="h-4 w-4 animate-spin" />}
              {committing ? 'Committing atomically...' : 'Commit to staging only'}
            </button>
          </div>
        </section>
      )}

      {stagedRevisions.length > 0 && (
        <div className="space-y-3">
          <div className="flex items-center justify-between gap-4 px-1">
            <h2 className="text-sm font-semibold text-slate-800">Staged revisions</h2>
            <span className="text-xs text-slate-500">Publish each bridge only after reviewing its evidence.</span>
          </div>
          {stagedRevisions.map((revision) => {
            const validation = validationDetails(revision);
            const isPublishing = Boolean(publishing[revision.revision_id]);
            return (
              <article key={revision.revision_id} className="rounded-2xl bg-white border border-slate-200 shadow-sm p-5 space-y-4">
                <div className="flex flex-col sm:flex-row sm:items-start gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <h3 className="font-semibold text-slate-900 break-all">{revision.bridge_number}</h3>
                      <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-600">
                        {revision.data_year}
                      </span>
                      <span className="rounded-full bg-brand-50 px-2.5 py-1 text-xs font-medium text-brand-700">
                        {revision.outcome}
                      </span>
                      {revision.published && (
                        <span className="rounded-full bg-emerald-100 px-2.5 py-1 text-xs font-medium text-emerald-700">
                          published
                        </span>
                      )}
                    </div>
                    <p className="mt-1 text-xs text-slate-500">
                      State {revision.state_code} | revision {revision.revision_number} | {revision.content_hash.slice(0, 12)}
                    </p>
                  </div>
                  <span className="rounded-full bg-amber-50 px-3 py-1 text-xs font-semibold text-amber-800">
                    Validation: {validation.status}
                  </span>
                </div>

                <div className="rounded-xl border border-amber-200 bg-amber-50 p-3">
                  <p className="text-xs font-semibold text-amber-900">Server validation</p>
                  <p className="mt-1 text-xs text-amber-800">{summaryText(validation.summary)}</p>
                  {validation.issues.length > 0 && (
                    <ul className="mt-2 max-h-32 space-y-1 overflow-auto text-xs text-amber-900">
                      {validation.issues.slice(0, 20).map((issue, index) => (
                        <li key={`${issue.code ?? 'issue'}-${index}`}>
                          {issue.severity ? `${issue.severity.toUpperCase()} ` : ''}
                          {issue.code ? `${issue.code}: ` : ''}
                          {issue.message ?? 'Validation issue'}
                          {(issue.json_pointer || issue.path) ? ` (${issue.json_pointer || issue.path})` : ''}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>

                <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
                  <p className="text-xs font-semibold text-slate-700">
                    Deterministic diff ({revision.diff?.length ?? 0} changes)
                  </p>
                  {revision.diff && revision.diff.length > 0 ? (
                    <ul className="mt-2 max-h-28 space-y-1 overflow-auto font-mono text-xs text-slate-600">
                      {revision.diff.slice(0, 30).map((change, index) => (
                        <li key={`${change.path ?? 'change'}-${index}`}>
                          {change.op ?? 'change'} {change.path ?? '/'}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="mt-1 text-xs text-slate-500">
                      {revision.outcome === 'noop' ? 'Canonical content is identical to the existing revision.' : 'No prior revision to compare.'}
                    </p>
                  )}
                </div>

                <div className="rounded-xl border border-slate-200 bg-white p-3">
                  <p className="text-xs font-semibold text-slate-700">Revision export packages</p>
                  <p className="mt-1 text-xs text-slate-500">
                    Original restoration returns only immutable source bytes. Legacy output is a separately labeled, lossy review projection with a loss report.
                  </p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    {(Object.keys(EXPORT_PROFILE) as ExportKind[]).map((kind) => {
                      const operation = `${revision.revision_id}:${kind}`;
                      const isExporting = Boolean(exporting[operation]);
                      return (
                        <button
                          key={kind}
                          type="button"
                          onClick={() => exportRevision(revision, kind)}
                          disabled={isExporting}
                          className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          {isExporting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
                          {EXPORT_LABEL[kind]}
                        </button>
                      );
                    })}
                  </div>
                </div>

                {!revision.published && (
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
                    <label className="flex flex-1 items-start gap-2 text-xs text-slate-600">
                      <input
                        type="checkbox"
                        checked={Boolean(reviewedWarnings[revision.revision_id])}
                        onChange={(event) =>
                          setReviewedWarnings((current) => ({
                            ...current,
                            [revision.revision_id]: event.target.checked,
                          }))
                        }
                        className="mt-0.5 rounded border-slate-300 text-brand-600 focus:ring-brand-500"
                      />
                      I reviewed the research-profile warnings and accept them for publication.
                    </label>
                    <button
                      onClick={() => publishRevision(revision)}
                      disabled={isPublishing}
                      className="inline-flex items-center justify-center gap-2 rounded-xl bg-brand-600 px-5 py-2.5 text-sm font-semibold text-white hover:bg-brand-700 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {isPublishing && <Loader2 className="h-4 w-4 animate-spin" />}
                      {isPublishing ? 'Publishing...' : 'Publish revision'}
                    </button>
                  </div>
                )}
                {publishErrors[revision.revision_id] && (
                  <p className="rounded-xl bg-red-50 p-3 text-xs text-red-700">
                    {publishErrors[revision.revision_id]}
                  </p>
                )}
                {exportErrors[revision.revision_id] && (
                  <p className="rounded-xl bg-red-50 p-3 text-xs text-red-700">
                    {exportErrors[revision.revision_id]}
                  </p>
                )}
              </article>
            );
          })}
        </div>
      )}

      {loadedYears.length > 0 && (
        <div className="rounded-3xl bg-white border border-slate-200 shadow-sm p-6">
          <p className="text-sm font-semibold text-slate-700 mb-3">Published data years</p>
          <div className="flex flex-wrap gap-2">
            {loadedYears.map((year) => (
              <span key={year} className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-1.5 text-sm font-medium text-slate-700">
                {year}
              </span>
            ))}
          </div>
          <p className="text-xs text-slate-400 mt-3">Historical revisions remain retained and auditable.</p>
        </div>
      )}
    </div>
  );
};

export default Loader;
