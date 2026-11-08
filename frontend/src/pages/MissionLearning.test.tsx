import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Analytics from "./Analytics";
import Missions from "./Missions";
import type {
  MissionAnalyticsView,
  MissionDetail,
  MissionLearningView,
  MissionView,
} from "../lib/types";

vi.mock("../lib/api", () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
  },
}));

import { api } from "../lib/api";

const getMock = vi.mocked(api.get);

const analytics: MissionAnalyticsView = {
  missions_total: 3,
  missions_learned: 2,
  decisions: { completed: 2, blocked: 1, cancelled: 0, completed_unverified: 0, failed: 0 },
  expired: 1,
  dimension_means: {
    plan_quality: 0.92,
    task_correctness: 0.67,
    verification_coverage: 1,
    resource_efficiency: 0.5,
    agent_performance: 0.9,
  },
  roles: [
    { role: "frontend", tasks: 2, succeeded: 2, failed: 0, blocked: 0, revisions: 0 },
    { role: "backend", tasks: 2, succeeded: 0, failed: 1, blocked: 1, revisions: 1 },
  ],
  lessons: [
    {
      lesson_id: "lesson-1",
      text: "Mission 'Ship the stalled feature' missed its budget deadline and was cancelled",
      mission_id: "m-2",
    },
  ],
};

const learnedView: MissionLearningView = {
  mission_id: "m-1",
  status: "completed",
  objective: "Ship the gateway feature",
  learned: true,
  evaluations: [
    {
      dimension: "plan_quality",
      score: 1,
      passed: true,
      evidence: ["tasks=2", "blocked=0", "revisions=0"],
      created_at: "2026-11-04T00:00:00Z",
    },
    {
      dimension: "verification_coverage",
      score: 1,
      passed: true,
      evidence: ["verifications=2", "covered_tasks=2/2"],
      created_at: "2026-11-04T00:00:00Z",
    },
    {
      dimension: "agent_performance",
      score: 0.7,
      passed: false,
      evidence: ["exhausted=0", "revision_pressure=0.4"],
      created_at: "2026-11-04T00:00:00Z",
    },
  ],
  lessons: [
    {
      lesson_id: "lesson-9",
      text: "Mission 'Ship the hardened feature' drove 2 revision task(s)",
    },
  ],
};

const mission: MissionView = {
  mission_id: "m-1",
  owner_id: "eurus",
  objective: "Ship the gateway feature",
  status: "completed",
  root_run_id: null,
  plan: { route: "hierarchical", task_count: 2 },
  acceptance_criteria: [],
  budget: {},
  version: 3,
  created_at: "2026-11-04T00:00:00Z",
  updated_at: "2026-11-04T00:10:00Z",
  completed_at: "2026-11-04T00:10:00Z",
};

const detail: MissionDetail = {
  mission,
  tasks: [],
  assignments: [],
  aggregation: { total: 2, succeeded: 2, failed: 0, pending: 0, cancelled: 0, complete: true },
};

function renderAnalytics() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Analytics />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderMissions() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <Missions />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getMock.mockReset();
});

afterEach(cleanup);

describe("Analytics mission intelligence", () => {
  it("renders the aggregate with roles, means and lessons", async () => {
    getMock.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/missions/analytics")) return analytics;
      return [];
    }) as unknown as typeof getMock;
    renderAnalytics();
    await waitFor(() => expect(screen.getByText("Mission intelligence")).toBeTruthy());
    expect(screen.getByText("Terminal missions")).toBeTruthy();
    expect(screen.getByText("backend")).toBeTruthy();
    expect(screen.getByText("plan quality")).toBeTruthy();
    expect(
      screen.getByText(/missed its budget deadline and was cancelled/),
    ).toBeTruthy();
    expect(screen.getByText(/Deadline expiries/)).toBeTruthy();
  });

  it("shows the empty state before any mission finished", async () => {
    getMock.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/missions/analytics")) {
        return { ...analytics, missions_total: 0, missions_learned: 0, roles: [], lessons: [] };
      }
      return [];
    }) as unknown as typeof getMock;
    renderAnalytics();
    await waitFor(() =>
      expect(screen.getByText(/No missions have finished yet/)).toBeTruthy(),
    );
  });

  it("renders the unavailable fallback when the gateway rejects analytics", async () => {
    getMock.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/missions/analytics")) {
        throw new Error("disabled by configuration");
      }
      return [];
    }) as unknown as typeof getMock;
    renderAnalytics();
    await waitFor(() => expect(screen.getByText(/unavailable/i)).toBeTruthy());
  });
});

describe("Missions learning post-mortem", () => {
  beforeEach(() => {
    getMock.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/missions/analytics")) return analytics;
      if (path === "/api/missions" || path === "/api/missions?") return [mission];
      if (path.startsWith("/api/missions/m-1/learning")) return learnedView;
      if (path.startsWith("/api/missions/m-1/artifacts")) return [];
      if (path.startsWith("/api/missions/m-1/verifications")) return [];
      if (path.startsWith("/api/missions/m-1/reviews")) return [];
      if (path.startsWith("/api/missions/m-1")) return detail;
      return [];
    }) as unknown as typeof getMock;
  });

  it("shows the post-mortem with evaluation chips and accepted lessons", async () => {
    renderMissions();
    fireEvent.click(await screen.findByText("Ship the gateway feature"));
    await waitFor(() => expect(screen.getByText(/post-mortem recorded/)).toBeTruthy());
    expect(screen.getByText("plan quality")).toBeTruthy();
    expect(screen.getByText("verification coverage")).toBeTruthy();
    expect(screen.getByText("agent performance")).toBeTruthy();
    expect(screen.getByText("1 lesson accepted")).toBeTruthy();
    expect(screen.getByText(/drove 2 revision task/)).toBeTruthy();
  });

  it("shows the pending note before the learning tick runs", async () => {
    getMock.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/missions/m-1/learning")) {
        return { ...learnedView, learned: false, evaluations: [], lessons: [] };
      }
      if (path.startsWith("/api/missions/m-1/artifacts")) return [];
      if (path.startsWith("/api/missions/m-1/verifications")) return [];
      if (path.startsWith("/api/missions/m-1/reviews")) return [];
      if (path.startsWith("/api/missions/m-1")) return detail;
      if (path === "/api/missions" || path === "/api/missions?") return [mission];
      return [];
    }) as unknown as typeof getMock;
    renderMissions();
    fireEvent.click(await screen.findByText("Ship the gateway feature"));
    await waitFor(() => expect(screen.getByText(/not learned yet/)).toBeTruthy());
    expect(screen.getByText(/learning tick records a post-mortem/)).toBeTruthy();
  });
});
