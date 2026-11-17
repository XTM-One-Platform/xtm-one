/**
 * The /integrations category filter is kept in the URL - `yarn test`.
 *
 * Design standards s.3 (state kept in the URL): the search, tags and
 * visibility segment of /integrations were in the query, the category was
 * page state, so a reload or a shared link lost it (UX r1 A2 on #10152).
 * The category is read from `?cat=` and written through the shared
 * useEntityFilter `writeParams`, never through a local useState or a second
 * setSearchParams that would undo a filter write of the same tick.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as list-toolbar-compact.test.ts.
const source = readFileSync(join(process.cwd(), "src", "pages", "Integrations.tsx"), "utf8");

test("the integrations category is read from the URL, not page state", () => {
  assert.match(source, /const CATEGORY_PARAM = "cat";/);
  assert.match(source, /searchParams\.get\(CATEGORY_PARAM\)/);
  assert.doesNotMatch(source, /useState<string \| null>\(null\)/);
});

test("the integrations category is written through the shared writeParams", () => {
  assert.match(source, /writeParams\(\(prev\) => \{[\s\S]*?next\.set\(CATEGORY_PARAM, category\)/);
  assert.match(source, /onChange=\{setCategoryFilter\}/);
});
