/** Memory — canonical memory store via the existing SessionStore interfaces. */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Brain, Plus, Search, Trash2, X } from "lucide-react";
import { api } from "../lib/api";
import type { MemoryView } from "../lib/types";
import { useUI } from "../store/ui";
import { formatTime } from "../lib/format";
import { Badge, Button, Card, Empty, ErrorNote, LoadingState, PageHeader, Spinner } from "../components/ui";

const TRUST_LEVELS = ["user_approved", "derived", "untrusted"] as const;

export default function Memory() {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [draft, setDraft] = useState("");
  const [formOpen, setFormOpen] = useState(false);
  const pushToast = useUI((state) => state.pushToast);

  const query = useQuery({
    queryKey: ["memory", search],
    queryFn: () =>
      api.get<MemoryView[]>(`/api/memory?query=${encodeURIComponent(search)}&limit=100`),
  });

  const add = useMutation({
    mutationFn: () =>
      api.post("/api/memory", { content: draft, memory_type: "fact", trust: "user_approved" }),
    onSuccess: () => {
      setDraft("");
      setFormOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["memory"] });
      pushToast({ tone: "ok", title: "Memory added", detail: "Trusted (user approved) and searchable." });
    },
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.delete(`/api/memory/${id}`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["memory"] }),
  });

  const setTrust = useMutation({
    mutationFn: (input: { id: number; trust: string }) =>
      api.post(`/api/memory/${input.id}/trust?trust=${input.trust}`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["memory"] }),
  });

  const memories = query.data ?? [];

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Memory"
        subtitle="Trust-tiered long-term memory injected into prompts with a budget"
        actions={
          <Button variant="primary" onClick={() => setFormOpen((open) => !open)}>
            <Plus size={13} /> {formOpen ? "Close" : "Add memory"}
          </Button>
        }
      />
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4 pb-28 sm:p-5 md:pb-12">
        {formOpen && (
          <Card title="New memory">
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              rows={3}
              placeholder="A durable fact, preference or instruction EURUS should remember…"
              aria-label="Memory content"
              className="w-full text-xs"
            />
            {add.isError && (
              <div className="mt-2">
                <ErrorNote detail={add.error instanceof Error ? add.error.message : "add failed"} />
              </div>
            )}
            <div className="mt-3 flex items-center justify-between gap-2">
              <p className="text-2xs text-faint">Stored as a user-approved fact in the canonical store.</p>
              <Button
                variant="primary"
                size="sm"
                onClick={() => add.mutate()}
                disabled={add.isPending || !draft.trim()}
              >
                {add.isPending ? <Spinner /> : <Brain size={13} />} Remember
              </Button>
            </div>
          </Card>
        )}

        <div className="mx-auto max-w-3xl">
          <div className="relative">
            <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint" />
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search memory (FTS5 full-text)…"
              aria-label="Search memory"
              className="w-full py-2 pl-9 pr-8 text-xs"
            />
            {search && (
              <button
                type="button"
                aria-label="Clear search"
                onClick={() => setSearch("")}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-faint hover:text-ink"
              >
                <X size={13} />
              </button>
            )}
          </div>

          {query.isLoading ? (
            <LoadingState label="Loading memories…" />
          ) : memories.length === 0 ? (
            <div className="mt-4">
              <Empty>
                {search ? "No memories match the search." : "No memories yet — add facts EURUS should retain."}
              </Empty>
            </div>
          ) : (
            <div className="mt-4 space-y-2">
              {memories.map((memory) => (
                <div
                  key={memory.id}
                  className="rounded-xl border border-line bg-surface px-3.5 py-3 transition-colors hover:border-line"
                >
                  <div className="flex items-start justify-between gap-3">
                    <p className="min-w-0 flex-1 break-words text-xs leading-relaxed text-ink">
                      {memory.content}
                    </p>
                    <div className="flex shrink-0 items-center gap-1.5">
                      <select
                        value={memory.trust}
                        onChange={(event) =>
                          setTrust.mutate({ id: memory.id, trust: event.target.value })
                        }
                        aria-label={`Trust level for memory ${memory.id}`}
                        className="py-1 text-2xs"
                      >
                        {TRUST_LEVELS.map((level) => (
                          <option key={level} value={level}>
                            {level}
                          </option>
                        ))}
                      </select>
                      <button
                        type="button"
                        onClick={() => remove.mutate(memory.id)}
                        aria-label={`Delete memory ${memory.id}`}
                        className="flex h-7 w-7 items-center justify-center rounded-lg text-faint transition-colors hover:text-danger"
                      >
                        <Trash2 size={12} />
                      </button>
                    </div>
                  </div>
                  <div className="mt-2 flex flex-wrap items-center gap-1.5">
                    <Badge tone={memory.trust === "user_approved" ? "ok" : memory.trust === "derived" ? "dim" : "danger"}>
                      {memory.trust}
                    </Badge>
                    <Badge tone="dim">{memory.memory_type}</Badge>
                    {memory.category && memory.category !== "general" && (
                      <Badge tone="dim">{memory.category}</Badge>
                    )}
                    {memory.tags && <Badge tone="dim">{memory.tags}</Badge>}
                    <span className="ml-auto font-mono text-2xs text-faint">
                      used {memory.use_count}× · {formatTime(memory.created_at)}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
