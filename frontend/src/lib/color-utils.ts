// SPDX-License-Identifier: Apache-2.0

interface RGB {
  r: number;
  g: number;
  b: number;
}

function expandHex(hex: string): string | null {
  const clean = hex.replace(/^#/, "");
  if (clean.length === 3) {
    return clean.split("").map((c) => c + c).join("");
  }
  if (clean.length === 6) return clean;
  // Accept #rrggbbaa for parity with backend validation; alpha is ignored
  // because CSS HSL custom properties are currently opaque tokens.
  if (clean.length === 8) return clean.slice(0, 6);
  return null;
}

function hexToRgb(hex: string): RGB | null {
  const full = expandHex(hex);
  if (!full || !/^[0-9a-f]{6}$/i.test(full)) return null;
  return {
    r: parseInt(full.slice(0, 2), 16),
    g: parseInt(full.slice(2, 4), 16),
    b: parseInt(full.slice(4, 6), 16),
  };
}

function relativeLuminance({ r, g, b }: RGB): number {
  const channel = (value: number) => {
    const normalized = value / 255;
    return normalized <= 0.03928
      ? normalized / 12.92
      : ((normalized + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

function readableTextTokens(hex: string): Record<string, string> | null {
  const rgb = hexToRgb(hex);
  if (!rgb) return null;
  const isLight = relativeLuminance(rgb) > 0.45;
  const primary = isLight ? "hsl(220 5% 10%)" : "hsl(0 0% 100%)";
  const secondary = isLight ? "hsl(222 10% 45%)" : "hsl(230 4% 70%)";
  return {
    "--text-default-primary": primary,
    "--text-default-secondary": secondary,
    // Third step of the design system's text ladder. Kept close to secondary
    // rather than derived separately: the operator chose a background, not a
    // typographic hierarchy.
    "--text-default-disabled": secondary,
  };
}

function contrastForeground(hex: string): string | null {
  const rgb = hexToRgb(hex);
  if (!rgb) return null;
  return relativeLuminance(rgb) > 0.45 ? "hsl(220 56% 6%)" : "hsl(0 0% 100%)";
}

// The two text colours a filled accent can carry: the dark background token
// (#070d19) and white. Relative luminance of each, per WCAG 2.x.
const DARK_TEXT = { hsl: "220 56% 6%", luminance: relativeLuminance({ r: 7, g: 13, b: 25 }) };
const LIGHT_TEXT = { hsl: "0 0% 100%", luminance: 1 };
// Pure black: on a mid-grey fill neither token colour reaches 4.5:1 (#777777
// gives 4.34:1 and 4.48:1), while the better of black and white always does -
// so a fill that fails with both tokens takes black.
const BLACK_TEXT = { hsl: "0 0% 0%", luminance: 0 };
const TEXT_AA = 4.5;

function contrastRatio(a: number, b: number): number {
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

function bestTextOnLuminance(fill: number): string {
  const dark = contrastRatio(fill, DARK_TEXT.luminance);
  const light = contrastRatio(fill, LIGHT_TEXT.luminance);
  if (Math.max(dark, light) < TEXT_AA) return BLACK_TEXT.hsl;
  return dark >= light ? DARK_TEXT.hsl : LIGHT_TEXT.hsl;
}

/**
 * The text colour with the HIGHER WCAG contrast on a fill of *hex*.
 * Unlike ``contrastForeground``'s fixed luminance threshold, this is the
 * maximum the two token colours can give, so a 9px label on a custom accent
 * (the Certified ribbon) is as readable as the accent allows - and pure black
 * when neither token reaches 4.5:1 on it.
 */
export function bestTextOn(hex: string): string | null {
  const rgb = hexToRgb(hex);
  if (!rgb) return null;
  return bestTextOnLuminance(relativeLuminance(rgb));
}

function hslToRgb(h: number, s: number, l: number): RGB {
  const sat = s / 100;
  const light = l / 100;
  const k = (n: number) => (n + h / 30) % 12;
  const a = sat * Math.min(light, 1 - light);
  const f = (n: number) => light - a * Math.max(-1, Math.min(k(n) - 3, 9 - k(n), 1));
  return { r: Math.round(f(0) * 255), g: Math.round(f(8) * 255), b: Math.round(f(4) * 255) };
}

/** Convert a hex color string (#rrggbb, #rgb, or #rrggbbaa) to HSL components, or null if invalid. */
export function hexToHsl(hex: string): { h: number; s: number; l: number } | null {
  const rgb = hexToRgb(hex);
  if (!rgb) return null;
  const r = rgb.r / 255;
  const g = rgb.g / 255;
  const b = rgb.b / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const l = (max + min) / 2;
  if (max === min) return { h: 0, s: 0, l: Math.round(l * 100) };
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  const rawH = max === r
    ? ((g - b) / d + (g < b ? 6 : 0)) / 6
    : max === g
    ? ((b - r) / d + 2) / 6
    : ((r - g) / d + 4) / 6;
  return { h: Math.round(rawH * 360), s: Math.round(s * 100), l: Math.round(l * 100) };
}

/**
 * Derive the design system's surface and text variables from one background hex.
 *
 * The design system paints depth with colour rather than shadow: four elevation
 * layers, each with a default, a hover and a highlight surface plus a hairline
 * border, selected by a `.layer-0…3` class on the container. An operator picks a
 * single colour, so the whole ladder is derived from it — the offsets below keep
 * the *relationships* the library's own tokens encode (each layer a little
 * lighter than the one under it in dark, oscillating in light) rather than
 * inventing a new scale.
 *
 * These are FDS's own variable names on purpose. The library's tokens are
 * generated from Figma and must not be hand-edited, but a host overriding them
 * at runtime is the documented seam for exactly this: the values move, the
 * design language's structure does not.
 */
export function deriveBackgroundPalette(hex: string): Record<string, string> | null {
  const base = hexToHsl(hex);
  const rgb = hexToRgb(hex);
  if (!base || !rgb) return null;
  const { h, s, l } = base;
  const isLight = relativeLuminance(rgb) > 0.45;
  const textTokens = readableTextTokens(hex) ?? {};
  const clamp = (v: number, lo = 0, hi = 100) => Math.max(lo, Math.min(hi, v));
  const hsl = (dh: number, ds: number, dl: number) =>
    `hsl(${clamp(h + dh, 0, 360)} ${clamp(s + ds)}% ${clamp(l + dl)}%)`;

  // Per-layer lightness offsets. Dark rises monotonically; light mirrors the
  // shape of the library's own light ladder, which lifts layer 1 to white and
  // then steps back down.
  const layers = isLight ? [0, 3, 1, -4] : [0, 2, 5, 9];

  // Text drawn ON the highlight fill (the Preview ribbon): the page
  // foreground is chosen for the BACKGROUND, and a derived highlight fill can
  // sit where that colour no longer reaches 4.5:1 - so it is recomputed from
  // the fill itself, the way the accent's own foreground already is.
  const onFill = (dh: number, ds: number, dl: number) =>
    `hsl(${bestTextOnLuminance(
      relativeLuminance(hslToRgb(clamp(h + dh, 0, 360), clamp(s + ds), clamp(l + dl))),
    )})`;

  const palette: Record<string, string> = { ...textTokens };
  layers.forEach((dl, layer) => {
    palette[`--bg-elevation-default-layer-${layer}`] = hsl(0, 0, dl);
    palette[`--bg-elevation-hover-layer-${layer}`] = hsl(0, -2, dl + (isLight ? -3 : 3));
    palette[`--bg-elevation-highlight-layer-${layer}`] = hsl(-2, -6, dl + (isLight ? -6 : 6));
    palette[`--bg-elevation-heading-layer-${layer}`] = hsl(0, 0, dl);
    palette[`--bg-elevation-disabled-layer-${layer}`] = hsl(0, -8, dl + (isLight ? -2 : 2));
    palette[`--border-elevation-subtle-layer-${layer}`] = hsl(1, -32, dl + (isLight ? -14 : 16));
    palette[`--border-elevation-default-layer-${layer}`] = hsl(1, -28, dl + (isLight ? -24 : 28));
  });
  // One value for every layer: the ribbon reads it wherever it sits, and the
  // highlight fills differ by a few points of lightness, not by a contrast band.
  palette["--color-on-highlight"] = onFill(-2, -6, isLight ? -6 : 6);

  return palette;
}

/**
 * The selector the administrator's ACCENT is declared on. The accent is the
 * brand and follows every surface, the forced-dark subtree of a presented deck
 * included (`[data-mode="dark"]`): that subtree carries the library's
 * `dark` class, whose token set would otherwise reinstate the library's own
 * accent over one inherited from `:root`.
 *
 * `:root:root` rather than `:root` is deliberate. The design system declares
 * its themes under `.light {}` and `.dark {}`, which have the same specificity
 * as a single `:root`, so the winner would be decided by stylesheet order
 * alone. React 19 does hoist this block after the bundled sheets today, but a
 * build change that reorders them would silently drop every branding colour.
 * Doubling the selector settles it on specificity instead — and the subtree
 * half is doubled for the same reason, against the `dark` class on the same
 * element.
 */
export const ACCENT_SELECTOR = ':root:root,[data-mode="dark"][data-mode="dark"]';

/**
 * Build the CSS blocks that carry the administrator's branding from optional
 * hex values: the accent on `ACCENT_SELECTOR`, the palette derived from the
 * background on `:root:root` alone — that palette is the app's MODE, and a
 * black stage wants the dark set its subtree carries, never surfaces derived
 * for the app's own background. Returns null when neither colour is
 * configured.
 *
 * What branding can and cannot reach, honestly. `--color-accent` is the product
 * token every accent utility in this app reads, and
 * `--color-filigran-brand-primary` is what the LIBRARY's own components paint
 * from — a primary Button reads the brand token directly and has no notion of a
 * host accent, so both have to move or a rebranded deployment still shows
 * Filigran blue on its main action.
 *
 * What it still does not reach: surfaces painted from the library's brand
 * GRADIENTS — the `ia` and `highlight` button variants, and anything using
 * `bg-gradient-*`. Those consume multi-stop Figma-generated tokens that cannot
 * be rebuilt from one hex without inventing a palette. Filed upstream rather
 * than worked around here.
 */
export function buildCssVarsBlock(
  accentColor: string | null | undefined,
  backgroundColor: string | null | undefined,
): string | null {
  const blocks: string[] = [];

  if (accentColor) {
    const hsl = hexToHsl(accentColor);
    if (hsl) {
      const value = `hsl(${hsl.h} ${hsl.s}% ${hsl.l}%)`;
      // The accent seam is a single product variable, so branding does not have
      // to know which utilities read it...
      const rules = [`--color-accent:${value}`];
      // ...but the LIBRARY's own components paint from its brand family and
      // know nothing about a host accent, so an operator's colour has to reach
      // that too or every primary button stays Filigran blue on a rebranded
      // deployment. Only the base step: `secondary` and `tertiary` are lighter
      // and darker relatives that cannot be derived from one hex without
      // inventing a palette, so they keep the library's values and the hover
      // state stays the design system's.
      rules.push(`--color-filigran-brand-primary:${value}`);
      const accentForeground = contrastForeground(accentColor);
      if (accentForeground) rules.push(`--color-accent-foreground:${accentForeground}`);
      blocks.push(`${ACCENT_SELECTOR}{${rules.join(";")}}`);
    }
  }

  if (backgroundColor) {
    const palette = deriveBackgroundPalette(backgroundColor);
    if (palette) {
      const rules = Object.entries(palette).map(([k, v]) => `${k}:${v}`);
      blocks.push(`:root:root{${rules.join(";")}}`);
    }
  }

  return blocks.length ? blocks.join("") : null;
}
