/**
 * Which vitest files scan the source tree from disk: the guards that list a
 * directory and read every file of it (`mode-split-guard.test.ts`,
 * `data-table-gates.test.ts`, ...).
 *
 * A change breaks such a guard without naming any of its files, so neither the
 * pull request's static checks nor `vitest related` reach it: #745 went red on
 * `mode-split-guard.test.ts` in the merge group only. CI's "Frontend gates"
 * job runs every file this script prints on each pull request, and the
 * sandbox's `frontend-gates` check runs the same command before a push.
 *
 * The rule is the one `backend/tests/selection.py` applies in the queue: the
 * file imports `fs` and lists a directory (`readdir`, `glob`, `opendir`).
 * Node's own test files are left to `node-test-files.mjs`.
 */
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { nodeTestFiles } from "./node-test-files.mjs";

const ROOTS = ["src", "tests_unit"];
const TEST_FILE = /\.test\.(ts|tsx)$/;
const READS_FILES = /(?:from|require\()\s*["'](?:node:)?fs(?:\/promises)?["']/;
const LISTS_A_DIRECTORY = /\breaddir(?:Sync)?\(|\bglob(?:Sync)?\(|\bopendir(?:Sync)?\(/;

function* walk(dir) {
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch {
    return;
  }
  for (const entry of entries) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "node_modules" || entry.name === "dist") continue;
      yield* walk(full);
    } else if (entry.isFile() && TEST_FILE.test(entry.name)) {
      yield full;
    }
  }
}

/** Whether a test file's source scans the tree: it imports `fs` and lists a directory. */
export function scansTheTree(source) {
  return READS_FILES.test(source) && LISTS_A_DIRECTORY.test(source);
}

/** Every vitest file under `root` that scans the tree, relative to `root` with forward slashes. */
export function sourceScanTests(root) {
  const nodeOwn = new Set(nodeTestFiles(root));
  const found = [];
  for (const dir of ROOTS) {
    for (const file of walk(join(root, dir))) {
      const path = relative(root, file).split(sep).join("/");
      if (nodeOwn.has(path)) continue;
      if (scansTheTree(readFileSync(file, "utf8"))) found.push(path);
    }
  }
  return found.sort();
}

// CLI: one path per line, for `yarn vitest run $(node scripts/source-scan-tests.mjs)`.
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const root = join(fileURLToPath(new URL(".", import.meta.url)), "..");
  console.log(sourceScanTests(root).join("\n"));
}
