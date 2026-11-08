import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GatewaySocket } from "./ws";

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 3;

  readyState = MockWebSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];

  constructor(_url: string) {
    MockWebSocket.instances.push(this);
  }

  send(value: string): void {
    this.sent.push(value);
  }

  close(code = 1000): void {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.({ code } as CloseEvent);
  }

  open(): void {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.();
  }
}

describe("GatewaySocket", () => {
  beforeEach(() => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    MockWebSocket.instances = [];
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("ignores callbacks from a socket stopped before a new start", () => {
    const client = new GatewaySocket();
    const status = vi.fn();
    client.onStatus(status);
    client.start();
    const first = MockWebSocket.instances[0];
    client.stop();
    client.start();
    const second = MockWebSocket.instances[1];

    first.onopen?.();
    expect(client.isConnected()).toBe(false);
    second.open();
    expect(client.isConnected()).toBe(true);
    client.stop();
  });

  it("uses subscription reference counts", () => {
    const client = new GatewaySocket();
    client.start();
    const current = MockWebSocket.instances[0];
    current.open();

    client.subscribe(["s1"]);
    client.subscribe(["s1"]);
    client.unsubscribe(["s1"]);

    expect(current.sent.map((value) => JSON.parse(value))).toEqual([
      { type: "subscribe", sessions: ["s1"] },
    ]);

    client.unsubscribe(["s1"]);
    expect(current.sent.map((value) => JSON.parse(value))).toEqual([
      { type: "subscribe", sessions: ["s1"] },
      { type: "unsubscribe", sessions: ["s1"] },
    ]);
    client.stop();
  });

  it("sends heartbeat pings and reconnects after a missing pong", () => {
    const client = new GatewaySocket();
    client.start();
    const first = MockWebSocket.instances[0];
    first.open();

    vi.advanceTimersByTime(20_000);
    expect(first.sent.map((value) => JSON.parse(value))).toContainEqual({ type: "ping" });

    vi.advanceTimersByTime(40_000);
    expect(first.readyState).toBe(MockWebSocket.CLOSED);
    vi.advanceTimersByTime(1000);
    expect(MockWebSocket.instances).toHaveLength(2);
    client.stop();
  });

  it("resubscribes and emits a resync event after reconnect", () => {
    const client = new GatewaySocket();
    const resync = vi.fn();
    window.addEventListener("eurus:gateway-resync", resync);
    client.subscribe(["s1"]);
    client.start();
    const first = MockWebSocket.instances[0];
    first.open();
    first.close(1006);
    vi.advanceTimersByTime(1000);
    const second = MockWebSocket.instances[1];
    second.open();

    expect(JSON.parse(second.sent[0] ?? "null")).toEqual({
      type: "subscribe",
      sessions: ["s1"],
    });
    expect(resync).toHaveBeenCalledTimes(1);
    window.removeEventListener("eurus:gateway-resync", resync);
    client.stop();
  });
  it("uses pong responses to extend the heartbeat deadline", () => {
    const client = new GatewaySocket();
    client.start();
    const current = MockWebSocket.instances[0];
    current.open();

    vi.advanceTimersByTime(20_000);
    current.onmessage?.({ data: JSON.stringify({ type: "pong", time: Date.now() }) } as MessageEvent);
    vi.advanceTimersByTime(40_000);
    expect(current.readyState).toBe(MockWebSocket.OPEN);
    vi.advanceTimersByTime(20_000);
    expect(current.readyState).toBe(MockWebSocket.CLOSED);
    client.stop();
  });

  it("does not reconnect after an unauthorized close", () => {
    const client = new GatewaySocket();
    const unauthorized = vi.fn();
    window.addEventListener("eurus:unauthorized", unauthorized);
    client.start();
    const current = MockWebSocket.instances[0];
    current.open();
    current.close(4401);
    vi.advanceTimersByTime(60_000);

    expect(MockWebSocket.instances).toHaveLength(1);
    expect(unauthorized).toHaveBeenCalledOnce();
    window.removeEventListener("eurus:unauthorized", unauthorized);
    client.stop();
  });

});
