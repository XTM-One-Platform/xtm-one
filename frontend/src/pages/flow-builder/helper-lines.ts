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
 * Alignment helper lines.
 *
 * While a single node drags, its edges and centers are compared with every
 * other node's; within the snap distance the drag position is snapped and the
 * matching guide line is returned for the canvas to draw. The classic React
 * Flow helper-lines recipe, kept dependency-free.
 */

import type { NodeChange } from "@xyflow/react";
import type { CanvasNode } from "./graph-adapter";

const SNAP_DISTANCE = 6;

export interface HelperLinesResult {
  /** Flow-space y of the horizontal guide, when one is active. */
  horizontal?: number;
  /** Flow-space x of the vertical guide, when one is active. */
  vertical?: number;
  snapX?: number;
  snapY?: number;
}

interface Bounds {
  left: number;
  right: number;
  top: number;
  bottom: number;
  centerX: number;
  centerY: number;
}

function boundsOf(x: number, y: number, width: number, height: number): Bounds {
  return {
    left: x,
    right: x + width,
    top: y,
    bottom: y + height,
    centerX: x + width / 2,
    centerY: y + height / 2,
  };
}

/** Compute guides + snapped position for a drag change of one node. */
export function getHelperLines(
  change: Extract<NodeChange<CanvasNode>, { type: "position" }>,
  nodes: CanvasNode[],
): HelperLinesResult {
  if (!change.position) return {};
  const dragged = nodes.find((n) => n.id === change.id);
  const width = dragged?.measured?.width ?? 240;
  const height = dragged?.measured?.height ?? 80;
  const a = boundsOf(change.position.x, change.position.y, width, height);

  const result: HelperLinesResult = {};
  let bestV = SNAP_DISTANCE;
  let bestH = SNAP_DISTANCE;

  for (const other of nodes) {
    if (other.id === change.id) continue;
    const w = other.measured?.width ?? 240;
    const h = other.measured?.height ?? 80;
    const b = boundsOf(other.position.x, other.position.y, w, h);

    // Vertical guides: left/right/center x alignments.
    const vCandidates: Array<[number, number]> = [
      [Math.abs(a.left - b.left), b.left],
      [Math.abs(a.right - b.right), b.right - width],
      [Math.abs(a.left - b.right), b.right],
      [Math.abs(a.right - b.left), b.left - width],
      [Math.abs(a.centerX - b.centerX), b.centerX - width / 2],
    ];
    for (const [distance, snappedX] of vCandidates) {
      if (distance < bestV) {
        bestV = distance;
        result.snapX = snappedX;
        const guides = [b.left, b.right, b.centerX];
        // The guide is whichever of the other node's x-lines we snapped to.
        result.vertical = guides.reduce((best, g) =>
          Math.abs(g - (snappedX + (g === b.centerX ? width / 2 : g === b.right ? width : 0))) <
          Math.abs(best - snappedX)
            ? g
            : best,
        );
      }
    }

    // Horizontal guides: top/bottom/center y alignments.
    const hCandidates: Array<[number, number]> = [
      [Math.abs(a.top - b.top), b.top],
      [Math.abs(a.bottom - b.bottom), b.bottom - height],
      [Math.abs(a.top - b.bottom), b.bottom],
      [Math.abs(a.bottom - b.top), b.top - height],
      [Math.abs(a.centerY - b.centerY), b.centerY - height / 2],
    ];
    for (const [distance, snappedY] of hCandidates) {
      if (distance < bestH) {
        bestH = distance;
        result.snapY = snappedY;
        result.horizontal = b.centerY;
      }
    }
  }

  // Guide positions: recompute from the SNAPPED dragged bounds so the line
  // sits exactly on the shared alignment, whatever it matched.
  if (result.snapX !== undefined) {
    result.vertical = closestAlignment(
      boundsOf(result.snapX, change.position.y, width, height),
      nodes.filter((n) => n.id !== change.id),
      "x",
    );
  }
  if (result.snapY !== undefined) {
    result.horizontal = closestAlignment(
      boundsOf(change.position.x, result.snapY, width, height),
      nodes.filter((n) => n.id !== change.id),
      "y",
    );
  }
  return result;
}

/** The other-node alignment line nearest to any of the dragged bounds. */
function closestAlignment(
  a: Bounds,
  others: CanvasNode[],
  axis: "x" | "y",
): number | undefined {
  let best: number | undefined;
  let bestDistance = 1;
  const mine = axis === "x" ? [a.left, a.right, a.centerX] : [a.top, a.bottom, a.centerY];
  for (const other of others) {
    const w = other.measured?.width ?? 240;
    const h = other.measured?.height ?? 80;
    const b = boundsOf(other.position.x, other.position.y, w, h);
    const theirs = axis === "x" ? [b.left, b.right, b.centerX] : [b.top, b.bottom, b.centerY];
    for (const m of mine) {
      for (const t of theirs) {
        const d = Math.abs(m - t);
        if (d < bestDistance) {
          bestDistance = d;
          best = t;
        }
      }
    }
  }
  return best;
}
