/** Dashboard — the reference EURUS light dashboard, recreated with live
 *  gateway data: hero banner, session/run/approval/provider stat cards,
 *  system overview chart, recent activity, providers and system health.
 *  The floating EURUS assistant is mounted globally by the App shell.
 */

import { useQuery } from "@tanstack/react-query";
import { LayoutDashboard } from "lucide-react";
import { api } from "../lib/api";
import type { DashboardView } from "../lib/types";
import { useGateway } from "../store/gateway";
import { PageHeader, ErrorState, LoadingState } from "../components/ui";
import {
  HeroBanner,
  ProviderOverview,
  RecentActivity,
  StatCards,
  SystemHealth,
  SystemOverview,
} from "../components/dashboard/DashboardWidgets";

export default function Dashboard() {
  const connected = useGateway((state) => state.connected);
  const lastChange = useGateway((state) => state.lastSessionChange);

  const query = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => api.get<DashboardView>("/api/dashboard"),
    refetchInterval: 5000,
    enabled: connected || undefined,
  });
  void lastChange;

  return (
    <div className="h-full overflow-y-auto bg-canvas">
      <PageHeader
        title="Dashboard"
        icon={<LayoutDashboard size={20} />}
        subtitle="Your gateway at a glance — sessions, runs, approvals and provider posture"
      />
      <div className="space-y-5 px-4 pb-28 sm:px-6 md:pb-10 lg:px-8">
        {query.isLoading ? (
          <LoadingState label="Loading gateway overview…" />
        ) : query.isError || !query.data ? (
          <ErrorState
            detail="Dashboard unavailable"
            hint="The gateway could not be reached. Check that `eurus gateway web` is still running."
          />
        ) : (
          <>
            <HeroBanner />
            <StatCards data={query.data} />
            <div className="grid gap-5 xl:grid-cols-3">
              <div className="xl:col-span-2">
                <SystemOverview />
              </div>
              <RecentActivity runs={query.data.runtime.recent_runs} />
            </div>
            <div className="grid gap-5 xl:grid-cols-2">
              <ProviderOverview providers={query.data.diagnostics.providers ?? []} />
              <SystemHealth data={query.data} />
            </div>
          </>
        )}
      </div>
    </div>
  );
}
