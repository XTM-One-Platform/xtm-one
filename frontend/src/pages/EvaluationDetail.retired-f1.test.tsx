// An evaluation still set to the retired F1 (an older row, an import or a copy)
// shows the judge's score on its Activity tab, like any other: no new run
// records an F1.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { EvaluationRun } from "@/types";
import EvaluationDetail from "./EvaluationDetail";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: { id: "me" }, isAdmin: true }),
}));

// The page's tabs register palette commands; the palette is not under test here.
vi.mock("@/lib/commands/CommandsProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/commands/CommandsProvider")>()),
  useCommandSections: () => {},
}));

vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => false,
}));

const get = vi.mocked(api.get);

function run(id: string, score: number, f1: number | null): EvaluationRun {
  return {
    id,
    evaluation_id: "e-1",
    evaluation_name: "Alert triage",
    agent_id: "a-1",
    agent_name: "Alert Triage Analyst",
    agent_slug: "alert-triage-analyst",
    agent_user_name: null,
    model_id: "gpt-x",
    is_default_model: false,
    status: "completed",
    trigger_reason: "manual",
    score,
    speed_score: null,
    overall_score: null,
    f1_score: f1,
    quality_score: null,
    performance_score: null,
    quality_metric_keys: [],
    performance_metric_keys: [],
    quality_contributions: {},
    performance_contributions: {},
    quality_aggregation_excluded: [],
    performance_aggregation_excluded: [],
    quality_source: "f1",
    duration_seconds: 1,
    avg_sub_scores: {},
    completed_entries: 1,
    total_entries: 1,
    error: null,
    started_at: "2026-10-25T10:00:00Z",
    completed_at: "2026-10-25T10:05:00Z",
    created_at: "2026-10-25T10:00:00Z",
    can_replay: true,
    arm: "real",
    trial_index: 0,
    placebo_target: null,
    placebo_comparison_id: null,
  };
}

const EVALUATION = {
  id: "e-1",
  user_id: "me",
  name: "Alert triage",
  description: null,
  enabled: true,
  family: "PROSE",
  execution_mode: "full_loop",
  run_mode: "parallel",
  evaluator_model: null,
  evaluator_prompt: null,
  evaluation_instructions: null,
  sub_score_weights: null,
  quality_source: "f1",
  penalty_factor: 0.5,
  timeout_seconds: 3600,
  trigger_config: { schedule: null, on_agent_change: false },
  use_agent_default_model: false,
  model_ids: ["gpt-x"],
  disabled_model_ids: [],
  agent_ids: ["a-1"],
  agents: [{ id: "a-1", name: "Alert Triage Analyst", slug: "alert-triage-analyst" }],
  is_company_managed: false,
  shared_group_ids: [],
  shared_user_ids: [],
  has_active_runs: false,
  last_triggered_at: null,
  created_at: "2026-10-25T09:00:00Z",
  updated_at: "2026-10-25T09:00:00Z",
};

// An imported run that holds an F1, and a new one that does not.
const RUNS = [run("r-imported", 81, 33), run("r-new", 62, null)];

beforeEach(() => {
  vi.clearAllMocks();
  get.mockImplementation(((path: string) => {
    if (path === "/evaluations/e-1") return Promise.resolve(EVALUATION);
    if (path.startsWith("/evaluations/e-1/runs/count")) return Promise.resolve({ count: RUNS.length });
    if (path.startsWith("/evaluations/e-1/runs")) return Promise.resolve(RUNS);
    return Promise.resolve([]);
  }) as typeof api.get);
});

describe("EvaluationDetail on an evaluation still set to the retired F1", () => {
  it("lists every run with its judge's score, never its F1", async () => {
    renderWithProviders(
      <MemoryRouter initialEntries={["/evaluations/e-1/activity"]}>
        <Routes>
          <Route path="/evaluations/:id/:tab?" element={<EvaluationDetail />} />
        </Routes>
      </MemoryRouter>,
    );

    expect((await screen.findAllByText("81")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("62").length).toBeGreaterThan(0);
    expect(screen.queryByText("33")).toBeNull();
    expect(screen.queryByText("F1 Accuracy (legacy)")).toBeNull();
    fireEvent.focus(screen.getAllByText("81")[0]);
    expect((await screen.findByRole("tooltip")).textContent).not.toMatch(/F1/);
  });
});
