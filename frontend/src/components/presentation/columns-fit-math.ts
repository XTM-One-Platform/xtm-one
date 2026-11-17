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
 * When a `columns` block keeps its columns side by side - the pure
 * half, run under Node's test runner (`columns-fit.test.ts`); the DOM half is
 * `columns-fit.ts`.
 *
 * The columns are equal tracks, so they fit side by side when the WIDEST
 * column's minimum width - its content's min-content width: its longest word,
 * a table's columns, a row of logos at their own size - fits one track. Only
 * four columns ever wrap, onto two rows of two (what four columns always
 * drew): three have no balanced wrap (two and one strands a
 * column), and two or three that do not fit keep squeezing their content, as
 * they always did.
 */

/** The one column count that falls back to wrapping. */
export const WRAPPING_COLUMN_COUNT = 4;
/** The columns a row of a wrapped block holds. */
export const WRAPPED_PER_ROW = 2;
/** Layout metrics are whole pixels: a column a pixel short of its content still fits. */
export const COLUMN_FIT_TOLERANCE_PX = 1;

/** The width of one of `count` equal tracks of a block `width` wide, `gap` between them. */
export function columnTrack(width: number, count: number, gap: number): number {
  return (width - (count - 1) * gap) / count;
}

/**
 * The columns a row of the block holds: all `count` of them, or
 * `WRAPPED_PER_ROW` when four do not fit because the widest column's content
 * (`need`) is wider than a track. A block that measures nothing (`width` 0, a
 * slide reveal keeps hidden) keeps its columns.
 */
export function columnsPerRow(count: number, need: number, width: number, gap: number): number {
  if (count !== WRAPPING_COLUMN_COUNT || width <= 0) return count;
  return need <= columnTrack(width, count, gap) + COLUMN_FIT_TOLERANCE_PX ? count : WRAPPED_PER_ROW;
}

/**
 * The rows columns whose blocks line up (`equal_rows`) are drawn on:
 * one per block of the longest column, so the Nth block of every column
 * shares the Nth row and its height.
 */
export function columnRowCount(columns: readonly (readonly unknown[])[]): number {
  return Math.max(1, ...columns.map((column) => column.length));
}
