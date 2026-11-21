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

/** Assignment and assignment run API hooks. */

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useEntityListScope } from "@/hooks/useEntityListScope";
import { api, notFoundIsOrdinary } from "@/lib/api";
import { REFETCH_INTERVAL_STANDARD, REFETCH_INTERVAL_FAST } from "@/lib/constants";
import { dropFromFlowNodeCatalog, syncFlowNodeCatalog } from "@/lib/flow-node-catalog";
import { exceedsRunAgentFilter, runAgentIdsParam } from "@/lib/run-agent-filter";
import type { PlatformEventSpec } from "@/lib/platform-events";
import type {
  PendingApprovals,
  ApprovalDecision,
  BatchApprovalDecision,
  BatchApprovalResponse,
  PendingByToolResponse,
  Assignment,
  AssignmentSchedule,
  CreateAssignmentRequest,
  AssignmentRun,
  AssignmentRunDetail,
  EntityListScope,
  FollowupChatResult,
  RunActionRequest,
} from "@/types";

/** The `review` filter of the runs lists and counts: `unreviewed` only, absent means all. */
export type RunReviewFilter = "unreviewed";

/** The `review` URL parameter of an Activity tab (#9365): `unreviewed`, else All. */
export function runReviewFromParam(value: string | null): RunReviewFilter | undefined {
  return value === "unreviewed" ? "unreviewed" : undefined;
}

/** The `sort` of the runs lists (#9365): `newest` is the server's default. */
export type RunSort = "newest" | "oldest" | "status";

/** Every order of the cross-agent feeds (Operations > Executions columns, #9378 slice 8f). */
export type ExecutionRunSort = RunSort | "status_desc" | "agent" | "agent_desc" | "longest" | "shortest";

// --- Assignments ---
export function useAssignment(
  id: string,
  options: {
    /**
     * Treat a 404 as an ordinary answer (`isError`, no retry) instead of
     * throwing it to the page boundary: for a caller that only names the
     * assignment, like the Trace Explorer's scope chip, a deleted one is a
     * state to render, not a fault; 5xx still throws.
     */
    notFoundIsOrdinary?: boolean;
  } = {},
) {
  return useQuery<Assignment>({
    queryKey: ["assignment", id],
    queryFn: () => api.get(`/assignments/${id}`),
    enabled: !!id,
    ...(options.notFoundIsOrdinary ? notFoundIsOrdinary : {}),
  });
}

export function useAssignments(agentId?: string) {
  const params = agentId ? `?agent_id=${agentId}` : "";
  return useQuery<Assignment[]>({
    queryKey: ["assignments", agentId ?? "all"],
    queryFn: () => api.get(`/assignments${params}`),
  });
}

/**
 * Trigger + next-run summary for every visible, enabled assignment.
 * Powers the Agentic Flow "Schedule" tab. Polled so countdowns stay fresh.
 */
export function useAssignmentSchedule(fleetId?: string | null) {
  // `?fleet=` narrows to a fleet's home members on the server.
  const qs = fleetId ? `?fleet=${encodeURIComponent(fleetId)}` : "";
  return useQuery<AssignmentSchedule[]>({
    queryKey: fleetId ? ["assignment-schedule", "fleet", fleetId] : ["assignment-schedule"],
    queryFn: () => api.get(`/assignments/schedule${qs}`),
    refetchInterval: REFETCH_INTERVAL_STANDARD,
  });
}

/** The platform events a `platform_event` trigger can name; the catalog only changes with a release. */
export function usePlatformEvents(enabled = true) {
  return useQuery<PlatformEventSpec[]>({
    queryKey: ["assignment-platform-events"],
    queryFn: () => api.get("/assignments/platform-events"),
    staleTime: Infinity,
    enabled,
  });
}

export function useCreateAssignment() {
  const qc = useQueryClient();
  return useMutation<Assignment, Error, CreateAssignmentRequest>({
    mutationFn: (data) => api.post("/assignments", data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["assignments"] });
      qc.invalidateQueries({ queryKey: ["agents"] });
      syncFlowNodeCatalog(qc);
    },
  });
}

export function useUpdateAssignment() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...data }: { id: string } & Partial<Assignment>) =>
      api.put(`/assignments/${id}`, data),
    onSuccess: (_data, variables) => {
      qc.invalidateQueries({ queryKey: ["assignments"] });
      qc.invalidateQueries({ queryKey: ["assignment", variables.id] });
      syncFlowNodeCatalog(qc);
    },
  });
}

export function useDeleteAssignment() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/assignments/${id}`),
    onSuccess: (_d, id) => {
      qc.invalidateQueries({ queryKey: ["assignments"] });
      qc.invalidateQueries({ queryKey: ["agents"] });
      dropFromFlowNodeCatalog(qc, "assignment", id);
    },
  });
}

export function useTriggerAssignment() {
  const qc = useQueryClient();
  return useMutation<AssignmentRun, Error, { assignmentId: string; payload?: Record<string, unknown> }>({
    mutationFn: ({ assignmentId, payload }) =>
      api.post(`/assignments/${assignmentId}/trigger`, { payload }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["assignment-runs"] });
      qc.invalidateQueries({ queryKey: ["activity"] });
    },
  });
}

export function useAssignmentRuns(
  assignmentId: string,
  limit = 20,
  offset = 0,
  search?: string,
  parentRunId = "none",
  /** `"unreviewed"`: only the runs whose ROI record has no outcome yet (#9310). */
  review?: RunReviewFilter,
  /** The order; absent = newest first. */
  sort?: RunSort,
) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (search) params.set("search", search);
  if (parentRunId) params.set("parent_run_id", parentRunId);
  if (review) params.set("review", review);
  if (sort) params.set("sort", sort);
  return useQuery<AssignmentRun[]>({
    queryKey: ["assignment-runs", assignmentId, limit, offset, search ?? "", parentRunId, review ?? "all", sort ?? "newest"],
    queryFn: () => api.get(`/assignments/${assignmentId}/runs?${params}`),
    enabled: !!assignmentId,
    refetchInterval: REFETCH_INTERVAL_STANDARD,
  });
}

export function useAssignmentRunDetail(
  runId: string,
  /** A 404 is an answer, not a fault: the run page renders Page not found (#797 S4). */
  options: { notFoundIsOrdinary?: boolean } = {},
) {
  return useQuery<AssignmentRunDetail>({
    queryKey: ["assignment-run", runId],
    queryFn: () => api.get(`/assignments/runs/${runId}`),
    enabled: !!runId,
    ...(options.notFoundIsOrdinary ? notFoundIsOrdinary : {}),
    // Auto-refresh while the run is still in progress. ``awaiting_human``
    // polls at the STANDARD interval (HITL waits can be long and the respond
    // mutation already invalidates the cache) so a response submitted by
    // another admin still flips the panel without a manual reload;
    // active execution keeps the FAST interval. ``queued`` (an accepted
    // webhook delivery waiting its turn) is open-ended like a HITL wait, so it
    // gets the STANDARD interval rather than hammering while a long run ahead
    // of it finishes - it still flips to running on its own.
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (status === "running" || status === "pending") return REFETCH_INTERVAL_FAST;
      if (status === "awaiting_human" || status === "queued")
        return REFETCH_INTERVAL_STANDARD;
      // A completed external run whose A2A task waits on its caller: the
      // answer links the run that continues it, so the page keeps checking.
      if (query.state.data?.a2a_exchange?.asked?.state === "waiting") return REFETCH_INTERVAL_STANDARD;
      return false;
    },
  });
}

/** Invalidate all run-related caches (after delete/purge). */
function invalidateRunCaches(qc: ReturnType<typeof useQueryClient>) {
  qc.invalidateQueries({ queryKey: ["all-assignment-runs"] });
  qc.invalidateQueries({ queryKey: ["assignment-runs"] });
  qc.invalidateQueries({ queryKey: ["assignment-run"] });
  qc.invalidateQueries({ queryKey: ["assignments"] });
  qc.invalidateQueries({ queryKey: ["dashboard-stats"] });
  // The cross-page approval summary is derived from run state, so every path
  // that changes a run stales it — answering, dismissing, deleting. It lived on
  // the by-tool mutation alone at first, which left the banner claiming work
  // that had just been finished from the card or the selection bar.
  qc.invalidateQueries({ queryKey: ["pending-approvals-by-tool"] });
}

export function useDeleteAssignmentRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) => api.delete(`/assignments/runs/${runId}`),
    onSuccess: () => invalidateRunCaches(qc),
  });
}

export function useCleanupStaleRuns() {
  const qc = useQueryClient();
  return useMutation<{ cleaned: number; message: string }, Error, void>({
    mutationFn: () => api.post("/assignments/runs/cleanup-stale"),
    onSuccess: () => invalidateRunCaches(qc),
  });
}

export function useAssignmentRunCount(
  status?: string,
  search?: string,
  notifications?: boolean,
  /** The visibility segment (`lib/visibility.ts`): mine | shared | company | all. */
  vis?: string,
  agentId?: string,
  assignmentId?: string,
  digests?: boolean,
  parentRunId?: string,
  /** Only work submitted by the A2A partner behind this integration. */
  a2aIntegrationId?: string,
  /** `"all"`: every run for an admin (an entity's Activity tab); the feed otherwise. */
  requestedScope?: EntityListScope,
  /** Only the runs of these agents (an entity's bound agents); empty = no run. */
  agentIds?: Iterable<string>,
  enabled = true,
  /** Only the runs of this fleet's current home members. */
  fleet?: string,
  /** `"unreviewed"`: only the runs whose ROI record has no outcome yet (#9310). */
  review?: RunReviewFilter,
) {
  const scope = useEntityListScope({ scope: requestedScope });
  const agentIdsParam = runAgentIdsParam(agentIds);
  const params = new URLSearchParams();
  if (review) params.set("review", review);
  if (status) params.set("status", status);
  if (search) params.set("search", search);
  if (notifications) params.set("notifications", "true");
  if (digests) params.set("digests", "true");
  if (vis) params.set("vis", vis);
  if (agentId) params.set("agent_id", agentId);
  if (agentIdsParam !== undefined) params.set("agent_ids", agentIdsParam);
  if (assignmentId) params.set("assignment_id", assignmentId);
  if (parentRunId !== undefined) params.set("parent_run_id", parentRunId);
  if (a2aIntegrationId) params.set("a2a_integration_id", a2aIntegrationId);
  if (scope) params.set("scope", scope);
  if (fleet) params.set("fleet", fleet);
  const qs = params.toString();
  return useQuery<{ count: number }>({
    queryKey: ["assignment-run-count", status ?? "all", search ?? "", notifications ?? false, digests ?? false, vis ?? "all", agentId ?? "all", assignmentId ?? "all", parentRunId ?? "none", a2aIntegrationId ?? "all", scope ?? "visible", agentIdsParam ?? "any", fleet ?? "any", review ?? "all"],
    queryFn: () => api.get(`/assignments/runs/count${qs ? `?${qs}` : ""}`),
    enabled,
    refetchInterval: REFETCH_INTERVAL_STANDARD,
  });
}

export function useBulkDeleteRuns() {
  const qc = useQueryClient();
  return useMutation<{ deleted: number }, Error, {
    run_ids?: string[];
    all_matching_filters?: boolean;
    status?: string;
    search?: string;
    notifications?: boolean;
    /** The visibility segment the list shows (`lib/visibility.ts`). */
    vis?: string;
    agent_id?: string;
    /** The feed the filters were read from: `all` for an admin's entity Activity tab. */
    scope?: EntityListScope;
  }>({
    mutationFn: (body) => api.post("/assignments/runs/bulk-delete", body),
    onSuccess: () => invalidateRunCaches(qc),
  });
}

/** What a paused run waits on: tool calls to approve, or a question (`GET .../pending-approvals`). */
export function usePendingApprovals(runId: string | null | undefined) {
  return useQuery<PendingApprovals>({
    queryKey: ["pending-approvals", runId],
    queryFn: () => api.get(`/assignments/runs/${runId}/pending-approvals`),
    enabled: !!runId,
    retry: false,
  });
}

/** The filter of `POST /assignments/runs/bulk-cancel`: runs that have
 *  not started yet, of an agent and / or an assignment (a status-only filter
 *  is refused with 422). `dry_run` answers the count and changes nothing. */
interface BulkCancelRunsBody {
  agent_id?: string;
  assignment_id?: string;
  status?: Array<"pending" | "delayed" | "queued">;
  created_before?: string;
  dry_run?: boolean;
}

export function useBulkCancelRuns() {
  const qc = useQueryClient();
  return useMutation<{ count: number; dry_run: boolean }, Error, BulkCancelRunsBody>({
    meta: { suppressGlobalErrorToast: true },
    mutationFn: (body) => api.post("/assignments/runs/bulk-cancel", body),
    // A dry run changes nothing: only the real call refreshes the lists.
    onSuccess: (data) => {
      if (!data.dry_run) invalidateRunCaches(qc);
    },
  });
}

export function useRespondToHumanInput() {
  const qc = useQueryClient();
  return useMutation<AssignmentRun, Error, { runId: string; response: string }>({
    mutationFn: ({ runId, response }) =>
      api.post(`/assignments/runs/${runId}/respond`, { response }),
    onSuccess: () => {
      invalidateRunCaches(qc);
      qc.invalidateQueries({ queryKey: ["activity"] });
    },
  });
}

/**
 * Resolve a run paused on the per-tool approval gate.
 *
 * Same endpoint as {@link useRespondToHumanInput}, different body: the backend
 * branches on `decisions` being present. It requires a decision for every
 * pending call — a partial set is refused, because resuming with an undecided
 * call leaves an unanswered tool call the LLM providers reject.
 */
export function useSubmitApprovalDecisions() {
  const qc = useQueryClient();
  return useMutation<
    AssignmentRun,
    Error,
    { runId: string; decisions: ApprovalDecision[] }
  >({
    mutationFn: ({ runId, decisions }) =>
      api.post(`/assignments/runs/${runId}/respond`, { decisions }),
    onSuccess: () => {
      invalidateRunCaches(qc);
      qc.invalidateQueries({ queryKey: ["activity"] });
      // A "Yes, always" decision writes a standing approval, so the user's
      // preference list is now stale.
      qc.invalidateQueries({ queryKey: ["tool-approvals"] });
    },
  });
}

/**
 * Answer approvals across several runs in one action (FR-031).
 *
 * Partial success is a success: the response reports per member, and a refused
 * one does not undo the applied ones (FR-033). Callers must read `results`
 * rather than treating a 200 as "all done".
 */
export function useBatchApprovalDecisions() {
  const qc = useQueryClient();
  return useMutation<
    BatchApprovalResponse,
    Error,
    { decisions: BatchApprovalDecision[] }
  >({
    mutationFn: ({ decisions }) =>
      api.post(`/assignments/runs/approvals/batch`, { decisions }),
    onSuccess: () => {
      invalidateRunCaches(qc);
      qc.invalidateQueries({ queryKey: ["activity"] });
      // This accepts approve_always too — TaskHistory's bulk approve sends it
      // — so the preference list goes stale here exactly as it does for the
      // two siblings above.
      qc.invalidateQueries({ queryKey: ["tool-approvals"] });
    },
  });
}

/**
 * What is waiting, grouped by tool, across every page.
 *
 * From the server rather than the loaded rows: a per-tool decision reaches past
 * the page, so a total taken from one page would understate exactly what the
 * reviewer is being asked to judge.
 */
export function usePendingApprovalsByTool(
  enabled = true,
  agentId?: string,
  ownOnly?: boolean,
) {
  return useQuery<PendingByToolResponse>({
    // Both scopes are part of the key: the agent-scoped total, the own-runs one
    // and the global one are different answers, and sharing a cache entry would
    // show one surface another's count.
    queryKey: ["pending-approvals-by-tool", agentId ?? null, ownOnly ?? false],
    queryFn: () => {
      const params = new URLSearchParams();
      if (agentId) params.set("agent_id", agentId);
      if (ownOnly) params.set("own_only", "true");
      const query = params.toString();
      return api.get(
        `/assignments/runs/approvals/pending-by-tool${query ? `?${query}` : ""}`,
      );
    },
    enabled,
    // Faster than the runs list beneath it, deliberately. Invalidation covers
    // work the reviewer finishes but not work that arrives, and the two lists
    // poll on independent clocks — so at equal intervals they can disagree for
    // a whole one, which reads as "the list shows 2 and the banner does not".
    // This is a small aggregate and it is the "does anything need me" signal,
    // so it is the cheap half to run often.
    //
    // Note it still pauses while the tab is hidden (`refetchIntervalInBackground`
    // is false by default), which is correct: nobody is reading it.
    refetchInterval: REFETCH_INTERVAL_FAST,
  });
}

/** Answer every paused run whose calls are all among the named tools. */
/**
 * Release the runs a standing approval already covers (FR-039b).
 *
 * Names runs and carries no decisions: which calls get approved is the
 * server's to determine. The page used to compute coverage itself and send
 * per-call decisions to the batch endpoint, which cannot check the rule — it
 * is told which calls to answer, so a partially covered run was
 * indistinguishable from an ordinary approval.
 */
export function useReleaseCovered() {
  const qc = useQueryClient();
  return useMutation<BatchApprovalResponse, Error, string[]>({
    mutationFn: (runIds) =>
      api.post(`/assignments/runs/approvals/release-covered`, {
        run_ids: runIds,
      }),
    onSuccess: () => {
      invalidateRunCaches(qc);
      qc.invalidateQueries({ queryKey: ["activity"] });
    },
  });
}

export function useApproveByTool() {
  const qc = useQueryClient();
  return useMutation<
    BatchApprovalResponse,
    Error,
    {
      toolNames: string[];
      decision: "approve" | "approve_always" | "reject";
      rejectionReason?: string;
      // Sent whenever the caller is scoped to one agent, so the sweep stops
      // where the page the reviewer answered from does.
      agentId?: string;
      // Scopes to the caller's own runs — the reach of their allow-all switch,
      // which an admin's answerable set is wider than.
      ownOnly?: boolean;
    }
  >({
    mutationFn: ({ toolNames, decision, rejectionReason, agentId, ownOnly }) =>
      api.post(`/assignments/runs/approvals/by-tool`, {
        tool_names: toolNames,
        decision,
        rejection_reason: rejectionReason,
        agent_id: agentId,
        own_only: ownOnly,
      }),
    onSuccess: () => {
      // Covers both scopes of the by-tool summary: the sweep drained runs the
      // global total was counting too, and the key prefix matches either.
      invalidateRunCaches(qc);
      qc.invalidateQueries({ queryKey: ["activity"] });
      // "always" writes a standing approval, so the profile list is now stale.
      qc.invalidateQueries({ queryKey: ["tool-approvals"] });
    },
  });
}

/** Open requests to act on a paused run, for the people who may answer it. */
export function useRunActionRequests(runId: string, enabled: boolean) {
  return useQuery<RunActionRequest[]>({
    queryKey: ["run-action-requests", runId],
    queryFn: () => api.get(`/assignments/runs/${runId}/action-requests`),
    enabled: enabled && !!runId,
    // A request can arrive while the run page is open.
    refetchInterval: REFETCH_INTERVAL_STANDARD,
  });
}

export function useDismissRunActionRequest() {
  const qc = useQueryClient();
  return useMutation<void, Error, { runId: string; requestId: string }>({
    mutationFn: ({ runId, requestId }) =>
      api.post(`/assignments/runs/${runId}/action-requests/${requestId}/dismiss`),
    onSuccess: (_data, { runId }) => qc.invalidateQueries({ queryKey: ["run-action-requests", runId] }),
  });
}

export function useDismissRun() {
  const qc = useQueryClient();
  return useMutation<AssignmentRun, Error, string>({
    mutationFn: (runId: string) =>
      api.post(`/assignments/runs/${runId}/dismiss`),
    onSuccess: () => {
      invalidateRunCaches(qc);
      qc.invalidateQueries({ queryKey: ["activity"] });
    },
  });
}

export function useFollowupRun() {
  const qc = useQueryClient();
  return useMutation<AssignmentRun, Error, { runId: string; prompt: string }>({
    mutationFn: ({ runId, prompt }) =>
      api.post(`/assignments/runs/${runId}/followup`, { prompt }),
    onSuccess: () => {
      invalidateRunCaches(qc);
      qc.invalidateQueries({ queryKey: ["activity"] });
    },
  });
}

/**
 * Open an interactive follow-up chat seeded from a terminal run. Returns the
 * new conversation (routed to the run's agent, pre-seeded with the run's task,
 * result and execution trace) so the caller can navigate to the chat page.
 */
export function useFollowupChat() {
  const qc = useQueryClient();
  return useMutation<FollowupChatResult, Error, { runId: string; prompt?: string }>({
    mutationFn: ({ runId, prompt }) =>
      api.post(`/assignments/runs/${runId}/followup-chat`, { prompt }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
}

export function useAllAssignmentRuns(
  limit = 50,
  offset = 0,
  status?: string,
  search?: string,
  notifications?: boolean,
  /** The visibility segment (`lib/visibility.ts`): mine | shared | company | all. */
  vis?: string,
  agentId?: string,
  digests?: boolean,
  parentRunId?: string,
  enabled = true,
  /** Only work submitted by the A2A partner behind this integration.
   *
   * Scoped server-side by the calling registration stamped on each run, not by
   * the agents the integration is bound to — two A2A integrations bound to one
   * agent would otherwise each display the other's traffic. */
  a2aIntegrationId?: string,
  /** `"all"`: every run for an admin (an entity's Activity tab); the feed otherwise. */
  requestedScope?: EntityListScope,
  /** Only the runs of these agents (an entity's bound agents); empty = no run. */
  agentIds?: Iterable<string>,
  /** Only the runs of this fleet's current home members. */
  fleet?: string,
  /** `"unreviewed"`: only the runs whose ROI record has no outcome yet (#9310). */
  review?: RunReviewFilter,
  /** The order; absent = newest first. */
  sort?: ExecutionRunSort,
  /** Only this assignment's runs (its Activity tab, #9378 slice 4a). */
  assignmentId?: string,
) {
  const scope = useEntityListScope({ scope: requestedScope });
  const agentIdsParam = runAgentIdsParam(agentIds);
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (review) params.set("review", review);
  if (sort) params.set("sort", sort);
  if (assignmentId) params.set("assignment_id", assignmentId);
  if (status) params.set("status", status);
  if (search) params.set("search", search);
  if (notifications) params.set("notifications", "true");
  if (digests) params.set("digests", "true");
  if (vis) params.set("vis", vis);
  if (agentId) params.set("agent_id", agentId);
  if (agentIdsParam !== undefined) params.set("agent_ids", agentIdsParam);
  // Default "none" = top-level only; pass a UUID to fetch children of a specific run
  if (parentRunId !== undefined) params.set("parent_run_id", parentRunId);
  if (a2aIntegrationId) params.set("a2a_integration_id", a2aIntegrationId);
  if (scope) params.set("scope", scope);
  if (fleet) params.set("fleet", fleet);
  return useQuery<AssignmentRunDetail[]>({
    queryKey: ["all-assignment-runs", limit, offset, status ?? "all", search ?? "", notifications ?? false, digests ?? false, vis ?? "all", agentId ?? "all", parentRunId ?? "none", a2aIntegrationId ?? "all", scope ?? "visible", agentIdsParam ?? "any", fleet ?? "any", review ?? "all", sort ?? "newest", assignmentId ?? "all"],
    queryFn: () => api.get(`/assignments/runs/all?${params}`),
    enabled,
    refetchInterval: REFETCH_INTERVAL_STANDARD,
  });
}

/**
 * Run total and latest run of the agents bound to an entity (its Overview
 * stats), both counted by the server over every run of those agents. The total
 * is `undefined` while loading and when there are more agents than one feed
 * read filters by.
 */
export function useBoundAgentRunStats(agentIds: ReadonlySet<string>) {
  const enabled = !exceedsRunAgentFilter(agentIds);
  const { data: countData } = useAssignmentRunCount(
    undefined, undefined, undefined, undefined, undefined, undefined, undefined, undefined, undefined, "all", agentIds, enabled,
  );
  // limit 1: the feed orders by created_at desc, so its head IS the latest run.
  const { data: latestRuns } = useAllAssignmentRuns(
    1, 0, undefined, undefined, undefined, undefined, undefined, undefined, undefined, enabled, undefined, "all", agentIds,
  );
  const latest = enabled ? latestRuns?.[0] : undefined;
  return {
    totalRuns: enabled ? countData?.count : undefined,
    lastRun: latest ? new Date(latest.started_at || latest.created_at) : null,
  };
}

/** Returns agent IDs that are currently active (assignment runs OR chat processing). */
export function useActiveAgentIds() {
  return useQuery<string[]>({
    queryKey: ["active-agent-ids"],
    queryFn: () => api.get("/assignments/runs/active-agents"),
    refetchInterval: REFETCH_INTERVAL_FAST,
  });
}

/** Fetch ALL descendants of a run as a flat list (recursive, chronological). */
export function useDescendantRuns(runId: string | null) {
  return useQuery<AssignmentRunDetail[]>({
    queryKey: ["descendant-runs", runId],
    queryFn: () => api.get(`/assignments/runs/${runId}/descendants`),
    enabled: !!runId,
    refetchInterval: REFETCH_INTERVAL_STANDARD,
  });
}
