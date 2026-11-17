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

/** Cross-run shared-state API hooks. */

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Visibility } from "@/lib/visibility";
import type { SharedState, SharedStateStatus, WorkItemOwnerCount } from "@/types";

interface SharedStateFilters {
  search?: string;
  /** Lifecycle status — omit for all statuses. */
  status?: SharedStateStatus;
  /** Only active entries that have gone quiet past the staleness window. */
  stale_only?: boolean;
  /** The visibility segment (`lib/visibility.ts`); omitted = every entry the caller can see. */
  vis?: Visibility;
  /** A workspace's Operations tab: filed in it, or owned by its fleet's members. */
  workspace?: string;
  /** A fleet: owned by its current home members. */
  fleet?: string;
  /** One owner agent. */
  owner_agent_id?: string;
  limit?: number;
  offset?: number;
  /** The Operations table's server sort (shared_state.py `LIST_SORTS`); omitted = newest updated first. */
  sort?: "key" | "owner" | "updated" | "size";
  direction?: "asc" | "desc";
}

/** Serialize the count-relevant filters (everything except pagination). */
function toCountQuery(filters: SharedStateFilters): string {
  const params = new URLSearchParams();
  if (filters.search) params.set("search", filters.search);
  if (filters.status) params.set("status", filters.status);
  if (filters.stale_only) params.set("stale_only", "true");
  if (filters.vis) params.set("vis", filters.vis);
  if (filters.workspace) params.set("workspace", filters.workspace);
  if (filters.fleet) params.set("fleet", filters.fleet);
  if (filters.owner_agent_id) params.set("owner_agent_id", filters.owner_agent_id);
  return params.toString();
}

function toQuery(filters: SharedStateFilters): string {
  const params = new URLSearchParams(toCountQuery(filters));
  if (filters.limit) params.set("limit", String(filters.limit));
  if (filters.offset) params.set("offset", String(filters.offset));
  if (filters.sort) params.set("sort", filters.sort);
  if (filters.direction) params.set("direction", filters.direction);
  return params.toString();
}

export function useSharedStates(filters: SharedStateFilters = {}, enabled = true) {
  const qs = toQuery(filters);
  return useQuery<SharedState[]>({
    queryKey: ["shared-state", "list", qs],
    queryFn: () => api.get(`/shared-state${qs ? `?${qs}` : ""}`),
    enabled,
  });
}

export function useSharedStateCount(filters: SharedStateFilters = {}, deletableOnly = false, enabled = true) {
  // `deletableOnly` counts only the rows the caller can bulk-delete so the
  // "select all N matching" UI matches what the backend will actually remove.
  // When false the query key/URL is identical to the visible-count call, so an
  // admin (who can delete everything visible) dedupes onto a single request.
  const base = toCountQuery(filters);
  const qs = deletableOnly ? `${base}${base ? "&" : ""}deletable_only=true` : base;
  return useQuery<{ count: number }>({
    queryKey: ["shared-state", "count", qs],
    queryFn: () => api.get(`/shared-state/count${qs ? `?${qs}` : ""}`),
    enabled,
  });
}

/**
 * The owner agents of the entries the list's filters match, with counts:
 * what the owner-agent filter offers, as `useWorkItemOwners` does for
 * Work Items. The owner filter itself is left out, so the choice stays visible
 * once made; `enabled: false` holds the request while the page settles its
 * scope.
 */
export function useSharedStateOwners(
  filters: SharedStateFilters = {},
  { enabled = true }: { enabled?: boolean } = {},
) {
  const qs = toCountQuery({ ...filters, owner_agent_id: undefined });
  return useQuery<WorkItemOwnerCount[]>({
    queryKey: ["shared-state", "owners", qs],
    queryFn: () => api.get(`/shared-state/owners${qs ? `?${qs}` : ""}`),
    enabled,
  });
}

export function useDeleteSharedState() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (id) => api.delete(`/shared-state/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["shared-state"] }),
  });
}

/** Resolve a specific entry (terminal) by its id via the dedicated endpoint.
 *
 * Resolving by id (not key) targets the exact row shown: multiple visible rows
 * (own + company-managed) can share a key, and going through the upsert endpoint
 * could resolve a different row or, racing a delete/purge, create a phantom
 * resolved entry. The endpoint 404s when the row is gone (resolve never creates).
 */
export function useResolveSharedState() {
  const qc = useQueryClient();
  return useMutation<SharedState, Error, string>({
    mutationFn: (id) => api.post(`/shared-state/${id}/resolve`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["shared-state"] }),
  });
}

interface BulkDeleteSharedStateRequest {
  ids?: string[];
  all_matching_filters?: boolean;
  search?: string;
  status?: SharedStateStatus;
  stale_only?: boolean;
  vis?: Visibility;
}

export function useBulkDeleteSharedState() {
  const qc = useQueryClient();
  return useMutation<{ deleted: number }, Error, BulkDeleteSharedStateRequest>({
    mutationFn: (body) => api.post("/shared-state/bulk-delete", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["shared-state"] }),
  });
}
