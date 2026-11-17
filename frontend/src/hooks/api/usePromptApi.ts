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

/** Prompt API hooks. */

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useEntityListScope } from "@/hooks/useEntityListScope";
import { api } from "@/lib/api";
import { entityListKey, entityListPath } from "@/lib/entity-list-scope";
import type {
  EntityListOptions,
  Prompt,
  CreatePromptRequest,
  VersionDiffResponse,
  VersionListResponse,
} from "@/types";

// --- Prompts (Reusable prompt templates) ---
/** The prompts list; `{ scope: "all" }` is every prompt for an admin (detail pages). */
export function usePrompts(options?: EntityListOptions) {
  const scope = useEntityListScope(options);
  return useQuery<Prompt[]>({
    queryKey: entityListKey(["prompts"], scope),
    queryFn: () => api.get(entityListPath("/prompts", scope)),
  });
}

export function usePrompt(id: string) {
  return useQuery<Prompt>({
    queryKey: ["prompt", id],
    queryFn: () => api.get(`/prompts/${id}`),
    enabled: !!id,
  });
}

export function useCreatePrompt() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: CreatePromptRequest) => api.post("/prompts", data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["prompts"] }),
  });
}

export function useUpdatePrompt() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...data }: { id: string; user_id?: string | null } & Partial<CreatePromptRequest>) =>
      api.put(`/prompts/${id}`, data),
    onSuccess: (_d, { id }) => {
      qc.invalidateQueries({ queryKey: ["prompts"] });
      qc.invalidateQueries({ queryKey: ["prompt", id] });
      qc.invalidateQueries({ queryKey: ["prompt-versions", id] });
    },
  });
}

export function usePromptVersions(
  promptId: string,
  params: { limit?: number; offset?: number } = {},
) {
  const query = new URLSearchParams();
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  if (params.offset !== undefined) query.set("offset", String(params.offset));
  const qs = query.toString();
  return useQuery<VersionListResponse>({
    queryKey: ["prompt-versions", promptId, params],
    queryFn: () => api.get(`/prompts/${promptId}/versions${qs ? `?${qs}` : ""}`),
    enabled: !!promptId,
    placeholderData: (prev) => prev,
  });
}

export function usePromptVersionDiff(
  promptId: string,
  fromVersion: number | null,
  toVersion: number | null,
  enabled = true,
) {
  return useQuery<VersionDiffResponse>({
    queryKey: ["prompt-version-diff", promptId, fromVersion, toVersion],
    queryFn: () => api.get(`/prompts/${promptId}/versions/diff?from=${fromVersion}&to=${toVersion}`),
    enabled: !!promptId && fromVersion !== null && toVersion !== null && enabled,
  });
}

export function useRollbackPromptVersion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      promptId,
      version,
      commit_message,
    }: {
      promptId: string;
      version: number;
      commit_message?: string;
    }) => api.post(`/prompts/${promptId}/versions/${version}/rollback`, { commit_message }),
    onSuccess: (_d, { promptId }) => {
      qc.invalidateQueries({ queryKey: ["prompts"] });
      qc.invalidateQueries({ queryKey: ["prompt", promptId] });
      qc.invalidateQueries({ queryKey: ["prompt-versions", promptId] });
      qc.invalidateQueries({ queryKey: ["prompt-version-diff", promptId] });
    },
  });
}

export function useDeletePromptVersion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ promptId, version }: { promptId: string; version: number }) =>
      api.delete(`/prompts/${promptId}/versions/${version}`),
    onSuccess: (_d, { promptId }) => {
      qc.invalidateQueries({ queryKey: ["prompt-versions", promptId] });
      qc.invalidateQueries({ queryKey: ["prompt-version-diff", promptId] });
    },
  });
}

export function useLockPromptVersion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ promptId, version }: { promptId: string; version: number }) =>
      api.post(`/prompts/${promptId}/versions/${version}/lock`, {}),
    onSuccess: (_d, { promptId }) => {
      qc.invalidateQueries({ queryKey: ["prompt-versions", promptId] });
    },
  });
}

export function useUnlockPromptVersion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ promptId, version }: { promptId: string; version: number }) =>
      api.post(`/prompts/${promptId}/versions/${version}/unlock`, {}),
    onSuccess: (_d, { promptId }) => {
      qc.invalidateQueries({ queryKey: ["prompt-versions", promptId] });
    },
  });
}

export function useDeletePrompt() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/prompts/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["prompts"] }),
  });
}
