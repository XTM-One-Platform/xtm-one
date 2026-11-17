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
 * Hero variants and overlays - the pure half, shared by the layouts,
 * the slide roots and the editor.
 *
 * A HERO is the backdrop of a title or closing slide (`Hero` in
 * `presentation_spec.py`): `plain` is the flat paper every deck had; the three
 * others draw a backdrop UNDER the slide's content, in the slide's resolved
 * scheme (`resolveSlideScheme`), so a dark cover on a light deck is
 * `theme: "dark"` plus any hero on that slide, never a variant of its own.
 *
 * The `split` hero HOSTS the slide's background image in its right panel, so a
 * slide root must not paint that image full-bleed as well
 * (`heroHostsBackgroundImage`); `cascade` and `horizon` host the brand LOGO,
 * drawn large in the cover's top corner, so the chrome leaves it out on that
 * slide (`heroHostsLogo`). An OVERLAY is the scrim drawn between a background
 * image and the content, on any layout.
 */
import type { DeckHero, DeckHeroTone, DeckOverlay, PresentationSlide, SlideLayout } from "@/types";

import type { MosaicPattern } from "./mosaic";

/** Mirrors `HERO_VARIANTS` on the backend, in catalog order. */
export const HERO_VARIANTS: readonly DeckHero[] = [
  "plain",
  "gradient",
  "split",
  "spotlight",
  "mosaic",
  "cascade",
  "horizon",
  "pillar",
];

/** Mirrors `SHARED_HEROES` on the backend: the covers every deck may wear. */
export const SHARED_HEROES: readonly DeckHero[] = ["plain", "gradient", "split", "spotlight"];

/**
 * Mirrors `TEMPLATE_HEROES` on the backend: the tiled covers are a brand's own
 * artwork, offered only by a template that declares them in
 * `extra_metadata.heroes` (`templateHeroes`).
 */
export const TEMPLATE_HEROES: readonly DeckHero[] = HERO_VARIANTS.filter((hero) => !SHARED_HEROES.includes(hero));

/** The cover a hero the template does not offer falls back to without a default of its own. */
const FALLBACK_COVER_HERO: DeckHero = "gradient";

/** Mirrors `template_heroes` on the backend: the covers a deck of this template may wear, in catalog order. */
export function templateHeroes(extraMetadata: unknown): DeckHero[] {
  const declared =
    extraMetadata && typeof extraMetadata === "object" && !Array.isArray(extraMetadata)
      ? (extraMetadata as Record<string, unknown>).heroes
      : undefined;
  const own = new Set(
    Array.isArray(declared)
      ? declared.filter((value): value is string => typeof value === "string").map((value) => value.trim().toLowerCase())
      : [],
  );
  return HERO_VARIANTS.filter((hero) => SHARED_HEROES.includes(hero) || (TEMPLATE_HEROES.includes(hero) && own.has(hero)));
}

/**
 * Mirrors the `hero_defaults` of `template_deck_defaults` on the backend: the
 * hero each cover of a new deck opens on, among the covers the template offers.
 */
export function templateHeroDefaults(extraMetadata: unknown): Partial<Record<string, DeckHero>> {
  const deckDefaults =
    extraMetadata && typeof extraMetadata === "object" && !Array.isArray(extraMetadata)
      ? (extraMetadata as Record<string, unknown>).deck_defaults
      : undefined;
  const raw =
    deckDefaults && typeof deckDefaults === "object" && !Array.isArray(deckDefaults)
      ? (deckDefaults as Record<string, unknown>).hero_defaults
      : undefined;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
  const offered = templateHeroes(extraMetadata);
  const out: Partial<Record<string, DeckHero>> = {};
  for (const layout of HERO_LAYOUTS) {
    const value = (raw as Record<string, unknown>)[layout];
    const cleaned = typeof value === "string" ? value.trim().toLowerCase() : "";
    const hero = offered.find((candidate) => candidate === cleaned);
    if (hero) out[layout] = hero;
  }
  return out;
}

/** Mirrors `cover_hero` on the backend: the hero a cover takes when its own is not offered. */
export function coverHero(
  layout: string,
  offered: readonly DeckHero[],
  heroDefaults: Partial<Record<string, DeckHero>>,
): DeckHero {
  const fallback = heroDefaults[layout];
  return fallback && offered.includes(fallback) ? fallback : FALLBACK_COVER_HERO;
}

/** The heroes drawn as tiles (`mosaic.ts`), and the shape each lays them in. */
export const MOSAIC_HEROES: Readonly<Partial<Record<DeckHero, MosaicPattern>>> = {
  mosaic: "scatter",
  cascade: "cascade",
  horizon: "horizon",
  pillar: "pillar",
};

/** The heroes that draw the brand logo themselves, large in the cover's top corner. */
const LOGO_HOSTING_HEROES: ReadonlySet<DeckHero> = new Set(["cascade", "horizon"]);
/**
 * Mirrors `HERO_TONES` on the backend, in catalog order: the colour a hero is
 * drawn in. `brand` is the template's own gradient (from -> to); every other
 * value is ONE palette colour used alone - the stylesheet maps each onto the
 * `--deck-hero-from` / `--deck-hero-to` pair the three backdrops read
 * (`[data-hero-tone]` in `presentation.css`), so the choice is a token lookup
 * on the slide's own root, never a colour value in the spec.
 */
export const HERO_TONES: readonly DeckHeroTone[] = [
  "brand",
  "accent",
  "gradient_from",
  "gradient_to",
  "info",
  "success",
  "warning",
  "error",
  "muted",
];
/** Mirrors `OVERLAY_VARIANTS` on the backend, in catalog order. */
export const OVERLAY_VARIANTS: readonly DeckOverlay[] = ["none", "soft", "strong"];
/** The layouts that carry a `hero` field. */
export const HERO_LAYOUTS: readonly SlideLayout[] = ["title", "closing"];

/** The slide's hero, `plain` when the field is absent, `null` on a layout without one. */
export function slideHero(slide: Pick<PresentationSlide, "layout"> & { hero?: DeckHero }): DeckHero | null {
  if (!HERO_LAYOUTS.includes(slide.layout as SlideLayout)) return null;
  return slide.hero && HERO_VARIANTS.includes(slide.hero) ? slide.hero : "plain";
}

/** The colour the slide's hero is drawn in, `brand` when the field is absent (or the layout has no hero). */
export function slideHeroTone(slide: Pick<PresentationSlide, "layout"> & { hero_tone?: DeckHeroTone }): DeckHeroTone {
  return slide.hero_tone && HERO_TONES.includes(slide.hero_tone) ? slide.hero_tone : "brand";
}

/** The slide's overlay, `none` when the field is absent. */
export function slideOverlay(slide: Pick<PresentationSlide, "overlay">): DeckOverlay {
  return slide.overlay && OVERLAY_VARIANTS.includes(slide.overlay) ? slide.overlay : "none";
}

/**
 * Whether the slide's background image is drawn by its hero (the `split`
 * panel) rather than by the slide root full-bleed. A root that painted it too
 * would show the picture twice.
 */
export function heroHostsBackgroundImage(slide: PresentationSlide): boolean {
  return slideHero(slide) === "split";
}

/** Whether the slide's hero draws the brand logo, so the chrome must not draw it again. */
export function heroHostsLogo(slide: PresentationSlide): boolean {
  const hero = slideHero(slide);
  return hero !== null && LOGO_HOSTING_HEROES.has(hero);
}
