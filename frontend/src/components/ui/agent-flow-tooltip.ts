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

/** Gap kept between the agent graph's hover tooltip and the canvas edge, in px. */
export const GRAPH_TOOLTIP_EDGE_GAP = 8;
/** Gap between the pointer and the tooltip's bottom edge when it sits above, in px. */
export const GRAPH_TOOLTIP_POINTER_GAP = 8;
/** Gap between the pointer and the tooltip's top edge when it flips below (clears the cursor), in px. */
export const GRAPH_TOOLTIP_CURSOR_GAP = 20;

/**
 * Left offset, in px from the canvas's left edge, of the agent graph's hover
 * tooltip. The tooltip is centred on the pointer, then clamped so it
 * never runs past either edge of the canvas: near the right edge it shifts
 * left, near the left edge it shifts right. A tooltip wider than the canvas
 * starts at the left gap.
 */
export function graphTooltipLeft(
  pointerX: number,
  tooltipWidth: number,
  containerWidth: number,
  gap: number = GRAPH_TOOLTIP_EDGE_GAP,
): number {
  const centred = pointerX - tooltipWidth / 2;
  const max = containerWidth - tooltipWidth - gap;
  if (max < gap) return gap;
  return Math.min(Math.max(centred, gap), max);
}

/**
 * Top offset, in px from the canvas's top edge, of the hover tooltip.
 * It sits above the pointer; when that would cross the canvas's top edge it
 * flips below the pointer, clamped so it never runs past the bottom edge. A
 * tooltip taller than the canvas starts at the top gap.
 */
export function graphTooltipTop(
  pointerY: number,
  tooltipHeight: number,
  containerHeight: number,
  gap: number = GRAPH_TOOLTIP_EDGE_GAP,
): number {
  const above = pointerY - GRAPH_TOOLTIP_POINTER_GAP - tooltipHeight;
  if (above >= gap) return above;
  const max = containerHeight - tooltipHeight - gap;
  if (max < gap) return gap;
  return Math.min(Math.max(pointerY + GRAPH_TOOLTIP_CURSOR_GAP, gap), max);
}

/** A box the tooltip must keep clear of, in px from the canvas's top left. */
export interface GraphTooltipObstacle {
  left: number;
  top: number;
  width: number;
  height: number;
}

export interface GraphTooltipPlacement {
  pointerX: number;
  pointerY: number;
  tooltipWidth: number;
  tooltipHeight: number;
  containerWidth: number;
  containerHeight: number;
  /** The canvas controls (the zoom rail at the top right) the tooltip never covers. */
  avoid?: GraphTooltipObstacle | null;
  /**
   * What the hovered element draws itself (an edge's type label): the tooltip
   * moves above or below it, else beside it, and never covers it.
   */
  keepClear?: GraphTooltipObstacle | null;
  gap?: number;
}

/**
 * The top or left that takes a box of the given size clear of `box`, nearest
 * to `from` first, inside the canvas and off the pointer; null when no side fits.
 */
function clearOf(
  from: number,
  size: number,
  boxStart: number,
  boxSize: number,
  room: number,
  gap: number,
  pointer: number,
): number | null {
  const before = boxStart - gap - size;
  const after = boxStart + boxSize + gap;
  const offPointer = (v: number) => pointer < v - GRAPH_TOOLTIP_POINTER_GAP || pointer > v + size + GRAPH_TOOLTIP_POINTER_GAP;
  const fits = (v: number) => v >= gap && v + size <= room - gap && offPointer(v);
  const sides = [before, after].filter(fits).sort((a, b) => Math.abs(a - from) - Math.abs(b - from));
  return sides.length > 0 ? sides[0] : null;
}

function overlaps(left: number, top: number, w: number, h: number, box: GraphTooltipObstacle, gap: number): boolean {
  return (
    left < box.left + box.width + gap &&
    left + w > box.left - gap &&
    top < box.top + box.height + gap &&
    top + h > box.top - gap
  );
}

/**
 * Position of the hover tooltip inside the canvas: clamped on
 * both axes, clear of what the hovered element draws (`keepClear`: above or
 * below it, else beside it), and clear of the canvas controls. When the
 * clamped box would cover the controls it shifts left of them; when the
 * canvas is too narrow for that it moves below them instead.
 */
export function graphTooltipPosition({
  pointerX,
  pointerY,
  tooltipWidth,
  tooltipHeight,
  containerWidth,
  containerHeight,
  avoid,
  keepClear,
  gap = GRAPH_TOOLTIP_EDGE_GAP,
}: GraphTooltipPlacement): { left: number; top: number } {
  let left = graphTooltipLeft(pointerX, tooltipWidth, containerWidth, gap);
  let top = graphTooltipTop(pointerY, tooltipHeight, containerHeight, gap);
  if (keepClear && overlaps(left, top, tooltipWidth, tooltipHeight, keepClear, gap)) {
    const clearTop = clearOf(top, tooltipHeight, keepClear.top, keepClear.height, containerHeight, gap, pointerY);
    if (clearTop !== null) {
      top = clearTop;
    } else {
      const clearLeft = clearOf(left, tooltipWidth, keepClear.left, keepClear.width, containerWidth, gap, pointerX);
      if (clearLeft !== null) left = clearLeft;
    }
  }
  if (avoid && overlaps(left, top, tooltipWidth, tooltipHeight, avoid, gap)) {
    const leftOfControls = avoid.left - gap - tooltipWidth;
    if (leftOfControls >= gap) {
      left = Math.min(left, leftOfControls);
    } else {
      top = Math.max(top, avoid.top + avoid.height + gap);
    }
  }
  return { left, top };
}
