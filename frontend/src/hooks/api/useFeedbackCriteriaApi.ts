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

/** Feedback criteria hooks: the dimensions a rating of agent work is scored on. */

import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { FeedbackCriterion, FeedbackCriterionCreate, FeedbackCriterionUpdate } from "@/types";

const CRITERIA_KEY = ["feedback-criteria"] as const;

function invalidateCriteria(qc: QueryClient) {
  qc.invalidateQueries({ queryKey: CRITERIA_KEY });
  // A criterion's weight or options change what every rating is worth.
  qc.invalidateQueries({ queryKey: ["agent-annotation-summary"] });
  qc.invalidateQueries({ queryKey: ["agent-annotations"] });
  qc.invalidateQueries({ queryKey: ["annotation"] });
}

function criteriaQuery(includeDisabled: boolean) {
  return {
    queryKey: [...CRITERIA_KEY, includeDisabled],
    queryFn: () =>
      api.get<FeedbackCriterion[]>(`/feedback-criteria${includeDisabled ? "?include_disabled=true" : ""}`),
    // Definitions change rarely; the rating dialog must not refetch them on
    // every thumb.
    staleTime: 60_000,
  };
}

/**
 * The criteria in display order. Everyone reads the enabled ones (the rating
 * dialog draws them); `includeDisabled` is honoured for admins only.
 * `throwOnError: false` for a caller that renders without them.
 */
export function useFeedbackCriteria(
  options: { includeDisabled?: boolean; enabled?: boolean; throwOnError?: boolean } = {},
) {
  return useQuery<FeedbackCriterion[]>({
    ...criteriaQuery(options.includeDisabled ?? false),
    enabled: options.enabled ?? true,
    ...(options.throwOnError === false ? { throwOnError: false } : {}),
  });
}

/**
 * The enabled criteria for the thumbs of a page that exists without them (a
 * chat message, a run, a task): a failed read is a platform with no criteria,
 * never an ErrorBoundary, and the thumbs never wait for the first read -
 * `resolveCriteria()` gives the list when a thumb needs to decide.
 */
export function useRatingCriteria() {
  const qc = useQueryClient();
  const { data: criteria = [] } = useFeedbackCriteria({ throwOnError: false });
  const resolveCriteria = () =>
    qc.ensureQueryData(criteriaQuery(false)).catch((): FeedbackCriterion[] => []);
  return { criteria, resolveCriteria };
}

export function useCreateFeedbackCriterion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: FeedbackCriterionCreate) => api.post<FeedbackCriterion>("/feedback-criteria", body),
    onSuccess: () => invalidateCriteria(qc),
  });
}

export function useUpdateFeedbackCriterion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: FeedbackCriterionUpdate & { id: string }) =>
      api.put<FeedbackCriterion>(`/feedback-criteria/${id}`, body),
    onSuccess: () => invalidateCriteria(qc),
  });
}

export function useDeleteFeedbackCriterion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete<void>(`/feedback-criteria/${id}`),
    onSuccess: () => invalidateCriteria(qc),
  });
}
