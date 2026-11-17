// SPDX-License-Identifier: Apache-2.0

import { useMemo } from "react";
import { AlertTriangle } from "lucide-react";
import { ChartCard } from "@/components/ui/charts/chart-card";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { Tip } from "@/components/ui/tooltip";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { useFormatter } from "@/i18n";
import { formatCompactNumber, formatModelLabel } from "@/lib/utils";
import type { TraceAnalyticsErrorItem } from "@/types";

/** Column headers, translated by the table and its CSV. @i18n-keys */
const HEADERS = { error: "Error", count: "Count", agent: "Agent", model: "Model", lastSeen: "Last Seen" } as const;

/**
 * The window's failures, grouped by message, agent and model: sortable
 * on every column, searchable, filtered by agent and model, paged above and
 * below, exported to CSV.
 */
export function ErrorsCard({
  items,
  isLoading,
  isError,
  onRetry,
  isFetching,
  errorRate,
}: {
  items: TraceAnalyticsErrorItem[];
  isLoading: boolean;
  isError: boolean;
  /** Reloads the failed read: the table shows its own error line with Retry. */
  onRetry?: () => void;
  isFetching: boolean;
  /** The window's error rate (0-1), for the card's summary. */
  errorRate: number | null;
}) {
  const { t_i18n } = useFormatter();
  const total = items.reduce((sum, item) => sum + item.count, 0);
  const filters = useMemo<DataTableFilter<TraceAnalyticsErrorItem>[]>(
    () => [
      { key: "agent", label: HEADERS.agent, values: (item) => [item.agent_name] },
      {
        key: "model",
        label: HEADERS.model,
        values: (item) => [item.model === null ? null : formatModelLabel(item.model, t_i18n)],
      },
    ],
    [t_i18n],
  );
  const columns = useMemo<DataTableColumn<TraceAnalyticsErrorItem>[]>(
    () => [
      {
        key: "message",
        header: HEADERS.error,
        // The column takes the width the others leave (`w-full max-w-0` on the cell) and cuts its text there,
        // so the five columns fit the card at 1024 (696 px) without a sideways scroll.
        className: "w-full max-w-0",
        cell: (item) => (
          <Tip content={item.error_message}>
            <span tabIndex={0} data-testid="error-message" className="block truncate rounded-sm font-mono-plex outline-none focus-visible:ring-2 focus-visible:ring-focus">
              {item.error_message}
            </span>
          </Tip>
        ),
        sortValue: (item) => item.error_message,
        csv: (item) => item.error_message,
      },
      {
        key: "count",
        hideable: true,
        header: HEADERS.count,
        cell: (item) => formatCompactNumber(item.count),
        sortValue: (item) => item.count,
        csv: (item) => item.count,
        align: "right",
        className: "font-medium",
      },
      {
        key: "agent",
        hideable: true,
        header: HEADERS.agent,
        cell: (item) => (
          <span className="block max-w-[140px] truncate xl:max-w-[200px]" title={item.agent_name ?? undefined}>
            {item.agent_name ?? "-"}
          </span>
        ),
        sortValue: (item) => item.agent_name,
        csv: (item) => item.agent_name ?? "",
      },
      {
        key: "model",
        hideable: true,
        header: HEADERS.model,
        cell: (item) => {
          const label = item.model === null ? "-" : formatModelLabel(item.model, t_i18n);
          return (
            <span className="block max-w-[140px] truncate xl:max-w-[200px]" title={label}>
              {label}
            </span>
          );
        },
        sortValue: (item) => item.model,
        csv: (item) => (item.model === null ? "" : formatModelLabel(item.model, t_i18n)),
      },
      {
        key: "last_seen",
        hideable: true,
        header: HEADERS.lastSeen,
        cell: (item) => <DateTimeCell value={item.last_seen} testId="error-last-seen" />,
        sortValue: (item) => item.last_seen,
        csv: (item) => item.last_seen ?? "",
        align: "right",
        firstSort: "desc",
      },
    ],
    [t_i18n],
  );

  return (
    <ChartCard
      title={t_i18n("Top errors")}
      icon={AlertTriangle}
      hint={t_i18n("Failed traces of the window, grouped by their message (its first 200 characters), agent and model.")}
      isFetching={isFetching && !isLoading}
      className="xl:col-span-2"
      summary={
        !isLoading && items.length > 0
          ? t_i18n("{count, plural, one {# failure} other {# failures}} in {groups, plural, one {# group} other {# groups}}", {
              count: total,
              groups: items.length,
            }) + (errorRate !== null ? ` - ${t_i18n("{rate} of the traces", { rate: `${(errorRate * 100).toFixed(1)}%` })}` : "")
          : undefined
      }
    >
      <DataTable
        rows={items}
        columns={columns}
        rowKey={(item) => `${item.error_message}\u0000${item.agent_name ?? ""}\u0000${item.model ?? ""}`}
        defaultSort={{ key: "count", direction: "desc" }}
        searchFields={(item) => [item.error_message, item.agent_name, item.model]}
        searchPlaceholder={t_i18n("Search errors, agents or models...")}
        filters={filters}
        urlKey="te_"
        storageKey="analytics-top-errors"
        exportName="llm-errors"
        isLoading={isLoading}
        isError={isError}
        onRetry={onRetry}
        errorLabel={t_i18n("Could not load this breakdown.")}
        emptyLabel={t_i18n("No error records for this filter set.")}
      />
    </ChartCard>
  );
}
