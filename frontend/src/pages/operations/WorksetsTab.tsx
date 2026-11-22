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
 * Worksets — the working corpora agents build.
 *
 * The question this view exists to answer is "what did it actually collect,
 * and what did it leave out?". So the honesty the tools carry is carried here
 * too, in the same words: an unsealed corpus is never shown as complete, an
 * evicted item is named as no longer searchable, a declared total that
 * disagrees with what arrived is shown disagreeing, and an ETA appears only
 * once enough items have finished to mean one.
 *
 * Read and purge only. Nothing here writes a summary or a priority: those are
 * conclusions about a document, and a corpus whose analysis can be hand-edited
 * stops being able to say where any of it came from.
 */

import { useMemo, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  Hash,
  Library,
  Lock,
  Trash2,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { EmptyState } from "@/components/ui/empty-state";
import { Tip } from "@/components/ui/tooltip";
import { TruncatedText } from "@/components/ui/truncated-text";
import {
  OPS_ROW_CARD, OPS_ROW_HEIGHT, OpsChipStrip, OpsExpandToggle, OpsHoverCard, OpsListSkeleton, OpsMoreToken, OpsRowAction, OpsRowGrid, OpsToken, OpsUpdated,
  type OpsFact,
} from "@/components/ui/operations-list";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { useTick } from "@/hooks/useTick";
import { useViewMode } from "@/hooks/useViewMode";
import { REFETCH_INTERVAL_STANDARD } from "@/lib/constants";
import { clearFilterParams, readTableState, type DataTableDefaults } from "@/lib/data-table-state";
import { cn, formatCompactNumber, pluralize } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import {
  useDeleteWorkset,
  useResolveWorkset,
  useWorkset,
  useWorksetCount,
  useWorksetItems,
  useWorksets,
} from "@/hooks/api/useWorksetApi";
import type { Workset, WorksetItemStatus } from "@/types";
import { FleetFilter } from "@/components/fleets/FleetFilter";
import { useFleetParam } from "@/hooks/useFleetParam";
import { offersFleetPicker } from "@/lib/fleet-operations";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { VisibilityEmptyHint } from "@/components/ui/visibility-filter";
import { useVisibilityParam, visibilityQuery } from "@/hooks/useVisibilityParam";
import { useSegmentCounts } from "@/hooks/api/useSegmentCounts";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useAuth } from "@/hooks/useAuth";
import { formatInZone, viewerTimeZone } from "@/lib/scheduled-work-items";
import { WORKSET_COLLECTION_LABELS, worksetCollection } from "@/lib/workset-collection";
import { Spinner } from "@filigran/design-system";

/** The actions column: one width on every row (resolve, then delete). */
const ACTIONS_COLUMN = "w-16";

const ITEM_STATUS_STYLE: Record<WorksetItemStatus, string> = {
  pending: "text-feedback-warning-tertiary bg-feedback-alert-primary/10",
  ready: "text-feedback-success-primary bg-feedback-success-primary/10",
  consumed: "text-feedback-success-primary bg-feedback-success-primary/10",
  discarded: "text-default-secondary bg-elevation-highlight",
  evicted: "text-feedback-warning-tertiary bg-feedback-warning-primary/10",
  failed: "text-feedback-error-primary bg-feedback-error-primary/10",
};

/** The scope's words, catalog keys. @i18n-keys */
const SCOPE_LABELS = {
  run: "This run",
  flow: "This flow",
  global: "Global",
} as const satisfies Record<Workset["scope"], string>;

function ScopeBadge({ workset }: { workset: Workset }) {
  const { t_i18n } = useFormatter();
  return (
    <Badge variant="outline" className="content-compact">
      {t_i18n(SCOPE_LABELS[workset.scope])}
    </Badge>
  );
}

function WorksetDetailPanel({ worksetId }: { worksetId: string }) {
  const { t_i18n } = useFormatter();
  const { data: detail, isLoading } = useWorkset(worksetId);
  const { data: page } = useWorksetItems(worksetId, { limit: 50 });

  if (isLoading || !detail) {
    return (
      <div className="flex items-center gap-2 p-4 content-compact text-default-secondary">
        <Spinner size="sm" tone="inherit" />
        {t_i18n("Loading…")}
      </div>
    );
  }

  const { coverage, progress } = detail;

  return (
    <div className="space-y-4 border-t border-elevation-subtle p-4">
      {/* The coverage sentence, verbatim from the service — the operator reads
          exactly what the agents read. */}
      <p className="content-compact text-default-secondary">{coverage.description}</p>

      <div className="flex flex-wrap gap-2">
        <Badge
          className={cn(
            "content-compact",
            coverage.complete
              ? "text-feedback-success-primary bg-feedback-success-primary/10"
              : "text-feedback-warning-tertiary bg-feedback-alert-primary/10",
          )}
        >
          {coverage.complete ? t_i18n("Complete") : t_i18n("Not complete")}
        </Badge>
        {coverage.sealed ? (
          <Badge variant="outline" className="content-compact">
            <Lock className="mr-1 h-3 w-3" />
            {t_i18n("Sealed")}
          </Badge>
        ) : detail.implicit ? (
          <Badge variant="outline" className="content-compact" data-testid="workset-detail-collection">
            {t_i18n(WORKSET_COLLECTION_LABELS.rolling)}
          </Badge>
        ) : (
          <Badge variant="outline" className="content-compact text-feedback-warning-primary" data-testid="workset-detail-collection">
            {t_i18n("Still collecting")}
          </Badge>
        )}
        {coverage.failed > 0 && (
          <Badge className="content-compact text-feedback-error-primary bg-feedback-error-primary/10">
            <AlertTriangle className="mr-1 h-3 w-3" />
            {coverage.failed} {t_i18n("failed")}
          </Badge>
        )}
        {coverage.evicted > 0 && (
          <Tip
            content={t_i18n(
              "Their content was reclaimed to stay inside the budget. Summaries remain; they are no longer searchable.",
            )}
          >
            <Badge className="content-compact text-feedback-warning-tertiary bg-feedback-warning-primary/10">
              {coverage.evicted} {t_i18n("evicted")}
            </Badge>
          </Tip>
        )}
        {/* The SPEND ceiling, distinct from the storage one above: an item
            with no summary means something different when the corpus simply
            ran out of budget, and that is a setting to change rather than a
            failure to investigate. */}
        {coverage.auto_budget_exhausted && (
          <Tip
            content={t_i18n(
              "This corpus has spent its automatic summarisation budget. Anything still unsummarised needs an agent, or a larger budget.",
            )}
          >
            <Badge className="content-compact text-feedback-warning-tertiary bg-feedback-alert-primary/10">
              {t_i18n("Summary budget spent")}
            </Badge>
          </Tip>
        )}
      </div>

      {detail.processing === "auto" && (
        <p className="content-compact text-default-secondary">
          {t_i18n("Automatic summaries")}: {detail.auto_calls_used} /{" "}
          {detail.max_auto_calls} {t_i18n("calls")}
        </p>
      )}

      {/* Progress, including the refusal to estimate. */}
      {(progress.remaining > 0 || progress.stalled) && (
        <div className="rounded-sm border border-elevation-subtle p-3">
          <p className="content-compact text-default-secondary">{progress.description}</p>
          {progress.stalled && (
            <p className="mt-1 flex items-center gap-1 content-compact text-feedback-error-primary">
              <AlertTriangle className="h-3.5 w-3.5" />
              {t_i18n("Nothing has completed recently: check the processor.")}
            </p>
          )}
          {progress.eta_seconds === null && progress.remaining > 0 && (
            <p className="mt-1 content-compact text-default-secondary">
              {t_i18n("No estimate yet: too few items have finished.")}{" "}
              {progress.samples} {t_i18n("observed")}.
            </p>
          )}
        </div>
      )}

      {/* The corpus itself: summaries and metadata, never source text. */}
      {page && page.items.length > 0 && (
        <div className="space-y-1.5">
          <p className="content-compact font-semibold uppercase tracking-wide text-default-secondary">
            {t_i18n("Items")} ({page.total})
          </p>
          {page.items.map((item) => (
            <div
              key={item.id}
              className="rounded-sm border border-elevation-subtle p-2 content-compact"
            >
              <div className="flex items-start justify-between gap-2">
                <span className="font-medium">{item.title}</span>
                <div className="flex shrink-0 items-center gap-1">
                  {item.priority_score !== null && (
                    <Badge variant="outline" className="content-compact">
                      {item.priority_score}
                    </Badge>
                  )}
                  <Badge
                    className={cn("content-compact", ITEM_STATUS_STYLE[item.status])}
                  >
                    {item.status}
                  </Badge>
                </div>
              </div>
              <p className="mt-1 content-compact text-default-secondary">
                {item.summary || t_i18n("(not summarised yet)")}
              </p>
              <div className="mt-1 flex flex-wrap items-center gap-2 content-compact text-default-secondary">
                {item.occurred_at && <span>{item.occurred_at.slice(0, 10)}</span>}
                {item.revision > 1 && (
                  <Tip
                    content={t_i18n(
                      "The source changed after it was first collected, so its summary was rewritten.",
                    )}
                  >
                    <span>
                      {t_i18n("revision")} {item.revision}
                    </span>
                  </Tip>
                )}
                {item.corroboration_count > 0 && (
                  <span>
                    {t_i18n("corroborated by")} {item.corroboration_count}
                  </span>
                )}
                {item.chars > 0 && (
                  <span>{formatCompactNumber(item.chars)} {t_i18n("chars")}</span>
                )}
              </div>
            </div>
          ))}
          {page.total > page.items.length && (
            <p className="content-compact text-default-secondary">
              {t_i18n("Showing")} {page.items.length} {t_i18n("of")} {page.total}.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * `workspace` locks the list on a workspace (its page's Operations tab):
 * what is filed there or owned by its fleet's members, every segment
 * by default. `fleet` locks it on a fleet (its page's Operations tab);
 * elsewhere `?fleet=` narrows it to what a fleet's home members own.
 */
export default function WorksetsTab({
  workspace,
  fleet: lockedFleet,
  toolbarLead,
}: {
  workspace?: string;
  fleet?: string;
  /** A control of the host drawn first on the filter row (the fleet tab's list select), never a row of its own. */
  toolbarLead?: ReactNode;
} = {}) {
  useTick(REFETCH_INTERVAL_STANDARD);
  const { t_i18n, locale } = useFormatter();
  const { user } = useAuth();
  const zone = viewerTimeZone(user?.timezone);
  // Resolves the owner of each workset, as on Shared State.
  const { data: agents } = useAgents({ scope: "all" });
  const agentName = (id: string | null) =>
    id ? (agents?.find((a) => a.id === id)?.name ?? t_i18n("an agent you cannot open")) : null;
  const [confirmNode, confirm] = useConfirm();
  const { toast } = useToast();
  const [viewMode, setViewMode] = useViewMode("operations-worksets");
  const [expanded, setExpanded] = useState<string | null>(null);
  // Nothing is asked while a `?fleet=` waits on the reader's fleet access.
  const [fleet, setFleet, fleetPending] = useFleetParam(lockedFleet);
  const showFleetPicker = offersFleetPicker({ fleet: lockedFleet, workspace });

  // The table's state lives in the URL (`wks_` keys): the server searches, filters, sorts and
  // pages the whole list (dev-docs/reference/data-tables.md, slice 8b).
  const [searchParams, setSearchParams] = useSearchParams();
  const table = readTableState(searchParams, URL_KEY, DEFAULTS);
  const search = table.search.trim() || undefined;
  const statuses = (table.filters.status ?? []).filter((value): value is "active" | "resolved" => value === "active" || value === "resolved");
  const statusFilter = statuses.length === 1 ? statuses[0] : undefined;
  const scopes = (table.filters.scope ?? []).filter((value) => value in SCOPE_LABELS);
  const scope = scopes.length > 0 ? scopes.join(",") : undefined;
  const sortKey = table.sort && (SORT_KEYS as readonly string[]).includes(table.sort.key) ? table.sort.key : "updated";
  const direction = table.sort?.direction ?? "desc";
  const narrowing = { search, status: statusFilter, scope, workspace, fleet };

  // Each segment's count under the other filters, the numbers on the filter.
  const segmentCounts = useSegmentCounts(
    ["worksets"],
    "/worksets",
    { search, status_filter: statusFilter, scope, workspace, fleet },
    { enabled: !fleetPending },
  );
  // Mine | Company | Shared | All (`?vis=`); with none named, the first segment
  // holding a workset once the counts are in. All in a workspace, whose
  // list is the team's, and on a fleet.
  const [visibility, setVisibility, landingVisibility] = useVisibilityParam(
    undefined,
    workspace || lockedFleet ? "all" : undefined,
    { counts: segmentCounts, enabled: !workspace && !lockedFleet },
  );
  const landing = landingVisibility || fleetPending;
  const vis = visibilityQuery(visibility);
  const { data: worksets, isLoading, isError, refetch } = useWorksets(
    {
      ...narrowing,
      vis,
      limit: table.pageSize,
      offset: table.page * table.pageSize,
      sort: sortKey === "updated" && direction === "desc" ? undefined : sortKey,
      direction: sortKey === "updated" && direction === "desc" ? undefined : direction,
    },
    { enabled: !landing },
  );
  const { data: counted } = useWorksetCount({ ...narrowing, vis }, { enabled: !landing });
  const resolveWorkset = useResolveWorkset();
  const deleteWorkset = useDeleteWorkset();
  const aiOff = useAiActionDisabled();

  const onResolve = async (workset: Workset) => {
    const ok = await confirm({
      title: t_i18n("Resolve this workset?"),
      description: t_i18n(
        "It stops being active and is purged after its retention window. Its items and summaries stay readable until then.",
      ),
    });
    if (!ok) return;
    try {
      await resolveWorkset.mutateAsync({ id: workset.id });
      toast({ title: t_i18n("Workset resolved") });
    } catch (error) {
      // A company-managed corpus is admin-only to mutate, so a refusal here is
      // an ordinary outcome and has to be shown rather than swallowed.
      toast({
        title: t_i18n("Could not resolve this workset"),
        description: error instanceof Error ? error.message : undefined,
        variant: "error",
      });
    }
  };

  const onDelete = async (workset: Workset) => {
    const ok = await confirm({
      title: t_i18n("Delete this workset?"),
      description: t_i18n(
        "Its items, summaries and stored documents are deleted for good. Documents it only referenced from other knowledge bases are left untouched.",
      ),
      confirmLabel: t_i18n("Delete"),
      variant: "danger",
    });
    if (!ok) return;
    try {
      await deleteWorkset.mutateAsync(workset.id);
      toast({ title: t_i18n("Workset deleted") });
    } catch (error) {
      toast({
        title: t_i18n("Could not delete this workset"),
        description: error instanceof Error ? error.message : undefined,
        variant: "error",
      });
    }
  };

  const columns = useMemo(() => worksetColumns(agentName, t_i18n), [agents, t_i18n]); // eslint-disable-line react-hooks/exhaustive-deps
  const filters = useMemo<DataTableFilter<Workset>[]>(
    () => [
      {
        key: "status",
        label: "Status",
        options: [
          { value: "active", label: t_i18n("Active") },
          { value: "resolved", label: t_i18n("Resolved") },
        ],
      },
      {
        key: "scope",
        label: "Scope",
        options: (Object.keys(SCOPE_LABELS) as Workset["scope"][]).map((value) => ({ value, label: t_i18n(SCOPE_LABELS[value]) })),
      },
    ],
    [t_i18n],
  );

  const rowActions = (workset: Workset) => (
    <span className="flex items-center justify-end gap-1">
      {workset.status === "active" && (
        <OpsRowAction icon={CheckCircle2} tone="success" label={t_i18n("Resolve")} onClick={() => void onResolve(workset)} />
      )}
      <OpsRowAction icon={Trash2} tone="danger" label={t_i18n("Delete")} onClick={() => void onDelete(workset)} disabled={aiOff} />
    </span>
  );

  return (
    <div className="space-y-4" data-testid="worksets-list">
      {confirmNode}
      {/* One records table (dev-docs/reference/data-tables.md, slice 8b): search, Visibility,
          Status and Scope filters, sort, Columns, total, page size and URL state are the
          table's; the fleet picker is a pill of the page. The grid view keeps each row on
          the Operations row contract (OpsRowGrid) as its card; the list view draws columns. */}
      <DataTable<Workset>
        rows={landing ? [] : (worksets ?? [])}
        columns={columns}
        rowKey={(workset) => workset.id}
        urlKey={URL_KEY}
        storageKey="operations-worksets"
        defaultSort={DEFAULTS.sort ?? undefined}
        defaultPageSize={DEFAULTS.pageSize}
        server={{ total: counted?.count ?? worksets?.length ?? 0 }}
        searchPlaceholder={t_i18n("Search worksets...")}
        toolbarLead={toolbarLead}
        scope={{ value: visibility, onChange: setVisibility, counts: segmentCounts }}
        filters={filters}
        // One filter row at 1440 and 1024, as the other Operations lists (#10215 criterion 6):
        // Status, Scope and the fleet picker sit in the Filters pill.
        filtersInPill
        filterPills={
          showFleetPicker ? <FleetFilter value={fleet} onChange={setFleet} /> : undefined
        }
        externalFilterCount={showFleetPicker && fleet ? 1 : 0}
        // Clear all clears the fleet it counts with the table's own filters, in one write (#1001).
        onClearAll={() => setSearchParams((prev) => clearFilterParams(prev, lockedFleet ? [] : ["fleet", "agent"], URL_KEY), { replace: true })}
        exportName="worksets"
        isLoading={landing || isLoading}
        isError={isError}
        onRetry={() => void refetch()}
        emptyLabel={t_i18n("No worksets")}
        noResultsLabel={t_i18n("No workset matches")}
        emptyContent={
          <VisibilityEmptyHint value={visibility} counts={segmentCounts ?? undefined} onShowAll={() => setVisibility("all")}>
            <EmptyState
              icon={Library}
              title={t_i18n("No worksets")}
              description={t_i18n(
                "A workset is the corpus an agent builds when a task spans more material than it can hold at once: collected, summarised and ranked outside its context.",
              )}
            />
          </VisibilityEmptyHint>
        }
        rowTestId="workset-table-row"
        onRowClick={(workset) => setExpanded(expanded === workset.id ? null : workset.id)}
        expandedRow={(workset) => (expanded === workset.id ? <WorksetDetailPanel worksetId={workset.id} /> : null)}
        rowActions={rowActions}
        rowActionsWidth={64}
        view={{ value: viewMode, onChange: setViewMode }}
        renderGrid={
          viewMode === "grid"
            ? (rows) => (
                <div className="space-y-2">
                  {rows.map((workset) => (
                    <WorksetRow
                      key={workset.id}
                      workset={workset}
                      owner={agentName(workset.owner_agent_id)}
                      open={expanded === workset.id}
                      onToggle={() => setExpanded(expanded === workset.id ? null : workset.id)}
                      actions={rowActions(workset)}
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
          <OpsListSkeleton label={t_i18n("Loading...")} pageSize={pageSize} actionsClassName={ACTIONS_COLUMN} actions={2} lead={1} />
        )}
      />
    </div>
  );
}

/** The table's keys in the URL (`wks_q`, `wks_sort`, `wks_page`, `wks_f_status`...). */
const URL_KEY = "wks_";

/** The sorts the server offers (worksets.py `_SORTS`); the default is the newest activity first. */
const SORT_KEYS = ["name", "items", "size", "updated", "created"] as const;

const DEFAULTS: DataTableDefaults = { sort: { key: "updated", direction: "desc" }, pageSize: 25 };

type Translate = (id: string, values?: Record<string, string | number>) => string;

/**
 * The columns of the Worksets list: the cells of the list view (Columns chooses them per
 * user), the toolbar sort of the grid view and the CSV export. Every sort is the server's.
 */
function worksetColumns(agentName: (id: string | null) => string | null, t_i18n: Translate): DataTableColumn<Workset>[] {
  return [
    {
      key: "name",
      header: "Name",
      cell: (w) => <TruncatedText text={w.label || w.key} />,
      sortValue: (w) => (w.label || w.key).toLowerCase(),
      csv: (w) => w.label || w.key,
    },
    {
      key: "status",
      header: "Status",
      hideable: true,
      cell: (w) => (w.status === "resolved" ? t_i18n("Resolved") : t_i18n("Active")),
      csv: (w) => w.status,
    },
    {
      key: "scope",
      header: "Scope",
      hideable: true,
      hideBelow: "4xl",
      cell: (w) => t_i18n(SCOPE_LABELS[w.scope]),
      csv: (w) => w.scope,
    },
    {
      key: "items",
      header: "Items",
      hideable: true,
      align: "right",
      cell: (w) => formatCompactNumber(w.item_count),
      sortValue: (w) => w.item_count,
      csv: (w) => w.item_count,
    },
    {
      key: "size",
      header: "Size",
      hideable: true,
      hideBelow: "6xl",
      align: "right",
      cell: (w) => `${formatCompactNumber(w.content_chars)} ${t_i18n("chars")}`,
      sortValue: (w) => w.content_chars,
      csv: (w) => w.content_chars,
    },
    {
      key: "owner",
      header: "Owner",
      hideable: true,
      hideBelow: "6xl",
      cell: (w) => <TruncatedText text={agentName(w.owner_agent_id) ?? ""} />,
      csv: (w) => agentName(w.owner_agent_id) ?? "",
    },
    { key: "key", header: "Key", hideable: true, defaultHidden: true, cell: (w) => <span className="font-mono">{w.key}</span>, csv: (w) => w.key },
    {
      key: "updated",
      header: "Updated",
      hideable: true,
      align: "right",
      cell: (w) => <OpsUpdated at={w.updated_at} />,
      sortValue: (w) => w.updated_at,
      csv: (w) => w.updated_at,
    },
  ];
}

/**
 * One workset as a card of the grid view, on the Operations row contract: one height on every
 * row (OPS_ROW_HEIGHT), nothing wraps; the coverage and the items unfold under it.
 */
function WorksetRow({
  workset,
  owner,
  open: isOpen,
  onToggle: toggle,
  actions,
  zone,
  locale,
}: {
  workset: Workset;
  owner: string | null;
  open: boolean;
  onToggle: () => void;
  actions: ReactNode;
  zone: string;
  locale: string;
}) {
  const { t_i18n } = useFormatter();
  const title = workset.label || workset.key;
  const collection = worksetCollection(workset);
  // A resolved corpus shows no collecting chip; a sealed one keeps its lock.
  const collectionChip = collection !== "sealed" && workset.status === "active" ? collection : null;
  // A claim and a fact side by side, never merged.
  const announcedDiffers = workset.declared_total !== null && workset.declared_total !== workset.item_count;
  const items = `${workset.item_count} ${pluralize(workset.item_count, t_i18n("item"))}`;
  const size = `${formatCompactNumber(workset.content_chars)} ${t_i18n("chars")}`;
  // Every fact of the workset, in the contract's order: who owns it,
  // the state facts, the counts, the key, then the timestamps.
  const facts: OpsFact[] = [];
  if (owner) facts.push({ label: t_i18n("Owner"), value: owner });
  facts.push({ label: t_i18n("Status"), value: workset.status === "resolved" ? t_i18n("Resolved") : t_i18n("Active") });
  facts.push({ label: t_i18n("Scope"), value: t_i18n(SCOPE_LABELS[workset.scope]) });
  facts.push({ label: t_i18n("Collected"), value: t_i18n(WORKSET_COLLECTION_LABELS[collection]) });
  facts.push({ label: t_i18n("Items"), value: items });
  if (workset.evicted_count > 0) {
    facts.push({ label: t_i18n("Evicted"), tone: "warning", value: String(workset.evicted_count) });
  }
  if (announcedDiffers) {
    facts.push({
      label: t_i18n("Announced"),
      tone: "warning",
      value: `${workset.declared_total}. ${t_i18n("The caller announced this many; the difference is unexplained.")}`,
    });
  }
  facts.push({ label: t_i18n("Size"), value: size });
  facts.push({ label: t_i18n("Key"), value: <span className="font-mono">{workset.key}</span> });
  facts.push({ label: t_i18n("Created"), value: formatInZone(workset.created_at, zone, locale) });
  facts.push({ label: t_i18n("Updated"), value: formatInZone(workset.updated_at, zone, locale) });
  if (workset.resolved_at) facts.push({ label: t_i18n("Resolved"), value: formatInZone(workset.resolved_at, zone, locale) });
  if (workset.expires_at) facts.push({ label: t_i18n("Expires"), value: formatInZone(workset.expires_at, zone, locale) });
  // The row shows the status, the scope and collecting chips, the
  // counts on line 2, the owner and the key; "+N" counts the rest.
  const shownFacts = (owner ? 1 : 0) + 3 + 1 + (workset.evicted_count > 0 ? 1 : 0) + (announcedDiffers ? 1 : 0) + 1 + 1;
  const hiddenFacts = Math.max(0, facts.length - shownFacts);
  return (
    <Card
      data-testid="workset-row"
      onClick={toggle}
      className={cn(OPS_ROW_CARD, "group cursor-pointer overflow-hidden transition-all hover:border-elevation-default")}
    >
      <CardContent className="p-0">
        {/* The expander and the actions are SIBLINGS, never a button nested in another. */}
        <div className={cn(OPS_ROW_HEIGHT, "px-4 py-2.5")} data-testid="workset-line">
          <OpsRowGrid
            actionsClassName={ACTIONS_COLUMN}
            actionsTestId="workset-actions"
            lead={<OpsExpandToggle open={isOpen} onToggle={toggle} />}
            main={
              <>
                <div className="flex min-w-0 items-center gap-2 overflow-hidden whitespace-nowrap" data-testid="workset-line1">
                  <OpsHoverCard title={title} facts={facts}>
                    <button
                      type="button"
                      className="min-w-[4rem] shrink truncate text-left font-medium hover:text-accent focus-visible:outline-none focus-visible:underline"
                      data-testid="workset-title"
                      aria-expanded={isOpen}
                      onClick={(e) => {
                        e.stopPropagation();
                        toggle();
                      }}
                    >
                      {title}
                    </button>
                  </OpsHoverCard>
                  {/* The status and the collection state (Rolling, Still collecting)
                      never shrink: they sit outside the chip strip, which drops its
                      chips when a long implicit key fills the title. */}
                  <span className="flex shrink-0 items-center gap-1.5" data-testid="workset-state">
                    {workset.status === "resolved" ? (
                      <Badge variant="secondary" className="content-compact text-feedback-success-primary bg-feedback-success-primary/10" data-testid="workset-status">
                        {t_i18n("Resolved")}
                      </Badge>
                    ) : (
                      <Badge variant="secondary" className="content-compact text-accent bg-accent/10" data-testid="workset-status">
                        {t_i18n("Active")}
                      </Badge>
                    )}
                    {collectionChip && (
                      <Badge
                        variant="outline"
                        className={cn("content-compact", collectionChip === "collecting" && "text-feedback-warning-primary")}
                        data-testid="workset-collection"
                      >
                        {t_i18n(WORKSET_COLLECTION_LABELS[collectionChip])}
                      </Badge>
                    )}
                  </span>
                  <OpsChipStrip testId="workset-chips">
                    <ScopeBadge workset={workset} />
                    {workset.sealed_at && (
                      <Badge variant="outline" className="content-compact gap-1">
                        <Lock className="h-3 w-3" aria-hidden="true" />
                        {t_i18n("Sealed")}
                      </Badge>
                    )}
                  </OpsChipStrip>
                </div>
                {/* Line 2, one line: what it collected, what it lost, what was announced. */}
                <div className="flex min-w-0 items-center gap-3 overflow-hidden whitespace-nowrap content-compact text-default-secondary" data-testid="workset-summary">
                  <span className="shrink-0">{items}</span>
                  {workset.evicted_count > 0 && (
                    <span className="inline-flex shrink-0 items-center gap-1 text-feedback-warning-primary">
                      <AlertTriangle className="h-3 w-3" aria-hidden="true" />
                      {workset.evicted_count} {t_i18n("evicted")}
                    </span>
                  )}
                  {announcedDiffers && (
                    <span className="shrink-0 text-feedback-warning-primary">
                      {t_i18n("announced")} {workset.declared_total}
                    </span>
                  )}
                  <span className="min-w-0 truncate">{size}</span>
                </div>
              </>
            }
            meta={
              <>
                {owner ? (
                  <OpsToken icon={Bot} full={owner} testId="workset-owner">{owner}</OpsToken>
                ) : (
                  <span aria-hidden="true" className="h-4" />
                )}
                <span className="flex min-w-0 items-center gap-2">
                  <OpsToken icon={Hash} tone="muted" full={workset.key} copy={workset.key} copyLabel={t_i18n("Copy the key")} testId="workset-key">
                    {workset.key}
                  </OpsToken>
                  <OpsMoreToken count={hiddenFacts} title={title} facts={facts} onOpen={toggle} testId="workset-more" />
                </span>
              </>
            }
            updated={<OpsUpdated at={workset.updated_at} testId="workset-updated" />}
            actions={actions}
          />
        </div>
        {isOpen && (
          <div onClick={(e) => e.stopPropagation()} data-testid="workset-detail">
            <WorksetDetailPanel worksetId={workset.id} />
          </div>
        )}
      </CardContent>
    </Card>
  );
}
