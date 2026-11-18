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
 * Platform Concierge (#8254): the improvement opt-in of an agent or a flow, the user's
 * opt-out, Settings > Agents and automations > Concierge and the budget line. Built in since
 * #101 (S1): no feature flag gates these routes; callers pass `enabled` for their own reasons.
 * Level 8 (#8261): cycles and their digests, signals, schedule marks, "Improve
 * with Concierge" and the Improvements inbox (Approve / Reject are level 3's).
 */
import { hashKey, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { defaultConciergeName } from "@/lib/concierge";
import { syncFlowNodeCatalog } from "@/lib/flow-node-catalog";
import { REFETCH_INTERVAL_STANDARD } from "@/lib/constants";
import type {
  ConciergeBudget,
  ConciergeCycle,
  ConciergeCycleStart,
  ConciergeName,
  ConciergeOptIn,
  ConciergeScheduleMark,
  ConciergeSettings,
  ConciergeSignal,
  ConciergeTargetType,
  ImprovementEvidenceLink,
  ImprovementProposal,
  ImprovementListFilters,
  ImprovementListResponse,
} from "@/types";

const SETTINGS_KEY = ["concierge", "settings"] as const;
const BUDGET_KEY = ["concierge", "budget"] as const;
const OPT_OUT_KEY = ["concierge", "opt-out"] as const;
const NAME_KEY = ["concierge", "name"] as const;

/** What a manager opts in to improvement: an agent or a flow, on the same route shape. */
export type OptInTarget = "agent" | "flow";

const optInKey = (targetType: OptInTarget, targetId: string) => ["concierge", `${targetType}-opt-in`, targetId] as const;

export function useConciergeOptIn(targetType: OptInTarget, targetId: string, enabled = true) {
  return useQuery<ConciergeOptIn>({
    queryKey: optInKey(targetType, targetId),
    queryFn: () => api.get(`/health/${targetType}s/${targetId}/opt-in`),
    enabled: enabled && !!targetId,
  });
}

export function useSetConciergeOptIn(targetType: OptInTarget, targetId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (optIn: boolean) =>
      api.put<ConciergeOptIn>(`/health/${targetType}s/${targetId}/opt-in`, { opt_in: optIn }),
    onSuccess: (data) => queryClient.setQueryData(optInKey(targetType, targetId), { ...data, eligible: true }),
  });
}

export function useConciergeOptOut(enabled = true) {
  return useQuery<{ opt_out: boolean }>({
    queryKey: OPT_OUT_KEY,
    queryFn: () => api.get("/health/me/opt-out"),
    enabled,
  });
}

export function useSetConciergeOptOut() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (optOut: boolean) => api.put<{ opt_out: boolean }>("/health/me/opt-out", { opt_out: optOut }),
    onSuccess: (data) => queryClient.setQueryData(OPT_OUT_KEY, data),
  });
}

/**
 * The name Platform Health goes by with people (Settings > Automations > Platform Health, #8261, #101).
 * It is an administrator's text: render it as text or pass it as an ICU
 * value (`t_i18n("{name} is on it", { name })`), never as a message id.
 */
export function useConciergeName(enabled = true): string {
  const { data } = useQuery<ConciergeName>({
    queryKey: NAME_KEY,
    queryFn: () => api.get("/health/name"),
    enabled,
    staleTime: 5 * 60_000,
    // The default name stands in: a failed read never takes the page down.
    throwOnError: false,
  });
  return data?.name ?? defaultConciergeName();
}

export function useConciergeSettings(enabled = true) {
  return useQuery<ConciergeSettings>({
    queryKey: SETTINGS_KEY,
    queryFn: () => api.get("/health/settings"),
    enabled,
  });
}

export function useSaveConciergeSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (settings: ConciergeSettings) => api.put<ConciergeSettings>("/health/settings", settings),
    onSuccess: (data) => {
      qc.setQueryData(SETTINGS_KEY, data);
      void qc.invalidateQueries({ queryKey: BUDGET_KEY });
      void qc.invalidateQueries({ queryKey: NAME_KEY });
      // The orchestrator agent is renamed with it, in the lists and the flow node catalog.
      void qc.invalidateQueries({ queryKey: ["agents"] });
      syncFlowNodeCatalog(qc);
    },
  });
}

export function useConciergeBudget(enabled = true) {
  return useQuery<ConciergeBudget>({
    queryKey: BUDGET_KEY,
    queryFn: () => api.get("/health/budget"),
    enabled,
    staleTime: 30_000,
  });
}

interface TargetRef {
  targetType?: ConciergeTargetType;
  targetId?: string;
}

function targetParams({ targetType, targetId }: TargetRef, extra: Record<string, string | undefined> = {}) {
  const params = new URLSearchParams();
  if (targetType && targetId) {
    params.set("target_type", targetType);
    params.set("target_id", targetId);
  }
  for (const [key, value] of Object.entries(extra)) if (value) params.set(key, value);
  const query = params.toString();
  return query ? `?${query}` : "";
}

/** The cycles the viewer may read, newest first; polled while one is open. */
export function useConciergeCycles(target: TargetRef = {}, options: { status?: string; enabled?: boolean } = {}) {
  return useQuery<ConciergeCycle[]>({
    queryKey: ["concierge-cycles", target.targetType ?? null, target.targetId ?? null, options.status ?? null],
    queryFn: () => api.get(`/health/cycles${targetParams(target, { status: options.status })}`),
    enabled: options.enabled ?? true,
    throwOnError: false,
    refetchInterval: (query) =>
      query.state.data?.some((cycle) => cycle.status === "open") ? REFETCH_INTERVAL_STANDARD : false,
  });
}

export function useConciergeSignals(target: TargetRef = {}, options: { enabled?: boolean } = {}) {
  return useQuery<ConciergeSignal[]>({
    queryKey: ["concierge-signals", target.targetType ?? null, target.targetId ?? null],
    queryFn: () => api.get(`/health/signals${targetParams(target)}`),
    enabled: options.enabled ?? true,
    throwOnError: false,
  });
}

export function useConciergeScheduleMarks() {
  return useQuery<ConciergeScheduleMark[]>({
    queryKey: ["concierge-schedule-marks"],
    queryFn: () => api.get("/health/schedules"),
    throwOnError: false,
  });
}

/** "Improve with Concierge" on an agent or a flow. */
export function useRequestConciergeCycle() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { target_type: "agent" | "flow"; target_id: string }) =>
      api.post<ConciergeCycleStart>("/health/cycles", body),
    // The button says why nothing started (`conciergeRefusalKey`).
    meta: { suppressGlobalErrorToast: true },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["concierge-cycles"] }),
  });
}

export function useImprovements(filters: ImprovementListFilters, options: { enabled?: boolean } = {}) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (Array.isArray(value)) for (const item of value) params.append(key, String(item));
    else if (value !== undefined && value !== "") params.set(key, String(value));
  }
  const query = params.toString();
  const { limit, offset, ...list } = filters;
  return useQuery<ImprovementListResponse>({
    queryKey: ["improvements", list, { limit, offset }],
    queryFn: () => api.get(`/health/improvements${query ? `?${query}` : ""}`),
    // The page shown stays while the next page of the same list loads, never
    // while another target's or filter's list does.
    placeholderData: (previous, previousQuery) =>
      previousQuery && hashKey([previousQuery.queryKey[1]]) === hashKey([list]) ? previous : undefined,
    enabled: options.enabled ?? true,
    throwOnError: false,
  });
}

export function useImprovementEvidence(proposalId: string | undefined) {
  return useQuery<ImprovementEvidenceLink[]>({
    queryKey: ["improvement-evidence", proposalId],
    queryFn: () => api.get(`/health/improvements/${encodeURIComponent(proposalId as string)}/evidence`),
    enabled: !!proposalId,
    throwOnError: false,
  });
}

/** One proposal with its diff and measured result: level 3's route (#8256). */
export function useImprovementProposal(proposalId: string | undefined) {
  return useQuery<ImprovementProposal>({
    queryKey: ["improvement-proposal", proposalId],
    queryFn: () => api.get(`/improvement-proposals/${encodeURIComponent(proposalId as string)}`),
    enabled: !!proposalId,
    throwOnError: false,
  });
}

/** Approve or reject a proposal (level 3's routes); every list that shows it is refreshed. */
export function useDecideImprovementProposal() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, decision, reason }: { id: string; decision: "approve" | "reject"; reason?: string }) =>
      api.post<ImprovementProposal>(
        `/improvement-proposals/${encodeURIComponent(id)}/${decision}`,
        decision === "reject" ? { reason: reason ?? null } : {},
      ),
    // The proposal view says the decision was not recorded, with the server's reason.
    meta: { suppressGlobalErrorToast: true },
    onSuccess: (_data, { id }) => {
      void qc.invalidateQueries({ queryKey: ["improvements"] });
      void qc.invalidateQueries({ queryKey: ["improvement-proposal", id] });
      void qc.invalidateQueries({ queryKey: ["agent"] });
    },
  });
}
