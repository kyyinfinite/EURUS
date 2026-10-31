/** Home — the EURUS welcome experience.
 *
 *  Composition follows the welcome-screen reference (brand above, minimal
 *  navigation, spacious centered hero with a prominent central composer and
 *  shortcut actions) rendered in the EURUS identity (obsidian, bronze,
 *  copper, sage). Everything is real: the composer creates a canonical
 *  session and runs the agent; the status chips come from live APIs.
 *
 *  Below the hero (scroll), a restrained workspace overview surfaces runtime
 *  health, activity and quick actions — all from the dashboard API.
 */

import { useCallback, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  ArrowRight,
  CircleCheck,
  Container,
  Cpu,
  KeyRound,
  MessagesSquare,
  Settings as Config,
  ShieldCheck,
  Sparkles,
  Users,
} from "lucide-react";
import { api } from "../lib/api";
import type { DashboardView, MediaView, ProviderView, ToolView } from "../lib/types";
import { useGateway } from "../store/gateway";
import { useUI } from "../store/ui";
import { formatRelative, formatUptime, runStatusMeta } from "../lib/format";
import { Badge, Button, Card, Empty, MiniBars, Spinner, StatusDot } from "../components/ui";
import { EurusMark } from "../components/Logo";
import { ChatComposer } from "../components/chat/ChatComposer";

export default function Home() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const connected = useGateway((state) => state.connected);
  const openFloating = useUI((state) => state.openFloatingChat);
  const pushToast = useUI((state) => state.pushToast);

  const healthQuery = useQuery({
    queryKey: ["health"],
    queryFn: () => api.get<{ version: string; uptime_seconds: number }>("/api/health"),
    refetchInterval: 15_000,
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

  const providers = providersQuery.data ?? [];
  const readyProviders = providers.filter((provider) => provider.status === "ready").length;
  const missingKeys = providers.filter((provider) => provider.status === "missing key").length;
  const toolCount = toolsQuery.data?.length ?? 0;

  const titleFor = (text: string) =>
    (text.trim().split(/\s+/).slice(0, 6).join(" ") || "New conversation").slice(0, 80);

  const ensureSession = useCallback(
    async (firstText?: string) => {
      const session = await api.post<{ id: string }>("/api/sessions", {
        title: titleFor(firstText ?? "New conversation"),
      });
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
      return session.id;
    },
    [queryClient],
  );

  const send = useMutation({
    mutationFn: async (input: { text: string; attachments: MediaView[]; sessionId: string }) => {
      return api.post<{ run_id: string }>(`/api/sessions/${input.sessionId}/messages`, {
        text: input.text,
        attachments: input.attachments.map((media) => media.id),
      });
    },
  });

  const handleHeroSend = useCallback(
    async (text: string, attachments: MediaView[], resolvedSessionId: string) => {
      try {
        await send.mutateAsync({ text, attachments, sessionId: resolvedSessionId });
        navigate(`/chat/${resolvedSessionId}`);
      } catch (error) {
        pushToast({
          tone: "danger",
          title: "Could not start the run",
          detail: error instanceof Error ? error.message : undefined,
        });
        navigate(`/chat/${resolvedSessionId}`);
      }
    },
    [send, navigate, pushToast],
  );

  const shortcuts = [
    { label: "Open Chat", icon: MessagesSquare, to: "/chat" },
    { label: "View Sessions", icon: Container, to: "/sessions" },
    { label: "Runtime", icon: Activity, to: "/runtime" },
    { label: "Configuration", icon: Config, to: "/config" },
  ];

  return (
    <div className="h-full overflow-y-auto bg-atmosphere">
      {/* ---- welcome hero ------------------------------------------------------ */}
      <section className="flex min-h-[calc(100dvh-8rem)] flex-col items-center justify-center px-4 py-10 sm:px-6 md:justify-start md:pt-[8vh]">
        <div className="flex w-full max-w-2xl flex-col items-center">
          <div className="animate-fade-in">
            <EurusMark size={56} className="" />
          </div>
          <h1 className="mt-5 animate-fade-in text-lg font-semibold tracking-[0.4em] text-ink">
            EURUS
          </h1>
          <p className="mt-2.5 animate-fade-in text-2xs uppercase tracking-[0.24em] text-bronze">
            Your Personal AI Agent
          </p>
          <h2 className="mt-6 animate-fade-in text-center text-2xl font-light leading-snug text-ink sm:text-[28px]">
            Explore. <span className="font-normal text-copper">Build.</span> Evolve.
          </h2>
          <p className="mt-3 max-w-md animate-fade-in text-center text-xs leading-relaxed text-dim sm:text-[13px]">
            A powerful, flexible, and reliable AI agent system — coding tools, research, memory and
            verification, running on your own machine.
          </p>

          {/* announcement / system strip */}
          <div className="mt-6 flex flex-wrap items-center justify-center gap-2 animate-fade-in">
            <span className="inline-flex items-center gap-1.5 rounded-full border border-line bg-surface px-3 py-1 text-2xs text-dim">
              <StatusDot tone={connected ? "ok" : "danger"} pulse={!connected} />
              {healthQuery.data
                ? `Gateway v${healthQuery.data.version} · up ${formatUptime(healthQuery.data.uptime_seconds)}`
                : connected
                  ? "Gateway online"
                  : "Connecting…"}
            </span>
            {missingKeys > 0 ? (
              <button
                type="button"
                onClick={() => navigate("/providers")}
                className="inline-flex items-center gap-1.5 rounded-full border border-warn/30 bg-warn-soft px-3 py-1 text-2xs text-warn transition-colors hover:bg-warn-soft"
              >
                <KeyRound size={10} /> {missingKeys} provider{missingKeys > 1 ? "s" : ""} missing API keys
              </button>
            ) : (
              readyProviders > 0 && (
                <span className="inline-flex items-center gap-1.5 rounded-full border border-sage/25 bg-sage-soft px-3 py-1 text-2xs text-sage">
                  <ShieldCheck size={10} /> {readyProviders} provider{readyProviders > 1 ? "s" : ""} ready
                </span>
              )
            )}
            {toolCount > 0 && (
              <span className="inline-flex items-center gap-1.5 rounded-full border border-line bg-surface px-3 py-1 text-2xs text-dim">
                <Sparkles size={10} className="text-copper" /> {toolCount} tools available
              </span>
            )}
          </div>

          {/* central composer */}
          <div className="mt-8 w-full animate-slide-up">
            <ChatComposer
              sessionId={null}
              onEnsureSession={async () => ensureSession()}
              onSend={async (text, attachments) => {
                const sessionId = attachments[0]?.session_id ?? (await ensureSession(text));
                await handleHeroSend(text, attachments, sessionId);
              }}
              onCancel={undefined}
              placeholder="Ask anything — plan, research, code, automate…"
              variant="hero"
              maxRows={6}
            />
          </div>

          {/* honest capability note */}
          <p className="mt-3 max-w-md text-center text-2xs leading-relaxed text-faint">
            Attachments are stored in the canonical media store and delivered to the agent as
            references with inlined text — today&apos;s model providers are text-only.
          </p>

          {/* shortcut actions */}
          <nav className="mt-8 flex flex-wrap items-center justify-center gap-2.5 animate-fade-in" aria-label="Quick actions">
            {shortcuts.map((shortcut) => (
              <button
                key={shortcut.to}
                type="button"
                onClick={() => navigate(shortcut.to)}
                className="group flex items-center gap-2 rounded-xl border border-line bg-surface px-4 py-2.5 text-xs text-dim transition-all hover:border-copper/50 hover:text-copper"
              >
                <shortcut.icon size={14} strokeWidth={1.75} />
                {shortcut.label}
                <ArrowRight
                  size={12}
                  className="opacity-0 transition-opacity group-hover:opacity-100"
                />
              </button>
            ))}
            <button
              type="button"
              onClick={() => openFloating()}
              className="group flex items-center gap-2 rounded-xl border border-copper/40 bg-copper-soft px-4 py-2.5 text-xs text-copper transition-all hover:bg-copper/20"
            >
              <MessagesSquare size={14} strokeWidth={1.75} />
              Floating chat
            </button>
          </nav>
        </div>
      </section>

      {/* ---- workspace overview -------------------------------------------------- */}
      <WorkspaceOverview />
    </div>
  );
}

function WorkspaceOverview() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const approvals = useGateway((state) => state.approvals);
  const lastChange = useGateway((state) => state.lastSessionChange);

  const dashboardQuery = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => api.get<DashboardView>("/api/dashboard"),
    refetchInterval: 5000,
  });
  void lastChange;

  const cancelRun = useMutation({
    mutationFn: (runId: string) => api.post(`/api/runs/${runId}/cancel`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["dashboard"] }),
  });

  const data = dashboardQuery.data;
  const eventValues = useMemo(() => {
    if (!data) return [] as number[];
    return Object.values(data.session_events ?? {});
  }, [data]);

  if (dashboardQuery.isLoading) {
    return (
      <section className="px-4 pb-14 sm:px-6">
        <div className="mx-auto max-w-5xl">
          <Spinner label="Loading workspace overview…" />
        </div>
      </section>
    );
  }
  if (!data) {
    return (
      <section className="px-4 pb-14 sm:px-6">
        <div className="mx-auto max-w-5xl">
          <Empty>Workspace overview unavailable — is the gateway running?</Empty>
        </div>
      </section>
    );
  }

  const runtime = data.runtime;

  return (
    <section className="border-t border-line-soft bg-surface px-4 py-10 sm:px-6">
      <div className="mx-auto max-w-5xl space-y-5">
        <header className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold tracking-wide text-ink">Workspace overview</h2>
            <p className="mt-1 text-2xs text-faint">
              Live from the canonical runtime — sessions, runs and approvals shared by every
              interface.
            </p>
          </div>
          <div className="flex items-center gap-2 text-2xs text-dim">
            <StatusDot tone={runtime.runtime_health.started ? "ok" : "warn"} />
            engine {runtime.runtime_health.started ? "started" : "idle"}
          </div>
        </header>

        {/* metric strip */}
        <div className="grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-line bg-edge-soft sm:grid-cols-4">
          <MetricTile icon={<Cpu size={13} />} label="Active runs" value={runtime.active_runs.length} tone={runtime.active_runs.length > 0 ? "active" : "dim"} />
          <MetricTile
            icon={<CircleCheck size={13} />}
            label="Pending approvals"
            value={runtime.pending_approvals}
            tone={runtime.pending_approvals > 0 ? "warn" : "dim"}
            onClick={runtime.pending_approvals > 0 ? () => navigate("/approvals") : undefined}
          />
          <MetricTile icon={<Container size={13} />} label="Sessions" value={runtime.session_count} tone="dim" onClick={() => navigate("/sessions")} />
          <MetricTile icon={<Users size={13} />} label="Browser clients" value={runtime.websocket_clients} tone="dim" />
        </div>

        <div className="grid gap-5 lg:grid-cols-2">
          {/* active runs */}
          <Card title="Active runs" dense>
            {runtime.active_runs.length === 0 ? (
              <div className="p-3.5">
                <Empty>No active runs. Start one from Chat or any interface.</Empty>
              </div>
            ) : (
              <ul className="divide-y divide-line-soft">
                {runtime.active_runs.map((run) => (
                  <li key={run.run_id} className="flex items-center justify-between gap-3 px-3.5 py-2.5">
                    <div className="min-w-0">
                      <p className="truncate text-xs text-ink">{run.objective}</p>
                      <p className="mt-0.5 font-mono text-2xs text-faint">
                        {run.origin} · {formatRelative(run.created_at)}
                      </p>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <Badge tone={runStatusMeta(run.status).tone}>
                        {runStatusMeta(run.status).label}
                      </Badge>
                      <Button variant="danger" size="sm" onClick={() => cancelRun.mutate(run.run_id)}>
                        Cancel
                      </Button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          {/* recent sessions */}
          <Card title="Recent sessions" dense>
            {data.recent_sessions.length === 0 ? (
              <div className="p-3.5">
                <Empty>No sessions yet — the first message creates one.</Empty>
              </div>
            ) : (
              <ul className="divide-y divide-line-soft">
                {data.recent_sessions.slice(0, 6).map((session) => (
                  <li key={session.id}>
                    <button
                      type="button"
                      onClick={() => navigate(`/chat/${session.id}`)}
                      className="flex w-full items-center justify-between gap-3 px-3.5 py-2.5 text-left transition-colors hover:bg-raised-soft"
                    >
                      <div className="min-w-0">
                        <p className="truncate text-xs text-ink">{session.title || "Untitled"}</p>
                        <p className="mt-0.5 font-mono text-2xs text-faint">
                          {session.message_count} messages · {formatRelative(session.updated_at)}
                        </p>
                      </div>
                      {session.active_run_id && <StatusDot tone="active" pulse />}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>

        {/* activity row */}
        <div className="grid gap-5 lg:grid-cols-3">
          <Card title="Activity — last 7 days">
            {eventValues.length === 0 ? (
              <p className="text-2xs text-faint">No recorded events yet.</p>
            ) : (
              <div className="space-y-3">
                <MiniBars values={eventValues} />
                <p className="font-mono text-2xs text-faint">
                  {Object.keys(data.session_events ?? {}).join(" · ")}
                </p>
              </div>
            )}
          </Card>

          <Card title="Providers">
            <ul className="space-y-1.5">
              {(data.diagnostics.providers ?? []).slice(0, 5).map((provider) => (
                <li key={provider.name} className="flex items-center justify-between gap-2 text-xs">
                  <span className="flex min-w-0 items-center gap-2">
                    <StatusDot tone={provider.ready ? "ok" : "warn"} />
                    <span className="truncate text-dim">{provider.name}</span>
                  </span>
                  <Badge tone={provider.ready ? "ok" : "warn"}>{provider.protocol}</Badge>
                </li>
              ))}
            </ul>
          </Card>

          <Card title="Quick actions">
            <div className="flex flex-col gap-2">
              <Button variant="primary" onClick={() => navigate("/chat")}>
                <MessagesSquare size={13} /> Open Chat workspace
              </Button>
              {approvals.length > 0 && (
                <Button variant="sage" onClick={() => navigate("/approvals")}>
                  <ShieldCheck size={13} /> Review {approvals.length} approval
                  {approvals.length > 1 ? "s" : ""}
                </Button>
              )}
              <Button onClick={() => navigate("/runtime")}>
                <Activity size={13} /> Runtime monitor
              </Button>
              <Button onClick={() => navigate("/logs")}>View logs</Button>
            </div>
          </Card>
        </div>
      </div>
    </section>
  );
}

function MetricTile({
  icon,
  label,
  value,
  tone,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  value: number;
  tone: "ok" | "warn" | "active" | "dim";
  onClick?: () => void;
}) {
  const body = (
    <>
      <span className="flex items-center gap-1.5 text-2xs uppercase tracking-[0.14em] text-faint">
        {icon}
        {label}
      </span>
      <span
        className={`mt-1 font-mono text-lg ${
          tone === "active" ? "text-copper" : tone === "warn" ? "text-warn" : tone === "ok" ? "text-ok" : "text-ink"
        }`}
      >
        {value}
      </span>
    </>
  );
  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        className="flex flex-col items-start bg-surface px-3.5 py-3 text-left transition-colors hover:bg-raised-soft"
      >
        {body}
      </button>
    );
  }
  return <div className="flex flex-col items-start bg-surface px-3.5 py-3">{body}</div>;
}
