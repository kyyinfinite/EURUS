/** UI shell state: sidebar, mobile navigation, floating chat, toasts. */

import { create } from "zustand";

export type ToastTone = "info" | "ok" | "warn" | "danger";

export interface Toast {
  id: number;
  tone: ToastTone;
  title: string;
  detail?: string;
}

interface UIState {
  sidebarCollapsed: boolean;
  toggleSidebar: () => void;

  mobileNavOpen: boolean;
  setMobileNavOpen: (open: boolean) => void;

  contextPanelOpen: boolean;
  setContextPanelOpen: (open: boolean) => void;
  toggleContextPanel: () => void;

  floatingChatOpen: boolean;
  floatingSessionId: string | null;
  /** mobile floating chat renders as a bottom sheet; user may expand it */
  floatingExpanded: boolean;
  openFloatingChat: (sessionId?: string | null) => void;
  closeFloatingChat: () => void;
  setFloatingSession: (sessionId: string) => void;
  setFloatingExpanded: (expanded: boolean) => void;

  commandPaletteOpen: boolean;
  setCommandPaletteOpen: (open: boolean) => void;
  toggleCommandPalette: () => void;

  toasts: Toast[];
  pushToast: (toast: Omit<Toast, "id">) => void;
  dismissToast: (id: number) => void;
}

const SIDEBAR_KEY = "eurus.ui.sidebarCollapsed";

function readSidebarPreference(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_KEY) === "1";
  } catch {
    return false;
  }
}

let toastSeq = 1;

export const useUI = create<UIState>((set, get) => ({
  sidebarCollapsed: readSidebarPreference(),
  toggleSidebar: () => {
    const next = !get().sidebarCollapsed;
    try {
      localStorage.setItem(SIDEBAR_KEY, next ? "1" : "0");
    } catch {
      /* preference is best-effort */
    }
    set({ sidebarCollapsed: next });
  },

  mobileNavOpen: false,
  setMobileNavOpen: (open) => set({ mobileNavOpen: open }),

  contextPanelOpen: true,
  setContextPanelOpen: (open) => set({ contextPanelOpen: open }),
  toggleContextPanel: () => set({ contextPanelOpen: !get().contextPanelOpen }),

  floatingChatOpen: false,
  floatingSessionId: null,
  floatingExpanded: false,
  openFloatingChat: (sessionId) =>
    set((state) => ({
      floatingChatOpen: true,
      floatingSessionId: sessionId ?? state.floatingSessionId,
    })),
  closeFloatingChat: () => set({ floatingChatOpen: false, floatingExpanded: false }),
  setFloatingSession: (sessionId) => set({ floatingSessionId: sessionId }),
  setFloatingExpanded: (expanded) => set({ floatingExpanded: expanded }),

  commandPaletteOpen: false,
  setCommandPaletteOpen: (open) => set({ commandPaletteOpen: open }),
  toggleCommandPalette: () => set((state) => ({ commandPaletteOpen: !state.commandPaletteOpen })),

  toasts: [],
  pushToast: (toast) => {
    const id = toastSeq++;
    set({ toasts: [...get().toasts.slice(-3), { ...toast, id }] });
    window.setTimeout(() => {
      get().dismissToast(id);
    }, toast.tone === "danger" ? 7000 : 4500);
  },
  dismissToast: (id) => set({ toasts: get().toasts.filter((item) => item.id !== id) }),
}));
