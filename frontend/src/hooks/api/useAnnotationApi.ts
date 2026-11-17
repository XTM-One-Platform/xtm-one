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

/** Annotation hooks: a user's rating of a piece of agent work, and an agent's feedback. */

import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type {
  AgentAnnotationListResponse,
  AgentAnnotationSummary,
  Annotation,
  AnnotationTargetType,
  AnnotationUpsert,
  MessageFeedbackRating,
} from "@/types";

function annotationPath(targetType: AnnotationTargetType, targetId: string): string {
  return `/annotations/${targetType}/${targetId}`;
}

function invalidateAgentFeedback(qc: QueryClient) {
  qc.invalidateQueries({ queryKey: ["agent-annotations"] });
  qc.invalidateQueries({ queryKey: ["agent-annotation-summary"] });
}

/** The caller's own rating of one target — `null` when they have none.
 *  Like `useMyAnnotations`, never thrown to the page: the thumbs sit on pages
 *  (a run, a task list, a delegation) that exist without the rating. */
export function useMyAnnotation(
  targetType: AnnotationTargetType,
  targetId: string | undefined,
  options: { enabled?: boolean } = {},
) {
  return useQuery<Annotation | null>({
    queryKey: ["annotation", targetType, targetId],
    queryFn: () => api.get<Annotation | null>(annotationPath(targetType, targetId as string)),
    enabled: (options.enabled ?? true) && !!targetId,
    throwOnError: false,
  });
}

/**
 * The caller's own ratings of many targets in one request - a list page's rows,
 * each of which renders `AnnotationButtons batched` instead of fetching its own.
 * Every target is seeded into the `useMyAnnotation` cache (`null` when not
 * rated), each time the page reads - the rows never refetch on their own -
 * except a rating written after the request left: it is newer than what the
 * request read. When the batch fails, the page renders its rows unbatched, so
 * each reads its own instead of waiting for a seed that never comes.
 */
export function useMyAnnotations(targetType: AnnotationTargetType, targetIds: readonly string[]) {
  const qc = useQueryClient();
  const ids = [...new Set(targetIds)].sort();
  return useQuery<Annotation[]>({
    queryKey: ["my-annotations", targetType, ids],
    queryFn: async () => {
      const query = new URLSearchParams({ target_type: targetType });
      for (const id of ids) query.append("target_id", id);
      const sentAt = Date.now();
      const rows = await api.get<Annotation[]>(`/annotations/mine?${query.toString()}`);
      const byTarget = new Map(rows.map((row) => [row.target_id, row]));
      for (const id of ids) {
        const key = ["annotation", targetType, id];
        if ((qc.getQueryState(key)?.dataUpdatedAt ?? 0) < sentAt) qc.setQueryData(key, byTarget.get(id) ?? null);
      }
      return rows;
    },
    enabled: ids.length > 0,
    throwOnError: false,
  });
}

/** Set the caller's rating of one target; rating again replaces it. */
export function useUpsertAnnotation(targetType: AnnotationTargetType, targetId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: AnnotationUpsert) => api.post<Annotation>(annotationPath(targetType, targetId), body),
    onSuccess: (annotation) => {
      qc.setQueryData(["annotation", targetType, targetId], annotation);
      invalidateAgentFeedback(qc);
    },
  });
}

/** Retract the caller's rating of one target. */
export function useDeleteAnnotation(targetType: AnnotationTargetType, targetId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.delete<void>(annotationPath(targetType, targetId)),
    onSuccess: () => {
      qc.setQueryData(["annotation", targetType, targetId], null);
      invalidateAgentFeedback(qc);
    },
  });
}

interface AgentAnnotationFilters {
  /** One kind of rated work, or several (any of them). */
  targetType?: AnnotationTargetType | readonly AnnotationTargetType[];
  rating?: MessageFeedbackRating;
  search?: string;
  /** Column the API orders by: the rating's date (default), its rating or the rated work's kind. */
  sort?: "date" | "rating" | "kind";
  direction?: "asc" | "desc";
  limit?: number;
  offset?: number;
}

/** Every rating of one agent's work, newest first unless `sort` says otherwise (agent managers only). */
export function useAgentAnnotations(
  agentId: string | undefined,
  filters: AgentAnnotationFilters = {},
  options: { enabled?: boolean; refetchInterval?: number; throwOnError?: boolean } = {},
) {
  const query = new URLSearchParams();
  if (agentId) query.set("agent_id", agentId);
  for (const type of typeof filters.targetType === "string" ? [filters.targetType] : (filters.targetType ?? [])) {
    query.append("target_type", type);
  }
  if (filters.rating) query.set("rating", filters.rating);
  if (filters.search) query.set("search", filters.search);
  if (filters.sort) query.set("sort", filters.sort);
  if (filters.direction) query.set("direction", filters.direction);
  if (filters.limit !== undefined) query.set("limit", String(filters.limit));
  if (filters.offset !== undefined) query.set("offset", String(filters.offset));
  return useQuery<AgentAnnotationListResponse>({
    queryKey: ["agent-annotations", agentId, filters],
    queryFn: () => api.get(`/annotations?${query.toString()}`),
    enabled: (options.enabled ?? true) && !!agentId,
    refetchInterval: options.refetchInterval,
    ...(options.throwOnError === undefined ? {} : { throwOnError: options.throwOnError }),
  });
}

/** Positive / negative counts of one agent's work, per target type (agent managers only). */
export function useAgentAnnotationSummary(
  agentId: string | undefined,
  options: { enabled?: boolean; refetchInterval?: number; throwOnError?: boolean } = {},
) {
  return useQuery<AgentAnnotationSummary>({
    queryKey: ["agent-annotation-summary", agentId],
    queryFn: () => api.get(`/annotations/summary?agent_id=${encodeURIComponent(agentId as string)}`),
    enabled: (options.enabled ?? true) && !!agentId,
    refetchInterval: options.refetchInterval,
    ...(options.throwOnError === undefined ? {} : { throwOnError: options.throwOnError }),
  });
}
