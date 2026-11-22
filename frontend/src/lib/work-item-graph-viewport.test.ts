import assert from "node:assert/strict";
import { test } from "node:test";

import { anchoredViewport, graphFitKey } from "./work-item-graph-viewport.ts";

const CHOICES = {
  layout: "hierarchy-lr",
  groupBy: "none",
  collapsedKeys: new Set<string>(),
  focusMode: "dim",
  selected: null,
  showDone: false,
};

test("a live refresh leaves the fit key alone: it carries no count (#1158)", () => {
  // The key is built from the reader's choices only, so an item created, updated
  // or dropped by a refresh never refits the canvas.
  assert.equal(graphFitKey(CHOICES), graphFitKey({ ...CHOICES, collapsedKeys: new Set<string>() }));
  assert.equal(graphFitKey(CHOICES).includes("151"), false);
});

test("each of the reader's own choices changes the fit key (#1158)", () => {
  const base = graphFitKey(CHOICES);
  for (const changed of [
    { ...CHOICES, layout: "hierarchy-tb" },
    { ...CHOICES, groupBy: "owner" },
    { ...CHOICES, collapsedKeys: new Set(["owner:a"]) },
    { ...CHOICES, focusMode: "isolate" },
    { ...CHOICES, showDone: true },
  ]) {
    assert.notEqual(graphFitKey(changed), base);
  }
});

test("a selection refits only when the focus isolates its chain (#1158)", () => {
  // Highlight keeps the whole graph drawn: selecting a node never moves the camera.
  assert.equal(graphFitKey({ ...CHOICES, selected: "a" }), graphFitKey(CHOICES));
  const isolated = { ...CHOICES, focusMode: "isolate" };
  assert.notEqual(graphFitKey({ ...isolated, selected: "a" }), graphFitKey({ ...isolated, selected: "b" }));
});

test("the collapsed groups key does not depend on the order they were collapsed in", () => {
  assert.equal(
    graphFitKey({ ...CHOICES, collapsedKeys: new Set(["b", "a"]) }),
    graphFitKey({ ...CHOICES, collapsedKeys: new Set(["a", "b"]) }),
  );
});

test("the node at the centre of the canvas keeps its screen place when the layout moves it (#1158)", () => {
  // Canvas 1000 x 600 at zoom 2, panned so flow point (400, 200) sits at the centre.
  const viewport = { x: 500 - 400 * 2, y: 300 - 200 * 2, zoom: 2 };
  const before = new Map([
    ["far", { x: 0, y: 0 }],
    ["centre", { x: 395, y: 198 }],
  ]);
  // A new item pushed the centre node 120 right and 40 down.
  const after = new Map([
    ["far", { x: 0, y: 0 }],
    ["centre", { x: 515, y: 238 }],
    ["new", { x: 395, y: 198 }],
  ]);
  const next = anchoredViewport(before, after, viewport, { width: 1000, height: 600 });
  assert.deepEqual(next, { x: viewport.x - 240, y: viewport.y - 80, zoom: 2 });
  // Its screen place is the one it had.
  const screen = (p: { x: number; y: number }, v: { x: number; y: number; zoom: number }) => ({ x: p.x * v.zoom + v.x, y: p.y * v.zoom + v.y });
  assert.deepEqual(screen(after.get("centre")!, next!), screen(before.get("centre")!, viewport));
});

test("the zoom is kept and nothing moves when the anchor did not move (#1158)", () => {
  const viewport = { x: -300, y: -100, zoom: 1.5 };
  const same = new Map([["a", { x: 400, y: 250 }]]);
  assert.equal(anchoredViewport(same, new Map(same), viewport, { width: 800, height: 600 }), null);
});

test("an anchor that left the drawing hands over to the nearest node still drawn", () => {
  const viewport = { x: 0, y: 0, zoom: 1 };
  const before = new Map([
    ["gone", { x: 400, y: 300 }],
    ["near", { x: 450, y: 300 }],
  ]);
  const after = new Map([["near", { x: 470, y: 300 }]]);
  assert.deepEqual(anchoredViewport(before, after, viewport, { width: 800, height: 600 }), { x: -20, y: 0, zoom: 1 });
});

test("no anchor and no canvas: nothing to move", () => {
  const viewport = { x: 0, y: 0, zoom: 1 };
  assert.equal(anchoredViewport(new Map(), new Map([["a", { x: 1, y: 1 }]]), viewport, { width: 800, height: 600 }), null);
  assert.equal(anchoredViewport(new Map([["a", { x: 0, y: 0 }]]), new Map([["a", { x: 5, y: 5 }]]), viewport, { width: 0, height: 0 }), null);
});
