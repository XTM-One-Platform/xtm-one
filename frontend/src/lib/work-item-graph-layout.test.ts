import assert from "node:assert/strict";
import { test } from "node:test";

import {
  WORK_ITEM_GRAPH_LAYOUTS,
  floatingEndpoints,
  graphGroups,
  groupItemCount,
  groupNodeId,
  layoutWorkItemGraph,
  progressionEdge,
  routeEdge,
  segmentCrossesBox,
  type WorkItemGraphLayout,
} from "./work-item-graph-layout.ts";
import { drawnCounts, focusChains } from "./work-item-graph.ts";
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

test("radial: each connected part is rings around its root, never one row of every card (UX r2 B1 on #1067)", () => {
  const laid = layoutWorkItemGraph(nodes, edges, { layout: "radial", groups: [], sizeOf });
  const tops = new Set(nodes.map((n) => centre(laid, n.id).y));
  assert.ok(tops.size > 2, "the cards spread over several rows, not a single line");
  // The program part (R, K1, K2) sits apart from the chain part: no card of one inside the other's extent row by row.
  const chain = ["A", "B", "C", "D", "X", "ref:pr", "ref:dep", "person:u"].map((id) => centre(laid, id));
  const program = ["R", "K1", "K2"].map((id) => centre(laid, id));
  const box = (cards: ReturnType<typeof centre>[]) => ({
    left: Math.min(...cards.map((c) => c.left)),
    right: Math.max(...cards.map((c) => c.left + c.width)),
    top: Math.min(...cards.map((c) => c.top)),
    bottom: Math.max(...cards.map((c) => c.top + c.height)),
  });
  const a = box(chain);
  const b = box(program);
  assert.ok(a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top, "the two parts are packed apart");
});

test("a floating edge meets each card on the side facing the other, never inside either card", () => {
  const left = { x: 0, y: 0, width: 260, height: 92 };
  const right = { x: 600, y: 300, width: 260, height: 92 };
  const ends = floatingEndpoints(left, right);
  const onBorder = (p: { x: number; y: number }, b: typeof left) =>
    (Math.abs(p.x - b.x) < 1e-6 || Math.abs(p.x - (b.x + b.width)) < 1e-6 || Math.abs(p.y - b.y) < 1e-6 || Math.abs(p.y - (b.y + b.height)) < 1e-6) &&
    p.x >= b.x - 1e-6 && p.x <= b.x + b.width + 1e-6 && p.y >= b.y - 1e-6 && p.y <= b.y + b.height + 1e-6;
  assert.ok(onBorder(ends.source, left) && onBorder(ends.target, right));
  // The source end faces the target: on the right or bottom side of the left card, the target end on the left or top side.
  assert.ok(ends.source.x === left.x + left.width || ends.source.y === left.y + left.height);
  assert.ok(ends.target.x === right.x || ends.target.y === right.y);
  // Waiter on the left, prerequisite on the right (force): the edge leaves the prerequisite on its left side.
  const back = floatingEndpoints(right, { x: 0, y: 300, width: 260, height: 92 });
  assert.equal(back.source.x, right.x);
});

test("the status line counts what is drawn: Show only counts the focused chain (UX r2 B2 on #1067)", () => {
  const all = drawnCounts({ nodes, edges });
  assert.equal(all.items, nodes.filter((n) => n.kind === "item").length);
  assert.equal(all.links, edges.length);
  assert.equal(all.waitingForPerson, 1);
  const chains = focusChains(edges, "A");
  const kept = new Set(["A", ...chains.upstream, ...chains.downstream]);
  const isolated = { nodes: nodes.filter((n) => kept.has(n.id)), edges: edges.filter((e) => kept.has(e.source) && kept.has(e.target)) };
  const counts = drawnCounts(isolated);
  assert.equal(counts.items, isolated.nodes.filter((n) => n.kind === "item").length);
  assert.ok(counts.items < all.items, "Show only counts fewer items than the whole graph");
  const groups = graphGroups(isolated.nodes, isolated.edges, "program");
  assert.equal(groups.reduce((sum, g) => sum + groupItemCount(g, isolated.nodes), 0), counts.items, "zones and status line share one population");
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

test("a group counts its items only, the status line's population (UX r1 B2 on #1067)", () => {
  const groups = graphGroups(nodes, edges, "owner");
  const devGroup = groups.find((g) => g.key === "owner:agent-dev")!;
  const items = devGroup.members.filter((id) => !id.startsWith("ref:") && !id.startsWith("person:")).length;
  assert.ok(devGroup.members.length > items, "the group draws references beside its items");
  assert.equal(groupItemCount(devGroup, nodes), items);
  const total = groups.reduce((sum, group) => sum + groupItemCount(group, nodes), 0);
  assert.equal(total, nodes.filter((node) => node.kind === "item").length);
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

const ROUTE_CLEARANCE = 8;

/** The longest run two polylines' axis-aligned segments keep side by side, closer than `within` px. */
function sharedRun(a: readonly { x: number; y: number }[], b: readonly { x: number; y: number }[], within: number): number {
  let longest = 0;
  for (let i = 1; i < a.length; i++) {
    for (let j = 1; j < b.length; j++) {
      const [p, q, r, s] = [a[i - 1], a[i], b[j - 1], b[j]];
      if (p.y === q.y && r.y === s.y && Math.abs(p.y - r.y) < within) {
        longest = Math.max(longest, Math.min(Math.max(p.x, q.x), Math.max(r.x, s.x)) - Math.max(Math.min(p.x, q.x), Math.min(r.x, s.x)));
      } else if (p.x === q.x && r.x === s.x && Math.abs(p.x - r.x) < within) {
        longest = Math.max(longest, Math.min(Math.max(p.y, q.y), Math.max(r.y, s.y)) - Math.max(Math.min(p.y, q.y), Math.min(r.y, s.y)));
      }
    }
  }
  return longest;
}

// UX r3 B1 on #1067: a program child in another owner's zone, so the program
// edge crosses zones (the drawing where Program to P2 ran through P1).
const crossZoneNodes: WorkItemGraphNode[] = [
  ...nodes.filter((n) => n.id !== "K2"),
  item("K2", { status: "in_progress", owner_agent: ux }),
  item("P1", { status: "open", owner_agent: dev }),
  item("P2", { status: "open", owner_agent: ux }),
];
const crossZoneEdges: WorkItemGraphEdge[] = [...edges, edge("R", "P2", "program"), edge("P1", "K1")];

test("no edge segment runs inside a card that is not one of its ends, in every layout and grouping (UX r3 B1 on #1067)", () => {
  const programKey = "program:R";
  const cases: { by: "none" | "program" | "owner"; collapsed: string[] }[] = [
    { by: "none", collapsed: [] },
    { by: "program", collapsed: [] },
    { by: "program", collapsed: [programKey] },
    { by: "owner", collapsed: [] },
    { by: "owner", collapsed: ["owner:agent-ux"] },
  ];
  for (const [label, set] of [["seeded", { nodes, edges }], ["cross-zone", { nodes: crossZoneNodes, edges: crossZoneEdges }]] as const) {
    for (const layout of WORK_ITEM_GRAPH_LAYOUTS as readonly WorkItemGraphLayout[]) {
      for (const { by, collapsed } of cases) {
        for (const aspect of [16 / 9, 1]) {
          const groups = graphGroups(set.nodes, set.edges, by);
          const laid = layoutWorkItemGraph(set.nodes, set.edges, { layout, groups, collapsedKeys: new Set(collapsed), sizeOf, aspect });
          const byId = new Map(set.nodes.map((n) => [n.id, n]));
          const cards = new Map(
            [...laid.positions].map(([id, p]) => [id, { ...p, ...(byId.has(id) ? sizeOf(byId.get(id)!) : { width: 220, height: 56 }) }] as const),
          );
          const where = `${label} ${layout}/${by}${collapsed.length ? " collapsed" : ""}/${aspect.toFixed(2)}`;
          assert.ok(laid.edges.length > 0, `${where}: edges drawn`);
          for (const e of laid.edges) {
            assert.ok(e.points.length >= 2, `${where}: ${e.id} has a route`);
            for (const [id, card] of cards) {
              if (id === e.drawnSource || id === e.drawnTarget) continue;
              // UX r4 B1: the route keeps 8 px off every card that is not one of its ends.
              const kept = { x: card.x - ROUTE_CLEARANCE + 0.5, y: card.y - ROUTE_CLEARANCE + 0.5, width: card.width + 2 * ROUTE_CLEARANCE - 1, height: card.height + 2 * ROUTE_CLEARANCE - 1 };
              for (let i = 1; i < e.points.length; i++) {
                assert.ok(!segmentCrossesBox(e.points[i - 1], e.points[i], card), `${where}: ${e.id} runs through ${id}`);
                assert.ok(!segmentCrossesBox(e.points[i - 1], e.points[i], kept), `${where}: ${e.id} runs within ${ROUTE_CLEARANCE} px of ${id}`);
              }
            }
          }
          // UX r4 B1: unrelated edges never share a corridor, and no route rides a zone frame.
          for (let a = 0; a < laid.edges.length; a++) {
            for (let b = a + 1; b < laid.edges.length; b++) {
              const ea = laid.edges[a];
              const eb = laid.edges[b];
              if ([ea.drawnSource, ea.drawnTarget].some((id) => id === eb.drawnSource || id === eb.drawnTarget)) continue;
              const shared = sharedRun(ea.points, eb.points, 4);
              assert.ok(shared <= 1, `${where}: ${ea.id} and ${eb.id} share ${shared} px of one line`);
            }
          }
          for (const zone of laid.zones) {
            const frame = [
              { x: zone.x, y: zone.y },
              { x: zone.x + zone.width, y: zone.y },
              { x: zone.x + zone.width, y: zone.y + zone.height },
              { x: zone.x, y: zone.y + zone.height },
              { x: zone.x, y: zone.y },
            ];
            for (const e of laid.edges) {
              const ride = sharedRun(e.points, frame, ROUTE_CLEARANCE);
              assert.ok(ride <= 20, `${where}: ${e.id} rides the frame of ${zone.group.key} for ${ride} px`);
            }
          }
        }
      }
    }
  }
});

test("a ranked edge leaves the prerequisite on its trailing side and enters on the leading side; a clear route stays direct", () => {
  const source = { x: 0, y: 0, width: 260, height: 92 };
  const target = { x: 400, y: 200, width: 260, height: 92 };
  const extent = { x: 0, y: 0, width: 660, height: 292 };
  const lr = routeEdge(source, target, "lr", [source, target], extent);
  assert.deepEqual(lr[0], { x: 260, y: 46 });
  assert.deepEqual(lr[lr.length - 1], { x: 400, y: 246 });
  assert.equal(lr.length, 4, "the direct step: out, across, in");
  // A card in the way of the step: the route goes around it.
  const blocker = { x: 300, y: 0, width: 60, height: 292 };
  const around = routeEdge(source, target, "lr", [source, target, blocker], { x: 0, y: 0, width: 660, height: 292 });
  for (let i = 1; i < around.length; i++) assert.ok(!segmentCrossesBox(around[i - 1], around[i], blocker));
  assert.deepEqual(around[0], lr[0]);
  assert.deepEqual(around[around.length - 1], lr[lr.length - 1]);
});

test("focus chains keep their meaning after the flip: upstream is what the item waits for", () => {
  const chains = focusChains(edges, "B");
  assert.ok(chains.upstream.has("C") && chains.upstream.has("ref:pr"));
  assert.ok(chains.downstream.has("A") && chains.downstream.has("D"));
  // The lit edges are the API's ids, the ones the drawn edges keep.
  const drawnIds = new Set(edges.map(progressionEdge).map((e) => e.id));
  for (const id of chains.edges) assert.ok(drawnIds.has(id));
});
