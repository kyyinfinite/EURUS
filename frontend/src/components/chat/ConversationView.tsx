/** ConversationView — the canonical conversation surface.
 *
 *  Shared by the full Chat workspace and the floating chat panel so both are
 *  literally the same code path over the same canonical state: react-query
 *  caches + the gateway WebSocket store. No duplicate submissions are
 *  possible — the composer disables while a run is active (backend 409s
 *  anyway) and both surfaces invalidate the same query keys.
 *
 *  Streaming behaviour: markdown renders through the one intelligence
 *  renderer with `streaming` set; auto-scroll pins to the bottom only while
 *  the user is already there; a jump-to-latest control appears otherwise.
 */

import { memo, useCallback, useEffect, useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDown, Bot, ShieldAlert, User, Check, X } from "lucide-react";
import { api } from "../../lib/api";
import type { ApprovalView, MediaView, MessageView } from "../../lib/types";
import { useGateway } from "../../store/gateway";
import { Badge, Button, Spinner, useCountdown, useAutoScroll } from "../ui";
import { InlineActivity } from "../EventTrace";
import { Markdown } from "../markdown/Markdown";
import { MessageRenderer } from "./MessageRenderer";
import { ChatComposer } from "./ChatComposer";
import type { LiveTurn } from "../../store/gateway";

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
  const turns = useGateway((state) => state.turns);
  const sessionTurnsMap = useGateway((state) => state.sessionTurns);
  const approvals = useGateway((state) => state.approvals);
  const { ref: scrollRef, onScroll, notify, reset, scrollToBottom, showJump } = useAutoScroll<HTMLDivElement>();

  const sessionTurns = useMemo(
    () => sessionTurnsMap[sessionId] ?? [],
    [sessionTurnsMap, sessionId],
  );

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
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
      onSent?.();
    },
  });

  const cancel = useMutation({
    mutationFn: (runId: string) => api.post(`/api/runs/${runId}/cancel`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
    },
  });

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

  // Keep the view pinned to the bottom while the user is already there.
  useEffect(() => {
    notify();
  }, [items.length, turns, notify]);

  useEffect(() => {
    reset();
  }, [sessionId, reset]);

  const pendingApproval = approvals.find((approval) => approval.session_id === sessionId);
  const runningTurn = sessionTurns
    .map((runId) => turns[runId])
    .find((turn) => turn && turn.status === "running");
  const runActive = Boolean(runningTurn) || Boolean(activeRunId);
  const runningRunId = runningTurn?.runId ?? activeRunId;

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
              ) : item.runId && turns[item.runId] ? (
                <LiveTurnCard key={item.key} turn={turns[item.runId]} />
              ) : null,
            )
          )}
          {!runningTurn && activeRunId && !sessionTurns.includes(activeRunId) && (
            <div className="flex items-center gap-2.5 rounded-xl border border-copper/25 bg-copper-tint px-4 py-2.5 text-[13px] text-copper-dark">
              <Spinner size={13} /> A run is active on this session (started elsewhere).
            </div>
          )}
        </div>
      </div>

      {/* jump-to-latest (streaming) */}
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
        disabled={runActive && !runningTurn}
        placeholder={runActive ? "A run is active on this session…" : "Message EURUS — Enter to send"}
      />
    </div>
  );
});

function MessageBubble({ message }: { message: MessageView }) {
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
}

function LiveTurnCard({ turn }: { turn: LiveTurn }) {
  const visibleEvents = turn.events.filter(
    (event) => event.kind !== "text" && event.kind !== "text_reset",
  );
  const streaming = turn.status === "running";
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
        <div className="mb-1.5 flex flex-wrap items-center gap-2">
          {turn.origin === "external" ? <Badge tone="sage">observed · external run</Badge> : null}
          {streaming && <Spinner label="working" size={13} />}
          {turn.status === "failed" && (
            <Badge tone="danger">failed{turn.stopReason ? ` · ${turn.stopReason}` : ""}</Badge>
          )}
          {turn.status === "cancelled" && <Badge tone="dim">cancelled</Badge>}
        </div>
        <InlineActivity events={visibleEvents} />
        {turn.text && <Markdown text={turn.text} streaming={streaming} />}
        {streaming && !turn.text && visibleEvents.length === 0 && (
          <p className="text-sm text-muted">Waiting for the agent…</p>
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
