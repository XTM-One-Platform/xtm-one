#!/usr/bin/env node
/**
 * Rebuild tests_e2e/shard-timings.json - the seconds each spec file takes on
 * CI, which the E2E jobs deal the files by (tests_e2e/shards.ts) - from the
 * JSON reports of a run.
 *
 *   yarn e2e:shard-timings <timings.json>... [-- <timings.json>...]...
 *
 * Every E2E job of the `E2E Tests` workflow uploads its report in its
 * `test-results-*` artifact (`test-results/timings.json`): download the
 * artifacts of a green run and pass all of them. Reports of several runs go
 * between `--`: a file takes the MEDIAN of its timings across the runs. One
 * run is a noisy sample - over five runs the median of a spec moved by 22 %
 * on average, up to 43 % - and weights taken from a single run did not predict
 * the next one: 3-5 runs is what makes the deal hold. A file the reports do not
 * cover keeps its previous timing; a file that no longer exists is dropped.
 * First attempts only: a retry measures the flake, not the spec.
 */

import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { collectTimings, emptyRunIndexes, median, splitRuns } from "./lib/e2e-shard-timings.mjs";

const E2E_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "tests_e2e");
const TIMINGS_FILE = path.join(E2E_DIR, "shard-timings.json");
const USAGE = "usage: yarn e2e:shard-timings <timings.json>... [-- <timings.json>...]...";

const runs = splitRuns(process.argv.slice(2));
const empty = emptyRunIndexes(runs);
if (empty.length > 0) {
  console.error(USAGE);
  if (runs.length === 1) console.error("no report given: pass the timings.json of each E2E job of a run");
  else console.error(`run ${empty.map((index) => index + 1).join(", ")} has no report - a lone or doubled "--" separates runs, it does not start or end one`);
  process.exit(1);
}

const perRun = {};
for (const reports of runs) {
  const measured = {};
  for (const report of reports) {
    for (const suite of JSON.parse(readFileSync(report, "utf8")).suites ?? []) collectTimings(suite, measured);
  }
  for (const [file, seconds] of Object.entries(measured)) (perRun[file] ??= []).push(seconds);
}
const weights = Object.fromEntries(Object.entries(perRun).map(([file, seconds]) => [file, median(seconds)]));

const previous = existsSync(TIMINGS_FILE) ? JSON.parse(readFileSync(TIMINGS_FILE, "utf8")) : {};
const merged = { ...previous, ...weights };
const timings = Object.fromEntries(
  Object.keys(merged)
    .filter((file) => existsSync(path.join(E2E_DIR, file)))
    .sort()
    .map((file) => [file, Math.round(merged[file] * 10) / 10]),
);
writeFileSync(TIMINGS_FILE, `${JSON.stringify(timings, null, 2)}\n`);
console.log(
  `${Object.keys(weights).length} files measured over ${runs.length} run(s), ${Object.keys(timings).length} in ${path.relative(process.cwd(), TIMINGS_FILE)}`,
);
