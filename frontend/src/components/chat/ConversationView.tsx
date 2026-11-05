

import { memo, useCallback, useEffect, useMemo, useRef } from "react";
import { useShallow } from "zustand/react/shallow";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDown, Bot, ShieldAlert, User, Check, X } from "lucide-react";
import { api } from "../../lib/api";
import { createOptimisticUserMessage } from "../../lib/chat";
import type { ApprovalView, MediaView, MessageView, SessionView } from "../../lib/types";
import { useGateway } from "../../store/gateway";
import { Badge, Button, Spinner, useCountdown, useAutoScroll } from "../ui";
import { Markdown } from "../markdown/Markdown";
import { MessageRenderer } from "./MessageRenderer";
import { ChatComposer } from "./ChatComposer";
import { ThinkingPanel } from "./ThinkingPanel";

const EMPTY_TURNS: string[] = [];

interface ChatItem {
  key: string;
  kind: "message" | "turn";
  sequence: number;
  message?: MessageView;
  runId?: string;
}

export interface ConversationViewProps {
  sessionId: string;
  activeRunId: string | null;
  variant?: "page" | "floating" | "drawer";
  onSent?: () => void;
}

export const ConversationView = memo(function ConversationView({
  sessionId,
  activeRunId,
  variant = "page",
  onSent,
}: ConversationViewProps) {
  const queryClient = useQueryClient();
  const sessionTurns = useGateway(
    useShallow((state) => state.sessionTurns[sessionId] ?? EMPTY_TURNS),
  );
  const pendingApproval = useGateway(
    useShallow((state) => state.approvals.find((approval) => approval.session_id === sessionId)),
  );
  const turnStatuses = useGateway(
    useShallow((state) =>
      Object.fromEntries(sessionTurns.map((runId) => [runId, state.turns[runId]?.status])),
    ),
  );
  const { ref: scrollRef, onScroll, notify, reset, scrollToBottom, showJump } = useAutoScroll<HTMLDivElement>();

  const sessionsQuery = useQuery({
    queryKey: ["sessions"],
    queryFn: () => api.get<SessionView[]>("/api/sessions"),
  });

  const messagesQuery = useQuery({
    queryKey: ["messages", sessionId],
    queryFn: () => api.get<MessageView[]>(`/api/sessions/${sessionId}/messages`),
  });

  const send = useMutation({
    mutationFn: (input: { text: string; attachments: MediaView[] }) =>
      api.post<{ run_id: string }>(`/api/sessions/${sessionId}/messages`, {
        text: input.text,
        attachments: input.attachments.map((media) => media.id),
      }),
    onMutate: async (input) => {
      await queryClient.cancelQueries({ queryKey: ["messages", sessionId], exact: true });
      const previousMessages = queryClient.getQueryData<MessageView[]>(["messages", sessionId]);
      const optimisticMessage = createOptimisticUserMessage(input.text, previousMessages);
      queryClient.setQueryData<MessageView[]>(["messages", sessionId], [
        ...(previousMessages ?? []),
        optimisticMessage,
      ]);
      return { previousMessages };
    },
    onError: (_error, _input, context) => {
      if (context?.previousMessages) {
        queryClient.setQueryData(["messages", sessionId], context.previousMessages);
      } else {
        queryClient.removeQueries({ queryKey: ["messages", sessionId], exact: true });
      }
    },
    onSuccess: () => {
      for (const runId of sessionTurns) {
        const turn = useGateway.getState().turns[runId];
        if (turn && (turn.status === "failed" || turn.status === "cancelled")) {
          useGateway.getState().clearTurn(runId);
        }
      }
      void queryClient.invalidateQueries({ queryKey: ["messages", sessionId] });
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
      onSent?.();
    },
  });

  const cancel = useMutation({
    mutationFn: (runId: string) => api.post(`/api/runs/${runId}/cancel`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["messages", sessionId] });
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
    },
  });

  const cleanupStarted = useRef(new Set<string>());
  const terminalRunIds = useRef(new Set<string>());

  useEffect(() => {
    const completedRunIds = sessionTurns.filter((runId) => turnStatuses[runId] === "completed");

    for (const runId of completedRunIds) {
      if (cleanupStarted.current.has(runId)) continue;
      cleanupStarted.current.add(runId);
      terminalRunIds.current.add(runId);
      void (async () => {
        for (let attempt = 0; attempt < 20; attempt += 1) {
          await queryClient.refetchQueries({ queryKey: ["messages", sessionId], exact: true });
          const loaded = queryClient.getQueryData<MessageView[]>(["messages", sessionId]) ?? [];
          const lastUserIndex = loaded.reduce(
            (last, message, index) => message.role === "user" ? index : last,
            -1,
          );
          const assistantPersisted = loaded.some(
            (message, index) => index > lastUserIndex && message.role === "assistant",
          );
          if (assistantPersisted) {
            useGateway.getState().clearTurn(runId);
            return;
          }
          if (attempt < 19) await new Promise((resolve) => window.setTimeout(resolve, 350));
        }
        cleanupStarted.current.delete(runId);
      })();
    }
  }, [queryClient, sessionId, sessionTurns, turnStatuses]);

  useEffect(() => {
    const handleResync = (event: Event) => {
      const detail = (event as CustomEvent<{ sessions: string[] }>).detail;
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
      if (detail.sessions.includes(sessionId)) {
        void queryClient.invalidateQueries({ queryKey: ["messages", sessionId] });
      }
    };
    window.addEventListener("eurus:gateway-resync", handleResync);
    return () => window.removeEventListener("eurus:gateway-resync", handleResync);
  }, [queryClient, sessionId]);

  const messages = messagesQuery.data ?? [];

  const items = useMemo<ChatItem[]>(() => {
    const list: ChatItem[] = messages.map((message, index) => ({
      key: `m${message.sequence}-${index}`,
      kind: "message",
      sequence: index,
      message,
    }));
    for (const runId of sessionTurns) {
      list.push({ key: `t${runId}`, kind: "turn", sequence: 10_000 + list.length, runId });
    }
    return list;
  }, [messages, sessionTurns]);
  useEffect(() => {
    notify();
  }, [items.length, notify]);

  useEffect(() => {
    reset();
  }, [sessionId, reset]);

  const runningTurnId = sessionTurns.find((runId) => turnStatuses[runId] === "running");
  const activeLocalStatus = useGateway((state) =>
    activeRunId ? state.turns[activeRunId]?.status : undefined,
  );
  const activeRunIsTerminal = Boolean(
    activeLocalStatus && activeLocalStatus !== "running",
  ) || Boolean(activeRunId && terminalRunIds.current.has(activeRunId));
  const sessionDataIsFresh = !sessionsQuery.isLoading && !sessionsQuery.isFetching;
  const externalActiveRunId =
    activeRunId &&
    !sessionTurns.includes(activeRunId) &&
    !activeRunIsTerminal &&
    sessionDataIsFresh
      ? activeRunId
      : null;
  const runActive = Boolean(runningTurnId || externalActiveRunId);
  const runningRunId = runningTurnId ?? externalActiveRunId;

  const handleSend = useCallback(
    async (text: string, attachments: MediaView[]) => {
      await send.mutateAsync({ text, attachments });
    },
    [send],
  );

  const maxWidth = variant === "page" ? "max-w-3xl" : "max-w-2xl";

  return (
    <div className="relative flex min-h-0 min-w-0 flex-1 flex-col">
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-3 py-5 sm:px-6"
        aria-label="Conversation"
      >
        <div className={`mx-auto flex flex-col gap-4 ${maxWidth}`}>
          {messagesQuery.isLoading ? (
            <div className="py-10">
              <div className="flex justify-center">
                <Spinner label="Loading conversation…" size={16} />
              </div>
            </div>
          ) : items.length === 0 ? (
            <div className="mt-12 text-center sm:mt-20">
              <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl border border-copper/25 bg-copper-soft">
                <Bot size={26} className="text-copper" />
              </div>
              <p className="mx-auto max-w-sm text-sm leading-relaxed text-muted">
                Start the conversation. EURUS runs with its full toolset — coding tools, research,
                memory, MCP servers — exactly like the terminal.
              </p>
            </div>
          ) : (
            items.map((item) =>
              item.kind === "message" && item.message ? (
                <MessageBubble key={item.key} message={item.message} />
              ) : item.runId ? (
                <LiveTurnCard key={item.key} runId={item.runId} />
              ) : null,
            )
          )}
          {!runningTurnId && externalActiveRunId && sessionDataIsFresh && (
            <div className="flex items-center gap-2.5 rounded-xl border border-copper/25 bg-copper-tint px-4 py-2.5 text-[13px] text-copper-dark">
              <Spinner size={13} /> A run is active on this session (started elsewhere).
            </div>
          )}
        </div>
      </div>

      {showJump && (
        <div className="pointer-events-none absolute inset-x-0 bottom-[104px] z-10 flex justify-center">
          <button
            type="button"
            onClick={() => scrollToBottom("smooth")}
            className="pointer-events-auto flex items-center gap-1.5 rounded-full border border-line bg-surface px-3.5 py-1.5 text-xs font-semibold text-copper-dark shadow-float transition-colors hover:border-copper/50"
          >
            <ArrowDown size={13} />
            {runActive ? "Jump to latest" : "Back to latest"}
          </button>
        </div>
      )}

      {pendingApproval && <ApprovalBanner approval={pendingApproval} />}

      {send.isError && (
        <div className="border-t border-danger/30 bg-danger-soft px-4 py-2.5 text-[13px] text-danger">
          {send.error instanceof Error ? send.error.message : "send failed"} — your message was
          restored to the composer.
        </div>
      )}

      <ChatComposer
        sessionId={sessionId}
        onSend={handleSend}
        onCancel={runningRunId ? () => cancel.mutate(runningRunId) : undefined}
        busy={send.isPending}
        running={runActive}
        disabled={runActive && !runningTurnId}
        placeholder={runActive ? "A run is active on this session…" : "Message EURUS — Enter to send"}
      />
    </div>
  );
});

const MessageBubble = memo(function MessageBubble({ message }: { message: MessageView }) {
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="flex max-w-[92%] gap-3 rounded-2xl rounded-br-md border border-copper/25 bg-copper-tint px-4 py-3 sm:max-w-[80%]">
          <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-copper/30 bg-surface">
            <User size={13} className="text-copper" />
          </div>
          <MessageRenderer message={message} />
        </div>
      </div>
    );
  }
  if (message.role === "assistant") {
    return (
      <div className="flex max-w-[94%] gap-3 rounded-2xl rounded-bl-md border border-line bg-surface px-4 py-3 shadow-card sm:max-w-[88%]">
        <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-line bg-raised-soft">
          <Bot size={13} className="text-copper-dark" />
        </div>
        <MessageRenderer message={message} />
      </div>
    );
  }
  if (message.role === "tool") {
    return (
      <div className="pl-10 sm:pl-11">
        <MessageRenderer message={message} />
      </div>
    );
  }
  return null;
});

function LiveTurnCard({ runId }: { runId: string }) {
  const turn = useGateway((state) => state.turns[runId]);
  if (!turn) return null;
  const visibleEvents = turn.events.filter(
    (event) => event.kind !== "text" && event.kind !== "text_reset",
  );
  const streaming = turn.status === "running";
  const objective = turn.runtimeEvents.find((event) => event.event_type === "run.started")?.payload.objective;
  return (
    <div
      className={`flex max-w-[94%] gap-3 rounded-2xl rounded-bl-md border px-4 py-3 shadow-card sm:max-w-[88%] ${
        turn.origin === "external" ? "border-sage/30 bg-sage-soft/50" : "border-line bg-surface"
      }`}
    >
      <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-line bg-raised-soft">
        <Bot
          size={13}
          className={streaming ? "animate-pulse-soft text-copper" : "text-copper-dark"}
        />
      </div>
      <div className="min-w-0 flex-1">
        {turn.origin === "external" && objective ? (
          <p className="mb-2 rounded-lg border border-sage/20 bg-sage-soft/60 px-3 py-2 text-xs leading-relaxed text-sage">
            <span className="mb-0.5 block text-[9px] font-semibold uppercase tracking-[0.14em]">Observed objective</span>
            {String(objective)}
          </p>
        ) : null}
        <ThinkingPanel
          status={turn.status}
          origin={turn.origin}
          events={visibleEvents}
          runtimeEvents={turn.runtimeEvents}
          stopReason={turn.stopReason}
        />
        {turn.text && <div className="mt-3"><Markdown text={turn.text} streaming={streaming} /></div>}
        {streaming && !turn.text && visibleEvents.length === 0 && (
          <p className="mt-3 text-xs text-muted">Preparing the first activity update…</p>
        )}
      </div>
    </div>
  );
}

function ApprovalBanner({ approval }: { approval: ApprovalView }) {
  const queryClient = useQueryClient();
  const remaining = useCountdown(approval.expires_in_seconds);
  const resolve = useMutation({
    mutationFn: (allowed: boolean) =>
      api.post(`/api/approvals/${approval.token}/resolve`, {
        session_id: approval.session_id,
        decision: allowed ? "approved" : "denied",
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["approvals"] });
    },
  });
  return (
    <div className="border-t border-warn/40 bg-warn-soft px-4 py-3.5">
      <div className="mx-auto max-w-3xl">
        <div className="flex flex-wrap items-center gap-2 text-sm font-semibold text-warn">
          <ShieldAlert size={16} className="shrink-0" />
          <span className="truncate">Approval required — {approval.tool_name}</span>
          <Badge tone={remaining > 30 ? "warn" : "danger"}>expires in {remaining}s</Badge>
        </div>
        <pre className="mt-2.5 max-h-36 overflow-auto whitespace-pre-wrap break-all rounded-lg border border-line bg-surface p-3 font-mono text-2xs text-dim">
          {approval.arguments_json}
          {approval.display_notice ? `\n${approval.display_notice}` : ""}
        </pre>
        <div className="mt-3 flex gap-2">
          <Button
            variant="primary"
            size="sm"
            onClick={() => resolve.mutate(true)}
            disabled={resolve.isPending}
          >
            <Check size={14} /> Allow once
          </Button>
          <Button variant="danger" size="sm" onClick={() => resolve.mutate(false)} disabled={resolve.isPending}>
            <X size={14} /> Deny
          </Button>
        </div>
      </div>
    </div>
  );
}
