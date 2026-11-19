import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * The spec files one CI job runs when `E2E_SHARD=<index>/<total>` is set.
 *
 * Playwright's own `--shard` cuts the ordered test list into equal COUNTS, and
 * the heaviest specs sit next to each other in that order (the presentation
 * files): one job ran twice as long as another. Whole files are dealt out
 * instead, heaviest first, each to the job with the least work so far,
 * weighed by the seconds the file took on CI (the nightly table a run's select
 * job hands out, else `shard-timings.json`); a file with no timing yet weighs the median. Every
 * job of a run computes the same deal from the same files, so together they
 * run every file exactly once.
 */

export const E2E_DIR = path.dirname(fileURLToPath(import.meta.url));
/**
 * The table a CI run deals by: `E2E_SHARD_TIMINGS`, the one its select job
 * picked (the nightly table, or this committed one) and handed every job.
 */
const TIMINGS_FILE = process.env.E2E_SHARD_TIMINGS || path.join(E2E_DIR, 'shard-timings.json');

export interface Shard {
  index: number;
  total: number;
}

export function parseShard(value: string | undefined): Shard | undefined {
  if (!value) return undefined;
  const match = /^(\d+)\/(\d+)$/.exec(value.trim());
  const index = Number(match?.[1]);
  const total = Number(match?.[2]);
  if (!match || total < 1 || index < 1 || index > total) {
    throw new Error(`E2E_SHARD must read <index>/<total> with 1 <= index <= total, got "${value}"`);
  }
  return { index, total };
}

/** Every `*.spec.ts` under `dir`, relative to `tests_e2e/`, minus the `skip` subdirectories. */
export function specFiles(dir: string, skip: readonly string[] = []): string[] {
  const found: string[] = [];
  const walk = (current: string) => {
    for (const entry of readdirSync(current, { withFileTypes: true })) {
      const full = path.join(current, entry.name);
      if (entry.isDirectory()) {
        if (!skip.includes(path.relative(dir, full).split(path.sep).join('/'))) walk(full);
      } else if (entry.name.endsWith('.spec.ts')) {
        found.push(path.relative(E2E_DIR, full).split(path.sep).join('/'));
      }
    }
  };
  walk(dir);
  return found.sort();
}

export function readTimings(file: string = TIMINGS_FILE, handedOut = Boolean(process.env.E2E_SHARD_TIMINGS)): Record<string, number> {
  try {
    return JSON.parse(readFileSync(file, 'utf8')) as Record<string, number>;
  } catch (error) {
    // A job that fell back on another table than its siblings would deal the
    // files differently: a spec would run twice or never.
    if (handedOut) throw new Error(`E2E_SHARD_TIMINGS names ${file}, which does not read: ${String(error)}`);
    return {};
  }
}

/** `files` dealt to `total` jobs, largest weight first, each to the lightest job. */
export function deal(files: readonly string[], total: number, timings: Record<string, number>): string[][] {
  const known = files
    .map((file) => timings[file])
    .filter((seconds): seconds is number => typeof seconds === 'number' && seconds >= 0)
    .sort((a, b) => a - b);
  const fallback = known.length > 0 ? known[Math.floor(known.length / 2)] : 1;
  const weight = (file: string) => timings[file] ?? fallback;
  const jobs: string[][] = Array.from({ length: total }, () => []);
  const loads: number[] = Array.from({ length: total }, () => 0);
  const order = [...files].sort((a, b) => weight(b) - weight(a) || a.localeCompare(b));
  for (const file of order) {
    const lightest = loads.indexOf(Math.min(...loads));
    jobs[lightest].push(file);
    loads[lightest] += weight(file);
  }
  return jobs.map((job) => job.sort());
}

/**
 * The `testMatch` of a project whose specs live in `dir`: every spec there
 * when no shard is asked for, otherwise this job's share of them.
 */
export function shardMatch(dir: string, skip: readonly string[] = [], shard = parseShard(process.env.E2E_SHARD)): string[] | undefined {
  if (!shard) return undefined;
  const mine = deal(specFiles(dir, skip), shard.total, readTimings())[shard.index - 1];
  // A job with nothing to run must still match nothing rather than everything.
  return mine.length > 0 ? mine.map((file) => `**/${file}`) : ['**/__no_spec_in_this_shard__'];
}
