/**
 * One search width on every list page - `yarn test`.
 *
 * The width of a list-page toolbar search is owned by `SearchInput`'s
 * `toolbar` prop (components/ui/search-input.tsx). Each page once
 * set its own width class on the field (`min-w-40 flex-1`, `min-w-32
 * flex-1`, or nothing), which gave three widths across list pages. This gate
 * keeps it from coming back:
 * - every list page below passes `toolbar` to its toolbar `SearchInput`;
 * - no `SearchInput` that passes `toolbar`, anywhere under src/pages, carries a
 *   width class of its own (`flex-1`, `min-w-*`, `w-*`, `max-w-*`).
 * A new list page with a search in its toolbar row is added to LIST_PAGES.
 * A page on the shared `DataTable` (Users, Groups, Integrations, Agents, Prompts, MCP servers,
 * Investigation packs, Skills, Variables, Knowledge bases, Dashboards, Tools, Flows, Presentations, Video studio) is not listed: the table toolbar owns its search field.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as no-hand-rolled-menu.test.ts.
const PAGES = join(process.cwd(), "src", "pages");

/** List pages whose toolbar row holds a search, relative to src/pages. */
const LIST_PAGES = [
  "BrowserProfiles.tsx",
  "Datasets.tsx",
  "Evaluations.tsx",
  "Files.tsx",
  "Templates.tsx",
];

const WIDTH_CLASS = /(^|\s)(flex-1|min-w-\S+|w-\S+|max-w-\S+)(\s|$)/;

function* sourceFiles(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) yield* sourceFiles(full);
    else if (entry.name.endsWith(".tsx")) yield full;
  }
}

/** Every `<SearchInput ... />` element of a source, with its attributes. */
function searchInputs(source: string): string[] {
  return [...source.matchAll(/<SearchInput\b([\s\S]*?)\/>/g)].map((match) => match[1]);
}

const hasToolbar = (attributes: string) => /(^|\s)toolbar(\s|=\{true\}|$)/.test(attributes);

function classNameOf(attributes: string): string {
  const match = attributes.match(/className=(?:"([^"]*)"|\{\s*"([^"]*)"\s*\})/);
  return match ? (match[1] ?? match[2] ?? "") : "";
}

test("every list page passes `toolbar` to its toolbar search", () => {
  const missing: string[] = [];
  for (const page of LIST_PAGES) {
    const inputs = searchInputs(readFileSync(join(PAGES, page), "utf8"));
    assert.ok(inputs.length > 0, `${page} is listed but renders no SearchInput`);
    if (!inputs.some(hasToolbar)) missing.push(page);
  }
  assert.deepEqual(missing, [], `list pages without <SearchInput toolbar>: ${missing.join(", ")}`);
});

test("no toolbar search sets a width class of its own", () => {
  const offenders: string[] = [];
  for (const file of sourceFiles(PAGES)) {
    for (const attributes of searchInputs(readFileSync(file, "utf8"))) {
      if (!hasToolbar(attributes)) continue;
      if (/className=\{(?!\s*")/.test(attributes) || WIDTH_CLASS.test(classNameOf(attributes))) {
        offenders.push(relative(PAGES, file).split(sep).join("/"));
      }
    }
  }
  assert.deepEqual(offenders, [], `toolbar searches with their own width: ${offenders.join(", ")}`);
});
