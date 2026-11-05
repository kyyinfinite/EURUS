
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentTaskPanel } from "./AgentTaskPanel";
import { ArtifactPanel } from "./ArtifactPanel";
import { MissionTree } from "./MissionTree";
import { ReviewPanel } from "./ReviewPanel";
import type {
  MissionFindingView,
  MissionReviewView,
  MissionTaskView,
  MissionView,
  MissionVerificationView,
} from "../../lib/types";

afterEach(cleanup);

const mission: MissionView = {
  mission_id: "m-12345678",
  owner_id: "eurus",
  objective: "Build the settings feature end to end",
  status: "running",
  root_run_id: null,
  plan: { route: "hierarchical", task_count: 2 },
  acceptance_criteria: ["tests pass"],
  budget: {},
  version: 3,
  created_at: "2026-11-03T00:00:00Z",
  updated_at: "2026-11-03T01:00:00Z",
  completed_at: null,
};

const apiTask: MissionTaskView = {
  task_id: "t-api",
  mission_id: "m-12345678",
  parent_task_id: null,
  task_key: "api",
  title: "Settings API",
  objective: "implement the settings API",
  depends_on: [],
  required_capabilities: [],
  preferred_role: "backend",
  input_contract: {},
  output_contract: {},
  acceptance_criteria: [],
  status: "succeeded",
  priority: 5,
  attempt: 1,
  assignment_id: "asg-1",
  created_at: "2026-11-03T00:00:00Z",
  updated_at: "2026-11-03T00:30:00Z",
};

const uiTask: MissionTaskView = {
  ...apiTask,
  task_id: "t-ui",
  task_key: "ui",
  title: "Settings page",
  objective: "implement the settings page",
  preferred_role: "frontend",
  status: "running",
  attempt: 0,
  assignment_id: "asg-2",
};

describe("MissionTree", () => {
  it("renders the mission status, objective and task rows", () => {
    render(<MissionTree mission={mission} tasks={[apiTask, uiTask]} />);
    expect(screen.getAllByText("running").length).toBe(2);
    expect(screen.getByText("succeeded")).toBeTruthy();
    expect(screen.getByText(/settings feature end to end/)).toBeTruthy();
    expect(screen.getByText("api")).toBeTruthy();
    expect(screen.getByText("ui")).toBeTruthy();
    expect(screen.getByText("backend")).toBeTruthy();
    expect(screen.getByText("attempt 1")).toBeTruthy();
  });

  it("shows dependency hints between tasks", () => {
    const dependent = { ...uiTask, depends_on: ["t-api"] };
    render(<MissionTree mission={mission} tasks={[apiTask, dependent]} />);
    expect(screen.getByText(/after api/)).toBeTruthy();
  });

  it("invokes onSelectTask when a row is clicked", () => {
    const onSelectTask = vi.fn();
    render(
      <MissionTree mission={mission} tasks={[apiTask]} onSelectTask={onSelectTask} />,
    );
    fireEvent.click(screen.getByText("api"));
    expect(onSelectTask).toHaveBeenCalledWith(apiTask);
  });

  it("renders an empty hint without tasks", () => {
    render(<MissionTree mission={mission} tasks={[]} />);
    expect(screen.getByText(/No tasks planned yet/)).toBeTruthy();
  });
});

describe("AgentTaskPanel", () => {
  it("lists assignment badges and per-task detail", () => {
    render(
      <AgentTaskPanel
        tasks={[apiTask]}
        assignments={[{ assignment_id: "asg-1", role: "backend", status: "succeeded", task_id: "t-api" }]}
      />,
    );
    expect(screen.getByText(/backend · succeeded/)).toBeTruthy();
    expect(screen.getByText(/asg asg-1/)).toBeTruthy();
    expect(screen.getByText(/implement the settings API/)).toBeTruthy();
  });
});

describe("ArtifactPanel", () => {
  const artifact = {
    artifact_id: "a-1",
    owner_id: "eurus",
    mission_id: "m-12345678",
    task_id: "t-api",
    assignment_id: "asg-1",
    artifact_type: "patch",
    media_type: "text/x-diff",
    content_hash: "f".repeat(64),
    size_bytes: 128,
    version: 2,
    metadata: {},
    allowed_recipients: [],
    created_at: "2026-11-03T00:00:00Z",
  };

  const verification: MissionVerificationView = {
    check_name: "pytest targeted",
    status: "passed",
    category: "test",
    command_argv: ["python", "-m", "pytest", "tests/test_settings.py"],
    working_directory: "/repo",
    exit_code: 0,
    duration_ms: 1200,
    stdout_tail: "",
    stderr_tail: "",
    environment: {},
    mission_id: "m-12345678",
    task_id: "t-api",
    artifact_id: "a-1",
  };

  it("shows artifact version, hash and verification evidence", () => {
    render(<ArtifactPanel artifacts={[artifact]} verifications={[verification]} />);
    expect(screen.getByText("patch")).toBeTruthy();
    expect(screen.getByText(/v2 · text\/x-diff · 128 B/)).toBeTruthy();
    expect(screen.getByText("pytest targeted")).toBeTruthy();
    expect(screen.getByText("$ python -m pytest tests/test_settings.py")).toBeTruthy();
    expect(screen.getByText("passed")).toBeTruthy();
  });

  it("explains that claims are not evidence when nothing ran", () => {
    render(<ArtifactPanel artifacts={[]} verifications={[]} />);
    expect(screen.getByText(/claims are never evidence/)).toBeTruthy();
  });
});

describe("ReviewPanel", () => {
  const finding: MissionFindingView = {
    finding_id: "f-1",
    severity: "critical",
    category: "correctness",
    location: "app.py:42",
    evidence: "off-by-one in retry loop",
    suggested_remediation: "use <= when comparing",
    disposition: "open",
    revision_task_id: "t-rev",
  };

  const review: MissionReviewView = {
    review_id: "r-1",
    mission_id: "m-12345678",
    task_id: "t-api",
    reviewer_assignment_id: "asg-rev",
    status: "changes_requested",
    findings: [finding],
    summary: "Needs one fix",
    cycle: 2,
    created_at: "2026-11-03T01:00:00Z",
  };

  it("marks blocking findings and revision linkage", () => {
    render(<ReviewPanel reviews={[review]} />);
    expect(screen.getByText("changes_requested")).toBeTruthy();
    expect(screen.getByText("critical")).toBeTruthy();
    expect(screen.getByText(/1 blocking finding/)).toBeTruthy();
    expect(screen.getByText(/blocks acceptance until resolved/)).toBeTruthy();
    expect(screen.getByText(/revision t-rev/)).toBeTruthy();
  });

  it("renders the empty state without reviews", () => {
    render(<ReviewPanel reviews={[]} />);
    expect(screen.getByText(/No reviews recorded/)).toBeTruthy();
  });
});
