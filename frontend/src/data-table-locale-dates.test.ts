/**
 * Dates in the user's locale on the shared DataTable - `yarn test`.
 *
 * UX r3 B3 on #10581 (design standards s.3, "dates in the user's locale"): the
 * Users and Groups tables wrote their Created column with `formatDateTime`,
 * hard-coded to en-US, so a French UI read "Nov 10, 2026, 5:08 AM". A page that
 * renders the shared DataTable formats its dates through the active locale
 * (`useFormatter().intl.formatDate`), never through `formatDateTime`.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`).
const ROOT = process.cwd();
const PAGES = join(ROOT, "src/pages");

function sources(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) out.push(...sources(path));
    else if (name.endsWith(".tsx") && !name.endsWith(".test.tsx")) out.push(path);
  }
  return out;
}

test("a DataTable page formats its dates in the user's locale", () => {
  const offenders = sources(PAGES)
    .filter((path) => {
      const source = readFileSync(path, "utf8");
      return source.includes("@/components/ui/data-table") && /\bformatDateTime\(/.test(source);
    })
    .map((path) => relative(ROOT, path));
  assert.deepEqual(offenders, [], `use useFormatter().intl.formatDate instead of formatDateTime in: ${offenders.join(", ")}`);
});
