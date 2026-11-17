/**
 * Animated layout changes of the agent graph - `yarn test:unit` (vitest).
 */
import { test } from "vitest";
import assert from "node:assert/strict";

import {
  LAYOUT_TWEEN_MS,
  arcPath,
  drawnState,
  easeCubicInOut,
  edgeKeys,
  fitTransform,
  frameRate,
  layoutFrame,
  samePlacement,
  type TweenEdge,
  type TweenNode,
} from "./agent-graph-tween";

const node = (id: string, x: number, y: number, r = 10): TweenNode => ({ id, x, y, r });
const edge = (id: string, sourceId: string, targetId: string, a: TweenNode, b: TweenNode): TweenEdge => {
  const arc = { a: { x: a.x, y: a.y }, control: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 - 20 }, b: { x: b.x, y: b.y } };
  return { id, sourceId, targetId, arc, path: arcPath(arc), midX: 0, midY: 0 };
};
const keyOf = (e: TweenEdge) => `${e.sourceId}>${e.targetId}`;

test("the tween lasts between 600 and 800 ms", () => {
  assert.ok(LAYOUT_TWEEN_MS >= 600 && LAYOUT_TWEEN_MS <= 800);
});

test("ease cubic in-out starts and ends still, symmetric, clamped", () => {
  assert.equal(easeCubicInOut(0), 0);
  assert.equal(easeCubicInOut(1), 1);
  assert.equal(easeCubicInOut(0.5), 0.5);
  assert.ok(Math.abs(easeCubicInOut(0.25) + easeCubicInOut(0.75) - 1) < 1e-9);
  assert.ok(easeCubicInOut(0.1) < 0.1);
  assert.equal(easeCubicInOut(-1), 0);
  assert.equal(easeCubicInOut(2), 1);
});

test("a node that stays slides from where it is drawn to its target", () => {
  const from = node("a", 0, 0);
  const to = node("a", 100, 50);
  const start = drawnState([from], [], keyOf);
  const half = layoutFrame(start, { nodes: [to], edges: [] }, { nodes: [], edges: [] }, 0.5, keyOf);
  assert.deepEqual([half.nodes[0].x, half.nodes[0].y, half.nodes[0].fade], [50, 25, 1]);
  const end = layoutFrame(start, { nodes: [to], edges: [] }, { nodes: [], edges: [] }, 1, keyOf);
  assert.deepEqual([end.nodes[0].x, end.nodes[0].y, end.nodes[0].r], [100, 50, 10]);
});

test("a new node fades in and grows where it lands; a removed one fades out where it was", () => {
  const stays = node("a", 0, 0);
  const gone = node("b", 40, 40);
  const added = node("c", 80, 0);
  const start = drawnState([stays, gone], [], keyOf);
  const f = layoutFrame(start, { nodes: [stays, added], edges: [] }, { nodes: [gone], edges: [] }, 0.5, keyOf);
  const c = f.nodes.find((n) => n.id === "c")!;
  const b = f.nodes.find((n) => n.id === "b")!;
  assert.deepEqual([c.x, c.y, c.fade, c.r], [80, 0, 0.5, 7.5]);
  assert.deepEqual([b.x, b.y, b.fade, b.leaving], [40, 40, 0.5, true]);
  const end = layoutFrame(start, { nodes: [stays, added], edges: [] }, { nodes: [gone], edges: [] }, 1, keyOf);
  assert.equal(end.nodes.find((n) => n.id === "b")!.fade, 0);
  assert.equal(end.nodes.find((n) => n.id === "c")!.r, 10);
});

test("an edge stays attached to its two nodes on every frame", () => {
  const a0 = node("a", 0, 0), b0 = node("b", 100, 0);
  const a1 = node("a", 0, 100), b1 = node("b", 200, 100);
  const start = drawnState([a0, b0], [edge("e-0", "a", "b", a0, b0)], keyOf);
  for (const t of [0, 0.3, 0.5, 0.8, 1]) {
    const f = layoutFrame(start, { nodes: [a1, b1], edges: [edge("e-0", "a", "b", a1, b1)] }, { nodes: [], edges: [] }, t, keyOf);
    const [a, b] = f.nodes;
    const arc = f.edges[0].arc!;
    assert.ok(Math.abs(arc.a.x - a.x) < 1e-9 && Math.abs(arc.a.y - a.y) < 1e-9, `start at t=${t}`);
    assert.ok(Math.abs(arc.b.x - b.x) < 1e-9 && Math.abs(arc.b.y - b.y) < 1e-9, `end at t=${t}`);
    assert.equal(f.edges[0].path, arcPath(arc));
  }
});

test("an edge to a new node fades in with it; a leaving edge follows its moving node and fades out", () => {
  const a0 = node("a", 0, 0), b0 = node("b", 100, 0);
  const a1 = node("a", 50, 0), c1 = node("c", 0, 100);
  const start = drawnState([a0, b0], [edge("e-0", "a", "b", a0, b0)], keyOf);
  const f = layoutFrame(
    start,
    { nodes: [a1, c1], edges: [edge("e-0", "a", "c", a1, c1)] },
    { nodes: [b0], edges: [edge("e-0", "a", "b", a0, b0)] },
    0.5,
    keyOf,
  );
  const ac = f.edges.find((e) => e.targetId === "c")!;
  const ab = f.edges.find((e) => e.targetId === "b")!;
  assert.equal(ac.fade, 0.5);
  assert.equal(ab.leaving, true);
  assert.equal(ab.fade, 0.5);
  assert.deepEqual(ab.arc!.a, { x: 25, y: 0 });
  assert.deepEqual(ab.arc!.b, { x: 100, y: 0 });
});

test("edge keys stay stable whatever the index, and rank parallel edges", () => {
  assert.deepEqual(
    edgeKeys([{ sourceId: "a", targetId: "b" }, { sourceId: "a", targetId: "b" }, { sourceId: "b", targetId: "a" }]),
    ["a>b#0", "a>b#1", "b>a#0"],
  );
});

test("same placement ignores new objects at the same points, and sees a move, a node or an edge more", () => {
  const a = node("a", 0, 0), b = node("b", 10, 0);
  const e = edge("e-0", "a", "b", a, b);
  assert.ok(samePlacement({ nodes: [a, b], edges: [e] }, { nodes: [{ ...b, r: 4 }, { ...a }], edges: [{ ...e }] }));
  assert.ok(!samePlacement({ nodes: [a, b], edges: [e] }, { nodes: [a, { ...b, x: 11 }], edges: [e] }));
  assert.ok(!samePlacement({ nodes: [a, b], edges: [e] }, { nodes: [a, b], edges: [] }));
  assert.ok(!samePlacement({ nodes: [a], edges: [] }, { nodes: [a, b], edges: [] }));
});

test("fit to view centres the box and zooms to the tighter side, clamped", () => {
  const fit = fitTransform({ x0: 100, y0: 100, x1: 300, y1: 200 }, 1400, 600, 0, 0.3, 5);
  assert.equal(fit.zoom, 5);
  const wide = fitTransform({ x0: 0, y0: 0, x1: 2800, y1: 600 }, 1400, 600, 0, 0.3, 5);
  assert.equal(wide.zoom, 0.5);
  // The viewBox centre (W/2 - pan.x/zoom) lands on the box centre.
  assert.equal(1400 / 2 - wide.pan.x / wide.zoom, 1400);
  assert.equal(600 / 2 - wide.pan.y / wide.zoom, 300);
});

test("frame rate of a run", () => {
  assert.equal(frameRate(42, 700), 60);
  assert.equal(frameRate(0, 0), 0);
});
