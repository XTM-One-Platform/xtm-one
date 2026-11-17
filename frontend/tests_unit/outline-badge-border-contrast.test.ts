// @vitest-environment node
import { readFile } from "node:fs/promises";
import path from "node:path";
import { describe, it, expect } from "vitest";

/**
 * The outline Badge edge reads against the surfaces it sits on.
 *
 * A badge edge is a non-text boundary: it needs 3:1 against its ground. The
 * elevation borders never got there (`border-elevation-subtle` 1.0 to 1.7:1,
 * `border-elevation-default` 1.72 to 2.16:1 on a light layer-1 card), so the
 * outline variant draws its edge with the library's neutral feedback tone.
 * This reads the border utility the outline variant actually uses in
 * `badge.tsx`, resolves it from the library's own theme tokens, and holds 3:1
 * on every default, highlight and hover surface of layers 0 to 2, in both
 * themes.
 */

const FRONTEND = path.resolve(__dirname, "..");
const THEME = path.join(FRONTEND, "node_modules/@filigran/design-system/dist/tokens/theme.css");
const BADGE = path.join(FRONTEND, "src/components/ui/badge.tsx");

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

/** The border colour utility of the outline variant, read from the component source. */
async function outlineBorder(): Promise<string> {
  const source = await readFile(BADGE, "utf8");
  const outline = source.match(/\boutline:\s*"([^"]*)"/);
  expect(outline, "badge.tsx no longer declares an outline variant").not.toBeNull();
  const border = outline![1].match(/\bborder-([a-z][\w-]*)/);
  expect(border, "the outline variant no longer draws a border colour").not.toBeNull();
  return border![1];
}

/** The CSS variable a border utility paints with, per elevation layer. */
function edgeVariable(token: string, layer: number): string {
  const elevation = token.match(/^elevation-([\w-]+)$/);
  return elevation ? `var(--border-elevation-${elevation[1]}-layer-${layer})` : `var(--color-${token})`;
}

const LAYERS = [0, 1, 2];
const PANELS = ["default", "highlight", "hover"];

describe("outline Badge edge", () => {
  it("reaches 3:1 on every elevation surface of layers 0 to 2, in both themes", async () => {
    const token = await outlineBorder();
    const { primitives, schemes } = await themes();
    for (const [name, scope] of Object.entries(schemes)) {
      for (const layer of LAYERS) {
        const edge = resolve(edgeVariable(token, layer), scope, primitives);
        for (const panel of PANELS) {
          const ground = resolve(`var(--bg-elevation-${panel}-layer-${layer})`, scope, primitives);
          expect(
            contrast(edge, ground),
            `border-${token} on ${panel} layer ${layer}, ${name}`,
          ).toBeGreaterThanOrEqual(3);
        }
      }
    }
  });

  it("does not draw its edge with an elevation border", async () => {
    expect(await outlineBorder()).not.toMatch(/^elevation-/);
  });
});
