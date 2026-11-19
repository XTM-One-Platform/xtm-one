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

// The run review of a STIX bundle evaluation (#12282), its pure part: one run over every
// report it answered, against the expected bundles, nothing, or another run of the same
// evaluation. The figures follow the platform's rule: an entry the run's headlines leave
// out (`counted === false`: a platform error, a judge failure) is left out here too; an
// answer the scorer could not read is counted, as everything missing.

import type { ComparedCell, ComparedRun, EvaluationComparison } from "@/types";
import { EXPECTED, NONE, compareEntity, nearKind, type NearKind, type ReviewModel } from "./evaluation-bundle-review.ts";
import { pooled } from "./evaluation-compare.ts";
import { configKey } from "./evaluation-leaderboard.ts";

export const BUNDLE_F1 = "stix.bundle_f1";
export const ENTITY_F1 = "stix.entity_prf";
export const RELATIONSHIP_F1 = "stix.relationship_prf";
/** The official Entity F1 per type (the mean of each type's F1). */
export const ENTITY_F1_BY_TYPE = "stix.entity_prf.by_type";
/** The run figures the review shows, in order. */
const RUN_METRICS = [BUNDLE_F1, ENTITY_F1, RELATIONSHIP_F1] as const;
export type RunMetric = (typeof RUN_METRICS)[number];

export interface Prf {
  tp: number;
  fp: number;
  fn: number;
}

/** Precision on 0-100; an answer with nothing found and nothing wrong is right. */
export function precision(c: Prf): number {
  return c.tp + c.fp === 0 ? 100 : (100 * c.tp) / (c.tp + c.fp);
}

/** Recall on 0-100; nothing expected and nothing missed is complete. */
export function recall(c: Prf): number {
  return c.tp + c.fn === 0 ? 100 : (100 * c.tp) / (c.tp + c.fn);
}

/** Every completed run the comparison knows (the compared ones and the choices), oldest first. */
export function runsOfComparison(comparison: EvaluationComparison | undefined): ComparedRun[] {
  const byId = new Map<string, ComparedRun>();
  for (const ev of comparison?.evaluations ?? []) {
    for (const run of [...ev.run_choices, ...ev.runs]) if (!byId.has(run.id)) byId.set(run.id, run);
  }
  return [...byId.values()].sort((a, b) => (a.created_at ?? "").localeCompare(b.created_at ?? "") || a.id.localeCompare(b.id));
}

/** The runs of the same agent (or flow) on the same model, oldest first, `run` included. */
export function configRuns(runs: readonly ComparedRun[], run: ComparedRun): ComparedRun[] {
  const key = configKey(run);
  return runs.filter((r) => configKey(r) === key);
}

/** The previous completed run of the same agent on the same model, or null. */
export function previousRun(runs: readonly ComparedRun[], run: ComparedRun): ComparedRun | null {
  const mine = configRuns(runs, run);
  const i = mine.findIndex((r) => r.id === run.id);
  return i > 0 ? mine[i - 1] : null;
}

/** What the run is compared with: the reference run, else the previous run of its configuration. */
export function comparatorOf(runs: readonly ComparedRun[], under: ComparedRun, against: string): ComparedRun | null {
  if (against !== EXPECTED && against !== NONE) return runs.find((r) => r.id === against) ?? null;
  return previousRun(runs, under);
}

export interface RunFigures {
  /** Entries the run has a result on. */
  entries: number;
  /** Entries its figures count. */
  counted: number;
  /** Entries left out of its figures (platform error, judge failure). */
  leftOut: number;
  /** Counted entries whose answer the scorer could not read. */
  unreadable: number;
  /** Each run metric pooled over the counted entries (0-100), null without one. */
  value: Record<RunMetric, number | null>;
  /** Each run metric's tp / fp / fn over the counted entries, null when not served. */
  counts: Record<RunMetric, Prf | null>;
  /** The judge's mean over the counted entries it scored. */
  judge: number | null;
  /** Totals over every result of the run (an answer left out still cost its run). */
  cost: number | null;
  seconds: number | null;
}

/** A run's figures over its cells, on `only` (entry keys) when given. */
export function runFigures(cells: ReadonlyMap<string, ComparedCell> | undefined, only?: ReadonlySet<string>): RunFigures {
  const list = [...(cells?.values() ?? [])].filter((c) => !only || only.has(c.entry_key));
  const counted = list.filter((c) => c.counted);
  const value = {} as Record<RunMetric, number | null>;
  const counts = {} as Record<RunMetric, Prf | null>;
  for (const key of RUN_METRICS) {
    const pairs = counted.map((c) => c.metrics[key]).filter((p): p is [number, number] => !!p);
    value[key] = pooled(pairs);
    const triples = counted.map((c) => c.counts?.[key]);
    counts[key] =
      triples.length && triples.every(Boolean)
        ? triples.reduce<Prf>((acc, t) => ({ tp: acc.tp + (t as number[])[0], fp: acc.fp + (t as number[])[1], fn: acc.fn + (t as number[])[2] }), { tp: 0, fp: 0, fn: 0 })
        : null;
  }
  const judged = counted.map((c) => c.judge_score).filter((v): v is number => typeof v === "number");
  const sum = (key: "cost" | "seconds") => {
    const v = list.map((c) => c[key]).filter((x): x is number => typeof x === "number");
    return v.length ? v.reduce((a, b) => a + b, 0) : null;
  };
  return {
    entries: list.length,
    counted: counted.length,
    leftOut: list.length - counted.length,
    unreadable: counted.filter((c) => c.unreadable).length,
    value,
    counts,
    judge: judged.length ? judged.reduce((a, b) => a + b, 0) / judged.length : null,
    cost: sum("cost"),
    seconds: sum("seconds"),
  };
}

/** The entries two runs both count: where a change between them is measured. */
export function sharedCounted(a: ReadonlyMap<string, ComparedCell> | undefined, b: ReadonlyMap<string, ComparedCell> | undefined): Set<string> {
  const out = new Set<string>();
  for (const [key, cell] of a ?? []) if (cell.counted && b?.get(key)?.counted) out.add(key);
  return out;
}

/** A cell's value of one metric (0-100); null when its run's figures leave it out. */
export function cellValue(cell: ComparedCell | undefined, metric: string): number | null {
  if (!cell?.counted) return null;
  const pair = cell.metrics[metric];
  return pair ? pooled([pair]) : null;
}

export interface ReviewEntry {
  key: string;
  label: string;
  position: number;
}

/** The entries every given run has a result on, in dataset order. */
export function reviewEntries(comparison: EvaluationComparison | undefined, cells: ReadonlyMap<string, ReadonlyMap<string, ComparedCell>>, runIds: readonly string[]): ReviewEntry[] {
  return (comparison?.entries ?? [])
    .filter((e) => runIds.every((id) => cells.get(id)?.has(e.key)))
    .map((e) => ({ key: e.key, label: e.label, position: Math.min(...e.members.map((m) => m.position)) }))
    .sort((a, b) => a.position - b.position || a.label.localeCompare(b.label));
}

// ─── Where the entity points go ─────────────────────────────────────────────

export type CauseKey = NearKind | "missing" | "spurious" | "duplicates";

/** One answer's entity errors on one entry: each near-miss kind (pairs), missing and spurious objects, duplicates. */
export function entryCauses(model: ReviewModel, source: string): Partial<Record<CauseKey, number>> {
  const out: Partial<Record<CauseKey, number>> = {};
  const bump = (k: CauseKey, n = 1) => {
    out[k] = (out[k] ?? 0) + n;
  };
  for (const g of model.groups) {
    const st = compareEntity(g, EXPECTED, source);
    if (st === "near") bump(nearKind(g.by[EXPECTED], g.by[source]));
    else if (st === "fn") bump("missing");
    else if (st === "fp") bump("spurious");
    if (g.extra[source]) bump("duplicates", g.extra[source]);
  }
  return out;
}

interface CauseRow {
  key: CauseKey;
  count: number;
  /** How the count reads: near-miss pairs, objects, or extra copies. */
  unit: "pairs" | "objects" | "extra";
}

export interface CauseSummary {
  rows: CauseRow[];
  /** Near-miss pairs, each one missing plus one spurious object. */
  nearPairs: number;
  /** Every counted entity error: two per near-miss pair, plus missing, spurious and duplicates. */
  errors: number;
}

/** The causes added up over entries: near-miss kinds first (most first), then missing, spurious, duplicates. */
export function causeSummary(perEntry: readonly Partial<Record<CauseKey, number>>[]): CauseSummary {
  const tot: Partial<Record<CauseKey, number>> = {};
  for (const c of perEntry) for (const [k, n] of Object.entries(c) as [CauseKey, number][]) tot[k] = (tot[k] ?? 0) + n;
  const plain: CauseKey[] = ["missing", "spurious", "duplicates"];
  const near = (Object.entries(tot) as [CauseKey, number][]).filter(([k]) => !plain.includes(k)).sort((a, b) => b[1] - a[1]);
  const nearPairs = near.reduce((s, [, n]) => s + n, 0);
  const rows: CauseRow[] = [
    ...near.map(([key, count]) => ({ key, count, unit: "pairs" as const })),
    { key: "missing", count: tot.missing ?? 0, unit: "objects" },
    { key: "spurious", count: tot.spurious ?? 0, unit: "objects" },
    { key: "duplicates", count: tot.duplicates ?? 0, unit: "extra" },
  ];
  return {
    rows: rows.filter((r) => r.count > 0),
    nearPairs,
    errors: 2 * nearPairs + (tot.missing ?? 0) + (tot.spurious ?? 0) + (tot.duplicates ?? 0),
  };
}

// ─── Quick comparisons ───────────────────────────────────────────────────────

type PresetKind = "first-vs-expected" | "latest-vs-expected" | "first-to-latest" | "previous-to-latest" | "rerun" | "best-other" | "latest-alone";

export interface Preset {
  kind: PresetKind;
  /** The reference: `expected`, `none` or a run id. */
  ref: string;
  /** The run under review. */
  under: string;
  /** The runs the label names, in its order. */
  runs: ComparedRun[];
}

/**
 * The comparisons a reviewer reaches for, around the configuration of `run` (its agent on
 * its model): its first and latest runs against the expected bundles, first to latest,
 * previous to latest, a rerun of one setup against the other (the variance), the latest
 * against the best other configuration of the same launch, and the latest alone.
 * `figure` is a run's headline used to pick the best (Entity F1).
 */
export function presets(runs: readonly ComparedRun[], run: ComparedRun, figure: (runId: string) => number | null): Preset[] {
  const mine = configRuns(runs, run);
  if (!mine.length) return [];
  const first = mine[0];
  const latest = mine[mine.length - 1];
  const out: Preset[] = [{ kind: "first-vs-expected", ref: EXPECTED, under: first.id, runs: [first] }];
  if (latest.id !== first.id) {
    out.push({ kind: "latest-vs-expected", ref: EXPECTED, under: latest.id, runs: [latest] });
    out.push({ kind: "first-to-latest", ref: first.id, under: latest.id, runs: [first, latest] });
    const prev = mine[mine.length - 2];
    if (prev.id !== first.id) out.push({ kind: "previous-to-latest", ref: prev.id, under: latest.id, runs: [prev, latest] });
  }
  // A rerun: the newest two runs of one setup.
  for (let i = mine.length - 1; i > 0; i--) {
    const [x, y] = [mine[i - 1], mine[i]];
    if (x.setup_key && x.setup_key === y.setup_key) {
      out.push({ kind: "rerun", ref: x.id, under: y.id, runs: [x, y] });
      break;
    }
  }
  // The best other configuration launched with the latest run (same minute, same trigger).
  const launch = (r: ComparedRun) => `${(r.created_at ?? "").slice(0, 16)}|${r.trigger_reason ?? ""}`;
  const best = runs
    .filter((r) => r.id !== latest.id && configKey(r) !== configKey(latest) && launch(r) === launch(latest))
    .map((r) => ({ r, v: figure(r.id) }))
    .filter((x): x is { r: ComparedRun; v: number } => x.v != null)
    .sort((a, b) => b.v - a.v)[0];
  if (best) out.push({ kind: "best-other", ref: latest.id, under: best.r.id, runs: [latest, best.r] });
  out.push({ kind: "latest-alone", ref: NONE, under: latest.id, runs: [latest] });
  return out;
}

/** The answers one entry's review loads, at most `max`: the pair, the comparator, then the
 *  configuration's other runs (its history), newest first so the closest ones stay. */
export function reviewResultIds(
  resultOf: (runId: string) => string | undefined,
  pair: { under: string; ref: string },
  comparator: string | null,
  history: readonly string[],
  max = 32,
): string[] {
  const ids: string[] = [];
  const push = (runId: string | null | undefined) => {
    const rid = runId ? resultOf(runId) : undefined;
    if (rid && !ids.includes(rid) && ids.length < max) ids.push(rid);
  };
  push(pair.under);
  if (pair.ref !== EXPECTED && pair.ref !== NONE) push(pair.ref);
  push(comparator);
  for (const id of [...history].reverse()) push(id);
  return ids;
}
