/**
 * The view of the agent graph: zoom at the pointer and arrowhead sizes - vitest.
 */
import { describe, expect, it } from "vitest";

import { fitTransform } from "./agent-graph-tween";
import {
  ARROW_GAP,
  ARROW_MIN_PX,
  VIEW_ZOOM,
  arrowHead,
  framedPanY,
  revealShift,
  ringReach,
  viewHeight,
  viewOrigin,
  visibleViewHeight,
  wheelFactor,
  wheelZoom,
  type GraphView,
} from "./agent-graph-view";

describe("viewHeight", () => {
  it("shows as many units down as a taller box holds, at the scale of its width", () => {
    // 1400 units over 1000 px: 1.4 units per px; a 700 px tall box holds 980 units.
    expect(viewHeight(600, 1000, 700, 1400)).toBeCloseTo(980);
  });

  it("never shows fewer units than the canvas", () => {
    expect(viewHeight(600, 1000, 300, 1400)).toBe(600);
  });

  it("keeps the canvas for a box not measured yet", () => {
    expect(viewHeight(600, 0, 700, 1400)).toBe(600);
    expect(viewHeight(600, 1000, 0, 1400)).toBe(600);
    expect(viewHeight(600, Number.NaN, 700, 1400)).toBe(600);
  });
});

describe("viewOrigin", () => {
  // A box three times taller than the canvas: 1400 x 600 units over 1000 px
  // wide and 1260 px tall holds 1764 units down.
  const tall = viewHeight(600, 1000, 1260, 1400);

  it("anchors the canvas at the top of a view 3x taller, the extra height below", () => {
    expect(tall).toBeCloseTo(1764);
    const origin = viewOrigin({ zoom: 1, pan: { x: 0, y: 0 } }, 1400, 600);
    expect(origin).toEqual({ x: 0, y: 0 });
    // The whole canvas lies inside the first 600 units of the view.
    expect(origin.y + 600).toBeLessThan(origin.y + tall);
  });

  it("keeps the canvas centred on its own size when zoomed", () => {
    // Zoom 2: the view shows 700 x 300 canvas units around the canvas centre.
    expect(viewOrigin({ zoom: 2, pan: { x: 0, y: 0 } }, 1400, 600)).toEqual({ x: 350, y: 150 });
    // A pan moves the origin by pan / zoom units.
    expect(viewOrigin({ zoom: 2, pan: { x: 100, y: -60 } }, 1400, 600)).toEqual({ x: 300, y: 180 });
  });

  it("wheel zoom keeps the point under the pointer in a view taller than the canvas", () => {
    const start: GraphView = { zoom: 1, pan: { x: 0, y: 0 } };
    const at = (view: GraphView, fy: number) => viewOrigin(view, 1400, 600).y + fy * (tall / view.zoom);
    const next = wheelZoom(start, { fx: 0.5, fy: 0.8 }, 1.5, 1400, 600, tall);
    expect(at(next, 0.8)).toBeCloseTo(at(start, 0.8));
  });
});

describe("visibleViewHeight", () => {
  it("is the part of a tall view the window shows below the svg's top", () => {
    // 1000 px wide over 1400 units; svg top at 368 px in a 900 px window: 532 px, 744.8 units.
    expect(visibleViewHeight(2780, 600, 1000, 368, 900, 1400)).toBeCloseTo(744.8);
  });

  it("never exceeds the view nor falls under the canvas", () => {
    expect(visibleViewHeight(700, 600, 1000, 0, 2000, 1400)).toBe(700);
    expect(visibleViewHeight(2780, 600, 1000, 880, 900, 1400)).toBe(600);
  });

  it("keeps the view when nothing is measured", () => {
    expect(visibleViewHeight(2780, 600, 0, 368, 900, 1400)).toBe(2780);
    expect(visibleViewHeight(2780, 600, 1000, Number.NaN, 900, 1400)).toBe(2780);
  });
});

describe("framedPanY", () => {
  it("puts the fitted box inside the visible frame at the top of the view", () => {
    const canvasH = 600;
    const frameH = 744.8;
    const box = { x0: 100, y0: 50, x1: 1300, y1: 550 };
    const fit = fitTransform(box, 1400, frameH, 24, VIEW_ZOOM.min, VIEW_ZOOM.max);
    const view = { zoom: fit.zoom, pan: { x: fit.pan.x, y: framedPanY(fit.pan.y, fit.zoom, canvasH, frameH) } };
    const top = viewOrigin(view, 1400, canvasH).y;
    const shown = frameH / view.zoom;
    expect(box.y0).toBeGreaterThanOrEqual(top - 1e-6);
    expect(box.y1).toBeLessThanOrEqual(top + shown + 1e-6);
  });

  it("is the fit's own pan when the frame is the canvas", () => {
    expect(framedPanY(42, 1.7, 600, 600)).toBe(42);
  });
});

describe("revealShift", () => {
  // A 1000 px svg from x=0 drawing 1000 units (1 px per unit); the drawer
  // covers the screen from x=580 (a 1000 px window minus 420 px).
  const frame = { svgLeft: 0, svgWidth: 1000, coveredFrom: 580, viewX: 0, viewW: 1000 };

  it("leaves a node already clear of the drawer where it is", () => {
    expect(revealShift(300, 10, frame)).toBe(0);
  });

  it("centres a node drawn under the drawer in the visible part", () => {
    const shift = revealShift(800, 10, frame);
    expect(shift).toBe(800 - 290);
    // After the shift the node sits at the visible part's centre.
    expect(revealShift(800, 10, { ...frame, viewX: shift })).toBe(0);
  });

  it("moves a node half under the drawer edge", () => {
    expect(revealShift(570, 10, frame)).toBeGreaterThan(0);
  });

  it("works in drawing units at any zoom", () => {
    // 2 px per unit: the viewBox shows 500 units from x=100.
    const zoomed = { ...frame, viewX: 100, viewW: 500 };
    const shift = revealShift(450, 5, zoomed);
    // On screen at (450 - 100) * 2 = 700 px, centred at 290 px: 410 px, 205 units.
    expect(shift).toBe(205);
    expect(revealShift(450, 5, { ...zoomed, viewX: 100 + shift })).toBe(0);
  });

  it("does nothing when the drawer covers none of the svg or leaves no room", () => {
    expect(revealShift(800, 10, { ...frame, coveredFrom: 1200 })).toBe(0);
    expect(revealShift(800, 10, { ...frame, coveredFrom: 20 })).toBe(0);
    expect(revealShift(800, 10, { ...frame, svgWidth: 0 })).toBe(0);
  });

  it("moves a node off the left edge back into view", () => {
    expect(revealShift(-50, 10, frame)).toBeLessThan(0);
  });
});

const W = 1400;
const H = 600;

/** The drawing point under the canvas fraction (fx, fy) for a view, as the renderer's viewBox puts it. */
function under(view: GraphView, fx: number, fy: number) {
  const vbW = W / view.zoom, vbH = H / view.zoom;
  const vbX = (W - vbW) / 2 - view.pan.x / view.zoom;
  const vbY = (H - vbH) / 2 - view.pan.y / view.zoom;
  return { x: vbX + fx * vbW, y: vbY + fy * vbH };
}

describe("wheelZoom", () => {
  const start: GraphView = { zoom: 1, pan: { x: 0, y: 0 } };

  it.each([
    [0.5, 0.5],
    [0.1, 0.2],
    [0.9, 0.85],
  ])("keeps the point under the pointer at (%s, %s)", (fx, fy) => {
    const before = under(start, fx, fy);
    const zoomed = wheelZoom(start, { fx, fy }, 1.6, W, H);
    expect(zoomed.zoom).toBeCloseTo(1.6);
    const after = under(zoomed, fx, fy);
    expect(after.x).toBeCloseTo(before.x, 6);
    expect(after.y).toBeCloseTo(before.y, 6);
    // And back out, from a panned view.
    const out = wheelZoom(zoomed, { fx, fy }, 1 / 1.6, W, H);
    expect(under(out, fx, fy).x).toBeCloseTo(before.x, 6);
    expect(out.zoom).toBeCloseTo(1);
  });

  it("zooms at the centre without panning", () => {
    const zoomed = wheelZoom(start, { fx: 0.5, fy: 0.5 }, 2, W, H);
    expect(zoomed.pan.x).toBeCloseTo(0);
    expect(zoomed.pan.y).toBeCloseTo(0);
  });

  it("stays within the zoom bounds", () => {
    expect(wheelZoom(start, { fx: 0.3, fy: 0.3 }, 100, W, H).zoom).toBe(VIEW_ZOOM.max);
    expect(wheelZoom(start, { fx: 0.3, fy: 0.3 }, 0.001, W, H).zoom).toBe(VIEW_ZOOM.min);
    const atMax: GraphView = { zoom: VIEW_ZOOM.max, pan: { x: 10, y: 5 } };
    expect(wheelZoom(atMax, { fx: 0.3, fy: 0.3 }, 2, W, H)).toBe(atMax);
  });
});

describe("wheelFactor", () => {
  it("zooms in on a scroll up and out on a scroll down, as d3-zoom", () => {
    expect(wheelFactor({ deltaY: -100, deltaMode: 0, ctrlKey: false })).toBeCloseTo(2 ** 0.2);
    expect(wheelFactor({ deltaY: 100, deltaMode: 0, ctrlKey: false })).toBeCloseTo(2 ** -0.2);
  });

  it("weighs a line more than a pixel and a pinch ten times a scroll", () => {
    expect(wheelFactor({ deltaY: -3, deltaMode: 1, ctrlKey: false })).toBeCloseTo(2 ** 0.15);
    expect(wheelFactor({ deltaY: -10, deltaMode: 0, ctrlKey: true })).toBeCloseTo(2 ** 0.2);
  });
});

describe("arrowHead", () => {
  it.each([6, 8, 10, 12, 20])("is under half the radius of a %s-unit node, and readable", (r) => {
    const head = arrowHead(r);
    expect(head.length).toBeLessThanOrEqual(Math.max(3, r * 0.45) + 1e-9);
    expect(head.length).toBeGreaterThanOrEqual(3);
    expect(head.halfWidth * 2).toBeLessThan(head.length);
  });

  it("grows with the node, capped", () => {
    expect(arrowHead(10).length).toBeGreaterThan(arrowHead(7).length);
    expect(arrowHead(40).length).toBe(arrowHead(80).length);
  });

  it("is at least ARROW_MIN_PX on screen on a fitted map, never past the node's radius", () => {
    // A fitted map at 0.6 px per unit: 4.5 px is 7.5 units, under a 12-unit radius.
    expect(arrowHead(12, 0.6).length * 0.6).toBeCloseTo(ARROW_MIN_PX);
    // A 6-unit node at 0.3 px per unit: capped at its radius.
    expect(arrowHead(6, 0.3).length).toBe(6);
    // Zoomed in, the drawing-unit size wins and grows with the zoom.
    expect(arrowHead(10, 4).length).toBe(arrowHead(10).length);
  });

  it("leaves a small gap off the ring", () => {
    expect(ARROW_GAP).toBeGreaterThan(0);
    expect(ARROW_GAP).toBeLessThan(arrowHead(6).length);
  });
});

describe("ringReach", () => {
  const plain = { isRunning: false, isEditable: true, isGeneralAssistant: false, isCompanyManaged: false, isGhost: false };

  it("keeps a plain node's clearance to its disc stroke", () => {
    expect(ringReach(plain)).toBe(0.8);
    expect(ringReach({ ...plain, isEditable: false, isGeneralAssistant: true })).toBe(0.8);
    // A ghost draws no read-only ring of its own.
    expect(ringReach({ ...plain, isEditable: false, isGhost: true })).toBe(0.8);
  });

  it("reaches the dashed ring of a read-only or company-managed node", () => {
    expect(ringReach({ ...plain, isEditable: false })).toBe(3.3);
    expect(ringReach({ ...plain, isCompanyManaged: true })).toBe(3.3);
  });

  it("reaches the running spinner, the widest ring", () => {
    expect(ringReach({ ...plain, isRunning: true })).toBe(4.6);
    expect(ringReach({ ...plain, isRunning: true, isEditable: false })).toBe(4.6);
  });
});
