/**
 * Which test files belong to Node's runner rather than to vitest.
 *
 * Two runners share this tree and each chokes on the other's files: vitest
 * reports "No test suite found" for a `node:test` file, and `node --test` fails
 * every vitest one. There is no glob that separates them — the two kinds sit
 * side by side under `src/` — so the split used to be a path named in two
 * places (`vitest.config.ts`'s `exclude` and the `test:unit` script). That list
 * went stale the first time main added a second `node:test` file: vitest failed
 * on it and `node --test` never saw it, so it was run by nobody.
 *
 * So the split is DERIVED instead. The one fact that decides it is the import
 * the file itself writes, which is the same fact that makes the two runners
 * incompatible in the first place.
 *
 * Used as a module by `vitest.config.ts` (for `exclude`) and as a CLI by the
 * `test:unit` script, so the two can never disagree again.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

/** Roots that may hold tests. `tests_e2e` is Playwright's and is never scanned. */
const ROOTS = ["src", "tests_unit"];

const TEST_FILE = /\.test\.(ts|tsx|js|jsx|mjs)$/;

/** `import ... from "node:test"` / `require("node:test")`, either quote style. */
const NODE_TEST_IMPORT = /(?:from|require\()\s*["']node:test["']/;

function* walk(dir) {
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch {
    return; // a root that does not exist is not an error
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

/**
 * Every test file under `root` written against Node's test runner, as paths
 * relative to `root` with forward slashes (what vitest's `exclude` expects).
 */
export function nodeTestFiles(root) {
  const found = [];
  for (const dir of ROOTS) {
    for (const file of walk(join(root, dir))) {
      let source;
      try {
        source = readFileSync(file, "utf8");
      } catch {
        continue;
      }
      if (NODE_TEST_IMPORT.test(source)) {
        found.push(relative(root, file).split(sep).join("/"));
      }
    }
  }
  return found.sort();
}

// CLI: run exactly those files through Node's own runner. Files named on the
// command line (paths relative to the frontend directory) narrow the run to
// them, so one node:test file can be run alone (the sandbox's
// frontend-node-unit check passes its args here).
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const root = join(fileURLToPath(new URL(".", import.meta.url)), "..");
  const requested = process.argv.slice(2);
  const files = requested.length > 0 ? requested : nodeTestFiles(root);
  if (files.length === 0) {
    console.log("No node:test files found — nothing for Node's runner to do.");
    process.exit(0);
  }
  const { spawnSync } = await import("node:child_process");
  const result = spawnSync(process.execPath, ["--test", ...files], {
    cwd: root,
    stdio: "inherit",
  });
  process.exit(result.status ?? 1);
}
