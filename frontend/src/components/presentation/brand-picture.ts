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
 * The template's own brand pictures placed IN A BLOCK (an architecture node
 * or zone, an image, a row logo, a profile line) read like the chrome reads
 * them.
 *
 * The chrome resolves the brand per slide scheme: the logo's light variant on
 * a light slide, the mark of the slide's own palette, and, when the template
 * declares `chrome.emblem_ink: "accent"`, the mark inked in ONE colour
 * (`--deck-emblem-ink`: the slide's accent on light, its foreground on dark).
 * A block that names the same file by id used to draw its raw bytes, so a
 * white one-colour mark was a white symbol on a pale light slide. A block
 * picture now goes through `brandPicture()`: a file that is one of the
 * template's brand assets is swapped for the scheme's variant and, for an
 * inked template's mark, flagged `inked`, which the renderers draw as a MASK
 * over the ink (never a filter: skill rule "brand emblem ink"). Any other
 * file is drawn as it is.
 */
import { createContext, useContext } from "react";

import type { Presentation, PresentationThemeConfig, PresentationThemeScheme } from "@/types";

import { useSlideScheme } from "./theme";

/** The template's brand assets for a deck, read once per slide root. */
export interface DeckBrandAssets {
  /** The mark of each scheme's palette (`mark_file_id`). */
  marks: { dark?: string; light?: string };
  /** Every logo the palettes name (`logo_file_id`, either scheme). */
  logos: string[];
  /** The logo's variant for a light slide (`logo_light_file_id`). */
  logoLight?: string;
  /** The template inks its mark in one colour (`chrome.emblem_ink: "accent"`). */
  inkMark: boolean;
}

/** What a block draws for a file id on a slide of `scheme`. */
export interface BrandPicture {
  fileId: string;
  /** Drawn as a mask over `--deck-emblem-ink` instead of its own colours. */
  inked: boolean;
}

function configs(
  presentation: Pick<Presentation, "theme_config" | "theme_configs">,
): Record<PresentationThemeScheme, PresentationThemeConfig | undefined> {
  return {
    dark: presentation.theme_configs?.dark ?? presentation.theme_config,
    light: presentation.theme_configs?.light ?? presentation.theme_config,
  };
}

/** The brand assets of a deck response; `inkMark` from its template chrome. */
export function deckBrandAssets(
  presentation: Pick<Presentation, "theme_config" | "theme_configs" | "template_chrome">,
): DeckBrandAssets {
  const { dark, light } = configs(presentation);
  const logos = [dark?.logo_file_id, light?.logo_file_id].filter((id): id is string => !!id);
  return {
    marks: { dark: dark?.mark_file_id || undefined, light: light?.mark_file_id || undefined },
    logos: Array.from(new Set(logos)),
    logoLight: light?.logo_light_file_id || dark?.logo_light_file_id || undefined,
    inkMark: presentation.template_chrome?.emblem_ink === "accent",
  };
}

/**
 * The picture a block draws for `fileId` on a slide of `scheme`. The rule:
 * - one of the template's marks: the mark of the slide's palette (its own
 *   mark when that palette names one), inked when the template inks its mark;
 * - one of the template's logos on a light slide: its light variant when the
 *   template ships one;
 * - anything else, or no brand known (a block rendered outside a slide root):
 *   the file as it is.
 */
export function brandPicture(
  fileId: string,
  brand: DeckBrandAssets | null | undefined,
  scheme: PresentationThemeScheme | undefined,
): BrandPicture {
  if (!brand || !scheme) return { fileId, inked: false };
  if (fileId === brand.marks.dark || fileId === brand.marks.light) {
    return { fileId: brand.marks[scheme] ?? fileId, inked: brand.inkMark };
  }
  if (scheme === "light" && brand.logoLight && brand.logos.includes(fileId)) {
    return { fileId: brand.logoLight, inked: false };
  }
  return { fileId, inked: false };
}

/** Provided by the slide roots (`PresentationDeck`, `StaticSlide`); `null` elsewhere. */
export const DeckBrandContext = createContext<DeckBrandAssets | null>(null);

/** `brandPicture()` for the slide being rendered. */
export function useBrandPicture(fileId: string): BrandPicture;
export function useBrandPicture(fileId: string | null | undefined): BrandPicture | null;
export function useBrandPicture(fileId: string | null | undefined): BrandPicture | null {
  const brand = useContext(DeckBrandContext);
  const scheme = useSlideScheme()?.scheme;
  return fileId ? brandPicture(fileId, brand, scheme) : null;
}
