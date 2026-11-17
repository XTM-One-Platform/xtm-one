/**
 * Unit tests for the Analytics Outcomes tab rows and states (#9706) - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { ledgerOutcomeRows, outcomeRows, outcomesState, scopeHeroFigures, widerWindow } from "./roi-outcomes.ts";
import { exactCost, formatCost } from "./trace-utils.ts";
import type { RoiLedgerFigures, RoiLedgerRow, RoiReportResponse } from "../hooks/api/useRoiReportApi.ts";

const figures = (extra: Partial<RoiReportResponse["organization"]> = {}) => ({
  currency: "EUR",
  runs: 0,
  units: 0,
  hours_saved: 0,
  llm_cost: 0,
  value: 0,
  net_value: 0,
  roi: null,
  reviewed: 0,
  unreviewed: 0,
  rejected: 0,
  review_rate: null,
  assumptions: [],
  ...extra,
});

const report = (
  runs: number,
  fleets: { id: string; name: string; net: number }[] = [],
  agents: { id: string; name: string; net: number }[] = [],
): RoiReportResponse => ({
  window: "30d",
  currency: "EUR",
  rate_label: "assumption",
  estimated: true,
  organization: figures({ runs }),
  fleets: fleets.map((f) => ({ ...figures({ runs: 2, net_value: f.net, review_rate: 0.5 }), id: f.id, name: f.name })),
  agents: agents.map((a) => ({ ...figures({ runs: 1, net_value: a.net }), id: a.id, name: a.name })),
});

test("no report: no row and the data state (the loading state is the caller's)", () => {
  assert.deepEqual(outcomeRows(undefined), []);
  assert.equal(outcomesState(undefined, "30d"), "data");
});

test("fleets come first, then agents, each by net value then name", () => {
  const rows = outcomeRows(
    report(3, [{ id: "f1", name: "Beta", net: 10 }, { id: "f2", name: "Alpha", net: 10 }, { id: "f3", name: "Zed", net: 50 }], [{ id: "a1", name: "Writer", net: 99 }]),
  );
  assert.deepEqual(rows.map((r) => `${r.kind}:${r.name}`), ["fleet:Zed", "fleet:Alpha", "fleet:Beta", "agent:Writer"]);
  assert.equal(rows[0].reviewRate, 0.5);
  assert.equal(rows[3].reviewRate, null);
});

test("a sub-dollar cost row keeps its cents through the shared formatCost (UX round 2 B1)", () => {
  const base = report(3, [{ id: "f1", name: "Lab sample fleet", net: 5 }]);
  const withCost: RoiReportResponse = { ...base, fleets: base.fleets.map((f) => ({ ...f, llm_cost: 0.42 })) };
  const [row] = outcomeRows(withCost);
  assert.equal(row.cost, 0.42);
  assert.equal(formatCost(row.cost), "$0.42");
  assert.equal(formatCost(1.02), "$1.02");
  assert.equal(formatCost(0.004), "$0.0040");
  assert.equal(formatCost(0), "$0.00");
  assert.equal(formatCost(null), "—");
});

test("a run cost of $1,000 or more is compact with its exact amount on hover (#10476)", () => {
  assert.equal(formatCost(1234.5), "$1.2K");
  assert.equal(exactCost(1234.5), "$1,234.50");
  assert.equal(formatCost(752.4), "$752");
  assert.equal(exactCost(752.4), undefined);
  assert.equal(exactCost(0.42), undefined);
  assert.equal(exactCost(null), undefined);
  assert.equal(formatCost(0, "fr").replace(/[\u00a0\u202f]/g, " "), "0,00 $US");
});

const ledgerRow = (key: string, runs: number, value: number, cost: number, deliverables = 0): RoiLedgerRow => ({
  key,
  runs,
  reviewed: 0,
  units: runs,
  deliverables,
  hours: value / 100,
  hours_low: value / 200,
  hours_high: value / 50,
  value,
  value_low: value / 2,
  value_high: value * 2,
  ai_cost: cost,
  net_value: value - cost,
  net_value_low: value / 2 - cost,
  net_value_high: value * 2 - cost,
  roi: null,
  payback_months: null,
});

/** A platform answer whose ledger differs from its legacy blocks (chat value counted, an agent the legacy rows miss). */
function withLedger(base: RoiReportResponse, chatOnly = false): RoiReportResponse {
  const { key: _key, ...totals } = chatOnly ? ledgerRow("total", 0, 500, 5, 3) : ledgerRow("total", 6, 9000, 90);
  const ledger: RoiLedgerFigures = {
    scope: "organization",
    currency: "EUR",
    totals,
    by_source: [],
    by_unit: [],
    by_agent: [ledgerRow("a1", 4, 6000, 60), ledgerRow("a2", 1, 2000, 20), ledgerRow("no_agent", 1, 1000, 10)],
    by_fleet: [ledgerRow("f1", 5, 8000, 80), ledgerRow("no_fleet", 1, 1000, 10)],
    by_week: [],
    by_person: null,
    cost_masked: false,
    since: null,
    names: { a1: "Writer", a2: "Reader", f1: "CTEM" },
  };
  return { ...base, figures: ledger };
}

test("on the ledger, the rows are its fleet then agent objects and the hero is its totals (#12077 L6b4, invariant d)", () => {
  const data = withLedger(report(3, [{ id: "legacy-f", name: "Legacy", net: 1 }]));
  const table = ledgerOutcomeRows(data);
  assert.deepEqual(table?.rows.map((r) => `${r.kind}:${r.name}:${r.netValue}`), ["fleet:CTEM:7920", "agent:Writer:5940", "agent:Reader:1980"]);
  assert.deepEqual(table?.remainders.map((r) => `${r.key}:${r.row.netValue}`), ["no_agent:990"]);
  const hero = scopeHeroFigures(data);
  assert.equal(hero.value, 9000);
  assert.equal(hero.runs, 6);
  assert.equal(ledgerOutcomeRows(report(3)), null);
});

test("a chat-only ledger draws its value as a no_agent remainder equal to the hero (#12077 L6b4 UX r1 B1, invariant c)", () => {
  const data = withLedger(report(0), true);
  const figures = data.figures!;
  const chat = ledgerRow("no_agent", 0, 500, 5, 3);
  const table = ledgerOutcomeRows({ ...data, figures: { ...figures, by_agent: [chat], by_fleet: [{ ...chat, key: "no_fleet" }] } });
  assert.deepEqual(table?.rows, []);
  assert.deepEqual(table?.remainders.map((r) => r.key), ["no_agent"]);
  const hero = scopeHeroFigures(data);
  assert.equal(table?.remainders[0].row.hoursSaved, hero.hours_saved);
  assert.equal(table?.remainders[0].row.cost, hero.llm_cost);
});

test("a chat-only window is data on the ledger, not the empty state (#12077 L6b4)", () => {
  assert.equal(outcomesState(withLedger(report(0), true), "30d"), "data");
  assert.equal(outcomesState(report(0), "30d"), "window");
});

test("an empty window offers a wider one; an empty all-time report is the never-measured state", () => {
  assert.equal(outcomesState(report(0), "7d"), "window");
  assert.equal(outcomesState(report(0), "all"), "none");
  assert.equal(outcomesState(report(4), "7d"), "data");
  assert.equal(widerWindow("7d"), "30d");
  assert.equal(widerWindow("90d"), "all");
  assert.equal(widerWindow("all"), null);
});
