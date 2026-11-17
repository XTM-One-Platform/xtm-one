// The Overview's placebo comparison block (FR-29) and the arm tag (FR-28).

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { PlaceboArmSummary, PlaceboComparison, PlaceboComparisonRow } from "@/types";
import { PlaceboArmTag } from "./PlaceboArmTag";
import { PlaceboComparisonsCard } from "./PlaceboComparisonsCard";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const get = vi.mocked(api.get);

function arm(over: Partial<PlaceboArmSummary> = {}): PlaceboArmSummary {
  return { entries: 12, trials: 60, tokens: 900, judge: null, latency: 2.5, tool_calls: 1, skill_loaded: null, ...over };
}

const METHOD = { resamples: 10000, confidence: 0.95, alpha: 0.05, multiple_comparisons: "holm", quality_selection: null };

function row(over: Partial<PlaceboComparisonRow> = {}): PlaceboComparisonRow {
  return {
    agent_id: "a-1",
    agent_name: "Threat Analyst",
    model_id: "gpt-x",
    target: { kind: "persona", skill_id: null },
    target_key: "persona",
    entries: 12,
    arms: { real: arm(), placebo: arm(), no_target: arm() },
    warnings: [],
    vs_placebo: {
      quality: { estimate: 12.34, ci_low: 8.06, ci_high: 16, p: 0.0002, p_adjusted: 0.0004 },
      tokens: { estimate: 1.02, ci_low: 0.98, ci_high: 1.06, p: 0.3, p_adjusted: 0.3 },
      entries: 12,
      quality_keys: ["prose.token_prf"],
    },
    vs_no_target: {
      quality: { estimate: 20, ci_low: 15, ci_high: 25, p: 0.0002, p_adjusted: null },
      tokens: null,
      entries: 12,
      quality_keys: ["prose.token_prf"],
    },
    placebo_vs_no_target: null,
    verdict: "beats_placebo",
    rests_on_tokens_alone: false,
    ...over,
  };
}

function comparison(over: Partial<PlaceboComparison> = {}): PlaceboComparison {
  return {
    id: "c-1",
    evaluation_id: "e-1",
    status: "completed",
    seed: "abc123",
    corpus_version: "0123456789abcdef",
    config: null,
    warnings: [],
    results: { method: METHOD, rows: [row()] },
    created_by: null,
    created_at: "2026-10-25T10:00:00Z",
    completed_at: "2026-10-25T10:30:00Z",
    runs: [],
    ...over,
  };
}

function answer(comparisons: PlaceboComparison[]) {
  get.mockImplementation(((path: string) =>
    Promise.resolve(path.startsWith("/evaluations/e-1/placebo-comparisons") ? comparisons : [])) as typeof api.get);
}

beforeEach(() => {
  vi.clearAllMocks();
  // The Columns choices are kept per user (storageKey): each test starts from the defaults.
  window.localStorage.clear();
});

describe("PlaceboComparisonsCard", () => {
  it("shows a row's verdict, effects with their intervals and trial counts", async () => {
    answer([comparison()]);
    renderWithProviders(<MemoryRouter><PlaceboComparisonsCard evaluationId="e-1" /></MemoryRouter>);

    expect(await screen.findByText("Beats placebo")).toBeInTheDocument();
    expect(screen.getByText("Threat Analyst")).toBeInTheDocument();
    expect(screen.getByText("Persona")).toBeInTheDocument();
    expect(screen.getByText("+12.3 [+8.1, +16.0]")).toBeInTheDocument();
    expect(screen.getByText("1.02× [0.98, 1.06]")).toBeInTheDocument();
    expect(screen.getByText("12 entries")).toBeInTheDocument();
    expect(screen.getByText("real 60 · placebo 60 · no target 60")).toBeInTheDocument();
    // "Against no target" starts hidden (UX r1 B2 on #10914) and comes back from Columns.
    expect(screen.queryByText("Real: D +20.0 [+15.0, +25.0]")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Columns/ }));
    fireEvent.click(screen.getByRole("button", { name: "Against no target" }));
    expect(screen.getByText("Real: D +20.0 [+15.0, +25.0]")).toBeInTheDocument();
    expect(get.mock.calls.some(([path]) => path === "/evaluations/e-1/placebo-comparisons?limit=10")).toBe(true);
  });

  it("flags a verdict resting on tokens alone, few entries and a warning it does not know", async () => {
    answer([
      comparison({
        results: {
          method: METHOD,
          rows: [row({ rests_on_tokens_alone: true, warnings: ["few_entries", "a_new_code"] })],
        },
      }),
    ]);
    renderWithProviders(<MemoryRouter><PlaceboComparisonsCard evaluationId="e-1" /></MemoryRouter>);

    expect(await screen.findByText("On tokens alone; quality lower bound 8.1")).toBeInTheDocument();
    expect(screen.getByText("Few entries: read with care")).toBeInTheDocument();
    expect(screen.getByText("a_new_code")).toBeInTheDocument();
  });

  it("keeps every row at two lines with no sideways scroll: fixed widths, one-line cells (UX r1 B1 and B2 on #10914)", async () => {
    answer([comparison()]);
    renderWithProviders(<MemoryRouter><PlaceboComparisonsCard evaluationId="e-1" /></MemoryRouter>);

    await screen.findByText("Beats placebo");
    expect(screen.getByRole("table").getAttribute("data-layout")).toBe("fixed");
    for (const name of ["Agent / Model", "Target", "Verdict", "Warnings"]) {
      const header = screen.getAllByRole("columnheader").find((cell) => cell.textContent?.startsWith(name));
      expect(header?.style.width, name).toMatch(/px$/);
    }
    // Columns past the 1432 px box at 1728 are hidden or drawn from 1280 px only.
    expect(screen.queryByRole("columnheader", { name: /Against no target/ })).toBeNull();
  });

  it("offers the Columns chooser and no skill column without a skill target", async () => {
    answer([comparison()]);
    renderWithProviders(<MemoryRouter><PlaceboComparisonsCard evaluationId="e-1" /></MemoryRouter>);

    await screen.findByText("Beats placebo");
    expect(screen.getByRole("button", { name: /Columns/ })).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Skill opened" })).toBeNull();
  });

  it("gives no verdict while a comparison runs or after one of its runs did not complete", async () => {
    answer([
      comparison({
        status: "running",
        results: null,
        runs: [
          { id: "r-1", agent_id: "a-1", model_id: "gpt-x", arm: "real", trial_index: 0, placebo_target: null, status: "completed" },
          {
            id: "r-2", agent_id: "a-1", model_id: "gpt-x", arm: "placebo", trial_index: 0,
            placebo_target: { kind: "persona", skill_id: null }, status: "running",
          },
        ],
      }),
    ]);
    const { unmount } = renderWithProviders(<MemoryRouter><PlaceboComparisonsCard evaluationId="e-1" /></MemoryRouter>);

    expect(await screen.findByText(/^Running: 1 of 2 runs finished\./)).toBeInTheDocument();
    expect(screen.queryByText("Beats placebo")).toBeNull();
    unmount();

    answer([comparison({ status: "cancelled", results: null, warnings: [{ code: "run_deleted" }] })]);
    const second = renderWithProviders(<MemoryRouter><PlaceboComparisonsCard evaluationId="e-1" /></MemoryRouter>);

    expect(await screen.findByText("No results: this comparison ended (Cancelled).")).toBeInTheDocument();
    expect(screen.getByText("One of its runs was deleted: no results")).toBeInTheDocument();
    second.unmount();

    answer([
      comparison({
        status: "failed",
        results: { method: METHOD, rows: [row({ verdict: "not_computable", warnings: ["run_incomplete"] })] },
      }),
    ]);
    renderWithProviders(<MemoryRouter><PlaceboComparisonsCard evaluationId="e-1" /></MemoryRouter>);

    expect(await screen.findByText("A run of this comparison did not complete (Failed): its rows have no verdict.")).toBeInTheDocument();
    expect(screen.getByText("Not computable")).toBeInTheDocument();
    expect(screen.getByText("A run did not complete: no verdict")).toBeInTheDocument();
  });

  it("says how often each arm opened a skill target", async () => {
    answer([
      comparison({
        results: {
          method: METHOD,
          rows: [
            row({
              target: { kind: "skills", skill_id: null },
              target_key: "skills",
              arms: { real: arm({ skill_loaded: 0.75 }), placebo: arm({ skill_loaded: 0.2 }) },
            }),
          ],
        },
      }),
    ]);
    renderWithProviders(<MemoryRouter><PlaceboComparisonsCard evaluationId="e-1" /></MemoryRouter>);

    expect(await screen.findByText("real 75 % · placebo 20 %")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Skill opened" })).toBeInTheDocument();
    expect(screen.getByText("real 60 · placebo 60")).toBeInTheDocument();
  });

  it("explains a verdict's rule to a keyboard user", async () => {
    const user = userEvent.setup();
    answer([comparison({ results: { method: METHOD, rows: [row({ verdict: "trade_off" })] } })]);
    renderWithProviders(<MemoryRouter><PlaceboComparisonsCard evaluationId="e-1" /></MemoryRouter>);

    const verdict = (await screen.findByText("Trade-off")).closest('[tabindex="0"]');
    for (let presses = 0; presses < 30 && document.activeElement !== verdict; presses++) await user.tab();
    expect(document.activeElement).toBe(verdict);
    expect((await screen.findByRole("tooltip")).textContent).toContain("One outcome improves and the other worsens");
  });

  it.each([
    ["Tokens R [95 % CI]", "Below 1, the real agent is cheaper"],
    ["Trials", "the trials each arm ran in total over those entries"],
    ["Quality D [95 % CI]", "Overall Quality minus its placebo's"],
  ])("explains the %s column from the i beside its header", async (label, explanation) => {
    answer([comparison()]);
    renderWithProviders(<MemoryRouter><PlaceboComparisonsCard evaluationId="e-1" /></MemoryRouter>);

    await screen.findByText("Beats placebo");
    fireEvent.focus(screen.getByRole("img", { name: label }));
    expect((await screen.findByRole("tooltip")).textContent).toContain(explanation);
  });

  it("explains seed abc123", async () => {
    answer([comparison()]);
    renderWithProviders(<MemoryRouter><PlaceboComparisonsCard evaluationId="e-1" /></MemoryRouter>);

    fireEvent.focus(await screen.findByText("seed abc123"));
    expect((await screen.findByRole("tooltip")).textContent).toContain("the order the arms ran in");
  });

  it("says what a warning changes in how the row reads", async () => {
    answer([comparison({ results: { method: METHOD, rows: [row({ warnings: ["few_entries"] })] } })]);
    renderWithProviders(<MemoryRouter><PlaceboComparisonsCard evaluationId="e-1" /></MemoryRouter>);

    const chip = (await screen.findByText("Few entries: read with care")).closest('[tabindex="0"]');
    fireEvent.focus(chip as HTMLElement);
    expect((await screen.findByRole("tooltip")).textContent).toContain("Fewer than 10 entries were counted");
  });

  it("names a cut warning whole in its tooltip and keeps the count of the rest (UX r2 B1 on #10914)", async () => {
    answer([
      comparison({ results: { method: METHOD, rows: [row({ warnings: ["few_entries", "a_new_code", "another_code", "third_code"] })] } }),
    ]);
    renderWithProviders(<MemoryRouter><PlaceboComparisonsCard evaluationId="e-1" /></MemoryRouter>);

    // The "+N" of the warnings past the second stays in the cell, outside any chip.
    const rest = await screen.findByText("+2");
    expect(rest.closest('[tabindex="0"]')).toBe(rest);
    expect(screen.getByText("a_new_code").closest('[tabindex="0"]')).not.toBe(rest);
    // The chip's tooltip starts with its whole label, then what it changes.
    fireEvent.focus(screen.getByText("Few entries: read with care").closest('[tabindex="0"]') as HTMLElement);
    const tip = (await screen.findByRole("tooltip")).textContent ?? "";
    expect(tip.startsWith("Few entries: read with care")).toBe(true);
    expect(tip).toContain("Fewer than 10 entries were counted");
  });

  it("names the tokens-alone line whole in its tooltip, value first (UX r2 B2 on #10914)", async () => {
    answer([comparison({ results: { method: METHOD, rows: [row({ rests_on_tokens_alone: true })] } })]);
    renderWithProviders(<MemoryRouter><PlaceboComparisonsCard evaluationId="e-1" /></MemoryRouter>);

    fireEvent.focus(await screen.findByText("On tokens alone; quality lower bound 8.1"));
    const tip = (await screen.findByRole("tooltip")).textContent ?? "";
    expect(tip.startsWith("On tokens alone; quality lower bound 8.1")).toBe(true);
    expect(tip).toContain("The verdict rests on tokens alone");
  });

  it("draws the rows through the shared data table: a sortable D column and a search", async () => {
    const user = userEvent.setup();
    answer([
      comparison({
        results: {
          method: METHOD,
          rows: [
            row({ agent_id: "a-1", agent_name: "Threat Analyst", verdict: "beats_placebo" }),
            row({ agent_id: "a-2", agent_name: "Hunt Planner", verdict: "trade_off" }),
          ],
        },
      }),
    ]);
    renderWithProviders(<MemoryRouter><PlaceboComparisonsCard evaluationId="e-1" /></MemoryRouter>);

    expect(await screen.findByText("Hunt Planner")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Quality D \[95 % CI\]/ })).toBeInTheDocument();
    await user.type(screen.getByRole("searchbox"), "hunt");
    expect(await screen.findByText("Hunt Planner")).toBeInTheDocument();
    await vi.waitFor(() => expect(screen.queryByText("Threat Analyst")).toBeNull());
  });

  it("renders nothing for an evaluation that never ran a comparison", async () => {
    answer([]);
    const { container } = renderWithProviders(<MemoryRouter><PlaceboComparisonsCard evaluationId="e-1" /></MemoryRouter>);

    await vi.waitFor(() => expect(get).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});

describe("PlaceboArmTag", () => {
  it("names each arm of a comparison, with a control arm's target, and nothing outside a comparison", () => {
    const tag = (run: Parameters<typeof PlaceboArmTag>[0]["run"]) => <PlaceboArmTag run={run} />;
    const { rerender } = renderWithProviders(
      tag({ arm: "placebo", placebo_target: { kind: "skills", skill_id: null }, placebo_comparison_id: "c-1" }),
    );
    expect(screen.getByText("Placebo · Skills")).toBeInTheDocument();

    rerender(tag({ arm: "no_target", placebo_target: { kind: "persona", skill_id: null }, placebo_comparison_id: "c-1" }));
    expect(screen.getByText("No target · Persona")).toBeInTheDocument();

    rerender(tag({ arm: "real", placebo_target: null, placebo_comparison_id: "c-1" }));
    expect(screen.getByText("Real")).toBeInTheDocument();

    rerender(tag({ arm: "real", placebo_target: null, placebo_comparison_id: null }));
    expect(screen.queryByText(/Placebo|No target|Real/)).toBeNull();
  });

  it("names a single-skill target by its skill", async () => {
    get.mockImplementation(((path: string) =>
      Promise.resolve(path.startsWith("/skills") ? [{ id: "s-1", name: "Triage Checklist" }] : [])) as typeof api.get);
    renderWithProviders(
      <PlaceboArmTag run={{ arm: "placebo", placebo_target: { kind: "skill", skill_id: "s-1" }, placebo_comparison_id: "c-1" }} />,
    );

    expect(await screen.findByText(/Placebo · Skill · Triage Checklist/)).toBeInTheDocument();
  });
});
