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
 * The runs of an Activity tab on the shared DataTable (standard v2, #9378 slice 4a):
 * an agent's, an assignment's, and an entity's (integration, MCP server, custom
 * tool, skill, prompt; an A2A integration's partner runs only). The same shape as
 * Operations > Executions (slice 8f): server mode, columns Run, Status, Type,
 * Agent, Started, Duration, Status as a visible multi-select filter on the
 * toolbar with its counts, search, one total, one pager, per-user page size and
 * columns, the grid view keeping the run cards with every action they carry.
 *
 * The state lives in the URL (`q`, `page`, `size`, `sort`, `dir`, `status`,
 * `review`); an old link's `?sort=oldest` or `?sort=status` still opens that order.
 */

import { useCallback, useMemo, type ReactNode } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ActivityEmptyState } from "@/components/ui/activity-empty-state";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { RowRemoveButton } from "@/components/ui/row-remove-button";
import { RunCard } from "@/components/ui/run-card";
import { RunPauseDecision, runPauseState } from "@/components/decisions/RunPauseDecision";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { useToast } from "@/components/ui/toast";
import { TruncatedText } from "@/components/ui/truncated-text";
import {
  runReviewFromParam, useAllAssignmentRuns, useAssignmentRunCount, useDeleteAssignmentRun, useDismissRun,
  useRespondToHumanInput, useSubmitApprovalDecisions,
  type RunReviewFilter,
} from "@/hooks/api/useAssignmentApi";
import { useRunStatusCounts } from "@/hooks/api/useRunStatusCounts";
import { useCompactToolbar } from "@/hooks/useCompactToolbar";
import { useDebounce } from "@/hooks/useDebounce";
import { useEntityListScope } from "@/hooks/useEntityListScope";
import { useViewMode } from "@/hooks/useViewMode";
import { useFormatter } from "@/i18n";
import { REFETCH_INTERVAL_STANDARD, runStatusConfig } from "@/lib/constants";
import { storedPageSize, type DataTableState } from "@/lib/data-table-state";
import { runAgentIdsParam } from "@/lib/run-agent-filter";
import {
  REVIEW_OPTIONS, RUN_DEFAULT_DIR, RUN_DEFAULT_SORT, RUN_SORTS, runDurationSeconds, runSortFromParams, runTypeLabel,
} from "@/lib/run-activity";
import { formatDurationShort, parsePageParam } from "@/lib/utils";
import { assignmentRunTitle, ExecutionStatusBadge } from "@/pages/operations/ExecutionRow";
import { ExecutionStatusFilter, statusesFromParams, type ExecutionStatus } from "@/pages/operations/ExecutionStatusFilter";
import type { ApprovalDecision, AssignmentRunDetail } from "@/types";

const DEFAULT_PAGE_SIZE = 25;
const DEFAULT_SORT = RUN_DEFAULT_SORT;
const DEFAULT_DIR = RUN_DEFAULT_DIR;
const SORTS = RUN_SORTS;
const runDuration = runDurationSeconds;
/** The list view's fixed column widths (px, `layout="fixed"`): the Run title takes what is left. */
const COLUMN_WIDTHS = { status: 140, type: 120, agent: 168, started: 140, duration: 96, actions: 48 } as const;

interface RunActivityListProps {
  /** Per-user key of the page size, the hidden columns and the view (localStorage), one per tab kind. */
  storageKey: string;
  /** The empty state's noun ("agent", "assignment", "MCP server"...). */
  entityLabel: string;
  /** Only this agent's runs (its own tab: the Agent column is hidden). */
  agentId?: string;
  /** Only this assignment's runs. */
  assignmentId?: string;
  /** Only the runs of these agents (an entity's bound agents). */
  agentIds?: ReadonlySet<string>;
  /** Only the work this A2A partner submitted, scoped server-side by its registration. */
  a2aIntegrationId?: string;
  /** Offer the All / Unreviewed ROI review filter. */
  reviewFilter?: boolean;
  /** The empty state's sentence, when the entity says it better than the default. */
  emptyMessage?: string;
  /** Controls of the host on the toolbar (Delete All). */
  toolbar?: ReactNode;
}

export function RunActivityList({
  storageKey,
  entityLabel,
  agentId,
  assignmentId,
  agentIds,
  a2aIntegrationId,
  reviewFilter = false,
  emptyMessage,
  toolbar,
}: RunActivityListProps) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { toast } = useToast();
  const [ConfirmEl, confirmDelete] = useConfirm();
  const deleteRun = useDeleteAssignmentRun();
  const respondHitl = useRespondToHumanInput();
  const submitApprovals = useSubmitApprovalDecisions();
  const dismissRun = useDismissRun();
  const compact = useCompactToolbar();
  const [viewMode, setViewMode] = useViewMode(`activity-${storageKey}`, "list");
  const scope = useEntityListScope({ scope: "all" });

  // --- State, all in the URL ---
  const [searchParams, setSearchParams] = useSearchParams();
  const search = searchParams.get("q") ?? "";
  const debouncedSearch = useDebounce(search, 300);
  const page = parsePageParam(searchParams.get("page"));
  const pageSize = Number(searchParams.get("size")) || storedPageSize(storageKey, DEFAULT_PAGE_SIZE);
  const { key: sortKey, direction: sortDir } = runSortFromParams(searchParams.get("sort"), searchParams.get("dir"));
  const runSort = SORTS[sortKey][sortDir];
  const review = reviewFilter ? runReviewFromParam(searchParams.get("review")) : undefined;
  // Memoised: a fresh array each render would break the React Compiler's memoisation.
  const statusParam = searchParams.get("status");
  const statuses = useMemo(() => statusesFromParams(statusParam, null), [statusParam]);
  const statusFilter = statuses.length > 0 ? statuses.join(",") : undefined;

  const update = useCallback(
    (changes: Record<string, string | undefined>, resetPage = true) =>
      setSearchParams(
        (prev) => {
          const params = new URLSearchParams(prev);
          for (const [key, value] of Object.entries(changes)) {
            if (value) params.set(key, value);
            else params.delete(key);
          }
          if (resetPage) params.delete("page");
          return params;
        },
        { replace: true },
      ),
    [setSearchParams],
  );
  const setStatuses = useCallback((next: ExecutionStatus[]) => update({ status: next.join(",") || undefined }), [update]);

  // --- Data ---
  const searchParam = debouncedSearch || undefined;
  const { data: runs, isLoading } = useAllAssignmentRuns(
    pageSize,
    page * pageSize,
    statusFilter,
    searchParam,
    undefined,                      // notifications
    undefined,                      // vis
    agentId,
    undefined,                      // digests
    "none",                         // top-level runs; a delegation tree opens on the run page
    true,
    a2aIntegrationId,
    "all",                          // every run of the entity for an admin
    a2aIntegrationId ? undefined : agentIds,
    undefined,                      // fleet
    review,
    runSort,
    assignmentId,
  );
  const { data: countData } = useAssignmentRunCount(
    statusFilter,
    searchParam,
    undefined,
    undefined,
    agentId,
    assignmentId,
    undefined,
    "none",
    a2aIntegrationId,
    "all",
    a2aIntegrationId ? undefined : agentIds,
    true,
    undefined,
    review,
  );
  const statusCounts = useRunStatusCounts(
    ["all-assignment-runs"],
    "/assignments/runs",
    {
      search: searchParam,
      agent_id: agentId,
      assignment_id: assignmentId,
      agent_ids: a2aIntegrationId ? undefined : runAgentIdsParam(agentIds),
      a2a_integration_id: a2aIntegrationId,
      scope,
      review,
    },
    { refetchInterval: REFETCH_INTERVAL_STANDARD },
  );
  const total = countData?.count ?? 0;
  const rows = runs ?? [];

  // --- Table state ---
  const tableState: DataTableState = {
    search,
    sort: { key: sortKey, direction: sortDir },
    page,
    pageSize,
    filters: {},
  };
  const onTableState = (next: DataTableState) => {
    const searchChanged = next.search !== search;
    const nextKey = next.sort && SORTS[next.sort.key] ? next.sort.key : DEFAULT_SORT;
    const nextDir = next.sort?.direction ?? DEFAULT_DIR;
    const isDefault = nextKey === DEFAULT_SORT && nextDir === DEFAULT_DIR;
    update(
      {
        q: next.search || undefined,
        sort: isDefault ? undefined : nextKey,
        dir: isDefault ? undefined : nextDir,
        size: next.pageSize === DEFAULT_PAGE_SIZE ? undefined : String(next.pageSize),
        page: !searchChanged && next.page > 0 ? String(next.page) : undefined,
      },
      false,
    );
  };
  const clearAll = () => update({ q: undefined, status: undefined, review: undefined });

  // --- Actions ---
  const handleDelete = async (id: string) => {
    if (await confirmDelete({ title: "Delete this run?", description: "This action cannot be undone.", confirmLabel: "Delete" })) {
      deleteRun.mutate(id, { onSuccess: () => toast({ title: t_i18n("Run deleted"), variant: "success" }) });
    }
  };
  const onRespond = (runId: string, response: string) =>
    respondHitl.mutate(
      { runId, response },
      { onSuccess: () => toast({ title: t_i18n("Response sent: assignment resuming"), variant: "success" }) },
    );
  const onDismiss = (runId: string) =>
    dismissRun.mutate(runId, { onSuccess: () => toast({ title: t_i18n("Run dismissed"), variant: "success" }) });
  const onApprove = (runId: string, decisions: ApprovalDecision[]) =>
    submitApprovals.mutate(
      { runId, decisions },
      {
        onSuccess: () => toast({ title: t_i18n("Decision recorded: assignment resuming"), variant: "success" }),
        onError: (err) => toast({ title: t_i18n("Could not submit decisions"), description: err.message, variant: "error" }),
      },
    );

  // layout="fixed": the Run title takes what is left and the long texts truncate with
  // the DS tooltip, so the list never scrolls sideways at 1024.
  const columns: DataTableColumn<AssignmentRunDetail>[] = [
    {
      key: "run",
      header: "Run",
      cell: (run) => <TruncatedText className="font-medium" text={assignmentRunTitle(run, t_i18n)} />,
      csv: (run) => assignmentRunTitle(run, t_i18n),
    },
    {
      key: "status",
      header: "Status",
      width: COLUMN_WIDTHS.status,
      cell: (run) => <ExecutionStatusBadge run={run} kind="assignment" />,
      sortValue: (run) => run.status,
      csv: (run) => t_i18n(runStatusConfig(run).label),
      hideable: true,
    },
    {
      key: "type",
      header: "Type",
      width: COLUMN_WIDTHS.type,
      // Below a 1152 px box the Run title keeps its room; the Columns pill brings it back.
      hideBelow: "6xl",
      cell: (run) => <TruncatedText text={t_i18n(runTypeLabel(run))} />,
      csv: (run) => runTypeLabel(run),
      hideable: true,
    },
    {
      key: "agent",
      header: "Agent",
      width: COLUMN_WIDTHS.agent,
      cell: (run) => <TruncatedText text={run.agent_name || "-"} />,
      sortValue: (run) => run.agent_name,
      csv: (run) => run.agent_name,
      hideable: true,
      // An agent's own tab: every row is that agent.
      defaultHidden: !!agentId,
    },
    {
      key: "started",
      header: "Started",
      width: COLUMN_WIDTHS.started,
      cell: (run) => <DateTimeCell value={run.started_at ?? run.created_at} />,
      sortValue: (run) => run.created_at,
      csv: (run) => run.started_at ?? run.created_at,
      firstSort: "desc",
      hideable: true,
    },
    {
      key: "duration",
      header: "Duration",
      width: COLUMN_WIDTHS.duration,
      align: "right",
      cell: (run) => {
        const seconds = runDuration(run);
        return seconds === null ? "-" : formatDurationShort(seconds);
      },
      sortValue: (run) => runDuration(run) ?? -1,
      csv: (run) => runDuration(run) ?? "",
      firstSort: "desc",
      hideable: true,
    },
  ];

  const reviewPill = reviewFilter ? (
    <SegmentedFilter<"all" | RunReviewFilter>
      options={REVIEW_OPTIONS}
      value={review ?? "all"}
      onChange={(key) => update({ review: key === "all" ? undefined : key })}
      ariaLabel={t_i18n("ROI review")}
      className="shrink-0"
      testId="run-review-filter"
    />
  ) : undefined;

  const emptyState = (
    <ActivityEmptyState entityLabel={entityLabel} message={emptyMessage} />
  );

  return (
    <>
      <DataTable
        rows={rows}
        columns={columns}
        rowKey={(run) => run.id}
        server={{ total }}
        state={tableState}
        onStateChange={onTableState}
        storageKey={`activity-${storageKey}`}
        defaultPageSize={DEFAULT_PAGE_SIZE}
        defaultSort={{ key: DEFAULT_SORT, direction: DEFAULT_DIR }}
        searchPlaceholder={t_i18n("Search runs...")}
        compactSearch={compact}
        noFilters="status and ROI review are server filters (toolbar Status pill and filterPills)"
        toolbarLead={<ExecutionStatusFilter value={statuses} onChange={setStatuses} counts={statusCounts} compact={compact} />}
        toolbar={toolbar}
        filterPills={reviewPill}
        externalFilterCount={(statuses.length > 0 ? 1 : 0) + (review ? 1 : 0)}
        onClearAll={clearAll}
        view={{ value: viewMode, onChange: setViewMode }}
        // One scrollbar in the grid too: the card list gets the table's fit-to-window scroll area,
        // as Executions (UX round 1 B1 on #11934).
        fitGrid
        // The grid keeps the run cards, every action they carry (delete, answer, approve, dismiss).
        renderGrid={
          viewMode === "grid"
            ? (items) => (
                <div className="space-y-3" data-testid="run-activity-cards">
                  {items.map((run) => (
                    <RunCard
                      key={run.id}
                      run={run}
                      onDelete={handleDelete}
                      onRespond={onRespond}
                      onDismiss={onDismiss}
                      approveFailed={submitApprovals.isError}
                      onApprove={onApprove}
                    />
                  ))}
                </div>
              )
            : undefined
        }
        isLoading={isLoading}
        emptyLabel={t_i18n("No runs match your filters.")}
        noResultsLabel={review === "unreviewed" ? t_i18n("No run waits for an ROI review.") : t_i18n("No runs matching your search.")}
        emptyContent={emptyState}
        onRowClick={(run) => navigate(`/operations/executions/${run.id}`)}
        // A run waiting for a person unfolds its question under the row, with Answer, Approve and Dismiss.
        expandedRow={(run) =>
          runPauseState(run).isHitlPending ? (
            <RunPauseDecision
              run={run}
              onRespond={onRespond}
              onDismiss={onDismiss}
              onApprove={onApprove}
              approveFailed={submitApprovals.isError}
              showResolved={false}
            />
          ) : null
        }
        toggleInCell
        rowActions={(run) => <RowRemoveButton label={t_i18n("Delete this run")} onClick={() => void handleDelete(run.id)} />}
        rowActionsWidth={COLUMN_WIDTHS.actions}
        rowTestId="run-activity-row"
        layout="fixed"
      />
      {ConfirmEl}
    </>
  );
}
