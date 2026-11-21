// The Module Analyst on the Health page (#101 S14c-2): a module card's last investigation and
// "Investigate now", and a finding's Module Analyst block.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
const flags = vi.hoisted(() => ({ aiEnabled: true }));
vi.mock("@/lib/api", () => ({ api }));
vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useAiEnabled: () => flags.aiEnabled,
}));

import { AnalystRecommendationBlock, ModuleInvestigationLine, investigateRefusalKey } from "./ModuleAnalystParts";

const INVESTIGATION = {
  cycle_id: "c-1",
  registration_id: "r-1",
  status: "budget_reached" as const,
  trigger: "signal" as const,
  opened_at: "2026-11-21T10:00:00Z",
  closed_at: "2026-11-21T10:05:00Z",
};

describe("ModuleInvestigationLine", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    flags.aiEnabled = true;
    api.get.mockImplementation((path: string) =>
      path === "/health/name" ? Promise.resolve({ name: "Concierge", default_name: "Concierge" }) : Promise.reject(new Error(path)),
    );
  });

  it("shows the outcome and what started the last investigation in plain words", () => {
    renderWithProviders(<ModuleInvestigationLine module="opencti" investigation={INVESTIGATION} />);
    const line = screen.getByTestId("health-investigation-opencti");
    expect(line.textContent).toContain("Ceiling reached");
    expect(line.textContent).toContain("Findings changed");
    expect(line.textContent).not.toContain("budget_reached");
  });

  it("says when the module was never investigated", () => {
    renderWithProviders(<ModuleInvestigationLine module="openaev" investigation={null} />);
    expect(screen.getByTestId("health-investigation-openaev").textContent).toContain("Not investigated yet");
  });

  it("starts an investigation of the module on demand", async () => {
    api.post.mockResolvedValue({ module: "opencti", items: [{ registration_id: "r-1", cycle_id: "c-2", run_id: "run-2", refusal: null }] });
    renderWithProviders(<ModuleInvestigationLine module="opencti" investigation={INVESTIGATION} />);
    fireEvent.click(screen.getByTestId("health-investigate-opencti"));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/health/modules/opencti/investigate", {}));
  });

  it("disables Investigate now while AI is off", () => {
    flags.aiEnabled = false;
    renderWithProviders(<ModuleInvestigationLine module="opencti" investigation={null} />);
    expect(screen.getByTestId("health-investigate-opencti").hasAttribute("disabled")).toBe(true);
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
