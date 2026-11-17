// Regression gate: list pages draw ownership and kind quick filters
// through the shared VisibilityFilter / SegmentedFilter, never with page-local
// toggle or radio pills. A page that re-inlines Mine / Shared / Company buttons,
// or a `*FilterPills` component, fails here.

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// vitest runs from frontend/ (jsdom gives import.meta.url no file: scheme).
const PAGES = join(process.cwd(), "src", "pages");

/** Every page source, tests left out. */
function pageSources(): { path: string; text: string }[] {
  return (readdirSync(PAGES, { recursive: true }) as string[])
    .filter((path) => path.endsWith(".tsx") && !path.includes(".test."))
    .map((path) => ({ path, text: readFileSync(join(PAGES, path), "utf8") }));
}

const OWNERSHIP_LABEL = /t_i18n\("(Mine|Shared|My Runs|Shared Runs|Shared with me|My tasks|All users)"\)/;
const LOCAL_PILL = /aria-pressed=|role="radio"/;

describe("list page quick filters", () => {
  const pages = pageSources();

  it("reads the pages", () => {
    expect(pages.length).toBeGreaterThan(50);
  });

  it("no page draws its own ownership pills", () => {
    const offenders = pages
      .filter(({ text }) => LOCAL_PILL.test(text) && OWNERSHIP_LABEL.test(text))
      .map(({ path }) => path);
    expect(offenders).toEqual([]);
  });

  it("no page defines its own filter pill component", () => {
    const offenders = pages.filter(({ text }) => /function \w*FilterPills\b[\s\S]*?aria-pressed=/.test(text)).map(({ path }) => path);
    expect(offenders).toEqual([]);
  });
});
