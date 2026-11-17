/**
 * One status config for a run on every surface - `yarn test`.
 *
 * A failed run the platform ended (a rollout cut, a restart) keeps its `failed`
 * status and carries the server's `outcome: "interrupted"`. Only
 * `runStatusConfig` reads that outcome: a surface that maps a run status
 * through `STATUS_CONFIG[...status]` directly shows "Failed" for it while the
 * run list one click away reads "Interrupted". This test fails when a surface
 * that shows a run or a delegation status looks it up without
 * `runStatusConfig`.
 */
import { expect, test } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`).
const SRC = join(process.cwd(), "src") + sep;

function* files(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "node_modules") yield* files(full);
    } else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
      yield full;
    }
  }
}

/** A run or a delegation status looked up straight in the table. */
const DIRECT_RUN_LOOKUP = /(?<![A-Z_])STATUS_CONFIG\[\s*(run|desc|d|delegation|r)\.status\s*\]/;
/** Where `runStatusConfig` itself lives: the one place allowed to read the table. */
const HOME = `${sep}lib${sep}constants.ts`;

test("every surface maps a run status through runStatusConfig", () => {
  const offenders: string[] = [];
  for (const path of files(SRC)) {
    if (path.endsWith(HOME)) continue;
    const text = readFileSync(path, "utf8");
    text.split("\n").forEach((line, i) => {
      if (DIRECT_RUN_LOOKUP.test(line)) offenders.push(`${path.slice(SRC.length)}:${i + 1}`);
    });
  }
  expect(offenders, "use runStatusConfig(run) from @/lib/constants").toEqual([]);
});

/** An error block of a run or a delegation, keyed on its error text or its failed status. */
const ERROR_BLOCK = /\{\s*(run|desc|d|delegation|r)\.(error|status\s*===\s*"failed")\s*&&/;

// A run the platform ended reads "Interrupted" in
// its badge, so its error block must not say "Error" in red under it. On every
// surface that maps a run status through runStatusConfig, an error block reads the
// interrupted outcome on its line or the two lines after it, directly or through
// runErrorIsNeutral (lib/run-error-text.ts), which reads it for the surface.
const KNOWS_INTERRUPTED = /interrupted|runErrorIsNeutral\(/i;
test("every run error block knows the interrupted outcome", () => {
  const offenders: string[] = [];
  for (const path of files(SRC)) {
    if (path.endsWith(HOME)) continue;
    const text = readFileSync(path, "utf8");
    if (!text.includes("runStatusConfig(")) continue;
    const lines = text.split("\n");
    lines.forEach((line, i) => {
      if (!ERROR_BLOCK.test(line)) return;
      const window = lines.slice(i, i + 3).join("\n");
      if (!KNOWS_INTERRUPTED.test(window)) offenders.push(`${path.slice(SRC.length)}:${i + 1}`);
    });
  }
  expect(offenders, "render an interrupted run's error neutral (outcome === \"interrupted\")").toEqual([]);
});

test("no surface keeps its own run status table", () => {
  const offenders: string[] = [];
  for (const path of files(SRC)) {
    if (path.endsWith(HOME)) continue;
    if (/^const STATUS_CONFIG\b/m.test(readFileSync(path, "utf8"))) offenders.push(path.slice(SRC.length));
  }
  expect(offenders, "a local STATUS_CONFIG never knows the interrupted outcome").toEqual([]);
});
