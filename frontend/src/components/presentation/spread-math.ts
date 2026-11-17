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
 * The arithmetic of the bounded spread, free of React and the DOM so
 * `spread.test.ts` can pin it under Node's runner. `spread.ts` is the DOM half.
 */

/**
 * How far one gap between the ROWS of a `fill` grid block may grow past the
 * block's own row gap, in canvas px. A list's 16px gap grows to 64px at most -
 * about 1.6 lines at the body size - which reads as a relaxed list, not as
 * orphaned lines.
 */
export const SPREAD_ROW_MAX_EXTRA_PX = 48;
/**
 * How far one gap between the BLOCKS of a `distribute` run may grow past the
 * run's 32px gap: 96px at most between a list and the callout under it.
 */
export const SPREAD_BLOCK_MAX_EXTRA_PX = 64;
/**
 * How far one gap between the ROWS of a `distribute` agenda may grow past the
 * agenda's 24px row gap: 160px at most, about two rows of a titled item with
 * its one-line description (81px on the 1080px canvas). On a 16:9 body of
 * about 800px, two or three such rows stop there and the group is centred in
 * the body (`margin-block: auto` on the capped list); four rows need about
 * 157px per gap to reach the floor and five about 98px, so they still fill
 * the body as before.
 */
export const SPREAD_AGENDA_ROW_MAX_EXTRA_PX = 136;
/**
 * The same cap for an agenda whose `list` items carry ICONS: their tiles read
 * as a feature list, so a gap grows from the agenda's 24px to 46px (48px with
 * the rounding slack) at most - 1.5x the feature list's 32px row gap. Eight
 * icon items in two columns then
 * sit as a centred group (`margin-block: auto`) instead of four rows pushed to
 * the body's edges.
 */
export const SPREAD_AGENDA_ICON_ROW_MAX_EXTRA_PX = 22;
/**
 * How far one ROW of `fill` numbered cards (the takeaways body) may
 * grow past its natural height: the cards themselves take the extra - the
 * poster gap between the number and the title - not the gaps between them. A
 * row of three cards around 300px tall reaches about 400px, a card rather
 * than a pillar, while two rows (four to six takeaways) still share the
 * whole body as before.
 */
export const SPREAD_CARD_MAX_EXTRA_PX = 96;
/**
 * `offsetHeight` is a whole pixel, so a row measures up to half a pixel short
 * of its true height. The cap carries this slack so a row is never squeezed
 * out of its box by rounding alone: with two or more rows the `maxExtra` the
 * cap adds per gap dwarfs the deficit (twelve rows lose at most 6px against
 * 11 x 48px of extras - a spread gap a fraction of a pixel narrower, never an
 * overflow), and a single row, which has no gap to grow, can lose at most the
 * half pixel this covers. Pinned by `spread.test.ts`.
 */
export const SPREAD_ROUNDING_SLACK_PX = 2;
/** Children whose top edges are within this many px share a row. */
const ROW_TOLERANCE_PX = 1;

/** One child of the spreading element, in layout px. */
export interface SpreadBox {
  top: number;
  height: number;
}

/**
 * Group boxes that share a top edge into rows (a two-column list, a metrics
 * grid) and return each row's height, top to bottom. A run of blocks is a
 * column, so every child is a row of its own.
 */
export function rowHeights(boxes: readonly SpreadBox[]): number[] {
  const rows: { top: number; height: number }[] = [];
  for (const box of [...boxes].sort((a, b) => a.top - b.top)) {
    const row = rows.find((r) => Math.abs(r.top - box.top) <= ROW_TOLERANCE_PX);
    if (row) row.height = Math.max(row.height, box.height);
    else rows.push({ top: box.top, height: box.height });
  }
  return rows.map((row) => row.height);
}

/**
 * The blocks a `distribute` run spreads over the WHOLE body, uncapped: cards,
 * timelines and grids are drawn to be read apart, so a run made only of them
 * and asked to be distributed reaches the body's floor (the block gap cap and
 * the per-gap `SPREAD_BLOCK_MAX_EXTRA_PX` keep bounding any run that holds
 * text, a list or a quote).
 */
const SPREAD_FREE_BLOCKS: ReadonlySet<string> = new Set([
  "callout",
  "phase_card",
  "numbered_cards",
  "feature_list",
  "timeline",
  "process_flow",
  "logo_grid",
  "logo_wall",
  "pricing_tiers",
  "personas",
  "profiles",
  "metrics_grid",
  "comparison",
  "challenge_answer",
]);

/**
 * Blocks that only caption a card or a grid beside them in a column (a heading
 * over a logo grid): they do not stop a `columns` block from spreading free.
 */
const SPREAD_CAPTION_BLOCKS: ReadonlySet<string> = new Set(["heading"]);

/** What `spreadsFree` reads of a block: its type and, for `columns`, what it holds. */
interface SpreadBlock {
  type: string;
  columns?: unknown;
}

function isSpreadBlock(value: unknown): value is SpreadBlock {
  return typeof value === "object" && value !== null && typeof (value as SpreadBlock).type === "string";
}

/** The blocks a `columns` block holds, every column flattened (`null` for any other block). */
function heldBlocks(block: SpreadBlock): SpreadBlock[] | null {
  if (block.type !== "columns" || !Array.isArray(block.columns)) return null;
  return block.columns.flatMap((column: unknown) => (Array.isArray(column) ? column.filter(isSpreadBlock) : []));
}

/**
 * Whether one block of a `distribute` run spreads free. A `columns` block
 * counts by what it holds: columns of cards, timelines or grids (a heading over
 * them allowed) spread like the cards themselves; a column holding text, a
 * list or a quote keeps the run bounded.
 */
function blockSpreadsFree(block: SpreadBlock): boolean {
  if (SPREAD_FREE_BLOCKS.has(block.type)) return true;
  const held = heldBlocks(block);
  if (!held) return false;
  return (
    held.some(blockSpreadsFree) &&
    held.every((inner) => SPREAD_CAPTION_BLOCKS.has(inner.type) || blockSpreadsFree(inner))
  );
}

/** Whether a `distribute` run of these blocks spreads to the floor, uncapped. */
export function spreadsFree(blocks: readonly SpreadBlock[] | undefined): boolean {
  return !!blocks?.length && blocks.every(blockSpreadsFree);
}

/**
 * What takes the extra height of a spread: the GAPS between the rows
 * (`space-between` - a list, a run of blocks, a table's row boundaries) or the
 * ROWS themselves (numbered cards sharing the height).
 */
export type SpreadGrowth = "gaps" | "rows";

/**
 * The height the element may grow to: its natural height - the rows at their
 * content size, the base `gap` between them, `frame` for the padding and
 * border a `border-box` element counts inside its max-height - plus one
 * `maxExtra` per gap (or per row, when the rows are what `grows`) and the
 * rounding slack. `null` when there is nothing to measure (no visible row),
 * which leaves the element alone.
 */
export function spreadCap(
  rows: readonly number[],
  gap: number,
  maxExtra: number,
  frame = 0,
  grows: SpreadGrowth = "gaps",
): number | null {
  if (rows.length === 0) return null;
  const gaps = rows.length - 1;
  const natural = rows.reduce((sum, height) => sum + height, 0) + gaps * gap + frame;
  const growing = grows === "rows" ? rows.length : gaps;
  return natural + growing * maxExtra + SPREAD_ROUNDING_SLACK_PX;
}
