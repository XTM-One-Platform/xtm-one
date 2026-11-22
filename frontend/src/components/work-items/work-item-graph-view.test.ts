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
  assert.match(VIEW, /onNodeClick=\{\(_, node\) => selectNode\(node\.id\)\}/);
});

test("a node's own element takes the keyboard only, never a second click handler", () => {
  assert.equal(/\{\.\.\.buttonRoleProps\(/.test(VIEW), false);
  assert.ok((VIEW.match(/\{\.\.\.nodeKeyProps\(/g) ?? []).length === 3, "item, ref and person nodes");
});

test("the canvas fits the graph again after a resize (UX r1 A1 on #979)", () => {
  assert.match(VIEW, /<FitOnResize \/>/);
});

test("the fit reserves the in-frame bar at the top, so no node is fitted under it (UX r2 B1 on #979)", () => {
  // The bar sits at top-2 (8 px) and is one control row high (about 42 px): the top fit
  // inset must clear it with a gap, on mount and after a resize (same options).
  const inset = Number(/const GRAPH_BAR_INSET_PX = (\d+);/.exec(VIEW)?.[1]);
  assert.ok(inset >= 56, `top inset ${inset}px clears the bar`);
  assert.match(VIEW, /padding: \{ top: `\$\{GRAPH_BAR_INSET_PX\}px`/);
  assert.match(VIEW, /className="pointer-events-none absolute inset-x-2 top-2 z-10/);
  assert.match(VIEW, /fitViewOptions=\{FIT_VIEW_OPTIONS\}/);
  assert.match(VIEW, /fitView\(FIT_VIEW_OPTIONS\)/);
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
