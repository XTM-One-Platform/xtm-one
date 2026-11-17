/**
 * The compact number rules of every surface (#10476) - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  MISSING_FIGURE,
  exactCount,
  exactCurrency,
  exactPercent,
  formatChartCurrency,
  formatCount,
  formatCurrency,
  formatPercent,
  isCompacted,
  roundForCompact,
} from "./number-format.ts";

const en = "en-US";
/** Intl writes a no-break or narrow no-break space in fr; compare on plain spaces. */
const plain = (text: string) => text.replace(/[\u00a0\u202f]/g, " ");

test("a count under 1,000 reads as it is, to one decimal", () => {
  assert.equal(formatCount(en, 0), "0");
  assert.equal(formatCount(en, 921), "921");
  assert.equal(formatCount(en, 17.54), "17.5");
  assert.equal(formatCount(en, 999), "999");
});

test("a count from 1,000 is compact with at most 3 significant digits", () => {
  assert.equal(formatCount(en, 1_000), "1K");
  assert.equal(formatCount(en, 3_470.6), "3.5K");
  assert.equal(formatCount(en, 4_231), "4.2K");
  assert.equal(formatCount(en, 25_900), "25.9K");
  assert.equal(formatCount(en, 316_151), "316K");
  assert.equal(formatCount(en, 3_500_000), "3.5M");
  assert.equal(formatCount(en, 1_234_000_000), "1.2B");
});

test("a value that rounds up moves to the next unit, never 1000K", () => {
  assert.equal(formatCount(en, 999.96), "1K");
  assert.equal(formatCount(en, 999_950), "1M");
  assert.equal(formatCount(en, 999_950_000), "1B");
  assert.equal(roundForCompact(999_950), 1_000_000);
});

test("negatives keep their sign", () => {
  assert.equal(formatCount(en, -40_522), "-40.5K");
  assert.equal(formatCount(en, -12.5), "-12.5");
});

test("a missing figure reads '-', never 0", () => {
  for (const value of [null, undefined, Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.equal(formatCount(en, value), MISSING_FIGURE);
    assert.equal(formatCurrency(en, "USD", value), MISSING_FIGURE);
    assert.equal(formatPercent(en, value), MISSING_FIGURE);
    assert.equal(exactCount(en, value), MISSING_FIGURE);
    assert.equal(exactCurrency(en, "USD", value), MISSING_FIGURE);
    assert.equal(exactPercent(en, value), MISSING_FIGURE);
    assert.equal(isCompacted(value), false);
  }
});

test("an amount keeps cents under 100, whole units under 1,000, then compact", () => {
  assert.equal(formatCurrency(en, "USD", 0), "$0");
  assert.equal(formatCurrency(en, "USD", 0.004), "$0.0040");
  assert.equal(formatCurrency(en, "USD", 6.19), "$6.19");
  assert.equal(formatCurrency(en, "USD", 99.99), "$99.99");
  assert.equal(formatCurrency(en, "USD", 100.4), "$100");
  assert.equal(formatCurrency(en, "USD", 921), "$921");
  assert.equal(formatCurrency(en, "USD", 999.6), "$1K");
  assert.equal(formatCurrency(en, "USD", 97_767.05), "$97.8K");
  assert.equal(formatCurrency(en, "USD", 316_151), "$316K");
  assert.equal(formatCurrency(en, "USD", 685_659), "$686K");
  assert.equal(formatCurrency(en, "USD", -40_522), "-$40.5K");
  assert.equal(formatCurrency(en, "USD", 999_950), "$1M");
});

test("a chart amount drops the cents a narrow axis or a point label has no room for", () => {
  assert.equal(formatChartCurrency(en, "USD", 0), "$0");
  assert.equal(formatChartCurrency(en, "USD", 0.004), "$0.0040");
  assert.equal(formatChartCurrency(en, "USD", 0.5), "$0.5");
  assert.equal(formatChartCurrency(en, "USD", 2), "$2");
  assert.equal(formatChartCurrency(en, "USD", 6.19), "$6.19");
  assert.equal(formatChartCurrency(en, "USD", 40.94), "$41");
  assert.equal(formatChartCurrency(en, "USD", 921), "$921");
  assert.equal(formatChartCurrency(en, "USD", 61_234), "$61.2K");
  assert.equal(plain(formatChartCurrency("fr", "USD", 40.94)), "41 $US");
  assert.equal(plain(formatChartCurrency("fr", "USD", 10)), "10 $US");
  assert.equal(plain(formatChartCurrency("fr", "USD", 20)), "20 $US");
  assert.equal(formatChartCurrency(en, "USD", null), MISSING_FIGURE);
});

test("no figure of the Value page is longer than 7 characters once compact", () => {
  for (const value of [316_151, 97_767.05, 218_384, 127_356, 685_659, 308_642, 54_861.67, 41_442.52, -40_522, 600_000, -200_000]) {
    assert.ok(formatCurrency(en, "USD", value).length <= 7, `${value} -> ${formatCurrency(en, "USD", value)}`);
  }
  for (const value of [3_470.6, 1_394.4, 7_538.7, 3_377.8, 4_231]) {
    assert.ok(formatCount(en, value).length <= 7, `${value} -> ${formatCount(en, value)}`);
  }
});

test("an unknown currency code is written after the figure", () => {
  assert.equal(plain(formatCurrency(en, "NOTACODE", 316_151)), "316K NOTACODE");
  assert.equal(plain(formatCurrency(en, "NOTACODE", 12.5)), "12.50 NOTACODE");
});

test("the exact forms carry full precision for the tooltip", () => {
  assert.equal(exactCount(en, 3_470.6), "3,470.6");
  assert.equal(exactCount(en, 4_231), "4,231");
  assert.equal(exactCurrency(en, "USD", 97_767.05), "$97,767.05");
  assert.equal(exactCurrency(en, "USD", -40_522), "-$40,522.00");
  assert.equal(exactCurrency(en, "USD", 0.004), "$0.0040");
  assert.equal(exactPercent(en, 0.6243), "62.4%");
});

test("a rate is a whole percent", () => {
  assert.equal(formatPercent(en, 0), "0%");
  assert.equal(formatPercent(en, 0.62), "62%");
  assert.equal(formatPercent(en, 25), "2.5K%");
});

test("isCompacted tells when the exact form is worth a tooltip", () => {
  assert.equal(isCompacted(999), false);
  assert.equal(isCompacted(999.96), true);
  assert.equal(isCompacted(-1_000), true);
});

test("the four UI languages use their own compact units and separators", () => {
  assert.equal(plain(formatCount("fr", 3_470.6)), "3,5 k");
  assert.equal(plain(formatCount("fr", 3_500_000)), "3,5 M");
  assert.equal(plain(formatCount("es", 3_470.6)), "3,5 mil");
  assert.equal(formatCount("ja", 3_500_000), "350万");
  assert.match(plain(formatCurrency("fr", "EUR", 316_151)), /^316 k €$/);
  assert.equal(plain(exactCount("fr", 3_470.6)), "3 470,6");
});

test("ja keeps the grouping from 1,000 to 9,999, its first compact unit being 10,000", () => {
  assert.equal(formatCount("ja", 1_659.25), "1,700");
  assert.equal(formatCount("ja", -2_940), "-2,900");
  assert.equal(formatCount("ja", 9_999), "1万");
  assert.equal(formatCount("ja", 12_000), "1.2万");
  assert.equal(plain(formatCurrency("ja", "USD", 1_659.25)), "$1,700");
  assert.equal(plain(formatCurrency("ja", "USD", 4_100)), "$4,100");
  assert.equal(plain(formatCurrency("ja", "USD", -1_160)), "-$1,200");
  assert.equal(plain(formatCurrency("ja", "USD", 683)), "$683");
  assert.equal(plain(formatCurrency("en", "USD", 1_659.25)), "$1.7K");
});
