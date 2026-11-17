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

import { useMemo, useState } from "react";
import { Bot, UserMinus, Activity, BarChart3, CheckCircle } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { WidgetRow } from "@/components/ui/widget-row";
import { cn, formatCompactNumber } from "@/lib/utils";
import { useChartTheme } from "@/hooks/useColorMode";
import { useAiAdoptionGaps } from "@/hooks/api/useAiAdoptionApi";
import { KpiCard, KpiCount, KpiRangePicker, RangePicker, RankedRow } from "./shared";
import { TS_INTERVALS, filterByDays, fmtChartDate, rangeToDays, rangeDiffLabel, useGroupFilter, type ActivityRange, type OverviewRange, type TSInterval } from "./shared-utils";
import { useFormatter } from "@/i18n";

// ── Shared hook: fetch the agentic gaps data for a given range ─────────────
function useGapsData(range: ActivityRange) {
  const groupId = useGroupFilter();
  const days = rangeToDays(range);
  const { data: gaps, isLoading } = useAiAdoptionGaps(days, groupId);
  return {
    isLoading,
    summary: gaps?.summary,
    prevSummary: gaps?.prev_summary,
    totalPool: gaps?.total_users ?? 0,
    inactive: gaps?.inactive ?? [],
    lowActivity: gaps?.low_activity ?? [],
    trend: gaps?.trend ?? [],
    volume: gaps?.volume ?? [],
    threshold: gaps?.activity_threshold ?? 0,
  };
}

// ===================================================================
// Main tab: agentic AI adoption gaps, one section, no heading
// ===================================================================
export default function GapsTab() {
  return (
    <div className="space-y-4">
      {/* KPIs — each self-contained with its own range picker. Four per line
          from xl only: under it a card is too narrow for its label and its six
          range buttons on one line (#11470 UX r3 A1/A2, 1080 px). */}
      <WidgetRow className="sm:grid-cols-2 xl:grid-cols-4 stagger-children" data-testid="gaps-kpi-row">
        <AdoptionRateKpi />
        <InactiveUsersKpi />
        <LowActivityKpi />
        <AvgActiveDaysKpi />
      </WidgetRow>

      {/* Bottom-row cards — each self-contained */}
      {/* Below xl the inactive users table takes the full row so its toolbar
          and cells keep their width (UX r1 B2 of #10268, 1024 px). Every card of
          the row is as tall as the tallest, the table's pager at its bottom (#11470). */}
      <WidgetRow className="lg:grid-cols-2 xl:grid-cols-[4fr_3fr_2fr]" data-testid="gaps-bottom-row">
        <InactiveUsersCard />
        <LowestActivityCard />
        <AdoptionBreakdownCard />
      </WidgetRow>

      {/* Time series — self-contained with range + interval switchers */}
      <WidgetRow className="lg:grid-cols-2">
        <GapTrendChart gradId="gap-agentic" />
        <GapVolumeChart gradId="gap-agentic-vol" />
      </WidgetRow>
    </div>
  );
}

// ===================================================================
// Self-contained KPI cards (each with own range picker + API call)
// ===================================================================

function AdoptionRateKpi() {
  const [range, setRange] = useState<ActivityRange>("1m");
  const d = useGapsData(range);
  if (d.isLoading || !d.summary) return <KpiCard label="Adoption Rate" value="—" icon={Bot} subtitle="Loading..." />;
  const adoption = d.totalPool > 0 ? Math.round(d.summary.active_count / d.totalPool * 100) : 0;
  const prevAdoption = d.totalPool > 0 && d.prevSummary ? Math.round(d.prevSummary.active_count / d.totalPool * 100) : 0;
  return <KpiCard label="Adoption Rate" value={`${adoption}%`} icon={Bot} diff={range !== "all" ? prevAdoption : undefined} diffLabel={rangeDiffLabel(range)} subtitle={<><KpiCount value={d.summary.active_count} /> of <KpiCount value={d.totalPool} /> active</>} headerRight={<KpiRangePicker value={range} onChange={setRange} />} />;
}

function InactiveUsersKpi() {
  const [range, setRange] = useState<ActivityRange>("1m");
  const d = useGapsData(range);
  if (d.isLoading || !d.summary) return <KpiCard label="Inactive Users" value="—" icon={UserMinus} subtitle="Loading..." />;
  const prevInactive = d.prevSummary?.inactive_count ?? 0;
  return <KpiCard label="Inactive Users" value={d.summary.inactive_count} icon={UserMinus} diff={range !== "all" ? prevInactive : undefined} diffLabel={rangeDiffLabel(range)} diffInvert subtitle={`${Math.round(d.totalPool > 0 ? d.summary.inactive_count / d.totalPool * 100 : 0)}% of pool`} headerRight={<KpiRangePicker value={range} onChange={setRange} />} />;
}

function LowActivityKpi() {
  const [range, setRange] = useState<ActivityRange>("1m");
  const d = useGapsData(range);
  if (d.isLoading || !d.summary) return <KpiCard label="Low Activity" value="—" icon={Activity} subtitle="Loading..." />;
  const prevLow = d.prevSummary?.low_activity_count ?? 0;
  return <KpiCard label="Low Activity" value={d.summary.low_activity_count} icon={Activity} diff={range !== "all" ? prevLow : undefined} diffLabel={rangeDiffLabel(range)} diffInvert subtitle={`< ${d.threshold} interactions`} headerRight={<KpiRangePicker value={range} onChange={setRange} />} />;
}

function AvgActiveDaysKpi() {
  const [range, setRange] = useState<ActivityRange>("1m");
  const d = useGapsData(range);
  if (d.isLoading || !d.summary) return <KpiCard label="Avg Active Days" value="—" icon={BarChart3} subtitle="Loading..." />;
  const prevAvg = d.prevSummary?.avg_active_days ?? 0;
  return <KpiCard label="Avg Active Days" value={d.summary.avg_active_days} icon={BarChart3} diff={range !== "all" ? prevAvg : undefined} diffLabel={rangeDiffLabel(range)} subtitle="per user in period" headerRight={<KpiRangePicker value={range} onChange={setRange} />} />;
}

// ===================================================================
// Self-contained bottom-row cards (each with own range picker)
// ===================================================================

type InactiveUser = { user_id: string; name: string; email: string };

function InactiveUsersCard() {
  const { t_i18n } = useFormatter();
  const [range, setRange] = useState<ActivityRange>("1m");
  const d = useGapsData(range);
  const users = d.inactive;

  const columns = useMemo<DataTableColumn<InactiveUser>[]>(
    () => [
      {
        key: "name",
        header: "Name",
        className: "w-[45%] max-w-0",
        cell: (u) => <span className="block truncate font-medium" title={u.name}>{u.name}</span>,
        sortValue: (u) => u.name.toLowerCase(),
        csv: (u) => u.name,
      },
      {
        key: "email",
        header: "Email",
        className: "max-w-0",
        hideable: true,
        cell: (u) => <span className="block truncate text-default-secondary" title={u.email}>{u.email}</span>,
        sortValue: (u) => u.email.toLowerCase(),
        csv: (u) => u.email,
      },
    ],
    [],
  );

  return (
    <Card className="min-w-0 lg:col-span-2 xl:col-span-1" data-testid="gaps-inactive-users-cell">
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex min-w-0 items-center gap-2 content-base font-semibold">
            <UserMinus className="h-4 w-4 text-feedback-error-primary" />
            {t_i18n("Inactive Users")}
            <Badge variant="outline" className="content-caption ml-1 border-feedback-error-primary/30 text-feedback-error-primary"><KpiCount value={users.length} /></Badge>
          </CardTitle>
          <KpiRangePicker value={range} onChange={setRange} />
        </div>
      </CardHeader>
      <CardContent className="flex flex-col">
        {users.length > 0 ? (
          <DataTable
            fill
            rows={users}
            columns={columns}
            rowKey={(u) => u.user_id}
            defaultSort={{ key: "name", direction: "asc" }}
            searchFields={(u) => [u.name, u.email]}
            searchPlaceholder={t_i18n("Search by name or email...")}
            noFilters="One row per inactive user with a name and an email: the search is the only narrowing it needs."
            exportName="inactive-users"
            urlKey="giua_"
            storageKey="ai-adoption-inactive-users"
            emptyLabel={t_i18n("No users yet.")}
          />
        ) : (
          <div className="text-center py-6">
            <CheckCircle className="h-6 w-6 text-feedback-success-primary/30 mx-auto mb-1" />
            <p className="content-compact text-default-secondary">{t_i18n("No inactive users")}</p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function LowestActivityCard() {
  const [range, setRange] = useState<ActivityRange>("1m");
  const d = useGapsData(range);
  const users = d.lowActivity;
  const maxVal = Math.max(...users.map((u) => u.score), 1);
  return (
    <Card className="transition-all duration-300 hover:bg-elevation-hover">
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex min-w-0 items-center gap-2 content-base font-semibold"><UserMinus className="h-4 w-4 text-feedback-warning-primary" />Lowest Activity<Badge variant="outline" className="content-caption ml-1 border-feedback-alert-primary/30 text-feedback-warning-primary"><KpiCount value={users.length} /></Badge></CardTitle>
          <KpiRangePicker value={range} onChange={setRange} />
        </div>
      </CardHeader>
      <CardContent className="space-y-2.5">
        {users.length > 0 ? users.slice(0, 15).map((u) => {
          const pct = Math.max(10, ((maxVal + 1 - u.score) / maxVal) * 100);
          return (
            <RankedRow key={u.user_id} name={u.name} value={u.score} max={maxVal} pct={pct} valueLabel={String(u.score)} barClassName="bg-feedback-alert-primary" />
          );
        }) : (
          <div className="text-center py-4"><CheckCircle className="h-6 w-6 text-feedback-success-primary/30 mx-auto mb-1" /><p className="content-compact text-default-secondary">No low-activity users</p></div>
        )}
      </CardContent>
    </Card>
  );
}

function AdoptionBreakdownCard() {
  const [range, setRange] = useState<ActivityRange>("1m");
  const d = useGapsData(range);
  const summary = d.summary;
  const totalPool = d.totalPool;
  if (!summary) return null;
  return (
    <Card className="transition-all duration-300 hover:bg-elevation-hover">
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex min-w-0 items-center gap-2 content-base font-semibold"><BarChart3 className="h-4 w-4 text-accent" />Adoption Breakdown</CardTitle>
          <KpiRangePicker value={range} onChange={setRange} />
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {[
          { label: "Active", count: summary.active_count, color: "bg-accent", pct: totalPool > 0 ? summary.active_count / totalPool * 100 : 0 },
          { label: "Low Activity", count: summary.low_activity_count, color: "bg-feedback-alert-primary", pct: totalPool > 0 ? summary.low_activity_count / totalPool * 100 : 0 },
          { label: "Inactive", count: summary.inactive_count, color: "bg-feedback-error-primary", pct: totalPool > 0 ? summary.inactive_count / totalPool * 100 : 0 },
        ].map((s) => (
          <div key={s.label} className="space-y-1">
            <div className="flex items-center justify-between"><span className="content-compact font-medium">{s.label}</span><span className="content-compact text-default-secondary tabular-nums"><KpiCount value={s.count} /> ({Math.round(s.pct)}%)</span></div>
            <div className="h-2 w-full rounded-full bg-elevation-hover"><div className={cn("h-2 rounded-full transition-all duration-700", s.color)} style={{ width: `${s.pct}%` }} /></div>
          </div>
        ))}
        <div className="pt-2 border-t border-elevation-subtle-soft flex justify-between content-compact text-default-secondary">
          <span>Total pool</span><span className="font-medium text-default-primary"><KpiCount value={totalPool} /></span>
        </div>
      </CardContent>
    </Card>
  );
}

// ===================================================================
// Time-series charts (range + interval switchers)
// ===================================================================

function GapTrendChart({ gradId }: { gradId: string }) {
  const [range, setRange] = useState<OverviewRange>(9999);
  const [interval, setI] = useState<TSInterval>("1m");
  const d = useGapsData("1m");
  const points = filterByDays(d.trend, range);
  const { gridStroke, tickFill } = useChartTheme();
  if (d.isLoading || points.length < 2) return <Card className="transition-all duration-300 hover:bg-elevation-hover"><CardHeader><CardTitle className="content-base font-semibold">Active vs Inactive</CardTitle></CardHeader><CardContent><p className="content-compact text-default-secondary text-center py-4">No data</p></CardContent></Card>;
  const W = 560, H = 190, PAD = { top: 12, right: 12, bottom: 34, left: 40 };
  const cW = W - PAD.left - PAD.right, cH = H - PAD.top - PAD.bottom;
  const mx = Math.max(...points.map((p) => p.total_users), 1);
  const px = (i: number) => PAD.left + (i / Math.max(points.length - 1, 1)) * cW;
  const py = (v: number) => PAD.top + cH - (v / mx) * cH;
  const aL = points.map((p, i) => `${i === 0 ? "M" : "L"} ${px(i)} ${py(p.active_users)}`).join(" ");
  const iL = points.map((p, i) => `${i === 0 ? "M" : "L"} ${px(i)} ${py(p.inactive_users)}`).join(" ");
  const aA = `${aL} L ${px(points.length - 1)} ${PAD.top + cH} L ${px(0)} ${PAD.top + cH} Z`;
  const xStep = Math.max(1, Math.floor(points.length / 6));
  return (
    <Card className="transition-all duration-300 hover:bg-elevation-hover">
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex min-w-0 items-center gap-2 content-base font-semibold"><Activity className="h-4 w-4 text-accent" />Active vs Inactive</CardTitle>
          <div className="flex flex-wrap items-center gap-2">
            <RangePicker value={range} onChange={setRange} />
            <div className="flex items-center gap-px rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-px">
              {TS_INTERVALS.map((r) => (<button key={r.value} onClick={() => setI(r.value)} className={cn("px-1.5 py-0.5 content-caption font-medium rounded-[3px] transition-all duration-200 leading-tight", interval === r.value ? "bg-accent/20 text-accent" : "text-default-secondary hover:text-default-primary hover:bg-elevation-hover")}>{r.label}</button>))}
            </div>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <div className="flex items-center gap-4 mb-2">
          <div className="flex items-center gap-1.5"><span className="inline-block h-2 w-2 rounded-full bg-accent" /><span className="content-caption text-default-secondary">Active</span></div>
          <div className="flex items-center gap-1.5"><span className="inline-block h-2 w-2 rounded-full bg-feedback-error-primary" /><span className="content-caption text-default-secondary">Inactive</span></div>
        </div>
        <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto">
          <defs><linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="var(--color-accent)" stopOpacity={0.2} /><stop offset="100%" stopColor="var(--color-accent)" stopOpacity={0.02} /></linearGradient></defs>
          {Array.from({ length: 5 }, (_, i) => { const val = Math.round(mx * (1 - i / 4)); const y = PAD.top + (i / 4) * cH; return (<g key={i}><line x1={PAD.left} y1={y} x2={PAD.left + cW} y2={y} stroke={gridStroke} /><text x={PAD.left - 5} y={y + 3} textAnchor="end" fontSize={6.5} fill={tickFill}>{formatCompactNumber(val)}</text></g>); })}
          <path d={aA} fill={`url(#${gradId})`} />
          <path d={aL} fill="none" stroke="var(--color-accent)" strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
          <path d={iL} fill="none" stroke="var(--color-feedback-error-primary)" strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" strokeDasharray="4 3" />
          {points.map((p, i) => <circle key={`a${i}`} cx={px(i)} cy={py(p.active_users)} r={2.5} fill="var(--color-accent)" />)}
          {points.map((p, i) => <circle key={`i${i}`} cx={px(i)} cy={py(p.inactive_users)} r={2.5} fill="var(--color-feedback-error-primary)" />)}
          {points.map((p, i) => i % xStep === 0 || i === points.length - 1 ? <text key={i} x={px(i)} y={H - 16} textAnchor="end" fontSize={6} fill={tickFill} transform={`rotate(-35 ${px(i)} ${H - 16})`}>{fmtChartDate(p.date, interval)}</text> : null)}
        </svg>
      </CardContent>
    </Card>
  );
}

function GapVolumeChart({ gradId }: { gradId: string }) {
  const [range, setRange] = useState<OverviewRange>(9999);
  const [interval, setI] = useState<TSInterval>("1m");
  const d = useGapsData("1m");
  const points = filterByDays(d.volume, range);
  const { gridStroke, tickFill } = useChartTheme();
  if (d.isLoading || points.length < 2) return <Card className="transition-all duration-300 hover:bg-elevation-hover"><CardHeader><CardTitle className="content-base font-semibold">Activity Volume</CardTitle></CardHeader><CardContent><p className="content-compact text-default-secondary text-center py-4">No data</p></CardContent></Card>;
  const W = 560, H = 190, PAD = { top: 12, right: 12, bottom: 34, left: 40 };
  const cW = W - PAD.left - PAD.right, cH = H - PAD.top - PAD.bottom;
  const mx = Math.max(...points.map((p) => p.value), 1);
  const px = (i: number) => PAD.left + (i / Math.max(points.length - 1, 1)) * cW;
  const py = (v: number) => PAD.top + cH - (v / mx) * cH;
  const line = points.map((p, i) => `${i === 0 ? "M" : "L"} ${px(i)} ${py(p.value)}`).join(" ");
  const area = `${line} L ${px(points.length - 1)} ${PAD.top + cH} L ${px(0)} ${PAD.top + cH} Z`;
  const xStep = Math.max(1, Math.floor(points.length / 6));
  return (
    <Card className="transition-all duration-300 hover:bg-elevation-hover">
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex min-w-0 items-center gap-2 content-base font-semibold"><BarChart3 className="h-4 w-4 text-feedback-success-primary" />Activity Volume</CardTitle>
          <div className="flex flex-wrap items-center gap-2">
            <RangePicker value={range} onChange={setRange} />
            <div className="flex items-center gap-px rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-px">
              {TS_INTERVALS.map((r) => (<button key={r.value} onClick={() => setI(r.value)} className={cn("px-1.5 py-0.5 content-caption font-medium rounded-[3px] transition-all duration-200 leading-tight", interval === r.value ? "bg-accent/20 text-accent" : "text-default-secondary hover:text-default-primary hover:bg-elevation-hover")}>{r.label}</button>))}
            </div>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto">
          <defs><linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="var(--color-feedback-success-primary)" stopOpacity={0.25} /><stop offset="100%" stopColor="var(--color-feedback-success-primary)" stopOpacity={0.02} /></linearGradient></defs>
          {Array.from({ length: 5 }, (_, i) => { const val = Math.round(mx * (1 - i / 4)); const y = PAD.top + (i / 4) * cH; return (<g key={i}><line x1={PAD.left} y1={y} x2={PAD.left + cW} y2={y} stroke={gridStroke} /><text x={PAD.left - 5} y={y + 3} textAnchor="end" fontSize={6.5} fill={tickFill}>{formatCompactNumber(val)}</text></g>); })}
          <path d={area} fill={`url(#${gradId})`} />
          <path d={line} fill="none" stroke="var(--color-feedback-success-primary)" strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
          {points.map((p, i) => <circle key={i} cx={px(i)} cy={py(p.value)} r={2} fill="var(--color-feedback-success-primary)" />)}
          {points.map((p, i) => i % xStep === 0 || i === points.length - 1 ? <text key={i} x={px(i)} y={H - 16} textAnchor="end" fontSize={6} fill={tickFill} transform={`rotate(-35 ${px(i)} ${H - 16})`}>{fmtChartDate(p.date, interval)}</text> : null)}
        </svg>
      </CardContent>
    </Card>
  );
}
