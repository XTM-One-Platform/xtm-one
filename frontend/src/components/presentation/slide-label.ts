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

import type { DeckBlock, PresentationSlide } from "@/types";

import { markupToPlainText } from "./markup";

/**
 * The human label of a slide, shared by the editor's slide list and the
 * toolbar's "go to slide" menu so the two name a slide the same way. Every
 * layout that has a title stores it as `title`; a slide without one falls back
 * to the first block that carries a headline of its own — a `banner` (how the
 * collateral pages open: a hero header instead of a slide title), a
 * `section_header` or a `heading` — and only then to the caller's placeholder.
 * Without that fallback every page of a brief reads "Untitled page" in the
 * go-to menu, which names nothing.
 *
 * Titles carry inline markup (`**bold**`, `[[accent:…]]`, a `\n` break) that
 * only the slide renders — a label is plain text, one line.
 */
export function slideHeading(slide: PresentationSlide): string {
  const title = "title" in slide && typeof slide.title === "string" ? slide.title : "";
  return oneLine(title) || oneLine(firstHeadline(slideBlocks(slide)));
}

function oneLine(markup: string): string {
  return markupToPlainText(markup).replace(/\s*\n\s*/g, " ").trim();
}

/** The blocks of a slide in reading order — a single flow or its columns. */
function slideBlocks(slide: PresentationSlide): DeckBlock[] {
  const blocks: DeckBlock[] = [];
  if ("blocks" in slide && Array.isArray(slide.blocks)) blocks.push(...slide.blocks);
  if ("columns" in slide && Array.isArray(slide.columns)) {
    for (const column of slide.columns) if (Array.isArray(column)) blocks.push(...column);
  }
  return blocks;
}

/** The headline of the first block that has one, looking inside `columns`. */
function firstHeadline(blocks: DeckBlock[]): string {
  for (const block of blocks) {
    switch (block.type) {
      case "banner":
      case "section_header":
        if (block.title) return block.title;
        break;
      case "heading":
        if (block.content) return block.content;
        break;
      case "columns": {
        const nested = firstHeadline(block.columns.flat());
        if (nested) return nested;
        break;
      }
      default:
        break;
    }
  }
  return "";
}
