
import type { ReactNode } from "react";

export type Tone = "ok" | "warn" | "danger" | "info" | "dim" | "active" | "sage" | "bronze";

const TONE_CLASS: Record<Tone, string> = {
  ok: "bg-ok-soft text-ok border-ok/25",
  warn: "bg-warn-soft text-warn border-warn/25",
  danger: "bg-danger-soft text-danger border-danger/25",
  info: "bg-info-soft text-info border-info/25",
  dim: "bg-raised-soft text-muted border-line",
  active: "bg-copper-soft text-copper-dark border-copper/25",
  sage: "bg-sage-soft text-sage border-sage/25",
  bronze: "bg-bronze-soft text-bronze-dark border-bronze/30",
};

export function Badge({
  tone = "dim",
  children,
  className = "",
}: {
  tone?: Tone;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium leading-4 ${TONE_CLASS[tone]} ${className}`}
    >
      {children}
    </span>
  );
}

const DOT_CLASS: Record<Tone, string> = {
  ok: "bg-ok",
  warn: "bg-warn",
  danger: "bg-danger",
  info: "bg-info",
  dim: "bg-faint",
  active: "bg-copper",
  sage: "bg-sage",
  bronze: "bg-bronze",
};

export function StatusDot({
  tone = "dim",
  pulse = false,
}: {
  tone?: Tone;
  pulse?: boolean;
}) {
  return (
    <span className="relative inline-flex h-2 w-2 shrink-0">
      {pulse && (
        <span
          className={`absolute inset-0 rounded-full ${DOT_CLASS[tone]} animate-breathe`}
          aria-hidden="true"
        />
      )}
      <span className={`relative h-2 w-2 rounded-full ${DOT_CLASS[tone]}`} />
    </span>
  );
}

export function StatusIndicator({
  tone = "dim",
  label,
  pulse = false,
  className = "",
}: {
  tone?: Tone;
  label: string;
  pulse?: boolean;
  className?: string;
}) {
  const textColor =
    tone === "ok"
      ? "text-ok"
      : tone === "warn"
        ? "text-warn"
        : tone === "danger"
          ? "text-danger"
          : tone === "sage"
            ? "text-sage"
            : tone === "info"
              ? "text-info"
              : tone === "active" || tone === "bronze"
                ? "text-copper-dark"
                : "text-muted";
  return (
    <span
      className={`inline-flex items-center gap-1.5 text-[13px] font-medium ${textColor} ${className}`}
    >
      <StatusDot tone={tone} pulse={pulse} />
      {label}
    </span>
  );
}
