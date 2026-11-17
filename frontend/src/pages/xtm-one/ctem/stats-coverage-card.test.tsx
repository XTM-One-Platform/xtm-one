// Stats and coverage of the CTEM command center (C2 slice V5, redrawn by
// #11547 slice S2d): a heat map of the stages by week with its band key, one
// signed bar per stage from a labelled zero line, and the vertical's findings
// compact, traceable to their stage page, each with its 30-day trend.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import { exposureStats, type CoverageHistoryPoint } from "@/lib/ctem-coverage-stats";
import type { CtemStage } from "@/types";
import { StatsCoverageCard } from "./StatsCoverageCard";
import { metricSeries, scoreBand, stageName, weeklyHeat } from "./stats-coverage";

const ctx = vi.hoisted(() => ({ isAdmin: true, version: null as number | null }));
vi.mock("@/hooks/useAuth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAuth")>()),
  useAuth: () => ({ isAdmin: ctx.isAdmin }),
}));
vi.mock("@/hooks/api/useOrganizationContextApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useOrganizationContextApi")>()),
  useOrganizationContext: () => ({ data: { version: ctx.version } }),
}));

beforeEach(() => {
  ctx.isAdmin = true;
  ctx.version = null;
});

function stage(key: string, over: Partial<CtemStage> = {}): CtemStage {
  return {
    key, label: `server ${key}`, score: 80, prev_score: 70, band: "strong", covered: true, confidence: "high",
    primary_products: ["opencti"], secondary_products: [], missing_products: [], products_present: ["opencti"],
    methodology: "", facts: [], viz: {},
    ...over,
  };
}

const stages = [
  stage("scoping"),
  stage("discovery", { score: 40, band: "at_risk" }),
  stage("validation", { score: null, band: "unknown", covered: false, primary_products: ["openaev"], products_present: [] }),
];
const posture = { posture_score: 60, stages, products: { opencti: { present: true } } };
const history = [
  { date: "2026-10-10", posture_score: 50, stage_metrics: { scoping: { score: 70 }, discovery: { score: 45, kev_count: 1200 } } },
  { date: "2026-11-08", posture_score: 60, stage_metrics: { scoping: { score: 80 }, discovery: { score: 40, kev_count: 1284 } } },
];

function Where() {
  return <p data-testid="where">{useLocation().pathname}</p>;
}

function card(props: Partial<Parameters<typeof StatsCoverageCard>[0]> = {}) {
  const onConnect = vi.fn();
  renderWithProviders(
    <MemoryRouter>
      <Routes>
        <Route
          path="*"
          element={
            <>
              <StatsCoverageCard
                history={history}
                stats={exposureStats(posture, history)}
                findings={[
                  { label: "Known-exploited", value: 4, stage: "prioritization", series: [3, 4] },
                  { label: "Validation success", value: null, unit: "%", connect: "openaev" },
                ]}
                onConnect={onConnect}
                {...props}
              />
              <Where />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
  return { onConnect };
}

describe("StatsCoverageCard", () => {
  it("draws the five stages by week in their score band, dates the first and the last week, and keys the bands", () => {
    card();
    const map = screen.getByTestId("ctem-heat-map");
    const rows = within(map).getAllByRole("row").slice(1);
    expect(rows.map((r) => r.getAttribute("data-stage"))).toEqual(["scoping", "discovery", "prioritization", "validation", "remediation"]);
    const last = (r: HTMLElement) => {
      const bands = Array.from(r.querySelectorAll("[data-band]")).map((c) => c.getAttribute("data-band"));
      return bands[bands.length - 1];
    };
    expect(last(rows[0])).toBe("good");
    expect(last(rows[1])).toBe("at_risk");
    expect(last(rows[3])).toBe("none");
    // Each cell says its stage, week, score and band, never colour alone.
    expect(within(rows[0]).getByText(/^Scoping, week of .+: 80, Good$/)).toBeTruthy();
    expect(within(screen.getByTestId("ctem-heat-legend")).getAllByRole("listitem").map((l) => l.textContent)).toEqual([
      "Critical", "At risk", "Fair", "Good", "Strong", "No data",
    ]);
  });

  it("says when no day is recorded yet instead of an empty grid", () => {
    card({ history: [] });
    expect(screen.getByText("Scores by week after the first recorded day")).toBeTruthy();
  });

  it("names the stages by key, never the server label, and opens the stage page", () => {
    card();
    expect(screen.queryByText("server scoping")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Open the Discovery stage" }));
    expect(screen.getByTestId("where").textContent).toBe("/ctem/discovery");
  });

  it("draws one signed bar per stage from the zero line, its value at the bar end", () => {
    card();
    expect(screen.getByRole("img", { name: "Scoping: +10 over the period" })).toBeTruthy();
    expect(screen.getByRole("img", { name: "Discovery: -5 over the period" })).toBeTruthy();
    expect(screen.getByRole("img", { name: "Validation: not measured over the period" })).toBeTruthy();
    expect(within(screen.getByTestId("ctem-delta-scoping")).getByTestId("ctem-delta-value").textContent).toBe("+10");
  });

  it("opens a finding's in-app stage page, never a module list, and offers Connect for a missing one (#11324)", () => {
    const { onConnect } = card();
    const findings = screen.getByTestId("ctem-findings");
    expect(within(findings).queryByRole("link")).toBeNull();
    fireEvent.click(within(findings).getByRole("button", { name: "Known-exploited 4: open the Prioritization stage" }));
    expect(screen.getByTestId("where").textContent).toBe("/ctem/prioritization");
    fireEvent.click(within(findings).getByRole("button", { name: /Connect OpenAEV/ }));
    expect(onConnect).toHaveBeenCalledWith("openaev");
  });

  it("draws a finding's 30-day trend at full row width, or says it waits for 2 recorded days", () => {
    card({ findings: [{ label: "Known-exploited", value: 4, series: [3, 4] }, { label: "Critical", value: 2, series: [2] }] });
    expect(screen.getByRole("img", { name: "Known-exploited: last 30 days" })).toBeTruthy();
    expect(screen.getByText("Trend after 2 recorded days")).toBeTruthy();
  });

  it("says a figure is missing from the recorded days, never that it waits for them (UX round 1 on #11631, B1)", () => {
    card({ findings: [{ label: "Critical", value: 2, series: [null, null, 2] }] });
    expect(screen.getByText("Too few days with this figure for a trend")).toBeTruthy();
    expect(screen.queryByText("Trend after 2 recorded days")).toBeNull();
  });

  it("says when the figures are module-wide, with the setup call to action for an administrator", () => {
    card({ generic: true });
    expect(screen.getByText("These figures are module-wide")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Set up organization context" }).getAttribute("href")).toBe("/settings/organization?setup=1");
  });

  it("words the notice for a reader who cannot set the context", () => {
    ctx.isAdmin = false;
    card({ generic: true });
    expect(screen.getByTestId("ctem-stats-generic")).toHaveTextContent("An administrator can set up");
    expect(screen.queryByRole("link", { name: /organization context/ })).toBeNull();
  });

  it("shows no notice when the figures are tailored", () => {
    card();
    expect(screen.queryByText("These figures are module-wide")).toBeNull();
  });

  it("keeps a missing module's label whole, Connect on the line under it", () => {
    card();
    const row = within(screen.getByTestId("ctem-findings"))
      .getAllByTestId("ctem-finding")
      .find((li) => within(li).queryByRole("button", { name: /Connect OpenAEV/ }));
    if (!row) throw new Error("no Findings row offers Connect OpenAEV");
    const label = within(row).getByText("Validation success");
    expect(label.textContent).toBe("Validation success");
    expect(label.nextElementSibling).toBe(within(row).getByRole("button", { name: /Connect OpenAEV/ }));
  });

  it("prints a count of 1,000 or more compact, the exact value on hover (R4-B5)", () => {
    card({ findings: [{ label: "Known-exploited", value: 1284 }, { label: "Critical", value: 492 }] });
    const value = within(screen.getByTestId("ctem-findings")).getAllByTestId("ctem-finding-value");
    expect(value[0].textContent).toBe("1.3K");
    expect(value[0].getAttribute("title")).toBe("1,284");
    expect(value[1].textContent).toBe("492");
    expect(value[1].getAttribute("title")).toBeNull();
  });

  it("drops the ATT&CK tested vs untested tile: the strip's Discovery column carries it (R3-B2)", () => {
    card();
    expect(screen.queryByText("ATT&CK exercised")).toBeNull();
    expect(screen.getByRole("heading", { level: 2, name: "Stats and coverage" })).toBeTruthy();
  });
});

describe("weeklyHeat, scoreBand, metricSeries and stageName", () => {
  it("cuts the bands as ctem_scoring.py does", () => {
    expect([39, 40, 59, 60, 74, 75, 89, 90].map(scoreBand)).toEqual([
      "critical", "at_risk", "at_risk", "fair", "fair", "good", "good", "strong",
    ]);
  });

  it("averages each stage's recorded scores per week over the last 8 weeks, ending on the last recorded day", () => {
    const heat = weeklyHeat(
      [
        { date: "2026-11-01", posture_score: null, stage_metrics: { scoping: { score: 60 } } },
        { date: "2026-11-08", posture_score: null, stage_metrics: { scoping: { score: 70 } } },
        { date: "2026-11-09", posture_score: null, stage_metrics: { scoping: { score: 80 } } },
      ],
      ["scoping", "discovery"],
    );
    if (!heat) throw new Error("no heat map");
    expect(heat.weeks).toHaveLength(8);
    expect(heat.weeks[7]).toBe("2026-11-03");
    expect(heat.rows[0].cells[7]).toEqual({ score: 75, band: "good" });
    expect(heat.rows[0].cells[6]).toEqual({ score: 60, band: "fair" });
    expect(heat.rows[1].cells.every((c) => c === null)).toBe(true);
    expect(weeklyHeat([], ["scoping"])).toBeNull();
  });

  it("reads a finding's series from its stage, null on a day without it", () => {
    expect(metricSeries(history, "discovery", "kev_count")).toEqual([1200, 1284]);
    expect(metricSeries(history, "scoping", "kev_count")).toEqual([null, null]);
  });

  it("rebuilds Validation success from prevention and detection, which the snapshot stores (UX round 1 on #11631, B1)", () => {
    const days: CoverageHistoryPoint[] = [
      { date: "2026-11-01", posture_score: null, stage_metrics: { validation: { prevention_success_pct: 54, detection_success_pct: 41 } } },
      { date: "2026-11-02", posture_score: null, stage_metrics: { validation: { prevention_success_pct: 60 } } },
      { date: "2026-11-03", posture_score: null, stage_metrics: { validation: { finished_simulations: 5 } } },
    ];
    expect(metricSeries(days, "validation", "validation_success_pct")).toEqual([48, 60, null]);
  });

  it("falls back to the server label for an unknown stage key", () => {
    const t = (k: string) => k;
    expect(stageName("remediation", "x", t)).toBe("Remediation");
    expect(stageName("other", "Other stage", t)).toBe("Other stage");
  });
});
