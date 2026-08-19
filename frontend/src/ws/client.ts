// WebSocket client: one socket per tab, ticket-authenticated, self-healing.
//
// REST is the source of truth on mount; this carries deltas on top. After a
// reconnect the client re-fetches over REST rather than trusting the socket to
// have kept up - a gap in a delta stream is invisible, and trusting it is how a
// UI ends up showing an alarm that was cleared while the tab was asleep.

export type Frame = {
  event: string;
  [key: string]: unknown;
};

type Listener = (frame: Frame) => void;

const MAX_BACKOFF_MS = 30_000;

export class DcimSocket {
  private ws: WebSocket | null = null;
  private topics = new Set<string>();
  private listeners = new Set<Listener>();
  private statusListeners = new Set<(s: SocketStatus) => void>();
  private attempt = 0;
  private closed = false;
  private pingTimer: number | null = null;

  status: SocketStatus = 'connecting';

  constructor(private readonly getTicket: () => Promise<string>) {}

  connect(): void {
    if (this.closed) return;
    this.setStatus(this.attempt === 0 ? 'connecting' : 'reconnecting');

    this.getTicket()
      .then((ticket) => {
        const proto = location.protocol === 'https:' ? 'wss' : 'ws';
        const ws = new WebSocket(
          `${proto}://${location.host}/api/v1/ws?ticket=${encodeURIComponent(ticket)}`,
        );
        this.ws = ws;

        ws.onopen = () => {
          this.attempt = 0;
          this.setStatus('open');
          if (this.topics.size) {
            ws.send(JSON.stringify({ op: 'subscribe', topics: [...this.topics] }));
          }
          this.pingTimer = window.setInterval(() => {
            if (ws.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({ op: 'ping', ts: Date.now() }));
            }
          }, 30_000);
        };

        ws.onmessage = (e) => {
          try {
            const frame = JSON.parse(e.data as string) as Frame;
            this.listeners.forEach((fn) => fn(frame));
          } catch {
            /* a malformed frame must not kill the socket */
          }
        };

        ws.onclose = () => this.scheduleReconnect();
        ws.onerror = () => ws.close();
      })
      .catch(() => this.scheduleReconnect());
  }

  private scheduleReconnect(): void {
    if (this.pingTimer !== null) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
    this.ws = null;
    if (this.closed) return;
    this.setStatus('reconnecting');

    // Jitter matters: without it every browser comes back at the same instant
    // after an API restart and stampedes it.
    const base = Math.min(1000 * 2 ** this.attempt, MAX_BACKOFF_MS);
    const delay = base * (0.7 + Math.random() * 0.6);
    this.attempt += 1;
    window.setTimeout(() => this.connect(), delay);
  }

  subscribe(topics: string[]): void {
    topics.forEach((t) => this.topics.add(t));
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ op: 'subscribe', topics }));
    }
  }

  unsubscribe(topics: string[]): void {
    topics.forEach((t) => this.topics.delete(t));
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ op: 'unsubscribe', topics }));
    }
  }

  onFrame(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  onStatus(fn: (s: SocketStatus) => void): () => void {
    this.statusListeners.add(fn);
    return () => this.statusListeners.delete(fn);
  }

  close(): void {
    this.closed = true;
    this.ws?.close();
  }

  private setStatus(s: SocketStatus): void {
    this.status = s;
    this.statusListeners.forEach((fn) => fn(s));
  }
}

export type SocketStatus = 'connecting' | 'open' | 'reconnecting' | 'closed';
