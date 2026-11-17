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

/** The `list` block: bullets, checks, arrows, crosses, numbers or plain lines. */
import { useRef } from "react";

import type { DeckListBlock, DeckListStyle } from "@/types";

import { DeckIcon } from "../DeckIcon";
import { useBlockEditAttrs } from "../edit-context";
import { InlineMarkup } from "../inline-markup";
import { soleSpanTone } from "../markup";
import { SPREAD_ROW_MAX_EXTRA_PX, useSpreadCap } from "../spread";
import { IconText, RowLogo } from "./shared";

/** Marker icon per list style; `bullet`/`numbered`/`plain` use CSS markers. */
const STYLE_ICON: Partial<Record<DeckListStyle, string>> = {
  check: "check",
  arrow: "arrow-right",
  cross: "x",
};

export function ListBlockView({ block }: { block: DeckListBlock }) {
  const style = block.style ?? "bullet";
  const Tag = style === "numbered" ? "ol" : "ul";
  const styleIcon = STYLE_ICON[style];
  // `fill` spreads the rows up to a readable gap, never to the floor.
  // The intersection satisfies both tags' `ref` at once.
  const rootRef = useRef<HTMLUListElement & HTMLOListElement>(null);
  useSpreadCap(rootRef, block.fill, SPREAD_ROW_MAX_EXTRA_PX, block.items);
  const editAttrs = useBlockEditAttrs();
  // A row's picture (`logo_file_id`) takes its marker's place; once one row
  // has one, every row's marker sits in a slot of the same width so the text
  // keeps one left edge (`data-row-logos`, `presentation.css`).
  const hasLogos = block.items.some((item) => item.logo_file_id);
  return (
    <Tag
      ref={rootRef}
      className="deck-list"
      data-style={style}
      data-density={block.density ?? "normal"}
      data-columns={block.columns ?? 1}
      data-size={block.size ?? "md"}
      data-fill={block.fill ? "true" : undefined}
      data-row-logos={hasLogos ? "true" : undefined}
      {...editAttrs}
    >
      {block.items.map((item, index) => {
        const icon = item.icon ?? styleIcon;
        // An item whose whole text is one `[[tone:...]]` span is a toned item:
        // its marker follows the text.
        const tone = item.tone ?? soleSpanTone(item.text);
        // The item is the shared icon + text row: the marker icon on
        // the centre of the text's first line; the bullet and the number of
        // the CSS-marker styles sit on the same row before the text.
        return (
          <IconText
            as="li"
            key={index}
            data-tone={tone}
            data-logo={item.logo_file_id ? "true" : undefined}
            icon={
              item.logo_file_id ? (
                <RowLogo fileId={item.logo_file_id} border={item.logo_border} />
              ) : icon ? (
                <DeckIcon name={icon} />
              ) : undefined
            }
            iconClassName="deck-list-marker"
            labelClassName="deck-list-text"
          >
            <InlineMarkup value={item.text} field={`items.${index}.text`} />
          </IconText>
        );
      })}
    </Tag>
  );
}
