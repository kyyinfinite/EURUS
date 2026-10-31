/** Floating chat — a compact entry point into the *canonical* EURUS chat
 *  system, available on every page (welcome screen included). It shares the
 *  exact same state as the full Chat workspace: the same react-query cache,
 *  the same WebSocket store, the same session records. Opening it never
 *  creates a parallel chat system.
 *
 *  Desktop: bottom-right panel (390px wide, max 620px tall) with session
 *  selector, minimize, expand-to-workspace and streaming responses.
 *  Mobile: compact bottom sheet (~70dvh) with optional full-screen expansion,
 *  safe-area aware and keyboard friendly — never auto full-screen.
 */

import { useEffect, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, Maximize2, MessagesSquare, Minus, Plus, X } from "lucide-react";
import { api } from "../../lib/api";
import { socket } from "../../lib/ws";
import type { SessionView } from "../../lib/types";
import { useGateway } from "../../store/gateway";
import { useUI } from "../../store/ui";
import { EurusMark } from "../Logo";
import { StatusDot } from "../ui";
import { ConversationView } from "./ConversationView";
import { SessionPicker } from "./ConversationSidebar";

export function FloatingChat() {
  const open = useUI((state) => state.floatingChatOpen);
  const openFloating = useUI((state) => state.openFloatingChat);
  const closeFloating = useUI((state) => state.closeFloatingChat);
  const floatingSessionId = useUI((state) => state.floatingSessionId);
  const setFloatingSession = useUI((state) => state.setFloatingSession);
  const expanded = useUI((state) => state.floatingExpanded);
  const setFloatingExpanded = useUI((state) => state.setFloatingExpanded);
  const connected = useGateway((state) => state.connected);
  const turns = useGateway((state) => state.turns);
  const sessionTurnsMap = useGateway((state) => state.sessionTurns);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const sessionsQuery = useQuery({
    queryKey: ["sessions"],
    queryFn: () => api.get<SessionView[]>("/api/sessions"),
    enabled: open || undefined,
  });
  const sessions = sessionsQuery.data ?? [];

  // Resolve the effective session: pinned choice → most recent → null (new).
  const sessionId = useMemo(() => {
    if (floatingSessionId && sessions.some((session) => session.id === floatingSessionId)) {
      return floatingSessionId;
    }
    if (sessions.length > 0) return sessions[0].id;
    return null;
  }, [floatingSessionId, sessions]);

  const active = sessions.find((session) => session.id === sessionId) ?? null;

  // Subscribe to the floating session's live events (mirrors Chat page logic).
  useEffect(() => {
    if (open && sessionId) socket.subscribe([sessionId]);
    return () => {
      if (open && sessionId) socket.unsubscribe([sessionId]);
    };
  }, [open, sessionId]);

  // Keep the floating session in sync with the full workspace selection.
  useEffect(() => {
    if (open && !floatingSessionId && sessionId) setFloatingSession(sessionId);
  }, [open, floatingSessionId, sessionId, setFloatingSession]);

  const createSession = useMutation({
    mutationFn: () => api.post<{ id: string }>("/api/sessions", { title: "New conversation" }),
    onSuccess: (session) => {
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
      setFloatingSession(session.id);
    },
  });

  const runningTurn =
    sessionId && (sessionTurnsMap[sessionId] ?? []).some((runId) => turns[runId]?.status === "running");

  // Close on Escape for keyboard users — but never while typing, and let
  // open modals/drawers (which trap focus) handle their own Escape first.
  useEffect(() => {
    if (!open) return;
    const handler = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      const target = event.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable)
      ) {
        return;
      }
      if (document.querySelector('[role="dialog"][aria-modal="true"]')) return;
      closeFloating();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [open, closeFloating]);

  const expandToWorkspace = () => {
    closeFloating();
    navigate(sessionId ? `/chat/${sessionId}` : "/chat");
  };

  return (
    <>
      {/* global launcher */}
      {!open && (
        <button
          type="button"
          aria-label="Open EURUS assistant"
          aria-expanded={false}
          onClick={() => openFloating()}
          className="fixed bottom-[calc(var(--safe-bottom)+1.25rem)] right-4 z-[60] flex h-[54px] w-[54px] items-center justify-center rounded-full bg-copper text-white shadow-launcher transition-all hover:bg-copper-dark active:scale-95 sm:right-6"
        >
          <MessagesSquare size={21} />
          {connected && runningTurn && (
            <span className="absolute -right-0.5 -top-0.5 h-3.5 w-3.5 animate-pulse-soft rounded-full border-2 border-surface bg-warn" />
          )}
        </button>
      )}

      {open && !expanded && (
        <div
          className="fixed inset-0 z-[64] animate-fade-in bg-ink/25 sm:hidden"
          onClick={closeFloating}
          aria-hidden="true"
        />
      )}

      {open && (
        <div
          role="dialog"
          aria-label="EURUS assistant"
          className={
            expanded
              ? // expanded mode: full screen (explicit user choice)
                "fixed inset-0 z-[65] flex animate-fade-in flex-col overflow-hidden border border-line bg-surface shadow-overlay safe-top safe-bottom"
              : // mobile: compact bottom sheet (~70dvh); desktop: 390px panel
                "fixed z-[65] flex animate-slide-up-sheet flex-col overflow-hidden rounded-t-2xl border border-line bg-surface shadow-overlay " +
                "inset-x-0 bottom-0 h-[72dvh] safe-bottom " +
                "sm:inset-auto sm:bottom-[calc(var(--safe-bottom)+5.75rem)] sm:right-6 sm:h-[min(620px,calc(100dvh-8.5rem))] sm:w-[390px] sm:rounded-2xl"
          }
        >
          {/* mobile drag handle */}
          <div className="flex justify-center pb-1 pt-2.5 sm:hidden" aria-hidden="true">
            <div className="h-1.5 w-10 rounded-full bg-line" />
          </div>

          <header className="flex items-center justify-between gap-2 border-b border-line-soft px-2.5 py-2.5 sm:px-3">
            <div className="flex min-w-0 items-center gap-2">
              <button
                type="button"
                onClick={expandToWorkspace}
                aria-label="Open in Chat workspace"
                title="Open in Chat workspace"
                className="hidden shrink-0 sm:block"
              >
                <EurusMark size={24} className="transition-transform hover:scale-105" />
              </button>
              <SessionPicker
                sessions={sessions}
                activeId={sessionId}
                onSelect={setFloatingSession}
                onCreate={() => createSession.mutate()}
              />
            </div>
            <div className="flex shrink-0 items-center gap-0.5">
              <span className="mr-1 hidden items-center gap-1.5 text-xs text-faint sm:flex">
                <StatusDot tone={connected ? "ok" : "danger"} pulse={!connected} />
                {connected ? "live" : "offline"}
              </span>
              <button
                type="button"
                onClick={() => createSession.mutate()}
                aria-label="New conversation"
                title="New conversation"
                disabled={createSession.isPending}
                className="flex h-10 w-10 items-center justify-center rounded-lg text-muted transition-colors hover:bg-raised-soft hover:text-copper"
              >
                <Plus size={17} />
              </button>
              <button
                type="button"
                onClick={() => setFloatingExpanded(!expanded)}
                aria-label={expanded ? "Collapse to panel" : "Expand to full screen"}
                title={expanded ? "Collapse to panel" : "Expand to full screen"}
                className="flex h-10 w-10 items-center justify-center rounded-lg text-muted transition-colors hover:bg-raised-soft hover:text-copper"
              >
                {expanded ? <ChevronDown size={16} /> : <Maximize2 size={15} />}
              </button>
              <button
                type="button"
                onClick={closeFloating}
                aria-label="Close floating chat"
                title="Close"
                className="flex h-10 w-10 items-center justify-center rounded-lg text-muted transition-colors hover:bg-raised-soft hover:text-ink"
              >
                {expanded ? <X size={16} /> : <Minus size={16} className="hidden sm:block" />}
                {expanded ? null : <X size={16} className="sm:hidden" />}
              </button>
            </div>
          </header>

          {sessionId ? (
            <ConversationView
              key={sessionId}
              sessionId={sessionId}
              activeRunId={active?.active_run_id ?? null}
              variant="floating"
            />
          ) : (
            <div className="flex flex-1 flex-col items-center justify-center gap-3.5 p-6 text-center">
              <EurusMark size={38} />
              <p className="max-w-[260px] text-sm leading-relaxed text-muted">
                No conversations yet. Start one — it uses the same canonical session store as the
                full workspace.
              </p>
              <button
                type="button"
                onClick={() => createSession.mutate()}
                disabled={createSession.isPending}
                className="rounded-full border border-copper/50 bg-copper-soft px-4 py-2 text-[13px] font-semibold text-copper-dark transition-colors hover:bg-copper/20"
              >
                <Plus size={14} className="mr-1.5 inline" /> New conversation
              </button>
            </div>
          )}
        </div>
      )}
    </>
  );
}
