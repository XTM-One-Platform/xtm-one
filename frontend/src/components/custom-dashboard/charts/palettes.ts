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
 * The named palettes and colour scales a presentation preset may ask for: the
 * colours the modules' own widgets draw with
 * (`dev-docs/dashboard-rendering-reference.md`), named for what they are, each
 * for the dark and the light theme.
 *
 * A palette's colours are data, as a definition's hex colours are: strict hex
 * or a design-system token, never a CSS expression.
 *
 * Pure, with no import: runs on `node --test`.
 */

export type ColorMode = "dark" | "light";

// The Material Design hues (MUI 6.5), tints 400, 500, 600 and 700, in Material's own order.
const HUES = {
  red: ["#ef5350", "#f44336", "#e53935", "#d32f2f"],
  pink: ["#ec407a", "#e91e63", "#d81b60", "#c2185b"],
  purple: ["#ab47bc", "#9c27b0", "#8e24aa", "#7b1fa2"],
  deepPurple: ["#7e57c2", "#673ab7", "#5e35b1", "#512da8"],
  indigo: ["#5c6bc0", "#3f51b5", "#3949ab", "#303f9f"],
  blue: ["#42a5f5", "#2196f3", "#1e88e5", "#1976d2"],
  lightBlue: ["#29b6f6", "#03a9f4", "#039be5", "#0288d1"],
  cyan: ["#26c6da", "#00bcd4", "#00acc1", "#0097a7"],
  teal: ["#26a69a", "#009688", "#00897b", "#00796b"],
  green: ["#66bb6a", "#4caf50", "#43a047", "#388e3c"],
  lightGreen: ["#9ccc65", "#8bc34a", "#7cb342", "#689f38"],
  lime: ["#d4e157", "#cddc39", "#c0ca33", "#afb42b"],
  yellow: ["#ffee58", "#ffeb3b", "#fdd835", "#fbc02d"],
  amber: ["#ffca28", "#ffc107", "#ffb300", "#ffa000"],
  orange: ["#ffa726", "#ff9800", "#fb8c00", "#f57c00"],
  deepOrange: ["#ff7043", "#ff5722", "#f4511e", "#e64a19"],
  brown: ["#8d6e63", "#795548", "#6d4c41", "#5d4037"],
  grey: ["#bdbdbd", "#9e9e9e", "#757575", "#616161"],
  blueGrey: ["#78909c", "#607d8b", "#546e7a", "#455a64"],
} as const;
type Hue = keyof typeof HUES;
const TINT = { 400: 0, 500: 1, 600: 2, 700: 3 } as const;

const HUE_ORDER = Object.keys(HUES) as Hue[];
// The order a palette of mixed hues takes, so that neighbours differ.
const MIXED_ORDER: Hue[] = [
  "red", "purple", "pink", "deepPurple", "indigo", "blue", "cyan", "blueGrey", "lightBlue", "green",
  "teal", "lightGreen", "amber", "deepOrange", "lime", "yellow", "brown", "orange", "grey",
]; // prettier-ignore

/** A hue's tint: 400 in the dark theme, 600 in the light one, or the tint above (`deeper`). */
const tint = (hue: Hue, mode: ColorMode, deeper = false): string =>
  HUES[hue][TINT[mode === "dark" ? (deeper ? 500 : 400) : deeper ? 700 : 600]];

/** The threat levels 0-9, from the faintest to the most severe. */
const THREAT_COLORS = [
  "#fff59d", "#ffe082", "#ffb300", "#ffb74d", "#fb8c00", "#d95f00", "#e64a19", "#f44336", "#d32f2f", "#b71c1c",
] as const; // prettier-ignore

const HEAT: Record<ColorMode, readonly string[]> = {
  dark: ["#001e3c", "#023362", "#02407a", "#045198", "#0561b4", "#0b75d9", "#2986e7", "#3a95f3", "#4da3ff", "#76bbff", "#9eccff"],
  light: ["#f3f6f9", "#76bbff", "#4da3ff", "#3a95f3", "#2986e7", "#0b75d9", "#0561b4", "#02407a", "#023362", "#001e3c", "#021428"],
};

/** The palettes a preset may name. */
export type PaletteName = "material" | "materialMixed" | "results" | "threat" | "heat";

/**
 * A named palette's colours for a theme:
 * - `material`: the 19 Material hues in Material's order, then the same a tint deeper (38);
 * - `materialMixed`: the 19 hues in an order where neighbours differ;
 * - `results`: three cool hues for results (deep purple, indigo, light blue);
 * - `threat`: the ten threat levels (the same in both themes);
 * - `heat`: eleven blues, the first for zero, darkest to lightest in the dark theme and the reverse in the light one.
 */
export function paletteColors(name: PaletteName, mode: ColorMode): string[] {
  switch (name) {
    case "material":
      return [...HUE_ORDER.map((hue) => tint(hue, mode)), ...HUE_ORDER.map((hue) => tint(hue, mode, true))];
    case "materialMixed":
      return MIXED_ORDER.map((hue) => tint(hue, mode));
    case "results":
      return (["deepPurple", "indigo", "lightBlue"] as const).map((hue) => tint(hue, mode));
    case "threat":
      return [...THREAT_COLORS];
    case "heat":
      return [...HEAT[mode]];
  }
}

/** A threat level's colour; a level 0 (or none) is drawn as level 5, as a threat map draws an unranked region. */
export function threatColor(level: number | null | undefined): string {
  const at = typeof level === "number" && Number.isFinite(level) ? Math.trunc(level) : 0;
  return THREAT_COLORS[at >= 1 && at <= 9 ? at : 5];
}

/** The 400 tint of each Material hue and its 600 twin: a module's colour named in its dark tint is
 *  drawn in the light one as the light palette draws it. */
const LIGHT_TWINS: Readonly<Record<string, string>> = {
  ...Object.fromEntries(Object.values(HUES).map((tints) => [tints[0], tints[2]])),
  // The disabled text and the brand's secondary turquoise.
  "#75829a": "#6e7788",
  "#00f0bc": "#009474",
};

/**
 * A data colour (an item's, a series') as the theme draws it: in the light theme a Material 400
 * tint takes its 600 twin and white turns black (white on a white surface would vanish); the dark
 * theme draws it as given. Anything that is not a strict hex is returned as it is.
 */
export function themedColor(color: string, mode: ColorMode): string {
  if (mode === "dark" || !color.startsWith("#")) return color;
  const hex = color.toLowerCase();
  if (hex === "#ffffff" || hex === "#fff") return "#000000";
  return LIGHT_TWINS[hex] ?? color;
}

/** A colour too close to white to draw on a light cell: a perceived brightness (0.299 R + 0.587 G
 *  + 0.114 B) of 0.9 or more, as a treemap of distinct colours leaves them out. Not a hex: no. */
export function isPale(color: string): boolean {
  const match = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(color);
  if (!match) return false;
  const hex = match[1].length === 3 ? [...match[1]].map((c) => c + c).join("") : match[1];
  const [r, g, b] = [0, 2, 4].map((at) => Number.parseInt(hex.slice(at, at + 2), 16));
  return (0.299 * r + 0.587 * g + 0.114 * b) / 255 >= 0.9;
}

// ── Heat ranges ─────────────────────────────────────────────────────────────

export interface HeatRange {
  from: number;
  to: number;
  color: string;
}

/**
 * The colour ranges of a heatmap between its lowest and highest values: zero
 * apart (the palette's first colour), then ten equal steps of a whole width
 * (at least 1), each the next colour. A step starting at 0 starts at 1.
 */
export function heatRanges(min: number, max: number, palette: readonly string[]): HeatRange[] {
  const step = Math.max(1, Math.trunc((max - min) / 9));
  const ranges: HeatRange[] = [];
  for (let i = 0; i <= 9; i += 1) {
    const from = min + i * step;
    ranges.push({ from: from === 0 ? 1 : from, to: min + (i + 1) * step, color: palette[i + 1] ?? palette[palette.length - 1] });
  }
  return [...ranges, { from: 0, to: 0, color: palette[0] }];
}

/** A value's colour among heat ranges: the last range holding it wins (a value on a boundary takes the higher step). */
export function heatColor(value: number, ranges: readonly HeatRange[]): string | undefined {
  let color: string | undefined;
  for (const range of ranges) if (value >= range.from && value <= range.to) color = range.color;
  return color;
}

// ── Rate scales ─────────────────────────────────────────────────────────────

/** The rate scales a preset may name. */
export type RateScaleName = "coverage" | "average" | "gauge";

interface RateScale {
  /** `[bound, colour]` from the lowest; a rate takes the first band it is under. */
  bands: readonly [number, string][];
  /** A rate equal to a bound is in the band below it (`closed`) or above it (`open`). */
  upper: "closed" | "open";
  /** A rate past every bound. */
  above: string;
  /** No rate (nothing resolved). */
  none: string;
}

const SUCCESS = "var(--color-feedback-success-primary)";
const ERROR = "var(--color-feedback-error-primary)";
const SCALES: Record<RateScaleName, Record<ColorMode, RateScale>> = {
  // A technique's success rate: red, deep orange, amber, green; slate when untested.
  coverage: {
    dark: { bands: [[0.25, "#f44336"], [0.5, "#ff7043"], [0.75, "#ffb300"]], upper: "closed", above: "#26a96c", none: "#5c6b7a" },
    light: { bands: [[0.25, "#f44336"], [0.5, "#ff7043"], [0.75, "#ffb300"]], upper: "closed", above: "#26a96c", none: "#5c6b7a" },
  },
  // An average of outcomes: failed, warning, intermediate, success; grey when there is none.
  average: {
    dark: { bands: [[0.25, "rgb(220, 81, 72)"], [0.5, "rgb(245, 166, 35)"], [0.75, "rgb(255, 216, 0)"]], upper: "closed", above: "rgb(2, 129, 8)", none: "rgba(128, 127, 127, 0.37)" },
    light: { bands: [[0.25, "rgb(220, 81, 72)"], [0.5, "rgb(245, 166, 35)"], [0.75, "rgb(255, 216, 0)"]], upper: "closed", above: "rgb(2, 129, 8)", none: "rgba(128, 127, 127, 0.37)" },
  },
  // A gauge's rate: error, deep orange, warning, success; muted when nothing is resolved.
  gauge: {
    dark: { bands: [[0.25, ERROR], [0.5, "#ff7043"], [0.75, "#ffa726"]], upper: "open", above: SUCCESS, none: "#75829a" },
    light: { bands: [[0.25, ERROR], [0.5, "#ff7043"], [0.75, "#ed6c02"]], upper: "open", above: SUCCESS, none: "#6e7788" },
  },
};

/** A rate's colour (0..1) on a named scale; a missing or negative rate is the scale's `none`. */
export function rateColor(rate: number | null | undefined, scale: RateScaleName, mode: ColorMode): string {
  const { bands, upper, above, none } = SCALES[scale][mode];
  if (rate === null || rate === undefined || !Number.isFinite(rate) || rate < 0) return none;
  const band = bands.find(([bound]) => (upper === "closed" ? rate <= bound : rate < bound));
  return band ? band[1] : above;
}
