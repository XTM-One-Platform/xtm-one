// SPDX-License-Identifier: Apache-2.0

/**
 * Where the shared info popover (components/ui/info-popover.tsx) goes: pure,
 * so `yarn test:unit` covers the flip and the caps (#9836 UX r1 B5).
 */

/** Gap between the trigger and the popover, and the popover and the viewport edge, in px. */
const GAP = 6;
const EDGE = 8;

export interface InfoPopoverPlacement {
  top: number;
  left: number;
  width: number;
  maxHeight: number;
}

/**
 * Under the trigger, or above it when its height would pass the viewport's
 * bottom and there is more room above; aligned on the trigger's start or end
 * edge and kept inside the viewport; its height capped to the room it has.
 */
export function placeInfoPopover(
  trigger: { top: number; bottom: number; left: number; right: number },
  popoverHeight: number,
  width: number,
  viewport: { width: number; height: number },
  align: "start" | "end",
): InfoPopoverPlacement {
  const w = Math.min(width, viewport.width - 2 * EDGE);
  const wanted = align === "end" ? trigger.right - w : trigger.left;
  const left = Math.min(Math.max(EDGE, wanted), viewport.width - w - EDGE);
  const below = viewport.height - trigger.bottom - GAP - EDGE;
  const above = trigger.top - GAP - EDGE;
  if (popoverHeight > below && above > below) {
    const maxHeight = Math.max(0, above);
    const height = Math.min(popoverHeight, maxHeight);
    return { top: trigger.top - GAP - height, left, width: w, maxHeight };
  }
  return { top: trigger.bottom + GAP, left, width: w, maxHeight: Math.max(0, below) };
}
