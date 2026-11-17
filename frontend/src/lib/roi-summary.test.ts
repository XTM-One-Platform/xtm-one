/**
 * The "Net value (30d)" list column (#9836 R6b, design 10.8; ledger since #12077 L7a): one
 * summary row is the ledger's figures of the object, drawn through the agent page hero's own
 * mapping, so the list column and the hero show one figure (invariant d); a masked cost is
 * never 0, a row with nothing declared has no figure and sorts after every figure, and a list
 * is read in chunks of at most 200 ids (one read per page, never per row).
 */
import { describe, expect, it } from "vitest";

import { ROI_SUMMARY_MAX_IDS, roiSummaryChunks, type RoiLedgerFigures, type RoiLedgerTotals, type RoiReportResponse, type RoiSummaryItem } from "@/hooks/api/useRoiReportApi";
import { metricValue, roiComputation } from "@/lib/roi-figure";
import { scopeHeroFigures, scopeUnderK } from "@/lib/roi-outcomes";
import { ROI_COLUMN_HEADERS, roiColumnMetric, summaryFigures, summarySortValue, summaryUnderK } from "@/lib/roi-summary";

const totals = (masked = false): RoiLedgerTotals => ({
  runs: 12,
  reviewed: 4,
  units: 16,
  deliverables: 2,
  hours: 9.5,
  hours_low: 6,
  hours_high: 14,
  value: 712.5,
  value_low: 450,
  value_high: 1050,
  ai_cost: masked ? null : 1.5,
  net_value: masked ? null : 711,
  net_value_low: masked ? null : 448.5,
  net_value_high: masked ? null : 1048.5,
  roi: masked ? null : 474,
  payback_months: masked ? null : 0,
});

function ledger(masked = false, ownShareOnly = false): RoiLedgerFigures {
  const t = totals(masked);
  return {
    scope: "agent",
    currency: "USD",
    totals: t,
    by_source: [
      { key: "automation", ...t, deliverables: 0, hours: 8, value: 600 },
      { key: "chat", ...t, runs: 0, deliverables: 2, hours: 1.5, value: 112.5 },
    ],
    by_unit: [],
    by_agent: [{ key: "a1", ...t }],
    by_fleet: [],
    by_week: [],
    by_person: null,
    cost_masked: masked,
    since: null,
    suppressed: ownShareOnly ? { k: 5, groups: 0, own_share_only: true, sources_split: true, weeks_merged: 0 } : null,
  };
}

const row = (figures: RoiLedgerFigures): RoiSummaryItem => ({
  id: "a1",
  declared: true,
  figures: { scope: figures.scope, currency: figures.currency, totals: figures.totals, by_source: figures.by_source, cost_masked: figures.cost_masked, suppressed: figures.suppressed },
});

/** The agent page's answer for the same agent, reader and window: `GET /roi?scope=agent&id=a1`. */
function agentReport(figures: RoiLedgerFigures): RoiReportResponse {
  return {
    window: "30d",
    currency: "USD",
    rate_label: "assumption",
    estimated: true,
    cost_masked: figures.cost_masked,
    scope: "agent",
    organization: { currency: "USD", runs: 99, units: 99, hours_saved: 99, llm_cost: 99, value: 99, net_value: 99, roi: 99, reviewed: 0, unreviewed: 0, rejected: 0, review_rate: null, assumptions: [] },
    agents: [],
    fleets: [],
    figures,
  };
}

describe("roi summary column", () => {
  for (const [name, masked] of [
    ["priced", false],
    ["costs masked", true],
  ] as const) {
    it(`draws the agent page hero's figure for the same agent (invariant d, ${name})`, () => {
      const figures = ledger(masked);
      const cell = summaryFigures(row(figures))!;
      const hero = scopeHeroFigures(agentReport(figures));
      for (const metric of ["hours", "value", "net", "cost", "roi"] as const) {
        expect(metricValue(cell, metric), metric).toBe(metricValue(hero, metric));
      }
      expect(cell.range).toEqual(hero.range);
      expect(cell.computation?.filter((s) => s.kind === "source" || s.kind === "total")).toEqual(hero.computation?.filter((s) => s.kind === "source" || s.kind === "total"));
    });
  }

  it("is in the agent page hero's under-k state, never a figure among the totals (L7f, design 12.10)", () => {
    for (const masked of [false, true]) {
      const figures = ledger(masked, true);
      expect(summaryUnderK(row(figures))).toEqual(scopeUnderK(agentReport(figures)));
      expect(summaryUnderK(row(figures))).toEqual({ k: 5, viewerUsed: true });
      expect(summarySortValue(row(figures), masked)).toBeNull();
    }
    const unused = ledger(false, true);
    unused.totals = { ...unused.totals, runs: 0, deliverables: 0, hours: 0, value: 0 };
    expect(summaryUnderK(row(unused))).toEqual(scopeUnderK(agentReport(unused)));
    expect(summaryUnderK(row(unused))).toEqual({ k: 5, viewerUsed: false });
    expect(summaryUnderK(row(ledger()))).toBeNull();
    expect(summaryUnderK({ id: "a4", declared: false })).toBeNull();
  });

  it("says in its popover the rejected records, work items and inputs the agent page says (UX r1 B1)", () => {
    const figures = ledger();
    const unit = { kind: "unit" as const, unit: "threat_report_triaged", name: "Threat report triaged", manual_min: 30, review_min: 5, label: "assumption", source: "catalog" };
    const rate = { kind: "rate" as const, role: "analyst", amount: 90, currency: "USD", label: "assumption", source: "catalog" };
    const report = agentReport(figures);
    report.organization = { ...report.organization, rejected: 1, work_items: 2, unreviewed_work_items: 1, assumption_facts: [unit, rate], computation: [{ kind: "rejected", count: 1 }] };
    const cell = summaryFigures({ ...row(figures), records: { rejected: 1, work_items: 2, unreviewed_work_items: 1, assumption_facts: [unit, rate] } })!;
    const mine = roiComputation(cell, false);
    const hero = roiComputation(scopeHeroFigures(report), false);
    for (const key of ["rejected", "runs", "workItems", "unitFacts", "rateFacts", "sources", "value", "net"] as const) {
      expect(mine[key], key).toEqual(hero[key]);
    }
    expect(cell.computation?.filter((s) => s.kind === "rejected")).toEqual([{ kind: "rejected", count: 1 }]);
  });

  it("draws the ledger's numbers, never the report's own", () => {
    const figures = summaryFigures(row(ledger()))!;
    expect(metricValue(figures, "net")).toBe(711);
    expect(metricValue(figures, "hours")).toBe(9.5);
    expect(metricValue(figures, "value")).toBe(712.5);
    expect(figures.deliverables).toBe(2);
  });

  it("names the net value, and the gross value when costs are masked", () => {
    expect(ROI_COLUMN_HEADERS[roiColumnMetric(false)]).toBe("Net value (30d)");
    expect(ROI_COLUMN_HEADERS[roiColumnMetric(true)]).toBe("Value (30d)");
  });

  it("never turns a masked cost into 0", () => {
    const masked = row(ledger(true));
    const figures = summaryFigures(masked)!;
    expect(figures.net_value).toBeNull();
    expect(figures.llm_cost).toBeNull();
    expect(summarySortValue(masked, true)).toBe(712.5);
    expect(summarySortValue(masked, false)).toBeNull();
  });

  it("has no figure for a row that declares nothing or that the reader cannot read", () => {
    expect(summaryFigures({ id: "a4", declared: false })).toBeNull();
    expect(summaryFigures(undefined)).toBeNull();
    expect(summarySortValue({ id: "a4", declared: false }, false)).toBeNull();
    expect(summarySortValue(row(ledger()), false)).toBe(711);
  });

  it("reads a list in sorted, deduplicated chunks of at most 200 ids", () => {
    const ids = Array.from({ length: 450 }, (_, i) => `id-${String(i).padStart(3, "0")}`);
    const chunks = roiSummaryChunks([...ids].reverse().concat(ids.slice(0, 10)));
    expect(chunks.map((c) => c.length)).toEqual([ROI_SUMMARY_MAX_IDS, ROI_SUMMARY_MAX_IDS, 50]);
    expect(chunks.flat()).toEqual(ids);
    expect(roiSummaryChunks([])).toEqual([]);
  });
});
