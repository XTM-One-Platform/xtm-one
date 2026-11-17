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
 * Shrink-to-fit for a slide body.
 *
 * A slide is a fixed canvas (1920x1080 for 16:9); the body's natural height is
 * whatever the agent's content adds up to. When that is taller than the room
 * left between the header and the footer, the overflow used to paint under the
 * footer block and the chrome, or past the bottom edge: overlapping text and
 * cut-off cards, with nothing in the data to say so. PowerPoint's answer is
 * autofit; this is the same answer. The body's content is measured at its
 * natural height, scaled down by exactly the ratio that makes it fit, and
 * widened by the inverse so the scaled box still spans the full width - text
 * reflows into the wider box first, which is why the ratio is searched for
 * (`searchFit`) rather than computed once. Nothing overlaps; the text gets
 * smaller instead.
 *
 * The ratio is published on the host as `data-fit` (and `--deck-fit`) so that:
 *   - the layout audit (`presentation_layout.py`) can read how far over budget
 *     a slide is and tell the agent to cut content, since a shrunk slide is
 *     still a slide too dense to read from the back of the room;
 *   - the PPTX writer, which reads computed font sizes and paddings that the
 *     transform does not touch, can correct them by the same ratio.
 *
 * Content that fits is left exactly as it was (no attribute, no transform), so
 * a deck within budget renders pixel-for-pixel as before.
 *
 * A block that sets its own size - a `metrics_grid`, whose KPI row
 * must read the same on every slide of the deck - is HELD at it: it carries
 * `data-fit-hold` (its block type) and the stylesheet multiplies every length
 * it is drawn with by `--deck-fit-hold`, the inverse of the ratio, so the
 * scaled body draws it at its size and the rest of the body gives up the
 * room. The hold is let go (`data-fit-released` on the host, the hold back to
 * 1) only when the body would still overflow at the floor with it; the held
 * blocks then shrink with the body and the audit reports each one.
 */
import { type RefObject, useLayoutEffect } from "react";

import { recapSpreadsWithin } from "./spread";

/**
 * Below this the text is unreadable anyway, so the ratio stops here and the
 * fitted host clips what still does not fit (`.deck-fit[data-fit]` in the
 * stylesheet) rather than letting it paint under the footer; the audit reports
 * the cut. Mirrored as `FIT_FLOOR` in `services/presentation_layout.py`.
 * @public
 */
export const FIT_MIN = 0.35;
/** Ratio changes smaller than this are noise from integer layout metrics. */
const FIT_EPSILON = 0.002;
/** Layout metrics are whole pixels; half a pixel of overflow is invisible. */
const FIT_TOLERANCE_PX = 0.5;
/**
 * A body that fits and leaves less than this share of its room unused is
 * fitted: chasing the last percent costs a synchronous layout per probe for a
 * difference nobody can see, and it is also what makes a second pass over a
 * fitted body a no-op.
 * @public
 */
export const FIT_SLACK_RATIO = 0.01;
/**
 * Layout reads per pass, each one a synchronous layout of the slide. The
 * bracket below converges in three or four on any body that reflows normally;
 * the cap is for one that refuses to, and the fitting end still wins then.
 * @public
 */
export const FIT_MAX_PROBES = 8;

/** To a thousandth, never up: a ratio rounded up by a hair is one that does not fit. */
const quantize = (k: number) => Math.floor(k * 1000 + 1e-9) / 1000;

/** The content's layout height at ratio `k` (its box is `1/k` wide), read after applying `k`. */
type FitMeasure = (k: number) => number;

interface FitProbe {
  k: number;
  height: number;
}

/**
 * The largest ratio in `[FIT_MIN, 1]` at which the scaled content fits its
 * room: `k * height(k) <= available`, `height(k)` being the layout height of
 * the content in a box `1/k` wide.
 *
 * Pure - every layout read goes through `measure`, which is what lets the
 * search be reasoned about (and exercised) without a DOM.
 *
 * The fixed-point step `k' = available / height(k)` assumes the height holds
 * while the box widens. It does for fixed-height cards (one probe, exact), but
 * text reflows: a body whose height tracks the ratio turns a true fit of 0.8
 * into the sequence 1 -> 0.64 -> 1 -> 0.64, over-shrinking readable content
 * and reporting a false overloaded slide. So the step is taken ONCE, to land
 * on the other side of the fit - from a ratio that does not fit it always
 * reaches one that does, and vice versa, for any body that is not taller in a
 * wider box - and from then on the fit is bracketed: `lo` fits, `hi` does not,
 * the next probe interpolates on the visual height (regula falsi) and bisects
 * after two moves of the same end so a stepwise reflow cannot stall it. The
 * answer is always the fitting end, so running out of probes costs precision,
 * never an overlap; `FIT_SLACK_RATIO` says when the precision is enough.
 * @public
 */
export function searchFit(available: number, startK: number, startHeight: number, measure: FitMeasure): number {
  const fits = (k: number, height: number) => k * height <= available + FIT_TOLERANCE_PX;
  const settled = (k: number, height: number) =>
    fits(k, height) && (k >= 1 || available - k * height <= available * FIT_SLACK_RATIO);
  // Fits already (this is the untouched body within budget): no write, ever.
  if (settled(startK, startHeight)) return startK;

  let lo: FitProbe | null = fits(startK, startHeight) ? { k: startK, height: startHeight } : null;
  let hi: FitProbe | null = lo ? null : { k: startK, height: startHeight };
  // Which end the last probe moved, and how many probes in a row did so.
  let lastMoved: "lo" | "hi" = lo ? "lo" : "hi";
  let sameSide = 1;
  for (let probes = 0; probes < FIT_MAX_PROBES; probes += 1) {
    let candidate: number;
    if (lo && hi) {
      if (hi.k - lo.k <= FIT_EPSILON) break;
      const visibleLo = lo.k * lo.height;
      const visibleHi = hi.k * hi.height;
      candidate =
        sameSide >= 2 || visibleHi <= visibleLo
          ? (lo.k + hi.k) / 2
          : lo.k + ((available - visibleLo) / (visibleHi - visibleLo)) * (hi.k - lo.k);
      candidate = Math.min(hi.k - FIT_EPSILON, Math.max(lo.k + FIT_EPSILON, candidate));
    } else {
      // One end known: the fixed-point step, exact when the height holds and
      // otherwise the probe that closes the bracket.
      const from = (lo ?? hi) as FitProbe;
      candidate = available / from.height;
      // A step from above that did not cross means the body grows as its box
      // widens (an image at full width): the plain step then chases the fit
      // from above without reaching it, so lean in by the overflow that is left.
      if (hi && sameSide >= 2) candidate *= available / (hi.k * hi.height);
    }
    const k = quantize(Math.max(FIT_MIN, Math.min(1, candidate)));
    // Nothing left between the known end and the clamp.
    if ((lo && k <= lo.k) || (hi && k >= hi.k)) break;
    const height = measure(k);
    if (fits(k, height)) {
      if (settled(k, height)) return k;
      sameSide = lastMoved === "lo" ? sameSide + 1 : 1;
      lastMoved = "lo";
      lo = { k, height };
    } else {
      sameSide = lastMoved === "hi" ? sameSide + 1 : 1;
      lastMoved = "hi";
      hi = { k, height };
    }
  }
  // Out of probes or out of room: the fitting end; with none, the floor (the host clips).
  return lo ? lo.k : FIT_MIN;
}

/** Apply (or clear, for `k === 1`) the fit ratio to a host + content pair. */
function applyFit(host: HTMLElement, content: HTMLElement, k: number): void {
  if (k >= 1) {
    content.style.removeProperty("transform");
    content.style.removeProperty("width");
    host.style.removeProperty("--deck-fit");
    delete host.dataset.fit;
    return;
  }
  content.style.transform = `scale(${k})`;
  content.style.width = `${(100 / k).toFixed(3)}%`;
  host.style.setProperty("--deck-fit", String(k));
  host.dataset.fit = k.toFixed(3);
}

/** Current ratio published on the host (1 when the content fits). */
export function currentFit(host: HTMLElement): number {
  const k = Number.parseFloat(host.dataset.fit ?? "");
  return Number.isFinite(k) && k > 0 && k < 1 ? k : 1;
}

/** The attribute of a block held at its size under the fit; its value is the block type.
 * @public
 */
export const FIT_HOLD_ATTR = "data-fit-hold";

/** Let go of the held blocks of a host (they shrink with the body), or hold them again.
 * @public
 */
export function releaseHold(host: HTMLElement, released: boolean): void {
  if (released) host.dataset.fitReleased = "true";
  else delete host.dataset.fitReleased;
}

/**
 * The content's layout height as it stands, its `fill` caps re-measured on it
 * first (`spread.ts`): a ratio or a hold change resizes the rows a cap was
 * measured on, and a stale cap clips its block short of them.
 */
function contentHeight(content: HTMLElement): number {
  recapSpreadsWithin(content);
  return content.offsetHeight;
}

/**
 * The attribute the host carries while a pass measures. A fitted content is at
 * least as tall as the host's room divided by the ratio
 * (`.deck-fit[data-fit]:not([data-fit-measuring]) > .deck-fit-content` in the
 * stylesheet), so its `fill` blocks reach the body floor once scaled; while
 * the pass measures, that floor is lifted and the content is read at its
 * minimum height, so `fill` growth never decides that a body overflows.
 * @public
 */
export const FIT_MEASURING_ATTR = "data-fit-measuring";

/** One `searchFit` over the pair as it stands, with the DOM as its ruler. */
function fitPass(host: HTMLElement, content: HTMLElement, available: number): number {
  host.setAttribute(FIT_MEASURING_ATTR, "");
  const startK = currentFit(host);
  const startHeight = contentHeight(content);
  let applied = startK;
  const k = searchFit(available, startK, startHeight, (candidate) => {
    applyFit(host, content, candidate);
    applied = candidate;
    return contentHeight(content);
  });
  // The search may end on a probe that did not fit; the host keeps the answer.
  if (applied !== k) applyFit(host, content, k);
  // `fill` resolves against the fitted room (`100% / k`), its caps re-measured on it.
  host.removeAttribute(FIT_MEASURING_ATTR);
  recapSpreadsWithin(content);
  return k;
}

/**
 * One fitting pass over a host + content pair: `searchFit` with the DOM as its
 * ruler. Every `offsetHeight` read after a style write forces a synchronous
 * layout of the slide, which is why the search is bounded and quantized (a
 * thousandth); a body already fitted within `FIT_SLACK_RATIO` is left alone,
 * which is what keeps the resize observer from chasing sub-pixel drift
 * forever. Returns the ratio left on the host.
 *
 * The held blocks keep their size first; a body that does not fit even at
 * the floor with them is fitted again with the hold let go. A pass over a
 * released body takes the hold back and ends where it started unless the
 * content changed, so the pair still settles: the resize observer is told of
 * the boxes as the pass leaves them, never of the probes in between.
 */
function fitContent(host: HTMLElement, content: HTMLElement): number {
  const available = host.clientHeight;
  // A hidden slide (reveal keeps far-away sections at display: none)
  // measures zero on both sides; leave it alone until it is shown.
  if (available <= 0 || content.offsetHeight <= 0) return currentFit(host);
  if (host.dataset.fitReleased !== undefined) releaseHold(host, false);
  const k = fitPass(host, content, available);
  if (!content.querySelector(`[${FIT_HOLD_ATTR}]`)) return k;
  // Read at the minimum height too: the fitted floor would always read as a fit.
  host.setAttribute(FIT_MEASURING_ATTR, "");
  const height = content.offsetHeight;
  host.removeAttribute(FIT_MEASURING_ATTR);
  if (k * height <= available + FIT_TOLERANCE_PX) return k;
  releaseHold(host, true);
  return fitPass(host, content, available);
}

/**
 * Keep `content` fitted inside `host` for as long as both are mounted.
 *
 * Runs once before the first paint (so a too-tall slide never flashes its
 * overlap), then again whenever either box changes size (a hidden slide being
 * shown, an edit landing from the studio), once the deck fonts land and
 * whenever an image in the body finishes loading. The last two matter for a
 * body that was fitted before they arrived: `.deck-fit-content` is at least
 * as tall as its host (`min-height: 100%`), so when the fonts or an image
 * make the content SHORTER than the room no observed box changes size, and
 * without them the stale ratio stayed - a `fill` table under a KPI row,
 * shrunk on a fresh load and on the floor once reached by navigation (#10602).
 * Writes are deferred to an animation frame so the observer's own delivery
 * never triggers the "loop completed with undelivered notifications" error; a
 * pass that changes nothing produces no notification, which is what ends the
 * cycle.
 */
export function useFitToHeight(
  hostRef: RefObject<HTMLElement | null>,
  contentRef: RefObject<HTMLElement | null>,
): void {
  useLayoutEffect(() => {
    const host = hostRef.current;
    const content = contentRef.current;
    if (!host || !content || typeof ResizeObserver === "undefined") return undefined;
    let frame = 0;
    let cancelled = false;
    const schedule = () => {
      if (frame || cancelled) return;
      frame = window.requestAnimationFrame(() => {
        frame = 0;
        fitContent(host, content);
      });
    };
    fitContent(host, content);
    const observer = new ResizeObserver(schedule);
    observer.observe(host);
    observer.observe(content);
    // `load` does not bubble: listen in the capture phase for every image of the body.
    content.addEventListener("load", schedule, true);
    const fonts = typeof document !== "undefined" ? document.fonts : undefined;
    if (typeof fonts?.ready?.then === "function") void fonts.ready.then(schedule);
    fonts?.addEventListener?.("loadingdone", schedule);
    return () => {
      cancelled = true;
      observer.disconnect();
      content.removeEventListener("load", schedule, true);
      fonts?.removeEventListener?.("loadingdone", schedule);
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, [hostRef, contentRef]);
}
