// The chart series palette reads on the card in both themes: a
// graphical object needs 3:1 against what it sits on (WCAG 1.4.11). The lime
// "Handled elsewhere" swatch shipped at 1.5:1 on the white card.
import { describe, expect, it } from "vitest";

import { EVALUATION_CHART_COLORS } from "./constants";

/** The light card (white) and the darkest and lightest dark-theme surfaces a chart sits on. */
const SURFACES = { light: "#ffffff", darkPage: "#070d19", darkCard: "#1e2433" } as const;

function luminance(hex: string): number {
  const h = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => {
    const c = parseInt(h.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** CIE L*a*b* of an sRGB hex colour (D65). */
function toLab(hex: string): [number, number, number] {
  const h = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => {
    const c = parseInt(h.slice(i, i + 2), 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  const x = (r * 0.4124 + g * 0.3576 + b * 0.1805) / 0.95047;
  const y = r * 0.2126 + g * 0.7152 + b * 0.0722;
  const z = (r * 0.0193 + g * 0.1192 + b * 0.9505) / 1.08883;
  const f = (t: number) => (t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116);
  return [116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))];
}

/** CIE76 colour difference. */
function deltaE(a: string, b: string): number {
  const [la, lb] = [toLab(a), toLab(b)];
  return Math.hypot(la[0] - lb[0], la[1] - lb[1], la[2] - lb[2]);
}

/** The warning token (palette index 7) as it renders: rgb(184,85,10) light, rgb(230,112,15) dark. */
const WARNING_TOKEN = { light: "#b8550a", dark: "#e6700f" } as const;

describe("chart series palette", () => {
  const fixed = EVALUATION_CHART_COLORS.filter((colour) => colour.startsWith("#"));

  it("keeps fixed swatches to check (the token ones follow the design system)", () => {
    expect(fixed.length).toBeGreaterThan(0);
  });

  it("keeps the alert token out of the series fills: it reads 1.72:1 on the light card", () => {
    expect(EVALUATION_CHART_COLORS.some((colour) => colour.includes("feedback-alert-primary"))).toBe(false);
    expect(contrast("#f2be3a", SURFACES.light)).toBeLessThan(3);
  });

  it("keeps the amber (index 1) apart from the warning token (index 7) in both themes", () => {
    expect(EVALUATION_CHART_COLORS[7]).toContain("feedback-warning-primary");
    const amber = EVALUATION_CHART_COLORS[1];
    for (const [theme, warning] of Object.entries(WARNING_TOKEN)) {
      expect(deltaE(amber, warning), `${amber} vs ${warning} (${theme})`).toBeGreaterThanOrEqual(15);
    }
  });

  it.each(Object.entries(SURFACES))("reads at 3:1 or more on the %s surface", (_name, surface) => {
    for (const colour of fixed) {
      expect(contrast(colour, surface), `${colour} on ${surface}`).toBeGreaterThanOrEqual(3);
    }
  });
});
