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
 * One value: the single value (trend, sparkline, unit, precision,
 * abbreviation, under-label, dynamic colours), its icon and radial variants,
 * and the filler and marker gauges.
 *
 * Every value and colour is an option, read through the dynamic options
 * (`> sparklineValues | lastPoint()`), with the defaults the data's shape
 * calls for (`withValueDefaults`). A single value's `valueStyle` may lay it out
 * as a difference or a tile instead (`ValueStyles.tsx`).
 */
import type { CSSProperties } from "react";
import { ArrowDown, ArrowUp, Minus } from "lucide-react";

import { DeckIcon } from "@/components/presentation/DeckIcon";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

import { sparklinePoints } from "../spec";
import {
  gaugeShare,
  gaugeTicks,
  percentTrend,
  readGaugeOptions,
  readSingleValueOptions,
  withValueDefaults,
  type GaugeOptions,
  type SingleValueOptions,
} from "./chart-options";
import { clickable, useChartEvents, type ChartProps } from "./common";
import { formatNumber, optionColor, optionValue, optionValues, toNumber, type DosScope } from "./dynamic-options";
import ValueStyleView from "./ValueStyles";
import { readValueStyle } from "../viz/value";

type View = "number" | "icon" | "radial" | "filler" | "marker";

function Sparkline({ values, color, area, label }: { values: number[]; color: string; area: boolean; label: string }) {
  const points = sparklinePoints(values, 320, 56);
  if (points.length === 0) return null;
  const line = points.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  return (
    <svg viewBox="0 0 320 56" preserveAspectRatio="none" className="h-12 w-full min-w-16" role="img" aria-label={label}>
      {area && <polygon points={`${points[0][0]},56 ${line} ${points[points.length - 1][0]},56`} fill={color} fillOpacity={0.2} stroke="none" />}
      <polyline points={line} fill="none" stroke={color} strokeWidth={2.5} strokeLinecap="round" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function Trend({ major, options, scope }: { major: number | null; options: SingleValueOptions; scope: DosScope }) {
  const delta = toNumber(optionValue(options.trendValue, scope));
  if (options.trendDisplay === "off" || delta === null) return null;
  const shown =
    options.trendDisplay === "percent"
      ? major === null
        ? undefined
        : percentTrend(major, delta)
      : delta;
  if (shown === undefined) return null;
  const Icon = delta > 0 ? ArrowUp : delta < 0 ? ArrowDown : Minus;
  const text = formatNumber(Math.abs(shown), {
    precision: options.trendDisplay === "percent" ? 1 : options.numberPrecision,
    abbreviate: options.shouldAbbreviateTrendValue,
    thousandSeparated: options.shouldUseThousandSeparators,
  });
  const color = optionColor(options.trendColor, scope) ?? "var(--text-default-secondary)";
  return (
    <span className="inline-flex items-center gap-1 content-compact font-medium tabular-nums" style={{ color }}>
      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
      {text}
      {options.trendDisplay === "percent" ? "%" : ""}
    </span>
  );
}

function SingleValueView({ view, options, scope, height }: { view: "number" | "icon"; options: SingleValueOptions; scope: DosScope; height: number }) {
  const { t_i18n } = useFormatter();
  const raw = optionValue(options.majorValue, scope);
  const major = toNumber(raw);
  const text =
    major !== null
      ? formatNumber(major, {
          precision: options.numberPrecision,
          abbreviate: options.shouldAbbreviateValue,
          thousandSeparated: options.shouldUseThousandSeparators,
        })
      : raw === null || raw === undefined
        ? "-"
        : String(raw);
  const majorColor = optionColor(options.majorColor, scope) ?? "var(--text-default-primary)";
  const background = optionColor(options.backgroundColor, scope);
  const sparkline = (optionValues(options.sparklineValues, scope) ?? []).map(toNumber).filter((n): n is number => n !== null);
  const showSparkline = options.sparklineDisplay !== "off" && sparkline.length > 1 && view === "number";
  const sparklineColor = optionColor(options.sparklineStrokeColor, scope) ?? "var(--color-accent)";
  const unit = options.unit ? <span className="text-lg font-medium text-default-secondary">{options.unit}</span> : null;
  const value = (
    <span className="inline-flex items-baseline gap-1 text-4xl font-semibold tabular-nums" style={{ color: majorColor }}>
      {options.unitPosition === "before" && unit}
      {text}
      {options.unitPosition === "after" && unit}
    </span>
  );
  const icon =
    view === "icon" && options.icon ? (
      <span className="shrink-0" style={{ color: optionColor(options.iconColor, scope) ?? "var(--color-accent)" }}>
        <DeckIcon name={options.icon} size={options.iconPosition === "above" ? 40 : 32} />
      </span>
    ) : null;
  const line = (
    <Sparkline values={sparkline} color={sparklineColor} area={options.showSparklineAreaGraph} label={t_i18n("Trend over the period")} />
  );
  return (
    <div
      className={cn("flex h-full min-w-0 flex-col justify-center gap-1 rounded-sm", background && "px-4")}
      style={{ minHeight: Math.min(height, 120), backgroundColor: background }}
      data-testid="single-value"
    >
      <div className={cn("flex min-w-0 gap-3", options.iconPosition === "above" ? "flex-col items-start" : "items-center")}>
        {options.iconPosition !== "after" && icon}
        {showSparkline && options.sparklineDisplay === "before" && <div className="w-24 shrink-0">{line}</div>}
        {(view === "number" || options.showValue) && value}
        {showSparkline && options.sparklineDisplay === "after" && <div className="w-24 shrink-0">{line}</div>}
        {options.iconPosition === "after" && icon}
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <Trend major={major} options={options} scope={scope} />
        {options.underLabel && <span className="content-caption text-default-secondary">{options.underLabel}</span>}
      </div>
      {showSparkline && options.sparklineDisplay === "below" && line}
    </div>
  );
}

function RadialView({ options, scope, height }: { options: SingleValueOptions; scope: DosScope; height: number }) {
  const major = toNumber(optionValue(options.majorValue, scope));
  const share = major === null ? 0 : gaugeShare(major, Math.min(0, options.maxValue), options.maxValue);
  const size = Math.max(80, Math.min(height - 24, 200));
  const radius = 42;
  const circumference = 2 * Math.PI * radius;
  const stroke = optionColor(options.radialStrokeColor, scope) ?? "var(--color-accent)";
  const track = optionColor(options.radialBackgroundColor, scope) ?? "var(--border-elevation-subtle)";
  const text = major === null ? "-" : formatNumber(major, { precision: options.numberPrecision, abbreviate: options.shouldAbbreviateValue, thousandSeparated: options.shouldUseThousandSeparators, unit: options.unit, unitPosition: options.unitPosition });
  return (
    <div className="flex h-full flex-col items-center justify-center gap-1" data-testid="single-value-radial">
      <svg viewBox="0 0 100 100" width={size} height={size} role="img" aria-label={text}>
        <circle cx={50} cy={50} r={radius} fill="none" stroke={track} strokeWidth={8} />
        <circle
          cx={50}
          cy={50}
          r={radius}
          fill="none"
          stroke={stroke}
          strokeWidth={8}
          strokeLinecap="round"
          strokeDasharray={`${(share * circumference).toFixed(2)} ${circumference.toFixed(2)}`}
          transform="rotate(-90 50 50)"
        />
        <text x={50} y={54} textAnchor="middle" fontSize={text.length > 7 ? 11 : 15} fontWeight={600} fill={optionColor(options.majorColor, scope) ?? "var(--text-default-primary)"}>
          {text}
        </text>
      </svg>
      <div className="flex flex-wrap items-center justify-center gap-x-3">
        <Trend major={major} options={options} scope={scope} />
        {options.underLabel && <span className="content-caption text-default-secondary">{options.underLabel}</span>}
      </div>
    </div>
  );
}

function gaugeLabel(value: number, options: GaugeOptions, display: GaugeOptions["labelDisplay"]): string {
  if (display === "percentage") return `${Math.round(gaugeShare(value, options.minValue, options.maxValue) * 100)}%`;
  return formatNumber(value, { precision: options.numberPrecision, unit: options.unit });
}

function GaugeView({ view, options, scope, height }: { view: "filler" | "marker"; options: GaugeOptions; scope: DosScope; height: number }) {
  const value = toNumber(optionValue(options.value, scope));
  const share = value === null ? 0 : gaugeShare(value, options.minValue, options.maxValue);
  const vertical = options.orientation === "vertical";
  const ticks = options.labelDisplay === "off" ? [] : gaugeTicks(options.minValue, options.maxValue, options.majorTickInterval);
  const range = value === null ? undefined : options.gaugeRanges.find((r) => value >= r.from && value < r.to) ?? options.gaugeRanges[options.gaugeRanges.length - 1];
  const fill = optionColor(options.gaugeColor, scope) ?? range?.color ?? "var(--color-accent)";
  const at = (n: number) => `${(gaugeShare(n, options.minValue, options.maxValue) * 100).toFixed(2)}%`;
  const shown = value === null || options.valueDisplay === "off" ? null : gaugeLabel(value, options, options.valueDisplay === "percentage" ? "percentage" : "number");
  const track: CSSProperties = vertical ? { width: 20, height: Math.max(80, height - 48) } : { height: 20, width: "100%" };
  const segments =
    view === "marker" && options.gaugeRanges.length
      ? options.gaugeRanges.map((r) => ({ start: at(r.from), size: `${(((r.to - r.from) / (options.maxValue - options.minValue)) * 100).toFixed(2)}%`, color: r.color ?? "var(--border-elevation-subtle)" }))
      : [];
  return (
    <div
      className={cn("flex h-full items-center justify-center gap-4", vertical ? "flex-row" : "flex-col")}
      role="meter"
      aria-valuemin={options.minValue}
      aria-valuemax={options.maxValue}
      aria-valuenow={value ?? undefined}
      data-testid={`gauge-${view}`}
    >
      {shown && (
        <span className="text-3xl font-semibold tabular-nums" style={{ color: view === "filler" ? fill : "var(--text-default-primary)" }}>
          {shown}
        </span>
      )}
      <div className={cn("relative", vertical ? "" : "w-full max-w-md")}>
        <div className="relative overflow-hidden rounded-sm bg-elevation-hover" style={track}>
          {segments.map((s, i) => (
            <span key={i} className="absolute" style={vertical ? { bottom: s.start, height: s.size, left: 0, right: 0, backgroundColor: s.color } : { left: s.start, width: s.size, top: 0, bottom: 0, backgroundColor: s.color }} />
          ))}
          {view === "filler" && (
            <span className="absolute" style={vertical ? { bottom: 0, left: 0, right: 0, height: `${share * 100}%`, backgroundColor: fill } : { left: 0, top: 0, bottom: 0, width: `${share * 100}%`, backgroundColor: fill }} />
          )}
        </div>
        {view === "marker" && value !== null && (
          <span
            className="absolute rounded-sm"
            style={{
              backgroundColor: "var(--text-default-primary)",
              ...(vertical ? { left: -4, right: -4, height: 3, bottom: `calc(${share * 100}% - 1.5px)` } : { top: -4, bottom: -4, width: 3, left: `calc(${share * 100}% - 1.5px)` }),
            }}
            data-testid="gauge-marker"
          />
        )}
        {ticks.length > 0 && (
          <div className={cn("content-caption text-default-secondary", vertical ? "absolute top-0 left-7 h-full" : "relative mt-1 h-4 w-full")}>
            {ticks.map((tick) => (
              <span key={tick} className="absolute whitespace-nowrap tabular-nums" style={vertical ? { bottom: at(tick), transform: "translateY(50%)" } : { left: at(tick), transform: "translateX(-50%)" }}>
                {gaugeLabel(tick, options, options.labelDisplay)}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default function SingleValue({ shape, data, scope, height, view }: ChartProps & { view: View }) {
  const { onSelect } = useChartEvents();
  const options = withValueDefaults(shape, scope.options);
  const withDefaults: DosScope = { ...scope, options };
  const single = readSingleValueOptions(options);
  const style = readValueStyle(options);
  // A tile hands its own value to the interactions.
  if (view === "number" && style === "tile") return <ValueStyleView style="tile" shape={shape} data={data} scope={withDefaults} />;
  const drawn =
    view === "number" && style === "difference" ? (
      <ValueStyleView style="difference" shape={shape} data={data} scope={withDefaults} />
    ) : view === "filler" || view === "marker" ? (
      <GaugeView view={view} options={readGaugeOptions(options)} scope={withDefaults} height={height} />
    ) : view === "radial" ? (
      <RadialView options={single} scope={withDefaults} height={height} />
    ) : (
      <SingleValueView view={view} options={single} scope={withDefaults} height={height} />
    );
  if (!onSelect) return drawn;
  // A click on the value hands it to the dashboard's interactions.
  const value = shape === "number" ? String((data as { value?: unknown })?.value ?? "") : "";
  const click = clickable(onSelect, { name: "value", value });
  return (
    <div {...click} className={cn("h-full", click.className)}>
      {drawn}
    </div>
  );
}
