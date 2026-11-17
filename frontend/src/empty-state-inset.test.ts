/**
 * An empty state inside a card is inset - `yarn test`.
 *
 * `<EmptyState>` draws its own dashed card by default. Inside a `<CardContent>`
 * that draws a card in a card, so it must pass `variant="inset"`. This
 * guard reads every .tsx file under src/pages and src/components and fails an
 * `<EmptyState>` opened while a `<CardContent>` is still open without
 * `variant="inset"`.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as no-hand-rolled-menu.test.ts.
const SRC = join(process.cwd(), "src");
const SCANNED = [join(SRC, "pages"), join(SRC, "components")];

function* tsxFiles(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      yield* tsxFiles(full);
    } else if (entry.name.endsWith(".tsx") && !/\.test\.tsx$/.test(entry.name)) {
      yield full;
    }
  }
}

/** The `<EmptyState ...>` tags opened inside an open `<CardContent>` without `variant="inset"`, as line numbers. */
function emptyStatesNotInset(source: string): number[] {
  const offenders: number[] = [];
  const tags = /<CardContent\b[^>]*?(\/)?>|<\/CardContent>|<EmptyState\b/g;
  let depth = 0;
  for (let match = tags.exec(source); match; match = tags.exec(source)) {
    const token = match[0];
    if (token.startsWith("</")) {
      depth = Math.max(0, depth - 1);
    } else if (token.startsWith("<CardContent")) {
      if (!match[1]) depth += 1;
    } else if (depth > 0) {
      const end = source.indexOf("/>", match.index);
      const tag = source.slice(match.index, end === -1 ? undefined : end);
      if (!/\bvariant="inset"/.test(tag)) {
        offenders.push(source.slice(0, match.index).split("\n").length);
      }
    }
  }
  return offenders;
}

test("the scanner flags a card in a card and passes an inset one", () => {
  const bad = '<Card>\n<CardContent className="p-0">\n<EmptyState icon={X} title="t" description="d" />\n</CardContent>\n</Card>';
  const good = '<CardContent>\n<EmptyState\n  variant="inset"\n  icon={X}\n  title="t"\n  description="d"\n/>\n</CardContent>';
  const outside = '<CardContent>x</CardContent>\n<EmptyState icon={X} title="t" description="d" />';
  assert.deepEqual(emptyStatesNotInset(bad), [3]);
  assert.deepEqual(emptyStatesNotInset(good), []);
  assert.deepEqual(emptyStatesNotInset(outside), []);
});

test("every EmptyState inside a CardContent of src/pages and src/components is inset", () => {
  const offenders: string[] = [];
  let scanned = 0;
  for (const dir of SCANNED) {
    for (const file of tsxFiles(dir)) {
      scanned += 1;
      for (const line of emptyStatesNotInset(readFileSync(file, "utf8"))) {
        offenders.push(`${relative(SRC, file).split(sep).join("/")}:${line}`);
      }
    }
  }
  assert.ok(scanned > 0, "the guard scanned no file: check the working directory");
  assert.deepEqual(offenders, [], `EmptyState without variant="inset" inside a CardContent: ${offenders.join(", ")}`);
});
