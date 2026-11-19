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
 * Platform Health (#8254): the improvement opt-in of an agent or a flow, the user's
 * opt-out, Settings > Agents and automations > Platform Health and the budget line. Built in since
 * #101 (S1): no feature flag gates these routes; callers pass `enabled` for their own reasons.
 * Level 8 (#8261): cycles and their digests, signals, schedule marks, "Improve
 * with {name}" and the Improvements inbox (Approve / Reject are level 3's).
 */
import { hashKey, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { defaultHealthName } from "@/lib/health";
import { syncFlowNodeCatalog } from "@/lib/flow-node-catalog";
import { REFETCH_INTERVAL_STANDARD } from "@/lib/constants";
import type {
  HealthBudget,
  HealthCycle,
  HealthCycleStart,
  HealthName,
  HealthOptIn,
  HealthScheduleMark,
  HealthSettings,
  HealthSignal,
  HealthTargetType,
  ImprovementEvidenceLink,
  ImprovementProposal,
  ImprovementListFilters,
  ImprovementListResponse,
} from "@/types";

const SETTINGS_KEY = ["health", "settings"] as const;
const BUDGET_KEY = ["health", "budget"] as const;
const OPT_OUT_KEY = ["health", "opt-out"] as const;
const NAME_KEY = ["health", "name"] as const;

/** What a manager opts in to improvement: an agent or a flow, on the same route shape. */
export type OptInTarget = "agent" | "flow";

const optInKey = (targetType: OptInTarget, targetId: string) => ["health", `${targetType}-opt-in`, targetId] as const;

export function useHealthOptIn(targetType: OptInTarget, targetId: string, enabled = true) {
  return useQuery<HealthOptIn>({
    queryKey: optInKey(targetType, targetId),
    queryFn: () => api.get(`/health/${targetType}s/${targetId}/opt-in`),
    enabled: enabled && !!targetId,
  });
}

export function useSetHealthOptIn(targetType: OptInTarget, targetId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (optIn: boolean) =>
      api.put<HealthOptIn>(`/health/${targetType}s/${targetId}/opt-in`, { opt_in: optIn }),
    onSuccess: (data) => queryClient.setQueryData(optInKey(targetType, targetId), { ...data, eligible: true }),
  });
}

export function useHealthOptOut(enabled = true) {
  return useQuery<{ opt_out: boolean }>({
    queryKey: OPT_OUT_KEY,
    queryFn: () => api.get("/health/me/opt-out"),
    enabled,
  });
}

export function useSetHealthOptOut() {
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
export function useHealthName(enabled = true): string {
  const { data } = useQuery<HealthName>({
    queryKey: NAME_KEY,
    queryFn: () => api.get("/health/name"),
    enabled,
    staleTime: 5 * 60_000,
    // The default name stands in: a failed read never takes the page down.
    throwOnError: false,
  });
  return data?.name ?? defaultHealthName();
}

export function useHealthSettings(enabled = true) {
  return useQuery<HealthSettings>({
    queryKey: SETTINGS_KEY,
    queryFn: () => api.get("/health/settings"),
    enabled,
  });
}

export function useSaveHealthSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (settings: HealthSettings) => api.put<HealthSettings>("/health/settings", settings),
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

export function useHealthBudget(enabled = true) {
  return useQuery<HealthBudget>({
    queryKey: BUDGET_KEY,
    queryFn: () => api.get("/health/budget"),
    enabled,
    staleTime: 30_000,
  });
}

interface TargetRef {
  targetType?: HealthTargetType;
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
export function useHealthCycles(target: TargetRef = {}, options: { status?: string; enabled?: boolean } = {}) {
  return useQuery<HealthCycle[]>({
    queryKey: ["health-cycles", target.targetType ?? null, target.targetId ?? null, options.status ?? null],
    queryFn: () => api.get(`/health/cycles${targetParams(target, { status: options.status })}`),
    enabled: options.enabled ?? true,
    throwOnError: false,
    refetchInterval: (query) =>
      query.state.data?.some((cycle) => cycle.status === "open") ? REFETCH_INTERVAL_STANDARD : false,
  });
}

export function useHealthSignals(target: TargetRef = {}, options: { enabled?: boolean } = {}) {
  return useQuery<HealthSignal[]>({
    queryKey: ["health-signals", target.targetType ?? null, target.targetId ?? null],
    queryFn: () => api.get(`/health/signals${targetParams(target)}`),
    enabled: options.enabled ?? true,
    throwOnError: false,
  });
}

export function useHealthScheduleMarks() {
  return useQuery<HealthScheduleMark[]>({
    queryKey: ["health-schedule-marks"],
    queryFn: () => api.get("/health/schedules"),
    throwOnError: false,
  });
}

/** "Improve with {name}" on an agent or a flow. */
export function useRequestHealthCycle() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { target_type: "agent" | "flow"; target_id: string }) =>
      api.post<HealthCycleStart>("/health/cycles", body),
    // The button says why nothing started (`healthRefusalKey`).
    meta: { suppressGlobalErrorToast: true },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["health-cycles"] }),
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
