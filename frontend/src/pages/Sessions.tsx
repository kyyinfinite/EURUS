/** Sessions — browse, inspect, continue and delete canonical sessions. */

import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, MessageSquare, MessagesSquare, Plus, Search, Trash2, X } from "lucide-react";
import { api } from "../lib/api";
import type { MessageView, SessionView } from "../lib/types";
import { useUI } from "../store/ui";
import { formatRelative, formatTime } from "../lib/format";
import { Badge, Button, Empty, IconButton, Modal, PageHeader, StatusDot } from "../components/ui";
import { MessageRenderer } from "../components/chat/MessageRenderer";

export default function Sessions() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<SessionView | null>(null);
  const pushToast = useUI((state) => state.pushToast);

  const sessionsQuery = useQuery({
    queryKey: ["sessions"],
    queryFn: () => api.get<SessionView[]>("/api/sessions"),
    refetchInterval: 5000,
  });

  const create = useMutation({
    mutationFn: () => api.post<{ id: string }>("/api/sessions", { title: "New conversation" }),
    onSuccess: (session) => {
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
      navigate(`/chat/${session.id}`);
    },
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.delete(`/api/sessions/${id}`),
    onSuccess: () => {
      setConfirmDelete(null);
      setSelected(null);
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
      pushToast({ tone: "info", title: "Session deleted" });
    },
    onError: (error) =>
      pushToast({
        tone: "danger",
        title: "Delete failed",
        detail: error instanceof Error ? error.message : undefined,
      }),
  });

  const sessions = sessionsQuery.data ?? [];
  const needle = search.trim().toLowerCase();
  const filtered = needle
    ? sessions.filter(
        (session) => session.title.toLowerCase().includes(needle) || session.id.includes(needle),
      )
    : sessions;
  const detailId = selected ?? filtered[0]?.id ?? null;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Sessions"
        subtitle="Canonical conversations shared by the TUI, CLI and this workspace"
        actions={
          <Button variant="primary" onClick={() => create.mutate()} disabled={create.isPending}>
            <Plus size={13} /> New session
          </Button>
        }
      />
      <div className="grid min-h-0 flex-1 lg:grid-cols-[340px_1fr]">
        {/* session list */}
        <div className="flex min-h-0 flex-col border-b border-line bg-surface lg:border-b-0 lg:border-r">
          <div className="border-b border-line-soft p-3">
            <div className="relative">
              <Search size={13} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" />
              <input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Search sessions…"
                aria-label="Search sessions"
                className="w-full py-1.5 pl-8 pr-7 text-2xs"
              />
              {search && (
                <button
                  type="button"
                  aria-label="Clear search"
                  onClick={() => setSearch("")}
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-faint hover:text-ink"
                >
                  <X size={12} />
                </button>
              )}
            </div>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {sessionsQuery.isLoading ? (
              <p className="p-4 text-center text-2xs text-faint">Loading…</p>
            ) : filtered.length === 0 ? (
              <div className="p-3">
                <Empty>{needle ? "No sessions match." : "No sessions yet."}</Empty>
              </div>
            ) : (
              <ul>
                {filtered.map((session) => (
                  <li key={session.id}>
                    <button
                      type="button"
                      onClick={() => setSelected(session.id)}
                      className={`block w-full border-l-2 px-4 py-3 text-left transition-colors ${
                        session.id === detailId
                          ? "border-copper bg-copper-soft"
                          : "border-transparent hover:bg-raised-soft"
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate text-xs text-ink">
                          {session.title || "Untitled"}
                        </span>
                        {session.active_run_id && (
                          <StatusDot tone="active" pulse aria-label="run active" />
                        )}
                      </div>
                      <div className="mt-1 flex items-center gap-2 font-mono text-2xs text-faint">
                        <MessageSquare size={9} /> {session.message_count} ·{" "}
                        {formatRelative(session.updated_at)}
                      </div>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        {/* detail pane */}
        <SessionDetail
          sessionId={detailId}
          onBack={() => setSelected(null)}
          onDelete={(session) => setConfirmDelete(session)}
        />
      </div>

      <Modal
        open={confirmDelete !== null}
        onClose={() => setConfirmDelete(null)}
        title="Delete session?"
        footer={
          <div className="flex justify-end gap-2">
            <Button size="sm" onClick={() => setConfirmDelete(null)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              size="sm"
              onClick={() => confirmDelete && remove.mutate(confirmDelete.id)}
              disabled={remove.isPending}
            >
              <Trash2 size={13} /> Delete
            </Button>
          </div>
        }
      >
        <p className="text-xs leading-relaxed text-dim">
          <span className="text-ink">{confirmDelete?.title}</span> and its{" "}
          {confirmDelete?.message_count ?? 0} messages will be removed from the canonical store.
          This cannot be undone.
        </p>
      </Modal>
    </div>
  );
}

function SessionDetail({
  sessionId,
  onBack,
  onDelete,
}: {
  sessionId: string | null;
  onBack: () => void;
  onDelete: (session: SessionView) => void;
}) {
  const navigate = useNavigate();
  const sessionsQuery = useQuery({
    queryKey: ["sessions"],
    queryFn: () => api.get<SessionView[]>("/api/sessions"),
  });
  const messagesQuery = useQuery({
    queryKey: ["messages", sessionId],
    queryFn: () => api.get<MessageView[]>(`/api/sessions/${sessionId}/messages`),
    enabled: Boolean(sessionId),
  });

  const session = (sessionsQuery.data ?? []).find((item) => item.id === sessionId) ?? null;
  const messages = messagesQuery.data ?? [];

  if (!sessionId || !session) {
    return (
      <div className="flex flex-1 items-center justify-center p-8">
        <Empty>Select a session to inspect its history.</Empty>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-line bg-surface px-4 py-3">
        <div className="flex min-w-0 items-center gap-2">
          <IconButton label="Back to list" onClick={onBack} size={30} className="lg:hidden">
            <ArrowLeft size={15} className="text-dim" />
          </IconButton>
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold text-ink">{session.title || "Untitled"}</h2>
            <p className="truncate font-mono text-2xs text-faint">
              {session.id} · created {formatTime(session.created_at)}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="primary" size="sm" onClick={() => navigate(`/chat/${session.id}`)}>
            <MessagesSquare size={13} /> Continue in Chat
          </Button>
          <Button variant="danger" size="sm" onClick={() => onDelete(session)}>
            <Trash2 size={13} /> Delete
          </Button>
        </div>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        <div className="mx-auto max-w-2xl space-y-2.5">
          {messagesQuery.isLoading ? (
            <p className="py-8 text-center text-2xs text-faint">Loading messages…</p>
          ) : messages.length === 0 ? (
            <Empty>No messages in this session yet.</Empty>
          ) : (
            messages
              .filter((message) => message.role !== "tool")
              .map((message, index) => (
                <div
                  key={`${message.sequence}-${index}`}
                  className={`rounded-xl border px-3.5 py-2.5 ${
                    message.role === "user"
                      ? "border-copper/30 bg-copper/[0.06]"
                      : "border-line bg-surface"
                  }`}
                >
                  <div className="mb-1.5 flex items-center gap-2">
                    <Badge tone={message.role === "user" ? "bronze" : "dim"}>{message.role}</Badge>
                  </div>
                  <MessageRenderer message={message} />
                </div>
              ))
          )}
        </div>
      </div>
    </div>
  );
}
