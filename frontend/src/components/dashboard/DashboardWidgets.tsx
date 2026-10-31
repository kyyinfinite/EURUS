/** Dashboard widgets — the reference dashboard recreated with real
 *  backend data only (brief §5). Where the reference shows a statistic the
 *  gateway cannot know (e.g. +12% trend deltas), the honest equivalent is
 *  rendered from live data instead. */

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import {
  ArrowRight,
  CircleCheck,
  Container,
  Cpu,
  Database,
  KeyRound,
  ShieldAlert,
  Sparkles,
  Zap,
} from "lucide-react";
import { api } from "../../lib/api";
import type { DashboardView, RunView } from "../../lib/types";
import { formatRelative, runStatusMeta } from "../../lib/format";
import { useGateway } from "../../store/gateway";
import { Badge, Card, MetricCard, StatusIndicator } from "../ui";
import { ActivityArea, DonutMeter } from "../charts/Charts";

/* ---- hero banner ------------------------------------------------------------- */

export function HeroBanner() {
  return (
    <section className="relative overflow-hidden rounded-2xl border border-line bg-gradient-to-r from-copper-tint via-surface to-[#F6EDE5] px-5 py-9 shadow-card sm:px-10 sm:py-12">
      {/* restrained copper atmosphere (reference: soft warm shapes) */}
      <div
        className="pointer-events-none absolute -right-24 -top-28 h-[340px] w-[340px] rounded-full opacity-70 blur-3xl"
        style={{ background: "radial-gradient(circle, rgba(153,83,41,0.14), transparent 68%)" }}
        aria-hidden="true"
      />
      <div
        className="pointer-events-none absolute -bottom-32 right-40 h-[260px] w-[260px] rounded-full opacity-50 blur-3xl"
        style={{ background: "radial-gradient(circle, rgba(176,112,63,0.12), transparent 70%)" }}
        aria-hidden="true"
      />
      <svg
        className="pointer-events-none absolute right-8 top-1/2 hidden h-44 w-44 -translate-y-1/2 text-copper/25 lg:block"
        viewBox="0 0 200 200"
        fill="none"
        aria-hidden="true"
      >
        <circle cx="100" cy="100" r="92" stroke="currentColor" strokeWidth="1.5" />
        <circle cx="100" cy="100" r="62" stroke="currentColor" strokeWidth="1.5" />
        <circle cx="100" cy="100" r="34" stroke="currentColor" strokeWidth="1.5" />
        <path d="M46 128 L100 66 L154 128" stroke="currentColor" strokeWidth="1.5" />
      </svg>
      <div className="relative max-w-2xl">
        <p className="text-[11px] font-semibold uppercase tracking-[0.22em] text-copper">
          EURUS AI Gateway
        </p>
        <h1 className="mt-3 text-[28px] font-bold leading-tight tracking-tight text-ink sm:text-[34px]">
          Your AI Workspace in One Powerful Platform
        </h1>
        <p className="mt-3 max-w-lg text-[15px] leading-relaxed text-muted">
          Manage agents, tools, and workflows with ease.
        </p>
        <div className="mt-7 flex flex-wrap items-center gap-3">
          <Link
            to="/home"
            className="inline-flex h-11 items-center gap-2 rounded-full bg-copper px-6 text-sm font-semibold text-white shadow-launcher transition-colors hover:bg-copper-dark"
          >
            <Sparkles size={16} /> Start Exploring
          </Link>
        </div>
      </div>
    </section>
  );
}

/* ---- stat cards ----------------------------------------------------------------- */

export function StatCards({ data }: { data: DashboardView }) {
  const runtime = data.runtime;
  const navigate = useNavigate();
  const readyProviders = (data.diagnostics.providers ?? []).filter((p) => p.ready).length;
  const totalProviders = (data.diagnostics.providers ?? []).length;
  return (
    <div className="grid grid-cols-2 items-stretch gap-3 sm:gap-4 xl:grid-cols-4">
      <MetricCard
        icon={<Container size={18} />}
        label="Total Sessions"
        value={runtime.session_count}
        hint="Canonical conversations across every interface"
        onClick={() => navigate("/sessions")}
      />
      <MetricCard
        icon={<Zap size={18} />}
        label="Active Runs"
        value={String(runtime.active_runs.length).padStart(2, "0")}
        hint={runtime.active_runs.length > 0 ? "executing right now" : "no work in flight"}
        tone="copper"
        live={runtime.active_runs.length > 0}
        onClick={() => navigate("/runtime")}
      />
      <MetricCard
        icon={<CircleCheck size={18} />}
        label="Pending Approvals"
        value={String(runtime.pending_approvals).padStart(2, "0")}
        hint={runtime.pending_approvals > 0 ? "decisions waiting" : "nothing awaiting you"}
        tone={runtime.pending_approvals > 0 ? "warn" : "ink"}
        onClick={() => navigate("/approvals")}
      />
      <MetricCard
        icon={<KeyRound size={18} />}
        label="Providers Connected"
        value={String(readyProviders).padStart(2, "0")}
        hint={`of ${totalProviders} configured`}
        tone={readyProviders > 0 ? "ok" : "danger"}
        onClick={() => navigate("/providers")}
      />
    </div>
  );
}

/* ---- system overview chart --------------------------------------------------------- */

type Range = "24h" | "7d";

export function SystemOverview() {
  const [range, setRange] = useState<Range>("24h");
  const runsQuery = useQuery({
    queryKey: ["runs", "chart", 1000],
    queryFn: () => api.get<RunView[]>("/api/runs?limit=1000"),
    refetchInterval: 30_000,
  });

  const series = useMemo(() => {
    const runs = runsQuery.data ?? [];
    const buckets = new Map<number, number>();
    const now = Date.now();
    const spanMs = range === "24h" ? 24 * 3600_000 : 7 * 24 * 3600_000;
    const stepMs = range === "24h" ? 3600_000 : 24 * 3600_000;
    const start = now - spanMs + stepMs;
    for (let t = start; t <= now; t += stepMs) buckets.set(t, 0);
    for (const run of runs) {
      const created = new Date(run.created_at).getTime();
      if (Number.isNaN(created) || created < now - spanMs) continue;
      const key = Math.floor(created / stepMs) * stepMs;
      buckets.set(key, (buckets.get(key) ?? 0) + 1);
    }
    return [...buckets.entries()]
      .sort(([a], [b]) => a - b)
      .map(([time, value]) => ({
        label:
          range === "24h"
            ? new Date(time).toLocaleTimeString(undefined, { hour: "2-digit" })
            : new Date(time).toLocaleDateString(undefined, { weekday: "short" }),
        value,
      }));
  }, [runsQuery.data, range]);

  const total = series.reduce((sum, point) => sum + point.value, 0);

  return (
    <Card
      title={
        <span className="flex items-center gap-2.5">
          System Overview
          <span className="inline-flex items-center gap-1.5 rounded-full bg-ok-soft px-2 py-0.5 text-[11px] font-semibold text-ok">
            <span className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-ok" />
            Live
          </span>
        </span>
      }
      icon={<Cpu size={16} className="text-copper" />}
      actions={
        <div className="flex items-center gap-1 rounded-full border border-line bg-raised-soft p-0.5">
          {(["24h", "7d"] as Range[]).map((option) => (
            <button
              key={option}
              type="button"
              onClick={() => setRange(option)}
              className={`rounded-full px-3 py-1 text-xs font-semibold transition-colors ${
                range === option ? "bg-surface text-ink shadow-sm" : "text-muted hover:text-ink"
              }`}
            >
              {option}
            </button>
          ))}
        </div>
      }
    >
      <div className="mb-3 flex items-center gap-2 text-xs text-muted">
        {total} run{total === 1 ? "" : "s"} in the last {range} · runs per{" "}
        {range === "24h" ? "hour" : "day"} from the canonical run store
      </div>
      <ActivityArea data={series} />
    </Card>
  );
}

/* ---- recent activity ------------------------------------------------------------------ */

export function RecentActivity({ runs }: { runs: RunView[] }) {
  const items = runs.slice(0, 7);
  return (
    <Card
      title="Recent Activity"
      icon={<Sparkles size={16} className="text-copper" />}
      actions={
        <Link to="/runtime" className="flex items-center gap-1 text-[13px] font-medium text-copper hover:underline">
          View all <ArrowRight size={13} />
        </Link>
      }
      dense
    >
      {items.length === 0 ? (
        <div className="p-5">
          <p className="rounded-xl border border-dashed border-line px-4 py-8 text-center text-[13px] text-muted">
            No runs yet — activity appears here the moment the agent starts working.
          </p>
        </div>
      ) : (
        <ul className="divide-y divide-line-soft">
          {items.map((run) => {
            const meta = runStatusMeta(run.status);
            const tone =
              meta.tone === "ok" ? "ok" : meta.tone === "danger" ? "danger" : meta.tone === "warn" ? "warn" : meta.tone === "active" ? "active" : "dim";
            return (
              <li key={run.run_id} className="flex items-start gap-3 px-4 py-3 sm:px-5">
                <span
                  className={`mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-full ${
                    tone === "ok"
                      ? "bg-ok-soft text-ok"
                      : tone === "danger"
                        ? "bg-danger-soft text-danger"
                        : tone === "warn"
                          ? "bg-warn-soft text-warn"
                          : tone === "active"
                            ? "bg-copper-soft text-copper"
                            : "bg-raised-soft text-muted"
                  }`}
                >
                  {tone === "ok" ? (
                    <CircleCheck size={16} />
                  ) : tone === "danger" ? (
                    <ShieldAlert size={16} />
                  ) : tone === "active" ? (
                    <Cpu size={16} />
                  ) : (
                    <Database size={16} />
                  )}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-ink">
                    {run.objective || "Agent run"}
                  </p>
                  <p className="mt-0.5 text-xs text-muted">
                    {run.origin} origin · {run.run_id.slice(0, 8)} · {formatRelative(run.created_at)}
                  </p>
                </div>
                <Badge tone={tone}>{meta.label}</Badge>
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

/* ---- providers overview ------------------------------------------------------------------- */

export function ProviderOverview({ providers }: { providers: { name: string; protocol: string; ready: boolean }[] }) {
  return (
    <Card
      title="Providers"
      icon={<KeyRound size={16} className="text-copper" />}
      actions={
        <Link to="/providers" className="flex items-center gap-1 text-[13px] font-medium text-copper hover:underline">
          View all <ArrowRight size={13} />
        </Link>
      }
    >
      {providers.length === 0 ? (
        <p className="rounded-xl border border-dashed border-line px-4 py-8 text-center text-[13px] text-muted">
          No providers configured yet.
        </p>
      ) : (
        <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 2xl:grid-cols-2">
          {providers.slice(0, 6).map((provider) => (
            <li
              key={provider.name}
              className="flex items-center gap-3 rounded-xl border border-line bg-canvas/60 px-3.5 py-3"
            >
              <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-copper/20 bg-copper-soft text-sm font-bold text-copper-dark">
                {provider.name.slice(0, 2).toUpperCase()}
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-semibold text-ink">{provider.name}</p>
                <p className="truncate text-xs text-muted">{provider.protocol}</p>
              </div>
              <StatusIndicator
                tone={provider.ready ? "ok" : "warn"}
                label={provider.ready ? "Ready" : "Missing key"}
              />
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

/* ---- system health ---------------------------------------------------------------------------- */

export function SystemHealth({ data }: { data: DashboardView }) {
  const connected = useGateway((state) => state.connected);
  const health = data.runtime.runtime_health;
  const recent = data.runtime.recent_runs;
  const completed = recent.filter((run) => run.status === "completed").length;
  const failed = recent.filter((run) => run.status === "failed").length;
  const other = Math.max(0, recent.length - completed - failed);
  const completionRate = completed + failed > 0 ? completed / (completed + failed) : null;

  const engineStarted = health.started !== false;
  const degraded = !connected || !engineStarted || health.closed === true;

  return (
    <Card title="System Health" icon={<ShieldAlert size={16} className="text-copper" />}>
      <div className="flex flex-col items-center gap-5 sm:flex-row sm:items-center">
        <DonutMeter
          size={140}
          slices={[
            { label: "Completed", value: completed, color: "#278866" },
            { label: "Failed", value: failed, color: "#B3261E" },
            { label: "Other", value: other, color: "#EDE7DF" },
          ]}
          centerValue={
            completionRate === null ? "—" : `${Math.round(completionRate * 100)}%`
          }
          centerLabel={completionRate === null ? "no finished runs" : "run success"}
          centerTone={
            completionRate === null ? "copper" : completionRate >= 0.8 ? "ok" : completionRate >= 0.5 ? "warn" : "danger"
          }
        />
        <ul className="w-full flex-1 space-y-2.5">
          <HealthRow label="Gateway connection" ok={connected} detail={connected ? "event stream live" : "reconnecting"} />
          <HealthRow label="Agent engine" ok={engineStarted} detail={engineStarted ? "started" : "idle"} />
          <HealthRow
            label="Session store"
            ok
            detail={`${data.runtime.session_count} sessions · ${formatBytesShort(data.diagnostics.database_size_bytes)}`}
          />
          <HealthRow
            label="Event stream"
            ok={health.closed !== true}
            detail={`${data.runtime.websocket_clients} client${data.runtime.websocket_clients === 1 ? "" : "s"}${
              (health.dropped_events ?? 0) > 0 ? ` · ${health.dropped_events} dropped` : ""
            }`}
          />
        </ul>
      </div>
      <p className={`mt-4 rounded-lg px-3 py-2 text-xs font-medium ${degraded ? "bg-warn-soft text-warn" : "bg-ok-soft text-ok"}`}>
        {degraded
          ? "Degraded — one or more components need attention."
          : "All gateway components operational."}
      </p>
    </Card>
  );
}

function HealthRow({ label, ok, detail }: { label: string; ok: boolean; detail: string }) {
  return (
    <li className="flex items-center gap-2.5 rounded-lg border border-line-soft bg-canvas/60 px-3 py-2">
      <span className={`h-2 w-2 shrink-0 rounded-full ${ok ? "bg-ok" : "bg-warn"}`} />
      <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-ink">{label}</span>
      <span className="shrink-0 truncate text-xs text-muted">{detail}</span>
    </li>
  );
}

function formatBytesShort(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
