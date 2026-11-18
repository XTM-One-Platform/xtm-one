// One evaluation's Leaderboard and Results (#2) as a reviewer meets them: the board's
// metrics past the pooled ones, its launches, a run opened for review (and a reference set
// first), and the Results' run sets, sections, consistency tally and methodology.

import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { api } from "@/lib/api";
import { boardOf, runLabeler } from "@/lib/evaluation-leaderboard";
import { renderWithProviders } from "@/testing/providers";
import type { BundleReview, ComparedCell, ComparedEvaluation, ComparedMetric, ComparedRun } from "@/types";
import { LeaderboardTab } from "./leaderboard-tab";
import { ResultsTab } from "./results-tab";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

beforeAll(() => {
  if (!("ResizeObserver" in globalThis)) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
  }
});

const run = (id: string, created_at: string, trigger_reason: string, model_id: string, extra: Partial<ComparedRun> = {}): ComparedRun => ({
  id, evaluation_id: "ev", subject_id: "a", subject_name: "Harvester", model_id, is_default_model: model_id === "m1", created_at,
  completed_at: created_at.replace("T09:00", "T09:30"), trigger_reason, metric_suite_version: "15.0.0", execution_mode: "full_loop",
  quality_score: null, completed_entries: 2, total_entries: 2, judge_score: 70, setup_key: "s1", setup: { persona_version: 3 }, ...extra,
});

const cell = (run_id: string, entry_key: string, f1: [number, number], unreadable = false): ComparedCell => ({
  run_id, entry_key, result_id: `${run_id}-${entry_key}`, counted: true, anomaly: null,
  metrics: { "stix.bundle_f1": f1 }, cost: 0.1, seconds: 30, counts: { "stix.bundle_f1": [f1[0] / 2, 1, 1] }, unreadable,
});

const runs = [
  run("r1", "2026-11-16T09:00:00Z", "manual", "m1"),
  run("r2", "2026-11-16T09:00:00Z", "manual", "m2"),
  run("r3", "2026-11-17T09:00:00Z", "schedule", "m1"),
];
const cellList = [
  cell("r1", "k1", [2, 4]), cell("r1", "k2", [0, 2], true),
  cell("r2", "k1", [4, 4]), cell("r2", "k2", [2, 2]),
  cell("r3", "k1", [4, 4]), cell("r3", "k2", [2, 2]),
];
const cells = new Map<string, Map<string, ComparedCell>>();
for (const c of cellList) cells.set(c.run_id, new Map([...(cells.get(c.run_id) ?? []), [c.entry_key, c]]));
const metrics: ComparedMetric[] = [{ key: "stix.bundle_f1", label: "Bundle F1", direction: "higher_is_better" }];
const board = boardOf(runs, cells, metrics.map((m) => m.key));
const label = runLabeler(runs);

function Where() {
  const location = useLocation();
  return <p>at {location.pathname + location.search}</p>;
}

describe("LeaderboardTab", () => {
  it("offers the judge, the cost and the duration, and opens a run's review", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <MemoryRouter initialEntries={["/board"]}>
        <Routes>
          <Route path="/board" element={<LeaderboardTab board={board} metric={metrics[0]} metrics={metrics} label={label} />} />
          <Route path="/evaluations/review/:view?" element={<Where />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText(/Click a run to review it against the dataset/)).toBeInTheDocument();
    for (const name of ["Judge score", "Cost (USD)", "Duration"]) expect(screen.getByRole("radio", { name })).toBeInTheDocument();
    expect(screen.getByText(/2 launches, 3 runs · bar = best run of the launch, Bundle F1/)).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Experiments" })).toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: "Judge score" }));
    expect(screen.getAllByText("70.0").length).toBeGreaterThan(0);

    const table = screen.getByRole("table");
    const m2 = within(table).getAllByRole("button", { name: /Review the run m2/ })[0];
    await user.click(m2);
    expect(screen.getByText("at /evaluations/review?run=r2")).toBeInTheDocument();
  });

  it("sets a reference with shift+click, then compares a run with it", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <MemoryRouter initialEntries={["/board"]}>
        <Routes>
          <Route path="/board" element={<LeaderboardTab board={board} metric={metrics[0]} metrics={metrics} label={label} />} />
          <Route path="/evaluations/review/:view?" element={<Where />} />
        </Routes>
      </MemoryRouter>,
    );
    const table = screen.getByRole("table");
    await user.keyboard("{Shift>}");
    await user.click(within(table).getAllByRole("button", { name: /Review the run m1 · #1/ })[0]);
    await user.keyboard("{/Shift}");
    expect(screen.getByText(/Reference A: m1 · #1/)).toBeInTheDocument();
    await user.click(within(table).getAllByRole("button", { name: /Review the run m2/ })[0]);
    expect(screen.getByText("at /evaluations/review?run=r2&against=r1")).toBeInTheDocument();
  });
});

describe("ResultsTab", () => {
  const evaluation = {
    id: "ev", name: "STIX extraction", family: "STIX_BUNDLE", execution_mode: "full_loop", timeout_seconds: 7200, judge_on: true,
    evaluator_model: "judge-model", judge_weights: { correctness: 0.5, completeness: 0.5 }, datasets: [{ id: "d", name: "CTI reports", entry_count: 2, instructions_digest: null, description: "Ten public reports." }],
  } as unknown as ComparedEvaluation;

  it("ranks the newest experiment launch, and explains every figure", async () => {
    const review = (ids: string[]): BundleReview => ({
      entry: { label: "e", pages: [], file_name: null, text_source: null },
      answers: ids.map((id) => ({ key: id }) as BundleReview["answers"][number]),
      objects: [{ t: "malware", n: "X", c: "x" }, ...ids.map(() => ({ t: "malware", n: "X", c: "x" }))],
      groups: [0, ...ids.map(() => 0)],
      sources: { expected: [0], ...Object.fromEntries(ids.map((id, i) => [id, [i + 1]])) },
      relationships: [],
      unreadable: [],
    });
    vi.mocked(api.post).mockImplementation(async (_path, body) => (body as { groups: string[][] }).groups.map(review));
    renderWithProviders(
      <MemoryRouter>
        <ResultsTab
          evaluationName="STIX extraction"
          board={board}
          cells={cells}
          metric={metrics[0]}
          metrics={metrics}
          entryCount={2}
          label={label}
          evaluation={evaluation}
        />
      </MemoryRouter>,
    );
    expect(screen.getByText("1 · Ranking with 95% intervals")).toBeInTheDocument();
    expect(screen.getByText(/One line per configuration, pooled over the 2 entries/)).toBeInTheDocument();
    expect(screen.getByText(/Green: on the frontier/)).toBeInTheDocument();
    expect(screen.getByText("3 · Consistency across reruns")).toBeInTheDocument();
    expect(screen.getByText(/Most stable: m1/)).toBeInTheDocument();
    expect(await screen.findByText("In every run")).toBeInTheDocument();
    expect(screen.getByText(/Right: expected objects and relationships found in every run/)).toBeInTheDocument();
    expect(screen.getByText(/LLM judge, judge-model; weights correctness 50%, completeness 50%/)).toBeInTheDocument();
    expect(screen.getByText(/timeout 2 h per run/)).toBeInTheDocument();
    expect(screen.getByText(/“CTI reports”: 2 entries/)).toBeInTheDocument();
    for (const name of ["Export CSV", "Markdown"]) expect(screen.getByRole("button", { name: new RegExp(name) })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /SVG/ })).toHaveLength(3);
  });
});
