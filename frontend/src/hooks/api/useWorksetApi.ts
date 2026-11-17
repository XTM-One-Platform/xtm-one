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

/** Workset API hooks — read and purge only.
 *
 * There is deliberately no hook that writes a summary or a priority: those are
 * conclusions about a document, and a corpus whose analysis can be hand-edited
 * from the UI stops being able to say where any of it came from.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Visibility } from "@/lib/visibility";
import type { Workset, WorksetDetail, WorksetItemPage } from "@/types";

interface WorksetFilters {
  search?: string;
  /** Lifecycle status — omit for all. */
  status?: "active" | "resolved";
  /** run | flow | global, or several comma-separated (the table's Scope filter). */
  scope?: string;
  /** The run or flow id a corpus is scoped to — what the run panel asks for. */
  scopeRef?: string;
  /** The visibility segment (`lib/visibility.ts`); omitted = every workset the caller can see. */
  vis?: Visibility;
  /** A workspace's Operations tab: filed in it, or owned by its fleet's members. */
  workspace?: string;
  /** A fleet: owned by its current home members. */
  fleet?: string;
  limit?: number;
  offset?: number;
  /** name | items | size | updated | created; omitted = updated (newest activity first). */
  sort?: string;
  direction?: "asc" | "desc";
}

function toCountQuery(filters: WorksetFilters): string {
  const params = new URLSearchParams();
  if (filters.search) params.set("search", filters.search);
  if (filters.status) params.set("status_filter", filters.status);
  if (filters.scope) params.set("scope", filters.scope);
  if (filters.scopeRef) params.set("scope_ref", filters.scopeRef);
  if (filters.vis) params.set("vis", filters.vis);
  if (filters.workspace) params.set("workspace", filters.workspace);
  if (filters.fleet) params.set("fleet", filters.fleet);
  return params.toString();
}

function toQuery(filters: WorksetFilters): string {
  const params = new URLSearchParams(toCountQuery(filters));
  if (filters.limit) params.set("limit", String(filters.limit));
  if (filters.offset) params.set("offset", String(filters.offset));
  if (filters.sort) params.set("sort", filters.sort);
  if (filters.direction) params.set("direction", filters.direction);
  return params.toString();
}

/** How many worksets the list holds under the same filters, search included: the table's total. */
export function useWorksetCount(
  filters: WorksetFilters = {},
  options: { enabled?: boolean } = {},
) {
  const qs = toCountQuery(filters);
  return useQuery<{ count: number }>({
    queryKey: ["worksets", "count", qs],
    queryFn: () => api.get(`/worksets/count${qs ? `?${qs}` : ""}`),
    enabled: options.enabled ?? true,
  });
}

export function useWorksets(
  filters: WorksetFilters = {},
  options: { enabled?: boolean; refetchInterval?: number | false } = {},
) {
  const qs = toQuery(filters);
  return useQuery<Workset[]>({
    queryKey: ["worksets", "list", qs],
    queryFn: () => api.get(`/worksets${qs ? `?${qs}` : ""}`),
    enabled: options.enabled ?? true,
    refetchInterval: options.refetchInterval,
  });
}

/** One workset with the same coverage and progress the agents read. */
export function useWorkset(worksetId: string | null) {
  return useQuery<WorksetDetail>({
    queryKey: ["worksets", "detail", worksetId],
    queryFn: () => api.get(`/worksets/${worksetId}`),
    enabled: !!worksetId,
  });
}

export function useWorksetItems(
  worksetId: string | null,
  options: { status?: string; limit?: number; offset?: number } = {},
) {
  const params = new URLSearchParams();
  if (options.status) params.set("status_filter", options.status);
  if (options.limit) params.set("limit", String(options.limit));
  if (options.offset) params.set("offset", String(options.offset));
  const qs = params.toString();
  return useQuery<WorksetItemPage>({
    queryKey: ["worksets", "items", worksetId, qs],
    queryFn: () => api.get(`/worksets/${worksetId}/items${qs ? `?${qs}` : ""}`),
    enabled: !!worksetId,
  });
}

export function useResolveWorkset() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, note }: { id: string; note?: string }) =>
      api.post(`/worksets/${id}/resolve`, { note }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["worksets"] });
    },
  });
}

export function useDeleteWorkset() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/worksets/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["worksets"] });
    },
  });
}
