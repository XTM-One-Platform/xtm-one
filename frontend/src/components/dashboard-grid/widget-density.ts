/**
 * How much of itself a dashboard widget draws at a given grid size: the parts
 * every dashboard shares (the cell's height, how many list rows fit, the KPI
 * cards). Each dashboard adds the rules of its own widgets (the CTEM command
 * center's `pages/xtm-one/ctem/widget-density.ts`, the Copilot dashboard's
 * `pages/dashboard/copilot-density.ts`).
 *
 * The grid hands each widget its size in grid units (`DashboardGrid`, through
 * `WidgetSizeContext`); a widget drops its secondary parts, in a fixed order
 * of importance, rather than clipping them out of sight. `null` is a widget
 * drawn at its natural height (the stacked layout of a narrow window): it
 * draws everything.
 */
import { GRID_MARGIN, GRID_ROW_HEIGHT } from "./layout.ts";

/** A widget's size on the grid, in columns and rows. */
export interface WidgetSize {
  cols: number;
  rows: number;
}

/** The height in pixels of a widget `rows` grid rows tall. */
export function cellHeight(rows: number): number {
  return rows * GRID_ROW_HEIGHT + Math.max(rows - 1, 0) * GRID_MARGIN;
}

export interface KpiDensity {
  /** Drop the secondary line under the value. */
  hideSub: boolean;
  /** Move the period-over-period badge next to the value: the label row is
   *  too narrow to hold both. */
  badgeByValue: boolean;
  /** Let the label take two lines instead of cutting it: a narrow card's
   *  label box is too short for most labels on one line ("Leaked credentials",
   *  "Identifiants divulgués"), and nothing would show the cut text. */
  wrapLabel: boolean;
}

/** A KPI card: label, value, secondary line, comparison badge. */
export function kpiDensity(size: WidgetSize | null): KpiDensity {
  if (!size) return { hideSub: false, badgeByValue: false, wrapLabel: false };
  const hideSub = size.rows <= 3;
  const narrow = size.cols <= 2;
  return { hideSub, badgeByValue: narrow, wrapLabel: narrow };
}

export interface KpiLayout {
  /** The card's vertical placement of its content. */
  card: string;
  /** The content row: alignment and padding. */
  content: string;
  /** The label row (label, info icon, badge). */
  labelRow: string;
  /** The label paragraph. */
  label: string;
}

/** The classes that keep every value of a KPI row on one line.
 *
 *  In the grid, the cards of a row sit side by side with labels of one or two
 *  lines; centred content would move each value up or down with its label
 *  (#9426 UX round 3 B1: an 11 px spread across the CTEM strip, 35 to 49 px
 *  on the home tiles). So every card in a cell top-aligns its content with one
 *  padding and reserves a two-line label block, whatever its width: every
 *  value of the row starts at the same y. A narrow card wraps its label in
 *  that block; a wide one keeps it on one line with the wide tracking.
 *
 *  Out of the grid (the stacked layout, one card per line) nothing sits beside
 *  the card and the content stays centred. */
export function kpiLayout(size: WidgetSize | null): KpiLayout {
  const density = kpiDensity(size);
  if (!size) {
    return {
      card: "justify-center",
      content: density.hideSub ? "items-center py-3" : "items-center pt-5 pb-4",
      labelRow: "items-center min-h-4",
      label: "truncate tracking-wider",
    };
  }
  return {
    card: "justify-start",
    content: "items-start py-3",
    labelRow: "items-start",
    label: `min-h-[2lh] leading-tight ${density.wrapLabel ? "line-clamp-2 break-words" : "truncate tracking-wider"}`,
  };
}

/** Whether a KPI card draws its decorative icon. A narrow card puts its badge
 *  next to the value; a sample card's badge is a 64 px chip that the value
 *  column cannot hold beside the 32 px icon, so the icon gives way rather than
 *  sit under the badge. */
export function kpiShowsIcon(density: KpiDensity, sample: boolean): boolean {
  return !(density.badgeByValue && sample);
}

/** Rows of `rowHeight` pixels a list fits in a widget, once `chrome` pixels of
 *  header, footer and padding are taken: between `min` and `max`. */
export function listCapacity(
  size: WidgetSize | null,
  { chrome, rowHeight, min = 1, max }: { chrome: number; rowHeight: number; min?: number; max: number },
): number {
  if (!size) return max;
  const fits = Math.floor((cellHeight(size.rows) - chrome) / rowHeight);
  return Math.max(min, Math.min(max, fits));
}
