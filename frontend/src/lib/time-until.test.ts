// timeUntil in the viewer's language: the knowledge base retry notice
// read "est planifiée in 42m" in fr, es and ja because the relative time was
// hard-coded English.
import { describe, expect, it } from "vitest";

import { timeUntil } from "./utils";

const NOW = Date.parse("2026-11-03T12:00:00Z");
const inMs = (ms: number) => new Date(NOW + ms).toISOString();
const MIN = 60_000;
const norm = (s: string) => s.replace(/\s/g, " ");

describe("timeUntil", () => {
  it("keeps the compact English form without a locale or in English", () => {
    expect(timeUntil(inMs(42 * MIN), undefined, NOW)).toBe("in 42m");
    expect(timeUntil(inMs(42 * MIN), "en-us", NOW)).toBe("in 42m");
    expect(timeUntil(inMs(45_000), "en-us", NOW)).toBe("in 1m");
    expect(timeUntil(inMs(61 * MIN), "en-us", NOW)).toBe("in 1h");
    expect(timeUntil(inMs(2 * 24 * 60 * MIN), "en-us", NOW)).toBe("in 2d");
    expect(timeUntil(inMs(-MIN), "en-us", NOW)).toBe("shortly");
  });

  it("reads fr and ja literally, never the English fragment", () => {
    expect(norm(timeUntil(inMs(42 * MIN), "fr-fr", NOW))).toBe("dans 42 min");
    expect(norm(timeUntil(inMs(42 * MIN), "ja-jp", NOW))).toBe("42 分後");
    for (const locale of ["fr-fr", "es-es", "ja-jp"]) {
      expect(timeUntil(inMs(42 * MIN), locale, NOW)).not.toContain("in 42m");
      expect(timeUntil(inMs(42 * MIN), locale, NOW)).not.toContain("-");
    }
  });

  it("follows the same buckets as English in fr, es and ja", () => {
    const expected = (locale: string, value: number, unit: Intl.RelativeTimeFormatUnit) =>
      new Intl.RelativeTimeFormat(locale, { numeric: value === 0 ? "auto" : "always", style: "short" }).format(value, unit);
    for (const locale of ["fr-fr", "es-es", "ja-jp"]) {
      expect(timeUntil(inMs(45_000), locale, NOW)).toBe(expected(locale, 1, "minute"));
      expect(timeUntil(inMs(61 * MIN), locale, NOW)).toBe(expected(locale, 1, "hour"));
      expect(timeUntil(inMs(2 * 24 * 60 * MIN), locale, NOW)).toBe(expected(locale, 2, "day"));
      // Now or past is the word for now, never a zero count or "shortly".
      expect(timeUntil(inMs(-MIN), locale, NOW)).toBe(expected(locale, 0, "second"));
      expect(timeUntil(inMs(-MIN), locale, NOW)).not.toContain("0");
      expect(timeUntil(inMs(-MIN), locale, NOW)).not.toBe("shortly");
    }
    // Two days ahead keeps a number, never "après-demain".
    expect(timeUntil(inMs(2 * 24 * 60 * MIN), "fr-fr", NOW)).not.toContain("après-demain");
  });
});
