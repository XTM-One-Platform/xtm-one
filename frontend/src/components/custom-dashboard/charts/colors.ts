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
 * The colours a dashboard visualization draws with.
 *
 * Every colour this module produces is a design-system token: the categorical
 * palette is the platform's entity colours (themed for light and dark, distinct
 * hues, ordered so neighbours never share a hue family), the named colours are
 * its feedback and brand tokens. A definition may still name a colour as a hex
 * value: it is data, taken only in its strict `#rgb` /
 * `#rrggbb` form, never a CSS expression. A gradient between two colours is a
 * `color-mix`, so it follows the theme too.
 *
 * Pure, with no import: runs on `node --test`.
 */

/** The categorical palette, in drawing order. The entity colour closest to the accent (the
 *  victimology violet) is left out, and colours of one family (orange and red-orange, green
 *  and lime, sky and teal, pink and magenta) sit at least three places apart. */
export const CHART_PALETTE = [
  "var(--color-accent)",
  "var(--color-entities-all-threats)",
  "var(--color-entities-analyses)",
  "var(--color-feedback-info-primary)",
  "var(--color-entities-events)",
  "var(--color-entities-arsenal)",
  "var(--color-entities-location)",
  "var(--color-entities-observations)",
  "var(--color-entities-cases)",
  "var(--color-entities-techniques)",
] as const;

/** The colour of the `index`-th series or slice. */
export const paletteColor = (index: number): string =>
  CHART_PALETTE[((index % CHART_PALETTE.length) + CHART_PALETTE.length) % CHART_PALETTE.length];

/** The colours a definition may name, by meaning (the widgets' tones and the palette). */
const NAMED_COLORS: Readonly<Record<string, string>> = {
  accent: "var(--color-accent)",
  // The design system's own brand primary (`index.css`, untouched by XTM One mode and branding): the accent
  // OpenCTI and OpenAEV draw their widgets in.
  brand: "var(--color-module-brand)",
  ai: "var(--color-filigran-ia-primary)",
  success: "var(--color-feedback-success-primary)",
  warning: "var(--color-feedback-warning-primary)",
  alert: "var(--color-feedback-alert-primary)",
  danger: "var(--color-feedback-error-primary)",
  info: "var(--color-feedback-info-primary)",
  neutral: "var(--color-feedback-neutral-primary)",
  text: "var(--text-default-primary)",
  muted: "var(--text-default-secondary)",
  ...Object.fromEntries(CHART_PALETTE.map((token, index) => [`chart${index + 1}`, token])),
};

const HEX_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;
// What `mixColor` writes from two resolved colours, and nothing else.
const RESOLVED = String.raw`(?:var\(--[a-z0-9-]+\)|#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6})`;
const MIX_RE = new RegExp(String.raw`^color-mix\(in srgb, ${RESOLVED} \d{1,2}%, ${RESOLVED}\)$`);

/** A colour named in a definition, as CSS: a named colour's token, a strict hex, a mix of
 *  two of them (`gradient`'s output); anything else `undefined`. */
export function resolveColor(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const name = value.trim();
  if (HEX_RE.test(name) || MIX_RE.test(name)) return name;
  return Object.prototype.hasOwnProperty.call(NAMED_COLORS, name) ? NAMED_COLORS[name] : undefined;
}

/** The accent a widget's content is drawn in (`accentColor`, a module's own), as the CSS variable its renderers
 *  read (`--color-accent`, set on the content alone: the frame keeps the platform's); nothing when unset. */
export function accentStyle(options: unknown): Record<string, string> | undefined {
  const accent = options && typeof options === "object" ? resolveColor((options as Record<string, unknown>).accentColor) : undefined;
  return accent ? { "--color-accent": accent } : undefined;
}

/** The colour `t` (0..1) of the way from `from` to `to`, two resolved colours. */
export function mixColor(from: string, to: string, t: number): string {
  const share = Math.round(Math.min(1, Math.max(0, Number.isFinite(t) ? t : 0)) * 100);
  if (share === 0) return from;
  if (share === 100) return to;
  return `color-mix(in srgb, ${to} ${share}%, ${from})`;
}
