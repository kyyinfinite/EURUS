
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, CircleAlert, CircleCheck, Flag, Play, RotateCcw, Send, ShieldQuestion, Sparkles, StopCircle } from "lucide-react";
import { api } from "../lib/api";
import type {
  AcceptanceDecisionView,
  MissionArtifactView,
  MissionDetail,
  MissionLearningView,
  MissionReviewView,
  MissionVerificationView,
  MissionView,
} from "../lib/types";
import { AgentTaskPanel } from "../components/agents/AgentTaskPanel";
import { ArtifactPanel } from "../components/agents/ArtifactPanel";
import { MissionTree, missionTone } from "../components/agents/MissionTree";
import { ReviewPanel } from "../components/agents/ReviewPanel";
import { Badge, Button, Card, Empty, ErrorNote, LoadingState, MetricCard, Modal, PageHeader } from "../components/ui";
import { useUI } from "../store/ui";

export default function Missions() {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const list = useQuery({
    queryKey: ["missions"],
    queryFn: () => api.get<MissionView[]>("/api/missions"),
    refetchInterval: 5000,
  });

  if (list.isLoading) return <LoadingState label="Loading missions…" />;
  if (list.isError) {
    return (
      <ErrorNote
        detail={list.error instanceof Error ? list.error.message : "failed to load missions"}
      />
    );
  }

  const missions = list.data ?? [];
  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Missions"
        subtitle="Hierarchical engineering work: Main Orchestrator plans a task DAG, specialists execute, acceptance needs evidence"
      />
      <div className="min-h-0 flex-1 overflow-y-auto p-4 pb-28 sm:p-5 md:pb-12">
        <div className="mx-auto max-w-4xl">
          {selectedId ? (
            <MissionDetailPanel
              missionId={selectedId}
              onBack={() => setSelectedId(null)}
            />
          ) : missions.length === 0 ? (
            <div className="pt-10">
              <Empty>
                No missions yet. Create one to see the orchestrator decompose an objective into a
                dependency-aware task DAG.
              </Empty>
              <div className="mt-4 flex justify-center">
                <Button variant="primary" onClick={() => setCreating(true)}>
                  <Flag size={13} /> New mission
                </Button>
              </div>
            </div>
          ) : (
            <div className="space-y-3">
              <div className="flex justify-end">
                <Button variant="primary" onClick={() => setCreating(true)}>
                  <Flag size={13} /> New mission
                </Button>
              </div>
              {missions.map((mission) => (
                <button
                  key={mission.mission_id}
                  type="button"
                  className="block w-full text-left"
                  onClick={() => setSelectedId(mission.mission_id)}
                >
                  <Card className="transition-colors hover:border-copper/40">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge tone={missionTone(mission.status)}>{mission.status}</Badge>
                      <span className="font-mono text-2xs text-faint">
                        {mission.mission_id.slice(0, 8)} · {String(mission.plan?.task_count ?? 0)}{" "}
                        tasks
                      </span>
                      <span className="ml-auto text-2xs text-faint">{mission.updated_at}</span>
                    </div>
                    <p className="mt-1.5 line-clamp-2 text-sm text-dim">{mission.objective}</p>
                  </Card>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
      <CreateMissionModal open={creating} onClose={() => setCreating(false)} />
    </div>
  );
}

function MissionDetailPanel({ missionId, onBack }: { missionId: string; onBack: () => void }) {
  const queryClient = useQueryClient();
  const pushToast = useUI((state) => state.pushToast);

  const detail = useQuery({
    queryKey: ["mission", missionId],
    queryFn: () => api.get<MissionDetail>(`/api/missions/${missionId}`),
    refetchInterval: 4000,
  });
  const artifacts = useQuery({
    queryKey: ["mission", missionId, "artifacts"],
    queryFn: () => api.get<MissionArtifactView[]>(`/api/missions/${missionId}/artifacts`),
  });
  const verifications = useQuery({
    queryKey: ["mission", missionId, "verifications"],
    queryFn: () => api.get<MissionVerificationView[]>(`/api/missions/${missionId}/verifications`),
  });
  const reviews = useQuery({
    queryKey: ["mission", missionId, "reviews"],
    queryFn: () => api.get<MissionReviewView[]>(`/api/missions/${missionId}/reviews`),
  });
  const learning = useQuery({
    queryKey: ["mission", missionId, "learning"],
    queryFn: () => api.get<MissionLearningView>(`/api/missions/${missionId}/learning`),
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["mission", missionId] });
    queryClient.invalidateQueries({ queryKey: ["missions"] });
  };

  const action = useMutation({
    mutationFn: ({ verb, body }: { verb: string; body?: unknown }) =>
      api.post(`/api/missions/${missionId}/${verb}`, body),
    onSuccess: (_data, variables) => {
      pushToast({ tone: "ok", title: `Mission ${variables.verb}`, detail: "accepted by the orchestrator" });
      invalidate();
    },
    onError: (error) => {
      pushToast({
        tone: "warn",
        title: "Action rejected",
        detail: error instanceof Error ? error.message : "unknown error",
      });
    },
  });

  const [decision, setDecision] = useState<AcceptanceDecisionView | null>(null);

  if (detail.isLoading) return <LoadingState label="Loading mission…" />;
  if (detail.isError || !detail.data) {
    return (
      <div className="space-y-3">
        <Button variant="ghost" onClick={onBack}>
          ← All missions
        </Button>
        <ErrorNote
          detail={detail.error instanceof Error ? detail.error.message : "mission not found"}
        />
      </div>
    );
  }

  const { mission, tasks, assignments, aggregation } = detail.data;
  const run = (verb: string, body?: unknown) => action.mutate({ verb, body });
  const status = mission.status;
  const canStart = ["created", "planned", "planning", "failed", "blocked"].includes(status);
  const canDispatch = ["planned", "running", "awaiting_approval"].includes(status);
  const canResume = ["blocked", "failed", "cancelled"].includes(status);
  const canFinalize = ["running", "verifying", "awaiting_approval", "completed_unverified"].includes(status);
  const canCancel = ["planning", "planned", "running", "verifying", "awaiting_approval"].includes(status);

  const finalize = async () => {
    try {
      const result = await api.post<AcceptanceDecisionView>(
        `/api/missions/${missionId}/finalize`,
        {},
      );
      setDecision(result);
      invalidate();
    } catch (error) {
      pushToast({
        tone: "warn",
        title: "Finalize rejected",
        detail: error instanceof Error ? error.message : "unknown error",
      });
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="ghost" onClick={onBack}>
          ← All missions
        </Button>
        <div className="ml-auto flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => run("start")} disabled={action.isPending || !canStart}>
            <Play size={13} /> Start
          </Button>
          <Button variant="outline" onClick={() => run("dispatch", { limit: 10 })} disabled={action.isPending || !canDispatch}>
            <Send size={13} /> Dispatch ready
          </Button>
          <Button variant="outline" onClick={() => run("resume")} disabled={action.isPending || !canResume}>
            <RotateCcw size={13} /> Resume
          </Button>
          <Button variant="sage" onClick={() => void finalize()} disabled={action.isPending || !canFinalize}>
            <ShieldQuestion size={13} /> Finalize
          </Button>
          <Button variant="danger" onClick={() => run("cancel", { reason: "cancelled from web" })} disabled={action.isPending || !canCancel}>
            <StopCircle size={13} /> Cancel
          </Button>
        </div>
      </div>

      {decision && (
        <Card className={decision.decision === "completed" ? "border-sage/40" : "border-warn/40"}>
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={decision.decision === "completed" ? "ok" : "warn"}>
              {decision.decision}
            </Badge>
            <span className="text-2xs text-faint">acceptance policy decision</span>
          </div>
          <ul className="mt-2 space-y-1">
            {decision.reasons.map((reason, index) => (
              <li key={`${index}-${reason}`} className="text-xs text-muted">
                · {reason}
              </li>
            ))}
          </ul>
        </Card>
      )}

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <MetricCard label="Assignments" value={aggregation.total} icon={<Bot size={14} />} />
        <MetricCard label="Succeeded" value={aggregation.succeeded} icon={<CircleCheck size={14} />} tone="ok" />
        <MetricCard label="Pending" value={aggregation.pending} icon={<Send size={14} />} tone="info" />
        <MetricCard label="Failed" value={aggregation.failed} icon={<CircleAlert size={14} />} tone="danger" />
      </div>

      <Card>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-faint">Mission tree</h3>
        <MissionTree mission={mission} tasks={tasks} />
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-faint">Specialists</h3>
          <AgentTaskPanel tasks={tasks} assignments={assignments} />
        </Card>
        <Card>
          <ArtifactPanel artifacts={artifacts.data ?? []} verifications={verifications.data ?? []} />
        </Card>
      </div>

      <Card>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-faint">Reviews</h3>
        <ReviewPanel reviews={reviews.data ?? []} />
      </Card>

      <LearningPostMortem learning={learning.data ?? null} learningError={learning.error} />
    </div>
  );
}

function dimensionLabel(dimension: string): string {
  return dimension.replace(/_/g, " ");
}

function LearningPostMortem({
  learning,
  learningError,
}: {
  learning: MissionLearningView | null;
  learningError: unknown;
}) {
  if (learningError) {
    return null;
  }
  if (!learning) {
    return null;
  }
  return (
    <Card>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-faint">
          Learning post-mortem
        </h3>
        {learning.learned ? (
          <Badge tone="ok">post-mortem recorded</Badge>
        ) : (
          <Badge tone="dim">not learned yet</Badge>
        )}
        {learning.learned && learning.lessons.length > 0 && (
          <span className="text-2xs text-faint">
            {learning.lessons.length} lesson{learning.lessons.length === 1 ? "" : "s"} accepted
          </span>
        )}
      </div>
      {!learning.learned ? (
        <p className="text-xs leading-relaxed text-muted">
          The learning tick records a post-mortem for terminal missions: outcome dimensions,
          specialist performance and the lessons the agent keeps for future objectives.
        </p>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-2">
            {learning.evaluations.map((item) => (
              <span
                key={`${item.dimension}-${item.created_at}`}
                className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-2xs font-medium ${
                  item.passed
                    ? "border-sage/40 bg-sage-soft text-sage-dark"
                    : "border-warn/40 bg-warn-soft text-warn-dark"
                }`}
              >
                {item.passed ? <CircleCheck size={11} /> : <CircleAlert size={11} />}
                <span>{dimensionLabel(item.dimension)}</span>
                <span className="font-mono">{item.score}</span>
              </span>
            ))}
          </div>
          {learning.lessons.length > 0 ? (
            <ul className="space-y-1.5">
              {learning.lessons.map((lesson) => (
                <li
                  key={lesson.lesson_id}
                  className="flex items-start gap-2 rounded-lg border border-line-soft bg-canvas/60 px-3 py-2 text-xs leading-relaxed text-muted"
                >
                  <Sparkles size={12} className="mt-0.5 shrink-0 text-copper" />
                  {lesson.text}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-muted">No lessons were accepted from this mission.</p>
          )}
        </div>
      )}
    </Card>
  );
}

function CreateMissionModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const pushToast = useUI((state) => state.pushToast);
  const [objective, setObjective] = useState("");
  const [criteria, setCriteria] = useState("");
  const [tasksJson, setTasksJson] = useState("");

  const create = useMutation({
    mutationFn: () => {
      let tasks: unknown;
      if (tasksJson.trim()) {
        tasks = JSON.parse(tasksJson);
      }
      return api.post("/api/missions", {
        objective,
        acceptance_criteria: criteria
          .split(";")
          .map((item) => item.trim())
          .filter(Boolean),
        tasks,
      });
    },
    onSuccess: () => {
      pushToast({ tone: "ok", title: "Mission created", detail: "the orchestrator owns it now" });
      queryClient.invalidateQueries({ queryKey: ["missions"] });
      setObjective("");
      setCriteria("");
      setTasksJson("");
      onClose();
    },
  });

  return (
    <Modal open={open} onClose={onClose} title="New mission">
      <div className="space-y-3">
        <label className="block text-xs text-muted">
          Objective
          <textarea
            className="mt-1 w-full"
            rows={3}
            value={objective}
            onChange={(event) => setObjective(event.target.value)}
            placeholder="What outcome must the team deliver?"
          />
        </label>
        <label className="block text-xs text-muted">
          Acceptance criteria (semicolon-separated)
          <input
            className="mt-1 w-full"
            value={criteria}
            onChange={(event) => setCriteria(event.target.value)}
            placeholder="tests pass; docs updated"
          />
        </label>
        <label className="block text-xs text-muted">
          Optional task DAG (JSON array of subtasks)
          <textarea
            className="mt-1 w-full font-mono text-2xs"
            rows={4}
            value={tasksJson}
            onChange={(event) => setTasksJson(event.target.value)}
            placeholder='[{"task_key":"ui","objective":"…","preferred_role":"frontend"}]'
          />
        </label>
        {create.isError && (
          <ErrorNote
            detail={create.error instanceof Error ? create.error.message : "create failed"}
          />
        )}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            onClick={() => create.mutate()}
            disabled={!objective.trim() || create.isPending}
          >
            Create mission
          </Button>
        </div>
      </div>
    </Modal>
  );
}
