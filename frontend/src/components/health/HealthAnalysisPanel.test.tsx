// The analysis panel of the Platform Health overview (#101 S9c): spend against the ceilings,
// one row per module with its summary or why it did not run, Analyse now gated on AI.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
const flags = vi.hoisted(() => ({ aiEnabled: true }));
vi.mock("@/lib/api", () => ({ api }));
vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useAiEnabled: () => flags.aiEnabled,
}));

import { HealthAnalysisPanel } from "./HealthAnalysisPanel";

const CEILINGS = { day_tokens: 100000, day_cost_usd: "1", month_tokens: 3100000, month_cost_usd: "31" };

function serve(overrides: Record<string, unknown> = {}) {
  api.get.mockImplementation((path: string) => {
    if (path === "/health/analysis") {
      return Promise.resolve({
        enabled: true,
        ceilings_set: true,
        ceilings: CEILINGS,
        day: { tokens: 4763, cost_usd: "0.031852", reserved_tokens: 0 },
        month: { tokens: 9526, cost_usd: "0.063704", reserved_tokens: 0 },
        modules: [
          { module: "xtm_one", status: "completed", reason: null, summary: "Two agents exceed their quota.", model: "m", tokens: 4763, cost_usd: "0.031852", trigger: "schedule", analysed_at: "2026-11-20T10:00:00Z" },
          { module: "opencti", status: "refused", reason: "ceiling_reached", summary: "", model: null, tokens: 0, cost_usd: "0", trigger: "schedule", analysed_at: "2026-11-20T10:00:00Z" },
          { module: "openaev", status: null, reason: null, summary: "", model: null, tokens: 0, cost_usd: "0", trigger: null, analysed_at: null },
        ],
        ...overrides,
      });
    }
    return Promise.reject(new Error(path));
  });
}

const render = () => renderWithProviders(<MemoryRouter><HealthAnalysisPanel /></MemoryRouter>);

describe("HealthAnalysisPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    flags.aiEnabled = true;
  });

  it("shows the spend against each ceiling and every module's latest analysis", async () => {
    serve();
    render();
    expect(await screen.findByTestId("health-analysis")).toBeTruthy();
    expect(screen.getByText("Tokens: 4.8K of 100K")).toBeTruthy();
    expect(screen.getByText("Two agents exceed their quota.")).toBeTruthy();
    expect(screen.getByTestId("health-analysis-opencti").textContent).toContain("Ceiling reached");
    expect(screen.getByTestId("health-analysis-openaev").textContent).toContain("Not analysed yet");
  });

  it("says analysis is off and offers no run while it is off", async () => {
    serve({ enabled: false });
    render();
    expect(await screen.findByTestId("health-analysis-off")).toBeTruthy();
    expect(screen.queryByTestId("health-analysis-run-xtm_one")).toBeNull();
  });

  it("runs one module on demand", async () => {
    serve();
    api.post.mockResolvedValue({});
    render();
    fireEvent.click(await screen.findByTestId("health-analysis-run-opencti"));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/health/analysis/opencti/run", {}));
  });

  it("disables Analyse now while AI is off", async () => {
    flags.aiEnabled = false;
    serve();
    render();
    expect((await screen.findByTestId("health-analysis-run-xtm_one")).hasAttribute("disabled")).toBe(true);
  });

  it("disables Analyse now and says why when a ceiling is cleared", async () => {
    serve({ ceilings_set: false, ceilings: { ...CEILINGS, day_tokens: null } });
    render();
    expect(await screen.findByTestId("health-analysis-no-ceiling")).toBeTruthy();
    expect(screen.getByText("Tokens: 4.8K, no ceiling")).toBeTruthy();
    expect(screen.getByTestId("health-analysis-run-xtm_one").hasAttribute("disabled")).toBe(true);
  });
});
