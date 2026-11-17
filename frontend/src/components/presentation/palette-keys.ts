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
 * The palette keys a template may set, how the template form groups the
 * colours among them, and what an empty colour falls back to.
 *
 * `THEME_CONFIG_KEYS` mirrors `THEME_CONFIG_KEYS` in
 * `app/services/presentations.py` (a backend test pins the two): the keys the
 * deck renderer honours. The form's colour groups are cut from it, so the form
 * never offers a key the renderer ignores, and a test fails when a colour key
 * the renderer honours is missing from the form.
 *
 * Dependency-free on purpose: the unit test imports it directly.
 */
import type { PresentationThemeConfig } from "@/types";

/** Keys the deck renderer maps to CSS custom properties. */
export const THEME_CONFIG_KEYS: (keyof PresentationThemeConfig)[] = [
  "background",
  "foreground",
  "accent",
  "muted",
  "heading_font",
  "body_font",
  "mono_font",
  "surface",
  "line",
  "info",
  "success",
  "warning",
  "error",
  "success_text",
  "warning_text",
  "error_text",
  "gradient_from",
  "gradient_to",
  "chart_1",
  "chart_2",
  "chart_3",
  "chart_4",
  "chart_5",
  "chart_6",
  "brand_1",
  "brand_2",
  "brand_3",
  "brand_4",
  "logo_file_id",
  "logo_light_file_id",
  "mark_file_id",
];

/** The keys that are not colours: type families and asset ids. */
const NON_COLOR_KEYS = [
  "heading_font",
  "body_font",
  "mono_font",
  "logo_file_id",
  "logo_light_file_id",
  "mark_file_id",
] as const;

export type PaletteColorKey = Exclude<keyof PresentationThemeConfig, (typeof NON_COLOR_KEYS)[number]>;

const isColorKey = (key: keyof PresentationThemeConfig): key is PaletteColorKey =>
  !(NON_COLOR_KEYS as readonly string[]).includes(key);

/** Every colour key the renderer honours. */
export const PALETTE_COLOR_KEYS: PaletteColorKey[] = THEME_CONFIG_KEYS.filter(isColorKey);

/** The six ordered chart series, in rank order. */
export const CHART_SERIES_KEYS: PaletteColorKey[] = PALETTE_COLOR_KEYS.filter((key) => /^chart_\d+$/.test(key));

/** The brand hues: the product colours a node or zone wears through `color`. */
export const BRAND_HUE_KEYS: PaletteColorKey[] = PALETTE_COLOR_KEYS.filter((key) => /^brand_\d+$/.test(key));

/** The core palette in reading order: surfaces and ink first, then the brand, tones and gradient. */
export const CORE_COLOR_KEYS: PaletteColorKey[] = [
  "background",
  "surface",
  "foreground",
  "muted",
  "line",
  "accent",
  "info",
  "success",
  "warning",
  "error",
  "success_text",
  "warning_text",
  "error_text",
  "gradient_from",
  "gradient_to",
];

/** Every colour of a palette in the order the form and the swatches show them. */
export const PALETTE_SWATCH_KEYS: PaletteColorKey[] = [...CORE_COLOR_KEYS, ...CHART_SERIES_KEYS, ...BRAND_HUE_KEYS];

/**
 * The readable name of every colour key, in English, read by
 * `t_i18n(PALETTE_KEY_LABELS[key])`: the template form's caption above the
 * key itself, and the source an empty field's placeholder names. A caption
 * whose bare word means something else in another language ("Background" is a
 * background process in es, "Lines" a row of text in ja) is its own colour
 * string, never the generic catalog word. @i18n-keys
 */
export const PALETTE_KEY_LABELS = {
  background: "Background colour",
  surface: "Surface",
  foreground: "Text colour",
  muted: "Muted text",
  line: "Line colour",
  accent: "Accent colour",
  info: "Info colour",
  success: "Success",
  warning: "Warning",
  error: "Error",
  success_text: "Success text",
  warning_text: "Warning text",
  error_text: "Error text",
  gradient_from: "Gradient start",
  gradient_to: "Gradient end",
  chart_1: "Series 1",
  chart_2: "Series 2",
  chart_3: "Series 3",
  chart_4: "Series 4",
  chart_5: "Series 5",
  chart_6: "Series 6",
  brand_1: "Brand hue 1",
  brand_2: "Brand hue 2",
  brand_3: "Brand hue 3",
  brand_4: "Brand hue 4",
} as const satisfies Record<PaletteColorKey, string>;

/**
 * The chart series a brand hue falls back to when it is not set: the series of
 * the same rank (`brand_2` -> `chart_2`), as the renderer draws it.
 */
export function brandHueFallbackKey(key: PaletteColorKey): PaletteColorKey | null {
  const rank = /^brand_(\d+)$/.exec(key)?.[1];
  if (!rank) return null;
  const chart = `chart_${rank}`;
  return (CHART_SERIES_KEYS as string[]).includes(chart) ? (chart as PaletteColorKey) : null;
}

/** A feedback tone as text -> the tone it falls back to. */
const TEXT_TONE_FALLBACK: Partial<Record<PaletteColorKey, PaletteColorKey>> = {
  success_text: "success",
  warning_text: "warning",
  error_text: "error",
};

/**
 * The palette key an EMPTY colour takes its value from in the renderer, the
 * same in both schemes: a brand hue the chart series of the same rank, the
 * first chart series the accent, a tone as text the tone itself
 * (`--deck-brand-N: var(--deck-chart-N)`, `--deck-chart-1: var(--deck-accent)`
 * and `--deck-success-text: var(--deck-success)` in `presentation.css`, pinned
 * by a test). `null` where an empty value leaves the renderer's built-in colour.
 */
export function paletteFallbackKey(key: PaletteColorKey): PaletteColorKey | null {
  if (key === "chart_1") return "accent";
  const tone = TEXT_TONE_FALLBACK[key];
  if (tone) return tone;
  return brandHueFallbackKey(key);
}

/** A palette as stored (`Record`) or as the preview reads it (`PresentationThemeConfig`). */
type PaletteValues = PresentationThemeConfig | Record<string, unknown>;

const text = (palette: PaletteValues, key: string): string => {
  const value = (palette as Record<string, unknown>)[key];
  return typeof value === "string" ? value.trim() : "";
};

/**
 * The colour `key` draws in `palette` (a palette as a deck reads it): its own
 * value, else the one it falls back to, down the chain (`brand_1` -> `chart_1`
 * -> `accent`). `null` when the renderer's built-in colour applies.
 */
export function resolvedPaletteColor(key: PaletteColorKey, palette: PaletteValues): string | null {
  for (let current: PaletteColorKey | null = key; current; current = paletteFallbackKey(current)) {
    const value = text(palette, current);
    if (value) return value;
  }
  return null;
}

/** Where an empty colour field's value comes from: the base palette a variant inherits, or another key. */
export interface PaletteFallback {
  source: "inherited" | PaletteColorKey;
  value: string;
}

/**
 * What an EMPTY colour field draws instead: the value its palette
 * inherits for the key (`inherited`, what a variant takes from the base), else
 * the colour of the key it falls back to in `effective`, the palette as a deck
 * reads it. `null` when the renderer's built-in colour applies: the form shows
 * no guess.
 */
export function paletteFallback(
  key: PaletteColorKey,
  effective: PaletteValues,
  inherited: PaletteValues = {},
): PaletteFallback | null {
  const own = text(inherited, key);
  if (own) return { source: "inherited", value: own };
  const next = paletteFallbackKey(key);
  const value = next ? resolvedPaletteColor(next, effective) : null;
  return next && value ? { source: next, value } : null;
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);

/**
 * `extraMetadata` without the value `theme_variants[scheme]` holds for `key`;
 * a variant left empty is dropped, and so is an empty `theme_variants`. The
 * same object when there is nothing to remove. A base palette field edit goes
 * through it (`withBaseKeyTakenBack`): decks lay a variant stored for the base
 * palette's own theme over the base, so without it the edit would change
 * nothing they draw.
 */
export function withoutVariantKey(
  extraMetadata: Record<string, unknown>,
  scheme: string,
  key: string,
): Record<string, unknown> {
  const variants = extraMetadata.theme_variants;
  const palette = isObject(variants) ? variants[scheme] : undefined;
  if (!isObject(variants) || !isObject(palette) || !(key in palette)) return extraMetadata;
  const rest = { ...palette };
  delete rest[key];
  const nextVariants = { ...variants };
  if (Object.keys(rest).length > 0) nextVariants[scheme] = rest;
  else delete nextVariants[scheme];
  const next = { ...extraMetadata };
  if (Object.keys(nextVariants).length > 0) next.theme_variants = nextVariants;
  else delete next.theme_variants;
  return next;
}

/**
 * `extraMetadata` once base palette field `key` was edited, `scheme` being the
 * theme the EDITED base palette is drawn for: the key is taken back from the
 * variant stored for that theme (`withoutVariantKey`), so the field decides
 * what decks in that theme draw. `background` is never taken back: it decides
 * that theme, and a colour typed key by key passes through values that read as
 * the other one (`#1a2` on the way to `#1a2230`), so taking it back would
 * delete the background of the other theme's own variant.
 */
export function withBaseKeyTakenBack(
  extraMetadata: Record<string, unknown>,
  scheme: string,
  key: string,
): Record<string, unknown> {
  if (key === "background") return extraMetadata;
  return withoutVariantKey(extraMetadata, scheme, key);
}
