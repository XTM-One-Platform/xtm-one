// Gate: one Japanese term per concept. "Primary asset" is 主要資産 across the
// ja catalog; the katakana form プライマリ資産 crept into the CTEM funnel tooltip
// once (UX round 1 of xtm-one #1989) and must not come back.

import { describe, expect, it } from "vitest";

import ja from "@/i18n/lang/ja.json";

describe("ja primary asset term", () => {
  it("never writes primary asset as プライマリ資産", () => {
    const offenders = Object.entries(ja as Record<string, string>)
      .filter(([, value]) => value.includes("プライマリ資産"))
      .map(([key]) => key);
    expect(offenders).toEqual([]);
  });

  it("uses 主要資産 in the funnel reach tooltip", () => {
    const key =
      "{primary, plural, one {# primary asset} other {# primary assets}} reached through {supporting, plural, one {# supporting asset} other {# supporting assets}}.";
    expect((ja as Record<string, string>)[key]).toContain("主要資産");
  });
});
