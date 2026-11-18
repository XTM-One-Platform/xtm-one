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
 * The Leaderboard and Results of one evaluation's runs (#2), pure.
 *
 * Read from the same payload as the comparison (`GET /evaluations/compare`, one
 * evaluation, every completed run): per run and entry, each pooled metric's
 * `[numerator, denominator]`, the LLM cost and the agent's seconds. A run's figure adds
 * its entries' counts up and divides once, as its own headline does (`pooled`).
 *
 * - A launch is the runs started together: the same minute and the same trigger, as the
 *   Activity tab groups them.
 * - A configuration is an agent (or flow) on a model; it may have run in several launches.
 * - A configuration's figure over several runs pools them all; its 95% interval resamples
 *   the runs, then the entries of each drawn run (fixed seed), so a configuration run
 *   once is resampled on its entries alone.
 */

import type { ComparedCell, ComparedRun } from "@/types";
import { pooled, type Counts, type Interval } from "./evaluation-compare.ts";

/** Resamples per configuration interval. */
export const SAMPLES = 2000;

/** mulberry32, as `evaluation-compare.ts`: stable intervals across loads. */
function seededRandom(seed: number): () => number {
  let a = seed | 0;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export interface Launch {
  key: string;
  /** The first run's start, ISO. */
  time: string;
  trigger: string | null;
  runs: ComparedRun[];
}

/** The runs grouped by launch (same minute, same trigger), oldest launch first. */
export function launchesOf(runs: readonly ComparedRun[]): Launch[] {
  const byKey = new Map<string, Launch>();
  const ordered = [...runs].sort((a, b) => (a.created_at ?? "").localeCompare(b.created_at ?? ""));
  for (const run of ordered) {
    const key = `${(run.created_at ?? "").slice(0, 16)}|${run.trigger_reason ?? ""}`;
    const launch = byKey.get(key);
    if (launch) launch.runs.push(run);
    else byKey.set(key, { key, time: run.created_at ?? "", trigger: run.trigger_reason ?? null, runs: [run] });
  }
  return [...byKey.values()];
}

/** An agent (or flow) on a model. */
export function configKey(run: Pick<ComparedRun, "subject_id" | "model_id">): string {
  return `${run.subject_id ?? ""}|${run.model_id}`;
}

/**
 * How runs read side by side: by what differs between them. The model (without its
 * provider route prefix) unless every run has the same one and the agents differ, then
 * the agent unless every run has the same one.
 */
export function runLabeler(runs: readonly Pick<ComparedRun, "model_id" | "subject_id">[]) {
  const oneModel = new Set(runs.map((r) => r.model_id)).size === 1;
  const oneSubject = new Set(runs.map((r) => r.subject_id)).size === 1;
  return (run: Pick<ComparedRun, "model_id" | "subject_name">): string => {
    const model = run.model_id.replace(/^~/, "").split("/").pop() || run.model_id;
    // An agent gone (no name) leaves the model, never an empty label.
    return [oneModel && !oneSubject && run.subject_name ? null : model, oneSubject ? null : run.subject_name]
      .filter(Boolean)
      .join(" · ");
  };
}

/** A run's counts of `metric`, one per entry it counted. */
export function countsOf(cells: Map<string, Map<string, ComparedCell>>, runId: string, metric: string): Counts[] {
  const out: Counts[] = [];
  for (const cell of cells.get(runId)?.values() ?? []) {
    const counts = cell.metrics[metric];
    if (counts) out.push(counts);
  }
  return out;
}

/** A run's pooled figure on its own entries; null when it counted none. */
export function runValue(cells: Map<string, Map<string, ComparedCell>>, runId: string, metric: string): number | null {
  return pooled(countsOf(cells, runId, metric));
}

/** The mean per-entry cost (USD, recorded costs only) or agent seconds of some runs. */
export function perEntry(
  cells: Map<string, Map<string, ComparedCell>>,
  runIds: readonly string[],
  field: "cost" | "seconds",
): number | null {
  const values: number[] = [];
  for (const id of runIds) {
    for (const cell of cells.get(id)?.values() ?? []) {
      const v = cell[field];
      if (typeof v === "number" && Number.isFinite(v) && (field === "seconds" || v > 0)) values.push(v);
    }
  }
  return values.length ? values.reduce((s, v) => s + v, 0) / values.length : null;
}

export interface BoardRun {
  run: ComparedRun;
  config: string;
  /** Index of its launch in `launches`. */
  launch: number;
  /** Its pooled figure per metric key, on its own entries. */
  values: Record<string, number | null>;
  /** Mean per-entry cost (USD) and agent seconds. */
  cost: number | null;
  seconds: number | null;
  /** The judge's run score (0-100); null when its judge was off. */
  judge: number | null;
  /** What the run cost in all (USD, its counted entries); null when none is recorded. */
  costTotal: number | null;
  /** Wall-clock seconds from its start to its end; else its agent seconds summed. */
  duration: number | null;
  /** Its counted entries, and those whose answer the scorer could not read. */
  answers: number;
  unreadable: number;
  /** `[tp, fp, fn]` summed over its counted entries, per metric that reports them. */
  counts: Record<string, [number, number, number]>;
}

/** The cells a run's figures read: the counted ones (an anomaly or a judge failure is left out). */
function countedCells(cells: Map<string, Map<string, ComparedCell>>, runId: string): ComparedCell[] {
  return [...(cells.get(runId)?.values() ?? [])].filter((cell) => cell.counted !== false);
}

function durationOf(run: ComparedRun, counted: readonly ComparedCell[]): number | null {
  const start = run.created_at ? Date.parse(run.created_at) : NaN;
  const end = run.completed_at ? Date.parse(run.completed_at) : NaN;
  if (Number.isFinite(start) && Number.isFinite(end) && end >= start) return (end - start) / 1000;
  const seconds = counted.map((c) => c.seconds).filter((v): v is number => typeof v === "number" && Number.isFinite(v));
  return seconds.length ? seconds.reduce((s, v) => s + v, 0) : null;
}

function summedCounts(counted: readonly ComparedCell[]): Record<string, [number, number, number]> {
  const out: Record<string, [number, number, number]> = {};
  for (const cell of counted) {
    for (const [key, [tp, fp, fn]] of Object.entries(cell.counts ?? {})) {
      const sum = (out[key] ??= [0, 0, 0]);
      sum[0] += tp;
      sum[1] += fp;
      sum[2] += fn;
    }
  }
  return out;
}

/** Every run with its launch and its figures: what the Leaderboard and Results read. */
export function boardOf(
  runs: readonly ComparedRun[],
  cells: Map<string, Map<string, ComparedCell>>,
  metricKeys: readonly string[],
): { launches: Launch[]; runs: BoardRun[] } {
  const launches = launchesOf(runs);
  const out: BoardRun[] = [];
  launches.forEach((launch, index) => {
    for (const run of launch.runs) {
      const counted = countedCells(cells, run.id);
      const costs = counted.map((c) => c.cost).filter((v): v is number => typeof v === "number" && Number.isFinite(v) && v > 0);
      out.push({
        run,
        config: configKey(run),
        launch: index,
        values: Object.fromEntries(metricKeys.map((key) => [key, runValue(cells, run.id, key)])),
        cost: perEntry(cells, [run.id], "cost"),
        seconds: perEntry(cells, [run.id], "seconds"),
        judge: run.judge_score ?? null,
        costTotal: costs.length ? costs.reduce((s, v) => s + v, 0) : null,
        duration: durationOf(run, counted),
        answers: counted.length,
        unreadable: counted.filter((c) => c.unreadable).length,
        counts: summedCounts(counted),
      });
    }
  });
  return { launches, runs: out };
}

// ── The board's metrics: the family's pooled ones, then the judge, the cost and the time ──

/** The board's own metrics, past the pooled ones (keys no metric package uses). */
export const JUDGE_METRIC = "board.judge";
export const COST_METRIC = "board.cost";
export const DURATION_METRIC = "board.duration";

/** Their labels, read by `t_i18n`. @i18n-keys */
export const BOARD_METRIC_LABELS = {
  [JUDGE_METRIC]: "Judge score",
  [COST_METRIC]: "Cost (USD)",
  [DURATION_METRIC]: "Duration",
} as const;

/** A run's figure on a board metric: a pooled metric's, its judge score, its cost or its duration. */
export function boardValue(r: BoardRun, key: string): number | null {
  if (key === JUDGE_METRIC) return r.judge;
  if (key === COST_METRIC) return r.costTotal;
  if (key === DURATION_METRIC) return r.duration;
  return r.values[key] ?? null;
}

/** Cost and time are better lower; every quality figure higher. */
export function higherIsBetter(key: string): boolean {
  return key !== COST_METRIC && key !== DURATION_METRIC;
}

/** Bigger is better whatever the metric, so every "best first" is one sort; none last. */
export function goodness(value: number | null | undefined, key: string): number {
  if (value == null) return Number.NEGATIVE_INFINITY;
  return higherIsBetter(key) ? value : -value;
}

/** The best of some runs on a metric, or null when none has a figure. */
function bestOf(runs: readonly BoardRun[], key: string): BoardRun | null {
  let best: BoardRun | null = null;
  for (const r of runs) {
    if (boardValue(r, key) == null) continue;
    if (!best || goodness(boardValue(r, key), key) > goodness(boardValue(best, key), key)) best = r;
  }
  return best;
}

// ── Launches: their kind, their figures, which are shown and in what order ──

export type LaunchKind = "experiment" | "scheduled";

/** A launch a schedule started is a rerun; any other (by hand, by the API) an experiment. */
export function launchKind(launch: Pick<Launch, "trigger">): LaunchKind {
  return launch.trigger === "schedule" ? "scheduled" : "experiment";
}

export interface LaunchStats {
  best: number | null;
  bestRun: BoardRun | null;
  mean: number | null;
  runs: number;
}

/** Per launch: its best run on the metric, the mean of its runs, how many ran. */
export function launchStats(board: { launches: Launch[]; runs: BoardRun[] }, key: string): LaunchStats[] {
  return board.launches.map((_, index) => {
    const own = board.runs.filter((r) => r.launch === index);
    const values = own.map((r) => boardValue(r, key)).filter((v): v is number => v != null);
    const bestRun = bestOf(own, key);
    return {
      best: bestRun ? boardValue(bestRun, key) : null,
      bestRun,
      mean: values.length ? values.reduce((s, v) => s + v, 0) / values.length : null,
      runs: own.length,
    };
  });
}

/** How many launch columns the grid shows at a time. */
export const LAUNCH_PAGE = 6;

export type LaunchPick = "latest" | "best" | "experiment" | "scheduled" | "all" | "custom";
export type LaunchOrder = "chrono" | "newest" | "best" | "mean";

/** The launches shown (indices into `launches`), picked then ordered. */
export function shownLaunches(
  launches: readonly Launch[],
  stats: readonly LaunchStats[],
  key: string,
  pick: LaunchPick,
  order: LaunchOrder,
  custom: ReadonlySet<number> = new Set(),
): number[] {
  const all = launches.map((_, i) => i);
  const byBest = [...all].sort((a, b) => goodness(stats[b].best, key) - goodness(stats[a].best, key) || b - a);
  const list =
    pick === "latest" ? all.slice(-LAUNCH_PAGE)
      : pick === "best" ? byBest.slice(0, LAUNCH_PAGE)
        : pick === "experiment" || pick === "scheduled" ? all.filter((i) => launchKind(launches[i]) === pick)
          : pick === "custom" ? all.filter((i) => custom.has(i))
            : all;
  const cmp = (a: number, b: number) => {
    if (order === "chrono") return a - b;
    if (order === "newest") return b - a;
    const by = order === "best" ? "best" : "mean";
    return goodness(stats[b][by], key) - goodness(stats[a][by], key) || a - b;
  };
  return [...list].sort(cmp);
}

/** The bar chart's axis, zoomed on the range the bars reach so a bad launch shows. */
export function zoomedAxis(values: readonly (number | null)[]): { min: number; max: number } {
  const reached = values.filter((v): v is number => v != null);
  if (!reached.length) return { min: 0, max: 1 };
  const hi = Math.max(...reached);
  const lo = Math.min(...reached);
  const max = hi + (hi - lo) * 0.05 || hi || 1;
  const min = Math.max(0, lo - (hi - lo) * 0.25);
  return { min: min < max ? min : 0, max };
}

// ── What a launch ran: its setups, and what changed since the launch before ──

/** A run's setup key: reruns of one setup share it; without one, its agent stands for it. */
export function setupKeyOf(run: Pick<ComparedRun, "setup_key" | "subject_id">): string {
  return run.setup_key ?? `subject:${run.subject_id ?? ""}`;
}

const versionOf = (setup: Record<string, unknown> | undefined): number | null => {
  const v = setup?.persona_version ?? setup?.flow_version;
  return typeof v === "number" ? v : null;
};

/**
 * A short name per setup: its agent and version ("Harvester v3"); two setups of one agent
 * and version (other prompts, skills or settings) are told apart by their order (" · 2").
 */
export function setupNames(runs: readonly ComparedRun[]): Map<string, string> {
  const ordered = [...runs].sort((a, b) => (a.created_at ?? "").localeCompare(b.created_at ?? ""));
  const firstRun = new Map<string, ComparedRun>();
  for (const run of ordered) if (!firstRun.has(setupKeyOf(run))) firstRun.set(setupKeyOf(run), run);
  const base = new Map<string, string>();
  for (const [key, run] of firstRun) {
    const version = versionOf(run.setup);
    base.set(key, [run.subject_name || run.model_id, version != null ? `v${version}` : null].filter(Boolean).join(" "));
  }
  const seen = new Map<string, number>();
  const counts = new Map<string, number>();
  for (const name of base.values()) counts.set(name, (counts.get(name) ?? 0) + 1);
  const out = new Map<string, string>();
  for (const [key, name] of base) {
    if ((counts.get(name) ?? 0) < 2) out.set(key, name);
    else {
      const n = (seen.get(name) ?? 0) + 1;
      seen.set(name, n);
      out.set(key, `${name} · ${n}`);
    }
  }
  return out;
}

export type SetupChange =
  | { kind: "version"; field: "persona" | "flow"; from: number | null; to: number | null }
  | { kind: "prompts" | "skills" | "settings" }
  | { kind: "mode"; from: string | null; to: string | null };

/** What one agent's setup in a launch changed since its previous launch. */
export interface LaunchNotePart {
  subject: string;
  /** `first`: its first launch; `same`: nothing changed; else the changes. */
  status: "first" | "same" | "changed";
  changes: SetupChange[];
}

const same = (a: unknown, b: unknown) => JSON.stringify(a ?? {}) === JSON.stringify(b ?? {});

function setupChanges(before: Record<string, unknown>, after: Record<string, unknown>): SetupChange[] {
  const out: SetupChange[] = [];
  for (const field of ["persona", "flow"] as const) {
    const a = before[`${field}_version`];
    const b = after[`${field}_version`];
    if (a !== b && (a != null || b != null)) out.push({ kind: "version", field, from: (a as number) ?? null, to: (b as number) ?? null });
  }
  if (!same(before.prompt_versions, after.prompt_versions)) out.push({ kind: "prompts" });
  if (!same(before.skill_versions, after.skill_versions)) out.push({ kind: "skills" });
  if (!same(before.agent_config_versions, after.agent_config_versions)) out.push({ kind: "settings" });
  if ((before.execution_mode ?? null) !== (after.execution_mode ?? null)) {
    out.push({ kind: "mode", from: (before.execution_mode as string) ?? null, to: (after.execution_mode as string) ?? null });
  }
  return out;
}

/** Per launch, per agent it ran: what changed since that agent's previous launch. */
export function launchNotes(launches: readonly Launch[]): LaunchNotePart[][] {
  const last = new Map<string, Record<string, unknown>>();
  return launches.map((launch) => {
    const bySubject = new Map<string, ComparedRun>();
    for (const run of launch.runs) if (!bySubject.has(run.subject_id ?? "")) bySubject.set(run.subject_id ?? "", run);
    const parts: LaunchNotePart[] = [];
    for (const [subjectId, run] of bySubject) {
      const setup = run.setup ?? {};
      const before = last.get(subjectId);
      const subject = run.subject_name || run.model_id;
      if (!before) parts.push({ subject, status: "first", changes: [] });
      else {
        const changes = setupChanges(before, setup);
        parts.push({ subject, status: changes.length ? "changed" : "same", changes });
      }
      last.set(subjectId, setup);
    }
    return parts;
  });
}

// ── The configuration grid ──

export interface ConfigRow {
  key: string;
  runs: BoardRun[];
  /** Its runs with a figure, oldest launch first. */
  scored: BoardRun[];
  best: BoardRun | null;
  /** Last run's figure minus the first's (two scored runs at least). */
  trend: number | null;
  /** Whether it ran in one of the launches shown. */
  inShown: boolean;
}

/** One row per configuration (agent × model), over every launch. */
export function configRows(runs: readonly BoardRun[], key: string, shown: readonly number[]): ConfigRow[] {
  const byConfig = new Map<string, BoardRun[]>();
  for (const r of runs) byConfig.set(r.config, [...(byConfig.get(r.config) ?? []), r]);
  const shownSet = new Set(shown);
  return [...byConfig.entries()].map(([config, own]) => {
    const sorted = [...own].sort((a, b) => a.launch - b.launch);
    const scored = sorted.filter((r) => boardValue(r, key) != null);
    return {
      key: config,
      runs: sorted,
      scored,
      best: bestOf(scored, key),
      trend: scored.length > 1 ? (boardValue(scored[scored.length - 1], key) as number) - (boardValue(scored[0], key) as number) : null,
      inShown: sorted.some((r) => shownSet.has(r.launch)),
    };
  });
}

/** A grid column to sort on: the name, the best run, the trend, or a launch (its index). */
export type GridSort = { col: "name" | "best" | "trend" | number; dir: 1 | -1 };

/** The rows sorted on a column; `name` reads each row's name. */
export function sortConfigRows(rows: readonly ConfigRow[], sort: GridSort, key: string, name: (row: ConfigRow) => string): ConfigRow[] {
  const higher = higherIsBetter(key);
  const keyOf = (row: ConfigRow): string | number => {
    if (sort.col === "name") return name(row);
    if (sort.col === "best") return goodness(row.best ? boardValue(row.best, key) : null, key);
    if (sort.col === "trend") return row.trend == null ? Number.NEGATIVE_INFINITY : higher ? row.trend : -row.trend;
    const launch = sort.col;
    const run = row.runs.find((r) => r.launch === launch);
    return goodness(run ? boardValue(run, key) : null, key);
  };
  return [...rows].sort((x, y) => {
    const a = keyOf(x);
    const b = keyOf(y);
    const c = typeof a === "string" && typeof b === "string" ? a.localeCompare(b) : a === b ? 0 : a > b ? 1 : -1;
    return c * sort.dir || name(x).localeCompare(name(y));
  });
}

/** Several runs pooled, with a 95% interval resampling the runs then their entries. */
export function poolRuns(perRun: readonly (readonly Counts[])[], seed: number, samples = SAMPLES): Interval | null {
  const runs = perRun.filter((counts) => counts.length > 0);
  const value = pooled(runs.flat());
  if (value === null) return null;
  const rnd = seededRandom(seed);
  const draws: number[] = [];
  for (let b = 0; b < samples; b++) {
    let num = 0;
    let den = 0;
    for (let r = 0; r < runs.length; r++) {
      const run = runs[Math.floor(rnd() * runs.length)];
      for (let e = 0; e < run.length; e++) {
        const [n, d] = run[Math.floor(rnd() * run.length)];
        num += n;
        den += d;
      }
    }
    draws.push(den > 0 ? (100 * num) / den : 100);
  }
  draws.sort((x, y) => x - y);
  const at = (q: number) => draws[Math.min(draws.length - 1, Math.max(0, Math.round(q * (draws.length - 1))))];
  return { value, low: at(0.025), high: at(0.975) };
}

/** The points no other point beats on both axes: lower cost (x) and higher figure (y). */
export function paretoFront<T extends { x: number; y: number }>(points: readonly T[]): T[] {
  const front: T[] = [];
  for (const p of [...points].sort((a, b) => a.x - b.x || b.y - a.y)) {
    if (!front.length || p.y > front[front.length - 1].y) front.push(p);
  }
  return front;
}

export interface Spread {
  mean: number;
  /** Sample standard deviation. */
  sd: number;
  min: number;
  max: number;
}

/** How far a configuration's reruns spread; null under two runs. */
export function spreadOf(values: readonly number[]): Spread | null {
  if (values.length < 2) return null;
  const mean = values.reduce((s, v) => s + v, 0) / values.length;
  const sd = Math.sqrt(values.reduce((s, v) => s + (v - mean) ** 2, 0) / (values.length - 1));
  return { mean, sd, min: Math.min(...values), max: Math.max(...values) };
}

/** RFC 4180 CSV: every field quoted when it holds a comma, a quote or a line break. */
export function toCsv(rows: readonly (readonly (string | number | null)[])[]): string {
  const field = (v: string | number | null) => {
    const s = v === null ? "" : String(v);
    return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return rows.map((row) => row.map(field).join(",")).join("\n") + "\n";
}

/** A board figure as text: a dollar amount, minutes (seconds under one), else one decimal. */
export function formatBoardValue(value: number | null | undefined, key: string): string {
  if (value == null || (key === COST_METRIC && value === 0)) return "—";
  if (key === COST_METRIC) return `$${value.toFixed(2)}`;
  if (key === DURATION_METRIC) return value < 60 ? `${Math.round(value)} s` : `${Math.round(value / 60)} min`;
  return value.toFixed(1);
}
