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
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  Activity,
  ArrowRight,
  Bot,
  Hash,
  Trash2,
  X,
} from "lucide-react";
import {
  useBackgroundTasks,
  useBackgroundTaskCount,
  useBulkDeleteBackgroundTasks,
  useCancelBackgroundTask,
  useDeleteBackgroundTask,
  type BackgroundTaskSort,
} from "@/hooks/api/useAgentApi";
import { useAuth } from "@/hooks/useAuth";
import { useTick } from "@/hooks/useTick";
import { useDebounce } from "@/hooks/useDebounce";
import { useViewMode } from "@/hooks/useViewMode";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DataTable, TOOLBAR_CONTROL_HEIGHT, type DataTableColumn } from "@/components/ui/data-table";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { TruncatedText } from "@/components/ui/truncated-text";
import { HeldBadge, heldExplanation } from "@/components/ui/held-badge";
import {
  OPS_ROW_CARD, OPS_ROW_HEIGHT, OpsChipStrip, OpsHoverCard, OpsListSkeleton, OpsMoreToken, OpsRowAction, OpsRowGrid, OpsToken, OpsUpdated,
  type OpsFact,
} from "@/components/ui/operations-list";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { VisibilityEmptyHint } from "@/components/ui/visibility-filter";
import { useVisibilityParam, visibilityQuery } from "@/hooks/useVisibilityParam";
import { useSegmentCounts } from "@/hooks/api/useSegmentCounts";
import { useRunStatusCounts } from "@/hooks/api/useRunStatusCounts";
import { EmptyState } from "@/components/ui/empty-state";
import { SelectionBox } from "@/components/ui/list-control-bar";
import { Tip } from "@/components/ui/tooltip";
import { useToast } from "@/components/ui/toast";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { AnnotationButtons } from "@/components/ui/annotation-buttons";
import { useMyAnnotations } from "@/hooks/api/useAnnotationApi";
import { ICON_MAP, STATUS_CONFIG, REFETCH_INTERVAL_FAST } from "@/lib/constants";
import { clearFilterParams, storedPageSize, type DataTableState } from "@/lib/data-table-state";
import type { SortDirection } from "@/lib/table-rows";
import { cn, formatDurationShort, parsePageParam } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { SubAgentTask } from "@/types";
import { FleetFilter } from "@/components/fleets/FleetFilter";
import { useFleetParam } from "@/hooks/useFleetParam";
import { useCompactToolbar } from "@/hooks/useCompactToolbar";
import { TaskExecutionLine } from "@/pages/background-tasks/TaskExecutionLine";
import { hasNoLiveExecution, OPEN_TASK_STATUSES } from "@/pages/background-tasks/liveness";
import { OperationsStatusFilter, statusesFromParam } from "@/pages/operations/OperationsStatusFilter";
import { useIsXtmOne } from "@/hooks/usePlatformConfig";
import { formatInZone, viewerTimeZone } from "@/lib/scheduled-work-items";

/** The table's per-user preferences (page size, columns) and its default page size. */
const STORAGE_KEY = "operations-background-tasks";
const DEFAULT_PAGE_SIZE = 25;

/** The actions column: one width on every row (the two thumbs, kill, delete). */
const ACTIONS_COLUMN = "w-32";
/**
 * The list view's fixed column widths (px, `layout="fixed"`): the Task title takes what is left,
 * at least about 300 px in an 850 px table at 1080 (Created and Delegated to hide below 1152 px).
 * Actions fits its three buttons (24 + 24 + 28 and the gaps).
 */
const TASK_COLUMN_WIDTHS = { status: 128, from: 168, to: 168, created: 120, updated: 120, actions: 96 } as const;

/** The execution filter: every task, or the in-flight ones no execution runs. */
const LIVENESS_FILTERS = [
  { key: "", label: "Any execution" },
  { key: "no_live_execution", label: "No live execution" },
];

/** Every status a task takes, in the order of its life: the Status pill's choices. */
const TASK_STATUSES = ["pending", "running", "completed", "incomplete", "failed", "cancelled"] as const;
type TaskStatus = (typeof TASK_STATUSES)[number];

const DEFAULT_SORT = "created";
const DEFAULT_DIR: SortDirection = "desc";
/**
 * The server order of each sortable column and direction (`?sort=` of
 * /chat/sub-agent-tasks, #9378 slice 8e). An old `?sort=last_progress` link
 * reads as the Updated column, newest move first.
 */
const SORTS: Record<string, Record<SortDirection, BackgroundTaskSort>> = {
  created: { desc: "created", asc: "created_asc" },
  updated: { desc: "last_progress", asc: "last_progress_asc" },
  status: { asc: "status", desc: "status_desc" },
  from: { asc: "from", desc: "from_desc" },
  to: { asc: "to", desc: "to_desc" },
};

/**
 * A background task belongs to the person who spawned it. The page offers the
 * four segments of every list: All is every user's tasks for an admin
 * and the caller's own for anyone else; an admin's tasks that are not theirs
 * are Company when the agent that delegated them is company-managed, Shared
 * otherwise. A non-admin's Shared and Company are empty.
 */

const ACTIVE_STATUSES = new Set(["pending", "running"]);
/** A task that finished with something to judge. */
const RATEABLE_STATUSES = new Set(["completed", "failed", "incomplete"]);

/** Whether the caller rates *task* on this page: a finished task of their own
 *  spawned from a chat - one with a run is rated on the run page its row opens. */
function isRateableHere(task: SubAgentTask, userId: string | undefined): boolean {
  return !task.assignment_run_id && task.user_id === userId && RATEABLE_STATUSES.has(task.status);
}

/** When the task last moved: its execution's last progress, else its end, start or creation. */
function taskUpdatedAt(task: SubAgentTask): string | null {
  return task.last_progress_at ?? task.completed_at ?? task.started_at ?? task.created_at ?? null;
}

function AgentChip({ name, icon }: { name?: string | null; icon?: string | null }) {
  const { t_i18n } = useFormatter();
  const Icon = (icon && ICON_MAP[icon]) || Bot;
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5">
      <Icon className="h-3.5 w-3.5 shrink-0 text-accent" />
      <span className="min-w-0 truncate">{name || t_i18n("Unknown agent")}</span>
    </span>
  );
}

/**
 * `fleet` locks the list on a fleet (its page's Operations tab): the
 * tasks whose parent or target agent is one of its home members. Elsewhere
 * `?fleet=` narrows the list the same way.
 *
 * The list is the shared DataTable (standard v2, #9378 slice 8e): server mode,
 * its state in the URL (`q`, `page`, `sort`, `dir`, `size`), Status as a
 * multi-select pill on the toolbar (`?status=a,b`), the execution filter and
 * the fleet in the Filters pill, the grid kept on the Operations row contract.
 */
export default function BackgroundTasks({
  hideHeader = false,
  fleet: lockedFleet,
  toolbarLead,
}: {
  hideHeader?: boolean;
  fleet?: string;
  /** A control of the host drawn first on the filter row (the fleet tab's list select), never a row of its own. */
  toolbarLead?: ReactNode;
}) {
  const { t_i18n, locale } = useFormatter();
  useTick(REFETCH_INTERVAL_FAST); // keep relative timestamps fresh
  const navigate = useNavigate();
  const { isAdmin, user } = useAuth();
  const zone = viewerTimeZone(user?.timezone);
  const isXtmOne = useIsXtmOne();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const cancelTask = useCancelBackgroundTask();
  const deleteTask = useDeleteBackgroundTask();
  const bulkDelete = useBulkDeleteBackgroundTasks();
  const [viewMode, setViewMode] = useViewMode("operations-background-tasks");

  // --- Filter and table state are derived from URL params so back-navigation restores it ---
  const [searchParams, setSearchParams] = useSearchParams();
  const search = searchParams.get("q") ?? "";
  // The Status pill (`?status=a,b`, multi-select); an old single `?status=x` reads the same.
  // Memoised: a fresh array each render would break the React Compiler's memoisation.
  const rawStatus = searchParams.get("status");
  const selectedStatuses = useMemo(() => statusesFromParam(TASK_STATUSES, rawStatus), [rawStatus]);
  const statusParam = selectedStatuses.join(",");
  const livenessParam = searchParams.get("liveness") === "no_live_execution" ? "no_live_execution" : undefined;
  const page = parsePageParam(searchParams.get("page"));
  const rawSort = searchParams.get("sort") ?? "";
  const legacyProgressSort = rawSort === "last_progress";
  const sortKey = SORTS[rawSort] ? rawSort : legacyProgressSort ? "updated" : DEFAULT_SORT;
  // A `sort` without `dir` takes the column's first direction: newest first for a date, A to Z otherwise.
  const dirParam = searchParams.get("dir");
  const sortDir: SortDirection =
    dirParam === "asc" || dirParam === "desc" ? dirParam : sortKey === "created" || sortKey === "updated" ? "desc" : "asc";
  const taskSort = SORTS[sortKey][sortDir];
  const pageSize = Number(searchParams.get("size")) || storedPageSize(STORAGE_KEY, DEFAULT_PAGE_SIZE);
  const debouncedSearch = useDebounce(search, 300);
  // Nothing is asked while a `?fleet=` waits on the reader's fleet access.
  const [fleet, setFleet, fleetPending] = useFleetParam(lockedFleet);
  // Below 1440 px the search is compact and the visibility pills icon-only.
  const compact = useCompactToolbar();
  // The filters behind the Filters pill that narrow the list: its badge. Status is on the toolbar.
  const moreFilterCount = (livenessParam ? 1 : 0) + (!lockedFleet && fleet ? 1 : 0);
  // Each segment's count under the other filters, the numbers on the filter.
  const segmentCounts = useSegmentCounts(
    ["background-task-count"],
    "/chat/sub-agent-tasks",
    { status: statusParam || undefined, search: debouncedSearch || undefined, fleet, liveness: livenessParam },
    { enabled: !fleetPending, refetchInterval: REFETCH_INTERVAL_FAST },
  );
  // Mine | Company | Shared | All (`?vis=`; the [DEPRECATED] `?scope=` read as
  // an alias); with none named, the first segment holding a task once the
  // counts are in. All when locked on a fleet, as the fleet page's
  // other lists.
  const [visibility, setVisibility, landingVisibility] = useVisibilityParam(undefined, lockedFleet ? "all" : undefined, {
    counts: segmentCounts,
    enabled: !lockedFleet,
  });
  const landing = landingVisibility || fleetPending;
  const vis = visibilityQuery(visibility) ?? "all";
  // How many tasks each status holds under the other filters: the numbers on the Status pill.
  const statusCounts = useRunStatusCounts(
    ["background-task-count"],
    "/chat/sub-agent-tasks",
    { vis, search: debouncedSearch || undefined, fleet, liveness: livenessParam },
    { enabled: !landing, refetchInterval: REFETCH_INTERVAL_FAST },
  );

  const updateParam = useCallback(
    (key: string, value: string | undefined) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (value) next.set(key, value);
          else next.delete(key);
          next.delete("page");
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );
  const setStatuses = useCallback(
    (next: TaskStatus[]) => updateParam("status", next.length > 0 ? next.join(",") : undefined),
    [updateParam],
  );

  const { data: tasks, isLoading } = useBackgroundTasks(
    {
      vis,
      status: statusParam || undefined,
      search: debouncedSearch || undefined,
      fleet,
      liveness: livenessParam,
      sort: taskSort,
      limit: pageSize,
      offset: page * pageSize,
    },
    !landing,
  );
  const { data: countData } = useBackgroundTaskCount(
    {
      vis,
      status: statusParam || undefined,
      search: debouncedSearch || undefined,
      fleet,
      liveness: livenessParam,
    },
    !landing,
  );
  const totalMatchingCount = countData?.count ?? 0;
  // Every user's tasks under the same filters, asked by an admin on an empty Mine only.
  const { data: allSegmentsCount } = useBackgroundTaskCount(
    { vis: "all", status: statusParam || undefined, search: debouncedSearch || undefined, fleet, liveness: livenessParam },
    isAdmin && !landing && visibility === "mine" && countData?.count === 0,
  );

  // -- Selection state --
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [allMatchingSelected, setAllMatchingSelected] = useState(false);

  // Clear selection when filters change (adjust-state-during-render pattern).
  const filterKey = `${visibility}|${statusParam}|${debouncedSearch}|${livenessParam ?? ""}|${fleet ?? ""}`;
  const [prevFilterKey, setPrevFilterKey] = useState(filterKey);
  if (prevFilterKey !== filterKey) {
    setPrevFilterKey(filterKey);
    setSelectedIds(new Set());
    setAllMatchingSelected(false);
  }

  const toggleSelect = useCallback((id: string) => {
    setAllMatchingSelected(false);
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const rows = useMemo(() => tasks ?? [], [tasks]);
  const visibleIds = useMemo(() => rows.map((t) => t.task_id), [rows]);
  const rateableIds = useMemo(
    () => rows.filter((task) => isRateableHere(task, user?.id)).map((task) => task.task_id),
    [rows, user?.id],
  );
  // The caller's ratings of the whole page in one request, not one per row.
  const myRatings = useMyAnnotations("sub_agent_task", rateableIds);
  const allVisibleSelected =
    visibleIds.length > 0 && visibleIds.every((id) => selectedIds.has(id));

  const handleSelectAllMatching = useCallback(() => {
    setAllMatchingSelected(true);
    setSelectedIds(new Set(visibleIds));
  }, [visibleIds]);

  const hasMoreBeyondVisible = totalMatchingCount > visibleIds.length;

  const handleCancel = async (task: SubAgentTask) => {
    const ok = await confirm({
      title: "Kill this background task?",
      description:
        task.status === "running"
          ? "The task is running on a worker. It will be marked cancelled and its result suppressed; the worker stops at its next step (it is not hard-killed mid-tool)."
          : "The pending task will be cancelled and never run.",
      confirmLabel: "Kill task",
    });
    if (!ok) return;
    cancelTask.mutate(task.task_id, {
      onSuccess: () => toast({ title: t_i18n("Background task cancelled"), variant: "success" }),
      onError: () => toast({ title: t_i18n("Failed to cancel task"), variant: "error" }),
    });
  };

  const handleDelete = async (task: SubAgentTask) => {
    const ok = await confirm({
      title: "Delete this background task?",
      description: ACTIVE_STATUSES.has(task.status)
        ? "The task is still in flight: it will be cancelled and then removed. This cannot be undone."
        : "This action cannot be undone.",
      confirmLabel: "Delete",
    });
    if (!ok) return;
    deleteTask.mutate(task.task_id, {
      onSuccess: () => toast({ title: t_i18n("Background task deleted"), variant: "success" }),
      onError: () => toast({ title: t_i18n("Failed to delete task"), variant: "error" }),
    });
  };

  const handleBulkDelete = async () => {
    const title = allMatchingSelected
      ? t_i18n("Delete {count, plural, one {# task} other {# tasks}} matching current filters?", { count: totalMatchingCount })
      : t_i18n("Delete {count, plural, one {# selected task} other {# selected tasks}}?", { count: selectedIds.size });
    const ok = await confirm({
      title,
      description:
        "In-flight tasks are cancelled first. This action cannot be undone.",
      confirmLabel: "Delete",
    });
    if (!ok) return;
    // Bulk delete by filters has no execution filter: with it on, the selected rows go by id.
    const payload = allMatchingSelected && !livenessParam
      ? {
          all_matching_filters: true,
          vis,
          status: statusParam || undefined,
          search: debouncedSearch || undefined,
        }
      : { task_ids: Array.from(selectedIds) };
    bulkDelete.mutate(payload, {
      onSuccess: (data) => {
        toast({
          title: t_i18n("{count, plural, one {# task deleted} other {# tasks deleted}}", { count: data.deleted }),
          variant: "success",
        });
        setSelectedIds(new Set());
        setAllMatchingSelected(false);
      },
      onError: () => toast({ title: t_i18n("Failed to delete tasks"), variant: "error" }),
    });
  };

  // The toolbar stays mounted while the list loads; the skeleton fills the
  // list slot only.
  const listLoading = landing || isLoading;

  // The table's state lives in the URL keys this page always had (`q`, `page`,
  // `sort`) plus `dir` and `size`: old links keep working (#9378 slice 8e).
  const tableState: DataTableState = {
    search,
    sort: { key: sortKey, direction: sortDir },
    page,
    pageSize,
    filters: {},
  };
  const onTableState = (next: DataTableState) => {
    setSearchParams(
      (prev) => {
        const params = new URLSearchParams(prev);
        const set = (key: string, value: string | undefined) => (value ? params.set(key, value) : params.delete(key));
        const searchChanged = next.search !== search;
        set("q", next.search || undefined);
        const nextKey = next.sort && SORTS[next.sort.key] ? next.sort.key : DEFAULT_SORT;
        const nextDir = next.sort?.direction ?? DEFAULT_DIR;
        const isDefault = nextKey === DEFAULT_SORT && nextDir === DEFAULT_DIR;
        set("sort", isDefault ? undefined : nextKey);
        set("dir", isDefault ? undefined : nextDir);
        set("size", next.pageSize === DEFAULT_PAGE_SIZE ? undefined : String(next.pageSize));
        set("page", !searchChanged && next.page > 0 ? String(next.page) : undefined);
        return params;
      },
      { replace: true },
    );
  };

  // A run's task opens the run; a chat's task has no page of its own.
  const openTask = (task: SubAgentTask) => {
    if (task.assignment_run_id) navigate(`/operations/executions/${task.assignment_run_id}`);
  };

  const statusLabels: Record<TaskStatus, string> = {
    pending: t_i18n("Pending"),
    running: t_i18n("Running"),
    completed: t_i18n("Completed"),
    incomplete: t_i18n("Incomplete"),
    failed: t_i18n("Failed"),
    cancelled: t_i18n("Cancelled"),
  };

  const taskTitle = (task: SubAgentTask) =>
    task.task_description || `${task.parent_agent_name || t_i18n("Unknown agent")} → ${task.target_agent_name || t_i18n("Unknown agent")}`;

  // The row's own actions, one OpsRowAction each: rate, kill while in flight, delete.
  const taskActions = (task: SubAgentTask) => (
    <>
      {isRateableHere(task, user?.id) && (
        <AnnotationButtons
          targetType="sub_agent_task"
          targetId={task.task_id}
          batched={!myRatings.isError}
        />
      )}
      {ACTIVE_STATUSES.has(task.status) && (
        <OpsRowAction
          icon={X}
          tone="danger"
          label={t_i18n("Kill task")}
          onClick={() => handleCancel(task)}
          busy={cancelTask.isPending}
        />
      )}
      <OpsRowAction
        icon={Trash2}
        tone="danger"
        label={t_i18n("Delete")}
        onClick={() => handleDelete(task)}
        busy={deleteTask.isPending}
      />
    </>
  );

  // layout="fixed" with the widths of TASK_COLUMN_WIDTHS: the Task title takes what is
  // left and the long texts truncate with the DS tooltip, so the list never scrolls
  // sideways at 1080 (as Executions and Work items).
  const columns: DataTableColumn<SubAgentTask>[] = [
    {
      key: "task",
      header: "Task",
      cell: (task) => <TruncatedText className="font-medium" text={taskTitle(task)} />,
      csv: (task) => taskTitle(task),
    },
    {
      key: "status",
      header: "Status",
      width: TASK_COLUMN_WIDTHS.status,
      cell: (task) => {
        const cfg = STATUS_CONFIG[task.status] ?? STATUS_CONFIG.pending;
        return (
          <span className="flex min-w-0 items-center gap-1.5">
            <Badge variant={cfg.badge} className="content-compact">{t_i18n(cfg.label)}</Badge>
            {task.held_reason && <HeldBadge reason={task.held_reason} className="content-compact" />}
          </span>
        );
      },
      sortValue: (task) => task.status,
      csv: (task) => task.status,
      hideable: true,
    },
    {
      key: "from",
      header: "Delegated by",
      width: TASK_COLUMN_WIDTHS.from,
      cell: (task) => <AgentChip name={task.parent_agent_name} icon={task.parent_agent_icon} />,
      sortValue: (task) => task.parent_agent_name ?? "",
      csv: (task) => task.parent_agent_name ?? "",
      hideable: true,
    },
    {
      key: "to",
      header: "Delegated to",
      width: TASK_COLUMN_WIDTHS.to,
      // Below a 1152 px box the Task title keeps its room; the Columns pill brings it back.
      hideBelow: "6xl",
      cell: (task) => <AgentChip name={task.target_agent_name} icon={task.target_agent_icon} />,
      sortValue: (task) => task.target_agent_name ?? "",
      csv: (task) => task.target_agent_name ?? "",
      hideable: true,
    },
    {
      key: "created",
      header: "Created",
      width: TASK_COLUMN_WIDTHS.created,
      // Below a 1152 px box the Task title keeps its room; Updated stays, the Columns pill brings it back.
      hideBelow: "6xl",
      cell: (task) => <DateTimeCell value={task.created_at} />,
      sortValue: (task) => task.created_at ?? "",
      csv: (task) => task.created_at ?? "",
      firstSort: "desc",
      hideable: true,
    },
    {
      key: "updated",
      header: "Updated",
      width: TASK_COLUMN_WIDTHS.updated,
      cell: (task) => <DateTimeCell value={taskUpdatedAt(task)} />,
      sortValue: (task) => taskUpdatedAt(task) ?? "",
      csv: (task) => taskUpdatedAt(task) ?? "",
      firstSort: "desc",
      hideable: true,
    },
  ];

  // One task card, the grid view (the Operations row contract, OpsRowGrid).
  const renderTask = (task: SubAgentTask) => {
    const cfg = STATUS_CONFIG[task.status] ?? STATUS_CONFIG.pending;
    const StatusIcon = cfg.icon;
    const selected = selectedIds.has(task.task_id);
    const runId = task.assignment_run_id;
    const openRun = runId ? () => navigate(`/operations/executions/${runId}`) : undefined;
    const parentName = task.parent_agent_name || t_i18n("Unknown agent");
    const targetName = task.target_agent_name || t_i18n("Unknown agent");
    const title = taskTitle(task);
    const showsExecution = OPEN_TASK_STATUSES.has(task.status) && !!task.execution_state;
    const zombie = hasNoLiveExecution(task);
    const durationMs =
      task.started_at && task.completed_at
        ? Date.parse(task.completed_at) - Date.parse(task.started_at)
        : null;
    // Every fact of the task, in the contract's order: the state, who
    // delegated it to whom, the run and execution ids, the error, then
    // the timestamps.
    const facts: OpsFact[] = [{ label: t_i18n("Status"), value: t_i18n(cfg.label) }];
    if (task.held_reason) {
      facts.push({ label: t_i18n("Held"), tone: "warning", value: heldExplanation(t_i18n, task.held_reason, isXtmOne) });
    }
    if (zombie) {
      facts.push({
        label: t_i18n("No live execution"),
        tone: "warning",
        value: t_i18n(
          "This task is still marked in flight but no execution is running it. The recovery sweep settles it; kill it to settle it now.",
        ),
      });
    }
    facts.push({ label: t_i18n("From"), value: parentName });
    facts.push({ label: t_i18n("To"), value: targetName });
    if (runId) facts.push({ label: t_i18n("Run"), value: <span className="font-mono">{runId}</span> });
    if (task.execution_id) {
      facts.push({ label: t_i18n("Orchestration"), value: <span className="font-mono">{task.execution_id}</span> });
    }
    if (task.error) facts.push({ label: t_i18n("Error"), value: task.error, testId: "background-task-fact-error" });
    if (task.created_at) facts.push({ label: t_i18n("Created"), value: formatInZone(task.created_at, zone, locale) });
    if (task.started_at) facts.push({ label: t_i18n("Started"), value: formatInZone(task.started_at, zone, locale) });
    if (task.completed_at) facts.push({ label: t_i18n("Ended"), value: formatInZone(task.completed_at, zone, locale) });
    if (durationMs !== null && durationMs >= 0) {
      facts.push({ label: t_i18n("Duration"), value: formatDurationShort(durationMs / 1000) });
    }
    if (task.last_progress_at) {
      facts.push({ label: t_i18n("Last progress"), value: formatInZone(task.last_progress_at, zone, locale) });
    }
    // The row shows the status (and the held chip), From and To, the
    // run and line 2; "+N" counts what only the hover card holds.
    const shownFacts =
      3 + (task.held_reason ? 1 : 0) + (zombie ? 1 : 0) + (runId ? 1 : 0) + (task.error ? 1 : 0);
    const hiddenFacts = Math.max(0, facts.length - shownFacts);
    return (
      <Card
        key={task.task_id}
        data-testid="background-task-row"
        className={cn(
          OPS_ROW_CARD,
          "group overflow-hidden backdrop-blur-sm layer-1 bg-elevation-default transition-all",
          runId && "cursor-pointer hover:bg-elevation-hover",
          task.status === "failed" && "border-feedback-error-primary/30",
          task.status === "incomplete" && "border-feedback-alert-primary/30",
          selected && "border-elevation-default bg-elevation-highlight/[0.04]",
        )}
        onClick={openRun}
      >
        <CardContent className="p-0">
          {/* The row: one height on every row (OPS_ROW_HEIGHT), nothing
              wraps; a click opens the run the task belongs to. */}
          <div className={cn(OPS_ROW_HEIGHT, "px-4 py-2.5")} data-testid="background-task-line">
            <OpsRowGrid
              actionsClassName={ACTIONS_COLUMN}
              actionsTestId="background-task-actions"
              lead={
                <>
                  <SelectionBox
                    checked={selected}
                    onToggle={() => toggleSelect(task.task_id)}
                    label={selected ? t_i18n("Deselect task") : t_i18n("Select task")}
                  />
                  <StatusIcon
                    className={cn("h-4 w-4 shrink-0", cfg.color, task.status === "running" && "animate-spin")}
                    aria-hidden="true"
                  />
                </>
              }
              main={
                <>
                  <div className="flex min-w-0 items-center gap-2 overflow-hidden whitespace-nowrap">
                    <OpsHoverCard title={title} facts={facts}>
                      <button
                        type="button"
                        className="min-w-[4rem] shrink truncate text-left font-medium hover:text-accent focus-visible:outline-none focus-visible:underline"
                        data-testid="background-task-title"
                        onClick={(e) => {
                          e.stopPropagation();
                          openRun?.();
                        }}
                      >
                        {title}
                      </button>
                    </OpsHoverCard>
                    <span className="flex shrink-0 items-center">
                      <Badge variant={cfg.badge} className="content-compact" data-testid="background-task-status">
                        {t_i18n(cfg.label)}
                      </Badge>
                    </span>
                    <OpsChipStrip testId="background-task-chips">
                      {task.held_reason && <HeldBadge reason={task.held_reason} className="content-compact" />}
                    </OpsChipStrip>
                  </div>
                  {/* Line 2, one line: the error, else the execution of an
                      in-flight task, else the result. One height on every
                      row (h-5, the execution line's chip strip), so a
                      row in flight is never taller than the others now
                      that OPS_ROW_HEIGHT is a floor (#8922). */}
                  {task.error ? (
                    <div className="h-5 min-w-0 truncate content-compact text-feedback-error-primary" data-testid="background-task-summary">
                      {task.error}
                    </div>
                  ) : showsExecution ? (
                    <TaskExecutionLine task={task} />
                  ) : (
                    <div className="h-5 min-w-0 truncate content-compact text-default-secondary" data-testid="background-task-summary">
                      {task.result ?? ""}
                    </div>
                  )}
                </>
              }
              meta={
                <>
                  <Tip content={`${parentName} → ${targetName}`}>
                    <span className="flex min-w-0 items-center gap-1 content-compact" data-testid="background-task-agents">
                      <AgentChip name={task.parent_agent_name} icon={task.parent_agent_icon} />
                      <ArrowRight className="h-3 w-3 shrink-0 text-default-secondary" aria-hidden="true" />
                      <AgentChip name={task.target_agent_name} icon={task.target_agent_icon} />
                    </span>
                  </Tip>
                  <span className="flex min-w-0 items-center gap-2">
                    {runId ? (
                      <OpsToken
                        icon={Hash}
                        tone="muted"
                        full={runId}
                        copy={runId}
                        copyLabel={t_i18n("Copy run ID {id}", { id: runId.slice(0, 8) })}
                        testId="background-task-run"
                        className="font-mono"
                      >
                        {runId.slice(0, 8)}
                      </OpsToken>
                    ) : (
                      <span aria-hidden="true" className="h-4" />
                    )}
                    <OpsMoreToken
                      count={hiddenFacts}
                      title={title}
                      facts={facts}
                      onOpen={openRun ?? (() => undefined)}
                      testId="background-task-more"
                    />
                  </span>
                </>
              }
              updated={<OpsUpdated at={taskUpdatedAt(task)} testId="background-task-updated" />}
              actions={taskActions(task)}
            />
          </div>
        </CardContent>
      </Card>
    );
  };

  // Not in a fleet, nor with the execution filter: bulk delete by filters has neither scope.
  const canSelectAllMatching = !fleet && !livenessParam && allVisibleSelected && hasMoreBeyondVisible && !allMatchingSelected;
  const selectionActions = (
    <span className="flex flex-wrap items-center gap-2" data-testid="background-tasks-bulk-actions">
      {canSelectAllMatching && (
        <Button variant="link" size="sm" className={cn(TOOLBAR_CONTROL_HEIGHT, "px-1 content-compact")} onClick={handleSelectAllMatching}>
          {t_i18n("select all {count}", { count: totalMatchingCount })}
        </Button>
      )}
      <Button
        variant="destructive"
        size="sm"
        className={cn(TOOLBAR_CONTROL_HEIGHT, "px-2 content-compact")}
        disabled={bulkDelete.isPending}
        onClick={handleBulkDelete}
        data-testid="background-tasks-bulk-delete"
      >
        {t_i18n("Delete")}
      </Button>
    </span>
  );

  // The execution filter and the fleet: the Filters pill of the table.
  const morePills = (
    <>
      <SegmentedFilter
        ariaLabel={t_i18n("Execution filter")}
        options={LIVENESS_FILTERS.map((f) => ({ ...f, label: t_i18n(f.label) }))}
        value={livenessParam ?? ""}
        onChange={(key) => updateParam("liveness", key || undefined)}
      />
      {!lockedFleet && <FleetFilter value={fleet} onChange={setFleet} className="w-full" />}
    </>
  );

  const emptyState = (
    <VisibilityEmptyHint
      value={visibility}
      counts={countData && allSegmentsCount ? { mine: countData.count, all: allSegmentsCount.count } : undefined}
      onShowAll={() => setVisibility("all")}
    >
      <EmptyState
        icon={Activity}
        title={
          // A segment other than All is a filter too (as on Executions): an empty
          // Shared, Company or Mine does not mean the platform has no tasks.
          debouncedSearch || statusParam || livenessParam || visibility !== "all"
            ? "No background tasks match your filters."
            : "No background tasks yet."
        }
        description="Background tasks appear when an agent delegates work to another agent."
      />
    </VisibilityEmptyHint>
  );

  return (
    <div className="space-y-4">
      {!hideHeader && (
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-3xl font-bold">{t_i18n("Background Tasks")}</h1>
            <p className="mt-2 text-default-secondary">
              {t_i18n("Agent-to-agent delegations running in the background. See what is in flight and kill anything stuck.")}
            </p>
          </div>
        </div>
      )}

      {/* The tasks on the shared DataTable (standard v2, #9378 slice 8e): server mode,
          state in the URL, Status visible on the toolbar with its counts (the in-flight
          numbers the header used to show), the execution filter and the fleet in the
          Filters pill, the grid kept on the Operations row contract. */}
      <DataTable
        rows={rows}
        columns={columns}
        rowKey={(task) => task.task_id}
        server={{ total: totalMatchingCount }}
        state={tableState}
        onStateChange={onTableState}
        storageKey={STORAGE_KEY}
        defaultPageSize={DEFAULT_PAGE_SIZE}
        defaultSort={{ key: DEFAULT_SORT, direction: DEFAULT_DIR }}
        searchPlaceholder={t_i18n("Search tasks...")}
        compactSearch={compact}
        filtersInPill
        noFilters="status, execution and fleet are server filters (toolbar Status pill and filterPills)"
        scope={lockedFleet ? undefined : { value: visibility, onChange: setVisibility, counts: segmentCounts }}
        toolbarLead={
          <span className="flex items-center gap-2" data-testid="background-tasks-filter-row">
            {toolbarLead}
            <OperationsStatusFilter
              statuses={TASK_STATUSES}
              labels={statusLabels}
              value={selectedStatuses}
              onChange={setStatuses}
              counts={statusCounts}
              compact={compact}
              testIdPrefix="background-tasks"
            />
          </span>
        }
        filterPills={morePills}
        externalFilterCount={moreFilterCount}
        // A Status chosen on the toolbar narrows the list too: Clear all shows for it (UX r1 B1 on #1004).
        toolbarFilterActive={selectedStatuses.length > 0}
        // Clear all clears what the count counts, the status and search too, in one write (#1001).
        onClearAll={() =>
          setSearchParams((prev) => clearFilterParams(prev, ["status", "liveness", ...(lockedFleet ? [] : ["fleet"])]), {
            replace: true,
          })
        }
        view={{ value: viewMode, onChange: setViewMode }}
        // One scroller, inside the list: the cards scroll in their box, the page does not.
        fitGrid
        // The cards only in grid view: the list view draws the table.
        renderGrid={
          viewMode === "grid"
            ? (items) => (
                <div className="space-y-2" data-testid="background-task-list">
                  {items.map(renderTask)}
                </div>
              )
            : undefined
        }
        renderGridSkeleton={(size) => (
          <OpsListSkeleton label={t_i18n("Loading...")} pageSize={size} actionsClassName={ACTIONS_COLUMN} actions={2} />
        )}
        isLoading={listLoading}
        emptyLabel={t_i18n("No background tasks yet.")}
        noResultsLabel={t_i18n("No background tasks match your filters.")}
        emptyContent={emptyState}
        onRowClick={openTask}
        rowActions={taskActions}
        rowTestId="background-task-table-row"
        rowActionsWidth={TASK_COLUMN_WIDTHS.actions}
        layout="fixed"
        selection={{
          selected: Array.from(selectedIds),
          onChange: (keys) => {
            setAllMatchingSelected(false);
            setSelectedIds(new Set(keys));
          },
          count: allMatchingSelected ? totalMatchingCount : undefined,
          actions: selectionActions,
        }}
      />
      {ConfirmEl}
    </div>
  );
}
