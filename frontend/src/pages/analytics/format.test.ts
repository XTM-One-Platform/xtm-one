/** `yarn test:unit`: one scale for every LLM Analytics figure. */
import { describe, expect, it } from "vitest";

import { exactCompactCost, formatCompactCost } from "@/lib/utils";
import { formatCount, formatUsd } from "@/pages/analytics/format";

describe("formatUsd", () => {
  it("keeps cents under $100 and whole dollars up to $999", () => {
    expect(formatUsd(0)).toBe("$0");
    expect(formatUsd(0.004)).toBe("$0.0040");
    expect(formatUsd(0.55)).toBe("$0.55");
    expect(formatUsd(12.5)).toBe("$12.50");
    expect(formatUsd(99.99)).toBe("$99.99");
    expect(formatUsd(752.3)).toBe("$752");
    expect(formatUsd(999.4)).toBe("$999");
  });

  it("moves to k, M and B with one decimal at each boundary", () => {
    expect(formatUsd(999.99)).toBe("$1K");
    expect(formatUsd(1_000)).toBe("$1K");
    expect(formatUsd(3_912.02)).toBe("$3.9K");
    expect(formatUsd(27_016.44)).toBe("$27K");
    expect(formatUsd(61_176.89)).toBe("$61.2K");
    expect(formatUsd(999_999)).toBe("$1M");
    expect(formatUsd(1_000_000)).toBe("$1M");
    expect(formatUsd(3_450_000)).toBe("$3.5M");
    expect(formatUsd(1e9)).toBe("$1B");
    expect(formatUsd(36_500_000_000)).toBe("$36.5B");
  });

  it("writes the sign before the currency symbol", () => {
    expect(formatUsd(-1_500)).toBe("-$1.5K");
    expect(formatUsd(-752.3)).toBe("-$752");
    expect(formatUsd(-0.5)).toBe("-$0.50");
  });

  it("never writes a raw number for an unreadable amount", () => {
    expect(formatCompactCost(Number.NaN)).toBe("-");
    expect(formatCompactCost(Number.POSITIVE_INFINITY)).toBe("-");
  });

  it("gives the exact amount for a compact cost only, never for one already exact (UX B3 on #10529)", () => {
    expect(formatCompactCost(1234.5)).toBe("$1.2K");
    expect(exactCompactCost(1234.5)).toBe("$1,234.50");
    expect(exactCompactCost(999.6)).toBe("$999.60");
    expect(exactCompactCost(-40_522)).toBe("-$40,522.00");
    expect(exactCompactCost(921)).toBeUndefined();
    expect(exactCompactCost(6.19)).toBeUndefined();
    expect(exactCompactCost(0)).toBeUndefined();
    expect(exactCompactCost(null)).toBeUndefined();
    expect(exactCompactCost(Number.NaN)).toBeUndefined();
  });
});

describe("formatCount", () => {
  it("writes an integer under 1,000 and k / M / B above", () => {
    expect(formatCount(0)).toBe("0");
    expect(formatCount(999)).toBe("999");
    expect(formatCount(999.99)).toBe("1K");
    expect(formatCount(1_000)).toBe("1K");
    expect(formatCount(110_600)).toBe("111K");
    expect(formatCount(999_999)).toBe("1M");
    expect(formatCount(1_000_000)).toBe("1M");
    expect(formatCount(1e9)).toBe("1B");
    expect(formatCount(36_500_000_000)).toBe("36.5B");
    expect(formatCount(-27_016)).toBe("-27K");
  });

  it("writes the UI's language, as <html lang> carries it", () => {
    document.documentElement.lang = "fr";
    try {
      expect(formatCount(25_900)).toMatch(/^25,9\s?k$/);
      expect(formatUsd(316_151)).toMatch(/^316\s?k/);
    } finally {
      document.documentElement.lang = "";
    }
  });
});
