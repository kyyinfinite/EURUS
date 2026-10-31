/** Usage & Analytics — real run analytics from the canonical run store.
 *  Run volume per day, outcome distribution, origin split and durations are
 *  computed client-side from /api/runs (durable records — no sampling, no
 *  fabrication). Token-level aggregation needs a backend usage endpoint;
 *  that gap is documented on the page.
 */

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { BarChart3, ArrowRight, Info } from "lucide-react";
import { api } from "../lib/api";
import type { RunView } from "../lib/types";
import { Card, EmptyState, ErrorState, LoadingState, PageHeader, StatusIndicator } from "../components/ui";
import { BarSeries, DonutMeter } from "../components/charts/Charts";

type Span = "7d" | "30d" | "all";

export default function Analytics() {
  const [span, setSpan] = useState<Span>("30d");

  const runsQuery = useQuery({
    queryKey: ["runs", "analytics", 1000],
    queryFn: () => api.get<RunView[]>("/api/runs?limit=1000"),
    refetchInterval: 30_000,
  });

  const runs = useMemo(() => {
    const all = runsQuery.data ?? [];
    if (span === "all") return all;
    const cutoff = Date.now() - (span === "7d" ? 7 : 30) * 24 * 3600_000;
    return all.filter((run) => new Date(run.created_at).getTime() >= cutoff);
  }, [runsQuery.data, span]);

  const stats = useMemo(() => {
    const byDay = new Map<string, number>();
    for (const run of runs) {
      const key = new Date(run.created_at).toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
      });
      byDay.set(key, (byDay.get(key) ?? 0) + 1);
    }
    const completed = runs.filter((run) => run.status === "completed");
    const failed = runs.filter((run) => run.status === "failed");
    const cancelled = runs.filter((run) => run.status === "cancelled");
    const active = runs.filter((run) =>
      ["running", "queued", "verifying", "waiting_approval", "waiting_input", "retry_wait", "interrupted"].includes(
        run.status,
      ),
    );
    const web = runs.filter((run) => run.origin === "web").length;
    const external = runs.length - web;
    const durations = completed
      .map((run) => {
        if (!run.finished_at) return null;
        const ms = new Date(run.finished_at).getTime() - new Date(run.created_at).getTime();
        return Number.isFinite(ms) && ms >= 0 ? ms : null;
      })
      .filter((ms): ms is number => ms !== null)
      .sort((a, b) => a - b);
    const avgMs = durations.length > 0 ? durations.reduce((a, b) => a + b, 0) / durations.length : null;
    const medianMs = durations.length > 0 ? durations[Math.floor(durations.length / 2)] : null;
    const finished = completed.length + failed.length;
    return {
      series: [...byDay.entries()].map(([label, value]) => ({ label, value })),
      completed: completed.length,
      failed: failed.length,
      cancelled: cancelled.length,
      active: active.length,
      web,
      external,
      successRate: finished > 0 ? completed.length / finished : null,
      avgMs,
      medianMs,
    };
  }, [runs]);

  return (
    <div className="h-full overflow-y-auto">
      <PageHeader
        title="Usage"
        icon={<BarChart3 size={20} />}
        subtitle="Run volume and outcomes from the canonical run store"
        actions={
          <div className="flex items-center gap-1 rounded-full border border-line bg-raised-soft p-0.5">
            {(["7d", "30d", "all"] as Span[]).map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => setSpan(option)}
                className={`rounded-full px-3 py-1.5 text-xs font-semibold transition-colors ${
                  span === option ? "bg-surface text-ink shadow-sm" : "text-muted hover:text-ink"
                }`}
              >
                {option === "all" ? "All time" : option}
              </button>
            ))}
          </div>
        }
      />
      <div className="space-y-5 px-4 pb-28 sm:px-6 md:pb-12 lg:px-8">
        {runsQuery.isLoading ? (
          <LoadingState label="Loading run history…" />
        ) : runsQuery.isError ? (
          <ErrorState detail="Run history unavailable" hint="The gateway could not list runs." />
        ) : runs.length === 0 ? (
          <EmptyState
            icon={<BarChart3 size={24} />}
            title="No runs in this period"
            description="Analytics appear as soon as the agent executes runs. Start a conversation to see it working."
          />
        ) : (
          <>
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <StatTile label="Total runs" value={String(runs.length)} />
              <StatTile
                label="Success rate"
                value={stats.successRate === null ? "—" : `${Math.round(stats.successRate * 100)}%`}
                tone={stats.successRate !== null && stats.successRate >= 0.8 ? "ok" : stats.successRate !== null && stats.successRate >= 0.5 ? "warn" : "ink"}
              />
              <StatTile
                label="Median duration"
                value={stats.medianMs === null ? "—" : formatDuration(stats.medianMs)}
              />
              <StatTile
                label="Active right now"
                value={String(stats.active)}
                tone={stats.active > 0 ? "copper" : "ink"}
              />
            </div>

            <Card title="Run volume" icon={<BarChart3 size={16} className="text-copper" />}>
              <BarSeries data={stats.series} />
            </Card>

            <div className="grid gap-5 lg:grid-cols-2">
              <Card title="Outcomes" icon={<BarChart3 size={16} className="text-copper" />}>
                <div className="flex flex-col items-center gap-6 sm:flex-row">
                  <DonutMeter
                    slices={[
                      { label: "Completed", value: stats.completed, color: "#278866" },
                      { label: "Failed", value: stats.failed, color: "#B3261E" },
                      { label: "Cancelled", value: stats.cancelled, color: "#EDE7DF" },
                    ]}
                    centerValue={`${stats.completed + stats.failed + stats.cancelled}`}
                    centerLabel="finished runs"
                    centerTone="copper"
                  />
                  <ul className="flex-1 space-y-2.5">
                    <OutcomeRow label="Completed" value={stats.completed} tone="ok" />
                    <OutcomeRow label="Failed" value={stats.failed} tone="danger" />
                    <OutcomeRow label="Cancelled" value={stats.cancelled} tone="dim" />
                    <OutcomeRow label="In flight / interrupted" value={stats.active} tone="active" />
                  </ul>
                </div>
              </Card>

              <Card title="Origins" icon={<BarChart3 size={16} className="text-copper" />}>
                <ul className="space-y-3">
                  <li className="flex items-center justify-between rounded-xl border border-line bg-canvas/60 px-4 py-3">
                    <div>
                      <p className="text-sm font-semibold text-ink">Web workspace</p>
                      <p className="text-xs text-muted">runs started from this gateway</p>
                    </div>
                    <span className="text-xl font-bold text-ink">{stats.web}</span>
                  </li>
                  <li className="flex items-center justify-between rounded-xl border border-line bg-canvas/60 px-4 py-3">
                    <div>
                      <p className="text-sm font-semibold text-ink">External interfaces</p>
                      <p className="text-xs text-muted">TUI, CLI, Telegram and scheduled triggers</p>
                    </div>
                    <span className="text-xl font-bold text-ink">{stats.external}</span>
                  </li>
                  <li className="flex items-center justify-between rounded-xl border border-line bg-canvas/60 px-4 py-3">
                    <div>
                      <p className="text-sm font-semibold text-ink">Average completed duration</p>
                      <p className="text-xs text-muted">
                        {stats.avgMs === null ? "—" : formatDuration(stats.avgMs)}
                      </p>
                    </div>
                    <Link
                      to="/runtime"
                      className="flex items-center gap-1 text-[13px] font-medium text-copper hover:underline"
                    >
                      Runtime <ArrowRight size={13} />
                    </Link>
                  </li>
                </ul>
                <p className="mt-4 flex items-start gap-2 rounded-lg bg-info-soft px-3.5 py-2.5 text-xs leading-relaxed text-info">
                  <Info size={14} className="mt-0.5 shrink-0" />
                  Token-level usage aggregation needs a backend endpoint (per-run model_call usage
                  rollup). The agent events stream token counts live, but they are not persisted in
                  a queryable form yet.
                </p>
              </Card>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function StatTile({
  label,
  value,
  tone = "ink",
}: {
  label: string;
  value: string;
  tone?: "ink" | "ok" | "warn" | "copper";
}) {
  const color =
    tone === "ok" ? "text-ok" : tone === "warn" ? "text-warn" : tone === "copper" ? "text-copper-dark" : "text-ink";
  return (
    <div className="rounded-xl border border-line bg-surface p-5 shadow-card">
      <p className={`text-[28px] font-bold leading-none ${color}`}>{value}</p>
      <p className="mt-2 text-[13px] font-medium text-muted">{label}</p>
    </div>
  );
}

function OutcomeRow({ label, value, tone }: { label: string; value: number; tone: "ok" | "danger" | "dim" | "active" }) {
  return (
    <li className="flex items-center justify-between gap-3 rounded-lg border border-line-soft bg-canvas/60 px-3.5 py-2.5">
      <StatusIndicator tone={tone} label={label} />
      <span className="font-mono text-sm font-semibold text-ink">{value}</span>
    </li>
  );
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
  return `${Math.floor(ms / 3_600_000)}h ${Math.floor((ms % 3_600_000) / 60_000)}m`;
}
