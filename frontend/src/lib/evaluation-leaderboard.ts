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
const SAMPLES = 1000;

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
  cost: number | null;
  seconds: number | null;
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
      out.push({
        run,
        config: configKey(run),
        launch: index,
        values: Object.fromEntries(metricKeys.map((key) => [key, runValue(cells, run.id, key)])),
        cost: perEntry(cells, [run.id], "cost"),
        seconds: perEntry(cells, [run.id], "seconds"),
      });
    }
  });
  return { launches, runs: out };
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
