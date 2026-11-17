// SPDX-License-Identifier: Apache-2.0

/**
 * The one way a number is drawn in the UI (#10476): compact on every surface,
 * exact on hover and in exports.
 *
 * - Compact from 1,000 in the reader's locale: "1.2K", "316K", "3.5M", "1.2B"
 *   (fr "316 k", "3,5 M"), at most 3 significant digits and 1 decimal, so
 *   999,950 reads "1M", never "1000K".
 * - Under 1,000 a figure reads as it is ("921", "17.5").
 * - Currency: four decimals under one cent (a per-trace cost never reads
 *   "$0.00"), cents under 100, whole units under 1,000, then compact
 *   ("$316K", "-$40.5K"); never cents on a figure of 100 or more.
 * - The exact form (`exact*`) carries full precision and is what a tooltip
 *   shows; CSV exports and sorting read the raw value, never a string.
 * - A missing figure (null, undefined, NaN, Infinity) reads "-", never 0.
 *
 * Display only: the API, the agent tools and generated text keep exact
 * numbers. Pure functions, the locale passed in, so the rules are tested
 * without React; components use `useNumberFormat` (components/ui/compact-figure).
 */

/** What a missing figure reads. */
export const MISSING_FIGURE = "-";

const UNITS = [1e3, 1e6, 1e9, 1e12] as const;

/**
 * Round a magnitude the way the compact form shows it: 3 significant digits,
 * at most 1 decimal on its unit (3,470.6 -> 3,500; 97,767 -> 97,800;
 * 316,151 -> 316,000; 999,950 -> 1,000,000). Under 1,000 the value is
 * returned to 1 decimal.
 */
export function roundForCompact(abs: number): number {
  const small = Math.round(abs * 10) / 10;
  if (small < 1_000) return small;
  for (const unit of UNITS) {
    const scaled = abs / unit;
    const digits = scaled >= 100 ? 0 : 1;
    const factor = 10 ** digits;
    const rounded = Math.round(scaled * factor) / factor;
    if (rounded < 1_000 || unit === UNITS[UNITS.length - 1]) return rounded * unit;
  }
  return abs;
}

function isFigure(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** True when the compact form of `value` differs from its plain form (1,000 and above once rounded). */
export function isCompacted(value: number | null | undefined): boolean {
  return isFigure(value) && roundForCompact(Math.abs(value)) >= 1_000;
}

/**
 * The compact form of an already rounded figure. A locale whose first compact
 * unit is 10,000 (ja 万; zh and ko alike) draws 1,000 to 9,999 as bare digits
 * ("1700"): the figure then keeps its 3 significant digits with the locale's
 * grouping ("1,700") instead.
 */
function compactOrGrouped(locale: string, options: Intl.NumberFormatOptions, rounded: number): string {
  const compact = new Intl.NumberFormat(locale, { ...options, notation: "compact" }).format(rounded);
  const ungrouped = new Intl.NumberFormat(locale, { ...options, useGrouping: false }).format(rounded);
  if (compact !== ungrouped) return compact;
  return new Intl.NumberFormat(locale, options).format(rounded);
}

function compactNumber(locale: string, value: number): string {
  const rounded = roundForCompact(Math.abs(value)) * Math.sign(value);
  return compactOrGrouped(locale, { maximumFractionDigits: 1 }, rounded);
}

/** A count or a plain figure: "921", "17.5", "25.9K", "1.2M". */
export function formatCount(locale: string, value: number | null | undefined): string {
  if (!isFigure(value)) return MISSING_FIGURE;
  if (isCompacted(value)) return compactNumber(locale, value);
  return new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(value);
}

/** The exact count, to two decimals at most, with the locale's grouping: "3,470.62". */
export function exactCount(locale: string, value: number | null | undefined): string {
  if (!isFigure(value)) return MISSING_FIGURE;
  return new Intl.NumberFormat(locale, { maximumFractionDigits: 2 }).format(value);
}

function currencyFormat(locale: string, currency: string, options: Intl.NumberFormatOptions): Intl.NumberFormat | null {
  try {
    return new Intl.NumberFormat(locale, { style: "currency", currency, ...options });
  } catch {
    // An unknown currency code: the figure is still written, the code after it.
    return null;
  }
}

function withCode(locale: string, currency: string, value: number, options: Intl.NumberFormatOptions): string {
  return `${new Intl.NumberFormat(locale, options).format(value)}\u00a0${currency}`;
}

function currencyDigits(abs: number): number {
  if (abs > 0 && abs < 0.01) return 4;
  if (Math.round(abs * 100) / 100 < 100) return 2;
  return 0;
}

/** An amount: "$0.0040", "$6.19", "$921", "$97.8K", "-$40.5K", "$3.5M". */
export function formatCurrency(locale: string, currency: string, value: number | null | undefined): string {
  if (!isFigure(value)) return MISSING_FIGURE;
  if (value === 0) {
    const zero: Intl.NumberFormatOptions = { minimumFractionDigits: 0, maximumFractionDigits: 0 };
    return currencyFormat(locale, currency, zero)?.format(0) ?? withCode(locale, currency, 0, zero);
  }
  // From 100 an amount is written in whole units, so $999.60 is already $1,000: compact it.
  const abs = Math.abs(value) >= 100 ? Math.round(Math.abs(value)) : Math.abs(value);
  if (isCompacted(abs)) {
    const rounded = roundForCompact(abs) * Math.sign(value);
    const digits: Intl.NumberFormatOptions = { minimumFractionDigits: 0, maximumFractionDigits: 1 };
    if (currencyFormat(locale, currency, digits)) {
      return compactOrGrouped(locale, { style: "currency", currency, ...digits }, rounded);
    }
    return `${compactOrGrouped(locale, digits, rounded)}\u00a0${currency}`;
  }
  const digits = currencyDigits(Math.abs(value));
  const plain: Intl.NumberFormatOptions = { minimumFractionDigits: digits, maximumFractionDigits: digits };
  return currencyFormat(locale, currency, plain)?.format(value) ?? withCode(locale, currency, value, plain);
}

/**
 * An amount drawn on a chart (axis tick, figure over a point), where a narrow
 * pinned axis or a label per point has no room for cents: whole units from 10
 * ("$41", fr "41 $US"), at most two decimals below and none when they are
 * zero ("$0.5", "$2"), compact from 1,000 like `formatCurrency`. The tooltip
 * keeps the full `formatCurrency` figure.
 */
export function formatChartCurrency(locale: string, currency: string, value: number | null | undefined): string {
  if (!isFigure(value)) return MISSING_FIGURE;
  const abs = Math.abs(value);
  if (abs === 0 || abs < 0.01 || isCompacted(Math.round(abs))) return formatCurrency(locale, currency, value);
  const short: Intl.NumberFormatOptions = { minimumFractionDigits: 0, maximumFractionDigits: abs >= 10 ? 0 : 2 };
  return currencyFormat(locale, currency, short)?.format(value) ?? withCode(locale, currency, value, short);
}

/** The exact amount, in cents (four decimals under one cent): "$316,151.00". */
export function exactCurrency(locale: string, currency: string, value: number | null | undefined): string {
  if (!isFigure(value)) return MISSING_FIGURE;
  const digits = value !== 0 && Math.abs(value) < 0.01 ? 4 : 2;
  const exact: Intl.NumberFormatOptions = { minimumFractionDigits: digits, maximumFractionDigits: digits };
  return currencyFormat(locale, currency, exact)?.format(value) ?? withCode(locale, currency, value, exact);
}

/** A 0-1 rate as a whole percent: "62%"; a rate of 10 (1,000%) or more is compact. */
export function formatPercent(locale: string, rate: number | null | undefined): string {
  if (!isFigure(rate)) return MISSING_FIGURE;
  if (isCompacted(rate * 100)) return `${compactNumber(locale, rate * 100)}%`;
  return new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 0 }).format(rate);
}

/** The exact rate, to one decimal: "62.4%". */
export function exactPercent(locale: string, rate: number | null | undefined): string {
  if (!isFigure(rate)) return MISSING_FIGURE;
  return new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 1 }).format(rate);
}
