import { test as r5test } from "vitest";
import r5assert from "node:assert/strict";
import { spreadNameBlocks, staticNameLines } from "./agent-graph-layout";

r5test("staticNameLines never cuts a name: two lines, three when two are too wide (#10147 UX r5)", () => {
  r5assert.deepEqual(staticNameLines("assistant", 6.72, 160), ["assistant"]);
  r5assert.deepEqual(staticNameLines("design review", 6.72, 160).join(" "), "design review");
  const long = staticNameLines("4 agents from Threat-informed exposure response", 5.6, 160);
  r5assert.ok(long.length <= 3);
  r5assert.equal(long.join(" "), "4 agents from Threat-informed exposure response");
  for (const l of long) r5assert.ok(!l.includes("…"));
});

r5test("spreadNameBlocks moves overlapping name blocks apart inside the frame", () => {
  const blocks = ["a", "b", "c"].map((id, i) => ({ id, x: 200 + i * 10, y: 100, w: 60, above: 8, below: 30 }));
  const out = spreadNameBlocks(blocks, { x0: 0, x1: 504, y0: 0, y1: 260 }, 8);
  const ps = blocks.map((b) => ({ ...b, ...out.get(b.id)! }));
  for (let i = 0; i < ps.length; i++) {
    r5assert.ok(ps[i].x - 30 >= 0 && ps[i].x + 30 <= 504);
    for (let j = i + 1; j < ps.length; j++) {
      const a = ps[i], b = ps[j];
      const apartX = Math.abs(a.x - b.x) >= 60 + 8 - 1e-6;
      const apartY = Math.abs(a.y - b.y) >= 38 + 8 - 1e-6;
      r5assert.ok(apartX || apartY, `${a.id} ${b.id}`);
    }
  }
});

/**
 * Layouts of the agent graph (Agentic Flow map, fleet views) - `yarn test:unit` (vitest).
 */
import { test } from "vitest";
import assert from "node:assert/strict";

import {
  GRAPH_LAYOUTS,
  arcControlPoint,
  arcRoutes,
  spreadAngles,
  keepArcInside,
  badgeAngles,
  breakCycles,
  clearCurvature,
  edgeCurvatures,
  namesOverGlyphs,
  fitNameStacks,
  nameRoomsPx,
  fleetNameRooms,
  placeArcLabels,
  quadraticPoint,
  wrapLabel,
  fitToCanvas,
  layoutGraph,
  layeredLayout,
  longestPathLayers,
  minimumDistance,
  orderLayers,
  orgChartLayout,
  orgChartMetrics,
  orgChartRoutes,
  orgChartTree,
  isolatedCluster,
  groupBlocks,
  connectedComponents,
  lyingFlat,
  glyphRadius,
  loneCellWidth,
  packShelves,
  packForCanvas,
  automaticForces,
  FORCE_LIMITS,
  radialLayout,
  labelBoxAcross,
  separate,
  separateLabelled,
  spacingFor,
  staticLabelEdgePx,
  truncateLabel,
} from "./agent-graph-layout";
import { ARROW_GAP, arrowHead } from "./agent-graph-view";

test("keepArcInside pulls an arc's apex back inside the canvas", () => {
  const box = { x0: 0, y0: 0, x1: 1400, y1: 600 };
  const a = { x: 100, y: 50 };
  const b = { x: 1300, y: 50 };
  // Bowed far up: the apex is at y = -100, out of the canvas.
  const control = { x: 700, y: -250 };
  assert.ok(quadraticPoint(a, control, b, 0.5).y < 0);
  const kept = keepArcInside(a, control, b, box);
  const apex = quadraticPoint(a, kept, b, 0.5);
  assert.ok(Math.abs(apex.y - 0) < 1e-9);
  assert.equal(apex.x, 700);
  // An arc inside keeps its control point object.
  const inside = { x: 700, y: 20 };
  assert.equal(keepArcInside(a, inside, b, box), inside);
});

const e = (sourceId: string, targetId: string) => ({ sourceId, targetId });

test("fitToCanvas keeps a short drawing on the base canvas, never enlarged past the cap", () => {
  const fit = fitToCanvas(300, 100, 1400, 600, 1600, 90, 70);
  assert.equal(fit.height, 600);
  assert.equal(fit.scale, 1.5);
});

test("fitToCanvas grows the canvas for a tall drawing instead of shrinking it", () => {
  // 48 agents left to right: a narrow, tall drawing.
  const fit = fitToCanvas(900, 1800, 1400, 600, 1600, 90, 70);
  assert.ok(fit.height > 600, `canvas grew (${fit.height})`);
  assert.ok(fit.height <= 1600);
  // The old fit, height-bound on a 600 canvas, scaled it to 460 / 1800.
  assert.ok(fit.scale > 460 / 1800 * 2, `scale ${fit.scale}`);
  assert.ok(1800 * fit.scale + 140 <= fit.height + 1);
  assert.ok(900 * fit.scale + 180 <= 1400 + 1);
});

test("fitToCanvas keeps a wider right pad clear for the zoom controls", () => {
  const even = fitToCanvas(2800, 100, 1400, 600, 720, 90, 70);
  const railed = fitToCanvas(2800, 100, 1400, 600, 720, 90, 70, undefined, 142);
  assert.ok(railed.scale < even.scale);
  // The drawing ends 142 units before the right edge, starting at the left pad.
  assert.ok(Math.abs(90 + 2800 * railed.scale - (1400 - 142)) < 1e-9);
});

test("fitToCanvas reports the width-bound scale across, never under the uniform one", () => {
  // Narrow and tall at the maximum height: height-bound, the width unused.
  const tall = fitToCanvas(300, 2000, 1400, 600, 720, 90, 70);
  assert.ok(tall.scale < 1);
  assert.equal(tall.across, 1.5);
  // Wide: width-bound, across is the uniform scale.
  const wide = fitToCanvas(2800, 100, 1400, 600, 720, 90, 70, undefined, 142);
  assert.equal(wide.across, wide.scale);
});

test("fitToCanvas stops growing at the maximum height", () => {
  const fit = fitToCanvas(100, 100000, 1400, 600, 1600, 90, 70);
  assert.equal(fit.height, 1600);
  assert.ok(100000 * fit.scale + 140 <= 1600 + 1);
});

test("truncateLabel keeps a short name and cuts a long one with an ellipsis", () => {
  assert.equal(truncateLabel("Analyst", 12), "Analyst");
  assert.equal(truncateLabel("Browser - Container Discovery", 12), "Browser - C\u2026");
  assert.equal(truncateLabel("Relationship Resolution", 12).length, 12);
  // A space before the cut is not kept in front of the ellipsis.
  assert.equal(truncateLabel("CTEM Validation", 6), "CTEM\u2026");
});

// The CEO Office shape: an orchestrator and six desks, two desks consulting each other.
const FLEET_IDS = ["ea", "mail", "analyst", "clerk", "comms", "dev", "ux"];
const FLEET_EDGES = [
  e("ea", "mail"), e("ea", "analyst"), e("ea", "clerk"), e("ea", "comms"), e("ea", "dev"), e("ea", "ux"),
  e("dev", "ux"), e("ux", "dev"),
];

function bigGraph(n: number) {
  const ids = Array.from({ length: n }, (_, i) => `agent-${String(i).padStart(3, "0")}`);
  const edges = [];
  for (let i = 1; i < n; i++) {
    if (i % 5 !== 0) edges.push(e(ids[Math.floor((i * 7) % i)], ids[i]));
    if (i % 11 === 0) edges.push(e(ids[i], ids[Math.floor(i / 2)]));
  }
  return { ids, edges };
}

test("a cycle is broken by reversing its back edge, deterministically", () => {
  const edges = [e("a", "b"), e("b", "c"), e("c", "a")];
  const acyclic = breakCycles(["a", "b", "c"], edges);
  assert.deepEqual(acyclic, [e("a", "b"), e("b", "c"), e("a", "c")]);
  assert.deepEqual(breakCycles(["c", "b", "a"], [...edges].reverse()), acyclic);
});

test("longest-path layering puts a node one layer under its deepest parent", () => {
  const layers = longestPathLayers(["a", "b", "c", "d"], [e("a", "b"), e("b", "c"), e("a", "c"), e("d", "c")]);
  assert.deepEqual(Object.fromEntries(layers), { a: 0, b: 1, c: 2, d: 0 });
});

test("barycentre ordering uncrosses two parallel chains", () => {
  const ordered = orderLayers([["a", "b"], ["y", "x"]], [e("a", "x"), e("b", "y")]);
  assert.deepEqual(ordered, [["a", "b"], ["x", "y"]]);
});

test("the hierarchy puts the orchestrator on top (TB) or on the left (LR)", () => {
  const tb = layeredLayout(FLEET_IDS, FLEET_EDGES, true);
  const lr = layeredLayout(FLEET_IDS, FLEET_EDGES, false);
  for (const id of FLEET_IDS.filter((x) => x !== "ea")) {
    assert.ok(tb.get("ea")!.y < tb.get(id)!.y, `${id} under ea`);
    assert.ok(lr.get("ea")!.x < lr.get(id)!.x, `${id} right of ea`);
  }
});

test("layerWrap spreads a wide top-down layer over several rows, never overlapping (#9760 B1)", () => {
  const spokes = Array.from({ length: 6 }, (_, i) => `s${i}`);
  const edges = spokes.map((s) => e("hub", s));
  const pos = layeredLayout(["hub", ...spokes], edges, true, { layerWrap: 4 });
  const rows = new Set(spokes.map((s) => pos.get(s)!.y));
  assert.equal(rows.size, 2);
  for (const s of spokes) assert.ok(pos.get(s)!.y > pos.get("hub")!.y, `${s} under the hub`);
  assert.ok(minimumDistance(pos.values()) >= 90 - 1e-6);
  // Unset, the layer stays one row.
  const flat = layeredLayout(["hub", ...spokes], edges, true);
  assert.equal(new Set(spokes.map((s) => flat.get(s)!.y)).size, 1);
});

test("the agents with no handover sit in a strip after the last layer", () => {
  const positions = layeredLayout(["a", "b", "lone"], [e("a", "b")], true);
  assert.ok(positions.get("lone")!.y > positions.get("b")!.y);
});

test("the force layout keeps the agents with no handover in rows under the connected ones", () => {
  const ids = [...FLEET_IDS, ...Array.from({ length: 20 }, (_, i) => `lone-${i}`)];
  const positions = layoutGraph("force", ids, FLEET_EDGES);
  const connectedBottom = Math.max(...FLEET_IDS.map((id) => positions.get(id)!.y));
  for (let i = 0; i < 20; i++) assert.ok(positions.get(`lone-${i}`)!.y > connectedBottom, `lone-${i} under`);
  assert.ok(minimumDistance(positions.values()) >= 90 - 1e-6);
});

test("the force layout packs separate components next to each other", () => {
  const ids = [...FLEET_IDS, "p1", "p2", "q1", "q2", "q3"];
  const edges = [...FLEET_EDGES, e("p1", "p2"), e("q1", "q2"), e("q2", "q3")];
  const positions = layoutGraph("force", ids, edges);
  const xs = [...positions.values()].map((p) => p.x);
  const ys = [...positions.values()].map((p) => p.y);
  // Twelve agents in three components fit a box a few label boxes wide.
  assert.ok(Math.max(...xs) - Math.min(...xs) < labelBoxAcross(90, 150) * 7, `width ${Math.max(...xs) - Math.min(...xs)}`);
  assert.ok(Math.max(...ys) - Math.min(...ys) < 90 * 8, `height ${Math.max(...ys) - Math.min(...ys)}`);
  assert.ok(minimumDistance(positions.values()) >= 90 - 1e-6);
});

test("the radial layout centres the selection and rings its neighbours", () => {
  const positions = radialLayout(FLEET_IDS, FLEET_EDGES, { centerId: "dev" });
  assert.deepEqual(positions.get("dev"), { x: 0, y: 0 });
  const r = (id: string) => Math.hypot(positions.get(id)!.x, positions.get(id)!.y);
  assert.ok(Math.abs(r("ea") - r("ux")) < 1e-6, "one hop: same ring");
  assert.ok(r("mail") > r("ea"), "two hops: outer ring");
});

test("the radial layout puts the agents it cannot reach in rows under the rings", () => {
  const ids = [...FLEET_IDS, ...Array.from({ length: 30 }, (_, i) => `lone-${i}`)];
  const positions = radialLayout(ids, FLEET_EDGES, { centerId: "ea" });
  const ringBottom = Math.max(...FLEET_IDS.map((id) => positions.get(id)!.y));
  for (let i = 0; i < 30; i++) assert.ok(positions.get(`lone-${i}`)!.y > ringBottom, `lone-${i} under`);
  assert.ok(minimumDistance(positions.values()) >= 90 - 1e-6);
});

for (const layout of GRAPH_LAYOUTS) {
  test(`${layout}: same positions whatever the input order`, () => {
    const a = layoutGraph(layout, FLEET_IDS, FLEET_EDGES, { centerId: "ea" });
    const b = layoutGraph(layout, [...FLEET_IDS].reverse(), [...FLEET_EDGES].reverse(), { centerId: "ea" });
    assert.deepEqual(Object.fromEntries(a), Object.fromEntries(b));
  });

  for (const size of [7, 150]) {
    test(`${layout}: no overlap at ${size} nodes`, () => {
      const { ids, edges } = size === 7 ? { ids: FLEET_IDS, edges: FLEET_EDGES } : bigGraph(size);
      const positions = layoutGraph(layout, ids, edges, { minDistance: 90, centerId: ids[0] });
      assert.equal(positions.size, ids.length);
      assert.ok(minimumDistance(positions.values()) >= 90 - 1e-6, `min distance ${minimumDistance(positions.values())}`);
    });
  }
}

// ─── Org chart ───

// The CEO Office fleet: the EA hands over to every desk, the Developer and the
// UX Director hand back to the EA and to each other.
const CEO_IDS = ["ea", "mail", "analyst", "clerk", "comms", "dev", "ux"];
const CEO_EDGES = [
  ...CEO_IDS.slice(1).map((id) => e("ea", id)),
  e("dev", "ea"), e("ux", "ea"), e("dev", "ux"), e("ux", "dev"),
];

type Seg = { a: { x: number; y: number }; b: { x: number; y: number } };
const segments = (points: { x: number; y: number }[]): Seg[] => points.slice(1).map((b, i) => ({ a: points[i], b }));

/** Whether an axis-aligned segment passes within `r` of a point. */
function segmentHitsDisc(s: Seg, c: { x: number; y: number }, r: number): boolean {
  const x0 = Math.min(s.a.x, s.b.x), x1 = Math.max(s.a.x, s.b.x);
  const y0 = Math.min(s.a.y, s.b.y), y1 = Math.max(s.a.y, s.b.y);
  const dx = Math.max(x0 - c.x, 0, c.x - x1);
  const dy = Math.max(y0 - c.y, 0, c.y - y1);
  return Math.hypot(dx, dy) < r;
}

function drawOrgChart(ids: string[], edges: { sourceId: string; targetId: string }[], options = { minDistance: 90, labelWidth: 150 }) {
  const pos = orgChartLayout(ids, edges, options);
  const r = 10;
  const nodes = new Map([...pos].map(([id, p]) => [id, { ...p, r }]));
  const routes = orgChartRoutes(ids, edges, nodes, orgChartMetrics(options), 1, 3);
  return { pos, nodes, routes, r };
}

test("orgchart: the CEO Office is a tree under the EA, children centred on one level", () => {
  const pos = orgChartLayout(CEO_IDS, CEO_EDGES);
  const ea = pos.get("ea")!;
  const desks = CEO_IDS.slice(1).map((id) => pos.get(id)!);
  for (const p of desks) assert.ok(p.y > ea.y, "every desk under the EA");
  assert.equal(new Set(desks.map((p) => p.y)).size, 1, "the six desks on one level");
  const xs = desks.map((p) => p.x);
  assert.ok(Math.abs((Math.min(...xs) + Math.max(...xs)) / 2 - ea.x) < 1e-6, "centred under the EA");
  assert.ok(minimumDistance(pos.values()) >= 150 - 1e-6);
});

test("orgchart: a wide level folds its leaves into columns", () => {
  const ids = ["boss", ...Array.from({ length: 30 }, (_, i) => `leaf-${String(i).padStart(2, "0")}`)];
  const edges = ids.slice(1).map((id) => e("boss", id));
  const tree = orgChartTree(ids, edges);
  assert.ok(tree.folded.has("boss"));
  const pos = orgChartLayout(ids, edges, { minDistance: 90, labelWidth: 150 });
  const leafXs = new Set(ids.slice(1).map((id) => pos.get(id)!.x));
  assert.ok(leafXs.size <= 6, `${leafXs.size} columns`);
  assert.ok(minimumDistance(pos.values()) >= 90 - 1e-6);
  // Two leaves of a column stand a level apart, so each name has the room
  // under its card before the next one.
  const m = orgChartMetrics({ minDistance: 90, labelWidth: 150 });
  for (const x of leafXs) {
    const ys = ids.slice(1).map((id) => pos.get(id)!).filter((p) => p.x === x).map((p) => p.y).sort((a, b) => a - b);
    for (let i = 1; i < ys.length; i++) assert.ok(ys[i] - ys[i - 1] >= m.levelHeight - 1e-6, `column ${x}: rows ${ys[i] - ys[i - 1]} apart`);
  }
});

test("orgchart: every folded leaf has the room for a drawn name at the 1024 fit", () => {
  // The UX lab's case: a parent with 10 handover
  // targets, one handing back. The renderer's name room is the screen gap to
  // the nearest card whose name sits on the same row (agent-flow.tsx
  // nameRoomPx); a name needs MIN_LABEL_CHARS (8) at 11 px. Probed at the
  // lab's 1024 scale: one column pitch drawn 37 px wide.
  const ids = ["boss", ...Array.from({ length: 10 }, (_, i) => `leaf-${i}`)];
  const edges = [...ids.slice(1).map((id) => e("boss", id)), e("leaf-0", "boss")];
  assert.ok(orgChartTree(ids, edges).folded.has("boss"));
  const options = { minDistance: 90, labelWidth: 150 };
  const m = orgChartMetrics(options);
  const pos = orgChartLayout(ids, edges, options);
  // Round 2 (B1 on f5a3a826): the lab's 1024 fit draws one column pitch
  // 46.1 px wide, and a name with room across was still left out because it
  // covered the glyph of the next column's leaf one row pitch lower
  // (namesOverGlyphs). The renderer's boxes, end to end, at the lab's
  // measured glyph box (about 30 px with its 4 px clearance), names at 11 px.
  const px = 46.1 / (m.unit + m.spineRoom);
  const reach = 0.8;
  const r = (30 - 8) / 2 / px - reach;
  const nodes = [...pos].map(([id, p]) => ({ id, x: p.x, y: p.y, r, reach, labelRoom: m.unit }));
  const rooms = nameRoomsPx(nodes, { pxPerUnit: px, namePx: 11, lonePx: 160, gapPx: 8, clearancePx: 4 });
  const nameSize = 11 / px;
  const names = nodes.map((n) => {
    const chars = Math.min("Fold leaf analyst 10".length, Math.floor((rooms.get(n.id)! - 8) / (11 * 0.56)));
    const h = nameSize * 1.2;
    return { id: n.id, chars, box: { x: n.x, y: n.y + n.r + 3 + h / 2, w: chars * nameSize * 0.56, h } };
  });
  const side = 2 * (r + reach) + (2 * 4) / px;
  const glyphs = nodes.map((n) => ({ id: n.id, box: { x: n.x, y: n.y, w: side, h: side } }));
  for (const name of names.filter((n) => n.id !== "boss")) {
    assert.ok(name.chars >= 8, `${name.id}: ${rooms.get(name.id)!.toFixed(1)} px of room, ${name.chars} chars`);
  }
  assert.deepEqual([...namesOverGlyphs(names, glyphs)], [], "no fitted name covers a neighbour's glyph");
});

test("nameRoomsPx: a name is cut before a lower neighbour's glyph, not only before its row neighbour", () => {
  const metrics = { pxPerUnit: 1, namePx: 11, lonePx: 160, gapPx: 8, clearancePx: 4 };
  // b sits 30 px right and 20 px lower: off a's row, but its glyph box
  // (2 * (6 + 0.8) + 8 = 21.6 px) reaches a's name band (y 9 to 22.2).
  const a = { id: "a", x: 0, y: 0, r: 6, reach: 0.8, labelRoom: 200 };
  const b = { id: "b", x: 30, y: 20, r: 6, reach: 0.8, labelRoom: 200 };
  const rooms = nameRoomsPx([a, b], metrics);
  assert.ok(Math.abs(rooms.get("a")! - (2 * 30 - 21.6 + 8)) < 1e-9, `a: ${rooms.get("a")}`);
  // Far below the name band, b only limits nothing: a keeps its layout room.
  const far = nameRoomsPx([a, { ...b, y: 80 }], metrics);
  assert.equal(far.get("a"), 200);
  // A node with no layout room gets no entry.
  assert.equal(nameRoomsPx([{ ...a, labelRoom: undefined }], metrics).size, 0);
});

test("nameRoomsPx: the canvas edge cuts a name like a neighbour", () => {
  const metrics = { pxPerUnit: 2, namePx: 11, lonePx: 160, gapPx: 8, clearancePx: 4, canvasWidth: 1400 };
  // 30 units from the right edge at 2 px per unit: 2 * 30 * 2 = 120 px of room,
  // so the centred name never runs past the canvas.
  const edge = nameRoomsPx([{ id: "e", x: 1370, y: 0, r: 6, reach: 0.8, labelRoom: 200 }], metrics);
  assert.equal(edge.get("e"), 120);
  // Same from the left edge.
  const left = nameRoomsPx([{ id: "l", x: 10, y: 0, r: 6, reach: 0.8, labelRoom: 200 }], metrics);
  assert.equal(left.get("l"), 40);
  // In the middle, the layout room is kept; without canvasWidth nothing changes.
  const mid = nameRoomsPx([{ id: "m", x: 700, y: 0, r: 6, reach: 0.8, labelRoom: 200 }], metrics);
  assert.equal(mid.get("m"), 400);
  const free = nameRoomsPx([{ id: "e", x: 1370, y: 0, r: 6, reach: 0.8, labelRoom: 200 }], { ...metrics, canvasWidth: undefined });
  assert.equal(free.get("e"), 400);
});

test("nameRoomsPx: a name near the right edge stops before the zoom controls docked there", () => {
  // At 0.77 px per unit a 1400-unit canvas is 1078 px wide; the zoom rail
  // keeps 44 px at its right edge (x 1034 to 1078 on screen).
  const px = 0.77;
  const reserve = 44;
  const metrics = { pxPerUnit: px, namePx: 11, lonePx: 160, gapPx: 8, clearancePx: 4, canvasWidth: 1400, rightReservePx: reserve };
  const railLeft = 1400 * px - reserve;
  for (const x of [1200, 1250, 1300, 1340]) {
    const room = nameRoomsPx([{ id: "n", x, y: 0, r: 6, reach: 0.8, labelRoom: 400 }], metrics).get("n")!;
    // The name is centred under its node: its right end is x * px + room / 2.
    assert.ok(x * px + room / 2 <= railLeft + 1e-9, `name at x ${x} ends at ${x * px + room / 2}, rail at ${railLeft}`);
    // Without the reserve the same name ran into the rail.
    const bare = nameRoomsPx([{ id: "n", x, y: 0, r: 6, reach: 0.8, labelRoom: 400 }], { ...metrics, rightReservePx: undefined }).get("n")!;
    assert.ok(x * px + bare / 2 > railLeft);
  }
  // The left edge and the middle are unchanged by the right reserve.
  assert.equal(nameRoomsPx([{ id: "l", x: 10, y: 0, r: 6, reach: 0.8, labelRoom: 400 }], metrics).get("l"), 2 * 10 * px);
  assert.equal(nameRoomsPx([{ id: "m", x: 700, y: 0, r: 6, reach: 0.8, labelRoom: 400 }], metrics).get("m"), 400 * px);
});

test("fleetNameRooms: a name runs past its grid to the next name, glyph or edge", () => {
  const a = { id: "a", x: 100, y: 100, width: 60 };
  const b = { id: "b", x: 300, y: 100, width: 200 };
  const below = { id: "c", x: 120, y: 400, width: 40 };
  const rooms = fleetNameRooms([a, b, below], [], 1400, 16, 10);
  // To the next grid on its row, less the gap.
  assert.equal(rooms.get("a"), 300 - 10 - 100);
  // The right-most grid runs to the canvas edge.
  assert.equal(rooms.get("b"), 1400 - 300);
  // A grid on another row is not cut by either.
  assert.equal(rooms.get("c"), 1400 - 120);
  // A glyph reaching into the band above the grid stops the name before it.
  const withGlyph = fleetNameRooms([a, b], [{ x: 220, y: 95, r: 8 }], 1400, 16, 10);
  assert.equal(withGlyph.get("a"), 220 - 8 - 10 - 100);
  // A glyph of the grid itself, or one below the band, does not.
  const own = fleetNameRooms([a, b], [{ x: 150, y: 110, r: 8 }, { x: 220, y: 130, r: 8 }], 1400, 16, 10);
  assert.equal(own.get("a"), 190);
  // Never shorter than its own grid.
  const tight = fleetNameRooms([a, { ...b, x: 150 }], [], 1400, 16, 10);
  assert.equal(tight.get("a"), 60);
});

test("orgchart: edges are orthogonal, reciprocal pairs on parallel lanes, nothing crosses a card", () => {
  const { nodes, routes, r } = drawOrgChart(CEO_IDS, CEO_EDGES);
  assert.equal(routes.length, CEO_EDGES.length);
  CEO_EDGES.forEach((edge, k) => {
    const route = routes[k]!;
    assert.ok(route, `${edge.sourceId}->${edge.targetId} routed`);
    assert.match(route.path, /^M [-\d.]+,[-\d.]+( L [-\d.]+,[-\d.]+)+$/, "straight segments only, no curve");
    for (const s of segments(route.points)) {
      assert.ok(s.a.x === s.b.x || s.a.y === s.b.y, `${edge.sourceId}->${edge.targetId}: 90 degree elbows`);
      // Every segment but the first and the last stays off every card.
    }
    const inner = segments(route.points).slice(1, -1);
    for (const [id, c] of nodes) {
      for (const s of inner) assert.ok(!segmentHitsDisc(s, c, r), `${edge.sourceId}->${edge.targetId} crosses ${id}`);
    }
    // The arrow end sits off the target ring, the start on the source ring.
    const end = route.points[route.points.length - 1];
    const tgt = nodes.get(edge.targetId)!;
    assert.ok(Math.hypot(end.x - tgt.x, end.y - tgt.y) > r, "ends outside the target");
  });
  // A reciprocal pair never shares a point where it meets either card.
  for (const [a, b] of [["ea", "dev"], ["ea", "ux"], ["dev", "ux"]]) {
    const ab = routes[CEO_EDGES.findIndex((x) => x.sourceId === a && x.targetId === b)]!;
    const ba = routes[CEO_EDGES.findIndex((x) => x.sourceId === b && x.targetId === a)]!;
    assert.notDeepEqual(ab.points[0], ba.points[ba.points.length - 1], `${a}<->${b} apart on ${a}`);
    assert.notDeepEqual(ba.points[0], ab.points[ab.points.length - 1], `${a}<->${b} apart on ${b}`);
    assert.notEqual(ab.path, ba.path);
  }
});

/** Length two axis-aligned segments run on top of each other (0 when they only cross or touch). */
function collinearOverlap(p: Seg, q: Seg): number {
  const eps = 0.5;
  const vertical = (s: Seg) => Math.abs(s.a.x - s.b.x) < 1e-9;
  if (vertical(p) !== vertical(q)) return 0;
  const [across, along] = vertical(p) ? ["x", "y"] as const : ["y", "x"] as const;
  if (Math.abs(p.a[across] - q.a[across]) > eps) return 0;
  const lo = Math.max(Math.min(p.a[along], p.b[along]), Math.min(q.a[along], q.b[along]));
  const hi = Math.min(Math.max(p.a[along], p.b[along]), Math.max(q.a[along], q.b[along]));
  return Math.max(0, hi - lo);
}

/**
 * Pairs of distinct edges that share a collinear stretch longer than 2 px,
 * siblings on their parent's bus and a folded column's branches and
 * hand-backs on its spine excepted.
 */
function sharedSegments(ids: string[], edges: { sourceId: string; targetId: string }[], scale = 1): string[] {
  const options = { minDistance: 90, labelWidth: 150 };
  const pos = orgChartLayout(ids, edges, options);
  const nodes = new Map([...pos].map(([id, p]) => [id, { x: p.x * scale, y: p.y * scale, r: 17 * scale }]));
  const routes = orgChartRoutes(ids, edges, nodes, orgChartMetrics(options), scale, 3 * scale);
  const tree = orgChartTree(ids, edges);
  const branchOf = (k: number) => tree.parent.get(edges[k].targetId) === edges[k].sourceId ? edges[k].sourceId : null;
  const foldedBackOf = (k: number) => {
    const { sourceId, targetId } = edges[k];
    return tree.parent.get(sourceId) === targetId && tree.folded.has(targetId) && (tree.children.get(sourceId) ?? []).length === 0
      ? targetId : null;
  };
  const found: string[] = [];
  for (let i = 0; i < edges.length; i++) for (let j = i + 1; j < edges.length; j++) {
    if (branchOf(i) && branchOf(i) === branchOf(j)) continue;
    const back = foldedBackOf(i) ?? foldedBackOf(j);
    if (back && [branchOf(i) ?? foldedBackOf(i), branchOf(j) ?? foldedBackOf(j)].every((p) => p === back)) continue;
    for (const p of segments(routes[i]!.points)) for (const q of segments(routes[j]!.points)) {
      const shared = collinearOverlap(p, q);
      if (shared > 2) {
        found.push(`${edges[i].sourceId}->${edges[i].targetId} and ${edges[j].sourceId}->${edges[j].targetId}: ${shared.toFixed(1)} px`);
      }
    }
  }
  return found;
}

test("orgchart: no two distinct edges share a segment, the CEO Office knot under the EA included", () => {
  for (const scale of [1, 0.6, 0.35]) assert.deepEqual(sharedSegments(CEO_IDS, CEO_EDGES, scale), [], `scale ${scale}`);
  // The UX lab's shape: the CEO Office, a Triage Lead with 9 folded desks and
  // one hand-back, and a 4-agent cycle.
  const triage = Array.from({ length: 9 }, (_, i) => `tdesk-${i}`);
  const ids = [...CEO_IDS, "tlead", ...triage, "enrich", "score", "report", "intake"];
  const edges = [
    ...CEO_EDGES,
    ...triage.map((id) => e("tlead", id)), e("tdesk-0", "tlead"),
    e("intake", "enrich"), e("enrich", "score"), e("score", "report"), e("report", "intake"),
  ];
  for (const scale of [1, 0.6, 0.35]) assert.deepEqual(sharedSegments(ids, edges, scale), [], `scale ${scale}`);
  // Every desk hands back to the EA, and two pairs of desks talk to each other.
  const busy = [...CEO_EDGES, e("mail", "ea"), e("clerk", "ea"), e("analyst", "ea"), e("comms", "ea"), e("mail", "clerk"), e("clerk", "mail"), e("analyst", "ux")];
  assert.deepEqual(sharedSegments(CEO_IDS, busy), []);
});

/**
 * Arrowheads drawn over another edge: each edge's arrowhead box (its length
 * back from the tip along the last segment, its width across) must touch no
 * segment of another edge. The heads and the port
 * gap are sized as the renderer does at `pxPerUnit` screen px per unit.
 */
function arrowheadsOnEdges(ids: string[], edges: { sourceId: string; targetId: string }[], scale: number, pxPerUnit: number): string[] {
  const options = { minDistance: 90, labelWidth: 150 };
  const pos = orgChartLayout(ids, edges, options);
  const r = Math.max(17 * scale, 6 / pxPerUnit);
  const head = arrowHead(r, pxPerUnit);
  const portGap = 2 * head.halfWidth + 1.2;
  const nodes = new Map([...pos].map(([id, p]) => [id, { x: p.x * scale, y: p.y * scale, r }]));
  const routes = orgChartRoutes(ids, edges, nodes, orgChartMetrics(options), scale, 2, portGap);
  const found: string[] = [];
  const eps = 0.05;
  routes.forEach((route, i) => {
    const pts = route!.points;
    const tip = pts[pts.length - 1], prev = pts[pts.length - 2];
    const len = Math.hypot(tip.x - prev.x, tip.y - prev.y) || 1;
    const ux = (tip.x - prev.x) / len, uy = (tip.y - prev.y) / len;
    const base = { x: tip.x - ux * head.length, y: tip.y - uy * head.length };
    const box = {
      x0: Math.min(tip.x, base.x) - Math.abs(uy) * head.halfWidth + eps,
      x1: Math.max(tip.x, base.x) + Math.abs(uy) * head.halfWidth - eps,
      y0: Math.min(tip.y, base.y) - Math.abs(ux) * head.halfWidth + eps,
      y1: Math.max(tip.y, base.y) + Math.abs(ux) * head.halfWidth - eps,
    };
    routes.forEach((other, j) => {
      if (i === j) return;
      for (const s of segments(other!.points)) {
        const sx0 = Math.min(s.a.x, s.b.x), sx1 = Math.max(s.a.x, s.b.x);
        const sy0 = Math.min(s.a.y, s.b.y), sy1 = Math.max(s.a.y, s.b.y);
        if (sx0 <= box.x1 && box.x0 <= sx1 && sy0 <= box.y1 && box.y0 <= sy1) {
          found.push(`${edges[i].sourceId}->${edges[i].targetId} lands on ${edges[j].sourceId}->${edges[j].targetId}`);
          return;
        }
      }
    });
  });
  return found;
}

test("orgchart: no arrowhead lands on another edge, ports an arrowhead's width apart", () => {
  const triage = Array.from({ length: 9 }, (_, i) => `tdesk-${i}`);
  const labIds = [...CEO_IDS, "tlead", ...triage, "enrich", "score", "report", "intake"];
  const labEdges = [
    ...CEO_EDGES,
    ...triage.map((id) => e("tlead", id)), e("tdesk-0", "tlead"),
    e("intake", "enrich"), e("enrich", "score"), e("score", "report"), e("report", "intake"),
  ];
  const busy = [...CEO_EDGES, e("mail", "ea"), e("clerk", "ea"), e("analyst", "ea"), e("comms", "ea"), e("mail", "clerk"), e("clerk", "mail"), e("analyst", "ux")];
  // 0.85 and 0.55 screen px per unit: the fitted map at 1440 and at 1024.
  for (const pxPerUnit of [0.85, 0.55]) {
    for (const scale of [1, 0.6, 0.35]) {
      const at = `scale ${scale}, ${pxPerUnit} px/unit`;
      assert.deepEqual(arrowheadsOnEdges(CEO_IDS, CEO_EDGES, scale, pxPerUnit), [], at);
      assert.deepEqual(arrowheadsOnEdges(labIds, labEdges, scale, pxPerUnit), [], `lab, ${at}`);
      assert.deepEqual(arrowheadsOnEdges(CEO_IDS, busy, scale, pxPerUnit), [], `busy, ${at}`);
    }
  }
});

/** A seeded graph with many incoming handovers per card: 60% of the edges land on a sixth of the agents. */
function hubGraph(seed: number, n: number, m: number) {
  let state = seed >>> 0;
  const rnd = () => (state = (state * 1664525 + 1013904223) >>> 0) / 2 ** 32;
  const ids = Array.from({ length: n }, (_, i) => `a${String(i).padStart(2, "0")}`);
  const hubs = ids.slice(0, Math.max(3, Math.floor(n / 6)));
  const seen = new Set<string>();
  const edges: { sourceId: string; targetId: string }[] = [];
  while (edges.length < m) {
    const s = ids[Math.floor(rnd() * n)];
    const t = rnd() < 0.6 ? hubs[Math.floor(rnd() * hubs.length)] : ids[Math.floor(rnd() * n)];
    if (s === t || seen.has(`${s}>${t}`)) continue;
    seen.add(`${s}>${t}`);
    edges.push(e(s, t));
  }
  return { ids, edges };
}

test("orgchart: no arrowhead lands on another edge on cards with many flank ends", () => {
  // The map's All scope: 43 handovers, several cards with 4 to 6 incoming
  // flank or corner ends. A flank row stops outside the face ends it would cross.
  for (let seed = 1; seed <= 12; seed++) {
    const { ids, edges } = hubGraph(seed, 30, 43);
    for (const pxPerUnit of [0.85, 0.55]) {
      for (const scale of [1, 0.6]) {
        assert.deepEqual(arrowheadsOnEdges(ids, edges, scale, pxPerUnit), [], `seed ${seed}, scale ${scale}, ${pxPerUnit} px/unit`);
      }
    }
  }
});

test("orgchart: no riser beside another card's face drop, row channels above the flank rows, at the 1024 fit", () => {
  // 0.556 px per unit is the map's All scope fitted at 1024: the port gap is
  // about 6.9 units, and a card side with several flank rows reaches above
  // the lowest channel, whose run then crossed the arrowhead of a flank end.
  // A card with many ends on one side packed its flank risers out to the
  // next card, beside that card's face drops (seeds 72 and 84): the risers
  // now stay within half the distance to it.
  for (let seed = 1; seed <= 120; seed++) {
    for (const n of [30, 48]) {
      const { ids, edges } = hubGraph(seed, n, 43);
      assert.deepEqual(arrowheadsOnEdges(ids, edges, 0.6, 0.556), [], `seed ${seed}, ${n} agents`);
    }
  }
});

/** Distance between two segments (any direction). */
function segmentDistance(p: Seg, q: Seg): number {
  const pointToSeg = (c: { x: number; y: number }, s: Seg) => {
    const vx = s.b.x - s.a.x, vy = s.b.y - s.a.y;
    const l2 = vx * vx + vy * vy;
    const t = l2 > 0 ? Math.max(0, Math.min(1, ((c.x - s.a.x) * vx + (c.y - s.a.y) * vy) / l2)) : 0;
    return Math.hypot(c.x - (s.a.x + t * vx), c.y - (s.a.y + t * vy));
  };
  const cross = (o: { x: number; y: number }, a: { x: number; y: number }, b: { x: number; y: number }) =>
    (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x);
  const d1 = cross(q.a, q.b, p.a), d2 = cross(q.a, q.b, p.b), d3 = cross(p.a, p.b, q.a), d4 = cross(p.a, p.b, q.b);
  if (((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0))) return 0;
  return Math.min(pointToSeg(p.a, q), pointToSeg(p.b, q), pointToSeg(q.a, p), pointToSeg(q.b, p));
}

/**
 * The UX Director's arrowhead probe: each
 * arrowhead is the triangle the renderer draws (tip, both wings, base), and
 * none of its three edges may come within 1.5 screen px of any segment of
 * another edge, flank-to-flank contacts on one card included.
 */
function arrowheadContacts(
  ids: string[], edges: { sourceId: string; targetId: string }[], scale: number, pxPerUnit: number,
): string[] {
  const options = { minDistance: 90, labelWidth: 150 };
  const pos = orgChartLayout(ids, edges, options);
  const r = Math.max(17 * scale, 6 / pxPerUnit);
  const nodes = new Map([...pos].map(([id, p]) => [id, { x: p.x * scale, y: p.y * scale, r }]));
  return headContacts(edges, nodes, options, scale, pxPerUnit, pxPerUnit);
}

/**
 * The org chart as the renderer draws it on /flows/map (agent-flow.tsx): the
 * spacing for the node count, the fit into the 1400-unit canvas, positions
 * rounded to the unit, the glyph and its 12 px screen floor, the port gap
 * from the arrowhead at the width step's px per unit; probed at `pxPerUnit`.
 */
function renderedContacts(
  ids: string[], edges: { sourceId: string; targetId: string }[], pxPerUnit: number, maxH = 720,
): string[] {
  const W = 1400, H = 600;
  const minDistance = spacingFor(ids.length);
  const options = { minDistance, labelWidth: Math.max(150, minDistance) };
  const pos = orgChartLayout(ids, edges, options);
  const xs = [...pos.values()].map((p) => p.x), ys = [...pos.values()].map((p) => p.y);
  const fit = fitToCanvas(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), W, H, maxH, 90, 70);
  const cx = (Math.max(...xs) + Math.min(...xs)) / 2, cy = (Math.max(...ys) + Math.min(...ys)) / 2;
  const stepPx = Math.max(80, Math.round((pxPerUnit * W) / 80) * 80) / W;
  const r = Math.max(glyphRadius(minDistance * fit.scale), 6 / stepPx);
  const nodes = new Map([...pos].map(([id, p]) => [id, {
    x: Math.round(W / 2 + (p.x - cx) * fit.scale), y: Math.round(fit.height / 2 + (p.y - cy) * fit.scale), r,
  }]));
  return headContacts(edges, nodes, options, fit.scale, stepPx, pxPerUnit);
}

function headContacts(
  edges: { sourceId: string; targetId: string }[],
  nodes: Map<string, { x: number; y: number; r: number }>,
  options: { minDistance: number; labelWidth: number },
  scale: number, routePxPerUnit: number, pxPerUnit: number,
): string[] {
  const r = [...nodes.values()][0]?.r ?? 10;
  const portGap = 2 * arrowHead(r, routePxPerUnit).halfWidth + 1.2;
  const routes = orgChartRoutes([...nodes.keys()], edges, nodes, orgChartMetrics(options), scale, 2, portGap);
  // The arrowhead as drawn at the probed px per unit, not the one the routes
  // were planned with (8.10 units drawn at 1024).
  const head = arrowHead(r, pxPerUnit);
  const threshold = 1.5 / pxPerUnit;
  const found: string[] = [];
  routes.forEach((route, i) => {
    const pts = route!.points;
    const tip = pts[pts.length - 1], prev = pts[pts.length - 2];
    const len = Math.hypot(tip.x - prev.x, tip.y - prev.y) || 1;
    const ux = (tip.x - prev.x) / len, uy = (tip.y - prev.y) / len;
    const base = { x: tip.x - ux * head.length, y: tip.y - uy * head.length };
    const w1 = { x: base.x - uy * head.halfWidth, y: base.y + ux * head.halfWidth };
    const w2 = { x: base.x + uy * head.halfWidth, y: base.y - ux * head.halfWidth };
    const sides: Seg[] = [{ a: w1, b: tip }, { a: tip, b: w2 }, { a: w2, b: w1 }];
    routes.forEach((other, j) => {
      if (i === j) return;
      let min = Infinity;
      for (const s of segments(other!.points)) for (const side of sides) min = Math.min(min, segmentDistance(side, s));
      if (min < threshold) {
        found.push(`${edges[i].sourceId}->${edges[i].targetId} ${(min * pxPerUnit).toFixed(2)} px from ${edges[j].sourceId}->${edges[j].targetId}`);
      }
    });
  });
  return found;
}

// The built-in agents and their handovers (backend/app/agents/builtin_agents.py):
// the map's All scope in the UX lab, 48 agents and 43 handovers.
const BUILTIN_IDS = [
  "ctem-scoping-analyst", "ctem-discovery-analyst", "ctem-prioritization-analyst", "ctem-validation-analyst",
  "ctem-mobilization-analyst", "xtm-one-assistant", "opencti-nlq", "opencti-dashboard-widget-creator",
  "opencti-dashboard-creator", "opencti-detection-rule-converter", "opencti-threat-hunt-creator", "opencti-assistant",
  "fimi-analyst", "fimi-stix-transformer", "openaev-payload-creator", "openaev-attack-path-orchestrator",
  "openaev-scenario-creator", "openaev-scenario-publisher", "openaev-assistant", "opencrq-assistant",
  "deep-investigation-agent", "ctem-assistant", "aev-phishing-email-generator", "aev-phishing-landing-page-generator",
  "aev-detection-rules-generator", "remediation-rule-type-detector", "remediation-crowdstrike-rule-generator",
  "remediation-splunk-rule-generator", "opencti-entity-insights", "cti-ttp-harvester-filigran-ai",
  "cti-stix-harvester-filigran-ai", "cti-stix-harvester", "cti-stix-transformer", "cti-stix-consumer",
  "exposure-briefing-analyst", "ocr-benchmark-transcriber", "browser-container-description",
  "browser-scenario-generation", "browser-full-scenario-generation", "browser-atomic-test-generation",
  "browser-email-generation", "browser-entity-discovery", "browser-relationship-resolution", "browser-scan-all",
  "template-designer", "deck-importer", "indicator-decay-guardian", "indicator-decay-judge",
];
const BUILTIN_EDGES = ([
  ["xtm-one-assistant", "opencti-assistant"], ["xtm-one-assistant", "openaev-assistant"],
  ["xtm-one-assistant", "opencrq-assistant"], ["xtm-one-assistant", "ctem-assistant"],
  ["opencti-dashboard-creator", "opencti-dashboard-widget-creator"], ["opencti-dashboard-creator", "opencti-assistant"],
  ["opencti-dashboard-creator", "ctem-assistant"], ["opencti-threat-hunt-creator", "opencti-detection-rule-converter"],
  ["opencti-threat-hunt-creator", "opencti-assistant"], ["opencti-threat-hunt-creator", "ctem-assistant"],
  ["opencti-assistant", "opencti-dashboard-creator"], ["opencti-assistant", "opencti-threat-hunt-creator"],
  ["opencti-assistant", "opencti-entity-insights"], ["opencti-assistant", "fimi-analyst"],
  ["opencti-assistant", "ctem-assistant"], ["opencti-assistant", "xtm-one-assistant"],
  ["fimi-analyst", "opencti-assistant"], ["fimi-analyst", "opencti-dashboard-creator"],
  ["fimi-analyst", "opencti-threat-hunt-creator"], ["fimi-analyst", "ctem-assistant"],
  ["openaev-attack-path-orchestrator", "openaev-payload-creator"], ["openaev-scenario-creator", "openaev-payload-creator"],
  ["openaev-scenario-creator", "openaev-scenario-publisher"], ["openaev-scenario-creator", "openaev-assistant"],
  ["openaev-scenario-creator", "ctem-assistant"], ["openaev-scenario-publisher", "openaev-assistant"],
  ["openaev-scenario-publisher", "ctem-assistant"], ["openaev-assistant", "openaev-scenario-creator"],
  ["openaev-assistant", "ctem-assistant"], ["openaev-assistant", "xtm-one-assistant"],
  ["opencrq-assistant", "ctem-assistant"], ["opencrq-assistant", "xtm-one-assistant"],
  ["ctem-assistant", "opencti-assistant"], ["ctem-assistant", "openaev-assistant"],
  ["ctem-assistant", "opencrq-assistant"], ["ctem-assistant", "xtm-one-assistant"],
  ["ctem-assistant", "deep-investigation-agent"], ["aev-detection-rules-generator", "remediation-rule-type-detector"],
  ["remediation-rule-type-detector", "remediation-crowdstrike-rule-generator"],
  ["remediation-rule-type-detector", "remediation-splunk-rule-generator"],
  ["opencti-entity-insights", "opencti-assistant"], ["opencti-entity-insights", "ctem-assistant"],
  ["indicator-decay-guardian", "indicator-decay-judge"],
] as const).map(([s, t]) => e(s, t));

test("orgchart: no arrowhead within 1.5 px of another edge, flank to flank included, built-in agents at 1024 and 1440", () => {
  assert.equal(BUILTIN_IDS.length, 48);
  assert.equal(BUILTIN_EDGES.length, 43);
  // 0.556 and 0.853 px per unit: the map's All scope fitted at 1024 and 1440.
  for (const pxPerUnit of [0.556, 0.853]) {
    assert.deepEqual(renderedContacts(BUILTIN_IDS, BUILTIN_EDGES, pxPerUnit), [], `built-in as rendered, ${pxPerUnit} px/unit`);
    for (const scale of [1, 0.6]) {
      assert.deepEqual(arrowheadContacts(BUILTIN_IDS, BUILTIN_EDGES, scale, pxPerUnit), [], `built-in, scale ${scale}, ${pxPerUnit} px/unit`);
    }
  }
  // A lab seeds the agents under random UUIDs, so ties break in another
  // order: the same graph under 40 seeded id sets.
  for (let seed = 1; seed <= 40; seed++) {
    let state = seed >>> 0;
    const hex = () => ((state = (state * 1664525 + 1013904223) >>> 0) >>> 0).toString(16).padStart(8, "0");
    const id = new Map(BUILTIN_IDS.map((slug) => [slug, `${hex()}-${slug}`]));
    const ids = BUILTIN_IDS.map((slug) => id.get(slug)!);
    const edges = BUILTIN_EDGES.map(({ sourceId, targetId }) => e(id.get(sourceId)!, id.get(targetId)!));
    for (const pxPerUnit of [0.556, 0.853]) {
      assert.deepEqual(renderedContacts(ids, edges, pxPerUnit), [], `built-in, id seed ${seed}, ${pxPerUnit} px/unit`);
    }
  }
});

// The ids the UX lab gives the 22 built-in agents that carry the 43 handovers:
// real UUIDs, so ties break in the lab's order,
// which `<hex>-<slug>` ids never reproduce. The standing gate of this graph.
const LAB_IDS: Record<string, string> = {
  "xtm-one-assistant": "1c5145f1-66aa-41b7-a90b-d396cfbfc101",
  "opencti-assistant": "bc99c27b-6e4e-46c7-8008-eaab6a51aa9a",
  "opencrq-assistant": "a54a9e66-371d-4918-8b26-825dc3295cd5",
  "openaev-assistant": "c5b1271e-1628-4ecb-b46c-3d757c9fd431",
  "ctem-assistant": "045850ba-08a5-4e6d-8c56-6300c3a733eb",
  "opencti-threat-hunt-creator": "003ad147-0c9d-4396-9d3b-276e662c6f7b",
  "opencti-dashboard-creator": "666b66c9-99db-4fb9-9f7a-70d718d24402",
  "fimi-analyst": "c77d8456-adb4-41b7-a8f8-80d941522ec4",
  "opencti-dashboard-widget-creator": "52f08f21-329d-4d76-ab30-d12827be467a",
  "opencti-detection-rule-converter": "56d40248-5f72-40d8-9cf2-af3b1f79cbcf",
  "opencti-entity-insights": "d72f3172-f08d-466d-8ac5-48dbf2c6968d",
  "openaev-scenario-creator": "a3156d2d-1487-4b4d-a2b4-fdbd48bd428c",
  "openaev-scenario-publisher": "4206696e-bf5a-4fc2-82e4-737c8cc25bbc",
  "openaev-payload-creator": "b03a65c0-5c29-499e-ad99-0e9eb3a5449f",
  "openaev-attack-path-orchestrator": "6b9cf727-7289-4ac4-98e0-cb8e09a7a428",
  "deep-investigation-agent": "aa34150d-0d18-4bff-88e4-ee890c78d5c3",
  "aev-detection-rules-generator": "26ae0700-d9c9-42f4-bd40-23178f337813",
  "remediation-rule-type-detector": "cfc2a7e7-5f3e-4b8c-a99b-25e350cafab0",
  "remediation-crowdstrike-rule-generator": "903e52db-2c86-41b7-9ccb-77395ea56893",
  "remediation-splunk-rule-generator": "fab2256d-d572-456d-91ec-b8f5d349a2e0",
  "indicator-decay-guardian": "19944b87-2a50-4521-9a8b-3bd6b0ae35b0",
  "indicator-decay-judge": "a943fa9a-7a28-4ed8-be63-6889d9005ab9",
};

test("orgchart: the built-in graph under the UX lab's real ids has no arrowhead within 1.5 px of another edge at 1024 and 1440", () => {
  assert.equal(Object.keys(LAB_IDS).length, 22);
  const id = (slug: string) => LAB_IDS[slug] ?? slug;
  const ids = BUILTIN_IDS.map(id);
  const edges = BUILTIN_EDGES.map(({ sourceId, targetId }) => e(id(sourceId), id(targetId)));
  // The canvas is 600 units tall at a 768 px high window, 720 on a taller one.
  for (const maxH of [600, 720]) {
    for (const pxPerUnit of [0.556, 0.853]) {
      assert.deepEqual(renderedContacts(ids, edges, pxPerUnit, maxH), [], `lab ids as rendered, ${pxPerUnit} px/unit, canvas ${maxH}`);
    }
  }
});

test("spreadAngles keeps angles already apart, spreads a crowded run around its mean, across the cut too", () => {
  const apart = [0, 1, 2, 4];
  assert.deepEqual(spreadAngles(apart, 0.5), apart);
  const [a, b, c] = spreadAngles([1, 1.05, 3], 0.4);
  assert.ok(Math.abs(b - a - 0.4) < 1e-9 && Math.abs((a + b) / 2 - 1.025) < 1e-9, `${a} ${b}`);
  assert.equal(c, 3);
  // Two ends either side of angle 0 (pi and -pi meet): spread across it.
  const [x, y] = spreadAngles([-0.05, 0.05], 0.4).map((v) => ((v % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI));
  const d = Math.min(Math.abs(x - y), 2 * Math.PI - Math.abs(x - y));
  assert.ok(Math.abs(d - 0.4) < 1e-9, `${x} ${y}`);
  // More ends than the circle holds share it evenly.
  const many = spreadAngles([0, 0, 0, 0], 3);
  const sorted = [...many].sort((p, q) => p - q);
  for (let k = 1; k < 4; k++) assert.ok(Math.abs(sorted[k] - sorted[k - 1] - Math.PI / 2) < 1e-9);
});

/**
 * Arrowheads drawn within 1.5 screen px of another edge in a layout drawn
 * with arcs (force, top down, left to right, radial), the built-in graph
 * fitted as /flows/map does at `pxPerUnit`: the UX probe (tip, both
 * wings, base against every other arc) applied to the arc layouts (A6).
 */
function arcHeadContacts(layout: (typeof GRAPH_LAYOUTS)[number], pxPerUnit: number, spread: boolean): string[] {
  const W = 1400, H = 600, PAD_X = 90, PAD_Y = 70;
  const stepPx = Math.max(80, Math.round((pxPerUnit * W) / 80) * 80) / W;
  const minDistance = spacingFor(BUILTIN_IDS.length);
  const pos = layoutGraph(layout, BUILTIN_IDS, BUILTIN_EDGES, {
    minDistance, labelWidth: Math.max(150, minDistance), aspect: W / H, centerId: "ctem-assistant",
  });
  const xs = [...pos.values()].map((p) => p.x), ys = [...pos.values()].map((p) => p.y);
  const padRight = PAD_X + 44 / stepPx;
  const fit = fitToCanvas(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), W, H, 720, PAD_X, PAD_Y, undefined, padRight);
  const midX = (PAD_X + W - padRight) / 2;
  const cx = (Math.max(...xs) + Math.min(...xs)) / 2, cy = (Math.max(...ys) + Math.min(...ys)) / 2;
  const r = Math.max(glyphRadius(minDistance * fit.scale), 6 / stepPx);
  const nodes = new Map([...pos].map(([id, p]) => [id, {
    x: Math.round(midX + (p.x - cx) * fit.scale), y: Math.round(fit.height / 2 + (p.y - cy) * fit.scale), r,
  }]));
  const head = arrowHead(r, pxPerUnit);
  const planned = arrowHead(r, stepPx);
  const routes = arcRoutes(BUILTIN_EDGES, nodes, {
    curvatures: edgeCurvatures(BUILTIN_EDGES),
    clearance: spread ? r + ARROW_GAP + planned.length : layout === "force" ? undefined : r + 4,
    box: { x0: PAD_X / 2, y0: PAD_Y / 2, x1: W - PAD_X / 2, y1: fit.height - PAD_Y / 2 },
    arrowGap: ARROW_GAP,
    head: spread ? { length: planned.length, room: 2 * planned.halfWidth + 3 / stepPx } : undefined,
  });
  const lines = routes.map((route) => Array.from({ length: 25 }, (_, k) => quadraticPoint(route!.a, route!.control, route!.b, k / 24)));
  const threshold = 1.5 / pxPerUnit;
  const found: string[] = [];
  routes.forEach((route, i) => {
    const tip = route!.b;
    const len = Math.hypot(tip.x - route!.control.x, tip.y - route!.control.y) || 1;
    const ux = (tip.x - route!.control.x) / len, uy = (tip.y - route!.control.y) / len;
    const base = { x: tip.x - ux * head.length, y: tip.y - uy * head.length };
    const w1 = { x: base.x - uy * head.halfWidth, y: base.y + ux * head.halfWidth };
    const w2 = { x: base.x + uy * head.halfWidth, y: base.y - ux * head.halfWidth };
    const sides: Seg[] = [{ a: w1, b: tip }, { a: tip, b: w2 }, { a: w2, b: w1 }];
    lines.forEach((other, j) => {
      if (i === j) return;
      let min = Infinity;
      for (const s of segments(other)) for (const side of sides) min = Math.min(min, segmentDistance(side, s));
      if (min < threshold) found.push(`${BUILTIN_EDGES[i].sourceId}->${BUILTIN_EDGES[i].targetId} ${(min * pxPerUnit).toFixed(2)} px from ${BUILTIN_EDGES[j].sourceId}->${BUILTIN_EDGES[j].targetId}`);
    });
  });
  return found;
}

test("arc layouts: spreading the ends on each ring takes most arrowheads off other edges, built-in graph at 1024 and 1440", () => {
  // Measured on the built-in graph (48 agents, 43 handovers): the hits left
  // are arcs converging on a hub from close directions further out than the
  // ring; the bound is a ratchet, lowered as the routing improves.
  const bound: Record<string, [number, number]> = {
    force: [10, 1], "hierarchy-tb": [26, 16], "hierarchy-lr": [22, 14], radial: [8, 2],
  };
  for (const layout of GRAPH_LAYOUTS) {
    if (layout === "orgchart") continue;
    [0.556, 0.853].forEach((pxPerUnit, k) => {
      const before = arcHeadContacts(layout, pxPerUnit, false).length;
      const after = arcHeadContacts(layout, pxPerUnit, true);
      assert.ok(after.length <= bound[layout][k], `${layout} at ${pxPerUnit} px/unit: ${after.length} hits\n${after.join("\n")}`);
      assert.ok(after.length < before, `${layout} at ${pxPerUnit} px/unit: ${after.length} hits, ${before} before`);
    });
  }
});

test("arcRoutes without head keeps every end where its arc meets the ring ", () => {
  const nodes = new Map([["a", { x: 0, y: 0, r: 10 }], ["b", { x: 100, y: 0, r: 10 }]]);
  const [route] = arcRoutes([e("a", "b")], nodes, { curvatures: [0], arrowGap: 2 });
  assert.deepEqual(route!.a, { x: 10, y: 0 });
  assert.ok(Math.abs(route!.b.x - 88) < 1e-9 && Math.abs(route!.b.y) < 1e-9);
});

/**
 * Pairs of parallel segments of two distinct edges under a folded parent's
 * bus (its block's trunks) running side by side closer than 6 screen px or
 * half a port gap, whichever is wider, over more than a port gap: the UX lab
 * read 4 px as a doubled line. Two segments on one line read
 * as one riser and pass. Drawn as the renderer does (`renderedContacts`) at
 * `pxPerUnit`; reported in screen px.
 */
function closeParallels(
  ids: string[], edges: { sourceId: string; targetId: string }[], pxPerUnit: number, maxH = 720,
): string[] {
  const W = 1400;
  const minDistance = spacingFor(ids.length);
  const options = { minDistance, labelWidth: Math.max(150, minDistance) };
  const pos = orgChartLayout(ids, edges, options);
  const xs = [...pos.values()].map((p) => p.x), ys = [...pos.values()].map((p) => p.y);
  const fit = fitToCanvas(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), W, 600, maxH, 90, 70);
  const cx = (Math.max(...xs) + Math.min(...xs)) / 2, cy = (Math.max(...ys) + Math.min(...ys)) / 2;
  const stepPx = Math.max(80, Math.round((pxPerUnit * W) / 80) * 80) / W;
  const r = Math.max(glyphRadius(minDistance * fit.scale), 6 / stepPx);
  const nodes = new Map([...pos].map(([id, p]) => [id, {
    x: Math.round(W / 2 + (p.x - cx) * fit.scale), y: Math.round(fit.height / 2 + (p.y - cy) * fit.scale), r,
  }]));
  const portGap = 2 * arrowHead(r, stepPx).halfWidth + 1.2;
  const routes = orgChartRoutes(ids, edges, nodes, orgChartMetrics(options), fit.scale, 2, portGap);
  const tree = orgChartTree(ids, edges);
  const buses = [...tree.folded].map((id) => nodes.get(id)!.y + orgChartMetrics(options).rowPitch * fit.scale);
  const near = Math.max(portGap / 2, 6 / pxPerUnit);
  const found: string[] = [];
  const segs = routes.map((route) => segments(route!.points));
  segs.forEach((own, i) => segs.forEach((other, j) => {
    if (j <= i) return;
    for (const p of own) for (const q of other) {
      const vertical = p.a.x === p.b.x && q.a.x === q.b.x;
      if (!vertical) continue;
      const [p0, p1] = [p.a.y, p.b.y].sort((a, b) => a - b);
      const [q0, q1] = [q.a.y, q.b.y].sort((a, b) => a - b);
      const top = Math.max(p0, q0, Math.min(...buses) - 1e-6);
      const gap = Math.abs(p.a.x - q.a.x);
      if (gap > 1e-6 && gap < near && Math.min(p1, q1) - top > portGap) {
        found.push(`${edges[i].sourceId}->${edges[i].targetId} ${(gap * pxPerUnit).toFixed(2)} px beside ${edges[j].sourceId}->${edges[j].targetId}`);
      }
    }
  }));
  return found;
}

test("orgchart: a folded block's trunks never run two risers closer than half a port gap at 1440 and 1024", () => {
  // The UX lab's case: a parent with 10 handover targets folding into 3
  // columns, one leaf handing back. Ids sort as the lab's UUIDs placed the
  // leaves (columns 5 9 4 7 | 1 2 6 3 | 8 10), the hand-back from the top
  // leaf of the middle column, whose trunk drew the branch spine and the
  // hand-back riser 4 px apart at 1440.
  const order = ["5", "9", "4", "7", "1", "2", "6", "3", "8", "10"];
  const leaves = order.map((n, i) => `${String.fromCharCode(97 + i)}-fold-leaf-analyst-${n}`);
  for (const back of [leaves[4], leaves[0], leaves[9]]) {
    const ids = ["0-fold-test-coordinator", ...leaves];
    const edges = [...leaves.map((id) => e(ids[0], id)), e(back, ids[0])];
    assert.ok(orgChartTree(ids, edges).folded.has(ids[0]));
    for (const maxH of [600, 720]) {
      for (const pxPerUnit of [0.853, 0.556]) {
        assert.deepEqual(closeParallels(ids, edges, pxPerUnit, maxH), [], `hand-back from ${back}, ${pxPerUnit} px/unit, canvas ${maxH}`);
        assert.deepEqual(renderedContacts(ids, edges, pxPerUnit, maxH), [], `arrowheads, hand-back from ${back}, ${pxPerUnit} px/unit`);
      }
    }
  }
});

/**
 * Pairs of parallel segments (vertical or horizontal) of two distinct edges
 * anywhere on the org chart running closer than half a port gap over more
 * than a port gap: two lines 1 to 3.5 px apart read as one doubled line. Two segments on one line read as one bus and pass.
 * Drawn as the renderer does (`renderedContacts`), reported in screen px.
 */
function parallelsAnywhere(
  ids: string[], edges: { sourceId: string; targetId: string }[], pxPerUnit: number, maxH = 720,
): string[] {
  const W = 1400;
  const minDistance = spacingFor(ids.length);
  const options = { minDistance, labelWidth: Math.max(150, minDistance) };
  const pos = orgChartLayout(ids, edges, options);
  const xs = [...pos.values()].map((p) => p.x), ys = [...pos.values()].map((p) => p.y);
  const fit = fitToCanvas(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), W, 600, maxH, 90, 70);
  const cx = (Math.max(...xs) + Math.min(...xs)) / 2, cy = (Math.max(...ys) + Math.min(...ys)) / 2;
  const stepPx = Math.max(80, Math.round((pxPerUnit * W) / 80) * 80) / W;
  const r = Math.max(glyphRadius(minDistance * fit.scale), 6 / stepPx);
  const nodes = new Map([...pos].map(([id, p]) => [id, {
    x: Math.round(W / 2 + (p.x - cx) * fit.scale), y: Math.round(fit.height / 2 + (p.y - cy) * fit.scale), r,
  }]));
  const portGap = 2 * arrowHead(r, stepPx).halfWidth + 1.2;
  const routes = orgChartRoutes(ids, edges, nodes, orgChartMetrics(options), fit.scale, 2, portGap);
  const found: string[] = [];
  const segs = routes.map((route) => segments(route!.points));
  segs.forEach((own, i) => segs.forEach((other, j) => {
    if (j <= i) return;
    for (const p of own) for (const q of other) {
      for (const [across, along] of [["x", "y"], ["y", "x"]] as const) {
        if (p.a[across] !== p.b[across] || q.a[across] !== q.b[across]) continue;
        const [p0, p1] = [p.a[along], p.b[along]].sort((a, b) => a - b);
        const [q0, q1] = [q.a[along], q.b[along]].sort((a, b) => a - b);
        const gap = Math.abs(p.a[across] - q.a[across]);
        // 0.02 units: the routes are rounded to the hundredth.
        if (gap > 1e-6 && gap < portGap / 2 - 0.02 && Math.min(p1, q1) - Math.max(p0, q0) > portGap) {
          found.push(`${edges[i].sourceId}->${edges[i].targetId} ${(gap * pxPerUnit).toFixed(2)} px beside ${edges[j].sourceId}->${edges[j].targetId}`);
        }
      }
    }
  }));
  return found;
}

test("orgchart: the unfolded built-in graph runs no two edges closer than half a port gap at 1440", () => {
  const id = (slug: string) => LAB_IDS[slug] ?? slug;
  const ids = BUILTIN_IDS.map(id);
  const edges = BUILTIN_EDGES.map(({ sourceId, targetId }) => e(id(sourceId), id(targetId)));
  for (const maxH of [600, 720]) {
    assert.deepEqual(parallelsAnywhere(ids, edges, 0.853, maxH), [], `lab ids, 1440, canvas ${maxH}`);
  }
  // A lab seeds the agents under random UUIDs (a lab drew
  // 629f>4cce 3.45 px beside 629f>82bf): the same graph under seeded id sets.
  for (let seed = 1; seed <= 20; seed++) {
    let state = seed >>> 0;
    const hex = () => ((state = (state * 1664525 + 1013904223) >>> 0) >>> 0).toString(16).padStart(8, "0");
    const seeded = new Map(BUILTIN_IDS.map((slug) => [slug, `${hex()}-${slug}`]));
    const sIds = BUILTIN_IDS.map((slug) => seeded.get(slug)!);
    const sEdges = BUILTIN_EDGES.map(({ sourceId, targetId }) => e(seeded.get(sourceId)!, seeded.get(targetId)!));
    assert.deepEqual(parallelsAnywhere(sIds, sEdges, 0.853, 720), [], `id seed ${seed}, 1440`);
  }
});

test("orgchart: two neighbours on a row handing over to one hub keep their risers off each other's arrowheads", () => {
  for (let seed = 1; seed <= 120; seed++) {
    for (const n of [30, 48]) {
      const { ids, edges } = hubGraph(seed, n, 43);
      assert.deepEqual(arrowheadContacts(ids, edges, 0.6, 0.556), [], `seed ${seed}, ${n} agents`);
    }
  }
});

test("orgchart: with a port gap, edges stay orthogonal and off every card but their own ends", () => {
  const busy = [...CEO_EDGES, e("mail", "ea"), e("clerk", "ea"), e("analyst", "ea"), e("comms", "ea"), e("mail", "clerk"), e("clerk", "mail"), e("analyst", "ux")];
  const options = { minDistance: 90, labelWidth: 150 };
  const pos = orgChartLayout(CEO_IDS, busy, options);
  const scale = 0.35, r = 11;
  const nodes = new Map([...pos].map(([id, p]) => [id, { x: p.x * scale, y: p.y * scale, r }]));
  const routes = orgChartRoutes(CEO_IDS, busy, nodes, orgChartMetrics(options), scale, 2, 6);
  busy.forEach((edge, k) => {
    const pts = routes[k]!.points;
    for (const s of segments(pts)) assert.ok(s.a.x === s.b.x || s.a.y === s.b.y, `${edge.sourceId}->${edge.targetId}: 90 degree elbows`);
    for (const [id, c] of nodes) {
      for (const s of segments(pts).slice(1, -1)) assert.ok(!segmentHitsDisc(s, c, r), `${edge.sourceId}->${edge.targetId} crosses ${id}`);
    }
    const tgt = nodes.get(edge.targetId)!;
    const end = pts[pts.length - 1];
    assert.ok(Math.hypot(end.x - tgt.x, end.y - tgt.y) > r, "ends outside the target");
  });
});

test("orgchart: a back edge across levels is a return lane outside the component, off every card", () => {
  const ids = ["a", "b", "c", "d"];
  const edges = [e("a", "b"), e("b", "c"), e("c", "d"), e("d", "a")];
  const { nodes, routes, r } = drawOrgChart(ids, edges);
  const back = routes[3]!;
  const maxX = Math.max(...[...nodes.values()].map((n) => n.x));
  assert.ok(back.points.some((p) => p.x > maxX), "runs right of every card");
  for (const [id, c] of nodes) {
    for (const s of segments(back.points).slice(1, -1)) assert.ok(!segmentHitsDisc(s, c, r), `crosses ${id}`);
  }
});

test("orgchart: deterministic and no two cards closer than the label room at 150 nodes", () => {
  const { ids, edges } = bigGraph(150);
  const options = { minDistance: 90, labelWidth: 150, aspect: 1400 / 600 };
  const a = orgChartLayout(ids, edges, options);
  const b = orgChartLayout([...ids].reverse(), [...edges].reverse(), options);
  assert.deepEqual(Object.fromEntries(a), Object.fromEntries(b));
  assert.equal(a.size, ids.length);
  assert.ok(minimumDistance(a.values()) >= 90 - 1e-6);
  // Two cards on one row are a label apart, so names never overlap.
  const pts = [...a.values()];
  for (const p of pts) for (const q of pts) {
    if (p !== q && Math.abs(p.y - q.y) < 1e-6) assert.ok(Math.abs(p.x - q.x) >= 150 - 1e-6);
  }
  const nodes = new Map([...a].map(([id, p]) => [id, { ...p, r: 10 }]));
  const routes = orgChartRoutes(ids, edges, nodes, orgChartMetrics(options), 1, 3);
  routes.forEach((route, k) => {
    assert.ok(route, `edge ${k} routed`);
    for (const s of segments(route!.points)) assert.ok(s.a.x === s.b.x || s.a.y === s.b.y);
  });
});

test("separate settles points stacked on one spot", () => {
  const stacked = new Map(["a", "b", "c", "d"].map((id) => [id, { x: 0, y: 0 }]));
  assert.ok(minimumDistance(separate(stacked, 50).values()) >= 50 - 1e-6);
});

test("layeredLayout centres the hierarchy and the isolated strip on the same axis", () => {
  // A narrow chain and many agents with no edge: the chain must not sit at one side.
  const ids = ["a", "b", "c", ...Array.from({ length: 26 }, (_, i) => `z${String(i).padStart(2, "0")}`)];
  const pos = layeredLayout(ids, [e("a", "b"), e("b", "c")], true);
  for (const id of ["a", "b", "c"]) assert.equal(pos.get(id)!.x, 0);
  const strip = ids.slice(3).map((id) => pos.get(id)!);
  const xs = strip.map((p) => p.x);
  assert.ok(Math.abs(Math.min(...xs) + Math.max(...xs)) < 1e-6, "strip centred on the hierarchy axis");
  // Rows capped at max(4, 1.5 x widest layer) = 4 agents.
  const firstRowY = Math.min(...strip.map((p) => p.y));
  assert.equal(strip.filter((p) => p.y === firstRowY).length, 4);
  assert.ok(minimumDistance(pos.values()) >= 90);
});

test("spacingFor grows with the node count and stays bounded", () => {
  assert.equal(spacingFor(7), 115);
  assert.equal(spacingFor(20), 115);
  assert.ok(spacingFor(150) > spacingFor(40), `${spacingFor(150)} > ${spacingFor(40)}`);
  // A quarter more room than the first cut (90 to 140) at every size.
  for (const n of [7, 45, 150]) {
    const before = n <= 20 ? 90 : Math.min(140, Math.round(90 + 20 * Math.log2(n / 20)));
    const ratio = spacingFor(n) / before;
    assert.ok(ratio >= 1.2 && ratio <= 1.3, `${n} nodes: x${ratio.toFixed(2)}`);
  }
  assert.equal(spacingFor(5000), 175);
});

test("automaticForces: edges one node distance long, half the repulsion, within the slider bounds", () => {
  for (const n of [1, 7, 45, 93, 150, 5000]) {
    const f = automaticForces(n);
    assert.equal(f.charge, 0.5);
    assert.equal(f.collide % FORCE_LIMITS.collide.step, 0);
    assert.equal(f.linkDistance, f.collide);
    assert.ok(f.collide >= FORCE_LIMITS.collide.min && f.collide <= FORCE_LIMITS.collide.max);
    assert.ok(f.linkDistance >= FORCE_LIMITS.linkDistance.min && f.linkDistance <= FORCE_LIMITS.linkDistance.max);
  }
  assert.equal(automaticForces(93).collide, 170);
});

test("the automatic forces keep 93 agents 21 px apart on a 1440 screen, canvas not taller", () => {
  // Ten hubs of five, a chain of six, 35 agents with no handover: the shape of
  // the lab map Sam's review was measured on.
  const ids = Array.from({ length: 93 }, (_, i) => `n${String(i).padStart(3, "0")}`);
  const edges: { sourceId: string; targetId: string }[] = [];
  for (let h = 0; h < 10; h++) for (let k = 1; k <= 5; k++) edges.push(e(ids[h * 6], ids[h * 6 + k]));
  for (let i = 60; i < 63; i++) edges.push(e(ids[i], ids[i + 1]));
  const f = automaticForces(ids.length);
  const pos = layoutGraph("force", ids, edges, { minDistance: f.collide, labelWidth: Math.max(150, f.collide), aspect: 1400 / 600, linkDistance: f.linkDistance, charge: f.charge });
  const xs = [...pos.values()].map((p) => p.x), ys = [...pos.values()].map((p) => p.y);
  const fit = fitToCanvas(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), 1400, 600, 600, 90, 70);
  assert.equal(fit.height, 600, "the canvas does not grow");
  // On a 1440 px screen the map's svg is 1194 px wide.
  const screen = fit.scale * (1194 / 1400);
  const nearest = minimumDistance(pos.values()) * screen;
  assert.ok(nearest >= 21, `nearest ${nearest.toFixed(1)} px`);
});

test("lyingFlat lays a standing drawing across, keeps distances, leaves a wide one alone", () => {
  const standing = new Map(Array.from({ length: 6 }, (_, i) => [`s${i}`, { x: (i % 2) * 40, y: i * 100 }]));
  const flat = lyingFlat(standing);
  const ext = (p: Map<string, { x: number; y: number }>) => {
    const xs = [...p.values()].map((q) => q.x), ys = [...p.values()].map((q) => q.y);
    return { w: Math.max(...xs) - Math.min(...xs), h: Math.max(...ys) - Math.min(...ys) };
  };
  assert.ok(ext(flat).w > ext(flat).h, `${ext(flat).w} x ${ext(flat).h}`);
  assert.ok(Math.abs(minimumDistance(flat.values()) - minimumDistance(standing.values())) < 1e-6, "a rotation keeps every distance");
  const wide = new Map([["a", { x: 0, y: 0 }], ["b", { x: 300, y: 0 }], ["c", { x: 150, y: 40 }]]);
  assert.equal(lyingFlat(wide), wide);
});

test("the lab's 93-agent map, a chain of hub clusters, keeps median 32 px on a 1440 screen", () => {
  // Five hubs of five chained hub to hub, plus a second component: the chain
  // used to stand top to bottom and bind the fit by its height.
  const ids = Array.from({ length: 93 }, (_, i) => `n${String(i).padStart(3, "0")}`);
  const edges: { sourceId: string; targetId: string }[] = [];
  for (let h = 0; h < 5; h++) {
    for (let k = 1; k <= 5; k++) edges.push(e(ids[h * 6], ids[h * 6 + k]));
    if (h < 4) edges.push(e(ids[h * 6], ids[(h + 1) * 6]));
  }
  for (let i = 30; i < 45; i++) edges.push(e(ids[30], ids[i + 1]));
  const f = automaticForces(ids.length);
  const pos = layoutGraph("force", ids, edges, { minDistance: f.collide, labelWidth: Math.max(150, f.collide), aspect: 1400 / 600, linkDistance: f.linkDistance, charge: f.charge });
  const xs = [...pos.values()].map((p) => p.x), ys = [...pos.values()].map((p) => p.y);
  const fit = fitToCanvas(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), 1400, 600, 600, 90, 70);
  assert.equal(fit.height, 600, "the canvas does not grow");
  const screen = fit.scale * (1194 / 1400);
  const pts = [...pos.values()];
  const nearest = pts.map((a) => Math.min(...pts.filter((b) => b !== a).map((b) => Math.hypot(a.x - b.x, a.y - b.y)))).sort((a, b) => a - b);
  const median = nearest[Math.floor(nearest.length / 2)] * screen;
  assert.ok(nearest[0] * screen >= 21, `nearest ${(nearest[0] * screen).toFixed(1)} px`);
  assert.ok(median >= 32, `median ${median.toFixed(1)} px`);
});

test("packForCanvas picks the shelf width that fits the canvas largest, lone grid included", () => {
  const block = (prefix: string) => new Map([[`${prefix}a`, { x: 0, y: 0 }], [`${prefix}b`, { x: 100, y: 0 }], [`${prefix}c`, { x: 50, y: 100 }]]);
  const blocks = Array.from({ length: 8 }, (_, i) => block(`b${i}`));
  const lone = Array.from({ length: 30 }, (_, i) => `l${i}`);
  const extentOf = (p: Map<string, { x: number; y: number }>) => {
    const xs = [...p.values()].map((q) => q.x), ys = [...p.values()].map((q) => q.y);
    return { w: Math.max(...xs) - Math.min(...xs), h: Math.max(...ys) - Math.min(...ys) };
  };
  const fitScale = (p: Map<string, { x: number; y: number }>) => { const { w, h } = extentOf(p); return Math.min(1400 / (w + 100), 600 / (h + 100)); };
  const packed = packForCanvas(blocks, lone, 50, 100, 150, 1400 / 600);
  assert.equal(packed.size, 8 * 3 + 30);
  const plain = new Map<string, { x: number; y: number }>();
  packShelves(blocks, plain, 50, 100, 1400 / 600);
  const baseline = isolatedCluster(lone, plain, 150, 100, 1400 / 600);
  assert.ok(fitScale(packed) >= fitScale(baseline) - 1e-9, `${fitScale(packed)} >= ${fitScale(baseline)}`);
  assert.ok(minimumDistance(packed.values()) >= 50 - 1e-9, "blocks stay a column gap apart");
});

test("labelBoxAcross is the label's width, between one and 1.6 node distances", () => {
  assert.equal(labelBoxAcross(100, 50), 100);
  assert.equal(labelBoxAcross(100, 150), 150);
  assert.equal(labelBoxAcross(100, 400), 160);
});

test("separateLabelled keeps side-by-side nodes a label apart, stacked ones a node apart", () => {
  const pts = new Map([
    ["a", { x: 0, y: 0 }],
    ["b", { x: 20, y: 0 }],
    ["c", { x: 0, y: 20 }],
  ]);
  const out = separateLabelled(pts, 100, 150);
  const a = out.get("a")!, b = out.get("b")!, c = out.get("c")!;
  // On an ellipse 150 across and 100 high, every pair is clear.
  for (const [p, q] of [[a, b], [a, c], [b, c]]) {
    const d = Math.hypot((p.x - q.x) / 150, (p.y - q.y) / 100);
    assert.ok(d >= 1 - 1e-6, `normalised distance ${d}`);
  }
  assert.ok(Math.abs(a.x - b.x) >= 150 - 1e-6 || Math.abs(a.y - b.y) >= 100 - 1e-6);
});

test("isolatedCluster alone is a grid with the canvas proportions, centred", () => {
  const ids = Array.from({ length: 60 }, (_, i) => `z${String(i).padStart(2, "0")}`);
  const pos = isolatedCluster(ids, new Map(), 150, 90, 1400 / 600);
  const xs = [...pos.values()].map((p) => p.x);
  const ys = [...pos.values()].map((p) => p.y);
  const w = Math.max(...xs) - Math.min(...xs);
  const h = Math.max(...ys) - Math.min(...ys);
  assert.ok(w > h, `wider than tall (${w} x ${h})`);
  assert.ok(Math.abs(Math.min(...xs) + Math.max(...xs)) < 1e-6, "centred across");
  assert.ok(minimumDistance(pos.values()) >= 90);
});

for (const layout of GRAPH_LAYOUTS) {
  test(`${layout}: the production shape (153 agents, 86 handovers) keeps every node apart with a canvas aspect`, () => {
    const ids = Array.from({ length: 153 }, (_, i) => `agent-${String(i).padStart(3, "0")}`);
    const edges = [];
    // 40 connected agents in four handover trees, 113 with no handover.
    for (let i = 1; i <= 86; i++) edges.push(e(ids[(i * 3) % 40], ids[(i * 7 + 1) % 40]));
    const minDistance = spacingFor(ids.length);
    const positions = layoutGraph(layout, ids, edges, { minDistance, labelWidth: 150, aspect: 1400 / 600, centerId: ids[0] });
    assert.equal(positions.size, ids.length);
    assert.ok(minimumDistance(positions.values()) >= minDistance - 1e-6, `min distance ${minimumDistance(positions.values())}`);
    // The cluster is shaped for the canvas: the drawing is no more than three
    // times as wide as the canvas proportions ask (the old strip was 12 wide
    // and set the scale on its own).
    const xs = [...positions.values()].map((p) => p.x);
    const ys = [...positions.values()].map((p) => p.y);
    const w = Math.max(...xs) - Math.min(...xs);
    const h = Math.max(...ys) - Math.min(...ys);
    assert.ok(w / Math.max(1, h) < (1400 / 600) * 3, `aspect ${w / h}`);
  });
}

test("connectedComponents: largest first, lone nodes left out", () => {
  const comps = connectedComponents(["a", "b", "c", "d", "e", "f"], [e("a", "b"), e("d", "e"), e("e", "f")]);
  assert.deepEqual(comps, [["d", "e", "f"], ["a", "b"]]);
});

test("packShelves keeps blocks apart and wraps them into the canvas proportions", () => {
  const block = (prefix: string) => new Map([[`${prefix}1`, { x: 0, y: 0 }], [`${prefix}2`, { x: 100, y: 100 }]]);
  const blocks = Array.from({ length: 12 }, (_, i) => block(`b${i}`));
  const out = new Map<string, { x: number; y: number }>();
  const { right, bottom } = packShelves(blocks, out, 50, 50, 1400 / 600);
  assert.equal(out.size, 24);
  assert.ok(right / bottom > 1 && right / bottom < (1400 / 600) * 2, `aspect ${right / bottom}`);
  assert.ok(minimumDistance(out.values()) >= 50);
});

test("packShelves keeps a block with a margin as far from a plain block as from another margined one", () => {
  const block = (prefix: string) => new Map([[`${prefix}1`, { x: 0, y: 0 }], [`${prefix}2`, { x: 100, y: 100 }]]);
  const blocks = Array.from({ length: 9 }, (_, i) => block(`b${i}`));
  // Every other block is a fleet grid with a margin of 40.
  const margins = new Map(blocks.filter((_, i) => i % 2 === 1).map((b) => [b, 40]));
  const out = new Map<string, { x: number; y: number }>();
  packShelves(blocks, out, 50, 50, 1400 / 600, undefined, margins);
  const box = (b: Map<string, unknown>) => {
    const pts = [...b.keys()].map((id) => out.get(id)!);
    return { x0: Math.min(...pts.map((p) => p.x)), x1: Math.max(...pts.map((p) => p.x)), y0: Math.min(...pts.map((p) => p.y)), y1: Math.max(...pts.map((p) => p.y)) };
  };
  const gap = (a: ReturnType<typeof box>, b: ReturnType<typeof box>) =>
    Math.max(b.x0 - a.x1, a.x0 - b.x1, b.y0 - a.y1, a.y0 - b.y1);
  for (const [g] of margins) {
    for (const other of blocks) {
      if (other === g) continue;
      // The plain gap plus twice the larger margin, whatever the neighbour.
      assert.ok(gap(box(g), box(other)) >= 50 + 2 * 40 - 1e-9, `gap ${gap(box(g), box(other))}`);
    }
  }
});

test("glyphRadius is a fifth of the room, between 6 and 12", () => {
  assert.equal(glyphRadius(10), 6);
  assert.equal(glyphRadius(50), 10);
  assert.equal(glyphRadius(500), 12);
});

for (const layout of GRAPH_LAYOUTS) {
  test(`${layout}: with a canvas aspect, lone agents sit in wide cells and the drawing fits the canvas shape`, () => {
    // The lab shape: a hub of 20, six 7-agent trees, 100 lone agents.
    const ids: string[] = [];
    const edges = [];
    for (let i = 0; i < 20; i++) ids.push(`hub-${String(i).padStart(2, "0")}`);
    for (let i = 1; i < 20; i++) edges.push(e(ids[0], ids[i]));
    for (let c = 0; c < 6; c++) {
      const tree = Array.from({ length: 7 }, (_, i) => `t${c}-${i}`);
      ids.push(...tree);
      for (let i = 1; i < 7; i++) edges.push(e(tree[Math.floor((i - 1) / 2)], tree[i]));
    }
    const lone = Array.from({ length: 100 }, (_, i) => `z-${String(i).padStart(3, "0")}`);
    ids.push(...lone);
    const minDistance = spacingFor(ids.length);
    const aspect = 1400 / 600;
    const pos = layoutGraph(layout, ids, edges, { minDistance, labelWidth: 150, aspect, centerId: ids[0] });
    assert.equal(pos.size, ids.length);
    assert.ok(minimumDistance(pos.values()) >= minDistance - 1e-6);
    // Two lone agents on one row are a wide cell apart, so a name fits.
    const cell = loneCellWidth(minDistance, 150);
    const lonePts = lone.map((id) => pos.get(id)!);
    for (const p of lonePts) {
      for (const q of lonePts) {
        if (p !== q && Math.abs(p.y - q.y) < 1e-6) assert.ok(Math.abs(p.x - q.x) >= cell - 1e-6);
      }
    }
    // Components packed in shelves: the drawing is within twice the canvas shape either way.
    const xs = [...pos.values()].map((p) => p.x);
    const ys = [...pos.values()].map((p) => p.y);
    const ratio = (Math.max(...xs) - Math.min(...xs)) / Math.max(1, Math.max(...ys) - Math.min(...ys));
    assert.ok(ratio > aspect / 2.5 && ratio < aspect * 2.5, `aspect ${ratio}`);
  });
}

test("edgeCurvatures: the one edge of a pair bows slightly, on the pair's side whatever its direction", () => {
  const [ab] = edgeCurvatures([e("a", "b")]);
  const [ba] = edgeCurvatures([e("b", "a")]);
  assert.ok(ab > 0 && ab < 0.2);
  // Same side of the pair: the opposite sign in the edge's own frame.
  assert.equal(ba, -ab);
  assert.deepEqual(edgeCurvatures([e("a", "a")]), [0]);
});

test("edgeCurvatures: A->B and B->A bow to opposite sides with equal curvature, labels apart", () => {
  const edges = [e("a", "b"), e("b", "a")];
  const [c1, c2] = edgeCurvatures(edges);
  // Equal in each edge's own frame: one arc on each side of the straight line.
  assert.equal(c1, c2);
  assert.ok(c1 > 0.2);
  const A = { x: 0, y: 0 };
  const B = { x: 100, y: 0 };
  const apex1 = quadraticPoint(A, arcControlPoint(A, B, c1), B, 0.5);
  const apex2 = quadraticPoint(B, arcControlPoint(B, A, c2), A, 0.5);
  assert.ok(apex1.y * apex2.y < 0, `apexes ${apex1.y} ${apex2.y} on the same side`);
  assert.ok(Math.abs(apex1.y - apex2.y) >= 24, "the two labels are at least two text lines apart");
});

test("edgeCurvatures: three edges of a pair get three distinct arcs, stepped and deterministic", () => {
  const edges = [e("b", "a"), e("a", "b"), e("a", "b"), e("x", "y")];
  const curv = edgeCurvatures(edges);
  // In the pair's frame (a->b): the bow of each arc.
  const inPairFrame = curv.slice(0, 3).map((c, i) => (edges[i].sourceId > edges[i].targetId ? -c : c));
  assert.equal(new Set(inPairFrame.map((c) => c.toFixed(6))).size, 3);
  const magnitudes = inPairFrame.map(Math.abs).sort((p, q) => p - q);
  assert.ok(magnitudes[0] > 0.2 && magnitudes[2] > magnitudes[0], `stepped ${magnitudes}`);
  assert.ok(inPairFrame.some((c) => c > 0) && inPairFrame.some((c) => c < 0), "both sides used");
  // Unrelated pair unaffected; same input, same output.
  assert.equal(curv[3], edgeCurvatures([e("x", "y")])[0]);
  assert.deepEqual(edgeCurvatures(edges), curv);
});

test("placeArcLabels keeps the apex when free, slides a colliding label along its own arc", () => {
  const a = { x: 0, y: 0 };
  const b = { x: 100, y: 0 };
  const control = arcControlPoint(a, b, 0.28);
  const arc = { a, control, b, w: 15, h: 10 };
  const [first, second] = placeArcLabels([arc, arc], []);
  assert.deepEqual(first, quadraticPoint(a, control, b, 0.5));
  assert.ok(second && first && (Math.abs(second.x - first.x) >= 15 || Math.abs(second.y - first.y) >= 10));
  // An obstacle on the apex moves the label off it; nothing free: the apex.
  const [moved] = placeArcLabels([arc], [{ ...first!, w: 15, h: 10 }]);
  assert.notDeepEqual(moved, first);
  // Nothing free: left out, unless forced (the hovered edge), which takes its apex.
  const wall = [{ x: 50, y: 0, w: 1000, h: 1000 }];
  assert.deepEqual(placeArcLabels([arc], wall), [null]);
  assert.deepEqual(placeArcLabels([{ ...arc, forced: true }], wall), [first]);
  // A forced label is placed first: it keeps the apex, the other one moves or drops.
  const [other, hovered] = placeArcLabels([arc, { ...arc, forced: true }], []);
  assert.deepEqual(hovered, first);
  assert.notDeepEqual(other, first);
  assert.deepEqual(placeArcLabels([null], []), [null]);
});

test("placeArcLabels steps a forced label off a short arc whose slots all meet a name (#12347 UX r2 B2)", () => {
  // A short arc (partner to a collapsed fleet node): every slot on it meets the name box below the apex.
  const a = { x: 0, y: 0 };
  const b = { x: 30, y: 0 };
  const control = arcControlPoint(a, b, 0.28);
  const arc = { a, control, b, w: 22, h: 11, forced: true };
  const apex = quadraticPoint(a, control, b, 0.5);
  const name = { x: 15, y: apex.y, w: 60, h: 12 };
  const sub = { x: 15, y: apex.y + 12, w: 60, h: 10 };
  assert.deepEqual(placeArcLabels([{ ...arc, forced: false }], [name, sub]), [null]);
  const [placed] = placeArcLabels([arc], [name, sub]);
  assert.ok(placed);
  const box = { ...placed, w: arc.w, h: arc.h };
  for (const q of [name, sub]) {
    const clear = Math.abs(box.x - q.x) * 2 >= box.w + q.w || Math.abs(box.y - q.y) * 2 >= box.h + q.h;
    assert.ok(clear, `forced label at ${box.x},${box.y} covers ${q.x},${q.y}`);
  }
  assert.notDeepEqual(placed, apex);
});

test("namesOverGlyphs leaves out a name drawn over another node's glyph, never over its own", () => {
  const glyphs = [
    { id: "hub", box: { x: 0, y: 0, w: 12, h: 12 } },
    { id: "spoke", box: { x: 30, y: -20, w: 12, h: 12 } },
    { id: "far", box: { x: 300, y: 300, w: 12, h: 12 } },
  ];
  const names = [
    // Under its own glyph, nothing else near: kept.
    { id: "hub", box: { x: 0, y: 12, w: 40, h: 8 } },
    // Reaches over the hub glyph: left out.
    { id: "spoke", box: { x: 20, y: -5, w: 40, h: 8 } },
    { id: "far", box: { x: 300, y: 312, w: 40, h: 8 } },
  ];
  assert.deepEqual([...namesOverGlyphs(names, glyphs)], ["spoke"]);
  assert.equal(namesOverGlyphs([], glyphs).size, 0);
});

test("fitNameStacks drops a ghost's group line, then its name, before either covers a glyph or a name", () => {
  // A ghost over a node one row lower: its two-line name fits, its group line
  // under it would reach the lower node's ring.
  const glyphs = [
    { id: "ghost", box: { x: 0, y: 0, w: 16, h: 16 } },
    { id: "below", box: { x: 0, y: 44, w: 16, h: 16 } },
    { id: "side", box: { x: 200, y: 0, w: 16, h: 16 } },
  ];
  const stacks = [
    { id: "ghost", name: { x: 0, y: 20, w: 60, h: 23 }, sub: { x: 0, y: 36, w: 50, h: 11 } },
    { id: "below", name: { x: 0, y: 64, w: 60, h: 13 }, sub: null },
    // Its group line overlaps its own name by a line gap, as drawn: with free
    // room around, it is still drawn.
    { id: "side", name: { x: 200, y: 20, w: 60, h: 13 }, sub: { x: 200, y: 31, w: 50, h: 11 } },
  ];
  const fit = fitNameStacks(stacks, glyphs);
  assert.deepEqual([...fit.subless], ["ghost"]);
  // A lone ghost keeps its group line, which always meets its own name.
  const lone = fitNameStacks([{ id: "g", name: { x: 0, y: 20, w: 60, h: 23 }, sub: { x: 0, y: 34, w: 50, h: 11 } }], [glyphs[0]]);
  assert.equal(lone.subless.size, 0);
  assert.equal(lone.hidden.size, 0);
  // A later node's name over a group line drawn earlier is left out.
  const under = fitNameStacks(
    [
      { id: "g", name: { x: 0, y: 20, w: 60, h: 13 }, sub: { x: 0, y: 31, w: 50, h: 11 } },
      { id: "n", name: { x: 10, y: 36, w: 60, h: 13 }, sub: null },
    ],
    [],
  );
  assert.deepEqual([...under.hidden], ["n"]);
  assert.equal(under.subless.size, 0);
  assert.deepEqual([...fit.hidden], []);
  // A name over a name drawn earlier is left out, its group line with it.
  const crossing = fitNameStacks(
    [
      { id: "a", name: { x: 0, y: 0, w: 60, h: 13 }, sub: null },
      { id: "b", name: { x: 30, y: 4, w: 60, h: 13 }, sub: { x: 30, y: 16, w: 40, h: 11 } },
    ],
    [],
  );
  assert.deepEqual([...crossing.hidden], ["b"]);
  assert.deepEqual([...crossing.subless], ["b"]);
  assert.equal(fitNameStacks([], glyphs).hidden.size, 0);
});

test("clearCurvature bows an arc further around a third node in its way", () => {
  const A = { x: 0, y: 0 };
  const B = { x: 200, y: 0 };
  const blocker = { x: 100, y: -3 };
  const c = clearCurvature(A, B, 0.12, [A, B, blocker], 16);
  assert.ok(c > 0.12);
  const control = arcControlPoint(A, B, c);
  for (let k = 1; k < 10; k++) {
    const q = quadraticPoint(A, control, B, k / 10);
    assert.ok(Math.hypot(q.x - blocker.x, q.y - blocker.y) >= 16);
  }
  // Nothing in the way: unchanged.
  assert.equal(clearCurvature(A, B, 0.12, [A, B, { x: 100, y: 300 }], 16), 0.12);
});

test("badgeAngles keeps every badge off the node's edge directions and off the name below", () => {
  const edgeAngles = [-Math.PI / 4, 0];
  const angles = badgeAngles(edgeAngles, 3);
  assert.equal(angles.length, 3);
  assert.equal(new Set(angles).size, 3);
  for (const a of angles) {
    for (const edge of edgeAngles) assert.ok(Math.abs(Math.atan2(Math.sin(a - edge), Math.cos(a - edge))) > 0.5);
    assert.ok(Math.abs(a - Math.PI / 2) > 0.5, "never straight down");
  }
});

test("wrapLabel wraps a fleet name on two lines before cutting it", () => {
  assert.deepEqual(wrapLabel("CEO Office", 8), ["CEO", "Office"]);
  assert.deepEqual(wrapLabel("CEO Office", 20), ["CEO Office"]);
  const lines = wrapLabel("Security operations centre fleet", 10);
  assert.equal(lines.length, 2);
  assert.ok(lines[1].endsWith("\u2026"));
  for (const line of lines) assert.ok([...line].length <= 10);
});

test("radialLayout starts the unreached rows one node distance under the lowest ring node", () => {
  const ids = ["c", "r1", "r2", "r3", "r4", "u1", "u2"];
  const pos = radialLayout(ids, [e("c", "r1"), e("c", "r2"), e("c", "r3"), e("c", "r4")], { centerId: "c" });
  const lowest = Math.max(...["c", "r1", "r2", "r3", "r4"].map((id) => pos.get(id)!.y));
  assert.equal(pos.get("u1")!.y, lowest + 90);
  assert.equal(pos.get("u2")!.y, lowest + 90);
});

test("groupBlocks makes one grid per group of two or more, the rest stays lone", () => {
  const groups = new Map([["a1", "A"], ["a2", "A"], ["a3", "A"], ["a4", "A"], ["b1", "B"]]);
  const out = groupBlocks(["x", "a3", "a1", "b1", "a4", "a2"], groups, 100, 50);
  assert.equal(out.blocks.length, 1);
  assert.deepEqual([...out.blocks[0].entries()], [
    ["a1", { x: 0, y: 0 }], ["a2", { x: 100, y: 0 }], ["a3", { x: 0, y: 50 }], ["a4", { x: 100, y: 50 }],
  ]);
  assert.deepEqual(out.rest, ["x", "b1"]);
});

test("with groups, each fleet's lone agents are packed apart from the other lone agents", () => {
  const fleetA = ["a1", "a2", "a3", "a4"];
  const fleetB = ["b1", "b2", "b3", "b4"];
  const lone = ["z1", "z2", "z3", "z4"];
  const ids = [...fleetA, ...fleetB, ...lone, "h1", "h2"];
  const groups = new Map([...fleetA.map((id) => [id, "A"] as [string, string]), ...fleetB.map((id) => [id, "B"] as [string, string])]);
  for (const layout of GRAPH_LAYOUTS) {
    const pos = layoutGraph(layout, ids, [e("h1", "h2")], { aspect: 2.4, groups, centerId: "h1" });
    assert.equal(pos.size, ids.length, layout);
    const box = (members: string[]) => {
      const pts = members.map((id) => pos.get(id)!);
      return { x0: Math.min(...pts.map((p) => p.x)), x1: Math.max(...pts.map((p) => p.x)), y0: Math.min(...pts.map((p) => p.y)), y1: Math.max(...pts.map((p) => p.y)) };
    };
    const inside = (p: { x: number; y: number }, b: ReturnType<typeof box>) => p.x >= b.x0 && p.x <= b.x1 && p.y >= b.y0 && p.y <= b.y1;
    // No agent of another group falls inside a fleet's box.
    for (const [members, others] of [[fleetA, [...fleetB, ...lone]], [fleetB, [...fleetA, ...lone]]] as const) {
      const b = box([...members]);
      for (const id of others) assert.ok(!inside(pos.get(id)!, b), `${layout}: ${id} inside a fleet's box`);
    }
    assert.ok(minimumDistance(pos.values()) >= 90 - 1e-6, layout);
  }
});

test("two fleet grids stand further apart than two columns of one grid", () => {
  const groups = new Map<string, string>();
  const ids: string[] = [];
  for (const g of ["A", "B", "C", "D", "E"]) {
    for (let i = 0; i < 9; i++) { ids.push(`${g}${i}`); groups.set(`${g}${i}`, g); }
  }
  const cell = loneCellWidth(90, 150);
  const pos = packForCanvas([], ids, 120, 90, cell, 2.4, groups);
  const boxOf = (g: string) => {
    const pts = ids.filter((id) => groups.get(id) === g).map((id) => pos.get(id)!);
    return { x0: Math.min(...pts.map((p) => p.x)), x1: Math.max(...pts.map((p) => p.x)), y0: Math.min(...pts.map((p) => p.y)), y1: Math.max(...pts.map((p) => p.y)) };
  };
  const boxes = ["A", "B", "C", "D", "E"].map(boxOf);
  for (let i = 0; i < boxes.length; i++) {
    for (let j = i + 1; j < boxes.length; j++) {
      const a = boxes[i], b = boxes[j];
      const dx = Math.max(0, b.x0 - a.x1, a.x0 - b.x1);
      const dy = Math.max(0, b.y0 - a.y1, a.y0 - b.y1);
      // Columns of one grid are `cell` apart and rows 90: two grids stand at
      // least a cell and a row apart, a row more than two columns of one.
      assert.ok(dx >= cell + 90 - 1e-6 || dy >= cell + 90 - 1e-6, `grids ${i} and ${j}: dx ${dx}, dy ${dy}`);
    }
  }
});

// #10147 UX r2 B3: a static force view keeps half its widest label line and 12 px from the frame.
test("staticLabelEdgePx keeps a long name inside the frame with 12 px", () => {
  const opts = { namePx: 11, glyphAdvance: 0.56, roomPx: 160, edgePx: 12 };
  // "corporate communications": 24 characters, 147.84 px at 11 px, under the 160 px room.
  assert.equal(staticLabelEdgePx(["corporate communications"], opts), Math.ceil(147.84 / 2) + 12);
  // A longer name wraps in its room: the line is the room, 160 px.
  assert.equal(staticLabelEdgePx(["CEO", "corporate communications and investor relations office"], opts), 80 + 12);
  assert.ok(staticLabelEdgePx(["CEO"], opts) < staticLabelEdgePx(["corporate communications"], opts));
  assert.equal(staticLabelEdgePx([], opts), 12);
});
