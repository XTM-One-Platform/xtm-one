// The /ctem/:stage header on a covered stage with no score (C2 V4, UX round 2
// B1): one statement of the cause, the badge, and no "No data" under the gauge.

import { describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import CtemStageDetail from "./CtemStageDetail";

const state = vi.hoisted(() => ({
  score: null as number | null,
  products: undefined as Record<string, { present: boolean; url?: string }> | undefined,
  missing: [] as string[],
}));

vi.mock("@/hooks/api/useAuthApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useAuthApi")>()),
  useMe: () => ({ data: { ctem_vertical: "cyber" } }),
}));

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: true }) }));

vi.mock("@/hooks/useAiLockReason", () => ({ useAiLockReason: () => null }));

vi.mock("@/hooks/api/useCtemMetricsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemMetricsApi")>()),
  useCtemPosture: () => ({
    isLoading: false,
    data: {
      posture_score: null, posture_band: "unknown", covered_stages: 1, total_stages: 5,
      context: { state: "partial", missing_fields: ["software"] },
      products: state.products,
      stages: [{
        qualitative_score: state.score == null ? null : 50, qualitative_band: state.score == null ? "unknown" : "at_risk",
        key: "discovery", label: "Discovery", score: state.score, prev_score: null,
        band: state.score == null ? "unknown" : "fair", covered: true, confidence: "low",
        primary_products: ["opencti"], secondary_products: [], missing_products: state.missing, products_present: ["opencti"],
        methodology: "", facts: [{ label: "Threat techniques", value: 0, unit: "", kind: "fact" }], viz: {},
      }],
    },
  }),
  useCtemStageAssessment: () => ({ data: { status: "none", assessment: null } }),
  useCtemPostureHistory: () => ({ data: { history: [] } }),
  useRefreshStageAssessment: () => ({ isPending: false, mutate: () => undefined }),
}));

function page() {
  return renderWithProviders(
    <MemoryRouter initialEntries={["/ctem/discovery"]}>
      <Routes>
        <Route path="/ctem/:stage" element={<CtemStageDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("CtemStageDetail hero connection state (UX round 9 of #11549, B1)", () => {
  it("reads OpenAEV as connected from posture.products, not from the stage's products_present", () => {
    state.score = 64;
    state.products = { opencti: { present: true }, openaev: { present: true, url: "https://aev.example.com" } };
    page();
    expect(screen.queryByTestId("ctem-area-needs-aev")).toBeNull();
    expect(screen.getByTestId("ctem-area-no-data")).toBeTruthy();
    expect(screen.getByTestId("ctem-area-source-openaev").getAttribute("title")).not.toBe("Not connected");
    state.products = undefined;
  });

  it("asks to connect OpenAEV only when posture.products says it is absent", () => {
    state.score = 64;
    state.products = { opencti: { present: true }, openaev: { present: false } };
    page();
    expect(screen.getByTestId("ctem-area-needs-aev")).toBeTruthy();
    state.products = undefined;
  });
});

describe("CtemStageDetail header", () => {
  it("states the missing score once: the badge, never No data under the gauge", () => {
    state.score = null;
    page();
    expect(screen.getByTestId("ctem-stage-unscored").textContent).toBe("Not enough data to score");
    expect(screen.queryByText("No data")).toBeNull();
  });

  it("draws the CTEM header band with one cell per score, never the score twice (#11660)", () => {
    state.score = 64;
    page();
    expect(screen.queryByTestId("ctem-stage-unscored")).toBeNull();
    const header = screen.getByTestId("ctem-page-header");
    expect(within(header).getAllByText("64")).toHaveLength(1);
    expect(screen.getByText("Stage 2 of 5")).toBeTruthy();
  });

  it("draws no score cell on an unscored stage", () => {
    state.score = null;
    page();
    expect(screen.queryByTestId("ctem-page-header-stats")).toBeNull();
  });

  it("shows both scores, labelled, and names the missing context instead of the old knowledge base (#11464)", () => {
    state.score = 64;
    page();
    expect(screen.getByTestId("ctem-score-quantitative").textContent).toContain("64");
    expect(screen.getByTestId("ctem-score-qualitative").textContent).toContain("50");
    expect(screen.getByTestId("ctem-context-partial").textContent).toContain("Software");
    expect(screen.queryByText(/Company context is sharpening/)).toBeNull();
  });

  it("says a Discovery 0 without OpenAEV is the missing module, never Critical (#11998)", () => {
    state.score = 0;
    state.products = { opencti: { present: true }, openaev: { present: false } };
    page();
    expect(screen.getByTestId("ctem-score-needs-module").textContent).toBe("--");
    expect(screen.getByText("Not scored: needs OpenAEV")).toBeTruthy();
    expect(screen.queryByTestId("ctem-score-quantitative")).toBeNull();
    expect(screen.getByTestId("ctem-discovery-gap-openaev")).toBeTruthy();
    // The Metrics header says the same: no 0 in a band colour (UX round 1 of #12027, B2).
    const metrics = screen.getByTestId("ctem-metrics-score");
    expect(metrics.textContent).toBe("--");
    expect(metrics.getAttribute("data-band")).toBe("none");
    state.products = undefined;
  });

  it("never offers a module twice: one with a gap filler is left out of the bottom card (UX r1 of #12142, B2)", () => {
    state.score = 64;
    state.products = { opencti: { present: true }, openaev: { present: false } };
    state.missing = ["openaev"];
    page();
    expect(screen.getByTestId("ctem-discovery-gap-openaev")).toBeTruthy();
    expect(screen.queryByText(/This stage is strengthened by/)).toBeNull();
    state.products = undefined;
    state.missing = [];
  });

  it("keeps a detection coverage score above 0 without OpenAEV", () => {
    state.score = 64;
    state.products = { opencti: { present: true }, openaev: { present: false } };
    page();
    expect(screen.queryByTestId("ctem-score-needs-module")).toBeNull();
    expect(screen.getByTestId("ctem-score-quantitative").textContent).toContain("64");
    state.products = undefined;
  });
});
