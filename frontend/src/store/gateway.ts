/** Shared client state: live event streams, run status, approvals, connection. */

import { create } from "zustand";
import { socket } from "../lib/ws";
import type { AgentEventPayload, ApprovalView, GatewayEvent } from "../lib/types";

export interface LiveTurn {
  runId: string;
  origin: "web" | "external";
  status: "running" | "completed" | "failed" | "cancelled";
  stopReason?: string | null;
  text: string;
  events: AgentEventPayload[];
  runtimeEvents: {
    event_type: string;
    payload: Record<string, unknown>;
    created_at: string;
  }[];
}

interface GatewayState {
  connected: boolean;
  turns: Record<string, LiveTurn>; // by run_id
  sessionTurns: Record<string, string[]>; // session_id -> run_ids (active/recent)
  approvals: ApprovalView[];
  lastSessionChange: number;

  setConnected: (value: boolean) => void;
  ingest: (event: GatewayEvent) => void;
  setApprovals: (items: ApprovalView[]) => void;
  removeApproval: (token: string, decision: string) => void;
  clearTurn: (runId: string) => void;
}

function upsertTurn(state: GatewayState, runId: string, sessionId: string, origin: "web" | "external"): LiveTurn {
  const existing = state.turns[runId];
  if (existing) return existing;
  const turn: LiveTurn = {
    runId,
    origin,
    status: "running",
    text: "",
    events: [],
    runtimeEvents: [],
  };
  const list = state.sessionTurns[sessionId] ?? [];
  state.sessionTurns[sessionId] = [...list.slice(-9), runId];
  state.turns[runId] = turn;
  return turn;
}

export const useGateway = create<GatewayState>((set, get) => ({
  connected: false,
  turns: {},
  sessionTurns: {},
  approvals: [],
  lastSessionChange: 0,

  setConnected: (value) => set({ connected: value }),

  ingest: (event) => {
    const state = get();
    if (event.type === "agent_event") {
      const turn = upsertTurn(state, event.run_id, event.session_id, event.origin);
      const next: LiveTurn = { ...turn, events: [...turn.events, event.event] };
      if (event.event.kind === "text" && event.event.text) {
        next.text = turn.text + event.event.text;
      } else if (event.event.kind === "text_reset") {
        next.text = "";
      } else if (event.event.kind === "done") {
        next.status = "completed";
        if (event.event.text && !next.text) next.text = event.event.text;
      }
      set({ turns: { ...get().turns, [event.run_id]: next } });
      return;
    }
    if (event.type === "run_event") {
      const turn = upsertTurn(state, event.run_id, event.session_id, event.origin);
      const next: LiveTurn = {
        ...turn,
        runtimeEvents: [
          ...turn.runtimeEvents,
          {
            event_type: event.runtime_event.event_type,
            payload: event.runtime_event.payload,
            created_at: event.runtime_event.created_at,
          },
        ],
      };
      if (event.runtime_event.event_type === "run.started") {
        next.text =
          next.text ||
          String(event.runtime_event.payload.objective ?? "");
      }
      set({ turns: { ...get().turns, [event.run_id]: next } });
      return;
    }
    if (event.type === "run_status") {
      const turn = state.turns[event.run_id];
      if (turn) {
        const status =
          event.status === "running" || event.status === "queued" || event.status === "verifying" || event.status === "waiting_approval"
            ? "running"
            : event.status === "completed"
              ? "completed"
              : event.status === "cancelled"
                ? "cancelled"
                : "failed";
        set({
          turns: {
            ...get().turns,
            [event.run_id]: { ...turn, status, stopReason: event.stop_reason ?? turn.stopReason },
          },
        });
      }
      set({ lastSessionChange: Date.now() });
      return;
    }
    if (event.type === "approval_required") {
      const { token, run_id, session_id, tool_name, arguments_json, fingerprint, expires_in_seconds, display_notice } = event;
      const approval: ApprovalView = {
        token,
        run_id,
        session_id,
        tool_name,
        arguments_json,
        fingerprint,
        expires_in_seconds,
        display_notice,
      };
      set({ approvals: [approval, ...get().approvals.filter((a) => a.token !== token)] });
      return;
    }
    if (event.type === "approval_resolved") {
      set({
        approvals: get().approvals.filter((a) => a.token !== event.token),
        lastSessionChange: Date.now(),
      });
      return;
    }
    if (event.type === "sessions_changed") {
      set({ lastSessionChange: Date.now() });
    }
  },

  setApprovals: (items) => set({ approvals: items }),

  removeApproval: (token, _decision) =>
    set({ approvals: get().approvals.filter((a) => a.token !== token) }),

  clearTurn: (runId) => {
    const turns = { ...get().turns };
    delete turns[runId];
    const sessionTurns: Record<string, string[]> = {};
    for (const [sessionId, ids] of Object.entries(get().sessionTurns)) {
      sessionTurns[sessionId] = ids.filter((id) => id !== runId);
    }
    set({ turns, sessionTurns });
  },
}));

/** Attach the socket handlers once (called from App bootstrap). */
export function bindSocket(): () => void {
  const offStatus = socket.onStatus((connected) => {
    useGateway.getState().setConnected(connected);
  });
  const offEvents = socket.on((event) => {
    useGateway.getState().ingest(event);
  });
  socket.start();
  return () => {
    offStatus();
    offEvents();
    socket.stop();
  };
}
