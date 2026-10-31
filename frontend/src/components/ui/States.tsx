/** Loading / empty / error states — one consistent voice per state. */

import type { ReactNode } from "react";

export function Spinner({ label, size = 14 }: { label?: string; size?: number }) {
  return (
    <span className="inline-flex items-center gap-2 text-[13px] text-muted" role="status">
      <span
        className="animate-spin rounded-full border-[2px] border-copper/30 border-t-copper"
        style={{ width: size, height: size }}
      />
      {label}
    </span>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded-lg bg-raised-soft ${className}`} />;
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-24 items-center justify-center rounded-lg border border-dashed border-line px-6 py-6 text-center text-[13px] text-muted">
      {children}
    </div>
  );
}

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: ReactNode;
  title: string;
  description?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-6 py-16 text-center">
      {icon && (
        <div className="flex h-14 w-14 items-center justify-center rounded-2xl border border-copper/20 bg-copper-soft text-copper">
          {icon}
        </div>
      )}
      <div className="max-w-md">
        <p className="text-[15px] font-semibold text-ink">{title}</p>
        {description && (
          <p className="mt-1.5 text-sm leading-relaxed text-muted">{description}</p>
        )}
      </div>
      {action}
    </div>
  );
}

export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center justify-center p-12" role="status" aria-live="polite">
      <Spinner label={label} size={18} />
    </div>
  );
}

export function ErrorState({ detail, hint }: { detail: string; hint?: string }) {
  return (
    <div className="rounded-xl border border-danger/30 bg-danger-soft px-4 py-3.5" role="alert">
      <p className="text-sm font-medium text-danger">{detail}</p>
      {hint && <p className="mt-1 text-[13px] leading-relaxed text-danger/80">{hint}</p>}
    </div>
  );
}

export function ErrorNote({ detail }: { detail: string }) {
  return (
    <div className="rounded-lg border border-danger/30 bg-danger-soft px-3 py-2 text-[13px] text-danger" role="alert">
      {detail}
    </div>
  );
}

export function KeyValue({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[minmax(110px,max-content)_1fr] gap-x-4 gap-y-2 text-sm">
      {rows.map(([key, value]) => (
        <div key={key} className="contents">
          <dt className="text-muted">{key}</dt>
          <dd className="min-w-0 break-words text-ink">{value}</dd>
        </div>
      ))}
    </dl>
  );
}
