// SPDX-License-Identifier: Apache-2.0

/**
 * The pure rules of the stats and coverage card (C2 slice V5, redrawn by
 * #11547 slice S2d): the score band of a heat-map cell and its key, the
 * stages' weekly scores, a finding's 30-day series, and the localized stage
 * name by key (#9237 A4). Kept apart from the card so the card file exports
 * components only.
 */
import { CTEM_BAND_CONFIG, CTEM_STAGE_CONFIG } from "@/lib/constants";
import type { CoverageHistoryPoint } from "@/lib/ctem-coverage-stats";

/** The five score bands of the weekly heat map, worst first (the key under it). */
export type ScoreBand = "critical" | "at_risk" | "fair" | "good" | "strong";
export const SCORE_BANDS: readonly ScoreBand[] = ["critical", "at_risk", "fair", "good", "strong"];

/** The band of a score, as `ctem_scoring.py` BANDS cuts it (critical < 40,
 *  at_risk < 60, fair < 75, good < 90, strong >= 90). */
export function scoreBand(score: number): ScoreBand {
  if (score < 40) return "critical";
  if (score < 60) return "at_risk";
  if (score < 75) return "fair";
  if (score < 90) return "good";
  return "strong";
}

/** The fill and the label of a band (`CTEM_BAND_CONFIG`). */
export function scoreBandStyle(band: ScoreBand): { label: string; fill: string } {
  const conf = CTEM_BAND_CONFIG[band];
  return { label: conf.label, fill: conf.ring };
}

/** Weeks the heat map draws. */
const HEAT_WEEKS = 8;

interface WeeklyHeat {
  /** The first day of each week, ISO date, oldest first. */
  weeks: string[];
  /** One row per stage key asked: the week's mean score and its band, `null` on a week without a score. */
  rows: { key: string; cells: ({ score: number; band: ScoreBand } | null)[] }[];
}

const DAY_MS = 86_400_000;
const isoDay = (ms: number) => new Date(ms).toISOString().slice(0, 10);

/**
 * The stages' scores over the last `weeks` weeks of the history, ending on its
 * last recorded day: each cell is the mean of the week's recorded scores of
 * the stage, `null` when none. `null` without any recorded day.
 */
export function weeklyHeat(
  history: readonly CoverageHistoryPoint[] | null | undefined,
  stageKeys: readonly string[],
  weeks: number = HEAT_WEEKS,
): WeeklyHeat | null {
  const points = (history ?? []).filter((p) => !Number.isNaN(Date.parse(p.date)));
  if (points.length === 0) return null;
  const end = Math.max(...points.map((p) => Date.parse(p.date)));
  const first = end - (weeks * 7 - 1) * DAY_MS;
  const starts = Array.from({ length: weeks }, (_, i) => first + i * 7 * DAY_MS);
  const rows = stageKeys.map((key) => {
    const sums = Array.from({ length: weeks }, () => ({ total: 0, n: 0 }));
    for (const p of points) {
      const at = Date.parse(p.date);
      const score = p.stage_metrics?.[key]?.score;
      if (at < first || typeof score !== "number") continue;
      const week = Math.min(weeks - 1, Math.floor((at - first) / (7 * DAY_MS)));
      sums[week].total += score;
      sums[week].n += 1;
    }
    const cells = sums.map((s) => {
      if (s.n === 0) return null;
      const score = Math.round(s.total / s.n);
      return { score, band: scoreBand(score) };
    });
    return { key, cells };
  });
  return { weeks: starts.map(isoDay), rows };
}

/** Headline figures the daily snapshot never stores, with the stored metrics
 *  they are the rounded mean of (the server's headline does the same). */
const DERIVED_METRICS: Record<string, readonly string[] | undefined> = {
  validation_success_pct: ["prevention_success_pct", "detection_success_pct"],
};

/** The last `days` recorded values of the metric `key` of `stage` in the
 *  history, oldest first; `null` on a day without it. A derived figure is
 *  rebuilt from the metrics it is the mean of. */
export function metricSeries(
  history: readonly CoverageHistoryPoint[] | null | undefined,
  stage: string,
  key: string,
  days = 30,
): (number | null)[] {
  const parts = DERIVED_METRICS[key];
  return (history ?? []).slice(-days).map((p) => {
    const metrics = p.stage_metrics?.[stage];
    const v = metrics?.[key];
    if (typeof v === "number") return v;
    if (!parts) return null;
    const known = parts.map((k) => metrics?.[k]).filter((x): x is number => typeof x === "number");
    return known.length ? Math.round(known.reduce((a, b) => a + b, 0) / known.length) : null;
  });
}

/** The stage configs keyed by any string, so a server key is looked up without a cast. */
const STAGE_LABELS: Record<string, { label: string } | undefined> = CTEM_STAGE_CONFIG;

/** The localized stage name by key (#9237 A4), the server label only for an unknown key. */
export function stageName(key: string, fallback: string, t: (k: string) => string): string {
  const conf = STAGE_LABELS[key];
  return conf ? t(conf.label) : fallback;
}
