/** AppShell — the global application frame.
 *
 *  Desktop: fixed 260px sidebar (collapsible to a 72px icon rail, persisted
 *  preference) + a 68px topbar; the main area owns its own scrolling.
 *  Mobile: topbar with hamburger + slide-in navigation drawer with backdrop.
 */

import { useEffect } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { useGateway } from "../../store/gateway";
import { useUI } from "../../store/ui";
import { BrandLockup } from "../Logo";
import { Drawer } from "../ui";
import { DesktopSidebar, SidebarFooter, SidebarNav } from "./Sidebar";
import { Topbar } from "./Topbar";

function MobileNavDrawer() {
  const open = useUI((state) => state.mobileNavOpen);
  const setOpen = useUI((state) => state.setMobileNavOpen);
  const location = useLocation();

  useEffect(() => {
    setOpen(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname]);

  return (
    <Drawer open={open} onClose={() => setOpen(false)} title={<BrandLockup />} widthClass="w-[290px]">
      <div className="flex h-full flex-col">
        <SidebarNav collapsed={false} onNavigate={() => setOpen(false)} />
        <SidebarFooter collapsed={false} />
      </div>
    </Drawer>
  );
}

export function AppShell() {
  const connected = useGateway((state) => state.connected);
  void connected;
  return (
    <div className="flex h-dvh overflow-hidden bg-canvas">
      <DesktopSidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <Topbar />
        <main className="min-h-0 min-w-0 flex-1 overflow-hidden">
          <Outlet />
        </main>
      </div>
      <MobileNavDrawer />
    </div>
  );
}
