/** Approval Center — pending approvals with fast, decisive actions. */

import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ShieldAlert, X } from "lucide-react";
import { api } from "../lib/api";
import type { ApprovalView } from "../lib/types";
import { useGateway } from "../store/gateway";
import { useUI } from "../store/ui";
import { Badge, Button, Card, Empty, ErrorNote, LoadingState, PageHeader, useCountdown } from "../components/ui";

export default function Approvals() {
  const queryClient = useQueryClient();
  const liveApprovals = useGateway((state) => state.approvals);
  const setApprovals = useGateway((state) => state.setApprovals);

  const query = useQuery({
    queryKey: ["approvals"],
    queryFn: () => api.get<ApprovalView[]>("/api/approvals"),
    refetchInterval: 4000,
  });

  useEffect(() => {
    if (query.data) setApprovals(query.data);
  }, [query.data, setApprovals]);

  const approvals = query.data ?? liveApprovals;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Approval Center"
        subtitle="Mutating tool executions wait here until you decide — fail-closed, one authoritative decision"
      />
      <div className="min-h-0 flex-1 overflow-y-auto p-4 pb-28 sm:p-5 md:pb-12">
        {query.isLoading ? (
          <LoadingState label="Loading approvals…" />
        ) : approvals.length === 0 ? (
          <div className="mx-auto max-w-3xl pt-10">
            <Empty>
              No pending approvals. When the agent proposes a mutating action from the web
              runtime, it appears here, in Chat and in the floating panel.
            </Empty>
          </div>
        ) : (
          <div className="mx-auto max-w-3xl space-y-3">
            {approvals.map((approval) => (
              <ApprovalCard
                key={approval.token}
                approval={approval}
                onDone={() => queryClient.invalidateQueries({ queryKey: ["approvals"] })}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function ApprovalCard({ approval, onDone }: { approval: ApprovalView; onDone: () => void }) {
  const pushToast = useUI((state) => state.pushToast);
  const remaining = useCountdown(approval.expires_in_seconds);
  const resolve = useMutation({
    mutationFn: (allowed: boolean) =>
      api.post(`/api/approvals/${approval.token}/resolve`, {
        session_id: approval.session_id,
        decision: allowed ? "approved" : "denied",
      }),
    onSuccess: (_data, allowed) => {
      pushToast({
        tone: allowed ? "ok" : "warn",
        title: allowed ? "Approved" : "Denied",
        detail: `${approval.tool_name} — ${allowed ? "the run continues" : "the tool call was blocked"}`,
      });
      onDone();
    },
  });

  let parsedArgs: unknown = approval.arguments_json;
  try {
    parsedArgs = JSON.parse(approval.arguments_json);
  } catch {
    /* keep raw */
  }

  const expiring = remaining <= 60;

  return (
    <Card className={expiring ? "border-danger/40" : "border-warn/40"}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-warn/40 bg-warn/10">
            <ShieldAlert size={16} className="text-warn" />
          </span>
          <div className="min-w-0">
            <div className="truncate font-mono text-sm text-warn">{approval.tool_name}</div>
            <div className="mt-0.5 truncate font-mono text-2xs text-faint">
              run {approval.run_id.slice(0, 8)} · session {approval.session_id.slice(0, 8)} · action{" "}
              {approval.fingerprint.slice(0, 12)}
            </div>
          </div>
        </div>
        <Badge tone={expiring ? "danger" : "warn"}>expires in {remaining}s</Badge>
      </div>
      <pre className="mt-3 max-h-48 overflow-auto whitespace-pre-wrap break-all rounded-lg border border-line-soft bg-canvas p-3 font-mono text-2xs text-dim">
        {typeof parsedArgs === "string" ? parsedArgs : JSON.stringify(parsedArgs, null, 2)}
        {approval.display_notice ? `\n${approval.display_notice}` : ""}
      </pre>
      {resolve.isError && (
        <div className="mt-2">
          <ErrorNote detail={resolve.error instanceof Error ? resolve.error.message : "resolve failed"} />
        </div>
      )}
      <div className="mt-3 flex flex-wrap gap-2">
        <Button variant="primary" onClick={() => resolve.mutate(true)} disabled={resolve.isPending}>
          <Check size={13} /> Approve once
        </Button>
        <Button variant="danger" onClick={() => resolve.mutate(false)} disabled={resolve.isPending}>
          <X size={13} /> Deny
        </Button>
        <span className="self-center text-2xs text-faint">
          Applies to this single tool call only — approvals never persist as blanket permission.
        </span>
      </div>
    </Card>
  );
}
