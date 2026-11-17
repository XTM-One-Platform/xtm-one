/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The view of the agent graph (Agentic Flow map, fleet views): zoom at the
 * pointer and arrowhead sizes. Pure functions, so the renderer stays
 * a drawing and the arithmetic is tested on its own.
 */

/** Bounds of the map's zoom. */
export const VIEW_ZOOM = { min: 0.3, max: 5 } as const;

export interface GraphView {
  /** Scale of the drawing (1: the layout fitted into the canvas). */
  zoom: number;
  /** Offset of the drawing, in canvas units at zoom 1. */
  pan: { x: number; y: number };
}

/** The pointer on the canvas, as fractions of its width and height (0 to 1). */
export interface ViewAnchor {
  fx: number;
  fy: number;
}

/**
 * The zoom factor of one wheel event, as d3-zoom computes it (the OpenCTI
 * graph's behaviour): a line or a page of scroll counts more than a pixel, and
 * a trackpad pinch (a wheel event with ctrlKey) ten times a scroll.
 */
export function wheelFactor(e: Pick<WheelEvent, "deltaY" | "deltaMode" | "ctrlKey">): number {
  const perUnit = e.deltaMode === 1 ? 0.05 : e.deltaMode ? 1 : 0.002;
  return 2 ** (-e.deltaY * perUnit * (e.ctrlKey ? 10 : 1));
}

/**
 * The view after zooming `view` by `factor` at `anchor`: the point of the
 * drawing under the pointer stays under it, the zoom stays within VIEW_ZOOM.
 * The canvas is `width` x `height` drawing units and the view's origin is
 * `viewOrigin`; the view shows `viewH` units down (`viewHeight`, the canvas
 * height when omitted), the extra height below the canvas.
 */
export function wheelZoom(
  view: GraphView,
  anchor: ViewAnchor,
  factor: number,
  width: number,
  height: number,
  viewH: number = height,
): GraphView {
  const z = view.zoom;
  const next = Math.min(VIEW_ZOOM.max, Math.max(VIEW_ZOOM.min, z * factor));
  if (next === z || !Number.isFinite(next)) return view;
  const axis = (size: number, span: number, pan: number, f: number) => {
    const origin = (size - size / z) / 2 - pan / z;
    const under = origin + f * (span / z);
    const nextOrigin = under - f * (span / next);
    return next * ((size - size / next) / 2 - nextOrigin);
  };
  return {
    zoom: next,
    pan: { x: axis(width, width, view.pan.x, anchor.fx), y: axis(height, viewH, view.pan.y, anchor.fy) },
  };
}

/**
 * Top-left corner of the view, in drawing units, for a canvas `width` x
 * `canvasH`: zoom keeps the canvas centred on its own size, so at zoom 1 and
 * no pan the canvas sits at the top of the view, and a view taller than the
 * canvas (`viewHeight`) shows its extra height below the drawing, never above.
 */
export function viewOrigin(view: GraphView, width: number, canvasH: number): { x: number; y: number } {
  const z = view.zoom;
  return {
    x: (width - width / z) / 2 - view.pan.x / z,
    y: (canvasH - canvasH / z) / 2 - view.pan.y / z,
  };
}

/**
 * The drawing units of a view `viewH` tall that the window shows: the svg is
 * `widthPx` wide over `width` units, its top `svgTopPx` from the top of a
 * window `windowPx` tall. Never more than the view, never less than the
 * canvas (a graph scrolled away still frames at the canvas size).
 */
export function visibleViewHeight(
  viewH: number,
  canvasH: number,
  widthPx: number,
  svgTopPx: number,
  windowPx: number,
  width: number,
): number {
  if (!(widthPx > 0) || !Number.isFinite(svgTopPx) || !(windowPx > 0)) return viewH;
  const shown = ((windowPx - Math.max(0, svgTopPx)) * width) / widthPx;
  return Math.min(viewH, Math.max(canvasH, shown));
}

/**
 * The vertical pan of the renderer (`viewOrigin`) that puts the view where
 * `fitTransform` framed it in a frame `frameH` units tall from the view's top:
 * `fitTransform` centres its zoom on the frame, the renderer on the canvas.
 */
export function framedPanY(fitPanY: number, zoom: number, canvasH: number, frameH: number): number {
  return fitPanY + ((canvasH - frameH) * (zoom - 1)) / 2;
}

/**
 * The height of the view (drawing units) of a graph drawn `widthPx` x
 * `heightPx` on screen over a canvas `width` units wide and `canvasH` tall,
 * with no distortion: as many units down as the box holds at the scale of its
 * width, never fewer than the canvas. An unmeasured box keeps the canvas.
 */
export function viewHeight(canvasH: number, widthPx: number, heightPx: number, width: number): number {
  if (!(widthPx > 0) || !(heightPx > 0) || !Number.isFinite(widthPx) || !Number.isFinite(heightPx)) return canvasH;
  return Math.max(canvasH, (heightPx * width) / widthPx);
}

/**
 * Width of the agent definition drawer (`AgentFlowDrawer`, `w-[420px]`), on
 * the right edge of the window over the graph.
 */
export const AGENT_DRAWER_PX = 420;

/** Where the graph and an overlay sit on the screen, in CSS px. */
export interface RevealFrame {
  /** The svg's left edge and drawn width on the screen. */
  svgLeft: number;
  svgWidth: number;
  /** The screen x where the overlay starts (it covers everything to its right). */
  coveredFrom: number;
  /** The viewBox's left edge and width, in drawing units. */
  viewX: number;
  viewW: number;
}

/**
 * How far the viewBox moves right, in drawing units, so a node at `x` with
 * radius `r` stays in the part of the svg an overlay on the right (the agent
 * drawer) leaves visible. A node already clear of the overlay by
 * `marginPx` does not move (0); a hidden one is centred in the visible part.
 * Nothing moves when the overlay leaves no room or covers none of the svg.
 */
export function revealShift(x: number, r: number, frame: RevealFrame, marginPx = 24): number {
  const { svgLeft, svgWidth, coveredFrom, viewX, viewW } = frame;
  if (svgWidth <= 0 || viewW <= 0) return 0;
  const visibleRight = Math.min(svgLeft + svgWidth, coveredFrom);
  if (visibleRight >= svgLeft + svgWidth) return 0;
  const pxPerUnit = svgWidth / viewW;
  const room = visibleRight - svgLeft;
  if (room < 2 * (r * pxPerUnit + marginPx)) return 0;
  const screenX = svgLeft + (x - viewX) * pxPerUnit;
  const reach = r * pxPerUnit + marginPx;
  if (screenX + reach <= visibleRight && screenX - reach >= svgLeft) return 0;
  return (screenX - (svgLeft + room / 2)) / pxPerUnit;
}

/** Bounds of an arrowhead's length, in drawing units. */
const ARROW_LENGTH = { min: 3, max: 5.5, share: 0.45 } as const;

export interface ArrowHead {
  /** Length from base to tip. */
  length: number;
  /** Half the base's width. */
  halfWidth: number;
}

/** Shortest arrowhead on screen, in px, so the direction stays readable on a fitted map. */
export const ARROW_MIN_PX = 4.5;

/**
 * The arrowhead of an edge ending on a node of radius `r` (drawing units):
 * under half the radius, so the tip marks the direction and never outweighs
 * the node, as on the OpenCTI graph. Drawn in drawing units, it scales with
 * the zoom like the node it points to. With `pxPerUnit` (the screen px of a
 * drawing unit), it is never shorter than ARROW_MIN_PX on screen nor longer
 * than the node's radius, half the glyph's diameter.
 */
export function arrowHead(r: number, pxPerUnit?: number): ArrowHead {
  let length = Math.min(ARROW_LENGTH.max, Math.max(ARROW_LENGTH.min, r * ARROW_LENGTH.share));
  if (pxPerUnit && pxPerUnit > 0) length = Math.min(Math.max(r, ARROW_LENGTH.min), Math.max(length, ARROW_MIN_PX / pxPerUnit));
  return { length, halfWidth: length * 0.35 };
}

/** Gap between an arrow's tip and the ring of the node it points to, in drawing units. */
export const ARROW_GAP = 2;

/** What a node draws around its disc at rest. */
export interface NodeRings {
  isRunning: boolean;
  isEditable: boolean;
  isGeneralAssistant: boolean;
  isCompanyManaged: boolean;
  isGhost: boolean;
}

/**
 * How far a node's outermost drawn ring at rest reaches past its radius, in
 * drawing units, half the stroke included: the running spinner
 * (r + 4, stroke 1.2), the read-only or company-managed dashed ring (r + 3,
 * stroke 0.5 at most), or only the disc's own stroke. A name at rest keeps
 * its clearance from this reach, so a plain node does not hide its
 * neighbours' names for a ring it never draws.
 */
export function ringReach(node: NodeRings): number {
  if (node.isRunning) return 4.6;
  const isReadOnly = !node.isEditable && !node.isGeneralAssistant;
  if ((isReadOnly && !node.isGhost) || (node.isCompanyManaged && !isReadOnly)) return 3.3;
  return 0.8;
}
