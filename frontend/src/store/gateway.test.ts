import { beforeEach, describe, expect, it } from "vitest";
import { useGateway } from "./gateway";
import type { GatewayEvent } from "../lib/types";

function reset() {
  useGateway.setState({
    connected: false,
    turns: {},
    sessionTurns: {},
    approvals: [],
    lastSessionChange: 0,
  });
}

describe("gateway store ingestion", () => {
  beforeEach(reset);

  it("accumulates streamed text events into a live turn", () => {
    const events: GatewayEvent[] = [
      {
        type: "agent_event",
        session_id: "s1",
        run_id: "r1",
        origin: "web",
        sequence: 1,
        event: { kind: "text", step: 1, text: "Hello " },
      },
      {
        type: "agent_event",
        session_id: "s1",
        run_id: "r1",
        origin: "web",
        sequence: 2,
        event: { kind: "text", step: 1, text: "world" },
      },
    ];
    for (const event of events) useGateway.getState().ingest(event);
    const turn = useGateway.getState().turns.r1;
    expect(turn.text).toBe("Hello world");
    expect(turn.status).toBe("running");
    expect(useGateway.getState().sessionTurns.s1).toEqual(["r1"]);
  });

  it("respects text_reset", () => {
    useGateway.getState().ingest({
      type: "agent_event",
      session_id: "s1",
      run_id: "r1",
      origin: "web",
      sequence: 1,
      event: { kind: "text", step: 1, text: "draft" },
    });
    useGateway.getState().ingest({
      type: "agent_event",
      session_id: "s1",
      run_id: "r1",
      origin: "web",
      sequence: 2,
      event: { kind: "text_reset", step: 1 },
    });
    expect(useGateway.getState().turns.r1.text).toBe("");
  });

  it("maps run_status to terminal turn states", () => {
    useGateway.getState().ingest({
      type: "agent_event",
      session_id: "s1",
      run_id: "r1",
      origin: "web",
      sequence: 1,
      event: { kind: "text", step: 1, text: "x" },
    });
    useGateway.getState().ingest({
      type: "run_status",
      session_id: "s1",
      run_id: "r1",
      status: "failed",
      stop_reason: "boom",
      origin: "web",
    });
    const turn = useGateway.getState().turns.r1;
    expect(turn.status).toBe("failed");
    expect(turn.stopReason).toBe("boom");
  });

  it("tracks pending approvals and their resolution", () => {
    useGateway.getState().ingest({
      type: "approval_required",
      token: "tok1",
      run_id: "r1",
      session_id: "s1",
      tool_name: "write_file",
      arguments_json: "{}",
      fingerprint: "fp",
      expires_in_seconds: 120,
    });
    expect(useGateway.getState().approvals).toHaveLength(1);
    useGateway.getState().ingest({
      type: "approval_resolved",
      session_id: "s1",
      token: "tok1",
      decision: "approved",
    });
    expect(useGateway.getState().approvals).toHaveLength(0);
    expect(useGateway.getState().lastSessionChange).toBeGreaterThan(0);
  });

  it("keeps at most 10 recent turns per session", () => {
    for (let index = 0; index < 12; index += 1) {
      useGateway.getState().ingest({
        type: "agent_event",
        session_id: "s1",
        run_id: `r${index}`,
        origin: "web",
        sequence: index,
        event: { kind: "text", step: 1, text: "x" },
      });
    }
    expect(useGateway.getState().sessionTurns.s1).toHaveLength(10);
    expect(useGateway.getState().sessionTurns.s1[0]).toBe("r2");
  });
  it("replaces sessionTurns and turns references after the first event", () => {
    const previousSessionTurns = useGateway.getState().sessionTurns;
    const previousTurns = useGateway.getState().turns;

    useGateway.getState().ingest({
      type: "agent_event",
      session_id: "s1",
      run_id: "r1",
      origin: "web",
      sequence: 1,
      event: { kind: "text", step: 1, text: "hello" },
    });

    expect(useGateway.getState().sessionTurns).not.toBe(previousSessionTurns);
    expect(useGateway.getState().turns).not.toBe(previousTurns);
  });

  it("does not store text events in the event history", () => {
    useGateway.getState().ingest({
      type: "agent_event",
      session_id: "s1",
      run_id: "r1",
      origin: "web",
      sequence: 1,
      event: { kind: "text", step: 1, text: "hello" },
    });

    expect(useGateway.getState().turns.r1.events).toEqual([]);
    expect(useGateway.getState().turns.r1.text).toBe("hello");
  });

  it("keeps external run objectives out of assistant text", () => {
    useGateway.getState().ingest({
      type: "run_event",
      session_id: "s1",
      run_id: "external-1",
      origin: "external",
      runtime_event: {
        event_id: "e1",
        event_type: "run.started",
        sequence_no: 1,
        payload: { objective: "Inspect repository" },
        created_at: "2026-11-01T00:00:00Z",
        step_id: null,
        attempt_id: null,
      },
    });

    expect(useGateway.getState().turns["external-1"].text).toBe("");
  });

  it("clears turns immutably", () => {
    useGateway.getState().ingest({
      type: "agent_event",
      session_id: "s1",
      run_id: "r1",
      origin: "web",
      sequence: 1,
      event: { kind: "text", step: 1, text: "hello" },
    });
    const previousTurns = useGateway.getState().turns;

    useGateway.getState().clearTurn("r1");

    expect(useGateway.getState().turns).not.toBe(previousTurns);
    expect(useGateway.getState().turns.r1).toBeUndefined();
    expect(useGateway.getState().sessionTurns.s1).toBeUndefined();
  });

  it("applies a frame of gateway events with one store notification", () => {
    const notifications: number[] = [];
    const unsubscribe = useGateway.subscribe((state) => notifications.push(state.lastSessionChange));
    useGateway.getState().ingestMany([
      {
        type: "agent_event",
        session_id: "s1",
        run_id: "r1",
        origin: "web",
        sequence: 1,
        event: { kind: "text", step: 1, text: "a" },
      },
      {
        type: "agent_event",
        session_id: "s1",
        run_id: "r1",
        origin: "web",
        sequence: 2,
        event: { kind: "text", step: 1, text: "b" },
      },
    ]);
    unsubscribe();
    expect(notifications).toHaveLength(1);
    expect(useGateway.getState().turns.r1.text).toBe("ab");
  });

});
