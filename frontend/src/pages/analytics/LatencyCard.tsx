// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import { Clock3, Info } from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  LabelList,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { ChartCard, ChartPlaceholder } from "@/components/ui/charts/chart-card";
import { ChartScroller, PINNED_AXIS_WIDTH, PLOT_X_PADDING } from "@/components/ui/charts/chart-scroller";
import { CHART_TOOLTIP_PROPS } from "@/components/ui/charts/chart-tooltip";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { useFormatter } from "@/i18n";
import { cn, formatModelLabel } from "@/lib/utils";
import { longestFigure, valueLabelLayout } from "@/pages/analytics/chart-data";
import { formatBucketLabel, formatCount, formatLatency } from "@/pages/analytics/format";
import { drawValueLabel } from "@/pages/analytics/value-label";
import type { TraceAnalyticsLatencyResponse } from "@/types";

/** @i18n-keys */
const VIEW_LABELS = { percentiles: "Percentiles", daily: "Daily median", models: "By model" } as const;
type View = keyof typeof VIEW_LABELS;
const VIEW_OPTIONS = (Object.keys(VIEW_LABELS) as View[]).map((key) => ({ key, label: VIEW_LABELS[key] }));

const PERCENTILES = ["p50", "p75", "p90", "p95", "p99"] as const;
/** The busiest models only: past this the category axis no longer fits the card. */
const MAX_MODELS = 10;
/** @i18n-keys */
const SERIES_LABELS = { p50: "Median", p95: "P95" } as const;
const MODEL_SERIES = [
  { key: "p50", fill: "var(--color-accent)", swatch: "bg-accent" },
  { key: "p95", fill: "var(--color-feedback-warning-primary)", swatch: "bg-feedback-warning-primary" },
] as const;

const TICK = { fill: "var(--text-default-secondary)", fontSize: 11 };
/** The X axis band under the line, the same on the plot and on the pinned Y axis chart. */
const X_AXIS_HEIGHT = 30;

export default function LatencyCard({
  latency,
  isLoading,
  isFetching,
}: {
  latency: TraceAnalyticsLatencyResponse | undefined;
  isLoading: boolean;
  isFetching: boolean;
}) {
  const { t_i18n } = useFormatter();
  const [view, setView] = useState<View>("percentiles");
  const [plotWidth, setPlotWidth] = useState(0);
  const percentiles = PERCENTILES.map((key) => ({ name: key.toUpperCase(), value: latency?.overall?.[key] ?? null })).filter(
    (row): row is { name: string; value: number } => row.value !== null,
  );
  const daily = (latency?.daily_median ?? []).filter((point) => point.p50 !== null);
  const models = (latency?.by_model ?? [])
    .filter((row) => row.p50 !== null)
    .sort((a, b) => b.trace_count - a.trace_count)
    .slice(0, MAX_MODELS)
    .map((row) => ({ name: formatModelLabel(row.model, t_i18n), p50: row.p50, p95: row.p95, traces: row.trace_count }));

  let body;
  if (isLoading) {
    body = <ChartPlaceholder>{t_i18n("Loading...")}</ChartPlaceholder>;
  } else if (view === "percentiles") {
    body =
      percentiles.length === 0 ? (
        <ChartPlaceholder>{t_i18n("No latency percentile data available.")}</ChartPlaceholder>
      ) : (
        <div className="h-[260px]">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={percentiles} margin={{ top: 20, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid vertical={false} strokeDasharray="3 3" stroke="var(--border-elevation-subtle)" opacity={0.4} />
              <XAxis dataKey="name" tick={TICK} stroke="var(--border-elevation-subtle)" />
              <YAxis tickFormatter={(value) => formatLatency(Number(value))} tick={TICK} stroke="var(--border-elevation-subtle)" width={52} />
              <Tooltip
                // The series is named by what it measures, as the card title, never by its data key ("value").
                formatter={(value) => [formatLatency(Number(value)), t_i18n("Latency")]}
                {...CHART_TOOLTIP_PROPS}
                cursor={{ fill: "color-mix(in srgb, var(--text-default-primary) calc(0.06 * 100%), transparent)" }}
              />
              <Bar dataKey="value" fill="var(--color-accent)" radius={[4, 4, 0, 0]} maxBarSize={56}>
                <LabelList
                  dataKey="value"
                  position="top"
                  formatter={(value) => formatLatency(Number(value))}
                  fill="var(--text-default-primary)"
                  fontSize={11}
                />
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      );
  } else if (view === "models") {
    body =
      models.length === 0 ? (
        <ChartPlaceholder>{t_i18n("No latency data per model in this window.")}</ChartPlaceholder>
      ) : (
        // The legend sits under the 260 px plot box, not inside it, so the box never overflows its own content.
        <div>
          <div className="h-[260px]">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={models} margin={{ top: 20, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid vertical={false} strokeDasharray="3 3" stroke="var(--border-elevation-subtle)" opacity={0.4} />
              <XAxis dataKey="name" tick={TICK} stroke="var(--border-elevation-subtle)" interval={0} />
              <YAxis tickFormatter={(value) => formatLatency(Number(value))} tick={TICK} stroke="var(--border-elevation-subtle)" width={52} />
              <Tooltip
                formatter={(value, name) => [formatLatency(Number(value)), t_i18n(SERIES_LABELS[name === "p95" ? "p95" : "p50"])]}
                labelFormatter={(label, payload) => {
                  const traces = payload?.[0]?.payload?.traces;
                  return traces == null ? String(label) : `${label} - ${t_i18n("Traces: {count}", { count: formatCount(traces) })}`;
                }}
                {...CHART_TOOLTIP_PROPS}
                cursor={{ fill: "color-mix(in srgb, var(--text-default-primary) calc(0.06 * 100%), transparent)" }}
              />
              {MODEL_SERIES.map(({ key, fill }) => (
                <Bar key={key} dataKey={key} fill={fill} radius={[4, 4, 0, 0]} maxBarSize={40}>
                  <LabelList
                    dataKey={key}
                    position="top"
                    formatter={(value) => formatLatency(Number(value))}
                    fill="var(--text-default-secondary)"
                    fontSize={10}
                  />
                </Bar>
              ))}
            </BarChart>
          </ResponsiveContainer>
          </div>
          <ul className="mt-1 flex justify-center gap-4 content-caption text-default-secondary" aria-label={t_i18n("Series")}>
            {MODEL_SERIES.map(({ key, swatch }) => (
              <li key={key} className="inline-flex items-center gap-1.5">
                <span className={cn("h-2 w-2 rounded-full", swatch)} aria-hidden />
                {t_i18n(SERIES_LABELS[key])}
              </li>
            ))}
          </ul>
        </div>
      );
  } else {
    // From the room each point really has and the longest figure: never turned 90 degrees.
    const pointLabels = valueLabelLayout(daily.length, {
      plotWidth: plotWidth - 2 * PLOT_X_PADDING,
      longest: longestFigure(daily.map((point) => formatLatency(point.p50 ?? null))),
      boxWidth: plotWidth,
    });
    const margin = { top: pointLabels.topMargin, right: 16 + pointLabels.rightMargin, left: 0, bottom: 0 };
    body =
      daily.length === 0 ? (
        <ChartPlaceholder>{t_i18n("No daily median latency data available.")}</ChartPlaceholder>
      ) : (
        <ChartScroller
          label={t_i18n("Daily median latency chart, scrolls sideways")}
          resetKey={`${daily.length}:${daily[daily.length - 1]?.date ?? ""}`}
          minWidth={pointLabels.minWidth}
          heightClassName="h-[260px]"
          onPlotWidth={setPlotWidth}
          // The pinned Y axis: the same points and margins, so its domain is the line's own.
          axis={
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={daily} margin={{ ...margin, right: 0 }}>
                <XAxis dataKey="date" tick={false} axisLine={false} tickLine={false} height={X_AXIS_HEIGHT} padding={{ left: PLOT_X_PADDING, right: PLOT_X_PADDING }} />
                <YAxis tickFormatter={(value) => formatLatency(Number(value))} tick={TICK} stroke="var(--border-elevation-subtle)" width={PINNED_AXIS_WIDTH} />
                <Line dataKey="p50" stroke="transparent" dot={false} activeDot={false} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          }
        >
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={daily} margin={margin}>
              <CartesianGrid vertical={false} strokeDasharray="3 3" stroke="var(--border-elevation-subtle)" opacity={0.4} />
              <XAxis dataKey="date" tickFormatter={(value) => formatBucketLabel(String(value), "day")} tick={TICK} stroke="var(--border-elevation-subtle)" minTickGap={8} height={X_AXIS_HEIGHT} padding={{ left: PLOT_X_PADDING, right: PLOT_X_PADDING }} />
              {/* Hidden here, drawn pinned beside the scroller: it still sets the domain and the grid. */}
              <YAxis hide />
              <Tooltip
                labelFormatter={(value) => formatBucketLabel(String(value), "day")}
                formatter={(value) => [formatLatency(Number(value)), t_i18n("Median")]}
                {...CHART_TOOLTIP_PROPS}
                cursor={{ stroke: "color-mix(in srgb, var(--text-default-primary) calc(0.2 * 100%), transparent)", strokeWidth: 1 }}
              />
              <Line dataKey="p50" stroke="var(--color-feedback-success-primary)" strokeWidth={2} dot={{ r: 2.5 }}>
                {/* The figure is the formatted median ("3.5s"), as on the axis and the tooltip: a custom content reads the value, not the formatter. */}
                <LabelList
                  valueAccessor={(entry: { payload?: { p50?: number | null } }) => formatLatency(entry.payload?.p50 ?? null)}
                  content={(props) => drawValueLabel(props, pointLabels, "var(--text-default-secondary)")}
                />
              </Line>
            </LineChart>
          </ResponsiveContainer>
        </ChartScroller>
      );
  }

  return (
    <ChartCard
      title={t_i18n("Latency")}
      icon={Clock3}
      hint={t_i18n("How long a trace takes from start to end: the percentiles over the window, the median of each day, or the median and P95 of each model.")}
      isFetching={isFetching && !isLoading}
      className="xl:col-span-2"
      summary={
        latency?.overall?.p50 != null
          ? t_i18n("Median {p50}, 95% under {p95}", {
              p50: formatLatency(latency.overall.p50),
              p95: formatLatency(latency.overall.p95 ?? null),
            })
          : undefined
      }
      actions={<SegmentedFilter size="sm" options={VIEW_OPTIONS} value={view} onChange={setView} ariaLabel={t_i18n("View")} />}
    >
      {body}
      {latency?.approximate ? (
        <p className="mt-2 flex items-start gap-1.5 content-caption text-default-secondary">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          {t_i18n(
            "Part of this range predates per-day latency distributions, so its percentiles are approximated from stored daily values.",
          )}
        </p>
      ) : null}
    </ChartCard>
  );
}
