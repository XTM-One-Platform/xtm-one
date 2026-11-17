/**
 * Focus on the agent handover map (Agentic Flow > Map) - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { edgeTouchesFocus, focusNeighbourhood, focusOnGraph, isolateFocus } from "./agent-flow-focus.ts";

// a -> b -> c -> d, e -> b, f alone, g -> c
const EDGES = [
  { id: "ab", sourceId: "a", targetId: "b" },
  { id: "bc", sourceId: "b", targetId: "c" },
  { id: "cd", sourceId: "c", targetId: "d" },
  { id: "eb", sourceId: "e", targetId: "b" },
  { id: "gc", sourceId: "g", targetId: "c" },
];

const sorted = (ids: Iterable<string>) => [...ids].sort();

test("a handover belongs to the focus when either end is focused", () => {
  const focused = new Set(["b"]);
  assert.equal(edgeTouchesFocus(EDGES[0], focused), true);
  assert.equal(edgeTouchesFocus(EDGES[1], focused), true);
  assert.equal(edgeTouchesFocus(EDGES[2], focused), false);
});

test("the neighbourhood is the focus plus one handover away, both directions", () => {
  assert.deepEqual(sorted(focusNeighbourhood(EDGES, new Set(["b"]))), ["a", "b", "c", "e"]);
  assert.deepEqual(sorted(focusNeighbourhood(EDGES, new Set(["a", "d"]))), ["a", "b", "c", "d"]);
});

test("an agent without handovers is its own neighbourhood", () => {
  assert.deepEqual(sorted(focusNeighbourhood(EDGES, new Set(["f"]))), ["f"]);
  assert.deepEqual(isolateFocus(EDGES, new Set(["f"])), { nodeIds: new Set(["f"]), edges: [] });
});

test("isolating keeps the neighbourhood and the handovers touching a focused agent", () => {
  const isolated = isolateFocus(EDGES, new Set(["a"]));
  assert.deepEqual(sorted(isolated.nodeIds), ["a", "b"]);
  assert.deepEqual(isolated.edges.map((e) => e.id), ["ab"]);
});

test("isolating leaves out a handover between two neighbours", () => {
  const triangle = [
    { id: "xy", sourceId: "x", targetId: "y" },
    { id: "xz", sourceId: "x", targetId: "z" },
    { id: "yz", sourceId: "y", targetId: "z" },
  ];
  const isolated = isolateFocus(triangle, new Set(["x"]));
  assert.deepEqual(sorted(isolated.nodeIds), ["x", "y", "z"]);
  assert.deepEqual(isolated.edges.map((e) => e.id), ["xy", "xz"]);
});

test("isolating keeps the edges' own fields", () => {
  const [edge] = isolateFocus([{ sourceId: "x", targetId: "y", handoverType: "consult" }], new Set(["x"])).edges;
  assert.deepEqual(edge, { sourceId: "x", targetId: "y", handoverType: "consult" });
});

test("only the focused ids the graph draws make the focus", () => {
  const graph = new Set(["a", "b", "c"]);
  assert.deepEqual(focusOnGraph(new Set(["a", "gone"]), graph), new Set(["a"]));
  assert.deepEqual(focusOnGraph(new Set(["a", "c"]), graph), new Set(["a", "c"]));
});

test("a focus of unknown ids, an empty one or none focuses nothing", () => {
  const graph = new Set(["a", "b"]);
  assert.equal(focusOnGraph(new Set(["gone", "deleted"]), graph), null);
  assert.equal(focusOnGraph(new Set(), graph), null);
  assert.equal(focusOnGraph(null, graph), null);
  assert.equal(focusOnGraph(undefined, graph), null);
});
