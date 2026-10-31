/** App — routing, token gate, global overlays. */

import { Suspense, lazy, useEffect, useState } from "react";
import { Route, Routes, useLocation } from "react-router-dom";
import { AppShell } from "./components/layout/AppShell";
import { FloatingChat } from "./components/chat/FloatingChat";
import { ToastViewport, LoadingState, Button, TooltipProvider } from "./components/ui";
import { CommandPalette } from "./components/navigation/CommandPalette";
import { bindSocket } from "./store/gateway";
import { useUI } from "./store/ui";
import { useHotkey } from "./hooks/useInteractions";
import { resolveToken } from "./lib/token";
import { EurusMark } from "./components/Logo";
import Dashboard from "./pages/Dashboard";
import Home from "./pages/Home";

// Route-level code splitting keeps the first paint small. Each route module
// is loaded through lazyPage() below.
function lazyPage(factory: () => Promise<{ default: React.ComponentType }>) {
  const Component = lazy(factory);
  return (
    <Suspense fallback={<LoadingState />}>
      <Component />
    </Suspense>
  );
}

function TokenGate({ children }: { children: React.ReactNode }) {
  const [token, setToken] = useState(resolveToken());

  if (token) return <>{children}</>;

  return (
    <div className="bg-atmosphere flex h-dvh items-center justify-center p-4">
      <form
        className="w-full max-w-sm animate-scale-in rounded-2xl border border-line bg-surface p-7 shadow-overlay"
        onSubmit={(event) => {
          event.preventDefault();
          const input = new FormData(event.currentTarget).get("token");
          if (typeof input === "string" && input.trim()) {
            localStorage.setItem("eurus.gateway.token", input.trim());
            setToken(input.trim());
            window.location.reload();
          }
        }}
      >
        <div className="flex flex-col items-center gap-3.5 text-center">
          <EurusMark size={44} />
          <div>
            <h1 className="text-base font-bold tracking-[0.32em] text-ink">EURUS</h1>
            <p className="mt-1.5 text-xs uppercase tracking-[0.22em] text-copper">
              AI Gateway Workspace
            </p>
          </div>
        </div>
        <p className="mt-6 text-sm leading-relaxed text-muted">
          This gateway is private. Paste the access token printed by{" "}
          <code className="font-mono text-[13px] text-copper-dark">eurus gateway web</code> (the part after{" "}
          <code className="font-mono text-[13px] text-copper-dark">?token=</code> in the ready URL).
        </p>
        <input
          name="token"
          type="password"
          placeholder="Access token"
          aria-label="Access token"
          className="mt-5 w-full font-mono text-sm"
          autoFocus
        />
        <Button type="submit" variant="primary" size="lg" className="mt-4 w-full justify-center">
          Unlock workspace
        </Button>
      </form>
    </div>
  );
}

function FloatingChatGate() {
  const location = useLocation();
  // The full Chat workspace already provides chat; everywhere else the
  // floating entry point stays available (welcome screen included).
  if (location.pathname.startsWith("/chat")) return null;
  return <FloatingChat />;
}

export default function App() {
  const togglePalette = useUI((state) => state.toggleCommandPalette);

  useEffect(() => {
    if (!resolveToken()) return;
    return bindSocket();
  }, []);

  useEffect(() => {
    const handler = () => {
      // Force a reload so the token gate re-renders with a clean state.
      window.location.reload();
    };
    window.addEventListener("eurus:unauthorized", handler);
    return () => window.removeEventListener("eurus:unauthorized", handler);
  }, []);

  useHotkey({ key: "k", meta: true }, togglePalette, { allowInInputs: true });

  return (
    <TokenGate>
      <TooltipProvider>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="/" element={<Dashboard />} />
            <Route path="/home" element={<Home />} />
            <Route path="/chat" element={lazyPage(() => import("./pages/Chat"))} />
            <Route path="/chat/:sessionId" element={lazyPage(() => import("./pages/Chat"))} />
            <Route path="/sessions" element={lazyPage(() => import("./pages/Sessions"))} />
            <Route path="/files" element={lazyPage(() => import("./pages/Files"))} />
            <Route path="/agents" element={lazyPage(() => import("./pages/Agents"))} />
            <Route path="/workflows" element={lazyPage(() => import("./pages/Workflows"))} />
            <Route path="/analytics" element={lazyPage(() => import("./pages/Analytics"))} />
            <Route path="/runtime" element={lazyPage(() => import("./pages/Runtime"))} />
            <Route path="/approvals" element={lazyPage(() => import("./pages/Approvals"))} />
            <Route path="/providers" element={lazyPage(() => import("./pages/Providers"))} />
            <Route path="/tools" element={lazyPage(() => import("./pages/Tools"))} />
            <Route path="/memory" element={lazyPage(() => import("./pages/Memory"))} />
            <Route path="/security" element={lazyPage(() => import("./pages/Security"))} />
            <Route path="/config" element={lazyPage(() => import("./pages/Configuration"))} />
            <Route path="/logs" element={lazyPage(() => import("./pages/Logs"))} />
            <Route path="*" element={<Dashboard />} />
          </Route>
        </Routes>
        <FloatingChatGate />
        <CommandPalette />
        <ToastViewport />
      </TooltipProvider>
    </TokenGate>
  );
}
