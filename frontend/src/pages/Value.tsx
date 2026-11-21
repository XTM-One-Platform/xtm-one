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
 * The Value page (#9836 R5b, design dev-docs/roi-outcomes.md 10.7): what the
 * agents and fleets delivered, for every user, in both modes. The hero (hours
 * saved, value, AI cost, net value, ROI, payback, each with its range), the
 * review shares, the weekly trend and three tables (by fleet, by agent, by
 * unit of work) that add up to the hero. One `GET /roi` read through the
 * shared hook, so a figure never differs from another screen's for the same
 * scope and window. Reading it never starts a run nor calls a model.
 */
import { useMemo } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import RouteNotFound from "@/pages/RouteNotFound";
import { Bot, Clock3, Layers, Target, TrendingUp, Workflow } from "lucide-react";
import { useIntl } from "react-intl";
import { Area, ComposedChart, CartesianGrid, Legend, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ChartCard, ChartPlaceholder } from "@/components/ui/charts/chart-card";
import { CHART_TOOLTIP_PROPS } from "@/components/ui/charts/chart-tooltip";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { EmptyState } from "@/components/ui/empty-state";
import { EnterpriseCtaButtons } from "@/components/ui/enterprise-upsell";
import { InfoTip } from "@/components/ui/info-tip";
import { ListPageHeader } from "@/components/ui/list-page-header";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { CompactFigure } from "@/components/ui/compact-figure";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { RoiChatSeriesCard } from "@/components/roi/RoiChatSeriesCard";
import { RoiFigure } from "@/components/roi/RoiFigure";
import { RoiEstimatedNote, RoiReportSkeleton } from "@/components/roi/RoiReportParts";
import { useRoiSettings, useValueReport, type RoiReportResponse, type RoiWindow } from "@/hooks/api/useRoiReportApi";
import { RoiExportButtons } from "@/components/roi/RoiExportButtons";
import { useAuth } from "@/hooks/useAuth";
import { useEnterpriseLockedReason } from "@/hooks/useEnterpriseUpsell";
import { useIsXtmOne } from "@/hooks/usePlatformConfig";
import { useTabParam } from "@/hooks/useTabParam";
import { useFormatter } from "@/i18n";
import { outcomeRowPath } from "@/lib/roi-outcomes";
import { operationsPath, workItemsPath } from "@/lib/task-routes";
import {
  VALUE_SCOPE_LABELS,
  VALUE_SCOPE_LINES,
  VALUE_WINDOW_LABELS,
  VALUE_WINDOWS,
  ledgerHeroFigures,
  ledgerTable,
  ledgerTrend,
  parseValueScope,
  parseValueTab,
  parseValueWindow,
  valueCostNote,
  valueIsEmpty,
  valueRemainders,
  valueReviewCounts,
  valueRows,
  valueScopes,
  valueTrend,
  widerValueWindow,
  type ValueRemainder,
  type ValueRow,
  type ValueScope,
  type ValueTab,
} from "@/lib/roi-value";

/** The three tables, each a URL-backed tab (`/tasks/value/:tab?`). */
const VALUE_TABS: { key: ValueTab; label: string; icon: React.ElementType }[] = [
  { key: "fleets", label: "By fleet", icon: Workflow },
  { key: "agents", label: "By agent", icon: Bot },
  { key: "units", label: "By unit of work", icon: Layers },
];

/** Each table keeps its own URL keys (search, sort, filters), so each survives a reload (design 10.7). */
const TABLE_KEYS = { fleets: "fleet_", agents: "agent_", units: "unit_" } as const satisfies Record<ValueTab, string>;

/** The axis labels of the trend, as on the sibling charts (UX round 1 on #10020, B2: 4.5:1 in both themes). */
const TICK = { fill: "var(--text-default-secondary)", fontSize: 12 };

/**
 * A table cell of the Value page: the compact figure (#10476), its exact value on hover. The
 * sort and the CSV read the raw value.
 */
function useValueCells(currency: string) {
  const f = useNumberFormat(currency);
  return useMemo(
    () => ({
      count: (value: number) => <CompactFigure exact={f.exactCount(value)}>{f.count(value)}</CompactFigure>,
      money: (value: number) => <CompactFigure exact={f.exactMoney(value)}>{f.money(value)}</CompactFigure>,
      percent: (rate: number | null) => <CompactFigure exact={f.exactPercent(rate)}>{f.percent(rate)}</CompactFigure>,
    }),
    [f],
  );
}

function useValueColumns(currency: string): DataTableColumn<ValueRow>[] {
  const { t_i18n } = useFormatter();
  const f = useValueCells(currency);
  return useMemo<DataTableColumn<ValueRow>[]>(
    () => [
      {
        key: "name",
        header: "Name",
        cell: (row) => <span className="block max-w-[16rem] truncate font-medium" title={row.name}>{row.name}</span>,
        sortValue: (row) => row.name,
        csv: (row) => row.name,
      },
      { key: "runs", header: "Runs", align: "right", hideBelow: "4xl", hideable: true, cell: (row) => f.count(row.runs), sortValue: (row) => row.runs, csv: (row) => row.runs },
      { key: "units", header: "Units", align: "right", hideBelow: "4xl", hideable: true, cell: (row) => f.count(row.units), sortValue: (row) => row.units, csv: (row) => row.units },
      { key: "hours", header: "Hours saved", align: "right", cell: (row) => f.count(row.hoursSaved), sortValue: (row) => row.hoursSaved, csv: (row) => row.hoursSaved },
      { key: "value", header: "Value", align: "right", cell: (row) => f.money(row.value), sortValue: (row) => row.value, csv: (row) => row.value },
      { key: "cost", header: "AI cost", align: "right", hideBelow: "4xl", hideable: true, cell: (row) => (row.cost === null ? t_i18n("Hidden") : f.money(row.cost)), sortValue: (row) => row.cost ?? -1, csv: (row) => row.cost ?? "" },
      { key: "net", header: "Net value", align: "right", cell: (row) => (row.netValue === null ? t_i18n("Hidden") : f.money(row.netValue)), sortValue: (row) => row.netValue ?? -1, csv: (row) => row.netValue ?? "" },
      { key: "reviewed", header: "Reviewed", align: "right", cell: (row) => f.percent(row.reviewRate), sortValue: (row) => row.reviewRate ?? -1, csv: (row) => row.reviewRate ?? "" },
    ],
    [t_i18n, f],
  );
}

/**
 * The first cell of a remainder row: plain text, no link, its reason in a
 * tooltip. Truncated on one line like the row name cell, the full label in
 * `title`, so the row keeps the table's row height at every width (UX round 2
 * on #10020, B1).
 */
export function RemainderLabel({ remainder }: { remainder: ValueRemainder }) {
  const { t_i18n } = useFormatter();
  const label = t_i18n(remainder.label);
  return (
    <span className="flex min-w-0 max-w-[16rem] items-center gap-1">
      <span className="min-w-0 truncate" title={label} data-truncate="true">
        {label}
      </span>
      <span className="shrink-0">
        <InfoTip label={label} content={t_i18n(remainder.hint)} />
      </span>
    </span>
  );
}

function ValueTable({ report, tab }: { report: RoiReportResponse; tab: ValueTab }) {
  const { t_i18n } = useFormatter();
  const columns = useValueColumns(report.currency);
  // The ledger's breakdowns when the answer carries them (slice L6a), so every tab sums to the hero.
  const { rows, remainders } = useMemo(
    () => (report.figures ? ledgerTable(report.figures, tab) : { rows: valueRows(report, tab), remainders: valueRemainders(report, tab) }),
    [report, tab],
  );
  return (
    <DataTable
      urlKey={TABLE_KEYS[tab]}
      storageKey={`value-${tab}`}
      rows={rows}
      columns={columns}
      rowKey={(row) => `${row.kind}:${row.id}`}
      rowHref={tab === "units" ? undefined : (row) => outcomeRowPath({ kind: row.kind as "fleet" | "agent", id: row.id })}
      defaultSort={{ key: "value", direction: "desc" }}
      searchFields={(row) => [row.name]}
      searchPlaceholder={t_i18n(tab === "fleets" ? "Search fleets..." : tab === "agents" ? "Search agents..." : "Search units of work...")}
      noFilters="One row per fleet, agent or unit of work of the report, searched by name and sorted on every figure."
      remainderRows={remainders.map((r) => ({ key: r.key, row: r.row, label: <RemainderLabel remainder={r} />, testId: `value-remainder-${r.key}` }))}
      emptyLabel={t_i18n("Nothing was counted in this period.")}
      rowTestId={`value-row-${tab}`}
    />
  );
}

/** The weekly value and net value, the value's range shaded (design 10.7, Standards s8: fixed height, no zoom). */
function ValueTrendCard({ report }: { report: RoiReportResponse }) {
  const { t_i18n } = useFormatter();
  const intl = useIntl();
  const f = useNumberFormat(report.currency);
  const data = useMemo(() => (report.figures ? ledgerTrend(report.figures) : valueTrend(report.trend)), [report.figures, report.trend]);
  const week = (value: string) => intl.formatDate(`${value}T00:00:00Z`, { day: "numeric", month: "short", timeZone: "UTC" });
  return (
    <ChartCard title={t_i18n("Value over time")} icon={TrendingUp} hint={t_i18n("Value and net value per week, the shaded band from the low to the high estimate.")}>
      {data.length < 2 ? (
        <ChartPlaceholder className="h-[200px]">{t_i18n("Not enough history yet")}</ChartPlaceholder>
      ) : (
        <div className="h-[200px]" data-testid="value-trend">
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
              <CartesianGrid vertical={false} strokeDasharray="3 3" stroke="var(--border-elevation-subtle)" opacity={0.4} />
              <XAxis dataKey="week" tickFormatter={(value) => week(String(value))} tick={TICK} stroke="var(--border-elevation-subtle)" minTickGap={8} />
              <YAxis tickFormatter={(value) => f.money(Number(value))} tick={TICK} stroke="var(--border-elevation-subtle)" width={72} />
              <Tooltip
                {...CHART_TOOLTIP_PROPS}
                labelFormatter={(value) => week(String(value))}
                formatter={(value, key) => {
                  const name = key === "band" ? t_i18n("Range: low to high estimate") : key === "net" ? t_i18n("Net value") : t_i18n("Value");
                  // The tooltip is where the exact amount is read (#10476); the axis stays compact.
                  const shown = Array.isArray(value)
                    ? t_i18n("{low} to {high}", { low: f.exactMoney(Number(value[0])), high: f.exactMoney(Number(value[1])) })
                    : f.exactMoney(Number(value));
                  return [shown, name];
                }}
              />
              <Legend
                formatter={(key) => (key === "band" ? t_i18n("Range: low to high estimate") : key === "net" ? t_i18n("Net value") : t_i18n("Value"))}
                wrapperStyle={{ fontSize: 12 }}
              />
              <Area dataKey="band" stroke="none" fill="var(--color-accent)" fillOpacity={0.15} isAnimationActive={false} />
              <Line dataKey="value" stroke="var(--color-accent)" strokeWidth={2} dot={false} isAnimationActive={false} />
              {!report.cost_masked && <Line dataKey="net" stroke="var(--text-default-secondary)" strokeWidth={2} strokeDasharray="4 3" dot={false} isAnimationActive={false} />}
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}
    </ChartCard>
  );
}

/** "Reviewed: 148 of 239 runs (62%)", runs and work items apart, each with its list (round 1 B5), from the hero's figures (round 2 B4). */
function ValueReviewLine({ report, linked }: { report: RoiReportResponse; linked: boolean }) {
  const { t_i18n } = useFormatter();
  const f = useNumberFormat(report.currency);
  const shares = valueReviewCounts(report);
  // Nothing reviewed or to review in this scope (a chat-only Mine): no line (UX round 2 B4).
  if (!shares) return null;
  const share = (reviewed: number, total: number) => f.percent(total > 0 ? reviewed / total : null);
  // The counts compact ("0 of 4.2K runs"), the exact ones on hover (#10476).
  const exactLine = (reviewed: number, total: number) => `${f.exactCount(reviewed)} / ${f.exactCount(total)}`;
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 content-compact text-default-secondary" data-testid="value-review-line">
      <span title={exactLine(shares.runs.reviewed, shares.runs.total)}>
        {t_i18n("Reviewed: {reviewed} of {total, plural, one {{count} run} other {{count} runs}} ({share})", {
          reviewed: f.count(shares.runs.reviewed),
          total: shares.runs.total,
          count: f.count(shares.runs.total),
          share: share(shares.runs.reviewed, shares.runs.total),
        })}
        {linked && shares.runs.reviewed < shares.runs.total && (
          <Link to={operationsPath("executions")} className="ml-2 text-accent hover:underline">{t_i18n("Review the rest")}</Link>
        )}
      </span>
      {shares.workItems.total > 0 && (
        <span title={exactLine(shares.workItems.reviewed, shares.workItems.total)}>
          {t_i18n("Work items: {reviewed} of {total} reviewed ({share})", {
            reviewed: f.count(shares.workItems.reviewed),
            total: f.count(shares.workItems.total),
            share: share(shares.workItems.reviewed, shares.workItems.total),
          })}
          {linked && (
            <Link to={workItemsPath()} className="ml-2 text-accent hover:underline">{t_i18n("Open the work items")}</Link>
          )}
        </span>
      )}
      {!linked && <InfoTip label={t_i18n("Why there is no link")} content={t_i18n("Totals of the whole organization; you can open only your own runs.")} />}
    </div>
  );
}

/**
 * Where the AI cost comes from (#10441): the LLM spend of the agents in view,
 * the same figure as LLM Analytics, and the part of it no unit of work carries.
 * Not drawn when costs are hidden or the report reads another source.
 */
export function ValueCostNote({ report }: { report: RoiReportResponse }) {
  const { t_i18n } = useFormatter();
  const f = useNumberFormat(report.currency);
  const note = valueCostNote(report);
  if (!note) return null;
  return (
    <p className="content-compact text-default-secondary" data-testid="value-cost-note">
      {t_i18n("AI cost is the LLM cost in view, the same figure as LLM Analytics, general chat included: By agent shows it under No agent (general chat).")}
      {note.unallocated !== null && (
        <>
          {" "}
          {t_i18n("{amount} of it is not tied to a unit of work and has its own row under By unit of work.", { amount: f.money(note.unallocated) })}
        </>
      )}
    </p>
  );
}

/**
 * The hero: six cards (2 x 3 below 1280 px, 3 x 2 from 1280, six across from 1760), three when costs are masked.
 * It draws `figures.totals` (slice L6a): the same total as the export and every breakdown below (invariant d).
 */
function ValueHero({ report, window }: { report: RoiReportResponse; window: RoiWindow }) {
  const shared = { figures: ledgerHeroFigures(report) ?? report.organization, currency: report.currency, window, costMasked: report.cost_masked, layout: "card" as const };
  if (report.cost_masked) {
    return (
      <div className="grid grid-cols-3 gap-4" data-testid="value-hero">
        <RoiFigure {...shared} metric="hours" />
        <RoiFigure {...shared} metric="value" />
        <RoiFigure {...shared} metric="cost" />
      </div>
    );
  }
  return (
    <div className="grid grid-cols-2 gap-4 min-[1280px]:grid-cols-3 min-[1760px]:grid-cols-6" data-testid="value-hero">
      <RoiFigure {...shared} metric="hours" />
      <RoiFigure {...shared} metric="value" />
      <RoiFigure {...shared} metric="cost" />
      <RoiFigure {...shared} metric="net" />
      <RoiFigure {...shared} metric="roi" />
      <RoiFigure {...shared} metric="payback" />
    </div>
  );
}

/**
 * The Value report, the Value tab of Tasks (#775). `embedded`: the Tasks page
 * draws the page title, so the report draws none of its own.
 */
export default function Value({ embedded = false }: { embedded?: boolean } = {}) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { isAdmin } = useAuth();
  const isXtmOne = useIsXtmOne();
  const locked = useEnterpriseLockedReason();
  const [searchParams, setSearchParams] = useSearchParams();
  const [rawTab] = useTabParam<ValueTab>("fleets");
  const tab = parseValueTab(rawTab);

  const settings = useRoiSettings({ enabled: !locked && !isAdmin });
  const offered = valueScopes(isAdmin, isAdmin || Boolean(settings.data?.roi_org_totals_visible));
  const scope = parseValueScope(searchParams.get("scope"), offered);
  const window = parseValueWindow(searchParams.get("window"));
  const report = useValueReport(scope === "organization" ? "platform" : "mine", window, { enabled: !locked });

  const setParam = (key: "scope" | "window", value: string, fallback: string) =>
    setSearchParams(
      (previous) => {
        const params = new URLSearchParams(previous);
        if (value === fallback) params.delete(key);
        else params.set(key, value);
        return params;
      },
      { replace: true },
    );

  // An unknown or retired table (e.g. `/tasks/value/by-agent`) is not found: no redirect (#797).
  if (rawTab !== tab) return <RouteNotFound />;

  // #10215 slice c23 (criterion 5): the page's help sentence sits in the title's "i", never as a visible line under the h1.
  const title = embedded ? null : (
    <ListPageHeader
      title={
        <span className="flex items-center gap-2">
          <TrendingUp className="h-7 w-7 text-accent" aria-hidden /> {t_i18n("Value")}
        </span>
      }
      infoLabel={t_i18n("About value")}
      info={t_i18n(isXtmOne ? "Value of your exposure management" : "Value of your agents")}
    />
  );

  // Community Edition: the upgrade path in place of the page, never a dead end (b3cd836b).
  if (locked) {
    return (
      <div className="space-y-6">
        {title}
        <EmptyState icon={TrendingUp} title="Value is part of Enterprise Edition." description={locked}>
          <EnterpriseCtaButtons className="justify-center" />
        </EmptyState>
      </div>
    );
  }

  const controls = (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <span className="content-base font-medium" data-testid="value-scope-line">{t_i18n(VALUE_SCOPE_LINES[scope])}</span>
        {offered.scopes.length > 1 && (
          <SegmentedFilter<ValueScope>
            options={offered.scopes.map((key) => ({ key, label: VALUE_SCOPE_LABELS[key] }))}
            value={scope}
            onChange={(next) => setParam("scope", next, offered.fallback)}
            ariaLabel={t_i18n("Scope")}
            selection="radio"
            testId="value-scope"
          />
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <SegmentedFilter<RoiWindow>
          options={VALUE_WINDOWS.map((key) => ({ key, label: VALUE_WINDOW_LABELS[key] }))}
          value={window}
          onChange={(next) => setParam("window", next, "30d")}
          ariaLabel={t_i18n("Window")}
          collapsed
          collapsedClassName="w-[180px]"
          testId="value-window"
        />
        {/* The export of the scope and window shown (slice L6a): its total line is the hero. */}
        <RoiExportButtons window={window} scope={scope === "organization" ? "platform" : "mine"} testId="value-export" />
      </div>
    </div>
  );

  if (report.isLoading) {
    return (
      <div className="space-y-6" aria-busy="true">
        {title}
        {controls}
        <RoiReportSkeleton />
      </div>
    );
  }

  if (report.isError || !report.data) {
    return (
      <div className="space-y-6">
        {title}
        {controls}
        <div role="alert" className="flex flex-wrap items-center gap-3 content-compact text-feedback-error-primary">
          {t_i18n("The value report could not be read.")}
          <Button type="button" variant="outline" size="sm" onClick={() => report.refetch()}>
            {t_i18n("Retry")}
          </Button>
        </div>
      </div>
    );
  }

  const data = report.data;
  // The chat series is its own read, kept apart from the automation figures (#10544 S5).
  const chat = <RoiChatSeriesCard scope={{ kind: scope === "organization" ? "organization" : "mine" }} window={window} urlKey="chat_" />;
  // Empty only when nothing at all was counted: a chat-only scope has no run but draws its hero (UX round 1 B1).
  if (valueIsEmpty(data)) {
    const wider = widerValueWindow(window);
    return (
      <div className="space-y-6">
        {title}
        {controls}
        {chat}
        {wider ? (
          <EmptyState
            icon={Clock3}
            title="Nothing was counted in this period."
            actionLabel="Widen the window"
            actionIcon={Clock3}
            onAction={() => setParam("window", wider, "30d")}
          />
        ) : isAdmin ? (
          <EmptyState
            icon={Target}
            title="No agent is measured yet."
            description="Pick a unit of work on an agent: what it produces, and the minutes a person would spend on it."
            actionLabel="Pick a unit on an agent"
            actionIcon={Target}
            onAction={() => navigate("/agents")}
          />
        ) : (
          <EmptyState
            icon={Target}
            title="No agent is measured yet."
            description="Your administrator or an agent's owner declares what an agent produces."
          />
        )}
      </div>
    );
  }

  // A non-administrator on the organization view reads totals he cannot open: no links (round 1 B5).
  const linked = isAdmin || scope === "mine";
  return (
    <div className="space-y-6">
      {title}
      {controls}
      {data.estimated && <RoiEstimatedNote />}
      <ValueHero report={data} window={window} />
      <ValueReviewLine report={data} linked={linked} />
      <ValueTrendCard report={data} />
      <Card className="space-y-4 p-4">
        <DetailTabs tabs={VALUE_TABS} active={tab} searchFor={(_, current) => (current.toString() ? `?${current.toString()}` : "")} />
        <ValueTable report={data} tab={tab} />
      </Card>
      <ValueCostNote report={data} />
      {chat}
    </div>
  );
}
