/**
 * The Value page's arithmetic (#9836 R5b, design roi-outcomes 10.7) - `yarn test:unit`.
 * Each table plus its remainders adds up to the hero (round 2 B1), the scope
 * switch follows the reader and the organization setting, and the review line
 * counts runs and work items apart from the hero's own figures.
 */
import { describe, expect, it } from "vitest";
import { roiExportPath, type RoiLedgerFigures, type RoiLedgerRow, type RoiReportResponse } from "@/hooks/api/useRoiReportApi";
import {
  ledgerHeroFigures,
  ledgerTable,
  ledgerTrend,
  parseValueScope,
  parseValueTab,
  isValueTab,
  parseValueWindow,
  valueRemainders,
  valueReviewShares,
  valueReviewCounts,
  valueRows,
  valueScopes,
  VALUE_SCOPE_LINES,
  valueCostNote,
  valueIsEmpty,
  valueTotals,
  valueTrend,
  widerValueWindow,
} from "@/lib/roi-value";

const figures = (runs: number, hours: number, value: number, cost: number | null) => ({
  currency: "USD",
  runs,
  units: runs,
  hours_saved: hours,
  llm_cost: cost,
  value,
  net_value: cost === null ? null : value - cost,
  roi: null,
  reviewed: 0,
  unreviewed: runs,
  rejected: 0,
  review_rate: runs ? 0 : null,
  assumptions: [],
});

function report(costMasked = false): RoiReportResponse {
  const c = (n: number) => (costMasked ? null : n);
  return {
    window: "30d",
    currency: "USD",
    rate_label: "assumption",
    estimated: true,
    cost_masked: costMasked,
    organization: { ...figures(10, 20, 2000, c(30)), work_items: 2, unreviewed_work_items: 1, reviewed: 6, unreviewed: 4 },
    fleets: [{ id: "f1", name: "Alpha", ...figures(5, 10, 1000, c(10)) }],
    agents: [
      { id: "a1", name: "Bravo", ...figures(4, 8, 800, c(12)) },
      { id: "a2", name: "Charlie", ...figures(3, 6, 600, c(8)) },
    ],
    units: [
      { id: "u1", name: "Triage", ...figures(6, 12, 1200, c(18)) },
      { id: "u2", name: "Report", ...figures(4, 8, 800, c(12)) },
    ],
    no_fleet: figures(3, 6, 600, c(10)),
    other_fleets: figures(2, 4, 400, c(10)),
    other_agents: figures(3, 6, 600, c(10)),
    trend: [
      { week: "2026-10-26", value: 900, value_range: [600, 1300], net_value: c(880), net_value_range: [null, null], runs: 4 },
      { week: "2026-11-02", value: 1100, value_range: [700, 1500], net_value: c(1090), net_value_range: [null, null], runs: 6 },
    ],
  };
}

describe("Value page arithmetic", () => {
  it("adds each table and its remainders up to the hero", () => {
    const data = report();
    for (const tab of ["fleets", "agents", "units"] as const) {
      const totals = valueTotals(valueRows(data, tab), valueRemainders(data, tab));
      expect(totals.value, tab).toBe(data.organization.value);
      expect(totals.hours, tab).toBe(data.organization.hours_saved);
      expect(totals.cost, tab).toBe(data.organization.llm_cost);
    }
  });

  it("keeps a masked cost hidden in every sum, never 0", () => {
    const data = report(true);
    const totals = valueTotals(valueRows(data, "fleets"), valueRemainders(data, "fleets"));
    expect(totals.cost).toBeNull();
    expect(totals.net).toBeNull();
    expect(totals.value).toBe(2000);
    expect(valueTrend(data.trend).every((point) => point.net === null)).toBe(true);
  });

  it("puts agents outside a fleet in By fleet only, and the folded rows in their own tab", () => {
    const data = report();
    expect(valueRemainders(data, "fleets").map((r) => r.key)).toEqual(["no_fleet", "other"]);
    expect(valueRemainders(data, "agents").map((r) => r.key)).toEqual(["other"]);
    expect(valueRemainders(data, "units")).toEqual([]);
  });

  it("sorts rows by value, highest first", () => {
    expect(valueRows(report(), "agents").map((row) => row.name)).toEqual(["Bravo", "Charlie"]);
  });

  it("counts runs and work items apart in the review line", () => {
    expect(valueReviewShares(report().organization)).toEqual({
      runs: { reviewed: 5, total: 8 },
      workItems: { reviewed: 1, total: 2 },
    });
  });

  it("offers the scopes per reader and setting", () => {
    expect(valueScopes(true, false)).toEqual({ scopes: ["mine", "organization"], fallback: "organization" });
    expect(valueScopes(false, true)).toEqual({ scopes: ["mine", "organization"], fallback: "mine" });
    expect(valueScopes(false, false)).toEqual({ scopes: ["mine"], fallback: "mine" });
    expect(parseValueScope("organization", valueScopes(false, false))).toBe("mine");
  });

  it("names Mine as what the reader did, never as their agents and fleets (UX #12247 r1 A1)", () => {
    expect(VALUE_SCOPE_LINES).toEqual({ mine: "What you did", organization: "Whole organization" });
  });

  it("reads and widens the windows, the quarter included", () => {
    expect(parseValueWindow("quarter")).toBe("quarter");
    expect(parseValueWindow("bogus")).toBe("30d");
    expect(widerValueWindow("quarter")).toBe("all");
    expect(widerValueWindow("90d")).toBe("all");
    expect(widerValueWindow("all")).toBeNull();
  });

  it("keeps every known tab and sends an unknown or retired one to the default tab (#12229)", () => {
    expect(parseValueTab("fleets")).toBe("fleets");
    expect(parseValueTab("agents")).toBe("agents");
    expect(parseValueTab("units")).toBe("units");
    expect(parseValueTab(undefined)).toBe("fleets");
    expect(parseValueTab("")).toBe("fleets");
    expect(parseValueTab("by-agent")).toBe("fleets");
    expect(parseValueTab("constructor")).toBe("fleets");
    expect(isValueTab(undefined)).toBe(true);
    expect(isValueTab("agents")).toBe(true);
    expect(isValueTab("by-agent")).toBe(false);
    expect(isValueTab("constructor")).toBe(false);
  });

  it("draws the value's range as the trend band", () => {
    expect(valueTrend(report().trend)[0].band).toEqual([600, 1300]);
  });

  it("draws the cost no unit carries as a remainder of By unit of work, not a counted row (#10441)", () => {
    const data = report();
    data.organization = { ...data.organization, llm_cost: 35, net_value: 1965 };
    data.units = [...(data.units ?? []), { id: "_cost_not_tied_to_a_unit", name: "_cost_not_tied_to_a_unit", ...figures(0, 0, 0, 5) }];
    const rows = valueRows(data, "units");
    const remainders = valueRemainders(data, "units");
    expect(rows.map((row) => row.id)).toEqual(["u1", "u2"]);
    expect(remainders.map((r) => [r.key, r.label, r.row.cost])).toEqual([["unallocated", "Cost not tied to a unit", 5]]);
    expect(valueTotals(rows, remainders).cost).toBe(35);
  });
});

const ledgerRow = (key: string, runs: number, hours: number, value: number, cost: number | null): RoiLedgerRow => ({
  key,
  runs,
  reviewed: Math.floor(runs / 2),
  units: runs,
  deliverables: 0,
  hours,
  hours_low: hours / 2,
  hours_high: hours * 2,
  value,
  value_low: value / 2,
  value_high: value * 2,
  ai_cost: cost,
  net_value: cost === null ? null : value - cost,
  net_value_low: cost === null ? null : value / 2 - cost,
  net_value_high: cost === null ? null : value * 2 - cost,
  roi: null,
  payback_months: null,
});

/** A ledger answer whose hero differs from the legacy `organization` block (the #12147 retest's hero defect). */
function ledger(costMasked = false): RoiLedgerFigures {
  const c = (n: number) => (costMasked ? null : n);
  const { key: _key, until: _until, ...totals } = ledgerRow("total", 20, 733.33, 70000, c(300));
  return {
    scope: "mine",
    currency: "USD",
    totals,
    by_source: [ledgerRow("automation", 20, 733.33, 70000, c(300))],
    by_agent: [ledgerRow("a1", 8, 300, 30000, c(100)), ledgerRow("a2", 6, 233.33, 20000, c(100)), ledgerRow("other", 4, 100, 10000, c(50)), ledgerRow("no_agent", 2, 100, 10000, c(50))],
    by_fleet: [ledgerRow("f1", 12, 500, 45000, c(200)), ledgerRow("no_fleet", 8, 233.33, 25000, c(100))],
    by_unit: [ledgerRow("triage", 15, 600, 60000, c(250)), ledgerRow("other_units", 3, 83.33, 6000, c(30)), ledgerRow("no_unit", 2, 50, 4000, c(20))],
    by_week: [ledgerRow("2026-11-09", 12, 400, 40000, c(200)), ledgerRow("2026-11-02", 8, 333.33, 30000, c(100))],
    by_person: null,
    cost_masked: costMasked,
    since: null,
    names: { a1: "Bravo", a2: "Alpha", f1: "CTEM", triage: "Threat report triaged" },
  };
}

describe("Value page on the ledger's figures (#12077 slice L6a)", () => {
  it("draws the hero from figures.totals, not the legacy organization block", () => {
    const data = { ...report(), figures: ledger() };
    const hero = ledgerHeroFigures(data);
    expect(hero?.hours_saved).toBe(733.33);
    expect(hero?.value).toBe(70000);
    expect(hero?.llm_cost).toBe(300);
    expect(hero?.range?.value).toEqual([35000, 140000]);
    expect(hero?.computation?.some((step) => step.kind === "saving")).toBe(false);
    expect(ledgerHeroFigures(report())).toBeNull();
  });

  it("adds every tab and its remainders up to the hero, which is the export's total line (invariants c and d)", () => {
    const figures = ledger();
    for (const tab of ["fleets", "agents", "units"] as const) {
      const { rows, remainders } = ledgerTable(figures, tab);
      const totals = valueTotals(rows, remainders);
      expect(totals.value, tab).toBe(figures.totals.value);
      expect(totals.hours, tab).toBeCloseTo(figures.totals.hours, 2);
      expect(totals.cost, tab).toBe(figures.totals.ai_cost);
    }
  });

  it("names the object rows and draws residual and folded rows as remainders, in order", () => {
    const agents = ledgerTable(ledger(), "agents");
    expect(agents.rows.map((row) => row.name)).toEqual(["Bravo", "Alpha"]);
    expect(agents.remainders.map((r) => r.key)).toEqual(["no_agent", "other"]);
    expect(ledgerTable(ledger(), "units").remainders.map((r) => [r.key, r.label])).toEqual([
      ["no_unit", "No unit of work"],
      ["other_units", "Other units of work"],
    ]);
    expect(ledgerTable(ledger(), "fleets").remainders.map((r) => r.key)).toEqual(["no_fleet"]);
  });

  it("keeps a masked cost hidden in the hero, the tables and the trend, never 0", () => {
    const data = { ...report(true), figures: ledger(true) };
    expect(ledgerHeroFigures(data)?.llm_cost).toBeNull();
    const { rows, remainders } = ledgerTable(data.figures, "agents");
    expect(valueTotals(rows, remainders).cost).toBeNull();
    expect(ledgerTrend(data.figures).every((point) => point.net === null)).toBe(true);
  });

  it("draws By week oldest first with the value band", () => {
    expect(ledgerTrend(ledger()).map((p) => [p.week, p.band])).toEqual([
      ["2026-11-02", [15000, 60000]],
      ["2026-11-09", [20000, 80000]],
    ]);
  });

  it("draws the hero of a chat-only scope: no run, but value, is not empty (UX round 1 B1)", () => {
    const chatOnly = ledger();
    chatOnly.totals = { ...chatOnly.totals, runs: 0, reviewed: 0, deliverables: 3 };
    expect(valueIsEmpty({ ...report(), figures: chatOnly })).toBe(false);
    const empty = ledger();
    empty.totals = { ...empty.totals, runs: 0, reviewed: 0, units: 0, deliverables: 0, hours: 0, hours_low: 0, hours_high: 0, value: 0, value_low: 0, value_high: 0 };
    expect(valueIsEmpty({ ...report(), figures: empty })).toBe(true);
    // An older backend without figures: the report's run count.
    expect(valueIsEmpty(report())).toBe(false);
  });

  it("counts the review line from figures.totals and hides it on a chat-only scope (UX round 2 B4)", () => {
    const scoped = ledger();
    scoped.totals = { ...scoped.totals, runs: 4, reviewed: 3 };
    expect(valueReviewCounts({ ...report(), figures: scoped })).toEqual({
      runs: { reviewed: 3, total: 4 },
      workItems: { reviewed: 0, total: 0 },
    });
    const chatOnly = ledger();
    chatOnly.totals = { ...chatOnly.totals, runs: 0, reviewed: 0, deliverables: 3 };
    // The organization block counts 8 runs: never shown for a scope with none.
    expect(valueReviewCounts({ ...report(), figures: chatOnly })).toBeNull();
    // An older backend without figures: the report's own counts.
    expect(valueReviewCounts(report())).toEqual(valueReviewShares(report().organization));
  });

  it("takes the cost note's unallocated part from the ledger's no_unit row (UX round 1 B3)", () => {
    const data = { ...report(), cost_source: "llm_analytics_by_agent", unallocated_cost: 1.4, figures: ledger() };
    expect(valueCostNote(data)).toEqual({ unallocated: 20 });
    expect(valueCostNote({ ...data, figures: ledger(true) })).toBeNull();
    expect(valueCostNote({ ...data, figures: undefined })).toEqual({ unallocated: 1.4 });
  });

  it("exports the scope the page shows, mine included, and a fleet by its id", () => {
    expect(roiExportPath({ window: "30d", format: "csv", scope: "mine" })).toBe("/roi/export?format=csv&window=30d&scope=mine");
    expect(roiExportPath({ window: "90d", format: "pdf", scope: "platform" })).toBe("/roi/export?format=pdf&window=90d&scope=platform");
    expect(roiExportPath({ window: "7d", format: "csv", fleetId: "f1" })).toBe("/roi/export?format=csv&window=7d&fleet_id=f1");
  });
});
