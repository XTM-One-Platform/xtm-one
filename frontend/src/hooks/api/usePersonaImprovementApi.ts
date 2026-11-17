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

/** Persona self-improvement hooks: ask for a rewrite, approve or reject it, follow its verdict. */

import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { REFETCH_INTERVAL_STANDARD } from "@/lib/constants";
import { cycleSettled, inFlight } from "@/lib/persona-improvements";
import type { ImprovementProposal, ImprovementProposalList } from "@/types";

/** An approval or a settled cycle changes the agent's persona and its version history. */
function invalidateAgentPersona(qc: QueryClient, agentId: string) {
  qc.invalidateQueries({ queryKey: ["agent", agentId] });
  qc.invalidateQueries({ queryKey: ["agent-persona-versions", agentId] });
  qc.invalidateQueries({ queryKey: ["agent-persona-version-diff", agentId] });
}

function invalidate(qc: QueryClient, agentId: string) {
  qc.invalidateQueries({ queryKey: ["persona-improvements", agentId] });
  invalidateAgentPersona(qc, agentId);
}

/** Every rewrite cycle of the agent's persona, newest first; polled while one is being judged.
 *  The poll is what sees the scheduler's verdict, including a rollback. */
export function usePersonaImprovements(agentId: string | undefined) {
  const qc = useQueryClient();
  return useQuery<ImprovementProposalList>({
    queryKey: ["persona-improvements", agentId],
    queryFn: async () => {
      const before = qc.getQueryData<ImprovementProposalList>(["persona-improvements", agentId]);
      const list = await api.get<ImprovementProposalList>(
        `/improvement-proposals?agent_id=${encodeURIComponent(agentId as string)}`,
      );
      if (cycleSettled(before, list)) invalidateAgentPersona(qc, agentId as string);
      return list;
    },
    enabled: !!agentId,
    // The card is a side-show of the Versioning tab: a failed read hides it, never the tab.
    throwOnError: false,
    refetchInterval: (query) =>
      query.state.data?.items.some((item) => inFlight(item.status)) ? REFETCH_INTERVAL_STANDARD : false,
  });
}

export function useProposePersonaImprovement(agentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (evaluationId: string) =>
      api.post<ImprovementProposal>("/improvement-proposals/persona", { agent_id: agentId, evaluation_id: evaluationId }),
    onSuccess: () => invalidate(qc, agentId),
  });
}

export function useApprovePersonaImprovement(agentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (improvementId: string) =>
      api.post<ImprovementProposal>(`/improvement-proposals/${improvementId}/approve`, {}),
    onSuccess: () => invalidate(qc, agentId),
  });
}

export function useRejectPersonaImprovement(agentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ improvementId, reason }: { improvementId: string; reason?: string | null }) =>
      api.post<ImprovementProposal>(`/improvement-proposals/${improvementId}/reject`, { reason: reason ?? null }),
    onSuccess: () => invalidate(qc, agentId),
  });
}
