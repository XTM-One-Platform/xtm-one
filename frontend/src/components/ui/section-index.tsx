// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef, useState } from "react";

import { PAGE_STICKY_BAR_ATTRIBUTE } from "@/hooks/useFitToViewport";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

/**
 * The one in-page section index of the frontend (#9539, UX B3): the sticky
 * "On this page" list that jumps to the sections of a long page.
 *
 * Two orientations. `vertical` (the default) is the list beside the page: it
 * sticks below the shell's sticky header (`--fds-header-height`, 68 px by
 * default) plus a 16 px gap. `horizontal` is the bar above the page (#12271,
 * UX r2 B1): it sticks right under the header and wraps its entries on a
 * narrow screen; it publishes its real height as `--section-bar-height` on
 * the document, so a jump lands below it at every width and in every locale.
 *
 * The current section carries `aria-current="location"`: the one clicked
 * last, kept until the reader scrolls (wheel, touch, a press or a scroll key), then the
 * one the reader scrolls to, read from the sections' positions on every
 * scroll (`pickCurrentSection`, #794 UX r2 and r3 B1): over the last viewport
 * of scroll the band slides down to the bottom of the screen, so each short
 * last section that can never reach the band under the header gets its own
 * stretch of scroll and is current in turn. A target section takes
 * `SECTION_ANCHOR_CLASS` (vertical) or `SECTION_BAR_ANCHOR_CLASS` (horizontal)
 * so a jump lands its heading below the header and the bar, not behind them.
 * `section-index.test.ts` fails on a second hand-written index.
 */
interface SectionIndexEntry {
  /** Anchor id of the target section. */
  id: string;
  /** English title, translated here. */
  title: string;
}

/** The scroll margin of a section a vertical index jumps to: the header height plus a 16 px gap. */
export const SECTION_ANCHOR_CLASS = "scroll-mt-[calc(var(--fds-header-height,68px)_+_1rem)]";

/**
 * The scroll margin of a section the horizontal bar jumps to: the header, the bar's measured
 * height (one row, 49 px, until it is measured) and a 16 px gap.
 */
export const SECTION_BAR_ANCHOR_CLASS = "scroll-mt-[calc(var(--fds-header-height,68px)_+_var(--section-bar-height,49px)_+_1rem)]";

/** The document variable the horizontal bar writes its height to. */
const BAR_HEIGHT_VAR = "--section-bar-height";

/** Where a section turns current: its top at or above this line (the header, the bar and a gap). */
const BAND_TOP = { horizontal: 140, vertical: 84 } as const;

/** The keys that scroll the page: pressed, they release the entry the reader clicked. */
const SCROLL_KEYS = new Set(["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "]);

/** The geometry of one scroll position: what `pickCurrentSection` reads. */
interface SectionGeometry {
  /** Top of each section in viewport pixels, in page order. */
  tops: readonly number[];
  /** The line a section's top crosses to become current. */
  bandTop: number;
  /** Bottom of the visible area in viewport pixels. */
  viewportBottom: number;
  /** The page is scrolled to its top. */
  atTop: boolean;
  /** Pixels of scroll left below this position (0 at the bottom). */
  remaining: number;
  /** Height of the visible area: the last viewport of scroll is where the band slides. */
  viewportHeight: number;
}

/**
 * The line a section's top crosses to become current: the band under the header, sliding down to
 * the bottom of the visible area over the last viewport of scroll (#794 UX r3 B1), so short last
 * sections whose tops cannot reach the band each get a stretch of scroll where they are current.
 */
export function currentLine({ bandTop, viewportBottom, remaining, viewportHeight }: Omit<SectionGeometry, "tops" | "atTop">): number {
  const progress = viewportHeight > 0 ? Math.min(1, Math.max(0, 1 - remaining / viewportHeight)) : 1;
  return bandTop + (viewportBottom - bandTop) * progress;
}

/**
 * The index of the current section: the first at the top of the page; otherwise the last one whose
 * top is at or above the current line (`currentLine`), or the first while none is.
 */
export function pickCurrentSection(geometry: SectionGeometry): number {
  const { tops, atTop } = geometry;
  if (tops.length === 0) return -1;
  if (atTop) return 0;
  const line = currentLine(geometry);
  let picked = 0;
  tops.forEach((top, i) => {
    if (top <= line) picked = i;
  });
  return picked;
}

/** The element that scrolls the page holding `el`: its nearest scrolling ancestor, else the document. */
function scrollerOf(el: HTMLElement): HTMLElement {
  for (let node = el.parentElement; node; node = node.parentElement) {
    const overflow = getComputedStyle(node).overflowY;
    if ((overflow === "auto" || overflow === "scroll") && node.scrollHeight > node.clientHeight) return node;
  }
  return (document.scrollingElement as HTMLElement | null) ?? document.documentElement;
}

export function SectionIndex({
  entries,
  label = "On this page",
  orientation = "vertical",
  onSelect,
  className,
}: {
  entries: readonly SectionIndexEntry[];
  /** English accessible name, translated here. */
  label?: string;
  orientation?: "vertical" | "horizontal";
  /**
   * The page moves to the section itself (it unfolds a card first, then scrolls): the link
   * does not jump. Without it the link jumps natively to its anchor.
   */
  onSelect?: (id: string) => void;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const [current, setCurrent] = useState<string | null>(entries[0]?.id ?? null);
  const ids = entries.map((e) => e.id).join(" ");
  const horizontal = orientation === "horizontal";
  const navRef = useRef<HTMLElement>(null);

  // The bar's height changes when its entries wrap (a narrow screen, a long locale): publish it.
  useEffect(() => {
    const nav = navRef.current;
    if (!horizontal || !nav || typeof ResizeObserver === "undefined") return;
    const root = document.documentElement;
    const publish = () => root.style.setProperty(BAR_HEIGHT_VAR, `${Math.ceil(nav.getBoundingClientRect().height)}px`);
    publish();
    const observer = new ResizeObserver(publish);
    observer.observe(nav);
    return () => {
      observer.disconnect();
      root.style.removeProperty(BAR_HEIGHT_VAR);
    };
  }, [horizontal]);

  // The entry the reader clicked stays current while the jump scrolls, until the reader scrolls.
  const pinnedRef = useRef<string | null>(null);

  useEffect(() => {
    const targets = ids
      .split(" ")
      .map((id) => document.getElementById(id))
      .filter((el): el is HTMLElement => !!el);
    if (targets.length === 0) return;
    const scroller = scrollerOf(targets[0]);
    const isDocument = scroller === document.scrollingElement || scroller === document.documentElement;
    const update = () => {
      if (pinnedRef.current) return;
      const { scrollTop, scrollHeight, clientHeight } = scroller;
      // A page that does not scroll keeps the entry it has.
      if (scrollHeight <= clientHeight) return;
      const viewportBottom = isDocument ? clientHeight : scroller.getBoundingClientRect().bottom;
      const index = pickCurrentSection({
        tops: targets.map((el) => el.getBoundingClientRect().top),
        bandTop: horizontal ? BAND_TOP.horizontal : BAND_TOP.vertical,
        viewportBottom,
        atTop: scrollTop <= 0,
        // Within 2 px of the bottom counts as the bottom (fractional scroll positions).
        remaining: Math.max(0, scrollHeight - clientHeight - scrollTop - 2),
        viewportHeight: clientHeight,
      });
      if (index >= 0) setCurrent(targets[index].id);
    };
    const release = () => {
      pinnedRef.current = null;
    };
    const releaseOnKey = (event: KeyboardEvent) => {
      if (SCROLL_KEYS.has(event.key)) release();
    };
    update();
    const opts = { capture: true, passive: true } as const;
    document.addEventListener("scroll", update, opts);
    document.addEventListener("wheel", release, opts);
    document.addEventListener("touchmove", release, opts);
    // A scrollbar drag starts with a press; a click on an entry pins it again right after.
    document.addEventListener("mousedown", release, opts);
    document.addEventListener("keydown", releaseOnKey, opts);
    window.addEventListener("resize", update, { passive: true });
    return () => {
      document.removeEventListener("scroll", update, opts);
      document.removeEventListener("wheel", release, opts);
      document.removeEventListener("touchmove", release, opts);
      document.removeEventListener("mousedown", release, opts);
      document.removeEventListener("keydown", releaseOnKey, opts);
      window.removeEventListener("resize", update);
    };
  }, [ids, horizontal]);

  return (
    <nav
      ref={navRef}
      aria-label={t_i18n(label)}
      // Horizontal: the tables' sticky headers stop under it (usePageStickyHead) and, as a card
      // scrolls away, pass beneath it (z-20 over their z-10; #12271 UX r3 B1).
      className={cn(horizontal && "sticky top-[var(--fds-header-height,68px)] z-20 border-b border-elevation-subtle bg-elevation-default", className)}
      {...(horizontal ? { [PAGE_STICKY_BAR_ATTRIBUTE]: "" } : {})}
      data-testid="section-index"
      data-orientation={orientation}
    >
      <ul
        className={cn(
          "content-compact",
          horizontal
            ? "flex flex-wrap gap-1 py-2"
            : "sticky top-[calc(var(--fds-header-height,68px)_+_1rem)] space-y-1",
        )}
      >
        {entries.map((e) => {
          const isCurrent = current === e.id;
          return (
            <li key={e.id} className={horizontal ? "shrink-0" : undefined}>
              <a
                href={`#${e.id}`}
                aria-current={isCurrent ? "location" : undefined}
                onClick={(event) => {
                  pinnedRef.current = e.id;
                  setCurrent(e.id);
                  if (onSelect) {
                    event.preventDefault();
                    onSelect(e.id);
                  }
                }}
                className={cn(
                  "rounded-sm px-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus",
                  horizontal ? "inline-flex h-8 items-center" : "block py-1",
                  isCurrent
                    ? "bg-accent/10 font-medium text-accent"
                    : "text-default-secondary hover:bg-elevation-hover hover:text-default-primary",
                )}
              >
                {t_i18n(e.title)}
              </a>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
