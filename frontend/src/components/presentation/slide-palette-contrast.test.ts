/**
 * The XTM One palette's dark gradient carries the
 * dark page ink on the gradient banner and the highlighted tier badge, so both
 * ends of the gradient must read 4.5:1 behind that ink (the first gradient,
 * Violet #5E1AD5 to Light violet, measured 2.39:1 at its start). And a bento
 * image fills its cell at every cell count: the bento rule must outrank the
 * bucket width a measured image gets elsewhere (a cover photo was drawn at 674
 * of 828 px in a 3-cell bento).
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { slidePaletteStyle } from "./slide-palette.ts";

const stylesheet = readFileSync(`${__dirname}/presentation.css`, "utf8");

function luminance(hex: string): number {
  const value = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => {
    const c = parseInt(value.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** The declarations of the first rule whose selector list is exactly `selector`. */
function ruleBody(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`(?:^|\\n)${escaped}\\s*\\{([^}]*)\\}`).exec(stylesheet);
  expect(match, `no rule for ${selector}`).not.toBeNull();
  return match![1];
}

describe("XTM One palette, dark gradient behind the page ink", () => {
  const dark = slidePaletteStyle({ palette: "xtm_one" }, "dark") as Record<string, string>;
  // The dark page backgrounds the ink takes (`--deck-bg`): the Filigran Brand
  // Deck page and the default dark page.
  const inks = ["#0a0c1c", "#0d172b"];

  for (const end of ["--deck-gradient-from", "--deck-gradient-to"] as const) {
    for (const ink of inks) {
      it(`${end} reads 4.5:1 behind ${ink}`, () => {
        expect(contrast(dark[end], ink)).toBeGreaterThanOrEqual(4.5);
      });
    }
  }

  it("the tier badge on the dark gradient is drawn in the dark ink", () => {
    expect(
      ruleBody('.deck-root [data-palette="xtm_one"][data-design-glow="false"] .deck-tier[data-highlight="true"] .deck-tier-badge'),
    ).toMatch(/color:\s*#0a0c1c/);
  });

  it("the light gradient banner keeps white ink on two violets that both read 4.5:1", () => {
    const body = ruleBody(
      '.deck-root [data-palette="xtm_one"][data-scheme="light"][data-design-glow="false"] .deck-banner[data-variant="gradient"]',
    );
    expect(body).toMatch(/color:\s*#fbfbfb/);
    const stops = body.match(/#[0-9a-f]{6}/gi) ?? [];
    for (const stop of stops.filter((s) => s.toLowerCase() !== "#fbfbfb")) {
      expect(contrast(stop, "#fbfbfb")).toBeGreaterThanOrEqual(4.5);
    }
  });
});

describe("bento text cell", () => {
  // Text and KPI cells centre their content
  // vertically, with the `safe` keyword so content taller than the cell starts
  // at the top instead of being clipped above it.
  it("centres its content vertically, safely", () => {
    expect(ruleBody('.deck-root .deck-bento-cell[data-kind="text"]')).toMatch(/justify-content:\s*safe center/);
  });
});

describe("XTM One palette, margins over the floors", () => {
  it("the light phases timeline dot reads 4.5:1 on the light page", () => {
    const body = ruleBody(
      '.deck-root [data-palette="xtm_one"][data-scheme="light"][data-design-glow="false"] .deck-timeline[data-style="phases"] .deck-timeline-item::before',
    );
    const dot = body.match(/background:\s*(#[0-9a-f]{6})/i);
    expect(dot).not.toBeNull();
    // The light page (`--deck-bg`) of the default light scheme.
    expect(contrast(dot![1], "#e6f7ff")).toBeGreaterThanOrEqual(4.5);
  });

  it("the dark banner eyebrow is drawn at full ink, not at the base 0.85 opacity", () => {
    expect(ruleBody('.deck-root [data-palette="xtm_one"][data-scheme="dark"] .deck-banner-eyebrow')).toMatch(
      /opacity:\s*1\s*;/,
    );
    // The override is more specific than the base rule and also comes after it.
    expect(stylesheet.indexOf(".deck-root .deck-banner-eyebrow {")).toBeLessThan(
      stylesheet.indexOf('.deck-root [data-palette="xtm_one"][data-scheme="dark"] .deck-banner-eyebrow'),
    );
  });
});

describe("bento image cell", () => {
  it("outranks the bucket width of a measured image, so the image fills its cell", () => {
    const body = ruleBody(
      '.deck-root .deck-bento-cell[data-kind="image"] .deck-image,\n.deck-root .deck-bento-cell[data-kind="image"] .deck-figure .deck-image[data-sized="true"]',
    );
    expect(body).toMatch(/width:\s*100%/);
    expect(body).toMatch(/height:\s*100%/);
    // The bucket rule it must beat comes first in the file (equal specificity,
    // the later rule wins).
    const bucket = stylesheet.indexOf(
      '.deck-root .deck-figure:not([data-size="full"]):not([data-fill="true"]) .deck-image[data-sized="true"]',
    );
    const bento = stylesheet.indexOf('.deck-root .deck-bento-cell[data-kind="image"] .deck-figure .deck-image[data-sized="true"]');
    expect(bucket).toBeGreaterThan(-1);
    expect(bento).toBeGreaterThan(bucket);
  });
});
