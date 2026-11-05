
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
  if (schedule.kind === "interval") return `setiap ${schedule.interval_seconds}s`;
  if (schedule.kind === "cron") return `cron ${schedule.cron_expr}`;
  return `sekali pada ${schedule.next_run_at}`;
}

export default function Workflows() {
  const queryClient = useQueryClient();
  const pushToast = useUI((state) => state.pushToast);
  const [dryRun, setDryRun] = useState<DryRunView | null>(null);
  const [createOpen, setCreateOpen] = useState(false);

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
        title: "Hasil jadwal terkirim",
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
      pushToast({ tone: "ok", title: "Subsistem diperbarui" });
    },
    onError: (error: Error) =>
      pushToast({ tone: "danger", title: "Gagal mengubah subsistem", detail: error.message }),
  });

  const action = useMutation({
    mutationFn: ({ id, verb }: { id: string; verb: string }) =>
      api.post<Record<string, unknown>>(`/api/schedules/${id}/${verb}`),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["schedules"] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      if (variables.verb === "run") {
        pushToast({ tone: "ok", title: "Jadwal antre dijalankan" });
      } else {
        pushToast({ tone: "ok", title: "Jadwal diperbarui" });
      }
    },
    onError: (error: Error) =>
      pushToast({ tone: "danger", title: "Aksi gagal", detail: error.message }),
  });

  const dryRunMutation = useMutation({
    mutationFn: (id: string) => api.post<DryRunView>(`/api/schedules/${id}/dry-run`),
    onSuccess: (data) => setDryRun(data),
    onError: (error: Error) =>
      pushToast({ tone: "danger", title: "Uji coba gagal", detail: error.message }),
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.delete<{ removed: boolean }>(`/api/schedules/${id}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["schedules"] });
      pushToast({ tone: "ok", title: "Jadwal dihapus" });
    },
    onError: (error: Error) =>
      pushToast({ tone: "danger", title: "Gagal menghapus", detail: error.message }),
  });

  const scheduleRows = schedules.data ?? [];
  const jobRows = jobs.data ?? [];
  const deliveryRows = deliveries.data ?? [];
  const status = autonomy.data;

  return (
    <div className="h-full overflow-y-auto">
      <PageHeader
        title="Jadwal"
        icon={<CalendarClock size={20} />}
        subtitle="Run otomatis, hasil terkirim ke kanal asal — tanpa CLI"
        actions={
          <Button variant="primary" onClick={() => setCreateOpen(true)}>
            Jadwal baru
          </Button>
        }
      />
      <div className="space-y-5 px-4 pb-28 sm:px-6 md:pb-12 lg:px-8">
        <Card title="Runtime otonomi" icon={<CalendarClock size={16} className="text-copper" />}>
          {autonomy.isLoading || !status ? (
            <LoadingState label="Memuat status otonomi…" />
          ) : (
            <div className="flex flex-wrap items-center gap-x-6 gap-y-3 text-sm">
              <span className="flex items-center gap-2">
                <Badge tone={status.mode === "owner" ? "bronze" : "dim"}>
                  {status.mode === "owner" ? "runtime aktif" : status.mode}
                </Badge>
                <span className="text-muted">
                  {status.config.workers} worker · poll {status.config.poll_seconds}s ·
                  limit {status.config.daily_job_limit} job/hari
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

        <Card title="Jadwal" icon={<CalendarClock size={16} className="text-copper" />}>
          {schedules.isLoading ? (
            <LoadingState label="Memuat jadwal…" />
          ) : scheduleRows.length === 0 ? (
            <Empty>
              Belum ada jadwal. Buat dari chat dengan perintah ke Eurus, atau lewat tombol
              “Jadwal baru”.
            </Empty>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-[11px] uppercase tracking-[0.1em] text-muted">
                    <th className="pb-2.5 pr-4">Nama</th>
                    <th className="pb-2.5 pr-4">Objektif</th>
                    <th className="pb-2.5 pr-4">Pola</th>
                    <th className="pb-2.5 pr-4">Kirim ke</th>
                    <th className="pb-2.5 pr-4">Status</th>
                    <th className="pb-2.5">Aksi</th>
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
                          {schedule.enabled ? "aktif" : "jeda"}
                        </Badge>
                      </td>
                      <td className="py-3">
                        <div className="flex flex-wrap gap-1.5">
                          <Button
                            size="sm"
                            variant="primary"
                            onClick={() => action.mutate({ id: schedule.schedule_id, verb: "run" })}
                          >
                            <PlayCircle size={14} /> Jalankan sekarang
                          </Button>
                          <Button
                            size="sm"
                            onClick={() => dryRunMutation.mutate(schedule.schedule_id)}
                          >
                            <FlaskConical size={14} /> Uji coba
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
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
                            onClick={() => remove.mutate(schedule.schedule_id)}
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
          <Card title="Job terbaru" icon={<PlayCircle size={16} className="text-copper" />}>
            {jobRows.length === 0 ? (
              <Empty>Belum ada job.</Empty>
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

          <Card title="Antrean pengiriman" icon={<CalendarClock size={16} className="text-copper" />}>
            {deliveryRows.length === 0 ? (
              <Empty>Belum ada pengiriman.</Empty>
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

      <Modal open={dryRun !== null} onClose={() => setDryRun(null)} title="Uji coba jadwal" wide>
        {dryRun && (
          <div className="space-y-3 text-sm">
            <p className="text-muted">
              Prompt berikut yang akan diterima agent saat jadwal berjalan
              (tanpa memanggil model):
            </p>
            <pre className="max-h-72 overflow-auto rounded-lg bg-raised-soft/60 p-3 text-xs whitespace-pre-wrap text-ink">
              {dryRun.prompt_preview}
            </pre>
            <p className="text-muted">
              Kirim ke <span className="font-semibold text-ink">{dryRun.deliver}</span> ·
              jalan berikutnya {dryRun.next_run_at}
            </p>
          </div>
        )}
      </Modal>

      <CreateScheduleModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={() => {
          void queryClient.invalidateQueries({ queryKey: ["schedules"] });
          pushToast({ tone: "ok", title: "Jadwal dibuat" });
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
      pushToast({ tone: "danger", title: "Jadwal ditolak", detail: error.message }),
  });

  return (
    <Modal open={open} onClose={onClose} title="Jadwal baru" wide>
      <div className="space-y-3">
        <FormField label="Objektif">
          <Input value={objective} onChange={(event) => setObjective(event.target.value)} placeholder="Apa yang dijalankan agent" />
        </FormField>
        <div className="grid gap-3 sm:grid-cols-2">
          <FormField label="Nama">
            <Input value={name} onChange={(event) => setName(event.target.value)} placeholder="opsional" />
          </FormField>
          <FormField label="Pola">
            <Select value={kind} onChange={(event) => setKind(event.target.value)}>
              <option value="interval">interval</option>
              <option value="once">sekali</option>
              <option value="cron">cron</option>
            </Select>
          </FormField>
        </div>
        {kind === "interval" && (
          <FormField label="Interval (detik)">
            <Input value={intervalSeconds} onChange={(event) => setIntervalSeconds(event.target.value)} />
          </FormField>
        )}
        {kind === "once" && (
          <FormField label="Waktu (ISO-8601)">
            <Input value={runAt} onChange={(event) => setRunAt(event.target.value)} placeholder="2026-01-01T09:00:00+07:00" />
          </FormField>
        )}
        {kind === "cron" && (
          <FormField label="Ekspresi cron">
            <Input value={cronExpr} onChange={(event) => setCronExpr(event.target.value)} placeholder="0 9 * * *" />
          </FormField>
        )}
        <div className="grid gap-3 sm:grid-cols-2">
          <FormField label="Kirim hasil ke">
            <Select value={deliver} onChange={(event) => setDeliver(event.target.value)}>
              <option value="web">web (broadcast)</option>
              <option value="local">berkas lokal</option>
              <option value="tui">TUI</option>
              <option value="telegram">Telegram</option>
            </Select>
          </FormField>
          {deliver === "telegram" && (
            <FormField label="Chat ID Telegram">
              <Input value={telegramChatId} onChange={(event) => setTelegramChatId(event.target.value)} />
            </FormField>
          )}
        </div>
        <FormField label="Pre_script (opsional)">
          <Input value={preScript} onChange={(event) => setPreScript(event.target.value)} placeholder="stdout disuntikkan ke prompt" />
        </FormField>
        <label className="flex items-center gap-2 text-sm text-muted">
          <input type="checkbox" className="accent-copper" checked={silent} onChange={(event) => setSilent(event.target.checked)} />
          Senyap (jangan kirim hasil)
        </label>
      </div>
      <div className="mt-4 flex justify-end gap-2">
        <Button onClick={onClose}>Batal</Button>
        <Button
          variant="primary"
          disabled={!objective.trim()}
          onClick={() => create.mutate()}
        >
          Simpan jadwal
        </Button>
      </div>
    </Modal>
  );
}
