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

import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Scan } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { DateFilterInput } from "@/components/ui/date-filter-input";
import { Switch } from "@/components/ui/switch";
import { DataTable, type DataTableColumn, type DataTableFilter, type DataTableState } from "@/components/ui/data-table";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { EmptyState } from "@/components/ui/empty-state";
import { NavFamilyHeader } from "@/components/layout/NavFamilyHeader";
import { Label } from "@/components/ui/label";
import { MultiSelect } from "@/components/ui/multi-select";
import { useToast } from "@/components/ui/toast";
import type { SortDirection } from "@/components/ui/table-sort-header";
import { MaskedCost } from "@/components/ui/masked-cost";
import { GroupScopeFilter } from "@/components/traces/group-scope-filter";
import { TraceListOutOfReach } from "@/components/traces/trace-list-out-of-reach";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useTraceSessions } from "@/hooks/api/useTraceApi";
import { useCostDataAvailability } from "@/hooks/useCostDataAvailability";
import { useRecordTraceListTotal, useTraceListPaging } from "@/hooks/useTraceListPaging";
import { api } from "@/lib/api";
import { displayedTotal, pageRequest, pageRows, readsRows, recordsNextCursor } from "@/lib/trace-list-paging";
import { formatCompactNumber, formatModelLabel } from "@/lib/utils";
import { PLATFORM_DEFAULT_MODEL_FILTER_VALUE } from "@/lib/constants";
import { exactCost, formatCost, traceStatusLabelKey } from "@/lib/trace-utils";
import { useFormatter } from "@/i18n";
import type { LLMTraceListResponse, TraceFilters, TraceSession } from "@/types";

const SOURCE_OPTIONS = [
  "openai",
  "anthropic",
  "openrouter",
  "azure_openai",
  "ollama",
  "mistral",
  "xai",
  "litellm",
  "custom_openai",
] as const;

const DEFAULT_LIMIT = 50;

const DEFAULT_SORT = "last_activity_at";
const DEFAULT_SORT_DIR: SortDirection = "desc";

/**
 * Sortable columns, mapped to the direction a first click opens with: newest
 * and biggest first for dates and counts, A→Z for the conversation id. The keys
 * are the `sort_by` values the API allows for `/traces/sessions`.
 */
const SORT_COLUMNS: Record<string, SortDirection> = {
  last_activity_at: "desc",
  conversation_id: "asc",
  trace_count: "desc",
  agent_count: "desc",
  model_count: "desc",
  total_tokens: "desc",
  total_cost: "desc",
  error_count: "desc",
};

interface CursorEntry {
  cursor?: string;
  cursorId?: string;
}

function parsePositiveInt(value: string | null, fallback: number, max?: number): number {
  const parsed = Number.parseInt(value ?? "", 10);
  if (!Number.isFinite(parsed) || parsed < 1) return fallback;
  return max ? Math.min(parsed, max) : parsed;
}

function parseNonNegativeInt(value: string | null, fallback = 0): number {
  const parsed = Number.parseInt(value ?? "", 10);
  if (!Number.isFinite(parsed) || parsed < 0) return fallback;
  return parsed;
}

function parseCsv(value: string | null): string[] {
  if (!value) return [];
  return value.split(",").map((v) => v.trim()).filter(Boolean);
}

/**
 * Normalize the URL's sort column against the columns this table can sort on.
 * A hand-edited or stale link falls back to the default order instead of
 * sending the API a column it rejects.
 */
function parseSort(value: string | null): string {
  return value && value in SORT_COLUMNS ? value : DEFAULT_SORT;
}

export default function TraceSessions() {
  const { t_i18n } = useFormatter();
  const costDataAvailable = useCostDataAvailability();
  const navigate = useNavigate();
  const { toast } = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const [pageCursors, setPageCursors] = useState<Record<number, CursorEntry>>({ 0: {} });
  const [openingSessionId, setOpeningSessionId] = useState<string | null>(null);

  const q = searchParams.get("q") ?? "";
  const sources = parseCsv(searchParams.get("source"));
  const agentIds = parseCsv(searchParams.get("agent_id"));
  const groupIds = parseCsv(searchParams.get("group_id"));
  const models = parseCsv(searchParams.get("model"));
  const status = searchParams.get("status") ?? "";
  const dateFrom = searchParams.get("date_from") ?? "";
  const dateTo = searchParams.get("date_to") ?? "";
  const limit = parsePositiveInt(searchParams.get("limit"), DEFAULT_LIMIT, 200);
  const page = parseNonNegativeInt(searchParams.get("page"));
  const requestedSort = parseSort(searchParams.get("sort"));
  // Ranking by a cost the UI masks would be a sort nobody can see — and the
  // Cost header offers no affordance then either.
  const sortBy = requestedSort === "total_cost" && !costDataAvailable ? DEFAULT_SORT : requestedSort;
  const sortDir: SortDirection = searchParams.get("dir") === "asc" ? "asc" : "desc";
  // The keyset cursor is a `<` on the session's last activity, so it only
  // describes the default newest-first order; every other order - and a page
  // that walk holds no cursor for - is read by offset from whichever end the
  // API's bound reaches.
  const isKeysetSort = sortBy === DEFAULT_SORT && sortDir === DEFAULT_SORT_DIR;

  const updateParams = useCallback(
    (updates: Record<string, string>, resetPage = true) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          for (const [k, v] of Object.entries(updates)) {
            if (v) next.set(k, v);
            else next.delete(k);
          }
          if (resetPage) next.delete("page");
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const setPage = useCallback(
    (target: number) => {
      if (target < 0) return;
      // Any page can be entered: useTraceListPaging reads it by cursor, by
      // offset from either end, or explains why it is out of reach.
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (target > 0) next.set("page", String(target));
          else next.delete("page");
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const toggleCsvParam = useCallback(
    (key: string, value: string, values: string[]) => {
      const nextValues = values.includes(value) ? values.filter((v) => v !== value) : [...values, value];
      updateParams({ [key]: nextValues.join(",") });
    },
    [updateParams],
  );

  const clearFilters = useCallback(() => {
    const next = new URLSearchParams({ limit: String(limit) });
    // The sort is a view preference, not a filter: clearing filters keeps it.
    if (sortBy !== DEFAULT_SORT) next.set("sort", sortBy);
    if (sortDir !== DEFAULT_SORT_DIR) next.set("dir", sortDir);
    setSearchParams(next, { replace: true });
  }, [limit, setSearchParams, sortBy, sortDir]);

  const filterFingerprint = useMemo(
    () => JSON.stringify({ q, sources, agentIds, groupIds, models, status, dateFrom, dateTo, limit, sortBy, sortDir }),
    [q, sources, agentIds, groupIds, models, status, dateFrom, dateTo, limit, sortBy, sortDir],
  );

  useEffect(() => {
    setPageCursors({ 0: {} });
  }, [filterFingerprint]);

  useEffect(() => {
    const currentPageParam = searchParams.get("page");
    const currentLimitParam = searchParams.get("limit");
    const normalizedPage = parseNonNegativeInt(currentPageParam);
    const normalizedLimit = parsePositiveInt(currentLimitParam, DEFAULT_LIMIT, 200);
    if (
      (currentPageParam !== null && currentPageParam !== String(normalizedPage)) ||
      currentLimitParam !== String(normalizedLimit)
    ) {
      const next = new URLSearchParams(searchParams);
      if (normalizedPage > 0) next.set("page", String(normalizedPage));
      else next.delete("page");
      next.set("limit", String(normalizedLimit));
      setSearchParams(next, { replace: true });
    }
  }, [searchParams, setSearchParams]);

  const cursor = pageCursors[page]?.cursor;
  const cursorId = pageCursors[page]?.cursorId;

  const listFilters: TraceFilters = {
    search: q || undefined,
    source: sources.length > 0 ? sources : undefined,
    agent_id: agentIds.length > 0 ? agentIds : undefined,
    model: models.length > 0 ? models.map((m) => (m === PLATFORM_DEFAULT_MODEL_FILTER_VALUE ? "" : m)) : undefined,
    status: status || undefined,
    date_from: dateFrom ? `${dateFrom}T00:00:00Z` : undefined,
    date_to: dateTo ? `${dateTo}T23:59:59Z` : undefined,
    group_id: groupIds.length > 0 ? groupIds : undefined,
  };

  const paging = useTraceListPaging({
    fingerprint: filterFingerprint,
    page,
    limit,
    hasCursor: isKeysetSort && (page === 0 || !!pageCursors[page]),
  });
  const { plan } = paging;
  const apiFilters: TraceFilters = {
    ...listFilters,
    cursor: plan.kind === "cursor" ? cursor : undefined,
    cursor_id: plan.kind === "cursor" ? cursorId : undefined,
    sort_by: sortBy,
    sort_dir: sortDir,
    ...pageRequest(plan, page, limit),
  };

  const sessionListQuery = useTraceSessions(apiFilters, { enabled: readsRows(plan) });
  // A deep link past the offset bound needs the count before its far-end
  // offset is known; one row in the default order is the cheapest read of it.
  const sessionCountQuery = useTraceSessions({ ...listFilters, limit: 1 }, { enabled: plan.kind === "count-first" });
  useRecordTraceListTotal(paging, sessionListQuery.data?.total, sessionCountQuery.data?.total);
  // Every session the viewer may read, before the card's filters, the status and the search:
  // the total reads "n of N" when they narrow it (#12039).
  const everySessionQuery = useTraceSessions({ limit: 1 });
  const { data: agents } = useAgents();
  const sessionItems = useMemo(() => pageRows(plan, sessionListQuery.data?.items), [plan, sessionListQuery.data]);
  const total = displayedTotal(plan, paging.planningTotal, sessionListQuery.data?.total);

  useEffect(() => {
    if (!isKeysetSort || sessionItems.length === 0 || !recordsNextCursor(plan, page, limit, total)) return;
    const last = sessionItems[sessionItems.length - 1];
    if (!last?.last_activity_at || !last?.conversation_id) return;
    setPageCursors((prev) => ({
      ...prev,
      [page + 1]: { cursor: last.last_activity_at, cursorId: last.conversation_id },
    }));
  }, [isKeysetSort, limit, page, plan, sessionItems, total]);

  const modelOptions = useMemo(() => {
    const values = new Set(models);
    return Array.from(values)
      .sort((a, b) => a.localeCompare(b))
      .map((model) => ({
        value: model,
        label: formatModelLabel(model === PLATFORM_DEFAULT_MODEL_FILTER_VALUE ? "" : model, t_i18n),
      }));
  }, [models, t_i18n]);

  const agentOptions = useMemo(
    () => (agents ?? []).map((agent) => ({ value: agent.id, label: agent.name })),
    [agents],
  );

  const isLoading = sessionListQuery.isLoading || sessionCountQuery.isLoading;

  // The shared table draws the page the API returned; its state is the page's own URL keys
  // (q, status, sort, dir, page, limit), so every link to /trace-sessions keeps working.
  const tableState: DataTableState = {
    search: q,
    sort: { key: sortBy, direction: sortDir },
    page,
    pageSize: limit,
    filters: status ? { status: [status] } : {},
  };
  const onTableState = useCallback(
    (next: DataTableState) => {
      const statusValues = next.filters.status ?? [];
      const nextSort = next.sort ?? { key: DEFAULT_SORT, direction: DEFAULT_SORT_DIR };
      // The default order needs no params: those URLs stay as short as they have always been.
      const isDefault = nextSort.key === DEFAULT_SORT && nextSort.direction === DEFAULT_SORT_DIR;
      updateParams({
        q: next.search,
        // One status narrows; both (or none) is every session.
        status: statusValues.length === 1 ? statusValues[0] : "",
        sort: isDefault ? "" : nextSort.key,
        dir: isDefault ? "" : nextSort.direction,
        page: next.page > 0 ? String(next.page) : "",
        limit: String(next.pageSize),
      }, false);
    },
    [updateParams],
  );
  const statusFilters = useMemo<DataTableFilter<TraceSession>[]>(
    () => [
      {
        key: "status",
        label: "Status",
        options: [
          { value: "ok", label: t_i18n(traceStatusLabelKey("ok")) },
          { value: "error", label: t_i18n(traceStatusLabelKey("error")) },
        ],
      },
    ],
    [t_i18n],
  );

  const openSessionInTraceDetail = useCallback(
    async (conversationId: string) => {
      if (!conversationId || openingSessionId) return;
      setOpeningSessionId(conversationId);
      try {
        const params = new URLSearchParams();
        params.set("conversation_id", conversationId);
        params.set("limit", "1");
        const response = await api.get<LLMTraceListResponse>(`/traces?${params.toString()}`);
        const firstTrace = response.items[0];
        if (!firstTrace) return;
        navigate(`/observability/traces/${firstTrace.id}?session_id=${conversationId}`);
      } catch (error) {
        toast({
          title: t_i18n("Failed to open session"),
          description: error instanceof Error ? error.message : t_i18n("Could not load traces for this session."),
          variant: "error",
        });
      } finally {
        setOpeningSessionId(null);
      }
    },
    [navigate, openingSessionId, toast, t_i18n],
  );

  const sessionColumns = useSessionColumns(costDataAvailable, openingSessionId);

  // The filters the card above the table holds: the table counts them with its own and its
  // "Clear all filters" clears them too, so the page has one count and one clear.
  const cardFilterCount = [sources.length > 0, agentIds.length > 0, groupIds.length > 0, models.length > 0, !!dateFrom, !!dateTo].filter(
    Boolean,
  ).length;
  const pageFilterCount = cardFilterCount + (q ? 1 : 0) + (status ? 1 : 0);

  // The page's own filters sit in the table's one Filters pill (#12070): the toolbar keeps
  // one row and the list is the page's last block, so it fits the window (one scroller).
  const sessionPills = (
    <div className="w-80 space-y-3" data-testid="trace-sessions-filters">
      <div role="group" aria-labelledby="trace-sessions-filter-time-range" className="space-y-1">
        <Label id="trace-sessions-filter-time-range">{t_i18n("Time range")}</Label>
        <div className="flex flex-wrap items-center gap-2">
          <DateFilterInput aria-label={t_i18n("From")} value={dateFrom} onChange={(value) => updateParams({ date_from: value })} />
          <DateFilterInput aria-label={t_i18n("To")} value={dateTo} onChange={(value) => updateParams({ date_to: value })} />
        </div>
      </div>
      <div role="group" aria-labelledby="trace-sessions-filter-sources" className="space-y-1">
        <Label id="trace-sessions-filter-sources">{t_i18n("Sources")}</Label>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          {SOURCE_OPTIONS.map((source) => (
            <label key={source} className="flex items-center gap-2 content-compact text-default-secondary">
              <Switch checked={sources.includes(source)} onCheckedChange={() => toggleCsvParam("source", source, sources)} />
              {source}
            </label>
          ))}
        </div>
      </div>
      <GroupScopeFilter selected={groupIds} onToggle={(value) => toggleCsvParam("group_id", value, groupIds)} />
      <div className="space-y-1">
        <Label htmlFor="trace-sessions-filter-agents">{t_i18n("Agents")}</Label>
        <MultiSelect
          id="trace-sessions-filter-agents"
          options={agentOptions}
          selected={agentIds}
          onToggle={(value) => toggleCsvParam("agent_id", value, agentIds)}
          placeholder={t_i18n("Select agents")}
          searchPlaceholder={t_i18n("Search agents...")}
          emptyOptionsLabel={t_i18n("No agents available")}
          noMatchLabel={t_i18n("No matching agents.")}
        />
      </div>
      {modelOptions.length > 0 && (
        <div className="space-y-1">
          <Label htmlFor="trace-sessions-filter-models">{t_i18n("Models")}</Label>
          <MultiSelect
            id="trace-sessions-filter-models"
            options={modelOptions}
            sort={false}
            selected={models}
            onToggle={(value) => toggleCsvParam("model", value, models)}
            placeholder={t_i18n("Select models")}
            searchPlaceholder={t_i18n("Search models...")}
            emptyOptionsLabel={t_i18n("No models available yet")}
            noMatchLabel={t_i18n("No matching models.")}
          />
        </div>
      )}
    </div>
  );

  return (
    <div className="space-y-6">
      {/* The Observability family header (#9301 N3): its h1 and one tab row. */}
      <NavFamilyHeader
        to="/observability/sessions"
        title="Trace Sessions"
        infoLabel={t_i18n("About sessions")}
        info={t_i18n("Browse conversation-level session aggregates across LLM traces.")}
      />


      {plan.kind === "out-of-reach" ? (
        <TraceListOutOfReach
          page={page}
          newestFirst={isKeysetSort}
          onSortNewestFirst={() => updateParams({ sort: "", dir: "" })}
          onFirstPage={() => setPage(0)}
        />
      ) : !isLoading && sessionItems.length === 0 && pageFilterCount === 0 ? (
        <EmptyState
          icon={Scan}
          /* FR-027: the copy below talks about filters, which is the
             wrong story on a platform that never produced a trace. */
          requiresAi
          title="No sessions found"
          description="No sessions match your current filters. Try clearing one or more filters."
        />
      ) : (
        <DataTable
          rows={[...sessionItems]}
          columns={sessionColumns}
          rowKey={(session) => session.conversation_id}
          server={{ total }}
          totalAll={everySessionQuery.data?.total}
          state={tableState}
          onStateChange={onTableState}
          searchPlaceholder={t_i18n("Search conversation, model, agent...")}
          filters={statusFilters}
          filtersInPill
          filterPills={sessionPills}
          externalFilterCount={cardFilterCount}
          onClearAll={clearFilters}
          defaultPageSize={DEFAULT_LIMIT}
          isLoading={isLoading}
          isError={sessionListQuery.isError}
          onRetry={() => void sessionListQuery.refetch()}
          emptyLabel={t_i18n("No sessions found")}
          onRowClick={(session) => void openSessionInTraceDetail(session.conversation_id)}
          rowClassName={() => "cursor-pointer"}
          rowTestId="session-row"
        />
      )}
    </div>
  );
}

/** The columns of the session list; the sortable ones are the `sort_by` values the API allows. */
function useSessionColumns(costDataAvailable: boolean, openingSessionId: string | null) {
  const { t_i18n, intl } = useFormatter();
  const locale = intl.locale;
  return useMemo<DataTableColumn<TraceSession>[]>(() => {
    // Server mode: the API sorts, so a sortable column only needs a sortValue to offer its header.
    const sortable = (key: string) => ({ sortValue: () => null, firstSort: SORT_COLUMNS[key] });
    const count = (key: "trace_count" | "agent_count" | "model_count" | "total_tokens") => (session: TraceSession) => (
      <span className="whitespace-nowrap">{formatCompactNumber(session[key])}</span>
    );
    return [
      {
        key: "conversation_id",
        header: "Conversation",
        // A person reads the conversation's title, never its id; the tooltip carries the text the
        // truncated cell shows (UX round 1 B4 and round 3 B6 on #9728).
        cell: (session) => {
          const label = openingSessionId === session.conversation_id
            ? t_i18n("Opening trace details...")
            : session.conversation_title?.trim() || t_i18n("Untitled conversation");
          return (
            <span className="block max-w-[220px] truncate whitespace-nowrap" title={label}>
              {label}
            </span>
          );
        },
        // Not sortable: the API orders by the id, an order the title the row shows cannot explain.
      },
      {
        key: "user",
        header: "User",
        hideable: true,
        // Steps aside in a narrow table (1024) instead of scrolling it sideways.
        hideBelow: "4xl",
        cell: (session) => (
          <span className="block max-w-[160px] truncate whitespace-nowrap text-default-secondary" title={session.user_email ?? undefined}>
            {session.user_email ?? "-"}
          </span>
        ),
      },
      { key: "last_activity_at", header: "Last Activity", cell: (session) => <DateTimeCell value={session.last_activity_at} />, ...sortable("last_activity_at") },
      { key: "trace_count", header: "Traces", align: "right", cell: count("trace_count"), ...sortable("trace_count") },
      { key: "agent_count", header: "Agents", align: "right", cell: count("agent_count"), ...sortable("agent_count") },
      // Models steps aside in the 1024 table instead of scrolling it sideways (UX round 2 on #9728, B1).
      { key: "model_count", header: "Models", align: "right", hideBelow: "4xl", cell: count("model_count"), ...sortable("model_count") },
      { key: "total_tokens", header: "Tokens", align: "right", cell: count("total_tokens"), ...sortable("total_tokens") },
      {
        key: "total_cost",
        header: "Cost",
        align: "right",
        cell: (session) =>
          costDataAvailable ? <span className="whitespace-nowrap" title={exactCost(session.total_cost, locale)}>{formatCost(session.total_cost, locale)}</span> : <MaskedCost className="inline-flex" />,
        // Masked costs are all the same value on screen: ranking by one would be a sort the user cannot see.
        ...(costDataAvailable ? sortable("total_cost") : {}),
      },
      {
        key: "error_count",
        header: "Errors",
        align: "right",
        cell: (session) => (
          <Badge variant={session.error_count > 0 ? "destructive" : "secondary"}>{formatCompactNumber(session.error_count)}</Badge>
        ),
        ...sortable("error_count"),
      },
    ];
  }, [costDataAvailable, locale, openingSessionId, t_i18n]);
}
