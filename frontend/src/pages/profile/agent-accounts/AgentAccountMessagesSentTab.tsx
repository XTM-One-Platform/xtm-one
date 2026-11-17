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
 * The Messages sent tab of an agent account: every message the
 * AI sent in the account's name - the replies to a message it heard and the
 * messages the agent sent on its own (a chat run, an assignment, a tool call) -
 * on the shared data table: searched, filtered by kind and status, sorted by
 * date or conversation and paged by the server. A row opens in place on the
 * whole message, and a draft waiting for approval is sent or dropped there.
 */
import { useCallback, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ChevronDown, ChevronRight, Send } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  DataTable,
  type DataTableColumn,
  type DataTableFilter,
  type DataTableState,
  type SortDirection,
} from "@/components/ui/data-table";
import { Tip } from "@/components/ui/tooltip";
import { useAgentAccountProvider, useAgentAccountReplyPage } from "@/hooks/api/useAgentAccountApi";
import { useMediaQuery } from "@/hooks/useCompactToolbar";
import { useFormatter } from "@/i18n";
import { formatShortDateTimeIntl, formatTimeAgo } from "@/lib/relative-time";
import { storedPageSize } from "@/lib/data-table-state";
import { toggleArrayItem } from "@/lib/utils";
import type { AgentAccount, AgentAccountReply, AgentAccountSentKind } from "@/types";
import { AgentAccountReplyCard } from "./AgentAccountReplyCard";
import { OUTCOME_LABELS, SENT_KIND_LABELS, SENT_STATUS_LABELS, outcomeTone, sentFilterQuery, wording } from "./agent-accounts";
import { Spinner } from "@filigran/design-system";

const PAGE_SIZE = 25;
const STORAGE_KEY = "agent-account-messages-sent";
const PAGE_SIZES = [10, 25, 50, 100];
/** The columns the server orders the messages by, and the direction a first click sorts. */
const SORT_FIRST = { date: "desc", conversation: "asc" } as const satisfies Record<string, SortDirection>;
type SentSort = keyof typeof SORT_FIRST;
const SORT_KEYS = Object.keys(SORT_FIRST) as SentSort[];
const KINDS = Object.keys(SENT_KIND_LABELS) as AgentAccountSentKind[];
const STATUSES = Object.keys(SENT_STATUS_LABELS) as AgentAccountReply["status"][];

/**
 * Column widths in px under the table's fixed layout; Message takes the rest. Status is wide
 * enough for its longest badge ("Waiting for approval") plus the Proactive send mark, so the
 * status badge is never cut (UX round 2 on #9944, B1).
 */
const COLUMN_WIDTHS = {
  date: 144,
  status: { narrow: 200, wide: 240 },
  conversation: { narrow: 160, wide: 192 },
  rule: 160,
} as const;
/** From xl the Rule column shows; below it the rule is read in the opened row only. */
const RULE_COLUMN_QUERY = "(min-width: 1280px)";

export function AgentAccountMessagesSentTab({ account }: { account: AgentAccount }) {
  const { t_i18n, intl } = useFormatter();
  const kinds = wording(useAgentAccountProvider(account.provider)?.kind).kinds;
  const [params, setParams] = useSearchParams();
  const read = (key: string) => params.get(key) ?? "";
  const list = (key: string) => read(key).split(",").filter(Boolean);
  // `show` is the tab's former one-choice filter: the Decisions tab and older bookmarks still link to it.
  const legacy = sentFilterQuery(read("show"));
  const kindValues = (list("kind").length ? list("kind") : legacy.kind ? [legacy.kind] : []).filter((value) =>
    (KINDS as string[]).includes(value),
  ) as AgentAccountSentKind[];
  const statusValues = (list("status").length ? list("status") : (legacy.status ?? [])).filter((value) =>
    (STATUSES as string[]).includes(value),
  ) as AgentAccountReply["status"][];
  const sortKey = (SORT_KEYS as readonly string[]).includes(read("sort")) ? (read("sort") as SentSort) : "date";
  const direction: SortDirection = read("dir") === "asc" ? "asc" : read("dir") === "desc" ? "desc" : SORT_FIRST[sortKey];
  // No `limit` in the URL: the size the user kept (DataTable storageKey), else the default.
  const defaultPageSize = storedPageSize(STORAGE_KEY, PAGE_SIZE);
  const pageSize = PAGE_SIZES.includes(Number(read("limit"))) ? Number(read("limit")) : defaultPageSize;
  const page = Math.max(0, Number(read("page")) || 0);

  const { data, isLoading, isFetching, isError, refetch } = useAgentAccountReplyPage(
    account.id,
    {
      // Both kinds selected is every message: the API takes one kind or none.
      kind: kindValues.length === 1 ? kindValues[0] : undefined,
      status: statusValues.length ? statusValues : undefined,
      q: read("q") || undefined,
      sort: sortKey,
      direction,
    },
    page,
    pageSize,
  );
  const [open, setOpen] = useState<string[]>([]);
  const toggle = useCallback((id: string) => setOpen((ids) => toggleArrayItem(ids, id)), []);
  const showRule = useMediaQuery(RULE_COLUMN_QUERY, false);

  const tableState: DataTableState = {
    search: read("q"),
    sort: { key: sortKey, direction },
    page,
    pageSize,
    filters: { kind: kindValues, status: statusValues },
  };
  const write = useCallback(
    (changes: Record<string, string>) =>
      setParams(
        (previous) => {
          const next = new URLSearchParams(previous);
          for (const [key, value] of Object.entries(changes)) {
            if (value) next.set(key, value);
            else next.delete(key);
          }
          return next;
        },
        { replace: true },
      ),
    [setParams],
  );
  const onTableState = useCallback(
    (next: DataTableState) => {
      const nextSort = next.sort ?? { key: "date", direction: "desc" as SortDirection };
      // Newest first needs no params: the URL stays as short as it was.
      const isDefault = nextSort.key === "date" && nextSort.direction === "desc";
      write({
        q: next.search,
        show: "",
        kind: (next.filters.kind ?? []).join(","),
        status: (next.filters.status ?? []).join(","),
        sort: isDefault ? "" : nextSort.key,
        dir: isDefault ? "" : nextSort.direction,
        page: next.page > 0 ? String(next.page) : "",
        limit: next.pageSize === defaultPageSize ? "" : String(next.pageSize),
      });
    },
    [write, defaultPageSize],
  );

  const tableFilters = useMemo<DataTableFilter<AgentAccountReply>[]>(
    () => [
      { key: "kind", label: "Kind", options: KINDS.map((key) => ({ value: key, label: t_i18n(SENT_KIND_LABELS[key]) })) },
      {
        key: "status",
        label: "Status",
        options: STATUSES.map((key) => ({ value: key, label: t_i18n(SENT_STATUS_LABELS[key]) })),
      },
    ],
    [t_i18n],
  );

  const columns = useMemo<DataTableColumn<AgentAccountReply>[]>(() => {
    // Server mode: the API sorts, so a sortable column only needs a sortValue to offer its header.
    const sortable = (key: SentSort) => ({ sortValue: () => null, firstSort: SORT_FIRST[key] });
    const drawn: DataTableColumn<AgentAccountReply>[] = [
      {
        key: "date",
        header: "When",
        ...sortable("date"),
        interactive: true,
        width: COLUMN_WIDTHS.date,
        className: "whitespace-nowrap align-top text-default-secondary",
        cell: (reply) => {
          const isOpen = open.includes(reply.id);
          const Chevron = isOpen ? ChevronDown : ChevronRight;
          return (
            <button
              type="button"
              onClick={() => toggle(reply.id)}
              aria-expanded={isOpen}
              aria-label={t_i18n(isOpen ? "Collapse" : "Expand")}
              className="flex items-center gap-1.5 rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-focus"
            >
              <Chevron className="h-3.5 w-3.5 shrink-0" aria-hidden />
              <Tip content={formatShortDateTimeIntl(intl, reply.created_at)}>
                <span>{formatTimeAgo(intl, reply.created_at)}</span>
              </Tip>
            </button>
          );
        },
        csv: (reply) => reply.created_at,
      },
      {
        key: "status",
        header: "Status",
        hideable: true,
        width: showRule ? COLUMN_WIDTHS.status.wide : COLUMN_WIDTHS.status.narrow,
        className: "align-top",
        // One line per row and the status badge is never cut. From xl Proactive send is a badge
        // beside it; below xl it is an icon mark that keeps its name and a tooltip.
        cell: (reply) => {
          const status = t_i18n(OUTCOME_LABELS[reply.status] ?? OUTCOME_LABELS.failed);
          const proactive = t_i18n("Proactive send");
          return (
            <div
              className="flex min-w-0 flex-nowrap items-center gap-1"
              data-testid="sent-status-cell"
              title={reply.kind === "proactive" ? `${status} - ${proactive}` : status}
            >
              <Badge variant={outcomeTone(reply.status)} className="shrink-0 whitespace-nowrap">
                {status}
              </Badge>
              {reply.kind === "proactive" &&
                (showRule ? (
                  <Badge variant="outline" className="min-w-0 whitespace-nowrap content-caption">
                    <span className="truncate">{proactive}</span>
                  </Badge>
                ) : (
                  <Tip content={proactive}>
                    <span role="img" aria-label={proactive} className="inline-flex shrink-0 text-default-secondary">
                      <Send className="h-3.5 w-3.5" aria-hidden />
                    </span>
                  </Tip>
                ))}
            </div>
          );
        },
        csv: (reply) => reply.status,
      },
      {
        key: "conversation",
        header: "Conversation",
        hideable: true,
        ...sortable("conversation"),
        width: showRule ? COLUMN_WIDTHS.conversation.wide : COLUMN_WIDTHS.conversation.narrow,
        className: "max-w-0 align-top",
        cell: (reply) => {
          const name = reply.conversation_name || t_i18n(kinds[reply.conversation_type] ?? kinds.direct);
          return (
            <span className="block truncate" title={name}>
              {name}
            </span>
          );
        },
        csv: (reply) => reply.conversation_name ?? "",
      },
      {
        key: "message",
        header: "Message",
        hideable: true,
        className: "max-w-0 align-top text-default-secondary",
        // One line, like every other cell: the opened row shows the whole message.
        cell: (reply) => {
          const text = reply.sent_text ?? reply.draft_text ?? "";
          return (
            <span className="block truncate" title={text}>
              {text}
            </span>
          );
        },
        csv: (reply) => reply.sent_text ?? reply.draft_text ?? "",
      },
    ];
    // Below xl the rule is read in the opened row only; the column is left out, not hidden by
    // CSS, so the opened row always spans the columns on screen.
    if (showRule) {
      drawn.push({
        key: "rule",
        header: "Rule",
        hideable: true,
        width: COLUMN_WIDTHS.rule,
        className: "max-w-0 truncate align-top",
        cell: (reply) => (
          <span title={reply.rule_name ?? undefined}>
            {reply.rule_name ?? <span className="text-default-secondary">-</span>}
          </span>
        ),
        csv: (reply) => reply.rule_name ?? "",
      });
    }
    return drawn;
  }, [intl, kinds, open, showRule, t_i18n, toggle]);

  return (
    <DataTable
      rows={data?.items ?? []}
      columns={columns}
      rowKey={(reply) => reply.id}
      server={{ total: data?.total ?? 0 }}
      state={tableState}
      onStateChange={onTableState}
      searchPlaceholder={t_i18n("Search the messages sent...")}
      filters={tableFilters}
      exportName="agent-account-messages-sent"
      storageKey={STORAGE_KEY}
      defaultPageSize={PAGE_SIZE}
      isLoading={isLoading}
      isError={isError}
      onRetry={() => void refetch()}
      emptyLabel={t_i18n("The account has sent no message yet.")}
      noResultsLabel={t_i18n("No message sent matches these filters.")}
      onRowClick={(reply) => toggle(reply.id)}
      toggleInCell
      rowClassName={(reply) => (open.includes(reply.id) ? "bg-elevation-hover" : undefined)}
      expandedRow={(reply) =>
        open.includes(reply.id) ? <AgentAccountReplyCard reply={reply} canAct={account.can_manage} /> : null
      }
      toolbar={isFetching && !isLoading && <Spinner size="sm" tone="inherit" className="text-default-secondary" />}
      density="comfortable"
      layout="fixed"
    />
  );
}
