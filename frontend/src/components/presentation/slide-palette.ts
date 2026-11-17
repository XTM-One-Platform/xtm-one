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
 * A slide's own palette inside its template (spec
 * "PPT Design System" 3.2).
 *
 * `palette: "xtm_one"` redraws ONE slide's accent, block gradients and chart
 * series from the XTM One palette - Violet #5E1AD5, Light violet #9575CD,
 * Medium violet #9C27B0, Sky Blue #0FBCFF, Aqua #00F1BD - in either scheme.
 * The slide root carries `data-palette` and, from `slidePaletteStyle`, the
 * palette's tokens as inline custom properties merged AFTER the template's
 * (`slideRootAttributes`), so they win over the template palette on the same
 * root (a stylesheet rule cannot: inline properties outrank it). The
 * stylesheet's "Slide palettes" block keeps only the flat-block overrides.
 * DeckSlide and StaticSlide (the export page) read the same helper. A slide
 * without the field publishes nothing and renders as before.
 *
 * Text floors (WCAG AA, measured in the stylesheet comments): Violet fails as
 * text on dark (2.4:1 on #0A0C1C), so the dark accent is Light violet (5.3:1
 * on #0A0C1C, 4.9:1 on #0d172b); Light violet fails on light (3.4:1 on
 * #E6F7FF), so the light accent is Violet (7.4:1 on #E6F7FF).
 *
 * The dark gradient carries the page ink (`--deck-bg`) on the gradient banner
 * and the highlighted tier badge, so BOTH of its ends pass 4.5:1 behind that
 * ink (Violet #5E1AD5 at the start measured 2.39:1):
 * Light violet #9575CD to #B39DDB. The light scheme draws white on its own
 * Violet to deep violet gradient (presentation.css, "Slide palettes").
 */
import type { CSSProperties } from "react";

import type { DeckSlidePalette, PresentationSlide, PresentationThemeScheme } from "@/types";

import type { SchemaField } from "./block-schema.ts";

/** Every slide palette, in picker order. Mirrors `SLIDE_PALETTES`. */
const SLIDE_PALETTES: readonly DeckSlidePalette[] = ["xtm_one"];

/** The inspector's palette control: unset (Default) keeps the template's palette. */
export const SLIDE_PALETTE_FIELD: SchemaField = {
  kind: "enum",
  key: "palette",
  label: "Palette",
  options: SLIDE_PALETTES,
  optional: true,
  optionLabels: { xtm_one: "XTM One" },
  hint: "XTM One redraws this slide's accent, gradients and chart colours; the logo and footer stay the template's.",
};

/** The palette a slide is drawn with, `undefined` for the template's. */
export function slidePalette(slide: Pick<PresentationSlide, "palette"> | undefined): DeckSlidePalette | undefined {
  const value = slide?.palette;
  return value && (SLIDE_PALETTES as readonly string[]).includes(value) ? value : undefined;
}

/** The tokens each palette redraws, per scheme. */
const PALETTE_TOKENS: Record<DeckSlidePalette, Record<PresentationThemeScheme, Record<string, string>>> = {
  xtm_one: {
    dark: {
      "--deck-accent": "#9575cd",
      "--deck-info": "#9575cd",
      "--deck-gradient-from": "#9575cd",
      "--deck-gradient-to": "#b39ddb",
      "--deck-chart-1": "#9575cd",
      "--deck-chart-2": "#00f1bd",
      "--deck-chart-3": "#0fbcff",
      "--deck-chart-4": "#b39ddb",
      "--deck-chart-5": "#9c27b0",
      "--deck-chart-6": "#5e1ad5",
    },
    light: {
      "--deck-accent": "#5e1ad5",
      "--deck-info": "#5e1ad5",
      "--deck-gradient-from": "#5e1ad5",
      "--deck-gradient-to": "#9575cd",
      "--deck-chart-1": "#5e1ad5",
      "--deck-chart-2": "#9c27b0",
      "--deck-chart-3": "#9575cd",
      "--deck-chart-4": "#00795f",
      "--deck-chart-5": "#085e80",
      "--deck-chart-6": "#4527a0",
    },
  },
};

/**
 * The palette's tokens for the slide's resolved scheme, as inline custom
 * properties to spread AFTER the template palette on the slide root; `{}` for
 * a slide in the template's palette.
 */
export function slidePaletteStyle(
  slide: Pick<PresentationSlide, "palette"> | undefined,
  scheme: PresentationThemeScheme,
): CSSProperties {
  const palette = slidePalette(slide);
  return palette ? ({ ...PALETTE_TOKENS[palette][scheme] } as CSSProperties) : {};
}

/** The attribute the slide root publishes for its palette. */
export function slidePaletteAttributes(slide: Pick<PresentationSlide, "palette"> | undefined): {
  "data-palette"?: DeckSlidePalette;
} {
  const palette = slidePalette(slide);
  return palette ? { "data-palette": palette } : {};
}
