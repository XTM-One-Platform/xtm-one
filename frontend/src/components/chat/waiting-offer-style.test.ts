/**
 * The waiting arcade's offers meet the contrast minimum - `yarn test:unit`.
 *
 * The colours are read from `waiting-offer-style.ts` and the tokens from
 * `index.css`, so a class or a token that drops a control under its WCAG
 * minimum fails here: 4.5:1 for the 10 px text (1.4.3), 3:1 for an icon-only
 * control (1.4.11), at rest and on hover / keyboard focus, in both modes of
 * both themes, over the page and over a card.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  OFFER_ACCENT_INTERACTIVE,
  OFFER_ACCENT_TEXT,
  OFFER_ICON_REST,
  OFFER_PLATE,
  OFFER_TEXT_REST,
} from "./waiting-offer-style.ts";

const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
const strip = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, "");
/* The token VALUES are the design system's, declared on the classes it themes
   through; `index.css` carries only the host's own overrides on top of them, so
   the cascade this measures is read from both, in that order. */
const LIB = strip(read("../../../node_modules/@filigran/design-system/dist/tokens/theme.css"));
const CSS = strip(read("../../index.css"));
const PANEL = read("./ChatWaitingGame.tsx");

type Tokens = Record<string, string>;
type RGB = [number, number, number];

/** Every declaration under `selector`, across both stylesheets, in cascade order. */
function declarations(selector: RegExp, required = true): Tokens {
  const tokens: Tokens = {};
  let found = false;
  for (const css of [LIB, CSS]) {
    for (const block of css.matchAll(new RegExp(`${selector.source}\\s*\\{([^}]*)\\}`, "g"))) {
      found = true;
      for (const [, name, value] of block[1].matchAll(/--([\w-]+)\s*:\s*([^;]+);/g)) tokens[name] = value.trim();
    }
  }
  assert.ok(found || !required, `no block for ${selector.source} in either stylesheet`);
  return tokens;
}

/* The primitives every theme reads, then each theme's own layer. The library
   declares its dark set on `.dark` and the app mirrors the mode onto that class
   (`useThemeEffect`), so `.dark` IS the default here. */
const ROOT = declarations(/:root/);
const DARK = { ...ROOT, ...declarations(/\.dark/) };
const LIGHT = { ...ROOT, ...declarations(/\.light/) };
const HOST = declarations(/@theme/, false);
const XTM = declarations(/\[data-theme="xtm-one"\]/, false);
const XTM_DARK = declarations(/\.dark\[data-theme="xtm-one"\]/, false);

/** The cascade of each theme and mode, in the order the browser resolves it. */
const THEMES: { name: string; light: boolean; tokens: Tokens }[] = [
  { name: "dark", light: false, tokens: { ...DARK, ...HOST } },
  { name: "light", light: true, tokens: { ...LIGHT, ...HOST } },
  { name: "XTM One dark", light: false, tokens: { ...DARK, ...HOST, ...XTM, ...XTM_DARK } },
  { name: "XTM One light", light: true, tokens: { ...LIGHT, ...HOST, ...XTM } },
];

function hsl(triple: string): RGB {
  const [h, s, l] = triple.split(/[\s,]+/).map((part) => parseFloat(part));
  const sat = s / 100;
  const lig = l / 100;
  const c = (1 - Math.abs(2 * lig - 1)) * sat;
  const hp = (h % 360) / 60;
  const x = c * (1 - Math.abs((hp % 2) - 1));
  const [r, g, b] =
    hp < 1 ? [c, x, 0] : hp < 2 ? [x, c, 0] : hp < 3 ? [0, c, x] : hp < 4 ? [0, x, c] : hp < 5 ? [x, 0, c] : [c, 0, x];
  const m = lig - c / 2;
  return [r + m, g + m, b + m].map((v) => v * 255) as RGB;
}

function hex(value: string): RGB | null {
  const m = value.match(/^#([\da-f]{3}|[\da-f]{6})$/i);
  if (!m) return null;
  const full = m[1].length === 3 ? [...m[1]].map((c) => c + c).join("") : m[1];
  return [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16)) as RGB;
}

/** A token's colour, following the `var()` chain the design system builds them from. */
function value(name: string, tokens: Tokens, depth = 0): RGB | null {
  const raw = tokens[name];
  if (!raw || depth > 8) return null;
  const chain = raw.match(/^var\(\s*--([\w-]+)\s*\)$/);
  if (chain) return value(chain[1], tokens, depth + 1);
  const inner = raw.match(/^hsl\(([^)]*)\)$/);
  if (inner) return hsl(inner[1]);
  return hex(raw) ?? (/^[\d.]+\s/.test(raw) ? hsl(raw) : null);
}

/** `a` at `alpha` over `b`, as the browser composites (sRGB); also `color-mix(in srgb)`. */
const blend = (a: RGB, alpha: number, b: RGB): RGB => a.map((v, i) => alpha * v + (1 - alpha) * b[i]) as RGB;

function luminance(rgb: RGB): number {
  const [r, g, b] = rgb.map((v) => {
    const n = v / 255;
    return n <= 0.04045 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: RGB, b: RGB): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const MIX = /^\[color:color-mix\(in_srgb,var\(--([\w-]+)\)_(\d+)%,var\(--([\w-]+)\)\)\]$/;

/** The colour a `text-*` / `bg-*` utility paints, or null when it is not a colour of the theme. */
function paint(utility: string, tokens: Tokens, under: RGB, property?: "text" | "bg"): RGB | null {
  const mix = utility.match(MIX);
  if (mix) {
    const [a, b] = [value(mix[1], tokens), value(mix[3], tokens)];
    return a && b ? blend(a, Number(mix[2]) / 100, b) : null;
  }
  const plain = utility.match(/^([\w-]+?)(?:\/(\d+)|\/\[(0?\.\d+)\])?$/);
  if (!plain) return null;
  // A utility names its token one of three ways: with the property's own family
  // (`text-default-secondary` -> `--text-default-secondary`), as a colour
  // (`bg-accent` -> `--color-accent`), or by the token's bare name.
  const painted =
    (property ? value(`${property}-${plain[1]}`, tokens) : null) ??
    value(`color-${plain[1]}`, tokens) ??
    value(plain[1], tokens);
  if (!painted) return null;
  const alpha = plain[2] ? Number(plain[2]) / 100 : plain[3] ? Number(plain[3]) : 1;
  return blend(painted, alpha, under);
}

type State = "rest" | "hover" | "focus";
const STATE_VARIANT: Record<State, string | null> = { rest: null, hover: "hover", focus: "focus-visible" };

/** The last colour `classes` give to `property` in a state, the way the variants' specificity resolves it. */
function resolve(classes: string, property: "text" | "bg", state: State, light: boolean, tokens: Tokens, under: RGB): RGB | null {
  let rank = -1;
  let color: RGB | null = null;
  for (const cls of classes.split(/\s+/)) {
    // Variants are the leading `name:` prefixes; an arbitrary value keeps its own colons.
    const [, prefix = "", utility = ""] = cls.match(/^((?:[a-z-]+:)*)(.*)$/) ?? [];
    if (!utility.startsWith(`${property}-`)) continue;
    const variants = prefix.split(":").filter(Boolean);
    if (variants.some((v) => v !== "light" && v !== STATE_VARIANT[state])) continue;
    if (variants.includes("light") && !light) continue;
    const painted = paint(utility.slice(property.length + 1), tokens, under, property);
    if (!painted) continue;
    const r = variants.includes(STATE_VARIANT[state] ?? "-") ? 2 : variants.includes("light") ? 1 : 0;
    if (r >= rank) [rank, color] = [r, painted];
  }
  return color;
}

const PANEL_BG = "bg-accent/[0.03]";
/** The waiting panel's controls and status line: the panel, then their plate over the canvas. */
const ON_PLATE = [PANEL_BG, OFFER_PLATE];

function check(label: string, classes: string, minimum: number, states: State[], layers: string[]) {
  for (const theme of THEMES) {
    for (const surface of ["bg-elevation-default", "bg-elevation-highlight"]) {
      const base = value(surface, theme.tokens);
      assert.ok(base, `no ${surface} in the ${theme.name} theme`);
      let panel = base;
      for (const layer of layers) panel = resolve(layer, "bg", "rest", theme.light, theme.tokens, panel) ?? panel;
      for (const state of states) {
        const behind = resolve(classes, "bg", state, theme.light, theme.tokens, panel) ?? panel;
        const ink = resolve(classes, "text", state, theme.light, theme.tokens, behind);
        assert.ok(ink, `${label}: no text colour ${state}`);
        const ratio = contrast(ink, behind);
        assert.ok(
          ratio >= minimum,
          `${label} ${state}, ${theme.name} over the ${surface}: ${ratio.toFixed(2)}:1, needs ${minimum}:1`,
        );
      }
    }
  }
}

test("the panel paints the background the ratios are measured against", () => {
  assert.match(PANEL, /bg-accent\/\[0\.03\]/);
});

test("the text of the offers meets 4.5:1 at rest, on hover and on focus", () => {
  check("Take control", `${OFFER_TEXT_REST} ${OFFER_ACCENT_INTERACTIVE}`, 4.5, ["rest", "hover", "focus"], ON_PLATE);
  check("status line", OFFER_TEXT_REST, 4.5, ["rest"], ON_PLATE);
  check("status line accent", OFFER_ACCENT_TEXT, 4.5, ["rest"], ON_PLATE);
});

test("an icon-only offer meets 3:1 at rest, on hover and on focus", () => {
  const icon = `${OFFER_ICON_REST} ${OFFER_ACCENT_INTERACTIVE}`;
  check("trophy", icon, 3, ["rest", "hover", "focus"], ON_PLATE);
});

test("the waiting panel's controls and status line sit on an opaque plate over the canvas", () => {
  assert.match(OFFER_PLATE, /(^|\s)bg-elevation-default(\s|$)/, "the plate must be opaque, or the canvas shows through");
  assert.match(PANEL, /absolute top-1 right-1 flex items-center gap-0\.5", OFFER_PLATE\)/);
  assert.match(PANEL, /font-mono-plex content-caption",\s*OFFER_PLATE,\s*OFFER_TEXT_REST,/);
});

test("the accent is what hover and focus show, and the rest is neutral", () => {
  for (const variant of ["hover", "focus-visible"]) {
    assert.match(OFFER_ACCENT_INTERACTIVE, new RegExp(`${variant}:text-\\[color:color-mix\\(in_srgb,var\\(--color-accent\\)`));
  }
  assert.match(OFFER_ACCENT_INTERACTIVE, /hover:bg-accent\/10/);
  for (const rest of [OFFER_TEXT_REST, OFFER_ICON_REST]) assert.doesNotMatch(rest, /accent/);
});

test("the waiting panel draws its offers with these colours", () => {
  const takeControl = PANEL.match(/aria-label=\{t_i18n\("Take control of the waiting game"\)\}[\s\S]*?<\/button>/)?.[0] ?? "";
  assert.match(takeControl, /OFFER_TEXT_REST,\s*OFFER_ACCENT_INTERACTIVE/);
  // The leaderboard trophy is a design-system IconButton drawn in the offer colours.
  assert.match(
    PANEL,
    /<IconButton[^>]*?aria-label=\{t_i18n\("Arcade leaderboard"\)\}\s*className=\{cn\(OFFER_ICON_REST, OFFER_ACCENT_INTERACTIVE\)\}\s*icon=\{<Trophy/,
  );
  assert.match(PANEL, /<span className=\{OFFER_ACCENT_TEXT\}>\{leader\.callsign\}/);
  // The faded accent is gone: no control rests on an opacity or on the raw accent.
  assert.doesNotMatch(PANEL, /\bopacity-(40|60|70)\b|\btext-cyan\b/, "ChatWaitingGame still fades the accent");
});
