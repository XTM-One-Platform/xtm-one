/**
 * The Graph view of Work Items (#968), UX round 1 on #979 - `yarn test`.
 *
 * B1: React Flow gives a node wrapper `pointer-events: none` when the canvas has no
 * node click handler and elements are not selectable, so a mouse click never reached
 * the node (focus mode) nor its open button. The canvas carries `onNodeClick`, and a
 * node's own element keeps the keyboard side only (no second click handler).
 * B4: the view switch draws the graph glyph through the design system's `<Icon>`.
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`).
const VIEW = readFileSync(join(process.cwd(), "src", "components", "work-items", "WorkItemGraphView.tsx"), "utf8");
const TOGGLE = readFileSync(join(process.cwd(), "src", "components", "ui", "view-toggle.tsx"), "utf8");

test("the canvas selects a node on a mouse click (UX r1 B1 on #979)", () => {
  // A zone or a collapsed group (#1062) is no item: a click there clears the focus.
  assert.match(VIEW, /onNodeClick=\{\(_, node\) => \(node\.type === "zone" \|\| node\.type === "collapsed" \? setSelected\(null\) : selectNode\(node\.id\)\)\}/);
});

test("a node's own element takes the keyboard only, never a second click handler", () => {
  assert.equal(/\{\.\.\.buttonRoleProps\(/.test(VIEW), false);
  assert.ok((VIEW.match(/\{\.\.\.nodeKeyProps\(/g) ?? []).length === 3, "item, ref and person nodes");
});

test("the canvas fits the graph again after a resize (UX r1 A1 on #979)", () => {
  // And again after the reader changes the drawing: a layout, a grouping, a collapse (#1062).
  assert.match(VIEW, /<GraphCamera fitKey=\{fitKey\} positions=\{laid\?\.positions\} \/>/);
  assert.match(VIEW, /\}, \[width, height, fitKey, fitView\]\);/);
});

test("a live refresh never refits the canvas nor resets the camera (#1158)", () => {
  // The fit key is the reader's choices alone, never a count a refresh changes.
  assert.match(VIEW, /const fitKey = graphFitKey\(\{ layout: layoutChoice, groupBy, collapsedKeys, focusMode, selected, showDone \}\);/);
  assert.equal(/nodes\.length \?\? 0\}`/.test(VIEW), false, "no node count in the fit key");
  // A refresh keeps the zoom and anchors the pan on the node at the centre.
  assert.match(VIEW, /anchoredViewport\(last\.positions, positions, getViewport\(\)/);
  // One fitView call in the file: the camera's, keyed on the fit key.
  assert.equal((VIEW.match(/fitView\(FIT_VIEW_OPTIONS\)/g) ?? []).length, 1);
});

test("the controls sit in the fleet graph's toolbar above the canvas, the counters in its status line (#1062)", () => {
  // No bar drawn over the canvas any more: nothing is fitted under a control.
  assert.equal(/work-item-graph-bar/.test(VIEW), false);
  assert.match(VIEW, /<AgentGraphToolbar\n\s+layout=\{layoutChoice\}\n\s+layouts=\{WORK_ITEM_GRAPH_LAYOUTS\}/);
  assert.match(VIEW, /<AgentGraphStatusLine counters=\{counters\} \/>/);
  assert.match(VIEW, /fitViewOptions=\{FIT_VIEW_OPTIONS\}/);
  assert.match(VIEW, /fitView\(FIT_VIEW_OPTIONS\)/);
});

test("the canvas wears the shared graph surface, never React Flow's own #141414 (#1062)", () => {
  const CSS = readFileSync(join(process.cwd(), "src", "components", "ui", "graph-canvas.css"), "utf8");
  assert.match(VIEW, /import "@\/components\/ui\/graph-canvas\.css";/);
  assert.match(VIEW, /className="graph-canvas /);
  // Transparent: the canvas shows the card's own surface, as the fleet Graph tab does (UX r1 B3 on #1067).
  assert.match(CSS, /\.graph-canvas \.react-flow \{\n\s+--xy-background-color: transparent;/);
  assert.match(CSS, /\.graph-canvas \{\n\s+--flow-grid:/, "the grid colour is defined where the canvas reads it");
  assert.equal(/style=\{\{ background: "transparent" \}\}/.test(VIEW), false);
});

test("no minimap over the cards, the fleet graph has none (UX r1 B1 on #1067)", () => {
  assert.equal(/<MiniMap\b/.test(VIEW), false);
});

test("a zone counts items, the status line's unit (UX r1 B2 on #1067)", () => {
  assert.equal(/# node\} other \{# nodes\}\}", \{ count: data\.count \}/.test(VIEW), false);
  assert.equal((VIEW.match(/t_i18n\("\{count, plural, one \{# item\} other \{# items\}\}", \{ count: data\.count \}\)/g) ?? []).length, 2);
  assert.match(VIEW, /count: itemCount\(zone\.group\)/);
});

test("the graph card is the fleet Graph tab's Card, the same elevation layer (UX r1 B3 on #1067)", () => {
  assert.match(VIEW, /import \{ Card \} from "@\/components\/ui\/card";/);
  assert.match(VIEW, /<Card\n\s+ref=\{setFrame\}/);
});

test("the legend reads the progression: the arrow unblocks (#1062)", () => {
  assert.match(VIEW, /t_i18n\("Arrow: unblocks"\)/);
  assert.equal(/Arrow: waits for/.test(VIEW), false);
});

test("the layout and the grouping are kept per reader, like List | Graph (#1062)", () => {
  assert.match(VIEW, /useStoredChoice<WorkItemGraphLayout>\(\n\s+"copilot-graph:work-items:layout"/);
  assert.match(VIEW, /useStoredChoice<WorkItemGraphGroupBy>\("copilot-graph:work-items:group-by"/);
});

test("an item node that waits for the reader carries the list's Waiting for you marker (#1001)", () => {
  // Same rule as the list rows (waitsForReader), never on a blocker drawn outside the filter.
  assert.match(VIEW, /waitingForYou: node\.kind === "item" && !node\.outside_filter && waitsForReader\(node, readerId\)/);
  assert.match(VIEW, /<WaitingForYouBadge testId="work-item-graph-waiting-for-you" \/>/);
});

test("cards and graph nodes draw the one shared Waiting for you badge, never inline markup (UX r3 B1 on #1004)", () => {
  const TAB = readFileSync(join(process.cwd(), "src", "pages", "operations", "WorkItemsTab.tsx"), "utf8");
  const BADGE = readFileSync(join(process.cwd(), "src", "components", "work-items", "WaitingForYouBadge.tsx"), "utf8");
  for (const [name, source] of [["WorkItemsTab.tsx", TAB], ["WorkItemGraphView.tsx", VIEW]] as const) {
    assert.equal(/t_i18n\("Waiting for you"\)/.test(source), false, `${name} renders the marker inline`);
    assert.match(source, /<WaitingForYouBadge testId="work-item-(graph-)?waiting-for-you" \/>/, name);
  }
  // One glyph, one size, through the design system's Icon.
  assert.match(BADGE, /<Icon name="user" size=\{16\} className="size-3 shrink-0" aria-hidden \/>/);
  assert.equal(/lucide-react/.test(BADGE), false);
});

test("the graph glyph of the view switch is the design system's Icon (UX r1 B4 on #979)", () => {
  assert.equal(/from "lucide-react"[^\n]*Network|\bNetwork\b,/.test(TOGGLE), false);
  assert.match(TOGGLE, /<Icon name="network"/);
});
