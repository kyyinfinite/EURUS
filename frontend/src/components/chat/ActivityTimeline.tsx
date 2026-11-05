import { Activity, AlertCircle, CheckCircle2, Circle, Clock3, ShieldCheck } from "lucide-react";
import { useMemo } from "react";
import type { AgentEventPayload } from "../../lib/types";
import { buildActivityTimeline, type RuntimeActivity } from "../../lib/thinking";

const ICONS = {
  neutral: Circle,
  success: CheckCircle2,
  warning: ShieldCheck,
  danger: AlertCircle,
} as const;

export function ActivityTimeline({ events, runtimeEvents, expanded }: {
  events: AgentEventPayload[];
  runtimeEvents: RuntimeActivity[];
  expanded: boolean;
}) {
  const items = useMemo(() => buildActivityTimeline(events, runtimeEvents), [events, runtimeEvents]);
  const visible = expanded ? items : items.slice(-4);
  if (items.length === 0) {
    return (
      <div className="flex items-center gap-2 rounded-lg bg-canvas/70 px-3 py-2.5 text-xs text-muted">
        <Clock3 size={13} /> Activity details will appear as EURUS works.
      </div>
    );
  }
  return (
    <ol className="space-y-0.5" aria-label="Agent activity timeline">
      {visible.map((item, index) => {
        const Icon = ICONS[item.tone];
        const isLatest = index === visible.length - 1;
        return (
          <li key={item.id} className="relative flex gap-2.5 py-1.5">
            <div className="relative flex w-4 shrink-0 justify-center">
              {index < visible.length - 1 && <span className="absolute bottom-[-8px] top-4 w-px bg-line" />}
              <span className={`relative z-[1] mt-0.5 flex h-4 w-4 items-center justify-center rounded-full ${item.tone === "success" ? "bg-sage-soft text-sage" : item.tone === "warning" ? "bg-warn-soft text-warn" : item.tone === "danger" ? "bg-danger-soft text-danger" : "bg-raised-soft text-faint"}`}>
                <Icon size={10} />
              </span>
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className={`text-xs font-medium ${item.tone === "danger" ? "text-danger" : "text-ink"}`}>{item.title}</span>
                {isLatest && <span className="rounded-full bg-copper-soft px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-copper">Latest</span>}
                {item.source === "runtime" && <span className="text-[9px] uppercase tracking-wide text-faint">Trace</span>}
              </div>
              {item.detail && <p className="mt-0.5 break-words text-[11px] leading-relaxed text-muted">{item.detail}</p>}
            </div>
          </li>
        );
      })}
      {items.length > visible.length && (
        <li className="flex items-center gap-1.5 pl-6 pt-1 text-[10px] text-faint">
          <Activity size={10} /> {items.length - visible.length} earlier activities hidden
        </li>
      )}
    </ol>
  );
}
