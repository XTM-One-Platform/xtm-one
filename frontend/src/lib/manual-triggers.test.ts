/**
 * Unit tests for the Manual Triggers panel's selection - `yarn test:unit`.
 *
 * The panel offers what the trigger endpoint would run for this user, and only
 * against an agent it can name: an assignment whose agent is not in the user's
 * agent list used to be rendered as "Unknown agent".
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import type { Agent, Assignment } from "@/types";
import { triggerableManualAssignments } from "./manual-triggers.ts";

type AssignmentFixture = Pick<
  Assignment,
  "agent_id" | "name" | "trigger_type" | "enabled" | "is_company_managed" | "run_as_all_users" | "hide_runs"
> &
  Partial<Pick<Assignment, "paused_by_owner_deactivation">>;
type AgentFixture = Pick<Agent, "id" | "name" | "enabled" | "hide_runs">;

function agent(id: string, overrides: Partial<AgentFixture> = {}): AgentFixture {
  return { id, name: id, enabled: true, hide_runs: false, ...overrides };
}

function manual(name: string, agentId: string, overrides: Partial<AssignmentFixture> = {}): AssignmentFixture {
  return {
    agent_id: agentId,
    name,
    trigger_type: "manual",
    enabled: true,
    is_company_managed: false,
    run_as_all_users: false,
    hide_runs: null,
    ...overrides,
  };
}

function offered(assignments: AssignmentFixture[], agents: AgentFixture[], isAdmin: boolean): string[] {
  return triggerableManualAssignments(assignments, agents, isAdmin).map(({ assignment }) => assignment.name);
}

test("an assignment whose agent the user cannot see is not offered", () => {
  const assignments = [manual("mine", "a"), manual("a colleague's", "private")];

  assert.deepEqual(offered(assignments, [agent("a")], true), ["mine"]);
  assert.deepEqual(offered(assignments, [agent("a")], false), ["mine"]);
});

test("nothing is offered before the agents are known", () => {
  assert.deepEqual(offered([manual("mine", "a")], [], false), []);
});

test("an assignment paused by its owner's deactivation is offered to nobody", () => {
  const assignments = [
    manual("shared by a departed colleague", "a", { paused_by_owner_deactivation: true }),
    manual("mine", "a", { paused_by_owner_deactivation: false }),
  ];

  assert.deepEqual(offered(assignments, [agent("a")], false), ["mine"]);
  assert.deepEqual(offered(assignments, [agent("a")], true), ["mine"]);
});

test("only enabled manual assignments of an enabled agent", () => {
  const agents = [agent("on"), agent("off", { enabled: false })];
  const assignments = [
    manual("runs", "on"),
    manual("scheduled", "on", { trigger_type: "schedule" }),
    manual("paused", "on", { enabled: false }),
    manual("agent off", "off"),
  ];

  assert.deepEqual(offered(assignments, agents, true), ["runs"]);
});

test("a company-managed assignment needs an admin unless it runs per user", () => {
  const agents = [agent("fleet"), agent("private-runs", { hide_runs: true })];
  const assignments = [
    manual("shared runs", "fleet", { is_company_managed: true }),
    manual("fan-out", "fleet", { is_company_managed: true, run_as_all_users: true }),
    manual("hidden runs", "fleet", { is_company_managed: true, hide_runs: true }),
    manual("inherits hidden runs", "private-runs", { is_company_managed: true }),
    manual("overrides to shared runs", "private-runs", { is_company_managed: true, hide_runs: false }),
  ];

  assert.deepEqual(offered(assignments, agents, true), [
    "fan-out",
    "hidden runs",
    "shared runs",
    "inherits hidden runs",
    "overrides to shared runs",
  ]);
  assert.deepEqual(offered(assignments, agents, false), ["fan-out", "hidden runs", "inherits hidden runs"]);
});

test("sorted by agent name, then assignment name", () => {
  const agents = [agent("b", { name: "Beta" }), agent("a", { name: "Alpha" })];
  const assignments = [manual("zeta", "a"), manual("delta", "b"), manual("alpha", "a")];

  const triggers = triggerableManualAssignments(assignments, agents, false);

  assert.deepEqual(
    triggers.map(({ agent: a, assignment }) => `${a.name}/${assignment.name}`),
    ["Alpha/alpha", "Alpha/zeta", "Beta/delta"],
  );
});
