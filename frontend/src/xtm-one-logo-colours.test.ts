/**
 * The XTM One logo colours, pinned (#964).
 *
 * Source of truth: Figma XTM_Hub frame 6685:1130 and filigran-design-system
 * docs/public/product-logos/xtmone.svg (node 2624:11345): dark emblem
 * purple-300 #A47AF0, wordmark gray-100 #F2F2F3; light, from the `.light`
 * tokens, emblem --color-filigran-ia-primary (purple-500 #651FE5), wordmark
 * --text-default-primary (gray-900 #18191B). #903 drew the whole lockup
 * #9C27B0, off the palette and 3.05:1 on the dark sidebar.
 *
 * - The bundled public/ files (login, register, about, landing, every other
 *   surface) draw that Figma lockup.
 * - The header and switcher assets (src/assets/products/xtm-one*) draw the
 *   emblem and the wordmark in the emblem's one purple: "product logos and
 *   switchers only", on Sam's decision (21 Nov 2026).
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`).
const read = (path: string) => readFileSync(join(process.cwd(), path), "utf8");

/** The four emblem paths start at these points; every other path is the wordmark. */
const EMBLEM_STARTS = ["M122.326", "M116.525", "M41.2039", "M54.8965"];

function fills(path: string): { emblem: string[]; wordmark: string[] } {
  const emblem: string[] = [];
  const wordmark: string[] = [];
  for (const [, d, fill] of read(path).matchAll(/<path d="([^"]*)"[^>]*\bfill="([^"]*)"/g)) {
    (EMBLEM_STARTS.some((start) => d.startsWith(start)) ? emblem : wordmark).push(fill.toUpperCase());
  }
  return { emblem, wordmark };
}

const DARK = { emblem: "#A47AF0", wordmark: "#F2F2F3" };
const LIGHT = { emblem: "#651FE5", wordmark: "#18191B" };

describe("XTM One logo colours", () => {
  it.each([
    ["public/xtp-one.svg", DARK.emblem, DARK.wordmark],
    ["public/xtp-one-light.svg", LIGHT.emblem, LIGHT.wordmark],
    ["public/xtm-one-emblem.svg", DARK.emblem, null],
    ["public/xtm-one-emblem-light.svg", LIGHT.emblem, null],
    ["src/assets/products/xtm-one.svg", DARK.emblem, DARK.emblem],
    ["src/assets/products/xtm-one-light.svg", LIGHT.emblem, LIGHT.emblem],
    ["src/assets/products/xtm-one-mark.svg", DARK.emblem, null],
    ["src/assets/products/xtm-one-mark-light.svg", LIGHT.emblem, null],
  ])("%s draws the emblem %s and the wordmark %s", (path, emblem, wordmark) => {
    const got = fills(path);
    expect(got.emblem).toEqual([emblem, emblem, emblem, emblem]);
    expect(new Set(got.wordmark)).toEqual(new Set(wordmark ? [wordmark] : []));
    if (wordmark) expect(got.wordmark).toHaveLength(6);
  });
});
