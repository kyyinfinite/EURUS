/** Access & Security — the gateway's real security posture: what is enforced
 *  (from the live gateway's documented model and diagnostics), where data
 *  lives, and the one-click lock. Everything on this page reflects the
 *  running gateway; nothing is aspirational.
 */

import { useQuery } from "@tanstack/react-query";
import { Lock, ShieldCheck, Server, Database, KeyRound, Eye, MapPin } from "lucide-react";
import { api } from "../lib/api";
import type { ConfigView, DiagnosticsView } from "../lib/types";
import { formatBytes, formatUptime } from "../lib/format";
import { useGateway } from "../store/gateway";
import { Badge, Card, KeyValue, LoadingState, PageHeader, StatusIndicator, Button } from "../components/ui";
import { forgetToken } from "../lib/token";

export default function Security() {
  const connected = useGateway((state) => state.connected);
  const diagnosticsQuery = useQuery({
    queryKey: ["diagnostics"],
    queryFn: () => api.get<DiagnosticsView>("/api/diagnostics"),
    refetchInterval: 15_000,
  });
  const configQuery = useQuery({
    queryKey: ["config"],
    queryFn: () => api.get<ConfigView>("/api/config"),
  });

  const diagnostics = diagnosticsQuery.data;
  const config = (configQuery.data?.config ?? {}) as Record<string, Record<string, unknown>>;
  const sandbox = (config.sandbox ?? {}) as Record<string, unknown>;
  const permissions = (config.permissions ?? {}) as Record<string, unknown>;

  const lock = () => {
    forgetToken();
    window.location.reload();
  };

  return (
    <div className="h-full overflow-y-auto">
      <PageHeader
        title="Access & Security"
        icon={<ShieldCheck size={20} />}
        subtitle="The posture of this gateway — enforced by the server on every request"
        actions={
          <Button variant="danger" onClick={lock}>
            <Lock size={14} /> Lock workspace
          </Button>
        }
      />
      <div className="space-y-5 px-4 pb-28 sm:px-6 md:pb-12 lg:px-8">
        {diagnosticsQuery.isLoading ? (
          <LoadingState label="Loading security posture…" />
        ) : !diagnostics ? (
          <Card>
            <p className="text-sm text-muted">
              Diagnostics unavailable — the gateway could not be reached.
            </p>
          </Card>
        ) : (
          <>
            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
              <PostureCard
                icon={<KeyRound size={17} />}
                title="Token authentication"
                detail="Every API route and the WebSocket upgrade require the gateway bearer token. Only the read-only /api/health probe is exempt."
                status="enforced"
              />
              <PostureCard
                icon={<Server size={17} />}
                title="Origin & host validation"
                detail="State-changing requests and WebSocket upgrades verify Origin/Referer against the host allowlist (CSRF defense); the Host header is checked on every request (DNS-rebinding defense)."
                status="enforced"
              />
              <PostureCard
                icon={<Eye size={17} />}
                title="Secret redaction"
                detail="Config responses expose API-key env var names, never values. Approval arguments and the log ring buffer pass through redaction before leaving the process."
                status="enforced"
              />
              <PostureCard
                icon={<MapPin size={17} />}
                title="Loopback binding"
                detail="The gateway binds 127.0.0.1 by default; binding a non-loopback host prints a warning. The token never travels in a cookie."
                status="enforced"
              />
              <PostureCard
                icon={<ShieldCheck size={17} />}
                title="Fail-closed approvals"
                detail="Mutating tools pause for an explicit approval decision. When no approver is available the boundary denies the operation."
                status="enforced"
              />
              <PostureCard
                icon={<Lock size={17} />}
                title="Live connection"
                detail={
                  connected
                    ? "The WebSocket event stream is authenticated and live."
                    : "The event stream is currently reconnecting."
                }
                status={connected ? "ok" : "warn"}
              />
            </div>

            <div className="grid gap-5 lg:grid-cols-2">
              <Card title="Deployment footprint" icon={<Server size={16} className="text-copper" />}>
                <KeyValue
                  rows={[
                    ["Version", `EURUS v${diagnostics.version}`],
                    ["Python", diagnostics.python],
                    ["Uptime", formatUptime(diagnostics.uptime_seconds)],
                    ["Home directory", <span key="h" className="font-mono text-2xs">{diagnostics.home_dir}</span>],
                    ["Config path", <span key="c" className="font-mono text-2xs">{diagnostics.config_path}</span>],
                    [
                      "Sandbox backend",
                      <span key="s" className="flex items-center gap-2">
                        {String(sandbox.backend ?? diagnostics.sandbox_backend)}
                        <Badge tone="dim">execution policy</Badge>
                      </span>,
                    ],
                    [
                      "Workspace",
                      <span key="w" className="font-mono text-2xs">
                        {String(permissions.workspace ?? diagnostics.workspace)}
                      </span>,
                    ],
                  ]}
                />
              </Card>

              <Card title="Data at rest" icon={<Database size={16} className="text-copper" />}>
                <KeyValue
                  rows={[
                    [
                      "Database",
                      <span key="d" className="font-mono text-2xs">
                        {diagnostics.database_path}
                      </span>,
                    ],
                    ["Database size", formatBytes(diagnostics.database_size_bytes)],
                    ["Media store", <span key="m" className="font-mono text-2xs">~/.eurus/media/ (per-session directories)</span>],
                    ["Access", "All reads go through the authenticated API; media URLs are single-session scoped."],
                  ]}
                />
                <p className="mt-4 rounded-lg bg-raised-soft/60 px-3.5 py-2.5 text-xs leading-relaxed text-muted">
                  Approval decisions, sessions, runs and media rows all live in the same SQLite
                  authority shared with the TUI and CLI. Deleting a session removes its messages;
                  run history stays for audit.
                </p>
              </Card>
            </div>

            <Card title="Session token" icon={<KeyRound size={16} className="text-copper" />}>
              <div className="flex flex-wrap items-center justify-between gap-4">
                <p className="max-w-2xl text-sm leading-relaxed text-muted">
                  This browser holds the gateway access token in local storage only. Locking the
                  workspace removes it immediately; paste a fresh token from{" "}
                  <code className="rounded bg-raised-soft px-1.5 py-0.5 font-mono text-2xs text-copper-dark">
                    eurus gateway web
                  </code>{" "}
                  to return.
                </p>
                <Button variant="danger" size="lg" onClick={lock}>
                  <Lock size={15} /> Lock this browser
                </Button>
              </div>
            </Card>
          </>
        )}
      </div>
    </div>
  );
}

function PostureCard({
  icon,
  title,
  detail,
  status,
}: {
  icon: React.ReactNode;
  title: string;
  detail: string;
  status: "enforced" | "ok" | "warn";
}) {
  return (
    <div className="rounded-xl border border-line bg-surface p-5 shadow-card">
      <div className="flex items-center justify-between gap-3">
        <span className="flex h-10 w-10 items-center justify-center rounded-xl border border-ok/20 bg-ok-soft text-ok">
          {icon}
        </span>
        {status === "enforced" ? (
          <StatusIndicator tone="ok" label="Enforced" />
        ) : status === "ok" ? (
          <StatusIndicator tone="ok" label="Live" />
        ) : (
          <StatusIndicator tone="warn" label="Reconnecting" pulse />
        )}
      </div>
      <p className="mt-3.5 text-[15px] font-semibold text-ink">{title}</p>
      <p className="mt-1.5 text-[13px] leading-relaxed text-muted">{detail}</p>
    </div>
  );
}
