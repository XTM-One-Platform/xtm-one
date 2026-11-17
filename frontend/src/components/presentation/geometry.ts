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
 * Deck reference canvas.
 *
 * Every deck is laid out on a fixed pixel canvas that reveal.js scales to its
 * container; `presentation.css` sizes everything in absolute pixels on it. Kept
 * in a plain module (no React) so the renderer, the export path and the print
 * view share one definition — and so Vite's Fast Refresh keeps working in the
 * component files that import it.
 *
 * Two families: the SLIDE formats (`16:9`, `4:3`) and the PORTRAIT PAGE formats
 * (`A4`, `letter`) used for a product brief, a one-pager, a datasheet. A page
 * keeps the 1920px canvas width so every block keeps the proportions it was
 * designed with on a slide; it is simply taller than wide, and the export
 * prints it on real paper.
 *
 * Mirrors `CANVAS_SIZES_PX` in `backend/app/services/presentation_spec.py`; the
 * server renders exports at exactly this size.
 */
import type { DeckAspectRatio, PresentationKind } from "@/types";

export const DECK_SIZE: Record<DeckAspectRatio, { width: number; height: number }> = {
  "16:9": { width: 1920, height: 1080 },
  "4:3": { width: 1440, height: 1080 },
  "A4": { width: 1920, height: 2715 }, // 210 x 297 mm
  "letter": { width: 1920, height: 2485 }, // 8.5 x 11 in
};

export const DECK_ASPECT_RATIOS = Object.keys(DECK_SIZE) as DeckAspectRatio[];

/** The portrait page formats — a document rather than a slide show.
 * @public
 */
export const DOCUMENT_ASPECT_RATIOS: readonly DeckAspectRatio[] = ["A4", "letter"];

/** Normalise a stored / typed value onto the catalog (`"a4"` → `"A4"`), else 16:9. */
export function deckAspectRatio(aspectRatio: string | undefined): DeckAspectRatio {
  if (!aspectRatio) return "16:9";
  const lowered = aspectRatio.trim().toLowerCase();
  return DECK_ASPECT_RATIOS.find((ratio) => ratio.toLowerCase() === lowered) ?? "16:9";
}

/** Canvas for an aspect ratio, defaulting to 16:9 for anything unknown. */
export function deckSize(aspectRatio: string | undefined) {
  return DECK_SIZE[deckAspectRatio(aspectRatio)];
}

/** True for the portrait page formats (`A4` / `letter`). */
export function isDocumentFormat(aspectRatio: string | undefined): boolean {
  return DOCUMENT_ASPECT_RATIOS.includes(deckAspectRatio(aspectRatio));
}

/** The family a deck belongs to — what `presentation.css` keys its type scale on. */
type DeckFormat = "slide" | "page";

/**
 * `data-deck-format` of the deck root. A slide is read from the back of a room,
 * a page at arm's length or on paper: the two families get different type
 * scales and safe margins (see the tokens in `presentation.css`), and every
 * root the stylesheet types under — on screen and on the static render page —
 * must carry it, or the export would print a page at slide scale.
 */
export function deckFormat(aspectRatio: string | undefined): DeckFormat {
  return isDocumentFormat(aspectRatio) ? "page" : "slide";
}

/**
 * The family a presentation belongs to — `deck` (presented) or `page` (read).
 * Mirrors `presentation_kind` in `backend/app/services/presentation_spec.py`,
 * which is what the API serializes as `kind`; this is for a spec that has not
 * been round-tripped through the server yet (the editor's working copy, a
 * template preview). Prefer the server's `kind` whenever a row carries one.
 */
export function presentationKind(aspectRatio: string | undefined): PresentationKind {
  return isDocumentFormat(aspectRatio) ? "page" : "deck";
}

/**
 * Paper the PDF export prints on — PowerPoint's 13.333x7.5in / 10x7.5in for the
 * slide formats, ISO A4 and US Letter for the portrait pages. Mirrors
 * `PDF_PAGE_SIZES_IN` in `backend/app/services/presentation_export.py`.
 *
 * The static render page declares it as its `@page` size. Chromium takes the
 * ORIENTATION of a document's `@page` rule even when the caller passes an
 * explicit paper size (`preferCSSPageSize: false` only drops the dimensions),
 * and the app ships a global `@page { size: A4 }` for report printing: without
 * a later, deck-specific rule every slide came out on a portrait page, shrunk
 * into its upper half.
 */
const PDF_PAGE_SIZE: Record<DeckAspectRatio, string> = {
  "16:9": "13.3333in 7.5in",
  "4:3": "10in 7.5in",
  "A4": "8.2677in 11.6929in",
  "letter": "8.5in 11in",
};

export function pdfPageSize(aspectRatio: string | undefined) {
  return PDF_PAGE_SIZE[deckAspectRatio(aspectRatio)];
}
