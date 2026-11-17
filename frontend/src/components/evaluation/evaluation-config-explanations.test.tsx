// The evaluation creation page's and Configuration tab's cards explain what they show in
// place: each explanation opens on hover and on keyboard focus, and the texts
// corrected against the backend say what it does.

import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { EvaluationRunDetail, ExtractorSpec, MetricCatalogEntry } from "@/types";
import { EvaluationSettingsCard, type EvaluationSettingsCardProps } from "./evaluation-settings-card";
import { EvaluationScoringCard, type EvaluationScoringCardProps } from "./evaluation-scoring-card";
import { OverallScoreComposition } from "./metric-selector";
import { EvaluationAgentPickerCard } from "./evaluation-agent-picker-card";
import { EvaluationModelPickerCard } from "./evaluation-model-picker-card";
import { EvaluationExtractorCard } from "./evaluation-extractor-card";
import { PlaceboConfigCard } from "./PlaceboConfigCard";
import { PlaceboSnapshotCard } from "./PlaceboSnapshotCard";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => false,
}));

/** The searchable lists are positioned with floating-ui, which happy-dom does not lay out. */
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
const post = vi.mocked(api.post);

beforeEach(() => {
  vi.clearAllMocks();
  get.mockImplementation(((path: string) =>
    Promise.resolve(path.startsWith("/skills") ? [{ id: "s-1", name: "Triage Checklist" }] : [])) as typeof api.get);
});

/** Tabs to `target` as a keyboard user does, then reads the tooltip it opens. */
async function tooltipByKeyboard(user: ReturnType<typeof userEvent.setup>, target: Element): Promise<string> {
  for (let presses = 0; presses < 80 && document.activeElement !== target; presses++) await user.tab();
  expect(document.activeElement).toBe(target);
  return (await screen.findByRole("tooltip")).textContent ?? "";
}

/** The tab stop that explains `text`: the term itself, or the wrapper around its badge. */
const termStop = (text: string) => screen.getByText(text).closest('[tabindex="0"]')!;

const noop = () => {};

function settingsProps(over: Partial<EvaluationSettingsCardProps> = {}): EvaluationSettingsCardProps {
  return {
    editing: true,
    name: "Eval",
    onNameChange: noop,
    description: "",
    onDescriptionChange: noop,
    executionMode: "full_loop",
    onExecutionModeChange: noop,
    runMode: "parallel",
    onRunModeChange: noop,
    timeoutSeconds: 120,
    onTimeoutSecondsChange: noop,
    scheduleEnabled: true,
    onScheduleEnabledChange: noop,
    schedule: "0 8 * * *",
    onScheduleChange: noop,
    reuseDefault: true,
    onReuseDefaultChange: noop,
    applyPromptGuards: false,
    onApplyPromptGuardsChange: noop,
    requireToolApproval: true,
    onRequireToolApprovalChange: noop,
    agentIds: [],
    ...over,
  };
}

describe("EvaluationSettingsCard explanations", () => {
  it("says the schedule is read in UTC, presets included", async () => {
    const user = userEvent.setup();
    renderWithProviders(<EvaluationSettingsCard {...settingsProps()} />);

    const tip = await tooltipByKeyboard(user, screen.getByRole("img", { name: "Which time zone the schedule uses" }));
    expect(tip).toContain("Daily at 8 AM starts at 08:00 UTC");
  });

  it("names the timeout's budget without the callback sentence it no longer makes", async () => {
    const user = userEvent.setup();
    renderWithProviders(<EvaluationSettingsCard {...settingsProps()} />);

    const tip = await tooltipByKeyboard(user, screen.getByRole("img", { name: "What the execution timeout covers" }));
    expect(tip).toContain("all its runs together");
    expect(tip).not.toContain("callback");
  });

  it("reads a stored schedule in words and in UTC, and explains a paused one", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <EvaluationSettingsCard
        {...settingsProps({
          editing: false,
          evaluationSnapshot: {
            execution_mode: "full_loop",
            run_mode: "parallel",
            timeout_seconds: 120,
            schedule: "0 8 * * *",
            automatic_runs: false,
            reuse_default: "on",
            apply_prompt_guards: false,
            require_tool_approval: false,
          },
        })}
      />,
    );

    expect(await tooltipByKeyboard(user, termStop("0 8 * * * UTC"))).toContain("Starts a launch on this schedule: Daily at 8 AM, read in UTC.");
    await user.keyboard("{Escape}");
    expect(await tooltipByKeyboard(user, termStop("Paused"))).toContain("Automatic runs are off");
  });

  it("describes a custom schedule without the English-only cron describer", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <EvaluationSettingsCard
        {...settingsProps({
          editing: false,
          evaluationSnapshot: {
            execution_mode: "full_loop",
            run_mode: "parallel",
            timeout_seconds: 120,
            schedule: "15 3 * * 2",
            automatic_runs: true,
            reuse_default: "on",
            apply_prompt_guards: false,
            require_tool_approval: false,
          },
        })}
      />,
    );

    expect(await tooltipByKeyboard(user, termStop("15 3 * * 2 UTC"))).toBe("Starts a launch on this cron schedule, read in UTC.");
  });

  it("explains prediction reuse in read-only mode too", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <EvaluationSettingsCard
        {...settingsProps({
          editing: false,
          evaluationSnapshot: {
            execution_mode: "full_loop",
            run_mode: "parallel",
            timeout_seconds: 120,
            schedule: null,
            reuse_default: "on",
            apply_prompt_guards: false,
            require_tool_approval: false,
          },
        })}
      />,
    );

    const tip = await tooltipByKeyboard(user, screen.getByRole("img", { name: "What prediction reuse copies" }));
    expect(tip).toContain("Only successful answers are copied");
  });
});

const FAMILIES = [{ id: "PROSE", label: "Prose", description: "Natural-language prose.", metric_keys: [] }];

function scoringProps(over: Partial<EvaluationScoringCardProps> = {}): EvaluationScoringCardProps {
  return {
    editing: true,
    familyCatalog: FAMILIES,
    family: "PROSE",
    onFamilyChange: noop,
    judgeEnabled: true,
    onJudgeEnabledChange: noop,
    performanceTolerance: 5,
    onPerformanceToleranceChange: noop,
    penaltyFactor: 0.5,
    onPenaltyFactorChange: noop,
    subScoreWeights: { tool_selection_quality: 1, response_completeness: 1, response_accuracy: 1, instruction_adherence: 1, coherence: 1 },
    onSubScoreWeightChange: noop,
    subScoreTotal: 5,
    ...over,
  };
}

describe("EvaluationScoringCard explanations", () => {
  it("says the LLM judge is optional for every family and what turning it off removes", () => {
    renderWithProviders(<EvaluationScoringCard {...scoringProps()} />);

    expect(screen.getByText(/Off, no judge runs and Judge Quality reads N\/A/)).toBeInTheDocument();
    expect(screen.queryByText(/Required for families/)).toBeNull();
  });

  it("works the consistency penalty example with the population standard deviation", () => {
    renderWithProviders(<EvaluationScoringCard {...scoringProps()} />);

    // 85, 92, 70: mean 82.3, population std 9.18; with k = 0.5, 82.3 - 4.6 = 77.7.
    expect(screen.getByText(/std = 9\.2/)).toBeInTheDocument();
    expect(screen.getByText("77.7")).toBeInTheDocument();
  });

  it("explains each judge sub-score by what the judge asks", async () => {
    const user = userEvent.setup();
    renderWithProviders(<EvaluationScoringCard {...scoringProps()} />);

    expect(await tooltipByKeyboard(user, termStop("Accuracy"))).toContain("no wrong claim or contradiction");
  });

  it("explains the judge and the family in read-only mode", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <EvaluationScoringCard
        {...scoringProps({ editing: false, judgeConfigured: false, evaluationSnapshot: { family: "PROSE", penalty_factor: 0.5, performance_tolerance: 5 } })}
      />,
    );

    expect(await tooltipByKeyboard(user, screen.getByRole("img", { name: "What the LLM judge scores" }))).toContain("Off, no judge runs");
    await user.keyboard("{Escape}");
    expect(await tooltipByKeyboard(user, termStop("Prose"))).toContain("Natural-language prose.");
  });
});

describe("OverallScoreComposition explanations", () => {
  const CATALOG: MetricCatalogEntry[] = [
    { key: "prose.exact_match", label: "Exact", kind: "quality", direction: "higher_is_better", default_contributor: true },
    { key: "prose.rouge_l", label: "Rouge", kind: "quality", direction: "higher_is_better", default_contributor: false },
  ];

  it("says what each overall score is, and why a metric is not counted, to a keyboard user", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <OverallScoreComposition catalog={CATALOG} quality={["prose.exact_match"]} performance={null} onQualityChange={noop} onPerformanceChange={noop} />,
    );

    expect(await tooltipByKeyboard(user, termStop("Overall Quality"))).toContain("deterministic quality metrics");
    await user.keyboard("{Escape}");
    expect(await tooltipByKeyboard(user, termStop("not counted by default"))).toContain("Not part of the default");
  });
});

describe("EvaluationAgentPickerCard explanations", () => {
  it("explains the baseline badge to a keyboard user", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <EvaluationAgentPickerCard
        agents={[{ id: "a-1", name: "Triage", slug: null, is_baseline: true }]}
        boundIds={["a-1"]}
        canManage={false}
        editing={false}
        onAdd={noop}
        onRemove={noop}
      />,
    );

    expect(await tooltipByKeyboard(user, termStop("Baseline"))).toContain("it changes no score");
  });
});

describe("EvaluationModelPickerCard explanations", () => {
  const props = {
    onUseAgentDefaultModelChange: noop,
    availableModelsToAdd: [],
    onAddModel: noop,
    onRemoveModel: noop,
    evaluationUseAgentDefaultModel: true,
  };

  it("says why the agent's own model cannot be turned off while no model is added", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <EvaluationModelPickerCard {...props} editing useAgentDefaultModel canDisableDefaultModel={false} selectedModels={[]} />,
    );

    const toggle = screen.getByRole("switch", { name: "Use agent's own model" });
    expect(toggle).toBeDisabled();
    expect(await tooltipByKeyboard(user, toggle.closest('[tabindex="0"]')!)).toContain("Add a model first");
  });

  it("explains a model's enabled state", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <EvaluationModelPickerCard
        {...props}
        editing={false}
        useAgentDefaultModel
        canDisableDefaultModel
        selectedModels={[{ model: "gpt-4o", provider: "openai", disabled: true }]}
      />,
    );

    expect(await tooltipByKeyboard(user, termStop("Disabled"))).toContain("A disabled one is skipped");
  });
});

describe("EvaluationExtractorCard explanations", () => {
  const EXTRACTOR: ExtractorSpec = { source: { kind: "agent_output" }, operations: [{ op: "find_json" }], output_kind: "object" };

  function renderCard() {
    renderWithProviders(
      <EvaluationExtractorCard
        target={{ evaluationId: "eval-1" }}
        agents={[{ id: "agent-1", name: "Triage" }]}
        extractors={{ "agent-1": EXTRACTOR }}
        canManage
        onChange={noop}
      />,
    );
  }

  async function preview(user: ReturnType<typeof userEvent.setup>) {
    await user.type(screen.getByLabelText("Sample output to preview for Triage"), "text");
    await user.click(screen.getByRole("button", { name: "Preview" }));
  }

  it("explains each operation to a keyboard user", async () => {
    const user = userEvent.setup();
    renderCard();

    expect(await tooltipByKeyboard(user, screen.getByRole("img", { name: "What operation 1 does" }))).toContain(
      "the first code block holding JSON, else the outermost object or array",
    );
  });

  it("names a failure reason in words, not by its code", async () => {
    const user = userEvent.setup();
    post.mockResolvedValue({
      status: "failed",
      prediction: null,
      output_kind: "object",
      failure: { reason: "payload_unparseable", message: "no JSON object or array found in the text" },
    });
    renderCard();
    await preview(user);

    expect(await screen.findByText(/Extraction failed — Not valid JSON: no JSON object/)).toBeInTheDocument();
    expect(screen.queryByText(/payload_unparseable/)).toBeNull();
  });

  it("says a failed request failed, never that the answer had the wrong type", async () => {
    const user = userEvent.setup();
    post.mockRejectedValue(new Error("Service unavailable"));
    renderCard();
    await preview(user);

    expect(await screen.findByText("Preview failed: Service unavailable")).toBeInTheDocument();
    expect(screen.queryByText(/Extraction failed/)).toBeNull();
  });
});

describe("PlaceboConfigCard explanations", () => {
  it("defines an arm and the targets in the stored configuration", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <PlaceboConfigCard
        evaluation={{
          id: "e-1",
          execution_mode: "full_loop",
          placebo_config: { targets: [{ kind: "persona", skill_id: null }], include_no_target: true, trials_per_entry: 3, custom_text: null },
        }}
        canManage={false}
      />,
    );

    expect(await tooltipByKeyboard(user, screen.getByRole("img", { name: "What an arm is" }))).toContain("real, placebo or no target");
    await user.keyboard("{Escape}");
    expect(await tooltipByKeyboard(user, screen.getByRole("img", { name: "What a placebo control compares" }))).toContain(
      "neutral text of the same length",
    );
  });

  it("says why a Prompts target cannot be chosen in LLM Only", async () => {
    const user = userEvent.setup();
    renderWithProviders(<PlaceboConfigCard evaluation={{ id: "e-1", execution_mode: "llm_only", placebo_config: null }} canManage />);
    await user.click(screen.getByRole("button", { name: "Set up" }));

    const toggle = screen.getByRole("switch", { name: "Attached Prompts" });
    expect(toggle).toBeDisabled();
    expect(await tooltipByKeyboard(user, toggle.closest('[tabindex="0"]')!)).toContain("need the Full Loop execution mode");
  });
});

describe("PlaceboSnapshotCard explanations", () => {
  const RUN: Pick<EvaluationRunDetail, "arm" | "placebo_target" | "placebo_snapshot"> = {
    arm: "placebo",
    placebo_target: { kind: "persona", skill_id: null },
    placebo_snapshot: {
      kind: "persona",
      elements: [
        {
          name: "",
          description: "",
          content: "Neutral text.",
          always_load: false,
          ref_id: null,
          version: null,
          lengths: { content: { original: 100, placebo: 90, deviation: -0.1, within_tolerance: false, repeated: false } },
        },
      ],
      language: "en",
      language_detected: true,
      corpus_version: "v1",
      seed: "abc123",
      unit: "tokens",
      custom_text: false,
      warnings: [],
      leakage_check: "passed",
    },
  };

  it("explains the seed, and says a part outside tolerance in words as well as colour", async () => {
    const user = userEvent.setup();
    renderWithProviders(<PlaceboSnapshotCard run={RUN} />);

    expect(screen.getByText("outside tolerance")).toBeInTheDocument();
    expect(await tooltipByKeyboard(user, screen.getByRole("img", { name: "What Placebo seed means" }))).toContain(
      "a replay rebuilds the same placebo",
    );
    await user.keyboard("{Escape}");
    expect(await tooltipByKeyboard(user, termStop("Deviation"))).toContain("Beyond ±5 %");
  });
});
