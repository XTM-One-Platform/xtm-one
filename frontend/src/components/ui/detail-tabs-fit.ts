// SPDX-License-Identifier: Apache-2.0

/**
 * Which tabs of a detail tab bar stay in the bar when the bar is narrower than
 * its tabs. Pure, so the rule is tested without a layout engine.
 *
 * The bar keeps its tabs in order and moves the trailing ones that do not fit
 * into a "More tabs" menu. The active tab always stays in the bar: when it
 * would fall in the menu, it takes the place of the last tab that fits, so the
 * selected state is always visible where the person looks.
 */
export interface TabFitInput {
  /** Rendered width of every tab trigger, in bar order. */
  widths: readonly number[];
  /** Gap between two triggers of the list. */
  gap: number;
  /** The list's own horizontal padding and border, added once. */
  chrome: number;
  /** Width the bar may take. */
  available: number;
  /** Width the "More tabs" trigger takes, its gap to the list included. */
  more: number;
  /** Index of the active tab, -1 when none is. */
  activeIndex: number;
}

/** Indices of the tabs that stay in the bar, in bar order. */
export function fitTabs({ widths, gap, chrome, available, more, activeIndex }: TabFitInput): number[] {
  const all = widths.map((_, index) => index);
  const span = (indices: readonly number[]) =>
    chrome + indices.reduce((total, index) => total + widths[index], 0) + gap * Math.max(0, indices.length - 1);
  if (span(all) <= available) return all;

  const room = available - more;
  const active = activeIndex >= 0 && activeIndex < widths.length ? activeIndex : -1;
  for (let count = widths.length - 1; count >= 1; count -= 1) {
    const kept = active >= count ? [...all.slice(0, count - 1), active] : all.slice(0, count);
    if (span(kept) <= room) return kept;
  }
  // Not even one tab fits beside the menu: keep the active one (or the first),
  // and let the list's own horizontal scroll show what it can.
  return [active >= 0 ? active : 0];
}

/**
 * Whether the committed list overflows enough for the library to show its
 * scroll chevron: the library's own test (`scrollLeft + clientWidth <
 * scrollWidth - 1`, at rest), so the bar drops a tab exactly when a chevron
 * would appear and never for a sub-pixel rounding it would ignore.
 */
export function listOverflows({ scrollWidth, clientWidth }: { scrollWidth: number; clientWidth: number }): boolean {
  return clientWidth < scrollWidth - 1;
}

/**
 * The kept tabs less one: the last that is not the active tab. Used when the
 * rendered list still overflows after the kept set is committed (sub-pixel
 * widths, a font swap), so no list scrolls beside the "More tabs" trigger.
 * A single tab is kept as is.
 */
export function dropLastTab(kept: readonly number[], activeIndex: number): number[] {
  if (kept.length <= 1) return [...kept];
  for (let position = kept.length - 1; position >= 0; position -= 1) {
    if (kept[position] !== activeIndex) return kept.filter((_, other) => other !== position);
  }
  return [...kept];
}

/**
 * The `scrollLeft` that brings `item` into the horizontal view of a scroll
 * container, moving it as little as possible ("nearest"). Both boxes are
 * viewport rectangles (`getBoundingClientRect`). Used instead of
 * `element.scrollIntoView`, which also scrolls every scrollable ancestor,
 * the window included, and so undid a deep link's own scroll to an anchor
 * below the tab bar (xtm-one #9291, round 2 B1).
 */
export function scrollLeftToReveal(
  view: { left: number; right: number },
  item: { left: number; right: number },
  scrollLeft: number,
): number {
  if (item.left < view.left) return scrollLeft - (view.left - item.left);
  if (item.right > view.right) {
    const shift = Math.min(item.right - view.right, item.left - view.left);
    return scrollLeft + shift;
  }
  return scrollLeft;
}

/**
 * Reveal `tab` inside the tab bar only: every horizontally scrollable box
 * from the tab up to `bar` (included) gets its `scrollLeft` adjusted, and
 * nothing above `bar` moves, so the page keeps its vertical scroll.
 */
export function revealTabInBar(tab: HTMLElement, bar: HTMLElement): void {
  for (let box = tab.parentElement; box; box = box.parentElement) {
    if (box.scrollWidth > box.clientWidth + 1) {
      const next = scrollLeftToReveal(box.getBoundingClientRect(), tab.getBoundingClientRect(), box.scrollLeft);
      if (next !== box.scrollLeft) box.scrollLeft = next;
    }
    if (box === bar) break;
  }
}
