/**
 * The fleet page's tabs (#9980) - `yarn test`.
 *
 * Data flow is a tab of the fleet page, right after Graph, never a switch
 * inside the Graph tab: one row of tabs, no tab-styled row under it. The Graph
 * tab's toolbar keeps only the canvas controls (the layouts, the legend, the
 * counters).
 */
import { test } from "vitest";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { FLEET_TAB_LABELS, FLEET_TABS } from "@/lib/fleet-operations";

// vitest runs from frontend/ (CI and `yarn test`), as fleet-about-one-graph.test.ts.
const PAGE = readFileSync(join(process.cwd(), "src", "pages", "flows", "FleetPage.tsx"), "utf8");
const TOOLBAR = readFileSync(join(process.cwd(), "src", "components", "ui", "agent-graph-toolbar.tsx"), "utf8");
const OPERATIONS = readFileSync(join(process.cwd(), "src", "components", "fleets", "FleetOperations.tsx"), "utf8");

test("the fleet tabs read About, Graph, Data flow, Members, Approval, Schedule, Operations, then ROI for its managers (#10176)", () => {
  // Sam's order and cap, 8 Nov 2026: eight tabs, none added after.
  assert.deepEqual(
    FLEET_TABS.map((key) => `${key}:${FLEET_TAB_LABELS[key]}`),
    [
      "about:About",
      "graph:Graph",
      "flow:Data flow",
      "members:Members",
      "approval:Approval",
      "schedule:Schedule",
      "operations:Operations",
      "roi:ROI",
    ],
  );
  assert.ok(FLEET_TABS.length <= 8, "a ninth fleet tab fails review");
  assert.match(PAGE, /const MANAGER_VIEW_OPTIONS[^\n]*FLEET_TABS\.map/);
  assert.match(PAGE, /const VIEW_OPTIONS = MANAGER_VIEW_OPTIONS\.filter\(\(option\) => option\.key !== "roi"\)/);
});

test("human approval is a tab: the header above the tabs carries nothing of it (#10176)", () => {
  const header = PAGE.slice(0, PAGE.indexOf("<SegmentedFilter"));
  const body = PAGE.slice(PAGE.indexOf("<SegmentedFilter"));
  assert.equal(/<FleetAutonomyStrip\b/.test(header.slice(header.indexOf("return ("))), false);
  assert.match(body, /view === "approval" \?[\s\S]{0,200}<FleetAutonomyStrip\b/);
});

test("one row of tabs: the page draws a single view switch and the graph toolbar none", () => {
  assert.equal((PAGE.match(/<SegmentedFilter\b/g) ?? []).length, 1);
  assert.equal(/\bmode=\{|onModeChange|graphMode/.test(PAGE), false);
  assert.equal(/\bmode\?:|onModeChange|"graph-mode"/.test(TOOLBAR), false);
});

test("no pill row under the fleet tabs: the graph layout and the Operations list are selects (UX round 1 B1, B2)", () => {
  const toolbar = TOOLBAR.slice(TOOLBAR.indexOf("export function AgentGraphToolbar"), TOOLBAR.indexOf("export interface AgentGraphStatusLineProps"));
  assert.equal(/<SegmentedFilter\b/.test(toolbar), false);
  assert.match(toolbar, /<SelectTrigger[^>]*aria-label=\{t_i18n\("Graph layout"\)\}/);
  assert.equal(/SegmentedFilter/.test(OPERATIONS), false);
  assert.match(OPERATIONS, /<SelectTrigger[^>]*aria-label=\{t_i18n\("Operations list"\)\}/);
});

test("the data flow is read only on its tab: no other tab asks for it", () => {
  assert.match(PAGE, /useFleetFlow\(fleet \? fleetId : null, requestedView === "flow"\)/);
});
