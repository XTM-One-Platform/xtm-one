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
 * Statistics of the evaluation comparison page (#12282), pure.
 *
 * The API serves, per run and entry, each pooled metric's `[numerator, denominator]`
 * (`GET /evaluations/compare`). A run's figure on a set of entries adds those counts up
 * and divides once (micro), exactly as the run's own headline does; this module
 * re-pools them for whatever entries the page shows, and measures how sure a figure is:
 *
 * - a 95% percentile bootstrap interval, resampling the entries (fixed seed, so a page
 *   shows the same interval on every load);
 * - for one model in two evaluations, a paired bootstrap of the change: the same
 *   resampled entries on both sides;
 * - ranks as best–worst (as LMArena shows them): a run is surely above another only when
 *   their intervals do not overlap.
 */

import type { ComparedCell, ComparedEntry, EvaluationComparison } from "@/types";

export type Counts = [number, number];

/** Resamples per interval. */
const BOOTSTRAP_SAMPLES = 2000;

/** mulberry32: a small seeded generator, so intervals are stable across loads. */
function seededRandom(seed: number): () => number {
  let a = seed | 0;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** The pooled value (0-100) of some entries' counts; `null` with no entry. A 0-of-0 total
 *  is a correct empty answer, as the scorer reads it. */
export function pooled(counts: readonly Counts[]): number | null {
  if (!counts.length) return null;
  let num = 0;
  let den = 0;
  for (const [n, d] of counts) {
    num += n;
    den += d;
  }
  return den > 0 ? (100 * num) / den : 100;
}

function percentile(sorted: number[], q: number): number {
  const i = Math.min(sorted.length - 1, Math.max(0, Math.round(q * (sorted.length - 1))));
  return sorted[i];
}

export interface Interval {
  value: number;
  low: number;
  high: number;
}

/** The pooled value and its 95% bootstrap interval over the entries. */
export function bootstrap(counts: readonly Counts[], seed: number, samples = BOOTSTRAP_SAMPLES): Interval | null {
  const value = pooled(counts);
  if (value === null) return null;
  const rnd = seededRandom(seed);
  const draws: number[] = [];
  for (let b = 0; b < samples; b++) {
    const pick: Counts[] = [];
    for (let i = 0; i < counts.length; i++) pick.push(counts[Math.floor(rnd() * counts.length)]);
    draws.push(pooled(pick) as number);
  }
  draws.sort((x, y) => x - y);
  return { value, low: percentile(draws, 0.025), high: percentile(draws, 0.975) };
}

export interface PairedChange {
  /** The first side's pooled value, then the second's, on the entries both have. */
  before: number;
  after: number;
  /** after − before, and its 95% interval (paired bootstrap over the entries). */
  change: number;
  low: number;
  high: number;
  /** The interval leaves 0 out: the change holds across the entries. */
  holds: boolean;
}

/** The change from `before` to `after`, both given per entry, in the same order. */
export function pairedChange(
  before: readonly Counts[],
  after: readonly Counts[],
  seed: number,
  samples = BOOTSTRAP_SAMPLES,
): PairedChange | null {
  if (!before.length || before.length !== after.length) return null;
  const b0 = pooled(before) as number;
  const a0 = pooled(after) as number;
  const rnd = seededRandom(seed);
  const draws: number[] = [];
  for (let s = 0; s < samples; s++) {
    const pb: Counts[] = [];
    const pa: Counts[] = [];
    for (let i = 0; i < before.length; i++) {
      const k = Math.floor(rnd() * before.length);
      pb.push(before[k]);
      pa.push(after[k]);
    }
    draws.push((pooled(pa) as number) - (pooled(pb) as number));
  }
  draws.sort((x, y) => x - y);
  const low = percentile(draws, 0.025);
  const high = percentile(draws, 0.975);
  return { before: b0, after: a0, change: a0 - b0, low, high, holds: low > 0 || high < 0 };
}

/** Best and worst rank each interval allows: 1 + those surely above, N − those surely below. */
export function rankSpread<T extends Interval>(rows: readonly T[]): Array<T & { best: number; worst: number }> {
  return rows.map((row) => ({
    ...row,
    best: 1 + rows.filter((other) => other.low > row.high).length,
    worst: rows.length - rows.filter((other) => other.high < row.low).length,
  }));
}

// ── Reading the comparison payload ──

/** cells by run, then by entry key. */
export function cellIndex(comparison: EvaluationComparison): Map<string, Map<string, ComparedCell>> {
  const out = new Map<string, Map<string, ComparedCell>>();
  for (const cell of comparison.cells) {
    if (!out.has(cell.run_id)) out.set(cell.run_id, new Map());
    out.get(cell.run_id)!.set(cell.entry_key, cell);
  }
  return out;
}

/** The entries every listed evaluation has. */
export function sharedEntries(entries: readonly ComparedEntry[], evaluationIds: readonly string[]): ComparedEntry[] {
  return entries.filter((entry) => evaluationIds.every((id) => entry.members.some((m) => m.evaluation_id === id)));
}

/** The entries of one evaluation. */
export function entriesOf(entries: readonly ComparedEntry[], evaluationId: string): ComparedEntry[] {
  return entries.filter((entry) => entry.members.some((m) => m.evaluation_id === evaluationId));
}

/** One run's counts of `metric` on `keys` (entries it did not count, or that lack the
 *  metric, are left out). */
export function runCounts(
  cells: Map<string, Map<string, ComparedCell>>,
  runId: string,
  metric: string,
  keys: readonly string[],
): Counts[] {
  const byEntry = cells.get(runId);
  if (!byEntry) return [];
  const out: Counts[] = [];
  for (const key of keys) {
    const counts = byEntry.get(key)?.metrics[metric];
    if (counts) out.push(counts);
  }
  return out;
}

/** Two runs' counts on the entries both counted, in the same order. */
export function pairedCounts(
  cells: Map<string, Map<string, ComparedCell>>,
  runA: string,
  runB: string,
  metric: string,
  keys: readonly string[],
): { keys: string[]; before: Counts[]; after: Counts[] } {
  const a = cells.get(runA);
  const b = cells.get(runB);
  const out = { keys: [] as string[], before: [] as Counts[], after: [] as Counts[] };
  for (const key of keys) {
    const ca = a?.get(key)?.metrics[metric];
    const cb = b?.get(key)?.metrics[metric];
    if (ca && cb) {
      out.keys.push(key);
      out.before.push(ca);
      out.after.push(cb);
    }
  }
  return out;
}

/** Mean of a run's per-entry cost or agent time on `keys`; `null` when none is recorded. */
export function perEntryMean(
  cells: Map<string, Map<string, ComparedCell>>,
  runId: string,
  field: "cost" | "seconds",
  keys: readonly string[],
): number | null {
  const values: number[] = [];
  for (const key of keys) {
    const value = cells.get(runId)?.get(key)?.[field];
    if (typeof value === "number" && Number.isFinite(value) && (field === "seconds" || value > 0)) values.push(value);
  }
  return values.length ? values.reduce((s, v) => s + v, 0) / values.length : null;
}

/** A stable seed from a string (a run id), so each row's interval is its own. */
export function seedOf(text: string): number {
  let h = 2166136261;
  for (let i = 0; i < text.length; i++) h = Math.imul(h ^ text.charCodeAt(i), 16777619);
  return h >>> 0;
}

/** The most evaluations one comparison takes (the API's `MAX_COMPARED_EVALUATIONS`). */
const MAX_COMPARED_EVALUATIONS = 4;

/** Why a selection cannot be compared, read by `t_i18n`. @i18n-keys */
const NOT_COMPARABLE = {
  tooFew: "Select at least two evaluations to compare them.",
  tooMany: "Compare at most four evaluations at a time.",
  noFamily: "An evaluation that has never run has no family to compare on.",
  families: "Only evaluations of the same family can be compared.",
} as const;

/** Whether the selected evaluations can be compared, and if not why (an i18n key). */
export function comparisonEligibility(
  selected: ReadonlyArray<{ family: string | null }>,
): { canCompare: boolean; reason: string | null } {
  if (selected.length < 2) return { canCompare: false, reason: NOT_COMPARABLE.tooFew };
  if (selected.length > MAX_COMPARED_EVALUATIONS) return { canCompare: false, reason: NOT_COMPARABLE.tooMany };
  if (selected.some((e) => !e.family)) return { canCompare: false, reason: NOT_COMPARABLE.noFamily };
  if (new Set(selected.map((e) => e.family)).size > 1) return { canCompare: false, reason: NOT_COMPARABLE.families };
  return { canCompare: true, reason: null };
}

/** Metrics a family's pages derive from two of its pooled metrics: one micro F1 over
 *  both item sets together (their numerators and denominators added up), listed first.
 *  STIX: Bundle F1, the objects and the relationships a bundle should hold, counted
 *  together, so relationships weigh by their number. */
const DERIVED_METRICS: Record<string, Array<{ key: string; label: string; parts: [string, string] }>> = {
  STIX_BUNDLE: [{ key: "stix.bundle_f1", label: "Bundle F1", parts: ["stix.entity_prf", "stix.relationship_prf"] }],
};

/** The comparison with its family's derived metrics added to `metrics` and to every cell
 *  that holds both parts (counts and tp/fp/fn summed). */
export function withDerivedMetrics(comparison: EvaluationComparison): EvaluationComparison {
  const derived = (DERIVED_METRICS[comparison.family] ?? []).filter(
    (d) => d.parts.every((p) => comparison.metrics.some((m) => m.key === p)) && !comparison.metrics.some((m) => m.key === d.key),
  );
  if (!derived.length) return comparison;
  return {
    ...comparison,
    metrics: [...derived.map((d) => ({ key: d.key, label: d.label, direction: "higher_is_better" })), ...comparison.metrics],
    cells: comparison.cells.map((cell) => {
      const metrics = { ...cell.metrics };
      const counts = { ...(cell.counts ?? {}) };
      for (const d of derived) {
        const [a, b] = d.parts.map((p) => cell.metrics[p]);
        if (a && b) metrics[d.key] = [a[0] + b[0], a[1] + b[1]];
        const [ca, cb] = d.parts.map((p) => cell.counts?.[p]);
        if (ca && cb) counts[d.key] = [ca[0] + cb[0], ca[1] + cb[1], ca[2] + cb[2]];
      }
      return { ...cell, metrics, counts };
    }),
  };
}
