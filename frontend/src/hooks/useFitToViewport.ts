// SPDX-License-Identifier: Apache-2.0

/**
 * One vertical scrollbar per list page (issue #11233, dev-docs/reference/data-tables.md
 * "One scrollbar"): a scroll area inside a page (the records table's body) takes the
 * height the window has left for it, so the page itself never scrolls as well.
 *
 * The room is the window's height minus what sits above the area in the document (top
 * bar, page title, toolbar) and minus what follows it in the page (pagination, the rest of
 * the page, the main padding), measured from the content, never from the shell, which is
 * stretched to the window. Under `FIT_FLOOR_PX`, or when the page holds several scroll
 * areas, the page is long-form: no bound, the page scrolls alone and the area draws no
 * scroller of its own. Never both. In that mode `usePageStickyHead` keeps the area's
 * header under the app's top bar while the page scrolls (UX round 1 on #11241, B1).
 *
 * Only the page's main records list fits the window: the last block of the page, as on
 * /users. A table inside a dashboard widget (a card beside other cards, or above more of
 * the page) hugs its rows up to the page size and lets the page scroll (#11470).
 */
import { useEffect, useState } from "react";

/**
 * Below this room the area is not bounded: the page scrolls, the area does not. The
 * header row plus three one-line rows (40 px each), so a list under a tall page header
 * still fits the window instead of falling back to a page scroll.
 */
export const FIT_FLOOR_PX = 160;

/** Marks a scroll area that fits the window; the measure counts them per page. */
const FIT_AREA_ATTRIBUTE = "data-scroll-fit";

export interface FitMeasure {
  /** The window's height (innerHeight). */
  viewport: number;
  /** The area's top in the document (its rect top plus the page's scroll offset). */
  top: number;
  /** What the page draws under the area: its content's bottom plus the main padding, minus the area's bottom. */
  below: number;
}

/** The max height of the area in px, or null for no bound (long-form page). */
export function fitRoom({ viewport, top, below }: FitMeasure, floor = FIT_FLOOR_PX): number | null {
  const room = Math.floor(viewport - top - Math.max(0, below));
  return room >= floor ? room : null;
}

/**
 * The attribute of a side panel that fits the window on its own beside the page's list (the
 * faceted filter sidebar of a records list, #1501 UX r1 B1): the list beside it is still the
 * page's last block, so both are bounded and the page never scrolls.
 */
export const FIT_BESIDE_ATTRIBUTE = "data-fit-beside";

/**
 * True when `root` is the page's last block: at every level up to `main`, no laid-out
 * sibling sits beside it or under it (a sibling above it, a page title or a toolbar, is
 * fine). A widget in a dashboard grid has one (#11470): its bound came from the next row
 * of the page, not from the window, and cut the table to a few rows inside its card.
 * A side panel marked `FIT_BESIDE_ATTRIBUTE` is not such a sibling: it bounds itself.
 */
function isLastBlock(root: HTMLElement, main: HTMLElement): boolean {
  for (let node: HTMLElement | null = root; node && node !== main; node = node.parentElement) {
    const parent: HTMLElement | null = node.parentElement;
    if (!parent) return false;
    const top = node.getBoundingClientRect().top;
    for (const sibling of Array.from(parent.children)) {
      if (sibling === node || !(sibling instanceof HTMLElement) || sibling.hasAttribute(FIT_BESIDE_ATTRIBUTE)) continue;
      const position = getComputedStyle(sibling).position;
      if (position === "absolute" || position === "fixed") continue;
      const box = sibling.getBoundingClientRect();
      if (box.height > 0 && box.bottom > top + 1) return false;
    }
  }
  return true;
}

/**
 * Measures the area at its natural height: the bound this hook set is lifted for the read
 * and put back in the same task (no paint, no resize notification). Measured with the
 * bound on, a page whose height is set by a taller neighbour (a dashboard grid row: the
 * table beside a longer card) gave a room that depended on the bound itself: each bound
 * moved the room again, down to the floor, then back unbounded, and the table re-rendered
 * and blinked forever (#11467). The natural measure is the same whatever the bound, so
 * the room settles after one write.
 */
function measureNatural(el: HTMLElement, main: HTMLElement): FitMeasure {
  const bound = el.style.maxHeight;
  if (!bound) return measure(el, main);
  const scrollTop = el.scrollTop;
  el.style.maxHeight = "";
  try {
    return measure(el, main);
  } finally {
    el.style.maxHeight = bound;
    el.scrollTop = scrollTop;
  }
}

function measure(el: HTMLElement, main: HTMLElement): FitMeasure {
  const rect = el.getBoundingClientRect();
  // The content's bottom, not the main's: the shell stretches the main to the window.
  let contentBottom = rect.bottom;
  for (const child of Array.from(main.children)) {
    contentBottom = Math.max(contentBottom, child.getBoundingClientRect().bottom);
  }
  const padding = Number.parseFloat(getComputedStyle(main).paddingBottom) || 0;
  return {
    viewport: window.innerHeight,
    top: rect.top + window.scrollY,
    below: contentBottom + padding - rect.bottom,
  };
}

/**
 * The max height to give `el`: a number (fits the window), null (long-form page: no
 * bound) or undefined (not measured yet, or not in the app's page, a dialog for
 * example: the caller keeps its own cap). Re-measured when the window, the page's
 * content or the area changes size; a 1 px growth is ignored, so the bound it sets
 * never feeds back into a resize loop, but a 1 px shrink is applied, so the bound never
 * exceeds the room and the page never scrolls (#1300). `root` is the table's whole block (toolbar, area,
 * pager), the one that must be the page's last block to fit; it defaults to `el`.
 */
export function useFitToViewport(el: HTMLElement | null, root?: HTMLElement | null): number | null | undefined {
  const [room, setRoom] = useState<number | null | undefined>(undefined);
  useEffect(() => {
    if (!el || typeof window === "undefined") return;
    const main = el.closest<HTMLElement>("#app-content");
    if (!main || el.closest("[role=dialog]")) return;
    let frame = 0;
    const update = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        // Several scroll areas on one page: the page scrolls, none of them does.
        const several = main.querySelectorAll(`[${FIT_AREA_ATTRIBUTE}]`).length > 1;
        // A widget, not the page's list: no bound, the table hugs its rows (#11470).
        const widget = !isLastBlock(root ?? el, main);
        const next = several || widget ? null : fitRoom(measureNatural(el, main));
        // Keep the bound on a 1 px growth only: a bound 1 px over the room would scroll the page.
        setRoom((prev) => (typeof prev === "number" && next !== null && prev <= next && next - prev <= 1 ? prev : next));
      });
    };
    update();
    window.addEventListener("resize", update);
    main.addEventListener("animationend", update);
    let observer: ResizeObserver | undefined;
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(update);
      observer.observe(el);
      for (const child of Array.from(main.children)) observer.observe(child);
    }
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("resize", update);
      main.removeEventListener("animationend", update);
      observer?.disconnect();
    };
  }, [el, root]);
  return room;
}

/**
 * The max height of a side panel marked `FIT_BESIDE_ATTRIBUTE` (a facet sidebar beside the page's
 * list): the window's height minus the panel's top in the document and the main's bottom padding,
 * so the panel ends where the list beside it ends and scrolls inside itself, never the page (#1501
 * UX r1 B1). Undefined outside the app's page or before the first measure: the caller keeps its cap.
 */
export function useFitBeside(el: HTMLElement | null): number | undefined {
  const [room, setRoom] = useState<number | undefined>(undefined);
  useEffect(() => {
    if (!el || typeof window === "undefined") return;
    const main = el.closest<HTMLElement>("#app-content");
    if (!main || el.closest("[role=dialog]")) return;
    let frame = 0;
    const update = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const top = el.getBoundingClientRect().top + window.scrollY;
        const padding = Number.parseFloat(getComputedStyle(main).paddingBottom) || 0;
        const next = Math.floor(window.innerHeight - top - padding);
        setRoom(next >= FIT_FLOOR_PX ? next : undefined);
      });
    };
    update();
    window.addEventListener("resize", update);
    let observer: ResizeObserver | undefined;
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(update);
      for (const child of Array.from(main.children)) observer.observe(child);
    }
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("resize", update);
      observer?.disconnect();
    };
  }, [el]);
  return room;
}

/** The attribute a page-level sticky bar under the top bar carries (the horizontal SectionIndex). */
export const PAGE_STICKY_BAR_ATTRIBUTE = "data-page-sticky-bar";

/** The lowest edge a table header must clear: the top bar's, or a sticky page bar's below it. */
export function stickyBarBottom(barBottoms: readonly number[]): number {
  return barBottoms.reduce((low, bottom) => Math.max(low, bottom), 0);
}

/**
 * The page bars a table header must clear: only those placed before the area in the page.
 * A sticky bar that comes after the table (a tab bar under a card) sits below it at rest;
 * counting it pushed the header to the bottom of the table and hid its last row (#1294 UX r1 B1).
 */
export function pageBarsAbove(area: Node, bars: readonly HTMLElement[]): HTMLElement[] {
  return bars.filter((bar) => (bar.compareDocumentPosition(area) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0 && !bar.contains(area));
}

/** How far to push the header down so it sits under the top bar, kept inside the area. */
export function stickyShift(barBottom: number, areaTop: number, areaHeight: number, headHeight: number): number {
  return Math.round(Math.min(Math.max(0, barBottom - areaTop), Math.max(0, areaHeight - headHeight)));
}

/**
 * Long-form page (`active`): the area is the header's scroll box for `position: sticky`
 * (it keeps `overflow-x: auto` for wide tables) but has no height, so a sticky header
 * would scroll away with the page. The header is moved instead, under the app's top bar
 * (the first child of the shell column that holds `#app-content`), never past the
 * area's last row, and under any page bar that sticks below the top bar (the horizontal
 * SectionIndex, `data-page-sticky-bar`; #12271 UX r3 B1). Written to the style directly: a
 * scroll never re-renders the table.
 */
export function usePageStickyHead(area: HTMLElement | null, head: HTMLElement | null, active: boolean): void {
  useEffect(() => {
    if (!active || !area || !head || typeof window === "undefined") return;
    const main = area.closest<HTMLElement>("#app-content");
    if (!main) return;
    const bar = main.parentElement?.firstElementChild as HTMLElement | null | undefined;
    let frame = 0;
    const update = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const rect = area.getBoundingClientRect();
        const pageBars = pageBarsAbove(area, Array.from(main.querySelectorAll<HTMLElement>(`[${PAGE_STICKY_BAR_ATTRIBUTE}]`)));
        const barBottom = stickyBarBottom([
          bar && bar !== main ? bar.getBoundingClientRect().bottom : 0,
          ...pageBars.map((pageBar) => pageBar.getBoundingClientRect().bottom),
        ]);
        const shift = stickyShift(barBottom, rect.top, rect.height, head.offsetHeight);
        head.style.transform = shift > 0 ? `translateY(${shift}px)` : "";
      });
    };
    update();
    window.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("scroll", update);
      window.removeEventListener("resize", update);
      head.style.transform = "";
    };
  }, [area, head, active]);
}
