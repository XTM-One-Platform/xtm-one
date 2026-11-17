// @vitest-environment node
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { describe, it, expect } from "vitest";
// @ts-expect-error - a plain .mjs build script, typed by its JSDoc only
import { SOLID_ALERT_FILL } from "../scripts/fds-migration/token-map.mjs";

/**
 * Text on a fill that keeps one colour in both themes must keep one colour too.
 *
 * `text-negative-primary` inverts with the theme, as the accent does: right on
 * the accent, wrong on the amber alert fill (light in both themes: 1.5:1 in the
 * light theme) and on the scrim (dark in both: dark on dark in the dark theme).
 * The impersonation banner shipped that way - a security notice that renders
 * only while an admin impersonates, so no rendered accessibility pass could see
 * it. `--color-on-feedback-fill` and `--color-on-scrim` (index.css) name the text
 * for those fills; this holds their contrast, computed from the design system's
 * own tokens in both themes, and keeps the inverting text off those fills.
 */

const FRONTEND = path.resolve(__dirname, "..");
const THEME = path.join(FRONTEND, "node_modules/@filigran/design-system/dist/tokens/theme.css");

type Rgb = [number, number, number];
type Scope = Record<string, string>;

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

/** A token's colour in a scope: a primitive's hex, through `var()` chains and `color-mix(…, transparent)`. */
function resolve(value: string, scope: Scope, primitives: Scope, over: Rgb): Rgb {
  const v = value.trim();
  const variable = v.match(/^var\(--([\w-]+)\)$/);
  if (variable) {
    const name = variable[1];
    if (primitives[name]) return hex(primitives[name]);
    expect(scope[name], `--${name} is not declared`).toBeDefined();
    return resolve(scope[name], scope, primitives, over);
  }
  const mix = v.match(/^color-mix\(in srgb,\s*(.+?)\s+(\d+(?:\.\d+)?)%,\s*transparent\)$/);
  if (mix) {
    const colour = resolve(mix[1], scope, primitives, over);
    const share = Number(mix[2]) / 100;
    return colour.map((c, i) => c * share + over[i] * (1 - share)) as Rgb;
  }
  if (v.startsWith("#")) return hex(v);
  throw new Error(`cannot resolve ${v}`);
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
  const host = await readFile(path.join(FRONTEND, "src/index.css"), "utf8");
  const hostTokens: Scope = Object.fromEntries(
    [...host.matchAll(/--(color-(?:on-feedback-fill|on-scrim|scrim)):\s*([^;]+);/g)].map(([, name, value]) => [name, value.trim()]),
  );
  const root = declarations(css, ":root");
  const dark = { ...root, ...declarations(css, ".dark"), ...hostTokens };
  const light = { ...root, ...declarations(css, ".light"), ...hostTokens };
  return { primitives, schemes: { dark, light } };
}

async function* sources(dir: string): AsyncGenerator<string> {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* sources(full);
    else if (/\.(tsx?)$/.test(entry.name) && !entry.name.includes(".test.")) yield full;
  }
}

describe("text on fills that keep one colour", () => {
  it("reads at 4.5:1 on the amber alert fill and on the scrim, in both themes", async () => {
    const { primitives, schemes } = await themes();
    for (const [name, scope] of Object.entries(schemes)) {
      const page = resolve("var(--bg-elevation-default)", scope, primitives, [0, 0, 0]);
      const amber = resolve("var(--color-feedback-alert-primary)", scope, primitives, page);
      const onAmber = resolve("var(--color-on-feedback-fill)", scope, primitives, page);
      const scrim = resolve("var(--color-scrim)", scope, primitives, page);
      const onScrim = resolve("var(--color-on-scrim)", scope, primitives, scrim);
      expect(contrast(onAmber, amber), `on-feedback-fill on the amber fill, ${name}`).toBeGreaterThanOrEqual(4.5);
      expect(contrast(onScrim, scrim), `on-scrim on the scrim, ${name}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("the inverting text would not: it is why these tokens exist", async () => {
    const { primitives, schemes } = await themes();
    const page = resolve("var(--bg-elevation-default)", schemes.light, primitives, [0, 0, 0]);
    const amber = resolve("var(--color-feedback-alert-primary)", schemes.light, primitives, page);
    const negative = resolve("var(--text-negative-primary)", schemes.light, primitives, page);
    expect(contrast(negative, amber)).toBeLessThan(2);
  });

  it("never puts text-negative-primary on the amber fill or on the scrim", async () => {
    const offenders: string[] = [];
    for await (const file of sources(path.join(FRONTEND, "src"))) {
      const source = await readFile(file, "utf8");
      for (const [classes] of source.matchAll(/["'`][^"'`\n]*["'`]/g)) {
        const fixedFill = /\bbg-scrim\b/.test(classes) || SOLID_ALERT_FILL.test(classes);
        if (fixedFill && /\btext-negative-primary\b/.test(classes)) {
          offenders.push(`${path.relative(FRONTEND, file)}: ${classes.slice(0, 120)}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  // The codemod picks the label with this same constant: a threshold of its
  // own here would flag the codemod's output on its next run.
  it.each([
    ["bg-feedback-alert-primary", true],
    ["bg-feedback-alert-primary/90", true],
    ["bg-feedback-alert-primary/70", true],
    ["bg-feedback-alert-primary/100", true],
    ["hover:bg-feedback-alert-primary/90", true],
    ["bg-feedback-alert-primary/60", false],
    ["bg-feedback-alert-primary/10", false],
    ["bg-feedback-alert-primary/5", false],
    ["bg-feedback-alert-primary-transparency", false],
  ])("counts %s as the solid fill: %s", (classes, solid) => {
    expect(SOLID_ALERT_FILL.test(classes)).toBe(solid);
  });

  it("dresses the impersonation banner in the fixed-fill text", async () => {
    const source = await readFile(path.join(FRONTEND, "src/components/layout/AppLayout.tsx"), "utf8");
    const banner = source.slice(source.indexOf("function ImpersonationBanner"), source.indexOf("function AppShell"));
    expect(banner).toContain("bg-feedback-alert-primary");
    expect(banner).toContain("text-on-feedback-fill");
    expect(banner).not.toContain("text-negative-primary");
  });
});
