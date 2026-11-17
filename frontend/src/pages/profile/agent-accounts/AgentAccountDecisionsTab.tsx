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
 * The Decisions tab of an agent account: every message the account
 * heard, one row each with what became of it - filtered by outcome, kind of
 * conversation, conversation, person and text, paged by the server. A row
 * opens on why: the reason, the rule, how the sender was matched, and the
 * reply the message produced. A message the agent sent on its own through the
 * account is a row too: "Proactive send", its recipients and origin
 * in place of why, the rule and how it was addressed.
 */
import { useCallback, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ChevronDown, ChevronRight, Hash, User, Users, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  DataTable,
  type DataTableColumn,
  type DataTableFilter,
  type DataTableState,
  type SortDirection,
} from "@/components/ui/data-table";
import { Tip } from "@/components/ui/tooltip";
import { useAgentAccountDecisionLog, useAgentAccountProvider, useAgentAccounts } from "@/hooks/api/useAgentAccountApi";
import { useFormatter } from "@/i18n";
import { useMediaQuery } from "@/hooks/useCompactToolbar";
import { formatShortDateTimeIntl, formatTimeAgo } from "@/lib/relative-time";
import type {
  AgentAccount,
  AgentAccountActivityGroup,
  AgentAccountConversationType,
  AgentAccountLoggedDecision,
  AgentAccountProviderKind,
} from "@/types";
import {
  DECISION_GROUP_FILTER_LABELS,
  OUTCOME_LABELS,
  SENDER_MATCH_LABELS,
  groupTone,
  outcomeLabelKey,
  outcomeTone,
  providerConversationTypes,
  sendOriginLinks,
  senderMatch,
  wording,
} from "./agent-accounts";
import { SendOriginLinks } from "./SendOriginLinks";
import { storedPageSize } from "@/lib/data-table-state";
import { toggleArrayItem } from "@/lib/utils";
import { Spinner } from "@filigran/design-system";

const PAGE_SIZE = 25;
const STORAGE_KEY = "agent-account-decisions";

/**
 * Column widths in px under the table's fixed layout (`wide` from xl, when the Rule column
 * shows): the names keep their room whatever a long message holds, and Message takes the
 * rest (UX round 1 on #9911, B1).
 */
const COLUMN_WIDTHS = {
  date: 144,
  outcome: { narrow: 160, wide: 192 },
  conversation: { narrow: 160, wide: 224 },
  sender: { narrow: 112, wide: 160 },
  rule: 160,
} as const;

const PAGE_SIZES = [10, 25, 50, 100];
/** The columns the server orders the log by, and the direction a first click sorts. */
const SORT_FIRST = { date: "desc", conversation: "asc", sender: "asc" } as const satisfies Record<string, SortDirection>;
type DecisionSort = keyof typeof SORT_FIRST;
const SORT_KEYS = Object.keys(SORT_FIRST) as DecisionSort[];
/**
 * From xl the Rule column shows; below it the rule is read in the
 * opened row only. The column is rendered from this query, not hidden by CSS,
 * so the opened row's colSpan always equals the columns on screen (a span of 7 over 6 visible columns added a phantom track under table-fixed
 * and halved the Message column at 1024).
 */
const RULE_COLUMN_QUERY = "(min-width: 1280px)";

export function AgentAccountDecisionsTab({ account }: { account: AgentAccount }) {
  const { t_i18n, intl } = useFormatter();
  const info = useAgentAccountProvider(account.provider);
  const kind = info?.kind ?? "chat";
  const words = wording(kind);
  const [params, setParams] = useSearchParams();
  const read = (key: string) => params.get(key) ?? "";
  const list = (key: string) => read(key).split(",").filter(Boolean);
  const groups = list("group").filter((value) => value in DECISION_GROUP_FILTER_LABELS && value !== "all");
  const types = list("type");
  const sortKey = (SORT_KEYS as readonly string[]).includes(read("sort")) ? (read("sort") as DecisionSort) : "date";
  const direction: SortDirection = read("dir") === "asc" ? "asc" : read("dir") === "desc" ? "desc" : SORT_FIRST[sortKey];
  // No `limit` in the URL: the size the user kept (DataTable storageKey), else the default.
  const defaultPageSize = storedPageSize(STORAGE_KEY, PAGE_SIZE);
  const pageSize = PAGE_SIZES.includes(Number(read("limit"))) ? Number(read("limit")) : defaultPageSize;
  const page = Math.max(0, Number(read("page")) || 0);
  const conversation = read("conversation");
  const sender = read("sender");
  const scoped = conversation || sender;

  const { data, isLoading, isFetching, isError, refetch } = useAgentAccountDecisionLog(
    account.id,
    {
      groups: groups as AgentAccountActivityGroup[],
      conversationTypes: types as AgentAccountConversationType[],
      conversationId: conversation || undefined,
      sender: sender || undefined,
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

  // The table's state is the tab's own URL keys (q, group, type, sort, dir, page, limit), so the
  // links the Overview opens (`?conversation=...&group=left`) and older bookmarks keep working.
  const tableState: DataTableState = {
    search: read("q"),
    sort: { key: sortKey, direction },
    page,
    pageSize,
    filters: { group: groups, type: types },
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
        group: (next.filters.group ?? []).join(","),
        type: (next.filters.type ?? []).join(","),
        sort: isDefault ? "" : nextSort.key,
        dir: isDefault ? "" : nextSort.direction,
        page: next.page > 0 ? String(next.page) : "",
        limit: next.pageSize === defaultPageSize ? "" : String(next.pageSize),
      });
    },
    [write, defaultPageSize],
  );
  const clearAll = useCallback(
    () => write({ q: "", group: "", type: "", conversation: "", sender: "", label: "", page: "" }),
    [write],
  );

  const tableFilters = useMemo<DataTableFilter<AgentAccountLoggedDecision>[]>(
    () => [
      {
        key: "group",
        label: "Outcome",
        options: (Object.keys(DECISION_GROUP_FILTER_LABELS) as (keyof typeof DECISION_GROUP_FILTER_LABELS)[])
          .filter((key) => key !== "all")
          .map((key) => ({ value: key, label: t_i18n(DECISION_GROUP_FILTER_LABELS[key]) })),
      },
      {
        key: "type",
        label: "Kind of conversation",
        options: providerConversationTypes(info).map((type) => ({ value: type, label: t_i18n(words.types[type]) })),
      },
    ],
    [info, t_i18n, words],
  );

  const columns = useMemo<DataTableColumn<AgentAccountLoggedDecision>[]>(() => {
    // Server mode: the API sorts, so a sortable column only needs a sortValue to offer its header.
    const sortable = (key: DecisionSort) => ({ sortValue: () => null, firstSort: SORT_FIRST[key] });
    const drawn: DataTableColumn<AgentAccountLoggedDecision>[] = [
      {
        key: "date",
        header: "When",
        ...sortable("date"),
        interactive: true,
        width: COLUMN_WIDTHS.date,
        className: "whitespace-nowrap align-top text-default-secondary",
        cell: (entry) => {
          const isOpen = open.includes(entry.id);
          const Chevron = isOpen ? ChevronDown : ChevronRight;
          return (
            <button
              type="button"
              onClick={() => toggle(entry.id)}
              aria-expanded={isOpen}
              aria-label={t_i18n(isOpen ? "Collapse" : "Expand")}
              className="flex items-center gap-1.5 rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-focus"
            >
              <Chevron className="h-3.5 w-3.5 shrink-0" aria-hidden />
              <Tip content={formatShortDateTimeIntl(intl, entry.at)}>
                <span>{formatTimeAgo(intl, entry.at)}</span>
              </Tip>
            </button>
          );
        },
        csv: (entry) => entry.at,
      },
      {
        key: "outcome",
        header: "What it did",
        hideable: true,
        width: showRule ? COLUMN_WIDTHS.outcome.wide : COLUMN_WIDTHS.outcome.narrow,
        className: "align-top",
        // One line per row, like every data table: the badges never wrap, a long one is cut and
        // the cell's title carries every wording (UX run 64ae3448 on #9944, Decisions drift).
        cell: (entry) => {
          const key = outcomeLabelKey(entry.outcome, kind);
          const parts = [key ? t_i18n(key) : entry.outcome];
          if (entry.kind === "proactive") parts.push(t_i18n("Proactive send"));
          if (entry.waited) parts.push(t_i18n("after a grace period"));
          return (
            <div
              className="flex min-w-0 flex-nowrap items-center gap-1 overflow-hidden"
              data-testid="decision-outcome-cell"
              title={parts.join(" - ")}
            >
              <OutcomeBadge entry={entry} kind={kind} />
              {entry.kind === "proactive" && (
                <Badge variant="outline" className="min-w-0 whitespace-nowrap content-caption">
                  <span className="truncate">{t_i18n("Proactive send")}</span>
                </Badge>
              )}
              {entry.waited && (
                <Badge variant="outline" className="min-w-0 whitespace-nowrap content-caption">
                  <span className="truncate">{t_i18n("after a grace period")}</span>
                </Badge>
              )}
            </div>
          );
        },
        csv: (entry) => entry.outcome,
      },
      {
        key: "conversation",
        header: "Conversation",
        hideable: true,
        ...sortable("conversation"),
        width: showRule ? COLUMN_WIDTHS.conversation.wide : COLUMN_WIDTHS.conversation.narrow,
        className: "max-w-0 align-top",
        cell: (entry) => {
          const name = entry.conversation_name || t_i18n(words.kinds[entry.conversation_type] ?? words.kinds.direct);
          return (
            <span className="flex min-w-0 items-center gap-1.5 overflow-hidden" title={name}>
              <ConversationIcon type={entry.conversation_type} />
              <span className="truncate">{name}</span>
            </span>
          );
        },
        csv: (entry) => entry.conversation_name ?? "",
      },
      {
        key: "sender",
        header: "Sent by",
        hideable: true,
        ...sortable("sender"),
        width: showRule ? COLUMN_WIDTHS.sender.wide : COLUMN_WIDTHS.sender.narrow,
        className: "max-w-0 align-top",
        cell: (entry) => (
          <span className="flex min-w-0 items-center overflow-hidden" title={entry.sender_name ?? undefined}>
            <span className="truncate">{entry.sender_name ?? "-"}</span>
            {entry.mentions_account && <Badge variant="outline" className="ml-1 content-caption">@</Badge>}
          </span>
        ),
        csv: (entry) => entry.sender_name ?? "",
      },
      {
        key: "message",
        header: "Message",
        hideable: true,
        className: "max-w-0 align-top text-default-secondary",
        // One line, like every other cell: the opened row shows the whole message.
        cell: (entry) => (
          <span className="block truncate" title={entry.preview ?? undefined}>
            {entry.preview}
          </span>
        ),
        csv: (entry) => entry.preview ?? "",
      },
    ];
    // Below xl the rule is read in the opened row only, so the table fits 1024 in every locale; the
    // column is left out, not hidden by CSS, so the opened row always spans the columns on screen.
    if (showRule) {
      drawn.push({
        key: "rule",
        header: "Rule",
        hideable: true,
        width: COLUMN_WIDTHS.rule,
        className: "max-w-0 truncate align-top",
        cell: (entry) => (
          <span title={entry.rule_name ?? undefined}>
            {entry.rule_name ?? <span className="text-default-secondary">-</span>}
          </span>
        ),
        csv: (entry) => entry.rule_name ?? "",
      });
    }
    return drawn;
  }, [intl, kind, open, showRule, t_i18n, toggle, words]);

  return (
    <div className="space-y-4">
      {scoped && (
        <div className="flex items-center gap-2 content-base">
          <span className="text-default-secondary">{conversation ? t_i18n("In conversation") : t_i18n("Sent by")}</span>
          <Badge variant="secondary" className="gap-1">
            {read("label") || conversation || sender}
            <button
              type="button"
              onClick={() => write({ conversation: "", sender: "", label: "", page: "" })}
              aria-label={t_i18n("Clear")}
              className="ml-1 rounded-sm hover:text-default-primary"
            >
              <X className="h-3 w-3" />
            </button>
          </Badge>
        </div>
      )}
      <DataTable
        rows={data?.items ?? []}
        columns={columns}
        rowKey={(entry) => entry.id}
        server={{ total: data?.total ?? 0 }}
        state={tableState}
        onStateChange={onTableState}
        searchPlaceholder={t_i18n("Search decisions...")}
        filters={tableFilters}
        exportName="agent-account-decisions"
        storageKey={STORAGE_KEY}
        externalFilterCount={scoped ? 1 : 0}
        onClearAll={clearAll}
        defaultPageSize={PAGE_SIZE}
        isLoading={isLoading}
        isError={isError}
        onRetry={() => void refetch()}
        emptyLabel={t_i18n("The account has heard no message yet.")}
        noResultsLabel={t_i18n("No message matches these filters.")}
        onRowClick={(entry) => toggle(entry.id)}
        toggleInCell
        rowClassName={(entry) => (open.includes(entry.id) ? "bg-elevation-hover" : undefined)}
        expandedRow={(entry) =>
          open.includes(entry.id) ? <DecisionDetail account={account} entry={entry} kind={kind} /> : null
        }
        toolbar={isFetching && !isLoading && <Spinner size="sm" tone="inherit" className="text-default-secondary" />}
        density="comfortable"
        layout="fixed"
      />
    </div>
  );
}

function OutcomeBadge({ entry, kind }: { entry: AgentAccountLoggedDecision; kind: AgentAccountProviderKind }) {
  const { t_i18n } = useFormatter();
  const key = outcomeLabelKey(entry.outcome, kind);
  // One line: the longest outcomes ("left alone" application_author) run past 200 px in fr, es
  // and ja, so the badge shrinks and cuts its wording under the fixed layout instead of wrapping
  // the row to two lines; the cell's title holds the whole wording.
  return (
    <Badge variant={groupTone(entry.group)} className="min-w-0 whitespace-nowrap">
      <span className="truncate">{key ? t_i18n(key) : entry.outcome}</span>
    </Badge>
  );
}

function ConversationIcon({ type }: { type: AgentAccountConversationType }) {
  const Icon = type === "channel" ? Hash : type === "group" ? Users : User;
  return <Icon className="h-3.5 w-3.5 shrink-0 text-default-secondary" />;
}

/**
 * A link inside the decision panel: the size and weight of the compact
 * line it sits in, so the holder line reads the same linked or plain, with the
 * accent, the underline on hover and the focus ring of a link.
 */
const PANEL_LINK =
  "block w-fit content-compact text-accent underline-offset-4 hover:underline rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-focus";

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-[11px] font-medium uppercase tracking-wider text-default-secondary">{label}</dt>
      <dd className="mt-0.5 content-base">{children}</dd>
    </div>
  );
}

function DecisionDetail({ account, entry, kind }: {
  account: AgentAccount;
  entry: AgentAccountLoggedDecision;
  kind: AgentAccountProviderKind;
}) {
  const { t_i18n, intl } = useFormatter();
  const words = wording(kind);
  if (entry.kind === "proactive") return <ProactiveDetail account={account} entry={entry} />;
  const reasonKey = entry.reason ? outcomeLabelKey(entry.reason, kind) : null;
  const sender = senderMatch(entry.sender);
  const how = entry.conversation_type === "direct"
    ? t_i18n(words.how.direct)
    : entry.mentions_account
      ? t_i18n(words.how.mention)
      : entry.thread_continuation
        ? t_i18n("A follow-up in a thread the agent answered in")
        : t_i18n(words.how.none);
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <div className="space-y-3">
        <div className="rounded-sm border border-elevation-subtle bg-elevation-default p-3 content-base">
          <div className="mb-1 content-compact font-medium">{entry.sender_name ?? t_i18n("Someone")}</div>
          <p className="whitespace-pre-wrap text-default-secondary">{entry.preview}</p>
        </div>
        <dl className="grid grid-cols-2 gap-3">
          <Fact label={t_i18n("Why")}>
            {entry.respond ? t_i18n("A rule answers it") : reasonKey ? t_i18n(reasonKey) : entry.reason ?? "-"}
          </Fact>
          <Fact label={t_i18n("Rule")}>{entry.rule_name ?? "-"}</Fact>
          <Fact label={t_i18n("Addressed")}>{how}</Fact>
          <Fact label={t_i18n("Grace period")}>{entry.waited ? t_i18n("Waited before deciding") : t_i18n("No grace period")}</Fact>
          <Fact label={t_i18n("Written")}>{entry.message_at ? formatShortDateTimeIntl(intl, entry.message_at) : "-"}</Fact>
          <Fact label={t_i18n("Decided")}>{formatShortDateTimeIntl(intl, entry.updated_at)}</Fact>
        </dl>
        {sender && <p className="content-compact text-default-secondary">{t_i18n(SENDER_MATCH_LABELS[sender.key], sender.values)}</p>}
      </div>
      <div className="space-y-2">
        {entry.note && (
          <div className="rounded-sm border border-elevation-subtle bg-elevation-default p-3 content-base">
            <div className="mb-1 content-compact font-medium uppercase tracking-wider text-default-secondary">{t_i18n("The agent's reason")}</div>
            <p className="whitespace-pre-wrap">{entry.note}</p>
          </div>
        )}
        <div className="content-compact font-medium uppercase tracking-wider text-default-secondary">{t_i18n("The reply")}</div>
        {entry.reply_id ? (
          <div className="space-y-2 rounded-sm border border-elevation-subtle bg-elevation-default p-3">
            {entry.reply_status && (
              <Badge variant={outcomeTone(entry.reply_status)}>
                {t_i18n(OUTCOME_LABELS[entry.reply_status] ?? OUTCOME_LABELS.failed)}
              </Badge>
            )}
            {entry.reply_text && <p className="whitespace-pre-wrap content-base">{entry.reply_text}</p>}
            {entry.reply_error && <p className="whitespace-pre-wrap break-words content-compact text-feedback-error-primary">{entry.reply_error}</p>}
            <Link to={`/profile/agent-accounts/${account.id}/sent`} className={PANEL_LINK}>{t_i18n("See all messages sent")}</Link>
          </div>
        ) : (
          <p className="content-base text-default-secondary">
            {entry.outcome === "queued"
              ? t_i18n("It is answered once the current reply is posted.")
              : entry.outcome === "coalesced"
              ? t_i18n("Answered together with a later message of this conversation.")
              : entry.group === "needs_owner"
              ? t_i18n("The agent left it to you: nothing was sent, and it is waiting for your answer.")
              : entry.group === "silent"
              ? t_i18n("The agent read the message and chose not to answer.")
              : entry.group === "waiting"
                ? t_i18n("The grace period is not over yet.")
                : entry.group === "failed"
                  ? t_i18n("Nothing was posted: the agent could not answer.")
                  : entry.group === "already_handled"
                    ? t_i18n("No reply from this account: another pipeline of the agent took the message first and answers it.")
                    : t_i18n("No reply: the message was not for the agent.")}
          </p>
        )}
        {entry.holder_kind && <HolderLine entry={entry} />}
      </div>
    </div>
  );
}

/**
 * Who took a message another pipeline of the agent answers: the
 * holder travels as data and is worded here, in the viewer's language. An
 * agent account the viewer can see is named and linked; one they cannot see
 * is said without a name, never as a raw id.
 */
function HolderLine({ entry }: { entry: AgentAccountLoggedDecision }) {
  const { t_i18n } = useFormatter();
  const byAccount = entry.holder_kind === "agent_account";
  const { data: accounts } = useAgentAccounts();
  const holder = byAccount ? accounts?.find((a) => a.id === entry.holder_account_id) : undefined;
  return (
    <p className="content-compact text-default-secondary" data-holder={entry.holder_kind}>
      {!byAccount ? (
        t_i18n("Taken by an assignment run of this agent.")
      ) : holder ? (
        <Link to={`/profile/agent-accounts/${holder.id}`} className={PANEL_LINK}>
          {t_i18n("Taken by the agent account {name}.", { name: holder.name })}
        </Link>
      ) : (
        t_i18n("Taken by another agent account of this agent.")
      )}
    </p>
  );
}

/** A message the agent sent on its own: no message heard, no rule, no grace period. */
function ProactiveDetail({ account, entry }: { account: AgentAccount; entry: AgentAccountLoggedDecision }) {
  const { t_i18n, intl } = useFormatter();
  const recipients = (entry.origin?.recipients ?? []).join(", ");
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <dl className="grid grid-cols-2 content-start gap-3">
        <div className="col-span-2">
          <Fact label={t_i18n("Why")}>{t_i18n("Proactive send")}</Fact>
        </div>
        <Fact label={t_i18n("Sent to")}>{recipients || "-"}</Fact>
        <Fact label={t_i18n("Origin")}>
          {sendOriginLinks(entry.origin).length ? null : "-"}
          <SendOriginLinks origin={entry.origin} className="flex flex-wrap gap-x-3 gap-y-1" />
        </Fact>
        <Fact label={t_i18n("Sent")}>{formatShortDateTimeIntl(intl, entry.at)}</Fact>
      </dl>
      <div className="space-y-2">
        <div className="content-compact font-medium uppercase tracking-wider text-default-secondary">{t_i18n("The message")}</div>
        <div className="space-y-2 rounded-sm border border-elevation-subtle bg-elevation-default p-3">
          {entry.reply_status && (
            <Badge variant={outcomeTone(entry.reply_status)}>
              {t_i18n(OUTCOME_LABELS[entry.reply_status] ?? OUTCOME_LABELS.failed)}
            </Badge>
          )}
          <p className="whitespace-pre-wrap content-base">{entry.reply_text ?? entry.preview}</p>
          {entry.reply_error && <p className="whitespace-pre-wrap break-words content-compact text-feedback-error-primary">{entry.reply_error}</p>}
          <Link to={`/profile/agent-accounts/${account.id}/sent?show=proactive`} className={PANEL_LINK}>{t_i18n("See all messages sent")}</Link>
        </div>
      </div>
    </div>
  );
}
