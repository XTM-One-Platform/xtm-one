import assert from "node:assert/strict";
import { test } from "node:test";

import {
  WORK_ITEM_GRAPH_LAYOUTS,
  graphGroups,
  groupNodeId,
  layoutWorkItemGraph,
  progressionEdge,
  type WorkItemGraphLayout,
} from "./work-item-graph-layout.ts";
import { focusChains } from "./work-item-graph.ts";
import type { WorkItemGraphEdge, WorkItemGraphItemNode, WorkItemGraphNode } from "../types/index.ts";

function item(id: string, extra: Partial<WorkItemGraphItemNode> = {}): WorkItemGraphItemNode {
  return {
    id,
    kind: "item",
    title: id,
    status: "blocked",
    priority: "medium",
    correlation_key: null,
    owner_agent: null,
    needs_human: false,
    ask: null,
    is_program_root: false,
    outside_filter: false,
    user_id: null,
    waits_for_user_id: null,
    owner_agent_id: null,
    due_at: null,
    scheduled: false,
    ...extra,
  };
}

const dev = { id: "agent-dev", name: "Developer", icon: null };
const ux = { id: "agent-ux", name: "UX Director", icon: null };

// The acceptance's seeded set (#1062): chain A waits B waits C, a fork (D waits
// B), a pull request and a deployment reference on B, a person card on C, a
// program R with children K1 and K2, a cancelled blocker X.
const nodes: WorkItemGraphNode[] = [
  item("A", { owner_agent: dev, correlation_key: "dev:a" }),
  item("B", { owner_agent: dev, correlation_key: "dev:b" }),
  item("C", { owner_agent: ux, correlation_key: "ux:c", needs_human: true }),
  item("D", { owner_agent: ux, correlation_key: "ux:d", priority: "high" }),
  item("R", { status: "open", is_program_root: true, correlation_key: "program:p", owner_agent: dev }),
  item("K1", { status: "open", owner_agent: dev }),
  item("K2", { status: "in_progress", owner_agent: dev }),
  item("X", { status: "cancelled", outside_filter: true }),
  { id: "ref:pr", kind: "ref", ref_kind: "pull_request", label: "o/r#1", repo: "o/r", number: 1, state: null, satisfied: false, url: null },
  { id: "ref:dep", kind: "ref", ref_kind: "deployment", label: "o/r#1", repo: "o/r", number: 1, state: "deployed", satisfied: true, url: null },
  { id: "person:u", kind: "person", name: "Sam", is_you: true },
];
const edge = (source: string, target: string, kind: WorkItemGraphEdge["kind"] = "blocked_on"): WorkItemGraphEdge => ({
  id: `${kind}:${source}->${target}`,
  source,
  target,
  kind,
});
const edges: WorkItemGraphEdge[] = [
  edge("A", "B"),
  edge("B", "C"),
  edge("D", "B"),
  edge("B", "X"),
  edge("B", "ref:pr", "ref"),
  edge("B", "ref:dep", "ref"),
  edge("C", "person:u", "person"),
  edge("R", "K1", "program"),
  edge("R", "K2", "program"),
];
const sizeOf = (node: WorkItemGraphNode) => (node.kind === "item" ? { width: 260, height: 92 } : { width: 200, height: 52 });

function centre(layout: ReturnType<typeof layoutWorkItemGraph>, id: string) {
  const p = layout.positions.get(id)!;
  const node = nodes.find((n) => n.id === id);
  const size = node ? sizeOf(node) : { width: 220, height: 56 };
  return { x: p.x + size.width / 2, y: p.y + size.height / 2, ...size, left: p.x, top: p.y };
}

test("a wait edge is drawn from the prerequisite to what it releases; a program edge keeps its direction", () => {
  assert.deepEqual(progressionEdge(edge("A", "B")), { ...edge("A", "B"), source: "B", target: "A" });
  assert.deepEqual(progressionEdge(edge("B", "ref:pr", "ref")).source, "ref:pr");
  assert.deepEqual(progressionEdge(edge("C", "person:u", "person")).source, "person:u");
  assert.deepEqual(progressionEdge(edge("R", "K1", "program")), edge("R", "K1", "program"));
});

test("left to right: C left of B left of A, the PR and the person left of what they release", () => {
  const laid = layoutWorkItemGraph(nodes, edges, { layout: "hierarchy-lr", groups: [], sizeOf });
  assert.ok(centre(laid, "C").x < centre(laid, "B").x, "C left of B");
  assert.ok(centre(laid, "B").x < centre(laid, "A").x, "B left of A");
  assert.ok(centre(laid, "B").x < centre(laid, "D").x, "the fork: B left of D");
  assert.ok(centre(laid, "ref:pr").x < centre(laid, "B").x, "the pull request left of B");
  assert.ok(centre(laid, "person:u").x < centre(laid, "C").x, "the person left of C");
  assert.ok(centre(laid, "R").x < centre(laid, "K1").x, "a program root left of its children");
});

test("top down: C above B above A", () => {
  const laid = layoutWorkItemGraph(nodes, edges, { layout: "hierarchy-tb", groups: [], sizeOf });
  assert.ok(centre(laid, "C").y < centre(laid, "B").y);
  assert.ok(centre(laid, "B").y < centre(laid, "A").y);
});

test("every layout draws every node with no two cards overlapping", () => {
  for (const layout of WORK_ITEM_GRAPH_LAYOUTS as readonly WorkItemGraphLayout[]) {
    for (const by of ["none", "program", "owner"] as const) {
      const groups = graphGroups(nodes, edges, by);
      const laid = layoutWorkItemGraph(nodes, edges, { layout, groups, sizeOf });
      assert.equal(laid.positions.size, nodes.length, `${layout}/${by}: every node placed`);
      const boxes = nodes.map((n) => centre(laid, n.id));
      for (let i = 0; i < boxes.length; i++) {
        for (let j = i + 1; j < boxes.length; j++) {
          const a = boxes[i];
          const b = boxes[j];
          const apart = a.left + a.width <= b.left || b.left + b.width <= a.left || a.top + a.height <= b.top || b.top + b.height <= a.top;
          assert.ok(apart, `${layout}/${by}: ${nodes[i].id} and ${nodes[j].id} overlap`);
        }
      }
    }
  }
});

test("group by program: the root and its children share a group, the rest is No program", () => {
  const groups = graphGroups(nodes, edges, "program");
  const program = groups.find((g) => g.key === "program:R")!;
  assert.deepEqual([...program.members].sort(), ["K1", "K2", "R"]);
  assert.equal(program.label, "R");
  assert.equal(groups[groups.length - 1].label, "No program", "the no-group bucket comes last");
});

test("group by owner: a reference and a person join the group of the item that waits for them", () => {
  const groups = graphGroups(nodes, edges, "owner");
  const devGroup = groups.find((g) => g.key === "owner:agent-dev")!;
  const uxGroup = groups.find((g) => g.key === "owner:agent-ux")!;
  assert.ok(devGroup.members.includes("ref:pr") && devGroup.members.includes("ref:dep"));
  assert.ok(uxGroup.members.includes("person:u"));
});

test("group by fleet, key prefix, status and priority", () => {
  const fleetOfAgent = new Map([["agent-dev", { id: "f1", name: "CEO Office" }]]);
  const fleets = graphGroups(nodes, edges, "fleet", { fleetOfAgent });
  assert.ok(fleets.find((g) => g.key === "fleet:f1")!.members.includes("A"));
  assert.ok(fleets.find((g) => g.label === "No fleet")!.members.includes("C"));
  assert.ok(graphGroups(nodes, edges, "prefix").find((g) => g.label === "ux:")!.members.includes("D"));
  assert.ok(graphGroups(nodes, edges, "status").some((g) => g.key === "status:cancelled"));
  assert.ok(graphGroups(nodes, edges, "priority").find((g) => g.key === "priority:high")!.members.includes("D"));
  assert.deepEqual(graphGroups(nodes, edges, "none"), []);
});

test("grouped: every member sits inside its zone, under the zone's header", () => {
  const groups = graphGroups(nodes, edges, "owner");
  const laid = layoutWorkItemGraph(nodes, edges, { layout: "hierarchy-lr", groups, sizeOf });
  assert.equal(laid.zones.length, groups.length);
  for (const zone of laid.zones) {
    for (const id of zone.group.members) {
      const c = centre(laid, id);
      assert.ok(c.left >= zone.x && c.top >= zone.y + 30, `${id} under the header of ${zone.group.key}`);
      assert.ok(c.left + c.width <= zone.x + zone.width && c.top + c.height <= zone.y + zone.height, `${id} inside ${zone.group.key}`);
    }
  }
});

test("a collapsed group is one node; its edges move onto it, deduplicated", () => {
  const groups = graphGroups(nodes, edges, "owner");
  const laid = layoutWorkItemGraph(nodes, edges, { layout: "hierarchy-lr", groups, collapsedKeys: new Set(["owner:agent-ux"]), sizeOf });
  const node = groupNodeId("owner:agent-ux");
  assert.ok(laid.positions.has(node));
  assert.ok(!laid.positions.has("C") && !laid.positions.has("D"));
  const toB = laid.edges.filter((e) => e.drawnSource === "B" && e.drawnTarget === node);
  const fromGroup = laid.edges.filter((e) => e.drawnSource === node && e.drawnTarget === "B");
  // B waits for C (C unblocks B) and D waits for B (B unblocks D): one edge each way.
  assert.equal(fromGroup.length, 1);
  assert.equal(toB.length, 1);
  assert.ok(!laid.edges.some((e) => e.drawnSource === e.drawnTarget), "no edge inside a collapsed group");
});

test("focus chains keep their meaning after the flip: upstream is what the item waits for", () => {
  const chains = focusChains(edges, "B");
  assert.ok(chains.upstream.has("C") && chains.upstream.has("ref:pr"));
  assert.ok(chains.downstream.has("A") && chains.downstream.has("D"));
  // The lit edges are the API's ids, the ones the drawn edges keep.
  const drawnIds = new Set(edges.map(progressionEdge).map((e) => e.id));
  for (const id of chains.edges) assert.ok(drawnIds.has(id));
});
