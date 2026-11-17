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
 * The `donut` kind: a pie or a donut, slices in the data's order (the legend
 * follows it), each its own colour, the slices under `collapseThreshold` of
 * the total gathered into one, values or shares as labels when asked.
 *
 * A slice's colour is, in order: the visualization's own (`seriesColorsByField`,
 * `seriesColors`), the item's (`color`, which a module's adapter sends: a
 * label's colour, a status', true in green and false in red), the look's
 * palette, the dashboard's. The look (`look.ts`) draws the modules' donuts
 * (OpenCTI's and OpenAEV's alike): a hole of 70%, slices parted by a 3 px line
 * in the surface colour, shares in 10 px bold on the ring (none on a slice
 * under 10 degrees), the legend below, a tooltip naming the slice only; set
 * at the top (`pieAlign`), as large as the width and the height above the
 * legend allow.
 */
import { Cell, Legend, Pie, PieChart as RechartsPieChart, ResponsiveContainer, Tooltip } from "recharts";

import { CHART_TOOLTIP_PROPS } from "@/components/ui/charts/chart-tooltip";
import { useColorMode } from "@/hooks/useColorMode";
import { useFormatter } from "@/i18n";

import { collapseSlices, readPieOptions, seriesColor } from "./chart-options";
import { paletteColor } from "./colors";
import { compact, LABEL_TIP_STYLE, legendProps, useChartEvents, useElementWidth, type ChartProps } from "./common";
import { frameOf, toNumber } from "./dynamic-options";
import { lookColors, lookNumber, nthColor, readLook, valueExtras } from "./look";

/** Slices under this share of the total carry no label. */
const MIN_LABELLED_SHARE = 0.03;
/** A share label is left off a slice narrower than this (10 degrees), as a module's donut does. */
const MIN_SHARE_LABEL = 10 / 360;
const RADIAN = Math.PI / 180;
/** The room a legend below takes (px). */
const LEGEND_ROOM = 32;
// A pie set by its centre, measured from the chart's own corner.
const NO_MARGIN = { top: 0, right: 0, bottom: 0, left: 0 };

export default function PieChart({ shape, data, scope, height }: ChartProps) {
  const { t_i18n, locale } = useFormatter();
  const mode = useColorMode();
  const options = readPieOptions(scope.options);
  const look = readLook(scope.options);
  const { ref, width } = useElementWidth<HTMLDivElement>();
  const fields = frameOf(shape, data);
  const extras = shape === "distribution" ? valueExtras(shape, data, mode) : [];
  const palette = lookColors(look, mode);
  // A distribution's labels and values; a series' totals by series.
  const items =
    shape === "series"
      ? fields.slice(1).map((f) => ({ label: f.name, value: f.values.reduce<number>((sum, v) => sum + (toNumber(v) ?? 0), 0) }))
      : (fields[0]?.values ?? []).map((label, i) => ({ label: String(label ?? ""), value: toNumber(fields[1]?.values[i]) ?? 0 }));
  const slices = collapseSlices(items, options.collapseThreshold, options.collapseLabel ?? t_i18n("Other"));
  const total = slices.reduce((sum, s) => sum + s.value, 0);
  const legend = legendProps(options.legendDisplay, options.legendTruncation, look);
  const colorOf = (slice: (typeof slices)[number]) =>
    slice.index < 0
      ? "var(--color-feedback-neutral-primary)"
      : seriesColor(slice.label, slice.index, options, (i) => extras[i]?.color ?? nthColor(palette, i) ?? paletteColor(i));
  const shares = options.labelDisplay === "percentage";
  const label =
    options.labelDisplay === "off"
      ? false
      : shares
        ? shareLabel
        : ({ value, percent }: { value?: unknown; percent?: number }) =>
            // A sliver's label would sit on its neighbour's: the tooltip has it.
            (percent ?? 0) < MIN_LABELLED_SHARE
              ? ""
              : options.labelDisplay === "values"
                ? compact(value)
                : `${compact(value)} (${Math.round((percent ?? 0) * 100)}%)`;
  const { onSelect } = useChartEvents();
  // A slice clicked: its label is the name, its key (else its label) the value, its count the y.
  const select = onSelect
    ? (item: unknown) => {
        const raw = item as { payload?: { label?: unknown; value?: unknown; index?: number }; label?: unknown; value?: unknown };
        const entry = raw.payload ?? raw;
        const at = (entry as { index?: number }).index;
        if (typeof entry.label === "string") onSelect({ name: entry.label, value: (at !== undefined ? extras[at]?.key : undefined) ?? entry.label, y: String(entry.value ?? "") });
      }
    : undefined;
  const separated = look.separatorWidth !== undefined;
  const outer = label && !shares ? "68%" : "85%";
  const hole = look.donutHoleSize !== undefined ? `${Math.round(0.85 * look.donutHoleSize)}%` : label && !shares ? "45%" : "55%";
  // At the top: the circle that fits the width and the height the legend leaves, its centre half
  // of that down (a little inside its edge, for the separator).
  const below = legend && legend.verticalAlign === "bottom" ? LEGEND_ROOM : 0;
  const size = Math.min(width, height - below);
  const top = look.pieAlign === "top" && size > 0;
  const outerRadius = size / 2.05;
  const geometry = top
    ? { cx: width / 2, cy: size / 2, outerRadius, innerRadius: options.showDonutHole ? (outerRadius * (look.donutHoleSize ?? 65)) / 100 : 0 }
    : { outerRadius: outer, innerRadius: options.showDonutHole ? hole : 0 };
  return (
    <div ref={ref} style={{ height }} className="min-w-0">
      <ResponsiveContainer width="100%" height={height}>
        <RechartsPieChart margin={top ? NO_MARGIN : undefined}>
          <Pie
            data={slices}
            dataKey="value"
            nameKey="label"
            {...geometry}
            paddingAngle={!separated && slices.length > 1 ? 1 : 0}
            startAngle={90}
            endAngle={-270}
            label={label}
            labelLine={false}
            isAnimationActive={false}
            onClick={select}
            className={onSelect ? "cursor-pointer" : undefined}
          >
            {slices.map((slice, i) => (
              <Cell key={`${slice.label}-${i}`} fill={colorOf(slice)} stroke="var(--bg-elevation-default)" strokeWidth={separated ? look.separatorWidth : 1} />
            ))}
          </Pie>
          {look.tooltipContent === "label" ? (
            <Tooltip content={({ active, payload }) => (active && payload?.length ? <div style={LABEL_TIP_STYLE}>{String(payload[0].name ?? "")}</div> : null)} />
          ) : (
            <Tooltip
              {...CHART_TOOLTIP_PROPS}
              formatter={(v) => `${lookNumber(look, v, compact, locale)}${total ? ` (${Math.round((Number(v) / total) * 100)}%)` : ""}`}
            />
          )}
          {legend && <Legend {...legend} />}
        </RechartsPieChart>
      </ResponsiveContainer>
    </div>
  );
}

/** A slice's share, one decimal, on the middle of its ring, in 10 px bold; none on a slice under 10 degrees. */
function shareLabel(props: { cx?: number; cy?: number; midAngle?: number; innerRadius?: number; outerRadius?: number; percent?: number; fill?: string }) {
  const { cx = 0, cy = 0, midAngle = 0, innerRadius = 0, outerRadius = 0, percent = 0, fill } = props;
  if (percent < MIN_SHARE_LABEL) return null;
  const radius = innerRadius + (outerRadius - innerRadius) / 2;
  const x = cx + radius * Math.cos(-midAngle * RADIAN);
  const y = cy + radius * Math.sin(-midAngle * RADIAN);
  // On a white slice the label is black.
  const color = fill?.toLowerCase() === "#ffffff" ? "#000000" : "var(--text-default-primary)";
  return (
    <text x={x} y={y} fill={color} textAnchor="middle" dominantBaseline="central" fontSize={10} fontWeight={600} fontFamily='"IBM Plex Sans", sans-serif' pointerEvents="none">
      {`${(percent * 100).toFixed(1)}%`}
    </text>
  );
}
