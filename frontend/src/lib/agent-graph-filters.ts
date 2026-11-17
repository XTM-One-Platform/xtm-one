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
 * Filters of the agent graph toolbar (Agentic Flow map).
 *
 * Pure: which agents the map draws for the filters chosen. An agent's kind is
 * built-in, company-managed or personal; "enabled" keeps the enabled or the
 * disabled ones; the two activity filters keep the agents that own an open
 * work item or have a run in flight. A filter left at its default keeps
 * everyone, so the default filters draw the whole map.
 *
 * Ownership is not one of these filters: the map's quick filter Mine | Shared |
 * Company | All (`agentsInVisibility`, the shared convention of
 * `lib/visibility.ts`) sits on the toolbar row itself and runs first.
 */

import { inSegment, type Visibility, type VisibilityFields } from "@/lib/visibility";

export type AgentKind = "builtin" | "company" | "personal";

export const AGENT_KINDS: readonly AgentKind[] = ["builtin", "company", "personal"];

export type EnabledFilter = "all" | "enabled" | "disabled";

export interface AgentGraphFilters {
  /** Kinds drawn; empty draws every kind. */
  kinds: AgentKind[];
  enabled: EnabledFilter;
  withOpenWorkItems: boolean;
  withRunsInFlight: boolean;
}

export const DEFAULT_GRAPH_FILTERS: AgentGraphFilters = {
  kinds: [],
  enabled: "all",
  withOpenWorkItems: false,
  withRunsInFlight: false,
};

/** The fields of an agent the filters read. */
export interface FilterableAgent {
  id: string;
  enabled: boolean;
  is_builtin: boolean;
  is_company_managed: boolean;
}

export function agentKind(agent: FilterableAgent): AgentKind {
  if (agent.is_builtin) return "builtin";
  if (agent.is_company_managed) return "company";
  return "personal";
}

/** How many filters differ from their default (the badge of the Filters button). */
export function activeFilterCount(filters: AgentGraphFilters): number {
  return (
    (filters.kinds.length > 0 && filters.kinds.length < AGENT_KINDS.length ? 1 : 0) +
    (filters.enabled !== "all" ? 1 : 0) +
    (filters.withOpenWorkItems ? 1 : 0) +
    (filters.withRunsInFlight ? 1 : 0)
  );
}

export interface FilterContext {
  /** Agents owning at least one open work item. */
  withOpenWorkItems: ReadonlySet<string>;
  /** Agents with a run in flight. */
  withRunsInFlight: ReadonlySet<string>;
  /** Agents always kept whatever the filters (the General Assistant's stand-in, a fleet node). */
  keep?: ReadonlySet<string>;
}

/**
 * The agents of the visibility segment chosen on the toolbar, in their input
 * order; the agents of *keep* (the General Assistant's stand-in) are always
 * kept. All keeps every agent the caller can read.
 */
export function agentsInVisibility<T extends VisibilityFields & { id: string }>(
  agents: readonly T[],
  visibility: Visibility,
  userId: string | undefined,
  keep?: ReadonlySet<string>,
): T[] {
  if (visibility === "all") return [...agents];
  return agents.filter((agent) => keep?.has(agent.id) || inSegment(agent, visibility, userId));
}

/** The agents the filters keep, in their input order. */
export function filterGraphAgents<T extends FilterableAgent>(
  agents: readonly T[],
  filters: AgentGraphFilters,
  context: FilterContext,
): T[] {
  if (activeFilterCount(filters) === 0) return [...agents];
  const kinds = new Set(filters.kinds);
  return agents.filter((agent) => {
    if (context.keep?.has(agent.id)) return true;
    if (kinds.size > 0 && !kinds.has(agentKind(agent))) return false;
    if (filters.enabled === "enabled" && !agent.enabled) return false;
    if (filters.enabled === "disabled" && agent.enabled) return false;
    if (filters.withOpenWorkItems && !context.withOpenWorkItems.has(agent.id)) return false;
    if (filters.withRunsInFlight && !context.withRunsInFlight.has(agent.id)) return false;
    return true;
  });
}
