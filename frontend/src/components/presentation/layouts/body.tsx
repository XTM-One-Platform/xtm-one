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

/** Body layouts: agenda, content, roadmap, takeaways. */
import { useId, useRef } from "react";

import type {
  AgendaSlide,
  ContentSlide,
  DeckAgendaItem,
  RoadmapSlide,
  TakeawaysSlide,
} from "@/types";

import { resolveAgendaLayout } from "../agenda-layout";
import { bentoLayout } from "../bento";
import { Block, BlockList } from "../blocks";
import { NumberedCards, PhaseCard } from "../blocks/cards";
import { bodyVerticalAlign, isLoneStatement } from "../blocks/quote-style";
import { equalRowsAttributes } from "../columns-fit";
import { DeckIcon } from "../DeckIcon";
import { useDeckDesign } from "../design-context";
import { InlineMarkup } from "../inline-markup";
import { SPREAD_AGENDA_ICON_ROW_MAX_EXTRA_PX, SPREAD_AGENDA_ROW_MAX_EXTRA_PX, useSpreadCap } from "../spread";
import { FooterSlot, SlideBodyArea, SlideHeader } from "./frame";

/**
 * The agenda: the items grouped into rows of `columns` cells (row by
 * row, or down the columns), each row a grid of two shared tracks - head and
 * description - that every cell of the row joins through a CSS subgrid. So the
 * titles of a row start on one line and the descriptions under them on
 * another, whatever a neighbour wraps to, and the rows are spread evenly over
 * the body (`distribute`) with the free height only ever landing BETWEEN rows,
 * never between a title and its own description. Each gap grows up to a
 * readable maximum (`SPREAD_AGENDA_ROW_MAX_EXTRA_PX`, the bounded spread of
 * `spread.ts`): two or three rows stop there and sit centred in the
 * body as a group, four and more normally fill it. A `list` of icon items
 * reads as a feature list and takes the feature list's tighter cap
 * (`SPREAD_AGENDA_ICON_ROW_MAX_EXTRA_PX`). Two looks per cell: `list` is
 * the numbered chip in front of the title, `grid` the board-deck `N. | TITLE`
 * head in accent caps with a thin separator bar and the description under it.
 *
 * The number, the separator, the title and the description are SEPARATE
 * elements on purpose: the editable PPTX export reads the DOM and turns each
 * text-bearing box into its own text box (the bar into a thin rectangle), which
 * is what lets the board office edit a title without retyping its number. When
 * any item has a description every cell reserves the line (an empty,
 * decorative box) so a row with and without one keeps one height.
 *
 * The rows are `li`s of the ordered list demoted to presentation, the cells
 * `listitem`s: the list stays a list for assistive technology while the DOM
 * carries the row a subgrid needs. With `flow: "column"` the DOM holds the
 * rows of the TRANSPOSED grid (1 4 7 / 2 5 8 / 3 6 9), so the list `aria-owns`
 * its cells in item order: the accessibility tree reads 1, 2, 3... down the
 * columns, the way the eye does, while the DOM keeps the rows the subgrid needs.
 */
export function AgendaLayout({ slide }: { slide: AgendaSlide }) {
  // A template may opt into the plain numbered grid: no cards, two
  // columns (three from seven items), the rows on top of the body, and every
  // item drawn alike: a stored `current` highlights nothing there.
  const plain = useDeckDesign()?.agenda === "plain";
  const layout = resolveAgendaLayout(slide, plain ? "plain" : undefined);
  const baseId = useId();
  const transposed = layout.flow === "column";
  const cellId = (index: number) => `${baseId}-${index}`;
  const listRef = useRef<HTMLOListElement>(null);
  const iconList = layout.style === "list" && slide.items.some((item) => item.icon);
  useSpreadCap(
    listRef,
    layout.distribute,
    iconList ? SPREAD_AGENDA_ICON_ROW_MAX_EXTRA_PX : SPREAD_AGENDA_ROW_MAX_EXTRA_PX,
    slide.items,
  );
  return (
    <>
      <SlideHeader title={slide.title ?? "Agenda"} subtitle={slide.subtitle} />
      <SlideBodyArea>
        <ol
          ref={listRef}
          className="deck-agenda"
          data-columns={layout.columns}
          data-style={layout.style}
          data-flow={layout.flow}
          data-distribute={layout.distribute ? "true" : "false"}
          data-descriptions={layout.hasDescriptions ? "true" : undefined}
          data-plain={plain ? "true" : undefined}
          aria-owns={transposed ? slide.items.map((_, index) => cellId(index)).join(" ") : undefined}
        >
          {layout.rows.map((row, rowIndex) => (
            <li className="deck-agenda-row" key={rowIndex} role="presentation">
              {row.map((index) => {
                const item = slide.items[index];
                const position = index + 1;
                const current = !plain && slide.current === position;
                const dimmed = !plain && slide.current !== undefined && !current;
                return (
                  <div
                    className="deck-agenda-item"
                    key={index}
                    id={transposed ? cellId(index) : undefined}
                    role="listitem"
                    data-current={current ? "true" : undefined}
                    data-dimmed={dimmed ? "true" : undefined}
                    data-description={item.description ? undefined : "none"}
                  >
                    {layout.style === "grid" ? (
                      <AgendaGridCell
                        item={item}
                        position={position}
                        reserve={layout.hasDescriptions}
                        field={`items.${index}`}
                      />
                    ) : (
                      <AgendaListItem
                        item={item}
                        position={position}
                        reserve={layout.hasDescriptions}
                        field={`items.${index}`}
                      />
                    )}
                  </div>
                );
              })}
            </li>
          ))}
        </ol>
      </SlideBodyArea>
      <FooterSlot block={slide.footer_block} />
    </>
  );
}

interface AgendaCellProps {
  item: DeckAgendaItem;
  /** 1-based place in the list - the number drawn when the item names none. */
  position: number;
  /** Reserve the description line when the item has none. */
  reserve: boolean;
  /** The item's spec path on the slide (`items.N`), for in-place editing. */
  field: string;
}

/** The description, or the box that stands in for it so the row stays level. */
function AgendaDescription({ item, reserve, field }: Pick<AgendaCellProps, "item" | "reserve" | "field">) {
  if (item.description) {
    return (
      <InlineMarkup as="div" className="deck-agenda-desc" value={item.description} field={`${field}.description`} />
    );
  }
  if (!reserve) return null;
  return <div className="deck-agenda-desc" data-empty="true" aria-hidden="true" />;
}

/** `list`: the numbered chip (or icon) in front of the title. The chip, the
 *  title and the description are direct children of the cell so the title and
 *  the description land in the row's two shared tracks (the chip spans both). */
function AgendaListItem({ item, position, reserve, field }: AgendaCellProps) {
  return (
    <>
      <span className="deck-agenda-marker" data-icon={item.icon ? "true" : undefined}>
        {item.icon ? <DeckIcon name={item.icon} /> : String(item.number ?? position).padStart(2, "0")}
      </span>
      <InlineMarkup as="div" className="deck-agenda-title" value={item.title} field={`${field}.title`} />
      <AgendaDescription item={item} reserve={reserve} field={field} />
    </>
  );
}

/** `grid`: `N. | TITLE` on one line, the description under it. */
function AgendaGridCell({ item, position, reserve, field }: AgendaCellProps) {
  return (
    <>
      <div className="deck-agenda-head">
        <span className="deck-agenda-number">
          {item.icon ? <DeckIcon name={item.icon} /> : `${item.number ?? position}.`}
        </span>
        <span className="deck-agenda-sep" aria-hidden="true" />
        <InlineMarkup as="div" className="deck-agenda-title" value={item.title} field={`${field}.title`} />
      </div>
      <AgendaDescription item={item} reserve={reserve} field={field} />
    </>
  );
}

/**
 * A bento body: the blocks placed as grid cells by `bentoLayout()`,
 * the grid filling the body. Blocks that are no bento (a stored deck edited
 * by hand to 2 blocks) fall back to the single flow.
 */
function BentoGrid({ blocks }: { blocks: NonNullable<ContentSlide["blocks"]> }) {
  const layout = bentoLayout(blocks);
  if (!layout) return <BlockList blocks={blocks} className="deck-blocks deck-blocks-slide" path="blocks" />;
  return (
    <div className="deck-bento" data-cells={blocks.length} style={{ gridTemplateColumns: layout.columns }}>
      {layout.cells.map((cell) => {
        const block = blocks[cell.index];
        return (
          <div
            key={cell.index}
            className="deck-bento-cell"
            data-role={cell.role}
            data-kind={block.type === "image" || block.type === "video" ? "image" : "text"}
            style={{ gridArea: cell.area }}
          >
            <Block block={block} path={`blocks.${cell.index}`} />
          </div>
        );
      })}
    </div>
  );
}

export function ContentLayout({ slide }: { slide: ContentSlide }) {
  const spread = slide.distribute && !slide.equal_rows;
  return (
    <>
      <SlideHeader title={slide.title} subtitle={slide.subtitle} />
      <SlideBodyArea>
        {slide.bento && slide.blocks?.length ? (
          <BentoGrid blocks={slide.blocks} />
        ) : slide.columns ? (
          <div
            className="deck-columns deck-columns-slide"
            data-count={slide.columns.length}
            data-ratio={slide.ratio ?? "equal"}
            data-valign={slide.vertical_align ?? "start"}
            data-gap="lg"
            data-equal-height={
              slide.equal_height || slide.vertical_align === "stretch" ? "true" : undefined
            }
            // Blocks lined up as rows are not spread: `equal_rows` wins.
            data-distribute={spread ? "true" : undefined}
            {...equalRowsAttributes(slide.equal_rows, slide.columns)}
          >
            {slide.columns.map((column, index) => (
              <div className="deck-column" key={index}>
                {/* Each column spreads its own run (the host's CSS rule); the
                    prop is what bounds that spread at a readable gap. */}
                <BlockList blocks={column} distribute={spread} path={`columns.${index}`} />
              </div>
            ))}
          </div>
        ) : (
          // A body that is one statement quote is centred; `distribute`
          // spreads nothing over one block and would pin it to the top instead.
          <BlockList
            blocks={slide.blocks}
            className="deck-blocks deck-blocks-slide"
            verticalAlign={bodyVerticalAlign(slide.blocks, slide.vertical_align)}
            distribute={isLoneStatement(slide.blocks) ? undefined : slide.distribute}
            path="blocks"
          />
        )}
      </SlideBodyArea>
      <FooterSlot block={slide.footer_block} />
    </>
  );
}

export function RoadmapLayout({ slide }: { slide: RoadmapSlide }) {
  return (
    <>
      <SlideHeader title={slide.title} subtitle={slide.subtitle} />
      <SlideBodyArea>
        <div className="deck-roadmap" data-count={slide.phases.length}>
          {slide.phases.map((phase, index) => (
            <PhaseCard key={index} phase={phase} fill field={`phases.${index}`} />
          ))}
        </div>
      </SlideBodyArea>
      <FooterSlot block={slide.footer_block} />
    </>
  );
}

export function TakeawaysLayout({ slide }: { slide: TakeawaysSlide }) {
  return (
    <>
      <SlideHeader title={slide.title ?? "Key takeaways"} subtitle={slide.subtitle} />
      <SlideBodyArea>
        <NumberedCards items={slide.items} columns={slide.columns} fill />
      </SlideBodyArea>
      <FooterSlot block={slide.footer_block} />
    </>
  );
}
