/** ToastViewport — transient notifications (bottom-center mobile / bottom-right desktop). */

import { createPortal } from "react-dom";
import { AlertTriangle, CheckCircle2, Info, X, XCircle } from "lucide-react";
import { useUI, type Toast } from "../../store/ui";

const TOAST_ICON: Record<Toast["tone"], React.ReactNode> = {
  info: <Info size={16} className="text-copper" />,
  ok: <CheckCircle2 size={16} className="text-ok" />,
  warn: <AlertTriangle size={16} className="text-warn" />,
  danger: <XCircle size={16} className="text-danger" />,
};

export function ToastViewport() {
  const toasts = useUI((state) => state.toasts);
  const dismiss = useUI((state) => state.dismissToast);
  if (toasts.length === 0) return null;
  return createPortal(
    <div
      className="pointer-events-none fixed inset-x-0 bottom-[calc(var(--safe-bottom)+1rem)] z-[90] flex flex-col items-center gap-2 px-4 sm:items-end sm:pr-6"
      role="region"
      aria-label="notifications"
    >
      {toasts.map((toast) => (
        <div
          key={toast.id}
          role="status"
          className="pointer-events-auto flex w-full max-w-sm animate-toast-in items-start gap-3 rounded-xl border border-line bg-surface px-4 py-3.5 shadow-overlay"
        >
          <span className="mt-0.5 shrink-0">{TOAST_ICON[toast.tone]}</span>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-semibold text-ink">{toast.title}</p>
            {toast.detail && (
              <p className="mt-0.5 text-[13px] leading-relaxed text-muted">{toast.detail}</p>
            )}
          </div>
          <button
            type="button"
            aria-label="Dismiss notification"
            onClick={() => dismiss(toast.id)}
            className="shrink-0 text-faint transition-colors hover:text-ink"
          >
            <X size={14} />
          </button>
        </div>
      ))}
    </div>,
    document.body,
  );
}
