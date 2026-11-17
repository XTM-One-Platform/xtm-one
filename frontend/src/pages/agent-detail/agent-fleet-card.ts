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
 * Pure helpers of the agent page's Fleet card:
 * what the card says about the agent's home fleet and the fleets that connect
 * it. Kept apart from the component so the wording rules are unit-tested.
 */
import { CONTEXT_INCLUSION_LABELS } from "@/components/fleets/context-inclusion";
import { isFleetsUnavailable, type AgentConnectedFleet, type AgentFleets, type AgentHomeFleet } from "@/hooks/api/useFleetApi";
import { assignmentDisplayStatus, type StatusAssignment } from "../assignment-detail/assignment-status";

/** The fleet page of a fleet (edits live there, never on the agent). */
export function fleetPath(fleetId: string): string {
  return `/fleets/${fleetId}`;
}

/** The fleet's About page (#9539 S5): the page with no tab (#10203), so the fleet path itself. */
export function fleetAboutPath(fleetId: string): string {
  return fleetPath(fleetId);
}

/**
 * The agent's own automations as the About card counts them (#9539 S5): an
 * automation is an assignment, and only one that still starts runs is "on"
 * (the shared `assignmentDisplayStatus`: a paused or ended one is not).
 * `null` while the assignments are not read.
 */
export function agentAutomations(
  assignments: readonly StatusAssignment[] | null | undefined,
  now: Date = new Date(),
): { on: number; total: number } | null {
  if (!assignments) return null;
  return { on: assignments.filter((a) => assignmentDisplayStatus(a, now) === "active").length, total: assignments.length };
}

/**
 * Whether the card offers "Add Assignment" next to "No automation yet"
 * (#9539 S5, UX round 1 B1): only when the assignments are read, the agent
 * has none, and the reader can add one on this page.
 */
export function offersAddAutomation(
  automations: { on: number; total: number } | null,
  canAdd: boolean,
): boolean {
  return canAdd && automations !== null && automations.total === 0;
}

/** Who the agent reports to, as an i18n key and its values. */
export type ReportsTo =
  | { key: "Leads the fleet" }
  | { key: "Reports to the orchestrator" }
  | { key: "At the top level: the fleet has no orchestrator" }
  | { key: "Reports to {name}"; name: string }
  | { key: "Reports to a member you cannot see" };

export function reportsTo(home: AgentHomeFleet): ReportsTo {
  if (home.is_orchestrator) return { key: "Leads the fleet" };
  if (!home.parent_member_id) {
    return home.has_orchestrator
      ? { key: "Reports to the orchestrator" }
      : { key: "At the top level: the fleet has no orchestrator" };
  }
  if (home.parent_name) return { key: "Reports to {name}", name: home.parent_name };
  return { key: "Reports to a member you cannot see" };
}

/** The chips of the home fleet row, in display order (i18n keys). */
export function homeChips(home: AgentHomeFleet): string[] {
  const chips: string[] = [];
  if (home.is_orchestrator) chips.push("Orchestrator");
  if (home.is_exposed) chips.push("Reachable from other fleets");
  if (home.is_archived) chips.push("Archived");
  return chips;
}

/** The context a connecting fleet receives, as an i18n key: the words of the members panel. */
export function contextLabel(link: AgentConnectedFleet): string {
  return CONTEXT_INCLUSION_LABELS[link.context_inclusion] ?? CONTEXT_INCLUSION_LABELS.none;
}

/** In the playground: no home fleet at all (not even a hidden one) and no connecting fleet the caller can see. */
export function isInPlayground(fleets: AgentFleets | undefined): boolean {
  return !fleets || (!fleets.home && !fleets.home_hidden && fleets.connected_to.length === 0);
}

/** The card is hidden only on the licence refusal (403); any other failed read is said on the card. */
export function hidesFleetCard(error: unknown): boolean {
  return isFleetsUnavailable(error);
}

/** The agent has a home fleet the caller cannot see: said, never named. */
export function isHomeHidden(fleets: AgentFleets | undefined): boolean {
  return !!fleets && !fleets.home && !!fleets.home_hidden;
}
