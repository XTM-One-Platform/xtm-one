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
 * The pure half of the "New fleet" dialog, tested by
 * `new-fleet-form.test.ts`: who may be a home member, what the form refuses
 * before anything is sent, and the request it becomes.
 */
import type { FleetContextInclusion, NewFleet } from "@/hooks/api/useFleetApi";
import type { Agent } from "@/types";

import { type FleetRefusal, MEMBER_REFUSAL_SENTENCES, matchFleetRefusal } from "./fleet-refusals";

interface ConnectedRow {
  agent_id: string;
  collaboration_instruction: string;
  context_inclusion: FleetContextInclusion;
}

export interface NewFleetForm {
  name: string;
  description: string;
  /** `null` creates a workspace named after the fleet. */
  workspaceId: string | null;
  orchestratorId: string | null;
  memberIds: string[];
  connected: ConnectedRow[];
  exposedIds: string[];
}

export const EMPTY_FLEET_FORM: NewFleetForm = {
  name: "",
  description: "",
  workspaceId: null,
  orchestratorId: null,
  memberIds: [],
  connected: [],
  exposedIds: [],
};

/** An agent as the agents API serves it: `fleet` is its home fleet, null in
 *  the playground (`AgentResponse.fleet`). */
type AgentWithHome = Agent & { fleet?: { fleet_id: string } | null };

/** The agents of a fleet member list: enabled or not, the caller may manage
 *  them, and none has a home fleet yet (an agent has one home fleet). */
export function homeMemberCandidates(
  agents: readonly Agent[],
  canManage: (agent: Agent) => boolean,
): Agent[] {
  return agents
    .filter((a) => !(a as AgentWithHome).fleet && canManage(a))
    .sort((a, b) => a.name.localeCompare(b.name));
}

/** The home members, orchestrator first and counted once. */
export function homeMembers(form: NewFleetForm): string[] {
  const rest = form.memberIds.filter((id) => id !== form.orchestratorId);
  return form.orchestratorId ? [form.orchestratorId, ...rest] : rest;
}

export type FleetFormErrors = Partial<Record<"name" | "connected", string>>;

/** What the dialog refuses inline, before the API is asked. */
export function validateFleetForm(form: NewFleetForm, t: (text: string) => string): FleetFormErrors {
  const errors: FleetFormErrors = {};
  const name = form.name.trim();
  if (!name) errors.name = t("A fleet needs a name.");
  else if (name.length > 200) errors.name = t("At most 200 characters.");
  const home = new Set(homeMembers(form));
  if (form.connected.some((row) => home.has(row.agent_id))) {
    errors.connected = t("An agent is either a home member or a connected agent, not both.");
  }
  return errors;
}

/**
 * The sentence the New fleet dialog shows for a known refusal, where the
 * creation context words it differently from an existing fleet; the wordings
 * are recognized once, by `matchFleetRefusal` (`fleet-refusals.ts`). The API's
 * own message stays available as a detail: it names the agent or the fleet.
 * @i18n-keys
 */
const CREATE_REFUSAL_SENTENCES = {
  already_member: "An agent was picked twice.",
  not_exposed: "A connected agent belongs to another fleet that does not expose it.",
} as const satisfies Partial<Record<FleetRefusal, string>>;

/** What the dialog shows for a refused creation: a localized message, and the API's words as a detail. */
export function describeFleetRefusal(
  error: unknown,
  t: (text: string) => string,
): { message: string; detail: string | null } {
  const raw = error instanceof Error ? error.message : "";
  const status = (error as { status?: number } | null)?.status;
  const known = matchFleetRefusal(error);
  if (known) {
    const sentences: Partial<Record<FleetRefusal, string>> = CREATE_REFUSAL_SENTENCES;
    return { message: t(sentences[known] ?? MEMBER_REFUSAL_SENTENCES[known]), detail: raw };
  }
  if (status === 403) return { message: t("You are not allowed to create this fleet."), detail: raw || null };
  return { message: t("The fleet was not created."), detail: raw || null };
}

/** The request the form sends (`useCreateFleet`). */
export function toNewFleet(form: NewFleetForm): NewFleet {
  const home = homeMembers(form);
  return {
    name: form.name.trim(),
    description: form.description.trim(),
    workspace_id: form.workspaceId,
    orchestrator_agent_id: form.orchestratorId,
    member_agent_ids: home,
    connected: form.connected.map((row) => ({
      ...row,
      collaboration_instruction: row.collaboration_instruction.trim(),
    })),
    // Only home members can be exposed; the orchestrator always is.
    exposed_agent_ids: form.exposedIds.filter((id) => home.includes(id)),
  };
}
