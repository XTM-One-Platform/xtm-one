// A classification entry shows one label F1: micro, macro and samples combine
// entries, so on one entry they repeat or rename the entry's own F1. The run's Metrics
// panel keeps all three, and another family's entry reads as before.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { EvaluationRunDetail as RunDetail, RunMetric } from "@/types";
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

const RUN = {
  id: "run-1",
  evaluation_id: "e-1",
  evaluation_name: "ATT&CK techniques",
  agent_id: "a-1",
  agent_name: "TTP Harvester",
  agent_slug: "ttp-harvester",
  agent_user_name: null,
  model_id: "gpt-x",
  is_default_model: true,
  status: "completed",
  trigger_reason: "manual",
  score: null,
  speed_score: null,
  overall_score: null,
  f1_score: null,
  quality_score: 75,
  performance_score: null,
  quality_metric_keys: [],
  performance_metric_keys: [],
  quality_contributions: {},
  performance_contributions: {},
  quality_aggregation_excluded: [],
  performance_aggregation_excluded: [],
  quality_source: "judge",
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
      description: "Phobos ransomware",
      input_prompt: "List the techniques",
      expected_answer: "[]",
      files: null,
      agent_output: "[]",
      trace_ids: [],
      score: 0,
      duration_seconds: 2,
      speed_score: null,
      overall_score: null,
      f1_score: null,
      is_reused: false,
      reused_from_result_id: null,
      sub_scores: {},
      evaluator_reasoning: null,
      execution_details: {
        total_duration_ms: 2000,
        agent_duration_ms: 2000,
        traces: [],
        metric_results: {
          "classification.micro_prf": {
            status: "scored",
            value: 83.33,
            detail: { precision: 71.43, recall: 100, f1: 83.33, tp: 5, fp: 2, fn: 0 },
          },
        },
      },
      created_at: "2026-10-25T10:00:00Z",
    },
  ],
} as unknown as RunDetail;

const CLASSIFICATION_ENTRY: RunMetric[] = [
  { key: "classification.micro_prf", value: 83.33, half: "quality", shown_on_entry: true, entry_label: "Label F1" },
  { key: "classification.macro_prf", value: 71.43, half: "quality", shown_on_entry: false, entry_label: null },
  { key: "classification.samples_f1", value: 83.33, half: "quality", shown_on_entry: false, entry_label: null },
  { key: "classification.subset_accuracy", value: 0, half: "quality", shown_on_entry: true, entry_label: null },
];

// As an older backend serves it: no entry view at all.
const STIX_ENTRY: RunMetric[] = [
  { key: "stix.entity_prf", value: 50, half: "quality" },
  { key: "stix.relationship_prf", value: 40, half: "quality" },
];

/** The run with its entry's stored outcomes replaced. */
function runWith(metricResults: Record<string, unknown>): RunDetail {
  const [result] = RUN.results;
  return {
    ...RUN,
    results: [{ ...result, execution_details: { ...result.execution_details, metric_results: metricResults } }],
  } as RunDetail;
}

function serve(entry: RunMetric[], run: RunDetail = RUN) {
  get.mockImplementation(async (path: unknown) => {
    const url = typeof path === "string" ? path : "";
    if (url === "/evaluations/runs/run-1") return run;
    if (url.endsWith("/params")) return { params: [] };
    if (url.endsWith("/tags")) return { tags: {} };
    if (url.endsWith("/metrics/aggregate")) {
      return {
        run_id: "run-1",
        aggregates: entry.map(({ key, value, half }) => ({
          key,
          half,
          mean: value,
          p50: value,
          p95: value,
          min: value,
          max: value,
          count: 1,
        })),
      };
    }
    if (url.endsWith("/results/res-1/metrics")) return entry;
    return undefined;
  });
}

function renderPage() {
  return renderWithProviders(
    <MemoryRouter initialEntries={["/evaluations/runs/run-1"]}>
      <Routes>
        <Route path="/evaluations/runs/:id" element={<EvaluationRunDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

async function openEntry(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: /Phobos ransomware/ }));
}

describe("an entry's metrics", () => {
  beforeEach(() => get.mockReset());

  it("shows one label F1 on a classification entry", async () => {
    const user = userEvent.setup();
    serve(CLASSIFICATION_ENTRY);
    renderPage();
    await openEntry(user);

    expect(await screen.findByText("Label F1")).toBeTruthy();
    expect(screen.getByRole("button", { name: "83.33: show the precision, recall and counts of Label F1" })).toBeTruthy();
    expect(screen.getByText("Exact Label Set")).toBeTruthy();
    expect(screen.queryByText("Label F1, All Labels Pooled (Micro)")).toBeNull();
    expect(screen.queryByText("Label F1, Averaged per Label (Macro)")).toBeNull();
    expect(screen.queryByText("Label F1, Averaged per Entry (Samples)")).toBeNull();
  });

  it("keeps the three F1s, with their canonical names, in the run's Metrics panel", async () => {
    const user = userEvent.setup();
    serve(CLASSIFICATION_ENTRY);
    renderPage();
    await user.click(await screen.findByRole("button", { name: /Metrics/ }));

    expect(await screen.findByText("Label F1, All Labels Pooled (Micro)")).toBeTruthy();
    expect(screen.getByText("Label F1, Averaged per Label (Macro)")).toBeTruthy();
    expect(screen.getByText("Label F1, Averaged per Entry (Samples)")).toBeTruthy();
  });

  it("gives a classification entry that scored 0 one quality note, under its entry name", async () => {
    const user = userEvent.setup();
    const unreadable = { status: "scored", value: 0, detail: { reason: "The answer is not a list of labels." } };
    serve(
      CLASSIFICATION_ENTRY,
      runWith({
        "classification.micro_prf": unreadable,
        "classification.macro_prf": unreadable,
        "classification.samples_f1": unreadable,
      }),
    );
    renderPage();
    await openEntry(user);

    const notes = (await screen.findByText("Quality notes")).parentElement as HTMLElement;
    await waitFor(() => expect(within(notes).getAllByText(/The answer is not a list of labels/)).toHaveLength(1));
    expect(within(notes).getByText("Label F1")).toBeTruthy();
    expect(within(notes).queryByText(/\((Micro|Macro|Samples)\)/)).toBeNull();
  });

  it("reads another family's entry as before", async () => {
    const user = userEvent.setup();
    serve(STIX_ENTRY);
    renderPage();
    await openEntry(user);

    expect(await screen.findByText("Entity F1")).toBeTruthy();
    expect(screen.getByText("Relationship F1")).toBeTruthy();
  });
});
