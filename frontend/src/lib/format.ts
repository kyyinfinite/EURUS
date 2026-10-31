/** Small formatting helpers shared across pages. */

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86_400)}d ago`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatUptime(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

export function shortId(id: string, size = 8): string {
  return id.length > size ? `${id.slice(0, size)}…` : id;
}

export const RUN_STATUS_LABEL: Record<string, { label: string; tone: "ok" | "warn" | "danger" | "dim" | "active" }> = {
  queued: { label: "Queued", tone: "dim" },
  running: { label: "Running", tone: "active" },
  waiting_approval: { label: "Awaiting approval", tone: "warn" },
  waiting_input: { label: "Awaiting input", tone: "warn" },
  retry_wait: { label: "Retrying", tone: "warn" },
  verifying: { label: "Verifying", tone: "active" },
  interrupted: { label: "Interrupted", tone: "warn" },
  completed: { label: "Completed", tone: "ok" },
  failed: { label: "Failed", tone: "danger" },
  cancelled: { label: "Cancelled", tone: "dim" },
};

export function runStatusMeta(status: string) {
  return RUN_STATUS_LABEL[status] ?? { label: status, tone: "dim" as const };
}
