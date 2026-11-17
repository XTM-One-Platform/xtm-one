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
 * The ink of the agent graph's edges: a handover edge
 * at rest must keep 3:1 against the canvas it is drawn on (WCAG 1.4.11, non
 * text contrast). The light canvas takes a darker shade of each handover
 * colour and a higher rest opacity; the dark canvas keeps its colours at a
 * rest opacity that keeps them at 3:1 too. Pure
 * functions, so the ratio is tested without a browser.
 */

/**
 * Opacity of an edge at rest (not live, not dimmed), per canvas. Dark 0.66:
 * the token-drawn consult (#17ab1f) edge needs 0.64 to reach 3:1; 0.66 keeps
 * a margin (3.21:1).
 */
export const EDGE_REST_OPACITY = { light: 0.8, dark: 0.66 } as const;

/**
 * The dark values the theme tokens some edges are drawn in resolve to, as the
 * UX lab measured them painted, so the ratio of the
 * token-drawn edges is tested too. A token change must update this map.
 */
export const DARK_TOKEN_INK: Record<string, string> = {
  "var(--color-feedback-success-primary)": "#17ab1f",
};

/**
 * The light canvas under the map: the map card paints `bg-elevation-default`,
 * #FFFFFF in light (the test had used #FAFAFB).
 */
export const LIGHT_CANVAS = "#FFFFFF";

/** The dark canvas under the map: `bg-elevation-default` in dark, as the UX lab measured it. */
export const DARK_CANVAS = "#0D162A";

/** The dark canvas as painted in the UX lab, one step lighter. */
export const DARK_CANVAS_PAINTED = "#0D172B";

/** Smallest contrast an edge keeps against its canvas (WCAG 1.4.11). */
export const EDGE_MIN_CONTRAST = 3;

type Rgb = [number, number, number];

function parseHex(hex: string): Rgb {
  const h = hex.replace("#", "");
  const full = h.length === 3 ? [...h].map((c) => c + c).join("") : h;
  return [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16)) as Rgb;
}

function channel(c: number): number {
  const v = c / 255;
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}

/** WCAG relative luminance of an sRGB colour. */
function relativeLuminance(rgb: Rgb): number {
  const [r, g, b] = rgb.map(channel);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** Contrast of `fg` drawn at `opacity` over `canvas` (hex colours), against that canvas. */
export function contrastOnCanvas(fg: string, opacity: number, canvas: string): number {
  const f = parseHex(fg);
  const c = parseHex(canvas);
  const mixed = f.map((v, i) => opacity * v + (1 - opacity) * c[i]) as Rgb;
  const a = relativeLuminance(mixed);
  const b = relativeLuminance(c);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

/** The colour an edge of this handover type is drawn in on the light or the dark canvas. */
export function edgeInk(config: { edgeColor: string; lightEdgeColor: string }, isLight: boolean): string {
  return isLight ? config.lightEdgeColor : config.edgeColor;
}
