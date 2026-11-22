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

/** Work-item queue API hooks. */

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, notFoundIsOrdinary } from "@/lib/api";
import { REFETCH_INTERVAL_STANDARD } from "@/lib/constants";
import type { Visibility } from "@/lib/visibility";
import type {
  ScheduledWorkItem,
  WorkItem,
  WorkItemGraph,
  WorkItemOwnerCount,
  WorkItemStatus,
} from "@/types";

interface WorkItemFilters {
  owner_agent_id?: string;
  /** One status or several, comma-separated (`open,blocked`). */
  status?: WorkItemStatus | string;
  correlation_key?: string;
  /** The items whose key starts with it (`GET /work-items?correlation_key_prefix=`). */
  correlation_key_prefix?: string;
  open_only?: boolean;
  stale_only?: boolean;
  /** Only the items the platform runs at their time. */
  scheduled_only?: boolean;
  /** Only the items waiting for the caller: what the dashboard notice counts. */
  waiting_for_me?: boolean;
  search?: string;
  /** The visibility segment (`lib/visibility.ts`); omitted = every item the caller can see. */
  vis?: Visibility;
  /** A workspace's Operations tab: filed in it, or owned or created by its fleet's members. */
  workspace?: string;
  /** A fleet: owned or created by its current home members. */
  fleet?: string;
  /** The list's order (`services/work_item.WORK_ITEM_SORTS`); omitted = newest created first. */
  sort?: WorkItemSort;
  /** The Operations table's column sort (`services/work_item.WORK_ITEM_TABLE_ORDERS`); wins over `sort`. */
  order?: WorkItemOrder;
  direction?: "asc" | "desc";
  limit?: number;
  offset?: number;
}

/** The columns `GET /work-items?order=` sorts by. */
export type WorkItemOrder = "created" | "updated" | "due" | "priority" | "status" | "owner" | "completed";

/** The orders `GET /work-items` takes; `completed_desc` is newest completion first. */
type WorkItemSort = "created_desc" | "completed_desc" | "updated_desc";

/** Serialize the count-relevant filters (everything except pagination). */
function toCountQuery(filters: WorkItemFilters): string {
  const params = new URLSearchParams();
  if (filters.owner_agent_id) params.set("owner_agent_id", filters.owner_agent_id);
  if (filters.status) params.set("status", filters.status);
  if (filters.correlation_key) params.set("correlation_key", filters.correlation_key);
  if (filters.correlation_key_prefix) params.set("correlation_key_prefix", filters.correlation_key_prefix);
  if (filters.open_only) params.set("open_only", "true");
  if (filters.stale_only) params.set("stale_only", "true");
  if (filters.scheduled_only) params.set("scheduled_only", "true");
  if (filters.waiting_for_me) params.set("waiting_for_me", "true");
  if (filters.search) params.set("search", filters.search);
  if (filters.vis) params.set("vis", filters.vis);
  if (filters.workspace) params.set("workspace", filters.workspace);
  if (filters.fleet) params.set("fleet", filters.fleet);
  return params.toString();
}

function toQuery(filters: WorkItemFilters): string {
  const params = new URLSearchParams(toCountQuery(filters));
  if (filters.sort && filters.sort !== "created_desc") params.set("sort", filters.sort);
  if (filters.order) {
    params.set("order", filters.order);
    params.set("direction", filters.direction ?? "desc");
  }
  if (filters.limit) params.set("limit", String(filters.limit));
  if (filters.offset) params.set("offset", String(filters.offset));
  return params.toString();
}

/**
 * `GET /work-items/graph`: the Graph view's nodes and edges for the list's
 * filters, in one call (`services/work_item_graph.py`). Pagination and sort
 * do not apply: the graph reads the first 500 matches (`truncated` says so).
 */
export function useWorkItemGraph(filters: WorkItemFilters = {}, enabled = true) {
  const qs = toCountQuery(filters);
  return useQuery<WorkItemGraph>({
    queryKey: ["work-items", "graph", qs],
    queryFn: () => api.get(`/work-items/graph${qs ? `?${qs}` : ""}`),
    enabled,
    refetchInterval: REFETCH_INTERVAL_STANDARD,
  });
}

export function useWorkItems(filters: WorkItemFilters = {}, enabled = true) {
  // Key on the resolved query string (the actual request) so a `false`/omitted
  // boolean or a `0` offset can never split the cache while hitting the same URL.
  const qs = toQuery(filters);
  return useQuery<WorkItem[]>({
    queryKey: ["work-items", "list", qs],
    queryFn: () => api.get(`/work-items${qs ? `?${qs}` : ""}`),
    enabled,
  });
}

export function useWorkItemCount(filters: WorkItemFilters = {}, deletableOnly = false, enabled = true) {
  // `deletableOnly` counts only the rows the caller can bulk-delete so the
  // "select all N matching" UI matches what the backend will actually remove.
  // When false the query key/URL is identical to the visible-count call, so an
  // admin (who can delete everything visible) dedupes onto a single request.
  const base = toCountQuery(filters);
  const qs = deletableOnly ? `${base}${base ? "&" : ""}deletable_only=true` : base;
  return useQuery<{ count: number }>({
    queryKey: ["work-items", "count", qs],
    queryFn: () => api.get(`/work-items/count${qs ? `?${qs}` : ""}`),
    enabled,
  });
}

/**
 * The owner agents of the items the list's filters match, with counts:
 * what the agent filter offers. The owner filter itself is left out, so the
 * choice stays visible once made. `enabled: false` holds the request, as the
 * list and counts do while the page settles its scope: a `?fleet=`
 * read waits until the fleet is known to be the reader's.
 */
export function useWorkItemOwners(
  filters: WorkItemFilters = {},
  { enabled = true }: { enabled?: boolean } = {},
) {
  const params = new URLSearchParams(toCountQuery({ ...filters, owner_agent_id: undefined }));
  const qs = params.toString();
  return useQuery<WorkItemOwnerCount[]>({
    queryKey: ["work-items", "owners", qs],
    queryFn: () => api.get(`/work-items/owners${qs ? `?${qs}` : ""}`),
    enabled,
  });
}

/**
 * The items per status under the list's filters minus the status: the numbers
 * the Status filter of Operations > Work items shows beside each value.
 */
export function useWorkItemStatusCounts(
  filters: WorkItemFilters = {},
  { enabled = true }: { enabled?: boolean } = {},
) {
  const params = new URLSearchParams(toCountQuery({ ...filters, status: undefined, open_only: false }));
  const qs = params.toString();
  return useQuery<Record<WorkItemStatus, number>>({
    queryKey: ["work-items", "status-counts", qs],
    queryFn: () => api.get(`/work-items/status-counts${qs ? `?${qs}` : ""}`),
    enabled,
  });
}

export function useStaleWorkItemCount(vis?: Visibility, enabled = true) {
  const qs = vis ? `?vis=${vis}` : "";
  return useQuery<{ count: number }>({
    queryKey: ["work-items", "stale-count", vis ?? "all"],
    queryFn: () => api.get(`/work-items/stale-count${qs}`),
    refetchInterval: REFETCH_INTERVAL_STANDARD,
    enabled,
  });
}

/** The oldest of the items waiting for the caller, as the dashboard names it. */
export interface WorkItemWaitingForMe {
  id: string;
  title: string;
  reason: string | null;
  since: string | null;
}

/**
 * The caller's work items that wait for the caller (a decision owed, or an
 * item of theirs with no owner agent open past due): the dashboard notice.
 * An agent's own waits are never in it.
 */
export function useWorkItemsWaitingForMe(enabled = true) {
  return useQuery<{ count: number; oldest: WorkItemWaitingForMe | null }>({
    queryKey: ["work-items", "waiting-for-me"],
    queryFn: () => api.get("/work-items/waiting-for-me"),
    refetchInterval: REFETCH_INTERVAL_STANDARD,
    enabled,
  });
}

export function useCompleteWorkItem() {
  const qc = useQueryClient();
  return useMutation<
    WorkItem,
    Error,
    { id: string; result?: string; status?: "done" | "cancelled" }
  >({
    mutationFn: ({ id, ...data }) => api.post(`/work-items/${id}/complete`, data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["work-items"] }),
  });
}

/**
 * Answer the decision an item waits for (`POST /work-items/{id}/answer`):
 * an option key, a free text, or both. The item goes back to its owner
 * agent's queue with the answer on it. The decision card toasts a failure
 * itself with a translated hint, so the global error toast (raw backend
 * detail) stays quiet.
 */
export function useAnswerWorkItem() {
  const qc = useQueryClient();
  return useMutation<WorkItem, Error, { id: string; option?: string; text?: string }>({
    mutationFn: ({ id, ...body }) => api.post(`/work-items/${id}/answer`, body),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["work-items"] }),
  });
}

/**
 * Send Re-check, It's solved or Do it to the item's own agent (#774): a note
 * lands on the item and the agent takes it now. The caller toasts the
 * outcome itself, so the global error toast stays quiet.
 */
export function useAskWorkItem() {
  const qc = useQueryClient();
  return useMutation<
    { item: WorkItem; agent: { id: string; name: string }; via: "inbox" | "run" },
    Error,
    { id: string; action: "recheck" | "solved" | "do"; text?: string }
  >({
    mutationFn: ({ id, ...body }) => api.post(`/work-items/${id}/ask`, body),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["work-items"] }),
  });
}

/** Put an item back in the queue: open, unclaimed, dispatch attempts reset. */
export function useRequeueWorkItem() {
  const qc = useQueryClient();
  return useMutation<WorkItem, Error, string>({
    mutationFn: (id) => api.post(`/work-items/${id}/requeue`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["work-items"] }),
  });
}

export function useDeleteWorkItem() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (id) => api.delete(`/work-items/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["work-items"] }),
  });
}

interface BulkDeleteWorkItemsRequest {
  ids?: string[];
  all_matching_filters?: boolean;
  owner_agent_id?: string;
  /** One status or several, comma-separated, as the list asks. */
  status?: WorkItemStatus | string;
  correlation_key?: string;
  open_only?: boolean;
  stale_only?: boolean;
  scheduled_only?: boolean;
  search?: string;
  vis?: Visibility;
}

/**
 * One work item by id (the chat card, a run's "Scheduled" link). A deleted or
 * invisible item answers 404, which its callers show as gone: thrown, it
 * replaced the whole conversation (or the video editor around its rail).
 */
export function useWorkItem(id: string | null | undefined) {
  return useQuery<WorkItem>({
    queryKey: ["work-items", "one", id],
    queryFn: () => api.get(`/work-items/${id}`),
    enabled: !!id,
    ...notFoundIsOrdinary,
  });
}

type ScheduledListState = "upcoming" | "done" | "blocked" | "missed" | "cancelled" | "all";

/**
 * The caller's scheduled work items with their agent: the one-off half
 * of Agentic Flow > Schedule and of an agent's "Coming up" card. Keyed under
 * `work-items` so every activity-stream event refreshes it.
 */
export function useScheduledWorkItems(agentId?: string, state: ScheduledListState = "upcoming", fleetId?: string | null) {
  const params = new URLSearchParams({ state });
  if (agentId) params.set("agent_id", agentId);
  // A fleet's schedule is narrowed by the server, not by filtering a global page.
  if (fleetId) params.set("fleet", fleetId);
  const qs = params.toString();
  return useQuery<ScheduledWorkItem[]>({
    queryKey: ["work-items", "scheduled", qs],
    queryFn: () => api.get(`/work-items/scheduled?${qs}`),
    refetchInterval: REFETCH_INTERVAL_STANDARD,
  });
}

export function useCancelScheduledWorkItem() {
  const qc = useQueryClient();
  return useMutation<WorkItem, Error, string>({
    mutationFn: (id) => api.post(`/work-items/${id}/cancel`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["work-items"] }),
  });
}

export function useRescheduleScheduledWorkItem() {
  const qc = useQueryClient();
  return useMutation<WorkItem, Error, { id: string; due_at: string }>({
    mutationFn: ({ id, due_at }) => api.post(`/work-items/${id}/reschedule`, { due_at }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["work-items"] }),
  });
}

export function useBulkDeleteWorkItems() {
  const qc = useQueryClient();
  return useMutation<{ deleted: number }, Error, BulkDeleteWorkItemsRequest>({
    mutationFn: (body) => api.post("/work-items/bulk-delete", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["work-items"] }),
  });
}
