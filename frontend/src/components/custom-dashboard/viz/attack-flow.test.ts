import { describe, expect, it } from "vitest";

import type { WidgetGraphData } from "@/types";

import { FLOW, READABLE_ZOOM, layoutFlow, openingViewport } from "./attack-flow";

const node = (id: string, kind?: "start" | "action" | "operator") => ({ id, label: id, group: null, value: null, ...(kind ? { kind } : {}) });
const edge = (source: string, target: string) => ({ source, target, label: null, value: null });

describe("an attack flow's layout", () => {
  it("puts a step one column after the latest step leading to it, the columns' steps centred", () => {
    const graph: WidgetGraphData = {
      nodes: [node("start", "start"), node("a"), node("b"), node("or", "operator"), node("c")],
      edges: [edge("start", "a"), edge("start", "b"), edge("a", "or"), edge("b", "or"), edge("or", "c"), edge("a", "c")],
    };
    const { placed, columns } = layoutFlow(graph);
    const at = Object.fromEntries(placed.map((p) => [p.node.id, p]));
    expect(columns).toBe(4);
    expect([at.start.x, at.a.x, at.or.x, at.c.x]).toEqual([0, FLOW.column, 2 * FLOW.column + (FLOW.width - FLOW.operator) / 2, 3 * FLOW.column]);
    expect(at.b.x).toBe(FLOW.column);
    // Two steps in the tallest column: the single ones sit half a row down, between them.
    expect([at.a.y, at.b.y, at.start.y]).toEqual([0, FLOW.row, FLOW.row / 2]);
    expect(at.a.kind).toBe("action");
  });

  it("still draws a flow that loops back: the steps of the loop stacked in one column after the rest", () => {
    const { placed } = layoutFlow({ nodes: [node("s", "start"), node("a"), node("b")], edges: [edge("a", "b"), edge("b", "a")] });
    expect(placed.map((p) => [p.node.id, p.x, p.y])).toEqual([
      ["s", 0, FLOW.row / 2],
      ["a", FLOW.column, 0],
      ["b", FLOW.column, FLOW.row],
    ]);
  });

  it("opens whole when the flow fits readable, else at a readable zoom from its start", () => {
    const small = openingViewport({ width: 500, height: 200 }, { width: 1000, height: 400 });
    expect(small.zoom).toBe(1);
    expect(small.x).toBe(250);
    // Fifteen tactics of a long kill chain in a dashboard tile: too small whole, so readable from the left.
    const long = openingViewport({ width: 15 * FLOW.column, height: 23 * FLOW.row }, { width: 1200, height: 480 });
    expect(long).toEqual({ x: 16, y: (480 - 23 * FLOW.row * READABLE_ZOOM) / 2, zoom: READABLE_ZOOM });
  });
});
