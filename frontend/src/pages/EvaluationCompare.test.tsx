// Evaluations of one family compared on their shared entries (#12282): the list's
// selection opens the comparison, which says what can be compared before it compares.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import EvaluationCompare from "./EvaluationCompare";
import Evaluations from "./Evaluations";
import type { EvaluationComparison } from "@/types";

// One evaluation's tabs register palette commands; the palette is not under test here.
vi.mock("@/lib/commands/CommandsProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/commands/CommandsProvider")>()),
  useCommandSections: () => {},
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

vi.mock("@/lib/utils", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/utils")>()),
  saveBlobAs: vi.fn(),
}));

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: { id: "me" }, isAdmin: true }),
}));

const get = vi.mocked(api.get);

function evaluation(id: string, name: string, family: string | null) {
  return {
    id, user_id: "me", name, description: null, enabled: true, family,
    trigger_config: { schedule: null, on_agent_change: false }, use_agent_default_model: false,
    model_ids: ["gpt-x"], disabled_model_ids: [], agent_ids: ["a-1"], entry_count: 5,
    is_company_managed: false, shared_group_ids: [], shared_user_ids: [], has_active_runs: false, last_triggered_at: null,
  };
}

const run = (id: string, evaluationId: string, subject: string, suite: string) => ({
  id, evaluation_id: evaluationId, subject_id: `${subject}-id`, subject_name: subject, model_id: "claude-opus-5-5",
  is_default_model: true, created_at: "2026-11-16T12:00:00Z", metric_suite_version: suite, execution_mode: "full_loop",
  quality_score: 50, completed_entries: 3, total_entries: 3,
});

const subject = (name: string, tools: string[]) => ({
  id: `${name}-id`, kind: "agent" as const, name, is_baseline: false, builtin_tools: tools, custom_tool_count: 0,
  mcp_server_count: 0, skills: [], max_iterations: 4, temperature: null, model: null,
});

const COMPARISON: EvaluationComparison = {
  family: "STIX_BUNDLE",
  metrics: [
    { key: "stix.entity_prf", label: "Entity F1", direction: "higher_is_better" },
    { key: "stix.relationship_prf", label: "Relationship F1", direction: "higher_is_better" },
  ],
  evaluations: [
    {
      id: "ea", name: "STIX Extraction", family: "STIX_BUNDLE", execution_mode: "full_loop", timeout_seconds: 10800,
      judge_on: true, evaluator_model: null, judge_weights: { response_accuracy: 0.49 }, judge_instructions_digest: "x",
      subjects: [subject("Harvester", ["cti", "web"])], datasets: [{ id: "da", name: "Mini Golden", entry_count: 2, instructions_digest: null }],
      runs: [run("ra", "ea", "Harvester", "15.0.0")], run_choices: [run("ra", "ea", "Harvester", "15.0.0")],
    },
    {
      id: "eb", name: "Grounded", family: "STIX_BUNDLE", execution_mode: "full_loop", timeout_seconds: 3600,
      judge_on: true, evaluator_model: null, judge_weights: { response_accuracy: 0.2 }, judge_instructions_digest: "y",
      subjects: [subject("Grounded", ["stix_extractor"])], datasets: [{ id: "db", name: "CTI PDFs", entry_count: 3, instructions_digest: null }],
      runs: [run("rb", "eb", "Grounded", "15.1.0")], run_choices: [run("rb", "eb", "Grounded", "15.1.0")],
    },
  ],
  entries: [
    { key: "k1", label: "APT_K_47", members: [{ evaluation_id: "ea", entry_id: "1", position: 0, has_file: false }, { evaluation_id: "eb", entry_id: "4", position: 1, has_file: true }] },
    { key: "k2", label: "CERTFR", members: [{ evaluation_id: "ea", entry_id: "2", position: 1, has_file: false }, { evaluation_id: "eb", entry_id: "5", position: 2, has_file: true }] },
    { key: "k3", label: "Only in Grounded", members: [{ evaluation_id: "eb", entry_id: "6", position: 0, has_file: true }] },
  ],
  cells: [
    { run_id: "ra", entry_key: "k1", result_id: "res-a1", counted: true, anomaly: null, metrics: { "stix.entity_prf": [0, 34], "stix.relationship_prf": [0, 37] }, cost: 0.9, seconds: 60, unreadable: true },
    { run_id: "ra", entry_key: "k2", result_id: "res-a2", counted: true, anomaly: null, metrics: { "stix.entity_prf": [4, 14], "stix.relationship_prf": [0, 8] }, cost: 1.0, seconds: 80 },
    { run_id: "rb", entry_key: "k1", result_id: "res-b1", counted: true, anomaly: null, metrics: { "stix.entity_prf": [16, 34], "stix.relationship_prf": [6, 37] }, cost: 0.3, seconds: 70 },
    { run_id: "rb", entry_key: "k2", result_id: "res-b2", counted: true, anomaly: null, metrics: { "stix.entity_prf": [2, 14], "stix.relationship_prf": [1, 8] }, cost: 0.4, seconds: 50 },
    { run_id: "rb", entry_key: "k3", result_id: "res-b3", counted: true, anomaly: null, metrics: { "stix.entity_prf": [5, 10], "stix.relationship_prf": [2, 6] }, cost: 0.2, seconds: 40 },
  ],
};

function Where() {
  const location = useLocation();
  return <p>at {location.pathname}{location.search}</p>;
}

beforeEach(() => {
  vi.clearAllMocks();
  get.mockImplementation(((path: string) => {
    if (path.startsWith("/evaluations/compare")) return Promise.resolve(COMPARISON);
    if (path === "/evaluations/families") return Promise.resolve([]);
    if (path.startsWith("/evaluations")) {
      return Promise.resolve([evaluation("ea", "STIX Extraction", "STIX_BUNDLE"), evaluation("eb", "Grounded", "STIX_BUNDLE"), evaluation("ep", "Prose", "PROSE")]);
    }
    return Promise.resolve([]);
  }) as typeof api.get);
});

describe("Selecting evaluations to compare", () => {
  it("compares two evaluations of one family and says why another selection cannot be", async () => {
    renderWithProviders(
      <MemoryRouter initialEntries={["/evaluations"]}>
        <Routes>
          <Route path="/evaluations" element={<Evaluations />} />
          <Route path="/evaluations/compare" element={<Where />} />
        </Routes>
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select STIX Extraction to compare" }));
    expect(screen.getByRole("button", { name: /Compare/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Prose to compare" }));
    expect(screen.getByRole("button", { name: /Compare/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Prose to compare" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Grounded to compare" }));
    fireEvent.click(screen.getByRole("button", { name: /Compare/ }));
    expect(await screen.findByText("at /evaluations/compare?ids=ea,eb")).toBeInTheDocument();
  });
});

describe("EvaluationCompare", () => {
  function renderPage() {
    renderWithProviders(
      <MemoryRouter initialEntries={["/evaluations/compare?ids=ea,eb"]}>
        <Routes>
          <Route path="/evaluations/compare" element={<EvaluationCompare />} />
          <Route path="/evaluations/review/:view?" element={<Where />} />
        </Routes>
      </MemoryRouter>,
    );
  }

  it("says what can be compared before comparing it", async () => {
    renderPage();
    expect(await screen.findByRole("heading", { name: "STIX Extraction vs Grounded" })).toBeInTheDocument();
    expect(screen.getByText("STIX_BUNDLE · 2 entries in common")).toBeInTheDocument();
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(crumbs).toHaveTextContent("Evaluations");
    expect(crumbs).toHaveTextContent("Compare 2 evaluations");
    for (const name of ["Compare evaluations", "Leaderboard", "Run review", "Results"]) {
      expect(screen.getByRole("tab", { name: new RegExp(name) })).toBeInTheDocument();
    }
    expect(screen.getByText(/^Opened from Evaluations with 2 STIX_BUNDLE evaluations selected\./)).toBeInTheDocument();
    expect(screen.getByRole("row", { name: /^Family/ })).toHaveTextContent("Same output, same metrics.");
    const entries = screen.getByRole("row", { name: /^Entries/ });
    expect(entries).toHaveTextContent("2 shared");
    expect(entries).toHaveTextContent("0 only in the first, 1 only in the second.");
    expect(screen.getByRole("row", { name: /^Agent/ })).toHaveTextContent("Under test");
    expect(screen.getByRole("row", { name: /^Input/ })).toHaveTextContent("Changed too");
    expect(screen.getByRole("row", { name: /^Models/ })).toHaveTextContent("1 in common");
    expect(screen.getByRole("row", { name: /^Judge/ })).toHaveTextContent("Not comparable");
    expect(screen.getByRole("row", { name: /^Scorer/ })).toHaveTextContent("re-score the older runs");
    expect(screen.getByRole("row", { name: /^Run limits/ })).toHaveTextContent("No answer on the shared entries reached its limit (2 runs checked).");
  });

  it("leads with Bundle F1, pools the same model's counts on the shared entries and diffs an entry's two answers", async () => {
    renderPage();
    // Bundle F1 pooled over the two shared entries (objects and relationships together):
    // 4/93 → 25/93, never the mean of entry values.
    expect(await screen.findByText(/scores 26\.9 in Grounded against 4\.3 in STIX Extraction/)).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Bundle F1" })).toBeChecked();
    expect(screen.getByText("Cost · time per entry")).toBeInTheDocument();
    expect(screen.getByText(/A hollow dot is an entry without a readable answer/)).toBeInTheDocument();
    const row = screen.getByRole("button", { name: /^APT_K_47:.*\(no readable answer\).*Click to diff them on this entry/ });
    fireEvent.click(row);
    expect(await screen.findByText("at /evaluations/review/diff?run=rb&against=ra&entry=k1")).toBeInTheDocument();
  });

  it("names the configurations that can be first and the best of each evaluation", async () => {
    renderPage();
    expect(await screen.findByText("3 · Every configuration on the 2 shared entries")).toBeInTheDocument();
    expect(screen.getByText(/Best of each: STIX Extraction Harvester 4\.3 · Grounded Grounded 26\.9\./)).toBeInTheDocument();
  });

  it("checks the shared entries against each evaluation's others, and reviews a run", async () => {
    renderPage();
    const row = await screen.findByRole("row", { name: /^Grounded default/ });
    // 32/109 on all three entries, 25/93 shared, 7/16 on its own entry.
    expect(row).toHaveTextContent("29.4");
    expect(row).toHaveTextContent("26.9");
    expect(row).toHaveTextContent("43.8");
    expect(row).toHaveTextContent("$0.30");
    expect(screen.getByText(/Grounded on its 3 entries: the largest gap between the 2 shared entries and the 1 others is −16\.9 points/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Review its 3 entries" }));
    expect(await screen.findByText("at /evaluations/review?run=rb")).toBeInTheDocument();
  });

  it("exports the ranking as CSV and says where it went", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Export CSV/ }));
    expect(await screen.findByText(/^Saved stix-extraction-vs-grounded-stix-bundle-f1-\d{4}-\d{2}-\d{2}\.csv$/)).toBeInTheDocument();
  });
});

describe("EvaluationCompare on one evaluation", () => {
  const sonnet = { ...run("rs", "eb", "Grounded", "15.1.0"), model_id: "claude-sonnet-5-5", is_default_model: false };
  const ONE: EvaluationComparison = {
    ...COMPARISON,
    evaluations: [{ ...COMPARISON.evaluations[1], runs: [sonnet, run("rb", "eb", "Grounded", "15.1.0")], run_choices: [sonnet, run("rb", "eb", "Grounded", "15.1.0")] }],
    entries: COMPARISON.entries.map((e) => ({ ...e, members: e.members.filter((m) => m.evaluation_id === "eb") })),
    cells: [
      ...COMPARISON.cells.filter((c) => c.run_id === "rb"),
      { run_id: "rs", entry_key: "k1", result_id: "res-s1", counted: true, anomaly: null, metrics: { "stix.entity_prf": [0, 34], "stix.relationship_prf": [0, 37] }, cost: 0.9, seconds: 60 },
      { run_id: "rs", entry_key: "k2", result_id: "res-s2", counted: true, anomaly: null, metrics: { "stix.entity_prf": [4, 14], "stix.relationship_prf": [0, 8] }, cost: 1.0, seconds: 80 },
      { run_id: "rs", entry_key: "k3", result_id: "res-s3", counted: true, anomaly: null, metrics: { "stix.entity_prf": [0, 10], "stix.relationship_prf": [0, 6] }, cost: 0.2, seconds: 40 },
    ],
  };

  function renderOne(path: string) {
    get.mockImplementation(((p: string) => Promise.resolve(p.startsWith("/evaluations/compare") ? ONE : [])) as typeof api.get);
    renderWithProviders(
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/evaluations/compare/:tab?" element={<EvaluationCompare />} />
          <Route path="/evaluations/review/:view?" element={<Where />} />
          <Route path="/evaluations/runs/:id" element={<Where />} />
        </Routes>
      </MemoryRouter>,
    );
  }

  it("opens on its Leaderboard: its launches, then configurations by launch or every run ranked, a run opening its review", async () => {
    renderOne("/evaluations/compare?ids=eb");
    expect(await screen.findByText("Launches")).toBeInTheDocument();
    expect(screen.getByText("Dataset “CTI PDFs” · 3 entries · 2 models · 1 launch")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Leaderboard/ })).toHaveAttribute("aria-selected", "true");
    // Both runs started in one minute: one launch; opus (32/109) leads sonnet (4/109) on Bundle F1.
    const opus = screen.getByRole("rowheader", { name: /claude-opus-5-5/ });
    expect(opus.closest("tr")).toHaveTextContent("29.4");
    fireEvent.click(screen.getByRole("radio", { name: "Every run, ranked" }));
    const rows = await screen.findAllByTestId("leaderboard-run-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("claude-opus-5-5");
    fireEvent.click(rows[1]);
    expect(await screen.findByText(/^at \/evaluations\/review\?run=rs/)).toBeInTheDocument();
  });

  it("ranks its configurations with intervals in Results, and says how", async () => {
    renderOne("/evaluations/compare/results?ids=eb");
    expect(await screen.findByText("1 · Ranking with 95% intervals")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Bundle F1 of each configuration with its 95% interval/ })).toBeInTheDocument();
    expect(screen.getByText("2 · Quality against cost")).toBeInTheDocument();
    expect(screen.getAllByText(/No configuration ran this setup more than once/).length).toBeGreaterThan(0);
    expect(screen.getByText("4 · Methodology")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Export CSV/ })).toBeEnabled();
  });

  it("compares its models run against run, the default model first, and opens both answers", async () => {
    renderOne("/evaluations/compare/pairs?ids=eb");
    expect(await screen.findByRole("heading", { name: "Grounded" })).toBeInTheDocument();
    expect(await screen.findByText("2 · Every run ranked on the same entries")).toBeInTheDocument();
    expect(screen.queryByText("1 · What can be compared")).not.toBeInTheDocument();
    expect(screen.queryByText("4 · Do the shared entries stand for each dataset?")).not.toBeInTheDocument();
    expect(screen.getByText("2 · Every run ranked on the same entries")).toBeInTheDocument();
    // Bundle F1 pooled over the three entries: the default model (opus) 32/109, sonnet 4/109;
    // one agent ran them all, so the runs read by their model alone.
    expect(
      screen.getByText(/On the 3 entries both ran, claude-sonnet-5-5 scores 3\.7 against 29\.4 for claude-opus-5-5 \(/),
    ).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Breadcrumb" })).toHaveTextContent("Run against run");
    fireEvent.click(screen.getByRole("button", { name: /^APT_K_47:.*Click to diff them on this entry/ }));
    expect(await screen.findByText("at /evaluations/review/diff?run=rs&against=rb&entry=k1")).toBeInTheDocument();
  });
});
