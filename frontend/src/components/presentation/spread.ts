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
 * Bounded spread for `fill` and `distribute`.
 *
 * `fill` on a grid block and `distribute` on a content slide are
 * `space-between`: the free height is divided over the gaps whatever
 * the content is, so three one-line bullets on a 1080px canvas ended up ~250px
 * apart and a callout and two arrow items sat at opposite ends of the body.
 * The switches made a THIN slide look stretched, not finished, and the audit
 * saw nothing: the block reached the floor, so nothing was left to measure.
 *
 * The fix is the same shape as `fit.ts`: measured, deterministic, no prompt
 * text. The spreading element gets an inline `max-height` equal to its
 * NATURAL height (the rows at their content size plus the base gaps) plus one
 * `maxExtra` per gap (`spread-math.ts`). The flex layout does the rest: a room
 * smaller than the cap still hands the element every pixel it has and
 * `space-between` spreads exactly as before, a room larger than the cap stops
 * it there and the rest stays BELOW the last row - which is what the layout
 * audit now measures honestly and reports as `sparse`, with remedies that
 * cannot be "set fill" again (`presentation_layout.sparse_remedy`).
 *
 * The cap is published as `data-spread-cap` (canvas px) so the audit, the e2e
 * specs and the export read one geometry, and so a run that holds a capped
 * block can read the block's natural size off it instead of its stretched box.
 *
 * A `table` block spreads its ROWS rather than the gaps between them
 * (a stretched `<table>` hands its extra height to its rows), so its natural
 * rows are read by a measurer of its own (`measureTableRows`); the cap is the
 * same arithmetic, one readable extra per row boundary.
 *
 * `numbered_cards` with `fill` - the takeaways body - shares the height between
 * its cards instead: each row of cards grows, not the gaps, so its
 * natural rows are read by `measureCardRows` and the cap adds one readable
 * extra per ROW. Left unbounded, one row of three takeaways was stretched to
 * the whole body: the number at the top of a near-empty pillar, the text at
 * its foot.
 *
 * Nothing here scales: `offsetTop` / `offsetHeight` are layout metrics, which
 * the canvas transform and the fit ratio do not touch.
 */
import { type RefObject, useLayoutEffect } from "react";

import { type SpreadBox, type SpreadGrowth, rowHeights, spreadCap } from "./spread-math";

export {
  SPREAD_AGENDA_ICON_ROW_MAX_EXTRA_PX,
  SPREAD_AGENDA_ROW_MAX_EXTRA_PX,
  SPREAD_BLOCK_MAX_EXTRA_PX,
  SPREAD_CARD_MAX_EXTRA_PX,
  SPREAD_ROW_MAX_EXTRA_PX,
} from "./spread-math";

/** Apply (or clear, for `null`) the cap on the element. Writes only what changed. */
function applySpreadCap(el: HTMLElement, cap: number | null): void {
  if (cap === null) {
    if (el.style.maxHeight) el.style.removeProperty("max-height");
    if (el.dataset.spreadCap !== undefined) delete el.dataset.spreadCap;
    return;
  }
  const px = String(Math.ceil(cap));
  if (el.style.maxHeight !== `${px}px`) el.style.maxHeight = `${px}px`;
  if (el.dataset.spreadCap !== px) el.dataset.spreadCap = px;
}

/** The cap an element carries (`null` when uncapped). */
function currentSpreadCap(el: HTMLElement): number | null {
  const cap = Number.parseFloat(el.dataset.spreadCap ?? "");
  return Number.isFinite(cap) && cap > 0 ? cap : null;
}

/**
 * What one capping pass measured on an element, in layout px: the rows at
 * their natural height, the base `gap` between them and the `frame` the cap
 * must carry on top of the rows (padding and border of a `border-box`
 * element, a footnote) - or a filler, which opts the element out.
 */
export interface SpreadMeasurement {
  rows: number[];
  gap: number;
  frame: number;
  /**
   * The element holds a child that takes the room BY DESIGN - a `fill` chart,
   * image, diagram, callout or phase card, a grow spacer: anything that grows
   * and carries no cap of its own. Its height is whatever the room is, so a
   * cap read off it would freeze the run at the size it happened to have;
   * such a run is not capped at all - the filler leaves nothing to spread
   * anyway. Also an element that has nothing to spread where it stands (a
   * `fill` table in a dashboard panel, which sizes its table itself): no cap
   * either.
   */
  filler: boolean;
  /** What takes the extra height: the gaps (the default) or the rows themselves. */
  grows?: SpreadGrowth;
}

/**
 * How an element's natural rows are read. `null` when nothing is visible -
 * reveal keeps far-away slides at `display: none` and everything then
 * measures zero; the element is left as it is until it is shown.
 */
export type SpreadMeasurer = (el: HTMLElement) => SpreadMeasurement | null;

const px = (value: string) => Number.parseFloat(value) || 0;

/** The padding and border a `border-box` element counts inside its max-height. */
function boxFrame(cs: CSSStyleDeclaration): number {
  return cs.boxSizing === "border-box"
    ? px(cs.paddingTop) + px(cs.paddingBottom) + px(cs.borderTopWidth) + px(cs.borderBottomWidth)
    : 0;
}

/**
 * The default measurer, for a grid or a run whose rows are its CHILDREN and
 * whose spread grows the gaps between them (`space-between`): the visible
 * children grouped into rows by their top edge. A capped child (a `fill`
 * grid block inside a `distribute` run) is measured at ITS cap, not at the
 * height the room gave it: the cap is what the block would take in a room
 * large enough, which is the size the run must plan for.
 */
function measureChildren(el: HTMLElement): SpreadMeasurement | null {
  const boxes: SpreadBox[] = [];
  let filler = false;
  for (const child of el.children) {
    if (!(child instanceof HTMLElement) || child.offsetHeight <= 0) continue;
    const cap = currentSpreadCap(child);
    if (cap === null && Number.parseFloat(getComputedStyle(child).flexGrow) > 0) filler = true;
    boxes.push({ top: child.offsetTop, height: cap ?? child.offsetHeight });
  }
  if (!boxes.length) return null;
  const cs = getComputedStyle(el);
  return { rows: rowHeights(boxes), gap: px(cs.rowGap), frame: boxFrame(cs), filler };
}

/**
 * Whether the element is a block of a column whose row is stretched
 * (`vertical_align` / `align: stretch`, or equal height): the column's height
 * is the row's, not the element's content, so a `fill` block there takes it.
 */
function inStretchedColumn(el: HTMLElement): boolean {
  return el.matches(
    '.deck-columns:is([data-valign="stretch"], [data-equal-height="true"]) > .deck-column > .deck-blocks > *',
  );
}

/**
 * The measurer of a `table` block, whose spread grows the ROWS
 * themselves: the stretched `<table>` hands its extra height to its rows, so
 * their boxes are only natural while nothing stretches them. The root is put
 * at its content height for one synchronous layout - the run's `flex` hook
 * and the cap it carries (stale after an edit that added rows) lifted - the
 * `<tr>`s read, and everything put back before anything paints, the way
 * `columns-fit.ts` reads a column's min-content width. The frame is whatever
 * the root holds besides the rows: the wrap's border and the footnote. Only a
 * block of a run (`.deck-blocks`, the body's or a column's) spreads: a
 * dashboard panel sizes its table itself, so a table there is left uncapped,
 * as the stylesheet leaves it unstretched.
 */
export function measureTableRows(el: HTMLElement): SpreadMeasurement | null {
  // A `fill` table in a stretched or equal-height column (slide-level or a
  // `columns` block) is that column's room, as in a dashboard panel: its body
  // rows spread to the column's floor, uncapped, beside a chart that reaches it.
  const table = el.querySelector<HTMLTableElement>(":scope > .deck-table-wrap > .deck-table");
  if (!table) return null;
  if (!el.matches(".deck-blocks > *") || inStretchedColumn(el)) return { rows: [], gap: 0, frame: 0, filler: true };
  const { flex, maxHeight } = el.style;
  el.style.flex = "none";
  el.style.maxHeight = "none";
  const rows = [...table.rows].map((row) => row.offsetHeight).filter((height) => height > 0);
  const natural = el.offsetHeight;
  el.style.flex = flex;
  el.style.maxHeight = maxHeight;
  if (!rows.length || natural <= 0) return null;
  const frame = Math.max(0, natural - rows.reduce((sum, height) => sum + height, 0));
  return { rows, gap: 0, frame, filler: false };
}

/**
 * The measurer of a grid whose spread grows its ROWS: the `fill` numbered
 * cards (the grid hands every row `1fr` of the height it is given) and the
 * `fill` key / value pairs (the `auto` rows stretch, each pair centred between
 * its rules), so a row's box is only natural while nothing stretches the grid. The root is
 * put at its content height for one synchronous layout - the `flex` the body
 * or the run hands it and the cap it carries lifted - the cards read (the
 * `1fr` rows of a grid of no set height all take the tallest card's), and
 * everything put back before anything paints, as `measureTableRows` does.
 */
export function measureCardRows(el: HTMLElement): SpreadMeasurement | null {
  const { flex, maxHeight } = el.style;
  el.style.flex = "none";
  el.style.maxHeight = "none";
  const boxes: SpreadBox[] = [];
  for (const child of el.children) {
    if (child instanceof HTMLElement && child.offsetHeight > 0) boxes.push({ top: child.offsetTop, height: child.offsetHeight });
  }
  const natural = el.offsetHeight;
  const gap = px(getComputedStyle(el).rowGap);
  el.style.flex = flex;
  el.style.maxHeight = maxHeight;
  if (!boxes.length || natural <= 0) return null;
  const rows = rowHeights(boxes);
  const frame = Math.max(0, natural - rows.reduce((sum, height) => sum + height, 0) - (rows.length - 1) * gap);
  return { rows, gap, frame, filler: false, grows: "rows" };
}

/**
 * The measurer of a `fill` callout: its body's run is what grows in
 * it, so the callout only takes the whole room when something in that
 * run does BY DESIGN - a `fill` chart, image or diagram, any growing child that
 * carries no cap of its own. When every growing child of the run is a bounded
 * spread (a `fill` list, key-values, metrics...), the callout follows them: its
 * natural height, read with its `flex` and its cap lifted for one synchronous
 * layout, plus the extra each capped child may still take (its cap minus its
 * natural height). Left uncapped, the list stopped at its cap and the callout's
 * tint ran on below the last row, an empty band of about 350 canvas px on a
 * 1080px slide. A callout in a column keeps stretching: the cards of a row share
 * one height (spec 3.6), the height of the tallest card. A callout
 * with no growing child keeps the room only when it is alone in its run and
 * its content takes at least `CALLOUT_FILL_MIN_SHARE` of it
 * (`calloutFillHonoured`); otherwise it is capped at its natural height.
 */
export function measureCalloutRun(el: HTMLElement): SpreadMeasurement | null {
  const filler: SpreadMeasurement = { rows: [], gap: 0, frame: 0, filler: true };
  if (el.closest(".deck-column")) return filler;
  const run = el.querySelector<HTMLElement>(":scope > .deck-callout-body > .deck-blocks");
  if (!run) return filler;
  const capped: HTMLElement[] = [];
  for (const child of run.children) {
    if (!(child instanceof HTMLElement) || child.offsetHeight <= 0) continue;
    if (currentSpreadCap(child) !== null) capped.push(child);
    else if (child.dataset.fill === "true" || Number.parseFloat(getComputedStyle(child).flexGrow) > 0) return filler;
  }
  if (!run.children.length) return null;
  const { flex, maxHeight } = el.style;
  el.style.flex = "none";
  el.style.maxHeight = "none";
  const natural = el.offsetHeight;
  const extra = capped.reduce(
    (sum, child) => sum + Math.max(0, (currentSpreadCap(child) as number) - child.offsetHeight),
    0,
  );
  el.style.flex = flex;
  el.style.maxHeight = maxHeight;
  if (natural <= 0) return null;
  // Nothing in the run grows: the callout takes the room only when it
  // is alone in its run and its content already fills most of it; otherwise
  // it is capped at its natural height (no capped child, so no extra) and the
  // free height stays below it.
  if (!capped.length && calloutFillHonoured(el, natural)) return filler;
  // `offsetHeight` is the border box: a `content-box` callout counts its padding
  // and border outside its max-height.
  const cs = getComputedStyle(el);
  const outside =
    cs.boxSizing === "border-box"
      ? 0
      : px(cs.paddingTop) + px(cs.paddingBottom) + px(cs.borderTopWidth) + px(cs.borderBottomWidth);
  return { rows: [Math.max(0, natural + extra - outside)], gap: 0, frame: 0, filler: false };
}

/**
 * The share of its room a `fill` callout's content must already take for the
 * callout to stretch to the rest (brand rule: no big empty card).
 */
const CALLOUT_FILL_MIN_SHARE = 0.6;

/**
 * Whether a `fill` callout whose run holds nothing that grows keeps taking
 * the room. An explicit request wins: in a run the slide asked to stretch
 * (`vertical_align: stretch`, `data-valign="stretch"`) every `fill` callout
 * shares the body, as the renderer did before the hug-content default.
 * Otherwise only when it is the one visible block of its run and its natural
 * height is at least `CALLOUT_FILL_MIN_SHARE` of the run's height. A short
 * callout, or one beside other blocks, hugs its content instead of drawing a
 * tall tinted card with two thirds of it empty.
 */
function calloutFillHonoured(el: HTMLElement, natural: number): boolean {
  const run = el.parentElement;
  if (!run || !run.classList.contains("deck-blocks")) return false;
  if (run.dataset.valign === "stretch") return true;
  for (const sibling of run.children) {
    if (sibling !== el && sibling instanceof HTMLElement && sibling.offsetHeight > 0) return false;
  }
  const room = run.clientHeight;
  return room > 0 && natural >= room * CALLOUT_FILL_MIN_SHARE;
}

/**
 * One capping pass over an element: measure its rows, compute the cap and
 * write it. Returns the cap left on the element.
 */
function capSpread(el: HTMLElement, maxExtra: number, measure: SpreadMeasurer = measureChildren): number | null {
  const measured = measure(el);
  if (!measured) return currentSpreadCap(el);
  if (measured.filler) {
    applySpreadCap(el, null);
    return null;
  }
  const cap = spreadCap(measured.rows, measured.gap, maxExtra, measured.frame, measured.grows);
  applySpreadCap(el, cap);
  return cap;
}

/** How one bounded element is re-measured: the `maxExtra` it is capped at and its measurer. */
interface Bound {
  maxExtra: number;
  measure: SpreadMeasurer;
}

/** Every element `useSpreadCap` keeps bounded. */
const bounded = new Map<HTMLElement, Bound>();

/**
 * Re-measure, synchronously, the cap of every bounded element inside `root`,
 * innermost first, so a `distribute` run reads the cap its `fill` block has
 * just taken.
 *
 * Called by a pass that changes the layout the caps were measured on and reads
 * the result in the same task: the shrink-to-fit search (`fit.ts`), whose
 * every probe widens the body and rescales the blocks it holds at their size.
 * Left to the observers, a cap lands a frame after the layout it describes: a
 * cap measured on smaller rows clips its block short of them, the search then
 * reads a body that fits when its rows do not, and the two passes chase each
 * other on every frame.
 */
export function recapSpreadsWithin(root: HTMLElement): void {
  const inside = [...bounded].filter(([el]) => root.contains(el));
  // Reverse document order puts every element after the ones it contains.
  inside.sort(([a], [b]) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? 1 : -1));
  for (const [el, bound] of inside) capSpread(el, bound.maxExtra, bound.measure);
}

/**
 * Keep the element's spread bounded for as long as it is mounted and
 * `enabled` (the block's `fill`, the slide's `distribute`). Runs before the
 * first paint, then again whenever the element, its parent or one of its
 * children changes size: fonts landing, a hidden slide being shown, an edit
 * from the studio. `key` re-arms the observers when the children are
 * replaced (pass the items or blocks the element renders). Writes are
 * deferred to an animation frame so the observer's own delivery never loops;
 * a pass that changes nothing produces no notification, which ends the cycle.
 * A nested pair - a `fill` grid block inside a `distribute` run - settles in
 * two rounds: React runs the block's layout effect before the run's, so the
 * first paint is right, and on a later resize the run may read the block
 * before its new cap lands (it then reads the block's `flex-grow` as a
 * filler and drops its own cap for one frame) until the block's write fires
 * the run's child observer and the run measures the cap; `deckSettled()`
 * (`ready.ts`) waits that round out before an export reads the boxes.
 * Disabling clears the cap, so a `fill: false` edit renders as if it had
 * never been set. `measure` is how the element's natural rows are read: its
 * children by default, the `<tr>`s of a `table` block (`measureTableRows`),
 * the cards of a `fill` numbered cards grid (`measureCardRows`), the run of a
 * `fill` callout (`measureCalloutRun`).
 */
export function useSpreadCap(
  ref: RefObject<HTMLElement | null>,
  enabled: boolean | undefined,
  maxExtra: number,
  key: unknown,
  measure: SpreadMeasurer = measureChildren,
): void {
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    if (!enabled) {
      applySpreadCap(el, null);
      return undefined;
    }
    capSpread(el, maxExtra, measure);
    bounded.set(el, { maxExtra, measure });
    if (typeof ResizeObserver === "undefined") {
      return () => {
        bounded.delete(el);
      };
    }
    let frame = 0;
    const schedule = () => {
      if (frame) return;
      frame = window.requestAnimationFrame(() => {
        frame = 0;
        capSpread(el, maxExtra, measure);
      });
    };
    const observer = new ResizeObserver(schedule);
    observer.observe(el);
    if (el.parentElement) observer.observe(el.parentElement);
    for (const child of el.children) observer.observe(child);
    return () => {
      bounded.delete(el);
      observer.disconnect();
      if (frame) window.cancelAnimationFrame(frame);
    };
    // `key` is the content the element renders: new children need observing.
  }, [ref, enabled, maxExtra, key, measure]);
}
