// timeAgo in the viewer's language: "Last run 9m ago" read "9m ago" in
// English in fr, es and ja because the relative time was never localised.
import { describe, expect, it } from "vitest";

import { timeAgo } from "./utils";

const NOW = Date.parse("2026-11-03T12:00:00Z");
const ago = (ms: number) => new Date(NOW - ms).toISOString();
const MIN = 60_000;

describe("timeAgo", () => {
  it("keeps the compact English form without a locale or in English", () => {
    expect(timeAgo(ago(9 * MIN), undefined, NOW)).toBe("9m ago");
    expect(timeAgo(ago(9 * MIN), "en-us", NOW)).toBe("9m ago");
    expect(timeAgo(ago(30_000), "en-us", NOW)).toBe("just now");
    expect(timeAgo(ago(3 * 60 * MIN), "en-us", NOW)).toBe("3h ago");
    expect(timeAgo(ago(2 * 24 * 60 * MIN), "en-us", NOW)).toBe("2d ago");
  });

  it("reads the viewer's language in fr, es and ja", () => {
    const expected = (locale: string, value: number, unit: Intl.RelativeTimeFormatUnit) =>
      new Intl.RelativeTimeFormat(locale, { numeric: value === 0 ? "auto" : "always", style: "short" }).format(value, unit);
    for (const locale of ["fr-fr", "es-es", "ja-jp"]) {
      expect(timeAgo(ago(9 * MIN), locale, NOW)).toBe(expected(locale, -9, "minute"));
      expect(timeAgo(ago(3 * 60 * MIN), locale, NOW)).toBe(expected(locale, -3, "hour"));
      expect(timeAgo(ago(2 * 24 * 60 * MIN), locale, NOW)).toBe(expected(locale, -2, "day"));
      expect(timeAgo(ago(30_000), locale, NOW)).toBe(expected(locale, 0, "second"));
    }
    expect(timeAgo(ago(9 * MIN), "fr-fr", NOW)).toContain("9");
    expect(timeAgo(ago(9 * MIN), "fr-fr", NOW)).not.toContain("ago");
  });

  it("reads naturally in fr, es and ja, never with a minus sign", () => {
    // Literal strings: an expectation built with the same formatter cannot
    // catch a wrong style (narrow fr gave "-9 min").
    const norm = (s: string) => s.replace(/\s/g, " ");
    expect(norm(timeAgo(ago(9 * MIN), "fr-fr", NOW))).toBe("il y a 9 min");
    expect(norm(timeAgo(ago(9 * MIN), "es-es", NOW))).toBe("hace 9 min");
    expect(norm(timeAgo(ago(9 * MIN), "ja-jp", NOW))).toBe("9 分前");
    expect(timeAgo(ago(9 * MIN), "fr-fr", NOW)).not.toContain("-");
    expect(timeAgo(ago(3 * 60 * MIN), "fr-fr", NOW)).not.toContain("-");
    expect(timeAgo(ago(3 * 24 * 60 * MIN), "fr-fr", NOW)).not.toContain("-");
  });

  it("counts days with a number in every language, like \"2d ago\"", () => {
    const twoDays = ago(2 * 24 * 60 * MIN);
    const oneDay = ago(24 * 60 * MIN);
    expect(timeAgo(twoDays, "fr-fr", NOW)).not.toContain("avant-hier");
    expect(timeAgo(oneDay, "fr-fr", NOW)).not.toContain("hier");
    expect(timeAgo(twoDays, "es-es", NOW)).not.toContain("anteayer");
    expect(timeAgo(oneDay, "es-es", NOW)).not.toContain("ayer");
    expect(timeAgo(twoDays, "ja-jp", NOW)).not.toContain("一昨日");
    for (const locale of ["fr-fr", "es-es", "ja-jp"]) {
      expect(timeAgo(twoDays, locale, NOW)).toContain("2");
      expect(timeAgo(oneDay, locale, NOW)).toContain("1");
      // Under a minute is the word for now, never a zero count.
      expect(timeAgo(ago(30_000), locale, NOW)).not.toContain("0");
    }
  });
});
