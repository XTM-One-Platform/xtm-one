// @vitest-environment node
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { describe, it, expect } from "vitest";

/**
 * Warning text on a warning tint reads at AA in both themes.
 *
 * The chip pattern `bg-feedback-warning-primary/10 text-feedback-warning-primary`
 * (and its `bg-feedback-alert-primary/N` twin) puts the orange text on a wash of
 * itself: 3.3:1 to 3.8:1 in the light theme, 4.0:1 to 4.9:1 in the dark one,
 * under the 4.5:1 that caption-size text needs. The design system's
 * `--color-feedback-warning-tertiary` is the same family a step away from the
 * surface in each theme (orange-800 in light, orange-200 in dark), which is the
 * text a tinted warning chip takes. This holds that contrast, computed from the
 * library's own tokens on every elevation surface a chip sits on, and keeps the
 * primary text off a light warning tint everywhere in `src`.
 */

const FRONTEND = path.resolve(__dirname, "..");
const THEME = path.join(FRONTEND, "node_modules/@filigran/design-system/dist/tokens/theme.css");

type Rgb = [number, number, number];
type Scope = Record<string, string>;

/** A tint at or below this share is a wash, not a fill: the text keeps the surface's contrast band. */
const MAX_WASH = 30;
const TINT = /\bbg-feedback-(?:warning|alert)-primary\/(\d+|\[(0?\.\d+)\])/g;
const PRIMARY_TEXT = /(?<![\w-])(?:[\w-]+:)*text-feedback-warning-primary(?![\w-])/;

function declarations(css: string, selector: string): Scope {
  const start = css.indexOf(`\n${selector} {`);
  expect(start, `the token sheet no longer declares ${selector}`).toBeGreaterThan(-1);
  const end = css.indexOf("\n}", start);
  return Object.fromEntries(
    [...css.slice(start, end).matchAll(/--([\w-]+):\s*([^;]+);/g)].map(([, name, value]) => [name, value.trim()]),
  );
}

function hex(value: string): Rgb {
  const h = value.replace("#", "");
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)) as Rgb;
}

function resolve(value: string, scope: Scope, primitives: Scope): Rgb {
  const v = value.trim();
  const variable = v.match(/^var\(--([\w-]+)\)$/);
  if (variable) {
    const name = variable[1];
    if (primitives[name]) return hex(primitives[name]);
    expect(scope[name], `--${name} is not declared`).toBeDefined();
    return resolve(scope[name], scope, primitives);
  }
  if (v.startsWith("#")) return hex(v);
  throw new Error(`cannot resolve ${v}`);
}

function mix(colour: Rgb, over: Rgb, share: number): Rgb {
  return colour.map((c, i) => c * share + over[i] * (1 - share)) as Rgb;
}

function luminance([r, g, b]: Rgb): number {
  const linear = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
}

function contrast(a: Rgb, b: Rgb): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

async function themes() {
  const css = await readFile(THEME, "utf8");
  const primitives: Scope = Object.fromEntries(
    [...css.matchAll(/--([a-z]+-\d+):\s*(#[0-9a-fA-F]{6})/g)].map(([, name, value]) => [name, value]),
  );
  const root = declarations(css, ":root");
  return {
    primitives,
    schemes: { dark: { ...root, ...declarations(css, ".dark") }, light: { ...root, ...declarations(css, ".light") } },
  };
}

/** The surfaces a chip sits on: the page, a card and a dialog layer, and the hover step of each. */
const SURFACES = [0, 1, 2].flatMap((layer) => [`bg-elevation-default-layer-${layer}`, `bg-elevation-hover-layer-${layer}`]);

async function* sources(dir: string): AsyncGenerator<string> {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* sources(full);
    else if (/\.(tsx?)$/.test(entry.name) && !entry.name.includes(".test.")) yield full;
  }
}

/** A warning or alert class: the text, the fill or the wash of a notice, a chip or a badge. */
const NOTICE = /(?<![\w-])(?:[\w-]+:)*(?:text|bg)-feedback-(?:warning|alert)-[\w/.[\]-]+/;
/** The text on a solid feedback fill (the EE badge on a Create button). */
const FILL_TEXT = /(?<![\w-])text-on-feedback-fill(?![\w-])/;
const LITERAL_SIZE = /(?<![\w-])(?:[\w-]+:)*text-\[(\d*\.?\d+)(px|rem)\]/g;

function literalSizes(classes: string): string[] {
  return [...classes.matchAll(LITERAL_SIZE)].map(([size]) => size.slice(size.indexOf("text-")));
}

function px(size: string): number {
  const [, value, unit] = size.match(/\[(\d*\.?\d+)(px|rem)\]/) ?? [];
  return unit === "rem" ? Number(value) * 16 : Number(value);
}

function washShare(classes: string): number | null {
  let lightest: number | null = null;
  for (const m of classes.matchAll(TINT)) {
    const share = m[2] ? Number(m[2]) * 100 : Number(m[1]);
    if (share <= MAX_WASH) lightest = lightest === null ? share : Math.min(lightest, share);
  }
  return lightest;
}

describe("warning text on a warning tint", () => {
  it("the tertiary text reads at 4.5:1 on a /10 to /30 tint, on every surface, in both themes", async () => {
    const { primitives, schemes } = await themes();
    for (const [name, scope] of Object.entries(schemes)) {
      const tint = resolve("var(--color-feedback-warning-primary)", scope, primitives);
      const text = resolve("var(--color-feedback-warning-tertiary)", scope, primitives);
      for (const surface of SURFACES) {
        const ground = resolve(`var(--${surface})`, scope, primitives);
        for (const share of [0.05, 0.1, 0.2, MAX_WASH / 100]) {
          const chip = mix(tint, ground, share);
          expect(contrast(text, chip), `warning-tertiary on ${share * 100}% tint over ${surface}, ${name}`).toBeGreaterThanOrEqual(4.5);
        }
      }
    }
  });

  it("the primary text would not: it is why the chips take the tertiary", async () => {
    const { primitives, schemes } = await themes();
    const scope = schemes.light;
    const tint = resolve("var(--color-feedback-warning-primary)", scope, primitives);
    const ground = resolve("var(--bg-elevation-default-layer-0)", scope, primitives);
    expect(contrast(tint, mix(tint, ground, 0.1))).toBeLessThan(4.5);
  });

  it.each([
    ["bg-feedback-warning-primary/10 text-feedback-warning-primary", 10],
    ["bg-feedback-alert-primary/[0.06] text-feedback-warning-primary", 6],
    ["hover:bg-feedback-alert-primary/30", 30],
    ["bg-feedback-alert-primary/90", null],
    ["bg-feedback-warning-secondary-transparency-30", null],
  ])("reads %s as a wash of %s%%", (classes, share) => {
    expect(washShare(classes)).toBe(share);
  });

  it("never puts text-feedback-warning-primary on a warning or alert wash", async () => {
    const offenders: string[] = [];
    for await (const file of sources(path.join(FRONTEND, "src"))) {
      const source = await readFile(file, "utf8");
      for (const [classes] of source.matchAll(/["'`][^"'`\n]*["'`]/g)) {
        if (washShare(classes) !== null && PRIMARY_TEXT.test(classes)) {
          offenders.push(`${path.relative(FRONTEND, file)}: ${classes.slice(0, 120)}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it("sizes warning and alert notices with content-caption, never a literal text-[11px]", async () => {
    const offenders: string[] = [];
    for await (const file of sources(path.join(FRONTEND, "src"))) {
      const source = await readFile(file, "utf8");
      for (const [classes] of source.matchAll(/["'`][^"'`\n]*["'`]/g)) {
        if (classes.includes("text-[11px]") && NOTICE.test(classes)) {
          offenders.push(`${path.relative(FRONTEND, file)}: ${classes.slice(0, 120)}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it.each([
    ["px-1 py-px text-[9px] text-on-feedback-fill bg-feedback-alert-primary", ["text-[9px]"]],
    ["text-[10.5px] text-feedback-warning-primary", ["text-[10.5px]"]],
    ["text-[0.625rem] bg-feedback-alert-primary/10", ["text-[0.625rem]"]],
    ["content-caption text-feedback-warning-tertiary", []],
    ["w-[9px] text-feedback-warning-primary", []],
  ])("reads the literal sizes of %s", (classes, sizes) => {
    expect(literalSizes(classes)).toEqual(sizes);
  });

  it("sizes warning and alert text with a type token, never a literal size of 10 px or more", async () => {
    // Sizes under 10 px are ratcheted down to zero by src/no-text-under-10px.test.ts.
    const offenders: string[] = [];
    for await (const file of sources(path.join(FRONTEND, "src"))) {
      const source = await readFile(file, "utf8");
      for (const [classes] of source.matchAll(/["'`][^"'`\n]*["'`]/g)) {
        if (!NOTICE.test(classes) && !FILL_TEXT.test(classes)) continue;
        if (literalSizes(classes).some((size) => px(size) >= 10)) {
          offenders.push(`${path.relative(FRONTEND, file)}: ${classes.slice(0, 120)}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it("the shared components carry no literal size at all on warning and alert text, the EE badges included", async () => {
    const offenders: string[] = [];
    for await (const file of sources(path.join(FRONTEND, "src/components/ui"))) {
      const source = await readFile(file, "utf8");
      for (const [classes] of source.matchAll(/["'`][^"'`\n]*["'`]/g)) {
        if ((NOTICE.test(classes) || FILL_TEXT.test(classes)) && literalSizes(classes).length > 0) {
          offenders.push(`${path.relative(FRONTEND, file)}: ${classes.slice(0, 120)}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
