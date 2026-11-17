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
 * The Activity tab of a relay group: every request its relays carried, one row
 * each, through the shared data table (#9378, slice 7). The server searches by
 * host, filters by outcome and relay, orders by date and pages; the table's
 * search, filter, sort and page live in the URL (`act_` keys) so reload and
 * back keep the view, the page size and columns per user. The relay is a filter
 * pill of the table like the outcome (T2 of the data tables program, #10977),
 * several relays at once. Metadata only: never a header, a body or a credential.
 */
import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { Icon } from "@filigran/design-system";

import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { TIP_WRAP_FOCUS_RING, Tip } from "@/components/ui/tooltip";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useGroupRelays, useRelayGroupRequests } from "@/hooks/api/useRelayApi";
import { useFormatter } from "@/i18n";
import { readTableState, storedPageSize, type DataTableSort } from "@/lib/data-table-state";
import { exactCount } from "@/lib/number-format";
import { formatDuration } from "@/lib/trace-utils";
import { cn } from "@/lib/utils";
import type { RelayGroup, RelayRequestLog } from "@/types";
import {
  RELAY_OUTCOME_LABELS,
  formatBytes,
  relayErrorLabel,
  relayOutcomeParam,
  relayRequestFailed,
  relaySourceLabel,
  type RelayOutcome,
} from "./relay-labels";

const PAGE_SIZE = 50;

/** The prefix of the table's keys in the URL (search, outcome and relay filters, sort, page). */
const ACTIVITY_URL_KEY = "act_";
/** The page size and hidden columns kept per user. */
const STORAGE_KEY = "relay-group-activity";

/** The one sortable column: the date, which the API orders (server mode). */
const DEFAULT_SORT: DataTableSort = { key: "created_at", direction: "desc" };

/**
 * Column widths in px under the fixed layout, each sized to its text (UX r2 B2
 * on #10752): the request line takes what is left (about 250 px at 1080) and a
 * cut target names itself whole in the shared `TruncatedText` tooltip, so a long
 * URL never scrolls the card sideways. The outcome holds the longest error code
 * ("destination_denied", 123 px) with its icon.
 */
const ACTIVITY_COLUMN_WIDTHS = {
  created_at: 128,
  outcome: 152,
  source: 128,
  relay: 160,
  duration: 88,
  size: 88,
} as const;

export function ActivityTab({ group }: { group: RelayGroup }) {
  const { t_i18n, locale } = useFormatter();
  const [searchParams] = useSearchParams();
  // The size the user kept (DataTable storageKey) is the default the API is asked with.
  const state = readTableState(searchParams, ACTIVITY_URL_KEY, { sort: DEFAULT_SORT, pageSize: storedPageSize(STORAGE_KEY, PAGE_SIZE) });
  const relayIds = state.filters.relay ?? [];
  const direction = state.sort?.key === "created_at" ? state.sort.direction : DEFAULT_SORT.direction;
  const { data: relays } = useGroupRelays(group.id, true);
  // throwOnError false: a failed read shows the table's own error line with
  // Retry, never the page boundary that would replace the header and tabs.
  const { data, isLoading, isError, refetch } = useRelayGroupRequests(
    group.id,
    {
      limit: state.pageSize,
      offset: state.page * state.pageSize,
      outcome: relayOutcomeParam(state.filters.outcome ?? []),
      host: state.search.trim() || undefined,
      relay_id: relayIds.length > 0 ? relayIds : undefined,
      direction,
    },
    { throwOnError: false },
  );
  const relayNames = useMemo(() => new Map((relays ?? []).map((r) => [r.id, r.name])), [relays]);

  const filters = useMemo<DataTableFilter<RelayRequestLog>[]>(
    () => [
      {
        key: "outcome",
        label: "Outcome",
        options: (Object.keys(RELAY_OUTCOME_LABELS) as RelayOutcome[]).map((key) => ({ value: key, label: t_i18n(RELAY_OUTCOME_LABELS[key]) })),
      },
      {
        // The relays of the group, several at once (the API takes a repeated relay_id).
        key: "relay",
        label: "Relay",
        options: (relays ?? []).map((r) => ({ value: r.id, label: r.name })),
      },
    ],
    [relays, t_i18n],
  );

  const columns = useMemo<DataTableColumn<RelayRequestLog>[]>(
    () => [
      {
        key: "created_at",
        header: "When",
        width: ACTIVITY_COLUMN_WIDTHS.created_at,
        className: "whitespace-nowrap text-default-secondary",
        cell: (entry) => <DateTimeCell value={entry.created_at} />,
        csv: (entry) => entry.created_at ?? "",
        sortValue: () => null,
        firstSort: "desc",
      },
      {
        key: "request",
        header: "Request",
        className: "max-w-0 font-mono-plex",
        cell: (entry) => {
          const target = `${entry.scheme}://${entry.host}${entry.port ? `:${entry.port}` : ""}${entry.path}`;
          // The method stays whole; only the target is cut, and only a cut target
          // is a tab stop with its tooltip (UX r2 B1 on #10752).
          return (
            <span className="flex min-w-0 items-center gap-1" data-testid="relay-activity-request">
              <span className="shrink-0 text-accent">{entry.method}</span>{" "}
              <TruncatedText text={target} />
            </span>
          );
        },
        csv: (entry) => `${entry.method} ${entry.scheme}://${entry.host}${entry.port ? `:${entry.port}` : ""}${entry.path}`,
      },
      {
        key: "outcome",
        header: "Outcome",
        width: ACTIVITY_COLUMN_WIDTHS.outcome,
        className: "max-w-0 whitespace-nowrap",
        cell: (entry) => {
          const isFailed = relayRequestFailed(entry);
          const explanation = relayErrorLabel(entry.error_code);
          return (
            <Tip content={explanation ? t_i18n(explanation) : entry.error_code ?? String(entry.status_code ?? "")}>
              <span
                tabIndex={0}
                className={cn(
                  "flex min-w-0 items-center gap-1",
                  TIP_WRAP_FOCUS_RING,
                  isFailed ? "text-feedback-error-primary" : "text-feedback-success-primary",
                )}
              >
                {isFailed ? <Icon name="circle-alert" size={16} className="shrink-0" /> : <Icon name="circle-check" size={16} className="shrink-0" />}
                <span className="truncate">{entry.error_code ?? entry.status_code ?? "-"}</span>
              </span>
            </Tip>
          );
        },
        csv: (entry) => entry.error_code ?? entry.status_code ?? "",
      },
      {
        key: "source",
        header: "Source",
        width: ACTIVITY_COLUMN_WIDTHS.source,
        hideBelow: "3xl",
        hideable: true,
        className: "max-w-0 truncate text-default-secondary",
        cell: (entry) => (entry.source_type ? t_i18n(relaySourceLabel(entry.source_type)) : "-"),
        csv: (entry) => entry.source_type ?? "",
      },
      {
        key: "relay",
        header: "Relay",
        width: ACTIVITY_COLUMN_WIDTHS.relay,
        hideBelow: "4xl",
        hideable: true,
        className: "max-w-0 text-default-secondary",
        cell: (entry) => {
          const name = entry.relay_id ? relayNames.get(entry.relay_id) ?? entry.relay_id.slice(0, 8) : "-";
          return <TruncatedText text={name} />;
        },
        csv: (entry) => (entry.relay_id ? relayNames.get(entry.relay_id) ?? entry.relay_id : ""),
      },
      {
        key: "duration",
        header: "Duration",
        align: "right",
        width: ACTIVITY_COLUMN_WIDTHS.duration,
        className: "tabular-nums text-default-secondary",
        cell: (entry) =>
          entry.duration_ms != null ? (
            <Tip content={`${exactCount(locale, entry.duration_ms)} ms`}>
              <span tabIndex={0} className={TIP_WRAP_FOCUS_RING}>
                {formatDuration(entry.duration_ms)}
              </span>
            </Tip>
          ) : (
            "-"
          ),
        csv: (entry) => entry.duration_ms ?? "",
        csvHeader: "Duration (ms)",
      },
      {
        key: "size",
        header: "Size",
        align: "right",
        width: ACTIVITY_COLUMN_WIDTHS.size,
        className: "tabular-nums text-default-secondary",
        cell: (entry) => formatBytes(entry.bytes_sent + entry.bytes_received),
        csv: (entry) => entry.bytes_sent + entry.bytes_received,
        csvHeader: "Size (bytes)",
      },
    ],
    [locale, relayNames, t_i18n],
  );

  return (
    <div className="space-y-3">
      <p className="content-caption text-default-secondary">
        {t_i18n("Metadata only: no header, body or credential of a relayed request is ever recorded.")}
      </p>
      <DataTable
        rows={data?.items ?? []}
        columns={columns}
        rowKey={(entry) => entry.id}
        server={{ total: data?.total ?? 0 }}
        urlKey={ACTIVITY_URL_KEY}
        storageKey={STORAGE_KEY}
        defaultSort={DEFAULT_SORT}
        defaultPageSize={PAGE_SIZE}
        searchPlaceholder={t_i18n("Filter by host...")}
        filters={filters}
        exportName="relay-group-activity"
        layout="fixed"
        isLoading={isLoading}
        isError={isError}
        onRetry={() => void refetch()}
        emptyLabel={t_i18n("No request has gone through this group yet. Every request a relay carries appears here.")}
        noResultsLabel={t_i18n("No request matches these filters.")}
        rowTestId="relay-activity-row"
      />
    </div>
  );
}
