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
  HealthAnalysisOverview,
  HealthBudget,
  HealthCycle,
  HealthCycleStart,
  HealthFindingBulkResult,
  HealthFindingList,
  HealthFindingStatus,
  HealthHistoryPoint,
  HealthModule,
  HealthModuleAnalysis,
  HealthModuleCheckReports,
  HealthModuleInvestigation,
  HealthName,
  HealthOverview,
  HealthOptIn,
  HealthScheduleMark,
  HealthSettings,
  HealthSpendLimits,
  HealthSeverity,
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
const ANALYSIS_KEY = ["health", "analysis"] as const;
const SPEND_LIMITS_KEY = ["health", "settings", "spend-limits"] as const;

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
    // The settings section answers every failure itself: one toast, or the refused field inline (UX r1 B1).
    meta: { suppressGlobalErrorToast: true },
    onSuccess: (data) => {
      qc.setQueryData(SETTINGS_KEY, data);
      void qc.invalidateQueries({ queryKey: BUDGET_KEY });
      void qc.invalidateQueries({ queryKey: NAME_KEY });
      void qc.invalidateQueries({ queryKey: ANALYSIS_KEY });
      // The orchestrator agent is renamed with it, in the lists and the flow node catalog.
      void qc.invalidateQueries({ queryKey: ["agents"] });
      syncFlowNodeCatalog(qc);
    },
  });
}

/**
 * Set the "Run checks as" user alone (dev-docs 12.3), from the onboarding step. A user who cannot read a
 * registered module is refused (400) with the modules named; nothing is written then.
 */
export function useSaveHealthRunAs() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (userId: string) => api.put<{ module_run_as_user_id: string | null }>("/health/settings/run-as", { user_id: userId }),
    // The onboarding step shows the refusal under its picker.
    meta: { suppressGlobalErrorToast: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: SETTINGS_KEY });
      void qc.invalidateQueries({ queryKey: ["health-overview"] });
    },
  });
}

/** Whether the spend ceilings are locked at their defaults (#1202) and the variable that unlocks them. */
export function useHealthSpendLimits(enabled = true) {
  return useQuery<HealthSpendLimits>({
    queryKey: SPEND_LIMITS_KEY,
    queryFn: () => api.get("/health/settings/spend-limits"),
    enabled,
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

// ── Health page (#101 S8): module cards, findings, history ─────────────────

export interface HealthFindingFilters {
  module?: HealthModule[];
  severity?: HealthSeverity[];
  category?: string[];
  status?: HealthFindingStatus[];
  limit?: number;
  offset?: number;
}

export function useHealthOverview(options: { enabled?: boolean } = {}) {
  return useQuery<HealthOverview>({
    queryKey: ["health-overview"],
    queryFn: () => api.get("/health/overview"),
    enabled: options.enabled ?? true,
    throwOnError: false,
    refetchInterval: REFETCH_INTERVAL_STANDARD,
  });
}

export function useHealthFindings(filters: HealthFindingFilters, options: { enabled?: boolean } = {}) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (Array.isArray(value)) for (const item of value) params.append(key, String(item));
    else if (value !== undefined) params.set(key, String(value));
  }
  const query = params.toString();
  return useQuery<HealthFindingList>({
    queryKey: ["health-findings", filters],
    queryFn: () => api.get(`/health/findings${query ? `?${query}` : ""}`),
    enabled: options.enabled ?? true,
    // The toolbar and the rows shown stay while a filter's list loads (13bis.10 (2)).
    placeholderData: (previous) => previous,
    throwOnError: false,
  });
}

export function useHealthHistory(days: 7 | 30 | 90) {
  return useQuery<HealthHistoryPoint[]>({
    queryKey: ["health-history", days],
    queryFn: () => api.get(`/health/history?days=${days}`),
    throwOnError: false,
  });
}

function useRefreshHealth() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: ["health-findings"] });
    void qc.invalidateQueries({ queryKey: ["health-overview"] });
  };
}

/** Dismiss open findings with one reason (one id: the row's Dismiss; several: Dismiss all); the cards and the lists refresh. */
export function useDismissHealthFindings() {
  const refresh = useRefreshHealth();
  return useMutation({
    mutationFn: ({ ids, reason, days }: { ids: string[]; reason: string; days: number | null }) =>
      api.post<HealthFindingBulkResult>("/health/findings/dismiss", { ids, reason, days }),
    onSuccess: refresh,
  });
}

// ── Analysis layer (#101 S9): administrators only ────────────────────────

/** The latest analysis per module, the ceilings and the spend: stored rows, no model call. */
export function useHealthAnalysis(options: { enabled?: boolean } = {}) {
  return useQuery<HealthAnalysisOverview>({
    queryKey: ANALYSIS_KEY,
    queryFn: () => api.get("/health/analysis"),
    enabled: options.enabled ?? true,
    throwOnError: false,
  });
}

/** Analyse one module now, within the ceilings; a refusal comes back as the module's status, not an error. */
export function useRunHealthAnalysis() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (module: HealthModule) => api.post<HealthModuleAnalysis>(`/health/analysis/${module}/run`, {}),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ANALYSIS_KEY }),
  });
}

/**
 * "Run checks now" (#101 S15e-2, sections 13.2 and 13.8; "Investigate now" of S14c before it): the
 * module checker's round on every active registration of one module, administrators only. Its
 * `["health-overview"]` invalidation refreshes the "Agentic checks" line too. A per-registration refusal comes back in `items`; a refusal of the
 * whole request (`module_analyst_off`, `no_registration`, AI off) is a 409 the card words itself.
 */
export function useInvestigateHealthModule() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (module: HealthModule) => api.post<HealthModuleInvestigation>(`/health/modules/${module}/investigate`, {}),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["health-overview"] });
      void qc.invalidateQueries({ queryKey: ["health-cycles"] });
    },
  });
}

/**
 * The latest agentic check report of every registration of one module (#101 S15e, section 13.6),
 * administrators only: stored rows, no model call, readable with AI off. Refreshed with the overview.
 */
export function useHealthModuleChecks(module: HealthModule, options: { enabled?: boolean } = {}) {
  return useQuery<HealthModuleCheckReports>({
    queryKey: ["health-overview", "checks", module],
    queryFn: () => api.get(`/health/modules/${module}/checks`),
    enabled: options.enabled ?? true,
    throwOnError: false,
  });
}

/** Bring dismissed findings back to the open list (Restore, Restore all). */
export function useRestoreHealthFindings() {
  const refresh = useRefreshHealth();
  return useMutation({
    mutationFn: ({ ids }: { ids: string[] }) => api.post<HealthFindingBulkResult>("/health/findings/restore", { ids }),
    onSuccess: refresh,
  });
}
