import { describe, expect, it } from "vitest";
import {
  GRAPH_TOOLTIP_CURSOR_GAP,
  GRAPH_TOOLTIP_EDGE_GAP,
  GRAPH_TOOLTIP_POINTER_GAP,
  graphTooltipLeft,
  graphTooltipPosition,
  graphTooltipTop,
} from "./agent-flow-tooltip";

// The agent graph's hover tooltip stays inside the canvas.
describe("graphTooltipLeft", () => {
  it("centres the tooltip on the pointer away from the edges", () => {
    expect(graphTooltipLeft(500, 200, 1440)).toBe(400);
  });

  it("shifts the tooltip left near the right edge", () => {
    const left = graphTooltipLeft(1430, 200, 1440);
    expect(left).toBe(1440 - 200 - GRAPH_TOOLTIP_EDGE_GAP);
    expect(left + 200).toBeLessThanOrEqual(1440);
  });

  it("shifts the tooltip right near the left edge", () => {
    expect(graphTooltipLeft(5, 200, 1024)).toBe(GRAPH_TOOLTIP_EDGE_GAP);
  });

  it("starts a tooltip wider than the canvas at the left gap", () => {
    expect(graphTooltipLeft(300, 700, 600)).toBe(GRAPH_TOOLTIP_EDGE_GAP);
  });
});

// Vertically it sits above the pointer and flips below near the top.
describe("graphTooltipTop", () => {
  it("sits above the pointer away from the top edge", () => {
    expect(graphTooltipTop(300, 34, 600)).toBe(300 - GRAPH_TOOLTIP_POINTER_GAP - 34);
  });

  it("flips below the pointer near the top edge", () => {
    const top = graphTooltipTop(20, 34, 600);
    expect(top).toBe(20 + GRAPH_TOOLTIP_CURSOR_GAP);
    expect(top).toBeGreaterThanOrEqual(GRAPH_TOOLTIP_EDGE_GAP);
  });

  it("never runs past the bottom edge once flipped", () => {
    const top = graphTooltipTop(30, 34, 70);
    expect(top + 34).toBeLessThanOrEqual(70 - GRAPH_TOOLTIP_EDGE_GAP);
  });

  it("starts a tooltip taller than the canvas at the top gap", () => {
    expect(graphTooltipTop(10, 200, 100)).toBe(GRAPH_TOOLTIP_EDGE_GAP);
  });
});

// It never covers the zoom controls at the canvas's top right.
describe("graphTooltipPosition", () => {
  // The zoom rail: right-3 top-3, 28 px wide, four buttons.
  const rail = { left: 1440 - 12 - 28, top: 12, width: 28, height: 124 };

  it("keeps the clamped position when the controls are not in the way", () => {
    expect(
      graphTooltipPosition({
        pointerX: 500, pointerY: 300, tooltipWidth: 200, tooltipHeight: 34,
        containerWidth: 1440, containerHeight: 600, avoid: rail,
      }),
    ).toEqual({ left: 400, top: 300 - GRAPH_TOOLTIP_POINTER_GAP - 34 });
  });

  it("shifts left of the controls near the top right corner", () => {
    const { left, top } = graphTooltipPosition({
      pointerX: 1380, pointerY: 90, tooltipWidth: 220, tooltipHeight: 34,
      containerWidth: 1440, containerHeight: 600, avoid: rail,
    });
    expect(left + 220).toBeLessThanOrEqual(rail.left - GRAPH_TOOLTIP_EDGE_GAP);
    expect(top).toBe(90 - GRAPH_TOOLTIP_POINTER_GAP - 34);
  });

  it("moves below the controls when the canvas is too narrow to pass them", () => {
    const narrowRail = { left: 300 - 12 - 28, top: 12, width: 28, height: 124 };
    const { top } = graphTooltipPosition({
      pointerX: 200, pointerY: 60, tooltipWidth: 280, tooltipHeight: 34,
      containerWidth: 300, containerHeight: 600, avoid: narrowRail,
    });
    expect(top).toBeGreaterThanOrEqual(narrowRail.top + narrowRail.height + GRAPH_TOOLTIP_EDGE_GAP);
  });

  // #12347 UX r3 B3: the tip on a hovered liaison covered its own type label.
  it("moves off the hovered edge's own label, nearest side first", () => {
    const label = { left: 565, top: 480, width: 22, height: 11 };
    const { left, top } = graphTooltipPosition({
      pointerX: 578, pointerY: 530, tooltipWidth: 264, tooltipHeight: 34,
      containerWidth: 1440, containerHeight: 900, keepClear: label,
    });
    const clear =
      left + 264 <= label.left - GRAPH_TOOLTIP_EDGE_GAP || left >= label.left + label.width + GRAPH_TOOLTIP_EDGE_GAP
      || top + 34 <= label.top - GRAPH_TOOLTIP_EDGE_GAP || top >= label.top + label.height + GRAPH_TOOLTIP_EDGE_GAP;
    expect(clear).toBe(true);
    // Above the label, the side nearest to the default place above the pointer.
    expect(top).toBe(label.top - GRAPH_TOOLTIP_EDGE_GAP - 34);
  });

  it("goes beside the label when neither above nor below fits", () => {
    const label = { left: 400, top: 30, width: 40, height: 40 };
    const { left } = graphTooltipPosition({
      pointerX: 420, pointerY: 50, tooltipWidth: 200, tooltipHeight: 34,
      containerWidth: 1000, containerHeight: 100, keepClear: label,
    });
    expect(left + 200 <= label.left - GRAPH_TOOLTIP_EDGE_GAP || left >= label.left + label.width + GRAPH_TOOLTIP_EDGE_GAP).toBe(true);
  });

  it("keeps the default place when it already clears the label", () => {
    expect(
      graphTooltipPosition({
        pointerX: 500, pointerY: 300, tooltipWidth: 200, tooltipHeight: 34,
        containerWidth: 1440, containerHeight: 600, keepClear: { left: 480, top: 320, width: 40, height: 11 },
      }),
    ).toEqual({ left: 400, top: 300 - GRAPH_TOOLTIP_POINTER_GAP - 34 });
  });

  it("clamps on both axes without controls", () => {
    expect(
      graphTooltipPosition({
        pointerX: 1430, pointerY: 10, tooltipWidth: 200, tooltipHeight: 34,
        containerWidth: 1440, containerHeight: 600,
      }),
    ).toEqual({ left: 1440 - 200 - GRAPH_TOOLTIP_EDGE_GAP, top: 10 + GRAPH_TOOLTIP_CURSOR_GAP });
  });
});
