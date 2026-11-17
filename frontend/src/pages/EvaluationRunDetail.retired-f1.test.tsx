// A run of an evaluation still set to the retired F1 (an older row, an import or
// a copy) shows its judge's score, on the run and on each entry, like any other:
// no new run records an F1.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { EvaluationRunDetail as RunDetail } from "@/types";
import EvaluationRunDetail from "./EvaluationRunDetail";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => false,
  useAiDisabledReason: () => null,
}));

const get = vi.mocked(api.get);

// An imported run that holds an F1 beside its judge's score.
const RUN = {
  id: "run-1",
  evaluation_id: "e-1",
  evaluation_name: "Alert triage",
  agent_id: "a-1",
  agent_name: "Alert Triage Analyst",
  agent_slug: "alert-triage-analyst",
  agent_user_name: null,
  model_id: "gpt-x",
  is_default_model: true,
  status: "completed",
  trigger_reason: "import",
  score: 81,
  speed_score: null,
  overall_score: null,
  f1_score: 33,
  quality_score: null,
  performance_score: null,
  quality_metric_keys: [],
  performance_metric_keys: [],
  quality_contributions: {},
  performance_contributions: {},
  quality_aggregation_excluded: [],
  performance_aggregation_excluded: [],
  quality_source: "f1",
  duration_seconds: 2,
  avg_sub_scores: {},
  completed_entries: 1,
  total_entries: 1,
  error: null,
  started_at: "2026-10-25T10:00:00Z",
  completed_at: "2026-10-25T10:05:00Z",
  created_at: "2026-10-25T10:00:00Z",
  can_replay: true,
  results: [
    {
      id: "res-1",
      run_id: "run-1",
      entry_id: "en-1",
      description: "Triage the phishing alert",
      input_prompt: "Triage it",
      expected_answer: "Escalate",
      files: null,
      agent_output: "Escalate it",
      trace_ids: [],
      score: 77,
      duration_seconds: 2,
      speed_score: null,
      overall_score: null,
      f1_score: 44,
      is_reused: false,
      reused_from_result_id: null,
      sub_scores: { response_accuracy: 77 },
      evaluator_reasoning: "Escalated, as expected.",
      execution_details: { total_duration_ms: 2000, agent_duration_ms: 2000, traces: [] },
      created_at: "2026-10-25T10:00:00Z",
    },
  ],
} as unknown as RunDetail;

describe("EvaluationRunDetail on an evaluation still set to the retired F1", () => {
  it("shows the judge's score of the run and of its entry, never their F1", async () => {
    get.mockImplementation(async (path: unknown) => {
      const url = typeof path === "string" ? path : "";
      if (url === "/evaluations/runs/run-1") return RUN;
      if (url.endsWith("/params")) return { params: [] };
      if (url.endsWith("/tags")) return { tags: {} };
      if (url.endsWith("/metrics/aggregate")) return { run_id: "run-1", aggregates: [] };
      if (url.endsWith("/results/res-1/metrics")) return [];
      return undefined;
    });
    const user = userEvent.setup();
    renderWithProviders(
      <MemoryRouter initialEntries={["/evaluations/runs/run-1"]}>
        <Routes>
          <Route path="/evaluations/runs/:id" element={<EvaluationRunDetail />} />
        </Routes>
      </MemoryRouter>,
    );

    expect((await screen.findAllByText("81")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("77").length).toBeGreaterThan(0);
    expect(screen.getByText("Judge Score")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: /Triage the phishing alert/ }));
    // The opened entry shows the judge's sub-scores and reasoning in place, no F1 line.
    expect(await screen.findByText("Escalated, as expected.")).toBeTruthy();
    expect(screen.queryByText("33")).toBeNull();
    expect(screen.queryByText("44")).toBeNull();
    expect(screen.queryByText("F1 (legacy)")).toBeNull();
  });
});
