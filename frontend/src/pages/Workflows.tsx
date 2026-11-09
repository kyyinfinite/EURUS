
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, FlaskConical, Pause, Play, PlayCircle, Trash2 } from "lucide-react";
import { api } from "../lib/api";
import { socket } from "../lib/ws";
import { useUI } from "../store/ui";
import {
  Badge,
  Button,
  Card,
  Empty,
  FormField,
  Input,
  LoadingState,
  Modal,
  PageHeader,
  Select,
} from "../components/ui";

interface AutonomyStatus {
  mode: string;
  uptime_seconds: number;
  subsystems: Record<string, boolean>;
  channels: string[];
  config: { enabled: boolean; workers: number; poll_seconds: number; daily_job_limit: number };
}

interface ScheduleView {
  schedule_id: string;
  name: string;
  objective: string;
  kind: string;
  enabled: boolean;
  timezone: string;
  next_run_at: string;
  interval_seconds: number | null;
  cron_expr: string | null;
  deliver: string;
  skills: string[];
  pre_script: string;
  silent: boolean;
  occurrences_count: number;
}

interface JobView {
  job_id: string;
  status: string;
  objective: string;
  delivery_status: string;
  attempts: number;
  created_at: string;
  error: string | null;
}

interface DeliveryView {
  delivery_id: string;
  job_id: string;
  channel: string;
  target: string;
  status: string;
  subject: string;
  body: string;
}

interface DryRunView {
  objective: string;
  prelude: string;
  prompt_preview: string;
  deliver: string;
  next_run_at: string;
}

const SUBSYSTEM_LABEL: Record<string, string> = {
  goals: "Goals",
  workflows: "Workflows",
  events: "Events",
  specialists: "Specialists",
  learning: "Learning",
};

function scheduleTiming(schedule: ScheduleView): string {
  if (schedule.kind === "interval") return `every ${schedule.interval_seconds}s`;
  if (schedule.kind === "cron") return `cron ${schedule.cron_expr}`;
  return `once at ${schedule.next_run_at}`;
}

export default function Workflows() {
  const queryClient = useQueryClient();
  const pushToast = useUI((state) => state.pushToast);
  const [dryRun, setDryRun] = useState<DryRunView | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<ScheduleView | null>(null);

  const autonomy = useQuery({
    queryKey: ["autonomy"],
    queryFn: () => api.get<AutonomyStatus>("/api/autonomy"),
    refetchInterval: 8000,
  });
  const schedules = useQuery({
    queryKey: ["schedules"],
    queryFn: () => api.get<ScheduleView[]>("/api/schedules"),
    refetchInterval: 6000,
  });
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api.get<JobView[]>("/api/jobs?limit=25"),
    refetchInterval: 6000,
  });
  const deliveries = useQuery({
    queryKey: ["deliveries"],
    queryFn: () => api.get<DeliveryView[]>("/api/deliveries?limit=25"),
    refetchInterval: 6000,
  });

  useEffect(() => {
    const handler = (event: { type: string; subject?: string; body?: string }) => {
      if (event.type !== "delivery") return;
      pushToast({
        tone: "info",
        title: "Scheduled result delivered",
        detail: (event.subject || event.body || "").slice(0, 140),
      });
      void queryClient.invalidateQueries({ queryKey: ["deliveries"] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    };
    const unsubscribe = socket.on(handler);
    return unsubscribe;
  }, [pushToast, queryClient]);

  const toggle = useMutation({
    mutationFn: (input: { subsystem: string; enabled: boolean }) =>
      api.post<AutonomyStatus>("/api/autonomy", input),
    onSuccess: (data) => {
      queryClient.setQueryData(["autonomy"], data);
      pushToast({ tone: "ok", title: "Subsystem updated" });
    },
    onError: (error: Error) =>
      pushToast({ tone: "danger", title: "Could not update subsystem", detail: error.message }),
  });

  const action = useMutation({
    mutationFn: ({ id, verb }: { id: string; verb: string }) =>
      api.post<Record<string, unknown>>(`/api/schedules/${id}/${verb}`),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["schedules"] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      if (variables.verb === "run") {
        pushToast({ tone: "ok", title: "Schedule queued to run" });
      } else {
        pushToast({ tone: "ok", title: "Schedule updated" });
      }
    },
    onError: (error: Error) =>
      pushToast({ tone: "danger", title: "Action failed", detail: error.message }),
  });

  const dryRunMutation = useMutation({
    mutationFn: (id: string) => api.post<DryRunView>(`/api/schedules/${id}/dry-run`),
    onSuccess: (data) => setDryRun(data),
    onError: (error: Error) =>
      pushToast({ tone: "danger", title: "Dry run failed", detail: error.message }),
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.delete<{ removed: boolean }>(`/api/schedules/${id}`),
    onSuccess: () => {
      setConfirmDelete(null);
      void queryClient.invalidateQueries({ queryKey: ["schedules"] });
      pushToast({ tone: "ok", title: "Schedule deleted" });
    },
    onError: (error: Error) =>
      pushToast({ tone: "danger", title: "Delete failed", detail: error.message }),
  });

  const scheduleRows = schedules.data ?? [];
  const jobRows = jobs.data ?? [];
  const deliveryRows = deliveries.data ?? [];
  const status = autonomy.data;

  return (
    <div className="h-full overflow-y-auto">
      <PageHeader
        title="Schedules"
        icon={<CalendarClock size={20} />}
        subtitle="Autonomous runs with results delivered to the origin channel — no CLI needed"
        actions={
          <Button variant="primary" onClick={() => setCreateOpen(true)}>
            New schedule
          </Button>
        }
      />
      <div className="space-y-5 px-4 pb-28 sm:px-6 md:pb-12 lg:px-8">
        <Card title="Autonomy runtime" icon={<CalendarClock size={16} className="text-copper" />}>
          {autonomy.isLoading || !status ? (
            <LoadingState label="Loading autonomy status…" />
          ) : (
            <div className="flex flex-wrap items-center gap-x-6 gap-y-3 text-sm">
              <span className="flex items-center gap-2">
                <Badge tone={status.mode === "owner" ? "bronze" : "dim"}>
                  {status.mode === "owner" ? "runtime active" : status.mode}
                </Badge>
                <span className="text-muted">
                  {status.config.workers} workers · poll {status.config.poll_seconds}s ·
                  limit {status.config.daily_job_limit} jobs/day
                </span>
              </span>
              {Object.keys(status.subsystems).map((name) => (
                <label key={name} className="flex items-center gap-2 text-muted">
                  <input
                    type="checkbox"
                    className="accent-copper"
                    checked={status.subsystems[name]}
                    onChange={(event) =>
                      toggle.mutate({ subsystem: name, enabled: event.target.checked })
                    }
                  />
                  {SUBSYSTEM_LABEL[name] ?? name}
                </label>
              ))}
            </div>
          )}
        </Card>

        <Card title="Schedules" icon={<CalendarClock size={16} className="text-copper" />}>
          {schedules.isLoading ? (
            <LoadingState label="Loading schedules…" />
          ) : scheduleRows.length === 0 ? (
            <Empty>
              No schedules yet. Create one from chat by asking EURUS, or use the “New schedule”
              button.
            </Empty>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-[11px] uppercase tracking-[0.1em] text-muted">
                    <th scope="col" className="pb-2.5 pr-4">Name</th>
                    <th scope="col" className="pb-2.5 pr-4">Objective</th>
                    <th scope="col" className="pb-2.5 pr-4">Pattern</th>
                    <th scope="col" className="pb-2.5 pr-4">Deliver to</th>
                    <th scope="col" className="pb-2.5 pr-4">Status</th>
                    <th scope="col" className="pb-2.5">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {scheduleRows.map((schedule) => (
                    <tr key={schedule.schedule_id} className="border-b border-line-soft last:border-b-0">
                      <td className="py-3 pr-4 font-medium text-ink">{schedule.name || "—"}</td>
                      <td className="max-w-[280px] py-3 pr-4 text-muted">
                        {schedule.objective.slice(0, 90)}
                      </td>
                      <td className="py-3 pr-4 font-mono text-2xs text-muted">
                        {scheduleTiming(schedule)}
                      </td>
                      <td className="py-3 pr-4 text-muted">{schedule.deliver}</td>
                      <td className="py-3 pr-4">
                        <Badge tone={schedule.enabled ? "sage" : "dim"}>
                          {schedule.enabled ? "active" : "paused"}
                        </Badge>
                      </td>
                      <td className="py-3">
                        <div className="flex flex-wrap gap-1.5">
                          <Button
                            size="sm"
                            variant="primary"
                            onClick={() => action.mutate({ id: schedule.schedule_id, verb: "run" })}
                          >
                            <PlayCircle size={14} /> Run now
                          </Button>
                          <Button
                            size="sm"
                            onClick={() => dryRunMutation.mutate(schedule.schedule_id)}
                          >
                            <FlaskConical size={14} /> Dry run
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            aria-label={schedule.enabled ? "Pause schedule" : "Resume schedule"}
                            onClick={() =>
                              action.mutate({
                                id: schedule.schedule_id,
                                verb: schedule.enabled ? "pause" : "resume",
                              })
                            }
                          >
                            {schedule.enabled ? <Pause size={14} /> : <Play size={14} />}
                          </Button>
                          <Button
                            size="sm"
                            variant="danger"
                            aria-label={`Delete ${schedule.name || schedule.schedule_id}`}
                            onClick={() => setConfirmDelete(schedule)}
                          >
                            <Trash2 size={14} />
                          </Button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <div className="grid gap-5 lg:grid-cols-2">
          <Card title="Recent jobs" icon={<PlayCircle size={16} className="text-copper" />}>
            {jobRows.length === 0 ? (
              <Empty>No jobs yet.</Empty>
            ) : (
              <ul className="space-y-2 text-sm">
                {jobRows.map((job) => (
                  <li key={job.job_id} className="flex items-center justify-between gap-3">
                    <span className="min-w-0 truncate text-ink">{job.objective || job.job_id.slice(0, 8)}</span>
                    <span className="flex shrink-0 items-center gap-1.5">
                      <Badge tone={job.status === "completed" ? "sage" : job.status === "failed" ? "danger" : "dim"}>
                        {job.status}
                      </Badge>
                      {job.delivery_status && (
                        <Badge tone={job.delivery_status === "ok" ? "sage" : job.delivery_status === "delivery_failed" ? "danger" : "dim"}>
                          {job.delivery_status}
                        </Badge>
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card title="Delivery queue" icon={<CalendarClock size={16} className="text-copper" />}>
            {deliveryRows.length === 0 ? (
              <Empty>No deliveries yet.</Empty>
            ) : (
              <ul className="space-y-2 text-sm">
                {deliveryRows.map((row) => (
                  <li key={row.delivery_id} className="flex items-center justify-between gap-3">
                    <span className="min-w-0 truncate text-muted">
                      {row.channel}
                      {row.target ? ` → ${row.target}` : ""} · {row.subject || row.body.slice(0, 60)}
                    </span>
                    <Badge
                      tone={
                        row.status === "delivered"
                          ? "sage"
                          : row.status === "failed"
                            ? "danger"
                            : row.status === "suppressed"
                              ? "dim"
                              : "bronze"
                      }
                    >
                      {row.status}
                    </Badge>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </div>

      <Modal open={dryRun !== null} onClose={() => setDryRun(null)} title="Schedule dry run" wide>
        {dryRun && (
          <div className="space-y-3 text-sm">
            <p className="text-muted">
              This is the prompt the agent would receive when the schedule fires (no model is
              called):
            </p>
            <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-lg border border-line-soft bg-raised-soft/60 p-3 text-xs text-ink shadow-[inset_0_1px_2px_rgba(51,42,36,0.05)]">
              {dryRun.prompt_preview}
            </pre>
            <p className="text-muted">
              Deliver to <span className="font-semibold text-ink">{dryRun.deliver}</span> · next
              run {dryRun.next_run_at}
            </p>
          </div>
        )}
      </Modal>

      <Modal
        open={confirmDelete !== null}
        onClose={() => setConfirmDelete(null)}
        title="Delete schedule?"
        description="The schedule and its pending jobs will be removed."
        footer={
          <div className="flex justify-end gap-2">
            <Button size="sm" onClick={() => setConfirmDelete(null)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              size="sm"
              onClick={() => confirmDelete && remove.mutate(confirmDelete.schedule_id)}
              disabled={remove.isPending}
            >
              <Trash2 size={13} /> Delete
            </Button>
          </div>
        }
      >
        <p className="text-sm leading-relaxed text-muted">
          <span className="font-semibold text-ink">{confirmDelete?.name || confirmDelete?.schedule_id}</span>{" "}
          runs {confirmDelete ? scheduleTiming(confirmDelete) : ""} and has fired{" "}
          {confirmDelete?.occurrences_count ?? 0} time
          {(confirmDelete?.occurrences_count ?? 0) === 1 ? "" : "s"}. This cannot be undone.
        </p>
      </Modal>

      <CreateScheduleModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={() => {
          void queryClient.invalidateQueries({ queryKey: ["schedules"] });
          pushToast({ tone: "ok", title: "Schedule created" });
        }}
      />
    </div>
  );
}

function CreateScheduleModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}) {
  const pushToast = useUI((state) => state.pushToast);
  const [objective, setObjective] = useState("");
  const [name, setName] = useState("");
  const [kind, setKind] = useState("interval");
  const [intervalSeconds, setIntervalSeconds] = useState("3600");
  const [runAt, setRunAt] = useState("");
  const [cronExpr, setCronExpr] = useState("");
  const [deliver, setDeliver] = useState("web");
  const [telegramChatId, setTelegramChatId] = useState("");
  const [preScript, setPreScript] = useState("");
  const [silent, setSilent] = useState(false);

  const create = useMutation({
    mutationFn: () =>
      api.post("/api/schedules", {
        objective,
        kind,
        name,
        deliver: telegramChatId ? "telegram" : deliver,
        telegram_chat_id: telegramChatId || undefined,
        interval_seconds: kind === "interval" ? Number(intervalSeconds) : undefined,
        run_at: kind === "once" ? runAt : undefined,
        cron_expr: kind === "cron" ? cronExpr : undefined,
        pre_script: preScript || undefined,
        silent,
      }),
    onSuccess: () => {
      onClose();
      onCreated();
    },
    onError: (error: Error) =>
      pushToast({ tone: "danger", title: "Schedule rejected", detail: error.message }),
  });

  const intervalValid = kind !== "interval" || (Number(intervalSeconds) >= 60 && Number.isFinite(Number(intervalSeconds)));
  const canSubmit = Boolean(objective.trim()) && intervalValid;

  return (
    <Modal open={open} onClose={onClose} title="New schedule" wide>
      <div className="space-y-3">
        <FormField label="Objective">
          <Input value={objective} onChange={(event) => setObjective(event.target.value)} placeholder="What the agent should run" />
        </FormField>
        <div className="grid gap-3 sm:grid-cols-2">
          <FormField label="Name">
            <Input value={name} onChange={(event) => setName(event.target.value)} placeholder="optional" />
          </FormField>
          <FormField label="Pattern">
            <Select value={kind} onChange={(event) => setKind(event.target.value)}>
              <option value="interval">interval</option>
              <option value="once">once</option>
              <option value="cron">cron</option>
            </Select>
          </FormField>
        </div>
        {kind === "interval" && (
          <FormField label="Interval (seconds, minimum 60)" error={intervalValid ? undefined : "Interval must be at least 60 seconds"}>
            <Input value={intervalSeconds} onChange={(event) => setIntervalSeconds(event.target.value)} inputMode="numeric" />
          </FormField>
        )}
        {kind === "once" && (
          <FormField label="Run at (ISO-8601)">
            <Input value={runAt} onChange={(event) => setRunAt(event.target.value)} placeholder="2026-01-01T09:00:00+07:00" />
          </FormField>
        )}
        {kind === "cron" && (
          <FormField label="Cron expression">
            <Input value={cronExpr} onChange={(event) => setCronExpr(event.target.value)} placeholder="0 9 * * *" />
          </FormField>
        )}
        <div className="grid gap-3 sm:grid-cols-2">
          <FormField label="Deliver results to">
            <Select value={deliver} onChange={(event) => setDeliver(event.target.value)}>
              <option value="web">web (broadcast)</option>
              <option value="local">local file</option>
              <option value="tui">TUI</option>
              <option value="telegram">Telegram</option>
            </Select>
          </FormField>
          {deliver === "telegram" && (
            <FormField label="Telegram chat ID">
              <Input value={telegramChatId} onChange={(event) => setTelegramChatId(event.target.value)} />
            </FormField>
          )}
        </div>
        <FormField label="Pre-script (optional)">
          <Input value={preScript} onChange={(event) => setPreScript(event.target.value)} placeholder="stdout is injected into the prompt" />
        </FormField>
        <label className="flex items-center gap-2 text-sm text-muted">
          <input type="checkbox" className="accent-copper" checked={silent} onChange={(event) => setSilent(event.target.checked)} />
          Silent (do not deliver results)
        </label>
      </div>
      <div className="mt-4 flex justify-end gap-2">
        <Button onClick={onClose}>Cancel</Button>
        <Button
          variant="primary"
          disabled={!canSubmit}
          onClick={() => create.mutate()}
        >
          Save schedule
        </Button>
      </div>
    </Modal>
  );
}
