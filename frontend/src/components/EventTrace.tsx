/** Render of a run's live agent events + durable runtime events.
 *  Used by the Chat page (inline turns) and the Runtime monitor. */

import { Wrench, CheckCircle2, AlertTriangle, Cog, BookMarked, Map, LifeBuoy } from "lucide-react";
import type { AgentEventPayload, RuntimeEventView } from "../lib/types";
import { Badge } from "./ui";
import { Markdown } from "./markdown/Markdown";

export function ToolChip({ event }: { event: AgentEventPayload }) {
  if (event.tool_call) {
    const args = JSON.stringify(event.tool_call.arguments);
    return (
      <div className="my-1.5 flex items-start gap-2.5 rounded-lg border border-line bg-raised-soft/60 px-3 py-2">
        <Wrench size={13} className="mt-0.5 shrink-0 text-copper" />
        <div className="min-w-0">
          <div className="font-mono text-2xs font-medium text-copper-dark">{event.tool_call.name}</div>
          <div className="truncate font-mono text-2xs text-muted" title={args}>
            {args.length > 160 ? `${args.slice(0, 160)}…` : args}
          </div>
        </div>
      </div>
    );
  }
  if (event.tool_result) {
    return (
      <div
        className={`my-1.5 rounded-lg border px-3 py-2 ${
          event.tool_result.is_error
            ? "border-danger/30 bg-danger-soft"
            : "border-line bg-raised-soft/60"
        }`}
      >
        <div className="flex items-center gap-1.5 font-mono text-2xs font-medium">
          {event.tool_result.is_error ? (
            <AlertTriangle size={12} className="text-danger" />
          ) : (
            <CheckCircle2 size={12} className="text-ok" />
          )}
          <span className={event.tool_result.is_error ? "text-danger" : "text-ok"}>
            {event.tool_result.name}
          </span>
          {event.tool_result.error_code && (
            <span className="text-faint">{event.tool_result.error_code}</span>
          )}
        </div>
        {event.tool_result.output && (
          <pre className="mt-1.5 max-h-36 overflow-y-auto whitespace-pre-wrap rounded-md bg-surface px-2.5 py-2 font-mono text-2xs leading-relaxed text-muted">
            {event.tool_result.output}
          </pre>
        )}
      </div>
    );
  }
  return null;
}

const RUNTIME_EVENT_LABEL: Record<string, string> = {
  "run.started": "Run started",
  "run.completed": "Run completed",
  "run.failed": "Run failed",
  "run.cancelled": "Run cancelled",
  model_call: "Model call finished",
  "checkpoint.saved": "Checkpoint saved",
  "checkpoint.failed": "Checkpoint failed",
  "tool.call.started": "Tool started",
  "tool.call.finished": "Tool finished",
  "approval.requested": "Approval requested",
  "approval.granted": "Approval granted",
  "approval.denied": "Approval denied",
  "approval.unavailable": "Approval unavailable (denied)",
  "plan.updated": "Plan updated",
  "recovery.attempted": "Recovery attempted",
};

export function RuntimeEventRow({ event }: { event: RuntimeEventView }) {
  const label = RUNTIME_EVENT_LABEL[event.event_type] ?? event.event_type;
  const tone =
    event.event_type.includes("failed") || event.event_type.includes("denied")
      ? "danger"
      : event.event_type.includes("granted") || event.event_type.includes("completed")
        ? "ok"
        : "dim";
  return (
    <div className="flex items-center gap-2 py-1 font-mono text-2xs text-muted">
      <Badge tone={tone}>{label}</Badge>
      <span className="truncate text-faint">{JSON.stringify(event.payload).slice(0, 120)}</span>
    </div>
  );
}

export function InlineActivity({ events }: { events: AgentEventPayload[] }) {
  return (
    <div>
      {events.map((event, index) => {
        if (event.kind === "tool_call" || event.kind === "tool_result") {
          return <ToolChip key={index} event={event} />;
        }
        if (event.kind === "step") {
          return (
            <div key={index} className="my-1 flex items-center gap-1.5 font-mono text-2xs text-faint">
              <Cog size={11} className="animate-spin-slow" />
              step {event.step}
            </div>
          );
        }
        if (event.kind === "plan") {
          return (
            <div
              key={index}
              className="my-1.5 flex items-start gap-2.5 rounded-lg border border-sage/25 bg-sage-soft px-3 py-2 text-[13px] text-sage"
            >
              <Map size={13} className="mt-0.5 shrink-0" />
              <span className="whitespace-pre-wrap">{event.text}</span>
            </div>
          );
        }
        if (event.kind === "recovery") {
          return (
            <div
              key={index}
              className="my-1.5 flex items-start gap-2.5 rounded-lg border border-warn/30 bg-warn-soft px-3 py-2 text-[13px] text-warn"
            >
              <LifeBuoy size={13} className="mt-0.5 shrink-0" />
              <span className="whitespace-pre-wrap">{event.text}</span>
            </div>
          );
        }
        if (event.kind === "lesson" || event.kind === "skill") {
          return (
            <div key={index} className="my-1 flex items-center gap-1.5 font-mono text-2xs text-sage">
              <BookMarked size={11} />
              {event.text}
            </div>
          );
        }
        if (event.kind === "model_call") {
          return (
            <div key={index} className="my-0.5 font-mono text-2xs text-faint">
              step {event.step} · in {event.usage?.input_tokens ?? 0} / out{" "}
              {event.usage?.output_tokens ?? 0}
              {event.stop_reason ? ` · ${event.stop_reason}` : ""}
            </div>
          );
        }
        return null;
      })}
    </div>
  );
}

export function AssistantMarkdown({ text, streaming }: { text: string; streaming?: boolean }) {
  return <Markdown text={text} streaming={streaming} />;
}
