/** ContextPanel — runtime context for the current conversation.
 *
 *  Shows the current run (status, live agent activity, verification state),
 *  pending approvals, session facts and the model provider in use. Optional
 *  on desktop (toggle), a bottom sheet on mobile.
 */

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity, Bot, Layers, ShieldCheck } from "lucide-react";
import { api } from "../../lib/api";
import type { ProviderView, RunView, SessionView } from "../../lib/types";
import { useGateway } from "../../store/gateway";
import { formatRelative, runStatusMeta } from "../../lib/format";
import { Badge, Button, Empty, KeyValue, Spinner, StatusDot } from "../ui";
import { RuntimeEventRow } from "../EventTrace";

export function ContextPanelBody({
  session,
  activeRunId,
  onCancelRun,
}: {
  session: SessionView | null;
  activeRunId: string | null;
  onCancelRun?: (runId: string) => void;
}) {
  const turns = useGateway((state) => state.turns);
  const sessionTurnsMap = useGateway((state) => state.sessionTurns);
  const approvals = useGateway((state) => state.approvals);

  const runsQuery = useQuery({
    queryKey: ["runs", session?.id],
    queryFn: () =>
      session ? api.get<RunView[]>(`/api/runs?session_id=${session.id}&limit=15`) : Promise.resolve([]),
    enabled: Boolean(session),
    refetchInterval: 5000,
  });
  const providersQuery = useQuery({
    queryKey: ["providers"],
    queryFn: () => api.get<ProviderView[]>("/api/providers"),
    staleTime: 30_000,
  });

  const sessionTurns = session ? (sessionTurnsMap[session.id] ?? []) : [];
  const liveTurns = sessionTurns.map((runId) => turns[runId]).filter(Boolean);
  const currentTurn = liveTurns.find((turn) => turn.status === "running") ?? liveTurns[liveTurns.length - 1];

  const currentRun: RunView | null = useMemo(() => {
    const runs = runsQuery.data ?? [];
    return runs.find((run) => run.run_id === activeRunId) ?? runs[0] ?? null;
  }, [runsQuery.data, activeRunId]);

  const sessionApprovals = approvals.filter((approval) => approval.session_id === session?.id);
  const readyProviders = (providersQuery.data ?? []).filter((provider) => provider.status === "ready");

  return (
    <div className="space-y-4 p-3.5">
      {/* Current run */}
      <section>
        <h3 className="mb-2 flex items-center gap-1.5 text-2xs font-semibold uppercase tracking-[0.16em] text-faint">
          <Activity size={11} /> Current run
        </h3>
        {currentTurn ? (
          <div className="rounded-xl border border-line bg-canvas/70 p-3">
            <div className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-2 text-2xs text-ink">
                {currentTurn.status === "running" ? (
                  <Spinner />
                ) : (
                  <StatusDot tone={currentTurn.status === "failed" ? "danger" : currentTurn.status === "completed" ? "ok" : "dim"} />
                )}
                <span className="font-mono">{currentTurn.status}</span>
              </span>
              {currentTurn.status === "running" && activeRunId && onCancelRun && (
                <Button variant="danger" size="sm" onClick={() => onCancelRun(activeRunId)}>
                  Cancel
                </Button>
              )}
            </div>
            <div className="mt-2 space-y-1">
              {currentTurn.events
                .filter((event) => event.kind === "tool_call" || event.kind === "plan" || event.kind === "recovery" || event.kind === "lesson" || event.kind === "skill")
                .slice(-6)
                .map((event, index) => (
                  <div key={index} className="truncate font-mono text-2xs text-faint">
                    {event.kind === "tool_call" && event.tool_call ? `→ ${event.tool_call.name}` : event.kind}
                  </div>
                ))}
              {currentTurn.runtimeEvents.slice(-5).map((event, index) => (
                <div key={`r${index}`} className="truncate font-mono text-2xs text-dim">
                  {event.event_type}
                </div>
              ))}
            </div>
          </div>
        ) : currentRun ? (
          <div className="rounded-xl border border-line bg-canvas/70 p-3">
            <div className="flex items-center justify-between gap-2">
              <span className="font-mono text-2xs text-dim">{currentRun.objective.slice(0, 60)}</span>
              <Badge tone={runStatusMeta(currentRun.status).tone}>
                {runStatusMeta(currentRun.status).label}
              </Badge>
            </div>
            {currentRun.stop_reason && (
              <p className="mt-1.5 font-mono text-2xs text-faint">{currentRun.stop_reason}</p>
            )}
          </div>
        ) : (
          <Empty>No runs on this session yet.</Empty>
        )}
      </section>

      {/* Verification */}
      <section>
        <h3 className="mb-2 flex items-center gap-1.5 text-2xs font-semibold uppercase tracking-[0.16em] text-faint">
          <ShieldCheck size={11} className="text-sage" /> Verification
        </h3>
        <p className="rounded-xl border border-line bg-canvas/70 px-3 py-2.5 text-2xs leading-relaxed text-dim">
          Step and completion checks run inside the canonical engine. Claims the agent could not
          verify are marked as unverified in its answers.
          {currentRun?.status === "verifying" && (
            <span className="mt-1.5 flex items-center gap-1.5 text-copper">
              <Spinner /> verifying current step…
            </span>
          )}
        </p>
      </section>

      {/* Approvals */}
      <section>
        <h3 className="mb-2 flex items-center gap-1.5 text-2xs font-semibold uppercase tracking-[0.16em] text-faint">
          <Layers size={11} className="text-warn" /> Approvals
        </h3>
        {sessionApprovals.length === 0 ? (
          <p className="rounded-xl border border-line bg-canvas/70 px-3 py-2.5 text-2xs text-faint">
            No approvals pending for this session. Mutating tools pause here.
          </p>
        ) : (
          <div className="space-y-1.5">
            {sessionApprovals.map((approval) => (
              <div
                key={approval.token}
                className="flex items-center justify-between gap-2 rounded-xl border border-warn/40 bg-warn/5 px-3 py-2"
              >
                <span className="truncate font-mono text-2xs text-warn">{approval.tool_name}</span>
                <Badge tone="warn">{Math.max(0, Math.round(approval.expires_in_seconds))}s</Badge>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Session facts */}
      <section>
        <h3 className="mb-2 flex items-center gap-1.5 text-2xs font-semibold uppercase tracking-[0.16em] text-faint">
          <Bot size={11} className="text-copper" /> Session
        </h3>
        {session ? (
          <div className="rounded-xl border border-line bg-canvas/70 p-3">
            <KeyValue
              rows={[
                ["Title", <span key="t">{session.title}</span>],
                ["Messages", <span key="m" className="font-mono">{session.message_count}</span>],
                ["Updated", <span key="u">{formatRelative(session.updated_at)}</span>],
                [
                  "Providers ready",
                  <span key="p" className="font-mono">
                    {readyProviders.length}
                  </span>,
                ],
              ]}
            />
          </div>
        ) : (
          <Empty>No session selected.</Empty>
        )}
      </section>

      {/* Recent runs of this session */}
      {(runsQuery.data ?? []).length > 0 && (
        <section>
          <h3 className="mb-2 flex items-center gap-1.5 text-2xs font-semibold uppercase tracking-[0.16em] text-faint">
            <Activity size={11} /> Recent runs
          </h3>
          <ul className="space-y-1">
            {(runsQuery.data ?? []).slice(0, 8).map((run) => (
              <li key={run.run_id}>
                <RuntimeEventRow
                  event={{
                    event_id: run.run_id,
                    run_id: run.run_id,
                    event_type: run.status,
                    sequence_no: 0,
                    payload: { objective: run.objective },
                    created_at: run.created_at,
                    step_id: null,
                    attempt_id: null,
                    parent_run_id: null,
                  }}
                />
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
