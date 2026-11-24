// SPDX-License-Identifier: Apache-2.0

import { lazy, Suspense, useMemo } from "react";
import { AlertTriangle, BarChart3, Bot, Boxes, Info, RefreshCw, Radio, Users } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { NavFamilyHeader } from "@/components/layout/NavFamilyHeader";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorBoundary } from "@/components/ui/error-boundary";
import { PageSkeleton, Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/components/ui/toast";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useGroups } from "@/hooks/api/useGroupApi";
import {
  useRefreshTraceAnalytics,
  useTraceAnalyticsCostByAgent,
  useTraceAnalyticsCostByGroup,
  useTraceAnalyticsCostByModel,
  useTraceAnalyticsCostByUser,
  useTraceAnalyticsCostOverTime,
  useTraceAnalyticsErrors,
  useTraceAnalyticsLatency,
  useTraceAnalyticsSummary,
  useTraceAnalyticsVolumeOverTime,
} from "@/hooks/api/useTraceApi";
import { useAuth } from "@/hooks/useAuth";
import { useCostDataAvailability } from "@/hooks/useCostDataAvailability";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { useTabParam } from "@/hooks/useTabParam";
import { useFormatter } from "@/i18n";
import { ANALYTICS_ERRORS_LIMIT, PLATFORM_DEFAULT_MODEL_FILTER_VALUE } from "@/lib/constants";
import { formatCompactNumber, formatModelLabel, formatShortDate } from "@/lib/utils";
import { AnalyticsFilters, type CsvFilterKey } from "@/pages/analytics/AnalyticsFilters";
import { BreakdownCard } from "@/pages/analytics/BreakdownCard";
import { getStackKeys, pivotCostSeries, pivotVolumeSeries, windowDays } from "@/pages/analytics/chart-data";
import {
  parseBreakdown,
  parsePeriod,
  PREVIOUS_PERIOD_LABELS,
  SOURCE_LABELS,
  sourceLabel,
  type Breakdown,
} from "@/pages/analytics/constants";
import { ErrorsCard } from "@/pages/analytics/ErrorsCard";
import { formatCount, formatLatency, formatUsd } from "@/pages/analytics/format";
import { KpiStrip } from "@/pages/analytics/KpiStrip";
import { OutcomesTab } from "@/pages/analytics/OutcomesTab";
import { RankingCard, type RankingMetric } from "@/pages/analytics/RankingCard";
import { TopAgentStatusBadge, topAgentHref, topAgentLabel } from "@/pages/analytics/top-agents";
import type {
  TraceAnalyticsCostByAgentItem,
  TraceAnalyticsCostByModelItem,
  TraceAnalyticsCostByUserItem,
  TraceAnalyticsFilters,
  TraceAnalyticsPeriod,
} from "@/types";

// recharts is the heavy part of the page: its two cards load on their own.
const UsageOverTimeCard = lazy(() => import("@/pages/analytics/UsageOverTimeCard"));
const LatencyCard = lazy(() => import("@/pages/analytics/LatencyCard"));

/** The metrics the ranking cards switch between, translated by the switch. @i18n-keys */
const METRIC_NAMES = {
  cost: "Cost",
  traces: "Traces",
  tokens: "Tokens",
  latency: "Avg latency",
  perTrace: "Cost / trace",
} as const;

const AGENT_METRICS: RankingMetric<TraceAnalyticsCostByAgentItem>[] = [
  { key: "cost", label: METRIC_NAMES.cost, value: (item) => item.total_cost, format: formatUsd, additive: true, cost: true },
  { key: "traces", label: METRIC_NAMES.traces, value: (item) => item.trace_count, format: formatCount, additive: true },
  { key: "perTrace", label: METRIC_NAMES.perTrace, value: (item) => item.avg_cost_per_trace, format: formatUsd, additive: false, cost: true },
  { key: "latency", label: METRIC_NAMES.latency, value: (item) => item.avg_duration_ms, format: formatLatency, additive: false },
];

const MODEL_METRICS: RankingMetric<TraceAnalyticsCostByModelItem>[] = [
  { key: "cost", label: METRIC_NAMES.cost, value: (item) => item.total_cost, format: formatUsd, additive: true, cost: true },
  { key: "traces", label: METRIC_NAMES.traces, value: (item) => item.trace_count, format: formatCount, additive: true },
  { key: "tokens", label: METRIC_NAMES.tokens, value: (item) => item.input_tokens + item.output_tokens, format: formatCount, additive: true },
];

const PEOPLE_METRICS: RankingMetric<TraceAnalyticsCostByUserItem>[] = [
  { key: "cost", label: METRIC_NAMES.cost, value: (item) => item.total_cost, format: formatUsd, additive: true, cost: true },
  { key: "traces", label: METRIC_NAMES.traces, value: (item) => item.trace_count, format: formatCount, additive: true },
  { key: "tokens", label: METRIC_NAMES.tokens, value: (item) => item.total_tokens, format: formatCount, additive: true },
];

interface SourceTotal {
  source: string;
  count: number;
}
const SOURCE_METRICS: RankingMetric<SourceTotal>[] = [
  { key: "traces", label: METRIC_NAMES.traces, value: (item) => item.count, format: formatCount, additive: true },
];

const ALLOWED_SOURCES = new Set<string>(Object.keys(SOURCE_LABELS));

function parseCsv(value: string | null): string[] {
  if (!value) return [];
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

function formatAggregationAge(
  t_i18n: (id: string, values?: Record<string, string | number>) => string,
  lastAggregationAt: string | null,
): string {
  const lastDate = lastAggregationAt ? new Date(lastAggregationAt) : null;
  if (!lastDate || Number.isNaN(lastDate.getTime())) return t_i18n("last aggregation unavailable");
  const hours = Math.max(0, Math.round((Date.now() - lastDate.getTime()) / 3_600_000));
  return t_i18n("last aggregation: {hours, plural, one {# hour} other {# hours}} ago", { hours });
}

function dailyTotals(rows: { date: string; [key: string]: string | number }[]): number[] {
  const keys = getStackKeys(rows);
  return rows.map((row) => keys.reduce((sum, key) => sum + (Number(row[key]) || 0), 0));
}

type AnalyticsTab = "usage" | "outcomes";

export default function Analytics() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { isAdmin, user } = useAuth();
  const costDataAvailable = useCostDataAvailability();
  /* Render-only decision (which options the picker lists), so the plain hook is
     right here - nothing is fetched or routed off it. */
  const flowBuilderEnabled = useFeatureFlag("FLOW_BUILDER");
  // The evaluation traces stay readable; only the picker drops the source.
  const evaluationOn = useFeatureFlag("EVALUATION");
  const [searchParams, setSearchParams] = useSearchParams();

  // The page opens to administrators and to group managers: a
  // manager reads the members of the groups they manage, all of them unless
  // the Groups picker narrows it - the server enforces the same scope.
  const managedGroups = useMemo(() => user?.managed_groups ?? [], [user?.managed_groups]);
  const managerScope = !isAdmin;
  /* Two URL-backed tabs (#9706, design dev-docs/roi-outcomes.md 6.3): "Model
     usage" (the page as it was) and "Outcomes" (the ROI report, both modes).
     The platform ROI view stays administrators' (D1 = B), so a group manager
     gets no tab bar and always reads Model usage. */
  const [rawTab] = useTabParam<AnalyticsTab>("usage");
  const tab: AnalyticsTab = isAdmin && rawTab === "outcomes" ? "outcomes" : "usage";

  const period = parsePeriod(searchParams.get("period"));
  const dateFrom = searchParams.get("date_from") ?? "";
  const dateTo = searchParams.get("date_to") ?? "";
  const breakdown = parseBreakdown(searchParams.get("breakdown"));
  const selected = useMemo<Record<CsvFilterKey, string[]>>(
    () => ({
      group_ids: parseCsv(searchParams.get("group_ids")),
      agent_ids: parseCsv(searchParams.get("agent_ids")),
      models: parseCsv(searchParams.get("models")),
      sources: parseCsv(searchParams.get("sources")).filter((source) => ALLOWED_SOURCES.has(source)),
    }),
    [searchParams],
  );

  const updateParams = (updates: Record<string, string>) =>
    setSearchParams(
      (previous) => {
        const next = new URLSearchParams(previous);
        for (const [key, value] of Object.entries(updates)) {
          if (value) next.set(key, value);
          else next.delete(key);
        }
        return next;
      },
      { replace: true },
    );
  const toggle = (key: CsvFilterKey, value: string) => {
    const current = selected[key];
    const next = current.includes(value) ? current.filter((item) => item !== value) : [...current, value];
    updateParams({ [key]: next.join(",") });
  };

  const filters = useMemo<TraceAnalyticsFilters>(
    () => ({
      period,
      date_from: period === "custom" ? dateFrom || undefined : undefined,
      date_to: period === "custom" ? dateTo || undefined : undefined,
      agent_ids: selected.agent_ids.length > 0 ? selected.agent_ids : undefined,
      models:
        selected.models.length > 0
          ? selected.models.map((model) => (model === PLATFORM_DEFAULT_MODEL_FILTER_VALUE ? "" : model))
          : undefined,
      sources: selected.sources.length > 0 ? selected.sources : undefined,
      group_ids: selected.group_ids.length > 0 ? selected.group_ids : undefined,
    }),
    [period, dateFrom, dateTo, selected],
  );

  // The Outcomes tab reads none of the trace analytics.
  const ready = tab === "usage" && (period !== "custom" || (!!dateFrom && !!dateTo));
  const days = windowDays(period, dateFrom, dateTo);

  const { data: agents = [] } = useAgents();
  const { data: allGroups = [] } = useGroups();
  const summaryQuery = useTraceAnalyticsSummary(filters, { enabled: ready });
  // The tables' reads keep a failure in their card (own error line + Retry): one
  // failing breakdown never replaces the whole page (UX round 1 on #9434, B2).
  const costByModelQuery = useTraceAnalyticsCostByModel(filters, { enabled: ready, throwOnError: false });
  const costOverTimeQuery = useTraceAnalyticsCostOverTime(filters, { enabled: ready });
  const volumeOverTimeQuery = useTraceAnalyticsVolumeOverTime(filters, { enabled: ready });
  const latencyQuery = useTraceAnalyticsLatency(filters, { enabled: ready });
  const costByAgentQuery = useTraceAnalyticsCostByAgent(filters, { enabled: ready, throwOnError: false });
  const costByUserQuery = useTraceAnalyticsCostByUser(filters, { enabled: ready, throwOnError: false });
  const costByGroupQuery = useTraceAnalyticsCostByGroup(filters, { enabled: ready && breakdown === "groups", throwOnError: false });
  const errorsQuery = useTraceAnalyticsErrors(filters, { enabled: ready, limit: ANALYTICS_ERRORS_LIMIT, throwOnError: false });
  const refreshAnalyticsMutation = useRefreshTraceAnalytics();

  const groupOptions = useMemo(
    () =>
      (managerScope ? managedGroups : allGroups).map((group) => ({ value: group.id, label: group.name })),
    [managerScope, managedGroups, allGroups],
  );

  // A manager may not be able to open the agents their team used: their names
  // come with the usage itself.
  const agentOptions = useMemo(() => {
    const names = new Map<string, string>();
    for (const agent of agents) names.set(agent.id, agent.name);
    for (const item of costByAgentQuery.data?.items ?? []) {
      if (!names.has(item.agent_id)) names.set(item.agent_id, item.agent_name ?? item.agent_id);
    }
    return Array.from(names, ([value, label]) => ({ value, label }));
  }, [agents, costByAgentQuery.data?.items]);

  const modelOptions = useMemo(() => {
    const dataModels = [
      ...(costByModelQuery.data?.items.map((item) => item.model) ?? []),
      ...(costOverTimeQuery.data?.series.map((item) => item.model) ?? []),
    ];
    const explicit = new Set([
      ...dataModels.filter(Boolean),
      ...selected.models.filter((model) => model !== PLATFORM_DEFAULT_MODEL_FILTER_VALUE),
    ]);
    const values = Array.from(explicit).sort((a, b) => a.localeCompare(b));
    // "Platform default" only where it discriminates, or already selected.
    if (selected.models.includes(PLATFORM_DEFAULT_MODEL_FILTER_VALUE) || (dataModels.some((model) => !model) && values.length > 0)) {
      values.push(PLATFORM_DEFAULT_MODEL_FILTER_VALUE);
    }
    return values.map((model) => ({
      value: model,
      label: formatModelLabel(model === PLATFORM_DEFAULT_MODEL_FILTER_VALUE ? "" : model, t_i18n),
    }));
  }, [costByModelQuery.data?.items, costOverTimeQuery.data?.series, selected.models, t_i18n]);

  /* `|| selected...`: a bookmarked `?sources=flow` still filters the charts, so
     the option stays listed while it is on, to be turned off. */
  const sourceOptions = Object.keys(SOURCE_LABELS)
    .filter((source) => source !== "flow" || flowBuilderEnabled || selected.sources.includes(source))
    .filter((source) => source !== "evaluation" || evaluationOn || selected.sources.includes(source))
    .map((source) => ({ value: source, label: t_i18n(sourceLabel(source)) }));

  const sourceTotals = useMemo<SourceTotal[]>(() => {
    const totals = new Map<string, number>();
    for (const point of volumeOverTimeQuery.data?.series ?? []) totals.set(point.source, (totals.get(point.source) ?? 0) + point.count);
    return Array.from(totals, ([source, count]) => ({ source, count }));
  }, [volumeOverTimeQuery.data?.series]);

  const costSpark = useMemo(() => dailyTotals(pivotCostSeries(costOverTimeQuery.data?.series ?? [])), [costOverTimeQuery.data?.series]);
  const traceSpark = useMemo(
    () => dailyTotals(pivotVolumeSeries(volumeOverTimeQuery.data?.series ?? [])),
    [volumeOverTimeQuery.data?.series],
  );

  const clearFilters = () =>
    setSearchParams(breakdown === "agents" ? {} : { breakdown }, { replace: true });

  const handleRefreshAnalytics = () =>
    refreshAnalyticsMutation.mutate(undefined, {
      onSuccess: (result) =>
        toast({ title: t_i18n("Analytics refresh started"), description: result.message, variant: "success" }),
      onError: (error) =>
        toast({ title: t_i18n("Failed to start analytics refresh"), description: error.message, variant: "error" }),
    });

  if (tab === "usage" && summaryQuery.isLoading && !summaryQuery.data) return <PageSkeleton />;

  const summary = summaryQuery.data;
  const hasData = (summary?.trace_count ?? 0) > 0;
  const totalCost = summary?.total_cost ?? 0;
  const scopedGroups = selected.group_ids.length > 0
    ? managedGroups.filter((group) => selected.group_ids.includes(group.id))
    : managedGroups;
  const chartFallback = <Skeleton className="h-[340px] w-full rounded-sm xl:col-span-2" />;

  return (
    <div className="space-y-6">
      {/* The Observability family header (#9301 N3): its h1 and ONE tab row
          (Model usage, Outcomes, Traces, Sessions), the former Model usage /
          Outcomes row flattened into it. A group manager sees Model usage
          alone: the page's own title, no tab row. */}
      <NavFamilyHeader
        to={tab === "outcomes" ? "/observability/outcomes" : "/observability"}
        title="Model usage"
        infoLabel={t_i18n("About analytics")}
        info={managerScope
          ? t_i18n("Monitor the cost, usage, latency and quality of your teams' AI work.")
          : t_i18n("Monitor cost, usage, latency, and quality trends.")}
        actions={isAdmin && tab === "usage" ? (
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={handleRefreshAnalytics}
            disabled={refreshAnalyticsMutation.isPending}
            className="gap-2"
          >
            <RefreshCw className={refreshAnalyticsMutation.isPending ? "h-4 w-4 animate-spin" : "h-4 w-4"} />
            {refreshAnalyticsMutation.isPending ? t_i18n("Refreshing...") : t_i18n("Refresh Analytics")}
          </Button>
        ) : undefined}
      />
      {managerScope && scopedGroups.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 content-compact text-default-secondary">
          <Users className="h-3.5 w-3.5 text-accent" aria-hidden />
          {t_i18n("The members of:")}
          {scopedGroups.map((group) => (
            <Badge key={group.id} variant="secondary" className="content-caption">
              {group.name}
            </Badge>
          ))}
        </div>
      )}

      {tab === "outcomes" ? (
        <OutcomesTab />
      ) : (
      <>
      <AnalyticsFilters
        period={period}
        dateFrom={dateFrom}
        dateTo={dateTo}
        selected={selected}
        groupOptions={groupOptions}
        agentOptions={agentOptions}
        modelOptions={modelOptions}
        sourceOptions={sourceOptions}
        managerScope={managerScope}
        onPeriodChange={(next: TraceAnalyticsPeriod) => updateParams({ period: next === "30d" ? "" : next })}
        onDateFromChange={(value) => updateParams({ date_from: value })}
        onDateToChange={(value) => updateParams({ date_to: value })}
        onToggle={toggle}
        onClear={clearFilters}
      />

      {summary?.aggregation_stale ? (
        <div className="flex items-start gap-2 rounded-sm border border-feedback-alert-primary/30 bg-warning-box p-3 content-compact text-feedback-warning-tertiary">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{t_i18n("Stats may be incomplete: {age}.", { age: formatAggregationAge(t_i18n, summary.last_aggregation_at) })}</span>
        </div>
      ) : null}

      {summary?.window_starts_before_coverage && summary.stats_coverage_start ? (
        <div className="flex items-start gap-2 rounded-sm border border-elevation-subtle-soft bg-elevation-highlight p-3 content-compact text-default-secondary">
          <Info className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            {t_i18n("This range reaches back further than the available data, which starts on {date}. Earlier days are missing, not idle.", {
              date: formatShortDate(summary.stats_coverage_start),
            })}
          </span>
        </div>
      ) : null}

      {!hasData ? (
        <EmptyState
          icon={BarChart3}
          /* FR-027: the copy below talks about filters, which is the
             wrong story on a platform that never produced a trace. */
          requiresAi
          title="No analytics data available"
          description={
            ready
              ? "No traces match the current filters. Adjust the filters or wait for new traces."
              : "Choose both custom start and end dates to load analytics."
          }
        />
      ) : (
        <>
          <KpiStrip
            summary={summary}
            costDataAvailable={costDataAvailable}
            previousLabel={PREVIOUS_PERIOD_LABELS[period]}
            costSpark={costSpark}
            traceSpark={traceSpark}
          />
          {summary && summary.unknown_cost_trace_count > 0 ? (
            <p className="inline-flex items-center gap-1.5 content-compact text-feedback-warning-primary">
              <AlertTriangle className="h-3.5 w-3.5" />
              {t_i18n("{count} traces with unknown cost", { count: formatCompactNumber(summary.unknown_cost_trace_count) })}
            </p>
          ) : null}

          {/* grid-cols-1 caps the single column at the page width (minmax(0, 1fr)): a long chart scrolls inside its card, never the page. */}
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <Suspense fallback={chartFallback}>
              <UsageOverTimeCard
                // A new window reopens the card on its own defaults.
                key={`${period}:${dateFrom}:${dateTo}`}
                costSeries={costOverTimeQuery.data?.series ?? []}
                volumeSeries={volumeOverTimeQuery.data?.series ?? []}
                isLoading={costOverTimeQuery.isLoading || volumeOverTimeQuery.isLoading}
                isFetching={costOverTimeQuery.isFetching || volumeOverTimeQuery.isFetching}
                costDataAvailable={costDataAvailable}
                days={days}
              />
            </Suspense>

            <RankingCard
              title={t_i18n("Top agents")}
              icon={Bot}
              hint={t_i18n("The agents that ran in the window. Click one you can open to go to its page.")}
              items={costByAgentQuery.data?.items ?? []}
              itemKey={(item) => item.agent_id}
              itemLabel={(item) => topAgentLabel(item, t_i18n)}
              itemTitle={(item) => topAgentLabel(item, t_i18n)}
              metrics={AGENT_METRICS}
              costDataAvailable={costDataAvailable}
              isLoading={costByAgentQuery.isLoading}
              isFetching={costByAgentQuery.isFetching}
              emptyLabel={t_i18n("No agent activity in this window")}
              barClassName="bg-feedback-success-primary"
              // A row opens the agent page where the server allows it, never filters.
              itemHref={topAgentHref}
              itemBadge={(item) => <TopAgentStatusBadge item={item} />}
              isItemActive={(item) => selected.agent_ids.includes(item.agent_id)}
            />

            <RankingCard
              title={t_i18n("Top models")}
              icon={Boxes}
              items={costByModelQuery.data?.items ?? []}
              itemKey={(item) => item.model || "__platform_default__"}
              itemLabel={(item) => formatModelLabel(item.model, t_i18n)}
              itemTitle={(item) => formatModelLabel(item.model, t_i18n)}
              metrics={MODEL_METRICS}
              costDataAvailable={costDataAvailable}
              isLoading={costByModelQuery.isLoading}
              isFetching={costByModelQuery.isFetching}
              emptyLabel={t_i18n("No cost-by-model data for this filter set.")}
              barClassName="bg-filigran-ia-primary"
            />

            <RankingCard
              title={managerScope ? t_i18n("Top team members") : t_i18n("Top users")}
              icon={Users}
              hint={t_i18n("The people who ran traces in the window, by the usage they generated.")}
              items={costByUserQuery.data?.items ?? []}
              itemKey={(item) => item.user_id}
              itemLabel={(item) => item.user_name || item.user_email || t_i18n("Deleted user")}
              itemTitle={(item) => item.user_email ?? ""}
              metrics={PEOPLE_METRICS}
              costDataAvailable={costDataAvailable}
              isLoading={costByUserQuery.isLoading}
              isFetching={costByUserQuery.isFetching}
              emptyLabel={t_i18n("No user activity in this window")}
              barClassName="bg-accent"
            />

            <RankingCard
              title={t_i18n("Traces by source")}
              icon={Radio}
              hint={t_i18n("Where the traces of the window came from. Click one to filter the dashboard on it.")}
              items={sourceTotals}
              itemKey={(item) => item.source}
              itemLabel={(item) => t_i18n(sourceLabel(item.source))}
              metrics={SOURCE_METRICS}
              costDataAvailable={costDataAvailable}
              isLoading={volumeOverTimeQuery.isLoading}
              isFetching={volumeOverTimeQuery.isFetching}
              emptyLabel={t_i18n("No activity in this window for these filters.")}
              barClassName="bg-feedback-info-primary"
              onItemClick={(item) => {
                if (ALLOWED_SOURCES.has(item.source)) toggle("sources", item.source);
              }}
              isItemActive={(item) => selected.sources.includes(item.source)}
            />

            <Suspense fallback={chartFallback}>
              <LatencyCard latency={latencyQuery.data} isLoading={latencyQuery.isLoading} isFetching={latencyQuery.isFetching} />
            </Suspense>

            {/* Its own boundary: a render error in one breakdown row stays in this
                card instead of replacing the whole page (16 Nov 2026, users breakdown). */}
            <ErrorBoundary resetKey={breakdown}>
            <BreakdownCard
              breakdown={breakdown}
              onBreakdownChange={(next: Breakdown) => updateParams({ breakdown: next === "agents" ? "" : next })}
              totalCost={totalCost}
              costDataAvailable={costDataAvailable}
              linkEntities={isAdmin}
              agents={{
                items: costByAgentQuery.data?.items ?? [],
                isLoading: costByAgentQuery.isLoading,
                isError: costByAgentQuery.isError,
                onRetry: () => void costByAgentQuery.refetch(),
              }}
              users={{
                data: costByUserQuery.data,
                isLoading: costByUserQuery.isLoading,
                isError: costByUserQuery.isError,
                onRetry: () => void costByUserQuery.refetch(),
              }}
              groups={{
                data: costByGroupQuery.data,
                isLoading: costByGroupQuery.isLoading,
                isError: costByGroupQuery.isError,
                onRetry: () => void costByGroupQuery.refetch(),
              }}
              models={{
                items: costByModelQuery.data?.items ?? [],
                latency: latencyQuery.data?.by_model ?? [],
                isLoading: costByModelQuery.isLoading,
                isError: costByModelQuery.isError,
                onRetry: () => void costByModelQuery.refetch(),
              }}
            />
            </ErrorBoundary>

            <ErrorsCard
              items={errorsQuery.data?.items ?? []}
              isLoading={errorsQuery.isLoading}
              isError={errorsQuery.isError}
              onRetry={() => void errorsQuery.refetch()}
              isFetching={errorsQuery.isFetching}
              errorRate={summary?.error_rate ?? null}
            />
          </div>
        </>
      )}
      </>
      )}
    </div>
  );
}
