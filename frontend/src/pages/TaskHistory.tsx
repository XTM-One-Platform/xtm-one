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

import { useState, useMemo, useCallback, type ReactNode } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useTick } from "@/hooks/useTick";
import { Ban, PlayCircle, ShieldAlert } from "lucide-react";
import { DataTable, TOOLBAR_CONTROL_HEIGHT, type DataTableColumn } from "@/components/ui/data-table";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { TruncatedText } from "@/components/ui/truncated-text";
import { RowRemoveButton } from "@/components/ui/row-remove-button";
import { clearFilterParams, storedPageSize, type DataTableState } from "@/lib/data-table-state";
import type { SortDirection } from "@/lib/table-rows";
import { useViewMode } from "@/hooks/useViewMode";
import type { ExecutionRunSort } from "@/hooks/api/useAssignmentApi";
import { runStatusConfig } from "@/lib/constants";
import { cn, formatDurationShort } from "@/lib/utils";
import { assignmentRunTitle, ExecutionStatusBadge } from "@/pages/operations/ExecutionRow";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { OpsListSkeleton } from "@/components/ui/operations-list";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { useToast } from "@/components/ui/toast";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useToolApprovals } from "@/hooks/api/useToolApprovals";
import { ApproveByToolDialog } from "./ApproveByToolDialog";
import { usePendingApprovalsByTool } from "@/hooks/api/useAssignmentApi";
import { runReviewFromParam, useAllAssignmentRuns, useDeleteAssignmentRun, useBulkDeleteRuns, useBulkCancelRuns, useAssignmentRunCount, useRespondToHumanInput, useDismissRun, useCleanupStaleRuns, useSubmitApprovalDecisions, useBatchApprovalDecisions, useReleaseCovered } from "@/hooks/api/useAssignmentApi";
import { useAllFlowRuns } from "@/hooks/api/useFlowApi";
import { useAuth } from "@/hooks/useAuth";
import { useDebounce } from "@/hooks/useDebounce";
import { useFeatureFlagResolved } from "@/hooks/useFeatureFlag";
import { AssignmentExecutionRow, FlowExecutionRow } from "@/pages/operations/ExecutionRow";
import { REFETCH_INTERVAL_SLOW, REFETCH_INTERVAL_STANDARD } from "@/lib/constants";
import { parsePageParam } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { HitlIcon } from "@/components/ui/hitl-icon";
import { VisibilityEmptyHint } from "@/components/ui/visibility-filter";
import { useVisibilityParam, visibilityQuery } from "@/hooks/useVisibilityParam";
import { addSegmentCounts, useSegmentCounts } from "@/hooks/api/useSegmentCounts";
import { addRunStatusCounts, useRunStatusCounts } from "@/hooks/api/useRunStatusCounts";
import { ExecutionStatusFilter, statusesFromParams, type ExecutionStatus } from "@/pages/operations/ExecutionStatusFilter";
import type { AssignmentRunDetail, FlowRunSummary } from "@/types";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { FleetAgentPickerGroup } from "@/components/fleets/FleetAgentPickerGroup";
import { useCompactToolbar } from "@/hooks/useCompactToolbar";
import { useFleetParam } from "@/hooks/useFleetParam";
import { useFleetMembers } from "@/hooks/api/useFleetApi";
import { REVIEW_OPTIONS } from "@/lib/run-activity";

/** One row of the mixed Executions feed — an assignment run or a flow run. */
type ExecutionItem =
  | { kind: "assignment"; run: AssignmentRunDetail }
  | { kind: "flow"; run: FlowRunSummary };

const ANY_AGENT = "__any_agent__";

/** The table's per-user preferences (page size, columns) and its default page size. */
const STORAGE_KEY = "operations-executions";
const DEFAULT_PAGE_SIZE = 25;
/** The list view's fixed column widths (px, `layout="fixed"`): the Run title takes what is left. */
const EXECUTION_COLUMN_WIDTHS = { status: 140, type: 112, agent: 168, started: 140, duration: 96, actions: 48 } as const;
const DEFAULT_SORT = "started";
const DEFAULT_DIR: SortDirection = "desc";

/**
 * The server order of each sortable column and direction (`?sort=` of
 * /assignments/runs/all and /flows/runs/all, #9378 slice 8f).
 */
const SORTS: Record<string, Record<SortDirection, ExecutionRunSort>> = {
  started: { desc: "newest", asc: "oldest" },
  status: { asc: "status", desc: "status_desc" },
  agent: { asc: "agent", desc: "agent_desc" },
  duration: { desc: "longest", asc: "shortest" },
};

/** Seconds a run took, or null while it has not ended. */
function runDuration(run: { started_at?: string | null; completed_at?: string | null }): number | null {
  if (!run.started_at || !run.completed_at) return null;
  return (new Date(run.completed_at).getTime() - new Date(run.started_at).getTime()) / 1000;
}

/** What the Agent column shows: the agent of an assignment run, the flow of a flow run. */
function itemAgent(item: ExecutionItem): string {
  return item.kind === "flow" ? (item.run.flow_name ?? "") : item.run.agent_name;
}

/** The merged window's order, the same the server applies to each feed. */
function compareItems(a: ExecutionItem, b: ExecutionItem, key: string, dir: SortDirection): number {
  const sign = dir === "asc" ? 1 : -1;
  if (key === "status") return sign * a.run.status.localeCompare(b.run.status);
  if (key === "agent") return sign * itemAgent(a).localeCompare(itemAgent(b));
  if (key === "duration") {
    const da = runDuration(a.run);
    const db = runDuration(b.run);
    // Runs without a duration last, both ways.
    if (da === null || db === null) return da === db ? 0 : da === null ? 1 : -1;
    return sign * (da - db);
  }
  return sign * (new Date(a.run.created_at).getTime() - new Date(b.run.created_at).getTime());
}

/**
 * `fleet` locks the feed on a fleet (its page's Operations tab): the
 * runs of its current home members, resolved server-side. Elsewhere `?fleet=`
 * narrows it the same way, and the agent picker then offers its members only.
 */
export default function TaskHistory({
  hideHeader = false,
  fleet: lockedFleet,
  toolbarLead,
}: {
  hideHeader?: boolean;
  fleet?: string;
  /** A control of the host drawn first on the filter row (the fleet tab's list select), never a row of its own. */
  toolbarLead?: ReactNode;
}) {
  const { t_i18n } = useFormatter();
  useTick(REFETCH_INTERVAL_SLOW); // keep relative timestamps fresh
  const [searchParams, setSearchParams] = useSearchParams();

  // --- All filter state is derived from URL params so back-navigation restores it ---
  const search = searchParams.get("q") ?? "";
  const filterParam = searchParams.get("filter") ?? "";
  const agentFilter = searchParams.get("agent") || undefined;
  const [fleetFilter, setFleetFilter, fleetPending] = useFleetParam(lockedFleet);
  // The ROI review filter (`?review=unreviewed`), offered with a fleet only: a
  // fleet's review rate links here (#9837 A2), and its runs are assignment runs.
  const review = fleetFilter ? runReviewFromParam(searchParams.get("review")) : undefined;
  // Below 1440 px the toolbar holds on two rows.
  const compact = useCompactToolbar();
  const { data: fleetMembers } = useFleetMembers(fleetFilter);
  const page = parsePageParam(searchParams.get("page"));
  // The table's sort and page size in the URL (`sort`, `dir`, `size`); the page size
  // falls back to the reader's stored choice, then 25.
  const sortKey = SORTS[searchParams.get("sort") ?? ""] ? (searchParams.get("sort") as string) : DEFAULT_SORT;
  const sortDir: SortDirection = searchParams.get("dir") === "asc" ? "asc" : searchParams.get("dir") === "desc" ? "desc" : DEFAULT_DIR;
  const runSort: ExecutionRunSort = SORTS[sortKey][sortDir];
  const PAGE_SIZE = Number(searchParams.get("size")) || storedPageSize(STORAGE_KEY, DEFAULT_PAGE_SIZE);
  const navigate = useNavigate();
  const [viewMode, setViewMode] = useViewMode("operations-executions");

  const debouncedSearch = useDebounce(search, 300);

  // -- Selection state --
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [allMatchingSelected, setAllMatchingSelected] = useState(false);

  // Helper to update URL params (always resets page and selection)
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

  // Fetch agents for the agent filter dropdown
  const { data: agents } = useAgents();

  // Sort agents alphabetically; with a fleet chosen, only its home members
  // are a choice. The picker searches by itself.
  const sortedAgents = useMemo(() => {
    if (!agents) return [];
    const homeIds = fleetFilter
      ? new Set((fleetMembers ?? []).filter((m) => m.role === "member" && m.agent_id).map((m) => m.agent_id))
      : null;
    const sorted = [...agents]
      .filter((a) => !homeIds || homeIds.has(a.id))
      .sort((a, b) => a.name.localeCompare(b.name));
    return sorted;
  }, [agents, fleetFilter, fleetMembers]);

  // Derive effective filters from the single `filter` URL param
  const effectiveNotifOnly = filterParam === "notifications";
  const effectiveDigestsOnly = filterParam === "digests";
  // The Status pill on the toolbar (#9378 slice 8f): `?status=a,b`, multi-select;
  // an old link's single `?filter=<status>` still reads as that one status.
  // Memoised: a fresh array each render would break the React Compiler's memoisation of the whole component.
  const statusParam = searchParams.get("status");
  const selectedStatuses = useMemo(
    () => statusesFromParams(statusParam, effectiveNotifOnly || effectiveDigestsOnly ? null : filterParam),
    [statusParam, effectiveNotifOnly, effectiveDigestsOnly, filterParam],
  );
  // A notifications or digests view is its own status rule on the server: no status sent with it.
  const effectiveStatusFilter =
    selectedStatuses.length > 0 && !effectiveNotifOnly && !effectiveDigestsOnly ? selectedStatuses.join(",") : undefined;
  const setStatuses = useCallback(
    (next: ExecutionStatus[]) =>
      setSearchParams(
        (prev) => {
          const params = new URLSearchParams(prev);
          if (next.length > 0) params.set("status", next.join(","));
          else params.delete("status");
          // The legacy single-status `filter` gives way to `status`; notifications and digests stay.
          const legacy = params.get("filter");
          if (legacy && legacy !== "notifications" && legacy !== "digests") params.delete("filter");
          params.delete("page");
          return params;
        },
        { replace: true },
      ),
    [setSearchParams],
  );

  // --- Run type: assignments, flows, or both interleaved ---
  // The flow feed follows the `FLOW_BUILDER` feature flag: every card in it
  // deep-links into `/flows/library/<id>`, which redirects home while the
  // builder is hidden. A stale `?type=flows` URL falls back to "all" rather
  // than rendering a feed that cannot exist — with both halves disabled the
  // page would otherwise show an empty state and a pager over nothing.
  //
  // `useFeatureFlagResolved`, not `useFeatureFlag`: the flag is fail-closed and
  // this decides what to fetch on the first render. Reading it too early on a
  // flag-ON instance coerced `?type=flows` to "all", fired and threw away an
  // assignment-runs request, highlighted the wrong pill, then flipped the whole
  // view on a second skeleton once `/auth/config` landed — and moved
  // `filterKey` with it, clearing any selection made in between. An errored
  // config falls through fail-closed rather than blocking: the assignment feed,
  // which is most of this page, does not depend on the flag.
  const { enabled: flowBuilderEnabled, isResolved: flagResolved, isError: flagError } =
    useFeatureFlagResolved("FLOW_BUILDER");
  /* "We know what the flag says" — an errored config counts, fail-closed, so a
     failed `/auth/config` degrades to the assignment feed instead of blocking
     the page on a skeleton forever. Both feeds stay *disabled* until then, so
     nothing is fetched on a guess: gating only the skeleton below would still
     fire and discard a request, because hooks run before the early return. */
  // Nor while a `?fleet=` waits on the reader's fleet access: the feeds
  // would be asked with a fleet dropped a moment later.
  const flagKnown = (flagResolved || flagError) && !fleetPending;
  const rawTypeParam = searchParams.get("type") ?? "all";
  // A fleet narrows to its members' assignment runs: a flow run has no
  // member to scope it by, so the run type is fixed while a fleet is chosen.
  const typeParam = fleetFilter
    ? "assignments"
    : rawTypeParam === "flows" && !flowBuilderEnabled ? "all" : rawTypeParam;
  // Notifications/digests/agent/fleet are assignment concepts — while one is
  // active the flow feed cannot match it, so it drops out of the "All" view.
  const assignmentSpecificFilter = !!(agentFilter || fleetFilter || effectiveNotifOnly || effectiveDigestsOnly);
  const assignmentsEnabled = flagKnown && typeParam !== "flows";
  const flowsEnabled = flagKnown
    && flowBuilderEnabled
    && typeParam !== "assignments"
    && !(typeParam === "all" && assignmentSpecificFilter);
  // Both feeds shown: page over the MERGED chronology. Each source is asked
  // for the whole window up to the current page (from offset 0), the merge
  // is sorted by created_at and the page sliced out — correct interleaved
  // pagination without a cross-table endpoint. The window is deliberately
  // small: each page re-fetches the whole window from BOTH feeds (offset 0)
  // on a fast poll while anything runs, so the cap bounds that cost to two
  // 100-row payloads at most. Deeper history stays reachable through the
  // type filter, and a hint below says so when the merged view is clamped.
  const MERGED_WINDOW_MAX = 100;
  const mergedMode = assignmentsEnabled && flowsEnabled;
  const fetchLimit = mergedMode
    ? Math.min((page + 1) * PAGE_SIZE, MERGED_WINDOW_MAX)
    : PAGE_SIZE;
  const fetchOffset = mergedMode ? 0 : page * PAGE_SIZE;

  // Each segment's count under the other filters, the numbers on the filter:
  // the feeds shown, added segment by segment.
  const assignmentSegmentCounts = useSegmentCounts(
    ["all-assignment-runs"],
    "/assignments/runs",
    {
      status: effectiveStatusFilter,
      search: debouncedSearch || undefined,
      notifications: effectiveNotifOnly || undefined,
      digests: effectiveDigestsOnly || undefined,
      agent_id: agentFilter,
      fleet: fleetFilter,
      review,
    },
    { enabled: assignmentsEnabled, refetchInterval: REFETCH_INTERVAL_STANDARD },
  );
  const flowSegmentCounts = useSegmentCounts(
    ["flow-runs-global"],
    "/flows/runs",
    { status: effectiveStatusFilter, search: debouncedSearch || undefined },
    { enabled: flowsEnabled, refetchInterval: REFETCH_INTERVAL_STANDARD },
  );
  const segmentCounts = addSegmentCounts(
    assignmentsEnabled ? assignmentSegmentCounts : false,
    flowsEnabled ? flowSegmentCounts : false,
  );
  // Mine | Company | Shared | All (`?vis=`), the one segment of every
  // Operations tab; with none named, the first segment holding a run once the
  // counts of the feeds shown are in. All when locked on a fleet.
  const [visibility, setVisibility, landing] = useVisibilityParam(undefined, lockedFleet ? "all" : undefined, {
    counts: segmentCounts,
    enabled: !lockedFleet,
  });
  const visFilter = visibilityQuery(visibility);

  // Each status's count under the other filters, the numbers on the Status pill.
  const assignmentStatusCounts = useRunStatusCounts(
    ["all-assignment-runs"],
    "/assignments/runs",
    {
      search: debouncedSearch || undefined,
      vis: visFilter,
      agent_id: agentFilter,
      fleet: fleetFilter,
      review,
    },
    { enabled: assignmentsEnabled && !landing, refetchInterval: REFETCH_INTERVAL_STANDARD },
  );
  const flowStatusCounts = useRunStatusCounts(
    ["flow-runs-global"],
    "/flows/runs",
    { search: debouncedSearch || undefined, vis: visFilter },
    { enabled: flowsEnabled && !landing, refetchInterval: REFETCH_INTERVAL_STANDARD },
  );
  const statusCounts = addRunStatusCounts(
    assignmentsEnabled && assignmentStatusCounts,
    flowsEnabled && flowStatusCounts,
  );

  const { data: runs, isLoading } = useAllAssignmentRuns(
    fetchLimit,
    fetchOffset,
    effectiveStatusFilter,
    debouncedSearch || undefined,
    effectiveNotifOnly || undefined,
    visFilter,
    agentFilter,
    effectiveDigestsOnly || undefined,
    undefined,
    assignmentsEnabled && !landing,
    undefined,
    undefined,
    undefined,
    fleetFilter,
    review,
    runSort === "newest" ? undefined : runSort,
  );

  const { data: flowRunsData, isLoading: flowRunsLoading } = useAllFlowRuns(
    fetchLimit,
    fetchOffset,
    effectiveStatusFilter,
    debouncedSearch || undefined,
    visFilter,
    flowsEnabled && !landing,
    runSort === "newest" ? undefined : runSort,
  );

  // Total count of runs matching current filters (for "select all matching" banner)
  const { data: countData } = useAssignmentRunCount(
    effectiveStatusFilter,
    debouncedSearch || undefined,
    effectiveNotifOnly || undefined,
    visFilter,
    agentFilter,
    undefined,
    effectiveDigestsOnly || undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    !landing,
    fleetFilter,
    review,
  );
  const totalMatchingCount = countData?.count ?? 0;
  const flowTotal = flowsEnabled ? (flowRunsData?.total ?? 0) : 0;
  const rawCombinedTotal = (assignmentsEnabled ? totalMatchingCount : 0) + flowTotal;
  // Drives pagination: every run the current type selection can reach — in
  // merged mode, capped at what the window can actually fetch so the pager
  // never offers pages that would render empty or under-filled.
  const mergedClamped = mergedMode && rawCombinedTotal > MERGED_WINDOW_MAX;
  const combinedTotal = mergedClamped ? MERGED_WINDOW_MAX : rawCombinedTotal;

  // The same filters over every segment, asked only on an empty Mine: the
  // empty-Mine hint says whether other segments hold runs.
  const askAllSegments = visibility === "mine" && flagKnown && rawCombinedTotal === 0;
  const { data: allSegmentsRunCount } = useAssignmentRunCount(
    effectiveStatusFilter,
    debouncedSearch || undefined,
    effectiveNotifOnly || undefined,
    undefined,
    agentFilter,
    undefined,
    effectiveDigestsOnly || undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    askAllSegments && assignmentsEnabled,
    fleetFilter,
  );
  const { data: allSegmentsFlowRuns } = useAllFlowRuns(
    1,
    0,
    effectiveStatusFilter,
    debouncedSearch || undefined,
    undefined,
    askAllSegments && flowsEnabled,
  );
  const allSegmentsTotal =
    (assignmentsEnabled ? (allSegmentsRunCount?.count ?? 0) : 0)
    + (flowsEnabled ? (allSegmentsFlowRuns?.total ?? 0) : 0);

  const { isAdmin } = useAuth();
  const deleteRun = useDeleteAssignmentRun();
  const bulkDelete = useBulkDeleteRuns();
  const submitApprovals = useSubmitApprovalDecisions();
  const batchApprovals = useBatchApprovalDecisions();
  const releaseCoveredRuns = useReleaseCovered();
  const cleanupStale = useCleanupStaleRuns();
  const bulkCancel = useBulkCancelRuns();
  const respondHitl = useRespondToHumanInput();
  const dismissRun = useDismissRun();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();

  // -- Clear selection when filters change (adjust-state-during-render pattern) --
  const filterKey = `${effectiveStatusFilter}|${effectiveNotifOnly}|${effectiveDigestsOnly}|${visibility}|${agentFilter}|${debouncedSearch}|${typeParam}`;
  const [prevFilterKey, setPrevFilterKey] = useState(filterKey);
  if (prevFilterKey !== filterKey) {
    setPrevFilterKey(filterKey);
    setSelectedIds(new Set());
    setAllMatchingSelected(false);
  }

  // -- Selection helpers --
  const toggleSelect = useCallback((id: string) => {
    setAllMatchingSelected(false);
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  // The page actually rendered: one feed straight through, or both merged
  // by created_at with the current page sliced out of the fetched window.
  const displayItems = useMemo<ExecutionItem[]>(() => {
    const asg: ExecutionItem[] = (assignmentsEnabled && runs ? runs : []).map(
      (r) => ({ kind: "assignment", run: r }),
    );
    const flw: ExecutionItem[] = (
      flowsEnabled && flowRunsData ? flowRunsData.runs : []
    ).map((r) => ({ kind: "flow", run: r }));
    if (!mergedMode) return assignmentsEnabled ? asg : flw;
    return [...asg, ...flw]
      .sort((a, b) => compareItems(a, b, sortKey, sortDir))
      .slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
  }, [runs, flowRunsData, assignmentsEnabled, flowsEnabled, mergedMode, page, PAGE_SIZE, sortKey, sortDir]);

  // Selection (and bulk delete) applies to assignment runs only — flow runs
  // are inspected in the builder and have no per-run delete.
  const visibleIds = useMemo(
    () =>
      displayItems
        .filter((item) => item.kind === "assignment")
        .map((item) => item.run.id),
    [displayItems],
  );

  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every((id) => selectedIds.has(id));

  /* The selected rows that are actually awaiting a decision (FR-031).
     Reuses the existing selection rather than adding a second one — a page
     with two independent sets of checkboxes is a page nobody trusts.

     No read gate here, unlike the notification dropdown: this page renders
     every pending proposal inline and expanded, so FR-034's "each was shown"
     is satisfied by construction rather than by tracking clicks. */
  const approvableSelected = useMemo(
    () =>
      displayItems
        .filter((item) => item.kind === "assignment")
        .map((item) => item.run)
        .filter(
          (run) =>
            selectedIds.has(run.id) &&
            run.status === "awaiting_human" &&
            (run.pending_approvals?.length ?? 0) > 0 &&
            !run.approval_answered,
        ),
    [displayItems, selectedIds],
  );

  /* Every visible row that can actually be answered, whether or not it is
     currently selected — the shortcut's target. */
  const approvableVisible = useMemo(
    () =>
      displayItems
        .filter((item) => item.kind === "assignment")
        .map((item) => item.run)
        .filter(
          (run) =>
            run.status === "awaiting_human" &&
            (run.pending_approvals?.length ?? 0) > 0 &&
            // Answered already, waiting on the worker: `resume_assignment_run`
            // emits an outbox event without changing the status, so the run
            // stays `awaiting_human` until the queue drains.
            !run.approval_answered,
        ),
    [displayItems],
  );

  /* Approve/reject act on the loaded rows only — there is no filter-based
     batch endpoint and inventing one would answer runs nobody looked at. So
     while "all N matching" is armed the two scopes disagree, and the honest
     move is to withdraw the approval actions rather than quietly narrow them. */
  const approvalsScopeAmbiguous = allMatchingSelected;

  /* Runs paused on tools this reviewer has ALREADY said "always" to.
     The exception is read by the gate, which has already run for anything
     sitting in awaiting_human — so granting it never releases what is already
     stopped, and a reviewer who answers one of three identical pauses still
     faces the other two.

     Every pending call on the run must be covered. A run holding one covered
     and one uncovered call still needs answering by hand: releasing it would
     approve something the reviewer never consented to. */
  const [byToolOpen, setByToolOpen] = useState(false);
  /* Fetched as soon as anything is paused here, not only once the dialog
     opens: the cross-page total is the whole point of the control, and a
     button that had to be opened before it could say how much it would answer
     would bury its own reason for existing. */
  const { data: byToolSummary, isLoading: byToolLoading } =
    usePendingApprovalsByTool(true);
  const { data: approvalsData } = useToolApprovals();
  const coveredTools = useMemo(
    () => new Set((approvalsData?.preferences ?? []).map((p) => p.tool_key)),
    [approvalsData],
  );
  const alreadyCovered = useMemo(
    () =>
      coveredTools.size === 0
        ? []
        : approvableVisible.filter(
            (run) =>
              /* Personal runs only. The server resolves coverage for the
                 RUN's effective user, and these preferences are the caller's:
                 an admin answering a colleague's company-managed run would be
                 offered "Release 3" and get three refusals. A personal run is
                 always the caller's own — ``require_run_access`` 404s any
                 other — while a company-managed one may not be, and the API
                 does not report its owner. Under-offering is the honest side
                 of that: those runs stay answerable one at a time. */
              !run.is_company_managed &&
              (run.pending_approvals ?? []).every((p) =>
                coveredTools.has(p.tool_name),
              ),
          ),
    [approvableVisible, coveredTools],
  );

  const releaseCovered = useCallback(() => {
    /* Names the runs and lets the server decide the calls (FR-039b). Sending
       per-call decisions to the batch endpoint put the "every call must be
       covered" arithmetic here, where nothing could enforce it — the batch is
       told what to answer, so a partially covered run looked like an ordinary
       approval. The coverage below still decides what to *offer*; it no longer
       decides what is allowed. */
    releaseCoveredRuns.mutate(
      alreadyCovered.map((run) => run.id),
      {
        onSuccess: (res) => {
          const refused = res.results.filter((r) => r.status === "refused");
          toast(
            refused.length === 0
              ? { title: t_i18n("Decisions recorded: assignments resuming"), variant: "success" }
              : {
                  title: t_i18n("{n} of {total} could not be answered", {
                    n: refused.length,
                    total: res.results.length,
                  }),
                  description: refused[0]?.detail ?? undefined,
                  variant: "info",
                },
          );
        },
        onError: (err) =>
          toast({ title: t_i18n("Could not submit decisions"), description: err.message, variant: "error" }),
      },
    );
  }, [alreadyCovered, releaseCoveredRuns, toast, t_i18n]);

  const handleSelectApprovable = useCallback(() => {
    // Replaces rather than adds: the reviewer asked for "the ones I can
    // answer", and quietly keeping unanswerable rows selected would make the
    // count on the Approve button disagree with the checkboxes.
    setSelectedIds(new Set(approvableVisible.map((r) => r.id)));
    setAllMatchingSelected(false);
  }, [approvableVisible]);

  const handleBulkApprove = useCallback(
    (verdict: "approve" | "approve_always" | "reject") => {
      /* FR-032 as revised: the durable choice is reachable from a group, and
         FR-015c's disclosure is kept by ENUMERATING what it would un-gate.
         "Are you sure?" would not be a disclosure; a list of tool names is. */
      if (verdict === "approve_always") {
        const tools = Array.from(
          new Set(
            approvableSelected.flatMap((run) =>
              (run.pending_approvals ?? []).map((p) => p.tool_name),
            ),
          ),
        ).sort();
        const ok = window.confirm(
          t_i18n(
            "Stop asking for {n, plural, one {this tool} other {these # tools}} on every run carrying your identity, including scheduled ones nobody is watching?",
            { n: tools.length },
          ) +
            "\n\n" +
            tools.join("\n") +
            "\n\n" +
            t_i18n("You can revoke this at any time in your profile."),
        );
        if (!ok) return;
      }
      /* One reason, applied to every member. A rejection's reason is the
         agent's only signal about what was wrong: with none it is told
         "No reason given.", and the instruction it receives then leaves it
         nothing to do but ask how to proceed — so a reasonless bulk reject
         buys one click and costs N follow-up questions. Asked once here
         because the whole point of the group is not answering N times. */
      let sharedReason: string | undefined;
      if (verdict === "reject") {
        const entered = window.prompt(
          t_i18n(
            "Why are these being rejected? The agents receive this and will otherwise just ask you what to do instead.",
          ),
          "",
        );
        // Cancel aborts; an empty string is a deliberate "no reason", which
        // stays permitted — a reviewer may have nothing to say.
        if (entered === null) return;
        sharedReason = entered.trim() || undefined;
      }
      const decisions = approvableSelected.flatMap((run) =>
        (run.pending_approvals ?? []).map((p) => ({
          run_id: run.id,
          tool_call_id: p.tool_call_id,
          decision: verdict,
          ...(sharedReason ? { rejection_reason: sharedReason } : {}),
        })),
      );
      batchApprovals.mutate(
        { decisions },
        {
          onSuccess: (res) => {
            /* Partial success is a success (FR-033): report per member rather
               than letting a 200 imply everything landed. */
            const refused = res.results.filter((r) => r.status === "refused");
            setSelectedIds(new Set());
            // Both halves, like every other path that empties the selection.
            // Leaving this armed keeps the bar reading "All N selected" and
            // keeps Delete pointing at the filter-based payload — one click
            // from deleting every matching run with nothing ticked.
            setAllMatchingSelected(false);
            toast(
              refused.length === 0
                ? { title: t_i18n("Decisions recorded: assignments resuming"), variant: "success" }
                : {
                    title: t_i18n("{n} of {total} could not be answered", {
                      n: refused.length,
                      total: res.results.length,
                    }),
                    description: refused[0]?.detail ?? undefined,
                    variant: "info",
                  },
            );
          },
          onError: (err) =>
            toast({ title: t_i18n("Could not submit decisions"), description: err.message, variant: "error" }),
        },
      );
    },
    [approvableSelected, batchApprovals, toast, t_i18n],
  );

  const handleSelectAllMatching = useCallback(() => {
    setAllMatchingSelected(true);
    // Also select all visible so the UI shows them checked
    setSelectedIds(new Set(visibleIds));
  }, [visibleIds]);

  // Whether there are more runs beyond the visible page (hint for "select all matching").
  const hasMoreBeyondVisible = countData
    ? totalMatchingCount > visibleIds.length
    : (runs?.length ?? 0) >= PAGE_SIZE;

  const handleDelete = async (id: string) => {
    if (await confirm({ title: "Delete this run?", description: "This action cannot be undone.", confirmLabel: "Delete" })) {
      deleteRun.mutate(id, { onSuccess: () => toast({ title: t_i18n("Run deleted"), variant: "success" }) });
    }
  };

  const handleBulkDelete = async () => {
    const title = allMatchingSelected
      ? t_i18n("Delete {count, plural, one {# run} other {# runs}} matching current filters?", { count: totalMatchingCount })
      : t_i18n("Delete {count, plural, one {# selected run} other {# selected runs}}?", { count: selectedIds.size });

    if (
      await confirm({
        title,
        description: "This action cannot be undone.",
        confirmLabel: "Delete",
      })
    ) {
      const payload = allMatchingSelected
        ? {
            all_matching_filters: true,
            status: effectiveStatusFilter || undefined,
            search: debouncedSearch || undefined,
            notifications: effectiveNotifOnly || undefined,
            digests: effectiveDigestsOnly || undefined,
            vis: visFilter,
            agent_id: agentFilter,
          }
        : { run_ids: Array.from(selectedIds) };

      bulkDelete.mutate(payload, {
        onSuccess: (data) => {
          toast({ title: t_i18n("{count, plural, one {# run deleted} other {# runs deleted}}", { count: data.deleted }), variant: "success" });
          setSelectedIds(new Set());
          setAllMatchingSelected(false);
        },
      });
    }
  };

  // The agent picker's choices; a selected agent outside them stays named.
  const agentOptions = sortedAgents.map((a) => ({ value: a.id, label: a.name }));
  if (agentFilter && !agentOptions.some((o) => o.value === agentFilter)) {
    const named = agents?.find((a) => a.id === agentFilter);
    agentOptions.push({ value: agentFilter, label: named?.name ?? t_i18n("Unknown agent") });
  }

  const filters = [
    { key: "all", label: t_i18n("All") },
    // Notification/digest pills are assignment concepts — hidden while the
    // feed shows flows only.
    ...(typeParam !== "flows"
      ? [
          { key: "notifications", label: t_i18n("Notifications") },
          { key: "digests", label: t_i18n("Digests") },
        ]
      : []),
  ];
  // Notifications and digests stay a view of the Filters pill; the statuses are the toolbar's Status pill.
  const viewParam = effectiveNotifOnly || effectiveDigestsOnly ? filterParam : "all";

  const typeFilters = [
    { key: "all", label: t_i18n("All") },
    { key: "assignments", label: t_i18n("Assignments") },
    ...(flowBuilderEnabled ? [{ key: "flows", label: t_i18n("Flows") }] : []),
  ];

  // Until the feature flags are known the toolbar itself is unknown (the run
  // type filter depends on them): the whole tab waits. Once known, the toolbar
  // stays mounted and the skeleton fills the list slot only. The wait draws the run
  // rows, never a generic card grid (#11345).
  if (!flagKnown) return <OpsListSkeleton label={t_i18n("Loading...")} pageSize={PAGE_SIZE} actionsClassName="w-16" actions={1} />;
  const listLoading =
    landing
    || (assignmentsEnabled && isLoading)
    || (flowsEnabled && flowRunsLoading);

  // Admin-only maintenance action. Rendered in the title row when standalone, or
  // folded into the filter row when embedded (hideHeader) so the filter row is
  // the first child and lines up flush under the Operations tabs.
  // Not with a fleet: the cleanup is platform-wide, never scoped to a fleet.
  // An icon-only 32 px button at every width, its name an aria-label and a Tip,
  // like the bulk cancel action beside it: the text
  // label took 236 to 256 px in fr and es and pushed the 1440 toolbar to a
  // third row.
  const cleanupLabel = cleanupStale.isPending ? t_i18n("Cleaning...") : t_i18n("Cleanup Stale Runs");
  const cleanupButton = isAdmin && !fleetFilter ? (
    <Tip content={cleanupLabel} wrap="auto">
      <Button
        variant="outline"
        // The square icon size, held at 32 px beside the 32 px pickers:
        // `sm` drew a 24 px high box.
        size="icon"
        className="h-8 w-8 shrink-0"
        disabled={cleanupStale.isPending}
        aria-label={cleanupLabel}
        data-testid="cleanup-stale-runs"
        onClick={async () => {
          if (await confirm({
            title: t_i18n("Cleanup stale runs?"),
            description: t_i18n("This will mark all assignment runs stuck in 'running', 'pending', or 'delayed' status that have exceeded their timeout as failed. This is useful after a platform restart."),
            confirmLabel: t_i18n("Cleanup"),
          })) {
            cleanupStale.mutate(undefined, {
              onSuccess: (data) => {
                toast({
                  title: data.cleaned > 0
                    ? t_i18n("Cleaned up {count, plural, one {# stale run} other {# stale runs}}", { count: data.cleaned })
                    : t_i18n("No stale runs found"),
                  variant: data.cleaned > 0 ? "success" : "default",
                });
              },
            });
          }
        }}
      >
        <ShieldAlert className="h-3.5 w-3.5" aria-hidden />
      </Button>
    </Tip>
  ) : null;

  // Admin-only bulk cancel of the runs that have not started: every
  // pending, delayed or queued run of the agent the list is filtered on. The
  // API refuses a filter on status alone (it would reach every waiting run of
  // the platform), so without an agent the action is disabled and says why.
  // A dry run counts first; the confirmation names that count; nothing is
  // changed until it is confirmed. Not in a fleet: the cancel has no fleet scope.
  const handleBulkCancel = async () => {
    if (!agentFilter) return;
    let count: number;
    try {
      ({ count } = await bulkCancel.mutateAsync({ agent_id: agentFilter, dry_run: true }));
    } catch (err) {
      toast({ title: t_i18n("The runs were not cancelled"), description: (err as Error).message, variant: "error" });
      return;
    }
    if (count === 0) {
      toast({ title: t_i18n("No run of this agent is waiting to start") });
      return;
    }
    const ok = await confirm({
      title: t_i18n("Cancel {count, plural, one {# run that has not started} other {# runs that have not started}}?", { count }),
      description: t_i18n("Every pending, delayed or queued run of this agent ends cancelled and stays in the history. Running runs are not touched."),
      confirmLabel: t_i18n("Cancel runs"),
      cancelLabel: t_i18n("Keep them"),
      variant: "warning",
    });
    if (!ok) return;
    bulkCancel.mutate(
      { agent_id: agentFilter },
      {
        onSuccess: (data) => {
          toast({ title: t_i18n("{count, plural, one {# run cancelled} other {# runs cancelled}}", { count: data.count }), variant: "success" });
          // Show what was just cancelled.
          setStatuses(["cancelled"]);
        },
        onError: (err) => toast({ title: t_i18n("The runs were not cancelled"), description: err.message, variant: "error" }),
      },
    );
  };

  // Sits right after the agent picker it is scoped to, as an icon-only 32 px
  // button, so adding it changes the toolbar
  // height at neither 1024 nor 1440. Its name is an aria-label and a Tip. The
  // reason it is disabled is a Tip on a focusable wrapper carrying the product
  // focus ring, plus an accessible description (UX B2): a disabled Button takes
  // neither pointer nor focus, so a native title would never show.
  const bulkCancelLabel = t_i18n("Cancel runs not started");
  const bulkCancelHint = agentFilter ? undefined : t_i18n("Choose an agent to cancel its runs that have not started");
  const bulkCancelButton = isAdmin && !fleetFilter && typeParam !== "flows" ? (
    <Tip
      content={bulkCancelHint ?? bulkCancelLabel}
      wrap="auto"
      wrapClassName="inline-flex shrink-0 rounded-md"
    >
      <Button
        variant="outline"
        size="icon"
        className="h-8 w-8 shrink-0"
        disabled={!agentFilter || bulkCancel.isPending}
        aria-label={bulkCancelLabel}
        aria-describedby={bulkCancelHint ? "bulk-cancel-runs-hint" : undefined}
        data-testid="bulk-cancel-runs"
        onClick={handleBulkCancel}
      >
        <Ban className="h-3.5 w-3.5" />
      </Button>
    </Tip>
  ) : null;
  // The filters behind the Filters pill that narrow the list: its badge. The
  // run type is fixed with a fleet, so it counts only when chosen.
  const moreFilterCount =
    (!fleetFilter && typeParam !== "all" ? 1 : 0) +
    (viewParam !== "all" ? 1 : 0) +
    (review ? 1 : 0) +
    (fleetFilter && !lockedFleet ? 1 : 0) +
    (agentFilter && typeParam !== "flows" ? 1 : 0);

  // Outside the button, so the hint describes it without joining its name.
  const bulkCancelHintNode = bulkCancelButton && bulkCancelHint ? (
    <span id="bulk-cancel-runs-hint" className="sr-only">{bulkCancelHint}</span>
  ) : null;

  // The table's state lives in the URL keys this page always had (`q`, `page`)
  // plus `sort`, `dir` and `size`: old links keep working (#9378 slice 8f).
  const tableState: DataTableState = {
    search,
    sort: { key: sortKey, direction: sortDir },
    page,
    pageSize: PAGE_SIZE,
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

  const itemKey = (item: ExecutionItem) => item.run.id;
  const openItem = (item: ExecutionItem) =>
    navigate(item.kind === "flow" ? `/flows/library/${item.run.flow_id}?run=${item.run.id}` : `/operations/executions/${item.run.id}`);

  // layout="fixed" with the widths of EXECUTION_COLUMN_WIDTHS: the Run title takes what is
  // left and the long texts truncate with the DS tooltip, so the list never scrolls
  // sideways at 1080 (UX r2 B4 on #11894, as WorkItemsTab).
  const columns: DataTableColumn<ExecutionItem>[] = [
    {
      key: "run",
      header: "Run",
      cell: (item) => (
        <TruncatedText
          className="font-medium"
          text={item.kind === "flow" ? item.run.flow_name || t_i18n("Flow run") : assignmentRunTitle(item.run, t_i18n)}
        />
      ),
      csv: (item) => (item.kind === "flow" ? item.run.flow_name ?? "" : assignmentRunTitle(item.run, t_i18n)),
    },
    {
      key: "status",
      header: "Status",
      width: EXECUTION_COLUMN_WIDTHS.status,
      cell: (item) => <ExecutionStatusBadge run={item.run} kind={item.kind} />,
      sortValue: (item) => item.run.status,
      csv: (item) => (item.kind === "flow" ? item.run.status : t_i18n(runStatusConfig(item.run).label)),
      hideable: true,
    },
    {
      key: "type",
      header: "Type",
      width: EXECUTION_COLUMN_WIDTHS.type,
      // Below a 1152 px box the Run title keeps its room; the Columns pill brings it back.
      hideBelow: "6xl",
      cell: (item) => t_i18n(item.kind === "flow" ? "Flow" : "Assignment"),
      csv: (item) => item.kind,
      hideable: true,
      defaultHidden: !mergedMode,
    },
    {
      key: "agent",
      header: "Agent",
      width: EXECUTION_COLUMN_WIDTHS.agent,
      cell: (item) => <TruncatedText text={itemAgent(item) || "-"} />,
      sortValue: (item) => itemAgent(item),
      csv: (item) => itemAgent(item),
      hideable: true,
    },
    {
      key: "started",
      header: "Started",
      width: EXECUTION_COLUMN_WIDTHS.started,
      cell: (item) => <DateTimeCell value={item.run.started_at ?? item.run.created_at} />,
      sortValue: (item) => item.run.created_at,
      csv: (item) => item.run.started_at ?? item.run.created_at,
      firstSort: "desc",
      hideable: true,
    },
    {
      key: "duration",
      header: "Duration",
      width: EXECUTION_COLUMN_WIDTHS.duration,
      align: "right",
      cell: (item) => {
        const seconds = runDuration(item.run);
        return seconds === null ? "-" : formatDurationShort(seconds);
      },
      sortValue: (item) => runDuration(item.run) ?? -1,
      csv: (item) => runDuration(item.run) ?? "",
      firstSort: "desc",
      hideable: true,
    },
  ];

  // One run card, the grid view (the Operations row contract, OpsRowGrid).
  const renderItem = (item: ExecutionItem) =>
    item.kind === "flow" ? (
      <FlowExecutionRow key={item.run.id} run={item.run} typeBadge={mergedMode} />
    ) : (
      <AssignmentExecutionRow
        key={item.run.id}
        run={item.run}
        typeBadge={mergedMode}
        onDelete={handleDelete}
        selected={selectedIds.has(item.run.id)}
        onSelect={toggleSelect}
        onRespond={(rid, resp) => respondHitl.mutate({ runId: rid, response: resp }, { onSuccess: () => toast({ title: t_i18n("Response sent: assignment resuming"), variant: "success" }) })}
        onDismiss={(rid) => dismissRun.mutate(rid, { onSuccess: () => toast({ title: t_i18n("Run dismissed"), variant: "success" }) })}
        /* Without this the row tells whoever can approve that it is
           "waiting for approval from someone with access", on the one
           page built for acting on notifications. */
        onApprove={(rid, decisions) => submitApprovals.mutate({ runId: rid, decisions }, {
          onSuccess: () => toast({ title: t_i18n("Decision recorded: assignment resuming"), variant: "success" }),
          onError: (err) => toast({ title: t_i18n("Could not submit decisions"), description: err.message, variant: "error" }),
        })}
        approveFailed={submitApprovals.isError}
      />
    );

  // Decisions, not rows: one run can hold several gated calls, and the
  // button must say what it will actually answer (FR-031b).
  const approvableCount = approvableSelected.reduce((n, run) => n + (run.pending_approvals?.length ?? 0), 0);
  const approvableVisibleCount = approvalsScopeAmbiguous ? 0 : approvableVisible.length;
  // Not in a fleet: bulk delete by filters has no fleet scope.
  const canSelectAllMatching = !fleetFilter && allVisibleSelected && hasMoreBeyondVisible && !allMatchingSelected;
  const selectionActions = (
    <span className="flex flex-wrap items-center gap-2" data-testid="executions-bulk-actions">
      {canSelectAllMatching && (
        <Button variant="link" size="sm" className={cn(TOOLBAR_CONTROL_HEIGHT, "px-1 content-compact")} onClick={handleSelectAllMatching}>
          {t_i18n("select all {count}", { count: totalMatchingCount })}
        </Button>
      )}
      {!allMatchingSelected && approvableVisibleCount > 0 && approvableCount === 0 && (
        <Button variant="outline" size="sm" className={cn(TOOLBAR_CONTROL_HEIGHT, "px-2 content-compact")} onClick={handleSelectApprovable}>
          {t_i18n("Select the {n} awaiting approval", { n: approvableVisibleCount })}
        </Button>
      )}
      {!approvalsScopeAmbiguous && approvableCount > 0 && (
        <>
          <Button variant="outline" size="sm" className={cn(TOOLBAR_CONTROL_HEIGHT, "px-2 content-compact")} disabled={batchApprovals.isPending} onClick={() => handleBulkApprove("reject")}>
            {t_i18n("Reject {n} selected", { n: approvableCount })}
          </Button>
          <Button size="sm" className={cn(TOOLBAR_CONTROL_HEIGHT, "px-2 content-compact")} disabled={batchApprovals.isPending} onClick={() => handleBulkApprove("approve")}>
            {t_i18n("Approve {n} selected", { n: approvableCount })}
          </Button>
          <Button variant="outline" size="sm" className={cn(TOOLBAR_CONTROL_HEIGHT, "px-2 content-compact")} disabled={batchApprovals.isPending} onClick={() => handleBulkApprove("approve_always")}>
            {t_i18n("Approve always {n} selected", { n: approvableCount })}
          </Button>
        </>
      )}
      <Button variant="destructive" size="sm" className={cn(TOOLBAR_CONTROL_HEIGHT, "px-2 content-compact")} disabled={bulkDelete.isPending} onClick={handleBulkDelete} data-testid="executions-bulk-delete">
        {t_i18n("Delete")}
      </Button>
    </span>
  );

  // The agent picker, kept in the Filters pill with the bulk cancel scoped to it.
  const agentPicker = (
    <div className="flex w-full items-center gap-2">
      <SearchableSelect
        aria-label={t_i18n("Filter by agent")}
        className="h-8 min-w-0 flex-1 content-compact"
        searchable="auto"
        value={agentFilter ?? ANY_AGENT}
        onValueChange={(next) => updateParam("agent", next === ANY_AGENT ? undefined : next)}
        pinned={[{ value: ANY_AGENT, label: t_i18n("All Agents") }]}
        options={agentOptions}
      />
      {bulkCancelButton}
      {bulkCancelHintNode}
    </div>
  );

  // Run type, view, ROI review and Fleet + Agent: the Filters pill of the table.
  const morePills = (
    <>
      {!fleetFilter && (
        <SegmentedFilter
          ariaLabel={t_i18n("Run type filter")}
          options={typeFilters}
          value={typeParam}
          onChange={(key) => updateParam("type", key === "all" ? undefined : key)}
        />
      )}
      {typeParam !== "flows" && (
        <SegmentedFilter
          ariaLabel={t_i18n("View filter")}
          options={filters}
          value={viewParam}
          onChange={(key) => updateParam("filter", key === "all" ? undefined : key)}
        />
      )}
      {fleetFilter && (
        <SegmentedFilter
          ariaLabel={t_i18n("ROI review")}
          options={REVIEW_OPTIONS}
          value={review ?? "all"}
          onChange={(key) => updateParam("review", key === "all" ? undefined : key)}
          testId="run-review-filter"
        />
      )}
      {typeParam !== "flows" && (
        <FleetAgentPickerGroup
          testId="executions-picker-group"
          className="w-full flex-col items-stretch"
          showFleet={!lockedFleet}
          fleet={fleetFilter}
          onFleetChange={setFleetFilter}
          fleetClassName="w-full"
          agentPicker={agentPicker}
        />
      )}
    </>
  );

  const emptyState = (
    <VisibilityEmptyHint
      value={visibility}
      counts={askAllSegments ? { mine: rawCombinedTotal, all: allSegmentsTotal } : undefined}
      onShowAll={() => setVisibility("all")}
    >
      <Card className="border-dashed">
        <CardContent className="flex flex-col items-center justify-center py-16 text-center">
          <PlayCircle className="h-12 w-12 text-default-disabled mb-4" />
          <p className="text-default-secondary">
            {filterParam || agentFilter || review || visibility !== "all" || selectedStatuses.length > 0
              ? t_i18n("No runs match your filters.")
              : typeParam === "flows"
                ? t_i18n("No flow runs yet.")
                : t_i18n("No assignment runs yet.")}
          </p>
          <p className="content-compact text-default-secondary mt-1">
            {typeParam === "flows"
              ? t_i18n("Run a flow from the flow builder.")
              : t_i18n("Trigger an assignment from the Agents page.")}
          </p>
        </CardContent>
      </Card>
    </VisibilityEmptyHint>
  );

  return (
    <div className="space-y-4">
      {!hideHeader && (
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-3xl font-bold">{t_i18n("Executions")}</h1>
            <p className="mt-2 text-default-secondary">{t_i18n("Execution history of all assignment runs across your agents.")}</p>
          </div>
          <div className="flex items-center gap-3">{cleanupButton}</div>
        </div>
      )}

      <div className="space-y-3">

        {/* The merged view is windowed — say so instead of silently stopping */}
        {mergedClamped && (
          <p className="content-compact text-default-secondary">
            {t_i18n(
              "Showing the {count} most recent runs: filter by type to browse older ones.",
              { count: MERGED_WINDOW_MAX },
            )}
          </p>
        )}

        <ApproveByToolDialog
          open={byToolOpen}
          onOpenChange={setByToolOpen}
          data={byToolSummary}
          isLoading={byToolLoading}
        />

        {/* Paused on something this reviewer has already said "always" to.
            Offered rather than applied: a run resuming with no
            explicit action is surprising, and the click costs one gesture
            instead of one per run. */}
        {alreadyCovered.length > 0 && (
          <div className="flex items-center gap-3 rounded-sm border border-feedback-success-primary/30 bg-feedback-success-primary/[0.05] px-3 py-2">
            <HitlIcon state="gated" className="h-4 w-4" />
            <p className="min-w-0 flex-1 content-compact text-feedback-success-primary">
              {/* "on this page" is not padding: the batch answers only the runs
                  it names, so a count that read as "everything waiting" would
                  promise a release this cannot perform. */}
              {t_i18n(
                "{n, plural, one {# paused run on this page only uses} other {# paused runs on this page only use}} tools you have already approved for good.",
                { n: alreadyCovered.length },
              )}
            </p>
            <Button
              size="sm"
              className="h-6 shrink-0 px-2 content-compact"
              disabled={releaseCoveredRuns.isPending}
              onClick={releaseCovered}
            >
              {t_i18n("Release {n}", { n: alreadyCovered.length })}
            </Button>
          </div>
        )}

        {/* The strongest control on this page, so it does not hide as a ghost
            link. The count is the cross-page total from the server, which is
            what distinguishes it at a glance from the selection bar below —
            that one says "selected", this one says how many are waiting
            everywhere. */}
        {((byToolSummary?.tools.length ?? 0) > 0 ||
          (byToolSummary?.mixed_run_count ?? 0) > 0) && (
          <div className="flex items-center gap-3 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/[0.05] px-3 py-2">
            <HitlIcon state="waiting" className="h-4 w-4" />
            <p className="min-w-0 flex-1 content-compact text-feedback-warning-primary">
              {t_i18n(
                "{runs, plural, one {# run is} other {# runs are}} waiting for approval across every page, using {tools, plural, one {# tool} other {# tools}}.",
                {
                  runs: (byToolSummary?.tools ?? []).reduce((n, t) => n + t.run_count, 0)
                    + (byToolSummary?.mixed_run_count ?? 0),
                  tools: byToolSummary?.tools.length ?? 0,
                },
              )}
            </p>
            <Button
              size="sm"
              variant="outline"
              className="h-6 shrink-0 border-feedback-alert-primary/40 px-2 text-feedback-warning-tertiary hover:bg-feedback-alert-primary/10"
              onClick={() => setByToolOpen(true)}
            >
              {t_i18n("Approve by tool…")}
            </Button>
          </div>
        )}

        {/* The runs on the shared DataTable (standard v2, #9378 slice 8f): server mode,
            state in the URL, Status visible on the toolbar, the other filters in the
            Filters pill, the grid kept on the Operations row contract. */}
        <DataTable
          rows={displayItems}
          columns={columns}
          rowKey={itemKey}
          server={{ total: combinedTotal }}
          state={tableState}
          onStateChange={onTableState}
          storageKey={STORAGE_KEY}
          defaultPageSize={DEFAULT_PAGE_SIZE}
          defaultSort={{ key: DEFAULT_SORT, direction: DEFAULT_DIR }}
          searchPlaceholder={t_i18n("Search runs...")}
          compactSearch={compact}
          filtersInPill
          noFilters="status, run type, view, fleet and agent are server filters (toolbar Status pill and filterPills)"
          scope={lockedFleet ? undefined : { value: visibility, onChange: setVisibility, counts: segmentCounts }}
          // The Status pill leads the filter row, beside the host's lead: a lead the box cannot
          // hold on one row with the compact controls wraps the right group (total, page size,
          // view) to its own line, so nothing runs past the edge at 1080 (UX r1 B3 on #11894).
          toolbarLead={
            toolbarLead || !(effectiveNotifOnly || effectiveDigestsOnly) ? (
              <span className="flex items-center gap-2">
                {toolbarLead}
                {!effectiveNotifOnly && !effectiveDigestsOnly && (
                  <ExecutionStatusFilter value={selectedStatuses} onChange={setStatuses} counts={statusCounts} compact={compact} />
                )}
              </span>
            ) : undefined
          }
          toolbar={hideHeader ? cleanupButton : undefined}
          filterPills={morePills}
          externalFilterCount={moreFilterCount}
          // A Status chosen on the toolbar narrows the list too: Clear all shows for it (UX r1 B1 on #1004).
          toolbarFilterActive={selectedStatuses.length > 0}
          // Clear all clears what the count counts, the status and search too, in one write (#1001).
          onClearAll={() =>
            setSearchParams(
              (prev) => clearFilterParams(prev, ["status", "type", "filter", "review", "agent", ...(lockedFleet ? [] : ["fleet"])]),
              { replace: true },
            )
          }
          view={{ value: viewMode, onChange: setViewMode }}
          // One scroller, inside the list: the cards scroll in their box, the page does not
          // (UX r1 B2 on #11894; the table body already fits the viewport).
          fitGrid
          // The cards only in grid view: the list view draws the table (UX r1 B1 on #11894).
          renderGrid={
            viewMode === "grid"
              ? (items) => (
                  <div className="space-y-2" data-testid="execution-list">
                    {items.map(renderItem)}
                  </div>
                )
              : undefined
          }
          renderGridSkeleton={(size) => (
            <OpsListSkeleton label={t_i18n("Loading...")} pageSize={size} actionsClassName="w-16" actions={1} />
          )}
          isLoading={listLoading}
          emptyLabel={t_i18n("No runs match your filters.")}
          emptyContent={emptyState}
          onRowClick={openItem}
          rowActions={(item) =>
            item.kind === "assignment" ? (
              <RowRemoveButton label={t_i18n("Delete this run")} onClick={() => void handleDelete(item.run.id)} />
            ) : null
          }
          rowTestId="execution-row"
          rowActionsWidth={EXECUTION_COLUMN_WIDTHS.actions}
          layout="fixed"
          selection={{
            selected: Array.from(selectedIds),
            onChange: (keys) => {
              setAllMatchingSelected(false);
              setSelectedIds(new Set(keys));
            },
            count: allMatchingSelected ? totalMatchingCount : undefined,
            actions: selectionActions,
            // Flow runs are inspected in the builder and have no bulk action.
            disabledReason: (key) =>
              displayItems.some((item) => item.kind === "flow" && item.run.id === key)
                ? t_i18n("Flow runs have no bulk action")
                : undefined,
          }}
        />
      </div>
      {ConfirmEl}
    </div>
  );
}
