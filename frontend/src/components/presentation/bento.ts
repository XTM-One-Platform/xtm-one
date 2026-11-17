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
 * The bento grid of a content slide (spec "PPT
 * Design System" 3.7): the slide's blocks ARE the cells of a grid that fills
 * the body. The first image block is the large cell (left, both rows); the
 * other cells fill the right in reading order. Pure, so the unit tests and
 * the renderer read one placement.
 *
 *   3 cells  | big | a |     4 cells  | big | a | b |     5 cells  | big | a | b |
 *            | big | b |              | big |  c    |              | big | c | d |
 *
 * Gap 24 px (`--deck-tile-gap`) and radius 12 px (`--deck-radius`) come from
 * the template's design tokens, so a template declaring none draws the same
 * grid on its own scale.
 */
import type { DeckBlock } from "@/types";

interface BentoCell {
  /** Index of the block in `slide.blocks` (the editing path). */
  index: number;
  /** `big` = the large image cell; `wide` = a cell spanning two columns. */
  role: "big" | "wide" | "cell";
  /** The cell's grid area, `row-start / column-start / row-end / column-end`. */
  area: string;
}

export interface BentoLayout {
  /** Grid columns as a `grid-template-columns` value. */
  columns: string;
  cells: BentoCell[];
}

/**
 * Where each block goes. `null` when the blocks are no bento (fewer than 3,
 * more than 5, or no image): the caller then lays them out as a flow.
 */
export function bentoLayout(blocks: readonly Pick<DeckBlock, "type">[]): BentoLayout | null {
  if (blocks.length < 3 || blocks.length > 5) return null;
  const big = blocks.findIndex((block) => block.type === "image" || block.type === "video");
  if (big < 0) return null;
  const rest = blocks.map((_, index) => index).filter((index) => index !== big);
  const cells: BentoCell[] = [{ index: big, role: "big", area: "1 / 1 / 3 / 2" }];
  if (rest.length === 2) {
    cells.push({ index: rest[0], role: "cell", area: "1 / 2 / 2 / 3" });
    cells.push({ index: rest[1], role: "cell", area: "2 / 2 / 3 / 3" });
    return { columns: "2fr 1fr", cells };
  }
  if (rest.length === 3) {
    cells.push({ index: rest[0], role: "cell", area: "1 / 2 / 2 / 3" });
    cells.push({ index: rest[1], role: "cell", area: "1 / 3 / 2 / 4" });
    cells.push({ index: rest[2], role: "wide", area: "2 / 2 / 3 / 4" });
    return { columns: "2fr 1fr 1fr", cells };
  }
  rest.forEach((index, position) => {
    const row = position < 2 ? 1 : 2;
    const column = 2 + (position % 2);
    cells.push({ index, role: "cell", area: `${row} / ${column} / ${row + 1} / ${column + 1}` });
  });
  return { columns: "2fr 1fr 1fr", cells };
}
