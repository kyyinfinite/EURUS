
import type { ButtonHTMLAttributes, ReactNode } from "react";

export type ButtonVariant = "primary" | "default" | "ghost" | "danger" | "sage" | "outline";
export type ButtonSize = "sm" | "md" | "lg";

const BUTTON_VARIANT: Record<ButtonVariant, string> = {
  primary:
    "bg-gradient-to-b from-copper to-copper-dark text-white border border-copper-dark shadow-[0_1px_2px_rgba(115,59,32,0.28),inset_0_1px_0_rgba(255,255,255,0.22)] hover:from-copper-dark hover:to-copper-deep hover:border-copper-deep active:shadow-[inset_0_1px_3px_rgba(94,47,26,0.4)]",
  default:
    "bg-surface text-ink border border-line shadow-[0_1px_2px_rgba(51,42,36,0.06),inset_0_1px_0_rgba(255,255,255,0.8)] hover:border-copper/50 hover:text-copper-dark hover:shadow-card active:shadow-[inset_0_1px_2px_rgba(51,42,36,0.1)]",
  outline:
    "bg-transparent text-copper-dark border border-copper/45 shadow-none hover:bg-copper-soft active:shadow-[inset_0_1px_2px_rgba(153,83,41,0.18)]",
  ghost:
    "bg-transparent border-transparent text-muted shadow-none hover:bg-raised-soft hover:text-ink active:bg-raised",
  danger:
    "bg-surface text-danger border border-danger/35 shadow-[0_1px_2px_rgba(51,42,36,0.05)] hover:bg-danger-soft hover:border-danger/50 active:shadow-[inset_0_1px_2px_rgba(179,38,30,0.14)]",
  sage:
    "bg-gradient-to-b from-sage to-[#4A6759] text-white border border-[#4A6759] shadow-[0_1px_2px_rgba(86,119,106,0.28),inset_0_1px_0_rgba(255,255,255,0.2)] hover:from-[#4A6759] hover:to-[#3D574B] active:shadow-[inset_0_1px_3px_rgba(61,87,75,0.4)]",
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
      className={`inline-flex items-center justify-center font-medium transition-[background,border-color,box-shadow,color,transform] duration-150 active:translate-y-px disabled:opacity-50 disabled:pointer-events-none disabled:active:translate-y-0 focus-visible:outline-2 ${BUTTON_VARIANT[variant]} ${BUTTON_SIZE[size]} ${className}`}
      {...rest}
    >
      {children}
    </button>
  );
}

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
      className={`inline-flex shrink-0 items-center justify-center rounded-lg border transition-[background,border-color,box-shadow,color,transform] duration-150 active:translate-y-px active:scale-[0.97] disabled:opacity-50 disabled:pointer-events-none ${BUTTON_VARIANT[variant]} ${className}`}
      {...rest}
    >
      {children}
    </button>
  );
}
