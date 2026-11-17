// SPDX-License-Identifier: Apache-2.0

/**
 * Column spans of the overview grid (#11838), apart from the components so the
 * component file exports components only (fast refresh).
 *
 * The grid has one column count per breakpoint (`OVERVIEW_GRID_COLUMNS`: 1, then
 * 2 at `sm`, 4 at `md`, 6 at `lg`, 12 at `2xl`) and each tile declares how many
 * of those columns it spans at each breakpoint. A breakpoint a tile does not
 * name keeps the span of the narrower one; the base span is 1.
 */

/** The breakpoints a tile can set its span at, narrowest first. */
const BREAKPOINTS = ["sm", "md", "lg", "2xl"] as const;
type OverviewBreakpoint = (typeof BREAKPOINTS)[number];

/** How many columns the grid has at each breakpoint (base: 1). */
export const OVERVIEW_GRID_COLUMNS = { base: 1, sm: 2, md: 4, lg: 6, "2xl": 12 } as const;

/** Columns a tile spans at each breakpoint; an omitted breakpoint inherits the narrower one. */
export type OverviewSpan = Partial<Record<OverviewBreakpoint, number>>;

// Literal class names, so the Tailwind scanner sees every one of them.
const SPAN_CLASS: Record<OverviewBreakpoint, Record<number, string>> = {
  sm: { 1: "sm:col-span-1", 2: "sm:col-span-2" },
  md: { 1: "md:col-span-1", 2: "md:col-span-2", 3: "md:col-span-3", 4: "md:col-span-4" },
  lg: { 1: "lg:col-span-1", 2: "lg:col-span-2", 3: "lg:col-span-3", 4: "lg:col-span-4", 5: "lg:col-span-5", 6: "lg:col-span-6" },
  "2xl": {
    1: "2xl:col-span-1",
    2: "2xl:col-span-2",
    3: "2xl:col-span-3",
    4: "2xl:col-span-4",
    5: "2xl:col-span-5",
    6: "2xl:col-span-6",
    7: "2xl:col-span-7",
    8: "2xl:col-span-8",
    9: "2xl:col-span-9",
    10: "2xl:col-span-10",
    11: "2xl:col-span-11",
    12: "2xl:col-span-12",
  },
};

/** The column classes of a span; a span past the grid's columns at a breakpoint is clamped to them. */
export function overviewSpanClasses(span: OverviewSpan = {}): string {
  return BREAKPOINTS.filter((bp) => span[bp] !== undefined)
    .map((bp) => SPAN_CLASS[bp][Math.max(1, Math.min(span[bp] as number, OVERVIEW_GRID_COLUMNS[bp]))])
    .join(" ");
}

/**
 * The span of a tile at every breakpoint, inherited from the narrower one when
 * it is not set (what the grid actually lays out). Tests use it to check that a
 * page's rows are full.
 */
export function resolvedOverviewSpans(span: OverviewSpan = {}): Record<"base" | OverviewBreakpoint, number> {
  const resolved = { base: 1 } as Record<"base" | OverviewBreakpoint, number>;
  let current = 1;
  for (const bp of BREAKPOINTS) {
    current = Math.min(span[bp] ?? current, OVERVIEW_GRID_COLUMNS[bp]);
    resolved[bp] = current;
  }
  return resolved;
}
