// The Configuration tab explains its own fields in place: the judge's model, budget
// and instructions, the baseline, the models' enabled state and per-field scoring, each to
// a keyboard user as to a mouse user.

import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders, testQueryClient } from "@/testing/providers";
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

const ai = vi.hoisted(() => ({ off: false }));
vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => ai.off,
  useAiDisabledReason: () => (ai.off ? "AI is switched off on this platform" : null),
}));

beforeAll(() => {
  if (!("ResizeObserver" in globalThis)) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = vi.fn();
  }
});

const get = vi.mocked(api.get);
const put = vi.mocked(api.put);
const post = vi.mocked(api.post);

const EVALUATION = {
  id: "e-1",
  user_id: "me",
  name: "Alert triage",
  description: null,
  enabled: true,
  family: "STRUCTURED_JSON",
  execution_mode: "full_loop",
  run_mode: "parallel",
  evaluator_model: null,
  evaluator_prompt: null,
  evaluation_instructions: "Grade the severity strictly.",
  sub_score_weights: null,
  quality_source: "judge",
  penalty_factor: 0.5,
  performance_tolerance: 5,
  timeout_seconds: 3600,
  judge_max_tokens: 8192,
  field_scoring_overrides: { field_modes: { "/severity": "enum" }, field_weights: null },
  scorer_selection: null,
  reuse_default: "on",
  apply_prompt_guards: false,
  require_tool_approval: false,
  overall_quality_metrics: null,
  overall_performance_metrics: null,
  trigger_config: { schedule: null, on_agent_change: false },
  use_agent_default_model: true,
  model_ids: ["gpt-x"],
  disabled_model_ids: ["gpt-x"],
  agent_ids: ["a-1"],
  agents: [{ id: "a-1", name: "Alert Triage Analyst", slug: "alert-triage-analyst", is_baseline: true }],
  is_company_managed: false,
  shared_group_ids: [],
  shared_user_ids: [],
  has_active_runs: false,
  last_triggered_at: null,
  created_at: "2026-10-25T09:00:00Z",
  updated_at: "2026-10-25T09:00:00Z",
  placebo_config: null,
};

const FAMILIES = [
  { id: "STRUCTURED_JSON", label: "JSON", description: "Any JSON answer.", metric_keys: [], structured_output: true, extractor_output_kinds: ["array", "object"] },
];

beforeEach(() => {
  vi.clearAllMocks();
  ai.off = false;
  get.mockImplementation(((path: string) => {
    if (path === "/evaluations/e-1") return Promise.resolve(EVALUATION);
    if (path === "/evaluations/families") return Promise.resolve(FAMILIES);
    if (path.startsWith("/evaluations/e-1/runs/count")) return Promise.resolve({ count: 0 });
    return Promise.resolve([]);
  }) as typeof api.get);
});

/** Serves the evaluation with its stored "On agent change" value. */
function withOnAgentChange(on: boolean) {
  const evaluation = { ...EVALUATION, trigger_config: { schedule: null, on_agent_change: on } };
  get.mockImplementation(((path: string) => {
    if (path === "/evaluations/e-1") return Promise.resolve(evaluation);
    if (path === "/evaluations/families") return Promise.resolve(FAMILIES);
    if (path.startsWith("/evaluations/e-1/runs/count")) return Promise.resolve({ count: 0 });
    return Promise.resolve([]);
  }) as typeof api.get);
  put.mockResolvedValue(evaluation);
}

/** Serves *evaluation* in place of the default one: the default with any field replaced. */
function serve(evaluation: Record<string, unknown>) {
  get.mockImplementation(((path: string) => {
    if (path === "/evaluations/e-1") return Promise.resolve(evaluation);
    if (path === "/evaluations/families") return Promise.resolve(FAMILIES);
    if (path.startsWith("/evaluations/e-1/runs/count")) return Promise.resolve({ count: 0 });
    return Promise.resolve([]);
  }) as typeof api.get);
}

function openConfiguration() {
  renderWithProviders(
    <MemoryRouter initialEntries={["/evaluations/e-1/config"]}>
      <Routes>
        <Route path="/evaluations/:id/:tab?" element={<EvaluationDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

/** Tabs to `target` as a keyboard user does, then reads the tooltips it opened. */
async function tooltipByKeyboard(user: ReturnType<typeof userEvent.setup>, target: Element): Promise<string> {
  for (let presses = 0; presses < 200 && document.activeElement !== target; presses++) await user.tab();
  expect(document.activeElement).toBe(target);
  return (await screen.findAllByRole("tooltip")).map((tip) => tip.textContent).join(" ");
}

describe("EvaluationDetail Configuration tab explanations", () => {
  it("explains the judge's model and calls its instructions judge instructions, replacing the datasets'", async () => {
    const user = userEvent.setup();
    openConfiguration();

    expect(await screen.findByText("Grade the severity strictly.")).toBeInTheDocument();
    expect(screen.getByText("Judge Instructions")).toBeInTheDocument();
    expect(screen.queryByText("Evaluation Instructions")).toBeNull();

    expect(await tooltipByKeyboard(user, screen.getByRole("img", { name: "Which model the LLM judge runs on" }))).toContain(
      "providers the evaluation's owner may use",
    );
    await user.keyboard("{Escape}");
    expect(await tooltipByKeyboard(user, screen.getByRole("img", { name: "When the evaluation's judge instructions apply" }))).toContain(
      "They replace the datasets' default judge instructions",
    );
  });

  it("names the baseline switch for its agent", async () => {
    openConfiguration();

    expect(await screen.findByRole("switch", { name: "Baseline: Alert Triage Analyst" })).toBeChecked();
  });

  it("explains each per-field scoring mode while editing", async () => {
    const user = userEvent.setup();
    openConfiguration();
    await user.click(await screen.findByRole("button", { name: /Edit configuration/ }));

    expect(screen.getByRole("combobox", { name: "Field mode" })).toBeInTheDocument();
    expect(await tooltipByKeyboard(user, screen.getByRole("img", { name: "What this field mode does" }))).toContain(
      "the expected value from the allowed list",
    );
  });

  it("offers each per-field scoring mode by its name, never by the id it is saved as", async () => {
    const user = userEvent.setup();
    openConfiguration();
    await user.click(await screen.findByRole("button", { name: /Edit configuration/ }));

    const mode = screen.getByRole("combobox", { name: "Field mode" });
    expect(mode).toHaveTextContent(/^Enum$/);
    await user.click(mode);
    expect((await screen.findAllByRole("option")).map((option) => option.textContent)).toEqual([
      "Derived from schema",
      "Exact",
      "Enum",
      "Enum Set",
      "Free Text",
      "Resource Address",
      "Object List",
      "Excluded",
    ]);

    await user.click(screen.getByRole("option", { name: "Resource Address" }));
    await user.click(screen.getByRole("button", { name: /^Save/ }));
    await waitFor(() => expect(put).toHaveBeenCalled());
    expect(put.mock.calls[0][1]).toMatchObject({
      field_scoring_overrides: { field_modes: { "/severity": "resource_address" } },
    });
  });

  it("offers no On Agent Change, which starts no run yet, to an evaluation that never turned it on", async () => {
    withOnAgentChange(false);
    const user = userEvent.setup();
    openConfiguration();
    await user.click(await screen.findByRole("button", { name: /Edit configuration/ }));

    expect(screen.queryByRole("switch", { name: "On Agent Change" })).toBeNull();
  });

  it("keeps a stored On Agent Change on save, and lets the evaluation turn it off", async () => {
    withOnAgentChange(true);
    const user = userEvent.setup();
    openConfiguration();
    await user.click(await screen.findByRole("button", { name: /Edit configuration/ }));

    await user.click(screen.getByRole("button", { name: /^Save$/ }));
    expect(put.mock.calls[put.mock.calls.length - 1]?.[1]).toMatchObject({ trigger_config: { on_agent_change: true } });

    await user.click(await screen.findByRole("button", { name: /Edit configuration/ }));
    await user.click(screen.getByRole("switch", { name: "On Agent Change" }));
    await user.click(screen.getByRole("button", { name: /^Save$/ }));
    expect(put.mock.calls[put.mock.calls.length - 1]?.[1]).toMatchObject({ trigger_config: { on_agent_change: false } });
  });

  it("marks a saved model no provider offers on the Models card and keeps it out of a Run selection", async () => {
    serve({ ...EVALUATION, model_ids: ["gpt-x", "gone-model"], disabled_model_ids: [], unavailable_model_ids: ["gone-model"] });
    const user = userEvent.setup();
    openConfiguration();

    const card = (await screen.findByText("Additional models:")).closest(".layer-1") as HTMLElement;
    expect(within(card).getByText("Unavailable").closest(".group")).toHaveTextContent("gone-model");
    expect(within(card).getAllByText("Unavailable")).toHaveLength(1);
    expect(await tooltipByKeyboard(user, within(card).getByText("Unavailable").closest('[tabindex="0"]')!)).toContain(
      "a launch that includes it is refused",
    );

    await user.click(screen.getByRole("button", { name: "Open run options" }));
    expect(screen.getByRole("button", { name: /gone-model/ })).toBeDisabled();
    const served = screen.getByRole("button", { name: "gpt-x" });
    await user.click(served);
    expect(served).toHaveAttribute("aria-pressed", "true");
  });

  it("offers the owner's models to add, not the viewer's", async () => {
    get.mockImplementation(((path: string) => {
      if (path === "/evaluations/e-1") return Promise.resolve(EVALUATION);
      if (path === "/evaluations/e-1/available-models") return Promise.resolve([{ model: "owner-extra", provider: "openai" }]);
      if (path.startsWith("/settings/available-models")) return Promise.resolve([{ model: "mine", provider: "openai" }]);
      if (path === "/evaluations/families") return Promise.resolve(FAMILIES);
      if (path.startsWith("/evaluations/e-1/runs/count")) return Promise.resolve({ count: 0 });
      return Promise.resolve([]);
    }) as typeof api.get);
    const user = userEvent.setup();
    openConfiguration();
    await user.click(await screen.findByRole("button", { name: /Edit configuration/ }));

    await user.click(screen.getByRole("combobox", { name: "Search models to add..." }));
    expect((await screen.findAllByRole("option")).map((option) => option.textContent)).toEqual(["owner-extra"]);
  });

  it("reads the owner's models when the Run menu opens, so one with no saved model can still run on one", async () => {
    get.mockImplementation(((path: string) => {
      if (path === "/evaluations/e-1") return Promise.resolve({ ...EVALUATION, use_agent_default_model: false, model_ids: [], disabled_model_ids: [] });
      if (path === "/evaluations/e-1/available-models") return Promise.resolve([{ model: "owner-extra", provider: "openai" }]);
      if (path === "/evaluations/families") return Promise.resolve(FAMILIES);
      if (path.startsWith("/evaluations/e-1/runs/count")) return Promise.resolve({ count: 0 });
      return Promise.resolve([]);
    }) as typeof api.get);
    const user = userEvent.setup();
    renderWithProviders(
      <MemoryRouter initialEntries={["/evaluations/e-1/activity"]}>
        <Routes>
          <Route path="/evaluations/:id/:tab?" element={<EvaluationDetail />} />
        </Routes>
      </MemoryRouter>,
    );

    const options = await screen.findByRole("button", { name: "Open run options" });
    expect(get.mock.calls.map(([path]) => path)).not.toContain("/evaluations/e-1/available-models");
    await user.click(options);
    expect(await screen.findByRole("button", { name: /owner-extra/ })).toBeInTheDocument();
  });

  it("drops a selected model from the Run selection once the server flags it", async () => {
    let flagged: string[] = [];
    get.mockImplementation(((path: string) => {
      if (path === "/evaluations/e-1") return Promise.resolve({ ...EVALUATION, disabled_model_ids: [], unavailable_model_ids: flagged });
      if (path === "/evaluations/families") return Promise.resolve(FAMILIES);
      if (path.startsWith("/evaluations/e-1/runs/count")) return Promise.resolve({ count: 0 });
      return Promise.resolve([]);
    }) as typeof api.get);
    const user = userEvent.setup();
    const { client } = renderWithProviders(
      <MemoryRouter initialEntries={["/evaluations/e-1/activity"]}>
        <Routes>
          <Route path="/evaluations/:id/:tab?" element={<EvaluationDetail />} />
        </Routes>
      </MemoryRouter>,
    );

    await user.click(await screen.findByRole("button", { name: "Open run options" }));
    await user.click(screen.getByRole("button", { name: "gpt-x" }));
    expect(screen.getByText("1 selected")).toBeInTheDocument();

    flagged = ["gpt-x"];
    await client.invalidateQueries();
    await waitFor(() => expect(screen.getByRole("button", { name: /gpt-x/ })).toBeDisabled());
    expect(screen.getByRole("button", { name: /gpt-x/ })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByText("0 selected")).toBeInTheDocument();
  });

  // Saved scoring settings read back after the save.
  /** Each sub-score's weight as the Sub-Score Weights block shows it. */
  function shownWeights(block: HTMLElement): Record<string, string> {
    return Object.fromEntries(
      within(block).getAllByRole("definition").map((weight) => [weight.previousElementSibling?.textContent ?? "", weight.textContent ?? ""]),
    );
  }
  const weightsBlock = async () => (await screen.findByText("Sub-Score Weights")).closest("div.space-y-2") as HTMLElement;

  it("shows the saved sub-score weights the judge applies, read-only", async () => {
    serve({
      ...EVALUATION,
      sub_score_weights: { tool_selection_quality: 0.4, response_completeness: 0.3, response_accuracy: 0.2, instruction_adherence: 0.1 },
    });
    openConfiguration();

    expect(shownWeights(await weightsBlock())).toEqual({
      "Tool Selection": "40%",
      Completeness: "30%",
      Accuracy: "20%",
      "Instruction Adherence": "10%",
      Coherence: "0%",
    });
  });

  it.each([
    ["none", null],
    ["an empty set of", {}],
  ])("shows 20%% each for an evaluation that saved %s sub-score weights, and keeps them read-only while editing", async (_saved, weights) => {
    serve({ ...EVALUATION, sub_score_weights: weights });
    const user = userEvent.setup();
    openConfiguration();

    expect(Object.values(shownWeights(await weightsBlock()))).toEqual(["20%", "20%", "20%", "20%", "20%"]);
    await user.click(screen.getByRole("button", { name: /Edit configuration/ }));
    const block = await weightsBlock();
    expect(Object.values(shownWeights(block))).toEqual(["20%", "20%", "20%", "20%", "20%"]);
    expect(within(block).queryByRole("spinbutton")).toBeNull();
  });

  it("shows weights saved as thirds as whole percents that add up to 100%", async () => {
    serve({ ...EVALUATION, sub_score_weights: { tool_selection_quality: 1 / 3, response_completeness: 1 / 3, response_accuracy: 1 / 3 } });
    openConfiguration();

    expect(Object.values(shownWeights(await weightsBlock()))).toEqual(["34%", "33%", "33%", "0%", "0%"]);
  });

  it("shows no sub-score weights while the judge is off", async () => {
    serve({ ...EVALUATION, scorer_selection: [] });
    openConfiguration();

    expect(await screen.findByText("Per-Field Scoring")).toBeInTheDocument();
    expect(screen.queryByText("Sub-Score Weights")).toBeNull();
  });

  it("lists the saved per-field scoring overrides outside edit mode", async () => {
    serve({
      ...EVALUATION,
      field_scoring_overrides: { field_modes: { "/severity": "enum", "/notes": "excluded" }, field_weights: { "/severity": 2 } },
    });
    openConfiguration();

    const rows = (await screen.findAllByRole("listitem")).map((row) => row.textContent);
    expect(rows).toEqual(["/notesExcluded", "/severityEnumweight 2"]);
  });

  it("says no field is overridden when none is", async () => {
    serve({ ...EVALUATION, field_scoring_overrides: null });
    openConfiguration();

    expect(await screen.findByText("No override: every field is scored according to its schema declaration.")).toBeInTheDocument();
  });

  it("names each linked dataset's family and marks one that blocks a launch or conflicts", async () => {
    const linked = (name: string, family: string | null, conflict: string | null = null) => ({
      id: `l-${name}`,
      dataset_id: `d-${name}`,
      dataset_name: name,
      dataset_description: null,
      entry_count: 3,
      position: 0,
      created_at: "2026-10-25T09:00:00Z",
      family,
      needs_family: family === null,
      family_conflict: conflict,
    });
    get.mockImplementation(((path: string) => {
      if (path === "/evaluations/e-1") return Promise.resolve(EVALUATION);
      if (path === "/evaluations/families") return Promise.resolve(FAMILIES);
      if (path === "/datasets/for-evaluation/e-1")
        return Promise.resolve([
          linked("Matching", "STRUCTURED_JSON"),
          linked("No family", null),
          linked("Changed", "PROSE", "Dataset family 'PROSE' conflicts with evaluation family 'STRUCTURED_JSON'."),
        ]);
      if (path.startsWith("/evaluations/e-1/runs/count")) return Promise.resolve({ count: 0 });
      return Promise.resolve([]);
    }) as typeof api.get);
    const user = userEvent.setup();
    renderWithProviders(
      <MemoryRouter initialEntries={["/evaluations/e-1/dataset"]}>
        <Routes>
          <Route path="/evaluations/:id/:tab?" element={<EvaluationDetail />} />
        </Routes>
      </MemoryRouter>,
    );

    const card = async (name: string) => (await screen.findByText(name)).closest(".layer-1") as HTMLElement;
    const matching = await card("Matching");
    expect(within(matching).getByText("JSON")).toBeInTheDocument();
    expect(within(matching).queryByText("Family conflict")).toBeNull();

    const missing = await card("No family");
    expect(await tooltipByKeyboard(user, within(missing).getByText("Needs family").closest('[tabindex="0"]')!)).toContain(
      "cannot run, replay or rescore",
    );

    const changed = await card("Changed");
    expect(within(changed).getByText("PROSE")).toBeInTheDocument();
    expect(await tooltipByKeyboard(user, within(changed).getByText("Family conflict").closest('[tabindex="0"]')!)).toContain(
      "This dataset's output family, PROSE, differs from the evaluation's, JSON,",
    );
  });

  it("marks a dataset with no family with a Needs family icon in the Link Dataset list, and the row explains it", async () => {
    const dataset = (name: string, family: string | null) => ({
      id: `d-${name}`,
      name,
      description: null,
      entry_count: 2,
      family,
      needs_family: family === null,
    });
    get.mockImplementation(((path: string) => {
      if (path === "/evaluations/e-1") return Promise.resolve(EVALUATION);
      if (path === "/evaluations/families") return Promise.resolve(FAMILIES);
      if (path.startsWith("/datasets?")) return Promise.resolve([dataset("Ready", "STRUCTURED_JSON"), dataset("Unset", null)]);
      if (path.startsWith("/evaluations/e-1/runs/count")) return Promise.resolve({ count: 0 });
      return Promise.resolve([]);
    }) as typeof api.get);
    const user = userEvent.setup();
    renderWithProviders(
      <MemoryRouter initialEntries={["/evaluations/e-1/dataset"]}>
        <Routes>
          <Route path="/evaluations/:id/:tab?" element={<EvaluationDetail />} />
        </Routes>
      </MemoryRouter>,
    );

    await user.click(await screen.findByRole("button", { name: "Link Dataset" }));
    const unset = await screen.findByRole("button", { name: /Unset/ });
    expect(within(unset).getByRole("img", { name: "Needs family" })).toBeInTheDocument();
    expect(within(screen.getByRole("button", { name: /Ready/ })).queryByRole("img", { name: "Needs family" })).toBeNull();
    // The mark is part of the row, not a stop of its own inside the button.
    expect(unset.querySelector('[tabindex="0"]')).toBeNull();
    await user.hover(unset);
    expect((await screen.findAllByRole("tooltip")).map((tip) => tip.textContent).join(" ")).toContain(
      "cannot run, replay or rescore",
    );
  });

  // The server says why it refuses a launch, once: the app's own error toast stays quiet.
  it.each([
    ["Run", [] as string[]],
    ["Run Selected (1)", ["gpt-x"]],
  ])("says once why the server refuses %s", async (button, picked) => {
    const reason = "Model gone-model is not offered by any provider of the evaluation's owner";
    serve({ ...EVALUATION, disabled_model_ids: [] });
    post.mockRejectedValue(new Error(reason));
    const user = userEvent.setup();
    renderWithProviders(
      <MemoryRouter initialEntries={["/evaluations/e-1/activity"]}>
        <Routes>
          <Route path="/evaluations/:id/:tab?" element={<EvaluationDetail />} />
        </Routes>
      </MemoryRouter>,
      { client: testQueryClient({ appMutationCache: true }) },
    );

    if (picked.length) {
      await user.click(await screen.findByRole("button", { name: "Open run options" }));
      for (const model of picked) await user.click(screen.getByRole("button", { name: model }));
    }
    await user.click(await screen.findByRole("button", { name: button }));
    expect(await screen.findAllByText(reason)).toHaveLength(1);
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("launches nothing from the Run menu while AI is off, and says why", async () => {
    ai.off = true;
    serve({ ...EVALUATION, disabled_model_ids: [] });
    const user = userEvent.setup();
    renderWithProviders(
      <MemoryRouter initialEntries={["/evaluations/e-1/activity"]}>
        <Routes>
          <Route path="/evaluations/:id/:tab?" element={<EvaluationDetail />} />
        </Routes>
      </MemoryRouter>,
    );

    await user.click(await screen.findByRole("button", { name: "Open run options" }));
    await user.click(screen.getByRole("button", { name: "gpt-x" }));
    const runAll = screen.getByRole("button", { name: "Run All" });
    const runSelected = screen.getByRole("button", { name: "Run Selected (1)" });
    expect(runAll).toBeDisabled();
    expect(runSelected).toBeDisabled();
    expect(await tooltipByKeyboard(user, runSelected.closest('[tabindex="0"]')!)).toContain("AI is switched off on this platform");
    expect(post).not.toHaveBeenCalled();
  });
});
