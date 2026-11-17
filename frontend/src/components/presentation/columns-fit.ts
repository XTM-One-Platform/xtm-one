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
 * Four columns side by side whenever their content fits.
 *
 * A `columns` block of four drew them 2 x 2 on every slide: the stylesheet
 * mapped three columns to three tracks and left four on the two-track
 * default. Four tracks are now the rule, and the block wraps to 2 x 2 only
 * when its widest column's content cannot fit one of them
 * (`columns-fit-math.ts`) - measured, deterministic, like `fit.ts`.
 *
 * A column's minimum width is its min-content width, read by sizing the
 * column `min-content` for one synchronous layout and putting it back before
 * anything paints. The room is the block's width AS IF the body were not
 * shrunk: `fit.ts` widens a shrunk body by `1 / k`, and a decision read off
 * the widened width would flip the wrap, the height, the fit and the wrap
 * again. A wrapped block carries `data-wrapped` (the columns a row holds),
 * `data-column-need` and `data-column-room` (canvas px): the stylesheet reads
 * the first, the layout audit reports all three.
 */
import { type CSSProperties, type RefObject, useLayoutEffect } from "react";

import { WRAPPING_COLUMN_COUNT, columnRowCount, columnTrack, columnsPerRow } from "./columns-fit-math";
import { currentFit } from "./fit";

/**
 * The attributes of a columns form whose blocks line up as rows (`equal_rows`)
 * - a slide's columns or a `columns` block: the rows are subgrid tracks
 * the stylesheet sizes from `--deck-column-rows`, a runtime count like the
 * logo geometry. None at all when the switch is off, so the DOM is as before.
 */
export function equalRowsAttributes(
  enabled: boolean | undefined,
  columns: readonly (readonly unknown[])[],
): { "data-equal-rows"?: "true"; style?: CSSProperties } {
  if (!enabled) return {};
  return {
    "data-equal-rows": "true",
    style: { "--deck-column-rows": String(columnRowCount(columns)) } as CSSProperties,
  };
}

/** The min-content width of `el`, in layout px. */
function minContentWidth(el: HTMLElement): number {
  const previous = el.style.width;
  el.style.width = "min-content";
  const width = el.offsetWidth;
  el.style.width = previous;
  return width;
}

/** Remove what a pass wrote: the block draws its columns side by side. */
function clearColumnsFit(el: HTMLElement): void {
  delete el.dataset.wrapped;
  delete el.dataset.columnNeed;
  delete el.dataset.columnRoom;
}

/** One pass over a `columns` block: measure, then wrap or unwrap it. Writes only what changed. */
function fitColumns(el: HTMLElement): void {
  const count = el.children.length;
  // A hidden slide (reveal keeps far-away sections at display: none)
  // measures zero; leave it alone until it is shown.
  if (count !== WRAPPING_COLUMN_COUNT || el.clientWidth <= 0) {
    if (count !== WRAPPING_COLUMN_COUNT) clearColumnsFit(el);
    return;
  }
  let need = 0;
  for (const column of el.children) {
    if (column instanceof HTMLElement) need = Math.max(need, minContentWidth(column));
  }
  const host = el.closest<HTMLElement>(".deck-fit");
  const width = el.clientWidth * (host ? currentFit(host) : 1);
  const gap = Number.parseFloat(getComputedStyle(el).columnGap) || 0;
  const perRow = columnsPerRow(count, need, width, gap);
  if (perRow >= count) {
    clearColumnsFit(el);
    return;
  }
  const values = {
    wrapped: String(perRow),
    columnNeed: String(Math.ceil(need)),
    columnRoom: String(Math.floor(columnTrack(width, count, gap))),
  };
  for (const [key, value] of Object.entries(values)) {
    if (el.dataset[key] !== value) el.dataset[key] = value;
  }
}

/**
 * Keep a `columns` block's wrap decision current for as long as it is
 * mounted: before the first paint, then whenever the block or one of its
 * blocks changes size - a logo loading, fonts landing, a hidden slide being
 * shown, an edit. `key` re-arms the observers when the blocks are replaced.
 * Writes are deferred to an animation frame so the observer's own delivery
 * never loops; `deckSettled()` (`ready.ts`) waits that frame out.
 */
export function useColumnsFit(ref: RefObject<HTMLElement | null>, key: unknown): void {
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    fitColumns(el);
    if (el.children.length !== WRAPPING_COLUMN_COUNT || typeof ResizeObserver === "undefined") return undefined;
    let frame = 0;
    const schedule = () => {
      if (frame) return;
      frame = window.requestAnimationFrame(() => {
        frame = 0;
        fitColumns(el);
      });
    };
    const observer = new ResizeObserver(schedule);
    observer.observe(el);
    for (const block of el.querySelectorAll(":scope > .deck-column > .deck-blocks > *")) observer.observe(block);
    return () => {
      observer.disconnect();
      if (frame) window.cancelAnimationFrame(frame);
    };
    // `key` is the content the block renders: new blocks need observing.
  }, [ref, key]);
}
