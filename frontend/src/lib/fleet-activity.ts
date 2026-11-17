/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The fleet activity of the CTEM command center (#9050 slice 2): one tile per
 * fleet the reader can see, built from the reads the platform already answers
 * and nothing else, so every number on a tile traces to one endpoint:
 *
 * - `GET /fleets` (`Fleet`): the fleet, its members, whether it is seeded;
 * - `GET /fleets/{id}/health` (`FleetHealth`): runs in flight, recent runs and
 *   failures, open and stale work items, the last run, per member;
 * - `GET /roi` (`RoiReport`, admins only): units produced, hours saved and net
 *   value per fleet, with the report's assumptions.
 *
 * A figure the reader cannot read stays `null` (a health read still loading or
 * refused, the ROI report for a non-admin), never 0: the tile says "not
 * available" instead of a number nobody measured.
 *
 * Fleets are opt-in (#9160) and most organizations keep them off for quota, so
 * the tiles cover the CTEM fleets only (seeded `ctem-*` fleets and forks of
 * them, #9566) and each says whether it is set up (`optIn`, from the
 * automations' enabled state of `FleetHealth.automations`) before any run
 * health: a fleet nobody switched on is "Not set up", never "Healthy", and its
 * ROI figures are not shown at all.
 */

/** The fields of `Fleet` (`hooks/api/useFleetApi.ts`) a tile reads. */
interface ActivityFleet {
  id: string;
  name: string;
  slug?: string;
  description?: string;
  icon: string;
  color: string;
  is_builtin?: boolean;
  is_archived: boolean;
  member_count: number;
  forked_from?: { slug: string; is_builtin: boolean } | null;
}

/** The slug prefix of the seeded CTEM fleets (`backend/app/agents/builtin_fleets.py`). */
const CTEM_FLEET_SLUG_PREFIX = "ctem-";

/** A CTEM fleet: a seeded `ctem-*` fleet, or a fleet forked from one. */
export function isCtemFleet(fleet: Pick<ActivityFleet, "slug" | "is_builtin" | "forked_from">): boolean {
  if (fleet.is_builtin) return !!fleet.slug?.startsWith(CTEM_FLEET_SLUG_PREFIX);
  const origin = fleet.forked_from;
  return !!origin?.is_builtin && origin.slug.startsWith(CTEM_FLEET_SLUG_PREFIX);
}

/** Which fleets a home shows with no choice saved: the CTEM fleets (command center) or every fleet (Copilot home, #12063). */
export type FleetDefault = "ctem" | "all";

/**
 * The fleets a home shows (#11464): with no choice saved
 * (`users.ctem_dashboard_fleet_ids` or `users.copilot_dashboard_fleet_ids`
 * null), the default (`FleetDefault`); with a choice, the fleets it names, in
 * its order, built-in or custom. Archived fleets and ids that name no fleet the
 * reader sees are left out.
 */
export function dashboardFleets<F extends Pick<ActivityFleet, "id" | "slug" | "is_builtin" | "forked_from" | "is_archived">>(
  fleets: readonly F[] | undefined,
  chosenIds: readonly string[] | null | undefined,
  fallback: FleetDefault = "ctem",
): F[] {
  const active = (fleets ?? []).filter((f) => !f.is_archived);
  if (chosenIds == null) return fallback === "all" ? active : active.filter((f) => isCtemFleet(f));
  const byId = new Map(active.map((f) => [f.id, f]));
  return chosenIds.map((id) => byId.get(id)).filter((f): f is F => f !== undefined);
}

/** `FleetHealth.automations`: the fleet's automations, on and paused. */
interface ActivityAutomations {
  total: number;
  enabled: number;
  paused: number;
  /** The names of the automations that are not on, sorted (#11996); absent on an older backend. */
  off_names?: string[];
}

/** Whether a fleet is set up, from its automations, never from its runs. */
export type FleetOptIn = "not_set_up" | "partial" | "on" | "paused";

/**
 * The opt-in state of a fleet: every automation on is "on", some is
 * "partial", none on but one switched off after being on is "paused",
 * otherwise "not_set_up" (as seeded). `null` while the health is not read.
 */
export function fleetOptIn(automations: ActivityAutomations | null | undefined): FleetOptIn | null {
  if (!automations) return null;
  const { total, enabled, paused } = automations;
  if (total > 0 && enabled >= total) return "on";
  if (enabled > 0) return "partial";
  if (paused > 0) return "paused";
  return "not_set_up";
}

/** A fleet that runs: its health and figures are shown. */
export const isFleetOn = (optIn: FleetOptIn | null): boolean => optIn === "on" || optIn === "partial";

/** The fields of `FleetMemberHealth` a tile sums. */
interface ActivityMemberHealth {
  /** The member, named on the events timeline (`lib/ctem-fleet-timeline.ts`). */
  agent_id?: string;
  name?: string;
  /** The member's health verdict (`ok`, `idle`, `stale`, `failing`, `disabled`). */
  status?: string;
  last_run_at: string | null;
  last_run_status: string | null;
  runs_in_flight: number;
  failures_24h?: number;
  recent_runs?: number;
  open_work_items: number;
  stale_work_items: number;
}

/** The fields of `FleetHealth` a tile reads. */
export interface ActivityFleetHealth {
  fleet_id: string;
  status: string;
  members: ActivityMemberHealth[];
  automations?: ActivityAutomations;
  /**
   * The fleet's runs of the last 30 days (#10123): `by_day` one count per day, oldest first;
   * `succeeded_by_day` / `failed_by_day` the same days per outcome and `last_summary` the
   * one-line result of the newest ended run (#11996).
   */
  run_stats?: { by_day?: number[]; succeeded_by_day?: number[]; failed_by_day?: number[]; last_summary?: string | null };
}

/** The days the home Fleets widget draws (#11996): the last 7, today last. */
export const WIDGET_DAYS = 7;

/** One group's figures of `GET /roi` (`roi_report.figures_dict`). */
interface RoiFigures {
  currency: string;
  runs: number;
  units: number;
  units_by_kind: Record<string, number>;
  minutes_saved: number;
  hours_saved: number;
  llm_cost: number;
  value: number;
  net_value: number;
  roi: number | null;
  payback_months: number | null;
  /** `reviewed / runs`, `null` without a run (#9310); absent on an older report. */
  review_rate?: number | null;
  assumptions: string[];
}

/** `GET /roi` (`roi_report.report_dict`). */
export interface RoiReport {
  window: string;
  since: string | null;
  currency: string;
  rate_label: string;
  estimated: boolean;
  organization: RoiFigures;
  agents: (RoiFigures & { id: string | null; name: string })[];
  fleets: (RoiFigures & { id: string | null; name: string })[];
}

/** What one fleet tile shows; `null` = not readable by this reader yet. */
export interface FleetActivityTile {
  id: string;
  name: string;
  slug: string;
  description: string;
  /** Whether the fleet is set up; `null` while its health is not read. */
  optIn: FleetOptIn | null;
  icon: string;
  color: string;
  builtin: boolean;
  members: number;
  /** The fleet health status (`ok`, `idle`, `stale`, `failing`, `disabled`); `null` for a fleet that is not on. */
  status: string | null;
  runsInFlight: number | null;
  recentRuns: number | null;
  failures24h: number | null;
  openWorkItems: number | null;
  staleWorkItems: number | null;
  lastRunAt: string | null;
  lastRunStatus: string | null;
  units: number | null;
  hoursSaved: number | null;
  netValue: number | null;
  currency: string | null;
  /** Share of the fleet's counted runs a person reviewed, 0 to 1 (#9807); `null` when unreadable or without a run. */
  reviewRate: number | null;
  /** Runs per day over the last 30 days, oldest first, for the fleet's sparkline (#11547 S2d); absent or `null` for a fleet that is not on or whose health is not read. */
  runsByDay?: number[] | null;
  /** The fleet's automations on and in all; `null` while its health is not read (#11996). */
  automationsOn?: number | null;
  automationsTotal?: number | null;
  /** The names of its automations that are off, sorted (#11996). */
  offAutomations?: string[];
  /** The last {@link WIDGET_DAYS} days, oldest first: succeeded and failed runs per day; `null` for a fleet that is not on. */
  succeededByDay?: number[] | null;
  failedByDay?: number[] | null;
  /** Its members whose health is failing, and its members read (#11996). */
  failingMembers?: number | null;
  memberCount?: number | null;
  /** The one-line result of its newest ended run; `null` when none has one. */
  lastSummary?: string | null;
}

export interface FleetActivity {
  tiles: FleetActivityTile[];
  /** Sums over the tiles whose figure is known; `null` when none is. */
  totals: {
    runsInFlight: number | null;
    recentRuns: number | null;
    openWorkItems: number | null;
    hoursSaved: number | null;
  };
  /** Whether the ROI figures were readable (admins, licence). */
  roiAvailable: boolean;
  estimated: boolean;
  /** Every CTEM fleet is read and none is on or paused: the card invites the setup instead of tiles. */
  allOff: boolean;
}

/** One row of a fleet documentation's `structured.quota` (`DocQuota`, `services/fleet_docs.py`). */
export interface DocumentedQuota {
  trigger?: string;
  runs_min?: number;
  runs_max?: number;
}

/**
 * A fleet's documented runs per month (UX round 1 on #9570, B2): the sum of
 * its triggers' `runs_min` to `runs_max`, an estimate written with the fleet.
 * `null` when the fleet documents none or every range is 0, so the card never
 * says "No run expected" for a fleet with scheduled triggers.
 */
export function documentedRunRange(quota: readonly DocumentedQuota[] | null | undefined): { min: number; max: number } | null {
  if (!Array.isArray(quota) || quota.length === 0) return null;
  let min = 0;
  let max = 0;
  for (const row of quota) {
    min += Math.max(0, Number(row?.runs_min) || 0);
    max += Math.max(0, Number(row?.runs_max) || 0);
  }
  return max > 0 ? { min, max: Math.max(min, max) } : null;
}

/** What the person saw when they closed the invitation (`users.ctem_fleet_invite_dismissed`). */
export interface FleetInviteDismissal {
  fleets: string[];
  modules: string[];
}

/**
 * Whether the setup invitation shows: never closed, or a fleet or a connected
 * module appeared since it was closed (a new fleet shipped, a new product
 * connected).
 */
export function inviteVisible(
  dismissed: FleetInviteDismissal | null | undefined,
  fleetSlugs: readonly string[],
  modules: readonly string[],
): boolean {
  if (!dismissed) return true;
  const fleets = new Set(dismissed.fleets ?? []);
  const seen = new Set(dismissed.modules ?? []);
  return fleetSlugs.some((s) => !fleets.has(s)) || modules.some((m) => !seen.has(m));
}

const sum = (values: (number | null)[]): number | null => {
  const known = values.filter((v): v is number => v != null);
  return known.length === 0 ? null : known.reduce((a, b) => a + b, 0);
};

/** The fleet's runs per day, oldest first, `null` when the health does not carry them. */
function runsByDay(health: ActivityFleetHealth | undefined): number[] | null {
  const days = health?.run_stats?.by_day;
  return Array.isArray(days) ? days : null;
}

/** The last {@link WIDGET_DAYS} entries of a per-day series, left-padded with 0; `null` when absent. */
export function lastDays(series: readonly number[] | null | undefined, days = WIDGET_DAYS): number[] | null {
  if (!Array.isArray(series)) return null;
  const tail = series.slice(-days).map((v) => Math.max(0, Number(v) || 0));
  return [...Array<number>(Math.max(0, days - tail.length)).fill(0), ...tail];
}

/**
 * Succeeded runs of the ended ones, 0 to 1, over the series given; `null` when
 * no run ended (the widget says "No runs", never 0 % or a dash).
 */
export function successRate(succeeded: readonly number[] | null | undefined, failed: readonly number[] | null | undefined): number | null {
  if (!succeeded || !failed) return null;
  const ok = succeeded.reduce((a, b) => a + b, 0);
  const ended = ok + failed.reduce((a, b) => a + b, 0);
  return ended === 0 ? null : ok / ended;
}

/**
 * How many columns the last of `count` tiles spans in the widget's grid, for the 2-column and
 * the 3-column widths, so a row never ends on an empty area (round 1 B5 on #12015): in 2 columns
 * an odd last tile spans both; in 3 columns a last tile alone spans 3 and the last of two spans 2.
 */
export function lastTileSpan(count: number): { two: 1 | 2; three: 1 | 2 | 3 } {
  const two = count > 1 && count % 2 === 1 ? 2 : 1;
  const rest = count % 3;
  const three = count <= 2 ? two : rest === 1 ? 3 : rest === 2 ? 2 : 1;
  return { two, three };
}

/** The header line of the widget (#11996): fleets on, their runs over the last 7 days and the share that succeeded. */
export function fleetsSummary(tiles: readonly FleetActivityTile[]): { fleets: number; runs: number; rate: number | null } {
  const on = tiles.filter((t) => t.succeededByDay && t.failedByDay);
  const ok = on.flatMap((t) => t.succeededByDay ?? []);
  const failed = on.flatMap((t) => t.failedByDay ?? []);
  const runs = ok.reduce((a, b) => a + b, 0) + failed.reduce((a, b) => a + b, 0);
  return { fleets: tiles.length, runs, rate: successRate(ok, failed) };
}

/**
 * The attention rank of a tile on the Copilot home (#12063, UX design round 2
 * B3): a failing fleet first, then one with stale work items, then one with
 * automations off, then the rest; 3 for a fleet that is not on.
 */
function attentionRank(tile: FleetActivityTile): number {
  if (!isFleetOn(tile.optIn)) return 3;
  if (tile.status === "failing" || (tile.failingMembers ?? 0) > 0) return 0;
  if (tile.status === "stale" || (tile.staleWorkItems ?? 0) > 0) return 1;
  if (tile.automationsTotal != null && tile.automationsOn != null && tile.automationsOn < tile.automationsTotal) return 2;
  return 3;
}

const weekRuns = (tile: FleetActivityTile): number =>
  [...(tile.succeededByDay ?? []), ...(tile.failedByDay ?? [])].reduce((a, b) => a + b, 0);

/**
 * The Copilot home's default order (#12063, round 2 B3): attention first
 * (`attentionRank`), then the most runs over 7 days, then by name. A saved
 * choice keeps the person's order: the caller skips this sort.
 */
export function byAttention(tiles: readonly FleetActivityTile[]): FleetActivityTile[] {
  return tiles
    .slice()
    .sort((a, b) => attentionRank(a) - attentionRank(b) || weekRuns(b) - weekRuns(a) || a.name.localeCompare(b.name));
}

/** The most recent run of the members, with its status. */
function lastRun(members: ActivityMemberHealth[]): { at: string | null; status: string | null } {
  let at: string | null = null;
  let status: string | null = null;
  for (const m of members) {
    if (m.last_run_at && (at === null || Date.parse(m.last_run_at) > Date.parse(at))) {
      at = m.last_run_at;
      status = m.last_run_status;
    }
  }
  return { at, status };
}

/**
 * One tile per fleet the page shows (`dashboardFleets`): with no choice, the
 * seeded fleets first, then the custom ones, each group by name; with a
 * choice, in the person's order. Archived fleets are left out.
 */
export function fleetActivity(
  fleets: readonly ActivityFleet[] | undefined,
  healthById: Readonly<Record<string, ActivityFleetHealth | undefined>>,
  roi: RoiReport | null | undefined,
  chosenIds: readonly string[] | null = null,
  fallback: FleetDefault = "ctem",
): FleetActivity {
  const roiByFleet = new Map((roi?.fleets ?? []).filter((f) => f.id).map((f) => [f.id as string, f]));
  const ordered = chosenIds != null;
  const tiles = dashboardFleets(fleets, chosenIds, fallback)
    .map((fleet): FleetActivityTile => {
      const health = healthById[fleet.id];
      const optIn = fleetOptIn(health?.automations);
      const on = isFleetOn(optIn);
      // A fleet that is off shows no run figure and no ROI: a 0 there would
      // read as a broken product, not as a fleet nobody switched on.
      const members = on ? (health?.members ?? null) : null;
      const figures = roi ? roiByFleet.get(fleet.id) : undefined;
      const roiShown = !!roi && on;
      const last = members ? lastRun(members) : { at: null, status: null };
      const total = (pick: (m: ActivityMemberHealth) => number | undefined) =>
        members ? members.reduce((acc, m) => acc + (pick(m) ?? 0), 0) : null;
      return {
        id: fleet.id,
        name: fleet.name,
        slug: fleet.slug ?? "",
        description: fleet.description ?? "",
        optIn,
        icon: fleet.icon,
        color: fleet.color,
        builtin: !!fleet.is_builtin,
        members: fleet.member_count,
        status: on ? (health?.status ?? null) : null,
        runsInFlight: total((m) => m.runs_in_flight),
        recentRuns: total((m) => m.recent_runs),
        failures24h: total((m) => m.failures_24h),
        openWorkItems: total((m) => m.open_work_items),
        staleWorkItems: total((m) => m.stale_work_items),
        lastRunAt: last.at,
        lastRunStatus: last.status,
        // A fleet the report does not name produced nothing in its window: 0,
        // not unknown, once the report itself was readable.
        units: roiShown ? (figures?.units ?? 0) : null,
        hoursSaved: roiShown ? (figures?.hours_saved ?? 0) : null,
        netValue: roiShown ? (figures?.net_value ?? 0) : null,
        currency: roiShown && roi ? roi.currency : null,
        reviewRate: roiShown ? (figures?.review_rate ?? null) : null,
        runsByDay: on ? runsByDay(health) : null,
        automationsOn: health?.automations ? health.automations.enabled : null,
        automationsTotal: health?.automations ? health.automations.total : null,
        offAutomations: health?.automations?.off_names ?? [],
        succeededByDay: on ? lastDays(health?.run_stats?.succeeded_by_day) : null,
        failedByDay: on ? lastDays(health?.run_stats?.failed_by_day) : null,
        failingMembers: members ? members.filter((m) => m.status === "failing").length : null,
        memberCount: members ? members.length : null,
        lastSummary: on ? (health?.run_stats?.last_summary ?? null) : null,
      };
    })
    .sort((a, b) => (ordered ? 0 : Number(b.builtin) - Number(a.builtin) || a.name.localeCompare(b.name)));
  return {
    tiles,
    totals: {
      runsInFlight: sum(tiles.map((t) => t.runsInFlight)),
      recentRuns: sum(tiles.map((t) => t.recentRuns)),
      openWorkItems: sum(tiles.map((t) => t.openWorkItems)),
      hoursSaved: roi ? sum(tiles.map((t) => t.hoursSaved)) : null,
    },
    roiAvailable: !!roi,
    estimated: roi?.estimated ?? true,
    allOff: tiles.length > 0 && tiles.every((t) => t.optIn === "not_set_up"),
  };
}
