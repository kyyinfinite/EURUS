
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, MessageSquare, PanelRight, Plus, X } from "lucide-react";
import { api } from "../../lib/api";
import { socket } from "../../lib/ws";
import type { SessionView } from "../../lib/types";
import { useGateway } from "../../store/gateway";
import { useUI } from "../../store/ui";
import { Badge, Button, Drawer, IconButton, StatusDot } from "../ui";
import { ConversationSidebar } from "./ConversationSidebar";
import { ConversationView } from "./ConversationView";
import { ContextPanelBody } from "./ContextPanel";
import { OrbitalLines } from "../visual/Decorations";
import { formatRelative } from "../../lib/format";

export function ChatWorkspace() {
  const { sessionId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const contextOpen = useUI((state) => state.contextPanelOpen);
  const toggleContext = useUI((state) => state.toggleContextPanel);
  const [conversationsOpen, setConversationsOpen] = useState(false);
  const [contextSheetOpen, setContextSheetOpen] = useState(false);

  const sessionsQuery = useQuery({
    queryKey: ["sessions"],
    queryFn: () => api.get<SessionView[]>("/api/sessions"),
  });
  const sessions = sessionsQuery.data ?? [];
  const active = useMemo(
    () => sessions.find((session) => session.id === sessionId) ?? null,
    [sessions, sessionId],
  );

  useEffect(() => {
    if (!sessionId && sessions.length > 0) {
      navigate(`/chat/${sessions[0].id}`, { replace: true });
    }
  }, [sessionId, sessions, navigate]);

  useEffect(() => {
    if (sessionId) socket.subscribe([sessionId]);
    return () => {
      if (sessionId) socket.unsubscribe([sessionId]);
    };
  }, [sessionId]);

  const lastChange = useGateway((state) => state.lastSessionChange);
  useEffect(() => {
    if (lastChange > 0) {
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
      if (sessionId) {
        void queryClient.invalidateQueries({ queryKey: ["messages", sessionId] });
      }
    }
  }, [lastChange, queryClient, sessionId]);

  const cancelRun = useMutation({
    mutationFn: (runId: string) => api.post(`/api/runs/${runId}/cancel`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["sessions"] }),
  });

  const createSession = useMutation({
    mutationFn: () => api.post<{ id: string }>("/api/sessions", { title: "New conversation" }),
    onSuccess: (session) => {
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
      navigate(`/chat/${session.id}`);
    },
  });

  const selectSession = (id: string) => {
    setConversationsOpen(false);
    navigate(`/chat/${id}`);
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex min-h-[53px] items-center justify-between gap-2 border-b border-line bg-surface/95 px-2.5 py-2 backdrop-blur-sm sm:px-4">
        <div className="flex min-w-0 items-center gap-2">
          <IconButton
            label="Browse conversations"
            onClick={() => setConversationsOpen(true)}
            size={34}
            className="lg:hidden"
          >
            <MessageSquare size={16} className="text-dim" />
          </IconButton>
          <div className="min-w-0">
            <h1 className="truncate text-sm font-semibold text-ink">
              {active ? active.title || "Untitled" : "Chat"}
            </h1>
            <p className="hidden truncate text-2xs text-faint sm:block">
              {active
                ? `${active.message_count} messages · updated ${formatRelative(active.updated_at)}`
                : "Select or create a conversation"}
            </p>
          </div>
          {active?.active_run_id && (
            <Badge tone="active" className="hidden sm:inline-flex">
              <StatusDot tone="active" pulse /> run active
            </Badge>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => setContextSheetOpen(true)}
            className="flex h-[34px] items-center gap-1.5 rounded-xl border border-line bg-raised-soft px-2.5 text-2xs text-dim shadow-[inset_0_1px_0_rgba(255,255,255,0.6)] transition-colors hover:text-copper xl:hidden"
          >
            <Activity size={13} /> Context
          </button>
          <IconButton
            label={contextOpen ? "Hide context panel" : "Show context panel"}
            onClick={toggleContext}
            size={34}
            variant={contextOpen ? "default" : "ghost"}
            className="hidden xl:inline-flex"
          >
            <PanelRight size={16} className={contextOpen ? "text-copper" : "text-dim"} />
          </IconButton>
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        <aside className="hidden w-64 shrink-0 border-r border-line bg-surface lg:flex xl:w-72">
          <ConversationSidebar
            activeSessionId={sessionId ?? null}
            onSelect={selectSession}
          />
        </aside>

        {sessionId ? (
          <ConversationView
            key={sessionId}
            sessionId={sessionId}
            activeRunId={active?.active_run_id ?? null}
          />
        ) : (
          <div className="relative flex flex-1 items-center justify-center overflow-hidden p-8">
            <OrbitalLines className="pointer-events-none absolute left-1/2 top-1/2 h-auto w-[560px] max-w-none -translate-x-1/2 -translate-y-1/2 opacity-[0.12]" />
            <div className="relative flex max-w-xs flex-col items-center gap-4 text-center">
              <p className="text-sm leading-relaxed text-muted">
                Open the conversations panel or start a new one to work with EURUS.
              </p>
              <Button
                variant="primary"
                onClick={() => createSession.mutate()}
                disabled={createSession.isPending}
              >
                <Plus size={14} /> New conversation
              </Button>
            </div>
          </div>
        )}

        {contextOpen && (
          <aside className="hidden w-80 shrink-0 overflow-y-auto border-l border-line bg-surface xl:block">
            <div className="sticky top-0 z-10 flex items-center justify-between border-b border-line-soft bg-surface px-3.5 py-2.5">
              <h2 className="text-2xs font-semibold uppercase tracking-[0.14em] text-dim">
                Runtime context
              </h2>
              <IconButton label="Hide context panel" onClick={toggleContext} size={26}>
                <X size={13} className="text-faint" />
              </IconButton>
            </div>
            <ContextPanelBody
              session={active}
              activeRunId={active?.active_run_id ?? null}
              onCancelRun={(runId) => cancelRun.mutate(runId)}
            />
          </aside>
        )}
      </div>

      <Drawer
        open={conversationsOpen}
        onClose={() => setConversationsOpen(false)}
        title="Conversations"
        side="left"
        widthClass="w-[85vw] max-w-sm"
      >
        <ConversationSidebar
          activeSessionId={sessionId ?? null}
          onSelect={selectSession}
        />
      </Drawer>

      <Drawer
        open={contextSheetOpen}
        onClose={() => setContextSheetOpen(false)}
        title="Runtime context"
        side="bottom"
      >
        <ContextPanelBody
          session={active}
          activeRunId={active?.active_run_id ?? null}
          onCancelRun={(runId) => cancelRun.mutate(runId)}
        />
      </Drawer>
    </div>
  );
}
