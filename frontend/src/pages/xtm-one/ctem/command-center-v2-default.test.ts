/**
 * The command center default layout (C2 slice V7, refocused by the focus
 * sweep #10721, lifecycle on top #11464): the animated hero, the lifecycle
 * band, the Exposure briefing, Act now, stats and coverage, Done for you and
 * the fleets row, in that order; no ROI on the page; and no widget is lost: every other one waits in
 * the catalog (Add a widget) and a saved layout keeps it.
 * `yarn test:unit` (node:test).
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { cellHeight } from "../../../components/dashboard-grid/widget-density.ts";
import { GRID_COLUMNS, defaultLayout, isDefaultLayout, readingOrder, resolveLayout, widgetIds } from "../../../components/dashboard-grid/layout.ts";
import { CTEM_WIDGETS, valueBandCarriesTopLine } from "./dashboard-layout.ts";

const HERE = dirname(fileURLToPath(import.meta.url));

const DEFAULT = ["hero", "lifecycle_band", "cases", "prioritization_funnel", "briefing", "act_now", "stats_coverage", "done_for_you", "fleets_row"] as const;
const CATALOG = [
  "value_band",
  "kpi_posture",
  "kpi_exposure_1",
  "kpi_exposure_2",
  "kpi_exposure_3",
  "kpi_agents",
  "kpi_executions",
  "lifecycle_header",
  "stage_scoping",
  "stage_discovery",
  "stage_prioritization",
  "stage_validation",
  "stage_remediation",
  "stage_governance",
  "recommended_actions",
  "live_activity",
  "digests",
  "fleet_activity",
  "fleet_timeline",
  "coverage_matrix",
  "exposure_stats",
] as const;

test("every widget id of the command center still exists: none lost, none added unseen", () => {
  assert.deepEqual([...widgetIds(CTEM_WIDGETS)].sort(), [...DEFAULT, ...CATALOG].sort());
});

test("the default layout reads hero, lifecycle, Cases, funnel, briefing, Act now, stats, Done for you, fleets, at full width, with no gap or overlap (#1621)", () => {
  const layout = defaultLayout(CTEM_WIDGETS);
  assert.deepEqual(readingOrder(layout.items).map((item) => item.i), [...DEFAULT]);
  let row = 0;
  for (const item of readingOrder(layout.items)) {
    assert.equal(item.x, 0, item.i);
    assert.equal(item.w, GRID_COLUMNS, item.i);
    assert.equal(item.y, row, `${item.i} starts where the one above ends`);
    row += item.h;
  }
  assert.equal(isDefaultLayout(layout, CTEM_WIDGETS), true);
});

test("every other widget waits in the catalog of the default layout", () => {
  assert.deepEqual([...defaultLayout(CTEM_WIDGETS).hidden].sort(), [...CATALOG].sort());
});

test("no ROI on the default layout: the value band and the fleet tiles wait in the catalog (#10721)", () => {
  const shown = defaultLayout(CTEM_WIDGETS).items.map((item) => item.i);
  assert.ok(!shown.includes("value_band"), "the value band carries hours saved and net value");
  assert.ok(!shown.includes("fleet_activity"), "the fleet tiles carry hours saved and net value");
  // The cards the default layout draws read no ROI report and print no ROI figure.
  for (const file of ["PrioritizationFunnel.tsx", "BriefingCard.tsx", "ActNowCard.tsx", "CasesCountsCard.tsx", "LifecycleBandCard.tsx", "StatsCoverageCard.tsx", "DoneForYouCard.tsx", "FleetsRowCard.tsx"]) {
    const source = readFileSync(join(HERE, file), "utf8");
    assert.doesNotMatch(source, /components\/roi\/|useRoiReportApi|useValueReport|RoiFigure/, `${file} reads no ROI report`);
    assert.doesNotMatch(source, /h saved|Hours saved|Net value/, `${file} prints no ROI figure`);
  }
});

test("the first screen at 1440x900 holds the hero and the whole lifecycle band (#11464)", () => {
  // 900 px less the top bar (56), the page padding (24) and the Customize row (40).
  const firstScreen = 900 - 56 - 24 - 40;
  const hero = cellHeight(CTEM_WIDGETS.hero.h);
  const band = cellHeight(CTEM_WIDGETS.lifecycle_band.h);
  assert.ok(hero + 16 + band <= firstScreen, `hero ${hero} px + band ${band} px`);
});

test("a layout saved on the v1 page keeps every widget it drew where it was saved, the default sections below it", () => {
  const saved = {
    items: [
      { i: "stage_scoping", x: 0, y: 0, w: 4, h: 14 },
      { i: "fleet_activity", x: 0, y: 14, w: 12, h: 16 },
    ],
    hidden: [],
  };
  const layout = resolveLayout(saved, CTEM_WIDGETS);
  assert.deepEqual(layout.items.map((item) => item.i).slice(0, 2), ["stage_scoping", "fleet_activity"]);
  // The funnel and the Cases row came after every v1 layout: they wait in the catalog of a saved one.
  for (const id of DEFAULT.filter((id) => id !== "prioritization_funnel" && id !== "cases")) assert.ok(layout.items.some((item) => item.i === id), id);
  assert.ok(layout.hidden.includes("prioritization_funnel"));
  assert.ok(layout.hidden.includes("cases"));
  for (const id of CATALOG) {
    if (!saved.items.some((item) => item.i === id)) assert.ok(layout.hidden.includes(id), id);
  }
});

test("a layout saved before the prioritization funnel draws exactly as saved, the funnel in the catalog (#11546)", () => {
  const items = defaultLayout(CTEM_WIDGETS).items.map(({ i, x, y, w, h }) => ({ i, x, y, w, h }));
  // A layout saved before #11546 names neither the funnel nor any other new widget.
  const savedItems = items
    .filter((item) => item.i !== "prioritization_funnel")
    .map((item) => (item.y > CTEM_WIDGETS.prioritization_funnel.y ? { ...item, y: item.y - CTEM_WIDGETS.prioritization_funnel.h } : item));
  const hidden = defaultLayout(CTEM_WIDGETS).hidden;
  const layout = resolveLayout({ items: savedItems, hidden }, CTEM_WIDGETS);
  assert.deepEqual(layout.items, savedItems);
  assert.ok(layout.hidden.includes("prioritization_funnel"));
  assert.ok(!layout.items.some((item) => item.i === "prioritization_funnel"));
});

test("a layout saved on the v2 page keeps its value band where it was saved", () => {
  const saved = { items: [{ i: "value_band", x: 0, y: 0, w: 12, h: 10 }, { i: "act_now", x: 0, y: 10, w: 12, h: 12 }], hidden: [] };
  const layout = resolveLayout(saved, CTEM_WIDGETS);
  assert.deepEqual(layout.items.slice(0, 2).map((item) => item.i), ["value_band", "act_now"]);
});

test("the greeting and the product pills are drawn once: in the hero by default, in the value band only without the hero", () => {
  assert.equal(valueBandCarriesTopLine(defaultLayout(CTEM_WIDGETS)), false);
  const bandOnly = resolveLayout({ items: [{ i: "value_band", x: 0, y: 0, w: 12, h: 10 }], hidden: ["hero"] }, CTEM_WIDGETS);
  assert.equal(valueBandCarriesTopLine(bandOnly), true);
});
