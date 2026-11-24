// SPDX-License-Identifier: Apache-2.0

/**
 * The widgets of the CTEM command center grid (XTM One home) and their default
 * places. The grid, and how a saved layout becomes the one drawn, are shared
 * with the other dashboards in `components/dashboard-grid/layout.ts`.
 *
 * Each person builds their own command center, one layout per vertical
 * (`User.ctem_dashboard_layouts`, written through `PUT /settings/preferences`).
 * The backend accepts the widget ids of `services/dashboard_layouts.py` only,
 * the same as below.
 */
import type { WidgetSpec } from "@/components/dashboard-grid/layout";
import { MAX_WIDGET_HEIGHT } from "../../../components/dashboard-grid/layout.ts";
import { cellHeight } from "../../../components/dashboard-grid/widget-density.ts";
import type { CtemWidgetId, DashboardLayout } from "@/types";

// The v1 widgets the default leaves out (C2 slice V7): offered from the
// catalog (Add a widget). Their ids stay, so a saved layout keeps them and a
// person can add any of them back; their old places are kept for that add.
const kpi = (x: number): WidgetSpec => ({ x, y: 12, w: 2, h: 4, minW: 2, minH: 3, hidden: true });
const stage = (x: number, y: number): WidgetSpec => ({ x, y, w: 4, h: 14, minW: 3, minH: 8, hidden: true });

/** Every widget of the command center, with its default place. The default
 *  layout reads top to bottom (focus sweep, #10721; lifecycle on top, #11464):
 *  the animated hero (posture), the Exposure lifecycle band (the stages, shown
 *  once: the hero carries no stage ribbon any more), the Exposure briefing
 *  (what happened, what to do), Act now (the top five), stats and coverage,
 *  Done for you and the fleets row. No ROI on this page: the value band waits
 *  in the catalog, the Value page carries the figures. A saved layout draws as
 *  it was saved. */
export const CTEM_WIDGETS: Record<CtemWidgetId, WidgetSpec> = {
  // The posture hero: orbit with the integration logos, the greeting, one row
  // of score cells, product pills and sync (11 rows: the orbit beside the text).
  hero: { x: 0, y: 0, w: 12, h: 11, minW: 6, minH: 9 },
  kpi_posture: kpi(0),
  kpi_exposure_1: kpi(2),
  kpi_exposure_2: kpi(4),
  kpi_exposure_3: kpi(6),
  kpi_agents: kpi(8),
  kpi_executions: kpi(10),
  // The Exposure briefing, under the lifecycle band, drawn at its content height.
  briefing: { x: 0, y: 28, w: 12, h: 14, minW: 4, minH: 3 },
  // The title line of the stage cards, with its Export CSV button (#9477). Two
  // rows (56 px), never one: a 32 px button does not fit a 20 px row and was
  // clipped (UX round 1 on #9478, B1). minH 2 raises a layout saved at one row.
  lifecycle_header: { x: 0, y: 24, w: 12, h: 2, minW: 4, minH: 2, hidden: true },
  stage_scoping: stage(0, 26),
  stage_discovery: stage(4, 26),
  stage_prioritization: stage(8, 26),
  stage_validation: stage(0, 40),
  stage_remediation: stage(4, 40),
  stage_governance: stage(8, 40),
  recommended_actions: { x: 0, y: 54, w: 7, h: 12, minW: 4, minH: 7, hidden: true },
  live_activity: { x: 7, y: 54, w: 5, h: 12, minW: 3, minH: 6, hidden: true },
  digests: { x: 0, y: 66, w: 12, h: 9, minW: 4, minH: 6, hidden: true },
  // The fleets at work (#9050): one tile per fleet. 16 rows (560 px): two rows
  // of CTEM tiles in 3 columns, or the six-fleet invitation, with no inner
  // scroll (UX rounds 1 and 2 on #9570, B1; 15 rows left 9 px to scroll).
  fleet_activity: { x: 0, y: 75, w: 12, h: 16, minW: 4, minH: 6, hidden: true },
  // What the fleets did and what started it (#9050 slice 3).
  fleet_timeline: { x: 0, y: 91, w: 12, h: 10, minW: 4, minH: 6, hidden: true },
  // The five stages against the three modules, and the period stats (#9050 slice 4).
  coverage_matrix: { x: 0, y: 101, w: 6, h: 10, minW: 4, minH: 7, hidden: true },
  exposure_stats: { x: 6, y: 101, w: 6, h: 10, minW: 4, minH: 7, hidden: true },
  // The value band (C2 V1): posture, risk, value and actions. Off the default
  // layout since #10721 (no ROI on the CTEM page); a saved layout keeps it.
  value_band: { x: 0, y: 0, w: 12, h: 10, minW: 6, minH: 8, hidden: true },
  // The Act now queue (C2 V2): five rows and Show all, drawn at its rows' height.
  act_now: { x: 0, y: 42, w: 12, h: 8, minW: 4, minH: 5 },
  // The lifecycle band (C2 V4): six stage chips under the Export CSV title
  // line, each opening its stage page; right under the hero (#11464).
  lifecycle_band: { x: 0, y: 11, w: 12, h: 8, minW: 6, minH: 6 },
  // Stats and coverage (C2 V5): heat map, deltas, findings, ATT&CK tested vs untested.
  stats_coverage: { x: 0, y: 50, w: 12, h: 8, minW: 6, minH: 6 },
  // The Done for you timeline (C2 V3): lanes on a 7 or 30 day axis.
  done_for_you: { x: 0, y: 58, w: 12, h: 10, minW: 6, minH: 6 },
  // The fleets row (C2 V6): one line per fleet the person chose, or the setup
  // invitation when every fleet is off; drawn at its content height.
  fleets_row: { x: 0, y: 68, w: 12, h: 5, minW: 6, minH: 3 },
  // The prioritization funnel (#11546): the raw volume narrowed to what needs
  // action, compact, linking to the Prioritization stage page that carries the
  // full funnel. One compact full-width row under the lifecycle band, above the
  // briefing and Act now (UX r1 decision on #11557); a layout saved before it
  // keeps drawing exactly as saved, the funnel in its catalog.
  prioritization_funnel: { x: 0, y: 23, w: 12, h: 5, minW: 6, minH: 4, catalogWhenSkipped: true },
  // The CTEM Cases row (#11545 slice S5a, dev-docs/ctem-cases.md 7.4, UX Q1): a
  // compact full-width row, right under the lifecycle band and above the funnel
  // since #1621, so the page reads lifecycle, cases in motion, funnel; a layout
  // saved before it keeps drawing exactly as saved, the row in its catalog.
  cases: { x: 0, y: 19, w: 12, h: 4, minW: 6, minH: 3, catalogWhenSkipped: true },
};

/** Rows of the Fleet activity card once every CTEM fleet is off and the
 *  invitation is closed: its title line and one sentence (92 px at full width;
 *  a narrower card gets a fourth row for the sentence to wrap). */
export const FLEET_ACTIVITY_CLOSED_ROWS = 3;

/**
 * The layout drawn while the Fleet activity card holds only its closed line
 * (every CTEM fleet off, invitation closed): the card shrinks to that line and
 * every widget below it moves up by the rows it gave back (UX round 3 on
 * #9570, B1: 488 px of a 560 px card were empty). Never saved: the person's
 * layout keeps the card's own height, and the page draws it again as soon as
 * a fleet is on or the invitation comes back. A card already at most that
 * tall is left as it is.
 */
export function withClosedFleetActivity(layout: DashboardLayout<CtemWidgetId>): DashboardLayout<CtemWidgetId> {
  const card = layout.items.find((item) => item.i === "fleet_activity");
  if (!card) return layout;
  const rows = card.w >= 8 ? FLEET_ACTIVITY_CLOSED_ROWS : FLEET_ACTIVITY_CLOSED_ROWS + 1;
  const freed = card.h - rows;
  if (freed <= 0) return layout;
  const end = card.y + card.h;
  const overlaps = (item: { x: number; w: number }) => item.x < card.x + card.w && card.x < item.x + item.w;
  return {
    ...layout,
    items: layout.items.map((item) => {
      if (item.i === "fleet_activity") return { ...item, h: rows };
      // Only what sits under the card moves up: a widget beside it keeps its row.
      return item.y >= end && overlaps(item) ? { ...item, y: item.y - freed } : item;
    }),
  };
}

/**
 * Whether the value band carries the page's greeting and product pills (C2 V7,
 * parity W1 and W7): only when the layout does not show the v1 hero, which
 * carries them already. A saved layout holding both draws one h1 and one set
 * of pills, in the hero (UX round 1 on #10534, B1).
 */
export function valueBandCarriesTopLine(layout: DashboardLayout<CtemWidgetId>): boolean {
  return !layout.items.some((item) => item.i === "hero");
}

/** The fewest grid rows whose cell holds `px` pixels, at most the tallest widget. */
export function rowsForHeight(px: number): number {
  let rows = 1;
  while (rows < MAX_WIDGET_HEIGHT && cellHeight(rows) < px) rows += 1;
  return rows;
}

/**
 * The layout drawn with the Act now card at the height of its rows (UX round 1
 * on #10357, B1 and B2): three rows leave no empty band, Show all grows the
 * card in place instead of scrolling inside it, Show fewer gives the rows back.
 * Every widget under the card moves by the rows it took or gave back; a widget
 * beside it keeps its row. Never saved: the person's layout is untouched, and
 * the editor draws it. No measure yet (`null`): the layout as it is.
 */
export function withActNowHeight(
  layout: DashboardLayout<CtemWidgetId>,
  contentPx: number | null,
): DashboardLayout<CtemWidgetId> {
  return withContentHeight(layout, "act_now", contentPx);
}

/**
 * The layout drawn with one widget at the height of its content (the Act now
 * queue, the Done for you timeline sized to its lanes): the widgets under it
 * move by the rows it took or gave back, one beside it keeps its row. Never
 * saved. No measure yet (`null`): the layout as it is. Any grid: the Copilot
 * home draws its Fleets widget the same way (#12063).
 */
export function withContentHeight<W extends string = CtemWidgetId>(
  layout: DashboardLayout<W>,
  id: W,
  contentPx: number | null,
): DashboardLayout<W> {
  const card = layout.items.find((item) => item.i === id);
  if (!card || contentPx === null || contentPx <= 0) return layout;
  const rows = rowsForHeight(contentPx);
  const delta = rows - card.h;
  if (delta === 0) return layout;
  const end = card.y + card.h;
  const overlaps = (item: { x: number; w: number }) => item.x < card.x + card.w && card.x < item.x + item.w;
  return {
    ...layout,
    items: layout.items.map((item) => {
      if (item.i === id) return { ...item, h: rows };
      return item.y >= end && overlaps(item) ? { ...item, y: item.y + delta } : item;
    }),
  };
}
