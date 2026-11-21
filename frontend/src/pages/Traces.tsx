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
import { Link, useSearchParams } from "react-router-dom";
import { AlertCircle, CheckCircle2, Play, Scan, X, Zap, type LucideIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { DateFilterInput } from "@/components/ui/date-filter-input";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { DataTable, type DataTableColumn, type DataTableFilter, type DataTableState } from "@/components/ui/data-table";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { EmptyState } from "@/components/ui/empty-state";
import { NavFamilyHeader } from "@/components/layout/NavFamilyHeader";
import { Label } from "@/components/ui/label";
import { MultiSelect } from "@/components/ui/multi-select";
import { GroupScopeFilter } from "@/components/traces/group-scope-filter";
import { TracePeekDrawer } from "@/components/traces/trace-peek-drawer";
import { TraceListOutOfReach } from "@/components/traces/trace-list-out-of-reach";
import type { SortDirection } from "@/components/ui/table-sort-header";
import { MaskedCost } from "@/components/ui/masked-cost";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useAssignment } from "@/hooks/api/useAssignmentApi";
import { useTraces } from "@/hooks/api/useTraceApi";
import { useCostDataAvailability } from "@/hooks/useCostDataAvailability";
import { useRecordTraceListTotal, useTraceListPaging } from "@/hooks/useTraceListPaging";
import { displayedTotal, pageRequest, pageRows, readsRows, recordsNextCursor } from "@/lib/trace-list-paging";
import { ASSIGNMENT_RUN_TRACE_PARAM, ASSIGNMENT_TRACE_PARAM, isTraceScopeId } from "@/lib/trace-links";
import { formatCompactNumber, formatModelLabel } from "@/lib/utils";
import { PLATFORM_DEFAULT_MODEL_FILTER_VALUE } from "@/lib/constants";
import { FAILURE_CAUSE_GROUPS, formatFailureCause } from "@/lib/failure-causes";
import { exactCost, formatCost, formatDuration, traceStatusLabelKey } from "@/lib/trace-utils";
import { useFormatter } from "@/i18n";
import type { LLMTrace, TraceFilters } from "@/types";

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

const DEFAULT_SORT = "started_at";
const DEFAULT_SORT_DIR: SortDirection = "desc";

/**
 * Sortable columns, mapped to the direction a first click opens with: newest
 * and biggest first for dates and numbers, A→Z for text. The keys are the
 * `sort_by` values the API allows for `/observability/traces` — Agent and Score are absent on
 * purpose, since both live outside the trace row and the ClickHouse read path
 * cannot order by them.
 */
const SORT_COLUMNS: Record<string, SortDirection> = {
  started_at: "desc",
  name: "asc",
  model: "asc",
  duration_ms: "desc",
  total_tokens: "desc",
  total_cost: "desc",
  status: "asc",
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

export default function Traces() {
  const { t_i18n } = useFormatter();
  const costDataAvailable = useCostDataAvailability();
  const [searchParams, setSearchParams] = useSearchParams();
  const [pageCursors, setPageCursors] = useState<Record<number, CursorEntry>>({ 0: {} });
  const selectedTraceId = searchParams.get("trace_id");

  const q = searchParams.get("q") ?? "";
  const sources = parseCsv(searchParams.get("source"));
  const agentIds = parseCsv(searchParams.get("agent_id"));
  const groupIds = parseCsv(searchParams.get("group_id"));
  const models = parseCsv(searchParams.get("model"));
  const status = searchParams.get("status") ?? "";
  const failureCauses = parseCsv(searchParams.get("failure_cause"));
  const dateFrom = searchParams.get("date_from") ?? "";
  const dateTo = searchParams.get("date_to") ?? "";
  // Set by the "View Traces" shortcuts of an assignment and of a run.
  const assignmentId = searchParams.get(ASSIGNMENT_TRACE_PARAM) ?? "";
  const assignmentRunId = searchParams.get(ASSIGNMENT_RUN_TRACE_PARAM) ?? "";
  const limit = parsePositiveInt(searchParams.get("limit"), DEFAULT_LIMIT, 200);
  const page = parseNonNegativeInt(searchParams.get("page"));
  const requestedSort = parseSort(searchParams.get("sort"));
  // Ranking by a cost the UI masks would be a sort nobody can see — and the
  // Cost header offers no affordance then either.
  const sortBy = requestedSort === "total_cost" && !costDataAvailable ? DEFAULT_SORT : requestedSort;
  const sortDir: SortDirection = searchParams.get("dir") === "asc" ? "asc" : "desc";
  // The keyset cursor is a `<` on started_at, so it only describes the default
  // newest-first order; every other order - and a page that walk holds no
  // cursor for - is read by offset from whichever end the API's bound reaches.
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

  const openTraceDrawer = useCallback(
    (traceId: string) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.set("trace_id", traceId);
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const onTraceDrawerOpenChange = useCallback(
    (open: boolean) => {
      if (open) return;
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.delete("trace_id");
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const filterFingerprint = useMemo(
    () =>
      JSON.stringify({
        q, sources, agentIds, groupIds, models, status, failureCauses, dateFrom, dateTo, assignmentId, assignmentRunId,
        limit, sortBy, sortDir,
      }),
    [q, sources, agentIds, groupIds, models, status, failureCauses, dateFrom, dateTo, assignmentId, assignmentRunId, limit, sortBy, sortDir],
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
    failure_cause: failureCauses.length > 0 ? failureCauses : undefined,
    date_from: dateFrom ? `${dateFrom}T00:00:00Z` : undefined,
    date_to: dateTo ? `${dateTo}T23:59:59Z` : undefined,
    assignment_id: assignmentId || undefined,
    assignment_run_id: assignmentRunId || undefined,
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

  const traceListQuery = useTraces(apiFilters, { enabled: readsRows(plan) });
  // A deep link past the offset bound needs the count before its far-end
  // offset is known; one row in the default order is the cheapest read of it.
  const traceCountQuery = useTraces({ ...listFilters, limit: 1 }, { enabled: plan.kind === "count-first" });
  useRecordTraceListTotal(paging, traceListQuery.data?.total, traceCountQuery.data?.total);
  // Every trace the viewer may read, before the card's filters, the status and the search:
  // the total reads "n of N" when they narrow it (#12039).
  const everyTraceQuery = useTraces({ limit: 1 });
  const { data: agents } = useAgents();
  // Only names the chip: a deleted assignment falls back to its id, and a
  // value that is no id is never spliced into the lookup's path.
  const { data: scopedAssignment } = useAssignment(isTraceScopeId(assignmentId) ? assignmentId : "", {
    notFoundIsOrdinary: true,
  });
  const items = useMemo(() => pageRows(plan, traceListQuery.data?.items), [plan, traceListQuery.data]);
  const total = displayedTotal(plan, paging.planningTotal, traceListQuery.data?.total);

  useEffect(() => {
    if (!isKeysetSort || items.length === 0 || !recordsNextCursor(plan, page, limit, total)) return;
    const last = items[items.length - 1];
    if (!last?.started_at || !last?.id) return;
    setPageCursors((prev) => ({
      ...prev,
      [page + 1]: { cursor: last.started_at, cursorId: last.id },
    }));
  }, [isKeysetSort, items, limit, page, plan, total]);

  const modelOptions = useMemo(() => {
    const explicitModels = new Set(models.filter((m) => m !== PLATFORM_DEFAULT_MODEL_FILTER_VALUE));
    let hasPlatformDefaultTraces = false;
    for (const item of items) {
      if (item.model) explicitModels.add(item.model);
      else hasPlatformDefaultTraces = true;
    }
    const values = Array.from(explicitModels).sort((a, b) => a.localeCompare(b));
    // Offer the "Platform default" pseudo-model only when it can actually
    // discriminate (at least one explicit model exists) or when it is already
    // selected via the URL and must stay visible/removable. Otherwise every
    // trace is on the platform default and the Models filter is pure noise.
    if (
      models.includes(PLATFORM_DEFAULT_MODEL_FILTER_VALUE) ||
      (hasPlatformDefaultTraces && values.length > 0)
    ) {
      values.push(PLATFORM_DEFAULT_MODEL_FILTER_VALUE);
    }
    return values.map((model) => ({
      value: model,
      label: formatModelLabel(model === PLATFORM_DEFAULT_MODEL_FILTER_VALUE ? "" : model, t_i18n),
    }));
  }, [items, models, t_i18n]);

  const agentOptions = useMemo(
    () => (agents ?? []).map((agent) => ({ value: agent.id, label: agent.name })),
    [agents],
  );

  // Flat list, ordered by group and prefixed with the group name: the
  // multi-select has no section headers, and "Provider · Rate limited" keeps
  // the grouping legible in both the dropdown and the selected chips — which
  // matters here, because the same word ("timeout") means different things
  // depending on whose side it happened on.
  const failureCauseOptions = useMemo(
    () =>
      FAILURE_CAUSE_GROUPS.flatMap((group) =>
        group.causes.map((cause) => ({
          value: cause,
          label: `${group.label} · ${formatFailureCause(cause)}`,
        })),
      ),
    [],
  );

  const isLoading = traceListQuery.isLoading || traceCountQuery.isLoading;

  // The shared table draws the page the API returned; its state is the page's own URL keys
  // (q, status, sort, dir, page, limit), so every link to /traces keeps working.
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
        // One status narrows; both (or none) is every trace.
        status: statusValues.length === 1 ? statusValues[0] : "",
        sort: isDefault ? "" : nextSort.key,
        dir: isDefault ? "" : nextSort.direction,
        page: next.page > 0 ? String(next.page) : "",
        limit: String(next.pageSize),
      }, false);
    },
    [updateParams],
  );
  const traceColumns = useTraceColumns(costDataAvailable);
  const statusFilters = useMemo<DataTableFilter<LLMTrace>[]>(
    () => [
      {
        key: "status",
        label: "Status",
        // The same words as the Status cells ("OK", "Error"), never the raw API value.
        options: [
          { value: "ok", label: t_i18n(traceStatusLabelKey("ok")) },
          { value: "error", label: t_i18n(traceStatusLabelKey("error")) },
        ],
      },
    ],
    [t_i18n],
  );

  // The filters the card above the table holds: the table counts them with its own and its
  // "Clear all filters" clears them too, so the page has one count and one clear.
  const cardFilterCount = [
    sources.length > 0,
    agentIds.length > 0,
    groupIds.length > 0,
    models.length > 0,
    failureCauses.length > 0,
    !!dateFrom,
    !!dateTo,
    !!assignmentId,
    !!assignmentRunId,
  ].filter(Boolean).length;
  const pageFilterCount = cardFilterCount + (q ? 1 : 0) + (status ? 1 : 0);

  // The page's own filters sit in the table's one Filters pill (#12070): the toolbar keeps
  // one row and the list is the page's last block, so it fits the window (one scroller).
  const tracePills = (
    <div className="w-80 space-y-3" data-testid="traces-filters">
      <div role="group" aria-labelledby="traces-filter-time-range" className="space-y-1">
        <Label id="traces-filter-time-range">{t_i18n("Time range")}</Label>
        <div className="flex flex-wrap items-center gap-2">
          <DateFilterInput aria-label={t_i18n("From")} value={dateFrom} onChange={(value) => updateParams({ date_from: value })} />
          <DateFilterInput aria-label={t_i18n("To")} value={dateTo} onChange={(value) => updateParams({ date_to: value })} />
        </div>
      </div>
      <div role="group" aria-labelledby="traces-filter-sources" className="space-y-1">
        <Label id="traces-filter-sources">{t_i18n("Sources")}</Label>
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
        <Label htmlFor="traces-filter-agents">{t_i18n("Agents")}</Label>
        <MultiSelect
          id="traces-filter-agents"
          options={agentOptions}
          selected={agentIds}
          onToggle={(value) => toggleCsvParam("agent_id", value, agentIds)}
          placeholder={t_i18n("Select agents")}
          searchPlaceholder={t_i18n("Search agents...")}
          emptyOptionsLabel={t_i18n("No agents available")}
          noMatchLabel={t_i18n("No matching agents.")}
        />
      </div>
      {/* Kept in place during the first read, so the panel does not move when the traces
          land and their models fill the filter (#11345). */}
      {(modelOptions.length > 0 || isLoading) && (
        <div className="space-y-1">
          <Label htmlFor="traces-filter-models">{t_i18n("Models")}</Label>
          <MultiSelect
            id="traces-filter-models"
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
      <div className="space-y-1">
        <Label htmlFor="traces-filter-failure-cause">{t_i18n("Failure cause")}</Label>
        <MultiSelect
          id="traces-filter-failure-cause"
          options={failureCauseOptions}
          sort={false}
          selected={failureCauses}
          onToggle={(value) => toggleCsvParam("failure_cause", value, failureCauses)}
          placeholder={t_i18n("Select failure causes")}
          searchPlaceholder={t_i18n("Search failure causes...")}
          emptyOptionsLabel={t_i18n("No failure causes available")}
          noMatchLabel={t_i18n("No matching failure causes.")}
        />
      </div>
    </div>
  );

  return (
    <div className="space-y-6">
      {/* The Observability family header (#9301 N3): its h1 and one tab row. */}
      <NavFamilyHeader
        to="/observability/traces"
        title="Traces"
        infoLabel={t_i18n("About traces")}
        info={t_i18n("Browse and filter LLM traces to debug agent behavior.")}
      />

      {(assignmentId || assignmentRunId) && (
        <div className="flex flex-wrap items-center gap-2">
          <span className="content-compact text-default-secondary">{t_i18n("Scoped to:")}</span>
          {assignmentId && (
            <TraceScopeChip
              icon={Zap}
              label={scopedAssignment?.name ?? t_i18n("Assignment {id}", { id: shortId(assignmentId) })}
              to={`/agents/assignments/${encodeURIComponent(assignmentId)}`}
              removeLabel={t_i18n("Remove the assignment scope")}
              onRemove={() => updateParams({ [ASSIGNMENT_TRACE_PARAM]: "" })}
            />
          )}
          {assignmentRunId && (
            <TraceScopeChip
              icon={Play}
              label={t_i18n("Run {id}", { id: shortId(assignmentRunId) })}
              to={`/operations/executions/${encodeURIComponent(assignmentRunId)}`}
              removeLabel={t_i18n("Remove the run scope")}
              onRemove={() => updateParams({ [ASSIGNMENT_RUN_TRACE_PARAM]: "" })}
            />
          )}
        </div>
      )}

      {plan.kind === "out-of-reach" ? (
        <TraceListOutOfReach
          page={page}
          newestFirst={isKeysetSort}
          onSortNewestFirst={() => updateParams({ sort: "", dir: "" })}
          onFirstPage={() => setPage(0)}
        />
      ) : !isLoading && items.length === 0 && pageFilterCount === 0 ? (
        <EmptyState
          icon={Scan}
          /* FR-027: the copy below talks about filters, which is the
             wrong story on a platform that never produced a trace. */
          requiresAi
          title="No traces found"
          description="No traces match your current filters. Try clearing one or more filters."
        />
      ) : (
        <DataTable
          rows={[...items]}
          columns={traceColumns}
          rowKey={(trace) => trace.id}
          server={{ total }}
          totalAll={everyTraceQuery.data?.total}
          state={tableState}
          onStateChange={onTableState}
          searchPlaceholder={t_i18n("Search trace name...")}
          filters={statusFilters}
          filtersInPill
          filterPills={tracePills}
          externalFilterCount={cardFilterCount}
          onClearAll={clearFilters}
          defaultPageSize={DEFAULT_LIMIT}
          isLoading={isLoading}
          isError={traceListQuery.isError}
          onRetry={() => void traceListQuery.refetch()}
          emptyLabel={t_i18n("No traces found")}
          onRowClick={(trace) => openTraceDrawer(trace.id)}
          rowClassName={() => "cursor-pointer"}
          rowTestId="trace-row"
        />
      )}

      <TracePeekDrawer
        traceId={selectedTraceId}
        open={!!selectedTraceId}
        onOpenChange={onTraceDrawerOpenChange}
      />
    </div>
  );
}

function shortId(id: string): string {
  return id.length > 8 ? `${id.slice(0, 8)}…` : id;
}

/** One removable scope set by an assignment or run shortcut, linking back to it. */
function TraceScopeChip({
  icon: Icon,
  label,
  to,
  removeLabel,
  onRemove,
}: {
  icon: LucideIcon;
  label: string;
  to: string;
  removeLabel: string;
  onRemove: () => void;
}) {
  return (
    <span className="inline-flex items-center gap-1 rounded-sm border border-accent/30 bg-accent/5 py-0.5 pl-2 pr-0.5 content-compact">
      <Icon className="h-3 w-3 text-accent" />
      <Link to={to} className="max-w-[260px] truncate hover:underline">
        {label}
      </Link>
      <Button
        variant="ghost"
        size="icon"
        className="h-5 w-5"
        aria-label={removeLabel}
        onClick={onRemove}
      >
        <X className="h-3 w-3" />
      </Button>
    </span>
  );
}

/** The columns of the trace list; the sortable ones are the `sort_by` values the API allows. */
function useTraceColumns(costDataAvailable: boolean) {
  const { t_i18n, intl } = useFormatter();
  const locale = intl.locale;
  return useMemo<DataTableColumn<LLMTrace>[]>(() => {
    // Server mode: the API sorts, so a sortable column only needs a sortValue to offer its header.
    const sortable = (key: string) => ({ sortValue: () => null, firstSort: SORT_COLUMNS[key] });
    return [
      { key: "started_at", header: "Timestamp", cell: (trace) => <DateTimeCell value={trace.started_at} />, ...sortable("started_at") },
      {
        key: "name",
        header: "Name",
        cell: (trace) => (
          <span className="block max-w-[160px] truncate" title={trace.name}>
            {trace.name}
          </span>
        ),
        ...sortable("name"),
      },
      {
        key: "agent",
        header: "Agent",
        // Plain text: the whole row opens the trace (#12070).
        cell: (trace) =>
          trace.agent_id ? (
            <span className="block max-w-[140px] truncate whitespace-nowrap" title={trace.agent_name ?? trace.agent_id}>
              {trace.agent_name ?? trace.agent_id}
            </span>
          ) : (
            <span className="text-default-secondary">-</span>
          ),
      },
      {
        key: "user",
        header: "User",
        hideable: true,
        // Steps aside in a narrow table (1024) instead of scrolling it sideways.
        hideBelow: "4xl",
        cell: (trace) => (
          <span className="block max-w-[160px] truncate text-default-secondary" title={trace.user_email ?? undefined}>
            {trace.user_email ?? "-"}
          </span>
        ),
      },
      {
        key: "model",
        header: "Model",
        cell: (trace) => {
          const label = formatModelLabel(trace.model, t_i18n);
          return (
            <span className="block max-w-[140px] truncate whitespace-nowrap" title={label}>
              {label}
            </span>
          );
        },
        ...sortable("model"),
      },
      {
        key: "duration_ms",
        header: "Duration",
        align: "right",
        cell: (trace) => <span className="whitespace-nowrap">{formatDuration(trace.duration_ms)}</span>,
        ...sortable("duration_ms"),
      },
      {
        key: "total_tokens",
        header: "Tokens (in/out)",
        align: "right",
        // Score and Tokens step aside in the 1024 table so Cost and Status stay in view
        // without a sideways scroll (UX round 2 on #9728, B1).
        hideBelow: "4xl",
        cell: (trace) => (
          <span className="whitespace-nowrap">
            {formatCompactNumber(trace.input_tokens)} / {formatCompactNumber(trace.output_tokens)}
          </span>
        ),
        ...sortable("total_tokens"),
      },
      {
        key: "total_cost",
        header: "Cost",
        align: "right",
        cell: (trace) =>
          costDataAvailable ? <span className="whitespace-nowrap" title={exactCost(trace.total_cost, locale)}>{formatCost(trace.total_cost, locale)}</span> : <MaskedCost className="inline-flex" />,
        // Masked costs are all the same value on screen: ranking by one would be a sort the user cannot see.
        ...(costDataAvailable ? sortable("total_cost") : {}),
      },
      {
        key: "status",
        header: "Status",
        cell: (trace) => {
          // A failed row's "why" is the badge's tooltip: a second line under the badge
          // made the row taller than its neighbours (UX round 1 on #9728, B1).
          const cause = trace.status !== "ok" ? formatFailureCause(trace.failure_cause) : undefined;
          return (
            // `relative` keeps the absolute sr-only span inside the cell: without it the span
            // escaped the table and scrolled the whole page sideways (UX round 2 on #9728, B5).
            <span className="relative inline-flex whitespace-nowrap" title={cause}>
              <Badge variant={trace.status === "ok" ? "secondary" : "destructive"}>
                {t_i18n(traceStatusLabelKey(trace.status))}
              </Badge>
              {cause && <span className="sr-only">{cause}</span>}
            </span>
          );
        },
        ...sortable("status"),
      },
      {
        key: "score",
        header: "Score",
        hideBelow: "4xl",
        cell: (trace) =>
          trace.has_scores ? (
            <CheckCircle2 className="h-4 w-4 text-accent" aria-label={t_i18n("Score")} />
          ) : (
            <AlertCircle className="h-4 w-4 text-default-secondary" aria-hidden />
          ),
      },
    ];
  }, [costDataAvailable, locale, t_i18n]);
}
