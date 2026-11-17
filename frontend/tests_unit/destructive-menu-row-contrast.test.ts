// @vitest-environment node
import { readFile } from "node:fs/promises";
import path from "node:path";
import { describe, it, expect } from "vitest";

/**
 * A destructive menu row reads at AA in both themes.
 *
 * `OverflowMenu` and `PointMenu` draw every destructive row ("Delete edge",
 * "Delete") through the shared `renderRow`. In the error primary, that text
 * read 4.26:1 on the dark menu panel (red-400 on grayblue-700) and 4.08:1 on a
 * hovered light row (red-600 on gray-200), under the 4.5:1 that 14 px text
 * needs. The tertiary error token is the same family a step away from the
 * surface in each theme. This holds its contrast, computed from the library's
 * own tokens on every panel and hover layer, and keeps the shared row on it.
 */

const FRONTEND = path.resolve(__dirname, "..");
const THEME = path.join(FRONTEND, "node_modules/@filigran/design-system/dist/tokens/theme.css");
const ROW = path.join(FRONTEND, "src/components/ui/overflow-menu.tsx");
const HEADER = path.join(FRONTEND, "src/components/layout/Header.tsx");

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

/** The menu panel (`bg-elevation-highlight`) and a highlighted row (`bg-input-hover`, the hover step), on every layer. */
const SURFACES = [0, 1, 2, 3].flatMap((layer) => [`bg-elevation-highlight-layer-${layer}`, `bg-elevation-hover-layer-${layer}`]);

/**
 * The header user menu panel and its hovered row. The panel declares
 * `layer-1 bg-elevation-default` in Header.tsx, so its ground is layer 1 (the
 * higher default layers are lighter panels it never sits on).
 */
const USER_MENU_SURFACES = ["bg-elevation-default-layer-1", "bg-elevation-hover-layer-1"];

describe("destructive menu row contrast", () => {
  it("the tertiary error text reads at 4.5:1 on the menu panel and its hover step, in both themes", async () => {
    const { primitives, schemes } = await themes();
    for (const [name, scope] of Object.entries(schemes)) {
      const text = resolve("var(--color-feedback-error-tertiary)", scope, primitives);
      for (const surface of SURFACES) {
        const ground = resolve(`var(--${surface})`, scope, primitives);
        expect(contrast(text, ground), `error-tertiary on ${surface}, ${name}`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it("the tertiary error text reads at 4.5:1 on the header user menu panel and its hover step, in both themes", async () => {
    const { primitives, schemes } = await themes();
    for (const [name, scope] of Object.entries(schemes)) {
      const text = resolve("var(--color-feedback-error-tertiary)", scope, primitives);
      for (const surface of USER_MENU_SURFACES) {
        const ground = resolve(`var(--${surface})`, scope, primitives);
        expect(contrast(text, ground), `error-tertiary on ${surface}, ${name}`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it("the header user menu draws its destructive rows in the tertiary error token, never the primary", async () => {
    const source = await readFile(HEADER, "utf8");
    for (const testId of ["emergency-stop-menu", "user-menu-sign-out"]) {
      const row = source.split("\n").find((line) => line.includes(`data-testid="${testId}"`));
      expect(row, `the ${testId} row is gone from Header.tsx`).toBeDefined();
      expect(row).toContain("text-feedback-error-tertiary");
      expect(row).not.toContain("text-feedback-error-primary");
    }
  });

  it("the primary would not: it is why the row takes the tertiary", async () => {
    const { primitives, schemes } = await themes();
    const dark = schemes.dark;
    const text = resolve("var(--color-feedback-error-primary)", dark, primitives);
    expect(contrast(text, resolve("var(--bg-elevation-highlight-layer-0)", dark, primitives))).toBeLessThan(4.5);
  });

  it("the shared menu row draws destructive items in the tertiary error token", async () => {
    const source = await readFile(ROW, "utf8");
    expect(source).toMatch(/item\.destructive && "text-feedback-error-tertiary"/);
    expect(source).not.toMatch(/item\.destructive && "text-feedback-error-primary"/);
    // The icon takes the label's token too.
    expect(source).toMatch(/"size-4 shrink-0", item\.destructive && "text-feedback-error-tertiary"/);
  });
});
