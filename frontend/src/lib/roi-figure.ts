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
 * The pure half of the one ROI figure component (#9836 R4b, design
 * dev-docs/roi-outcomes.md 10.5 and 10.6): which figure a metric reads, its
 * low / high range, the one vocabulary of the input labels, and the sums the
 * "How this is computed" popover shows. No React, so `yarn test:unit` covers
 * the arithmetic the popover draws.
 */

import type { RoiAssumptionFact, RoiComputationStep, RoiRange, RoiReportResponse } from "@/hooks/api/useRoiReportApi";

export type RoiFigures = RoiReportResponse["organization"];

/** The metrics a figure draws (design 10.5). */
export type RoiMetric = "hours" | "value" | "cost" | "net" | "roi" | "payback" | "review_rate";

/** The names of the figures, one per metric on every surface (design 10.8). @i18n-keys */
export const ROI_METRIC_NAMES = {
  hours: "Hours saved",
  value: "Value",
  cost: "AI cost",
  net: "Net value",
  roi: "ROI",
  payback: "Payback",
  review_rate: "Review rate",
} as const satisfies Record<RoiMetric, string>;

/** The metrics whose figure is a model cost, or derived from one: masked together (design 10.2). */
const COST_METRICS: ReadonlySet<RoiMetric> = new Set(["cost", "net", "roi", "payback"]);

/** True when the report hides this metric from the reader: never drawn as 0. */
export function isMaskedMetric(metric: RoiMetric, costMasked: boolean | undefined): boolean {
  return Boolean(costMasked) && COST_METRICS.has(metric);
}

/** The likely value of a metric, `null` when the report has none (masked, no run, no positive net). */
export function metricValue(figures: RoiFigures, metric: RoiMetric): number | null {
  switch (metric) {
    case "hours":
      return figures.hours_saved;
    case "value":
      return figures.value;
    case "cost":
      return figures.llm_cost;
    case "net":
      return figures.net_value;
    case "roi":
      return figures.roi;
    case "payback":
      return figures.payback_months ?? null;
    case "review_rate":
      return figures.review_rate;
  }
}

const RANGE_KEY = {
  hours: "hours_saved",
  value: "value",
  net: "net_value",
  roi: "roi",
  payback: "payback_months",
} as const;

/** The `[low, high]` range of a metric, or `null` when it has none (cost, review rate, an older report). */
export function metricRange(figures: RoiFigures, metric: RoiMetric): RoiRange | null {
  if (!(metric in RANGE_KEY)) return null;
  const range = figures.range?.[RANGE_KEY[metric as keyof typeof RANGE_KEY]];
  if (!range || range[0] === null || range[1] === null) return null;
  return range;
}

/** The kind of a label once mapped to the one vocabulary shown to a person (design 10.6). */
export type RoiShownLabel = "default" | "customized" | "assumption" | "published" | "measured" | "mixed";

/**
 * Every label the API puts on a minute or a rate (`services/roi.py` LABEL_*,
 * `roi_records.py`, `roi_report.py`), mapped to what a person reads: "Override"
 * is never shown (design 10.6, round 1 B3). A test checks every API label has
 * an entry.
 */
export const API_LABEL_SHOWN = {
  assumption: "assumption",
  "published estimate": "published",
  measured: "measured",
  "organization override": "customized",
  "organization unit": "customized",
  "organization rates by role": "customized",
  "catalog default": "default",
  "low: published estimate; likely and high: assumption": "mixed",
} as const satisfies Record<string, RoiShownLabel>;

/** The words of each shown label. @i18n-keys */
export const SHOWN_LABEL_NAMES = {
  default: "Default",
  customized: "Customized",
  assumption: "Assumption",
  published: "Published estimate",
  measured: "Measured",
  mixed: "Published estimate and assumption",
} as const satisfies Record<RoiShownLabel, string>;

/** The shown label of an API label; a label made of several (";"-joined) takes its first known part. */
export function shownLabel(apiLabel: string): RoiShownLabel {
  if (apiLabel in API_LABEL_SHOWN) return API_LABEL_SHOWN[apiLabel as keyof typeof API_LABEL_SHOWN];
  for (const part of apiLabel.split(";")) {
    const key = part.trim();
    if (key in API_LABEL_SHOWN) return API_LABEL_SHOWN[key as keyof typeof API_LABEL_SHOWN];
  }
  return "assumption";
}

/** A payback under 30 days reads "Under a month", never "< 1 month" (design 10.5). */
export function isUnderAMonth(months: number): boolean {
  return months < 1;
}

/** One saving line of the popover: the API's TOTAL minutes of a unit, divided back to one unit (UX r1 B1). */
type RoiSavingLine = Extract<RoiComputationStep, { kind: "saving" }> & {
  /** Manual minutes of ONE unit: `manual_min / units`. */
  manual_per_unit: number;
  /** Review minutes of ONE unit: `review_min / units`. */
  review_per_unit: number;
};

/**
 * The saving step as the popover writes it. The API's `manual_min` and
 * `review_min` are the totals of the unit's `units` (services/roi.py
 * `_computation_facts`), so "units x (manual - review) min" is written with
 * the minutes of one unit: units x (per-unit manual - per-unit review) x rate
 * / 60 is the step's value.
 */
function savingLine(step: Extract<RoiComputationStep, { kind: "saving" }>): RoiSavingLine {
  const per = (total: number) => (step.units > 0 ? total / step.units : 0);
  return { ...step, manual_per_unit: per(step.manual_min), review_per_unit: per(step.review_min) };
}

/** The rate roles the catalog ships, by their names (UX r1 A2): a role an organization adds reads as typed. @i18n-keys */
export const RATE_ROLE_NAMES = {
  analyst: "Analyst",
  ciso: "CISO",
} as const satisfies Record<string, string>;

/** The ledger's By source rows by their names (design 12.9, `services/roi_figures.py` CLASS_* and ROW_OTHER_SOURCES). @i18n-keys */
export const SOURCE_NAMES = {
  automation: "Automations",
  chat: "Chat (estimated)",
  platform: "Other AI usage",
  other_sources: "Other sources",
} as const satisfies Record<string, string>;

/** One By source line of the ledger (#12077 L6b3 UX r1 B1): what sums to the hero, chat included. */
type RoiSourceLine = Extract<RoiComputationStep, { kind: "source" }>;

/** The sums the popover writes, from the report's own numbers, so the figure is reproduced by hand. */
export interface RoiComputation {
  savings: RoiSavingLine[];
  /** The ledger's By source lines (automations, chat, other usage), summing to `value`; empty on the report's own figures. */
  sources: RoiSourceLine[];
  /** Chat deliverables the hero counts beside the runs. */
  deliverables: number;
  /** The sum of the savings' values: the gross value. */
  value: number;
  /** `null` when the cost is masked. */
  cost: number | null;
  /** value - cost; `null` when the cost is masked. */
  net: number | null;
  /** net / cost; `null` without a cost or when masked. */
  roi: number | null;
  units: number;
  /** Records of a run, work items left out: the same split as the card (UX #9961 r2 B1). */
  runs: number;
  /** Records of a work item, which are not runs. */
  workItems: number;
  rejected: number;
  unreviewed: number;
  unitFacts: Extract<RoiAssumptionFact, { kind: "unit" }>[];
  rateFacts: Extract<RoiAssumptionFact, { kind: "rate" }>[];
}

/**
 * The records of a report split into runs and work items: a work item's record is not a run (#9810).
 * The card caption and the "How this is computed" popover both read it, so their counts never differ (UX #9961 r2 B1).
 */
export function roiRecordCounts(figures: Pick<RoiFigures, "runs" | "work_items">): { runs: number; workItems: number } {
  const workItems = figures.work_items ?? 0;
  return { runs: figures.runs - workItems, workItems };
}

/** What "How this is computed" shows for one report scope (design 10.6). */
export function roiComputation(figures: RoiFigures, costMasked: boolean | undefined): RoiComputation {
  const savings = (figures.computation ?? [])
    .filter((s): s is Extract<RoiComputationStep, { kind: "saving" }> => s.kind === "saving")
    .map(savingLine);
  const facts = figures.assumption_facts ?? [];
  const sources = (figures.computation ?? []).filter((s): s is RoiSourceLine => s.kind === "source");
  // The ledger's hero sums its sources; the report's own figures sum their savings.
  const value = sources.length === 0 && savings.length > 0 ? savings.reduce((sum, s) => sum + s.value, 0) : figures.value;
  const cost = costMasked ? null : figures.llm_cost;
  const net = cost === null ? null : value - cost;
  return {
    savings: sources.length > 0 ? [] : savings,
    sources,
    deliverables: figures.deliverables ?? 0,
    value,
    cost,
    net,
    roi: net !== null && cost !== null && cost > 0 ? net / cost : null,
    units: figures.units,
    ...roiRecordCounts(figures),
    rejected: figures.rejected,
    unreviewed: figures.unreviewed,
    unitFacts: facts.filter((f): f is Extract<RoiAssumptionFact, { kind: "unit" }> => f.kind === "unit"),
    rateFacts: facts.filter((f): f is Extract<RoiAssumptionFact, { kind: "rate" }> => f.kind === "rate"),
  };
}
