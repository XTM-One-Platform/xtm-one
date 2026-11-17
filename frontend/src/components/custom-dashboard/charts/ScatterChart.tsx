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
 * Scatter and bubble charts (a point per row, or per point of each series;
 * a bubble's area follows its size field) and the punchcard (two categories
 * and a size, a circle per pair).
 */
import {
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Scatter,
  ScatterChart as RechartsScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";

import { CHART_TOOLTIP_PROPS } from "@/components/ui/charts/chart-tooltip";
import { bubbleRadius, punchcard, readScatterOptions, scatterPoints, seriesColor } from "./chart-options";
import { paletteColor } from "./colors";
import { AXIS_STROKE, AXIS_TICK, compact, GRID_STROKE, LABEL_STYLE, legendProps, type ChartProps } from "./common";
import { frameOf } from "./dynamic-options";

export default function ScatterChart({ shape, data, scope, height, view }: ChartProps & { view: "scatter" | "bubble" | "punchcard" }) {
  const options = readScatterOptions(scope.options);
  const fields = frameOf(shape, data);
  const xTitle = options.xAxisTitleVisibility === "show" ? options.xAxisTitleText : undefined;
  const yTitle = options.yAxisTitleVisibility === "show" ? options.yAxisTitleText : undefined;
  const xLabel = xTitle ? { value: xTitle, position: "insideBottom" as const, offset: 0, style: LABEL_STYLE } : undefined;
  const yLabel = yTitle ? { value: yTitle, angle: -90, position: "insideLeft" as const, style: LABEL_STYLE } : undefined;

  if (view === "punchcard") {
    const card = punchcard(fields, options);
    const color = seriesColor("", 0, options, paletteColor);
    const cells = card.cells.map((c) => ({ ...c, r: bubbleRadius(c.size, card.largest, 2, options.bubbleSizeMax) }));
    return (
      <ResponsiveContainer width="100%" height={height}>
        <RechartsScatterChart margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} opacity={0.3} />
          <XAxis dataKey="x" type="category" allowDuplicatedCategory={false} tick={AXIS_TICK} stroke={AXIS_STROKE} label={xLabel} height={xTitle ? 40 : 24} />
          <YAxis dataKey="y" type="category" allowDuplicatedCategory={false} tick={AXIS_TICK} stroke={AXIS_STROKE} width={yTitle ? 72 : 60} label={yLabel} />
          <ZAxis dataKey="size" range={[0, 1]} />
          <Tooltip {...CHART_TOOLTIP_PROPS} formatter={(v, name) => (name === "size" ? compact(v) : String(v))} cursor={false} />
          <Scatter
            data={cells}
            isAnimationActive={false}
            shape={(props: { cx?: number; cy?: number; payload?: { r: number } }) => (
              <circle cx={props.cx} cy={props.cy} r={props.payload?.r ?? 2} fill={color} fillOpacity={0.75} />
            )}
          />
        </RechartsScatterChart>
      </ResponsiveContainer>
    );
  }

  const bubble = view === "bubble";
  const points = scatterPoints(shape, fields, options, bubble);
  const categories = [...new Set(points.map((p) => p.category))].slice(0, 20);
  const largest = Math.max(0, ...points.map((p) => p.size ?? 0));
  const legend = categories.length > 1 ? legendProps(options.legendDisplay, "ellipsisEnd") : null;
  const domain = (min?: number, max?: number) => [min ?? "auto", max ?? "auto"] as [number | string, number | string];
  return (
    <ResponsiveContainer width="100%" height={height}>
      <RechartsScatterChart margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} opacity={0.4} />
        <XAxis dataKey="x" type="number" domain={["auto", "auto"]} tickFormatter={compact} tick={AXIS_TICK} stroke={AXIS_STROKE} label={xLabel} height={xTitle ? 40 : 24} />
        <YAxis
          dataKey="y"
          type="number"
          scale={options.yAxisScale}
          domain={domain(options.yAxisMin, options.yAxisMax)}
          allowDataOverflow={options.yAxisMin !== undefined || options.yAxisMax !== undefined}
          tickFormatter={compact}
          tick={AXIS_TICK}
          stroke={AXIS_STROKE}
          width={yTitle ? 56 : 44}
          label={yLabel}
        />
        <Tooltip {...CHART_TOOLTIP_PROPS} formatter={(v) => compact(v)} cursor={{ strokeDasharray: "3 3" }} />
        {legend && <Legend {...legend} />}
        {categories.map((category, i) => {
          const color = seriesColor(category, i, options, paletteColor);
          return (
            <Scatter
              key={`${category}-${i}`}
              name={category || undefined}
              data={points.filter((p) => p.category === category)}
              fill={color}
              isAnimationActive={false}
              shape={(props: { cx?: number; cy?: number; payload?: { size?: number } }) => (
                <circle
                  cx={props.cx}
                  cy={props.cy}
                  r={bubble ? bubbleRadius(props.payload?.size, largest, options.bubbleSizeMin, options.bubbleSizeMax) : 4}
                  fill={color}
                  fillOpacity={bubble ? 0.6 : 0.9}
                  stroke={color}
                />
              )}
            />
          );
        })}
      </RechartsScatterChart>
    </ResponsiveContainer>
  );
}
