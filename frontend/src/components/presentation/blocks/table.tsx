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

/** The `table` block. Cells are inline markup, never HTML. */
import { useRef } from "react";

import type { DeckIconName, DeckMarkup, DeckTableBlock } from "@/types";

import { DeckIcon } from "../DeckIcon";
import { useBlockEditAttrs, useBlockPath, useEditMode } from "../edit-context";
import { childPath } from "../edit-paths";
import { InlineMarkup } from "../inline-markup";
import { SPREAD_ROW_MAX_EXTRA_PX, measureTableRows, useSpreadCap } from "../spread";
import { alignAttr } from "./helpers";
import { IconText } from "./shared";

/**
 * What a cell draws: its text, led by its icon on the shared icon + text row
 * when it has one; the icon alone - no label box, so it stays centred
 * in a centred column - when it has an icon and no text (a `true` / `false`
 * cell). Without an icon the text stands as it always did, the cell itself
 * being the editing target of an empty one.
 */
function CellContent({ icon, value, field }: { icon?: DeckIconName; value?: DeckMarkup; field: string }) {
  if (!icon) return <InlineMarkup value={value} field={field} />;
  return <IconText icon={<DeckIcon name={icon} />}>{value ? <InlineMarkup value={value} field={field} /> : null}</IconText>;
}

export function TableBlockView({ block }: { block: DeckTableBlock }) {
  // In edit mode the CELL carries the text target too: an empty cell renders
  // no text element to click on, and the cell is what the pointer lands on.
  const editing = useEditMode() !== null;
  const blockPath = useBlockPath();
  const cellTarget = (field: string) => (editing ? childPath(blockPath ?? "", field) : undefined);
  // `fill` hands the block the run's free height and spreads its ROWS
  // over it - the stretched table grows every row - up to a readable extra
  // per row, never to the floor: the cap is read off the natural rows.
  const rootRef = useRef<HTMLDivElement>(null);
  useSpreadCap(rootRef, block.fill, SPREAD_ROW_MAX_EXTRA_PX, block.rows, measureTableRows);
  // The frame (`deck-table-wrap`) is the part that shrinks and clips when a
  // slide is over-full - rows disappear from the bottom rather than the table
  // blowing the slide. The footnote sits OUTSIDE it: the fine print is the one
  // line a comparison must never lose, and inside the clipped frame it would
  // be the first thing to go. The root carries the `size`: the cell
  // type and padding scale, and the footnote's type, read off it.
  return (
    <div
      ref={rootRef}
      className="deck-table-block"
      data-size={block.size ?? "md"}
      data-fill={block.fill ? "true" : undefined}
      {...useBlockEditAttrs()}
    >
      <div className="deck-table-wrap">
        <table
          className="deck-table"
          data-density={block.density ?? "normal"}
          data-highlight-first={block.highlight_first_column ? "true" : undefined}
        >
          <thead>
            <tr>
              {block.headers.map((header, index) => (
                <th
                  key={index}
                  data-align={alignAttr(header.align)}
                  data-tone={header.tone}
                  data-edit-text={cellTarget(`headers.${index}.text`)}
                >
                  <CellContent icon={header.icon} value={header.text} field={`headers.${index}.text`} />
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {block.rows.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {row.map((cell, cellIndex) => (
                  <td
                    key={cellIndex}
                    data-tone={cell.tone}
                    data-align={alignAttr(block.headers[cellIndex]?.align)}
                    // A toned header emphasises its whole column (the "our
                    // product" column of a comparison): the cells carry the
                    // column tone separately from their own text tone.
                    data-col-tone={block.headers[cellIndex]?.tone}
                    data-edit-text={cellTarget(`rows.${rowIndex}.${cellIndex}.text`)}
                  >
                    <CellContent icon={cell.icon} value={cell.text} field={`rows.${rowIndex}.${cellIndex}.text`} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {block.footnote ? (
        <InlineMarkup as="div" className="deck-table-footnote" value={block.footnote} field="footnote" />
      ) : null}
    </div>
  );
}
