import { resolveToken } from "./token";
import type { GatewayEvent } from "./types";

type Handler = (event: GatewayEvent) => void;

const MAX_BACKOFF_MS = 10_000;
const HEARTBEAT_INTERVAL_MS = 20_000;
const HEARTBEAT_TIMEOUT_MS = 45_000;

export class GatewaySocket {
  private ws: WebSocket | null = null;
  private handlers = new Set<Handler>();
  private statusHandlers = new Set<(connected: boolean) => void>();
  private subscriptions = new Map<string, number>();
  private backoff = 1000;
  private closedByUser = true;
  private reconnectTimer: number | null = null;
  private heartbeatTimer: number | null = null;
  private lastPongAt = 0;
  private generation = 0;
  private hasConnectedBefore = false;

  on(handler: Handler): () => void {
    this.handlers.add(handler);
    return () => this.handlers.delete(handler);
  }

  onStatus(handler: (connected: boolean) => void): () => void {
    this.statusHandlers.add(handler);
    handler(this.isConnected());
    return () => this.statusHandlers.delete(handler);
  }

  isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  start(): void {
    this.closedByUser = false;
    if (this.ws && (this.ws.readyState === WebSocket.CONNECTING || this.ws.readyState === WebSocket.OPEN)) {
      return;
    }
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.connect();
  }

  stop(): void {
    this.closedByUser = true;
    this.generation += 1;
    this.clearHeartbeat();
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    const current = this.ws;
    this.ws = null;
    current?.close();
    this.emitStatus(false);
  }

  subscribe(sessionIds: string[]): void {
    const newlySubscribed: string[] = [];
    for (const id of sessionIds) {
      const count = this.subscriptions.get(id) ?? 0;
      this.subscriptions.set(id, count + 1);
      if (count === 0) newlySubscribed.push(id);
    }
    if (newlySubscribed.length > 0) this.send({ type: "subscribe", sessions: newlySubscribed });
  }

  unsubscribe(sessionIds: string[]): void {
    const noLongerSubscribed: string[] = [];
    for (const id of sessionIds) {
      const count = this.subscriptions.get(id) ?? 0;
      if (count <= 1) {
        if (count === 1) noLongerSubscribed.push(id);
        this.subscriptions.delete(id);
      } else {
        this.subscriptions.set(id, count - 1);
      }
    }
    if (noLongerSubscribed.length > 0) {
      this.send({ type: "unsubscribe", sessions: noLongerSubscribed });
    }
  }

  private send(payload: unknown): void {
    const current = this.ws;
    if (!current || current.readyState !== WebSocket.OPEN) return;
    try {
      current.send(JSON.stringify(payload));
    } catch {
      current.close();
    }
  }

  private connect(): void {
    if (this.closedByUser || this.ws) return;
    const generation = ++this.generation;
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const token = resolveToken();
    let candidate: WebSocket;
    try {
      candidate = new WebSocket(`${proto}://${window.location.host}/api/ws?token=${encodeURIComponent(token)}`);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = candidate;

    candidate.onopen = () => {
      if (this.ws !== candidate || generation !== this.generation) return;
      this.backoff = 1000;
      this.lastPongAt = Date.now();
      this.emitStatus(true);
      if (this.hasConnectedBefore) {
        window.dispatchEvent(new CustomEvent("eurus:gateway-resync", {
          detail: { sessions: [...this.subscriptions.keys()] },
        }));
      }
      this.hasConnectedBefore = true;
      const sessions = [...this.subscriptions.keys()];
      if (sessions.length > 0) this.send({ type: "subscribe", sessions });
      this.startHeartbeat(candidate, generation);
    };

    candidate.onmessage = (message) => {
      if (this.ws !== candidate || generation !== this.generation) return;
      let parsed: GatewayEvent;
      try {
        parsed = JSON.parse(String(message.data)) as GatewayEvent;
      } catch {
        return;
      }
      if (parsed.type === "pong") this.lastPongAt = Date.now();
      for (const handler of this.handlers) handler(parsed);
    };

    candidate.onclose = (event) => {
      if (this.ws !== candidate || generation !== this.generation) return;
      this.ws = null;
      this.clearHeartbeat();
      this.emitStatus(false);
      if (this.closedByUser) return;
      if (event.code === 4401) {
        window.dispatchEvent(new CustomEvent("eurus:unauthorized"));
        return;
      }
      this.scheduleReconnect();
    };

    candidate.onerror = () => {
      if (this.ws === candidate) candidate.close();
    };
  }

  private startHeartbeat(candidate: WebSocket, generation: number): void {
    this.clearHeartbeat();
    this.heartbeatTimer = window.setInterval(() => {
      if (this.ws !== candidate || generation !== this.generation) return;
      if (Date.now() - this.lastPongAt > HEARTBEAT_TIMEOUT_MS) {
        candidate.close();
        return;
      }
      this.send({ type: "ping" });
    }, HEARTBEAT_INTERVAL_MS);
  }

  private clearHeartbeat(): void {
    if (this.heartbeatTimer !== null) window.clearInterval(this.heartbeatTimer);
    this.heartbeatTimer = null;
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer !== null || this.closedByUser) return;
    const delay = this.backoff;
    this.backoff = Math.min(this.backoff * 2, MAX_BACKOFF_MS);
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  private emitStatus(connected: boolean): void {
    for (const handler of this.statusHandlers) handler(connected);
  }
}

export const socket = new GatewaySocket();
