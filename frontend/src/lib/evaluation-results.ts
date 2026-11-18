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
 * One evaluation's Results (#2), pure: which runs a ranking pools (one launch, every
 * rerun of one setup, the newest run of each configuration, every run), each
 * configuration pooled over them with its 95% interval, precision and recall; how far
 * reruns of one setup spread, and which expected items they found every time; the CSV
 * and Markdown exports.
 *
 * The platform rule holds throughout: an entry a run's headlines leave out (a platform
 * error, a judge failure: `counted === false`) is left out here too; an answer the scorer
 * could not read counts, as everything expected missing.
 */

import type { BundleReview, ComparedCell, ComparedRun } from "@/types";
import { buildModel, compareEntity, compareRelationship, EXPECTED } from "./evaluation-bundle-review.ts";
import { rankSpread, seedOf, type Counts, type Interval } from "./evaluation-compare.ts";
import {
  countsOf,
  launchKind,
  perEntry,
  poolRuns,
  setupKeyOf,
  spreadOf,
  type BoardRun,
  type Launch,
  type Spread,
} from "./evaluation-leaderboard.ts";

type Board = { launches: Launch[]; runs: BoardRun[] };
type CellIndex = Map<string, Map<string, ComparedCell>>;

// ── Which runs a ranking pools ──

export type ResultSet =
  | { id: string; kind: "launch"; launch: number }
  | { id: string; kind: "setup"; setup: string; launches: number[] }
  | { id: "newest"; kind: "newest" }
  | { id: "all"; kind: "all" };

/**
 * The run sets offered: each experiment launch, newest first; every run of a setup that
 * ran in several launches (its reruns); then the newest run of each configuration, and
 * every run.
 */
export function resultSets(board: Board): ResultSet[] {
  const out: ResultSet[] = [];
  for (let i = board.launches.length - 1; i >= 0; i--) {
    if (launchKind(board.launches[i]) === "experiment") out.push({ id: `l${i}`, kind: "launch", launch: i });
  }
  const bySetup = new Map<string, Set<number>>();
  for (const r of [...board.runs].sort((a, b) => a.launch - b.launch)) {
    const key = setupKeyOf(r.run);
    if (!bySetup.has(key)) bySetup.set(key, new Set());
    bySetup.get(key)!.add(r.launch);
  }
  for (const [setup, launches] of bySetup) {
    if (launches.size > 1) out.push({ id: `s:${setup}`, kind: "setup", setup, launches: [...launches].sort((a, b) => a - b) });
  }
  out.push({ id: "newest", kind: "newest" }, { id: "all", kind: "all" });
  return out;
}

/** The runs a set pools. */
export function runsOfSet(board: Board, set: ResultSet): BoardRun[] {
  if (set.kind === "launch") return board.runs.filter((r) => r.launch === set.launch);
  if (set.kind === "setup") return board.runs.filter((r) => setupKeyOf(r.run) === set.setup);
  if (set.kind === "all") return [...board.runs];
  const newest = new Map<string, BoardRun>();
  for (const r of board.runs) {
    const cur = newest.get(r.config);
    if (!cur || (r.run.created_at ?? "") > (cur.run.created_at ?? "")) newest.set(r.config, r);
  }
  return [...newest.values()];
}

// ── The ranking ──

export interface ResultRow extends Interval {
  /** The configuration (agent × model). */
  id: string;
  /** Its newest run of the set, which names it. */
  run: ComparedRun;
  runs: BoardRun[];
  best: number;
  worst: number;
  /** Micro precision and recall (0-100) from the summed tp / fp / fn; null when not reported. */
  precision: number | null;
  recall: number | null;
  /** Mean per-entry cost (USD) and agent seconds over its runs. */
  cost: number | null;
  seconds: number | null;
  /** Its counted answers, and those the scorer could not read. */
  answers: number;
  unreadable: number;
}

/** Precision and recall of `[tp, fp, fn]`: a side with nothing to count is perfect. */
export function precisionRecall(c: readonly [number, number, number] | undefined): { precision: number | null; recall: number | null } {
  if (!c) return { precision: null, recall: null };
  const [tp, fp, fn] = c;
  return { precision: tp + fp ? (100 * tp) / (tp + fp) : 100, recall: tp + fn ? (100 * tp) / (tp + fn) : 100 };
}

/** Each configuration pooled over its runs of the set, best first, with its rank spread. */
export function rankConfigs(runs: readonly BoardRun[], cells: CellIndex, key: string, salt: string): ResultRow[] {
  const byConfig = new Map<string, BoardRun[]>();
  for (const r of runs) byConfig.set(r.config, [...(byConfig.get(r.config) ?? []), r]);
  const rows: Omit<ResultRow, "best" | "worst">[] = [];
  for (const [config, own] of byConfig) {
    const perRun: Counts[][] = own.map((r) => countsOf(cells, r.run.id, key));
    const interval = poolRuns(perRun, seedOf(config + key + salt));
    if (!interval) continue;
    const summed = own.reduce<[number, number, number] | undefined>((acc, r) => {
      const c = r.counts[key];
      if (!c) return acc;
      return acc ? [acc[0] + c[0], acc[1] + c[1], acc[2] + c[2]] : [c[0], c[1], c[2]];
    }, undefined);
    const ids = own.map((r) => r.run.id);
    const newest = own.reduce((a, b) => ((b.run.created_at ?? "") > (a.run.created_at ?? "") ? b : a));
    rows.push({
      id: config,
      run: newest.run,
      runs: own,
      ...interval,
      ...precisionRecall(summed),
      cost: perEntry(cells, ids, "cost"),
      seconds: perEntry(cells, ids, "seconds"),
      answers: own.reduce((s, r) => s + r.answers, 0),
      unreadable: own.reduce((s, r) => s + r.unreadable, 0),
    });
  }
  return rankSpread(rows.sort((a, b) => b.value - a.value));
}

// ── Consistency across reruns of one setup ──

export interface SetupChoice {
  setup: string;
  /** The launches its runs belong to. */
  launches: number;
  runs: number;
}

/** The setups some configuration ran more than once, the most recent first. */
export function consistencySetups(board: Board): SetupChoice[] {
  const bySetup = new Map<string, BoardRun[]>();
  for (const r of board.runs) {
    const key = setupKeyOf(r.run);
    bySetup.set(key, [...(bySetup.get(key) ?? []), r]);
  }
  return [...bySetup.entries()]
    .filter(([, runs]) => {
      const perConfig = new Map<string, number>();
      for (const r of runs) perConfig.set(r.config, (perConfig.get(r.config) ?? 0) + 1);
      return [...perConfig.values()].some((n) => n > 1);
    })
    .map(([setup, runs]) => ({ setup, launches: new Set(runs.map((r) => r.launch)).size, runs: runs.length, last: Math.max(...runs.map((r) => r.launch)) }))
    .sort((a, b) => b.last - a.last)
    .map(({ setup, launches, runs }) => ({ setup, launches, runs }));
}

export interface ConsistencyRow extends Spread {
  /** The configuration (agent × model). */
  id: string;
  run: ComparedRun;
  runs: BoardRun[];
  /** Each run's figure. */
  values: number[];
}

/** Each configuration that ran the setup twice or more: its runs' figures and their spread. */
export function consistencyRows(board: Board, setup: string, key: string): ConsistencyRow[] {
  const byConfig = new Map<string, BoardRun[]>();
  for (const r of board.runs) if (setupKeyOf(r.run) === setup) byConfig.set(r.config, [...(byConfig.get(r.config) ?? []), r]);
  const out: ConsistencyRow[] = [];
  for (const [config, runs] of byConfig) {
    const scored = runs.filter((r) => r.values[key] != null);
    const values = scored.map((r) => r.values[key] as number);
    const spread = spreadOf(values);
    if (spread) out.push({ id: config, run: scored[0].run, runs: scored, values, ...spread });
  }
  return out.sort((a, b) => b.mean - a.mean);
}

/** Which expected items a metric counts, for the "found every time" tally. */
export const FOUND_PARTS: Record<string, "objects" | "relationships" | "both"> = {
  "stix.entity_prf": "objects",
  "stix.relationship_prf": "relationships",
  "stix.bundle_f1": "both",
};

/** Bundle reviews read at once (the API's limits). */
const MAX_GROUPS = 200;
const MAX_GROUP_SIZE = 32;

/**
 * The result groups to review for the tally: per configuration row and entry, its runs'
 * counted answers (32 newest at most). Rows past what one read takes are `skipped`.
 */
export function foundGroups(
  rows: readonly Pick<ConsistencyRow, "id" | "runs">[],
  cells: CellIndex,
): { groups: string[][]; owners: string[]; skipped: string[] } {
  const groups: string[][] = [];
  const owners: string[] = [];
  const skipped: string[] = [];
  for (const row of rows) {
    const byEntry = new Map<string, string[]>();
    const newestFirst = [...row.runs].sort((a, b) => (b.run.created_at ?? "").localeCompare(a.run.created_at ?? ""));
    for (const r of newestFirst) {
      for (const cell of cells.get(r.run.id)?.values() ?? []) {
        if (cell.counted === false) continue;
        const ids = byEntry.get(cell.entry_key) ?? [];
        if (ids.length < MAX_GROUP_SIZE) ids.push(cell.result_id);
        byEntry.set(cell.entry_key, ids);
      }
    }
    const own = [...byEntry.values()].filter((ids) => ids.length > 0);
    if (!own.length) continue;
    if (groups.length + own.length > MAX_GROUPS) {
      skipped.push(row.id);
      continue;
    }
    for (const ids of own) {
      groups.push(ids);
      owners.push(row.id);
    }
  }
  return { groups, owners, skipped };
}

export interface Found {
  every: number;
  some: number;
  never: number;
}

/** Per row, how many expected items every answer, some answers, or none found. */
export function tallyFound(
  reviews: readonly BundleReview[],
  owners: readonly string[],
  part: "objects" | "relationships" | "both",
): Map<string, Found> {
  const out = new Map<string, Found>();
  reviews.forEach((review, i) => {
    const owner = owners[i];
    if (owner == null) return;
    const found = out.get(owner) ?? { every: 0, some: 0, never: 0 };
    const keys = review.answers.map((a) => a.key);
    const tally = (n: number) => {
      if (n === keys.length) found.every++;
      else if (n > 0) found.some++;
      else found.never++;
    };
    const model = buildModel(review);
    if (keys.length) {
      if (part !== "relationships") {
        for (const g of model.groups) if (g.by[EXPECTED]) tally(keys.filter((s) => compareEntity(g, EXPECTED, s) === "tp").length);
      }
      if (part !== "objects") {
        for (const r of model.rels) {
          if (r.kinds[EXPECTED]) tally(keys.filter((s) => compareRelationship(model, r, EXPECTED, s)?.st === "tp").length);
        }
      }
    }
    out.set(owner, found);
  });
  return out;
}

// ── Methodology figures and exports ──

/** What the entries expect in all on a metric reporting `[tp, fp, fn]`: tp + fn of one
 *  counted answer per entry (every answer of an entry has the same expected items). */
export function expectedTotal(cells: CellIndex, key: string): number | null {
  const perEntry = new Map<string, number>();
  for (const byEntry of cells.values()) {
    for (const cell of byEntry.values()) {
      const c = cell.counted === false ? undefined : cell.counts?.[key];
      if (c && !perEntry.has(cell.entry_key)) perEntry.set(cell.entry_key, c[0] + c[2]);
    }
  }
  return perEntry.size ? [...perEntry.values()].reduce((s, v) => s + v, 0) : null;
}

/** The metric suite versions the runs were scored with. */
export function suiteVersions(runs: readonly BoardRun[]): string[] {
  return [...new Set(runs.map((r) => r.run.metric_suite_version).filter((v): v is string => !!v))].sort();
}

/** A methodology as Markdown: its title, then one bold term and its text per paragraph. */
export function methodologyMarkdown(title: string, items: readonly { term: string; text: string }[]): string {
  return `# ${title}\n\n${items.map((it) => `**${it.term}.** ${it.text}`).join("\n\n")}\n`;
}

/** A file name's safe stem. */
export function slugOf(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "evaluation";
}

/** Every `var(--name)` of an SVG's markup resolved to its value, so the file stands alone. */
export function resolveCssVars(markup: string, lookup: (name: string) => string): string {
  return markup.replace(/var\((--[\w-]+)\)/g, (_whole, name: string) => lookup(name) || "currentColor");
}
