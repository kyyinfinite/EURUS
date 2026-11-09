
import type { ReactNode } from "react";

export type CardElevation = "flat" | "card" | "raised" | "floating";

const ELEVATION_CLASS: Record<CardElevation, string> = {
  flat: "bg-surface border border-line",
  card: "bg-surface border border-line shadow-card",
  raised: "surface-raised-sculpted border border-line",
  floating: "surface-floating border border-line",
};

export function Card({
  title,
  actions,
  children,
  className = "",
  dense = false,
  icon,
  bodyClassName = "",
  elevation = "card",
  edgeHighlight = false,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  dense?: boolean;
  icon?: ReactNode;
  bodyClassName?: string;
  elevation?: CardElevation;
  edgeHighlight?: boolean;
}) {
  return (
    <section
      className={`relative overflow-hidden rounded-xl ${ELEVATION_CLASS[elevation]} ${
        edgeHighlight ? "edge-top-highlight" : ""
      } ${className}`}
    >
      {(title || actions) && (
        <header className="flex min-h-[52px] items-center justify-between gap-3 border-b border-line-soft px-4 py-3 sm:px-5">
          <h2 className="flex min-w-0 items-center gap-2 text-[15px] font-semibold text-ink">
            {icon}
            {title}
          </h2>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={dense ? bodyClassName : `p-4 sm:p-5 ${bodyClassName}`}>{children}</div>
    </section>
  );
}

export function MiniBars({ values, className = "" }: { values: number[]; className?: string }) {
  const max = Math.max(1, ...values);
  return (
    <div className={`flex h-10 items-end gap-1 ${className}`} aria-hidden="true">
      {values.map((value, index) => (
        <div
          key={index}
          className="w-2 rounded-sm bg-gradient-to-t from-copper/45 to-copper/75"
          style={{ height: `${Math.max(8, (value / max) * 100)}%`, opacity: value === 0 ? 0.3 : 1 }}
        />
      ))}
    </div>
  );
}

export function SegmentedControl<T extends string>({
  options,
  value,
  onChange,
  size = "md",
  className = "",
  ariaLabel,
}: {
  options: readonly T[];
  value: T;
  onChange: (value: T) => void;
  size?: "sm" | "md";
  className?: string;
  ariaLabel?: string;
}) {
  return (
    <div
      className={`inline-flex items-center gap-0.5 rounded-full border border-line bg-raised-soft/80 p-0.5 shadow-[inset_0_1px_2px_rgba(51,42,36,0.06)] ${className}`}
      role="group"
      aria-label={ariaLabel}
    >
      {options.map((option) => {
        const active = option === value;
        return (
          <button
            key={option}
            type="button"
            onClick={() => onChange(option)}
            aria-pressed={active}
            className={`rounded-full font-semibold capitalize transition-[background,color,box-shadow] duration-150 ${
              size === "sm" ? "px-2.5 py-0.5 text-[11px]" : "px-3 py-1 text-xs"
            } ${
              active
                ? "bg-surface text-ink shadow-[0_1px_2px_rgba(51,42,36,0.1),inset_0_1px_0_rgba(255,255,255,0.8)]"
                : "text-muted hover:text-ink"
            }`}
          >
            {option}
          </button>
        );
      })}
    </div>
  );
}
