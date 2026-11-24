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

import { useCallback, useMemo, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { Database, Trash2, Bot, Hash, AlertTriangle, CheckCircle2 } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DataTable, TOOLBAR_CONTROL_HEIGHT, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { TruncatedText } from "@/components/ui/truncated-text";
import { VisibilityEmptyHint } from "@/components/ui/visibility-filter";
import { useVisibilityParam, visibilityQuery } from "@/hooks/useVisibilityParam";
import { useSegmentCounts } from "@/hooks/api/useSegmentCounts";
import { EmptyState } from "@/components/ui/empty-state";
import { SelectionBox } from "@/components/ui/list-control-bar";
import {
  OPS_ROW_CARD, OPS_ROW_HEIGHT, OpsChipStrip, OpsExpandToggle, OpsHoverCard, OpsListSkeleton, OpsMoreToken, OpsRowAction, OpsRowGrid, OpsToken, OpsUpdated,
  type OpsFact,
} from "@/components/ui/operations-list";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { useAuth } from "@/hooks/useAuth";
import { useTick } from "@/hooks/useTick";
import { useViewMode } from "@/hooks/useViewMode";
import { cn, pluralize, formatCompactNumber } from "@/lib/utils";
import { REFETCH_INTERVAL_STANDARD } from "@/lib/constants";
import { clearFilterParams, readTableState, type DataTableDefaults } from "@/lib/data-table-state";
import {
  useSharedStates, useSharedStateCount, useDeleteSharedState, useBulkDeleteSharedState,
  useResolveSharedState, useSharedStateOwners,
} from "@/hooks/api/useSharedStateApi";
import { useAgents } from "@/hooks/api/useAgentApi";
import type { SharedState, SharedStateStatus } from "@/types";
import { FleetFilter } from "@/components/fleets/FleetFilter";
import { useFleetParam } from "@/hooks/useFleetParam";
import { offersFleetPicker } from "@/lib/fleet-operations";
import { useFormatter } from "@/i18n";
import { formatInZone, viewerTimeZone } from "@/lib/scheduled-work-items";

/** The actions column: one width on every row (resolve, then delete), as on Worksets. */
const ACTIONS_COLUMN = "w-16";

/**
 * The list view's fixed column widths (px, `layout="fixed"`): Key and Label share what is left,
 * so the table is never wider than its card (UX round 1 on #11896, B1: 1794 px in a 1498 px card
 * at 1728 with the browser sizing the columns from a long key).
 */
const COLUMN_WIDTHS = { status: 200, owner: 200, size: 96, updated: 120 } as const;

/** The table's keys in the URL (`sst_q`, `sst_sort`, `sst_page`, `sst_f_owner`...), and the status filter's (`sst_status`). */
const URL_KEY = "sst_";
const STATUS_PARAM = `${URL_KEY}status`;

/** The sorts the server offers (shared_state.py `LIST_SORTS`); the default is the newest write first. */
const SORT_KEYS = ["key", "owner", "updated", "size"] as const;

const DEFAULTS: DataTableDefaults = { sort: { key: "updated", direction: "desc" }, pageSize: 25 };

/** The empty state's default description, a catalog key EmptyState translates. @i18n-keys */
const EMPTY_DESCRIPTION = {
  default:
    "Shared state is a generic, cross-run key -> value store agents read and write so the whole fleet works from the same source of truth. Agents resolve entries when the thing they track concludes and purge the ones no longer needed. Enable the Shared State built-in tool on an agent to get started.",
} as const;

/**
 * The Status filter, on the toolbar itself (#9378 slice 8c: the status is the filter people
 * reach for first, never behind the Filters pill). "" = Active, the landing view: live state,
 * never resolved noise. Stale is a kind of Active (`stale_only` only ever returns active
 * entries), so it is one of the four values rather than a second control that could contradict
 * the first. All opts into everything.
 */
type StatusKey = "" | "resolved" | "stale" | "all";
const STATUS_FILTERS: { key: StatusKey; label: string }[] = [
  { key: "", label: "Active" },
  { key: "stale", label: "Stale" },
  { key: "resolved", label: "Resolved" },
  { key: "all", label: "All" },
];

/** The status a URL names: `sst_status`, else the legacy `status` / `stale` keys of the former list. */
function readStatus(params: URLSearchParams): StatusKey {
  const raw = params.get(STATUS_PARAM) ?? (params.get("stale") === "stale" ? "stale" : params.get("status") ?? "");
  return raw === "resolved" || raw === "stale" || raw === "all" ? raw : "";
}

/** What the server is asked for a status: the `status` and `stale_only` it filters on. */
function statusQuery(key: StatusKey): { status: SharedStateStatus | undefined; stale_only: boolean } {
  if (key === "all") return { status: undefined, stale_only: false };
  if (key === "resolved") return { status: "resolved", stale_only: false };
  return { status: "active", stale_only: key === "stale" };
}

const STATUS_BADGE: Record<SharedStateStatus, string> = {
  active: "text-accent bg-accent/10",
  resolved: "text-feedback-success-primary bg-feedback-success-primary/10",
};

/** The status chip's words, catalog keys. @i18n-keys */
const STATUS_LABELS = {
  active: "Active",
  resolved: "Resolved",
} as const satisfies Record<SharedStateStatus, string>;

type Translate = (id: string, values?: Record<string, string | number>) => string;

/**
 * `workspace` locks the list on a workspace (its page's Operations tab):
 * what is filed there or owned by its fleet's members, every segment
 * by default. `fleet` locks it on a fleet (its page's Operations tab);
 * elsewhere `?fleet=` narrows it to what a fleet's home members own.
 */
export default function SharedStateTab({
  workspace,
  fleet: lockedFleet,
  emptyDescription,
  toolbarLead,
}: {
  workspace?: string;
  fleet?: string;
  emptyDescription?: string;
  /** A control of the host drawn first on the filter row (the fleet tab's list select), never a row of its own. */
  toolbarLead?: ReactNode;
} = {}) {
  const { t_i18n, locale } = useFormatter();
  useTick(REFETCH_INTERVAL_STANDARD); // keep relative timestamps fresh
  const { isAdmin, user } = useAuth();
  const zone = viewerTimeZone(user?.timezone);
  const [viewMode, setViewMode] = useViewMode("operations-shared-state");
  // Nothing is asked while a `?fleet=` waits on the reader's fleet access.
  const [fleet, setFleet, fleetPending] = useFleetParam(lockedFleet);
  const showFleetPicker = offersFleetPicker({ fleet: lockedFleet, workspace });

  // The table's state lives in the URL (`sst_` keys): the server searches, filters, sorts and
  // pages the whole list (dev-docs/reference/data-tables.md, slice 8c).
  const [searchParams, setSearchParams] = useSearchParams();
  const table = readTableState(searchParams, URL_KEY, DEFAULTS);
  const search = table.search.trim() || undefined;
  const statusKey = readStatus(searchParams);
  const { status, stale_only } = statusQuery(statusKey);
  const owners = table.filters.owner ?? [];
  const ownerQuery = owners.length > 0 ? owners.join(",") : undefined;
  const sortKey = table.sort && (SORT_KEYS as readonly string[]).includes(table.sort.key) ? table.sort.key : "updated";
  const direction = table.sort?.direction ?? "desc";
  const defaultOrder = sortKey === "updated" && direction === "desc";

  // One write: the status, the legacy keys it replaces, and the first page.
  const selectStatus = useCallback(
    (key: StatusKey) =>
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (key) next.set(STATUS_PARAM, key);
          else next.delete(STATUS_PARAM);
          next.delete("status");
          next.delete("stale");
          next.delete(`${URL_KEY}page`);
          return next;
        },
        { replace: true },
      ),
    [setSearchParams],
  );

  // The filters every count shares: everything but the status.
  const base = { search, workspace, fleet, owner_agent_id: ownerQuery };
  const otherFilters = { ...base, status, stale_only };
  // Each visibility segment's count under the other filters, the numbers on the switcher.
  const segmentCounts = useSegmentCounts(["shared-state"], "/shared-state", otherFilters, { enabled: !fleetPending });
  // Mine | Company | Shared | All (`?vis=`); with none named, the first segment
  // holding an entry once the counts are in. All in a workspace, whose
  // list is the team's, and on a fleet.
  const [visibility, setVisibility, landingVisibility] = useVisibilityParam(
    undefined,
    workspace || lockedFleet ? "all" : undefined,
    { counts: segmentCounts, enabled: !workspace && !lockedFleet },
  );
  const landing = landingVisibility || fleetPending;
  const vis = visibilityQuery(visibility);
  const filters = { ...otherFilters, vis };
  const { data: states, isLoading, isError, refetch } = useSharedStates(
    {
      ...filters,
      limit: table.pageSize,
      offset: table.page * table.pageSize,
      sort: defaultOrder ? undefined : (sortKey as (typeof SORT_KEYS)[number]),
      direction: defaultOrder ? undefined : direction,
    },
    !landing,
  );
  // The count of each Status value under the other filters: the numbers on the Status filter.
  const { data: activeCount } = useSharedStateCount({ ...base, vis, status: "active" }, false, !landing);
  const { data: staleSegmentCount } = useSharedStateCount({ ...base, vis, status: "active", stale_only: true }, false, !landing);
  const { data: resolvedCount } = useSharedStateCount({ ...base, vis, status: "resolved" }, false, !landing);
  const { data: allCount } = useSharedStateCount({ ...base, vis }, false, !landing);
  const statusCounts: Record<StatusKey, number | undefined> = {
    "": activeCount?.count,
    stale: staleSegmentCount?.count,
    resolved: resolvedCount?.count,
    all: allCount?.count,
  };
  const totalMatchingCount = statusCounts[statusKey] ?? 0;
  // The same filters over every segment: the empty-Mine hint reads it.
  const { data: allSegmentsCount } = useSharedStateCount(otherFilters, false, !fleetPending);
  // Non-admins can only bulk-delete their OWN rows (company-managed rows are
  // skipped server-side), so "select all matching" promises the *deletable*
  // count, not the visible total.
  const { data: deletableData } = useSharedStateCount(filters, !isAdmin, !landing);
  const deletableMatchingCount = isAdmin ? totalMatchingCount : (deletableData?.count ?? 0);
  // Global stale awareness for the banner (independent of the status and the
  // search). Scoped to *deletable* stale rows for non-admins so its call to
  // resolve or delete them is always actionable.
  const { data: staleCountData } = useSharedStateCount(
    { stale_only: true, vis, workspace, fleet, owner_agent_id: ownerQuery },
    !isAdmin,
    !landing,
  );
  const staleCount = staleCountData?.count ?? 0;
  // Resolves the owner of each entry, so every agent an admin may open.
  const { data: agents } = useAgents({ scope: "all" });
  // What the owner filter offers: the owners of the entries the other filters
  // match, each with its count, the ones the reader may name.
  const { data: ownerRows } = useSharedStateOwners(filters, { enabled: !landing });
  const ownerOptions = useMemo(() => {
    const named = (ownerRows ?? []).flatMap((o) =>
      o.agent?.accessible && o.agent.id
        ? [{ value: o.agent.id, label: `${o.agent.name ?? ""} (${formatCompactNumber(o.count)})` }]
        : [],
    );
    // An owner named in the URL that holds no entry stays on offer, so it can be cleared.
    const orphans = (ownerQuery ? ownerQuery.split(",") : [])
      .filter((id) => !named.some((o) => o.value === id))
      .map((id) => ({ value: id, label: t_i18n("The selected agent (no entry)") }));
    return [...named, ...orphans];
  }, [ownerRows, ownerQuery, t_i18n]);

  const del = useDeleteSharedState();
  const bulkDelete = useBulkDeleteSharedState();
  const resolve = useResolveSharedState();
  const { toast } = useToast();
  const [confirmNode, confirm] = useConfirm();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const agentNames = useMemo(() => {
    const map: Record<string, string> = {};
    (agents ?? []).forEach((a) => { map[a.id] = a.name; });
    return map;
  }, [agents]);
  const ownerName = useCallback(
    (s: SharedState) => (s.owner_agent_id ? (agentNames[s.owner_agent_id] ?? t_i18n("an agent you cannot open")) : null),
    [agentNames, t_i18n],
  );

  // A company-managed entry is read-only to non-admins, whoever its recorded user is: the
  // backend refuses the resolve / delete and leaves it out of a non-admin's bulk delete.
  const canManage = useCallback((s: SharedState) => !s.is_company_managed || isAdmin, [isAdmin]);

  const toggleExpand = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  // -- Selection (only the entries the reader may delete are selectable) --
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [allMatchingSelected, setAllMatchingSelected] = useState(false);

  // Clear the selection when a filter changes (adjust-state-during-render, as the other
  // Operations lists: no extra commit, no flash of a stale selection).
  const filterKey = `${statusKey}|${visibility}|${search ?? ""}|${ownerQuery ?? ""}|${fleet ?? ""}`;
  const [prevFilterKey, setPrevFilterKey] = useState(filterKey);
  if (prevFilterKey !== filterKey) {
    setPrevFilterKey(filterKey);
    setSelectedIds([]);
    setAllMatchingSelected(false);
  }

  const rows = landing ? [] : (states ?? []);
  const selectableIds = rows.filter(canManage).map((s) => s.id);
  const allVisibleSelected = selectableIds.length > 0 && selectableIds.every((id) => selectedIds.includes(id));
  const hasMoreBeyondVisible = deletableMatchingCount > selectableIds.length;
  // Not in a workspace, a fleet or on chosen owners: bulk delete by filters has no such scope.
  const canSelectAllMatching = !workspace && !fleet && !ownerQuery && allVisibleSelected && hasMoreBeyondVisible;

  const changeSelection = (keys: string[]) => {
    setAllMatchingSelected(false);
    setSelectedIds(keys);
  };
  const clearSelection = () => changeSelection([]);
  const toggleOne = (id: string) =>
    changeSelection(selectedIds.includes(id) ? selectedIds.filter((key) => key !== id) : [...selectedIds, id]);

  const handleResolve = async (s: SharedState) => {
    try {
      // Resolve the exact row by id (not key): multiple visible rows can share a
      // key, so a key-addressed resolve could flip a different entry than shown.
      await resolve.mutateAsync(s.id);
      toast({ title: "Shared state resolved", variant: "success" });
    } catch {
      toast({ title: "Failed to resolve shared state", variant: "error" });
    }
  };

  const handleDelete = async (s: SharedState) => {
    const ok = await confirm({
      title: "Delete shared state?",
      description: `Entry "${s.label || s.key}" (${s.key}) will be permanently removed.`,
      confirmLabel: t_i18n("Delete"),
      variant: "danger",
    });
    if (!ok) return;
    try {
      await del.mutateAsync(s.id);
      toast({ title: "Shared state deleted", variant: "success" });
    } catch {
      toast({ title: "Failed to delete shared state", variant: "error" });
    }
  };

  const handleBulkDelete = async () => {
    const title = allMatchingSelected
      ? t_i18n("Delete {count, plural, one {# entry} other {# entries}} matching current filters?", { count: deletableMatchingCount })
      : t_i18n("Delete {count, plural, one {# selected entry} other {# selected entries}}?", { count: selectedIds.length });
    const ok = await confirm({
      title,
      description: t_i18n("This action cannot be undone."),
      confirmLabel: t_i18n("Delete"),
      variant: "danger",
    });
    if (!ok) return;
    const payload = allMatchingSelected ? { all_matching_filters: true, ...filters } : { ids: selectedIds };
    bulkDelete.mutate(payload, {
      onSuccess: (data) => {
        toast({
          title: `${formatCompactNumber(data.deleted)} ${pluralize(data.deleted, "entry")} deleted`,
          variant: "success",
        });
        clearSelection();
      },
      onError: () => toast({ title: "Failed to delete shared state", variant: "error" }),
    });
  };

  const rowActions = (s: SharedState) =>
    canManage(s) ? (
      <span className="flex items-center justify-end gap-1">
        {s.status === "active" && (
          <OpsRowAction icon={CheckCircle2} tone="success" label={t_i18n("Resolve")} onClick={() => void handleResolve(s)} busy={resolve.isPending} />
        )}
        <OpsRowAction icon={Trash2} tone="danger" label={t_i18n("Delete")} onClick={() => void handleDelete(s)} busy={del.isPending} />
      </span>
    ) : null;

  const columns = useMemo(() => sharedStateColumns(ownerName, t_i18n), [ownerName, t_i18n]);
  const tableFilters = useMemo<DataTableFilter<SharedState>[]>(
    () => [{ key: "owner", label: "Owner", options: ownerOptions }],
    [ownerOptions],
  );

  // The Status filter: on the toolbar row, each value with its count, never in the Filters pill.
  const statusFilter = (
    <SegmentedFilter<StatusKey>
      selection="radio"
      ariaLabel={t_i18n("Status filter")}
      testId="shared-state-status-filter"
      options={STATUS_FILTERS.map((option) => ({
        ...option,
        count: statusCounts[option.key],
        countPending: landing || statusCounts[option.key] === undefined,
      }))}
      value={statusKey}
      onChange={selectStatus}
    />
  );

  const emptyTitle =
    search || statusKey === "resolved" || statusKey === "stale" || ownerQuery
      ? "No shared state matches your filters."
      : statusKey === "all"
        ? "No shared state"
        : "No active shared state";

  return (
    <div className="space-y-4" data-testid="shared-state-list">
      {confirmNode}

      {staleCount > 0 && (
        <div
          className="flex items-center gap-2 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/10 px-4 py-3 content-base text-feedback-warning-tertiary"
          data-testid="shared-state-stale-banner"
        >
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span>
            {t_i18n(
              "{count, plural, one {# active entry has gone quiet} other {# active entries have gone quiet}}. Resolve the ones whose thing is done, or delete the ones no longer needed, so stale state does not pile up.",
              { count: staleCount },
            )}
          </span>
          {statusKey !== "stale" && (
            <Button variant="ghost" size="sm" className="ml-auto h-7 content-compact text-feedback-warning-primary" onClick={() => selectStatus("stale")}>
              {t_i18n("Show stale")}
            </Button>
          )}
        </div>
      )}

      {/* One records table (dev-docs/reference/data-tables.md, slice 8c): search, Visibility,
          the Status filter (on the row itself), the owner and fleet filters (Filters pill), sort,
          Columns, total, page size and URL state are the table's. The grid view keeps each row
          on the Operations row contract (OpsRowGrid) as its card; the list view draws columns. */}
      <DataTable<SharedState>
        rows={rows}
        columns={columns}
        rowKey={(s) => s.id}
        urlKey={URL_KEY}
        storageKey="operations-shared-state"
        defaultSort={DEFAULTS.sort ?? undefined}
        defaultPageSize={DEFAULTS.pageSize}
        server={{ total: totalMatchingCount }}
        searchPlaceholder={t_i18n("Search key or label...")}
        toolbarLead={
          // The filter row the e2e budget measures (#10215 criterion 6), as on Work items: the
          // toolbar's right group (total, page size, view) takes its own line when this lead crowds it.
          <span className="flex items-center gap-2" data-testid="shared-state-filter-row">
            {toolbarLead}
            {statusFilter}
          </span>
        }
        scope={{ value: visibility, onChange: setVisibility, counts: segmentCounts }}
        filters={tableFilters}
        // One filter row at 1440 and 1024 (#10215 criterion 6): the owner and the fleet
        // pickers sit in the Filters pill; the Status filter stays on the row.
        filtersInPill
        filterPills={showFleetPicker ? <FleetFilter value={fleet} onChange={setFleet} /> : undefined}
        externalFilterCount={showFleetPicker && fleet ? 1 : 0}
        // Clear all clears the fleet it counts with the table's own filters, in one write (#1001).
        onClearAll={() => setSearchParams((prev) => clearFilterParams(prev, lockedFleet ? [] : ["fleet", "agent"], URL_KEY), { replace: true })}
        exportName="shared-state"
        isLoading={landing || isLoading}
        isError={isError}
        onRetry={() => void refetch()}
        emptyLabel={t_i18n(emptyTitle)}
        noResultsLabel={t_i18n("No shared state matches your filters.")}
        emptyContent={
          <VisibilityEmptyHint
            value={visibility}
            counts={allSegmentsCount ? { mine: totalMatchingCount, all: allSegmentsCount.count } : undefined}
            onShowAll={() => setVisibility("all")}
          >
            <EmptyState icon={Database} title={emptyTitle} description={emptyDescription ?? EMPTY_DESCRIPTION.default} />
          </VisibilityEmptyHint>
        }
        selection={{
          selected: selectedIds,
          onChange: changeSelection,
          count: allMatchingSelected ? deletableMatchingCount : undefined,
          disabledReason: (id) => {
            const s = rows.find((row) => row.id === id);
            return s && !canManage(s) ? t_i18n("Only an administrator can manage a company-managed entry.") : undefined;
          },
          actions: (
            <>
              {canSelectAllMatching && !allMatchingSelected && (
                <Button variant="ghost" size="sm" className={TOOLBAR_CONTROL_HEIGHT} onClick={() => setAllMatchingSelected(true)}>
                  {t_i18n("select all {count}", { count: formatCompactNumber(deletableMatchingCount) })}
                </Button>
              )}
              <Button variant="ghost" size="sm" className={TOOLBAR_CONTROL_HEIGHT} onClick={clearSelection}>
                {t_i18n("Clear")}
              </Button>
              <Button
                variant="destructive"
                size="sm"
                className={TOOLBAR_CONTROL_HEIGHT}
                onClick={() => void handleBulkDelete()}
                disabled={bulkDelete.isPending}
                data-testid="shared-state-bulk-delete"
              >
                <Trash2 className="h-3.5 w-3.5" aria-hidden /> {t_i18n("Delete")}
              </Button>
            </>
          ),
        }}
        rowTestId="shared-state-table-row"
        onRowClick={(s) => toggleExpand(s.id)}
        expandedRow={(s) => (expanded.has(s.id) ? <SharedStateDetail state={s} zone={zone} locale={locale} /> : null)}
        rowActions={rowActions}
        rowActionsWidth={64}
        // Fixed widths on Status, Owner, Size and Updated: Key and Label share the rest, the
        // table never outgrows its card (UX round 1 on #11896, B1).
        layout="fixed"
        view={{ value: viewMode, onChange: setViewMode }}
        renderGrid={
          viewMode === "grid"
            ? (gridRows) => (
                // The page box of the grid is the DataTable's, first in its toolbar: no row of its own here.
                <div className="space-y-2">
                  {gridRows.map((s) => (
                    <SharedStateRow
                      key={s.id}
                      state={s}
                      owner={ownerName(s)}
                      manageable={canManage(s)}
                      selected={selectedIds.includes(s.id)}
                      onSelect={() => toggleOne(s.id)}
                      open={expanded.has(s.id)}
                      onToggle={() => toggleExpand(s.id)}
                      actions={rowActions(s)}
                      zone={zone}
                      locale={locale}
                    />
                  ))}
                </div>
              )
            : undefined
        }
        // The grid view's first read: a window's worth of rows on the Operations row
        // contract (OpsRowSkeleton), so the cards land without a layout shift.
        renderGridSkeleton={(pageSize) => (
          <OpsListSkeleton label={t_i18n("Loading...")} pageSize={pageSize} actionsClassName={ACTIONS_COLUMN} actions={2} lead={2} />
        )}
      />
    </div>
  );
}

/**
 * The columns of the Shared State list: the cells of the list view (Columns chooses them per
 * user), the toolbar sort of the grid view and the CSV export. Every sort is the server's.
 */
function sharedStateColumns(ownerName: (s: SharedState) => string | null, t_i18n: Translate): DataTableColumn<SharedState>[] {
  return [
    {
      key: "key",
      header: "Key",
      cell: (s) => <TruncatedText text={s.key} className="font-mono" />,
      sortValue: (s) => s.key.toLowerCase(),
      csv: (s) => s.key,
    },
    {
      key: "label",
      header: "Label",
      hideable: true,
      hideBelow: "4xl",
      cell: (s) => <TruncatedText text={s.label ?? ""} />,
      csv: (s) => s.label ?? "",
    },
    {
      key: "status",
      header: "Status",
      hideable: true,
      width: COLUMN_WIDTHS.status,
      cell: (s) => (
        <span className="flex items-center gap-1 overflow-hidden">
          <Badge variant="secondary" className={cn("content-compact", STATUS_BADGE[s.status])}>{t_i18n(STATUS_LABELS[s.status])}</Badge>
          {s.is_stale && <Badge variant="secondary" className="content-compact text-feedback-warning-tertiary bg-feedback-alert-primary/10">{t_i18n("Stale")}</Badge>}
          {s.over_soft_limit && <Badge variant="secondary" className="content-compact text-feedback-warning-tertiary bg-feedback-alert-primary/10">{t_i18n("Oversized")}</Badge>}
        </span>
      ),
      csv: (s) => (s.is_stale ? `${s.status} (stale)` : s.status),
    },
    {
      key: "owner",
      header: "Owner",
      hideable: true,
      hideBelow: "4xl",
      width: COLUMN_WIDTHS.owner,
      cell: (s) => <TruncatedText text={ownerName(s) ?? ""} />,
      sortValue: (s) => (ownerName(s) ?? "").toLowerCase(),
      csv: (s) => ownerName(s) ?? "",
    },
    {
      key: "size",
      header: "Size",
      hideable: true,
      hideBelow: "6xl",
      width: COLUMN_WIDTHS.size,
      align: "right",
      cell: (s) => (typeof s.size_chars === "number" ? `${formatCompactNumber(s.size_chars)} ${t_i18n("chars")}` : ""),
      sortValue: (s) => s.size_chars ?? 0,
      csv: (s) => s.size_chars ?? "",
    },
    {
      key: "updated",
      header: "Updated",
      hideable: true,
      width: COLUMN_WIDTHS.updated,
      align: "right",
      cell: (s) => <OpsUpdated at={s.updated_at} />,
      sortValue: (s) => s.updated_at ?? "",
      csv: (s) => s.updated_at ?? "",
    },
  ];
}

/** What unfolds under an entry: its value and its latest notes. */
function SharedStateDetail({ state: s, zone, locale }: { state: SharedState; zone: string; locale: string }) {
  const { t_i18n } = useFormatter();
  const valueKeys = Object.keys(s.value ?? {});
  return (
    <div className="space-y-3 border-t border-elevation-subtle p-4" onClick={(e) => e.stopPropagation()} data-testid="shared-state-detail">
      {valueKeys.length > 0 && (
        <pre className="overflow-x-auto rounded-sm bg-elevation-hover p-3 content-compact text-default-secondary">
          {JSON.stringify(s.value, null, 2)}
        </pre>
      )}
      {s.notes.length > 0 && (
        <div className="space-y-1">
          <p className="content-compact uppercase tracking-wider text-default-secondary">{t_i18n("Notes")}</p>
          {s.notes.slice(-20).map((n, i) => (
            <p key={i} className="content-compact text-default-secondary">
              <span className="text-default-disabled">[{n.at ? formatInZone(n.at, zone, locale) : ""}]</span> {n.note}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * One entry as a card of the grid view, on the Operations row contract: one height on every
 * row (OPS_ROW_HEIGHT), nothing wraps; the value and the notes unfold under it.
 */
function SharedStateRow({
  state: s,
  owner,
  manageable,
  selected,
  onSelect,
  open: isOpen,
  onToggle: toggle,
  actions,
  zone,
  locale,
}: {
  state: SharedState;
  owner: string | null;
  manageable: boolean;
  selected: boolean;
  onSelect: () => void;
  open: boolean;
  onToggle: () => void;
  actions: ReactNode;
  zone: string;
  locale: string;
}) {
  const { t_i18n } = useFormatter();
  const valueKeys = Object.keys(s.value ?? {});
  const latestNote = [...s.notes].reverse().find((n) => n.note?.trim())?.note?.trim() ?? "";
  const title = s.label || s.key;
  // Every fact of the entry, in the contract's order: who owns it,
  // the state facts, the key, the contents, then the timestamps.
  const facts: OpsFact[] = [];
  if (owner) facts.push({ label: t_i18n("Owner"), value: owner });
  facts.push({ label: t_i18n("Status"), value: t_i18n(STATUS_LABELS[s.status]) });
  if (s.is_stale) facts.push({ label: t_i18n("Stale"), tone: "warning", value: t_i18n("No write within the staleness window.") });
  if (s.over_soft_limit) {
    facts.push({ label: t_i18n("Oversized"), tone: "warning", value: t_i18n("Over the size limit. Bound its longest list or archive its old items.") });
  }
  if (s.is_company_managed) facts.push({ label: t_i18n("Company-managed"), value: t_i18n("Only an administrator can manage a company-managed entry.") });
  facts.push({ label: t_i18n("Key"), value: <span className="font-mono">{s.key}</span> });
  facts.push({
    label: t_i18n("Contents"),
    value: `${t_i18n("{count, plural, one {# field} other {# fields}}", { count: valueKeys.length })}, ${t_i18n("{count, plural, one {# note} other {# notes}}", { count: s.notes.length })}`,
  });
  if (typeof s.size_chars === "number") {
    const size = t_i18n("{count, plural, one {# character} other {# characters}}", { count: s.size_chars });
    facts.push({
      label: t_i18n("Size"),
      value: s.largest_list
        ? `${size}, ${t_i18n("{count, plural, one {longest list, # item} other {longest list, # items}}", { count: s.largest_list })}`
        : size,
      testId: "shared-state-fact-size",
    });
  }
  if (latestNote) facts.push({ label: t_i18n("Latest note"), value: latestNote, testId: "shared-state-fact-note" });
  if (s.created_at) facts.push({ label: t_i18n("Created"), value: formatInZone(s.created_at, zone, locale) });
  if (s.updated_at) facts.push({ label: t_i18n("Updated"), value: formatInZone(s.updated_at, zone, locale) });
  if (s.resolved_at) facts.push({ label: t_i18n("Resolved"), value: formatInZone(s.resolved_at, zone, locale) });
  // The row shows the status (and the stale and oversized chips), the
  // owner, the key and line 2; "+N" counts what only the hover card holds.
  const shownFacts = 3 + (owner ? 1 : 0) + (s.is_stale ? 1 : 0) + (s.over_soft_limit ? 1 : 0) + (s.is_company_managed ? 1 : 0);
  const hiddenFacts = Math.max(0, facts.length - shownFacts);
  return (
    <Card
      data-testid="shared-state-row"
      data-shared-state-id={s.id}
      onClick={toggle}
      className={cn(
        OPS_ROW_CARD,
        "group cursor-pointer overflow-hidden transition-all hover:border-elevation-default",
        s.is_stale && "border-feedback-alert-primary/30",
        selected && "border-elevation-default bg-elevation-highlight/[0.04]",
      )}
    >
      <CardContent className="p-0">
        {/* The row: one height on every row (OPS_ROW_HEIGHT), nothing wraps; the value and the notes unfold under it. */}
        <div className={cn(OPS_ROW_HEIGHT, "px-4 py-2.5")} data-testid="shared-state-line">
          <OpsRowGrid
            actionsClassName={ACTIONS_COLUMN}
            actionsTestId="shared-state-actions"
            lead={
              <>
                {/* Every row keeps one selection column: a row the reader cannot manage shows its box disabled, with why. */}
                {manageable ? (
                  <SelectionBox checked={selected} onToggle={onSelect} label={selected ? t_i18n("Deselect entry") : t_i18n("Select entry")} />
                ) : (
                  <SelectionBox
                    checked={false}
                    onToggle={() => undefined}
                    label={t_i18n("Not selectable")}
                    disabled
                    reason={t_i18n("Only an administrator can manage a company-managed entry.")}
                  />
                )}
                <OpsExpandToggle open={isOpen} onToggle={toggle} />
              </>
            }
            main={
              <>
                <div className="flex min-w-0 items-center gap-2 overflow-hidden whitespace-nowrap" data-testid="shared-state-line1">
                  <OpsHoverCard title={title} facts={facts}>
                    <button
                      type="button"
                      className="min-w-[4rem] shrink truncate text-left font-medium hover:text-accent focus-visible:outline-none focus-visible:underline"
                      data-testid="shared-state-title"
                      aria-expanded={isOpen}
                      onClick={(e) => {
                        e.stopPropagation();
                        toggle();
                      }}
                    >
                      {title}
                    </button>
                  </OpsHoverCard>
                  <span className="flex shrink-0 items-center">
                    <Badge variant="secondary" className={cn("content-compact", STATUS_BADGE[s.status])} data-testid="shared-state-status">
                      {t_i18n(STATUS_LABELS[s.status])}
                    </Badge>
                  </span>
                  <OpsChipStrip testId="shared-state-chips">
                    {s.is_stale && (
                      <Badge variant="secondary" className="content-compact text-feedback-warning-tertiary bg-feedback-alert-primary/10 gap-1">
                        <AlertTriangle className="h-3 w-3" aria-hidden="true" /> {t_i18n("Stale")}
                      </Badge>
                    )}
                    {s.over_soft_limit && (
                      <Badge variant="secondary" className="content-compact text-feedback-warning-tertiary bg-feedback-alert-primary/10 gap-1" data-testid="shared-state-oversized">
                        <Database className="h-3 w-3" aria-hidden="true" /> {t_i18n("Oversized")}
                      </Badge>
                    )}
                    {s.is_company_managed && <Badge variant="outline" className="content-compact">{t_i18n("Company-managed")}</Badge>}
                  </OpsChipStrip>
                </div>
                {/* Line 2, one line: the latest note, else what the value holds. */}
                <div className="min-w-0 truncate content-compact text-default-secondary" data-testid="shared-state-summary">
                  {latestNote || t_i18n("{count, plural, one {# field} other {# fields}}", { count: valueKeys.length })}
                </div>
              </>
            }
            meta={
              <>
                {owner ? (
                  <OpsToken icon={Bot} full={owner} testId="shared-state-owner">{owner}</OpsToken>
                ) : (
                  <span aria-hidden="true" className="h-4" />
                )}
                <span className="flex min-w-0 items-center gap-2">
                  <OpsToken icon={Hash} tone="muted" full={s.key} copy={s.key} copyLabel={t_i18n("Copy the key")} testId="shared-state-key">
                    {s.key}
                  </OpsToken>
                  <OpsMoreToken count={hiddenFacts} title={title} facts={facts} onOpen={toggle} testId="shared-state-more" />
                </span>
              </>
            }
            updated={<OpsUpdated at={s.updated_at} testId="shared-state-updated" />}
            actions={actions}
          />
        </div>
        {isOpen && <SharedStateDetail state={s} zone={zone} locale={locale} />}
      </CardContent>
    </Card>
  );
}
