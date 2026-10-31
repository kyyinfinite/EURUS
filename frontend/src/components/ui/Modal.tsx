/** Modal — Radix Dialog based (focus trap, esc, scroll lock, aria). */

import type { ReactNode } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { IconButton } from "./Button";

export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  wide = false,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={(next) => !next && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-[80] animate-fade-in bg-ink/45" />
        <Dialog.Content
          className={`fixed left-1/2 top-1/2 z-[81] w-[calc(100vw-2rem)] max-h-[86dvh] -translate-x-1/2 -translate-y-1/2 flex flex-col overflow-hidden rounded-2xl border border-line bg-surface shadow-overlay animate-scale-in focus:outline-none ${
            wide ? "max-w-3xl" : "max-w-lg"
          }`}
        >
          <div className="flex items-center justify-between gap-3 border-b border-line-soft px-5 py-4">
            <Dialog.Title className="text-[15px] font-semibold text-ink">{title}</Dialog.Title>
            <Dialog.Close asChild>
              <IconButton label="Close dialog" size={32}>
                <X size={15} className="text-muted" />
              </IconButton>
            </Dialog.Close>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
          {footer && (
            <footer className="border-t border-line-soft bg-canvas/60 px-5 py-3.5">{footer}</footer>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
