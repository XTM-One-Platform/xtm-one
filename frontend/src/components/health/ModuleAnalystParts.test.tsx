// The Module Analyst on the Health page (#101 S14c-2): a module card's one AI block (the last
// investigation or the analysis summary under one shared header, Run checks now in its menu), and a
// finding's Module Analyst block.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/testing/providers";
import type { HealthAnalysisOverview } from "@/types";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
const flags = vi.hoisted(() => ({ aiEnabled: true }));
vi.mock("@/lib/api", () => ({ api }));
vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useAiEnabled: () => flags.aiEnabled,
}));

import { AnalystRecommendationBlock, ModuleAnalystBlock, investigateRefusalKey } from "./ModuleAnalystParts";

const INVESTIGATION = {
  cycle_id: "c-1",
  registration_id: "r-1",
  status: "budget_reached" as const,
  trigger: "signal" as const,
  opened_at: "2026-11-21T10:00:00Z",
  closed_at: "2026-11-21T10:05:00Z",
};

const OVERVIEW = {
  enabled: true,
  ceilings_set: true,
  modules: [{ module: "opencti", status: "completed", summary: "Restart the stalled connector.", reason: null, analysed_at: "2026-11-21T11:00:00Z" }],
} as unknown as HealthAnalysisOverview;

const renderBlock = (props: Partial<Parameters<typeof ModuleAnalystBlock>[0]> = {}) =>
  renderWithProviders(
    <ModuleAnalystBlock module="opencti" investigation={INVESTIGATION} showSummary={false} hasOpenFindings overview={OVERVIEW} {...props} />,
  );

const openMenu = async () => {
  await userEvent.setup().click(screen.getByRole("button", { name: "OpenCTI actions" }));
  return screen.findByRole("menuitem", { name: /Run checks now/ });
};

describe("ModuleAnalystBlock", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    flags.aiEnabled = true;
    api.get.mockImplementation((path: string) =>
      path === "/health/name" ? Promise.resolve({ name: "Concierge", default_name: "Concierge" }) : Promise.reject(new Error(path)),
    );
  });

  it("shows the outcome and what started the last investigation in plain words", () => {
    renderBlock();
    const line = screen.getByTestId("health-investigation-opencti");
    expect(line.textContent).toContain("Ceiling reached");
    expect(line.textContent).toContain("Findings changed");
    expect(line.textContent).not.toContain("budget_reached");
  });

  it("says when the module was never investigated", () => {
    renderBlock({ module: "openaev", investigation: null });
    expect(screen.getByTestId("health-investigation-openaev").textContent).toContain("No check round yet");
  });

  it("renders the same AI block header whatever the body shows (UX round 6 B1 on #953)", () => {
    const { unmount } = renderBlock();
    const investigationHeader = screen.getByTestId("health-ai-block-header-opencti");
    expect(screen.getByTestId("health-investigation-opencti").contains(investigationHeader)).toBe(true);
    const investigationTags = [...investigationHeader.querySelectorAll("*")].map((node) => node.tagName);
    unmount();
    renderBlock({ showSummary: true });
    const summaryHeader = screen.getByTestId("health-ai-block-header-opencti");
    expect(screen.getByTestId("health-analysis-opencti").contains(summaryHeader)).toBe(true);
    expect([...summaryHeader.querySelectorAll("*")].map((node) => node.tagName)).toEqual(investigationTags);
  });

  it("keeps Run checks now in the menu when the summary is the body (UX round 6 B2 on #953)", async () => {
    renderBlock({ showSummary: true });
    expect(await openMenu()).toBeTruthy();
    expect(screen.getByRole("menuitem", { name: /Analyse now/ })).toBeTruthy();
  });

  it("gives every item of the AI actions menu an icon, so the labels line up (UX round 7 B1 on #953)", async () => {
    renderBlock();
    await openMenu();
    const items = screen.getAllByRole("menuitem");
    expect(items.length).toBeGreaterThanOrEqual(2);
    for (const item of items) expect(item.querySelector("svg")).not.toBeNull();
  });

  it("starts an investigation of the module on demand from the menu", async () => {
    api.post.mockResolvedValue({ module: "opencti", items: [{ registration_id: "r-1", cycle_id: "c-2", run_id: "run-2", refusal: null }] });
    renderBlock();
    await userEvent.setup().click(await openMenu());
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/health/modules/opencti/investigate", {}));
  });

  it("disables Run checks now with the reason while AI is off", async () => {
    flags.aiEnabled = false;
    renderBlock({ investigation: null });
    const item = await openMenu();
    expect(item.getAttribute("aria-disabled") ?? item.getAttribute("data-disabled")).not.toBeNull();
    expect(screen.getAllByText("AI is off on this platform.").length).toBeGreaterThan(0);
  });

  it("words the refusals of the module route and of a cycle", () => {
    expect(investigateRefusalKey("module_analyst_off")).toContain("Module Analyst is off");
    expect(investigateRefusalKey("no_registration")).toContain("No active registration");
    expect(investigateRefusalKey("cycle_open")).toBe("{name} is already working on this target.");
    expect(investigateRefusalKey("unknown_code")).toBe("{name} could not start this work.");
  });
});

describe("AnalystRecommendationBlock", () => {
  const recommendation = {
    text: "Restart the stalled connector.",
    confidence: "high" as const,
    evidence: [{ tool: "opencti_list_connectors" }, { tool: "opencti_list_connectors" }],
    cycle_id: "c-1",
    created_at: "2026-11-21T10:05:00Z",
  };

  it("shows the recommendation, its confidence and what it read", () => {
    renderWithProviders(<AnalystRecommendationBlock recommendation={recommendation} />);
    const block = screen.getByTestId("health-analyst-recommendation");
    expect(block.textContent).toContain("Module Analyst");
    expect(block.textContent).toContain("High confidence");
    expect(block.textContent).toContain("Restart the stalled connector.");
    expect(screen.getByTestId("health-analyst-evidence").textContent).toBe("Based on 2 reads of the module: opencti_list_connectors");
  });

  it("counts a single read in the singular", () => {
    renderWithProviders(<AnalystRecommendationBlock recommendation={{ ...recommendation, evidence: [{}] }} />);
    expect(screen.getByTestId("health-analyst-evidence").textContent).toBe("Based on 1 read of the module");
  });
});
