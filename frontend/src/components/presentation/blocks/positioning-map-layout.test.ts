import { describe, expect, it } from "vitest";

import { MAP_HIGHLIGHT_MARK_PX, MAP_MARK_PX, layoutPositioningMap } from "./positioning-map-layout";

describe("layoutPositioningMap", () => {
  it("places a mark at its x / y, y growing upwards", () => {
    const { items } = layoutPositioningMap([{ x: 25, y: 75, label: "A" }], 800, 600);
    expect(items[0].cx).toBe(200);
    expect(items[0].cy).toBe(150);
  });

  it("moves a mark visibly for a change of 10", () => {
    const a = layoutPositioningMap([{ x: 40, y: 50, label: "A" }], 800, 600).items[0];
    const b = layoutPositioningMap([{ x: 50, y: 50, label: "A" }], 800, 600).items[0];
    expect(b.cx - a.cx).toBe(80);
  });

  it("keeps a mark at the edge inside the plot", () => {
    const { items } = layoutPositioningMap([{ x: 100, y: 0, label: "A" }], 800, 600);
    expect(items[0].cx).toBe(800 - MAP_MARK_PX / 2);
    expect(items[0].cy).toBe(600 - MAP_MARK_PX / 2);
  });

  it("draws the highlight larger", () => {
    const { items } = layoutPositioningMap([{ x: 90, y: 92, label: "Acme", highlight: true }], 800, 600);
    expect(items[0].mark).toBe(MAP_HIGHLIGHT_MARK_PX);
  });

  it("moves a label off a neighbour's label", () => {
    const { items, overlaps } = layoutPositioningMap(
      [
        { x: 50, y: 50, label: "Northwind" },
        { x: 52, y: 50, label: "Contoso" },
      ],
      800,
      600,
    );
    const [a, b] = items.map((item) => item.label);
    const separate = a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y;
    expect(separate).toBe(true);
    expect(overlaps).toBe(0);
  });

  it("keeps a leader's label clear of the highlight next to it (82, 80 by 90, 92)", () => {
    // A two-up map on a 16:9 slide: the plot is about 700 x 520 canvas px.
    const { items, overlaps } = layoutPositioningMap(
      [
        { x: 90, y: 92, label: "Acme", highlight: true },
        { x: 82, y: 80, label: "Northwind" },
      ],
      700,
      520,
    );
    const [hi, leader] = items;
    const box = (cx: number, cy: number, s: number) => ({ x: cx - s / 2, y: cy - s / 2, w: s, h: s });
    const apart = (a: { x: number; y: number; w: number; h: number }, b: typeof a) =>
      a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y;
    expect(apart(hi.label, leader.label)).toBe(true);
    expect(apart(leader.label, box(hi.cx, hi.cy, hi.mark))).toBe(true);
    expect(apart(hi.label, box(leader.cx, leader.cy, leader.mark))).toBe(true);
    expect(overlaps).toBe(0);
  });

  const apartRects = (a: { x: number; y: number; w: number; h: number }, b: typeof a) =>
    a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y;
  const neutral = {
    quadrants: {
      top_left: "Open tools",
      top_right: "Open platform",
      bottom_left: "Closed tools",
      bottom_right: "Closed suites",
    },
    ends: { xLow: "Single stage", xHigh: "Whole loop", yLow: "Closed", yHigh: "Open" },
  };

  it("moves a quadrant label off the highlight in its corner (UX round 1 B1)", () => {
    const layout = layoutPositioningMap(
      [
        { x: 90, y: 92, label: "Acme", highlight: true },
        { x: 82, y: 80, label: "Northwind" },
      ],
      700,
      520,
      { axes: "center", ...neutral },
    );
    const [hi, leader] = layout.items;
    const box = (cx: number, cy: number, s: number) => ({ x: cx - s / 2, y: cy - s / 2, w: s, h: s });
    const topRight = layout.quadrants.top_right!;
    expect(topRight.overlap).toBe(false);
    expect(apartRects(topRight.rect, box(hi.cx, hi.cy, hi.mark))).toBe(true);
    expect(apartRects(topRight.rect, box(leader.cx, leader.cy, leader.mark))).toBe(true);
    expect(apartRects(topRight.rect, hi.label)).toBe(true);
    expect(apartRects(topRight.rect, leader.label)).toBe(true);
    expect(layout.overlaps).toBe(0);
  });

  it("keeps an item label off a quadrant label (UX round 1 B1, Initech over Closed tools)", () => {
    const layout = layoutPositioningMap([{ x: 6, y: 6, label: "Initech" }], 700, 520, { axes: "center", ...neutral });
    const bottomLeft = layout.quadrants.bottom_left!;
    expect(apartRects(layout.items[0].label, bottomLeft.rect)).toBe(true);
    expect(apartRects(bottomLeft.rect, { x: layout.items[0].cx - 20, y: layout.items[0].cy - 20, w: 40, h: 40 })).toBe(true);
  });

  it("keeps the quadrant labels clear of the end labels on edges axes (UX round 1 B2)", () => {
    const layout = layoutPositioningMap([], 700, 520, { axes: "edges", ...neutral });
    const quadrants = Object.values(layout.quadrants);
    expect(quadrants).toHaveLength(4);
    for (const q of quadrants) expect(q!.overlap).toBe(false);
    expect(layout.overlaps).toBe(0);
  });

  it("keeps a corner mark clear of every end label on edges axes (UX round 2 B1, Globex at (6, 6))", () => {
    const layout = layoutPositioningMap([{ x: 6, y: 6, label: "Globex" }], 700, 520, { axes: "edges", ...neutral });
    // The end labels sit outside the plot: nothing inside is reserved for them.
    expect(layout.items[0].overlap).toBe(false);
    expect(layout.overlaps).toBe(0);
  });

  it("keeps an item label off the crossing axis lines on center axes (UX round 3 B1, Stark at (15, 55))", () => {
    const width = 700;
    const height = 520;
    const layout = layoutPositioningMap([{ x: 15, y: 55, label: "Stark" }], width, height, { axes: "center", ...neutral });
    const label = layout.items[0].label;
    // The x axis is a 2 px line at mid height; the label clears it on one side.
    const xAxis = { x: 0, y: height / 2 - 1, w: width, h: 2 };
    const yAxis = { x: width / 2 - 1, y: 0, w: 2, h: height };
    expect(apartRects(label, xAxis)).toBe(true);
    expect(apartRects(label, yAxis)).toBe(true);
    expect(Math.min(Math.abs(label.y + label.h - (height / 2 - 1)), Math.abs(label.y - (height / 2 + 1)))).toBeGreaterThanOrEqual(3);
    expect(layout.items[0].overlap).toBe(false);
    expect(layout.overlaps).toBe(0);
  });

  it("lets the label of a mark sitting on an axis cross that axis, never the other one", () => {
    const width = 700;
    const height = 520;
    // (15, 50): the mark is on the x axis, far from the y axis.
    const layout = layoutPositioningMap([{ x: 15, y: 50, label: "Stark" }], width, height, { axes: "center", ...neutral });
    expect(apartRects(layout.items[0].label, { x: width / 2 - 1, y: 0, w: 2, h: height })).toBe(true);
    expect(layout.overlaps).toBe(0);
  });

  it("moves the label of a mark on the y axis beside it, off that axis (UX round 4 B1, Tyrell at (50, 70))", () => {
    const width = 700;
    const height = 520;
    const layout = layoutPositioningMap([{ x: 50, y: 70, label: "Tyrell" }], width, height, { axes: "center", ...neutral });
    const item = layout.items[0];
    expect(["left", "right"]).toContain(item.side);
    expect(apartRects(item.label, { x: width / 2 - 1, y: 0, w: 2, h: height })).toBe(true);
    expect(apartRects(item.label, { x: 0, y: height / 2 - 1, w: width, h: 2 })).toBe(true);
    expect(layout.overlaps).toBe(0);
  });

  it("does not report the crossing a mark on both axes cannot avoid", () => {
    const layout = layoutPositioningMap([{ x: 50, y: 50, label: "Center" }], 700, 520, { axes: "center", ...neutral });
    expect(layout.items[0].overlap).toBe(false);
    expect(layout.overlaps).toBe(0);
  });

  it("keeps the natural side on edges axes, whose lines run along the plot's border", () => {
    const layout = layoutPositioningMap([{ x: 15, y: 55, label: "Stark" }], 700, 520, { axes: "edges", ...neutral });
    expect(layout.items[0].side).toBe("below");
  });

  it("is deterministic", () => {
    const input = Array.from({ length: 12 }, (_, i) => ({ x: (i * 37) % 100, y: (i * 53) % 100, label: `Vendor ${i}` }));
    expect(layoutPositioningMap(input, 700, 520)).toEqual(layoutPositioningMap(input, 700, 520));
  });
});
