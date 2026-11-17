/**
 * The agent's ROI section, first in its Value tab since #11037 (#9807, slice O8c of #9206; design
 * dev-docs/roi-outcomes.md 6.3 "Per agent (C2)") - `yarn test:unit`.
 * It reads the agent's report on the page's `?window=`, tells "never counted"
 * from "nothing in this period", and its review-rate card opens the agent's
 * unreviewed runs.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import type { RoiReportResponse } from "@/hooks/api/useRoiReportApi";
import { api } from "@/lib/api";
import { agentReviewPath } from "@/lib/roi-outcomes";
import { renderWithProviders } from "@/testing/providers";

import { AgentRoiSection } from "./AgentRoiSection";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), download: vi.fn() },
}));

const get = vi.mocked(api.get);

const figures = (runs: number, extra: Record<string, number> = {}) => ({
  currency: "EUR",
  runs,
  units: runs,
  hours_saved: runs * 2,
  llm_cost: 0.5,
  value: runs * 100,
  net_value: runs * 100,
  roi: null,
  reviewed: runs > 0 ? 1 : 0,
  unreviewed: runs > 0 ? runs - 1 : 0,
  rejected: 0,
  review_rate: runs > 0 ? 1 / runs : null,
  assumptions: [],
  ...extra,
});

function report(runs: number, extra: Record<string, number> = {}): RoiReportResponse {
  return {
    window: "30d",
    currency: "EUR",
    rate_label: "assumption",
    estimated: false,
    organization: figures(runs, extra),
    fleets: [],
    agents: runs > 0 ? [{ id: "a-1", name: "Contract reviewer", ...figures(runs) }] : [],
  };
}

/** `figures` of `GET /roi` (the ledger): *runs* records, the viewer's own share when `own_share_only`. */
function ledger(runs: number, totals: Record<string, number> = {}, suppressed: { k: number; own_share_only: boolean } | null = null) {
  const t = {
    runs,
    reviewed: 0,
    units: runs,
    deliverables: 0,
    hours: runs * 2,
    hours_low: runs,
    hours_high: runs * 3,
    value: runs * 100,
    value_low: runs * 50,
    value_high: runs * 150,
    ai_cost: 0.5,
    net_value: runs * 100,
    net_value_low: runs * 50,
    net_value_high: runs * 150,
    roi: null,
    payback_months: null,
    ...totals,
  };
  return {
    scope: "agent",
    currency: "EUR",
    totals: t,
    by_source: [],
    by_unit: [],
    by_agent: [],
    by_fleet: [],
    by_week: [],
    by_person: null,
    cost_masked: false,
    since: null,
    suppressed: suppressed && { groups: 0, sources_split: true, weeks_merged: 0, ...suppressed },
  };
}

function serve({
  windowRuns,
  lifetimeRuns = windowRuns,
  fail = false,
  extra = {},
}: {
  windowRuns: number;
  lifetimeRuns?: number;
  fail?: boolean;
  extra?: Record<string, number>;
}) {
  get.mockImplementation(async (url: string) => {
    // The chat series is its own read (#10544 S5): an empty one here.
    if (url.startsWith("/roi/chat")) return { currency: "EUR", totals: { deliverables: 0, pending: 0, corrected: 0, minutes: 0, hours: 0, value: 0 }, lines: [] };
    if (fail) throw Object.assign(new Error("boom"), { status: 500 });
    return url.includes("window=all") ? report(lifetimeRuns, extra) : report(windowRuns, extra);
  });
}

function Where() {
  const location = useLocation();
  return <p data-testid="where">{`${location.pathname}${location.search}`}</p>;
}

function renderSection(url = "/agents/a-1", canManage = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: 3, throwOnError: true } } });
  return renderWithProviders(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/agents/:id/:tab?" element={<><AgentRoiSection agentId="a-1" canManage={canManage} /><Where /></>} />
        <Route path="/agents/:id/:tab/:section" element={<Where />} />
      </Routes>
    </MemoryRouter>,
    client,
  );
}

afterEach(() => {
  get.mockReset();
  vi.restoreAllMocks();
});

describe("the agent ROI section", () => {
  it("reads the agent's report on the page's window and shows the three cards", async () => {
    serve({ windowRuns: 4 });
    renderSection("/agents/a-1?window=7d");
    expect(await screen.findByText("Review rate")).toBeTruthy();
    expect(get).toHaveBeenCalledWith("/roi?scope=agent&id=a-1&window=7d");
    expect(screen.getByText("Hours saved")).toBeTruthy();
    expect(screen.getByText("Net value")).toBeTruthy();
    // A window with runs never reads the lifetime report.
    expect(get).not.toHaveBeenCalledWith("/roi?scope=agent&id=a-1&window=all");
  });

  it("opens the agent's ROI records, runs and work items, on the card's window (#10570)", async () => {
    serve({ windowRuns: 4 });
    renderSection("/agents/a-1?window=7d");
    const card = (await screen.findByText("Review rate")).closest("[role='button']") as HTMLElement;
    fireEvent.click(card);
    expect(screen.getByTestId("where").textContent).toBe(agentReviewPath("a-1", "7d"));
    expect(agentReviewPath("a-1", "7d")).toBe("/agents/a-1/activity/records?review=unreviewed&window=7d");
  });

  it("says the rate's denominator, runs and work items, as optional (#10570)", async () => {
    // 5 records: 4 runs and 1 work item; 3 reviewed.
    serve({ windowRuns: 5, extra: { reviewed: 3, unreviewed: 2, review_rate: 0.6, work_items: 1, unreviewed_work_items: 1 } });
    renderSection();
    expect(await screen.findByText("Reviewed 3 of 5 records (optional)")).toBeTruthy();
    expect(screen.getByText("4 runs, 1 work item")).toBeTruthy();
  });

  it("keeps the plain counts when every record is a run", async () => {
    serve({ windowRuns: 4 });
    renderSection();
    expect(await screen.findByText("Reviewed 1 of 4 records (optional)")).toBeTruthy();
    expect(screen.getByText("4 runs")).toBeTruthy();
  });

  it("says the agent is not counted when nothing ever was, with the way to pick a unit", async () => {
    serve({ windowRuns: 0, lifetimeRuns: 0 });
    renderSection();
    expect(await screen.findByText("This agent is not counted in ROI.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Pick a unit" }));
    // Configuration > Outcomes, on the unit picker (UX #9810 B2), not General.
    expect(screen.getByTestId("where").textContent).toBe("/agents/a-1/config/outcomes?pick=unit");
  });

  it("tells a viewer who cannot manage the agent who picks its unit, with no Pick a unit action (UX #9810 r3 B1)", async () => {
    serve({ windowRuns: 0, lifetimeRuns: 0 });
    renderSection("/agents/a-1", false);
    expect(await screen.findByText("This agent is not counted in ROI.")).toBeTruthy();
    expect(screen.getByText("Ask the agent's owner to pick a unit of work: only someone who can manage the agent sets it.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Pick a unit" })).toBeNull();
  });

  it("says how the figures are reached, with their numbers and the rejected runs left out (UX #9810 r3 B2)", async () => {
    get.mockImplementation(async () => {
      const r = report(5);
      r.organization.computation = [
        { kind: "saving", unit: "case", name: "Case triaged", units: 10, manual_min: 200, review_min: 30, minutes_saved: 170, rate: 88.73, value: 251.4, currency: "USD" },
        { kind: "total", minutes_saved: 170, hours_saved: 2.83, value: 251.4, llm_cost: 0.42, net_value: 250.98, currency: "USD" },
        { kind: "review", reviewed: 3, total: 5 },
        { kind: "rejected", count: 1 },
      ];
      return r;
    });
    renderSection();
    expect(await screen.findByText("How this is computed")).toBeTruthy();
    const lines = screen.getByTestId("roi-computation").textContent ?? "";
    expect(lines).toContain("Case triaged: 10 units, 200 manual min minus 30 review min = 170 min saved");
    expect(lines).toContain("170 min = 2.8 h");
    expect(lines).toContain("Review rate: 3 reviewed of 5 records");
    expect(lines).toContain("1 rejected run counts no time saved, only its cost.");
  });

  it("says nothing was counted in this period and widens the window", async () => {
    serve({ windowRuns: 0, lifetimeRuns: 3 });
    renderSection("/agents/a-1?window=7d");
    expect(await screen.findByText("Nothing was counted in this period.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Widen the window" }));
    expect(screen.getByTestId("where").textContent).toBe("/agents/a-1");
  });

  it("says how the figures are computed under the cards, with the shared Assumptions block (UX #9810 r2 B1)", async () => {
    get.mockImplementation(async () => {
      const r = report(4);
      r.organization.assumption_facts = [
        { kind: "unit", unit: "case", name: "Case triaged", manual_min: 30, review_min: 5, label: "assumption" },
        { kind: "measured_review" },
      ];
      return r;
    });
    renderSection();
    expect(await screen.findByText("Assumptions")).toBeTruthy();
    expect(screen.getByText("Review minutes: measured where recorded, the baseline otherwise.")).toBeTruthy();
  });

  it("renders for any reader of the agent: the section has no administrator gate (UX #9810 r2 B2)", async () => {
    // renderWithProviders signs nobody in as an administrator; the API decides who reads the agent's scope.
    serve({ windowRuns: 4 });
    renderSection();
    expect(await screen.findByText("Review rate")).toBeTruthy();
    expect(screen.getByTestId("agent-roi-section")).toBeTruthy();
  });

  it("draws the ledger's totals, the figure the Value page and the export show (#12077 L6b3, invariant d)", async () => {
    get.mockImplementation(async (url: string) => {
      if (url.startsWith("/roi/chat")) return { currency: "EUR", totals: { deliverables: 0, pending: 0, corrected: 0, minutes: 0, hours: 0, value: 0 }, lines: [] };
      return { ...report(4), figures: ledger(4, { hours: 37 }) };
    });
    renderSection();
    expect(await screen.findByText("Review rate")).toBeTruthy();
    // The report's own figures say 8 h; the ledger's 37 h is the one drawn.
    expect(screen.getByTestId("roi-figure-hours-value").textContent).toBe("37 h");
    expect(screen.queryByTestId("roi-under-k")).toBeNull();
  });

  it("under k, shows the viewer's own share with the k caption and no review link (design 12.5)", async () => {
    get.mockImplementation(async (url: string) => {
      if (url.startsWith("/roi/chat")) return { currency: "EUR", totals: { deliverables: 0, pending: 0, corrected: 0, minutes: 0, hours: 0, value: 0 }, lines: [] };
      return { ...report(9), figures: ledger(1, { hours: 3 }, { k: 5, own_share_only: true }) };
    });
    renderSection();
    expect(await screen.findByText("Your use of this agent")).toBeTruthy();
    expect(screen.getByText("Totals are shown once 5 people use this agent in the period.")).toBeTruthy();
    // No scope-wide assumption nor computation step under the viewer's own share.
    expect(screen.queryByText("Assumptions")).toBeNull();
  });

  it("draws the hero of a chat-only agent, never 'not counted' (#12077 L6b3 UX r1 B2)", async () => {
    get.mockImplementation(async (url: string) => {
      if (url.startsWith("/roi/chat")) return { currency: "EUR", totals: { deliverables: 0, pending: 0, corrected: 0, minutes: 0, hours: 0, value: 0 }, lines: [] };
      // No automation run, in the window or ever; two chat deliverables carry value.
      return { ...report(0), figures: ledger(0, { deliverables: 2, hours: 6, value: 600, net_value: 599.5 }) };
    });
    renderSection("/agents/a-1", false);
    expect(await screen.findByText("Net value")).toBeTruthy();
    expect(screen.queryByText("This agent is not counted in ROI.")).toBeNull();
    // The window holds value: the lifetime report is never read.
    expect(get).not.toHaveBeenCalledWith("/roi?scope=agent&id=a-1&window=all");
  });

  it("sums the computation to the hero, chat share included (#12077 L6b3 UX r1 B1)", async () => {
    get.mockImplementation(async (url: string) => {
      if (url.startsWith("/roi/chat")) return { currency: "EUR", totals: { deliverables: 0, pending: 0, corrected: 0, minutes: 0, hours: 0, value: 0 }, lines: [] };
      const r = report(4);
      r.organization.computation = [
        { kind: "total", minutes_saved: 480, hours_saved: 8, value: 400, llm_cost: 0.5, net_value: 399.5, currency: "EUR" },
        { kind: "review", reviewed: 1, total: 4 },
      ];
      const f = ledger(4, { deliverables: 2, hours: 14, value: 1000, ai_cost: 0.5, net_value: 999.5 });
      const row = (key: string, hours: number, value: number) => ({ ...f.totals, key, hours, value });
      return { ...r, figures: { ...f, by_source: [row("automation", 8, 400), row("chat", 6, 600)] } };
    });
    renderSection();
    expect(await screen.findByText("How this is computed")).toBeTruthy();
    const lines = screen.getByTestId("roi-computation").textContent ?? "";
    expect(lines).toContain("Automations: 8 h");
    expect(lines).toContain("Chat (estimated): 6 h");
    // The total line is the hero's (14 h), not the report's own 8 h.
    expect(lines).toContain("= 14 h");
    expect(lines).not.toContain("= 8 h");
    expect(lines).toContain("Review rate: 1 reviewed of 4 records");
  });

  it("under k, hides the totals when the viewer did not use the agent", async () => {
    get.mockImplementation(async (url: string) => {
      if (url.startsWith("/roi/chat")) return { currency: "EUR", totals: { deliverables: 0, pending: 0, corrected: 0, minutes: 0, hours: 0, value: 0 }, lines: [] };
      return { ...report(9), figures: ledger(0, {}, { k: 5, own_share_only: true }) };
    });
    renderSection();
    expect(await screen.findByText("Fewer than 5 people used this agent in the period: totals are hidden.")).toBeTruthy();
    expect(screen.queryByText("Review rate")).toBeNull();
    // Never the lifetime read: the k rule, not an empty window, explains the empty hero.
    expect(get).not.toHaveBeenCalledWith("/roi?scope=agent&id=a-1&window=all");
  });

  it("keeps a failed read in its own error line, with Retry", async () => {
    serve({ windowRuns: 0, fail: true });
    renderSection();
    expect(await screen.findByText("The ROI of this agent could not be read.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });
});
