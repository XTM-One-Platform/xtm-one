/**
 * One selection box for every selectable list - `yarn test`.
 *
 * The rows and the control bar of the Operations lists (Work Items, Executions,
 * Background Tasks, Shared State) and the Video Studio library select with the
 * shared `SelectionBox` (components/ui/list-control-bar.tsx), so a row box and
 * the header "Select all" box above it are drawn by one implementation and read
 * the same in both themes. A hand-rolled `role="checkbox"` button with
 * its own `h-4 w-4` box under src/pages or src/components split that column
 * in two: it must not come back. The integration
 * catalog facets were the last one under src/components.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as no-hand-rolled-menu.test.ts.
const SRC = join(process.cwd(), "src");
const ROOTS = ["pages", "components"];
const ALLOWED = new Set(["components/ui/list-control-bar.tsx"]);

function* sourceFiles(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "node_modules") yield* sourceFiles(full);
    } else if (/\.tsx$/.test(entry.name) && !/\.test\.tsx$/.test(entry.name)) {
      yield full;
    }
  }
}

// A JSX element opening with role="checkbox" whose own attributes carry an h-4 w-4 box.
const HAND_ROLLED = /<button\b(?:(?!<\/?button\b)[\s\S]){0,600}?role="checkbox"(?:(?!<\/?button\b)[\s\S]){0,600}?\bh-4 w-4\b/;

test("the pattern catches a hand-rolled box and spares the shared one", () => {
  const copy = `<button\n  type="button"\n  role="checkbox"\n  aria-checked={selected}\n  className={cn("h-4 w-4 rounded-[3px] border")}\n/>`;
  assert.match(copy, HAND_ROLLED);
  assert.doesNotMatch(`<SelectionBox checked={selected} onToggle={toggle} label="Select run" />`, HAND_ROLLED);
});

test("no list under src/pages or src/components hand-rolls its selection box", () => {
  const offenders: string[] = [];
  for (const root of ROOTS) {
    for (const file of sourceFiles(join(SRC, root))) {
      const path = relative(SRC, file).split(sep).join("/");
      if (!ALLOWED.has(path) && HAND_ROLLED.test(readFileSync(file, "utf8"))) offenders.push(path);
    }
  }
  assert.deepEqual(offenders, [], `hand-rolled selection box in: ${offenders.join(", ")}; use SelectionBox`);
});
