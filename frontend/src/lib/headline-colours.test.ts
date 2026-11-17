// One rule colours a headline's value and one colour marks each headline, on every
// page: the value takes its score band, the marker a hue no band uses.

import { describe, expect, it } from "vitest";

import { HEADLINES, headlineColorClass, headlineDef } from "@/lib/constants";
import { HEADLINE_LABELS } from "@/lib/evaluation-headlines";

const GOOD = "text-feedback-success-primary";
const MEDIUM = "text-feedback-warning-primary";
const POOR = "text-feedback-error-primary";

describe("a headline value's band", () => {
  it.each([
    [100, GOOD],
    [70, GOOD],
    [69.99, MEDIUM],
    [40, MEDIUM],
    [39.99, POOR],
    [0, POOR],
  ])("puts %j in its band", (value, band) => {
    expect(headlineColorClass(value)).toBe(band);
  });

  it("leaves an unmeasured value out of every band", () => {
    for (const value of [null, undefined]) {
      expect([GOOD, MEDIUM, POOR]).not.toContain(headlineColorClass(value));
    }
  });
});

describe("a headline's marker", () => {
  it("names each headline once, as every page shows it", () => {
    for (const id of ["quality", "performance", "judge"] as const) {
      expect(headlineDef(id).label).toBe(HEADLINE_LABELS[id]);
    }
  });

  it("gives each headline its own colour, none of them a band's", () => {
    const bandHues = /success|warning|error|alert/;
    expect(new Set(HEADLINES.map((h) => h.color)).size).toBe(HEADLINES.length);
    for (const h of HEADLINES) {
      expect(h.color).not.toMatch(bandHues);
      expect(h.textClass).not.toMatch(bandHues);
    }
  });

  it("never marks a headline with the accent, which branding can change", () => {
    for (const h of HEADLINES) {
      expect(h.color).not.toContain("accent");
      expect(h.textClass).not.toContain("accent");
    }
  });
});
