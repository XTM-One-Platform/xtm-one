// A placebo comparison on the evaluation page (FR-28): its runs are tagged, never re-run
// alone, and rank no configuration on the Overview, which shows its block instead.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import userEvent from "@testing-library/user-event";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { EvaluationRun } from "@/types";
import EvaluationDetail, { OverviewTab } from "./EvaluationDetail";

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
const post = vi.mocked(api.post);

function run(id: string, over: Partial<EvaluationRun> = {}): EvaluationRun {
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
    score: 80,
    speed_score: null,
    overall_score: null,
    f1_score: null,
    quality_score: 80,
    performance_score: null,
    quality_metric_keys: [],
    performance_metric_keys: [],
    quality_contributions: {},
    performance_contributions: {},
    quality_aggregation_excluded: [],
    performance_aggregation_excluded: [],
    quality_source: "judge",
    duration_seconds: 1,
    avg_sub_scores: {},
    completed_entries: 2,
    total_entries: 2,
    error: null,
    started_at: "2026-10-25T10:00:00Z",
    completed_at: "2026-10-25T10:05:00Z",
    created_at: "2026-10-25T10:00:00Z",
    can_replay: true,
    arm: "real",
    trial_index: 0,
    placebo_target: null,
    placebo_comparison_id: "c-1",
    ...over,
  };
}

const COMPARISON_RUNS = [
  run("r-real"),
  run("r-placebo", { arm: "placebo", placebo_target: { kind: "persona", skill_id: null }, quality_score: 70 }),
  run("r-no-target", { arm: "no_target", placebo_target: { kind: "persona", skill_id: null }, quality_score: 40 }),
];

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
  quality_source: "judge",
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
  placebo_config: { targets: [{ kind: "persona", skill_id: null }], include_no_target: true, trials_per_entry: 1, custom_text: null },
};

function answer(runs: EvaluationRun[], evaluation: Record<string, unknown> = {}) {
  get.mockImplementation(((path: string) => {
    if (path === "/evaluations/e-1") return Promise.resolve({ ...EVALUATION, ...evaluation });
    if (path.startsWith("/evaluations/e-1/runs/count")) return Promise.resolve({ count: runs.length });
    // As the server does: `exclude_comparisons` leaves a comparison's runs out.
    if (path.startsWith("/evaluations/e-1/runs")) {
      return Promise.resolve(path.includes("exclude_comparisons=true") ? runs.filter((r) => !r.placebo_comparison_id) : runs);
    }
    if (path.startsWith("/evaluations/e-1/placebo-comparisons")) return Promise.resolve([]);
    return Promise.resolve([]);
  }) as typeof api.get);
}

beforeEach(() => {
  vi.clearAllMocks();
  post.mockResolvedValue([run("r-new", { placebo_comparison_id: null })]);
});

function openActivity() {
  renderWithProviders(
    <MemoryRouter initialEntries={["/evaluations/e-1/activity"]}>
      <Routes>
        <Route path="/evaluations/:id/:tab?" element={<EvaluationDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("EvaluationDetail with a placebo comparison", () => {
  it("tags each arm, and re-runs only the selected runs outside the comparison", async () => {
    answer([run("r-plain", { placebo_comparison_id: null, arm: "real" }), ...COMPARISON_RUNS]);
    openActivity();

    expect(await screen.findByText("Placebo · Persona")).toBeInTheDocument();
    expect(screen.getByText("No target · Persona")).toBeInTheDocument();
    expect(screen.getByText("Real")).toBeInTheDocument();
    for (const toggle of screen.getAllByRole("switch", { name: "Select all completed runs" })) fireEvent.click(toggle);

    expect(screen.getByRole("button", { name: /Compare \(4\)/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Re-run full \(1\)/ }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Re-run full" }));

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post.mock.calls[0]).toEqual(["/evaluations/e-1/trigger-from-runs", { run_ids: ["r-plain"] }]);
  });

  it("offers no re-run when only a comparison's runs are selected", async () => {
    answer(COMPARISON_RUNS);
    openActivity();

    await screen.findByText("Placebo · Persona");
    for (const toggle of screen.getAllByRole("switch", { name: "Select all completed runs" })) fireEvent.click(toggle);

    expect(screen.getByRole("button", { name: /Compare \(3\)/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Re-run full/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Re-score/ })).toBeNull();
  });

  it("ranks no comparison run on the Overview", async () => {
    answer(COMPARISON_RUNS);
    renderWithProviders(
      <MemoryRouter>
        <OverviewTab evaluationId="e-1" />
      </MemoryRouter>,
    );

    expect(await screen.findByText("No results yet")).toBeInTheDocument();
    await waitFor(() =>
      expect(get.mock.calls.some(([path]) => String(path).startsWith("/evaluations/e-1/placebo-comparisons"))).toBe(true),
    );
    expect(screen.queryByText("Leader")).toBeNull();
    // The server leaves the comparison's runs out, so they never fill the 100 runs ranked.
    expect(get.mock.calls.map(([path]) => path)).toContain("/evaluations/e-1/runs?limit=100&offset=0&exclude_comparisons=true");
  });
});

describe("a comparison run that cannot be re-run alone", () => {
  it("is not flagged like a run whose replay is broken", async () => {
    answer([
      run("r-broken", { placebo_comparison_id: null, can_replay: false, replay_block_reason: "Replay impossible: missing version snapshots (prompt p v1)" }),
      run("r-placebo", {
        arm: "placebo",
        placebo_target: { kind: "persona", skill_id: null },
        can_replay: false,
        replay_block_reason: "A placebo comparison's run cannot be re-run on its own: launch a new placebo comparison",
      }),
    ]);
    openActivity();

    await screen.findByText("Placebo · Persona");
    expect(screen.getAllByText("No replay")).toHaveLength(1);
  });
});

describe("the evaluation page explains itself", () => {
  it("says Re-run full replays each selected run's own versions", async () => {
    answer([run("r-plain", { placebo_comparison_id: null })]);
    openActivity();

    fireEvent.click((await screen.findAllByRole("switch", { name: "Select all completed runs" }))[0]);
    fireEvent.focus(screen.getByRole("button", { name: /Re-run full \(1\)/ }));
    expect((await screen.findByRole("tooltip")).textContent).toContain("replaying its own versions and pinned dataset versions");
  });

  it("says what started a launch and how its runs are grouped", async () => {
    answer([run("r-plain", { placebo_comparison_id: null })]);
    openActivity();

    fireEvent.focus(await screen.findByText("Manual"));
    const tip = (await screen.findByRole("tooltip")).textContent;
    expect(tip).toContain("Started on request");
    expect(tip).toContain("same minute");
  });

  it("explains a disabled Run to a keyboard user when no output family is set", async () => {
    const user = userEvent.setup();
    answer([], { family: null });
    openActivity();

    const wrapper = (await screen.findByRole("button", { name: "Run" })).closest('[tabindex="0"]');
    expect(wrapper).not.toBeNull();
    for (let presses = 0; presses < 20 && document.activeElement !== wrapper; presses++) await user.tab();
    expect(document.activeElement).toBe(wrapper);
    expect((await screen.findByRole("tooltip")).textContent).toContain("Set an output family in the Configuration tab");
  });

  it("explains the company-managed badge, which swallows a tooltip of its own", async () => {
    answer([], { is_company_managed: true });
    openActivity();

    fireEvent.focus((await screen.findByText("Company-managed")).closest('[tabindex="0"]')!);
    expect((await screen.findByRole("tooltip")).textContent).toContain("only administrators can change it");
  });

  it("says the Leader ignores the chart range", async () => {
    answer([run("r-plain", { placebo_comparison_id: null })]);
    renderWithProviders(
      <MemoryRouter>
        <OverviewTab evaluationId="e-1" />
      </MemoryRouter>,
    );

    fireEvent.focus((await screen.findAllByText("Leader"))[0]);
    expect((await screen.findByRole("tooltip")).textContent).toContain("It ignores the chart range.");
  });
});
