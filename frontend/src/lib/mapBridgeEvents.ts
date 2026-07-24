import type { MapBridge } from '../components/ArcgisMap';

export const SHOW_BRIDGE_ON_MAP_EVENT = 'bridgeguard:show-bridge-on-map';
export const SHOW_BRIDGE_ON_MAP_RESULT_EVENT = 'bridgeguard:show-bridge-on-map-result';

export interface ShowBridgeOnMapRequest {
  requestId: string;
  question: string;
  searchText: string;
}

export interface ShowBridgeMatch {
  bridge_number: string;
  label: string;
}

export interface ShowBridgeOnMapResult {
  requestId: string;
  status: 'shown' | 'not_found' | 'ambiguous' | 'error';
  message: string;
  bridge?: MapBridge;
  matches?: ShowBridgeMatch[];
}

const bridgeIdentifier = (question: string): string | null => {
  const candidates = question.toUpperCase().match(/\b(?=[A-Z0-9-]{4,20}\b)(?=[A-Z0-9-]*\d)[A-Z0-9-]+\b/g) || [];
  return candidates.find((value) => !/^(?:19|20)\d{2}$/.test(value)) || null;
};

export const isSidebarMapRequest = (question: string): boolean => {
  const action = /\b(?:show|find|locate|centre|center|zoom|where\s+is|take\s+me\s+to)\b/i.test(question);
  if (!action) return false;
  return /\bmap\b/i.test(question) || /\bbridge\b/i.test(question) || bridgeIdentifier(question) !== null;
};

export const extractBridgeSearchText = (question: string): string => {
  const identifier = bridgeIdentifier(question);
  if (identifier) return identifier;
  const cleaned = question
    .replace(/\b(?:show|find|locate|centre|center|zoom|where|is|take|me|to|on|the|map|bridge|card|please)\b/gi, ' ')
    .replace(/[^a-z0-9'-]+/gi, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return cleaned || question.trim();
};

export const emitShowBridgeOnMapResult = (result: ShowBridgeOnMapResult): void => {
  window.dispatchEvent(new CustomEvent<ShowBridgeOnMapResult>(SHOW_BRIDGE_ON_MAP_RESULT_EVENT, { detail: result }));
};

export const requestShowBridgeOnMap = (question: string): Promise<ShowBridgeOnMapResult> => {
  const requestId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const detail: ShowBridgeOnMapRequest = {
    requestId,
    question,
    searchText: extractBridgeSearchText(question),
  };

  return new Promise((resolve) => {
    const finish = (result: ShowBridgeOnMapResult) => {
      window.removeEventListener(SHOW_BRIDGE_ON_MAP_RESULT_EVENT, onResult as EventListener);
      window.clearTimeout(timeout);
      resolve(result);
    };
    const onResult = (event: Event) => {
      const result = (event as CustomEvent<ShowBridgeOnMapResult>).detail;
      if (result?.requestId === requestId) finish(result);
    };
    const timeout = window.setTimeout(() => finish({
      requestId,
      status: 'error',
      message: 'The map did not finish locating that bridge. Please try again.',
    }), 45000);

    window.addEventListener(SHOW_BRIDGE_ON_MAP_RESULT_EVENT, onResult as EventListener);
    window.dispatchEvent(new CustomEvent<ShowBridgeOnMapRequest>(SHOW_BRIDGE_ON_MAP_EVENT, { detail }));
  });
};
