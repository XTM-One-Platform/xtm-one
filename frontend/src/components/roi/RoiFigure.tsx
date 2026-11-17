/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The one way a screen draws an ROI number (#9836 R4b, design
 * dev-docs/roi-outcomes.md 10.5): the figure, its low / high range, "Hidden"
 * for a cost the reader cannot see (never 0), and beside it the info button
 * that opens "How this is computed" (10.6). Two layouts, never a third:
 * `card` (a StatCard: the value, then the range as the caption) and `inline`
 * ("12 h (8 to 17)", for text and table cells). Reading a figure never starts
 * a run nor calls a model.
 */
import { useMemo } from "react";
import { CheckCheck, Clock3, Coins, Gauge, Hourglass, TrendingUp, Wallet, type LucideIcon } from "lucide-react";
import { CompactFigure } from "@/components/ui/compact-figure";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { InfoTip } from "@/components/ui/info-tip";
import { StatCard } from "@/components/ui/stat-card";
import { RoiHowComputed } from "@/components/roi/RoiHowComputed";
import type { RoiWindow } from "@/hooks/api/useRoiReportApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { isMaskedMetric, isUnderAMonth, metricRange, metricValue, ROI_METRIC_NAMES, type RoiFigures, type RoiMetric } from "@/lib/roi-figure";

const METRIC_ICONS: Record<RoiMetric, LucideIcon> = {
  hours: Clock3,
  value: TrendingUp,
  cost: Coins,
  net: Wallet,
  roi: Gauge,
  payback: Hourglass,
  review_rate: CheckCheck,
};

/** Why a cost reads "Hidden" (design 10.7, round 2 B3): the reason, never the figure. */
const COST_HIDDEN_REASON = "Cost data is hidden on this deployment; an administrator sets it at install. The value is shown before AI cost.";

/**
 * The number formats of every ROI figure, in the reader's locale (design
 * 10.5, compact since #10476): money and cost compact from 1,000 with no cents
 * from 100 (cents below, four decimals below one cent), hours and the ROI
 * multiple compact from 1,000, a payback in months, a rate as a whole
 * percent. `exact` is the full-precision form a tooltip shows.
 */
function useRoiFigureFormat(currency: string) {
  const { t_i18n } = useFormatter();
  const nf = useNumberFormat(currency);
  return useMemo(() => {
    const number = (value: number) => nf.count(value);
    const hours = (value: number) => t_i18n("{hours} h", { hours: nf.count(value) });
    const multiple = (value: number) => t_i18n("{value} x", { value: nf.count(value) });
    const months = (value: number) =>
      isUnderAMonth(value) ? t_i18n("Under a month") : t_i18n("{count, plural, one {# month} other {# months}}", { count: Math.round(value) });
    /** One metric's value as text; the bare number of a range end has no unit. */
    const format = (metric: RoiMetric, value: number): string => {
      switch (metric) {
        case "hours":
          return hours(value);
        case "value":
        case "net":
        case "cost":
          return nf.money(value);
        case "roi":
          return multiple(value);
        case "payback":
          return months(value);
        case "review_rate":
          return nf.percent(value);
      }
    };
    /** The same value at full precision, for the tooltip. */
    const exact = (metric: RoiMetric, value: number): string => {
      switch (metric) {
        case "hours":
          return t_i18n("{hours} h", { hours: nf.exactCount(value) });
        case "value":
        case "net":
        case "cost":
          return nf.exactMoney(value);
        case "roi":
          return t_i18n("{value} x", { value: nf.exactCount(value) });
        case "payback":
          return months(value);
        case "review_rate":
          return nf.exactPercent(value);
      }
    };
    return { number, format, exact };
  }, [nf, t_i18n]);
}

/** The range caption of a metric ("96 to 170 h"), or `null` when it has none or collapses onto the value. */
function useRangeText(figures: RoiFigures, metric: RoiMetric, currency: string): { text: string; exact: string } | null {
  const { t_i18n } = useFormatter();
  const fmt = useRoiFigureFormat(currency);
  const range = metricRange(figures, metric);
  const value = metricValue(figures, metric);
  if (!range || value === null) return null;
  const [lowEnd, highEnd] = range as [number, number];
  const low = fmt.format(metric, lowEnd);
  const high = fmt.format(metric, highEnd);
  const likely = fmt.format(metric, value);
  if (low === likely && high === likely) {
    // Both ends write the value itself (a payback under a month at both ends): the basis instead (round 2 A1).
    return metric === "payback" ? { text: t_i18n("No setup cost"), exact: t_i18n("No setup cost") } : null;
  }
  // The unit is written once, after the high end ("96 to 170 h", "21.2 to 38.4 x"; design 10.5).
  const bareLow = metric === "hours" || metric === "roi" ? fmt.number(lowEnd) : low;
  return {
    text: t_i18n("{low} to {high}", { low: bareLow, high }),
    exact: t_i18n("{low} to {high}", { low: fmt.exact(metric, lowEnd), high: fmt.exact(metric, highEnd) }),
  };
}

interface RoiFigureProps {
  figures: RoiFigures;
  metric: RoiMetric;
  currency: string;
  window: RoiWindow;
  /** The report hides its cost figures from this reader. */
  costMasked?: boolean;
  layout: "card" | "inline";
  /** A card's caption when the metric has no range (the cost's "Measured", a review count). */
  caption?: React.ReactNode;
  /** Card only: opens what the figure counts (the review rate opens the unreviewed runs). */
  onClick?: () => void;
  /** Inline only: leave the range out of the cell (a list column, design 10.8); the figure and its button stay. */
  hideRange?: boolean;
  /** Inline only: let the range break onto its own line in a narrow slot (the command center value band); each part stays whole. */
  wrap?: boolean;
}

/** One ROI figure, with its range and its "How this is computed" button. */
export function RoiFigure({ figures, metric, currency, window, costMasked, layout, caption, onClick, hideRange, wrap }: RoiFigureProps) {
  const { t_i18n } = useFormatter();
  const fmt = useRoiFigureFormat(currency);
  const rangeText = useRangeText(figures, metric, currency);
  const masked = isMaskedMetric(metric, costMasked);
  const value = masked ? null : metricValue(figures, metric);
  const shown = masked ? t_i18n("Hidden") : value === null ? "-" : fmt.format(metric, value);
  // The exact figure on hover (#10476); a hidden or missing figure has none.
  const figure =
    masked || value === null ? shown : <CompactFigure exact={fmt.exact(metric, value)} testId={`roi-figure-${metric}-value`}>{shown}</CompactFigure>;
  const range = rangeText ? <CompactFigure exact={rangeText.exact}>{rangeText.text}</CompactFigure> : null;
  const how = <RoiHowComputed figures={figures} metric={metric} currency={currency} window={window} costMasked={costMasked} />;
  const reason = masked ? (
    <InfoTip label={t_i18n("Why this figure is hidden")} content={t_i18n(COST_HIDDEN_REASON)} />
  ) : value === null ? (
    <InfoTip label={t_i18n("Why there is no figure")} content={t_i18n("Not enough data in this period to compute it.")} />
  ) : null;

  if (layout === "inline") {
    return (
      <span className={cn("inline-flex items-center gap-1", wrap ? "max-w-full flex-wrap" : "whitespace-nowrap")} data-testid={`roi-figure-${metric}`}>
        <span className="whitespace-nowrap">{figure}</span>
        {!masked && !hideRange && range && <span className="whitespace-nowrap text-default-secondary">({range})</span>}
        {reason}
        {how}
      </span>
    );
  }
  // The range is the caption (design 10.5); the scope's own line (a count, the cost) follows it, never lost.
  const subtitle = masked ? (
    t_i18n("Costs hidden on this deployment")
  ) : range && caption ? (
    <>
      <span className="block">{range}</span>
      <span className="block">{caption}</span>
    </>
  ) : (
    (range ?? caption ?? "")
  );
  return (
    // The wrapper stretches with its grid row and the card fills it, so every card of a row is one height (UX r1 B7).
    <div className="relative flex h-full min-w-0 flex-col" data-testid={`roi-figure-${metric}`}>
      <StatCard
        className="flex-1"
        icon={METRIC_ICONS[metric]}
        label={ROI_METRIC_NAMES[metric]}
        value={figure}
        subtitle={
          <span className="inline-flex flex-wrap items-center gap-1">
            <span data-testid={`roi-figure-${metric}-caption`}>{subtitle}</span>
            {reason}
          </span>
        }
        onClick={onClick}
      />
      <div className="absolute right-2 top-2">{how}</div>
    </div>
  );
}
