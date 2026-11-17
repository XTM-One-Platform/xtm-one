// Run detail: every concept explains itself in place, to a keyboard user as to a mouse
// user. Each test reaches its trigger with Tab alone and reads the tooltip.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { EvaluationResult, EvaluationRunDetail as RunDetail } from "@/types";
import { MetricName } from "@/components/evaluation/metric-name";
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

function result(over: Partial<EvaluationResult> = {}): EvaluationResult {
  return {
    id: "res-1",
    run_id: "run-1",
    entry_id: "en-1",
    description: "Triage the phishing alert",
    input_prompt: "Triage this alert",
    expected_answer: "Benign",
    files: null,
    agent_output: "Benign",
    trace_ids: [],
    score: 72,
    duration_seconds: 4.2,
    speed_score: null,
    overall_score: null,
    f1_score: null,
    is_reused: false,
    reused_from_result_id: null,
    sub_scores: {
      tool_selection_quality: 100,
      response_completeness: 70,
      response_accuracy: 60,
      instruction_adherence: 70,
      coherence: 60,
    },
    evaluator_reasoning: null,
    execution_details: {
      total_duration_ms: 5000,
      agent_duration_ms: 4200,
      judge_duration_ms: 800,
      ttft_ms: 300,
      execution_mode: "full_loop",
      traces: [
        { type: "llm", description: "LLM call", duration_ms: 1200, model: "gpt-x" },
        { type: "judge", description: "Judge", duration_ms: 800, model: "judge-x", stop_reason: "max_tokens" },
      ],
      metric_results: {
        "json.field_accuracy": {
          status: "scored",
          value: 50,
          detail: {
            exact_object_match: false,
            per_field: [
              { pointer: "/verdict", mode: "enum", score: 1, weight: 1, status: "matched" },
              { pointer: "/summary", mode: "free_text", score: 0.4, weight: 2, status: "matched" },
            ],
          },
        },
      },
    },
    created_at: "2026-10-25T10:00:00Z",
    ...over,
  } as EvaluationResult;
}

function run(over: Partial<RunDetail> = {}): RunDetail {
  return {
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
    trigger_reason: "manual",
    score: 72,
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
    duration_seconds: 4.2,
    avg_sub_scores: {},
    completed_entries: 1,
    total_entries: 1,
    error: null,
    started_at: "2026-10-25T10:00:00Z",
    completed_at: "2026-10-25T10:05:00Z",
    created_at: "2026-10-25T10:00:00Z",
    can_replay: true,
    anomaly_count: 1,
    results: [result()],
    ...over,
  } as RunDetail;
}

function serve(detail: RunDetail) {
  get.mockImplementation(async (path: unknown) => {
    const url = typeof path === "string" ? path : "";
    if (url === "/evaluations/runs/run-1") return detail;
    if (url.endsWith("/params")) return { params: [{ key: "reuse", value: "off", source: "manual" }] };
    if (url.endsWith("/tags")) return { tags: {} };
    if (url.endsWith("/metrics/aggregate")) {
      return {
        run_id: "run-1",
        aggregates: [
          { key: "json.field_accuracy", half: "quality", mean: 50, p50: 50, p95: 50, min: 50, max: 50, count: 1 },
          { key: "judge_seconds", half: null, mean: 0.8, p50: 0.8, p95: 0.8, min: 0.8, max: 0.8, count: 1 },
        ],
      };
    }
    if (url.endsWith("/metrics")) return [];
    return undefined;
  });
}

function renderPage(detail: RunDetail = run()) {
  serve(detail);
  return renderWithProviders(
    <MemoryRouter initialEntries={["/evaluations/runs/run-1"]}>
      <Routes>
        <Route path="/evaluations/runs/:id" element={<EvaluationRunDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

/** Tabs to `target` as a keyboard user does, then returns the tooltip it opens. */
async function tooltipByTab(user: ReturnType<typeof userEvent.setup>, target: HTMLElement): Promise<string> {
  for (let presses = 0; presses < 200 && document.activeElement !== target; presses++) await user.tab();
  expect(document.activeElement).toBe(target);
  return (await screen.findByRole("tooltip")).textContent ?? "";
}

/** The focusable trigger that holds `text`: the element itself, or its InfoLabel. */
function trigger(text: string, nth = 0): HTMLElement {
  const node = screen.getAllByText(text)[nth];
  return (node.closest("[tabindex='0']") as HTMLElement | null) ?? node;
}

describe("run header", () => {
  beforeEach(() => get.mockReset());

  it.each([
    ["Duration", "The agent's working time summed over every entry of the run"],
    ["Manual", "Started on request: Run, Re-run full, Replay, a placebo comparison or the API."],
    ["(default)", "The agent's own configured model, not one added to the evaluation."],
  ])("explains %j to a keyboard user", async (label, explanation) => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Run Detail");
    expect(await tooltipByTab(user, trigger(label))).toContain(explanation);
  });

  it("says Re-run full starts a new run and leaves this one unchanged", async () => {
    const user = userEvent.setup();
    renderPage();
    const button = await screen.findByRole("button", { name: "Re-run full" });
    expect(await tooltipByTab(user, button)).toContain("Starts a new run that replays this one");
  });

  it("keeps the reason a disabled Re-run full gives within keyboard reach", async () => {
    const user = userEvent.setup();
    renderPage(run({ can_replay: false, replay_block_reason: "Replay impossible: missing version snapshots (persona v3)." }));
    const button = await screen.findByRole("button", { name: "Re-run full" });
    expect(await tooltipByTab(user, button.parentElement as HTMLElement)).toContain("missing version snapshots (persona v3)");
  });

  it("names platform errors in the anomaly banner", async () => {
    renderPage();
    expect(await screen.findByText(/could not be run by the platform/)).toBeTruthy();
  });
});

describe("an entry", () => {
  beforeEach(() => get.mockReset());

  it("opens from the keyboard and explains its sub-scores", async () => {
    const user = userEvent.setup();
    renderPage();
    const toggle = await screen.findByRole("button", { name: /Triage the phishing alert/ });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    toggle.focus();
    await user.keyboard("{Enter}");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(await tooltipByTab(user, trigger("Tool Selection"))).toContain("An agent with no tools scores 100.");
  });

  it("explains its own Duration apart from the run's", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("button", { name: /Triage the phishing alert/ });
    // The run's Duration comes first, the entry's second.
    expect(await tooltipByTab(user, trigger("Duration", 1))).toContain("The agent's working time on this entry");
  });

  it("shows a judged entry's score on a failed run instead of saying the judge was off", async () => {
    renderPage(run({ status: "failed", score: null, quality_score: null }));
    const toggle = await screen.findByRole("button", { name: /Triage the phishing alert/ });
    const row = toggle.parentElement as HTMLElement;
    expect(within(row).getByText("72")).toBeTruthy();
  });

  it("explains a partly credited field as Incorrect", async () => {
    const user = userEvent.setup();
    renderPage();
    const toggle = await screen.findByRole("button", { name: /Triage the phishing alert/ });
    await user.click(toggle);
    const field = screen.getByRole("button", { name: /Per-Field Scoring/ });
    await user.click(field);
    const badges = screen.getAllByText("Incorrect");
    expect(await tooltipByTab(user, trigger("Incorrect", badges.length - 1))).toContain("matches it only in part");
  });

  it("names each field's scoring mode, never by the id it is stored as", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: /Triage the phishing alert/ }));
    await user.click(screen.getByRole("button", { name: /Per-Field Scoring/ }));
    expect(screen.getByText("Free Text")).toBeTruthy();
    expect(screen.getByText("Enum")).toBeTruthy();
    expect(screen.queryByText("free_text")).toBeNull();
    expect(screen.queryByText("enum")).toBeNull();
  });

  it("tells a judge cut at its output budget how to recover", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: /Triage the phishing alert/ }));
    const details = screen.getAllByRole("button", { name: "Details" });
    await user.click(details[details.length - 1]);
    expect(await screen.findByText(/its verdict is cut and counts as a judge failure/)).toBeTruthy();
  });

  it("opens a step's details from a click anywhere on its row, as from its Details button", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: /Triage the phishing alert/ }));
    // The judge step's own description, not its Details button.
    await user.click(screen.getByText("Judge", { selector: "p" }));
    expect(await screen.findByText(/its verdict is cut and counts as a judge failure/)).toBeTruthy();
  });

  it("does not open a step's details from a click on an explanation inside the row", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: /Triage the phishing alert/ }));
    // The judge step starts after the 1200 ms model call.
    await user.click(screen.getByText("T+1200ms"));
    expect(screen.queryByText(/its verdict is cut and counts as a judge failure/)).toBeNull();
  });
});

describe("run panels", () => {
  beforeEach(() => get.mockReset());

  it("explains a parameter's source and key", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: /Parameters/ }));
    expect(await tooltipByTab(user, trigger("Manual", 1))).toContain("Given when the run was launched");
    await user.keyboard("{Escape}");
    expect(await tooltipByTab(user, trigger("reuse"))).toContain("Whether prediction reuse was on for this run");
  });

  it("explains a metric column, and marks no telemetry row as not counted", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: /Metrics/ }));
    const telemetry = (await screen.findByText("Judge Time (s)")).closest("tr") as HTMLElement;
    expect(within(telemetry).queryByText("Not counted")).toBeNull();
    expect(await tooltipByTab(user, trigger("P95"))).toContain("The 95th percentile over the entries");
  });
});

describe("a metric only older runs recorded", () => {
  it("says what it measured", async () => {
    const user = userEvent.setup();
    renderWithProviders(<MetricName metricKey="cost_usd" />);
    expect(await tooltipByTab(user, trigger("Cost (USD)"))).toContain("Recorded by older runs: the agent's LLM cost");
  });
});
