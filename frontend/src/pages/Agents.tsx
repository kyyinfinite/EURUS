/** Agents — the canonical agent profile: model chain, budgets, toolset and
 *  provider capabilities, all read from live config + registry APIs.
 *  Editing lives in Settings (the config PATCH endpoint); this page is the
 *  honest operational picture.
 */

import { useQuery } from "@tanstack/react-query";
import { Bot, Wrench, KeyRound, ListChecks, ArrowRight } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import type { ConfigView, ProviderView, ToolView } from "../lib/types";
import { Badge, Card, EmptyState, ErrorState, KeyValue, LoadingState, PageHeader, StatusDot } from "../components/ui";

export default function Agents() {
  const configQuery = useQuery({
    queryKey: ["config"],
    queryFn: () => api.get<ConfigView>("/api/config"),
  });
  const providersQuery = useQuery({
    queryKey: ["providers"],
    queryFn: () => api.get<ProviderView[]>("/api/providers"),
    staleTime: 30_000,
  });
  const toolsQuery = useQuery({
    queryKey: ["tools"],
    queryFn: () => api.get<ToolView[]>("/api/tools"),
    staleTime: 60_000,
  });

  if (configQuery.isLoading) {
    return (
      <div className="h-full overflow-y-auto">
        <PageHeader title="Agents" icon={<Bot size={20} />} />
        <LoadingState label="Loading agent profile…" />
      </div>
    );
  }
  if (configQuery.isError || !configQuery.data) {
    return (
      <div className="h-full overflow-y-auto">
        <PageHeader title="Agents" icon={<Bot size={20} />} />
        <div className="px-8">
          <ErrorState detail="Agent configuration unavailable" hint="Check that the gateway is running." />
        </div>
      </div>
    );
  }

  const config = configQuery.data.config as Record<string, Record<string, unknown>>;
  const agent = (config.agent ?? {}) as Record<string, unknown>;
  const providers = providersQuery.data ?? [];
  const readyProviders = providers.filter((p) => p.status === "ready");
  const tools = toolsQuery.data ?? [];
  const mutating = tools.filter((tool) => tool.mutates).length;
  const approvalGated = tools.filter((tool) => tool.requires_approval).length;

  const modelChain = [
    agent.model as string | undefined,
    ...((agent.fallback as (string | undefined)[] | undefined) ?? []),
  ].filter(Boolean) as string[];

  return (
    <div className="h-full overflow-y-auto">
      <PageHeader
        title="Agents"
        icon={<Bot size={20} />}
        subtitle="The canonical agent runtime this gateway drives — one profile, every interface"
        actions={
          <Link
            to="/config"
            className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-line bg-surface px-3.5 text-sm font-medium text-ink transition-colors hover:border-copper/50 hover:text-copper-dark"
          >
            Edit in Settings <ArrowRight size={14} />
          </Link>
        }
      />
      <div className="space-y-5 px-4 pb-28 sm:px-6 md:pb-12 lg:px-8">
        <div className="grid gap-5 lg:grid-cols-2">
          <Card title="Agent profile" icon={<Bot size={16} className="text-copper" />}>
            <KeyValue
              rows={[
                [
                  "Primary model",
                  <span key="m" className="font-mono text-[13px]">
                    {String(agent.model ?? "not configured")}
                  </span>,
                ],
                [
                  "Fallback chain",
                  modelChain.length > 1 ? (
                    <span key="f" className="flex flex-wrap items-center gap-1.5">
                      {modelChain.map((model, index) => (
                        <span key={index} className="flex items-center gap-1.5">
                          {index > 0 && <span className="text-faint">→</span>}
                          <code className="rounded-md bg-raised-soft px-1.5 py-0.5 font-mono text-2xs text-dim">
                            {model}
                          </code>
                        </span>
                      ))}
                    </span>
                  ) : (
                    <span key="f" className="text-muted">
                      none
                    </span>
                  ),
                ],
                ["Iteration budget", String(agent.iteration_budget ?? "—")],
                ["Config path", <span key="c" className="font-mono text-2xs">{configQuery.data.config_path}</span>],
                ["Revision", String(configQuery.data.revision)],
              ]}
            />
          </Card>

          <Card title="Model chain readiness" icon={<KeyRound size={16} className="text-copper" />}>
            {readyProviders.length === 0 ? (
              <EmptyState
                icon={<KeyRound size={22} />}
                title="No ready providers"
                description="Add a provider API key in your environment for the agent's model chain to become executable."
              />
            ) : (
              <ul className="space-y-2.5">
                {readyProviders.map((provider) => (
                  <li
                    key={provider.name}
                    className="flex flex-wrap items-center gap-2 rounded-xl border border-line bg-canvas/60 px-3.5 py-3"
                  >
                    <StatusDot tone="ok" />
                    <span className="font-mono text-[13px] font-semibold text-ink">{provider.name}</span>
                    <Badge tone="dim">{provider.protocol}</Badge>
                    {provider.multimodal ? (
                      <Badge tone="info">multimodal</Badge>
                    ) : (
                      <Badge tone="dim">text-only</Badge>
                    )}
                    <span className="ml-auto flex flex-wrap gap-1.5">
                        <Badge tone={provider.capabilities?.toolCalling ? "ok" : "dim"}>tools</Badge>
                        <Badge tone={provider.capabilities?.streaming ? "ok" : "dim"}>streaming</Badge>
                    </span>
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-4 rounded-lg bg-raised-soft/60 px-3 py-2 text-xs leading-relaxed text-muted">
              Capability metadata comes from the provider adapters via{" "}
              <code className="font-mono text-2xs text-copper-dark">/api/providers</code>. Today
              every supported protocol is text-only for model input — image inputs are a separate
              backend integration phase.
            </p>
          </Card>
        </div>

        <Card
          title="Toolset"
          icon={<Wrench size={16} className="text-copper" />}
          actions={
            <Link to="/tools" className="flex items-center gap-1 text-[13px] font-medium text-copper hover:underline">
              Browse tools <ArrowRight size={13} />
            </Link>
          }
        >
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <ToolStat icon={<Wrench size={15} />} label="Available tools" value={tools.length} />
            <ToolStat icon={<ListChecks size={15} />} label="Approval-gated" value={approvalGated} tone="warn" />
            <ToolStat icon={<ListChecks size={15} />} label="Mutating" value={mutating} tone="warn" />
            <ToolStat
              icon={<Bot size={15} />}
              label="Parallel-safe"
              value={tools.filter((tool) => tool.parallel_safe).length}
              tone="ok"
            />
          </div>
        </Card>

        <Card title="Runtime posture" icon={<ListChecks size={16} className="text-copper" />}>
          <p className="text-sm leading-relaxed text-muted">
            The gateway drives the canonical{" "}
            <code className="rounded bg-raised-soft px-1.5 py-0.5 font-mono text-2xs text-copper-dark">
              AgentEngine
            </code>
            : durable runs, tool boundaries with approval gates, checkpoints and verification.
            Every interface — TUI, CLI and this workspace — shares the same session store and run
            history.{" "}
            <Link to="/security" className="text-copper hover:underline">
              See Access &amp; Security →
            </Link>
          </p>
        </Card>
      </div>
    </div>
  );
}

function ToolStat({
  icon,
  label,
  value,
  tone = "ink",
}: {
  icon: React.ReactNode;
  label: string;
  value: number;
  tone?: "ink" | "ok" | "warn";
}) {
  return (
    <div className="rounded-xl border border-line bg-canvas/60 px-4 py-3.5">
      <div className="flex items-center gap-2 text-muted">
        <span className={tone === "ok" ? "text-ok" : tone === "warn" ? "text-warn" : "text-copper"}>{icon}</span>
        <span className="text-xs font-medium">{label}</span>
      </div>
      <p className={`mt-1.5 text-2xl font-bold ${tone === "ok" ? "text-ok" : tone === "warn" ? "text-warn" : "text-ink"}`}>
        {value}
      </p>
    </div>
  );
}
