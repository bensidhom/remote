export type BridgetteSurface = 'full' | 'sidebar';

export interface BridgetteStreamEvent {
  type: 'accepted' | 'evidence' | 'clarification' | 'abstention' | 'token' | 'done' | 'stopped' | 'error';
  request_id: string;
  content?: string;
  text?: string;
  detail?: string;
  sql?: string;
  rows?: unknown[][];
  columns?: string[];
  count?: number;
  truncated?: boolean;
  code?: string;
  message?: string;
  options?: Array<{ id: string; label: string }>;
  warnings?: string[];
  [key: string]: unknown;
}

export const newBridgetteRequestId = (): string => {
  if (typeof crypto.randomUUID !== 'function') {
    throw new Error('This browser cannot create a secure request identifier.');
  }
  return crypto.randomUUID();
};

export const bridgetteStreamPath = (surface: BridgetteSurface): string =>
  surface === 'sidebar' ? '/api/bridgette/sidebar/stream' : '/api/bridgette/stream';

export const cancelBridgetteRequest = async (requestId: string): Promise<void> => {
  await fetch(`/api/bridgette/requests/${encodeURIComponent(requestId)}/cancel`, {
    method: 'POST',
    headers: { 'X-Request-ID': newBridgetteRequestId() },
  });
};

export const readBridgetteStream = async (
  response: Response,
  expectedRequestId: string,
  onEvent: (event: BridgetteStreamEvent) => void,
): Promise<void> => {
  if (!response.ok || !response.body) {
    let detail = `Request failed (${response.status})`;
    try {
      const payload = await response.json();
      if (typeof payload?.detail === 'string') detail = payload.detail;
    } catch {
      // The status code remains the safe fallback for non-JSON errors.
    }
    throw new Error(detail);
  }
  if (response.headers.get('X-Request-ID') !== expectedRequestId) {
    throw new Error('The server returned an unrelated Bridgette response.');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let terminalSeen = false;
  const consume = (line: string) => {
    if (!line.trim()) return;
    const event = JSON.parse(line) as BridgetteStreamEvent;
    if (event.request_id !== expectedRequestId || terminalSeen) return;
    if (event.type === 'done' || event.type === 'stopped' || event.type === 'error') {
      terminalSeen = true;
    }
    onEvent(event);
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let newline = buffer.indexOf('\n');
    while (newline >= 0) {
      consume(buffer.slice(0, newline));
      buffer = buffer.slice(newline + 1);
      newline = buffer.indexOf('\n');
    }
  }
  buffer += decoder.decode();
  if (buffer.trim()) consume(buffer);
  if (!terminalSeen) throw new Error('Bridgette stream ended without a terminal event.');
};
