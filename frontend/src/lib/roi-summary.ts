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
 * The "Net value (30d)" list column (#9836 R6b, design dev-docs/roi-outcomes.md
 * 10.8): what one row of `GET /roi/summary` becomes in the shared `RoiFigure`,
 * how the column sorts and which name it carries. Since #12077 slice L7a a row
 * is the ledger's `figures` of the object, drawn through `ledgerFigures`, the
 * mapping of the agent page hero, so the two never differ (invariant d). Pure,
 * so it is tested without a page.
 */
import type { RoiSummaryItem } from "@/hooks/api/useRoiReportApi";
import type { RoiFigures, RoiMetric } from "@/lib/roi-figure";
import { ledgerUnderK } from "@/lib/roi-outcomes";
import { ledgerFigures } from "@/lib/roi-value";

/** The column's header: the net value, or the gross value when costs are masked (design 10.8). @i18n-keys */
export const ROI_COLUMN_HEADERS = {
  net: "Net value (30d)",
  value: "Value (30d)",
} as const;

/** The metric the column draws: the net value, the gross value when the reader's costs are masked. */
export function roiColumnMetric(costMasked: boolean): Extract<RoiMetric, "net" | "value"> {
  return costMasked ? "value" : "net";
}

/**
 * One summary row as the figures `RoiFigure` reads: the ledger's hero of that agent or fleet,
 * mapped by `ledgerFigures` as the agent page and fleet view map theirs, so the cell, its
 * "How this is computed" (the By source lines and the total) and the hero say one figure. Under
 * k it is the reader's own share, as on the agent page. `null` when nothing counts.
 */
export function summaryFigures(item: RoiSummaryItem | undefined): RoiFigures | null {
  if (!item || !item.declared || !item.figures) return null;
  const records = item.records;
  if (!records) return ledgerFigures(item.figures);
  // The records' counts and inputs the agent page reads from its report (UX r1 B1): the
  // rejected line, the work items split from the runs, and the Inputs block.
  const base: RoiFigures = {
    currency: item.figures.currency,
    runs: 0,
    units: 0,
    hours_saved: 0,
    llm_cost: null,
    value: 0,
    net_value: null,
    roi: null,
    reviewed: 0,
    unreviewed: 0,
    rejected: records.rejected,
    review_rate: null,
    assumptions: [],
    work_items: records.work_items,
    unreviewed_work_items: records.unreviewed_work_items,
    assumption_facts: records.assumption_facts,
    computation: [
      ...(item.figures.totals.runs > 0 ? [{ kind: "review" as const, reviewed: item.figures.totals.reviewed, total: item.figures.totals.runs }] : []),
      ...(records.rejected > 0 ? [{ kind: "rejected" as const, count: records.rejected }] : []),
    ],
  };
  return ledgerFigures(item.figures, base);
}

/**
 * The k rule on one row (#12077 slice L7f, design 12.10 row "List column cell"): `null` when the
 * row shows its totals; otherwise k and whether the reader used the agent or fleet. The hero's own
 * reading (`ledgerUnderK`), so the cell, the agent page and the fleet view say one state.
 */
export function summaryUnderK(item: RoiSummaryItem | undefined): { k: number; viewerUsed: boolean } | null {
  if (!item || !item.declared) return null;
  return ledgerUnderK(item.figures);
}

/**
 * The sort value of a row: its net value (gross when masked); a row with no
 * figure, or whose totals the k rule hides, sorts as empty, after every figure
 * in both directions (design 12.10: never mixed with totals).
 */
export function summarySortValue(item: RoiSummaryItem | undefined, costMasked: boolean): number | null {
  if (!item || !item.declared || !item.figures || summaryUnderK(item)) return null;
  const figure = costMasked ? item.figures.totals.value : item.figures.totals.net_value;
  return typeof figure === "number" ? figure : null;
}
