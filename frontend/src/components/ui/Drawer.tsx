/** Drawer — Radix Dialog based side/bottom panel (mobile navigation,
 *  conversation lists, bottom sheets). */

import type { ReactNode } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { IconButton } from "./Button";

export function Drawer({
  open,
  onClose,
  side = "left",
  title,
  children,
  widthClass = "w-[300px]",
  showHandle = false,
}: {
  open: boolean;
  onClose: () => void;
  side?: "left" | "right" | "bottom";
  title?: ReactNode;
  children: ReactNode;
  widthClass?: string;
  showHandle?: boolean;
}) {
  const sideClass =
    side === "left"
      ? `left-0 top-0 h-full ${widthClass} animate-slide-in-left border-r`
      : side === "right"
        ? `right-0 top-0 h-full ${widthClass} animate-slide-in-right border-l`
        : `inset-x-0 bottom-0 max-h-[85dvh] animate-slide-up-sheet rounded-t-2xl border-t safe-bottom`;
  return (
    <Dialog.Root open={open} onOpenChange={(next) => !next && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-[70] animate-fade-in bg-ink/45" />
        <Dialog.Content
          className={`fixed z-[71] flex flex-col border-line bg-surface shadow-overlay focus:outline-none ${sideClass}`}
        >
          {showHandle && side === "bottom" && (
            <div className="flex justify-center pt-2.5" aria-hidden="true">
              <div className="h-1.5 w-10 rounded-full bg-line" />
            </div>
          )}
          {title && (
            <header className="flex items-center justify-between gap-3 border-b border-line-soft px-4 py-3.5">
              <Dialog.Title className="text-[15px] font-semibold text-ink">{title}</Dialog.Title>
              <Dialog.Close asChild>
                <IconButton label="Close panel" size={32}>
                  <X size={15} className="text-muted" />
                </IconButton>
              </Dialog.Close>
            </header>
          )}
          {!title && (
            <Dialog.Title className="sr-only">
              {side === "bottom" ? "Panel" : "Navigation panel"}
            </Dialog.Title>
          )}
          <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
