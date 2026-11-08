import { create } from "zustand";
import type { ChatArtifact } from "../lib/artifacts";

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
  floatingExpanded: boolean;
  openFloatingChat: (sessionId?: string | null) => void;
  closeFloatingChat: () => void;
  setFloatingSession: (sessionId: string) => void;
  setFloatingExpanded: (expanded: boolean) => void;
  thinkingPanelExpanded: boolean;
  setThinkingPanelExpanded: (expanded: boolean) => void;
  thinkingTimelineExpanded: boolean;
  setThinkingTimelineExpanded: (expanded: boolean) => void;
  commandPaletteOpen: boolean;
  setCommandPaletteOpen: (open: boolean) => void;
  toggleCommandPalette: () => void;
  artifact: ChatArtifact | null;
  openArtifact: (artifact: ChatArtifact) => void;
  closeArtifact: () => void;
  toasts: Toast[];
  pushToast: (toast: Omit<Toast, "id">) => void;
  dismissToast: (id: number) => void;
}

const SIDEBAR_KEY = "eurus.ui.sidebarCollapsed";
const THINKING_PANEL_KEY = "eurus.ui.thinkingPanelExpanded";
const THINKING_TIMELINE_KEY = "eurus.ui.thinkingTimelineExpanded";

function readPreference(key: string, fallback: boolean): boolean {
  try {
    const value = localStorage.getItem(key);
    return value === null ? fallback : value === "1";
  } catch {
    return fallback;
  }
}

function writePreference(key: string, value: boolean): void {
  try {
    localStorage.setItem(key, value ? "1" : "0");
  } catch {
    return;
  }
}

let toastSeq = 1;

export const useUI = create<UIState>((set, get) => ({
  sidebarCollapsed: readPreference(SIDEBAR_KEY, false),
  toggleSidebar: () => {
    const next = !get().sidebarCollapsed;
    writePreference(SIDEBAR_KEY, next);
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
  thinkingPanelExpanded: readPreference(THINKING_PANEL_KEY, true),
  setThinkingPanelExpanded: (expanded) => {
    writePreference(THINKING_PANEL_KEY, expanded);
    set({ thinkingPanelExpanded: expanded });
  },
  thinkingTimelineExpanded: readPreference(THINKING_TIMELINE_KEY, false),
  setThinkingTimelineExpanded: (expanded) => {
    writePreference(THINKING_TIMELINE_KEY, expanded);
    set({ thinkingTimelineExpanded: expanded });
  },
  commandPaletteOpen: false,
  setCommandPaletteOpen: (open) => set({ commandPaletteOpen: open }),
  toggleCommandPalette: () => set((state) => ({ commandPaletteOpen: !state.commandPaletteOpen })),
  artifact: null,
  openArtifact: (artifact) => set({ artifact }),
  closeArtifact: () => set({ artifact: null }),
  toasts: [],
  pushToast: (toast) => {
    const id = toastSeq++;
    set((state) => ({ toasts: [...state.toasts.slice(-3), { ...toast, id }] }));
    window.setTimeout(() => get().dismissToast(id), toast.tone === "danger" ? 7000 : 4500);
  },
  dismissToast: (id) => set((state) => ({ toasts: state.toasts.filter((item) => item.id !== id) })),
}));
