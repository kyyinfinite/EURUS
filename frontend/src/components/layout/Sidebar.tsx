/** Sidebar — desktop fixed rail (260px, collapsible to 72px icon rail) and the
 *  shared nav body reused by the mobile drawer. Light, spacious, copper
 *  active state with a soft tinted fill (reference dashboard style). */

import { NavLink } from "react-router-dom";
import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { useGateway } from "../../store/gateway";
import { useUI } from "../../store/ui";
import { BrandLockup } from "../Logo";
import { Badge, IconButton, StatusDot, Tooltip } from "../ui";
import { NAV_GROUPS } from "../navigation/nav";

export function SidebarNav({
  collapsed,
  onNavigate,
}: {
  collapsed: boolean;
  onNavigate?: () => void;
}) {
  const approvals = useGateway((state) => state.approvals);
  return (
    <nav className="flex-1 overflow-y-auto px-3 py-4" aria-label="Primary">
      {NAV_GROUPS.map((group) => (
        <div key={group.name} className="mb-5 last:mb-1">
          {!collapsed ? (
            <p className="mb-2 px-3 text-[11px] font-semibold uppercase tracking-[0.14em] text-faint">
              {group.name}
            </p>
          ) : (
            <div className="mx-auto mb-3 h-px w-6 bg-line" aria-hidden="true" />
          )}
          <ul className="space-y-0.5">
            {group.items.map((item) => (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  end={item.end}
                  onClick={onNavigate}
                  className={({ isActive }) =>
                    `group relative flex items-center rounded-lg transition-colors ${
                      collapsed ? "justify-center px-0 py-2.5" : "gap-3 px-3 py-2.5"
                    } ${isActive
                      ? "bg-[#F5E9DC] font-semibold text-copper-dark"
                      : "text-dim hover:bg-raised-soft hover:text-ink"
                    }`
                  }
                >
                  {({ isActive }) => (
                    <>
                      {isActive && !collapsed && (
                        <span
                          className="absolute left-0 top-1/2 h-7 w-1.5 -translate-y-1/2 rounded-r-full bg-copper"
                          aria-hidden="true"
                        />
                      )}
                      <Tooltip label={collapsed ? item.label : ""}>
                        <item.icon
                          size={collapsed ? 19 : 17}
                          strokeWidth={1.9}
                          className={collapsed ? "" : "shrink-0"}
                        />
                      </Tooltip>
                      {!collapsed && <span className="truncate text-sm">{item.label}</span>}
                      {!collapsed && item.badge === "approvals" && approvals.length > 0 && (
                        <span className="ml-auto">
                          <Badge tone="warn">{approvals.length}</Badge>
                        </span>
                      )}
                      {collapsed && item.badge === "approvals" && approvals.length > 0 && (
                        <span className="absolute right-2 top-2 h-2 w-2 rounded-full bg-warn" />
                      )}
                    </>
                  )}
                </NavLink>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </nav>
  );
}

/** Bottom of the sidebar: gateway status card + operator row. */
export function SidebarFooter({ collapsed }: { collapsed: boolean }) {
  const connected = useGateway((state) => state.connected);
  if (collapsed) {
    return (
      <div className="flex justify-center border-t border-line px-2 py-3">
        <Tooltip label={connected ? "Gateway online" : "Reconnecting…"}>
          <StatusDot tone={connected ? "ok" : "danger"} pulse={!connected} />
        </Tooltip>
      </div>
    );
  }
  return (
    <div className="border-t border-line px-4 py-4">
      <div className="rounded-xl border border-line bg-surface p-3.5">
        <div className="flex items-center gap-2.5">
          <StatusDot tone={connected ? "ok" : "danger"} pulse={!connected} />
          <div className="min-w-0">
            <p className="truncate text-[13px] font-semibold text-ink">
              {connected ? "Gateway Online" : "Reconnecting…"}
            </p>
            <p className={`truncate text-xs ${connected ? "text-ok" : "text-warn"}`}>
              {connected ? "All systems operational" : "Restoring the event stream"}
            </p>
          </div>
        </div>
      </div>
      <div className="mt-3 flex items-center gap-2.5 px-1">
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-copper text-xs font-bold text-white">
          EO
        </span>
        <div className="min-w-0">
          <p className="truncate text-[13px] font-medium text-ink">Local Operator</p>
          <p className="truncate text-xs text-faint">Token-authenticated session</p>
        </div>
      </div>
    </div>
  );
}

export function DesktopSidebar() {
  const collapsed = useUI((state) => state.sidebarCollapsed);
  const toggle = useUI((state) => state.toggleSidebar);
  return (
    <aside
      className={`hidden shrink-0 flex-col border-r border-line bg-surface transition-[width] duration-200 md:flex ${
        collapsed ? "w-[72px]" : "w-[260px]"
      }`}
    >
      <div
        className={`flex h-[68px] items-center border-b border-line px-4 ${
          collapsed ? "justify-center px-0" : "justify-between"
        }`}
      >
        <BrandLockup collapsed={collapsed} />
        {!collapsed && (
          <IconButton label="Collapse sidebar" onClick={toggle} size={32} className="border-transparent">
            <PanelLeftClose size={16} className="text-faint" />
          </IconButton>
        )}
      </div>
      <SidebarNav collapsed={collapsed} />
      {collapsed && (
        <div className="flex justify-center border-t border-line py-2">
          <IconButton label="Expand sidebar" onClick={toggle} size={34} className="border-transparent">
            <PanelLeftOpen size={16} className="text-faint" />
          </IconButton>
        </div>
      )}
      <SidebarFooter collapsed={collapsed} />
    </aside>
  );
}
