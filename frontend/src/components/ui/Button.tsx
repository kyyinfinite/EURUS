/** Button + IconButton — the primary interactive primitives.
 *  Light enterprise look: copper solid, white outline, quiet ghost. */

import type { ButtonHTMLAttributes, ReactNode } from "react";

export type ButtonVariant = "primary" | "default" | "ghost" | "danger" | "sage" | "outline";
export type ButtonSize = "sm" | "md" | "lg";

const BUTTON_VARIANT: Record<ButtonVariant, string> = {
  primary:
    "bg-copper text-white border border-copper shadow-sm hover:bg-copper-dark hover:border-copper-dark active:bg-copper-deep",
  default:
    "bg-surface text-ink border border-line hover:border-copper/50 hover:text-copper-dark shadow-sm",
  outline:
    "bg-transparent text-copper-dark border border-copper/45 hover:bg-copper-soft",
  ghost: "bg-transparent border-transparent text-muted hover:bg-raised-soft hover:text-ink",
  danger:
    "bg-surface text-danger border border-danger/35 hover:bg-danger-soft hover:border-danger/50",
  sage: "bg-sage text-white border border-sage hover:opacity-90",
};

const BUTTON_SIZE: Record<ButtonSize, string> = {
  sm: "h-8 px-3 text-[13px] gap-1.5 rounded-md",
  md: "h-9 px-3.5 text-sm gap-2 rounded-lg",
  lg: "h-11 px-5 text-sm gap-2 rounded-full",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  children?: ReactNode;
}

export function Button({
  children,
  variant = "default",
  size = "md",
  className = "",
  type = "button",
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      className={`inline-flex items-center justify-center font-medium transition-colors disabled:opacity-50 disabled:pointer-events-none focus-visible:outline-2 ${BUTTON_VARIANT[variant]} ${BUTTON_SIZE[size]} ${className}`}
      {...rest}
    >
      {children}
    </button>
  );
}

/** Square icon-only button with a comfortable touch target. */
export function IconButton({
  children,
  label,
  variant = "ghost",
  size = 36,
  className = "",
  ...rest
}: Omit<ButtonProps, "size" | "children"> & {
  label: string;
  size?: number;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      style={{ width: size, height: size }}
      className={`inline-flex shrink-0 items-center justify-center rounded-lg border transition-colors disabled:opacity-50 disabled:pointer-events-none ${BUTTON_VARIANT[variant]} ${className}`}
      {...rest}
    >
      {children}
    </button>
  );
}
