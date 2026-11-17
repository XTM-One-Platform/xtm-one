/**
 * The pure part of `yarn e2e:shard-timings` (scripts/e2e-shard-timings.mjs):
 * how the command line splits into runs, and what a file weighs when several
 * runs measured it. Tested by scripts/lib/e2e-shard-timings.test.mjs.
 */

/** One group of reports per run, `--` between runs. */
export function splitRuns(args) {
  const runs = [[]];
  for (const arg of args) {
    if (arg === "--") runs.push([]);
    else runs[runs.length - 1].push(arg);
  }
  return runs;
}

/** The run groups that make no sense: a bare `--` anywhere means a run with no report. */
export function emptyRunIndexes(runs) {
  const empty = [];
  runs.forEach((reports, index) => {
    if (reports.length === 0) empty.push(index);
  });
  return empty;
}

/** The middle value, or the average of the two middles. */
export function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = sorted.length >> 1;
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

/** Sum the first attempts of every spec of a Playwright report into `into`. */
export function collectTimings(suite, into) {
  for (const spec of suite.specs ?? []) {
    if (!spec.file?.endsWith(".spec.ts")) continue;
    for (const run of spec.tests ?? []) {
      const first = run.results?.[0];
      if (typeof first?.duration === "number") {
        into[spec.file] = (into[spec.file] ?? 0) + first.duration / 1000;
      }
    }
  }
  for (const child of suite.suites ?? []) collectTimings(child, into);
}
