
import { GitBranch, ListTree, ShieldCheck } from "lucide-react";
import type { MissionTaskView, MissionView } from "../../lib/types";
import { Badge } from "../ui";

const TASK_TONES: Record<string, "ok" | "warn" | "danger" | "info" | "dim" | "active"> = {
  pending: "dim",
  ready: "info",
  dispatched: "info",
  running: "active",
  review_pending: "warn",
  succeeded: "ok",
  blocked: "danger",
  failed: "danger",
  cancelled: "dim",
  skipped: "dim",
  revision_required: "warn",
  reassigned: "warn",
};

const MISSION_TONES: Record<string, "ok" | "warn" | "danger" | "info" | "dim" | "active"> = {
  created: "dim",
  planning: "info",
  planned: "info",
  running: "active",
  verifying: "warn",
  awaiting_approval: "warn",
  completed: "ok",
  completed_unverified: "warn",
  failed: "danger",
  cancelled: "dim",
  blocked: "danger",
};

export function missionTone(status: string) {
  return MISSION_TONES[status] ?? "dim";
}

export function taskTone(status: string) {
  return TASK_TONES[status] ?? "dim";
}

export interface MissionTreeProps {
  mission: MissionView;
  tasks: MissionTaskView[];
  onSelectTask?: (task: MissionTaskView) => void;
  selectedTaskId?: string | null;
}

export function MissionTree({ mission, tasks, onSelectTask, selectedTaskId }: MissionTreeProps) {
  const byId = new Map(tasks.map((task) => [task.task_id, task]));
  const roots = tasks.filter((task) => !task.parent_task_id);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={missionTone(mission.status)}>{mission.status}</Badge>
        <span className="font-mono text-2xs text-faint">
          {mission.mission_id.slice(0, 8)} · v{mission.version} ·{" "}
          {String(mission.plan?.route ?? "direct")} route
        </span>
      </div>
      <p className="text-sm leading-relaxed text-dim">{mission.objective}</p>
      {mission.acceptance_criteria.length > 0 && (
        <ul className="space-y-1">
          {mission.acceptance_criteria.map((criterion, index) => (
            <li key={`${index}-${criterion}`} className="flex items-start gap-1.5 text-xs text-muted">
              <ShieldCheck size={13} className="mt-0.5 shrink-0 text-sage" />
              {criterion}
            </li>
          ))}
        </ul>
      )}
      <div className="space-y-1.5">
        {roots.map((task) => (
          <TreeRow
            key={task.task_id}
            task={task}
            depth={0}
            byId={byId}
            onSelectTask={onSelectTask}
            selectedTaskId={selectedTaskId}
          />
        ))}
        {roots.length === 0 && (
          <p className="flex items-center gap-1.5 text-xs text-faint">
            <ListTree size={13} /> No tasks planned yet.
          </p>
        )}
      </div>
    </div>
  );
}

function TreeRow({
  task,
  depth,
  byId,
  onSelectTask,
  selectedTaskId,
}: {
  task: MissionTaskView;
  depth: number;
  byId: Map<string, MissionTaskView>;
  onSelectTask?: (task: MissionTaskView) => void;
  selectedTaskId?: string | null;
}) {
  const children = [...byId.values()].filter((item) => item.parent_task_id === task.task_id);
  const selected = selectedTaskId === task.task_id;
  const rowClasses = `flex w-full items-start gap-2 rounded-lg border px-2.5 py-2 text-left ${
    selected
      ? "border-copper/50 bg-copper-soft/60 shadow-[inset_0_1px_0_rgba(255,255,255,0.6)]"
      : "border-line-soft"
  }`;
  const rowBody = (
    <>
      <GitBranch size={13} className="mt-0.5 shrink-0 text-faint" />
      <span className="min-w-0 flex-1">
        <span className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-xs text-ink">{task.task_key}</span>
          <Badge tone={taskTone(task.status)}>{task.status}</Badge>
          {task.preferred_role && (
            <span className="font-mono text-2xs text-copper">{task.preferred_role}</span>
          )}
          {task.attempt > 0 && (
            <span className="font-mono text-2xs text-faint">attempt {task.attempt}</span>
          )}
        </span>
        <span className="mt-0.5 block truncate text-xs text-muted">
          {task.title || task.objective}
        </span>
        {task.depends_on.length > 0 && (
          <span className="mt-0.5 block font-mono text-2xs text-faint">
            after{" "}
            {task.depends_on
              .map((id) => byId.get(id)?.task_key ?? id.slice(0, 6))
              .join(", ")}
          </span>
        )}
      </span>
    </>
  );
  return (
    <div>
      {onSelectTask ? (
        <button
          type="button"
          onClick={() => onSelectTask(task)}
          className={`${rowClasses} transition-colors hover:border-line hover:bg-raised-soft/60`}
          style={{ marginLeft: depth * 14 }}
        >
          {rowBody}
        </button>
      ) : (
        <div className={rowClasses} style={{ marginLeft: depth * 14 }}>
          {rowBody}
        </div>
      )}
      {children.map((child) => (
        <TreeRow
          key={child.task_id}
          task={child}
          depth={depth + 1}
          byId={byId}
          onSelectTask={onSelectTask}
          selectedTaskId={selectedTaskId}
        />
      ))}
    </div>
  );
}
