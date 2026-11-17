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
 * Build a throw-away lorem-ipsum deck from a template container.
 *
 * The point is to answer "what will a deck built from this template look like?"
 * without generating one: no agent call, no `Presentation` row, nothing
 * persisted. The object returned here has the same shape the renderer gets from
 * `GET /presentations/{id}`, so the preview goes through exactly the same
 * components as a real deck.
 *
 * **Fidelity is the whole value.** A preview that shows a colour production
 * would drop is worse than no preview, so this module mirrors the backend's two
 * filters: only the whitelisted `theme_config` keys are forwarded, and a value
 * that could escape its CSS declaration or make the browser load a URL is
 * dropped (`isSafeThemeValue` - the template reaches this module
 * unfiltered). The backend (`app/services/presentations.py`) remains the
 * authority — this is a mirror for display, kept in sync by hand like
 * `TEMPLATE_ASSET_ROLES`.
 */
import type {
  DeckHero,
  DeckLogoGridBlock,
  DeckLogoPosition,
  Presentation,
  PresentationSlide,
  PresentationSpec,
  PresentationTheme,
  PresentationThemeConfig,
  PresentationTransition,
  SlideLayout,
  TemplateAsset,
  TemplateCandidate,
  TemplateContainerDetail,
} from "@/types";
import { templateChromeOf } from "./chrome-pattern";
import { templateDesignOf } from "./design-tokens";
import { DECK_ASPECT_RATIOS, presentationKind } from "./geometry";
import { HERO_LAYOUTS, HERO_TONES, templateHeroDefaults, templateHeroes } from "./hero";
import { SLIDE_LAYOUTS } from "./layout-options";
import { THEME_CONFIG_KEYS } from "./palette-keys";
import { isSafeThemeValue, trimThemeValue } from "./theme-value";

/** Deck themes the renderer knows (mirrors `PRESENTATION_THEMES`): a theme is a
 *  colour scheme, `dark` or `light`. */
export const DECK_THEMES: readonly PresentationTheme[] = ["dark", "light"];
/** Slide transitions the renderer knows (mirrors `_TRANSITIONS`). */
export const DECK_TRANSITIONS: readonly PresentationTransition[] = [
  "none",
  "fade",
  "slide",
  "convex",
  "concave",
  "zoom",
];
/** Logo placements on the opening and closing slides, in picker order
 *  (mirrors `LOGO_POSITIONS`). */
export const DECK_LOGO_POSITIONS: readonly DeckLogoPosition[] = ["auto", "start", "center", "end"];

/** The layouts a preview shows when the container advertises none: the v2
 *  set, in deck order (agenda before the first section), so the theme is
 *  judged on what an agent will produce. */
const PREVIEW_LAYOUTS: SlideLayout[] = [
  "title",
  "agenda",
  "section",
  "content",
  "dashboard",
  "roadmap",
  "takeaways",
  "closing",
];

const LOREM_SHORT = "Lorem ipsum dolor sit amet";
const LOREM_LINE = "Consectetur adipiscing elit, sed do eiusmod tempor";
const LOREM_MARKUP =
  "Lorem ipsum dolor sit amet, **consectetur adipiscing** elit. Sed do eiusmod tempor " +
  "incididunt ut [[accent:labore et dolore]] magna aliqua.";

/**
 * Keep only theme values the renderer will actually honour.
 *
 * Anything else is dropped rather than shown, so the preview cannot advertise a
 * palette the product would ignore — which is exactly the trap this preview
 * exists to expose.
 */
export function previewThemeConfig(
  raw: Record<string, unknown> | null | undefined,
): PresentationThemeConfig {
  const config: PresentationThemeConfig = {};
  if (!raw) return config;
  for (const key of THEME_CONFIG_KEYS) {
    const value = raw[key];
    if (typeof value !== "string" || !isSafeThemeValue(key, value)) continue;
    config[key] = trimThemeValue(value);
  }
  return config;
}

/** The colour schemes a template may carry a second palette for. */
export const THEME_SCHEMES = ["dark", "light"] as const;
export type ThemeScheme = (typeof THEME_SCHEMES)[number];

/**
 * Theme keys whose value only makes sense on ONE colour scheme — the surfaces,
 * the ink on them, the feedback tones tuned for that surface, and the chart
 * series palette, six hues picked for contrast against that surface.
 * Everything else (accent, gradient, fonts, logo ids) is the brand and carries
 * to either scheme. Mirrors `SCHEME_BOUND_THEME_KEYS` in
 * `app/services/presentations.py` (a backend test pins the two lists to each
 * other).
 */
export const SCHEME_BOUND_THEME_KEYS: (keyof PresentationThemeConfig)[] = [
  "background",
  "surface",
  "line",
  "foreground",
  "muted",
  "info",
  "success",
  "warning",
  "error",
  "success_text",
  "warning_text",
  "error_text",
  "chart_1",
  "chart_2",
  "chart_3",
  "chart_4",
  "chart_5",
  "chart_6",
];

/** WCAG crossover below which white ink out-contrasts black on a surface. */
const DARK_SURFACE_LUMINANCE = 0.1791;

const HEX_COLOR = /^#([0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i;
const FUNC_COLOR = /^(rgba?|hsla?)\((.*)\)$/is;
const NAMED_COLOR_RGB: Record<string, [number, number, number]> = {
  white: [1, 1, 1],
  black: [0, 0, 0],
};

function colorChannel(token: string, scale: number): number | null {
  const trimmed = token.trim();
  const percent = trimmed.endsWith("%");
  const value = Number.parseFloat(percent ? trimmed.slice(0, -1) : trimmed);
  if (Number.isNaN(value)) return null;
  return Math.min(1, Math.max(0, percent ? value / 100 : value / scale));
}

function hslToRgb(hue: number, sat: number, light: number): [number, number, number] {
  const h = ((hue % 360) + 360) % 360;
  const channel = (n: number) => {
    const k = (n + h / 30) % 12;
    const a = sat * Math.min(light, 1 - light);
    return light - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
  };
  return [channel(0), channel(8), channel(4)];
}

/**
 * WCAG relative luminance (0 black .. 1 white) of a CSS colour token, or `null`
 * when it cannot be read — mirrors `css_color_luminance` on the backend, same
 * grammar: hex, `rgb()` / `hsl()` in either syntax, `white` / `black`.
 */
function cssColorLuminance(value: unknown): number | null {
  if (typeof value !== "string") return null;
  const text = value.trim().toLowerCase();
  if (!text) return null;
  let rgb: [number, number, number] | undefined = NAMED_COLOR_RGB[text];
  const hex = rgb ? null : HEX_COLOR.exec(text);
  if (hex) {
    let digits = hex[1];
    if (digits.length <= 4) digits = [...digits.slice(0, 3)].map((ch) => ch + ch).join("");
    digits = digits.slice(0, 6);
    rgb = [
      Number.parseInt(digits.slice(0, 2), 16) / 255,
      Number.parseInt(digits.slice(2, 4), 16) / 255,
      Number.parseInt(digits.slice(4, 6), 16) / 255,
    ];
  } else if (!rgb) {
    const fn = FUNC_COLOR.exec(text);
    if (!fn) return null;
    const parts = fn[2].split("/", 1)[0].trim().split(/[,\s]+/).filter(Boolean);
    if (parts.length < 3) return null;
    if (fn[1].startsWith("rgb")) {
      const channels = parts.slice(0, 3).map((p) => colorChannel(p, 255));
      if (channels.some((c) => c === null)) return null;
      rgb = channels as [number, number, number];
    } else {
      const hue = Number.parseFloat(parts[0].replace(/deg$/, ""));
      const sat = colorChannel(parts[1], 100);
      const light = colorChannel(parts[2], 100);
      if (Number.isNaN(hue) || sat === null || light === null) return null;
      rgb = hslToRgb(hue, sat, light);
    }
  }
  if (!rgb) return null;
  const linear = (c: number) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return 0.2126 * linear(rgb[0]) + 0.7152 * linear(rgb[1]) + 0.0722 * linear(rgb[2]);
}

/**
 * The scheme a palette is designed for, read off its `background`; `null`
 * when there is none to read (mirrors `palette_scheme` on the backend).
 */
export function paletteScheme(palette: PresentationThemeConfig): ThemeScheme | null {
  const luminance = cssColorLuminance(palette.background);
  if (luminance === null) return null;
  return luminance < DARK_SURFACE_LUMINANCE ? "dark" : "light";
}

/** The template's design values a template may edit and a preview reads. */
type TemplateDesign = Pick<TemplateContainerDetail, "theme_config" | "extra_metadata">;

/** The sanitized `extra_metadata.theme_variants[scheme]` palette, or `{}`
 *  (mirrors `template_theme_variant` on the backend). */
export function templateThemeVariant(
  template: TemplateDesign,
  scheme: ThemeScheme,
): PresentationThemeConfig {
  const variants = template.extra_metadata?.theme_variants;
  const variant =
    variants && typeof variants === "object"
      ? (variants as Record<string, unknown>)[scheme]
      : undefined;
  return previewThemeConfig(
    variant && typeof variant === "object" ? (variant as Record<string, unknown>) : undefined,
  );
}

/** Where a template DECLARES the schemes it supports — mirrors
 *  `SUPPORTED_SCHEMES_KEY` on the backend. */
export const SUPPORTED_SCHEMES_KEY = "supported_schemes";

/**
 * The schemes a template's author DECLARED it supports, in canonical order —
 * the Dark / Light switches of the template form; mirrors
 * `declared_supported_schemes` on the backend. `null` when the template
 * declares nothing (a row written before the declaration existed) or the
 * declaration is unreadable; unknown entries are ignored, so a declaration
 * naming only unknown schemes is also `null`, never `[]`.
 */
export function declaredSupportedSchemes(template: TemplateDesign): ThemeScheme[] | null {
  const raw = template.extra_metadata?.[SUPPORTED_SCHEMES_KEY];
  if (!Array.isArray(raw)) return null;
  const wanted = new Set(
    raw.filter((item): item is string => typeof item === "string").map((item) => item.trim().toLowerCase()),
  );
  const declared = THEME_SCHEMES.filter((scheme) => wanted.has(scheme));
  return declared.length > 0 ? declared : null;
}

/**
 * The colour schemes a template's PALETTES can dress a deck in, in canonical
 * order — mirrors `derived_supported_schemes` on the backend. The base
 * palette is designed for the scheme its `background` reads as; the OTHER
 * scheme is reachable only through a `theme_variants` palette that reads as
 * that scheme (a variant naming no background is taken at its word). A base
 * whose background cannot be read reaches both.
 *
 * This is what the template form PROPOSES when a template declares
 * nothing yet, and what its warning compares the declaration against — never
 * the answer on its own.
 */
export function derivedSupportedSchemes(template: TemplateDesign): ThemeScheme[] {
  const base = previewThemeConfig(template.theme_config);
  const designedFor = paletteScheme(base);
  if (designedFor === null) return [...THEME_SCHEMES];
  const supported: ThemeScheme[] = [];
  for (const scheme of THEME_SCHEMES) {
    if (scheme === designedFor) {
      supported.push(scheme);
      continue;
    }
    const variant = templateThemeVariant(template, scheme);
    if (Object.keys(variant).length === 0) continue;
    const variantScheme = paletteScheme(variant);
    if (variantScheme === null || variantScheme === scheme) supported.push(scheme);
  }
  return supported;
}

/**
 * The colour schemes a template can dress a deck in, in canonical order —
 * mirrors `template_supported_schemes` on the backend, which is the authority
 * (`supported_schemes` on every template response). The author's declaration
 * wins; the palette rule only answers for a template that has
 * not declared anything. This mirror exists for the template FORM: the designer
 * sees which schemes the template being edited will support before it is saved.
 */
export function templateSupportedSchemes(template: TemplateDesign): ThemeScheme[] {
  return declaredSupportedSchemes(template) ?? derivedSupportedSchemes(template);
}

/**
 * The theme a deck keeps (or is snapped to) when it wears a template that
 * supports `schemes` — mirrors `fit_theme_to_schemes` on the backend: the
 * current scheme when the template can dress it, the template's first scheme
 * otherwise. An empty list (a container that is not a presentation template)
 * changes nothing.
 */
export function fitThemeToSchemes(
  theme: PresentationTheme,
  schemes: readonly ThemeScheme[],
): PresentationTheme {
  if (schemes.length === 0 || schemes.includes(theme)) return theme;
  return schemes[0];
}

/**
 * The template's palette for the deck's colour scheme, mirroring
 * `scheme_palette` on the backend: `extra_metadata.theme_variants[scheme]` laid
 * over what the base `theme_config` hands that scheme (`inheritedThemeForScheme`)
 * — the base minus its scheme-bound surfaces when it is designed for the OTHER
 * scheme, so a dark brand previewed as a `light` deck comes out light (paper and
 * ink from the renderer's light tokens, brand accent / fonts / logos carried
 * over) whether or not it ships a light variant. Otherwise the preview shows a
 * deck production would never draw.
 */
export function previewThemeForDeck(
  template: TemplateDesign,
  deckTheme: PresentationTheme,
): PresentationThemeConfig {
  const scheme: ThemeScheme = deckTheme === "light" ? "light" : "dark";
  return { ...inheritedThemeForScheme(template, scheme), ...templateThemeVariant(template, scheme) };
}

/**
 * What the base `theme_config` hands a deck of colour scheme `scheme` before
 * that scheme's variant is laid over it: the base as authored when it is
 * designed for `scheme` or its scheme cannot be read, without its scheme-bound
 * keys when it is designed for the other scheme (`scheme_palette` on the
 * backend). The template form reads it for what an empty field of a variant
 * palette falls back to, so the form and the preview share this rule.
 */
export function inheritedThemeForScheme(
  template: TemplateDesign,
  scheme: ThemeScheme,
): PresentationThemeConfig {
  const base = previewThemeConfig(template.theme_config);
  const designedFor = paletteScheme(base);
  if (designedFor === null || designedFor === scheme) return base;
  return Object.fromEntries(
    Object.entries(base).filter(
      ([key]) => !SCHEME_BOUND_THEME_KEYS.includes(key as keyof PresentationThemeConfig),
    ),
  ) as PresentationThemeConfig;
}

/**
 * The template's palette per colour scheme — the `theme_configs` map a deck
 * response carries (`resolve_template_themes` on the backend), built on the
 * client so the deck editor can preview a template it is about to apply, and
 * the theme switch under it, before anything is saved. The chrome's
 * logo falls back to the container's `logo` / `logo_light` assets the way the
 * backend's `resolve_template_theme` does.
 */
export function previewThemeConfigs(
  template: TemplateContainerDetail,
): Record<ThemeScheme, PresentationThemeConfig> {
  const configs = {} as Record<ThemeScheme, PresentationThemeConfig>;
  for (const scheme of THEME_SCHEMES) {
    const config = previewThemeForDeck(template, scheme);
    config.logo_file_id ??= imageAssetId(template, "logo");
    config.logo_light_file_id ??= imageAssetId(template, "logo_light");
    // The brand mark the `gradient` hero draws as a watermark.
    config.mark_file_id ??= imageAssetId(template, "logo_mark");
    configs[scheme] = config;
  }
  return configs;
}

/** Layout ids the container advertises, minus the ones we cannot render. */
function previewLayouts(template: TemplateContainerDetail): SlideLayout[] {
  const advertised = (template.layouts ?? [])
    .map((entry) => (typeof entry?.id === "string" ? entry.id : null))
    .filter((id): id is string => !!id)
    .filter((id): id is SlideLayout => (SLIDE_LAYOUTS as readonly string[]).includes(id));
  // A container with no catalog (or one full of unknown ids) still deserves a
  // preview: fall back to the standard deck so the theme can be judged.
  const unique = [...new Set(advertised)];
  return unique.length > 0 ? unique : PREVIEW_LAYOUTS;
}

/** First image asset with the given role — fonts and other roles are skipped. */
function imageAssetId(
  template: TemplateContainerDetail,
  role: string,
): string | undefined {
  return (template.assets ?? []).find(
    (asset) =>
      asset.asset_role === role && (asset.content_type ?? "").startsWith("image/"),
  )?.id;
}

/** Roles a `logo_grid` preview is made of — the predefined logo assets:
 * product marks first, then partner / integration / customer logos. */
const LOGO_GRID_ROLES: readonly string[] = [
  "product_logo",
  "partner_logo",
  "integration_logo",
  "customer_logo",
];
const LOGO_GRID_PREVIEW_ITEMS = 6;

/**
 * Up to six of the container's logo assets, as `logo_grid` items — so the
 * preview of the brand deck shows the product marks it ships, and a template
 * with no palette to judge still shows what it holds.
 */
function previewLogoGrid(template: TemplateContainerDetail): DeckLogoGridBlock | null {
  const logos = (template.assets ?? []).filter(
    (asset) =>
      LOGO_GRID_ROLES.includes(asset.asset_role ?? "") &&
      (asset.content_type ?? "").startsWith("image/"),
  );
  if (logos.length < 2) return null;
  return {
    type: "logo_grid",
    items: logos.slice(0, LOGO_GRID_PREVIEW_ITEMS).map((asset) => ({ file_id: asset.id })),
    columns: 3,
    logo_height: "md",
  };
}

function skeletonSlide(
  layout: SlideLayout,
  template: TemplateContainerDetail,
): PresentationSlide {
  const notes = "Lorem ipsum — placeholder speaker notes.";
  switch (layout) {
    case "title":
      return {
        layout: "title",
        title: template.name,
        subtitle: LOREM_LINE,
        event_label: "Lorem ipsum 2026",
        speakers: [{ name: "Lorem Ipsum", title: "Dolor sit amet", credentials: ["Consectetur", "Adipiscing"] }],
        icon_strip: ["shield-check", "radar", "workflow", "sparkles"],
        footer: LOREM_SHORT,
        notes,
        background_image_file_id: imageAssetId(template, "background"),
      };
    case "section":
      return {
        layout: "section",
        number: 1,
        label: "Lorem ipsum",
        title: LOREM_SHORT,
        subtitle: LOREM_LINE,
        icon: "compass",
        notes,
      };
    case "agenda":
      return {
        layout: "agenda",
        items: [
          { title: "Lorem ipsum", description: LOREM_LINE, icon: "target" },
          { title: "Dolor sit amet", description: "Ut enim ad minim veniam", icon: "layers" },
          { title: "Consectetur", description: "Quis nostrud exercitation", icon: "rocket" },
          { title: "Adipiscing elit", description: "Duis aute irure dolor", icon: "flag" },
        ],
        current: 1,
        notes,
      };
    case "content": {
      const logoGrid = previewLogoGrid(template);
      return {
        layout: "content",
        title: LOREM_SHORT,
        subtitle: LOREM_LINE,
        columns: [
          [
            { type: "text", content: LOREM_MARKUP },
            {
              type: "list",
              style: "check",
              items: [
                { text: "Ut enim ad **minim** veniam" },
                { text: "Quis nostrud exercitation" },
                { text: "Duis aute irure dolor", tone: "warning", icon: "triangle-alert" },
              ],
            },
          ],
          [
            {
              type: "metrics_grid",
              columns: 2,
              density: "compact",
              items: [
                { value: "128", label: "Lorem", delta: "+12%", trend: "up", icon: "activity" },
                { value: "3.4h", label: "Ipsum", delta: "-40m", trend: "down", icon: "timer" },
              ],
            },
            // The container's own logo pack when it has one, so the preview
            // shows the predefined assets rather than only lorem ipsum.
            logoGrid ?? {
              type: "callout",
              tone: "info",
              icon: "lightbulb",
              title: "Lorem ipsum",
              content: "Excepteur sint occaecat cupidatat non proident.",
            },
          ],
        ],
        ratio: "wide_left",
        footer_block: {
          type: "callout",
          tone: "accent",
          icon: "sparkles",
          density: "compact",
          content: "Sunt in culpa qui officia deserunt mollit anim id est laborum.",
        },
        notes,
      };
    }
    case "roadmap":
      return {
        layout: "roadmap",
        title: LOREM_SHORT,
        phases: [
          { icon: "flag", title: "Lorem", subtitle: "Phase 1", items: ["Ipsum dolor", "Sit amet"], goal: { label: "Goal", value: "Consectetur" } },
          { icon: "rocket", title: "Ipsum", subtitle: "Phase 2", items: ["Adipiscing elit", "Sed do eiusmod"], goal: { label: "Goal", value: "Tempor" }, tone: "info" },
          { icon: "trophy", title: "Dolor", subtitle: "Phase 3", items: ["Incididunt ut", "Labore et dolore"], goal: { label: "Goal", value: "Magna aliqua" }, tone: "success" },
        ],
        notes,
      };
    case "takeaways":
      return {
        layout: "takeaways",
        items: [
          { title: "Lorem ipsum", description: LOREM_LINE },
          { title: "Dolor sit amet", description: "Ut enim ad minim veniam" },
          { title: "Consectetur", description: "Quis nostrud exercitation" },
        ],
        notes,
      };
    case "dashboard":
      // The chart palette is what this preview judges: three panels
      // exercise chart_1..chart_4 over the template's surface and lines.
      return {
        layout: "dashboard",
        title: LOREM_SHORT,
        subtitle: LOREM_LINE,
        tiles: [
          { value: "6,250", label: "Lorem", delta: "+9%", trend: "up", sparkline: [41, 48, 53, 57, 62] },
          { value: "38%", label: "Ipsum", delta: "+6 pts", trend: "up" },
          { value: "112", label: "Dolor", delta: "+21", trend: "up", tone: "success" },
          { value: "3.4h", label: "Sit amet", delta: "-40m", trend: "down" },
        ],
        panels: [
          {
            title: "Lorem ipsum per quarter",
            span: 2,
            block: {
              type: "chart",
              kind: "line",
              categories: ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6"],
              series: [
                { name: "Lorem", values: [3100, 3600, 4100, 4800, 5300, 6250] },
                { name: "Ipsum", values: [1200, 1500, 1900, 2400, 2800, 3300] },
              ],
              target: 6000,
              goal_label: "Plan",
            },
          },
          {
            title: "Dolor by version",
            block: {
              type: "chart",
              kind: "donut",
              categories: ["6.x", "5.x", "Older"],
              series: [{ name: "Share", values: [38, 47, 15] }],
              format: "percent",
            },
          },
          {
            title: "Sit amet",
            span: 3,
            block: {
              type: "chart",
              kind: "stacked_bar",
              categories: ["Q1", "Q2", "Q3", "Q4"],
              series: [
                { name: "Lorem", values: [12, 18, 24, 31] },
                { name: "Ipsum", values: [8, 11, 13, 17] },
                { name: "Dolor", values: [4, 5, 7, 9] },
              ],
              show_legend: true,
            },
          },
        ],
        notes,
      };
    case "closing":
      return {
        layout: "closing",
        title: "Thank you",
        subtitle: LOREM_LINE,
        contacts: [
          { kind: "web", value: "example.com" },
          { kind: "email", value: "lorem@example.com" },
          { kind: "linkedin", value: "lorem-ipsum" },
        ],
        tagline: LOREM_SHORT,
        notes,
      };
    default:
      // Every catalog layout has its case above; a value outside the union
      // (a template advertising an id this bundle predates) previews as the
      // plainest slide rather than nothing.
      return {
        layout: "content",
        title: LOREM_SHORT,
        blocks: [{ type: "text", content: LOREM_MARKUP }],
        notes,
      };
  }
}

/**
 * The template's per-cover defaults (`deck_defaults.hero_defaults` /
 * `hero_tones` / `slide_themes`, each keyed by `title` / `closing`) laid on the
 * preview's opening and closing skeletons the way `apply_deck_defaults` lays
 * them on a new deck: absent-only, catalog values only (a hero among the
 * covers the template offers), and a slide theme the template cannot
 * dress snapped like the deck's - so the seeded brand deck previews on the
 * gradient cover its decks open with, not on plain paper.
 * Mutates `slides` in place, like the backend helper.
 */
function applyCoverDefaults(
  slides: PresentationSlide[],
  defaults: Record<string, unknown>,
  supportedSchemes: readonly PresentationTheme[],
  coverHeroes: Partial<Record<string, DeckHero>>,
): void {
  const perCover = <T extends string>(key: string, allowed: readonly T[]): Partial<Record<"title" | "closing", T>> => {
    const raw = defaults[key];
    const out: Partial<Record<"title" | "closing", T>> = {};
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return out;
    for (const layout of HERO_LAYOUTS) {
      const value = (raw as Record<string, unknown>)[layout];
      const cleaned = typeof value === "string" ? value.trim().toLowerCase() : "";
      if ((allowed as readonly string[]).includes(cleaned)) out[layout as "title" | "closing"] = cleaned as T;
    }
    return out;
  };
  const tones = perCover("hero_tones", HERO_TONES);
  const themes = perCover("slide_themes", DECK_THEMES);
  for (const slide of slides) {
    if (slide.layout !== "title" && slide.layout !== "closing") continue;
    const hero = coverHeroes[slide.layout];
    if (hero && slide.hero === undefined) slide.hero = hero;
    const tone = tones[slide.layout];
    if (tone && slide.hero_tone === undefined) slide.hero_tone = tone;
    const theme = themes[slide.layout];
    if (theme && slide.theme === undefined) slide.theme = fitThemeToSchemes(theme, supportedSchemes);
  }
}

/**
 * Assemble the preview deck.
 *
 * Deck-level values come from `extra_metadata.deck_defaults` when the template
 * declares them (that is what an agent is told to pass), then from the
 * container's own `aspect_ratio` when it happens to be a valid deck value, then
 * from the renderer defaults. The theme is a colour scheme, and the template
 * decides which it can dress: whatever the caller or the defaults ask for is
 * snapped to the template's `supported_schemes` exactly as a deck is when the
 * template is applied — so the preview never shows a deck the product
 * would refuse to save. The container's legacy `theme` label is ignored.
 */
export function buildTemplatePreview(
  template: TemplateContainerDetail,
  options: {
    /** Preview the template on a deck of this theme instead of the one its
     *  `deck_defaults` would produce — how the light variant of a dark brand is
     *  inspected before any deck exists. Snapped to what the template supports. */
    theme?: PresentationTheme;
  } = {},
): Presentation {
  const defaults = (template.extra_metadata?.deck_defaults ?? {}) as Record<
    string,
    unknown
  >;

  const pick = <T extends string>(candidates: unknown[], allowed: readonly T[], fallback: T): T =>
    (candidates.find(
      (value): value is T => typeof value === "string" && (allowed as readonly string[]).includes(value),
    ) as T | undefined) ?? fallback;

  const supportedSchemes = templateSupportedSchemes(template);
  const spec: PresentationSpec = {
    title: template.name,
    theme: fitThemeToSchemes(
      pick([options.theme, defaults.theme], DECK_THEMES, "dark"),
      supportedSchemes,
    ),
    aspect_ratio: pick(
      [defaults.aspect_ratio, template.aspect_ratio],
      DECK_ASPECT_RATIOS,
      "16:9",
    ),
    transition: pick([defaults.transition], DECK_TRANSITIONS, "slide"),
    // The v1 pair reads on a template with no footer pattern; a template that
    // defines one draws it filled from these settings and the variables'
    // defaults instead, which is exactly what a deck will show.
    chrome: { event_label: "Lorem ipsum 2026", footer_text: template.name },
    slides: previewLayouts(template).map((layout) => skeletonSlide(layout, template)),
  };
  // The covers the template's decks open and close on, among the
  // covers it offers.
  applyCoverDefaults(spec.slides, defaults, supportedSchemes, templateHeroDefaults(template.extra_metadata));

  // The chrome draws the container's `logo` asset when the theme config does
  // not pin one explicitly — the same fallback the backend applies when it
  // resolves a deck's theme from its template (`resolve_template_theme`).
  const themeConfigs = previewThemeConfigs(template);
  const themeConfig = themeConfigs[spec.theme];

  return {
    // Not a real deck id — nothing is persisted. `updated_at` is part of it so
    // editing the template's aspect ratio remounts the deck instead of leaving
    // reveal initialized at the old slide geometry.
    id: `preview-${template.id}-${template.updated_at ?? ""}`,
    title: template.name,
    theme: spec.theme,
    // Derived the way the server derives it, so a template on A4 previews as
    // the document its decks will be (stacked sheets), not as a slide show.
    aspect_ratio: spec.aspect_ratio,
    kind: presentationKind(spec.aspect_ratio),
    slide_count: spec.slides.length,
    revision: 1,
    conversation_id: null,
    assignment_run_id: null,
    template_container_id: template.id,
    template_name: template.name,
    spec,
    theme_config: themeConfig,
    theme_configs: themeConfigs,
    // Both when the container is not a presentation template - the same
    // "nothing to restrict" a free deck reports.
    supported_schemes: supportedSchemes.length > 0 ? supportedSchemes : [...THEME_SCHEMES],
    supported_heroes: templateHeroes(template.extra_metadata),
    // The footer / header the template defines, read the lenient way the
    // deck response resolves it.
    template_chrome: templateChromeOf(template),
    // Its design tokens, read the way the deck response serves them.
    template_design: templateDesignOf(template),
    // The stored geometry of the template's logos: the preview crops
    // and balances them exactly as a deck response lets a deck do.
    asset_meta: Object.fromEntries(
      (template.assets ?? []).flatMap((asset) => (asset.image_meta ? [[asset.id, asset.image_meta]] : [])),
    ),
    created_at: template.created_at,
    updated_at: template.updated_at,
    // A preview is nobody's deck: no owner, no grants, and never manageable —
    // so no share / edit affordance can appear on it.
    user_id: null,
    owner_name: null,
    is_company_managed: false,
    shared_group_ids: null,
    shared_group_permissions: null,
    shared_user_ids: null,
    shared_user_permissions: null,
    access_restricted: false,
    permission: null,
    can_manage: false,
    // Nothing collaborative either: no editor, no review threads.
    updated_by_user_id: null,
    updated_by_name: null,
    open_comment_count: 0,
  };
}

/**
 * Wrap a "Generate with AI" candidate in the detail shape `buildTemplatePreview`
 * reads, so a not-yet-saved template previews through exactly the same path as
 * a saved one.
 *
 * `updated_at` carries the round number: the preview's deck id is derived from
 * it, so a new round remounts reveal at the new geometry instead of leaving it
 * initialised on the previous candidate's aspect ratio.
 */
export function candidateToTemplate(
  candidate: TemplateCandidate,
  assets: TemplateAsset[],
  round: number,
): TemplateContainerDetail {
  const stamp = new Date(0).toISOString();
  return {
    id: `generated-${round}`,
    user_id: null,
    owner_name: null,
    name: candidate.name,
    description: candidate.description,
    kind: candidate.kind,
    tags: candidate.tags,
    theme: candidate.theme,
    theme_config: candidate.theme_config,
    aspect_ratio: candidate.aspect_ratio,
    layouts: candidate.layouts,
    extra_metadata: candidate.extra_metadata,
    // Read off the candidate's palette the way the server reads a saved row's.
    supported_schemes: templateSupportedSchemes({
      theme_config: candidate.theme_config,
      extra_metadata: candidate.extra_metadata,
    }),
    lookup_count: 0,
    asset_count: assets.length,
    is_company_managed: false,
    is_builtin: false,
    shared_group_ids: null,
    shared_group_permissions: null,
    created_at: stamp,
    updated_at: `round-${round}`,
    assets,
  };
}
