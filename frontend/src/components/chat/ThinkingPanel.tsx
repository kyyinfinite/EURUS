import { memo } from "react";
import { ChevronDown, ChevronUp, CircleCheck, CircleDot, ListTree, Sparkles, Wrench } from "lucide-react";
import type { AgentEventPayload } from "../../lib/types";
import { describeCurrentActivity, type RuntimeActivity } from "../../lib/thinking";
import { useUI } from "../../store/ui";
import { ActivityTimeline } from "./ActivityTimeline";
import { ModelIdentity } from "./ModelIdentity";

const STATUS_BADGE: Record<string, string> = {
  danger: "border-danger/25 bg-danger-soft text-danger",
  warn: "border-warn/25 bg-warn-soft text-warn",
  active: "border-copper/25 bg-copper-soft text-copper",
  success: "border-sage/25 bg-sage-soft text-sage",
};

export const ThinkingPanel = memo(function ThinkingPanel({
  status,
  origin,
  events,
  runtimeEvents,
  stopReason,
}: {
  status: "running" | "completed" | "failed" | "cancelled";
  origin: "web" | "external";
  events: AgentEventPayload[];
  runtimeEvents: RuntimeActivity[];
  stopReason?: string | null;
}) {
  const expanded = useUI((state) => state.thinkingPanelExpanded);
  const setExpanded = useUI((state) => state.setThinkingPanelExpanded);
  const timelineExpanded = useUI((state) => state.thinkingTimelineExpanded);
  const setTimelineExpanded = useUI((state) => state.setThinkingTimelineExpanded);
  const running = status === "running";
  const failed = status === "failed";
  const title = running
    ? "EURUS is thinking"
    : failed
      ? "Execution needs attention"
      : status === "cancelled"
        ? "Run stopped"
        : "Execution complete";
  const currentActivity = describeCurrentActivity(events);
  const toolCount = events.filter((event) => event.kind === "tool_call").length;
  const plan = [...events].reverse().find((event) => event.kind === "plan" && event.text)?.text;
  const statusTone = failed ? "danger" : status === "cancelled" ? "warn" : running ? "active" : "success";
  const statusLabel =
    origin === "external" ? "External run" : status === "completed" ? "complete" : status;

  return (
    <section
      className={`overflow-hidden rounded-xl border shadow-[inset_0_1px_0_rgba(255,255,255,0.55)] ${
        failed
          ? "border-danger/30 bg-danger-soft/40"
          : "border-copper/20 bg-gradient-to-b from-copper-tint/80 via-surface to-surface"
      }`}
      aria-label="Agent thinking panel"
    >
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
        className="flex w-full items-center gap-3 px-3.5 py-3 text-left transition-colors hover:bg-white/40 sm:px-4"
      >
        <span className="relative flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border bg-white shadow-[0_1px_2px_rgba(51,42,36,0.05)]">
          {running ? (
            <>
              <span
                className="absolute inset-0 rounded-xl border border-copper/40 animate-breathe"
                aria-hidden="true"
              />
              <Sparkles size={16} className="text-copper" />
            </>
          ) : failed ? (
            <CircleDot size={16} className="text-danger" />
          ) : (
            <CircleCheck size={16} className="text-sage" />
          )}
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-2">
            <span className="text-[13px] font-semibold text-ink">{title}</span>
            <span
              className={`rounded-full border px-2 py-0.5 text-[9px] font-semibold uppercase tracking-[0.12em] ${STATUS_BADGE[statusTone]}`}
            >
              {statusLabel}
            </span>
          </span>
          <span className="mt-0.5 block truncate text-[11px] text-muted">
            {running
              ? currentActivity
              : stopReason ||
                (failed
                  ? "The run ended before completion"
                  : status === "cancelled"
                    ? "The run was cancelled"
                    : "EURUS has finished this run")}
          </span>
        </span>
        <span className="flex shrink-0 items-center gap-2 text-[10px] text-faint">
          {toolCount > 0 && (
            <span className="hidden items-center gap-1 sm:flex">
              <Wrench size={11} /> {toolCount}
            </span>
          )}
          {expanded ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
        </span>
      </button>

      {expanded && (
        <div className="space-y-3 border-t border-copper/10 px-3.5 pb-3.5 pt-3 sm:px-4">
          {plan && (
            <div className="rounded-xl border border-sage/20 bg-sage-soft/60 px-3 py-2.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.4)]">
              <div className="mb-1 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.13em] text-sage">
                <ListTree size={11} /> Current plan
              </div>
              <p className="whitespace-pre-wrap text-xs leading-relaxed text-ink">{plan}</p>
            </div>
          )}
          <ModelIdentity events={events} runtimeEvents={runtimeEvents} />
          <div className="overflow-hidden rounded-xl border border-line-soft bg-white/70 shadow-[inset_0_1px_0_rgba(255,255,255,0.6)]">
            <div className="flex items-center justify-between gap-2 border-b border-line-soft px-3 py-2">
              <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-dim">
                <ListTree size={11} /> Activity timeline
              </div>
              <button
                type="button"
                onClick={() => setTimelineExpanded(!timelineExpanded)}
                className="rounded-md px-2 py-1 text-[10px] font-medium text-copper transition-colors hover:bg-copper-soft"
                aria-expanded={timelineExpanded}
              >
                {timelineExpanded ? "Recent only" : "Show all"}
              </button>
            </div>
            <div className="px-3 py-2.5">
              <ActivityTimeline events={events} runtimeEvents={runtimeEvents} expanded={timelineExpanded} />
            </div>
          </div>
        </div>
      )}
    </section>
  );
});
