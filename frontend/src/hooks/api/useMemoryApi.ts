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

/**
 * Shared memory hooks — agent memory served by the makeMemoryHooks factory.
 *
 * The factory currently supports only the "agents" entity path. To add a new
 * memory-backed entity, widen the entityPath union and instantiate the factory
 * with the new path (never duplicate the 7-hook pattern).
 */

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { REFETCH_INTERVAL_SLOW } from "@/lib/constants";
import type { AgentMemoryEntry, AgentMemoryList, AgentMemoryHealth, AgentMemoryMaintenanceResult } from "@/types";

/** The filters, search, order and page of `GET /agents/{id}/memory`. */
export interface MemoryListParams {
  memory_type?: string[];
  source?: string[];
  state?: "current" | "superseded";
  /** Words the content or the subject holds (a text match, no provider). */
  q?: string;
  sort?: "date" | "type";
  direction?: "asc" | "desc";
  limit?: number;
  offset?: number;
}

function makeMemoryHooks<TMaint = AgentMemoryMaintenanceResult>(
  entityPath: "agents",
  keyPrefix: string,
) {
  const memoriesKey = `${keyPrefix}-memories`;
  const searchKey = `${keyPrefix}-memory-search`;
  const healthKey = `${keyPrefix}-memory-health`;

  /**
   * A page of the entries. Every parameter is optional: none is the newest entries first.
   * `throwOnError: false` lets a data table draw its own error row with Retry.
   */
  function useMemories(
    entityId: string | undefined,
    params: MemoryListParams = {},
    options: { throwOnError?: boolean } = {},
  ) {
    const query = new URLSearchParams();
    for (const type of params.memory_type ?? []) query.append("memory_type", type);
    for (const source of params.source ?? []) query.append("source", source);
    if (params.state) query.set("state", params.state);
    if (params.q) query.set("q", params.q);
    if (params.sort) query.set("sort", params.sort);
    if (params.direction) query.set("direction", params.direction);
    if (params.limit) query.set("limit", String(params.limit));
    if (params.offset) query.set("offset", String(params.offset));
    const qs = query.toString();
    return useQuery<AgentMemoryList>({
      queryKey: [memoriesKey, entityId, params],
      queryFn: () => api.get(`/${entityPath}/${entityId}/memory${qs ? `?${qs}` : ""}`),
      enabled: !!entityId,
      ...(options.throwOnError === undefined ? {} : { throwOnError: options.throwOnError }),
    });
  }

  function useSearchMemory(entityId: string | undefined, searchQuery: string, limit = 10) {
    return useQuery<AgentMemoryEntry[]>({
      queryKey: [searchKey, entityId, searchQuery, limit],
      queryFn: () =>
        api.get(`/${entityPath}/${entityId}/memory/search?query=${encodeURIComponent(searchQuery)}&limit=${limit}`),
      enabled: !!entityId && searchQuery.length > 0,
      /* Not thrown to the page ErrorBoundary: this reaches a provider, so it
         is refused with AI off (FR-007), and the global `throwOnError: true`
         turned that refusal into a blank page. The caller shows no results
         instead (/code-review, 2026-08-28). */
      throwOnError: false,
    });
  }

  function useUpdateMemoryEntry(entityId: string | undefined) {
    const qc = useQueryClient();
    return useMutation({
      mutationFn: ({ entryId, content }: { entryId: string; content: string }) =>
        api.put(`/${entityPath}/${entityId}/memory/${entryId}`, { content }),
      onSuccess: () => {
        qc.invalidateQueries({ queryKey: [memoriesKey, entityId] });
        qc.invalidateQueries({ queryKey: [searchKey, entityId] });
      },
    });
  }

  function useDeleteMemoryEntry(entityId: string | undefined) {
    const qc = useQueryClient();
    return useMutation({
      mutationFn: (entryId: string) =>
        api.delete(`/${entityPath}/${entityId}/memory/${entryId}`),
      onSuccess: () => {
        qc.invalidateQueries({ queryKey: [memoriesKey, entityId] });
        qc.invalidateQueries({ queryKey: [searchKey, entityId] });
      },
    });
  }

  function useClearMemory(entityId: string | undefined) {
    const qc = useQueryClient();
    return useMutation({
      mutationFn: () => api.delete(`/${entityPath}/${entityId}/memory`),
      onSuccess: () => {
        qc.invalidateQueries({ queryKey: [memoriesKey, entityId] });
        qc.invalidateQueries({ queryKey: [searchKey, entityId] });
        qc.invalidateQueries({ queryKey: [healthKey, entityId] });
      },
    });
  }

  function useMemoryHealth(entityId: string | undefined) {
    return useQuery<AgentMemoryHealth>({
      queryKey: [healthKey, entityId],
      queryFn: () => api.get(`/${entityPath}/${entityId}/memory/health`),
      enabled: !!entityId,
      staleTime: REFETCH_INTERVAL_SLOW,
    });
  }

  function useTriggerMemoryMaintenance(entityId: string | undefined) {
    const qc = useQueryClient();
    return useMutation<TMaint>({
      mutationFn: () => api.post(`/${entityPath}/${entityId}/memory/maintain`),
      onSuccess: () => {
        qc.invalidateQueries({ queryKey: [memoriesKey, entityId] });
        qc.invalidateQueries({ queryKey: [searchKey, entityId] });
        qc.invalidateQueries({ queryKey: [healthKey, entityId] });
      },
    });
  }

  return {
    useMemories,
    useSearchMemory,
    useUpdateMemoryEntry,
    useDeleteMemoryEntry,
    useClearMemory,
    useMemoryHealth,
    useTriggerMemoryMaintenance,
  };
}

// --- Agent Memory ---
const _agent = makeMemoryHooks("agents", "agent");
export const useAgentMemories = _agent.useMemories;
export const useSearchAgentMemory = _agent.useSearchMemory;
export const useUpdateMemoryEntry = _agent.useUpdateMemoryEntry;
export const useDeleteMemoryEntry = _agent.useDeleteMemoryEntry;
export const useClearAgentMemory = _agent.useClearMemory;
export const useAgentMemoryHealth = _agent.useMemoryHealth;
export const useTriggerMemoryMaintenance = _agent.useTriggerMemoryMaintenance;
