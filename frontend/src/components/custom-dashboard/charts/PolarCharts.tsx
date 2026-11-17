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
 * The `radar`, `polarArea`, `wordCloud` and `heatmap` kinds, drawn as the
 * modules draw these widgets (`dev-docs/dashboard-rendering-reference.md`:
 * OpenCTI's radar, polar area, word cloud and heatmap, OpenAEV's posture
 * radar), any dashboard's distribution or series too.
 *
 * - radar: one axis per label, one light shape per series (a 1 px line, a
 *   0.2 fill, round markers), labels cut to 25 characters, polygons in the
 *   text colour at 0.1; no legend for one series.
 * - polar area: equal slices from the top, each reaching out as far as its
 *   value on round rings, half opaque, spokes and rings at 0.1, the ring
 *   values up the top spoke, the legend below, a label-only tooltip.
 * - word cloud: horizontal words on a rectangular spiral, 20 to 50 px on a log
 *   scale, the Material palette.
 * - heatmap: a row per series (the first at the bottom), a cell per point,
 *   coloured by ten equal ranges of the values (zero apart), the ranges as the
 *   legend.
 *
 * Their look's options (`look.ts`) let a module's preset say what differs; the
 * geometry is `polar-charts.ts`. A click on a label, a marker, a slice, a word
 * or a cell says what was clicked (the dashboard's interactions). Every label
 * is text.
 */
import { useState } from "react";
import { Legend, PolarAngleAxis, PolarGrid, PolarRadiusAxis, Radar, RadarChart as RechartsRadarChart, ResponsiveContainer, Tooltip } from "recharts";

import { CHART_TOOLTIP_PROPS } from "@/components/ui/charts/chart-tooltip";
import { useColorMode } from "@/hooks/useColorMode";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

import { collapseSlices, readPieOptions, seriesColor, truncateLabel } from "./chart-options";
import { mixColor, paletteColor } from "./colors";
import { compact, legendProps, clickable, useChartEvents, useElementWidth, type ChartProps } from "./common";
import { LabelTip } from "./LabelTip";
import { frameOf, toNumber } from "./dynamic-options";
import { cutLabel, gridColor, lookCategory, lookColors, lookNumber, nthColor, readLook, seriesColorsOf, valueExtras } from "./look";
import { niceScale } from "./nice-scale";
import { heatColor, heatRanges, paletteColors } from "./palettes";
import {
  axisLabelPlaces,
  heatmapGrid,
  heatShare,
  itemsOf,
  labelPixels,
  polarRings,
  polarSectors,
  radarFit,
  readHeatmapOptions,
  readRadarOptions,
  readWordCloudOptions,
  wordCloudLayout,
} from "./polar-charts";

/** The surface the charts sit on: separators are drawn in it. */
const SURFACE = "var(--bg-elevation-default)";
/** A radar's labels cut to this many characters, on a word, unless the look says otherwise. */
const RADAR_LABEL_MAX = 25;
/** A radar's markers (px radius) and grid opacity, unless the look says otherwise. */
const RADAR_MARKER = 5;
const GRID_OPACITY = 0.1;

/** A radar: a distribution is one shape over its labels; a series frame, one shape per series over its x. */
export function RadarChart({ shape, data, scope, height }: ChartProps) {
  const { t_i18n, locale } = useFormatter();
  const mode = useColorMode();
  const options = readRadarOptions(scope.options);
  const look = readLook(scope.options);
  const fields = frameOf(shape, data);
  const { onSelect } = useChartEvents();
  const { ref, width } = useElementWidth<HTMLDivElement>();
  const series = shape === "series" ? fields.slice(1).map((f) => f.name) : [t_i18n("Count")];
  const labels = shape === "series" ? (fields[0]?.values ?? []).map((x) => String(x ?? "")) : itemsOf(shape, fields).map((i) => i.label);
  const rows =
    shape === "series"
      ? labels.map((label, i) => ({ label, ...Object.fromEntries(fields.slice(1).map((f) => [f.name, toNumber(f.values[i]) ?? 0])) }))
      : itemsOf(shape, fields).map((item) => ({ label: item.label, [series[0]]: item.value }));
  // What a click on a label filters on: the point's key when the data gives one.
  const keys = valueExtras(shape, data, mode).map((extras, i) => extras.key ?? labels[i]);
  const own = shape === "series" ? seriesColorsOf(data, mode) : [];
  const palette = lookColors(look, mode);
  const legend = series.length > 1 ? legendProps(options.legendDisplay, options.legendTruncation, look) : null;
  const tick = { fill: look.axisLabelColor ?? "var(--text-default-primary)", fontSize: look.xAxisFontSize ?? 11 };
  const grid = gridColor(look.gridOpacity ?? GRID_OPACITY, mode);
  const marker = look.markerSize ?? RADAR_MARKER;
  const select = (index: number) => onSelect?.({ name: labels[index], value: keys[index] ?? labels[index] });
  // The labels cut to what the box leaves beside the shape, and a radius that keeps them in it
  // (as the look asks, never past that room).
  const fit = radarFit(labels, width || 400, height - (legend ? 32 : 0), tick.fontSize, look.labelMaxLength ?? RADAR_LABEL_MAX);
  const radius = Math.min(look.radius ?? fit.radius, fit.radius);
  const cut = look.labelMaxLength ? look.labelCut : "words";
  // The centre is zero (or the least value, when below it); the rim the axis' nice end, as ApexCharts scales it.
  const values = rows.flatMap((row) => series.map((name) => Number(row[name as keyof typeof row]) || 0));
  const domain = look.niceTicks ? niceScale({ min: Math.min(0, ...values), max: Math.max(0, ...values), length: radius * 2, horizontal: false }) : null;
  return (
    <div ref={ref} className="w-full" style={{ height }}>
      <ResponsiveContainer width="100%" height={height}>
        <RechartsRadarChart data={rows} outerRadius={radius}>
          <PolarGrid stroke={grid} gridType="polygon" />
          <PolarAngleAxis
            dataKey="label"
            tick={tick}
            tickFormatter={(value: unknown) => cutLabel(String(value ?? ""), fit.chars, fit.chars < (look.labelMaxLength ?? RADAR_LABEL_MAX) ? "chars" : cut)}
            onClick={onSelect ? (item: { index?: number }) => select(Number(item.index)) : undefined}
            className={onSelect ? "cursor-pointer" : undefined}
          />
          <PolarRadiusAxis tick={false} axisLine={false} domain={domain ? [domain[0], domain[domain.length - 1]] : undefined} />
          {series.map((name, index) => {
            const color = seriesColor(name, index, options, (i) => own[i] ?? nthColor(palette, i) ?? paletteColor(i));
            return (
              <Radar
                key={name}
                name={name}
                dataKey={name}
                stroke={color}
                strokeWidth={options.lineWidth}
                fill={color}
                fillOpacity={options.areaOpacity}
                dot={{ r: marker, fill: color, fillOpacity: 1, stroke: color, strokeWidth: 1 }}
                activeDot={{ r: Math.max(marker, 6), onClick: onSelect ? (_e: unknown, point: unknown) => select(Number((point as { index?: number })?.index)) : undefined }}
                isAnimationActive={false}
              />
            );
          })}
          <Tooltip {...CHART_TOOLTIP_PROPS} formatter={(v) => lookNumber(look, v, compact, locale)} />
          {legend && <Legend {...legend} />}
        </RechartsRadarChart>
      </ResponsiveContainer>
    </div>
  );
}

/** A polar area: equal slices, each reaching out as far as its value on round rings. */
export function PolarAreaChart({ shape, data, scope, height }: ChartProps) {
  const { t_i18n, locale } = useFormatter();
  const mode = useColorMode();
  const options = readPieOptions(scope.options);
  const look = readLook(scope.options);
  const { onSelect } = useChartEvents();
  const [hover, setHover] = useState<{ label: string; x: number; y: number } | null>(null);
  const extras = valueExtras(shape, data, mode);
  const slices = collapseSlices(itemsOf(shape, frameOf(shape, data)), options.collapseThreshold, options.collapseLabel ?? t_i18n("Other"));
  const rings = polarRings(Math.max(0, ...slices.map((s) => s.value)));
  const outer = rings[rings.length - 1] ?? 0;
  const sectors = polarSectors(slices.map((s) => ({ label: s.label, value: s.value })), outer);
  const total = slices.reduce((sum, s) => sum + s.value, 0);
  const palette = lookColors(look, mode) ?? paletteColors("materialMixed", mode);
  const color = (index: number) => {
    const slice = slices[index];
    if (slice.index < 0) return "var(--color-feedback-neutral-primary)";
    return seriesColor(slice.label, slice.index, options, (i) => extras[i]?.color ?? nthColor(palette, i) ?? paletteColor(i));
  };
  const grid = gridColor(look.gridOpacity ?? GRID_OPACITY, mode);
  const legend = options.legendDisplay !== "off";
  const side = options.legendDisplay === "left" || options.legendDisplay === "right";
  const chartHeight = legend && !side ? Math.max(80, height - 28 - Math.ceil(sectors.length / 4) * 16) : height;
  const opacity = options.areaOpacity ?? 0.5;
  const separator = look.separatorWidth ?? 2;
  // The chart is drawn in a unit circle; a pixel is this much of it.
  const pixel = 2.24 / Math.max(80, chartHeight);
  const step = sectors.length ? (2 * Math.PI) / sectors.length : 0;
  return (
    <div
      className={cn("relative flex h-full w-full items-center gap-3", side ? (options.legendDisplay === "left" ? "flex-row-reverse" : "flex-row") : options.legendDisplay === "top" ? "flex-col-reverse" : "flex-col")}
      style={{ height }}
      onMouseLeave={() => setHover(null)}
    >
      <svg viewBox="-1.12 -1.12 2.24 2.24" style={{ height: chartHeight, maxWidth: "100%" }} className="shrink-0 overflow-visible" role="img" aria-label={t_i18n("Polar area chart")}>
        {rings.map((ring) => (
          <circle key={ring} r={ring / outer} fill="none" stroke={grid} strokeWidth={pixel} data-ring={ring} />
        ))}
        {sectors.length > 1 &&
          sectors.map((_, index) => (
            <line key={`spoke-${index}`} x1={0} y1={0} x2={Math.sin(index * step)} y2={-Math.cos(index * step)} stroke={grid} strokeWidth={pixel} />
          ))}
        {sectors.map((sector, index) => (
          <path
            key={`${sector.label}-${index}`}
            d={sector.path}
            fill={color(index)}
            fillOpacity={opacity}
            stroke={SURFACE}
            strokeWidth={separator * pixel}
            onMouseMove={(event) => {
              const box = (event.currentTarget.ownerSVGElement?.parentElement as HTMLElement | null)?.getBoundingClientRect();
              setHover({ label: sector.label, x: event.clientX - (box?.left ?? 0), y: event.clientY - (box?.top ?? 0) });
            }}
            {...clickable(onSelect, { name: sector.label, value: extras[slices[index].index]?.key ?? sector.label, y: String(sector.value) })}
          >
            {look.tooltipContent === "label" ? null : <title>{`${sector.label}: ${lookNumber(look, sector.value, compact, locale)}`}</title>}
          </path>
        ))}
        {rings.map((ring) => (
          <text key={`ring-${ring}`} x={3 * pixel} y={-ring / outer} dominantBaseline="middle" fontSize={11 * pixel} fill={look.axisLabelColor ?? "var(--text-default-secondary)"} pointerEvents="none">
            {lookNumber(look, ring, compact, locale)}
          </text>
        ))}
        {options.labelDisplay !== "off" &&
          sectors.map((sector, index) =>
            total && sector.value / total >= 0.03 ? (
              <text key={`label-${index}`} x={sector.labelAt.x} y={sector.labelAt.y} textAnchor="middle" dominantBaseline="middle" fontSize={11 * pixel} fill="var(--text-default-primary)" pointerEvents="none">
                {options.labelDisplay === "values" ? compact(sector.value) : `${compact(sector.value)} (${Math.round((sector.value / total) * 100)}%)`}
              </text>
            ) : null,
          )}
      </svg>
      {look.tooltipContent === "label" && hover && <LabelTip label={hover.label} x={hover.x} y={hover.y} />}
      {legend && (
        <ul className={cn("flex min-w-0 gap-x-3 gap-y-1 content-caption text-default-secondary", side ? "flex-col" : "flex-wrap justify-center")}>
          {sectors.map((sector, index) => (
            <li key={`${sector.label}-${index}`} className="flex min-w-0 items-center gap-1.5">
              <span className="h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: color(index) }} />
              <span className="truncate">{truncateLabel(sector.label, side ? 20 : 28, options.legendTruncation)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** A word cloud: the labels, each as large as its value (on a log scale), the largest in the middle. */
export function WordCloud({ shape, data, scope, height }: ChartProps) {
  const { t_i18n } = useFormatter();
  const mode = useColorMode();
  const options = readWordCloudOptions(scope.options);
  const look = readLook(scope.options);
  const { onSelect } = useChartEvents();
  const { ref, width } = useElementWidth<HTMLDivElement>();
  const extras = valueExtras(shape, data, mode);
  const palette = lookColors(look, mode) ?? paletteColors("materialMixed", mode);
  const words = wordCloudLayout(itemsOf(shape, frameOf(shape, data)), width, height, options.fontSizeMin, options.fontSizeMax);
  return (
    <div ref={ref} className="w-full" style={{ height }}>
      {width > 10 && (
        <svg width={width} height={height} viewBox={`${-width / 2} ${-height / 2} ${width} ${height}`} role="img" aria-label={t_i18n("Word cloud")}>
          {words.map((word) => (
            <text
              key={`${word.text}-${word.index}`}
              x={word.x}
              y={word.y}
              textAnchor="middle"
              dominantBaseline="central"
              fontSize={word.size}
              fontFamily='"IBM Plex Sans", sans-serif'
              fill={options.seriesColors[word.index] ?? extras[word.index]?.color ?? nthColor(palette, word.index)}
              {...clickable(onSelect, { name: word.text, value: extras[word.index]?.key ?? word.text, y: String(word.value) })}
            >
              <title>{`${word.text} (${word.value})`}</title>
              {word.text}
            </text>
          ))}
        </svg>
      )}
    </div>
  );
}

const LABEL_WIDTH = 120;
const AXIS_HEIGHT = 22;
const LEGEND_ROW = 18;

/** A heatmap: a row per series (the first at the bottom), a cell per point, coloured by ranges of the values. */
export function HeatmapChart({ shape, data, scope, height }: ChartProps) {
  const { t_i18n, locale } = useFormatter();
  const mode = useColorMode();
  const options = readHeatmapOptions(scope.options);
  const look = readLook(scope.options);
  const { onSelect } = useChartEvents();
  const { ref, width } = useElementWidth<HTMLDivElement>();
  const grid = heatmapGrid(shape, frameOf(shape, data));
  // A person's two colours draw a gradient; else the ranges of the values.
  const gradient = options.colorMin !== undefined || options.colorMax !== undefined;
  const ranges = gradient ? [] : heatRanges(grid.min, grid.max, paletteColors("heat", mode));
  const legend = ranges.length > 0 && options.legendDisplay !== "off";
  const number = (value: number) => lookNumber(look, value, compact, locale);
  // The legend wraps: about 6.5 px a character, a swatch and a gap per range.
  const legendWidth = ranges.reduce((sum, r) => sum + `${number(r.from)} - ${number(r.to)}`.length * 6.5 + 26, 0);
  const legendHeight = legend ? Math.max(1, Math.ceil(legendWidth / Math.max(1, width))) * LEGEND_ROW : 0;
  const rows = [...grid.rows.keys()].reverse();
  const labelWidth = grid.rows.length === 1 && grid.rows[0] === "" ? 0 : Math.min(LABEL_WIDTH, Math.round(width * 0.3));
  const cellWidth = grid.columns.length ? Math.max(1, (width - labelWidth) / grid.columns.length) : 0;
  const svgHeight = Math.max(40, height - legendHeight);
  const plotHeight = svgHeight - AXIS_HEIGHT;
  const cellHeight = grid.rows.length ? Math.max(8, plotHeight / grid.rows.length) : 0;
  const low = options.colorMin ?? mixColor(SURFACE, "var(--color-accent)", 0.15);
  const high = options.colorMax ?? "var(--color-accent)";
  const fill = (value: number | null) => {
    if (value === null) return "transparent";
    if (!gradient) return heatColor(value, ranges) ?? "transparent";
    return value <= 0 ? "var(--bg-elevation-hover)" : mixColor(low, high, heatShare(value, grid.min, grid.max));
  };
  const separator = look.separatorWidth ?? 1;
  const xTick = { fontSize: look.xAxisFontSize ?? 12, fill: look.axisLabelColor ?? "var(--text-default-secondary)" };
  const yTick = { fontSize: look.yAxisFontSize ?? 14, fill: look.axisLabelColor ?? "var(--text-default-secondary)" };
  const column = (value: string) => lookCategory({ dateFormat: look.dateFormat ?? "date" }, value, locale);
  // Each column's label as written under the axis (cut at 14 characters); only those with room are drawn.
  const columnLabels = grid.columns.map((x) => truncateLabel(column(x), 14, "ellipsisEnd"));
  return (
    <div ref={ref} className="w-full" style={{ height }}>
      {width > 10 && (
        <svg width={width} height={svgHeight} role="img" aria-label={t_i18n("Heatmap")}>
          {rows.map((r, place) => {
            const row = grid.rows[r];
            return (
              <g key={`${row}-${r}`} transform={`translate(0 ${place * cellHeight})`} data-row={row}>
                {labelWidth > 0 && (
                  <text x={labelWidth - 6} y={cellHeight / 2} textAnchor="end" dominantBaseline="central" {...yTick}>
                    {truncateLabel(row, 18, "ellipsisEnd")}
                  </text>
                )}
                {grid.columns.map((x, c) => {
                  const value = grid.cells[r][c];
                  return (
                    <g key={`${x}-${c}`}>
                      <rect
                        x={labelWidth + c * cellWidth}
                        y={0}
                        width={Math.max(0, cellWidth)}
                        height={Math.max(0, cellHeight)}
                        fill={fill(value)}
                        stroke={SURFACE}
                        strokeWidth={separator}
                        {...(value === null ? {} : clickable(onSelect, { name: row || x, value: x, x, y: String(value) }))}
                      >
                        <title>{`${row ? `${row} · ` : ""}${column(x)}: ${value === null ? "-" : lookNumber(look, value, compact, locale)}`}</title>
                      </rect>
                      {options.dataValuesDisplay === "all" && value !== null && cellWidth >= 24 && cellHeight >= 14 && (
                        <text
                          x={labelWidth + c * cellWidth + cellWidth / 2}
                          y={cellHeight / 2}
                          textAnchor="middle"
                          dominantBaseline="central"
                          fontSize={10}
                          fill={heatShare(value, grid.min, grid.max) > 0.55 ? SURFACE : "var(--text-default-primary)"}
                          pointerEvents="none"
                        >
                          {lookNumber(look, value, compact, locale)}
                        </text>
                      )}
                    </g>
                  );
                })}
              </g>
            );
          })}
          {axisLabelPlaces(
            columnLabels.map((_l, c) => labelWidth + c * cellWidth + cellWidth / 2),
            columnLabels.map((l) => labelPixels(l, xTick.fontSize)),
            labelWidth,
            width - 2,
          ).map(({ index, x }) => (
            <text key={`x-${grid.columns[index]}-${index}`} x={x} y={grid.rows.length * cellHeight + AXIS_HEIGHT / 2 + 2} textAnchor="middle" dominantBaseline="central" {...xTick}>
              {columnLabels[index]}
            </text>
          ))}
        </svg>
      )}
      {legend && (
        <ul className="flex flex-wrap content-center items-center justify-center gap-x-3 content-caption text-default-secondary" style={{ height: legendHeight }}>
          {ranges.map((range, index) => (
            <li key={index} className="flex items-center gap-1.5" data-range={`${range.from}-${range.to}`}>
              <span className="h-2 w-2 shrink-0 rounded-sm" style={{ backgroundColor: range.color }} />
              {`${number(range.from)} - ${number(range.to)}`}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
