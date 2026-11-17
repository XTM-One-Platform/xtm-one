import { test } from "vitest";
import assert from "node:assert/strict";

import type { FleetMember } from "@/hooks/api/useFleetApi";
import {
  ARCHIVED_REASON,
  BUILTIN_REASON,
  builtinActivityEditable,
  READ_ONLY_REASON,
  canReparent,
  changedSettings,
  connectableAgentIds,
  connectableFleetIds,
  connectedRows,
  explicitExposure,
  exposureToSend,
  fleetEditRights,
  graphHandoversEditable,
  graphReparent,
  memberTree,
  moveTargets,
  parentCandidates,
  subtreeIds,
  toggledExposure,
} from "./fleet-edition";

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

// ea (orchestrator) -> dev -> qa ; ux root ; c connected agent ; cf connected fleet
const MEMBERS: FleetMember[] = [
  member("ux"),
  member("qa", { parent_member_id: "dev" }),
  member("dev", { parent_member_id: "ea" }),
  member("ea", { is_orchestrator: true }),
  member("c", { role: "connected", name: "Zed" }),
  member("cf", { role: "connected", agent_id: null, target_fleet_id: "f2", name: "Alpha fleet" }),
];

test("fleetEditRights: read only with the reason without manage rights, members frozen on an archived fleet", () => {
  assert.deepEqual(fleetEditRights({ can_manage: false, is_archived: false }).settings, { canEdit: false, reason: READ_ONLY_REASON });
  assert.equal(fleetEditRights(undefined).members.canEdit, false);
  const archived = fleetEditRights({ can_manage: true, is_archived: true });
  assert.equal(archived.settings.canEdit, true);
  assert.deepEqual(archived.members, { canEdit: false, reason: ARCHIVED_REASON });
  assert.deepEqual(fleetEditRights({ can_manage: true, is_archived: false }).members, { canEdit: true, reason: "" });
  const builtin = fleetEditRights({ can_manage: true, is_archived: false, is_builtin: true });
  assert.deepEqual(builtin.settings, { canEdit: false, reason: BUILTIN_REASON });
  assert.deepEqual(builtin.members, { canEdit: false, reason: BUILTIN_REASON });
});

test("builtinActivityEditable: an administrator alone, on a built-in company-managed fleet", () => {
  const builtin = { can_manage: true, is_company_managed: true, is_builtin: true };
  assert.equal(builtinActivityEditable(builtin, true), true);
  assert.equal(builtinActivityEditable(builtin, false), false);
  assert.equal(builtinActivityEditable({ ...builtin, is_builtin: false }, true), false);
  assert.equal(builtinActivityEditable({ ...builtin, is_company_managed: false }, true), false);
  assert.equal(builtinActivityEditable({ ...builtin, can_manage: false }, true), false);
  assert.equal(builtinActivityEditable(undefined, true), false);
});

test("graphHandoversEditable: the graph edits no handover on an archived or a built-in fleet", () => {
  assert.equal(graphHandoversEditable({ is_archived: false }), true);
  assert.equal(graphHandoversEditable({ is_archived: false, is_builtin: false }), true);
  assert.equal(graphHandoversEditable({ is_archived: true }), false);
  assert.equal(graphHandoversEditable({ is_archived: false, is_builtin: true }), false);
  assert.equal(graphHandoversEditable({ is_archived: true, is_builtin: true }), false);
});

test("memberTree: orchestrator first, children under their parent, connected rows left out", () => {
  assert.deepEqual(
    memberTree(MEMBERS).map((r) => [r.member.id, r.depth]),
    [["ea", 0], ["dev", 1], ["qa", 2], ["ux", 0]],
  );
});

test("memberTree: a missing parent or a stored cycle never hides a member", () => {
  const rows = memberTree([
    member("a", { parent_member_id: "gone" }),
    member("b", { parent_member_id: "c" }),
    member("c", { parent_member_id: "b" }),
  ]);
  assert.deepEqual(rows.map((r) => r.member.id).sort(), ["a", "b", "c"]);
});

test("subtreeIds and parentCandidates never offer the member itself or a descendant", () => {
  assert.deepEqual([...subtreeIds(MEMBERS, "dev")].sort(), ["dev", "qa"]);
  assert.deepEqual(parentCandidates(MEMBERS, "dev").map((m) => m.id), ["ea", "ux"]);
  assert.deepEqual(parentCandidates(MEMBERS, "ux").map((m) => m.id), ["dev", "ea", "qa"]);
});

test("canReparent refuses itself, a descendant, the current parent and connected rows", () => {
  assert.equal(canReparent(MEMBERS, "dev", "ux"), true);
  assert.equal(canReparent(MEMBERS, "dev", "qa"), false);
  assert.equal(canReparent(MEMBERS, "dev", "dev"), false);
  assert.equal(canReparent(MEMBERS, "dev", "ea"), false);
  assert.equal(canReparent(MEMBERS, "dev", "c"), false);
  assert.equal(canReparent(MEMBERS, "c", "ea"), false);
  // To the root: only when it has a parent.
  assert.equal(canReparent(MEMBERS, "dev", null), true);
  assert.equal(canReparent(MEMBERS, "ux", null), false);
});

test("graphReparent maps a drop between agent nodes to a member and its new parent, or nothing", () => {
  // dev dropped on ux: dev now reports to ux.
  const drop = graphReparent(MEMBERS, "agent-dev", "agent-ux");
  assert.equal(drop?.member.id, "dev");
  assert.equal(drop?.parentId, "ux");
  // Its descendant, itself, its current parent: nothing.
  assert.equal(graphReparent(MEMBERS, "agent-dev", "agent-qa"), null);
  assert.equal(graphReparent(MEMBERS, "agent-dev", "agent-dev"), null);
  assert.equal(graphReparent(MEMBERS, "agent-dev", "agent-ea"), null);
  // A connected agent (a ghost on the graph) is neither dragged nor a parent.
  assert.equal(graphReparent(MEMBERS, "agent-c", "agent-ea"), null);
  assert.equal(graphReparent(MEMBERS, "agent-ux", "agent-c"), null);
  // An agent outside the fleet: nothing.
  assert.equal(graphReparent(MEMBERS, "agent-dev", "agent-elsewhere"), null);
});

test("connectedRows lists agents before fleets", () => {
  assert.deepEqual(connectedRows(MEMBERS).map((m) => m.id), ["c", "cf"]);
});

test("connectable pickers leave out home members, connected rows and the fleet itself", () => {
  assert.deepEqual(connectableAgentIds(["agent-ea", "agent-c", "agent-new"], MEMBERS), ["agent-new"]);
  assert.deepEqual(connectableFleetIds(["f1", "f2", "f3"], "f1", MEMBERS), ["f3"]);
});

test("moveTargets keeps fleets the caller manages, not archived, not this one, by name", () => {
  const fleets = [
    { id: "f1", name: "Self", can_manage: true, is_archived: false },
    { id: "f3", name: "Zeta", can_manage: true, is_archived: false },
    { id: "f2", name: "Beta", can_manage: true, is_archived: false },
    { id: "f4", name: "Ro", can_manage: false, is_archived: false },
    { id: "f5", name: "Old", can_manage: true, is_archived: true },
  ];
  assert.deepEqual(moveTargets(fleets, "f1").map((f) => f.id), ["f2", "f3"]);
});

test("toggledExposure never sends the orchestrator", () => {
  assert.deepEqual(toggledExposure(["ea", "dev"], "ux", "ea"), ["dev", "ux"]);
  assert.deepEqual(toggledExposure(["dev", "ux"], "dev", "ea"), ["ux"]);
  assert.deepEqual(toggledExposure(["dev"], "ea", "ea"), ["dev"]);
  assert.deepEqual(toggledExposure([], "dev", null), ["dev"]);
});

test("explicitExposure reads the stored flags, never the implicit orchestrator", () => {
  // As GET /fleets/{id}/members answers: the orchestrator reads is_exposed true
  // with its stored flag false.
  const rows = [
    member("ea", { is_orchestrator: true, is_exposed: true }),
    member("dev", { is_exposed: true, exposed_explicitly: true }),
    member("ux"),
    member("c", { role: "connected", exposed_explicitly: true }),
  ];
  assert.deepEqual(explicitExposure(rows), ["agent-dev"]);
});

test("switching the orchestrator from the members read never sends the previous one", () => {
  const rows = [
    member("ea", { is_orchestrator: true, is_exposed: true }),
    member("dev", { is_exposed: true, exposed_explicitly: true }),
    member("ux"),
  ];
  const stored = explicitExposure(rows);
  // The panel's draft starts from the stored list; the orchestrator moves to ux.
  assert.deepEqual(exposureToSend(stored, stored, "agent-ux", true), ["agent-dev"]);
  // An orchestrator that was also flagged on purpose keeps its flag.
  const flagged = explicitExposure([member("ea", { is_orchestrator: true, is_exposed: true, exposed_explicitly: true }), ...rows.slice(1)]);
  assert.deepEqual(exposureToSend(flagged, flagged, "agent-ux", true), ["agent-ea", "agent-dev"]);
});

test("exposureToSend: a new orchestrator drops the previous one unless it was exposed on purpose", () => {
  // Orchestrator A -> B, A only implicitly exposed: A is not sent, the explicit list is resent.
  assert.deepEqual(exposureToSend(["dev"], ["dev"], "B", true), ["dev"]);
  // A explicitly flagged before the change keeps its flag; B is never sent.
  assert.deepEqual(exposureToSend(["A", "B", "dev"], ["A", "B", "dev"], "B", true), ["A", "dev"]);
  // Nothing changed: no exposure write.
  assert.equal(exposureToSend(["dev"], ["dev"], "A", false), null);
  assert.equal(exposureToSend(["dev", "A"], ["dev"], "A", false), null);
  // A toggle alone is sent.
  assert.deepEqual(exposureToSend(["dev", "ux"], ["dev"], "A", false), ["dev", "ux"]);
  assert.deepEqual(exposureToSend([], ["dev"], null, false), []);
});

test("changedSettings sends only what changed", () => {
  assert.deepEqual(
    changedSettings({ name: "A", color: "", tags: ["x"] }, { name: "B", color: "", tags: ["x"] }),
    { name: "B" },
  );
  assert.deepEqual(changedSettings({ name: "A" }, { name: "A" }), {});
});
