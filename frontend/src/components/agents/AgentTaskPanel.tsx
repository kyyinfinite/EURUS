
import { Bot } from "lucide-react";
import type { MissionAssignmentView, MissionTaskView } from "../../lib/types";
import { Badge } from "../ui";
import { taskTone } from "./MissionTree";

export interface AgentTaskPanelProps {
  tasks: MissionTaskView[];
  assignments: MissionAssignmentView[];
}

export function AgentTaskPanel({ tasks, assignments }: AgentTaskPanelProps) {
  const assignmentByTask = new Map(
    assignments.filter((item) => item.task_id).map((item) => [item.task_id, item]),
  );
  return (
    <div className="space-y-2">
      {assignments.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {assignments.map((assignment) => (
            <Badge key={assignment.assignment_id} tone={taskTone(assignment.status)}>
              <Bot size={11} className="mr-1 inline" />
              {assignment.role} · {assignment.status}
            </Badge>
          ))}
        </div>
      )}
      {tasks.map((task) => {
        const assignment = task.assignment_id
          ? (assignmentByTask.get(task.task_id) ?? undefined)
          : undefined;
        return (
          <div key={task.task_id} className="rounded-lg border border-line-soft p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-xs text-ink">{task.task_key}</span>
              <Badge tone={taskTone(task.status)}>{task.status}</Badge>
              {task.preferred_role && (
                <span className="font-mono text-2xs text-copper">
                  {assignment ? assignment.role : task.preferred_role}
                </span>
              )}
              {assignment && (
                <span className="font-mono text-2xs text-faint">
                  asg {assignment.assignment_id.slice(0, 8)}
                </span>
              )}
            </div>
            <p className="mt-1.5 whitespace-pre-wrap text-xs leading-relaxed text-muted">
              {task.objective}
            </p>
            {task.acceptance_criteria.length > 0 && (
              <ul className="mt-1.5 list-inside list-disc text-2xs text-faint">
                {task.acceptance_criteria.map((criterion, index) => (
                  <li key={`${index}-${criterion}`}>{criterion}</li>
                ))}
              </ul>
            )}
          </div>
        );
      })}
      {tasks.length === 0 && <p className="text-xs text-faint">Nothing dispatched yet.</p>}
    </div>
  );
}
