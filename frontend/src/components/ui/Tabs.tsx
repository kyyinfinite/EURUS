/** Tabs — Radix based segmented control with the light pill style. */

import type { ReactNode } from "react";
import * as TabsPrimitive from "@radix-ui/react-tabs";

export const Tabs = TabsPrimitive.Root;

export function TabsList({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <TabsPrimitive.List
      className={`inline-flex items-center gap-1 rounded-xl border border-line bg-raised-soft p-1 ${className}`}
    >
      {children}
    </TabsPrimitive.List>
  );
}

export function TabsTrigger({
  value,
  children,
  count,
}: {
  value: string;
  children: ReactNode;
  count?: number;
}) {
  return (
    <TabsPrimitive.Trigger
      value={value}
      className="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[13px] font-medium text-muted transition-colors hover:text-ink focus:outline-none data-[state=active]:bg-surface data-[state=active]:text-ink data-[state=active]:shadow-sm"
    >
      {children}
      {typeof count === "number" && count > 0 && (
        <span className="rounded-full bg-copper/10 px-1.5 font-mono text-2xs text-copper-dark">
          {count}
        </span>
      )}
    </TabsPrimitive.Trigger>
  );
}

export function TabsContent({
  value,
  children,
  className = "",
}: {
  value: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <TabsPrimitive.Content value={value} className={`mt-4 focus:outline-none ${className}`}>
      {children}
    </TabsPrimitive.Content>
  );
}
