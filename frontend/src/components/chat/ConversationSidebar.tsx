/** ConversationSidebar — searchable session list with metadata and actions. */

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MessageSquare, Plus, Search, Trash2, X } from "lucide-react";
import { api } from "../../lib/api";
import type { SessionView } from "../../lib/types";
import { useGateway } from "../../store/gateway";
import { useUI } from "../../store/ui";
import { formatRelative } from "../../lib/format";
import { Button, Empty, IconButton, Modal } from "../ui";

export function ConversationSidebar({
  activeSessionId,
  onSelect,
  onSessionCreated,
}: {
  activeSessionId: string | null;
  onSelect: (id: string) => void;
  onSessionCreated?: (id: string) => void;
}) {
  const [search, setSearch] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<SessionView | null>(null);
  const queryClient = useQueryClient();
  const lastChange = useGateway((state) => state.lastSessionChange);
  const pushToast = useUI((state) => state.pushToast);

  const sessionsQuery = useQuery({
    queryKey: ["sessions"],
    queryFn: () => api.get<SessionView[]>("/api/sessions"),
    refetchInterval: 8000,
  });
  void lastChange;

  const create = useMutation({
    mutationFn: () => api.post<{ id: string }>("/api/sessions", { title: "New conversation" }),
    onSuccess: (session) => {
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
      onSelect(session.id);
      onSessionCreated?.(session.id);
    },
    onError: (error) =>
      pushToast({
        tone: "danger",
        title: "Could not create conversation",
        detail: error instanceof Error ? error.message : undefined,
      }),
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.delete(`/api/sessions/${id}`),
    onSuccess: () => {
      setConfirmDelete(null);
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
      pushToast({ tone: "info", title: "Conversation deleted" });
    },
    onError: (error) =>
      pushToast({
        tone: "danger",
        title: "Delete failed",
        detail: error instanceof Error ? error.message : undefined,
      }),
  });

  const sessions = sessionsQuery.data ?? [];
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return sessions;
    return sessions.filter(
      (session) =>
        session.title.toLowerCase().includes(needle) || session.id.toLowerCase().includes(needle),
    );
  }, [sessions, search]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="space-y-2.5 border-b border-line-soft p-3">
        <Button
          variant="primary"
          className="w-full justify-center"
          onClick={() => create.mutate()}
          disabled={create.isPending}
        >
          <Plus size={13} /> New conversation
        </Button>
        <div className="relative">
          <Search size={13} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" />
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search conversations…"
            aria-label="Search conversations"
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

      <div className="min-h-0 flex-1 overflow-y-auto py-1" role="list" aria-label="Conversations">
        {sessionsQuery.isLoading ? (
          <p className="px-4 py-6 text-center text-2xs text-faint">Loading…</p>
        ) : filtered.length === 0 ? (
          <div className="p-3">
            <Empty>{search ? "No conversations match." : "No conversations yet."}</Empty>
          </div>
        ) : (
          filtered.map((session) => (
            <SessionRow
              key={session.id}
              session={session}
              active={session.id === activeSessionId}
              onSelect={() => onSelect(session.id)}
              onDelete={() => setConfirmDelete(session)}
            />
          ))
        )}
      </div>

      <Modal
        open={confirmDelete !== null}
        onClose={() => setConfirmDelete(null)}
        title="Delete conversation?"
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
          {confirmDelete?.message_count ?? 0} messages will be removed from the canonical session
          store. Runs that already finished stay in the run history. This cannot be undone.
        </p>
      </Modal>
    </div>
  );
}

function SessionRow({
  session,
  active,
  onSelect,
  onDelete,
}: {
  session: SessionView;
  active: boolean;
  onSelect: () => void;
  onDelete: () => void;
}) {
  return (
    <div
      className={`group relative flex items-center gap-1 pr-1 transition-colors ${
        active ? "bg-copper-soft" : "hover:bg-raised-soft"
      }`}
      role="listitem"
    >
      <button
        type="button"
        onClick={onSelect}
        aria-current={active ? "true" : undefined}
        className="min-w-0 flex-1 px-3 py-2.5 text-left"
      >
        <div className="flex items-center justify-between gap-2">
          <span
            className={`truncate text-xs ${active ? "text-copper" : "text-dim group-hover:text-ink"}`}
          >
            {session.title || "Untitled"}
          </span>
          {session.active_run_id && (
            <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-copper" aria-label="run active" />
          )}
        </div>
        <div className="mt-0.5 flex items-center gap-1.5 font-mono text-2xs text-faint">
          <MessageSquare size={9} />
          {session.message_count} · {formatRelative(session.updated_at)}
        </div>
      </button>
      <button
        type="button"
        onClick={onDelete}
        aria-label={`Delete ${session.title}`}
        className="mr-1.5 hidden h-7 w-7 items-center justify-center rounded-lg text-faint transition-colors hover:text-danger group-hover:flex"
      >
        <Trash2 size={12} />
      </button>
      {active && (
        <span className="absolute left-0 top-1/2 h-6 w-0.5 -translate-y-1/2 rounded-r-full bg-copper" />
      )}
    </div>
  );
}

/** Compact session picker used by the floating chat header. */
export function SessionPicker({
  sessions,
  activeId,
  onSelect,
  onCreate,
}: {
  sessions: SessionView[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onCreate: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-label="Switch conversation"
        className="flex items-center gap-1.5 rounded-lg px-1.5 py-1 text-left transition-colors hover:bg-raised-soft"
      >
        <span className="max-w-[150px] truncate text-[13px] font-semibold text-ink sm:max-w-[240px]">
          {sessions.find((session) => session.id === activeId)?.title ?? "New conversation"}
        </span>
        <svg width="10" height="10" viewBox="0 0 12 12" className="text-faint" aria-hidden="true">
          <path d="M2 4l4 4 4-4" stroke="currentColor" strokeWidth="1.6" fill="none" strokeLinecap="round" />
        </svg>
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-30" onClick={() => setOpen(false)} aria-hidden="true" />
          <div
            role="listbox"
            className="absolute left-0 top-9 z-40 max-h-72 w-64 animate-scale-in overflow-y-auto rounded-xl border border-line bg-raised-soft shadow-float"
          >
            <button
              type="button"
              role="option"
              aria-selected={activeId === null}
              onClick={() => {
                onCreate();
                setOpen(false);
              }}
              className="flex w-full items-center gap-2 border-b border-line-soft px-3 py-2.5 text-left text-xs text-copper transition-colors hover:bg-surface"
            >
              <Plus size={13} /> New conversation
            </button>
            {sessions.slice(0, 20).map((session) => (
              <button
                key={session.id}
                type="button"
                role="option"
                aria-selected={session.id === activeId}
                onClick={() => {
                  onSelect(session.id);
                  setOpen(false);
                }}
                className={`block w-full truncate px-3 py-2 text-left text-xs transition-colors hover:bg-surface ${
                  session.id === activeId ? "text-copper" : "text-dim"
                }`}
              >
                <span className="block truncate">{session.title || "Untitled"}</span>
                <span className="mt-0.5 block font-mono text-2xs text-faint">
                  {session.message_count} msgs · {formatRelative(session.updated_at)}
                </span>
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

export { IconButton };
