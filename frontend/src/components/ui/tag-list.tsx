// SPDX-License-Identifier: Apache-2.0

/**
 * Inline tag badges with a `+N` overflow marker: the shared tag display used
 * in entity card footers and list rows.
 *
 * The row shrinks with its container. A badge truncates its label and
 * carries the full tag as its title, but never shrinks below a readable width
 * (about three characters). When the badges that remain still do not fit, the
 * last ones fold into the `+N` marker, which names every tag it hides.
 *
 * Only the badges sit in the part of the row that clips; the `+N` marker sits
 * beside it, so the row is never narrower than its marker and the marker is
 * never cut, even when no badge fits. Callers place it with `ml-auto` and never
 * `shrink-0`; a sibling that should give up space first takes a higher shrink.
 *
 * In a flex row the root must be able to shrink below the width of its badges:
 * a flex item's automatic minimum is its min-content width, which is the sum
 * of every badge, so without `min-w-0` the row never gets squeezed, the fold
 * never sees an overflow and the badges run past the card. Once every badge has folded, the root keeps the marker's width.
 */
import { useLayoutEffect, useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { useTriggerTabIndex } from "@/components/ui/row-target-context";
import { Tip } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

interface TagListProps {
  tags?: string[] | null;
  /** How many tags to render at most before collapsing the rest into `+N`. */
  max?: number;
  /**
   * The narrowest a badge may get before it folds into `+N`: `short` (the
   * default, about three characters) for a row of short tags, `readable`
   * (64 px) for names that are unreadable when cut to a letter (organization
   * names in a table cell), `whole` for names that must never be cut: every
   * badge keeps its full width and the last ones fold into `+N` while the row
   * overflows; the first badge never folds and truncates only once it is
   * alone and still wider than the row (the primary of a scope stays shown).
   */
  badgeMin?: keyof typeof BADGE_MIN_CLASS;
  className?: string;
}

const BADGE_MIN_CLASS = { short: "min-w-8", readable: "min-w-16", whole: "min-w-16" } as const;

/**
 * The badges to keep after an overflowing pass: the leading badges whose right
 * edge falls inside the row, at least one fewer than now and never under
 * `floor`. Badges have different widths, so the count is read from each
 * badge's box; only when no box has a width (no layout, as in a test DOM)
 * does it fall back to the share the row holds, rounded down.
 */
function nextVisible(row: HTMLElement, visible: number, floor: number): number {
  const { scrollWidth, clientWidth } = row;
  const left = row.getBoundingClientRect().left;
  const boxes = Array.from(row.children, (badge) => badge.getBoundingClientRect());
  let fits: number;
  if (boxes.some((box) => box.width > 0)) {
    const edge = left + clientWidth + 0.5;
    const firstOut = boxes.findIndex((box) => box.right > edge);
    fits = firstOut === -1 ? boxes.length : firstOut;
  } else {
    fits = scrollWidth > 0 ? Math.floor((visible * clientWidth) / scrollWidth) : visible - 1;
  }
  return Math.max(floor, Math.min(visible - 1, fits));
}

export function TagList({ tags, max = 3, badgeMin = "short", className }: TagListProps) {
  const count = tags?.length ?? 0;
  const limit = Math.min(max, count);
  const listRef = useRef<HTMLDivElement>(null);
  const badgesRef = useRef<HTMLDivElement>(null);
  const whole = badgeMin === "whole";
  // In `whole` mode the first badge never folds: it is the one that names the set.
  const floor = whole && limit > 0 ? 1 : 0;
  const [visible, setVisible] = useState(limit);
  // Bumped when the space the row gets changes, so the fold below measures again.
  const [layoutPass, setLayoutPass] = useState(0);
  const tagKey = tags?.join("\u0000") ?? "";

  // A new tag set or a new max starts from the widest layout again.
  useLayoutEffect(() => {
    setVisible(limit);
  }, [limit, tagKey]);

  // Fold badges into `+N` while the badges overflow their part of the row: they
  // have already shrunk to their minimum width when this happens. Each pass
  // keeps the leading badges that end inside the row, at once:
  // folding one badge per pass chained one synchronous update per tag, and a
  // row of 60 group names went past React's nested-update limit and blanked
  // the whole Analytics page ("Maximum update depth exceeded").
  useLayoutEffect(() => {
    const badges = badgesRef.current;
    if (!badges || visible <= floor) return;
    if (badges.scrollWidth > badges.clientWidth) {
      const next = nextVisible(badges, visible, floor);
      setVisible(next);
    }
  }, [visible, layoutPass, limit, tagKey, floor]);

  // The space the row gets comes from its parent (the card footer): when that
  // width changes, measure again from the widest layout. The row itself is
  // not observed, so folding a badge never restarts the measure.
  useLayoutEffect(() => {
    const parent = listRef.current?.parentElement;
    if (!parent || typeof ResizeObserver === "undefined") return;
    let width = parent.clientWidth;
    const observer = new ResizeObserver(() => {
      if (parent.clientWidth === width) return;
      width = parent.clientWidth;
      setVisible(limit);
      setLayoutPass((pass) => pass + 1);
    });
    observer.observe(parent);
    return () => observer.disconnect();
  }, [limit]);
  // In a routed data table row the row is the one tab stop (#12070).
  const moreTabIndex = useTriggerTabIndex();

  if (!tags || count === 0) return null;
  const shown = tags.slice(0, Math.min(visible, limit));
  const hidden = tags.slice(shown.length);
  return (
    <div
      ref={listRef}
      data-tag-list=""
      className={cn("flex items-center gap-1", shown.length > 0 ? "min-w-0" : "shrink-0", className)}
    >
      {shown.length > 0 && (
        <div ref={badgesRef} data-tag-row="" className="flex min-w-0 flex-1 items-center gap-1 overflow-hidden">
          {shown.map((tag) => (
            <Badge
              key={tag}
              variant="outline"
              title={tag}
              // `whole`: a badge keeps its full width while it shares the row, so
              // the row overflows and folds instead of cutting two names.
              className={cn("content-caption px-1.5 py-0", BADGE_MIN_CLASS[badgeMin], whole && shown.length > 1 && "shrink-0")}
            >
              <span className="truncate">{tag}</span>
            </Badge>
          ))}
        </div>
      )}
      {hidden.length > 0 && (
        // The hidden names are reachable by keyboard and touch: the marker is
        // focusable, names them for assistive tech and opens the shared Tip.
        <Tip content={hidden.join(", ")}>
          <span
            tabIndex={moreTabIndex}
            role="note"
            aria-label={hidden.join(", ")}
            data-tag-more=""
            className="content-caption shrink-0 whitespace-nowrap rounded-sm text-default-secondary outline-none focus-visible:ring-2 focus-visible:ring-focus"
          >
            +{hidden.length}
          </span>
        </Tip>
      )}
    </div>
  );
}
