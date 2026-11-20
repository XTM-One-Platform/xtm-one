// SPDX-License-Identifier: Apache-2.0

import type { TraceAnalyticsPeriod } from "@/types";

/**
 * The window switch, the AI Adoption idiom. Translated by
 * `SegmentedFilter`. @i18n-keys
 */
export const PERIOD_LABELS = {
  "24h": "24H",
  "7d": "7D",
  "30d": "30D",
  "90d": "90D",
  "180d": "6M",
  "1y": "1Y",
  custom: "Custom",
} as const;

export const PERIODS = Object.keys(PERIOD_LABELS) as TraceAnalyticsPeriod[];

export function parsePeriod(value: string | null): TraceAnalyticsPeriod {
  return value && value in PERIOD_LABELS ? (value as TraceAnalyticsPeriod) : "30d";
}

/** What "the previous period" is, for each window's trend badge. @i18n-keys */
export const PREVIOUS_PERIOD_LABELS = {
  "24h": "vs previous 24 hours",
  "7d": "vs previous 7 days",
  "30d": "vs previous 30 days",
  "90d": "vs previous 90 days",
  "180d": "vs previous 6 months",
  "1y": "vs previous year",
  custom: "vs previous period",
} as const;

/**
 * The trace sources, as the volume chart and the Sources filter name them.
 * Kept whole, `flow` included: it validates the `?sources=` URL param, and a
 * stale link to flow traces keeps working while the builder is hidden - only
 * the picker drops it. @i18n-keys
 */
export const SOURCE_LABELS = {
  "agent-chat": "Agent Chat",
  "general-chat": "General Chat",
  assignment: "Assignment",
  memory: "Memory",
  evaluation: "Evaluation",
  flow: "Flows",
  proxy: "Proxy",
  // The analysis layer of Platform Health (#101 S9): its own source, never an agentic execution.
  platform_health: "Platform Health",
} as const;

export function sourceLabel(source: string): string {
  return (SOURCE_LABELS as Record<string, string>)[source] ?? source;
}

/** The cost breakdown switch, translated by `SegmentedFilter`. @i18n-keys */
export const BREAKDOWN_LABELS = {
  agents: "By agent",
  users: "By user",
  groups: "By group",
  models: "By model",
} as const;

export type Breakdown = keyof typeof BREAKDOWN_LABELS;

export function parseBreakdown(value: string | null): Breakdown {
  return value && value in BREAKDOWN_LABELS ? (value as Breakdown) : "agents";
}
