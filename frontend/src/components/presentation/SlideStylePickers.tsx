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
 * The slide-level style pickers of the inspector:
 *
 *   - `SlideThemePicker`: Inherit / Dark / Light - the slide's own colour
 *     scheme. Inherit CLEARS the field (the slide follows the deck again);
 *     a scheme the deck's template cannot dress is greyed out, saying why,
 *     exactly like the deck's `ThemeSchemePicker`.
 *   - `HeroPicker`: the backdrops of a title / closing slide the deck's
 *     template offers, each as a small drawn thumbnail (no raster) - plain,
 *     gradient, split, spotlight, and the template's own tiled covers (mosaic,
 *     cascade, horizon, pillar) when it declares them.
 *   - `HeroTonePicker`: the colour that backdrop is drawn in, one chip per
 *     palette choice, each chip painted in the colour the SLIDE will draw
 *     (the chips sit in a `.deck-palette` carrier of the slide's own tokens
 *     and read the stylesheet's `[data-hero-tone]` mapping, like the backdrop).
 *   - `OverlayPicker`: none / soft / strong, the scrim over a background image.
 *   - `QuoteVariantPicker`: the four looks of a SELECTED quote block -
 *     card, statement, pull, testimonial - each a small drawn thumbnail.
 *
 * Every control is a real radio group to the keyboard: one tab stop, arrows
 * move and select. Choosing the value a slide already renders with is a no-op
 * (`onChange` is not called), so a click on "plain" never dirties a deck
 * stored before the field existed. These live in the Selection tab only: a
 * filmstrip row stays one line (thumbnail, title, layout, arrangement).
 */
import { useId, useRef, type KeyboardEvent, type ReactNode } from "react";
import { Moon, Sun, Undo2 } from "lucide-react";

import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type {
  DeckHero,
  DeckHeroTone,
  DeckOverlay,
  DeckQuoteVariant,
  PresentationTheme,
  PresentationThemeConfig,
  PresentationThemeScheme,
} from "@/types";

import { QUOTE_VARIANTS } from "./block-schema";
import { HERO_TONES, HERO_VARIANTS, OVERLAY_VARIANTS } from "./hero";
import { useHeroLabel, useHeroToneLabel, useOverlayLabel } from "./hero-label";
import { THEME_SCHEMES } from "./previewSpec";
import { useSchemeLabel } from "./scheme-label";
import { themeStyle } from "./theme";

/** Where a key moves the selection within the enabled options, wrapping. */
function stepFor(key: string, index: number, count: number): number | null {
  switch (key) {
    case "ArrowRight":
    case "ArrowDown":
      return (index + 1) % count;
    case "ArrowLeft":
    case "ArrowUp":
      return (index - 1 + count) % count;
    case "Home":
      return 0;
    case "End":
      return count - 1;
    default:
      return null;
  }
}

interface RadioOption<T extends string> {
  value: T;
  label: string;
  /** Drawn in the button before (or instead of) the label. */
  visual?: ReactNode;
  disabled?: boolean;
  /** Why the option is off - its tooltip and its description for assistive tech. */
  reason?: string;
}

/**
 * The shared radio-group shell: the buttons, the one tab stop, the arrow
 * keys, the tooltip on a disabled option. Presentational choices are the
 * caller's (`className`, `optionClassName`).
 */
function RadioRow<T extends string>({
  label,
  value,
  options,
  onChange,
  className,
  optionClassName,
  testId,
}: {
  label: string;
  value: T;
  options: RadioOption<T>[];
  onChange: (next: T) => void;
  className?: string;
  optionClassName?: (option: RadioOption<T>, active: boolean) => string;
  testId?: string;
}) {
  const noteId = useId();
  const buttons = useRef(new Map<T, HTMLButtonElement>());
  const enabled = options.filter((option) => !option.disabled);
  const tabStop = enabled.find((option) => option.value === value) ?? enabled[0];

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const index = enabled.findIndex((option) => buttons.current.get(option.value) === event.target);
    if (index < 0) return;
    const step = stepFor(event.key, index, enabled.length);
    if (step === null) return;
    event.preventDefault();
    const next = enabled[step];
    buttons.current.get(next.value)?.focus();
    if (next.value !== value) onChange(next.value);
  };

  return (
    <div role="radiogroup" aria-label={label} className={className} onKeyDown={onKeyDown} data-testid={testId}>
      {options.map((option) => {
        const active = option.value === value;
        const reasonId = `${noteId}-${option.value}`;
        const button = (
          <button
            ref={(element) => {
              if (element) buttons.current.set(option.value, element);
              else buttons.current.delete(option.value);
            }}
            type="button"
            role="radio"
            aria-checked={active}
            aria-describedby={option.disabled ? reasonId : undefined}
            disabled={option.disabled}
            tabIndex={!option.disabled && option.value === tabStop?.value ? 0 : -1}
            onClick={() => {
              if (!active) onChange(option.value);
            }}
            className={cn(
              "flex w-full items-center justify-center gap-1.5 rounded-sm content-compact transition-colors",
              active ? "bg-accent/15 text-accent ring-1 ring-accent/40" : "text-default-secondary hover:bg-elevation-highlight hover:text-default-primary",
              option.disabled && "cursor-not-allowed opacity-40 hover:bg-transparent hover:text-default-secondary",
              optionClassName?.(option, active),
            )}
            data-value={option.value}
          >
            {option.visual}
            {option.label}
          </button>
        );
        return option.disabled && option.reason ? (
          <Tip key={option.value} wrap wrapTabIndex={-1} wrapClassName="flex w-full" content={option.reason}>
            {button}
            <span id={reasonId} className="sr-only">
              {option.reason}
            </span>
          </Tip>
        ) : (
          <div key={option.value} className="flex min-w-0">
            {button}
          </div>
        );
      })}
    </div>
  );
}

/** The value a slide's theme field takes: a scheme, or `null` for "inherit". */
type SlideThemeChoice = PresentationTheme | null;

export function SlideThemePicker({
  value,
  deckTheme,
  supported,
  templateName,
  onChange,
}: {
  /** The slide's own theme, `null` when it inherits. */
  value: SlideThemeChoice;
  /** What "Inherit" resolves to today, for the option's caption. */
  deckTheme: PresentationTheme;
  supported: readonly PresentationThemeScheme[];
  templateName?: string | null;
  onChange: (next: SlideThemeChoice) => void;
}) {
  const { t_i18n } = useFormatter();
  const schemeLabel = useSchemeLabel();
  const reason = t_i18n("Not supported by {template}", { template: templateName ?? t_i18n("this template") });
  const options: RadioOption<"inherit" | PresentationThemeScheme>[] = [
    {
      value: "inherit",
      label: t_i18n("Inherit"),
      visual: <Undo2 className="h-3.5 w-3.5" aria-hidden="true" />,
    },
    ...THEME_SCHEMES.map((scheme) => ({
      value: scheme,
      label: schemeLabel(scheme),
      visual: scheme === "dark" ? <Moon className="h-3.5 w-3.5" aria-hidden="true" /> : <Sun className="h-3.5 w-3.5" aria-hidden="true" />,
      disabled: !supported.includes(scheme),
      reason,
    })),
  ];
  return (
    <div className="space-y-1">
      <RadioRow
        label={t_i18n("Slide theme")}
        value={value ?? "inherit"}
        options={options}
        onChange={(next) => onChange(next === "inherit" ? null : next)}
        className="grid grid-cols-3 gap-1 rounded-sm border border-elevation-subtle bg-elevation-default p-1"
        optionClassName={() => "h-8"}
        testId="presentation.slideThemePicker"
      />
      <p className="content-compact leading-relaxed text-default-secondary">
        {value
          ? t_i18n("This slide keeps {theme} whatever the deck theme is.", { theme: schemeLabel(value) })
          : t_i18n("Follows the deck theme ({theme}).", { theme: schemeLabel(deckTheme) })}
      </p>
    </div>
  );
}

/** A drawn miniature of each hero variant - deck tokens only, no raster. */
function HeroThumbnail({ hero }: { hero: DeckHero }) {
  return (
    <span
      className={cn(
        "relative block aspect-video w-full overflow-hidden rounded-sm border border-elevation-subtle-soft bg-elevation-default",
        hero === "gradient" && "bg-gradient-to-br from-accent/50 via-[var(--bg-elevation-default)] to-accent/25",
      )}
      aria-hidden="true"
    >
      {hero === "gradient" && (
        <span className="absolute -bottom-2 -right-1 h-8 w-8 rounded-full border-[3px] border-elevation-subtle-soft" />
      )}
      {hero === "split" && <span className="absolute inset-y-0 right-0 w-[45%] bg-gradient-to-br from-accent/60 to-accent/20" />}
      {hero === "spotlight" && (
        <>
          <span className="absolute left-1 top-1 h-6 w-9 rounded-full bg-accent/40 blur-[6px]" />
          <span className="absolute -bottom-3 -right-3 h-8 w-8 rounded-full border border-accent/50" />
          <span className="absolute -bottom-5 -right-5 h-12 w-12 rounded-full border border-accent/35" />
        </>
      )}
      {hero === "mosaic" && (
        <>
          <span className="absolute right-0 top-0 h-2 w-2 bg-accent/80" />
          <span className="absolute right-2 top-0 h-2 w-2 bg-elevation-hover" />
          <span className="absolute right-0 top-2 h-2 w-2 bg-elevation-hover" />
          <span className="absolute right-4 top-0 h-1 w-1 bg-accent/50" />
          <span className="absolute right-2 top-2 h-1 w-1 bg-accent/40" />
          <span className="absolute bottom-0 right-0 h-2 w-2 bg-accent/60" />
          <span className="absolute bottom-0 right-2 h-1 w-1 bg-elevation-hover" />
        </>
      )}
      {hero === "cascade" && (
        <>
          <span className="absolute left-1.5 top-1 h-1 w-3 rounded-full bg-elevation-hover" />
          <span className="absolute right-0 top-0 h-1.5 w-1.5 bg-accent/80" />
          <span className="absolute right-1.5 top-0 h-1.5 w-1.5 bg-elevation-hover" />
          <span className="absolute right-3 top-0 h-1.5 w-1.5 bg-accent/50" />
          <span className="absolute right-[18px] top-0 h-1.5 w-1.5 bg-elevation-hover" />
          <span className="absolute right-0 top-1.5 h-1.5 w-1.5 bg-elevation-hover" />
          <span className="absolute right-1.5 top-1.5 h-1.5 w-1.5 bg-accent/60" />
          <span className="absolute right-0 top-3 h-1.5 w-1.5 bg-accent/40" />
          <span className="absolute bottom-2 left-1.5 h-px w-[80%] bg-accent/60" />
        </>
      )}
      {hero === "horizon" && (
        <>
          <span className="absolute left-1.5 top-1 h-1 w-3 rounded-full bg-elevation-hover" />
          <span className="absolute bottom-0 left-0 h-1.5 w-1.5 bg-accent/60" />
          <span className="absolute bottom-0 left-1.5 h-3 w-1.5 bg-elevation-hover" />
          <span className="absolute bottom-0 left-3 h-2 w-1.5 bg-accent/80" />
          <span className="absolute bottom-0 left-[18px] h-1.5 w-1.5 bg-elevation-hover" />
          <span className="absolute bottom-0 left-6 h-2.5 w-1.5 bg-accent/50" />
          <span className="absolute bottom-0 left-[30px] h-1.5 w-1.5 bg-elevation-hover" />
          <span className="absolute bottom-0 right-1.5 h-2 w-1.5 bg-accent/70" />
          <span className="absolute bottom-0 right-0 h-3 w-1.5 bg-elevation-hover" />
        </>
      )}
      {hero === "pillar" && (
        <>
          <span className="absolute right-0 top-0 h-1.5 w-3 bg-accent/70" />
          <span className="absolute right-0 top-1.5 h-1.5 w-1.5 bg-elevation-hover" />
          <span className="absolute right-0 top-3 h-1.5 w-4 bg-accent/50" />
          <span className="absolute right-0 top-[18px] h-1.5 w-2 bg-elevation-hover" />
          <span className="absolute right-0 top-6 h-1.5 w-3 bg-accent/80" />
          <span className="absolute right-0 bottom-0 h-1.5 w-1.5 bg-elevation-hover" />
        </>
      )}
      <span
        className={cn(
          "absolute left-1.5 h-1 rounded-full bg-elevation-hover",
          hero === "cascade" ? "top-[52%]" : "top-2.5",
          hero === "split" || hero === "pillar" ? "w-[40%]" : "w-[55%]",
        )}
      />
      <span
        className={cn(
          "absolute left-1.5 h-0.5 rounded-full bg-elevation-hover",
          hero === "cascade" ? "top-[66%]" : "top-4",
          hero === "split" || hero === "pillar" ? "w-[30%]" : "w-[40%]",
        )}
      />
    </span>
  );
}

export function HeroPicker({
  value,
  offered,
  onChange,
}: {
  value: DeckHero;
  /** The covers the deck's template offers; the others are not listed. */
  offered: readonly DeckHero[];
  onChange: (next: DeckHero) => void;
}) {
  const { t_i18n } = useFormatter();
  const heroLabel = useHeroLabel();
  return (
    <RadioRow
      label={t_i18n("Hero")}
      value={value}
      options={HERO_VARIANTS.filter((hero) => offered.includes(hero)).map((hero) => ({
        value: hero,
        label: heroLabel(hero),
        visual: <HeroThumbnail hero={hero} />,
      }))}
      onChange={onChange}
      className="grid grid-cols-4 gap-1"
      optionClassName={() => "h-auto flex-col items-stretch gap-1 p-1 content-compact"}
      testId="presentation.heroPicker"
    />
  );
}

/**
 * The slide's palette as a token carrier for controls OUTSIDE the stage: the
 * scheme's defaults from the stylesheet (`.deck-palette[data-deck-theme]`),
 * the template's own values inline - exactly what the slide root carries.
 */
export interface SlidePalette {
  scheme: PresentationThemeScheme;
  tokens: PresentationThemeConfig;
}

/** One chip: the from -> to pair of a hero tone, painted by the stylesheet. */
function HeroToneSwatch({ tone, className }: { tone: DeckHeroTone; className?: string }) {
  return (
    <span
      className={cn("deck-hero-tone-swatch shrink-0 rounded-full border border-elevation-subtle-soft", className)}
      data-hero-tone={tone}
      aria-hidden="true"
    />
  );
}

export function HeroTonePicker({
  value,
  palette,
  onChange,
}: {
  value: DeckHeroTone;
  palette: SlidePalette;
  onChange: (next: DeckHeroTone) => void;
}) {
  const { t_i18n } = useFormatter();
  const toneLabel = useHeroToneLabel();
  return (
    <div className="deck-palette" data-deck-theme={palette.scheme} style={themeStyle(palette.tokens)}>
      <RadioRow
        label={t_i18n("Hero colour")}
        value={value}
        options={HERO_TONES.map((tone) => ({
          value: tone,
          label: toneLabel(tone),
          visual: <HeroToneSwatch tone={tone} className="h-3.5 w-3.5 shrink-0" />,
        }))}
        onChange={onChange}
        className="grid grid-cols-2 gap-1 rounded-sm border border-elevation-subtle bg-elevation-default p-1"
        optionClassName={() => "h-auto min-h-8 justify-start px-1.5 py-1 text-left content-compact"}
        testId="presentation.heroTonePicker"
      />
    </div>
  );
}

/** A drawn miniature of each quote look - app tokens only, no raster. */
function QuoteThumbnail({ variant }: { variant: DeckQuoteVariant }) {
  return (
    <span
      className="relative block aspect-video w-full overflow-hidden rounded-sm border border-elevation-subtle-soft bg-elevation-default"
      aria-hidden="true"
    >
      {variant === "card" && (
        <span className="absolute inset-x-1.5 inset-y-2 rounded-r-sm border-l-2 border-accent bg-elevation-hover">
          <span className="absolute left-1 top-1.5 h-0.5 w-[70%] rounded-full bg-elevation-hover" />
          <span className="absolute left-1 top-3 h-0.5 w-[40%] rounded-full bg-elevation-hover" />
        </span>
      )}
      {variant === "statement" && (
        <>
          <span className="absolute left-1/2 top-0 -translate-x-1/2 text-[13px] font-bold leading-none text-accent">&ldquo;</span>
          <span className="absolute left-1/2 top-[42%] h-1 w-[75%] -translate-x-1/2 rounded-full bg-elevation-hover" />
          <span className="absolute left-1/2 top-[60%] h-1 w-[55%] -translate-x-1/2 rounded-full bg-elevation-hover" />
          <span className="absolute left-1/2 top-[80%] h-0.5 w-[35%] -translate-x-1/2 rounded-full bg-elevation-hover" />
        </>
      )}
      {variant === "pull" && (
        <>
          <span className="absolute bottom-2 left-1.5 top-2 w-1 rounded-full bg-accent" />
          <span className="absolute left-4 top-[35%] h-1 w-[60%] rounded-full bg-elevation-hover" />
          <span className="absolute left-4 top-[55%] h-1 w-[45%] rounded-full bg-elevation-hover" />
        </>
      )}
      {variant === "testimonial" && (
        <span className="absolute inset-1.5 rounded-sm border border-elevation-subtle-soft bg-elevation-hover">
          <span className="absolute left-1 top-1.5 h-0.5 w-[70%] rounded-full bg-elevation-hover" />
          <span className="absolute left-1 top-3 h-0.5 w-[55%] rounded-full bg-elevation-hover" />
          <span className="absolute bottom-1 left-1 h-1.5 w-1.5 rounded-full bg-accent/70" />
          <span className="absolute bottom-1.5 left-3.5 h-0.5 w-[35%] rounded-full bg-elevation-hover" />
        </span>
      )}
    </span>
  );
}

export function QuoteVariantPicker({
  value,
  onChange,
}: {
  value: DeckQuoteVariant;
  onChange: (next: DeckQuoteVariant) => void;
}) {
  const { t_i18n } = useFormatter();
  const labels: Record<DeckQuoteVariant, string> = {
    card: t_i18n("Card"),
    statement: t_i18n("Statement"),
    pull: t_i18n("Pull quote"),
    testimonial: t_i18n("Testimonial"),
  };
  return (
    <RadioRow
      label={t_i18n("Quote style")}
      value={value}
      options={QUOTE_VARIANTS.map((variant) => ({
        value: variant,
        label: labels[variant],
        visual: <QuoteThumbnail variant={variant} />,
      }))}
      onChange={onChange}
      className="grid grid-cols-4 gap-1"
      optionClassName={() => "h-auto flex-col items-stretch gap-1 p-1 content-compact"}
      testId="presentation.quoteVariantPicker"
    />
  );
}

export function OverlayPicker({ value, onChange }: { value: DeckOverlay; onChange: (next: DeckOverlay) => void }) {
  const { t_i18n } = useFormatter();
  const overlayLabel = useOverlayLabel();
  return (
    <RadioRow
      label={t_i18n("Overlay")}
      value={value}
      options={OVERLAY_VARIANTS.map((overlay) => ({ value: overlay, label: overlayLabel(overlay) }))}
      onChange={onChange}
      className="grid grid-cols-3 gap-1 rounded-sm border border-elevation-subtle bg-elevation-default p-1"
      optionClassName={() => "h-8"}
      testId="presentation.overlayPicker"
    />
  );
}
