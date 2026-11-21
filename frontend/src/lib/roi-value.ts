/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * What the Value page draws from the ROI report (#9836 R5b, design
 * dev-docs/roi-outcomes.md 10.7): its windows and scopes as URL values, the
 * rows of its three tables (by fleet, by agent, by unit of work) with the
 * remainders that make them add up to the hero, the review shares and the
 * weekly trend. Pure, so `yarn test:unit` covers it.
 */

import type { RoiComputationStep, RoiGroupFigures, RoiLedgerFigures, RoiLedgerRow, RoiLedgerTotals, RoiReportResponse, RoiTrendPoint, RoiWindow } from "@/hooks/api/useRoiReportApi";

/** The windows of the Value page, in the switch's order. Translated by the select. @i18n-keys */
export const VALUE_WINDOW_LABELS = {
  "7d": "Last 7 days",
  "30d": "Last 30 days",
  "90d": "Last 90 days",
  quarter: "This quarter",
  all: "All time",
} as const satisfies Record<RoiWindow, string>;

export const VALUE_WINDOWS = Object.keys(VALUE_WINDOW_LABELS) as RoiWindow[];

/** The `window` URL value, `30d` when absent or unknown (shared with the other ROI views). */
export function parseValueWindow(value: string | null): RoiWindow {
  return value && value in VALUE_WINDOW_LABELS ? (value as RoiWindow) : "30d";
}

/** The next longer window, for "Widen the window"; `null` at "all". The quarter widens to all time. */
export function widerValueWindow(window: RoiWindow): RoiWindow | null {
  if (window === "quarter") return "all";
  const order: RoiWindow[] = ["7d", "30d", "90d", "all"];
  return order[order.indexOf(window) + 1] ?? null;
}

/** The two scopes of the page: the reader's own agents and fleets, or the whole organization. */
export type ValueScope = "mine" | "organization";

/** The scope switch's names, and the scope line beside it (round 2 B2). @i18n-keys */
export const VALUE_SCOPE_LABELS = {
  mine: "Mine",
  organization: "Whole organization",
} as const satisfies Record<ValueScope, string>;

/** The home's Top value drivers (#12009): the agents that saved time, most hours first, ties by name so the order is stable. */
export function valueDrivers(agents: readonly RoiGroupFigures[]): RoiGroupFigures[] {
  return agents
    .filter((agent) => agent.hours_saved > 0)
    .sort((a, b) => b.hours_saved - a.hours_saved || a.name.localeCompare(b.name));
}

/**
 * The line that names the selected scope. Mine is what the reader did (their own runs, chats and AI cost,
 * on any agent and in general chat; dev-docs/roi-methodology.md section 3). @i18n-keys
 */
export const VALUE_SCOPE_LINES = {
  mine: "What you did",
  organization: "Whole organization",
} as const satisfies Record<ValueScope, string>;

/**
 * Which scopes a reader gets and which one opens (design 10.7, scope): an
 * administrator both, organization first; a non-administrator both while the
 * organization totals are shown to everyone, his own first; else his own only.
 */
export function valueScopes(isAdmin: boolean, orgTotalsVisible: boolean): { scopes: ValueScope[]; fallback: ValueScope } {
  if (isAdmin) return { scopes: ["mine", "organization"], fallback: "organization" };
  if (orgTotalsVisible) return { scopes: ["mine", "organization"], fallback: "mine" };
  return { scopes: ["mine"], fallback: "mine" };
}

/** The `scope` URL value, kept only when the reader is offered it. */
export function parseValueScope(value: string | null, offered: { scopes: ValueScope[]; fallback: ValueScope }): ValueScope {
  return value && (offered.scopes as string[]).includes(value) ? (value as ValueScope) : offered.fallback;
}

/** The three tables of the page, each a tab with its own URL keys. */
export type ValueTab = "fleets" | "agents" | "units";

/** The tabs in their page order; the first is the default (`/tasks/value`). */
const VALUE_TAB_KEYS = ["fleets", "agents", "units"] as const satisfies readonly ValueTab[];

/** The `/tasks/value/:tab?` segment, kept only when it names a tab: an unknown or retired one answers the default (#12229). */
export function parseValueTab(value: string | undefined): ValueTab {
  return value && (VALUE_TAB_KEYS as readonly string[]).includes(value) ? (value as ValueTab) : VALUE_TAB_KEYS[0];
}

/** Whether a `/tasks/value/:tab?` segment names a table (none is the default); an unknown or retired one is not found (#797). */
export function isValueTab(value: string | undefined): boolean {
  return value === undefined || (VALUE_TAB_KEYS as readonly string[]).includes(value);
}

export interface ValueRow {
  kind: "fleet" | "agent" | "unit";
  id: string;
  name: string;
  runs: number;
  units: number;
  hoursSaved: number;
  value: number;
  /** `null` when costs are masked: drawn "Hidden", never 0. */
  cost: number | null;
  netValue: number | null;
  reviewRate: number | null;
}

/** The rows that are not an agent, a fleet or a unit: summed, plain text, always last (design 10.7). */
export interface ValueRemainder {
  key: "other" | "no_fleet" | "unallocated" | "no_agent" | "no_unit" | "other_units";
  /** English, translated by the page. */
  label: string;
  /** English, translated by the page. */
  hint: string;
  row: ValueRow;
}

type Figures = RoiReportResponse["organization"];

function toRow(kind: ValueRow["kind"], id: string, name: string, figures: Figures): ValueRow {
  return {
    kind,
    id,
    name,
    runs: figures.runs,
    units: figures.units,
    hoursSaved: figures.hours_saved,
    value: figures.value,
    cost: figures.llm_cost,
    netValue: figures.net_value,
    reviewRate: figures.review_rate,
  };
}

const TAB_KIND = { fleets: "fleet", agents: "agent", units: "unit" } as const satisfies Record<ValueTab, ValueRow["kind"]>;

/** The unit key of the spend no recorded run carries (`roi.COST_ONLY_UNIT`, #10441): drawn as a remainder, never a unit row. */
const COST_ONLY_UNIT_ID = "_cost_not_tied_to_a_unit";

/** One tab's rows, highest value first then by name. The cost no unit carries is a remainder, not a row. */
export function valueRows(report: RoiReportResponse | undefined, tab: ValueTab): ValueRow[] {
  if (!report) return [];
  const groups: RoiGroupFigures[] = tab === "fleets" ? report.fleets : tab === "agents" ? report.agents : (report.units ?? []).filter((unit) => unit.id !== COST_ONLY_UNIT_ID);
  return groups
    .map((group) => toRow(TAB_KIND[tab], group.id, group.name, group))
    .sort((a, b) => b.value - a.value || a.name.localeCompare(b.name));
}

/**
 * The remainders of a tab, so its rows add up to the hero (round 2 B1): the
 * folded agents or fleets a non-administrator cannot open, and in By fleet the
 * records of agents outside any fleet. By unit of work has one: the AI cost
 * no recorded unit carries (#10441, UX round 1 B1 on #10453).
 */
export function valueRemainders(report: RoiReportResponse | undefined, tab: ValueTab): ValueRemainder[] {
  if (!report) return [];
  if (tab === "units") {
    const costOnly = (report.units ?? []).find((unit) => unit.id === COST_ONLY_UNIT_ID);
    return costOnly
      ? [
          {
            key: "unallocated",
            label: "Cost not tied to a unit",
            hint: "AI cost of the agents' runs that produced no unit of work, summed.",
            row: toRow("unit", COST_ONLY_UNIT_ID, "", costOnly),
          },
        ]
      : [];
  }
  const out: ValueRemainder[] = [];
  if (tab === "fleets" && report.no_fleet && report.no_fleet.runs > 0) {
    out.push({
      key: "no_fleet",
      label: "Agents outside a fleet",
      hint: "Runs of agents that belong to no fleet, summed.",
      row: toRow("fleet", "no_fleet", "", report.no_fleet),
    });
  }
  const other = tab === "fleets" ? report.other_fleets : report.other_agents;
  if (other && other.runs > 0) {
    out.push({
      key: "other",
      label: "Other agents and fleets",
      hint: "Agents and fleets you cannot open, summed.",
      row: toRow(TAB_KIND[tab], "other", "", other),
    });
  }
  return out;
}

/**
 * The review line (round 1 B5): runs and work items counted apart, each with
 * how many a person reviewed, from the same figures as the hero.
 */
export function valueReviewShares(figures: Figures): {
  runs: { reviewed: number; total: number };
  workItems: { reviewed: number; total: number };
} {
  const workItems = figures.work_items ?? 0;
  const unreviewedItems = figures.unreviewed_work_items ?? 0;
  const runRecords = Math.max(0, figures.runs - workItems);
  const unreviewedRuns = Math.max(0, figures.unreviewed - unreviewedItems);
  return {
    runs: { reviewed: Math.max(0, runRecords - unreviewedRuns), total: runRecords },
    workItems: { reviewed: Math.max(0, workItems - unreviewedItems), total: workItems },
  };
}

export interface ValueTrendDatum {
  week: string;
  value: number;
  net: number | null;
  /** `[low, high]` of the value, the shaded band; `null` without a range. */
  band: [number, number] | null;
}

/** The weekly points the chart draws, oldest first; fewer than two = "Not enough history yet". */
export function valueTrend(points: RoiTrendPoint[] | undefined): ValueTrendDatum[] {
  return (points ?? []).map((point) => {
    const [low, high] = point.value_range;
    return {
      week: point.week,
      value: point.value,
      net: point.net_value,
      band: low === null || high === null ? null : [low, high],
    };
  });
}

/** The AI cost source the footnote names: the agents' LLM spend, as LLM Analytics reads it (#10441). */
const AGENT_SPEND_SOURCE = "llm_analytics_by_agent";

/**
 * The Value page's cost footnote (#10441): shown when the AI cost is the
 * LLM spend LLM Analytics reads and costs are not masked, with the part no unit
 * carries (`null` when there is none). `null` when there is nothing to say.
 * The part is the ledger's `no_unit` row of By unit of work (#12077 L6a UX round 1 B3),
 * so the note and the row show one figure; the report's own `unallocated_cost` only
 * when the answer carries no `figures` (an older backend).
 */
export function valueCostNote(report: RoiReportResponse | undefined): { unallocated: number | null } | null {
  if (!report || report.cost_masked || report.figures?.cost_masked || report.cost_source !== AGENT_SPEND_SOURCE) return null;
  const unallocated = report.figures
    ? (report.figures.by_unit.find((row) => row.key === "no_unit")?.ai_cost ?? 0)
    : (report.unallocated_cost ?? 0);
  return { unallocated: unallocated > 0 ? unallocated : null };
}

/**
 * The review line's counts for the scope in view (#12077 L6a UX round 2 B4): from
 * `figures.totals`, the same records as the hero, so a chat-only Mine never shows the
 * organization's runs. The ledger counts a work item's record as a run and does not split
 * them, so the work-item share is left out there. The report's own figures only when the
 * answer carries no `figures` (an older backend). `null` when the scope has no record: the
 * line is not drawn.
 */
export function valueReviewCounts(report: RoiReportResponse): ReturnType<typeof valueReviewShares> | null {
  const t = report.figures?.totals;
  const shares = t
    ? { runs: { reviewed: Math.min(t.reviewed, t.runs), total: t.runs }, workItems: { reviewed: 0, total: 0 } }
    : valueReviewShares(report.organization);
  return shares.runs.total > 0 || shares.workItems.total > 0 ? shares : null;
}

/**
 * Whether the Value page has nothing to draw (#12077 L6a UX round 1 B1): no run, no
 * deliverable, no hour and no value in `figures.totals`. A chat-only scope has no run
 * but does have value, and draws its hero. The report's own run count only when the
 * answer carries no `figures` (an older backend).
 */
export function valueIsEmpty(report: RoiReportResponse): boolean {
  const t = report.figures?.totals;
  if (!t) return report.organization.runs === 0;
  return t.runs === 0 && t.deliverables === 0 && t.hours === 0 && t.value === 0;
}

/**
 * The hero's figures from the ledger (#12077 slice L6a, design 12.8): `figures.totals` in the
 * shape `RoiFigure` draws, so the hero is the export's total line and `GET /roi` `figures`
 * (invariant d). The "How this is computed" steps are the ledger's By source rows (automations,
 * chat, other usage) and a total line from its totals, so the caption and the popover sum to this
 * hero, chat share included (#12077 L6b3 UX r1 B1); the report's own saving and total lines are
 * left out, its review and rejected lines kept. `null` when the answer carries no `figures` (an
 * older backend).
 */
export function ledgerHeroFigures(report: RoiReportResponse | undefined): Figures | null {
  const ledger = report?.figures;
  if (!report || !ledger) return null;
  return ledgerFigures(ledger, report.organization);
}

/** The part of a ledger answer a hero is drawn from: what `GET /roi` `figures` and a `GET /roi/summary` row both carry. */
type LedgerHero = Pick<RoiLedgerFigures, "currency" | "totals" | "by_source">;

/** Nothing beyond the ledger: the base of a hero drawn from a list row, which carries no report (#12077 L7a). */
function emptyFigures(currency: string): Figures {
  return { currency, runs: 0, units: 0, hours_saved: 0, llm_cost: null, value: 0, net_value: null, roi: null, reviewed: 0, unreviewed: 0, rejected: 0, review_rate: null, assumptions: [] };
}

/**
 * The hero of one ledger answer (#12077 L7a): the one mapping of `figures.totals` and By source
 * into the shape `RoiFigure` draws, used by the agent and fleet heroes, the Value page and the
 * list column, so agent X shows one figure on each (invariant d). *base* keeps the report's own
 * review and rejected lines; a list row passes none.
 */
export function ledgerFigures(ledger: LedgerHero, base?: Figures): Figures {
  const t = ledger.totals;
  const from = base ?? emptyFigures(ledger.currency);
  return {
    ...from,
    runs: t.runs,
    units: t.units,
    hours_saved: t.hours,
    value: t.value,
    llm_cost: t.ai_cost,
    net_value: t.net_value,
    roi: t.roi,
    payback_months: t.payback_months,
    reviewed: t.reviewed,
    unreviewed: Math.max(0, t.runs - t.reviewed),
    review_rate: t.runs > 0 ? t.reviewed / t.runs : null,
    range: {
      hours_saved: [t.hours_low, t.hours_high],
      value: [t.value_low, t.value_high],
      net_value: [t.net_value_low, t.net_value_high],
    },
    deliverables: t.deliverables,
    computation: ledgerComputation(from.computation ?? [], ledger),
  };
}

/** The ledger's computation lines: one per By source row that carries something, the total, then the report's review and rejected lines. */
function ledgerComputation(steps: RoiComputationStep[], ledger: LedgerHero): RoiComputationStep[] {
  const t = ledger.totals;
  const sources: RoiComputationStep[] = ledger.by_source
    .filter((row) => row.runs > 0 || row.deliverables > 0 || row.hours > 0 || row.value > 0)
    .map((row) => ({ kind: "source", source: row.key, runs: row.runs, deliverables: row.deliverables, hours: row.hours, value: row.value, currency: ledger.currency }));
  const total: RoiComputationStep = {
    kind: "total",
    minutes_saved: Math.round(t.hours * 60),
    hours_saved: t.hours,
    value: t.value,
    llm_cost: t.ai_cost,
    net_value: t.net_value,
    currency: ledger.currency,
  };
  return [...sources, total, ...steps.filter((step) => step.kind === "review" || step.kind === "rejected")];
}

/** The ledger row keys that are not an object (`services/roi_figures.py` ROW_*), drawn as remainders in this order. */
const LEDGER_REMAINDERS = {
  fleets: ["no_fleet", "other"],
  agents: ["no_agent", "other"],
  units: ["no_unit", "other_units"],
} as const satisfies Record<ValueTab, readonly ValueRemainder["key"][]>;

/** The words of each ledger remainder (design 12.4 and 12.5). @i18n-keys */
const LEDGER_REMAINDER_WORDS = {
  no_fleet: { label: "Agents outside a fleet", hint: "Runs of agents that belong to no fleet, summed." },
  no_agent: { label: "No agent (general chat)", hint: "Value and cost no agent carries, such as general chat, summed." },
  no_unit: { label: "No unit of work", hint: "Value and cost tied to no unit of work, summed." },
  other_units: { label: "Other units of work", hint: "Units of work used by too few people to be shown apart, summed." },
  other: { label: "Other agents and fleets", hint: "Agents and fleets you cannot open, or used by too few people to be shown apart, summed." },
  unallocated: { label: "Cost not tied to a unit", hint: "AI cost of the agents' runs that produced no unit of work, summed." },
} as const satisfies Record<ValueRemainder["key"], { label: string; hint: string }>;

const LEDGER_SECTION = { fleets: "by_fleet", agents: "by_agent", units: "by_unit" } as const satisfies Record<ValueTab, keyof RoiLedgerFigures>;

function ledgerRow(kind: ValueRow["kind"], row: RoiLedgerRow, name: string): ValueRow {
  return {
    kind,
    id: row.key,
    name,
    runs: row.runs,
    units: row.units,
    hoursSaved: row.hours,
    value: row.value,
    cost: row.ai_cost,
    netValue: row.net_value,
    reviewRate: row.runs > 0 ? row.reviewed / row.runs : null,
  };
}

/**
 * One tab of the Value page from the ledger (slice L6a): its object rows, named by
 * `figures.names` (an object gone keeps its id), highest value first, and its residual and
 * folded rows as remainders, so rows plus remainders sum to the hero (invariant c).
 */
export function ledgerTable(ledger: RoiLedgerFigures, tab: ValueTab): { rows: ValueRow[]; remainders: ValueRemainder[] } {
  const kind = TAB_KIND[tab];
  const residual: readonly string[] = LEDGER_REMAINDERS[tab];
  const section: RoiLedgerRow[] = ledger[LEDGER_SECTION[tab]];
  const names = ledger.names ?? {};
  const rows = section
    .filter((row) => !residual.includes(row.key))
    .map((row) => ledgerRow(kind, row, names[row.key] ?? row.key))
    .sort((a, b) => b.value - a.value || a.name.localeCompare(b.name));
  const remainders = LEDGER_REMAINDERS[tab].flatMap((key) => {
    const row = section.find((r) => r.key === key);
    return row ? [{ key, ...LEDGER_REMAINDER_WORDS[key], row: ledgerRow(kind, row, "") }] : [];
  });
  return { rows, remainders };
}

/** The weekly points from the ledger's By week (slice L6a); a merged point (k rule) keeps its first week. */
export function ledgerTrend(ledger: RoiLedgerFigures): ValueTrendDatum[] {
  return [...ledger.by_week]
    .sort((a, b) => a.key.localeCompare(b.key))
    .map((row: RoiLedgerTotals & { key: string }) => ({ week: row.key, value: row.value, net: row.net_value, band: [row.value_low, row.value_high] }));
}

/** The sums a tab adds up to (its rows plus its remainders): equal to the hero for the same report. */
export function valueTotals(rows: ValueRow[], remainders: ValueRemainder[]): { hours: number; value: number; cost: number | null; net: number | null } {
  const all = [...rows, ...remainders.map((r) => r.row)];
  const sum = (pick: (row: ValueRow) => number | null): number | null =>
    all.some((row) => pick(row) === null) ? null : all.reduce((total, row) => total + (pick(row) ?? 0), 0);
  return {
    hours: all.reduce((total, row) => total + row.hoursSaved, 0),
    value: all.reduce((total, row) => total + row.value, 0),
    cost: sum((row) => row.cost),
    net: sum((row) => row.netValue),
  };
}
