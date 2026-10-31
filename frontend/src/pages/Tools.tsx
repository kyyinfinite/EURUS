/** Tools & Integrations — the canonical tool registry + MCP server status. */

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Search, X } from "lucide-react";
import { api } from "../lib/api";
import type { MCPServerView, ToolView } from "../lib/types";
import { Badge, Card, Empty, LoadingState, PageHeader, StatusDot } from "../components/ui";

type ToolFilter = "all" | "mutating" | "approval" | "parallel";

export default function Tools() {
  const [filter, setFilter] = useState("");
  const [capability, setCapability] = useState<ToolFilter>("all");
  const toolsQuery = useQuery({
    queryKey: ["tools"],
    queryFn: () => api.get<ToolView[]>("/api/tools"),
  });
  const mcpQuery = useQuery({
    queryKey: ["mcp"],
    queryFn: () => api.get<{ servers: MCPServerView[] }>("/api/mcp"),
    refetchInterval: 8000,
  });

  const tools = toolsQuery.data ?? [];
  const mutating = tools.filter((tool) => tool.mutates).length;

  const filtered = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return tools.filter((tool) => {
      if (capability === "mutating" && !tool.mutates) return false;
      if (capability === "approval" && !tool.requires_approval) return false;
      if (capability === "parallel" && !tool.parallel_safe) return false;
      if (!needle) return true;
      return (
        tool.name.toLowerCase().includes(needle) || tool.description.toLowerCase().includes(needle)
      );
    });
  }, [tools, filter, capability]);

  const FILTERS: { key: ToolFilter; label: string }[] = [
    { key: "all", label: `All ${tools.length}` },
    { key: "mutating", label: `Mutating ${mutating}` },
    { key: "approval", label: `Gated ${tools.filter((t) => t.requires_approval).length}` },
    { key: "parallel", label: "Parallel-safe" },
  ];

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Tools & Integrations"
        subtitle={`${tools.length} tools in the canonical registry · ${mutating} mutating · ${tools.filter((t) => t.requires_approval).length} approval-gated`}
      />
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4 pb-28 sm:p-5 md:pb-12">
        <Card
          title="Tool registry"
          actions={
            <div className="flex flex-wrap items-center gap-1.5">
              {FILTERS.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  onClick={() => setCapability(item.key)}
                  aria-pressed={capability === item.key}
                  className={`whitespace-nowrap rounded-full border px-2.5 py-1 text-2xs transition-colors ${
                    capability === item.key
                      ? "border-copper/50 bg-copper-soft text-copper"
                      : "border-line bg-raised-soft text-dim hover:text-ink"
                  }`}
                >
                  {item.label}
                </button>
              ))}
              <span className="relative ml-1">
                <Search size={12} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" />
                <input
                  value={filter}
                  onChange={(event) => setFilter(event.target.value)}
                  placeholder="Filter tools…"
                  aria-label="Filter tools"
                  className="w-40 py-1 pl-7 pr-6 text-2xs sm:w-48"
                />
                {filter && (
                  <button
                    type="button"
                    aria-label="Clear filter"
                    onClick={() => setFilter("")}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-faint hover:text-ink"
                  >
                    <X size={11} />
                  </button>
                )}
              </span>
            </div>
          }
          dense
        >
          {toolsQuery.isLoading ? (
            <div className="p-3.5">
              <LoadingState label="Loading tools…" />
            </div>
          ) : filtered.length === 0 ? (
            <div className="p-3.5">
              <Empty>No tools match the filter.</Empty>
            </div>
          ) : (
            <ul className="divide-y divide-line-soft">
              {filtered.map((tool) => (
                <li
                  key={tool.name}
                  className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1.5 px-3.5 py-2.5"
                >
                  <div className="min-w-0 flex-1">
                    <div className="font-mono text-xs text-ink">{tool.name}</div>
                    <p className="mt-0.5 text-2xs leading-relaxed text-dim">{tool.description}</p>
                  </div>
                  <div className="flex shrink-0 flex-wrap gap-1">
                    {tool.mutates && <Badge tone="warn">mutates</Badge>}
                    {tool.requires_approval && <Badge tone="warn">approval</Badge>}
                    {!tool.parallel_safe && <Badge tone="dim">serial</Badge>}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="MCP servers" dense>
          {!mcpQuery.data || mcpQuery.data.servers.length === 0 ? (
            <div className="p-3.5">
              <Empty>
                No MCP servers configured. Add them to{" "}
                <code className="font-mono text-copper">~/.eurus/mcp.json</code>.
              </Empty>
            </div>
          ) : (
            <ul className="divide-y divide-line-soft">
              {mcpQuery.data.servers.map((server) => (
                <li
                  key={server.name}
                  className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 px-3.5 py-2.5"
                >
                  <div className="flex min-w-0 items-center gap-2.5">
                    <StatusDot tone={server.connected ? "ok" : server.enabled ? "warn" : "dim"} />
                    <div className="min-w-0">
                      <div className="font-mono text-xs text-ink">{server.name}</div>
                      <div className="truncate text-2xs text-faint">
                        {server.transport} · {server.tools} tools{" "}
                        {server.error ? `· ${server.error}` : ""}
                      </div>
                    </div>
                  </div>
                  <Badge tone={server.connected ? "ok" : server.enabled ? "warn" : "dim"}>
                    {server.state}
                  </Badge>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  );
}
