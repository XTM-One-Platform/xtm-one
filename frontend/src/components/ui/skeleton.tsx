// SPDX-License-Identifier: Apache-2.0

import { cn } from "@/lib/utils";

interface SkeletonProps {
  className?: string;
}

export function Skeleton({ className }: SkeletonProps) {
  return (
    <div className={cn("animate-pulse rounded-sm bg-elevation-hover", className)} />
  );
}

/** A full card skeleton with title, description, and content areas. */
export function CardSkeleton({ className }: SkeletonProps) {
  return (
    <div className={cn("rounded-sm layer-1 bg-elevation-default p-6 space-y-4", className)}>
      <div className="flex items-center gap-3">
        <Skeleton className="h-10 w-10 rounded-sm" />
        <div className="space-y-2 flex-1">
          <Skeleton className="h-4 w-1/3" />
          <Skeleton className="h-3 w-1/5" />
        </div>
      </div>
      <Skeleton className="h-3 w-full" />
      <Skeleton className="h-3 w-4/5" />
    </div>
  );
}

interface ListSkeletonProps {
  /** What is loading, for assistive technology (already translated). */
  label: string;
  /** How many placeholder rows to draw. */
  rows?: number;
  /** `line`: compact rows (an icon tile, a title line, a badge line), for a
   *  sidebar list. `card`: bordered card rows, for the list slot of a page
   *  whose header and toolbar stay mounted while the list loads. */
  variant?: "line" | "card";
  className?: string;
  "data-testid"?: string;
}

/**
 * A list on its way, announced as one `role="status"` region. Shown while the
 * first read of a list is in flight, so an empty state is only ever drawn for
 * a list that was read and is empty. It never paints a page-title
 * block: a `PageSkeleton` under a mounted toolbar draws a second, fake title.
 * A `line` row (a 28 px icon tile, a title line, a badge line)
 * measures what a loaded conversation row measures (58 px), so nothing moves
 * when the list lands.
 */
export function ListSkeleton({
  label,
  rows = 5,
  variant = "line",
  className,
  "data-testid": testId,
}: ListSkeletonProps) {
  return (
    <div
      role="status"
      aria-busy="true"
      aria-label={label}
      data-testid={testId}
      className={cn(variant === "card" ? "space-y-3" : "space-y-0.5", className)}
    >
      {Array.from({ length: rows }).map((_, i) =>
        variant === "card" ? (
          // Same outline as a loaded row, so the border does not pop in.
          <CardSkeleton key={i} className="border border-elevation-subtle-soft" />
        ) : (
          <div key={i} className="flex items-start gap-2.5 rounded-sm px-3 py-2.5">
            <Skeleton className="mt-0.5 h-7 w-7 shrink-0" />
            <div className="min-w-0 flex-1">
              {/* The title's 18 px line box, then the badge line (mt-1 h-4). */}
              <div className="flex h-[18px] items-center">
                <Skeleton className={cn("h-3.5", i % 2 === 0 ? "w-3/4" : "w-1/2")} />
              </div>
              <Skeleton className="mt-1 h-4 w-1/3" />
            </div>
          </div>
        ),
      )}
    </div>
  );
}

/** A page-level skeleton with header and cards grid.
 *
 *  Deliberately NOT animated with `animate-fade-in`: a page load can chain
 *  several skeleton phases (route chunk Suspense fallback, then the page's
 *  own data-loading gate), each mounting a fresh PageSkeleton instance. An
 *  entrance animation restarts on every remount and makes each phase read
 *  as a separate "render flash" — without it, consecutive skeleton
 *  phases are visually continuous. */
export function PageSkeleton({ cards = 4 }: { cards?: number }) {
  return (
    <div className="space-y-8">
      <div className="space-y-2">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-4 w-80" />
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        {Array.from({ length: cards }).map((_, i) => (
          <CardSkeleton key={i} />
        ))}
      </div>
    </div>
  );
}
