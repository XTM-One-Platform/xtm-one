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
 * A horizontal process flow's titles never break inside a word.
 *
 * A step's title wraps between words only (`overflow-wrap: normal`,
 * `hyphens: none`, `presentation.css`). When a title's longest word is wider
 * than the step's equal share of the flow, the flow's titles step down ONE
 * type size - all of them, so the steps keep one voice - and when the smaller
 * size is what makes every word fit, that is all that happens. When even the
 * smaller size does not fit the word, the titles keep their size and the step
 * widens to the word instead: the stylesheet lets a horizontal step be no
 * narrower than its content (`min-width: auto`, a flex item's min-content), so
 * its siblings give. Narrow steps still wrap a title between its words.
 *
 * Measured, not guessed, like `profiles-fit.ts` and `columns-fit.ts`: after
 * every layout the flow is read at the steps' EQUAL shares (`data-measuring`
 * pins their minimum width to 0 for the synchronous reads, so no step has
 * widened yet), at the base size and then one step down, and
 * `data-title-fit="tight"` is written only when the smaller size is what fits.
 * `scrollWidth` / `clientWidth` are layout metrics, untouched by the canvas
 * transform. A vertical flow sizes its title column to its widest title
 * and never needs the pass.
 */
import { type RefObject, useLayoutEffect } from "react";

/** The class of a step's title, the run the pass measures. */
const FLOW_TITLE_CLASS = "deck-flow-title";

/** Whether a title under `root` is wider than its box: a word of it does not fit. */
function titleOverflows(root: HTMLElement): boolean {
  for (const title of root.querySelectorAll<HTMLElement>(`.${FLOW_TITLE_CLASS}`)) {
    if (title.scrollWidth > title.clientWidth + 1) return true;
  }
  return false;
}

/** One pass over a flow: measure at the equal shares, then write the one size that fits. */
function fitFlowTitles(root: HTMLElement): void {
  if (root.dataset.direction === "vertical") {
    delete root.dataset.titleFit;
    return;
  }
  // A hidden slide (reveal keeps far-away sections at display: none)
  // measures zero; leave it alone until it is shown.
  if (root.clientWidth <= 0) return;
  root.dataset.measuring = "true";
  delete root.dataset.titleFit;
  if (titleOverflows(root)) {
    root.dataset.titleFit = "tight";
    // One step down is not what fits the word: the step widens to it at the base size.
    if (titleOverflows(root)) delete root.dataset.titleFit;
  }
  delete root.dataset.measuring;
}

/**
 * Keep a flow's title size current for as long as it is mounted: before the
 * first paint, once the deck fonts land (a glyph-width change moves no box a
 * resize observer watches), then whenever the flow changes size - a hidden
 * slide being shown, a column wrapping. `key` re-arms when the steps change.
 * Writes are deferred to an animation frame so the observer's own delivery
 * never loops; `deckSettled()` (`ready.ts`) waits that frame out.
 */
export function useFlowTitleFit(ref: RefObject<HTMLElement | null>, key: unknown): void {
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || el.dataset.direction === "vertical") return undefined;
    fitFlowTitles(el);
    let cancelled = false;
    const fonts = typeof document !== "undefined" ? document.fonts : undefined;
    if (fonts?.ready) {
      void fonts.ready.then(() => {
        if (!cancelled) fitFlowTitles(el);
      });
    }
    if (typeof ResizeObserver === "undefined") {
      return () => {
        cancelled = true;
      };
    }
    let frame = 0;
    const schedule = () => {
      if (frame) return;
      frame = window.requestAnimationFrame(() => {
        frame = 0;
        fitFlowTitles(el);
      });
    };
    const observer = new ResizeObserver(schedule);
    observer.observe(el);
    return () => {
      cancelled = true;
      observer.disconnect();
      if (frame) window.cancelAnimationFrame(frame);
    };
    // `key` is the steps the flow renders: new titles need measuring.
  }, [ref, key]);
}
