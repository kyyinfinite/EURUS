/** Command palette (⌘K / Ctrl+K) — one global search across pages, sessions,
 *  tools and providers. Data comes from the live gateway APIs (react-query
 *  caches); nothing is hardcoded. */

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  Boxes,
  Container,
  CornerDownLeft,
  KeyRound,
  MessagesSquare,
  Search,
  Sparkles,
  Wrench,
} from "lucide-react";
import { api } from "../../lib/api";
import type { ProviderView, SessionView, ToolView } from "../../lib/types";
import { useUI } from "../../store/ui";
import { formatRelative } from "../../lib/format";
import { NAV_FLAT } from "./nav";

interface PaletteItem {
  id: string;
  group: string;
  label: string;
  hint?: string;
  icon: React.ReactNode;
  keywords: string;
  action: () => void;
}

export function CommandPalette() {
  const open = useUI((state) => state.commandPaletteOpen);
  const setOpen = useUI((state) => state.setCommandPaletteOpen);
  const openFloating = useUI((state) => state.openFloatingChat);
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  const sessionsQuery = useQuery({
    queryKey: ["sessions"],
    queryFn: () => api.get<SessionView[]>("/api/sessions"),
    enabled: open,
  });
  const toolsQuery = useQuery({
    queryKey: ["tools"],
    queryFn: () => api.get<ToolView[]>("/api/tools"),
    enabled: open,
    staleTime: 60_000,
  });
  const providersQuery = useQuery({
    queryKey: ["providers"],
    queryFn: () => api.get<ProviderView[]>("/api/providers"),
    enabled: open,
    staleTime: 30_000,
  });

  useEffect(() => {
    if (open) {
      setQuery("");
      setActive(0);
      window.setTimeout(() => inputRef.current?.focus(), 30);
    }
  }, [open]);

  const close = () => setOpen(false);

  const items = useMemo<PaletteItem[]>(() => {
    const pages: PaletteItem[] = NAV_FLAT.map((item) => ({
      id: `page-${item.to}`,
      group: "Navigate",
      label: item.label,
      hint: item.description,
      icon: <item.icon size={16} />,
      keywords: `${item.label} ${item.description} page navigation`.toLowerCase(),
      action: () => {
        close();
        navigate(item.to);
      },
    }));
    const actions: PaletteItem[] = [
      {
        id: "action-new-task",
        group: "Actions",
        label: "Start a new task",
        hint: "Prompt-first welcome screen",
        icon: <Sparkles size={16} />,
        keywords: "new task prompt ask start chat message".toLowerCase(),
        action: () => {
          close();
          navigate("/home");
        },
      },
      {
        id: "action-floating",
        group: "Actions",
        label: "Ask EURUS assistant",
        hint: "Open the floating chat",
        icon: <MessagesSquare size={16} />,
        keywords: "ask assistant floating chat quick".toLowerCase(),
        action: () => {
          close();
          openFloating();
        },
      },
    ];
    const sessions: PaletteItem[] = (sessionsQuery.data ?? []).slice(0, 12).map((session) => ({
      id: `session-${session.id}`,
      group: "Conversations",
      label: session.title || "Untitled",
      hint: `${session.message_count} messages · ${formatRelative(session.updated_at)}`,
      icon: <Container size={16} />,
      keywords: `${session.title} ${session.id} session conversation chat`.toLowerCase(),
      action: () => {
        close();
        navigate(`/chat/${session.id}`);
      },
    }));
    const tools: PaletteItem[] = (toolsQuery.data ?? []).slice(0, 10).map((tool) => ({
      id: `tool-${tool.name}`,
      group: "Tools",
      label: tool.name,
      hint: tool.description,
      icon: <Wrench size={16} />,
      keywords: `${tool.name} ${tool.description} tool`.toLowerCase(),
      action: () => {
        close();
        navigate("/tools");
      },
    }));
    const providers: PaletteItem[] = (providersQuery.data ?? []).slice(0, 8).map((provider) => ({
      id: `provider-${provider.name}`,
      group: "Providers",
      label: provider.name,
      hint: `${provider.protocol} · ${provider.status}`,
      icon: <KeyRound size={16} />,
      keywords: `${provider.name} ${provider.protocol} provider model`.toLowerCase(),
      action: () => {
        close();
        navigate("/providers");
      },
    }));
    return [...actions, ...pages, ...sessions, ...tools, ...providers];
  }, [sessionsQuery.data, toolsQuery.data, providersQuery.data, navigate, openFloating]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return items.slice(0, 24);
    return items
      .filter((item) => item.keywords.includes(needle) || item.label.toLowerCase().includes(needle))
      .slice(0, 24);
  }, [items, query]);

  useEffect(() => {
    setActive(0);
  }, [query]);

  useEffect(() => {
    const node = listRef.current?.querySelector<HTMLElement>(`[data-index="${active}"]`);
    node?.scrollIntoView({ block: "nearest" });
  }, [active]);

  if (!open) return null;

  const run = (item: PaletteItem | undefined) => {
    if (item) item.action();
  };

  let lastGroup = "";

  return createPortal(
    <div className="fixed inset-0 z-[100] flex items-start justify-center px-4 pt-[12vh]">
      <div className="absolute inset-0 animate-fade-in bg-ink/45" onClick={close} aria-hidden="true" />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Global search"
        className="relative flex max-h-[64vh] w-full max-w-xl animate-scale-in flex-col overflow-hidden rounded-2xl border border-line bg-surface shadow-overlay"
      >
        <div className="flex items-center gap-3 border-b border-line-soft px-4">
          <Search size={17} className="shrink-0 text-faint" />
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown") {
                event.preventDefault();
                setActive((value) => Math.min(value + 1, filtered.length - 1));
              } else if (event.key === "ArrowUp") {
                event.preventDefault();
                setActive((value) => Math.max(value - 1, 0));
              } else if (event.key === "Enter") {
                event.preventDefault();
                run(filtered[active]);
              } else if (event.key === "Escape") {
                event.preventDefault();
                close();
              }
            }}
            placeholder="Search sessions, tools, providers, pages…"
            aria-label="Search everything"
            className="!w-full !rounded-none !border-0 !bg-transparent !px-0 !py-4 !text-[15px] !ring-0 focus:!border-0"
          />
          <kbd className="shrink-0 rounded-md border border-line bg-canvas px-1.5 py-0.5 font-mono text-[11px] text-muted">
            esc
          </kbd>
        </div>
        <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto p-2" role="listbox">
          {filtered.length === 0 ? (
            <div className="flex flex-col items-center gap-1.5 px-4 py-10 text-center">
              <Boxes size={20} className="text-faint" />
              <p className="text-sm text-muted">
                No matches for “{query}”. Try a session title or a tool name.
              </p>
            </div>
          ) : (
            filtered.map((item, index) => {
              const showGroup = item.group !== lastGroup;
              lastGroup = item.group;
              return (
                <div key={item.id}>
                  {showGroup && (
                    <p className="px-3 pb-1 pt-3 text-[11px] font-semibold uppercase tracking-[0.14em] text-faint first:pt-1">
                      {item.group}
                    </p>
                  )}
                  <button
                    type="button"
                    role="option"
                    aria-selected={index === active}
                    data-index={index}
                    onMouseEnter={() => setActive(index)}
                    onClick={() => run(item)}
                    className={`flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-left transition-colors ${
                      index === active ? "bg-copper-soft" : "hover:bg-raised-soft"
                    }`}
                  >
                    <span
                      className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border ${
                        index === active
                          ? "border-copper/25 bg-surface text-copper"
                          : "border-line bg-canvas text-muted"
                      }`}
                    >
                      {item.icon}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium text-ink">{item.label}</span>
                      {item.hint && <span className="block truncate text-xs text-muted">{item.hint}</span>}
                    </span>
                    {index === active && (
                      <CornerDownLeft size={14} className="shrink-0 text-copper" />
                    )}
                  </button>
                </div>
              );
            })
          )}
        </div>
        <footer className="flex items-center gap-4 border-t border-line-soft bg-canvas/70 px-4 py-2.5 text-[11px] text-faint">
          <span className="flex items-center gap-1">
            <kbd className="rounded border border-line bg-surface px-1 font-mono">↑↓</kbd> navigate
          </span>
          <span className="flex items-center gap-1">
            <kbd className="rounded border border-line bg-surface px-1 font-mono">↵</kbd> open
          </span>
          <span className="ml-auto flex items-center gap-1">
            <ArrowRight size={11} /> {filtered.length} result{filtered.length === 1 ? "" : "s"}
          </span>
        </footer>
      </div>
    </div>,
    document.body,
  );
}
