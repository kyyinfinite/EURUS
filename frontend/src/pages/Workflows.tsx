/** Workflows — repeatable task pipelines.
 *
 *  The gateway runtime does not expose a workflow registry yet (no endpoint
 *  exists for stored or scheduled workflows). Per the workspace brief, this
 *  page documents the required backend contract instead of faking data.
 */

import { Workflow, ArrowRight } from "lucide-react";
import { Link } from "react-router-dom";
import { Card, EmptyState, PageHeader } from "../components/ui";

const REQUIRED_ENDPOINTS: { method: string; path: string; purpose: string; schema: string }[] = [
  {
    method: "GET",
    path: "/api/workflows",
    purpose: "List stored workflow definitions",
    schema: "WorkflowView[]: { id, name, description, steps: [{ tool, args_schema }], created_at, updated_at }",
  },
  {
    method: "POST",
    path: "/api/workflows",
    purpose: "Create a workflow from a verified run pattern",
    schema: "{ name, description, steps[] } → 201 WorkflowView",
  },
  {
    method: "POST",
    path: "/api/workflows/{id}/run",
    purpose: "Execute a workflow as a canonical run",
    schema: "{ inputs: Record<string, unknown> } → 202 { run_id } (same run contract as chat)",
  },
  {
    method: "GET",
    path: "/api/schedules",
    purpose: "List scheduled executions (the runtime already has a scheduler)",
    schema: "ScheduleView[]: { id, workflow_id | prompt, cron, next_run_at, last_run_id }",
  },
];

export default function Workflows() {
  return (
    <div className="h-full overflow-y-auto">
      <PageHeader
        title="Workflows"
        icon={<Workflow size={20} />}
        subtitle="Repeatable pipelines over the canonical run engine — in design"
      />
      <div className="space-y-5 px-4 pb-28 sm:px-6 md:pb-12 lg:px-8">
        <EmptyState
          icon={<Workflow size={26} />}
          title="Workflows are not part of the gateway yet"
          description="No workflow registry or scheduling endpoint exists in the backend today, so this page deliberately shows no demo data. Below is the exact contract this module needs — once the endpoints ship, the UI plugs straight into the canonical run system."
          action={
            <Link
              to="/home"
              className="inline-flex h-10 items-center gap-2 rounded-full bg-copper px-5 text-sm font-semibold text-white shadow-launcher transition-colors hover:bg-copper-dark"
            >
              Ask EURUS to build one <ArrowRight size={14} />
            </Link>
          }
        />

        <Card title="Required backend contract" icon={<Workflow size={16} className="text-copper" />}>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-line text-left">
                  <th className="pb-2.5 pr-4 text-[11px] font-semibold uppercase tracking-[0.1em] text-muted">
                    Method
                  </th>
                  <th className="pb-2.5 pr-4 text-[11px] font-semibold uppercase tracking-[0.1em] text-muted">
                    Endpoint
                  </th>
                  <th className="pb-2.5 pr-4 text-[11px] font-semibold uppercase tracking-[0.1em] text-muted">
                    Purpose
                  </th>
                  <th className="pb-2.5 text-[11px] font-semibold uppercase tracking-[0.1em] text-muted">
                    Schema
                  </th>
                </tr>
              </thead>
              <tbody>
                {REQUIRED_ENDPOINTS.map((endpoint) => (
                  <tr key={endpoint.path} className="border-b border-line-soft last:border-b-0">
                    <td className="py-3 pr-4">
                      <span className="rounded-md bg-copper-soft px-1.5 py-0.5 font-mono text-2xs font-semibold text-copper-dark">
                        {endpoint.method}
                      </span>
                    </td>
                    <td className="py-3 pr-4 font-mono text-[13px] text-ink">{endpoint.path}</td>
                    <td className="py-3 pr-4 text-[13px] text-muted">{endpoint.purpose}</td>
                    <td className="py-3 font-mono text-2xs leading-relaxed text-muted">
                      {endpoint.schema}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-4 rounded-lg bg-raised-soft/60 px-3.5 py-2.5 text-xs leading-relaxed text-muted">
            Design note: workflow execution should reuse the canonical run store, event stream and
            approval boundary — a workflow step that mutates state must pass the same approval gate
            as a chat-initiated tool call.
          </p>
        </Card>
      </div>
    </div>
  );
}
