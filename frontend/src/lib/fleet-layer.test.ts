/**
 * Fleets layer of the agent-to-agent map - `yarn test:unit` (vitest).
 */
import { test } from "vitest";
import assert from "node:assert/strict";

import { collapseFleets, drawnIds, fleetIdOfNode, fleetNodeId, remapHandovers, type FleetLayerAgent } from "./fleet-layer";
import { ALL_HANDOVER_TYPES, shownHandoverType } from "./handover-types";

function agent(id: string, handovers: string[] = [], fleet: [string, string, boolean?] | null = null): FleetLayerAgent {
  return {
    id,
    name: id,
    icon: "Bot",
    enabled: true,
    is_company_managed: false,
    handover_agent_ids: handovers,
    handover_types: Object.fromEntries(handovers.map((h) => [h, "delegate"])),
    handover_conditions: null,
    fleet: fleet
      ? { fleet_id: fleet[0], fleet_name: fleet[1], member_id: `m-${id}`, is_orchestrator: !!fleet[2], is_exposed: !!fleet[2] }
      : null,
  };
}

// Playground: me -> ea (fleet A). Fleet A: ea -> dev, dev -> ux (fleet B), dev -> me.
// Fleet B: ux -> ea (fleet A), ux -> ux.
const AGENTS = [
  agent("me", ["ea"]),
  agent("ea", ["dev"], ["A", "CEO Office", true]),
  agent("dev", ["ux", "me"], ["A", "CEO Office"]),
  agent("ux", ["ea", "ux"], ["B", "Design"]),
  agent("solo"),
];

test("fleet node ids round trip and agent ids are not fleet nodes", () => {
  assert.equal(fleetIdOfNode(fleetNodeId("A")), "A");
  assert.equal(fleetIdOfNode("me"), null);
});

test("collapseFleets draws the playground agents and one node per fleet, members left out", () => {
  const layer = collapseFleets(AGENTS);
  assert.deepEqual(layer.agents.map((a) => a.id), ["me", "fleet:A", "fleet:B", "solo"]);
  const a = layer.agents.find((x) => x.id === "fleet:A");
  assert.equal(a?.name, "CEO Office");
  assert.equal(a?.icon, "Boxes");
  assert.deepEqual(layer.fleetNodes.get("fleet:A")?.memberIds, ["ea", "dev"]);
});

test("handovers are rewired to the drawn nodes: intra-fleet and self edges dropped, fleet edges kept once", () => {
  const byId = new Map(collapseFleets(AGENTS).agents.map((a) => [a.id, a]));
  assert.deepEqual(byId.get("me")?.handover_agent_ids, ["fleet:A"]);
  assert.deepEqual(byId.get("me")?.handover_types, { "fleet:A": "delegate" });
  // ea -> dev is inside fleet A; dev -> ux and dev -> me leave it.
  assert.deepEqual(byId.get("fleet:A")?.handover_agent_ids, ["fleet:B", "me"]);
  // ux -> ux is a self edge; ux -> ea reaches fleet A.
  assert.deepEqual(byId.get("fleet:B")?.handover_agent_ids, ["fleet:A"]);
  assert.deepEqual(byId.get("solo")?.handover_agent_ids, []);
});

test("an agent list without fleet members is drawn as it is", () => {
  const plain = [agent("x", ["y"]), agent("y")];
  const layer = collapseFleets(plain);
  assert.deepEqual(layer.agents.map((a) => [a.id, a.handover_agent_ids]), [["x", ["y"]], ["y", []]]);
  assert.equal(layer.fleetNodes.size, 0);
});

test("remapHandovers keeps the first type of each drawn target", () => {
  const nodeOf = (id: string) => (id === "a" || id === "b" ? "fleet:F" : id);
  const out = remapHandovers(["a", "b", "c"], { a: "consult", b: "delegate" }, { a: "if asked" }, nodeOf, null);
  assert.deepEqual(out, { ids: ["fleet:F", "c"], types: { "fleet:F": "consult" }, conditions: { "fleet:F": "if asked" } });
});

test("a fleet the caller cannot open keeps its members as agent nodes", () => {
  const layer = collapseFleets(AGENTS, { canOpenFleet: (id) => id === "A" });
  assert.deepEqual(layer.agents.map((a) => a.id), ["me", "fleet:A", "ux", "solo"]);
  const byId = new Map(layer.agents.map((a) => [a.id, a]));
  assert.deepEqual(byId.get("ux")?.handover_agent_ids, ["fleet:A"]);
  assert.deepEqual(byId.get("fleet:A")?.handover_agent_ids, ["ux", "me"]);
});

test("a kept agent (the General Assistant's stand-in) is drawn as itself", () => {
  const layer = collapseFleets(AGENTS, { keepIds: new Set(["ea"]) });
  assert.deepEqual(layer.agents.map((a) => a.id), ["me", "ea", "fleet:A", "fleet:B", "solo"]);
  assert.deepEqual(layer.fleetNodes.get("fleet:A")?.memberIds, ["dev"]);
  assert.equal(layer.nodeOf("ea"), "ea");
});

test("a legacy member's targets run every type on the fleet node, beside typed ones", () => {
  const legacy = { ...agent("old", ["x"], ["F", "Mixed"]), handover_types: {} };
  const typed = agent("new", ["y"], ["F", "Mixed"]);
  const node = collapseFleets([legacy, typed, agent("x"), agent("y")]).agents.find((a) => a.id === "fleet:F");
  assert.deepEqual(node?.handover_types, { x: ALL_HANDOVER_TYPES, y: "delegate" });
  assert.equal(shownHandoverType(node?.handover_types, "x", ["delegate", "consult"]), ALL_HANDOVER_TYPES);
  assert.equal(shownHandoverType(node?.handover_types, "y", ["delegate", "consult"]), "delegate");
  // Only legacy members: the plain legacy (empty) map.
  const onlyLegacy = collapseFleets([legacy, agent("x")]).agents.find((a) => a.id === "fleet:F");
  assert.deepEqual(onlyLegacy?.handover_types, {});
});

test("drawnIds maps running members onto their fleet node", () => {
  const { nodeOf } = collapseFleets(AGENTS);
  assert.deepEqual([...drawnIds(["dev", "ea", "me"], nodeOf)], ["fleet:A", "me"]);
});
