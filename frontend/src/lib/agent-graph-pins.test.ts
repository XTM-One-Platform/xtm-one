/**
 * Agents pinned by hand on the agent map - `yarn test:unit` (vitest).
 */
import { test } from "vitest";
import assert from "node:assert/strict";

import { altArrowFocus, freeSpot, keyboardMove, nearestInDirection, nudged, paddedNameBox, pinDrawing, withPin } from "./agent-graph-pins";

const arc = { a: { x: 10, y: 0 }, control: { x: 50, y: 20 }, b: { x: 90, y: 0 } };
const nodes = [
  { id: "a", x: 0, y: 0 },
  { id: "b", x: 100, y: 0 },
  { id: "c", x: 200, y: 0 },
];
const edges = [
  { sourceId: "a", targetId: "b", path: "M 10,0 Q 50,20 90,0", midX: 50, midY: 10, arc },
  { sourceId: "b", targetId: "c", path: "M 110,0 Q 150,20 190,0", midX: 150, midY: 10, arc: { a: { x: 110, y: 0 }, control: { x: 150, y: 20 }, b: { x: 190, y: 0 } } },
];

test("nearestInDirection picks the closest agent in the arrow's cone, deterministic on ties", () => {
  const grid = [
    { id: "right-far", x: 300, y: 0 },
    { id: "right", x: 100, y: 10 },
    { id: "up", x: 0, y: -100 },
    { id: "diag", x: 80, y: 90 },
    { id: "tie-b", x: -100, y: 20 },
    { id: "tie-a", x: -100, y: -20 },
  ];
  const from = { x: 0, y: 0 };
  assert.equal(nearestInDirection(from, "ArrowRight", grid)?.id, "right");
  assert.equal(nearestInDirection(from, "ArrowUp", grid)?.id, "up");
  // (80, 90) is off the right cone (across > along) but inside the down one.
  assert.equal(nearestInDirection(from, "ArrowDown", grid)?.id, "diag");
  assert.equal(nearestInDirection(from, "ArrowLeft", grid)?.id, "tie-a");
  assert.equal(nearestInDirection(from, "ArrowLeft", [...grid].reverse())?.id, "tie-a");
  assert.equal(nearestInDirection(from, "Enter", grid), null);
  assert.equal(nearestInDirection(from, "ArrowUp", [{ id: "below", x: 0, y: 50 }]), null);
});

test("altArrowFocus handles every Alt+Arrow, a target or not, so the browser never goes Back", () => {
  const from = { x: 0, y: 0 };
  const right = [{ id: "right", x: 100, y: 0 }];
  // An agent that way: handled, focus moves.
  assert.deepEqual(altArrowFocus(from, "ArrowRight", right), { handled: true, next: right[0] });
  // The leftmost agent: nothing to the left, the key is still handled (no Back).
  assert.deepEqual(altArrowFocus(from, "ArrowLeft", right), { handled: true, next: null });
  for (const key of ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"]) {
    assert.equal(altArrowFocus(from, key, []).handled, true, key);
  }
  // Not an arrow: left to the browser.
  assert.deepEqual(altArrowFocus(from, "Enter", right), { handled: false, next: null });
  assert.deepEqual(altArrowFocus(from, "Tab", right), { handled: false, next: null });
});

test("no pin hands the same arrays back", () => {
  const out = pinDrawing(nodes, edges, new Map());
  assert.equal(out.nodes, nodes);
  assert.equal(out.edges, edges);
});

test("a pinned node moves, its arcs follow, the rest keeps its objects", () => {
  const out = pinDrawing(nodes, edges, new Map([["a", { x: 0, y: 40 }]]));
  assert.deepEqual(out.nodes[0], { id: "a", x: 0, y: 40 });
  assert.equal(out.nodes[1], nodes[1]);
  assert.equal(out.nodes[2], nodes[2]);
  // The arc's start moves with a, its end stays on b, its control by half.
  assert.deepEqual(out.edges[0].arc, { a: { x: 10, y: 40 }, control: { x: 50, y: 40 }, b: { x: 90, y: 0 } });
  assert.equal(out.edges[0].path, "M 10,40 Q 50,40 90,0");
  assert.equal(out.edges[0].midX, 50);
  assert.equal(out.edges[0].midY, 30);
  assert.equal(out.edges[1], edges[1]);
});

test("a pin where the node already is changes nothing", () => {
  const out = pinDrawing(nodes, edges, new Map([["b", { x: 100, y: 0 }]]));
  assert.equal(out.nodes, nodes);
  assert.equal(out.edges, edges);
});

test("withPin rounds and leaves the given map untouched", () => {
  const pins = new Map([["a", { x: 1, y: 1 }]]);
  const next = withPin(pins, "b", { x: 10.4, y: 19.6 });
  assert.equal(pins.size, 1);
  assert.deepEqual(next.get("b"), { x: 10, y: 20 });
  assert.deepEqual(next.get("a"), { x: 1, y: 1 });
});

test("freeSpot keeps a drop clear of every other node", () => {
  const others = [{ x: 0, y: 0 }, { x: 100, y: 0 }];
  const clearDrop = { x: 50, y: 60 };
  assert.equal(freeSpot(clearDrop, others, 40), clearDrop);
  // Dropped 10 to the right of the first node: moved out, away from it.
  const moved = freeSpot({ x: 10, y: 0 }, others, 40);
  for (const o of others) assert.ok(Math.hypot(o.x - moved.x, o.y - moved.y) >= 40);
  assert.ok(moved.x > 10 || Math.abs(moved.y) > 0);
  // Dropped exactly on a node: still clear of it.
  const onTop = freeSpot({ x: 100, y: 0 }, others, 40);
  for (const o of others) assert.ok(Math.hypot(o.x - onTop.x, o.y - onTop.y) >= 40);
  // Nothing else on the map: the drop is kept.
  const lone = { x: 3, y: 4 };
  assert.equal(freeSpot(lone, [], 40), lone);
});

test("freeSpot keeps a drop off a neighbour's name", () => {
  const neighbour = { x: 0, y: 0 };
  // Its name, 60 wide and 10 high, centred 20 under it.
  const name = { x: 0, y: 20, w: 60, h: 10 };
  const radius = 8;
  const offName = (p: { x: number; y: number }) => {
    const dx = Math.max(0, Math.abs(p.x - name.x) - name.w / 2);
    const dy = Math.max(0, Math.abs(p.y - name.y) - name.h / 2);
    return Math.hypot(dx, dy) >= radius;
  };
  // Dropped on the name, clear of the glyph: moved off the name too.
  const drop = { x: 20, y: 30 };
  assert.ok(Math.hypot(drop.x, drop.y) >= 30);
  assert.ok(!offName(drop));
  const spot = freeSpot(drop, [neighbour], 30, [name], radius);
  assert.ok(offName(spot), `on the name at ${spot.x},${spot.y}`);
  assert.ok(Math.hypot(spot.x - neighbour.x, spot.y - neighbour.y) >= 30);
  // Clear of both: kept as it is.
  const far = { x: 200, y: 0 };
  assert.equal(freeSpot(far, [neighbour], 30, [name], radius), far);
  // Without names the old rule holds: the drop stays.
  assert.equal(freeSpot(drop, [neighbour], 30), drop);
});

test("a padded ghost group line keeps the moved glyph's ring a clearance away", () => {
  const neighbour = { x: 0, y: 0 };
  // A ghost's group line, 40 wide and 6 high, centred 30 under it.
  const line = { id: "g", x: 0, y: 30, w: 40, h: 6 };
  const disc = 8;
  const ring = 10; // the outer status ring, drawn past the disc
  const pad = 4; // NAME_CLEARANCE_PX at one screen px per unit
  const padded = paddedNameBox(line, pad);
  assert.deepEqual(padded, { id: "g", x: 0, y: 30, w: 48, h: 14 });
  assert.equal(paddedNameBox(line, 0), line);
  const gapToLine = (p: { x: number; y: number }) => {
    const dx = Math.max(0, Math.abs(p.x - line.x) - line.w / 2);
    const dy = Math.max(0, Math.abs(p.y - line.y) - line.h / 2);
    return Math.hypot(dx, dy);
  };
  // Dropped just under the line: the bare box only keeps the disc off it.
  const drop = { x: 0, y: 44 };
  const bare = freeSpot(drop, [neighbour], 30, [line], disc);
  assert.ok(gapToLine(bare) - ring < pad, `bare box already clear at ${bare.x},${bare.y}`);
  // The padded box keeps the ring a clearance away.
  const spot = freeSpot(drop, [neighbour], 30, [padded], disc);
  assert.ok(gapToLine(spot) >= disc + pad - 1e-9, `ring on the line at ${spot.x},${spot.y}`);
});

test("keyboardMove keeps a moved node off a neighbour's name", () => {
  const name = { x: 0, y: 20, w: 60, h: 10 };
  const to = keyboardMove({ x: 0, y: 60 }, "ArrowUp", 30, [{ x: 0, y: 0 }], 30, [name], 8)!;
  const dx = Math.max(0, Math.abs(to.x - name.x) - name.w / 2);
  const dy = Math.max(0, Math.abs(to.y - name.y) - name.h / 2);
  assert.ok(Math.hypot(dx, dy) >= 8, `on the name at ${to.x},${to.y}`);
});

test("nudged moves by the step on an arrow key only", () => {
  assert.deepEqual(nudged({ x: 10, y: 10 }, "ArrowLeft", 5), { x: 5, y: 10 });
  assert.deepEqual(nudged({ x: 10, y: 10 }, "ArrowDown", 5), { x: 10, y: 15 });
  assert.equal(nudged({ x: 10, y: 10 }, "Enter", 5), null);
});

test("keyboardMove never lands a node on another one", () => {
  const others = [{ x: 0, y: -50 }];
  // One step up would put the node 10 from its neighbour: moved clear of it.
  const to = keyboardMove({ x: 0, y: 0 }, "ArrowUp", 40, others, 34);
  assert.ok(to);
  assert.ok(Math.hypot(to.x - others[0].x, to.y - others[0].y) >= 34);
  // A step into free room is the plain step.
  assert.deepEqual(keyboardMove({ x: 0, y: 0 }, "ArrowDown", 40, others, 34), { x: 0, y: 40 });
  assert.equal(keyboardMove({ x: 0, y: 0 }, "Tab", 40, others, 34), null);
});
