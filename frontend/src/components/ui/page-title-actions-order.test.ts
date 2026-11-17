/**
 * One order for the actions beside a page title - `yarn test`.
 *
 * Rule (dev-docs/reference/frontend-design-system.md, "Page-title actions"): the secondary actions come first and the
 * filled primary action sits at the right end of the labelled actions, as on
 * Presentations (Import, then New presentation). Icon-only controls (enable,
 * delete) may follow it. Before this guard the Fleets header put its filled
 * "New fleet" first and Presentations put it last.
 *
 * The check is static, as `filter-pill-class.test.ts`: it reads every page's
 * title actions (an `EntityPageHeader` or `ListPageHeader` `actions` slot, the
 * row of a list page's `text-3xl` title, what a tab renders through
 * `FlowsHeaderActions`)
 * and fails when a labelled `Button` that is not filled follows a filled one.
 * A `Button` is filled when it has no `variant` or its `variant` can be
 * "default"; it is labelled when its children call `t_i18n`.
 *
 * Known limits:
 * - The row of a `text-3xl` title is read up to its first blank line: an action
 *   placed after a blank line inside that row is not checked.
 * - The check reads the `variant` prop only: a filled `Button` that renders grey
 *   at runtime (the agent detail "Chat with Agent" of a `disable_chat` agent) is
 *   still counted as filled.
 *
 * A second test pins one gap token (`gap-3`) on the list-page title action rows
 * of Fleets and of `ListPageHeader` (Presentations draws it since #9301 N5).
 *
 * A third test keeps one button height in the title actions of Agents (its
 * `ListPageHeader` slot since #9301 N4) and Presentations (its slot since N5):
 * a labelled `Button` there takes the default size, never `size=` (the Agents
 * Import rendered 24 px beside the 36 px "New Agent").
 */
import { expect, test } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as filter-pill-class.test.ts.
const PAGES = join(process.cwd(), "src", "pages") + sep;

function* sourceFiles(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) yield* sourceFiles(full);
    else if (entry.name.endsWith(".tsx") && !/\.test\.tsx$/.test(entry.name)) yield full;
  }
}

/** The index just past the brace that closes the one opened before `from`. */
function closeBrace(source: string, from: number): number {
  let depth = 1;
  let i = from;
  while (depth > 0 && i < source.length) {
    const ch = source[i];
    if (ch === "{") depth += 1;
    else if (ch === "}") depth -= 1;
    i += 1;
  }
  return i;
}

/** The index of the `>` that ends a JSX opening tag starting at `from`. */
function tagEnd(source: string, from: number): number {
  let depth = 0;
  let quote: string | null = null;
  for (let i = from; i < source.length; i += 1) {
    const ch = source[i];
    if (quote) {
      if (ch === quote) quote = null;
    } else if (ch === '"' || ch === "'" || ch === "`") {
      quote = ch;
    } else if (ch === "{") {
      depth += 1;
    } else if (ch === "}") {
      depth -= 1;
    } else if (ch === ">" && depth === 0) {
      return i;
    }
  }
  return source.length;
}

type TitleAction = { filled: boolean; labelled: boolean; sized: boolean };

/** The `Button`s of a JSX fragment, in source order. */
function titleActions(fragment: string): TitleAction[] {
  const actions: TitleAction[] = [];
  const opening = /<Button\b/g;
  let match: RegExpExecArray | null;
  while ((match = opening.exec(fragment))) {
    const end = tagEnd(fragment, match.index + match[0].length);
    const props = fragment.slice(match.index, end);
    const close = fragment.indexOf("</Button>", end);
    const children = props.endsWith("/") || close < 0 ? "" : fragment.slice(end, close);
    const variant = /\bvariant=("[^"]*"|\{[^}]*\})/.exec(props);
    actions.push({
      filled: variant === null || variant[1].includes('"default"'),
      labelled: children.includes("t_i18n("),
      sized: /\bsize=/.test(props),
    });
  }
  return actions;
}

/** True when no labelled secondary action follows a labelled filled one. */
function primaryLast(actions: TitleAction[]): boolean {
  let filledSeen = false;
  for (const action of actions.filter((a) => a.labelled)) {
    if (action.filled) filledSeen = true;
    else if (filledSeen) return false;
  }
  return true;
}

/** The row of each list page's `text-3xl` title, up to its first blank line. */
function listTitleRows(source: string): string[] {
  const rows: string[] = [];
  for (const title of source.matchAll(/<h1 className="text-3xl[^"]*"/g)) {
    const start = (title.index ?? 0) + title[0].length;
    const blank = source.indexOf("\n\n", start);
    rows.push(source.slice(start, blank < 0 ? undefined : blank));
  }
  return rows;
}

/** Every title-actions fragment of a page source. */
function titleActionFragments(source: string): string[] {
  const fragments: string[] = [];
  for (const header of source.matchAll(/<(EntityPageHeader|ListPageHeader)\b/g)) {
    // The header's own `actions` prop: the first one before the next header.
    const next = source.indexOf(`<${header[1]}`, (header.index ?? 0) + 1);
    const slot = source.indexOf("actions={", header.index);
    if (slot >= 0 && (next < 0 || slot < next)) {
      const start = slot + "actions={".length;
      fragments.push(source.slice(start, closeBrace(source, start)));
    }
  }
  fragments.push(...listTitleRows(source));
  for (const slot of source.matchAll(/<FlowsHeaderActions>([\s\S]*?)<\/FlowsHeaderActions>/g)) {
    fragments.push(slot[1]);
    // A component the slot renders, declared in the same file (CreateFleetActions).
    for (const used of slot[1].matchAll(/<([A-Z]\w*)\b/g)) {
      const decl = source.indexOf(`function ${used[1]}(`);
      if (decl < 0) continue;
      const body = source.indexOf("return (", decl);
      const end = source.indexOf("\n}\n", body);
      if (body >= 0) fragments.push(source.slice(body, end < 0 ? undefined : end));
    }
  }
  return fragments;
}

test("the guard reads a filled action before a secondary one as inverted", () => {
  const inverted = `<Button onClick={go}>{t_i18n("New fleet")}</Button>
    <Button variant="secondary" onClick={prompt}>{t_i18n("Create with a prompt")}</Button>`;
  const settled = `<Button variant="outline" onClick={pick}>{t_i18n("Import")}</Button>
    <Button className="gap-1.5" onClick={start}>{t_i18n("New presentation")}</Button>
    <Button variant="ghost" aria-label={t_i18n("Delete")}><Trash2 /></Button>`;
  expect(primaryLast(titleActions(inverted))).toBe(false);
  expect(primaryLast(titleActions(settled))).toBe(true);
  expect(titleActions(`<Button variant={on ? "secondary" : "ghost"}>{t_i18n("Packages")}</Button>`)).toEqual([
    { filled: false, labelled: true, sized: false },
  ]);
  expect(titleActions(`<Button variant={builtin ? "default" : "outline"}>{t_i18n("Duplicate")}</Button>`)).toEqual([
    { filled: true, labelled: true, sized: false },
  ]);
  expect(titleActions(`<Button variant="ghost" size="sm">{t_i18n("Import")}</Button>`)).toEqual([
    { filled: false, labelled: true, sized: true },
  ]);
});

test("every page puts its filled title action at the right end (design-system doc, Page-title actions)", () => {
  const inverted: string[] = [];
  for (const full of sourceFiles(PAGES)) {
    const source = readFileSync(full, "utf8");
    if (titleActionFragments(source).some((fragment) => !primaryLast(titleActions(fragment)))) {
      inverted.push(full.slice(PAGES.length).split(sep).join("/"));
    }
  }
  expect(inverted, "a page puts a secondary title action after its filled one: move the filled action to the right end").toEqual([]);
});

test("Fleets and Presentations space their title actions with one gap token (gap-3)", () => {
  const fleets = readFileSync(join(PAGES, "flows", "FleetsTab.tsx"), "utf8");
  const createActions = fleets.slice(fleets.indexOf("function CreateFleetActions("));
  const fleetsRow = /"flex flex-wrap items-center justify-center (gap-[\d.]+)" : "flex flex-wrap items-center justify-end (gap-[\d.]+)"/.exec(createActions);
  expect(fleetsRow?.slice(1)).toEqual(["gap-3", "gap-3"]);

  const presentations = readFileSync(join(PAGES, "Presentations.tsx"), "utf8");
  expect(presentations.includes("<ListPageHeader"), "Presentations draws the shared header").toBe(true);
  const header = readFileSync(join(process.cwd(), "src", "components", "ui", "list-page-header.tsx"), "utf8");
  const actionsRow = /\{actions \? <div className="flex items-center (gap-[\d.]+)">/.exec(header);
  expect(actionsRow?.[1]).toBe("gap-3");
});

test("Agents and Presentations keep one button height in their title row", () => {
  for (const page of ["Agents.tsx", "Presentations.tsx"]) {
    const rows = titleActionFragments(readFileSync(join(PAGES, page), "utf8"));
    const labelled = rows.flatMap((row) => titleActions(row)).filter((a) => a.labelled);
    expect(labelled.length, `${page}: no labelled title action read`).toBeGreaterThanOrEqual(2);
    expect(labelled.filter((a) => a.sized), `${page}: a labelled title action sets size=`).toEqual([]);
  }
});
