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
 * Template theme → CSS custom properties.
 *
 * Shared by the interactive deck (`PresentationDeck.tsx`) and the static
 * render page behind the PDF / PPTX export (`PresentationStaticRender.tsx`) so
 * an exported deck is themed by exactly the code path the screen uses.
 */
import { createContext, useContext, type CSSProperties } from "react";

import type {
  Presentation,
  PresentationSlide,
  PresentationTheme,
  PresentationThemeConfig,
  PresentationThemeScheme,
} from "@/types";

import { isSafeThemeValue, trimThemeValue } from "./theme-value.ts";

/**
 * The colour scheme of a deck theme name — mirrors `theme_scheme()` in
 * `backend/app/services/presentations.py`: `light` for `light`, anything else
 * is a dark surface. A theme IS a scheme; the tolerance is for a
 * spec cached before the retired `filigran` preset was normalised away.
 * @public
 */
export function themeScheme(theme: PresentationTheme | string | undefined): PresentationThemeScheme {
  return theme === "light" ? "light" : "dark";
}

/**
 * The scheme ONE slide is read in: its own `theme` when it names one,
 * the deck's otherwise. The precedence rule in one place - `slide.theme` wins
 * over `spec.theme` whenever it is set, and a deck theme change reaches only
 * the slides that inherit. Mirrors `resolved_slide_scheme()` in
 * `backend/app/services/presentation_spec.py`.
 */
export function slideScheme(
  deckTheme: PresentationTheme | string | undefined,
  slide: Pick<PresentationSlide, "theme"> | undefined,
): PresentationThemeScheme {
  return slide?.theme === "light" || slide?.theme === "dark" ? slide.theme : themeScheme(deckTheme);
}

/** What a slide root renders with: its resolved scheme and the template palette for it. */
interface SlideSchemeResolution {
  scheme: PresentationThemeScheme;
  /** `true` when the slide names its own theme rather than inheriting the deck's. */
  overridden: boolean;
  tokens: PresentationThemeConfig;
}

/**
 * The ONE resolver behind every slide root: `StaticSlide` (the export
 * page, the page document, the thumbnails, the speaker view) and the reveal
 * deck's `DeckSlide` both read the scheme a slide is drawn in and the
 * template palette for THAT scheme through here - exactly as the deck resolver
 * (`themeConfigFor`) reads them for the deck - so the PPTX writer and the
 * layout check, which measure the static page, see the same pixels the
 * screen shows. `deckTheme` defaults to the spec's theme; the editor passes
 * its draft's.
 */
export function resolveSlideScheme(
  presentation: Pick<Presentation, "theme_config" | "theme_configs"> & { spec: Pick<Presentation["spec"], "theme"> },
  slide: Pick<PresentationSlide, "theme"> | undefined,
  deckTheme: PresentationTheme | string | undefined = presentation.spec.theme,
): SlideSchemeResolution {
  const own = slide?.theme === "light" || slide?.theme === "dark";
  const scheme = slideScheme(deckTheme, slide);
  return { scheme, overridden: own, tokens: themeConfigFor(presentation, scheme) };
}

/**
 * The paper of each scheme when the template names no `background`: the
 * `--deck-paper-<scheme>` token `presentation.css` declares once and reads
 * every token block's `--deck-bg` from. A reference, never a copy: reveal's
 * background layer, outside every slide root, resolves it off the deck root
 * to the value the slide root draws.
 */
export const SCHEME_PAPER: Record<PresentationThemeScheme, string> = {
  dark: "var(--deck-paper-dark)",
  light: "var(--deck-paper-light)",
};

/** A palette `background` that is an image layer - a CSS gradient, legacy `-webkit-gradient(` included - rather than a colour. */
const GRADIENT_PAPER = /^(?:-(?:webkit|moz)-)?(?:repeating-)?(?:(?:linear|radial|conic)-)?gradient\(/i;

/**
 * A colour, for a runtime without `CSS.supports` (the unit tests): a hex, a
 * keyword or ONE colour function call (nesting one call at most) and nothing
 * after it - `rgb(0 0 0) image-set(...)` is no colour to the browser either.
 */
const COLOR_PAPER =
  /^(?:#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})|(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color|color-mix|light-dark|var)\((?:[^()]|\([^()]*\))*\)|(?!none$)[a-z]+)$/i;

/** The CSS-wide keywords: `CSS.supports` accepts them as colours, but they paint no opaque paper. */
const CSS_WIDE_KEYWORD = /^(?:inherit|initial|unset|revert|revert-layer)$/i;

/**
 * Whether reveal can paint `value` as a background colour: anything else it
 * drops, leaving the slide transparent. A `var()` passes on trust (the browser
 * cannot know what it will hold), and a colour the template makes see-through
 * (`transparent`, an alpha) is its own choice.
 */
function isPaperColor(value: string): boolean {
  if (CSS_WIDE_KEYWORD.test(value)) return false;
  if (typeof CSS !== "undefined" && typeof CSS.supports === "function") return CSS.supports("color", value);
  return COLOR_PAPER.test(value);
}

/** What reveal paints under ONE slide. */
interface SlidePaper {
  /** reveal's `backgroundColor`: always a colour, so a slide is never without a paper. */
  color: string;
  /** reveal's `backgroundGradient`, drawn over `color`: a palette background that is a gradient. */
  gradient?: string;
}

/**
 * ONE slide's paper: its palette's `background` for the scheme it is
 * drawn in, else that scheme's own. reveal paints it as the slide's
 * background, under the slide and inside the deck's clip, so the paper at a
 * slide's edge is always the slide's - never the deck's white showing as a
 * hairline along a dark cover. A palette may name a gradient (the deck root
 * paints one through `background`), which is no colour: it rides reveal's
 * gradient layer over the scheme's paper instead of leaving the slide
 * transparent over the stage. A value that is neither (`none`), or that the
 * theme guard refuses (reveal paints the gradient through
 * `style.background`, where an `image-set()` layer after it would load), is no
 * paper at all: the scheme's own is painted instead.
 */
export function slidePaper(resolution: SlideSchemeResolution): SlidePaper {
  const own = trimThemeValue(resolution.tokens?.background ?? "");
  const scheme = SCHEME_PAPER[resolution.scheme];
  if (!own || !isSafeThemeValue("background", own)) return { color: scheme };
  if (GRADIENT_PAPER.test(own)) return { color: scheme, gradient: own };
  return isPaperColor(own) ? { color: own } : { color: scheme };
}

/**
 * What the slide root resolved, for the components drawn inside it:
 * the hero backdrop reads the template's mark asset off the slide's own
 * palette, never off the deck's. `null` outside a slide root (a block rendered
 * in isolation by a test or a preview).
 */
export const SlideSchemeContext = createContext<SlideSchemeResolution | null>(null);

export function useSlideScheme(): SlideSchemeResolution | null {
  return useContext(SlideSchemeContext);
}

/**
 * The attributes every slide root publishes: `data-scheme` keys the
 * stylesheet's token block for the slide, `data-scheme-override` marks a slide
 * that names its own theme (the reveal section then paints its own paper),
 * and the inline palette is the template's for THAT scheme. One helper so the
 * static canvas and the reveal section cannot publish different things.
 */
export function slideRootAttributes(resolution: SlideSchemeResolution): {
  "data-scheme": PresentationThemeScheme;
  "data-scheme-override": "true" | undefined;
  style: CSSProperties;
} {
  return {
    "data-scheme": resolution.scheme,
    "data-scheme-override": resolution.overridden ? "true" : undefined,
    style: themeStyle(resolution.tokens),
  };
}

/**
 * The template palette to lay under the theme that is ON SCREEN.
 *
 * `presentation.theme_config` is resolved by the server for the SAVED theme.
 * The editor previews its draft before saving, and a draft can name another
 * theme: rendering the saved palette under it would keep the deck dark while
 * the theme select says light — the inline `--deck-*` variables outrank the
 * `[data-deck-theme]` tokens. So the response also carries the palette per
 * scheme (`theme_configs`), and every host resolves through here with the
 * theme it actually renders. Falls back to `theme_config` for a response
 * cached before the map existed.
 */
export function themeConfigFor(
  presentation: Pick<Presentation, "theme_config" | "theme_configs">,
  theme: PresentationTheme | string | undefined,
): PresentationThemeConfig {
  return presentation.theme_configs?.[themeScheme(theme)] ?? presentation.theme_config;
}

/**
 * `theme_config` key → CSS custom property consumed by `presentation.css`.
 *
 * Mirrors `THEME_CONFIG_KEYS` in `backend/app/services/presentations.py` (the
 * backend test suite greps this map for every key). The `*_file_id` keys are
 * not CSS: they name the logo asset the chrome draws (`chrome.tsx`) and the
 * brand MARK the `gradient` hero draws as a watermark
 * (`HeroBackdrop.tsx`).
 */
export const THEME_VARIABLES: Record<keyof PresentationThemeConfig, string | null> = {
  background: "--deck-bg",
  foreground: "--deck-fg",
  accent: "--deck-accent",
  muted: "--deck-muted",
  heading_font: "--deck-heading-font",
  body_font: "--deck-body-font",
  mono_font: "--deck-mono-font",
  surface: "--deck-surface",
  line: "--deck-line",
  info: "--deck-info",
  success: "--deck-success",
  warning: "--deck-warning",
  error: "--deck-error",
  success_text: "--deck-success-text",
  warning_text: "--deck-warning-text",
  error_text: "--deck-error-text",
  gradient_from: "--deck-gradient-from",
  gradient_to: "--deck-gradient-to",
  chart_1: "--deck-chart-1",
  chart_2: "--deck-chart-2",
  chart_3: "--deck-chart-3",
  chart_4: "--deck-chart-4",
  chart_5: "--deck-chart-5",
  chart_6: "--deck-chart-6",
  brand_1: "--deck-brand-1",
  brand_2: "--deck-brand-2",
  brand_3: "--deck-brand-3",
  brand_4: "--deck-brand-4",
  logo_file_id: null,
  logo_light_file_id: null,
  mark_file_id: null,
};

/** The `--deck-chart-N` variables in series order - what an untoned
 *  chart series is coloured with. `presentation.css` gives each a fallback
 *  derived from the accent, so a deck with no template palette still draws. */
export const CHART_SERIES_VARIABLES: readonly string[] = [
  THEME_VARIABLES.chart_1,
  THEME_VARIABLES.chart_2,
  THEME_VARIABLES.chart_3,
  THEME_VARIABLES.chart_4,
  THEME_VARIABLES.chart_5,
  THEME_VARIABLES.chart_6,
].filter((cssVar): cssVar is string => cssVar !== null);

/**
 * Brand overrides as CSS custom properties.
 *
 * The one place the deck sets inline styles: the values are per-deck data
 * (resolved from a template container), so they cannot be Tailwind classes.
 * The backend whitelists the keys and drops any value that could escape its
 * declaration (`;`, braces, a comment) or call a CSS function that loads a URL
 * - only colour, gradient, math and `var()` functions pass (see
 * `theme_value_problem`) - and the preview filters through the same
 * rule; each value passes it here again, so a palette that reached this sink
 * by another road still cannot make the viewer's browser load a URL. Unknown
 * keys never reach here because `THEME_VARIABLES` is the map.
 */
export function themeStyle(config: PresentationThemeConfig | undefined): CSSProperties {
  const style: Record<string, string> = {};
  for (const [key, cssVar] of Object.entries(THEME_VARIABLES)) {
    if (!cssVar) continue;
    const value = config?.[key as keyof PresentationThemeConfig];
    if (value && isSafeThemeValue(key, value)) style[cssVar] = trimThemeValue(value);
  }
  return style as CSSProperties;
}
