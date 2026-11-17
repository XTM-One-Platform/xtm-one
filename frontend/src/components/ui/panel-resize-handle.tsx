// SPDX-License-Identifier: Apache-2.0

/**
 * The drag handle of a panel sized by `useResizableWidth`: it straddles the
 * panel's edge (easy to grab from either side), lights up on hover, focus and
 * drag, and is left out when the viewport leaves no room between the bounds
 * (`canResize`) - a divider that cannot move is a promise the screen cannot
 * keep. `left` sits on the start edge of a right-hand panel, `top` on the top
 * edge of a bottom panel; the panel itself is `relative`.
 */
import { Tip } from "@/components/ui/tooltip";
import type { ResizableWidth } from "@/hooks/useResizableWidth";
import { cn } from "@/lib/utils";

export function PanelResizeHandle({
  panel,
  edge,
  label,
  tip,
  testId,
}: {
  panel: ResizableWidth;
  edge: "left" | "top";
  label: string;
  tip: string;
  testId?: string;
}) {
  if (!panel.canResize) return null;
  const side = edge === "left";
  return (
    <Tip content={tip} side={side ? "left" : "top"}>
      <div
        {...panel.handleProps}
        aria-label={label}
        data-testid={testId}
        className={cn(
          "group absolute z-20 touch-none outline-none",
          side ? "inset-y-0 -left-1 w-2 cursor-col-resize" : "inset-x-0 -top-1 h-2 cursor-row-resize",
        )}
      >
        <span
          aria-hidden="true"
          className={cn(
            "absolute transition-[width,height,background-color,box-shadow] duration-150",
            side ? "inset-y-0 left-1/2 w-px -translate-x-1/2" : "inset-x-0 top-1/2 h-px -translate-y-1/2",
            panel.isResizing
              ? cn(
                  "bg-accent shadow-[0_0_12px_color-mix(in_srgb,_var(--color-accent)_calc(0.8_*_100%),_transparent)]",
                  side ? "w-0.5" : "h-0.5",
                )
              : cn(
                  "bg-transparent group-hover:bg-accent/70 group-focus-visible:bg-accent group-focus-visible:shadow-[0_0_8px_color-mix(in_srgb,_var(--color-accent)_calc(0.6_*_100%),_transparent)]",
                  side ? "group-hover:w-0.5 group-focus-visible:w-0.5" : "group-hover:h-0.5 group-focus-visible:h-0.5",
                ),
          )}
        />
      </div>
    </Tip>
  );
}
