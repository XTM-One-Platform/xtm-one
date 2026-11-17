/**
 * Unit tests for how much of itself a command center widget draws at a given
 * grid size - `yarn test:unit`.
 *
 * Out of the grid (`null`, the stacked layout) every widget draws in full; in
 * a cell, the secondary parts go first, and every widget still draws at its
 * minimum size and at its default one.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { CTEM_WIDGETS, FLEET_ACTIVITY_CLOSED_ROWS, rowsForHeight, withActNowHeight, withClosedFleetActivity, withContentHeight } from "./dashboard-layout.ts";
import { defaultLayout, type WidgetSpec } from "../../../components/dashboard-grid/layout.ts";
import { cellHeight, kpiDensity, kpiLayout, kpiShowsIcon, listCapacity } from "../../../components/dashboard-grid/widget-density.ts";
import { COPILOT_WIDGETS } from "../../dashboard/copilot-dashboard-layout.ts";
import { heroDensity, stageDensity } from "./widget-density.ts";

/** Every widget shown, the v1 ones the v2 default leaves in the catalog too
 *  (a layout where a person added them back). */
const everyWidget = () =>
  defaultLayout(
    Object.fromEntries(Object.entries(CTEM_WIDGETS).map(([id, { hidden, ...spec }]) => [id, hidden ? spec : { ...spec, y: spec.y + 111 }])) as Record<
      keyof typeof CTEM_WIDGETS,
      WidgetSpec
    >,
  );

const at = (id: keyof typeof CTEM_WIDGETS, which: "default" | "min") => {
  const spec = CTEM_WIDGETS[id];
  return which === "default" ? { cols: spec.w, rows: spec.h } : { cols: spec.minW, rows: spec.minH };
};

test("a cell is its rows plus the gaps between them", () => {
  assert.equal(cellHeight(1), 20);
  assert.equal(cellHeight(4), 4 * 20 + 3 * 16);
  assert.equal(cellHeight(12), 12 * 20 + 11 * 16);
});

test("out of the grid every widget draws in full", () => {
  assert.deepEqual(heroDensity(null), { orbit: "side", subtitle: "full", pills: "full", syncDetails: true });
  assert.deepEqual(kpiDensity(null), { hideSub: false, badgeByValue: false, wrapLabel: false });
  assert.equal(stageDensity(null), "full");
  assert.equal(listCapacity(null, { chrome: 92, rowHeight: 52, max: 6 }), 6);
});

test("the default hero is the command center as it was: orbit beside, full pills, no stage ribbon (#11464)", () => {
  assert.deepEqual(heroDensity(at("hero", "default")), {
    orbit: "side",
    subtitle: "full",
    pills: "full",
    syncDetails: true,
  });
});

test("the smallest hero keeps its score as a chip and its products as logos", () => {
  assert.deepEqual(heroDensity(at("hero", "min")), {
    orbit: "chip",
    subtitle: "clamped",
    pills: "compact",
    syncDetails: false,
  });
});

test("a tall narrow hero draws its orbit below the text", () => {
  assert.equal(heroDensity({ cols: 6, rows: 18 }).orbit, "below");
});

test("a wide but short hero drops the orbit, keeps full pills", () => {
  const d = heroDensity({ cols: 12, rows: 9 });
  assert.equal(d.orbit, "chip");
  assert.equal(d.pills, "full");
});

test("a KPI card drops its secondary line at three rows and moves its badge when narrow", () => {
  assert.deepEqual(kpiDensity(at("kpi_posture", "default")), { hideSub: false, badgeByValue: true, wrapLabel: true });
  assert.deepEqual(kpiDensity(at("kpi_posture", "min")), { hideSub: true, badgeByValue: true, wrapLabel: true });
  assert.deepEqual(kpiDensity({ cols: 4, rows: 3 }), { hideSub: true, badgeByValue: false, wrapLabel: false });
});

// #9426 UX round 1 B1: at 1440 a strip card's label box is 84 px, so "Leaked
// credentials" or "Identifiants divulgués" was cut with nothing showing the
// rest. Every strip card wraps its label on two lines, at its default size and
// at its minimum one, instead of cutting it.
test("every KPI card of the strip wraps its label instead of cutting it", () => {
  const strip = Object.keys(CTEM_WIDGETS).filter((id) => id.startsWith("kpi_")) as (keyof typeof CTEM_WIDGETS)[];
  assert.equal(strip.length, 6);
  for (const id of strip) {
    assert.equal(kpiDensity(at(id, "default")).wrapLabel, true, `${id} default`);
    assert.equal(kpiDensity(at(id, "min")).wrapLabel, true, `${id} min`);
  }
});

// #9426 UX round 3 B1: centred content moved each value up or down with its
// one- or two-line label (11 px spread on the CTEM strip, 35 to 49 px on the
// home tiles). Every KPI card of a row, narrow or wide, top-aligns its content
// with the same padding and reserves the same two-line label block, so the
// values of the row start at one y.
test("every KPI card of a grid row puts its value at the same height", () => {
  const rows = [
    Object.entries(CTEM_WIDGETS).filter(([id]) => id.startsWith("kpi_")),
    Object.entries(COPILOT_WIDGETS).filter(([id]) => id.startsWith("kpi_")),
  ];
  for (const row of rows) {
    assert.ok(row.length >= 5);
    for (const which of ["default", "min"] as const) {
      const layouts = row.map(([, spec]) => kpiLayout(which === "default" ? { cols: spec.w, rows: spec.h } : { cols: spec.minW, rows: spec.minH }));
      for (const layout of layouts) {
        assert.equal(layout.card, "justify-start");
        assert.equal(layout.content, layouts[0].content);
        assert.match(layout.content, /\bitems-start\b/);
        assert.match(layout.labelRow, /\bitems-start\b/);
        assert.match(layout.label, /\bmin-h-\[2lh\]/);
      }
    }
  }
});

test("a stacked KPI card, alone on its line, keeps its content centred", () => {
  assert.equal(kpiLayout(null).card, "justify-center");
  assert.match(kpiLayout(null).content, /\bitems-center\b/);
});

test("a narrow sample KPI card drops its icon so the Sample badge never sits under it", () => {
  const narrow = kpiDensity(at("kpi_posture", "default"));
  const wide = kpiDensity({ cols: 4, rows: 3 });
  // The badge sits by the value in a narrow card: icon and badge would intersect.
  assert.equal(kpiShowsIcon(narrow, true), false);
  // Live values keep the icon, and a wide card keeps the badge in the label row.
  assert.equal(kpiShowsIcon(narrow, false), true);
  assert.equal(kpiShowsIcon(wide, true), true);
  assert.equal(kpiShowsIcon(kpiDensity(null), true), true);
});

test("a stage card draws in full by default and compacts at its minimum", () => {
  assert.equal(stageDensity(at("stage_scoping", "default")), "full");
  assert.equal(stageDensity({ cols: 4, rows: 12 }), "medium");
  assert.equal(stageDensity({ cols: 4, rows: 10 }), "medium");
  assert.equal(stageDensity(at("stage_scoping", "min")), "compact");
});

test("a list holds as many rows as fit, never fewer than one nor more than it has", () => {
  const recs = { chrome: 92, rowHeight: 52, max: 6 };
  assert.equal(listCapacity(at("recommended_actions", "default"), recs), 6);
  assert.equal(listCapacity(at("recommended_actions", "min"), recs), 2);
  assert.equal(listCapacity({ cols: 4, rows: 1 }, recs), 1);
  assert.equal(listCapacity({ cols: 4, rows: 40 }, { ...recs, max: 3 }), 3);
});

test("the fleet activity card holds two rows of tiles in 3 columns with no inner scroll", () => {
  // UX round 2 on #9570, B1, measured at 1440: at 15 rows (524 px) the tiles grid
  // had 431 px for a 440 px scroll height (two rows of 210 px tiles, a 12 px gap,
  // the 4 px focus inset), so the card spends 93 px around the grid.
  const chrome = cellHeight(15) - 431;
  const tiles = 440;
  assert.ok(cellHeight(15) - chrome < tiles);
  assert.ok(cellHeight(CTEM_WIDGETS.fleet_activity.h) - chrome >= tiles);
});

test("with every fleet off and the invitation closed, the fleet activity card is its one line and the widgets below move up", () => {
  // UX round 3 on #9570, B1: the closed card kept 560 px for one line (72 px of
  // content). Drawn at 3 rows (92 px): p-4 (32), the 28 px header line, the
  // 12 px gap and one 12 px line fit; nothing is left empty under it.
  assert.equal(cellHeight(FLEET_ACTIVITY_CLOSED_ROWS), 92);
  assert.ok(cellHeight(FLEET_ACTIVITY_CLOSED_ROWS) >= 32 + 28 + 12 + 18);
  const base = everyWidget();
  const drawn = withClosedFleetActivity(base);
  const at = (layout: typeof base, id: string) => {
    const item = layout.items.find((candidate) => candidate.i === id);
    assert.ok(item, `${id} is on the grid`);
    return item;
  };
  const freed = CTEM_WIDGETS.fleet_activity.h - FLEET_ACTIVITY_CLOSED_ROWS;
  assert.equal(at(drawn, "fleet_activity").h, FLEET_ACTIVITY_CLOSED_ROWS);
  assert.equal(at(drawn, "fleet_timeline").y, at(base, "fleet_timeline").y - freed);
  assert.equal(at(drawn, "coverage_matrix").y, at(base, "coverage_matrix").y - freed);
  assert.equal(at(drawn, "exposure_stats").y, at(base, "exposure_stats").y - freed);
  // What sits above the card does not move, and the saved layout is not changed.
  assert.deepEqual(at(drawn, "digests"), at(base, "digests"));
  assert.equal(at(base, "fleet_activity").h, CTEM_WIDGETS.fleet_activity.h);
  // A narrow card keeps a fourth row for the sentence to wrap; a card already
  // that short is left as it is.
  const narrow = { ...base, items: base.items.map((item) => (item.i === "fleet_activity" ? { ...item, w: 4 } : item)) };
  assert.equal(at(withClosedFleetActivity(narrow), "fleet_activity").h, FLEET_ACTIVITY_CLOSED_ROWS + 1);
  const short = { ...base, items: base.items.map((item) => (item.i === "fleet_activity" ? { ...item, h: 3 } : item)) };
  assert.equal(withClosedFleetActivity(short), short);
});

test("the act now card is drawn at the height of its rows, and Show all grows it in place", () => {
  // UX round 1 on #10357: B2, three rows left about 200 px empty in a 416 px
  // card; B1, Show all scrolled inside the card and cut row 6.
  assert.equal(rowsForHeight(cellHeight(5)), 5);
  assert.equal(rowsForHeight(cellHeight(5) + 1), 6);
  assert.equal(rowsForHeight(100_000), 40);
  const base = everyWidget();
  const card = base.items.find((item) => item.i === "act_now");
  assert.ok(card);
  assert.equal(withActNowHeight(base, null), base);
  // Three rows: p-4 (32) + 36 px header + 8 px gap + 3 x 52 px rows + 2 px border.
  const three = withActNowHeight(base, 32 + 36 + 8 + 3 * 52 + 2);
  const short = three.items.find((item) => item.i === "act_now");
  assert.ok(short && short.h < card.h);
  assert.ok(cellHeight(short.h) - (32 + 36 + 8 + 3 * 52 + 2) < 36, "less than one grid step left empty");
  // Ten rows and the Show fewer button: taller than the default card, and a
  // widget placed under it moves down by the rows it took.
  const below = { ...base, items: base.items.map((item) => (item.i === "digests" ? { ...item, x: 0, y: card.y + card.h } : item)) };
  const tallPx = 32 + 36 + 8 + 10 * 52 + 8 + 36 + 2;
  const tall = withActNowHeight(below, tallPx);
  const grown = tall.items.find((item) => item.i === "act_now");
  assert.ok(grown && cellHeight(grown.h) >= tallPx && grown.h > card.h);
  const under = below.items.find((item) => item.i === "digests");
  const moved = tall.items.find((item) => item.i === "digests");
  assert.ok(under && moved);
  assert.equal(moved.y, under.y + (grown.h - card.h));
  // A widget above the card keeps its place.
  assert.deepEqual(tall.items.find((item) => item.i === "hero"), below.items.find((item) => item.i === "hero"));
  // The saved layout is never changed.
  assert.equal(base.items.find((item) => item.i === "act_now")?.h, CTEM_WIDGETS.act_now.h);
});

test("the Done for you timeline is drawn at the height of its lanes (C2 V3)", () => {
  const base = defaultLayout(CTEM_WIDGETS);
  const card = base.items.find((item) => item.i === "done_for_you");
  assert.ok(card);
  assert.equal(withContentHeight(base, "done_for_you", null), base);
  // Two lanes, the header, the legend and the axis: about 190 px.
  const drawn = withContentHeight(base, "done_for_you", 190);
  const short = drawn.items.find((item) => item.i === "done_for_you");
  assert.ok(short && short.h < card.h && cellHeight(short.h) >= 190);
  // Only it moves: the act now card above keeps its place.
  assert.deepEqual(drawn.items.find((item) => item.i === "act_now"), base.items.find((item) => item.i === "act_now"));
});
