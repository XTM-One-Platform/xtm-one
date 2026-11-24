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

import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { AlertTriangle, Clock3, Coins, Gauge, MessageSquare, Scan, ThumbsDown, ThumbsUp } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { StatCard } from "@/components/ui/stat-card";
import { Badge } from "@/components/ui/badge";
import { MaskedCost } from "@/components/ui/masked-cost";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { CellTipText } from "@/components/ui/cell-tip-text";
import CostOverTimeChart from "@/components/ui/charts/cost-over-time-chart";
import { REFETCH_INTERVAL_SLOW, CHANNEL_TYPE_LABELS } from "@/lib/constants";
import { readTableState, storedPageSize } from "@/lib/data-table-state";
import { exactCompactCost, exactMetricValue, formatCompactCost, formatCompactNumber } from "@/lib/utils";
import { traceStatusLabelKey } from "@/lib/trace-utils";
import { useCostDataAvailability } from "@/hooks/useCostDataAvailability";
import {
  useTraceAnalyticsCostByModel,
  useTraceAnalyticsCostOverTime,
  useTraceAnalyticsSummary,
  useTraces,
} from "@/hooks/api/useTraceApi";
import { useAgentFeedbackComments } from "@/hooks/api/useAgentApi";
import { useFormatter } from "@/i18n";
import type {
  Agent,
  AgentFeedbackCommentListResponse,
  LLMTrace,
  TraceAnalyticsCostByModelItem,
  TraceAnalyticsFilters,
  TraceFilters,
} from "@/types";

const FEEDBACK_PAGE_SIZE = 10;
const FEEDBACK_STORAGE_KEY = "agent-observability-feedback-comments";
const FEEDBACK_URL_KEY = "fc_";

type FeedbackComment = AgentFeedbackCommentListResponse["items"][number];

// Compact from $1,000, no cents from $100 (#10476): the shared rule of lib/number-format.
function formatCurrency(value: number | null): string {
  if (value === null || Number.isNaN(value)) return "—";
  return formatCompactCost(value);
}

/** A compact cost in a table cell, its exact amount on hover once compacted (UX B4 on #10529). */
function CostCell({ value }: { value: number | null }) {
  return <span className="whitespace-nowrap tabular-nums" title={exactCompactCost(value)}>{formatCurrency(value)}</span>;
}

/** A compact count in a table cell, its exact value on hover once compacted. */
function CountCell({ value }: { value: number }) {
  return <span className="whitespace-nowrap tabular-nums" title={exactMetricValue(value)}>{formatCompactNumber(value)}</span>;
}

function formatDuration(value: number | null): string {
  if (value === null || Number.isNaN(value)) return "—";
  if (value < 1000) return `${Math.round(value)}ms`;
  if (value < 60_000) return `${(value / 1000).toFixed(1)}s`;
  return `${(value / 60_000).toFixed(1)}m`;
}

function formatPercent(value: number | null): string {
  if (value === null || Number.isNaN(value)) return "—";
  return `${value.toFixed(1)}%`;
}

function formatScore(value: number | null): string {
  if (value === null || Number.isNaN(value)) return "-";
  return value.toFixed(2);
}

function formatQuotaPerMessage(value: number | null): string {
  if (value === null || Number.isNaN(value)) return "-";
  if (value === 0) return "0";
  if (value < 0.01) return value.toFixed(4);
  return value.toFixed(2);
}

function ratioToPercent(value: number | null): number | null {
  if (value === null || Number.isNaN(value)) return null;
  return value * 100;
}

function avgCostPerTrace(totalCost: number | null, traceCount: number | null): number | null {
  if (totalCost === null || traceCount === null || traceCount <= 0) return null;
  return totalCost / traceCount;
}

/** Recent traces of the agent (the API's last 10): client-side search, filters and sort. */
function useTraceColumns(costDataAvailable: boolean) {
  const { t_i18n } = useFormatter();
  return useMemo<DataTableColumn<LLMTrace>[]>(
    () => [
      {
        key: "started_at",
        header: "Timestamp",
        cell: (trace) => <DateTimeCell value={trace.started_at} />,
        sortValue: (trace) => trace.started_at,
        firstSort: "desc",
      },
      {
        key: "name",
        header: "Trace",
        cell: (trace) => (
          <span className="block max-w-[280px] truncate" title={trace.name}>
            {trace.name}
          </span>
        ),
        sortValue: (trace) => trace.name,
      },
      {
        key: "model",
        hideable: true,
        header: "Model",
        cell: (trace) => <span className="block max-w-[220px] truncate" title={trace.model ?? undefined}>{trace.model}</span>,
        sortValue: (trace) => trace.model,
      },
      {
        key: "duration",
        hideable: true,
        header: "Latency",
        cell: (trace) => <span className="whitespace-nowrap">{formatDuration(trace.duration_ms)}</span>,
        sortValue: (trace) => trace.duration_ms,
        align: "right",
      },
      {
        key: "cost",
        hideable: true,
        header: "Cost",
        cell: (trace) => (costDataAvailable ? <CostCell value={trace.total_cost} /> : <MaskedCost className="inline-flex" />),
        sortValue: (trace) => (costDataAvailable ? trace.total_cost : null),
        align: "right",
      },
      {
        key: "status",
        hideable: true,
        header: "Status",
        cell: (trace) => <Badge variant={trace.status === "ok" ? "secondary" : "destructive"}>{t_i18n(traceStatusLabelKey(trace.status))}</Badge>,
        sortValue: (trace) => trace.status,
      },
    ],
    [costDataAvailable, t_i18n],
  );
}

/** Cost of the agent per model over 7 days (one row per model). */
function useModelColumns(costDataAvailable: boolean) {
  return useMemo<DataTableColumn<TraceAnalyticsCostByModelItem>[]>(
    () => [
      {
        key: "model",
        header: "Model",
        cell: (row) => <span className="block max-w-[280px] truncate" title={row.model}>{row.model}</span>,
        sortValue: (row) => row.model,
      },
      {
        key: "traces",
        hideable: true,
        header: "Traces",
        cell: (row) => <CountCell value={row.trace_count} />,
        sortValue: (row) => row.trace_count,
        align: "right",
      },
      {
        key: "input_tokens",
        hideable: true,
        header: "Input Tokens",
        cell: (row) => <CountCell value={row.input_tokens} />,
        sortValue: (row) => row.input_tokens,
        align: "right",
      },
      {
        key: "output_tokens",
        hideable: true,
        header: "Output Tokens",
        cell: (row) => <CountCell value={row.output_tokens} />,
        sortValue: (row) => row.output_tokens,
        align: "right",
      },
      {
        key: "cost",
        hideable: true,
        header: "Total Cost",
        cell: (row) => (costDataAvailable ? <CostCell value={row.total_cost} /> : <MaskedCost className="inline-flex" />),
        sortValue: (row) => (costDataAvailable ? row.total_cost : null),
        align: "right",
      },
    ],
    [costDataAvailable],
  );
}

/** Feedback comments (server mode): the API orders them newest first, so no column sorts. */
function useFeedbackColumns() {
  const { t_i18n } = useFormatter();
  return useMemo<DataTableColumn<FeedbackComment>[]>(
    () => [
      { key: "updated_at", header: "Date", cell: (item) => <DateTimeCell value={item.updated_at} testId="feedback-date" /> },
      {
        key: "rating",
        hideable: true,
        header: "Rating",
        cell: (item) => (
          // One line in every locale: ja "肯定的" wraps one character per line without it (row 77 px vs 41 px).
          <Badge variant={item.rating === "positive" ? "secondary" : "destructive"} className="whitespace-nowrap">
            {item.rating === "positive" ? t_i18n("Positive") : t_i18n("Negative")}
          </Badge>
        ),
      },
      {
        key: "comment",
        header: "Comment",
        // One line per row: the column takes the free width and cuts the comment there; the full text
        // is in the tooltip, reachable by keyboard (a tab stop unless the row is the one stop).
        className: "w-full max-w-0",
        cell: (item) => <CellTipText text={item.comment} testId="feedback-comment" />,
      },
      { key: "user", hideable: true, header: "User", cell: (item) => <span className="whitespace-nowrap">{item.user_name || item.user_email || "-"}</span> },
      {
        key: "channel",
        hideable: true,
        header: "Channel",
        cell: (item) => <span className="whitespace-nowrap">{t_i18n(CHANNEL_TYPE_LABELS[item.channel] ?? item.channel)}</span>,
      },
    ],
    [t_i18n],
  );
}

export function ObservabilityTab({ agent }: { agent: Agent }) {
  const { t_i18n } = useFormatter();
  const costDataAvailable = useCostDataAvailability();
  const summaryFilters: TraceAnalyticsFilters = { period: "7d", agent_ids: [agent.id] };
  const costFilters: TraceAnalyticsFilters = { period: "30d", agent_ids: [agent.id] };
  const recentTraceFilters: TraceFilters = { agent_id: [agent.id], limit: 10 };

  // Aggregated cost/analytics over 7d/30d windows do not need aggressive
  // refresh; polling on the SLOW interval (instead of STANDARD) halves the
  // number of admin analytics queries that check out a main-pool DB connection
  // while an agent Observability tab is open.
  const summaryQuery = useTraceAnalyticsSummary(summaryFilters, {
    enabled: !!agent.id,
    refetchInterval: REFETCH_INTERVAL_SLOW,
  });
  const costOverTimeQuery = useTraceAnalyticsCostOverTime(costFilters, {
    enabled: !!agent.id,
    refetchInterval: REFETCH_INTERVAL_SLOW,
  });
  // The three tables keep a failed read in their own error line with Retry (throwOnError false): one
  // failing endpoint never replaces the whole tab with the page boundary.
  const costByModelQuery = useTraceAnalyticsCostByModel(summaryFilters, {
    enabled: !!agent.id,
    refetchInterval: REFETCH_INTERVAL_SLOW,
    throwOnError: false,
  });
  const recentTracesQuery = useTraces(recentTraceFilters, {
    enabled: !!agent.id,
    refetchInterval: REFETCH_INTERVAL_SLOW,
    throwOnError: false,
  });
  // The comments table keeps its state in the URL (`fc_` keys): read it here to ask the API for that page.
  const [params] = useSearchParams();
  const feedbackState = readTableState(params, FEEDBACK_URL_KEY, {
    sort: null,
    // The size the user kept (DataTable storageKey) is the default the API is asked with.
    pageSize: storedPageSize(FEEDBACK_STORAGE_KEY, FEEDBACK_PAGE_SIZE),
  });
  const ratingFilter = feedbackState.filters.rating ?? [];
  const feedbackCommentsQuery = useAgentFeedbackComments(
    agent.id,
    {
      limit: feedbackState.pageSize,
      offset: feedbackState.page * feedbackState.pageSize,
      search: feedbackState.search.trim() || undefined,
      // One rating narrows; both (or none) is every comment.
      rating: ratingFilter.length === 1 ? (ratingFilter[0] as FeedbackComment["rating"]) : undefined,
    },
    { refetchInterval: REFETCH_INTERVAL_SLOW, throwOnError: false },
  );
  const traceColumns = useTraceColumns(costDataAvailable);
  const modelColumns = useModelColumns(costDataAvailable);
  const feedbackColumns = useFeedbackColumns();
  const traceFilters = useMemo<DataTableFilter<LLMTrace>[]>(
    () => [
      {
        key: "status",
        label: "Status",
        values: (trace) => [trace.status],
        // The same words as the Status cells ("OK", "Error"), never the raw API value.
        options: [
          { value: "ok", label: t_i18n(traceStatusLabelKey("ok")) },
          { value: "error", label: t_i18n(traceStatusLabelKey("error")) },
        ],
      },
      { key: "model", label: "Model", values: (trace) => [trace.model] },
    ],
    [t_i18n],
  );
  const feedbackFilters = useMemo<DataTableFilter<FeedbackComment>[]>(
    () => [
      {
        key: "rating",
        label: "Rating",
        options: [
          { value: "positive", label: t_i18n("Positive") },
          { value: "negative", label: t_i18n("Negative") },
        ],
      },
    ],
    [t_i18n],
  );

  const summary = summaryQuery.data;
  const traces = recentTracesQuery.data?.items ?? [];
  const modelRows = costByModelQuery.data?.items ?? [];
  const feedbackComments = feedbackCommentsQuery.data?.items ?? [];
  const feedbackCommentsTotal = feedbackCommentsQuery.data?.total ?? 0;
  const feedbackPercent = ratioToPercent(summary?.avg_feedback_score ?? null);
  const hasFeedback = feedbackPercent !== null;
  const positiveFeedback = hasFeedback ? Math.max(0, Math.min(100, feedbackPercent)) : 0;
  const negativeFeedback = hasFeedback ? 100 - positiveFeedback : 0;

  const isInitialLoading =
    summaryQuery.isLoading &&
    costOverTimeQuery.isLoading &&
    costByModelQuery.isLoading &&
    recentTracesQuery.isLoading;

  if (isInitialLoading && !summary && traces.length === 0) {
    return (
      <div className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
          {Array.from({ length: 7 }).map((_, index) => (
            <Skeleton key={index} className="h-[110px] w-full rounded-sm" />
          ))}
        </div>
        <Skeleton className="h-[280px] w-full rounded-sm" />
        <Skeleton className="h-[240px] w-full rounded-sm" />
      </div>
    );
  }

  if (summaryQuery.isError && !summaryQuery.isLoading && !summary) {
    return (
      <EmptyState
        icon={AlertTriangle}
        title="Observability data is unavailable"
        description="The metrics request failed. Try refreshing or adjusting filters."
      />
    );
  }

  return (
    <div className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
        <StatCard
          icon={Scan}
          label="Traces (7d)"
          value={summary ? formatCompactNumber(summary.trace_count) : "—"}
          exact={summary ? exactMetricValue(summary.trace_count) : undefined}
          subtitle="Last 7 days"
        />
        <StatCard
          icon={Coins}
          label="Avg Cost / Trace"
          value={costDataAvailable ? formatCurrency(avgCostPerTrace(summary?.total_cost ?? null, summary?.trace_count ?? null)) : <MaskedCost className="inline-flex" />}
          exact={costDataAvailable ? exactCompactCost(avgCostPerTrace(summary?.total_cost ?? null, summary?.trace_count ?? null)) : undefined}
          subtitle="Last 7 days"
        />
        <StatCard
          icon={Clock3}
          label="Avg Latency"
          value={formatDuration(summary?.avg_duration_ms ?? null)}
          subtitle="Last 7 days"
        />
        <StatCard
          icon={AlertTriangle}
          label="Error Rate"
          value={formatPercent(summary?.error_rate ?? null)}
          subtitle="Last 7 days"
        />
        <StatCard
          icon={Gauge}
          label="Avg Feedback (0-1)"
          value={formatScore(summary?.avg_feedback_score ?? null)}
          subtitle="Last 7 days"
        />
        <StatCard
          icon={Coins}
          label="Quota Consumed"
          value={summary ? formatCompactNumber(summary.quota_consumed) : "—"}
          exact={summary ? exactMetricValue(summary.quota_consumed) : undefined}
          subtitle="All time"
        />
        <StatCard
          icon={MessageSquare}
          label="Avg Quota / Message"
          value={formatQuotaPerMessage(summary?.avg_quota_per_message ?? null)}
          subtitle="All time"
        />
      </div>

      <section className="space-y-2">
        <h3 className="content-base font-medium text-default-secondary">{t_i18n("Cost over time (30d)")}</h3>
        <CostOverTimeChart isLoading={costOverTimeQuery.isLoading} series={costOverTimeQuery.data?.series ?? []} costDataAvailable={costDataAvailable} />
      </section>

      <section className="space-y-2">
        <h3 className="content-base font-medium text-default-secondary">{t_i18n("Recent traces")}</h3>
        <DataTable
          rows={traces}
          columns={traceColumns}
          rowKey={(trace) => trace.id}
          defaultSort={{ key: "started_at", direction: "desc" }}
          searchFields={(trace) => [trace.name, trace.model]}
          searchPlaceholder={t_i18n("Search trace name...")}
          filters={traceFilters}
          urlKey="rt_"
          storageKey="agent-observability-recent-traces"
          isLoading={recentTracesQuery.isLoading}
          isError={recentTracesQuery.isError}
          onRetry={() => void recentTracesQuery.refetch()}
          emptyLabel={t_i18n("No recent traces for this agent.")}
          rowHref={(trace) => `/observability/traces/${trace.id}`}
        />
      </section>

      {(modelRows.length > 0 || costByModelQuery.isError) && (
        <section className="space-y-2">
          <h3 className="content-base font-medium text-default-secondary">{t_i18n("Model breakdown")}</h3>
          <DataTable
            rows={modelRows}
            columns={modelColumns}
            rowKey={(row) => row.model || "__platform_default__"}
            defaultSort={{ key: "cost", direction: "desc" }}
            searchFields={(row) => [row.model]}
            searchPlaceholder={t_i18n("Search models...")}
            noFilters="One row per model of the window: the search is the only narrowing it needs."
            urlKey="mb_"
            storageKey="agent-observability-model-breakdown"
            isError={costByModelQuery.isError}
            onRetry={() => void costByModelQuery.refetch()}
            emptyLabel={t_i18n("No data")}
          />
        </section>
      )}

      {hasFeedback && (
        <section className="space-y-2">
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="content-base">{t_i18n("Feedback distribution")}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              <div className="flex items-center justify-between content-compact text-default-secondary">
                <span className="inline-flex items-center gap-1.5">
                  <ThumbsUp className="h-3.5 w-3.5" /> {positiveFeedback.toFixed(1)}%
                </span>
                <span className="inline-flex items-center gap-1.5">
                  <ThumbsDown className="h-3.5 w-3.5" /> {negativeFeedback.toFixed(1)}%
                </span>
              </div>
              <div className="h-2 w-full overflow-hidden rounded-full bg-elevation-hover">
                <div className="h-full bg-accent" style={{ width: `${positiveFeedback}%` }} />
              </div>
            </CardContent>
          </Card>
        </section>
      )}

      <section className="space-y-2">
        <h3 className="content-base font-medium text-default-secondary">{t_i18n("Feedback comments")}</h3>
        {/* Server mode: the API searches the comment, filters the rating and pages, newest first. */}
        <DataTable
          rows={feedbackComments}
          columns={feedbackColumns}
          rowKey={(item) => item.id}
          server={{ total: feedbackCommentsTotal }}
          searchPlaceholder={t_i18n("Search comments...")}
          filters={feedbackFilters}
          urlKey={FEEDBACK_URL_KEY}
          storageKey={FEEDBACK_STORAGE_KEY}
          defaultPageSize={FEEDBACK_PAGE_SIZE}
          isLoading={feedbackCommentsQuery.isLoading}
          isError={feedbackCommentsQuery.isError}
          onRetry={() => void feedbackCommentsQuery.refetch()}
          emptyLabel={t_i18n("No feedback comments for this agent.")}
        />
      </section>
    </div>
  );
}
