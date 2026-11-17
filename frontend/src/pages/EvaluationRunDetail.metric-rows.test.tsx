// Run detail > Metrics: the state a metric row shows for each exclusion reason.
// A reason this build does not know reads as neutral, never as a
// known cause.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { EvaluationRun, RunMetricsAggregate } from "@/types";
import { RunMetricsPanel } from "./EvaluationRunDetail";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const get = vi.mocked(api.get);

const NEUTRAL = { badge: "Not counted", note: "Measured, but not counted towards the overall score above." };
const FAILED = { badge: "Failed", note: "The metric failed to compute for this run. A re-run may succeed." };
const OPT_IN = {
  badge: "Not counted",
  note: "Measured, but not counted: this metric is not counted by default, and this evaluation has not selected it. Select it under Overall Score Composition to count it.",
};

function runExcluding(reason: string): EvaluationRun {
  return {
    id: "run-1",
    evaluation_id: "e-1",
    evaluation_name: "Eval",
    agent_id: "a-1",
    agent_name: "Alpha",
    agent_slug: "alpha",
    agent_user_name: null,
    model_id: "gpt-5",
    is_default_model: false,
    status: "completed",
    trigger_reason: "manual",
    score: null,
    speed_score: null,
    overall_score: null,
    f1_score: null,
    quality_score: null,
    performance_score: null,
    quality_metric_keys: [],
    performance_metric_keys: [],
    quality_contributions: {},
    performance_contributions: {},
    quality_aggregation_excluded: [{ key: "probe_metric", reason }],
    performance_aggregation_excluded: [],
    quality_source: "judge",
    duration_seconds: 10,
    avg_sub_scores: {},
    completed_entries: 1,
    total_entries: 1,
    error: null,
    started_at: "2026-10-01T10:00:00Z",
    completed_at: "2026-10-01T10:05:00Z",
    created_at: "2026-10-01T10:00:00Z",
    can_replay: false,
  };
}

const AGGREGATE: RunMetricsAggregate = {
  run_id: "run-1",
  aggregates: [{ key: "probe_metric", half: "quality", mean: 0.5, p50: 0.5, p95: 0.5, min: 0.5, max: 0.5, count: 3 }],
};

/** The badge and its note on the row of `probe_metric`. */
async function probeRowState(reason: string) {
  renderWithProviders(<RunMetricsPanel run={runExcluding(reason)} />);
  fireEvent.click(await screen.findByRole("button", { name: /Metrics/ }));
  const row = screen.getByText("probe_metric").closest("tr")!;
  const badge = within(row).getByText(/^(Counted|Not counted|N\/A|Failed)$/);
  fireEvent.focus(badge);
  const note = await screen.findByRole("tooltip");
  return { badge: badge.textContent, note: note.textContent };
}

describe("metric row state per exclusion reason", () => {
  beforeEach(() => {
    get.mockReset();
    get.mockResolvedValue(AGGREGATE);
  });

  it.each(["a_reason_from_a_newer_backend", "toString", "constructor", "__proto__", "hasOwnProperty"])(
    "an unrecognised reason %j renders the neutral state",
    async (reason) => {
      expect(await probeRowState(reason)).toEqual(NEUTRAL);
    },
  );

  it.each([
    ["failed", FAILED],
    ["not_a_default_contributor", OPT_IN],
  ])("a known reason %j renders its own state", async (reason, expected) => {
    expect(await probeRowState(reason)).toEqual(expected);
  });
});

// A metric the package pools or macro-averages has a run value that can
// differ from its Mean: said once, on the Mean header, never beside a value.
describe("the Mean column of a pooled or macro metric", () => {
  const MEAN = "The average of this metric over every entry that has a value, entries left out of the overall scores included.";
  const POOLED_NOTE =
    "For a metric marked Pooled, the run's value adds up the entries' counts before dividing, so an entry with more of them weighs more: it can differ from this mean.";
  const MACRO_NOTE =
    "For a metric marked Macro, the run's value adds up each class's counts over the entries, then averages the classes' F1, so a rare class weighs as much as a frequent one: it can differ from this mean.";
  const values = { mean: 0.5, p50: 0.5, p95: 0.5, min: 0.5, max: 0.5, count: 3 };

  async function openPanel(aggregates: RunMetricsAggregate["aggregates"]) {
    get.mockReset();
    get.mockResolvedValue({ run_id: "run-1", aggregates });
    renderWithProviders(<RunMetricsPanel run={runExcluding("failed")} />);
    fireEvent.click(await screen.findByRole("button", { name: /Metrics/ }));
  }

  const row = (key: string) => screen.getByText(key).closest("tr")!;

  /** The note on the Mean header of the table that holds *key*'s row: each headline's
   * table has its own. */
  async function meanHeaderNote(key: string) {
    fireEvent.focus(within(row(key).closest("table")!).getByText("Mean"));
    return (await screen.findByRole("tooltip")).textContent;
  }

  it("marks the pooled and macro rows, and explains both markers once on the Mean header", async () => {
    await openPanel([
      { key: "pooled_metric", half: "quality", pooled: true, ...values },
      { key: "macro_metric", half: "quality", macro: true, ...values },
      { key: "probe_metric", half: "quality", ...values },
    ]);

    expect(within(row("pooled_metric")).getByText("Pooled")).toBeInTheDocument();
    expect(within(row("macro_metric")).getByText("Macro")).toBeInTheDocument();
    expect(within(row("probe_metric")).queryByText(/^(Pooled|Macro)$/)).toBeNull();
    // Nothing to hover beside a value: the values are plain text.
    for (const key of ["pooled_metric", "macro_metric"]) {
      for (const cell of Array.from(row(key).querySelectorAll("td")).slice(1)) {
        expect(cell.querySelector("[tabindex]")).toBeNull();
      }
    }
    expect(await meanHeaderNote("pooled_metric")).toBe(MEAN + POOLED_NOTE + MACRO_NOTE);
  });

  it("explains only the kind the table holds", async () => {
    await openPanel([{ key: "pooled_metric", half: "quality", pooled: true, ...values }]);

    expect(await meanHeaderNote("pooled_metric")).toBe(MEAN + POOLED_NOTE);
  });

  it("keeps the Mean header to its own description when no metric is pooled or macro", async () => {
    await openPanel([{ key: "probe_metric", half: "quality", ...values }]);

    expect(await meanHeaderNote("probe_metric")).toBe(MEAN);
  });
});
