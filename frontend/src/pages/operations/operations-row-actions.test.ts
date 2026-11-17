/**
 * One row action on every Operations tab - `yarn test`.
 *
 * Shared State once drew its delete as a raw `<button>` (24 px, hidden until
 * hover, the browser's default focus outline) beside Worksets' design-system
 * Button (28 px, always visible). This gate reads the `actions` slot of every
 * `OpsRowGrid` under pages/operations and fails on a raw `<button>` there:
 * an icon action is an `OpsRowAction` (components/ui/operations-list.tsx), a
 * labelled one the design-system Button.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as operations-toolbar-compact.test.ts.
const OPERATIONS = join(process.cwd(), "src", "pages", "operations");

/** The index just past the brace that closes the one opened before `start`. */
function closeBrace(source: string, start: number): number {
  let depth = 1;
  for (let i = start; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    else if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) return i;
    }
  }
  return source.length;
}

/** The `actions={...}` slot of every `<OpsRowGrid` in a source file. */
function rowActionSlots(source: string): string[] {
  const slots: string[] = [];
  for (const grid of source.matchAll(/<OpsRowGrid\b/g)) {
    const slot = source.indexOf("actions={", grid.index);
    if (slot < 0) continue;
    const start = slot + "actions={".length;
    slots.push(source.slice(start, closeBrace(source, start)));
  }
  return slots;
}

test("the slot reader finds the actions of a row and stops at its brace", () => {
  const source = `<OpsRowGrid lead={<A />} actions={<>{x && <OpsRowAction label="a" />}<button>b</button></>} />\n<p>{"<button>"}</p>`;
  const [slot] = rowActionSlots(source);
  assert.match(slot, /OpsRowAction/);
  assert.match(slot, /<button>b<\/button>/);
  assert.doesNotMatch(slot, /<p>/);
});

test("no Operations row draws a raw <button> in its actions column", () => {
  const offenders: string[] = [];
  let grids = 0;
  for (const file of readdirSync(OPERATIONS).filter((name) => name.endsWith(".tsx") && !name.includes(".test."))) {
    const slots = rowActionSlots(readFileSync(join(OPERATIONS, file), "utf8"));
    grids += slots.length;
    if (slots.some((slot) => /<button\b/.test(slot))) offenders.push(file);
  }
  assert.ok(grids >= 6, `expected the rows of every Operations list, found ${grids} actions slot(s)`);
  assert.deepEqual(offenders, [], `use OpsRowAction (or the design-system Button) in the actions column of: ${offenders.join(", ")}`);
});

// Every list of the Operations page draws its rows on the one shared grid:
// a tab that forks its own row (a card grid, a stacked key/value block) is
// the drift the list contract was written against (#8395, advisory A3).
const OPERATIONS_LISTS = [
  "src/pages/operations/WorkItemsTab.tsx",
  "src/pages/operations/SharedStateTab.tsx",
  "src/pages/operations/WorksetsTab.tsx",
  "src/pages/operations/WorkspacesTab.tsx",
  "src/pages/operations/ExecutionRow.tsx",
  "src/pages/BackgroundTasks.tsx",
];

test("every Operations list draws its rows on OpsRowGrid with one row height", () => {
  const missing = OPERATIONS_LISTS.filter((file) => {
    const source = readFileSync(join(process.cwd(), file), "utf8");
    return !/<OpsRowGrid\b/.test(source) || !/\bOPS_ROW_HEIGHT\b/.test(source);
  });
  assert.deepEqual(missing, [], `draw the rows of these lists on OpsRowGrid inside OPS_ROW_HEIGHT: ${missing.join(", ")}`);
});

/** The opening of every row `<Card` (data-testid "...-row"): from the tag to its first `<CardContent`. */
function rowCards(source: string): string[] {
  const cards: string[] = [];
  for (const card of source.matchAll(/<Card\b(?!Content|Header|Title|Description|Footer)/g)) {
    const end = source.indexOf("<CardContent", card.index);
    const tag = source.slice(card.index, end < 0 ? undefined : end);
    if (/data-testid="[a-z-]+-row"/.test(tag)) cards.push(tag);
  }
  return cards;
}

test("the row card reader keeps row cards only", () => {
  const source = `<Card data-testid="x-row" className={cn(OPS_ROW_CARD)}>\n<CardContent />\n<Card className="p-4">\n<CardContent />`;
  const cards = rowCards(source);
  assert.equal(cards.length, 1);
  assert.match(cards[0], /OPS_ROW_CARD/);
});

// Every Operations row card draws the default hairline through the one shared
// carrier, so the edge never falls back to Paper's fainter `-soft` hairline
// (about 1.09:1 on dark, read as a missing bottom border), a state colour is
// never as faint as /10, and a seventh tab cannot drift.
test("every Operations row card draws its edge through OPS_ROW_CARD", () => {
  const offenders: string[] = [];
  let cards = 0;
  for (const file of OPERATIONS_LISTS) {
    for (const tag of rowCards(readFileSync(join(process.cwd(), file), "utf8"))) {
      cards += 1;
      if (!/\bOPS_ROW_CARD\b/.test(tag) || /border-elevation-subtle-soft/.test(tag) || /\bborder-[a-z-]+\/10\b/.test(tag)) {
        offenders.push(`${file}: ${tag.match(/data-testid="([a-z-]+)"/)?.[1]}`);
      }
    }
  }
  assert.ok(cards >= 7, `expected the row cards of every Operations list, found ${cards}`);
  assert.deepEqual(offenders, [], `pass OPS_ROW_CARD first and no soft or /10 edge on: ${offenders.join(", ")}`);
});

// Every slot of an Operations card sits on the card's vertical centre (Sam,
// 12 Nov 2026: the date and the kebab sat at the top of a card whose owner and
// key formed a two-line stack). The shared grid carries the one alignment
// class; no slot opts out of it or nudges itself down, and the grid takes no
// class from a tab, so a new tab inherits the centre.
test("the shared row grid centres every slot vertically, with no per-tab override", () => {
  const source = readFileSync(join(process.cwd(), "src", "components", "ui", "operations-list.tsx"), "utf8");
  assert.match(source, /export const OPS_ROW_ALIGN = "items-center";/);
  const start = source.indexOf("export function OpsRowGrid(");
  assert.ok(start >= 0, "OpsRowGrid not found");
  const grid = source.slice(start);
  const signature = grid.slice(0, grid.indexOf("}) {"));
  assert.doesNotMatch(signature, /\bclassName\b/, "OpsRowGrid takes no className: the alignment is never overridden by a tab");
  const markup = grid.slice(grid.indexOf("return ("));
  assert.match(markup, /cn\("flex w-full min-w-0 gap-3", OPS_ROW_ALIGN\)/);
  assert.doesNotMatch(markup, /\b(items-(start|end|baseline)|self-(start|end)|pt-[\w.]+|mt-[\w.]+)\b/);
});
