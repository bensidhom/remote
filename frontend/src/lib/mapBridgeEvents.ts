import type { MapBridge, MapBridgeFocus } from '../components/ArcgisMap';
import {
  DATA_GENERATION_CHANGE_EVENT,
  type DataGenerationChange,
} from './dataGeneration';

export const SHOW_BRIDGE_ON_MAP_EVENT = 'bridgeguard:show-bridge-on-map';
export const SHOW_BRIDGE_ON_MAP_RESULT_EVENT = 'bridgeguard:show-bridge-on-map-result';
export const CANCEL_SHOW_BRIDGE_ON_MAP_EVENT = 'bridgeguard:cancel-show-bridge-on-map';
export const MAP_DATA_REVISION_EVENT = 'bridgeguard:map-data-revision';

const SERVER_STATE = 34;
const REQUEST_TIMEOUT_MS = 30_000;
const MAX_CACHE_ENTRIES = 100;

export interface MapResolveCandidate extends MapBridge {
  state_code: number;
  bridge_number: string;
  bridge_id: string;
  snapshot_id: string;
  snapshot_revision_id?: string | null;
  inventory_revision?: string | null;
  identity_source: 'published' | 'legacy_projection';
  identity_status: 'published' | 'legacy_projection_unversioned';
  revision_number?: number | null;
  canonical_sha256?: string | null;
  data_revision: number;
  schema_generation: number;
  snapshot_year: number;
  coordinates: { latitude: number; longitude: number };
  county_name?: string | null;
  label: string;
  match_basis: 'bridge_number' | 'approved_alias' | 'controlled_text';
  match_score: number;
}

export interface MapResolveResponse {
  status: 'exact' | 'ambiguous' | 'not_found';
  workspace_id: string;
  state_code: number;
  requested_year?: number | null;
  data_generation: number;
  schema_generation: number;
  candidate_count: number;
  inventory_revision?: string | null;
  bridge?: MapResolveCandidate | null;
  candidates: MapResolveCandidate[];
}

export interface ShowBridgeOnMapRequest {
  requestId: string;
  resolution: MapResolveResponse;
  bridge: MapResolveCandidate;
  focus: MapBridgeFocus;
}

export interface ShowBridgeMatch {
  bridge_number: string;
  label: string;
  county_code?: string | number;
  county_name?: string | null;
  snapshot_year: number;
  bridge_id: string;
  snapshot_id: string;
  inventory_revision?: string | null;
}

export interface ShowBridgeOnMapResult {
  requestId: string;
  status: 'shown' | 'not_found' | 'ambiguous' | 'error' | 'stopped';
  message: string;
  bridge?: MapResolveCandidate;
  matches?: ShowBridgeMatch[];
  resolution?: MapResolveResponse;
}

export interface MapRevisionEvent {
  workspace_id: string;
  data_generation: number;
  schema_generation: number;
}

export interface MapRequestOptions {
  year?: number;
  signal?: AbortSignal;
}

const resolutionCache = new Map<string, MapResolveResponse>();
let observedRevision: MapRevisionEvent | null = null;

const newRequestId = (): string =>
  globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;

const bridgeIdentifier = (question: string): string | null => {
  const candidates = question.toUpperCase().match(/\b(?=[A-Z0-9-]{4,20}\b)(?=[A-Z0-9-]*\d)[A-Z0-9-]+\b/g) || [];
  return candidates.find((value) => !/^(?:19|20)\d{2}$/.test(value)) || null;
};

export const isMapRequest = (question: string): boolean => {
  const action = /\b(?:show|find|locate|centre|center|zoom|where\s+is|take\s+me\s+to)\b/i.test(question);
  if (!action) return false;
  return /\bmap\b/i.test(question) || /\bbridge\b/i.test(question) || bridgeIdentifier(question) !== null;
};

// Backward-compatible export for the sidebar while both Bridgette surfaces use
// the same detector and resolver implementation.
export const isSidebarMapRequest = isMapRequest;

const normalizeCacheQuery = (value: string): string => value
  .normalize('NFKC')
  .toLocaleLowerCase('en-US')
  .replace(/[^a-z0-9]+/g, ' ')
  .replace(/\s+/g, ' ')
  .trim();

const explicitInventoryYear = (question: string): number | undefined => {
  const match = question.match(/\b(?:inventory\s+year|year|in|for)\s+((?:19|20)\d{2})\b/i);
  if (!match) return undefined;
  const year = Number(match[1]);
  return Number.isSafeInteger(year) && year >= 1900 && year <= 2100 ? year : undefined;
};

const applyObservedRevision = (revision: MapRevisionEvent): void => {
  const changed = observedRevision !== null && (
    revision.workspace_id !== observedRevision.workspace_id ||
    revision.data_generation !== observedRevision.data_generation ||
    revision.schema_generation !== observedRevision.schema_generation
  );
  observedRevision = revision;
  if (!changed) return;
  resolutionCache.clear();
  window.dispatchEvent(new CustomEvent<MapRevisionEvent>(MAP_DATA_REVISION_EVENT, { detail: revision }));
};

// The app-level observer is the authoritative cross-client signal. Preserve the
// map-specific event for navigation cancellation and map refetches, and clear
// Bridgette's bounded resolver cache before another answer can reuse it.
const onSharedGenerationChange = (event: Event): void => {
  const change = (event as CustomEvent<DataGenerationChange>).detail;
  const current = change?.current;
  if (!current || !Number.isSafeInteger(current.data_generation) || !Number.isSafeInteger(current.schema_generation)) return;
  const revision: MapRevisionEvent = {
    workspace_id: current.workspace_id,
    data_generation: current.data_generation,
    schema_generation: current.schema_generation,
  };
  if (change.previous !== null && observedRevision && !(
    revision.workspace_id !== observedRevision.workspace_id ||
    revision.data_generation !== observedRevision.data_generation ||
    revision.schema_generation !== observedRevision.schema_generation
  )) return;
  observedRevision = revision;
  resolutionCache.clear();
  window.dispatchEvent(new CustomEvent<MapRevisionEvent>(MAP_DATA_REVISION_EVENT, { detail: revision }));
};

window.addEventListener(DATA_GENERATION_CHANGE_EVENT, onSharedGenerationChange as EventListener);

const fetchCurrentRevision = async (signal: AbortSignal): Promise<MapRevisionEvent> => {
  const response = await fetch('/api/revisions/state', {
    method: 'GET',
    cache: 'no-store',
    signal,
    headers: { Accept: 'application/json' },
  });
  if (!response.ok) throw new Error('Could not verify the current bridge inventory revision.');
  const payload = await response.json();
  const revision = {
    workspace_id: String(payload?.workspace_id || ''),
    data_generation: Number(payload.data_generation),
    schema_generation: Number(payload.schema_generation),
  };
  if (!revision.workspace_id || !Number.isSafeInteger(revision.data_generation) || !Number.isSafeInteger(revision.schema_generation)) {
    throw new Error('The server returned an invalid bridge inventory revision.');
  }
  applyObservedRevision(revision);
  return revision;
};

const cacheResolution = (key: string, resolution: MapResolveResponse): void => {
  resolutionCache.delete(key);
  resolutionCache.set(key, resolution);
  while (resolutionCache.size > MAX_CACHE_ENTRIES) {
    const oldest = resolutionCache.keys().next().value;
    if (oldest === undefined) break;
    resolutionCache.delete(oldest);
  }
};

const validateResolution = (payload: MapResolveResponse, revision: MapRevisionEvent): MapResolveResponse => {
  if (!payload || !['exact', 'ambiguous', 'not_found'].includes(payload.status)) {
    throw new Error('The server returned an invalid map resolution.');
  }
  if (payload.workspace_id !== revision.workspace_id || Number(payload.state_code) !== SERVER_STATE) {
    throw new Error('The server returned an unrelated map scope.');
  }
  if (
    Number(payload.data_generation) !== revision.data_generation ||
    Number(payload.schema_generation) !== revision.schema_generation
  ) {
    // The inventory changed between the revision read and resolver read.  Keep
    // the newer resolver token, invalidate old entries, and never use the stale key.
    applyObservedRevision({
      workspace_id: String(payload.workspace_id),
      data_generation: Number(payload.data_generation),
      schema_generation: Number(payload.schema_generation),
    });
  }
  if (payload.status === 'exact' && !payload.bridge) {
    throw new Error('The server omitted the resolved bridge identity.');
  }
  if (payload.candidates?.length > 5) {
    throw new Error('The server returned too many map candidates.');
  }
  return payload;
};

const resolveOnServer = async (
  question: string,
  options: MapRequestOptions,
  signal: AbortSignal,
): Promise<MapResolveResponse> => {
  const revision = await fetchCurrentRevision(signal);
  const requestedYear = options.year ?? explicitInventoryYear(question);
  const key = [
    revision.workspace_id,
    SERVER_STATE,
    requestedYear ?? 'latest',
    revision.schema_generation,
    revision.data_generation,
    normalizeCacheQuery(question),
  ].join('|');
  const cached = resolutionCache.get(key);
  if (cached) return cached;

  // Workspace and state are intentionally absent: the authenticated server
  // owns both scopes.  A client may only request an explicit inventory year.
  const body: { query: string; year?: number } = { query: question };
  if (requestedYear !== undefined) body.year = requestedYear;
  const response = await fetch('/api/map/resolve-bridge', {
    method: 'POST',
    cache: 'no-store',
    signal,
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw new Error(payload?.detail || 'Could not resolve the bridge map target.');
  const resolution = validateResolution(payload as MapResolveResponse, revision);
  const responseKey = [
    resolution.workspace_id,
    SERVER_STATE,
    requestedYear ?? 'latest',
    resolution.schema_generation,
    resolution.data_generation,
    normalizeCacheQuery(question),
  ].join('|');
  cacheResolution(responseKey, resolution);
  return resolution;
};

export const emitShowBridgeOnMapResult = (result: ShowBridgeOnMapResult): void => {
  window.dispatchEvent(new CustomEvent<ShowBridgeOnMapResult>(SHOW_BRIDGE_ON_MAP_RESULT_EVENT, { detail: result }));
};

const ambiguityResult = (
  requestId: string,
  resolution: MapResolveResponse,
): ShowBridgeOnMapResult => {
  const matches = resolution.candidates.map((candidate) => ({
    bridge_number: String(candidate.bridge_number),
    label: candidate.label,
    county_code: candidate.county_code,
    county_name: candidate.county_name,
    snapshot_year: candidate.snapshot_year,
    bridge_id: candidate.bridge_id,
    snapshot_id: candidate.snapshot_id,
    inventory_revision: candidate.inventory_revision,
  }));
  return {
    requestId,
    status: 'ambiguous',
    message: 'I found multiple matching bridges. Choose a structure number from the candidates below.',
    matches,
    resolution,
  };
};

export const requestShowBridgeOnMap = async (
  question: string,
  options: MapRequestOptions = {},
): Promise<ShowBridgeOnMapResult> => {
  const requestId = newRequestId();
  const controller = new AbortController();
  const abortFromCaller = () => controller.abort(options.signal?.reason);
  if (options.signal?.aborted) abortFromCaller();
  else options.signal?.addEventListener('abort', abortFromCaller, { once: true });
  const timeout = window.setTimeout(
    () => controller.abort(new DOMException('Map request timed out.', 'TimeoutError')),
    REQUEST_TIMEOUT_MS,
  );

  try {
    const resolution = await resolveOnServer(question, options, controller.signal);
    if (resolution.status === 'not_found') {
      return {
        requestId,
        status: 'not_found',
        message: `I couldn't find a published New Jersey bridge matching “${question.trim()}”. Try its structure number.`,
        resolution,
      };
    }
    if (resolution.status === 'ambiguous') return ambiguityResult(requestId, resolution);
    const bridge = resolution.bridge!;
    const focus: MapBridgeFocus = {
      requestId,
      state_code: bridge.state_code,
      bridge_number: String(bridge.bridge_number),
      snapshot_year: bridge.snapshot_year,
      bridge_id: bridge.bridge_id,
      snapshot_id: bridge.snapshot_id,
      snapshot_revision_id: bridge.snapshot_revision_id,
      inventory_revision: bridge.inventory_revision,
      data_revision: bridge.data_revision,
    };

    return await new Promise<ShowBridgeOnMapResult>((resolve, reject) => {
      const finish = (result: ShowBridgeOnMapResult) => {
        window.removeEventListener(SHOW_BRIDGE_ON_MAP_RESULT_EVENT, onResult as EventListener);
        controller.signal.removeEventListener('abort', onAbort);
        resolve(result);
      };
      const onResult = (event: Event) => {
        const result = (event as CustomEvent<ShowBridgeOnMapResult>).detail;
        if (result?.requestId === requestId) finish(result);
      };
      const onAbort = () => {
        window.removeEventListener(SHOW_BRIDGE_ON_MAP_RESULT_EVENT, onResult as EventListener);
        window.dispatchEvent(new CustomEvent<{ requestId: string }>(CANCEL_SHOW_BRIDGE_ON_MAP_EVENT, {
          detail: { requestId },
        }));
        reject(controller.signal.reason || new DOMException('Map request stopped.', 'AbortError'));
      };
      window.addEventListener(SHOW_BRIDGE_ON_MAP_RESULT_EVENT, onResult as EventListener);
      controller.signal.addEventListener('abort', onAbort, { once: true });
      window.dispatchEvent(new CustomEvent<ShowBridgeOnMapRequest>(SHOW_BRIDGE_ON_MAP_EVENT, {
        detail: { requestId, resolution, bridge, focus },
      }));
    });
  } finally {
    window.clearTimeout(timeout);
    options.signal?.removeEventListener('abort', abortFromCaller);
  }
};

export const clearMapResolutionCache = (): void => {
  resolutionCache.clear();
};
