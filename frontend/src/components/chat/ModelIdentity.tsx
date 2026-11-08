import { Cpu, Gauge, Sparkles } from "lucide-react";
import type { AgentEventPayload } from "../../lib/types";
import { getModelIdentity, getTokenUsage, type RuntimeActivity } from "../../lib/thinking";

export function ModelIdentity({ events, runtimeEvents }: {
  events: AgentEventPayload[];
  runtimeEvents: RuntimeActivity[];
}) {
  const identity = getModelIdentity(runtimeEvents);
  const usage = getTokenUsage(events, runtimeEvents);
  const hasUsage = usage.input + usage.output > 0;
  return (
    <div className="rounded-xl border border-line-soft bg-white/70 p-3">
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-copper/15 bg-copper-soft text-copper">
            <Cpu size={15} />
          </div>
          <div className="min-w-0">
            <p className="text-[10px] font-semibold uppercase tracking-[0.15em] text-faint">Model route</p>
            <p className="truncate text-xs font-semibold text-ink">{identity.name}</p>
            <p className="truncate text-[11px] text-muted">{identity.detail}</p>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1 rounded-full border border-sage/25 bg-sage-soft px-2 py-1 text-[10px] font-medium text-sage">
          <Sparkles size={10} /> Runtime
        </div>
      </div>
      {hasUsage && (
        <div className="mt-3 grid grid-cols-2 gap-2 border-t border-line-soft pt-2.5">
          <div>
            <p className="text-[10px] text-faint">Input tokens</p>
            <p className="mt-0.5 font-mono text-xs font-semibold tabular-nums text-ink">{usage.input.toLocaleString()}</p>
          </div>
          <div>
            <p className="text-[10px] text-faint">Output tokens</p>
            <p className="mt-0.5 font-mono text-xs font-semibold tabular-nums text-ink">{usage.output.toLocaleString()}</p>
          </div>
        </div>
      )}
      {!hasUsage && (
        <div className="mt-2 flex items-center gap-1.5 text-[10px] text-faint">
          <Gauge size={11} /> Usage details appear when the runtime reports them
        </div>
      )}
    </div>
  );
}
