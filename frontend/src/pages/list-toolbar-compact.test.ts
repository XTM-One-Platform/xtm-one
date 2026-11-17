/**
 * One compact rule for the library list toolbars - `yarn test`.
 *
 * Below 1280 px the visibility segments of Agents, Knowledge, Templates,
 * Presentations and Integrations show their icon and count only, so the search, the segments and
 * the filter pills hold on one row at 1024. Presentations was once
 * alone on that rule, through a page-local media query, while the three other
 * pages wrapped. This gate keeps the list pages on the one shared hook:
 * - each page calls `useCompactListToolbar()` and passes its result to the
 *   toolbar `VisibilityFilter` as `iconOnly`;
 * - no page under src/pages declares its own `(max-width: 1279px)` query.
 * A new library list page with visibility segments in its toolbar is added to
 * LIST_PAGES.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";

import { LIST_COMPACT_TOOLBAR_QUERY } from "@/hooks/useCompactToolbar";

// vitest runs from frontend/ (CI and `yarn test`), as list-toolbar-search.test.ts.
const PAGES = join(process.cwd(), "src", "pages");

/** Library list pages whose toolbar row holds the visibility segments, relative to src/pages. */
// Agents, Integrations, Knowledge bases and Presentations are on the data table standard v2:
// the shared DataTable toolbar draws their segments, icon-only under its own compact width
// (data-table-gates.test.ts).
const LIST_PAGES = ["Templates.tsx"];

function* sourceFiles(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) yield* sourceFiles(full);
    else if (entry.name.endsWith(".tsx") && !entry.name.endsWith(".test.tsx")) yield full;
  }
}

test("the shared list query is the 1280 breakpoint", () => {
  assert.equal(LIST_COMPACT_TOOLBAR_QUERY, "(max-width: 1279px)");
});

test("every library list page passes iconOnly from useCompactListToolbar", () => {
  const missing: string[] = [];
  for (const page of LIST_PAGES) {
    const source = readFileSync(join(PAGES, page), "utf8");
    const hook = source.match(/const (\w+) = useCompactListToolbar\(\);/);
    const filters = [...source.matchAll(/<VisibilityFilter\b([\s\S]*?)\/>/g)].map((match) => match[1]);
    assert.ok(filters.length > 0, `${page} is listed but renders no VisibilityFilter`);
    if (!hook || !filters.every((attributes) => attributes.includes(`iconOnly={${hook[1]}}`))) missing.push(page);
  }
  assert.deepEqual(missing, [], `list pages not on the shared compact toolbar: ${missing.join(", ")}`);
});

test("the Integrations list draws its toolbar through the shared records table (standard v2)", () => {
  // The search, segments, Tags, Category pill, total and view toggle are the
  // DataTable toolbar's, in its one order: the page draws no toolbar row of its own.
  const source = readFileSync(join(PAGES, "Integrations.tsx"), "utf8");
  assert.doesNotMatch(source, /data-testid="integrations-toolbar"/);
  assert.match(source, /<EntityTable[\s\S]*?search=\{\{/);
  assert.match(source, /scope=\{\{ value: visibilityFilter/);
});

test("the Agents list draws its toolbar through the shared records table in list and grid (standard v2)", () => {
  // Search, segments, Properties, Tags, the fleet pill, total and view toggle are the
  // DataTable toolbar's; the grid view is the table's grid render mode, under the same toolbar.
  const source = readFileSync(join(PAGES, "Agents.tsx"), "utf8");
  assert.doesNotMatch(source, /<SearchInput\b/);
  assert.doesNotMatch(source, /<VisibilityFilter\b/);
  assert.match(source, /<EntityTable[\s\S]*?search=\{\{/);
  assert.match(source, /scope=\{\{ value: visibilityFilter/);
  assert.match(source, /renderGrid=\{/);
  assert.match(source, /totalAll=\{allAgents\.length\}/);
});

test("the Templates toolbar never wraps below 1280 and lets the search shrink to its floor", () => {
  // In fr at 1024 the search (248), segments (220), kind picker (192), Tags (83)
  // and the count needed more than the 780 px row, so the count wrapped (UX r2
  // drift note on #10207): the row is flex-nowrap when compact, the search gives
  // way first and the kind picker never shrinks.
  const source = readFileSync(join(PAGES, "Templates.tsx"), "utf8");
  const row = source.match(/data-testid="templates-toolbar" className=\{([^\n]*)\}>/);
  assert.ok(row, "Templates.tsx renders no templates-toolbar row");
  assert.match(row[1], /compactToolbar \? "flex-nowrap" : "flex-wrap"/);
  assert.match(source, /<SearchInput toolbar compact=\{compactToolbar\}/);
  assert.match(source, /className="h-8 w-48 shrink-0"/);
});

test("the integrations catalog toolbar never wraps below 1280 and lets the search shrink to its floor", () => {
  // At 1024 the search (220 px floor), Sort (176) and the count (83) needed 503 px
  // in a 500 px row, so the count wrapped (UX r1 B1 on #10207): the row is
  // flex-nowrap when compact, the search is a toolbar search that gives way first,
  // and Sort and the count never shrink.
  const source = readFileSync(join(PAGES, "..", "components", "integrations", "integration-catalog-browser.tsx"), "utf8");
  assert.match(source, /const compactToolbar = useCompactListToolbar\(\);/);
  const row = source.match(/data-testid="catalog-toolbar"\s+className=\{([^\n]*)\}/);
  assert.ok(row, "integration-catalog-browser.tsx renders no catalog-toolbar row");
  assert.match(row[1], /compactToolbar \? "flex-nowrap" : "flex-wrap"/);
  assert.match(source, /<SearchInput\s+toolbar\s+compact=\{compactToolbar\}/);
  assert.doesNotMatch(source, /min-w-\[220px\]/);
  assert.match(source, /<SelectTrigger className="[^"]*\bshrink-0\b[^"]*" aria-label=\{t_i18n\("Sort"\)\}/);
});

test("no page declares its own copy of the list compact query", () => {
  const copies: string[] = [];
  for (const file of sourceFiles(PAGES)) {
    if (readFileSync(file, "utf8").includes('"(max-width: 1279px)"')) {
      copies.push(relative(PAGES, file).split(sep).join("/"));
    }
  }
  assert.deepEqual(copies, [], `pages with a page-local 1279px query: ${copies.join(", ")}`);
});
