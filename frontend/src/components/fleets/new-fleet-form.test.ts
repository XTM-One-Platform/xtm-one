/**
 * The "New fleet" form - `yarn test:unit` (vitest).
 */
import { test } from "vitest";
import assert from "node:assert/strict";

import type { Agent } from "@/types";

import {
  EMPTY_FLEET_FORM,
  describeFleetRefusal,
  homeMemberCandidates,
  homeMembers,
  toNewFleet,
  validateFleetForm,
  type NewFleetForm,
} from "./new-fleet-form";

const t = (text: string) => text;

function agent(id: string, fleet: boolean = false): Agent {
  return {
    id,
    name: id.toUpperCase(),
    fleet: fleet ? { fleet_id: "f", fleet_name: "F", member_id: "m", is_orchestrator: false, is_exposed: false } : null,
  } as unknown as Agent;
}

test("home member candidates are the manageable agents with no home fleet, by name", () => {
  const agents = [agent("c"), agent("a"), agent("b", true), agent("d")];
  const ids = homeMemberCandidates(agents, (a) => a.id !== "d").map((a) => a.id);
  assert.deepEqual(ids, ["a", "c"]);
});

test("the orchestrator is a home member, first and once", () => {
  const form: NewFleetForm = { ...EMPTY_FLEET_FORM, orchestratorId: "o", memberIds: ["x", "o", "y"] };
  assert.deepEqual(homeMembers(form), ["o", "x", "y"]);
  assert.deepEqual(homeMembers({ ...form, orchestratorId: null }), ["x", "o", "y"]);
});

test("a name is required and bounded", () => {
  assert.equal(validateFleetForm({ ...EMPTY_FLEET_FORM, name: "  " }, t).name, "A fleet needs a name.");
  assert.equal(validateFleetForm({ ...EMPTY_FLEET_FORM, name: "x".repeat(201) }, t).name, "At most 200 characters.");
  assert.deepEqual(validateFleetForm({ ...EMPTY_FLEET_FORM, name: "CEO Office" }, t), {});
});

test("an agent cannot be both a home member and a connected agent", () => {
  const form: NewFleetForm = {
    ...EMPTY_FLEET_FORM,
    name: "F",
    orchestratorId: "o",
    connected: [{ agent_id: "o", collaboration_instruction: "", context_inclusion: "none" }],
  };
  assert.ok(validateFleetForm(form, t).connected);
});

test("the request trims, orders the members and exposes home members only", () => {
  const form: NewFleetForm = {
    name: " CEO Office ",
    description: " The desks ",
    workspaceId: "w",
    orchestratorId: "o",
    memberIds: ["m1"],
    connected: [{ agent_id: "c", collaboration_instruction: " ask first ", context_inclusion: "summary" }],
    exposedIds: ["m1", "c"],
  };
  assert.deepEqual(toNewFleet(form), {
    name: "CEO Office",
    description: "The desks",
    workspace_id: "w",
    orchestrator_agent_id: "o",
    member_agent_ids: ["o", "m1"],
    connected: [{ agent_id: "c", collaboration_instruction: "ask first", context_inclusion: "summary" }],
    exposed_agent_ids: ["m1"],
  });
});

const refused = (message: string, status: number) => Object.assign(new Error(message), { status });

test("a known API refusal becomes a localized message, the API's words kept as detail", () => {
  const raw = "'Scout' is already a member of the fleet 'SOC'. An agent has one home fleet: remove it there first, or add it here as a connected agent.";
  assert.deepEqual(describeFleetRefusal(refused(raw, 409), t), {
    message: "An agent you picked already has a home fleet: remove it there first, or connect it instead.",
    detail: raw,
  });
  assert.equal(describeFleetRefusal(refused("That workspace is archived.", 400), t).message, "The workspace you picked is archived: pick another one.");
  assert.equal(describeFleetRefusal(refused("'Scout' is already a member of this fleet.", 409), t).message, "An agent was picked twice.");
});

test("an unknown refusal falls back to a generic message, by status", () => {
  assert.deepEqual(describeFleetRefusal(refused("Forbidden", 403), t), {
    message: "You are not allowed to create this fleet.",
    detail: "Forbidden",
  });
  assert.deepEqual(describeFleetRefusal(refused("HTTP 500", 500), t), { message: "The fleet was not created.", detail: "HTTP 500" });
  assert.deepEqual(describeFleetRefusal("boom", t), { message: "The fleet was not created.", detail: null });
});
