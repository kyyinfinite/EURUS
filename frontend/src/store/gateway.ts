import { create } from "zustand";
import { socket } from "../lib/ws";
import { createGatewayEventBatcher } from "../lib/gatewayBatch";
import type { AgentEventPayload, ApprovalView, GatewayEvent } from "../lib/types";

export interface LiveTurn {
  runId: string;
  sessionId: string;
  origin: "web" | "external";
  status: "running" | "completed" | "failed" | "cancelled";
  stopReason?: string | null;
  text: string;
  events: AgentEventPayload[];
  lastEventAt: number;
  runtimeEvents: {
    event_type: string;
    payload: Record<string, unknown>;
    created_at: string;
  }[];
}

interface GatewayState {
  connected: boolean;
  turns: Record<string, LiveTurn>;
  sessionTurns: Record<string, string[]>;
  approvals: ApprovalView[];
  lastSessionChange: number;
  setConnected: (value: boolean) => void;
  ingest: (event: GatewayEvent) => void;
  ingestMany: (events: GatewayEvent[]) => void;
  setApprovals: (items: ApprovalView[]) => void;
  removeApproval: (token: string, decision: string) => void;
  clearTurn: (runId: string) => void;
}

const MAX_TURN_EVENTS = 200;

function createTurn(
  runId: string,
  sessionId: string,
  origin: "web" | "external",
): LiveTurn {
  return {
    runId,
    sessionId,
    origin,
    status: "running",
    text: "",
    events: [],
    lastEventAt: Date.now(),
    runtimeEvents: [],
  };
}

function addSessionTurn(
  sessionTurns: Record<string, string[]>,
  sessionId: string,
  runId: string,
): Record<string, string[]> {
  const current = sessionTurns[sessionId] ?? [];
  if (current.includes(runId)) return sessionTurns;
  return { ...sessionTurns, [sessionId]: [...current.slice(-9), runId] };
}

function mapStatus(status: string): LiveTurn["status"] {
  if (status === "completed") return "completed";
  if (status === "cancelled") return "cancelled";
  if (["running", "queued", "verifying", "waiting_approval"].includes(status)) {
    return "running";
  }
  return "failed";
}

export const useGateway = create<GatewayState>((set) => ({
  connected: false,
  turns: {},
  sessionTurns: {},
  approvals: [],
  lastSessionChange: 0,

  setConnected: (connected) => set({ connected }),

  ingest: (event) => {
    useGateway.getState().ingestMany([event]);
  },

  ingestMany: (events) => {
    if (events.length === 0) return;
    set((state) => {
      let turns = state.turns;
      let sessionTurns = state.sessionTurns;
      let approvals = state.approvals;
      let lastSessionChange = state.lastSessionChange;
      let changed = false;

      for (const event of events) {
        if (event.type === "agent_event") {
          const current = turns[event.run_id] ?? createTurn(
            event.run_id,
            event.session_id,
            event.origin,
          );
          const payload = event.event;
          const next: LiveTurn = {
            ...current,
            lastEventAt: Date.now(),
            events:
              payload.kind === "text" || payload.kind === "text_reset"
                ? current.events
                : [...current.events, payload].slice(-MAX_TURN_EVENTS),
            text:
              payload.kind === "text"
                ? current.text + (payload.text ?? "")
                : payload.kind === "text_reset"
                  ? ""
                  : current.text,
            status: payload.kind === "done" ? "completed" : current.status,
          };
          if (payload.kind === "done" && payload.text && !next.text) {
            next.text = payload.text;
          }
          turns = { ...turns, [event.run_id]: next };
          sessionTurns = addSessionTurn(sessionTurns, event.session_id, event.run_id);
          changed = true;
          continue;
        }

        if (event.type === "run_event") {
          const current = turns[event.run_id] ?? createTurn(
            event.run_id,
            event.session_id,
            event.origin,
          );
          turns = {
            ...turns,
            [event.run_id]: {
              ...current,
              lastEventAt: Date.now(),
              runtimeEvents: [
                ...current.runtimeEvents,
                {
                  event_type: event.runtime_event.event_type,
                  payload: event.runtime_event.payload,
                  created_at: event.runtime_event.created_at,
                },
              ].slice(-MAX_TURN_EVENTS),
            },
          };
          sessionTurns = addSessionTurn(sessionTurns, event.session_id, event.run_id);
          changed = true;
          continue;
        }

        if (event.type === "run_status") {
          const current = turns[event.run_id];
          if (current) {
            turns = {
              ...turns,
              [event.run_id]: {
                ...current,
                status: mapStatus(event.status),
                stopReason: event.stop_reason ?? current.stopReason,
              },
            };
          }
          lastSessionChange = Date.now();
          changed = true;
          continue;
        }

        if (event.type === "approval_required") {
          const approval: ApprovalView = {
            token: event.token,
            run_id: event.run_id,
            session_id: event.session_id,
            tool_name: event.tool_name,
            arguments_json: event.arguments_json,
            fingerprint: event.fingerprint,
            expires_in_seconds: event.expires_in_seconds,
            display_notice: event.display_notice,
          };
          approvals = [approval, ...approvals.filter((item) => item.token !== approval.token)];
          changed = true;
          continue;
        }

        if (event.type === "approval_resolved") {
          approvals = approvals.filter((item) => item.token !== event.token);
          lastSessionChange = Date.now();
          changed = true;
          continue;
        }

        if (event.type === "sessions_changed") {
          lastSessionChange = Date.now();
          changed = true;
        }
      }

      return changed ? { turns, sessionTurns, approvals, lastSessionChange } : state;
    });
  },

  setApprovals: (items) => set({ approvals: items }),

  removeApproval: (token, _decision) =>
    set((state) => ({
      approvals: state.approvals.filter((item) => item.token !== token),
    })),

  clearTurn: (runId) =>
    set((state) => {
      if (!state.turns[runId]) return state;
      const turns = { ...state.turns };
      delete turns[runId];
      const sessionTurns: Record<string, string[]> = {};
      for (const [sessionId, ids] of Object.entries(state.sessionTurns)) {
        const remaining = ids.filter((id) => id !== runId);
        if (remaining.length > 0) sessionTurns[sessionId] = remaining;
      }
      return { turns, sessionTurns };
    }),
}));

export function bindSocket(): () => void {
  const offStatus = socket.onStatus((connected) => {
    useGateway.getState().setConnected(connected);
  });
  const batcher = createGatewayEventBatcher(
    (events) => useGateway.getState().ingestMany(events),
    {
      request: (callback) => window.requestAnimationFrame(callback),
      cancel: (handle) => window.cancelAnimationFrame(handle),
    },
  );
  const offEvents = socket.on(batcher.push);
  socket.start();
  return () => {
    offStatus();
    offEvents();
    batcher.dispose();
    socket.stop();
  };
}
