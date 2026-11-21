/**
 * The bundled XTM One logo and rail emblem draw the design system's colours.
 *
 * Source of truth: Figma (XTM_Hub frame 6685:1130, design-system node
 * 2624:11345) and filigran-design-system docs/public/product-logos/xtmone.svg.
 * Dark: emblem purple-300 #A47AF0, wordmark gray-100 #F2F2F3. Light, from the
 * `.light` tokens: emblem --color-filigran-ia-primary (purple-500 #651FE5),
 * wordmark --text-default-primary (gray-900 #18191B). #903 drew the whole
 * lockup #9C27B0, off the palette and 3.05:1 on the dark sidebar (#964).
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`).
const read = (file: string) => readFileSync(join(process.cwd(), "public", file), "utf8");

/** The four emblem paths start at these points; every other path is the wordmark. */
const EMBLEM_STARTS = ["M122.326", "M116.525", "M41.2039", "M54.8965"];

function fills(file: string): { emblem: Set<string>; wordmark: Set<string> } {
  const emblem = new Set<string>();
  const wordmark = new Set<string>();
  for (const [, d, fill] of read(file).matchAll(/<path d="([^"]*)"[^>]*\bfill="([^"]*)"/g)) {
    const isEmblem = EMBLEM_STARTS.some((start) => d.startsWith(start));
    (isEmblem ? emblem : wordmark).add(fill.toUpperCase());
  }
  return { emblem, wordmark };
}

describe("XTM One logo colours", () => {
  it.each([
    ["xtp-one.svg", "#A47AF0", "#F2F2F3"],
    ["xtp-one-light.svg", "#651FE5", "#18191B"],
  ])("%s draws the emblem %s and the wordmark %s", (file, emblem, wordmark) => {
    const got = fills(file);
    expect([...got.emblem]).toEqual([emblem]);
    expect([...read.wordmark]).toEqual([wordmark]);
  });

  it.each([
    ["xtm-one-emblem.svg", "#A47AF0"],
    ["xtm-one-emblem-light.svg", "#651FE5"],
  ])("the rail emblem %s draws %s", (file, emblem) => {
    const got = fills(file);
    expect([...got.emblem]).toEqual([emblem]);
    expect(read.wordmark.size).toBe(0);
  });
});
