/** Runtime Monitor — active runs, durable event streams, diagnostics. */

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, Square } from "lucide-react";
import { api } from "../lib/api";
import type {
  DiagnosticsView,
  RuntimeEventView,
  RunView,
  RuntimeStatusView,
} from "../lib/types";
import { useGateway } from "../store/gateway";
import {
  Badge,
  Button,
  Card,
  Empty,
  KeyValue,
  LoadingState,
  PageHeader,
  StatusDot,
} from "../components/ui";
import { RuntimeEventRow } from "../components/EventTrace";
import { formatBytes, formatTime, formatUptime, runStatusMeta, shortId } from "../lib/format";

export default function Runtime() {
  const queryClient = useQueryClient();
  const lastChange = useGateway((state) => state.lastSessionChange);
  const [selectedRun, setSelectedRun] = useState<string | null>(null);

  const statusQuery = useQuery({
    queryKey: ["runtime"],
    queryFn: () => api.get<RuntimeStatusView>("/api/runtime"),
    refetchInterval: 3000,
  });
  const diagnosticsQuery = useQuery({
    queryKey: ["diagnostics"],
    queryFn: () => api.get<DiagnosticsView>("/api/diagnostics"),
    refetchInterval: 15_000,
  });

  useEffect(() => {
    if (lastChange > 0) void queryClient.invalidateQueries({ queryKey: ["runtime"] });
  }, [lastChange, queryClient]);

  const status = statusQuery.data;
  const diagnostics = diagnosticsQuery.data;

  const eventsQuery = useQuery({
    queryKey: ["run-events", selectedRun],
    queryFn: () => api.get<RuntimeEventView[]>(`/api/runs/${selectedRun}/events`),
    enabled: Boolean(selectedRun),
    refetchInterval: 2000,
  });

  const cancelRun = useMutation({
    mutationFn: (runId: string) => api.post(`/api/runs/${runId}/cancel`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["runtime"] }),
  });

  const events = useMemo(
    () => (eventsQuery.data ?? []).slice().reverse(),
    [eventsQuery.data],
  );

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Runtime Monitor"
        subtitle="One canonical AgentEngine runtime — observed from every interface"
      />
      {statusQuery.isLoading ? (
        <LoadingState label="Loading runtime status…" />
      ) : (
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4 pb-28 sm:p-5 md:pb-12">
          {/* status strip */}
          <div className="grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-line bg-edge-soft sm:grid-cols-4">
            <Stat label="Engine" value={status?.runtime_health.started ? "started" : "idle"} tone={status?.runtime_health.started ? "ok" : "warn"} />
            <Stat label="In-process runs" value={status?.runtime_health.active_runs ?? 0} tone="dim" />
            <Stat label="Pending approvals" value={status?.pending_approvals ?? 0} tone={(status?.pending_approvals ?? 0) > 0 ? "warn" : "dim"} />
            <Stat label="Browser clients" value={status?.websocket_clients ?? 0} tone="dim" />
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            {/* active runs */}
            <Card title="Active runs" dense>
              {status && status.active_runs.length > 0 ? (
                <ul className="divide-y divide-line-soft">
                  {status.active_runs.map((run) => (
                    <li key={run.run_id} className="px-3.5 py-3">
                      <RunRow
                        run={run}
                        onCancel={cancelRun.mutate}
                        onSelect={() => setSelectedRun(run.run_id)}
                        selected={selectedRun === run.run_id}
                        cancelPending={cancelRun.isPending && cancelRun.variables === run.run_id}
                      />
                    </li>
                  ))}
                </ul>
              ) : (
                <div className="p-3.5">
                  <Empty>No active runs right now.</Empty>
                </div>
              )}
            </Card>

            {/* engine health */}
            <Card title="Runtime engine health">
              {status ? (
                <KeyValue
                  rows={[
                    ["Engine started", status.runtime_health.started ? "yes" : "no"],
                    ["In-process active runs", status.runtime_health.active_runs ?? 0],
                    ["Cached agents", status.runtime_health.cached_sessions ?? 0],
                    ["Session locks", status.runtime_health.session_locks ?? 0],
                    ["Event subscribers", status.runtime_health.subscribers ?? 0],
                    ["Dropped live events", status.runtime_health.dropped_events ?? 0],
                    ["Sessions", status.session_count],
                    ["Browser clients", status.websocket_clients],
                  ]}
                />
              ) : (
                <Empty>Loading…</Empty>
              )}
            </Card>
          </div>

          {/* event stream for the selected run */}
          {selectedRun && (
            <Card
              title={`Event stream — ${shortId(selectedRun)}`}
              dense
              actions={
                <Button size="sm" onClick={() => setSelectedRun(null)}>
                  Close
                </Button>
              }
            >
              <div className="max-h-80 overflow-y-auto p-3">
                {events.length === 0 ? (
                  <p className="py-4 text-center text-2xs text-faint">
                    No durable events recorded for this run yet.
                  </p>
                ) : (
                  events.map((event) => <RuntimeEventRow key={event.event_id} event={event} />)
                )}
              </div>
            </Card>
          )}

          {/* recent runs */}
          <Card title="Recent runs (all interfaces)" dense>
            {status && status.recent_runs.length > 0 ? (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[640px] text-left text-xs">
                  <thead>
                    <tr className="border-b border-line-soft text-2xs uppercase tracking-wide text-faint">
                      <th className="px-3.5 py-2 font-medium">Run</th>
                      <th className="px-3.5 py-2 font-medium">Objective</th>
                      <th className="px-3.5 py-2 font-medium">Origin</th>
                      <th className="px-3.5 py-2 font-medium">Status</th>
                      <th className="px-3.5 py-2 font-medium">Updated</th>
                    </tr>
                  </thead>
                  <tbody>
                    {status.recent_runs.map((run) => (
                      <tr
                        key={run.run_id}
                        onClick={() => setSelectedRun(run.run_id)}
                        className="cursor-pointer border-b border-line-soft/60 transition-colors last:border-0 hover:bg-raised-soft"
                      >
                        <td className="px-3.5 py-2 font-mono text-2xs text-dim">
                          {shortId(run.run_id)}
                        </td>
                        <td className="max-w-[280px] truncate px-3.5 py-2 text-dim">
                          {run.objective}
                        </td>
                        <td className="px-3.5 py-2">
                          <Badge tone={run.origin === "web" ? "active" : "dim"}>{run.origin}</Badge>
                        </td>
                        <td className="px-3.5 py-2">
                          <Badge tone={runStatusMeta(run.status).tone}>
                            {runStatusMeta(run.status).label}
                          </Badge>
                        </td>
                        <td className="px-3.5 py-2 font-mono text-2xs text-faint">
                          {formatTime(run.updated_at)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="p-3.5">
                <Empty>No runs recorded yet.</Empty>
              </div>
            )}
          </Card>

          {/* diagnostics */}
          {diagnostics && (
            <Card title="Diagnostics">
              <KeyValue
                rows={[
                  ["Version", diagnostics.version],
                  ["Python", diagnostics.python],
                  ["Uptime", formatUptime(diagnostics.uptime_seconds)],
                  ["Database", `${diagnostics.database_path} (${formatBytes(diagnostics.database_size_bytes)})`],
                  ["Sandbox backend", diagnostics.sandbox_backend],
                  ["Workspace", diagnostics.workspace],
                  ["Home", diagnostics.home_dir],
                ]}
              />
            </Card>
          )}
        </div>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string | number;
  tone: "ok" | "warn" | "dim";
}) {
  return (
    <div className="flex flex-col bg-surface px-3.5 py-3">
      <span className="text-2xs uppercase tracking-[0.14em] text-faint">{label}</span>
      <span
        className={`mt-1 font-mono text-base ${
          tone === "ok" ? "text-ok" : tone === "warn" ? "text-warn" : "text-ink"
        }`}
      >
        {value}
      </span>
    </div>
  );
}

function RunRow({
  run,
  onCancel,
  onSelect,
  selected,
  cancelPending,
}: {
  run: RunView;
  onCancel: (runId: string) => void;
  onSelect: () => void;
  selected: boolean;
  cancelPending: boolean;
}) {
  return (
    <div className="flex items-center justify-between gap-3">
      <button
        type="button"
        onClick={onSelect}
        className="group flex min-w-0 flex-1 items-center gap-2 text-left"
      >
        <StatusDot tone={runStatusMeta(run.status).tone} pulse={runStatusMeta(run.status).tone === "active"} />
        <div className="min-w-0">
          <p className="truncate text-xs text-ink group-hover:text-copper">{run.objective}</p>
          <p className="mt-0.5 font-mono text-2xs text-faint">
            {shortId(run.run_id)} · {run.origin} · started {formatTime(run.started_at)}
          </p>
        </div>
        <ChevronRight
          size={13}
          className={`shrink-0 transition-colors ${selected ? "text-copper" : "text-faint group-hover:text-copper"}`}
        />
      </button>
      <div className="flex shrink-0 items-center gap-2">
        <Badge tone={runStatusMeta(run.status).tone}>{runStatusMeta(run.status).label}</Badge>
        <Button variant="danger" size="sm" onClick={() => onCancel(run.run_id)} disabled={cancelPending}>
          <Square size={11} /> Cancel
        </Button>
      </div>
    </div>
  );
}
