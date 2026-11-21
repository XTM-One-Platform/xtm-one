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
import { Gauge, MessageSquare, ThumbsDown, ThumbsUp } from "lucide-react";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { StatCard } from "@/components/ui/stat-card";
import { Badge } from "@/components/ui/badge";
import { CellTipText } from "@/components/ui/cell-tip-text";
import { useAgentAnnotationSummary, useAgentAnnotations } from "@/hooks/api/useAnnotationApi";
import {
  ANNOTATION_SOURCE_RATERS,
  ANNOTATION_TARGET_LABELS,
  ANNOTATION_TARGET_TYPES,
  REFETCH_INTERVAL_SLOW,
} from "@/lib/constants";
import { readTableState, storedPageSize, type DataTableSort } from "@/lib/data-table-state";
import { scorePercent } from "@/lib/feedback-criteria";
import { formatCompactNumber } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { Agent, AgentAnnotationItem, AgentAnnotationSummary, AnnotationTargetType, MessageFeedbackRating } from "@/types";
import { FeedbackCriteriaCard } from "./FeedbackCriteriaCard";

// One of the shared page sizes (data-table.tsx PAGE_SIZES), so the page size select shows it.
const RATINGS_PAGE_SIZE = 25;
const RATINGS_STORAGE_KEY = "agent-feedback-ratings";
const RATINGS_URL_KEY = "fb_";
const RATINGS_DEFAULT_SORT: DataTableSort = { key: "updated_at", direction: "desc" };

/** The table's sortable columns and the API's `sort` value for each (server mode: the API orders). */
const RATINGS_SORT_PARAM: Record<string, "date" | "rating" | "kind"> = {
  updated_at: "date",
  rating: "rating",
  target_type: "kind",
};

type KindRow = AgentAnnotationSummary["by_target_type"][number];

/** Where the rated work can be opened from here: only where the API says the viewer may open it. */
function targetLink(item: AgentAnnotationItem, agentId: string): string | null {
  if (!item.can_open) return null;
  if (item.target_type === "message") {
    return item.conversation_id ? `/agents/${agentId}/chats/${item.conversation_id}` : null;
  }
  if (item.target_type === "assignment_run") return `/operations/executions/${item.target_id}`;
  return null;
}

/** Who rated: the XTM One user, or the platform a reaction came from, with its emoji. */
function raterLabel(item: AgentAnnotationItem, t_i18n: (key: string) => string): string {
  // `||`, not `??`: an empty name falls through to the email, then to the platform.
  if (item.user_name || item.user_email) return item.user_name || item.user_email || "-";
  if (item.source in ANNOTATION_SOURCE_RATERS) {
    const platform = t_i18n(ANNOTATION_SOURCE_RATERS[item.source as keyof typeof ANNOTATION_SOURCE_RATERS]);
    return item.emoji ? `${item.emoji} ${platform}` : platform;
  }
  return "-";
}

function formatRatio(ratio: number | null): string {
  return ratio === null ? "-" : `${ratio.toFixed(1)}%`;
}

function formatScore(score: number | null | undefined): string {
  const percent = scorePercent(score);
  return percent === null ? "-" : `${percent}%`;
}

/** One row per kind of rated work (at most five): client-side search and sort. */
function useKindColumns() {
  const { t_i18n } = useFormatter();
  return useMemo<DataTableColumn<KindRow>[]>(
    () => [
      {
        key: "target_type",
        header: "Rated work",
        cell: (row) => <span className="whitespace-nowrap">{t_i18n(ANNOTATION_TARGET_LABELS[row.target_type])}</span>,
        sortValue: (row) => t_i18n(ANNOTATION_TARGET_LABELS[row.target_type]),
        csv: (row) => t_i18n(ANNOTATION_TARGET_LABELS[row.target_type]),
      },
      {
        key: "positive",
        header: "Positive",
        cell: (row) => formatCompactNumber(row.positive),
        sortValue: (row) => row.positive,
        csv: (row) => row.positive,
        align: "right",
        hideable: true,
      },
      {
        key: "negative",
        header: "Negative",
        cell: (row) => formatCompactNumber(row.negative),
        sortValue: (row) => row.negative,
        csv: (row) => row.negative,
        align: "right",
        hideable: true,
      },
      {
        key: "positive_ratio",
        header: "Positive share",
        cell: (row) => formatRatio(row.positive_ratio),
        sortValue: (row) => row.positive_ratio,
        csv: (row) => row.positive_ratio,
        align: "right",
        hideable: true,
      },
    ],
    [t_i18n],
  );
}

/** Every rating (server mode): the API searches the comment, filters, sorts and pages. */
function useRatingColumns() {
  const { t_i18n } = useFormatter();
  return useMemo<DataTableColumn<AgentAnnotationItem>[]>(
    () => [
      {
        key: "updated_at",
        header: "Date",
        cell: (item) => <DateTimeCell value={item.updated_at} testId="feedback-rating-date" />,
        sortValue: () => null,
        firstSort: "desc",
      },
      {
        key: "target_type",
        header: "Rated work",
        // Plain text: the row opens the rated work (rowHref, #12070).
        cell: (item) => <span className="whitespace-nowrap">{t_i18n(ANNOTATION_TARGET_LABELS[item.target_type])}</span>,
        sortValue: () => null,
      },
      {
        key: "rating",
        header: "Rating",
        cell: (item) => (
          <Badge variant={item.rating === "positive" ? "secondary" : "destructive"} className="whitespace-nowrap">
            {item.rating === "positive" ? t_i18n("Positive") : t_i18n("Negative")}
          </Badge>
        ),
        sortValue: () => null,
      },
      {
        key: "weighted_score",
        header: "Weighted score",
        cell: (item) => <span className="whitespace-nowrap">{formatScore(item.weighted_score)}</span>,
        align: "right",
        hideable: true,
      },
      {
        key: "comment",
        header: "Comment",
        // One line per row: the column takes the free width and cuts the comment there; the full
        // text is in the tooltip. The row opens the trace, so the row is the one tab stop.
        className: "w-full max-w-0",
        cell: (item) => (item.comment ? <CellTipText text={item.comment} testId="feedback-rating-comment" /> : <span>-</span>),
      },
      {
        key: "user",
        header: "User",
        cell: (item) => <span className="whitespace-nowrap">{raterLabel(item, t_i18n)}</span>,
        hideable: true,
      },
    ],
    [t_i18n],
  );
}

/** The single selected value of a two-value filter; both or none is no narrowing. */
function single<T extends string>(values: readonly string[] | undefined): T | undefined {
  return values && values.length === 1 ? (values[0] as T) : undefined;
}

/**
 * Every rating of this agent's work: chat messages, assignment runs,
 * background tasks, delegations and agent account answers (reactions in the
 * platform included), for the people who may manage the agent.
 */
export function FeedbackTab({ agent }: { agent: Agent }) {
  const { t_i18n } = useFormatter();
  // The ratings table keeps its state in the URL (`fb_` keys): read it here to ask the API for that page.
  const [params] = useSearchParams();
  const ratingsState = readTableState(params, RATINGS_URL_KEY, {
    sort: RATINGS_DEFAULT_SORT,
    // The size the user kept (DataTable storageKey) is the default the API is asked with.
    pageSize: storedPageSize(RATINGS_STORAGE_KEY, RATINGS_PAGE_SIZE),
  });
  const sort = ratingsState.sort ?? RATINGS_DEFAULT_SORT;

  const summaryQuery = useAgentAnnotationSummary(agent.id, { refetchInterval: REFETCH_INTERVAL_SLOW, throwOnError: false });
  const listQuery = useAgentAnnotations(
    agent.id,
    {
      targetType: (ratingsState.filters.target_type ?? []) as AnnotationTargetType[],
      rating: single<MessageFeedbackRating>(ratingsState.filters.rating),
      search: ratingsState.search.trim() || undefined,
      sort: RATINGS_SORT_PARAM[sort.key] ?? "date",
      direction: sort.direction,
      limit: ratingsState.pageSize,
      offset: ratingsState.page * ratingsState.pageSize,
    },
    { refetchInterval: REFETCH_INTERVAL_SLOW, throwOnError: false },
  );

  const kindColumns = useKindColumns();
  const ratingColumns = useRatingColumns();
  const ratingFilters = useMemo<DataTableFilter<AgentAnnotationItem>[]>(
    () => [
      {
        key: "target_type",
        label: "Rated work",
        options: ANNOTATION_TARGET_TYPES.map((type) => ({ value: type, label: t_i18n(ANNOTATION_TARGET_LABELS[type]) })),
      },
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
  const criteria = summary?.criteria ?? [];
  const scoredByCriteria = criteria.some((criterion) => criterion.enabled && criterion.count > 0);

  return (
    <div className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          icon={MessageSquare}
          label="Ratings"
          value={summary ? formatCompactNumber(summary.total) : "-"}
          subtitle="Across all rated work"
        />
        <StatCard
          icon={ThumbsUp}
          label="Positive"
          value={summary ? formatRatio(summary.positive_ratio) : "-"}
          subtitle={t_i18n("{count} positive", { count: formatCompactNumber(summary?.positive ?? 0) })}
        />
        <StatCard
          icon={ThumbsDown}
          label="Negative"
          value={summary ? formatCompactNumber(summary.negative) : "-"}
          subtitle="Ratings with a thumbs down"
        />
        <StatCard
          icon={Gauge}
          label="Weighted score"
          value={summary ? formatScore(summary.weighted_score) : "-"}
          subtitle={scoredByCriteria ? "From the criteria scores" : "From the thumbs"}
        />
      </div>

      {criteria.length > 0 && <FeedbackCriteriaCard criteria={criteria} />}

      <section className="space-y-2">
        <h3 className="content-base font-medium text-default-secondary">{t_i18n("Feedback by kind of work")}</h3>
        <DataTable
          rows={summary?.by_target_type ?? []}
          columns={kindColumns}
          rowKey={(row) => row.target_type}
          searchFields={(row) => [t_i18n(ANNOTATION_TARGET_LABELS[row.target_type])]}
          searchPlaceholder={t_i18n("Search...")}
          noFilters="One row per kind of rated work, at most five: the search is the only narrowing it needs."
          exportName="feedback-by-kind"
          urlKey="fk_"
          storageKey="agent-feedback-by-kind"
          isLoading={summaryQuery.isLoading}
          isError={summaryQuery.isError}
          onRetry={() => void summaryQuery.refetch()}
          emptyLabel={t_i18n("No feedback for this agent yet.")}
        />
      </section>

      <section className="space-y-2">
        <h3 className="content-base font-medium text-default-secondary">{t_i18n("Ratings")}</h3>
        <DataTable
          rows={listQuery.data?.items ?? []}
          columns={ratingColumns}
          rowKey={(item) => `${item.target_type}:${item.id}`}
          rowHref={(item) => targetLink(item, agent.id) ?? ""}
          server={{ total: listQuery.data?.total ?? 0 }}
          // Every rating of the agent (the summary's count): "n of N" once a filter narrows it (#12039).
          totalAll={summary?.total}
          defaultSort={RATINGS_DEFAULT_SORT}
          searchPlaceholder={t_i18n("Search comments...")}
          filters={ratingFilters}
          urlKey={RATINGS_URL_KEY}
          storageKey={RATINGS_STORAGE_KEY}
          defaultPageSize={RATINGS_PAGE_SIZE}
          isLoading={listQuery.isLoading}
          isError={listQuery.isError}
          onRetry={() => void listQuery.refetch()}
          emptyLabel={t_i18n("No feedback for this agent yet.")}
          rowTestId="feedback-rating-row"
        />
      </section>
    </div>
  );
}
