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

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { workItemPath, workItemsPath } from "@/lib/task-routes";
import {
  ListTodo, AlertTriangle, CheckCircle2, XCircle, Trash2, Eye,
  Clock, Hash, Circle, CircleDot, CircleDashed, Ban, CalendarClock, Hourglass, RotateCcw, User, History, GitPullRequest,
  RefreshCw, CheckCheck,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { InfoTip } from "@/components/ui/info-tip";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DataTable, TOOLBAR_CONTROL_HEIGHT, type DataTableColumn } from "@/components/ui/data-table";
import { WaitingForYou } from "@/components/decisions/WaitingForYou";
import { useWaitingDecisions } from "@/hooks/api/useDecisionApi";
import { TruncatedText } from "@/components/ui/truncated-text";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { VisibilityEmptyHint } from "@/components/ui/visibility-filter";
import { useVisibilityParam, visibilityQuery } from "@/hooks/useVisibilityParam";
import { writeVisibility } from "@/lib/visibility";
import { useSegmentCounts } from "@/hooks/api/useSegmentCounts";
import { EmptyState } from "@/components/ui/empty-state";
import { SelectionBox } from "@/components/ui/list-control-bar";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Tip } from "@/components/ui/tooltip";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { useAuth } from "@/hooks/useAuth";
import { useTick } from "@/hooks/useTick";
import { useDebounce } from "@/hooks/useDebounce";
import { useViewMode } from "@/hooks/useViewMode";
import { clearFilterParams, readTableState, type DataTableDefaults } from "@/lib/data-table-state";
import { cn, formatShortDateTime, pluralize, formatCompactNumber } from "@/lib/utils";
import { formatShortDateTimeIntl } from "@/lib/relative-time";
import { BUTTON_ROLE_FOCUS_RING } from "@/lib/button-role";
import {
  OPS_ROW_CARD, OPS_ROW_HEIGHT, OpsChipStrip, OpsHoverCard, OpsListSkeleton, OpsRowGrid, OpsToken, OpsUpdated, type OpsFact,
} from "@/components/ui/operations-list";
import { REFETCH_INTERVAL_STANDARD } from "@/lib/constants";
import {
  useWorkItems, useWorkItemCount, useStaleWorkItemCount, useDeleteWorkItem,
  useBulkDeleteWorkItems, useRequeueWorkItem, useWorkItemGraph, useWorkItemOwners, useWorkItemStatusCounts, type WorkItemOrder,
} from "@/hooks/api/useWorkItemApi";
import { WorkItemGraphView } from "@/components/work-items/WorkItemGraphView";
import { WorkItemStatusFilter, statusParamOf, statusesFromParam } from "./WorkItemStatusFilter";
import { useFormatter } from "@/i18n";
import { ScheduleActions } from "@/components/work-items/ScheduleActions";
import { ScheduleStateBadge, ScheduledSummary } from "@/components/work-items/ScheduledDetails";
import {
  WorkItemAgent, WorkItemBlockers, WorkItemHoldingRunLink, WorkItemImpacts, WorkItemOriginLink, WorkItemPerson,
  WorkItemPickupBadge, WorkItemVisibilityBadge, WorkItemBlockedWhy, WorkItemNoReasonChip, pickupWording,
} from "@/components/work-items/WorkItemContextParts";
import { WorkItemCloseDialog, type WorkItemCloseMode } from "@/components/work-items/WorkItemCloseDialog";
import { askPending, offersAskTheAgent, sendTarget, useAskTheAgent } from "@/components/work-items/WorkItemAskAgent";
import { REQUEUE_EFFECT, STALE_REASON_LABELS, staleBadgeText } from "@/lib/work-item-effects";
import { WorkItemDetailDialog, type WorkItemActions } from "@/components/work-items/WorkItemDetailDialog";
import { canChangeSchedule, formatInZone, isScheduledItem, viewerTimeZone } from "@/lib/scheduled-work-items";
import { markdownSummary } from "@/lib/markdown-summary";
import { formatList } from "@/lib/format-list";
import {
  BADGE_TIP_FOCUS, PRIORITY_LABELS, STATUS_LABELS, blockedKind, blockedReason, canClose, canRequeue, cancelledBlockers,
  listedBlockers, offersMarkDone, pullRequestsOf, unfinishedBlockers, waitingPullRequestCount, waitsForSomeone,
  blockedWithoutReason, pendingBlockedRefs, waitsForReader, answeredKeepsWaiting,
} from "@/lib/work-item-context";
import { BlockedStatusBadge } from "@/components/work-items/BlockedStatusBadge";
import { WaitingForYouBadge } from "@/components/work-items/WaitingForYouBadge";
import type { WorkItem, WorkItemStatus } from "@/types";
import { FleetAgentPickerGroup } from "@/components/fleets/FleetAgentPickerGroup";
import { useFleetParam } from "@/hooks/useFleetParam";
import { offersFleetPicker } from "@/lib/fleet-operations";

/** The table's keys in the URL (`wi_q`, `wi_sort`, `wi_page`, `wi_size`); the status keeps `?status=`. */
const URL_KEY = "wi_";

/** The columns the server sorts by (services/work_item.py `WORK_ITEM_TABLE_ORDERS`); the default is the newest first. */
const SORT_KEYS: readonly WorkItemOrder[] = ["created", "updated", "due", "priority", "status", "owner", "completed"];

// pageSize is one of the table's PAGE_SIZES (10, 25, 50, 100): a value outside them left the
// Rows per page select empty (UX round 1 on #11917, B2).
const DEFAULTS: DataTableDefaults = { sort: { key: "created", direction: "desc" }, pageSize: 25 };

/** The list view's fixed column widths (px, `layout="fixed"`): the title takes what is left. */
const COLUMN_WIDTHS = { status: 132, priority: 96, owner: 200, due: 132, created: 132, updated: 120 } as const;

/** The empty states' descriptions, catalog keys EmptyState translates. @i18n-keys */
const EMPTY_DESCRIPTIONS = {
  items:
    "Agents create durable work items to track follow-ups that survive runs. Enable the Work Items built-in tool on an agent to get started.",
  scheduled:
    'Ask any agent in a chat to do something once, later - "post this tomorrow at 9:00" - and it appears here, with no assignment to set up.',
  waiting:
    "Decisions agents ask of you, and your own items no agent owns that are past due, land here.",
} as const;

const ANY_AGENT = "__any__";

/** The views Work Items offers: rows, cards and the dependency graph (#968). */
const WORK_ITEM_VIEW_MODES = ["list", "grid", "graph"] as const;

// The administrators' switch on a fleet's Work Items tab: "Fleet"
// (the default, `?fleet=`, the population of the agents' `list_work_items`
// scope fleet), "Mine" (the reader's own items, every fleet) and "All"
// (every item the reader can see). `?scope=` keeps it in the URL. Labels are
// catalog keys read through t_i18n by SegmentedFilter.
const FLEET_SCOPE_FILTERS = [
  { key: "", label: "Fleet" },
  { key: "mine", label: "Mine" },
  { key: "all", label: "All" },
];

/** The scope a fleet's Work Items tab reads: only an administrator may leave the fleet. */
export function fleetListScope(param: string | null, isAdmin: boolean): "fleet" | "mine" | "all" {
  if (!isAdmin) return "fleet";
  return param === "mine" || param === "all" ? param : "fleet";
}

// The second segment narrows the list: the items waiting for the reader (the
// dashboard notice's population, its Review action lands here), the stale
// items, or the scheduled ones - one-off actions the platform runs at their
// time, which are ordinary work items and need no tab of their own. Catalog
// keys too.
const WAITING_FOR_ME_VIEW = "waiting";
const VIEW_FILTERS = [
  { key: "", label: "All items" },
  { key: WAITING_FOR_ME_VIEW, label: "Waiting for me" },
  { key: "stale", label: "Stale" },
  { key: "scheduled", label: "Scheduled" },
];

/** The rendered row of a work item, by its id. */
function rowElement(id: string): HTMLElement | null {
  return document.querySelector<HTMLElement>(`[data-testid="work-item-row"][data-work-item-id="${id}"]`);
}

/** The control a row gives focus to: its actions menu, else its first button. */
function rowFocusTarget(row: HTMLElement | null): HTMLElement | null {
  if (!row) return null;
  return row.querySelector<HTMLElement>('button[aria-haspopup="menu"]') ?? row.querySelector<HTMLElement>("button");
}

/**
 * Where focus goes after a close that leaves the view with no row: the Status pill on the toolbar, never the page body.
 */
function statusFilterFocusTarget(): HTMLElement | null {
  return document.querySelector<HTMLElement>('[data-testid="work-items-status-pill"]');
}

/** The row that takes the closed row's place, else the status filter. */
function neighbourFocusTarget(next: string | null): HTMLElement | null {
  return rowFocusTarget(next ? rowElement(next) : null) ?? statusFilterFocusTarget();
}

/** A scheduled item that has not settled: cancelled, never deleted. */
function isLiveScheduled(item: WorkItem): boolean {
  return !!item.scheduled && ["open", "claimed", "in_progress"].includes(item.status);
}

const STATUS_ICON: Record<WorkItemStatus, { icon: React.ElementType; color: string }> = {
  open: { icon: Circle, color: "text-accent" },
  claimed: { icon: CircleDot, color: "text-filigran-ia-primary" },
  in_progress: { icon: CircleDashed, color: "text-feedback-warning-primary" },
  blocked: { icon: Ban, color: "text-feedback-error-primary" },
  done: { icon: CheckCircle2, color: "text-feedback-success-primary" },
  cancelled: { icon: XCircle, color: "text-default-secondary" },
};

// A blocked item that only waits for open dependencies reopens by itself: its
// row icon is neutral, like its "Waiting" chip, never the error Ban.
const WAITING_ICON: { icon: React.ElementType; color: string } = { icon: Hourglass, color: "text-default-secondary" };

/** The leading icon of a row: the status icon, neutral for a blocked item that waits. */
function rowStatusIcon(item: WorkItem): { icon: React.ElementType; color: string } {
  if (blockedKind(item) === "waiting") return WAITING_ICON;
  return STATUS_ICON[item.status] ?? STATUS_ICON.open;
}

const STATUS_BADGE: Record<WorkItemStatus, string> = {
  open: "text-accent bg-accent/10",
  claimed: "text-filigran-ia-primary bg-filigran-ia-primary/10",
  in_progress: "text-feedback-warning-tertiary bg-feedback-alert-primary/10",
  blocked: "text-feedback-error-primary bg-feedback-error-primary/10",
  done: "text-feedback-success-primary bg-feedback-success-primary/10",
  cancelled: "text-default-secondary bg-elevation-hover",
};

const PRIORITY_BADGE: Record<string, string> = {
  low: "text-default-secondary bg-elevation-hover",
  medium: "text-accent bg-accent/10",
  high: "text-feedback-warning-tertiary bg-feedback-alert-primary/10",
  urgent: "text-feedback-error-primary bg-feedback-error-primary/10",
};

const INACTIVITY_STATUSES = new Set<WorkItemStatus>(["claimed", "in_progress", "blocked"]);

// Human-readable reason mirroring the backend staleness rule so the
// "stale" badge explains itself: past an explicit due date, or claimed/quiet.
function staleReason(item: WorkItem, translate: (text: string) => string = (text) => text): string {
  // The server names its reason since #9879; older servers fall through to
  // the derivation below.
  if (item.stale_reason && item.stale_reason in STALE_REASON_LABELS) {
    return translate(STALE_REASON_LABELS[item.stale_reason]);
  }
  if (item.due_at && new Date(item.due_at).getTime() < Date.now()) {
    return `Past due since ${formatShortDateTime(item.due_at)}.`;
  }
  if (INACTIVITY_STATUSES.has(item.status)) {
    // Mirror the backend, which anchors inactivity on last_activity_at and
    // falls back to created_at (e.g. legacy rows with a null last_activity_at),
    // so the tooltip never degrades to the generic message for a real stale item.
    const since = item.last_activity_at ?? item.created_at;
    if (since) {
      return `${item.status.replace("_", " ")} but no activity since ${formatShortDateTime(since)}.`;
    }
  }
  return "Needs attention.";
}

/**
 * The actions column of a row: one width on every row of the tab, so
 * the updated column and the metadata column sit at the same x on every row.
 * Below 2xl the actions are icons ("Mark done" names itself to a screen reader
 * and in a tooltip): a scheduled row's four icons fit 8rem; from 2xl "Mark
 * done" shows its label and the column is 15rem (fr 231 px).
 */
const ACTIONS_COLUMN = "w-32 2xl:w-60";

/**
 * `workspace` locks the list on a workspace (its page's Operations tab):
 * what is filed there or owned or created by its fleet's members,
 * every segment by default. `fleet` locks it on a fleet (its page's
 * Operations tab); elsewhere `?fleet=` narrows it to what a fleet's
 * home members own or created. `itemInPath`: the Tasks tab (#775), where one
 * item opens as its own path (`/tasks/work-items/<id>`) instead of `?item=`.
 */
export default function WorkItemsTab({
  workspace,
  fleet: lockedFleet,
  emptyDescription,
  toolbarLead,
  itemInPath = false,
}: {
  workspace?: string;
  fleet?: string;
  itemInPath?: boolean;
  emptyDescription?: string;
  /** A control of the host drawn first on the filter row (the fleet tab's list select), never a row of its own. */
  toolbarLead?: ReactNode;
} = {}) {
  useTick(REFETCH_INTERVAL_STANDARD); // keep relative timestamps fresh
  const { isAdmin, user } = useAuth();
  const { t_i18n, locale, intl } = useFormatter();

  // --- Filter state lives in the URL so back-navigation restores it. The table's own keys
  // (search, sort, page, page size) are `wi_` keys (dev-docs/reference/data-tables.md, slice 8d);
  // the status, view, owner and fleet keep their names, which links elsewhere already write.
  const [searchParams, setSearchParams] = useSearchParams();
  const [viewMode, setViewMode] = useViewMode("operations-work-items");
  const table = readTableState(searchParams, URL_KEY, DEFAULTS);
  // The legacy `?q=` of a link written before the table: read until the table writes its own.
  const search = table.search || (searchParams.get("q") ?? "");
  const statusParam = searchParams.get("status") ?? "";
  const statuses = statusesFromParam(statusParam);
  // `view` narrows the list (stale, scheduled); `stale=stale` is its older spelling.
  const viewParam = searchParams.get("view") ?? (searchParams.get("stale") === "stale" ? "stale" : "");
  const agentParam = searchParams.get("agent") ?? "";
  // Nothing is asked while a `?fleet=` waits on the reader's fleet access.
  const [fleetParam, setFleet, fleetPending] = useFleetParam(lockedFleet);
  // On a fleet's tab, an administrator may read Mine or All instead.
  const fleetScope = lockedFleet ? fleetListScope(searchParams.get("scope"), isAdmin) : "fleet";
  const fleet = fleetScope === "fleet" ? fleetParam : undefined;
  // A row, or a run's "Scheduled" badge, opens one item's details: on Tasks its own path, on a host page `?item=`.
  const { itemId: pathItemId } = useParams<{ itemId?: string }>();
  const navigate = useNavigate();
  const openId = itemInPath ? (pathItemId ?? null) : searchParams.get("item");
  const page = table.page;
  const pageSize = table.pageSize;
  const debouncedSearch = useDebounce(search, 300);
  const staleOnly = viewParam === "stale";
  const scheduledOnly = viewParam === "scheduled";
  const waitingForMe = viewParam === WAITING_FOR_ME_VIEW;
  // "Waiting for me" with the read switch on is the decision inbox alone: one records list per
  // view (UX r1 B2 on #1081). Only a server answer `enabled: false` (switch off) falls back to
  // the per-engine work items list: while the read is pending or failed the inbox renders its
  // own skeleton or error line with Retry (UX r2 B1 on #1081).
  const waitingDecisions = useWaitingDecisions(waitingForMe);
  const inboxView = waitingForMe && waitingDecisions.data?.enabled !== false;
  const sortKey = table.sort && (SORT_KEYS as readonly string[]).includes(table.sort.key) ? (table.sort.key as WorkItemOrder) : "created";
  const direction = table.sort?.direction ?? "desc";

  const updateParam = useCallback(
    (key: string, value: string | undefined) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (value) next.set(key, value);
          else next.delete(key);
          // A filter changes the list: back to its first page (the legacy `page` too).
          if (key !== "item") {
            next.delete(`${URL_KEY}page`);
            next.delete("page");
          }
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  // The view (`view`, its older spelling `stale=stale` dropped): one write, back to the first page.
  // The Filters pill's View segment and the quick filters of the row write it alike (#1001).
  const setView = useCallback(
    (key: string) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.delete("stale");
          if (key) next.set("view", key);
          else next.delete("view");
          next.delete(`${URL_KEY}page`);
          next.delete("page");
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  // The "Waiting for me" chip (#1499): on, it resets every other filter (status, search, owner,
  // fleet unless locked, scope, segment to All) in the same write so the rows shown are the
  // chip's count; a second click clears the view alone.
  const toggleQuickView = useCallback(
    (key: string) => {
      if (key !== WAITING_FOR_ME_VIEW || key === viewParam) {
        setView(key === viewParam ? "" : key);
        return;
      }
      const keys = ["view", "stale", "scope", "agent", "status", ...(lockedFleet ? [] : ["fleet"])];
      setSearchParams(
        (prev) => {
          const next = clearFilterParams(prev, keys, URL_KEY);
          if (!workspace && !lockedFleet) {
            const all = writeVisibility(next, "all");
            all.set("view", key);
            return all;
          }
          next.set("view", key);
          return next;
        },
        { replace: true },
      );
    },
    [viewParam, setView, setSearchParams, lockedFleet, workspace],
  );

  // "Clear all filters" (toolbar, no-results line, empty states): every filter the count counts
  // and the status, search and page in ONE write; the open item, the segment (`vis`), the sort
  // and the page size stay (#1001: the table cleared only its own keys, the button looked dead).
  // A locked fleet is the host page's, never a filter.
  const clearAllFilters = useCallback(() => {
    const keys = ["view", "stale", "scope", "agent", "status", ...(lockedFleet ? [] : ["fleet"])];
    setSearchParams((prev) => clearFilterParams(prev, keys, URL_KEY), { replace: true });
  }, [setSearchParams, lockedFleet]);

  // One write for the Status filter: its URL value (none = open work, `all` = every status).
  const setStatuses = useCallback(
    (next: WorkItemStatus[]) => updateParam("status", statusParamOf(next)),
    [updateParam],
  );

  // Open work (no `status` key) asks `open_only`; every status asks nothing; a
  // selection asks its statuses joined by commas (`blocked,done`).
  const statusQuery = statusParamOf(statuses);
  const isExactStatus = statusQuery !== undefined && statusQuery !== "all";
  const otherFilters = {
    status: isExactStatus ? statusQuery : undefined,
    open_only: statusQuery === undefined,
    stale_only: staleOnly,
    scheduled_only: scheduledOnly,
    waiting_for_me: waitingForMe,
    search: debouncedSearch || undefined,
    owner_agent_id: agentParam || undefined,
    workspace,
    fleet,
  };
  // Each segment's count under the other filters, the numbers on the filter.
  const segmentCounts = useSegmentCounts(["work-items"], "/work-items", otherFilters, { enabled: !fleetPending });
  // Mine | Company | Shared | All (`?vis=`); with none named, the first segment
  // holding an item once the counts are in. All in a workspace, whose
  // list is the team's, and on a fleet.
  const [visibility, setVisibility, landingVisibility] = useVisibilityParam(
    undefined,
    workspace || lockedFleet ? "all" : undefined,
    { counts: segmentCounts, enabled: !workspace && !lockedFleet },
  );
  const landing = landingVisibility || fleetPending;
  // The filters behind the Filters pill that narrow the list: its badge. The status is on
  // the toolbar itself and counts on its own pill.
  const showFleetPicker = offersFleetPicker({ fleet: lockedFleet, workspace });
  const moreFilterCount =
    (lockedFleet && isAdmin && fleetScope !== "fleet" ? 1 : 0) +
    (viewParam ? 1 : 0) +
    (showFleetPicker && fleetParam ? 1 : 0) +
    (agentParam ? 1 : 0);
  const vis = fleetScope === "mine" ? "mine" : visibilityQuery(visibility);
  const filters = { ...otherFilters, vis };
  // Done and Cancelled alone read by completion, newest first (#8558), unless a column sort
  // was chosen: the order an audit of what just ended needs.
  const terminalView = statuses.length > 0 && statuses.every((s) => s === "done" || s === "cancelled");
  const defaultOrder = !table.sort || (table.sort.key === DEFAULTS.sort?.key && table.sort.direction === DEFAULTS.sort?.direction);
  // The count of each status under the other filters: the numbers of the Status pill.
  const { data: statusCounts } = useWorkItemStatusCounts({ ...otherFilters, vis }, { enabled: !landing });

  const { data: items, isLoading } = useWorkItems(
    {
      ...filters,
      sort: terminalView && defaultOrder ? "completed_desc" : undefined,
      order: defaultOrder ? undefined : sortKey,
      direction: defaultOrder ? undefined : direction,
      limit: pageSize,
      offset: page * pageSize,
    },
    !landing,
  );
  // The Graph view's one read (#968): the list's filters, no page, no sort.
  const graphView = viewMode === "graph";
  const { data: graph, isLoading: graphLoading } = useWorkItemGraph(filters, graphView && !landing);
  const { data: countData } = useWorkItemCount(filters, false, !landing);
  const totalMatchingCount = countData?.count ?? 0;
  // The blocked items under the other filters: how many wait on
  // dependencies and how many need an action, said on the Blocked filter.
  // Asked once the segment is known, as the list.
  const { data: blockedItems } = useWorkItems(
    { ...filters, status: "blocked", open_only: false, limit: 100, offset: 0 },
    !landing,
  );
  // What the Blocked line of the Status pill adds: how many wait, how many need an action.
  const blockedHint = useMemo(() => {
    const kinds = (blockedItems ?? []).map((item) => blockedKind(item));
    const waiting = kinds.filter((k) => k === "waiting").length;
    const action = kinds.filter((k) => k === "action").length;
    if (!waiting || !action) return undefined;
    return t_i18n("{waiting} waiting on dependencies, {action} needing an action", { waiting, action });
  }, [blockedItems, t_i18n]);
  // The blocked items that say nothing of what they wait for: a red
  // chip next to the status filter until each one is edited.
  const noReasonCount = useMemo(() => (blockedItems ?? []).filter((item) => blockedWithoutReason(item)).length, [blockedItems]);
  // The quick filters' counts (#1001): the server's, under every other filter (status, search,
  // owner, fleet, segment), never the loaded page; the view itself left out so each chip says
  // what a click on it would show.
  const quickBase = { ...otherFilters, stale_only: false, scheduled_only: false, waiting_for_me: false, vis };
  // "Waiting for me" counts every open item waiting for the caller (#1499): every segment, no
  // status, search, owner or picked fleet, so the chip reads the same number wherever the list
  // stands and a click (which resets those filters) shows exactly that many rows. A host page's
  // workspace or locked fleet is not a filter and stays: the lock is counted whatever `?scope=` an
  // administrator set, since the click clears that scope and lists inside the lock.
  const waitingFilters = { open_only: true, waiting_for_me: true, workspace, fleet: lockedFleet };
  const { data: waitingCountData } = useWorkItemCount(waitingFilters, false, !fleetPending);
  const { data: staleViewCountData } = useWorkItemCount({ ...quickBase, stale_only: true }, false, !landing);
  const quickOptions = [
    {
      key: WAITING_FOR_ME_VIEW,
      label: "Waiting for me",
      icon: User,
      count: waitingCountData?.count,
      countPending: waitingCountData === undefined,
    },
    { key: "stale", label: "Stale", icon: AlertTriangle, count: staleViewCountData?.count, countPending: staleViewCountData === undefined },
  ];
  // The same filters over every segment: the empty-Mine hint reads it.
  const { data: allSegmentsCount } = useWorkItemCount(otherFilters, false, !fleetPending);
  // Bulk delete skips server-side what the caller may not delete (a
  // non-admin's company-managed rows, and for everyone a scheduled item that
  // has not run, which is cancelled instead), so the "select all matching"
  // affordance promises the *deletable* count, never the visible total.
  const { data: deletableData } = useWorkItemCount(filters, true, !landing);
  const deletableMatchingCount = deletableData?.count ?? 0;
  // The stale banner counts the workspace's items in a workspace.
  // A fleet's too, and on a fleet's tab whatever its switch reads.
  const scoped = !!workspace || !!fleet || !!lockedFleet;
  const { data: globalStaleCount } = useStaleWorkItemCount(vis, !scoped && !landing);
  const { data: workspaceStaleCount } = useWorkItemCount(
    { stale_only: true, vis, workspace, fleet },
    false,
    scoped && !landing,
  );
  const staleCount = scoped ? workspaceStaleCount : globalStaleCount;
  const { data: owners } = useWorkItemOwners(filters, { enabled: !landing });
  // The filter offers the owners the reader may name; the unassigned items and
  // the agents they cannot open are not a choice it can make.
  const namedOwners = useMemo(
    () =>
      (owners ?? []).flatMap((o) =>
        o.agent?.accessible && o.agent.id ? [{ id: o.agent.id, name: o.agent.name ?? "", count: o.count }] : [],
      ),
    [owners],
  );
  const del = useDeleteWorkItem();
  const askAgent = useAskTheAgent();
  const requeue = useRequeueWorkItem();
  const bulkDelete = useBulkDeleteWorkItems();
  const { toast } = useToast();
  const [confirmNode, confirm] = useConfirm();
  const [closing, setClosing] = useState<{ item: WorkItem; mode: WorkItemCloseMode } | null>(null);

  const canManage = useCallback(
    (item: WorkItem) => !item.is_company_managed || isAdmin,
    [isAdmin],
  );
  // A scheduled item that has not settled is cancelled, never deleted.
  const canDelete = useCallback(
    (item: WorkItem) => canManage(item) && !isLiveScheduled(item),
    [canManage],
  );
  // Why a row has no selectable box: every row keeps the selection column, so
  // the list stays aligned whatever the item's kind. The reason names
  // a control the row really has: a running scheduled item has no Cancel.
  const notSelectableReason = (item: WorkItem): string => {
    if (!canManage(item)) return t_i18n("Only an administrator can delete a company-managed work item.");
    if (item.scheduled && !canChangeSchedule(item.scheduled.state)) {
      return t_i18n("A scheduled work item that is running cannot be deleted.");
    }
    return t_i18n("A scheduled work item that has not run is cancelled, not deleted: use Cancel on its row.");
  };

  // -- Selection state (only items the caller may delete are selectable) --
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [allMatchingSelected, setAllMatchingSelected] = useState(false);

  // Clear selection when filters change (adjust-state-during-render pattern,
  // matching TaskHistory / BackgroundTasks — resets in render rather than an
  // effect to avoid an extra commit + a flash of stale selection).
  const filterKey = `${statusParam}|${viewParam}|${agentParam}|${visibility}|${debouncedSearch}|${fleet ?? ""}`;
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

  const selectableIds = useMemo(
    () => (items ?? []).filter(canDelete).map((i) => i.id),
    [items, canDelete],
  );
  const allVisibleSelected =
    selectableIds.length > 0 && selectableIds.every((id) => selectedIds.has(id));

  const handleSelectAllMatching = useCallback(() => {
    setAllMatchingSelected(true);
    setSelectedIds(new Set(selectableIds));
  }, [selectableIds]);

  const handleClearSelection = useCallback(() => {
    setSelectedIds(new Set());
    setAllMatchingSelected(false);
  }, []);

  const hasMoreBeyondVisible = deletableMatchingCount > selectableIds.length;

  const openDetails = useCallback(
    (item: WorkItem) => (itemInPath ? navigate(workItemPath(item.id, searchParams)) : updateParam("item", item.id)),
    [itemInPath, navigate, searchParams, updateParam],
  );
  const closeDetails = useCallback(
    () => (itemInPath ? navigate(workItemsPath(searchParams), { replace: true }) : updateParam("item", undefined)),
    [itemInPath, navigate, searchParams, updateParam],
  );

  const handleRequeue = useCallback(async (item: WorkItem) => {
    try {
      await requeue.mutateAsync(item.id);
      if (openId === item.id) closeDetails();
      toast({ title: t_i18n("Work item put back in the queue"), description: t_i18n(REQUEUE_EFFECT), variant: "success" });
    } catch (e) {
      toast({
        title: t_i18n("Failed to update work item"),
        description: e instanceof Error ? e.message : undefined,
        variant: "error",
      });
    }
  }, [requeue, openId, closeDetails, toast, t_i18n]);

  const handleDelete = useCallback(async (item: WorkItem) => {
    const ok = await confirm({
      title: "Delete this work item?",
      description: "It is removed for good, with its result and payload. To stop it and keep its trail, cancel it instead.",
      confirmLabel: "Delete",
      variant: "danger",
    });
    if (!ok) return;
    try {
      await del.mutateAsync(item.id);
      if (openId === item.id) closeDetails();
      toast({ title: t_i18n("Work item deleted"), variant: "success" });
    } catch {
      toast({ title: t_i18n("Failed to delete work item"), variant: "error" });
    }
  }, [confirm, del, openId, closeDetails, toast, t_i18n]);

  const actionsFor = useCallback((item: WorkItem): WorkItemActions => {
    if (!canManage(item)) return {};
    return {
      close: (target, mode) => setClosing({ item: target, mode }),
      requeue: handleRequeue,
      remove: canDelete(item) ? handleDelete : undefined,
      answer: true,
      busy: requeue.isPending || del.isPending,
    };
  }, [canManage, canDelete, handleRequeue, handleDelete, requeue.isPending, del.isPending]);

  const handleBulkDelete = async () => {
    const title = allMatchingSelected
      ? t_i18n("Delete {count, plural, one {# work item} other {# work items}} matching current filters?", { count: deletableMatchingCount })
      : t_i18n("Delete {count, plural, one {# selected work item} other {# selected work items}}?", { count: selectedIds.size });
    const ok = await confirm({
      title,
      description: "This action cannot be undone.",
      confirmLabel: "Delete",
      variant: "danger",
    });
    if (!ok) return;
    const payload = allMatchingSelected
      ? { all_matching_filters: true, ...filters }
      : { ids: Array.from(selectedIds) };
    bulkDelete.mutate(payload, {
      onSuccess: (data) => {
        toast({
          title: `${formatCompactNumber(data.deleted)} ${pluralize(data.deleted, "work item")} deleted`,
          variant: "success",
        });
        setSelectedIds(new Set());
        setAllMatchingSelected(false);
      },
      onError: () => toast({ title: t_i18n("Failed to delete work items"), variant: "error" }),
    });
  };

  // Focus after an item is closed by hand: its "Mark done" button
  // unmounts once the item is done, so focus goes to the row's actions menu.
  // On a view the done item leaves (Open), the row goes on the next refetch:
  // focus then moves to the row that took its place, never to the page body.
  const refocusRow = useRef<{ id: string; next: string | null } | null>(null);
  // The target is recorded when the dialog OPENS, never at close: a
  // confirm clears `closing` before the dialog fires its close
  // focus event, and the mutation awaits its refetch, so on a view the item
  // leaves (Open) the row is already gone when the dialog closes. The ref
  // keeps the row's id and its neighbour (the next row, the previous one when
  // it is last), read by a callback that keeps its identity.
  const closingTarget = useRef<{ id: string; next: string | null } | null>(null);
  useEffect(() => {
    if (!closing) return;
    const id = closing.item.id;
    const row = rowElement(id);
    const sibling = (el: Element | null | undefined): string | null =>
      el instanceof HTMLElement && el.dataset.testid === "work-item-row" ? el.dataset.workItemId ?? null : null;
    closingTarget.current = { id, next: sibling(row?.nextElementSibling) ?? sibling(row?.previousElementSibling) };
  }, [closing]);
  const focusTargetAfterClose = useCallback((): HTMLElement | null => {
    const target = closingTarget.current;
    if (!target) return null;
    const row = rowElement(target.id);
    if (row) {
      // The row may still leave on a later refetch: the [items] effect below
      // then moves focus to its neighbour.
      refocusRow.current = target;
      return rowFocusTarget(row);
    }
    return neighbourFocusTarget(target.next);
  }, []);
  useEffect(() => {
    const pending = refocusRow.current;
    if (!pending || !items || items.some((i) => i.id === pending.id)) return;
    refocusRow.current = null;
    const active = document.activeElement;
    if (active && active !== document.body && active.isConnected) return;
    neighbourFocusTarget(pending.next)?.focus();
  }, [items]);

  // The toolbar stays mounted while the list loads; the skeleton fills the
  // list slot only.
  const listLoading = landing || isLoading;

  const displayItems = items ?? [];

  const menuItems = (item: WorkItem): OverflowMenuItem[] => {
    const actions = actionsFor(item);
    const entries: OverflowMenuItem[] = [
      { id: "details", label: t_i18n("View details"), icon: Eye, onSelect: () => openDetails(item) },
    ];
    if (offersAskTheAgent(item) && !isScheduledItem(item) && sendTarget(item) && !askPending(item)) {
      // Sent at once to the item's agent, as from the dialog (#774).
      entries.push(
        {
          id: "ask-recheck",
          label: t_i18n("Ask the agent to re-check"),
          icon: RefreshCw,
          separatorBefore: true,
          disabled: askAgent.aiOff,
          disabledReason: askAgent.aiReason ?? undefined,
          onSelect: () => void askAgent.send(item, "recheck"),
        },
        {
          id: "ask-solved",
          label: t_i18n("Tell the agent it's solved"),
          icon: CheckCheck,
          disabled: askAgent.aiOff,
          disabledReason: askAgent.aiReason ?? undefined,
          onSelect: () => void askAgent.send(item, "solved"),
        },
      );
    }
    if (actions.close && canClose(item)) {
      entries.push({
        id: "cancel",
        label: t_i18n("Cancel work item (the agent stops)"),
        icon: XCircle,
        onSelect: () => actions.close?.(item, "cancelled"),
      });
    }
    if (actions.requeue && canRequeue(item)) {
      entries.push({
        id: "requeue",
        label: t_i18n("Put back in the queue (the agent runs it again)"),
        icon: RotateCcw,
        onSelect: () => actions.requeue?.(item),
      });
    }
    if (actions.remove) {
      entries.push({
        id: "delete",
        label: t_i18n("Delete"),
        icon: Trash2,
        destructive: true,
        separatorBefore: true,
        onSelect: () => actions.remove?.(item),
      });
    }
    return entries;
  };

  const waitsText = (pickup: string) =>
    pickup === "blocked"
      ? t_i18n("Waits for someone to put it back in the queue or close it.")
      : t_i18n("Waits for someone: no inbox takes it.");

  // Every fact of a row, in the contract's order: the hover card's
  // body, names whole. The row itself shows the essentials only.
  const zone = viewerTimeZone(user?.timezone);
  const factsOf = (item: WorkItem): OpsFact[] => {
    const context = item.context;
    const pickup = context?.pickup.state;
    const scheduled = isScheduledItem(item);
    const pullRequests = pullRequestsOf(item.payload);
    const facts: OpsFact[] = [];
    // Every unfinished blocker item of a blocked item, whatever the pickup
    // state (a person's decision too), as the drawer lists them.
    const blockers = item.status === "blocked" ? listedBlockers(item) : [];
    if (blockers.length) {
      facts.push({ label: t_i18n("Waits for"), tone: "warning", value: <WorkItemBlockers blockers={blockers} /> });
    }
    if (item.status === "blocked" && (pendingBlockedRefs(item).length || item.blocked_reason?.trim() || item.needs_human)) {
      facts.push({
        label: blockers.length ? t_i18n("Also waits for") : t_i18n("Waits for"),
        tone: "warning",
        value: <WorkItemBlockedWhy refs={pendingBlockedRefs(item)} reason={item.blocked_reason} needsHuman={!!item.needs_human && !answeredKeepsWaiting(item)} />,
      });
    }
    if (context?.owner_agent) {
      facts.push({ label: t_i18n("Owner"), value: <WorkItemAgent agent={context.owner_agent} /> });
    } else if (!scheduled) {
      facts.push({ label: t_i18n("Owner"), value: <span className="text-feedback-warning-primary">{t_i18n("nobody")}</span> });
    }
    if (context?.fleet_impact?.agents.length) {
      facts.push({ label: t_i18n("Impacts"), value: <WorkItemImpacts impact={context.fleet_impact} /> });
    }
    if (item.claimed_by_run && (item.status === "claimed" || item.status === "in_progress")) {
      facts.push({ label: t_i18n("Held by"), value: <WorkItemHoldingRunLink run={item.claimed_by_run} /> });
    } else if (context?.claimed_by_agent && item.status !== "open") {
      facts.push({ label: t_i18n("Held by"), value: <WorkItemAgent agent={context.claimed_by_agent} /> });
    }
    if (context?.created_by_agent && !scheduled) {
      facts.push({ label: t_i18n("Created by"), value: <WorkItemAgent agent={context.created_by_agent} /> });
    }
    if (context && (context.origin.conversation || context.origin.conversation_hidden || context.origin.run || context.origin.flow_run)) {
      facts.push({ label: t_i18n("From"), value: <WorkItemOriginLink origin={context.origin} /> });
    }
    if (context?.person) facts.push({ label: t_i18n("For"), value: <WorkItemPerson person={context.person} /> });
    // The state facts a line 1 chip carries: a narrow
    // row hides the chips that do not fit, so the hover card names them all.
    facts.push({ label: t_i18n("Priority"), value: t_i18n(PRIORITY_LABELS[item.priority]), testId: "work-item-fact-priority" });
    if (pickup && !scheduled && pickup !== "finished") {
      const { tip } = pickupWording(t_i18n, locale, {
        state: pickup,
        blockedOn: listedBlockers(item).length || undefined,
        waiting: blockedKind(item) === "waiting",
        pullRequest: context?.pickup.pull_request,
        pullRequests: context?.pickup.pull_requests,
        answered: answeredKeepsWaiting(item),
      });
      facts.push({ label: t_i18n("Who acts on it"), value: tip, testId: "work-item-fact-pickup" });
    }
    if (item.is_stale) facts.push({ label: t_i18n("Stale"), tone: "warning", value: staleReason(item, t_i18n) });
    if (context && context.visibility.reason !== "own") {
      facts.push({ label: t_i18n("Why you see it"), value: <WorkItemVisibilityBadge visibility={context.visibility} /> });
    }
    if (item.correlation_key) {
      facts.push({ label: t_i18n("Key"), value: <span className="font-mono">{item.correlation_key}</span> });
    }
    if (pullRequests.length) {
      facts.push({ label: t_i18n("Pull requests"), value: formatList(pullRequests.map((pr) => `${pr.repo}#${pr.number}`), locale) });
    }
    if (item.due_at && !scheduled) facts.push({ label: t_i18n("Due"), value: formatInZone(item.due_at, zone, locale) });
    if (item.created_at) facts.push({ label: t_i18n("Created"), value: formatInZone(item.created_at, zone, locale) });
    if (item.claimed_at) facts.push({ label: t_i18n("Claimed"), value: formatInZone(item.claimed_at, zone, locale) });
    if (item.completed_at) facts.push({ label: t_i18n("Completed"), value: formatInZone(item.completed_at, zone, locale) });
    if (item.last_activity_at) facts.push({ label: t_i18n("Last activity"), value: formatInZone(item.last_activity_at, zone, locale) });
    return facts;
  };

  // One row: the shared Operations row, one height on every row
  // (OPS_ROW_HEIGHT), nothing wraps; the rest of the item is in its hover card.
  const renderItem = (item: WorkItem) => {
    const cfg = rowStatusIcon(item);
    const StatusIcon = cfg.icon;
    const selected = selectedIds.has(item.id);
    const manageable = canManage(item);
    const deletable = canDelete(item);
    const scheduled = isScheduledItem(item) ? item : null;
    const context = item.context;
    const pickup = context?.pickup.state;
    const pullRequests = pullRequestsOf(item.payload);
    const facts = factsOf(item);
    const blockers = item.status === "blocked" ? listedBlockers(item) : [];
    const waitsOnRefs =
      item.status === "blocked" && (pendingBlockedRefs(item).length > 0 || !!item.blocked_reason?.trim() || !!item.needs_human);
    // The meta column shows the owner, then the key, the references or the due date;
    // line 2 shows what it waits for. "+N" counts the facts left to the hover card.
    // The priority is always on line 1; the pickup, stale and visibility facts
    // are chips the row shows when they fit, so they never count in "+N".
    const chipFacts =
      (pickup && !scheduled && pickup !== "finished" ? 1 : 0) + (item.is_stale ? 1 : 0) + (context && context.visibility.reason !== "own" ? 1 : 0);
    const shownFacts =
      2 + chipFacts + (item.correlation_key || pullRequests.length || (item.due_at && !scheduled) ? 1 : 0) + (blockers.length || waitsOnRefs ? 1 : 0);
    // A row without a key shows its first pull request as one token; the others
    // join the single "+N", never a second counter beside it.
    const hiddenPullRequests = !item.correlation_key && pullRequests.length > 1 ? pullRequests.length - 1 : 0;
    const hiddenFacts = Math.max(0, facts.length - shownFacts) + hiddenPullRequests;
    return (
      <Card
        key={item.id}
        data-testid="work-item-row"
        data-work-item-id={item.id}
        onClick={() => openDetails(item)}
        className={cn(
          OPS_ROW_CARD,
          OPS_ROW_HEIGHT,
          "group cursor-pointer overflow-hidden transition-all hover:border-elevation-default",
          item.is_stale && "border-feedback-alert-primary/30",
          scheduled && "border-accent/20",
          selected && "border-elevation-default bg-elevation-highlight/[0.04]",
        )}
      >
        <CardContent className="h-full px-4 py-2.5">
          <OpsRowGrid
            actionsClassName={ACTIONS_COLUMN}
            actionsTestId="work-item-actions"
            lead={
              <>
            {deletable ? (
              <SelectionBox
                checked={selected}
                onToggle={() => toggleSelect(item.id)}
                label={selected ? t_i18n("Deselect work item") : t_i18n("Select work item")}
              />
            ) : (
              <SelectionBox
                checked={false}
                onToggle={() => undefined}
                label={t_i18n("Not selectable")}
                disabled
                reason={notSelectableReason(item)}
              />
            )}
            <StatusIcon className={cn("h-4 w-4 shrink-0", cfg.color)} data-testid="work-item-status-icon" data-tone={blockedKind(item) === "waiting" ? "waiting" : item.status} aria-hidden="true" />
              </>
            }
            main={
              <>
              {/* Line 1: the title shrinks, the state
                  and priority chips stay, and every other chip that does not fit whole
                  is dropped by OpsChipStrip (never cut, never focusable while hidden).
                  The hover card names every fact a dropped chip carries. */}
              <div className="flex min-w-0 items-center gap-2 overflow-hidden whitespace-nowrap" data-testid="work-item-line1">
                <OpsHoverCard title={item.title} facts={facts}>
                  <button
                    type="button"
                    className="min-w-[4rem] shrink truncate text-left font-medium hover:text-accent focus-visible:outline-none focus-visible:underline"
                    data-testid="work-item-title"
                    onClick={(e) => {
                      e.stopPropagation();
                      openDetails(item);
                    }}
                  >
                    {item.title}
                  </button>
                </OpsHoverCard>
                <span className="flex shrink-0 items-center" data-testid="work-item-state-chip">
                {scheduled ? (
                  // One badge per scheduled row: the calendar icon marks the
                  // kind and the word names the state, so a waiting item reads
                  // "Scheduled" once instead of a marker and a badge both saying it.
                  <ScheduleStateBadge scheduled={scheduled.scheduled} withIcon />
                ) : blockedKind(item) === "waiting" ? (
                  // Waits on open dependencies: it reopens by itself, nothing to do.
                  <BlockedStatusBadge
                    kind="waiting"
                    blockers={unfinishedBlockers(item).map((b) => b.title || t_i18n("a work item you cannot open"))}
                  />
                ) : item.status === "blocked" && pickup === "waiting_for_ci" ? (
                  // Blocked next to "Waiting for CI": the tooltip says how the two meet.
                  <Tip
                    content={t_i18n(
                      "{count, plural, one {It stays blocked until the CI of its pull request completes and the platform hands it over.} other {It stays blocked until the CI of its # pull requests completes and the platform hands it over.}}",
                      { count: waitingPullRequestCount(context?.pickup) },
                    )}
                  >
                    <Badge
                      variant="secondary"
                      tabIndex={0}
                      className={cn("content-compact", BADGE_TIP_FOCUS, STATUS_BADGE[item.status])}
                      data-testid="work-item-blocked-ci"
                    >
                      {t_i18n(STATUS_LABELS[item.status])}
                    </Badge>
                  </Tip>
                ) : item.status === "blocked" ? (
                  // Nothing it waits for is open: someone acts on it.
                  <BlockedStatusBadge
                    kind="action"
                    cancelled={cancelledBlockers(item).map((b) => b.title || t_i18n("a work item you cannot open"))}
                    reason={blockedReason(item)}
                  />
                ) : (
                  <Badge variant="secondary" className={cn("content-compact", STATUS_BADGE[item.status])}>
                    {t_i18n(STATUS_LABELS[item.status])}
                  </Badge>
                )}
                {/* The priority stays visible at every width. */}
                <Badge
                  variant="secondary"
                  className={cn("ms-1.5 content-compact", PRIORITY_BADGE[item.priority])}
                  data-testid="work-item-priority"
                >
                  {t_i18n(PRIORITY_LABELS[item.priority])}
                </Badge>
                </span>
                <OpsChipStrip testId="work-item-chips">
                {/* A decision or an item owed by the reader: the Waiting for me population (#1001). */}
                {waitsForReader(item, user?.id) && (
                  <WaitingForYouBadge testId="work-item-waiting-for-you" />
                )}
                {/* A blocked item that says nothing of what it waits for. */}
                {!scheduled && blockedWithoutReason(item) && <WorkItemNoReasonChip />}
                {item.is_stale && (
                  <Tip content={staleReason(item, t_i18n)} wrap>
                    <Badge variant="secondary" className="content-compact text-feedback-warning-tertiary bg-feedback-alert-primary/10 gap-1">
                      <AlertTriangle className="h-3 w-3" aria-hidden /> {staleBadgeText(t_i18n, item)}
                    </Badge>
                  </Tip>
                )}
                {pickup && !scheduled && pickup !== "finished" && (
                  <WorkItemPickupBadge
                    state={pickup}
                    blockedOn={listedBlockers(item).length || undefined}
                    waiting={blockedKind(item) === "waiting"}
                    pullRequest={context?.pickup.pull_request}
                    pullRequests={context?.pickup.pull_requests}
                    answered={answeredKeepsWaiting(item)}
                    compact
                  />
                )}
                {context && <WorkItemVisibilityBadge visibility={context.visibility} />}
                {pickup && waitsForSomeone(pickup) && context?.owner_agent && !scheduled && (
                  <Tip content={waitsText(pickup)}>
                    <span
                      role="img"
                      className="inline-flex items-center text-feedback-warning-primary"
                      data-testid="work-item-waits-for-someone"
                      aria-label={waitsText(pickup)}
                    >
                      <User className="h-3.5 w-3.5" />
                    </span>
                  </Tip>
                )}
                </OpsChipStrip>
              </div>
              {/* Line 2, one line on every row: what a blocked row waits for, else the summary. */}
              {blockers.length || waitsOnRefs ? (
                <div className="flex min-w-0 items-center gap-1.5 overflow-hidden whitespace-nowrap content-compact" data-testid="work-item-waits">
                  <span className="shrink-0 text-feedback-warning-primary">{t_i18n("Waits for")}</span>
                  {blockers.length ? (
                    <WorkItemBlockers blockers={blockers} nowrap />
                  ) : (
                    <WorkItemBlockedWhy refs={pendingBlockedRefs(item)} reason={item.blocked_reason} needsHuman={!!item.needs_human && !answeredKeepsWaiting(item)} nowrap />
                  )}
                </div>
              ) : (
                <div className="min-w-0 truncate content-compact text-default-secondary" data-testid="work-item-summary">
                  {scheduled ? (
                    <ScheduledSummary item={scheduled} />
                  ) : (
                    markdownSummary(item.description) || <span className="italic">{t_i18n("No description.")}</span>
                  )}
                </div>
              )}
              </>
            }
            meta={
              <div className="flex min-w-0 flex-col gap-0.5" data-testid="work-item-provenance">
                {context?.owner_agent ? (
                  <OpsToken testId="work-item-owner">
                    <WorkItemAgent agent={context.owner_agent} />
                  </OpsToken>
                ) : scheduled ? null : (
                  <OpsToken icon={User} tone="warning" testId="work-item-owner">{t_i18n("nobody")}</OpsToken>
                )}
                <span className="flex min-w-0 items-center gap-2">
                  {item.correlation_key ? (
                    <OpsToken icon={Hash} tone="muted" full={item.correlation_key} copy={item.correlation_key} copyLabel={t_i18n("Copy the key")}>
                      {item.correlation_key}
                    </OpsToken>
                  ) : pullRequests.length > 0 ? (
                    <OpsToken
                      icon={GitPullRequest}
                      tone="muted"
                      full={formatList(pullRequests.map((pr) => `${pr.repo}#${pr.number}`), locale)}
                      testId="work-item-pull-request"
                    >
                      {`${pullRequests[0].repo.split("/").pop()}#${pullRequests[0].number}`}
                    </OpsToken>
                  ) : item.due_at && !scheduled ? (
                    <OpsToken icon={Clock} tone="muted">{t_i18n("due {date}", { date: formatShortDateTimeIntl(intl, item.due_at) })}</OpsToken>
                  ) : null}
                  {hiddenFacts > 0 && (
                    <OpsHoverCard title={item.title} facts={facts}>
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          openDetails(item);
                        }}
                        className={cn("shrink-0 rounded-sm px-1 content-compact text-default-secondary hover:text-accent", BUTTON_ROLE_FOCUS_RING)}
                        aria-label={t_i18n("{count, plural, one {# more detail} other {# more details}}", { count: hiddenFacts })}
                        data-testid="work-item-more"
                      >
                        +{hiddenFacts}
                      </button>
                    </OpsHoverCard>
                  )}
                </span>
              </div>
            }
            updated={
              item.completed_at ? (
                <OpsUpdated at={item.completed_at} completed testId="work-item-updated" />
              ) : (
                <OpsUpdated at={item.last_activity_at} testId="work-item-updated" />
              )
            }
            actions={
              <>
              {scheduled?.scheduled.run_id && (
                <Tip content={t_i18n("View the run")}>
                  <Button variant="ghost" size="icon" className="h-7 w-7" asChild>
                    <Link to={`/operations/executions/${scheduled.scheduled.run_id}`} aria-label={t_i18n("View the run")}>
                      <History className="h-3.5 w-3.5" />
                    </Link>
                  </Button>
                </Tip>
              )}
              {manageable && scheduled && <ScheduleActions item={scheduled} iconOnly />}
              {manageable && offersMarkDone(item) && (
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={t_i18n("Mark done")}
                  title={t_i18n("Mark done")}
                  className="h-7 content-compact gap-1 px-1.5 text-feedback-success-primary"
                  onClick={() => setClosing({ item, mode: "done" })}
                >
                  <CheckCircle2 className="h-3.5 w-3.5" />
                  <span className="hidden 2xl:inline">{t_i18n("Mark done")}</span>
                </Button>
              )}
              <OverflowMenu items={menuItems(item)} label={t_i18n("Work item actions")} />
              </>
            }
          />
        </CardContent>
      </Card>
    );
  };

  // The landing view (open work, the default visibility) is not a filter the person set:
  // the empty title says what is empty instead of blaming filters.
  const emptyTitle = scheduledOnly
    ? "No scheduled work items match your filters."
    : waitingForMe && !debouncedSearch && !agentParam
      ? "Nothing is waiting for you"
      : debouncedSearch || isExactStatus || staleOnly || agentParam
        ? "No work items match your filters."
        : statusQuery === undefined
          ? "No open work items"
          : "No work items";
  const emptyContent = scheduledOnly ? (
    <EmptyState icon={CalendarClock} title={emptyTitle} description={EMPTY_DESCRIPTIONS.scheduled} />
  ) : waitingForMe && !debouncedSearch && !agentParam ? (
    // A cleared queue is good news, not a setup step: say what lands here, and the way back.
    <EmptyState icon={CheckCircle2} title={emptyTitle} description={EMPTY_DESCRIPTIONS.waiting}>
      <Button variant="outline" size="sm" onClick={clearAllFilters} data-testid="work-items-empty-clear-all">
        {t_i18n("Clear all filters")}
      </Button>
    </EmptyState>
  ) : (
    <VisibilityEmptyHint
      value={visibility}
      counts={countData && allSegmentsCount ? { mine: countData.count, all: allSegmentsCount.count } : undefined}
      onShowAll={() => setVisibility("all")}
    >
      <EmptyState
        icon={ListTodo}
        title={emptyTitle}
        // The fleet's description promises the fleet's items: only on the Fleet position.
        description={(fleetScope === "fleet" ? emptyDescription : undefined) ?? EMPTY_DESCRIPTIONS.items}
      />
    </VisibilityEmptyHint>
  );

  // The list view's columns: the cells, the toolbar sort of the grid view and the CSV export.
  // Every sort is the server's (`order` + `direction`).
  const columns: DataTableColumn<WorkItem>[] = [
    {
      key: "title",
      header: "Title",
      // The card's Waiting for you marker beside the title, same rule and component (#2200, clause 3 of #1001).
      cell: (item) => (
        <span className="flex min-w-0 items-center gap-1.5">
          <TruncatedText text={item.title} className="min-w-0" />
          {waitsForReader(item, user?.id) && <WaitingForYouBadge testId="work-item-list-waiting-for-you" />}
        </span>
      ),
      csv: (item) => item.title,
    },
    {
      key: "status",
      header: "Status",
      hideable: true,
      width: COLUMN_WIDTHS.status,
      cell: (item) => (
        <Badge variant="secondary" className={cn("content-compact", STATUS_BADGE[item.status])}>{t_i18n(STATUS_LABELS[item.status])}</Badge>
      ),
      sortValue: (item) => item.status,
      csv: (item) => item.status,
    },
    {
      key: "priority",
      header: "Priority",
      hideable: true,
      width: COLUMN_WIDTHS.priority,
      cell: (item) => (
        <Badge variant="secondary" className={cn("content-compact", PRIORITY_BADGE[item.priority])}>{t_i18n(PRIORITY_LABELS[item.priority])}</Badge>
      ),
      sortValue: (item) => item.priority,
      csv: (item) => item.priority,
    },
    {
      key: "owner",
      header: "Owner",
      hideable: true,
      hideBelow: "4xl",
      width: COLUMN_WIDTHS.owner,
      cell: (item) => (item.context?.owner_agent ? <WorkItemAgent agent={item.context.owner_agent} /> : null),
      sortValue: (item) => (item.context?.owner_agent?.name ?? "").toLowerCase(),
      csv: (item) => item.context?.owner_agent?.name ?? "",
    },
    {
      key: "due",
      header: "Due",
      hideable: true,
      hideBelow: "6xl",
      width: COLUMN_WIDTHS.due,
      cell: (item) => (item.due_at ? formatShortDateTimeIntl(intl, item.due_at) : ""),
      sortValue: (item) => item.due_at ?? "",
      csv: (item) => item.due_at ?? "",
    },
    {
      key: "created",
      header: "Created",
      hideable: true,
      hideBelow: "6xl",
      width: COLUMN_WIDTHS.created,
      cell: (item) => (item.created_at ? formatShortDateTimeIntl(intl, item.created_at) : ""),
      sortValue: (item) => item.created_at,
      csv: (item) => item.created_at,
    },
    {
      key: "updated",
      header: "Updated",
      hideable: true,
      width: COLUMN_WIDTHS.updated,
      align: "right",
      cell: (item) => <OpsUpdated at={item.updated_at} />,
      sortValue: (item) => item.updated_at ?? "",
      csv: (item) => item.updated_at ?? "",
    },
  ];

  const viewSwitch = (
    <SegmentedFilter
      ariaLabel={t_i18n("View filter")}
      testId="work-items-view-filter"
      options={VIEW_FILTERS}
      value={viewParam}
      onChange={setView}
    />
  );

  return (
    <div className="space-y-4">
      {confirmNode}

      {!!staleCount?.count && (
        <div className="flex items-center gap-2 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/10 px-4 py-3 content-base text-feedback-warning-tertiary">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span>
            {t_i18n(
              "{count, plural, one {# work item is stale} other {# work items are stale}} (past due or with no recent activity) and may be un-picked-up follow-ups.",
              { count: staleCount.count },
            )}
          </span>
          {!staleOnly && (
            <Button variant="ghost" size="sm" className="ml-auto h-7 content-compact text-feedback-warning-primary" onClick={() => updateParam("view", "stale")}>
              {t_i18n("Show stale")}
            </Button>
          )}
        </div>
      )}

      {/* "Waiting for me" lists every open decision, whatever asked it (#842, S5): its own list,
          the view switch first on its toolbar so the reader leaves it from there. */}
      {/* Otherwise one records table (dev-docs/reference/data-tables.md, slice 8d): search, Visibility,
          the Status pill (on the row itself), the view, scope, fleet and owner filters (Filters
          pill), sort, Columns, total, page size and URL state are the table's. The grid view keeps
          each row on the Operations row contract (OpsRowGrid) as its card; the list view draws columns. */}
      {inboxView ? <WaitingForYou toolbarLead={viewSwitch} /> : (
      <DataTable<WorkItem>
        rows={listLoading ? [] : displayItems}
        columns={columns}
        rowKey={(item) => item.id}
        urlKey={URL_KEY}
        storageKey="operations-work-items"
        defaultSort={DEFAULTS.sort ?? undefined}
        defaultPageSize={DEFAULTS.pageSize}
        server={{ total: totalMatchingCount }}
        searchPlaceholder={t_i18n("Search title or key...")}
        toolbarLead={
          <span className="flex items-center gap-2" data-testid="work-items-filter-row">
            {toolbarLead}
            {/* What a work item is lives in the "i" of the filter row, never a help paragraph above it (#10215 slice b, UX B1). */}
            <InfoTip
              label={t_i18n("About work items")}
              content={t_i18n("Tasks agents hand to each other, kept across runs. An item is picked up only when an inbox, an assignment or a flow drains its owner agent's queue; otherwise it waits. Open one to see why you see it and what will act on it.")}
            />
            <WorkItemStatusFilter value={statuses} onChange={setStatuses} counts={statusCounts} blockedHint={blockedHint} />
            {/* Quick filters on the row itself (#1001): the decisions owed to the reader and the stale
                items, each with the server's count under the other filters. One active at a time, the
                same `view` key as the Filters pill's View segment; a second click clears it. */}
            <SegmentedFilter
              ariaLabel={t_i18n("Quick filters")}
              testId="work-items-quick-filters"
              options={quickOptions}
              value={viewParam}
              onChange={toggleQuickView}
            />
            {noReasonCount > 0 && (
              <Tip content={t_i18n("Show the blocked work items: each one with no reason carries a No reason chip.")} wrap>
                {/* Icon and count on the row; the full wording is its accessible name. */}
                <button
                  type="button"
                  onClick={() => updateParam("status", "blocked")}
                  aria-label={t_i18n("{count, plural, one {# blocked with no reason} other {# blocked with no reason}}", { count: noReasonCount })}
                  className={cn(
                    "inline-flex h-8 shrink-0 items-center gap-1 rounded-sm px-2 content-compact tabular-nums",
                    BADGE_TIP_FOCUS,
                    "text-feedback-error-primary bg-feedback-error-primary/10 hover:bg-feedback-error-primary/15",
                  )}
                  data-testid="work-items-no-reason-count"
                >
                  <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />
                  {formatCompactNumber(noReasonCount)}
                </button>
              </Tip>
            )}
          </span>
        }
        scope={fleetScope !== "mine" ? { value: visibility, onChange: setVisibility, counts: segmentCounts } : undefined}
        // One filter row at 1440 and 1024 (#10215 criterion 6): view, scope, fleet and owner
        // sit in the Filters pill; the Status pill stays on the row.
        filtersInPill
        filterPills={
          <>
            {lockedFleet && isAdmin && (
              <SegmentedFilter
                ariaLabel={t_i18n("Scope")}
                testId="work-items-fleet-scope"
                options={FLEET_SCOPE_FILTERS}
                value={fleetScope === "fleet" ? "" : fleetScope}
                onChange={(key) => updateParam("scope", key || undefined)}
              />
            )}
            {viewSwitch}
            {/* The shared Fleet + Agent pair, as on Executions. */}
            <FleetAgentPickerGroup
              testId="work-items-picker-group"
              className="w-full flex-col items-stretch"
              fleetClassName="w-full"
              showFleet={showFleetPicker}
              fleet={fleet}
              onFleetChange={setFleet}
              agentPicker={(namedOwners.length > 0 || agentParam) ? (
                <SearchableSelect
                  aria-label={t_i18n("Filter by owner agent")}
                  className="h-8 w-full content-compact"
                  searchable="auto"
                  value={agentParam || ANY_AGENT}
                  onValueChange={(value) => updateParam("agent", value === ANY_AGENT ? undefined : value)}
                  pinned={[{ value: ANY_AGENT, label: t_i18n("Any owner agent") }]}
                  options={[
                    ...namedOwners.map((o) => ({ value: o.id, label: o.name, description: formatCompactNumber(o.count) })),
                    ...(agentParam && !namedOwners.some((o) => o.id === agentParam)
                      ? [{ value: agentParam, label: t_i18n("The selected agent (no item)") }]
                      : []),
                  ]}
                />
              ) : null}
            />
          </>
        }
        externalFilterCount={moreFilterCount}
        // A Status away from open work narrows the list too: Clear all shows for it (UX r1 B1 on #1004).
        toolbarFilterActive={!!statusParam}
        onClearAll={clearAllFilters}
        // The filters are the server's: the Status pill on the row, the view, scope, fleet and
        // owner in the Filters pill above (each a URL key), never a client `filters` list.
        noFilters="server-side filters: the Status pill and the Filters pill (view, scope, fleet, owner)"
        exportName="work-items"
        isLoading={listLoading}
        emptyLabel={t_i18n(emptyTitle)}
        // A view or scope of the Filters pill narrows the list: the table's one no-results
        // line with its Clear all, worded for the view (Waiting for me, Scheduled).
        noResultsLabel={t_i18n(emptyTitle === "No open work items" || emptyTitle === "No work items" ? "No work items match your filters." : emptyTitle)}
        emptyContent={emptyContent}
        selection={{
          selected: [...selectedIds],
          onChange: (keys) => {
            setAllMatchingSelected(false);
            setSelectedIds(new Set(keys));
          },
          count: allMatchingSelected ? deletableMatchingCount : undefined,
          disabledReason: (id) => {
            const item = displayItems.find((row) => row.id === id);
            return item && !canDelete(item) ? notSelectableReason(item) : undefined;
          },
          actions: (
            <>
              {!scoped && allVisibleSelected && hasMoreBeyondVisible && !allMatchingSelected && (
                <Button variant="ghost" size="sm" className={TOOLBAR_CONTROL_HEIGHT} onClick={handleSelectAllMatching}>
                  {t_i18n("select all {count}", { count: formatCompactNumber(deletableMatchingCount) })}
                </Button>
              )}
              <Button variant="ghost" size="sm" className={TOOLBAR_CONTROL_HEIGHT} onClick={handleClearSelection}>
                {t_i18n("Clear")}
              </Button>
              <Button
                variant="destructive"
                size="sm"
                className={TOOLBAR_CONTROL_HEIGHT}
                onClick={() => void handleBulkDelete()}
                disabled={bulkDelete.isPending}
                data-testid="work-items-bulk-delete"
              >
                <Trash2 className="h-3.5 w-3.5" aria-hidden /> {t_i18n("Delete")}
              </Button>
            </>
          ),
        }}
        rowTestId="work-items-table-row"
        onRowClick={openDetails}
        rowActions={(item) => <OverflowMenu items={menuItems(item)} label={t_i18n("Work item actions")} />}
        rowActionsWidth={48}
        layout="fixed"
        view={{ value: viewMode, onChange: setViewMode, modes: WORK_ITEM_VIEW_MODES }}
        // One scrollbar in grid view too: the cards scroll inside, the page does not (UX r1 on #11917, B3).
        fitGrid={!graphView}
        renderGrid={
          viewMode === "grid"
            ? (gridRows) => (
                <div className="space-y-2">
                  {/* No page-level select row above the cards (Sam, 14 Nov 2026): the page box
                      belongs in the DataTable toolbar row (item dev:data-table-grid-select-in-toolbar). */}
                  {gridRows.map(renderItem)}
                </div>
              )
            : graphView
              ? () => (
                  <WorkItemGraphView
                    graph={graph}
                    isLoading={graphLoading}
                    onOpenItem={(id) => openDetails({ id } as WorkItem)}
                    readerId={user?.id}
                  />
                )
              : undefined
        }
        // The grid view's first read: a window's worth of rows on the Operations row
        // contract (OpsRowSkeleton), so the cards land without a layout shift.
        renderGridSkeleton={(size) => (
          <OpsListSkeleton label={t_i18n("Loading...")} pageSize={size} actionsClassName={ACTIONS_COLUMN} actions={2} />
        )}
      />
      )}

      <WorkItemDetailDialog itemId={openId} onClose={closeDetails} actionsFor={actionsFor} />
      <WorkItemCloseDialog
        item={closing?.item ?? null}
        mode={closing?.mode ?? "done"}
        onOpenChange={(open) => !open && setClosing(null)}
        returnFocusTo={focusTargetAfterClose}
        onCompleted={(done) => openId === done.id && closeDetails()}
      />
    </div>
  );
}
