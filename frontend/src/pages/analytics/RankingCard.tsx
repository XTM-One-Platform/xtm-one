// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState, type ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { ChartCard, ChartPlaceholder } from "@/components/ui/charts/chart-card";
import { RankedBars } from "@/components/ui/charts/ranked-bars";
import { MaskedCost, COST_DATA_UNAVAILABLE_I18N_KEY } from "@/components/ui/masked-cost";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { useFormatter } from "@/i18n";

/** @i18n-keys */
const TOP_LABELS = { "5": "Top 5", "10": "Top 10", "25": "Top 25" } as const;
type Top = keyof typeof TOP_LABELS;
const TOP_OPTIONS = (Object.keys(TOP_LABELS) as Top[]).map((key) => ({ key, label: TOP_LABELS[key] }));

export interface RankingMetric<T> {
  key: string;
  /** English, translated by the switch. */
  label: string;
  value: (item: T) => number | null | undefined;
  format: (value: number) => string;
  /** A sum (cost, traces): each row shows its share of the whole. An average shows none. */
  additive: boolean;
  /** Hidden where the platform masks costs. */
  cost?: boolean;
}

interface RankingCardProps<T> {
  title: string;
  icon: LucideIcon;
  hint?: string;
  items: T[];
  itemKey: (item: T) => string;
  itemLabel: (item: T) => ReactNode;
  /** The full label, for one a row truncates. */
  itemTitle?: (item: T) => string;
  metrics: RankingMetric<T>[];
  costDataAvailable: boolean;
  isLoading: boolean;
  isFetching?: boolean;
  emptyLabel: string;
  barClassName?: string;
  onItemClick?: (item: T) => void;
  /** The page a row opens, or nothing for an inert row. Wins over `onItemClick`. */
  itemHref?: (item: T) => string | undefined;
  /** A chip after a row's label (a status). */
  itemBadge?: (item: T) => ReactNode;
  isItemActive?: (item: T) => boolean;
  className?: string;
}

/**
 * A "Top N" card: one ranking, the metric and the length switched in
 * its header, every row carrying its figure and share - the AI Adoption
 * "Top" cards, for any breakdown the dashboard reads.
 */
export function RankingCard<T>({
  title,
  icon,
  hint,
  items,
  itemKey,
  itemLabel,
  itemTitle,
  metrics,
  costDataAvailable,
  isLoading,
  isFetching,
  emptyLabel,
  barClassName,
  onItemClick,
  itemHref,
  itemBadge,
  isItemActive,
  className,
}: RankingCardProps<T>) {
  const { t_i18n } = useFormatter();
  const offered = useMemo(() => metrics.filter((metric) => costDataAvailable || !metric.cost), [metrics, costDataAvailable]);
  const [metricKey, setMetricKey] = useState<string>(offered[0]?.key ?? "");
  const [top, setTop] = useState<Top>("10");
  const metric = offered.find((item) => item.key === metricKey) ?? offered[0];

  const ranked = useMemo(() => {
    if (!metric) return [];
    return items
      .map((item) => ({ item, value: metric.value(item) }))
      .filter((entry): entry is { item: T; value: number } => typeof entry.value === "number" && !Number.isNaN(entry.value))
      .sort((a, b) => b.value - a.value);
  }, [items, metric]);
  const total = metric?.additive ? ranked.reduce((sum, entry) => sum + entry.value, 0) : undefined;
  const shown = ranked.slice(0, Number(top));

  let body: ReactNode;
  if (isLoading) {
    body = <ChartPlaceholder>{t_i18n("Loading...")}</ChartPlaceholder>;
  } else if (!metric) {
    body = (
      <ChartPlaceholder>
        <span className="inline-flex items-center gap-1.5">
          <MaskedCost />
          {t_i18n(COST_DATA_UNAVAILABLE_I18N_KEY)}
        </span>
      </ChartPlaceholder>
    );
  } else {
    body = (
      <RankedBars
        items={shown.map(({ item, value }) => ({
          key: itemKey(item),
          label: itemLabel(item),
          title: itemTitle?.(item),
          value,
          onClick: onItemClick ? () => onItemClick(item) : undefined,
          href: itemHref?.(item),
          badge: itemBadge?.(item),
          active: isItemActive?.(item),
        }))}
        format={metric.format}
        total={total}
        barClassName={barClassName}
        emptyLabel={emptyLabel}
      />
    );
  }

  return (
    <ChartCard
      title={title}
      icon={icon}
      hint={hint}
      isFetching={isFetching && !isLoading}
      className={className}
      summary={
        metric && total !== undefined && ranked.length > 0
          ? t_i18n("{total} across {count, plural, one {# row} other {# rows}}", {
              total: metric.format(total),
              count: ranked.length,
            })
          : undefined
      }
      actions={
        <>
          {offered.length > 1 && (
            <SegmentedFilter
              size="sm"
              options={offered.map((item) => ({ key: item.key, label: item.label }))}
              value={metric?.key ?? ""}
              onChange={setMetricKey}
              ariaLabel={t_i18n("Metric")}
            />
          )}
          <SegmentedFilter size="sm" options={TOP_OPTIONS} value={top} onChange={setTop} ariaLabel={t_i18n("Rows shown")} />
        </>
      }
    >
      {body}
    </ChartCard>
  );
}
