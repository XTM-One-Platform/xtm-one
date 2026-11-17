// SPDX-License-Identifier: Apache-2.0

import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

/**
 * The card grid of an asset list in grid view (agents, prompts, skills, tools, ...): the
 * loaded grid and its loading grid share this one class, so the cards land where their
 * placeholders stood (#11345).
 */
const CARD_GRID = "grid gap-4 md:grid-cols-2 lg:grid-cols-3";

/** An asset card's height on a 1440 window, for the loading cards to fill the window. */
const CARD_HEIGHT_PX = 140;
/** Cards per row at lg, the widest step of CARD_GRID. */
const CARDS_PER_ROW = 3;
/** The cap of a list that reads every card at once (no pager): ten rows of three. */
const UNPAGED_CARD_CAP = 30;

/**
 * How many loading cards the first read draws: a window's worth of rows at the card
 * height, never more than a page (a list with no pager passes nothing).
 */
export function cardSkeletonCount(pageSize = UNPAGED_CARD_CAP, viewportHeight?: number, perRow = CARDS_PER_ROW): number {
  const height = viewportHeight ?? (typeof window === "undefined" ? 0 : window.innerHeight);
  const rows = Math.ceil(height / CARD_HEIGHT_PX);
  return Math.max(1, Math.min(pageSize, rows * perRow || pageSize));
}

/**
 * The footer row of a loaded card, by its tallest content: `control` a 24 px badge or icon
 * button (agents, skills), `tags` the 17 px tag chips (prompts), `caption` one 15 px caption
 * line (variables, flows).
 */
export type CardSkeletonMeta = "control" | "tags" | "caption";

const META_ROW_HEIGHT: Record<CardSkeletonMeta, string> = { control: "h-6", tags: "h-[17px]", caption: "h-[15px]" };

/**
 * One asset card with its content greyed out, in the loaded card's anatomy: the same Card
 * surface and p-4 padding, the 40 px icon tile, the name and description lines, the tag
 * row when the loaded card has one, then the metadata footer under its hairline at the
 * loaded footer's height.
 */
function EntityCardSkeleton({ index = 0, tags = true, meta = "control" }: { index?: number; tags?: boolean; meta?: CardSkeletonMeta }) {
  return (
    <Card data-testid="entity-card-skeleton" className="h-full border-elevation-subtle-soft">
      <CardContent className="p-4 flex flex-col h-full">
        <div data-slot="card-head" className="flex items-center gap-3">
          <Skeleton className="size-10 shrink-0" />
          {/* The name's 21 px line and the description's 18 px line: 39 px, inside the 40 px tile row. */}
          <div className="min-w-0 flex-1">
            <div className="flex h-[21px] items-center">
              <Skeleton className={cn("h-3.5", index % 2 === 0 ? "w-3/4" : "w-3/5")} />
            </div>
            <div className="flex h-[18px] items-center">
              <Skeleton className="h-3 w-1/2" />
            </div>
          </div>
        </div>
        {tags && (
          <div data-slot="card-tags" className="flex items-center gap-1 mt-3 h-[18px]">
            <Skeleton className="h-3 w-16" />
          </div>
        )}
        <div data-slot="card-meta" className="flex items-center gap-3 mt-auto pt-3 border-t border-elevation-subtle-soft">
          <div data-meta={meta} className={cn("flex flex-1 items-center gap-3", META_ROW_HEIGHT[meta])}>
            <Skeleton className="h-3 w-24" />
            {meta === "control" && (
              <div data-slot="card-meta-control" className="ml-auto">
                <Skeleton className="size-6" />
              </div>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * The loading grid of a card list: `count` card skeletons in the grid the loaded cards use
 * (CARD_GRID unless the page names its own), announced as one status region. A page whose
 * cards have no tag row or a one-line footer says so (`tags`, `meta`), so each placeholder
 * is as tall as the card that replaces it.
 */
export function CardGridSkeleton({
  count,
  label,
  className = CARD_GRID,
  tags = true,
  meta = "control",
}: {
  count: number;
  /** What is loading, for assistive technology (already translated). */
  label: string;
  /** The page's own grid class, when its cards do not sit on CARD_GRID. */
  className?: string;
  /** The loaded card draws a tag row under its head (default true). */
  tags?: boolean;
  /** The loaded card's footer row: a 24 px control (default), 17 px tag chips or a 15 px caption line. */
  meta?: CardSkeletonMeta;
}) {
  return (
    <div role="status" aria-busy="true" aria-label={label} data-testid="card-grid-skeleton" className={className}>
      {Array.from({ length: count }).map((_, index) => (
        <EntityCardSkeleton key={index} index={index} tags={tags} meta={meta} />
      ))}
    </div>
  );
}
