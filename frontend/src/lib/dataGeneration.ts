import { useEffect, useState } from 'react';

export const DATA_GENERATION_CHANGE_EVENT = 'bridgeguard:data-generation-change';

const DEFAULT_POLL_INTERVAL_MS = 15_000;
const DEFAULT_MAX_BACKOFF_MS = 120_000;
const MIN_POLL_INTERVAL_MS = 5_000;

export interface DataGenerationState {
  workspace_id: string;
  data_generation: number;
  schema_generation: number;
}

export interface DataGenerationChange {
  previous: DataGenerationState | null;
  current: DataGenerationState;
  observed_at: string;
  source: 'poll' | 'resume' | 'manual';
}

interface ObserverOptions {
  expectedWorkspaceId: string;
  pollIntervalMs?: number;
  maxBackoffMs?: number;
}

let observedState: DataGenerationState | null = null;
let stopActiveObserver: (() => void) | null = null;
let refreshActiveObserver: (() => void) | null = null;

const isDocumentHidden = (): boolean => document.visibilityState === 'hidden';

const sameGeneration = (left: DataGenerationState, right: DataGenerationState): boolean =>
  left.workspace_id === right.workspace_id &&
  left.data_generation === right.data_generation &&
  left.schema_generation === right.schema_generation;

export const parseDataGenerationState = (
  payload: unknown,
  expectedWorkspaceId: string,
): DataGenerationState => {
  if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) {
    throw new Error('The revision service returned an invalid state document.');
  }
  const candidate = payload as Record<string, unknown>;
  if (
    typeof candidate.workspace_id !== 'string' ||
    typeof candidate.data_generation !== 'number' ||
    typeof candidate.schema_generation !== 'number'
  ) {
    throw new Error('The revision service returned an invalid state document.');
  }
  const state: DataGenerationState = {
    workspace_id: candidate.workspace_id,
    data_generation: candidate.data_generation,
    schema_generation: candidate.schema_generation,
  };
  if (
    !state.workspace_id ||
    state.workspace_id !== expectedWorkspaceId ||
    !Number.isSafeInteger(state.data_generation) ||
    state.data_generation < 0 ||
    !Number.isSafeInteger(state.schema_generation) ||
    state.schema_generation < 0
  ) {
    throw new Error('The revision service returned an unrelated or invalid generation.');
  }
  return state;
};

const acceptObservedState = (
  current: DataGenerationState,
  source: DataGenerationChange['source'],
): void => {
  const previous = observedState;
  observedState = current;
  if (previous && sameGeneration(previous, current)) return;
  window.dispatchEvent(new CustomEvent<DataGenerationChange>(DATA_GENERATION_CHANGE_EVENT, {
    detail: {
      previous,
      current,
      observed_at: new Date().toISOString(),
      source,
    },
  }));
};

export const onDataGenerationChange = (
  listener: (change: DataGenerationChange) => void,
): (() => void) => {
  const handler = (event: Event) => {
    const detail = (event as CustomEvent<DataGenerationChange>).detail;
    if (detail?.current && Object.prototype.hasOwnProperty.call(detail, 'previous')) listener(detail);
  };
  window.addEventListener(DATA_GENERATION_CHANGE_EVENT, handler as EventListener);
  return () => window.removeEventListener(DATA_GENERATION_CHANGE_EVENT, handler as EventListener);
};

/**
 * Subscribe to the one app-level observer without starting another poller.
 * The epoch starts at zero and increments after initial synchronization and
 * every later generation change.
 */
export const useDataGenerationEpoch = (): number => {
  const [epoch, setEpoch] = useState(0);
  useEffect(() => onDataGenerationChange(() => setEpoch((value) => value + 1)), []);
  return epoch;
};

/** Ask the singleton observer to check now (for example after a local publish). */
export const refreshDataGeneration = (): void => {
  refreshActiveObserver?.();
};

/**
 * Start the sole authenticated generation observer for the signed-in workspace.
 * Polling pauses while the document is hidden, resumes immediately when visible,
 * never overlaps, and backs off to a bounded cadence after transient failures.
 */
export const startDataGenerationObserver = (options: ObserverOptions): (() => void) => {
  stopActiveObserver?.();

  const expectedWorkspaceId = options.expectedWorkspaceId.trim();
  if (!expectedWorkspaceId) throw new Error('A workspace is required for generation observation.');
  if (observedState?.workspace_id !== expectedWorkspaceId) observedState = null;

  const pollIntervalMs = Math.max(MIN_POLL_INTERVAL_MS, options.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS);
  const maxBackoffMs = Math.max(pollIntervalMs, options.maxBackoffMs ?? DEFAULT_MAX_BACKOFF_MS);
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let controller: AbortController | null = null;
  let consecutiveFailures = 0;
  let inFlight = false;
  let runAfterFlight: DataGenerationChange['source'] | null = null;

  const clearTimer = () => {
    if (timer !== null) clearTimeout(timer);
    timer = null;
  };

  const schedule = (delayMs: number) => {
    clearTimer();
    if (stopped || isDocumentHidden()) return;
    timer = setTimeout(() => { void check('poll'); }, delayMs);
  };

  const check = async (source: DataGenerationChange['source']) => {
    if (stopped || inFlight || isDocumentHidden()) return;
    inFlight = true;
    controller = new AbortController();
    try {
      const response = await fetch('/api/revisions/state', {
        method: 'GET',
        cache: 'no-store',
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`Revision state request failed (${response.status}).`);
      acceptObservedState(parseDataGenerationState(await response.json(), expectedWorkspaceId), source);
      consecutiveFailures = 0;
    } catch (error) {
      if (!(error instanceof DOMException && error.name === 'AbortError')) {
        consecutiveFailures = Math.min(consecutiveFailures + 1, 8);
      }
    } finally {
      controller = null;
      inFlight = false;
      if (runAfterFlight && !stopped && !isDocumentHidden()) {
        const queuedSource = runAfterFlight;
        runAfterFlight = null;
        void check(queuedSource);
        return;
      }
      const backoff = Math.min(
        maxBackoffMs,
        pollIntervalMs * (2 ** Math.min(consecutiveFailures, 3)),
      );
      schedule(backoff);
    }
  };

  const requestRefresh = () => {
    if (stopped || isDocumentHidden()) return;
    clearTimer();
    if (inFlight) {
      runAfterFlight = 'manual';
      return;
    }
    void check('manual');
  };

  const onVisibilityChange = () => {
    if (isDocumentHidden()) {
      clearTimer();
      controller?.abort();
      return;
    }
    clearTimer();
    if (inFlight) runAfterFlight = 'resume';
    else void check('resume');
  };

  document.addEventListener('visibilitychange', onVisibilityChange);
  refreshActiveObserver = requestRefresh;
  if (!isDocumentHidden()) void check('poll');

  const stop = () => {
    if (stopped) return;
    stopped = true;
    clearTimer();
    controller?.abort();
    runAfterFlight = null;
    document.removeEventListener('visibilitychange', onVisibilityChange);
    if (refreshActiveObserver === requestRefresh) refreshActiveObserver = null;
    if (stopActiveObserver === stop) stopActiveObserver = null;
  };
  stopActiveObserver = stop;
  return stop;
};
