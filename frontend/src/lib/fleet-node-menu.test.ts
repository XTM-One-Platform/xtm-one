import { test } from "vitest";
import assert from "node:assert/strict";

import type { FleetMember } from "@/hooks/api/useFleetApi";
import {
  fleetNodeActions,
  handoverTargetCandidates,
  homeMemberOf,
} from "./fleet-node-menu";

function member(id: string, extra: Partial<FleetMember> = {}): FleetMember {
  return {
    id,
    fleet_id: "f1",
    role: "member",
    agent_id: `agent-${id}`,
    name: id.toUpperCase(),
    target_fleet_id: null,
    parent_member_id: null,
    is_orchestrator: false,
    is_exposed: false,
    exposed_explicitly: false,
    role_label: "",
    collaboration_instruction: "",
    context_inclusion: "none",
    created_at: "2026-11-01T00:00:00Z",
    ...extra,
  };
}

const MEMBERS: FleetMember[] = [
  member("ea", { is_orchestrator: true }),
  member("dev", { parent_member_id: "ea" }),
  member("c", { role: "connected", agent_id: "agent-ghost" }),
];

const base = { canEditMembers: true, canEditHandovers: true, focused: false, handoverTargetCount: 2 };

test("a home member, for a fleet manager, offers every action in order", () => {
  assert.deepEqual(fleetNodeActions({ ...base, member: MEMBERS[1] }), [
    "details",
    "open",
    "focus",
    "add_handover",
    "change_parent",
    "remove",
  ]);
});

test("a focused node offers to clear the focus instead", () => {
  assert.deepEqual(fleetNodeActions({ ...base, member: MEMBERS[1], focused: true }).slice(0, 3), ["details", "open", "clear_focus"]);
});

test("a ghost node reads only", () => {
  assert.deepEqual(fleetNodeActions({ ...base, member: undefined }), ["details", "open", "focus"]);
});

test("a reader who does not manage the fleet, or an archived fleet, reads only", () => {
  assert.deepEqual(fleetNodeActions({ ...base, member: MEMBERS[1], canEditMembers: false }), ["details", "open", "focus"]);
});

test("adding a handover needs the right to edit the agent and a member left to target", () => {
  assert.deepEqual(fleetNodeActions({ ...base, member: MEMBERS[1], canEditHandovers: false }), [
    "details",
    "open",
    "focus",
    "change_parent",
    "remove",
  ]);
  assert.ok(!fleetNodeActions({ ...base, member: MEMBERS[1], handoverTargetCount: 0 }).includes("add_handover"));
});

test("every node, home member or ghost, reader or manager, opens the agent definition drawer first", () => {
  for (const input of [
    { ...base, member: MEMBERS[1] },
    { ...base, member: undefined },
    { ...base, member: MEMBERS[1], canEditMembers: false },
    { ...base, member: MEMBERS[1], focused: true },
  ]) {
    assert.equal(fleetNodeActions(input)[0], "details");
  }
});

test("homeMemberOf finds home members only", () => {
  assert.equal(homeMemberOf(MEMBERS, "agent-dev")?.id, "dev");
  assert.equal(homeMemberOf(MEMBERS, "agent-ghost"), undefined);
  assert.equal(homeMemberOf(MEMBERS, "nobody"), undefined);
});

test("handoverTargetCandidates leaves out the source and its current targets, by name", () => {
  const agents = [
    { id: "a", name: "Zed" },
    { id: "b", name: "Bob" },
    { id: "c", name: "Alice" },
  ];
  assert.deepEqual(
    handoverTargetCandidates("a", ["b"], agents).map((x) => x.id),
    ["c"],
  );
  assert.deepEqual(
    handoverTargetCandidates("a", null, agents).map((x) => x.id),
    ["c", "b"],
  );
});
