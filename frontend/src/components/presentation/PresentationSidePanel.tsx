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
 * The viewer's side-panel slot: editor, comments, history and the
 * layout check share ONE width, which the reader drags from the panel's LEFT edge — up to a third of
 * the screen, so a long review thread or a wide editor form reads without
 * wrapping every other word, and never past it, so the deck keeps the room.
 *
 * Same hook as the studio rail (`useResizableWidth`), mirrored: the handle
 * sits on the start edge, so dragging LEFT widens. The preference is
 * remembered per browser and re-clamped against the viewport on every render
 * (a width saved on a wide monitor cannot push the stage off a laptop). The
 * deck's keys are off inside the slot — the handle takes the arrow keys to
 * nudge the width, and every panel here is typed into.
 */
import type { ReactNode } from "react";

import { Tip } from "@/components/ui/tooltip";
import { useResizableWidth } from "@/hooks/useResizableWidth";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

import { DECK_KEYS_OFF_ATTR } from "./deck-keys";

/** The width the panels had before they could be resized. */
const SIDE_PANEL_DEFAULT_WIDTH = 400;
/** Narrower and the composer's chips wrap under each other. */
const SIDE_PANEL_MIN_WIDTH = 320;
/** The ceiling: a third of the viewport, the deck keeps the rest. */
const SIDE_PANEL_MAX_FRACTION = 1 / 3;
const SIDE_PANEL_WIDTH_KEY = "xtm-presentation-panel-width";

function sidePanelMaxWidth(viewport: number): number {
  return Math.floor(viewport * SIDE_PANEL_MAX_FRACTION);
}

export function PresentationSidePanel({ children, className }: { children: ReactNode; className?: string }) {
  const { t_i18n } = useFormatter();
  const panel = useResizableWidth({
    defaultWidth: SIDE_PANEL_DEFAULT_WIDTH,
    minWidth: SIDE_PANEL_MIN_WIDTH,
    maxWidth: sidePanelMaxWidth,
    storageKey: SIDE_PANEL_WIDTH_KEY,
    edge: "start",
  });

  return (
    <div
      {...DECK_KEYS_OFF_ATTR}
      className={cn("relative flex h-full shrink-0 print:hidden", panel.isResizing && "select-none", className)}
      style={{ width: panel.width }}
      data-testid="presentation.side-panel"
    >
      {/* The divider: drag it to widen the panel (to a third of the screen),
          arrows nudge it, double-click resets. Straddles the border so it is
          easy to grab from either side. Out when the viewport leaves no room
          between the bounds (`canResize`, the studio rail's rule): a focusable
          divider that cannot move, with a "drag to resize" tip, is a promise
          the screen cannot keep. */}
      {panel.canResize && (
        <Tip content={t_i18n("Drag to resize the panel - double-click to reset")} side="left">
          <div
            {...panel.handleProps}
            aria-label={t_i18n("Resize the side panel")}
            data-testid="presentation.panel.resize"
            className="group absolute inset-y-0 -left-1 z-20 w-2 cursor-col-resize touch-none outline-none"
          >
            <span
              aria-hidden="true"
              className={cn(
                "absolute inset-y-0 left-1/2 w-px -translate-x-1/2 transition-[width,background-color,box-shadow] duration-150",
                panel.isResizing
                  ? "w-0.5 bg-accent shadow-[0_0_12px_color-mix(in_srgb,_var(--color-accent)_calc(0.8_*_100%),_transparent)]"
                  : "bg-transparent group-hover:w-0.5 group-hover:bg-accent/70 group-focus-visible:w-0.5 group-focus-visible:bg-accent group-focus-visible:shadow-[0_0_8px_color-mix(in_srgb,_var(--color-accent)_calc(0.6_*_100%),_transparent)]",
              )}
            />
          </div>
        </Tip>
      )}
      {children}
    </div>
  );
}
