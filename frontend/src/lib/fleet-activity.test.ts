/**
 * Unit tests for the fleet activity of the CTEM command center - `yarn test:unit`.
 *
 * A figure the reader could not read is `null`, never 0; the seeded fleets
 * come first; the ROI figures only exist once the report was readable.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  dashboardFleets,
  documentedRunRange,
  fleetActivity,
  fleetOptIn,
  fleetsSummary,
  inviteVisible,
  isCtemFleet,
  lastDays,
  lastTileSpan,
  successRate,
  type RoiReport,
} from "./fleet-activity.ts";

// UX round 1 on #9570, B2: the invitation shows the fleet's documented range, never "No run expected".
test("documentedRunRange sums the documented triggers and stays null without one", () => {
  assert.deepEqual(
    documentedRunRange([
      { trigger: "New relevant threat", runs_min: 4, runs_max: 30 },
      { trigger: "Weekly threat sweep", runs_min: 4, runs_max: 5 },
      { trigger: "Re-test due", runs_min: 0, runs_max: 10 },
    ]),
    { min: 8, max: 45 },
  );
  assert.equal(documentedRunRange(null), null);
  assert.equal(documentedRunRange([]), null);
  assert.equal(documentedRunRange([{ trigger: "Never", runs_min: 0, runs_max: 0 }]), null);
});

/** A custom fleet forked from a seeded CTEM fleet, so it is on the command center. */
const CTEM_FORK = { slug: "ctem-loop", is_builtin: true };

const fleet = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
  id,
  name,
  slug: `custom-${id}`,
  forked_from: CTEM_FORK,
  icon: "Bot",
  color: "#000",
  is_archived: false,
  member_count: 2,
  ...extra,
});

/** Every automation of the fleet on. */
const ON = { total: 2, enabled: 2, paused: 0 };
const OFF = { total: 2, enabled: 0, paused: 0 };

const member = (extra: Record<string, unknown> = {}) => ({
  last_run_at: null,
  last_run_status: null,
  runs_in_flight: 0,
  open_work_items: 0,
  stale_work_items: 0,
  ...extra,
});

// The home Fleets widget (#11996): the last 7 days, the success rate, the header line.
test("lastDays keeps the last 7 days, left-padded, and stays null without a series", () => {
  assert.deepEqual(lastDays(Array.from({ length: 30 }, (_, i) => i)), [23, 24, 25, 26, 27, 28, 29]);
  assert.deepEqual(lastDays([2, 1]), [0, 0, 0, 0, 0, 2, 1]);
  assert.equal(lastDays(undefined), null);
});

// UX round 1 on #12015, B5: the last tile fills the free columns of its row at 2 and 3 columns.
test("lastTileSpan fills the last row at the 2- and 3-column widths", () => {
  assert.deepEqual(lastTileSpan(1), { two: 1, three: 1 });
  assert.deepEqual(lastTileSpan(2), { two: 1, three: 1 });
  assert.deepEqual(lastTileSpan(3), { two: 2, three: 1 });
  assert.deepEqual(lastTileSpan(4), { two: 1, three: 3 });
  assert.deepEqual(lastTileSpan(5), { two: 2, three: 2 });
  assert.deepEqual(lastTileSpan(6), { two: 1, three: 1 });
  assert.deepEqual(lastTileSpan(7), { two: 2, three: 3 });
});

test("successRate counts ended runs only and is null when none ended", () => {
  assert.equal(successRate([3, 1], [0, 1]), 0.8);
  assert.equal(successRate([0, 0], [0, 0]), null);
  assert.equal(successRate(null, [1]), null);
});

test("fleetActivity maps the widget's fields from the health, and none for a fleet that is off", () => {
  const days = (v: number) => Array.from({ length: 30 }, () => v);
  const { tiles } = fleetActivity([fleet("a", "Alpha"), fleet("b", "Beta")], {
    a: {
      fleet_id: "a",
      status: "failing",
      members: [member({ status: "failing" }), member({ status: "ok" })],
      automations: { ...ON, off_names: [] },
      run_stats: { by_day: days(2), succeeded_by_day: days(1), failed_by_day: days(1), last_summary: "Did it" },
    },
    b: { fleet_id: "b", status: "idle", members: [], automations: { ...OFF, off_names: ["Daily", "Weekly"] } },
  }, null);
  const [a, b] = tiles;
  assert.deepEqual(a.succeededByDay, [1, 1, 1, 1, 1, 1, 1]);
  assert.equal(a.failingMembers, 1);
  assert.equal(a.memberCount, 2);
  assert.equal(a.lastSummary, "Did it");
  assert.equal(b.succeededByDay, null);
  assert.equal(b.lastSummary, null);
  assert.deepEqual(b.offAutomations, ["Daily", "Weekly"]);
  assert.equal(b.automationsOn, 0);
  assert.deepEqual(fleetsSummary([a]), { fleets: 1, runs: 14, rate: 0.5 });
});

const roi = (fleets: { id: string; units: number; hours_saved: number; net_value: number; review_rate?: number | null }[]): RoiReport => {
  const figures = { currency: "USD", runs: 0, units: 0, units_by_kind: {}, minutes_saved: 0, hours_saved: 0, llm_cost: 0, value: 0, net_value: 0, roi: null, payback_months: null, assumptions: [] };
  return {
    window: "7d",
    since: null,
    currency: "USD",
    rate_label: "assumption",
    estimated: true,
    organization: figures,
    agents: [],
    fleets: fleets.map((f) => ({ ...figures, name: f.id, ...f })),
  };
};

// #11464: the page shows the fleets the person chose, built-in or their own.
test("dashboardFleets: the CTEM fleets by default, the chosen ones in order otherwise", () => {
  const ctem = fleet("c", "CTEM");
  const own = fleet("o", "Own", { forked_from: null, slug: "mine" });
  const archived = fleet("x", "Old", { is_archived: true });
  const all = [ctem, own, archived];
  assert.deepEqual(dashboardFleets(all, null).map((f) => f.id), ["c"]);
  assert.deepEqual(dashboardFleets(all, ["o", "c"]).map((f) => f.id), ["o", "c"]);
  assert.deepEqual(dashboardFleets(all, ["x", "gone"]).map((f) => f.id), []);
  assert.deepEqual(dashboardFleets(all, []), []);
});

test("fleetActivity keeps the chosen order and a custom fleet", () => {
  const a = fleetActivity(
    [fleet("c", "A CTEM"), fleet("o", "Z own", { forked_from: null, slug: "mine" })],
    {},
    null,
    ["o", "c"],
  );
  assert.deepEqual(a.tiles.map((t) => t.id), ["o", "c"]);
});

test("no fleet: no tile and every total unknown", () => {
  const a = fleetActivity(undefined, {}, null);
  assert.deepEqual(a.tiles, []);
  assert.deepEqual(a.totals, { runsInFlight: null, recentRuns: null, openWorkItems: null, hoursSaved: null });
  assert.equal(a.roiAvailable, false);
});

test("seeded fleets first, then custom ones, each by name; archived ones left out", () => {
  const a = fleetActivity(
    [
      fleet("c", "Zeta custom"),
      fleet("s2", "Validation", { is_builtin: true, slug: "ctem-vuln-prioritization", forked_from: null }),
      fleet("x", "Old", { is_archived: true }),
      fleet("s1", "Threat", { is_builtin: true, slug: "ctem-loop", forked_from: null }),
      fleet("d", "Alpha custom"),
    ],
    {},
    null,
  );
  assert.deepEqual(a.tiles.map((t) => t.id), ["s1", "s2", "d", "c"]);
});

test("a health read not answered leaves the tile's figures unknown, not zero", () => {
  const a = fleetActivity([fleet("f", "F")], {}, null);
  const [t] = a.tiles;
  assert.equal(t.runsInFlight, null);
  assert.equal(t.openWorkItems, null);
  assert.equal(t.status, null);
  assert.equal(t.units, null);
});

test("the members' figures are summed and the latest run is kept with its status", () => {
  const a = fleetActivity(
    [fleet("f", "F")],
    {
      f: {
        fleet_id: "f",
        status: "ok",
        automations: ON,
        members: [
          member({ runs_in_flight: 1, recent_runs: 4, failures_24h: 1, open_work_items: 2, last_run_at: "2026-11-06T05:00:00Z", last_run_status: "failed" }),
          member({ runs_in_flight: 2, recent_runs: 3, open_work_items: 1, stale_work_items: 1, last_run_at: "2026-11-06T06:00:00Z", last_run_status: "completed" }),
        ],
      },
    },
    null,
  );
  const [t] = a.tiles;
  assert.equal(t.runsInFlight, 3);
  assert.equal(t.recentRuns, 7);
  assert.equal(t.failures24h, 1);
  assert.equal(t.openWorkItems, 3);
  assert.equal(t.staleWorkItems, 1);
  assert.equal(t.lastRunAt, "2026-11-06T06:00:00Z");
  assert.equal(t.lastRunStatus, "completed");
  assert.equal(a.totals.runsInFlight, 3);
});

test("totals sum the known tiles only", () => {
  const a = fleetActivity(
    [fleet("a", "A"), fleet("b", "B")],
    { a: { fleet_id: "a", status: "ok", automations: ON, members: [member({ runs_in_flight: 2 })] } },
    null,
  );
  assert.equal(a.totals.runsInFlight, 2);
});

test("with the ROI report, a fleet it does not name produced nothing: 0, not unknown", () => {
  const on = (id: string) => ({ fleet_id: id, status: "ok", automations: ON, members: [] });
  const a = fleetActivity([fleet("a", "A"), fleet("b", "B")], { a: on("a"), b: on("b") }, roi([{ id: "a", units: 12, hours_saved: 5.5, net_value: 300 }]));
  const [ta, tb] = a.tiles;
  assert.equal(ta.units, 12);
  assert.equal(ta.hoursSaved, 5.5);
  assert.equal(ta.netValue, 300);
  assert.equal(ta.currency, "USD");
  assert.equal(tb.units, 0);
  assert.equal(ta.reviewRate, null);
  assert.equal(tb.reviewRate, null);
  assert.equal(a.totals.hoursSaved, 5.5);
  assert.equal(a.roiAvailable, true);
  assert.equal(a.estimated, true);
});

test("the review rate comes from the report for a fleet that is on, never invented (#9807)", () => {
  const on = (id: string) => ({ fleet_id: id, status: "ok", automations: ON, members: [] });
  const report = roi([{ id: "a", units: 12, hours_saved: 5.5, net_value: 300, review_rate: 0.25 }]);
  const a = fleetActivity([fleet("a", "A")], { a: on("a") }, report);
  assert.equal(a.tiles[0].reviewRate, 0.25);
  // The same figures as before: the rate adds a line, it changes none of them.
  assert.equal(a.tiles[0].units, 12);
  assert.equal(a.tiles[0].hoursSaved, 5.5);
  assert.equal(a.tiles[0].netValue, 300);
  const off = fleetActivity([fleet("a", "A")], { a: { ...on("a"), automations: OFF } }, report);
  assert.equal(off.tiles[0].reviewRate, null);
  assert.equal(fleetActivity([fleet("a", "A")], { a: on("a") }, null).tiles[0].reviewRate, null);
});

// #9566: fleets are opt-in; a fleet nobody switched on is never "Healthy".
test("the opt-in state comes from the automations, never from the runs", () => {
  assert.equal(fleetOptIn(undefined), null);
  assert.equal(fleetOptIn({ total: 3, enabled: 0, paused: 0 }), "not_set_up");
  assert.equal(fleetOptIn({ total: 3, enabled: 1, paused: 0 }), "partial");
  assert.equal(fleetOptIn({ total: 3, enabled: 3, paused: 0 }), "on");
  assert.equal(fleetOptIn({ total: 3, enabled: 0, paused: 2 }), "paused");
  assert.equal(fleetOptIn({ total: 0, enabled: 0, paused: 0 }), "not_set_up");
});

test("a fleet that is off shows no health, no run figure and no ROI, not zeros", () => {
  const a = fleetActivity(
    [fleet("a", "A")],
    { a: { fleet_id: "a", status: "ok", automations: OFF, members: [member({ runs_in_flight: 0 })] } },
    roi([]),
  );
  const [t] = a.tiles;
  assert.equal(t.optIn, "not_set_up");
  assert.equal(t.status, null);
  assert.equal(t.runsInFlight, null);
  assert.equal(t.units, null);
  assert.equal(t.hoursSaved, null);
  assert.equal(t.netValue, null);
  assert.equal(a.totals.hoursSaved, null);
  assert.equal(a.allOff, true);
});

test("one fleet on is enough for the tiles; a fleet not read yet keeps the invitation away", () => {
  const two = [fleet("a", "A"), fleet("b", "B")];
  const off = (id: string) => ({ fleet_id: id, status: "ok", automations: OFF, members: [] });
  assert.equal(fleetActivity(two, { a: off("a"), b: { ...off("b"), automations: ON } }, null).allOff, false);
  assert.equal(fleetActivity(two, { a: off("a") }, null).allOff, false);
  assert.equal(fleetActivity(two, { a: off("a"), b: off("b") }, null).allOff, true);
});

test("only CTEM fleets: the seeded ctem-* ones and forks of them", () => {
  assert.equal(isCtemFleet({ slug: "ctem-loop", is_builtin: true }), true);
  assert.equal(isCtemFleet({ slug: "concierge", is_builtin: true }), false);
  assert.equal(isCtemFleet({ slug: "my-fork", is_builtin: false, forked_from: CTEM_FORK }), true);
  assert.equal(isCtemFleet({ slug: "ctem-lookalike", is_builtin: false, forked_from: null }), false);
  const a = fleetActivity([fleet("c", "Concierge", { is_builtin: true, slug: "concierge", forked_from: null }), fleet("p", "Posture")], {}, null);
  assert.deepEqual(a.tiles.map((t) => t.id), ["p"]);
});

test("the invitation comes back only for a new fleet or a new module", () => {
  const seen = { fleets: ["ctem-loop"], modules: ["opencti"] };
  assert.equal(inviteVisible(null, ["ctem-loop"], ["opencti"]), true);
  assert.equal(inviteVisible(seen, ["ctem-loop"], ["opencti"]), false);
  assert.equal(inviteVisible(seen, ["ctem-loop"], []), false);
  assert.equal(inviteVisible(seen, ["ctem-loop", "ctem-ai-security"], ["opencti"]), true);
  assert.equal(inviteVisible(seen, ["ctem-loop"], ["opencti", "openaev"]), true);
});
