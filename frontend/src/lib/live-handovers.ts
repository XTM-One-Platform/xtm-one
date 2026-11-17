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
 * Which handover edges of an agent graph read as live (Agentic Flow map, fleet
 * page graph).
 *
 * An edge is live while a handover along it is running or pending, or for
 * `LIVE_HANDOVER_WINDOW_MS` after it ended. The rule is never "one of the last
 * N delegations": with a short feed, one old handover stayed bright for hours
 * on a quiet map while every newer edge was still.
 */

/** How long an ended handover keeps its edge live. */
export const LIVE_HANDOVER_WINDOW_MS = 15 * 60 * 1000;

type LiveHandoverStatus = "pending" | "running" | "completed" | "failed";

/** The fields of a delegation the rule reads. */
export interface LiveHandoverSource {
  parent_agent_id: string;
  target_agent_id: string;
  status: LiveHandoverStatus;
  created_at: string;
  completed_at: string | null;
}

/** What a live edge says about its newest handover. */
export interface LiveHandover {
  status: LiveHandoverStatus;
  /** When the handover fired (ISO). */
  firedAt: string;
}

function timeOf(iso: string | null | undefined): number {
  if (!iso) return Number.NaN;
  return Date.parse(iso);
}

/**
 * Whether one handover keeps its edge live at `now`: running or pending, or
 * ended (completed or failed) at most `LIVE_HANDOVER_WINDOW_MS` ago. An ended
 * handover with no end time is judged on when it fired.
 */
export function isLiveHandover(d: LiveHandoverSource, now: number): boolean {
  if (d.status === "running" || d.status === "pending") return true;
  const ended = timeOf(d.completed_at);
  const at = Number.isNaN(ended) ? timeOf(d.created_at) : ended;
  if (Number.isNaN(at)) return false;
  return now - at <= LIVE_HANDOVER_WINDOW_MS;
}

/**
 * The live edges, keyed "sourceId->targetId" as the graph names them, each
 * with its newest live handover. `nodeOf` maps an agent to the node that draws
 * it (a fleet node when the map groups fleets).
 */
export function liveHandoverEdges(
  delegations: readonly LiveHandoverSource[] | null | undefined,
  now: number,
  nodeOf: (agentId: string) => string = (id) => id,
): Map<string, LiveHandover> {
  const edges = new Map<string, LiveHandover>();
  for (const d of delegations ?? []) {
    if (!isLiveHandover(d, now)) continue;
    const key = `${nodeOf(d.parent_agent_id)}->${nodeOf(d.target_agent_id)}`;
    const kept = edges.get(key);
    if (!kept || timeOf(d.created_at) > timeOf(kept.firedAt)) {
      edges.set(key, { status: d.status, firedAt: d.created_at });
    }
  }
  return edges;
}
