// SPDX-License-Identifier: Apache-2.0

/**
 * The Done for you timeline of the CTEM command center v2 (C2 slice V3, design
 * dev-docs/ctem-command-center-v2.md section 4.3): what the platform did over
 * the last 7 or 30 days, on a horizontal time axis with one lane per source.
 * Fleets off is the primary case: the module events and the built-in
 * automations' runs fill it; the fleets lane is drawn only when a fleet ran.
 * Pure: the card gives each point its words and its place.
 */
import type { ActivityEvent } from "./activity-feed";

/** The lanes, top to bottom. */
export type DfyLane = "modules" | "automations" | "fleets" | "outcomes";
const DFY_LANES: readonly DfyLane[] = ["modules", "automations", "fleets", "outcomes"];

/** The colour of a point, named in the legend. */
export type DfyTone = "module" | "automation" | "fleet" | "outcome" | "attention";

/** The periods of the toggle, in days. */
export const DFY_PERIODS = [7, 30] as const;
export type DfyPeriod = (typeof DFY_PERIODS)[number];

/** More points than this in one day of one lane become one count badge. */
export const DFY_CLUSTER_AT = 3;

const DAY_MS = 24 * 60 * 60 * 1000;

/** One point of the timeline. */
export interface DfyPoint {
  id: string;
  lane: DfyLane;
  at: string;
  tone: DfyTone;
  /** What the card says of it: a module event, a run or an outcome. */
  subject:
    | { kind: "event"; event: ActivityEvent }
    | { kind: "run"; name: string; agent: string; outcome: "ok" | "failed" | "interrupted" | "active" | "other" | null }
    | { kind: "actions"; count: number; name: string }
    | { kind: "digest"; name: string };
  /** The run the point opens, when it is one or comes from one. */
  runId: string | null;
  /** The point it came from (a run for its outcome): drawn as a dashed link. */
  from: string | null;
}

/** The fields of a run the timeline reads (`AssignmentRunDetail`). */
export interface DfyRun {
  id: string;
  agent_name: string;
  assignment_name: string | null;
  status: string;
  outcome?: "ok" | "failed" | "interrupted" | "active" | "other" | null;
  actions_taken: unknown[] | null;
  created_at: string;
  completed_at: string | null;
}

/** The fields of a digest the timeline reads (`DigestItem`). */
export interface DfyDigest {
  id: string;
  agent_name: string;
  assignment_name: string;
  created_at: string;
}

/** The fields of a fleet run the timeline reads (`TimelineEntry`). */
export interface DfyFleetRun {
  runId: string;
  fleetName: string;
  agentName: string | null;
  outcome: string | null;
  at: string;
}

function valid(at: string | null | undefined): at is string {
  return typeof at === "string" && !Number.isNaN(Date.parse(at));
}

type RunOutcome = Exclude<DfyRun["outcome"], undefined>;

function runOutcome(run: DfyRun): RunOutcome {
  if (run.outcome !== undefined) return run.outcome;
  if (run.status === "completed") return "ok";
  if (run.status === "failed") return "failed";
  if (run.status === "running" || run.status === "pending" || run.status === "queued") return "active";
  return "other";
}

const FLEET_OUTCOMES: Record<string, RunOutcome> = {
  succeeded: "ok", failed: "failed", interrupted: "interrupted", running: "active", pending: "active", cancelled: "other",
};

/**
 * Every point of the timeline, newest first: the module events, the runs of
 * the automations (and of the fleets when given), and the outcome each run
 * records (actions taken, a digest published). A run seen twice is listed
 * once; a fleet run is never also an automation run.
 */
export function doneForYouPoints(input: {
  events: readonly ActivityEvent[];
  runs: readonly DfyRun[];
  digests: readonly DfyDigest[];
  fleetRuns: readonly DfyFleetRun[];
}): DfyPoint[] {
  const points: DfyPoint[] = [];
  for (const event of input.events) {
    if (!valid(event.at)) continue;
    const attention =
      event.kind === "platform_unhealthy" || (event.kind === "validation" && event.met < event.total);
    points.push({
      id: `event:${event.id}`, lane: "modules", at: event.at, tone: attention ? "attention" : "module",
      subject: { kind: "event", event }, runId: null, from: null,
    });
  }

  const fleetIds = new Set(input.fleetRuns.map((r) => r.runId));
  const seen = new Set<string>();
  for (const run of input.fleetRuns) {
    if (!valid(run.at) || seen.has(run.runId)) continue;
    seen.add(run.runId);
    const outcome = FLEET_OUTCOMES[run.outcome ?? ""] ?? null;
    points.push({
      id: `run:${run.runId}`, lane: "fleets", at: run.at,
      tone: outcome === "failed" ? "attention" : "fleet",
      subject: { kind: "run", name: run.fleetName, agent: run.agentName ?? "", outcome },
      runId: run.runId, from: null,
    });
  }

  for (const run of input.runs) {
    if (!valid(run.created_at) || seen.has(run.id) || fleetIds.has(run.id)) continue;
    seen.add(run.id);
    const outcome = runOutcome(run);
    const name = run.assignment_name ?? run.agent_name;
    points.push({
      id: `run:${run.id}`, lane: "automations", at: run.created_at,
      tone: outcome === "failed" ? "attention" : "automation",
      subject: { kind: "run", name, agent: run.agent_name, outcome },
      runId: run.id, from: null,
    });
    const actions = run.actions_taken?.length ?? 0;
    const doneAt = valid(run.completed_at) ? run.completed_at : run.created_at;
    if (outcome === "ok" && actions > 0) {
      points.push({
        id: `outcome:${run.id}`, lane: "outcomes", at: doneAt, tone: "outcome",
        subject: { kind: "actions", count: actions, name }, runId: run.id, from: `run:${run.id}`,
      });
    }
  }

  for (const digest of input.digests) {
    if (!valid(digest.created_at) || seen.has(`digest:${digest.id}`)) continue;
    seen.add(`digest:${digest.id}`);
    const fromRun = seen.has(digest.id) ? `run:${digest.id}` : null;
    if (!fromRun) {
      seen.add(digest.id);
      points.push({
        id: `run:${digest.id}`, lane: "automations", at: digest.created_at, tone: "automation",
        subject: { kind: "run", name: digest.assignment_name, agent: digest.agent_name, outcome: "ok" },
        runId: digest.id, from: null,
      });
    }
    points.push({
      id: `digest:${digest.id}`, lane: "outcomes", at: digest.created_at, tone: "outcome",
      subject: { kind: "digest", name: digest.assignment_name }, runId: digest.id, from: `run:${digest.id}`,
    });
  }

  return points.sort((a, b) => Date.parse(b.at) - Date.parse(a.at) || a.id.localeCompare(b.id));
}

/** The day of a point on the axis: 0 is the oldest day of the period, `days - 1`
 *  today; `null` outside the period (older, or in the future). */
export function dayIndex(at: string, days: number, now: number): number | null {
  const startOfToday = new Date(now);
  startOfToday.setHours(0, 0, 0, 0);
  const t = Date.parse(at);
  if (Number.isNaN(t) || t > now) return null;
  const start = startOfToday.getTime();
  const back = t >= start ? 0 : Math.ceil((start - t) / DAY_MS);
  const day = days - 1 - back;
  return day >= 0 ? day : null;
}

/** The points of one day of one lane. */
export interface DfyCell {
  lane: DfyLane;
  day: number;
  points: DfyPoint[];
  /** More than `DFY_CLUSTER_AT` points: drawn as one count badge. */
  clustered: boolean;
}

/** The lanes drawn (a lane with no point in the period is not drawn, the
 *  modules and automations lanes always are: they are the fleets-off sources)
 *  and their cells, oldest day first. */
export function doneForYouCells(
  points: readonly DfyPoint[],
  days: number,
  now: number,
): { lanes: DfyLane[]; cells: DfyCell[]; count: number } {
  const byKey = new Map<string, DfyCell>();
  let count = 0;
  for (const point of points) {
    const day = dayIndex(point.at, days, now);
    if (day === null) continue;
    count += 1;
    const key = `${point.lane}:${day}`;
    const cell = byKey.get(key) ?? { lane: point.lane, day, points: [], clustered: false };
    cell.points.push(point);
    byKey.set(key, cell);
  }
  const cells = [...byKey.values()]
    .map((cell) => ({
      ...cell,
      points: [...cell.points].sort((a, b) => Date.parse(a.at) - Date.parse(b.at) || a.id.localeCompare(b.id)),
      clustered: cell.points.length > DFY_CLUSTER_AT,
    }))
    .sort((a, b) => DFY_LANES.indexOf(a.lane) - DFY_LANES.indexOf(b.lane) || a.day - b.day);
  const used = new Set(cells.map((c) => c.lane));
  const lanes = DFY_LANES.filter((lane) => lane === "modules" || lane === "automations" || used.has(lane));
  return { lanes, cells, count };
}
