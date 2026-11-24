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
 * The one decision inbox (#842, S5; dev-docs/human-gates.md section 4):
 * `GET /decisions/waiting` lists every open decision of the reader whatever
 * asked it, `POST /decisions/{id}/answer` answers any of them.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { REFETCH_INTERVAL_STANDARD } from "@/lib/constants";

/** Where a decision comes from (the `decisions.source_type` column). */
type DecisionSourceType = "run" | "flow_run" | "work_item";

/** The ask a decision carries (`decision_ask.normalize_ask`). */
interface DecisionAsk {
  question?: string;
  context?: string;
  options?: { key: string; label: string; effect?: string | null }[];
  recommendation?: string | null;
  deadline?: string | null;
  links?: { label: string; url: string }[];
}

/** One row of the inbox: the columns of section 4 and what the card needs. */
export interface WaitingDecision {
  id: string;
  source_type: DecisionSourceType;
  source_id: string;
  kind: "hitl" | "tool_approval" | "flow_ask" | "work_item";
  question: string;
  ask: DecisionAsk;
  origin: string | null;
  agent: string | null;
  asked_at: string | null;
  deadline: string | null;
  link: string;
  /** False on a fleet read for a decision addressed to someone else: shown read only (#1493). */
  can_answer?: boolean;
}

export interface WaitingDecisions {
  /** False while the read switch is off: the per-engine views stay. */
  enabled: boolean;
  reason: string | null;
  items: WaitingDecision[];
}

const WAITING_DECISIONS_KEY = ["decisions", "waiting"] as const;

/**
 * @param fleetId the fleet's Approval tab (#1493): only the decisions that fleet raised, under
 *   the same query key root so an answer anywhere refreshes it.
 */
export function useWaitingDecisions(enabled = true, fleetId?: string) {
  return useQuery<WaitingDecisions>({
    queryKey: fleetId ? [...WAITING_DECISIONS_KEY, { fleet: fleetId }] : WAITING_DECISIONS_KEY,
    queryFn: () =>
      fleetId ? api.get(`/decisions/waiting?fleet=${encodeURIComponent(fleetId)}&limit=200`) : api.get("/decisions/waiting"),
    enabled,
    refetchInterval: REFETCH_INTERVAL_STANDARD,
    // A failed read stays on its surface (an inline line), never the page error boundary.
    throwOnError: false,
    retry: 1,
  });
}

/**
 * Answer one decision from the inbox (surface `inbox`). The card toasts a
 * failure itself with a translated hint, so the global toast stays quiet.
 */
export function useAnswerDecision() {
  const qc = useQueryClient();
  return useMutation<unknown, Error, { id: string; option?: string; text?: string }>({
    mutationFn: ({ id, ...body }) => api.post(`/decisions/${encodeURIComponent(id)}/answer`, { ...body, surface: "inbox" }),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: async () => {
      await Promise.all([
        qc.invalidateQueries({ queryKey: WAITING_DECISIONS_KEY }),
        qc.invalidateQueries({ queryKey: ["work-items"] }),
      ]);
    },
  });
}
