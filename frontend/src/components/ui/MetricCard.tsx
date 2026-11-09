
import type { ReactNode } from "react";
import { ArrowDownRight, ArrowUpRight } from "lucide-react";

export function MetricCard({
  icon,
  label,
  value,
  hint,
  tone = "ink",
  delta,
  onClick,
  live = false,
  emphasis = false,
}: {
  icon: ReactNode;
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "ink" | "copper" | "ok" | "warn" | "danger" | "info";
  delta?: { value: string; direction: "up" | "down"; good?: boolean };
  onClick?: () => void;
  live?: boolean;
  emphasis?: boolean;
}) {
  const valueColor =
    tone === "copper"
      ? "text-copper-dark"
      : tone === "ok"
        ? "text-ok"
        : tone === "warn"
          ? "text-warn"
          : tone === "danger"
            ? "text-danger"
            : tone === "info"
              ? "text-info"
              : "text-ink";
  const iconTone =
    tone === "copper"
      ? "border-copper/20 bg-copper-soft text-copper"
      : tone === "ok"
        ? "border-ok/20 bg-ok-soft text-ok"
        : tone === "warn"
          ? "border-warn/25 bg-warn-soft text-warn"
          : tone === "danger"
            ? "border-danger/20 bg-danger-soft text-danger"
            : tone === "info"
              ? "border-info/20 bg-info-soft text-info"
              : "border-line bg-raised-soft text-muted";

  const body = (
    <>
      <div className="flex items-start justify-between gap-3">
        <span
          className={`flex h-10 w-10 items-center justify-center rounded-xl border shadow-[inset_0_1px_0_rgba(255,255,255,0.65)] ${iconTone}`}
        >
          {icon}
        </span>
        <span className="flex items-center gap-2">
          {live && (
            <span className="inline-flex items-center gap-1 rounded-full border border-ok/25 bg-ok-soft px-2 py-0.5 text-[11px] font-semibold text-ok">
              <span className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-ok" />
              Live
            </span>
          )}
          {delta && (
            <span
              className={`inline-flex items-center gap-0.5 rounded-full border px-2 py-0.5 text-[11px] font-semibold tabular-nums ${
                delta.good === false
                  ? "border-danger/25 bg-danger-soft text-danger"
                  : delta.good === true
                    ? "border-ok/25 bg-ok-soft text-ok"
                    : "border-line bg-raised-soft text-muted"
              }`}
            >
              {delta.direction === "up" ? <ArrowUpRight size={11} /> : <ArrowDownRight size={11} />}
              {delta.value}
            </span>
          )}
        </span>
      </div>
      <div
        className={`mt-4 font-bold leading-none tracking-tight tabular-nums ${valueColor} ${
          emphasis ? "text-[34px]" : "text-[30px]"
        }`}
      >
        {value}
      </div>
      <div className="mt-1.5 text-[13px] font-medium text-muted">{label}</div>
      {hint && <div className="mt-1 text-xs text-faint">{hint}</div>}
    </>
  );

  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        className={`edge-top-highlight relative rounded-xl border border-line bg-surface p-4 text-left shadow-card transition-[transform,box-shadow,border-color] duration-200 hover:-translate-y-0.5 hover:border-copper/40 hover:shadow-float focus-visible:outline-2 sm:p-5 ${
          emphasis ? "surface-raised-sculpted" : ""
        }`}
      >
        {body}
      </button>
    );
  }
  return (
    <div
      className={`edge-top-highlight relative flex h-full flex-col rounded-xl border border-line bg-surface p-4 shadow-card sm:p-5 ${
        emphasis ? "surface-raised-sculpted" : ""
      }`}
    >
      {body}
    </div>
  );
}
