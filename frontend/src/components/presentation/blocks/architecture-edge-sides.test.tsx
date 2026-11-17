/**
 * Edge sides, aligned pairs and zone ends in the architecture block -
 * `yarn test:dom`.
 *
 * An edge may pin the side it leaves and enters (`source_side` /
 * `target_side`), order the edges of one pair of nodes on `lane`s, and end on
 * a zone's border (`zone:<id, label or index>`): the geometry
 * `layoutArchitecture` returns for each, what `ArchitectureBlockView` draws,
 * and the fixture the backend's export and layout-check tests draw in
 * Chromium (`backend/tests/data/architecture/edge-sides.*`). Every label is a
 * generic placeholder.
 */
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test, vi } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckArchitectureBlock, DeckArchitectureEdge, DeckArchitectureZone, DeckEdgeSide } from "@/types";

import { ArchitectureBlockView } from "./Architecture";
import {
  ARCH,
  layoutArchitecture,
  resolveZoneRef,
  segmentIntersectsRect,
  segmentsCross,
  zoneHeadBox,
  type ArchitectureLayout,
  type LayoutEdge,
  type LayoutNode,
  type LayoutZone,
  type Point,
  type Rect,
} from "./architecture-layout";

// The icon catalog, loaded natively (see `metrics.test.tsx`).
vi.mock("../deck-icons", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../deck-icons")>();
  const { icons } = await import("lucide-react");
  return { ...actual, getDeckIconCatalog: () => icons, loadDeckIconCatalog: () => Promise.resolve(icons) };
});

const DATA = resolve(process.cwd(), "../backend/tests/data/architecture");
const SIDES: DeckEdgeSide[] = ["top", "right", "bottom", "left"];
const OUT: Record<DeckEdgeSide, Point> = { top: { x: 0, y: -1 }, right: { x: 1, y: 0 }, bottom: { x: 0, y: 1 }, left: { x: -1, y: 0 } };

// ── Helpers ───────────────────────────────────────────────────────────────────

const round = (n: number) => Math.round(n * 10) / 10;
const rect = (r: Rect) => [round(r.x), round(r.y), round(r.w), round(r.h)];
const point = (p: Point) => [round(p.x), round(p.y)];
const overlaps = (a: Rect, b: Rect): boolean => a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
const deflate = (r: Rect, by: number): Rect => ({ x: r.x + by, y: r.y + by, w: r.w - 2 * by, h: r.h - 2 * by });

const segments = (points: Point[]): [Point, Point][] => points.slice(1).map((p, i) => [points[i], p]);

const nodeOf = (layout: ArchitectureLayout, id: string): LayoutNode => {
  const node = layout.nodes.find((n) => n.id === id);
  if (!node) throw new Error(`no node ${id}`);
  return node;
};

const edgeOf = (layout: ArchitectureLayout, source: string, target: string): LayoutEdge => {
  const edge = layout.edges.find((e) => e.edge.source === source && e.edge.target === target);
  if (!edge) throw new Error(`no edge ${source} -> ${target}`);
  return edge;
};

/** Is `p` on the line of `side` of `box`, within its extent? */
function onSide(p: Point, box: Rect, side: DeckEdgeSide): boolean {
  const eps = 0.5;
  const withinX = p.x >= box.x - eps && p.x <= box.x + box.w + eps;
  const withinY = p.y >= box.y - eps && p.y <= box.y + box.h + eps;
  switch (side) {
    case "top":
      return Math.abs(p.y - box.y) <= eps && withinX;
    case "bottom":
      return Math.abs(p.y - box.y - box.h) <= eps && withinX;
    case "left":
      return Math.abs(p.x - box.x) <= eps && withinY;
    default:
      return Math.abs(p.x - box.x - box.w) <= eps && withinY;
  }
}

/** Every run of the path is horizontal or vertical. */
const orthogonal = (points: Point[]) => segments(points).every(([a, b]) => Math.abs(a.x - b.x) < 1e-6 || Math.abs(a.y - b.y) < 1e-6);

/** The path leaves along `side`'s normal at its first point and enters against `targetSide`'s normal at its last. */
function leavesAndEnters(points: Point[], side: DeckEdgeSide, targetSide: DeckEdgeSide): boolean {
  const [a, b] = [points[0], points[1]];
  const [c, d] = [points[points.length - 2], points[points.length - 1]];
  const along = (p: Point, q: Point, dir: Point) => (q.x - p.x) * dir.x + (q.y - p.y) * dir.y;
  const across = (p: Point, q: Point, dir: Point) => Math.abs((q.x - p.x) * dir.y - (q.y - p.y) * dir.x);
  return (
    along(a, b, OUT[side]) > 0 && across(a, b, OUT[side]) < 1e-6 && along(c, d, OUT[targetSide]) < 0 && across(c, d, OUT[targetSide]) < 1e-6
  );
}

const edge = (source: string, target: string, extra: Partial<DeckArchitectureEdge> = {}): DeckArchitectureEdge => ({ source, target, ...extra });

// ── Zone references ───────────────────────────────────────────────────────────

describe("resolveZoneRef", () => {
  const zones: DeckArchitectureZone[] = [
    { id: "platform", label: "Platform", row: 1, col: 1 },
    { label: "**Edge** tier", row: 2, col: 1 },
    { label: "Shared", row: 3, col: 1 },
    { label: "Shared", row: 4, col: 1 },
  ];

  test.each([
    ["zone:platform", 0],
    ["zone:0", 0],
    ["zone:1", 1],
    ["zone:Platform", 0],
    ["zone:**Edge** tier", 1],
    ["zone:Edge tier", 1],
  ])("%s names zones.%i", (ref, index) => {
    expect(resolveZoneRef(ref, zones)).toBe(index);
  });

  test.each(["zone:nowhere", "zone:4", "zone:Shared", "platform", "Platform"])("%s names no zone", (ref) => {
    expect(resolveZoneRef(ref, zones)).toBeNull();
  });

  test("a digit-only label past the last index names its zone", () => {
    const years: DeckArchitectureZone[] = [
      { label: "2025", row: 1, col: 1 },
      { label: "2026", row: 2, col: 1 },
    ];
    expect(resolveZoneRef("zone:2026", years)).toBe(1);
    expect(resolveZoneRef("zone:1", years)).toBe(1);
    expect(resolveZoneRef("zone:7", years)).toBeNull();
  });
});

// ── Pinned sides ──────────────────────────────────────────────────────────────

describe("source_side / target_side on every side", () => {
  // Two products on one row with a third under the gap between them: every
  // pair of sides, facing the other end or away from it, is honoured.
  const pinned = (sourceSide: DeckEdgeSide, targetSide: DeckEdgeSide): DeckArchitectureBlock => ({
    type: "architecture",
    nodes: [
      { id: "a", label: "Product A", row: 2, col: 2 },
      { id: "b", label: "Product B", row: 2, col: 4 },
      { id: "c", label: "Product C", row: 3, col: 3 },
    ],
    edges: [edge("a", "b", { source_side: sourceSide, target_side: targetSide, label: "Flow" })],
  });

  const pairs = SIDES.flatMap((s) => SIDES.map((t) => [s, t] as const));

  test.each(pairs)("leaves a by its %s side and enters b by its %s side, orthogonal, around every node", (sourceSide, targetSide) => {
    const layout = layoutArchitecture(pinned(sourceSide, targetSide));
    const a = nodeOf(layout, "a");
    const b = nodeOf(layout, "b");
    const { points } = edgeOf(layout, "a", "b");
    expect(onSide(points[0], a, sourceSide), `starts at ${point(points[0])}`).toBe(true);
    expect(onSide(points[points.length - 1], b, targetSide), `ends at ${point(points[points.length - 1])}`).toBe(true);
    expect(orthogonal(points)).toBe(true);
    expect(leavesAndEnters(points, sourceSide, targetSide)).toBe(true);
    for (const node of layout.nodes) {
      // Not through the third node, nor back through either end.
      for (const [p, q] of segments(points)) expect(segmentIntersectsRect(p, q, deflate(node, 1)), `through ${node.id}`).toBe(false);
    }
  });

  test("a side alone is pinned and the other end is the router's pick", () => {
    const layout = layoutArchitecture({ ...pinned("left", "left"), edges: [edge("a", "b", { source_side: "bottom" })] });
    const { points } = edgeOf(layout, "a", "b");
    expect(onSide(points[0], nodeOf(layout, "a"), "bottom")).toBe(true);
    expect(orthogonal(points)).toBe(true);
    // The shortest way in from under a: b's bottom, by one corridor under the row.
    expect(onSide(points[points.length - 1], nodeOf(layout, "b"), "bottom")).toBe(true);
    expect(points).toHaveLength(4);
  });

  test("a pinned side turns an auto tie to a core into elbows, an explicit straight one stays a chord", () => {
    const block: DeckArchitectureBlock = {
      type: "architecture",
      nodes: [
        { id: "core", label: "Core", shape: "circle", emphasis: "core", row: 1, col: 1 },
        { id: "x", label: "Product X", row: 2, col: 2 },
      ],
      edges: [edge("core", "x", { arrows: "none", source_side: "right", target_side: "top" })],
    };
    const elbow = edgeOf(layoutArchitecture(block), "core", "x");
    expect(elbow.points).toHaveLength(3);
    expect(leavesAndEnters(elbow.points, "right", "top")).toBe(true);
    const chord = edgeOf(layoutArchitecture({ ...block, edges: [{ ...block.edges![0], route: "straight" }] }), "core", "x");
    expect(chord.points).toHaveLength(2);
    expect(onSide(chord.points[1], nodeOf(layoutArchitecture(block), "x"), "top")).toBe(true);
  });

  test("pinned edges are deterministic", () => {
    const block = pinned("top", "bottom");
    expect(layoutArchitecture(block).edges.map((e) => e.d)).toEqual(layoutArchitecture(block).edges.map((e) => e.d));
  });
});

// ── Lanes and the default symmetric pair ──────────────────────────────────────

describe("an exchange between two nodes of one row", () => {
  // `a` also feeds `d` from its right side, so its right side carries three
  // anchors and `b`'s left side two: the case that slanted a straight pair
  // and jogged an orthogonal one before the pair was aligned.
  const row = (pair: DeckArchitectureEdge[]): DeckArchitectureBlock => ({
    type: "architecture",
    nodes: [
      { id: "a", label: "Product A", icon: "brain", row: 1, col: 1 },
      { id: "b", label: "Product B", icon: "crosshair", row: 1, col: 3 },
      { id: "d", label: "Product D", icon: "radar", row: 2, col: 2 },
    ],
    edges: [...pair, edge("a", "d", { source_side: "right", label: "Side" })],
  });

  const level = (e: LayoutEdge) => {
    expect(e.points).toHaveLength(2);
    expect(Math.abs(e.points[0].y - e.points[1].y)).toBeLessThan(1e-6);
    return e.points[0].y;
  };

  test("on lanes 1 and 2: two exactly horizontal parallel arrows, lane 1 above, labels centred", () => {
    const layout = layoutArchitecture(
      row([edge("a", "b", { route: "straight", lane: 1, label: "Ask" }), edge("b", "a", { route: "straight", lane: 2, label: "Answer" })]),
    );
    const forth = edgeOf(layout, "a", "b");
    const back = edgeOf(layout, "b", "a");
    const a = nodeOf(layout, "a");
    expect(level(forth)).toBeCloseTo(a.cy - ARCH.LANE / 2, 6);
    expect(level(back)).toBeCloseTo(a.cy + ARCH.LANE / 2, 6);
    for (const e of [forth, back]) {
      const middle = (e.points[0].x + e.points[1].x) / 2;
      expect(e.label!.x + e.label!.w / 2).toBeCloseTo(middle, 6);
      expect(e.label!.y + e.label!.h / 2).toBeCloseTo(e.points[0].y, 6);
    }
    // The third anchor on a's right side lines up past the pair, under its lower lane.
    const side = edgeOf(layout, "a", "d").points[0];
    expect(side.x).toBeCloseTo(a.x + a.w, 6);
    expect(side.y - a.cy).toBeGreaterThan(ARCH.LANE / 2);
  });

  test("swapping the lanes swaps the arrows", () => {
    const layout = layoutArchitecture(row([edge("a", "b", { route: "straight", lane: 2 }), edge("b", "a", { route: "straight", lane: 1 })]));
    expect(level(edgeOf(layout, "a", "b"))).toBeGreaterThan(level(edgeOf(layout, "b", "a")));
  });

  test.each([
    ["straight", "straight"],
    ["orthogonal", "orthogonal"],
    ["auto", undefined],
  ] as const)("without lanes, %s: still exactly horizontal, -d and +d on both nodes", (_name, route) => {
    const layout = layoutArchitecture(row([edge("a", "b", { route }), edge("b", "a", { route })]));
    const ys = [level(edgeOf(layout, "a", "b")), level(edgeOf(layout, "b", "a"))].sort((p, q) => p - q);
    const cy = nodeOf(layout, "a").cy;
    expect(ys[0]).toBeCloseTo(cy - ARCH.LANE / 2, 6);
    expect(ys[1]).toBeCloseTo(cy + ARCH.LANE / 2, 6);
  });

  test("a lone edge on a lane is level whatever else meets its sides", () => {
    const layout = layoutArchitecture(row([edge("a", "b", { route: "straight", lane: 1 })]));
    expect(level(edgeOf(layout, "a", "b"))).toBeCloseTo(nodeOf(layout, "a").cy, 6);
  });

  test("two stacked nodes on lanes: two exactly vertical arrows, lane 1 on the left", () => {
    const layout = layoutArchitecture({
      type: "architecture",
      nodes: [
        { id: "up", label: "Product A", row: 1, col: 1 },
        { id: "down", label: "Product B", row: 3, col: 1 },
        { id: "aside", label: "Product C", row: 2, col: 2 },
      ],
      edges: [edge("up", "down", { route: "straight", lane: 1 }), edge("down", "up", { route: "straight", lane: 2 }), edge("up", "aside")],
    });
    const xs = (e: LayoutEdge) => {
      expect(e.points).toHaveLength(2);
      expect(Math.abs(e.points[0].x - e.points[1].x)).toBeLessThan(1e-6);
      return e.points[0].x;
    };
    const cx = nodeOf(layout, "up").cx;
    expect(xs(edgeOf(layout, "up", "down"))).toBeCloseTo(cx - ARCH.LANE / 2, 6);
    expect(xs(edgeOf(layout, "down", "up"))).toBeCloseTo(cx + ARCH.LANE / 2, 6);
  });
});

// ── Zone ends ─────────────────────────────────────────────────────────────────

describe("an edge ending on a zone's border", () => {
  // The zone leaves a free column on either side, so a route may go around it.
  const zoned = (edges: DeckArchitectureEdge[]): DeckArchitectureBlock => ({
    type: "architecture",
    nodes: [
      { id: "ctx", label: "Context", row: 1, col: 3 },
      { id: "a", label: "Product A", row: 2, col: 2 },
      { id: "b", label: "Product B", row: 2, col: 4 },
      { id: "side", label: "Partner", row: 3, col: 5 },
    ],
    edges: [edge("a", "b"), ...edges],
    zones: [{ id: "platform", label: "Platform", row: 2, col: 2, row_span: 2, col_span: 3 }],
  });

  test("from a node above: straight down onto the zone's top border, the zone's first row holding nodes", () => {
    const layout = layoutArchitecture(zoned([edge("ctx", "zone:platform", { label: "Feeds" })]));
    const zone = layout.zones[0];
    const ctx = nodeOf(layout, "ctx");
    const { points, label } = edgeOf(layout, "ctx", "zone:platform");
    expect(points).toHaveLength(2);
    expect(point(points[0])).toEqual(point({ x: ctx.cx, y: ctx.y + ctx.h }));
    expect(point(points[1])).toEqual(point({ x: ctx.cx, y: zone.y }));
    expect(label).not.toBeNull();
    // No row was taken for it: the drawing is the one without the edge.
    const without = layoutArchitecture(zoned([]));
    expect(layout.nodes.map(rect)).toEqual(without.nodes.map(rect));
    expect(rect(zone)).toEqual(rect(without.zones[0]));
    expect(nodeOf(layout, "a").y).toBe(zone.y + 10 + ARCH.ZONE_HEAD);
  });

  test.each(["zone:0", "zone:Platform"])("%s draws what zone:platform draws", (ref) => {
    const byId = layoutArchitecture(zoned([edge("ctx", "zone:platform")]));
    const other = layoutArchitecture(zoned([edge("ctx", ref)]));
    expect(edgeOf(other, "ctx", ref).points).toEqual(edgeOf(byId, "ctx", "zone:platform").points);
  });

  test("a zone works as a source, and an exchange with it runs as two lines", () => {
    const layout = layoutArchitecture(zoned([edge("ctx", "zone:platform"), edge("zone:platform", "ctx")]));
    const zone = layout.zones[0];
    const down = edgeOf(layout, "ctx", "zone:platform").points;
    const up = edgeOf(layout, "zone:platform", "ctx").points;
    expect(up[0].y).toBeCloseTo(zone.y, 6);
    expect(down[down.length - 1].y).toBeCloseTo(zone.y, 6);
    for (const path of [down, up]) expect(path).toHaveLength(2);
    expect(Math.abs(down[0].x - up[0].x)).toBeCloseTo(ARCH.LANE, 6);
  });

  test("from a node beside the zone: across onto its side border, or onto the side it pins", () => {
    const layout = layoutArchitecture(zoned([edge("side", "zone:platform")]));
    const zone = layout.zones[0];
    const across = edgeOf(layout, "side", "zone:platform").points;
    expect(across).toHaveLength(2);
    expect(across[1].x).toBeCloseTo(zone.x + zone.w, 6);
    expect(across[0].y).toBeCloseTo(across[1].y, 6);
    const pinned = edgeOf(layoutArchitecture(zoned([edge("side", "zone:platform", { target_side: "bottom" })])), "side", "zone:platform");
    const end = pinned.points[pinned.points.length - 1];
    expect(end.y).toBeCloseTo(zone.y + zone.h, 6);
    expect(end.x).toBeGreaterThanOrEqual(zone.x);
    expect(end.x).toBeLessThanOrEqual(zone.x + zone.w);
    // Left by the side facing the zone, entered from under it.
    expect(leavesAndEnters(pinned.points, "left", "bottom")).toBe(true);
  });

  test("the route never runs through the zone it ends on", () => {
    for (const target_side of SIDES) {
      const layout = layoutArchitecture(zoned([edge("ctx", "zone:platform", { target_side })]));
      const inner = deflate(layout.zones[0], 2);
      for (const [p, q] of segments(edgeOf(layout, "ctx", "zone:platform").points)) expect(segmentIntersectsRect(p, q, inner), target_side).toBe(false);
    }
  });

  test("is drawn with its arrowhead at the border", () => {
    const { container } = renderWithProviders(<ArchitectureBlockView block={zoned([edge("ctx", "zone:platform")])} />);
    const paths = Array.from(container.querySelectorAll(".deck-arch-edge-line"));
    expect(paths).toHaveLength(2);
    expect(paths[1].getAttribute("marker-end")).toMatch(/^url\(#deck-arch-.*-accent\)$/);
  });
});

// ── The fixture the backend draws in Chromium ─────────────────────────────────

/**
 * A platform zone (`id: platform`) over rows 2-3 and a context node above it
 * feeding the zone's border; a centre node in the zone's first row with a
 * squared exchange to each bottom corner, every side pinned; and the two
 * corners exchanging along the bottom row on lanes 1 and 2.
 */
export const EDGE_SIDES: DeckArchitectureBlock = {
  type: "architecture",
  nodes: [
    { id: "context", label: "Context", icon: "radar", row: 1, col: 3 },
    { id: "centre", label: "Core", icon: "sparkles", row: 2, col: 3 },
    { id: "left", label: "Product A", icon: "brain", row: 3, col: 1 },
    { id: "right", label: "Product B", icon: "crosshair", row: 3, col: 5 },
  ],
  edges: [
    edge("context", "zone:platform", { label: "Feeds" }),
    edge("centre", "left", { source_side: "left", target_side: "top", label: "Tasks" }),
    edge("left", "centre", { source_side: "top", target_side: "left", label: "Results" }),
    edge("centre", "right", { source_side: "right", target_side: "top", label: "Tasks" }),
    edge("right", "centre", { source_side: "top", target_side: "right", label: "Results" }),
    edge("left", "right", { route: "straight", lane: 1, label: "Requests" }),
    edge("right", "left", { route: "straight", lane: 2, label: "Answers" }),
  ],
  zones: [{ id: "platform", label: "Platform", row: 2, col: 1, row_span: 2, col_span: 5 }],
  size: "lg",
};

function renderBlock(block: DeckArchitectureBlock, scheme = "dark") {
  return renderWithProviders(
    <div className="deck-root" data-scheme={scheme}>
      <ArchitectureBlockView block={block} />
    </div>,
  );
}

/** The drawing as a reviewer reads it off the page: every path and its marker, every label pill. */
function drawnEdges(container: HTMLElement) {
  return Array.from(container.querySelectorAll(".deck-arch-edge")).map((g) => {
    const path = g.querySelector(".deck-arch-edge-line")!;
    const pill = g.querySelector(".deck-arch-edge-label rect");
    return {
      d: path.getAttribute("d"),
      marker: path.getAttribute("marker-end") !== null,
      label: pill ? ["x", "y", "width", "height"].map((name) => pill.getAttribute(name)) : null,
      text: g.querySelector(".deck-arch-edge-text")?.textContent ?? null,
    };
  });
}

describe("the edge-sides fixture", () => {
  const layout = layoutArchitecture(EDGE_SIDES);
  const zone: LayoutZone = layout.zones[0];
  const centre = nodeOf(layout, "centre");
  const left = nodeOf(layout, "left");
  const right = nodeOf(layout, "right");
  const axis = centre.cx;

  test("the exchanges with the bottom corners are two squared paths on each side, parallel, mirrored, uncrossed", () => {
    const pairs = [
      [edgeOf(layout, "centre", "left"), edgeOf(layout, "left", "centre"), left, "left"],
      [edgeOf(layout, "centre", "right"), edgeOf(layout, "right", "centre"), right, "right"],
    ] as const;
    for (const [out, back, corner, side] of pairs) {
      expect(out.points).toHaveLength(3);
      expect(back.points).toHaveLength(3);
      expect(onSide(out.points[0], centre, side)).toBe(true);
      expect(onSide(out.points[2], corner, "top")).toBe(true);
      expect(onSide(back.points[0], corner, "top")).toBe(true);
      expect(onSide(back.points[2], centre, side)).toBe(true);
      // Nested elbows: one corner up and out of the other, both runs a lane apart.
      const [c1, c2] = [out.points[1], back.points[1]];
      expect(Math.abs(c1.x - c2.x)).toBeCloseTo(ARCH.LANE, 6);
      expect(Math.abs(c1.y - c2.y)).toBeCloseTo(ARCH.LANE, 6);
      expect(Math.sign(c1.x - c2.x) * (side === "left" ? 1 : -1)).toBe(Math.sign(c1.y - c2.y));
      for (const [p, q] of segments(out.points)) for (const [r, s] of segments(back.points)) expect(segmentsCross(p, q, r, s)).toBe(false);
    }
    // The right side is the left side's mirror image about the centre node's axis.
    const mirror = (points: Point[]) => points.map((p) => point({ x: 2 * axis - p.x, y: p.y }));
    const leftSet = [edgeOf(layout, "centre", "left"), edgeOf(layout, "left", "centre")].map((e) => [...e.points].sort((p, q) => p.y - q.y || p.x - q.x));
    const rightSet = [edgeOf(layout, "centre", "right"), edgeOf(layout, "right", "centre")].map((e) => mirror([...e.points].sort((p, q) => p.y - q.y || q.x - p.x)));
    expect(new Set(rightSet.map((points) => JSON.stringify(points)))).toEqual(new Set(leftSet.map((points) => JSON.stringify(points.map(point)))));
  });

  test("the bottom pair on lanes 1 and 2 is two exactly horizontal arrows, labels centred", () => {
    const forth = edgeOf(layout, "left", "right");
    const back = edgeOf(layout, "right", "left");
    for (const e of [forth, back]) {
      expect(e.points).toHaveLength(2);
      expect(e.points[0].y).toBe(e.points[1].y);
      expect(e.label!.x + e.label!.w / 2).toBeCloseTo((e.points[0].x + e.points[1].x) / 2, 6);
    }
    expect(back.points[0].y - forth.points[0].y).toBeCloseTo(ARCH.LANE, 6);
    expect(forth.points[0].y).toBeCloseTo(left.cy - ARCH.LANE / 2, 6);
  });

  test("the context feeds the zone's top border, straight down, the zone's first row holding the centre node", () => {
    const feed = edgeOf(layout, "context", "zone:platform");
    expect(feed.points).toHaveLength(2);
    expect(feed.points[1].y).toBe(zone.y);
    expect(feed.points[0].x).toBe(feed.points[1].x);
    expect(feed.points[1].x).toBeGreaterThan(zone.x);
    expect(feed.points[1].x).toBeLessThan(zone.x + zone.w);
    // No band: the centre node starts right under the head every zone reserves.
    expect(centre.y).toBe(zone.y + 10 + ARCH.ZONE_HEAD);
  });

  test("overlaps nothing: no edge through a node, no crossing, no label on a node, the head or another label", () => {
    const labels = layout.edges.flatMap((e) => (e.label ? [{ key: e.key, label: e.label }] : []));
    expect(labels).toHaveLength(EDGE_SIDES.edges!.length);
    const head = zoneHeadBox(zone);
    for (const [index, { key, label }] of labels.entries()) {
      expect(overlaps(label, head), `${key} on the head`).toBe(false);
      for (const node of layout.nodes) expect(overlaps(label, node), `${key} on ${node.id}`).toBe(false);
      for (const other of labels.slice(index + 1)) expect(overlaps(label, other.label), `${key} on ${other.key}`).toBe(false);
    }
    for (const e of layout.edges) {
      for (const node of layout.nodes) {
        if (node.id === e.edge.source || node.id === e.edge.target) continue;
        for (const [p, q] of segments(e.points)) expect(segmentIntersectsRect(p, q, node), `${e.key} through ${node.id}`).toBe(false);
      }
    }
    for (let i = 0; i < layout.edges.length; i += 1) {
      for (let j = i + 1; j < layout.edges.length; j += 1) {
        for (const [p, q] of segments(layout.edges[i].points)) {
          for (const [r, s] of segments(layout.edges[j].points)) expect(segmentsCross(p, q, r, s), `${layout.edges[i].key} x ${layout.edges[j].key}`).toBe(false);
        }
      }
    }
  });

  test("is drawn the same in both schemes, every arrow with its head", () => {
    const renders = ["dark", "light"].map((scheme) => {
      const { container, unmount } = renderBlock(EDGE_SIDES, scheme);
      const drawn = drawnEdges(container);
      unmount();
      return drawn;
    });
    expect(renders[1]).toEqual(renders[0]);
    expect(renders[0].every((e) => e.marker)).toBe(true);
    expect(renders[0]).toMatchSnapshot();
  });

  test("is the fixture the backend's export and layout-check tests draw in Chromium", () => {
    // `ARCHITECTURE_FIXTURE=write yarn test:dom` rewrites the files after a deliberate change to the drawing.
    const geometry = `${JSON.stringify(
      {
        viewBox: [layout.width, layout.height],
        nodes: layout.nodes.map((node) => ({ id: node.id, box: rect(node) })),
        zone: { panel: rect(zone) },
        edges: layout.edges.map((e) => ({
          source: e.edge.source,
          target: e.edge.target,
          points: e.points.map(point),
          label: e.label ? rect(e.label) : null,
        })),
      },
      null,
      2,
    )}\n`;
    const { container, unmount } = renderBlock(EDGE_SIDES);
    const figure = container.querySelector("figure.deck-architecture")!;
    const html = `${stableIds(new XMLSerializer().serializeToString(figure))}\n`;
    unmount();
    for (const [file, content] of [
      ["edge-sides.html", html],
      ["edge-sides.json", geometry],
    ]) {
      if (process.env.ARCHITECTURE_FIXTURE === "write") writeFileSync(resolve(DATA, file), content);
      expect(readFileSync(resolve(DATA, file), "utf8").replace(/\r\n/g, "\n"), file).toBe(content);
    }
  });
});

function stableIds(html: string): string {
  const ids = Array.from(new Set(Array.from(html.matchAll(/ id="([^"]+)"/g), (match) => match[1])));
  return [...ids].sort((a, b) => b.length - a.length).reduce((out, id) => out.split(id).join(`fixture-${ids.indexOf(id)}`), html);
}
