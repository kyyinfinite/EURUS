/** Topbar — 68px global bar: global search (⌘K), live notifications,
 *  connection status and the operator menu. Real data only: notifications
 *  are pending approvals + gateway state; no fabricated counters. */

import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { Bell, ChevronDown, CircleCheck, Lock, Menu, Search, ShieldAlert } from "lucide-react";
import { api } from "../../lib/api";
import { formatUptime } from "../../lib/format";
import { useGateway } from "../../store/gateway";
import { useUI } from "../../store/ui";
import { EurusMark } from "../Logo";
import {
  Badge,
  Dropdown,
  DropdownContent,
  DropdownItem,
  DropdownLabel,
  DropdownSeparator,
  DropdownTrigger,
  IconButton,
  PopoverControl,
  StatusDot,
} from "../ui";
import { forgetToken } from "../../lib/token";

function SearchTrigger({ onClick, className = "" }: { onClick: () => void; className?: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label="Open global search"
      className={`flex h-10 w-full items-center gap-2.5 rounded-full border border-line bg-canvas px-4 text-left text-[13px] text-faint transition-colors hover:border-copper/40 hover:text-muted ${className}`}
    >
      <Search size={15} />
      <span className="min-w-0 flex-1 truncate">Search anything…</span>
      <kbd className="hidden shrink-0 rounded-md border border-line bg-surface px-1.5 py-0.5 font-mono text-[11px] text-muted sm:inline">
        ⌘K
      </kbd>
    </button>
  );
}

function NotificationsBell() {
  const approvals = useGateway((state) => state.approvals);
  const connected = useGateway((state) => state.connected);
  const count = approvals.length + (connected ? 0 : 1);
  return (
    <PopoverControl
      ariaLabel="Notifications"
      trigger={({ open }) => (
        <button
          type="button"
          aria-label={`Notifications${count > 0 ? ` (${count} pending)` : ""}`}
          className={`relative flex h-10 w-10 items-center justify-center rounded-full border transition-colors ${
            open
              ? "border-copper/40 bg-copper-soft text-copper-dark"
              : "border-line bg-surface text-muted hover:border-copper/40 hover:text-ink"
          }`}
        >
          <Bell size={17} />
          {count > 0 && (
            <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full border-2 border-surface bg-copper px-0.5 text-[10px] font-bold text-white">
              {count}
            </span>
          )}
        </button>
      )}
    >
      {(close) => (
        <div className="w-[320px] max-w-[calc(100vw-2rem)]">
          <div className="flex items-center justify-between border-b border-line-soft px-4 py-3">
            <p className="text-sm font-semibold text-ink">Notifications</p>
            <Badge tone={connected ? "ok" : "danger"}>{connected ? "live" : "offline"}</Badge>
          </div>
          <div className="max-h-[320px] overflow-y-auto p-2">
            {approvals.length === 0 && connected && (
              <div className="flex flex-col items-center gap-2 px-4 py-8 text-center">
                <CircleCheck size={22} className="text-ok" />
                <p className="text-[13px] font-medium text-ink">All clear</p>
                <p className="text-xs text-muted">
                  Approval requests and gateway alerts appear here.
                </p>
              </div>
            )}
            {approvals.map((approval) => (
              <Link
                key={approval.token}
                to="/approvals"
                onClick={() => close()}
                className="flex items-start gap-3 rounded-lg px-3 py-2.5 transition-colors hover:bg-warn-soft"
              >
                <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-warn-soft text-warn">
                  <ShieldAlert size={15} />
                </span>
                <span className="min-w-0">
                  <span className="block truncate text-[13px] font-medium text-ink">
                    Approval required — {approval.tool_name}
                  </span>
                  <span className="block text-xs text-muted">
                    expires in {approval.expires_in_seconds}s · decision needed
                  </span>
                </span>
              </Link>
            ))}
            {!connected && (
              <div className="flex items-start gap-3 rounded-lg bg-danger-soft px-3 py-2.5">
                <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-danger-soft text-danger">
                  <StatusDot tone="danger" pulse />
                </span>
                <span className="min-w-0">
                  <span className="block text-[13px] font-medium text-ink">Gateway reconnecting</span>
                  <span className="block text-xs text-muted">
                    The event stream dropped; retrying automatically.
                  </span>
                </span>
              </div>
            )}
          </div>
        </div>
      )}
    </PopoverControl>
  );
}

function OperatorMenu() {
  const navigate = useNavigate();
  const healthQuery = useQuery({
    queryKey: ["health"],
    queryFn: () => api.get<{ version: string; uptime_seconds: number }>("/api/health"),
    refetchInterval: 30_000,
  });
  const lock = () => {
    forgetToken();
    window.location.reload();
  };
  return (
    <Dropdown>
      <DropdownTrigger asChild>
        <button
          type="button"
          aria-label="Account menu"
          className="flex h-10 items-center gap-1.5 rounded-full border border-line bg-surface pl-1 pr-2.5 transition-colors hover:border-copper/40"
        >
          <span className="flex h-8 w-8 items-center justify-center rounded-full bg-copper text-xs font-bold text-white">
            EO
          </span>
          <ChevronDown size={14} className="text-muted" />
        </button>
      </DropdownTrigger>
      <DropdownContent className="w-[250px]">
        <DropdownLabel>Gateway session</DropdownLabel>
        <div className="px-2.5 pb-2 pt-1">
          <p className="text-[13px] font-semibold text-ink">Local Operator</p>
          <p className="mt-0.5 text-xs text-muted">
            {healthQuery.data
              ? `EURUS v${healthQuery.data.version} · up ${formatUptime(healthQuery.data.uptime_seconds)}`
              : "EURUS web gateway"}
          </p>
        </div>
        <DropdownSeparator />
        <DropdownItem icon={<ShieldAlert size={15} />} onSelect={() => navigate("/security")}>
          Access &amp; Security
        </DropdownItem>
        <DropdownItem icon={<Lock size={15} />} tone="danger" onSelect={lock}>
          Lock workspace
        </DropdownItem>
      </DropdownContent>
    </Dropdown>
  );
}

export function Topbar() {
  const setMobileNavOpen = useUI((state) => state.setMobileNavOpen);
  const setPaletteOpen = useUI((state) => state.setCommandPaletteOpen);
  const connected = useGateway((state) => state.connected);
  return (
    <header className="flex h-[68px] shrink-0 items-center gap-3 border-b border-line bg-surface px-4 safe-top sm:px-5">
      {/* mobile menu */}
      <IconButton
        label="Open navigation"
        onClick={() => setMobileNavOpen(true)}
        size={40}
        className="border-transparent md:hidden"
      >
        <Menu size={19} className="text-dim" />
      </IconButton>

      {/* brand on mobile */}
      <div className="flex items-center gap-2 md:hidden">
        <EurusMark size={26} />
        <span className="text-sm font-bold tracking-[0.28em] text-ink">EURUS</span>
      </div>

      {/* global search (desktop) */}
      <div className="mx-auto hidden w-full max-w-2xl md:block">
        <SearchTrigger onClick={() => setPaletteOpen(true)} />
      </div>

      <div className="ml-auto flex items-center gap-2.5">
        {/* compact search icon (mobile) */}
        <IconButton
          label="Search"
          onClick={() => setPaletteOpen(true)}
          size={40}
          className="border-line bg-surface text-muted md:hidden"
        >
          <Search size={17} />
        </IconButton>

        {/* connection pill */}
        <span className="hidden items-center gap-2 rounded-full border border-line bg-surface px-3 py-1.5 text-xs font-medium text-muted lg:flex">
          <StatusDot tone={connected ? "ok" : "danger"} pulse={!connected} />
          {connected ? "Gateway Online" : "Reconnecting"}
        </span>

        <NotificationsBell />
        <OperatorMenu />
      </div>
    </header>
  );
}
