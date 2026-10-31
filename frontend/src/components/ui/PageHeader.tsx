/** PageHeader — module title row: large title, subtitle, actions. */

import type { ReactNode } from "react";

export function PageHeader({
  title,
  subtitle,
  actions,
  icon,
  meta,
}: {
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
  icon?: ReactNode;
  meta?: ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-x-4 gap-y-3 px-4 pb-5 pt-6 sm:px-6 lg:px-8">
      <div className="flex min-w-0 items-center gap-3.5">
        {icon && (
          <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border border-copper/20 bg-copper-soft text-copper">
            {icon}
          </span>
        )}
        <div className="min-w-0">
          <h1 className="truncate text-[26px] font-bold leading-tight tracking-tight text-ink sm:text-[28px]">
            {title}
          </h1>
          {subtitle && (
            <p className="mt-0.5 text-sm text-muted">{subtitle}</p>
          )}
        </div>
        {meta && <div className="hidden shrink-0 items-center gap-2 sm:flex">{meta}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}
