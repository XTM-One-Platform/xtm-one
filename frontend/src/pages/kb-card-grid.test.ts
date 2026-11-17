/**
 * Knowledge base card grid minimum width - `yarn test`.
 *
 * The knowledge base cards hold a title, up to three badges and a row of
 * chips. On viewport breakpoints (`lg:grid-cols-3`) the grid kept three
 * columns at 1024 even with the sidebar open, and titles shrank to one
 * letter (#9683). The grid now follows the width it is given: auto-fill
 * columns of at least CARD_MIN_REM, so a column drops before a card gets
 * narrower than its content.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as list-toolbar-compact.test.ts.
const SOURCE = readFileSync(join(process.cwd(), "src", "pages", "KnowledgeBases.tsx"), "utf8");
const CARD_MIN_REM = 22;

test("the knowledge base card grid sets a minimum card width", () => {
  const grid = SOURCE.match(/className="([^"]*)"\s+data-testid="kb-card-grid"/);
  assert.ok(grid, "the card grid carries data-testid=\"kb-card-grid\"");
  const min = grid[1].match(/grid-cols-\[repeat\(auto-fill,minmax\((\d+(?:\.\d+)?)rem,1fr\)\)\]/);
  assert.ok(min, `the card grid uses auto-fill columns with a rem minimum, got "${grid[1]}"`);
  assert.ok(Number(min[1]) >= CARD_MIN_REM, `card minimum ${min[1]}rem is under ${CARD_MIN_REM}rem`);
  assert.doesNotMatch(grid[1], /\b(?:sm|md|lg|xl):grid-cols-/, "no viewport breakpoint picks the column count");
});

test("the document count chip never wraps", () => {
  assert.match(SOURCE, /<Badge variant="outline" className="content-caption shrink-0 whitespace-nowrap">\s*<FileText/);
});
