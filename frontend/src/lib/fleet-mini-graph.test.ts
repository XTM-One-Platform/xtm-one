import { describe, expect, it } from "vitest";

import type { Agent } from "@/types";
import { edgelessIds, fleetMiniGraph, MINI_GRAPH_MAX_NODES } from "./fleet-mini-graph";

const LABELS = {
  fromFleet: (n: number, f: string) => `${n} agents from ${f}`,
  fromOtherFleets: (n: number) => `${n} agents from other fleets`,
  more: (n: number) => `${n} more agents`,
};

function agent(id: string, fleet: { fleet_id: string; fleet_name: string } | null, handovers: string[] = []): Agent {
  return {
    id,
    name: id.toUpperCase(),
    enabled: true,
    is_company_managed: false,
    icon: "bot",
    handover_agent_ids: handovers,
    handover_types: {},
    handover_conditions: {},
    fleet,
  } as unknown as Agent;
}

const OWN = { fleet_id: "own", fleet_name: "Own" };
const AIS = { fleet_id: "ais", fleet_name: "AI security" };
const VUL = { fleet_id: "vul", fleet_name: "Vulnerability" };

describe("edgelessIds (#10147 UX r3 B2)", () => {
  it("names the drawn nodes no handover reaches or leaves, in drawn order", () => {
    const agents = [agent("hub", OWN), agent("a", OWN), agent("sales", OWN), agent("watch", OWN), agent("self", OWN)];
    const edges = new Set(["hub->a", "self->self"]);
    expect(edgelessIds(agents, edges)).toEqual(["sales", "watch", "self"]);
  });

  it("is empty when every node has a handover", () => {
    expect(edgelessIds([agent("hub", OWN), agent("a", OWN)], new Set(["a->hub"]))).toEqual([]);
  });
});

describe("fleetMiniGraph (#9760 UX round 3, B1)", () => {
  const agents = [
    agent("hub", OWN, ["m", "a1", "a2", "a3", "v1"]),
    agent("m", OWN, ["a1"]),
    agent("a1", AIS),
    agent("a2", AIS),
    agent("a3", AIS),
    agent("v1", VUL),
  ];
  const ghosts = new Map([["a1", "AI security"], ["a2", "AI security"], ["a3", "AI security"], ["v1", "Vulnerability"]]);
  const edges = new Set(["hub->m", "hub->a1", "hub->a2", "hub->a3", "hub->v1", "m->a1"]);
  const mini = fleetMiniGraph(agents, ghosts, edges, new Set(["m->a1"]), LABELS);

  it("folds the agents one other fleet lends into one named group node", () => {
    expect(mini.agents.map((a) => a.id)).toEqual(["hub", "m", "fleet:ais", "v1"]);
    expect(mini.agents.find((a) => a.id === "fleet:ais")?.name).toBe("3 agents from AI security");
    expect([...mini.groupIds]).toEqual(["fleet:ais"]);
  });

  it("keeps the fleet's members and a lone ghost as themselves", () => {
    expect(mini.ghostNodes.get("v1")).toBe("Vulnerability");
    expect(mini.ghostNodes.get("fleet:ais")).toBe("");
    expect(mini.ghostNodes.has("hub")).toBe(false);
  });

  it("rewires every edge to the drawn ids, once", () => {
    expect([...mini.edgeIds].sort()).toEqual(["hub->fleet:ais", "hub->m", "hub->v1", "m->fleet:ais"]);
    expect([...mini.dashedEdgeIds]).toEqual(["m->fleet:ais"]);
    expect(mini.agents.find((a) => a.id === "hub")?.handover_agent_ids).toEqual(["m", "fleet:ais", "v1"]);
  });
});

// The real shape of the densest built-in fleet (UX round 4, B1): 14 members of
// its own fleet and 5 lone agents, each from a different fleet, so the
// per-fleet fold of round 3 folds nothing.
describe("fleetMiniGraph of 14 members and 5 lone ghosts (#9760 UX round 4, B1)", () => {
  const FLEETS = ["ais", "vul", "tpr", "fork1", "fork2"].map((id) => ({ fleet_id: id, fleet_name: id.toUpperCase() }));
  const ghosts = FLEETS.map((f, i) => agent(`g${i}`, f));
  // hub -> 4 stage leads; lead 0 -> 4 leaves, lead 1 -> 3 leaves, lead 2 -> 2 leaves; lead 3 alone.
  const leaves = [4, 3, 2].flatMap((n, lead) => Array.from({ length: n }, (_, j) => agent(`l${lead}${j}`, OWN)));
  const leads = [0, 1, 2, 3].map((lead) =>
    agent(`s${lead}`, OWN, [...leaves.filter((l) => l.id.startsWith(`l${lead}`)).map((l) => l.id), ...(lead < 2 ? [`g${lead}`] : [])]),
  );
  const hub = { ...agent("hub", OWN, [...leads.map((l) => l.id), "g2", "g3", "g4"]), fleet: { ...OWN, is_orchestrator: true } } as unknown as Agent;
  const all = [hub, ...leads, ...leaves, ...ghosts];
  const ghostMap = new Map(ghosts.map((g, i) => [g.id, FLEETS[i].fleet_name]));
  const edges = new Set(all.flatMap((a) => (a.handover_agent_ids ?? []).map((t) => `${a.id}->${t}`)));
  const mini = fleetMiniGraph(all, ghostMap, edges, new Set(), LABELS);
  const byId = new Map(mini.agents.map((a) => [a.id, a]));

  it("has the real shape: 14 members and 5 ghosts from 5 fleets", () => {
    expect(all).toHaveLength(19);
    expect(all.filter((a) => !ghostMap.has(a.id))).toHaveLength(14);
    expect(new Set(ghosts.map((g) => g.fleet?.fleet_id)).size).toBe(5);
  });

  it("draws at most the node budget", () => {
    expect(mini.agents.length).toBeLessThanOrEqual(MINI_GRAPH_MAX_NODES);
  });

  it("folds every outside agent into one group of other fleets", () => {
    expect(byId.get("group:other-fleets")?.name).toBe("5 agents from other fleets");
    expect(mini.ghostNodes.get("group:other-fleets")).toBe("");
    for (const g of ghosts) expect(byId.has(g.id)).toBe(false);
  });

  it("folds a parent's leaves under it with a count, keeps a lone leaf and the hub", () => {
    expect(byId.get("group:more:s0")?.name).toBe("4 more agents");
    expect(byId.get("group:more:s1")?.name).toBe("3 more agents");
    expect(byId.get("group:more:s2")?.name).toBe("2 more agents");
    expect(byId.has("hub")).toBe(true);
    expect(byId.has("s3")).toBe(true);
    // A group of the fleet's own members is drawn as a member, not a ghost.
    expect(mini.ghostNodes.has("group:more:s0")).toBe(false);
    expect([...mini.groupIds].sort()).toEqual(["group:more:s0", "group:more:s1", "group:more:s2", "group:other-fleets"]);
  });

  it("rewires every edge to the drawn ids", () => {
    for (const id of mini.edgeIds) {
      const [from, to] = id.split("->");
      expect(byId.has(from), id).toBe(true);
      expect(byId.has(to), id).toBe(true);
    }
    expect(mini.edgeIds).toContain("hub->group:other-fleets");
    expect(mini.edgeIds).toContain("s0->group:more:s0");
    // The group of other fleets is wired to the orchestrator only (UX round 5, B2).
    expect([...mini.edgeIds].filter((id) => id.includes("group:other-fleets"))).toEqual(["hub->group:other-fleets"]);
    expect(byId.get("s0")?.handover_agent_ids).not.toContain("group:other-fleets");
  });

  it("draws one edge back from the group to the orchestrator when an outside agent hands back", () => {
    const back = all.map((a) => (a.id === "g3" ? { ...a, handover_agent_ids: ["l00"] } : a));
    const backEdges = new Set([...edges, "g3->l00"]);
    const folded = fleetMiniGraph(back, ghostMap, backEdges, new Set(), LABELS);
    const group = [...folded.edgeIds].filter((id) => id.includes("group:other-fleets")).sort();
    expect(group).toEqual(["group:other-fleets->hub", "hub->group:other-fleets"]);
    expect(folded.agents.find((a) => a.id === "group:other-fleets")?.handover_agent_ids).toEqual(["hub"]);
  });

  it("leaves a small fleet whole", () => {
    const small = fleetMiniGraph(all.slice(0, 5), new Map(), new Set(), new Set(), LABELS);
    expect(small.agents.map((a) => a.id)).toEqual(["hub", "s0", "s1", "s2", "s3"]);
    expect(small.groupIds.size).toBe(0);
  });
});
