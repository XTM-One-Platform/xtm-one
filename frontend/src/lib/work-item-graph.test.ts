import assert from "node:assert/strict";
import { test } from "node:test";

import { waitsForReader } from "./work-item-context.ts";
import { criticalLeaves, focusChains, shownGraph } from "./work-item-graph.ts";
import type { WorkItemGraph, WorkItemGraphItemNode, WorkItemGraphNode } from "../types/index.ts";

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

// The acceptance's seeded set (#968): chain A waits B waits C, a fork (D waits
// B), a pull request and a deployment reference, a person card on C, a program
// R with children K1 and K2, a cancelled blocker X outside the filter, a done
// item Z and an isolated open item L.
const nodes: WorkItemGraphNode[] = [
  item("A"),
  item("B"),
  item("C", { needs_human: true, ask: { question: "Ship?", options: [{ key: "A", label: "Yes" }] } }),
  item("D"),
  item("R", { status: "open", is_program_root: true }),
  item("K1", { status: "open" }),
  item("K2", { status: "in_progress" }),
  item("X", { status: "cancelled", outside_filter: true }),
  item("Z", { status: "done" }),
  item("L", { status: "open" }),
  { id: "ref:pr", kind: "ref", ref_kind: "pull_request", label: "o/r#1", repo: "o/r", number: 1, state: null, satisfied: false, url: null },
  { id: "ref:dep", kind: "ref", ref_kind: "deployment", label: "o/r#1", repo: "o/r", number: 1, state: "deployed", satisfied: true, url: null },
  { id: "person:u", kind: "person", name: "Sam", is_you: true },
];
const edge = (source: string, target: string, kind: "blocked_on" | "ref" | "person" | "program" = "blocked_on") => ({
  id: `${kind}:${source}->${target}`,
  source,
  target,
  kind,
});
const graph: WorkItemGraph = {
  nodes,
  edges: [
    edge("A", "B"),
    edge("B", "C"),
    edge("D", "B"),
    edge("B", "X"),
    edge("B", "ref:pr", "ref"),
    edge("B", "ref:dep", "ref"),
    edge("C", "person:u", "person"),
    edge("R", "K1", "program"),
    edge("R", "K2", "program"),
    edge("Z", "C"),
  ],
  truncated: false,
};

test("done and cancelled are hidden by default, a blocker outside the filter stays", () => {
  const shown = shownGraph(graph, { showDone: false });
  const ids = shown.nodes.map((n) => n.id);
  assert.ok(!ids.includes("Z"), "a done item is hidden");
  assert.ok(ids.includes("X"), "the cancelled blocker outside the filter stays on its chain");
  assert.ok(!shown.edges.some((e) => e.source === "Z"));
  assert.equal(shown.isolated, 1, "L has no edge: counted, not drawn");
  assert.ok(!ids.includes("L"));
});

test("showing done and cancelled draws them with their edges", () => {
  const shown = shownGraph(graph, { showDone: true });
  assert.ok(shown.nodes.some((n) => n.id === "Z"));
  assert.ok(shown.edges.some((e) => e.id === "blocked_on:Z->C"));
});

test("focus on B lights what it waits for and what waits for it, recursively", () => {
  const chains = focusChains(graph.edges, "B");
  assert.deepEqual([...chains.upstream].sort(), ["C", "X", "person:u", "ref:dep", "ref:pr"]);
  assert.deepEqual([...chains.downstream].sort(), ["A", "D"], "Z waits for C, not for B");
  assert.ok(chains.edges.has("blocked_on:A->B") && chains.edges.has("person:C->person:u"));
  assert.ok(!chains.edges.has("program:R->K1"), "a program edge is not a wait");
});

test("focus on A reaches C through B; a sibling waiter D is not upstream", () => {
  const chains = focusChains(graph.edges, "A");
  assert.ok(chains.upstream.has("C") && chains.upstream.has("B"));
  assert.ok(!chains.upstream.has("D") && chains.downstream.size === 0);
});

test("the critical leaf is the item only a person can release", () => {
  const leaves = criticalLeaves(graph);
  assert.deepEqual([...leaves], ["C"]);
  // C also waiting for an open pull request is no longer a leaf: an event may release it.
  const withRef = { ...graph, edges: [...graph.edges, edge("C", "ref:pr", "ref")] };
  assert.deepEqual([...criticalLeaves(withRef)], []);
});

test("a graph item node answers the Waiting for me rule as a list row does (#1001)", () => {
  const asked = item("W", { needs_human: true, user_id: "u-1" });
  assert.equal(waitsForReader(asked, "u-1"), true);
  assert.equal(waitsForReader(asked, "u-2"), false);
  assert.equal(waitsForReader({ ...asked, waits_for_user_id: "u-2" }, "u-2"), true);
  assert.equal(waitsForReader({ ...asked, status: "done" }, "u-1"), false);
  assert.equal(waitsForReader({ ...asked, scheduled: true }, "u-1"), false);
});
