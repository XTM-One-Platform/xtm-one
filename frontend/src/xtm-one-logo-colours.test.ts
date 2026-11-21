/**
 * XTM One's header and switcher lockup draws one design-system purple.
 *
 * Sam, 21 Nov 2026 (#964): "product logos and switchers only", the emblem and
 * the "XTM One" wordmark in the emblem's purple. Dark: purple-300 #A47AF0, the
 * emblem colour of Figma XTM_Hub frame 6685:1130 and of filigran-design-system
 * docs/public/product-logos/xtmone.svg (node 2624:11345). Light: the `.light`
 * --color-filigran-ia-primary, purple-500 #651FE5. #903 drew it #9C27B0, off
 * the palette and 3.05:1 on the dark sidebar. Every other surface keeps the
 * bundled public/ files, which this test does not pin.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`).
const read = (file: string) => readFileSync(join(process.cwd(), "src", "assets", "products", file), "utf8");

/** Every path fill of an SVG, upper-cased (a clip-path rect is not a path). */
function pathFills(file: string): string[] {
  return [...read(file).matchAll(/<path\b[^>]*\bfill="([^"]*)"/g)].map(([, fill]) => fill.toUpperCase());
}

describe("XTM One header and switcher lockup colours", () => {
  it.each([
    ["xtm-one.svg", "#A47AF0", 10],
    ["xtm-one-mark.svg", "#A47AF0", 4],
    ["xtm-one-light.svg", "#651FE5", 10],
    ["xtm-one-mark-light.svg", "#651FE5", 4],
  ])("%s draws every path in %s", (file, purple, paths) => {
    const fills = pathFills(file);
    expect(fills).toHaveLength(paths);
    expect(new Set(fills)).toEqual(new Set([purple]));
  });
});
