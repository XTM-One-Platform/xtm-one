/**
 * The bundled XTM One logo and rail emblem draw one design-system purple.
 *
 * Sam, 21 Nov 2026 (#964): the whole lockup, emblem and "XTM One" wordmark,
 * is one purple from the Figma tokens. Dark: purple-300 #A47AF0, the emblem
 * colour of Figma XTM_Hub frame 6685:1130 and of filigran-design-system
 * docs/public/product-logos/xtmone.svg (node 2624:11345). Light: the `.light`
 * --color-filigran-ia-primary, purple-500 #651FE5. #903 drew it #9C27B0,
 * off the palette and 3.05:1 on the dark sidebar.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`).
const read = (file: string) => readFileSync(join(process.cwd(), "public", file), "utf8");

/** Every path fill of a bundled SVG, upper-cased (the clip-path rect is not a path). */
function pathFills(file: string): string[] {
  return [...read(file).matchAll(/<path\b[^>]*\bfill="([^"]*)"/g)].map(([, fill]) => fill.toUpperCase());
}

describe("XTM One logo colours", () => {
  it.each([
    ["xtp-one.svg", "#A47AF0", 10],
    ["xtm-one-emblem.svg", "#A47AF0", 4],
    ["xtp-one-light.svg", "#651FE5", 10],
    ["xtm-one-emblem-light.svg", "#651FE5", 4],
  ])("%s draws every path in %s", (file, purple, paths) => {
    const fills = pathFills(file);
    expect(fills).toHaveLength(paths);
    expect(new Set(fills)).toEqual(new Set([purple]));
  });
});
