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
 * The handover edits a graph offers (draw, retype, delete an edge), as pure
 * plans, shared by the agent-to-agent map and the fleet page graph.
 * The hook `useHandoverEdgeEditing` runs them.
 *
 * A handover is a field of its source agent (`handover_agent_ids`,
 * `handover_types`): an edge edit is an agent update, checked by the server
 * like any other. The one refusal a graph meets in practice is the fleet
 * boundary: a new handover to a member another fleet does not expose answers
 * 422 with the reason (`refused_cross_fleet_handovers`), which the graph
 * shows as it is rather than a bare "failed".
 */

import { ALL_HANDOVER_TYPES, runsEveryHandoverType, withHandoverEdge, withoutHandoverEdge } from "@/lib/handover-types";
import { canManageEntity } from "@/lib/utils";
import type { Agent } from "@/types";

/** What an edge edit writes on its source agent, and the targets it narrows to delegate. */
export interface HandoverEdgePlan {
  ids: string[];
  types: Record<string, string>;
  narrowed: string[];
}

/**
 * The plan for drawing `source -> targetId` with `handoverType`, or
 * `"duplicate"` when that exact handover already exists. A target that exists
 * with another type is retyped.
 */
export function planHandoverEdgeAdd(
  source: Pick<Agent, "handover_agent_ids" | "handover_types">,
  targetId: string,
  handoverType: string,
): HandoverEdgePlan | "duplicate" {
  const currentIds = source.handover_agent_ids ?? [];
  const currentTypes = source.handover_types ?? {};
  const sameType = runsEveryHandoverType(currentIds, currentTypes)
    ? handoverType === ALL_HANDOVER_TYPES
    : currentTypes[targetId] === handoverType;
  if (currentIds.includes(targetId) && sameType) return "duplicate";
  return withHandoverEdge(currentIds, currentTypes, targetId, handoverType);
}

/** The plan for retyping `source -> targetId`, or removing it when `newTypeOrDelete` is `"delete"`. */
export function planHandoverEdgeEdit(
  source: Pick<Agent, "handover_agent_ids" | "handover_types">,
  targetId: string,
  newTypeOrDelete: string,
): HandoverEdgePlan {
  const currentIds = source.handover_agent_ids ?? [];
  const currentTypes = source.handover_types ?? {};
  if (newTypeOrDelete === "delete") {
    return { ...withoutHandoverEdge(currentIds, currentTypes, targetId), narrowed: [] };
  }
  return withHandoverEdge(currentIds, currentTypes, targetId, newTypeOrDelete);
}

/**
 * The agents a graph lets the reader draw handovers from: the server's
 * tier-aware `can_manage`, minus the built-in agents of XTM One mode.
 */
export function canEditHandoversOf(
  agent: Agent,
  userId: string | undefined,
  isAdmin: boolean,
  isXtmOne: boolean,
): boolean {
  if (agent.is_builtin && isXtmOne) return false;
  return canManageEntity(agent, userId, isAdmin);
}

/** Statuses whose message is the server's own reason, fit to show the user. */
const REASONED_STATUSES = new Set([400, 403, 409, 422]);

/**
 * The server's reason for refusing a handover write (the fleet boundary's
 * 422 above all), or `undefined` for a failure with no reason to show (a 5xx,
 * a network error), which keeps the generic message.
 */
export function handoverWriteRefusal(error: unknown): string | undefined {
  const { status, message } = (error ?? {}) as { status?: number; message?: unknown };
  if (status === undefined || !REASONED_STATUSES.has(status)) return undefined;
  return typeof message === "string" && message.trim() !== "" ? message : undefined;
}
