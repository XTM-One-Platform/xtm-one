/**
 * One compact rule for the Operations toolbars - `yarn test`.
 *
 * Below 1440 px the visibility pills of every Operations tab show their icon
 * and count only. Shared state and
 * Worksets (both on the shared DataTable since slices 8b and 8c) once drew the labelled pills (429 px at 1024 in fr) beside
 * Executions, Background tasks and Work items (220 px). This gate keeps the
 * five tabs on the one shared hook: each calls `useCompactToolbar()` and passes
 * its result to every `VisibilityFilter` it renders as `iconOnly`.
 * A new Operations tab with visibility pills is added to OPERATIONS_TABS.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";

// vitest runs from frontend/ (CI and `yarn test`), as list-toolbar-compact.test.ts.
const PAGES = join(process.cwd(), "src", "pages");

/** The Operations tabs whose toolbar holds the visibility pills, relative to src/pages. */
// TaskHistory.tsx (8f) and BackgroundTasks.tsx (8e) left in #9378 slice 8: their visibility is the DataTable `scope`.
// Worksets (8b), Shared state (8c) and Work items (8d) are on the shared DataTable too: every Operations tab's
// scope pills take the table's compact toolbar, so the list is empty until a tab draws its own VisibilityFilter.
const OPERATIONS_TABS: string[] = [];

test("every Operations tab passes iconOnly from useCompactToolbar", () => {
  const missing: string[] = [];
  for (const page of OPERATIONS_TABS) {
    const source = readFileSync(join(PAGES, page), "utf8");
    const hook = source.match(/const (\w+) = useCompactToolbar\(\);/);
    const filters = [...source.matchAll(/<VisibilityFilter\b([\s\S]*?)\/>/g)].map((match) => match[1]);
    assert.ok(filters.length > 0, `${page} is listed but renders no VisibilityFilter`);
    if (!hook || !filters.every((attributes) => attributes.includes(`iconOnly={${hook[1]}}`))) missing.push(page);
  }
  assert.deepEqual(missing, [], `Operations tabs not on the shared compact toolbar: ${missing.join(", ")}`);
});

// The default Work items status filter is a state, never the verb "Open".
test("the Work items Open status filter reads a state in every language", () => {
  // The Status pill of the toolbar (slice 8d) names the open status with the adjective's key.
  const source = readFileSync(join(PAGES, "operations/WorkItemStatusFilter.tsx"), "utf8");
  assert.match(source, /open: "Open \(status\)"/);
  const catalogs = { en, es, fr, ja } as Record<string, Record<string, string>>;
  for (const [locale, catalog] of Object.entries(catalogs)) {
    const state = catalog["Open (status)"];
    assert.ok(state, `${locale} has no "Open (status)"`);
    if (locale !== "en") assert.notEqual(state, catalog.Open, `${locale} translates the Open state as the verb`);
  }
});
