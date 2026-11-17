// SPDX-License-Identifier: Apache-2.0

/**
 * The number formats of the active locale (#10476): `lib/number-format` bound
 * to the reader's language. Compact figures for display, exact ones for the
 * tooltip; `components/ui/compact-figure` draws one with its exact value.
 */
import { useMemo } from "react";
import { useIntl } from "react-intl";
import { exactCount, exactCurrency, exactPercent, formatCount, formatCurrency, formatPercent } from "@/lib/number-format";

interface NumberFormat {
  /** A count or a plain figure, compact from 1,000. */
  count: (value: number | null | undefined) => string;
  /** An amount in `currency`, compact from 1,000, no cents from 100. */
  money: (value: number | null | undefined) => string;
  /** A 0-1 rate as a whole percent. */
  percent: (value: number | null | undefined) => string;
  /** The exact forms, for a tooltip. */
  exactCount: (value: number | null | undefined) => string;
  exactMoney: (value: number | null | undefined) => string;
  exactPercent: (value: number | null | undefined) => string;
}

/** The number formats of the active locale; `currency` is the ISO code amounts are written in. */
export function useNumberFormat(currency = "USD"): NumberFormat {
  const { locale } = useIntl();
  return useMemo(
    () => ({
      count: (value) => formatCount(locale, value),
      money: (value) => formatCurrency(locale, currency, value),
      percent: (value) => formatPercent(locale, value),
      exactCount: (value) => exactCount(locale, value),
      exactMoney: (value) => exactCurrency(locale, currency, value),
      exactPercent: (value) => exactPercent(locale, value),
    }),
    [locale, currency],
  );
}
