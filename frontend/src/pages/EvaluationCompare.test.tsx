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

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
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
    { run_id: "ra", entry_key: "k1", result_id: "res-a1", counted: true, anomaly: null, metrics: { "stix.entity_prf": [0, 34], "stix.relationship_prf": [0, 37] }, cost: 0.9, seconds: 60 },
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
          <Route path="/evaluations/review" element={<Where />} />
        </Routes>
      </MemoryRouter>,
    );
  }

  it("says what can be compared before comparing it", async () => {
    renderPage();
    expect(await screen.findByRole("heading", { name: "STIX Extraction vs Grounded" })).toBeInTheDocument();
    expect(screen.getByText("STIX_BUNDLE · 2 entries in common")).toBeInTheDocument();
    const agent = screen.getByRole("row", { name: /^Agent/ });
    expect(agent).toHaveTextContent("Under test");
    expect(screen.getByRole("row", { name: /^Input/ })).toHaveTextContent("Changed too");
    expect(screen.getByRole("row", { name: /^Judge/ })).toHaveTextContent("Not comparable");
    expect(screen.getByRole("row", { name: /^Metric suite/ })).toHaveTextContent("re-score the older runs");
  });

  it("pools the same model's counts on the shared entries and opens an entry's two answers", async () => {
    renderPage();
    // Entity F1 pooled over the two shared entries: 4/48 → 18/48, never the mean of entry values.
    expect(await screen.findByText(/scores 37\.5 in Grounded against 8\.3 in STIX Extraction/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^APT_K_47:.*Open both answers side by side/ }));
    expect(await screen.findByText("at /evaluations/review?results=res-a1,res-b1")).toBeInTheDocument();
  });

  it("checks the shared entries against each evaluation's others", async () => {
    renderPage();
    const row = await screen.findByRole("row", { name: /claude-opus-5-5 · Grounded/ });
    // 23/58 on all three entries, 18/48 shared, 5/10 on its own entry.
    expect(row).toHaveTextContent("39.7");
    expect(row).toHaveTextContent("37.5");
    expect(row).toHaveTextContent("50.0");
  });
});
