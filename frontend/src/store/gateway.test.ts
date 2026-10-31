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
});
