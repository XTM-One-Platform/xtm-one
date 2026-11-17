// SPDX-License-Identifier: Apache-2.0

import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

/**
 * One row of dashboard widget cards: every card is as tall as the row's tallest
 * (Sam, 13 Nov 2026, #11470: "All widgets should have the same height").
 *
 * Each child is a grid item, so the grid stretches it to the row's height; the
 * child is a flex column whose last part (the card's content) takes the room left,
 * so a short card ends with the others and its footer (a table's pager) can sit at
 * the bottom. Every child must be the card itself (`Card`, `data-slot="card"`),
 * never a wrapper around it: a wrapper would stretch while the card inside it ends
 * short. A span goes on the card's own `className`. A stretched card body keeps
 * its content top-aligned (never `items-center` on it), so the labels and values
 * of one line share one top (#11470, UX round 1 B1).
 *
 * The page gives the columns (`className="lg:grid-cols-2"`); the row owns the gap
 * and the equal heights.
 */
export function WidgetRow({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      data-widget-row=""
      className={cn("grid gap-4 *:flex *:min-w-0 *:flex-col [&>*>:last-child]:flex-1", className)}
      {...props}
    />
  );
}
