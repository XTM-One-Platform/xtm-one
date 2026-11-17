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
 * last, then the one the reader scrolls to. A target section takes
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

  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    const targets = ids
      .split(" ")
      .map((id) => document.getElementById(id))
      .filter((el): el is HTMLElement => !!el);
    if (targets.length === 0) return;
    // A section is current once its top passes under the header band (and the bar, horizontal).
    const observer = new IntersectionObserver(
      (seen) => {
        const visible = seen.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible[0]) setCurrent(visible[0].target.id);
      },
      { rootMargin: horizontal ? "-140px 0px -60% 0px" : "-84px 0px -60% 0px" },
    );
    targets.forEach((el) => observer.observe(el));
    return () => observer.disconnect();
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
