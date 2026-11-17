/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * How a list of rows, a distribution list and a grid of cards are laid out,
 * apart from their drawing.
 *
 * A list (`listStyle`) is drawn:
 * - `compact`: XTM One's list, the first column linked, the rest after it;
 * - `table`: a 42 px header (12 px, weight 700), rows of `rowHeight` px
 *   (48 by default) with a divider, 13 px cells of equal width with ellipsis,
 *   chips for typed cells, and a 42 px arrow column (OpenCTI's DataTable,
 *   widget variant); a row that carries an icon leads with a 56 px icon column;
 *   the arrow is muted unless `arrowColor` names a colour;
 * - `rows`: no header, a leading icon (none when the row has no icon), cells
 *   of `100 / n` % each (10% at least), 14 px, a hover accent (a 2 px inset bar)
 *   and a trailing arrow on the rows that open somewhere (OpenAEV's list);
 * - `cards`: `cardsPerRow` cards a row (3), 24 px apart, each an avatar of the
 *   row's icon, its name and "Updated on" its date (OpenCTI's bookmarks).
 *
 * A distribution list (`listStyle`) is `bars` (XTM One's) or `rows`: 50 px
 * rows with a divider, the item's icon in its colour, its label with
 * ellipsis and its value in SI form, 18 px, weight 600, in the accent
 * (OpenCTI's distribution list).
 *
 * Chips that do not fit their cell end with a `+N` chip (OpenCTI's labels):
 * `fitChips` says how many fit, from the width each takes.
 *
 * Pure, type-only imports: runs on `node --test`.
 */
import type { WidgetRowsData } from "@/types";

const LIST_STYLES = ["compact", "table", "rows", "cards"] as const;
type ListStyle = (typeof LIST_STYLES)[number];
const DISTRIBUTION_STYLES = ["bars", "rows"] as const;
export type DistributionStyle = (typeof DISTRIBUTION_STYLES)[number];

/** A table's header height and its arrow column, a rows list's least column share (%), a leading icon column (px). */
export const TABLE_HEADER = 42;
export const ARROW_COLUMN = 42;
export const ICON_COLUMN = 56;
export const MIN_COLUMN_PERCENT = 10;
const DEFAULT_ROW_HEIGHT: Record<ListStyle, number> = { compact: 36, table: 48, rows: 50, cards: 72 };

export interface ListOptions {
  listStyle: ListStyle;
  rowHeight: number;
  cardsPerRow: number;
  /** The arrow's colour, as named in the options (resolved where it is drawn). */
  arrowColor?: string;
}

const clamp = (value: unknown, min: number, max: number): number | undefined =>
  typeof value === "number" && Number.isFinite(value) ? Math.min(max, Math.max(min, Math.round(value))) : undefined;

export function readListOptions(options: Record<string, unknown>): ListOptions {
  const listStyle = (LIST_STYLES as readonly unknown[]).includes(options.listStyle) ? (options.listStyle as ListStyle) : "compact";
  return {
    listStyle,
    rowHeight: clamp(options.rowHeight, 32, 64) ?? DEFAULT_ROW_HEIGHT[listStyle],
    cardsPerRow: clamp(options.cardsPerRow, 1, 6) ?? 3,
    ...(typeof options.arrowColor === "string" ? { arrowColor: options.arrowColor } : {}),
  };
}

export const readDistributionStyle = (options: Record<string, unknown>): DistributionStyle =>
  (DISTRIBUTION_STYLES as readonly unknown[]).includes(options.listStyle) ? (options.listStyle as DistributionStyle) : "bars";

/** Each of `count` columns' share of a rows list, in percent: equal, 10% at least (`ColumnStyles`). */
export const columnPercent = (count: number): number => (count <= 0 ? 100 : Math.max(MIN_COLUMN_PERCENT, 100 / count));

/** Whether a table leads with an icon column: a row carries an icon. */
export const hasIconColumn = (rows: WidgetRowsData["rows"]): boolean => rows.some((row) => typeof row._icon === "string" && row._icon !== "");

/** A chip's width estimate (px) at 12 px: its label (7 px a character), its padding (16) and its icon (20). */
export const chipWidth = (label: string, icon = false): number => Math.min(160, label.length * 7) + 16 + (icon ? 20 : 0);

/**
 * How many chips of `widths` fit in `room` px, `gap` apart, keeping room for a
 * `+N` chip of `overflowWidth` px when some are left out; at least one is shown.
 */
export function fitChips(widths: readonly number[], room: number, gap = 8, overflowWidth = 36): number {
  let used = 0;
  for (let index = 0; index < widths.length; index += 1) {
    const next = used + (index > 0 ? gap : 0) + widths[index];
    const rest = widths.length - index - 1;
    if (next + (rest > 0 ? gap + overflowWidth : 0) > room) return Math.max(1, index);
    used = next;
  }
  return widths.length;
}

/** The rows of a grid of cards, `perRow` cards each, in order. */
export function cardRows<T>(items: readonly T[], perRow: number): T[][] {
  const size = Math.max(1, Math.trunc(perRow));
  const out: T[][] = [];
  for (let index = 0; index < items.length; index += size) out.push(items.slice(index, index + size));
  return out;
}

/** A card's title, its icon and the date it was last updated: the row's name column, its icon, its first date column. */
export function cardOf(data: WidgetRowsData, row: WidgetRowsData["rows"][number]): { title: string; date: string | null; icon: string | null } {
  const name = data.columns.find((column) => column.key === "name") ?? data.columns.find((column) => !column.kind);
  const date = data.columns.find((column) => column.kind === "date" || column.kind === "datetime");
  const title = name && typeof row[name.key] === "string" ? (row[name.key] as string) : "";
  const when = date && typeof row[date.key] === "string" ? (row[date.key] as string) : null;
  return { title: title || "-", date: when, icon: typeof row._icon === "string" ? row._icon : null };
}
