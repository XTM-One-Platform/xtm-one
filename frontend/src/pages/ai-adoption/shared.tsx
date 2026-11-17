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

import React, { useState, memo } from "react";
import { Info } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { AnimatedNumber } from "@/components/ui/animated-number";
import { DiffBadge } from "@/components/ui/diff-badge";
import { Tip } from "@/components/ui/tooltip";
import { CompactFigure } from "@/components/ui/compact-figure";
import { TruncatedText } from "@/components/ui/truncated-text";
import { cn, formatCompactNumber } from "@/lib/utils";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useChartTheme } from "@/hooks/useColorMode";
import { useDashboardStats, useAdoptionTimeSeries } from "@/hooks/api/useMiscApi";
import type { RankedItem, TimeSeriesPoint } from "@/types";

import {
  ACTIVITY_RANGES,
  TS_INTERVALS,
  TS_COLORS,
  OVERVIEW_RANGES,
  fmtChartDate,
  filterByDays,
  useGroupFilter,
  type ActivityRange,
  type BarChartKey,
  type TSInterval,
  type OverviewRange,
  type AdoptionTimeSeries as AdoptionTimeSeriesType,
} from "./shared-utils";
import { Spinner } from "@filigran/design-system";

// ===================================================================
// RankedRow: the one "name, value, bar" row of every ranked card of the AI
// adoption tabs (top users, models, features, IDEs, languages, activity,
// lowest activity). The name is cut on one line inside its card with the full
// name in the tooltip, and the value never shrinks, so a long name never runs
// under the next card (#10396). No card draws its own bar track: the
// RankedRow test fails on an inline copy under pages/ai-adoption.
// ===================================================================
export function RankedRow({ name, value, max, pct, valueLabel, barClassName = "bg-accent", leading, badges }: {
  name: string;
  value: number;
  max: number;
  /** Bar width in percent when it is not value / max (a reversed scale). */
  pct?: number;
  /** Value as shown when it is not the compact number of value. */
  valueLabel?: string;
  barClassName?: string;
  /** Shown before the name (a provider logo). */
  leading?: React.ReactNode;
  /** Shown after the name (provider logos of a user). */
  badges?: React.ReactNode;
}) {
  const width = pct ?? (max > 0 ? (value / max) * 100 : 0);
  return (
    <div className="space-y-1" data-testid="ranked-row">
      <div className="flex min-w-0 items-center justify-between gap-2">
        <div className="flex min-w-0 flex-1 items-center gap-1.5">
          {leading}
          {/* A cut name is a tab stop and shows whole in the DS tooltip, on hover and focus (#11470 UX r1 A2). */}
          <span className="flex min-w-0" data-testid="ranked-row-name" data-truncate="one-line">
            <TruncatedText text={name} className="content-compact font-medium" />
          </span>
          {badges}
        </div>
        <span className="shrink-0 content-compact text-default-secondary tabular-nums" data-testid="ranked-row-value">{valueLabel ?? formatCompactNumber(value)}</span>
      </div>
      <div className="h-1.5 w-full rounded-full bg-elevation-hover"><div className={cn("h-1.5 rounded-full transition-all duration-700", barClassName)} style={{ width: `${width}%` }} /></div>
    </div>
  );
}

// ===================================================================
// HorizontalBarChart component
// ===================================================================
export const HorizontalBarChart = memo(function HorizontalBarChart({ dataKey, icon: Icon, title, color = "bg-accent", nameMapper }: {
  dataKey: BarChartKey; icon: React.ElementType; title: string; color?: string; nameMapper?: Record<string, string>;
}) {
  const [range, setRange] = useState<ActivityRange>("1m");
  const { data: stats, isFetching } = useDashboardStats(range, "platform");
  const data: RankedItem[] = stats?.[dataKey] ?? [];
  const maxValue = Math.max(...data.map((d) => d.value), 1);
  return (
    <Card className="transition-all duration-300 hover:bg-elevation-hover">
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex min-w-0 items-center gap-2 content-base font-semibold"><Icon className="h-4 w-4 text-accent" />{title}</CardTitle>
          <div className="flex flex-wrap items-center gap-1">
            {isFetching && <Spinner size="sm" tone="inherit" className="text-accent" />}
            <div className="flex items-center gap-px rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-px">
              {ACTIVITY_RANGES.map((r) => (
                <button key={r.value} onClick={() => setRange(r.value)} className={cn("px-1.5 py-0.5 content-caption font-medium rounded-[3px] transition-all duration-200 leading-tight", range === r.value ? "bg-accent/20 text-accent shadow-[0_0_6px_color-mix(in_srgb,_var(--color-accent)_calc(0.15_*_100%),_transparent)]" : "text-default-secondary hover:text-default-primary hover:bg-elevation-hover")}>{r.label}</button>
              ))}
            </div>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-2.5">
        {data.length === 0 ? <p className="content-compact text-default-secondary text-center py-4">No data</p> : data.map((item, i) => (
          <RankedRow key={i} name={nameMapper?.[item.name] ?? item.name} value={item.value} max={maxValue} barClassName={color} />
        ))}
      </CardContent>
    </Card>
  );
});

// ===================================================================
// TimeSeriesChart component
// ===================================================================
export const TimeSeriesChart = memo(function TimeSeriesChart({ dataKey, icon: Icon, title, color = "cyan" }: { dataKey: keyof AdoptionTimeSeriesType; icon: React.ElementType; title: string; color?: string; }) {
  const [interval, setI] = useState<TSInterval>("1w");
  const [range, setRange] = useState<OverviewRange>(30);
  const { data, isFetching } = useAdoptionTimeSeries(interval);
  const rawPoints: TimeSeriesPoint[] = data?.[dataKey] ?? [];
  const points = filterByDays(rawPoints, range);
  const W = 560, H = 190, PAD = { top: 12, right: 12, bottom: 34, left: 40 };
  const cW = W - PAD.left - PAD.right, cH = H - PAD.top - PAD.bottom;
  const maxVal = Math.max(...points.map((p) => p.value), 1);
  const gradId = `ts-${dataKey}`, c = TS_COLORS[color] ?? TS_COLORS.cyan;
  const px = (i: number) => PAD.left + (i / Math.max(points.length - 1, 1)) * cW;
  const py = (v: number) => PAD.top + cH - (v / maxVal) * cH;
  const linePath = points.map((p, i) => `${i === 0 ? "M" : "L"} ${px(i)} ${py(p.value)}`).join(" ");
  const areaPath = points.length > 1 ? `${linePath} L ${px(points.length - 1)} ${PAD.top + cH} L ${px(0)} ${PAD.top + cH} Z` : "";
  const yTicks = 4, xStep = Math.max(1, Math.floor(points.length / 6));
  const { gridStroke, tickFill } = useChartTheme();
  return (
    <Card className="transition-all duration-300 hover:bg-elevation-hover">
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex min-w-0 items-center gap-2 content-base font-semibold"><Icon className="h-4 w-4 text-accent" />{title}</CardTitle>
          <div className="flex flex-wrap items-center gap-2">
            {isFetching && <Spinner size="sm" tone="inherit" className="text-accent" />}
            <RangePicker value={range} onChange={setRange} />
            <div className="flex items-center gap-px rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-px">
              {TS_INTERVALS.map((r) => (<button key={r.value} onClick={() => setI(r.value)} className={cn("px-1.5 py-0.5 content-caption font-medium rounded-[3px] transition-all duration-200 leading-tight", interval === r.value ? "bg-accent/20 text-accent" : "text-default-secondary hover:text-default-primary hover:bg-elevation-hover")}>{r.label}</button>))}
            </div>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {points.length === 0 ? <p className="content-compact text-default-secondary text-center py-8">No data</p> : (
          <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto">
            <defs><linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor={c.stop} stopOpacity={0.25} /><stop offset="100%" stopColor={c.stop} stopOpacity={0.02} /></linearGradient></defs>
            {Array.from({ length: yTicks + 1 }, (_, i) => { const val = Math.round(maxVal * (1 - i / yTicks)); const y = PAD.top + (i / yTicks) * cH; return (<g key={i}><line x1={PAD.left} y1={y} x2={PAD.left + cW} y2={y} stroke={gridStroke} /><text x={PAD.left - 5} y={y + 3} textAnchor="end" fontSize={6.5} fill={tickFill}>{formatCompactNumber(val)}</text></g>); })}
            {areaPath && <path d={areaPath} fill={`url(#${gradId})`} />}
            <path d={linePath} fill="none" stroke={c.stroke} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
            {points.map((p, i) => <circle key={i} cx={px(i)} cy={py(p.value)} r={2} fill={c.stroke} />)}
            {points.map((p, i) => i % xStep === 0 || i === points.length - 1 ? <text key={i} x={px(i)} y={H - 16} textAnchor="end" fontSize={6} fill={tickFill} transform={`rotate(-35 ${px(i)} ${H - 16})`}>{fmtChartDate(p.date, interval)}</text> : null)}
          </svg>
        )}
      </CardContent>
    </Card>
  );
});

// ===================================================================
// KpiRangePicker component
// ===================================================================
export function KpiRangePicker({ value, onChange }: { value: ActivityRange; onChange: (v: ActivityRange) => void }) {
  return (
    <div className="inline-flex max-w-full flex-wrap items-center gap-px rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-px">
      {ACTIVITY_RANGES.map((r) => (
        <button key={r.value} onClick={(e) => { e.stopPropagation(); onChange(r.value); }} className={cn("px-1.5 py-0.5 content-caption font-medium rounded-[3px] transition-all duration-200 leading-tight", value === r.value ? "bg-accent/20 text-accent shadow-[0_0_6px_color-mix(in_srgb,_var(--color-accent)_calc(0.15_*_100%),_transparent)]" : "text-default-secondary hover:text-default-primary hover:bg-elevation-hover")}>{r.label}</button>
      ))}
    </div>
  );
}

/** One count of a KPI line: compact, its exact value on hover (#10476, UX round 1 B1/B2). */
export function KpiCount({ value }: { value: number }) {
  const nf = useNumberFormat();
  return <CompactFigure exact={nf.exactCount(value)}>{nf.count(value)}</CompactFigure>;
}

// ===================================================================
// KpiCard component
// ===================================================================
export function KpiCard({ label, value, subtitle, icon: Icon, diff, diffLabel, diffInvert, color, onClick, info, headerRight }: {
  label: string; value: number | string; subtitle?: React.ReactNode; icon: React.ElementType; diff?: number; diffLabel?: string; diffInvert?: boolean; color?: string; onClick?: () => void; info?: React.ReactNode; headerRight?: React.ReactNode;
}) {
  const nf = useNumberFormat();
  // A compact count carries its exact value on hover (#10476); "921" carries none.
  const exact = typeof value === "number" && nf.count(value) !== nf.exactCount(value) ? nf.exactCount(value) : undefined;
  return (
    <Card className={cn("@container transition-all duration-300 hover:bg-elevation-hover relative", onClick && "cursor-pointer")} onClick={onClick}>
      {/* Top-aligned: a WidgetRow stretches this body to the row's tallest card, and labels and values of one line share one top (#11470, UX round 1 B1). */}
      <CardContent className="flex items-start justify-between gap-3 pt-5 pb-4">
        <div className="min-w-0 flex-1">
          {/* One fixed-height line: a card with a diff badge keeps the label row of a card without one, so values of a line share one top; a narrow label is cut with its tooltip, never wrapped (#11470, UX round 2 B1). */}
          <div className="flex h-5 flex-nowrap items-center gap-2" data-testid="kpi-card-label-row">
            <TruncatedText text={label} className="content-caption font-medium text-default-secondary uppercase tracking-wider" />
            {info && <Tip content={info}><Info className="h-3 w-3 shrink-0 text-default-disabled hover:text-default-secondary cursor-help transition-colors" /></Tip>}
            {diff !== undefined && <span className="inline-flex shrink-0"><DiffBadge current={typeof value === "number" ? value : 0} previous={diff} label={diffLabel || "vs last month"} invertColor={diffInvert} /></span>}
          </div>
          {/* The range picker sits on its own row under the label, so label and picker never share a line and no button runs past the card (#9589, UX round 1 B2). */}
          {headerRight && <div className="mt-2 max-w-full">{headerRight}</div>}
          <p className={cn("text-3xl font-bold mt-1 tabular-nums", color)} title={exact} data-testid="kpi-card-value">
            {typeof value === "number" ? <AnimatedNumber value={value} /> : value}
          </p>
          {subtitle && <p className="content-caption text-default-secondary mt-1.5">{subtitle}</p>}
        </div>
        {/* Decorative: a card under 18rem gives its 44 px to the label and the range picker, so neither is cut nor wrapped (#11470 UX r3 A1/A2). */}
        <Icon className="hidden @2xs:block h-8 w-8 text-default-disabled shrink-0 self-center" aria-hidden data-testid="kpi-card-icon" />
      </CardContent>
    </Card>
  );
}

// ===================================================================
// RangePicker component
// ===================================================================
export function RangePicker({ value, onChange }: { value: OverviewRange; onChange: (v: OverviewRange) => void }) {
  return (
    <div className="flex items-center gap-px rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-px">
      {OVERVIEW_RANGES.map((r) => (
        <button key={r.value} onClick={() => onChange(r.value)} className={cn("px-1.5 py-0.5 content-caption font-medium rounded-[3px] transition-all duration-200 leading-tight", value === r.value ? "bg-accent/20 text-accent shadow-[0_0_6px_color-mix(in_srgb,_var(--color-accent)_calc(0.15_*_100%),_transparent)]" : "text-default-secondary hover:text-default-primary hover:bg-elevation-hover")}>{r.label}</button>
      ))}
    </div>
  );
}

// ===================================================================
// GroupFilterWarning component
// ===================================================================
export function GroupFilterWarning() {
  const gf = useGroupFilter();
  if (!gf) return null;
  return <Tip content="This metric is company-wide and not filtered by the selected group"><span className="inline-flex items-center gap-0.5 px-1 py-0.5 rounded-sm text-[9px] font-medium bg-feedback-alert-primary/10 text-feedback-warning-tertiary border border-feedback-alert-primary/20">unfiltered</span></Tip>;
}
