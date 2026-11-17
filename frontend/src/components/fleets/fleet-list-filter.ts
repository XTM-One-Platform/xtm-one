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
 * The pure half of the Fleets list toolbar, tested by
 * `fleet-list-filter.test.ts`.
 *
 * The search, the ownership filter and the `has:` properties are the shared
 * `useEntityFilter` ones (Agents, Integrations, Presentations...); what is
 * proper to a fleet lives here: the workspace and orchestrator filters, the
 * archived switch, and the `has:` properties a fleet offers. Each one reads
 * and writes its own URL query parameter, so a link keeps the filters.
 */
import type { EntityProperty } from "@/hooks/useEntityFilter";
import type { Fleet } from "@/hooks/api/useFleetApi";

/** URL query parameters of the fleet-specific filters. */
const FLEET_FILTER_PARAMS = {
  workspace: "ws",
  orchestrator: "orch",
  archived: "archived",
} as const;

/** The `has:` properties a fleet offers, at module scope for a stable identity. */
export const FLEET_PROPERTIES: (EntityProperty<Fleet> & { label: string; description: string })[] = [
  {
    key: "members",
    label: "Has members",
    description: "Fleets with at least one home member besides the orchestrator.",
    // member_count counts the orchestrator, which is always a home member.
    matches: (fleet) => fleet.member_count - (fleet.orchestrator_agent_id ? 1 : 0) > 0,
  },
  {
    key: "connected",
    // connected_count counts connected agents and connected fleets alike.
    label: "Has connections",
    description: "Fleets connected to other agents or fleets.",
    matches: (fleet) => fleet.connected_count > 0,
  },
  {
    key: "orchestrator",
    label: "Has an orchestrator",
    description: "Fleets whose orchestrator is set.",
    matches: (fleet) => !!fleet.orchestrator_agent_id,
  },
];

/** The fleet-specific filters, as read from the URL. */
export interface FleetFilters {
  /** A workspace id, or null for every workspace. */
  workspaceId: string | null;
  /** An orchestrator agent id, or null for every orchestrator. */
  orchestratorId: string | null;
  /** Archived fleets are listed too. */
  includeArchived: boolean;
}

export const NO_FLEET_FILTERS: FleetFilters = { workspaceId: null, orchestratorId: null, includeArchived: false };

export function readFleetFilters(params: URLSearchParams): FleetFilters {
  return {
    workspaceId: params.get(FLEET_FILTER_PARAMS.workspace) || null,
    orchestratorId: params.get(FLEET_FILTER_PARAMS.orchestrator) || null,
    includeArchived: params.get(FLEET_FILTER_PARAMS.archived) === "1",
  };
}

/** Writes the filters into a copy of the query, dropping the defaults. */
export function writeFleetFilters(params: URLSearchParams, filters: FleetFilters): URLSearchParams {
  const next = new URLSearchParams(params);
  const set = (key: string, value: string | null) => (value ? next.set(key, value) : next.delete(key));
  set(FLEET_FILTER_PARAMS.workspace, filters.workspaceId);
  set(FLEET_FILTER_PARAMS.orchestrator, filters.orchestratorId);
  set(FLEET_FILTER_PARAMS.archived, filters.includeArchived ? "1" : null);
  return next;
}

/** True when a fleet-specific filter narrows the list. */
export function hasFleetFilters(filters: FleetFilters): boolean {
  return !!filters.workspaceId || !!filters.orchestratorId || filters.includeArchived;
}

/** The fleets the fleet-specific filters keep, in the order given. */
export function applyFleetFilters(fleets: readonly Fleet[], filters: FleetFilters): Fleet[] {
  return fleets.filter(
    (fleet) =>
      (filters.includeArchived || !fleet.is_archived) &&
      (!filters.workspaceId || fleet.workspace_id === filters.workspaceId) &&
      (!filters.orchestratorId || fleet.orchestrator_agent_id === filters.orchestratorId),
  );
}

/**
 * The fleets the list shows, or `undefined` while the query loads: the
 * visibility landing (`useEntityFilter`) waits for a loaded list, and an empty
 * array in its place would land once on All.
 */
export function narrowFleets(fleets: readonly Fleet[] | undefined, filters: FleetFilters): Fleet[] | undefined {
  return fleets === undefined ? undefined : applyFleetFilters(fleets, filters);
}

/** The options of a picker: each distinct value once, by label. */
export function distinctOptions(
  fleets: readonly Fleet[],
  pick: (fleet: Fleet) => { value: string | null; label: string | null },
): { value: string; label: string }[] {
  const seen = new Map<string, string>();
  for (const fleet of fleets) {
    const { value, label } = pick(fleet);
    if (value && !seen.has(value)) seen.set(value, label || value);
  }
  return [...seen].map(([value, label]) => ({ value, label })).sort((a, b) => a.label.localeCompare(b.label));
}
