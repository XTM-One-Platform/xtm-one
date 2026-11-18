// The STIX bundle review (#12282) as a reviewer meets it: one answer checked against the
// expected bundle, two answers compared, the five views, the inspector, and the states of a
// refused review, an unreadable answer and an entry without report text.

import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { BundleReview, BundleReviewAnswer, ComparedCell, ComparedRun, EvaluationComparison } from "@/types";
import EvaluationBundleReview from "./EvaluationBundleReview";

// The evaluation's page tabs register palette commands; the palette is not under test here.
vi.mock("@/lib/commands/CommandsProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/commands/CommandsProvider")>()),
  useCommandSections: () => {},
}));

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

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);

function answer(key: string, model: string, entity: number, relationship: number): BundleReviewAnswer {
  return {
    key,
    result_id: key,
    run_id: `run-${key}`,
    evaluation_id: "ev-1",
    evaluation_name: "STIX extraction",
    model_id: model,
    subject_id: null,
    subject_name: null,
    created_at: null,
    judge_score: 70,
    anomaly: null,
    metrics: { "stix.entity_prf": entity, "stix.relationship_prf": relationship },
  };
}

// APT29 (alias Cozy Bear) uses Cobalt Strike and Phishing. Answer r1 names the actor
// Cozy Bear and adds a spurious domain; answer r2 names it right.
function review(over: Partial<BundleReview> = {}): BundleReview {
  return {
    entry: {
      label: "APT29 phishing report",
      pages: ["APT29, also known as Cozy Bear, used Cobalt Strike after Phishing (T1566) from evil.com."],
      file_name: null,
      text_source: "prompt",
    },
    answers: [answer("r1", "gpt-a", 33.3, 0), answer("r2", "gpt-b", 85.7, 66.7)],
    objects: [
      { t: "intrusion-set", n: "APT29", c: "intrusion-set--1", a: ["Cozy Bear"] },
      { t: "tool", n: "Cobalt Strike", c: "tool--1" },
      { t: "attack-pattern", n: "Phishing", c: "attack-pattern--1", x: "T1566" },
      { t: "intrusion-set", n: "Cozy Bear", c: "intrusion-set--2" },
      { t: "domain-name", n: "evil[.]com", c: "domain-name--1" },
    ],
    groups: [0, 1, 2, 0, 3],
    sources: { expected: [0, 1, 2], r1: [1, 3, 4], r2: [0, 1, 2] },
    relationships: [
      [0, 1, { expected: ["uses", 0, 1, 0], r1: ["uses", 3, 1, 0], r2: ["uses", 0, 1, 0] }],
      [0, 2, { expected: ["uses", 0, 2, 0], r2: ["uses", 0, 2, 0] }],
    ],
    unreadable: [],
    ...over,
  };
}

function renderAt(search: string, view = "") {
  return renderWithProviders(
    <MemoryRouter initialEntries={[`/evaluations/review${view ? `/${view}` : ""}${search}`]}>
      <Routes>
        <Route path="/evaluations/review/:view?" element={<EvaluationBundleReview />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  get.mockReset();
  post.mockReset();
});

describe("EvaluationBundleReview", () => {
  it("checks one answer against the expected bundle and explains a near-miss", async () => {
    const data = review({ answers: [answer("r1", "gpt-a", 33.3, 0)], sources: { expected: [0, 1, 2], r1: [1, 3, 4] } });
    get.mockResolvedValue(data);
    renderAt("?results=r1");

    expect(await screen.findByText("APT29 phishing report")).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/evaluations/bundle-review?result_ids=r1");
    const scores = screen.getByRole("region", { name: "Scores on this report" });
    expect(within(scores).getByText("33.3")).toBeInTheDocument();
    expect(screen.getByText(/checked against the expected bundle\./)).toBeInTheDocument();
    // The near-miss and the spurious domain, counted as the scorer counts them.
    expect(screen.getByRole("list", { name: "Objects by status" })).toHaveTextContent("1matched");

    await userEvent.click(screen.getByRole("button", { name: "APT29" }));
    const inspector = screen.getByRole("complementary", { name: "Inspector" });
    expect(within(inspector).getByText(/Near-miss · Alias/)).toBeInTheDocument();
    expect(within(inspector).getByText(/an alias of the expected “APT29”/)).toBeInTheDocument();
  });

  it("compares two answers: what the second fixed", async () => {
    get.mockResolvedValue(review());
    renderAt("?results=r1,r2");

    expect(await screen.findByText(/What changed from A to B/)).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/evaluations/bundle-review?result_ids=r1%2Cr2");
    const filters = screen.getByRole("group", { name: "Status" });
    for (const name of ["Fixed", "Regressed", "New error", "Still wrong", "Unchanged"]) {
      expect(within(filters).getByRole("button", { name })).toBeInTheDocument();
    }
    expect(screen.getByRole("list", { name: "Objects by status" })).toHaveTextContent("3fixed");
    expect(within(screen.getByRole("region", { name: "Scores on this report" })).getByText("85.7")).toBeInTheDocument();
    expect(screen.getByText("Objects · A → B")).toBeInTheDocument();
  });

  it("draws the five views and filters them by type", async () => {
    get.mockResolvedValue(review());
    renderAt("?results=r1,r2");
    await screen.findByText(/What changed from A to B/);

    await userEvent.click(screen.getByRole("tab", { name: "Diff" }));
    expect(screen.getByRole("region", { name: "intrusion-set" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: "Annotate" }));
    expect(screen.getByRole("region", { name: "Page 1 of 1" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /intrusion-set APT29, Fixed, located by/ }).length).toBeGreaterThan(0);

    await userEvent.click(screen.getByRole("tab", { name: "Graph" }));
    expect(screen.getByRole("group", { name: "Merged graph of the compared bundles" })).toBeInTheDocument();
    // A merged node carries the reference's name, the answer's other name under it.
    expect(screen.getByRole("button", { name: "intrusion-set Cozy Bear, Fixed" })).toBeInTheDocument();
    expect(screen.getByText("≈ APT29")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: "Knowledge graph" }));
    expect(screen.getByRole("group", { name: "Knowledge graph of the compared bundles" })).toBeInTheDocument();
    const node = screen.getByRole("button", { name: "tool Cobalt Strike, Unchanged" });
    node.focus();
    await userEvent.keyboard("{Enter}");
    expect(within(screen.getByRole("complementary", { name: "Inspector" })).getByRole("heading", { name: "Cobalt Strike" })).toBeInTheDocument();

    const rail = screen.getByRole("navigation", { name: "Filters" });
    await userEvent.click(within(rail).getByRole("button", { name: /^attack-pattern/ }));
    expect(screen.getByText("Filtered on type attack-pattern.")).toBeInTheDocument();
  });

  it("says when an entry has no report text", async () => {
    get.mockResolvedValue(review({ entry: { label: "No text", pages: [], file_name: null, text_source: null } }));
    renderAt("?results=r1,r2");
    await screen.findByText(/What changed from A to B/);
    await userEvent.click(screen.getByRole("tab", { name: "Annotate" }));
    expect(screen.getByText("No report text")).toBeInTheDocument();
  });

  it("says when an answer is not a readable bundle", async () => {
    get.mockResolvedValue(review({ unreadable: ["r2"], sources: { expected: [0, 1, 2], r1: [1, 3, 4] } }));
    renderAt("?results=r1,r2");
    expect(await screen.findByText("Answer B is not a readable STIX bundle")).toBeInTheDocument();
  });

  it("shows why the review was refused", async () => {
    get.mockRejectedValue(Object.assign(new Error("Only STIX_BUNDLE answers have a bundle review"), { status: 422 }));
    renderAt("?results=r1");
    expect(await screen.findByText("This review cannot be shown")).toBeInTheDocument();
    expect(screen.getByText("Only STIX_BUNDLE answers have a bundle review")).toBeInTheDocument();
  });

  it("asks for an answer when the link names none", async () => {
    renderAt("");
    expect(await screen.findByText("No answer to review")).toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
  });
});

// ─── The run review ──────────────────────────────────────────────────────────

function run(id: string, at: string): ComparedRun {
  return {
    id,
    evaluation_id: "ev-1",
    subject_id: "agent-1",
    subject_name: "Extractor",
    model_id: "gpt-a",
    is_default_model: true,
    created_at: at,
    trigger_reason: "manual",
    metric_suite_version: "1",
    execution_mode: null,
    quality_score: null,
    completed_entries: 2,
    total_entries: 2,
    judge_score: 70,
  };
}

function cell(runId: string, entry: string, rid: string, e: [number, number, number], over: Partial<ComparedCell> = {}): ComparedCell {
  const pair = ([tp, fp, fn]: [number, number, number]): [number, number] => [2 * tp, 2 * tp + fp + fn];
  const r: [number, number, number] = [1, 0, 1];
  return {
    run_id: runId,
    entry_key: entry,
    result_id: rid,
    counted: true,
    anomaly: null,
    metrics: { "stix.entity_prf": pair(e), "stix.relationship_prf": pair(r) },
    counts: { "stix.entity_prf": e, "stix.relationship_prf": r },
    cost: 0.5,
    seconds: 300,
    judge_score: 70,
    ...over,
  };
}

// The previous run (run-r1) named APT29 Cozy Bear; the run reviewed (run-r2) names it
// right, and could not answer the second report.
const COMPARISON: EvaluationComparison = {
  family: "STIX_BUNDLE",
  metrics: [
    { key: "stix.entity_prf", label: "Entity F1", direction: "higher_is_better" },
    { key: "stix.relationship_prf", label: "Relationship F1", direction: "higher_is_better" },
  ],
  evaluations: [
    {
      id: "ev-1",
      name: "STIX extraction",
      family: "STIX_BUNDLE",
      execution_mode: null,
      timeout_seconds: null,
      judge_on: true,
      evaluator_model: null,
      judge_weights: null,
      judge_instructions_digest: null,
      subjects: [],
      datasets: [],
      runs: [run("run-r2", "2026-02-02T10:00:00Z")],
      run_choices: [run("run-r2", "2026-02-02T10:00:00Z"), run("run-r1", "2026-02-01T10:00:00Z")],
    },
  ],
  entries: [
    { key: "e1", label: "APT29 phishing report", members: [{ evaluation_id: "ev-1", entry_id: "1", position: 0, has_file: false }] },
    { key: "e2", label: "Second report", members: [{ evaluation_id: "ev-1", entry_id: "2", position: 1, has_file: false }] },
  ],
  cells: [
    cell("run-r1", "e1", "r1", [1, 2, 2]),
    cell("run-r1", "e2", "r1b", [2, 0, 1]),
    cell("run-r2", "e1", "r2", [3, 0, 0]),
    cell("run-r2", "e2", "r2b", [0, 0, 3], { unreadable: true }),
  ],
};

function mockRunReview() {
  const entry = review({
    entry: { ...review().entry, key: "e1", dataset_name: "Golden reports", dataset_description: "Five CTI reports." },
    answers: [
      { ...answer("r2", "gpt-a", 85.7, 66.7), judge_reasoning: "Every actor found." },
      answer("r1", "gpt-a", 33.3, 0),
    ],
  });
  get.mockImplementation(async (url: string) => {
    if (url.startsWith("/evaluations/runs/")) return { id: "run-r2", evaluation_id: "ev-1", status: "completed" };
    if (url.startsWith("/evaluations/compare")) return COMPARISON;
    if (url.startsWith("/evaluations/bundle-review")) return entry;
    throw new Error(`unexpected ${url}`);
  });
  // Every report's answer of the run, for the causes.
  post.mockImplementation(async () => [
    review({ answers: [answer("r2", "gpt-a", 85.7, 66.7)], sources: { expected: [0, 1, 2], r2: [3, 1] } }),
  ]);
}

describe("EvaluationBundleReview, a run", () => {
  it("shows the run's figures over its reports, then one report", async () => {
    mockRunReview();
    renderAt("?run=run-r2");

    expect(await screen.findByText("Report 1 of 2")).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/evaluations/bundle-review?result_ids=r2%2Cr1");
    // The evaluation's pages: crumbs and the one tab row.
    expect(screen.getByRole("heading", { level: 1, name: "STIX extraction" })).toBeInTheDocument();
    expect(within(screen.getByRole("navigation", { name: "Breadcrumb" })).getByText("Run review")).toBeInTheDocument();
    // Pooled over the reports, against the previous run of the same agent and model.
    expect(screen.getByText(/Pooled over the 2 reports, change vs gpt-a · /)).toBeInTheDocument();
    expect(screen.getByText(/1 report without a readable answer/)).toBeInTheDocument();
    const figures = screen.getByRole("group", { name: "Run figures" });
    for (const label of ["Bundle F1", "Entity F1", "Relationship F1", "Judge score", "Cost · time"]) {
      expect(within(figures).getByText(label)).toBeInTheDocument();
    }
    expect(within(figures).getAllByText(/^P [\d.]+ · R [\d.]+$/)).toHaveLength(3);

    const reports = screen.getByRole("tablist", { name: "Reports" });
    expect(within(reports).getAllByRole("tab")).toHaveLength(2);
    expect(within(reports).getByText("no answer")).toBeInTheDocument();
    expect(screen.getByText(/Entity F1 of gpt-a · .* on each report, change vs gpt-a · /)).toBeInTheDocument();

    // Where the entity points go, over every report.
    const causes = screen.getByRole("region", { name: "Where the entity points go" });
    expect(await within(causes).findByRole("button", { name: /Alias/ })).toBeInTheDocument();
    expect(post).toHaveBeenCalledWith("/evaluations/bundle-reviews", { groups: [["r2"], ["r2b"]], include_text: false });
    await userEvent.click(within(causes).getByRole("button", { name: /Alias/ }));
    expect(screen.getByText(/dimmed: no “Alias”/)).toBeInTheDocument();
  });

  it("opens the judge's reasoning and the object's history", async () => {
    mockRunReview();
    renderAt("?run=run-r2&entry=e1");
    await screen.findByText("Report 1 of 2");

    const inspector = await screen.findByRole("complementary", { name: "Inspector" });
    expect(within(inspector).getByText("Pick an object")).toBeInTheDocument();
    expect(within(inspector).getByText("Dataset “Golden reports”: Five CTI reports.")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Read the judge’s reasoning" }));
    expect(within(screen.getByRole("region", { name: "What the judge said" })).getByText("Every actor found.")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "APT29" }));
    const history = within(inspector).getByLabelText("History of this object");
    expect(history).toHaveTextContent(/near-miss.*→.*matched/);
    expect(within(history).getByText("Fixed")).toBeInTheDocument();
  });

  it("compares the run with another run of the evaluation", async () => {
    mockRunReview();
    renderAt("?run=run-r2&against=run-r1", "diff");
    expect(await screen.findByText(/What changed from A · .* to B · /)).toBeInTheDocument();
    expect(await screen.findByText("Objects · A → B")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Diff", selected: true })).toBeInTheDocument();
  });

  it("says when the run's evaluation is not a STIX bundle one", async () => {
    get.mockImplementation(async (url: string) =>
      url.startsWith("/evaluations/runs/") ? { id: "run-r2", evaluation_id: "ev-1", status: "completed" } : { ...COMPARISON, family: "PROSE" },
    );
    renderAt("?run=run-r2");
    expect(await screen.findByText("No bundle review for this run")).toBeInTheDocument();
  });
});
