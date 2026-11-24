/**
 * Every filter lists its options alphabetically - `yarn test`.
 *
 * Sam, 16 Nov 2026 (#12289): "In all filters list like this, can we ensure it's
 * sorted alphabetically?" The rule lives in lib/select-options.ts
 * (sortFilterOptions, sortByLabel) and the shared filters apply it by default:
 * DataTable's filter pills, CategoryFilter, PropertyFilter, TagFilter,
 * MultiSelect and SearchableSelect. Two guards keep it that way:
 *   - a file that draws its own filter panel (`<FilterPillPanel`) orders its
 *     options through lib/select-options, or is listed in OWN_ORDER with why;
 *   - a DataTable filter that keeps its order (`ordered: true`) says why on the
 *     line above: an ordinal scale only (severity, priority, criticality,
 *     confidence, a time range).
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as no-direct-lucide-import.test.ts.
const ROOT = process.cwd();
const SELF = "src/filter-options-sorted.test.ts";

/** Panels whose content is not a list of options to sort, or a lifecycle scale; each says why. */
const OWN_ORDER: Record<string, string> = {
  "src/components/ui/filter-pill.tsx": "defines FilterPillPanel itself",
  "src/components/audit-logs/AuditLogList.tsx": "the panel holds a date range, no option list",
  "src/components/fleets/FleetMoreFilters.tsx": "its lists are SearchableSelect, sorted by the select",
  "src/pages/VideoStudio.tsx": "a segmented usage switch and a date range, no option list",
  "src/pages/operations/OperationsStatusFilter.tsx": "statuses in the order of a run's life (ordinal)",
  "src/pages/operations/WorkItemStatusFilter.tsx": "statuses in the order of an item's life (ordinal)",
  "src/pages/xtm-one/ctem/CasesGroupByPill.tsx": "a single grouping choice, No grouping first, not a filter list",
  "src/pages/xtm-one/ctem/CasesViewsPill.tsx": "the user's saved views in the order they were saved, not a filter list",
};

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) out.push(...sourceFiles(path));
    else if (/\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name)) out.push(relative(ROOT, path).split(sep).join("/"));
  }
  return out;
}

const FILES = sourceFiles(join(ROOT, "src"));

test("a hand-built filter panel orders its options through lib/select-options", () => {
  const offenders = FILES.filter((file) => {
    if (file in OWN_ORDER) return false;
    const source = readFileSync(join(ROOT, file), "utf8");
    return source.includes("<FilterPillPanel") && !source.includes('from "@/lib/select-options"');
  });
  assert.deepEqual(
    offenders,
    [],
    `Sort the panel's options with sortFilterOptions or sortByLabel (lib/select-options), or list the file in OWN_ORDER with why (${SELF}):\n${offenders.join("\n")}`,
  );
});

test("OWN_ORDER names files that exist and still draw a panel", () => {
  const stale = Object.keys(OWN_ORDER).filter((file) => !FILES.includes(file) || !readFileSync(join(ROOT, file), "utf8").includes("FilterPillPanel"));
  assert.deepEqual(stale, [], `Drop these OWN_ORDER entries (${SELF})`);
});

/** `ordered: true` with the line above it, so the reason is read where the order is kept. */
const ORDERED = /^(.*)\n\s*ordered: true,?\s*$/gm;

test("every filter that keeps its order says it is an ordinal scale on the line above", () => {
  const offenders: string[] = [];
  for (const file of FILES) {
    const source = readFileSync(join(ROOT, file), "utf8");
    for (const match of source.matchAll(ORDERED)) {
      if (!/\/\/.*\bordinal\b/i.test(match[1])) offenders.push(`${file}: ${match[1].trim()}`);
    }
  }
  assert.deepEqual(offenders, [], `Write '// Ordinal scale: <the order>' above 'ordered: true', or drop it (${SELF}):\n${offenders.join("\n")}`);
});
