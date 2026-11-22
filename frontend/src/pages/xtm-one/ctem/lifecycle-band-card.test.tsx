// The exposure story strip of the CTEM command center (lifecycle_band widget,
// dev-docs/ctem-stage-viz.md section 4, slice S2a of #11547): five stage
// columns and Governance & Risk, each a concentric ring (measured outside, in
// your context inside), band and delta, the stage metric and a sparkline,
// with honest not-configured and no-context states; never a generated
// assessment.

import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { CtemStage } from "@/types";
import { LifecycleBandCard, ORGANIZATION_CONTEXT_SETUP, stageMetricValue, stageTrend } from "./LifecycleBandCard";

const state = vi.hoisted(() => ({
  assessments: [] as { data?: unknown }[],
  assessmentArgs: [] as unknown[][],
}));

vi.mock("@/hooks/api/useCtemMetricsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemMetricsApi")>()),
  useCtemStageAssessments: (...args: unknown[]) => {
    state.assessmentArgs.push(args);
    return state.assessments;
  },
  useRefreshStageAssessment: () => {
    throw new Error("the lifecycle band never generates an assessment");
  },
}));

function stage(key: string, over: Partial<CtemStage> = {}): CtemStage {
  return {
    key, label: key, score: 64, prev_score: 60, band: "fair", covered: true, confidence: "high",
    primary_products: ["opencti"], secondary_products: [], missing_products: [], products_present: ["opencti"],
    methodology: "", facts: [{ label: "Assets in scope", value: 1200, unit: "", kind: "fact" }], viz: {},
    qualitative_score: 52, qualitative_band: "fair",
    ...over,
  };
}

const NOW = new Date("2026-11-09T12:00:00Z");
const history = [
  { date: "2026-10-20", posture_score: 50, stage_metrics: { scoping: { score: 55, scope_breadth_pct: 67 } } },
  { date: "2026-11-08", posture_score: 58, stage_metrics: { scoping: { score: 64, scope_breadth_pct: 71 } } },
];

function Where() {
  return <p data-testid="where">{useLocation().pathname}</p>;
}

function band(props: Partial<Parameters<typeof LifecycleBandCard>[0]> = {}) {
  return renderWithProviders(
    <MemoryRouter>
      <Routes>
        <Route
          path="*"
          element={
            <>
              <LifecycleBandCard
                stages={[stage("scoping"), stage("discovery", { covered: false, score: null, missing_products: ["opencti"] })]}
                live
                covered={1}
                total={5}
                history={history}
                vertical="cyber"
                aiLockReason={null}
                opencrq={null}
                onExport={() => undefined}
                onConnect={() => undefined}
                posture={{ score: 58, band: "fair", qualitativeScore: 49, qualitativeBand: "at_risk" }}
                now={NOW}
                {...props}
              />
              <Where />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

function column(key: string) {
  const li = within(screen.getByTestId("ctem-lifecycle-chips"))
    .getAllByRole("listitem")
    .find((c) => c.getAttribute("data-stage") === key);
  if (!li) throw new Error(`no column ${key}`);
  return li;
}

beforeEach(() => {
  state.assessments = [];
  state.assessmentArgs = [];
});

describe("LifecycleBandCard, the exposure story strip", () => {
  it("draws the five stages and Governance & Risk, in order", () => {
    band();
    const cols = within(screen.getByTestId("ctem-lifecycle-chips")).getAllByRole("listitem");
    expect(cols.map((c) => c.getAttribute("data-stage"))).toEqual([
      "scoping", "discovery", "prioritization", "validation", "remediation", "governance",
    ]);
  });

  it("names the two rings once, in the strip's key, quantitative and qualitative", () => {
    band();
    const key = screen.getByTestId("ctem-story-key");
    expect(key.textContent).toContain("Outer ring: Measured (quantitative)");
    expect(key.textContent).toContain("Inner ring: In your context (qualitative)");
  });

  it("puts the signals first in the key row, the ring legend after them, no signal sources icon (#1189, #1263)", () => {
    band({ signals: { counts: { new: 2 }, onOpen: () => {} } });
    const key = screen.getByTestId("ctem-story-key");
    const children = Array.from(key.children);
    const signalsAt = children.indexOf(screen.getByTestId("ctem-story-key-signals"));
    const ringsAt = children.indexOf(screen.getByTestId("ctem-story-key-rings"));
    expect(signalsAt).toBe(0);
    expect(ringsAt).toBeGreaterThan(signalsAt);
    // The sources are a tab of the Signals page the counter opens (Sam, 22 Nov 2026, #1263).
    expect(within(key).queryByRole("link", { name: "Signal sources" })).toBeNull();
    expect(within(key).queryByRole("button", { name: "Signal sources" })).toBeNull();
    expect(within(key).getByRole("button", { name: "Open all signals" })).toBeTruthy();
  });

  it("prints both readings as labelled figures beside the ring, not only rings (#11689)", () => {
    band();
    const scoping = column("scoping");
    expect(within(scoping).getByTestId("ctem-story-measured").textContent).toBe("Measured64");
    expect(within(scoping).getByTestId("ctem-story-context").textContent).toBe("In your context52");
    const gov = column("governance");
    expect(within(gov).getByTestId("ctem-story-context").textContent).toBe("In your context49");
  });

  it("writes -- for the context figure when the context gives none, never a made-up number", () => {
    band({ stages: [stage("scoping", { qualitative_score: null })] });
    expect(within(column("scoping")).getByTestId("ctem-story-context").textContent).toBe("In your context--");
  });

  it("draws no reading figures on a stage with no source", () => {
    band();
    expect(within(column("discovery")).queryByTestId("ctem-story-figures")).toBeNull();
  });

  it("draws the measured score as the big number and both scores in the ring's accessible name", () => {
    band();
    const scoping = column("scoping");
    expect(within(scoping).getByTestId("ctem-story-score").textContent).toBe("64");
    expect(within(scoping).getByRole("img", { name: "Scoping: measured 64, in your context 52" })).toBeTruthy();
  });

  it("prints the band name and the 30-day delta under the ring", () => {
    band();
    const delta = within(column("scoping")).getByTestId("ctem-story-delta");
    expect(delta.textContent).toContain("Fair");
    expect(delta.textContent).toContain("+9");
  });

  it("draws the stage metric with its value and its delta", () => {
    band();
    const metric = within(column("scoping")).getByTestId("ctem-story-metric");
    expect(metric.textContent).toContain("Breadth");
    expect(metric.textContent).toContain("71%");
    expect(metric.textContent).toContain("+4");
  });

  it("asks for the organization context instead of drawing an inner arc", () => {
    band({ stages: [stage("scoping", { qualitative_score: null })] });
    const scoping = column("scoping");
    expect(within(scoping).getByRole("img", { name: "Scoping: measured 64, no organization context, add organization context" })).toBeTruthy();
    const link = within(scoping).getByRole("link", { name: "Add organization context" });
    expect(link.getAttribute("href")).toBe(ORGANIZATION_CONTEXT_SETUP);
  });

  it("draws -- and one connect link on a stage with no source, never a figure", () => {
    const onConnect = vi.fn();
    band({ onConnect });
    const discovery = column("discovery");
    expect(within(discovery).getByTestId("ctem-story-score").textContent).toBe("--");
    expect(within(discovery).getByRole("img", { name: "Discovery: not configured" })).toBeTruthy();
    fireEvent.click(within(discovery).getByRole("button", { name: "Connect OpenCTI" }));
    expect(onConnect).toHaveBeenCalledWith("opencti");
    expect(screen.getByTestId("where").textContent).toBe("/");
    expect(within(discovery).getByTestId("ctem-story-metric").textContent).not.toMatch(/\d/);
  });

  it("names the cause on a covered stage with no score, never a bare No data", () => {
    band({ stages: [stage("scoping"), stage("discovery", { score: null, band: "unknown" })] });
    const discovery = column("discovery");
    expect(within(discovery).getByText("Not enough data to score")).toBeTruthy();
    expect(within(discovery).queryByText("No data")).toBeNull();
  });

  it("never calls a connected stage with no score not configured", () => {
    band({ stages: [stage("scoping"), stage("discovery", { score: null, band: "unknown" })] });
    const discovery = column("discovery");
    expect(within(discovery).getByRole("img", { name: "Discovery: connected, not enough data to score" })).toBeTruthy();
    expect(within(discovery).queryByRole("img", { name: "Discovery: not configured" })).toBeNull();
  });

  it("writes every delta with its period, a zero included", () => {
    const flat = [
      { date: "2026-10-20", stage_metrics: { scoping: { score: 64, scope_breadth_pct: 70 } } },
      { date: "2026-11-08", stage_metrics: { scoping: { score: 64, scope_breadth_pct: 70 } } },
    ];
    band({ history: flat });
    const scoping = column("scoping");
    expect(within(scoping).getByTestId("ctem-story-delta").textContent).toContain("+0 / 30 d");
    expect(within(scoping).getByTestId("ctem-story-metric").textContent).toContain("+0 / 30 d");
  });

  it("explains every figure in the column button's description, for hover and keyboard", () => {
    band();
    const button = screen.getByRole("button", { name: "Open the Scoping stage" });
    const descId = button.getAttribute("aria-describedby");
    expect(descId).toBeTruthy();
    const summary = document.getElementById(descId as string)?.textContent ?? "";
    expect(summary).toContain("Scoping: measured 64, in your context 52");
    expect(summary).toContain("Change over the last 30 days: +9");
    expect(summary).toContain("Breadth 71%, Change over the last 30 days: +4");
  });

  it("says the change once when the stage metric is the score's own input", () => {
    const same = [
      { date: "2026-10-20", stage_metrics: { discovery: { score: 57, threat_informed_coverage_pct: 57 } } },
      { date: "2026-11-08", stage_metrics: { discovery: { score: 3, threat_informed_coverage_pct: 3 } } },
    ];
    band({ stages: [stage("discovery", { score: 3, band: "critical", qualitative_score: 2, qualitative_band: "critical" })], history: same });
    const button = screen.getByRole("button", { name: "Open the Discovery stage" });
    const summary = document.getElementById(button.getAttribute("aria-describedby") as string)?.textContent ?? "";
    expect(summary.match(/Change over the last 30 days: -54/g)).toHaveLength(1);
  });

  it("opens the stage page on a click, with an accessible name", () => {
    band();
    fireEvent.click(screen.getByRole("button", { name: "Open the Scoping stage" }));
    expect(screen.getByTestId("where").textContent).toBe("/ctem/scoping");
  });

  it("gives every column one cell per subgrid row, so rows share a baseline", () => {
    band();
    for (const key of ["scoping", "discovery", "prioritization", "validation", "remediation", "governance"]) {
      const col = column(key);
      expect(within(col).getAllByTestId("ctem-lifecycle-trend-line")).toHaveLength(1);
      expect(within(col).getAllByTestId("ctem-story-metric")).toHaveLength(1);
    }
  });

  it("reads Governance & Risk from the posture: both scores, band, delta, no invented target", () => {
    band();
    const gov = column("governance");
    expect(within(gov).getByTestId("ctem-story-score").textContent).toBe("58");
    expect(within(gov).getByRole("img", { name: "Governance & Risk: measured 58, in your context 49" })).toBeTruthy();
    expect(within(gov).getByTestId("ctem-story-delta").textContent).toContain("+8");
    expect(within(gov).getByTestId("ctem-story-metric").textContent).toBe("No posture target yet");
  });

  it("shows the saved posture target on Governance & Risk, and opens its page from the default band", () => {
    band({ posture: { score: 58, band: "fair", qualitativeScore: 49, qualitativeBand: "at_risk", target: 75 } });
    const gov = column("governance");
    expect(within(gov).getByTestId("ctem-story-metric").textContent).toBe("Posture target75");
    fireEvent.click(screen.getByRole("button", { name: "Open Governance & Risk" }));
    expect(screen.getByTestId("where").textContent).toBe("/ctem/governance");
  });

  it("draws no figure on Governance & Risk without a posture", () => {
    band({ posture: null });
    const gov = column("governance");
    expect(within(gov).getByTestId("ctem-story-score").textContent).toBe("--");
    expect(within(gov).getByTestId("ctem-story-delta").textContent).not.toMatch(/\d/);
  });

  it("links Governance & Risk to OpenCRQ once it registers, an http(s) address only", () => {
    band({ opencrq: { url: "https://crq.example.com" } });
    expect(screen.getByRole("link", { name: /Open in OpenCRQ/ }).getAttribute("href")).toBe("https://crq.example.com/");
  });

  it("holds at most two caption lines in every delta row, so the tiles keep one height", () => {
    band({
      opencrq: { url: "https://crq.example.com" },
      posture: { score: 58, band: "fair", qualitativeScore: null, qualitativeBand: "unknown" },
    });
    for (const key of ["scoping", "discovery", "governance"]) {
      const row = within(column(key)).getByTestId("ctem-story-delta");
      expect(row.children.length).toBeLessThanOrEqual(2);
    }
    const gov = within(column("governance")).getByTestId("ctem-story-delta");
    expect(within(gov).getByRole("link", { name: "Add organization context" })).toBeTruthy();
    expect(within(gov).queryByRole("link", { name: /Open in OpenCRQ/ })).toBeNull();
  });

  it("never links OpenCRQ to a non-web address", () => {
    band({ opencrq: { url: "javascript:alert(1)" } });
    expect(screen.queryByRole("link", { name: /Open in OpenCRQ/ })).toBeNull();
  });

  it("marks a stage whose stored assessment has a recommended action, reading only covered stages", () => {
    state.assessments = [{ data: { status: "ready", assessment: { recommended_action: "Scope the new VPN" } } }];
    band();
    expect(state.assessmentArgs[0]?.[0]).toEqual(["scoping"]);
    expect(screen.getAllByRole("img", { name: "A recommended action is pending" })).toHaveLength(1);
  });

  it("reads no assessment while AI is locked", () => {
    band({ aiLockReason: "ai_off" });
    expect(state.assessmentArgs[0]?.[2]).toBe(false);
  });
});

describe("stageTrend", () => {
  it("keeps the last 30 days of one stage's score", () => {
    const old = { date: "2026-09-01", stage_metrics: { scoping: { score: 10 } } };
    expect(stageTrend([old, ...history], "scoping", 30, NOW)).toEqual([55, 64]);
    expect(stageTrend(history, "discovery", 30, NOW)).toEqual([null, null]);
  });
});

describe("stageMetricValue", () => {
  it("reads the first recorded key of the stage, clamped to 0-100", () => {
    expect(stageMetricValue("discovery", { detection_coverage_pct: 38 })).toBe(38);
    expect(stageMetricValue("discovery", { threat_informed_coverage_pct: 41, detection_coverage_pct: 38 })).toBe(41);
    expect(stageMetricValue("remediation", { case_closure_pct: 140 })).toBe(100);
  });

  it("draws Prioritization as the critical share, the complement of what its score reads", () => {
    expect(stageMetricValue("prioritization", { noncritical_share_pct: 82 })).toBe(18);
  });

  it("returns null when no key is recorded, never 0", () => {
    expect(stageMetricValue("validation", {})).toBeNull();
    expect(stageMetricValue("validation", undefined)).toBeNull();
  });
});
