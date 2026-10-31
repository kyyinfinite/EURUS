/** Resilient WebSocket client for the gateway event stream. */

import { resolveToken } from "./token";
import type { GatewayEvent } from "./types";

type Handler = (event: GatewayEvent) => void;

const MAX_BACKOFF_MS = 10_000;

export class GatewaySocket {
  private ws: WebSocket | null = null;
  private handlers = new Set<Handler>();
  private statusHandlers = new Set<(connected: boolean) => void>();
  private subscribed = new Set<string>();
  private backoff = 1000;
  private closedByUser = false;
  private reconnectTimer: number | null = null;

  on(handler: Handler): () => void {
    this.handlers.add(handler);
    return () => {
      this.handlers.delete(handler);
    };
  }

  onStatus(handler: (connected: boolean) => void): () => void {
    this.statusHandlers.add(handler);
    handler(this.isConnected());
    return () => {
      this.statusHandlers.delete(handler);
    };
  }

  isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  start(): void {
    this.closedByUser = false;
    if (this.ws) return;
    this.connect();
  }

  stop(): void {
    this.closedByUser = true;
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
  }

  subscribe(sessionIds: string[]): void {
    for (const id of sessionIds) this.subscribed.add(id);
    if (this.isConnected() && sessionIds.length > 0) {
      this.send({ type: "subscribe", sessions: sessionIds });
    }
  }

  unsubscribe(sessionIds: string[]): void {
    for (const id of sessionIds) this.subscribed.delete(id);
    if (this.isConnected() && sessionIds.length > 0) {
      this.send({ type: "unsubscribe", sessions: sessionIds });
    }
  }

  private send(payload: unknown): void {
    try {
      this.ws?.send(JSON.stringify(payload));
    } catch {
      /* connection is dropping; reconnect logic handles it */
    }
  }

  private connect(): void {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const token = resolveToken();
    try {
      this.ws = new WebSocket(`${proto}://${window.location.host}/api/ws?token=${encodeURIComponent(token)}`);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws.onopen = () => {
      this.backoff = 1000;
      this.emitStatus(true);
      if (this.subscribed.size > 0) {
        this.send({ type: "subscribe", sessions: [...this.subscribed] });
      }
    };
    this.ws.onmessage = (message) => {
      let parsed: GatewayEvent;
      try {
        parsed = JSON.parse(message.data as string) as GatewayEvent;
      } catch {
        return;
      }
      for (const handler of this.handlers) handler(parsed);
    };
    this.ws.onclose = (event) => {
      this.emitStatus(false);
      this.ws = null;
      if (!this.closedByUser) {
        if (event.code === 4401) {
          window.dispatchEvent(new CustomEvent("eurus:unauthorized"));
          return;
        }
        this.scheduleReconnect();
      }
    };
    this.ws.onerror = () => {
      /* onclose follows */
    };
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
