// The outcome of a module's analysis on its Platform Health card (#961): the summary headline,
// expandable to the whole text, one muted line for a refusal or a failure, nothing for a module
// with no open finding, Analyse now in the card's actions menu gated on AI.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/testing/providers";
import type { HealthAnalysisOverview, HealthModuleAnalysis } from "@/types";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
const flags = vi.hoisted(() => ({ aiEnabled: true }));
vi.mock("@/lib/api", () => ({ api }));
vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useAiEnabled: () => flags.aiEnabled,
}));

import { ModuleAnalysisSummary, analysisRowFor, summaryHeadline } from "./ModuleAnalysisSummary";

const ROW: HealthModuleAnalysis = {
  module: "xtm_one",
  status: "completed",
  reason: null,
  summary: "Two agents exceed their quota.\n\n1. Raise the quota of the triage agent.\n2. Retire the idle agent.",
  model: "m",
  tokens: 4763,
  cost_usd: "0.031852",
  trigger: "schedule",
  analysed_at: "2026-11-20T10:00:00Z",
};

function overview(row: Partial<HealthModuleAnalysis> = {}, rest: Partial<HealthAnalysisOverview> = {}): HealthAnalysisOverview {
  return {
    enabled: true,
    ceilings_set: true,
    ceilings: { day_tokens: 100000, day_cost_usd: "1", month_tokens: 3100000, month_cost_usd: "31" },
    day: { tokens: 4763, cost_usd: "0.031852", reserved_tokens: 0 },
    month: { tokens: 9526, cost_usd: "0.063704", reserved_tokens: 0 },
    modules: [{ ...ROW, ...row }],
    module_analyst: {
      enabled: false,
      ceilings_set: true,
      ceilings: { cycle_tokens: 80000, day_tokens: 500000, day_cost_usd: "4", month_tokens: 10000000, month_cost_usd: "60" },
      day: { tokens: 0, cost_usd: "0", reserved_tokens: 0 },
      month: { tokens: 0, cost_usd: "0", reserved_tokens: 0 },
    },
    ...rest,
  };
}

const render = (data: HealthAnalysisOverview | undefined, hasOpenFindings = true) =>
  renderWithProviders(<ModuleAnalysisSummary module="xtm_one" hasOpenFindings={hasOpenFindings} overview={data} />);

describe("ModuleAnalysisSummary", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    flags.aiEnabled = true;
  });

  it("takes the first non-empty line of a summary as its headline", () => {
    expect(summaryHeadline("\n  First fix.  \nSecond.")).toBe("First fix.");
    expect(summaryHeadline("")).toBe("");
  });

  it("shows the headline of a completed analysis, the whole summary in a popover, never in place", () => {
    render(overview());
    expect(screen.getByTestId("health-analysis-headline").textContent).toBe("Two agents exceed their quota.");
    expect(screen.queryByTestId("health-analysis-summary")).toBeNull();
    expect(screen.getByText("As of")).toBeTruthy();
    fireEvent.click(screen.getByTestId("health-analysis-expand"));
    const dialog = screen.getByRole("dialog", { name: "AI summary" });
    expect(dialog.textContent).toContain("2. Retire the idle agent.");
    // The card keeps its clamped headline: the summary is not written into the card (UX round 1 B2 on #965).
    expect(screen.getByTestId("health-analysis-xtm_one").textContent).not.toContain("Retire the idle agent");
    expect(screen.getByTestId("health-analysis-expand").getAttribute("aria-expanded")).toBe("true");
  });

  it("answers no row, so the card gets no footer, for a module with no open finding or the analysis off", () => {
    expect(analysisRowFor("xtm_one", false, overview())).toBeNull();
    expect(analysisRowFor("xtm_one", true, overview({}, { enabled: false }))).toBeNull();
    expect(analysisRowFor("xtm_one", true, undefined)).toBeNull();
    expect(analysisRowFor("opencti", true, overview())).toBeNull();
    expect(analysisRowFor("xtm_one", true, overview())?.module).toBe("xtm_one");
  });

  it("offers no expand for a one-line summary that fits", () => {
    render(overview({ summary: "One fix." }));
    expect(screen.getByTestId("health-analysis-headline").textContent).toBe("One fix.");
    expect(screen.queryByTestId("health-analysis-expand")).toBeNull();
  });

  it("offers Show more for a one-line summary whose clamped headline is cut (UX round 3 B4 on #965)", () => {
    // happy-dom lays nothing out: the headline reports a scroll height past its clamped height.
    const scroll = vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(120);
    const client = vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(40);
    const long = "A one-line summary long enough to need more than the two lines the card clamps it to.";
    try {
      render(overview({ summary: long }));
      fireEvent.click(screen.getByTestId("health-analysis-expand"));
      expect(screen.getByRole("dialog", { name: "AI summary" }).textContent).toContain(long);
    } finally {
      scroll.mockRestore();
      client.mockRestore();
    }
  });

  it("shows nothing for a module with no open finding", () => {
    render(overview(), false);
    expect(screen.queryByTestId("health-analysis-xtm_one")).toBeNull();
  });

  it("shows nothing while the analysis is off", () => {
    render(overview({}, { enabled: false }));
    expect(screen.queryByTestId("health-analysis-xtm_one")).toBeNull();
  });

  it("says why a refused analysis did not run, in one muted line and never a success badge", () => {
    render(overview({ status: "refused", reason: "ceiling_reached", summary: "" }));
    expect(screen.getByTestId("health-analysis-reason").textContent).toContain("Ceiling reached");
    expect(screen.queryByText("Analysed")).toBeNull();
    expect(screen.queryByTestId("health-analysis-headline")).toBeNull();
  });

  it("says a failed analysis failed", () => {
    render(overview({ status: "failed", reason: "model_error", summary: "" }));
    expect(screen.getByTestId("health-analysis-reason").textContent).toContain("The model call failed.");
  });

  it("runs one module on demand from the card's actions menu", async () => {
    api.post.mockResolvedValue({});
    render(overview());
    await userEvent.setup().click(screen.getByRole("button", { name: "XTM One actions" }));
    await userEvent.setup().click(await screen.findByRole("menuitem", { name: /Analyse now/ }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/health/analysis/xtm_one/run", {}));
  });

  it("disables Analyse now and says why while AI is off", async () => {
    flags.aiEnabled = false;
    render(overview());
    await userEvent.setup().click(screen.getByRole("button", { name: "XTM One actions" }));
    const item = await screen.findByRole("menuitem", { name: /Analyse now/ });
    expect(item.getAttribute("aria-disabled") ?? item.getAttribute("data-disabled")).not.toBeNull();
    expect(screen.getByText("AI is off on this platform.")).toBeTruthy();
  });
});
