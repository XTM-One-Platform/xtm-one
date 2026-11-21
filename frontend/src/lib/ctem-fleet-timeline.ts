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
 * The events timeline of the CTEM command center (#9050 slice 3): what the
 * fleets did lately and what started each run, merged across the fleets the
 * reader can see, newest first. Every row traces to one read:
 *
 * - `GET /fleets/{id}/activity` (`runs`): the members' latest runs the reader
 *   may see, with what started each (`trigger_type`, and for an integration
 *   event its `trigger_provider` and `trigger_event`);
 * - `GET /fleets/{id}/health` (`members`): the member names.
 *
 * A run seen through two fleets (an agent connected to both) is listed once,
 * under the first fleet that names it.
 */

/** One run of `GET /fleets/{id}/activity` (`FleetRunEntry`). */
export interface TimelineRun {
  id: string;
  agent_id: string;
  assignment_id: string | null;
  status: string;
  created_at: string;
  completed_at: string | null;
  outcome?: string;
  trigger_type?: string | null;
  trigger_provider?: string | null;
  trigger_event?: string | null;
  /** The CTEM case the run works on, when a case event started it (#915). */
  case_id?: string | null;
}

/** `GET /fleets/{id}/activity`. */
export interface TimelineFleetActivity {
  fleet_id: string;
  runs: TimelineRun[];
}

/** The fields of a fleet the timeline reads. */
interface TimelineFleet {
  id: string;
  name: string;
  is_archived: boolean;
}

/** The member names of `GET /fleets/{id}/health`. */
interface TimelineHealth {
  members: { agent_id?: string; name?: string }[];
}

/**
 * Where a run came from. `opencti`, `openaev` and `opencrq` are the XTM
 * Platform modules; `integration` any other integration event.
 */
export type TimelineSource =
  | "opencti"
  | "openaev"
  | "opencrq"
  | "integration"
  | "schedule"
  | "work_item"
  | "chat"
  | "webhook"
  | "platform_event"
  | "delegation"
  | "other";

export interface TimelineEntry {
  runId: string;
  fleetId: string;
  fleetName: string;
  agentId: string;
  /** `null` when the fleet health that names the member is not read yet. */
  agentName: string | null;
  status: string;
  /** The badge (`timelineOutcome`), `null` when none applies. */
  outcome: TimelineOutcome | null;
  at: string;
  source: TimelineSource;
  /** The integration event the run answered (`report.created`...), when known. */
  event: string | null;
  /** The CTEM case the run works on (#915), `null` for any other run. */
  caseId: string | null;
}

const MODULES = new Set(["opencti", "openaev", "opencrq"]);

/**
 * The source of a run from its trigger fields: every `trigger_context.type` the
 * backend writes (`schedule`, `schedule_fanout`, `manual`, `integration_event`,
 * `webhook`, `work_item`, `scheduled_work_item`, `chat_handover`, `chain`,
 * `delegation`, `event`) has its place; `manual` and the flow `event` read
 * "Manual or other".
 */
export function timelineSource(run: Pick<TimelineRun, "trigger_type" | "trigger_provider">): TimelineSource {
  const type = run.trigger_type ?? null;
  const provider = (run.trigger_provider ?? "").toLowerCase();
  if (type === "integration_event") {
    return MODULES.has(provider) ? (provider as TimelineSource) : "integration";
  }
  switch (type) {
    case "schedule":
    case "schedule_fanout":
      return "schedule";
    case "work_item":
    case "scheduled_work_item":
      return "work_item";
    case "chat":
    case "chat_handover":
      return "chat";
    case "webhook":
      return "webhook";
    case "platform_event":
      return "platform_event";
    case "chain":
    case "delegation":
    case "sub_agent":
      return "delegation";
    default:
      return "other";
  }
}

/** The badge of one run. */
export type TimelineOutcome = "succeeded" | "failed" | "interrupted" | "cancelled" | "running" | "pending";

const PENDING_STATUSES = new Set(["pending", "queued"]);

/**
 * The badge of a run from the API's `outcome` (`run_outcome.run_outcome`: `ok`,
 * `failed`, `interrupted`, `active`, `other`) and its `status`; `null` when the
 * run ended in a way no badge names (skipped, dismissed...). A run read before
 * `outcome` existed falls back to its status.
 */
export function timelineOutcome(outcome: string | null | undefined, status: string): TimelineOutcome | null {
  switch (outcome) {
    case "ok":
      return "succeeded";
    case "failed":
      return "failed";
    case "interrupted":
      return "interrupted";
    case "active":
      return PENDING_STATUSES.has(status) ? "pending" : "running";
    case "other":
      return status === "cancelled" ? "cancelled" : null;
  }
  switch (status) {
    case "completed":
      return "succeeded";
    case "failed":
      return "failed";
    case "cancelled":
      return "cancelled";
    case "running":
    case "awaiting_human":
      return "running";
    default:
      return PENDING_STATUSES.has(status) ? "pending" : null;
  }
}

/** Rows shown at most. */
const TIMELINE_LIMIT = 30;

/**
 * The runs of every active fleet, newest first, at most `limit`, a run
 * listed once whatever the number of fleets that see it.
 */
export function fleetTimeline(
  fleets: readonly TimelineFleet[] | undefined,
  activityById: Readonly<Record<string, TimelineFleetActivity | undefined>>,
  healthById: Readonly<Record<string, TimelineHealth | undefined>>,
  limit: number = TIMELINE_LIMIT,
): TimelineEntry[] {
  const seen = new Set<string>();
  const entries: TimelineEntry[] = [];
  for (const fleet of fleets ?? []) {
    if (fleet.is_archived) continue;
    const names = new Map<string, string>();
    for (const m of healthById[fleet.id]?.members ?? []) {
      if (m.agent_id && m.name) names.set(m.agent_id, m.name);
    }
    for (const run of activityById[fleet.id]?.runs ?? []) {
      if (seen.has(run.id)) continue;
      seen.add(run.id);
      entries.push({
        runId: run.id,
        fleetId: fleet.id,
        fleetName: fleet.name,
        agentId: run.agent_id,
        agentName: names.get(run.agent_id) ?? null,
        status: run.status,
        outcome: timelineOutcome(run.outcome, run.status),
        at: run.created_at,
        source: timelineSource(run),
        event: run.trigger_type === "integration_event" ? (run.trigger_event ?? null) : null,
        caseId: run.case_id ?? null,
      });
    }
  }
  entries.sort((a, b) => Date.parse(b.at) - Date.parse(a.at) || a.runId.localeCompare(b.runId));
  return entries.slice(0, limit);
}
