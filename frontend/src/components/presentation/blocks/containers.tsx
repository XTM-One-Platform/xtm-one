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

/** Container blocks: callout, comparison, columns. They render other blocks. */
import { useRef } from "react";

import type {
  DeckCalloutBlock,
  DeckColumnsBlock,
  DeckComparisonBlock,
  DeckComparisonSide,
} from "@/types";

import { equalRowsAttributes, useColumnsFit } from "../columns-fit";
import { DeckIcon } from "../DeckIcon";
import { useBlockEditAttrs, useBlockPath } from "../edit-context";
import { childPath } from "../edit-paths";
import { InlineMarkup } from "../inline-markup";
import { measureCalloutRun, useSpreadCap } from "../spread";
import { BlockList } from "./index";
import { IconText } from "./shared";

/** The nested list's path under this block, or none outside a path-aware render. */
function nestedPath(blockPath: string | null, ...more: (string | number)[]): string | undefined {
  return blockPath === null ? undefined : childPath(blockPath, ...more);
}

/**
 * A callout: the BOX is the variant's (`tinted` - the tone wash with a
 * bar on the left, the historical look - `card`, `outline`, `plain`), the
 * tone colours the icon and the title. Without `header` the icon keeps its
 * column left of the text (the historical layout, markup unchanged); with a
 * header the icon and the title form one head - on one line or stacked - and
 * everything under them uses the callout's full width.
 */
export function CalloutBlockView({ block }: { block: DeckCalloutBlock }) {
  const blockPath = useBlockPath();
  const variant = block.variant ?? "tinted";
  const bar = block.accent_bar ?? variant === "tinted";
  const header = block.header;
  // The icon's default size follows the head it sits in: the historical
  // gutter icon keeps its stylesheet size, an inline icon matches the title
  // line and a stacked one leads it.
  const iconSize = block.icon_size ?? block.size ?? (header === "stacked" ? "lg" : header ? "md" : undefined);
  const titleSize = block.title_size ?? block.size;
  // A `fill` callout around bounded spreads only (a `fill` list...) ends with
  // them instead of stretching its tint past their cap.
  const rootRef = useRef<HTMLDivElement>(null);
  useSpreadCap(rootRef, block.fill, 0, block.children, measureCalloutRun);
  const icon =
    block.icon && header !== "none" ? (
      <span className="deck-callout-icon">
        <DeckIcon name={block.icon} />
      </span>
    ) : null;
  const title = block.title ? (
    <InlineMarkup as="div" className="deck-callout-title" value={block.title} field="title" />
  ) : null;
  // An inline head is the shared icon + text row: the icon on the
  // centre of the title's first line, whether the title wraps or not.
  const head =
    header === "inline" ? (
      <IconText
        as="div"
        className="deck-callout-head"
        icon={block.icon ? <DeckIcon name={block.icon} /> : undefined}
        iconClassName="deck-callout-icon"
        labelAs="div"
      >
        {title}
      </IconText>
    ) : (
      <div className="deck-callout-head">
        {icon}
        {title}
      </div>
    );
  const rest = (
    <>
      {block.subtitle ? (
        <InlineMarkup as="div" className="deck-callout-subtitle" value={block.subtitle} field="subtitle" />
      ) : null}
      {block.content ? (
        <InlineMarkup as="p" className="deck-callout-content" value={block.content} field="content" />
      ) : null}
      {block.children?.length ? (
        <BlockList blocks={block.children} path={nestedPath(blockPath, "children")} />
      ) : null}
    </>
  );
  return (
    <div
      ref={rootRef}
      className="deck-callout"
      data-tone={block.tone ?? "accent"}
      data-density={block.density ?? "normal"}
      data-fill={block.fill ? "true" : undefined}
      // Only a look that differs from the historical one is written, so a
      // callout that sets none of the new fields keeps its original DOM.
      data-variant={variant !== "tinted" ? variant : undefined}
      data-bar={bar !== (variant === "tinted") ? String(bar) : undefined}
      data-header={header}
      data-icon-size={iconSize}
      data-title-size={titleSize}
      data-padding={block.padding}
      data-gap={block.children_gap}
      {...useBlockEditAttrs()}
    >
      {header ? (
        <>
          {icon || title ? head : null}
          {block.subtitle || block.content || block.children?.length ? (
            <div className="deck-callout-body">{rest}</div>
          ) : null}
        </>
      ) : (
        <>
          {icon}
          <div className="deck-callout-body">
            {title}
            {rest}
          </div>
        </>
      )}
    </div>
  );
}

function ComparisonPanel({
  side,
  sideKey,
  defaultTone,
}: {
  side: DeckComparisonSide;
  sideKey: "left" | "right";
  defaultTone: "muted" | "accent";
}) {
  const blockPath = useBlockPath();
  return (
    <div className="deck-compare-side" data-tone={side.tone ?? defaultTone}>
      {/* The side's label is the shared icon + text row. */}
      <IconText as="div" className="deck-compare-label" icon={side.icon ? <DeckIcon name={side.icon} /> : undefined}>
        <InlineMarkup value={side.label} field={`${sideKey}.label`} />
      </IconText>
      <BlockList blocks={side.blocks} path={nestedPath(blockPath, sideKey, "blocks")} />
    </div>
  );
}

export function ComparisonBlockView({ block }: { block: DeckComparisonBlock }) {
  return (
    <div
      className="deck-compare"
      data-equal-height={block.equal_height === false ? undefined : "true"}
      {...useBlockEditAttrs()}
    >
      <ComparisonPanel side={block.left} sideKey="left" defaultTone="muted" />
      <ComparisonPanel side={block.right} sideKey="right" defaultTone="accent" />
    </div>
  );
}

/** Side-by-side columns; four wrap 2 x 2 only when their content cannot fit (`columns-fit.ts`). */
export function ColumnsBlockView({ block }: { block: DeckColumnsBlock }) {
  const blockPath = useBlockPath();
  const ref = useRef<HTMLDivElement>(null);
  useColumnsFit(ref, block.columns);
  return (
    <div
      ref={ref}
      className="deck-columns"
      data-count={block.columns.length}
      data-ratio={block.ratio ?? "equal"}
      data-valign={block.align ?? "start"}
      data-gap={block.gap ?? "md"}
      data-equal-height={block.equal_height ? "true" : undefined}
      {...equalRowsAttributes(block.equal_rows, block.columns)}
      {...useBlockEditAttrs()}
    >
      {block.columns.map((column, index) => (
        <div className="deck-column" key={index}>
          <BlockList blocks={column} path={nestedPath(blockPath, "columns", index)} />
        </div>
      ))}
    </div>
  );
}
