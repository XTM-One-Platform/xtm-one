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
 * Where the brand logo sits on a slide.
 *
 * The deck stores ONE value, `chrome.logo_position`; the answer is PER SLIDE.
 * Body and section slides always keep the bottom-right corner: the footer bar
 * and the slide number share their row, and the mark must not move between two
 * body slides. The opening and closing slides are the two whose bottom row
 * carries nothing else (no footer bar; the title slide has no number either),
 * so the corner is a choice there — an explicit deck value wins, and `auto`
 * follows the slide's own composition: the title slide's `align` (bottom-left
 * under a left-aligned headline, on the slide axis under a centred one), the
 * closing slide's `align` (centred unless it asks for `start`).
 *
 * Kept out of `chrome.tsx` so the component file exports components only.
 */
import type { DeckLogoPosition, PresentationSlide } from "@/types";

/** A placement the chrome can draw — `auto` resolved away. */
export type ResolvedLogoPosition = Exclude<DeckLogoPosition, "auto">;

/** The layouts whose bottom row is the logo's alone: `logo_position` applies
 *  there and nowhere else. */
const PLACEABLE_LAYOUTS: ReadonlySet<PresentationSlide["layout"]> = new Set(["title", "closing"]);

export function resolveLogoPosition(
  position: DeckLogoPosition | undefined,
  slide: PresentationSlide,
): ResolvedLogoPosition {
  if (!PLACEABLE_LAYOUTS.has(slide.layout)) return "end";
  if (position && position !== "auto") return position;
  if (slide.layout === "title") return slide.align === "center" ? "center" : "start";
  return slide.layout === "closing" && slide.align === "start" ? "start" : "center";
}
