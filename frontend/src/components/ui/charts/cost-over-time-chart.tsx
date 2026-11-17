import { useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { ChartScroller, PINNED_AXIS_WIDTH, PLOT_X_PADDING } from "@/components/ui/charts/chart-scroller";
import { CHART_TOOLTIP_PROPS } from "@/components/ui/charts/chart-tooltip";
import { MaskedCost, COST_DATA_UNAVAILABLE_I18N_KEY } from "@/components/ui/masked-cost";
import { Area, AreaChart, CartesianGrid, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { EVALUATION_CHART_COLORS } from "@/lib/constants";
import { formatChartCurrency } from "@/lib/number-format";
import { formatShortDate } from "@/lib/utils";
import type { TraceAnalyticsCostOverTimeResponse } from "@/types";
import { TOTAL_KEY, getStackKeys, longestFigure, pivotCostSeries, stackTotals, valueLabelLayout } from "@/pages/analytics/chart-data";
import { formatUsd } from "@/pages/analytics/format";
import { drawValueLabel } from "@/pages/analytics/value-label";
import { useFormatter } from "@/i18n";

const TICK = { fill: "var(--text-default-secondary)", fontSize: 11 };
/** The X axis band under the areas, the same on the plot and on the pinned Y axis chart. */
const X_AXIS_HEIGHT = 30;

/**
 * Cost over time per model (agent Observability tab, OpenRouter usage), on the
 * one cost scale of the product (#10476): the compact chart figures
 * (`formatChartCurrency`, `$41`, `$61.2K`, fr `41 $US`) on the axis and over
 * every point (the day's total), the full `formatUsd` figure in the shared
 * tooltip, and the pinned Y axis scroller when the figures need more than the card.
 */
export default function CostOverTimeChart({
  isLoading = false,
  series = [],
  costDataAvailable = true,
}: {
  isLoading?: boolean;
  series?: TraceAnalyticsCostOverTimeResponse["series"];
  costDataAvailable?: boolean;
}) {
  const { t_i18n, intl } = useFormatter();
  const chartUsd = (value: number) => formatChartCurrency(intl.locale, "USD", value);
  const pivoted = pivotCostSeries(series);
  const modelKeys = getStackKeys(pivoted);
  const rows = stackTotals(pivoted, modelKeys);
  const [plotWidth, setPlotWidth] = useState(0);
  // From the room each point really has and the longest figure: never turned 90 degrees.
  const labels = valueLabelLayout(rows.length, {
    plotWidth: plotWidth - 2 * PLOT_X_PADDING,
    longest: longestFigure(rows.map((row) => chartUsd(Number(row[TOTAL_KEY]) || 0))),
    boxWidth: plotWidth,
  });
  const margin = { top: labels.topMargin, right: 16 + labels.rightMargin, left: 0, bottom: 0 };
  const topKey = modelKeys[modelKeys.length - 1];

  return (
    <Card className="h-full">
      <CardContent className="p-4">
        {isLoading ? (
          <div className="h-[280px] rounded-sm border border-dashed border-elevation-subtle-soft flex items-center justify-center">
            <p className="content-base text-default-secondary">{t_i18n("Loading cost over time...")}</p>
          </div>
        ) : !costDataAvailable ? (
          <div className="h-[280px] rounded-sm border border-dashed border-elevation-subtle-soft flex items-center justify-center">
            <p className="content-base text-default-secondary inline-flex items-center gap-1.5">
              <MaskedCost />
              {t_i18n(COST_DATA_UNAVAILABLE_I18N_KEY)}
            </p>
          </div>
        ) : rows.length === 0 ? (
          <div className="h-[280px] rounded-sm border border-dashed border-elevation-subtle-soft flex items-center justify-center">
            <p className="content-base text-default-secondary">{t_i18n("No cost-over-time data for this filter set.")}</p>
          </div>
        ) : (
          <ChartScroller
            label={t_i18n("Cost over time chart, scrolls sideways")}
            resetKey={`${rows.length}:${rows[rows.length - 1]?.date ?? ""}`}
            minWidth={labels.minWidth}
            heightClassName="h-[280px]"
            onPlotWidth={setPlotWidth}
            axis={
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={rows} margin={{ ...margin, right: 0 }}>
                  <XAxis dataKey="date" tick={false} axisLine={false} tickLine={false} height={X_AXIS_HEIGHT} padding={{ left: PLOT_X_PADDING, right: PLOT_X_PADDING }} />
                  <YAxis tickFormatter={(value) => chartUsd(Number(value))} tick={TICK} stroke="var(--border-elevation-subtle)" width={PINNED_AXIS_WIDTH} />
                  {modelKeys.map((key) => (
                    <Area key={key} dataKey={key} stackId="cost" stroke="transparent" fill="transparent" isAnimationActive={false} activeDot={false} />
                  ))}
                </AreaChart>
              </ResponsiveContainer>
            }
          >
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={rows} margin={margin}>
                <CartesianGrid vertical={false} strokeDasharray="3 3" stroke="var(--border-elevation-subtle)" opacity={0.4} />
                <XAxis
                  dataKey="date"
                  tickFormatter={(value) => formatShortDate(value)}
                  tick={TICK}
                  stroke="var(--border-elevation-subtle)"
                  minTickGap={8}
                  height={X_AXIS_HEIGHT}
                  padding={{ left: PLOT_X_PADDING, right: PLOT_X_PADDING }}
                />
                {/* Hidden here, drawn pinned beside the scroller: it still sets the domain and the grid. */}
                <YAxis hide />
                <Tooltip
                  {...CHART_TOOLTIP_PROPS}
                  labelFormatter={(label) => formatShortDate(label)}
                  formatter={(value, key) => [formatUsd(Number(value)), String(key)]}
                  cursor={{ stroke: "color-mix(in srgb, var(--text-default-primary) calc(0.2 * 100%), transparent)", strokeWidth: 1 }}
                />
                {modelKeys.map((key, index) => (
                  <Area
                    key={key}
                    type="monotone"
                    dataKey={key}
                    stackId="cost"
                    stroke={EVALUATION_CHART_COLORS[index % EVALUATION_CHART_COLORS.length]}
                    fill={EVALUATION_CHART_COLORS[index % EVALUATION_CHART_COLORS.length]}
                    fillOpacity={0.3}
                  >
                    {/* The day's total rides on the top of the stack, like the LLM Analytics bars. */}
                    {key === topKey && (
                      <LabelList
                        valueAccessor={(entry: { payload?: Record<string, string | number> }) => chartUsd(Number(entry.payload?.[TOTAL_KEY]) || 0)}
                        content={(props) => drawValueLabel(props, labels, "var(--text-default-secondary)")}
                      />
                    )}
                  </Area>
                ))}
              </AreaChart>
            </ResponsiveContainer>
          </ChartScroller>
        )}
      </CardContent>
    </Card>
  );
}
