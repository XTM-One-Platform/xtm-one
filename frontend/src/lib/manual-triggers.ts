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

import type { Agent, Assignment } from "@/types";

type TriggerAssignment = Pick<
  Assignment,
  "agent_id" | "name" | "trigger_type" | "enabled" | "is_company_managed" | "run_as_all_users" | "hide_runs"
> &
  Partial<Pick<Assignment, "paused_by_owner_deactivation">>;
type TriggerAgent = Pick<Agent, "id" | "name" | "enabled" | "hide_runs">;

interface ManualTrigger<W extends TriggerAssignment, A extends TriggerAgent> {
  assignment: W;
  agent: A;
}

/**
 * The assignments the Manual Triggers panel offers: those the trigger endpoint
 * (`POST /assignments/{id}/trigger`) would run for this user, sorted by agent
 * name then assignment name.
 *
 * - A manual, enabled assignment of an agent in `agents` (the ones this user
 *   can see) that is enabled. An assignment whose agent is not in that list is
 *   not this user's to run, and it has no agent to show.
 * - A company-managed assignment needs an admin unless it runs per user
 *   (`run_as_all_users`, else the assignment's `hide_runs`, else the agent's):
 *   it then runs AS the user and produces their own private run.
 * - Not one paused because its owner's account is deactivated: the
 *   endpoint refuses it for everyone, admins included.
 */
export function triggerableManualAssignments<W extends TriggerAssignment, A extends TriggerAgent>(
  assignments: readonly W[],
  agents: readonly A[],
  isAdmin: boolean,
): ManualTrigger<W, A>[] {
  const agentById = new Map(agents.map((agent) => [agent.id, agent]));
  const triggers: ManualTrigger<W, A>[] = [];
  for (const assignment of assignments) {
    if (assignment.trigger_type !== "manual" || !assignment.enabled) continue;
    if (assignment.paused_by_owner_deactivation) continue;
    const agent = agentById.get(assignment.agent_id);
    if (!agent?.enabled) continue;
    const perUser = assignment.run_as_all_users || (assignment.hide_runs ?? agent.hide_runs);
    if (assignment.is_company_managed && !isAdmin && !perUser) continue;
    triggers.push({ assignment, agent });
  }
  return triggers.sort(
    (a, b) =>
      a.agent.name.localeCompare(b.agent.name) || a.assignment.name.localeCompare(b.assignment.name),
  );
}
