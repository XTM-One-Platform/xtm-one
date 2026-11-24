/**
 * The pure part of the flaky-test report over the Playwright JSON report
 * (`frontend/test-results/timings.json`): which tests passed only after a
 * retry, what the retries cost, and the Markdown block and GitHub annotation
 * to write for them. Tested by scripts/lib/e2e-flaky.test.mjs.
 */

/**
 * Collect the tests whose `status` is `"flaky"` into `into`, recursing over
 * the suite tree. `titles` is the chain of the enclosing `suite.title`s;
 * empty titles and titles that just repeat the spec file name drop out of
 * the chain. Returns `into`.
 */
export function collectFlaky(suite, into = [], titles = []) {
  for (const spec of suite.specs ?? []) {
    const fileName = spec.file?.split("/").pop();
    const chain = titles.filter((title) => title && title !== spec.file && title !== fileName);
    for (const test of spec.tests ?? []) {
      if (test.status !== "flaky") continue;
      const results = test.results ?? [];
      const firstFailed = results.find((result) => result.status !== "passed");
      const message = firstFailed?.error?.message;
      const wasted = results.reduce(
        (sum, result) =>
          result.status !== "passed" && typeof result.duration === "number" ? sum + result.duration : sum,
        0,
      );
      into.push({
        file: spec.file,
        line: spec.line,
        title: [...chain, spec.title].join(" › "),
        project: test.projectName,
        attempts: results.length,
        firstError: typeof message === "string" ? message.split("\n")[0] : "",
        wastedSeconds: Math.round((wasted / 1000) * 10) / 10,
      });
    }
  }
  for (const child of suite.suites ?? []) {
    collectFlaky(child, into, child.title ? [...titles, child.title] : titles);
  }
  return into;
}

/** The Markdown block for a flaky list: `""` when the list is empty. */
export function formatFlakyReport(flaky) {
  if (flaky.length === 0) return "";
  const lines = [
    `### Flaky tests (passed only on retry): ${flaky.length}`,
    "",
    "| Test | Attempts | Lost | First error |",
    "| --- | --- | --- | --- |",
  ];
  for (const entry of flaky) {
    const firstError = String(entry.firstError ?? "")
      .replace(/\|/g, "\\|")
      .slice(0, 120);
    lines.push(
      `| \`${entry.file}:${entry.line}\` ${entry.title} | ${entry.attempts} | ${entry.wastedSeconds} s | ${firstError} |`,
    );
  }
  return lines.join("\n");
}

/**
 * The GitHub workflow annotation for one flaky entry. The message has `%`,
 * carriage returns and newlines percent-encoded, as workflow commands want.
 */
export function annotation(entry) {
  const retries = entry.attempts - 1;
  const message = `${entry.title} passed only after ${retries} ${retries === 1 ? "retry" : "retries"}`;
  const escaped = message
    .replace(/%/g, "%25")
    .replace(/\r/g, "%0D")
    .replace(/\n/g, "%0A");
  return `::warning file=frontend/tests_e2e/${entry.file},line=${entry.line},title=Flaky E2E test::${escaped}`;
}
