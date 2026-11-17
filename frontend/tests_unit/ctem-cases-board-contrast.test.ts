// @vitest-environment node
import { readFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { BOARD_COLUMN_SURFACE, OVERDUE_TEXT } from "../src/pages/xtm-one/ctem/cases-board";

/**
 * The Cases board column (dev-docs/ctem-cases.md 7.0, S5c of #11545, UX round 1 B1 and
 * B2 on #11666): every age band holds 3:1 on the column surface, and the overdue and
 * "expire in 14 days" lines hold 4.5:1 (10-12 px text), in both themes. Computed from
 * the design system's own tokens and the surface the board actually paints.
 */

const FRONTEND = path.resolve(__dirname, "..");
const THEME = path.join(FRONTEND, "node_modules/@filigran/design-system/dist/tokens/theme.css");
const BOARD = path.join(FRONTEND, "src/pages/xtm-one/ctem/CasesBoard.tsx");

type Rgb = [number, number, number];
type Scope = Record<string, string>;

function block(css: string, selector: string): Scope {
  const start = css.indexOf(`\n${selector} {`);
  expect(start, `the token sheet no longer declares ${selector}`).toBeGreaterThan(-1);
  const end = css.indexOf("\n}", start);
  return Object.fromEntries([...css.slice(start, end).matchAll(/--([\w-]+):\s*([^;]+);/g)].map(([, n, v]) => [n, v.trim()]));
}

function resolve(value: string, scope: Scope, primitives: Scope): Rgb {
  const v = value.trim();
  const variable = v.match(/^var\(--([\w-]+)\)$/);
  if (variable) {
    const name = variable[1];
    if (primitives[name]) return resolve(primitives[name], scope, primitives);
    expect(scope[name], `--${name} is not declared`).toBeDefined();
    return resolve(scope[name], scope, primitives);
  }
  expect(v, `cannot resolve ${v}`).toMatch(/^#[0-9a-fA-F]{6}$/);
  const h = v.slice(1);
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)) as Rgb;
}

function luminance([r, g, b]: Rgb): number {
  const f = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}

function contrast(a: Rgb, b: Rgb): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** The `var(--token)` of the dark (unprefixed) or light (`light:`) bg utility of a class list. */
function bgToken(classes: string, theme: "dark" | "light"): string {
  const prefix = theme === "light" ? "light:" : "(?:^|\\s)";
  const match = classes.match(new RegExp(`${prefix}bg-\\[(var\\(--[\\w-]+\\))\\]`));
  expect(match, `no ${theme} background in "${classes}"`).not.toBeNull();
  return (match as RegExpMatchArray)[1];
}

async function setup() {
  const css = await readFile(THEME, "utf8");
  const primitives: Scope = Object.fromEntries(
    [...css.matchAll(/--([a-z]+-\d+):\s*(#[0-9a-fA-F]{6})\b/g)].map(([, n, v]) => [n, v]),
  );
  const root = block(css, ":root");
  const themes = { dark: { ...root, ...block(css, ".dark") }, light: { ...root, ...block(css, ".light") } };
  const source = await readFile(BOARD, "utf8");
  return { primitives, themes, source };
}

describe("Cases board column contrast (S5c of #11545)", () => {
  for (const theme of ["dark", "light"] as const) {
    it(`holds every age band at 3:1 and the column lines at 4.5:1 on the ${theme} column`, async () => {
      const { primitives, themes, source } = await setup();
      const scope = themes[theme];
      const surface = resolve(bgToken(BOARD_COLUMN_SURFACE, theme), scope, primitives);

      const bands = [...source.matchAll(/className: "(bg-\[var\(--[\w-]+\)\] light:bg-\[var\(--[\w-]+\)\])"/g)].map((m) => m[1]);
      expect(bands).toHaveLength(4);
      for (const band of bands) {
        const ratio = contrast(resolve(bgToken(band, theme), scope, primitives), surface);
        expect(ratio, `${band} on the ${theme} column`).toBeGreaterThanOrEqual(3);
      }

      const lineTokens = [OVERDUE_TEXT, "text-feedback-warning-primary"].map((cls) => cls.replace(/^text-/, "color-"));
      for (const token of lineTokens) {
        const ratio = contrast(resolve(`var(--${token})`, scope, primitives), surface);
        expect(ratio, `--${token} on the ${theme} column`).toBeGreaterThanOrEqual(4.5);
      }
    });
  }
});
