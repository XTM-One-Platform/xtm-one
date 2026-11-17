// The CTEM score bands are levels on a red to green scale (#11661): none is
// drawn in the accent, which is the brand (purple in XTM One, blue in Copilot)
// and the colour of links, buttons and data bars, and no two levels share a
// ring colour.

import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { CTEM_BAND_CONFIG } from "@/lib/constants";

const LEVELS = ["critical", "at_risk", "fair", "good", "strong"] as const;

describe("CTEM_BAND_CONFIG", () => {
  it("draws no band in the accent, ring or name", () => {
    for (const [band, conf] of Object.entries(CTEM_BAND_CONFIG)) {
      expect(conf.ring, band).not.toMatch(/--color-accent\b/);
      expect(conf.color, band).not.toMatch(/(^|\s)text-accent(\/|\s|$)/);
    }
  });

  it("gives every level its own ring colour", () => {
    const rings = LEVELS.map((band) => CTEM_BAND_CONFIG[band].ring);
    expect(new Set(rings).size).toBe(LEVELS.length);
  });

  it("draws Good in the design system's tonic family, apart from Fair and Strong", () => {
    expect(CTEM_BAND_CONFIG.good).toMatchObject({
      label: "Good",
      color: "text-filigran-tonic-accent",
      ring: "var(--color-filigran-tonic-primary)",
    });
    expect(CTEM_BAND_CONFIG.good.color).not.toBe(CTEM_BAND_CONFIG.strong.color);
    expect(CTEM_BAND_CONFIG.good.color).not.toBe(CTEM_BAND_CONFIG.fair.color);
  });

  // The ring colour is a 3:1 graphics colour (light Good #009474 is 3.42:1 on
  // the page): a score or band name drawn in it fails 4.5:1. Text takes the
  // band's `color` class, the ring only strokes and fills (UX round 1, #11664).
  it("paints no text in the ring colour", () => {
    const sources = [
      "./ctem-viz.tsx",
      "./CtemStageCard.tsx",
      "./LifecycleBandCard.tsx",
      "../CtemCommandCenter.tsx",
      "../CtemStageDetail.tsx",
    ];
    for (const path of sources) {
      const source = readFileSync(new URL(path, import.meta.url), "utf8");
      expect(source, path).not.toMatch(/color:\s*[\w.?[\]]*\.ring\b/);
      expect(source, path).not.toMatch(/color:\s*ring\b/);
    }
  });
});
