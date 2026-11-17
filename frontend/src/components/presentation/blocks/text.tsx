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

/** Text-level blocks: text, heading, section_header, divider, spacer (the quote is `quote.tsx`). */
import type {
  DeckDividerBlock,
  DeckHeadingBlock,
  DeckSectionHeaderBlock,
  DeckSpacerBlock,
  DeckTextBlock,
} from "@/types";

import { DeckIcon } from "../DeckIcon";
import { useBlockEditAttrs } from "../edit-context";
import { InlineMarkup } from "../inline-markup";
import { soleSpanTone } from "../markup";
import { alignAttr } from "./helpers";

export function TextBlockView({ block }: { block: DeckTextBlock }) {
  return (
    <div
      className="deck-text"
      data-size={block.size ?? "md"}
      data-align={alignAttr(block.align)}
      data-tone={block.tone}
      {...useBlockEditAttrs()}
    >
      <InlineMarkup value={block.content} field="content" />
    </div>
  );
}

export function HeadingBlockView({ block }: { block: DeckHeadingBlock }) {
  const level = block.level ?? 3;
  const Tag = (`h${level}` as "h2" | "h3" | "h4");
  // The tone colours the WHOLE heading, icon included (the stylesheet reads it
  // off this element). A heading whose entire text is one `[[tone:...]]` span
  // is the same heading written the other way, so it takes that tone too —
  // otherwise the text turns green and the icon stays accent beside it.
  const tone = block.tone ?? soleSpanTone(block.content);
  return (
    <Tag className="deck-heading" data-align={alignAttr(block.align)} data-tone={tone} {...useBlockEditAttrs()}>
      {block.icon ? <DeckIcon name={block.icon} className="deck-heading-icon" /> : null}
      {/* `.deck-heading` is a flex row (icon + text): the markup needs ONE
          wrapper, or each bold / italic run becomes its own flex item and
          the heading is torn apart around it. */}
      <InlineMarkup as="span" value={block.content} field="content" />
    </Tag>
  );
}

export function SectionHeaderBlockView({ block }: { block: DeckSectionHeaderBlock }) {
  return (
    <div
      className="deck-section-header"
      data-tone={block.tone ?? soleSpanTone(block.title) ?? "accent"}
      {...useBlockEditAttrs()}
    >
      {/* The label is its own box: the corner marks frame it, and the rule
          after it runs on to the edge of the column. */}
      <span className="deck-section-header-label">
        {block.icon ? <DeckIcon name={block.icon} /> : null}
        <InlineMarkup as="span" value={block.title} field="title" />
      </span>
    </div>
  );
}

export function DividerBlockView({ block }: { block: DeckDividerBlock }) {
  return (
    <div
      className="deck-divider"
      data-tone={block.tone}
      data-labelled={block.label ? "true" : undefined}
      role="separator"
      {...useBlockEditAttrs()}
    >
      {block.label ? <InlineMarkup as="span" value={block.label} field="label" /> : null}
    </div>
  );
}

export function SpacerBlockView({ block }: { block: DeckSpacerBlock }) {
  return (
    <div
      className="deck-spacer"
      data-size={block.size ?? "md"}
      data-grow={block.grow ? "true" : undefined}
      aria-hidden="true"
      {...useBlockEditAttrs()}
    />
  );
}
