/** Card — the single surface container used by every module.
 *  White surface, hairline border, one soft shadow; no nested cards. */

import type { ReactNode } from "react";

export function Card({
  title,
  actions,
  children,
  className = "",
  dense = false,
  icon,
  bodyClassName = "",
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  dense?: boolean;
  icon?: ReactNode;
  bodyClassName?: string;
}) {
  return (
    <section className={`rounded-xl border border-line bg-surface shadow-card ${className}`}>
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

/** Small horizontal bar meter (activity sparkline fallback). */
export function MiniBars({ values, className = "" }: { values: number[]; className?: string }) {
  const max = Math.max(1, ...values);
  return (
    <div className={`flex h-10 items-end gap-1 ${className}`} aria-hidden="true">
      {values.map((value, index) => (
        <div
          key={index}
          className="w-2 rounded-sm bg-copper/60"
          style={{ height: `${Math.max(8, (value / max) * 100)}%`, opacity: value === 0 ? 0.3 : 1 }}
        />
      ))}
    </div>
  );
}
