// SPDX-License-Identifier: Apache-2.0

/**
 * Coverage and stats of the CTEM command center (#9050 slice 4), computed
 * from reads the page already makes, never from a model and never from a
 * sample figure:
 *
 * - `GET /ctem-metrics/posture`: each stage's source products (primary and
 *   secondary), the products connected, whether the stage has collected data,
 *   its score;
 * - `GET /ctem-metrics/posture/history`: one recorded day per row (oldest
 *   first), the composite score and each stage's score.
 *
 * A value that cannot be read is `null`, shown as a dash with its reason,
 * never 0.
 */

/** The XTM Platform modules a CTEM stage can read, in column order. */
export const COVERAGE_MODULES = ["opencti", "openaev", "opencrq"] as const;
export type CoverageModule = (typeof COVERAGE_MODULES)[number];

/** The fields of a posture stage the coverage reads. */
interface CoverageStageInput {
  key: string;
  label: string;
  score: number | null;
  covered: boolean;
  primary_products: string[];
  secondary_products: string[];
}

/** The fields of `GET /ctem-metrics/posture` the coverage and stats read. */
export interface CoveragePostureInput {
  posture_score: number | null;
  stages: CoverageStageInput[];
  products: Record<string, { present: boolean } | undefined>;
}

/** One recorded day of `GET /ctem-metrics/posture/history`. */
export interface CoverageHistoryPoint {
  date: string;
  posture_score: number | null;
  stage_metrics: Record<string, Record<string, number | null> | undefined>;
}

/**
 * One cell of the stage x module matrix:
 * - `live`: the module feeds the stage and is connected;
 * - `missing`: the module is a primary source of the stage and is not connected
 *   (a blind spot, with a connect path);
 * - `optional`: a secondary source not connected;
 * - `none`: the module does not feed this stage.
 */
export type CoverageCell = "live" | "missing" | "optional" | "none";

interface CoverageRow {
  key: string;
  label: string;
  /** The stage has collected data (a score or at least one fact). */
  covered: boolean;
  score: number | null;
  cells: Record<CoverageModule, CoverageCell>;
  /** Primary sources not connected, in column order: the connect path. */
  missing: CoverageModule[];
}

export interface CoverageMatrix {
  rows: CoverageRow[];
  /** Stages with collected data, out of `rows.length`. */
  covered: number;
  /** Modules connected, in column order. */
  connected: CoverageModule[];
  /** Whether any stage reads the module at all (OpenCRQ feeds none today). */
  feedsAnyStage: Record<CoverageModule, boolean>;
}

const canon = (product: string): string => (product === "openbas" ? "openaev" : product);

/** The stage x module matrix, `null` until the posture is read. */
export function coverageMatrix(posture: CoveragePostureInput | null | undefined): CoverageMatrix | null {
  if (!posture) return null;
  const present = new Set(
    Object.entries(posture.products ?? {})
      .filter(([, p]) => p?.present)
      .map(([name]) => canon(name)),
  );
  const feedsAnyStage = { opencti: false, openaev: false, opencrq: false } as Record<CoverageModule, boolean>;
  const rows = posture.stages.map((stage): CoverageRow => {
    const primary = new Set(stage.primary_products.map(canon));
    const secondary = new Set(stage.secondary_products.map(canon));
    const cells = {} as Record<CoverageModule, CoverageCell>;
    const missing: CoverageModule[] = [];
    for (const module of COVERAGE_MODULES) {
      const feeds = primary.has(module) || secondary.has(module);
      if (feeds) feedsAnyStage[module] = true;
      if (!feeds) cells[module] = "none";
      else if (present.has(module)) cells[module] = "live";
      else if (primary.has(module)) {
        cells[module] = "missing";
        missing.push(module);
      } else cells[module] = "optional";
    }
    return { key: stage.key, label: stage.label, covered: stage.covered, score: stage.score, cells, missing };
  });
  return {
    rows,
    covered: rows.filter((r) => r.covered).length,
    connected: COVERAGE_MODULES.filter((m) => present.has(m)),
    feedsAnyStage,
  };
}

interface StageTrend {
  key: string;
  label: string;
  score: number | null;
  /** The score on the first recorded day of the period that has one. */
  startScore: number | null;
  /** `score - startScore`, `null` when either is unknown or fewer than two days carry a score. */
  change: number | null;
  /** The recorded scores, oldest first (`null` on a day without one). */
  series: (number | null)[];
}

export interface ExposureStats {
  postureScore: number | null;
  /** The composite score on the first recorded day of the period that has one. */
  postureStart: number | null;
  postureChange: number | null;
  postureSeries: (number | null)[];
  /** Days recorded in the period. */
  daysRecorded: number;
  /** The first recorded day, ISO date, `null` with no history. */
  since: string | null;
  stages: StageTrend[];
  /** Stages whose score went up / down over the period. */
  improved: number;
  declined: number;
}

const firstNumber = (values: (number | null)[]): number | null => {
  for (const v of values) if (typeof v === "number") return v;
  return null;
};

/**
 * The change over the period, `null` when it cannot be measured: no score now,
 * no recorded score, or a single recorded day (its score is today's, so there
 * is no period to compare and 0 would claim a change nobody measured).
 */
const diff = (now: number | null, series: readonly (number | null)[]): number | null => {
  const recorded = series.filter((v): v is number => typeof v === "number");
  if (now === null || recorded.length < 2) return null;
  return Math.round(now - recorded[0]);
};

/**
 * The period stats: the composite and per-stage score now against the first
 * recorded day of the history, and the series for the trend lines. `null`
 * until the posture is read.
 */
export function exposureStats(
  posture: CoveragePostureInput | null | undefined,
  history: readonly CoverageHistoryPoint[] | null | undefined,
): ExposureStats | null {
  if (!posture) return null;
  const points = history ?? [];
  const postureSeries = points.map((p) => (typeof p.posture_score === "number" ? p.posture_score : null));
  const postureStart = firstNumber(postureSeries);
  const stages = posture.stages.map((stage): StageTrend => {
    const series = points.map((p) => {
      const s = p.stage_metrics?.[stage.key]?.score;
      return typeof s === "number" ? s : null;
    });
    const startScore = firstNumber(series);
    return {
      key: stage.key,
      label: stage.label,
      score: stage.score,
      startScore,
      change: diff(stage.score, series),
      series,
    };
  });
  return {
    postureScore: posture.posture_score,
    postureStart,
    postureChange: diff(posture.posture_score, postureSeries),
    postureSeries,
    daysRecorded: points.length,
    since: points.length ? points[0].date : null,
    stages,
    improved: stages.filter((s) => (s.change ?? 0) > 0).length,
    declined: stages.filter((s) => (s.change ?? 0) < 0).length,
  };
}
