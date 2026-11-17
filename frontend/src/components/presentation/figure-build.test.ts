/**
 * Unit tests for a figure scene's build - `yarn test:unit`.
 *
 * What is pinned is what the render relies on: the nodes of a plane are read
 * row by row, the camera never zooms past what keeps the figure readable and
 * never leaves it, it moves to each step in `CAMERA_MOVE` and is on the whole
 * figure for the scene's last `WHOLE_HOLD` seconds, a framing keeps clear of
 * the subtitles' band, and the spans where something moves (the only frames
 * the render draws) cover every step and every camera move and nothing else.
 * Built, a drifting figure zooms in eased from 1.00 to 1.06 by the end
 * of the scene, about the middle of the room the subtitles leave, from the
 * moment the camera is back on the whole figure (or the figure is built).
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  CAMERA_MOVE,
  COUNT_SECONDS,
  DRIFT_ZOOM,
  FIGURE_PARTS,
  EDGE_DELAY,
  EDGE_SECONDS,
  MAX_ZOOM,
  PIECE_SECONDS,
  STAGGER_SECONDS,
  WHOLE_HOLD,
  activeSpans,
  builtAt,
  cameraAt,
  cameraKeyframes,
  countAt,
  driftAt,
  driftFrom,
  drifted,
  framing,
  hubNode,
  levelGroups,
  rows,
  settleSeconds,
  stepFrame,
  union,
  within,
  type Rect,
} from "./figure-build.ts";

const FIGURE: Rect = { x: 0, y: 0, w: 1600, h: 900 };

test("an org chart is built level by level: the root, each level, its bubbles growing, the dotted lines last", () => {
  const parts = FIGURE_PARTS.org_chart;
  assert.equal(parts.levels, "data-level");
  assert.match(parts.grow ?? "", /data-shape="circle"/);
  assert.match(parts.late ?? "", /data-dotted/);
  assert.match(parts.edges ?? "", /:not\(\[data-dotted\]\)/);
  // The seats in markup order, by level: the root first, then each level in order.
  assert.deepEqual(levelGroups([0, 1, 1, 2, 2, 1, 2]), [[0], [1, 2, 5], [3, 4, 6]]);
  assert.deepEqual(levelGroups([2, 0, Number.NaN, 1]), [[1], [3], [0], [2]]);
  assert.deepEqual(levelGroups([]), []);
});

test("a count counts up from 0 to its value, from when its piece comes in", () => {
  assert.equal(countAt(47, 2, 0), 0);
  assert.equal(countAt(47, 2, 2), 0);
  assert.ok(countAt(47, 2, 2 + COUNT_SECONDS / 3) > 0);
  assert.ok(countAt(47, 2, 2 + COUNT_SECONDS / 3) < 47);
  assert.equal(countAt(47, 2, 2 + COUNT_SECONDS), 47);
  assert.equal(countAt(47, 2, 60), 47);
  // Never past its value, never backwards.
  let last = 0;
  for (let t = 2; t <= 2 + COUNT_SECONDS; t += 0.05) {
    const shown = countAt(47, 2, t);
    assert.ok(shown >= last && shown <= 47);
    last = shown;
  }
});

test("the union of rects ignores the empty ones", () => {
  assert.equal(union([]), null);
  assert.equal(union([{ x: 5, y: 5, w: 0, h: 0 }]), null);
  assert.deepEqual(
    union([
      { x: 10, y: 20, w: 100, h: 50 },
      { x: 0, y: 0, w: 0, h: 0 },
      { x: 200, y: 100, w: 40, h: 40 },
    ]),
    { x: 10, y: 20, w: 230, h: 120 },
  );
});

test("the nodes of a plane are read row by row, each row left to right", () => {
  const rects: Rect[] = [
    { x: 600, y: 400, w: 100, h: 60 },
    { x: 0, y: 300, w: 100, h: 60 },
    { x: 610, y: 100, w: 100, h: 60 },
    { x: 5, y: 105, w: 100, h: 60 },
    // Spanning the whole first row: its centre stands where the row does.
    { x: 0, y: 0, w: 700, h: 60 },
  ];
  assert.deepEqual(rows(rects), [[4], [3, 2], [1], [0]]);
  assert.deepEqual(rows([]), []);
});

test("a step is framed no closer than the zoom limit, inside the figure", () => {
  const small = stepFrame({ x: 1550, y: 860, w: 20, h: 20 }, FIGURE);
  assert.equal(small.w, FIGURE.w / MAX_ZOOM);
  assert.equal(small.h, FIGURE.h / MAX_ZOOM);
  assert.ok(small.x + small.w <= FIGURE.w && small.y + small.h <= FIGURE.h);
  const large = stepFrame({ x: -50, y: 0, w: 2000, h: 300 }, FIGURE);
  assert.equal(large.w, FIGURE.w);
  assert.equal(large.x, 0);
});

test("the camera moves to each step as it comes in and is on the whole figure for the last seconds", () => {
  const steps: Rect[] = [
    { x: 0, y: 0, w: 300, h: 300 },
    { x: 1200, y: 500, w: 300, h: 300 },
    { x: 600, y: 0, w: 300, h: 300 },
  ];
  const plan = { times: [0.35, 4, 8], length: 16, camera: "follow" as const };
  const keys = cameraKeyframes(steps, FIGURE, plan);
  assert.deepEqual(keys[0], [0, stepFrame(steps[0], FIGURE)]);
  assert.ok(keys.every(([t], i) => i === 0 || t >= keys[i - 1][0]));
  // Each step is framed by the time it has come in, after a move of CAMERA_MOVE.
  for (const [i, at] of plan.times.entries()) {
    assert.deepEqual(cameraAt(keys, at + CAMERA_MOVE), stepFrame(steps[i], FIGURE));
  }
  // The whole figure from WHOLE_HOLD before the end.
  const [end, whole] = keys[keys.length - 1];
  assert.deepEqual(whole, FIGURE);
  assert.ok(Math.abs(end - (plan.length - WHOLE_HOLD)) < 1e-9);
  assert.deepEqual(cameraAt(keys, plan.length), FIGURE);
  // Still, a single step, or a scene too short to move in: the whole figure throughout.
  assert.deepEqual(cameraKeyframes(steps, FIGURE, { ...plan, camera: "still" }), [[0, FIGURE]]);
  assert.deepEqual(cameraKeyframes(steps.slice(0, 1), FIGURE, plan), [[0, FIGURE]]);
  assert.deepEqual(cameraKeyframes(steps, FIGURE, { ...plan, length: 2.5 }), [[0, FIGURE]]);
  assert.deepEqual(cameraKeyframes(steps, FIGURE, plan, true), [[0, FIGURE]]);
});

test("steps coming in within one move are framed together", () => {
  const steps: Rect[] = [
    { x: 0, y: 0, w: 300, h: 300 },
    { x: 1200, y: 500, w: 300, h: 300 },
    { x: 1200, y: 0, w: 300, h: 300 },
  ];
  const keys = cameraKeyframes(steps, FIGURE, { times: [0.35, 5, 5], length: 16, camera: "follow" });
  assert.deepEqual(cameraAt(keys, 5 + CAMERA_MOVE), stepFrame(union(steps.slice(1)) as Rect, FIGURE));
});

test("a step too late to be framed before the end is seen as the camera pulls back", () => {
  const steps: Rect[] = [
    { x: 0, y: 0, w: 300, h: 300 },
    { x: 1200, y: 500, w: 300, h: 300 },
  ];
  const plan = { times: [0.35, 13.9], length: 16, camera: "follow" as const };
  const keys = cameraKeyframes(steps, FIGURE, plan);
  assert.ok(keys.every(([, r]) => r === FIGURE || r.w === stepFrame(steps[0], FIGURE).w));
  assert.deepEqual(cameraAt(keys, plan.length - WHOLE_HOLD), FIGURE);
});

test("a node inside its zone does not stand on it: the camera still follows", () => {
  const zone: Rect = { x: 0, y: 0, w: 1600, h: 900 };
  const node: Rect = { x: 100, y: 100, w: 200, h: 120 };
  const keys = cameraKeyframes([zone, node], FIGURE, { times: [0.35, 4], length: 16, camera: "follow" });
  assert.deepEqual(cameraAt(keys, 4 + CAMERA_MOVE), stepFrame(node, FIGURE));
});

test("steps standing on one another, like a chart's series, keep the whole figure in view", () => {
  const series: Rect[] = [
    { x: 100, y: 100, w: 1400, h: 700 },
    { x: 140, y: 150, w: 1400, h: 650 },
  ];
  assert.deepEqual(cameraKeyframes(series, FIGURE, { times: [0.35, 3], length: 8, camera: "follow" }), [[0, FIGURE]]);
});

test("the camera moves smoothly between two keyframes", () => {
  const a: Rect = { x: 0, y: 0, w: 100, h: 100 };
  const b: Rect = { x: 100, y: 0, w: 100, h: 100 };
  const keys: [number, Rect][] = [
    [1, a],
    [2, b],
  ];
  assert.deepEqual(cameraAt(keys, 0), a);
  assert.equal(cameraAt(keys, 1.5).x, 50);
  assert.ok(cameraAt(keys, 1.1).x < 10);
  assert.deepEqual(cameraAt(keys, 3), b);
});

test("what the camera frames is centred in the viewport with a margin", () => {
  const view = framing({ x: 100, y: 100, w: 800, h: 450 }, { w: 1920, h: 1080 });
  assert.ok(view.scale > 2 && view.scale < 2.4);
  assert.ok(Math.abs(view.x + 500 * view.scale - 960) < 1e-6);
  assert.ok(Math.abs(view.y + 325 * view.scale - 540) < 1e-6);
});

test("a framing keeps clear of the subtitles' band", () => {
  const rect: Rect = { x: 0, y: 0, w: 1600, h: 900 };
  const viewport = { w: 1920, h: 1080 };
  const view = framing(rect, viewport, { top: 0, bottom: 200 });
  const bottom = view.y + rect.h * view.scale;
  assert.ok(bottom <= viewport.h - 200, `the figure ends at ${bottom}`);
  assert.ok(view.scale < framing(rect, viewport).scale);
  assert.ok(Math.abs(view.y + (rect.h / 2) * view.scale - (viewport.h - 200) / 2) < 1e-6);
});

test("a step has settled once its pieces are in and its edges drawn", () => {
  assert.equal(settleSeconds(1, 0), PIECE_SECONDS);
  assert.equal(settleSeconds(3, 0), PIECE_SECONDS + 2 * STAGGER_SECONDS);
  assert.equal(settleSeconds(1, 2), Math.max(PIECE_SECONDS, EDGE_DELAY + EDGE_SECONDS));
  assert.equal(settleSeconds(0, 0), 0);
});

test("the render draws every step and every camera move, and holds the rest", () => {
  const still: Rect = { x: 0, y: 0, w: 10, h: 10 };
  const moved: Rect = { x: 5, y: 0, w: 10, h: 10 };
  const keys: [number, Rect][] = [
    [0, still],
    [4, still],
    [4.9, moved],
  ];
  const spans = activeSpans([0.35, 1, 6], [0.95, 0.95, 1.2], keys);
  // The first two steps overlap into one span; the camera's move and the last step stand alone.
  assert.equal(spans.length, 3);
  assert.deepEqual(spans[0], [0.35, 1 + 0.95 + 0.05]);
  assert.deepEqual(spans[1], [4, 4.9]);
  assert.equal(spans[2][0], 6);
  assert.ok(Math.abs(spans[2][1] - (6 + 1.2 + 0.05)) < 1e-9);
  // A drifting figure moves from its first loop to the end of the scene: one span to the end.
  const moving = activeSpans([0.35, 1, 6], [0.95, 0.95, 1.2], keys, [[2.2, 16]]);
  assert.deepEqual(moving, [
    [0.35, 1 + 0.95 + 0.05],
    [2.2, 16],
  ]);
});

test("a built figure drifts in, eased, from 1.00 to 1.06 by the end of the scene", () => {
  assert.equal(driftAt(4, 6, 16), 1);
  assert.equal(driftAt(6, 6, 16), 1);
  assert.ok(Math.abs(driftAt(11, 6, 16) - (1 + DRIFT_ZOOM / 2)) < 1e-9);
  assert.ok(Math.abs(driftAt(16, 6, 16) - (1 + DRIFT_ZOOM)) < 1e-9);
  // Eased: slow to start and to end, quickest in the middle.
  assert.ok(driftAt(7, 6, 16) - 1 < (driftAt(11, 6, 16) - driftAt(10, 6, 16)));
  // A build that ends with the scene has no room to drift.
  assert.equal(driftAt(16, 16, 16), 1);
  // The render caps the drift so the figure stays inside the frame: the plan's amount, eased the same way.
  assert.ok(Math.abs(driftAt(16, 6, 16, 0.024) - 1.024) < 1e-9);
  assert.ok(Math.abs(driftAt(11, 6, 16, 0.024) - 1.012) < 1e-9);
  assert.equal(driftAt(16, 6, 16, 0), 1);
});

test("what a piece paints past its box counts, as far as what holds it lets it show", () => {
  const label = { x: -40, y: 10, w: 200, h: 30 };
  // A label overflowing its node to the left: cut by nothing, counted whole.
  assert.deepEqual(within(label, { x: -100, y: 0, w: 400, h: 100 }), label);
  // Cut by an ancestor that clips (an SVG's viewport, an overflow: hidden): only what shows.
  assert.deepEqual(within(label, { x: 0, y: 0, w: 120, h: 100 }), { x: 0, y: 10, w: 120, h: 30 });
  // Nothing of it shows: empty, left out of the union.
  assert.equal(union([within(label, { x: 500, y: 500, w: 10, h: 10 })]), null);
});

test("the drift starts when the camera is back on the whole figure, or once a still figure is built", () => {
  const followed: [number, Rect][] = [
    [0, { x: 0, y: 0, w: 10, h: 10 }],
    [13.4, { x: 0, y: 0, w: 10, h: 10 }],
    [14, FIGURE],
  ];
  assert.equal(driftFrom(followed, 12.5, 16), 14);
  assert.equal(driftFrom([[0, FIGURE]], 9.2, 16), 9.2);
  assert.equal(driftFrom([[0, FIGURE]], 18, 16), 16);
  assert.ok(Math.abs(builtAt([0.35, 4, 8], [0.95, 0.6, 1.2]) - 9.2) < 1e-9);
  assert.equal(builtAt([], []), 0);
});

test("the drift zooms about the middle of the room the subtitles leave, and keeps the figure clear of them", () => {
  const viewport = { w: 1920, h: 1080 };
  const safe = { top: 0, bottom: 200 };
  const view = framing(FIGURE, viewport, safe);
  const zoomed = drifted(view, viewport, safe, 1 + DRIFT_ZOOM);
  assert.ok(Math.abs(zoomed.scale - view.scale * (1 + DRIFT_ZOOM)) < 1e-9);
  const middle = (v: { scale: number; x: number; y: number }) => ({ x: v.x + (FIGURE.w / 2) * v.scale, y: v.y + (FIGURE.h / 2) * v.scale });
  assert.ok(Math.abs(middle(zoomed).x - middle(view).x) < 1e-6 && Math.abs(middle(zoomed).y - middle(view).y) < 1e-6);
  assert.ok(zoomed.y + FIGURE.h * zoomed.scale <= viewport.h - safe.bottom, "the drifted figure stays above the subtitles");
  assert.ok(zoomed.x >= 0 && zoomed.x + FIGURE.w * zoomed.scale <= viewport.w);
  // Not drifting yet: the very framing the camera gives, to the last digit.
  assert.deepEqual(drifted(view, viewport, safe, 1), view);
});

test("an architecture's frameless node never breathes, whatever its emphasis", () => {
  const parts = FIGURE_PARTS.architecture;
  assert.equal(parts.unframed, '.deck-arch-node[data-frame="false"]');
  assert.match(parts.core ?? "", /data-emphasis="core"/);
});

test("without a core in its markup, the node most edges meet is the core - if one stands out", () => {
  const star: [number, number][] = [
    [0, 1],
    [0, 2],
    [3, 0],
    [1, 2],
  ];
  assert.equal(hubNode(star, 4), 0);
  // A ring: every node is met as often, none stands out.
  assert.equal(
    hubNode(
      [
        [0, 1],
        [1, 2],
        [2, 3],
        [3, 0],
      ],
      4,
    ),
    -1,
  );
  // Two edges are not a hub, and an edge's end near no node counts for none.
  assert.equal(
    hubNode(
      [
        [0, 1],
        [0, -1],
      ],
      2,
    ),
    -1,
  );
});
