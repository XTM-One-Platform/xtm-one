// @vitest-environment node
import { readFile } from "node:fs/promises";
import path from "node:path";
import { describe, it, expect } from "vitest";

/**
 * "Go to the newer version" reads at 4.5:1 on the file card, in both themes.
 *
 * The link sits on a download card painted `bg-elevation-hover` (FileChips.tsx).
 * In the accent it read 4.49:1 on the light card surface (#651FE5 on #CACBCE,
 * XTM-One-Platform/xtm-one#10911 UX round 1, B3). It now takes the design
 * system's default text colour with the link type; this computes that contrast
 * from the library's own tokens on every hover layer and keeps the link on it.
 */

const FRONTEND = path.resolve(__dirname, "..");
const THEME = path.join(FRONTEND, "node_modules/@filigran/design-system/dist/tokens/theme.css");
const CHIPS = path.join(FRONTEND, "src/components/chat-panel/FileChips.tsx");

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

/** The card surface (`bg-elevation-hover`) on every layer the transcript can sit on. */
const CARD_SURFACES = [0, 1, 2, 3].map((layer) => `bg-elevation-hover-layer-${layer}`);

describe("newer-version link contrast", () => {
  it("the default text reads at 4.5:1 on the card surface, in both themes", async () => {
    const { primitives, schemes } = await themes();
    for (const [name, scope] of Object.entries(schemes)) {
      const text = resolve("var(--text-default-primary)", scope, primitives);
      for (const surface of CARD_SURFACES) {
        const ground = resolve(`var(--${surface})`, scope, primitives);
        expect(contrast(text, ground), `text-default-primary on ${surface}, ${name}`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it("the link takes the default text and the link type, never the accent", async () => {
    const source = await readFile(CHIPS, "utf8");
    const start = source.indexOf("function NewerVersionLink");
    expect(start, "NewerVersionLink is gone from FileChips.tsx").toBeGreaterThan(-1);
    const body = source.slice(start, source.indexOf("\n}\n", start));
    expect(body).toContain("text-default-primary");
    expect(body).toContain("content-compact-link");
    expect(body).not.toMatch(/\btext-accent\b/);
  });
});
