// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from "react";
import { TrendingUp } from "lucide-react";
import { Bar, BarChart, CartesianGrid, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { ChartCard, ChartPlaceholder } from "@/components/ui/charts/chart-card";
import { ChartScroller, PINNED_AXIS_WIDTH } from "@/components/ui/charts/chart-scroller";
import { CHART_TOOLTIP_PROPS } from "@/components/ui/charts/chart-tooltip";
import { MaskedCost, COST_DATA_UNAVAILABLE_I18N_KEY } from "@/components/ui/masked-cost";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { useFormatter } from "@/i18n";
import { EVALUATION_CHART_COLORS } from "@/lib/constants";
import { cn, formatModelLabel } from "@/lib/utils";
import {
  OTHER_KEY,
  TOTAL_KEY,
  bucketRows,
  defaultBucket,
  foldStack,
  keyTotals,
  labelKey,
  longestFigure,
  pivotCostSeries,
  pivotVolumeSeries,
  valueLabelLayout,
  type TimeBucket,
} from "@/pages/analytics/chart-data";
import { sourceLabel } from "@/pages/analytics/constants";
import { formatBucketLabel, formatCount, formatUsd } from "@/pages/analytics/format";
import { drawValueLabel } from "@/pages/analytics/value-label";
import type { TraceAnalyticsCostOverTimeResponse, TraceAnalyticsVolumeOverTimeResponse } from "@/types";

/** @i18n-keys */
const METRIC_LABELS = { cost: "Cost", traces: "Traces" } as const;
/** @i18n-keys */
const BUCKET_LABELS = { day: "Day", week: "Week", month: "Month" } as const;
/** @i18n-keys */
const VIEW_LABELS = { stacked: "Stacked", total: "Total" } as const;

type Metric = keyof typeof METRIC_LABELS;
type View = keyof typeof VIEW_LABELS;

const METRIC_OPTIONS = (Object.keys(METRIC_LABELS) as Metric[]).map((key) => ({ key, label: METRIC_LABELS[key] }));
const BUCKET_OPTIONS = (Object.keys(BUCKET_LABELS) as TimeBucket[]).map((key) => ({ key, label: BUCKET_LABELS[key] }));
const VIEW_OPTIONS = (Object.keys(VIEW_LABELS) as View[]).map((key) => ({ key, label: VIEW_LABELS[key] }));

/** The series a stack keeps before folding the rest into Other. */
const STACK_KEYS = 6;
/** The X axis band under the bars, the same on the plot and on the pinned Y axis chart. */
const X_AXIS_HEIGHT = 30;
const TICK = { fill: "var(--text-default-secondary)", fontSize: 11 };

interface UsageOverTimeCardProps {
  costSeries: TraceAnalyticsCostOverTimeResponse["series"];
  volumeSeries: TraceAnalyticsVolumeOverTimeResponse["series"];
  isLoading: boolean;
  isFetching: boolean;
  costDataAvailable: boolean;
  /** The window's length, which picks the opening bucket. */
  days: number;
}

export default function UsageOverTimeCard({
  costSeries,
  volumeSeries,
  isLoading,
  isFetching,
  costDataAvailable,
  days,
}: UsageOverTimeCardProps) {
  const { t_i18n } = useFormatter();
  const [metric, setMetric] = useState<Metric>(costDataAvailable ? "cost" : "traces");
  const [chosenBucket, setChosenBucket] = useState<{ days: number; bucket: TimeBucket } | null>(null);
  const [view, setView] = useState<View>("stacked");
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [plotWidth, setPlotWidth] = useState(0);
  // A new window opens on the bucket that suits it; a choice made for this one is kept.
  const bucket = chosenBucket && chosenBucket.days === days ? chosenBucket.bucket : defaultBucket(days);

  const { rows, keys, totals } = useMemo(() => {
    const pivoted = metric === "cost" ? pivotCostSeries(costSeries) : pivotVolumeSeries(volumeSeries);
    const folded = foldStack(bucketRows(pivoted, bucket), STACK_KEYS);
    return { rows: folded.rows, keys: folded.keys, totals: keyTotals(folded.rows, folded.keys) };
  }, [metric, costSeries, volumeSeries, bucket]);

  const visibleKeys = useMemo(
    () => (view === "total" ? [] : keys.filter((key) => !hidden.has(key))),
    [view, keys, hidden],
  );
  const drawn = useMemo(
    () =>
      view === "total"
        ? rows
        : rows.map((row) => ({ ...row, [TOTAL_KEY]: visibleKeys.reduce((sum, key) => sum + (Number(row[key]) || 0), 0) })),
    [rows, view, visibleKeys],
  );
  const windowTotal = drawn.reduce((sum, row) => sum + (Number(row[TOTAL_KEY]) || 0), 0);
  const peak = drawn.reduce<{ date: string; value: number } | null>((best, row) => {
    const value = Number(row[TOTAL_KEY]) || 0;
    return !best || value > best.value ? { date: row.date, value } : best;
  }, null);
  // One scale for the header, the axis, the figures over the bars, the tooltip and the legend.
  const format = metric === "cost" ? formatUsd : formatCount;
  // The figures are laid out from the room each bucket really has and the longest figure.
  const longest = longestFigure(drawn.map((row) => format(Number(row[TOTAL_KEY]) || 0)));
  const labels = valueLabelLayout(drawn.length, { plotWidth, longest, boxWidth: plotWidth });

  const label = (key: string) => {
    if (key === OTHER_KEY) return t_i18n("Other");
    return metric === "cost" ? formatModelLabel(key, t_i18n) : t_i18n(sourceLabel(key));
  };
  const color = (key: string) =>
    key === OTHER_KEY ? "var(--text-default-disabled)" : EVALUATION_CHART_COLORS[keys.indexOf(key) % EVALUATION_CHART_COLORS.length];
  const toggle = (key: string) =>
    setHidden((previous) => {
      const next = new Set(previous);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const masked = metric === "cost" && !costDataAvailable;
  let body;
  if (isLoading) {
    body = <ChartPlaceholder className="h-[300px]">{t_i18n("Loading...")}</ChartPlaceholder>;
  } else if (masked) {
    body = (
      <ChartPlaceholder className="h-[300px]">
        <span className="inline-flex items-center gap-1.5">
          <MaskedCost />
          {t_i18n(COST_DATA_UNAVAILABLE_I18N_KEY)}
        </span>
      </ChartPlaceholder>
    );
  } else if (drawn.length === 0) {
    body = <ChartPlaceholder className="h-[300px]">{t_i18n("No activity in this window for these filters.")}</ChartPlaceholder>;
  } else {
    const barKeys = view === "total" ? [TOTAL_KEY] : visibleKeys;
    // The stack's total rides on its topmost segment with a value, so a bucket
    // whose top series is at zero still carries its figure.
    const totalOver = (key: string) => (entry: { payload?: Record<string, string | number> }) => {
      const row = entry.payload;
      if (!row || labelKey(row, barKeys) !== key) return "";
      return format(Number(row[TOTAL_KEY]) || 0);
    };
    const margin = { top: labels.topMargin, right: 8 + labels.rightMargin, left: 0, bottom: 0 };
    // The pinned Y axis: the same rows, stack and margins, so its domain and ticks are the
    // plot's own; it draws its axis only and stays in view while the bars scroll beside it.
    const axis = (
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={drawn} margin={{ ...margin, right: 0 }}>
          <XAxis dataKey="date" tick={false} axisLine={false} tickLine={false} height={X_AXIS_HEIGHT} />
          <YAxis tickFormatter={(value) => format(Number(value))} tick={TICK} stroke="var(--border-elevation-subtle)" width={PINNED_AXIS_WIDTH} />
          {barKeys.map((key) => (
            <Bar key={key} dataKey={key} stackId="usage" fill="transparent" isAnimationActive={false} />
          ))}
        </BarChart>
      </ResponsiveContainer>
    );
    body = (
      <div className="space-y-3">
        {/* The scroller only ever holds the chart's own box: the tooltip is clipped to it, so a
            tooltip measured late or still moving can never widen the scroller and flash a
            scrollbar on hover. It scrolls only while the bars need more than the card,
            and opens on the latest bars again after a window, bucket, metric or view change. */}
        <ChartScroller
          label={t_i18n("Usage over time chart, scrolls sideways")}
          resetKey={`${days}:${bucket}:${metric}:${view}`}
          minWidth={labels.minWidth}
          heightClassName="h-[300px]"
          axis={axis}
          testId="usage-over-time-scroller"
          onPlotWidth={setPlotWidth}
        >
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={drawn} margin={margin}>
                <CartesianGrid vertical={false} strokeDasharray="3 3" stroke="var(--border-elevation-subtle)" opacity={0.4} />
                <XAxis
                  dataKey="date"
                  tickFormatter={(value) => formatBucketLabel(String(value), bucket)}
                  tick={TICK}
                  stroke="var(--border-elevation-subtle)"
                  minTickGap={8}
                  height={X_AXIS_HEIGHT}
                />
                {/* Hidden here, drawn pinned beside the scroller: it still sets the domain and the grid. */}
                <YAxis hide />
                {/* The shared compact tooltip: the tallest one (six series and Other) fits the plot,
                    so the clip above never cuts it and it never reaches below the chart. */}
                <Tooltip
                  {...CHART_TOOLTIP_PROPS}
                  labelFormatter={(value) => formatBucketLabel(String(value), bucket)}
                  formatter={(value, key) => [format(Number(value)), key === TOTAL_KEY ? t_i18n("Total") : label(String(key))]}
                  cursor={{ fill: "color-mix(in srgb, var(--text-default-primary) calc(0.06 * 100%), transparent)" }}
                />
                {barKeys.map((key, index) => (
                  <Bar
                    key={key}
                    dataKey={key}
                    stackId="usage"
                    fill={key === TOTAL_KEY ? "var(--color-accent)" : color(key)}
                    radius={index === barKeys.length - 1 ? [3, 3, 0, 0] : 0}
                    maxBarSize={48}
                  >
                    <LabelList
                      valueAccessor={totalOver(key)}
                      content={(props) => drawValueLabel(props, labels, "var(--text-default-secondary)")}
                    />
                  </Bar>
                ))}
              </BarChart>
            </ResponsiveContainer>
        </ChartScroller>
        {view === "stacked" && (
          <ul className="flex flex-wrap gap-1.5" aria-label={t_i18n("Series")}>
            {keys.map((key) => (
              <li key={key}>
                <button
                  type="button"
                  onClick={() => toggle(key)}
                  aria-pressed={!hidden.has(key)}
                  className={cn(
                    "inline-flex items-center gap-1.5 rounded-sm border border-elevation-subtle-soft px-2 py-0.5 content-caption transition-colors hover:bg-elevation-hover",
                    hidden.has(key) && "opacity-40",
                  )}
                >
                  <span className="h-2 w-2 rounded-full" style={{ backgroundColor: color(key) }} aria-hidden />
                  <span className="max-w-[180px] truncate">{label(key)}</span>
                  <span className="tabular-nums text-default-secondary">{format(totals[key] ?? 0)}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    );
  }

  return (
    <ChartCard
      title={t_i18n("Usage over time")}
      icon={TrendingUp}
      hint={t_i18n("Cost per model or traces per source, per day, week or month. The figure above each bar is its total; click a series below to hide it.")}
      isFetching={isFetching && !isLoading}
      summary={
        !isLoading && !masked && drawn.length > 0 ? (
          <span>
            {t_i18n("{total} in the window", { total: format(windowTotal) })}
            {peak && peak.value > 0 && (
              <span className="text-default-disabled">
                {" - "}
                {t_i18n("peak {value} ({date})", { value: format(peak.value), date: formatBucketLabel(peak.date, bucket) })}
              </span>
            )}
          </span>
        ) : undefined
      }
      actions={
        <>
          <SegmentedFilter size="sm" options={METRIC_OPTIONS} value={metric} onChange={setMetric} ariaLabel={t_i18n("Metric")} />
          <SegmentedFilter size="sm" options={VIEW_OPTIONS} value={view} onChange={setView} ariaLabel={t_i18n("Stacking")} />
          <SegmentedFilter
            size="sm"
            options={BUCKET_OPTIONS}
            value={bucket}
            onChange={(next) => setChosenBucket({ days, bucket: next })}
            ariaLabel={t_i18n("Interval")}
          />
        </>
      }
      className="xl:col-span-2"
    >
      {body}
    </ChartCard>
  );
}
