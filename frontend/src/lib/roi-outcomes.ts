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
 * What the Analytics Outcomes tab draws from the ROI report (#9706, design
 * dev-docs/roi-outcomes.md 6.3): the table rows, fleets first then agents,
 * and the empty state that applies. Pure, so `yarn test:unit` covers it.
 */

import type { RoiReportResponse, RoiWindow } from "@/hooks/api/useRoiReportApi";
import { roiRecordsPath } from "./roi-records.ts";
import { ledgerHeroFigures, ledgerTable, valueIsEmpty, type ValueRow } from "./roi-value.ts";

export interface OutcomeRow {
  kind: "fleet" | "agent";
  id: string;
  name: string;
  runs: number;
  units: number;
  hoursSaved: number;
  /** `null` when the report masks costs (#9836 R1): drawn "Hidden", never 0. */
  cost: number | null;
  netValue: number | null;
  /** Share of the runs a person reviewed, 0 to 1; `null` without a run. */
  reviewRate: number | null;
}

/** A summed row under an outcomes table (no agent, other agents): English label and hint, translated by the table. */
export interface OutcomeRemainder {
  key: string;
  label: string;
  hint: string;
  row: OutcomeRow;
}

/** The kind cell of an outcomes table and the Type filter's options. @i18n-keys */
export const KIND_LABELS = { fleet: "Fleet", agent: "Agent" } as const satisfies Record<OutcomeRow["kind"], string>;

/** Where an agent's ROI lives: the Value tab of its page, ROI first (#11037). */
function agentValuePath(agentId: string): string {
  return `/agents/${agentId}/value`;
}

/** Where a row's agent or fleet lives: its page (UX #9790 B2, a name links to its object), an agent on its Value tab (#11037). */
export function outcomeRowPath(row: Pick<OutcomeRow, "kind" | "id">): string {
  return row.kind === "fleet" ? `/fleets/${row.id}` : agentValuePath(row.id);
}

/** An agent's ROI records nobody reviewed yet in *window*, runs and work items alike (#10570). */
export function agentReviewPath(agentId: string, window: RoiWindow = "30d"): string {
  return roiRecordsPath({ scope: "agent", id: agentId, window, review: "unreviewed" });
}

/** A fleet's ROI records nobody reviewed yet in *window*, runs and work items alike (#10570). */
export function fleetReviewPath(fleetId: string, window: RoiWindow = "30d"): string {
  return roiRecordsPath({ scope: "fleet", id: fleetId, window, review: "unreviewed" });
}

/** The query that opens an agent's Outcomes section on the unit picker, focused (#9810). */
export const PICK_UNIT_PARAM = { key: "pick", value: "unit" } as const;

/** "Pick a unit" from the agent Overview: Configuration > Outcomes, on the unit picker (UX #9810 B2). */
export function agentPickUnitPath(agentId: string): string {
  return `/agents/${agentId}/config/outcomes?${PICK_UNIT_PARAM.key}=${PICK_UNIT_PARAM.value}`;
}

/** Fleets then agents, each by net value, highest first, then by name. */
export function outcomeRows(report: RoiReportResponse | undefined): OutcomeRow[] {
  if (!report) return [];
  const rows = (kind: OutcomeRow["kind"], groups: RoiReportResponse["fleets"]): OutcomeRow[] =>
    groups
      .map((g) => ({
        kind,
        id: g.id,
        name: g.name,
        runs: g.runs,
        units: g.units,
        hoursSaved: g.hours_saved,
        cost: g.llm_cost,
        netValue: g.net_value,
        reviewRate: g.review_rate,
      }))
      .sort((a, b) => (b.netValue ?? b.hoursSaved) - (a.netValue ?? a.hoursSaved) || a.name.localeCompare(b.name));
  return [...rows("fleet", report.fleets), ...rows("agent", report.agents)];
}

/**
 * The Outcomes tab's rows from the ledger (#12077 slice L6b4, design 12.8 "Analytics > Outcomes"):
 * the fleet then agent object rows of `figures.by_fleet` and `figures.by_agent`, so a row reads the
 * figure the Value page, the agent page and the export draw (invariant d), and the residual and
 * folded rows of By agent (`no_agent`, `other`) as remainders, so value carried by chat or by a
 * folded agent is in a row as well as in the hero (L6b4 UX r1 B1, invariant c). `null` when the
 * answer carries no `figures` (an older backend): the report's own rows are drawn then.
 */
export function ledgerOutcomeRows(report: RoiReportResponse | undefined): { rows: OutcomeRow[]; remainders: OutcomeRemainder[] } | null {
  if (!report?.figures) return null;
  const toOutcome = (kind: OutcomeRow["kind"]) => (row: ValueRow): OutcomeRow => ({
    kind,
    id: row.id,
    name: row.name,
    runs: row.runs,
    units: row.units,
    hoursSaved: row.hoursSaved,
    cost: row.cost,
    netValue: row.netValue,
    reviewRate: row.reviewRate,
  });
  const agents = ledgerTable(report.figures, "agents");
  return {
    rows: [...ledgerTable(report.figures, "fleets").rows.map(toOutcome("fleet")), ...agents.rows.map(toOutcome("agent"))],
    remainders: agents.remainders.map((r) => ({ key: r.key, label: r.label, hint: r.hint, row: toOutcome("agent")(r.row) })),
  };
}

/**
 * Which state the tab shows: `none` when nothing was ever counted (no unit
 * declared, or no counted run yet), `window` when the period holds no run but a
 * longer one may, `data` otherwise.
 */
export function outcomesState(report: RoiReportResponse | undefined, window: RoiWindow): "none" | "window" | "data" {
  if (!report || !valueIsEmpty(report)) return "data";
  return window === "all" ? "none" : "window";
}

/**
 * Which state the fleet ROI view shows (#9785): its window offers no "all",
 * so the lifetime report (`window=all`, read only when the window counted
 * nothing) tells "never measured" (`none`) from "nothing in this period"
 * (`window`). `null` while the lifetime read is pending. Emptiness is the ledger's
 * (`valueIsEmpty`): a chat-only agent or fleet has no run but does have value, and draws its
 * hero (#12077 L6b3 UX r1 B2, invariant d).
 */
export function fleetRoiState(
  report: RoiReportResponse | undefined,
  lifetime: RoiReportResponse | undefined,
): "none" | "window" | "data" | null {
  if (!report || !valueIsEmpty(report)) return "data";
  if (!lifetime) return null;
  return valueIsEmpty(lifetime) ? "none" : "window";
}

/**
 * The k rule on one agent or fleet (#12077 slice L6b3, design 12.5 and the under-k table of
 * 12.10): `null` when the scope shows its totals; otherwise k and whether the viewer used it in
 * the window. The ledger then answers the viewer's own share only, so `figures.totals` is the
 * viewer's own and the report's other keys (rows, counts, steps) must not be drawn.
 */
export function scopeUnderK(report: RoiReportResponse | undefined): { k: number; viewerUsed: boolean } | null {
  return ledgerUnderK(report?.figures);
}

/**
 * The k rule read from the ledger's figures alone (#12077 slice L7f): the one reading of
 * `suppressed.own_share_only` the agent page, the fleet view and the list column share, so the
 * list cell and the hero never disagree on whether totals are hidden.
 */
export function ledgerUnderK(
  ledger: Pick<NonNullable<RoiReportResponse["figures"]>, "totals" | "suppressed"> | undefined,
): { k: number; viewerUsed: boolean } | null {
  if (!ledger?.suppressed?.own_share_only) return null;
  const t = ledger.totals;
  return { k: ledger.suppressed.k, viewerUsed: t.runs > 0 || t.deliverables > 0 || t.hours > 0 || t.value > 0 };
}

/**
 * The figures an agent or fleet hero draws (#12077 slice L6b3): the ledger's totals, so the agent
 * page, the fleet view, the Value page row and the export show one figure (invariant d). Under k
 * the hero is the viewer's own share: nothing of the report's own scope-wide keys is kept (no
 * review or rejected step, no assumption, no rejected or work-item count); the ledger's own source
 * and total steps stay, since they are the viewer's own share too, so the popover sums to the hero
 * instead of falling back to a unit formula (#12077 L6b3 UX r2 B5). The report's own figures when
 * the answer carries no `figures` (an older backend).
 */
export function scopeHeroFigures(report: RoiReportResponse): RoiReportResponse["organization"] {
  const hero = ledgerHeroFigures(report);
  if (!hero) return report.organization;
  if (!scopeUnderK(report)) return hero;
  const computation = (hero.computation ?? []).filter((step) => step.kind === "source" || step.kind === "total");
  return { ...hero, rejected: 0, work_items: 0, unreviewed_work_items: 0, computation, assumption_facts: [] };
}

/**
 * The per-agent rows of a fleet or agent report from the ledger's By agent (#12077 L6b3 UX r2 B6):
 * the object rows named by `figures.names`, and the residual and folded rows as remainders, so the
 * table sums to the hero the KPI cards draw. `null` when the answer carries no `figures` (an older
 * backend): the report's own agent rows are drawn then.
 */
export function ledgerAgentRows(report: RoiReportResponse | undefined): { rows: OutcomeRow[]; remainders: OutcomeRemainder[] } | null {
  if (!report?.figures) return null;
  const { rows, remainders } = ledgerTable(report.figures, "agents");
  const toOutcome = (row: ValueRow): OutcomeRow => ({
    kind: "agent",
    id: row.id,
    name: row.name,
    runs: row.runs,
    units: row.units,
    hoursSaved: row.hoursSaved,
    cost: row.cost,
    netValue: row.netValue,
    reviewRate: row.reviewRate,
  });
  return {
    rows: rows.map(toOutcome),
    remainders: remainders.map((r) => ({ key: r.key, label: r.label, hint: r.hint, row: toOutcome(r.row) })),
  };
}

/** The next longer window, for the "widen the window" action; `null` at "all". */
export function widerWindow(window: RoiWindow): RoiWindow | null {
  const order: RoiWindow[] = ["7d", "30d", "90d", "all"];
  const next = order[order.indexOf(window) + 1];
  return next ?? null;
}
