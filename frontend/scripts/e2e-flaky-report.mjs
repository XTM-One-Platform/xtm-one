#!/usr/bin/env node
/**
 * Report the flaky tests of one E2E job from the Playwright JSON report its
 * "Run E2E tests" step writes to `test-results/timings.json`: the Markdown
 * table goes to stdout (the step appends it to the job summary) and one
 * `::warning` annotation per flaky test goes to stderr, on its own line. It
 * never fails the job: a report that is missing, not valid JSON or not in
 * Playwright's shape - the job crashed before writing it - prints nothing
 * and exits 0.
 *
 *   node scripts/e2e-flaky-report.mjs <report.json>
 */

import { readFileSync } from "node:fs";

import { annotation, collectFlaky, formatFlakyReport } from "./lib/e2e-flaky.mjs";

const [reportPath] = process.argv.slice(2);

let flaky;
try {
  const report = JSON.parse(readFileSync(reportPath, "utf8"));
  flaky = [];
  for (const suite of report.suites ?? []) collectFlaky(suite, flaky);
} catch {
  process.exit(0); // missing, invalid or malformed report: nothing to report
}

for (const entry of flaky) console.error(annotation(entry));
const markdown = formatFlakyReport(flaky);
if (markdown) console.log(markdown);
