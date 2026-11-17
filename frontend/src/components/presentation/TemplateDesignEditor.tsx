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
 * Structured editor for a presentation template's DESIGN — the
 * part of a template a deck inherits live: the themes it supports, one palette
 * per supported theme, the type families, and the deck defaults.
 *
 * It replaces typing `theme_config` / `extra_metadata` as raw JSON, so what a
 * deck will be ALLOWED to pick is decided here, in the open:
 *
 *   - the THEMES a template supports are DECLARED with the Dark / Light
 *     switches (`extra_metadata.supported_schemes`, at least one on) — never
 *     inferred from a colour behind the designer's back;
 *   - the BASE palette (`theme_config`) is the palette of the theme its
 *     `background` reads as — a dark background makes it the Dark palette;
 *   - the other supported theme is drawn from `theme_variants[scheme]`, a
 *     palette of surfaces and ink laid over the base, whose brand accent, fonts
 *     and logos carry over;
 *   - a declared theme with no palette of its own still renders: the deck takes
 *     the renderer's built-in surfaces for that theme with the brand carried
 *     over. The editor says so instead of hiding it.
 *
 * `templateSupportedSchemes()` (mirroring the backend's
 * `template_supported_schemes`: declaration first, palette rule for a template
 * saved before the declaration existed) is what the badges on the Templates
 * pages and the deck editor's theme picker read, so a switch here is exactly
 * what a deck will be offered. A template that declares nothing yet is pinned
 * to what its palettes read as the moment it is opened for editing, so the
 * next save writes the declaration the switches show.
 *
 * HEADER & FOOTER: a template also defines what every slide of a deck
 * wearing it says at the bottom (and, optionally, the top) — a PATTERN such as
 * `{{event}} - {{title}} - {{classification}}`. `{{title}}`, `{{subtitle}}`
 * and `{{event}}` read the deck itself; any other placeholder is a VARIABLE
 * declared here (key, label, hint, default) that the deck fills once in its
 * settings, never by editing slides. The card holds the declaration as typed
 * (`extra_metadata.chrome`), checks it live with the same rules the server
 * applies on save (`validateTemplateChrome`, mirroring
 * `normalize_template_chrome`) and previews the line with sample values; a
 * declaration with a problem is refused by the save, so a typo can never be
 * drawn literally on every slide.
 *
 * The editor mutates the two JSON documents the Configuration form already
 * holds and hands them back whole; the raw JSON fields stay available under
 * "Advanced" for anything this form has no control for.
 */
import { useEffect, useRef, useState } from "react";
import { ColorPicker } from "@filigran/design-system";
import { Moon, Plus, Sun, Trash2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type {
  PresentationThemeConfig,
  PresentationThemeScheme,
  TemplateChromeVariable,
  TemplateCoverEmblem,
} from "@/types";

import {
  CHROME_KEY,
  EMBLEM_KEY,
  MAX_CHROME_VARIABLES,
  MAX_FIELD_VALUE_CHARS,
  MAX_PATTERN_CHARS,
  MAX_VARIABLE_HINT_CHARS,
  MAX_VARIABLE_LABEL_CHARS,
  renderChromePattern,
  validateTemplateChrome,
  WATERMARK_KEY,
  type ChromeProblem,
  type TemplateChromeDraft,
} from "./chrome-pattern";
import { COVER_EMBLEM_OPACITY, COVER_EMBLEM_SCALE, coverEmblemOf } from "./design-tokens";
import { DECK_ASPECT_RATIOS } from "./geometry";
import {
  BRAND_HUE_KEYS,
  CHART_SERIES_KEYS,
  CORE_COLOR_KEYS,
  PALETTE_KEY_LABELS,
  PALETTE_SWATCH_KEYS,
  paletteFallback,
  resolvedPaletteColor,
  withBaseKeyTakenBack,
  type PaletteColorKey,
  type PaletteFallback,
} from "./palette-keys";
import {
  DECK_TRANSITIONS,
  declaredSupportedSchemes,
  derivedSupportedSchemes,
  inheritedThemeForScheme,
  paletteScheme,
  previewThemeConfig,
  previewThemeForDeck,
  SCHEME_BOUND_THEME_KEYS,
  SUPPORTED_SCHEMES_KEY,
  templateSupportedSchemes,
  templateThemeVariant,
  THEME_SCHEMES,
} from "./previewSpec";
import { useSchemeLabel } from "./scheme-label";
import { readDeckDefaults, withDeckDefault } from "./template-deck-defaults";
import { isSafeThemeValue, trimThemeValue } from "./theme-value";

type ColorKey = PaletteColorKey;
type FontKey = "heading_font" | "body_font" | "mono_font";

const FONT_KEYS: FontKey[] = ["heading_font", "body_font", "mono_font"];

/**
 * Starting palette for a scheme variant the designer switches on: the
 * scheme-bound surfaces and ink in that scheme (the brand accent, fonts and
 * logos carry over from the base). Palette VALUES for the data, not UI colours.
 */
const VARIANT_STARTERS: Record<PresentationThemeScheme, Partial<Record<ColorKey, string>>> = {
  light: { background: "#ffffff", surface: "#f4f6fa", foreground: "#0f172a", muted: "#5b6472", line: "#dfe4ea" },
  dark: { background: "#0a1220", surface: "#111c2d", foreground: "#ffffff", muted: "#9aa6b6", line: "#22304a" },
};

const str = (value: unknown): string => (typeof value === "string" ? value : "");

function readVariants(extraMetadata: Record<string, unknown>): Record<string, Record<string, unknown>> {
  const variants = extraMetadata.theme_variants;
  if (!variants || typeof variants !== "object" || Array.isArray(variants)) return {};
  const out: Record<string, Record<string, unknown>> = {};
  for (const [scheme, palette] of Object.entries(variants as Record<string, unknown>)) {
    if (palette && typeof palette === "object" && !Array.isArray(palette)) {
      out[scheme] = palette as Record<string, unknown>;
    }
  }
  return out;
}

/** What an empty field draws instead: where the colour comes from, and its value. */
interface ColorFallback {
  source: string;
  value: string;
}

/**
 * A colour value: text for any CSS colour, a native picker when it is #rrggbb.
 *
 * Two controls share one caption, so the wrapper is a `div`, not a `label`: a
 * label may name only one control, and one wrapping both bound its text and
 * its click to whichever the browser picked. The caption carries an id
 * the text input is named by; the picker gets its own distinguishing name.
 */
function ColorField({
  label,
  name,
  value,
  onChange,
  disabled,
  fallback,
}: {
  /** The key's readable name, translated. */
  label: string;
  /** The palette key itself, as Advanced (JSON) and the API spell it. */
  name: string;
  value: string;
  onChange: (next: string) => void;
  disabled?: boolean;
  fallback?: ColorFallback | null;
}) {
  const { t_i18n } = useFormatter();
  const inherited = !value.trim() && fallback ? fallback : null;
  // The design system's ColorPicker: its label above (linked), the swatch, the panel and the hex field in one
  // control. The palette key is the helper line: it is what a designer types in Advanced (JSON). An empty field
  // names, as its placeholder, the colour it falls back to.
  return (
    <ColorPicker
      label={label}
      helperText={<span className="font-mono-plex">{name}</span>}
      value={value}
      disabled={disabled}
      placeholder={inherited ? t_i18n("Falls back to {source}", { source: inherited.source }) : "#rrggbb"}
      className="min-w-0"
      onValueChange={onChange}
    />
  );
}

function PaletteGrid({
  palette,
  keys,
  onChange,
  disabled,
  fallbackFor,
}: {
  palette: Record<string, unknown>;
  keys: readonly ColorKey[];
  onChange: (key: ColorKey, value: string) => void;
  disabled?: boolean;
  fallbackFor?: (key: ColorKey) => ColorFallback | null;
}) {
  const { t_i18n } = useFormatter();
  return (
    // One column below xl: two columns at 1024 leave the input too narrow for
    // the French fallback placeholder; never truncate it.
    <div className="grid gap-1.5 xl:grid-cols-2">
      {keys.map((key) => (
        <ColorField
          key={key}
          label={t_i18n(PALETTE_KEY_LABELS[key])}
          name={key}
          value={str(palette[key])}
          onChange={(value) => onChange(key, value)}
          disabled={disabled}
          fallback={fallbackFor?.(key)}
        />
      ))}
    </div>
  );
}

/** Bar heights of the series preview: a fixed, varied silhouette. */
const SERIES_BAR_HEIGHTS = ["h-[60%]", "h-[85%]", "h-[45%]", "h-full", "h-[70%]", "h-[55%]"];

/**
 * The chart series drawn as bars on the palette's background, redrawn on every
 * keystroke: the colours a deck draws (`chart_1` follows the accent when it is
 * empty); a dashed bar is a series left to the renderer's built-in colour.
 */
function SeriesPreview({ palette }: { palette: PresentationThemeConfig }) {
  const { t_i18n } = useFormatter();
  // Painted the way a deck paints `--deck-bg`, through `background`, so a
  // gradient shows; a shorthand can load a URL, so the theme guard is
  // re-applied here like at every sink.
  const background = trimThemeValue(str(palette.background));
  const paper = background && isSafeThemeValue("background", background) ? { background } : undefined;
  return (
    <div
      role="img"
      aria-label={t_i18n("Chart series preview")}
      className="flex h-12 items-end gap-1 rounded-sm border border-elevation-subtle-soft bg-elevation-default px-2 py-1.5"
      // Data-driven colour: the palette's own background.
      style={paper}
    >
      {CHART_SERIES_KEYS.map((key, index) => {
        const colour = resolvedPaletteColor(key, palette);
        return (
          <span
            key={key}
            data-series={key}
            className={cn(
              "flex-1 rounded-sm",
              SERIES_BAR_HEIGHTS[index % SERIES_BAR_HEIGHTS.length],
              !colour && "border border-dashed border-elevation-subtle-soft",
            )}
            // Data-driven colour: the series value itself.
            style={colour ? { backgroundColor: colour } : undefined}
          />
        );
      })}
    </div>
  );
}

/**
 * A palette's colours in three groups: the core palette, the chart series and
 * the brand hues. `effective` is the palette as a deck reads it (a
 * variant laid over the base), for the preview and what an empty field falls
 * back to.
 */
function PaletteGroups({
  palette,
  coreKeys,
  effective,
  onChange,
  disabled,
  fallbackFor,
}: {
  palette: Record<string, unknown>;
  coreKeys: readonly ColorKey[];
  effective: PresentationThemeConfig;
  onChange: (key: ColorKey, value: string) => void;
  disabled?: boolean;
  fallbackFor: (key: ColorKey) => ColorFallback | null;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div className="space-y-3">
      <PaletteGrid palette={palette} keys={coreKeys} onChange={onChange} disabled={disabled} fallbackFor={fallbackFor} />
      <div className="space-y-1.5">
        <span className="text-[11px] font-medium">{t_i18n("Chart series")}</span>
        <p className="text-[11px] text-default-secondary">
          {t_i18n("The colours of the chart series, in order. Each theme tunes its own. An empty series keeps the built-in colour; the first follows the accent.")}
        </p>
        <SeriesPreview palette={effective} />
        <PaletteGrid palette={palette} keys={CHART_SERIES_KEYS} onChange={onChange} disabled={disabled} fallbackFor={fallbackFor} />
      </div>
      <div className="space-y-1.5">
        <span className="text-[11px] font-medium">{t_i18n("Brand hues (optional)")}</span>
        <p className="text-[11px] text-default-secondary">
          {t_i18n("Product colours an architecture node or zone can wear. An empty hue falls back to the chart series of the same rank.")}
        </p>
        <PaletteGrid palette={palette} keys={BRAND_HUE_KEYS} onChange={onChange} disabled={disabled} fallbackFor={fallbackFor} />
      </div>
    </div>
  );
}

/**
 * The base palette's colour groups and type families (`theme_config`).
 * `effective` is the palette a deck of the base's scheme reads; `shadowed`
 * names the keys a variant stored for that same scheme sets over the base,
 * and `fallbackFor` says what an empty field draws instead.
 */
function BasePaletteFields({
  themeConfig,
  effective,
  shadowed,
  onChange,
  disabled,
  fallbackFor,
}: {
  themeConfig: Record<string, unknown>;
  effective: PresentationThemeConfig;
  shadowed: { scheme: string; keys: readonly string[] };
  onChange: (key: string, value: string) => void;
  disabled?: boolean;
  fallbackFor: (key: ColorKey) => ColorFallback | null;
}) {
  const { t_i18n } = useFormatter();
  const shadow = { scheme: shadowed.scheme, keys: shadowed.keys.join(", ") };
  return (
    <>
      {shadowed.keys.length > 0 && (
        <p className="rounded-sm bg-feedback-alert-primary/10 px-2 py-1.5 content-caption text-feedback-warning-tertiary">
          {/* A base edit never takes `background` back (`withBaseKeyTakenBack`). */}
          {shadowed.keys.includes("background")
            ? t_i18n("Advanced (JSON) also holds a {scheme} variant palette, which decks in this theme lay over this one: {keys} come from it, not from the fields below. Editing one of those fields here removes it from the variant, except background, which decides this palette's theme: to use this palette's background, remove it from the variant in Advanced (JSON).", shadow)
            : t_i18n("Advanced (JSON) also holds a {scheme} variant palette, which decks in this theme lay over this one: {keys} come from it, not from the fields below. Editing one of those fields here removes it from the variant.", shadow)}
        </p>
      )}
      <PaletteGroups
        palette={themeConfig}
        coreKeys={CORE_COLOR_KEYS}
        effective={effective}
        onChange={onChange}
        disabled={disabled}
        fallbackFor={fallbackFor}
      />
      <div className="grid gap-1.5 sm:grid-cols-3">
        {FONT_KEYS.map((key) => (
          <div key={key} className="min-w-0 space-y-1">
            <Label htmlFor={`template-font-${key}`} className="font-mono-plex">{key}</Label>
            <Input aria-label={key}
              id={`template-font-${key}`}
              value={typeof themeConfig[key] === "string" ? (themeConfig[key] as string) : ""}
              disabled={disabled}
              placeholder={key === "mono_font" ? "IBM Plex Mono" : key === "heading_font" ? "Geologica" : "IBM Plex Sans"}
              onChange={(e) => onChange(key, e.target.value)}
            />
          </div>
        ))}
      </div>
    </>
  );
}

/** The core colours a scheme's VARIANT palette edits: the scheme-bound surfaces
 *  and ink, plus the brand colours a theme may want to re-tune. Its chart
 *  series (scheme-bound) and brand hues (carried over, re-tunable) are the
 *  groups every palette shows. */
const VARIANT_CORE_KEYS: ColorKey[] = [
  ...SCHEME_BOUND_THEME_KEYS.filter((k): k is ColorKey => CORE_COLOR_KEYS.includes(k as ColorKey)),
  "accent",
  "gradient_from",
  "gradient_to",
];

// ── Header & footer ──────────────────────────────────────────────────

/** The declaration as the form holds it - strings as typed, never normalised
 *  under the designer's cursor (the save canonicalises). */
function readChromeDraft(extraMetadata: Record<string, unknown>): TemplateChromeDraft {
  const raw = extraMetadata[CHROME_KEY];
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return { footer: "", header: "", variables: [], watermark: false, emblem: false };
  }
  const declaration = raw as Record<string, unknown>;
  const variables: TemplateChromeVariable[] = Array.isArray(declaration.variables)
    ? declaration.variables
        .filter((item): item is Record<string, unknown> => !!item && typeof item === "object" && !Array.isArray(item))
        .map((item) => ({
          key: str(item.key),
          label: str(item.label),
          ...(str(item.hint) ? { hint: str(item.hint) } : {}),
          ...(str(item.default) ? { default: str(item.default) } : {}),
        }))
    : [];
  return {
    footer: str(declaration.footer),
    header: str(declaration.header),
    variables,
    watermark: declaration[WATERMARK_KEY] === true,
    emblem: declaration[EMBLEM_KEY] === true,
    // Placement and the right footer ride through the form untouched.
    ...(typeof declaration.footer_right === "string" ? { footer_right: declaration.footer_right } : {}),
    ...(typeof declaration.emblem_size === "number" ? { emblem_size: declaration.emblem_size } : {}),
    ...(declaration.emblem_align === "margin" || declaration.emblem_align === "title"
      ? { emblem_align: declaration.emblem_align }
      : {}),
    ...(declaration.emblem_ink === "mark" || declaration.emblem_ink === "accent"
      ? { emblem_ink: declaration.emblem_ink }
      : {}),
  };
}

/** Write the draft back: a declaration that says nothing drops the key, which
 *  is how "no pattern, v1 footer" is stored. */
function withChromeDraft(
  extraMetadata: Record<string, unknown>,
  draft: TemplateChromeDraft,
): Record<string, unknown> {
  const next = { ...extraMetadata };
  if (
    !draft.footer.trim() &&
    !draft.header.trim() &&
    draft.variables.length === 0 &&
    !draft.watermark &&
    !draft.emblem &&
    !draft.footer_right
  ) {
    delete next[CHROME_KEY];
    return next;
  }
  next[CHROME_KEY] = {
    footer: draft.footer === "" ? null : draft.footer,
    header: draft.header === "" ? null : draft.header,
    variables: draft.variables.map((variable) => ({
      key: variable.key,
      label: variable.label,
      ...(variable.hint ? { hint: variable.hint } : {}),
      ...(variable.default ? { default: variable.default } : {}),
    })),
    ...(draft.watermark ? { [WATERMARK_KEY]: true } : {}),
    ...(draft.emblem ? { [EMBLEM_KEY]: true } : {}),
    ...(draft.footer_right ? { footer_right: draft.footer_right } : {}),
    ...(draft.emblem_size ? { emblem_size: draft.emblem_size } : {}),
    ...(draft.emblem_align ? { emblem_align: draft.emblem_align } : {}),
    ...(draft.emblem_ink ? { emblem_ink: draft.emblem_ink } : {}),
  };
  return next;
}

/** The emblem sizes the form offers, plus the stored one when it is
 *  off the steps, so a JSON-set size still reads in the select. */
const EMBLEM_SIZE_STEPS = [24, 32, 40, 48, 56, 64, 80, 96] as const;
function emblemSizeOptions(current: number | undefined): number[] {
  const steps: number[] = [...EMBLEM_SIZE_STEPS];
  if (current && !steps.includes(current)) steps.push(current);
  return steps.sort((a, b) => a - b);
}

/** Sample values the preview fills the patterns with - a variable shows its
 *  default, else its label in brackets, so an unset one still reads. */
function sampleChromeValues(
  draft: TemplateChromeDraft,
  samples: { title: string; subtitle: string; event: string },
): Record<string, string> {
  const values: Record<string, string> = { ...samples };
  for (const variable of draft.variables) {
    const key = variable.key.trim().toLowerCase();
    if (key) values[key] = variable.default || (variable.label ? `[${variable.label}]` : `[${key}]`);
  }
  return values;
}

/** Steps offered for the cover emblem; a stored value off the steps is kept and listed. */
const COVER_EMBLEM_SCALE_STEPS = [60, 80, 95, 110, 125, 140] as const;
const COVER_EMBLEM_OPACITY_STEPS = [3, 5, 7, 10] as const;

const withStored = (steps: readonly number[], stored: number | undefined): number[] =>
  stored !== undefined && !steps.includes(stored) ? [...steps, stored].sort((a, b) => a - b) : [...steps];

/** The cover emblem scale steps the form offers, kept in the token's range. */
function coverEmblemScaleOptions(stored: number | undefined): number[] {
  return withStored(COVER_EMBLEM_SCALE_STEPS, stored).filter(
    (n) => n >= COVER_EMBLEM_SCALE.min && n <= COVER_EMBLEM_SCALE.max,
  );
}

/** The cover emblem opacity steps the form offers, kept in the token's range. */
function coverEmblemOpacityOptions(stored: number | undefined): number[] {
  return withStored(COVER_EMBLEM_OPACITY_STEPS, stored).filter(
    (n) => n >= COVER_EMBLEM_OPACITY.min && n <= COVER_EMBLEM_OPACITY.max,
  );
}

/**
 * The brand cover emblem (`extra_metadata.design.cover_emblem`): the
 * mark the `cover: brand` backdrop draws behind the cover and the closing.
 * Shown only on a template that declares the brand cover; an unset key keeps
 * the renderer's own value (95% of the slide height, cropped bottom right, 7%).
 */
function CoverEmblemEditor({
  extraMetadata,
  onExtraMetadataChange,
  disabled,
}: {
  extraMetadata: Record<string, unknown>;
  onExtraMetadataChange: (next: Record<string, unknown>) => void;
  disabled?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const raw = extraMetadata.design;
  const design = raw && typeof raw === "object" && !Array.isArray(raw) ? (raw as Record<string, unknown>) : null;
  if (design?.cover !== "brand") return null;
  const emblem = coverEmblemOf(design.cover_emblem) ?? {};
  const setEmblem = (patch: Partial<TemplateCoverEmblem>) => {
    const next: Record<string, unknown> = { ...emblem, ...patch };
    for (const key of Object.keys(next)) if (next[key] === undefined) delete next[key];
    const nextDesign = { ...design };
    if (Object.keys(next).length > 0) nextDesign.cover_emblem = next;
    else delete nextDesign.cover_emblem;
    onExtraMetadataChange({ ...extraMetadata, design: nextDesign });
  };
  // The value each trigger shows, also its title: a long translation truncates
  // inside its column instead of painting over the next trigger.
  const scaleLabel = emblem.scale !== undefined ? t_i18n("{n}%", { n: emblem.scale }) : t_i18n("Default size");
  const anchorLabel = emblem.anchor === "right" ? t_i18n("Inside the right edge") : t_i18n("Cropped by the bottom-right corner");
  const opacityLabel = emblem.opacity !== undefined ? t_i18n("{n}%", { n: emblem.opacity }) : t_i18n("Default");
  return (
    <div className="space-y-2 rounded-sm border border-elevation-subtle-soft p-3" data-testid="template-cover-emblem">
      <div>
        <p className="content-compact font-medium">{t_i18n("Cover emblem")}</p>
        <p className="text-[11px] text-default-secondary">
          {t_i18n("The template's mark drawn faint behind the cover and the closing slide. Sized on the slide height, so 16:9 and 4:3 draw it alike.")}
        </p>
      </div>
      <div className="grid gap-2 sm:grid-cols-3">
        <div className="min-w-0 space-y-1">
          <Label htmlFor="template-cover-emblem-size">{t_i18n("Size, % of the slide height")}</Label>
          <Select
            value={emblem.scale !== undefined ? String(emblem.scale) : "__unset__"}
            disabled={disabled}
            onValueChange={(v) => setEmblem({ scale: v === "__unset__" ? undefined : Number(v) })}
          >
            <SelectTrigger id="template-cover-emblem-size" className="w-full min-w-0" aria-label={t_i18n("Cover emblem size")} title={scaleLabel}><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="__unset__">{t_i18n("Default size")}</SelectItem>
              {coverEmblemScaleOptions(emblem.scale).map((n) => (
                  <SelectItem key={n} value={String(n)}>{t_i18n("{n}%", { n })}</SelectItem>
                ))}
            </SelectContent>
          </Select>
        </div>
        <div className="min-w-0 space-y-1">
          <Label htmlFor="template-cover-emblem-placement">{t_i18n("Cover emblem placement")}</Label>
          <Select
            value={emblem.anchor ?? "bottom_right"}
            disabled={disabled}
            onValueChange={(v) => setEmblem({ anchor: v === "right" ? "right" : "bottom_right" })}
          >
            <SelectTrigger id="template-cover-emblem-placement" className="w-full min-w-0" aria-label={t_i18n("Cover emblem placement")} title={anchorLabel}><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="bottom_right">{t_i18n("Cropped by the bottom-right corner")}</SelectItem>
              <SelectItem value="right">{t_i18n("Inside the right edge")}</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="min-w-0 space-y-1">
          <Label htmlFor="template-cover-emblem-opacity">{t_i18n("Cover emblem opacity")}</Label>
          <Select
            value={emblem.opacity !== undefined ? String(emblem.opacity) : "__unset__"}
            disabled={disabled}
            onValueChange={(v) => setEmblem({ opacity: v === "__unset__" ? undefined : Number(v) })}
          >
            <SelectTrigger id="template-cover-emblem-opacity" className="w-full min-w-0" aria-label={t_i18n("Cover emblem opacity")} title={opacityLabel}><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="__unset__">{t_i18n("Default")}</SelectItem>
              {coverEmblemOpacityOptions(emblem.opacity).map((n) => (
                  <SelectItem key={n} value={String(n)}>{t_i18n("{n}%", { n })}</SelectItem>
                ))}
            </SelectContent>
          </Select>
        </div>
      </div>
    </div>
  );
}

function ChromeEditor({
  extraMetadata,
  onExtraMetadataChange,
  disabled,
}: {
  extraMetadata: Record<string, unknown>;
  onExtraMetadataChange: (next: Record<string, unknown>) => void;
  disabled?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const [year] = useState(() => String(new Date().getFullYear()));
  const draft = readChromeDraft(extraMetadata);
  const { problems } = validateTemplateChrome(draft);
  const footerRight = draft.footer_right?.trim() ?? "";
  const declares = !!draft.footer.trim() || !!draft.header.trim() || !!footerRight;
  const samples = { title: t_i18n("Quarterly review"), subtitle: t_i18n("Q3 2026"), event: t_i18n("FIC 2026") };
  const values = sampleChromeValues(draft, samples);
  const footerPreview = renderChromePattern(draft.footer, values);
  const headerPreview = renderChromePattern(draft.header, values);
  // The right corner reads the slide number and the year as well:
  // the sample is the first slide after the cover, in the current year.
  const footerRightPreview = footerRight
    ? renderChromePattern(footerRight, { ...values, page: "01", year })
    : "";

  const setDraft = (next: TemplateChromeDraft) => onExtraMetadataChange(withChromeDraft(extraMetadata, next));
  const setVariable = (index: number, patch: Partial<TemplateChromeVariable>) =>
    setDraft({
      ...draft,
      variables: draft.variables.map((variable, i) => {
        if (i !== index) return variable;
        const next = { ...variable, ...patch };
        if (!next.hint) delete next.hint;
        if (!next.default) delete next.default;
        return next;
      }),
    });

  // Open braces are passed as ARGUMENTS: a literal `{` in an ICU message is
  // an argument start, so the message text itself never carries one.
  const braces = { open: "{{", close: "}}", ph: "{{key}}" };
  const slotName = (slot: ChromeProblem["slot"]) => (slot === "header" ? t_i18n("Header") : t_i18n("Footer"));
  const describe = (problem: ChromeProblem): string => {
    const n = (problem.index ?? 0) + 1;
    switch (problem.code) {
      case "pattern_too_long":
        return t_i18n("{slot} pattern is over {max} characters - a footer is one line.", { slot: slotName(problem.slot), max: MAX_PATTERN_CHARS });
      case "pattern_unbalanced":
        return t_i18n("{slot} pattern has an unbalanced placeholder: every {open} needs its {close}.", { slot: slotName(problem.slot), ...braces });
      case "pattern_malformed_slot":
        return t_i18n("{slot} pattern has slots that are not placeholders: {detail}. A placeholder is {ph} - one word, letters, digits and underscores.", { slot: slotName(problem.slot), detail: problem.detail ?? "", ...braces });
      case "too_many_variables":
        return t_i18n("{count} variables declared; the maximum is {max} - a footer is one line, not a form.", { count: problem.detail ?? "", max: MAX_CHROME_VARIABLES });
      case "variable_key_invalid":
        return t_i18n("Variable {n}: key {detail} is not valid - use a lowercase word (letters, digits, underscores, up to 32 characters); the pattern spells it {ph}.", { n, detail: problem.detail ?? "", ...braces });
      case "variable_key_builtin":
        return t_i18n("Variable {n}: {detail} is a built-in placeholder (title, subtitle and event are read from the deck) and cannot be declared.", { n, detail: problem.detail ?? "" });
      case "variable_key_duplicate":
        return t_i18n("Variable {n}: key {detail} is declared twice.", { n, detail: problem.detail ?? "" });
      case "variable_label_missing":
        return t_i18n("Variable {n} ({detail}) needs a label: it names the field in the deck settings.", { n, detail: problem.detail ?? "" });
      case "variable_label_too_long":
        return t_i18n("Variable {n} ({detail}): the label is over {max} characters.", { n, detail: problem.detail ?? "", max: MAX_VARIABLE_LABEL_CHARS });
      case "variable_hint_too_long":
        return t_i18n("Variable {n} ({detail}): the hint is over {max} characters.", { n, detail: problem.detail ?? "", max: MAX_VARIABLE_HINT_CHARS });
      case "variable_default_too_long":
        return t_i18n("Variable {n} ({detail}): the default is over {max} characters.", { n, detail: problem.detail ?? "", max: MAX_FIELD_VALUE_CHARS });
      case "variables_without_pattern":
        return t_i18n("Variables are declared but there is no footer or header pattern to fill them into.");
      case "placeholder_undeclared":
        return t_i18n("The patterns use placeholders no variable declares: {detail}. Declare them below (key and label).", { detail: problem.detail ?? "" });
      case "variable_unused":
        return t_i18n("Declared but used by no pattern: {detail}. Put it in a pattern or remove the variable.", { detail: problem.detail ?? "" });
    }
  };

  return (
    <div className="space-y-2 rounded-sm border border-elevation-subtle-soft p-3">
      <span className="content-compact font-medium">{t_i18n("Header & footer")}</span>
      <p className="text-[11px] text-default-secondary">
        {t_i18n(
          "What every slide of a deck wearing this template says at the bottom - and, if you want, at the top. Write a pattern with placeholders: {title}, {subtitle} and {event} read the deck itself; any other {ph} is a variable declared below, which the deck fills once in its settings, never by editing slides. An empty placeholder takes its separator with it.",
          { title: "{{title}}", subtitle: "{{subtitle}}", event: "{{event}}", ph: "{{word}}" },
        )}
      </p>
      <div className="grid gap-2 sm:grid-cols-2">
        <div className="space-y-1">
          <Label htmlFor="template-chrome-footer">
            {t_i18n("Footer pattern")}
          </Label>
          <Input aria-label={t_i18n("Footer pattern")}
            id="template-chrome-footer"
            value={draft.footer}
            disabled={disabled}
            onChange={(e) => setDraft({ ...draft, footer: e.target.value })}
            placeholder="{{event}} - {{title}} - {{classification}}"
            className="font-mono-plex content-compact"
            maxLength={MAX_PATTERN_CHARS}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="template-chrome-header">
            {t_i18n("Header pattern (optional)")}
          </Label>
          <Input aria-label={t_i18n("Header pattern (optional)")}
            id="template-chrome-header"
            value={draft.header}
            disabled={disabled}
            onChange={(e) => setDraft({ ...draft, header: e.target.value })}
            placeholder="{{title}}"
            className="font-mono-plex content-compact"
            maxLength={MAX_PATTERN_CHARS}
          />
        </div>
      </div>

      <div className="space-y-1.5">
        <div className="flex items-center justify-between gap-2">
          <span className="content-caption text-default-secondary">{t_i18n("Variables")}</span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-7 gap-1 px-2 text-[11px]"
            disabled={disabled || draft.variables.length >= MAX_CHROME_VARIABLES}
            onClick={() => setDraft({ ...draft, variables: [...draft.variables, { key: "", label: "" }] })}
          >
            <Plus className="h-3 w-3" />
            {t_i18n("Add variable")}
          </Button>
        </div>
        {draft.variables.length === 0 ? (
          <p className="text-[11px] text-default-secondary">
            {declares
              ? t_i18n("None: the patterns read the deck title, the opening slide's subtitle and the event only.")
              : t_i18n("None yet. A variable is a footer field the deck fills once - a classification, a client, a version.")}
          </p>
        ) : (
          <div className="space-y-1.5">
            {draft.variables.map((variable, index) => (
              <div key={index} className="space-y-1.5 rounded-sm border border-elevation-subtle-soft p-2">
                <div className="grid gap-1.5 sm:grid-cols-[1fr_1fr_auto]">
                  <Input
                    value={variable.key}
                    disabled={disabled}
                    onChange={(e) => setVariable(index, { key: e.target.value })}
                    placeholder={t_i18n("key, e.g. classification")}
                    className="font-mono-plex content-compact"
                    aria-label={t_i18n("Variable {n} key", { n: index + 1 })}
                    maxLength={32}
                  />
                  <Input
                    value={variable.label}
                    disabled={disabled}
                    onChange={(e) => setVariable(index, { label: e.target.value })}
                    placeholder={t_i18n("Label shown in the deck settings")}
                    className="content-compact"
                    aria-label={t_i18n("Variable {n} label", { n: index + 1 })}
                    maxLength={MAX_VARIABLE_LABEL_CHARS}
                  />
                  <Tip content={t_i18n("Remove variable")}>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="h-8 w-8 p-0"
                      disabled={disabled}
                      onClick={() => setDraft({ ...draft, variables: draft.variables.filter((_, i) => i !== index) })}
                      aria-label={t_i18n("Remove variable")}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </Tip>
                </div>
                <div className="grid gap-1.5 sm:grid-cols-2">
                  <Input
                    value={variable.hint ?? ""}
                    disabled={disabled}
                    onChange={(e) => setVariable(index, { hint: e.target.value })}
                    placeholder={t_i18n("Hint under the field (optional)")}
                    className="content-compact"
                    aria-label={t_i18n("Variable {n} hint", { n: index + 1 })}
                    maxLength={MAX_VARIABLE_HINT_CHARS}
                  />
                  <Input
                    value={variable.default ?? ""}
                    disabled={disabled}
                    onChange={(e) => setVariable(index, { default: e.target.value })}
                    placeholder={t_i18n("Default when the deck sets nothing (optional)")}
                    className="content-compact"
                    aria-label={t_i18n("Variable {n} default", { n: index + 1 })}
                    maxLength={MAX_FIELD_VALUE_CHARS}
                  />
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {problems.length > 0 && (
        <div className="space-y-1 rounded-sm bg-feedback-alert-primary/10 px-2 py-1.5">
          {problems.map((problem, index) => (
            <p key={index} className="content-caption text-feedback-warning-primary">{describe(problem)}</p>
          ))}
        </div>
      )}

      <div className="flex items-center gap-3 rounded-sm border border-elevation-subtle-soft px-2.5 py-2">
        <div className="min-w-0 flex-1">
          <p className="content-compact font-medium">{t_i18n("Brand watermark")}</p>
          <p className="text-[11px] text-default-secondary">
            {t_i18n(
              "Draws the template's mark faint in the corner of every content, agenda, roadmap, takeaways and dashboard slide, over a soft glow of the brand gradient. Needs an asset with the logo_mark role; a deck that hides its logo hides it too.",
            )}
          </p>
        </div>
        <Switch
          checked={!!draft.watermark}
          disabled={disabled}
          onCheckedChange={(checked) => setDraft({ ...draft, watermark: checked })}
          aria-label={t_i18n("Brand watermark")}
        />
      </div>

      <div className="flex items-center gap-3 rounded-sm border border-elevation-subtle-soft px-2.5 py-2">
        <div className="min-w-0 flex-1">
          <p className="content-compact font-medium">{t_i18n("Corner emblem")}</p>
          <p className="text-[11px] text-default-secondary">
            {t_i18n(
              "Draws the template's mark small, at full strength, in the top-right corner of every content and section slide. Needs an asset with the logo_mark role; a deck that hides its logo hides it too.",
            )}
          </p>
        </div>
        <Switch
          checked={!!draft.emblem}
          disabled={disabled}
          onCheckedChange={(checked) => setDraft({ ...draft, emblem: checked })}
          aria-label={t_i18n("Corner emblem")}
        />
      </div>

      {/* The right footer and the emblem placement and ink: edited
          here instead of in the extra metadata JSON only. */}
      <div className="space-y-2 rounded-sm border border-elevation-subtle-soft px-2.5 py-2" data-testid="template-chrome-placement">
        <div className="space-y-1">
          <Label htmlFor="template-chrome-footer-right">
            {t_i18n("Right footer pattern (optional)")}
          </Label>
          <Input aria-label={t_i18n("Right footer pattern (optional)")}
            id="template-chrome-footer-right"
            value={draft.footer_right ?? ""}
            disabled={disabled}
            onChange={(e) => setDraft({ ...draft, footer_right: e.target.value || undefined })}
            placeholder="{{page}} · © {{year}}"
            className="font-mono-plex content-compact"
            maxLength={MAX_PATTERN_CHARS}
          />
          <p className="text-[11px] text-default-secondary">
            {t_i18n("Replaces the slide number in the footer's right corner. {page} is the slide number, {year} the current year.", { page: "{{page}}", year: "{{year}}" })}
          </p>
        </div>
        <div className="grid gap-2 sm:grid-cols-2">
          <div className="space-y-1">
            <Label htmlFor="template-emblem-size">{t_i18n("Emblem size")}</Label>
            {/* Steps of the 24 to 96 px range the backend accepts; a size
                stored outside the steps (set in the JSON) is kept and listed. */}
            <Select
              value={draft.emblem_size ? String(draft.emblem_size) : "__unset__"}
              disabled={disabled}
              onValueChange={(v) => setDraft({ ...draft, emblem_size: v === "__unset__" ? undefined : Number(v) })}
            >
              <SelectTrigger id="template-emblem-size" className="w-full min-w-0" aria-label={t_i18n("Emblem size")}><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__unset__">{t_i18n("Default size")}</SelectItem>
                {emblemSizeOptions(draft.emblem_size).map((size) => (
                  <SelectItem key={size} value={String(size)}>{t_i18n("{n} px", { n: size })}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="template-emblem-placement">{t_i18n("Emblem placement")}</Label>
            <Select
              value={draft.emblem_align ?? "margin"}
              disabled={disabled}
              onValueChange={(v) => setDraft({ ...draft, emblem_align: v === "title" ? "title" : undefined })}
            >
              <SelectTrigger id="template-emblem-placement" className="w-full min-w-0" aria-label={t_i18n("Emblem placement")}><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="margin">{t_i18n("In the top margin")}</SelectItem>
                <SelectItem value="title">{t_i18n("Level with the slide title")}</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <div className="min-w-0 flex-1">
            <p className="content-compact font-medium">{t_i18n("Ink the emblem with the slide accent")}</p>
            <p className="text-[11px] text-default-secondary">
              {t_i18n("Draws the mark in one colour, the slide's accent, instead of its own colours, so a one-colour mark reads on the dark and the light theme.")}
            </p>
          </div>
          <Switch
            checked={draft.emblem_ink === "accent"}
            disabled={disabled}
            onCheckedChange={(checked) => setDraft({ ...draft, emblem_ink: checked ? "accent" : undefined })}
            aria-label={t_i18n("Ink the emblem with the slide accent")}
          />
        </div>
      </div>

      {declares && (
        <div className="space-y-1 rounded-sm border border-dashed border-elevation-subtle-soft px-2.5 py-2">
          <p className="content-caption uppercase tracking-wide text-default-secondary">
            {t_i18n("Preview with sample values")}
          </p>
          <p className={cn("truncate text-[12px]", !footerPreview && "italic text-default-secondary")}>
            <span className="text-default-secondary">{t_i18n("Footer")}: </span>
            {footerPreview || t_i18n("nothing")}
          </p>
          {draft.header.trim() && (
            <p className={cn("truncate text-[12px]", !headerPreview && "italic text-default-secondary")}>
              <span className="text-default-secondary">{t_i18n("Header")}: </span>
              {headerPreview || t_i18n("nothing")}
            </p>
          )}
          {footerRight && (
            <p
              className={cn("truncate text-[12px]", !footerRightPreview && "italic text-default-secondary")}
              data-testid="template-chrome-preview-footer-right"
            >
              <span className="text-default-secondary">{t_i18n("Right footer")}: </span>
              {footerRightPreview || t_i18n("nothing")}
            </p>
          )}
          <p className="content-caption text-default-secondary">
            {t_i18n("Sample deck: title {title}, subtitle {subtitle}, event {event}; variables show their default or their label in brackets.", samples)}
          </p>
        </div>
      )}
    </div>
  );
}

/**
 * Which stored document holds a supported scheme's palette. The base palette
 * belongs to the scheme its background reads as; with no readable background
 * it serves the first supported scheme. Every other supported scheme is a
 * `theme_variants` palette. Mirrors `scheme_palette` on the backend, which is
 * how the renderer will read the same documents back.
 */
function paletteSlotFor(
  scheme: PresentationThemeScheme,
  designedFor: PresentationThemeScheme | null,
  supported: readonly PresentationThemeScheme[],
): "base" | "variant" {
  if (designedFor === scheme) return "base";
  if (designedFor === null && supported[0] === scheme) return "base";
  return "variant";
}

/**
 * The theme a design's base palette is drawn for (`paletteSlotFor`): the one
 * its background reads as, else the first the template supports. A variant
 * stored for that theme is laid over the base by every deck in it.
 */
function baseSchemeOf(design: Parameters<typeof templateSupportedSchemes>[0]): PresentationThemeScheme {
  return paletteScheme(previewThemeConfig(design.theme_config)) ?? templateSupportedSchemes(design)[0];
}

/**
 * The deck default theme a declaration change leaves valid: unchanged when the
 * template still supports it, the first supported scheme otherwise (the same
 * snap the backend applies when the deck is created).
 */
function withDefaultThemeIn(
  extraMetadata: Record<string, unknown>,
  supported: readonly PresentationThemeScheme[],
): Record<string, unknown> {
  const current = readDeckDefaults(extraMetadata).theme;
  if (!current || (supported as readonly string[]).includes(current)) return extraMetadata;
  // Through the one writer, so the per-cover objects ride along.
  return withDeckDefault(extraMetadata, "theme", supported[0]);
}

export function TemplateDesignEditor({
  themeConfig,
  extraMetadata,
  onThemeConfigChange,
  onExtraMetadataChange,
  disabled,
}: {
  themeConfig: Record<string, unknown>;
  extraMetadata: Record<string, unknown>;
  onThemeConfigChange: (next: Record<string, unknown>) => void;
  onExtraMetadataChange: (next: Record<string, unknown>) => void;
  disabled?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const schemeLabel = useSchemeLabel();

  const design = { theme_config: themeConfig, extra_metadata: extraMetadata };
  const base = previewThemeConfig(themeConfig);
  const designedFor = paletteScheme(base);
  const declared = declaredSupportedSchemes(design);
  const derived = derivedSupportedSchemes(design);
  const supported = declared ?? derived;
  const variants = readVariants(extraMetadata);
  const deckDefaults = readDeckDefaults(extraMetadata);
  // The theme the base palette is drawn for. A variant stored for that same
  // theme is valid JSON, and decks lay it over the base: the keys it sets are
  // not what the base fields say.
  const baseScheme = baseSchemeOf(design);
  const baseShadow = templateThemeVariant(design, baseScheme);
  // What decks in that theme draw: the base with that variant laid over it.
  const baseEffective = previewThemeForDeck(design, baseScheme);
  const shadowed = {
    scheme: schemeLabel(baseScheme),
    keys: [...PALETTE_SWATCH_KEYS, ...FONT_KEYS].filter((key) => key in baseShadow),
  };

  // A template saved before the declaration existed is pinned to what
  // its palettes read as, once, the moment it is opened for editing: the
  // switches below then SHOW what the next save WRITES, and a later colour
  // change can no longer flip the supported themes behind the designer's back.
  const pinned = useRef(false);
  useEffect(() => {
    if (disabled || declared !== null || pinned.current) return;
    pinned.current = true;
    onExtraMetadataChange({ ...extraMetadata, [SUPPORTED_SCHEMES_KEY]: derived });
  }, [disabled, declared, derived, extraMetadata, onExtraMetadataChange]);

  const setBaseKey = (key: string, value: string) => {
    const next = { ...themeConfig };
    if (value.trim()) next[key] = value;
    else delete next[key];
    onThemeConfigChange(next);
    // The field now decides the key for its theme - the theme the EDITED
    // palette is drawn for: a variant stored for that theme setting the key
    // would keep laying its own value over the edit.
    const editedScheme = baseSchemeOf({ theme_config: next, extra_metadata: extraMetadata });
    const unshadowed = withBaseKeyTakenBack(extraMetadata, editedScheme, key);
    if (unshadowed !== extraMetadata) onExtraMetadataChange(unshadowed);
  };

  const withVariant = (
    metadata: Record<string, unknown>,
    scheme: PresentationThemeScheme,
    palette: Record<string, unknown> | null,
  ): Record<string, unknown> => {
    const nextVariants = { ...readVariants(metadata) };
    if (palette) nextVariants[scheme] = palette;
    else delete nextVariants[scheme];
    const next = { ...metadata };
    if (Object.keys(nextVariants).length > 0) next.theme_variants = nextVariants;
    else delete next.theme_variants;
    return next;
  };

  const setVariantKey = (scheme: PresentationThemeScheme, key: string, value: string) => {
    const palette = { ...(variants[scheme] ?? {}) };
    if (value.trim()) palette[key] = value;
    else delete palette[key];
    onExtraMetadataChange(withVariant(extraMetadata, scheme, palette));
  };

  // One string key at a time; the per-cover objects a brand declares in the
  // JSON (`hero_defaults` / `hero_tones` / `slide_themes`) are carried
  // through by the writer - rebuilding the object from the selects' strings
  // dropped them on the first transition change.
  const setDeckDefault = (key: string, value: string | null) =>
    onExtraMetadataChange(withDeckDefault(extraMetadata, key, value));

  /**
   * Declare or withdraw a theme. Switching a theme ON whose palette would be a
   * variant seeds that variant with starter surfaces so the designer edits real
   * values rather than an empty grid; switching it OFF drops the dead variant
   * and snaps a deck default that pointed at it. The last theme cannot be
   * switched off: a template that supports no theme can dress no deck.
   */
  const setSchemeSupported = (scheme: PresentationThemeScheme, on: boolean) => {
    const nextSupported = THEME_SCHEMES.filter((s) => (s === scheme ? on : supported.includes(s)));
    if (nextSupported.length === 0) return;
    let next: Record<string, unknown> = { ...extraMetadata, [SUPPORTED_SCHEMES_KEY]: nextSupported };
    if (paletteSlotFor(scheme, designedFor, nextSupported) === "variant") {
      const existing = variants[scheme];
      next = withVariant(next, scheme, on ? (existing ?? { ...VARIANT_STARTERS[scheme] }) : null);
    }
    onExtraMetadataChange(withDefaultThemeIn(next, nextSupported));
  };

  // The base palette reads as a theme the template withdrew: decks in the
  // supported theme still render (built-in surfaces, brand carried over) but
  // the designer controls none of their surfaces from here.
  const baseUnsupported = designedFor !== null && !supported.includes(designedFor);
  // Every supported theme the base palette does not serve has a variant card.
  const variantSchemes = supported.filter((scheme) => paletteSlotFor(scheme, designedFor, supported) === "variant");
  // What an empty field shows: what its palette takes the key from (`from`: a
  // variant the base palette, the base palette a variant stored for its own
  // theme), or the key its colour falls back to (`paletteFallback`).
  const fallbackView = (fallback: PaletteFallback | null, from: string): ColorFallback | null =>
    fallback && {
      // A key the colour falls back to is named as the form captions it.
      source: fallback.source === "inherited" ? from : t_i18n(PALETTE_KEY_LABELS[fallback.source]),
      value: fallback.value,
    };
  const baseFallbackFor = (key: ColorKey) =>
    fallbackView(paletteFallback(key, baseEffective, baseShadow), t_i18n("Variant palette"));

  return (
    <div className="space-y-4">
      {/* What the deck will be allowed to pick, decided before any colour. */}
      <div className="space-y-2 rounded-sm bg-elevation-hover p-3">
        <span className="content-compact font-medium">{t_i18n("Themes this template supports")}</span>
        <p className="text-[11px] text-default-secondary">
          {t_i18n("Decks wearing this template can only pick a supported theme. Each supported theme gets its own palette below; at least one stays on.")}
        </p>
        <div className="grid gap-2 sm:grid-cols-2">
          {THEME_SCHEMES.map((scheme) => {
            const on = supported.includes(scheme);
            const last = on && supported.length === 1;
            const Icon = scheme === "dark" ? Moon : Sun;
            return (
              <div
                key={scheme}
                className={cn(
                  "flex items-center gap-3 rounded-sm border px-3 py-2",
                  on ? "border-accent/40 bg-accent/5" : "border-dashed border-elevation-subtle-soft",
                )}
              >
                <Icon className={cn("h-4 w-4 shrink-0", on ? "text-accent" : "text-default-secondary")} />
                <div className="min-w-0 flex-1">
                  <p className="content-compact font-medium">{t_i18n("{scheme} theme", { scheme: schemeLabel(scheme) })}</p>
                  <p className="text-[11px] text-default-secondary">
                    {on
                      ? last
                        ? t_i18n("Supported - the only theme, so it stays on.")
                        : t_i18n("Supported")
                      : t_i18n("Not supported")}
                  </p>
                </div>
                <Switch
                  checked={on}
                  disabled={disabled || last}
                  onCheckedChange={(checked) => setSchemeSupported(scheme, checked)}
                  aria-label={t_i18n("{scheme} theme", { scheme: schemeLabel(scheme) })}
                />
              </div>
            );
          })}
        </div>
      </div>

      {/* The base palette first, as ONE card whatever theme its background
          reads as: a colour typed key by key passes through values that read
          as the other theme, and a card that moved or remounted would take
          the focused field (and an open colour picker) with it. */}
      <div
        data-palette-scheme={baseUnsupported ? undefined : baseScheme}
        className={cn(
          "space-y-2 rounded-sm border p-3",
          baseUnsupported ? "border-dashed border-feedback-alert-primary/40" : "border-elevation-subtle-soft",
        )}
      >
        <div className="flex flex-wrap items-center gap-2">
          {baseUnsupported ? (
            <>
              <span className="content-compact font-medium">{t_i18n("Base palette")}</span>
              <Badge variant="outline" className="text-[9px] px-1.5 py-0">
                {t_i18n("Reads as {scheme}", { scheme: schemeLabel(baseScheme) })}
              </Badge>
            </>
          ) : (
            <>
              <span className="content-compact font-medium">{t_i18n("{scheme} palette", { scheme: schemeLabel(baseScheme) })}</span>
              <Badge variant="outline" className="text-[9px] px-1.5 py-0">{t_i18n("Base palette")}</Badge>
              {designedFor === null && (
                <Badge variant="outline" className="text-[9px] px-1.5 py-0">{t_i18n("No background set")}</Badge>
              )}
            </>
          )}
        </div>
        {/* A base palette read as a withdrawn theme is still the brand source,
            but none of its surfaces reach a deck. */}
        {baseUnsupported ? (
          <p className="rounded-sm bg-feedback-alert-primary/10 px-2 py-1.5 content-caption text-feedback-warning-tertiary">
            {t_i18n("Its background reads as {reads}, a theme this template does not support: only its accent, fonts and logos reach a deck. Give it a {scheme} background to make it the {scheme} palette, or switch {reads} on.", {
              reads: schemeLabel(baseScheme),
              scheme: schemeLabel(supported[0]),
            })}
          </p>
        ) : (
          <p className="text-[11px] text-default-secondary">
            {t_i18n("The full palette and the type families. Everything here is inherited live by decks wearing the template; the other theme's palette lays its surfaces over it.")}
          </p>
        )}
        <BasePaletteFields
          themeConfig={themeConfig}
          effective={baseEffective}
          shadowed={shadowed}
          onChange={setBaseKey}
          disabled={disabled}
          fallbackFor={baseFallbackFor}
        />
      </div>

      {/* Then the variant palette of every other supported theme. */}
      {variantSchemes.map((scheme) => {
        const variant = templateThemeVariant(design, scheme);
        // The variant as a deck of this theme reads it, and what it takes from
        // the base: the renderer's own rule, never re-spelled here.
        const effective = previewThemeForDeck(design, scheme);
        const inherited = inheritedThemeForScheme(design, scheme);
        const variantEmpty = Object.keys(variant).length === 0;
        const readsAs = paletteScheme(variant);
        const mismatch = readsAs !== null && readsAs !== scheme;
        return (
          <div key={scheme} data-palette-scheme={scheme} className="space-y-2 rounded-sm border border-elevation-subtle-soft p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="content-compact font-medium">{t_i18n("{scheme} palette", { scheme: schemeLabel(scheme) })}</span>
              <Badge variant="outline" className="text-[9px] px-1.5 py-0">{t_i18n("Variant palette")}</Badge>
            </div>
            <p className="text-[11px] text-default-secondary">
              {variantEmpty
                ? t_i18n("Empty: decks in this theme use the renderer's built-in surfaces with the base palette's accent, fonts and logos. Set colours here to control them.")
                : t_i18n("Only the colours set here differ from the base palette; the accent, fonts and logos carry over.")}
            </p>
            {mismatch && (
              <p className="rounded-sm bg-feedback-alert-primary/10 px-2 py-1.5 content-caption text-feedback-warning-tertiary">
                {t_i18n("This palette's background reads as {reads}: {scheme} decks wearing this template will look {reads}. Give it a {scheme} background.", {
                  reads: schemeLabel(readsAs as PresentationThemeScheme),
                  scheme: schemeLabel(scheme),
                })}
              </p>
            )}
            <PaletteGroups
              palette={variants[scheme] ?? {}}
              coreKeys={VARIANT_CORE_KEYS}
              effective={effective}
              onChange={(key, value) => setVariantKey(scheme, key, value)}
              disabled={disabled}
              fallbackFor={(key) => fallbackView(paletteFallback(key, effective, inherited), t_i18n("Base palette"))}
            />
          </div>
        );
      })}

      {/* Deck defaults */}
      <div className="space-y-2 rounded-sm border border-elevation-subtle-soft p-3">
        <span className="content-compact font-medium">{t_i18n("Deck defaults")}</span>
        <p className="text-[11px] text-default-secondary">
          {t_i18n("Pre-filled on a new deck when the agent does not set them. A deck can always change them afterwards; a default theme the template does not support is snapped to one it does.")}
        </p>
        <div className="grid gap-2 sm:grid-cols-3">
          <div className="space-y-1">
            <Label htmlFor="template-default-theme">{t_i18n("Theme")}</Label>
            <Select
              value={deckDefaults.theme ?? "__unset__"}
              disabled={disabled}
              onValueChange={(v) => setDeckDefault("theme", v === "__unset__" ? null : v)}
            >
              <SelectTrigger id="template-default-theme" className="w-full min-w-0"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__unset__">{t_i18n("No default")}</SelectItem>
                {supported.map((scheme) => (
                  <SelectItem key={scheme} value={scheme}>{schemeLabel(scheme)}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="template-default-transition">{t_i18n("Transition")}</Label>
            <Select
              value={deckDefaults.transition ?? "__unset__"}
              disabled={disabled}
              onValueChange={(v) => setDeckDefault("transition", v === "__unset__" ? null : v)}
            >
              <SelectTrigger id="template-default-transition" className="w-full min-w-0"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__unset__">{t_i18n("No default")}</SelectItem>
                {DECK_TRANSITIONS.map((transition) => (
                  <SelectItem key={transition} value={transition}>{transition}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <Label htmlFor="template-default-aspect-ratio">{t_i18n("Aspect ratio")}</Label>
            <Select
              value={deckDefaults.aspect_ratio ?? "__unset__"}
              disabled={disabled}
              onValueChange={(v) => setDeckDefault("aspect_ratio", v === "__unset__" ? null : v)}
            >
              <SelectTrigger id="template-default-aspect-ratio" className="w-full min-w-0"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__unset__">{t_i18n("No default")}</SelectItem>
                {DECK_ASPECT_RATIOS.map((ratio) => (
                  <SelectItem key={ratio} value={ratio}>{ratio}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
      </div>

      {/* The brand cover emblem, on a template with the brand cover */}
      <CoverEmblemEditor
        extraMetadata={extraMetadata}
        onExtraMetadataChange={onExtraMetadataChange}
        disabled={disabled}
      />

      {/* Header & footer: the pattern every deck wearing the template fills */}
      <ChromeEditor
        extraMetadata={extraMetadata}
        onExtraMetadataChange={onExtraMetadataChange}
        disabled={disabled}
      />
    </div>
  );
}
