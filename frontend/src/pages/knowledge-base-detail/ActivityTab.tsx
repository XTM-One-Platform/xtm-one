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
 * The Activity tab of a knowledge base: every sync, document and embedding
 * event, one row each, through the shared data table (#9378, slice 4a part 2).
 * The server searches, filters by event type (sync, documents, embeddings,
 * several at once), orders by time and pages; search, filter, sort and page
 * live in the URL (`act_` keys) so reload and back keep the view, the page
 * size and columns per user.
 */
import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { Icon, type IconName } from "@filigran/design-system";

import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useKBActivity, useKBActivityCount, type KBActivityEventType } from "@/hooks/api/useKnowledgeBaseApi";
import { useFormatter } from "@/i18n";
import { readTableState, storedPageSize, type DataTableSort } from "@/lib/data-table-state";
import { formatCompactNumber, cn } from "@/lib/utils";
import type { KBActivityLog, KnowledgeBase } from "@/types";

import { EVENT_CONFIG, EVENT_TYPE_LABELS, EVENT_TYPES } from "./kb-activity-events";
import { sourceLabel } from "./sync-sources";

const PAGE_SIZE = 25;

/** The prefix of the table's keys in the URL (search, event type filter, sort, page). */
const ACTIVITY_URL_KEY = "act_";
/** The page size and hidden columns kept per user. */
const STORAGE_KEY = "kb-activity";

/** The one sortable column: the time, which the API orders (server mode). */
const DEFAULT_SORT: DataTableSort = { key: "created_at", direction: "desc" };

/** Column widths in px under the fixed layout; the details take what is left. */
const ACTIVITY_COLUMN_WIDTHS = {
  created_at: 128,
  event: 208,
  source: 200,
  documents: 96,
} as const;

/** The event types a URL names, in the filter's order; unknown values dropped. */
function kbActivityEventTypes(values: readonly string[]): KBActivityEventType[] {
  return EVENT_TYPES.filter((type) => values.includes(type));
}

export function ActivityTab({ kb }: { kb: KnowledgeBase }) {
  const { t_i18n } = useFormatter();
  const [searchParams] = useSearchParams();
  // The size the user kept (DataTable storageKey) is the default the API is asked with.
  const state = readTableState(searchParams, ACTIVITY_URL_KEY, { sort: DEFAULT_SORT, pageSize: storedPageSize(STORAGE_KEY, PAGE_SIZE) });
  const eventTypes = kbActivityEventTypes(state.filters.event_type ?? []);
  const direction = state.sort?.key === "created_at" ? state.sort.direction : DEFAULT_SORT.direction;
  const search = state.search.trim() || undefined;

  const { data: logs, isLoading, isError, refetch } = useKBActivity(kb.id, state.pageSize, state.page * state.pageSize, {
    search,
    eventTypes,
    direction,
  });
  const { data: countData } = useKBActivityCount(kb.id, { search, eventTypes });
  // The unfiltered count: a search or an event type filter shows "n of N" on the total chip.
  const { data: allCountData } = useKBActivityCount(kb.id);

  const filters = useMemo<DataTableFilter<KBActivityLog>[]>(
    () => [
      {
        key: "event_type",
        label: "Event type",
        options: EVENT_TYPES.map((type) => ({ value: type, label: t_i18n(EVENT_TYPE_LABELS[type]) })),
      },
    ],
    [t_i18n],
  );

  const columns = useMemo<DataTableColumn<KBActivityLog>[]>(
    () => [
      {
        key: "created_at",
        header: "When",
        width: ACTIVITY_COLUMN_WIDTHS.created_at,
        className: "whitespace-nowrap text-default-secondary",
        cell: (log) => <DateTimeCell value={log.created_at} />,
        csv: (log) => log.created_at ?? "",
        sortValue: () => null,
        firstSort: "desc",
      },
      {
        key: "event",
        header: "Event",
        width: ACTIVITY_COLUMN_WIDTHS.event,
        className: "max-w-0",
        cell: (log) => {
          const config = EVENT_CONFIG[log.event] ?? { icon: "activity" as IconName, color: "text-default-secondary", label: log.event };
          return (
            <span className="flex min-w-0 items-center gap-1.5" data-testid="kb-activity-event">
              <Icon name={config.icon} size={16} className={cn("shrink-0", config.color)} aria-hidden />
              <TruncatedText text={t_i18n(config.label)} />
            </span>
          );
        },
        csv: (log) => log.event,
      },
      {
        key: "details",
        header: "Details",
        className: "max-w-0 text-default-secondary",
        cell: (log) => (log.details ? <TruncatedText text={log.details} /> : "-"),
        csv: (log) => log.details ?? "",
      },
      {
        key: "source",
        header: "Source",
        width: ACTIVITY_COLUMN_WIDTHS.source,
        hideBelow: "3xl",
        hideable: true,
        className: "max-w-0 text-default-secondary",
        cell: (log) => (log.source_type ? <TruncatedText text={t_i18n(sourceLabel(log.source_type))} /> : "-"),
        csv: (log) => log.source_type ?? "",
      },
      {
        key: "documents",
        header: "Documents",
        align: "right",
        width: ACTIVITY_COLUMN_WIDTHS.documents,
        hideable: true,
        className: "tabular-nums text-default-secondary",
        cell: (log) => (log.document_count != null && log.document_count > 0 ? formatCompactNumber(log.document_count) : "-"),
        csv: (log) => log.document_count ?? "",
      },
    ],
    [t_i18n],
  );

  return (
    <DataTable
      rows={logs ?? []}
      columns={columns}
      rowKey={(log) => log.id}
      server={{ total: countData?.count ?? 0 }}
      totalAll={allCountData?.count}
      urlKey={ACTIVITY_URL_KEY}
      storageKey={STORAGE_KEY}
      defaultSort={DEFAULT_SORT}
      defaultPageSize={PAGE_SIZE}
      searchPlaceholder={t_i18n("Search activity...")}
      filters={filters}
      exportName="kb-activity"
      layout="fixed"
      isLoading={isLoading && !logs}
      isError={isError}
      onRetry={() => void refetch()}
      emptyLabel={t_i18n("No activity yet. Every sync, document and embedding event of this knowledge base appears here.")}
      noResultsLabel={t_i18n("No activity matches these filters.")}
      rowTestId="kb-activity-row"
    />
  );
}
