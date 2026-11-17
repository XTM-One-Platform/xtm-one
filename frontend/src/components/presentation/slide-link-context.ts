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
 * Slide links INSIDE a deck: `[text](#/<slide id>)` in any markup field
 * (`markup.ts`) is drawn by `inline-markup.tsx` as a link to that slide.
 *
 * The link names its target by the slide's STABLE id, never by its position,
 * so a reorder never breaks it; what it resolves to is the deck host's
 * business, provided here once per rendered deck:
 *
 *   - `PresentationDeck` (the viewer, the chat card, the studio stage) moves
 *     reveal to the slide;
 *   - `PageDocument` scrolls the page document to the page;
 *   - `PresentationStaticRender` (what the PDF is printed from and the PPTX is
 *     measured on) follows nothing: the link is a plain `#deck-slide-<n>`
 *     anchor to the slide's canvas, which Chromium writes into the PDF as an
 *     internal link, and `data-slide-link-index` is what the PPTX exporter
 *     reads to lay a "jump to slide" target over it.
 *
 * Without a host (a thumbnail, the speaker view's previews) the link is drawn
 * as a link and does nothing. A link whose target is no slide of the deck (a
 * typo, a deleted slide) is drawn as its plain label: the agent's save result
 * names it (`slide_links_line`).
 *
 * Plain module (no JSX) so the pure helpers stay testable under Node.
 */
import { createContext, useContext, useMemo } from "react";

import type { PresentationSlide } from "../../types/index.ts";

export interface SlideLinks {
  /** The 0-based position of the slide carrying `id`, `null` when none does. */
  indexOf: (id: string) => number | null;
  /** Move the deck to slide `index`; absent where the host does not move (the export page). */
  follow?: (index: number) => void;
  /** The link's `href` for slide `index`; absent, it is the viewer's fragment `#/<slide id>`. */
  href?: (index: number) => string;
}

export const SlideLinkContext = createContext<SlideLinks | null>(null);

export function useSlideLinks(): SlideLinks | null {
  return useContext(SlideLinkContext);
}

/**
 * The position of each slide by its id - ids ONLY: unlike a chat link
 * (`resolveSlideRef`), an in-deck link never falls back to reading its ref
 * as a number, so `#/19` is the slide whose id is `19`, never "slide 19".
 */
export function slideIndexer(slides: readonly Pick<PresentationSlide, "id">[]): (id: string) => number | null {
  const positions = new Map<string, number>();
  slides.forEach((slide, index) => {
    if (slide.id && !positions.has(slide.id)) positions.set(slide.id, index);
  });
  return (id) => positions.get(id) ?? null;
}

/** The element id of slide `index`'s canvas on the export page - the PDF's link target. */
export function slideAnchorId(index: number): string {
  return `deck-slide-${index}`;
}

/** The context value for a deck host, stable while the slides and the callbacks are. */
export function useSlideLinksValue(
  slides: readonly Pick<PresentationSlide, "id">[],
  follow?: (index: number) => void,
  href?: (index: number) => string,
): SlideLinks {
  return useMemo(() => ({ indexOf: slideIndexer(slides), follow, href }), [slides, follow, href]);
}
