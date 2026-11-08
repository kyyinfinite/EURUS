
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { BarChart3, ArrowRight, BrainCircuit, Info } from "lucide-react";
import { api } from "../lib/api";
import type { MissionAnalyticsView, RunView } from "../lib/types";
import { PALETTE } from "../lib/palette";
import { Card, EmptyState, ErrorState, LoadingState, PageHeader, SegmentedControl, StatusIndicator } from "../components/ui";
import { BarSeries, DonutMeter } from "../components/charts/Charts";

type Span = "7d" | "30d" | "all";
const SPANS: readonly Span[] = ["7d", "30d", "all"] as const;

export default function Analytics() {
  const [span, setSpan] = useState<Span>("30d");

  const runsQuery = useQuery({
    queryKey: ["runs", "analytics", 1000],
    queryFn: () => api.get<RunView[]>("/api/runs?limit=1000"),
    refetchInterval: 30_000,
  });

  const missionQuery = useQuery({
    queryKey: ["missions", "analytics"],
    queryFn: () => api.get<MissionAnalyticsView>("/api/missions/analytics"),
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
    const mid = Math.floor(durations.length / 2);
    const medianMs =
      durations.length === 0
        ? null
        : durations.length % 2 === 1
          ? durations[mid]
          : (durations[mid - 1] + durations[mid]) / 2;
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
          <SegmentedControl
            options={SPANS}
            value={span}
            onChange={setSpan}
            ariaLabel="Usage span"
            className="shrink-0"
          />
        }
      />
      <div className="space-y-5 px-4 pb-28 sm:px-6 md:pb-12 lg:px-8">
        <MissionIntelligence query={missionQuery} />
        {runsQuery.isLoading ? (
          <LoadingState label="Loading run history…" />
        ) : runsQuery.isError ? (
          <ErrorState detail="Run history unavailable" hint="The gateway could not list runs." />
        ) : runs.length === 0 ? (
          <Card title="Run volume" icon={<BarChart3 size={16} className="text-copper" />}>
            <EmptyState
              icon={<BarChart3 size={24} />}
              title="No runs in this period"
              description="Analytics appear as soon as the agent executes runs. Start a conversation to see it working."
            />
          </Card>
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
                      { label: "Completed", value: stats.completed, color: PALETTE.ok },
                      { label: "Failed", value: stats.failed, color: PALETTE.danger },
                      { label: "Cancelled", value: stats.cancelled, color: PALETTE.raised },
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

function MissionIntelligence({ query }: { query: {
  isLoading: boolean;
  isError: boolean;
  error: unknown;
  data?: MissionAnalyticsView;
} }) {
  if (query.isLoading) {
    return null;
  }
  if (query.isError) {
    return (
      <Card title="Mission intelligence" icon={<BrainCircuit size={16} className="text-copper" />}>
        <ErrorState
          detail="Mission intelligence unavailable"
          hint="The gateway rejected the analytics request. Enable agentic engineering to collect it."
        />
      </Card>
    );
  }
  const snapshot = query.data;
  if (!snapshot || snapshot.missions_total === 0) {
    return (
      <Card title="Mission intelligence" icon={<BrainCircuit size={16} className="text-copper" />}>
        <EmptyState
          icon={<BrainCircuit size={24} />}
          title="No missions have finished yet"
          description="Terminal missions appear here with outcome decisions, role performance and accepted lessons once the orchestrator completes work."
        />
      </Card>
    );
  }
  return (
    <Card title="Mission intelligence" icon={<BrainCircuit size={16} className="text-copper" />}>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <StatTile label="Terminal missions" value={String(snapshot.missions_total)} />
        <StatTile label="Post-mortems learned" value={String(snapshot.missions_learned)} />
        <StatTile label="Deadline expiries" value={String(snapshot.expired)} />
        <StatTile
          label="Lesson backlog"
          value={String(Math.max(0, snapshot.missions_total - snapshot.missions_learned))}
        />
      </div>
      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <div>
          <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-faint">Outcomes</h4>
          <ul className="space-y-1.5">
            {Object.entries(snapshot.decisions).map(([status, count]) => (
              <li
                key={status}
                className="flex items-center justify-between rounded-lg border border-line-soft bg-canvas/60 px-3 py-1.5 text-xs"
              >
                <span className="text-muted">{status.replace(/_/g, " ")}</span>
                <span className="font-mono font-semibold text-ink">{count}</span>
              </li>
            ))}
          </ul>
        </div>
        <div>
          <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-faint">Role performance</h4>
          {snapshot.roles.length === 0 ? (
            <p className="text-xs text-muted">No specialist assignments recorded yet.</p>
          ) : (
            <ul className="space-y-1.5">
              {snapshot.roles.map((role) => (
                <li
                  key={role.role}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-line-soft bg-canvas/60 px-3 py-1.5 text-xs"
                >
                  <span className="font-semibold text-ink">{role.role}</span>
                  <span className="font-mono text-2xs text-muted">
                    tasks={role.tasks} succeeded={role.succeeded} failed={role.failed} blocked={role.blocked}{" "}
                    revisions={role.revisions}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
      <div className="mt-4">
        <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-faint">Dimension means</h4>
        {Object.keys(snapshot.dimension_means).length === 0 ? (
          <p className="text-xs text-muted">No post-mortem evaluations recorded yet.</p>
        ) : (
          <ul className="grid gap-1.5 sm:grid-cols-2">
            {Object.entries(snapshot.dimension_means).map(([dimension, mean]) => (
              <li
                key={dimension}
                className="flex items-center justify-between rounded-lg border border-line-soft bg-canvas/60 px-3 py-1.5 text-xs"
              >
                <span className="text-muted">{dimension.replace(/_/g, " ")}</span>
                <span className="font-mono font-semibold text-ink">{mean}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
      {snapshot.lessons.length > 0 && (
        <div className="mt-4">
          <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-faint">Accepted lessons</h4>
          <ul className="space-y-1.5">
            {snapshot.lessons.map((lesson) => (
              <li
                key={lesson.lesson_id}
                className="rounded-lg border border-line-soft bg-canvas/60 px-3 py-2 text-xs leading-relaxed text-muted"
              >
                {lesson.text}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
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
