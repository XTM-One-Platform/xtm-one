// @vitest-environment node
import { readFile } from "node:fs/promises";
import path from "node:path";
import { describe, it, expect } from "vitest";

/**
 * The hot-tone badge (#1267) reads in both themes.
 *
 * Each tone of `HOT_TONE_CLASS` (components/ui/tone-badge.tsx) is read from the
 * source, its text and wash resolved from the library's own theme tokens, and
 * held on every default and hover surface of layers 0 to 2: the label at 4.5:1
 * against the washed pill. One chip style since @filigran/design-system 1.13.0
 * (#1595): the library Chip's 30% secondary fill, the default label and the
 * 1px inset hairline in the tone's `-tertiary` ink at 30% (decorative, as in
 * the library, so it is checked for presence, not contrast).
 */

const FRONTEND = path.resolve(__dirname, "..");
const THEME = path.join(FRONTEND, "node_modules/@filigran/design-system/dist/tokens/theme.css");
const SOURCE = path.join(FRONTEND, "src/components/ui/tone-badge.tsx");

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

/** Each tone's classes, read from the component source. */
async function tones(): Promise<Record<string, string>> {
  const source = await readFile(SOURCE, "utf8");
  const block = source.match(/HOT_TONE_CLASS[^{]*\{([\s\S]*?)\n\};/);
  expect(block, "tone-badge.tsx no longer declares HOT_TONE_CLASS").not.toBeNull();
  return Object.fromEntries([...block![1].matchAll(/(\w+):\s*"([^"]+)"/g)].map(([, tone, classes]) => [tone, classes]));
}

/** The CSS variable a text or bg utility paints with. */
function variableOf(classes: string, kind: "text" | "bg"): { variable: string; share: number } | null {
  const m = classes.match(new RegExp(`(?<![\\w:-])${kind}-([a-z][\\w-]*?)(?:/(\\d+))?(?=\\s|$)`));
  if (!m || m[1] === "transparent") return null;
  const token = m[1];
  const translucent = token.match(/^(.*)-transparency-(\d+)$/);
  if (translucent) return { variable: `var(--color-${translucent[1]})`, share: Number(translucent[2]) / 100 };
  const variable = kind === "text" && token.startsWith("default-") ? `var(--text-${token})` : `var(--color-${token})`;
  return { variable, share: m[2] ? Number(m[2]) / 100 : 1 };
}

const SURFACES = [0, 1, 2].flatMap((layer) => [`bg-elevation-default-layer-${layer}`, `bg-elevation-hover-layer-${layer}`]);

describe("hot-tone badge", () => {
  it("declares the six hot tones", async () => {
    expect(Object.keys(await tones()).sort()).toEqual(["blue", "green", "neutral", "orange", "red", "yellow"]);
  });

  it("is the library Chip's single style: 30% fill, default label, inset hairline at 30%, no pastel class", async () => {
    for (const [tone, classes] of Object.entries(await tones())) {
      expect(classes, `${tone} still follows the retired pastel class`).not.toMatch(/pastel:/);
      expect(classes, `${tone} keeps the box size`).toMatch(/(^|\s)border-transparent(\s|$)/);
      expect(classes, `${tone} fill`).toMatch(/(^|\s)bg-feedback-[a-z]+-secondary-transparency-30(\s|$)/);
      expect(classes, `${tone} label`).toMatch(/(^|\s)text-default-primary(\s|$)/);
      expect(classes, `${tone} hairline`).toMatch(/(^|\s)inset-ring inset-ring-feedback-[a-z]+-tertiary\/30(\s|$)/);
    }
  });

  it("reads the label at 4.5:1 on every surface, in both themes", async () => {
    const table = await tones();
    const { primitives, schemes } = await themes();
    for (const [scheme, scope] of Object.entries(schemes)) {
      for (const [tone, classes] of Object.entries(table)) {
        const text = variableOf(classes, "text");
        const wash = variableOf(classes, "bg");
        expect(text, `${tone} has no text colour`).not.toBeNull();
        expect(wash, `${tone} has no wash`).not.toBeNull();
        for (const surface of SURFACES) {
          const ground = resolve(`var(--${surface})`, scope, primitives);
          const pill = mix(resolve(wash!.variable, scope, primitives), ground, wash!.share);
          expect(
            contrast(resolve(text!.variable, scope, primitives), pill),
            `${tone} label on ${surface}, ${scheme}`,
          ).toBeGreaterThanOrEqual(4.5);
        }
      }
    }
  });
});
