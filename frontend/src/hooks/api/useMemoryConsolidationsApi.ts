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

/** Memory Steward consolidations (#8258): the caller's own lanes, and an agent's own lane for its managers. */

import { type QueryClient, useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { MemoryConsolidation, MemoryConsolidationList } from "@/types";

const MY_KEY = "my-memory-consolidations";
const AGENT_KEY = "agent-memory-consolidations";

/** The caller's memory changed: a purge also deletes its lanes' history, so the rows and their Undo go too. */
export function invalidateMyMemory(qc: QueryClient) {
  qc.invalidateQueries({ queryKey: ["my-memory"] });
  qc.invalidateQueries({ queryKey: [MY_KEY] });
}

export function useMyMemoryConsolidations() {
  return useQuery<MemoryConsolidationList>({
    queryKey: [MY_KEY],
    queryFn: () => api.get("/settings/me/memory/consolidations"),
  });
}

export function useUndoMyMemoryConsolidation() {
  const qc = useQueryClient();
  return useMutation<MemoryConsolidation, Error, string>({
    mutationFn: (id: string) => api.post(`/settings/me/memory/consolidations/${id}/undo`, {}),
    onSettled: () => invalidateMyMemory(qc),
  });
}

export function useAgentMemoryConsolidations(agentId: string | undefined, enabled = true) {
  return useQuery<MemoryConsolidationList>({
    queryKey: [AGENT_KEY, agentId],
    queryFn: () => api.get(`/agents/${agentId}/memory/consolidations`),
    enabled: !!agentId && enabled,
  });
}

export function useUndoAgentMemoryConsolidation(agentId: string) {
  const qc = useQueryClient();
  return useMutation<MemoryConsolidation, Error, string>({
    mutationFn: (id: string) => api.post(`/agents/${agentId}/memory/consolidations/${id}/undo`, {}),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: [AGENT_KEY, agentId] });
      qc.invalidateQueries({ queryKey: ["agent-memories", agentId] });
    },
  });
}
