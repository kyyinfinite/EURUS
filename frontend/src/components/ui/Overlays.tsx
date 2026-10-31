/** Accessible overlays: Tooltip, Popover, DropdownMenu (Radix). */

import type { ReactNode } from "react";
import { useState } from "react";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import * as PopoverPrimitive from "@radix-ui/react-popover";
import * as DropdownPrimitive from "@radix-ui/react-dropdown-menu";

/* ---- tooltip ---------------------------------------------------------------- */

export const TooltipProvider = TooltipPrimitive.Provider;

export function Tooltip({
  label,
  children,
  side = "right",
}: {
  label: string;
  children: ReactNode;
  side?: "top" | "right" | "bottom" | "left";
}) {
  if (!label) return <>{children}</>;
  return (
    <TooltipPrimitive.Root delayDuration={250}>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          side={side}
          sideOffset={6}
          className="z-[95] max-w-[220px] rounded-md bg-ink px-2.5 py-1.5 text-xs font-medium text-canvas shadow-float"
        >
          {label}
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}

/* ---- popover ------------------------------------------------------------------ */

export function Popover({
  trigger,
  children,
  align = "end",
  className = "",
  ariaLabel,
}: {
  trigger: ReactNode;
  children: ReactNode | ((close: () => void) => ReactNode);
  align?: "start" | "center" | "end";
  className?: string;
  ariaLabel?: string;
}) {
  return (
    <PopoverPrimitive.Root>
      <PopoverPrimitive.Trigger asChild aria-label={ariaLabel}>
        {trigger}
      </PopoverPrimitive.Trigger>
      <PopoverPrimitive.Portal>
        <PopoverPrimitive.Content
          align={align}
          sideOffset={10}
          className={`z-[90] rounded-xl border border-line bg-surface shadow-overlay focus:outline-none ${className}`}
        >
          {typeof children === "function" ? children(() => void 0) : children}
        </PopoverPrimitive.Content>
      </PopoverPrimitive.Portal>
    </PopoverPrimitive.Root>
  );
}

/** Render-prop variant that exposes close() for item clicks. */
export function PopoverControl({
  trigger,
  children,
  align = "end",
  className = "",
  ariaLabel,
}: {
  trigger: (props: { open: boolean }) => ReactNode;
  children: (close: () => void) => ReactNode;
  align?: "start" | "center" | "end";
  className?: string;
  ariaLabel?: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <PopoverPrimitive.Root open={open} onOpenChange={setOpen}>
      <PopoverPrimitive.Trigger asChild aria-label={ariaLabel}>
        {trigger({ open })}
      </PopoverPrimitive.Trigger>
      <PopoverPrimitive.Portal>
        <PopoverPrimitive.Content
          align={align}
          sideOffset={10}
          className={`z-[90] rounded-xl border border-line bg-surface shadow-overlay focus:outline-none ${className}`}
        >
          {children(() => setOpen(false))}
        </PopoverPrimitive.Content>
      </PopoverPrimitive.Portal>
    </PopoverPrimitive.Root>
  );
}

/* ---- dropdown menu --------------------------------------------------------------- */

export const Dropdown = DropdownPrimitive.Root;
export const DropdownTrigger = DropdownPrimitive.Trigger;

export function DropdownContent({
  children,
  align = "end",
  className = "",
}: {
  children: ReactNode;
  align?: "start" | "center" | "end";
  className?: string;
}) {
  return (
    <DropdownPrimitive.Portal>
      <DropdownPrimitive.Content
        align={align}
        sideOffset={8}
        className={`z-[90] min-w-[190px] overflow-hidden rounded-xl border border-line bg-surface p-1.5 shadow-overlay focus:outline-none animate-scale-in ${className}`}
      >
        {children}
      </DropdownPrimitive.Content>
    </DropdownPrimitive.Portal>
  );
}

export function DropdownItem({
  children,
  onSelect,
  disabled,
  tone = "default",
  icon,
  shortcut,
}: {
  children: ReactNode;
  onSelect?: () => void;
  disabled?: boolean;
  tone?: "default" | "danger";
  icon?: ReactNode;
  shortcut?: string;
}) {
  return (
    <DropdownPrimitive.Item
      disabled={disabled}
      onSelect={(event) => {
        event.preventDefault();
        onSelect?.();
      }}
      className={`flex cursor-pointer select-none items-center gap-2.5 rounded-lg px-2.5 py-2 text-[13px] font-medium outline-none transition-colors ${
        tone === "danger"
          ? "text-danger focus:bg-danger-soft"
          : "text-ink focus:bg-raised-soft"
      } data-[disabled]:pointer-events-none data-[disabled]:opacity-50`}
    >
      {icon && <span className="shrink-0 text-muted">{icon}</span>}
      <span className="min-w-0 flex-1 truncate">{children}</span>
      {shortcut && <span className="shrink-0 font-mono text-2xs text-faint">{shortcut}</span>}
    </DropdownPrimitive.Item>
  );
}

export function DropdownLabel({ children }: { children: ReactNode }) {
  return (
    <DropdownPrimitive.Label className="px-2.5 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-[0.14em] text-faint">
      {children}
    </DropdownPrimitive.Label>
  );
}

export function DropdownSeparator() {
  return <DropdownPrimitive.Separator className="my-1.5 h-px bg-line-soft" />;
}
